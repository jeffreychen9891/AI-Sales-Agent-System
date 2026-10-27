"""`064` 那条外键在**连库那一层**真的拒得动，而合法的那一行真的进得去。

## 为什么这份文件必须存在

`scheduled_events.commitment_id` 从 `001_m0.sql:143` 起就在那儿，
**而在 11d 之前它没有任何写入方**（T138 逐字记过：「一根悬空的列」）。
在一根没人写的列上立外键，是一条**今天不可能被违反**的约束 ——
它绿着，什么都不证明。ADR-0139 §三 把这一笔放到最后，正是这个道理。

所以这一批把外键与写入方一起做，而这份文件证的是那条约束**真的会拒**：

1. 指着一条真存在的承诺 → 进得去；
2. 指着一个不存在的 id → **拒**（不是写进去、不是置 NULL）；
3. **指着另一个租户的承诺 → 拒** —— 这一条是复合键那半的理由，
   单列外键会让它过；
4. `NULL` → 进得去，因为 `silence_check` 这类规则本来就没有承诺。

**第 3 条只有连库才验得了**：RLS 管的是读，不管这一次插入引用了谁。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors

from tests.conftest import TENANT_A, TENANT_B, _migration_url

pytestmark = [pytest.mark.security]

CONVERSATION = uuid.UUID("c0000000-0000-4000-8000-00000000cf01")
EVENT = uuid.UUID("e0000000-0000-4000-8000-00000000ef01")


@pytest.fixture()
def wired(seeded_database: dict[str, object]) -> Iterator[dict[str, Any]]:
    """一条真会话 + 一条真承诺（A 租户）+ 一条真承诺（B 租户）。

    **两个租户各一条，是第 3 条用例的全部前提**：跨租户那次引用要想被拒，
    被引用的那一行必须真的存在 —— 否则拒的理由会退化成「那个 id 不存在」，
    而那是第 2 条已经验过的东西。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url()) as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (TENANT_A,)
        ).fetchone()
        assert release is not None, "播种夹具里没有 profile_release —— 换一个取法"
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (TENANT_A, CONVERSATION, release[0]),
        )

        ours = uuid.uuid4()
        theirs = uuid.uuid4()
        _commitment(connection, tenant_id=TENANT_A, conversation_id=CONVERSATION, row_id=ours)

        their_release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (TENANT_B,)
        ).fetchone()
        their_conversation = uuid.uuid4()
        if their_release is not None:
            connection.execute(
                """
                INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (TENANT_B, their_conversation, their_release[0]),
            )
            _commitment(
                connection,
                tenant_id=TENANT_B,
                conversation_id=their_conversation,
                row_id=theirs,
            )
        connection.commit()

        yield {
            "connection": connection,
            "release_id": release[0],
            "ours": ours,
            "theirs": theirs if their_release is not None else None,
        }

        connection.execute("DELETE FROM scheduled_events WHERE tenant_id = %s", (TENANT_A,))
        connection.execute(
            "DELETE FROM commitments WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B)
        )
        connection.execute(
            "DELETE FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_A, CONVERSATION),
        )
        connection.execute(
            "DELETE FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_B, their_conversation),
        )
        connection.commit()


def _commitment(
    connection: Connection, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, row_id: uuid.UUID
) -> None:
    connection.execute(
        """
        INSERT INTO commitments(
            tenant_id, id, conversation_id, party, type, summary, source_event_id,
            due_at, status, evidence_refs, scheduled_event_id, generation, version, dedupe_key
        ) VALUES (%s, %s, %s, 'customer', 'take_action', '客户说下周三之前把材料发过来',
                  %s, now(), 'proposed', %s, NULL, 0, 0, %s)
        ON CONFLICT (tenant_id, conversation_id, dedupe_key) DO NOTHING
        """,
        (tenant_id, row_id, conversation_id, EVENT, json.dumps([str(EVENT)]), f"{row_id}:0"),
    )


def _timer(
    connection: Connection,
    *,
    release_id: Any,
    commitment_id: uuid.UUID | None,
    dedupe_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO scheduled_events(
            tenant_id, id, conversation_id, due_at, kind, dedupe_key, profile_release_id,
            aggregate_generation, target_frame_id, target_stack_version, commitment_id,
            status, created_by_transition_id
        ) VALUES (%s, %s, %s, now(), 'customer_promise_due', %s, %s, 0, %s, 0, %s,
                  'PENDING', %s)
        """,
        (
            TENANT_A,
            uuid.uuid4(),
            CONVERSATION,
            dedupe_key,
            release_id,
            uuid.uuid4(),
            commitment_id,
            uuid.uuid4(),
        ),
    )


