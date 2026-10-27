"""`066`：那两列凭据落了库，**而读路由那个角色真的读不到它们**。

## 这条用例防的是一个「加一列就自动打开」的口子

`058` 给 `fastagent_app` 的是**表级** `GRANT SELECT ON materials`，
而表级授权**覆盖以后新增的每一列**。所以 `066` 的 `ADD COLUMN` 一落地，
那两列凭据本来会**立刻**对读路由可见 —— 而当时不会有任何东西变红：
`GET /materials` 与 `GET /materials/{id}` 各自只 `SELECT` 十列，
它们不会因为多了两列就开始返回它们，**直到有人加一个字段**。

也就是说这是本仓那条老毛病的形状：**错的方向是安静的**。
`066` 因此把这张表从表级换成列级 —— `017` 的表头把这条规则写过一遍：

  > `007` defined the authorisation on this table as **column-level** ...
  > so a new column is granted to nobody by construction.

**这条用例钉的就是 by construction 那四个字。**

## 四件

1. 三列存在且可空（既有行不受影响）；
2. **表级 `SELECT` 没有了** —— 这一条正是「以后加列默认谁都读不到」的成因，
   它不是一句注释，`has_table_privilege` 会答；
3. **按列发出去的就是那十五列**，一个不多 —— 用相等而不是包含：
   多出来一列（比如谁把表级 `GRANT` 又发了一遍）当场红；
4. **真的读不到**：`SET ROLE fastagent_app` 之后 `SELECT` 那一列会抛
   `InsufficientPrivilege`。前三条是问系统目录，这一条是真读一次 ——
   目录说得对而实际读得到，这种事只有真读一次才看得见。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors, sql

from tests.conftest import _migration_url

pytestmark = [pytest.mark.security]

#: `066` 之后 `fastagent_app` 能 `SELECT` 的**全部**列。
#:
#: 前十一列是两条读路由逐字 `SELECT` 的那些（`api/app.py` 的 `GET /materials`
#: 与 `GET /materials/{id}`，外加 RLS 要用的 `tenant_id`）；后四列是句柄与来源，
#: 排障要答「这份资料从哪儿拉、有没有传过、供应商那边叫什么、它读到多大」。
#:
#: **不在这里的是那两列凭据。** 要往这张表加列的人：先想清楚它是句柄还是密钥，
#: 是密钥就别加进这个集合 —— 加进来这条用例会绿，而绿得毫无意义。
APP_READABLE_COLUMNS = frozenset(
    {
        "tenant_id",
        "id",
        "slug",
        "version",
        "filename",
        "media_type",
        "byte_size",
        "content_hash",
        "status",
        "archived_at",
        "created_at",
        "source_url",
        "upload_request_id",
        "provider_file_id",
        "provider_file_size",
        # **`069` 加的两列**：失败的时点与供应商原话。
        #
        # 它们给 `fastagent_app` 读，是因为上传脚本按它们选行、运维按它们排障 ——
        # 而**它们不是凭据**：一句「500100 获取wxCdnInfo失败」谁看都无所谓，
        # 泄漏不了任何东西。那两列真凭据仍然不在这个集合里，
        # 而下面那条真读一次的用例仍然钉着它。
        "upload_failed_at",
        "upload_failure_code",
    }
)

#: **给谁都不给的那两列。**
CREDENTIAL_COLUMNS = ("provider_file_aes_key", "provider_file_md5")


@pytest.fixture()
def connection(seeded_database: dict[str, Any]) -> Iterator[Connection]:
    assert seeded_database  # 库已迁移是前提，不是一个没用到的参数
    with Connection.connect(_migration_url()) as conn:
        yield conn


def test_the_three_columns_exist_and_are_nullable(connection: Connection) -> None:
    """三列在，且都可空 —— 上传之前每一行的这三列都是 NULL。"""

    rows = {
        row[0]: row[1]
        for row in connection.execute(
            """
            SELECT column_name, is_nullable FROM information_schema.columns
             WHERE table_name = 'materials'
               AND column_name IN (
                   'provider_file_aes_key', 'provider_file_md5', 'provider_file_size'
               )
            """
        ).fetchall()
    }

    assert set(rows) == {
        "provider_file_aes_key",
        "provider_file_md5",
        "provider_file_size",
    }, f"066 的三列没都到位，现在有的是 {sorted(rows)}"
    assert set(rows.values()) == {"YES"}, (
        "有一列是 NOT NULL —— 上传之前它们本来就该是空的，"
        f"给了非空约束等于 065 那条导入路当场插不进去；现在是 {rows}"
    )


def test_the_table_level_select_is_gone_which_is_what_closes_later_columns(
    connection: Connection,
) -> None:
    """**表级 `SELECT` 必须不在** —— 这是「以后加列默认谁都读不到」的成因。

    `has_table_privilege` 对只有列级授权的角色回 `false`：这正是要的答案。
    它一旦变回 `true`，说明有人（多半是无意地）又发了一条表级 `GRANT`，
    而那条 `GRANT` 会把**当时表上的每一列**——包括那两列凭据——一起给出去。
    """

    granted = connection.execute(
        """
        SELECT has_table_privilege('fastagent_app', 'materials', 'SELECT') AS table_select,
               has_table_privilege('fastagent_app', 'materials', 'INSERT') AS may_insert,
               has_table_privilege('fastagent_app', 'materials', 'DELETE') AS may_delete
        """
    ).fetchone()
    assert granted is not None

    assert granted[0] is False, (
        "`fastagent_app` 又拿到了 materials 的**表级** SELECT。"
        "表级授权覆盖以后新增的每一列 —— 包括那两列凭据，"
        "而两条读路由只选十列，所以它会一直不红，直到有人加一个字段。"
        "要放开某一列就把它写进 066 的列清单里，不要发表级。"
    )
    assert granted[1] is True, "INSERT 没了 —— 那是 065 给导入脚本的，先看那边"
    assert granted[2] is False, "拿到了 DELETE —— 目录是版本化的，旧版本置 archived 不删"


def test_the_granted_columns_are_exactly_the_fifteen(connection: Connection) -> None:
    """按列发出去的**就是**那十五列 —— 用相等，不用包含。

    包含式（`期望 <= 实到`）答不出「有没有多给」，而这条用例要防的恰恰是多给：
    表级 `GRANT` 发回去的时候，`column_privileges` 里会冒出**每一列**，
    包含式一个字都不会说。
    """

    granted = {
        row[0]
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.column_privileges
             WHERE table_name = 'materials' AND grantee = 'fastagent_app'
               AND privilege_type = 'SELECT'
            """
        ).fetchall()
    }

    assert granted == set(APP_READABLE_COLUMNS), (
        "materials 上 fastagent_app 的可读列变了。"
        f"多出来：{sorted(granted - APP_READABLE_COLUMNS)}；"
        f"少掉了：{sorted(APP_READABLE_COLUMNS - granted)}。"
        "多出来的那些先判断是句柄还是密钥；少掉的先看读路由还跑不跑得起来。"
    )


