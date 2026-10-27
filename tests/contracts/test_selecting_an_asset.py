"""挑文件那一步：**只读 schema 允许的字段，三档闸各自成立**。

## 为什么有这份

`select_assets` 第一版读了 `item.get("title")`，而 `content_assets` 是
`additionalProperties: false` 且允许的五个字段里没有 `title` ——
**那一支永远命中不了**。行为没错（回落到 slug），但它是一段声称某字段存在的代码。

这是本仓最贵那类错的镜像：不是「声明了但不存在」，是「读了一个不存在的」。
两者的共同点是**都不会有任何东西变红** —— 读一个不存在的键回 `None`，
而 `None or 默认值` 每次都走默认值，看上去一切正常。

所以这里把「它读哪些字段」与「schema 允许哪些字段」对起来，
**判据从 schema 现读**，不是抄一份字段清单。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.material_selection import select_assets

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = REPOSITORY_ROOT / "packages/contracts/schemas/merchant-profile.schema.json"


def _asset_properties() -> set[str]:
    """`content_assets` 每一项允许的字段，**从 schema 现读**。"""

    def find(node: Any) -> Any:
        if isinstance(node, dict):
            if "content_assets" in node:
                return node["content_assets"]
            for value in node.values():
                found = find(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = find(value)
                if found is not None:
                    return found
        return None

    declaration = find(json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert declaration is not None, "schema 里找不到 content_assets —— 这条用例的锚点没命中"
    return set(declaration["items"]["properties"])


def test_the_selector_reads_only_fields_the_schema_allows() -> None:
    """**它读的每一个字段，schema 都得允许商家写。**

    反过来不要求（schema 允许而选取用不上的字段是合法的：`emit_kinds` 只在
    定时那一档用得到）。要求的是**没有反向的那一种** ——
    读一个商家根本写不进去的字段，那一支永远是死的。
    """

    allowed = _asset_properties()
    read_by_selection = {"ref", "availability", "topics", "stages", "emit_kinds"}

    assert read_by_selection <= allowed, (
        f"选取读了 schema 不允许的字段：{sorted(read_by_selection - allowed)}。"
        "商家写不进去，所以那一支永远命中不了 —— 而它不会红，只会安静地走默认值"
    )


def test_title_is_not_a_field_today_and_the_label_is_the_slug() -> None:
    """**标签就是 slug，而这是一条裁定，不是一个待办。**

    用户 2026-09-16：**不为现有的资料和素材去改平台，该改的是那两样。**
    所以「slug 起得不好，模型挑不准文件」的修法是**把 slug 起好**
    （`price-list` 而不是 `doc-2`），不是给 schema 加一个 `title`
    让一份起错名的资料继续叫那个名字。

    这条红的时候，说明有人还是加了 `title`。那时该做的是先回头看这条裁定；
    真要加，就**让选取用上它**（`label` 优先取 `title`）——
    加了字段而没人读，与读一个没有的字段是同一枚硬币。
    """

    assert "title" not in _asset_properties(), (
        "schema 加了 `title`，而 `select_assets` 还在用 slug 当标签。"
        "去 material_selection.py 里那段注释，它写着该怎么改"
    )

    chosen = select_assets(
        {"content_assets": [{"ref": "asset:price-list:v1", "availability": "on_request"}]},
        understanding={},
    )

    # **硬断言，不是 `空 or 相等`。** 第一版写成了后者 —— 那种写法在
    # 选取一条都没挑中时也会过，而「没挑中」正是这条用例最该发现的失败。
    assert len(chosen) == 1, f"这一条该被挑中，实际挑到 {chosen}"
    assert chosen[0]["label"] == "price-list"


@pytest.mark.parametrize(
    ("availability", "gate", "emit_kind", "offered"),
    [
        ("timer", {"emit_kinds": ["followup"]}, "followup", True),
        ("timer", {"emit_kinds": ["followup"]}, "other_kind", False),
        ("timer", {"emit_kinds": ["followup"]}, None, False),
        ("proactive", {"stages": ["intro"]}, "followup", False),
    ],
    ids=["timer-matches", "timer-other-kind", "timer-on-live-turn", "proactive-on-timer-turn"],
)
def test_the_timer_gate_works_through_the_shared_predicate(
    availability: str, gate: dict[str, Any], emit_kind: str | None, offered: bool
) -> None:
    """**第三档（`timer`）也成立** —— 它是靠复用 `_is_offered` 来的，不是另写的。

    两个方向都钉：定时回合只拿到它那条规则的文件，而**一个 `timer` 档的文件
    不会在活人对话里冒出来**。后者与话术那边逐字同源：
    「跟进的说法不该在对话中间说出来」。
    """

    chosen = select_assets(
        {"content_assets": [{"ref": "asset:f:v1", "availability": availability, **gate}]},
        understanding={},
        emit_kind=emit_kind,
    )

    assert (len(chosen) == 1) is offered


PRICING = {"ref": "asset:pricing-sheet:v1", "availability": "on_request", "topics": ["花钱"]}
DEMO = {"ref": "asset:demo:v1", "availability": "proactive", "stages": ["intro"]}
FOLLOWUP = {"ref": "asset:case:v1", "availability": "timer", "emit_kinds": ["followup_due"]}


def test_a_proactive_asset_is_offered_once_and_then_not_again() -> None:
    """**主动只发一次**（用户 2026-09-17 裁定）。

    两个方向都钉：**没发过时它在**，发过之后它不在。
    只断言后一半的话，「发过就不再递」与「它根本没在选」长得一模一样 ——
    而后者会让这条用例在选取整个坏掉之后依然绿。
    """

    frame = {"content_assets": [DEMO], "material_stages": [{"name": "intro", "after": []}]}

    first = select_assets(frame, understanding={})
    assert [item["ref"] for item in first] == ["asset:demo:v1"], "第一次就没递出去"

    again = select_assets(frame, understanding={}, already_sent=("asset:demo:v1",))
    assert again == (), f"发过之后又递了一次：{again}"


def test_a_timer_asset_is_also_once_only() -> None:
    """`timer` 档同理 —— 它也是平台自己决定递的。"""

    frame = {"content_assets": [FOLLOWUP]}

    first = select_assets(frame, understanding={}, emit_kind="followup_due")
    assert len(first) == 1

    again = select_assets(
        frame, understanding={}, emit_kind="followup_due", already_sent=("asset:case:v1",)
    )
    assert again == ()


def test_an_on_request_asset_is_offered_again_because_he_asked_again() -> None:
    """**`on_request` 不受这条管** —— 那一档的触发条件就是客户问到了。

    他又问了一次价格，就该又拿到价目表：**这不是重复推送，是他要的**。
    这一条与上面两条是同一条裁定的两半，少了它那条裁定就变成了
    「资料一场只发一次」，而那不是用户说的话。
    """

    frame = {"content_assets": [PRICING], "material_topic_slot": "concern_topic"}
    understanding = {"slot_updates": [{"slot": "concern_topic", "value": "花钱"}]}

    again = select_assets(
        frame, understanding=understanding, already_sent=("asset:pricing-sheet:v1",)
    )

    assert [item["ref"] for item in again] == ["asset:pricing-sheet:v1"], (
        "客户又问了一次而价目表没再递 —— `on_request` 被那条「只发一次」误伤了"
    )

