"""`067`：入站侧认领上传结果 —— **写得进那四列，而密钥读不回来**。

## 这一条守的是一次面向公网的服务拿到的写权

用户 2026-09-16 裁定 ingress 直接认领。授权因此发到了**面向公网的那个服务**上，
所以边界不能只写在注释里：

| 它该能做的 | 它该做不到的 |
|---|---|
| 把回调带回的四列写进那一行 | **把任何一行的密钥读出来** |
| 按 `upload_request_id` 找到是哪一行 | 读 slug / 文件名 / 地址 / 哈希 |
| 看出那一行认领过没有 | —— |
| 认领**本租户**的行 | 碰到另一个租户的行（RLS） |

列级 `UPDATE` 与列级 `SELECT` 是两份独立的授权，PostgreSQL 会保证它 ——
而 **`066` 把这张表从表级改成列级正是为了今天**：那时它还是表级 `SELECT`，
`067` 一发，ingress 连带就读到了全表。

## SQL 那半也在这里验

`claim_upload` 带 `WHERE provider_file_id IS NULL`。少了它，供应商重投一次回调
就会把已认领的行重写一遍 —— `UPDATE` 覆盖不报错，`rowcount` 照样是 1。
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

from tests.conftest import TENANT_A, TENANT_B, _migration_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/ingress/src"))
from fastagent_v2.ingress.wecom_proxy.upload_claim import claim_upload  # noqa: E402

pytestmark = [pytest.mark.security]

RESULT = {
    "provider_file_id": "306b0201020460400",
    "provider_file_aes_key": "8e1987e241",
    "provider_file_md5": "5c6ebc096b9390da5",
    "provider_file_size": 2899681,
}


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url(), row_factory=dict_row) as conn:
        yield conn
        conn.execute("DELETE FROM materials WHERE tenant_id IN (%s, %s)", (TENANT_A, TENANT_B))
        conn.commit()


def _insert(conn: Connection, *, tenant: uuid.UUID, request_id: str | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type,
            byte_size, content_hash, status, archived_at, source_url, upload_request_id
        ) VALUES (%s, %s, %s, 1, 'price.pdf', 'application/pdf',
                  1024, %s, 'active', NULL, 'https://example.test/p.pdf', %s)
        """,
        (tenant, row_id, f"probe-{row_id.hex[:8]}", "a" * 64, request_id),
    )
    return row_id


def _stored(conn: Connection, row_id: uuid.UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT provider_file_id, provider_file_aes_key, provider_file_md5,
               provider_file_size
          FROM materials WHERE id = %s
        """,
        (row_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def test_the_row_with_that_request_id_is_the_one_claimed(connection: Connection) -> None:
    """**按 `requestId` 找到那一行**，四列都写进去。"""

    request_id = str(uuid.uuid4())
    target = _insert(connection, tenant=TENANT_A, request_id=request_id)
    other = _insert(connection, tenant=TENANT_A, request_id=str(uuid.uuid4()))

    claimed = claim_upload(
        connection, tenant_id=TENANT_A, request_id=request_id, result=RESULT
    )

    assert claimed == 1
    assert _stored(connection, target) == RESULT
    assert _stored(connection, other)["provider_file_id"] is None, "认领改到了别的行"


def test_a_request_id_we_never_issued_claims_nothing(connection: Connection) -> None:
    """**取件结果就是这一种**，而它每次都改 0 行 —— 按构造如此。

    同一个 `cmd=20000` 底下还有「取件一条入站媒体」的结果，`msgData` 几乎一样。
    区分它们的不是字段，是那个 `requestId` 在不在目录里：
    取件的 `requestId` 我们从没写进 `upload_request_id`，所以匹配不到任何行。
    """

    _insert(connection, tenant=TENANT_A, request_id=str(uuid.uuid4()))

    claimed = claim_upload(
        connection, tenant_id=TENANT_A, request_id=str(uuid.uuid4()), result=RESULT
    )

    assert claimed == 0


def test_a_redelivered_callback_does_not_rewrite_an_already_claimed_row(
    connection: Connection,
) -> None:
    """重投一次不覆盖 —— 判据是 **rowcount 为 0 且库里留着第一次的值**。

    `UPDATE` 覆盖不报错、`rowcount` 照样是 1，所以「第二次没抛」证明不了任何事。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, tenant=TENANT_A, request_id=request_id)
    assert claim_upload(
        connection, tenant_id=TENANT_A, request_id=request_id, result=RESULT
    ) == 1

    second = {**RESULT, "provider_file_id": "a-different-file-id"}
    again = claim_upload(
        connection, tenant_id=TENANT_A, request_id=request_id, result=second
    )

    assert again == 0, "重投的回调改到了行 —— WHERE provider_file_id IS NULL 那半漏了"
    assert _stored(connection, row_id)["provider_file_id"] == RESULT["provider_file_id"]


