"""The platform's built-in "no-goal" frame. **ADR-0099 (Q20), landing in B4-7.**

ADR-0031 gave the terminal turn a legal intent owner by having the *profile*
declare a closing frame, and kept a hard boundary for profiles that declare
none: hand off to a person rather than send a message with no owner. **T8
measured what that boundary costs once `complete` is reachable** -- every
successfully finished conversation produces a manual task, 0 to 100%.

ADR-0099 takes the third road: the platform supplies the owner. These cases are
about the two halves of that -- the frame exists for every tenant without being
declared, and it cannot be turned into a way around the rules the profile's own
frames obey.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected, frame_definition
from fastagent_v2.worker.platform_frames import (
    NO_GOAL_FRAME_DEFINITION_ID,
    PLATFORM_FRAME_ID_PREFIX,
    PLATFORM_STRATEGY_REGISTRY,
    frame_definitions,
    is_platform_frame,
    strategy_registration,
)
from fastagent_v2.worker.strategies import STRATEGY_CATALOG
from fastagent_v2.worker.target_stack import (
    TargetStackTransitionError,
    apply_target_stack_operation,
    enter_closing_frame,
    enter_no_goal_frame,
    repair_target_stack_for_repin,
    should_enter_closing_frame,
    should_enter_no_goal_frame,
    stack_requires_human_disposition,
)

OCCURRED_AT = "2026-09-10T00:00:00Z"


def _frame(
    frame_id: str, status: str = "active", *, definition_id: str = "welcome"
) -> dict[str, Any]:
    return {
        "id": frame_id,
        "definition_id": definition_id,
        "goal": f"goal for {definition_id}",
        "status": status,
        "slots": {},
        "slot_evidence": {},
        "entered_at": OCCURRED_AT,
        "last_progress_at": OCCURRED_AT,
        "parent_frame_id": None,
        "interruption_reason": None,
        "resume_confirmation_policy": "optional",
        "frame_generation": 2,
    }


def _profile(*definition_ids: str, closing: str | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "target_frames": [
            {
                "id": definition_id,
                "goal": f"goal for {definition_id}",
                "information_slots": [],
                "allowed_strategy_set": ["acknowledge", "close_goal"],
                "time_rules": [],
                "return_confirmation": {"policy": "optional"},
            }
            for definition_id in definition_ids
        ],
        "strategies": {
            "acknowledge": {
                "required_slots": [],
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            }
        },
    }
    if closing is not None:
        document["closing_frame_ref"] = closing
    return document


def _completed_root(definition_id: str = "welcome") -> dict[str, Any]:
    return {
        "generation": 2,
        "target_stack": [_frame("root", "completed", definition_id=definition_id)],
    }


# --------------------------------------------------------------- the frame itself


def test_every_profile_has_the_platform_frame_without_declaring_it() -> None:
    """**The whole point of ADR-0099**: 每个租户都有，不需要记得声明.

    A profile that declares one frame resolves two -- its own and the
    platform's -- and the second one is the same object for every tenant.
    """

    definitions = frame_definitions(_profile("welcome"))
    assert set(definitions) == {"welcome", NO_GOAL_FRAME_DEFINITION_ID}
    assert definitions[NO_GOAL_FRAME_DEFINITION_ID]["allowed_strategy_set"] == [
        "acknowledge",
        "no_message",
    ]


def test_a_profile_cannot_shadow_the_platform_frame() -> None:
    """Resolution writes the platform entry last, so a collision cannot win.

    Admission refuses such a profile outright (see the registry case in
    `test_profile_registry.py`); this is the second lock, for a document
    published before that guard existed.
    """

    shadow = _profile("welcome")
    shadow["target_frames"].append(
        {
            "id": NO_GOAL_FRAME_DEFINITION_ID,
            "goal": "a merchant's idea of what the platform frame is",
            "information_slots": [],
            "allowed_strategy_set": ["propose_order"],
            "time_rules": [],
            "return_confirmation": {"policy": "required"},
        }
    )
    resolved = frame_definitions(shadow)[NO_GOAL_FRAME_DEFINITION_ID]
    assert resolved["allowed_strategy_set"] == ["acknowledge", "no_message"]
    assert resolved["return_confirmation"]["policy"] == "forbidden"


def test_the_platform_frame_declares_no_slots_and_no_timers() -> None:
    """Both absences are load-bearing rather than incidental.

    No slots is what makes `complete` meaningless on it (the exit gate for
    `complete` is "are the required slots filled", vacuously true for an empty
    list -- which is why the refusal is explicit in both layers). No timers is
    why a finished conversation does not acquire new reasons to speak by
    finishing.
    """

    definition = frame_definitions(_profile("welcome"))[NO_GOAL_FRAME_DEFINITION_ID]
    assert definition["information_slots"] == []
    assert definition["time_rules"] == []
    assert definition["return_confirmation"]["policy"] == "forbidden"


def test_the_platform_frames_strategies_are_all_in_the_platform_catalog() -> None:
    """It may reference plays; it may not invent them (`strategies.py`)."""

    definition = frame_definitions(_profile("welcome"))[NO_GOAL_FRAME_DEFINITION_ID]
    for strategy in definition["allowed_strategy_set"]:
        assert strategy in STRATEGY_CATALOG
        assert strategy in PLATFORM_STRATEGY_REGISTRY


@pytest.mark.parametrize(
    ("definition_id", "expected"),
    [
        (NO_GOAL_FRAME_DEFINITION_ID, True),
        (f"{PLATFORM_FRAME_ID_PREFIX}anything_else", True),
        ("welcome", False),
        ("closing", False),
    ],
)
def test_the_reserved_namespace_is_what_marks_a_platform_frame(
    definition_id: str, expected: bool
) -> None:
    assert is_platform_frame(definition_id) is expected


# ------------------------------------------------------------- entering the frame


def test_a_profile_with_no_closing_frame_enters_the_platform_frame_instead_of_a_person() -> None:
    """**T8's cost, taken to zero.**

    Before this, a completed root under a profile that declared no closing frame
    produced `ROOT_TARGET_FRAME_COMPLETED` -- no send, `control_mode='human'`,
    one manual task, on *every* successfully finished conversation.
    """

    aggregate = _completed_root()
    profile = _profile("welcome")
    assert stack_requires_human_disposition(aggregate) == "ROOT_TARGET_FRAME_COMPLETED"
    assert not should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )
    assert should_enter_no_goal_frame(
        aggregate=aggregate, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )

    updated = enter_no_goal_frame(aggregate=aggregate, occurred_at=OCCURRED_AT)
    entered = updated["target_stack"][-1]
    assert entered["definition_id"] == NO_GOAL_FRAME_DEFINITION_ID
    assert entered["status"] == "active"
    assert entered["parent_frame_id"] == "root"
    # Still the same turn: nothing was invalidated.
    assert updated["generation"] == 2
    # And the stack can accept work again, which is what stops the handoff.
    assert stack_requires_human_disposition(updated) is None


def test_the_profiles_own_closing_frame_still_goes_first() -> None:
    """A profile that declares one is describing how *it* wants the last word
    said; the platform frame is what happens when nobody described it."""

    aggregate = _completed_root()
    profile = _profile("welcome", "closing", closing="closing")
    assert should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )
    assert not should_enter_no_goal_frame(
        aggregate=aggregate, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )


def test_the_platform_frame_catches_what_falls_through_when_the_closing_frame_completes() -> None:
    """**The terminus ADR-0031 defined, re-pointed by ADR-0099.**

    `should_enter_closing_frame` refuses to push the closing frame onto itself,
    and before this batch what fell through there was the handoff. Now it is the
    platform frame, so the completion side never produces a manual task.
    """

    profile = _profile("welcome", "closing", closing="closing")
    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("closer", "completed", definition_id="closing"),
        ],
    }
    disposition = stack_requires_human_disposition(aggregate)
    assert disposition == "ROOT_TARGET_FRAME_COMPLETED"
    assert not should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    )
    assert should_enter_no_goal_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    )


def test_an_abandoned_root_keeps_its_handoff() -> None:
    """**ADR-0099 §五.3**: abandonment and completion are not the same road.

    An abandoned root arrived with an exit signal or a request for a person, and
    a farewell is not what that conversation needs.
    """

    aggregate = {"generation": 2, "target_stack": [_frame("root", "abandoned")]}
    disposition = stack_requires_human_disposition(aggregate)
    assert disposition == "ROOT_TARGET_FRAME_ABANDONED"
    assert not should_enter_no_goal_frame(
        aggregate=aggregate, profile=_profile("welcome"), disposition=disposition
    )


def test_the_platform_frame_does_not_follow_itself() -> None:
    """The terminus, and **the producer `ROOT_TARGET_FRAME_COMPLETED` still has.**

    `complete` is refused on a platform frame, so the way a stack arrives here
    with one on top is `abandon`: the customer sent an exit signal after the
    goal was met. `[root completed, no-goal abandoned]` is not *all* abandoned,
    so the disposition is still `ROOT_TARGET_FRAME_COMPLETED` -- and pushing a
    second no-goal frame would answer "stop" by continuing. It keeps the
    handoff.
    """

    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("nogoal", "abandoned", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    disposition = stack_requires_human_disposition(aggregate)
    assert disposition == "ROOT_TARGET_FRAME_COMPLETED"
    with pytest.raises(TargetStackTransitionError):
        enter_no_goal_frame(aggregate=aggregate, occurred_at=OCCURRED_AT)

    # **Asserted for both profile shapes** (independent review 2026-09-10, P0-1).
    # The first version used only `_profile("welcome")` -- a profile with *no*
    # closing frame -- and that is the one shape in which the claim held. With a
    # declared closing frame, `should_enter_closing_frame` answered first and
    # this hand-off never happened at all, so the case passed while the sentence
    # it was written to defend was false for both shipped profiles.
    # Each shape gets the stack it can actually reach: a profile that declares a
    # closing frame goes through it first, so `[root, no-goal]` is not a state
    # that profile can be in. Asserting over an unreachable stack would be the
    # same mistake in the other direction.
    declared = _profile("welcome", "closing", closing="closing")
    reachable = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("closer", "completed", definition_id="closing"),
            _frame("nogoal", "abandoned", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    for profile, state in ((_profile("welcome"), aggregate), (declared, reachable)):
        disposition_here = stack_requires_human_disposition(state)
        assert disposition_here == "ROOT_TARGET_FRAME_COMPLETED"
        assert not should_enter_no_goal_frame(
            aggregate=state, profile=profile, disposition=disposition_here
        )
        assert not should_enter_closing_frame(
            aggregate=state, profile=profile, disposition=disposition_here
        ), "a conversation that has already closed must not close again"


def test_a_declared_closing_frame_is_entered_once_and_then_the_conversation_ends() -> None:
    """**Independent review 2026-09-10, P0-1** -- the sequence, in production functions.

    Both shipped profiles declare a closing frame, so this is the configuration
    that actually runs. Before the fix it grew without bound: the customer's
    exit signal was answered by entering the closing frame *again* -- another
    goodbye -- then the no-goal frame, then the closing frame, one extra frame
    per turn, `disposition` never leaving `None`, so a person was never
    involved. Depth 8 by turn 7 in the reproduction.

    The cause was that both entry predicates asked "is that frame on top right
    now" instead of "has this conversation already been there". `stack[-1]` was
    a sufficient reading only while the closing frame was the last thing that
    could ever be pushed -- which is exactly what B4-7 changed.
    """

    profile = _profile("welcome", "closing", closing="closing")
    aggregate: dict[str, Any] = _completed_root()

    # turn 1: the root completed, so the profile's own closing frame owns the turn.
    assert should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )
    aggregate = enter_closing_frame(
        aggregate=aggregate, profile=profile, occurred_at=OCCURRED_AT
    )

    # turn 2: the closing frame completes; the platform frame catches the fall.
    aggregate = apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "complete", "target_frame_definition_id": None},
        interruption_relation="same_goal",
        profile=profile,
        occurred_at=OCCURRED_AT,
    )
    disposition = stack_requires_human_disposition(aggregate)
    assert not should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    ), "the closing turn already happened; a second one is another goodbye"
    assert should_enter_no_goal_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    )
    aggregate = enter_no_goal_frame(aggregate=aggregate, occurred_at=OCCURRED_AT)
    assert len(aggregate["target_stack"]) == 3

    # turn 3: the customer says stop.
    aggregate = apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "abandon", "target_frame_definition_id": None},
        interruption_relation="exit",
        profile=profile,
        occurred_at=OCCURRED_AT,
    )
    disposition = stack_requires_human_disposition(aggregate)

    assert disposition == "ROOT_TARGET_FRAME_COMPLETED"
    assert not should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    )
    assert not should_enter_no_goal_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    )
    assert len(aggregate["target_stack"]) == 3, "the stack must stop growing"

    # ...and it stays that way however many turns follow.
    for _ in range(4):
        assert not should_enter_closing_frame(
            aggregate=aggregate, profile=profile, disposition=disposition
        )
        assert not should_enter_no_goal_frame(
            aggregate=aggregate, profile=profile, disposition=disposition
        )
    assert stack_requires_human_disposition(aggregate) == "ROOT_TARGET_FRAME_COMPLETED"


def test_entering_the_platform_frame_needs_no_profile_at_all() -> None:
    """The signature is the batch: `enter_closing_frame` takes a profile because
    the frame is the profile's; this one does not, because it is not."""

    updated = enter_no_goal_frame(aggregate=_completed_root(), occurred_at=OCCURRED_AT)
    assert updated["target_stack"][-1]["definition_id"] == NO_GOAL_FRAME_DEFINITION_ID


