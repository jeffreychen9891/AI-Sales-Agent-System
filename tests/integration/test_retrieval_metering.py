"""嵌入调用在真台账上的那一行：角色、token、扁平价、结算状态。

**为什么这一份必须连真库**：`model_invocation_costs` 的 `role` 是闭合词表
（`057` 之前没有嵌入这一态），`pricing_period` 也有自己的 CHECK ——
一行写不进去的账在单元替身上是看不出来的。

单元那一侧（绑定从 release 读、上限拒绝降级而不停回合）在
`tests/contracts/test_retrieval_cost_accounting.py`。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.cost import CallContext, CostGovernor
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import ProviderHttpError
from fastagent_v2.worker.retrieval import (
    EMBEDDING_COST_ROLE,
    RETRIEVER_VERSION,
    KnowledgeRetriever,
    query_hash,
)
from psycopg import Connection, sql
from psycopg.rows import dict_row

from tests.conftest import TENANT_A

pytestmark = [pytest.mark.integration]

DIM = 2048
PROMPT_TOKENS = 7


class _Embedder:
    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        return Embedding(
            vector=tuple([1.0] + [0.0] * (DIM - 1)),
            model_id="probe-embedding",
            dimension=DIM,
            prompt_tokens=PROMPT_TOKENS,
        )


class _FailingEmbedder:
    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        raise ProviderHttpError("502 bad gateway", role=EMBEDDING_COST_ROLE)


@pytest.fixture()
def app_db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """建语料的连接（`app` 角色；worker 没有 INSERT 知识库的权限）。"""

    with Connection.connect(Settings.from_env().database_url, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


@pytest.fixture()
def worker_db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    with Connection.connect(
        Settings.from_env().worker_database_url, row_factory=dict_row
    ) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


def _publish_one_chunk(connection: Connection) -> None:
    """一版 published release + 一条向量化过的 chunk。**形状照 `test_knowledge_publish.py`。**"""

    release_id = uuid.uuid4()
    row = {
        "tenant_id": TENANT_A,
        "id": release_id,
        "version": 1,
        "status": "draft",
        "embedding_model": "probe-embedding",
        "embedding_dimension": DIM,
        "distance_metric": "cosine",
        "normalization": "l2",
        "truncation_policy": "reject-over-limit",
        "authoring_spec": "probe",
        "content_hash": f"{1:064x}",
    }
    connection.execute(
        sql.SQL("INSERT INTO knowledge_releases ({}) VALUES ({})").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    )
    body = "kb-000 的正文。"
    # **`text_hash` 必须是正文的 sha256**：表上有一条
    # `knowledge_chunks_text_hash_matches_body` 在验（lite 门禁当场拒了我那个
    # "a" * 64 的假值）。`query_hash` 就是 sha256 的十六进制，两处同一个函数。
    digest = query_hash(body)
    connection.execute(
        "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
        " VALUES (%s, 'kb-000', 1, %s) ON CONFLICT DO NOTHING",
        (TENANT_A, digest),
    )
    chunk = {
        "tenant_id": TENANT_A,
        "id": uuid.uuid4(),
        "release_id": release_id,
        "document_ref": "docs/probe.md",
        "document_version": "1",
        "ordinal": 0,
        "ref": "knowledge:kb-000:v1",
        "slug": "kb-000",
        "chunk_version": 1,
        "question": "探针问题",
        "content_type": "实测数据",
        "confidence": "已实测",
        "body": body,
        "text_hash": digest,
        "source_spans": json.dumps([{"file": "docs/probe.md", "section": "kb-000"}]),
        "embedding": "[" + ",".join(["1.0"] + ["0.0"] * (DIM - 1)) + "]",
    }
    connection.execute(
        sql.SQL("INSERT INTO knowledge_chunks ({}) VALUES ({})").format(
            sql.SQL(", ").join(sql.Identifier(name) for name in chunk),
            sql.SQL(", ").join(sql.Placeholder() for _ in chunk),
        ),
        tuple(chunk.values()),
    )
    connection.execute(
        "UPDATE knowledge_releases SET status = 'published', published_at = now()"
        " WHERE tenant_id = %s AND id = %s",
        (TENANT_A, release_id),
    )
    connection.commit()


def _conversation(seeded: dict[str, object]) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    with Connection.connect(
        Settings.from_env().worker_database_url, row_factory=dict_row
    ) as worker:
        worker.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        worker.execute(
            "INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)"
            " VALUES (%s, %s, %s)",
            (TENANT_A, conversation_id, uuid.UUID(str(seeded["profile_release"]))),
        )
        worker.commit()
    return conversation_id


def _retriever(embedder: Any) -> KnowledgeRetriever:
    database = Database(Settings.from_env().worker_database_url)
    release = active_release()
    return KnowledgeRetriever(
        database=database,
        embedder=embedder,
        release=release,
        governor=CostGovernor(database=database, release=release),
    )


def test_a_retrieval_settles_exactly_one_embedding_row(
    app_db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """一次检索 = 一行 `EMBEDDING` 的账，**结算过，token 是量到的那个数**。

    这一行是「这个租户这个月花了多少」的一部分。`057` 之前它写不进去
    （`role` 的闭合词表里没有这一态），而**写不进去的后果不是报错**：
    没有 `057` 的话这一批要么绕过台账、要么整条路径炸 —— 前者更可能，也更贵。
    """

    _publish_one_chunk(app_db)
    conversation_id = _conversation(seeded_database)
    correlation_id = uuid.uuid4()
    context = CallContext(
        tenant_id=TENANT_A, conversation_id=conversation_id, correlation_id=correlation_id
    )

    result = _retriever(_Embedder()).run(context=context, query="运费规则")

    assert result.outcome == "hit"
    rows = worker_db.execute(
        "SELECT role, status, input_tokens, output_tokens, cost_cny, pricing_period,"
        "       prompt_release, model_id, price_input_snapshot"
        "  FROM model_invocation_costs WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchall()
    assert len(rows) == 1, "一次检索一次嵌入调用，一行账"
    row = rows[0]
    assert row["role"] == EMBEDDING_COST_ROLE
    assert row["status"] == "settled"
    assert row["input_tokens"] == PROMPT_TOKENS, "token 数来自供应商的 usage，不是估的"
    assert row["output_tokens"] == 0, "嵌入没有输出 token —— 0 是事实，不是「没量到」"
    assert row["pricing_period"] == "flat", "扁平价目（`003` 的 CHECK 里有这个值）"
    assert row["prompt_release"] == RETRIEVER_VERSION, (
        "嵌入调用没有提示词，这一列填检索器版本 —— `057` 的列注释写了为什么"
    )
    assert row["model_id"] == active_release().document["embedding"]["model_id"]
    assert float(row["price_input_snapshot"]) == float(
        active_release().document["embedding"]["pricing"]["text_input"]
    )
    assert row["cost_cny"] is not None and float(row["cost_cny"]) >= 0


def test_a_provider_failure_leaves_the_reservation_for_the_reconciler(
    app_db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """供应商挂了：证据行记 `failed`，**而账上那一行留在 `estimated`**。

    两个状态词长得像，意思完全不同：
    **证据行的 `failed` 说的是「这一轮没检到」**（裁定六要它可数）；
    **账上的 `failed` 会断言「供应商没向我们计费」** —— 一次 502 完全可能已经被计费，
    而台账里没有供应商侧标识可查。所以账上那一行按既有那条路走：
    `reconcile_abandoned` 到期把它转 `unreconciled`，与「调了但没回来」同形。

    本批第一版在这里写了 `failed`，被
    `test_nothing_in_production_ever_marks_a_reservation_failed` 拦下。
    """

    _publish_one_chunk(app_db)
    conversation_id = _conversation(seeded_database)
    correlation_id = uuid.uuid4()
    context = CallContext(
        tenant_id=TENANT_A, conversation_id=conversation_id, correlation_id=correlation_id
    )

    result = _retriever(_FailingEmbedder()).run(context=context, query="运费规则")

    assert result.outcome == "failed"
    assert "ProviderHttpError" in (result.degradation or "")
    ledger = worker_db.execute(
        "SELECT status FROM model_invocation_costs WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchall()
    assert [row["status"] for row in ledger] == ["estimated"], (
        "账上那一行不许记 failed —— 那是在断言一件台账查不到的事（有没有被计费）"
    )
    evidence = worker_db.execute(
        "SELECT outcome, hits FROM retrieval_evidence"
        " WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchone()
    assert evidence is not None
    assert evidence["outcome"] == "failed"
    assert evidence["hits"] == []


def test_no_published_release_costs_nothing(
    worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """没有语料 → `empty`，**账上一行都没有**。

    顺序（先读 release、后调供应商）在这一条上是可量的：
    每一个还没导入语料的租户都不该为每一次检索付一次嵌入调用。
    """

    conversation_id = _conversation(seeded_database)
    correlation_id = uuid.uuid4()
    context = CallContext(
        tenant_id=TENANT_A, conversation_id=conversation_id, correlation_id=correlation_id
    )

    result = _retriever(_Embedder()).run(context=context, query="运费规则")

    assert result.outcome == "empty"
    ledger = worker_db.execute(
        "SELECT count(*) AS n FROM model_invocation_costs"
        " WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchone()
    assert ledger is not None and ledger["n"] == 0

def _abandon(
    governor: CostGovernor,
    *,
    binding: Any,
    conversation_id: uuid.UUID,
    count: int,
) -> None:
    """开 `count` 条**两小时前**的预留，让它们落进「abandoned」那一侧。

    走 `open_row`（生产路径）而不是手写 INSERT：`occurred_at` 是它的参数，
    而「超过 `RECONCILE_AFTER_SECONDS`（900 秒）还在 `estimated`」正是那道计数闸的判据。
    """

    stale = datetime.now(UTC) - timedelta(hours=2)
    for _ in range(count):
        governor.open_row(
            binding=binding,
            context=CallContext(
                tenant_id=TENANT_A,
                conversation_id=conversation_id,
                correlation_id=uuid.uuid4(),
            ),
            estimated_input=20,
            estimated_output=0,
            estimated_cost=Decimal("0.000014"),
            prompt_release=RETRIEVER_VERSION,
            occurred_at=stale,
        )


def test_a_failing_embedding_endpoint_does_not_choke_the_conversation(
    worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**嵌入端点一直失败，模型调用仍然过得去** —— 而同样多的模型积账照样被拒。

    这一条是处置独立审查 **P1-3** 留下的性质（用户 2026-09-12 裁定 (a)）。
    没有它的时候链条是这样的：供应商失败那条路不动账上那一行（`mark_failed` 不许写，
    见 §六.2），于是每个检索回合留一条 `estimated`；900 秒后它算 abandoned；
    而 `abandoned_conversation` **不带时间窗、只增不减**，上限就那么几笔 ——
    **同一条会话里超过那个数的检索回合之后，每一次 `reserve()` 都被
    `cost_unsettled_backlog` 拒掉，包括 understand / plan / generate**，
    于是 `_persist_no_send`，客户从此在这条会话里收不到任何回复。
    **一个加分项拖垮了必需品**，而裁定六恰恰保证这时会话是健康的。

    处置是把 `role = 'EMBEDDING'` 从**会话级**那一格里排掉
    （日窗那一格照数：有窗、上限 200、自限且仍可见）。
    **两个方向一起断言** —— 只断言前半句的话，「把整道闸拆了」也会绿。
    """

    from fastagent_v2.worker.cost import UNSETTLED_BACKLOG_REASON_CODE
    from fastagent_v2.worker.retrieval import embedding_cost_binding

    database = Database(Settings.from_env().worker_database_url)
    release = active_release()
    governor = CostGovernor(database=database, release=release)
    conversation_id = _conversation(seeded_database)
    plan_binding = release.role("PLAN_CAPABILITY")
    # **取生效值，不取模块默认值**：这道闸的默认值 2026-09-15 起是按 release 现算的
    # （`derived_unsettled_conversation_limit`），而这一条要量的是生产真正用的那个数。
    limit = governor._unsettled_conversation_limit

    _abandon(
        governor,
        binding=embedding_cost_binding(release),
        conversation_id=conversation_id,
        count=limit + 1,
    )

    allowed = governor.precheck(
        binding=plan_binding,
        context=CallContext(
            tenant_id=TENANT_A,
            conversation_id=conversation_id,
            correlation_id=uuid.uuid4(),
        ),
        estimated_cost=Decimal("0.01"),
    )
    assert allowed.reason_code != UNSETTLED_BACKLOG_REASON_CODE, (
        "嵌入的积账把会话掐死了 —— 这正是 P1-3"
    )
    assert not allowed.is_deny

    # **反面**：同样多的**模型**积账必须照样拒 —— 那道闸的推导是按模型调用做的，
    # 本批只改它的适用面，不是把它拆了。
    _abandon(
        governor,
        binding=plan_binding,
        conversation_id=conversation_id,
        count=limit + 1,
    )
    denied = governor.precheck(
        binding=plan_binding,
        context=CallContext(
            tenant_id=TENANT_A,
            conversation_id=conversation_id,
            correlation_id=uuid.uuid4(),
        ),
        estimated_cost=Decimal("0.01"),
    )
    assert denied.is_deny
    assert denied.reason_code == UNSETTLED_BACKLOG_REASON_CODE