def test_one_tenants_callback_cannot_claim_another_tenants_row(
    connection: Connection,
) -> None:
    """**同一个 `requestId` 也只认本租户那一行。**

    `065` 那条唯一索引是 `(tenant_id, upload_request_id)` —— 也就是两个租户
    **可以**有同一个 `requestId` 字符串。少了 `tenant_id = %s` 那一半，
    一个租户的回调会去改另一个租户的资料，而那是一次静默的跨租户写。
    """

    shared = str(uuid.uuid4())
    mine = _insert(connection, tenant=TENANT_A, request_id=shared)
    theirs = _insert(connection, tenant=TENANT_B, request_id=shared)

    claimed = claim_upload(
        connection, tenant_id=TENANT_A, request_id=shared, result=RESULT
    )

    assert claimed == 1
    assert _stored(connection, mine)["provider_file_id"] == RESULT["provider_file_id"]
    assert _stored(connection, theirs)["provider_file_id"] is None, (
        "另一个租户的行被改了 —— 认领少了 tenant_id 那一半"
    )


def test_the_ingress_role_can_write_the_credentials_but_cannot_read_them(
    connection: Connection,
) -> None:
    """**`067` 的那条边界，真跑一次。**

    三段，缺一不可：

    1. `SELECT provider_file_aes_key` 抛权限错 —— 它读不回来；
    2. `SELECT upload_request_id` **读得通** —— 少了这一条，
       `UPDATE ... WHERE upload_request_id = ?` 会在真实回调回来时
       以 `permission denied` 失败（`WHERE` 里的列要的是 `SELECT` 权限）；
    3. `UPDATE` 那四列**改得动** —— 授权是发出去了的。

    只验第一段的话，「读不到」与「这个角色对这张表毫无权限」长得一模一样。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, tenant=TENANT_A, request_id=request_id)
    connection.commit()

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_ingress")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
    try:
        # 二 —— 先证明它对这张表不是毫无权限
        connection.execute(
            "SELECT id, upload_request_id FROM materials WHERE tenant_id = %s",
            (TENANT_A,),
        ).fetchall()

        # 三 —— 写得动
        written = claim_upload(
            connection, tenant_id=TENANT_A, request_id=request_id, result=RESULT
        )
        assert written == 1, "ingress 写不进那四列 —— 067 的 UPDATE 授权没到位"

        # 一 —— 而密钥读不回来
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SELECT provider_file_aes_key FROM materials").fetchall()
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")

    # **那次写随保存点一起回滚了，这里确认它确实回滚了。**
    #
    # 这条用例要证的是「权限到位」，不是「库里留下了一行认领好的资料」——
    # 而回滚掉它，下一条用例就不必先收拾它。断言写在这里是因为
    # `SET LOCAL ROLE` 与保存点的交互不是人人都熟：它要是没回滚，
    # 后面几条量到的东西就不是它们以为的那个了。
    assert _stored(connection, row_id)["provider_file_id"] is None


def test_the_ingress_role_cannot_read_the_rest_of_the_catalogue(
    connection: Connection,
) -> None:
    """**它也读不到 slug / 文件名 / 地址** —— `067` 的 `SELECT` 只发四列。

    一次面向公网的服务拿到的读面，越窄越好：认领需要的是「是哪一行」，
    不是「这份资料是什么」。
    """

    _insert(connection, tenant=TENANT_A, request_id=str(uuid.uuid4()))
    connection.commit()

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_ingress")
    try:
        for column in ("slug", "filename", "source_url", "content_hash"):
            connection.execute("SAVEPOINT per_column")
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(f"SELECT {column} FROM materials")  # noqa: S608
            connection.execute("ROLLBACK TO SAVEPOINT per_column")
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")


def test_the_archived_row_is_still_claimable_and_that_is_deliberate(
    connection: Connection,
) -> None:
    """归档的行**照样认领** —— 上传是在它归档之前发起的。

    拒绝它的代价是那次上传在供应商那边成了孤儿，而目录里那一行
    看上去像「从没传过」。认领它不会让它被发出去：挑发送对象的是别的判据。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, tenant=TENANT_A, request_id=request_id)
    connection.execute(
        "UPDATE materials SET status = 'archived', archived_at = %s WHERE id = %s",
        (datetime.now(UTC), row_id),
    )

    claimed = claim_upload(
        connection, tenant_id=TENANT_A, request_id=request_id, result=RESULT
    )

    assert claimed == 1