# ------------------------------------------------------- what it may not be used for


#: The evidence block every inference document carries. Shaped like
#: `return_path_builders`' so the two files agree about what a valid document is.
_EVIDENCE = {
    "provider": "test",
    "model": "test",
    "prompt_release": "test",
    "latency_ms": 0,
    "token_usage": {"input": 0, "output": 0},
}


def _plan(op: str, *, target: str | None = None, strategy: str = "acknowledge") -> dict[str, Any]:
    """A complete `plan-result`. **`validate_plan` validates the contract first**,
    so a short stub is refused before any target-stack rule is reached -- which
    would make these cases pass for the wrong reason."""

    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": op, "target_frame_definition_id": target},
        "selected_strategy_id": strategy,
        "strategy_reason_code": "DETERMINISTIC_TEST",
        "confidence": {
            "raw_score": 1.0,
            "calibrated_score": None,
            "assessment_version": "test",
            "factor_codes": ["deterministic_output"],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": [],
        "manual_review_reason": None,
        "model_evidence": _EVIDENCE,
    }


def _understanding(relation: str = "same_goal") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_id": str(uuid4()),
        "facts": [],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": relation, "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "用例夹具：这一轮的摘要。",
        "model_evidence": _EVIDENCE,
    }


def test_a_platform_frame_cannot_be_completed_by_the_transition_layer() -> None:
    """**ADR-0099 §五.2, the lifecycle question.**

    `complete`'s exit gate is "are the frame's required slots filled", and this
    frame declares none -- so that gate is vacuously satisfied. Without an
    explicit refusal the model could mark it `completed`, the whole stack would
    go terminal again, and the conversation would land on exactly the handoff
    this batch removes.
    """

    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("nogoal", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    with pytest.raises(TargetStackTransitionError, match="no goal to complete"):
        apply_target_stack_operation(
            aggregate=aggregate,
            operation={"op": "complete", "target_frame_definition_id": None},
            interruption_relation="same_goal",
            profile=_profile("welcome"),
            occurred_at=OCCURRED_AT,
        )


def test_a_platform_frame_can_still_be_abandoned() -> None:
    """`abandon` needs an exit signal, and a customer who says 别再发了 after the
    goal is met is asking for something real. ADR-0099 §五.3 keeps that road."""

    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("nogoal", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    updated = apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "abandon", "target_frame_definition_id": None},
        interruption_relation="exit",
        profile=_profile("welcome"),
        occurred_at=OCCURRED_AT,
    )
    assert updated["target_stack"][-1]["status"] == "abandoned"


