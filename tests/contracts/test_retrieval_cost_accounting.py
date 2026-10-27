"""嵌入调用的账：绑定怎么来的，以及被上限拒了之后这一回合还活着。

**裁定六**：检索失败不停这一回合。`providers.complete` 在上限拒绝那一刻抛
`CostCeilingExceeded`，而管线捕获它并走 `_persist_no_send`（不发消息）——
**检索不能走那条路**，否则「加分项」会决定客户这一轮收不到话。

真实台账那一侧（一行 `settled`、token 数、扁平价目）在
`tests/integration/test_retrieval_metering.py`。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker.cost import CallContext, CeilingDecision
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.metrics import RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.retrieval import (
    EMBEDDING_COST_ROLE,
    RETRIEVAL_DEADLINE_MS,
    RETRIEVER_VERSION,
    KnowledgeRetriever,
    ProviderCostCeiling,
    embedding_cost_binding,
)
from psycopg import OperationalError

TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DIM = 2048


def _context() -> CallContext:
    return CallContext(tenant_id=TENANT, conversation_id=uuid4(), correlation_id=uuid4())


def test_the_binding_comes_from_the_live_release_not_a_literal() -> None:
    """绑定的每一项都从活着那一版 model policy 的 `embedding` 块读。

    **写死 provider / model / 价目的后果是安静的**：换了一版 release 之后，
    账上记的仍是旧模型与旧价，而那两个数是「这个租户花了多少」的输入
    （`import_knowledge.py` 的 `_binding()` 为同一件事被审查打回过一次）。
    """

    document = active_release().document["embedding"]
    binding = embedding_cost_binding(active_release())

    assert binding.role == EMBEDDING_COST_ROLE
    assert binding.provider_id == document["provider_id"]
    assert binding.model_id == document["model_id"]
    assert binding.price_input == float(document["pricing"]["text_input"])
    assert binding.deadline_ms == RETRIEVAL_DEADLINE_MS


def test_the_binding_can_be_priced_and_therefore_falls_under_the_ceiling() -> None:
    """**`max_output_tokens=0` 不是随手填的。**

    `CostGovernor.enforcement_status` 对 `None` 返回「不可判上限」——
    那会让嵌入调用**绕过上限**。`0` 既是事实（嵌入没有输出 token），
    又让它受上限管。价目那一侧同理：`can_price` 为假时上限也不生效。
    """

    binding = embedding_cost_binding(active_release())

    assert binding.max_output_tokens == 0
    assert binding.can_price
    assert binding.pricing_period(datetime.now().astimezone()) == "flat"
    # 20 token 的输入，扁平价：cost = 20 * price / 1e6，输出恒 0。
    expected = 20 * float(binding.price_input or 0) / 1_000_000
    assert binding.cost_cny(input_tokens=20, output_tokens=0) == pytest.approx(expected)


class _DenyingGovernor:
    """一个只会拒的台账替身，记下它被要求做过什么。"""

    def __init__(self) -> None:
        self.marked_failed: list[UUID] = []
        self.settled: list[UUID] = []
        self.row_id = uuid4()

    def estimate(self, *, binding: Any, system_prompt: str, user_prompt: str) -> Any:
        return 20, 0, Decimal("0.001")

    def reserve(self, **kwargs: Any) -> Any:
        decision = CeilingDecision(
            effect="deny",
            reason_code="HARD_DAILY_CEILING",
            window="daily",
            limit=Decimal("0.0001"),
            projected=Decimal("0.001"),
            enforcement_active=True,
            inactive_reason=None,
        )
        return decision, self.row_id

    def mark_failed(self, *, row_id: UUID, context: CallContext) -> None:
        self.marked_failed.append(row_id)

    def settle(self, **kwargs: Any) -> None:
        self.settled.append(kwargs["row_id"])


class _Embedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        self.calls += 1
        return Embedding(
            vector=tuple([1.0] + [0.0] * (DIM - 1)),
            model_id="probe",
            dimension=DIM,
            prompt_tokens=7,
        )


class _Cursorish:
    def __init__(self, database: _Database) -> None:
        self._database = database

    def execute(self, query: str, params: Any = None) -> Any:
        if "INSERT INTO retrieval_evidence" in query:
            self._database.recorded.append(params)
            return self
        # `SET LOCAL statement_timeout` 是 P2-4 之后每段事务的第一句。
        if "statement_timeout" in query or "knowledge_releases" in query:
            return self
        raise AssertionError(f"这条用例没打算让这句 SQL 跑起来: {query[:60]}")

    def fetchone(self) -> Any:
        return {"id": uuid4()} if self._database.published else None

    def fetchall(self) -> list[Any]:
        return []


class _Transaction:
    def __init__(self, database: _Database) -> None:
        self._database = database

    def __enter__(self) -> Any:
        return _Cursorish(self._database)

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Database:
    """一个只支持 `transaction()` 的替身。

    **上限那条路在读 release 之后、调供应商之前**，所以这几条用例不需要真库 ——
    它们需要的那一点（`status='published'` 有没有行）在这里是可控的，
    而写进去的那一行被记下来供断言。真库那一侧另有一份 integration 用例。
    """

    def __init__(self, *, published: bool) -> None:
        self.published = published
        self.recorded: list[Any] = []

    def transaction(self, *, tenant_id: UUID) -> Any:
        return _Transaction(self)


def _retriever(database: _Database, governor: _DenyingGovernor, embedder: _Embedder) -> Any:
    return KnowledgeRetriever(
        database=database,  # type: ignore[arg-type]
        embedder=embedder,
        release=active_release(),
        governor=governor,  # type: ignore[arg-type]
    )


def test_a_ceiling_refusal_degrades_and_the_turn_survives() -> None:
    """被上限拒 → `outcome='failed'` + 指名上限的 `degradation`，**不抛**。

    这是裁定六在钱这一侧的样子：`providers.complete` 会抛 `CostCeilingExceeded`，
    而那个异常在管线里的意思是「这一轮不发消息」。检索借用它就等于让一个加分项
    决定客户收不收得到话。
    """

    governor = _DenyingGovernor()
    embedder = _Embedder()

    result = _retriever(_Database(published=True), governor, embedder).run(
        context=_context(), query="运费规则"
    )

    assert result.outcome == "failed"
    assert "HARD_DAILY_CEILING" in (result.degradation or "")
    assert embedder.calls == 0, "被拒之后不该再花那次调用"
    assert governor.settled == []


def test_a_refused_reservation_is_left_for_the_reconciler_not_marked_failed() -> None:
    """**不调 `mark_failed`** —— 这一条是门禁替本批改掉的一个决定，留档在此。

    第一版在这里调了它，理由是「这次调用确定没发生，留一串不会结算的行会顶爆
    未结算计数闸」。全量门禁红在
    `test_nothing_in_production_ever_marks_a_reservation_failed` 上，**而那条绊线是对的**：
    `failed` 断言的是「供应商没向我们计费」，**而台账里没有任何供应商侧标识，
    没有人可问** —— 计数闸的阈值正是按「这笔钱一直看不见」推出来的。

    我那条理由的方向也错了：**能拒掉一次嵌入调用的上限，一定也拒得掉同一回合里
    估值大它两个数量级的模型调用**，所以「检索被拒而模型调用照跑」几乎不可达。
    接住这些行的是 `reconcile_abandoned`：900 秒后转 `unreconciled` ——
    「不知道有没有被计费」，那才是诚实的状态。
    """

    governor = _DenyingGovernor()

    _retriever(_Database(published=True), governor, _Embedder()).run(
        context=_context(), query="运费规则"
    )

    assert governor.marked_failed == [], "写 failed 等于在没有证据时判定一次未计费"
    assert governor.settled == [], "调用没发生，也没有什么可结算"


def test_the_evidence_row_is_written_even_when_the_ceiling_refused() -> None:
    """降级也要留证据行 —— 那是裁定六不可省的另一半。"""

    database = _Database(published=True)

    _retriever(database, _DenyingGovernor(), _Embedder()).run(
        context=_context(), query="运费规则"
    )

    assert len(database.recorded) == 1
    params = database.recorded[0]
    assert RETRIEVER_VERSION in params
    assert "failed" in params


def test_the_ceiling_error_is_not_the_one_the_pipeline_treats_as_a_no_send() -> None:
    """`ProviderCostCeiling` **不是** `cost.CostCeilingExceeded` 的子类。

    管线捕获后者并走 `_persist_no_send`。检索用的是 `ProviderError` 那一支，
    于是 `retrieve` 现成的「按跑了多久判 timeout / failed」把它记成 `failed`。
    **这一条防的是有人图省事把两者合成一个类。**
    """

    from fastagent_v2.worker.cost import CostCeilingExceeded
    from fastagent_v2.worker.providers import ProviderError

    assert issubclass(ProviderCostCeiling, ProviderError)
    assert not issubclass(ProviderCostCeiling, CostCeilingExceeded)

class _DatabaseThatFailsOn:
    """一个在指定 SQL 上抛库侧异常的替身。

    **P2-1 的两条路各需要一个**：检索本身炸（读 release 那一句）、
    审计行写不进去（那条 INSERT）。
    """

    def __init__(self, *, on: str) -> None:
        self.on = on

    def transaction(self, *, tenant_id: UUID) -> Any:
        return _FailingTransaction(self.on)


class _FailingTransaction:
    def __init__(self, on: str) -> None:
        self.on = on

    def __enter__(self) -> Any:
        return _FailingCursor(self.on)

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FailingCursor:
    def __init__(self, on: str) -> None:
        self.on = on

    def execute(self, query: str, params: Any = None) -> Any:
        if self.on in query:
            raise OperationalError("connection reset by peer")
        return self

    def fetchone(self) -> Any:
        return {"id": uuid4()}

    def fetchall(self) -> list[Any]:
        return []


def test_a_database_failure_during_retrieval_becomes_a_failed_outcome() -> None:
    """**库侧异常不再穿出去**（独立审查 2026-09-12，**P2-1**）。

    先前模块 docstring 声称「每一条失败路径都不抛」，而实现只吃掉 `ProviderError` ——
    读 release 那一句一断连，异常就穿过 `run()`、穿过 `pipeline._retrieve_if_requested`
    （那里没有 try），把整回合打死。裁定六说检索是加分项不是必需品，
    **而一个加分项当时有能力停掉必需品**。
    """

    retriever = KnowledgeRetriever(
        database=_DatabaseThatFailsOn(on="knowledge_releases"),  # type: ignore[arg-type]
        embedder=_Embedder(),
        release=active_release(),
        governor=None,
    )

    result = retriever.run(context=_context(), query="运费规则")

    assert result.outcome == "failed"
    assert "OperationalError" in (result.degradation or "")


def test_a_database_failure_while_writing_the_audit_row_only_costs_the_row() -> None:
    """**审计行写不进去也不带走回合**，而它在计数器上留下一个数。

    写不下审计行是运维要知道的事 —— 而让客户这一轮收不到消息不是它的代价。
    没有那个计数器的话，「没有行」会被读成「没请求检索」，
    那正是裁定六与 ADR-0133 要分开的两件事。
    """

    before = RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL.value("empty")
    retriever = KnowledgeRetriever(
        database=_DatabaseThatFailsOn(on="INSERT INTO retrieval_evidence"),  # type: ignore[arg-type]
        embedder=_Embedder(),
        release=active_release(),
        governor=None,
    )

    result = retriever.run(context=_context(), query="运费规则")

    assert result.outcome in {"empty", "hit"}
    assert RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL.value(result.outcome) == before + 1
