"""Slot writeback and the closing frame (T6 + ADR-0031).

T6: `slot_updates` has been in the understanding contract since the beginning and
nothing ever wrote it, so `_missing_required_slots` read a dict nobody filled and
the `complete` exit-condition gate was unreachable outside fixtures.

ADR-0031: making it reachable is exactly what turns "the terminal turn hands off
to a human" from a cost of zero into a cost on every successfully finished
conversation. The closing frame gives that turn a legal intent owner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.target_stack import (
    TargetStackTransitionError,
    apply_slot_updates,
    apply_target_stack_operation,
    closing_frame_definition_id,
    enter_closing_frame,
    should_enter_closing_frame,
    should_enter_no_goal_frame,
    stack_requires_human_disposition,
)

OCCURRED_AT = "2026-09-03T00:00:00Z"
EVIDENCE_EVENT_ID = str(uuid4())


def _frame(
    frame_id: str, status: str = "active", *, definition_id: str = "welcome"
) -> dict[str, Any]:
    return {
        "id": frame_id,
        "definition_id": definition_id,
        "goal": f"goal for {definition_id}",
        "status": status,
        "slots": {},
        "entered_at": OCCURRED_AT,
        "last_progress_at": OCCURRED_AT,
        "parent_frame_id": None,
        "interruption_reason": None,
        "resume_confirmation_policy": "optional",
        "frame_generation": 2,
    }


def _update(frame_id: str, slot: str, value: Any) -> dict[str, Any]:
    return {
        "frame_id": frame_id,
        "slot": slot,
        "value": value,
        "confidence": 0.9,
        "evidence_event_id": EVIDENCE_EVENT_ID,
    }


def _profile(*definition_ids: str, closing: str | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "target_frames": [
            {
                "id": definition_id,
                "goal": f"goal for {definition_id}",
                "return_confirmation": {"policy": "optional"},
            }
            for definition_id in definition_ids
        ]
    }
    if closing is not None:
        document["closing_frame_ref"] = closing
    return document


def _slot_profile() -> dict[str, Any]:
    """A profile whose `welcome` frame declares the slots these tests write."""

    document = _profile("welcome")
    document["target_frames"][0]["information_slots"] = [
        {
            "name": "request_summary",
            "type": "string",
            "required": True,
            "description": "What this slot records.",
        },
        {
            "name": "routing_detail",
            "type": "string",
            "required": False,
            "description": "What this slot records.",
        },
    ]
    return document


def test_slot_updates_reach_the_frame_they_name() -> None:
    """The write that never existed. Without it `complete` cannot be satisfied."""

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[
            _update(frame_id, "request_summary", "broken login"),
            _update(frame_id, "routing_detail", "account team"),
        ],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 2
    assert updated["target_stack"][0]["slots"] == {
        "request_summary": "broken login",
        "routing_detail": "account team",
    }
    # The input is untouched: the caller still holds the pre-write state.
    assert aggregate["target_stack"][0]["slots"] == {}


def test_an_update_naming_a_frame_off_the_stack_is_ignored() -> None:
    """The model does not get to redirect a fact onto a goal it did not name."""

    aggregate = {"generation": 2, "target_stack": [_frame(str(uuid4()))]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(str(uuid4()), "request_summary", "x")],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 0
    assert updated["target_stack"][0]["slots"] == {}


def test_a_terminal_frames_slots_are_not_rewritten() -> None:
    """A completed goal's slots are the evidence of how it was satisfied."""

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id, "completed")]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", "late edit")],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 0
    assert updated["target_stack"][0]["slots"] == {}


def test_a_profile_without_a_closing_frame_declares_none() -> None:
    """Supported state, not a defect: ADR-0031's hard boundary is that such a
    profile still hands the terminal turn to a human."""

    assert closing_frame_definition_id(_profile("welcome")) is None
    assert closing_frame_definition_id(_profile("welcome", closing="closing")) == "closing"


