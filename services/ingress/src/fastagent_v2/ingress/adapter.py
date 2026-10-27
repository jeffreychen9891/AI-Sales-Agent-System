from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.ingress.wecom_proxy.callback import (
    DECLINED_ROOM,
    IGNORED_ECHO,
    IGNORED_MEDIA_FETCH_RESULT,
    IGNORED_MEDIA_KEY_NOTICE,
    IGNORED_NOTICE,
    IGNORED_OPERATION_HINT,
    IGNORED_SEND_FAILED,
    IGNORED_UPLOAD_FAILED,
    OUTCOME_DECLINED,
    OUTCOME_IGNORED,
    OUTCOME_MESSAGE,
    OUTCOME_UNREADABLE,
    OUTCOME_WITHDRAWAL,
    CallbackEnvelopeError,
    CallbackItem,
    conversation_id_for,
    envelope_status,
    read_callback,
)
from fastagent_v2.ingress.wecom_proxy.inbound import (
    parse_content,
    retained_raw_payload,
    storable_as_text,
    withdrawn_reference,
)
from fastagent_v2.ingress.wecom_proxy.upload_claim import (
    claim_upload,
    record_upload_failure,
    upload_result,
)
from fastagent_v2.runtime.auth import constant_time_token_match, sha256_token
from fastagent_v2.runtime.contracts import UNKNOWN_ACTOR, validate_contract
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from jsonschema import ValidationError
from psycopg import DataError
from psycopg.types.json import Jsonb

#: An element we could not read. **Not a `callback.py` outcome**: that module
#: says what an element *is*, and "unreadable" is decided here, where the
#: storable-value rules and the envelope contract live.
OUTCOME_REFUSED = "refused"

#: `039` bounds `contacts.external_user_id` at 128, the same bound `012`
#: puts on the manager's id. **The writer therefore has to bound it too**
#: (re-review 2026-09-06, P2-1): the constraint sits on the write path, and
#: a `CheckViolation` escaping `accept()` is not a rejected identity -- it
#: is the whole transaction rolled back, no `inbox_events` row, a 500 to the
#: channel, and a retry loop that can never succeed. Checked here, the
#: database constraint stays what it should be: the last line, unreachable
#: from the only writer.
#:
#: **Two copies of one bound, and only one direction of drift is caught**
#: (chain review 2026-09-06, P3-3). If `039` is ever tightened below this
#: number, `test_an_over_long_identity_costs_the_contact_not_the_message`
#: goes red on its 128-character case. If `039` is *widened*, nothing
#: complains and identities between 129 and the new bound are silently
#: dropped by this writer. Whoever changes the column changes this line.
MAX_EXTERNAL_USER_ID = 128

#: The two lengths `message-envelope.schema.json` already declares
#: (`external_event_id` 255, `content.text` 20000). Repeated here **because the
#: schema is checked too late**: `validate_contract` runs after the envelope is
#: built, raises `jsonschema.ValidationError` -- which is not a `ValueError` --
#: and the callback got a framework `500`. A customer pasting a long message is
#: not an outage, and ten of them delete the callback address (ADR-0103 §二.1,
#: independent review 2026-09-07 P1-1). Checked at the boundary, they are what
#: they are: a payload we refuse, answered `200` with a business error.
MAX_EXTERNAL_EVENT_ID = 255
MAX_TEXT = 20000


#: **`storable_as_text` moved to `wecom_proxy/inbound.py`** so the two halves
#: of T206's rule live in one place: a value the database cannot hold and a
#: field the payload did not carry are handled the same way, and a second
#: copy here is the drift that rule exists to prevent. Imported above.


#: How far out of step with our clock a payload's `occurred_at` may be and
#: still be treated as a real time rather than a broken one.
#:
#: **The floor costs something** (chain review P3-2): a redelivery from
#: three days ago is stored as one day ago, so `last_seen_at` reads newer
#: than the truth and the contact sorts higher in B3 #21's list. Bounded by
#: the window, and in the direction that misleads least -- but it is the
#: price of stopping a 1970 from pinning `first_seen_at` forever.
#:
#: **Both ends, because both are reachable** (re-review P3-1). The upper
#: bound was added first: a payload claiming next decade pinned a contact
#: at the top of every list forever. `LEAST` on `first_seen_at` then opened
#: the other end -- a payload claiming 1970 pins "first seen" just as
#: permanently, and B3 #22 hands that number out.
#:
#: **A day, because out-of-order delivery is real and a day of it is not.**
#: Channels retry for minutes and occasionally hours; a timestamp a day out
#: is a clock, not a delivery. Clamped rather than rejected -- the same
#: discipline the identity path keeps: nothing new is refused, the message
#: still arrives, and only the value we sort on is bounded.
CONTACT_CLOCK_WINDOW = timedelta(days=1)


LOGGER = logging.getLogger(__name__)


#: The reason codes the ingress records for its own answers, and the whole
#: vocabulary of the `reason` label on `ingress_non_2xx_total` /
#: `ingress_refused_total`. **Bounded on purpose** (ADR-0101 §三.1): the label
#: may only ever take one of these values, so a new refusal branch has to be
#: named here before it can be counted -- which is the point at which somebody
#: has to decide whether it is ours or the caller's.
#: A callback whose `data` array carried nothing. Answered, recorded, and not
#: a refusal -- there was nothing in it to refuse.
EMPTY_CALLBACK = "EMPTY_CALLBACK"

INGRESS_NON_2XX_REASONS = ("WINDOW_NOT_OPEN",)
INGRESS_REFUSED_REASONS = (
    "CHANNEL_AUTHENTICATION_FAILED",
    "INVALID_CALLBACK_PAYLOAD",
    # **A message we understood and declined** (2026-09-10). Named here before
    # it could be counted, which is what this tuple is for.
    DECLINED_ROOM,
)

#: The answers that are not refusals: a callback we handled, or one that carried
#: nothing to handle.
INGRESS_ANSWERED_REASONS = (
    IGNORED_NOTICE,
    IGNORED_ECHO,
    # **The one that proved this tuple and `IGNORED_REASONS` were two lists
    # nobody kept in step** (independent review 2026-09-10, P1-1). The batch
    # that added the 1011 code put it in `IGNORED_REASONS` -- which has
    # no reader -- and not here, which is the tuple `record_answer` actually
    # draws from. It went unnoticed because until then the two members of
    # `IGNORED_REASONS` both happened to appear here: **the hole was latent,
    # and the first new ignored reason in the repository's history walked
    # straight through it.**
    IGNORED_OPERATION_HINT,
    # **`1011` 里那一小类：我们发出去的那条被拒了**（T261 后半，09-17）。
    #
    # 与上面那条同一个教训的**正向执行**：在能产生它的同一笔里声明它 ——
    # 上一次没这么做，于是本仓历史上第一个新增的 ignored 原因码
    # 直接穿过了这个洞。这一次它当场红了
    # （`test_every_classified_reason_can_actually_be_recorded`），
    # 也就是那道闸真的在守。
    IGNORED_SEND_FAILED,
    # **上传失败**（运维实测 2026-09-17：十二份全失败而本仓一处都不红）。
    #
    # 这是**同一道闸第二次**在同一个位置抓住同一个人：上一批加
    # `SEND_FAILED` 时也是先进了 `IGNORED_REASONS` 而忘了这里。
    # 两个元组必须同步 —— 而记不住这件事本身，正是这道闸存在的理由。
    IGNORED_UPLOAD_FAILED,
    IGNORED_UPLOAD_FAILED,
    # **The two `cmd=20000` structures** (ADR-0126 §三, 2026-09-11). Declared
    # here in the same commit that can produce them -- which is the whole point
    # of the tuple above, and of the gate case that reads both lists together.
    IGNORED_MEDIA_FETCH_RESULT,
    IGNORED_MEDIA_KEY_NOTICE,
    EMPTY_CALLBACK,
    "WITHDRAWAL",
    "DUPLICATE",
)

