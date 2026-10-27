"""L1: the query that has to happen before anyone writes down "unknown".

RFC `:228` and ADR-0006 item 4 put this in M0's scope: *timeout 后先 reconcile；
无法确认则 UNKNOWN + manual task，禁止盲发*. `ChannelAdapter.reconcile` shipped in
the same commit as `DELIVERY_UNCONFIRMED` and then had **no production caller**
for a milestone -- the two halves of one requirement, one of them wired.

The tests below are about the wiring, not about the adapter (which already had
its own). What each asserts is the distinction the wiring exists to create:
**"we asked and it does not know" is a different fact from "we never asked"**,
and only the first may end an intent in UNKNOWN.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastagent_v2.worker.channel import (
    RECONCILE_BUDGET_SECONDS,
    ChannelCapabilities,
    SendCommand,
    SendReceipt,
)
from fastagent_v2.worker.pipeline import LEASE_SAFETY_MARGIN_SECONDS, OutboxDispatcher
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter


# **不是模块级常量，而这一格是本仓踩过的。**
#
# 它原本写成 `_deadline() = datetime.now(UTC) + timedelta(seconds=30)`——
# 一个**在 import（collection）那一刻**就固定下来的时刻。整棵树跑起来时，
# 从收集到这条用例真正执行之间超过那个窗口，它就已经过期了，
# 于是被测代码走的是「时间不够」那条分支，而用例读起来像是逻辑错了。
#
# **实测（2026-09-20）**：`pytest tests/contracts/test_reconcile_before_unknown.py`
# 单跑 17 条全绿；在 `pytest_collection_finish` 里插 26 秒再跑，**12 条红**，
# 与当天一次全量门禁里红的那 12 条逐条相同。
#
# 现取即可。**一个「还没到的时刻」必须相对它被用的那一刻算，不是相对 import。**
def _deadline() -> datetime:
    """调用时现取。理由见上。"""

    return datetime.now(UTC) + timedelta(seconds=30)
KEY = "intent:0123456789abcdef"
#: A lease with room to spare, which is the ordinary case: a send that failed
#: fast leaves most of the thirty seconds behind. The cases where it does not are
#: the two tests at the end of this file.
def _lease() -> datetime:
    """调用时现取。理由见上。"""

    return datetime.now(UTC) + timedelta(seconds=25)


class RecordingChannel:
    """A channel that answers as told and remembers how it was asked."""

    def __init__(
        self,
        *,
        send: SendReceipt,
        by_key: SendReceipt | None = None,
        by_operation: SendReceipt | None = None,
        can_be_asked_by_key: bool = True,
    ) -> None:
        self._send = send
        self._by_key = by_key
        self._by_operation = by_operation
        self._can_be_asked_by_key = can_be_asked_by_key
        self.calls: list[str] = []
        self.deadlines: list[datetime] = []
        # Kept out of `calls` on purpose: every assertion in this file reads
        # `calls` as "which round trips happened", and a declaration is not a
        # round trip. Counting it there would have made the existing cases pass
        # a list they were never about.
        self.capability_queries = 0

    def capabilities(self) -> ChannelCapabilities:
        self.capability_queries += 1
        return ChannelCapabilities(reconcile_by_key=self._can_be_asked_by_key)

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        self.calls.append("send")
        return self._send

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        self.calls.append("reconcile")
        self.deadlines.append(deadline_at)
        assert self._by_operation is not None, "this case did not expect an id lookup"
        return self._by_operation

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        self.calls.append("reconcile_by_key")
        self.deadlines.append(deadline_at)
        assert idempotency_key == KEY
        assert self._by_key is not None, "this case did not expect a key lookup"
        return self._by_key


def _dispatcher(channel: Any) -> OutboxDispatcher:
    return OutboxDispatcher(database=None, channel=channel, dispatcher_id="l1-test")  # type: ignore[arg-type]


def _resolve(channel: RecordingChannel, receipt: SendReceipt) -> SendReceipt:
    # `adapter=channel` since B2-3: the reconcile now runs through **the adapter
    # that sent the message**, not through `self._channel`. With a provider
    # factory wired those are different objects, and asking the M0 simulator
    # what became of a message WeComProxy was asked to deliver would be a
    # confident answer about the wrong system. Passing the same recorder here
    # keeps every assertion below about the same thing it was about before.
    return _dispatcher(channel)._reconcile_before_giving_up(
        receipt=receipt,
        idempotency_key=KEY,
        deadline_at=_deadline(),
        lease_deadline_at=_lease(),
        adapter=channel,
    )


def test_a_timeout_is_queried_by_key_because_it_left_no_operation_id() -> None:
    """The case the old code could not handle even in principle.

    A timeout produces no response and therefore no operation id, so a reconcile
    that only takes an id has nothing to take. That is not a small gap: timeouts
    and transport errors are exactly the failures that most need reconciling.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt(
            "delivered", "op-1", {"adapter": "http", "reconciled": True, "query_sent": True}
        ),
    )

    resolved = _resolve(channel, channel._send)

    assert channel.calls == ["reconcile_by_key"]
    assert resolved.status == "delivered"
    assert resolved.provider_operation_id == "op-1"
    assert resolved.evidence["reconcile_attempted"] is True


