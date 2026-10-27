"""发过的文件怎么被记下来 —— **以及它不许漏进算阶段的那份清单**。

用户 2026-09-17 裁定：「除非客户明确要，不然资料只能主动发一次。」
压住它要的数据在 `intent_records` 里本来就有，这份验的是**读它的那句 SQL**。

## 那条负向断言是这份的重点

`_delivered_materials` 喂给 `current_stage`，而阶段的 `after` **只收话术 ref**
（封版时就拒 asset）。把文件 ref 混进那一份，`after <= set(delivered)`
只会更容易成立 —— 也就是**悄悄把阶段推快**，而没有任何东西会红。

两份清单装的是两件事，所以这里既验「文件进了文件那份」，
也验「文件**没有**进话术那份」。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.worker.pipeline import InboxProcessor
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.conftest import PROFILE_RELEASE, TENANT_A, _migration_url

pytestmark = [pytest.mark.security]

CONVERSATION = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url(), row_factory=dict_row) as conn:
        # **这一行是必需的，不是布景**：`intent_records.conversation_id` 有
        # 一条指向 `conversation_aggregates` 的外键。第一版没建它，
        # 门禁里三条全红 —— 而契约层看不见这件事（它根本不连库）。
        #
        # 其余列都有默认值，所以最小只要这三个。
        conn.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (TENANT_A, CONVERSATION, PROFILE_RELEASE),
        )
        conn.commit()
        yield conn
        conn.execute(
            "DELETE FROM intent_records WHERE tenant_id = %s AND conversation_id = %s",
            (TENANT_A, CONVERSATION),
        )
        conn.execute(
            "DELETE FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_A, CONVERSATION),
        )
        conn.commit()


def _intent(conn: Connection, *, kind: str, proposal: dict[str, Any], status: str) -> None:
    conn.execute(
        """
        INSERT INTO intent_records(
            tenant_id, id, conversation_id, kind, status, proposal,
            proposed_by, strategy_id, target_frame_id, idempotency_key, preconditions
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'seed', %s, %s, %s)
        """,
        (
            TENANT_A,
            uuid.uuid4(),
            CONVERSATION,
            kind,
            status,
            Jsonb(proposal),
            Jsonb({"step": "generate", "version": "test"}),
            uuid.uuid4(),
            f"{kind}:{uuid.uuid4()}",
            Jsonb({}),
        ),
    )


def test_an_asset_that_went_out_is_remembered(connection: Connection) -> None:
    """排进出站就算发过 —— `EXECUTING` 与 `APPLIED` 都算。

    等确认送达才算，会让同一份文件在投递那几秒里被再提一次，
    **而那正是这条规则要防的样子**。
    """

    _intent(connection, kind="send_material", proposal={"ref": "asset:a:v1"}, status="EXECUTING")
    _intent(connection, kind="send_material", proposal={"ref": "asset:b:v1"}, status="APPLIED")

    sent = InboxProcessor._delivered_assets(  # noqa: SLF001 - 直接验那句 SQL
        connection, tenant_id=TENANT_A, conversation_id=CONVERSATION
    )

    # **按集合断言，不按顺序** —— 第一版按顺序写，门禁里红成 (b, a)。
    #
    # 那句 SQL 是 `ORDER BY created_at`，而这两行在**同一个事务**里插入：
    # `created_at` 的默认值 `now()` 是**事务时间**，两行相同，于是它们之间没有定序。
    # 真实流量里每一轮各自一个事务，所以不会撞 —— 是这条用例造了个平局。
    #
    # 而这个值的用途是**成员判断**（`already_sent` 只被 `in` 用到），
    # 顺序从来不重要。按顺序断言等于替 SQL 声称一件它没保证的事。
    assert set(sent) == {"asset:a:v1", "asset:b:v1"}


def test_a_proposal_that_never_went_out_is_not_remembered(connection: Connection) -> None:
    """只提过没发出去的不算 —— 否则一次被拒的提案会把那份文件永久压住。"""

    _intent(connection, kind="send_material", proposal={"ref": "asset:a:v1"}, status="PROPOSED")

    sent = InboxProcessor._delivered_assets(  # noqa: SLF001
        connection, tenant_id=TENANT_A, conversation_id=CONVERSATION
    )

    assert sent == ()


def test_an_asset_ref_never_reaches_the_list_that_moves_the_stage(
    connection: Connection,
) -> None:
    """**负向那条**：文件 ref 不进算阶段的那份清单。

    混进去只会让 `after <= set(delivered)` 更容易成立 ——
    **悄悄把阶段推快，而没有任何东西会红**。
    """

    _intent(connection, kind="send_material", proposal={"ref": "asset:a:v1"}, status="APPLIED")
    _intent(
        connection,
        kind="send_message",
        proposal={"content": {"segments": [
            {"type": "material_ref", "content_or_ref": "material:m1:v1"}
        ]}},
        status="APPLIED",
    )

    for_stages = InboxProcessor._delivered_materials(  # noqa: SLF001
        connection, tenant_id=TENANT_A, conversation_id=CONVERSATION
    )
    for_assets = InboxProcessor._delivered_assets(  # noqa: SLF001
        connection, tenant_id=TENANT_A, conversation_id=CONVERSATION
    )

    assert for_stages == ("material:m1:v1",), f"算阶段那份里混进了别的：{for_stages}"
    assert for_assets == ("asset:a:v1",)