def test_the_validator_refuses_completing_a_platform_frame_too() -> None:
    """**Both places or neither** (the N1 precedent).

    The validator runs first, so a rule living only in the transition layer
    would raise through the retry budget instead of landing on the no-send path
    with a reason code.
    """

    from fastagent_v2.worker.inference_validation import validate_plan

    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("nogoal", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan("complete"),
            understanding=_understanding(),
            aggregate=aggregate,
            profile=_profile("welcome"),
        )
    assert refused.value.reason_code == "COMPLETE_ON_PLATFORM_FRAME"


def test_the_platform_frame_cannot_be_pushed_by_a_plan() -> None:
    """It is entered by the platform, never proposed.

    `frame_definition` resolves platform ids since this batch, so without the
    refusal the model could push "there is no goal here" on top of a live goal
    -- and `complete` is refused on it, so the stack could not be unwound past
    it either.
    """

    from fastagent_v2.worker.inference_validation import validate_plan

    aggregate = {"generation": 2, "target_stack": [_frame("root")]}
    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan("push", target=NO_GOAL_FRAME_DEFINITION_ID),
            understanding=_understanding("new_goal"),
            aggregate=aggregate,
            profile=_profile("welcome"),
        )
    assert refused.value.reason_code == "PUSH_ONTO_PLATFORM_FRAME"


