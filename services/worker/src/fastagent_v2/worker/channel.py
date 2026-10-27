from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import quote

import httpx


@dataclass(frozen=True, slots=True)
class SendCommand:
    """What a channel needs in order to deliver one message.

    **`recipient` is why this class has four fields instead of three** (T90 /
    ADR-0095 §三). `conversation_id` used to be the address: the adapter looked
    up the conversation and sent to the customer in it. That is right for a
    customer message and **wrong for every other audience** -- the manager
    digest borrows a conversation's id so the row has a valid foreign key, and
    an adapter that treated it as the address would send a merchant's internal
    backlog summary to that merchant's customer.

    `None` means "the conversation is the address", which is the customer case
    and stays the default. A row whose audience is not the customer must carry
    one, and `OutboxDispatcher` refuses to send it if it does not.

    **This field is outside the `channel-adapter` contract, on purpose and for
    now** (independent review 2026-09-08, P2-1; registered as T214). That
    schema's `request` is `{"type": "object"}` with nothing inside it, so
    `validate_contract` carries `recipient` through without asserting one word
    about it -- the refusal that makes it safe lives in `dispatch_one`, in this
    package, and nowhere in the contract.

    **What that means for the next implementation of `send`.** There are two
    (T58): this one, and `RestrictedWeComProxyClient.send_text(guid=,
    recipient_id=, content=)`, which has zero production callers today and is on
    M1b's path to the real provider. Nothing anywhere maps the triple
    (`channel`, `config_id`, `external_user_id`) onto `guid` / `recipient_id`.
    An adapter written against the contract alone would honour `conversation_id`
    and ignore this field, which is T90 reproduced exactly. Whoever wires it up
    either tightens the schema (a frozen-contract revision, named as such) or
    writes that mapping down with a contract test behind it.
    """

    tenant_id: str
    conversation_id: str
    content: dict[str, object]
    recipient: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SendReceipt:
    status: str
    provider_operation_id: str | None
    evidence: dict[str, object]


#: The canonical shape a queued text message carries. **Written here, next to
#: `SendCommand`, because this is the type that crosses the boundary** -- and
#: because two ends each inventing their own answer is what this constant
#: exists to stop.
TEXT_CONTENT_TYPE = "text"

#: The segment type that names a merchant material instead of carrying words.
#: Spelled once, here, for the same reason `TEXT_CONTENT_TYPE` is: this is the
#: type that crosses the boundary, and `generation-result.schema.json` is the
#: other end of the same statement.
MATERIAL_REF_SEGMENT_TYPE = "material_ref"

#: 一行出站文件消息在**库里**的形状：只有一个引用。
#:
#: **与 `TEXT_CONTENT_TYPE` 同一个理由住在这里**：这是跨边界的那个类型。
#: 而它与文字那一侧有一处本质不同 —— **库里存的不是要发的东西，是去哪儿取它**。
ASSET_CONTENT_TYPE = "asset"

#: 同一行在**内存里**、被派发点解析之后的形状。
#:
#: ## 为什么是两个形状而不是一个
#:
#: `sendFile` 要六个字段，其中 `fileAesKey` 与 `fileMd5` **是凭据**
#: （`ingress/wecom_proxy/inbound.py` 对这一族的判断）。把它们拼进
#: `outbox_messages.content` 就是把凭据复制进另一张表，**而那张表的读面比
#: `materials` 宽得多**：`fastagent_app` 从 `058` 起就读得到出站行，消息页也读它。
#:
#: 所以库里那一行只带引用，四个 `provider_*` 字段在**发送那一刻**才从目录里取
#: （`068` 给 worker 发的那条读权），取出来只活在这个对象里。
FILE_CONTENT_TYPE = "file"


@dataclass(frozen=True, slots=True)
class FileToSend:
    """一次文件发送要的全部，**解析之后**。

    `aes_key` 可空**只在大文件那一档**：`fileId` 以 `*` 开头时供应商允许它为空
    （`channels.wecom_proxy.client.is_big_file` 是那个判据的唯一定义点）。
    这个类不替客户端判那一条 —— 它只是如实带着读到的东西，
    **判据留在最靠近那次调用的地方**，两处各判一遍迟早会有一处宽一点。
    """

    file_id: str
    file_size: int
    file_md5: str
    filename: str
    aes_key: str | None


