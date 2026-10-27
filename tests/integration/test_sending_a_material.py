"""发送那一刻的解析：**当前那一版**，以及 `068` 给 worker 的那条读权。

## 这里守的那条错，是这一格最安静的一种

`asset:price-list:v1` 里的 `v1` 是**语法占位** ——
ADR-0130 §二.2 逐字：「profile 声明 slug 不声明版本，**发送时解析当前那一版**」。

照着引用里的版本发：发得出去、供应商回成功、客户收到一个文件，
**而那是个旧版本**。价格表改过之后还在发上一版，没有任何东西会红。
契约层证不了它 —— 「当前那一版」是数据库对一句 `ORDER BY version DESC` 的回答。

## `068` 那条读权也在这里验

worker 是唯一读得到那两列凭据的角色，**而它一个字都写不了**。
入站写得进读不回来（`067`），worker 读得到写不进去 —— 两边各拿一半。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection, errors
from psycopg.rows import dict_row

from tests.conftest import TENANT_A, _migration_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/worker/src"))
from fastagent_v2.worker.pipeline import resolve_asset_for_send  # noqa: E402

pytestmark = [pytest.mark.security]


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url(), row_factory=dict_row) as conn:
        yield conn
        conn.execute("DELETE FROM materials WHERE tenant_id = %s", (TENANT_A,))
        conn.commit()


def _insert(
    conn: Connection,
    *,
    slug: str,
    version: int,
    uploaded: bool = True,
    status: str = "active",
    filename: str | None = None,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type, byte_size,
            content_hash, status, archived_at, source_url,
            provider_file_id, provider_file_aes_key, provider_file_md5, provider_file_size
        ) VALUES (%s, %s, %s, %s, %s, 'application/pdf', 1024, %s, %s, %s,
                  'https://example.test/p.pdf', %s, %s, %s, %s)
        """,
        (
            TENANT_A,
            row_id,
            slug,
            version,
            filename or f"{slug}-v{version}.pdf",
            "a" * 64,
            status,
            datetime.now(UTC) if status == "archived" else None,
            f"file-{slug}-v{version}" if uploaded else None,
            "aes-key" if uploaded else None,
            "md5" if uploaded else None,
            2048 if uploaded else None,
        ),
    )
    return row_id


def test_the_current_version_is_sent_not_the_one_in_the_ref(connection: Connection) -> None:
    """**引用里写 `v1`，发出去的是 `v3`** —— 那个 `v1` 是语法占位。

    这一条要是反了，症状是「价格表改过之后还在发上一版」：
    发得出去、回成功、没有任何东西变红。
    """

    _insert(connection, slug="price-list", version=1)
    _insert(connection, slug="price-list", version=2)
    _insert(connection, slug="price-list", version=3)

    content, refusal = resolve_asset_for_send(
        connection, tenant_id=TENANT_A, reference="asset:price-list:v1"
    )

    assert refusal is None
    assert content is not None
    assert content["file_id"] == "file-price-list-v3", (
        f"发的是 {content['file_id']} —— 照着引用里的版本取了，"
        "而 ADR-0130 §二.2 说发送时解析当前那一版"
    )
    assert content["filename"] == "price-list-v3.pdf"


def test_an_archived_version_is_not_the_current_one(connection: Connection) -> None:
    """归档的版本不算「当前」，哪怕它的版本号最大。

    旧版本置 `archived` 而不是删（`065`）—— 只按版本号取最大的那一行，
    会把刚刚被撤下的那一版发出去。
    """

    _insert(connection, slug="price-list", version=1)
    _insert(connection, slug="price-list", version=2, status="archived")

    content, refusal = resolve_asset_for_send(
        connection, tenant_id=TENANT_A, reference="asset:price-list:v2"
    )

    assert refusal is None
    assert content is not None
    assert content["file_id"] == "file-price-list-v1"


def test_a_material_that_was_never_uploaded_is_refused_with_the_fix_in_it(
    connection: Connection,
) -> None:
    """**最常见的那一种**：导了目录而没跑上传脚本。

    拒绝的理由里带着修法。让它带着空的 `fileId` 去 `sendFile`，
    换回来的只有 `code` / `msg` —— 读日志的人看不出是哪一种。
    """

    _insert(connection, slug="price-list", version=1, uploaded=False)

    content, refusal = resolve_asset_for_send(
        connection, tenant_id=TENANT_A, reference="asset:price-list:v1"
    )

    assert content is None
    assert refusal is not None
    assert "upload_materials.py" in refusal, f"拒绝理由里没有修法：{refusal}"


def test_a_slug_the_catalogue_never_heard_of_is_refused(connection: Connection) -> None:
    """包声明了而目录里没有 —— 另一种，另一个理由。"""

    content, refusal = resolve_asset_for_send(
        connection, tenant_id=TENANT_A, reference="asset:nothing-here:v1"
    )

    assert content is None
    assert refusal is not None
    assert "nothing-here" in refusal


def test_the_worker_role_reads_the_credentials_and_can_write_nothing(
    connection: Connection,
) -> None:
    """**`068` 的那条边界，真跑一次。**

    三段：读得到密钥（授权到位）· 读不到 `source_url`（没给的那些真没给）·
    **写不动任何一列**（它只有 `SELECT`）。

    第三段用 `UPDATE` 一列它读得到的（`provider_file_id`）——
    读得到而写不动，正是「只发了 SELECT」的样子；
    拿一列它读不到的去试，分不清拒的是读还是写。
    """

    _insert(connection, slug="price-list", version=1)
    connection.commit()

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_worker")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
    try:
        row = connection.execute(
            "SELECT provider_file_aes_key, provider_file_md5 FROM materials"
        ).fetchone()
        assert row is not None, "worker 读不到自己那一行 —— 068 的 SELECT 没到位，或者 RLS 挡了"
        assert row["provider_file_aes_key"] == "aes-key"

        connection.execute("SAVEPOINT no_source")
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SELECT source_url FROM materials")
        connection.execute("ROLLBACK TO SAVEPOINT no_source")

        connection.execute("SAVEPOINT no_write")
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("UPDATE materials SET provider_file_id = 'x'")
        connection.execute("ROLLBACK TO SAVEPOINT no_write")
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")


def test_the_worker_grants_are_exactly_these_columns(connection: Connection) -> None:
    """**按相等钉住授权面** —— 一条一条问，漏掉的那一列永远不会被问到。

    `067` 那次就是这么栽的：少发一列，而报文
    `permission denied for table materials` **不提是哪一列**。
    """

    granted: dict[str, set[str]] = {}
    for row in connection.execute(
        """
        SELECT privilege_type, column_name FROM information_schema.column_privileges
         WHERE table_name = 'materials' AND grantee = 'fastagent_worker'
        """
    ).fetchall():
        granted.setdefault(row["privilege_type"], set()).add(row["column_name"])

    readable = {
        "tenant_id",
        "id",
        "slug",
        "version",
        "status",
        "archived_at",
        "filename",
        "provider_file_id",
        "provider_file_aes_key",
        "provider_file_md5",
        "provider_file_size",
    }
    assert granted.get("SELECT", set()) == readable, (
        f"worker 的可读列变了。多出来：{sorted(granted.get('SELECT', set()) - readable)}；"
        f"少掉了：{sorted(readable - granted.get('SELECT', set()))}"
    )
    assert set(granted) == {"SELECT"}, (
        f"worker 在 materials 上拿到了写权限：{sorted(granted)}。"
        "发送只需要读 —— 认领是入站那一侧的事（067）"
    )
