"""主动打破静默之前那一步，**跑在真链路上**（ADR-0149）。

## 为什么这两条非连库不可

`tests/contracts/test_a_silence_is_a_question.py` 验的是形状与那个纯函数：
上下文按构造一致、点名发不出的照常说话、判断进得了请求体。**那几条全绿，
而它们答不了这一问**：那一步到底**被不被调用**，以及它说 `wait` 时这一轮
**真的不发**。

本仓为这个形状写过一整节（`AGENTS.md`：「contracts 全绿，不构成『这条用例
量对了东西』的证据」——`ruff` / `mypy` / contracts 三层全绿只证明语法对、
契约对、静态形状对；**一条用例量的是不是它声称要量的那件事，只有连库跑一遍
答得了**）。

## 两条各守一半

| 条 | 守什么 |
|---|---|
| `..._is_asked_on_a_silence_turn` | 那一步**被调用了**，而客户消息那一轮**没有** |
| `..._wait_sends_nothing` | 它说 `wait` 时，这一轮**一条出站都不产生** |

第一条是第二条的前提：少了它，一个「永远不调那一步」的实现也能让
第二条绿 —— 因为那一轮本来就常常不发。**两条都要，而且顺序是这个。**
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import (
    SILENCE_READING_CHOSE_TO_WAIT,
    InboxProcessor,
    OutboxDispatcher,
)
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e]


class CountingInference(DeterministicMockInference):
    """替身，外加一个计数器：**那一步被问了几次。**

    只数，不改答案 —— 替身仍然一律答 `wait`（见它自己的 docstring：
    四个动作里只有 `wait` 不会改掉别人的断言）。
    """

    def __init__(self) -> None:
        self.silence_questions = 0

    def read_silence(self, **kwargs: Any) -> dict[str, Any]:
        self.silence_questions += 1
        return super().read_silence(**kwargs)


class WaitingInference(CountingInference):
    """说 `wait` 的替身 —— **本条自己带，不改共享那个**。

    共享替身答 `ask_again`，理由写在它自己的 docstring 里：**中性的定义是
    「不改变别人的断言」**，而 `wait` 在理解步之前就返回，会让每一条碰到
    定时器轮次的既有用例不再产出出站（实测 148 条红）。

    **想验某一支就自己给一个替身** —— 这正是那段 docstring 要求的做法。
    """

    def read_silence(self, **kwargs: Any) -> dict[str, Any]:
        document = super().read_silence(**kwargs)
        document["action"]["do"] = "wait"
        document["action"]["because"] = "本条要验的就是这一支。"
        return document


class AcceptingAdapter:
    def send(self, *, command: SendCommand, idempotency_key: str, deadline_at: Any) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: Any) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: Any) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _processor(settings: Settings, worker_id: str, inference: Any) -> InboxProcessor:
    return InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id=worker_id,
    )


def _deliver_one(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> Any:
    """一轮客户消息，走到出站真的发出去 —— 定时器由那次投递排下。"""

    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    counting = CountingInference()
    _deliver_to_outbox(settings=settings, seeded_database=seeded, conversation_id=conversation_id)
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="silence-seed",
    )
    assert dispatcher.dispatch_one() is not None
    return counting


def _fire_the_timer(
    settings: Settings, tenant_id: UUID, conversation_id: UUID, inference: Any
) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'"
            " ORDER BY due_at LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "那次投递没有排下任何定时器 —— 本条无从谈起"
    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="silence-scheduler",
        runtime_deadline_seconds=600,
    )
    enqueued = scheduler.enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    assert any(result.tenant_id == tenant_id for result in enqueued)
    _processor(settings, "silence-timer-worker", inference).process_one(block_ms=1000)


def _outbound_count(tenant_id: UUID, conversation_id: UUID) -> int:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s"
            " AND command->>'audience' = 'customer'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_the_question_is_asked_on_a_silence_turn_and_not_on_a_customer_message(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**被调用的是定时器那一轮，不是客户说话那一轮。**

    客户消息那一轮一次都不该问 —— 那是 ADR-0149 的成本边界：
    「客户消息那一轮一个字节都不多付，也不多一次调用」。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])

    counting = CountingInference()
    from test_retry_budget import _accept  # noqa: PLC0415 - suite-local helper

    _accept(settings=settings, seeded_database=seeded_database, conversation_id=conversation_id)
    assert _processor(settings, "silence-inbound", counting).process_one(block_ms=1000) is not None
    assert counting.silence_questions == 0, (
        f"客户消息那一轮问了 {counting.silence_questions} 次 —— "
        "那一轮不该多付一次模型调用（ADR-0149 的成本边界）"
    )

    OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="silence-dispatch",
    ).dispatch_one()

    timer_side = CountingInference()
    _fire_the_timer(settings, tenant_id, conversation_id, timer_side)
    assert timer_side.silence_questions == 1, (
        f"定时器那一轮问了 {timer_side.silence_questions} 次 —— "
        "那一步没有被调用的话，后面「它说 wait 就不发」那一条会为了错的理由绿"
    )


def test_a_wait_sends_nothing(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**它说「等一等」，这一轮就一条出站都不产生。**

    替身一律答 `wait`，所以这一条量的是：定时器那一轮之后，
    客户可见的出站行数**没有变**。

    ⚠️ **数的是行数而不是「有没有报错」**：一个「照样发了、只是状态被改掉」
    的实现同样不报错，而客户那边已经收到了。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])

    _deliver_one(settings, dict(seeded_database), conversation_id)
    before = _outbound_count(tenant_id, conversation_id)
    assert before >= 1, "那一轮客户消息没有产生出站 —— 本条的前提不成立"

    _fire_the_timer(settings, tenant_id, conversation_id, WaitingInference())

    assert _outbound_count(tenant_id, conversation_id) == before, (
        "它说了 `wait`，而这一轮仍然产生了一条客户可见的出站"
    )

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT reason_code FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND reason_code = %s",
            (tenant_id, conversation_id, SILENCE_READING_CHOSE_TO_WAIT),
        ).fetchone()
    assert row is not None, (
        f"没有留下 {SILENCE_READING_CHOSE_TO_WAIT} 那一行 —— "
        "「这一轮为什么没说话」在台账上是一个空"
    )


class RecordingInference(CountingInference):
    """替身，再加一格：**那一步被告知静默了多久。**

    只记，不改答案 —— 与 `CountingInference` 同一条理由。
    """

    def __init__(self) -> None:
        super().__init__()
        self.silent_for_seconds: list[float] = []

    def read_silence(self, **kwargs: Any) -> dict[str, Any]:
        self.silent_for_seconds.append(float(kwargs["silent_for_seconds"]))
        return super().read_silence(**kwargs)


def test_the_silence_step_is_told_how_long_the_silence_actually_was(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**他静默了多久，那一步得听见真的那个数**（运维缺陷 20）。

    ## 修复前它恒为 `0.0`，而这一条就是为那一刻写的

    `last_outbound_at` 有两份：**列**由投递那一刻写，**JSONB `state`** 从建会话起
    置 `None` 且零写点。而管线工作用的是 `state` 那一份，于是
    `_seconds_since(...)` 恒回 `0.0` ——
    ADR-0149 那一步（主动打破静默前先问一句他为什么不说话）
    **最要紧的那个输入是个常量，它永远看不到一次长静默**。

    stage 2026-09-23 实测：真实静默 93 秒，那一步被告知 **0 秒**。

    ## ⚠️ 为什么把列往回拨，而不是等

    不拨的话，投递与定时器轮次之间的真实墙钟只有零点几秒 ——
    断言只能写成 `> 0`，**而那是一条会因为机器快慢而抖的判据**
    （本仓 `contracts` 里已经有一族挂钟夹具在这么抖）。
    往回拨 600 秒之后，判据是 `>= 600`：**修复前是 0.0，修复后是 600 出头，
    两者隔着六百秒，没有任何时序噪声能跨过去。**

    ## 它守的不是「这个数准不准」，是「读的是不是写的那一份」

    往回拨的是**列**。这一条因此只在「管线读列」时绿；
    任何一种「读 JSONB 那份快照」的实现都会看到 `None` → `0.0` → 红。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])

    _deliver_one(settings, dict(seeded_database), conversation_id)

    with _migration_connection() as connection:
        moved = connection.execute(
            """
            UPDATE conversation_aggregates
               SET last_outbound_at = now() - interval '600 seconds'
             WHERE tenant_id = %s AND id = %s
         RETURNING last_outbound_at, state
            """,
            (tenant_id, conversation_id),
        ).fetchone()
        connection.commit()
    assert moved is not None, "那通对话的聚合不在了 —— 本条的前提不成立"
    assert moved["last_outbound_at"] is not None
    # **先钉住 JSONB 那一份确实是空的** —— 没有这一句，一个「两份都写了」的
    # 实现会让下面那条为了别的理由绿，而本条守的正是「读的是哪一份」。
    assert moved["state"].get("last_outbound_at") is None, (
        "JSONB 那一份现在有值了 —— 那说明有人给它加了写点。"
        "本条的判据要跟着改：今天它守的是「管线读列」，"
        "而那时该守的是「两份不许分家」"
    )

    recording = RecordingInference()
    _fire_the_timer(settings, tenant_id, conversation_id, recording)

    assert recording.silent_for_seconds, (
        "那一步一次都没被调用 —— 本条无从谈起（那一半由上面那条守）"
    )
    assert recording.silent_for_seconds[0] >= 600, (
        f"那一步被告知静默了 {recording.silent_for_seconds[0]} 秒，而列里写着 600 秒之前 —— "
        "写的是列、读的是 JSONB 快照，于是它永远看不到一次长静默（运维缺陷 20）"
    )
