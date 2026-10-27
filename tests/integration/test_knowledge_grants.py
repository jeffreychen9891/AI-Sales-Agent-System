"""这三张表上，谁有哪个动词 —— **包括「没有」的那几个**。

**`051` 用三句话声明了它没发什么**（无 `DELETE`、`retrieval_evidence` 一条不发、
`fastagent_worker` 一条不发），**而那三句话没有任何东西守着**
（独立审查 2026-09-12，P2-4）：全仓 `has_table_privilege` 的断言里，
先前没有一条提到这三张表。

**一句没有东西守的声明会漂**，而这一类漂得最安静：多发一个动词不会让任何用例红，
只会让一个角色多出一项没人用、也没人看的权限。

仓里有现成的写法（`tests/security/test_security_birth.py`、
`test_tenant_purge_coverage.py:test_the_worker_holds_no_blanket_delete`），
这一份跟着它们走：**一张表 × 一组动词 × 一组角色，把整个面摊开断言**，
而不是只挑「应该有的」那几格。
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import Connection

from tests.conftest import _migration_url

pytestmark = [pytest.mark.security]

TABLES = (
    "knowledge_releases",
    "knowledge_chunks",
    "knowledge_chunk_identity",
    "retrieval_evidence",
)
VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")
ROLES = ("fastagent_app", "fastagent_worker", "fastagent_ingress", "fastagent_scheduler")

#: **整个授权面，摊开写。** 每一格都是一次决定，而空着的那些格子
#: 正是 `051` 那三句声明说的话。
#:
#: `knowledge_chunks` 的 `UPDATE` 是向量化那一步（`import_knowledge.py:vectorise`）。
#: `knowledge_releases` 的 `UPDATE` 是发布与回滚（`publish_knowledge.py`，`052`）——
#: **它在第 4 批时不在这里**，因为那时它没有调用方。
#: `knowledge_chunk_identity` 只有 `SELECT, INSERT`：身份行建了就不改
#: （改它等于改「这一版正文是什么」，而那是新的一版）。
#: `retrieval_evidence` **不再整行空着**：`056`（6b）发给了 `fastagent_worker`
#: `SELECT` 与**列级** `INSERT`。列级那一半在这张表上看不见 ——
#: `has_table_privilege` 对列级授权返回 false —— 它由
#: `test_knowledge_retrieval.py` 里那条
#: `..._can_read_the_corpus_and_write_the_audit_row_but_not_the_verdict`
#: 逐列钉住，**包括裁定四那三列碰不到**。
#: `knowledge_releases` / `knowledge_chunks` 的 worker `SELECT` 是检索的读路径（`056`）。
EXPECTED: dict[tuple[str, str], set[str]] = {
    # **这里没有 `UPDATE`，而发布确实要改这张表** —— 因为 `052` 发的是**列级**
    # 授权（复审 2026-09-12，N-4），而 `has_table_privilege` 对列级授权返回
    # **false**（返回 true 的是 `has_any_column_privilege`）。
    # 写这条时我以为是反过来的，是门禁纠正的：先前的断言红在这一格。
    # 于是「发布改得动哪几列」全部由下面
    # `test_the_update_on_releases_is_column_level_not_table_level` 钉住。
    ("knowledge_releases", "fastagent_app"): {"SELECT", "INSERT"},
    # **这里也没有 `UPDATE`**，与上面 `knowledge_releases` 同理：`055` 把它
    # 收成了列级 `UPDATE (embedding)`，而 `has_table_privilege` 对列级授权返回
    # false。向量化确实改得动 `embedding` —— 由下面那条列级用例钉住。
    ("knowledge_chunks", "fastagent_app"): {"SELECT", "INSERT"},
    ("knowledge_chunk_identity", "fastagent_app"): {"SELECT", "INSERT"},
    # **`056`（6b，检索层的读路径）**。只读：导入与发布归 `app` 那一侧，
    # 三批（`051` / `052` / `055`）已经把那一侧收到列级，worker 不该能碰知识库的任何一行。
    ("knowledge_releases", "fastagent_worker"): {"SELECT"},
    ("knowledge_chunks", "fastagent_worker"): {"SELECT"},
    # **这里没有 `INSERT`，而检索确实要写这张表** —— `056` 发的是列级
    # （裁定四那三列此刻不该被写者碰到），而 `has_table_privilege` 对列级返回 false。
    # 与上面 `knowledge_releases` / `knowledge_chunks` 的 `UPDATE` 同一个理由。
    ("retrieval_evidence", "fastagent_worker"): {"SELECT"},
}


@pytest.fixture(scope="module")
def db() -> Any:
    with Connection.connect(_migration_url()) as connection:
        yield connection


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("role", ROLES)
def test_the_granted_verbs_are_exactly_the_declared_ones(table: str, role: str, db: Any) -> None:
    """**两个方向**：该有的有，不该有的没有。

    只断言「该有的有」时，一次多发的 `GRANT` 永远不会被发现 ——
    而那正是 P2-4 找到的那一处。
    """

    held = {
        verb
        for verb in VERBS
        if db.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, table, verb)
        ).fetchone()[0]
    }
    assert held == EXPECTED.get((table, role), set()), (
        f"{role} 对 {table} 持有 {sorted(held) or '（无）'}，"
        f"而声明的是 {sorted(EXPECTED.get((table, role), set())) or '（无）'}。"
        "多一个动词也要在 051/052 的注释里写明它的调用方是谁"
    )


def test_nobody_can_delete_from_the_knowledge_tables(db: Any) -> None:
    """`DELETE` 一个角色都没有，而删除确实发生 —— 走的是 `purge_tenant_data`。

    那是一个 `SECURITY DEFINER` 函数，**权限附在一段被审过的函数体上**，
    而不是给某个角色一项通用的擦除能力（`043` 的原话）。
    """

    for table in TABLES:
        for role in ROLES:
            can_delete = db.execute(
                "SELECT has_table_privilege(%s, %s, 'DELETE')", (role, table)
            ).fetchone()[0]
            assert not can_delete, f"{role} 能 DELETE {table}"


def test_the_update_on_releases_is_column_level_not_table_level(db: Any) -> None:
    """发布只改四列，**而表级 `UPDATE` 会顺带放开「这一版是什么」那三列**。

    复审 2026-09-12，N-4：`version` / `content_hash` / `embedding_model` 是这一版的
    身份，改得了它们就等于能把一版 release 变成另一版而不留痕。

    `has_table_privilege` 在表这一层对列级授权返回 true，所以上面那张摊开的表
    **看不出这个差别** —— 要用 `has_column_privilege` 问。
    """

    allowed = {"status", "published_at", "superseded_at", "rolled_back_from"}
    forbidden = {"version", "content_hash", "embedding_model", "embedding_dimension",
                 "authoring_spec", "tenant_id", "id"}
    for column in allowed:
        assert db.execute(
            "SELECT has_column_privilege('fastagent_app', 'knowledge_releases', %s, 'UPDATE')",
            (column,),
        ).fetchone()[0], f"发布要改 {column}，而它没有权限"
    for column in forbidden:
        assert not db.execute(
            "SELECT has_column_privilege('fastagent_app', 'knowledge_releases', %s, 'UPDATE')",
            (column,),
        ).fetchone()[0], f"{column} 不该可写 —— 它是「这一版是什么」的一部分"


def test_the_update_on_chunks_reaches_only_the_embedding_column(db: Any) -> None:
    """向量化只改 `embedding`，**而表级 `UPDATE` 会顺带放开正文与它的 hash**。

    T296 / `055`。`051` 发的是表级，于是「改掉一条已发布 chunk 的正文」
    只被 `text_hash` 那条 CHECK 拦着 —— **而连 `text_hash` 一起改就不会**。

    这条与触发器**不是重复**：触发器管的是「在什么状态下能写」，
    这一条管的是「能碰哪几列」。draft 期间原地改正文，触发器是放行的。
    """

    assert db.execute(
        "SELECT has_column_privilege('fastagent_app', 'knowledge_chunks',"
        " 'embedding', 'UPDATE')"
    ).fetchone()[0], "向量化改不动 embedding，导入就走不完"

    for column in ("body", "text_hash", "ref", "slug", "chunk_version",
                   "release_id", "tenant_id", "question", "source_spans"):
        assert not db.execute(
            "SELECT has_column_privilege('fastagent_app', 'knowledge_chunks', %s, 'UPDATE')",
            (column,),
        ).fetchone()[0], f"{column} 可写 —— 一条已发布的 chunk 就能被原地换掉"
