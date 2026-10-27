"""知识库这一格的**出口判据**，跑成用例而不是写成一句承诺。

判据出自 `docs/plans/knowledge-base-schedule-2026-09-11.md` §六，三条：

* **6.1** 客户问一个素材未覆盖的问题 → 检索到资料 → 引用作答 → **引用可追溯**
  → `test_the_whole_chain_from_a_question_to_a_traceable_citation`
* **6.2** 知识库也没有的问题 → **说不知道，不许编**
  → `test_a_citation_the_corpus_cannot_back_stops_the_turn`
* **6.3** 知识库开着时字节那道闸仍算得出固定底、在架的包封得了版
  → `tests/contracts/test_knowledge_bound.py` 的预算表那条 +
  `test_prompt_bytes.py` 的基线那条（**不在本文件**：它们不需要起栈）

**为什么这一份必须连真栈**：前两条判据问的是**一条穿过整个管线的链路** ——
入站 → 理解 → 计划（请求检索）→ 检索（真向量、真库）→ 生成（命中进提示词）→
校验（引用闸）→ 出站，再从库里把那条追溯走回去。
每一段单独都有用例看着，而**把它们接在一起是另一件事**：
6c 第一轮门禁那 12 条红就是两段各自绿、接起来炸（`hits` 里没有正文）。

**替身只在两个供应商缝上**：嵌入与模型。其余每一段都是生产那一份代码。
"""

from __future__ import annotations

import json
import os
import uuid
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.pipeline import InboxProcessor
from fastagent_v2.worker.retrieval import KnowledgeRetriever, query_hash
from psycopg import Connection, OperationalError
from psycopg.rows import dict_row

from tests.conftest import TENANT_A
from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

DIM = 2048
SHIPPING_SLUG = "shipping-policy"
SHIPPING_REF = f"knowledge:{SHIPPING_SLUG}:v1"
SHIPPING_BODY = "单笔实付满 99 元包邮，不满 99 元运费 8 元。"
SHIPPING_SPANS = [{"file": "docs/knowledge/shipping.md", "section": "运费与包邮"}]
#: 模型自己写的那句话 —— **不是 chunk 的正文**（ADR-0135：知识库用自己的话说）。
REPLY = "满 99 元就包邮啦，没满的话运费 8 元哦～"


