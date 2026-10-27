"""检索层碰到真数据库的那一半：命中、排序、五态、列级授权、RLS。

**裁定三**把检索定成管线第四步，**裁定八**定 top-2，**裁定六**定失败不停回复。
纯规则那一半（不可用请求、`timeout` 与 `failed` 的分界、常量）在
`tests/contracts/test_retrieval_layer.py`，那一份不需要起栈。

**这一份为什么必须连真库**：它钉的四件事全都只有 PostgreSQL 说得准 ——
`<=>` 的排序、`056` 新加的三条形状 CHECK、**列级** `INSERT` 授权
（`has_table_privilege` 对列级返回 false，所以摊开那张表看不出来）、以及 RLS。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.retrieval import (
    RETRIEVER_VERSION,
    TOP_K,
    RetrievalResult,
    query_hash,
    record,
    retrieve,
)
from psycopg import Connection, errors, sql
from psycopg.rows import dict_row

from tests.conftest import TENANT_A, TENANT_B

pytestmark = [pytest.mark.integration]

DIM = 2048


class _Embedder:
    """返回**查询方向**的那个向量（第二维为 0），并数自己被调了几次。

    **不起 HTTP**：`retrieve` 收的是 `retrieval.Embedder` 协议，而那个协议只有一个方法。
    真实客户端的形状由 `tests/contracts/test_embedding_client.py` 钉（那一份连的是替身 transport）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        self.calls += 1
        return Embedding(
            vector=_tuple_vector(0.0),
            model_id="probe-embedding",
            dimension=DIM,
            prompt_tokens=7,
        )


class _EmbedderThatMustNotBeUsed:
    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        raise AssertionError("没有已发布的那一版时，这一次检索不该花钱")


def _tuple_vector(second: float) -> tuple[float, ...]:
    """`[1, second, 0, 0, …]` —— **第二维才是这一批夹具的自由度**。

    **第一版把自由度放在第一维、其余全 0，那是错的**：`<=>` 是**余弦**距离，
    只看方向，而 `[0.9, 0, …]` 与 `[0.1, 0, …]` **方向完全相同** ——
    五条 chunk 的距离于是全等于 0，`ORDER BY` 返回的是任意顺序。
    lite 门禁当场量到了这件事（`['kb-1-000', 'kb-1-002']`），
    **而两条 chunk 的那一版用例是「恰好绿」的** —— 它并没有在验排序。
    """

    return tuple([1.0, second] + [0.0] * (DIM - 2))


def _vector(second: float) -> str:
    return "[" + ",".join(str(value) for value in _tuple_vector(second)) + "]"


def _database() -> Database:
    """`retrieve` 现在收 `Database` 而不是连接（P3-2：那次 HTTP 不在事务里跑）。

    用 worker 的 URL —— 生产里跑检索的就是它，而**读语料的授权正是 `056` 发的那一条**。
    """

    return Database(Settings.from_env().worker_database_url)


def _insert(connection: Connection, table: str, row: dict[str, Any]) -> None:
    """与 `test_knowledge_publish.py` 同一个写法（那一份先有的）。"""

    connection.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(name) for name in row),
            sql.SQL(", ").join(sql.Placeholder() for _ in row),
        ),
        tuple(row.values()),
    )


