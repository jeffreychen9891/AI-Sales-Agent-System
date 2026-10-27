"""每一个 `audit_events` 写入点，都要能真的写进去。

## 它补的是什么

2026-09-22，运维在 stage 上量到这个：

```
ERROR:  null value in column "reason_code" of relation "audit_events"
        violates not-null constraint
STATEMENT:
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES ($1, $2, NULL, 'inference.memories_sent',
                  'conversation_aggregate', $3, $4, NULL, $5)
```

第 7 个位置是 `reason_code`，而那一列是 `NOT NULL`。
`inference.memories_sent` / `inference.memories_cited` 这一对留痕
**从上线到那天一行都没写进去过**：

```sql
select action, count(*) from audit_events where action like 'inference.%';
-- 2026-09-22T15:47Z 实测：空集
```

## ⚠️ 它藏了这么久，是因为外面那个 bug 一直遮着它

触发条件要求联系人至少有一条 active 记忆，而 `contact_memories` 因为外键
指错列**全库 0 行贯穿了这套系统的一生**。迁移 `073` 把外键改对之后，
实测 **91 秒**：`15:40:31Z` 第一次写入成功 → `15:42:02Z` 下一个回合当场炸。
那两条 INSERT 写在 `prepare` 的事务上，于是**整轮回滚**，
任何有记忆的联系人每一轮都死在 `prepare` 里，重试五次进死信。

**「从未成功执行过，因此从未有人发现」** —— 这条用例守的就是这个形状。

## 为什么是源码扫描，而它的边界在哪

本仓有一条记法：**源码扫描看不见代码跑没跑**（`if False:` 它照样绿）。
这条用例因此**不声称**「每个写入点都执行过」——它声称的是更窄、
但正是这次出事的那一件：**没有任何写入点给一个 `NOT NULL` 的列传字面 `NULL`**。

而它抓得住这次那个 bug，理由是那个 bug 的形状就是源码里的一个字面量：
`NULL` 与占位符 `%s` 在 SQL 文本里长得几乎一样，
**同族另外 39 个写入点全传具名常量，只有那两个是字面量**。

「它真的跑得起来」那一半由连库的用例顶（`tests/integration`）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPOSITORY_ROOT / "infrastructure" / "migrations"
SOURCE_ROOTS = (
    REPOSITORY_ROOT / "services",
    REPOSITORY_ROOT / "packages",
)

#: `INSERT INTO audit_events(<列>) VALUES (<值>)`，跨行。
INSERT = re.compile(
    r"INSERT\s+INTO\s+audit_events\s*\((?P<columns>[^)]*)\)\s*VALUES\s*\((?P<values>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)

#: 今天在架的写入点数。**按数断言**：新加一个写入点这条会红，
#: 而红的时候人要做的是确认新那处也没给 `NOT NULL` 的列传 `NULL`。
#: 2026-09-23 现读。
WRITE_SITES = 41


def _not_null_columns() -> frozenset[str]:
    """`audit_events` 里哪几列是 `NOT NULL` —— **从迁移里读，不在这里抄一遍**。

    抄一遍就是同一条规则两个定义点：迁移加一列 `NOT NULL` 而这里没跟上，
    这条用例会安静地不再守它。
    """

    ddl = (MIGRATIONS / "001_m0.sql").read_text(encoding="utf-8")
    body = ddl.split("CREATE TABLE IF NOT EXISTS audit_events (", 1)[1].split(");", 1)[0]
    columns: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith("PRIMARY KEY"):
            continue
        if "NOT NULL" not in stripped:
            continue
        name = stripped.split()[0]
        # `DEFAULT` 补得上的列，漏传不会炸 —— 本条只管必须由调用方给值的那些。
        if "DEFAULT" in stripped:
            continue
        columns.add(name)
    return frozenset(columns)


def _sites() -> list[tuple[Path, list[str], list[str]]]:
    found: list[tuple[Path, list[str], list[str]]] = []
    for root in SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "audit_events" not in text:
                continue
            for match in INSERT.finditer(text):
                columns = [c.strip() for c in match.group("columns").split(",") if c.strip()]
                values = [v.strip() for v in match.group("values").split(",") if v.strip()]
                found.append((path, columns, values))
    return found


def test_the_scanner_still_finds_every_write_site() -> None:
    """**先证明扫描器没瞎。**

    一条「没有任何写入点传 NULL」的断言，在扫描器一个点都没找到时
    **同样为真** —— `CLAUDE.md` 第三节末尾那句「守卫本身也可能是空的」。
    所以先按数钉住它找到了多少个。
    """

    sites = _sites()
    assert len(sites) == WRITE_SITES, (
        f"扫描器找到 {len(sites)} 个 audit_events 写入点，而登记的是 {WRITE_SITES}。"
        "新加了写入点就把这个数改过来，并确认新那处没给 NOT NULL 的列传 NULL。"
    )


def test_the_column_and_value_lists_line_up() -> None:
    """列数与值数必须一一对应 —— 对不上的话下面那条就在错位比较。"""

    for path, columns, values in _sites():
        assert len(columns) == len(values), (
            f"{path.relative_to(REPOSITORY_ROOT)}：{len(columns)} 列 / {len(values)} 值 —— "
            f"{columns} vs {values}"
        )


def test_no_write_site_hands_a_not_null_column_a_literal_null() -> None:
    """**这一条就是 2026-09-22 那次事故。**

    `NOT NULL` 的列拿到字面 `NULL`，每一次执行都抛，而那条 INSERT 写在
    `prepare` 的事务上 —— 整轮回滚，客户侧表现为「没反应」。
    """

    required = _not_null_columns()
    assert "reason_code" in required, f"从迁移里读出来的必填列是 {sorted(required)}"

    offenders: list[str] = []
    for path, columns, values in _sites():
        for column, value in zip(columns, values, strict=True):
            if column in required and value.upper() == "NULL":
                offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}：{column} 传了字面 NULL")

    assert not offenders, (
        "这些写入点每一次执行都会抛 NotNullViolation：\n  " + "\n  ".join(offenders)
    )


def test_actor_id_may_still_be_null() -> None:
    """⚠️ **反侧：可空的列传 `NULL` 是合法的，而且今天到处都是。**

    没有这一条，上面那条用「一律不许传 NULL」也能绿 —— 而那会把
    `actor_id`（平台自己动手、没有人类 actor）逼成一个假 uuid。
    **两个方向都要有东西接着。**
    """

    required = _not_null_columns()
    assert "actor_id" not in required

    nulls = sum(
        1
        for _, columns, values in _sites()
        for column, value in zip(columns, values, strict=True)
        if column == "actor_id" and value.upper() == "NULL"
    )
    assert nulls > 0, "一个 actor_id 传 NULL 的写入点都没有了 —— 上面那条可能被写成了一律禁止"
