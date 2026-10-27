"""检索这一步**真的被接上了**，以及它读的是哪一个判据。

**为什么需要这一份**：`InboxProcessor(retriever=...)` 是**可选参数**，
`cli.run_worker` 里那段装配删掉之后**不会有任何东西报错** ——
管线会安静地整步跳过，而数据上「没有检索器」与「模型没请求检索」同形
（两者都不留证据行）。那正是本仓反复付钱的那一类：
一句没有东西守着的声明会漂（`test_knowledge_grants.py` 的模块 docstring 记着上一次）。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.settings import OFFLINE_MODEL_PROVIDERS
from fastagent_v2.worker.cost import CallContext
from fastagent_v2.worker.inference_validation import ValidatedPlan
from fastagent_v2.worker.pipeline import InboxProcessor

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "services/worker/src/fastagent_v2/worker/cli.py"


class _RecordingRetriever:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, *, context: CallContext, query: str) -> Any:
        self.queries.append(query)
        return None


def _plan(**overrides: Any) -> ValidatedPlan:
    document: dict[str, Any] = {
        "schema_version": "1.0.0",
        "origin": "model",
        "selected_strategy_id": "acknowledge",
        "strategy_reason_code": "ACK",
        **overrides,
    }
    return ValidatedPlan(document=document, frame={}, definition={})


#: **绑了资料库的最小 profile**（2026-09-24 K1 接线后，这一步先读绑定）：
#: 这份用例验的是「读哪个字段、结果去哪」，开关那半在
#: `test_the_design_that_could_never_fire.py` 单独验，所以这里恒开。
_BOUND = {"knowledge_bindings": ["knowledge:probe-corpus:v1"]}


def _processor(retriever: Any) -> InboxProcessor:
    """只为调 `_retrieve_if_requested` 而装的一个处理器。

    **四个依赖全给 `None`**：这条路径一个都不碰，而给它们真东西会让这几条用例
    需要一个数据库与一个队列 —— 那样验的就不是「这一步读哪个字段」了。
    """

    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="probe",
        retriever=retriever,
    )


def _context() -> CallContext:
    return CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=uuid4(),
        correlation_id=uuid4(),
    )


def test_a_plan_that_asked_nothing_does_not_retrieve() -> None:
    """字段缺席 → 整步跳过，**不留证据行**（裁定三：没请求就不检）。"""

    retriever = _RecordingRetriever()
    _processor(retriever)._retrieve_if_requested(plan=_plan(), context=_context(), profile=_BOUND)

    assert retriever.queries == []


def test_a_plan_that_asked_retrieves_with_the_string_as_written() -> None:
    """请求串原样交给检索层 —— 预处理（strip、长度）归那一层。"""

    retriever = _RecordingRetriever()
    _processor(retriever)._retrieve_if_requested(
        plan=_plan(knowledge_query="  运费规则  "), context=_context(), profile=_BOUND
    )

    assert retriever.queries == ["  运费规则  "]


def test_an_unusable_request_still_reaches_the_retriever() -> None:
    """**这一条是这份用例里最要紧的。**

    管线读的是 `knowledge_query_as_written`，**不是** `requested_knowledge_query`。
    后者把不可用的请求读成 `None`，于是这一步会整步跳过 ——
    而 ADR-0133 的第二态（`unusable_request`）要求那种情况**留下一行审计**。
    拿错读法的后果不是崩，是「模型压根没请求」与「模型写歪了」在数据里长成一样。
    """

    retriever = _RecordingRetriever()
    _processor(retriever)._retrieve_if_requested(
        plan=_plan(knowledge_query="   "), context=_context(), profile=_BOUND
    )

    assert retriever.queries == ["   "], "不可用的请求也要走到检索层，它才写得下那一行"


def test_no_retriever_means_the_step_is_skipped_not_crashed() -> None:
    """没接检索器时整步跳过 —— mock 供应商那条路就是这样。"""

    _processor(None)._retrieve_if_requested(
        plan=_plan(knowledge_query="运费规则"), context=_context(), profile=_BOUND
    )


def test_the_worker_actually_wires_one_for_a_real_provider() -> None:
    """**装配那一处读出来验**，因为它没有别的守卫。

    三件事一起断言，少一件这条用例就会在装配被拆掉时仍然绿：
    `KnowledgeRetriever` 被构造、它拿到了 `governor`（否则嵌入调用没有账）、
    并且真的被交给了 `InboxProcessor`。
    """

    source = CLI.read_text(encoding="utf-8")

    assert "retriever = KnowledgeRetriever(" in source
    assert "retriever=retriever," in source, "构造了但没交给 InboxProcessor，等于没接"
    assert "governor=governor," in source
    # 与 governor 同一个条件：**离线供应商**下不装配。
    assert source.count("if settings.model_provider not in OFFLINE_MODEL_PROVIDERS:") == 2


def _code_without_prose(source: str) -> str:
    """源码去掉注释与 docstring，只留**真正会执行的那部分**。

    ⚠️ 为什么需要这一步：本仓纪律要求更正**不删原文**，于是
    `cli.py` 里那段撤销注记**逐字引用了**被它替换掉的旧代码
    （`!= "mock"`）。一条按纯文本搜的守卫会把那段散文当成违规，
    而**那种假红最后一定会以「把守卫改松」收场** —— 这比没有守卫更糟。

    所以这里按 token 判：丢掉 `COMMENT`，以及**出现在语句开头的字符串**
    （那就是 docstring）。比较用的字符串字面量不在语句开头，所以留得下来 ——
    这正是要查的东西。
    """

    kept: list[str] = []
    at_statement_start = True
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and at_statement_start:
            at_statement_start = False
            continue
        if token.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            at_statement_start = True
        elif token.type != tokenize.COMMENT:
            at_statement_start = False
        kept.append(token.string)
    return " ".join(kept)


def test_the_wiring_asks_the_table_and_never_names_one_offline_provider() -> None:
    """装配条件必须**查表**，不许写死某一支离线供应商的名字。

    ⚠️ 这一条是实测逼出来的，而它的代价是**一个离线演示会真的打网络**：

    原本两处都写 `settings.model_provider != "mock"`。`scripted` 进
    `OFFLINE_MODEL_PROVIDERS` 之后，**这两行没跟着改** —— 于是
    `FASTAGENT_MODEL_PROVIDER=scripted` 跑起来会：

    1. 装配 `CostGovernor`，**给一次零成本的回放记一笔编造的账**
       （正是那段注释说它要避免的事）；
    2. 装配 `KnowledgeRetriever`，而它内层是
       `HttpxChatTransport(httpx.Client())` —— **一个真的会去调嵌入端点的
       HTTP 客户端**。于是「不需要 key、不需要联网」的演示在第一次检索请求上
       就去连真实供应商了。

    `pytest-socket` 只在测试里拦得住它；**真跑演示的时候没有那道闸。**

    **这就是「忘了加一个 or」与「有意不挡它」在代码里长得一模一样** ——
    `runtime/settings.py` 那张表的注释已经把这句话写下来了，
    而这两行**在表外面**，所以那句话管不到它们。
    判据因此是：这个文件里不许出现把某一支离线供应商单独点名的比较。
    """

    code = _code_without_prose(CLI.read_text(encoding="utf-8"))
    quote = chr(34)
    named = [
        spelling + quote + name + quote
        for name in OFFLINE_MODEL_PROVIDERS
        for spelling in ("== ", "!= ")
        if spelling + quote + name + quote in code
    ]
    assert not named, (
        "cli.py singles out an offline provider by name; ask "
        "OFFLINE_MODEL_PROVIDERS instead, or the next offline provider added to "
        "that table will silently wire a cost ledger and a real HTTP client: "
        f"{named}"
    )