@pytest.fixture()
def db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """**建语料的那一个连接：`app` 角色。**

    只有 app 有 INSERT 知识库的权限（`051` / `055`），worker 一条都没有 ——
    那是 `056` 的一条决定，不是夹具的不便。
    """

    with Connection.connect(Settings.from_env().database_url, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


@pytest.fixture()
def worker_db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """**跑检索的那一个连接：`worker` 角色**，也就是生产里跑它的那个角色。

    **第一版两件事都用 app 连接，而写证据那一步红了**
    （`permission denied for table retrieval_evidence`，lite 门禁量到的）。
    那条红是对的：`056` 把 `INSERT` 只发给了 worker，**列级地**。
    用例因此改成走生产那条路 —— 否则它验的是一条谁都不会走的路径。
    """

    with Connection.connect(
        Settings.from_env().worker_database_url, row_factory=dict_row
    ) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


def _release(
    connection: Connection,
    *,
    tenant_id: uuid.UUID = TENANT_A,
    version: int = 1,
    status: str = "published",
    vectors: tuple[float, ...] = (0.0, 1.0),
) -> uuid.UUID:
    """一版 release 加若干条 chunk，每条向量的**第二维**由 `vectors` 给。

    第二维越大，与查询方向（第二维 0）的夹角越大 —— 所以 `vectors` 升序写，
    就是「按相关性从高到低」写。
    """

    release_id = uuid.uuid4()
    _insert(
        connection,
        "knowledge_releases",
        {
            "tenant_id": tenant_id,
            "id": release_id,
            "version": version,
            "status": "draft",
            "embedding_model": "probe-embedding",
            "embedding_dimension": DIM,
            "distance_metric": "cosine",
            "normalization": "l2",
            "truncation_policy": "reject-over-limit",
            "authoring_spec": "probe",
            "content_hash": f"{tenant_id.int % 16:x}{version:063x}",
        },
    )
    for ordinal, second in enumerate(vectors):
        slug = f"kb-{version}-{ordinal:03d}"
        body = f"{slug} 的正文。"
        digest = query_hash(body)
        connection.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING",
            (tenant_id, slug, digest),
        )
        _insert(
            connection,
            "knowledge_chunks",
            {
                "tenant_id": tenant_id,
                "id": uuid.uuid4(),
                "release_id": release_id,
                "document_ref": "docs/probe.md",
                "document_version": "1",
                "ordinal": ordinal,
                "ref": f"knowledge:{slug}:v1",
                "slug": slug,
                "chunk_version": 1,
                "question": "探针问题",
                "content_type": "实测数据",
                "confidence": "已实测",
                "body": body,
                "text_hash": digest,
                "source_spans": json.dumps([{"file": "docs/probe.md", "section": slug}]),
                "embedding": _vector(second),
            },
        )
    if status != "draft":
        connection.execute(
            "UPDATE knowledge_releases SET status = %s, published_at = now()"
            " WHERE tenant_id = %s AND id = %s",
            (status, tenant_id, release_id),
        )
    # **提交**：检索跑在另一个连接（worker）上，不提交它什么也看不见。
    connection.commit()
    return release_id


def _conversation(seeded: dict[str, object]) -> uuid.UUID:
    """建一行会话，**用 worker 的连接**。

    `fastagent_app` 建不了：`permission denied for table conversation_aggregates`
    （lite 门禁量到的）。**那不是夹具的麻烦，是生产的事实** ——
    会话聚合由 worker 写，而 app 那一侧读。检索也跑在 worker 里，
    所以这一行本来就该由 worker 建。

    `connection` 那个参数留着不用会误导人，所以这里不收它。
    """

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


def test_a_hit_carries_what_the_three_hop_traceback_needs(
    db: Connection, worker_db: Connection
) -> None:
    """命中的形状：**chunk id、分数、以及抄进来的 `source_spans`**。

    `050` 的列注释说清了为什么 span 是**抄**的而不是 join 出来的：
    三跳追溯（回复 → chunk → 原文）必须活过 chunk 被 supersede ——
    上周引的那一句今天仍要指得回原文。
    """

    release_id = _release(db, vectors=(0.0, 1.0))
    embedder = _Embedder()

    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=embedder)

    assert result.outcome == "hit"
    assert result.release_id == release_id, "命中必须记读了哪一版（`050` 的 CHECK (a)）"
    assert embedder.calls == 1, "一次检索一次嵌入调用"
    assert len(result.hits) == 2
    top = result.hits[0]
    assert set(top) == {"chunk_id", "ref", "slug", "score", "source_spans"}
    assert top["slug"] == "kb-1-000", "离查询最近的那一条排在前面"
    assert top["score"] > result.hits[1]["score"]
    assert top["source_spans"] == [{"file": "docs/probe.md", "section": "kb-1-000"}]


def test_only_the_top_two_come_back(db: Connection, worker_db: Connection) -> None:
    """裁定八：**top-2**，而语料里有五条。"""

    # **五条，方向依次离查询更远** —— 第二维升序。
    _release(db, vectors=(0.0, 0.2, 0.4, 0.8, 1.6))

    result = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder()
    )

    assert result.outcome == "hit"
    assert len(result.hits) == TOP_K == 2
    assert [hit["slug"] for hit in result.hits] == ["kb-1-000", "kb-1-001"]


def test_a_draft_release_is_not_retrieved_from(
    db: Connection, worker_db: Connection
) -> None:
    """只读 `published` 那一版。

    **草稿里的 chunk 可能还没向量化、也还没被人过一遍**（`052` 的发布闸就是为此存在），
    检索读它等于把没审完的资料当成来源。
    """

    _release(db, status="draft", vectors=(0.0,))

    result = retrieve(
        _database(),
        tenant_id=TENANT_A,
        query="运费规则",
        embedder=_EmbedderThatMustNotBeUsed(),
    )

    assert result.outcome == "empty"
    assert result.release_id is None
    assert result.degradation == "no published knowledge release"


