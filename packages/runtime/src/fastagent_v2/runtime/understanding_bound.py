"""`understanding` 进提示词之前要过的那个字节上界（ADR-0132，T282 到期）。

## 为什么需要它

`understanding` 的大小由**模型输出**决定，而它每个回合都在，且与素材抢同一个
`MAX_MODEL_INPUT_BYTES`。素材那一侧有 admission、有预算、有报告器；
在这个模块出现之前，**这一侧连一个数都没有**。

T282 写的是「没有上界」。**那句话不准确，而准确的版本更难看**：契约里已经有
`facts.maxItems = 64`、`slot_updates.maxItems = 32` 与逐字段 `maxLength`，
所以上界存在 —— 实测（线路口径）：

| | 字节 | 对 16,384（旧上限） | **对 49,000（今天）** |
|---|---:|---:|---:|
| 契约允许的最大 · 全 ASCII | 47,855 | 2.92 × | **0.98 ×** |
| **契约允许的最大 · 全中文** | **113,967** | 6.96 × | **2.33 ×** |

**右边那一列是 2026-09-16 补的，而补它是因为左边那一列的结论已经变了一半**：
预算链那一批（`763b477b`）把 `MAX_MODEL_INPUT_BYTES` 从 16,384 抬到 49,000，
**全 ASCII 那一行因此从「2.92 倍」变成「装得下」** ——
这张表原样留着的话，它支撑的下一句话（「可以单独把请求撑到上限的 N 倍」）
在两行里只剩一行成立。**中文那一行仍然成立，而且仍然是这个模块存在的理由。**
（复核 2026-09-15「成本闸 §五」列的三处失真数之一，本笔逐条处置。）

**这两个数是用本模块现在这把尺子量的。** 第一版这张表写的是 44,775 / 110,887，
那是被本模块自己废掉的那把尺子量的（独立复审 2026-09-12，N-2）——
**一个以「哪把尺子」为全部主题的模块，引用的读数不该是旧尺子量的。**

**「合法但全空」那一行删掉了。** 它先前写 413，而同一个量在本仓出现过
**四个值**（`material_selection.py` 记 395、本模块记 413、两个审查窗口分别量到
340 与 467）—— 因为「全空」不是一份确定的文档：必填的标量字段填什么，
它就是多少。**换成一个确定的量并由用例守着**：
真正的地板是「可丢字段全空 + 不可丢字段顶到契约上限」，
实测 **513（ASCII）/ 769（中文）**，由
`test_the_floor_is_the_droppable_ones_empty_plus_the_undroppable_ones_maxed` 钉住。

**一份完全合法、过得了全部契约校验的 understanding，可以单独把请求撑到上限的
2.3 倍**（全中文；旧上限下那一栏见上表），而没有任何东西会拒它。
**倍数变小不等于问题变小**：这个模块要保证的是「无论模型吐出多大的一份，
这个函数都终止在 1,580 以内」，而那个保证的证明在下面，与上限多大无关。

## 丢弃，不是截断，也不是拒绝（ADR-0132 §三）

* **截断字符串**会得到一条**看起来合法而意思变了**的事实 —— 下游分不出
  「客户只说了一半」和「我们砍了一半」；
* **整轮拒绝**的失败原因是模型输出太长，**而那是商家改不了的东西**，
  会变成客户侧的一次沉默；
* **整条丢弃**的降级是可解释的：少了几条低置信度的条目，而**留下的每一条都完整**。

## 丢弃顺序，以及为什么是这个顺序

`_DROP_ORDER` 从最先被丢的排到最后。判据是「**写这一句回复有多需要它**」：

1. `uncertainties` —— 模型自己的犹豫。**它是第二大的字段**（最大情形 37,681，
   与 `facts` 的 41,289 同一个量级），而 T282 原文一个字没提它。
   一条只盯 `facts` 的上界会在这里原样重演；
2. `customer_commitments` / `intents` / `facts` —— 有 `confidence`，低的先丢；
3. `slot_updates` —— **最后才丢，但它必须可丢**。不可丢的话，光是它顶到
   `maxItems` 就有 14,000 字节，**这个函数就给不出任何保证**。
   丢它只丢掉模型在提示词里的可见性：**槽位本身由 pipeline 从它自己那份副本写库**
   （`pipeline.py` 的 `slot_updates=list(understanding["slot_updates"])`），
   与这里丢不丢无关。

**不丢的**：`schema_version` / `event_id` / `interruption` / `sentiment` /
`model_evidence`，以及 **`risk_markers`** —— 最后这个是安全信号，
**丢一条安全标记去省几十个字节，是拿错方向换预算**。

`risk_markers` 顶到 `maxItems` 只有 8 条 —— 它是地板里最大的一项，而地板仍然是 769。

## 它给得出保证，而保证是可证的

把所有可丢字段清空之后剩下的是**真正的地板**：可丢字段全空 + 不可丢字段顶到
契约上限，实测 **513（ASCII）/ 769（中文）**，**低于 1,580**，
所以这个函数一定终止在限内。

**第一版这里用的是「合法但全空」那一档（413），而那是两份不同的文档**
（独立复审 N-2 / 第一轮 P3-1）：地板里不可丢的那些是**顶格**的，不是空的。
保证本来就成立，**而写下来的证明用的是另一份文档**。
`test_understanding_bound.py` 拿**契约允许的最大**（全中文）走一遍，
断言结果在限内 —— 那条用例在本模块存在之前是红的。

## 与 ADR-0132 §三 的两处偏离，**写明而不是悄悄改**

ADR §三 写的是「丢弃顺序按 `confidence` 升序，同分按文档内出现顺序」。
本模块做的是**先按字段优先级，只在字段内部按 confidence**，
且同分取**倒序**（后来的先丢）。两处都与 ADR 的字面不同：

1. **字段优先级在前**：`uncertainties` 没有 `confidence`，一条全局升序会把它
   按 0.0 一档整体先丢 —— 结果恰好一样，但那是**碰巧**，而按字段排是**有意的**；
2. **同分倒序**：让「最早提到的那条活得最久」。

**ADR §三 自己标了「作者提议，可被推翻」，所以这是可以推翻的** ——
而 `AGENTS.md`「图纸优先级」第 3 条说实现不得反过来解释图纸，
**所以偏离要留痕**：这一节就是那个痕，自审 §8.3 也记了一条。

## 丢了必须留痕

理由与裁定六的审计行完全同形：**没有它，「模型只给了 2 条事实」和
「模型给了 40 条而平台丢了 38 条」在数据里长得一模一样**，
而这两件事该修的地方不同（一个修提示词，一个修这个上界或那个预算）。

本模块只负责**说清丢了什么**（返回 `dropped`）；记到哪里是调用方的事 ——
`inference_model` 把它计进 `metrics.UNDERSTANDING_ENTRIES_DROPPED_TOTAL`，
**按 `stage` 分标签** —— plan 与 generate 各过一次上界，
不分标签的话那个计数器读到的是真实丢弃数的两倍，
而它存在的全部理由是「模型给了 40 条而平台丢了 38 条」那道算术题。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastagent_v2.runtime.prompt_bytes import document_cost

#: 一个回合的 `understanding` 在提示词里最多占多少**线路**字节。
#:
#: **这个数不是这里拍的**：它就是 `worker.material_selection.MATERIAL_BYTES_BUDGET`
#: 的推导式里减掉的那一项。两处写两遍、由
#: `test_the_bound_is_the_number_the_budget_subtracts` 钉死相等 ——
#: 否则预算表会按 1,580 分配，而执行者按另一个数放行，
#: **那正是「账本与闸不是同一把尺子」的第三次发作**（前两次见
#: `runtime/prompt_bytes.py` 顶部）。
UNDERSTANDING_PROMPT_BYTES = 1580

#: 可丢的字段，**从最先丢到最后丢**。理由见模块 docstring。
_DROP_ORDER: tuple[str, ...] = (
    "uncertainties",
    "customer_commitments",
    "intents",
    "facts",
    "slot_updates",
)

#: 一条都不丢的字段。`risk_markers` 在这里是有意的：它是安全信号。
_NEVER_DROPPED = frozenset(
    {"schema_version", "event_id", "interruption", "sentiment", "model_evidence",
     "risk_markers"}
)

#: **根本不进这个投影的字段。**
#:
#: `turn_summary` 是理解步的**写侧**产出（T200，2026-09-20）：管线拿它往
#: `conversation_summaries` 追一行，而 plan / generate 看到的是滚动摘要那一份
#: （`runtime.summary_prompt`，2,400 字节的独立份额，里面已经含着这一轮这一句）。
#:
#: **不摘掉会付两次账**，而且贵的是这一侧：合同允许它 480 **字符**，
#: 中文就是 1,440 字节 —— 顶着这个上界，本函数的地板会是 **2,233**，
#: 而上界是 1,580。**那时这个函数给不出它 docstring 里承诺的那条保证了**
#: （「不存在丢无可丢仍然超限的输入」）。
#:
#: ⚠️ **摘在这里，不摘在调用方**：第一版摘在 `inference_model` 里，
#: 于是这个函数**对任何一份合同内的文档都不再成立**，只是恰好没人那样调它 ——
#: 而 `test_the_floor_is_the_droppable_ones_empty_plus_the_undroppable_ones_maxed`
#: 当场把这件事报了出来。**保证归谁，谁自己守。**
_NOT_IN_PROMPT = frozenset({"turn_summary"})


@dataclass(frozen=True)
class BoundedUnderstanding:
    """过完上界的文档，以及**每个字段丢了几条**。

    `dropped` 为空 dict 表示一条都没丢 —— **不是 `None`**，
    因为调用方要能无条件地把它记进计数器而不先判空。
    """

    document: dict[str, Any]
    dropped: Mapping[str, int]

    @property
    def anything_dropped(self) -> bool:
        return bool(self.dropped)


#: 标记一条待删项。用哨兵而不是直接 `pop(index)`，因为边遍历边删下标会漂。
_TOMBSTONE = object()


def wire_bytes(document: Mapping[str, Any]) -> int:
    """这份文档进提示词要占的字节，**尺子是 `prompt_bytes.document_cost`**。

    **第一版在这里自己写了一个 `json.dumps`，而它两层都不对**
    （独立审查 2026-09-12，P1-1）：`understanding` 不在 payload 那一层，
    它在 **`user_prompt` 那个字符串里面**，所以要过两层 ——
    `inference_model` 那层 `ensure_ascii=False` 且**不传 `separators`**
    （默认 `", "`），再作为字符串值被转义一次（每个 `"` 变 `\\"`）。

    第一版两点都错，实测偏差 **1.075× – 1.163×，方向 fail-open**：
    `test_the_largest_legal_understanding_fits_after_bounding[x]` **是绿的**，
    而它 bound 出来的文档量成 1,431 ≤ 1,580，**真实进请求 1,601**。

    **本模块不再有自己的尺子。** 这个函数只是个别名，留着是因为
    「understanding 占多少字节」读起来比 `document_cost` 具体。
    """

    return document_cost(document)


def bound_for_prompt(
    document: Mapping[str, Any], *, limit: int = UNDERSTANDING_PROMPT_BYTES
) -> BoundedUnderstanding:
    """把 `document` 压到 `limit` 以内，**整条丢弃，不截断**。

    没超限时走的是一次浅拷贝就返回；浅拷贝让调用方改返回值时不会碰到原文档
    （pipeline 手上那一份要拿去写槽位）。

    **先前这里写着「绝大多数回合是这条路」。实测之后那句话是假的**
    （独立审查 2026-09-12，P2-1）—— 口径改对之后：

    | 文档形状（每个数组 n 条，中文 8 字一项） | 真实字节 | |
    |---|---:|---|
    | n = 1 | 1,074 | 过 |
    | **n = 2** | **1,797** | **超，丢 3 条** |
    | n = 3 | 2,520 | 超，丢 9 条 |

    **「每个数组 2 条」是一个完全普通的回合，而它已经要丢。**
    所以这个上界**不是兜底，是一条会经常走到的路** ——
    在有真实分布之前，按「会经常触发」看待它（计数器就是为这件事，T302）。
    """

    document = {
        key: value for key, value in document.items() if key not in _NOT_IN_PROMPT
    }
    if wire_bytes(document) <= limit:
        return BoundedUnderstanding(document=dict(document), dropped={})

    working = {key: list(value) if isinstance(value, list) else value
               for key, value in document.items()}
    dropped: dict[str, int] = {}

    for field in _DROP_ORDER:
        assert field not in _NEVER_DROPPED, f"{field} 同时出现在两张表里"
        original = working.get(field)
        if not isinstance(original, list) or not original:
            continue
        marked = list(original)
        for index in _drop_order_within(original):
            marked[index] = _TOMBSTONE
            dropped[field] = dropped.get(field, 0) + 1
            working[field] = [item for item in marked if item is not _TOMBSTONE]
            if wire_bytes(working) <= limit:
                return BoundedUnderstanding(document=working, dropped=dropped)

    # 到这里说明可丢的都丢光了。剩下的是「合法但全空」那一档（实测 413），
    # **所以这不是一条失败路径，是循环的自然终点** —— 而它是这个函数
    # 给得出保证的理由：不存在「丢无可丢仍然超限」的输入。
    return BoundedUnderstanding(document=working, dropped=dropped)


def _drop_order_within(items: Sequence[Any]) -> list[int]:
    """一个字段内部的丢弃顺序：**低 `confidence` 先丢；没有 `confidence` 的后进先丢**。

    后者（`uncertainties` 是唯一一个）取逆序而不是正序，
    是为了让**最早提到的那条活得最久** —— 与「低置信度先丢」同一个直觉。

    两种情况都必须是**确定的**：同一份输入两次跑要给出同一个请求，
    否则同一个回合重放会得到两个不同的 payload。
    """

    scored = [
        (item.get("confidence") if isinstance(item, Mapping) else None, position)
        for position, item in enumerate(items)
    ]
    if all(score is None for score, _ in scored):
        return list(range(len(items) - 1, -1, -1))
    return [
        position
        for _, position in sorted(
            scored, key=lambda pair: (pair[0] is not None, pair[0] or 0.0, -pair[1])
        )
    ]
