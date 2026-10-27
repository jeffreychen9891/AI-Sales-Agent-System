"""`generation_constraints` 的两张档：**哪些码会被执行，哪些码只是嘱咐。**

## 为什么要分档（T333 / T235）

配包在 `strategies.*.generation_constraints` 里写下的码，今天分成两种，
**而在数据里它们长得一模一样**：

* 有执行者的（`validate_generation` 里有一条分支读它）——写下去真的会拦东西；
* 没有执行者的——写下去只进提示词，是给模型的**嘱咐**。

`docs/handoffs/internal-demo-profile-2026-09-10/handoff.md:295-302` 早就把这两档
分清楚过了，**但那份区分只活在一份交接文档里**：配包作者看不到它，
schema 也不知道它（`generation_constraints` 是 `{"type":"array","items":{"type":"string"}}`，
**没有 `enum`**）。于是实测三份在架配包声明了 **8** 个码，
而 `validate_generation` 只认 **5** 个 —— **拼错一个码与发明一个码完全不可分辨**
（`no_new_commitmnet` 静默失效，而写配包的人以为自己加了一道约束）。

## 为什么档在平台这一侧，不在配包里（用户 2026-09-13 定形状）

**不是「配包里加一个字段说明这条只是嘱咐」**，两个理由：

1. 那是一次**契约变更**（要改 schema 加改三份配包）；两张表只改代码侧，
   **配包一个字都不用动**；
2. 更根本的：**嘱咐档的成员资格归平台，不归配包。**
   配包若能自己声明「我这条只是嘱咐」，**它就能把一条真约束降级成嘱咐** ——
   而那正是这条判据要防的东西。

## 这两张表改变了「差集」这个动作的含义

在它们之前，唯一可做的差集是「配包声明的 − 有执行者的」，
**而那个差集会红在 6 处有意的嘱咐上**（`current_goal_only` 在三份配包的
`advance_goal` 与 `propose_followup` 上，共 6 处）——
一个红在有意行为上的门禁，会被下一个人加进豁免名单，然后连真问题一起放过。

分档之后差集才问得出真正的那一问：**这个码，平台认不认识它？**

## ⚠️ 嘱咐档**不是垃圾桶**：配包侧那条检查红着，是正确状态

**进嘱咐档的条件很窄**（见 `ADVISORY_CONSTRAINTS` 的注释）：
**要么今天不可机器判，要么它要限制的东西还不存在。**

**「本该有执行者而还没写」的码不进这张表** ——
它应当让配包侧那条检查**红着**，直到有人给它写执行者，
或者把它从配包里删掉。

> **红着是正确状态，不是待修复的状态。**
> 看到那条检查红了，第一反应会是「把这个码加进嘱咐档」——
> **那一下就把这张表变成了垃圾桶**：任何没实现的约束往里一扔，检查就绿了。
>
> **而那恰好是这整件事要防的东西的镜像**：
> 原来的病是「配包里的码没人执行」，
> 扔进嘱咐档之后的病是「**这个码被归到嘱咐档，所以不用执行**」——
> 同一个洞，换了个说法，而且这次是绿的。

**判据**：往这张表里加一个码之前，先答一句
「**它为什么今天不可能有执行者**」。答案是「还没人写」时，**不要加**。

**今天就有一个这样的码，点名在这里，免得下一个人从零判断一次**：

* **`approved_material_only`** —— 它**不进嘱咐档**。
  依据：它写于 2026-09-03（建包那一笔），而 2026-09-10 落地的
  `must_quote_material` 就是同一个意图的实现版（检查 payload 里引了哪些素材），
  **只是没人回头改那份配包**。所以它属于「有替代、该改过去」（T333 ③），
  **不属于「不可机器判」也不属于「要限制的东西还不存在」**。
  **在它被改掉之前，配包侧那条检查红在它上面是预期状态。**

**这条点名有执行者，不只是一句话**：
`tests/contracts/test_constraint_vocabulary.py::
test_the_code_that_needs_an_executor_is_in_neither_table` ——
**把它加进任何一张表的那天，那条用例就红**。
（写在这里是给读代码的人一个解释，**而挡住那个动作的是那条断言** ——
改的人不会先读 docstring。）

## 不改提示词，也不动字节底

嘱咐档的成员**继续原样进 plan 提示词**（`allowed_generation_constraints_by_strategy`
取的是配包声明的那张清单）——它们的用途就是让模型看见
（`handoff.md` 的原话：「只是嘱咐（进提示词，没有校验器）」）。
**所以本批不改提示词组装，`PLATFORM_PROMPT_BYTES` 与请求底线都不动。**
"""

