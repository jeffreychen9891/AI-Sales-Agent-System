"""计划步看得见文件，而没递出去的 ref 一律拒。

## 为什么有这份

运维实测：十一轮真实对话，其中包括「这个多少钱」「能发一份报价单吗？」——
**零个 `send_material` 意图**。他们把门逐条量过：话题槽落在「花钱」、
资料声明了那个话题、那一轮的策略允许这个 kind、候选选取按设计不查目录。
门全是开的。

根因在他们量不到的一层：**`plan_prompt` 没有 `content_assets` 这个参数**。
而候选意图的 `typed_payload` 是**必填**的 —— 提名一个 `send_material`
就得同时给出 ref，ref 只能从那份清单来。计划步拿不到清单，于是从不提名；
**生成步提不出候选里没有的 kind**。

## 而补上这一半会开另一个口子，所以两件一起做

计划在换帧**之前**的帧里挑候选，生成用换帧**之后**的清单 —— 两份可能不同。
更早就存在的那个口子是：`send_material` 的 payload 闸只查 ref 语法，
派发按 slug 直接查目录，**没有任何东西问过「这一轮递给它了吗」**。

所以同一批补上那道闸。它一次兜住三件，第三件是回归：
**「主动只发一次」此前只滤候选、不拦发送** —— 模型记着上一轮那个 ref 就能再发。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.runtime import request_shape
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_the_plan_prompt_carries_the_files_this_turn_may_send() -> None:
    """**清单进了计划步的请求**，而这是零提名那件事的根因。"""

    prompt = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=["answer_question"],
        strategies={},
        event_kinds=[],
        content_assets=[{"ref": "asset:pricing-sheet:v1", "label": "pricing-sheet"}],
    )

    assert prompt["content_assets"] == [{"ref": "asset:pricing-sheet:v1", "label": "pricing-sheet"}]


def test_the_key_is_sent_even_when_there_is_nothing_to_send() -> None:
    """空着也发。

    「这一轮没有文件可发」与「没人告诉你有文件这回事」**不该在模型眼里长得一样** ——
    与生成步那三个键同一个理由。
    """

    assert (
        request_shape.plan_prompt(
            goal="g",
            exit_conditions=[],
            information_slots=[],
            allowed_strategies=[],
            strategies={},
            event_kinds=[],
        )["content_assets"]
        == []
    )


def test_plan_and_generate_are_handed_the_same_shape() -> None:
    """两步收到的是**同一个形状**，不是两种。

    形状一分叉，「计划提的 ref 生成认不认」就变成一道要读两处才能回答的题 ——
    而本仓为「同一件事两处各写一遍」付过好几次钱。
    """

    entry = {"ref": "asset:a:v1", "label": "a"}
    plan = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        content_assets=[entry],
    )
    generate = request_shape.generate_prompt(content_assets=[entry])

    assert plan["content_assets"] == generate["content_assets"]


def test_the_pipeline_hands_the_plan_the_list_it_just_selected() -> None:
    """**选出来的那份，就是喂进去的那份。**

    ## 为什么这条是读源码而不是跑一个回合

    调用点在 `process_prepared` 里，驱动它要一个真库、一个带资料的档案包、
    一条上传认领过的目录行 —— 那是集成层的一格。而**没有这条，
    `assets=()` 这个变异全绿**（反向探针 B 实测，2026-09-17）：
    参数在、没人喂，两头都在中间没接。本仓为这个形状付过好几次钱
    （`proactivity_policy` 整块生产代码零消费方那次）。

    ## 它不是一条「有没有这个字符串」的断言

    读的是 AST：`self._inference.plan(...)` 那次调用的 `assets=` 关键字，
    **必须绑到 `select_assets(...)` 赋给的那个名字上**。
    写死一个空元组、换成别的名字、整个参数删掉 —— 三样都会红。
    """

    import ast  # noqa: PLC0415

    source = (
        REPOSITORY_ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    selected: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if isinstance(call, ast.IfExp):
            # `select_assets(...) if frame else ()` —— 真正的调用在 body 那半。
            call = call.body
        if not isinstance(call, ast.Call):
            continue
        if isinstance(call.func, ast.Name) and call.func.id == "select_assets":
            selected.update(target.id for target in node.targets if isinstance(target, ast.Name))

    assert selected, "管线里找不到 `select_assets(...)` 的赋值 —— 选取那一步没了"

    fed: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "plan"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "assets":
                fed.append(
                    keyword.value.id
                    if isinstance(keyword.value, ast.Name)
                    else ast.dump(keyword.value)
                )

    assert fed, "计划那次调用没带 `assets=` —— 计划步又看不见文件了"
    assert set(fed) <= selected, (
        f"计划步收到的不是选出来的那份：喂的是 {sorted(set(fed))}，选取赋给的是 {sorted(selected)}"
    )


def _generation(reference: str) -> dict[str, Any]:
    """一份刚好过 schema 的生成结果 —— 十个必填字段一个不少。

    形状照抄 `test_generation_receives_materials` 里那份：模型返回七个键，
    另外三个（`schema_version` / `origin` / `model_evidence`）是
    `ModelBackedInference` 按实测事实盖上去的。**不省字段** ——
    `validate_generation` 先过 schema 再走那道闸，
    少一个字段这条用例红的就不是它要守的东西。
    """

    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "strategy_id": "acknowledge",
        "customer_visible_segments": [],
        "proposed_intents": [
            {
                "kind": "send_material",
                "typed_payload": {"ref": reference},
                "evidence_refs": [],
            }
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
            "prompt_release": "m1a-prompt-v6",
            "latency_ms": 1,
            "token_usage": {"input": 11, "output": 7},
        },
    }


@pytest.mark.parametrize(
    ("offered", "reference", "refused"),
    [
        ({"asset:a:v1"}, "asset:a:v1", False),
        ({"asset:a:v1"}, "asset:b:v1", True),
        (set(), "asset:a:v1", True),
    ],
    ids=["offered", "a-different-one", "nothing-offered"],
)
def test_an_asset_that_was_not_offered_this_turn_is_refused(
    offered: set[str], reference: str, refused: bool
) -> None:
    """**没递出去的不许发** —— 三档一起钉。

    第三档（这一轮一份都没递）单独列出来，是因为它与 `None` 是两件事：
    `None` = 调用方没走资料这条路，不施加；
    **空集 = 递出去零份，那么任何 ref 都是编的**。
    """

    plan = _plan_stub()
    if refused:
        with pytest.raises(GenerationRejected) as raised:
            validate_generation(generation=_generation(reference), plan=plan, assets=offered)
        assert "GENERATION_ASSET_NOT_OFFERED" in str(raised.value) or (
            raised.value.args and raised.value.args[0] == "GENERATION_ASSET_NOT_OFFERED"
        )
    else:
        validate_generation(generation=_generation(reference), plan=plan, assets=offered)


def _plan_stub() -> ValidatedPlan:
    """一个刚好够 `validate_generation` 走到资料那道闸的计划。

    **用真的 `ValidatedPlan`，不自造替身** —— `strategy_id` / `is_silent`
    是它从 `document` 上算出来的属性，自造的 dataclass 会让这条用例
    停在「替身缺个属性」上，而不是停在它要守的那道闸上。
    """

    return ValidatedPlan(
        document={
            "schema_version": "1.0.0",
            "origin": "model",
            # 与 `_generation()` 里的 `strategy_id` **必须一致**：不一致先撞
            # `GENERATION_CHANGED_STRATEGY`，资料那道闸一次都跑不到。
            "selected_strategy_id": "acknowledge",
            "generation_constraints": [],
            # 生成步只能提候选里有的 kind。
            "candidate_intents": [{"kind": "send_material", "idempotency_basis": "b1"}],
        },
        frame={"id": "frame-1", "definition_id": "introduce"},
        definition={"id": "introduce", "allowed_intent_kinds": ["send_material"]},
    )
