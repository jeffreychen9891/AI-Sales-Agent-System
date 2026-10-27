"""An undeclared `generation_constraints` name is refused, prompt or no prompt.

**This file must stay red when the prompt fix is removed.** That is its whole
purpose: the batch of 2026-09-10 changed *what the model is told*, and the
guarantee that nothing unconstrained reaches a customer belongs to
`inference_validation`, which that batch did not touch. If these cases only
passed because of the new prompt, the platform's safety would have moved from a
check into a suggestion.

The refusal these cases pin is the one that actually ran on 2026-09-10: six
plans out of seven events, `GENERATION_CONSTRAINT_NOT_DECLARED`, zero messages
sent. **Nothing unconstrained went to a real customer on WeChat**, and that is
the behaviour being protected -- not the prompt that now makes it rarer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected, plan_or_fallback, validate_plan

ROOT = Path(__file__).resolve().parents[2]

#: The two the model actually returned in the second real inbound window, and
#: the shape of the failure: near-misses of the declared name rather than
#: disagreements with it.
OBSERVED_INVENTIONS = (
    ["ask_one_question", "no_emoji_only_reply"],
    ["ask_for_request_summary", "no_assumptions_about_image"],
)

_EVIDENCE = {
    "provider": "test",
    "model": "test",
    "prompt_release": "test",
    "latency_ms": 0,
    "token_usage": {"input": 0, "output": 0},
}


def _profile() -> dict[str, Any]:
    return json.loads(
        (ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def _aggregate(profile: dict[str, Any]) -> dict[str, Any]:
    definition = profile["target_frames"][0]
    return {
        "generation": 1,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
                "slot_evidence": {},
                "entered_at": "2026-09-10T00:00:00Z",
                "last_progress_at": "2026-09-10T00:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": "optional",
                "frame_generation": 1,
            }
        ],
    }


def _understanding() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_id": str(uuid4()),
        "facts": [],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "用例夹具：这一轮的摘要。",
        "model_evidence": _EVIDENCE,
    }


def _plan(constraints: list[str], *, strategy: str = "ask_slot") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        "selected_strategy_id": strategy,
        "strategy_reason_code": "ASK_FOR_THE_MISSING_SLOT",
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": constraints,
        "manual_review_reason": None,
        # `validate_plan` checks the contract before any of its own rules, so a
        # document missing this is refused for the wrong reason and the case
        # would pass while proving nothing.
        "model_evidence": _EVIDENCE,
    }


@pytest.mark.parametrize("constraints", OBSERVED_INVENTIONS, ids=lambda value: value[0])
def test_the_names_the_model_invented_are_refused(constraints: list[str]) -> None:
    """Driven by what was actually returned, not by an invented counter-example."""

    profile = _profile()
    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan(constraints),
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )
    assert refused.value.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"


def test_a_near_miss_is_refused_exactly_like_a_wild_guess() -> None:
    """`ask_one_question` differs from `one_question` by a prefix.

    A validator that "helped" here -- prefix stripping, edit distance, any
    synonym map -- would make the profile's declaration decorative, which is the
    second direction ADR-0123 rejects by name.
    """

    profile = _profile()
    declared = profile["strategies"]["ask_slot"]["generation_constraints"]
    assert declared == ["one_question"], "precondition: this is what the profile declares"

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan(["ask_one_question"]),
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )
    assert refused.value.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"


def test_the_declared_name_passes() -> None:
    """The control. Without it the cases above would also pass if the validator
    refused everything, which is a different bug wearing the same colour."""

    profile = _profile()
    validated = validate_plan(
        plan=_plan(["one_question"]),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )
    assert validated.document["generation_constraints"] == ["one_question"]


def test_a_refused_plan_falls_back_to_no_message_and_sends_nothing() -> None:
    """**The second half of the guarantee**, and the half that matters to a customer.

    Refusing is not enough on its own: what happened on 2026-09-10 is that the
    turn fell back to a *silent* strategy, so the customer received nothing
    rather than receiving something unconstrained. `plan_or_fallback` is where
    that is decided.
    """

    profile = _profile()
    plan, rejection = plan_or_fallback(
        plan=_plan(OBSERVED_INVENTIONS[0]),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )
    assert rejection is not None
    assert rejection.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"
    assert plan.document["selected_strategy_id"] == "no_message"
    assert plan.document["origin"] == "safe_fallback"
    assert plan.is_silent, "a fallback that could speak would defeat the refusal"
    assert plan.document["candidate_intents"] == []


# --------------------------------------------------------------------------
# schema 里可选的键缺席时，闸要拒绝它，而不是自己抛（2026-09-13）
# --------------------------------------------------------------------------
#
# `merchant-profile.schema.json` 给 `strategies` 补形状时**没有加 `required`**
# （ADR-0137 裁定一步骤 1），所以三个键都是可选的 —— 一份只写了
# `allowed_intent_kinds` 的 profile 完全合法、封得版。而校验器与提示词组装
# 一直用下标：**schema 允许缺，代码假设有。**
#
# 三份现存配包都写全了，所以这个洞今天碰不到。而裁定一的两道新闸要读这几个键，
# 于是缺席必须落在「闸拒绝这份计划」那条路上：
# **前者留下一行带理由码的 `policy_decisions`，后者是一条 500 和一份堆栈。**


def test_a_strategy_without_allowed_intent_kinds_refuses_instead_of_raising() -> None:
    """缺 `allowed_intent_kinds` = 这个策略不允许任何意图 —— **拒，而不是抛**。

    缺席的语义是一句真话：没声明允许哪些意图，就是一个都不允许，
    于是任何 candidate 都该被拒，而理由码正好说的就是这件事。
    """

    profile = _profile()
    del profile["strategies"]["ask_slot"]["allowed_intent_kinds"]
    plan = _plan([], strategy="ask_slot")
    plan["candidate_intents"] = [
        {
            "kind": "send_message",
            "typed_payload": {"message_type": "acknowledgement"},
            # `additionalProperties: false` —— 契约只认这三个键，
            # 多给一个 `evidence_refs` 会让它在合同层被拒，
            # 而那不是本条要测的那道闸（第一版就是这么错的）。
            "idempotency_basis": "b1",
        }
    ]

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=plan,
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )

    assert refused.value.reason_code == "INTENT_KIND_NOT_ALLOWED_BY_STRATEGY"


def test_a_strategy_without_generation_constraints_refuses_instead_of_raising() -> None:
    """缺 `generation_constraints` = 它不声明任何约束 —— 计划里带一个就该被拒。

    与上一条同源：**`KeyError` 与 `PlanRejected` 在运行期的区别不是风格问题** ——
    一条是「这份计划不合规，已拒，理由是 X」，另一条是「引擎炸了」。
    """

    profile = _profile()
    declared = profile["strategies"]["ask_slot"]["generation_constraints"]
    assert declared, "这条用例要求这个策略本来声明了约束，好让删除是有意义的"
    del profile["strategies"]["ask_slot"]["generation_constraints"]

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan([declared[0]], strategy="ask_slot"),
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )

    assert refused.value.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"


def test_a_strategy_without_either_key_still_lets_a_bare_plan_through() -> None:
    """**两个键都缺、而计划什么都不要求时，照样放行。**

    这一条是上面两条的反面，防的是「把缺席当成错误」——
    一个只写了名字的策略是合法声明，它的意思是「这条打法不许带意图、不带约束」，
    而一份既不带意图也不带约束的计划**没有违反其中任何一条**。

    少了这一条，上面两条会被「缺键就一律拒」满足，而那是另一种行为。
    """

    profile = _profile()
    del profile["strategies"]["ask_slot"]["allowed_intent_kinds"]
    del profile["strategies"]["ask_slot"]["generation_constraints"]

    validated = validate_plan(
        plan=_plan([], strategy="ask_slot"),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert validated.strategy_id == "ask_slot"
