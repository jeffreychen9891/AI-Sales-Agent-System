"""裁定四那三列**是怎么算出来的** —— 尤其「素材优先」在 `quoted_side` 上的读法。

列的形状与授权在 `tests/integration/test_knowledge_in_generation.py`（那一份连真库）。
**这一份只问推导**：给定一份生成结果和一次检索，三个值各是什么。

**为什么它值一份单独的用例**：`quoted_side` 是本批唯一一个
**平台自己下的判断**（其余每一列都是抄一个已经发生过的事实）。
裁定四（`knowledge-base-schedule-2026-09-11.md` §5.2.2）把它的形状定成
「**共现 + 用了哪边**」而不是「冲突」—— 判两段自然语言矛不矛盾要理解它们，
**平台把它当成一次判断去做，就是在没有来源的情况下生成一个事实结论**，
正是整套机制要禁的那件事。所以这里能测的只有一件：
**这三个值是不是从数据里读出来的，而不是被谁猜出来的。**
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker import pipeline as pipeline_module
from fastagent_v2.worker.channel import MATERIAL_REF_SEGMENT_TYPE
from fastagent_v2.worker.cost import CallContext
from fastagent_v2.worker.metrics import RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL
from fastagent_v2.worker.pipeline import InboxProcessor
from fastagent_v2.worker.retrieval import RetrievalResult
from psycopg import OperationalError

SHIPPING = "knowledge:shipping-policy:v1"
RETURNS = "knowledge:returns-window:v1"
MATERIAL_REF = "material:demo-opening:v1"


class _FakeDatabase:
    """只提供 `transaction()`。**这条路径不碰别的**，给它一个真库会把问题换成别的。"""

    @contextmanager
    def transaction(self, *, tenant_id: UUID) -> Iterator[object]:
        yield object()


class _DatabaseThatIsDown:
    """开事务就抛 —— 一个真实的库侧故障长这样（连接断、RLS 拒、语句超时）。"""

    @contextmanager
    def transaction(self, *, tenant_id: UUID) -> Iterator[object]:
        raise OperationalError("injected: the database went away")
        yield object()  # pragma: no cover - 为了让它仍是一个 generator


@pytest.fixture()
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """把 `record_generation_side` 换成一个记录器 —— 它的 SQL 归集成用例验。"""

    calls: list[dict[str, Any]] = []

    def _capture(connection: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(pipeline_module, "record_generation_side", _capture)
    return calls


def _processor() -> InboxProcessor:
    return InboxProcessor(
        database=_FakeDatabase(),  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="probe",
    )


def _context() -> CallContext:
    return CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=uuid4(),
        correlation_id=uuid4(),
    )


def _retrieval(*refs: str) -> RetrievalResult:
    hits = tuple(
        {"ref": ref, "text": f"{ref} 的正文", "score": 0.1, "chunk_id": ref}
        for ref in refs
    )
    sent = tuple({"ref": str(hit["ref"]), "text": str(hit["text"])} for hit in hits)
    return RetrievalResult(
        outcome="hit" if hits else "empty",
        hits=hits,
        release_id=uuid4(),
        deadline_ms=2000,
        elapsed_ms=12,
        degradation=None,
        query_hash=sha256(b"q").hexdigest(),
        sent_chunks=sent,
    )


def _generation(
    *, segments: list[dict[str, Any]], cited: list[str] | None = None
) -> dict[str, Any]:
    return {
        "customer_visible_segments": segments,
        "claims": [
            {
                "text_hash": sha256("满 99 包邮".encode()).hexdigest(),
                "source_evidence_refs": list(cited or []),
            }
        ],
    }


def _text(content: str = "满 99 元就包邮。") -> dict[str, Any]:
    return {"type": "text", "content_or_ref": content}


def _record(
    *,
    retrieval: RetrievalResult,
    generation: dict[str, Any],
    materials: dict[str, str] | None = None,
) -> None:
    _processor()._record_generation_side(
        context=_context(),
        retrieval=retrieval,
        materials=materials or {},
        generation=generation,
    )


# ---------------------------------------------------------------------------
# `quoted_side`：客户真正看到的字属于哪一边
# ---------------------------------------------------------------------------


def test_a_material_quote_wins_even_when_a_chunk_was_also_cited(
    written: list[dict[str, Any]],
) -> None:
    """**这一条是裁定四「素材优先」在这一列上的全部含义。**

    模型两边都用了：正文里逐字引了一段素材，同时把知识库的 ref 放进了
    `source_evidence_refs` 当依据。这一列记 `material` ——
    因为**客户真正看到的字**是平台在发送路径上逐字换进去的那段商家话术，
    知识库只是模型写周边那几句时的依据。

    **反过来记 `knowledge` 会让这一列在最要紧的那个回合上说谎**：
    「素材与知识库同时可用时，客户听到的是谁的话」正是裁定四要回答的问题。
    """

    _record(
        retrieval=_retrieval(SHIPPING),
        generation=_generation(
            segments=[{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": MATERIAL_REF}],
            cited=[SHIPPING],
        ),
        materials={MATERIAL_REF: "您好，这里是示例商家的客服助手。"},
    )

    assert written[0]["quoted_side"] == "material"
    assert written[0]["cited_refs"] == [SHIPPING], (
        "**素材赢了这一列，而引用集照记**：两者答的是两个问题 —— "
        "「客户看到的字属于哪一边」与「这句话的依据是哪几条」。"
        "把引用集在 material 的回合上清空，等于把裁定四要的共现记录丢掉一半"
    )


def test_only_a_citation_and_no_material_segment_reads_as_knowledge(
    written: list[dict[str, Any]],
) -> None:
    """没有素材段、而引了本轮给过的 chunk —— 这一轮用的是知识库。"""

    _record(
        retrieval=_retrieval(SHIPPING, RETURNS),
        generation=_generation(segments=[_text()], cited=[SHIPPING]),
    )

    assert written[0]["quoted_side"] == "knowledge"


def test_neither_is_its_own_answer_not_a_missing_value(
    written: list[dict[str, Any]],
) -> None:
    """两边都没用：`neither`，**不是 `NULL`**。

    `NULL` 在这张表里已经有意思了 ——「这一轮没走到生成」（`050` 的列注释）。
    一个走到了生成、而模型两边都没用的回合是**一个真实结局**，
    而它恰好是最该被数出来的那一种：检索花了钱、模型没用。
    """

    _record(
        retrieval=_retrieval(SHIPPING),
        generation=_generation(segments=[_text()], cited=[]),
    )

    assert written[0]["quoted_side"] == "neither"
    assert written[0]["cited_refs"] == [], (
        "空列表不是 NULL：这一轮走到了生成、而模型一条都没引 —— "
        "那是一个真实结局（检索花了钱，没落地），也正是最该被数出来的那一种"
    )


def test_a_non_knowledge_ref_does_not_count_as_using_the_knowledge_base(
    written: list[dict[str, Any]],
) -> None:
    """`source_evidence_refs` 里也有 understanding 那一侧的东西，**它们不算**。

    一条只引了 `span:…` 的声明是模型拿**这一轮对话**当依据，
    与知识库无关。把它算成 `knowledge` 会让这一列虚高 ——
    而这一列的读者正在用它判断「语料到底有没有被用上」。
    """

    _record(
        retrieval=_retrieval(SHIPPING),
        generation=_generation(segments=[_text()], cited=["span:9f2c1b"]),
    )

    assert written[0]["quoted_side"] == "neither"
    assert written[0]["retrieved_unused"] == 1


# ---------------------------------------------------------------------------
# `retrieved_unused` 与 `materials_available`
# ---------------------------------------------------------------------------


def test_unused_counts_the_hits_the_model_did_not_cite(
    written: list[dict[str, Any]],
) -> None:
    """检出两条引了一条 → `1`。

    **它与 `hits_sent` 是两件事**：后者是「平台没送」（额度挡下来的），
    前者是「模型没用」。混成一个数，
    「该重推预算表」与「该调检索或补语料」就分不开了。
    """

    _record(
        retrieval=_retrieval(SHIPPING, RETURNS),
        generation=_generation(segments=[_text()], cited=[RETURNS]),
    )

    assert written[0]["retrieved_unused"] == 1


def test_it_counts_against_the_hits_not_against_what_was_sent(
    written: list[dict[str, Any]],
) -> None:
    """数的分母是 `hits`（检出的），**不是 `sent_chunks`（送进去的）**。

    **生产今天造不出 `sent < hits`**（额度覆盖 top-2，见 `test_knowledge_bound.py`
    那条「丢不掉」的用例），所以这一条手工造了一个 —— 而它**分得开两种实现**：
    分母取 `sent_chunks` 的话，引了唯一送出去的那条之后结果是 0。

    分母该是 `hits`：被额度挡下来的那条**模型根本没机会用它**，
    但它确实是「检出而没被用上」的一条。两个数各有读者 ——
    `hits_sent` 说平台没送，这一列说这一轮检出来的东西有多少落了地。
    """

    both = _retrieval(SHIPPING, RETURNS)
    only_first = RetrievalResult(
        outcome=both.outcome,
        hits=both.hits,
        release_id=both.release_id,
        deadline_ms=both.deadline_ms,
        elapsed_ms=both.elapsed_ms,
        degradation=None,
        query_hash=both.query_hash,
        sent_chunks=both.sent_chunks[:1],
        dropped=1,
    )

    _record(
        retrieval=only_first,
        generation=_generation(segments=[_text()], cited=[SHIPPING]),
    )

    assert written[0]["retrieved_unused"] == 1, (
        "被额度挡下来的那条算「没被引」—— 而它算的是 hits，"
        "所以引了送出去的那一条之后，剩下的一条仍然是 1"
    )


def test_materials_available_is_about_the_turn_not_about_the_quote(
    written: list[dict[str, Any]],
) -> None:
    """**「有素材可用」与「用了素材」是两件事，这一列记前者。**

    裁定四要记的是**共现**：两种来源同时摆在模型面前的那些回合。
    只在素材被引时才记 `true`，就永远看不到「素材明明有、模型却去用知识库」
    的那些回合 —— 而那正是这一列存在的理由。
    """

    _record(
        retrieval=_retrieval(SHIPPING),
        generation=_generation(segments=[_text()], cited=[SHIPPING]),
        materials={MATERIAL_REF: "您好，这里是示例商家的客服助手。"},
    )

    assert written[0]["materials_available"] is True
    assert written[0]["quoted_side"] == "knowledge", "有素材但没引它 —— 正是要记下来的那种回合"


def test_the_row_is_addressed_by_this_turns_correlation_id(
    written: list[dict[str, Any]],
) -> None:
    """写的是**这一回合那一行**：`tenant_id` + `correlation_id`。

    `correlation_id` 取自信封，与检索那一刻写下 `retrieval_evidence` 用的是同一个 ——
    所以这次 `UPDATE` 必定打在同一行上。用别的键（比如会话）会打到上一轮那一行。
    """

    context = _context()
    _processor()._record_generation_side(
        context=context,
        retrieval=_retrieval(SHIPPING),
        materials={},
        generation=_generation(segments=[_text()], cited=[]),
    )

    assert written[0]["tenant_id"] == context.tenant_id
    assert written[0]["correlation_id"] == context.correlation_id


# ---------------------------------------------------------------------------
# 接线：留住结果、两处共用同一个集合
# ---------------------------------------------------------------------------


def test_the_retrieval_result_is_kept_instead_of_discarded() -> None:
    """`_retrieve_if_requested` **把结果交回来**（6b 那一版返回 `None`）。

    丢掉它的后果不是崩：`hits_sent` 与裁定四那三列会**永远是 `NULL`**，
    而 `NULL` 在这张表里有意思（「没走到生成」）—— 于是一条接线断掉的生产线
    在数据上读起来像「每一轮都没走到生成」。
    """

    class _Retriever:
        def __init__(self) -> None:
            self.result = _retrieval(SHIPPING)

        def run(self, *, context: CallContext, query: str) -> RetrievalResult:
            return self.result

    retriever = _Retriever()
    processor = InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="probe",
        retriever=retriever,
    )
    from fastagent_v2.worker.inference_validation import ValidatedPlan

    plan = ValidatedPlan(
        document={
            "schema_version": "1.0.0",
            "origin": "model",
            "selected_strategy_id": "acknowledge",
            "strategy_reason_code": "ACK",
            "knowledge_query": "运费规则",
        },
        frame={},
        definition={},
    )

    returned = processor._retrieve_if_requested(
        plan=plan, context=_context(), profile={"knowledge_bindings": ["knowledge:probe-corpus:v1"]}
    )

    assert returned is retriever.result


def test_the_prompt_and_the_validator_are_handed_the_same_set() -> None:
    """**同一个集合交给两处**，而这是一条读源码的断言。

    只有一处拿到会长成两种故障，两种都不会崩：
    提示词有、校验器没有 → 模型引本轮真给过的 chunk 被拒（fail-closed 拒对了人）；
    校验器有、提示词没有 → 一条编出来的 ref 被放行（那道闸整条失效）。
    **管线里算两遍也算「不是同一个集合」** —— 与素材那一侧同一条纪律。
    """

    import inspect
    from pathlib import Path

    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")

    # **判据 2026-09-19（批 J / T367）换了形状，而性质没换 —— 换得更强了。**
    #
    # 先前这里数的是 `source.count("knowledge=turn_knowledge,") == 2`：
    # 那两处在同一个方法里并排写着。批 J 把「一次生成 + 校验」抽成
    # `_generate_and_validate`（因为 T367 的重跑要调它第二遍），于是那两处
    # 变成了**同一个形参**喂下去 —— **结构上不可能再分叉**，
    # 而旧的计数当场变成 1。
    #
    # 所以今天读那个方法的源码：`knowledge=knowledge` 出现两次，
    # 而它们指的是同一个形参。**「管线里算两遍」那一半判据不变**，
    # 仍然按 `turn_knowledge = ` 只出现一次钉住。
    body = inspect.getsource(pipeline_module.InboxProcessor._generate_and_validate)
    assert body.count("knowledge=knowledge,") == 2, (
        "generate 与 validate_generation 各一处 —— 少一处就是上面那两种故障之一"
    )
    assert "knowledge: Mapping[str, str]," in body, (
        "那两处不再由同一个形参喂 —— 它们又可以各自漂了"
    )
    assert source.count("turn_knowledge = ") == 1, "这一轮的知识库只算一次"
    # **两个调用点，而它们传的是同一个变量** —— 首次尝试与 T367 的重跑。
    # ⚠️ 判据是「**同一个** `turn_knowledge`」，不是「只有一处」：
    # 重跑**必须复用**第一次的检索结果（再检一遍要再花一次钱，
    # 还会多写一行 `retrieval_evidence` —— 而 `050` 把那张表定成了
    # 「这一轮检索发生过什么」，一轮两行读起来像检索了两次）。
    assert source.count("knowledge=turn_knowledge,") == 2, (
        "把 `turn_knowledge` 传进去的调用点不是两个 —— 要么重跑那一次没复用它"
        "（那就是再检一遍），要么首次尝试那一处没了"
    )


def test_the_citation_set_is_what_the_first_hop_needs(
    written: list[dict[str, Any]],
) -> None:
    """**引了哪几条**，而不只是「剩几条没被引」（`062`，出口判据 §6.1 第 4 条）。

    检出两条、引了第二条：`retrieved_unused = 1` 与 `cited_refs = [RETURNS]`
    是两件不同的事实，而在这一列之前**库里只有前者** ——
    于是「上周那句话指回哪一段原文」这个问题在 top-2 的回合上答不出来，
    尽管 `050` 的 `hits` 列注释正是为这个问题写的。

    **升序去重**：这一列是一个集合。顺序不定的数组会让「两行记的是不是同一件事」
    变成一次比对练习。
    """

    _record(
        retrieval=_retrieval(SHIPPING, RETURNS),
        generation=_generation(
            segments=[_text()], cited=[RETURNS, RETURNS, "span:9f2c1b"]
        ),
    )

    assert written[0]["cited_refs"] == [RETURNS], "去重，且非知识库的 ref 不进这一列"
    assert written[0]["retrieved_unused"] == 1
    assert written[0]["quoted_side"] == "knowledge"


# ---------------------------------------------------------------------------
# 库侧故障：写不下这几列，**不许把回复带走**（独立审查 P1-1）
# ---------------------------------------------------------------------------


def test_a_database_failure_here_does_not_take_the_turn_down() -> None:
    """**这几列写不进去，只留一个数，回合照走。**

    独立审查 2026-09-12 **P1-1**：这一句先前是裸的，于是任何库侧异常都会穿过
    `process_prepared`，`process_pointer` 记一次失败尝试后原样再抛，
    `MAX_INBOX_ATTEMPTS` 之后这条入站事件终态失败 ——
    **而这一刻三次模型调用的钱已经花完、生成也过了校验**，
    丢掉的是一条已经成文的回复，理由只是这四列没写进去。

    **口径是 6b 立的**（`KnowledgeRetriever.run` 那两处 `except PsycopgError` +
    计数器），6c 没带过来，6d 还往这条语句上又加了第四列 ——
    **写得越多，这条路的到达面越大**。

    断言两件：**不抛**，以及**那个数真的记下来了** ——
    没有第二件，这条路就从「丢回合」变成了「静默丢数据」，
    而后者正是这一族计数器存在的理由（`metrics.py` 那两段注释）。
    """

    before = RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL.value("knowledge")
    processor = InboxProcessor(
        database=_DatabaseThatIsDown(),  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="probe",
    )

    processor._record_generation_side(
        context=_context(),
        retrieval=_retrieval(SHIPPING),
        materials={},
        generation=_generation(segments=[_text()], cited=[SHIPPING]),
    )

    assert RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL.value("knowledge") == before + 1, (
        "写不下这几列必须留下一个数：行在、而判定那几列空着会被读成"
        "「这一轮没走到生成」（`050` 给 NULL 定的含义）"
    )
