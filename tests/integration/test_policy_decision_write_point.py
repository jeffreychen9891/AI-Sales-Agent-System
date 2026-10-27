"""那一行**每一格都落在它该落的列上**（裁定二 / ADR-0137 §二）。

收编成唯一写入点换来一件好处，也换来一个新的失败模式：
**十三列一份参数元组，错位一格，全仓所有判定一起错。**
七处各写一遍的时候，错位只毁一处；现在它毁全部。

而这种错位**过得了任何静态检查**：`policy_id` 与 `policy_version` 都是 `text`，
`hook` 与 `effect` 都是 `text` 且各有 CHECK——`('allow', 'pre_execute')` 这种
交换会被 CHECK 拦住，`('proactivity-window', '1.0.0')` 交换成
`('1.0.0', 'proactivity-window')` **不会被任何东西拦住**，
它会安安静静地写下一行「策略名叫 1.0.0」的判定。

所以这一份用例写一行**每格都不一样、且认得出自己**的判定，
再把十三列全读回来逐格比对。它是行为面的那一半：
`tests/contracts/test_policy_output.py` 管「不合法的值进不来」，
这一份管「合法的值没走错门」。

**为什么要连库**：错位的症状全在读回来的那一刻，
而一个假连接只会把我写进去的错位原样还给我。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.policy_output import PolicyOutput, record_policy_decision
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]

#: 每一格都认得出自己。**没有两格的值长得像**——错位一格，逐格比对里
#: 至少有两格会不一样，而不是「两个 text 悄悄换了个位置」。
HOOK = "post_plan"
EFFECT = "manual_review"
REASON_CODE = "WRITE_POINT_COLUMN_ORDER"
POLICY_NAME = "a-policy-name-not-a-version"
POLICY_VERSION = "9.9.9"


def _a_conversation(tenant_id: UUID) -> UUID:
    """一个真的存在于 `conversation_aggregates` 里的会话。

    **`new_conversation()` 只算出 id，不建行** —— 行是第一条消息进来时才建的，
    而 `policy_decisions` 有 `FOREIGN KEY (tenant_id, conversation_id)`。
    第一版只叫了 `new_conversation()`，两条用例都撞在那个外键上：
    **那个外键本身就是「判定必须挂在一个真会话上」的意思**，绕开它才是错的。
    """

    conversation_id = new_conversation(tenant_id)
    with Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]) as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None, "夹具给每个租户都发布了一版配包"
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
            VALUES (%s, %s, %s)
            """,
            (tenant_id, conversation_id, release[0]),
        )
    return conversation_id


def _read_back(tenant_id: UUID, row_id: UUID) -> dict[str, Any]:
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        row = connection.execute(
            """
            SELECT hook, effect, reason_code, modifications, obligations,
                   policy_id, policy_version, next_allowed_at, context,
                   conversation_id, intent_id
              FROM policy_decisions WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, row_id),
        ).fetchone()
    assert row is not None, "写入点写完之后那一行不在表里"
    return dict(row)


def test_every_column_holds_the_value_it_was_given(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """十三列逐格比对 —— **`manual_review` 这一行在生产里今天还没人发出过。**

    这条用例是它第一次真的被写进库：词表按图纸写全（`EFFECTS` 四值），
    而「写全了的词表真的写得进去」本身是一件要证的事 ——
    `effect` 那一列的 CHECK 里有它，可在此之前**从来没有一行验证过这件事**。

    `intent_id` 传的是 `None`，而它**不是**懒省事：
    拒绝路径上常常没有意图行（`pre_inference` 的成本拒绝、`pre_execute` 的
    配额拒绝都是），而这一列有 `FOREIGN KEY (tenant_id, intent_id)` ——
    传一个不存在的 id 会撞外键，`NULL` 才是「这条判定不针对某一个意图」。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = _a_conversation(tenant_id)
    row_id = uuid4()
    lifts_at = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

    database = Database(settings.worker_database_url)
    with database.transaction(tenant_id=tenant_id) as connection:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=row_id,
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook=HOOK,
                effect=EFFECT,
                reason_code=REASON_CODE,
                policy_id=POLICY_NAME,
                policy_version=POLICY_VERSION,
                obligations=("create_manual_task", "audit"),
                context={"only_here": "context", "depth": {"nested": 1}},
                modifications={"only_here": "modifications"},
                next_allowed_at=lifts_at,
            ),
        )

    row = _read_back(tenant_id, row_id)

    assert row["hook"] == HOOK
    assert row["effect"] == EFFECT
    assert row["reason_code"] == REASON_CODE
    # **这两格是本用例的主目标。** 两列都是 `text`、都没有 CHECK，
    # 交换之后没有任何东西会报错 —— 只有一行说「策略名叫 9.9.9」的证据。
    assert row["policy_id"] == POLICY_NAME
    assert row["policy_version"] == POLICY_VERSION
    # 两个 jsonb 也各自带着只属于自己的值，交换同样是静默的。
    assert row["context"] == {"only_here": "context", "depth": {"nested": 1}}
    assert row["modifications"] == {"only_here": "modifications"}
    # **顺序原样保留**：`obligations` 是数组而不是集合，
    # 排序会让一次「写入点悄悄整理了顺序」变得看不出来。
    assert row["obligations"] == ["create_manual_task", "audit"]
    assert row["next_allowed_at"] == lifts_at
    assert UUID(str(row["conversation_id"])) == conversation_id
    assert row["intent_id"] is None


def test_an_omitted_modifications_lands_as_an_empty_object_not_json_null(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**没传 `modifications` 时库里是 `{}`，不是 `null`。**

    `modifications jsonb NOT NULL DEFAULT '{}'` 拦不住这件事：
    `Jsonb(None)` 写出来的是 `'null'::jsonb`，**它过得了 NOT NULL**。
    于是「这条判定没有修改」与「这条判定的修改字段坏了」在库里长得不一样，
    而读那一行的人分不出是哪种。

    这不是假想的：旧那七处里，`decision.policy_context` 是 `dict | None`，
    而 `Jsonb(decision.policy_context)` 原样塞进去 ——
    换成写入点时 mypy 当场指出了这个洞（`pipeline.py` 那两条 `assert` 就是它）。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = _a_conversation(tenant_id)
    row_id = uuid4()

    database = Database(settings.worker_database_url)
    with database.transaction(tenant_id=tenant_id) as connection:
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=row_id,
            conversation_id=conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="pre_generation",
                effect="allow",
                reason_code="DEFAULTS_ARE_EMPTY_NOT_NULL",
                policy_id="write-point-defaults",
                policy_version="1.0.0",
                obligations=(),
                context={},
            ),
        )

    row = _read_back(tenant_id, row_id)

    assert row["modifications"] == {}
    assert row["context"] == {}
    # 空义务列表也是 `[]` 而不是 `null` —— 同一个陷阱的另一半。
    assert row["obligations"] == []
    assert row["next_allowed_at"] is None
