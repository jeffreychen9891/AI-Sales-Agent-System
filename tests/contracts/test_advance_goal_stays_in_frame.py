"""`advance_goal` 不许换目标帧 —— 闸一（ADR-0137 裁定一）。

## 在这条闸之前，图纸那句话没有执行点

RFC §4.1（ADR-0125 §七 明确「`advance_goal` 的边界一个字未改」）：

> 它不能切换目标、跳过必填槽位、构造未登记承诺或直接发送。

**四件里只有「直接发送」有执行点。** 只读对比实测（2026-09-13）：
`advance_goal` 在生产代码里只有三处 —— 词表（`strategies.py`）、
提示词示例（`inference_model.py`）、一句注释（`platform_frames.py`）；
而 `_validate_stack_operation` **只看 `op`，从不看 strategy**，
于是 `advance_goal` + `push` 一路放行。
那是本仓「五条判据」第 1 类（约束声明了，运行期无人执行）的一个标本。

## 它当天就抓到一处仓内的反例

`test_synthetic_carrier_executability.py` 的脚手架把 `complete` 映到
`advance_goal`。**那张表断言的正是图纸禁止的事** —— 而在闸建起来之前没人会红。
已改成按帧的 `allowed_strategy_set` 挑（见那份文件的注释）。

**这一条是「一条约束落地时，先看它抓到了谁」的实例**：
新闸的第一批红里，有一条是仓里自己写错的。

## 反向探针实测（2026-09-13）

命令一律是（私有 detached 检出里，范围**整个 contracts 目录**）：

    python -m pytest tests/contracts/ -q --no-header -p no:cacheprovider

| 变异 | 红了多少 |
|---|---|
| 整条闸删掉 | 6 |
| 闸只认 `push` | 4（另外四个 op） |
| `IN_FRAME_ONLY_STRATEGIES` 清空（闸还在，认不出策略） | 6 |
| **闸改成「换帧一律拒」（不看策略）** | **86 条 / 27 个用例函数 / 6 个文件** |
| 回放脚手架退回 `complete → advance_goal` | 1 |

五支全红，且每支跑前 `compile()` 过被改的文件（语法坏掉的变异会红一片，
那种红证明不了事）。**没有一支是绿的**，所以这一批没有「独守还是漏了」要判的。

### 那第四支是这五支里最有说服力的一支

红的分布：

    test_exit_signal_refuses_resume.py       59
    test_inference_contracts.py               9
    test_repair_resume_confirmation_policy.py 8
    test_advance_goal_stays_in_frame.py       6
    test_platform_no_goal_frame.py            3
    test_synthetic_carrier_executability.py   1

**一条不看策略的 `op != "keep"` 把整个退出信号族毁了** —— 59 条里 48 条来自一个
参数化函数（`test_exit_signal_refuses_resume.py::
test_the_validator_and_the_transition_never_disagree`），
它守的是「校验器与转换层永不打架」，而那个变异让它们**在 48 种组合上同时打架**。

**两个口径都给，因为它们说明不同的事**：86 是「多少个断言被打破」，
27 是「多少个**不同的检查**被打破」。只看 86 会把广度读高
（其中 48 是同一个函数的参数化），只看 27 会把严重性读低。

> **【更正 · 2026-09-13】那一笔的提交说明里，这一格写的是 `16`。**
>
> **那一笔在 `main` 上的号是 `d3de5078`。** 它在分支上一度是 `03487c37` ——
> 两者**内容逐字节相同，只有 SHA 不同**，成因是一次**被撤回的 `reset --soft`**
> 与合并时序重叠：为改这一格的数，我改写了那两笔的说明
> （`03487c37` → `d3de5078`）；随后被告知该号已被引用、更正应当另起一笔，
> 于是把分支指回原号；**而在那之前，合并窗口已经把改写后的那一对合进了 `main`**。
> 于是同一批改动在两处各有一份 SHA，最后按 `main` 上那一份为准（本批 rebase 丢掉原号）。
>
> **这句映射不能省**：没有它，这条更正就是一条指向 `main` 上不存在的坐标的引用 ——
> 而那正是 `check_adr_references.py` 那一族守的形状
> （`AGENTS.md`「一个指向不存在决定的引用比不引用更糟」）。
> 成因与教训写在 `AGENTS.md` Git 约定：**改写历史这类不可逆动作，先问再做。**
> 原文保留在这里：「| 闸改成「换帧一律拒」（不看策略） | **16** —— 一条过宽的闸
> 会毁掉多少正常转换 |」。**那个数低估了五倍**，而那一行整句就是
> 「过宽有多严重」的唯一量化 —— 16 把「整个退出信号族被毁」这件事藏住了。
>
> **错的不是数，是产生它的命令不覆盖它声称的范围，而说明里没写命令**：
> 初版只跑了三个文件（新用例文件 + `test_inference_contracts` +
> `test_synthetic_carrier_executability` = 6 + 9 + 1 = 16），
> 漏掉的三个文件里 `test_exit_signal_refuses_resume.py` 一个就 59 条。
> 从此每支探针的数都带上命令与范围。
>
> **只有那一支要改。** 另外四支（6 / 4 / 6 / 1）由复核窗口按全量口径逐支复现，
> 与初版**逐位相同** —— 初版那三个文件恰好覆盖了它们全部的红。
> **读到这段历史的人不必怀疑整批的数。**
>
> **另一处口径差**：用例函数数我第一次数出 26，正确是 **27** ——
> `test_the_validator_and_the_transition_never_disagree` 这个名字
> **在两个文件里各有一个**（59 那份 ×48、8 那份 ×4），按函数名去重会把它们合成一个。
> **数函数要按「文件 + 函数」，不能只按函数名。**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected, plan_or_fallback, validate_plan
from fastagent_v2.worker.strategies import (
    IN_FRAME_ONLY_STRATEGIES,
    TARGET_STACK_OPERATIONS,
)

ROOT = Path(__file__).resolve().parents[2]

_EVIDENCE = {
    "provider": "test",
    "model": "test",
    "prompt_release": "test",
    "latency_ms": 0,
    "token_usage": {"input": 0, "output": 0},
}

#: 换帧的每一种写法。ADR-0137 裁定一逐个点了名，所以这里也逐个跑。
LEAVING_OPS = tuple(op for op in TARGET_STACK_OPERATIONS if op != "keep")

#: `push` 与 `abandon` 各自只在校验器认得的中断上合法；其余操作用 `same_goal` 就够。
#:
#: **`abandon` 的值第一版写的是 `explicit_opt_out`，那是错的** ——
#: 它是 profile 里 `exit_signals` 的取值，而 `interruption.relation` 的枚举
#: 在 `message-envelope` 那一族契约里是
#: `same_goal / side_question / new_goal / correction / exit / human_request`。
#: 两个词表都叫「退出」，而**只有一个是这个字段的**：
#: 第一版红在合同层（`jsonschema` 报 enum 不匹配），而那条红证明不了闸一的任何事。
RELATION_FOR = {"push": "side_question", "abandon": "exit"}


def _profile() -> dict[str, Any]:
    return json.loads(
        # **目录名与 `profile_id` 不同**：这份包的 id 是 `synthetic-two-axis-desk`，
        # 而它住在 `profiles/synthetic-carrier/`。第一版按 id 拼路径，
        # 十三条用例一起 `FileNotFoundError` —— 报的是「文件不在」，
        # 而真正的事实是「id 不是目录名」。
        (ROOT / "profiles" / "synthetic-carrier" / "profile.json").read_text(encoding="utf-8")
    )


def _aggregate(profile: dict[str, Any], *, depth: int = 1) -> dict[str, Any]:
    """栈上 `depth` 个帧，最上面那个是 `rental`（它允许 `advance_goal`）。

    `depth=2` 是为 `repair` / `request_pop` 准备的：那两个操作要求栈上有父帧，
    而本文件要验的是**闸一先说话** —— 所以不能让「没有父帧」抢在它前面报错。
    """

    definition = next(f for f in profile["target_frames"] if f["id"] == "rental")
    frame = {
        "id": str(uuid4()),
        "definition_id": definition["id"],
        "goal": definition["goal"],
        "status": "active",
        "slots": {},
        "slot_evidence": {},
        "entered_at": "2026-09-13T00:00:00Z",
        "last_progress_at": "2026-09-13T00:00:00Z",
        "parent_frame_id": None,
        "interruption_reason": None,
        "resume_confirmation_policy": "optional",
        "frame_generation": 1,
    }
    stack = [json.loads(json.dumps(frame)) for _ in range(depth)]
    for index, item in enumerate(stack):
        item["id"] = str(uuid4())
        if index:
            item["parent_frame_id"] = stack[index - 1]["id"]
    return {"generation": 1, "target_stack": stack}


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
        "model_evidence": _EVIDENCE,
    }


def _plan(
    *, strategy: str, op: str, target: str | None, profile: dict[str, Any]
) -> dict[str, Any]:
    """一份合法的模型计划。

    **`generation_constraints` 取自这条策略自己声明的那些**，不写死 ——
    第一版写死了 `["current_goal_only"]`（`advance_goal` 声明的那一个），
    于是「别的打法照旧能换帧」那五条全红在
    `GENERATION_CONSTRAINT_NOT_DECLARED` 上：**红的是另一道闸**，
    而它们本该证明闸一的作用面。
    """

    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": op, "target_frame_definition_id": target},
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
        "candidate_intents": [],
        "generation_constraints": list(
            profile["strategies"][strategy].get("generation_constraints", [])
        ),
        "manual_review_reason": None,
        "model_evidence": _EVIDENCE,
    }


@pytest.mark.parametrize("op", LEAVING_OPS)
def test_advance_goal_with_any_leaving_op_is_refused(op: str) -> None:
    """**换帧的五种写法逐个跑，五个都要被拒，而且理由码是闸一的那个。**

    逐个跑而不是只跑 `push`：ADR-0137 裁定一把这五个**逐个点了名**
    （`push` / `repair` / `request_pop` / `complete` / `abandon`），
    而「只验最显然的那一个」正是本仓「一个样本不是一个结论」防的事 ——
    一个 `op != "keep"` 写成 `op == "push"` 的实现能通过只验 push 的用例。

    **理由码必须是 `ADVANCE_GOAL_MUST_STAY_IN_FRAME`，不能是别的**：
    `repair` / `request_pop` 在栈只有一个帧时会撞 `STACK_OP_REQUIRES_PARENT`，
    而那条红**不证明闸一存在**。所以夹具给了两层栈，让通用错没有机会先说话。
    """

    profile = _profile()
    target = "consultation" if op == "push" else None

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=_plan(strategy="advance_goal", op=op, target=target, profile=profile),
            understanding=_understanding(RELATION_FOR.get(op, "same_goal")),
            aggregate=_aggregate(profile, depth=2),
            profile=profile,
        )

    assert refused.value.reason_code == "ADVANCE_GOAL_MUST_STAY_IN_FRAME", (
        f"op {op!r} 被拒了，但理由码是 {refused.value.reason_code!r} —— "
        "那条红证明的是别的闸，不是闸一"
    )


def test_advance_goal_with_keep_is_allowed() -> None:
    """**反面：在当前帧内推进，照常放行。**

    没有这一条，一个「`advance_goal` 一律拒」的实现也能让上面五条全绿 ——
    而那会让这条打法彻底不可用，与图纸说的「根据未完成槽位选择下一要点」相反。
    """

    profile = _profile()

    validated = validate_plan(
        plan=_plan(strategy="advance_goal", op="keep", target=None, profile=profile),
        understanding=_understanding("same_goal"),
        aggregate=_aggregate(profile),
        profile=profile,
    )

    assert validated.strategy_id == "advance_goal"


@pytest.mark.parametrize("op", LEAVING_OPS)
def test_this_gate_never_fires_for_another_strategy(op: str) -> None:
    """**闸一的作用面：换帧的五种写法配一条别的打法，永远不会撞到闸一。**

    断言的是「**不是这条理由码**」，而不是「一路放行」—— 这不是退让，是准确：
    五种操作各有自己的前置（`complete` 要槽位填齐、`repair` 要父帧
    `repair_pending`、`request_pop` 要父帧在位），
    而**那些前置不是本文件要验的东西**。要让它们全部放行，得替每一个 op
    造一套合法状态，而那样这条用例就变成在验那三道闸，
    **一旦它们的前置改了，这里会红在与闸一无关的地方**。

    与上面那条配成一对，两者合起来把作用面钉死：
    * `advance_goal` + 五种换帧写法 → **一定**是 `ADVANCE_GOAL_MUST_STAY_IN_FRAME`；
    * 别的打法 + 同样五种 → **一定不是**它。

    一个写成「换帧一律拒」的实现会让这五条红；一个写成
    `op == "push"` 的实现会让上面那条的另外四个参数红。两种偏移都接得住。
    """

    profile = _profile()
    frame = next(f for f in profile["target_frames"] if f["id"] == "rental")
    strategy = next(
        name
        for name in frame["allowed_strategy_set"]
        if name not in IN_FRAME_ONLY_STRATEGIES
    )

    try:
        validate_plan(
            plan=_plan(
                strategy=strategy,
                op=op,
                target="consultation" if op == "push" else None,
                profile=profile,
            ),
            understanding=_understanding(RELATION_FOR.get(op, "same_goal")),
            aggregate=_aggregate(profile, depth=2),
            profile=profile,
        )
    except PlanRejected as rejected:
        assert rejected.reason_code != "ADVANCE_GOAL_MUST_STAY_IN_FRAME", (
            f"策略 {strategy!r} 不在 IN_FRAME_ONLY_STRATEGIES 里，"
            f"而闸一对它说话了（op {op!r}）—— 这条闸的作用面漏出去了"
        )


def test_the_gate_reads_the_catalogue_and_not_a_literal() -> None:
    """闸一认的是 `strategies.py` 那张表，不是一个写死的字符串。

    **这是「守卫的守卫」**：把表清空，那五条拒绝就该全部放行 ——
    如果闸里写的是 `== "advance_goal"`，清空表不会改变任何行为，
    而这一条会红并指着那个字面量说「它没读表」。

    表是**策略性质的唯一写入点**（`AGENTS.md`「单一写入点 + 绊线」）：
    哪天图纸给别的打法加同一条边界，改的是那一行。
    """

    import fastagent_v2.worker.inference_validation as module

    profile = _profile()
    plan = _plan(strategy="advance_goal", op="push", target="consultation", profile=profile)
    original = module.IN_FRAME_ONLY_STRATEGIES
    try:
        module.IN_FRAME_ONLY_STRATEGIES = ()
        validate_plan(
            plan=plan,
            understanding=_understanding("side_question"),
            aggregate=_aggregate(profile, depth=2),
            profile=profile,
        )
    finally:
        module.IN_FRAME_ONLY_STRATEGIES = original


def test_a_refused_plan_falls_back_and_the_fallback_itself_passes_the_gate() -> None:
    """被拒之后安全阀接得住，**而安全阀自己也过得了这条闸**。

    闸一**不看 `origin`**，所以它对回退计划同样生效。这一条把「那不会堵死安全阀」
    从注释变成断言：`safe_fallback_plan` 的 `op` 恒为 `keep`，
    `selected_strategy_id` 取自不含 `advance_goal` 的那张表 —— 两条合起来才成立。

    **不改 `safe_fallback_plan` 的语义**（本批范围之外）：这里只验它现有的形状
    与这条闸相容。
    """

    profile = _profile()

    validated, rejection = plan_or_fallback(
        plan=_plan(strategy="advance_goal", op="push", target="consultation", profile=profile),
        understanding=_understanding("side_question"),
        aggregate=_aggregate(profile, depth=2),
        profile=profile,
    )

    assert rejection is not None
    assert rejection.reason_code == "ADVANCE_GOAL_MUST_STAY_IN_FRAME"
    assert validated.origin == "safe_fallback"
    assert validated.document["target_stack_operation"]["op"] == "keep"
    assert validated.strategy_id not in IN_FRAME_ONLY_STRATEGIES
