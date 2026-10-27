"""把记下来的那几件事**带进提示词** —— 记忆这条路上一直缺的那一段。

## 在这个文件之前，记忆是一条断掉的路

`contact_memories` 有表、有迁移（`054`）、有 RLS、有控制面路由、
有一条写入路径（`remember` 意图）、有一道封闭词表的闸（`_declared_memory_fields`）。
**而值从来没有回到过任何一个提示词里。**

`pipeline._remembered_memories`（本批改名前叫 `_remembered_fields`）的 docstring
甚至把这件事写成了一条原则：

> **只取键不取值。** 要答的是「这个字段已经有值了吗」（裁定八），
> 答它不需要值；而值是客户事实，少带一份进内存就少一份写进日志的机会。

那句话在它自己的语境里是对的 —— **写入闸确实只需要键**。
而整条路上除了那道闸，**没有第二个读者** —— 于是「记住了什么」
对模型而言等于没发生过：下一轮它照样不知道这个客户是做什么的。

**两头都在、中间没接。** 本模块是中间那一段。

## 顺序是商家的，不是平台的

带哪几条由 **`memory_fields` 声明的顺序**决定 —— schema 的描述里写着理由：

> a jeweller needs ring size and anniversary, an airline needs document number
> and cabin preference, a tutoring business needs the child's grade and lesson
> times -- three sets with no intersection, and the platform can never guess
> what the next industry needs.

平台猜不出哪一条更重要，**而商家把它写在声明的顺序里**。
按字典序或按写入时间排都是平台在替商家排优先级。

## 装不下的要数出来，不许安静地少给

超预算的那些**不是消失**，而是回一个计数。
一次安静的截断读起来与「这个客户没有记忆」一模一样 ——
而这两件事对模型该做的事完全不同。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: 一条记忆值最多带多少字节。超了截断，**并留下一个可见的省略号**。
#:
#: `contact_memories.field_value` 的 CHECK 允许到 **1024 个字符** ——
#: 中文满格就是 3072 字节，**一条就能把下面那个预算吃光**。
#: 截断而不是整条丢：一条「他们主要做…」的记忆截了尾仍然有用，
#: 而丢掉它等于这一轮没有记忆。
REMEMBERED_VALUE_BYTES = 300

#: 所有记忆值加起来最多带多少字节进**一个**提示词。
#:
#: **与 `InboxProcessor.LAST_OUTBOUND_BYTES` 取同一个数，这是有意的**：
#: 两者都是「为了读懂这一轮而额外背上的上下文」，
#: 而理解步与计划步各收一份 —— **每一轮真实花费是这个数的两倍**。
#:
#: 它与 `MAX_MODEL_INPUT_BYTES = 49_000` 比小两个量级，
#: 所以**不会把哪一轮顶过上限**；而抬高它是一次成本决定，不是一次调参。
REMEMBERED_BYTES = 1_200

#: 截断的记号。**给模型看的**：它要能分得出「这就是全部」与「后面还有」。
ELLIPSIS = "…"

#: 一条记忆当**来源**引用时长什么样（T363，2026-09-19）。
#:
#: **写字那一步要能指着它说「这句话凭这条记忆」** —— 而那要一个 ref。
#: 形状照知识库那一族（`knowledge:<slug>:v<版本>`）：前缀 + 它自己的键。
#:
#: ⚠️ **记忆没有版本号，这是有意的**：`contact_memories` 的一行会被改写
#: （`remember` 再写一次同一个 field_key），而**引用集要答的是
#: 「这条回复凭哪一件事说的」，不是「凭那件事的哪一版」**。
#: 版本那一跳要的是一张历史表，而 `contact_memories` 今天只存当前值 ——
#: 声明一个假的版本号比不声明更糟。**登记在 T363 的状态栏里。**
MEMORY_REF_PREFIX = "memory:"


def memory_ref(field_key: str) -> str:
    """那条记忆的引用 ref。**唯一定义点。**

    三个消费方：生成步的提示词（把它递给模型）、校验器（判模型引的那个
    是不是本轮递过的）、以及留痕（把引用集写下来）。
    **三处各拼一次是这道闸漂掉的方式** —— 与 `TURN_KEY_SQL` 同一条纪律。
    """

    return f"{MEMORY_REF_PREFIX}{field_key}"


def field_key_of(ref: str) -> str:
    """把 ref 还原成 field_key。**留痕记的是键，不是 ref。**

    `inference.memories_sent` 记的就是 field_key（只记键、不记值），
    而引用集那一行要与它**对得上** —— 两行用两种标识，
    「递过去了哪几条」与「引了哪几条」就没法相减。
    """

    return ref[len(MEMORY_REF_PREFIX) :] if ref.startswith(MEMORY_REF_PREFIX) else ref


def _clip(value: str, budget: int = REMEMBERED_VALUE_BYTES) -> str:
    """按**字节**截断，切在多字节中间的那一刀由 `errors="ignore"` 收拾。"""

    encoded = value.encode("utf-8")
    if len(encoded) <= budget:
        return value
    return encoded[:budget].decode("utf-8", errors="ignore") + ELLIPSIS


def remembered_facts_for_prompt(
    shown: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """把 `memories_for_prompt` 的结果转成**生成步**那一份的形状（T363）。

    ## 为什么生成步那一份是 `{ref, value}` 而不是 `{field, value}`

    写字那一步要**引**它，所以它要一个可引的 ref —— 与
    `knowledge_chunks` / `content_materials` 的 `{ref, text}` 同一种记法：
    **「ref 与正文一起给，而 ref 是要模型吐回来的那个」**。

    ⚠️ **而它换了一个键名（`remembered_facts`，不是 `known_memories`）**：
    理解步与计划步那一份是 `{field, value}`，它们不引、只读。
    **同名而不同形状正是 `CLAUDE.md` 第三节点名的那个陷阱**
    （`RetrievalEvidence.release pin` 那一例：字段名没变、含义变了）——
    所以换名字，不换形状。

    **它不自己算一遍**：入参就是 `memories_for_prompt` 的返回值，
    于是「递给模型的是哪几条」在三个提示词上是同一个答案。
    """

    return [
        {"ref": memory_ref(str(entry["field"])), "value": str(entry["value"])}
        for entry in shown
    ]


def memories_for_prompt(
    declared: Sequence[Mapping[str, object]] | None,
    remembered: Mapping[str, str] | Sequence[tuple[str, str]] | None,
    *,
    budget: int = REMEMBERED_BYTES,
) -> tuple[list[dict[str, str]], int]:
    """这一轮带进提示词的记忆，以及**没带上的有几条**。

    ## 只带商家今天还声明着的那些

    与写入闸（`_declared_memory_fields`）**同一份清单**：
    商家把一个字段从 `memory_fields` 里删掉，就是说「这件事我不再想记」——
    那时库里那行还在（它有 TTL、有留痕，删不删是另一条路上的事），
    **而它不该再出现在提示词里**。两处用同一个判据，所以不会一处认一处不认。

    ## 装不下时**停在第一条装不下的**，而不是跳过它继续找小的

    跳过等于「按大小重排优先级」，而优先级是商家用声明顺序定的。
    停下来，剩下的都算「没带上」—— **可预测比多塞一条值钱**。
    """

    values = dict(remembered or {})
    shown: list[dict[str, str]] = []
    spent = 0
    stopped = False
    missing = 0

    for entry in declared or []:
        key = str((entry or {}).get("key") or "")
        if not key or key not in values:
            continue
        if stopped:
            missing += 1
            continue
        text = _clip(str(values[key]))
        # 键、值、以及 JSON 里那一圈引号与逗号 —— 量的是它真的要占多少。
        cost = len((key + text).encode("utf-8")) + len('{"field":"","value":""},')
        if spent + cost > budget:
            stopped = True
            missing += 1
            continue
        shown.append({"field": key, "value": text})
        spent += cost

    return shown, missing
