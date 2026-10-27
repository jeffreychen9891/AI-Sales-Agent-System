"""知识库进生成之后，证据行那一半：**送了几条**，以及**生成之后那三列**。

纯规则那一半在 `tests/contracts/test_knowledge_bound.py`（额度）与
`tests/contracts/test_generation_receives_knowledge.py`（提示词与闸），
两份都不需要起栈。

**这一份为什么必须连真库** —— 三件事只有 PostgreSQL 说得准：

1. `061` 那条 `hits_sent` 的形状 CHECK（`0 ≤ hits_sent ≤ jsonb_array_length(hits)`）；
2. **列级 `UPDATE`**：worker 改得动裁定四那三列 + `hits_sent`，
   而 `outcome` / `hits` / `query_hash` 改不动 —— `has_table_privilege` 对列级
   返回 false，所以 `test_knowledge_grants.py` 那张摊开的表看不出这几格；
3. 那次 `UPDATE` 打在**同一行**上，而不是新插一行。

**第 3 条不是洁癖**：`050` 的表注释把「这一轮没请求检索」定成了一次**反连接**
（`retrieval_evidence` 里没有这个 `correlation_id` 的行）。
生成之后补插一行，等于把一个没检索过的回合算成检索过的 —— 那张反连接就坏了。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.retrieval import (
    RetrievalResult,
    query_hash,
    record,
    record_generation_side,
    retrieve,
)
from psycopg import Connection, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.conftest import TENANT_A

pytestmark = [pytest.mark.integration]

DIM = 2048


class _Embedder:
    """查询方向那个向量。与 `test_knowledge_retrieval.py` 的同名替身同源。"""

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        return Embedding(
            vector=tuple([1.0, 0.0] + [0.0] * (DIM - 2)),
            model_id="probe-embedding",
            dimension=DIM,
            prompt_tokens=7,
        )


@pytest.fixture()
def db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """建语料的那个连接：`app` 角色（只有它 INSERT 得了知识库）。"""

    with Connection.connect(Settings.from_env().database_url, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


@pytest.fixture()
def worker_db(seeded_database: dict[str, object]) -> Iterator[Connection]:
    """跑检索、写证据、以及**生成之后那次 UPDATE** 的角色：`worker`。

    走生产那条路 —— 用 app 连接写会绕过 `056` / `061` 的列级授权，
    那样验的就是一条谁都不会走的路径。
    """

    with Connection.connect(
        Settings.from_env().worker_database_url, row_factory=dict_row
    ) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        yield connection


def _database() -> Database:
    """`retrieve` 收 `Database`（那次 HTTP 不在事务里跑），用 worker 的 URL。"""

    return Database(Settings.from_env().worker_database_url)


def _corpus(connection: Connection, *, chunks: int = 2) -> uuid.UUID:
    """一版已发布的 release 加 `chunks` 条 chunk。

    形状与 `test_knowledge_retrieval.py::_release` 同源，**自由度仍然在第二维**：
    `<=>` 是余弦距离只看方向，把自由度放在第一维会让所有 chunk 距离全等
    （那一份的 `_tuple_vector` 记着这次踩坑）。
    """

    release_id = uuid.uuid4()
    connection.execute(
        "INSERT INTO knowledge_releases(tenant_id, id, version, status, embedding_model,"
        "  embedding_dimension, distance_metric, normalization, truncation_policy,"
        "  authoring_spec, content_hash)"
        " VALUES (%s, %s, 1, 'draft', 'probe-embedding', %s, 'cosine', 'l2',"
        "         'reject-over-limit', 'probe', %s)",
        (TENANT_A, release_id, DIM, f"{7:064x}"),
    )
    for ordinal in range(chunks):
        slug = f"kb-gen-{ordinal:03d}"
        body = f"{slug} 的正文：满 99 元包邮。"
        digest = query_hash(body)
        vector = "[" + ",".join(
            str(value) for value in [1.0, ordinal * 0.1] + [0.0] * (DIM - 2)
        ) + "]"
        connection.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, %s, 1, %s) ON CONFLICT DO NOTHING",
            (TENANT_A, slug, digest),
        )
        connection.execute(
            "INSERT INTO knowledge_chunks(tenant_id, id, release_id, document_ref,"
            "  document_version, ordinal, ref, slug, chunk_version, question, content_type,"
            "  confidence, body, text_hash, source_spans, embedding)"
            " VALUES (%s, %s, %s, 'docs/probe.md', '1', %s, %s, %s, 1, '探针问题',"
            "         '实测数据', '已实测', %s, %s, %s, %s)",
            (
                TENANT_A,
                uuid.uuid4(),
                release_id,
                ordinal,
                f"knowledge:{slug}:v1",
                slug,
                body,
                digest,
                f'[{{"file": "docs/probe.md", "section": "{slug}"}}]',
                vector,
            ),
        )
    connection.execute(
        "UPDATE knowledge_releases SET status = 'published', published_at = now()"
        " WHERE tenant_id = %s AND id = %s",
        (TENANT_A, release_id),
    )
    # 检索跑在 worker 那个连接上 —— 不提交它什么也看不见。
    connection.commit()
    return release_id


def _conversation(seeded: dict[str, object]) -> uuid.UUID:
    """一行会话，**用 worker 的连接建**（`app` 建不了 `conversation_aggregates`）。"""

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


def _row(connection: Connection, correlation_id: uuid.UUID) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM retrieval_evidence WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchone()
    assert row is not None, "证据行不在 —— 后面每一条断言都会读成别的意思"
    return dict(row)


# ---------------------------------------------------------------------------
# 一、`hits_sent`：送进提示词的那几条是可数的
# ---------------------------------------------------------------------------


def test_a_hit_records_how_many_chunks_reached_the_prompt(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """检出两条、都装得下 —— `hits_sent` 就是 2，而 `sent_chunks` 只有两个键。

    「都装得下」在今天是**结构上必然**的（见下一条），
    所以这一条同时是那件事的正面测量。
    """

    _corpus(db)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())

    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=(correlation_id := uuid.uuid4()),
        result=result,
    )

    assert result.sent == 2
    assert result.dropped == 0
    assert all(sorted(chunk) == ["ref", "text"] for chunk in result.sent_chunks)
    # **送出去的是语料里那段字，不是别的什么**：`hits` 那一份**没有正文**
    # （见 `retrieval._Candidate`），所以「正文从哪儿来」这条缝只有这里看得见 ——
    # 6c 第一版把 `hits` 直接交给了上界，门禁上 12 条红在同一个 `KeyError: 'text'` 上。
    assert {chunk["text"] for chunk in result.sent_chunks} == {
        "kb-gen-000 的正文：满 99 元包邮。",
        "kb-gen-001 的正文：满 99 元包邮。",
    }
    assert _row(worker_db, correlation_id)["hits_sent"] == 2


def test_the_evidence_row_does_not_copy_the_chunk_body(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """审计行里**没有正文**，而这是一条决定不是疏忽。

    6b 定这一列时抄进去的只有 `source_spans` —— 三跳追溯必须活过 chunk 被
    supersede，而 `055` 只禁写不删，**正文靠 `ref` + `text_hash` 一定回得去**。
    6c 要把正文送进提示词，于是它多读了一列 `body`；
    **顺手把它也写进 `hits` 会让每个检索回合往审计表里多抄最多 1.8KB 语料** ——
    那是一次悄悄扩大一个已经过审的产物。

    这一条钉的就是那条线：键集恰好五个，正文与它无关。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=(correlation_id := uuid.uuid4()),
        result=result,
    )

    stored = _row(worker_db, correlation_id)["hits"]

    assert [sorted(hit) for hit in stored] == [
        ["chunk_id", "ref", "score", "slug", "source_spans"]
    ]
    assert "body" not in stored[0] and "text" not in stored[0]


