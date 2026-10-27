"""`exit_signals` maps onto engine facts, and stops proactive sending. **B4-4c / ADR-0115.**

Before ADR-0115 the two vocabularies had **no word in common**: the three values
both shipped profiles declare appeared only in those profiles and one contract
test, and nothing in the engine produced any of them. Writing the first reader
therefore meant inventing a mapping, which `AGENTS.md` 图纸优先级 第 3/4 条 forbid
-- so the mapping was ruled first (ADR-0115 §二) and this is its execution.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastagent_v2.worker.proactivity import (
    EXIT_PERMANENT_REASON,
    EXIT_TEMPORARY_REASON,
    active_exit_signals,
    evaluate,
    knobs,
)

NOW = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
ALL_THREE = ["explicit_opt_out", "complaint", "human_takeover"]


def _profile(exit_signals: list[str] | None = None) -> dict[str, Any]:
    return {
        "proactivity_policy": {
            "active_window_seconds": 1800,
            "reengagement_limits": {"window_seconds": 86400, "max_delivered": 1},
            "exit_signals": ALL_THREE if exit_signals is None else exit_signals,
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
    }


def _aggregate(**overrides: Any) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "control_mode": "ai",
        "last_inbound_at": "2026-09-09T04:00:00Z",
        "target_stack": [],
    }
    aggregate.update(overrides)
    return aggregate


def _understanding(relation: str = "same_goal", markers: list[str] | None = None) -> dict[str, Any]:
    return {
        "interruption": {"relation": relation},
        "risk_markers": markers if markers is not None else ["none"],
    }


def _signal(relation: str) -> dict[str, Any]:
    return {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "relation": relation,
        "recorded_at": "2026-09-09T03:00:00Z",
    }


def test_each_declared_signal_maps_to_the_engine_fact_the_adr_names() -> None:
    """ADR-0115 §二's table, one row at a time.

    Each is asserted **alone**, because the interesting failure is a mapping
    that fires on the wrong input -- and a case that switched every input on at
    once would pass with all three wired to the same fact.
    """

    parsed = knobs(_profile())
    opted_out = active_exit_signals(
        knobs=parsed,
        aggregate=_aggregate(),
        understanding=_understanding(relation="exit"),
        pending_exit_signal=None,
    )
    assert opted_out == ("explicit_opt_out",)

    complained = active_exit_signals(
        knobs=parsed,
        aggregate=_aggregate(),
        understanding=_understanding(markers=["complaint"]),
        pending_exit_signal=None,
    )
    assert complained == ("complaint",)

    resumed = active_exit_signals(
        knobs=parsed,
        aggregate=_aggregate(),
        understanding=_understanding(),
        pending_exit_signal=_signal("human_request"),
    )
    assert resumed == ("human_takeover",)

    quiet = active_exit_signals(
        knobs=parsed,
        aggregate=_aggregate(),
        understanding=_understanding(),
        pending_exit_signal=None,
    )
    assert quiet == (), "nothing was said, so nothing may be reported"


def test_the_durable_half_counts_on_a_timer_turn() -> None:
    """ADR-0115 §二.1: 「持久的那一半必须算」.

    A timer turn's `understanding` describes a **system event** -- it carries
    no relation the customer chose and no risk markers about anything they
    said. Reading only this turn would mean a customer says 「别再发了」, a timer
    fires an hour later, and the platform has forgotten.

    So the durable signal alone, with an `understanding` that says nothing, has
    to produce the refusal.
    """

    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=_understanding(),
        pending_exit_signal=_signal("exit"),
    )
    assert verdict.effect == "deny"
    assert verdict.reason_code == EXIT_PERMANENT_REASON
    assert verdict.window_expires_at is None, "a refusal queues nothing, so it expires nothing"


def test_the_durable_half_is_spent_by_the_first_timer_and_the_second_one_is_let_through() -> None:
    """**T232. This case is green today, and the day it goes red is its point.**

    `PROACTIVITY_EXIT_PERMANENT` names the **kind** of refusal, not its
    lifetime. `process_prepared` clears `pending_exit_signal` in the same turn
    it feeds the signal to the planner and writes the cleared aggregate back,
    so the **second** timer arrives with nothing durable to read and is allowed
    through. The user ruled on 2026-09-09 to acknowledge that and register it
    rather than build persistence in this batch.

    So this asserts what the batch **does** claim -- second timer allowed --
    and not what the reason code's name suggests. **When T232 is wired
    and the refusal really does outlive one turn, this case fails**, and the
    batch that wires it has to come here and rewrite the second half
    deliberately. That is the only machine-readable form T232's
    expiry conditions have; today they live only in `docs/backlog/ideas.md`,
    where nothing goes off when they come due (independent re-review
    2026-09-09, R2).

    Both durable relations are run, because **they are the same field being
    cleared the same way** -- `human_request` is not a second mechanism with a
    second lifetime, which is exactly what the RFC §6.4 table used to imply
    (re-review R1).
    """

    durable = (
        ("exit", EXIT_PERMANENT_REASON),
        ("human_request", EXIT_TEMPORARY_REASON),
    )
    for relation, refusal in durable:
        first = evaluate(
            knobs=knobs(_profile()),
            aggregate=_aggregate(),
            event_kind="goal_stall",
            now=NOW,
            understanding=_understanding(),
            pending_exit_signal=_signal(relation),
        )
        assert first.effect == "deny", relation
        assert first.reason_code == refusal, relation

        # The same conversation, the next timer. Nothing else changed: the only
        # difference is that the turn above consumed the signal.
        second = evaluate(
            knobs=knobs(_profile()),
            aggregate=_aggregate(),
            event_kind="goal_stall",
            now=NOW,
            understanding=_understanding(),
            pending_exit_signal=None,
        )
        assert second.effect == "allow", relation
        assert second.reason_code == "PROACTIVITY_ALLOWED", relation


def test_a_signal_the_profile_did_not_declare_does_not_stop_anything() -> None:
    """ADR-0115 §二.2: `exit_signals` is a **declaration**, not a platform constant.

    A merchant who does not list `complaint` has decided that a complaint does
    not stop follow-ups. The marker is still there and still reaches every other
    judgement -- what the declaration governs is only this one.

    Same family as `strategy` and `cancel_on` (ADR-0105 §零.1): the field
    carries a commercial judgement, and reading it as a constant takes that
    judgement back.
    """

    complaining = _understanding(markers=["complaint"])
    declared = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=complaining,
        pending_exit_signal=None,
    )
    assert declared.effect == "deny"

    silent = evaluate(
        knobs=knobs(_profile(["explicit_opt_out"])),
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=complaining,
        pending_exit_signal=None,
    )
    assert silent.effect == "allow", "an undeclared signal is not a platform veto"


def test_a_takeover_stops_sending_even_when_the_profile_omits_it() -> None:
    """ADR-0115 §二.2's one exception, and it is an exception on purpose.

    Refusing to act while a human holds the conversation is an **engine
    invariant**; listing `human_takeover` in a profile makes the declaration
    match the fact, it does not make the fact switchable. A profile that omits
    it must still be refused.

    **And it is the temporary one**: control comes back. That is the whole
    reason there are two reason codes rather than one.
    """

    verdict = evaluate(
        knobs=knobs(_profile(["explicit_opt_out"])),
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=_understanding(),
        pending_exit_signal=_signal("human_request"),
    )
    assert verdict.effect == "deny"
    assert verdict.reason_code == EXIT_TEMPORARY_REASON


def test_permanent_and_temporary_are_two_reason_codes_not_one_plus_a_flag() -> None:
    """RFC §6.4's 「永久/临时 deny」, made queryable (user's ruling 2026-09-09).

    `policy-hook.schema.json` reserves `terminal_exit` for this, but it is not a
    column -- a boolean would land in `context` and the distinction would only
    be recoverable by parsing. Two codes are read by everything that already
    groups on `reason_code`.

    **The mixed case decides which way the rule leans**: a takeover *and* an
    opt-out together is permanent, because the opt-out outlives the takeover
    (user's ruling 2026-09-09).

    **And a complaint alone is temporary** -- also that ruling, and also not a
    derivation: a complaint is something a person handles, and when they have,
    the conversation should be able to continue. RFC §6.4 asks for 「永久/临时」
    without assigning the signals.
    """

    parsed = knobs(_profile())
    both = evaluate(
        knobs=parsed,
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=_understanding(relation="exit"),
        pending_exit_signal=_signal("human_request"),
    )
    assert both.reason_code == EXIT_PERMANENT_REASON
    assert "explicit_opt_out" in both.detail
    assert "human_takeover" in both.detail, "the record has to name every signal that bit"

    complained = evaluate(
        knobs=parsed,
        aggregate=_aggregate(),
        event_kind="goal_stall",
        now=NOW,
        understanding=_understanding(markers=["complaint"]),
        pending_exit_signal=None,
    )
    assert complained.reason_code == EXIT_TEMPORARY_REASON, (
        "a complaint is handled by a person and then the conversation continues"
    )


def test_an_exit_signal_outranks_the_silence_check_window_rule() -> None:
    """Order of finality: a standing refusal about the conversation beats one
    about this event.

    Both would refuse an overdue `silence_check`, so the assertion is on **which
    reason code** comes back. It matters because the two name different things a
    person would have to change: one is a customer who asked us to stop, the
    other is a timer that arrived too late.
    """

    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(last_inbound_at="2026-09-08T04:00:00Z"),
        event_kind="silence_check",
        now=NOW,
        understanding=_understanding(),
        pending_exit_signal=_signal("exit"),
    )
    assert verdict.reason_code == EXIT_PERMANENT_REASON


def test_the_shipped_profiles_declare_exactly_the_three_values_the_enum_allows() -> None:
    """Why the contract could be narrowed without republishing anything.

    ADR-0115 §三's argument is that both shipped profiles already carry exactly
    these three values, so tightening `exit_signals` to an enum moves the
    contract and not the content -- no `content_hash` changes, no release is
    republished.

    **That argument expires**, which is why it becomes an assertion rather than
    a paragraph (independent review 2026-09-09, §4).
    `test_synthetic_carrier_profile.py` already pins the carrier side;
    `synthetic-third` had no counterpart. With both pinned, the first profile to
    declare a fourth value hits the schema enum first, and the chain is closed.
    """

    root = Path(__file__).resolve().parents[2]
    for name in ("synthetic-carrier", "synthetic-third"):
        profile = json.loads((root / "profiles" / name / "profile.json").read_text("utf-8"))
        assert profile["proactivity_policy"]["exit_signals"] == ALL_THREE, name