def test_the_deepest_stack_the_platform_allows_is_five() -> None:
    """**The number the regression assertion schema caps at, measured.**

    Independent review 2026-09-10 P2-3: `maxItems: 3` could no longer describe
    the stack a finished conversation ends on, and raising it is only as good
    as the number it is raised to. `MAX_STACK_DEPTH` counts frames that can
    still be worked, so the stack the platform permits is that plus the
    terminal frames sitting beneath them -- the completed root and the
    completed closing frame, which nothing pops (`complete` marks a frame in
    place, and `request_pop` needs a live parent to return to).

    Driven through the production validator and transition function rather than
    computed from the constant: "3 + 2" is exactly the arithmetic that stays
    green while ceasing to be true, which is the shape of this batch's own P0.
    """

    from fastagent_v2.worker.inference_validation import validate_plan

    profile = _profile("welcome", "closing", "goal_one", "goal_two", "goal_three")
    aggregate: dict[str, Any] = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed", definition_id="welcome"),
            _frame("closing", "completed", definition_id="closing"),
            _frame("nogoal", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    for goal in ("goal_one", "goal_two"):
        validate_plan(
            plan=_plan("push", target=goal),
            understanding=_understanding("new_goal"),
            aggregate=aggregate,
            profile=profile,
        )
        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=_plan("push", target=goal)["target_stack_operation"],
            interruption_relation="new_goal",
            profile=profile,
            occurred_at=OCCURRED_AT,
        )

    assert len(aggregate["target_stack"]) == 5
    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan("push", target="goal_three"),
            understanding=_understanding("new_goal"),
            aggregate=aggregate,
            profile=profile,
        )
    assert refused.value.reason_code == "STACK_DEPTH_EXCEEDED"