class _CountingEmbedder:
    """数自己被调了几次的嵌入替身。

    **它同时是「静默回合不该花钱」那一条的量具**：那一条断言的不是
    「没有证据行」（那可以有很多原因），而是**这一次嵌入压根没发生**。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        self.calls.append(text)
        return Embedding(
            vector=tuple([1.0, 0.0] + [0.0] * (DIM - 2)),
            model_id="probe-embedding",
            dimension=DIM,
            prompt_tokens=9,
        )


class _KnowledgeCitingInference(DeterministicMockInference):
    """计划步请求检索，生成步**读平台交给它的那几条**并引其 ref。

    **它读 `knowledge`，而基类刻意不读** —— 这就是这一份用例的要点：
    命中有没有真的穿过 `generate` 那个缝到模型面前，
    只有一个**会读它**的替身证明得了。基类那个双身如果开始编造引用，
    全仓两千多条用例都会看起来像是在证明这件事，而它们不是。
    """

    def __init__(self) -> None:
        super().__init__()
        self.knowledge_seen: list[dict[str, str]] = []

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["knowledge_query"] = "运费规则"
        return document

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        offered = dict(kwargs.get("knowledge") or {})
        self.knowledge_seen.append(dict(offered))
        document = super().generate(**kwargs)
        document["customer_visible_segments"] = [{"type": "text", "content_or_ref": REPLY}]
        for proposal in document["proposed_intents"]:
            if proposal["kind"] == "send_message":
                proposal["typed_payload"] = {"content": {"type": "text", "text": REPLY}}
        # **只引它真的用了的那一条** —— 而不是把收到的每个 ref 都堆进去。
        # 那个区别正是 `retrieved_unused` 与 `cited_refs` 要记的东西：
        # 平台送了两条，模型用了一条。
        used = [ref for ref in sorted(offered) if ref == SHIPPING_REF]
        document["claims"] = [
            {
                "text_hash": sha256(REPLY.encode("utf-8")).hexdigest(),
                "source_evidence_refs": used,
            }
        ]
        return document


class _FabricatingInference(_KnowledgeCitingInference):
    """引一条**这一轮没给过**的知识库 ref —— 6.2 的反面。"""

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        document = super().generate(**kwargs)
        document["claims"] = [
            {
                "text_hash": sha256(REPLY.encode("utf-8")).hexdigest(),
                "source_evidence_refs": ["knowledge:invented-policy:v9"],
            }
        ]
        return document


class _SilentInference(_KnowledgeCitingInference):
    """请求了检索，而策略是静默的（`wait`）。

    **两件事同时成立才是这一条要测的局面**：计划步写下了 `knowledge_query`
    （模型确实想查），而这一回合不会发消息。
    """

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["selected_strategy_id"] = "wait"
        document["candidate_intents"] = []
        document["generation_constraints"] = []
        return document


def _publish_corpus(settings: Settings, *, chunks: int = 1) -> UUID:
    """一版已发布的语料，用 **app** 角色写（只有它 INSERT 得了知识库）。"""

    release_id = uuid4()
    with Connection.connect(settings.database_url, row_factory=dict_row) as connection:
        connection.execute("SELECT set_config('app.tenant_id', %s, false)", (str(TENANT_A),))
        connection.execute(
            "INSERT INTO knowledge_releases(tenant_id, id, version, status, embedding_model,"
            "  embedding_dimension, distance_metric, normalization, truncation_policy,"
            "  authoring_spec, content_hash)"
            " VALUES (%s, %s, 1, 'draft', 'probe-embedding', %s, 'cosine', 'l2',"
            "         'reject-over-limit', 'exit-criteria', %s)",
            (TENANT_A, release_id, DIM, f"{11:064x}"),
        )
        for ordinal in range(chunks):
            slug = SHIPPING_SLUG if ordinal == 0 else f"filler-{ordinal:03d}"
            body = SHIPPING_BODY if ordinal == 0 else f"{slug} 的正文。"
            spans = SHIPPING_SPANS if ordinal == 0 else [{"file": "docs/x.md", "section": slug}]
            digest = query_hash(body)
            vector = "[" + ",".join(
                str(value) for value in [1.0, ordinal * 0.2] + [0.0] * (DIM - 2)
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
                " VALUES (%s, %s, %s, %s, '1', %s, %s, %s, 1, %s, '实测数据', '已实测',"
                "         %s, %s, %s, %s)",
                (
                    TENANT_A,
                    uuid4(),
                    release_id,
                    spans[0]["file"],
                    ordinal,
                    f"knowledge:{slug}:v1",
                    slug,
                    "运费怎么算？" if ordinal == 0 else "填充问题",
                    body,
                    digest,
                    json.dumps(spans, ensure_ascii=False),
                    vector,
                ),
            )
        connection.execute(
            "UPDATE knowledge_releases SET status = 'published', published_at = now()"
            " WHERE tenant_id = %s AND id = %s",
            (TENANT_A, release_id),
        )
        connection.commit()
    return release_id


def _accept_customer_message(
    *, settings: Settings, seeded_database: dict[str, object], conversation_id: UUID
) -> None:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"kb-exit-{uuid4()}", sender_id=sender_of(conversation_id)
        ),
    )
    assert accepted.accepted is True


def _drive(*, settings: Settings, inference: Any, embedder: _CountingEmbedder) -> Any:
    """一次真实回合，**检索器按生产那样装配**（`cli.run_worker` 的那一份）。"""

    database = Database(settings.worker_database_url)
    processor = InboxProcessor(
        database=database,
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="kb-exit-worker",
        retriever=KnowledgeRetriever(
            database=database,
            embedder=embedder,
            release=active_release(),
        ),
    )
    return processor.process_one(block_ms=500)


def _evidence(settings: Settings, conversation_id: UUID) -> list[dict[str, Any]]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM retrieval_evidence WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchall()
        ]


def _outbox(settings: Settings, conversation_id: UUID) -> list[dict[str, Any]]:
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT command, status FROM outbox_messages WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# 6.1 主判据
# ---------------------------------------------------------------------------


def test_the_whole_chain_from_a_question_to_a_traceable_citation(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**出口判据 6.1，一条链路走完，四件事逐个断言。**

    1. 客户消息进来，素材层这一轮给不出东西（这个架包没有 `materials.json`）；
    2. 检索层给出命中并落一行 `retrieval_evidence`，带 query hash 与 release；
    3. 回复**引用了其中一条**，走的是 **ref**：客户看到的是模型自己的话
       （ADR-0135：知识库不逐字引），而那句话的依据记在 `cited_refs` 里；
    4. **三跳都真能反查**：`cited_refs` → `knowledge_chunks` → `source_spans`。

    **第 3 件的读法由 ADR-0135 定**（用户 2026-09-12）。§6.1 原文写的是
    「走的是 ref 而不是模型复述」，而 ADR-0135 之后知识库**永远是模型复述 + 引 ref**：
    逐字引只属于素材（平台在发送路径上把 `material_ref` 换成商家审过的字）。
    所以这一条断言的是**引用走 ref**，不是「正文逐字出现在回复里」——
    计划那一行已加修订标注指向 ADR-0135。
    """

    release_id = _publish_corpus(settings, chunks=2)
    conversation_id = new_conversation(TENANT_A)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inference = _KnowledgeCitingInference()
    embedder = _CountingEmbedder()

    outcome = _drive(settings=settings, inference=inference, embedder=embedder)

    assert outcome is not None, "回合没跑起来 —— 后面每一条断言都会读成别的意思"
    # ① 素材那一侧这一轮确实是空的：否则 3 就不是「知识库答的」。
    assert inference.knowledge_seen, "generate 压根没被调到"
    # ② 命中真的到了模型面前，而且只有 {ref: text}。
    assert inference.knowledge_seen[0] == {
        SHIPPING_REF: SHIPPING_BODY,
        "knowledge:filler-001:v1": "filler-001 的正文。",
    }, "top-2 两条都该送进去（额度覆盖契约允许的两条）"
    assert embedder.calls == ["运费规则"], "一个回合一次嵌入，查询串原样"

    rows = _evidence(settings, conversation_id)
    assert len(rows) == 1, "一回合一行（`050` 的唯一约束）"
    row = rows[0]
    assert row["outcome"] == "hit"
    assert row["release_id"] == release_id, "记下当时读的是哪一版（三跳的中间那一跳）"
    assert row["query_hash"] == query_hash("运费规则")
    assert row["hits_sent"] == 2, "两条都装得下（额度覆盖契约允许的 top-2）"
    # ③ 引用走 ref，客户看到的是模型自己的话。
    assert row["quoted_side"] == "knowledge"
    assert row["materials_available"] is False
    assert row["cited_refs"] == [SHIPPING_REF], "引了哪一条 —— 三跳的第一跳（`062`）"
    assert row["retrieved_unused"] == 1, (
        "送了两条、模型用了一条：这个数与 `cited_refs` 是互补的两半，"
        "**而在 `062` 之前库里只有这个数**"
    )

    sent = _outbox(settings, conversation_id)
    assert len(sent) == 1, "客户这一轮收到了一条消息"
    assert sent[0]["command"]["content"]["text"] == REPLY
    assert SHIPPING_BODY not in json.dumps(sent[0]["command"], ensure_ascii=False), (
        "知识库不逐字进客户可见的字（ADR-0135）—— 逐字引是素材那一侧的事"
    )

    # ④ 三跳反查，从库里走。
    with Database(settings.worker_database_url).transaction(tenant_id=TENANT_A) as connection:
        hop = connection.execute(
            "SELECT chunk.ref, chunk.body, chunk.source_spans"
            "  FROM retrieval_evidence AS evidence"
            "  JOIN knowledge_chunks   AS chunk"
            "    ON chunk.tenant_id = evidence.tenant_id"
            "   AND chunk.ref = evidence.cited_refs ->> 0"
            " WHERE evidence.tenant_id = %s AND evidence.conversation_id = %s",
            (TENANT_A, conversation_id),
        ).fetchone()
    assert hop is not None, "第一跳断了：引用集里那个 ref 在语料里找不回来"
    assert hop["ref"] == SHIPPING_REF
    assert hop["body"] == SHIPPING_BODY, "第一跳落在了错的 chunk 上"
    assert hop["source_spans"] == SHIPPING_SPANS, (
        "第二跳断了：chunk 记不下它出自原文哪一段（§4.2 那条「记的形态可以变，"
        "记不记不能变」）"
    )


