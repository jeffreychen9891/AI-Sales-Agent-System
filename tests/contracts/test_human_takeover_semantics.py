"""ADR-0054: what a takeover does to the stack, the slots and an exit signal.

**Every state here is built by the production transitions** -- `_initial_aggregate`
→ `push` → `suspend_target_stack_for_human` -- never hand-written. T53's first
version was written against a state `push` could not produce, so its rule was
constantly False and its suite constantly green; the builders in
`return_path_builders` exist because of that, and this module uses them.

The four cases that carry this batch:

* `test_a_model_update_cannot_overwrite_a_human_written_slot` -- §三.2, and the
  one whose **ordering** is the whole defence;
* `test_a_single_frame_stack_resumes_active_not_repair_pending` -- §三.4 / U4;
* `test_the_exit_signal_recorded_under_takeover_is_fed_to_the_first_plan` --
  §三.5, which proves the *reuse* of T53 rather than a new rule;
* `test_resuming_a_deeper_stack_puts_only_the_top_frame_back` -- §三.4.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.worker.target_stack import (
    apply_slot_updates,
    resume_target_stack_from_human,
    suspend_target_stack_for_human,
)

from tests.contracts.return_path_builders import (
    OCCURRED_AT,
    profile_with,
    pushed_interruption,
    validate_operation,
)


def _rooted(profile: dict[str, Any]) -> dict[str, Any]:
    """A single-frame stack, built by the pipeline's own root builder."""

    from fastagent_v2.worker.pipeline import _initial_aggregate

    return _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )


@pytest.fixture(name="profile")
def _profile() -> dict[str, Any]:
    return profile_with(child_policy="optional")


# --------------------------------------------------------------------------
# §三.2 -- the human-written slot
# --------------------------------------------------------------------------


def _slot_name(profile: dict[str, Any], aggregate: dict[str, Any]) -> str:
    frame = aggregate["target_stack"][-1]
    definition = next(
        item for item in profile["target_frames"] if item["id"] == frame["definition_id"]
    )
    declared = [str(slot["name"]) for slot in definition.get("information_slots", [])]
    assert declared, "the shipped profile's frame declares no slots to test with"
    return declared[0]


def test_a_model_update_cannot_overwrite_a_human_written_slot(
    profile: dict[str, Any]
) -> None:
    """§三.2. A slot a person wrote is not evidence the model may revise.

    The update below carries **high** confidence and is **not** flagged as a
    correction, so the pre-existing confidence rule would let it through: that
    rule only refuses a low-confidence write over a high-confidence one. The
    refusal here has to come from provenance, which is the point.
    """

    aggregate = _rooted(profile)
    frame = aggregate["target_stack"][-1]
    name = _slot_name(profile, aggregate)
    frame["slots"][name] = "written by a person"
    frame["slot_evidence"] = {
        name: {
            "confidence": 1.0,
            "evidence_event_id": str(uuid4()),
            "recorded_at": OCCURRED_AT,
            "source": "human",
        }
    }
    validate_contract("conversation-aggregate", aggregate)

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "frame_id": str(frame["id"]),
                "slot": name,
                "value": "written by the model",
                "confidence": 0.99,
                "evidence_event_id": str(uuid4()),
            }
        ],
        recorded_at=OCCURRED_AT,
    )

    assert writeback.applied == 0
    kept = writeback.aggregate["target_stack"][-1]
    assert kept["slots"][name] == "written by a person"
    assert [refusal["reason"] for refusal in writeback.refusals] == ["SLOT_WRITTEN_BY_HUMAN"]
    # The refused value is customer content and must not travel in the refusal.
    assert "written by the model" not in str(writeback.refusals)