#: Which vocabulary each `kind` draws from. **Checked at the point of writing**
#: (independent review 2026-09-10, P3-4): before this, the tuples above were
#: read by nobody, so "a new refusal branch has to be named here before it can
#: be counted" was a promise the code did not keep -- a branch could invent a
#: `reason_code`, land in `audit_events`, and hand the collector an unbounded
#: label. ADR-0101 §三.1 forbids exactly that, and now the assertion is where
#: the row is written rather than in a comment.
_REASONS_BY_KIND = {
    "ingress.non_2xx": INGRESS_NON_2XX_REASONS,
    "ingress.refused": INGRESS_REFUSED_REASONS,
    "ingress.answered": INGRESS_ANSWERED_REASONS,
}


class IngressOutcomeError(RuntimeError):
    """Base for the refusals whose *answer* this service has to count.

    `binding_tenant_id` and `binding_config_id` are set only when the binding
    was resolved before the refusal happened. **When they are `None` the outcome
    is deliberately not recorded**, and that is a security property rather than
    a gap: `config_id` arrives in the URL, so keying a metric on an unresolved
    one would let any caller invent labels and make the series unbounded -- the
    cardinality failure ADR-0101 §三.1 forbids, reachable here by anyone who can
    reach the port.
    """

    def __init__(
        self,
        message: str,
        *,
        binding_tenant_id: UUID | None = None,
        binding_config_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.binding_tenant_id = binding_tenant_id
        self.binding_config_id = binding_config_id


class IngressAuthenticationError(IngressOutcomeError):
    pass


class IngressWindowNotOpenError(IngressOutcomeError):
    """No token window is open -- **and that is our problem, not the caller's.**

    Split out of `IngressAuthenticationError` by the user's ruling of
    2026-09-07 (independent review P2-1). The other four authentication
    refusals are about the caller: an unknown config, a disabled binding, a
    token that does not match, a replayed delivery. This one fires when the
    binding's own validity window has not opened -- or has closed while the
    rotation overlapped -- and the message it carries names both clocks
    (`db_now`, `process_now`) precisely because the usual cause is skew, the
    same family as T100 / T186.

    **Answering `200` here would lose the message.** The provider would record
    a successful delivery and never send it again, while the window that was
    not open yet opens a second later. So this one is answered non-200 and the
    provider redelivers -- at the cost of one `errorTimes`, against a deletion
    threshold of ten and an alert threshold of five (B3-4b).
    """


class IngressPayloadError(IngressOutcomeError):
    pass


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    """What became of one element of the callback's `data` array.

    **One element, one outcome.** Before this batch a request was a message, so
    the request's result and the message's were the same object; the vendor
    sends an array, so they are two things now and conflating them is how a
    partially-refused request comes to report itself as wholly refused.
    """

    #: `msgUniqueIdentifier`, or `""` when the element did not carry a usable
    #: one -- which is itself a refusal, and this is what names it in the answer.
    external_event_id: str
    #: `OUTCOME_MESSAGE` / `OUTCOME_WITHDRAWAL` / `OUTCOME_IGNORED` /
    #: `OUTCOME_REFUSED`.
    outcome: str
    #: `None` when the element produced no inbox row: a withdrawal (ADR-0107),
    #: an ignored notice, or a refusal. **Optional rather than a fabricated id**,
    #: because an id that points at no row is the kind of value someone
    #: eventually joins on.
    inbox_id: UUID | None
    event_id: UUID | None
    duplicate: bool
    #: Why it was ignored or refused; `None` when it was neither.
    reason_code: str | None
    pointer_published: bool


@dataclass(frozen=True, slots=True)
class IngressResult:
    #: Every element's outcome, in the order the vendor sent them. Empty when
    #: `data` was an empty array -- a request that carried nothing, which is
    #: answered `200` and writes nothing.
    items: tuple[ItemOutcome, ...]

    @property
    def accepted(self) -> bool:
        """Whether every element was handled. **Not "something was stored".**

        A request made only of notices is accepted: we understood it and
        answered it. What makes `accepted` false is a refusal -- an element we
        could not read, or one we read and **declined** (a group message) --
        because those are the outcomes the sender could act on.
        """

        return not any(
            item.outcome in (OUTCOME_REFUSED, OUTCOME_DECLINED) for item in self.items
        )

    @property
    def duplicate(self) -> bool:
        """Whether any element had already been delivered.

        Any rather than all: with one element -- every real callback so far, and
        every test written before this batch -- the two readings coincide, and
        for a mixed request "some of this you already have" is the true thing to
        say.
        """

        return any(item.duplicate for item in self.items)

    @property
    def event_id(self) -> UUID | None:
        """The first element that became an event, for a single-message answer."""

        for item in self.items:
            if item.event_id is not None:
                return item.event_id
        return None

    @property
    def inbox_id(self) -> UUID | None:
        for item in self.items:
            if item.inbox_id is not None:
                return item.inbox_id
        return None

    @property
    def pointer_published(self) -> bool:
        """Whether a worker was woken for this request.

        **`any`, not `all`, and the difference is not pedantry.** `all` over an
        empty sequence is `True`, so a withdrawal -- which deliberately produces
        no inbox row and no pointer -- would have reported `pointer_published:
        True` and the assertion "no worker may be woken for it" would have gone
        green while saying nothing. The single-element readings are unchanged:
        a delivered message is `True`, a message whose publish failed is
        `False`, a withdrawal is `False`.

        Per-element detail lives in `items`; this is the request-level answer to
        "did anything downstream get told".
        """

        return any(item.pointer_published for item in self.items)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class WeComProxyIngress:
    def __init__(
        self,
        *,
        database: Database,
        pointer_queue: PointerQueue,
        runtime_deadline_seconds: int,
    ) -> None:
        self._database = database
        self._queue = pointer_queue
        self._runtime_deadline_seconds = runtime_deadline_seconds

    def _claim_upload(
        self, *, tenant_id: UUID, parsed: CallbackItem, envelope_detail: str | None
    ) -> bool:
        """这条回调如果是我们某次上传的结果，把那一行补齐。**从不抛。**

        **从不抛，与 `record_answer` 同一个理由**：这条路上的调用方已经决定
        要回 `200` 了，而一次认领失败不该把它变成 `500` —— 那会花掉调用方的
        `errorTimes` 预算（ADR-0103 §二.1），而代价是供应商开始退避重投
        **一条我们本来就打算忽略的通知**。

        **自己开一个事务**：认领与那条 `ingress.answered` 审计行是两件事，
        一件失败不该把另一件带走 —— 尤其不该是"认领成功了、审计没写成，
        于是整笔回滚，回调下次重投时那一行已经被认领过了"。

        没有 `requestId` 的元素直接跳过：那是绝大多数通知的样子，
        **一次多余的库往返乘以每天的通知量不是零**。
        """

        if not parsed.request_id:
            return False
        result = upload_result(parsed.item)
        try:
            with self._database.transaction(tenant_id=tenant_id) as connection:
                if result is None:
                    # **这一支是 2026-09-17 补的，而在它之前这里直接 `return`。**
                    #
                    # 运维实测：十二份全失败，本仓**没有任何一处会红** ——
                    # 因为失败的样子就是「`msgData` 为 null，于是没有东西可认领」，
                    # 与「一条与我们无关的 20000」**在这个位置上长得一模一样**。
                    #
                    # 分开它们的判据只有一个：**那个 `requestId` 在不在目录里**。
                    # 在 = 这是我们发起的上传，而它没带回结果 → 记一次失败；
                    # 不在 = 别人的回调，`rowcount` 为 0，什么都不记。
                    failed = record_upload_failure(
                        connection,
                        tenant_id=tenant_id,
                        request_id=parsed.request_id,
                        detail=envelope_detail,
                    )
                    if failed:
                        LOGGER.warning(
                            "ingress.upload_failed request_id=%s detail=%s",
                            parsed.request_id,
                            envelope_detail or "(供应商没给 code)",
                        )
                    return bool(failed)
                claimed = claim_upload(
                    connection,
                    tenant_id=tenant_id,
                    request_id=parsed.request_id,
                    result=result,
                )
        except Exception:  # noqa: BLE001 - 见 docstring：这条路上不许抛
            LOGGER.warning(
                "ingress.upload_claim_failed request_id=%s", parsed.request_id
            )
            return False
        if claimed:
            # **只在改到行时记一句**，否则每一条取件结果都会打一行日志 ——
            # 而那一类每次都改 0 行，按构造如此。
            LOGGER.info("ingress.upload_claimed request_id=%s", parsed.request_id)
        return False

    def record_answer(
        self,
        *,
        tenant_id: UUID,
        config_id: str,
        kind: str,
        reason_code: str,
        http_status: int,
    ) -> None:
        """Write down what this service answered, for the callback-deletion gauge.

        **Every `reason_code` reaching here has to be declared in
        `_REASONS_BY_KIND`** (independent review 2026-09-10, P3-4), and that is
        enforced **at gate time** by
        `tests/contracts/test_ingress_answer_vocabulary.py` rather than here:
        this function may not raise (see below), so a check inside it could only
        log -- and a logged violation still hands the collector an unbounded
        label, which is the cardinality failure ADR-0101 §三.1 forbids. Reading
        the call sites is what makes "a new branch has to be named before it can
        be counted" true instead of merely written down.

        **The provider deletes a callback address after ten consecutive
        failures** (T204/T199, ADR-0103 §二). Nothing in this repository knew
        how many we had answered in a row, so the gauge ADR-0103 §二.2 asks for
        had no source. This is that source, and it is written to `audit_events`
        rather than to a table of its own because the row is already what an
        audit row is -- a decision this platform made about a request -- and
        because the Stage collector already counts that table (B0-5), so no
        migration and no schema deployment stand between the metric and the
        dashboard.

        **A separate transaction on purpose.** Every caller is on a failure
        path, and the transaction that failed is being rolled back; a record
        written inside it would roll back with it, which is the one outcome that
        must not happen -- the failures are exactly what the gauge counts.

        **It never raises.** If recording the answer failed loudly it could turn
        a `200` refusal into a `500`, which is the code path ADR-0103 §二.1
        exists to prevent: our own bookkeeping must not spend the caller's
        `errorTimes` budget.
        """

        try:
            with self._database.transaction(tenant_id=tenant_id) as connection:
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        tenant_id, id, actor_id, action, resource_type, resource_id,
                        correlation_id, reason_code, evidence
                    ) VALUES (%s, %s, NULL, %s, 'channel_binding', NULL, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        uuid4(),
                        kind,
                        # **Its own correlation id, because there is no upstream
                        # one to inherit.** These rows are written on paths that
                        # refused before a correlation id was derived -- and the
                        # column is `NOT NULL`. The first version passed `NULL`
                        # here; the insert failed the constraint, `record_answer`
                        # swallowed it as designed, and the metric would have
                        # been **silently absent in production** while every
                        # source-level case stayed green. The integration case
                        # against a real database is what caught it.
                        uuid4(),
                        reason_code,
                        Jsonb({"config_id": config_id, "http_status": http_status}),
                    ),
                )
        except Exception:  # noqa: BLE001 - see the docstring: never the caller's problem
            LOGGER.warning(
                "ingress could not record its own answer; config_id=%s reason=%s",
                config_id,
                reason_code,
            )

    #: The audit vocabulary for a withdrawal. Named constants because
    #: `action` and `reason_code` are what a person greps for months later, and
    #: because a typo in a string literal is invisible until someone looks for
    #: rows that were never written under the name they expected.
    WITHDRAWAL_ACTION = "channel.message_withdrawn"
    WITHDRAWAL_RESOURCE_TYPE = "channel_callback"
    WITHDRAWAL_REASON_CODE = "CUSTOMER_WITHDREW_MESSAGE"

    def _write_withdrawal_audit(
        self,
        connection: Any,
        *,
        tenant_id: UUID,
        config_id: str,
        external_event_id: str,
        conversation_id: UUID,
        correlation_id: UUID,
        event_id: UUID,
        payload: dict[str, object],
    ) -> None:
        """The row ADR-0107 §二.2 asks for: which message the customer took back.

        **No migration** (ADR-0107 §二.4 forbids one): `audit_events` exists in
        `001_m0.sql:285` and `fastagent_ingress` holds `INSERT` on it from
        `001_m0.sql:412`. No new column, no new table, no new grant.

        **`evidence` carries identifiers, not content.** The withdrawn text is
        exactly what the customer asked us to stop having, so copying the
        payload in wholesale to be thorough would mean a withdrawal *created* a
        durable copy of the withdrawn words. What is stored is: which callback,
        which conversation, and whatever reference the payload offered --
        `null` when it offered none, which is a fact worth recording rather than
        a gap to paper over.

        **The row's id is the `event_id` this call returns.** A withdrawal
        produces no inbox row, so that id has to identify *something* -- and a
        fresh `uuid4()` here would hand the caller an id pointing at nothing,
        which is the same defect `inbox_id=None` exists to avoid. Caught by the
        post-disposition gate: the two cases that look the row up by the
        returned id went red.

        **`withdrawn_reference` degrades; it never refuses** (independent review
        P3-1). It is optional by construction, so a value the database cannot
        hold is dropped exactly like `nonce` and `actor_id` are -- the T180
        family rule. `external_event_id` is *not* in that family and is checked
        before we ever get here.
        """

        reference = withdrawn_reference(payload)
        if reference is not None and not (
            storable_as_text(reference) and len(reference) <= MAX_EXTERNAL_EVENT_ID
        ):
            reference = None

        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, %s, %s, NULL, %s, %s, %s)
            """,
            (
                tenant_id,
                event_id,
                self.WITHDRAWAL_ACTION,
                self.WITHDRAWAL_RESOURCE_TYPE,
                correlation_id,
                self.WITHDRAWAL_REASON_CODE,
                Jsonb(
                    {
                        "config_id": config_id,
                        "external_event_id": external_event_id,
                        "conversation_id": str(conversation_id),
                        "withdrawn_reference": reference,
                    }
                ),
            ),
        )

    def _write_answered(
        self,
        connection: Any,
        *,
        tenant_id: UUID,
        config_id: str,
        correlation_id: UUID,
        reason_code: str,
    ) -> None:
        """Record that we answered `200`, so the failure streak goes to zero.

        `INGRESS_SQL` (in `scripts/stage_observability.py`) reads three signals
        as "we answered": an `ingress.answered` row, an `ingress.refused` row,
        or an `inbox_events` row carrying `config_id`. A path that writes none
        of them leaves the streak standing through a healthy answer -- and the
        gauge built on it guards the **irreversible** deletion of the callback
        address after ten consecutive failures (ADR-0103 §二.1 / §二.2).

        Extracted so the withdrawal path and the duplicate path state the same
        thing once; the duplicate branch had this reasoning in a comment and the
        withdrawal branch was written without it (independent review P2-1).
        """

        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'ingress.answered', 'channel_binding', NULL,
                      %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                correlation_id,
                reason_code,
                Jsonb({"config_id": config_id, "http_status": 200}),
            ),
        )

    def accept(
        self, *, config_id: str, path_token: str, payload: dict[str, object]
    ) -> IngressResult:
        now = datetime.now(UTC)
        with self._database.transaction(tenant_id=None) as connection:
            # **The window is decided by the clock that wrote it** (T186's
            # unfinished half). `current_valid_from`, `overlap_ends_at` and
            # `next_valid_from` are all database instants; comparing them to
            # this process's `now` put an authentication decision across two
            # clocks.
            #
            # T186 is registered as fixed, and for what it was about -- a
            # not-yet-valid binding reporting itself as a credential fault --
            # it is: the messages below were split, the fixture pins
            # `current_valid_from` into the past, and `conftest` fails loudly on
            # a large offset. **All three made the symptom legible. None of
            # them moved the comparison.**
            #
            # Both directions matter here and only one had been thought about.
            # Process behind the database: a binding that has just become valid
            # is refused. Process ahead: a token is accepted before its window
            # opens, and a retired one stays accepted past `overlap_ends_at`.
            # The second direction is a credential living longer than the
            # rotation says it does, and T193 measured this host's offset
            # crossing zero in both directions every thirty seconds.
            #
            # **`clock_timestamp()`, and bound to one instant** -- the rule the
            # auth-expiry batch settled on and wrote into `_authenticate`: a
            # predicate that decides whether to *admit* a caller reads
            # `clock_timestamp()`, so nothing outlives its window by the
            # duration of the transaction asking; `now()` stays for predicates
            # that only pick the rows a statement is about to stamp. This one
            # admits an inbound request, so it is a gate. The quantity is a
            # transaction's length and changes no answer today -- the reason to
            # spend the line is that `_authenticate` a few files over now reads
            # `clock_timestamp()` for exactly this reason, and an adjacent
            # counterexample teaches the next reader the opposite rule.
            #
            # The CTE is `MATERIALIZED` so the instant is taken *once*:
            # `clock_timestamp()` is volatile and advances within a statement,
            # so three calls would compare three different instants and print a
            # fourth as `db_now`. One row, one instant, and `db_now` is the
            # instant that actually decided.
            binding = connection.execute(
                """
                WITH decided_at AS MATERIALIZED (SELECT clock_timestamp() AS at)
                SELECT b.*,
                       d.at AS db_now,
                       d.at >= b.current_valid_from
                       AND (b.overlap_ends_at IS NULL OR d.at <= b.overlap_ends_at)
                           AS current_allowed,
                       (b.next_token_hash IS NOT NULL
                        AND b.next_valid_from IS NOT NULL
                        AND d.at >= b.next_valid_from) AS next_window_open
                  FROM resolve_channel_binding(%s) AS b, decided_at AS d
                """,
                (config_id,),
            ).fetchone()
        # **These messages name which check failed** (T186, ADR-0090). All three
        # used to be the literal string "channel authentication failed", so a
        # traceback could not say whether the binding was missing, outside every
        # token window, or simply presented the wrong token. That cost real time:
        # a run where the clock made every freshly seeded binding not-yet-valid
        # read as a credential fault, and the investigation went to hashing and
        # rotation before the time window.
        #
        # **Being specific here does not leak anything to the caller.** The 401
        # in `app.py` is built from a hardcoded `detail="channel authentication
        # failed"` and never from `str(exc)`, so these strings reach logs and
        # tests only. If that ever changes, this comment is the thing to check
        # first -- telling an attacker *which* half failed is exactly what an
        # authentication path must not do.
        if binding is None:
            # **No tenant, so no recorded outcome.** See `IngressOutcomeError`:
            # the `config_id` in the URL is unvalidated input until a binding
            # answers to it, and this is the branch where none did.
            raise IngressAuthenticationError("no channel binding for this config id")
        binding_tenant_id = UUID(str(binding["tenant_id"]))
        if binding["status"] != "active":
            raise IngressAuthenticationError(
                f"channel binding is not active: status={binding['status']!r}",
                binding_tenant_id=binding_tenant_id,
                binding_config_id=config_id,
            )

        current_allowed = bool(binding["current_allowed"])
        current_match = current_allowed and constant_time_token_match(
            path_token, str(binding["current_token_hash"])
        )
        # Split from `next_match` so the two failure modes stay distinguishable
        # below: an unopened window and a wrong token are different faults.
        next_window_open = bool(binding["next_window_open"])
        next_match = next_window_open and constant_time_token_match(
            path_token, str(binding["next_token_hash"])
        )
        if not (current_match or next_match):
            if not (current_allowed or next_window_open):
                # The token was never compared -- no window was open to compare
                # it against. Reporting the clock is the whole point.
                # Both clocks, because the offset between them is the thing
                # this message exists to make visible -- and now that the
                # decision is the database's, its clock is the one that decided.
                raise IngressWindowNotOpenError(
                    "no channel token window is open for this binding "
                    f"(db_now={binding['db_now']}, process_now={now.isoformat()}, "
                    f"current_valid_from={binding['current_valid_from']}, "
                    f"overlap_ends_at={binding['overlap_ends_at']})",
                    binding_tenant_id=binding_tenant_id,
                    binding_config_id=config_id,
                )
            raise IngressAuthenticationError(
                "channel token does not match the binding",
                binding_tenant_id=binding_tenant_id,
                binding_config_id=config_id,
            )

        # **One request, many messages.** `data` is an array and the vendor uses
        # it: the shape has always been `{code, msg, data:[…]}` and nothing in
        # the protocol promises one element. Authentication happened once above
        # -- it is a property of the request -- and everything below is decided
        # per element.
        try:
            items = read_callback(payload)
        except CallbackEnvelopeError as exc:
            # **The envelope itself is wrong**, so there are no elements to
            # answer individually. This is the branch a v2 `message-envelope`
            # body now lands on, which is what this batch's reverse probe
            # asserts.
            raise IngressPayloadError(
                "invalid callback payload",
                binding_tenant_id=binding_tenant_id,
                binding_config_id=config_id,
            ) from exc

        if not items:
            # **An empty `data` is answered, and saying so is the point**
            # (independent review 2026-09-10, P3-2). `read_callback`'s docstring
            # promised it "the same treatment a request full of notices gets" --
            # and notices write `ingress.answered`, which is what clears the
            # consecutive-failure streak guarding the callback address. An empty
            # request wrote nothing at all, so the two were not the same.
            self.record_answer(
                tenant_id=binding_tenant_id,
                config_id=config_id,
                kind="ingress.answered",
                reason_code=EMPTY_CALLBACK,
                http_status=200,
            )

        outcomes: list[ItemOutcome] = []
        for parsed in items:
            if parsed.outcome == OUTCOME_UNREADABLE:
                # **Nothing in this element to read** (review P2-1). Refused as
                # a payload fault, which is what it is -- and *not* as a group
                # message, which is what it used to come back as.
                self.record_answer(
                    tenant_id=binding_tenant_id,
                    config_id=config_id,
                    kind="ingress.refused",
                    reason_code="INVALID_CALLBACK_PAYLOAD",
                    http_status=200,
                )
                outcomes.append(
                    ItemOutcome(
                        external_event_id=parsed.external_event_id,
                        outcome=OUTCOME_REFUSED,
                        inbox_id=None,
                        event_id=None,
                        duplicate=False,
                        reason_code="INVALID_CALLBACK_PAYLOAD",
                        pointer_published=False,
                    )
                )
                continue
            if parsed.outcome == OUTCOME_DECLINED:
                # **A message we understood and will not handle** -- today, a
                # group message (user ruling 2026-09-10, after the v1 survey).
                #
                # **Refused rather than ignored, and the difference is the
                # merchant's.** An ignored element leaves the customer with no
                # answer and the merchant with nothing that says why; a refusal
                # is a `200` with a business error body and an audit row naming
                # the reason, so "we do not do group chats yet" is a fact
                # somebody can read. It is not a payload error either -- the
                # callback was perfectly well formed -- which is why it carries
                # its own reason code rather than `INVALID_CALLBACK_PAYLOAD`.
                #
                # **What the alternative costs**: a room message walked down the
                # 1:1 path would create a contact keyed on a group member's
                # personal id and deliver the AI's reply to that member in a
                # private chat.
                self.record_answer(
                    tenant_id=binding_tenant_id,
                    config_id=config_id,
                    kind="ingress.refused",
                    reason_code=parsed.declined_reason or DECLINED_ROOM,
                    http_status=200,
                )
                outcomes.append(
                    ItemOutcome(
                        external_event_id=parsed.external_event_id,
                        outcome=OUTCOME_DECLINED,
                        inbox_id=None,
                        event_id=None,
                        duplicate=False,
                        reason_code=parsed.declined_reason,
                        pointer_published=False,
                    )
                )
                continue
            if parsed.outcome == OUTCOME_IGNORED:
                # **Answered, recorded, and not fed to the pipeline** (user
                # ruling 2026-09-10). A group-membership notice or an
                # account-status callback is not a customer speaking; writing it
                # to `inbox_events` would run understanding, planning and
                # generation over it and reply to it. Refusing it would be the
                # other misreading -- ADR-0107 §一 names both, and this is the
                # third member of that family.
                #
                # **The audit row is not decoration**: `INGRESS_SQL` clears the
                # consecutive-failure streak on an `ingress.answered` row, and
                # that streak guards the irreversible deletion of the callback
                # address. A request made only of notices has to clear it.
                # **一件副作用，而不是一次改判**（用户 2026-09-16 裁定）。
                #
                # 上传结果就走这条 `cmd=20000`，而它**确实**不是客户在说话 ——
                # 分类保持原样。变的只是：在答复它之前，顺手把目录里那一行补齐。
                #
                # **判据是 `requestId` 在不在目录里**，不是这个元素长什么样。
                # 同一个 `cmd` 底下还有取件结果，`msgData` 几乎一样；
                # 它的 `requestId` 一行都匹配不到，于是认领是个空操作 ——
                # 这正是 `065` 说的「分流的唯一判据」。
                upload_failed = self._claim_upload(
                    tenant_id=binding_tenant_id,
                    parsed=parsed,
                    # **信封上的 code，只在这里取。** 一条回调里十二个元素共用
                    # 一个信封，而上传失败时元素里什么都没有（`msgData` 为 null）——
                    # 失败的原因**只在信封上**。
                    envelope_detail=envelope_status(payload),
                )
                self.record_answer(
                    tenant_id=binding_tenant_id,
                    config_id=config_id,
                    kind="ingress.answered",
                    # **失败走自己的原因码** —— 运维那句话逐字是
                    # 「下一次十二份全失败时，要有一个东西涨一格」。
                    # 在此之前它与「有人退群了」共用同一个计数。
                    reason_code=(
                        IGNORED_UPLOAD_FAILED
                        if upload_failed
                        else (parsed.ignored_reason or IGNORED_NOTICE)
                    ),
                    http_status=200,
                )
                outcomes.append(
                    ItemOutcome(
                        external_event_id=parsed.external_event_id,
                        outcome=OUTCOME_IGNORED,
                        inbox_id=None,
                        event_id=None,
                        duplicate=False,
                        reason_code=parsed.ignored_reason,
                        pointer_published=False,
                    )
                )
                continue
            try:
                outcomes.append(
                    self._accept_item(
                        binding=binding,
                        config_id=config_id,
                        tenant_id=binding_tenant_id,
                        parsed=parsed,
                        now=now,
                        current_match=current_match,
                    )
                )
            except (
                IngressPayloadError,
                ValidationError,
                DataError,
                IngressAuthenticationError,
            ):
                # **One bad element must not cost the good ones.** Each element
                # gets its own transaction, so the ones already written stay
                # written; this one is answered as a refusal *of that element*
                # and the loop goes on. Before this batch the question could not
                # arise -- one request was one message -- and answering it the
                # other way would discard a customer's message because something
                # else in the same POST was malformed.
                #
                # **`IngressAuthenticationError` is caught here too, and that
                # changed** (independent review 2026-09-10, P1-2). The only
                # place `_accept_item` raises it is the replay branch -- "this
                # key is already recorded against a different message id" --
                # and while the key was the vendor's `requestId` that fired on
                # *ordinary* traffic and took the whole request down with it.
                #
                # The key is the message id now, so the branch is a statement
                # about **one element** rather than about the caller's
                # credentials: the credential checks all happen above, before
                # the loop, and still abort the request. Refusing the element
                # and going on is what "one failure must not cost the others"
                # means when the failure is about that element's identity.
                self.record_answer(
                    tenant_id=binding_tenant_id,
                    config_id=config_id,
                    kind="ingress.refused",
                    reason_code="INVALID_CALLBACK_PAYLOAD",
                    http_status=200,
                )
                outcomes.append(
                    ItemOutcome(
                        external_event_id=parsed.external_event_id,
                        outcome=OUTCOME_REFUSED,
                        inbox_id=None,
                        event_id=None,
                        duplicate=False,
                        reason_code="INVALID_CALLBACK_PAYLOAD",
                        pointer_published=False,
                    )
                )
        return IngressResult(items=tuple(outcomes))

    def _accept_item(
        self,
        *,
        binding: dict[str, Any],
        config_id: str,
        tenant_id: UUID,
        parsed: CallbackItem,
        now: datetime,
        current_match: bool,
    ) -> ItemOutcome:
        """Turn one element of `data` into an inbox row, a withdrawal, or a refusal.

        **This is the old `accept()` body from the payload down**, with three
        changes and no others: it reads the element rather than the request, the
        conversation id is derived rather than read, and it returns one item's
        outcome rather than the request's. Keeping the rest byte-identical is
        deliberate -- every paragraph below was written against a review finding,
        and a rewrite would have quietly re-decided all of them.
        """

        payload = parsed.item
        external_event_id = parsed.external_event_id
        occurred_at = parsed.occurred_at
        if occurred_at is None:
            # `timestamp` was absent or unreadable. **Refused rather than
            # defaulted to now()**: `occurred_at` orders the out-of-order
            # deliveries this channel really sends (the first real window
            # arrived with three backfilled messages whose `seq` was *lower*
            # than the live ones), and a fabricated instant would order them
            # by when we happened to receive them -- which is the one thing
            # the field exists not to do.
            raise IngressPayloadError(
                "invalid callback payload",
                binding_tenant_id=tenant_id,
                binding_config_id=config_id,
            )
        # **A withdrawal is not a customer message, and it is decided here --
        # before the content rule** (ADR-0107 §二.1). A `msgType=2063` payload
        # carries no content of its own, so the rule below would refuse it as an
        # empty payload: "the customer withdrew something" would be recorded as
        # `INVALID_CALLBACK_PAYLOAD`. ADR-0107 §一 names that misreading, and the
        # worse one next to it, as the default outcome of not deciding.
        #
        # **What this branch does not do is the decision itself** (candidate (a),
        # user ruling over the author's recommendation of (b)): no inbox row, no
        # queue pointer, no state machine transition, no slot withdrawn, no
        # scheduled event cancelled, no sent reply chased. The customer will see
        # the system go on referring to what they took back -- the cost is real,
        # is registered as **T218**, and is bounded today because M1b's tenant is
        # synthetic and no real customer withdraws anything.
        # **Classified in `callback.py`, not sniffed here** (B3-2b's
        # `is_withdrawal` read the top level, where a real callback has only
        # `code` / `msg` / `data`). The outcome is decided once, for the whole
        # element, before any of its fields are read.
        withdrawal = parsed.outcome == OUTCOME_WITHDRAWAL

        # **The identity checks run for a withdrawal too** (independent review
        # 2026-09-08, P3-1). They used to sit below this branch, so a 2063 whose
        # `external_event_id` carried a NUL escaped as `UntranslatableCharacter`
        # and the callback was answered `200` **with no row written at all** --
        # which contradicts this batch's own argument that "a withdrawal arrived
        # and did not say what for" beats no record. An id we cannot store is
        # refused here, exactly as it is on the ordinary path.
        if (
            not external_event_id
            or occurred_at.tzinfo is None
            or not storable_as_text(external_event_id)
            or len(external_event_id) > MAX_EXTERNAL_EVENT_ID
        ):
            raise IngressPayloadError(
                "invalid callback payload",
                binding_tenant_id=tenant_id,
                binding_config_id=config_id,
            )

        # **Derived here rather than above, and the order is the fix**
        # (independent review 2026-09-10, P1-1). The derivation may fall back on
        # `external_event_id` when the sender is unidentifiable, and `uuid5`
        # encodes its name as UTF-8 -- so running it *before* the check above
        # handed a lone surrogate straight to `uuid5`, which raised
        # `UnicodeEncodeError` out of `accept()`: not a refusal with a reason
        # code but a framework **500**, a vendor redelivery, and ten of those
        # delete the callback address (ADR-0103 §二.1).
        #
        # The batch had already fixed exactly this rule for `senderId`
        # (`_peer_id`); this was the same rule's third entrance, and the case
        # that was supposed to guard it passed a clean literal as the fallback
        # value, so the path had no coverage at all.
        #
        # **Both inputs are now checked before they can reach `uuid5`**:
        # `parsed.sender_id` by `_peer_id`, `external_event_id` by the block
        # immediately above.
        #
        # **Derived, because the vendor has no conversation concept**
        # (user ruling 2026-09-10; the reasoning is in
        # `wecom_proxy/callback.conversation_id_for`).
        conversation_id = conversation_id_for(
            tenant_id=tenant_id,
            config_id=config_id,
            # **An unidentifiable sender gets a conversation of its own**, keyed
            # on the message rather than on the empty identity. Keying every one
            # of them on `""` would put every stranger who ever wrote into a
            # single thread where each can read the others' history -- the
            # failure ADR-0048 K11 names for `contacts` ("a customer who never
            # stops talking"), and worse here, because a conversation carries
            # the replies too.
            #
            # The identity still *degrades* rather than refusing: the message
            # arrives, it just arrives alone.
            peer_id=parsed.sender_id or f"anonymous:{external_event_id}",
        )

        # **The content is parsed from whichever fields arrived** -- see
        # `wecom_proxy/inbound.py`, which is the other half of the degradation
        # rule below and says so. T206 requires the two halves to sit together:
        # "a value the database cannot hold" and "a field the payload did not
        # carry" have the same consequence (a lost message) and the same
        # handling (degrade to "this item is absent", keep the message).
        content = None if withdrawal else parse_content(payload)
        # **Read out of the payload here, next to the content it is a copy
        # of** (T205 / B3-4d). It is not part of the envelope and never
        # validated against a contract -- it is the unparsed original, kept so
        # that a parsing mistake in `parse_content` above has something to be
        # diagnosed against.
        raw_payload = retained_raw_payload(payload)
        text = content.get("text", "") if content else ""
        # **The two fields that cannot degrade to nothing** (T207, closed here
        # because T204 is the batch that re-reads what "refuse" means).
        #
        # `actor_id` may be dropped and the message still arrives -- an identity
        # is something we are prepared to do without, which is why T180 degrades
        # it. **These two are not**: a message with no *content* is not a
        # message, and an event with no id has no idempotency key. So an
        # unstorable value here is a refusal rather than a degradation.
        #
        # **"No content" replaced "no text" in B3-2b** (T201/T206), and the
        # distinction is the point rather than pedantry: text used to be the
        # only content there was, so the two read the same. Now an image with
        # no caption is a perfectly good message, and requiring `text` would
        # refuse it -- turning the batch that exists to stop half a message
        # being dropped into one that drops all of it. What stays refused is an
        # envelope that would carry nothing at all.
        #
        # Left to the database it was neither: `psycopg` raised
        # `UntranslatableCharacter` out of `accept()`, the callback got a
        # **500**, and the channel redelivered into the same crash -- ten of
        # those and the address is deleted (ADR-0103 §二.1). Refusing here makes
        # it a `200` with a business error, which is what a refusal we *meant*
        # has to look like.
        if not withdrawal and (
            content is None
            or not storable_as_text(text)
            or len(text) > MAX_TEXT
        ):
            raise IngressPayloadError(
                "invalid callback payload",
                binding_tenant_id=tenant_id,
                binding_config_id=config_id,
            )

        profile_release_id = UUID(str(binding["profile_release_id"]))
        event_id = uuid4()
        inbox_id = uuid4()
        correlation_id = uuid4()
        credential_hash = str(
            binding["current_token_hash"] if current_match else binding["next_token_hash"]
        )
        credential_fingerprint = credential_hash[:16]
        # **`requestId` is evidence, not the replay key** (independent review
        # 2026-09-10, P1-2). The first version of this batch made it the key,
        # on the reasoning that it is "the vendor's per-delivery identifier".
        # **The vendor's own document falsifies that**: three pairs of
        # *different* messages share a `requestId` (`docs/wecom_proxy.md` lines
        # 675/737, 1235/1299, 2055/2115 -- different `msgType`, `msgServerId`,
        # `seq` and `msgUniqueIdentifier`).
        #
        # The consequence was not a duplicate but a **loss**: the second message
        # hit the "same key, another event id" branch below, which raises
        # `IngressAuthenticationError` -- and the per-element loop deliberately
        # does not catch that, so the whole request aborted with
        # `CHANNEL_AUTHENTICATION_FAILED` and a `200`. Every element after it was
        # dropped and the vendor never redelivers.
        #
        # **The key is the message identity**, which is per message by
        # construction and is what a redelivery repeats. `requestId` stays in
        # `auth_evidence.nonce`, where it is a record of which delivery carried
        # the message rather than a claim about uniqueness.
        nonce = parsed.request_id
        replay_key_hash = sha256_token(external_event_id)
        # **The identity, and the difference between "who" and "nobody said".**
        # The envelope has carried `actor_id` since M0 and filled it with
        # `UNKNOWN_ACTOR` when the payload had none -- fine for an envelope,
        # where the field is required. It is **not** fine as a contact: every
        # unidentified sender would collapse into one row that looks like a
        # customer who never stops talking (ADR-0048 K11's triple would make it
        # exactly one row per binding).
        #
        # **Validated rather than coerced** (independent review 2026-09-06,
        # P2-2). `str(payload.get("actor_id"))` accepted anything: a dict
        # became the identity `"{'id': 'x'}"` on a unique index, and `0` /
        # `False` / `""` silently became "no identity" through a truthiness
        # test. This field only ever occupied a string slot in the envelope
        # before this batch; it is a table's key now, and it is checked like
        # one -- a non-string, or blank after trimming, is **no identity**,
        # which is the treatment an absent field already gets. Nothing new is
        # rejected: the message is still accepted and still processed.
        #
        # **Trimmed, and that is normalisation rather than tidiness**: without
        # it `' c1 '` and `'c1'` are two contacts for one person, and the
        # triple makes that permanent.
        #
        # **The literal placeholder is excluded too** (P3-2): a payload that
        # actually says `actor_id: "unknown"` would otherwise build exactly
        # the row this paragraph exists to prevent. The comment promised that;
        # now the code does it.
        #
        # **And the third member of that family: a string the database cannot
        # store** (T180, ADR-0095 §八). There are three ways a value fails to
        # become a contact -- not a string, too long, and **not encodable** --
        # they have the same consequence, and only the first two were handled.
        # A single `\x00` in `actor_id` made `psycopg` raise
        # `UntranslatableCharacter` out of `accept()`: `inbox_events` got **no
        # row**, the callback got a **500**, and the channel redelivered into
        # the same crash. **The message was lost over a field we were prepared
        # to do without.** So it degrades the same way the other two do -- no
        # identity, message accepted -- which is the ruling's exact wording.
        # **`senderId`, read by `callback.read_item`.** It arrives as a JSON
        # *number* (`1688852365307991`), so the old `isinstance(str)` test made
        # every real sender "no identity" -- every customer would have collapsed
        # into `UNKNOWN_ACTOR` and, with it, into no contact row at all. The
        # normalisation lives in one place for that reason.
        trimmed = parsed.sender_id
        # **Length is checked here for the same reason type is** (re-review
        # P2-1). An id longer than the column allows is not "an identity we
        # refuse" -- left to the database it takes the message down with it.
        # Too long is therefore **no identity**, exactly as a non-string is,
        # and the message is still accepted and still processed.
        external_user_id = (
            trimmed
            if (
                trimmed
                and trimmed != UNKNOWN_ACTOR
                and len(trimmed) <= MAX_EXTERNAL_USER_ID
                and storable_as_text(trimmed)
            )
            else None
        )
        actor_id = external_user_id or UNKNOWN_ACTOR
        bounded_occurred_at = min(
            max(occurred_at, now - CONTACT_CLOCK_WINDOW), now
        )
        envelope: dict[str, object] = {
            "schema_version": "1.0.0",
            "event_id": str(event_id),
            "external_event_id": external_event_id,
            "event_type": "customer_message",
            "actor": {"party": "customer", "actor_id": actor_id},
            "tenant_id": str(tenant_id),
            "conversation_id": str(conversation_id),
            "occurred_at": _iso(occurred_at),
            "received_at": _iso(now),
            "correlation_id": str(correlation_id),
            "causation_id": None,
            "runtime_deadline_at": _iso(now + timedelta(seconds=self._runtime_deadline_seconds)),
            "profile_release_id": str(profile_release_id),
            "aggregate_generation": None,
            "content": content,
            "auth_evidence": {
                "verifier_id": "wecom-proxy-per-config-token",
                "verifier_version": "1.0.0",
                "credential_fingerprint": credential_fingerprint,
                # **The fourth string from the payload** (independent review
                # 2026-09-07, P1-1). T207 named three that reach a `text`
                # column; this one reaches `jsonb`, so that sentence did not
                # cover it, and a `\x00` in it made psycopg raise
                # `UntranslatableCharacter` out of `accept()` -- a `500` for a
                # field we can do without. Unlike `text` and
                # `external_event_id`, **this one degrades**: the replay key is
                # a hash of the nonce and is computed above regardless, so all
                # that is lost here is a copy kept for evidence.
                "nonce": nonce if nonce and storable_as_text(nonce) else None,
                "timestamp": _iso(occurred_at),
            },
            "scheduled_event_id": None,
            "business_event": None,
            "raw_payload_ref": None,
        }
        if not withdrawal:
            validate_contract("message-envelope", envelope)

        duplicate = False
        with self._database.transaction(
            tenant_id=tenant_id, correlation_id=correlation_id
        ) as connection:
            replay_inserted = connection.execute(
                """
                INSERT INTO ingress_replay_keys(
                    tenant_id, config_id, credential_fingerprint,
                    replay_key_hash, external_event_id
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING external_event_id
                """,
                (
                    tenant_id,
                    config_id,
                    credential_fingerprint,
                    replay_key_hash,
                    external_event_id,
                ),
            ).fetchone()
            if replay_inserted is None:
                replay = connection.execute(
                    """
                    SELECT external_event_id FROM ingress_replay_keys
                     WHERE tenant_id = %s AND config_id = %s
                       AND credential_fingerprint = %s AND replay_key_hash = %s
                    """,
                    (tenant_id, config_id, credential_fingerprint, replay_key_hash),
                ).fetchone()
                if replay is None or replay["external_event_id"] != external_event_id:
                    raise IngressAuthenticationError(
                        "channel authentication failed",
                        binding_tenant_id=tenant_id,
                        binding_config_id=config_id,
                    )
            # **The withdrawal joins the pipeline here rather than skipping
            # it** (independent review 2026-09-08, P2-1 + P2-2 -- one cause,
            # two symptoms). The branch used to `return` before this
            # transaction, and in doing so it walked past the two things this
            # pipeline provides for free:
            #
            # * **idempotency.** The provider redelivers on transport failure --
            #   ADR-0103 §二.1 says so in as many words -- and the replay key is
            #   what tells a redelivery from a second event. Skipping it, three
            #   deliveries of one withdrawal wrote three audit rows and answered
            #   `duplicate: false` each time. The batch's only deliverable is
            #   that row, so "it says the customer withdrew three times when
            #   they withdrew once" lands squarely on the deliverable. It also
            #   meant anyone holding the callback address could write audit rows
            #   without bound, while the ordinary path is capped per
            #   `external_event_id`.
            # * **the "we answered 200" signal.** `INGRESS_SQL` clears the
            #   consecutive-failure streak on an `ingress.answered` /
            #   `ingress.refused` audit row or an `inbox_events` row carrying
            #   `config_id`. A withdrawal writes none of those, so a healthy 200
            #   left the streak standing -- and that gauge guards the
            #   *irreversible* deletion of the callback address. **This is the
            #   same failure the duplicate branch below already had a comment
            #   about**, reappearing on a new branch.
            if withdrawal:
                if replay_inserted is None:
                    # Same nonce, same event: a redelivery. Answered, counted,
                    # and deliberately not recorded twice.
                    duplicate = True
                else:
                    self._write_withdrawal_audit(
                        connection,
                        tenant_id=tenant_id,
                        config_id=config_id,
                        external_event_id=external_event_id,
                        conversation_id=conversation_id,
                        correlation_id=correlation_id,
                        event_id=event_id,
                        payload=payload,
                    )
                # Written on both paths: the streak is about *answering*, not
                # about whether this particular callback was new.
                self._write_answered(
                    connection,
                    tenant_id=tenant_id,
                    config_id=config_id,
                    correlation_id=correlation_id,
                    reason_code="WITHDRAWAL",
                )
                return ItemOutcome(
                    external_event_id=external_event_id,
                    outcome=OUTCOME_WITHDRAWAL,
                    inbox_id=None,
                    event_id=event_id,
                    duplicate=duplicate,
                    reason_code=None,
                    pointer_published=False,
                )

            inserted = connection.execute(
                """
                INSERT INTO inbox_events(
                    tenant_id, id, event_id, source, external_event_id, conversation_id,
                    event_type, profile_release_id, aggregate_generation, envelope, config_id,
                    provider_raw_data
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'customer_message', %s, NULL, %s, %s, %s)
                ON CONFLICT (tenant_id, source, external_event_id) DO NOTHING
                RETURNING id, event_id
                """,
                (
                    tenant_id,
                    inbox_id,
                    event_id,
                    binding["source"],
                    external_event_id,
                    conversation_id,
                    profile_release_id,
                    Jsonb(envelope),
                    # T24. This is the only place the binding is known for
                    # certain: it has just been authenticated. The worker copies
                    # it onto the aggregate it creates from this event.
                    config_id,
                    # **T205 / B3-4d: written here, read nowhere.** The column
                    # comment in `047` and
                    # `tests/contracts/test_raw_payload_has_no_consumer.py` are
                    # the two places that keep saying so, because the cost of a
                    # stored-and-unread field is not the storage -- it is the
                    # next person assuming it does something (`redact()`,
                    # `reconcile()`, `frame_generation`, `legal_hold`, all four
                    # of them in this repository, ADR-0103 §三).
                    #
                    # **A withdrawal does not reach this statement** and so
                    # keeps nothing: `msgType=2063` writes an audit row and
                    # returns above. That is a consequence of ADR-0107's
                    # ruling (a withdrawal is not an inbox event), not a
                    # decision taken here -- worth knowing before reading
                    # `provider_raw_data` as "every callback we ever received".
                    raw_payload,
                ),
            ).fetchone()
            if inserted is None:
                duplicate = True
                # **A duplicate is a `200`, so it clears the failure streak**
                # (B3-4b). Nothing else on this branch writes a row -- a replayed
                # delivery is not the customer speaking twice -- so without this
                # one the streak would survive a healthy answer and the gauge
                # would report a provider about to delete a callback that is in
                # fact being answered.
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        tenant_id, id, actor_id, action, resource_type, resource_id,
                        correlation_id, reason_code, evidence
                    ) VALUES (%s, %s, NULL, 'ingress.answered', 'channel_binding', NULL,
                              %s, 'DUPLICATE', %s)
                    """,
                    (
                        tenant_id,
                        uuid4(),
                        correlation_id,
                        Jsonb({"config_id": config_id, "http_status": 200}),
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT id, event_id FROM inbox_events
                     WHERE tenant_id = %s AND source = %s AND external_event_id = %s
                    """,
                    (tenant_id, binding["source"], external_event_id),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("idempotent inbox row disappeared")
                inbox_id = UUID(str(existing["id"]))
                event_id = UUID(str(existing["event_id"]))
            else:
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        tenant_id, id, actor_id, action, resource_type, resource_id,
                        correlation_id, reason_code, evidence
                    ) VALUES (%s, %s, NULL, 'inbox.create', 'inbox_event', %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        uuid4(),
                        inbox_id,
                        correlation_id,
                        "CHANNEL_AUTHENTICATED",
                        Jsonb({"verifier": "wecom-proxy-per-config-token"}),
                    ),
                )
                # **The contact, created here and nowhere else** (ADR-0048 K11,
                # migration 039). Three things about the placement are the
                # ruling rather than convenience:
                #
                # * **after authentication** -- `unified-authorization-layer.md:199`
                #   forbids writing an inbox row or creating a contact for a
                #   request that failed it, and everything above this line has
                #   passed;
                # * **in this transaction** -- a contact without its message, or
                #   a message whose sender was never recorded, are both states
                #   nothing later can repair;
                # * **only on the branch that inserted** -- a replayed delivery
                #   of the same message is not the customer speaking twice, so
                #   it must not move `last_seen_at` or the count.
                #
                # The identity is K11's triple, so this is an upsert on the
                # unique index rather than a read-then-write: two messages from
                # one customer arriving together must produce one contact, and
                # the second must not fail.
                if external_user_id is not None:
                    connection.execute(
                        """
                        INSERT INTO contacts(
                            tenant_id, id, config_id, external_user_id,
                            first_seen_at, last_seen_at, message_count
                        ) VALUES (%s, %s, %s, %s, %s, %s, 1)
                        ON CONFLICT (tenant_id, config_id, external_user_id) DO UPDATE
                           SET last_seen_at = GREATEST(
                                   contacts.last_seen_at, EXCLUDED.last_seen_at),
                               -- **The other direction, and it was missing**
                               -- (independent review P3-1). The column says
                               -- "when this identity first spoke"; without
                               -- `LEAST` it said "the first one we happened to
                               -- insert", which an out-of-order delivery makes
                               -- wrong. Same argument as `GREATEST` above,
                               -- pointing the other way.
                               first_seen_at = LEAST(
                                   contacts.first_seen_at, EXCLUDED.first_seen_at),
                               message_count = contacts.message_count + 1,
                               updated_at = now()
                        """,
                        (
                            tenant_id,
                            uuid4(),
                            config_id,
                            external_user_id,
                            # **The customer's clock, bounded by ours**
                            # (independent review P2-1). `occurred_at` says
                            # when they spoke and orders out-of-order
                            # deliveries correctly; `LEAST(..., now())` stops a
                            # payload claiming the future from pinning this
                            # contact where no later message can move it.
                            # `GREATEST` / `LEAST` in the SQL then keep each
                            # column moving in its own direction only.
                            # Bounded at both ends (P2-1 upper, P3-1 lower).
                            bounded_occurred_at,
                            bounded_occurred_at,
                        ),
                    )

        pointer_published = True
        try:
            self._queue.publish(inbox_id=inbox_id, tenant_id=tenant_id)
        except Exception:
            pointer_published = False
        return ItemOutcome(
            external_event_id=external_event_id,
            outcome=OUTCOME_MESSAGE,
            inbox_id=inbox_id,
            event_id=event_id,
            duplicate=duplicate,
            reason_code=None,
            pointer_published=pointer_published,
        )
