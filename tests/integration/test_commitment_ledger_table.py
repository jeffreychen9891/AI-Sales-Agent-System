"""`commitments` 在**连库那一层**真的收下一行、真的拦住不该收的那些。

## 为什么这份文件必须存在

本轮交接里那条反复出现的形状：**契约层全绿而真链路是断的**
（D4 那次最完整 —— 策略选了、候选造了、生成结果造了，最后一行丢掉，
审计里记着，而 `intents == 0` / `outbox == 0`。**账面自洽，事实没发生**）。

台账这一批的每一条契约层守卫，证的都是「形状对不对」：
三份词表相等、写入点只有一处、`pipeline` 调得到那两个函数。
**它们一条都不证明库里真的会多出一行** —— 那需要一个真的 Postgres，
也就是这份文件。

## 它证的四件

1. 一行合法的承诺存得进去（RLS、授权、十四列的 CHECK 全都让路）；
2. **重放不会记成两条** —— `dedupe_key` 那道 `UNIQUE` 真的在；
3. 三个词表的 CHECK 真的会拒（迁移里那三行不是装饰）；
4. **`fastagent_app` 读不到 `summary`** —— `063` 给的是列级 `SELECT`，
   而那一列是客户原话的摘要。

**第 4 条是这批里唯一一处权限收窄**，也是唯一只有连库才验得了的那一条。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection, errors

from tests.conftest import TENANT_A, _migration_url

pytestmark = [pytest.mark.security]

CONVERSATION = uuid.UUID("c0000000-0000-4000-8000-00000000c001")
EVENT = uuid.UUID("e0000000-0000-4000-8000-00000000e001")


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "tenant_id": TENANT_A,
        "id": uuid.uuid4(),
        "conversation_id": CONVERSATION,
        "party": "customer",
        "type": "take_action",
        "summary": "客户说下周三之前把材料发过来",
        "source_event_id": EVENT,
        "due_at": None,
        "status": "proposed",
        "evidence_refs": json.dumps([str(EVENT)]),
        "scheduled_event_id": None,
        "generation": 0,
        "version": 0,
        "dedupe_key": f"{EVENT}:0",
    }
    row.update(overrides)
    return row


def _insert(connection: Connection, row: dict[str, Any]) -> Any:
    return connection.execute(
        """
        INSERT INTO commitments(
            tenant_id, id, conversation_id, party, type, summary, source_event_id,
            due_at, status, evidence_refs, scheduled_event_id, generation, version,
            dedupe_key
        ) VALUES (
            %(tenant_id)s, %(id)s, %(conversation_id)s, %(party)s, %(type)s, %(summary)s,
            %(source_event_id)s, %(due_at)s, %(status)s, %(evidence_refs)s,
            %(scheduled_event_id)s, %(generation)s, %(version)s, %(dedupe_key)s
        )
        ON CONFLICT (tenant_id, conversation_id, dedupe_key) DO NOTHING
        """,
        row,
    )


@pytest.fixture()
def tenant(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """一条真实的会话行 —— `commitments` 有 `(tenant_id, conversation_id)` 外键。

    **不编一个 conversation id**：编的那个会在 fixture setup 就炸，
    而那种红指着的是夹具、不是被测的东西（`test_contact_memories_table`
    的第一版为此给出 12 个 setup error，那段注释写在它开头）。
    """

    assert seeded_database["tenant_a"] == TENANT_A
    with Connection.connect(_migration_url()) as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (TENANT_A,)
        ).fetchone()
        assert release is not None, "播种夹具里没有 profile_release —— 换一个取法"
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (TENANT_A, CONVERSATION, release[0]),
        )
        connection.commit()
        yield connection
        connection.execute("DELETE FROM commitments WHERE tenant_id = %s", (TENANT_A,))
        connection.execute(
            "DELETE FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (TENANT_A, CONVERSATION),
        )
        connection.commit()


def test_a_legal_commitment_lands(tenant: Connection) -> None:
    """一行合法的承诺存得进去 —— **这一条红了，上面那些契约守卫全是空的**。"""

    _insert(tenant, _row())
    tenant.commit()
    count = tenant.execute(
        "SELECT count(*) FROM commitments WHERE tenant_id = %s", (TENANT_A,)
    ).fetchone()
    assert count is not None and count[0] == 1


def test_a_replay_does_not_record_it_twice(tenant: Connection) -> None:
    """**同一条事件被重放，台账仍然只有一行。**

    回合是至少一次的。没有 `dedupe_key` 那道 `UNIQUE` 时，一次重放会把同一条
    承诺记两遍 —— 而两遍在台账上读起来是「客户答应了两次」，
    **那不是一个错误提示，是一条看起来正常的假事实**。
    """

    _insert(tenant, _row())
    tenant.commit()
    # 重放：同一个 `dedupe_key`，而 `id` 是新的（真实重放正是这样 —— 新 uuid4）。
    _insert(tenant, _row(id=uuid.uuid4()))
    tenant.commit()

    count = tenant.execute(
        "SELECT count(*) FROM commitments WHERE tenant_id = %s", (TENANT_A,)
    ).fetchone()
    assert count is not None and count[0] == 1, "重放记成了两条 —— dedupe_key 那道 UNIQUE 没生效"


@pytest.mark.parametrize(
    ("column", "value"),
    [("party", "nobody"), ("type", "guess_something"), ("status", "quietly_dropped")],
)
def test_a_value_outside_the_vocabulary_is_refused(
    tenant: Connection, column: str, value: str
) -> None:
    """三个词表的 `CHECK` 真的会拒。

    契约层那条「契约 == 迁移」只证明**两张清单写的一样**；
    它证不了那三行 `CHECK` 真的在生效 —— 一条写错的 `CHECK` 语法
    在 Postgres 里会建表失败，而一条**漏掉的** `CHECK` 不会。
    """

    with pytest.raises(errors.CheckViolation):
        _insert(tenant, _row(**{column: value}))
    tenant.rollback()


def test_an_empty_evidence_list_is_refused(tenant: Connection) -> None:
    """**没有证据的承诺存不进去** —— 图纸 §5 最后一条那句话的执行点。

    契约里是 `minItems: 1`，库里是 `jsonb_array_length(...) BETWEEN 1 AND 8`。
    两道都在，而只有这一条证得了后者。
    """

    with pytest.raises(errors.CheckViolation):
        _insert(tenant, _row(evidence_refs=json.dumps([])))
    tenant.rollback()


#: `063` 给四个角色授了什么。**整个面，不是「至少有」** ——
#: 多一格少一格都要有人在这里写一行。形状照 `test_contact_memories_grants`。
EXPECTED_TABLE_PRIVILEGES = {
    "fastagent_worker": {"SELECT", "INSERT", "UPDATE"},
    # **`fastagent_app` 这一格是空的，而它不是漏的**：api 拿的是**列级** SELECT，
    # 见下面那条。`has_table_privilege` 对列级授权回 False，所以这里必须是空集。
    "fastagent_app": set(),
    "fastagent_ingress": set(),
    "fastagent_scheduler": set(),
}

#: api 读得到的那几列，以及**读不到的那一列**。
APP_READABLE = ("tenant_id", "conversation_id", "id", "party", "type", "status", "due_at")
APP_FORBIDDEN = ("summary", "evidence_refs", "dedupe_key", "source_event_id")


@pytest.mark.parametrize("role", sorted(EXPECTED_TABLE_PRIVILEGES))
def test_the_table_grants_are_exactly_the_declared_ones(tenant: Connection, role: str) -> None:
    """表级授权是**整个面**。worker 写，其余三个角色表级什么都没有。"""

    held = {
        verb
        for verb in ("SELECT", "INSERT", "UPDATE", "DELETE")
        if tenant.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, "commitments", verb)
        ).fetchone()[0]
    }
    assert held == EXPECTED_TABLE_PRIVILEGES[role], (
        f"{role} 对 commitments 持有 {sorted(held) or '（无）'}，"
        f"而声明的是 {sorted(EXPECTED_TABLE_PRIVILEGES[role]) or '（无）'}"
    )


def test_the_api_role_cannot_read_the_customers_words(tenant: Connection) -> None:
    """**`fastagent_app` 读不到 `summary`** —— 这批里唯一一处权限收窄。

    接管页那一格要的是「登记了几条、什么状态」，**不要客户原话的摘要**。
    给整表 `SELECT` 就等于顺手把它给了出去，而那一列正是本仓一直在收窄的那一类
    （`policy_decisions.context` 那次的教训）。

    **只有连库验得了这一条**：列级授权在 Python 这一侧完全看不见 ——
    代码里那条 `SELECT summary` 写出来是合法的，拒它的是数据库。
    """

    for column in APP_READABLE:
        allowed = tenant.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
            ("fastagent_app", "commitments", column),
        ).fetchone()
        assert allowed is not None and allowed[0], f"api 读不到 `{column}` —— 接管页那一格要它"

    for column in APP_FORBIDDEN:
        allowed = tenant.execute(
            "SELECT has_column_privilege(%s, %s, %s, 'SELECT')",
            ("fastagent_app", "commitments", column),
        ).fetchone()
        assert allowed is not None and not allowed[0], (
            f"api 读得到 `{column}` 了 —— `063` 给的应当是列级 SELECT，"
            "而 `summary` 是客户原话的摘要"
        )
