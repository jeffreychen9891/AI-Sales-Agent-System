"""`frames_entered` 是只增的，而**长到多长由客户决定** —— 那是一场他能自己触发的故障。

## 量出来的那一版（2026-09-22，`main` `a82f4332`）

`_record_entry` 当时是裸的 `history.append(...)`，
而 `conversation-aggregate.schema.json` 那边写着 `maxItems: 64`。
拿生产的 `apply_target_stack_operation` + `validate_contract` 跑异议帧往返：

```
第62次异议往返 -> frames_entered=64 契约=PASS
第63次异议往返 -> frames_entered=65 契约=[...] is too long
```

`validate_contract` 在 prepare 的**写路径**上。那通对话从第 65 次进帧起
**每一轮都炸，而且退不回来** —— 那个数组当时声明「永不删除」，
没有任何东西能把它缩短；商家改配包也救不了，数组在聚合上不在配包上。

## ⚠️ 这条与「配包写大了」那一类不同，因为**触发它的是客户**

`objection` 在演示包里是 `reenter`，他提 63 次疑虑就到了。
商家改不了、运维改不了，只能等它炸。
（2026-09-22 那场 `goal` 超 `maxLength` 的故障是商家配包撑爆的，改包就能救。）

契约那段描述原先写着「一通对话进过 64 个帧，那已经不是『聊回旧话题』」——
**那句是错的**：`reenter` 恰恰就是聊回旧话题，并且它计数。
它写在 `revisit` 落地之前，而 `revisit` 落地时没有人回来改它。

## 这一批怎么修的

到顶时按 `_index_to_forget` 丢掉**最旧的那笔重复记录**：
三个读者问的都是「进过没有」，丢掉一笔别处还有的记录不改变任何一个答案。
**丢的是「第几次」那一层精度，保的是「进过没有」。**

而「总能找到一笔重复的」靠配包那格 `target_frames.maxItems: 63` 撑着：
`63 = 64 - len(PLATFORM_FRAME_DEFINITIONS)`，下面第二条按这条算式核。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.worker.inference_validation import PlanRejected, validate_plan
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.platform_frames import PLATFORM_FRAME_DEFINITIONS
from fastagent_v2.worker.target_stack import (
    FRAMES_ENTERED_CAP,
    TargetStackTransitionError,
    apply_target_stack_operation,
    frames_ever_entered,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPOSITORY_ROOT / "packages" / "contracts" / "schemas"
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
WHEN = "2026-09-22T10:00:00+00:00"

#: 进一个子帧、办完、回到主线之后的三步 —— 生产的那条返回路径。
#: 实测 2026-09-21：`complete` **不弹栈**，真正弹栈的是 `request_pop`。
BACK_TO_THE_MAIN_LINE = (
    ({"op": "complete"}, "same_goal"),
    ({"op": "repair"}, "same_goal"),
    ({"op": "request_pop"}, "same_goal"),
)


def _profile() -> dict[str, Any]:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _seeded() -> dict[str, Any]:
    """起点用 pipeline 自己的根构造器 —— 手搭的聚合过不了契约，而那正是本条要核的东西。"""

    return _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=_profile(),
    )


def _visit(
    aggregate: dict[str, Any], definition_id: str, *, profile: dict[str, Any]
) -> dict[str, Any]:
    """进一次 `definition_id` 那个帧，办完，回到主线 —— 完整的四步。"""

    steps = (
        ({"op": "push", "target_frame_definition_id": definition_id}, "side_question"),
        *BACK_TO_THE_MAIN_LINE,
    )
    for operation, relation in steps:
        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=operation,
            interruption_relation=relation,
            profile=profile,
            occurred_at=WHEN,
        )
    return aggregate


def _objections(count: int, *, after_trial_close: bool = False) -> dict[str, Any]:
    """让客户提 `count` 条疑虑，每条都走完整的四步往返。

    `after_trial_close` 先走一次推成交 —— 那是一个 `brief_answer` 帧，
    **进过一次之后就不该再进**，用来核超界之后那道闸还认不认得它。

    ⚠️ **2026-09-22（ADR-0150）这里从 `quote` 换成了 `trial_close`**：
    用户裁定报价帧要能反复进，于是在架那份把 `quote` 改成了 `reenter`，
    它不再能当「只进一次」的例子。**换的是例子，不是判据。**
    """

    profile = _profile()
    aggregate = _seeded()
    if after_trial_close:
        aggregate = _visit(aggregate, "trial_close", profile=profile)
    for _ in range(count):
        aggregate = _visit(aggregate, "objection", profile=profile)
    return aggregate


def test_the_cap_is_one_number() -> None:
    """写入侧那个常量与契约那格 `maxItems` 必须是同一个数。

    **两个数才是这条缺陷的形状**：契约那边一直写着 64，
    而写入侧没有任何人执行它 —— 于是第 65 笔写进去、校验器拒、对话报废。
    """

    declared = _schema("conversation-aggregate")["properties"]["frames_entered"]["maxItems"]
    assert declared == FRAMES_ENTERED_CAP, (
        f"契约写 {declared}，而 target_stack.FRAMES_ENTERED_CAP 是 "
        f"{FRAMES_ENTERED_CAP} —— 写入侧执行的上界与校验器拒的上界不是一个数"
    )


def test_the_package_bound_leaves_room_for_the_platforms_own_frames() -> None:
    """配包能声明几个帧 = 表的上界 − 平台自己的帧数。

    ⚠️ **这一条是「总丢得掉一笔重复的」那个前提**。
    配包能声明的帧比表的上界还多时，表里可以坐满 64 个**互不相同**的帧，
    这时再进一个新帧，`_index_to_forget` 找不到重复的，
    只能丢掉一个唯一的 —— 而那正是 `FRAME_ALREADY_ENTERED`
    与平台推进防循环两道闸读的东西。

    **新加一个平台帧而不下调配包那格，这条就红。**
    """

    room = FRAMES_ENTERED_CAP - len(PLATFORM_FRAME_DEFINITIONS)
    declared = _schema("merchant-profile")["properties"]["target_frames"]["maxItems"]
    assert declared == room, (
        f"配包最多 {declared} 个帧，而表里只留得下 {room} 个商家帧"
        f"（上界 {FRAMES_ENTERED_CAP} 减掉 {len(PLATFORM_FRAME_DEFINITIONS)} 个平台帧）"
    )


@pytest.mark.parametrize("rounds", [62, 63, 64, 120])
def test_a_talkative_customer_cannot_break_the_conversation(rounds: int) -> None:
    """客户提多少条疑虑，聚合都还写得进去。

    `63` 是原来炸的那一次（表长 65）；`62` 是它前一次，**原来就是绿的**，
    留在这里是为了让「这条用例本来就过不了 63」与「它根本没跑到 63」
    区分得开。`120` 是远超之后。
    """

    aggregate = _objections(rounds)
    assert len(aggregate["frames_entered"]) <= FRAMES_ENTERED_CAP
    validate_contract("conversation-aggregate", aggregate)


def test_overflow_forgets_the_count_and_not_the_visit() -> None:
    """超界丢掉的必须是重复的那几笔，**进过哪些帧一个都不能少**。"""

    aggregate = _objections(120)
    entered = frames_ever_entered(aggregate)
    assert "introduce" in entered, f"最早那个帧被挤掉了：{entered}"
    assert "objection" in entered
    # 精度确实降了 —— 记了 120 次，而表里留不下 120 笔。这是被接受的代价。
    assert entered.count("objection") < 120


def test_a_brief_answer_frame_is_still_refused_after_overflow() -> None:
    """超界之后 `FRAME_ALREADY_ENTERED` 还认得那个只进一次的帧。

    ⚠️ **这一条才是「丢掉一笔重复记录」与「丢掉最旧一笔」的分界**。
    丢最旧的那种写法，上面两条照样绿 —— 推成交那一笔排在所有疑虑前面，
    而**推成交帧是 `brief_answer`**：它被挤掉之后这道闸就放行了，
    于是客户在第 120 条疑虑之后被**重新推一次成交**。

    ⚠️ 核的是**报价**不是自我介绍：`introduce` 是栈底那个活着的主线帧，
    往它上面 `push` 先撞上 `PUSH_ONTO_SAME_FRAME`，
    **那条闸与本条要核的东西无关**，绿了也说明不了记录还在。
    """

    profile = _profile()
    aggregate = _objections(120, after_trial_close=True)
    assert "trial_close" in frames_ever_entered(aggregate)

    with pytest.raises(TargetStackTransitionError, match="already been entered"):
        apply_target_stack_operation(
            aggregate=aggregate,
            operation={"op": "push", "target_frame_definition_id": "trial_close"},
            interruption_relation="side_question",
            profile=profile,
            occurred_at=WHEN,
        )

    # 验证器那一侧同一个判据（N1 先例：两处或都不做）。
    # 用仓里现成的那套夹具，不自己造一份 plan —— 自造夹具藏得住形状不匹配。
    from return_path_builders import plan_for, understanding_with  # noqa: PLC0415

    plan = plan_for("push", profile=profile)
    plan["target_stack_operation"]["target_frame_definition_id"] = "trial_close"
    plan["selected_strategy_id"] = "wait"

    with pytest.raises(PlanRejected) as rejected:
        validate_plan(
            plan=plan,
            understanding=understanding_with(relation="side_question"),
            aggregate=aggregate,
            profile=profile,
        )
    assert rejected.value.reason_code == "FRAME_ALREADY_ENTERED", (
        f"验证器给的是 {rejected.value.reason_code!r} —— "
        "状态机会拒它，而验证器放过去就意味着它穿过重试预算才被拦"
    )


def test_the_shipped_packages_fit_under_the_bound() -> None:
    """在架的三个包离上界还很远 —— 这一格今天不拒任何存量包。

    数是现读的：改包改到超过 63 个帧，这条会红并指着上面那条算式。
    """

    bound = _schema("merchant-profile")["properties"]["target_frames"]["maxItems"]
    counted = {
        path.parent.name: len(json.loads(path.read_text(encoding="utf-8"))["target_frames"])
        for path in sorted((REPOSITORY_ROOT / "profiles").glob("*/profile.json"))
    }
    assert counted == {
        "internal-demo-sales": 5,
        "synthetic-carrier": 3,
        "synthetic-third": 3,
    }, f"在架配包的帧数变了：{counted}"
    assert max(counted.values()) <= bound