def test_no_corpus_means_no_provider_call(worker_db: Connection) -> None:
    """**没有已发布的那一版时，这一次检索不花钱** —— 顺序不是随手排的。

    `_EmbedderThatMustNotBeUsed` 是这条断言本身：先读 release、后调供应商，
    反过来写的话每一个还没导入语料的租户都会为每一次检索付一次嵌入调用。
    """

    result = retrieve(
        _database(),
        tenant_id=TENANT_A,
        query="运费规则",
        embedder=_EmbedderThatMustNotBeUsed(),
    )

    assert result.outcome == "empty"
    assert result.degradation == "no published knowledge release"


def test_another_tenant_s_corpus_is_invisible(
    worker_db: Connection, seeded_database: Any
) -> None:
    """RLS：B 有语料、A 没有，A 的会话检出来的是「没有已发布的那一版」。

    **不是「检出零条」** —— 连那一版 release 都看不见。两者的区别在
    `release_id`：看得见但检不到会带着 id，看不见则是 `None`。
    """

    with Connection.connect(
        Settings.from_env().database_url, row_factory=dict_row
    ) as other:
        other.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_B),))
        _release(other, tenant_id=TENANT_B, version=7, vectors=(0.0,))
        other.commit()

    result = retrieve(
        _database(),
        tenant_id=TENANT_A,
        query="运费规则",
        embedder=_EmbedderThatMustNotBeUsed(),
    )

    assert result.outcome == "empty"
    assert result.release_id is None