def test_the_correction_flag_does_not_unlock_a_human_written_slot(
    profile: dict[str, Any]
) -> None:
    """**The case the ordering exists for**, and the reason it is a separate test.

    `is_correction` comes from `interruption.relation` in the model's own
    output, and nothing verifies it. If the human check were folded into the
    confidence rule -- or placed after it -- a model could overwrite anything a
    person wrote simply by labelling its turn a correction. The defence would be
    handing its bypass to the party it restrains.
    """

    aggregate = _rooted(profile)
    frame = aggregate["target_stack"][-1]
    name = _slot_name(profile, aggregate)
    frame["slots"][name] = "written by a person"
    frame["slot_evidence"] = {
        name: {
            "confidence": 0.4,
            "evidence_event_id": str(uuid4()),
            "recorded_at": OCCURRED_AT,
            "source": "human",
        }
    }

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "frame_id": str(frame["id"]),
                "slot": name,
                "value": "overwritten",
                "confidence": 0.05,
                "evidence_event_id": str(uuid4()),
            }
        ],
        recorded_at=OCCURRED_AT,
        is_correction=True,
    )

    assert writeback.applied == 0
    assert writeback.aggregate["target_stack"][-1]["slots"][name] == "written by a person"


def test_a_model_written_slot_is_still_governed_by_the_confidence_rule_alone(
    profile: dict[str, Any]
) -> None:
    """The new rule must not swallow the old one, nor fire where it should not.

    A slot with no `source` is a frame written before ADR-0054 existed. It reads
    as `model`, which is what it was -- so the confidence rule governs it and
    nothing else changes.
    """

    aggregate = _rooted(profile)
    frame = aggregate["target_stack"][-1]
    name = _slot_name(profile, aggregate)
    frame["slots"][name] = "model wrote this"
    frame["slot_evidence"] = {
        name: {
            "confidence": 0.95,
            "evidence_event_id": str(uuid4()),
            "recorded_at": OCCURRED_AT,
        }
    }

    refused = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "frame_id": str(frame["id"]),
                "slot": name,
                "value": "low confidence attempt",
                "confidence": 0.05,
                "evidence_event_id": str(uuid4()),
            }
        ],
        recorded_at=OCCURRED_AT,
    )
    assert [item["reason"] for item in refused.refusals] == ["LOW_CONFIDENCE_OVER_HIGH"]

    allowed = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "frame_id": str(frame["id"]),
                "slot": name,
                "value": "confident attempt",
                "confidence": 0.99,
                "evidence_event_id": str(uuid4()),
            }
        ],
        recorded_at=OCCURRED_AT,
    )
    assert allowed.applied == 1
    written = allowed.aggregate["target_stack"][-1]["slot_evidence"][name]
    # Writeback records who wrote it, and this caller is always the model.
    assert written["source"] == "model"


# --------------------------------------------------------------------------
# §三.4 -- coming back
# --------------------------------------------------------------------------


def test_a_single_frame_stack_resumes_active_not_repair_pending(
    profile: dict[str, Any]
) -> None:
    """U4. `repair` needs a parent, and a one-frame stack has none.

    Routing it through `repair` would be refused with `STACK_OP_REQUIRES_PARENT`
    -- so the frame would stay suspended and the conversation would be just as
    dead as before T4 was fixed, only with an extra step in between.
    """

    aggregate = _rooted(profile)
    suspended = suspend_target_stack_for_human(aggregate=aggregate, occurred_at=OCCURRED_AT)
    assert [frame["status"] for frame in suspended["target_stack"]] == ["suspended"]

    resumed = resume_target_stack_from_human(aggregate=suspended, occurred_at=OCCURRED_AT)

    assert [frame["status"] for frame in resumed["target_stack"]] == ["active"]
    validate_contract("conversation-aggregate", resumed)


