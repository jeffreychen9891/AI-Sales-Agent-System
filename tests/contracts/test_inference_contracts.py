"""Contract tests for the three-stage inference boundary.

Every test here answers one question: can a model result reach the outbox
without the platform having proved it legal? The answer must stay no.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.contracts import (
    CONTRACT_NAMES,
    INFERENCE_CONTRACT_NAMES,
    load_inference_schema,
    validate_inference_contract,
)
from fastagent_v2.worker.inference import DeterministicMockInference, safe_fallback_plan
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    PlanRejected,
    plan_or_fallback,
    validate_generation,
    validate_plan,
)
from fastagent_v2.worker.strategies import STRATEGY_CATALOG
from jsonschema import ValidationError

ROOT = Path(__file__).resolve().parents[2]
TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PROFILE_RELEASE = UUID("11111111-1111-4111-8111-000100000004")


def profile() -> dict[str, Any]:
    return json.loads(
        (ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def aggregate(*, definition_id: str = "welcome", status: str = "active") -> dict[str, Any]:
    definition = next(
        frame for frame in profile()["target_frames"] if frame["id"] == definition_id
    )
    return {
        "id": str(uuid4()),
        "tenant_id": str(TENANT),
        "state_version": 1,
        "generation": 0,
        "profile_release_id": str(PROFILE_RELEASE),
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition_id,
                "goal": definition["goal"],
                "status": status,
                "slots": {},
                "entered_at": "2026-09-01T00:00:00Z",
                "last_progress_at": "2026-09-01T00:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": definition["return_confirmation"]["policy"],
                "frame_generation": 0,
            }
        ],
    }


def envelope(event_type: str = "customer_message") -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "content": {"type": "text", "text": "hello"},
    }


def steps() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    inference = DeterministicMockInference()
    state = aggregate()
    understanding = inference.understand(
            envelope=envelope(), aggregate=state, profile=profile()
        )
    plan = inference.plan(understanding=understanding, aggregate=state, profile=profile())
    generation = inference.generate(
        plan=plan,
        understanding=understanding,
        envelope=envelope(),
        # The double ignores it; the shape is what the port asks for.
        materials={},
    )
    return understanding, plan, generation


def test_every_inference_schema_is_registered() -> None:
    on_disk = {
        path.name.removesuffix(".schema.json")
        for path in (ROOT / "packages" / "contracts" / "inference").glob("*.schema.json")
    }
    assert on_disk == set(INFERENCE_CONTRACT_NAMES)
    for name in INFERENCE_CONTRACT_NAMES:
        assert load_inference_schema(name)["$id"].endswith(f"{name}.schema.json")


def test_the_narrow_waist_contracts_are_untouched_by_this_family() -> None:
    """推理那一族加东西，**不许顺手加进窄腰**。

    这一条原本是 `len(schemas) == 7`。2026-09-16（批 11a）窄腰真的多了一个
    （`commitment-record`，与 `intent-record` 同类），于是它红了 ——
    **红得对，而它说的话不够**：「expected 7, found 8」不告诉读的人是哪一个。

    改成按名字比，理由与 `check_static.NARROW_WAIST` 那一段相同：
    **一个数只要改一位就还是绿的，一个名字要有人写下来。**
    而本条的题目仍然是它原来那个 —— 推理那一族的改动不该动到窄腰。
    """

    on_disk = {
        path.name.removesuffix(".schema.json")
        for path in (ROOT / "packages" / "contracts" / "schemas").glob("*.schema.json")
    }
    registered = set(CONTRACT_NAMES)
    assert on_disk == registered, (
        f"窄腰那一族与 `CONTRACT_NAMES` 对不上："
        f"只在磁盘上 {sorted(on_disk - registered)}，"
        f"只在登记里 {sorted(registered - on_disk)}"
    )


def test_mock_emits_documents_that_satisfy_their_schemas() -> None:
    understanding, plan, generation = steps()
    validate_inference_contract("understanding-result", understanding)
    validate_inference_contract("plan-result", plan)
    validate_inference_contract("generation-result", generation)


def test_understanding_carries_no_customer_text() -> None:
    understanding, _, _ = steps()
    serialised = json.dumps(understanding, ensure_ascii=False)
    assert "hello" not in serialised
    # 2026-09-24（缺陷 22）：`evidence_span_hash` 删掉了 —— 它是这条用例
    # 原来核「原文没被以指纹之外的形式带出去」的抓手。契约收缩后
    # `additionalProperties: false` 保证它回不来；上面那句 "hello" not in
    # 已经覆盖「客户原文不外带」这半，无需替代断言。
    assert "evidence_span_hash" not in understanding["facts"][0]


def test_understanding_may_not_select_a_strategy() -> None:
    understanding, _, _ = steps()
    understanding["selected_strategy_id"] = "acknowledge"
    with pytest.raises(ValidationError):
        validate_inference_contract("understanding-result", understanding)


def test_valid_plan_passes_validation() -> None:
    understanding, plan, _ = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    assert validated.strategy_id in STRATEGY_CATALOG
    assert validated.origin == "model"


def test_plan_outside_the_frame_allowlist_is_rejected() -> None:
    understanding, plan, _ = steps()
    plan["selected_strategy_id"] = "answer_question"
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "STRATEGY_NOT_IN_FRAME_ALLOWLIST"


def test_plan_inventing_a_strategy_is_rejected() -> None:
    understanding, plan, _ = steps()
    plan["selected_strategy_id"] = "upsell_aggressively"
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "STRATEGY_NOT_IN_CATALOG"


def test_plan_with_an_intent_the_strategy_does_not_allow_is_rejected() -> None:
    understanding, plan, _ = steps()
    plan["candidate_intents"] = [
        {"kind": "create_order", "typed_payload": {}, "idempotency_basis": "x"}
    ]
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "INTENT_KIND_NOT_ALLOWED_BY_STRATEGY"


def test_plan_scheduling_an_event_the_frame_does_not_declare_is_rejected() -> None:
    understanding, plan, _ = steps()
    state = aggregate()
    plan["scheduled_event_changes"]["create"] = [
        {
            "kind": "dormant_reengagement",
            "due_at": "2026-09-02T00:00:00Z",
            "semantic_key": "k",
            "target_frame_id": state["target_stack"][0]["id"],
        }
    ]
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(plan=plan, understanding=understanding, aggregate=state, profile=profile())
    assert rejection.value.reason_code == "EVENT_KIND_NOT_ALLOWED_BY_FRAME"


def test_plan_may_not_complete_a_frame_whose_required_slot_is_empty() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "complete", "target_frame_definition_id": None}
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "EXIT_CONDITION_NOT_SATISFIED"


def test_plan_may_complete_a_frame_once_the_required_slot_is_filled() -> None:
    understanding, plan, _ = steps()
    state = aggregate()
    state["target_stack"][0]["slots"] = {"request_summary": "a refund question"}
    plan["target_stack_operation"] = {"op": "complete", "target_frame_definition_id": None}
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=state, profile=profile()
    )
    assert validated.document["target_stack_operation"]["op"] == "complete"


def test_push_requires_an_interruption() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {
        "op": "push",
        "target_frame_definition_id": "clarify_request",
    }
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "PUSH_WITHOUT_INTERRUPTION"


def test_push_is_allowed_on_a_side_question() -> None:
    understanding, plan, _ = steps()
    understanding["interruption"]["relation"] = "side_question"
    plan["target_stack_operation"] = {
        "op": "push",
        "target_frame_definition_id": "clarify_request",
    }
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    assert validated.document["target_stack_operation"]["op"] == "push"


def test_pop_without_a_parent_frame_is_rejected() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "request_pop", "target_frame_definition_id": None}
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "STACK_OP_REQUIRES_PARENT"


def _two_frame_stack(*, child_status: str, parent_status: str, policy: str) -> dict:
    state = aggregate()
    root = state["target_stack"][0]
    root["status"] = parent_status
    child = dict(root)
    child["id"] = "11111111-1111-4111-8111-1111111111ff"
    child["definition_id"] = "clarify_request"
    child["status"] = child_status
    child["parent_frame_id"] = root["id"]
    child["resume_confirmation_policy"] = policy
    state["target_stack"] = [root, child]
    return state


def test_repair_on_a_parent_that_is_not_awaiting_repair_is_rejected() -> None:
    """Fail closed at plan time rather than in the transition.

    A `TargetStackTransitionError` escapes into the retry budget and costs three
    model calls per replay; a `PlanRejected` lands on the no-send path with a
    reason code. Same refusal, cheaper and traceable.
    """

    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "repair", "target_frame_definition_id": None}
    plan["selected_strategy_id"] = "confirm_resume"
    plan["candidate_intents"] = []
    plan["generation_constraints"] = []
    state = _two_frame_stack(child_status="active", parent_status="suspended", policy="optional")
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(plan=plan, understanding=understanding, aggregate=state, profile=profile())
    assert rejection.value.reason_code == "REPAIR_ON_UNPREPARED_PARENT"


def test_pop_before_the_required_confirmation_is_rejected() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "request_pop", "target_frame_definition_id": None}
    plan["selected_strategy_id"] = "wait"
    plan["candidate_intents"] = []
    plan["generation_constraints"] = []
    state = _two_frame_stack(
        child_status="completed", parent_status="repair_pending", policy="required"
    )
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(plan=plan, understanding=understanding, aggregate=state, profile=profile())
    assert rejection.value.reason_code == "POP_BEFORE_RESUME_CONFIRMATION"


def test_pop_after_the_confirmation_state_is_allowed() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "request_pop", "target_frame_definition_id": None}
    plan["selected_strategy_id"] = "wait"
    plan["candidate_intents"] = []
    plan["generation_constraints"] = []
    state = _two_frame_stack(
        child_status="completed",
        parent_status="resume_confirmation_pending",
        policy="required",
    )
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=state, profile=profile()
    )
    assert validated.document["target_stack_operation"]["op"] == "request_pop"


def test_abandon_requires_an_exit_signal() -> None:
    understanding, plan, _ = steps()
    plan["target_stack_operation"] = {"op": "abandon", "target_frame_definition_id": None}
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
        )
    assert rejection.value.reason_code == "ABANDON_WITHOUT_EXIT_SIGNAL"


def test_a_rejected_plan_becomes_a_deterministic_fallback() -> None:
    """被拒的计划变成平台自己的回退计划。

    ## 2026-09-15（D4，ADR-0138）：这一条的期待从 `no_message` 改成 `ask_clarification`

    **旧的期待不是错的，它是旧裁定的编码** —— T339 第二批（用户裁 (c)）定的是二分
    「点得出槽位 -> `ask_slot`；点不出 -> 静默」，
    **而静默是当时唯一诚实的选项**：词表里没有「请客户澄清」这个打法，
    于是「点不出」只能在「发一句说不出内容的话」与「不说」之间选，
    T339 选了后者，**那是对的**。

    ADR-0138 给了第三个选项。这个夹具的 `understanding` **有内容**
    （`steps()` 造的是一份正常的理解结果），
    所以它落在新的第二支：模型听懂了东西、只是拼不出合法计划 ——
    **那一轮说一句「我确认一下」比沉默好**，而那句话由生成步照 `understanding` 写。

    **旧断言留在这里不是历史癖**：下一个人看到 `ask_clarification` 时会问
    「静默那条规矩呢」，答案是**它还在，只是缩小到了它真正成立的范围**
    （`understanding` 也空、或不是客户回合）—— 那两支由本文件下面两条钉住。

    ## 2026-09-16：`event_type` 从「不传」改成**明写**

    这一条先前不传 `event_type`，靠它默认的 `""` 落进「不是 timer」那一支。
    **那正是被修掉的那个混淆**：`!= "timer"` 把「不是定时回合」读成了
    「是客户回合」，而 `event_type` 有六个值。判据改成正向之后，
    **一个没说自己是什么的回合落到 `no_message`** —— 方向对，
    而这条用例要测的是客户回合，所以它现在把话说出来。
    """

    understanding, plan, _ = steps()
    plan["selected_strategy_id"] = "propose_order"
    validated, rejection = plan_or_fallback(
        plan=plan,
        understanding=understanding,
        aggregate=aggregate(),
        profile=profile(),
        event_type="customer_message",
    )
    assert rejection is not None
    assert rejection.reason_code == "STRATEGY_NOT_IN_FRAME_ALLOWLIST"
    assert validated.origin == "safe_fallback"
    assert validated.strategy_id == "ask_clarification"
    assert validated.document["confidence"]["factor_codes"] == ["fallback_applied"]


def test_a_rejection_with_nothing_understood_is_still_silent() -> None:
    """**T339 那条规矩还在，只是缩小了范围**：理解步也空着时，仍然沉默。

    这是 ADR-0138 三分的第三支。没有它，「点不出槽位就说一句」会退化成
    **一句没有内容的套话** —— 正是 T339 判定的那个坏形状本身。
    """

    understanding, plan, _ = steps()
    for key in ("facts", "intents", "slot_updates"):
        understanding[key] = []
    plan["selected_strategy_id"] = "propose_order"
    validated, rejection = plan_or_fallback(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    assert rejection is not None
    assert validated.strategy_id == "no_message", (
        "理解步什么都没提取出来时那句澄清没有可确认的对象 —— 只能是套话"
    )
    assert validated.document["candidate_intents"] == []


def test_a_timer_turn_does_not_ask_the_customer_to_clarify() -> None:
    """**第四个条件**：定时回合不澄清 —— 客户刚才什么都没说。

    那一轮是平台自己发起的（静默检查之类）。在那里发一句「您刚才是说…？」
    **不是澄清，是无来由的主动消息。**

    这一条是接线时被 `test_a_timer_plan_that_names_another_strategy_is_refused`
    抓出来的：它当时红在 `KeyError: 'id'`（新路径开始产出候选意图了），
    **而红的表象是夹具缺一个键，真因是「定时回合不该说这句话」。**

    ## 两种 `required_strategy` 都跑，因为判据不是它

    第一版的判据取的是 `required_strategy is None`，**而那是错的**：
    `pipeline._required_strategy` 在**两种**情况下返回 `None` ——
    客户消息，**以及「定时器挂着时 release 被取代、那条规则不再声明打法」**。
    第二种仍然是定时回合，而第一版会在那里把澄清发出去。

    所以这一条**两种取值都跑**：判据是 `event_type`，
    **`required_strategy` 取什么都不该改变结论。**
    """

    for required in (None, "propose_followup"):
        understanding, plan, _ = steps()
        plan["selected_strategy_id"] = "propose_order"
        validated, rejection = plan_or_fallback(
            plan=plan,
            understanding=understanding,
            aggregate=aggregate(),
            profile=profile(),
            required_strategy=required,
            event_type="timer",
        )
        assert rejection is not None
        assert validated.strategy_id == "no_message", (
            f"定时回合上发出了一句澄清（required_strategy={required!r}）—— "
            "那一轮客户没说话，没有可澄清的对象"
        )


def test_a_fallback_may_not_choose_an_arbitrary_strategy() -> None:
    state = aggregate()
    document = safe_fallback_plan(
        reason_code="TEST_REASON", strategy_id="propose_quote", aggregate=state
    )
    understanding, _, _ = steps()
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=document, understanding=understanding, aggregate=state, profile=profile()
        )
    assert rejection.value.reason_code == "FALLBACK_STRATEGY_NOT_ALLOWED"


def test_a_silent_strategy_may_not_propose_a_send() -> None:
    # A profile that wrongly grants `wait` the right to send is still refused:
    # silence is a platform property of the strategy, not a profile setting.
    permissive = profile()
    permissive["strategies"]["wait"]["allowed_intent_kinds"] = ["send_message"]
    understanding, plan, _ = steps()
    plan["selected_strategy_id"] = "wait"
    plan["generation_constraints"] = []
    with pytest.raises(PlanRejected) as rejection:
        validate_plan(
            plan=plan, understanding=understanding, aggregate=aggregate(), profile=permissive
        )
    assert rejection.value.reason_code == "SILENT_STRATEGY_PROPOSES_SEND"


def test_generation_matching_its_plan_passes() -> None:
    understanding, plan, generation = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    assert validate_generation(generation=generation, plan=validated, )


def test_generation_may_not_change_the_strategy() -> None:
    understanding, plan, generation = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    generation["strategy_id"] = "advance_goal"
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "GENERATION_CHANGED_STRATEGY"


def test_generation_may_not_add_an_intent() -> None:
    understanding, plan, generation = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    generation["proposed_intents"] = [
        {"kind": "create_order", "typed_payload": {}, "evidence_refs": ["event:1"]}
    ]
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "GENERATION_ADDED_INTENT"


def test_generation_may_not_duplicate_a_planned_intent() -> None:
    understanding, plan, generation = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    generation["proposed_intents"] = generation["proposed_intents"] * 2
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "GENERATION_DUPLICATED_INTENT"


def test_generation_may_not_register_a_commitment_under_no_new_commitment() -> None:
    understanding, plan, generation = steps()
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    assert "no_new_commitment" in validated.document["generation_constraints"]
    generation["safety_markers"] = ["contains_commitment"]
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "UNREGISTERED_COMMITMENT"


def test_evidence_required_rejects_an_unsourced_claim() -> None:
    understanding, plan, generation = steps()
    plan["selected_strategy_id"] = "answer_question"
    plan["generation_constraints"] = ["evidence_required"]
    state = aggregate(definition_id="clarify_request")
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=state, profile=profile()
    )
    generation["strategy_id"] = "answer_question"
    generation["claims"] = [{"text_hash": "0" * 64, "source_evidence_refs": []}]
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "CLAIM_WITHOUT_EVIDENCE"


def test_one_question_constraint_counts_both_question_marks() -> None:
    understanding, plan, generation = steps()
    plan["selected_strategy_id"] = "ask_slot"
    plan["generation_constraints"] = ["one_question"]
    validated = validate_plan(
        plan=plan, understanding=understanding, aggregate=aggregate(), profile=profile()
    )
    generation["strategy_id"] = "ask_slot"
    generation["customer_visible_segments"] = [
        {"type": "text", "content_or_ref": "Which order? 还是另一笔？"}
    ]
    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=validated, )
    assert rejection.value.reason_code == "MORE_THAN_ONE_QUESTION"


def test_model_evidence_carries_no_prompt_or_response_content() -> None:
    _, plan, _ = steps()
    evidence = copy.deepcopy(plan["model_evidence"])
    evidence["prompt"] = "you are a helpful assistant"
    with pytest.raises(ValidationError):
        validate_inference_contract("model-evidence", evidence)


#: `event_type` 的**值域从契约读**，不在这里抄一份。
#:
#: **抄一份正是上一条同型守卫失效的原因**（独立复核 2026-09-16）：
#: `test_remember_write_path.py` 那条「正向判，不是反向列举」的守卫，
#: 迭代的是**手写的四个名字**，其中 `manager_comment` 根本不在这个 enum 里，
#: 而 enum 里真有的 `channel_event` 不在它的清单里。
#: 于是**它迭代的集合恰好等于反向列举会列的集合**，两种极性在它眼里一样绿 ——
#: 而它的 docstring 自称是那道闸极性的**唯一**守卫。
#:
#: 从契约读之后，**迁移或契约里新增一个 event_type，这条用例自动把它跑上**。
def _event_types() -> list[str]:
    schema = json.loads(
        (ROOT / "packages" / "contracts" / "schemas" / "message-envelope.schema.json").read_text(
            encoding="utf-8"
        )
    )
    values = schema["properties"]["event_type"]["enum"]
    assert isinstance(values, list) and len(values) >= 2, "契约里那个 enum 不见了"
    return [str(value) for value in values]


@pytest.mark.parametrize("event_type", _event_types())
def test_only_a_customer_message_turn_may_ask_the_customer_to_clarify(event_type: str) -> None:
    """**只有客户回合能说出那句「您刚才是说…？」** —— 判据正向，不是反向列举。

    D4 落地时写的是 `event_type != "timer"`，于是 `channel_event` /
    `business_event` / `delivery_receipt` / `internal` **四种全都走到
    `ask_clarification`**，而那四种没有一种是客户在说话 ——
    这条路的后果是**平台主动对客户开口**。

    今天那四种在生产里没有生产者（只有 ingress 产 `customer_message`、
    scheduler 产 `timer`），**所以这一条钉的是默认值，不是一个线上缺陷**：
    下一个 event_type 出现时，它应当默认落到「不说话」那一侧。

    **值域从 `message-envelope.schema.json` 读** —— 见 `_event_types` 上面那段：
    同型守卫上一次失效，正是因为它手抄了一份清单。
    """

    understanding, plan, _ = steps()
    plan["selected_strategy_id"] = "propose_order"
    validated, rejection = plan_or_fallback(
        plan=plan,
        understanding=understanding,
        aggregate=aggregate(),
        profile=profile(),
        required_strategy=None,
        event_type=event_type,
    )

    assert rejection is not None, "这一支要的是「计划被拒之后回退选了什么」"
    if event_type == "customer_message":
        assert validated.strategy_id == "ask_clarification", (
            "客户回合、understanding 有内容、点不出槽位 —— 这里正是 D4 要补的那一句话"
        )
    else:
        assert validated.strategy_id == "no_message", (
            f"`{event_type}` 回合上选了 {validated.strategy_id!r} —— "
            "那一轮客户没有说话，一句「您刚才是说…？」是无来由的主动消息。"
            "判据要正向写（`== \"customer_message\"`），不是列举要拒的那几种"
        )