# ---------------------------------------------------------------------------
# 6.2 反向判据
# ---------------------------------------------------------------------------


def test_a_citation_the_corpus_cannot_back_stops_the_turn(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**出口判据 6.2 的执行面**：编一条出处，这一轮就发不出去。

    判据原话是「客户问一个知识库也没有的问题，系统说不知道，而不是编一句」。
    「说不知道」那一半由 `evidence_required` 管（它有自己的用例）；
    **这一条管的是「编一句」那一半，而它比前者硬**：
    模型引了一条**这一轮没给过**的 ref → `GENERATION_KNOWLEDGE_REF_UNKNOWN`
    → 这一轮没有出站消息。

    **为什么不是 drop 掉那条引用继续发**：一条引了不存在来源的事实声明
    **读起来像有据可查**，而客户与商家都没有办法从那句话里看出它是编的。

    这一轮的**审计行照样落**（裁定六的另一半）：检索成功了，
    是生成被拒 —— 所以 `outcome = 'hit'`，而裁定四那四列是 `NULL`
    （「这一轮没走到生成」的那个含义）。
    """

    _publish_corpus(settings)
    conversation_id = new_conversation(TENANT_A)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    _drive(settings=settings, inference=_FabricatingInference(), embedder=_CountingEmbedder())

    assert _outbox(settings, conversation_id) == [], "编出来的出处不许发出去"
    rows = _evidence(settings, conversation_id)
    assert len(rows) == 1 and rows[0]["outcome"] == "hit", "检索成功过，审计行照落"
    assert rows[0]["quoted_side"] is None, "生成被拒 —— 裁定四那几列的 NULL 就是这个意思"
    assert rows[0]["cited_refs"] is None
    assert rows[0]["hits_sent"] == 1, "平台确实送了一条进提示词，这一列记的是那件事"


# ---------------------------------------------------------------------------
# 顺带：一条登记了两次的观察，到此关掉
# ---------------------------------------------------------------------------


def test_a_silent_strategy_turn_never_pays_for_an_embedding(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**静默回合不花钱检索** —— T306 / T315 尾注那条观察的量法。

    两处登记都写着「6b 零调用方所以不是缺陷，**接线那一刻它就是一次白花的检索**
    （还要付一次嵌入的钱）—— 那一批一并判」。**判出来的结果是：它已经成立**，
    因为管线那条「静默就收口」的 `return` 排在检索那一句**之前**。

    **恰好成立不等于被守着**，所以这一条钉住它：断言的是**嵌入压根没发生**
    （`embedder.calls == []`），而不只是「没有证据行」——
    后者可以因为很多别的原因成立（没接检索器、计划没请求），
    **而那正是这条观察一开始被漏掉的原因**。
    """

    _publish_corpus(settings)
    conversation_id = new_conversation(TENANT_A)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    embedder = _CountingEmbedder()

    _drive(settings=settings, inference=_SilentInference(), embedder=embedder)

    assert embedder.calls == [], "静默回合为一条没人会读的命中付了一次嵌入的钱"
    assert _evidence(settings, conversation_id) == [], "没检索就没有证据行（反连接照旧成立）"
    assert _outbox(settings, conversation_id) == [], "静默策略本来就不发消息"


def test_the_environment_this_file_needs_is_the_gate_s(
    settings: Settings,
) -> None:
    """本文件读三个角色的 URL，而它们由门禁那一套 env 提供。

    写下来是因为**这一份是全仓第一个既要 app 写语料、又要 worker 跑回合、
    还要读出站行的 e2e** —— 三个角色。哪天有人把它搬到一个只有 worker URL
    的地方跑，红在这里比红在一条 `permission denied` 上好读。
    """

    assert os.environ.get("FASTAGENT_DATABASE_URL"), "写语料要 app 角色"
    assert settings.worker_database_url, "跑回合要 worker 角色"
    assert str(uuid.UUID(str(TENANT_A))) == str(TENANT_A)


def test_the_reply_survives_a_failed_audit_write(
    settings: Settings, seeded_database: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**审计那几列写不进去时，客户照样收到那条回复**（独立审查 P1-1 的端到端那一半）。

    上面那条 e2e 证明的是链路通；这一条证明的是**链路最后一段坏掉时先保住谁**。
    这一刻三次模型调用已经花完钱、生成已经过校验、`intent` 已经落库 ——
    而 `_record_generation_side` 是**在那之后**的一次纯审计写。

    做法：让 `record_generation_side` 抛一个真实的库侧异常。
    断言三件：出站行**在**（回复没被丢）、证据行仍然是检索那一刻那一份
    （四列空着 —— 与「这一轮没走到生成」同形，这是这次失败的可见代价），
    以及**那个计数器涨了**（否则它就从「丢回合」变成了「静默丢数据」）。
    """

    from fastagent_v2.worker import pipeline as pipeline_module
    from fastagent_v2.worker.metrics import RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL

    def _explode(*_: Any, **__: Any) -> None:
        raise OperationalError("injected: the audit write failed")

    monkeypatch.setattr(pipeline_module, "record_generation_side", _explode)
    before = RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL.value("knowledge")

    _publish_corpus(settings)
    conversation_id = new_conversation(TENANT_A)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    _drive(
        settings=settings, inference=_KnowledgeCitingInference(), embedder=_CountingEmbedder()
    )

    sent = _outbox(settings, conversation_id)
    assert len(sent) == 1, "审计写失败把一条已经成文的回复带走了 —— 正是 P1-1 那条路"
    assert sent[0]["command"]["content"]["text"] == REPLY
    row = _evidence(settings, conversation_id)[0]
    assert row["quoted_side"] is None and row["cited_refs"] is None, (
        "这次失败的可见代价就是这几列空着，而它与「没走到生成」同形 —— "
        "所以必须有个计数器把两者分开"
    )
    assert RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL.value("knowledge") == before + 1
