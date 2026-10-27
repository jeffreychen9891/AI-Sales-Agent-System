"""「上一轮问了什么」**跨三个回合**活着（B1-2）。

RFC §3.2 给 `pending_question` 的字段是「AI 问题 ID、槽位、asked_at、answer evidence」，
而在此之前它从会话建出来就是 `None`、**零写点零读点** ——
也就是说「这个槽位我上一轮已经问过了」今天没有任何地方记得。

**验收口径（用户 2026-09-13）**：不是「字段被写到了」，是
**造一个会触发它的场景 → 证明行为变了 → 有证据行**。所以这一份跑三个回合：

| 回合 | 发生什么 | 断言 |
|---|---|---|
| ① | 计划点名要 `request_summary`，并且真的发了一条消息 | 挂上问题，`answer_evidence is None` |
| ② | 客户**答非所问**（没有 slot_update） | 那一条**原样还在**，`asked_at` 没变 |
| ③ | 客户答了 | `answer_evidence` == 这一轮的 `event_id` |

**②那一条是这份用例的重点**：一个「每轮都重置」的实现在①③上都绿，
而它恰恰把「别再问一遍」这件事做没了。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor

from tests.conftest import TENANT_A
from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.integration]

ASKED_SLOT = "request_summary"


class _AsksForASlot(DeterministicMockInference):
    """计划点名要一个槽位；可选地让理解步把它答上。

    **两半分开控制**，因为这一份要的正是「问了但没答」那个中间态 ——
    而一个把问与答绑在一起的替身造不出它。
    """

    def __init__(self, *, answers: bool = False, asks: bool = True) -> None:
        super().__init__()
        self._answers = answers
        self._asks = asks

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        # **`asks=False` 是第三轮要的那一档**：客户这一轮把槽位答上了，
        # 而计划**不再问**。若这一轮又问一次，收口时那条新问题会**整条替换**
        # 刚记上的答案证据 —— 那是设计上对的（一个会话只挂一个「上一句问的」），
        # 但它会让这条用例永远看不到 ③。
        document["required_slot_requests"] = [ASKED_SLOT] if self._asks else []
        return document

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        # **正文要真的是一个问句**（运维缺陷 19 之后）：记账判据从
        # 「计划点名了槽位」改成「发出去的客户可见正文里有问号」——
        # 共享替身的 "Message received." 没有问号，在新判据下**不算问了**，
        # 而本文件要的正是「问了」那个场景。问句只在 `asks` 那一档给：
        # 第三轮计划不问，正文也不该再问。
        document = super().generate(**kwargs)
        if self._asks:
            asked = "您想问的是哪一类？"
            document["customer_visible_segments"] = [
                {"type": "text", "content_or_ref": asked}
            ]
            for intent in document["proposed_intents"]:
                if intent.get("kind") == "send_message":
                    intent["typed_payload"]["content"] = {"type": "text", "text": asked}
        return document

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        if self._answers:
            frame = kwargs["aggregate"]["target_stack"][-1]
            document["slot_updates"] = [
                {
                    "frame_id": str(frame["id"]),
                    "slot": ASKED_SLOT,
                    "value": "想问退货怎么走",
                    "confidence": 0.95,
                    "evidence_event_id": str(kwargs["envelope"]["event_id"]),
                }
            ]
        return document


def _accept(settings: Settings, seeded: dict[str, object], conversation_id: UUID) -> None:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=callback(
            external_event_id=f"pq-{uuid4()}", sender_id=sender_of(conversation_id)
        ),
    )
    assert accepted.accepted is True


def _drive(settings: Settings, inference: Any) -> Any:
    return InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="pending-question-worker",
    ).process_one(block_ms=500)


def _state(settings: Settings, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        row = connection.execute(
            "SELECT state, target_stack FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (TENANT_A, conversation_id),
        ).fetchone()
    state = row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])
    return state


def _turn(
    settings: Settings,
    seeded: dict[str, object],
    conversation_id: UUID,
    inference: Any,
) -> None:
    _accept(settings, seeded, conversation_id)
    assert _drive(settings, inference) is not None


def test_a_question_survives_a_turn_that_does_not_answer_it(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """三个回合：问 → 答非所问 → 答。**中间那一轮是重点。**"""

    conversation_id = new_conversation(TENANT_A)

    # ① 问出去
    _turn(settings, seeded_database, conversation_id, _AsksForASlot())
    asked = _state(settings, conversation_id)["pending_question"]

    assert asked is not None, "计划点名要了槽位、消息也发出去了，这一条就该挂上"
    assert asked["slot"] == ASKED_SLOT
    assert asked["answer_evidence"] is None, "`None` 的意思就是「问了、还没答」"
    assert UUID(str(asked["question_id"])), "问题 ID 是那条 send_message 的 intent id"
    assert asked["asked_at"]

    # ② 答非所问 —— 同一个替身，但这一轮不写槽位
    _turn(settings, seeded_database, conversation_id, _AsksForASlot())
    still = _state(settings, conversation_id)["pending_question"]

    assert still is not None, "这一轮没人回答它，问题不该消失"
    assert still["answer_evidence"] is None, (
        "**这一条是本用例的重点**：一个「每轮重置」的实现在①③上都绿，"
        "而它把「别把同一个槽位再问一遍」这件事做没了"
    )

    # ③ 答了
    _turn(
        settings,
        seeded_database,
        conversation_id,
        _AsksForASlot(answers=True, asks=False),
    )
    answered = _state(settings, conversation_id)["pending_question"]

    assert answered["answer_evidence"] is not None, "槽位被填上了，这一条要记上答案证据"
    assert answered["answered_at"]


def test_an_unanswered_question_reaches_the_next_plan_prompt(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**行为变了的证据（上半）**：下一轮的计划步**手上有**这条悬着的问题。

    没有它，`pending_question` 就还是一个「写了没人读」的字段 ——
    B1 这一族的全部毛病就在这里（核心图纸对比 2026-09-13：12 条 B1 里 9 条是这样）。

    **这一条断言的是计划步收到的聚合，不是发出去的 `user_prompt`** ——
    这里用的是确定性替身，它不起 HTTP，读不到那个串。
    **下半在 `tests/contracts/test_plan_prompt_carries_the_turn_state.py`**：
    那一份拿记录型 transport 跑真实的 `ModelBackedInference.plan`，
    把 `user_prompt` 解析回对象来数键。两条合起来才是「模型真的看见了」。
    """

    recorded: list[dict[str, Any]] = []

    class _RecordsItsPrompt(_AsksForASlot):
        def plan(self, **kwargs: Any) -> dict[str, Any]:
            recorded.append(dict(kwargs["aggregate"]))
            return super().plan(**kwargs)

    conversation_id = new_conversation(TENANT_A)
    _turn(settings, seeded_database, conversation_id, _AsksForASlot())
    _turn(settings, seeded_database, conversation_id, _RecordsItsPrompt())

    assert recorded, "第二轮的计划步没跑起来"
    carried = recorded[-1].get("pending_question")
    assert carried is not None and carried["slot"] == ASKED_SLOT, (
        "第二轮的计划步要看得见「上一轮问过 request_summary、还没答」——"
        "这就是它不必再问一遍的依据"
    )


def test_the_focus_lands_on_the_aggregate_after_a_turn(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """`focus` 每轮收口时刷新，**而且只有 `contact` 一类**（B1-1）。

    另三类都不在：`topic` 2026-09-15 撤下（T349：那一支从落地起就是死的，
    而聚合契约同时把 `focus` 裁成了闭合键集，值必须是 typed ref，
    而 topic 今天是一个字符串、指不到一行）；`product` / `order` **指不到东西**。
    解除条件写在 `pipeline._focus_projection` 的 docstring 里；
    分支覆盖由 `tests/contracts/test_focus_projection.py` 管，
    这一条只证明它真的落到了聚合上。

    **这一条是连库才量得出的那一层**：撤掉 topic 那一支之后
    `tests/contracts/` 1,602 条全绿，而这一条红了 ——
    因为它读的是**真的写进库里的那份聚合**。
    """

    conversation_id = new_conversation(TENANT_A)
    _turn(settings, seeded_database, conversation_id, _AsksForASlot())

    focus = _state(settings, conversation_id)["focus"]

    assert set(focus) == {"contact"}, (
        f"focus 今天只该有 contact 一类，实测 {sorted(focus)}"
    )