def test_an_unreadable_answer_is_queried_by_the_id_it_did_give() -> None:
    """When the provider did answer, ask about the thing it named."""

    channel = RecordingChannel(
        send=SendReceipt("unknown", "op-2", {"adapter": "http", "reason": "unknown_status"}),
        by_operation=SendReceipt(
            "failed", "op-2", {"adapter": "http", "reconciled": True, "query_sent": True}
        ),
    )

    resolved = _resolve(channel, channel._send)

    assert channel.calls == ["reconcile"]
    assert resolved.status == "failed"


def test_asking_and_being_told_nothing_is_recorded_as_having_asked() -> None:
    """The whole point of L1, as an assertion.

    The status is still `unknown` -- the reconcile could not confirm -- but the
    receipt now says the question was put. Before this, an UNKNOWN receipt was
    indistinguishable from one where nobody had asked, and that ambiguity is
    what let the missing call hide for a milestone.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt(
            "unknown",
            None,
            {"adapter": "http", "reconciled": True, "query_sent": True, "reason": "not_found"},
        ),
    )

    resolved = _resolve(channel, channel._send)

    assert resolved.status == "unknown"
    assert resolved.evidence["reconcile_attempted"] is True
    assert resolved.evidence["reconcile"]["reason"] == "not_found"
    assert "reconcile_skipped_reason" not in resolved.evidence


def test_the_two_round_trips_keep_separate_evidence() -> None:
    """The reconciled answer wins; the send's account of itself is not overwritten.

    The first version merged both dicts, and `reason` / `http_status` are written
    by both sides. Two things went wrong at once: with both present the send's
    was erased, and with only the send's present it read as the *query's*
    result -- an APPLIED receipt carrying `http_status: 500` looks like a query
    that returned 500 (review 2026-09-04, P2-1). Both sides say `reason` here on
    purpose: that is the collision.
    """

    channel = RecordingChannel(
        send=SendReceipt(
            "unknown", None, {"adapter": "http", "reason": "provider_unreachable"}
        ),
        by_key=SendReceipt(
            "accepted",
            "op-3",
            {"adapter": "http", "reconciled": True, "query_sent": True, "reason": "found"},
        ),
    )

    resolved = _resolve(channel, channel._send)

    assert resolved.status == "accepted"
    assert resolved.evidence["send"]["reason"] == "provider_unreachable"
    assert resolved.evidence["reconcile"]["reason"] == "found"
    assert resolved.evidence["reconcile_attempted"] is True
    # The old flat keys are gone, not shadowed: a reader who still expects
    # `reason` at the top level must fail loudly rather than read one of the two.
    assert "reason" not in resolved.evidence
    assert "send_status_before_reconcile" not in resolved.evidence


def test_a_query_that_never_went_out_is_not_recorded_as_having_been_asked() -> None:
    """P0-1. The finding that made this batch a re-review.

    `reconcile_attempted: True` was written unconditionally, while the adapter
    returns without sending a single packet when its budget is already spent.
    So a message the platform never asked about carried a receipt saying it
    had -- and L2b puts this field in front of the person who picks up the
    manual task, where "did we ask the provider" is the first thing they need.

    The whole reason this batch exists is to make "asked and got nothing"
    distinguishable from "never asked". A false positive between them is not a
    smaller version of the old bug; it is the old bug with a claim on top.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http", "reason": "provider_unreachable"}),
        by_key=SendReceipt(
            "unknown",
            None,
            {
                "adapter": "http",
                "reconciled": True,
                "query_sent": False,
                "reason": "deadline_exceeded_before_reconcile",
            },
        ),
    )

    resolved = _resolve(channel, channel._send)

    assert resolved.status == "unknown"
    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile_skipped_reason"] == "deadline_exceeded_before_reconcile"
    # And the send's own account survives, so the receipt can still say why the
    # message was in doubt in the first place.
    assert resolved.evidence["send"]["reason"] == "provider_unreachable"