def test_empty_today_always_means_no_published_release(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**`empty` 今天只有一个意思，而判别式是 `release_id`**（独立审查 **P2-2**）。

    审查方指出 `empty` 装了两件事：「压根没有语料」与「检了没检到」，
    而那正是 `056` 自己刚裁过不许的形状。**量下来比那更干净**：
    「检了没检到」**今天不可达** —— `052` 的发布闸拒收「有 chunk 没向量化」
    与「一个 chunk 都没有」的 release，而 `_top_hits` 不设分数下界，
    所以一版 published release 必然至少返回一条。

    于是这条用例钉两件：
    * 无语料 → `empty` 且 `release_id IS NULL`；
    * 有语料 → **一定是 `hit`**（`empty` + 非空 `release_id` 在今天造不出来）。

    **哪天它变得可达**（有人给检索加分数下界，那是 6c 可能做的事），
    判别式已经在数据里：`release_id IS NULL` 是「没语料」，非空是「检了没检到」。
    这句话写进了 `056` 的列注释。
    """

    no_corpus = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_EmbedderThatMustNotBeUsed()
    )
    assert (no_corpus.outcome, no_corpus.release_id) == ("empty", None)

    _release(db, vectors=(0.0,))
    with_corpus = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder()
    )
    assert with_corpus.outcome == "hit", (
        "发布闸保证每条 chunk 都向量化过、且至少一条，而检索不设分数下界 —— "
        "所以有语料就必然有命中"
    )
    assert with_corpus.release_id is not None


def test_a_retrieval_that_outruns_its_budget_is_recorded_as_a_timeout(
    db: Connection, seeded_database: dict[str, object]
) -> None:
    """**总用时越界 → `timeout`**，哪怕供应商一点没慢（**P2-4** 的那一半）。

    这条用例是反向探针逼出来的：处置 P2-4 时我加了「扫描回来再判一次总用时」
    那个分支，**而它当时一条用例都没有** —— 把它删掉，六个变异里没有一个会红。
    期限给 1ms，于是无论嵌入还是那次扫描都必然越界：
    记的是 `timeout`（表上 CHECK (c) 要 `elapsed_ms >= deadline_ms`，正好成立），
    而不是 `hit`。

    **为什么这一条重要**：库那一侧慢下来与供应商慢下来，对客户是同一件事 ——
    这一回合的生成被推迟了。先前 `deadline_ms` 只传给 `embed_one`，
    于是 `deadline_ms=2000 / elapsed_ms=9000 / outcome='hit'` 是一行完全合法的记录。
    """

    _release(db, vectors=(0.0,))

    result = retrieve(
        _database(),
        tenant_id=TENANT_A,
        query="运费规则",
        embedder=_Embedder(),
        deadline_ms=1,
    )

    assert result.outcome == "timeout"
    assert result.elapsed_ms >= result.deadline_ms, "CHECK (c) 要的就是这个不等式"
    # 1ms 的预算下**语句本身会被 `statement_timeout` 取消**，而那也是超时 ——
    # `QueryCanceled` 不许穿出去变成 `failed`（那会说错话：`failed` 留给
    # 检索层或供应商出问题）。两条 degradation 任一都算对。
    assert "budget" in (result.degradation or "")
    assert result.hits == ()


def test_the_evidence_row_leaves_the_generation_side_columns_null(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """写下来的那一行：裁定四那三列**全空**，因为生成还没发生。

    `050` 的列注释把这件事写成了判据：三列**一起到**，
    而 `NULL` 的意思是「这一轮没走到生成」——**一个真实结局，不是漏写**。
    """

    release_id = _release(db, vectors=(0.0,))
    conversation_id = _conversation(seeded_database)
    result = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder()
    )
    correlation_id = uuid.uuid4()

    row_id = record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    row = worker_db.execute(
        "SELECT * FROM retrieval_evidence WHERE tenant_id = %s AND id = %s",
        (TENANT_A, row_id),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "hit"
    assert row["release_id"] == release_id
    assert row["retriever_version"] == RETRIEVER_VERSION
    assert row["query_hash"] == query_hash("运费规则")
    assert len(row["hits"]) == 1
    assert row["materials_available"] is None
    assert row["quoted_side"] is None
    assert row["retrieved_unused"] is None


def test_a_second_write_for_the_same_turn_keeps_the_first_row_and_does_not_raise(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**同一回合写第二次：不抛，库里仍然只有一行，而且是第一次那一行。**

    这一条是处置独立审查 **P1-1** 留下的性质，它取代了原来那条
    「第二次写会 `UniqueViolation`」—— 那条断言把一个**致命行为写成了规范**。

    `correlation_id` 取自信封，**重投时一模一样**：第一次跑到检索之后、回合收口之前
    出任何事（`AggregateConflict`、worker 挂掉、租约到期），事件被重新 claim →
    重跑 plan → 再写这一行 → `UniqueViolation` 一路穿过管线（`_retrieve_if_requested`
    没有 try），`MAX_INBOX_ATTEMPTS` 之后这条入站事件**终态失败，客户一条消息都收不到**。
    **那把裁定六反过来了**：那条裁定说检索失败不许停回合，而它是检索**成功**之后停掉整轮。

    「一回合只有一行」这件事仍然成立 —— 由 `ON CONFLICT … DO NOTHING` 保证，
    而不是由一个抛出去的异常保证。**留第一次那行**：它记的是真的发生过的那一次。
    """

    _release(db, vectors=(0.0,))
    conversation_id = _conversation(seeded_database)
    correlation_id = uuid.uuid4()
    first = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder()
    )
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=first,
    )
    worker_db.commit()

    # 第二次带一个**不同**的结果，好证明留下来的是第一次那一行而不是后来那一行。
    second = RetrievalResult(
        outcome="unusable_request",
        hits=(),
        release_id=None,
        deadline_ms=first.deadline_ms,
        elapsed_ms=first.elapsed_ms + 7,
        degradation="blank query string",
        query_hash=first.query_hash,
    )
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=second,
    )

    rows = worker_db.execute(
        "SELECT outcome, elapsed_ms FROM retrieval_evidence"
        " WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchall()
    assert len(rows) == 1, "一回合仍然只有一行"
    assert rows[0]["outcome"] == "hit", "留的是第一次那一行 —— 它记的是真发生过的那次"
    assert rows[0]["elapsed_ms"] == first.elapsed_ms


def test_an_unusable_request_row_is_accepted(
    worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """`056` 新加的那一态能写进去 —— 而它的三条形状同时成立。"""

    conversation_id = _conversation(seeded_database)
    result = retrieve(
        _database(), tenant_id=TENANT_A, query="   ", embedder=_EmbedderThatMustNotBeUsed()
    )
    assert result.outcome == "unusable_request"

    row_id = record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=uuid.uuid4(),
        result=result,
    )

    row = worker_db.execute(
        "SELECT outcome, degradation, release_id, hits FROM retrieval_evidence"
        " WHERE tenant_id = %s AND id = %s",
        (TENANT_A, row_id),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "unusable_request"
    assert row["degradation"] == "blank query string"
    assert row["release_id"] is None
    assert row["hits"] == []


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("degradation", None, "「为什么不可用」是这一态唯一的信息量"),
        ("release_id", "self", "没去检就不许记读过哪一版"),
    ],
)
def test_a_malformed_unusable_request_row_is_refused(
    db: Connection,
    worker_db: Connection,
    seeded_database: dict[str, object],
    field: str,
    value: Any,
    why: str,
) -> None:
    """**`056` 那条 CHECK 的反面，逐项试。**

    没有这两条，`unusable_request` 会变成一个「什么都能装」的值 ——
    而它存在的理由恰恰是让一种情况可数。`hits` 非空那一种由下一条单独验
    （它要先造出一个带命中的结果）。
    """

    release_id = _release(db, vectors=(0.0,))
    conversation_id = _conversation(seeded_database)
    result = retrieve(
        _database(), tenant_id=TENANT_A, query="  ", embedder=_EmbedderThatMustNotBeUsed()
    )
    broken = RetrievalResult(
        outcome="unusable_request",
        hits=result.hits,
        release_id=release_id if value == "self" else result.release_id,
        deadline_ms=result.deadline_ms,
        elapsed_ms=result.elapsed_ms,
        degradation=value if field == "degradation" else result.degradation,
        query_hash=result.query_hash,
    )

    with pytest.raises(errors.CheckViolation):
        record(
            worker_db,
            tenant_id=TENANT_A,
            conversation_id=conversation_id,
            correlation_id=uuid.uuid4(),
            result=broken,
        )


