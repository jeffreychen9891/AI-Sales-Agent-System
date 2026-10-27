"""定时器到期后的那一条出站，记成 `followup` —— 而它是**平台判的**。

## 这个 kind 为什么值得存在

在此之前，「这条出站是不是定时器到期后的主动跟进」**在 `intent_records` 里答不出来**：

* `kind` 是 `send_message`，与客户刚说完话那一条**一模一样**；
* `proposed_by` 只有 `{"step": "generate", "version": ...}`；
* `strategy_id` 是**配包自己取的名字** —— 商家把那条策略叫什么都行。

于是这一问只能 join 回 `scheduled_events` 再读 `payload_ref.rule_id`。
**现在它是 `kind` 的一个值**（用户 2026-09-16 裁定：
「`followup` 就是定时器到期后主动跟进那一条」）。

## 为什么由平台判，不让模型提名

本仓已经为同一个问题裁过一次：`_PLATFORM_OWNED_ITEM_FIELDS` 把 `frame_id` 与
`evidence_event_id` 从模型要答的东西里拿掉，**「平台供给身份」**。

「这一轮是不是定时器到期」同样是身份不是语义，而让模型提名要付四层的账：
提示词多一行（实测线路 **+145 字节**，吃素材预算）· 计划步也要改
（`GENERATION_ADDED_INTENT`）· **三份在架配包的 `allowed_intent_kinds` 各加一个值**
（`candidate_intents` 只能用策略允许的 kind）· 而模型还可能忘了 ——
**忘了的结果是这条记录说它是一次被动回复，一个安静的错**。

平台判的版本这四项一项都不用付。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastagent_v2.worker.pipeline import (
    GENERATION_WRITE_PATHS,
    PROACTIVE_EQUIVALENT,
    InboxProcessor,
    OutboxDelivery,
)


def _processor() -> InboxProcessor:
    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="unit",
    )


class _Turn:
    """只带一个 `envelope` 的替身 —— 这条判定只读它。

    **不造一个真的 `PreparedEvent`**：那要十几个字段，而其中没有一个参与
    这个判定。一个大夹具会让「它读了什么」这件事看不出来。
    """

    def __init__(self, event_type: str) -> None:
        self.envelope: dict[str, Any] = {"event_type": event_type}


def _kind_for(event_type: str, proposed: str) -> str:
    return InboxProcessor._outbound_kind_for_this_turn(_Turn(event_type), proposed)  # type: ignore[arg-type]


def test_a_timer_turn_records_the_outbound_as_a_followup() -> None:
    """**这一批的全部内容，正面一句。**"""

    assert _kind_for("timer", "send_message") == "followup"


def test_a_customer_message_turn_is_untouched() -> None:
    """客户刚说完话那一条仍然是 `send_message`。

    **非零对照**：只验上面那一条的话，一个「永远返回 followup」的实现也能过，
    而那会把每一条被动回复都记成主动跟进 —— 比记不出来更坏。
    """

    assert _kind_for("customer_message", "send_message") == "send_message"


def test_the_other_event_types_are_untouched_too() -> None:
    """**`event_type` 的值域不止两个。**

    `_remember_turn_refusal` 第 1 条那次的教训逐字适用：
    `manager_comment` / `business_event` / `delivery_receipt` / `internal`
    同样都不是「定时器到期」，而一个写成「不是客户消息就算主动」的实现
    会把它们四个全记成跟进。
    """

    for event_type in ("manager_comment", "business_event", "delivery_receipt", "internal"):
        assert _kind_for(event_type, "send_message") == "send_message", event_type


def test_only_the_outbound_kind_is_reclassified() -> None:
    """`remember` 在定时器回合里仍然是 `remember`。

    它不是一次出站 —— 改它的名字只会让「我们记住了什么」这一问多一个分支。
    """

    # **判据不能从被测的那张表里取。** 第一版写的是
    # `GENERATION_SUPPORTED_KINDS - set(PROACTIVE_EQUIVALENT)` —— 反向探针实测：
    # 把 `remember` 也加进那张表，这一条**照绿**，因为它把 `remember`
    # 从自己的范围里减掉了。**一条用被测对象定义自己范围的用例，
    # 正好在那个对象被改坏时失明。**
    #
    # 换成一个独立判据：**效果不是出站投递的那些 kind**（`DirectWrite` 那一族）
    # 一律不该被改名 —— 它们根本不是一次出站。
    not_outbound = [
        kind
        for kind, path in GENERATION_WRITE_PATHS.items()
        if not isinstance(path.effect, OutboxDelivery)
    ]
    assert not_outbound, "一个 DirectWrite 的 kind 都没有了 —— 这条用例的前提变了"
    for kind in not_outbound:
        assert _kind_for("timer", kind) == kind, kind

    # 反过来也钉一句：那张表的键只能是出站 kind。
    for passive in PROACTIVE_EQUIVALENT:
        assert isinstance(GENERATION_WRITE_PATHS[passive].effect, OutboxDelivery), (
            f"{passive} 不是一次出站投递，却被列进了「被动 → 主动」那张表"
        )


def test_the_map_has_exactly_one_entry_and_both_ends_are_registered() -> None:
    """那张表的两端都必须是真有写入路径的 kind。

    **一条映射指向一个没有写入路径的 kind，会让那一轮的提案被
    `GENERATION_INTENT_KIND_UNSUPPORTED` 拒掉** —— 而拒的理由会指着一个
    模型根本没提过的名字，读起来像模型出错。
    """

    assert len(PROACTIVE_EQUIVALENT) == 1, (
        f"这张表现在有 {len(PROACTIVE_EQUIVALENT)} 条：{dict(PROACTIVE_EQUIVALENT)}。"
        " 多出来的那条要回答：那个出站 kind 在定时器回合里该叫什么，为什么。"
    )
    for passive, proactive in PROACTIVE_EQUIVALENT.items():
        assert passive in GENERATION_WRITE_PATHS, f"{passive} 没有写入路径"
        assert proactive in GENERATION_WRITE_PATHS, f"{proactive} 没有写入路径"


def test_the_two_outbound_kinds_ask_the_same_payload_question() -> None:
    """两条 payload 闸问的是同一件事，**而各自说自己的名字**。

    合成一条泛化的「所有出站 kind 都要 content」会让拒绝理由不说自己在替谁说话
    （`test_the_payload_refusal_speaks_about_its_own_kind` 钉的就是那个）；
    而各写一遍又会漂。**这一条看着它们不漂。**
    """

    good: dict[str, Any] = {"content": {"type": "text", "text": "在"}}
    bad: dict[str, Any] = {"not_content": 1}

    for kind in ("send_message", "followup"):
        refuse = GENERATION_WRITE_PATHS[kind].payload_refusal
        assert refuse(good) is None, f"{kind}：合法 payload 被拒了"
        detail = refuse(bad)
        assert detail is not None, f"{kind}：缺 content 却没拒"
        assert kind in detail, f"{kind}：拒绝理由没说自己在替哪个 kind 说话"
        assert "content" in detail
        assert len(detail) <= 500, f"{kind}：detail 越界了（模型可控字符串）"


def test_the_turn_refusal_is_the_second_lock() -> None:
    """直接提名 `followup` 而这一轮不是定时器 —— 拒。

    **今天没有任何路径会走到这里**：到 `followup` 的只有平台判出来的那一条，
    而它只在定时器回合给这个值。这道闸守的是**明天** ——
    有人让模型直接提名，或者那个判定被改宽。

    **一道今天不会响的闸要有人证明它会响**，否则它与不存在没有区别。
    """

    refuse = GENERATION_WRITE_PATHS["followup"].turn_refusal
    assert refuse is not None, "`followup` 没有回合闸了 —— 它与 send_message 就无法区分了"

    payload: dict[str, Any] = {"content": {"type": "text", "text": "在"}}
    assert refuse(payload, _Turn("timer")) is None  # type: ignore[arg-type]

    detail = refuse(payload, _Turn("customer_message"))  # type: ignore[arg-type]
    assert detail is not None, "非定时器回合的 followup 没有被拒"
    assert "timer" in detail
    assert len(detail) <= 500


def test_the_decision_path_carries_the_reclassified_kind() -> None:
    """整条判定走一遍：定时器回合喂一个 `send_message` 提案，决策里是 `followup`。

    上面那几条问的是那个判定函数；**这一条问的是它真的被接在了链上** ——
    一个没有被调用的分类器与不存在没有区别。
    """

    decision = _processor()._decide_proposal(
        _Turn("timer"),  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal={
            "kind": "send_message",
            "typed_payload": {"not_content": 1},
            "evidence_refs": [f"event:{uuid4()}"],
        },
        ordinal=0,
        candidate_basis="basis-a",
    )

    assert decision.kind == "followup", (
        f"定时器回合的出站决策记成了 {decision.kind!r} —— 那个分类器没有接上链"
    )
    assert decision.accepted is False, "这条用例喂的是缺 content 的 payload，它该被拒"
    assert "followup" in decision.detail, "拒绝理由要说它在替哪个 kind 说话"
