"""这通对话聊过什么 —— **以及它在提示词里占多少字节。**

## 这个模块同时是两件事，而它们不可拆

`ADR-0122 §三` 写死了一条继承项：

> **谁把历史 / 摘要写进提示词，谁在同一批里建上下文预算。**
> 写死，不得再写成任何一种自指。

所以本模块既装摘要的拼装，也装它的预算常数。**分成两个模块是可以的，
分成两批不行** —— 而放在一起的理由是：预算是拼装的参数，
一个不读预算的拼装函数正是那条继承项要防的东西。

⚠️ **ADR-0122 §6.1 会因为本批而失效，这件事是它自己写的**：

> 谁把历史写进提示词，谁就让这段实测失效

那一节量的是「同一条会话跑到第 13 个回合，PLAN 的每次输入没有累积」。
**本批之后它会累积** —— 累积到 `SUMMARY_PROMPT_BYTES` 为止，然后停住。
ADR-0122 §四.2「不追认 M1b 的上下文安全」同时到期。

## 摘要是「每轮一句」，不是「整份重写」

**裁定八（用户 2026-09-12）**：摘要人工可编辑，**模型不能自改** ——
模型只能追加，不能重写已有的那一份。

这条规则有两种写法，而**只有一种不需要额外的闸**：

| 写法 | 裁定八靠什么成立 |
|---|---|
| 模型每轮输出整份摘要 | 靠一道比对新旧两份文本的闸 —— **而它说不出改了哪里** |
| **模型每轮输出「这一轮发生了什么」一句** | **靠结构**：模型根本拿不到已有的那些行，想改也改不到 |

取后者（用户 2026-09-20 裁定）。**滚动**那一半是读取侧的事：
`summary_for_prompt` 取**最近的、装得下的**那几行，旧的落在预算外。
⚠️ **落在预算外不等于被删** —— 行还在库里，把预算调大，旧行就回来了。
与 `memory_lifetime` 那一批同一条纪律：**能在读取侧做的事，不要写进不可逆的行状态。**

## 装不下的要数出来，不要安静地少给

与 `memory_prompt.memories_for_prompt` 的 `memories_not_shown` 逐字同源：
**一个安静截断的上下文，与一段本来就没发生过的对话，在提示词里长得一样。**
"""

from __future__ import annotations

from collections.abc import Sequence

#: 摘要在一个提示词里占多少字节。**用户 2026-09-20 裁定 2,400。**
#:
#: 它与同族那几个份额并排（都在各自模块里）：
#:
#: | 份额 | 字节 |
#: |---|---:|
#: | `knowledge_bound.KNOWLEDGE_PROMPT_BYTES` | 1,826 |
#: | `understanding_bound.UNDERSTANDING_PROMPT_BYTES` | 1,580 |
#: | `memory_prompt.REMEMBERED_BYTES` | 1,200 |
#: | **本条** | **2,400** |
#:
#: **量级对照**：`MAX_MODEL_INPUT_BYTES = 49,000`，而 ADR-0122 §六 实测
#: 31 次真实调用的**实际**入参是 1,670–2,219 token。2,400 字节按一行 120 字节
#: 算装得下约 **20 轮**，而本仓自己的注释写着「一通销售会话的长度以十条计」
#: （`pipeline.CUSTOMER_NUMBER_EVENTS` 那一段）——**所以它是一个量级以上的余量，
#: 不是一个刚好够的数**。
#:
#: ⚠️ **这个数变大时要重读 ADR-0122 §四.2**：单请求上限
#: `MAX_MODEL_INPUT_BYTES` 是一个合同常数，不是按模型窗口算出来的。
SUMMARY_PROMPT_BYTES = 2_400

#: 一行摘要最多多少字节。
#:
#: **与迁移 `071` 里那条 `CHECK (length(line) BETWEEN 1 AND 480)` 是一个数**，
#: `test_the_conversation_remembers_what_was_said.py` 把两处钉成相等。
#:
#: ⚠️ **两处的单位不同，而这是有意的**：这里是**字节**，迁移里是
#: PostgreSQL 的 `length()`，那是**字符**。一行 480 字符的中文是 1,440 字节，
#: 所以**库那一侧更宽** —— 真正的闸在这一侧，库那条是兜底。
#: 反过来（库更严）会让一条合法的行写不进去，而那时报错来自数据库、
#: 不来自校验器，排查要绕一圈。
SUMMARY_LINE_BYTES = 480


def clip_line(line: str) -> str:
    """把一行摘要裁到 `SUMMARY_LINE_BYTES`，**按字节裁，不按字符**。

    `str[:n]` 裁的是字符，而预算是字节 —— 本仓为这个差别付过一次钱
    （`byte-budget` 那一批：把字符当字节，一份三条中文声明就超了整个请求预算）。
    """

    encoded = line.encode("utf-8")
    if len(encoded) <= SUMMARY_LINE_BYTES:
        return line
    return encoded[:SUMMARY_LINE_BYTES].decode("utf-8", errors="ignore")


def summary_for_prompt(
    lines: Sequence[str],
    *,
    budget: int = SUMMARY_PROMPT_BYTES,
) -> tuple[tuple[str, ...], int]:
    """装得下的那几行（**按时间正序**），以及装不下的那几行有多少。

    `lines` 按**时间正序**给（最旧的在前），本函数**从最新的一端往回收**，
    返回时仍是正序 —— 读它的人看到的是一段按发生顺序排的话。

    ## 为什么淘汰最旧的那一端

    一通对话里「他刚说了什么」比「他二十轮前说了什么」更能决定这一句怎么回。
    ⚠️ **而这不是无代价的**：开场那几轮（他是谁、为什么来）恰恰在最旧的一端。
    **今天接受这个代价**，因为把「哪几行更该留」做成判断就是检索问题，
    而排期表 §4.4「不做」那一栏逐字写着**第一版不做检索排序**。

    ## 返回两个值，而不是一个

    第二个是**丢了几行**。与 `memories_for_prompt` 的 `memories_not_shown`
    同一条理由：**一个安静截断的上下文，与一段本来就没发生过的对话，
    在提示词里长得一模一样** —— 而模型会照着后者去问一件它其实已经问过的事。
    """

    if budget <= 0:
        return (), len(lines)

    kept: list[str] = []
    used = 0
    #: 行与行之间的分隔符也要记账 —— 与 `prompt_bytes.ENTRY_SEPARATOR_BYTES`
    #: 同一条理由：**一项开销真的在付，而它不属于任何一格账**
    #: （`AGENTS.md` 判据 2 的反面）就是没有人算它的那一笔。
    separator = 1
    for line in reversed(lines):
        cost = len(line.encode("utf-8")) + (separator if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return tuple(kept), len(lines) - len(kept)