def test_entering_the_closing_frame_gives_the_terminal_turn_an_owner() -> None:
    """The whole point of ADR-0031.

    Before this, `target_stack[-1]` after a root completion was a `completed`
    frame, so a customer-visible message had no legal owner and the turn was
    handed to a person instead.
    """

    root_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(root_id, "completed")]}
    assert stack_requires_human_disposition(aggregate) == "ROOT_TARGET_FRAME_COMPLETED"

    updated = enter_closing_frame(
        aggregate=aggregate,
        profile=_profile("welcome", "closing", closing="closing"),
        occurred_at=OCCURRED_AT,
    )
    closing = updated["target_stack"][-1]
    assert closing["definition_id"] == "closing"
    assert closing["status"] == "active"
    assert closing["parent_frame_id"] == root_id
    # Still the same turn, not a new external decision about the conversation.
    assert updated["generation"] == 2
    # And the stack can accept work again, which is what stops the handoff.
    assert stack_requires_human_disposition(updated) is None


def test_entering_a_closing_frame_the_profile_does_not_declare_is_refused() -> None:
    """Refuses rather than inventing a frame: an unowned send is the thing this
    whole mechanism exists to prevent."""

    aggregate = {"generation": 2, "target_stack": [_frame(str(uuid4()), "completed")]}
    with pytest.raises(TargetStackTransitionError):
        enter_closing_frame(
            aggregate=aggregate, profile=_profile("welcome"), occurred_at=OCCURRED_AT
        )


def test_an_abandoned_root_is_not_given_a_closing_frame_by_the_stack_rules() -> None:
    """`abandon` came with an exit signal or a human request.

    The disposition stays, and the pipeline keeps handing those conversations to
    a person; only `ROOT_TARGET_FRAME_COMPLETED` is redirected into the closing
    frame.
    """

    aggregate = {"generation": 2, "target_stack": [_frame(str(uuid4()), "abandoned")]}
    assert stack_requires_human_disposition(aggregate) == "ROOT_TARGET_FRAME_ABANDONED"


def test_the_three_conditions_for_entering_the_closing_frame_are_each_required() -> None:
    """One assertion per condition, so a regression names which one broke."""

    profile = _profile("welcome", "closing", closing="closing")
    completed_root = {
        "generation": 2,
        "target_stack": [_frame(str(uuid4()), "completed")],
    }
    assert should_enter_closing_frame(
        aggregate=completed_root, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )
    # 1. An abandoned root keeps its handoff.
    assert not should_enter_closing_frame(
        aggregate=completed_root, profile=profile, disposition="ROOT_TARGET_FRAME_ABANDONED"
    )
    # 2. A profile that declares no closing frame keeps its handoff.
    assert not should_enter_closing_frame(
        aggregate=completed_root,
        profile=_profile("welcome"),
        disposition="ROOT_TARGET_FRAME_COMPLETED",
    )
    # 3. A live conversation is not redirected at all.
    assert not should_enter_closing_frame(
        aggregate=completed_root, profile=profile, disposition=None
    )


def _real_profile() -> dict[str, Any]:
    """The shipped profile, so the walk-through uses definitions that exist."""

    return json.loads(
        (Path(__file__).resolve().parents[2] / "profiles/synthetic-third/profile.json")
        .read_text(encoding="utf-8")
    )


def _apply(aggregate: dict[str, Any], op: str, profile: dict[str, Any]) -> dict[str, Any]:
    return apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": op, "target_frame_definition_id": None},
        interruption_relation="continue",
        profile=profile,
        occurred_at=OCCURRED_AT,
    )


