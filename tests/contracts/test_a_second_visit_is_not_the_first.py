"""第二次进同一个帧，出帧的要求不能和第一次一样（ADR-0150）。

## 量出来的那一版（2026-09-22，`main` `a8678d74`）

`request_pop` 那一句 `stack.pop()` 把帧**连它填过的每一个槽一起**丢掉，
**在此之前没有任何地方归档**：

```
request_pop    [('introduce', 'active', {})]
>>> 第一次那条异议答了什么，平台还留着吗？ **没有了**
```

而散文那一侧接不住 —— `summary_for_prompt` 从**最旧**一端淘汰
（`SUMMARY_PROMPT_BYTES = 2_400`，实测那种长度的行只装得下 27 行）。

## 三类出帧要求，今天曾是三个不同的答案

| 出帧要求 | 二次进入时 | 对不对 |
|---|---|---|
| **发资料** | 不会重发（`already_sent` / `intent_records`）| ✅ 本来就对 |
| **取到的信息**（`quote.price_quoted`）| 要重来 → **再报一次价** | ❌ 本条修的就是它 |
| **这一轮要解决的事**（`objection.concern_answered`）| 要重来 | ✅ 必须重来 |

**后两行行为一样而正确答案相反**，而它俩在配包里长得也一样
（都是 `exit_conditions: [{slot: X, present: true}]`）。
平台不是判错了，是 `information_slots` 里**没有任何一格说
「这个值下次还算不算数」** —— 那一格就是 `on_revisit`。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.worker.inference_validation import PlanRejected, validate_plan
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.platform_frames import NO_GOAL_FRAME_DEFINITION_ID
from fastagent_v2.worker.target_stack import (
    CARRY_ON_REVISIT,
    RESET_ON_REVISIT,
    _slots_that_carry,
    apply_slot_updates,
    apply_target_stack_operation,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY_ROOT / "packages" / "contracts" / "schemas"
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
WHEN = "2026-09-22T10:00:00+00:00"
EVIDENCE = "22222222-2222-4222-8222-222222222222"

#: 进一个子帧、办完、回到主线 —— 实测 2026-09-21：`complete` **不弹栈**，
#: 真正弹栈的是 `request_pop`。
BACK_TO_THE_MAIN_LINE = (
    ({"op": "complete"}, "same_goal"),
    ({"op": "repair"}, "same_goal"),
    ({"op": "request_pop"}, "same_goal"),
)


def _profile() -> dict[str, Any]:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def _seeded(profile: dict[str, Any]) -> dict[str, Any]:
    """起点用 pipeline 自己的根构造器 —— 手搭的聚合过不了契约。"""

    return _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )


def _push(aggregate: dict[str, Any], definition_id: str, *, profile: dict[str, Any]):
    return apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "push", "target_frame_definition_id": definition_id},
        interruption_relation="side_question",
        profile=profile,
        occurred_at=WHEN,
    )


def _fill(aggregate: dict[str, Any], values: dict[str, Any], *, profile: dict[str, Any]):
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "slot": name,
                "value": value,
                "confidence": 0.9,
                "frame_id": aggregate["target_stack"][-1]["id"],
                "evidence_event_id": EVIDENCE,
            }
            for name, value in values.items()
        ],
        recorded_at=WHEN,
    )
    assert writeback.applied == len(values), (
        f"这一步没写进去：applied={writeback.applied}，refusals={writeback.refusals}"
    )
    return writeback.aggregate


def _visit_quote_once(profile: dict[str, Any]) -> dict[str, Any]:
    """完整地进一次报价帧：填上报过的价与他这次的反应，然后回主线。"""

    aggregate = _push(_seeded(profile), "quote", profile=profile)
    aggregate = _fill(
        aggregate,
        {"price_quoted": "按人头 199/月，含全部功能", "price_feedback": "嫌贵"},
        profile=profile,
    )
    for operation, relation in BACK_TO_THE_MAIN_LINE:
        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=operation,
            interruption_relation=relation,
            profile=profile,
            occurred_at=WHEN,
        )
    return aggregate


def test_the_carry_vocabulary_is_one_table() -> None:
    """代码里那两个取值与契约那张词表必须逐字相等。

    **两处词表是本仓反复踩的那一族**：一处加了一个取值，另一处读不认得，
    而读不认得的表现是**当成缺省处理**——安静地不带，没有任何东西变红。
    """

    schema = json.loads((SCHEMAS / "merchant-profile.schema.json").read_text(encoding="utf-8"))
    declared = schema["properties"]["target_frames"]["items"]["properties"][
        "information_slots"
    ]["items"]["properties"]["on_revisit"]["enum"]
    assert set(declared) == {CARRY_ON_REVISIT, RESET_ON_REVISIT}, (
        f"契约那张词表是 {declared}，而代码读的是 "
        f"{sorted({CARRY_ON_REVISIT, RESET_ON_REVISIT})}"
    )


def test_a_carry_slot_survives_the_pop_with_its_evidence() -> None:
    """报过的那个数带得过去 —— **连证据一起**。"""

    profile = _profile()
    aggregate = _visit_quote_once(profile)

    assert aggregate["carried_slots"]["quote"]["slots"] == {
        "price_quoted": "按人头 199/月，含全部功能"
    }, f"归档的不是这一格：{aggregate['carried_slots']}"

    second = _push(aggregate, "quote", profile=profile)
    frame = second["target_stack"][-1]
    assert frame["slots"]["price_quoted"] == "按人头 199/月，含全部功能", (
        f"第二次进报价帧，上次报的那个数没带过来：{frame['slots']}"
    )
    # 证据指向**上一次那条消息** —— 那正是这个事实的出处。
    evidence = frame["slot_evidence"]["price_quoted"]
    assert EVIDENCE in evidence.get("evidence_event_ids", []), (
        f"带过来的值没有证据：{evidence}"
    )


def test_a_reset_slot_does_not_survive() -> None:
    """⚠️ **对照条，而它比上面那条更要紧。**

    没有它，一个「一律全带」的实现会让上面那条绿 —— 而
    `price_feedback`（他**这次**对价格的反应）带过去就是错的：
    第二次问价时他还没表态，而帧上写着「嫌贵」。

    用户 2026-09-22 裁的就是这一格必须是**槽级**的：
    同一个帧里两种槽都有。
    """

    profile = _profile()
    aggregate = _visit_quote_once(profile)

    assert "price_feedback" not in aggregate["carried_slots"]["quote"]["slots"], (
        "他上一次的反应被归档了 —— 那一格必须重置"
    )
    second = _push(aggregate, "quote", profile=profile)
    assert "price_feedback" not in second["target_stack"][-1]["slots"]


def test_a_frame_with_nothing_to_carry_leaves_no_trace() -> None:
    """异议帧一格都没声明 `carry`，所以它根本不进这张表。

    **不是「进去一个空字典」** —— 那会让一通只聊异议的对话
    在聚合里长出一格永远为空的东西。
    """

    profile = _profile()
    aggregate = _push(_seeded(profile), "objection", profile=profile)
    aggregate = _fill(aggregate, {"concern_answered": True}, profile=profile)
    for operation, relation in BACK_TO_THE_MAIN_LINE:
        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=operation,
            interruption_relation=relation,
            profile=profile,
            occurred_at=WHEN,
        )

    assert "objection" not in (aggregate.get("carried_slots") or {})
    second = _push(aggregate, "objection", profile=profile)
    assert second["target_stack"][-1]["slots"] == {}, (
        "第二条疑虑进来时帧上还留着上一条的答案 —— 那条就不会被重新回答"
    )


def test_the_carried_value_satisfies_the_platform_exit_gate() -> None:
    """⚠️ **ADR-0150 §四那条裁定，跑一遍。**

    带过去的值**算数**：`complete` 那道必填槽闸读的是帧上的
    `slots`，于是第二次进报价帧可以直接 `complete` ——
    **不用把整个报价流程重走一遍**。

    对照在下面那半：把 `price_quoted` 从表里拿掉，同一道闸就拒，
    reason code 是 `EXIT_CONDITION_NOT_SATISFIED`。
    **两半都在，这条才说明「是带过去的那个值让它过的」。**
    """

    from return_path_builders import plan_for, understanding_with  # noqa: PLC0415

    profile = _profile()
    aggregate = _push(_visit_quote_once(profile), "quote", profile=profile)

    plan = plan_for("complete", profile=profile)
    plan["selected_strategy_id"] = "wait"
    validate_plan(
        plan=plan,
        understanding=understanding_with(relation="same_goal"),
        aggregate=aggregate,
        profile=profile,
    )

    # 对照：没带过去的话，同一道闸拒。
    without = deepcopy(aggregate)
    without["target_stack"][-1]["slots"] = {}
    with pytest.raises(PlanRejected) as rejected:
        validate_plan(
            plan=plan_for("complete", profile=profile) | {"selected_strategy_id": "wait"},
            understanding=understanding_with(relation="same_goal"),
            aggregate=without,
            profile=profile,
        )
    assert rejected.value.reason_code == "EXIT_CONDITION_NOT_SATISFIED"


def test_changing_the_declaration_takes_effect_without_waiting() -> None:
    """商家把那一格改回 `reset`，**存量归档立刻不再生效**。

    两头都按当时的配包过滤 —— 存的时候滤一遍，种的时候再滤一遍。
    只滤存的那一头，这条会红：归档里还留着值，而没有人再问一次配包。
    """

    profile = _profile()
    aggregate = _visit_quote_once(profile)
    assert aggregate["carried_slots"]["quote"]["slots"]  # 归档确实有东西

    changed = _profile()
    slot = next(
        s
        for f in changed["target_frames"]
        if f["id"] == "quote"
        for s in f["information_slots"]
        if s["name"] == "price_quoted"
    )
    slot["on_revisit"] = RESET_ON_REVISIT

    second = _push(aggregate, "quote", profile=changed)
    assert second["target_stack"][-1]["slots"] == {}, (
        "商家改回 `reset` 了，而上一次的值还是被种了进去"
    )


def test_a_platform_frame_carries_nothing() -> None:
    """平台帧没有配包定义，问它「带哪几个槽」必须是空集，不是抛。

    `_definition` 对平台帧会抛 —— 归档发生在**弹栈路径上**，
    在那里抛出来就是一条正常的对话走不下去。
    """

    assert _slots_that_carry(_profile(), NO_GOAL_FRAME_DEFINITION_ID) == frozenset()
    # 配包换过、旧帧的定义已经不在了 —— 同样是空集，不是抛。
    assert _slots_that_carry(_profile(), "a-frame-this-package-never-had") == frozenset()


def test_the_aggregate_still_validates_with_what_was_carried() -> None:
    """带着归档的聚合过得了契约 —— 它与 `target_stack` 那两格共用 `$ref`。"""

    profile = _profile()
    aggregate = _push(_visit_quote_once(profile), "quote", profile=profile)
    validate_contract("conversation-aggregate", aggregate)


def test_the_shipped_package_declares_the_pair() -> None:
    """在架那份逐字：**报价帧可重进，而它报过的那个数带得过去。**

    ⚠️ **两格是一对**，所以钉在一条里：只改 `revisit` 不改 `on_revisit`，
    客户再问价时会被**重报一遍**；只改 `on_revisit` 不改 `revisit`，
    那个帧根本进不去第二次，归档永远种不回去。
    """

    quote = next(f for f in _profile()["target_frames"] if f["id"] == "quote")
    assert quote["revisit"] == "reenter"
    carried = {
        s["name"] for s in quote["information_slots"] if s.get("on_revisit") == CARRY_ON_REVISIT
    }
    assert carried == {"price_quoted"}, f"报价帧声明带过去的是：{sorted(carried)}"


def test_no_other_shipped_frame_carries_anything_yet() -> None:
    """三个在架配包里，声明了 `carry` 的**只有那一格**。

    这条是数与名字：**缺省 `reset` 等于今天的行为**，本条落地
    不改动任何别的帧。加第二格时这条会红并指着名字说是谁。
    """

    declared = {
        (path.parent.name, frame["id"], slot["name"])
        for path in sorted((REPOSITORY_ROOT / "profiles").glob("*/profile.json"))
        for frame in json.loads(path.read_text(encoding="utf-8"))["target_frames"]
        for slot in frame["information_slots"]
        if slot.get("on_revisit") == CARRY_ON_REVISIT
    }
    assert declared == {("internal-demo-sales", "quote", "price_quoted")}, (
        f"在架配包声明带过去的槽变了：{sorted(declared)}"
    )
