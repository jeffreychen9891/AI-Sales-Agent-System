"""Can the platform actually perform the transitions this corpus records?

The suite that shipped with the carrier checked that the corpus was *well
formed* -- schema-valid, self-consistent, hash-pinned. It never asked whether
the sequence it describes is one `validate_plan` would accept, and the
independent review found two defects that made the headline
`push -> complete -> repair -> request_pop` path **unexecutable**:

* a required slot recorded as `null`, which `_missing_required_slots` counts as
  absent, so `complete` could never fire;
* `confirm_resume` missing from the *child* frame's allowlist, while the test
  asserted it on the *parent* -- and `validate_plan` resolves the allowlist from
  the stack top, which during a side question is the child.

Both were invisible to a green suite because nothing executed the corpus. This
file executes it: every event's operation is driven through the real validator
against the real profile, with the aggregate carried forward between events.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.worker.inference_validation import PlanRejected, validate_plan
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.platform_frames import frame_definitions
from fastagent_v2.worker.target_stack import apply_target_stack_operation

ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = ROOT / "regression" / "cases" / "synthetic-carrier-v1-2026-09-03.json"
OCCURRED_AT = "2026-09-03T09:00:00Z"

#: One legal strategy per operation. The corpus records operations, not
#: strategies, so the test has to choose -- and the choice is not free:
#: `repair` is the state half of `confirm_resume` (RFC §4.2 `:311`), and popping
#: waits. Picking a strategy the frame forbids would make this test fail for a
#: reason that has nothing to do with the corpus.
#: 每种栈操作**首选**哪条打法。挑不到就从帧的 allowlist 里退一档，见 `_strategy_for`。
#:
#: **`complete` 原来映的是 `advance_goal`，那是错的**（闸一落地当天红的就是这一行）：
#: ADR-0137 裁定一把 `complete` 明确列在「换帧的每一种写法」里，
#: 而 `advance_goal` 只许在当前帧内推进 ——
#: **这张表当时断言的正是图纸禁止的那件事**，只是在闸建起来之前没人会红。
STRATEGY_FOR = {
    "keep": "acknowledge",
    "push": "ask_slot",
    "complete": "close_goal",
    "repair": "confirm_resume",
    "request_pop": "wait",
}

#: 退档时按这个顺序挑，第一个被帧允许的就用它。
#: 都是**不换帧也说得通**的打法，所以任何一个被挑中都不会与闸一冲突。
STRATEGY_FALLBACK_ORDER = ("close_goal", "acknowledge", "answer_question", "wait")


def _strategy_for(definition: dict[str, Any], operation: str) -> str:
    """这一帧上跑这个操作，用哪条打法。

    **从帧的 `allowed_strategy_set` 里挑，而不是认一张全局表** ——
    一张全局表隐含「每个帧都允许这条打法」，而那不成立：实测
    `synthetic-two-axis-desk` 的 `consultation` 帧**不允许 `close_goal`**，
    尽管它声明了 exit condition。表写死的话，语料里那次 `complete` 就永远挑不到
    一条合法打法，而红出来的理由码会是
    `STRATEGY_NOT_IN_FRAME_ALLOWLIST` —— **指向 allowlist，而真正的问题在这张表**。

    脚手架挑的这条打法**不是语料在断言的东西**：本用例断言的是
    「语料记的那个栈转换，平台的校验器接不接受」，
    而 strategy 只是让计划文档合法所需的一格（理由码写着 `CORPUS_REPLAY`）。
    所以按帧挑是对的：它让脚手架跟着 profile 走，而不是替 profile 做声明。
    """

    allowed = tuple(definition["allowed_strategy_set"])
    preferred = STRATEGY_FOR[operation]
    if preferred in allowed:
        return preferred
    for candidate in STRATEGY_FALLBACK_ORDER:
        if candidate in allowed:
            return candidate
    raise AssertionError(
        f"frame {definition['id']!r} allows none of {STRATEGY_FALLBACK_ORDER} -- "
        f"语料里这一步没有合法打法可挑，先看这个帧的 allowed_strategy_set"
    )

#: `push` is only legal on an interruption the validator recognises as one.
RELATION_FOR = {"push": "side_question"}


@pytest.fixture(scope="module")
def profile() -> dict[str, Any]:
    return (
        ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
        .package(profile_id="synthetic-two-axis-desk", version="1.0.3")
        .document
    )


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def _understanding(relation: str) -> dict[str, Any]:
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
        "model_evidence": {
            "provider": "test",
            "model": "test",
            "prompt_release": "test",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def _plan(
    profile: dict[str, Any],
    operation: str,
    target: str | None,
    definition: dict[str, Any],
) -> dict[str, Any]:
    strategy = _strategy_for(definition, operation)
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {
            "op": operation,
            "target_frame_definition_id": target,
        },
        "selected_strategy_id": strategy,
        "strategy_reason_code": "CORPUS_REPLAY",
        "confidence": {
            "raw_score": 1.0,
            "calibrated_score": None,
            "assessment_version": "test",
            "factor_codes": ["deterministic_output"],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": list(
            profile["strategies"][strategy]["generation_constraints"]
        ),
        "manual_review_reason": None,
        "model_evidence": {
            "provider": "test",
            "model": "test",
            "prompt_release": "test",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def _apply_recorded_slots(aggregate: dict[str, Any], event: dict[str, Any]) -> None:
    """Write the slot values the corpus says this event records.

    Without this the frames never fill, and `complete` fails for a reason the
    corpus did not describe. It is also what makes the `null` defect visible:
    a recorded `null` lands as an absent slot.
    """

    frame = aggregate["target_stack"][-1]
    for slot in event["expected"]["slots"]:
        if slot["operation"] == "set":
            frame["slots"][slot["name"]] = slot["value"]
        elif slot["operation"] == "clear":
            frame["slots"].pop(slot["name"], None)


def test_every_recorded_transition_is_one_the_validator_accepts(
    profile: dict[str, Any], corpus: dict[str, Any]
) -> None:
    """The whole corpus, event by event, through the real validator.

    The aggregate is carried forward: each event sees the stack the previous one
    left behind, which is the only way `push` -> `complete` -> `repair` ->
    `request_pop` can be checked as a *sequence* rather than as four unrelated
    states someone wrote by hand.
    """

    case = corpus["cases"][0]
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    assert aggregate["target_stack"][0]["definition_id"] == "rental"

    for index, event in enumerate(case["events"]):
        expected = event["expected"]["goal_frame"]
        operation = expected["operation"]
        relation = RELATION_FOR.get(operation, "same_goal")

        _apply_recorded_slots(aggregate, event)

        assert aggregate["target_stack"][-1]["definition_id"] == (
            expected["before_definition_id"]
        ), f"event {index}: corpus says the turn starts on a frame the run is not on"

        # 当前帧的定义 —— 挑打法要按**这个帧**允许什么来挑（见 `_strategy_for`）。
        current = frame_definitions(profile)[
            str(aggregate["target_stack"][-1]["definition_id"])
        ]
        plan = _plan(profile, operation, expected["target_definition_id"], current)
        try:
            validate_plan(
                plan=plan,
                understanding=_understanding(relation),
                aggregate=json.loads(json.dumps(aggregate)),
                profile=profile,
            )
        except PlanRejected as rejected:
            pytest.fail(
                f"event {index} ({operation}): the platform refuses the transition the "
                f"corpus records -- {rejected.reason_code}: {rejected}"
            )

        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=plan["target_stack_operation"],
            interruption_relation=relation,
            profile=profile,
            occurred_at=OCCURRED_AT,
        )

        stack = [frame["definition_id"] for frame in aggregate["target_stack"]]
        assert stack == expected["after_stack_definition_ids"], (
            f"event {index} ({operation}): stack is {stack}, corpus says "
            f"{expected['after_stack_definition_ids']}"
        )
        assert aggregate["target_stack"][-1]["definition_id"] == (
            expected["after_definition_id"]
        )


def test_no_recorded_slot_is_set_to_nothing(corpus: dict[str, Any]) -> None:
    """`set` with a `null` value is the defect that made `complete` impossible.

    `_missing_required_slots` counts `None` as absent, so a required slot
    "recorded" as null leaves its frame permanently unable to satisfy its own
    exit condition. The retired corpus has 39 `set` operations and no nulls;
    this pattern arrived with the carrier.

    `not_set` with a null value is a different statement -- "this stays empty" --
    and stays legal.
    """

    offenders = [
        (index, slot["name"])
        for case in corpus["cases"]
        for index, event in enumerate(case["events"])
        for slot in event["expected"]["slots"]
        if slot["operation"] == "set" and slot["value"] is None
    ]
    assert not offenders, f"set-to-null slots: {offenders}"


def test_the_amount_a_quote_records_is_a_provenance_handle(
    corpus: dict[str, Any],
) -> None:
    """G1's slot has to be able to tell a correct figure from an invented one.

    Recording "the amount was set to nothing in particular" cannot do that. The
    corpus records a reference into the pricing policy instead, so the assertion
    has something to be wrong about.
    """

    events = corpus["cases"][0]["events"]
    quote = next(event for event in events if "G1" in event["expected"]["basis"])
    deposit = next(
        slot for slot in quote["expected"]["slots"] if slot["name"] == "deposit_amount"
    )
    assert deposit["operation"] == "set"
    assert isinstance(deposit["value"], str)
    assert deposit["value"].startswith("policy:carrier-fixed-pricing:v1#")