from __future__ import annotations

#: 有执行者的码 —— **这张表的每一项，`_validate_constraints` 里都有一条分支读它。**
#:
#: 两个方向都有守卫（`tests/contracts/test_constraint_vocabulary.py`）：
#: 表里多一个而实现里没有 → 红；实现里多一条分支而表里没有 → 红。
#: **后者是这张表唯一会悄悄过期的方向** —— 2026-09-10 加
#: `must_quote_material` 那一次就是：它是第五个有执行者的码，
#: 而当时没有任何东西记下这件事（T235 的到期条件写的正是这一刻，
#: 而那条到期条件是散文，没有检测方，于是四天没人知道）。
ENFORCED_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "evidence_required",
        "no_new_commitment",
        "one_question",
        "confirmation_only",
        "must_quote_material",
    }
)

#: 只是嘱咐的码 —— **进提示词，没有校验器，而这是有意的。**
#:
#: 进这张表的条件很窄：**它要么今天不可机器判，要么它要限制的东西还不存在。**
#: 一个「本该有执行者而还没写」的码**不进这张表** —— 它应当让那条检查红着，
#: 直到有人给它写执行者或把它从配包里删掉。
#:
#: 三个成员各自的理由（逐个查过，T333 的归类表里有依据）：
#:
#: * ``current_goal_only`` —— 生成这一层**没有换帧能力**，
#:   所以它在那一层只能是文字层面的意思，今天不可机器判。
#:   计划层那一半已经有闸了（`ADVANCE_GOAL_MUST_STAY_IN_FRAME`）。
#: * ``no_new_price_promise`` —— `no_new_commitment` **严格更强**（禁一切承诺，
#:   不只价格），而写配包的人当年只写了价格那一条。
#:   **用户 2026-09-13 裁定留在嘱咐档**：换成更严的那条，
#:   等于替当年那个人做了一个他没做的决定。**在嘱咐档里它至少是诚实的。**
#: * ``pricing_policy_only`` —— 它要限制的那份定价策略在系统里**还不存在**：
#:   配包里只有 `policy_bindings.pricing = "policy:carrier-fixed-pricing:v1"`
#:   这么一个字符串，而**全仓没有任何东西解析 `policy_bindings`**（T335）。
#:   **它与上面两个的区别**：上面两个是我方这一侧的取舍，
#:   这一个是「要限制的东西还没建」。
ADVISORY_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "current_goal_only",
        "no_new_price_promise",
        "pricing_policy_only",
    }
)

#: 平台认识的全部码。配包写下一个不在这里面的，就是拼错或自造。
KNOWN_CONSTRAINTS: frozenset[str] = ENFORCED_CONSTRAINTS | ADVISORY_CONSTRAINTS


def is_enforced(code: str) -> bool:
    """这个码会被 `validate_generation` 执行吗。

    **给读代码的人一个比 `in` 更明确的问法** —— `code in ENFORCED_CONSTRAINTS`
    与 `code in KNOWN_CONSTRAINTS` 写错一个字就变成另一件事，而两者都不报错。
    """

    return code in ENFORCED_CONSTRAINTS


def unknown_constraints(codes: object) -> tuple[str, ...]:
    """`codes` 里平台不认识的那些，排序返回。

    `object` 而不是 `Iterable[str]`：**它读的是配包里的值**，
    而那一侧的形状由 schema 保证到「字符串数组」为止
    —— 一份手改过的 profile 可以送进来任何东西，
    而这个函数的答案在那种情形下也必须有意义（「它不认识」）。
    """

    if not isinstance(codes, (list, tuple)):
        return ()
    return tuple(sorted({str(code) for code in codes} - KNOWN_CONSTRAINTS))
