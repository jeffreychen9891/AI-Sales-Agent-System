"""「这一轮的结果投影」这一格 —— 与 `commitments_display` 同构，同一个理由。

**单独成一个模块，不是洁癖**：与 `commitments_display.py` 一样，
它那条绊线问的是「常量与生产代码说的是不是同一件事」，
**那个问题不需要一个配好的进程** —— `app.py` 一 import 就要 `Settings`。

## 它修的是什么

`outcome_ref`（RFC:150）今天**零写点**：`worker/pipeline.py` 把它初始化成 `None`
之后，全仓没有第二处写它。而 `api/app.py` **有一个读点**，
把它原样投影给客户端 —— 于是**对外那个字段永远是 `null`**。

**客户端分不出两件事**：
「这一轮没有产出结果」与「结果投影这个能力整个还没实现」。
**那正是用户 2026-09-13 对 `commitments` 那个 `0` 的裁定所说的**：
一个**会误导人的真值**。处置一字不差地照抄那次 ——
**字段照显（图纸那半句不改），旁边加一句话说清它是哪一种 `null`。**
"""

from __future__ import annotations

#: 结果投影（RFC:150 的 `outcome_ref` 那一半）今天**有没有写者**。
#:
#: **它是一个会过期的断言，所以它有一条绊线**
#: （`tests/contracts/test_outcome_projection_absence.py`）：
#: 一旦生产代码里出现第二处写 `outcome_ref` 的地方，那条用例就红，
#: 并指着这里说「改我」。**AGENTS.md「图纸优先级」第 6 条**要的就是这个。
#:
#: **绊线走 AST，不走 `grep`** —— 本仓记过那条：`grep` 答不了「有几个符号」
#: （折行的名字看不出是同一个），而注释里提到这个名字会被误报成写点。
OUTCOME_PROJECTION_AVAILABLE = False

#: 没有写者时，那一格旁边的那句话。**一处写，一处用** —— 与承诺那一格同形。
OUTCOME_PROJECTION_DISABLED_NOTE = (
    "结果投影尚未落地（RFC:150 的 `outcome_ref` 今天零写点）："
    "这里的 null 表示「这个能力还没有启用」，不是「这一轮没有产出结果」。"
    "另一头在 §18.2，本轮范围外。"
)


def outcome_display(outcome_ref: object) -> dict[str, object]:
    """详情页那一格：`outcome_ref` 照显，而 `status` 说清这个 null 是哪一种。

    **签名收 `object` 不收 `str | None`**：它读的是聚合文档里的一个值，
    而那份文档来自库里的 JSON —— 类型由契约保证，不由这里断言。
    写成 `str | None` 会让一条坏数据在**显示层**炸掉，
    而显示层不是发现坏数据的地方。
    """

    if not OUTCOME_PROJECTION_AVAILABLE:
        return {
            "outcome_ref": outcome_ref,
            "status": "not_enabled",
            "note": OUTCOME_PROJECTION_DISABLED_NOTE,
        }
    return {
        "outcome_ref": outcome_ref,
        "status": "enabled",
        "note": (
            "no outcome has been projected for this turn"
            if outcome_ref is None
            else "outcome ref only; the outcome record itself is out of M1a"
        ),
    }
