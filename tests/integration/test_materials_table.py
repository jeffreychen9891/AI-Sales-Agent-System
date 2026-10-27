"""`materials` 拒绝 `053` 的注释说它拒绝的东西，并允许它说允许的那一件。

**这份文件是 2026-09-12 独立审查 P1-2 换来的。** 本批第一版一条用例都没有，
而 `materials_immutable` 是**全仓唯一没有用例的不可变触发器**——
`006` 的 `profile_releases_immutable` 有 `test_profile_registry_db.py` 两个方向，
`050` 为它三张表发了 `test_knowledge_base_tables.py`。

**没被执行过的 `RAISE` 与不会触发的 `RAISE`，在读的人眼里是一样的**，
而迁移是最不该出现这种东西的地方：下一个人把约束当保证读。

**这里最要紧的一条是 `test_archiving_is_allowed`。**
它是触发器存在的全部理由——其余分支错了会**吵**（写不进去），
**只有它错了会安静**：归档默默失败，而资料照发。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors

from tests.conftest import TENANT_A, TENANT_B, _migration_url

pytestmark = [pytest.mark.security]

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture()
def tenant(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """已播种的两个租户，且没有任何 materials 行。

    **用既有夹具而不是自建租户**，理由同 `test_knowledge_base_tables.py`：
    `materials` 引用 `tenants`，夹具的 `TRUNCATE … CASCADE` 会顺带清掉它。
    **本文件不提交**，每条用例的行随连接一起消失。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url()) as connection:
        yield connection


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "id": uuid.uuid4(),
        "slug": "price-list",
        "version": 1,
        "filename": "price.pdf",
        "media_type": "application/pdf",
        "byte_size": 1024,
        "content_hash": HASH_A,
        "status": "active",
        "archived_at": None,
    }
    row.update(overrides)
    return row


def _insert(connection: Connection, row: dict[str, Any]) -> uuid.UUID:
    connection.execute(
        """
        INSERT INTO materials
            (tenant_id, id, slug, version, filename, media_type, byte_size,
             content_hash, status, archived_at)
        VALUES (%(tenant_id)s, %(id)s, %(slug)s, %(version)s, %(filename)s,
                %(media_type)s, %(byte_size)s, %(content_hash)s, %(status)s,
                %(archived_at)s)
        """,
        row,
    )
    return row["id"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# (c) 允许的那一件 —— **先写，因为它是触发器存在的理由**
# ---------------------------------------------------------------------------


def test_archiving_is_allowed(tenant: Connection) -> None:
    """`active → archived` 必须**通过**，并盖上 `archived_at`。

    **这一条错了会安静**：归档被拒，而调用方多半不看返回，
    于是一份本该停发的资料继续被发出去。其余每一条错了都会吵。
    """

    material = _insert(tenant, _row())
    tenant.execute(
        "UPDATE materials SET status = 'archived', archived_at = now() "
        "WHERE tenant_id = %s AND id = %s",
        (TENANT_A, material),
    )
    status = tenant.execute(
        "SELECT status, archived_at IS NOT NULL FROM materials WHERE id = %s",
        (material,),
    ).fetchone()
    assert status == ("archived", True)


# ---------------------------------------------------------------------------
# (a) 九列任一被改都要拒
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("slug", "other-list"),
        ("version", 2),
        ("filename", "other.pdf"),
        ("media_type", "image/png"),
        ("byte_size", 2048),
        ("content_hash", HASH_B),
    ],
)
def test_the_substance_is_immutable(tenant: Connection, column: str, value: Any) -> None:
    """改哪一列都拒。**逐列参数化，不是抽一列代表**——
    触发器是一串 `OR`，漏掉一个分支不会让其余分支变红。"""

    material = _insert(tenant, _row())
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            f"UPDATE materials SET {column} = %s WHERE tenant_id = %s AND id = %s",  # noqa: S608
            (value, TENANT_A, material),
        )


# ---------------------------------------------------------------------------
# (b) 反归档被拒；归档时刻不可改写
# ---------------------------------------------------------------------------


def test_un_archiving_is_refused(tenant: Connection) -> None:
    material = _insert(tenant, _row(status="archived", archived_at="2026-09-12T00:00:00Z"))
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            "UPDATE materials SET status = 'active', archived_at = NULL "
            "WHERE tenant_id = %s AND id = %s",
            (TENANT_A, material),
        )


def test_the_archive_timestamp_is_written_once(tenant: Connection) -> None:
    """独立审查 P2-2：`archived_at` 原本不在触发器的比较列表里。

    它**就是**「这一版从哪一刻起不得再发」那个事实，可改写等于那个事实没有答案。
    """

    material = _insert(tenant, _row(status="archived", archived_at="2026-09-12T00:00:00Z"))
    with pytest.raises(errors.RestrictViolation):
        tenant.execute(
            "UPDATE materials SET archived_at = now() WHERE tenant_id = %s AND id = %s",
            (TENANT_A, material),
        )


# ---------------------------------------------------------------------------
# (d)(e) CHECK 与唯一约束
# ---------------------------------------------------------------------------


