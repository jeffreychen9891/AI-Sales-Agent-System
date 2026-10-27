"""一次失败的上传**有形态、且能重传** —— 运维 2026-09-17 实测的两件。

## 它们不是假想

十二份资料交给供应商去拉，供应商**把十二份都拉走了**（access.log 十二条 200），
然后回了十二条 `cmd=20000`。**认领 0 条。** 抓包才看到真面目：

    code : 500100
    msg  : 底层客户端业务错误[获取wxCdnInfo失败]
    data : [ { cmd: 20000, requestId: <我们的>, msgType: 0, msgData: null } ]

**代码没有做错任何一步** —— `msgData` 是 `null`，所以没有东西可认领。
问题是那次失败的全部可见形态：入站答 200、审计行与「有人退群了」共用一个计数、
目录里那一行**看上去是齐的**。运维靠「认领数一直是 0」发现，靠抓包才查清。

## 这份钉四件

1. **失败被写在那一行上** —— 时点 + 供应商原话；
2. **不是我们发起的那次回调不留痕** —— 判据与认领同源（`requestId` 在不在目录里）；
3. **已经认领成功的不被后到的失败翻回去** —— 供应商重投是常态；
4. **失败之后那一行能重传**，而**在途的那一行照旧挡着**。
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from tests.conftest import TENANT_A, _migration_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/ingress/src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from fastagent_v2.ingress.wecom_proxy.upload_claim import (  # noqa: E402
    claim_upload,
    record_upload_failure,
)
from upload_materials import claim, rows_needing_upload  # noqa: E402

pytestmark = [pytest.mark.security]

RESULT = {
    "provider_file_id": "306b0201020460400",
    "provider_file_aes_key": "aes",
    "provider_file_md5": "md5",
    "provider_file_size": 2048,
}
VENDOR = "500100 底层客户端业务错误[获取wxCdnInfo失败]"


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url(), row_factory=dict_row) as conn:
        yield conn
        conn.execute("DELETE FROM materials WHERE tenant_id = %s", (TENANT_A,))
        conn.commit()


def _insert(conn: Connection, *, slug: str, request_id: str | None) -> uuid.UUID:
    row_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type, byte_size,
            content_hash, status, archived_at, source_url, upload_request_id
        ) VALUES (%s, %s, %s, 1, 'p.pdf', 'application/pdf', 1024, %s,
                  'active', NULL, 'https://example.test/p.pdf', %s)
        """,
        (TENANT_A, row_id, slug, "a" * 64, request_id),
    )
    return row_id


def _row(conn: Connection, row_id: uuid.UUID) -> dict[str, Any]:
    found = conn.execute(
        """
        SELECT upload_request_id, upload_failed_at, upload_failure_code, provider_file_id
          FROM materials WHERE id = %s
        """,
        (row_id,),
    ).fetchone()
    assert found is not None
    return dict(found)


def test_a_failed_upload_is_written_on_the_row_with_the_vendors_own_words(
    connection: Connection,
) -> None:
    """**失败有了形态** —— 时点 + 供应商原话。

    记原话而不是映射成本仓的词：这一族的错误码表供应商没给
    （`docs/wecom-proxy-cdn-upload-api.md` §2.4 登记着要问而一直没有），
    而把没见过的码映射成已知词表是猜。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, slug="pricing-sheet", request_id=request_id)

    marked = record_upload_failure(
        connection, tenant_id=TENANT_A, request_id=request_id, detail=VENDOR
    )

    assert marked == 1
    row = _row(connection, row_id)
    assert row["upload_failed_at"] is not None
    assert row["upload_failure_code"] == VENDOR, (
        "供应商的原话没被原样记下来 —— 下一次失败时又得抓包"
    )


def test_a_callback_we_did_not_start_leaves_no_trace(connection: Connection) -> None:
    """**判据与认领同源**：那个 `requestId` 在不在目录里。

    取件结果那一类也是 `cmd=20000`，也可能 `msgData` 读不出结果。
    少了这一条，每一条与我们无关的回调都会被记成一次上传失败 ——
    而那个计数正是用来判断「要不要有人去看」的。
    """

    row_id = _insert(connection, slug="pricing-sheet", request_id=str(uuid.uuid4()))

    marked = record_upload_failure(
        connection, tenant_id=TENANT_A, request_id=str(uuid.uuid4()), detail=VENDOR
    )

    assert marked == 0
    assert _row(connection, row_id)["upload_failed_at"] is None


def test_a_late_failure_does_not_undo_a_successful_claim(connection: Connection) -> None:
    """**已经认领成功的不被翻回去。** 供应商重投是常态。

    一次成功之后到达的失败回调多半是重投的旧消息，不是新事实 ——
    让它把那一行标成失败，会让一份发得出去的资料在目录里显示为失败。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, slug="pricing-sheet", request_id=request_id)
    assert claim_upload(
        connection, tenant_id=TENANT_A, request_id=request_id, result=RESULT
    ) == 1

    marked = record_upload_failure(
        connection, tenant_id=TENANT_A, request_id=request_id, detail=VENDOR
    )

    assert marked == 0
    row = _row(connection, row_id)
    assert row["upload_failed_at"] is None
    assert row["provider_file_id"] == RESULT["provider_file_id"]


