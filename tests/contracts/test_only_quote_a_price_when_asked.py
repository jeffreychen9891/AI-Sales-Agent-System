"""没问价就不要报价 —— **话术按「这一轮问了」判，文件不动**（用户 2026-09-18 裁定）。

## 现场（运维 §9.1，逐字）

```
16:37:45 客户：5000条
16:37:50 我们：「5000条这个量我们接得住。价格是一个月 1888……」  ← 客户此前没问过价
16:38:36 客户：……那你们具体怎么收费的 1888包括全部吗？        ← 他这时才问
16:39:14 客户：有产品报价表吗/
16:39:19 我们：正文 + send_material(asset:pricing-sheet:v1)     ← 要了才发
```

> **用户的裁定**：「我没问价格，AI 自己报了价格；报价表没发，我具体要才给。」
> 两件都反了 —— **主动说出口的是数字，压着不发的是文件。**

## 机制：那个槽是黏的

`concern_topic` 在 2026-09-17 06:31 被写成「花钱」，
而 `on_request` 的判据是**话题槽命中**，不是「客户这一轮问了」——
于是此后每一轮三段报价话术都在候选里。

## 裁定与它的两侧

| | 判据 | 为什么 |
|---|---|---|
| **话术** | **这一轮问了** | 同一句话逐字说三遍，客户读到的是复读 |
| **文件** | 槽位命中（不变） | 运维原话：「他又问一次价格，就该又拿到价目表，这不是重复推送」 |
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

from fastagent_v2.worker.material_selection import (
    select_assets,
    select_materials,
    topic_asked_this_turn,
    topic_text,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SELECTION = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "material_selection.py"
)

PRICE = "material:demo-price-plain:v1"
VALVE = "material:demo-not-sure:v1"
SHEET = "asset:pricing-sheet:v1"

#: 一帧：一段带话题的报价话术、一段无话题的兜底、一份带同一话题的文件。
DEFINITION: dict[str, Any] = {
    "material_topic_slot": "concern_topic",
    "content_materials": [
        {"ref": PRICE, "availability": "on_request", "topics": ["花钱"]},
        {"ref": VALVE, "availability": "on_request"},
    ],
    "content_assets": [
        {"ref": SHEET, "availability": "on_request", "topics": ["花钱"]},
    ],
}
DECLARED = {PRICE: "价格是一个月 1888……", VALVE: "这个我不确定。"}

#: 客户这一轮真的问了。
ASKED = {"slot_updates": [{"slot": "concern_topic", "value": "花钱"}]}
#: 槽里还黏着上一轮那个值，而这一轮他聊的是别的。
STICKY = {"slots": {"concern_topic": "花钱"}}


def _materials(understanding: dict[str, Any] | None) -> set[str]:
    return set(
        select_materials(
            definition=DEFINITION,
            declared=DECLARED,
            emit_kind=None,
            understanding=understanding,
        )
    )


def _assets(understanding: dict[str, Any] | None) -> set[str]:
    return {
        str(entry["ref"])
        for entry in select_assets(
            DEFINITION,
            frame=STICKY,
            understanding=understanding,
            delivered=(),
            emit_kind=None,
        )
    }


# ---------------------------------------------------------------------------
# 一 · 话术：没问就不给，问了就给
# ---------------------------------------------------------------------------


def test_a_sticky_slot_no_longer_puts_the_price_in_front_of_the_model() -> None:
    """**运维 §9.1 那一轮**：客户说「5000条」，而他从没问过价。

    槽里黏着「花钱」——在这一批之前，那三段报价话术**每一轮都在候选里**，
    于是模型顺手说了。
    """

    assert PRICE not in _materials(None), "槽黏着就把报价摆在模型面前 —— 那正是要修的"


def test_asking_this_turn_puts_it_back() -> None:
    """**他这一轮问了，就该拿到。**

    这条与上一条成对：一条钉「没问不给」，一条钉「别把该给的也拦了」。
    只有前一条的闸，可以靠「一段都不给」拿满分。
    """

    assert PRICE in _materials(ASKED)


def test_the_ungated_material_is_still_offered_every_turn() -> None:
    """**无 `topics` 的那一段是兜底，与话题判据无关。**

    它的理由写在 `_is_offered` 里：话题闸的失败方向是「槽填歪了 →
    答这个问题的素材被挡住 → 模型自己编」，而一段不受话题限制的
    「我不确定」把那次失败变成说实话。**本批不许动到它。**
    """

    assert VALVE in _materials(None)
    assert VALVE in _materials(ASKED)


# ---------------------------------------------------------------------------
# 二 · 文件不动 —— 裁定的另一半
# ---------------------------------------------------------------------------


def test_files_still_follow_the_stored_slot() -> None:
    """**文件那一档不改**（用户 2026-09-18 裁定）。

    运维原话：`on_request` 对文件成立 ——
    「他又问一次价格，就该又拿到价目表，这不是重复推送」。
    而话术不是这样：同一句话逐字说三遍，客户读到的是复读。

    **两个判据的差别写在调用点上**：话术那一侧传
    `topic_asked_this_turn`，文件那一侧传 `topic_text`。
    """

    assert SHEET in _assets(None), "文件那一档被这一批动到了 —— 裁定说不动"
    assert topic_text(DEFINITION, frame=STICKY, understanding=None) == "花钱"
    assert topic_asked_this_turn(DEFINITION, understanding=None) == ""


# ---------------------------------------------------------------------------
# 三 · 参数的缺席就是那道守卫
# ---------------------------------------------------------------------------


def test_select_materials_cannot_see_the_frame_at_all() -> None:
    """**`select_materials` 不再收 `frame`。**

    改完判据之后帧在那个函数里一处都用不到。留着它并压掉未使用告警是最省事的，
    而那样：下一个人看到签名里有 `frame` 会以为话术仍然看槽位现值，
    且把 `topic_asked_this_turn` 改回 `topic_text` **一行就够、签名不会拦他**。

    **拿掉它，参数的缺席本身就是那道守卫** —— 想改回去先得把参数加回来，
    而那是一处看得见的改动。
    """

    assert "frame" not in inspect.signature(select_materials).parameters
    assert "frame" in inspect.signature(select_assets).parameters, (
        "文件那一侧也把 frame 拿掉了 —— 它的判据没变，不该跟着动"
    )


def test_the_pipeline_stopped_passing_it_too() -> None:
    """管线那一处也要跟上 —— 否则它连调用都过不去，而这条用例说得出是哪一处。"""

    tree = ast.parse(SELECTION.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }

    assert "topic_asked_this_turn" in names
    assert "_legal_update_values" in names, "词表那一道没了 —— 累积串会重新点亮两组"


# ---------------------------------------------------------------------------
# 四 · 我自己在这一批里做错又修好的那一处
# ---------------------------------------------------------------------------


def test_a_half_configured_package_is_not_locked_shut() -> None:
    """**声明了话题槽、素材还没标 `topics`** —— 那时这条规则整个不施加。

    ⚠️ **我第一版把它判错了。** 新加的词表过滤直接写
    `if topic_vocabulary(...) is None: return []` ——
    而那个 `None` **同时表示两件事**：帧没声明话题槽，与**词表是空的**。
    于是「配到一半」的包被判成一个值都不认。

    那正是 `topic_vocabulary` 那个 `return None` 要防的反面
    （`26ad831c` 开口二：空词表会把每个值都判非法，
    那个包的话题槽**永远写不进任何值**，而不是「拦得松了」）。

    现在先按槽名取值，词表只用来**过滤**：取不到词表就不过滤。
    """

    half_configured = {"material_topic_slot": "concern_topic", "content_materials": []}

    assert topic_asked_this_turn(half_configured, understanding=ASKED) == "花钱"

    no_slot: dict[str, Any] = {"content_materials": []}
    assert topic_asked_this_turn(no_slot, understanding=ASKED) == "", (
        "帧没声明话题槽时没有哪个槽可读 —— 这与「有槽而词表空」是两件事"
    )


def test_an_accumulated_string_cannot_light_two_topic_groups() -> None:
    """**这一轮那条 update 也要过词表** —— 本批顺带关上的那半。

    ## 是那条既有用例把它逼出来的

    改完判据之后 `test_the_offered_set_does_not_grow_with_the_conversation` 红了，
    而它红得对。当场量的：

    ```python
    topic_text(definition, frame={"slots": {}}, understanding={"slot_updates": [
        {"slot": "concern_topic", "value": "已覆盖：花钱；当前：查问题"}]})
    # → '已覆盖：花钱；当前：查问题'    两组素材一起点亮
    ```

    三道闸各关了一半，而这半没人关：**C-1** 关的是「累积串写不进槽」（存储那条路）·
    **T351 / T274** 关的是「一轮多发几条 update」（只认最后一条）·
    而「**一条 update 里塞一个累积串**」在此之前没有任何东西拦。

    它从前藏在「槽位现值」后面（`topic_text` 是两半之和，存的那半由 C-1 保证干净）。
    **话术改成只看这一轮那半之后，它成了唯一那条路。**

    修法用同一个词表（`runtime.topic_vocabulary`，与 `apply_slot_updates` 同源），
    **不另立第三份判据**。
    """

    two_groups: dict[str, Any] = {
        "material_topic_slot": "concern_topic",
        "content_materials": [
            {"ref": PRICE, "availability": "on_request", "topics": ["花钱"]},
            {"ref": "material:demo-audit:v1", "availability": "on_request",
             "topics": ["查问题"]},
        ],
    }
    declared = {PRICE: "价格是一个月 1888……", "material:demo-audit:v1": "查问题这样看。"}
    accumulated = {
        "slot_updates": [
            {"slot": "concern_topic", "value": "已覆盖：花钱；当前：查问题"}
        ]
    }

    offered = set(
        select_materials(
            definition=two_groups,
            declared=declared,
            emit_kind=None,
            understanding=accumulated,
        )
    )

    assert offered == set(), f"累积串点亮了 {sorted(offered)} —— 词表那一道没拦住"

    # 而一个合法值照样点得亮它那一组 —— 否则这条用例可以靠「一段都不给」拿满分。
    legal = set(
        select_materials(
            definition=two_groups,
            declared=declared,
            emit_kind=None,
            understanding={"slot_updates": [{"slot": "concern_topic", "value": "查问题"}]},
        )
    )
    assert legal == {"material:demo-audit:v1"}