@pytest.mark.security
def test_completing_the_closing_frame_ends_the_conversation_for_real() -> None:
    """The terminus, driven through the real transitions instead of hand-built.

    The first version asserted this against a hand-built `[completed, completed]`
    stack -- a state the pipeline could not produce. What it actually produced
    was `[repair_pending, completed]`, because `complete` promoted the parent
    unconditionally, and from there `stack_requires_human_disposition` returned
    `None` forever: no handoff, no second closing frame, the machine still
    holding a conversation whose goal was met. Two more legal plans (`repair`,
    `request_pop`) reopened the finished root with its slots intact
    (independent review 2026-09-03, P0-1 and P1-1).

    So this walks the real sequence and asserts the end of it.
    """

    profile = _real_profile()
    root = _frame("root", definition_id="welcome")
    root["slots"] = {"request_summary": "done"}
    aggregate = {"generation": 2, "target_stack": [root]}

    after_root = _apply(aggregate, "complete", profile)
    assert stack_requires_human_disposition(after_root) == "ROOT_TARGET_FRAME_COMPLETED"
    assert should_enter_closing_frame(
        aggregate=after_root, profile=profile, disposition="ROOT_TARGET_FRAME_COMPLETED"
    )

    in_closing = enter_closing_frame(
        aggregate=after_root, profile=profile, occurred_at=OCCURRED_AT
    )
    assert stack_requires_human_disposition(in_closing) is None

    after_closing = _apply(in_closing, "complete", profile)
    # The completed root stays completed: only a *suspended* parent is waiting to
    # be resumed, and this one is finished.
    assert [frame["status"] for frame in after_closing["target_stack"]] == [
        "completed",
        "completed",
    ]
    # Which means the stack is wholly terminal and cannot be reopened.
    disposition = stack_requires_human_disposition(after_closing)
    assert disposition == "ROOT_TARGET_FRAME_COMPLETED"
    assert not should_enter_closing_frame(
        aggregate=after_closing, profile=profile, disposition=disposition
    )
    # **Where it goes from here changed in B4-7** (ADR-0099). Until then this
    # was the handoff -- the terminus ADR-0031 defined ended at a person. The
    # platform's no-goal frame now catches what falls through, so the completion
    # side produces no manual task at all; the *stack* facts above are unchanged,
    # which is why they still read the same.
    assert should_enter_no_goal_frame(
        aggregate=after_closing, profile=profile, disposition=disposition
    )
    # `repair` cannot touch it: it demands a `repair_pending` parent.
    with pytest.raises(TargetStackTransitionError):
        _apply(after_closing, "repair", profile)
    # `request_pop` is allowed to run -- the child is terminal -- but it must not
    # hand the finished root back as `active`. That was the second road into the
    # same hole, and the first version of this fix left it open.
    popped = _apply(after_closing, "request_pop", profile)
    assert [frame["status"] for frame in popped["target_stack"]] == ["completed"]
    assert stack_requires_human_disposition(popped) == "ROOT_TARGET_FRAME_COMPLETED"


@pytest.mark.security
def test_an_interrupted_goal_still_resumes_the_way_it_always_did() -> None:
    """The guard on `complete` must not disturb the interruption path.

    `push` is the one operation that leaves a suspended parent, and that parent
    still has to become `repair_pending` when its child finishes -- otherwise
    fixing the closing frame would break every side question.
    """

    profile = _real_profile()
    aggregate = {"generation": 2, "target_stack": [_frame("root", definition_id="welcome")]}
    pushed = apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "push", "target_frame_definition_id": "clarify_request"},
        interruption_relation="side_question",
        profile=profile,
        occurred_at=OCCURRED_AT,
    )
    assert [frame["status"] for frame in pushed["target_stack"]] == ["suspended", "active"]
    finished = _apply(pushed, "complete", profile)
    assert [frame["status"] for frame in finished["target_stack"]] == [
        "repair_pending",
        "completed",
    ]
    repaired = _apply(finished, "repair", profile)
    popped = _apply(repaired, "request_pop", profile)
    assert [frame["status"] for frame in popped["target_stack"]] == ["active"]


@pytest.mark.security
def test_slot_updates_are_filtered_against_the_frames_declared_slots() -> None:
    """P1-2. The write side had no equivalent of the plan side's SLOT_NOT_IN_FRAME.

    A model could put arbitrary keys into a persisted aggregate, 32 per turn,
    kept forever -- in a module whose first rule is that the platform decides
    what is legitimate and never takes the model's word for it.
    """

    profile = _profile("welcome")
    profile["target_frames"][0]["information_slots"] = [
        {
            "name": "request_summary",
            "type": "string",
            "required": True,
            "description": "What this slot records.",
        }
    ]
    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        recorded_at=OCCURRED_AT,
        slot_updates=[
            _update(frame_id, "request_summary", "kept"),
            _update(frame_id, "not_declared_anywhere", "dropped"),
        ],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 1
    assert updated["target_stack"][0]["slots"] == {"request_summary": "kept"}


