"""声明的必填槽位空着而计划既不填也不问 → 拒（闸二，ADR-0137 裁定一）。

## 判据是「问了」，不是「填满了」

图纸 §4.1：`advance_goal`「不能…跳过必填槽位」。ADR 把它定成 ——
一个槽位还空着而计划**点名去问它**是合法的（那正是「根据未完成槽位选择下一要点」），
**空着又不问才是跳过**。取「一律拒」会让声明了必填槽位的打法在槽位填齐前完全不可用，
而那不是图纸说的话。

**数据源是 `strategies[<选中策略>].required_slots`**，不是
`definition["information_slots"][].required` —— 后者由 `op == "complete"` 那道闸读。
两个源、两个时点、两件事。

## 三条边界（用户 2026-09-13 重裁，`ask_slot` 整个拿掉）

1. `required_slot_requests` 点名了那个缺的槽位 → **算问了**；
2. 点名了**别的**槽位 → **不算**；
3. **自由文本里捎带问一句 → 不算** —— 平台读不出来，读出来也是猜。

**初版那三条写着「选了 `ask_slot` 且点名了那个槽位才算问了」，而它把判据和证据搞混了**：
判据是「这一轮有没有去问那个缺的槽位」，证据是 `required_slot_requests` 有没有点名它；
写成「证据是 `selected_strategy_id == ask_slot`」是另一件事。
实测 `ask_slot` 自己的 `required_slots` 在三份配包里都是 `[]` ——
于是那条边界描述的场景**这道闸根本走不到**：选 `ask_slot` 闸不触发，
选别的又永远不算「问了」，`required_slot_requests` 与本闸无关，闸退化成「一律拒」。

## ⚠️ 放行之后还有一个洞，今天没有检测方（登记 T331）

计划**点名**了那个槽位，而发出去的那句话**没真问**。
`pending_question` 记的是「点名了 + 这一轮发了消息」——**它记的是声称，不是问**
（实测它的两个前提：`required_slot_requests` 非空、且有一条客户可见出站行活下来；
它不看 `selected_strategy_id`，也不读那句话的正文）。

> **模型可以每轮点名同一个槽位、每轮发别的话，
> 而 `pending_question` 会一路显示「问过、没答」，看起来像客户不回答。**

下一个人看到「客户连着三轮不回答同一个问题」的数据时，要知道还有这另一种解释。
另一处盲区：那份证据**只在发出了消息的回合才留下**，被别的闸挡住的回合什么都不记。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected, plan_or_fallback, validate_plan

ROOT = Path(__file__).resolve().parents[2]

#: 那份包的 id 是 `synthetic-two-axis-desk`，而它住在 `profiles/synthetic-carrier/`
#: —— 按 id 拼路径会 `FileNotFoundError`（闸一那批踩过，13 条一起红）。
PROFILE_PATH = ROOT / "profiles" / "synthetic-carrier" / "profile.json"

#: 它声明 `required_slots: ["item_category", "rental_period"]`，是全仓唯一声明了
#: **两个**必填槽位的打法 —— 「缺两个点名一个」那条用例要用它。
STRATEGY = "propose_quote"
FRAME = "rental"

_EVIDENCE = {
    "provider": "test",
    "model": "test",
    "prompt_release": "test",
    "latency_ms": 0,
    "token_usage": {"input": 0, "output": 0},
}


def _profile() -> dict[str, Any]:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _declared(profile: dict[str, Any]) -> list[str]:
    declared = list(profile["strategies"][STRATEGY]["required_slots"])
    assert len(declared) >= 2, (
        f"这份用例依赖 {STRATEGY!r} 声明至少两个必填槽位，实测 {declared} —— "
        "配包变了就换一个声明了多个的打法，别把「缺两个点名一个」那条改成单槽位"
    )
    return declared


def _aggregate(profile: dict[str, Any], *, slots: dict[str, str] | None = None) -> dict[str, Any]:
    definition = next(f for f in profile["target_frames"] if f["id"] == FRAME)
    return {
        # **这两个键不是本批的新需求**（T339 第二批，2026-09-13）——
        # `safe_fallback_plan` 的 `idempotency_basis` 一直在读它们，
        # 而这份夹具一直没有：那条回退分支**从来没有被任何用例走过**（T340）。
        # 本批让「点得出槽位名的拒绝」走 `ask_slot`，于是它第一次被走到，
        # 缺键当场 `KeyError: 'id'`。**补上是让夹具变成生产形状，
        # 不是为了满足一条新要求** —— 生产里这两个键来自 `conversation_aggregates`。
        "id": "11111111-1111-4111-8111-111111111111",
        "state_version": 7,
        "generation": 1,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": dict(slots or {}),
                "slot_evidence": {},
                "entered_at": "2026-09-13T00:00:00Z",
                "last_progress_at": "2026-09-13T00:00:00Z",
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


def _plan(
    profile: dict[str, Any], *, requests: list[str], strategy: str = STRATEGY
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        "selected_strategy_id": strategy,
        "strategy_reason_code": "QUOTE_THE_RENTAL",
        "confidence": {
            "raw_score": 0.9,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "required_slot_requests": list(requests),
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": list(
            profile["strategies"][strategy].get("generation_constraints", [])
        ),
        "manual_review_reason": None,
        "model_evidence": _EVIDENCE,
    }


def test_empty_and_not_asked_is_refused() -> None:
    """**场景 ②**：槽位空着、计划也不问 → 拒，理由码是闸二的那个。

    这是图纸说的「跳过」：直接报价，而报价要的两个槽位一个都不知道，也不打算问。
    """

    profile = _profile()

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan(profile, requests=[]),
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )

    assert refused.value.reason_code == "STRATEGY_SKIPS_REQUIRED_SLOT"
    # 报错要点名是哪几个槽位 —— 否则读日志的人只知道「跳过了必填槽位」。
    for name in _declared(profile):
        assert name in str(refused.value)


def test_empty_but_asked_is_allowed() -> None:
    """**场景 ③（反面）**：同样的空槽位，计划点名去问 → 放行。

    没有这一条，一个「一律拒」的实现也能让上面那条通过 ——
    而那会让这条打法在槽位填齐前完全不可用，与图纸「根据未完成槽位选择下一要点」相反。
    **这一条是整个判据的立足点。**
    """

    profile = _profile()
    declared = _declared(profile)

    validated = validate_plan(
        plan=_plan(profile, requests=declared),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert validated.strategy_id == STRATEGY


def test_asking_a_different_slot_does_not_count() -> None:
    """**边界 2**：点名了**别的**槽位 → 不算问了。

    **这一条是丙那个读法退化成「问了任何东西就放行」的唯一入口** —— 所以单独成条。

    **「唯一」的适用范围是「模型计划之内」，不是全局。**
    `origin == "safe_fallback"` 的计划**永不经过这道闸**，而那是设计如此，不是漏洞：
    回退计划走自己的 `SAFE_FALLBACK_STRATEGIES` 白名单，
    它的 `required_slot_requests` 恒为空，管上它就会让它永远被拒 ——
    而它是被拒之后唯一的出路（那一条由
    `test_the_fallback_is_not_subject_to_this_gate` 钉着）。
    **不带这句限定，下一个人发现回退计划绕过本闸时会以为找到了一个漏洞。**
    点名的那个槽位必须在本帧里存在（否则先撞 `SLOT_NOT_IN_FRAME`，
    那是另一道闸、另一件事），所以这里借一个**本帧有、而这条打法没声明**的槽位。
    """

    profile = _profile()
    declared = set(_declared(profile))
    definition = next(f for f in profile["target_frames"] if f["id"] == FRAME)
    other = next(
        slot["name"] for slot in definition["information_slots"] if slot["name"] not in declared
    )

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan(profile, requests=[other]),
            understanding=_understanding(),
            aggregate=_aggregate(profile),
            profile=profile,
        )

    assert refused.value.reason_code == "STRATEGY_SKIPS_REQUIRED_SLOT", (
        f"点名了 {other!r}（本帧有，但不是这条打法声明的必填槽位）—— "
        "这不算问了，否则这道闸会退化成「问了任何东西就放行」"
    )


def test_the_loose_reading_naming_one_missing_slot_is_enough() -> None:
    """**这是一次取舍，不是一条漏写的断言。** 缺两个而只点名一个 → 放行。

    **另一侧长什么样**：取「严」= 缺 N 个必须点名 N 个，
    于是「这一轮先问最要紧的那一个」会被拒。

    **取松的依据（实测，不是偏好）**：严的成本是一次拒绝（拒绝路径不产出出站行，
    客户这一轮沉默），而它的收益是零 ——

    1. `pending_question` **只记第一个被点名的槽位**（`pipeline._record_pending_question`），
       所以把 N 个都列出来**不会**给另外那几个留下任何证据；
    2. 剩下那几个**下一轮照旧看得见** —— 它们在提示词的
       `current_frame.required_slots` 里（那处注释自己写着这句话）；
    3. 所以「点名两个」与「点名一个」在**下游行为上没有差别**，
       只在这道闸放不放行上有差别。

    **一处要更正的依据**：交办时给的理由是「点名两个会违反 `one_question`，
    点名一个被这道闸拒，模型两难」—— **实测不成立**，三点：
    `one_question` 由 **`ask_slot`** 声明（而 `ask_slot` 的 `required_slots` 是 `[]`，
    这道闸对它不触发）；它在 **generation** 步执行、量的是**那句话里有几个问号**
    （`inference_validation` 里 `_question_count(generation) > 1`）；
    而 `required_slot_requests` 的语义是「**还缺这些**」不是「这一轮全问」
    （`pipeline` 里那句注释）。

    **钉死它的那一个数**：`plan-result` 契约给这个字段的是
    `{"type": "array", "maxItems": 8, "uniqueItems": true}` ——
    **点名两个连合同都不违反**。而 `one_question` 量的是
    `customer_visible_segments` 里文本段的 `?` 与 `？` 的个数
    （`_question_count`），与这个数组有几项**没有任何关系**。
    **所以那个两难不存在。**

    **`uniqueItems: true` 还多说了一层，而它是一条可以自己走一遍的推理**：
    那条约束管的是「项与项之间不许重复」；一个**预期只有一项**的数组里
    没有第二项能与第一项重复，**对它而言这条约束不可能被违反、也就没有作用**；
    而写 schema 的人不会为一个不可能被违反的情形加一条约束 ——
    **所以它的存在本身就是「预期多项」的证据**：
    点名多个是契约**明确允许的形状**，不是钻了一个空子。
    （写成四步而不是写成「否则那个约束毫无意义」：后者要人相信一个判断，
    前者谁都能自己走一遍。）
    取松仍然对，只是依据换成上面那三条。

    **要改成严，改的是 `inference_validation` 里那个 `&`**，而这一条会红 ——
    红的时候要读的是本段，不是把它当成一条写错的用例。
    """

    profile = _profile()
    declared = _declared(profile)

    validated = validate_plan(
        plan=_plan(profile, requests=[declared[0]]),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert validated.strategy_id == STRATEGY, (
        "缺多个而点名其中一个，本仓取**松**（点名任意一个空着的即放行）—— "
        "这是一次取舍：严的成本是一次拒绝（客户沉默），而收益是零，"
        "因为 pending_question 只记第一个被点名的槽位、其余那些下一轮仍在提示词里。"
        "改成严之前先读这条用例的 docstring"
    )


def test_a_filled_slot_is_not_asked_about() -> None:
    """填齐了就不必问 —— 闸只管**空着的**那些。

    一个「声明了就必须点名」的实现会让这一条红，而那会要求模型在槽位已知时
    仍然把它列进 `required_slot_requests`，于是 `pending_question` 会记下
    一个根本没问的问题（它的判据是「点名了 + 发了消息」）。
    """

    profile = _profile()
    declared = _declared(profile)
    filled = {name: "已知" for name in declared}

    validated = validate_plan(
        plan=_plan(profile, requests=[]),
        understanding=_understanding(),
        aggregate=_aggregate(profile, slots=filled),
        profile=profile,
    )

    assert validated.strategy_id == STRATEGY


def test_a_blank_string_counts_as_empty() -> None:
    """空串与纯空白算「空着」—— 与「有值」在别处的判据一致。

    槽位值是模型写回的散文，`""` 与 `"  "` 都表示它没拿到；
    只判 `is None` 会让一次写回空串变成「填过了」，而那一轮的报价没有依据。
    """

    profile = _profile()
    declared = _declared(profile)

    with pytest.raises(PlanRejected, match="STRATEGY_SKIPS_REQUIRED_SLOT"):
        validate_plan(
            plan=_plan(profile, requests=[]),
            understanding=_understanding(),
            aggregate=_aggregate(profile, slots={name: "   " for name in declared}),
            profile=profile,
        )


def test_a_strategy_declaring_none_is_untouched() -> None:
    """没声明必填槽位的打法完全不受这道闸影响。

    这一条守**作用面**：一个读错了数据源的实现（比如去读帧的
    `information_slots[].required`）会让这条红 ——
    `rental` 帧的必填槽位在这个聚合里全是空的，而 `acknowledge` 一个都没声明。
    """

    profile = _profile()
    frame = next(f for f in profile["target_frames"] if f["id"] == FRAME)
    strategy = next(
        name
        for name in frame["allowed_strategy_set"]
        if not profile["strategies"].get(name, {}).get("required_slots")
    )

    validated = validate_plan(
        plan=_plan(profile, requests=[], strategy=strategy),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert validated.strategy_id == strategy


def test_the_fallback_is_not_subject_to_this_gate() -> None:
    """被拒之后安全阀接得住 —— **而安全阀自己不受这道闸管**。

    实现上靠的是本闸只在 `origin != "safe_fallback"` 那一支跑 ——
    这一条把那件事从注释变成断言。

    > ⚠️ **2026-09-13（T339 第一批）：这一条原来的理由已经不成立了，措辞跟着改。**
    >
    > 原文写的是「回退计划的 `required_slot_requests` **恒为 `[]`**，
    > 所以它一旦被这道闸管上就会**永远被拒**」。
    > **接线那一批之后回退会点名闸二报出来的那些空槽位**，
    > 于是 `asked ∩ empty` 不再为空 —— **它现在即使跑这道闸也能过。**
    >
    > **所以豁免的理由变了，而豁免本身仍然必要**：
    > `_validate_model_plan_against_profile` 里还有别的检查
    > （如 `SLOT_NOT_IN_FRAME`），它们对一份平台自造的确定性安全阀
    > 同样不该适用。**「它会永远被拒」这个论证不能再用**，
    > 因为它今天是假的 —— 留着它比没有它更糟。
    >
    > 回退现在点名什么、以及「点了名也不是它过闸的原因」，
    > 见 `test_the_fallback_names_the_missing_slot.py`。

    **不改 `safe_fallback_plan` 的选择语义**（改默认是 T339 第二批）：
    这里只验它现有形状与本闸相容。
    """

    profile = _profile()

    validated, rejection = plan_or_fallback(
        plan=_plan(profile, requests=[]),
        understanding=_understanding(),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert rejection is not None
    assert rejection.reason_code == "STRATEGY_SKIPS_REQUIRED_SLOT"
    assert validated.origin == "safe_fallback"
    # **不再断言它恒空**（T339 第一批之后它点名那些空槽位）——
    # 而本条要钉的是「安全阀接住了」，那一点与它点名什么无关。
    # **本批收紧成新行为**：点得出槽位名的拒绝现在选 `ask_slot`（T339 第二批）。
    # 第一批为兼容把这里写宽成一个集合，而**写宽的断言钉不住行为** ——
    # 现在它是一个具体值。
    assert validated.document["selected_strategy_id"] == "ask_slot"
