"""The outbound WeComProxy adapter: `ChannelPort` on one side, `/msg/sendText` on the other.

**T96, outbound half.** Until now the only implementation of `ChannelPort` was
`HttpChannelAdapter`, which speaks the M0 simulator's protocol -- POST a JSON
body, read back `{status, provider_operation_id}`. WeComProxy speaks something
else entirely: `{guid, toId, content}` in, its own envelope out, and no notion
of the caller's idempotency key at all.

Two things this module deliberately does **not** do:

* **It still does not import the WeComProxy client -- but the reason changed
  (B2-5).** The send is injected as a callable.

  It used to be a *boundary* argument: `RestrictedWeComProxyClient` lived in
  `services/ingress`, nothing in `services/worker` had ever imported across
  that line, and "where should that class live" was a bigger question than this
  adapter -- moving a module carrying an allowlist, an audit sink and a
  redaction boundary was not a thing to do as a side effect.

  **B2-5 did that move on purpose, so the boundary argument is spent.** The
  client is now `fastagent_v2.channels.wecom_proxy` -- a shared package that is
  deliberately *not* `runtime`, because `AGENTS.md` keeps channel SDK logic out
  of core (user ruling 2026-09-09; ADR-0110). This service may import from it
  like any other shared package, and the error taxonomy below does exactly
  that.

  What keeps the *send* injected is now only the reason that was always the
  better one: **this adapter is constructed per dispatch, and a unit test should
  be able to hand it a function.** Which client instance to build, with which
  credentials and which confirmer, stays a wiring decision in `cli.py`, where
  B2-3 will make it.
* **It does not pretend to reconcile.** See `reconcile_by_key`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from fastagent_v2.channels.wecom_proxy.errors import (
    ConfirmationRequiredError,
    ForbiddenOperationError,
    GuidNotAllowedError,
)
from fastagent_v2.worker.channel import (
    ChannelCapabilities,
    SendCommand,
    SendReceipt,
    customer_file,
    customer_text,
)


class SendText(Protocol):
    """What the adapter needs from the provider client.

    **A `Protocol` rather than `Callable[..., dict]`, because the shape is the
    point** (independent review 2026-09-05, P1-2). The old alias accepted
    anything callable, so `deadline_at` had nowhere to go and was silently
    dropped: the adapter took a deadline, ignored it, and let httpx's
    constructor default decide. A send that outruns the outbox lease is not a
    slow log line -- it is another dispatcher claiming the row and the customer
    receiving the same message twice.

    `timeout` is **seconds remaining**, not a wall-clock time, because that is
    what the callee needs and it keeps the clock in one place.
    """

    def __call__(
        self,
        *,
        guid: str,
        recipient_id: str,
        content: str,
        timeout: float | None = ...,
    ) -> dict[str, object]: ...


class SendFile(Protocol):
    """发一份供应商已经持有的文件。

    **与 `SendText` 并排而不是合成一个**：两次调用要的东西不是一回事
    （一个要一段字，一个要四个供应商侧的标识），而**一个宽到两者都能过的签名
    正是 `SendText` 那次要修掉的东西** —— 它当时宽到 `deadline_at` 无处可放，
    于是被安静地丢掉了。

    `aes_key` 可空只在大文件那一档，而**那一条由客户端判**（`is_big_file`）：
    判据留在最靠近那次调用的地方，两处各判一遍迟早有一处宽一点。
    """

    def __call__(
        self,
        *,
        guid: str,
        recipient_id: str,
        file_id: str,
        file_size: int,
        file_md5: str,
        filename: str,
        file_aes_key: str | None = ...,
        timeout: float | None = ...,
    ) -> dict[str, object]: ...


class WeComProxyChannelAdapter:
    """One binding's outbound path.

    Constructed per dispatch with the `guid` and recipient the row resolved,
    because both are per-message facts: the guid comes from the tenant's channel
    binding and the recipient from the conversation. `SendCommand` carries
    neither -- it was designed against a simulator that needed only a
    conversation id.
    """

    def __init__(
        self,
        *,
        send_text: SendText,
        guid: str,
        recipient_id: str,
        send_file: SendFile | None = None,
    ) -> None:
        self._send_text = send_text
        self._guid = guid
        self._recipient_id = recipient_id
        # **默认 `None`，而那是有意的。** 这个适配器有两个构造点（生产的
        # `cli.build` 与用例里的双件），而**只有生产那个接得上文件那条路**。
        # 没接上时一行文件消息得到的是一条说得出理由的失败回执
        # （`file_sending_not_wired`），不是 `AttributeError` ——
        # 后者在派发循环里等于整个进程掉线，而那一行本来只该是一次投递失败。
        self._send_file = send_file

    # -- ChannelPort ----------------------------------------------------

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime  # noqa: ARG002 - 通道适配器端口的签名；本适配器不用幂等键，端口要求它在场
    ) -> SendReceipt:
        """Send, and turn every outcome into a receipt.

        `RestrictedWeComProxyClient` raises `WeComProxyError` for a non-zero
        provider code and `httpx` errors for transport failures; the port's
        contract is that a failure is a *receipt*, never an exception, because
        the dispatcher's only answer to an exception is to drop the process
        (`channel.py`'s opening comment: "a provider having a bad hour became a
        crash-restart loop instead of a recorded delivery state").

        **The classification is deliberately coarse, and that is honest.** A
        real provider's error taxonomy is exactly what this batch exists to
        discover -- T96 was registered to expose "WeComProxy's actual response
        shape, timeout behaviour, and receipt fields". Guessing which codes are
        terminal before seeing one would be inventing `TERMINAL_REFUSAL_STATUSES`
        for a provider nobody has called yet, and `_status_receipt`'s own comment
        records what that costs: "over-claiming loses a delivered message with no
        trace". So every failure here is `unknown` until the first real run says
        otherwise, and the first real run is the deliverable.
        """

        remaining = (deadline_at - datetime.now(deadline_at.tzinfo)).total_seconds()
        if remaining <= 0:
            # **Do not send.** Same shape and same reason as
            # `HttpChannelAdapter.send`: the lease this turn is running on has
            # expired, so another dispatcher may already hold the row. Sending
            # now is the duplicate. `unknown` rather than `failed` because
            # nothing was attempted -- there is no evidence about the message,
            # only about us.
            return SendReceipt(
                status="unknown",
                provider_operation_id=None,
                evidence={
                    "adapter": "wecom_proxy",
                    "reason": "deadline_exceeded_before_send",
                },
            )
        # **文件那一支先判，而它不与文字互斥地共存**：一行出站消息要么带
        # 一份已解析的文件，要么带话。先判文件是因为 `_text_of` 对一个文件形状
        # 回 `None`，两支的顺序换过来只会多绕一圈得到同一个答案 ——
        # 而写在前面让「这一行是文件」这件事读得出来。
        file_to_send = customer_file(command.content)
        if file_to_send is not None:
            if self._send_file is None:
                return SendReceipt(
                    status="failed",
                    provider_operation_id=None,
                    evidence={"adapter": "wecom_proxy", "reason": "file_sending_not_wired"},
                )
            try:
                answer = self._send_file(
                    guid=self._guid,
                    recipient_id=self._recipient_id,
                    file_id=file_to_send.file_id,
                    file_size=file_to_send.file_size,
                    file_md5=file_to_send.file_md5,
                    filename=file_to_send.filename,
                    file_aes_key=file_to_send.aes_key,
                    timeout=remaining,
                )
            except Exception as error:  # noqa: BLE001 - 端口的契约：失败是回执不是异常
                # 与文字那一支逐字同源：**只记类型名，不记报文** ——
                # 一条 WeComProxy 的错误串会把请求原样引回来，
                # 而文件那条请求里带着 `fileAesKey`。
                return SendReceipt(
                    status=_status_for(error),
                    provider_operation_id=None,
                    evidence={
                        "adapter": "wecom_proxy",
                        "reason": _reason_for(error),
                        "error_type": type(error).__name__,
                    },
                )
            return SendReceipt(
                status="accepted",
                provider_operation_id=_operation_id(answer),
                evidence=_accepted_evidence(answer),
            )

        text = _text_of(command)
        if text is None:
            # Not a transport failure: the platform built something this channel
            # cannot carry. Terminal, because retrying changes nothing.
            return SendReceipt(
                status="failed",
                provider_operation_id=None,
                evidence={"adapter": "wecom_proxy", "reason": "unsendable_content_type"},
            )
        try:
            answer = self._send_text(
                guid=self._guid,
                recipient_id=self._recipient_id,
                content=text,
                # What is left, not a constant: the client divides it between
                # the token round trip and the send, and gives httpx four
                # phases rather than one number used four times.
                timeout=remaining,
            )
        except Exception as error:  # noqa: BLE001 - every failure becomes a receipt
            # The type name, never the message: an WeComProxy error string can
            # quote the request, and the request is the customer's message.
            return SendReceipt(
                status=_status_for(error),
                provider_operation_id=None,
                evidence={
                    "adapter": "wecom_proxy",
                    "reason": _reason_for(error),
                    "error_type": type(error).__name__,
                },
            )
        return SendReceipt(
            status="accepted",
            provider_operation_id=_operation_id(answer),
            evidence=_accepted_evidence(answer),
        )

    def capabilities(self) -> ChannelCapabilities:
        """No, this channel cannot be asked by the caller's key. **T78 / ADR-0094 §三.**

        The claim is about **this adapter's permitted surface**, not about
        everything WeComProxy sells, and that is the only claim worth making:
        `RestrictedWeComProxyClient` **permits eight** operations
        (`create_client`, `get_login_qrcode`, `check_login_qrcode`,
        `verify_login_qrcode`, `set_notify_url`, `send_text`,
        `upload_file_by_url`, `send_file`) **and refuses four outright**
        (`FORBIDDEN_OPERATIONS`) -- twelve public names, and none of the twelve
        answers "what became of a message".

        **六→八是 09-16 这一批改的，而结论没变**：多出来的两个里，
        `upload_file_by_url` 那条路上根本没有消息；`send_file` **发出一条消息**，
        但它的响应与 `sendText` 同一个信封（§0.3），回一个 `msgId` ——
        **而供应商没有任何一个操作拿这个 id 去问后来怎么样了**。
        也就是多了一条能发的路，没多出一条能问的路。（the earlier wording said
        "exposes six", which counted the permitted half as the whole; corrected
        after independent review 2026-09-09, P3-3, and the conclusion is
        unchanged because the four refusals are not lookups either). There is
        nothing to call.

        **And nothing was being spent before this** (independent review
        2026-09-09, P2-1): `reconcile_by_key` below already returned without
        sending a packet, so declaring it here buys a truthful receipt, not
        cheaper traffic.

        **What the provider does offer instead is a push, and it is keyed the
        other way.** `docs/wecom_proxy.md` (vendor callback documentation, taken
        2026-09-08) documents `cmd=20000`, "API 异步消息", carrying a
        `requestId` the provider itself minted. That is the provider's id, not
        ours, and it arrives as a callback rather than as an answer to a
        question -- so it cannot serve a by-key lookup even in principle. Read
        the shape before assuming: this is why the declaration says no rather
        than "not implemented yet".

        The same payload also carries `customParam`, empty in every example the
        document shows, and **nothing available says whether `send_text` can
        set it** (independent review 2026-09-09, P3-4). Named here so the next
        reader can see it was not overlooked. It does not move the declaration
        either way: the load-bearing half of the argument above is that this is
        a **push, not a question**, and a field we might be able to echo
        through it would not make it answerable.

        `reconcile_by_key` below stays and stays honest. The dispatcher reads
        this declaration and does not call it (`_reconcile_before_giving_up`),
        but a caller that does not read it must still get `_cannot_ask` rather
        than a bare `unknown` -- the receipt of last resort, not the plan.

        **`reconcile` is in the same position and is deliberately not declared
        here.** ADR-0094 §三 revised `reconcile_by_key` alone, and the by-id
        door is unreachable in production for this adapter anyway: `send`
        leaves `provider_operation_id` `None` on every failure path, so the
        dispatcher never takes it. A second field would be one no production
        path could read -- the shape the zero-caller check exists to stop.
        """

        return ChannelCapabilities(reconcile_by_key=False)

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:  # noqa: ARG002 - 同上，端口签名
        """WeComProxy has no per-message status lookup. Say so; do not guess.

        `/msg/sendText` is the whole of the send surface v2 is allowed to call
        (`FORBIDDEN_OPERATIONS` plus the allowlist), and none of the permitted
        operations answers "what happened to this message".
        """

        return _cannot_ask("provider_has_no_status_lookup", provider_operation_id)

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:  # noqa: ARG002 - 同上，端口签名
        """**T78, arriving exactly as predicted.**

        The registered item says: real providers generally do not offer lookup by
        the *caller's* idempotency key, and an adapter that cannot do it "today
        can only return `unknown` -- and that `unknown` is indistinguishable from
        'we asked and the provider said it has no record', which is precisely the
        confusion L1 exists to remove".

        So this does not return a bare `unknown`. It returns one that says the
        channel cannot be asked, and `query_sent: False` keeps
        `_reconcile_before_giving_up` from recording that the provider was
        consulted -- which is L1's P0 and the thing that batch was fixed for.
        """

        return _cannot_ask("channel_cannot_be_queried_by_key", None)


#: Exceptions raised **on this side of the wire**, before any packet exists.
#:
#: **These are the real classes now (B2-5).** They used to be *strings*, matched
#: against `type(error).__name__`, because importing them would have meant the
#: worker reaching into `services/ingress` -- the boundary this module's header
#: said it would not cross for the sake of a convenience. The cost was stated at
#: the time and it was real: **a rename over there silently downgraded every one
#: of these back to `unknown`**, and the only thing standing against that was a
#: test on the ingress side asserting the names still lined up.
#:
#: B2-5 moved the client to `fastagent_v2.channels.wecom_proxy`, which
#: `services/worker` already imports from freely. **The boundary that forced the
#: string matching is gone, so the string matching goes with it** -- a rename is
#: now an import error instead of a silent behaviour change. That is what the
#: move was *for*, rather than a bonus on top of it.
#:
#: `TypeError` stays and is deliberately not from that module: the injected
#: callable not accepting the keywords this adapter passes is a wiring mistake,
#: deterministic in exactly the same way as the three refusals.
_LOCAL_GUARD_ERRORS: frozenset[type[BaseException]] = frozenset(
    {
        # The guid is not in the allowlist (default-deny).
        GuidNotAllowedError,
        # An operation needing an operator's confirmation got none.
        ConfirmationRequiredError,
        # One of `FORBIDDEN_OPERATIONS`.
        ForbiddenOperationError,
        # The injected callable does not accept the keywords this adapter
        # passes -- a wiring mistake, and deterministic in exactly the same way.
        TypeError,
    }
)


def _is_local_guard(error: Exception) -> bool:
    """Was this raised by us, before anything left the process?

    **`type(error) in ...`, not `isinstance`.** The set is exact: a future
    subclass of one of these should be classified on purpose rather than
    inherit `failed` from a parent.

    **The equivalence with the string version is narrower than first claimed**
    (independent review 2026-09-09, P3-1). For **these four classes** the two
    give the same answer. They differ on a class that merely *shares a name*:
    the string version matched `type(error).__name__`, so **anything** called
    `ForbiddenOperationError` counted as our own refusal -- and `send_text` is
    an injected callable, so a third-party library raising a same-named
    exception was entirely possible. Now only these four objects count.

    **That is a deliberate tightening, and it errs on the safe side**: such an
    error becomes `unknown` rather than `failed`, i.e. "we do not know whether
    the message was delivered" instead of "it certainly was not" -- which is the
    honest answer about a failure we cannot attribute to ourselves.
    """

    return type(error) in _LOCAL_GUARD_ERRORS


def _status_for(error: Exception) -> str:
    """`failed` for our own refusals, `unknown` for everything from the wire.

    **The coarse "everything is `unknown`" rule was right about the provider and
    wrong about us** (independent review 2026-09-05, P2-3). Not guessing the
    provider's taxonomy before the first real call is deliberate; but an
    allowlist refusal, a missing confirmation, a forbidden operation and a
    keyword mismatch are **our own exceptions, raised before any packet
    exists**, and retrying them a thousand times gives the same answer.

    Calling them `unknown` costs a manual task per message whose text is "we do
    not know whether this was delivered" -- about something that was never sent.
    This adapter already treats "nothing left this process" as a fact that must
    stay visible: `_cannot_ask` sets `query_sent: False` for exactly that
    reason. This is the same discipline applied on the send side.
    """

    return "failed" if _is_local_guard(error) else "unknown"


def _reason_for(error: Exception) -> str:
    return (
        "local_guard_refused" if _is_local_guard(error) else "provider_call_failed"
    )


def _cannot_ask(reason: str, provider_operation_id: str | None) -> SendReceipt:
    return SendReceipt(
        status="unknown",
        provider_operation_id=provider_operation_id,
        evidence={
            "adapter": "wecom_proxy",
            "reconciled": True,
            # False on purpose: nothing was sent, so nothing was asked. The
            # dispatcher reads this and records `reconcile_attempted: false`
            # plus a reason, rather than claiming a query happened.
            "query_sent": False,
            "reason": reason,
        },
    )


def _text_of(command: SendCommand) -> str | None:
    """The words to send, read by `channel.customer_text` and not by this module.

    **This function used to be the second half of a boundary defect.** It read
    exactly one shape -- `{"type": "text", "text": ...}` -- and the real model
    produces two others (`{"text": ...}` and a `segments` list). The third real
    window therefore sent nothing at all: three replies `FAILED` with
    `unsendable_content_type` and no `provider_operation_id`, the provider never
    called. **Both ends' unit tests were green**, because both were written
    against the same invented shape.

    So the knowledge of which shapes exist now lives in one place, next to the
    type that crosses the boundary, and this adapter asks it. What stays here is
    the refusal: a channel that carries text still refuses content it cannot
    read, with a receipt rather than an exception.
    """

    return customer_text(command.content)


def _accepted_evidence(answer: dict[str, object]) -> dict[str, object]:
    """The receipt's evidence for a send the provider took.

    **`isSendSuccess` is carried, not interpreted** (independent review
    2026-09-10, non-blocking suggestion). The official response body (§0.3 of
    `docs/wecom-proxy-message-api.md`) declares it required, and this adapter has
    never read it: a `code == 0` answer is `accepted` whatever it says.

    Mapping `0` onto `failed` or `unknown` would be **guessing again** -- the
    same move that put four invented keys in `_OPERATION_ID_KEYS`, and the
    document itself points the other way: §21.3 says a failed send is reported
    **afterwards, by callback** (`msgType=1011`), and this body is titled
    「消息发送响应成功」. So the value is recorded and nothing branches on it,
    which is what makes the first real `isSendSuccess: 0` **visible** instead of
    being a thing nobody can prove happened. T261's second half is where it
    stops being decoration.
    """

    evidence: dict[str, object] = {"adapter": "wecom_proxy"}
    reported = answer.get("isSendSuccess")
    if isinstance(reported, int) and not isinstance(reported, bool):
        evidence["is_send_success"] = reported
    return evidence


#: What WeComProxy calls the thing it just created. **Documented, not guessed**
#: (`docs/wecom-proxy-message-api.md` §0.3, the response body §2–§15 share).
#:
#: **The four guesses that used to be here were all wrong**: `msgId` / `msgid` /
#: `messageId` / `id`. The official send response carries none of them --
#: `data` is `{isSendSuccess, msgServerId, msgType, msgUniqueIdentifier, seq,
#: timestamp}` -- so `_operation_id` returned `None` for **every real send** and
#: `delivery_receipts.provider_operation_id` would have been empty forever.
#:
#: **That is the second time this exact function shipped a guessed key**, and
#: the docstring below already tells the story of the first (independent review
#: 2026-09-05, P1-1: the fixture returned an envelope no caller produces and the
#: suite stayed green). It stayed green this time too, for the same reason: the
#: test's response was written next to the code that reads it. **Deleted rather
#: than kept as fallbacks**, exactly as `inbound.WITHDRAWN_REFERENCE_KEYS` did
#: when the vendor document arrived -- a fallback list leaves the question
#: permanently half-open.
#:
#: **Order is "which one operates on the message"**: `msgServerId` is what
#: `/msg/revokeMsg` takes (§16) and what §0.3 titles 「消息id」;
#: `msgUniqueIdentifier` is the id that comes back on callbacks, so it is the
#: one that ties a send to its echo. Either is a handle a person can use; the
#: first is the one that can act.
_OPERATION_ID_KEYS = ("msgServerId", "msgUniqueIdentifier")


def _operation_id(answer: dict[str, object]) -> str | None:
    """Whatever WeComProxy calls the thing it just created, if it names one.

    **The top level first, and that is the fix** (independent review 2026-09-05,
    P1-1). The only caller that will ever be injected here is
    `RestrictedWeComProxyClient.send_text`, and it returns
    `dict(result.get("data") or {})` -- it has **already unwrapped the
    envelope**. The first version looked only inside `answer["data"]`, so
    against the real client it found nothing and every receipt carried
    `provider_operation_id = None`, while the test's fixture returned an
    envelope no caller produces and the suite stayed green. The consequence is
    not a crash: it is that `delivery_receipts.provider_operation_id` is empty
    forever, and that column is the only handle a person has for finding the
    message on the WeComProxy side -- which is one of the four things T96 was
    registered to deliver.

    **Both layers are tried on purpose**, not as belt-and-braces: the top level
    is what the guarded client returns, and the `data` envelope is what a raw
    `/msg/sendText` response looks like. Anyone wiring this adapter to the
    transport directly -- during a Stage smoke test, say -- gets the same
    answer instead of a silent `None`.

    **Written to be wrong gracefully.** The real response shape is still one of
    the things a first real call has to confirm; for an unrecognised body the
    honest answer is `None`, which sends the delivery down L1's by-key path,
    where this channel says plainly that it cannot be queried rather than
    fabricating an id nothing can look up.
    """

    for key in _OPERATION_ID_KEYS:
        value = answer.get(key)
        if isinstance(value, str | int) and str(value):
            return str(value)
    data = answer.get("data")
    if isinstance(data, dict):
        for key in _OPERATION_ID_KEYS:
            value = data.get(key)
            if isinstance(value, str | int) and str(value):
                return str(value)
    return None
