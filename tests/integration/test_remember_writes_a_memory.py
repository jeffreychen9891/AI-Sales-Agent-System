"""`_remember_write` 的 SQL **真的被执行一次**。

本文件只有一个理由存在：E4-2 的合同用例（`tests/contracts/test_remember_write_path.py`）
把六道闸、效果分派、两处量纲全钉住了，**却没有一条让那段 `INSERT` 碰到数据库**。
一个写错列名、把 `revision` 写成 0、或者漏了 `status` 的版本，
**在那 12 条里全绿**——它们问的是「路由对不对」，不是「SQL 跑不跑得起来」。

**这不是端到端。** 驱动一整回合需要模型提案一条 `remember`，
而桩推理（`inference.py` 的 `_send_payload`）给每个 kind 都造 `send_message` 形状的
payload，`remember` 的 payload 闸会先拒掉它。**改桩是另一批的事**，
本文件退而求其次：**直接调那个写入函数，用真库、真外键**，
覆盖的正是「SQL 本身对不对」这一段风险。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.worker.pipeline import (
    MEMORIES_CITED_REASON_CODE,
    MEMORIES_SENT_REASON_CODE,
    InboxProcessor,
    PreparedEvent,
    ProposalDecision,
    _remember_write,
)
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.conftest import PROFILE_RELEASE, TENANT_A, _migration_url

CONTACT = uuid.UUID("cc000000-0000-4000-8000-0000000000b1")


@pytest.fixture()
def seeded(
    seeded_database: dict[str, object],
) -> Iterator[tuple[Connection, uuid.UUID, uuid.UUID]]:
    """一个 contact 和一条真实的 inbox 事件——**记忆的来源必须存在**。

    `contact_memories.source_event_id` 有 `(tenant_id, source_event_id) → inbox_events`
    的外键，所以这条事件不是布景，**它是被测的那一半**：
    `_remember_write` 盖上去的那个 id 如果不是一条真事件，这个 `INSERT` 会被库拒掉。

    ## ⚠️ 这个夹具曾经把两个 id 塞成同一个值，于是它为了错的理由绿着

    **2026-09-22 更正。** 原来这里写的是 ——

        INSERT INTO inbox_events(tenant_id, id, event_id, …)
        VALUES (%s, %s, %s, …)          -- 两个位置传的是**同一个** `event_id`

    `inbox_events.id` 与 `inbox_events.event_id` 在生产里是**两个不同的值**，
    信封里那个落在 `event_id` 列（运维 2026-09-22T07:20Z 在 stage 上逐行量过）。
    **夹具把它们并成一个之后，外键指哪一列都满足** —— 而生产里
    `_remember_write` 盖的是信封那个，外键（`054`）指的是 `id`，
    于是**从管线写记忆一次都不可能成功**，整轮回滚，客户一个字都收不到。
    这条用例的 docstring 当时就声称自己在守这条外键，**而它守不到**。

    所以现在两个 id **故意取不同的值**，并且下面有一句断言钉住这件事：
    一旦有人再把它们并回去，这份夹具就不再是生产的形状，而那正是
    `AGENTS.md`「那条用例喂的东西必须是生产里会出现的形状」说的那一类。
    真正的修法在 `073_contact_memories_source_event_fk.sql`（外键改指 `event_id`）。

    `config_id` 用播种夹具建好的 `'config-a'`——`contacts` 有指向
    `channel_bindings` 的外键，编一个字符串会在 fixture setup 就炸
    （E4-1 的第一版写了 `'cfg'`，门禁给出 12 个 setup error）。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    # **两个，不是一个。** `inbox_id` 是行主键，`event_id` 是信封里那个。
    inbox_id = uuid.uuid4()
    event_id = uuid.uuid4()
    assert inbox_id != event_id, (
        "这两个 id 必须不同 —— 并成一个的那一版让这份夹具变成了生产里不存在的形状，"
        "外键指哪一列都满足，于是这条用例守不住它声称在守的那条外键。"
    )
    with Connection.connect(_migration_url(), row_factory=dict_row) as connection:
        connection.execute(
            "INSERT INTO contacts (tenant_id, id, config_id, external_user_id) "
            "VALUES (%s, %s, %s, %s)",
            (TENANT_A, CONTACT, "config-a", str(CONTACT)),
        )
        connection.execute(
            """
            INSERT INTO inbox_events(
                tenant_id, id, event_id, source, external_event_id, conversation_id,
                event_type, profile_release_id, envelope, status
            ) VALUES (%s, %s, %s, 'wecom_proxy', %s, %s, 'customer_message', %s, %s,
                      'consumed')
            """,
            (
                TENANT_A,
                inbox_id,
                event_id,
                f"remember-{event_id}",
                uuid.uuid4(),
                PROFILE_RELEASE,
                Jsonb({"schema_version": "1.0.0", "event_id": str(event_id)}),
            ),
        )
        yield connection, inbox_id, event_id


