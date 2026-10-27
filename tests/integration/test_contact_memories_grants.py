"""`contact_memories` 上谁有哪个动词 —— **包括「没有 `UPDATE`」那一格**。

**那一格就是裁定八在数据库里的样子。** 「模型只能追加，不能重写已有的那一份」
（ADR-0131，排期 §4.3.4）今天在两处成立：一处是写入路径的拒绝
（`_remember_turn_refusal` 的第 4 条，操作员读得到理由），
**一处是这个角色根本没有 `UPDATE`**。
两处表达同一条规则，而只有后者在有人改代码时仍然成立。

**E4-3（`060`）给 `fastagent_app` 授了 `UPDATE`，而上面那句话一个字都不用改。**
裁定八有两半，指向相反：「**摘要人工可编辑，模型不能自改**」。
模型写在 `fastagent_worker` 上，登录的员工写在 `fastagent_app` 上——
**给后者授 `UPDATE` 是这条裁定的前半句，不是对后半句的例外。**

交接文档预告「那条用例会因此变红」。**实际变红的只有参数化那条的
`fastagent_app` 一格**（它断言整个面，所以 `EXPECTED` 多一行就得有人写一行）；
**下面那条专钉裁定八的用例问的是 `fastagent_worker`，它仍然是绿的，而那正是证据**：
如果它也红了，说明这次授权摸到了模型的角色，那就该停下来而不是改断言。

形状照 `test_knowledge_grants.py`，理由逐字相同：
**一张表 × 一组动词 × 一组角色，把整个面摊开断言**——
只断言「该有的有」时，一次多发的 `GRANT` 永远不会被发现。
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import Connection

from tests.conftest import _migration_url

pytestmark = [pytest.mark.security]

TABLE = "contact_memories"
VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")
ROLES = ("fastagent_app", "fastagent_worker", "fastagent_ingress", "fastagent_scheduler")

#: **整个授权面，摊开写。**
#:
#: `fastagent_worker` 的 `SELECT` 有一个调用方：`prepare` 每回合读一次
#: 「这个 contact 记着哪几件事」（`_remembered_memories`，
#: 2026-09-18 之前叫 `_remembered_fields`，那时只取键）。
#: `INSERT` 是 `_remember_write`。**两者都跑在 worker 的连接上**
#: （`Database(settings.worker_database_url)`），这也是门禁第一轮
#: 131 条 `permission denied` 的那个角色。
#:
#: **空着的那些格子是决定，不是遗漏**：
#:
#: * **`worker` 没有 `UPDATE`**——见模块 docstring。这一格**没有随 E4-3 改变**，
#:   见下面那条专门的用例。
#: * **`DELETE` 一个角色都没有**——删除走 `purge_tenant_data`
#:   （`SECURITY DEFINER`，权限附在一段被审过的函数体上，`043` 的原话）。
#:
#: **`fastagent_app` 这一行是 E4-3（`060`）加的**，而它曾经空着的那句理由
#: 值得原样留在这里：「API 只通过 E4-3 的 route 碰这张表，那一批到了再授。
#: **本批的第一版正是授给了它**，而那样一条红都修不掉：写这张表的是 worker，
#: 不是 API。」——**`057` 那次授错角色的代价，和 `060` 这次授对角色的理由，
#: 是同一件事的两面**：先核调用方跑在哪条连接上，再写 `GRANT`。
#:
#: 三个动词各自的调用方（`060` 的注释里有更长的一份）：
#:
#: * `SELECT` —— `GET /contacts/{id}/memories`，与三条写路径的前置检查；
#: * `INSERT` —— `POST` 写 revision 1，`PUT` 写下一个 revision
#:   （**修订是新行，不是改行**）；
#: * `UPDATE` —— **只够做两次生命周期移动**，其余全被 `054` 的触发器在数据库层
#:   顶回去：`PUT` 把被改的那行标 `superseded`，`DELETE` 标 `tombstoned` 并盖时刻。
EXPECTED: dict[tuple[str, str], set[str]] = {
    (TABLE, "fastagent_worker"): {"SELECT", "INSERT"},
    (TABLE, "fastagent_app"): {"SELECT", "INSERT", "UPDATE"},
}


@pytest.fixture(scope="module")
def db() -> Any:
    with Connection.connect(_migration_url()) as connection:
        yield connection


@pytest.mark.parametrize("role", ROLES)
def test_the_granted_verbs_are_exactly_the_declared_ones(role: str, db: Any) -> None:
    """**两个方向**：该有的有，不该有的没有。"""

    held = {
        verb
        for verb in VERBS
        if db.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, TABLE, verb)
        ).fetchone()[0]
    }
    assert held == EXPECTED.get((TABLE, role), set()), (
        f"{role} 对 {TABLE} 持有 {sorted(held) or '（无）'}，"
        f"而声明的是 {sorted(EXPECTED.get((TABLE, role), set())) or '（无）'}。"
        "多一个动词，要在 057 的注释里写明它的调用方是谁"
    )


def test_the_model_cannot_rewrite_a_memory_because_the_role_cannot(db: Any) -> None:
    """**裁定八的第二处表达，单独钉。**

    上面那条参数化用例已经覆盖了这一格，**而这一条仍然值得存在**：
    它把「为什么这一格是空的」写在名字里。
    上面那条红的时候读的人看到「多了一个动词」，
    这一条红的时候读的人看到**是哪条裁定被推翻了**。
    """

    can_update = db.execute(
        "SELECT has_table_privilege(%s, %s, 'UPDATE')", ("fastagent_worker", TABLE)
    ).fetchone()[0]
    assert not can_update, (
        "fastagent_worker 拿到了 contact_memories 的 UPDATE —— "
        "裁定八说模型只能追加不能重写，而这项权限让「重写」重新变成可能的。"
        "E4-3 要 supersede / tombstone 时请在它自己的迁移里授权并改本条。"
    )