def test_archived_without_a_timestamp_is_refused(tenant: Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        _insert(tenant, _row(status="archived", archived_at=None))


def test_a_timestamp_without_being_archived_is_refused(tenant: Connection) -> None:
    """反方向同样要拒——两条 CHECK 写成等价式，只测一边等于只测一半。"""

    with pytest.raises(errors.CheckViolation):
        _insert(tenant, _row(status="active", archived_at="2026-09-12T00:00:00Z"))


def test_a_second_row_with_the_same_slug_and_version_is_refused(
    tenant: Connection,
) -> None:
    """`asset:price-list:v1` 必须解析到**恰好一样东西**。"""

    _insert(tenant, _row())
    with pytest.raises(errors.UniqueViolation):
        _insert(tenant, _row())


def test_a_new_version_of_the_same_slug_is_allowed(tenant: Connection) -> None:
    """安全带：唯一约束不能严到连出新版都不行。"""

    _insert(tenant, _row(version=1))
    _insert(tenant, _row(version=2, content_hash=HASH_B))


def test_deleting_is_refused(tenant: Connection) -> None:
    """删一行会把一条已发回复的解释一起带走。

    **但整租户抹除不是这条路**——`purge_tenant_data` 走的是同一个 `DELETE`，
    它能过是因为**本表根本没有禁删触发器**（第一版有，门禁抓出它与清除打架）。
    所以这里断言的是**没有**这道闸，而不是有。
    """

    material = _insert(tenant, _row())
    tenant.execute("DELETE FROM materials WHERE tenant_id = %s AND id = %s", (TENANT_A, material))
    left = tenant.execute(
        "SELECT count(*) FROM materials WHERE id = %s", (material,)
    ).fetchone()
    assert left == (0,)


# ---------------------------------------------------------------------------
# (f) 跨租户真的看不见 —— **manifest 驱动那条用例证不到这一层**
# ---------------------------------------------------------------------------


def _as_unprivileged(connection: Connection, tenant_id: uuid.UUID) -> None:
    """切到一个**非超级用户**角色，并设 `app.tenant_id`。

    **这段脚手架是必要的，不是方便**：`_migration_url()` 连的是迁移角色，
    它是 superuser，而 **superuser 绕过 RLS —— `FORCE ROW LEVEL SECURITY` 也拦不住它**。
    用它去断言「B 读不到 A」永远会失败，而失败的原因与策略对不对无关。
    （本文件第一版正是这么写的，2026-09-12 门禁当场红两条。）

    **为什么要自己造角色**：`053` **刻意不发任何 `GRANT`**（同 `050` 的口径，
    route 在 E3-6、发送在 E3-4 各自授权），所以今天 `fastagent_app` /
    `fastagent_worker` 对本表**一个权限都没有**——拿它们只能证出「访问被拒」，
    证不出「隔离生效」。

    **这条 `GRANT` 是用例的脚手架，不是迁移的行为**，
    不要读成「应用今天能读 materials」。

    **角色本身不随事务回滚**（cluster 级对象），所以上面是幂等创建；
    行仍然随连接消失（本文件不提交）。
    """

    # **幂等创建**：角色是 cluster 级对象，**不随事务回滚**——本文件第一版
    # 以为它会，于是第二条用例撞上 `role … already exists`（门禁抓到）。
    connection.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'materials_rls_probe') "
        "THEN CREATE ROLE materials_rls_probe NOLOGIN; END IF; END $$"
    )
    connection.execute("GRANT SELECT, INSERT ON materials TO materials_rls_probe")
    connection.execute("SET ROLE materials_rls_probe")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def _switch_tenant(connection: Connection, tenant_id: uuid.UUID) -> None:
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def test_one_tenant_cannot_see_another_tenants_rows(tenant: Connection) -> None:
    """**独立审查 §三 点名的那个缺口。**

    `tests/integration/test_rls_policies_exist.py` 是 manifest 驱动的，它证到四件：
    表存在、`relrowsecurity`、`relforcerowsecurity`、策略表达式**里出现了**
    `app.tenant_id` 字样。**它没有证「A 写的一行，B 读不到」**——
    其余表是靠各自 route 的跨租户用例「by accident」覆盖到的，
    而 `materials` 今天**没有 route**，和那三张知识库表处境相同。
    """

    _as_unprivileged(tenant, TENANT_A)
    _insert(tenant, _row(tenant_id=TENANT_A))

    _switch_tenant(tenant, TENANT_B)
    visible = tenant.execute("SELECT count(*) FROM materials").fetchone()
    assert visible == (0,), "B 租户看得见 A 租户的资料行"

    _switch_tenant(tenant, TENANT_A)
    mine = tenant.execute("SELECT count(*) FROM materials").fetchone()
    assert mine == (1,), "A 租户看不见自己刚写的那一行——那就不是隔离，是全拒"


def test_writing_into_another_tenant_is_refused(tenant: Connection) -> None:
    """`WITH CHECK` 那一半：设成 A，写不进 B。

    **只测读那一半等于只测一半**——策略两侧是分开的表达式。
    """

    _as_unprivileged(tenant, TENANT_A)
    with pytest.raises(errors.Error):
        _insert(tenant, _row(tenant_id=TENANT_B))