# ------------------------------------------------------------ the strategy registry


def test_the_platform_registers_its_own_plays_so_a_profile_need_not() -> None:
    """**The second door the ADR closes.**

    `validate_plan` requires the strategy to be registered by the profile, and
    `inference` reads that entry's `generation_constraints`. Under a profile
    that never registered `acknowledge`, the model's only legal play inside the
    platform's frame would be refused -- "the profile has to remember to
    declare it" arriving through a different door.
    """

    bare = _profile("welcome")
    bare["strategies"] = {"ask_slot": {"allowed_intent_kinds": [], "generation_constraints": []}}

    assert strategy_registration(bare, NO_GOAL_FRAME_DEFINITION_ID, "acknowledge") is not None
    assert strategy_registration(bare, NO_GOAL_FRAME_DEFINITION_ID, "no_message") is not None
    # ...and the profile's own frames are unaffected: theirs is still the contract.
    assert strategy_registration(bare, "welcome", "acknowledge") is None


def test_the_platform_registration_does_not_leak_into_a_merchant_frame() -> None:
    """A profile must not be able to widen what `acknowledge` may do inside the
    platform's frame, and the platform's entry must not replace a profile's own
    registration in a frame the profile owns."""

    profile = _profile("welcome")
    profile["strategies"]["acknowledge"]["allowed_intent_kinds"] = ["send_message", "handoff"]

    merchant = strategy_registration(profile, "welcome", "acknowledge")
    platform = strategy_registration(profile, NO_GOAL_FRAME_DEFINITION_ID, "acknowledge")
    assert merchant is not None and platform is not None
    assert merchant["allowed_intent_kinds"] == ["send_message", "handoff"]
    assert platform["allowed_intent_kinds"] == ["send_message"]


