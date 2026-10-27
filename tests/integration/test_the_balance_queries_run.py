"""采集器那三条余额查询，**真的拿去数据库跑一遍**。

## 它防的那件事已经发生过，而且活了 14 天

`scripts/stage_observability.py` 的第三条查询把 `FILTER` 贴在了外面那个
`extract()` 上而不是聚合函数上。PostgreSQL 每分钟报一次
`syntax error at or near "FILTER"`，从 **2026-09-08 建机当天**起
**连报 20,147 次**，到 2026-09-22 才被运维从容器日志里翻出来
（`docs/handoffs/ops-to-author-the-memory-write-takes-the-turn-with-it-2026-09-22.md`
缺陷 3）。

**后果不是「少了一条曲线」，是「多了一条假曲线」**：三条查询共用一个 `try`，
一条炸就整体走异常分支，于是两条 age 指标双双变成 `864000.0` ——
`BALANCE_AGE_WHEN_UNKNOWN`，**10 天整，一个哨兵不是一个测量值**。
而采集器 `otlp_http_status: 200`，**它推送成功**。

## 既有的守卫为什么是绿的 —— 这两条是一对，不是重复

* `tests/contracts/test_observability_declarative_copy.py` 钉的是**指标名**：
  告警规则引用的名字必须是采集器真的会发的名字。
  **这个 bug 下它全绿**：名字在，也真的发了，发的是异常分支的常量。
* 本文件钉的是**这条查询跑不跑得通**。

`AGENTS.md`：源码扫描看不见代码跑没跑 —— 把一条 SQL 从「一个字符串」
变成「一条真的执行过的语句」，只有连库能做到。

## 为什么是「跑通」而不是「值对不对」

`provider_balance_snapshots` 在测试库里是空的，而这三条查询**在空表上的
返回值正是它们要回答的那一半**（「还没有任何快照」）。
所以这里断言的是**它们执行得了、而且回得出一个能被调用方解析的值** ——
值本身对不对由 `stage_observability` 自己那几条用例管。
"""

from __future__ import annotations

import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from scripts.stage_observability import (
    BALANCE_AGE_SQL,
    BALANCE_COMPARISON_AGE_SQL,
    BALANCE_DIFFERENCE_SQL,
)
from tests.conftest import _migration_url

pytestmark = [pytest.mark.integration]

#: 名字写出来，报错里才说得出是哪一条 —— 三条长得很像。
QUERIES = {
    "difference": BALANCE_DIFFERENCE_SQL,
    "age": BALANCE_AGE_SQL,
    "comparison_age": BALANCE_COMPARISON_AGE_SQL,
}


@pytest.mark.parametrize("name", sorted(QUERIES))
def test_the_query_is_accepted_by_postgres(name: str) -> None:
    """**这条 SQL 在真的 PostgreSQL 上跑得通。**

    删掉本条，`FILTER` 贴错位置那一类改动**不会有任何东西变红** ——
    它会在 stage 上每分钟失败一次，而指标照常推送、`otlp_http_status: 200`。
    """

    with Connection.connect(_migration_url(), row_factory=dict_row) as connection:
        try:
            connection.execute(QUERIES[name]).fetchone()
        except Exception as error:  # noqa: BLE001 - 任何一种失败都是本条要拦的
            pytest.fail(
                f"余额查询 `{name}` 被数据库拒了：{type(error).__name__}: {error}\n"
                f"SQL: {QUERIES[name]}\n"
                "成因（复数，不排他）：聚合函数上的 `FILTER` 写到了外层函数上；"
                "列名改了而这里没跟着；表被改名或撤权。\n"
                "⚠️ 它在 stage 上的样子不是「指标没了」，是"
                "**两条 age 指标变成 864000.0 这个哨兵，而推送仍然成功**。"
            )


def test_the_comparison_query_filters_on_the_aggregate_not_the_wrapper() -> None:
    """`FILTER` 必须贴在 `max(...)` 上 —— 而这一条**不是靠读字符串判的**。

    读字符串只能问「有没有这几个字」。这里问的是**它算出来的是不是那个语义**：
    造两行快照，只有**旧的那一行**有 `difference_cny`，
    于是「最近一次可比较的快照有多老」必须取**旧的那一行**，
    而不是更新的那一行（后者是 `FILTER` 没生效时会得到的答案）。

    `AGENTS.md`：一条关于**行为**的断言，证据只能是跑过一遍。
    """

    with Connection.connect(_migration_url(), row_factory=dict_row) as connection:
        connection.execute("DELETE FROM provider_balance_snapshots")
        # `provider_id` 与 `verdict` 都是 NOT NULL 且无默认值 —— 少了哪个这条
        # INSERT 都会在 setup 就炸，而那会把本条读成「查询坏了」。
        # `verdict` 取的是词表里与这两行相称的两个值：有 difference 的那一行
        # 是比较过的（`matched`），没有的那一行是比不了（`incomparable`）。
        connection.execute(
            "INSERT INTO provider_balance_snapshots"
            "(provider_id, taken_at, difference_cny, verdict) "
            "VALUES ('test', now() - interval '3 hours', 1.00, 'matched'), "
            "       ('test', now() - interval '1 hour', NULL, 'incomparable')"
        )

        comparison_row = connection.execute(BALANCE_COMPARISON_AGE_SQL).fetchone()
        plain_row = connection.execute(BALANCE_AGE_SQL).fetchone()
        assert comparison_row is not None and plain_row is not None
        comparison_age = float(comparison_row["coalesce"])
        plain_age = float(plain_row["coalesce"])

        connection.rollback()

    # 3 小时那一行是唯一可比较的，所以比较龄 ≈ 3h，而快照龄 ≈ 1h。
    assert comparison_age > plain_age, (
        f"比较龄 {comparison_age:.0f}s 不比快照龄 {plain_age:.0f}s 大 —— "
        "`FILTER` 没有生效：它取到了那条 `difference_cny IS NULL` 的新快照。"
    )
    assert 3 * 3600 - 300 < comparison_age < 3 * 3600 + 300, (
        f"比较龄 {comparison_age:.0f}s 不在 3 小时附近 —— 取错了行"
    )
