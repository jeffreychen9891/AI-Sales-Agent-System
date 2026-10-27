"""Platform strategy catalog and target-stack operation catalog.

These are platform-owned enumerations. A merchant profile may only *reference*
entries from `STRATEGY_CATALOG`; it can neither add a strategy nor redefine what
one means. Adding an entry here requires an RFC/ADR plus contract tests, per the
conversation-engine RFC section 4.1.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

STRATEGY_CATALOG: tuple[str, ...] = (
    "acknowledge",
    "answer_question",
    "ask_slot",
    "ask_clarification",
    "advance_goal",
    "offer_material",
    "confirm_resume",
    "wait",
    "close_goal",
    "propose_followup",
    "propose_quote",
    "propose_order",
    "handoff",
    "no_message",
)

#: Strategies the platform may select on its own after rejecting a model plan.
#: They are deterministic safety valves, so they are not subject to the target
#: frame allowlist; every other strategy is.
#:
#: **`ask_clarification` 是 2026-09-15（D4）加的第四个**，而它补的是一个真实缺口：
#: 图纸 RFC:515 给的三个安全回退是「**ask clarification** / no_message / handoff」，
#: 而实现里第一个是 `ask_slot` —— **两者不是同一个能力**。
#: `ask_slot` 要求**点得出一个已声明的槽位**（T339 第二批，用户裁 (c)：
#: 「点得出 -> `ask_slot`，点不出 -> `no_message`」），
#: 于是**那 59 个点不出槽位名的理由码，今天只能静默或转人工** ——
#: 没有「请客户澄清一句」这个选项。
#:
#: **它与 T339 认定的坏形状的区别要说清**，因为从外面看两者长得一样：
#: T339 坏在 `ask_slot` **声称在问某个槽位而不知道是哪个** —— 说的和做的不一致；
#: `ask_clarification` **不声称在问任何具体的东西**，
#: 「我没听清，你能再说一遍吗」本身是完整的、诚实的一句话。
#:
#: **它不朝配包**：安全回退不受帧 allowlist 约束（就是上面那句），
#: 所以在架配包一个都不用改；商家将来想给它配自己的措辞，
#: 走 `content_materials` 那条既有的路（生成步已有明文：素材为空就写自己的话）。
SAFE_FALLBACK_STRATEGIES: tuple[str, ...] = (
    "ask_slot",
    "ask_clarification",
    "no_message",
    "handoff",
)

#: Strategies that must never produce customer-visible content.
SILENT_STRATEGIES: tuple[str, ...] = ("wait", "no_message")

#: 只许在**当前目标帧内**推进的打法 —— 选了它，栈操作只能是 `keep`。
#:
#: **图纸原句**（`conversation-engine.md` §4.1；ADR-0125 §七 明确「`advance_goal`
#: 的边界一个字未改」）：「它不能切换目标、跳过必填槽位、构造未登记承诺或直接发送。」
#: 「只允许在当前目标帧内」与「不能切换目标」在实现上是同一件事：
#: 换帧的每一种写法（`push` / `repair` / `request_pop` / `complete` / `abandon`）
#: 都不是「在当前帧内」。执行它的是
#: `inference_validation._validate_stack_operation`，理由码
#: `ADVANCE_GOAL_MUST_STAY_IN_FRAME`。
#:
#: **今天只有一个成员，而写成一张表不是排场**：「这条打法不许换目标」是
#: **策略的性质**，而策略词表的唯一写入点是本模块 ——
#: 在校验器里写一个 `== "advance_goal"` 的字面量，等于在第二处定义一条策略的性质，
#: 而本仓为「同一件事写两处」付过账（`AGENTS.md`「单一写入点 + 绊线」）。
#: 哪天图纸给别的打法加同一条边界，改的是这一行，不是校验器。
IN_FRAME_ONLY_STRATEGIES: tuple[str, ...] = ("advance_goal",)


def time_rule_strategy_set(definition: Mapping[str, Any]) -> tuple[str, ...]:
    """Which strategies this frame's `time_rules` may declare. **ADR-0116.**

    **Omitting the field means `allowed_strategy_set`**, which is why the
    default is computed here rather than written into every profile: a frame
    that does not declare it behaves exactly as it did before the fork existed,
    and five of the six shipped rules need no change at all.

    The fork exists because one list was answering two questions. It only
    started to matter when `strategy` got its first reader (B4-4): a follow-up
    play a rule wants at the 24-hour mark is not necessarily a play the model
    should be offered while the customer is mid-sentence -- and before the fork,
    permitting the first meant permitting the second.

    **This is not a way past `profile["strategies"]`.** Registration stays a
    hard precondition; what this widens is which frame may use a play, never
    whether the profile knows it.
    """

    declared = definition.get("time_rule_strategy_set")
    if declared is None:
        declared = definition["allowed_strategy_set"]
    return tuple(str(item) for item in declared)


TARGET_STACK_OPERATIONS: tuple[str, ...] = (
    "keep",
    "push",
    "repair",
    "request_pop",
    "complete",
    "abandon",
)

INTENT_KINDS: tuple[str, ...] = (
    "send_message",
    "send_material",
    "remember",
    "schedule_event",
    "cancel_event",
    "reschedule_event",
    "quote",
    "create_order",
    "update_order",
    "followup",
    "handoff",
    "manual_task",
)

SCHEDULED_EVENT_KINDS: tuple[str, ...] = (
    "silence_check",
    "goal_stall",
    "followup_due",
    "customer_promise_due",
    "dormant_reengagement",
    "business_event_followup",
    "ai_commitment_due",
)