def test_a_strategy_the_platform_frame_does_not_allow_is_not_registered_by_it() -> None:
    """The registry is not a way past the frame's allowlist either."""

    assert (
        strategy_registration(_profile("welcome"), NO_GOAL_FRAME_DEFINITION_ID, "propose_order")
        is None
    )


# --------------------------------------------------------------------- the repin


def test_a_conversation_resting_in_the_platform_frame_survives_a_repin() -> None:
    """**The manual task, re-entering through the profile-publish door.**

    `repair_target_stack_for_repin` runs from the API with the *new* release
    document, and no release declares the platform's frames. Reported as
    `unknown_definition_ids`, the repin sets `control_mode='human'` -- which is
    the very handoff ADR-0099 removes, arriving from the other side.
    """

    aggregate = {
        "generation": 2,
        "target_stack": [
            _frame("root", "completed"),
            _frame("nogoal", definition_id=NO_GOAL_FRAME_DEFINITION_ID),
        ],
    }
    repair = repair_target_stack_for_repin(
        aggregate=aggregate, profile=_profile("welcome"), occurred_at=OCCURRED_AT
    )
    assert repair.unknown_definition_ids == ()
    assert repair.aggregate["target_stack"][-1]["status"] == "active"


def test_a_frame_the_new_release_really_does_not_declare_is_still_unknown() -> None:
    """The control: the repin still notices a merchant frame that vanished."""

    aggregate = {
        "generation": 2,
        "target_stack": [_frame("root", definition_id="a_frame_the_new_release_dropped")],
    }
    repair = repair_target_stack_for_repin(
        aggregate=aggregate, profile=_profile("welcome"), occurred_at=OCCURRED_AT
    )
    assert repair.unknown_definition_ids == ("a_frame_the_new_release_dropped",)


# ------------------------------------------------------- what the batch did not change


def test_the_intent_contract_still_requires_a_frame() -> None:
    """**ADR-0099 is not candidate ②.**

    Candidate ② relaxed `intent_records.target_frame_id` to nullable, and this
    batch deliberately does not: what changes is *who supplies the frame*, not
    whether one is required. The column is asserted from the migration text
    because that is where the contract lives.
    """

    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2]
        / "infrastructure"
        / "migrations"
        / "001_m0.sql"
    ).read_text(encoding="utf-8")
    intent_table = migration.split("CREATE TABLE IF NOT EXISTS intent_records")[1]
    intent_table = intent_table.split(");")[0]
    assert "target_frame_id uuid NOT NULL" in intent_table


def test_the_platform_frame_is_a_uuid_on_the_stack_like_any_other() -> None:
    """The instance carries a `uuid4`, so `target_frame_id NOT NULL` is
    satisfied by an ordinary value rather than by a sentinel."""

    from uuid import UUID

    updated = enter_no_goal_frame(aggregate=_completed_root(), occurred_at=OCCURRED_AT)
    assert UUID(str(updated["target_stack"][-1]["id"]))


def test_a_declared_closing_frame_still_works_the_way_it_did() -> None:
    """ADR-0031's road is narrowed, not removed: a profile that declares a
    closing frame still gets it, with the same shape as before."""

    profile = _profile("welcome", "closing", closing="closing")
    updated = enter_closing_frame(
        aggregate=_completed_root(), profile=profile, occurred_at=OCCURRED_AT
    )
    assert updated["target_stack"][-1]["definition_id"] == "closing"
    assert updated["target_stack"][-1]["status"] == "active"


def test_frame_definition_resolves_the_platform_frame_for_the_validator() -> None:
    """One resolution path, so the two layers cannot disagree about what exists."""

    assert (
        frame_definition(_profile("welcome"), NO_GOAL_FRAME_DEFINITION_ID)["id"]
        == NO_GOAL_FRAME_DEFINITION_ID
    )
    with pytest.raises(PlanRejected):
        frame_definition(_profile("welcome"), "a_frame_nobody_declared")