@pytest.mark.parametrize("column", CREDENTIAL_COLUMNS)
def test_the_app_role_really_cannot_read_a_credential_column(
    connection: Connection, column: str
) -> None:
    """**真读一次**：`SET ROLE fastagent_app` 之后选那一列会抛权限错。

    前三条问的是系统目录。目录说得对、而实际仍然读得到（别处一条
    `GRANT`、一个继承来的角色、一张视图）——这种事只有真读一次才看得见。

    **同一个角色下先读一列准许的**：不这么做，「抛了权限错」与「这个角色
    根本连不上这张表」长得一模一样，而后者会让这条用例在守卫失效之后
    依然绿（只要它以别的理由继续抛）。**对照在这里，不在 message 的措辞里** ——
    列级拒绝的报文逐字是 `permission denied for table materials`，
    **它不提列名**，所以拿列名去 `in` 报文是一条永远不会成立的断言。
    """

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_app")
    try:
        connection.execute("SELECT slug FROM materials").fetchall()

        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute(
                sql.SQL("SELECT {} FROM materials").format(sql.Identifier(column))
            )
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")


def test_the_app_role_can_still_read_what_the_routes_select(
    connection: Connection,
) -> None:
    """**读路由还跑得起来** —— 换成列级授权最容易顺手掐死的就是它。

    逐字选两条 route 选的那十列。这一条要是红了，`GET /materials` 在真实环境里
    也会红，而契约层不会有任何反应（它根本不连库）。
    """

    connection.execute("SAVEPOINT before_role_switch")
    connection.execute("SET LOCAL ROLE fastagent_app")
    try:
        connection.execute(
            """
            SELECT id, slug, version, filename, media_type, byte_size,
                   content_hash, status, archived_at, created_at
              FROM materials WHERE tenant_id IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.execute("ROLLBACK TO SAVEPOINT before_role_switch")
        connection.execute("RESET ROLE")