def _prepared(inbox_id: uuid.UUID, event_id: uuid.UUID) -> PreparedEvent:
    """生产的形状：`inbox_id` **就是** `inbox_events.id`，而信封里那个是 `event_id`。

    先前这里传的是第三个随机 uuid，于是「写入方改成盖 `prepared.inbox_id`」
    这个变异会因为那个 uuid 根本不在 `inbox_events` 里而红 —— 红得对，
    **而理由是假的**。传真的行主键之后，那个变异红在它该红的地方：
    外键（`073` 之后）指的是 `event_id`，盖 `inbox_id` 进去查不到。
    """

    return PreparedEvent(
        inbox_id=inbox_id,
        tenant_id=TENANT_A,
        conversation_id=uuid.uuid4(),
        envelope={"event_type": "customer_message", "event_id": str(event_id)},
        profile={},
        aggregate={},
        previous_state_version=0,
        previous_generation=0,
        contact_id=CONTACT,
    )


def _decision(**payload: Any) -> ProposalDecision:
    return ProposalDecision(
        ordinal=0,
        kind="remember",
        accepted=True,
        reason_code="",
        detail="",
        proposal={"kind": "remember", "typed_payload": payload},
    )


def test_the_memory_lands_with_the_event_that_justifies_it(
    seeded: tuple[Connection, uuid.UUID, uuid.UUID],
) -> None:
    """写进去了，**而且来源是平台盖的那条事件**。

    `source_event_id` 是这一行里唯一「本可以静默写错」的列：
    写成 `NULL` 库也收（它可空，因为 E4-3 的员工录入路径没有 inbound 事件），
    **于是一条来路不明的记忆看起来和一条有据可查的一模一样**。
    """

    connection, inbox_id, event_id = seeded
    _remember_write(
        connection,
        _prepared(inbox_id, event_id),
        _decision(field_key="ring_size", field_value="18 号"),
    )

    row = connection.execute(
        "SELECT * FROM contact_memories WHERE tenant_id = %s AND contact_id = %s",
        (TENANT_A, CONTACT),
    ).fetchone()

    assert row is not None, "记忆没写进去"
    assert row["field_key"] == "ring_size"
    assert row["field_value"] == "18 号"
    assert row["source_event_id"] == event_id, (
        "来源不是正在处理的那条事件——`source_event_id` 是平台盖的（T34 / T41 的规矩），"
        "盖错了没有任何东西会红，而它是「这条记忆凭什么」的全部答案"
    )
    # **本条的题眼，而它是运维 2026-09-22 那次事故要的那条守卫**：
    # 写进去的那个值查得到，**而且查的是 `event_id` 那一列，不是 `id`**。
    # 外键（`073` 之后）执行前一半；后一半只有这里说得出来 ——
    # 两个 id 都存在、都是 uuid、都能通过任何形状检查，
    # **指错了哪一个没有任何东西会红**（`054` 就指错了，而它必然违反）。
    anchor = connection.execute(
        "SELECT id, event_id FROM inbox_events WHERE tenant_id = %s AND event_id = %s",
        (TENANT_A, row["source_event_id"]),
    ).fetchone()
    assert anchor is not None, (
        "`source_event_id` 在 `inbox_events.event_id` 里查不到 —— "
        "外键指的就是这一列（`073`），所以这条记忆锚在一条不存在的事件上"
    )
    assert anchor["id"] != row["source_event_id"], (
        "写进去的值等于 `inbox_events.id` 了 —— 那是**另一个** id。"
        "\n本仓 `source_event_id` 一律指信封里那个（`commitments` 的两个生产者同口径），"
        "\n盖成行主键会让同名两列含义分叉，而没有任何东西会报错。"
    )

    assert row["revision"] == 1
    assert row["supersedes"] is None, "模型只能追加，第一版必然不 supersede（裁定八）"
    assert row["status"] == "active"
    assert row["tombstoned_at"] is None


