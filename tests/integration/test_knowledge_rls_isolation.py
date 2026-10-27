"""三张知识库表的隔离，**这一次是走出来的，不是读出来的**。

**第 3 批自审 §六.1 欠的就是这一条**，原话：

> 三条 RLS 策略今天没有任何运行期角色在走。用例走的是 `fastagent_migrator`，
> 它不受策略约束 —— **这意味着本批的用例没有证明隔离真的隔离**，
> 只证明了策略存在且语法正确。**证明要等第一个被授权的角色出现（第 4 批）。**

**那句话是对的，而本文件的第一版以为它错了。** 第一版的推理是：
三张表都 `FORCE ROW LEVEL SECURITY`，而 `FORCE` 的全部作用就是让表的属主也受约束，
所以在迁移连接上就能把隔离走完。

**跑出来不是那样**：不设 `app.tenant_id` 时它看见了两个租户的行。
`infrastructure/local/compose.yml:15` 把 `POSTGRES_USER` 设成
`fastagent_migrator`，**所以那个角色是集群超级用户**，而超级用户**整个绕过**
行级安全 —— `FORCE` 解的是**属主**的豁免，解不了这一个，
因为后者不是一条豁免，是那道检查根本没跑。

**留痕的理由**：错的那一版是讲得通的那一版。`FORCE` 确实是那个意思，
只是不针对这个角色。

所以这一份走的是 **`fastagent_app`**（`NOSUPERUSER`，`migrate.py:58`），
它在 `051` 里拿到了这两张表的第一条 `GRANT`，**而调用方就是第 4 批的导入脚本**。

`test_rls_policies_exist.py` 问的是「策略在不在、绑没绑 `app.tenant_id`」——
那是结构。**这一份问的是「换一个租户还读不读得到」**，那是行为。
两者都要：一条 `USING (true)` 的策略过得了结构那一关。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection, errors, sql

from tests.conftest import TENANT_A, TENANT_B

pytestmark = [pytest.mark.security]

RELEASE_A = uuid.UUID("0dd1e5e0-0000-4000-8000-0000000000a1")
RELEASE_B = uuid.UUID("0dd1e5e0-0000-4000-8000-0000000000b1")


def _as_tenant(connection: Connection, tenant: uuid.UUID) -> None:
    connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant),))


def _insert(connection: Connection, table: str, row: dict[str, Any]) -> None:
    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(name) for name in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    )


def _body_of(slug: str) -> str:
    return f"{slug} 的正文。"


def _hash_of(slug: str) -> str:
    """**真的 hash。**

    先前这里（与 `_chunk_row`）写的是 `"c" * 64` —— 而独立审查
    2026-09-12 P2-1 正是拿这一处当反例：「给一段真正文写 `'c'*64`，
    所有约束都满意」，所以「同一个 slug:vN 到哪儿都是同一段正文」
    **只保证到 hash 那一层**。`052` 补了
    `knowledge_chunks_text_hash_matches_body` 之后，这个 fixture 必须算真的。
    """

    return hashlib.sha256(_body_of(slug).encode("utf-8")).hexdigest()


def _identity(connection: Connection, tenant: uuid.UUID, slug: str) -> None:
    """`052` 的身份行 —— chunk 那条四列外键要它先在（T279）。

    **这个 helper 是 `052` 加进来的**：本文件是第 4 批写的，
    而第 5 批给 `knowledge_chunks` 加了外键之后，这个 fixture 当场红了。
    **那正是那条外键在干的事**，所以这里补的是 fixture，不是约束。
    """

    connection.execute(
        "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
        " VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING",
        (tenant, slug, _hash_of(slug)),
    )


def _release_row(tenant: uuid.UUID, release_id: uuid.UUID, *, content: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "id": release_id,
        "version": 1,
        "status": "draft",
        "embedding_model": "probe-embedding",
        "embedding_dimension": 2048,
        "distance_metric": "cosine",
        "normalization": "l2",
        "truncation_policy": "reject-over-limit",
        "authoring_spec": "probe",
        "content_hash": content,
    }


def _chunk_row(tenant: uuid.UUID, release_id: uuid.UUID, *, slug: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "id": uuid.uuid4(),
        "release_id": release_id,
        "document_ref": "docs/probe.md",
        "document_version": "1",
        "ordinal": 0,
        "ref": f"knowledge:{slug}:v1",
        "slug": slug,
        "chunk_version": 1,
        "question": "探针问题",
        "content_type": "实测数据",
        "confidence": "已实测",
        "body": _body_of(slug),
        "text_hash": _hash_of(slug),
        "source_spans": json.dumps([{"file": "docs/probe.md", "section": "probe"}]),
    }


@pytest.fixture()
def two_tenants(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """两个租户各有一版资料，**在同一条连接上分别以各自的身份写进去**。

    写的时候就已经在走策略了：`WITH CHECK` 那一半不允许把一行写进别人的租户。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_app_url()) as connection:
        _as_tenant(connection, TENANT_A)
        _insert(
            connection, "knowledge_releases", _release_row(TENANT_A, RELEASE_A, content="a" * 64)
        )
        _identity(connection, TENANT_A, "kb-a")
        _insert(connection, "knowledge_chunks", _chunk_row(TENANT_A, RELEASE_A, slug="kb-a"))

        _as_tenant(connection, TENANT_B)
        _insert(
            connection, "knowledge_releases", _release_row(TENANT_B, RELEASE_B, content="b" * 64)
        )
        _identity(connection, TENANT_B, "kb-b")
        _insert(connection, "knowledge_chunks", _chunk_row(TENANT_B, RELEASE_B, slug="kb-b"))
        yield connection


