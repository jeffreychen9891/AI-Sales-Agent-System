"""一个回合送进提示词的知识库命中，要压在额度以内 —— **整条丢弃，不截断**。

**ADR-0135（用户 2026-09-12）**定的形状，而它的来处是一次前置测量：

| 哪个「最坏」 | 一条 | top-2 |
|---|---:|---:|
| 今天语料的最坏（55 条真实 chunk 现量） | 879 | **1,740** |
| **契约允许**的最坏（`050` 的 CHECK 允许 `ref + body ≤ 880`） | **911** | **1,822** |
| 带 `score` + `source_spans` | 1,010 | 2,024 |

**先前的额度 1,800 坐在前两者中间**：覆盖今天的语料，**不覆盖合同允许的东西**。
而一个只覆盖今天语料的额度意味着 —— 下一个作者在合同允许的范围内写一条顶格 chunk，
就能让某一回合超版，**而超版的表现是 `ProviderInputTooLarge` → 回落保守计划 →
客户这一轮收不到消息**（2026-09-11 运维报障走过的正是这条路）。

**根因是一个代理数**：`050` 那条 CHECK 里的 `20` 是「一条 chunk 在提示词里的额外开销」
的代理，而实测是 **31**（两层 JSON 的引号与转义）—— 短了 11。
**DB 收得下不等于提示词装得下，而两处从来没有人对过。**

## 三条形状裁定（ADR-0135）

1. **只发 `{ref, text}`。** `score` 与 `source_spans` 不进提示词：分数对模型没有意义
   （让它看见分数等于请它按分数说话），而三跳追溯靠的是 `retrieval_evidence.hits`
   里那份 span 拷贝（`050` 的列注释：抄进来而不是 join，就是为了活过 chunk 被 supersede）。
2. **整条丢弃，不截断正文。** 截一半的 chunk 是一条会让模型引错的来源 ——
   与 `understanding_bound` 那条「整条丢弃、留下的每一条都完整」同一个理由。
3. **丢掉的那条必须可数。** 返回值带 `dropped`，调用方把它写进
   `retrieval_evidence.hits_sent`；没有那一列的话，「检出 2 条只送了 1 条」
   与「只检出 1 条」在数据里长得一模一样 —— 而前者该重推预算表，后者该去补语料。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastagent_v2.runtime.prompt_bytes import entry_cost

#: 一个回合的知识库命中在提示词里最多占多少**线路**字节。
#:
#: **1,826 不是凑的整数**：契约允许的最坏是 **1,822**（`ref + body ≤ 880` 再加两层
#: JSON 的 31，两条），余下的 4 是它与额度之间的全部空隙。
#: **这个数与 ADR-0132 §八 那张预算表里的那一行是同一个数**，而那张表因此
#: **从剩 188 变成剩 0** —— ADR-0135 第一版写的是 1,850 / 剩 138，
#: 漏算了「把知识库接进提示词」本身那 162（提示词一行 136 + 空键在线路上 26
#: —— 原始 24，外面那一层再把两个引号各转义一次），
#: 它从同一个池子里出。**池子满了**：下一个往任何提示词里加字的人先重推那张表。
#:
#: **两处写两遍、由用例现量现比**（`test_the_budget_covers_what_the_contract_allows`）：
#: 额度按**契约允许**的最坏算，不是按今天语料的最坏 —— 后者是 ADR-0135 要修订的那个读法。
KNOWLEDGE_PROMPT_BYTES = 1826


@dataclass(frozen=True, slots=True)
class BoundedKnowledge:
    """压过之后的命中，以及**丢了几条**。

    `dropped` 不是日志，是一列数据（`retrieval_evidence.hits_sent` 的另一半）——
    见模块 docstring 第 3 条。
    """

    chunks: tuple[dict[str, str], ...]
    dropped: int

    @property
    def sent(self) -> int:
        return len(self.chunks)


def wire_bytes(chunks: Sequence[Mapping[str, Any]]) -> int:
    """这些命中进提示词要占多少字节，**同一把尺子**（`entry_cost`，与素材同口径）。

    `entry_cost` 数的是一个 `{ref, text}` 在 `user_prompt` 那个字符串里的真实开销
    （两层：`inference_model.generate` 的 `json.dumps` 不传 `separators`，
    外面 `providers` 那一层再把它当字符串值转义一次）。
    **不在这里写第二个 `json.dumps`** —— 尺子只有 `runtime.prompt_bytes` 一处。
    """

    return sum(entry_cost(str(chunk["ref"]), str(chunk["text"])) for chunk in chunks)


def bound_for_prompt(
    hits: Sequence[Mapping[str, Any]], *, limit: int = KNOWLEDGE_PROMPT_BYTES
) -> BoundedKnowledge:
    """把命中压到 `limit` 以内：**按顺序留，装不下的整条丢**。

    `hits` 的顺序就是相关性顺序（检索层按 `<=>` 升序取的 top-2），
    所以「装不下就丢后面那条」丢掉的是**较不相关**的那一条 —— 这一点不是巧合，
    是它能这么简单的全部理由。**这里不重排、不打分、不截断。**

    只取 `ref` 与 `text` 两个键（ADR-0135 第 1 条）：多带的字段
    （`score` / `source_spans` / `chunk_id`）在这里就被挡住，
    **而不是靠提示词那一层记得不要塞进去**。
    """

    kept: list[dict[str, str]] = []
    used = 0
    for index, hit in enumerate(hits):
        chunk = {"ref": str(hit["ref"]), "text": str(hit["text"])}
        # **走 `wire_bytes`，不直接调 `entry_cost`**（独立审查 2026-09-12 P3-2）：
        # 先前这一行自己调 `entry_cost`，于是 `wire_bytes` 在生产里零调用方 ——
        # 只有用例在用它，而本仓对零消费方有登记纪律。
        # 现在「这些 chunk 要多少字节」全仓只有一个答法，
        # 与 `understanding_bound` 那一份同形（那里的实现也调自己的 `wire_bytes`）。
        cost = wire_bytes([chunk])
        if used + cost > limit:
            # **到这里就停，不去试后面那条。**
            #
            # 顺序是相关性顺序，**跳过一条去装更小的下一条等于让「装得下」决定
            # 送哪一条** —— 那是一次没人看得见的相关性重排，而相关性是检索层
            # 用 `<=>` 判过的，不该被一个字节预算悄悄改写。
            #
            # 第一版这里写的是 `continue`，**而注释写的就是现在这句话** ——
            # 代码与注释打架，改的是代码。
            return BoundedKnowledge(chunks=tuple(kept), dropped=len(hits) - index)
        kept.append(chunk)
        used += cost
    return BoundedKnowledge(chunks=tuple(kept), dropped=0)