@pytest.mark.security
def test_a_null_slot_value_does_not_erase_a_satisfied_slot() -> None:
    """P1-2, second half. "I found no value" is not "the value is gone".

    The exit gate counts `None` and `""` as absent, so honouring a null would let
    one understanding pass silently un-satisfy a goal that was already met -- and
    the contract cannot tell the two claims apart.
    """

    profile = _profile("welcome")
    profile["target_frames"][0]["information_slots"] = [
        {
            "name": "request_summary",
            "type": "string",
            "required": True,
            "description": "What this slot records.",
        }
    ]
    frame_id = str(uuid4())
    frame = _frame(frame_id)
    frame["slots"] = {"request_summary": "already satisfied"}
    aggregate = {"generation": 2, "target_stack": [frame]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", None)],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 0
    assert updated["target_stack"][0]["slots"] == {"request_summary": "already satisfied"}


@pytest.mark.security
def test_each_applied_slot_records_where_it_came_from() -> None:
    """T25. `confidence` and `evidence_event_id` used to be dropped on writeback.

    The consequence was not cosmetic: a 0.1-confidence slot and a 0.99 one were
    indistinguishable at the `complete` exit gate -- the one judgement of whether
    a goal was actually met -- and nothing could answer which message a slot came
    from.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", "reported")],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 1
    evidence = updated["target_stack"][0]["slot_evidence"]["request_summary"]
    assert evidence["confidence"] == 0.9
    assert evidence["evidence_event_ids"] == [EVIDENCE_EVENT_ID]
    assert evidence["recorded_at"] == OCCURRED_AT


@pytest.mark.security
def test_a_rejected_slot_leaves_no_evidence_behind() -> None:
    """Evidence follows the value. An update that was not applied has none.

    Otherwise `slot_evidence` would claim provenance for a slot that was never
    written -- worse than no provenance, because it reads as if it were.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[
            _update(frame_id, "not_declared_anywhere", "dropped"),
            _update(frame_id, "request_summary", None),
        ],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 0
    assert updated["target_stack"][0].get("slot_evidence", {}) == {}


def test_the_same_value_confirmed_twice_keeps_both_pieces_of_evidence() -> None:
    """**D3 的兑现物**：同一个值被第二条消息再次确认时，两条证据都留着。

    ## 它与上面那条 `@pytest.mark.security` 的用例是一对，不是对立

    那一条说的是：**值被改掉时**，旧消息支撑的是另一个值，
    留着就让出处指向一条「其值已被覆盖」的消息 —— 那是错的。
    本条说的是：**值没被改时**，第二条消息是对同一个值的**再次确认** ——
    而 RFC:165 要的数组正是这个。

    **第一版实现把两者混了**：无条件追加，于是上面那条当场红 ——
    **而它红得对**。判据因此是「值变没变」，不是「有没有已记录的证据」。
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    second_event = str(uuid4())
    again = _update(frame_id, "request_summary", "help with my order")
    again["evidence_event_id"] = second_event

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[
            _update(frame_id, "request_summary", "help with my order"),
            again,
        ],
    )

    evidence = writeback.aggregate["target_stack"][0]["slot_evidence"]["request_summary"]
    assert evidence["evidence_event_ids"] == [EVIDENCE_EVENT_ID, second_event], (
        "同一个值被两条消息确认，而证据里只剩一条 —— 那正是 D3 要修的"
    )


def test_evidence_read_from_a_row_written_before_the_array_existed() -> None:
    """**存量兼容**：D3 之前写下的行是单数键，而它们**不追溯改写**（T338 那一族）。

    新的写回读得懂旧形状，并从它继续追加 —— 否则升级那一刻，
    每个槽位的历史证据都会**静静地**消失一条。
    """

    frame_id = str(uuid4())
    frame = _frame(frame_id)
    legacy_event = str(uuid4())
    # **D3 之前的形状**：单数键、没有数组。
    frame["slots"] = {"request_summary": "help with my order"}
    frame["slot_evidence"] = {
        "request_summary": {
            "confidence": 0.9,
            "evidence_event_id": legacy_event,
            "recorded_at": OCCURRED_AT,
            "source": "model",
        }
    }
    aggregate = {"generation": 2, "target_stack": [frame]}

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", "help with my order")],
    )

    evidence = writeback.aggregate["target_stack"][0]["slot_evidence"]["request_summary"]
    assert evidence["evidence_event_ids"] == [legacy_event, EVIDENCE_EVENT_ID], (
        "旧的单数证据没被读进来 —— 升级那一刻每个槽位都会静静地丢一条历史证据"
    )


def test_the_evidence_list_stops_at_its_declared_bound() -> None:
    """**上界真的生效**，而且丢的是最老的那几条。

    `EVIDENCE_IDS_MAX` 与契约的 `maxItems` **写两遍**（`packages/contracts`
    不 import 生产代码），所以这一条同时钉住**两者相等** ——
    与 `MATERIAL_BYTES_BUDGET` 跨包那两份同一个守法。
    """

    from fastagent_v2.worker.target_stack import EVIDENCE_IDS_MAX

    contract = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "packages/contracts/schemas/conversation-aggregate.schema.json"
        ).read_text(encoding="utf-8")
    )
    declared = contract["properties"]["target_stack"]["items"]["properties"]["slot_evidence"][
        "additionalProperties"
    ]["properties"]["evidence_event_ids"]["maxItems"]
    assert declared == EVIDENCE_IDS_MAX, (
        f"契约写 {declared}、代码写 {EVIDENCE_IDS_MAX} —— 两份跨包的数必须相等"
    )

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    events = [str(uuid4()) for _ in range(EVIDENCE_IDS_MAX + 2)]
    updates = []
    for event in events:
        update = _update(frame_id, "request_summary", "help with my order")
        update["evidence_event_id"] = event
        updates.append(update)

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=updates,
    )

    kept = writeback.aggregate["target_stack"][0]["slot_evidence"]["request_summary"][
        "evidence_event_ids"
    ]
    assert kept == events[-EVIDENCE_IDS_MAX:], (
        "满了之后没有丢最老的那几条 —— 「最近是怎么被确认的」才是常被问到的那一半"
    )


@pytest.mark.security
def test_a_higher_or_equal_confidence_update_replaces_the_earlier_evidence() -> None:
    """The evidence describes the value that is there now, not a history of it.

    Keeping the first would make provenance point at a message whose value has
    since been overwritten -- an answer that is confidently wrong.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    second_event = str(uuid4())
    later = _update(frame_id, "request_summary", "corrected")
    later["evidence_event_id"] = second_event
    later["confidence"] = 0.95
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", "first"), later],
    )
    updated, applied = writeback.aggregate, writeback.applied
    assert applied == 2
    frame = updated["target_stack"][0]
    assert frame["slots"]["request_summary"] == "corrected"
    assert frame["slot_evidence"]["request_summary"]["evidence_event_ids"] == [second_event]
    assert frame["slot_evidence"]["request_summary"]["confidence"] == 0.95


