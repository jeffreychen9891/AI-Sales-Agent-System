"""`contact_memories` 拒绝 `054` 的注释说它拒绝的东西。

**与 `test_materials_table.py` 同批同因**（2026-09-12 独立审查：
`materials_immutable` 曾是全仓唯一没有用例的不可变触发器，本表的触发器同型）。

**这里最要紧的两条**：

* `test_a_revision_cannot_point_at_another_contacts_memory` —— P2-2 那条
  四列外键。没有它，「这条记忆的上一版是什么」能答出一个跨客户的答案，
  **而没有任何东西会红**；
* `test_superseding_is_allowed` —— 与 materials 的归档同理，
  **它是触发器存在的理由，也是唯一一条错了会安静的分支**。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors

from tests.conftest import EMPLOYEE_A, TENANT_A, TENANT_B, _migration_url

pytestmark = [pytest.mark.security]

CONTACT_A = uuid.UUID("cc000000-0000-4000-8000-0000000000a1")
CONTACT_B = uuid.UUID("cc000000-0000-4000-8000-0000000000a2")


@pytest.fixture()
def tenant(seeded_database: dict[str, object]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url()) as connection:
        # **`config_id` 用播种夹具建好的 `'config-a'`，不能自己编一个**：
        # `contacts` 有 `(tenant_id, config_id) → channel_bindings` 的外键，
        # 编一个字符串会在 fixture setup 就炸（本文件第一版写的是 `'cfg'`，
        # 门禁给出 12 个 setup error）。
        for contact in (CONTACT_A, CONTACT_B):
            connection.execute(
                "INSERT INTO contacts (tenant_id, id, config_id, external_user_id) "
                "VALUES (%s, %s, %s, %s)",
                (TENANT_A, contact, "config-a", str(contact)),
            )
        yield connection


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "id": uuid.uuid4(),
        "contact_id": CONTACT_A,
        "field_key": "ring_size",
        "field_value": "18",
        "source_event_id": None,
        # **E4-3（`060`）之后，每一行必须恰好有一个来源。**
        #
        # 本文件写成的时候这两列还不存在，`source_event_id: None` 是一个
        # 无关紧要的默认值；`060` 加上
        # `CHECK (num_nonnulls(source_event_id, authorised_by) = 1)` 之后，
        # **「两个来源都没有」成了一个被禁的形状**，这十二条于是全部在 `_insert`
        # 上炸掉。改的是夹具的默认值，不是任何一条断言——
        # **本文件钉的每一条不变量都还钉着原来那一条。**
        #
        # 取「员工授权」那一半而不是「客户事件」那一半，只是因为它便宜：
        # 员工是播种夹具建好的，而造一条 `inbox_events` 要先有 release 与会话。
        "authorised_by": EMPLOYEE_A,
        "authorisation_note": "E4-1 的表用例：这一行要有一个来源",
        "revision": 1,
        "supersedes": None,
        "status": "active",
        "tombstoned_at": None,
    }
    row.update(overrides)
    return row


def _insert(connection: Connection, row: dict[str, Any]) -> uuid.UUID:
    connection.execute(
        """
        INSERT INTO contact_memories
            (tenant_id, id, contact_id, field_key, field_value, source_event_id,
             revision, supersedes, status, tombstoned_at,
             authorised_by, authorisation_note)
        VALUES (%(tenant_id)s, %(id)s, %(contact_id)s, %(field_key)s, %(field_value)s,
                %(source_event_id)s, %(revision)s, %(supersedes)s, %(status)s,
                %(tombstoned_at)s, %(authorised_by)s, %(authorisation_note)s)
        """,
        row,
    )
    return row["id"]  # type: ignore[no-any-return]


def _as_unprivileged(connection: Connection, tenant_id: uuid.UUID) -> None:
    """切到一个**非超级用户**角色，理由同 `test_materials_table.py` 的同名函数。

    `_migration_url()` 是 superuser，**绕过 RLS，`FORCE` 也拦不住**；
    而 `054` 刻意不发 `GRANT`，所以既有应用角色对本表一个权限都没有。
    **这条 `GRANT` 是脚手架，不是迁移的行为**。
    **角色本身不随事务回滚**（cluster 级对象），所以是幂等创建。
    """

    # **幂等创建**：角色是 cluster 级对象，不随事务回滚（E3-2 那批的门禁抓到过）。
    connection.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memories_rls_probe') "
        "THEN CREATE ROLE memories_rls_probe NOLOGIN; END IF; END $$"
    )
    connection.execute("GRANT SELECT, INSERT ON contact_memories TO memories_rls_probe")
    connection.execute("SET ROLE memories_rls_probe")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def _switch_tenant(connection: Connection, tenant_id: uuid.UUID) -> None:
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def test_superseding_is_allowed(tenant: Connection) -> None:
    """`active → superseded` 必须通过——**错了会安静**：修正写不进去，而旧值继续被用。"""

    first = _insert(tenant, _row())
    tenant.execute(
        "UPDATE contact_memories SET status = 'superseded' WHERE id = %s", (first,)
    )
    _insert(tenant, _row(revision=2, supersedes=first, field_value="19"))
    rows = tenant.execute(
        "SELECT status FROM contact_memories WHERE contact_id = %s ORDER BY revision",
        (CONTACT_A,),
    ).fetchall()
    assert rows == [("superseded",), ("active",)]


def test_a_revision_cannot_point_at_another_contacts_memory(tenant: Connection) -> None:
    """**P2-2 那条四列外键。**

    没有它，修订链能跨客户，而「上一版是什么」会答出别人的事实。
    """

    theirs = _insert(tenant, _row(contact_id=CONTACT_B))
    with pytest.raises(errors.ForeignKeyViolation):
        _insert(tenant, _row(contact_id=CONTACT_A, revision=2, supersedes=theirs))


def test_a_revision_cannot_point_at_another_field(tenant: Connection) -> None:
    """同一条外键的另一半：跨字段也不行。"""

    other = _insert(tenant, _row(field_key="budget"))
    with pytest.raises(errors.ForeignKeyViolation):
        _insert(tenant, _row(field_key="ring_size", revision=2, supersedes=other))


@pytest.mark.parametrize(
    ("column", "value"),
    [("field_key", "budget"), ("field_value", "20"), ("revision", 3)],
)
def test_the_substance_is_immutable(tenant: Connection, column: str, value: Any) -> None:
    memory = _insert(tenant, _row())
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            f"UPDATE contact_memories SET {column} = %s WHERE id = %s",  # noqa: S608
            (value, memory),
        )


def test_un_tombstoning_is_refused(tenant: Connection) -> None:
    memory = _insert(
        tenant, _row(status="tombstoned", tombstoned_at="2026-09-12T00:00:00Z")
    )
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            "UPDATE contact_memories SET status = 'active', tombstoned_at = NULL "
            "WHERE id = %s",
            (memory,),
        )


def test_the_tombstone_timestamp_is_written_once(tenant: Connection) -> None:
    """P3-1：「我们什么时候忘掉它的」是事实不是字段。"""

    memory = _insert(
        tenant, _row(status="tombstoned", tombstoned_at="2026-09-12T00:00:00Z")
    )
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            "UPDATE contact_memories SET tombstoned_at = now() WHERE id = %s", (memory,)
        )


def test_a_superseded_memory_cannot_become_active(tenant: Connection) -> None:
    memory = _insert(tenant, _row())
    tenant.execute(
        "UPDATE contact_memories SET status = 'superseded' WHERE id = %s", (memory,)
    )
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            "UPDATE contact_memories SET status = 'active' WHERE id = %s", (memory,)
        )


def test_two_active_memories_for_one_field_are_refused(tenant: Connection) -> None:
    """「这个客户的手寸是多少」不许有两个答案。"""

    _insert(tenant, _row())
    with pytest.raises(errors.UniqueViolation):
        _insert(tenant, _row())


def test_revision_one_may_not_supersede(tenant: Connection) -> None:
    first = _insert(tenant, _row(field_key="budget"))
    with pytest.raises(errors.CheckViolation):
        _insert(tenant, _row(revision=1, supersedes=first))


def test_one_tenant_cannot_see_another_tenants_memories(tenant: Connection) -> None:
    """两租户可见性双向——manifest 驱动那条用例证不到这一层。"""

    _as_unprivileged(tenant, TENANT_A)
    _insert(tenant, _row())

    _switch_tenant(tenant, TENANT_B)
    assert tenant.execute("SELECT count(*) FROM contact_memories").fetchone() == (0,)

    _switch_tenant(tenant, TENANT_A)
    assert tenant.execute("SELECT count(*) FROM contact_memories").fetchone() == (1,)
