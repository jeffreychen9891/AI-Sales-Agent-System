"""平台自己把对话推进到下一个帧（ADR-0145）。

## 它补的是哪一段

在此之前一通对话只有两种推进：**模型在计划步提一个 `push`**（那是它的判断，
它可能不提），和**根目标完成后平台进 `closing`**（那只在终局）。
中间那一段 ——「两段讲完了，该报价了」—— 没有任何确定性的东西在推。

用户 2026-09-21 逐字：

> 一般销售觉得聊的差不多都会主动报价来促成交易
> 要的不是时间触发而是条件或者情况 事件触发主动

**而「觉得」交给模型就等于交给运气。**

## ⚠️ 这一族的错是安静的

一条 `advance_when` 写错了目标帧、或者防循环那一条被删掉，
**封版过得去、加载过得去、契约过得去** —— 只有真实对话里才看得见：
要么该报价的时候没报，要么同一个帧被无限推进，每一轮都给客户发一条消息。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastagent_v2.worker.target_stack import (
    PLATFORM_ADVANCE_DEPTH_CAP,
    advance_into_frame,
    frame_to_advance_into,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)

WHEN = "2026-09-21T10:00:00+00:00"

#: 在架那条链读的两个 ref —— **平台自己的记录**，不是模型自填的槽。
BRIDGE = "material:demo-bridge-to-capability:v1"
PRICE = "material:demo-price-plain:v1"


def _profile() -> dict[str, Any]:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def _aggregate(frame_id: str, slots: dict[str, Any], *, history: tuple[str, ...] = ()) -> dict:
    """一个栈：先放已经终结的那几帧（历史），再放当前活动帧。"""

    stack = [
        {
            "id": f"00000000-0000-4000-8000-00000000000{index}",
            "definition_id": definition_id,
            "goal": "…",
            "status": "completed",
            "slots": {},
            "slot_evidence": {},
            "entered_at": WHEN,
            "last_progress_at": WHEN,
            "parent_frame_id": None,
            "interruption_reason": None,
            "resume_confirmation_policy": "optional",
            "frame_generation": 1,
        }
        for index, definition_id in enumerate(history)
    ]
    stack.append(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "definition_id": frame_id,
            "goal": "…",
            "status": "active",
            "slots": dict(slots),
            "slot_evidence": {},
            "entered_at": WHEN,
            "last_progress_at": WHEN,
            "parent_frame_id": None,
            "interruption_reason": None,
            "resume_confirmation_policy": "optional",
            "frame_generation": 1,
        }
    )
    return {"generation": 1, "target_stack": stack}


def test_two_sections_told_means_go_and_quote() -> None:
    """**两段讲完，平台自己进报价帧** —— 不等他问，也不靠模型记得提 `push`。"""

    advancing = frame_to_advance_into(
        aggregate=_aggregate("introduce", {}),
        profile=_profile(),
        # ⚠️ **平台自己的记录，不是模型自填的槽**（ADR-0146）：
        # 这一份来自 `intent_records` 里真的发出去过的那些。
        delivered=(BRIDGE,),
    )
    assert advancing == "quote", f"两段都讲完了而平台没推进：{advancing!r}"


def test_nothing_told_yet_means_stay() -> None:
    """**条件没到就不推。** 对照条 —— 没有它，一个「一律推进」的实现会让上面那条绿。"""

    advancing = frame_to_advance_into(
        aggregate=_aggregate("introduce", {}), profile=_profile(), delivered=()
    )
    assert advancing is None, f"什么都还没讲就推进了：{advancing!r}"

    # ⚠️ **模型把槽填满也没用** —— 这一条是 ADR-0146 的核心：
    # 推进读的是平台的记录，**模型报了什么都不影响它**。
    lying = frame_to_advance_into(
        aggregate=_aggregate("introduce", {"material_delivered": "我说我讲完了"}),
        profile=_profile(),
        delivered=(),
    )
    assert lying is None, (
        f"模型说讲完了平台就推进了：{lying!r} —— 那还是「模型决定条件」"
    )


def test_a_frame_is_entered_once_in_a_lifetime() -> None:
    """⚠️ **防循环那一条，而它是这个机制里最要紧的一条。**

    `quote` 完成、pop 回主线之后 `material_delivered` **依然满足** ——
    没有这一条，平台会立刻再推一次，**而那是一个不会停的循环**：
    每一轮都花一次模型调用、给客户发一条消息。

    判据是 `definition_id` 在栈里**从未出现过**，**含已经终结的帧**。
    """

    advancing = frame_to_advance_into(
        aggregate=_aggregate("introduce", {}, history=("quote",)),
        profile=_profile(),
        delivered=(BRIDGE,),
    )
    assert advancing is None, (
        f"`quote` 已经进过一次了，而平台又要推一次：{advancing!r} —— 这是那个不会停的循环"
    )


def test_the_return_path_does_not_advance_halfway_through() -> None:
    """⚠️ **返回途中不推进** —— 而这一条是替一条错判据补的位。

    2026-09-21 我写过一条 `..._after_the_customer_interrupted_once`，
    夹具摆的是「父帧 `active` + 子帧 `completed` 在栈顶」，
    据此断定「一次 pop 之后平台推进整条失效」并改了实现。
    **那个夹具在真实系统里不会出现**，而改动反而把实现弄坏了。

    实测的返回路径是三步：

    ```
    complete      introduce(repair_pending)              > objection(completed)
    repair        introduce(resume_confirmation_pending) > objection(completed)
    request_pop   introduce(active)        ← 子帧在这一步才真的被弹掉
    ```

    `complete` **不弹栈**，所以那一刻栈里一个 `active` 都没有；
    真正弹栈的是 `request_pop`，而它跑完之后栈顶就是父帧、而且是 `active`。

    **所以这一条钉的是中间那两步不推进** —— 对话还在返回途中，
    那时推进等于在客户还没听完上一句时又换了话题。
    """

    profile = _profile()
    for halfway in ("repair_pending", "resume_confirmation_pending"):
        aggregate = _aggregate("introduce", {})
        aggregate["target_stack"][-1]["status"] = halfway
        finished = dict(aggregate["target_stack"][-1])
        finished["id"] = "33333333-3333-4333-8333-333333333333"
        finished["definition_id"] = "objection"
        finished["status"] = "completed"
        aggregate["target_stack"].append(finished)

        assert frame_to_advance_into(
            aggregate=aggregate, profile=profile, delivered=(BRIDGE,)
        ) is None, f"父帧还是 {halfway}，对话在返回途中，而平台已经推进了"


def test_a_full_stack_does_not_advance() -> None:
    """**栈满了不推**，与模型 `push` 同一个上限。"""

    aggregate = _aggregate("introduce", {})
    live = dict(aggregate["target_stack"][-1])
    # ⚠️ **字面量，不是 `PLATFORM_ADVANCE_DEPTH_CAP`**：拿被测常量去造夹具，
    # 常量一改夹具跟着改，**这条用例就永远绿** —— 反向探针实测过：
    # 把上限改成 9，它一声不吭。本仓「拿常量核常量」那一族。
    # 两处相等由 `test_the_platform_advance_obeys_the_same_depth_cap` 单独钉着。
    for index in range(2):
        extra = dict(live)
        extra["id"] = f"22222222-2222-4222-8222-22222222222{index}"
        extra["definition_id"] = f"filler-{index}"
        extra["slots"] = {}
        aggregate["target_stack"].insert(0, extra)

    assert frame_to_advance_into(
        aggregate=aggregate, profile=_profile(), delivered=(BRIDGE,)
    ) is None, "栈已经满了而平台还在往上压"


def test_the_platform_advance_obeys_the_same_depth_cap() -> None:
    """⚠️ **两处的上限不许分家。**

    `PLATFORM_ADVANCE_DEPTH_CAP` 是写死的常量而不是 import ——
    `inference_validation` 反过来依赖 `target_stack`，直接 import 会成环。
    **所以要有一条用例说「它们相等」**，否则改一处另一处不会响。
    """

    from fastagent_v2.worker.inference_validation import MAX_STACK_DEPTH  # noqa: PLC0415

    assert PLATFORM_ADVANCE_DEPTH_CAP == MAX_STACK_DEPTH, (
        f"平台推进用 {PLATFORM_ADVANCE_DEPTH_CAP}，而模型 push 用 {MAX_STACK_DEPTH} —— "
        "同一条约束两个数，其中一个一定是错的"
    )


def test_the_pushed_frame_is_active_and_the_parent_waits() -> None:
    """落地的形状：新帧 `active`、父帧 `suspended`、代次不抬。

    ⚠️ **`interruption_reason` 是 `None`，而那不是漏填**：模型 `push` 时
    这一格记的是打断关系；平台推进**不是打岔**，
    是这通对话按商家声明的顺序往下走了一步。
    """

    aggregate = _aggregate("introduce", {})
    advanced = advance_into_frame(
        aggregate=aggregate,
        profile=_profile(),
        definition_id="quote",
        occurred_at=WHEN,
    )
    parent, child = advanced["target_stack"][-2], advanced["target_stack"][-1]

    assert parent["status"] == "suspended", "父帧没挂起 —— 子帧回来时它就不在等了"
    assert child["status"] == "active"
    assert child["definition_id"] == "quote"
    assert child["parent_frame_id"] == parent["id"]
    assert child["interruption_reason"] is None, "平台推进被记成了打岔"
    assert advanced["generation"] == aggregate["generation"], (
        "代次抬了 —— 没有任何东西被作废，这是同一个回合的延续"
    )
    assert aggregate["target_stack"][-1]["status"] == "active", "原来那份被就地改了"


def test_the_advance_runs_before_the_plan_step() -> None:
    """⚠️ **位置就是这一批的全部** —— 它必须在计划步**之前**。

    条件满足的那一轮，计划步与生成步读到的要是**新帧**的 `goal` 与素材，
    **所以那一轮就报价**。放到计划之后就退化成「下次再说」，
    而用户要的原话是「聊到特定情况**立马**主动推进到下一步」。

    判据是源码顺序：`frame_to_advance_into` 的调用点要排在
    `self._inference.plan(` 之前。**源码扫描看不见代码跑没跑**，
    所以上面那几条量的是行为，这一条只量顺序。
    """

    source = PIPELINE.read_text(encoding="utf-8")
    advance_at = source.index("frame_to_advance_into(\n")
    plan_at = source.index("proposed_plan = self._inference.plan(")
    assert advance_at < plan_at, (
        "平台推进跑在计划步后面了 —— 那一轮的回复仍然按旧帧生成，「立马」就没了"
    )

    # ⚠️ **上面那一行只量顺序，量不到它跑没跑**（本仓「源码扫描看不见代码跑没跑」
    # 那一族）。反向探针实测过：把调用写成 `None and frame_to_advance_into(...)`
    # —— 源码里它还在、顺序也对，**而整条机制已经死了，上面那条纹丝不动**。
    #
    # 所以这里读 AST：那个调用的结果必须**直接**赋给一个名字，
    # 不能包在 `and` / `or` / 条件表达式里，而那个名字随后要被判 `is not None`。
    import ast  # noqa: PLC0415

    tree = ast.parse(source)
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        callee = node.value.func
        if isinstance(callee, ast.Name) and callee.id == "frame_to_advance_into":
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    assert bound, (
        "`frame_to_advance_into(...)` 的结果没有直接赋给任何名字 —— "
        "它被包在别的表达式里（`None and ...` 那一类），整条机制是死的"
    )

    guarded = {
        node.test.left.id
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and any(isinstance(op, ast.IsNot) for op in node.test.ops)
    }
    assert bound & guarded, (
        f"{sorted(bound)} 拿到了推进结果，而没有一处 `if ... is not None` 用它 —— "
        "算出来却没人据它行动"
    )


def test_every_declared_target_exists_and_is_not_a_platform_frame() -> None:
    """配包里每一条 `advance_when` 都指得到一个真的业务帧。

    ⚠️ **指错了是安静的**：封版过得去、加载过得去，
    只有真实对话走到那一步才会发现推不动。
    """

    document = _profile()
    declared = {f["id"] for f in document["target_frames"]}
    for frame in document["target_frames"]:
        for rule in frame.get("advance_when") or []:
            target = str(rule["push"])
            assert target in declared, f"{frame['id']} 要推进到 {target!r}，而包里没有这个帧"
            assert target != frame["id"], f"{frame['id']} 的 advance_when 指着它自己"
            assert target != "closing", (
                f"{frame['id']} 要推进到收尾帧 —— 那是平台帧，进了就解不开了"
            )


def test_the_shipped_chain_is_the_one_the_user_asked_for() -> None:
    """**在架那条链，逐字。** 两段讲完 → 报价 → 推成交。

    这一条钉的是数与形状：加一条推进要把它改过来，
    掉到零则说明有人把 `advance_when` 整段删了，
    **而上面那几条会因为读不到规则而静默地什么都不查**。
    """

    document = _profile()
    chain = {
        frame["id"]: [(r.get("delivered") or r.get("slot"), r["push"])
                      for r in frame.get("advance_when") or []]
        for frame in document["target_frames"]
        if frame.get("advance_when")
    }
    assert chain == {
        "introduce": [(BRIDGE, "quote")],
        "quote": [(PRICE, "trial_close")],
    }, f"在架的推进链变了：{chain}"

    # ⚠️ **两条都读平台的记录，一条都没挂在模型自填的槽上**（ADR-0146）。
    for frame in document["target_frames"]:
        for rule in frame.get("advance_when") or []:
            assert "delivered" in rule, (
                f"{frame['id']} 的推进条件挂在 `slot` 上：{rule} —— "
                "那是模型自填的，忘了填整条链不触发、乱填就过早推进，而两种都不会红"
            )