def test_the_ingress_grants_are_exactly_these_columns(connection: Connection) -> None:
    """**按相等钉住授权面** —— 它答得出「有没有多给」，而上面几条答不出。

    上面那几条各自问「这一列读得到吗」。一条一条问，**漏掉的那一列永远不会被问到**：
    `067` 第一版就少发了 `provider_file_id` 的 `SELECT`，而它是靠
    「真跑一次」那条在**写**那一段红出来的 —— 报文是
    `permission denied for table materials`，**它不提是哪一列**。

    这一条把两个集合整个取出来比对，所以少给会红、多给也会红，
    而报文直接说出是哪几列。
    """

    granted: dict[str, set[str]] = {"SELECT": set(), "UPDATE": set()}
    for row in connection.execute(
        """
        SELECT privilege_type, column_name FROM information_schema.column_privileges
         WHERE table_name = 'materials' AND grantee = 'fastagent_ingress'
        """
    ).fetchall():
        granted.setdefault(row["privilege_type"], set()).add(row["column_name"])

    # **`upload_failed_at` 是 `069` 加的**：记失败那句 `UPDATE` 的 `WHERE`
    # 要读它（`067` 少发一列的教训 —— `WHERE` 里的列要 `SELECT` 权限，
    # 而缺它的症状是整件事不工作、报文还不提是哪一列）。
    #
    # **它不是凭据，加进来不动 `067` 的理由**：那条理由是
    # 「写得进密钥却读不回来」，而下面那条断言仍然钉着这一点。
    readable = {
        "tenant_id",
        "id",
        "upload_request_id",
        "provider_file_id",
        "upload_failed_at",
    }
    assert granted["SELECT"] == readable, (
        "ingress 的可读列变了。"
        f"多出来：{sorted(granted['SELECT'] - readable)}；"
        f"少掉了：{sorted(readable - granted['SELECT'])}。"
        "**那两列凭据一旦出现在这里，`067` 的整条理由就没了** —— "
        "它的全部意思是「写得进却读不回来」。"
    )
    assert granted["UPDATE"] == {
        "provider_file_id",
        "provider_file_aes_key",
        "provider_file_md5",
        "provider_file_size",
        # `069`：记一次失败要写这两列。入站侧是唯一知道失败发生了的角色。
        "upload_failed_at",
        "upload_failure_code",
    }, f"ingress 的可写列变了：{sorted(granted['UPDATE'])}"

    assert granted.keys() <= {"SELECT", "UPDATE"}, (
        f"ingress 在 materials 上拿到了第三种权限：{sorted(granted)}。"
        "认领只需要读四列、写四列 —— INSERT / DELETE 都不该在这里"
    )