def test_two_different_fields_both_land(
    seeded: tuple[Connection, uuid.UUID, uuid.UUID],
) -> None:
    """**一个 contact 可以有多条记忆，只要字段不同。**

    `contact_memories_one_active_per_field_idx` 管的是「一个字段一条 active」，
    **不是「一个 contact 一条」**——一个把唯一索引读窄了的实现会在这里撞上。
    """

    connection, inbox_id, event_id = seeded
    prepared = _prepared(inbox_id, event_id)
    _remember_write(connection, prepared, _decision(field_key="ring_size", field_value="18 号"))
    _remember_write(
        connection, prepared, _decision(field_key="anniversary", field_value="6 月 1 日")
    )

    keys = [
        row["field_key"]
        for row in connection.execute(
            "SELECT field_key FROM contact_memories WHERE tenant_id = %s AND contact_id = %s "
            "ORDER BY field_key",
            (TENANT_A, CONTACT),
        ).fetchall()
    ]
    assert keys == ["anniversary", "ring_size"]


def test_the_two_memory_audit_rows_actually_go_in(
    seeded: tuple[Connection, uuid.UUID, uuid.UUID],
) -> None:
    """⚠️ **这一条是 2026-09-22 那场事故的连库那一半。**

    那两条 INSERT 把 `reason_code` 写成字面 `NULL`，而那一列是 `NOT NULL`——
    **从上线到那天一行都没写进去过**，因为触发条件要求联系人至少有一条
    active 记忆，而 `contact_memories` 因为外键指错列全库 0 行。
    迁移 `073` 把外键改对之后，实测 **91 秒**后整通对话被打死。

    ## 为什么连库这一条不可省

    同批那条源码扫描（`tests/contracts/test_every_audit_row_is_insertable.py`）
    能抓住「字面 NULL」这个形状，**而它看不见这段代码跑没跑** ——
    本仓那条记法逐字：把 `if` 改成 `if False:` 它照样绿。
    这一条真的开一个事务、真的把两行写进去、真的读回来。

    **这两个函数在此之前没有任何用例调过** ——
    `grep -rn "memories_sent\|memories_cited" tests/` 在本批之前 0 命中。
    「从未成功执行过，因此从未有人发现」就是这么来的。
    """

    connection, inbox_id, event_id = seeded
    conversation_id = uuid.uuid4()
    correlation_id = uuid.uuid4()

    InboxProcessor._record_memories_sent(
        connection,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        fields=["industry", "ring_size"],
        not_shown=1,
    )
    InboxProcessor._record_memories_cited(
        connection,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        offered=["memory:industry", "memory:ring_size"],
        cited=["memory:industry"],
    )

    rows = connection.execute(
        "SELECT action, reason_code, evidence FROM audit_events "
        "WHERE tenant_id = %s AND correlation_id = %s ORDER BY action",
        (TENANT_A, correlation_id),
    ).fetchall()

    assert [row["action"] for row in rows] == [
        "inference.memories_cited",
        "inference.memories_sent",
    ], f"两行没都写进去：{[row['action'] for row in rows]}"
    assert [row["reason_code"] for row in rows] == [
        MEMORIES_CITED_REASON_CODE,
        MEMORIES_SENT_REASON_CODE,
    ], "理由码不是那两个具名常量 —— 而字面 NULL 在这一列上是每次必抛"

    by_action = {row["action"]: row["evidence"] for row in rows}
    # 「两行相减才是『递过去而没被用上的』」—— 那句话要求两行的字段对得上。
    assert by_action["inference.memories_sent"]["fields"] == ["industry", "ring_size"]
    assert by_action["inference.memories_sent"]["not_shown"] == 1
    assert by_action["inference.memories_cited"]["fields"] == ["industry"]
