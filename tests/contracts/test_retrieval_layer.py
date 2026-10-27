"""检索层里**不需要数据库也不需要等**的那几条规则。

**裁定六**（`knowledge-base-schedule-2026-09-11.md` §5.3.2）：检索失败**不停掉这一轮的回复**，
照常生成并发消息，另加一条审计行。所以这个模块的失败路径一条都不抛 ——
它们是 `outcome` 与 `degradation`，而这份用例钉的就是「哪种情况记哪一个值」。

DB 那一侧（命中、排序、列级授权、RLS、五条 CHECK）在
`tests/integration/test_knowledge_retrieval.py`。
"""

from __future__ import annotations

from typing import Any, Never
from uuid import UUID

import pytest
from fastagent_v2.worker.inference_validation import is_usable_query, requested_knowledge_query
from fastagent_v2.worker.providers import (
    ProviderHttpError,
    ProviderTimeout,
)
from fastagent_v2.worker.retrieval import (
    QUERY_MAX_CHARS,
    RETRIEVAL_DEADLINE_MS,
    TOP_K,
    outcome_for_provider_error,
    query_hash,
    retrieve,
)

TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _DatabaseThatMustNotBeUsed:
    """一个会炸的替身**库**（`retrieve` 现在收的是 `Database`，不是连接）。

    **它是断言的一部分，不是夹具的方便**：不可用的请求必须在开事务、
    更在花掉一次嵌入调用**之前**就返回。用一个「被用到就炸」的替身，
    「早返回」这件事才是被证明的，而不是被相信的。
    """

    def transaction(self, *args: Any, **kwargs: Any) -> Never:
        raise AssertionError("不可用的请求不该碰数据库")


class _EmbedderThatMustNotBeUsed:
    def embed_one(self, text: str, *, timeout_seconds: float) -> Never:
        raise AssertionError("不可用的请求不该花一次嵌入调用")


@pytest.mark.parametrize(
    ("query", "why"),
    [
        ("", "空串"),
        ("   ", "只有空格"),
        ("\n\t ", "只有空白字符"),
        ("查" * (QUERY_MAX_CHARS + 1), "超出长度上界一个字"),
    ],
)
def test_an_unusable_request_is_recorded_not_raised(query: str, why: str) -> None:
    """**四种不可用的请求，四种都记成一行 `unusable_request`，一条都不抛。**

    这一态是 ADR-0133 加进 `outcome` 词表的（用户 2026-09-12 裁定 (b)）。
    它与 `failed` 分开，因为**两者该修的地方不同**：这一态修提示词，
    `failed` 修检索层或供应商 —— 与 `050` 把 `timeout` 从 `failed` 里拆出来同一条判据。
    """

    result = retrieve(
        _DatabaseThatMustNotBeUsed(),  # type: ignore[arg-type]
        tenant_id=TENANT,
        query=query,
        embedder=_EmbedderThatMustNotBeUsed(),
    )

    assert result.outcome == "unusable_request", why
    assert result.hits == ()
    assert result.release_id is None, "没去检就不许记读过哪一版（056 的那条 CHECK）"
    assert result.degradation, "「为什么不可用」是这一态唯一的信息量"
    # **hash 量的是 `stripped`**（P3-1：一个查询串只有一个 hash）。
    assert result.query_hash == query_hash(query.strip())
    assert result.deadline_ms == RETRIEVAL_DEADLINE_MS


def test_the_blank_rule_is_the_same_one_the_plan_reader_uses() -> None:
    """**空白的判据只有一处**，两个调用方共用 `is_usable_query`。

    没有这一条，下一个人在检索层写一个 `.strip()`、在读法里写另一个，
    两处慢慢分家 —— 而它们分家的那一天，
    「模型没请求检索」与「请求了但平台读成没请求」在数据里长得一模一样。
    """

    assert not is_usable_query("   ")
    assert is_usable_query(" 运费 ")
    # 读法那一侧的同一条判据（6a 落的）：不可用 → 读成「这一轮没请求」。
    assert requested_knowledge_query({"knowledge_query": "   "}) is None
    assert requested_knowledge_query({"knowledge_query": " 运费 "}) == "运费"


def test_a_query_exactly_at_the_bound_is_still_usable() -> None:
    """界是「超过」才不可用，**不是「达到」** —— 边界两侧各钉一次。"""

    at_bound = "查" * QUERY_MAX_CHARS

    # **那个会炸的连接就是断言**：刚好在界上的请求不被判成不可用，于是它继续往下走、
    # 去读 release —— 而连接一被碰就抛。**抛出来才是这条用例的绿。**
    with pytest.raises(AssertionError, match="不该碰数据库"):
        retrieve(
            _DatabaseThatMustNotBeUsed(),  # type: ignore[arg-type]
            tenant_id=TENANT,
            query=at_bound,
            embedder=_EmbedderThatMustNotBeUsed(),
        )


def test_a_provider_timeout_that_did_not_reach_the_deadline_is_failed_not_timeout() -> None:
    """**判据是跑了多久，不是异常叫什么名字。**

    `050` 的 CHECK (c) 与它的注释逐字裁过这件事：「说超时，却没跑到期限」是脏行，
    而**供应商侧几毫秒就返回的那种超时错误该记 `failed`** ——
    两者该修的地方不同（一个调期限或检索层，一个查供应商/网络），
    混在一个值里正是裁定六要拆开的那件事。
    """

    fast_timeout = ProviderTimeout("read timeout after 5ms", role="EMBEDDING")
    assert (
        outcome_for_provider_error(fast_timeout, elapsed_ms=5, deadline_ms=2000) == "failed"
    )
    assert (
        outcome_for_provider_error(fast_timeout, elapsed_ms=2000, deadline_ms=2000) == "timeout"
    ), "跑到期限了才算 timeout —— 等于也算"

    http = ProviderHttpError("502 bad gateway", role="EMBEDDING")
    assert (
        outcome_for_provider_error(http, elapsed_ms=9999, deadline_ms=2000) == "failed"
    ), "跑得再久，不是超时类的错就不是 timeout"


def test_the_top_k_is_the_ruling_not_a_preference() -> None:
    """裁定八：**top-2**。写成常量而不是字面量，好让改它的人看见它有来处。"""

    assert TOP_K == 2


def test_the_query_hash_is_what_goes_to_the_audit_row() -> None:
    """落库落 hash 不落原文（`050` 的列注释）：查询串是从客户内容派生的。

    顺带钉住它是 sha256 的十六进制 —— 表上那条
    `CHECK (query_hash ~ '^[0-9a-f]{64}$')` 就是照这个形状写的。
    """

    digest = query_hash("七天无理由退货怎么算运费")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest != query_hash("七天无理由退货怎么算运费 ")
