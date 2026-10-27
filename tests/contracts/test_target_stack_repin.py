"""The target-stack half of an explicit repin (T15, RFC 9.1).

These are the rules that do not need a database: what a repin does to the frames
themselves, and what it refuses to guess at.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastagent_v2.worker.target_stack import (
    repair_target_stack_for_repin,
    stack_requires_human_disposition,
)

OCCURRED_AT = "2026-09-03T00:00:00Z"


def _profile(*definition_ids: str) -> dict[str, Any]:
    return {
        "target_frames": [
            {"id": definition_id, "goal": f"goal for {definition_id}"}
            for definition_id in definition_ids
        ]
    }


#: What a frame carries **before** the repin, and it is deliberately not what
#: `_profile` declares (independent review 2026-09-09, P2-1). The two used to be
#: the same string, so every `goal` assertion in this file held whether
#: re-grounding happened or not: deleting `frame["goal"] = definition["goal"]`
#: outright -- the whole subject of `repair_target_stack_for_repin` -- produced
#: zero new failures, measured on both this branch and `main`. A fixture that
#: cannot tell the two states apart cannot witness either of them.
STALE_GOAL = "stale goal for {definition_id}"


def _frame(definition_id: str, status: str, *, frame_id: str) -> dict[str, Any]:
    return {
        "id": frame_id,
        "definition_id": definition_id,
        "goal": STALE_GOAL.format(definition_id=definition_id),
        "status": status,
        "slots": {},
        "entered_at": OCCURRED_AT,
        "last_progress_at": OCCURRED_AT,
        "parent_frame_id": None,
        "frame_generation": 3,
        "resume_confirmation_policy": "required",
    }


def _aggregate(*frames: dict[str, Any], generation: int = 3) -> dict[str, Any]:
    return {"generation": generation, "target_stack": list(frames), "state": {}}


@pytest.mark.security
def test_a_repin_bumps_the_aggregate_generation_and_leaves_the_frame_pins_alone() -> None:
    """One bump, and **it does not reach into the frames** (ADR-0098 §二, B4-3).

    Before B4-3 this function restamped every workable frame with the new
    generation, and this case asserted that. The ruling made
    `frame_generation` a **creation pin** -- 「在帧创建时写一次，之后不动」 --
    and a repin re-grounds the frames that exist rather than creating new ones.

    **What a repin has to invalidate is timers**, and that is the aggregate
    generation bumped here plus RFC §9.1's invalidation; neither reads the
    frame pin. So the two things this case now holds apart are: the aggregate
    moves, the frames keep their identity.
    """

    aggregate = _aggregate(_frame("welcome", "active", frame_id="f1"), generation=7)
    repair = repair_target_stack_for_repin(
        aggregate=aggregate, profile=_profile("welcome"), occurred_at=OCCURRED_AT
    )
    assert repair.aggregate["generation"] == 8
    assert repair.aggregate["target_stack"][0]["frame_generation"] == 3, (
        "the frame was created under generation 3 and a repin does not re-create it"
    )
    # The input is not mutated: the caller still holds the pre-repin state for
    # its optimistic-concurrency check.
    assert aggregate["generation"] == 7
    assert aggregate["target_stack"][0]["status"] == "active"


@pytest.mark.security
def test_a_repin_re_grounds_frames_without_changing_any_status() -> None:
    """Repair here means re-grounding, and it must not move a status.

    The first version set every workable frame to `repair_pending`, including
    the top one, reasoning that `repair_pending` is still workable so nothing was
    stranded. The independent review (2026-09-03, P0-2) disproved that against
    the validator: with `repair_pending` on top, `keep` is rejected
    `KEEP_ON_INACTIVE_FRAME`, `push` `PUSH_ON_INACTIVE_FRAME`, `complete`
    `EXIT_CONDITION_NOT_SATISFIED`, `abandon` `ABANDON_WITHOUT_EXIT_SIGNAL`, and
    `repair` / `request_pop` `STACK_OP_REQUIRES_PARENT` -- and `repair` advances
    the *parent* anyway, so a single-frame stack has no route back to `active`.
    `safe_fallback_plan` hard-codes `keep`, so the event would burn its retry
    budget and land `failed`: no reply and no handoff.

    Terminal frames stay untouched, including their goal text: rewriting them
    would falsify what the conversation actually pursued under the old release.
    """

    repair = repair_target_stack_for_repin(
        aggregate=_aggregate(
            _frame("welcome", "completed", frame_id="f1"),
            _frame("triage", "abandoned", frame_id="f2"),
            _frame("collect", "suspended", frame_id="f3"),
            _frame("answer", "active", frame_id="f4"),
        ),
        profile=_profile("welcome", "triage", "collect", "answer"),
        occurred_at=OCCURRED_AT,
    )
    statuses = [frame["status"] for frame in repair.aggregate["target_stack"]]
    assert statuses == ["completed", "abandoned", "suspended", "active"]
    assert repair.unknown_definition_ids == ()
    # What repair does change: the goal text comes from the release now in force.
    # **What it no longer changes is the frame pins** (ADR-0098 §二, B4-3): this
    # line used to read `[3, 3, 4, 4]`, the two workable frames having been
    # restamped to the post-bump generation.
    stack = repair.aggregate["target_stack"]
    # **The line below used to read `[3, 3, 4, 4]`, and that pair of values was
    # doing two jobs** (independent review 2026-09-09, P2-1). B4-3 made the pins
    # uniform, which is correct under ADR-0098 §二 -- but the `3`/`4` boundary
    # was also the only assertion in the repository holding
    # `repair_target_stack_for_repin` to skipping terminal frames, and it went
    # with them. The `goal` assertions below take that job over, and they can
    # only take it because `STALE_GOAL` differs from what `_profile` declares.
    assert [frame["goal"] for frame in stack] == [
        "stale goal for welcome",
        "stale goal for triage",
        "goal for collect",
        "goal for answer",
    ], "terminal frames keep the goal they were pursued under; workable ones are re-grounded"
    assert [frame["frame_generation"] for frame in stack] == [3, 3, 3, 3]
    assert stack_requires_human_disposition(repair.aggregate) is None


@pytest.mark.security
def test_a_frame_the_new_profile_does_not_define_is_reported_not_guessed() -> None:
    """There is no correct rule for "your goal no longer exists".

    Mapping it onto some other frame invents a goal the customer never agreed
    to; silently dropping it loses the commitment. Both are worse than telling
    the caller, which hands the conversation to a human.
    """

    repair = repair_target_stack_for_repin(
        aggregate=_aggregate(
            _frame("welcome", "active", frame_id="f1"),
            _frame("retired_goal", "suspended", frame_id="f2"),
        ),
        profile=_profile("welcome"),
        occurred_at=OCCURRED_AT,
    )
    assert repair.unknown_definition_ids == ("retired_goal",)
    statuses = [frame["status"] for frame in repair.aggregate["target_stack"]]
    # No status moves either way; the unknown frame is simply left un-re-grounded
    # for the human who is about to receive the conversation.
    assert statuses == ["active", "suspended"]
    stack = repair.aggregate["target_stack"]
    # Both keep their creation pin -- the re-grounded one and the un-re-grounded
    # one alike (ADR-0098 §二). Before B4-3 the first read `4`, and the
    # difference between the two was how this case told them apart; the
    # difference that matters is now `goal`, asserted below.
    assert stack[0]["frame_generation"] == 3
    assert stack[1]["frame_generation"] == 3
    assert stack[0]["goal"] == "goal for welcome", "the known frame was re-grounded"
    assert stack[1]["goal"] == "stale goal for retired_goal", "the unknown one was not"


@pytest.mark.security
def test_a_terminal_frame_missing_from_the_new_profile_is_not_reported() -> None:
    """Only live frames matter. A completed goal that the new profile dropped
    needs no repair -- nobody will work it again."""

    repair = repair_target_stack_for_repin(
        aggregate=_aggregate(_frame("retired_goal", "completed", frame_id="f1")),
        profile=_profile("welcome"),
        occurred_at=OCCURRED_AT,
    )
    assert repair.unknown_definition_ids == ()
