"""A timer turn plays the strategy its rule declared. **B4-4 / ADR-0105 §零.1 / ADR-0116.**

「打法固定在模板里，不让模型和商家自由发挥」 is the商业 logic the whole profile
mechanism exists for, and until this batch the one field carrying it --
`time_rules[].strategy` -- was read by nothing. Enforcing it is only half: the
planner is *told* the strategy as well, because a rule enforced without being
communicated sends every timer turn to the safe fallback, and 「打法固定在模板里」
would be implemented as "the platform says nothing on timers".
"""

from __future__ import annotations

from typing import Any

import pytest
from fastagent_v2.worker.inference import DeterministicMockInference, safe_fallback_plan
from fastagent_v2.worker.inference_validation import PlanRejected, plan_or_fallback, validate_plan

FRAME_ID = "welcome"
FOLLOWUP = "propose_followup"


def _profile() -> dict[str, Any]:
    """A frame whose time rules may use a play its ordinary turns may not.

    The shape `synthetic-third`'s `clarify_request` now has: `propose_followup`
    is registered and reachable by a rule, and deliberately **absent** from
    `allowed_strategy_set`.
    """

    return {
        "identity": {"version": "1.0.0"},
        "target_frames": [
            {
                "id": FRAME_ID,
                "goal": "greet",
                "entry_conditions": [],
                "exit_conditions": [],
                "information_slots": [],
                "allowed_strategy_set": ["acknowledge", "ask_slot"],
                "time_rule_strategy_set": [FOLLOWUP],
                "content_materials": [],
                "time_rules": [],
                "return_confirmation": {"policy": "optional", "tone_material_ref": None},
            }
        ],
        "strategies": {
            "acknowledge": {
                "required_slots": [],
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            },
            "ask_slot": {
                "required_slots": [],
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            },
            FOLLOWUP: {
                "required_slots": [],
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            },
        },
    }


def _aggregate() -> dict[str, Any]:
    return {
        "generation": 1,
        "state_version": 1,
        "control_mode": "ai",
        "target_stack": [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "definition_id": FRAME_ID,
                "goal": "greet",
                "status": "active",
                "slots": {},
                "entered_at": "2026-09-09T00:00:00Z",
                "last_progress_at": "2026-09-09T00:00:00Z",
                "parent_frame_id": None,
                "frame_generation": 0,
                "resume_confirmation_policy": "optional",
            }
        ],
    }


def _understanding() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_id": "33333333-3333-4333-8333-333333333333",
        "facts": [],
        "intents": [{"intent_label": "unclassified", "confidence": 1.0}],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "用例夹具：这一轮的摘要。",
        "model_evidence": {
            "provider": "mock",
            "model": "m",
            "prompt_release": "p",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def _plan(strategy: str) -> dict[str, Any]:
    return DeterministicMockInference().plan(
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
        required_strategy=strategy,
    )


def test_the_planner_is_told_the_rule_s_strategy_and_the_validator_holds_it_there() -> None:
    """Both halves in one case, because either alone is broken.

    Told-only would be a suggestion the model may ignore; enforced-only would
    reject every real plan. The mock is not a behaviour model, but it has to
    obey the same constraint the model-backed planner is given or the suite
    would exercise the fallback path instead of the one production takes.
    """

    plan = _plan(FOLLOWUP)
    assert plan["selected_strategy_id"] == FOLLOWUP
    validated = validate_plan(
        plan=plan,
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
        required_strategy=FOLLOWUP,
    )
    assert validated.strategy_id == FOLLOWUP


def test_a_timer_plan_that_names_another_strategy_is_refused() -> None:
    """The rule wins over the model. This is the enforcement half.

    Without it, `strategy` would be advisory -- and advisory is what
    ADR-0105 §零.1 refused: 「删掉等于把这个决定交回给模型」, and so does letting
    the model overrule it.
    """

    wandering = _plan(FOLLOWUP)
    wandering["selected_strategy_id"] = "acknowledge"
    with pytest.raises(PlanRejected) as raised:
        validate_plan(
            plan=wandering,
            understanding=_understanding(),
            aggregate=_aggregate(),
            profile=_profile(),
            required_strategy=FOLLOWUP,
        )
    assert raised.value.reason_code == "STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED"

    # And the turn still ends legally: the safe fallback replaces it rather than
    # the exception reaching the retry budget.
    plan, rejection = plan_or_fallback(
        plan=wandering,
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
        required_strategy=FOLLOWUP,
        # **这是一个定时回合，所以要照定时回合的样子调**（ADR-0138 加了第四个条件：
        # 定时回合不发澄清）。先前这里不传也对，因为那时回退只在
        # `ask_slot` / `no_message` 之间选；现在多了一支，而它按 `event_type` 判。
        event_type="timer",
    )
    assert rejection is not None
    assert plan.document["origin"] == "safe_fallback"


def test_the_two_strategy_lists_are_read_at_different_moments() -> None:
    """ADR-0116's fork, from both sides.

    `propose_followup` is in `time_rule_strategy_set` and **not** in
    `allowed_strategy_set`, so it is legal for the rule and illegal for the
    model's own choice. Before the fork one list answered both questions, and
    making the rule usable meant offering a follow-up play to the model
    mid-conversation.
    """

    ordinary = _plan(FOLLOWUP)
    ordinary["selected_strategy_id"] = FOLLOWUP
    with pytest.raises(PlanRejected) as raised:
        validate_plan(
            plan=ordinary,
            understanding=_understanding(),
            aggregate=_aggregate(),
            profile=_profile(),
            # No rule fired: this is an ordinary customer-message turn.
            required_strategy=None,
        )
    assert raised.value.reason_code == "STRATEGY_NOT_IN_FRAME_ALLOWLIST"


def test_a_safe_fallback_is_not_bound_by_the_rule_s_strategy() -> None:
    """The valve the rejection above falls through to must stay legal.

    Binding `origin="safe_fallback"` to the rule's strategy would leave a
    rejected timer plan with no legal replacement at all -- the platform's own
    deterministic answer refused for not being the merchant's play.
    """

    fallback = safe_fallback_plan(
        reason_code="STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED",
        strategy_id="no_message",
        aggregate=_aggregate(),
    )
    validated = validate_plan(
        plan=fallback,
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
        required_strategy=FOLLOWUP,
    )
    assert validated.strategy_id == "no_message"