def customer_file(content: object) -> FileToSend | None:
    """这一行要发的文件，或者 `None` —— 它不是一行文件消息。

    **与 `customer_text` 并排住在这里，而不是在适配器里**：
    2026-09-10 那次出站缺陷的全部教训就是「形状的知识只能有一个定义点」——
    当时适配器认一种形状而模型产出三种，**两端的单测都是绿的**，
    因为两端是照着同一个想象出来的形状写的。

    只认**已解析**的那个形状（`FILE_CONTENT_TYPE`）。库里那个只带引用的形状
    （`ASSET_CONTENT_TYPE`）到不了这里 —— 它在派发点就被换掉了，
    **而如果没被换掉，这里回 `None`**，于是适配器给出一条
    `unsendable_content_type` 的失败回执：一条说得出理由的失败，
    好过带着一个 `asset:...` 字样发给客户。
    """

    if not isinstance(content, Mapping):
        return None
    if str(content.get("type")) != FILE_CONTENT_TYPE:
        return None
    file_id = str(content.get("file_id") or "").strip()
    file_md5 = str(content.get("file_md5") or "").strip()
    filename = str(content.get("filename") or "").strip()
    raw_size = content.get("file_size")
    if not file_id or not file_md5 or not filename:
        return None
    try:
        file_size = int(raw_size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if file_size <= 0:
        return None
    aes_key = str(content.get("aes_key") or "").strip() or None
    return FileToSend(
        file_id=file_id,
        file_size=file_size,
        file_md5=file_md5,
        filename=filename,
        aes_key=aes_key,
    )



def customer_text(content: object, *, materials: Mapping[str, str] | None = None) -> str | None:
    """The words a text channel should send, or `None` if there are none to read.

    **This function is the whole of the 2026-09-10 outbound defect.** The
    platform had *three* content shapes and no single reader:

    * ``{"type": "text", "text": ...}`` -- what the deterministic inference
      double builds (`inference._send_payload`), what the manager-notification
      path builds (`pipeline`, whose comment claims it is "the same shape the
      customer row carries"), and the only one the WeComProxy adapter accepted;
    * ``{"text": ...}`` -- what the **real model** returned for two of the three
      replies in the third real window;
    * ``{"segments": [{"type": "text", "content_or_ref": ...}]}`` -- what it
      returned for the third, mirroring `customer_visible_segments`, which is
      the shape the prompt itself shows it.

    The first shape was asserted by every test on both sides, because both sides
    were tested against the same invented double. The third real window sent
    zero messages: three rows `FAILED` with `unsendable_content_type` and an
    empty `provider_operation_id` -- **the provider was never called**. Both
    halves' unit tests were green throughout, which is the point: *green unit
    tests are not evidence that a boundary is right.*

    So there is now one reader, and both ends use it -- the producer to refuse
    early with a record, the adapter to send. Adding a fourth shape means
    changing this function, and the cases that drive it are built from captured
    production commands rather than from anything written alongside it.

    **`material_ref` segments are resolved when this reader is given the
    materials, and refuse the whole message when it is not.**

    A segment list may legally mix `text` and `material_ref`
    (`generation-result.schema.json`), and a text-only channel that took the
    text half would send *part* of what the model composed with nothing saying
    so -- the same shape of loss inbound spent a batch removing ("a message that
    does not parse does not fail, it silently loses half"). Refusing the whole
    thing is recoverable; sending half of it is not.

    Until the generation step could see the merchant's materials at all, that
    refusal was the end of the story: nothing could ever supply the words behind
    a ref, so a `material_ref` segment was a dead message. `materials` is that
    supply -- ref to text, taken from the frame the turn is standing in
    (`platform_frames.materials_for_frame`) -- and a ref found in it is replaced
    by **the merchant's own text**. That is what makes "quote, do not rewrite" a
    property of the platform rather than a request in a prompt: what reaches the
    customer is the merchant's string whatever wording the model had in mind.

    **Trimmed, not byte-for-byte** (independent review 2026-09-10, P3-1). A
    resolved material goes through `_usable` like every other segment, so
    leading and trailing whitespace is dropped -- "whitespace is not content" is
    this reader's rule for all content, and exempting one segment type would
    make two rules where the platform has one. It is written down because the
    sentence above used to say *exact*, which was false for a material whose
    author had indented it.

    **The default stays `None`, and that is the important half.** The adapter
    calls this with no materials, because by then the outbox row already holds
    resolved text; a ref arriving there means resolution did not happen, and the
    message must not go out with a `material:...:v1` in it. So the same reader
    resolves on the producing side and fails closed on the sending side, without
    either end needing to know which one it is.
    """

    if not isinstance(content, dict):
        return None

    segments = content.get("segments")
    if isinstance(segments, list):
        return _text_of_segments(segments, materials)

    # `type` is checked only when it is present: the model omits it, and the
    # key's job here is to say "this is not text" when something else arrives,
    # not to be a password the model has to guess.
    declared = content.get("type")
    if declared is not None and declared != TEXT_CONTENT_TYPE:
        return None
    return _usable(content.get("text"))


def _text_of_segments(
    segments: list[object], materials: Mapping[str, str] | None
) -> str | None:
    """Join a segment list, or refuse it whole. Order is the model's."""

    if not segments:
        return None
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict):
            return None
        kind = segment.get("type")
        if kind == MATERIAL_REF_SEGMENT_TYPE:
            if materials is None:
                # No supply of materials: this caller cannot resolve a ref, and
                # a ref is not text. Refuse the whole message; see `customer_text`.
                return None
            resolved = _usable(materials.get(str(segment.get("content_or_ref"))))
            if resolved is None:
                # A ref this frame does not carry. `validate_generation` refuses
                # the generation before it gets here, so this is the second lock
                # rather than the first -- and the one that still holds for a
                # payload that reached the outbox by some other road.
                return None
            parts.append(resolved)
            continue
        if kind != TEXT_CONTENT_TYPE:
            return None
        part = _usable(segment.get("content_or_ref"))
        if part is None:
            return None
        parts.append(part)
    return "\n".join(parts)


