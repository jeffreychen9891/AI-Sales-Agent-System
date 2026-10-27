"""反例教了坏形（运维缺陷 23/24/25 + 顺带两条，2026-09-24）。

## 24/25 是同一个机制，修在同一处

模型在复测里做了 goal 反例警告的那件事本身：把客户刚说的话复述回去，
连词都是从反例里借的（「接住了」全配包只出现在「您说没人管——这话我接住了」
这句**反面**例句里）。缺陷 18「规则被它自己的示例拆了」第二例。

根因两半：**反例句是一个完整的可借用模板**（否定词是一个 token，模板是一整句），
而「他答了你的问题之后该怎么接」**没有正路指令**——所有轻量收束（回执、复述）
都被禁了，唯一的具体示范恰是那句反例。

修法（用户裁定「解决他为什么要复述」，不建闸）：四个 goal 里的反例模板
全部拆成**只描述动作、不给例句**的禁令，并给正路一句明确指令
（「答案直接用进你下一句要说的事里」）。

## 23 与顺①：同样只改 goal

- 「你的待办不是话题」——还缺什么槽、还有几件没定，是模型自己的清单，
  此前没有任何一句话说过不能讲给客户；
- 「报价是你的事」——quote 帧规则说先给价，模型却反过来问客户预算。

## 顺②：兜底要留来路

`ask_clarification` 兜底又被用了一次而 deny 行 context 为空——STACK_OP 族的
`PlanRejected.context` 本来就空，透传等于无痕。`_fallback_denial_context`
给两条路（静默/说话）的 deny 都补上「兜底是谁、它是兜底」。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastagent_v2.worker.pipeline import _fallback_denial_context

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: 被借走过（或与它同族同柜）的模板：反例整句 + 回执五连 + 那个被借走的词。
#: **这批不许出现在任何 goal 里** —— 禁令改成了只描述动作的写法。
BORROWABLE = (
    "这话我接住了",
    "我记下了",
    "我听到了",
    "我收到了",
    "我知道了",
    "我来安排",
    "接住",
)


def _demo() -> dict[str, Any]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json").read_text(
            encoding="utf-8"
        )
    )


def _goal(document: dict[str, Any], frame_id: str) -> str:
    return next(f for f in document["target_frames"] if f["id"] == frame_id)["goal"]


def test_no_goal_carries_a_borrowable_template() -> None:
    """反例句与回执五连从全部 goal 里拆净 —— 模型的上下文里不再有那个模板。

    「不要这么说」的例句对模型同时是禁令和措辞样本，后一半没人想要
    （缺陷 25 实测：例句里的「接住了」被当成业务概念反复使用，
    最后整句形状——复述回去——也被照着做了）。
    """

    demo = _demo()
    assert demo["identity"]["version"] == "1.4.12"
    for frame in demo["target_frames"]:
        for phrase in BORROWABLE:
            assert phrase not in frame["goal"], (frame["id"], phrase)


def test_the_positive_path_replaced_the_template() -> None:
    """拆掉模板的同时给了正路：答案直接用进下一句，不复述。

    修复前「他答了就用答案往下走」没有说明「用」与「引用」的区别，
    而 10:46:44 的复述正是把「用答案」做成了「把答案念一遍」。
    """

    demo = _demo()
    for frame_id in ("introduce", "quote", "trial_close"):
        assert "也不要把他答的先复述一遍" in _goal(demo, frame_id), frame_id
    assert "把答案直接用进你下一句要说的事里" in _goal(demo, "introduce")


def test_the_todo_is_not_a_topic() -> None:
    """缺陷 23：内部待办此前没有任何一句禁令 —— 现在 introduce.goal 有了。

    模型把「还剩两件事没定」念给了客户，而那两件是它自己发明的
    （introduce 的槽表里根本没有那两项）—— 没有字段可脱敏，
    只能从源头说清「你的清单不是话题」。
    """

    goal = _goal(_demo(), "introduce")
    assert "你的待办不是话题" in goal
    assert "一个字也别对他说" in goal


def test_the_quote_stays_on_our_side_of_the_table() -> None:
    """顺①：报价不外推 —— 「花多少钱」「心里有没有数」不该由客户答。

    quote.goal 第一句一直是「先给价格，再补口径」，而复测里模型
    反过来问客户预算。规则补的是**动作禁令**（不问他打算花多少），
    且刻意不写可借用的问句模板。
    """

    goal = _goal(_demo(), "quote")
    assert "不要把报价这件事反过来推给他" in goal
    assert "先给价格，再补口径" in goal


def test_a_fallback_denial_names_its_fallback() -> None:
    """顺②：deny 行的 context 说得出「兜底是谁、它是兜底」。

    STACK_OP 族的 `PlanRejected.context` 是空的，修复前静默路透传它 ——
    审计里那行 deny 什么都不带，而同一轮的 `strategy_id` 是一个
    **配包里不存在**的兜底名（`ask_clarification`），读的人对不上号。
    """

    rejection = SimpleNamespace(context={})
    plan = SimpleNamespace(
        strategy_id="ask_clarification", document={"origin": "safe_fallback"}
    )
    context = _fallback_denial_context(rejection=rejection, plan=plan)
    assert context == {
        "fallback_strategy": "ask_clarification",
        "fallback_origin": "safe_fallback",
    }

    # 拒绝自带的现场不许被盖掉 —— 兜底信息是**追加**的。
    rejection = SimpleNamespace(context={"empty_required_slots": ["current_handling"]})
    context = _fallback_denial_context(rejection=rejection, plan=plan)
    assert context is not None
    assert context["empty_required_slots"] == ["current_handling"]
    assert context["fallback_strategy"] == "ask_clarification"

    # 模型自己选了静默：没有拒绝、没有兜底，`None` 照旧。
    assert _fallback_denial_context(rejection=None, plan=plan) is None