def test_a_failed_row_is_picked_up_again_while_an_in_flight_one_is_not(
    connection: Connection,
) -> None:
    """**两个方向一起钉** —— 这一条是运维当场卡住的那件。

    失败那一行要能重传（在此之前重跑只会说「没有要传的」，
    因为 `upload_request_id` 已经写上了）；
    **而在途那一行照旧不能** —— 那正是第一版判据要防的：
    每跑一次就在供应商那边多一份孤儿文件。
    """

    failed_id = _insert(connection, slug="failed-one", request_id=str(uuid.uuid4()))
    record_upload_failure(
        connection,
        tenant_id=TENANT_A,
        request_id=str(_row(connection, failed_id)["upload_request_id"]),
        detail=VENDOR,
    )
    in_flight = _insert(connection, slug="in-flight", request_id=str(uuid.uuid4()))
    fresh = _insert(connection, slug="fresh", request_id=None)

    picked = {row["id"] for row in rows_needing_upload(connection, tenant_id=TENANT_A)}

    assert failed_id in picked, "失败过的那一行没被挑中 —— 重试仍然不可能"
    assert fresh in picked
    assert in_flight not in picked, (
        "在途那一行被挑中了 —— 每跑一次就在供应商那边多一份孤儿文件"
    )


def test_retrying_overwrites_the_dead_request_id_and_clears_the_failure(
    connection: Connection,
) -> None:
    """重传时覆盖那个已经没用的 `requestId`，并把失败痕迹清掉。

    上一次失败留下的那个 id 已经终结于失败，不会再有回调来认领它。
    **留着失败痕迹会让「这一行现在是什么状态」变成一道要读两列并推理的题**，
    而下一次真失败时它照样会被写上。
    """

    row_id = _insert(connection, slug="pricing-sheet", request_id=str(uuid.uuid4()))
    old = str(_row(connection, row_id)["upload_request_id"])
    record_upload_failure(connection, tenant_id=TENANT_A, request_id=old, detail=VENDOR)

    new = str(uuid.uuid4())
    changed = claim(connection, tenant_id=TENANT_A, material_id=row_id, request_id=new)

    assert changed == 1
    row = _row(connection, row_id)
    assert row["upload_request_id"] == new
    assert row["upload_failed_at"] is None
    assert row["upload_failure_code"] is None


def test_the_ingress_role_can_write_the_failure_but_still_cannot_read_the_credentials(
    connection: Connection,
) -> None:
    """`069` 发的两条授权不扩大读面。

    入站侧多了写「失败」的权，**而它读不到的还是读不到** ——
    `066` / `067` 围的那条线没有因为这一批松开。
    """

    request_id = str(uuid.uuid4())
    _insert(connection, slug="pricing-sheet", request_id=request_id)
    connection.commit()

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_ingress")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(TENANT_A),))
    try:
        assert record_upload_failure(
            connection, tenant_id=TENANT_A, request_id=request_id, detail=VENDOR
        ) == 1

        from psycopg import errors  # noqa: PLC0415

        connection.execute("SAVEPOINT no_read")
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SELECT provider_file_aes_key FROM materials")
        connection.execute("ROLLBACK TO SAVEPOINT no_read")
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")


def test_a_failure_older_than_the_current_attempt_is_replaced_not_appended(
    connection: Connection,
) -> None:
    """同一行失败两次，留下的是**最后一次**的原话。

    两次失败原因不同是常见的（第一次是实例离线，第二次是文件取不到），
    而留着第一次的会让排障的人查错方向。
    """

    request_id = str(uuid.uuid4())
    row_id = _insert(connection, slug="pricing-sheet", request_id=request_id)
    record_upload_failure(
        connection, tenant_id=TENANT_A, request_id=request_id, detail="500100 第一次"
    )
    record_upload_failure(
        connection, tenant_id=TENANT_A, request_id=request_id, detail="500200 第二次"
    )

    assert _row(connection, row_id)["upload_failure_code"] == "500200 第二次"