def _usable(value: object) -> str | None:
    """A non-empty string, or `None`. Whitespace is not content."""

    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


#: The only delivery states the outbox knows how to record. A provider that
#: answers with anything else has told us nothing we can act on, so the send is
#: unconfirmed rather than a crash.
RECEIPT_STATUSES = frozenset({"accepted", "delivered", "failed", "unknown"})

#: The only statuses that prove the message was **not** sent and that an
#: identical retry would be refused identically. Everything else that is not a
#: success is unconfirmed.
#:
#: This list is deliberately an enumeration rather than the range `4xx`. The
#: first version of this adapter inverted it -- `>= 500` meant unknown and every
#: other failure meant `failed` -- which quietly resolved `401`, `403`, `408`,
#: `409`, `429` and `451` to "definitely not delivered". None of those prove
#: that: an expired credential, a rate limit or a proxy-side conflict can all
#: arrive after the provider accepted the send. Under-claiming costs a manual
#: reconcile; over-claiming loses a delivered message with no trace.
#:
#: `404` was in this set until 2026-09-02 and was removed for the same reason.
#: It says the request was never examined -- the route is not there -- which is
#: a configuration fault, exactly like the 3xx case, not a message the provider
#: looked at and refused. With a mistyped send URL every message would have been
#: terminally `FAILED` forever, which is precisely the silent loss this
#: enumeration exists to prevent.
TERMINAL_REFUSAL_STATUSES = frozenset({400, 413, 415, 422})

