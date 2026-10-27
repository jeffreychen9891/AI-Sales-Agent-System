"""`065` 那三列在**连库那一层**真的立得住，而 `fastagent_app` 真的插得进去。

## 为什么要连库验

契约层能证的是「脚本会拒什么」；它证不了 **`GRANT INSERT` 真的发出去了** ——
而那条授权是本批唯一一处权限改动。`058` 给的是 `SELECT, UPDATE`，
一条少发的 `INSERT` 会让导入脚本在真实环境里当场失败，
**而门禁里一条用例都不会红**（契约层根本不连库）。

它证四件：

1. 三列存在，且**可空**（既有行不受影响）；
2. **`upload_request_id` 唯一** —— 回调回来时「这是哪一行的结果」要答得出；
3. **两个 NULL 不算冲突** —— 上传之前绝大多数行都是 NULL，
   部分索引的意义就在这里；
4. **`fastagent_app` 插得进去**，也就是那条 `GRANT INSERT` 真的到位了。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors

from tests.conftest import TENANT_A, _migration_url

pytestmark = [pytest.mark.security]


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url()) as conn:
        yield conn
        conn.execute("DELETE FROM materials WHERE tenant_id = %s", (TENANT_A,))
        conn.commit()


def _insert(conn: Connection, **overrides: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    values = {
        "slug": f"probe-{row_id.hex[:8]}",
        "version": 1,
        "filename": "price.pdf",
        "media_type": "application/pdf",
        "byte_size": 1024,
        "content_hash": "a" * 64,
        "source_url": "https://example.test/price.pdf",
        "upload_request_id": None,
        "provider_file_id": None,
    }
    values.update(overrides)
    conn.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type,
            byte_size, content_hash, status, source_url,
            upload_request_id, provider_file_id
        ) VALUES (%(tenant)s, %(id)s, %(slug)s, %(version)s, %(filename)s, %(media_type)s,
                  %(byte_size)s, %(content_hash)s, 'active', %(source_url)s,
                  %(upload_request_id)s, %(provider_file_id)s)
        """,
        {"tenant": TENANT_A, "id": row_id, **values},
    )
    return row_id


def test_a_catalogue_row_with_a_source_and_no_upload_yet_is_accepted(
    connection: Connection,
) -> None:
    """**先证明常态那一行进得去** —— 导进来但还没上传，两列都是 NULL。"""

    row_id = _insert(connection)
    connection.commit()

    stored = connection.execute(
        "SELECT source_url, upload_request_id, provider_file_id FROM materials"
        " WHERE tenant_id = %s AND id = %s",
        (TENANT_A, row_id),
    ).fetchone()
    assert stored is not None
    assert stored[0] == "https://example.test/price.pdf"
    assert stored[1] is None and stored[2] is None


def test_two_rows_waiting_to_be_uploaded_do_not_collide(connection: Connection) -> None:
    """两个 NULL 不算冲突。

    **这一条是部分索引的理由**：上传之前每一行的 `upload_request_id` 都是 NULL，
    一条普通的 `UNIQUE` 也能过（NULL 互不相等），而它会白白覆盖全表。
    """

    _insert(connection)
    _insert(connection)
    connection.commit()

    waiting = connection.execute(
        "SELECT count(*) FROM materials WHERE tenant_id = %s AND upload_request_id IS NULL",
        (TENANT_A,),
    ).fetchone()
    assert waiting is not None and waiting[0] == 2


def test_the_same_upload_request_cannot_land_on_two_rows(connection: Connection) -> None:
    """**同一个 `requestId` 不许出现在两行上。**

    它是 `cmd=20000` 分流的唯一判据 —— 上传结果与媒体密钥通知的 `msgData`
    形状相同，分得开的只有「这个 requestId 是不是我们发起的」。
    两行共用一个，回调回来时「这是哪一行的结果」就答不出来了。
    """

    shared = f"req-{uuid.uuid4().hex}"
    _insert(connection, upload_request_id=shared)
    connection.commit()

    with pytest.raises(errors.UniqueViolation):
        _insert(connection, upload_request_id=shared)
    connection.rollback()


def test_the_app_role_can_insert_which_is_this_batch_s_only_permission_change(
    connection: Connection,
) -> None:
    """**`fastagent_app` 真的插得进去** —— `065` 那条 `GRANT INSERT` 到位了。

    `058` 给的是 `SELECT, UPDATE`。少发这一条，导入脚本在真实环境里当场失败，
    而契约层一条都不会红（它根本不连库）。**这就是这条用例存在的全部理由。**
    """

    granted = connection.execute(
        """
        SELECT has_table_privilege('fastagent_app', 'materials', 'INSERT') AS may_insert,
               has_table_privilege('fastagent_app', 'materials', 'SELECT') AS may_select,
               has_table_privilege('fastagent_app', 'materials', 'DELETE') AS may_delete
        """
    ).fetchone()
    assert granted is not None
    assert granted[0] is True, "fastagent_app 没有 materials 的 INSERT —— 065 那条授权没到位"
    # **`066` 之后这里是 `False`，而这是有意的。**
    #
    # 原来写的是 `is True`：`058` 给的是**表级** `SELECT`。`066` 把它收回、
    # 改成按列发（那两列凭据不发），所以 `has_table_privilege` 从此回 `false` ——
    # 对只有列级授权的角色它本来就该这么回。
    #
    # 「读路由还读得到它要的那十列」这件事由
    # `test_material_credentials_are_not_readable.py` 真连库读一次来证，
    # 不在这里用一个表级问句代答 —— 那个问句现在答的已经不是那个问题了。
    assert granted[1] is False, (
        "`fastagent_app` 又拿到了 materials 的表级 SELECT。066 有意收掉了它："
        "表级授权覆盖以后新增的每一列，包括那两列凭据。"
        "要放开某一列，写进 066 的列清单，不要发表级。"
    )
    assert granted[2] is False, (
        "fastagent_app 拿到了 DELETE —— 目录是版本化的，旧版本置 archived 而不是删，"
        "065 有意没给这一条"
    )