def test_an_unusable_request_may_not_carry_hits(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """第三条形状：没去检，不可能有命中。"""

    _release(db, vectors=(0.0,))
    conversation_id = _conversation(seeded_database)
    hit = retrieve(
        _database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder()
    )
    broken = RetrievalResult(
        outcome="unusable_request",
        hits=hit.hits,
        release_id=None,
        deadline_ms=hit.deadline_ms,
        elapsed_ms=hit.elapsed_ms,
        degradation="blank query string",
        query_hash=hit.query_hash,
    )

    with pytest.raises(errors.CheckViolation):
        record(
            worker_db,
            tenant_id=TENANT_A,
            conversation_id=conversation_id,
            correlation_id=uuid.uuid4(),
            result=broken,
        )


def test_the_worker_can_read_the_corpus_and_write_the_audit_row_but_not_the_verdict(
    db: Connection,
) -> None:
    """**授权那一面，按列问。**

    `has_table_privilege` 对**列级**授权返回 false，所以
    `test_knowledge_grants.py` 那张摊开的表看不出「worker 能 INSERT 哪几列」。
    这一条问的就是那几格：读得到语料、写得下审计行，
    **而裁定四那三列在 `INSERT` 那一刻碰不到** —— 它们在生成之后才有值。

    **6c 之后 worker 对这三列有了列级 `UPDATE`**（`061`），
    而那不动这里的任何一条断言：本条问的是 `INSERT`，
    「检索那一刻不许填」与「生成之后才许填」正是同一条裁定的两面。
    `UPDATE` 那一侧由 `tests/integration/test_knowledge_in_generation.py` 问。
    """

    for table in ("knowledge_releases", "knowledge_chunks"):
        assert db.execute(
            "SELECT has_table_privilege('fastagent_worker', %s, 'SELECT')", (table,)
        ).fetchone()["has_table_privilege"], f"worker 读不到 {table}，检索跑不起来"
        assert not db.execute(
            "SELECT has_any_column_privilege('fastagent_worker', %s, 'UPDATE')", (table,)
        ).fetchone()["has_any_column_privilege"], f"worker 不该改得动 {table} 的任何一列"

    for column in ("outcome", "hits", "query_hash", "degradation", "release_id"):
        assert db.execute(
            "SELECT has_column_privilege('fastagent_worker', 'retrieval_evidence', %s, 'INSERT')",
            (column,),
        ).fetchone()["has_column_privilege"], f"检索层要写 {column}"

    for column in ("materials_available", "quoted_side", "retrieved_unused"):
        assert not db.execute(
            "SELECT has_column_privilege('fastagent_worker', 'retrieval_evidence', %s, 'INSERT')",
            (column,),
        ).fetchone()["has_column_privilege"], (
            f"{column} 是生成之后才知道的（裁定四），"
            "6b 的写者有能力在检索那一刻填它，就等于能写一条读起来像真实测量的假行"
        )