#: How long a reconcile may take, measured from when it starts and **not** taken
#: from the send's budget.
#:
#: The independent review of L1 (2026-09-04, P1-1) proved with a real socket
#: that the timeout branch could never reach the query it exists for. The send's
#: HTTP timeout is the whole remaining turn budget (`runtime_deadline_at`, ~30s
#: from ingress), so a genuine timeout consumes all of it by construction, and
#: the reconcile that follows finds `remaining <= 0` and returns without sending
#: a packet. RFC `:228` names timeout as *the* case for reconciling; it was the
#: one case the wiring could not serve.
#:
#: The budgets are separate because they answer to different clocks. The send's
#: deadline is the customer's: past it, the reply is late whatever we do. The
#: reconcile's deadline is the ledger's -- nobody is waiting on it, and its only
#: job is to make sure what gets written down is true. Two seconds is enough for
#: one round trip to a provider that is answering at all, and bounded enough
#: that a dead provider cannot stall the dispatcher loop: a stuck reconcile
#: costs 2s per message, not 30.
RECONCILE_BUDGET_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """What this adapter can be **asked**, declared rather than discovered.

    **T78 / ADR-0094 §三**, which is the named revision of ADR-0042 §二.1 and of
    RFC §3.5. ADR-0042 made `reconcile_by_key` a mandatory member of the port,
    on the reasoning that a timeout leaves no operation id and the caller's
    idempotency key is then the only identifier both sides share. That reasoning
    holds; what does not hold is the assumption underneath it -- that a provider
    will answer such a question at all. Real ones mostly do not: a receipt comes
    back with the provider's own message id, and lookup by *the caller's* key is
    not on offer.

    An adapter that cannot do it can only return `unknown`, and that `unknown`
    is byte-for-byte the one meaning "we asked and the provider has no record"
    -- the exact confusion L1 was created to remove, reintroduced by the first
    real adapter. So the port carries the answer explicitly.

    **Why a declaration and not the absence of the method** (ADR-0094 §三
    rejected that option by name): encoding "can it" as "does it have the
    method" hides the answer in the type, where no receipt, no log line and no
    manual task can quote it. `reconcile_by_key` therefore stays a required
    member; this says whether calling it is worth anything.

    **Divergence from RFC §3.5, stated rather than hidden.** The RFC writes this
    member as `async capabilities(binding_id: UUID) -> ChannelCapabilities`. The
    outbound port in this module is the sync, narrowed half of that contract
    (its `send` is sync and keyword-only, its `reconcile` returns `SendReceipt`
    rather than `DeliveryReceipt`), and a worker adapter is built **per
    dispatch** for one binding already resolved (`ChannelFactory`), so a
    `binding_id` argument would be a parameter every implementation ignores --
    the sort of decoration ADR-0112 exists to keep out. The RFC's enumeration
    carries this note; see the revision marker there.
    """

    #: Can the provider be asked "what happened to the send carrying *this*
    #: idempotency key?" -- the caller's key, not an id the provider issued.
    #: False obliges the dispatcher not to ask, and to write down that it did
    #: not, with the reason.
    #:
    #: **Not "saves a round trip"** (independent review 2026-09-09, P2-1). The
    #: only adapter declaring `False` today answers without sending a packet,
    #: so the declaration changes what the receipt says rather than what
    #: crosses the network. It would save a round trip for an adapter whose
    #: `reconcile_by_key` goes to the wire and comes back unable to answer --
    #: which is what a provider offering *some* lookup, but not by the caller's
    #: key, would look like.
    reconcile_by_key: bool


class ChannelAdapter(Protocol):
    def capabilities(self) -> ChannelCapabilities:
        """Declare what this channel can be asked. See `ChannelCapabilities`.

        Every implementation answers; there is no default. A default would be
        the same silence T78 registered, wearing a method name.
        """
        ...

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt: ...

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt: ...

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        """Ask about a send that never told us its operation id.

        `reconcile` needs an id the provider gave us, and the two failures that
        most need reconciling -- a timeout and a transport error -- produce no
        response at all, therefore no id. Without this, "query before you say
        unknown" (RFC `:228`, ADR-0006 item 4) is a rule the timeout path could
        not obey even in principle, which is how it came to be skipped for a
        whole milestone (registered as L1).

        The key is the one the send used, and the provider is required to treat
        it as the idempotency key (RFC 7.2 step 5), so it is the one identifier
        both sides are guaranteed to share.
        """
        ...


