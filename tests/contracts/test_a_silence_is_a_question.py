"""主动打破静默之前那一步（ADR-0149），以及它的三个消费方。

## 它修的是什么

2026-09-22 stage：`goal_stall` 在第 1 条发出后 6 秒触发，那一轮把第 1 条
拆成两句重说了一遍。**而模型没有偷懒** —— 定时器轮次照常调理解步，
提示词里也写着 `event_type: "timer"`，**问题是它被要求填的每一格都是
「客户刚说了什么」**。客户没说话，格子只能空着，它答
「nothing new was learned」，计划步拿着空答案只能复述刚讲过的话。

**那是对一个问错了的问题给出的正确答案。**

## 这份文件守哪几件

| # | 守什么 | 为什么它单独成一条 |
|---|---|---|
| 1 | 上下文与理解步**按构造一致** | 抄一份会漂，而漂了只在真实回合里显形 |
| 2 | 三个多出来的键确实多出来了 | 少一个，模型就分不出「6 秒」与「6 小时」 |
| 3 | 点名发不出的 ref → **照常说话**，只是不带素材 | 用户裁定「没有素材但是话要说」 |
| 4 | 点名发得出的 ref → **原样保留** | 反侧。少了它，一个「一律摘掉 ref」的实现也能过第 3 条 |
| 5 | `wait` 的判断**真的到得了**计划步的请求体 | 消费方 2；渲染对不代表线上读得到 |

连库那两件（`wait` 走 `no_send`、承诺锚点不问）在
`tests/e2e/test_a_silence_is_a_question.py` —— **那两件只有跑一轮才看得见**，
而本仓记过「contracts 全绿不构成『这条用例量对了东西』的证据」。
"""

from __future__ import annotations

import json
from typing import Any

from fastagent_v2.runtime import request_shape
from fastagent_v2.worker.pipeline import _without_an_unsendable_ref


def _reading(**action: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "reading": "他刚被问到谁在盯客户消息，还没来得及答。",
        "action": {"because": "开场问题还没落地，帧的目标是摸清现状", **action},
    }


def test_the_context_is_the_understanding_step_s_own() -> None:
    """**按构造一致，不是「看起来一致」。**

    `silence_prompt` 把 `understand_prompt` 的返回值原样拿来再加三个键。
    这一条断言的是**集合关系**：理解步的键一个不少。

    抄一份会漂，而本模块已经为这个形状写过两次纪律
    （「管线算一次，两步收同一份」）—— 那种不一致只在真实回合里显形。
    """

    understanding = request_shape.understand_prompt(goal="g", information_slots=[])
    silence = request_shape.silence_prompt(goal="g", information_slots=[])

    assert set(understanding) <= set(silence), (
        f"理解步有而这一步没有的键：{sorted(set(understanding) - set(silence))} —— "
        "上下文不再是同一份了"
    )
    for key, value in understanding.items():
        assert silence[key] == value, f"{key} 两边不一样：{value!r} vs {silence[key]!r}"


def test_the_three_extra_facts_are_the_only_extras() -> None:
    """多出来的**正好**那三样 —— 等值，不是包含。

    多一样：有人又往里塞了一条「对沉默的理论」，而用户 2026-09-22 裁过
    「静默情况也是无法穷尽的」，挑输入和挑词表是同一个错。
    少一样：模型分不出「6 秒」与「6 小时」，或分不出第一次追问与第五次。
    """

    understanding = request_shape.understand_prompt(goal="g", information_slots=[])
    silence = request_shape.silence_prompt(goal="g", information_slots=[])

    assert sorted(set(silence) - set(understanding)) == [
        "materials_offered_now",
        "silent_for_seconds",
        "unanswered_rounds",
    ]


def test_an_unsendable_ref_does_not_silence_the_turn() -> None:
    """**没有素材但是话要说**（用户 2026-09-22）。

    点名了一份这一轮发不出的话术 —— 不拒这一轮，摘掉 ref、降回 `ask_again`，
    并把「为什么」留在 `because` 里。

    ⚠️ **不退回「让平台自己挑一份」**：挑了就是平台在决定对客户说什么，
    而那正是这一批要消灭的自说自话。
    """

    reading = _without_an_unsendable_ref(
        _reading(do="send_material", material_ref="material:not-offered:v1"),
        offered={"material:offered:v1": "商家审过的原文"},
    )

    assert reading["action"]["do"] == "ask_again", "这一轮被判成不说话了"
    assert reading["action"]["material_ref"] is None
    assert "material:not-offered:v1" in reading["action"]["because"], (
        "留痕里没说是哪一个发不出 —— 读到它的人查不出该修配包的哪一段"
    )
    assert reading["reading"], "模型那段判断被顺手丢掉了"


def test_a_sendable_ref_is_left_alone() -> None:
    """反侧：发得出的那一份**原样保留**。

    少了这一条，一个「一律摘掉 ref」的实现也能过上面那条 ——
    而那种实现会让 `send_material` 永远发不出任何东西，
    **且没有任何东西变红**（`AGENTS.md`：放宽一条判据时正反两侧都要验）。
    """

    reading = _without_an_unsendable_ref(
        _reading(do="send_material", material_ref="material:offered:v1"),
        offered={"material:offered:v1": "商家审过的原文"},
    )

    assert reading["action"]["do"] == "send_material"
    assert reading["action"]["material_ref"] == "material:offered:v1"


def test_the_other_actions_are_untouched_even_with_an_empty_shelf() -> None:
    """货架空着时，另外三个动作一个都不该被改。

    **空货架是常态而不是异常**：在架配包今天一条 `availability: timer` 的
    素材都没有，而 `wait` / `ask_again` / `hand_to_human` 本来就不需要素材。
    """

    for do in ("wait", "ask_again", "hand_to_human"):
        reading = _without_an_unsendable_ref(_reading(do=do), offered={})
        assert reading["action"]["do"] == do, f"{do} 被改成了 {reading['action']['do']}"


def test_the_reading_reaches_the_plan_request_body() -> None:
    """**消费方 2 接上了** —— 那份判断真的进了计划步的 `user_prompt`。

    `test_request_shape_key_sets` 断言那个键**在**；这一条断言它**带着值**。
    两条差一层：一个恒为空字典的键同样满足前者，
    而那正是「字段是装饰」那一族（`AGENTS.md` 零调用方第二问）。
    """

    body = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        silence_reading=_reading(do="ask_again"),
    )

    rendered = json.dumps(body, ensure_ascii=False)
    assert body["silence_reading"]["action"]["do"] == "ask_again"
    assert "他刚被问到谁在盯客户消息" in rendered, "那段判断没进请求体"


def test_an_ordinary_turn_still_carries_the_key_empty() -> None:
    """客户消息那一轮**发空字典，而不是不发**。

    与本模块其余几处同一条纪律：「这一轮不是主动触达」与
    「没人告诉你有这回事」不该在模型眼里长得一样。
    """

    body = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
    )

    assert body["silence_reading"] == {}
