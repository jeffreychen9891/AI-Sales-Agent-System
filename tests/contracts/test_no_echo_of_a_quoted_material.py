"""引了素材就不许再用自己的话说一遍 · 主动话术也只发一次。

## 现场（用户 2026-09-17 16:40:02 实测，逐字）

```
segments = [
  {text:        "接入这块，您这边主要配合三件事：企微账号授权、话术终审、对外资料原件。"},
  {material_ref: "material:demo-trial-how:v1"},
]
```

模型先用自己的话复述了一遍固定话术，系统又把原文逐字贴上 ——
**客户在同一条消息里读到同一件事两遍**。

**逐字重合 20 个字**（「：企微账号授权、话术终审、对外资料原件。」），
占模型那句 57%、占素材 56%。不是碰巧撞词。

## 规则早就有，守卫不在

生成步的系统提示逐字写着：

> **do not copy, shorten, translate, merge or reword a material into a
> text segment.** Text segments are for your own words **around** a quoted one.

**而违反它不会红** —— CLAUDE.md 第三条那个形状。

## 顺带查出的不对称

`select_assets` 有 `already_sent`（「主动只发一次」），
而 **`select_materials` 根本不滤已发过的** —— 它只用 `delivered` 算阶段。
于是「只推一次」只能写进 `goal`，靠**模型自己填的** `material_delivered` 记账，
而同一个仓里 `delivered_materials` 的注释正是为避免这件事写的。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import (
    ECHOED_MATERIAL_CHARS,
    ValidatedPlan,
    validate_generation,
    validate_plan,
)
from fastagent_v2.worker.material_selection import select_materials

from tests.contracts.return_path_builders import understanding_with

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = json.loads(
    (REPOSITORY_ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
)

#: 逐字取自用户 2026-09-17 16:40:02 的实测。
MATERIAL_REF = "material:demo-trial-how:v1"
MATERIAL_TEXT = "前十天免费，开通要您这边配合三件：企微账号授权、话术终审、对外资料原件。"
ECHO_TEXT = "接入这块，您这边主要配合三件事：企微账号授权、话术终审、对外资料原件。"

#: 一句合理的呼应 —— 提到了素材里的数字，而没有抄走一整段。
FAIR_TEXT = "一个月 1888 这个数我先放这儿，您看要不要现在就开。"


def _aggregate() -> dict[str, Any]:
    definition = PROFILE["target_frames"][0]
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


def _plan() -> ValidatedPlan:
    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" in body["allowed_intent_kinds"] and name != "wait"
    )
    return validate_plan(
        plan={
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
            "candidate_intents": [
                {"kind": "send_message", "typed_payload": {}, "idempotency_basis": "b1"}
            ],
            "generation_constraints": [],
            "manual_review_reason": None,
            "model_evidence": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "prompt_release": "m1a-prompt-v8",
                "latency_ms": 1,
                "token_usage": {"input": 1, "output": 1},
            },
        },
        understanding=understanding_with(),
        aggregate=_aggregate(),
        profile=PROFILE,
    )


def _generation(plan: ValidatedPlan, texts: list[str]) -> dict[str, Any]:
    segments = [
        *({"type": "text", "content_or_ref": text} for text in texts),
        {"type": "material_ref", "content_or_ref": MATERIAL_REF},
    ]
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "strategy_id": plan.strategy_id,
        "customer_visible_segments": list(segments),
        "proposed_intents": [
            {
                "kind": "send_message",
                "typed_payload": {"content": {"segments": list(segments)}},
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
            "prompt_release": "m1a-prompt-v8",
            "latency_ms": 1,
            "token_usage": {"input": 1, "output": 1},
        },
    }


def _run(texts: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = _plan()
    echoed: list[dict[str, Any]] = []
    result = validate_generation(
        generation=_generation(plan, texts),
        plan=plan,
        materials={MATERIAL_REF: MATERIAL_TEXT},
        echoed=echoed,
    )
    return result, echoed


def _kinds(document: dict[str, Any]) -> list[str]:
    segments = document["proposed_intents"][0]["typed_payload"]["content"]["segments"]
    return [segment["type"] for segment in segments]


# ---------------------------------------------------------------------------
# 复述
# ---------------------------------------------------------------------------


def test_the_sentence_that_restates_a_quoted_material_is_dropped() -> None:
    """**实测那一条，逐字**。客户不该在一条消息里读到同一件事两遍。"""

    document, echoed = _run([ECHO_TEXT])

    assert _kinds(document) == ["material_ref"], "复述那句还在"
    assert document["customer_visible_segments"] == [
        {"type": "material_ref", "content_or_ref": MATERIAL_REF}
    ], "草稿那半没清"
    assert len(echoed) == 2, f"草稿与载荷各该记一条：{echoed}"
    assert all(item["shared_chars"] >= ECHOED_MATERIAL_CHARS for item in echoed)


def test_the_merchants_own_words_survive() -> None:
    """**丢的是复述，留的是素材原文** —— 不退回沉默。

    与「谈不拢逐条拒」同一条纪律（用户 2026-09-17）：
    客户拿到的是商家审过的那句，而不是零字。
    """

    document, _ = _run([ECHO_TEXT])

    segments = document["proposed_intents"][0]["typed_payload"]["content"]["segments"]
    assert segments, "全清光了 —— 那就是把一次复述换成了一次沉默"
    assert segments[0]["content_or_ref"] == MATERIAL_REF


def test_a_fair_echo_of_a_number_is_not_a_restatement() -> None:
    """**部分呼应不算复述。**

    模型说「一个月 1888」而素材里也有那四个字 —— 那是自然的衔接，
    不是抄走一整段。阈值 {chars} 正是为把这两种分开而定的。
    """

    document, echoed = _run([FAIR_TEXT])

    assert not echoed, f"一句正常的衔接被当成复述丢了：{echoed}"
    assert _kinds(document) == ["text", "material_ref"]


def test_text_without_any_quoted_material_is_never_touched() -> None:
    """**没引素材就没有复述可言** —— 那时用自己的话回答正是我们要的。"""

    plan = _plan()
    echoed: list[dict[str, Any]] = []
    generation = _generation(plan, [ECHO_TEXT])
    for where in (
        generation["customer_visible_segments"],
        generation["proposed_intents"][0]["typed_payload"]["content"]["segments"],
    ):
        where[:] = [s for s in where if s["type"] != "material_ref"]

    validate_generation(
        generation=generation, plan=plan, materials={MATERIAL_REF: MATERIAL_TEXT}, echoed=echoed
    )

    assert not echoed


def test_the_check_is_off_unless_the_caller_asks_for_it() -> None:
    """**不传收集器就不查** —— 与 `dropped` 同一个形状。

    现有调用方一点不变，而想要这件事的**必须接下记录的责任**。
    """

    plan = _plan()
    document = validate_generation(
        generation=_generation(plan, [ECHO_TEXT]),
        plan=plan,
        materials={MATERIAL_REF: MATERIAL_TEXT},
    )

    assert _kinds(document) == ["text", "material_ref"], "没要求查，却把片段丢了"


def test_the_threshold_separates_the_two_measured_cases() -> None:
    """阈值把**实测那条**与**合理呼应**分在两边 —— 这是它的全部理由。"""

    from fastagent_v2.worker.inference_validation import (  # noqa: PLC0415
        _longest_shared_run,
    )

    echo = _longest_shared_run(ECHO_TEXT, MATERIAL_TEXT)
    fair = _longest_shared_run(FAIR_TEXT, MATERIAL_TEXT)

    assert len(echo) >= ECHOED_MATERIAL_CHARS, f"实测那条只重合 {len(echo)} 字"
    assert len(fair) < ECHOED_MATERIAL_CHARS, f"正常衔接重合了 {len(fair)} 字"


# ---------------------------------------------------------------------------
# 主动话术只发一次
# ---------------------------------------------------------------------------


def _definition(availability: str) -> dict[str, Any]:
    return {
        "id": "f",
        "content_materials": [
            {
                "ref": MATERIAL_REF,
                "availability": availability,
                **({"stages": ["01"]} if availability == "proactive" else {"topics": ["t"]}),
            }
        ],
        "material_stages": [{"name": "01", "after": []}],
        "material_topic_slot": "topic",
        "information_slots": [{
            "name": "topic",
            "type": "string",
            "required": False,
            "description": "Which topic this turn is about.",
        }],
    }


def test_a_proactive_material_is_offered_once() -> None:
    """**主动话术也只发一次** —— 在此之前只有资料有这一档。

    `select_assets` 滤 `already_sent`，而这里此前只拿 `delivered` 算阶段，
    一条 `proactive` 话术**永远在被递**。
    """

    offered = select_materials(
        definition=_definition("proactive"),
        declared={MATERIAL_REF: MATERIAL_TEXT},
        emit_kind=None,
        already_sent=(MATERIAL_REF,),
    )

    assert offered == {}, "发过了还在递"


def test_an_on_request_material_is_not_read_out_a_second_time() -> None:
    """**一段话术，一通对话里只逐字发一次** —— `on_request` 也不例外。

    ## 这条用例 2026-09-20 反过来了，而原文留在这里

    ~~**`on_request` 不滤** —— 他又问一次价格，就该又拿到那句报价。
    与 `select_assets` 逐字同一条判据，**不另立一套**。~~

    **「不另立一套」那半句正是错的地方。** 运维 §八 实测：一小时里
    `material:demo-price-plain:v1` 被逐字引了 **3 次**。
    运维同节写下的判据逐字：

    > 那句话对**文件**成立（再要一次报价表，再发一份是对的）；
    > **对一段话术不成立** —— 同一句话逐字说三遍，客户读到的是复读。

    所以今天**确实是两套**：`select_assets` 的豁免一个字没动，
    `select_materials` 这一侧收掉了。

    ## 那客户第二次问价还拿得到价格吗 —— 拿得到

    **用户 2026-09-20 裁定：「第一次用商家给的固定话术，之后简单报价就行。」**
    帧目标同批改了：已经引过就直接用自己的话说那个数。
    而那句话**不会被数字闸丢掉** —— `_CUSTOMER_PAYMENT_CLAIM` 只匹配
    「您/你…付/交/花/掏…<数>」那种**替客户认下一笔支出**的说法。
    """

    offered = select_materials(
        definition=_definition("on_request"),
        declared={MATERIAL_REF: MATERIAL_TEXT},
        emit_kind=None,
        # ⚠️ **「他又问一次」要写成这一轮的 update**（2026-09-18 裁定）。
        #
        # 上一版把它写成 `frame={"slots": {"topic": "t"}}` —— 一个**存着的**值。
        # 而这条用例自己的话是「**他又问一次**价格，就该又拿到那句报价」：
        # 「又问一次」是这一轮发生的事，**不是槽位里黏着的那个值**。
        # 旧夹具正是把这两件事混在一起的那个写法。
        understanding={"slot_updates": [{"slot": "topic", "value": "t"}]},
        already_sent=(MATERIAL_REF,),
    )

    assert MATERIAL_REF not in offered, (
        "同一段话术又被递了第二次 —— 客户读到的是复读（运维 §八 实测 3 次）"
    )


def test_nothing_sent_yet_still_gets_offered() -> None:
    """**不是把什么都滤掉** —— 否则上面那条证明不了任何事。"""

    offered = select_materials(
        definition=_definition("proactive"), declared={MATERIAL_REF: MATERIAL_TEXT}, emit_kind=None
    )

    assert MATERIAL_REF in offered


@pytest.mark.parametrize("field", ["already_sent", "delivered"])
def test_the_two_records_are_different_parameters(field: str) -> None:
    """**「发过了」与「算到哪个阶段」是两个参数，别合并。**

    `delivered` 驱动阶段推进，`already_sent` 决定还递不递 ——
    合成一个看起来省事，而那会让「推进阶段」与「不再重复」变成同一件事，
    于是修其中一个必然动到另一个。
    """

    import inspect  # noqa: PLC0415

    assert field in inspect.signature(select_materials).parameters


# ---------------------------------------------------------------------------
# 「没声明 availability」那一档（复核 2026-09-18 退回）
# ---------------------------------------------------------------------------


#: `content_materials` 的两种合法拼法 —— schema 的 `required` 只有 `ref`。
#:
#: **资料那边不同**：`content_assets` 的 `required` 是 `['ref','availability']`，
#: 所以那边没有这一档。照抄它的判据时，正是这里会掉进去。
UNRESTRICTED_SPELLINGS = [
    pytest.param(MATERIAL_REF, id="裸字符串"),
    pytest.param({"ref": MATERIAL_REF}, id="只带-ref-的对象"),
]


def _definition_with(declaration: Any) -> dict[str, Any]:
    return {
        "id": "f",
        "content_materials": [declaration],
        "material_stages": [{"name": "01", "after": []}],
        "material_topic_slot": "topic",
        "information_slots": [{
            "name": "topic",
            "type": "string",
            "required": False,
            "description": "Which topic this turn is about.",
        }],
    }


@pytest.mark.parametrize("declaration", UNRESTRICTED_SPELLINGS)
def test_a_material_without_an_availability_is_offered_every_turn(declaration: Any) -> None:
    """**没声明 `availability` 的那一档，发过了也照样递。**

    ## 这条是复核 2026-09-18 退回本支的那一格

    第一版的过滤写成 `str(declaration.get("availability")) != ON_REQUEST` ——
    而 `str(None)` 是 `"None"`，不等于 `"on_request"`，**于是这一档被滤掉了**。

    而它的含义在两处白纸黑字：schema 的 description 写着
    「declared without restriction, **offered on every turn**」，
    `_is_offered` 对 `availability is None` 直接 `return True`。

    ## 为什么照抄资料那边的判据会掉进来

    `content_assets` 的 schema `required` 是 `['ref','availability']` ——
    **那边根本没有这一档**。判据照抄过来时，「不是 on_request」
    在资料那边等价于「proactive 或 timer」，**在话术这边不等价**。

    ## 今天影响是 0，而这正是它危险的地方

    三个配包 35 处声明全带了 `availability`，所以它不是在跑的 bug ——
    **是埋好的那种**：哪天有人写一条裸 ref，它会安静地不再被递，
    而包能封、能发、对话照跑。
    """

    offered = select_materials(
        definition=_definition_with(declaration),
        declared={MATERIAL_REF: MATERIAL_TEXT},
        emit_kind=None,
        already_sent=(MATERIAL_REF,),
    )

    assert MATERIAL_REF in offered, (
        "没声明 availability 的那一档被当成主动话术滤掉了 —— "
        "而 schema 与 `_is_offered` 都说它每轮都该递"
    )


def test_the_filter_names_the_tiers_it_means() -> None:
    """**正向列举，不写成「不是 on_request」。**

    两种写法只在「没声明」那一档上给出相反结果，
    而复核的探针 G 实测：换成另一种写法时**自身与 control 全绿** ——
    **没有一条用例说得出哪种是对的**。上面那条补的就是这件事，
    这一条补的是形状：**判据要点名它管的那几档**，
    因为「不是 X」会随着 X 的兄弟增加而悄悄改变含义。
    """

    source = (
        REPOSITORY_ROOT
        / "services"
        / "worker"
        / "src"
        / "fastagent_v2"
        / "worker"
        / "material_selection.py"
    ).read_text(encoding="utf-8")

    start = source.index("def select_materials(")
    body = source[start : source.index("\nCONTENT_ASSETS_KEY", start)]
    # **只看代码行，不看注释** —— 那段注释里逐字引着写错的那一版
    # （「第一版写的是 `str(...) != ON_REQUEST`」），而那是留痕不是代码。
    # 这条用例第一版连注释一起扫，于是**红在自己的解释上**。
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))

    # ⚠️ **判的是那个元组里有哪几个名字，不是一串字面量**
    # （2026-09-20 改）。上一版断言的是
    # `'availability") in (PROACTIVE, TIMER)'` 这一整串 ——
    # 本批把它扩成三档并换了行，那条断言就红在**排版**上，
    # 而它要守的「正向列举，不是排除式」一个字都没变。
    #
    # **同一段断言上一版写了两遍**（复制粘贴），这次一并去掉。
    import re  # noqa: PLC0415

    enumerated = re.search(r'availability"\)\s*in\s*\(([^)]*)\)', code)
    assert enumerated, (
        "那道过滤不再是「点名哪几档」的形状了 —— "
        "「不是 X」会随着 X 的兄弟增加而悄悄改变含义"
    )
    names = {name.strip() for name in enumerated.group(1).split(",") if name.strip()}
    assert names == {"PROACTIVE", "TIMER", "ON_REQUEST"}, (
        # 1) 看到了什么
        f"那道过滤现在点名的是 {sorted(names)}。"
        # 2) 为什么这条要红
        "\n**2026-09-20 起 `on_request` 也在里面**（用户裁定：一段话术一通对话"
        "只逐字发一次，之后简单报价就行）—— 而**「没声明 `availability`」那一档"
        "仍然不在**：schema 与 `_is_offered` 都说它「不受限制，每轮都递」。"
        # 3) 到期了该做什么
        "\n要改这几档，先读 `select_materials` 里那段留痕：资料那一侧的豁免"
        "（他又要一次价目表就再发一份）**是对的，只是不该套到话术上**。"
    )
    assert "!= ON_REQUEST" not in code, (
        "「不是 on_request」回来了 —— 它把「没声明」那一档也算了进去"
    )
