"""`policy_decisions` 的**唯一写入点**，以及那五个字段的封闭词表。

**裁定二（ADR-0137 §二，用户 2026-09-13）取的是折中 ③：只统一输出，不改结构。**
图纸（`conversation-engine.md` §3.4 / §8）把 policy 定成一个统一对象 ——
六个时点、四值 `effect`、`reason_code` / `modifications` / `obligations` /
`policy_id` / `policy_version`。而实现不是那个形状：十二个固定 hook 里只有
`ProactivityPolicy` 按名字存在，其余的功能（授权、成本、人工接管、渠道能力、投递）
都在，**形态是内联闸**。裁定二不把内联闸收回成 hook 对象，**只统一它们的输出**。

> **【更正 · 2026-09-14】上一段「只有 `ProactivityPolicy` 按名字存在」数错了。**
> 那个 26 是 `git grep -c` 的**子串**命中，其中 17 处来自
> `ProactivityPolicyError`；剩下 9 处全是散文。按 AST 数定义点，
> **十二个名字一个都不是符号**。结论（都不是 hook 对象、功能是内联闸）不变，
> 理由从「十一个零」变成「十二个零」。
> 执行者：`tests/contracts/test_policy_hook_shape.py::`
> `test_none_of_the_twelve_fixed_hooks_exists_as_a_symbol`。

## 它替掉了什么

此前**七处**各自手写同一条 12 列 `INSERT`，每处自带字面量的 hook / effect /
`policy_id` / `policy_version`（`cost.py`、`digest.py`、`pipeline.py` 五处）。
每一处单独看都对，**错的是它们之间的差** —— 而 diff 一次只给你看一处
（`AGENTS.md`「单一写入点 + 绊线」）。

**绊线**：`tests/contracts/test_policy_output.py` 断言全仓
`INSERT INTO policy_decisions` **只许一处**，多于一处即红。

## 守着这个模块的两处是**独守的**，改它的人要知道

反向探针实测 2026-09-13（同一组数字也记在 `AGENTS.md`
「单一写入点 + 绊线」第二个先例那一段，那里是仓内的落点）：

* **绊线** `test_policy_output.py::test_there_is_exactly_one_write_point`
  —— 把那条断言停掉：**全绿**。没有第二个东西看着「只许一处」。
* **逐格比对** `tests/integration/test_policy_decision_write_point.py`
  —— 整份 `skip` 掉，再把 `policy_id` / `policy_version` 两格交换：**还是全绿**。

**这是有意的，不是漏的**：给守卫再加一层看着它会无限递归，而这两条守的东西
（「只许一处」「十三列没错位」）都没有别的自然落点。
**代价是没有第二道保险** —— 删掉、改宽、或把它们 `skip` 掉的那一天，
不会有任何别的用例变红，本文件的「唯一写入点」就退回成一句注释。
所以：动它们之前先问「那件事此后由谁守」，而不是「门禁还绿吗」。

## 为什么词表在这里校验，而不是交给库

`policy_decisions.effect` 有 CHECK，**而 `obligations` 没有** ——
T35 记着那次：`"alert"` 这个不在契约枚举里的值写进去了，**库没拦、也没人读**。
封闭词表放在写入点意味着**不合法的值当场抛**，指着写的人说「这里」；
交给 CHECK 则要等到运行时，而且报错指向的是数据库。
`obligations` 这一列今天连 CHECK 都没有，所以这里是唯一的那道闸。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

#: 六个时点，逐字来自 RFC §3.4 的 `hook` 枚举与 §8 那张表。
#:
#: **`post_understanding` 与 `pre_generation` 今天零写者**，而它们仍然在这里：
#: 词表的边界是图纸定的，不是「今天谁在用」定的。裁定二明写**不新增发出点**
#: （补它们要先答「那两个时点各自要检什么」，那是另一次裁定）。
HOOK_POINTS: tuple[str, ...] = (
    "pre_inference",
    "post_understanding",
    "post_plan",
    "pre_generation",
    "pre_execute",
    "pre_delivery",
)

#: 四个 effect（RFC §3.4）。**`modify` / `manual_review` 今天零发出** ——
#: 前者要的 `modifications: typed bounded patch` 机制不存在，
#: 后者今天没有任何起点（`manual_review` 创建的任务该带 owner/SLA/expiry，
#: 而引擎一个都填不出来，登记 T91）。同上：词表按图纸写全。
EFFECTS: tuple[str, ...] = ("allow", "deny", "modify", "manual_review")

#: 四个 obligation（RFC §3.4）。`require_evidence` 今天零发出。
#: **这一列在库里没有 CHECK**（T35 的另一半），所以这里是唯一拦得住它的地方。
OBLIGATIONS: tuple[str, ...] = ("audit", "redact", "require_evidence", "create_manual_task")


@dataclass(frozen=True, slots=True)
class PolicyOutput:
    """一次 policy 判定的**输出那一半**，五个字段按图纸凑齐。

    **它不是一个 hook 对象**：谁判的、怎么判的、在哪个内联闸里判的，
    本类一概不管（裁定二：不改结构）。它管的是「判完之后落下来的那一行长什么样」。

    不合法的词表值**在构造时就抛**，而不是等到 `INSERT`：
    一个带着 `"alert"` 的 obligations 列表，错在写它的那一行，
    而不是错在数据库。
    """

    hook: str
    effect: str
    reason_code: str
    #: 策略的**名字**（`"cost-governor"` / `"plan-validator"` 这一类），不是行 id。
    #: 行 id 是 `record_policy_decision(row_id=...)` 那个参数 —— 两者同名过一次，
    #: 而 `decision.policy_id` 其实装的是行 id，这里把它们分开叫。
    policy_id: str
    policy_version: str
    obligations: tuple[str, ...]
    context: Mapping[str, Any]
    modifications: Mapping[str, Any] = field(default_factory=dict)
    next_allowed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.hook not in HOOK_POINTS:
            raise ValueError(
                f"{self.hook!r} 不是 RFC §3.4 的六个时点之一：{HOOK_POINTS}"
            )
        if self.effect not in EFFECTS:
            raise ValueError(f"{self.effect!r} 不是四个 effect 之一：{EFFECTS}")
        unknown = [item for item in self.obligations if item not in OBLIGATIONS]
        if unknown:
            raise ValueError(
                f"{unknown} 不在 obligations 的封闭词表里：{OBLIGATIONS}。"
                "这一列在库里没有 CHECK（T35），所以这里拦不住就没人拦得住了"
            )
        if not self.reason_code:
            raise ValueError("reason_code 不能是空串 —— 它是这行唯一可被搜索的东西")


def record_policy_decision(
    connection: Connection[DictRow],
    *,
    tenant_id: UUID,
    row_id: UUID,
    conversation_id: UUID,
    intent_id: UUID | None,
    output: PolicyOutput,
) -> None:
    """把一次判定写成一行 —— **全仓唯一那条 `INSERT INTO policy_decisions`。**

    `intent_id` 可空：拒绝路径上常常还没有意图行（`pre_inference` 的成本拒绝、
    `pre_execute` 的配额拒绝都没有），而 `NULL` 在这张表上的意思就是
    「这一条判定不是针对某一个意图的」。

    **`next_allowed_at` 不在这里判空**：它只有主动性那一族会填（`049` 加的列），
    而「这个 effect 该不该带它」是那一族自己的判断 —— 本函数不替它判，
    否则就从「统一输出」变成「替内联闸做决定」，而那是裁定二不做的那一半。
    """

    connection.execute(
        """
        INSERT INTO policy_decisions(
            tenant_id, id, conversation_id, intent_id, hook, effect, reason_code,
            modifications, obligations, policy_id, policy_version, next_allowed_at,
            context
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            row_id,
            conversation_id,
            intent_id,
            output.hook,
            output.effect,
            output.reason_code,
            Jsonb(dict(output.modifications)),
            Jsonb(list(output.obligations)),
            output.policy_id,
            output.policy_version,
            output.next_allowed_at,
            Jsonb(dict(output.context)),
        ),
    )


def audit_and_redact() -> tuple[str, ...]:
    """`("audit", "redact")` —— 放行客户可见投递那一族用的那两项。

    写成一个函数而不是常量，是为了让**调用点看起来像在声明义务**，
    而不是在引用一个元组；两者行为相同，可读性不同。
    """

    return ("audit", "redact")


def audit_only() -> tuple[str, ...]:
    """`("audit",)` —— 拒绝那一族用的。拒绝没有要脱敏的客户可见内容。"""

    return ("audit",)


def coerce_obligations(values: Sequence[str]) -> tuple[str, ...]:
    """调用点手上已经有一个列表时用它转成元组，顺带过一遍词表。

    存在的理由只有一个：`pipeline._record_proposal_denial` 那一处的 obligations
    是**参数传进来的**（不同拒绝带不同义务），而词表校验不该因此漏掉它。
    """

    unknown = [item for item in values if item not in OBLIGATIONS]
    if unknown:
        raise ValueError(f"{unknown} 不在 obligations 的封闭词表里：{OBLIGATIONS}")
    return tuple(values)
