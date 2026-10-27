"""One intent, one outbox row -- asserted rather than assumed (T86 / ADR-0095 §四).

**What this file is for.** ADR-0095 §四 ruled that a manager notification gets
its own intent rather than sharing the customer message's, and the read-only
check that ADR asked for (T197, B1-4, 2026-09-08) found the ruling **already
satisfied**: all three places that insert into `outbox_messages` mint their own
`intent_id`, and have since `35f4770` introduced the notice on 2026-09-04.

**And the database has been enforcing it since `001`.** `outbox_messages`
carries `UNIQUE (tenant_id, intent_id)` from the first migration, and no
migration since has dropped it. So the shape T86 feared --

> M1b 让 `audience != 'customer'` 的行可被认领时，同一个 intent 会有两行同时被
> 两个 dispatcher 持有，届时两边都会去写同一条 intent 的终态而互不知情。

-- was never reachable: a fourth call site reusing an intent would not create a
second row, it would fail to insert. **That is a stronger guarantee than the one
the read-only check first reported, and it was missed because that check read
the code and not the schema** (corrected in the T197 record, §五之二).

**What this file is for, then.** Two things a constraint alone does not do:

1. it says the invariant out loud, in a place a reader meets it;
2. **it goes red the day someone drops the constraint** -- which a migration can
   do, and which nothing else would notice, because every call site would keep
   working right up until two rows appeared.

So it asserts both halves: the constraint is still in the catalogue, and the
real paths produce rows that satisfy it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.digest import ManagerDigest
from fastagent_v2.worker.pipeline import OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _migration_connection() -> Connection[dict[str, Any]]:
    return Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    )


def _seed_manager(tenant_id: UUID) -> None:
    """A manager to notify -- without one the digest writes nothing."""

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO tenant_manager_contacts(
                tenant_id, channel, config_id, external_user_id
            ) VALUES (%s, 'wecom_proxy', 'config-a', 'manager-userid-t86')
            ON CONFLICT (tenant_id) DO UPDATE SET external_user_id = excluded.external_user_id
            """,
            (tenant_id,),
        )
        connection.commit()


def _a_delivery_that_ends_unknown(
    *, settings: Settings, seeded_database: dict[str, Any], conversation_id: UUID
) -> None:
    from test_retry_budget import _deliver_to_outbox

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def nothing_landed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"detail": "upstream exploded"})
        return httpx.Response(404, json={"detail": "not found"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(nothing_landed)
        ),
        dispatcher_id="t86-sender",
    ).dispatch_one()
    assert dispatched is not None and dispatched.receipt_status == "unknown"


def test_no_two_outbox_rows_share_an_intent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two customer messages and a manager digest: three rows, three intents.

    **The count assertion is not decoration.** An invariant of the form "no
    duplicates exist" is also satisfied by an empty table, and a broken filter
    reports the same emptiness a clean repository does (the lesson B2-1's
    independent review left behind). So this first insists the rows are there,
    then insists they do not share.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)

    for _ in range(2):
        _a_delivery_that_ends_unknown(
            settings=settings,
            seeded_database=seeded_database,
            conversation_id=new_conversation(seeded_database["tenant_a"]),
        )

    # The digest writes the third row, through the path that has its own intent
    # *and* its own `_write` docstring saying so.
    digest = ManagerDigest(database=Database(settings.worker_database_url))
    assert digest.run_for(tenant_id=tenant_id, now=datetime.now(UTC)).outbox_id is not None

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT intent_id, count(*) AS n FROM outbox_messages "
            " WHERE tenant_id = %s GROUP BY intent_id ORDER BY n DESC",
            (tenant_id,),
        ).fetchall()

    assert len(rows) >= 3, (
        "this case proves nothing unless the rows it is about actually exist; "
        f"found {len(rows)} outbox rows for the tenant"
    )
    shared = [row for row in rows if row["n"] > 1]
    assert not shared, (
        "two outbox rows share an intent -- the shape ADR-0095 §四 ruled against "
        f"and T86 described: {shared}"
    )


def test_the_constraint_that_makes_it_impossible_is_still_there() -> None:
    """The invariant above is the database's, and this is where it lives.

    `001` gave `outbox_messages` a `UNIQUE (tenant_id, intent_id)`. Everything
    else in this file describes behaviour that follows from it; **this asserts
    the cause rather than the effect**, because a migration can drop a
    constraint and nothing else here would notice until two rows appeared in
    production.

    Read from the catalogue, not from the migration text: what matters is what
    the database is enforcing now, not what a file once said.
    """

    with _migration_connection() as connection:
        found = connection.execute(
            """
            SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = 'outbox_messages' AND c.contype = 'u'
            """
        ).fetchall()

    definitions = [row["definition"].replace(" ", "") for row in found]

    # ⚠️ **2026-09-23（ADR-0152）：两列变三列，而这条用例守的东西没变。**
    #
    # 旧的 `UNIQUE (tenant_id, intent_id)` 顺带执行了「一个 intent 一行」，
    # 而用户 2026-09-23 裁定「一次生成是一次判定」之后，
    # **一次判定要铺成几行**（几个气泡），那条约束因此放松成三列（迁移 `076`）。
    #
    # **上面 docstring 里 T86 担心的那个形状仍然要挡**：
    #
    # > 同一个 intent 会有两行同时被两个 dispatcher 持有，
    # > 届时两边都会去写同一条 intent 的终态而互不知情。
    #
    # 今天挡它的**不再是这条约束**，而是两件，各有自己的用例：
    #
    # 1. **同一个 intent 的同一段只许一行** —— 就是下面这条三列约束；
    # 2. **intent 的终态按「最差的那一段」算**（`INTENT_STATUS_SEVERITY`）——
    #    所以「两边都写终态」不再是一次覆盖，而是一次**单调取最坏**。
    #    用户 2026-09-23 裁定：「一条消息只要有一段没到，那次判定就不算完成」。
    #
    # ⚠️ **而那第 2 件是本批新开的洞，不是本来就有的**：放松约束之后
    # 第 2 段以后的投递结果本来会写不进去（`WHERE status='EXECUTING'` 不匹配），
    # **全量门禁靠这条用例把它顶了出来** —— 它就是为这一刻写的。
    assert any("UNIQUE(tenant_id,intent_id,ordinal)" in d for d in definitions), (
        "outbox_messages lost UNIQUE (tenant_id, intent_id, ordinal) -- with it "
        "goes the only thing keeping one judgement's segments from doubling up; "
        f"found: {[row['definition'] for row in found]}"
    )
    assert not any(d == "UNIQUE(tenant_id,intent_id)" for d in definitions), (
        "旧的两列约束回来了 —— 它禁止「一次判定铺成几个气泡」（ADR-0152）"
    )
