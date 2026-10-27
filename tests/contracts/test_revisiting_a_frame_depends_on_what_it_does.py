"""他再问起这个帧管的事，怎么办**取决于这个帧的功能** —— **模型的 `push` 也拒**（ADR-0146）。

## 在此之前模型的 `push` 没有任何重复限制

`inference_validation._validate_stack_operation` 的 `push` 分支只检查三件：
不是平台帧、帧定义存在、栈没满。**没有一条问过「这个帧进过没有」。**

于是一个「报价」帧可以被推进去第二次、第三次，
而挡着它的只有 `goal` 里的一句话 ——
**运维 2026-09-20 §八 实测过同一段话术一小时被逐字引 3 次**。
那一批用 `already_sent` 挡住了逐字重发，**而没有挡住「再进一次那个帧」**。

## ⚠️ 两处闸，一个判据

验证器（`FRAME_ALREADY_ENTERED`）与状态机
（`target_stack.apply_target_stack_operation`）各有一份。
**N1 先例：两处或都不做** —— 验证器先跑，所以只住在状态机里的规则会
**穿过重试预算**，而不是落在 no-send 路径上带一个 reason code。

判据逐字相同：`definition_id` 在目标栈里出现过，**含已终结的帧**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected, validate_plan
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.target_stack import (
    TargetStackTransitionError,
    advance_into_frame,
    apply_target_stack_operation,
    frame_to_advance_into,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
WHEN = "2026-09-21T10:00:00+00:00"


def _profile() -> dict[str, Any]:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def _frame(definition_id: str, status: str, index: int) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-8000-00000000000{index}",
        "definition_id": definition_id,
        "goal": "…",
        "status": status,
        "slots": {},
        "slot_evidence": {},
        "entered_at": WHEN,
        "last_progress_at": WHEN,
        "parent_frame_id": None,
        "interruption_reason": None,
        "resume_confirmation_policy": "optional",
        "frame_generation": 1,
    }


def _stack_that_has_been_in(definition_id: str) -> dict[str, Any]:
    """主线活着，而 `definition_id` 那一帧已经进过、完成过、**并且被弹掉了**。

    ⚠️ **夹具的形状就是这一批最贵的那个教训。** 第一版把那个帧留在栈里
    （`completed`），据此以为「进过」读栈就够 —— **而真实路径不是那样**：

    ```
    complete      introduce(repair_pending)              > quote(completed)
    repair        introduce(resume_confirmation_pending) > quote(completed)
    request_pop   introduce(active)      ← quote 从栈里**消失了**
    ```

    所以一个**正常完成并返回**的帧在栈里一点痕迹都不留，
    而它恰恰是最该被拦住的那一个。历史存在 `frames_entered` 上。
    """

    return {
        "generation": 1,
        "frames_entered": ["introduce", definition_id],
        "target_stack": [_frame("introduce", "active", 0)],
    }


def test_a_brief_answer_frame_is_not_entered_twice() -> None:
    """状态机这一侧：`trial_close` 声明了 `brief_answer`，第二次 `push` 直接拒。

    ⚠️ **2026-09-22（ADR-0150）这里从 `quote` 换成了 `trial_close`**：
    用户裁定报价帧要能反复进（「**有些销售场景需要反复报价**」），
    于是在架那份把 `quote` 改成了 `reenter`。
    **换的是举例的那个帧，不是这条规则** —— 规则仍然是
    「声明了 `brief_answer` 的帧不许进第二次」。
    """

    with pytest.raises(TargetStackTransitionError, match="already been entered"):
        apply_target_stack_operation(
            aggregate=_stack_that_has_been_in("trial_close"),
            operation={"op": "push", "target_frame_definition_id": "trial_close"},
            interruption_relation="side_question",
            profile=_profile(),
            occurred_at=WHEN,
        )


def test_a_reenter_frame_can_be_entered_again() -> None:
    """⚠️ **对照条，而它比上面那条更要紧。**

    没有它，一个「一律拒第二次」的实现会让上面那条绿 ——
    **而异议帧必须能再进**：他可能提好几条疑虑，每一条都要答。
    用户 2026-09-21：「客人绝大多数情况就像一个有可能随时提问的听众」。
    """

    updated = apply_target_stack_operation(
        aggregate=_stack_that_has_been_in("objection"),
        operation={"op": "push", "target_frame_definition_id": "objection"},
        interruption_relation="side_question",
        profile=_profile(),
        occurred_at=WHEN,
    )
    # 上一次那个异议帧已经被 `request_pop` 弹掉了，所以栈里只会有新的这一个。
    entered = [f for f in updated["target_stack"] if f["definition_id"] == "objection"]
    assert len(entered) == 1, "异议帧进不去第二次了 —— 他提第二条疑虑就没人答"
    assert entered[0]["status"] == "active"
    # **而历史记了两次** —— 它只增不删，那正是它与栈的区别。
    assert updated["frames_entered"].count("objection") == 2, (
        f"历史没记上第二次：{updated['frames_entered']}"
    )


def test_the_shipped_package_says_what_each_frame_does_on_revisit() -> None:
    """在架那份逐字：**只有异议帧可重入。**

    这一条钉的是数与名字：改了要把它改过来，
    而**全部改成 `repeatable` 会让上面第一条红**，
    全部改成 `once` 会让第二条红 —— 两个方向都有东西接着。
    """

    modes = {f["id"]: f.get("revisit") for f in _profile()["target_frames"]}
    assert modes == {
        "introduce": "brief_answer",
        # ⚠️ **2026-09-22（ADR-0150）：`brief_answer` → `reenter`。**
        # 用户裁定：「**比如有些销售场景需要反复报价**」。
        # 这一格改动之后，这个帧里 `price_quoted` 也跟着声明了
        # `on_revisit: "carry"` —— **两格是一对**：可以再进来，
        # 而再进来时「已经报过的那个数」还算数，不用重报一遍。
        "quote": "reenter",
        "objection": "reenter",
        "trial_close": "brief_answer",
        "closing": "brief_answer",
    }, f"在架的可重入声明变了：{modes}"


def test_a_reenter_target_is_advanced_into_once_and_not_twice() -> None:
    """⚠️ **这一条替换了「`advance_when` 的目标帧必须是 `brief_answer`」。**

    原来那条是**对配包文本的断言**：目标帧声明成 `reenter` 就红。
    它挡的是一个不会停的循环 —— 平台推进的条件会**持续满足**
    （`quote` 完成 pop 回来之后「报过价」依然为真），
    可重进 + 条件持续满足 = 每一轮推一次、每一轮给客户发一条。

    **而那个循环有第二道闸，且那一道才是真正在挡的**：
    `advance_into_frame` 自己在结尾调 `_record_entry`，
    于是 `target in ever_entered` 从第二轮起恒为真。
    原来那条因此是多出来的一层，**而它的代价是「反复报价」配不出来**
    （`continue` 掉整条推进规则，配包照样封得出、没有任何东西变红）。
    ADR-0150 去掉了它。

    **所以这一条换成跑一遍**：`quote` 今天是 `reenter`，
    平台推进它**恰好一次**，第二次问回 `None`。
    ⚠️ **断言「第二次是 `None`」比断言配包文本强**：
    把 `_record_entry` 从 `advance_into_frame` 里拿掉，
    原来那条纹丝不动，这一条当场红。
    """

    profile = _profile()
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    # `introduce` 的推进条件：那一段话术真的发出去过。
    delivered = ["material:demo-bridge-to-capability:v1"]

    first = frame_to_advance_into(aggregate=aggregate, profile=profile, delivered=delivered)
    assert first == "quote", f"平台没有推进到报价帧：{first!r}"

    aggregate = advance_into_frame(
        aggregate=aggregate, profile=profile, definition_id=first, occurred_at=WHEN
    )
    # 报完价回到主线 —— 完整的那条返回路径（`complete` 不弹栈）。
    for operation in ({"op": "complete"}, {"op": "repair"}, {"op": "request_pop"}):
        aggregate = apply_target_stack_operation(
            aggregate=aggregate,
            operation=operation,
            interruption_relation="same_goal",
            profile=profile,
            occurred_at=WHEN,
        )

    # 推进条件此刻**依然满足**（那段话术还是发出去过的），而：
    again = frame_to_advance_into(aggregate=aggregate, profile=profile, delivered=delivered)
    assert again is None, (
        f"平台又要推一次 {again!r} —— 每一轮给客户重报一次价，那是不会停的循环"
    )


def test_the_validator_and_the_transition_agree() -> None:
    """⚠️ **两处闸，一个判据** —— 而这一条钉的是它们不许分家。

    验证器先跑：只住在状态机里的规则会**穿过重试预算**，
    而不是落在 no-send 路径上带一个 reason code（N1 先例）。
    """

    # 用仓里现成的那套夹具，不自己造一份 plan —— 自造夹具藏得住形状不匹配。
    from return_path_builders import plan_for, understanding_with  # noqa: PLC0415

    profile = _profile()
    plan = plan_for("push", profile=profile)
    plan["target_stack_operation"]["target_frame_definition_id"] = "trial_close"
    plan["selected_strategy_id"] = "wait"

    with pytest.raises(PlanRejected) as rejected:
        validate_plan(
            plan=plan,
            understanding=understanding_with(relation="side_question"),
            aggregate=_stack_that_has_been_in("trial_close"),
            profile=profile,
        )
    assert rejected.value.reason_code == "FRAME_ALREADY_ENTERED", (
        f"验证器给的是 {rejected.value.reason_code!r} —— "
        "状态机会拒它，而验证器放过去就意味着它穿过重试预算才被拦"
    )
