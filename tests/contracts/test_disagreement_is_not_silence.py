"""计划与生成谈不拢，不以对客户沉默结账（用户 2026-09-17 裁定）。

## 运维实测

`GENERATION_ADDED_INTENT` **九次，今天最大的单一沉默来源**：
`send_message` 6 · `send_material` 2 · `handoff` 1。

计划提名一组候选、生成给了另一个 kind —— 这是**我们内部两步之间的不一致**，
而此前的处置是整轮丢弃，**代价全部落在客户那一侧**：他说了话，没有回音。
入站 `consumed`、没有回执、没有告警，只有 `audit_events` 里一行 `plan.no_send`。
**客户那侧看到的是「已读不回」。**

> 用户：「客户说了话，就要有回复。计划与生成谈不拢是我们内部的事，
> 不该以『对客户沉默』结账 —— 这一族比『答非所问』更严重：那个至少还给了字。」

## 两件，合起来盖住那九次

| | 做什么 | 盖住 |
|---|---|---|
| **补底** | 打法允许 `send_message` 而计划没提名它，**平台补上** | 6/9 |
| **逐条拒** | 多出来的那条丢掉、其余照发 | 3/9 |

## 这份最要紧的两条

**「静默打法不补」**与**「护栏没被放松」** —— 一个放宽最容易在这两处
变成一个洞，而它们都不会自己说话。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import (
    REPLY_FLOOR_BASIS_PREFIX,
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
    validate_plan,
)
from fastagent_v2.worker.strategies import SILENT_STRATEGIES

from tests.contracts.return_path_builders import understanding_with

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = json.loads(
    (REPOSITORY_ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
)


def _aggregate(frame_id: str = "welcome") -> dict[str, Any]:
    definition = next(f for f in PROFILE["target_frames"] if f["id"] == frame_id)
    return {
        "id": str(uuid4()),
        "state_version": 3,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
            }
        ],
    }


def _plan(strategy: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        "selected_strategy_id": strategy,
        "strategy_reason_code": "TEST",
        "confidence": {
            "raw_score": 0.9,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": candidates,
        "generation_constraints": [],
        "manual_review_reason": None,
        "model_evidence": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prompt_release": "m1a-prompt-v8",
            "latency_ms": 1,
            "token_usage": {"input": 1, "output": 1},
        },
    }


def _validated(strategy: str, candidates: list[dict[str, Any]]) -> ValidatedPlan:
    return validate_plan(
        plan=_plan(strategy, candidates),
        # **理解结果也要合同** —— `validate_plan` 连它一起校验，
        # 一个短 stub 在走到目标栈规则之前就被拒了。
        understanding=understanding_with(),
        aggregate=_aggregate(),
        profile=PROFILE,
    )


# ---------------------------------------------------------------------------
# 补底
# ---------------------------------------------------------------------------


def test_a_plan_that_forgot_to_nominate_a_reply_gets_one() -> None:
    """**「回客户一句话」不是需要模型许可的事。**

    打法的 `allowed_intent_kinds` 已经授权了这个 kind；
    计划步没把它写进候选是一次疏漏，不是一次决定。
    运维实测九次里**六次**正是这个形状。
    """

    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" in body["allowed_intent_kinds"] and name not in SILENT_STRATEGIES
    )
    plan = _validated(strategy, [])

    kinds = [candidate["kind"] for candidate in plan.document["candidate_intents"]]
    assert "send_message" in kinds, f"打法 {strategy!r} 允许回话，而计划步没提名，平台也没补"
    assert plan.reply_floor_basis == REPLY_FLOOR_BASIS_PREFIX


def test_a_silent_strategy_is_never_given_a_reply_candidate() -> None:
    """**静默打法不补** —— 它们的全部意思就是这一轮不说话。

    ## 这条用例第一版是空的，探针当场证明了

    第一版直接拿在架配包里的 `wait` 跑 —— 而 `wait` 的
    `allowed_intent_kinds` 是 `[]`，于是**后一道闸（「打法没授权就不补」）
    先返回了**，把前一道遮得严严实实。把静默那道闸整个删掉，
    这条用例照样绿（**反向探针 B 实测**）。

    **两道闸叠在一起时，用例量的是外面那道。**

    ## 所以这里构造一个「静默打法却授权了发消息」的配包

    那时静默那道闸是**唯一的防线**：删掉它，一个本该沉默的回合就会开口，
    而**没有任何东西会说**。这个配包在现实里不该出现，
    而「不该出现」不等于「不会出现」—— 而它一旦出现，代价是客户收到
    一句我们决定了不说的话。
    """

    for strategy in SILENT_STRATEGIES:
        if strategy not in PROFILE["strategies"]:
            continue

        # **故意把后一道闸拿开**：授权它发消息，让静默那道闸单独受考。
        profile = copy.deepcopy(PROFILE)
        profile["strategies"][strategy]["allowed_intent_kinds"] = ["send_message"]

        plan = validate_plan(
            plan=_plan(strategy, []),
            understanding=understanding_with(),
            aggregate=_aggregate(),
            profile=profile,
        )
        kinds = [c["kind"] for c in plan.document["candidate_intents"]]
        assert "send_message" not in kinds, (
            f"{strategy!r} 是静默打法，却被补了一条回话候选 —— 一个本该沉默的回合会开口"
        )
        assert plan.reply_floor_basis is None


def test_that_silent_check_is_reached_at_all() -> None:
    """**上面那条真的跑到了静默那道闸** —— 不是又一次空转。

    判据：在架配包里至少有一个静默打法，否则上面那条的循环体一次都不执行。
    第一版的毛病不是循环空转，而是被后一道闸遮住；
    这条钉的是另一半 —— **循环本身不许空**。
    """

    present = [s for s in SILENT_STRATEGIES if s in PROFILE["strategies"]]
    assert present, (
        f"这个配包一个静默打法都没有（{sorted(PROFILE['strategies'])}）—— 上面那条用例是空转的"
    )


def test_a_strategy_that_may_not_reply_is_not_given_one() -> None:
    """打法没授权 `send_message` 的**不补** —— 那是配包的边界，不是疏漏。"""

    quiet = {
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" not in body["allowed_intent_kinds"]
    }
    for strategy in quiet - set(SILENT_STRATEGIES):
        plan = _validated(strategy, [])
        assert plan.reply_floor_basis is None, f"{strategy!r} 没被授权发消息，却被补了一条"


def test_the_platform_does_not_overwrite_a_reply_the_model_did_nominate() -> None:
    """模型自己提名了就不补 —— **补底只补缺的那一格**。"""

    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" in body["allowed_intent_kinds"] and name not in SILENT_STRATEGIES
    )
    mine = {"kind": "send_message", "typed_payload": {}, "idempotency_basis": "mine"}
    plan = _validated(strategy, [copy.deepcopy(mine)])

    assert plan.reply_floor_basis is None
    bases = [c["idempotency_basis"] for c in plan.document["candidate_intents"]]
    assert bases == ["mine"], f"模型提名过了，而平台还是动了候选：{bases}"


def test_the_floor_is_recognisable_as_the_platforms_own() -> None:
    """补出来的那条**一眼看得出是谁补的**。

    不留痕就等于悄悄改写了模型的计划，而「这一轮的 `send_message`
    是模型提的还是平台补的」必须永远答得出来。
    """

    assert REPLY_FLOOR_BASIS_PREFIX.startswith("platform-")


# ---------------------------------------------------------------------------
# 逐条拒
# ---------------------------------------------------------------------------


def _generation(strategy: str, kinds: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "strategy_id": strategy,
        "customer_visible_segments": [],
        "proposed_intents": [
            {"kind": kind, "typed_payload": {}, "evidence_refs": []} for kind in kinds
        ],
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "claims": [],
        "return_confirmation": {"included": False, "tone_material_ref": None},
        "safety_markers": ["none"],
        "model_evidence": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prompt_release": "m1a-prompt-v8",
            "latency_ms": 1,
            "token_usage": {"input": 1, "output": 1},
        },
    }


def _plan_allowing(kinds: list[str]) -> ValidatedPlan:
    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if set(kinds) <= set(body["allowed_intent_kinds"]) and name not in SILENT_STRATEGIES
    )
    return _validated(
        strategy,
        [{"kind": kind, "typed_payload": {}, "idempotency_basis": f"b-{kind}"} for kind in kinds],
    )


def test_an_unplanned_intent_is_dropped_and_the_rest_still_goes_out() -> None:
    """**多出来的那条丢掉，其余照发** —— 客户至少拿到字。"""

    plan = _plan_allowing(["send_message"])
    dropped: list[dict[str, Any]] = []

    result = validate_generation(
        generation=_generation(plan.strategy_id, ["send_message", "handoff"]),
        plan=plan,
        dropped=dropped,
    )

    assert [i["kind"] for i in result["proposed_intents"]] == ["send_message"]
    assert [i["kind"] for i in dropped] == ["handoff"]


def test_the_default_is_still_to_refuse_the_whole_document() -> None:
    """**不传收集器 = 原样整份拒。**

    这是这个放宽的闸：现有每个调用方的行为一点不变，
    而想走宽松那条路的**必须显式接下记录的责任**。
    一个「默认就宽松」的改动会让每个不知情的调用方悄悄换掉语义。
    """

    plan = _plan_allowing(["send_message"])

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(plan.strategy_id, ["send_message", "handoff"]),
            plan=plan,
        )

    assert "GENERATION_ADDED_INTENT" in str(raised.value.args[0])


def test_dropping_never_turns_a_refusal_into_an_empty_send() -> None:
    """全被丢光时**不是「发一条空的」** —— 它落回「这一轮没提出任何意图」。

    两者的修法不同：一个是提示词，一个是策略（`GENERATION_PRODUCED_NO_INTENT`
    那段注释写着这件事）。把它们混成一种，排查的人会找错方向。
    """

    plan = _plan_allowing(["send_message"])
    dropped: list[dict[str, Any]] = []

    result = validate_generation(
        generation=_generation(plan.strategy_id, ["handoff"]), plan=plan, dropped=dropped
    )

    assert result["proposed_intents"] == []
    assert [i["kind"] for i in dropped] == ["handoff"]


def test_a_second_copy_of_a_planned_kind_is_dropped_not_refused() -> None:
    """⚠️ **运维缺陷 11（2026-09-23，阻塞）：「想多回一句话」被分错了边。**

    计划列 `[send_message, remember]`，生成产出
    `[send_message, send_message, remember]` —— **每个 kind 都在计划里**，
    所以先前那个收集器（只问「这个 kind 在计划里吗」）**一条都丢不掉**，
    紧接着 `len(produced) > len(planned)` 无条件抛，
    整份拒、**0 条出站、客户侧没反应**。

    stage 实测**必现**：同一句客户消息发两次，两次 `detail` 逐字相同，
    而两次模型输出并不相同 —— **文字每次都不一样，形状每次都一样**。

    用户 2026-09-17 的裁定管的正是这一类：
    「客户说了话就要有回复。计划与生成谈不拢是我们内部的事，
    **不该以『对客户沉默』结账**。」
    """

    # ⚠️ **两条候选只能是同一个 kind 的两条** —— 这份配包里没有任何一个
    # 打法同时允许两种意图（实测：`send_message` 的六个打法各只允许它一个）。
    # 而 stage 上出事的那一轮正是这个形状：计划列了两条，生成产出三条。
    plan = _plan_allowing(["send_message", "send_message"])
    generation = _generation(
        plan.strategy_id, ["send_message", "send_message", "send_message"]
    )
    dropped: list[dict[str, Any]] = []

    validate_generation(generation=generation, plan=plan, dropped=dropped)

    assert len(generation["proposed_intents"]) == 2, "留下来的不是计划列的那两条"
    assert len(dropped) == 1, "多出来的那条没被记下来 —— 丢了而不留痕，等于悄悄变宽松"
    assert dropped[0]["kind"] == "send_message"


def test_the_kept_copy_is_the_first_one() -> None:
    """**留靠前的那几条，丢尾巴。**

    提示词说的是「they go out in the order listed」，
    所以「多出来的那一句」在尾巴上 —— 丢头会把客户该先读到的那句丢掉。
    """

    plan = _plan_allowing(["send_message"])
    generation = _generation(plan.strategy_id, ["send_message", "send_message"])
    generation["proposed_intents"][0]["evidence_refs"] = ["first"]
    generation["proposed_intents"][1]["evidence_refs"] = ["second"]
    dropped: list[dict[str, Any]] = []

    validate_generation(generation=generation, plan=plan, dropped=dropped)

    assert generation["proposed_intents"][0]["evidence_refs"] == ["first"]
    assert dropped[0]["evidence_refs"] == ["second"]


def test_without_a_collector_the_duplicate_still_refuses_the_whole_document() -> None:
    """⚠️ **反侧：不传收集器的调用方一点没变，而报里现在说得出多的是哪一个。**

    「悄悄变宽松」在那条路上不是一个可选项 —— 拿不到收集器就接不住，
    接不住就只能整份拒。**而运维 2026-09-23 点名要的那一格补上了**：
    光报数说不出「多的是第二条 `send_message`」还是「多的是第二条 `remember`」，
    而那两种对应完全不同的处置。
    """

    plan = _plan_allowing(["send_message"])
    generation = _generation(plan.strategy_id, ["send_message", "send_message"])

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(generation=generation, plan=plan)

    message = " ".join(str(arg) for arg in raised.value.args)
    assert "GENERATION_DUPLICATED_INTENT" in message
    assert "planned=" in message and "produced=" in message, (
        f"报里还是只有数，说不出多的是哪一个：{message}"
    )


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda g, p: g.__setitem__("strategy_id", "no_message"), "GENERATION_CHANGED_STRATEGY"),
    ],
    ids=["换了打法"],
)
def test_the_other_guards_still_refuse_the_whole_document(mutate, expected) -> None:  # noqa: ANN001
    """**护栏一点没放松。**

    换打法、引没递出的 ref、引不存在的材料/知识 —— 这些仍然整份拒。
    它们拦的是「引了没递给它的东西」，与「想多回一句话」不是一回事，
    **而一个放宽最容易在这里顺手把别的也放了**。
    """

    plan = _plan_allowing(["send_message"])
    generation = _generation(plan.strategy_id, ["send_message"])
    mutate(generation, plan)

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(generation=generation, plan=plan, dropped=[])

    assert expected in str(raised.value.args[0])