def _app_url() -> str:
    return Settings.from_env().database_url


def test_the_role_under_test_is_not_a_superuser() -> None:
    """**先证明这条连接上的测量有意义。**

    一个超级用户会让下面每一条「读不到别人的」都因为「它其实读得到全部、
    而我只查了一个租户」而偶然成立 —— 那正是本文件第一版踩的。
    **所以先问数据库这个角色是谁。**
    """

    with Connection.connect(_app_url()) as connection:
        # 默认 row factory 给的是元组，不是字典 —— 按位置取。
        who, is_super = connection.execute(
            "SELECT current_user, usesuper FROM pg_user WHERE usename = current_user"
        ).fetchone()
        assert who == "fastagent_app", who
        assert not is_super, (
            "本文件测的这个角色是超级用户，它整个绕过 RLS，下面每一条都无效"
        )


@pytest.mark.parametrize(
    "table", ["knowledge_releases", "knowledge_chunks", "knowledge_chunk_identity"]
)
def test_one_tenant_cannot_read_the_other(two_tenants: Connection, table: str) -> None:
    """A 读不到 B 的行，B 读不到 A 的行。**两个方向都要，不是一个。**

    只查一个方向时，一条「永远返回空」的坏策略也会绿。
    """

    connection = two_tenants
    _as_tenant(connection, TENANT_A)
    seen_by_a = {
        row[0]
        for row in connection.execute(
            sql.SQL("SELECT tenant_id FROM {}").format(sql.Identifier(table))
        ).fetchall()
    }
    _as_tenant(connection, TENANT_B)
    seen_by_b = {
        row[0]
        for row in connection.execute(
            sql.SQL("SELECT tenant_id FROM {}").format(sql.Identifier(table))
        ).fetchall()
    }

    assert seen_by_a == {TENANT_A}, f"{table}: A 看见了 {seen_by_a}"
    assert seen_by_b == {TENANT_B}, f"{table}: B 看见了 {seen_by_b}"


def test_naming_the_other_tenant_explicitly_does_not_help(two_tenants: Connection) -> None:
    """**点名对方的 id 也读不到** —— 这条与上一条不是同一件事。

    上一条证明「不带条件查只看得见自己的」；这一条证明**策略不是一个默认过滤器**，
    知道对方主键也绕不过去。跨租户读的真实形状是后者：
    调用方手里有一个 id，它只是不属于它。
    """

    connection = two_tenants
    _as_tenant(connection, TENANT_A)
    found = connection.execute(
        "SELECT id FROM knowledge_releases WHERE id = %s", (RELEASE_B,)
    ).fetchall()
    assert found == [], "A 点名 B 的 release id 就读到了它"


def test_a_tenant_cannot_write_a_row_into_another_tenant(two_tenants: Connection) -> None:
    """`WITH CHECK` 那一半：**以 A 的身份写一行 B 的数据，要被拒。**

    只有 `USING` 的策略读得干净而写得进去 —— 而写进去的那一行
    从此只有 B 看得见，**A 这边连自己刚写了什么都查不到**。
    """

    connection = two_tenants
    _as_tenant(connection, TENANT_A)
    with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
        _insert(
            connection,
            "knowledge_releases",
            _release_row(TENANT_B, uuid.uuid4(), content="d" * 64),
        )


def test_the_tenant_setting_being_absent_shows_nothing_rather_than_everything(
    two_tenants: Connection,
) -> None:
    """**没设 `app.tenant_id` 时是看不见，不是看得见全部。**

    策略写的是 `tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid`，
    而 `NULL = anything` 是 NULL、不是 true —— 所以失败方向是关着的。
    **这一条是那个 `nullif` 的理由**：没有它，空串会去 `::uuid` 上抛错，
    而抛错与「看不见」在一条 `SELECT` 上长得不一样。
    """

    connection = two_tenants
    connection.execute("SELECT set_config('app.tenant_id', '', false)")
    for table in ("knowledge_releases", "knowledge_chunks", "knowledge_chunk_identity"):
        rows = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
        assert rows[0] == 0, f"{table}: 没有租户身份时读到了 {rows[0]} 行"
