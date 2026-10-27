"""检索层：拿计划步给的查询串，读本租户已发布的那一版，取 top-2。

**裁定三**（`docs/plans/knowledge-base-schedule-2026-09-11.md` §5.2.1）把检索定成
管线的**第四步**，而「要不要检索」在 plan 就定了。**裁定八** 定了 top-2 与 chunk 900 字节上界。
**裁定六**（§5.3.2）定了失败的形状：**照常生成并发消息，另加一条审计行** ——
所以这个模块的每一条失败路径都不抛给调用方，而是变成一个 `outcome` 与一条 `degradation`。

## 这个模块**不**做什么，以及为什么

* **不把命中塞进提示词。** 那是 6c（裁定四：素材优先 + 共现记录），
  它还要决定「素材与知识库同时可用时引哪一边」。
* **不设相关性阈值。** 裁定八只说 top-2，没说分数下界。**编一个下界等于替用户定产品**
  —— 一条不相关的 chunk 是不是「命中」，取决于 6c 怎么用它。
  分数照原样写进 `hits`，让那一批有据可判。

## 嵌入调用有账（6b-2）

`KnowledgeRetriever` 把一次检索的嵌入调用走**同一套**成本台账：
`estimate → reserve → （拒就降级）→ 调用 → settle`，
与 `providers.complete` 对三个模型角色做的事逐步同形。
**不是另写一套**：上限、预留、结算、对账、`ceilings_cny` 都在那一套里，
绕过它等于让「这个租户这个月花了多少」从此偏低，而它偏低得账面自洽。

**上限拒绝也不停这一回合**（裁定六）：`providers.complete` 在那一刻抛
`CostCeilingExceeded`，**这里不抛** —— 它变成 `outcome='failed'` 加一条
指名上限的 `degradation`。检索是加分项，不是必需品。

**而账上那一行的处置与 `providers.complete` 逐字相同**：被拒或调用失败的预留
**留在 `estimated`**，由 `reconcile_abandoned` 到期转 `unreconciled`。
本批第一版在这两处调了 `mark_failed`，被门禁的那条绊线拦下 ——
`failed` 断言的是「供应商没向我们计费」，而台账里没有供应商侧标识，没有人可问。

## 判据在别处，这里只消费

「这一轮要不要检索」的唯一判据是
`worker.inference_validation.knowledge_query_as_written`（6a 落的那一处的另一面）。
**这个模块不重写「空白算不算能用」** —— 那条判据在 `is_usable_query`，两处共用；
本模块只加自己那一条：**长度上界**（`QUERY_MAX_CHARS`）。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.knowledge_bound import bound_for_prompt
from fastagent_v2.worker.cost import CallContext, CostGovernor
from fastagent_v2.worker.embedding import Embedding
from fastagent_v2.worker.inference_validation import is_usable_query
from fastagent_v2.worker.metrics import RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL
from fastagent_v2.worker.model_policy import ModelPolicyRelease, RoleBinding
from fastagent_v2.worker.providers import ProviderError, ProviderTimeout
from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.errors import QueryCanceled
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

#: 检索器的版本串，落进 `retrieval_evidence.retriever_version`。
#:
#: **它要跟着「同一个查询会得到不同结果」的任何改动走**：top-k、距离度量、
#: 读哪一版 release 的规则、查询串的预处理。**不跟着嵌入模型走** ——
#: 那一项由 `knowledge_releases.embedding_model` 记，而它是 release 的身份的一部分。
RETRIEVER_VERSION = "kb-retrieval-v1"

#: 一次检索的期限，毫秒。
#:
#: **这个数是推出来的，不是挑的**：一个回合的信封期限是 30,000ms
#: （活着那一版 model policy 的 `envelope_deadline_ms`），里面要装
#: understand / plan / generate 三次模型调用（三者各自 `deadline_ms` 8,000 上下）。
#: 检索夹在 plan 与 generate 之间，**它花的每一毫秒都是从生成那一步借的**。
#: 2,000ms 的依据：嵌入是一次单条请求（`embedding.embed_one`，实测 26 token 级的输入），
#: 向量比对是**一版 release 里几十行的精确扫描**（`050` 刻意没建近似索引，
#: 理由写在它的文件头）。**留在这里而不是进 release 文件**：它不是供应商的参数，
#: 是平台愿意为这一步花多少时间 —— 哪天要按租户调，那时再往 profile 里搬。
RETRIEVAL_DEADLINE_MS = 2_000

#: 查询串的长度上界，**归这一层管**。
#:
#: **契约层刻意不设界**（ADR-0133 / 独立审查 2026-09-12 P1-1）：界写在契约里意味着
#: 模型把查询写长一点就让**整份计划作废**，客户这一轮收不到消息 —— 而裁定六说的是
#: 检索失败照常发消息。**所以界落在花那笔字节的这一层**，越界的后果是
#: 「这一轮不检索」并留一条 `unusable_request` 的审计行。
#:
#: 200 的来处：提示词里要求的是「one short search phrase」，而 900 字节是一条 chunk
#: 的上界（裁定八）—— 一个比整条 chunk 还长的查询串不是查询串。
QUERY_MAX_CHARS = 200

#: 裁定八：**top-2**。
TOP_K = 2


class Embedder(Protocol):
    """`embedding.EmbeddingClient` 的那一面，按协议取，好让用例不必起 HTTP。"""

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding: ...


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """一次检索的全部可落库事实。**它就是 `retrieval_evidence` 的一行减掉标识列。**

    **没有「抛异常」这条出路**（裁定六）：失败是一个 `outcome` 加一条 `degradation`，
    调用方照常往下走。
    """

    outcome: str
    hits: tuple[dict[str, Any], ...]
    release_id: UUID | None
    deadline_ms: int
    elapsed_ms: int
    degradation: str | None
    query_hash: str
    #: **真的送进 generate 提示词的那几条**（`{ref, text}`，ADR-0135）。
    #:
    #: 与 `hits` 的差是字节额度挡下来的那些。两者必须分开记，
    #: 因为「检出 2 条送了 1 条」与「只检出 1 条」该修的地方不同
    #: （前者重推 ADR-0132 §八 那张表，后者补语料）。
    #:
    #: 默认空元组：不是「一条都没送」的语义，而是「这个结果没走到那一步」
    #: （不可用的请求、无语料、失败、超时）—— 那些回合 `hits` 本来也是空的。
    sent_chunks: tuple[dict[str, str], ...] = ()
    #: 被额度挡下来的条数（`hits` 减 `sent_chunks`）。
    dropped: int = 0

    @property
    def sent(self) -> int:
        return len(self.sent_chunks)


def query_hash(query: str) -> str:
    """查询串的 sha256。

    **落库落的是 hash 不是原文**（`050` 的列注释）：这是一条审计行，
    而查询串是从客户内容里派生出来的。
    """

    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _vector_literal(vector: tuple[float, ...]) -> str:
    """pgvector 的字面量形式。

    与 `scripts/import_knowledge.py:vectorise` **同一个写法** —— 仓里没有注册
    pgvector 的 psycopg 适配器，两处都拼字面量。两处不同才是问题。
    """

    return "[" + ",".join(repr(value) for value in vector) + "]"


def outcome_for_provider_error(
    error: ProviderError, *, elapsed_ms: int, deadline_ms: int
) -> str:
    """供应商那一侧出错时，这一行该记 `timeout` 还是 `failed`。

    **判据是本表的 CHECK (c)，不是异常类型**：
    「说超时，却没跑到期限」在库里是一条脏行（`CHECK (outcome <> 'timeout' OR
    elapsed_ms >= deadline_ms)`），而 `050` 的注释还多说了一句 ——
    **供应商侧几毫秒就返回的那种超时错误该记 `failed`**，因为两者该修的地方不同：
    一个调期限或检索层，一个查供应商/网络。

    **提成一个纯函数，好让这条规则被单独钉住**：它的三个输入都是数字与类型，
    不需要起数据库、也不需要真的等到 2 秒。
    """

    if isinstance(error, ProviderTimeout) and elapsed_ms >= deadline_ms:
        return "timeout"
    return "failed"


def _published_release(connection: Connection[DictRow], *, budget_ms: int) -> UUID | None:
    """本租户**已发布**的那一版，没有就是 `None`。

    `knowledge_releases_one_published_idx` 保证每个租户最多一版 `published`，
    所以这里不排序也不会有第二行。

    **`SET LOCAL statement_timeout` 只在本事务内有效**（P2-4）：库侧也要受期限管，
    一个把 30 秒信封吃光的扫描比一次超时更贵。
    """

    # **`set_config(..., true)` 而不是 `SET LOCAL … = %s`**：`SET` 不接绑定参数
    # （实测 `syntax error at or near "$1"`），而 `set_config` 的第三个参数 `true`
    # 就是 `LOCAL` 的意思 —— 只在本事务内有效。仓里设 `app.tenant_id` 用的也是它。
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)", (str(max(1, budget_ms)),)
    )
    row = connection.execute(
        "SELECT id FROM knowledge_releases WHERE status = 'published'"
    ).fetchone()
    return None if row is None else UUID(str(row["id"]))


@dataclass(frozen=True, slots=True)
class _Candidate:
    """一条命中的两半：**进证据行的那一份**，和**只进提示词的正文**。

    **正文刻意不进 `hits`**（也就是不进 `retrieval_evidence.hits`）。
    6b 定下那一列的形状时，抄进去的只有 `source_spans` ——
    理由是三跳追溯必须活过 chunk 被 supersede，而 `055` 只禁写不删，
    **正文靠 `ref` + `text_hash` 一定回得去**。
    把正文也抄一份进审计表，等于每个检索回合往那张表里多写最多 1.8KB 的语料副本，
    而**那是一次悄悄扩大一个已经过审的产物** —— 6c 不做这件事。

    这个分法本身有一条用例钉着（`test_knowledge_in_generation.py` 里
    `test_the_evidence_row_does_not_copy_the_chunk_body`）：
    `hits` 的键集恰好是那五个，正文不在里面。
    """

    evidence: dict[str, Any]
    text: str


def _top_hits(
    connection: Connection[DictRow],
    *,
    release_id: UUID,
    vector: tuple[float, ...],
    budget_ms: int,
) -> tuple[_Candidate, ...]:
    """那一版里离查询最近的 `TOP_K` 条（裁定八：top-2）。"""

    # **`set_config(..., true)` 而不是 `SET LOCAL … = %s`**：`SET` 不接绑定参数
    # （实测 `syntax error at or near "$1"`），而 `set_config` 的第三个参数 `true`
    # 就是 `LOCAL` 的意思 —— 只在本事务内有效。仓里设 `app.tenant_id` 用的也是它。
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)", (str(max(1, budget_ms)),)
    )
    literal = _vector_literal(vector)
    rows = connection.execute(
        # `body` **6c 才加**：它只去提示词，不进 `hits`（见 `_Candidate`）。
        # 先前这个 SELECT 不取正文，而 6c 第一版直接把 `hits` 交给了上界 ——
        # `KeyError: 'text'`，门禁上 12 条红（自审 §九.3）。
        "SELECT id, ref, slug, body, source_spans, "
        "       1 - (embedding <=> %s::vector) AS score "
        "  FROM knowledge_chunks "
        " WHERE release_id = %s AND embedding IS NOT NULL "
        " ORDER BY embedding <=> %s::vector "
        # **`LIMIT` 也走参数**，不是 f-string 插进去的 —— 不是因为 `TOP_K` 危险
        # （它是本模块的一个 int 常量），是因为 f-string 拼 SQL 这个**形状**危险，
        # 而 ruff 的 S608 拦的正是形状。少一个 `noqa` 比多一句解释好。
        " LIMIT %s",
        (literal, release_id, literal, TOP_K),
    ).fetchall()
    return tuple(
        _Candidate(
            evidence={
                "chunk_id": str(row["id"]),
                "ref": str(row["ref"]),
                "slug": str(row["slug"]),
                "score": float(row["score"]),
                # **`source_spans` 抄进来，不留 join**（`050` 的列注释）：三跳追溯
                # 必须活过 chunk 被 supersede —— 上周引的那一句今天仍要指得回原文。
                "source_spans": row["source_spans"],
            },
            text=str(row["body"]),
        )
        for row in rows
    )


def retrieve(
    database: Database,
    *,
    tenant_id: UUID,
    query: str,
    embedder: Embedder,
    deadline_ms: int = RETRIEVAL_DEADLINE_MS,
) -> RetrievalResult:
    """检一次，**永远返回一个结果，不抛供应商错误**（裁定六）。

    **两段事务，中间那次 HTTP 调用在事务之外**（独立审查 2026-09-12，**P3-2**）。
    先前整个过程在一个打开着的事务里，于是每个检索回合多一个最长 2 秒的
    idle-in-transaction 连接 —— 而三个模型角色的调用都在事务之外，
    **本批当时与仓里既有形状不同形**。换来的那点「release 与 chunk 同一个快照」
    并不值：release 被 supersede 之后 chunk 仍在（`055` 只禁写不删），
    而我们记的是**「当时读的是哪一版」**，不是「今天重放得到同样结果」
    （裁定七：会话不 pin release）。

    **期限管两侧**（**P2-4**）：先前 `deadline_ms` 只传给 `embed_one`，
    读 release 与 `<=>` 扫描不受任何约束，于是
    `deadline_ms=2000 / elapsed_ms=9000 / outcome='hit'` 是一行完全合法的记录。
    现在两段事务各钉 `statement_timeout`（剩余预算），扫描回来再判一次总用时，
    越界记成 `timeout`（表上 CHECK (c) 正好接得住）。

    **库侧异常这里不吃** —— 吃它的是 `KnowledgeRetriever.run`（P2-1），
    因为那一层才有「写不进审计行也别把回合带走」的语境。
    """

    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def unusable(reason: str, digest: str) -> RetrievalResult:
        return RetrievalResult(
            outcome="unusable_request",
            hits=(),
            release_id=None,
            deadline_ms=deadline_ms,
            elapsed_ms=elapsed(),
            degradation=reason,
            query_hash=digest,
        )

    stripped = query.strip()
    # **一个查询串只有一个 hash**（**P3-1**）：先前不可用那两条用原串、
    # 正常那条用 `stripped`，于是同一个逻辑查询会留下两个不同的 hash ——
    # 而这一列存在的理由就是「把同一个查询认出来」。
    digest = query_hash(stripped)
    # **空白这条判据不在这里实现** —— 它在 `inference_validation.is_usable_query`，
    # 与 `requested_knowledge_query` 共用同一处。这一层只加自己那条（长度）。
    if not is_usable_query(query):
        return unusable("blank query string", digest)
    if len(stripped) > QUERY_MAX_CHARS:
        return unusable(
            f"query longer than {QUERY_MAX_CHARS} characters ({len(stripped)})", digest
        )

    def timed_out(where: str, release_id: UUID | None) -> RetrievalResult:
        """**我们自己的 `statement_timeout` 把语句取消了，那就是一次超时。**

        `QueryCanceled` 穿出去会变成 `run()` 里的 `failed`（P2-1 兜住了回合），
        **而那条记录会说错话**：`failed` 该留给「检索层或供应商出了问题」，
        这一种是「这一步超出了它自己的预算」—— 与 `timeout` 是同一件事。
        `elapsed_ms` 取 `max(实测, deadline)`：表上 CHECK (c) 要
        `timeout` 的 `elapsed_ms >= deadline_ms`，而语句在 1ms 上被取消时
        实测可能还差一点。
        """

        return RetrievalResult(
            outcome="timeout",
            hits=(),
            release_id=release_id,
            deadline_ms=deadline_ms,
            elapsed_ms=max(elapsed(), deadline_ms),
            degradation=f"statement timeout while {where} (budget {deadline_ms}ms)",
            query_hash=digest,
        )

    # **先读 release，后调供应商**：没有已发布的那一版时，这一次检索不该花钱。
    try:
        with database.transaction(tenant_id=tenant_id) as connection:
            release_id = _published_release(connection, budget_ms=deadline_ms - elapsed())
    except QueryCanceled:
        return timed_out("reading the published release", None)
    if release_id is None:
        return RetrievalResult(
            outcome="empty",
            hits=(),
            # **`release_id IS NULL` 是「压根没有语料」与「检了没检到」的判别式**
            # （独立审查 2026-09-12，**P2-2**）：两者都记 `empty`，而该修的地方不同 ——
            # 一个去导入 + 发布，一个补语料或调检索。判别式写进了 `056` 的列注释，
            # 并由一条用例把两行的 `release_id` 一个 NULL 一个非 NULL 钉住。
            release_id=None,
            deadline_ms=deadline_ms,
            elapsed_ms=elapsed(),
            degradation="no published knowledge release",
            query_hash=digest,
        )

    try:
        # **给它的是剩余预算，不是整个预算**（复审 N-3）。先前传 `deadline_ms / 1000`：
        # 嵌入在 1,999ms 上成功返回时，扫描那段的预算被 `max(1, …)` 夹到 1ms、
        # 必然被取消，这一回合记成 `timeout` / 零命中 —— **而那次嵌入的钱已经花了**。
        # 行为上那是诚实降级（不违裁定六），但**它读起来像「库慢了」，
        # 实际是「嵌入把预算吃光了」** —— 两者该看的地方不同。
        embedding = embedder.embed_one(
            stripped, timeout_seconds=max(1, deadline_ms - elapsed()) / 1000
        )
    except ProviderError as error:
        spent = elapsed()
        return RetrievalResult(
            outcome=outcome_for_provider_error(error, elapsed_ms=spent, deadline_ms=deadline_ms),
            hits=(),
            release_id=release_id,
            deadline_ms=deadline_ms,
            elapsed_ms=spent,
            degradation=f"{type(error).__name__}: {error}"[:256],
            query_hash=digest,
        )

    try:
        with database.transaction(tenant_id=tenant_id) as connection:
            candidates = _top_hits(
                connection,
                release_id=release_id,
                vector=embedding.vector,
                budget_ms=deadline_ms - elapsed(),
            )
    except QueryCanceled:
        return timed_out("scanning the release", release_id)

    # **总用时越界就是 `timeout`**（P2-4）：库那一侧慢下来与供应商慢下来，
    # 对客户是同一件事 —— 这一回合的生成被推迟了。
    #
    # **而这一条今天不可达，是量出来的**：反向探针 M6'（把这个分支整段删掉）
    # **一条用例都没红**。原因是每段语句的预算就是**剩余时间**，所以真正会先发生的是
    # `statement_timeout` 取消语句、走上面那条 `QueryCanceled → timed_out`。
    # **留着它不是凑数**：哪天有人在嵌入与扫描之间加一步而**不给它预算**，
    # 这一条就是那一天唯一还站着的兜底。留痕在这里，免得下一个人把它当成活路径去读。
    spent = elapsed()
    if spent >= deadline_ms:
        return RetrievalResult(
            outcome="timeout",
            hits=(),
            release_id=release_id,
            deadline_ms=deadline_ms,
            elapsed_ms=spent,
            degradation=f"retrieval took {spent}ms over a {deadline_ms}ms budget",
            query_hash=digest,
        )

    # **压额度在这里做，而不是在管线里**（ADR-0135）：这样「送了几条」与
    # 「检出几条」在**同一个对象**上，写证据行的那一处不必再算一遍 ——
    # 而算两遍是「同一件事两处各写一遍」那一族的入口。
    #
    # 额度（`KNOWLEDGE_PROMPT_BYTES`）是**提示词**的预算，本模块只消费它，
    # 不在这里定：它与 ADR-0132 §八 那张表里的那一行是同一个数。
    #
    # **交给上界的不是 `hits`**：那一份里没有正文（`_Candidate` 写了为什么），
    # 而第一版直接传了它 —— 门禁上 12 条红在同一个 `KeyError: 'text'` 上。
    # 顺序照旧是 `<=>` 的相关性顺序，上界只按这个顺序往里装。
    hits = tuple(candidate.evidence for candidate in candidates)
    bounded = bound_for_prompt(
        [{"ref": candidate.evidence["ref"], "text": candidate.text} for candidate in candidates]
    )
    return RetrievalResult(
        outcome="hit" if hits else "empty",
        hits=hits,
        release_id=release_id,
        deadline_ms=deadline_ms,
        elapsed_ms=spent,
        degradation=None,
        query_hash=digest,
        sent_chunks=bounded.chunks,
        dropped=bounded.dropped,
    )


def record(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    correlation_id: UUID,
    result: RetrievalResult,
) -> UUID:
    """把一次检索写成一行 `retrieval_evidence`，返回行的 id。

    **一回合一行**（表上的 `UNIQUE (tenant_id, correlation_id)`）：裁定三把决定放在
    计划步，而计划步一回合跑一次。**这也是「没有行」能被当成一个可数事实的前提** ——
    「本轮没请求检索」是一次反连接，不是一个 `WHERE`。

    **而「同一回合被重投」与「一回合检两次」在这条唯一约束上长得一模一样**，
    所以这里 `ON CONFLICT … DO NOTHING`：重投不该把回合打死（P1-1）。
    返回的 `row_id` 在「这一次没插进去」时**指向一行不存在的记录** ——
    调用方不读它（`KnowledgeRetriever.run` 丢掉返回值），而写成 `None` 会让
    类型变成 `UUID | None` 并让每个将来的调用方都要处理一个它不关心的分支。

    **裁定四那三列这里一个都不写**（`materials_available` / `quoted_side` /
    `retrieved_unused`）：它们是生成之后才知道的，归 6c。
    `056` 发的是**列级** `INSERT`，所以这一点不靠作者记得 —— 数据库拦得住。
    """

    row_id = uuid4()
    connection.execute(
        "INSERT INTO retrieval_evidence ("
        "  tenant_id, id, conversation_id, correlation_id,"
        "  query_hash, release_id, retriever_version,"
        "  outcome, deadline_ms, elapsed_ms, degradation, hits, hits_sent"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        # **`DO NOTHING`，而且留第一次那一行**（独立审查 2026-09-12，**P1-1**）。
        # `correlation_id` 取自信封，**重投时一模一样**：第一次跑到检索之后、
        # 回合收口之前出任何事（`AggregateConflict`、worker 挂掉、租约到期），
        # 事件被重新 claim → 重跑 plan → 再写一次这一行 → `UniqueViolation`
        # 一路穿过管线，`MAX_INBOX_ATTEMPTS` 之后这条入站事件终态失败，
        # **客户一条消息都收不到**。那把裁定六反过来了：检索**成功**之后把整轮打死。
        # 留第一次那行而不是覆盖：它记的是真的发生过的那一次
        # （elapsed / release / hits 都是那一次的事实）。
        " ON CONFLICT (tenant_id, correlation_id) DO NOTHING",
        (
            tenant_id,
            row_id,
            conversation_id,
            correlation_id,
            result.query_hash,
            result.release_id,
            RETRIEVER_VERSION,
            result.outcome,
            result.deadline_ms,
            result.elapsed_ms,
            result.degradation,
            Jsonb(list(result.hits)),
            # **命中为空时写 `NULL` 而不是 0**（`061` 的列注释）：
            # 「这一轮没走到生成」与「送了 0 条」不是一件事，
            # 而后者在 `hits` 非空时才有意义。
            result.sent if result.hits else None,
        ),
    )
    return row_id

#: 嵌入调用在成本台账里的角色名。
#:
#: **与 `embedding.py` 抛异常时用的那个 `role="EMBEDDING"` 是同一个串** ——
#: 那一处先有（第 4 批），这一批只是让台账也认它。迁移 `059` 把它加进
#: `model_invocation_costs.role` 的闭合词表。
EMBEDDING_COST_ROLE = "EMBEDDING"


def embedding_cost_binding(release: ModelPolicyRelease) -> RoleBinding:
    """把活着那一版的 `embedding` 块包成一个 `RoleBinding`。

    **为什么是适配而不是给 `CostGovernor` 加一条新路**：上限判定、预留、结算、
    对账、`ceilings_cny` 全都写在那一条路上（`cost.py` 八百行里的大半），
    再开一条等于把同一件事做两遍 —— 而本仓为「同一件事两处各写一遍」付过多次钱。
    `RoleBinding` 是一个纯 dataclass，它的定价属性只读 `pricing` 那个 dict，
    所以**包一层就够**。

    三个值得说清的字段：

    * **`max_output_tokens=0`** —— 嵌入没有输出 token。写 `0` 而不是 `None`
      是有后果的：`CostGovernor.enforcement_status` 对 `None` 返回
      「不可判上限」，于是嵌入调用会绕过上限。**0 是事实，也让它受上限管**。
    * **`pricing={"input": …, "output": 0.0}`** —— 扁平价目，
      `RoleBinding.cost_cny` 走的是 `"input_uncached" not in pricing` 那一支，
      `pricing_period` 返回 `"flat"`（`003` 的 CHECK 里有这个值）。
      价是 release 里 `embedding.pricing.text_input`（用户提供的豆包价目）。
    * **`failure_action="degrade"`** —— 裁定六：检索失败不停这一回合。
      这个串**只被人读**（没有任何代码按它分支），写在这里是为了让读到这个绑定的人
      知道它的失败语义与三个模型角色不同。
    """

    document = release.document["embedding"]
    pricing = document["pricing"]
    return RoleBinding(
        role=EMBEDDING_COST_ROLE,
        provider_id=str(document["provider_id"]),
        model_id=str(document["model_id"]),
        api_base=str(document["api_base"]),
        temperature=None,
        # `embedding.py` 的 docstring 逐字写过：`/embeddings` 直接 400，
        # **只有 `/embeddings/multimodal` 答** —— 它不是 openai 兼容那一支。
        api_compatibility="volcengine-multimodal-embeddings-v1",
        structured_output="none",
        region=str(document["region"]),
        context_limit_tokens=None,
        deadline_ms=RETRIEVAL_DEADLINE_MS,
        max_output_tokens=0,
        fallback=(),
        failure_action="degrade",
        pricing={"input": float(pricing["text_input"]), "output": 0.0},
        thinking_mode=None,
        output_contract=None,
    )


class KnowledgeRetriever:
    """检索这一步的全部：算钱、检、写证据。**一个回合调一次。**

    **它自己开事务写那一行证据**，不等调用方的事务收口。理由是裁定六的那半条：
    审计行是「失败不停回合」的另一半，**一条被后面某一步回滚掉的审计行，
    留下的正是那条裁定要消灭的不可见状态**。
    """

    def __init__(
        self,
        *,
        database: Database,
        embedder: Embedder,
        release: ModelPolicyRelease,
        governor: CostGovernor | None = None,
    ) -> None:
        self._database = database
        self._embedder = embedder
        self._binding = embedding_cost_binding(release)
        self._governor = governor

    def run(self, *, context: CallContext, query: str) -> RetrievalResult:
        """检一次并把结果写成一行 `retrieval_evidence`，返回那个结果。

        **这一层的承诺是「不把这一回合带走」，而它对库侧异常也成立**
        （独立审查 2026-09-12，**P2-1**）。先前模块 docstring 声称
        「每一条失败路径都不抛」，**而实现只吃掉了 `ProviderError`** ——
        `record()` 的唯一约束、向量维度对不上、语句超时、连接断、RLS 拒绝，
        每一个都会穿过这里、穿过 `pipeline._retrieve_if_requested`（那里没有 try），
        把回合打死。裁定六说检索是加分项不是必需品，
        **而一个加分项当时有能力停掉必需品**。

        所以这里分两段兜：
        * **检索本身**炸了 → 记成 `failed` + 一条指名异常类型的 `degradation`，
          再试着把它写下来；
        * **审计行都写不进去** → 只剩下计数器
          （`metrics.RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL`）。
          **这里不再往外抛**：写不下审计行是运维要知道的事，
          而让客户这一轮收不到消息不是它的代价。
        """

        try:
            result = self._retrieve(context=context, query=query)
        except PsycopgError as error:
            result = RetrievalResult(
                outcome="failed",
                hits=(),
                release_id=None,
                deadline_ms=RETRIEVAL_DEADLINE_MS,
                elapsed_ms=0,
                degradation=f"{type(error).__name__}: {error}"[:256],
                query_hash=query_hash(query.strip()),
            )
        try:
            with self._database.transaction(tenant_id=context.tenant_id) as connection:
                record(
                    connection,
                    tenant_id=context.tenant_id,
                    conversation_id=context.conversation_id,
                    correlation_id=context.correlation_id,
                    result=result,
                )
        except PsycopgError:
            RETRIEVAL_EVIDENCE_WRITE_FAILED_TOTAL.increment(result.outcome)
        return result

    def _retrieve(self, *, context: CallContext, query: str) -> RetrievalResult:
        if self._governor is None:
            # mock 供应商那条路：没有台账，也不会有真实调用（`cli.py` 只在
            # `model_provider != "mock"` 时给检索器）。留这一支是为了让
            # 用例可以在不起台账的情况下验检索本身。
            return retrieve(
                self._database,
                tenant_id=context.tenant_id,
                query=query,
                embedder=self._embedder,
            )

        metered = _MeteredEmbedder(
            embedder=self._embedder,
            governor=self._governor,
            binding=self._binding,
            context=context,
        )
        result = retrieve(
            self._database,
            tenant_id=context.tenant_id,
            query=query,
            embedder=metered,
        )
        if metered.denial is not None:
            # **上限拒绝在这里变成一条降级，而不是一个异常**（裁定六）。
            # `retrieve` 看到的是一个 `ProviderError`，所以它已经把 outcome 定成
            # `failed`；这里只把 `degradation` 换成说得清的那一句 ——
            # 「被上限拒了」与「供应商挂了」该修的地方不同。
            return replace(result, degradation=metered.denial)
        return result


class ProviderCostCeiling(ProviderError):
    """上限拒绝，**在检索这条路上不出这个回合**。

    为什么不复用 `cost.CostCeilingExceeded`：那个类是管线的失败路径之一
    （`pipeline` 捕获它并走 `_persist_no_send`），而检索被拒**不该**走那条路。
    走 `ProviderError` 这一支，`retrieve` 现成的那段「按跑了多久判 timeout / failed」
    就把它记成 `failed`，而 `degradation` 由上层换成指名上限的那一句。
    """

    reason_code = "RETRIEVAL_COST_CEILING"


@dataclass
class _MeteredEmbedder:
    """把台账的三步夹在嵌入调用两侧：`estimate → reserve`、调用、`settle / mark_failed`。

    **形状照 `providers.complete` 抄**（那一处是三个模型角色的同一件事），
    唯一的差别是上限拒绝**不抛到回合外面** —— 它记在 `denial` 上，
    由 `KnowledgeRetriever._retrieve` 翻译成 `degradation`。
    """

    embedder: Embedder
    governor: CostGovernor
    binding: RoleBinding
    context: CallContext
    denial: str | None = None

    def embed_one(self, text: str, *, timeout_seconds: float) -> Embedding:
        estimated_input, estimated_output, estimated_cost = self.governor.estimate(
            binding=self.binding, system_prompt=text, user_prompt=""
        )
        occurred_at = datetime.now(UTC)
        decision, row_id = self.governor.reserve(
            binding=self.binding,
            context=self.context,
            estimated_input=estimated_input,
            estimated_output=estimated_output,
            estimated_cost=estimated_cost,
            # **这一列在这里没有提示词可填**，而它 NOT NULL。填检索器的版本 ——
            # 这一列答的是「这一行的请求是由哪一版的东西造出来的」，
            # 而对一次嵌入调用来说那就是检索器。`059` 把这句话写进了列注释。
            prompt_release=RETRIEVER_VERSION,
            occurred_at=occurred_at,
        )
        if decision.is_deny:
            self.denial = (
                f"refused by {decision.reason_code} ({decision.window}): "
                f"projected {decision.projected} over {decision.limit}"
            )[:256]
            # **那一行留在 `estimated`，与 `providers.complete` 逐字同形。**
            #
            # 本批第一版在这里调了 `mark_failed`，理由是「这次调用确定没发生，
            # 留一串不会结算的行会顶爆未结算计数闸」。**门禁把它拦下来了**
            # （`test_nothing_in_production_ever_marks_a_reservation_failed`），
            # 而那条绊线是对的：**`failed` 断言的是「供应商没向我们计费」，
            # 而台账里没有任何供应商侧标识，没有人可问** —— 计数闸的阈值正是
            # 按「这笔钱一直看不见」推出来的。
            #
            # 而我那条理由方向也错了：**能拒掉一次嵌入调用的上限，一定也拒得掉
            # 同一回合里估值大它两个数量级的模型调用**，所以「检索被拒而模型调用照跑」
            # 几乎不可达。接住这些行的是 `reconcile_abandoned`（T55 / ADR-0040）：
            # 900 秒后它们变成 `unreconciled` —— 「不知道有没有被计费」，
            # 那才是诚实的状态。**一条可判的未计费在这张表上没有值可填**，
            # 这件事登记在 T315 的附带观察里。
            raise ProviderCostCeiling(self.denial, role=EMBEDDING_COST_ROLE)

        try:
            embedding = self.embedder.embed_one(text, timeout_seconds=timeout_seconds)
        except ProviderError:
            # **不动那一行**（与 `providers.complete` 同）：一次 502 完全可能**已经被计费**，
            # 而 `failed` 的意思是「没被计费」。判它需要供应商侧的标识，台账里没有。
            # 它按既有那条路走：`reconcile_abandoned` 到期把它记成 `unreconciled`。
            raise

        if row_id is not None:
            self.governor.settle(
                row_id=row_id,
                context=self.context,
                binding=self.binding,
                input_tokens=embedding.prompt_tokens,
                # **嵌入没有输出 token**，而这不是「没量到」——是零。
                output_tokens=0,
                occurred_at=occurred_at,
            )
        return embedding

def record_generation_side(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    correlation_id: UUID,
    materials_available: bool,
    quoted_side: str,
    retrieved_unused: int,
    cited_refs: Sequence[str],
) -> None:
    """裁定四那三列 + 引用集，**生成之后**写回同一行。

    **形状是「共现 + 用了哪边」，不是「冲突」**（裁定四 §5.2.2 原话）：
    平台记得下「两种来源同时可用」与「模型引了哪一边」，
    **记不下「两者说的是不是同一件事」** —— 判断两段自然语言矛不矛盾要理解它们，
    那是模型的活，**平台把它当成一次判断去做，就是在没有来源的情况下生成一个事实结论**，
    正是整套机制要禁的那件事。

    **三列一起写**（表上那条 CHECK 要求它们同生同灭）：两列有值一列空是一次
    部分写入，而**一次部分写入在这张表里读起来像一次真实测量**。

    **`quoted_side` 在两边都引了时记 `material`**：那是**客户真正看到的字**
    （素材由平台在发送路径上逐字换进去），而知识库只是模型自己那段话的依据。
    裁定四的「素材优先」在这一列上的读法就是这个。

    **`cited_refs` 是三跳追溯的第一跳**（`062`，出口判据 §6.1 第 4 条）：
    `retrieved_unused` 只答「剩几条没被引」，**答不了引了哪一条** ——
    而「上周那句话指回哪一段原文」问的正是后者。`050` 的 `hits` 列注释
    自己写着「a citation from last week must still resolve to **the passage that
    was actually quoted**」，而在这一列之前，库里没有任何东西记得下 *which*。
    四列一起写（`062` 的 CHECK 要它与那三列同生同灭）。
    """

    connection.execute(
        "UPDATE retrieval_evidence"
        "   SET materials_available = %s, quoted_side = %s, retrieved_unused = %s,"
        "       cited_refs = %s"
        " WHERE tenant_id = %s AND correlation_id = %s",
        (
            materials_available,
            quoted_side,
            retrieved_unused,
            # **升序去重**：这一列是一个集合，而一个顺序不定的数组会让
            # 「两行记的是不是同一件事」在读的时候变成一次比对练习。
            Jsonb(sorted(set(cited_refs))),
            tenant_id,
            correlation_id,
        ),
    )