@pytest.mark.security
def test_a_lower_confidence_update_does_not_overwrite_stronger_evidence() -> None:
    """RFC 4.3: "a low-confidence key slot must not automatically overwrite
    existing high-confidence evidence."

    This is the rule that made storing provenance necessary at all -- it is a
    comparison against what is already on the frame, and with nothing recorded
    every update won simply by arriving last. The first draft of this batch
    recorded the evidence but kept last-write-wins, and this test asserted that
    behaviour; the rule is in the RFC, so the behaviour was wrong and so was the
    test.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    strong = _update(frame_id, "request_summary", "customer said this plainly")
    strong["confidence"] = 0.95
    weak = _update(frame_id, "request_summary", "model guessed this")
    weak["confidence"] = 0.2
    weak["evidence_event_id"] = str(uuid4())

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[strong, weak],
    )
    updated, applied = writeback.aggregate, writeback.applied

    assert applied == 1
    frame = updated["target_stack"][0]
    assert frame["slots"]["request_summary"] == "customer said this plainly"
    assert frame["slot_evidence"]["request_summary"]["confidence"] == 0.95
    assert frame["slot_evidence"]["request_summary"]["evidence_event_ids"] == [EVIDENCE_EVENT_ID]
    # The refusal is reported, not merely subtracted from `applied`. A smaller
    # count cannot be told apart from the model finding nothing, and the update
    # being dropped here may be the customer correcting a value the model was
    # confidently wrong about.
    assert len(writeback.refusals) == 1
    refusal = writeback.refusals[0]
    assert refusal["slot"] == "request_summary"
    assert refusal["frame_id"] == frame_id
    assert refusal["proposed_confidence"] == 0.2
    assert refusal["recorded_confidence"] == 0.95
    assert refusal["proposed_evidence_event_id"] == weak["evidence_event_id"]
    assert refusal["recorded_evidence_event_id"] == EVIDENCE_EVENT_ID
    # ...and it carries no customer content: the trail says which slot and which
    # message, which is what an operator needs, without copying what was said.
    assert "model guessed this" not in str(writeback.refusals)
    assert not any("value" in key for key in refusal)


@pytest.mark.security
def test_a_slot_with_no_recorded_evidence_is_not_protected() -> None:
    """Refusing on the strength of evidence nobody recorded would invent a reason.

    Slots written before T25 carry no `slot_evidence`, so there is nothing to
    compare a new update against; it is applied and gains provenance.
    """

    frame_id = str(uuid4())
    frame = _frame(frame_id)
    frame["slots"] = {"request_summary": "written before provenance existed"}
    aggregate = {"generation": 2, "target_stack": [frame]}
    weak = _update(frame_id, "request_summary", "low confidence but comparable to nothing")
    weak["confidence"] = 0.1

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[weak],
    )
    updated, applied = writeback.aggregate, writeback.applied

    assert applied == 1
    assert updated["target_stack"][0]["slot_evidence"]["request_summary"]["confidence"] == 0.1


@pytest.mark.security
def test_an_applied_update_leaves_no_refusal_behind() -> None:
    """The trace must mean something when it is empty.

    A record written on every turn would be as unreadable as no record at all --
    `audit_events` gains a `conversation.slot_update_refused` row only when RFC
    4.3 actually dropped something.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[_update(frame_id, "request_summary", "the only update")],
    )

    assert writeback.applied == 1
    assert writeback.refusals == ()


