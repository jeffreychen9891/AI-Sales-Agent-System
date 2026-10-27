"""发布一版知识库，或回滚到旧的一版。

**知识库第 5 批。** 第 4 批的导入脚本建出来的是 `draft`；本脚本是让它成为
「当前那一版」的唯一途径，也是回滚的唯一途径。

## 裁定七：**会话不 pin，所以发布与回滚都是立即生效的**

每次检索读的是 `status = 'published'` 的那一版，没有任何东西被钉住。于是：

* **发布** = 新的那一版转 `published`，旧的转 `superseded`。
  **在飞会话下一次检索就读到新的** —— 不需要任何人重启或重新绑定；
* **回滚** = 把旧的那一版重新置为 `published`。
  **同样下一次检索就生效**（裁定七 §5.4.1 第 2 条）。

**「当前只有一版」不是靠这个脚本守的**，是靠
`knowledge_releases_one_published_idx`（`050` 那条部分唯一索引）。
本脚本在一个事务里把旧的转走、新的转来 —— **顺序反了数据库会拒**，
而那正是想要的：两版同时 published 不是一个「降级状态」，
是一个没有答案的问题，而检索层会拿它碰到的第一条当答案。

## 回滚会留下痕迹，而发布不需要

`rolled_back_from` 记的是**这次发布顶掉了哪一版**，只在回滚时填。
理由写在 `050` 的列注释里：回滚之后旧的那一版只是「又变成 published 了」，
**没有任何东西说得出它是往回走的一步**。

## 它不检查向量，因为数据库检查

T281 的那条约束触发器（`052`）在转 `published` 的那一刻查
「这一版的 chunk 都向量化过吗」。**本脚本不重复那一遍** ——
一条规则两个执行点，漂开的那一天没有东西会红。
这里只负责把数据库的拒绝翻译成一句人话。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/runtime/src"))

from psycopg import Connection, errors  # noqa: E402
from psycopg.rows import DictRow, dict_row  # noqa: E402


class PublishRefused(RuntimeError):
    """不能发布，**而且理由要读得懂**。"""


def _current(connection: Connection[DictRow], tenant_id: UUID) -> DictRow | None:
    return connection.execute(
        "SELECT id, version FROM knowledge_releases "
        " WHERE tenant_id = %s AND status = 'published'",
        (tenant_id,),
    ).fetchone()


def _release_of(connection: Connection[DictRow], tenant_id: UUID, version: int) -> DictRow:
    found = connection.execute(
        "SELECT id, version, status FROM knowledge_releases "
        " WHERE tenant_id = %s AND version = %s",
        (tenant_id, version),
    ).fetchone()
    if found is None:
        raise PublishRefused(f"这个租户没有 v{version} 这一版")
    return found


def _supersede_current(connection: Connection[DictRow], tenant_id: UUID) -> UUID | None:
    """把当前那一版转 `superseded`，返回它的 id（没有就 `None`）。

    **先转走再转来**，顺序不能反：部分唯一索引不允许两版同时 published。
    """

    current = _current(connection, tenant_id)
    if current is None:
        return None
    connection.execute(
        "UPDATE knowledge_releases SET status = 'superseded', superseded_at = now()"
        " WHERE tenant_id = %s AND id = %s",
        (tenant_id, current["id"]),
    )
    return UUID(str(current["id"]))


def publish(connection: Connection[DictRow], *, tenant_id: UUID, version: int) -> dict[str, object]:
    target = _release_of(connection, tenant_id, version)
    if target["status"] == "published":
        raise PublishRefused(f"v{version} 已经是当前那一版了")

    displaced = _supersede_current(connection, tenant_id)
    rolling_back = target["status"] == "superseded"
    connection.execute(
        "UPDATE knowledge_releases"
        # **`published_at = now()`，不是 `coalesce`**（独立审查 2026-09-12，P3-2）。
        # 先前回滚保留原值，而 `--tenant` 的列表把它打出来 ——
        # 于是「这一版从什么时候起是当前」在库里答不出来。
        # `rolled_back_from` 记的是**顶掉了谁**，不是**何时**。
        "   SET status = 'published', published_at = now(),"
        "       superseded_at = NULL, rolled_back_from = %s"
        " WHERE tenant_id = %s AND id = %s",
        (displaced if rolling_back else None, tenant_id, target["id"]),
    )
    return {
        "version": version,
        "displaced": displaced,
        "rolled_back": rolling_back,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="租户 uuid")
    parser.add_argument(
        "--version", type=int, help="要成为当前那一版的版本号；不给就只看现状"
    )
    args = parser.parse_args()

    url = os.getenv("FASTAGENT_DATABASE_URL", "").strip()
    if not url:
        print("FASTAGENT_DATABASE_URL 没有设置", file=sys.stderr)
        return 2
    tenant_id = UUID(args.tenant)

    with Connection.connect(url, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))

        if args.version is None:
            rows = connection.execute(
                "SELECT version, status, published_at, superseded_at,"
                "       (SELECT count(*) FROM knowledge_chunks c"
                "         WHERE c.tenant_id = r.tenant_id AND c.release_id = r.id) AS chunks,"
                "       (SELECT count(embedding) FROM knowledge_chunks c"
                "         WHERE c.tenant_id = r.tenant_id AND c.release_id = r.id) AS embedded"
                "  FROM knowledge_releases r WHERE tenant_id = %s ORDER BY version",
                (tenant_id,),
            ).fetchall()
            if not rows:
                print("这个租户还没有任何一版知识库。")
                return 0
            for row in rows:
                mark = " ← 当前" if row["status"] == "published" else ""
                print(
                    f"  v{row['version']:<3} {row['status']:<11}"
                    f" chunk {row['embedded']}/{row['chunks']} 已向量化{mark}"
                )
            return 0

        try:
            result = publish(connection, tenant_id=tenant_id, version=args.version)
        except PublishRefused as refused:
            print(refused)
            return 3
        except errors.CheckViolation as violation:
            # T281 的那条触发器。**它的消息本来就是写给人看的**，
            # 这里不重写，只把 psycopg 的包装剥掉。
            print(str(violation).strip().splitlines()[0])
            return 4
        connection.commit()

    if result["rolled_back"]:
        print(
            f"已回滚到 v{result['version']}（顶掉的是 {result['displaced']}）。"
            "**下一次检索就读到它** —— 裁定七之后没有任何东西需要重新绑定。"
        )
    else:
        print(
            f"已发布 v{result['version']}。"
            + (
                "旧的那一版已转 superseded。"
                if result["displaced"]
                else "这是这个租户的第一版。"
            )
            + "**在飞会话下一次检索就读到它。**"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