def test_a_timer_pointing_at_a_real_commitment_is_accepted(wired: dict[str, Any]) -> None:
    """**先证明它让合法的那一行过** —— 否则下面几条红说明不了什么。

    一条永远拒的约束与一条永远放行的约束一样没用，
    而两者的区别只有把两种情况都跑一遍才看得出来。
    """

    connection = wired["connection"]
    _timer(
        connection,
        release_id=wired["release_id"],
        commitment_id=wired["ours"],
        dedupe_key="customer_promise_due:frame:ok",
    )
    connection.commit()

    stored = connection.execute(
        "SELECT commitment_id FROM scheduled_events WHERE tenant_id = %s AND dedupe_key = %s",
        (TENANT_A, "customer_promise_due:frame:ok"),
    ).fetchone()
    assert stored is not None and stored[0] == wired["ours"]


def test_a_timer_pointing_at_nothing_is_refused(wired: dict[str, Any]) -> None:
    """一个不存在的承诺 id **被拒**，不是被写进去、也不是被悄悄置 NULL。

    这是 11d 之前那根列的处境：没有外键时这一行会安静地进去，
    而「这条定时器挂在哪条承诺上」永远答不出来。
    """

    connection = wired["connection"]
    with pytest.raises(errors.ForeignKeyViolation):
        _timer(
            connection,
            release_id=wired["release_id"],
            commitment_id=uuid.uuid4(),
            dedupe_key="customer_promise_due:frame:ghost",
        )
    connection.rollback()


def test_a_timer_pointing_at_another_tenants_commitment_is_refused(
    wired: dict[str, Any],
) -> None:
    """**复合键那一半的理由**：被引用的那条承诺真的存在，只是不是这个租户的。

    单列外键 `REFERENCES commitments(id)` 会让这一行过 —— RLS 管读不管写，
    于是 A 租户的定时器挂上 B 租户的承诺，两边都查得到、含义是错的。
    """

    if wired["theirs"] is None:
        pytest.skip("播种夹具里没有 B 租户的 release，跨租户那一格这次跑不了")
    connection = wired["connection"]
    with pytest.raises(errors.ForeignKeyViolation):
        _timer(
            connection,
            release_id=wired["release_id"],
            commitment_id=wired["theirs"],
            dedupe_key="customer_promise_due:frame:cross",
        )
    connection.rollback()


def test_a_timer_with_no_commitment_is_still_accepted(wired: dict[str, Any]) -> None:
    """`NULL` 过得去，**而这一条不是形式主义**。

    `silence_check` / `goal_stall` 这些规则真的没有承诺 —— 不是「还没填」。
    `MATCH SIMPLE`（默认）下 NULL 不参与外键检查，这条用例钉的就是那个语义：
    把外键写成 `NOT NULL` 或者 `MATCH FULL` 会让全部会话级定时器写不进去。
    """

    connection = wired["connection"]
    _timer(
        connection,
        release_id=wired["release_id"],
        commitment_id=None,
        dedupe_key="silence_check:frame",
    )
    connection.commit()

    stored = connection.execute(
        "SELECT commitment_id FROM scheduled_events WHERE tenant_id = %s AND dedupe_key = %s",
        (TENANT_A, "silence_check:frame"),
    ).fetchone()
    assert stored is not None and stored[0] is None