class ChannelFactory(Protocol):
    """Build the adapter for **one** dispatch, from the address that row resolved to.

    **Why the dispatcher cannot simply hold an adapter** (B2-3, ADR-0094 §一 Q5):
    `HttpChannelAdapter` is constructed once because the M0 simulator's address
    is a URL that never changes. A real provider adapter is constructed **per
    dispatch** -- `WeComProxyChannelAdapter` takes the sending instance's `guid`
    and the recipient's id as constructor arguments, and both are facts about
    *this row*: the guid comes from the tenant's binding, which a rebind may
    have changed since the row was written, and the recipient from either the
    conversation's contact or the manager's.

    **This stays a `Protocol` here rather than an import of the WeComProxy
    adapter.** The dispatcher resolves an address and asks for an adapter; it
    never learns which provider it got. That is what keeps `pipeline.py` free of
    channel SDK logic -- the same boundary ADR-0110 drew when it put the client
    in `packages/channels` instead of `packages/runtime`.
    """

    def __call__(self, *, guid: str, recipient_id: str) -> ChannelAdapter: ...


class HttpChannelAdapter:
    """Narrow HTTP adapter used by the M0 simulator and future provider adapters.

    Every failure mode this adapter can reach returns a receipt. Raising instead
    would push the decision into the dispatcher loop, where the only available
    answer is to drop the process -- and a channel returning 5xx would then be a
    crash-restart loop rather than a recorded delivery state.
    """

    def __init__(self, send_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._send_url = send_url
        # Only tests pass a transport. The worker constructs the adapter with a
        # URL alone, so no production path can substitute the channel.
        self._transport = transport

    def capabilities(self) -> ChannelCapabilities:
        """This one really can be asked by key: it serves `/by-key/{key}`.

        The M0 simulator is written to the same contract as the send, so a
        by-key lookup is a route it implements and `reconcile_by_key` below is a
        real question with a real answer -- including `404`, which is the
        provider *answering* "no record of this key" and is exactly the evidence
        this declaration keeps distinguishable from "we never asked".
        """

        return ChannelCapabilities(reconcile_by_key=True)

    def _client(self) -> httpx.Client:
        return httpx.Client(trust_env=False, transport=self._transport)

    @staticmethod
    def _status_receipt(
        response: httpx.Response, *, provider_operation_id: str | None
    ) -> SendReceipt | None:
        """Classify a non-2xx response **to a send**, or None when the body should be read.

        The `reconciled` parameter this used to take is gone. It existed so the
        reconcile paths could reuse this classification, and reusing it is what
        let a refused *query* end a message terminally (review 2026-09-04, P1-2).
        Queries now go through `_reconcile_status_receipt`; leaving the knob here
        would only invite the same substitution again.

        `unknown` is the default and `failed` is the exception. Only the codes in
        `TERMINAL_REFUSAL_STATUSES` describe a request the provider examined and
        rejected on its own terms -- malformed, too large, wrong media type,
        unprocessable. Everything else that is not a success stays
        unconfirmed, per RFC §4.2: an unconfirmed send costs one reconcile, a
        wrongly-terminal send loses a delivered message silently.

        A 3xx is called out separately. The client does not follow redirects, so
        one means the endpoint moved and nothing was delivered to it -- but that
        is a configuration fault, not a rejected message, and it should read as
        such in the receipt rather than hide inside the generic bucket.
        """

        if response.is_success:
            return None
        status_code = response.status_code
        evidence: dict[str, object] = {"adapter": "http", "http_status": status_code}
        if status_code in TERMINAL_REFUSAL_STATUSES:
            evidence["reason"] = "provider_rejected_request"
            return SendReceipt(
                status="failed", provider_operation_id=provider_operation_id, evidence=evidence
            )
        if 300 <= status_code < 400:
            evidence["reason"] = "provider_redirected"
        elif status_code >= 500:
            evidence["reason"] = "provider_server_error"
        else:
            evidence["reason"] = "provider_response_inconclusive"
        return SendReceipt(
            status="unknown", provider_operation_id=provider_operation_id, evidence=evidence
        )

    @staticmethod
    def _reconcile_status_receipt(
        response: httpx.Response, *, provider_operation_id: str | None
    ) -> SendReceipt | None:
        """Classify a non-2xx answer **to a query**, which is not the same thing.

        `_status_receipt` may return `failed`, and it is right to: the codes in
        `TERMINAL_REFUSAL_STATUSES` mean the provider examined *the send* and
        refused it on its own terms. Reused on a `GET`, that reasoning does not
        survive the substitution -- a 422 on a lookup says the **lookup** was
        unprocessable, and a malformed question is not evidence about the
        message. The independent review of L1 (2026-09-04, P1-2) walked it end to
        end: `POST 500` then `GET 422` ended the intent in `FAILED`, i.e. a
        possibly-delivered message written off terminally because we asked about
        it badly. `_status_receipt`'s own comment already names that shape --
        "over-claiming loses a delivered message with no trace".

        So a query may only ever **narrow** uncertainty. Anything that is not a
        readable 2xx receipt stays `unknown`, with the status and a reason
        recorded so the difference is visible to a person even though it is not
        visible to the state machine.

        The reason is not one bucket, because 404 is not a refusal. On a by-key
        lookup it is the provider *answering*: "I have no record of this key" --
        the very answer this batch exists to go and collect. The first version of
        this classifier called it `provider_rejected_query`, the same words a 422
        gets, and L2b puts that string in front of the person who picks up the
        manual task: they would have read "the provider refused our query" where
        what happened was "the provider replied, and it has never heard of this
        message" (narrow re-review 2026-09-04). Both still resolve to `unknown` --
        the state machine cannot tell them apart and must not -- but the record
        can, and a person reading it can.
        """

        if response.is_success:
            return None
        status_code = response.status_code
        if status_code == 404:
            reason = "provider_has_no_record"
        elif status_code >= 500:
            reason = "provider_server_error"
        else:
            reason = "provider_rejected_query"
        return SendReceipt(
            status="unknown",
            provider_operation_id=provider_operation_id,
            evidence={
                "adapter": "http",
                "reconciled": True,
                "query_sent": True,
                "http_status": status_code,
                "reason": reason,
            },
        )

    @staticmethod
    def _query_timeout(remaining: float) -> httpx.Timeout:
        """Spend a query's budget once, not once per phase.

        A bare `timeout=2.0` is not a two-second bound: httpx reads a single
        float as the limit for *each* of connect, read, write and pool, so one
        round trip that is slow to connect and then slow to answer can take a
        multiple of it. `_reconcile_before_giving_up` now clamps the query to
        what is left of the outbox lease and the arithmetic there assumes the
        number means what it says (narrow re-review 2026-09-04, P3) -- an
        overrun is not a slow log line, it is another dispatcher claiming the
        row and sending the message twice.

        `send` still hands httpx a plain float; that is the turn budget's
        existing behaviour and out of scope here (registered as T79).
        """

        share = max(0.05, remaining / 4)
        return httpx.Timeout(connect=share, read=share, write=share, pool=share)

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        remaining = (deadline_at - datetime.now(deadline_at.tzinfo)).total_seconds()
        if remaining <= 0:
            return SendReceipt(
                status="unknown",
                provider_operation_id=None,
                evidence={"adapter": "http", "reason": "deadline_exceeded_before_send"},
            )
        timeout = max(0.05, remaining)
        try:
            with self._client() as client:
                response = client.post(
                    self._send_url,
                    json={
                        "tenant_id": command.tenant_id,
                        "conversation_id": command.conversation_id,
                        "content": command.content,
                        # **Present only when the conversation is not the
                        # address** (T90). Sending `null` for every customer
                        # message would make the sink's own logs say every
                        # message had no recipient, which is the opposite of
                        # what this field is for.
                        **(
                            {"recipient": command.recipient}
                            if command.recipient is not None
                            else {}
                        ),
                    },
                    headers={"Idempotency-Key": idempotency_key},
                    timeout=timeout,
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            return SendReceipt(
                status="unknown", provider_operation_id=None, evidence={"adapter": "http"}
            )
        refused = self._status_receipt(response, provider_operation_id=None)
        if refused is not None:
            return refused
        try:
            payload = response.json()
            status = str(payload["status"])
            operation_id = str(payload["provider_operation_id"])
        except (ValueError, TypeError, LookupError):
            # The provider accepted the request but did not describe what it did
            # with it. Never echo the body: it carries customer content.
            return SendReceipt(
                status="unknown",
                provider_operation_id=None,
                evidence={
                    "adapter": "http",
                    "http_status": response.status_code,
                    "reason": "unreadable_provider_receipt",
                },
            )
        if status not in RECEIPT_STATUSES:
            return SendReceipt(
                status="unknown",
                provider_operation_id=operation_id,
                evidence={
                    "adapter": "http",
                    "http_status": response.status_code,
                    "reason": "unknown_provider_status",
                },
            )
        return SendReceipt(
            status=status,
            provider_operation_id=operation_id,
            evidence={"adapter": "http", "http_status": response.status_code},
        )

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        """Same question as `reconcile`, asked with the only id a timeout leaves.

        Returns `unknown` for anything that is not a readable receipt --
        including 404, which here means "the provider has no record of this
        key". That unknown is *useful*: it is the difference between "we asked
        and it does not know" and "we never asked", and only the first is
        allowed to end the intent in UNKNOWN.
        """

        remaining = (deadline_at - datetime.now(deadline_at.tzinfo)).total_seconds()
        if remaining <= 0:
            # `query_sent: False` -- nothing left this process. The dispatcher
            # reads this flag instead of assuming, because assuming is what
            # produced a receipt saying "we asked the provider" about a message
            # for which not one packet was ever sent (review 2026-09-04, P0-1).
            return SendReceipt(
                status="unknown",
                provider_operation_id=None,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": False,
                    "reason": "deadline_exceeded_before_reconcile",
                },
            )
        try:
            with self._client() as client:
                response = client.get(
                    # `quote(..., safe="")` because the key lands in a path
                    # segment. Today's keys are `send_message:{uuid}` and hold no
                    # slash, but httpx normalises `..` segments, so a future key
                    # shape could silently redirect the query to another endpoint
                    # and answer about the wrong thing (review 2026-09-04, P3-1).
                    f"{self._send_url}/by-key/{quote(idempotency_key, safe='')}",
                    timeout=self._query_timeout(remaining),
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            # `query_sent: True`: the request left this process, or died trying.
            # The flag separates "we could not ask" from "we asked and heard
            # nothing"; it does not claim the packet provably arrived.
            return SendReceipt(
                "unknown",
                None,
                {
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "query_unreachable",
                },
            )
        refused = self._reconcile_status_receipt(response, provider_operation_id=None)
        if refused is not None:
            return refused
        try:
            payload = response.json()
            status = str(payload["status"])
            operation_id = str(payload["provider_operation_id"])
        except (ValueError, TypeError, LookupError):
            return SendReceipt(
                status="unknown",
                provider_operation_id=None,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "unreadable_status",
                },
            )
        if status not in RECEIPT_STATUSES:
            return SendReceipt(
                status="unknown",
                provider_operation_id=operation_id,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "unknown_provider_status",
                },
            )
        return SendReceipt(
            status=status,
            provider_operation_id=operation_id,
            evidence={"adapter": "http", "reconciled": True, "query_sent": True},
        )

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        remaining = (deadline_at - datetime.now(deadline_at.tzinfo)).total_seconds()
        if remaining <= 0:
            return SendReceipt(
                status="unknown",
                provider_operation_id=provider_operation_id,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": False,
                    "reason": "deadline_exceeded_before_reconcile",
                },
            )
        try:
            with self._client() as client:
                response = client.get(
                    f"{self._send_url}/{quote(provider_operation_id, safe='')}",
                    timeout=self._query_timeout(remaining),
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            return SendReceipt(
                "unknown",
                provider_operation_id,
                {
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "query_unreachable",
                },
            )
        # No 404 special case, and since 2026-09-04 no terminal case either: a
        # query may only narrow uncertainty, never create it. The reasoning is
        # in `_reconcile_status_receipt`.
        refused = self._reconcile_status_receipt(
            response, provider_operation_id=provider_operation_id
        )
        if refused is not None:
            return refused
        try:
            status = str(response.json()["status"])
        except (ValueError, TypeError, LookupError):
            return SendReceipt(
                status="unknown",
                provider_operation_id=provider_operation_id,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "unreadable_status",
                },
            )
        if status not in RECEIPT_STATUSES:
            return SendReceipt(
                status="unknown",
                provider_operation_id=provider_operation_id,
                evidence={
                    "adapter": "http",
                    "reconciled": True,
                    "query_sent": True,
                    "reason": "unknown_provider_status",
                },
            )
        return SendReceipt(
            status=status,
            provider_operation_id=provider_operation_id,
            evidence={"adapter": "http", "reconciled": True, "query_sent": True},
        )