def test_an_adapter_that_says_nothing_about_the_query_is_read_as_not_having_asked() -> None:
    """The default leans to under-claiming, and that direction is deliberate.

    An adapter that omits `query_sent` gets `reconcile_attempted: False`. Under-
    claiming costs a manual check; over-claiming is the P0 this batch is fixing.
    `HttpChannelAdapter` sets the flag on every return path and has its own
    guard for that (`test_channel_receipts.py`); this pins what happens to
    anything that does not.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt("unknown", None, {"adapter": "silent"}),
    )

    resolved = _resolve(channel, channel._send)

    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile_skipped_reason"] == "unspecified"


def test_the_query_gets_its_own_budget_and_not_what_the_send_left() -> None:
    """P1-1. The timeout branch could not reach the query it exists for.

    The send's HTTP timeout **is** the remaining turn budget, so a real timeout
    spends all of it by construction; the query that followed then found
    `remaining <= 0` and returned without sending anything. The review proved it
    with a real socket -- the server saw only the POST. RFC `:228` names timeout
    as *the* case for reconciling, and it was the one case the wiring could not
    serve.

    Here the send deadline is already in the past, which is exactly the state a
    timeout leaves behind. The query must still get a usable clock.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt(
            "delivered", "op-5", {"adapter": "http", "reconciled": True, "query_sent": True}
        ),
    )
    spent = datetime.now(UTC) - timedelta(seconds=1)

    resolved = _dispatcher(channel)._reconcile_before_giving_up(
        receipt=channel._send,
        idempotency_key=KEY,
        deadline_at=spent,
        lease_deadline_at=_lease(),
        adapter=channel,
    )

    assert channel.calls == ["reconcile_by_key"]
    assert channel.deadlines[0] > datetime.now(UTC), "the query was handed a dead clock"
    assert resolved.status == "delivered"


@pytest.mark.parametrize("status", ["accepted", "delivered", "failed"])
def test_a_definite_send_is_never_reconciled(status: str) -> None:
    """No query when there is nothing in doubt.

    A reconcile after a definite answer would be a second round trip per
    message, and worse, a second chance for the provider to contradict a receipt
    already written down.

    **This used to assert nothing.** It built a channel, called neither
    `dispatch_one` nor the helper, and then checked `channel.calls == []` --
    true of a channel nobody touched. The review's mutation proved it: turning
    the gate into `if True:` left the whole non-DB suite green (P2-3).

    The gate itself lives in `dispatch_one`, which needs a database, so the
    end-to-end version of this is in `tests/e2e/test_retry_budget.py`
    (`test_a_definite_receipt_is_not_second_guessed_by_a_query`) and it is that
    one which fails under the mutation. What is left here is the *precondition*
    check, and it is now written so it cannot pass vacuously: the helper is
    actually invoked, and a channel that is asked anything at all blows up.
    """

    definite = SendReceipt(status, "op-4", {"adapter": "http"})
    channel = RecordingChannel(send=definite)

    # `by_key` and `by_operation` are both None, so either query raises. The
    # assertion is that the helper is only ever reached with `unknown` -- the
    # same condition `dispatch_one` gates on.
    assert definite.status != "unknown"
    with pytest.raises(AssertionError):
        _resolve(channel, definite)
    assert channel.calls != [], "the helper must have been entered for this to mean anything"


