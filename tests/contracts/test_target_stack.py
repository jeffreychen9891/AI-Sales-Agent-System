from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from fastagent_v2.worker.target_stack import (
    TargetStackTransitionError,
    apply_target_stack_operation,
    stack_requires_human_disposition,
    suspend_target_stack_for_human,
)

OCCURRED_AT = "2026-09-02T08:00:00Z"


def _profile() -> dict[str, object]:
    return {
        "target_frames": [
            {
                "id": "root",
                "goal": "Complete the root goal",
                "return_confirmation": {"policy": "required"},
            },
            {
                "id": "child",
                "goal": "Answer the interruption",
                "return_confirmation": {"policy": "optional"},
            },
        ]
    }


def _aggregate() -> dict[str, object]:
    return {
        "generation": 7,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": "root",
                "goal": "Complete the root goal",
                "status": "active",
                "slots": {},
                "entered_at": "2026-09-02T07:00:00Z",
                "last_progress_at": "2026-09-02T07:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": "required",
                "frame_generation": 6,
            }
        ],
    }


def test_push_suspends_parent_and_creates_generation_pinned_child() -> None:
    original = _aggregate()
    snapshot = deepcopy(original)

    updated = apply_target_stack_operation(
        aggregate=original,
        operation={"op": "push", "target_frame_definition_id": "child"},
        interruption_relation="side_question",
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )

    assert original == snapshot, "transition calculation must not mutate the prepared CAS input"
    parent, child = updated["target_stack"]
    assert parent["status"] == "suspended"
    # **The parent keeps the generation it was created with** (ADR-0098 §二,
    # B4-3): the pin says which incarnation of the frame this is, and being
    # suspended by an interruption does not make it a different frame. Before
    # B4-3 this read `== 7`, i.e. the aggregate's current generation, which is
    # what made the field a last-touched counter.
    assert parent["frame_generation"] == 6
    assert child == {
        "id": child["id"],
        "definition_id": "child",
        "goal": "Answer the interruption",
        "status": "active",
        "slots": {},
        # T25: a new frame starts with an empty provenance map, so the shape is
        # uniform whether or not anything has been written to it yet.
        "slot_evidence": {},
        "entered_at": OCCURRED_AT,
        "last_progress_at": OCCURRED_AT,
        "parent_frame_id": parent["id"],
        "interruption_reason": "side_question",
        "resume_confirmation_policy": "optional",
        "frame_generation": 7,
    }


def test_complete_then_pop_repairs_and_reactivates_parent() -> None:
    pushed = apply_target_stack_operation(
        aggregate=_aggregate(),
        operation={"op": "push", "target_frame_definition_id": "child"},
        interruption_relation="new_goal",
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )
    completed = apply_target_stack_operation(
        aggregate=pushed,
        operation={"op": "complete", "target_frame_definition_id": None},
        interruption_relation="same_goal",
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )
    assert completed["target_stack"][-1]["status"] == "completed"
    assert completed["target_stack"][-2]["status"] == "repair_pending"

    popped = apply_target_stack_operation(
        aggregate=completed,
        operation={"op": "request_pop", "target_frame_definition_id": None},
        interruption_relation="same_goal",
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )
    assert len(popped["target_stack"]) == 1
    assert popped["target_stack"][0]["status"] == "active"
    # Resuming a parent does not re-create it either (ADR-0098 §二).
    assert popped["target_stack"][0]["frame_generation"] == 6


def _apply(aggregate: dict[str, object], op: str, **extra: object) -> dict[str, object]:
    return apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": op, "target_frame_definition_id": extra.get("definition_id")},
        interruption_relation=str(extra.get("relation", "same_goal")),
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )


def _pushed(policy: str = "optional") -> dict[str, object]:
    profile = _profile()
    profile["target_frames"][1]["return_confirmation"]["policy"] = policy  # type: ignore[index]
    return apply_target_stack_operation(
        aggregate=_aggregate(),
        operation={"op": "push", "target_frame_definition_id": "child"},
        interruption_relation="new_goal",
        profile=profile,
        occurred_at=OCCURRED_AT,
    )


