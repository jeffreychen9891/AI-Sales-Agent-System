"""那两个引用数组，**对着权威表逐一核**（B1-5）。

**验收口径由用户 2026-09-13 定死**：不是「字段读到了」，是
**造一个会触发它的场景 → 证明行为变了 → 有证据行**。所以这一份断言的是

> `state->'active_intent_refs'` **等于** `intent_records` 里此刻活着的那批 id，
> `state->'scheduled_event_refs'` **等于** `scheduled_events` 里活着的那批

—— 而不是「数组非空」。**「非空」会被一个「把所有 id 都塞进去」的实现骗过**，
而那种实现在意图走到终态之后仍然挂着引用，
数据上长得像「这条意图还没结」。

**B1-5 之前这两个数组是什么样**：会话建出来是 `[]`，此后**零写点**
（`pipeline.py` 只在建会话时初始化过一次）—— 这一份跑在那个版本上会红在第一条断言。
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
            external_event_id=f"refs-{uuid4()}", sender_id=sender_of(conversation_id)
        ),
    )
    assert accepted.accepted is True


def _drive(settings: Settings, inference: Any | None = None) -> Any:
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference or DeterministicMockInference(),
        worker_id="refs-worker",
    )
    return processor.process_one(block_ms=500)


def _facts(settings: Settings, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        state = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_A, conversation_id),
        ).fetchone()["state"]
        live_intents = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM intent_records"
                " WHERE tenant_id = %s AND conversation_id = %s"
                "   AND status NOT IN ('APPLIED', 'DENIED', 'COMPENSATED', 'FAILED')"
                " ORDER BY created_at, id",
                (TENANT_A, conversation_id),
            ).fetchall()
        ]
        live_events = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM scheduled_events"
                " WHERE tenant_id = %s AND conversation_id = %s"
                "   AND status IN ('PENDING', 'CLAIMED', 'ENQUEUED')"
                " ORDER BY due_at, id",
                (TENANT_A, conversation_id),
            ).fetchall()
        ]
        every_intent = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM intent_records WHERE tenant_id = %s AND conversation_id = %s",
                (TENANT_A, conversation_id),
            ).fetchall()
        ]
    state = state if isinstance(state, dict) else json.loads(state)
    return {
        "refs": state.get("active_intent_refs"),
        "event_refs": state.get("scheduled_event_refs"),
        "live_intents": live_intents,
        "live_events": live_events,
        "every_intent": every_intent,
    }


def test_the_refs_are_exactly_what_is_alive_in_the_authoritative_tables(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**一个真实回合之后，两个数组与权威表逐一对上。**

    这一条同时是 B1-5 的验收：行为变了（先前恒 `[]`），
    而证据就是聚合 `state` 里那两个数组本身 —— 它们现在指得到行。
    """

    conversation_id = new_conversation(TENANT_A)
    _accept(settings, seeded_database, conversation_id)

    outcome = _drive(settings)

    assert outcome is not None, "回合没跑起来，后面每条断言都会读成别的意思"
    facts = _facts(settings, conversation_id)

    assert facts["live_intents"], "这一轮没产出任何意图 —— 夹具选错了，换一个会产出意图的回合"
    assert facts["refs"] == facts["live_intents"], (
        "引用数组必须**逐一等于**权威表里活着的那批：\n"
        f"  聚合 {facts['refs']}\n  权威表 {facts['live_intents']}"
    )
    assert facts["event_refs"] == facts["live_events"]


def test_a_terminal_intent_leaves_the_refs_on_the_next_turn(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """走到终态的意图**不再挂在引用里** —— 这是「引用」与「历史」的分界。

    **这一条钉的是那个反面**：一个「把所有 id 都塞进去」的实现能通过上一条，
    却在这一条上红 —— 而它的症状正是那种最难查的：
    数据上看，那条意图像是还没结。

    **刷新发生在回合收口那一刻**，所以这里跑第二个回合来触发它。
    投递侧把意图推到 `APPLIED` 时不回头改聚合（那要跨 CAS 抢同一行），
    这条语义写在 `_refresh_reference_projection` 的 docstring 里，登记 **T322**。
    """

    conversation_id = new_conversation(TENANT_A)
    _accept(settings, seeded_database, conversation_id)
    _drive(settings)
    first = _facts(settings, conversation_id)
    assert first["refs"], "第一轮就没有引用可言，这一条证明不了任何事"

    # 把第一轮那条意图推到终态 —— 模拟投递成功之后的样子。
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        connection.execute(
            "UPDATE intent_records SET status = 'APPLIED', updated_at = now()"
            " WHERE tenant_id = %s AND conversation_id = %s AND status <> 'APPLIED'",
            (TENANT_A, conversation_id),
        )

    _accept(settings, seeded_database, conversation_id)
    _drive(settings)
    second = _facts(settings, conversation_id)

    assert second["refs"] == second["live_intents"], "第二轮收口时引用要与实况重新对上"
    for gone in set(first["refs"]) - set(second["live_intents"]):
        assert gone not in (second["refs"] or []), (
            f"{gone} 已经走到终态，却还挂在引用里 —— "
            "那在数据上读起来像「这条意图还没结」"
        )
    assert len(second["every_intent"]) > len(second["live_intents"]), (
        "这一条要的局面是「历史里有、活着的里没有」；两者相等说明没造出终态意图"
    )
