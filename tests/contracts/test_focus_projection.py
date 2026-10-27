"""`focus` 只有两类，**而「只有两类」本身要被钉住**（B1-1）。

RFC §3.2 那一行写的是「当前 **contact/product/order/topic** 的 typed refs」，
用户 2026-09-13 裁定**第一版只做 contact 与 topic**：另两类在这个仓里
**指不到东西**（没有商品表、没有订单表，而 §3.2 要的是 typed **ref**）。

**这一份钉的不是「实现了 focus」，是那条边界**：
下一个人看到 `focus` 有值，不会去数它有几个键 —— 而 `product` / `order`
悄悄出现一个空壳，就会被读成「这两类也能用了」。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.pipeline import _focus_projection

TOPIC_SLOT = "concern_topic"


def _prepared(
    *,
    contact: Any = None,
    slots: dict[str, Any] | None = None,
    topic_slot: str | None = TOPIC_SLOT,
) -> Any:
    definition: dict[str, Any] = {"id": "welcome", "information_slots": []}
    if topic_slot is not None:
        definition["material_topic_slot"] = topic_slot
    return SimpleNamespace(
        contact_id=contact,
        profile={"target_frames": [definition]},
        aggregate={
            "target_stack": [
                {"id": str(uuid4()), "definition_id": "welcome", "slots": slots or {}}
            ]
        },
    )


def test_it_carries_the_contact_when_the_customer_has_been_identified() -> None:
    """contact 那一类取 `conversation_aggregates.contact_id`。"""

    contact = uuid4()

    focus = _focus_projection(_prepared(contact=contact))

    assert focus["contact"] == {"contact_id": str(contact)}


def test_an_unidentified_customer_is_a_null_contact_not_a_missing_key() -> None:
    """还没被识别时是 `None`，**键仍然在**。

    与 `content_materials` 空着也发同一条理由：「这一轮没有可指的联系人」
    与「没人告诉你有这回事」不该长得一样。
    """

    focus = _focus_projection(_prepared(contact=None))

    assert "contact" in focus
    assert focus["contact"] is None


def test_the_dead_topic_branch_is_gone_rather_than_left_returning_none() -> None:
    """**T349 的处置：撤下，不是接上**（用户 2026-09-15）。

    先前这里有两条用例，而**两条都在证明一件不会发生的事**：
    一条喂 `{TOPIC_SLOT: {"value": ...}}` 走通了 topic 那一支，
    另一条断言「槽位空着时 topic 是 None」—— 而那句话在生产里**对任何取值都成立**，
    因为 `apply_slot_updates` 写进去的是**标量**，那一支恒不触发。
    两条都绿，两条都为了错的理由绿。

    今天断言的是它**不在了**：帧声明了 `material_topic_slot`、槽位也有值，
    `focus` 里仍然没有 `topic` 这个键。

    **为什么撤下而不是接上**：聚合契约同时把 `focus` 裁成了闭合键集，
    每个值要么 `null` 要么一个 **typed ref**，而 topic 今天是那个槽位的**值**
    （一个字符串，指不到一行）。要加回来先得给它一个 id，那时改 schema。
    """

    focus = _focus_projection(_prepared(slots={TOPIC_SLOT: "运费与包邮"}))

    assert "topic" not in focus, (
        "topic 又回来了，而它要回来得先是一个 typed ref —— "
        "聚合契约里 `focus` 是闭合键集，加一类要先改那份 schema"
    )


def test_product_and_order_are_absent_on_purpose() -> None:
    """**这一条是那条裁定的守卫。**

    用户 2026-09-13：`focus` 第一版只做 contact + topic，
    **另两类的解除条件是「出现权威表」**（`_focus_projection` 的 docstring 里有那张表）。

    **一个空壳比没有更糟**：`"product": None` 会被读成「这一类已经接好了，只是这轮没有」，
    而真相是它**指不到任何东西**。所以这里断言它们**根本不在这个投影里** ——
    哪天商品表来了，加键的人会先撞到这条用例，然后去读那张解除条件表。
    """

    focus = _focus_projection(_prepared(contact=uuid4()))

    assert set(focus) == {"contact"}, (
        f"focus 今天只该有两类，实测 {sorted(focus)} —— "
        "要加 product / order，先读 `_focus_projection` docstring 里那张解除条件表"
    )


def test_the_focus_contract_is_a_closed_key_set() -> None:
    """`focus` 是**闭合键集**（用户 2026-09-15 裁定）—— 加一类要先改 schema。

    先前它是裸 `{"type": "object"}`：**有人加第三类键不会红**，
    而 topic / product / order 三类今天各自都还不能加。

    **`topic` 那个键还在，而它只许是 `null`** —— 这是本次唯一一处"兼容"，
    理由写在 schema 的 description 里：撤下那一支之前每轮都写 `topic: null`，
    **所以已经存库的聚合上带着这个键**，而 `focus` 的校验发生在好几条
    **不经过 `_focus_projection`** 的路径上（人工接管那条读出一行就地校验）。
    直接判它非法 = 那些老行校验不过 = 一次线上 500。
    **`"type": "null"` 是最窄的兼容：老行过得去，而任何给它赋值的企图当场红。**
    """

    from fastagent_v2.runtime.contracts import validate_contract
    from jsonschema import ValidationError

    def _aggregate(focus: dict[str, object]) -> dict[str, object]:
        return {
            "id": "11111111-1111-4111-8111-111111111111",
            "tenant_id": "22222222-2222-4222-8222-222222222222",
            "state_version": 1,
            "lifecycle": "open",
            "control_mode": "ai",
            "generation": 1,
            "profile_release_id": "33333333-3333-4333-8333-333333333333",
            "target_stack": [],
            "focus": focus,
            "commitment_refs": [],
            "active_intent_refs": [],
            "scheduled_event_refs": [],
            "last_transition": {
                "event_id": "55555555-5555-4555-8555-555555555555",
                "transition_id": "66666666-6666-4666-8666-666666666666",
                "reason": "seed",
                "policy_version": "v1",
                "model_version": "v1",
                "profile_version": "v1",
            },
        }

    # 今天生产产出的那两种形态，以及建会话时的初值
    validate_contract("conversation-aggregate", _aggregate({}))
    validate_contract("conversation-aggregate", _aggregate({"contact": None}))
    validate_contract(
        "conversation-aggregate",
        _aggregate({"contact": {"contact_id": "44444444-4444-4444-8444-444444444444"}}),
    )
    # 已经存库的老行：带一个 null 的 topic
    validate_contract("conversation-aggregate", _aggregate({"contact": None, "topic": None}))

    with pytest.raises(ValidationError):
        # 第三类键 —— 闭合键集拦的就是它
        validate_contract("conversation-aggregate", _aggregate({"product": {"product_id": "x"}}))

    with pytest.raises(ValidationError):
        # 给 topic 赋值 —— 它要回来得先是一个 typed ref，而那要改 schema
        validate_contract(
            "conversation-aggregate",
            _aggregate({"contact": None, "topic": {"slot": "s", "value": "运费"}}),
        )