def test_repair_advances_the_parent_and_never_touches_the_child() -> None:
    """The defect this closes: `repair` used to be a synonym for `complete`.

    The validator checks the two at different strengths -- `complete` must
    satisfy the frame's required slots, `repair` only needs a parent -- so a
    model could mark an unsatisfied goal `completed` by renaming the op.
    `repair` now moves the parent along the RFC state table and leaves the child
    exactly as it was.
    """

    completed = _apply(_pushed(), "complete")
    child_before = deepcopy(completed["target_stack"][-1])  # type: ignore[index]

    repaired = _apply(completed, "repair")
    assert repaired["target_stack"][-2]["status"] == "resume_confirmation_pending"
    assert repaired["target_stack"][-1] == child_before


def test_repair_cannot_complete_a_frame_whose_exit_conditions_are_unmet() -> None:
    """The bypass, stated as a property: an active child stays active."""

    pushed = _pushed()
    with pytest.raises(TargetStackTransitionError, match="repair_pending"):
        _apply(pushed, "repair")
    assert pushed["target_stack"][-1]["status"] == "active"


def test_repair_requires_a_parent_frame_at_all() -> None:
    with pytest.raises(TargetStackTransitionError, match="requires a parent frame"):
        _apply(_aggregate(), "repair")


def test_a_required_confirmation_blocks_the_pop_until_repair_has_run() -> None:
    """RFC §4.3 puts a gate on the return path; before this it did not exist."""

    completed = _apply(_pushed(policy="required"), "complete")
    with pytest.raises(TargetStackTransitionError, match="confirmed return"):
        _apply(completed, "request_pop")

    repaired = _apply(completed, "repair")
    popped = _apply(repaired, "request_pop")
    assert [frame["status"] for frame in popped["target_stack"]] == ["active"]


def test_an_optional_confirmation_still_pops_directly() -> None:
    completed = _apply(_pushed(policy="optional"), "complete")
    popped = _apply(completed, "request_pop")
    assert [frame["status"] for frame in popped["target_stack"]] == ["active"]


def test_pop_refuses_a_child_that_is_not_resolved() -> None:
    with pytest.raises(TargetStackTransitionError, match="status"):
        _apply(_pushed(), "request_pop")


def test_a_stack_with_work_left_needs_no_human_disposition() -> None:
    assert stack_requires_human_disposition(_aggregate()) is None
    assert stack_requires_human_disposition(_pushed()) is None
    assert stack_requires_human_disposition(_apply(_pushed(), "complete")) is None
    suspended = suspend_target_stack_for_human(aggregate=_aggregate(), occurred_at=OCCURRED_AT)
    assert stack_requires_human_disposition(suspended) is None


def test_completing_the_root_frame_leaves_nothing_that_can_accept_work() -> None:
    """`keep`/`push` need an active frame, `repair`/`request_pop` need a parent.

    A completed root satisfies none of them, so without a disposition the
    conversation stays `ai`/`open` while every later message falls into a silent
    no-send and nobody is told.
    """

    completed_root = _apply(_aggregate(), "complete")
    assert [frame["status"] for frame in completed_root["target_stack"]] == ["completed"]
    assert stack_requires_human_disposition(completed_root) == "ROOT_TARGET_FRAME_COMPLETED"


def test_abandoning_the_root_frame_reports_its_own_reason() -> None:
    abandoned_root = _apply(_aggregate(), "abandon", relation="exit")
    assert stack_requires_human_disposition(abandoned_root) == "ROOT_TARGET_FRAME_ABANDONED"


def test_an_empty_stack_is_a_disposition_not_a_crash() -> None:
    assert stack_requires_human_disposition({"target_stack": []}) == "TARGET_STACK_EMPTY"


def test_human_handoff_suspends_resumable_frames_without_dropping_history() -> None:
    pushed = apply_target_stack_operation(
        aggregate=_aggregate(),
        operation={"op": "push", "target_frame_definition_id": "child"},
        interruption_relation="correction",
        profile=_profile(),
        occurred_at=OCCURRED_AT,
    )
    suspended = suspend_target_stack_for_human(
        aggregate=pushed, occurred_at="2026-09-02T08:01:00Z"
    )
    assert len(suspended["target_stack"]) == 2
    assert [frame["status"] for frame in suspended["target_stack"]] == [
        "suspended",
        "suspended",
    ]
    # Two frames, two pins, each from when that frame was created: the parent
    # from the fixture (6) and the child pushed during this test (7). A handoff
    # suspends both and creates neither (ADR-0098 §二).
    assert [frame["frame_generation"] for frame in suspended["target_stack"]] == [6, 7]
