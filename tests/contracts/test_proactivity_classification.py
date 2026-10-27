"""The classification, and the instant it stops being true. **B4-4a / RFC §6.4.**

These are the rules that need no database. The wiring -- that the pipeline
reaches any of it, and that the dispatcher acts on the instant -- is proved in
`tests/e2e/test_proactivity_window_reaches_the_row.py`, because a policy that
computes correctly and is never called is the shape this repository keeps paying
for.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastagent_v2.worker.proactivity import (
    IN_SESSION_CONTINUATION,
    RE_ENGAGEMENT,
    ProactivityKnobs,
    ProactivityPolicyError,
    ProactivityVerdict,
    classify,
    evaluate,
    knobs,
)

ACTIVE_WINDOW_SECONDS = 1800
LAST_INBOUND = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)


def _profile(**overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "active_window_seconds": ACTIVE_WINDOW_SECONDS,
        # Since B4-4b the parser needs it too; the cap itself is exercised
        # against the database in `tests/integration/test_reengagement_quota.py`.
        "reengagement_limits": {"window_seconds": 86400, "max_delivered": 1},
        # Since B4-4c the parser needs it too; the shipped profiles carry
        # exactly these three (ADR-0115 §三, which is why no release moved).
        "exit_signals": ["explicit_opt_out", "complaint", "human_takeover"],
        # Since B4-5 the parser needs it too. **Asia/Shanghai 22:30-08:00 is what
        # both shipped profiles declare**, and every instant these cases use
        # falls in the middle of the local afternoon -- so the window is present
        # and readable without any case here accidentally becoming a
        # quiet-hours refusal. `test_quiet_hours_refuse_and_say_when.py` is
        # where it does bite.
        "quiet_hours": {
            "timezone": "Asia/Shanghai",
            "ranges": [{"start": "22:30", "end": "08:00"}],
        },
    }
    policy.update(overrides)
    return {"proactivity_policy": policy}


def _aggregate(last_inbound_at: str | None = "2026-09-09T04:00:00Z") -> dict[str, Any]:
    return {"control_mode": "ai", "last_inbound_at": last_inbound_at, "target_stack": []}


def test_the_active_window_is_where_the_two_classes_divide() -> None:
    """RFC §6.4's boundary, and it is inclusive on the in-session side.

    「距客户最后一条消息 `≤ active_window_seconds`」 -- so a moment exactly one
    window after the customer spoke is still a continuation. The value matters
    because the two classes carry different rules, and an off-by-one at the
    boundary silently moves messages between them.
    """

    at_the_edge = LAST_INBOUND + timedelta(seconds=ACTIVE_WINDOW_SECONDS)
    assert (
        classify(
            last_inbound_at=LAST_INBOUND,
            now=at_the_edge,
            active_window_seconds=ACTIVE_WINDOW_SECONDS,
        )
        == IN_SESSION_CONTINUATION
    )
    assert (
        classify(
            last_inbound_at=LAST_INBOUND,
            now=at_the_edge + timedelta(seconds=1),
            active_window_seconds=ACTIVE_WINDOW_SECONDS,
        )
        == RE_ENGAGEMENT
    )
    # A customer who has never spoken has no session to continue. The other
    # reading -- treat a missing anchor as "inside the window" -- would give the
    # widest permission to the case with the least evidence.
    assert (
        classify(
            last_inbound_at=None, now=at_the_edge, active_window_seconds=ACTIVE_WINDOW_SECONDS
        )
        == RE_ENGAGEMENT
    )


def test_a_silence_check_consumed_outside_its_window_may_not_send() -> None:
    """RFC §6.4 names this kind specifically, and the reason is the last clause.

    「不得借旧 silence event 绕过再触达配额」 -- which is why an overdue
    `silence_check` is refused rather than reclassified as a re-engagement.
    Reclassifying is the bypass.

    **It is refused in this batch even though the quota it would bypass arrives
    in B4-4b**, because the bypass is created the moment the classification
    exists. A `goal_stall` in the same state is *allowed*, and that contrast is
    the assertion: the refusal is about this kind, not about the window.
    """

    stale = _aggregate("2026-09-08T04:00:00Z")
    refused = evaluate(
        knobs=knobs(_profile()),
        aggregate=stale,
        event_kind="silence_check",
        now=LAST_INBOUND,
        understanding=None,
        pending_exit_signal=None,
    )
    assert refused.effect == "deny"
    assert refused.reason_code == "PROACTIVITY_SILENCE_CHECK_OUTSIDE_WINDOW"
    assert refused.window_expires_at is None, "a refusal queues nothing, so it expires nothing"

    allowed = evaluate(
        knobs=knobs(_profile()),
        aggregate=stale,
        event_kind="goal_stall",
        now=LAST_INBOUND,
        understanding=None,
        pending_exit_signal=None,
    )
    assert allowed.effect == "allow"
    assert allowed.classification == RE_ENGAGEMENT


def test_the_dispatcher_deadline_is_the_earlier_of_the_window_and_the_next_quiet_hour() -> None:
    """ADR-0098 §一's 「到期时刻」, **now with two sources** (B4-5 / ADR-0119 §三.1).

    Until B4-5 this case was `…_is_the_window_and_a_re_engagement_has_none`, and
    that second half was true only because nothing else could expire a row. A
    quiet window opening ahead of the row expires **both** classes: a message
    judged at 22:00 and dispatched at 22:40 would go out inside the merchant's
    quiet hours, having been allowed before they began.

    So the assertion is the **earlier** of the two, and the re-engagement half
    is the interesting one: it used to be `None` **always**, and `None` was
    therefore a constant. `proactivity.py`'s own docstring had been carrying the
    note that 「the `min()` of the two belongs in the batch that has two」 --
    this is that batch.
    """

    now = LAST_INBOUND + timedelta(seconds=600)
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=now,
        understanding=None,
        pending_exit_signal=None,
    )
    assert verdict.classification == IN_SESSION_CONTINUATION
    # Anchored on the customer's clock plus the window, not on "now plus the
    # window": an implementation anchored on the wrong instant would still
    # produce a plausible timestamp.
    assert verdict.window_expires_at == LAST_INBOUND + timedelta(seconds=ACTIVE_WINDOW_SECONDS)

    stale = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate("2026-09-08T04:00:00Z"),
        event_kind="goal_stall",
        now=now,
        understanding=None,
        pending_exit_signal=None,
    )
    assert stale.classification == RE_ENGAGEMENT
    # **The half that changed.** A re-engagement still has no active window, but
    # it is not exempt from quiet hours -- so its deadline is the moment the
    # quiet window opens, 22:30 Shanghai on the day `now` falls in.
    assert stale.window_expires_at == datetime(2026, 9, 9, 14, 30, tzinfo=UTC)

    # And the in-session case keeps the *earlier* source: its active window
    # closes at 04:30 UTC, long before 22:30 local, so the quiet hour does not
    # get to move it later. A `max()` would pass the re-engagement assertion
    # above and fail here, which is why both halves are checked.
    assert verdict.window_expires_at < datetime(2026, 9, 9, 14, 30, tzinfo=UTC)


def test_a_quiet_window_opening_first_takes_over_the_deadline() -> None:
    """The other order, because `min()` has to be shown choosing each side.

    Here the customer wrote at 22:00 Shanghai, so the active window would close
    at 22:30... and the quiet window opens at exactly 22:30 too. Push the
    inbound later -- 22:20 local -- and the active window would run to 22:50,
    **past** the quiet boundary. The deadline must be the quiet one.

    Without this case a `min()` and a "always take the active window" would be
    indistinguishable in the suite.
    """

    late_inbound = datetime(2026, 9, 9, 14, 20, tzinfo=UTC)  # 22:20 Shanghai
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(late_inbound.isoformat().replace("+00:00", "Z")),
        event_kind="goal_stall",
        now=late_inbound + timedelta(seconds=60),
        understanding=None,
        pending_exit_signal=None,
    )
    assert verdict.classification == IN_SESSION_CONTINUATION
    assert verdict.window_expires_at == datetime(2026, 9, 9, 14, 30, tzinfo=UTC), (
        "the active window would have run to 22:50 local, past the quiet boundary"
    )


def test_a_profile_this_policy_cannot_read_is_refused_rather_than_defaulted() -> None:
    """A missing window is a profile fault, not a runtime one.

    Defaulting would silently give a merchant a window they never declared, and
    the merchant would never see it: every timer turn would classify against a
    number nobody wrote. `load_package` is where such a profile should be
    refused; this is what makes the refusal reach that far.
    """

    with pytest.raises(ProactivityPolicyError):
        knobs({"proactivity_policy": {}})
    with pytest.raises(ProactivityPolicyError):
        knobs({})
    # **Half a policy is refused too** (B4-4b). A profile with a window but no
    # `reengagement_limits` would otherwise classify every turn and cap none of
    # them -- a silent hole rather than a visible fault.
    with pytest.raises(ProactivityPolicyError):
        knobs({"proactivity_policy": {"active_window_seconds": ACTIVE_WINDOW_SECONDS}})
    # **And a policy with no `exit_signals` at all** (B4-4c). Defaulting to the
    # empty set would mean "this merchant declared that nothing stops proactive
    # sending" -- a statement nobody made.
    without = _profile()["proactivity_policy"]
    del without["exit_signals"]
    with pytest.raises(ProactivityPolicyError):
        knobs({"proactivity_policy": without})


def test_this_batch_declares_no_knob_and_no_field_it_cannot_act_on() -> None:
    """ADR-0117 §四, held to by a test rather than by a docstring.

    The batch this one was split out of computed `next_allowed_at` and
    `terminal_exit` and threaded them into the policy contract, where -- because
    the refusal path returns before the contract is built -- they were constant
    at the only place they landed. Their producers are quiet hours (B4-5) and
    `exit_signals` (B4-4c).

    **So the verdict must not carry them**, and the knobs must not carry the
    three fields nothing here reads. Asserting the shape is the cheapest thing
    that makes the next person add a field *and* its consumer, or neither.

    **The first version of this case did not hold.** It asserted
    `not hasattr(verdict, …)` and that parsing extra knobs changed nothing --
    and a probe that added `unread_extra: object = None` to `ProactivityKnobs`
    produced **zero** new failures, because a defaulted field changes neither
    `hasattr` on a different class nor the equality of two default-carrying
    instances. **A guard against constant fields that a constant field walks
    straight past is the defect it was written about**, so it is now a
    structural assertion on the field sets themselves.
    """

    # **Names, types and whether a default exists** (B4-4a independent review
    # 2026-09-09, P3-3). The first structural version compared only names, and
    # the reviewer pointed out that `active_window_seconds: int = 0` walks past
    # it -- a defaulted field is precisely the shape §四 is about, so a guard
    # that ignores defaults has the same hole its predecessor had, one level up.
    #
    # **B4-4b added the two `reengagement_*` entries together with the gate that
    # counts against them.** `quiet_hours` (B4-5) and `exit_signals` (B4-4c) are
    # still absent, and that is the assertion: a knob parsed here but acted on
    # nowhere is a value nothing reads.
    assert {
        (field.name, field.type, field.default is dataclasses.MISSING)
        for field in dataclasses.fields(ProactivityKnobs)
    } == {
        ("active_window_seconds", "int", True),
        ("exit_signals", "frozenset[str]", True),
        ("reengagement_window_seconds", "int", True),
        ("reengagement_max_delivered", "int", True),
        ("quiet_hours", "QuietHours", True),
    }, "a knob parsed here but acted on nowhere is a value nothing reads"
    assert {
        (field.name, field.default is dataclasses.MISSING)
        for field in dataclasses.fields(ProactivityVerdict)
    } == {
        ("classification", True),
        ("effect", True),
        ("reason_code", True),
        ("detail", True),
        ("window_expires_at", True),
        # **B4-5 brought it, with a producer and a column** (ADR-0119).
        # **Required, not defaulted**, and that is asserted rather than assumed:
        # a default would let the next refusal that expires by the clock be
        # built without one and land as `NULL`, which reads as 「没有失效时刻」 --
        # a wrong statement, not a missing value. `terminal_exit` is still
        # absent; `exit_signals` chose two reason codes over a boolean.
        ("next_allowed_at", True),
        # **B4-5 again, and for the same reason one level along** (independent
        # review 2026-09-09, P1-1). The quiet window that expires a queued row
        # has an end, and the dispatcher may not read a profile to find it --
        # so the answer travels with the row. Required, not defaulted: a
        # forgotten one lands as `NULL` in a column that defines `NULL` as
        # 「这条拒绝没有失效时刻」.
        ("window_lifts_at", True),
    }, "`terminal_exit` has no producer; the other two must stay required"
