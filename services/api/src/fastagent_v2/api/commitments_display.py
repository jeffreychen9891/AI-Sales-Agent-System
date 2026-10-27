"""「登记了几条承诺」这一格，两处读模型共用的那一份。

**单独成一个模块，不是洁癖**：`app.py` 一 import 就要 `Settings`（签名密钥等），
而这一格的那条绊线（`tests/contracts/test_commitment_ledger_absence.py`）
问的是「常量与迁移目录说的是不是同一件事」—— **那个问题不需要一个配好的进程**。
放在这里，绊线不必为了读一个布尔值去起半个服务。
"""

from __future__ import annotations

#: 承诺台账（RFC §5）今天**存不存在**。
#:
#: **它是一个会过期的断言，所以它有一条绊线**
#: （`tests/contracts/test_commitment_ledger_absence.py`）：
#: 只要迁移目录里出现建 `commitments` 表的那一条，那条用例就红，并指着这里说「改我」。
#: **AGENTS.md「图纸优先级」第 6 条**要的就是这个 —— 凡断言「某物今天不存在」，
#: 要么带坐标与计数，要么带一条会替你重核的用例。
#:
#: **为什么这个常量值得存在**：读模型有两处要显示「登记了几条承诺」
#: （详情页与接管期投影）。台账不存在时它们只能显示 `0`，
#: **而 `0` 与「这一轮确实没有承诺」在屏幕上长得一模一样** ——
#: 用户 2026-09-13 点名：那是一个**会误导人的真值**，该显示的是「未启用」。
#: 图纸那半句（ADR-0054 / ADR-0076 §四.1、`control-plane-parity.md:171`
#: 「必须显式显示「未登记任何承诺」，不得省略」）**一个字不改**：字段照显，
#: 改的是它旁边那句话说的是不是真话。
COMMITMENT_LEDGER_AVAILABLE = True

#: 台账没启用时，两处显示共用的那一段话。**一处写，两处用**
#: （AGENTS.md「每处都要写成同一个形状」那一节）。
COMMITMENT_LEDGER_DISABLED_NOTE = (
    "承诺台账尚未建立（RFC §5 未落地）：这里的 0 表示「这个能力还没有启用」，"
    "不是「这一轮没有任何承诺」。台账落地前，这个位置数不出真实承诺。"
)


def commitment_display(commitment_refs: list[object]) -> dict[str, object]:
    """两处显示（详情页 / 接管期投影）共用的那一份「登记了几条承诺」。

    **`status` 是这一批加的那一半**（用户 2026-09-13）：
    `count` 照旧显示、不省略（图纸那半句不改），而 `status` 说清这个 0 是哪一种 0。
    """

    if not COMMITMENT_LEDGER_AVAILABLE:
        return {
            "count": len(commitment_refs),
            "status": "not_enabled",
            "note": COMMITMENT_LEDGER_DISABLED_NOTE,
        }
    return {
        "count": len(commitment_refs),
        "status": "enabled",
        "note": (
            "这条会话还没有登记任何承诺"
            if not commitment_refs
            else "活着的承诺（已履行 / 已撤回 / 已被取代的不计）"
        ),
    }