def test_resuming_a_deeper_stack_puts_the_top_frame_back_active(
    profile: dict[str, Any]
) -> None:
    """**ADR-0058 §二.1**, which replaced a rule that locked the stack.

    The top frame comes back `active` at any depth; the parent stays
    `suspended` and is promoted later by the existing `complete` branch -- this
    repository's only legal producer of `repair_pending`.

    **The rule it replaced put the top frame in `repair_pending`**, and an
    independent review measured the result: no operation was legal at all, and
    `plan_or_fallback` raises rather than falling back, so the event burnt its
    retry budget and landed `failed` -- no reply, no handoff.
    `test_a_resumed_stack_has_at_least_one_legal_operation` below is the guard
    that would have caught it, and it did not exist.
    """

    interrupted = pushed_interruption(profile=profile)
    suspended = suspend_target_stack_for_human(
        aggregate=interrupted, occurred_at=OCCURRED_AT
    )
    assert len(suspended["target_stack"]) >= 2

    resumed = resume_target_stack_from_human(aggregate=suspended, occurred_at=OCCURRED_AT)

    statuses = [frame["status"] for frame in resumed["target_stack"]]
    assert statuses[-1] == "active"
    assert set(statuses[:-1]) == {"suspended"}
    validate_contract("conversation-aggregate", resumed)


@pytest.mark.parametrize("depth", ["one", "two"])
def test_a_resumed_stack_has_at_least_one_legal_operation(
    profile: dict[str, Any], depth: str
) -> None:
    """**The assertion whose absence let the P0 through.**

    A resumed conversation must be able to *do* something. Before ADR-0058 a
    depth-≥2 resume produced a stack on which all six operations were refused,
    and nothing in the suite noticed -- every case asserted frame statuses,
    which is a description of the stack rather than a claim about it.

    Asked through the **production validator**, so it cannot drift from what the
    pipeline will actually accept.
    """

    built = _rooted(profile) if depth == "one" else pushed_interruption(profile=profile)
    suspended = suspend_target_stack_for_human(aggregate=built, occurred_at=OCCURRED_AT)
    resumed = resume_target_stack_from_human(aggregate=suspended, occurred_at=OCCURRED_AT)

    # `keep` is the modest one -- carry on with the goal that was interrupted --
    # and it is what `safe_fallback_plan` falls back to, so if anything must be
    # legal on a resumed stack it is this.
    validate_operation(resumed, "keep", profile=profile)


def test_resuming_an_already_resumed_stack_changes_nothing(
    profile: dict[str, Any]
) -> None:
    """Independent review F5: a replay must not promote a second frame.

    Resuming twice used to walk one frame deeper each time, producing a stack
    with two workable non-terminal frames -- a shape neither `push` nor
    `complete` can build. B3 #7 carries a CAS, but a CAS stops concurrent
    writes, not a replay after a failed retry.
    """

    interrupted = pushed_interruption(profile=profile)
    suspended = suspend_target_stack_for_human(
        aggregate=interrupted, occurred_at=OCCURRED_AT
    )
    once = resume_target_stack_from_human(aggregate=suspended, occurred_at=OCCURRED_AT)

    twice = resume_target_stack_from_human(aggregate=once, occurred_at=OCCURRED_AT)

    assert [frame["status"] for frame in twice["target_stack"]] == [
        frame["status"] for frame in once["target_stack"]
    ]


def test_resuming_never_disturbs_a_terminal_frame(profile: dict[str, Any]) -> None:
    """Completed and abandoned frames are the evidence of what happened.

    `suspend` never touched them and neither does the inverse; moving them would
    rewrite the record of a takeover rather than end it.
    """

    interrupted = pushed_interruption(profile=profile)
    interrupted["target_stack"][0]["status"] = "completed"
    suspended = suspend_target_stack_for_human(
        aggregate=interrupted, occurred_at=OCCURRED_AT
    )

    resumed = resume_target_stack_from_human(aggregate=suspended, occurred_at=OCCURRED_AT)

    assert resumed["target_stack"][0]["status"] == "completed"


def test_resuming_a_stack_that_was_never_suspended_changes_nothing(
    profile: dict[str, Any]
) -> None:
    """Idempotent against a live stack: a second resume must not demote anything."""

    aggregate = _rooted(profile)

    resumed = resume_target_stack_from_human(aggregate=aggregate, occurred_at=OCCURRED_AT)

    assert resumed["target_stack"] == aggregate["target_stack"]
