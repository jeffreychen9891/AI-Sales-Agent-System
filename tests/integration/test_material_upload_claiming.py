"""上传脚本挑哪些行、怎么认领 —— **两条都防「再传一遍」**。

## 为什么这两件必须连库验

两件都是 SQL 的性质，而 SQL 写错的方向是安静的：

1. **挑行的判据是 `upload_request_id IS NULL`，不是 `provider_file_id IS NULL`。**
   两者差一个窗口 ——「已经交给供应商了，而回调还没回来」。
   用后者当判据，**每跑一次脚本就在供应商那边多一份孤儿文件**，
   而目录、日志、退出码**全都是成功的样子**；

2. **`claim` 带 `WHERE upload_request_id IS NULL`。** 少了它，两个人同时跑
   脚本时后一个会**覆盖**前一个的 id —— 被覆盖掉的那次上传从此认领不回来。
   `UPDATE` 覆盖不会报错，`rowcount` 照样是 1。

契约层证不了这两条中的任何一条：它们是数据库对一句 SQL 的回答。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from tests.conftest import TENANT_A, _migration_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from upload_materials import claim, rows_needing_upload  # noqa: E402

pytestmark = [pytest.mark.security]


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    # **`dict_row` 不是顺手加的**：`rows_needing_upload` 用 `row["id"]` 读，
    # 而脚本连库时给的就是 `row_factory=dict_row`。用例这边少给这一个参数，
    # 量的就不是脚本真跑的那条路 —— 第一次跑它以 `TypeError` 结束。
    with Connection.connect(_migration_url(), row_factory=dict_row) as conn:
        yield conn
        conn.execute("DELETE FROM materials WHERE tenant_id = %s", (TENANT_A,))
        conn.commit()


def _insert(conn: Connection, **overrides: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    values: dict[str, Any] = {
        "slug": f"probe-{row_id.hex[:8]}",
        "version": 1,
        "filename": "price.pdf",
        "media_type": "application/pdf",
        "byte_size": 1024,
        "content_hash": "a" * 64,
        "status": "active",
        "archived_at": None,
        "source_url": "https://example.test/price.pdf",
        "upload_request_id": None,
        "provider_file_id": None,
    }
    values.update(overrides)
    conn.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type,
            byte_size, content_hash, status, archived_at, source_url,
            upload_request_id, provider_file_id
        ) VALUES (%(tenant)s, %(id)s, %(slug)s, %(version)s, %(filename)s, %(media_type)s,
                  %(byte_size)s, %(content_hash)s, %(status)s, %(archived_at)s, %(source_url)s,
                  %(upload_request_id)s, %(provider_file_id)s)
        """,
        {"tenant": TENANT_A, "id": row_id, **values},
    )
    return row_id


def test_a_row_already_handed_to_the_provider_is_not_picked_up_again(
    connection: Connection,
) -> None:
    """**已经有 `requestId` 的行不再入选**，哪怕回调还没回来。

    这一条就是那个窗口：`upload_request_id` 有值而 `provider_file_id` 还是 NULL。
    判据写成后者的话，这一行每跑一次脚本就被再传一次。
    """

    waiting = _insert(
        connection, upload_request_id=str(uuid.uuid4()), provider_file_id=None
    )
    fresh = _insert(connection)

    picked = {row["id"] for row in rows_needing_upload(connection, tenant_id=TENANT_A)}

    assert fresh in picked, "刚导进来、还没传的那一行没被挑中"
    assert waiting not in picked, (
        "已经交给供应商、正在等回调的那一行又被挑中了 —— "
        "判据多半写成了 provider_file_id IS NULL，而那会让每次跑都重传一份"
    )


def test_an_archived_row_is_not_sent_to_the_provider(connection: Connection) -> None:
    """归档掉的版本不再往供应商那边送。

    目录是版本化的，旧版本置 `archived` 而不是删（`065`）。它还带着 `source_url`，
    所以**只按 source_url 挑就会把它一起送出去**。
    """

    # **`archived_at` 要一起给**：`053` 有一条
    # `CHECK ((status = 'archived') = (archived_at IS NOT NULL))` ——
    # 只置 status 不盖时间戳，库当场拒。第一次跑它就是这么红的。
    archived = _insert(
        connection, status="archived", archived_at=datetime.now(UTC)
    )

    picked = {row["id"] for row in rows_needing_upload(connection, tenant_id=TENANT_A)}

    assert archived not in picked, "已归档的版本被挑去上传了"


def test_a_row_without_a_source_url_is_not_picked_up(connection: Connection) -> None:
    """没有地址就没得传 —— 供应商是自己去拉的，它需要那个地址。"""

    no_source = _insert(connection, source_url=None)

    picked = {row["id"] for row in rows_needing_upload(connection, tenant_id=TENANT_A)}

    assert no_source not in picked


def test_claiming_a_row_twice_does_not_overwrite_the_first_request_id(
    connection: Connection,
) -> None:
    """**第二次认领改不到任何行** —— 覆盖掉的那个 id 就再也认领不回来了。

    这是两个实例同时跑的形状。`UPDATE` 覆盖不报错、`rowcount` 照样是 1，
    所以判据是**第二次的 rowcount 为 0 且库里留着第一个 id**，
    而不是「第二次抛了没有」。
    """

    row_id = _insert(connection)
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())

    assert claim(connection, tenant_id=TENANT_A, material_id=row_id, request_id=first) == 1
    changed = claim(connection, tenant_id=TENANT_A, material_id=row_id, request_id=second)

    assert changed == 0, "第二次认领改到了行 —— WHERE upload_request_id IS NULL 那半漏了"

    stored = connection.execute(
        "SELECT upload_request_id FROM materials WHERE tenant_id = %s AND id = %s",
        (TENANT_A, row_id),
    ).fetchone()
    assert stored is not None
    # 按列名读，不按位置 —— 这个连接是 `dict_row` 的（见 fixture）。
    assert stored["upload_request_id"] == first, (
        f"库里留下的是第二个 id（{stored['upload_request_id']}）——"
        " 第一次那份上传从此认领不回来"
    )