@pytest.mark.security
def test_a_customer_changing_their_mind_is_not_refused_as_low_confidence() -> None:
    """P1-1. The reviewer's two-round probe, kept as a test.

    RFC 4.3 refuses a **low**-confidence update against **high**-confidence
    evidence. The first version of this batch implemented neither adjective, so
    0.85 lost to 0.9 -- and the cost is not one lost update: the next turn hands
    the model the stale `known_slots`, `_missing_required_slots` sees the slot
    filled, and `complete` passes the exit gate on a value the customer had
    already corrected. There is no second way in either; `PUSH_ONTO_SAME_FRAME`
    forbids a sub-frame of the same definition.
    """

    frame_id = str(uuid4())
    aggregate = {"generation": 2, "target_stack": [_frame(frame_id)]}
    first = _update(frame_id, "request_summary", "ring size 15")
    first["confidence"] = 0.9
    second = _update(frame_id, "request_summary", "ring size 16")
    second["confidence"] = 0.85
    second["evidence_event_id"] = str(uuid4())

    round_one = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[first],
    )
    round_two = apply_slot_updates(
        aggregate=round_one.aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[second],
    )

    assert round_two.applied == 1
    assert round_two.refusals == ()
    frame = round_two.aggregate["target_stack"][0]
    assert frame["slots"]["request_summary"] == "ring size 16"
    assert frame["slot_evidence"]["request_summary"]["confidence"] == 0.85


@pytest.mark.security
def test_an_update_the_understanding_step_marked_a_correction_is_never_refused() -> None:
    """A customer correcting themselves is not what RFC 4.3 is about.

    Even a hesitant correction outranks a confident earlier reading: the
    understanding step already classifies the turn as `correction`, and that
    classification was being discarded. Without this path a 0.2 correction
    against a 0.95 reading has nowhere to go.
    """

    frame_id = str(uuid4())
    frame = _frame(frame_id)
    frame["slots"] = {"request_summary": "confidently misread"}
    frame["slot_evidence"] = {
        "request_summary": {
            "confidence": 0.95,
            "evidence_event_id": EVIDENCE_EVENT_ID,
            "recorded_at": OCCURRED_AT,
        }
    }
    aggregate = {"generation": 2, "target_stack": [frame]}
    hesitant = _update(frame_id, "request_summary", "what the customer actually said")
    hesitant["confidence"] = 0.2
    hesitant["evidence_event_id"] = str(uuid4())

    refused = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[hesitant],
    )
    assert refused.applied == 0 and len(refused.refusals) == 1

    corrected = apply_slot_updates(
        aggregate=aggregate,
        profile=_slot_profile(),
        recorded_at=OCCURRED_AT,
        slot_updates=[hesitant],
        is_correction=True,
    )
    assert corrected.applied == 1
    assert corrected.refusals == ()
    assert (
        corrected.aggregate["target_stack"][0]["slots"]["request_summary"]
        == "what the customer actually said"
    )