def test_a_turn_that_never_reached_the_prompt_leaves_the_count_null(
    worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**没命中就写 `NULL`，不写 0。**

    `061` 的列注释：`NULL` = 「这一轮没走到生成那一步」，而 `0` 会读成
    「送进去了 0 条」—— 后者只有在 `hits` 非空时才是一个有意义的事实
    （命中了但额度装不下）。两件事共用一个值，运维就没法把它们分开数。
    """

    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="   ", embedder=_Embedder())

    assert result.outcome == "unusable_request"
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=(correlation_id := uuid.uuid4()),
        result=result,
    )

    assert _row(worker_db, correlation_id)["hits_sent"] is None


def test_a_count_larger_than_the_hits_is_refused_by_the_database(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """`061` 那条形状 CHECK：**送出去的不可能多于检出来的**。

    这一条**手工造一个不可能的结果**，因为生产今天造不出它：
    额度 1,826 覆盖契约允许的两条（1,822），所以 `dropped > 0` 在
    `TOP_K = 2` 之下是结构上到不了的（`test_knowledge_bound.py` 的
    `test_dropping_is_unreachable_while_top_2_and_the_db_cap_hold` 量的就是这件事）。
    **那条 CHECK 因此是一道绊索而不是日常路径** —— 而绊索也要有人验它真的绷着：
    `TOP_K` 哪天变 3，第三条就会被丢，届时这一列是唯一能把它数出来的地方。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    hit = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    impossible = RetrievalResult(
        outcome="hit",
        hits=hit.hits,
        release_id=hit.release_id,
        deadline_ms=hit.deadline_ms,
        elapsed_ms=hit.elapsed_ms,
        degradation=None,
        query_hash=hit.query_hash,
        # 一条命中，却声称送了两条。
        sent_chunks=(*hit.sent_chunks, {"ref": "knowledge:ghost:v1", "text": "编的"}),
    )

    with pytest.raises(errors.CheckViolation):
        record(
            worker_db,
            tenant_id=TENANT_A,
            conversation_id=conversation_id,
            correlation_id=uuid.uuid4(),
            result=impossible,
        )


# ---------------------------------------------------------------------------
# 二、生成之后那三列（裁定四）
# ---------------------------------------------------------------------------


def test_the_generation_side_columns_land_on_the_same_row(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """三列一起写，**打在检索那一刻建的那一行上**。

    `050` 的表注释把「这一轮没请求检索」定成一次**反连接**，
    所以这里同时数行数：补插一行会把一个没检索过的回合算成检索过的。
    """

    _corpus(db)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    record_generation_side(
        worker_db,
        tenant_id=TENANT_A,
        correlation_id=correlation_id,
        materials_available=True,
        quoted_side="material",
        retrieved_unused=2,
        cited_refs=(),
    )

    rows = worker_db.execute(
        "SELECT materials_available, quoted_side, retrieved_unused, hits_sent, outcome"
        "  FROM retrieval_evidence WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchall()
    assert len(rows) == 1, "一回合一行 —— 生成之后补插一行会把那张反连接算坏"
    assert rows[0]["materials_available"] is True
    assert rows[0]["quoted_side"] == "material"
    assert rows[0]["retrieved_unused"] == 2
    assert rows[0]["hits_sent"] == 2, "这次 UPDATE 不该动检索那一刻写下的事实"
    assert rows[0]["outcome"] == "hit"


def test_the_update_touches_nothing_when_this_turn_has_no_evidence_row(
    worker_db: Connection
) -> None:
    """没有证据行的回合：这次 `UPDATE` **改 0 行，而且不建行**。

    管线那一侧已经用 `if retrieval is not None` 挡住了这条路
    （`pipeline._record_generation_side` 的调用点），
    **而这一条钉的是就算挡漏了也不会无声地制造一行** ——
    那一行会带着一个空的 `outcome`…… 其实连插都插不进去（`outcome` NOT NULL），
    所以真正的风险是 `UPDATE` 被人改成 `INSERT … ON CONFLICT`。这条用例看着那件事。
    """

    stranger = uuid.uuid4()

    record_generation_side(
        worker_db,
        tenant_id=TENANT_A,
        correlation_id=stranger,
        materials_available=False,
        quoted_side="neither",
        retrieved_unused=0,
        cited_refs=(),
    )

    count = worker_db.execute(
        "SELECT count(*) AS n FROM retrieval_evidence"
        " WHERE tenant_id = %s AND correlation_id = %s",
        (TENANT_A, stranger),
    ).fetchone()
    assert count is not None and count["n"] == 0


def test_a_row_may_not_carry_two_of_the_three_columns(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """`050` 那条「三列同生同灭」的 CHECK 还绷着 —— 而现在 worker 有 `UPDATE` 了。

    **这一条是新授权带来的新风险的测量**：`061` 之前 worker 碰不到这三列，
    所以那条 CHECK 没有任何实际的写者可以撞它；现在有了。
    两列有值一列空是一次**部分写入**，
    而**一次部分写入在这张表里读起来像一次真实测量**。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    with pytest.raises(errors.CheckViolation):
        worker_db.execute(
            "UPDATE retrieval_evidence SET materials_available = true, quoted_side = 'material'"
            " WHERE tenant_id = %s AND correlation_id = %s",
            (TENANT_A, correlation_id),
        )


# ---------------------------------------------------------------------------
# 三、授权那一面，按列问
# ---------------------------------------------------------------------------


def test_the_worker_may_update_the_generation_side_columns_and_nothing_else(
    db: Connection,
) -> None:
    """`061` 发的是**列级** `UPDATE`，而这一条把那几格逐个问出来。

    `has_table_privilege` 对列级授权返回 false，所以
    `test_knowledge_grants.py` 那张摊开的表看不出这件事。
    """

    for column in ("materials_available", "quoted_side", "retrieved_unused", "hits_sent"):
        assert db.execute(
            "SELECT has_column_privilege('fastagent_worker', 'retrieval_evidence', %s, 'UPDATE')",
            (column,),
        ).fetchone()["has_column_privilege"], f"6c 要在生成之后写 {column}"

    for column in ("outcome", "hits", "query_hash", "release_id", "elapsed_ms", "degradation"):
        assert not db.execute(
            "SELECT has_column_privilege('fastagent_worker', 'retrieval_evidence', %s, 'UPDATE')",
            (column,),
        ).fetchone()["has_column_privilege"], (
            f"{column} 是检索当时的事实 —— **事后改它等于改一条已经发生过的记录**"
        )


def test_the_refusal_is_the_database_s_not_the_code_s(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**执行在库里，不在代码里。**

    上一条问的是目录里的权限位；这一条真的发一条 `UPDATE outcome` ——
    一道只写在 `GRANT` 里而没人撞过的边界，与一道写在注释里的边界没有区别。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    with pytest.raises(errors.InsufficientPrivilege):
        worker_db.execute(
            "UPDATE retrieval_evidence SET outcome = 'empty'"
            " WHERE tenant_id = %s AND correlation_id = %s",
            (TENANT_A, correlation_id),
        )


# ---------------------------------------------------------------------------
# 三、三跳追溯的第一跳（`062`）
# ---------------------------------------------------------------------------


def test_the_first_hop_resolves_from_the_row_to_the_source_passage(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """**出口判据 §6.1 第 4 条**：从库里那一行走完「回复 → chunk → 原文哪一段」。

    这一条不是断言一个字段等于一个值，**它跑那次追溯本身** ——
    从 `cited_refs` 里那个 ref 出发，回到 `knowledge_chunks`，读出 `source_spans`。

    **为什么在 `062` 之前这条路走不通**：`retrieved_unused` 是一个数，
    top-2 的回合引了一条时它记「1 条没用」，**而没有记哪一条被用了**。
    `050` 的 `hits` 列注释写的是「a citation from last week must still resolve
    to the passage that was actually quoted」—— 缺的正是 *which*。

    **两条路都验**：库里那份 `hits` 的 span 拷贝（活过 supersede 的那一份），
    以及 join 回 `knowledge_chunks` 的那一份。两者今天必须一致 ——
    而它们分家的那一天（chunk 被 supersede、正文改了），**拷贝那一份才是对的**。
    """

    _corpus(db, chunks=2)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )
    quoted = str(result.sent_chunks[0]["ref"])

    record_generation_side(
        worker_db,
        tenant_id=TENANT_A,
        correlation_id=correlation_id,
        materials_available=False,
        quoted_side="knowledge",
        retrieved_unused=1,
        cited_refs=[quoted],
    )

    hop = worker_db.execute(
        "SELECT evidence.cited_refs ->> 0 AS cited_ref,"
        "       chunk.ref            AS chunk_ref,"
        "       chunk.source_spans   AS spans_from_the_corpus,"
        "       (SELECT hit -> 'source_spans'"
        "          FROM jsonb_array_elements(evidence.hits) AS hit"
        "         WHERE hit ->> 'ref' = evidence.cited_refs ->> 0) AS spans_from_the_row"
        "  FROM retrieval_evidence AS evidence"
        "  JOIN knowledge_chunks   AS chunk"
        "    ON chunk.tenant_id = evidence.tenant_id"
        "   AND chunk.ref = evidence.cited_refs ->> 0"
        " WHERE evidence.tenant_id = %s AND evidence.correlation_id = %s",
        (TENANT_A, correlation_id),
    ).fetchone()

    assert hop is not None, "第一跳断了 —— 引用集里那个 ref 在语料里找不到对应的 chunk"
    assert hop["cited_ref"] == quoted == hop["chunk_ref"]
    assert hop["spans_from_the_corpus"] == [
        {"file": "docs/probe.md", "section": quoted.split(":")[1]}
    ]
    assert hop["spans_from_the_row"] == hop["spans_from_the_corpus"], (
        "抄进证据行的那份 span 与语料里那份今天必须一致；"
        "它们分家的那天（chunk 被 supersede）该信的是拷贝那一份"
    )


def test_a_knowledge_verdict_with_an_empty_citation_set_is_refused(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """`quoted_side = 'knowledge'` 而引用集为空 —— **库拒**（`062`）。

    那样的一行说「这一轮用的是知识库」，同时说「一条都没引」。
    两句话不可能同时真，而它读起来像一次真实测量 ——
    与 `050` 那四条同族 CHECK 是同一条判据。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()
    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    with pytest.raises(errors.CheckViolation):
        record_generation_side(
            worker_db,
            tenant_id=TENANT_A,
            correlation_id=correlation_id,
            materials_available=False,
            quoted_side="knowledge",
            retrieved_unused=1,
            cited_refs=(),
        )


def test_the_citation_set_arrives_with_the_verdict_not_before_it(
    db: Connection, worker_db: Connection, seeded_database: dict[str, object]
) -> None:
    """检索那一刻写下的行里，引用集是 `NULL` —— 与裁定四那三列同生同灭。

    **`NULL` 与 `[]` 在这一列上是两件事**：前者「这一轮没走到生成」，
    后者「走到了生成，一条都没引」。共用一个值，
    「检索花了钱没落地」这件事就数不出来了。
    """

    _corpus(db, chunks=1)
    conversation_id = _conversation(seeded_database)
    result = retrieve(_database(), tenant_id=TENANT_A, query="运费规则", embedder=_Embedder())
    correlation_id = uuid.uuid4()

    record(
        worker_db,
        tenant_id=TENANT_A,
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        result=result,
    )

    row = _row(worker_db, correlation_id)
    assert row["cited_refs"] is None
    assert row["quoted_side"] is None

    with pytest.raises(errors.CheckViolation):
        # 只写引用集、不写那三列 —— 同生同灭那条 CHECK 接住它。
        worker_db.execute(
            "UPDATE retrieval_evidence SET cited_refs = %s"
            " WHERE tenant_id = %s AND correlation_id = %s",
            (Jsonb([]), TENANT_A, correlation_id),
        )