def test_the_query_may_not_outlive_the_lease_on_the_row() -> None:
    """The clock the query got in P1-1 is now bounded by the one that matters.

    `RECONCILE_BUDGET_SECONDS` knew nothing about the outbox lease. On the
    timeout path the send has already spent nearly all thirty seconds of it, so
    two more seconds of query put the dispatcher past expiry -- and past expiry
    another dispatcher claims the row and **sends the message again**. The
    re-review's probe watched a second POST go out and the first dispatcher come
    back to a `UniqueViolation` on `delivery_receipts` (2026-09-04).

    So the deadline handed to the adapter is the earlier of the two clocks. Here
    the lease has 1.4s left, which is less than the budget, and the query must be
    told so rather than being handed its full two seconds.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt(
            "delivered", "op-9", {"adapter": "http", "reconciled": True, "query_sent": True}
        ),
    )
    lease = datetime.now(UTC) + timedelta(seconds=LEASE_SAFETY_MARGIN_SECONDS + 1.4)

    resolved = _dispatcher(channel)._reconcile_before_giving_up(
        receipt=channel._send,
        idempotency_key=KEY,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        lease_deadline_at=lease,
        adapter=channel,
    )

    assert channel.calls == ["reconcile_by_key"], "a lease with room left must still be used"
    handed = channel.deadlines[0]
    assert handed <= lease - timedelta(seconds=LEASE_SAFETY_MARGIN_SECONDS), (
        "the query was handed a deadline that outlives the lease: "
        f"{handed.isoformat()} vs lease {lease.isoformat()}"
    )
    assert handed < datetime.now(UTC) + timedelta(seconds=RECONCILE_BUDGET_SECONDS), (
        "the clamp did not bite: the full budget was handed over"
    )
    assert resolved.status == "delivered"


def test_a_lease_with_no_room_left_skips_the_query_and_says_which_clock_ran_out() -> None:
    """Not asking is allowed; claiming to have asked is not.

    When the lease is about to expire there is no time to ask *and* write the
    answer down, so the query is not made at all. The receipt then has to say
    which of the two clocks ran out: the adapter's
    `deadline_exceeded_before_reconcile` means the send's budget was gone, while
    this one means we still held the row and simply not for long enough. Both
    leave `reconcile_attempted` False, because P0-1's rule is that nobody writes
    "we asked" unless a packet went out -- and here not even the call is made.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http", "reason": "provider_unreachable"}),
    )

    resolved = _dispatcher(channel)._reconcile_before_giving_up(
        receipt=channel._send,
        idempotency_key=KEY,
        deadline_at=_deadline(),
        lease_deadline_at=datetime.now(UTC) + timedelta(seconds=0.2),
        adapter=channel,
    )

    assert channel.calls == [], "the adapter must not be called with a dead lease"
    assert resolved.status == "unknown"
    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile_skipped_reason"] == "lease_expiring_before_reconcile"
    assert resolved.evidence["send"]["reason"] == "provider_unreachable"


def test_a_channel_that_says_it_cannot_be_asked_by_key_is_not_asked() -> None:
    """**B2-4 / T78 / ADR-0094 §三.** The declaration is read before the door is tried.

    Before this, a channel with no by-key lookup still got called and answered
    `unknown` with `query_sent: False` -- true, and arrived at by spending a
    round trip to be told something the adapter knew before it was called. The
    receipt now says the same thing without the trip, and says it about the
    channel rather than about this attempt.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "wecom_proxy", "reason": "provider_timeout"}),
        can_be_asked_by_key=False,
    )

    resolved = _resolve(channel, channel._send)

    assert channel.calls == [], "the adapter declared it cannot answer; asking anyway is the bug"
    assert channel.capability_queries == 1
    assert resolved.status == "unknown"
    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile_skipped_reason"] == "channel_cannot_be_queried_by_key"
    assert "reconcile" not in resolved.evidence, (
        "there was no second round trip, so there is no namespace describing one"
    )
    assert resolved.evidence["send"]["reason"] == "provider_timeout"


def test_not_asking_and_asking_in_vain_are_two_different_receipts() -> None:
    """The exit criterion of B2-4, read as a person reading two manual tasks.

    「答不了按键回查」 and 「问了、供应商说没有」 both end the message in the same
    state, and that is fine -- the state machine has one answer for both. What
    must not be the same is the record, because L2b puts these fields in front
    of whoever picks the message up, and "try again later" is right for exactly
    one of them.
    """

    cannot_ask = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "wecom_proxy"}),
        can_be_asked_by_key=False,
    )
    asked_in_vain = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "http"}),
        by_key=SendReceipt(
            "unknown",
            None,
            {
                "adapter": "http",
                "reconciled": True,
                "query_sent": True,
                "http_status": 404,
                "reason": "provider_has_no_record",
            },
        ),
    )

    silent = _resolve(cannot_ask, cannot_ask._send)
    answered = _resolve(asked_in_vain, asked_in_vain._send)

    assert silent.status == answered.status == "unknown", "the state machine has one answer"
    assert silent.evidence["reconcile_attempted"] is False
    assert answered.evidence["reconcile_attempted"] is True
    assert silent.evidence["reconcile_skipped_reason"] == "channel_cannot_be_queried_by_key"
    assert answered.evidence["reconcile"]["reason"] == "provider_has_no_record"
    assert silent.evidence != answered.evidence


def test_the_declaration_outranks_the_lease_because_it_outlives_this_attempt() -> None:
    """Two true reasons, and the durable one wins.

    A lease with no room left and a channel that cannot be asked are both
    reasons not to query, and here both hold. `lease_expiring_before_reconcile`
    invites a retry -- next time the lease will be fresh. The channel's answer
    will be the same one tomorrow, so that is the reason worth writing down.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", None, {"adapter": "wecom_proxy"}),
        can_be_asked_by_key=False,
    )

    resolved = _dispatcher(channel)._reconcile_before_giving_up(
        receipt=channel._send,
        idempotency_key=KEY,
        deadline_at=_deadline(),
        lease_deadline_at=datetime.now(UTC) + timedelta(seconds=0.2),
        adapter=channel,
    )

    assert resolved.evidence["reconcile_skipped_reason"] == "channel_cannot_be_queried_by_key"


def test_an_operation_id_is_still_queried_even_when_the_key_door_is_shut() -> None:
    """Only the by-key door is gated, because only it was revised.

    ADR-0094 §三 qualified `reconcile_by_key`. `reconcile` takes an id the
    provider itself issued, and an adapter that cannot answer that one either
    still says so through its own receipt, one round trip later. Gating both on
    a single flag would have been a second decision nobody made.
    """

    channel = RecordingChannel(
        send=SendReceipt("unknown", "op-7", {"adapter": "wecom_proxy"}),
        by_operation=SendReceipt(
            # 拆成多行不是风格偏好：这一行原本正好 100 字符（上限），
            # 而导出时 `wecom_proxy` → `wecom_proxy` 多一个字符，
            # **于是公开树里它是 E501，而源仓里是绿的**。
            "unknown",
            "op-7",
            {"adapter": "wecom_proxy", "query_sent": False, "reason": "no lookup"},
        ),
        can_be_asked_by_key=False,
    )

    resolved = _resolve(channel, channel._send)

    assert channel.calls == ["reconcile"]
    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile"]["reason"] == "no lookup"


def test_the_real_wecom_proxy_adapter_meets_the_real_dispatcher() -> None:
    """The seam the other cases each cover one side of (independent review 2026-09-09, P3-1).

    `tests/contracts/test_channel_capabilities.py` proves the adapter says
    `False`; the cases above prove a channel that says `False` is not asked.
    **Neither runs the two together**, and the reviewer measured what that
    costs: flipping the real declaration to `True` turned exactly one case red
    in all of `tests/contracts`. This file's own house style names the shape --
    "两边都是 mock，于是它们互相同意、与真客户端不同意"
    (`test_wecom_proxy_channel.py:198`).

    So this one takes the production adapter, hands it to the production
    dispatcher, and asserts the receipt the batch's exit criterion is written
    in -- the first row of the packet's three-row table, produced rather than
    assembled from two halves.
    """

    def refuse(**_: object) -> dict[str, object]:  # pragma: no cover - never called
        raise AssertionError("the dispatcher must not have reached the provider")

    adapter = WeComProxyChannelAdapter(send_text=refuse, guid="guid-1", recipient_id="wm-1")
    send = SendReceipt(
        "unknown", None, {"adapter": "wecom_proxy", "reason": "provider_call_failed"}
    )

    resolved = _dispatcher(adapter)._reconcile_before_giving_up(
        receipt=send,
        idempotency_key=KEY,
        deadline_at=_deadline(),
        lease_deadline_at=_lease(),
        adapter=adapter,
    )

    assert resolved.status == "unknown"
    assert resolved.evidence["reconcile_attempted"] is False
    assert resolved.evidence["reconcile_skipped_reason"] == "channel_cannot_be_queried_by_key"
    assert "reconcile" not in resolved.evidence
    assert resolved.evidence["send"]["reason"] == "provider_call_failed"
