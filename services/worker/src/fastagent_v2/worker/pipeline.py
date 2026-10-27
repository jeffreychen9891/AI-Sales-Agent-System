from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.contracts import (
    SLOT_UPDATE_REFUSED_ACTION,
    UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
    UNKNOWN_ACTOR,
    validate_contract,
)
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.memory_lifetime import (
    live_memories,
    ttl_days_by_field,
)
from fastagent_v2.runtime.memory_prompt import (
    MEMORY_REF_PREFIX,
    field_key_of,
    memories_for_prompt,
    remembered_facts_for_prompt,
)
from fastagent_v2.runtime.memory_values import forbidden_value_reason
from fastagent_v2.runtime.notification import (
    Classification,
    classify,
    manual_task_notice,
)
from fastagent_v2.runtime.queue import InboxPointer, PointerQueue
from fastagent_v2.runtime.references import reference_refusal
from fastagent_v2.runtime.summary_prompt import (
    clip_line,
    summary_for_prompt,
)
from fastagent_v2.worker.channel import (
    ASSET_CONTENT_TYPE,
    FILE_CONTENT_TYPE,
    MATERIAL_REF_SEGMENT_TYPE,
    RECONCILE_BUDGET_SECONDS,
    TEXT_CONTENT_TYPE,
    ChannelAdapter,
    ChannelFactory,
    SendCommand,
    SendReceipt,
    customer_text,
)
from fastagent_v2.worker.commitment_ledger import (
    admit_ai_commitments,
    admit_customer_commitments,
    stored_due_rows,
    write_admitted,
    write_ai_admitted,
)
from fastagent_v2.worker.cost import (
    ESTIMATED_HARD_REASON_CODE,
    HARD_REASON_CODE,
    UNSETTLED_BACKLOG_REASON_CODE,
    CallContext,
    CostCeilingExceeded,
)
from fastagent_v2.worker.inference import InferencePort
from fastagent_v2.worker.inference_validation import (
    ECHOED_MATERIAL_REASON_CODE,
    KNOWLEDGE_REF_PREFIX,
    REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE,
    UNFOUNDED_CUSTOMER_FACT_REASON_CODE,
    UNKEPT_PROMISE_REASON_CODE,
    GenerationRejected,
    ValidatedPlan,
    knowledge_query_as_written,
    plan_or_fallback,
    validate_generation,
)
from fastagent_v2.worker.material_selection import (
    select_assets,
    select_materials,
    superseded_topic_updates,
)
from fastagent_v2.worker.metrics import (
    RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL,
    SILENCE_ANCHOR_MISSING_TOTAL,
)
from fastagent_v2.worker.platform_frames import frame_definitions, materials_for_frame
from fastagent_v2.worker.policy_output import (
    PolicyOutput,
    audit_and_redact,
    audit_only,
    coerce_obligations,
    record_policy_decision,
)
from fastagent_v2.worker.proactivity import (
    QUIET_HOURS_REASON,
    RE_ENGAGEMENT,
    ProactivityPolicyError,
    ProactivityVerdict,
    strategy_for_rule,
)
from fastagent_v2.worker.proactivity import evaluate as evaluate_proactivity
from fastagent_v2.worker.proactivity import knobs as proactivity_knobs
from fastagent_v2.worker.providers import ProviderError
from fastagent_v2.worker.retrieval import (
    KnowledgeRetriever,
    RetrievalResult,
    record_generation_side,
)
from fastagent_v2.worker.target_stack import (
    EXIT_SIGNAL_RELATIONS,
    TERMINAL_FRAME_STATUSES,
    advance_into_frame,
    apply_slot_updates,
    apply_target_stack_operation,
    enter_closing_frame,
    enter_no_goal_frame,
    frame_to_advance_into,
    record_delivered_slots,
    should_enter_closing_frame,
    should_enter_no_goal_frame,
    stack_requires_human_disposition,
    suspend_target_stack_for_human,
)
from fastagent_v2.worker.time_rules import (
    ScheduledEventDraft,
    TimeRuleError,
    active_frame,
    drafts_for_anchor,
)
from psycopg import Error as PsycopgError
from psycopg.types.json import Jsonb

#: How many failed passes an inbox event or an outbox message may take before it
#: stops being retried. A lease alone bounds nothing: it only decides *when* the
#: next identical attempt happens, and with a model provider configured every
#: pass spends money. The terminal state is what makes the loop end.
MAX_INBOX_ATTEMPTS = 5

def _fallback_denial_context(
    *, rejection: Any, plan: ValidatedPlan
) -> dict[str, Any] | None:
    """deny 行的 context：拒绝的现场 + **这一轮被换成了哪个兜底**。

    运维 2026-09-24 顺带②：`ask_clarification` 兜底又被用了一次，而那一行
    deny 的 `context` 是空的 —— STACK_OP 族的 `PlanRejected.context` 本来
    就不带东西，透传等于什么都没说。读审计的人拿着 `strategy_id` 去配包里
    对（配包 1.4.11 的 strategies 现读九个，没有 `ask_clarification`），
    对不上号，**回退来路没有痕**。

    与说话那条路（`_record_plan_refusal` 那处已带 `fallback_strategy`）
    对齐口径：两条路的 deny 都答得出「兜底是谁、它是兜底」。
    `origin` 从计划文档现读（兜底恒为 `safe_fallback`）——
    不写字面量，免得这里与 `safe_fallback_plan` 各说各话。

    纯函数：`rejection is None`（模型自己选了静默）时没有兜底可指，回 `None`。
    """

    if rejection is None:
        return None
    return {
        **dict(rejection.context),
        "fallback_strategy": plan.strategy_id,
        "fallback_origin": str(plan.document.get("origin") or ""),
    }


def _seconds_since(moment: Any) -> float:
    """离那一刻过了多少秒。读不出来就回 `0.0`。

    **`0.0` 在这里是「说不上来」，而它与「刚刚」在这一步里同义** ——
    两者都让模型看到「几乎没等过」，而那正是读不出时最不容易出错的那一侧：
    它会倾向 `wait`，而不是倾向再发一条。
    """

    parsed = _parse_moment(moment)
    if parsed is None:
        return 0.0
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def _parse_moment(moment: Any) -> datetime | None:
    if isinstance(moment, datetime):
        return moment
    if not moment:
        return None
    try:
        return datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
    except ValueError:
        return None


def _without_an_unsendable_ref(
    reading: dict[str, Any], *, offered: Mapping[str, str]
) -> dict[str, Any]:
    """点名了一份这一轮发不出的话术时，**把 ref 摘掉，而这一轮照常说话**。

    用户 2026-09-22 裁定：「**没有素材但是话要说**」——
    素材发不出不等于这一轮闭嘴。所以这里**不拒这一轮**，
    只把那个 ref 摘掉，动作降回 `ask_again`。

    ## 为什么不退回「让平台自己挑一份」

    挑了就是**平台在决定对客户说什么**，而那正是这一批要消灭的「自说自话」。
    `ask_again` 是模型已经表达过的意图（它想再开一次口），
    只是少了它想引的那份东西。

    ## 三种「发不出」在这里是同一种处置

    不存在 / 这个帧没有 / 商家声明它这一轮不可用（`availability` 与
    `emit_kinds` 与 `stages`）—— 三种都表现为「不在 `offered` 里」。
    **前两种是平台做不了，第三种是商家说了不许**，而平台不绕过商家的声明，
    所以处置相同。

    ⚠️ 留痕落在 `action.because` 后面而不是覆盖它：模型说的「为什么这么做」
    仍然是这一轮最值得读的那句话。
    """

    action = reading.get("action")
    if not isinstance(action, dict) or str(action.get("do")) != "send_material":
        return reading
    ref = action.get("material_ref")
    if ref is not None and str(ref) in offered:
        return reading
    action["do"] = "ask_again"
    action["material_ref"] = None
    note = f"（点名的 {ref} 这一轮发不出，已改为再问一次）"
    action["because"] = (str(action.get("because") or "") + note)[:120]
    return reading


#: 静默类定时器的锚点 —— 它们响是**因为什么都没发生**（ADR-0149）。
#:
#: `commitment_due_at` / `business_event_at` **刻意不在里面**：那两个响是因为
#: 一件具体的事到期了，理由已经在那条承诺（或那个事件）里。
SILENCE_ANCHORS = frozenset({"last_outbound_at", "frame_entered_at"})

#: 第四步判「这一轮不说话」（ADR-0149）。**它不是一次失败**：
#: 用户 2026-09-22 裁定「如果没东西说，说明这个时候就不该推进」。
SILENCE_READING_CHOSE_TO_WAIT = "SILENCE_READING_CHOSE_TO_WAIT"
MAX_OUTBOX_ATTEMPTS = 5

#: How long a dispatcher owns an outbox row once it has claimed it, and how much
#: of that lease it must leave unspent.
#:
#: These used to be a literal in two SQL strings, and `RECONCILE_BUDGET_SECONDS`
#: knew nothing about them. The narrow re-review of L1 (2026-09-04) showed what
#: that costs: the turn budget (`runtime_deadline_at`, ~30s from ingress) and the
#: lease are both thirty seconds and the claim normally follows ingress within a
#: second, so a genuine timeout spends nearly the whole lease -- and the two
#: extra seconds the reconcile now takes push the dispatcher past the end of it.
#: Past expiry another dispatcher may claim the same row and **send the message
#: a second time**, which is the exact opposite of what this batch is for. The
#: review's probe saw it happen: a second POST, then a `UniqueViolation` on
#: `delivery_receipts` swallowed by the worker loop's `except Exception`.
#:
#: So the invariant lives here, in one place: **a reconcile must finish before
#: the lease does.** `_reconcile_before_giving_up` clamps its own deadline to
#: `lease_deadline_at - LEASE_SAFETY_MARGIN_SECONDS` -- a process-clock
#: instant derived from a database-clock difference (T183) -- which makes the
#: lease the ceiling and `RECONCILE_BUDGET_SECONDS` only the wish. Widening the
#: lease was
#: the other way to make the arithmetic fit; it would hold every failed row
#: locked for longer, and it would still leave the closing writes unguarded --
#: and it is the guard, not the arithmetic, that makes a lost race safe.
OUTBOX_LEASE_SECONDS = 30

#: The margin is for the writes that come after the query: the receipt insert,
#: the outbox and intent updates. One second is generous for three statements on
#: a local socket, and small enough that the reconcile still gets its whole
#: budget in the ordinary case (a send that failed fast leaves far more than
#: three seconds of lease behind).
LEASE_SAFETY_MARGIN_SECONDS = 1.0

#: How long an inbox event stays claimed. Named rather than inlined now that it
#: is interpolated into SQL (T183): the number and the clock it is measured on
#: belong together, and `OUTBOX_LEASE_SECONDS` above is the shape to match.
INBOX_LEASE_SECONDS = 30

#: Every way the cost governor can refuse a call (ADR-0034 / ADR-0036). They
#: are listed together because the pipeline treats them alike -- each one stops
#: the machine mid-conversation -- while remaining distinct in the record,
#: because they mean three different things to whoever reads it afterwards.
COST_REFUSAL_REASON_CODES = frozenset(
    {
        HARD_REASON_CODE,
        ESTIMATED_HARD_REASON_CODE,
        UNSETTLED_BACKLOG_REASON_CODE,
    }
)

#: Reason codes that hand the conversation to a person. A cost refusal stops the
#: machine mid-conversation; the target-stack dispositions mean the stack has no
#: frame left that can accept work, so the platform has nothing left to drive.
#: How long a queued manager notice stays worth sending. One day: long enough
#: that M1b's first delivery pass finds the backlog alive, short enough that
#: nobody is paged about a conversation from last week. Deliberately **not** the
#: customer runtime deadline, which is thirty seconds (independent review
#: 2026-09-04, F3).
NOTICE_DEADLINE_SECONDS = 24 * 3600

HANDOFF_REASON_CODES = frozenset(
    {
        *COST_REFUSAL_REASON_CODES,
        "ROOT_TARGET_FRAME_COMPLETED",
        "ROOT_TARGET_FRAME_ABANDONED",
        "TARGET_STACK_EMPTY",
    }
)

#: **A conversation born in `human` mode is not on this list, and that is a
#: ruling rather than an omission** (ADR-0077, user 2026-09-06; raised by the
#: B3 #10/#11 independent review as P2-2).
#:
#: `default_control_mode = 'human'` (freeze B3 #10, migration 037) is a third
#: way into human control, and it is unlike the other two: a takeover is
#: pressed by somebody who therefore knows, and a handoff files a task so
#: somebody can be found. A tenant-wide default is **the tenant's own standing
#: configuration**, and the act of setting it is already audited
#: (`CONTROL_POLICY_UPDATED`, both states). Filing a task per conversation
#: would report a tenant's own configuration back to them once per
#: conversation, which turns the one list people actually read into noise.
#:
#: **It holds on two conditions**, both written into ADR-0077 §三: inbound
#: messages are still stored and the conversation is still visible, and the
#: conversation list (freeze B3 #1) must be able to show "human-held and
#: unclaimed" when it ships. If it cannot, this ruling reopens.

#: `UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL` **and everything this block
#: used to say about it now live on the constant** in `runtime.contracts`
#: (independent review 2026-09-06, P3-5). Moving the literal and leaving
#: twenty lines of `#:` behind it left a documentation comment attached to
#: the next constant down, which it does not describe.

#: Reason codes that file a manual task **without** the handoff transition,
#: because the conversation is already where a handoff would put it.
TASK_WITHOUT_HANDOFF_REASON_CODES = frozenset({UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL})

#: 连着被同一条闸拒**两次**就升级出一条人工任务的那些理由码（ADR-0137 裁定一，N=2）。
#:
#: **为什么不是「第一次就建」**：一次同码拒绝可能是这一轮的判断失误；
#: **连续第二次同码说明它不会自己走出来**。而这一族的拒绝路径**不产出出站行**，
#: 所以「不会自己走出来」的形态是**客户每轮收不到东西，而没有任何人被叫醒** ——
#: 那比单次沉默严重得多。
#:
#: **为什么不是把这些码加进上面那两张表**：那两张表是「**每一次**都建任务」，
#: 而那是仓里已经点名过的坏形状（见上面那段注释：客户不停打字就一条任务一条消息）。
#: 这一族要的是「**一个循环一条任务**」，判据在时间上（连续）而不在类别上。
#:
#: **T330 记着这件事的通用形态**：不在任何一张表里的理由码默认落进「完全无声」，
#: 而新增码时没有任何东西提醒加闸的人去看那些表。本族只是给这**一个**码
#: 一个显式落点，不是那道守卫。
LOOP_ESCALATION_REASON_CODES = frozenset(
    {
        "STRATEGY_SKIPS_REQUIRED_SLOT",
        # **5.7 那条「宁可沉默」**（复核 2026-09-18 退回，用户裁定
        # 「拒绝可以，但要有人被告知」）。
        #
        # 它与上面那个同形：拒绝路径**不产出出站行**，于是「不会自己走出来」
        # 的形态就是**客户每轮收不到东西，而没有任何人被叫醒**。
        # 第一版把这个码留在三张表之外 —— 那正是本段注释预告过的
        # 「默认落进完全无声」。
        #
        # **单次那一半不在这张表里**：一次同码拒绝可能只是这一轮的判断失误，
        # 建一条任务是上面点过名的坏形状。单次由
        # `GOVERNANCE_SQL` 的 `unfounded_fact_refusal` 数出来（运维看得见），
        # **连续第二次才叫人**。
        UNFOUNDED_CUSTOMER_FACT_REASON_CODE,
        # **§10.4 判据 2 那条「许了就要这一轮兑现」**（批 F）。
        #
        # 与上面那条同形地落进这张表。**整份拒那一支要求这条消息的每一句
        # 都是一句没兑现的许诺** —— 丢的单位是句，不是段
        # （`drop_promises_this_turn_does_not_keep`）。
        #
        # ⚠️ **先前这里写的是「窄得多」，而那个估计是错的，复核 2026-09-18
        # 退回过一次**：那时丢的单位是**段**，而生产的一段是多句的，
        # 纯文本回合又常常只有一段 —— 于是「丢一段」= 整份拒 = 客户整轮
        # 收不到东西，一点都不窄。**按句丢之后它才真的窄。**
        # 真到了那一步，客户这一轮同样收不到东西 —— 连着两次就叫人。
        UNKEPT_PROMISE_REASON_CODE,
        # **T258 那条「一通对话一种语言」的整份拒**（2026-09-20）。
        #
        # 与上面三条**逐字同形**：拒绝路径不产出出站行，所以「不会自己
        # 走出来」的形态就是**客户每轮收不到东西，而没有任何人被叫醒**。
        #
        # ⚠️ **它落进这张表不是顺手，是上面那条注释预告的那个形状**：
        # 5.7 那个码第一版就被留在三张表之外，复核 2026-09-18 退回过，
        # 用户当时裁的是「**拒绝可以，但要有人被告知**」。
        # T330 记着它的通用形态：**不在任何一张表里的码默认落进完全无声，
        # 而新增码时没有任何东西提醒加闸的人来看这些表。**
        # 本批就是那条登记说的「下一次」—— 差一点又漏了。
        #
        # **单次那一半同样不在这里**：一次判成另一种语言可能只是这一轮的
        # 失误，由 `GOVERNANCE_SQL` 的 `another_language_refusal` 数出来
        # （运维看得见），**连续第二次才叫人**。
        REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE,
    }
)

#: 生成步提名得出、而发送路径今天还不认的那些 kind。
#:
#: **它们不是无效的，是没建**——`GENERATION_SUPPORTED_KINDS` 上面那段注释把
#: 「支持第二个 kind」的真实代价写清楚了（per-kind write path，四处下游为
#: `send_message` 而写）。在那之前，被拒的提案只留下一行 denial 记录，
#: 而 **denial 记录只有查库的人看得见**：客户听见「我把您标记一下」，
#: AI 应了，然后什么都不发生。
#:
#: **用户 2026-09-11 的裁定是「至少派一条待办」**（取甲不取乙）。
#: 这两个 kind 的共同点是它们要求的动作**本来就该由人做**，所以「落一条待办」
#: 不是把它们当成 `send_message` 的降级，而是它们本来的形状。
# **`handoff` 与 `manual_task` 一起收进来，是 ADR-0128 §六 裁的，不是顺带。**
#
# 独立审查 2026-09-11 P2-1 提请裁定：「不做移交」的论证原文是「『我把您标记一下』
# 不是『我把您转人工』」——**那句话对 `manual_task` 成立，而 `handoff` 的语义就是
# 「我把您转人工」**，两者唯一的差别正好是那一批决定不做的那件事。
#
# **用户裁定：两个 kind 都只落待办 + 推通知，不动 `control_mode`、不挂目标栈。**
# 理由是责任边界（ADR-0128 §六）：**系统保证通知送达，不保证有人接手。**
# 挂起等于**替商家决定这通对话交给人了，而人可能根本不在**——那时客户等到的是
# 彻底的沉默，而不挂起时最坏也只是 AI 继续答（它本来也在答）。
#
# **同一条裁定还改了话术那一侧**：AI **不说**「我把您转人工」，改成默默转
# （ADR-0128 §二），所以客户那一侧感受到的是「回复慢了一点」，
# **不是「被许诺了什么然后没下文」**。
TASK_FILING_KINDS = frozenset({"manual_task", "handoff"})

#: 这条待办说的是什么。
#:
#: **与拒绝那条 reason code 分开，是有意的。**
#: `GENERATION_INTENT_KIND_UNSUPPORTED` 说的是「发送路径不认这个 kind」——
#: 那是给读 `policy_decisions` 的人看的，是平台自己的事；
#: 这一条说的是「有人要去做这件事」——那是给拿到待办的同事看的。
#: 把两者合成一个，同事会读到一句在讲实现细节的话。
INTENT_NEEDS_A_PERSON_REASON = "INTENT_KIND_NEEDS_A_PERSON"

#: AI 说了一件**只有人能做**的事（T365，2026-09-19）。
#:
#: 与 `INTENT_NEEDS_A_PERSON_REASON` 是**两个触发点**，形状同源：
#: 那一个是「模型提名了一个平台发不出去的 `kind`」，
#: 这一个是「模型**只是说了**『报价单这边我来安排』，一个 kind 都没提名」。
#: 后者在批 F 之后的结局是**那句话被丢掉** ——
#: 于是客户看不到我们答应过，也看不到我们没答应。
#:
#: 处置照 ADR-0128 §六 已裁的形状：**只落待办 + 推通知，不挂目标栈**。
AI_PROMISED_A_HUMAN_ACTION_REASON = "AI_PROMISED_A_HUMAN_ACTION"

#: 发送路径拒掉一个它不认的 kind 时写下的那条 reason code。
#:
#: **提成常量是因为它现在有两个读者**：写它的那一处（`_decide_proposal`），
#: 和按它挑出「该落待办」的那一处（`_file_task_for_unsendable_intents`）。
#: 两边各写一遍字面量的话，改名字的人只会改到一边，**而漏掉的那一边不会报错，
#: 只会让待办悄悄不再产生**——这一整天修的都是这个形状。
UNSUPPORTED_INTENT_REASON = "GENERATION_INTENT_KIND_UNSUPPORTED"

#: payload 本身没问题，但这一回合的处境不允许这个 kind 时写下的 reason code。
#:
#: **与 `GENERATION_PAYLOAD_INCOMPLETE` 分开是有用的**：那一条说「模型给的东西不全」，
#: 这一条说「模型给的东西没问题，是我们这边现在不能收」。两者该查的地方不同——
#: 前者查提示词，后者查 profile 声明或这条会话的状态。
#:
#: **它不进 `_file_task_for_unsendable_intents`**（那一处只挑
#: `UNSUPPORTED_INTENT_REASON`），这是有意的：一条没记成的记忆**不让客户等着**，
#: 而那个待办面是为「客户在等一句没发出去的话」建的。
UNRECORDABLE_INTENT_REASON = "GENERATION_INTENT_NOT_RECORDABLE"

#: `contact_memories` 两个文本列的上界，**照 `054_contact_memories.sql` 抄**。
#:
#: **单位是字符，不是字节**——那张表写的是 `length(...) BETWEEN 1 AND N`，
#: 而 `length()` 数字符（`050` 在需要字节的地方用的是 `octet_length`）。
#: 抄错单位在本仓刚发生过一次：E4-1b 的第一版把这个 `1024` 当成字节做了一道闸，
#: 用户 2026-09-12 裁定撤掉。**这两行旁边的这句话就是那次的价钱。**
MEMORY_FIELD_KEY_MAX_CHARS = 128
MEMORY_FIELD_VALUE_MAX_CHARS = 1024

#: profile 顶层声明记忆字段的那个键。
#:
#: **唯一定义点在 `services/api/.../profiles/registry.py` 的 `MEMORY_FIELDS_KEY`**，
#: 而 worker 不能 import api 服务。**两处同名字符串靠一条门禁钉住**
#: （`test_remember_write_path.py::test_the_worker_and_the_registry_name_the_same_key`），
#: 而不是靠下一个改名的人两边都记得。
MEMORY_FIELDS_KEY = "memory_fields"

#: 一次最多从库里取几行摘要。**这是读取量的闸，不是预算那道闸**
#: —— 预算那道在 `summary_for_prompt`（按字节）。
#:
#: 取 64：`SUMMARY_PROMPT_BYTES` 是 2,400 字节，而一行最短可以是 1 字节，
#: 所以理论上装得下的行数没有上界 —— 但**一行 1 字节的摘要不是真实形状**。
#: 按一行 120 字节（40 个汉字）算，2,400 装得下 20 行；64 是它的三倍有余，
#: **取宽是因为算错的方向不对称**：取窄会让预算内的行读不到（静默少给），
#: 取宽只是多读几行再扔掉。
SUMMARY_LINES_READ = 64


def _declared_memory_fields(profile: Mapping[str, Any]) -> frozenset[str]:
    """这份 profile 声明可以记哪些字段。没声明 = 一个都不能记。

    **缺省是空集而不是「全放行」**：ADR-0131 M2 把词表交给 profile，
    平台守的是「**要有一份封闭的清单**」（ADR-0131 §三对 freeze「field allowlist」
    下的定义）。一份没声明 `memory_fields` 的 profile，
    它对「能记什么」的回答就是「什么都不记」——**而不是「没说所以随便」**。
    """

    declared = profile.get(MEMORY_FIELDS_KEY) or []
    return frozenset(str((entry or {}).get("key")) for entry in declared)


class AggregateConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedEvent:
    inbox_id: UUID
    tenant_id: UUID
    conversation_id: UUID
    envelope: dict[str, Any]
    profile: dict[str, Any]
    aggregate: dict[str, Any]
    previous_state_version: int
    previous_generation: int
    #: Material refs this conversation has already sent, **from the platform's
    #: own records** -- the `proposal` of `send_message` intents that have left
    #: `APPROVED`.
    #:
    #: **Not the model's report.** `material_delivered` is a model-filled slot
    #: like every other, so deriving the stage from it would carry exactly the
    #: provenance the stage mechanism exists to avoid: guess wrong, select the
    #: wrong materials, and nothing goes red. This is what actually left.
    #:
    #: **`EXECUTING` / `APPLIED`, not `APPROVED`** (ADR-0125 §6.1). `APPROVED`
    #: is a *transient*: the outbox claim moves it to `EXECUTING` immediately, so
    #: the intents that actually went out are no longer `APPROVED` -- the first
    #: version queried for it and therefore collected precisely the ones that had
    #: **not** been sent (independent review 2026-09-11, P0-2).
    delivered_materials: tuple[str, ...] = ()
    #: 这场对话已经发出去过的**文件** ref。
    #:
    #: 与上一行分开：那一份算阶段（`after` 只收话术 ref），
    #: 这一份管「主动只发一次」。混成一个会悄悄把阶段推快。
    delivered_assets: tuple[str, ...] = ()
    #: 上一轮**我们对这个客户说的那句话**，客户真正看到的正文。
    #:
    #: 理解步与计划步都要它，理由是同一个：**单看几个字读不出意思**。
    #: 运维 2026-09-17 实测的那一轮，客户回的是我们自己那道选择题的答案
    #: （「有人专门负责」四个字），被判 `AMBIGUOUS_LOW_SIGNAL_INPUT` 之后
    #: 选了 `wait`，客户那侧完全沉默 —— 而判它的那一步看不到我们问过什么。
    #:
    #: **空串是「这通对话我们还没说过话」**，不是「读不到」：
    #: 读不到的那几种（内容形状认不得）由 `customer_text` 判，同样落成空串，
    #: 而那正是「这句话客户也看不到」的意思。
    last_outbound: str = ""
    #: 这通对话聊过什么，**已经按 `SUMMARY_PROMPT_BYTES` 收过的那几行**
    #: （T200 / ADR-0122 §三，2026-09-20）。按发生顺序。
    conversation_summary: tuple[str, ...] = ()
    #: 装不下的有几行 —— 与 `memories_not_shown` 同一条理由。
    summary_lines_not_shown: int = 0
    #: 这通对话的摘要已经追到第几号。下一行用它 +1，**不用 `created_at`**：
    #: 同一个事务里写的两行时间戳相同，而「谁先谁后」正是滚动摘要的全部含义。
    summary_ordinal: int = 0
    #: 客户在这通对话里**自己说出过的那些数**，规范化后的字面
    #: （`("1888", "10")` 这种）。
    #:
    #: 只取数、不取正文（与 `remembered_fields` 的「只取键不取值」同源）：
    #: 要答的是「这个数客户说过吗」，答它不需要那句话，
    #: **而少带一份客户原话进内存就少一份写进日志的机会**。
    #:
    #: **空元组是「客户一个数都没说过」**，不是「读不到」——
    #: 而那正是 §5.7 那一轮的真实情况：`06:18:58` 我们说出 1888 时，
    #: 客户还要再等五分钟才第一次提到它。
    customer_numbers: tuple[str, ...] = ()
    #: **我们连着说了几轮而对面一次都没回**（ADR-0149）。
    #:
    #: 只有第四步用它，而它在那里是**分辨两件长得一模一样的事**的唯一依据：
    #: 「一次有分寸的追问」与「反复追问」在模型眼里没有区别，除非它知道这是第几次。
    #: 用户 2026-09-22：「**追问也可能是一种销售策略，不一定真的要客人回答**」——
    #: 所以平台不设「未答就不许再说」那条规则，它只把这个数给出去。
    #:
    #: **0 是「上一条是客户说的」**（或这通对话我们还没说过话），
    #: 不是「读不到」—— 与 `customer_numbers` 那条同源。
    unanswered_rounds: int = 0
    #: Slot updates this event proposed and RFC 4.3 refused (T25). Carried here
    #: rather than passed down each path because the trace has to be written
    #: wherever the transition is written, and that is both the send path and
    #: the seven no-send returns -- a refusal on a turn that ends in handoff is
    #: the case an operator most needs to see, and it is exactly the one a
    #: send-path-only record would lose.
    slot_update_refusals: tuple[dict[str, Any], ...] = ()
    #: 本回合确认入账的客户承诺，以及被拒掉的那些的理由码
    #: （`commitment_ledger.admit_customer_commitments` 的两个返回值）。
    #:
    #: **carried here，与 `slot_update_refusals` 逐字同一条理由**：台账那几行
    #: 必须与状态迁移写在同一个事务里 —— 一条能在迁移回滚之后活下来的承诺，
    #: 描述的是一个没有发生过的回合。而迁移写在**发送路径与七个 no-send 返回**
    #: 里，所以它只能挂在这里，不能当参数传。
    #:
    #: **接管期间照样记**（ADR-0054 §三.1 那条同源）：问是 AI 问的，
    #: 答是客户答的；理解步在接管期间继续跑，槽位继续写回，承诺没有理由例外。
    admitted_commitments: tuple[dict[str, Any], ...] = ()
    commitment_refusals: tuple[tuple[str, str], ...] = ()
    #: 台账的 **`party=ai`** 那一半：这一轮我们自己欠下的那几条。
    #:
    #: **与上面那两格分开，因为事实来源与时点都不同**：
    #: 客户那半从客户的话里读（理解步之后），我们这半由**打法**决定
    #: （计划步之后 —— 打法这时才定下来）。
    #:
    #: **挂在这里的理由与上面逐字相同**：台账那几行必须与状态迁移写在
    #: 同一个事务里，而迁移写在发送路径与七个 no-send 返回里。
    ai_commitments: tuple[dict[str, Any], ...] = ()
    ai_commitment_refusals: tuple[tuple[str, str], ...] = ()
    #: The closing / no-goal frame this turn entered, if it entered one, and the
    #: evidence its audit row carries (ADR-0031, ADR-0099).
    #:
    #: **Carried here for exactly the reason `slot_update_refusals` is**
    #: (independent review 2026-09-10, P2-1): the row has to be written wherever
    #: the transition is written, and that is the send path *and* every no-send
    #: return. B4-7's first version passed it as a parameter instead and reached
    #: two of the seven -- so a turn that entered a frame and then, say, hit the
    #: rate gate persisted the frame with nothing recording why. The field above
    #: had already learned this lesson; the parameter re-introduced the shape it
    #: exists to prevent.
    frame_entry: dict[str, Any] | None = None
    #: 这条会话属于哪个 contact，**没识别出来时是 `None`**
    #: （`conversation_aggregates.contact_id` 可空，`prepare` 在后续消息带上身份时回填）。
    #:
    #: **预取在这里，与 `delivered_materials` 同一条理由**：它在决策时就要被问到
    #: （`_remember_turn_refusal` 第 2 条），而 `_decide_proposal` 按设计没有连接——
    #: 「Decides; writes nothing」。要它去开一次查询，就等于让那句话不再成立。
    contact_id: UUID | None = None
    #: 这个 contact 今天已经有 active 记忆的那些 `field_key`。
    #:
    #: **不是全部记忆，只是键**：要答的问题是「这个字段已经有值了吗」
    #: （裁定八：模型只能追加不能重写），答它不需要值。
    #: 少带一份客户事实进内存，也少一份把它写进日志的机会。
    remembered_fields: tuple[str, ...] = ()
    #: 这一轮**真的递进提示词**的那几条记忆，以及装不下的有几条。
    #:
    #: **在 `prepare` 里算一次，理解步与计划步收同一份**
    #: （与 `turn_materials` / `turn_assets` 逐字同一条纪律：
    #: 两处各算一遍会让两步看见不同的清单，而那种不一致只在真实回合里显形）。
    #:
    #: 也正因为算在这里，**管线知道这一轮送了哪几条**，
    #: 于是「那条回复看见过哪几条记忆」答得出来（`inference.memories_sent`）。
    known_memories: tuple[Mapping[str, str], ...] = ()
    memories_not_shown: int = 0
    #: The tenant's `understanding_during_takeover` (B3 #10/#11). Read in
    #: `prepare()` with the rest of the row and carried, for the reason the
    #: profile is carried: the decision belongs to this turn, and re-reading it
    #: in the branch that uses it would let one event see two policies.
    #:
    #: **Defaults to `True`, which is the platform default the user ruled on
    #: 2026-09-05** -- so a `PreparedEvent` built by a fixture behaves the way
    #: the engine behaved before the switch existed.
    #:
    #: **That default is fail-open, and deliberately** (independent review
    #: 2026-09-06, P3-5): a construction site that forgot to pass it would
    #: silently resume paying for understanding calls rather than silently stop
    #: making them. It is the right way round -- the failure mode is a bill, not
    #: a tenant whose engine went quiet without being asked -- **and it holds
    #: only while `prepare()` is the single production construction site**,
    #: which it is today.
    understanding_during_takeover: bool = True


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """What one turn produced.

    `intent_ids` and `outbox_ids` are tuples because a turn may propose more
    than one intent (T44). They were single optional fields, and the singular
    spelling was not merely inconvenient: it was the shape that made
    "exactly one proposed intent" look like a reasonable restriction rather
    than a deviation from the intent-saga the RFC describes.

    Empty tuples where the old code wrote `None`. A caller asking "did anything
    get proposed" reads the length; nothing has to know about a sentinel.
    """

    inbox_id: UUID
    status: str
    intent_ids: tuple[UUID, ...] = ()
    outbox_ids: tuple[UUID, ...] = ()
    receipt_status: str | None = None


#: The intent kinds the send path can actually carry today.
#:
#: A frozenset rather than an equality test because the restriction is about
#: *this* step's reach, not about the vocabulary: `intent-record.schema.json`
#: already knows several kinds, and the ones outside this set are not malformed,
#: they are unbuilt.
#:
#: **Adding one is not a one-line change, and an earlier version of this comment
#: said it was** (independent review 2026-09-05, P2-2). Four places downstream
#: are written for `send_message` specifically, and a second kind reaching them
#: would be recorded wrongly rather than refused:
#:
#:   1. `_decide_proposal` builds `intent_contract["kind"] = "send_message"` --
#:      hard-coded, so a `send_material` proposal becomes a `send_message`
#:      intent record and still passes contract validation;
#:   2. `_persist_accepted_proposal` inserts the literal `'send_message'`;
#:   3. the same function builds `command` with `"audience": "customer"` and
#:      reads `typed_payload["content"]`. **It no longer raises `KeyError`**
#:      (T110): the payload guard below refuses such a proposal one step
#:      earlier, with a record. What remains here is the audience, still
#:      implicit;
#:   4. **that payload guard is itself written for `send_message`** -- it asks
#:      for a `content` object, which is what *this* kind needs. A second kind
#:      admitted to this set without its own write path would meet a refusal
#:      addressed to the wrong question; the guard names the kind it is
#:      speaking about so that the mismatch is visible in the record rather
#:      than silent (independent review 2026-09-07, P2-1).
#:
#: So the honest shape of "support a second kind" is: a per-kind write path,
#: with this set derived from its keys.
#:
#: **E3-3 (2026-09-12) built that shape -- for three of the four places, plus a
#: fifth nobody had listed.** What it did **not** close is site 3, and this
#: comment said it had:
#:
#:   * ✅ **sites 1 and 2** -- `intent_records.kind` and the contract's `kind`
#:     both read the proposal's kind, so a `send_material` proposal can no longer
#:     be recorded as a `send_message`;
#:   * ✅ **site 4** -- the payload refusal lives on the entry, so a second kind
#:     gets a refusal addressed to **its own** question rather than to
#:     `send_message`'s;
#:   * ✅ **the fifth** -- the idempotency prefix comes from the entry;
#:     **it was a hard-coded site the list above never mentioned**;
#:   * ✅ **site 3 -- closed by E4-2 (2026-09-12), not by E3-3.** The entry now
#:     carries a `ProposalEffect`: `OutboxDelivery` names the audience and builds
#:     the content, `DirectWrite` means this kind writes a row and **sends
#:     nothing**. `_persist_accepted_proposal` dispatches on it.
#:
#: **This bullet used to read「the outbox command is built by the entry」, and
#: that sentence was false the day it was written** (independent review
#: 2026-09-12, P1-1). The count stayed at five because the self-review's table
#: put "the derived set" where site 3 had been -- **one site went missing and the
#: arithmetic still worked**.
#:
#: **Why this is not merely a documentation defect.** `GenerationWritePath`'s
#: docstring says a kind needs the things on the entry and nothing else, so
#: someone registering a second kind by following this contract gets:
#: `remember` (sends nothing) -- an undeliverable `outbox_messages` row per
#: memory; `send_material` (no `content` in its payload) -- a `KeyError` **inside
#: the transaction**, rolling the turn back, leaving the inbox event unconsumed
#: and retrying into the same crash. **That is the exact shape T110 is registered
#: for, and the exact shape the paragraph below uses to argue against
#: registering `send_material` today.**
#:
#: **The structural fix landed in E4-2** (user ruling, 2026-09-12), the batch that
#: had to have it: `remember` sends nothing, and **that kind could not even be
#: expressed** while accepting a proposal and queueing an outbox row were the same
#: act. T308 closes with this batch.
#:
#: **`send_material` is deliberately not registered yet.** The write path is
#: only half of carrying a kind: the other half is a channel that can deliver
#: it, and `/msg/sendFile` needs an upload contract this repository does not
#: have (`docs/wecom-proxy-message-api.md:421` names `/cloud/cdnBigUploadByUrl`
#: once, with no request body, no response body and no error codes -- E3-4).
#: **Registering it now would queue outbox rows nothing can send**, which is
#: worse than the refusal below: a refusal leaves a record and a reason code,
#: a stuck row leaves a customer waiting.


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    """这个 kind 被接受之后的效果 = **排一行出站消息**。

    E4-2 之前这是唯一可能的效果，所以它不必被表达出来，于是它也没有被表达——
    `_persist_accepted_proposal` 无条件写 `outbox_messages`。
    **一个「不发任何东西」的 kind 在那个形状里无法表示**，而 `remember` 正是这种。
    """

    #: 出站行的收件对象。**这是上面第 3 处里「仍然隐含」的那一半**：
    #: 写死成 `"customer"` 时，一条经理通知只要走到这里就会被投给客户。
    audience: str

    #: 从 payload 造出渠道能发的那个 `content`。
    #: **每个 kind 自己的形状**——`send_message` 要 `content` 里的话术，
    #: 别的 kind 要什么不由它代答。
    build_content: Callable[[Mapping[str, Any], Mapping[str, str] | None], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class DirectWrite:
    """效果 = **在库里写一行，不发任何东西给任何人**。

    `remember` 是第一个这样的 kind：它改变的是我们记住了什么，
    **没有任何一个字送到客户那端**。用 `OutboxDelivery` 表示它会排出一行
    没有渠道能发的消息——**正是上面 `send_material` 那段警告的形状**。
    """

    #: 写那一行。在 `_persist_accepted_proposal` 已经开着的那个事务里跑，
    #: 所以它与 intent / decision / audit 三行同生共死。
    write: Callable[[Any, PreparedEvent, ProposalDecision], None]


#: 一个 kind 被接受之后到底发生什么。**两种，穷尽**。
ProposalEffect = OutboxDelivery | DirectWrite


def _queues_an_outbox_row(decision: ProposalDecision) -> bool:
    """这条被接受的提案会不会排一行出站消息。

    **两个调用点问的是同一件事，所以它是一个函数而不是两处 `isinstance`**：
    `_persist_accepted_proposal` 问「写什么」，发送路径问「哪几道闸管得着它」。
    两处各写一次 `isinstance(..., OutboxDelivery)` 时，
    **加第三个 kind 的人只会改到一处**——而漏掉的那一处不会报错，
    只会让一条不发消息的提案重新去占别人的名额（独立审查 2026-09-12，P1-1）。
    """

    return isinstance(GENERATION_WRITE_PATHS[decision.kind].effect, OutboxDelivery)


@dataclass(frozen=True, slots=True)
class GenerationWritePath:
    """把一个 intent kind 送出去，需要回答的五件。

    **缺任何一件都不该进 `GENERATION_SUPPORTED_KINDS`。**

    ## 「三件」曾经**不够**，而那正是本类要记住的一课

    E3-3 立这个类时是三件（`kind` / `payload_refusal` / `idempotency_prefix`），
    docstring 与用例把「三件齐了」说成了「可以注册」——**而那不是真的**：
    `_persist_accepted_proposal` 当时仍然无条件排一行出站消息，形状写死为
    `send_message` 的。照那份合同注册进来的 `remember` 会给每条记忆排一行
    **发不出去的** `outbox_messages`，`send_material` 会在事务里抛 `KeyError`。
    独立审查 2026-09-12 P1-1 抓到了它，登记为 T308。

    **E4-2 补的是 `effect`**，而它不是「再加一个字段」那么轻——
    它是**第一次让「接受一个提案」和「排一行出站消息」不再是同一件事**。
    `remember` 逼出了这一步：一个不发任何东西的 kind，
    在旧形状里**连表示都表示不出来**。

    > **留给下一个加 kind 的人**：这五件仍然只是**必要**条件。
    > 判断它们够不够的办法不是读这段话，是**问一遍你的 kind 在
    > `_persist_accepted_proposal` 里会走到哪几条 `INSERT`**——
    > 上一次出事，出在没人问这一句。
    """

    #: 写进 `intent_records.kind` 与合同的那个值。与注册键相同，写出来是为了
    #: 让「键」与「值」不一致这件事在读的时候就看得见。
    kind: str

    #: 这个 kind 的 payload 缺什么就发不出去；`None` 表示可以发。
    #: **每个 kind 问自己的问题**——`send_message` 要 `content`，
    #: 而别的 kind 要什么由它自己说，不由这一个代答。
    #:
    #: **只看 payload**，不看这一回合的处境——那是 `turn_refusal` 的事。
    #: 分成两个而不是合成一个，是因为这一个**纯**：它能被单独喂一个 dict 测，
    #: 不需要一个 `PreparedEvent`。
    payload_refusal: Callable[[object], str | None]

    #: 幂等键的前缀。**这一项是 E3-3 才暴露出来的第五处硬编码。**
    idempotency_prefix: str

    #: 被接受之后发生什么。**E4-2 加的第 3 处。**
    effect: ProposalEffect

    #: payload 没问题，但**这一回合的处境**不允许这个 kind 时的拒绝理由。
    #: `None` 表示这个 kind 对处境没有要求——`send_message` 就是这样。
    #:
    #: **`remember` 有四条这样的要求**，见 `_remember_turn_refusal`：
    #: 它们一条都放不进 `payload_refusal`，因为答案不在 payload 里。
    turn_refusal: Callable[[object, PreparedEvent], str | None] | None = None


def _send_message_payload_refusal(payload: object) -> str | None:
    """`send_message` 要一个 `content` 对象。

    **`detail` 是有界的**（独立审查 2026-09-07，P3-1）：键来自模型而
    `typed_payload` 没有 `maxProperties`，这是第一个到达
    `policy_decisions.context` 的模型可控字符串。审计侧已按 500 截断，
    这里与它取同一个数，而不是另发明一个。
    """

    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, dict):
        return None
    present = sorted(payload)[:20] if isinstance(payload, dict) else type(payload).__name__
    return (f"a send_message payload needs a `content` object; this one has {present}")[:500]


def resolve_asset_for_send(
    connection: Any, *, tenant_id: UUID, reference: str
) -> tuple[dict[str, Any] | None, str | None]:
    """把一行出站文件消息的引用解析成能发的东西，或者说清为什么发不了。

    回 `(content, None)` 或 `(None, 理由)`。

    ## **版本按目录里的当前那一版，不按引用里写的那个**

    ADR-0130 §二.2 逐字：「profile 声明 slug 不声明版本，**发送时解析当前那一版**」。
    所以引用里的 `v1` 是语法占位 —— 这里按 `slug` 取**版本最大的那一行 active**。

    **照着引用里的版本发是这一格最安静的一种错**：它发得出去、供应商回成功、
    客户收到一个文件 —— **而那是个旧版本**。价格表改过之后还在发上一版，
    没有任何东西会红。

    ## 拒绝的三种，各有各的名字

    没有这个 slug（包里声明了而目录里没导）· 只有归档的版本 ·
    **有行但还没传给供应商**（`provider_file_id IS NULL`）。
    第三种是最常见的一种：导了目录而没跑 `upload_materials.py`。
    三种都在**派发**这一刻被答出来，而不是让 `sendFile` 带着空字段去失败 ——
    供应商那边的失败只有 `code` / `msg`，读日志的人看不出是哪一种。
    """

    slug = reference.split(":")[1] if reference.count(":") >= 2 else ""
    if not slug:
        return None, f"发不出去的引用：{reference!r} 不是 asset:<slug>:v<N> 的形状"

    row = connection.execute(
        """
        SELECT id, version, status, filename,
               provider_file_id, provider_file_aes_key, provider_file_md5,
               provider_file_size
          FROM materials
         WHERE tenant_id = %s AND slug = %s AND status = 'active'
         ORDER BY version DESC
         LIMIT 1
        """,
        (tenant_id, slug),
    ).fetchone()
    if row is None:
        return None, (
            f"目录里没有 {slug!r} 的在用版本 —— 包声明了它，而 materials 里没有"
            "（多半是没跑 scripts/import_materials.py，或者所有版本都归档了）"
        )

    file_id = str(row["provider_file_id"] or "").strip()
    if not file_id:
        return None, (
            f"{slug}:v{row['version']} 还没传给供应商（provider_file_id 为空）——"
            " 跑 scripts/upload_materials.py，然后等那条 cmd=20000 回调认领它"
        )
    md5 = str(row["provider_file_md5"] or "").strip()
    size = row["provider_file_size"]
    if not md5 or not size:
        return None, (
            f"{slug}:v{row['version']} 只认领了一半（md5 或 size 空）——"
            " sendFile 要它们，而一个空字段发出去只会换回一句 code/msg"
        )

    return {
        "type": FILE_CONTENT_TYPE,
        "file_id": file_id,
        "file_size": int(size),
        "file_md5": md5,
        "filename": str(row["filename"]),
        # **可能是 `None`，而那是合法的一档**：大文件的 `fileId` 以 `*` 开头时
        # 供应商允许密钥为空。判据在客户端（`is_big_file`），不在这里 ——
        # 两处各判一遍迟早有一处宽一点。
        "aes_key": str(row["provider_file_aes_key"] or "").strip() or None,
    }, None


def _send_material_payload_refusal(payload: object) -> str | None:
    """`send_material` 的 payload 要的只有一个 `asset:` 引用。

    **不收 `content`、不收文件名、不收大小**：那几样都在 `materials` 表里，
    而模型手上没有它们。收下模型给的版本会更糟 ——
    ADR-0130 §二.2 写着「profile 声明 slug 不声明版本，**发送时解析当前那一版**」，
    一个从模型那里收来的版本号会把那句话作废，而症状是**发了一个旧版本**：
    发得出去、发成功、内容是过期的。

    **引用的合法性调唯一定义点**（`runtime.references`），不在这里重写一遍正则。
    """

    if not isinstance(payload, dict):
        return f"a send_material payload must be an object; this one is {type(payload).__name__}"
    reference = payload.get("ref")
    if not isinstance(reference, str) or not reference.strip():
        present = sorted(payload)[:20]
        return (f"a send_material payload needs a `ref` string; this one has {present}")[:500]
    refusal = reference_refusal(reference.strip(), expected_kind="asset")
    if refusal is not None:
        # **要 `asset:`，不是 `material:`。** 两者同形而指的不是一类东西：
        # `material:` 是一段话术（解析成商家原文进正文），`asset:` 是一份文件。
        # 拿 `material:` 去发文件，目录里一行都找不到 —— 而那个失败发生在
        # 派发那一刻，客户已经在等了。
        return f"send_material 的 ref 不合法：{refusal}"[:500]
    return None


def _send_material_content(
    typed_payload: Mapping[str, Any], materials: Mapping[str, str] | None  # noqa: ARG001 - 写入路径的统一签名；文件不从话术表里取
) -> dict[str, Any]:
    """`send_material` 排进出站行的 `content`：**只有一个引用**。

    **四个 `provider_*` 字段不在这里**，而这是这一格最要紧的一个决定：
    把它们拼进来就是把凭据（`fileAesKey` / `fileMd5`）复制进 `outbox_messages`，
    **而那张表的读面比 `materials` 宽得多** —— `fastagent_app` 从 `058` 起
    就读得到出站行，消息页也读它。`066` / `067` 那两条列级授权围的就是这件事，
    在这里拼一次就全绕过去了。

    引用在**发送那一刻**才被解析（`_resolve_asset_for_send`），
    取出来的密钥只活在那一次调用的内存里。

    `materials` 那个参数是写入路径的统一签名 —— 它是**话术**表（ref → 一段字），
    而文件不在里面。收下它却不用，比给这个 kind 另立一个签名好：
    签名一旦分叉，下一个 kind 要照着哪一个写就成了一道选择题。
    """

    return {
        "type": ASSET_CONTENT_TYPE,
        "ref": str(typed_payload["ref"]).strip(),
    }


def _send_message_content(
    typed_payload: Mapping[str, Any], materials: Mapping[str, str] | None
) -> dict[str, Any]:
    """`send_message` 排进出站行的 `content`。

    **The canonical shape, not the model's**——这句话原本写在
    `_persist_accepted_proposal` 里，连同 `materials` 必须与决策时同一份的理由。
    搬到这里之后它才真的是「这个 kind 的形状」，而不是「所有 kind 的形状」。

    `customer_text` 在这里不会返回 `None`：`_decide_proposal` 已经把那种提案拒掉了。
    **所以那是一道闸而不是这里的一句断言**（原注释的话，逐字保留）。
    """

    return {
        "type": TEXT_CONTENT_TYPE,
        "text": customer_text(typed_payload["content"], materials=materials),
    }


def _followup_payload_refusal(payload: object) -> str | None:
    """`followup` 与它的邻居问同一个问题：要一个 `content` 对象。

    **同一个问题，两条各自说自己的名字。** 合成一条泛化的
    「所有出站 kind 都要 content」会让拒绝理由不说自己在替谁说话，
    而那正是 `test_the_payload_refusal_speaks_about_its_own_kind` 钉住的东西。
    两条会不会漂由 `test_the_two_outbound_kinds_ask_the_same_payload_question` 看着。

    `detail` 的 500 上界照邻居抄：键来自模型，而 `typed_payload` 没有
    `maxProperties`。
    """

    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, dict):
        return None
    present = sorted(payload)[:20] if isinstance(payload, dict) else type(payload).__name__
    return (f"a followup payload needs a `content` object; this one has {present}")[:500]


def _followup_turn_refusal(payload: object, prepared: PreparedEvent) -> str | None:
    """**第二道锁**：`followup` 只在定时器到期的那种回合成立。

    今天到这里的 `followup` 都是平台判出来的
    （`_outbound_kind_for_this_turn`，它只在 `event_type == "timer"` 时给这个值），
    所以这道闸**今天不会触发** —— 它守的是**明天**：
    有人让模型直接提名 `followup`，或者那个判定被改宽。

    **一道今天不会响的闸要说清自己为什么在**（否则下一个人会把它当死代码删掉）：
    没有它，`followup` 与它的邻居在**语义上**无法区分，而那时它就是第二条路
    （T359 记的正是这个形状）。

    **正向判据，不是列举要拒的**（`_remember_turn_refusal` 第 1 条那次的教训）：
    `event_type` 的值域不止两个 —— `manager_comment` / `business_event` /
    `delivery_receipt` / `internal` 同样都不是定时器到期。
    """

    del payload  # 这道闸问的是回合，不是 payload —— 那一半是上面那条的事。
    if str(prepared.envelope.get("event_type")) != "timer":
        return (
            "a followup is the proactive send after a timer came due; this turn is "
            f"{str(prepared.envelope.get('event_type'))!r}"
        )[:500]
    return None


def _remember_payload_refusal(payload: object) -> str | None:
    """`remember` 要一个 `field_key` 和一个 `field_value`，两个都是非空字符串。

    **上界照 `054_contact_memories.sql` 抄，而那张表用的是 `length()`**——
    `length()` 数的是**字符**不是字节。这里因此也数字符。
    **这一句是本仓付过钱才写下的**：E4-1b 的第一版把同一个 `1024` 当成字节，
    用户 2026-09-12 裁定撤掉那道闸。**量纲写在旁边比记得住便宜。**

    **`source_event_id` 不在这里要**，理由见 `_remember_write`：
    它是平台盖的，不是模型报的。
    """

    if not isinstance(payload, dict):
        return f"a remember payload must be an object; this one is {type(payload).__name__}"

    missing = [name for name in ("field_key", "field_value") if not payload.get(name)]
    if missing:
        present = sorted(payload)[:20]
        return (
            f"a remember payload needs {missing} as non-empty strings; this one has {present}"
        )[:500]

    bounds = (
        ("field_key", MEMORY_FIELD_KEY_MAX_CHARS),
        ("field_value", MEMORY_FIELD_VALUE_MAX_CHARS),
    )
    for name, limit in bounds:
        value = payload[name]
        if not isinstance(value, str):
            return f"a remember payload's `{name}` must be a string, not {type(value).__name__}"
        if len(value) > limit:
            # 字符数，与表上的 `length()` 同口径。
            return (
                f"a remember payload's `{name}` is {len(value)} characters; "
                f"the table allows {limit}"
            )

    # **禁止项那一层，按取值判**（T298，到期条件点名 E4-2 / E4-3）。
    #
    # E4-1b 的闸读的是**商家给这一列取的名字**，它自己登记了这条缺口：
    # 写 `id_card_no` 会被挡，写 `customer_extra_2` 不会。
    # **而写入路径是唯一看得见取值的地方**，这正是 T298 把到期条件点在写入批次上的原因。
    # E4-2 落地时没做，**E4-3 是它点名的最后一批**，于是在这里补上——
    # 补在**两条**写入路径上而不是只补控制面那一条：**模型这一侧才是风险大的那半**，
    # 而共享判据放在 `packages/runtime` 里，两边读同一个定义。
    #
    # **拒的话里不带取值**，理由见 `forbidden_value_reason` 的 docstring：
    # 这条拒绝会落成一行记录，而 E4-2 刚在同族的地方付过这笔钱
    # （`_record_rate_limit` 把 `withheld_content` 原样写进了 `policy_decisions.context`）。
    hazard = forbidden_value_reason(str(payload["field_value"]))
    if hazard is not None:
        return f"a remember payload was refused on its value: {hazard}"[:500]

    return None


def _remember_turn_refusal(payload: object, prepared: PreparedEvent) -> str | None:
    """payload 没问题，但**这一回合**能不能记。四条，全都答不在 payload 里。

    **顺序是有意的**：先问「这一回合有没有客户的话」，再问「记给谁」，
    再问「这个字段商家声明过吗」，最后才问「这个字段是不是已经有值了」。
    **每一条拒绝都落成一条记录**，所以顺序决定了运维读到的是哪一个原因——
    把最根本的那条排在前面。
    """

    field_key = str(payload["field_key"]) if isinstance(payload, dict) else ""

    # 1. **只有客户自己说话的那种回合，才可能有客户说过的话。**
    #    排期 §4.6 的反向判据「客户没说过的事不许进记忆」，
    #    在平台能验的范围内，这是最实的那一条。
    #
    #    **正向写，不是列举要拒的**（独立审查 2026-09-12，P3-2）。
    #    第一版写的是 `== "timer"` 即拒，而 `event_type` 的值域不止两个：
    #    `manager_comment` / `business_event` / `delivery_receipt` / `internal`
    #    **同样一个字都不是客户说的，却都能过那道闸**。
    #    今天够不着（全仓只有 `adapter.py` 产 `customer_message`、
    #    `scheduler.py` 产 `timer`），**但极性是反的**——
    #    本批在 `memory_fields` 上刚选了「没声明 = 一个都不记」的关门方向，
    #    同一批里这道闸却朝开门方向写，**下一个 event_type 会默认被放行**。
    event_type = str(prepared.envelope.get("event_type"))
    if event_type != "customer_message":
        return (
            f"a remember proposal on a {event_type!r} event has no customer words "
            "behind it; only a customer_message turn can"
        )[:500]

    # 2. **记给谁。** `conversation_aggregates.contact_id` 可以是 NULL
    #    （客户还没被识别），而 `contact_memories.contact_id` 是 NOT NULL。
    #    **不在这里拒，就是一次 NOT NULL 违例**——异常回滚整个回合、
    #    inbox 事件不被消费、退回重试预算里再炸一次，
    #    正是本文件上方 T110 那段描述的那个形状。
    if prepared.contact_id is None:
        return (
            "this conversation has no identified contact yet, "
            "so there is nobody to remember about"
        )

    # 3. **商家声明过这个字段吗**（ADR-0131 M2）。平台不定词表，
    #    profile 定；**而「有一份封闭的清单」这件事由平台守**。
    #    E4-1b 建的是封版时的闸（禁止项、重复 key），
    #    **这里是它的第一个运行期消费者**。
    declared = _declared_memory_fields(prepared.profile)
    if field_key not in declared:
        # 声明的键名来自商家 profile，不是模型，可以原样写进记录。
        return (
            f"{field_key!r} is not one of the memory fields this profile declares "
            f"({sorted(declared)[:20]})"
        )[:500]

    # 4. **模型只能追加，不能重写**（裁定八 / 排期 §4.4）。
    #    表上 `contact_memories_one_active_per_field_idx` 保证一个字段只有一条 active，
    #    **而在这里拒和撞唯一索引是两回事**：撞索引是异常，拒是一条带原因的记录。
    if field_key in prepared.remembered_fields:
        return (
            f"{field_key!r} already has an active memory, and the model may only append, "
            "never rewrite one (ADR-0131 / 裁定八)"
        )[:500]

    return None


def _remember_write(connection: Any, prepared: PreparedEvent, decision: ProposalDecision) -> None:
    """写一行 `contact_memories`。**不排出站行，因为没有任何字要发给客户。**

    **`source_event_id` 是这里盖的，不是模型报的**，而这不是图省事：

    `inference_model.py` 的 `understand` 已经为同一个问题给过答案——
    「The two identity fields of every slot update, **supplied here rather than
    asked of the model**……**Asking the model for two uuids it has never seen is
    what made `slot_updates` unanswerable**, so it answered `[]` every time
    (T34)」。**模型在提示词里从来看不到 event uuid**，
    要它报一个，得到的要么是空要么是编的。

    **同一条注释还写了为什么盖的比报的好**：「this is also why
    `evidence_event_id` is trustworthy while `evidence_span_hash` is not
    (T41)」。记忆的来源是**客户自己说过的话**（`054` 的表头），
    而哪一条事件正被处理，平台自己知道得比模型准。

    **代价要说清，不藏**：盖上去之后，「来源事件存在」成了构造上必然成立的事，
    **它不再能证明「客户确实说过这句话」**。平台能守的是
    「每条记忆都锚在一条真实的客户事件上」与「只记商家声明过的字段」；
    **「这句话是不是真从客户嘴里出来的」平台验不了**，
    `_remember_turn_refusal` 的第 1 条是这条判据在可验范围内的全部。
    **登记为 T309**（T308 是第 3 处，不是这一条——两件事别合号）。
    """

    assert decision.proposal is not None  # noqa: S101 - accepted implies built
    payload = decision.proposal["typed_payload"]
    connection.execute(
        """
        INSERT INTO contact_memories(
            tenant_id, id, contact_id, field_key, field_value, source_event_id,
            revision, supersedes, status
        ) VALUES (%s, %s, %s, %s, %s, %s, 1, NULL, 'active')
        """,
        (
            prepared.tenant_id,
            uuid4(),
            prepared.contact_id,
            str(payload["field_key"]),
            str(payload["field_value"]),
            UUID(str(prepared.envelope["event_id"])),
        ),
    )


#: **唯一定义点。** 加一个 kind = 加一个条目；
#: `GENERATION_SUPPORTED_KINDS` 与写入路径都从这里读。
GENERATION_WRITE_PATHS: Mapping[str, GenerationWritePath] = {
    "send_message": GenerationWritePath(
        kind="send_message",
        payload_refusal=_send_message_payload_refusal,
        idempotency_prefix="send_message",
        effect=OutboxDelivery(
            # **L2a 的那条**：每一行出站消息都要说清它是发给谁的。
            # 客户消息曾经是唯一的一种，于是「发给客户」是隐含的——
            # 而一个隐含的收件对象，正是让一条经理通知被投给客户的那件事。
            audience="customer",
            build_content=_send_message_content,
        ),
    ),
    "followup": GenerationWritePath(
        kind="followup",
        payload_refusal=_followup_payload_refusal,
        idempotency_prefix="followup",
        effect=OutboxDelivery(
            audience="customer",
            # **借邻居那个 content 构造器，不另写一个。**
            # 两个 kind 送出去的东西形状完全相同（文字/段），
            # 而两份各自演化的构造器迟早不一致 —— 本仓为「同一件事两处各写一遍」
            # 付过好几次钱。**不同的是什么时候能送**，那由 `turn_refusal` 管。
            build_content=_send_message_content,
        ),
        turn_refusal=_followup_turn_refusal,
    ),
    "send_material": GenerationWritePath(
        kind="send_material",
        payload_refusal=_send_material_payload_refusal,
        idempotency_prefix="send_material",
        effect=OutboxDelivery(
            audience="customer",
            build_content=_send_material_content,
        ),
    ),
    "remember": GenerationWritePath(
        kind="remember",
        payload_refusal=_remember_payload_refusal,
        idempotency_prefix="remember",
        effect=DirectWrite(write=_remember_write),
        turn_refusal=_remember_turn_refusal,
    ),
}

#: 同一件出站，**被动那一种记法 → 主动那一种**。
#:
#: **它在这里而不是在判定函数里**，是 `test_every_hard_coded_send_message_is_gone`
#: 逼出来的 —— 那条守卫说「加一个 kind 应当是加一个注册表条目，
#: 不是在五个地方各改一次字面量」，而它是对的：这张表就是注册表数据。
#: 判定函数只问「这一轮是不是定时器到期」，查表的事交给这里。
#:
#: **一条，而不是给每个出站 kind 都配一条**：今天能被记成主动跟进的只有那一种。
#: 下一个出站 kind 要不要进这张表，由加它的人回答 —— 那时这张表会让他看见这一问。
PROACTIVE_EQUIVALENT: Mapping[str, str] = {"send_message": "followup"}

GENERATION_SUPPORTED_KINDS = frozenset(GENERATION_WRITE_PATHS)


@dataclass(frozen=True, slots=True)
class ProposalDecision:
    """One proposal's trip through the policy chain, accepted or not.

    The `ordinal` is the whole point of the type. Before T44 a turn had one
    proposal, so "which one was refused" had no meaning and the refusal record
    did not carry an answer. With several, a denial that does not say which
    proposal it belongs to is a record that cannot be acted on -- the fourth of
    the four things this step is required to do.
    """

    ordinal: int
    kind: str
    accepted: bool
    reason_code: str
    detail: str
    intent_id: UUID | None = None
    policy_id: UUID | None = None
    outbox_id: UUID | None = None
    proposal: dict[str, Any] | None = None
    intent_contract: dict[str, Any] | None = None
    policy_context: dict[str, Any] | None = None
    idempotency_key: str | None = None
    #: 这一段发出去之后要停多久，毫秒（ADR-0151）。**非分段行是 0**，
    #: 而取件谓词对 `pace_ms = 0` 的行恒真 —— 存量行为逐字不变。
    pace_ms: int = 0
    #: When **this** refusal stops being true, for refusals that expire by the
    #: clock. **Carried on the decision rather than taken from the turn**
    #: (independent re-review 2026-09-09, P2-1 / P3-1): a turn can refuse one
    #: proposal for quiet hours and another for a malformed payload, and only
    #: the first has an instant. Keyed off the turn instead, the second row
    #: would say 「这条 payload 缺失的拒绝会在 08:00 自动失效」.
    #:
    #: Defaulted, unlike `ProactivityVerdict.window_lifts_at`: this type has
    #: many construction sites for refusals that genuinely have no expiry, and
    #: `None` is a true statement at every one of them.
    next_allowed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    tenant_id: UUID
    intent_id: UUID
    outbox_id: UUID
    receipt_status: str
    #: True when the lease ran out while this dispatcher was talking to the
    #: provider and another one took the row. The send happened; **nothing was
    #: written down**, because the row is no longer ours to write. See the
    #: closing block of `dispatch_one`.
    lease_lost: bool = False


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _iso_or_none(value: Any) -> str | None:
    """一列 `timestamptz` 刷进聚合 JSONB 时该长的样子（运维缺陷 20）。

    **`None` 原样是 `None`，而那不是「读不出来」**：这一列为空只有一种意思 ——
    **这通对话一条出站都没发过**。契约那一格是 `["string", "null"]`，
    所以空也是一个合法的答案，不是一次失败。

    ⚠️ **不直接把 `datetime` 塞进 JSONB**：那一份要过
    `validate_contract("conversation-aggregate", …)`，而 `format: date-time`
    要的是字符串；塞 `datetime` 进去会在**序列化那一刻**才炸，
    而那时离这里已经隔了半条管线。
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return _iso(value)
    return str(value)


#: Why a binding cannot send, in the vocabulary the receipt already uses.
#: Named rather than inlined because each one is a *different* operational
#: story and collapsing them into "no guid" would make the three
#: indistinguishable in a receipt -- which is the L1 distinction this repo
#: keeps insisting on: "we asked and do not know" is not "we never asked",
#: and neither is "there was nobody to ask".
#: The conversation itself could not be read. **Not the same as "no binding"**
#: (independent review 2026-09-08, P2-1): `008` puts a foreign key on
#: `(tenant_id, config_id)`, so a non-null `config_id` *guarantees* a binding
#: row -- "no binding row" cannot happen and must not be reported. What can
#: happen, and what a single join flattened into it, is: the aggregate does not
#: exist, or it exists and row-level security hides it because the connection's
#: tenant context does not match the tenant asked about. **The second is the
#: dangerous one** -- a context or tenant wiring error would have been handed to
#: an operator wearing the costume of a provisioning gap.
CONVERSATION_NOT_VISIBLE = "conversation_not_visible"

#: The conversation is readable but carries no `config_id`. `008` added the
#: column **without** `NOT NULL`, so this is a state the schema allows: an
#: aggregate that predates the binding link, or one created without one.
CONVERSATION_NOT_BOUND = "conversation_not_bound"

BINDING_DISABLED = "binding_disabled"
BINDING_HAS_NO_INSTANCE = "binding_has_no_provider_instance"


def resolve_binding_guid(
    connection: Any, *, tenant_id: UUID, conversation_id: UUID
) -> tuple[str | None, str | None]:
    """The provider instance this conversation sends through, or why not.

    Returns `(guid, refusal_reason)` -- exactly one of them is `None`.

    **The dispatcher resolves this itself** (ADR-0094 §二, user ruling
    2026-09-07). The alternative considered and refused there was letting
    the outbox row carry the guid along, which "把当下才成立的事实冻进一条
    可能排队很久的行里" -- a row can sit in the queue across a rebind, and
    `007` already has a mechanism for rebinding. Resolving at dispatch time
    means the answer is as fresh as the send.

    **Reads four columns and no more** (`046`): the two that form the key,
    the guid, and `status`. The credentials on this table are deliberately
    outside the grant -- see the migration for why each column is or is not
    there.

    **A missing guid is a refusal, not an exception**, and the reasons are kept
    apart because they are different things to do about it:

    * `CONVERSATION_NOT_VISIBLE` -- the aggregate could not be read at all.
      Either it does not exist, or RLS is hiding it because the connection's
      tenant context disagrees with `tenant_id`. **That second case is a wiring
      fault on our side**, and it must not reach an operator disguised as a
      supply problem;
    * `CONVERSATION_NOT_BOUND` -- readable, but no `config_id`. `008` added that
      column without `NOT NULL`, so it is a state the schema permits;
    * `BINDING_DISABLED` -- somebody turned it off deliberately;
    * `BINDING_HAS_NO_INSTANCE` -- the ordinary state between creating a binding
      and the operator finishing the ceremony.

    **There is deliberately no "binding not found".** `008` constrains
    `(tenant_id, config_id)` with a foreign key, so a non-null `config_id`
    guarantees the binding row exists; reporting a state the schema forbids
    would be worse than useless -- it would send somebody looking for a row that
    could not be missing (independent review P2-1).
    """

    # **Resolved in two steps, not one join** (P2-1). A join answers "is there a
    # row on the other side" and cannot tell "the conversation is invisible to
    # me" apart from "the conversation has no binding" -- and those are reported
    # to different people.
    conversation = connection.execute(
        "SELECT config_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
        (tenant_id, conversation_id),
    ).fetchone()
    if conversation is None:
        return None, CONVERSATION_NOT_VISIBLE
    if not conversation["config_id"]:
        return None, CONVERSATION_NOT_BOUND

    return guid_for_config(connection, tenant_id=tenant_id, config_id=conversation["config_id"])


def guid_for_config(
    connection: Any, *, tenant_id: UUID, config_id: Any
) -> tuple[str | None, str | None]:
    """The provider instance behind one binding, or why it cannot send.

    **Split out of `resolve_binding_guid` by B2-3**, because the outbound path
    has two ways of arriving at a `config_id` and only one of them starts from a
    conversation:

    * a **customer** row is addressed by its conversation, which carries the
      `config_id` (`resolve_binding_guid`, above);
    * a **manager** row carries its own `recipient.config_id` (T90) -- the digest
      borrows a conversation id only to satisfy a foreign key, and reading the
      binding off that conversation would be the very substitution
      `unroutable_reason` exists to prevent.

    Sharing this half keeps the four refusal reasons identical for both, which
    matters because they are what an operator reads off the receipt.
    """

    row = connection.execute(
        """
        SELECT provider_instance_id, status FROM channel_bindings
         WHERE tenant_id = %s AND config_id = %s
        """,
        (tenant_id, config_id),
    ).fetchone()

    if row is None:
        # `008`'s foreign key says this cannot happen for a conversation-derived
        # `config_id`. If it ever does, the constraint is gone and the honest
        # answer is that the conversation is not usable -- not an invented
        # supply story. A *manager* row's `config_id` comes from the command
        # rather than from a foreign key, so for that path this is reachable and
        # means what it says.
        return None, CONVERSATION_NOT_BOUND
    if str(row["status"]) != "active":
        return None, BINDING_DISABLED
    guid = row["provider_instance_id"]
    if not guid:
        return None, BINDING_HAS_NO_INSTANCE
    return str(guid), None


#: The conversation has no contact recorded, so there is nobody to address.
#: `040` allows this on purpose and says why: `contact_id` is NULL when the
#: first message carried no identity, and for conversations older than `039`.
#: **Both are honestly "no contact recorded" and neither means "anonymous"** --
#: which is exactly why this is its own reason and not folded into a guid
#: failure. One is a supply problem; this one is a message we can never address.
CONVERSATION_HAS_NO_CONTACT = "conversation_has_no_contact"


def resolve_recipient_id(
    connection: Any, *, tenant_id: UUID, conversation_id: UUID
) -> tuple[str | None, str | None]:
    """Who this conversation sends *to*, or why it cannot.

    Returns `(external_user_id, refusal_reason)` -- exactly one is `None`.

    **The identity comes from the contact the ingress created**, which is the
    same `(config_id, external_user_id)` triple the inbound event carried
    (`039` / `040`). That is the durable home B3-2 settled on when it dropped
    the hand-seeded column: who the counterparty is belongs to the event, and
    `contacts` is where the event's identity was recorded.

    Kept separate from `resolve_binding_guid` because the two answer different
    questions -- *through which instance* and *to whom* -- and a caller that
    could not tell their failures apart would report "cannot send" for two
    situations with different owners.
    """

    row = connection.execute(
        """
        SELECT c.external_user_id
          FROM conversation_aggregates a
          JOIN contacts c ON c.tenant_id = a.tenant_id AND c.id = a.contact_id
         WHERE a.tenant_id = %s AND a.id = %s
        """,
        (tenant_id, conversation_id),
    ).fetchone()

    if row is None or not row["external_user_id"]:
        return None, CONVERSATION_HAS_NO_CONTACT
    return str(row["external_user_id"]), None


@dataclass(frozen=True)
class DispatchAddress:
    """Everything a provider adapter needs that the outbox row does not carry.

    `SendCommand` was designed against the M0 simulator, which needed only a
    conversation id: the simulator *was* the address book. A real provider needs
    **which instance sends** (`guid`) and **who receives** (`recipient_id`), and
    neither is a property of the message -- both are resolved at dispatch time
    so the answer is as fresh as the send (ADR-0094 §二).
    """

    guid: str
    recipient_id: str


def resolve_dispatch_address(
    connection: Any, *, tenant_id: UUID, conversation_id: UUID, command: dict[str, Any]
) -> tuple[DispatchAddress | None, str | None]:
    """Where this row actually goes, or the reason it cannot go anywhere.

    Returns `(address, refusal_reason)` -- exactly one is `None`, the same shape
    as the two resolvers it composes, because a caller that had to tell three
    shapes apart would get one of them wrong.

    **The two row kinds are addressed from different places, and that asymmetry
    is the whole point of this function** (T90 / ADR-0095 §三):

    | row | which instance | who receives |
    |---|---|---|
    | customer | the conversation's binding | the conversation's contact |
    | manager | **`recipient.config_id`** | **`recipient.external_user_id`** |

    A manager row's conversation id is borrowed to satisfy a foreign key. Taking
    *either* half of the address from it would deliver a merchant's backlog
    summary into that merchant's customer's chat -- the exact accident
    `unroutable_reason` was written to prevent, and it would be reintroduced
    here, one layer further down, by a resolver that treated all rows alike.

    **`unroutable_reason` is still the gate that runs first.** It decides whether
    the row has an address at all; this decides what that address resolves to.
    Both exist because they fail for different reasons and an operator needs to
    tell "the producer wrote a bad row" from "the binding is not ready".
    """

    if is_customer_row(command):
        guid, refusal = resolve_binding_guid(
            connection, tenant_id=tenant_id, conversation_id=conversation_id
        )
        if guid is None:
            return None, refusal
        recipient_id, refusal = resolve_recipient_id(
            connection, tenant_id=tenant_id, conversation_id=conversation_id
        )
        if recipient_id is None:
            return None, refusal
        return DispatchAddress(guid=guid, recipient_id=recipient_id), None

    # A manager row. `unroutable_reason` has already established that
    # `recipient` is a mapping carrying all of `RECIPIENT_FIELDS` non-blank, so
    # this reads them rather than re-validating -- two copies of that check
    # would be two places to change when the field list moves.
    recipient = command["recipient"]
    guid, refusal = guid_for_config(
        connection, tenant_id=tenant_id, config_id=recipient["config_id"]
    )
    if guid is None:
        return None, refusal
    return DispatchAddress(guid=guid, recipient_id=str(recipient["external_user_id"])), None


def customer_words(content: dict[str, Any] | None) -> str | None:
    """The customer's own words out of an envelope's content, if there are any.

    **Deliberately not gated on `content["type"]`.** It used to be
    `content.get("text") if content.get("type") == "text" else None`, which was
    correct while `text` was the only content type that carried words. It stopped
    being correct in B3-2b: a mixed message (**T201**) arrives as
    `type: "image"` **with the customer's sentence in `text`**, so the type gate
    would drop the words while keeping the picture -- half a message lost, one
    layer below the boundary that exists to prevent exactly that loss.

    Extracted into a function rather than fixed in place because the inline
    form could not be tested without driving the whole pipeline, **which is why
    the type gate had no test in the first place**; and because a bare
    `content.get("text")` reads like a simplification. The next person's
    "surely this should only apply to text messages?" is answered here rather
    than by archaeology.

    Returns `None` when there is nothing to quote -- an uncaptioned picture is
    a real message with no words in it, and `manual_task_notice` already says
    only what it knows when it gets `None`.
    """

    if not content:
        return None
    text = content.get("text")
    return text if isinstance(text, str) and text else None


def _lease_deadline(
    anchor: datetime, lease_expires_at: datetime, db_now: datetime
) -> datetime:
    """When this process should stop believing it holds the lease.

    Three instants, two clocks. `lease_expires_at` and `db_now` are both the
    database's; `anchor` is this process's, read immediately before the
    statement that produced the other two. Subtracting the two database
    readings leaves the remaining lease with **no clock offset in it** -- that
    difference is the whole point of T183 -- and adding it to `anchor` puts the
    answer back on the clock the caller will compare it against.

    It is a function rather than three lines inside `_claim_one` because of the
    independent review's P2-1: the tests that inject a clock offset had
    reimplemented this arithmetic in the test file, in a form that made their
    own conclusion an identity. They asserted a property of the copy. Reverting
    `_claim_one` to the pre-T183 subtraction left every one of them green.
    Now they call this.

    The caller owes one guarantee, and it is a direction, not a bound: `anchor`
    must be read **before** the database reads `db_now`, so that every source of
    error -- the round trip, the scheduler, a slow statement -- can only make
    the returned deadline earlier than the truth. Earlier is safe: the
    dispatcher gives up a lease it still holds. Later is the double send.
    """

    return anchor + (lease_expires_at - db_now)


def _initial_aggregate(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    profile_release_id: UUID,
    profile: dict[str, Any],
    config_id: str | None = None,
    default_control_mode: str = "ai",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    definition = profile["target_frames"][0]
    frame_id = uuid4()
    return {
        "id": str(conversation_id),
        "tenant_id": str(tenant_id),
        "state_version": 0,
        "lifecycle": "open",
        # **The tenant's standing decision, not a constant** (B3 #10/#11,
        # migration 037). It defaulted to `"ai"` here since M0 and the column
        # defaults to `'ai'` for exactly that reason -- the policy starts by
        # describing what this line already did. The parameter keeps its
        # default so the dozen fixtures that build an aggregate without a
        # tenant row keep meaning what they meant.
        "control_mode": default_control_mode,
        "generation": 0,
        "profile_release_id": str(profile_release_id),
        # T24. Carried from the inbox event, which is the only place the binding
        # was ever known. `None` for a conversation whose first event predates
        # migration 008, or one opened by a timer; a repin reports those as
        # unlinked rather than moving them on a guess.
        "config_id": str(config_id) if config_id else None,
        "target_stack": [
            {
                "id": str(frame_id),
                "definition_id": str(definition["id"]),
                "goal": str(definition["goal"]),
                "status": "active",
                "slots": {},
                "slot_evidence": {},
                "entered_at": _iso(now),
                "last_progress_at": _iso(now),
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": str(definition["return_confirmation"]["policy"]),
                "frame_generation": 0,
            }
        ],
        "focus": {},
        "commitment_refs": [],
        "last_inbound_at": None,
        "last_outbound_at": None,
        "pending_question": None,
        # **Both are constant `None`, deliberately, and ADR-0100 §二.3 asks for
        # exactly this note.**
        #
        # `summary_ref` is where a rolling summary would be referenced, and
        # ADR-0100 §二 moved rolling summary out of M1b: it is the E4 memory
        # question (who writes the summary, where it lives, what happens when it
        # is wrong), not a field somebody forgot to fill. `memory_cursor` is the
        # other half of the same absence.
        #
        # ~~**The consequence reaches further than two empty fields, which is
        # what ADR-0122 is about**: nothing anywhere puts prior turns into a
        # prompt, so a model request does not grow with the length of the
        # conversation -- and that is why B4-6's "context budget" had nothing to
        # bound and was closed as already delivered. **Whoever fills either of
        # these fields is the batch that has to build that budget**, in the same
        # batch, because that is the batch that creates the growth.~~
        #
        # **CORRECTED 2026-09-20 (T200 / E4 batch 4).** The struck paragraph is
        # what this said until this batch, and it is kept because a silently
        # rewritten note costs the whole file its credibility.
        #
        # **A prompt now carries prior turns**: `conversation_summaries` holds
        # one line per turn and `_conversation_summary` feeds the most recent
        # ones into all three steps. So the sentence above is false, and
        # **ADR-0122 §6.1 said this batch would be the one to falsify it**
        # ("谁把历史写进提示词，谁就让这段实测失效").
        #
        # **The budget came in the same batch, which is what §三 demanded**:
        # `runtime.summary_prompt.SUMMARY_PROMPT_BYTES` bounds the share and
        # `material_selection.SUMMARY_BYTES_RESERVED` takes it out of the pool.
        # The request therefore still does not grow without bound -- it grows to
        # 2,400 bytes of summary and stops. ADR-0122 §四.2 ("不追认 M1b 的上下文
        # 安全") comes due with it.
        #
        # ⚠️ **`summary_ref` below is still `None`, and now that is a smaller
        # claim than it was.** The lines are addressed by
        # `(tenant_id, conversation_id)`, not by a ref on the aggregate; filling
        # it would add a second address for the same rows and nothing reads it.
        # **`memory_cursor` is untouched** -- the rolling summary is not the
        # memory cursor, and this batch did not build one.
        "memory_cursor": None,
        "summary_ref": None,
        "outcome_ref": None,
        "annotation_refs": [],
        "active_intent_refs": [],
        "scheduled_event_refs": [],
        "last_transition": {
            "event_id": str(UUID(int=0)),
            "transition_id": str(uuid4()),
            "reason": "aggregate_created",
            "policy_version": "m0-policy-v1",
            "model_version": "not-invoked",
            "profile_version": str(profile["identity"]["version"]),
        },
    }


def _reschedule_after_quiet_hours(
    connection: Any,
    prepared: PreparedEvent,
    *,
    reason_code: str,
    next_allowed_at: datetime | None,
) -> int:
    """静默时段拒掉的那次跟进，排到时段结束的那一刻再来一次（ADR-0148）。

    **只对 `PROACTIVITY_QUIET_HOURS` 这一个理由码做。** 另外两种拒绝都不该重排：
    退出信号是一条关于这通对话的常驻拒绝；一个超窗的 `silence_check`
    **等下去也不会变得可发**（RFC §6.4 的那条绕行禁令）。
    `quiet_hours` 是三者里唯一「不是现在」的那一个 ——
    它也是唯一**说得出自己何时不再为真**的那一个。

    ⚠️ **`dedupe_key` 带后缀，不走 `dedupe_template`。** 原来那一行已经
    `CONSUMED`，而唯一键是 `(tenant, conversation, generation, dedupe_key)`：
    照原键插会 `ON CONFLICT` 命中它、而 `DO UPDATE … WHERE status = 'PENDING'`
    不成立，**于是什么都不会发生**。加后缀是为了让它成为新的一行，
    **而不是自由形式的模板** —— 它由平台构造，商家改不到。

    客户在静默期间自己说了话，代次推进，这一行跟着作废 —— 那正是要的：
    他都开口了，就不必再补一次昨晚没发出去的跟进。
    """

    if reason_code != QUIET_HOURS_REASON or next_allowed_at is None:
        return 0
    scheduled_id = prepared.envelope.get("scheduled_event_id")
    if scheduled_id is None:
        return 0
    row = connection.execute(
        """
        SELECT kind, dedupe_key, target_frame_id, commitment_id, payload_ref
          FROM scheduled_events
         WHERE tenant_id = %s AND id = %s
        """,
        (prepared.tenant_id, UUID(str(scheduled_id))),
    ).fetchone()
    if row is None:
        return 0
    return _persist_scheduled_events(
        connection,
        tenant_id=prepared.tenant_id,
        conversation_id=prepared.conversation_id,
        profile_release_id=UUID(str(prepared.aggregate["profile_release_id"])),
        aggregate_generation=int(prepared.aggregate["generation"]),
        target_stack_version=int(prepared.aggregate["state_version"]),
        transition_id=UUID(str(prepared.aggregate["last_transition"]["transition_id"])),
        drafts=[
            ScheduledEventDraft(
                rule_id=str((row["payload_ref"] or {}).get("rule_id") or ""),
                kind=str(row["kind"]),
                due_at=next_allowed_at,
                dedupe_key=f"{row['dedupe_key']}:after_quiet_hours",
                target_frame_id=UUID(str(row["target_frame_id"])),
                commitment_id=(
                    UUID(str(row["commitment_id"])) if row["commitment_id"] else None
                ),
            )
        ],
    )


def _persist_scheduled_events(
    connection: Any,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    profile_release_id: UUID,
    aggregate_generation: int,
    target_stack_version: int,
    transition_id: UUID,
    drafts: list[ScheduledEventDraft],
) -> int:
    """Write the rows a `TimeRule` asked for. **B4-2 / ADR-0096 §二.1.**

    The first production writer of `scheduled_events`. Everything the row needs
    is a fact of the transition that is creating it -- including
    `created_by_transition_id`, which ADR-0096 §四.3 names as the evidence that
    an event was created *by a transition* rather than proposed by a model.

    **A repeat within one generation re-anchors the row rather than being
    dropped** (user ruling 2026-09-09, after independent review P1-1). The key
    `(tenant_id, conversation_id, aggregate_generation, dedupe_key)` is identical
    for every delivery in a turn, because `dedupe_template` carries no ordinal --
    so `DO NOTHING` left `due_at` pinned to the **first** outbound of the turn
    while `last_outbound_at` moved on with each one. For the shipped 75-second
    `silence_check` that means firing 35 seconds after the last message when two
    are 40 seconds apart, and firing *before the second message is even sent*
    when a retry puts 75 seconds between them -- the very failure this batch's
    design argument is written against, reproduced inside a single turn.
    ADR-0105 §零.2 anchors silence on the platform's **last** outbound; first-wins
    measured it from the first.

    **`WHERE status = 'PENDING'` is the whole of the caution.** A row already
    `CLAIMED` or `ENQUEUED` is in flight -- the scheduler holds a lease on it or
    an inbox row already exists -- and moving its deadline underneath that would
    be a second writer to a row someone else owns. Those are left exactly as they
    are, and the conflict then updates nothing and returns nothing.

    **The re-anchor carries the whole as-of-delivery snapshot, not just the
    deadline** (independent re-review 2026-09-09, P2-1). Updating `due_at` alone
    left a row whose `created_by_transition_id` and `target_stack_version` were
    the *first* delivery's -- and `target_stack_version` is what the consumer
    asks "was the stack this was scheduled for still there" with (RFC §6.4,
    ADR-0098), so a freshly re-anchored row would read as staler than it is. The
    key columns are of course not touched.

    **`GREATEST` rather than assignment** (P3-1): `last_outbound_at = now()` is
    the transaction timestamp, so two dispatchers delivering for one
    conversation serialise on the aggregate row but the second one to commit may
    carry the *earlier* instant. Taking the later of the two makes "the timer
    follows the last outbound" true by construction instead of by lock order.

    Across generations it is deliberately **not** idempotent -- a new customer
    message is a new generation, and the previous generation's pending events
    have just been invalidated by the caller.
    """

    # Counts rows this call **left in place as PENDING with this due time** --
    # inserted or re-anchored, which the caller has no reason to tell apart. A
    # conflict against a `CLAIMED`/`ENQUEUED` row updates nothing and returns
    # nothing, so it is not counted, which is the honest answer: that row's
    # deadline is not the one this delivery asked for.
    written = 0
    for draft in drafts:
        inserted = connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, commitment_id, payload_ref, status,
                created_by_transition_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s)
            ON CONFLICT (tenant_id, conversation_id, aggregate_generation, dedupe_key)
            DO UPDATE SET due_at = GREATEST(scheduled_events.due_at, EXCLUDED.due_at),
                          created_by_transition_id = EXCLUDED.created_by_transition_id,
                          target_stack_version = EXCLUDED.target_stack_version,
                          commitment_id = EXCLUDED.commitment_id,
                          payload_ref = EXCLUDED.payload_ref
             WHERE scheduled_events.status = 'PENDING'
            RETURNING id
            """,
            (
                tenant_id,
                uuid4(),
                conversation_id,
                draft.due_at,
                draft.kind,
                draft.dedupe_key,
                profile_release_id,
                aggregate_generation,
                draft.target_frame_id,
                target_stack_version,
                # **11d: the column stops being decorative.** It has existed
                # since `001_m0.sql:143` with no writer at all -- T138 registered
                # it as "a dangling column" -- and migration 064 puts a foreign
                # key on it in the same batch that starts filling it, because a
                # constraint on a column nothing writes cannot be violated and
                # therefore proves nothing.
                #
                # `None` for every conversation-wide rule, and that is the
                # honest value: a `silence_check` has no commitment.
                draft.commitment_id,
                # **The rule id, and nothing copied from the rule** (T205's
                # lesson applied before it can be repeated). `strategy` lives on
                # the rule, the rule lives in the profile release, and the
                # release is pinned on this row -- so B4-4 can read the strategy
                # without a second copy that can disagree with the first.
                Jsonb({"rule_id": draft.rule_id}),
                transition_id,
            ),
        ).fetchone()
        if inserted is not None:
            written += 1
    return written


#: 一条承诺该实现 `commitment_due_at` 上**哪一种** `emit_kind`（T364，2026-09-19）。
#:
#: **`commitments.party` 的枚举有四个值**（`ai` / `customer` / `employee` /
#: `external`），而 `emit_kind` 里只有两个与承诺对应。
#: **这里只写认得的那两个，认不出的一条都不实现**（fail-closed）——
#: `employee` / `external` 那两族今天没有写入方，
#: 而给它们默认一种 kind 等于替商家决定「员工欠的东西到期该催谁」。
#:
#: ⚠️ **不写成 `if party == "ai"` 的分支**：那种写法在 `employee` 有了写入方
#: 的那一天会静默地走进 `else`，而这张表会让它**落在 `None` 上**。
KIND_BY_PARTY = {
    "customer": "customer_promise_due",
    "ai": "ai_commitment_due",
}


def _commitment_drafts(
    prepared: PreparedEvent, rows: Sequence[dict[str, Any]]
) -> list[ScheduledEventDraft]:
    """One anchor call **per commitment**, because the anchor is per commitment.

    `frame_entered_at` and `last_outbound_at` are one moment the conversation
    carries, so one call produces every row they want. `commitment_due_at` is a
    moment each ledger row carries of its own -- two promises in a turn are two
    deadlines -- and the producer takes one anchor value at a time, so this is a
    loop rather than a wider call.

    **A commitment with no `due_at` contributes nothing**, which is a real case
    rather than a defensive check: the ledger admits commitments whose deadline
    the customer never named ("I'll send it over"), and `063` keeps that column
    nullable for exactly those. A timer needs a moment; there isn't one.

    ## ⚠️ 按党派筛 `emit_kind`（T364，2026-09-19）

    `commitment_due_at` 这个锚点上**两种 `emit_kind` 都合法**
    （`customer_promise_due` / `ai_commitment_due`），而规则是商家按帧声明的。
    不筛的话，**一条 AI 的承诺会把客户那种规则也实现出来** ——
    写得进、外键过得去、`commitment_id` 也是真的，
    **只有那一行的 `kind` 是错的**，而没有任何东西会红：
    到期之后跑的是「催客户」那条路，而欠东西的是我们自己。
    **这正是 `CLAUDE.md` 第三节那个形状**，由
    `test_an_ai_commitment_does_not_materialise_the_customer_rule` 钉住。

    **判据是那一行自己的 `party`，不是调用点传了哪一族** ——
    调用点传两族（客户那几行与 AI 那几行一起），
    而「一条承诺该实现哪种 kind」是那条承诺自己的属性。
    """

    drafts: list[ScheduledEventDraft] = []
    for row in rows:
        due_at = row.get("due_at")
        if due_at is None:
            continue
        wanted = KIND_BY_PARTY.get(str(row.get("party")))
        parsed = (
            due_at
            if isinstance(due_at, datetime)
            else datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
        )
        drafts.extend(
            draft
            for draft in drafts_for_anchor(
                profile=prepared.profile,
                aggregate=prepared.aggregate,
                anchor="commitment_due_at",
                anchor_at=parsed,
                now=datetime.now(UTC),
                commitment_id=UUID(str(row["id"])),
            )
            # **认不出党派就一条都不实现**（fail-closed）：
            # `party` 的枚举有四个值（`ai` / `customer` / `employee` /
            # `external`），而只有两个有对应的 `emit_kind`。
            # 放过去的那一侧会写出一行指错 kind 的定时器。
            if wanted is not None and draft.kind == wanted
        )
    return drafts


def _frame_entry_drafts(prepared: PreparedEvent) -> list[ScheduledEventDraft]:
    """The active frame's `frame_entered_at` rules, or nothing.

    Separate from the call site so the "no active frame / unreadable
    `entered_at`" case is one `return []` rather than a conditional expression
    evaluated twice.
    """

    frame = active_frame(prepared.aggregate)
    if frame is None:
        return []
    entered_at = frame.get("entered_at")
    parsed = (
        datetime.fromisoformat(str(entered_at).replace("Z", "+00:00")) if entered_at else None
    )
    if parsed is None:
        return []
    return drafts_for_anchor(
        profile=prepared.profile,
        aggregate=prepared.aggregate,
        anchor="frame_entered_at",
        anchor_at=parsed,
        now=datetime.now(UTC),
    )



#: The two ways a conversation gets an owner for its terminal turn instead of a
#: person, and what each one writes down.
#:
#: **ADR-0031 (closing frame) and ADR-0099 (platform no-goal frame).** Both
#: branches replaced one that wrote `conversation.handoff`, so without a row of
#: their own the batches would have *removed* an audit event and left the stack
#: shape as the only evidence that anything happened.
_FRAME_ENTRY_AUDIT = {
    "closing": (
        "conversation.closing_frame_entered",
        "ROOT_TARGET_FRAME_COMPLETED_CLOSING_FRAME_ENTERED",
    ),
    "no_goal": (
        "conversation.no_goal_frame_entered",
        "ROOT_TARGET_FRAME_COMPLETED_NO_GOAL_FRAME_ENTERED",
    ),
}


def _write_frame_entry_audit(
    connection: Any, prepared: PreparedEvent, entry: dict[str, Any] | None
) -> None:
    """Record that this turn entered a frame rather than handing off.

    **Called from both places the transition is persisted**, and that is the
    point. It used to live only in the sending path's transaction -- but a
    terminal turn is normally *silent* (`wait` / `no_message`), and a silent
    turn returns through `_persist_no_send`. So the frame entry was written to
    the aggregate while the row explaining it was not, on exactly the turns that
    matter most. Found by B4-7's own end-to-end case; the closing frame had the
    same hole since ADR-0031's batch.
    """

    if entry is None:
        return
    action, reason_code = _FRAME_ENTRY_AUDIT[str(entry["kind"])]
    evidence = {key: value for key, value in entry.items() if key != "kind"}
    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES (%s, %s, NULL, %s, 'conversation_aggregate', %s, %s, %s, %s)
        """,
        (
            prepared.tenant_id,
            uuid4(),
            action,
            prepared.conversation_id,
            UUID(str(prepared.envelope["correlation_id"])),
            reason_code,
            Jsonb(evidence),
        ),
    )


#: 「活着」的意图状态 —— **`active_intent_refs` 数的就是这些**（RFC §3.2）。
#:
#: 终态四个（`APPLIED` / `DENIED` / `COMPENSATED` / `FAILED`）不在里面；
#: `UNKNOWN` **在**里面，因为它的意思正是「还没完，要对账」（§7.1）。
#:
#: **`COMPENSATING` 今天不可达，而它有意留着**（批 4，2026-09-15）：
#: 自动补偿那一段（`APPLIED -> COMPENSATING -> COMPENSATED`，RFC:731）**零实现** ——
#: `IntentRecord.inverse` 全仓零写点，所以没有任何一条路径把一个意图置成这个状态。
#: 由 `tests/contracts/test_compensation_chain_absence.py` 钉住。
#:
#: **为什么不顺手删掉它**：删了之后，将来接补偿链的人要记得**回来改这张表** ——
#: 而「要记得改另一处」正是本仓这两天反复在修的那个形状。
#: 留着它，那一批只要写 `inverse` 就会被绊线拦住，并被指到这里。
#: **DB 词表（`001_m0.sql:179` 的 CHECK）同理不动**：图纸仍然要求那两个状态，
#: 为一个没被撤销的词表做一次不可逆迁移，代价方向是反的。
_LIVE_INTENT_STATUSES: tuple[str, ...] = (
    "PROPOSED",
    "APPROVED",
    "EXECUTING",
    "UNKNOWN",
    "COMPENSATING",
)

#: 「活着」的定时事件状态。`CONSUMED` / `INVALIDATED` / `CANCELLED` / `FAILED` 是终态。
_LIVE_EVENT_STATUSES: tuple[str, ...] = ("PENDING", "CLAIMED", "ENQUEUED")


def _record_pending_question(
    prepared: PreparedEvent,
    *,
    plan: ValidatedPlan,
    delivered: Sequence[ProposalDecision],
    materials: Mapping[str, str] | None,
) -> None:
    """这一轮**真的问出去了**一个问题时，把它记在聚合上（RFC §3.2，B1-2）。

    ## 判据是发出去的正文，不是计划的声称（运维缺陷 19，用户 2026-09-23 裁定）

    第一版的前提是「计划点名了槽位」（`required_slot_requests` 非空）——
    **而出事的那份配包每一个策略的 `required_slots` 都是 `[]`**（demo 与
    synthetic-third 两包全空；carrier 有两条非空），于是那张表恒空、
    这里恒早退，`pending_question` 对这样的包**结构性地永远是 `None`**。
    stage 实测的后果：同一轮里第 ① 段刚用了客户的答案，第 ③ 段把问题
    原样又问了一遍，两轮都 `APPLIED`，没有任何拒绝或告警。

    用户裁定：「**平台据真的发出去的正文记**」。所以今天的判据是**确定性的**：
    那条排进出站的客户可见正文里**有问号**（全角或半角）。
    计划点名的槽位降级为**附注**——点了名就带上（`slot`），没点名就 `null`。

    ⚠️ **这同时关掉了 T331 那个洞**：先前记的是「点名了 + 发了消息」，
    **它记的是声称不是问** —— 模型可以每轮点名同一个槽位、每轮发别的话，
    `pending_question` 一路显示「问过、没答」。改成读正文之后，
    没真问就不记，T331 的形状不再存在。

    **`question_text` 截尾 200 字符**，与 `LAST_OUTBOUND_BYTES` 的「留尾不留头」
    同一条理由：问句落在末尾。存它是因为客户可能隔了几轮才回来 ——
    那时 `last_outbound` 已经是别的（比如静默唤醒那句），原问句只有这里还有。

    **两个前提缺一不可**：
    1. 这一轮**真的有一条客户可见的行活到了出站**（`delivered` 里有）——
       被限流闸或策略挡下来的回合**不算问过**，否则下一轮会以为问题在客户那里；
    2. 那条正文里**真的有一个问句**。

    **`question_id` 取那条 `send_message` 的 intent id**：它是这句话在库里的身份，
    顺着它能找到出站行与投递回执 —— 比新造一个 UUID 多一层可追。

    **已经挂着一个未答的问题时，直接替换。** 一个会话同时只有一个「上一句问的」，
    而把旧的留着会让「客户到底在答哪一个」变成一次猜测。

    **定时器轮次也走这里，那是对的**：静默唤醒发出的「您是不是在忙？」
    同样是一个挂着的问题，下一轮同样不该重问。
    """

    # **判据是「这条提案会不会给客户排一行出站」，不是 kind 的字面量**
    # （`test_generation_write_path_registry.py::test_every_hard_coded_send_message_is_gone`
    # 当场抓到了第一版那个 `decision.kind == "send_message"`）。
    # 走注册表意味着：将来 `send_material` 之类的 kind 注册成客户可见投递时，
    # 这一处**不必也不会**被漏掉。
    asked = next(
        (
            decision
            for decision in delivered
            if decision.intent_id is not None and _queues_an_outbox_row(decision)
        ),
        None,
    )
    if asked is None:
        return
    # **正文按发送路径同一个读法渲染**（`channel.customer_text`，规范形状的
    # 唯一定义点）：material_ref 段在这里换成商家原文，所以商家话术里带的
    # 问句同样算问了。
    content = ((asked.proposal or {}).get("typed_payload") or {}).get("content")
    text = customer_text(content, materials=materials) or ""
    if "？" not in text and "?" not in text:
        return
    requested = [str(slot) for slot in plan.document["required_slot_requests"]]
    stack = prepared.aggregate["target_stack"]
    if not stack:
        return
    prepared.aggregate["pending_question"] = {
        "question_id": str(asked.intent_id),
        "frame_id": str(stack[-1]["id"]),
        # 计划点名了就带上；没点名是 `null` —— 一个不绑槽位的问题
        # 由客户的下一条消息结掉（`_settle_pending_question` 的 null 支）。
        "slot": requested[0] if requested else None,
        "asked_at": _iso(datetime.now(UTC)),
        "question_text": text[-200:],
        # **答案到了才填**（`_settle_pending_question`）。`None` 的意思是
        # 「问了，还没答」—— 这一格就是「pending」两个字的实现。
        "answer_evidence": None,
    }


def _settle_pending_question(prepared: PreparedEvent, *, event_id: str) -> None:
    """这一轮把那个槽位填上了的话，给挂着的问题记上**答案证据**（RFC §3.2，B1-2）。

    **判据是槽位自己带的证据，不是「有没有 slot_update」**：
    一条 `slot_updates` 可能被 RFC 4.3 的低置信规则拒掉（`target_stack.apply_slot_updates`），
    那时槽位没变，问题当然还挂着。所以这里读的是**写回之后**那个槽位的
    `evidence_event_id` —— 它等于本轮事件 id，才算「这一轮答的」。

    **答完不清空，只填 `answer_evidence`。** 图纸把「answer evidence」列成这个字段的一部分，
    清空就把它丢了；而「还挂着没答」的判据是 `answer_evidence is None`，不是字段存不存在。
    下一次问出去的问题会**整条替换**它。
    """

    pending = prepared.aggregate.get("pending_question")
    if not isinstance(pending, dict) or pending.get("answer_evidence") is not None:
        return
    if pending.get("slot") is None:
        # **不绑槽位的问题，客户回话就是答**（运维缺陷 19 之后 `slot` 可为
        # `null` —— 判据从「计划点名了槽位」改成「正文里有问句」，
        # 而一个自由问句没有可核对的槽）。
        #
        # ⚠️ **只有客户消息算答**：定时器轮次也走这条管线，而静默唤醒
        # 不是客户在说话 —— 用它的 event_id 结掉问题，会把「他一直没答」
        # 写成「答了」，正反两头都错。
        #
        # 「回了话但没答到点上」在这一支里不存在 —— 没有槽就没有「点」，
        # 他开口了，对话就继续了；要不要追问由模型看正文判断。
        if str(prepared.envelope.get("event_type")) != "customer_message":
            return
        pending["answer_evidence"] = str(event_id)
        pending["answered_at"] = _iso(datetime.now(UTC))
        return
    slot = str(pending.get("slot"))
    for frame in prepared.aggregate["target_stack"]:
        if str(frame["id"]) != str(pending.get("frame_id")):
            continue
        if frame["slots"].get(slot) in (None, ""):
            return
        # **值与证据是两张表**（`target_stack.apply_slot_updates`）：
        # `frame["slots"][name]` 装的是**值本身**（一个标量，不是 dict），
        # 而 `confidence` / `evidence_event_id` / `source` 在
        # `frame["slot_evidence"][name]` 里。第一版我把值当成 dict 去读它的证据，
        # 于是「答了」这一步永远不触发 —— 连库那条三回合用例当场量到了。
        evidence = frame.get("slot_evidence", {}).get(slot)
        if not isinstance(evidence, dict):
            return
        # **D3（2026-09-15）之后这里是一个数组**，而判据没变：
        # 「这一轮答的」= **最新那条证据**是本轮事件。`_slot_evidence_ids` 两种形状都读
        # （存量行仍是单数键，不追溯改写）。
        #
        # **这一处是 D3 的漏网，而它被连库那条三回合用例抓住了** ——
        # 与上面那段注释记的上一次是同一个地方、同一种失败：
        # 读错证据的形状，于是「答了」这一步永远不触发。
        recorded_ids = _slot_evidence_ids(evidence)
        if not recorded_ids or str(recorded_ids[-1]) != str(event_id):
            # 这个槽位早就有值了（别的轮次填的）——那不是**这一轮**的回答。
            return
        pending["answer_evidence"] = str(event_id)
        pending["answered_at"] = _iso(datetime.now(UTC))
        return


def _slot_evidence_ids(evidence: dict[str, Any]) -> list[str]:
    """一个槽位的证据 id 列表，**两种形状都读**（D3，2026-09-15）。

    新写回产出 `evidence_event_ids`（数组，最新的在末尾）；
    D3 之前写下的行是单数 `evidence_event_id`，**它们不追溯改写**（T338 那一族）。
    """

    ids = evidence.get("evidence_event_ids")
    if isinstance(ids, list):
        return [str(item) for item in ids]
    single = evidence.get("evidence_event_id")
    return [str(single)] if single else []


def _focus_projection(prepared: PreparedEvent) -> dict[str, Any]:
    """这一轮的 `focus`（RFC §3.2，B1-1）—— **今天只有 `contact` 一类**。

    图纸那一行是「当前 **contact/product/order/topic** 的 typed refs」，
    而在此之前这个字段从会话建出来就是 `{}`、零写点零读点。

    | 类 | 今天 | 解除条件 |
    |---|---|---|
    | `contact` | ✅ `conversation_aggregates.contact_id`（可空：客户还没被识别） | —— |
    | `topic` | ❌ **2026-09-15 撤下** | 见下 |
    | `product` | ❌ **指不到东西** | 出现商品权威表（§3.2 要 typed **ref**，ref 得指向一行） |
    | `order` | ❌ 同上 | 出现订单权威表 |

    ## `topic` 那一支为什么撤下来，而不是修好（用户 2026-09-15 裁定）

    它**从落地起就是死的**（T349）：`apply_slot_updates` 写的是
    `frame["slots"][name] = update["value"]`（**一个标量**），
    而这里先前按 `isinstance(recorded, dict)` 读 —— 两者不可能同时成立，
    所以 `focus["topic"]` **恒为 `None`**，而它不报错、不变红、每轮照样进 plan 提示词。

    **撤下而不是接上，是因为聚合契约那一侧同时裁了形状**：`focus` 现在是
    **闭合键集**，每个值要么 `null` 要么一个 **typed ref**。
    而 topic 今天的语义是「那个槽位的**值**」——一个字符串，**不是 ref，指不到一行**。
    要把它加回来，先得给 topic 一个 id；**那时改 schema 就是那次裁定的执行者**。

    **顺带说清它省下的字节**：topic 一旦真的有值，它就是
    `frame["slots"][material_topic_slot]` **在同一个请求里的第二份副本**
    （实测 `42 + len(value)`，437 汉字时 +1,353），
    而槽位值今天的逐值上界还没裁。

    **那两条喂 dict 的用例一并删了**：它们走的是生产永远走不到的路
    （`AGENTS.md`「那条用例喂的东西必须是生产里会出现的形状」）。
    """

    return {
        "contact": (
            {"contact_id": str(prepared.contact_id)} if prepared.contact_id is not None else None
        ),
    }


def _refresh_reference_projection(connection: Any, prepared: PreparedEvent) -> None:
    """把 `active_intent_refs` / `scheduled_event_refs` 刷成**权威表此刻的实况**。

    **RFC §3.2 对这两个字段只说了一句**：「只存引用，事实各自在权威表」。
    在此之前它们从会话创建那一刻起就是 `[]`，**零写点、零读点**
    （核心图纸对比 2026-09-13，B1-5）—— 也就是说「这通对话上有哪些活着的意图」
    今天只能靠扫两张表回答，而聚合上那两个数组是摆设。

    **实现取「从权威表现算」而不是「逐笔增删」**，理由是那条验收：
    数组必须与权威表里**活着的那批逐一对上**。增删式维护做得到一次对上，
    做不到永远对上 —— 漏一条删除就是一条永远挂着的引用，
    而那种错在数据上长得像「这条意图还没结」。**现算没有这个失败面。**

    **它记的是「这一轮收口时」的实况，不是「此刻」**：投递侧把一条意图推到
    `APPLIED` 时不会回头改聚合（那要跨 CAS 抢同一行）。所以这两个数组的语义是
    **「上一次回合结束时还活着的引用」**，下一轮自动纠正。
    这一条写在这里，免得下一个人把它读成实时视图；登记 **T322**。

    **调用点在每条路径的末尾，不在 `_apply_aggregate_transition` 里** ——
    那个函数跑在事务的第一句，而这一轮自己的意图与定时事件都在它之后写入，
    放进去会永远漏掉本轮那几条。两处调用由
    `tests/contracts/test_reference_projection_wiring.py` 钉住。
    """

    live_intents = connection.execute(
        """
        SELECT id FROM intent_records
         WHERE tenant_id = %s AND conversation_id = %s AND status = ANY(%s)
         ORDER BY created_at, id
        """,
        (prepared.tenant_id, prepared.conversation_id, list(_LIVE_INTENT_STATUSES)),
    ).fetchall()
    live_events = connection.execute(
        """
        SELECT id FROM scheduled_events
         WHERE tenant_id = %s AND conversation_id = %s AND status = ANY(%s)
         ORDER BY due_at, id
        """,
        (prepared.tenant_id, prepared.conversation_id, list(_LIVE_EVENT_STATUSES)),
    ).fetchall()

    prepared.aggregate["active_intent_refs"] = [str(row["id"]) for row in live_intents]
    prepared.aggregate["scheduled_event_refs"] = [str(row["id"]) for row in live_events]
    # **`focus` 与那两个数组同属「聚合上的投影」**（RFC §3.2，B1-1）：
    # 都是从别处现算、每轮收口时刷新的东西，所以共用这一次写回，
    # 而不是各写各的 UPDATE（`AGENTS.md`「每处都要写成同一个形状」）。
    prepared.aggregate["focus"] = _focus_projection(prepared)

    # **只改 `state`，不碰 `state_version` / `generation`**：CAS 已经由
    # `_apply_aggregate_transition` 在本事务开头做过，这一句是同一次转换的一部分，
    # 再 bump 一次会让下一个读者以为发生了两次转换。
    connection.execute(
        """
        UPDATE conversation_aggregates
           SET state = %s, updated_at = now()
         WHERE tenant_id = %s AND id = %s
        """,
        (Jsonb(prepared.aggregate), prepared.tenant_id, prepared.conversation_id),
    )


def _admission(prepared: PreparedEvent, understanding: Mapping[str, Any]) -> dict[str, Any]:
    """`post_understanding` 那道确认闸的**调用点**，两处 `understand()` 之后各一次。

    判据与理由都在 `commitment_ledger.admit_customer_commitments` 里 ——
    这里只负责把结果挂到 `prepared` 上，因为写它的地方（状态迁移）离这里很远，
    而中间隔着七个 no-send 返回。**与 `slot_update_refusals` 同一个搬运理由。**
    """

    rows, refusals = admit_customer_commitments(
        understanding=understanding,
        event_type=str(prepared.envelope.get("event_type") or ""),
        source_event_id=str(prepared.envelope["event_id"]),
        conversation_id=prepared.conversation_id,
        generation=prepared.previous_generation,
    )
    return {
        "admitted_commitments": tuple(rows),
        "commitment_refusals": tuple(refusals),
    }


def _with_ai_commitments(
    prepared: PreparedEvent,
    *,
    strategy_id: str | None,
    said_something: bool,
    #: 这一轮的时刻。**在这里读一次时钟，往下传** —— 判据在纯函数里，
    #: 而纯函数不读时钟（见 `admit_ai_commitments` 的形参注释）。
    now: datetime | None = None,
) -> PreparedEvent:
    """把这一轮我们自己欠下的那几条挂到 `prepared` 上。

    **判据与理由都在 `commitment_ledger.admit_ai_commitments` 里** ——
    这里只负责搬运，与 `_admission` 逐字同一条理由：
    写它的地方（状态迁移）离这里很远，中间隔着七个 no-send 返回。

    **`strategy_id` 为 `None` 时什么都不做**：计划还没成立就没有打法，
    而 `outcome_rules` 是按打法索引的 —— 那时不是「没有承诺」，
    是**还没到能判断的那一步**。
    """

    if strategy_id is None:
        return prepared
    rows, refusals = admit_ai_commitments(
        profile=prepared.profile,
        strategy_id=strategy_id,
        source_event_id=str(prepared.envelope["event_id"]),
        conversation_id=prepared.conversation_id,
        generation=prepared.previous_generation,
        said_something=said_something,
        now=now or datetime.now(UTC),
    )
    if not rows and not refusals:
        return prepared
    return replace(
        prepared,
        ai_commitments=tuple(rows),
        ai_commitment_refusals=tuple(refusals),
    )


def _apply_aggregate_transition(connection: Any, prepared: PreparedEvent) -> None:
    """CAS the aggregate forward and invalidate superseded timers.

    Shared by the send and the no-send paths: whether or not this event produces
    a message, the state transition and the generation bump must be persisted
    identically, or a rejected plan would silently lose the customer's message.
    """

    current = connection.execute(
        """
        SELECT state_version, generation
          FROM conversation_aggregates
         WHERE tenant_id = %s AND id = %s
         FOR UPDATE
        """,
        (prepared.tenant_id, prepared.conversation_id),
    ).fetchone()
    if (
        current is None
        or int(current["state_version"]) != prepared.previous_state_version
        or int(current["generation"]) != prepared.previous_generation
    ):
        # The lease is released by `InboxProcessor._release_lease`, outside this
        # transaction. Releasing it here looked right and did nothing: raising
        # rolls the release back with everything else.
        raise AggregateConflict("aggregate changed during inference")
    if prepared.admitted_commitments or prepared.commitment_refusals:
        # **在这个事务里，与下面槽位拒绝那一段同一条理由**：台账那几行描述的是
        # 这一次迁移吸收了什么，而一条能在迁移回滚之后活下来的承诺，
        # 描述的是一个没有发生过的回合。
        #
        # **排在 CAS 之前，不是之后**，而这不是风格问题：`commitment_refs` 是
        # 聚合文档里的一格（`conversation_aggregates.state` 那个 jsonb，**不是一列**），
        # 所以它只能随下面那一次 `SET state = %s` 一起写出去。排在之后就要再发一条
        # `UPDATE`，而那条会绕过刚刚成立的那次 CAS。
        # 上面的 `FOR UPDATE` 已经握住了这一行，所以这里是安全的；CAS 失败时
        # 整个事务回滚，台账那几行跟着回滚。
        #
        # **refs 从库里现读，不从刚写的那几行攒**：那一格的定义是「活跃/待证据的
        # 承诺 id」，而重放同一条事件时 `ON CONFLICT DO NOTHING` 不再返回行 ——
        # 攒的话 refs 会在重放那一轮变短。
        prepared.aggregate["commitment_refs"] = write_admitted(
            connection,
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            rows=prepared.admitted_commitments,
            refusals=prepared.commitment_refusals,
        )
        validate_contract("conversation-aggregate", prepared.aggregate)
    if prepared.ai_commitments or prepared.ai_commitment_refusals:
        # **我们自己那半**（2026-09-18）。契约逐字写着「`ai` 的未来动作必须建账」，
        # 而在这一行之前 `git grep` 写入方 **0 个**。
        #
        # **第二次调用回的是完整的 refs**：`write_admitted` 的 refs 是一次
        # `SELECT` 现读（不是攒 `RETURNING`），所以它包含上面那一批刚写的行。
        prepared.aggregate["commitment_refs"] = write_ai_admitted(
            connection,
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            rows=prepared.ai_commitments,
            refusals=prepared.ai_commitment_refusals,
        )
        validate_contract("conversation-aggregate", prepared.aggregate)

    updated = connection.execute(
        """
        UPDATE conversation_aggregates
           SET state_version = %s, generation = %s, target_stack = %s,
               state = %s, last_inbound_at = %s, updated_at = now()
         WHERE tenant_id = %s AND id = %s AND state_version = %s AND generation = %s
        """,
        (
            prepared.aggregate["state_version"],
            prepared.aggregate["generation"],
            Jsonb(prepared.aggregate["target_stack"]),
            Jsonb(prepared.aggregate),
            prepared.aggregate["last_inbound_at"],
            prepared.tenant_id,
            prepared.conversation_id,
            prepared.previous_state_version,
            prepared.previous_generation,
        ),
    )
    if updated.rowcount != 1:
        raise AggregateConflict("aggregate CAS failed during event transition")
    if prepared.slot_update_refusals:
        # In this transaction on purpose: the refusal describes what this
        # transition did *not* absorb, so a record that could survive a rollback
        # of the transition would be describing a turn that never happened.
        #
        # **The reason code follows the refusals, it is not a constant**
        # (independent review 2026-09-05, F3). There are two kinds now --
        # a low-confidence overwrite and a model trying to revise what a person
        # wrote -- and `target_stack.py` argues at length that an operator who
        # cannot tell them apart will eventually "fix" the second by loosening
        # the first. Writing both under `LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED`
        # would have put that distinction only in the evidence JSON, invisible to
        # anyone searching audit by reason code -- which is how audit is searched.
        #
        # A turn can produce both kinds at once, so the mixed case gets its own
        # code rather than an arbitrary winner: `SLOT_UPDATES_REFUSED` says
        # "look at the evidence", which is true, instead of naming one of two.
        refused_reasons = {
            str(refusal.get("reason", "LOW_CONFIDENCE_OVER_HIGH"))
            for refusal in prepared.slot_update_refusals
        }
        if refused_reasons == {"SLOT_WRITTEN_BY_HUMAN"}:
            refusal_reason_code = "HUMAN_WRITTEN_SLOT_UPDATE_REFUSED"
        elif refused_reasons == {"LOW_CONFIDENCE_OVER_HIGH"}:
            refusal_reason_code = "LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED"
        else:
            refusal_reason_code = "SLOT_UPDATES_REFUSED"
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, %s,
                      'conversation_aggregate', %s, %s, %s, %s)
            """,
            (
                prepared.tenant_id,
                uuid4(),
                # The action is a parameter now because `app.py` selects on
                # the same constant; a literal on each side is two copies of
                # a name that must match for the projection to see anything.
                SLOT_UPDATE_REFUSED_ACTION,
                prepared.conversation_id,
                UUID(str(prepared.envelope["correlation_id"])),
                refusal_reason_code,
                Jsonb({"refusals": list(prepared.slot_update_refusals)}),
            ),
        )
    # **不读规则的 `cancel_on`，一律取消**（T358，2026-09-16 实测）：配包每条时间规则
    # 都必填那一格，而五个值里四个各有一处这样的无条件执行者。
    # **少声明不会让它不发生** —— 契约里 `cancel_on` 那一格逐字写着这件事。
    if prepared.envelope["event_type"] == "customer_message":
        connection.execute(
            """
            UPDATE scheduled_events
               SET status = 'INVALIDATED', invalidated_at = now(),
                   invalidation_reason = 'CUSTOMER_MESSAGE_NEW_GENERATION',
                   invalidated_by_event_id = %s
             WHERE tenant_id = %s AND conversation_id = %s
               AND aggregate_generation < %s
               AND status IN ('PENDING', 'CLAIMED')
            """,
            (
                UUID(str(prepared.envelope["event_id"])),
                prepared.tenant_id,
                prepared.conversation_id,
                prepared.aggregate["generation"],
            ),
        )
    # **B4-2: the rules anchored on `frame_entered_at`, created where that
    # anchor becomes true.** After the invalidation above on purpose -- a
    # customer message invalidates the previous generation's pending events and
    # then this generation's are written, so the two never race for the same
    # dedupe key. The `last_outbound_at` rules are not created here; their
    # anchor does not exist until a message has actually been delivered, and
    # anchoring them to the *previous* outbound would make the shipped
    # 75-second `silence_check` overdue at birth (`time_rules.py`).
    _persist_scheduled_events(
        connection,
        tenant_id=prepared.tenant_id,
        conversation_id=prepared.conversation_id,
        profile_release_id=UUID(str(prepared.aggregate["profile_release_id"])),
        aggregate_generation=int(prepared.aggregate["generation"]),
        target_stack_version=int(prepared.aggregate["state_version"]),
        transition_id=UUID(str(prepared.aggregate["last_transition"]["transition_id"])),
        # **两个锚点的草稿一起写，一次事务**：帧进入那一组是会话级的，
        # 承诺那一组是每行一个。合起来交给同一个写入方，
        # 是因为它们进的是同一张表、同一次 CAS 之后、同一个 generation ——
        # 分两次写会让第二次看不见第一次刚占掉的 dedupe_key。
        drafts=_frame_entry_drafts(prepared)
        + _commitment_drafts(
            prepared,
            stored_due_rows(
                connection,
                tenant_id=prepared.tenant_id,
                conversation_id=prepared.conversation_id,
                # **两族一起传（T364，2026-09-19）**：AI 那几行在此之前
                # **一次都没进过这个函数** —— 于是即便它们有了 `due_at`，
                # `fulfilment: ai_commitment_due` 仍然排不出任何定时器。
                # T364 登记时只说了「没有到期时刻」那一层，
                # **而缺的是两层**，这是第二层。
                dedupe_keys=[
                    str(row["_dedupe_key"])
                    for row in (
                        *prepared.admitted_commitments,
                        *prepared.ai_commitments,
                    )
                    if row.get("_dedupe_key")
                ],
            ),
        ),
    )


def _consume_inbox_event(connection: Any, prepared: PreparedEvent) -> None:
    connection.execute(
        """
        UPDATE inbox_events SET status = 'consumed', processed_at = now(),
               lease_owner = NULL, lease_expires_at = NULL
         WHERE tenant_id = %s AND id = %s
        """,
        (prepared.tenant_id, prepared.inbox_id),
    )
    if prepared.envelope["event_type"] == "timer":
        connection.execute(
            """
            UPDATE scheduled_events SET status = 'CONSUMED'
             WHERE tenant_id = %s AND id = %s AND status = 'ENQUEUED'
            """,
            (prepared.tenant_id, UUID(str(prepared.envelope["scheduled_event_id"]))),
        )


#: **模块级函数，不是 `InboxProcessor` 的方法**：调它的五条路分属两个类
#: （`InboxProcessor` 四条 · `OutboxDispatcher` 一条 ——
#: `_manual_task_for_delivery` 在投递那一侧）。第一版把它挂成了
#: `InboxProcessor` 的方法，于是投递那一处的 `self._file_a_manual_task`
#: 是一个 `AttributeError` —— **而它只在真的跑到那条路时才炸**。
#: 抓到它的是本批那条行为用例，不是任何一条读源码的断言。
def _file_a_manual_task(
    connection: Any,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    #: 调用方自己生成，**不由本函数生成**：`_persist_no_send` 在走到这里
    #: 之前就要用它（`_record_plan_refusal(intent_id=…)` 收的是同一个 id），
    #: 而一个「本函数生成、再返回给你」的写法会让那一处拿到 id 的时刻
    #: 晚于它需要的时刻。
    manual_task_id: UUID,
    reason_code: str,
    #: **本函数不自己 `classify()`** —— `_persist_no_send` 那一处调的是
    #: `classify(reason_code, interruption_relation=…)`，而那个参数对
    #: 词表里的某几个码**会换出另一条分类**。合并时第一版在这里写了
    #: `classify(reason_code)`，**它能跑、能落库、返回的也是一个合法分类**，
    #: 只有 `task_class` / `notify` 两列在那几个码上是错的 ——
    #: 正是 `CLAUDE.md` 第三条那一族。收分类本身，漏传就是 `TypeError`。
    classification: Classification,
    source: str,
    proposal_facts: Mapping[str, Any],
    proposed_by_version: str,
    strategy_id: str,
    target_frame_id: UUID,
    idempotency_key: str,
    preconditions: Mapping[str, Any],
    policy_evidence: Any,
    evidence: Mapping[str, Any],
) -> UUID | None:
    """**引擎侧落一条人工待办的唯一定义点**（T276）。

    ## 它合并的是什么

    「写一条 `intent_records(kind='manual_task')` + 一条 `manual_tasks`」
    这一对 INSERT，在本函数之前**引擎侧有五处各写一遍**：
    `_record_rate_limit`、`_file_a_task_for_a_promise_only_a_person_can_keep`、
    `_file_task_for_unsendable_intents`、`_persist_no_send`、
    `_manual_task_for_delivery`。**不是同一段代码的五个副本，是同一个概念的
    五次独立实现** —— T276 登记时的原话。

    ⚠️ **`api/app.py` 那一处（人工建单）没有并进来，这是有意的**：它的来源是
    人不是引擎，列集合是 10 列不是 8 列（多 `status` / `owner_employee_id` /
    `sla_due_at`），而 owner 与 SLA 在那一侧是**必填**的（freeze B3 #16），
    在这一侧是**一列都不填**（T91：§8 要三样而引擎没有任何一样的来源）。
    **把两者并成一个函数，就要造一个「有时填三列、有时不填」的参数** ——
    而那正是本函数存在的理由所要防的东西。
    `test_manual_task_owner_sla_expiry.py` 按「引擎 1 处 · 控制面 1 处」
    两侧分别钉住。

    ## 为什么它值得合并 —— 那五处已经漂成了三种幂等语义

    **不是理论风险，是 2026-09-20 跑出来的**（五处的幂等键都带 `event_id`，
    所以「一个回合至多一条待办」是五处共同的声称）：

    * `_record_rate_limit` / `_file_task_for_unsendable_intents` /
      `_manual_task_for_delivery` —— `ON CONFLICT` + `RETURNING` + 短路，
      **不落第二条**；
    * `_file_a_task_for_a_promise_only_a_person_can_keep` ——
      `ON CONFLICT` 在，**而没有人读它的答案**：不 `RETURNING`、不短路，
      于是 `manual_tasks` 照写、通知照推；
    * `_persist_no_send` —— **整条 `ON CONFLICT` 都没有**，撞上
      `intent_records` 的 `UNIQUE (tenant_id, idempotency_key)` 会抛。

    中间那一条是本仓「**多传一个不报错**」那一族的一个新形状：
    **该报错的地方一个字都没少写，少的是读它的那一步**。
    `manual_tasks.intent_id` **没有外键**（`034` 里只有 `owner_employee_id`
    那一条），所以那条指向一行不存在的意图的待办不会被任何约束挡下 ——
    它安静地落库，同事收到第二条通知。

    **而这三种语义谁都没写在任何一处的 docstring 里** ——
    三处都写着「一个回合至多一条待办」。

    ## 合并之后只剩一种：`ON CONFLICT` + `RETURNING` + 短路

    **返回 `None` 的含义是「这个幂等键已经落过一条待办了」**，
    而不是「出错了」。⚠️ **调用方拿到 `None` 时不要推通知** ——
    通知是待办的附属品，没落成待办就没有东西可通知。
    这一条是本函数与调用方之间唯一的约定，
    `test_filing_a_task_happens_in_one_place.py` 逐个调用点钉住它。

    ## 每一个参数都没有默认值

    与 `_record_rate_limit` 的 `promises_dropped` 同一条理由，
    而在这里它更硬：本函数有十二个参数，**十二个里没有一个的
    「忘了传」在库里看得出来** —— 一条 `evidence` 为 `{}` 的待办与一条
    「这件事本来就没有上下文」的待办，在拿到它的同事那边长得一模一样。
    **没有默认值，忘了传就是 `TypeError`，在第一次跑到它的用例上当场红。**

    ⚠️ **`classification` 是收进来的，不是本函数算的**，理由在它的参数注释里：
    合并这一批时第一版写的是 `classify(reason_code)`，而五处里有一处
    传了 `interruption_relation`。**那一版能跑**，错的只有两列。

    `reason_code` 与 `source` 不收整个 `proposal`，而是由本函数拼进去 ——
    那两个键是五处逐字相同的结构，**收整个 dict 就等于允许某一处漏掉
    其中一个**，而 `proposal` 是 jsonb，漏一个键不会有任何东西红。
    """

    inserted = connection.execute(
        """
        INSERT INTO intent_records(
            tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
            strategy_id, target_frame_id, idempotency_key, preconditions,
            policy_evidence, manual_task_id
        ) VALUES (%s, %s, %s, 'manual_task', 'PROPOSED', %s, %s, %s, %s, %s, %s,
                  %s, %s)
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
        RETURNING id
        """,
        (
            tenant_id,
            manual_task_id,
            conversation_id,
            Jsonb({"reason_code": reason_code, "source": source, **proposal_facts}),
            Jsonb({"step": "platform", "version": proposed_by_version}),
            strategy_id,
            target_frame_id,
            idempotency_key,
            Jsonb(dict(preconditions)),
            Jsonb(policy_evidence),
            manual_task_id,
        ),
    ).fetchone()
    if inserted is None:
        return None
    # **在 `ON CONFLICT` 的答案里面写，不是在它旁边。**
    # 意图那一行才是幂等的那一行 —— 第二个跑到同一个回合的人插不进去、
    # 拿回 `None`，于是待办只在真的立了意图的那一支上写。
    # 无条件写的话，第二个调用方会拿到一个主键冲突，
    # **而一条本该幂等的路就变成了一次崩溃**
    # （`_manual_task_for_delivery` 合并进来之前那段注释的原话）。
    connection.execute(
        """
        INSERT INTO manual_tasks(
            tenant_id, id, conversation_id, intent_id,
            task_class, notify, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            manual_task_id,
            conversation_id,
            manual_task_id,
            classification.audience_class,
            classification.cadence,
            reason_code,
            Jsonb(dict(evidence)),
        ),
    )
    return manual_task_id


class InboxProcessor:
    def __init__(
        self,
        *,
        database: Database,
        pointer_queue: PointerQueue,
        inference: InferencePort,
        worker_id: str,
        retriever: KnowledgeRetriever | None = None,
    ) -> None:
        self._database = database
        self._queue = pointer_queue
        self._inference = inference
        self._worker_id = worker_id
        #: 知识库检索（裁定三的第四步）。**可选，而 `None` 的意思是「这一格没接」**：
        #: 用 mock 供应商时没有嵌入端点也没有台账，`cli.py` 因此只在真实供应商下给它。
        #: **它是可选参数，所以「生产忘了接」不会有任何东西报错** ——
        #: 拦这一手的是 `tests/contracts/test_retrieval_wiring.py`，
        #: 它读 `cli.run_worker` 的装配，不是读这里。
        self._retriever = retriever

    def prepare(self, *, tenant_id: UUID, inbox_id: UUID) -> PreparedEvent | None:
        # **No process clock in here any more** (T183). The lease is written and
        # reclaimed entirely on the database's, so there is nothing left for
        # this function to read a local time for.
        with self._database.transaction(tenant_id=tenant_id) as connection:
            inbox = connection.execute(
                """
                UPDATE inbox_events AS candidate
                   -- **The lease is written on the database's clock** (T183).
                   -- It used to be a thirty-second offset added to this
                   -- process's own clock and handed over as a parameter -- the
                   -- literal is spelled out nowhere here on purpose, because a
                   -- case below asserts it is gone -- while the
                   -- reclaim predicate three lines down compares it against the
                   -- database's `now()`. That is the same defect this batch
                   -- fixes in the outbox dispatcher, pointing the other way:
                   -- with the database ahead by delta, the stored instant is
                   -- delta *earlier* in database terms, so the lease expires
                   -- early and a second worker can reclaim an event that is
                   -- still being processed. **The database ahead is the offset
                   -- actually measured on this deployment** (T186: +0.43s).
                   SET status = 'processing', lease_owner = %s,
                       lease_expires_at = now() + %s * interval '1 second'
                 WHERE candidate.tenant_id = %s AND candidate.id = %s
                   AND (candidate.status = 'pending' OR
                        (candidate.status = 'processing'
                         AND candidate.lease_expires_at < now()))
                   AND NOT EXISTS (
                       SELECT 1 FROM inbox_events AS earlier
                        WHERE earlier.tenant_id = candidate.tenant_id
                          AND earlier.conversation_id = candidate.conversation_id
                          AND earlier.status IN ('pending', 'processing')
                          AND (earlier.created_at, earlier.id)
                              < (candidate.created_at, candidate.id)
                   )
                RETURNING *
                """,
                (self._worker_id, INBOX_LEASE_SECONDS, tenant_id, inbox_id),
            ).fetchone()
            if inbox is None:
                return None

            # **The control policy, read once per event** (B3 #10/#11,
            # migration 037). Two switches with two different readers: the
            # default mode is consulted only when a conversation is created a
            # few lines below, and `understanding_during_takeover` on every
            # event that arrives while a person holds the conversation. Reading
            # them here rather than at each use keeps one transaction's view of
            # the tenant consistent -- an operator flipping the switch mid-event
            # cannot have it take effect halfway through a turn.
            policy = connection.execute(
                """
                SELECT default_control_mode, understanding_during_takeover
                  FROM tenants WHERE id = %s
                """,
                (tenant_id,),
            ).fetchone()
            if policy is None:
                # The tenant row is gone. `001` makes every aggregate reference
                # it, so this is not a state the rest of the turn can survive.
                #
                # **`return None` does not fail the event, and the first version
                # of this comment said it did** (independent review 2026-09-06,
                # P3-1). The pointer is acknowledged and the inbox row stays
                # `processing` until its lease expires, after which it is
                # republished -- the loop the `PROFILE_NOT_FOUND` branch a few
                # lines below deliberately avoids by marking the row `failed`.
                #
                # It is left as a return rather than given that treatment
                # because **it is unreachable today**: `inbox_events.tenant_id`
                # references `tenants(id)` without `ON DELETE CASCADE`, so a
                # tenant with events cannot be deleted at all. If that ever
                # changes, this needs the `failed` + `error_code` shape, not a
                # comment.
                return None

            profile_row = connection.execute(
                """
                SELECT profile FROM profile_releases
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, inbox["profile_release_id"]),
            ).fetchone()
            if profile_row is None:
                connection.execute(
                    """
                    UPDATE inbox_events
                       SET status = 'failed', error_code = 'PROFILE_NOT_FOUND',
                           lease_owner = NULL, lease_expires_at = NULL
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, inbox_id),
                )
                # Returning, not raising: raising rolls this transaction back, so
                # the terminal state never reached the database and the missing
                # release became a permanent republish loop instead of a fact.
                return None
            profile = profile_row["profile"]
            validate_contract("merchant-profile", profile)

            aggregate_row = connection.execute(
                """
                SELECT * FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                 FOR UPDATE
                """,
                (tenant_id, inbox["conversation_id"]),
            ).fetchone()
            if aggregate_row is None:
                # **Which customer this conversation is with** (freeze B3
                # #23, migration 040, user ruling 2026-09-06 to store it
                # rather than derive it on every read).
                #
                # **Resolved here rather than written by ingress**, because
                # ingress does not own this row: it writes `inbox_events`,
                # and the aggregate does not exist until this line creates
                # it. The identity is the same triple `039` keyed the
                # contact on, read from the event ingress authenticated --
                # so the two cannot disagree about who the customer is.
                #
                # `None` when the envelope carried no identity (the
                # `"unknown"` placeholder is not a person, `039`) and for
                # conversations older than that migration. Both mean "no
                # contact recorded"; neither means "anonymous customer".
                # `inbox["envelope"]` rather than the `envelope` local, which
                # is bound a few lines below this branch: same document, and the
                # contract check that follows still guards it.
                raw_envelope = inbox["envelope"]
                actor = raw_envelope.get("actor") if isinstance(raw_envelope, dict) else None
                external_user_id = (
                    actor.get("actor_id") if isinstance(actor, dict) else None
                )
                contact_row = (
                    connection.execute(
                        """
                        SELECT id FROM contacts
                         WHERE tenant_id = %s AND config_id = %s
                           AND external_user_id = %s
                        """,
                        (tenant_id, inbox["config_id"], external_user_id),
                    ).fetchone()
                    # `UNKNOWN_ACTOR` rather than the literal (independent
                    # review P3-2): ingress made this a named constant so the
                    # placeholder could not be trusted to differ by accident,
                    # and this side has to agree with it or the two services
                    # disagree about who the customer is.
                    if external_user_id and external_user_id != UNKNOWN_ACTOR
                    else None
                )
                aggregate = _initial_aggregate(
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                    profile_release_id=UUID(str(inbox["profile_release_id"])),
                    profile=profile,
                    config_id=inbox["config_id"],
                    default_control_mode=policy["default_control_mode"],
                )
                validate_contract("conversation-aggregate", aggregate)
                connection.execute(
                    """
                    INSERT INTO conversation_aggregates(
                        tenant_id, id, state_version, lifecycle, control_mode, generation,
                        profile_release_id, target_stack, state, config_id, contact_id
                    ) VALUES (%s, %s, 0, 'open', %s, 0, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        inbox["conversation_id"],
                        aggregate["control_mode"],
                        inbox["profile_release_id"],
                        Jsonb(aggregate["target_stack"]),
                        Jsonb(aggregate),
                        aggregate["config_id"],
                        contact_row["id"] if contact_row else None,
                    ),
                )
            else:
                aggregate = aggregate_row["state"]
                # **一个事实一个读者**（2026-09-23，运维缺陷 20）。
                #
                # `last_outbound_at` 有**两份**，而写的那份与读的那份不是同一份：
                #
                # | | 谁写 | 实测 |
                # |---|---|---|
                # | **列** | 投递之后 `UPDATE … SET last_outbound_at = now()` | 有值 |
                # | **JSONB `state`** | 建会话时置 `None`，**此后零写点** | 恒 `None` |
                #
                # 而管线工作用的是 `state` 这一份，于是
                # `_seconds_since(aggregate.get("last_outbound_at"))` **恒回 `0.0`** ——
                # ADR-0149 那一步（主动打破静默前先问一句他为什么不说话）
                # **最要紧的那个输入是个常量，它永远看不到一次长静默**。
                #
                # ⚠️ **而这个 bug 一直没被发现，恰恰因为兜底方向选对了**：
                # `_seconds_since` 读不出就回 `0.0`，模型因此倾向 `wait` ——
                # **输出永远看起来合理，因为输入永远是同一个值**。
                # stage 2026-09-23 实测：真实静默 93 秒，那一步被告知 0 秒，
                # 而定时器走 SQL 读的是**列**，所以它准时醒了。
                # **平台知道过了 93 秒，而被它叫醒的那一步被告知过了 0 秒。**
                #
                # **取列不取 JSONB**：列由投递那一刻在同一条语句里写下
                # （`GREATEST` 语义，见 `_schedule_outbound_rules`），
                # 时间规则那一族读的也是它 —— **权威的那份本来就是列**。
                # 这一句把 JSONB 那份降成「每轮从权威表刷新的投影」，
                # 与 `_refresh_reference_projection` 逐字同一条理由：
                # 那两个数组也曾经「零写点、零读点」（B1-5）。
                #
                # ⚠️ **`last_inbound_at` 没有这个分叉，核过了**：它在 JSONB 里写
                # （`aggregate["last_inbound_at"] = envelope["occurred_at"]`）
                # 再落到列（`state = %s, last_inbound_at = %s`），方向正好相反。
                aggregate["last_outbound_at"] = _iso_or_none(
                    aggregate_row["last_outbound_at"]
                )
                # **A conversation whose first message was anonymous is not
                # anonymous forever** (independent review 2026-09-06, P2-5).
                # The link is written when the aggregate is created, so a
                # conversation that opened without an identity used to keep
                # `contact_id` NULL even after the customer identified
                # themselves in the next message -- the contact existed and
                # was listable, while `GET /contacts/by-conversation` answered
                # 404 for that conversation permanently.
                #
                # **Only when it is still NULL**, so this can never move a
                # conversation from one customer to another: an identity that
                # changes mid-conversation is identity resolution (K11 §2
                # rules that out of M1a), not a repair.
                if aggregate_row["contact_id"] is None:
                    raw_envelope = inbox["envelope"]
                    actor = (
                        raw_envelope.get("actor")
                        if isinstance(raw_envelope, dict)
                        else None
                    )
                    later_identity = (
                        actor.get("actor_id") if isinstance(actor, dict) else None
                    )
                    if later_identity and later_identity != UNKNOWN_ACTOR:
                        connection.execute(
                            """
                            UPDATE conversation_aggregates a
                               SET contact_id = c.id
                              FROM contacts c
                             WHERE a.tenant_id = %s AND a.id = %s
                               AND a.contact_id IS NULL
                               AND c.tenant_id = a.tenant_id
                               AND c.config_id = %s
                               AND c.external_user_id = %s
                            """,
                            (
                                tenant_id,
                                inbox["conversation_id"],
                                inbox["config_id"],
                                later_identity,
                            ),
                        )

            # **在回填之后读，不用上面那个 `aggregate_row`。**
            # 那一行是回填 `UPDATE` **之前**取的，一条「第一句匿名、第二句报了身份」
            # 的会话在它里面永远是 `NULL`——而那正是本轮第一次能记东西的会话。
            contact_id = self._contact_id_now(
                connection,
                tenant_id=tenant_id,
                conversation_id=UUID(str(inbox["conversation_id"])),
            )

            envelope = inbox["envelope"]
            validate_contract("message-envelope", envelope)
            event_type = str(envelope["event_type"])
            current_generation = int(aggregate["generation"])
            if (
                event_type == "timer"
                and int(envelope["aggregate_generation"]) != current_generation
            ):
                connection.execute(
                    """
                    UPDATE inbox_events
                       SET status = 'stale', processed_at = now(),
                           lease_owner = NULL, lease_expires_at = NULL
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, inbox_id),
                )
                connection.execute(
                    """
                    UPDATE scheduled_events
                       SET status = 'INVALIDATED', invalidated_at = now(),
                           invalidation_reason = 'STALE_GENERATION',
                           invalidated_by_event_id = %s
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (
                        UUID(str(envelope["event_id"])),
                        tenant_id,
                        UUID(str(envelope["scheduled_event_id"])),
                    ),
                )
                return None

            if event_type == "timer" and self._frame_is_gone(
                connection,
                tenant_id=tenant_id,
                scheduled_event_id=UUID(str(envelope["scheduled_event_id"])),
                aggregate=aggregate,
            ):
                # **RFC §6.3.5's 「stale 只写 transition/audit」 is read as a
                # ceiling, not a floor** (independent review 2026-09-09, P2-3;
                # user's ruling 2026-09-09): it forbids doing anything more --
                # above all calling the model -- rather than requiring an audit
                # row. This branch writes neither, **and neither does the
                # `STALE_GENERATION` branch above it**; the two stay symmetric,
                # and `scheduled_events.invalidation_reason` is where the reason
                # is queryable. Recorded here so the reading does not have to be
                # settled again.
                #
                # **RFC §6.3 step 5's frame half, and T226's `frame_exit`**
                # (user's ruling 2026-09-09: 「留给 B4-4 的消费侧复检收掉」).
                # The generation check above catches a customer who spoke again;
                # it does **not** catch a frame that finished. A `goal_stall`
                # armed three days ago against a frame the conversation
                # completed yesterday would otherwise wake up, plan, and chase a
                # goal nobody is pursuing.
                #
                # **Keyed on `target_frame_id`, per ADR-0114** -- which revises
                # ADR-0098 §二's 「按 pin 判」: the frame id is a fresh `uuid4()`
                # per frame, so it already answers 「这个 timer 是为哪一代帧排的」,
                # and `scheduled_events` has no frame-generation column to check
                # against anyway.
                connection.execute(
                    """
                    UPDATE inbox_events
                       SET status = 'stale', processed_at = now(),
                           lease_owner = NULL, lease_expires_at = NULL
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, inbox_id),
                )
                # **不读规则的 `cancel_on`，一律作废**（T358）：目标帧已经退出，
                # 这条定时器指向的东西不在了 —— 与配包声明了什么无关。
                connection.execute(
                    """
                    UPDATE scheduled_events
                       SET status = 'INVALIDATED', invalidated_at = now(),
                           invalidation_reason = 'TARGET_FRAME_EXITED',
                           invalidated_by_event_id = %s
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (
                        UUID(str(envelope["event_id"])),
                        tenant_id,
                        UUID(str(envelope["scheduled_event_id"])),
                    ),
                )
                return None

            previous_version = int(aggregate["state_version"])
            aggregate = deepcopy(aggregate)
            new_generation = current_generation + (1 if event_type == "customer_message" else 0)
            transition_id = uuid4()
            aggregate["state_version"] = previous_version + 1
            aggregate["generation"] = new_generation
            if event_type == "customer_message":
                aggregate["last_inbound_at"] = envelope["occurred_at"]
            aggregate["last_transition"] = {
                "event_id": str(envelope["event_id"]),
                "transition_id": str(transition_id),
                "reason": "customer_message" if event_type == "customer_message" else "timer",
                "policy_version": "m0-policy-v1",
                "model_version": self._inference.version,
                "profile_version": str(profile["identity"]["version"]),
            }
            validate_contract("conversation-aggregate", aggregate)

            # **记忆这一轮带哪几条，算一次。** 顺序由 `memory_fields` 的
            # 声明顺序定 —— 平台猜不出哪一条对这个行业更重要
            # （schema 的描述里举了三个没有交集的行业），而商家把它写在了
            # 声明的顺序里。装不下的**数出来**，不安静地少给。
            remembered = self._remembered_memories(
                connection,
                tenant_id=tenant_id,
                contact_id=contact_id,
                lifetimes=ttl_days_by_field(profile),
            )
            known_memories, memories_not_shown = memories_for_prompt(
                profile.get("memory_fields"), dict(remembered)
            )
            if known_memories or memories_not_shown:
                # **只记键，不记值**（与 `forbidden_value_reason` 那条同源：
                # 一条引用了它所拒的身份证号的拒绝记录，已经把那个号写进审计面了）。
                self._record_memories_sent(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                    correlation_id=UUID(str(envelope["correlation_id"])),
                    fields=[str(entry["field"]) for entry in known_memories],
                    not_shown=memories_not_shown,
                )

            summary_lines, summary_dropped, summary_ordinal = self._conversation_summary(
                connection,
                tenant_id=tenant_id,
                conversation_id=UUID(str(inbox["conversation_id"])),
            )

            return PreparedEvent(
                inbox_id=inbox_id,
                tenant_id=tenant_id,
                conversation_id=UUID(str(inbox["conversation_id"])),
                envelope=envelope,
                profile=profile,
                aggregate=aggregate,
                previous_state_version=previous_version,
                previous_generation=current_generation,
                delivered_materials=self._delivered_materials(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                ),
                delivered_assets=self._delivered_assets(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                ),
                conversation_summary=summary_lines,
                summary_lines_not_shown=summary_dropped,
                summary_ordinal=summary_ordinal,
                unanswered_rounds=self._unanswered_rounds(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                    aggregate=aggregate,
                ),
                last_outbound=self._last_outbound(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                ),
                customer_numbers=self._customer_numbers(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(inbox["conversation_id"])),
                ),
                contact_id=contact_id,
                remembered_fields=tuple(key for key, _ in remembered),
                known_memories=tuple(known_memories),
                memories_not_shown=memories_not_shown,
                understanding_during_takeover=bool(policy["understanding_during_takeover"]),
            )

    @staticmethod
    def _contact_id_now(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> UUID | None:
        """这条会话此刻属于哪个 contact，`None` = 还没识别出来。

        **与 `_contact_of` 读的是同一件事，而本批不把两者合并。**
        那一个是再营销闸自己的读取，改它的数据来源是在动一个与本批无关的机制——
        真要合并也该由改那道闸的人做。**两者在同一个事务里必然一致**：
        身份回填只发生在 `prepare` 里，而这一读在回填之后。
        """

        row = connection.execute(
            "SELECT contact_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        if row is None or row["contact_id"] is None:
            return None
        return UUID(str(row["contact_id"]))

    @staticmethod
    def _record_memories_sent(
        connection: Any,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        correlation_id: UUID,
        fields: list[str],
        not_shown: int,
    ) -> None:
        """这一轮把哪几条记忆递给了模型。**只记键，不记值。**

        ## 它补的是一句本来写着「还没有发生」的话

        `app.py` 那条墓碑路由的 docstring 在本批之前写着：

        > **而「那条回复当时引用了哪几条记忆」今天没有地方记** ——
        > 记忆还没有进提示词（那是 E4-5），所以这一半**不是欠账，是还没有发生**。
        > 真发生的时候要有一条 `RetrievalEvidence` 同族的记录。

        **本批就是 E4-5**，所以那半从今天起发生了，这一行是它的落点。

        ⚠️ **这一行单独读仍然答不出「引了哪几条」** —— 它答的是
        「这一轮**递过去了**哪几条」，递过去而没被用上的与用上的长得一样。
        **而 2026-09-19（T363）补上了成对的那一行**：
        `_record_memories_cited` / `inference.memories_cited`。
        **两行相减才是「递过去而没被用上的那几条」** ——
        所以两行用同一种标识（field_key），而且**条件读的是同两个值**
        （`known_memories` 与 `memories_not_shown`）：一行在而另一行不在时，
        「没走到生成」与「走到了生成一条都没引」就分不开了。
        `test_the_two_rows_are_a_pair` 钉住这一条。

        ⚠️ **上一版这里写的是「差的那一跳登记在 T363」** —— T363 已落地，
        所以改在这里。**写清楚它答得了什么、答不了什么，
        比让下一个人以为它全答了便宜。**

        **写在 `prepare` 的那个事务上**：准备失败就整轮回滚，
        而一条能在回滚之后活下来的留痕，描述的是一个没有发生过的回合。
        """

        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'inference.memories_sent',
                      'conversation_aggregate', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                conversation_id,
                correlation_id,
                MEMORIES_SENT_REASON_CODE,
                Jsonb({"fields": sorted(fields), "not_shown": int(not_shown)}),
            ),
        )

    @staticmethod
    def _record_memories_cited(
        connection: Any,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        correlation_id: UUID,
        offered: Sequence[str],
        cited: Sequence[str],
    ) -> None:
        """**那条回复引了哪几条记忆**（T363）—— 引用集，不是投喂集。

        ## 这是 ADR-0127 那一族的一次**显式搬运**

        排期 §4.5 写死了「**可以搬，但要显式搬一次，不是默认继承**」。
        搬过来的是**那个问题**与三条纪律，不是那张表：

        | 知识库那边（`062` 的 `cited_refs`） | 这里 |
        |---|---|
        | `NULL` = 这一轮没走到生成 | **没有这一行** = 没走到生成 |
        | `[]` = 走到了生成而一条都没引（**最该被数出来的那一种**） | `fields: []`，同义 |
        | 引用集 ⊆ 命中集 | 引用集 ⊆ 递过去的那几条（校验器 fail-closed 保证）|
        | 只记 ref，不记正文 | **只记 field_key，不记值** |

        **没有搬的是「一张表加一列」那个形状** —— 记忆这一侧没有
        `retrieval_evidence` 那样的逐轮证据表，而为这一件事建一张只有一列的表
        不如与它的同族 `inference.memories_sent` 并排。
        **这一句是那次「显式搬」的记录本身。**

        ## 为什么它与 `memories_sent` 成对

        `memories_sent` 答「递过去了哪几条」，这一行答「引了哪几条」。
        **两行相减才是「递过去而没被用上的那几条」** —— 而那正是
        T363 登记时说「递过去而模型没用上的，这一行里与用上的长得一模一样」
        指的缺口。所以**两行用同一种标识（field_key）**，不是一行 ref 一行键。

        ⚠️ **写在这一轮那个事务里**，与 `memories_sent` 逐字同一条理由：
        一条能在回滚之后活下来的留痕，描述的是一个没有发生过的回合。
        """

        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'inference.memories_cited',
                      'conversation_aggregate', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                conversation_id,
                correlation_id,
                MEMORIES_CITED_REASON_CODE,
                Jsonb(
                    {
                        "fields": sorted(field_key_of(ref) for ref in cited),
                        # **递过去的那几条也记一份**，于是「引了哪条、剩几条没用」
                        # 一行就答得了 —— 不必去翻另一条审计行再相减。
                        # 与 `062` 的 `retrieved_unused` 同一个用意。
                        "offered": sorted(field_key_of(ref) for ref in offered),
                    }
                ),
            ),
        )

    @staticmethod
    def _remembered_memories(
        connection: Any,
        *,
        tenant_id: UUID,
        contact_id: UUID | None,
        #: 商家给哪几个字段声明了寿命（`ttl_days`），各几天。**没有默认值** ——
        #: 一个「忘了传就当没人声明」的默认，与「这个商家确实没声明过」
        #: 在库里长得一模一样，而前者是把过期整条静音掉（T299）。
        lifetimes: Mapping[str, int],
    ) -> tuple[tuple[str, str], ...]:
        """这个 contact 已经有 active 记忆的那几条，**键与值**，**过期的不算**。

        **在已经开着的那个事务上读**，与 `_delivered_materials` 同一条理由：
        多一条语句，而不是多一次往返。

        ## 它从前只取键，而那条理由在本批之后不再成立

        上一版的 docstring 写着：

        > **只取键不取值。** 要答的是「这个字段已经有值了吗」（裁定八），
        > 答它不需要值；而值是客户事实，少带一份进内存就少一份写进日志的机会。

        **那句话在它自己的语境里是对的** —— 写入闸确实只需要键。
        问题在于整条路上**除了那道闸没有第二个读者**：表、迁移、RLS、
        控制面路由、`remember` 写入路径、封闭词表的闸，六样都在，
        **而值从来没有回到过任何一个提示词**。
        于是「记住了什么」对模型等于没发生过 —— **两头都在、中间没接。**

        本批（E4-5）把值接进理解步与计划步，所以这里必须取值。
        **「少一份写进日志的机会」那一条仍然守着**：留痕只记键
        （`inference.memories_sent`），拒绝文本从来不含值
        （`memory_values.forbidden_value_reason` 的 docstring 记着那次事故）。

        **`contact_id` 为 `None` 时不查**——没有 contact 就没有记忆，
        而 `contact_id IS NULL` 的查询会扫出别人的行。

        ## 过期的不算（T299，2026-09-20）

        **`status = 'active'` 不再等于「这条还算数」。** 商家可以给一个字段
        声明 `ttl_days`，那之后这条事实按 `created_at` 算年龄；
        **没声明的字段永不过期**（用户 2026-09-20 裁定的那一半）。

        ⚠️ **过滤在读取侧，行还躺在表里、`status` 照旧是 `active`** ——
        理由在 `runtime/memory_lifetime.py` 开头：`ttl_days` 是商家**当前配包**
        的属性，而 `054` 的 tombstone 不可逆。

        **所以这条 SELECT 多取一列 `created_at`** ——
        `is_expired` 拿它当年龄，取不到就只能一律答「没过期」，
        而那等于这道闸没装。`test_every_reader_asks_for_created_at`
        按 AST 钉住「读 `contact_memories` 的每一处都点名了 `created_at`」。
        """

        if contact_id is None:
            return ()
        rows = connection.execute(
            """
            SELECT field_key, field_value, created_at FROM contact_memories
             WHERE tenant_id = %s AND contact_id = %s AND status = 'active'
            """,
            (tenant_id, contact_id),
        ).fetchall()
        live = live_memories(
            [dict(row) for row in rows],
            now=datetime.now(UTC),
            lifetimes=lifetimes,
        )
        return tuple(
            (str(row["field_key"]), str(row["field_value"])) for row in live
        )

    #: 往回看多少条入站。
    #:
    #: 一通销售会话的长度以十条计，200 条是它的一个量级以上；
    #: 而**不设界**会让一条长会话在这一格上慢慢变贵，
    #: 那正是「界归花那笔字节的那一层」那条纪律说的地方。
    CUSTOMER_NUMBER_EVENTS = 200

    #: 一个数长什么样。**只认阿拉伯数字**：
    #: 「一千八百八十八」抓不到，而那时我们那句「您付的那 1888」也不会命中
    #: —— 两边用同一把尺子，所以不会因为写法不同而**误判成没来源**。
    _NUMBER = re.compile(r"\d[\d,，.]*")

    @staticmethod
    def _customer_numbers(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[str, ...]:
        """客户在这通对话里自己说出过的那些数。

        **读 `inbox_events` 而不是槽位**：槽位是模型填的，
        而这一格问的正是「模型是不是替客户编了一件事」——
        **拿模型的产出去核模型的产出，等于没核**
        （与 `_delivered_materials` 那条「Not the model's report」同源）。

        **正文由 `customer_words` 读，不在这里另写一个。**
        那个函数的 docstring 记着 T201：混合消息以 `type: "image"` 到达而
        句子在 `text` 里，按 `type` 取会丢掉半条消息。**加第二个读者就是再演一遍。**

        **只取客户说的话**（`event_type = 'customer_message'`）：
        timer 与渠道事件里的数是我们自己的，拿它当「客户说过」会把这道闸掏空。
        """

        rows = connection.execute(
            """
            SELECT envelope FROM inbox_events
             WHERE tenant_id = %s AND conversation_id = %s
               AND event_type = 'customer_message'
             ORDER BY created_at DESC, id DESC
             LIMIT %s
            """,
            (tenant_id, conversation_id, InboxProcessor.CUSTOMER_NUMBER_EVENTS),
        ).fetchall()
        found: list[str] = []
        for row in rows:
            envelope = row["envelope"] if isinstance(row, dict) else row[0]
            words = customer_words((envelope or {}).get("content"))
            for match in InboxProcessor._NUMBER.finditer(words or ""):
                number = re.sub(r"[,，]", "", match.group(0)).rstrip(".")
                if number and number not in found:
                    found.append(number)
        return tuple(found)

    @staticmethod
    def _delivered_assets(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[str, ...]:
        """这场对话已经发出去过的**文件** ref。

        **与 `_delivered_materials` 分开，而不是往它里面加。** 那一份喂给
        `current_stage`，而阶段的 `after` 只收话术 ref（封版时就拒 asset）——
        把文件混进去只会让 `after <= set(delivered)` 更容易成立，
        也就是**悄悄把阶段推快**。两个清单装的是两件事。

        `kind = 'send_material'` 的 `proposal` 就是 `{"ref": ...}`
        （`_send_material_content` 的上游），所以这里不用像那边一样剥段。

        状态取 `EXECUTING` / `APPLIED` 与那边逐字相同：**排进出站就算发过**。
        等到确认送达才算，会让同一份文件在投递那几秒里被再提一次 ——
        而那正是这条规则要防的那个样子。
        """

        rows = connection.execute(
            """
            SELECT proposal FROM intent_records
             WHERE tenant_id = %s AND conversation_id = %s
               AND kind = 'send_material'
               AND status IN ('EXECUTING', 'APPLIED')
             ORDER BY created_at
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        found: list[str] = []
        for row in rows:
            proposal = row["proposal"] if isinstance(row, dict) else row[0]
            reference = str((proposal or {}).get("ref") or "").strip()
            if reference and reference not in found:
                found.append(reference)
        return tuple(found)

    #: 上一轮那句话最多带多少字节进提示词。
    #:
    #: **留尾不留头**：问句（「…还是有人专门负责这一块？」）与
    #: 「我下一句给您讲 X」都落在末尾，而那正是让一个四字回答变得可读的部分。
    #:
    #: 这个数与 `MAX_MODEL_INPUT_BYTES = 49_000` 比是两个量级，
    #: 所以它**不会把哪一轮顶过上限** —— 运维那六次 `PROVIDER_INPUT_TOO_LARGE`
    #: 是 106KB 级别的另一件事，与这一格不同源。
    LAST_OUTBOUND_BYTES = 1_200

    @staticmethod
    def _last_outbound(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> str:
        """上一轮我们对这个客户说的那句话，客户真正看到的正文。

        **读 `outbox_messages` 而不是 `intent_records`**：
        `_delivered_materials` 的 docstring 写着「outbox 行按设计存的是
        **解析后的正文**」—— 而这里要的正是客户看到的那句，不是 ref。

        **正文由 `channel.customer_text` 读，不在这里另写一个。**
        那个函数的 docstring 记着 2026-09-10 的事故：平台有三种出站内容形状、
        没有单一读者，于是一整个窗口零送达、三行 `unsendable_content_type`。
        **加第四个读者就是把那件事再演一遍。**

        **只看发给客户的那些**（`AUDIENCE_IS_CUSTOMER_SQL`）：
        发给管理员的通知不是这个客户看见的话，拿它当上下文会让模型
        以为自己说过一句从没发出去的话。

        ## ⚠️ `ordinal` 在排序里，而它在 2026-09-19（批 G）之前不在

        **一轮可以写好几行出站**，而一轮写的行 `created_at` **相同**
        （它是事务时间戳）。先前这里排 `created_at DESC, id DESC` ——
        `id` 是 `uuid4()`，**于是「我们上一句说了什么」在一轮多行时是抛硬币**。

        **投递那一侧早就修过这件事**（T109 / 迁移 044 —— 那条领取查询的排序里
        有 `ordinal`，它的注释逐字写着「`created_at` 是事务时间戳，所以每一行
        都一样，排序落到了 `id`，而 `ordinal` 才是这一轮真正想要的位置」）。
        **一处修了、这个读取方没跟上** —— 本仓「写两遍会漂」那一族。

        ⚠️ **这段话有意不复现那一句 SQL 的字面量**：`test_the_delivery_order_…`
        是按整文件找那个字符串的，而一段引了它的 docstring 会把那条断言喂饱 ——
        **实测过**：探针 E 把真 SQL 改坏了而它照样绿。
        本仓这一族（「数错了的守卫看起来在守」）本轮撞到三次。

        **它不是批 G 引入的，是批 G 让它够得着**：运维 2026-09-18 实测那一轮
        `16:39:19` 就是「正文 + `send_material`」两行同一个事务。
        而批 G 把「一轮两条消息」变成**有意为之的形状**，于是这个抛硬币
        会直接打坏批 E 那套「跟进要接住上次说的话」：读到许诺那条，
        跟进就会去接一句我们其实已经兑现过的话。
        """

        row = connection.execute(
            f"""
            SELECT command FROM outbox_messages
             WHERE tenant_id = %s AND conversation_id = %s
               AND {AUDIENCE_IS_CUSTOMER_SQL}
             ORDER BY created_at DESC, ordinal DESC, id DESC
             LIMIT 1
            """,  # noqa: S608 - 唯一的插值是模块常量 AUDIENCE_IS_CUSTOMER_SQL
            (tenant_id, conversation_id),
        ).fetchone()
        if row is None:
            return ""
        command = row["command"] if isinstance(row, dict) else row[0]
        text = customer_text((command or {}).get("content"))
        if not text:
            return ""
        encoded = text.encode("utf-8")
        if len(encoded) <= InboxProcessor.LAST_OUTBOUND_BYTES:
            return text
        # 留尾。`errors="ignore"` 处理切在多字节中间的那一刀。
        return "…" + encoded[-InboxProcessor.LAST_OUTBOUND_BYTES :].decode(
            "utf-8", errors="ignore"
        )

    @staticmethod
    def _append_summary_line(
        connection: Any,
        prepared: PreparedEvent,
        *,
        understanding: Mapping[str, Any],
    ) -> None:
        """把理解步那一句「这一轮发生了什么」追进 `conversation_summaries`。

        ## 模型碰不到已有的那几行，而这正是裁定八的落地形态

        **裁定八（用户 2026-09-12）**：摘要人工可编辑，**模型不能自改** ——
        只能追加，不能重写已有的那一份。
        本函数收的是 `understanding["turn_summary"]`，**一句，说这一轮**；
        它既不读旧行也不写旧行，所以「模型改写了摘要」这件事
        **在结构上不可能发生**，不靠一道比对新旧两份文本的闸。

        ## 幂等：一个回合一行

        入站是至少一次，同一个事件重放会再走一遍这里。
        `conversation_summaries_one_line_per_turn_idx` 是
        `UNIQUE (tenant_id, conversation_id, source_event_id)`，
        配 `ON CONFLICT DO NOTHING` —— **重放写不进第二行**。
        ⚠️ 与 T276 那一批修掉的那个形状同源：`ON CONFLICT` 写了而没有人读它的
        答案，等于没写。这里不读答案是**对的**，因为这一处没有第二件要做的事
        （不推通知、不落别的行）—— 而这句话写在这里，是为了让下一个想在
        本函数里加一件事的人先看见它。

        ## 裁到字节，不是裁到字符

        合同那一侧是 `maxLength: 480`（**字符**），迁移那一侧是
        `CHECK (length(line) BETWEEN 1 AND 480)`（也是字符）。
        **而预算是字节** —— 一行 480 汉字是 1,440 字节，顶着它会让
        `SUMMARY_PROMPT_BYTES` 只装得下一行半。所以这里按 `SUMMARY_LINE_BYTES`
        再裁一次，**裁的是字节**（`clip_line`）。
        """

        line = clip_line(str(understanding.get("turn_summary") or "").strip())
        if not line:
            # 合同里它是必填的，所以空串只可能来自一个不照合同走的替身。
            # **不抛**：一条摘要写不进去不该让整轮回滚 —— 这一轮的回复本身
            # 与它无关。下一轮照常追它自己那一行。
            return
        connection.execute(
            """
            INSERT INTO conversation_summaries(
                tenant_id, id, conversation_id, ordinal,
                line, source_event_id, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'active')
            ON CONFLICT (tenant_id, conversation_id, source_event_id) DO NOTHING
            """,
            (
                prepared.tenant_id,
                uuid4(),
                prepared.conversation_id,
                prepared.summary_ordinal + 1,
                line,
                UUID(str(prepared.envelope["event_id"])),
            ),
        )

    @staticmethod
    def _conversation_summary(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[tuple[str, ...], int, int]:
        """这通对话聊过什么 —— **收过预算的那几行、丢了几行、追到第几号**。

        **在已经开着的那个事务上读**，与 `_delivered_materials` 同一条理由：
        多一条语句，而不是多一次往返。

        ## 它是本仓第一处把过往回合放进提示词的地方

        在此之前 `pipeline` 里那段注释逐字写着
        「nothing anywhere puts prior turns into a prompt」，
        而 **ADR-0122 §二.2 正是据它判 B4-6「上下文预算」已交付并关格**。
        ADR-0122 §6.1 自己写好了失效条件：「谁把历史写进提示词，
        谁就让这段实测失效」—— **本批就是那一笔**，所以它同一批建预算
        （§三 那条继承项：不同批 = 违反一条写死的继承项）。

        ## `status = 'active'` —— 被人作废的那几行不算

        作废是裁定九取的 (c)：**只作废、不逐字改**。行留在库里
        （`保留双版本/审计`），只是不再进提示词。

        ## 顺序与收口

        取**最近的**那些行（`ordinal DESC` + `LIMIT`），交给
        `summary_for_prompt` 按字节收到 `SUMMARY_PROMPT_BYTES`，**再翻回正序**。
        ⚠️ **`LIMIT` 是第一道，不是唯一一道**：它防的是一通两千轮的会话
        把两千行读进内存再扔掉一千九百行 —— **真正的闸是字节那一道**，
        因为一行可以长到 480 字符而 `LIMIT` 数的是行。
        """

        rows = connection.execute(
            """
            SELECT line, ordinal FROM conversation_summaries
             WHERE tenant_id = %s AND conversation_id = %s AND status = 'active'
             ORDER BY ordinal DESC
             LIMIT %s
            """,
            (tenant_id, conversation_id, SUMMARY_LINES_READ),
        ).fetchall()
        if not rows:
            return (), 0, 0
        highest = max(int(row["ordinal"]) for row in rows)
        newest_first = [str(row["line"]) for row in rows]
        kept, dropped = summary_for_prompt(list(reversed(newest_first)))
        return kept, dropped, highest

    @staticmethod
    @staticmethod
    def _unanswered_rounds(
        connection: Any,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        aggregate: Mapping[str, Any],
    ) -> int:
        """我们连着说了几轮而对面一次都没回（ADR-0149）。

        **按「客户最后一次开口之后我们发了几条」数** —— 锚点是
        `conversation_aggregates.last_inbound_at`，那一列由入站那一步写
        （`pipeline` 里 `aggregate["last_inbound_at"] = envelope["occurred_at"]`）。

        ⚠️ **数的是客户可见的出站**：经理通知与经理摘要也走 `outbox_messages`
        （全仓三处 `INSERT`，另两处是经理受众），而**给同事发一条通知
        不是「我们又跟客户说了一句」**。少了这个条件，一通有经理通知的对话
        会把追问次数虚报高，而模型据它判断「是不是一直在追」。

        ⚠️ **受众在 `command->>'audience'` 里，不是一列** —— 第一版写的是
        `AND audience = 'customer'`，而 `outbox_messages` 根本没有那一列
        （`001_m0.sql:225` 起十二列，受众住在 `command` 这个 jsonb 里；
        `012` 那条迁移的注释写得很清楚）。**全量门禁 148 条红**才把它翻出来，
        因为这条查询只在定时器那一轮跑得到，而契约层一条都跑不到它。
        **那一版的注释把「另两处是经理受众」讲得很详细，而列名是照印象写的** ——
        一段听起来有依据的话，依据却没被量过。

        `->>` 对缺键回 `NULL`，而 `012` 已经把历史行回填成 `'customer'`，
        所以这个等值判断不会把老行漏掉。

        **`last_inbound_at` 为空 = 客户从没开口过**，这时整通对话的出站数就是答案 ——
        那正是「我们一直在说而对面一次都没回」的极端情形，不是读不到。
        """

        last_inbound_at = aggregate.get("last_inbound_at")
        row = connection.execute(
            """
            SELECT count(*) AS rounds FROM outbox_messages
             WHERE tenant_id = %s AND conversation_id = %s
               AND command->>'audience' = 'customer'
               AND (%s::timestamptz IS NULL OR created_at > %s::timestamptz)
            """,
            (tenant_id, conversation_id, last_inbound_at, last_inbound_at),
        ).fetchone()
        if row is None:
            return 0
        return int(row["rounds"] if isinstance(row, dict) else row[0])

    @staticmethod
    def _delivered_materials(
        connection: Any, *, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[str, ...]:
        """Material refs this conversation has already sent, in the order sent.

        **Read on the transaction that is already open** for the profile and the
        aggregate, so this is one more statement rather than one more round trip.
        Scoped to the conversation and to `send_message`, which is the only kind
        the send path carries.

        The refs are pulled out of the intent's `proposal` rather than the outbox
        row because the outbox row holds resolved text by design -- resolving the
        ref into the merchant's words before queueing is the property the payload
        batch established, and it means the ref itself survives only here.
        """

        rows = connection.execute(
            """
            SELECT proposal FROM intent_records
             WHERE tenant_id = %s AND conversation_id = %s
               AND kind = 'send_message'
               AND status IN ('EXECUTING', 'APPLIED')
             ORDER BY created_at
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        found: list[str] = []
        for row in rows:
            proposal = row["proposal"] if isinstance(row, dict) else row[0]
            # **这一列装的就是 `typed_payload` 本身**，不是包着它的那一层：
            # `_persist_accepted_proposal` 写的是
            # `Jsonb(decision.proposal["typed_payload"])`。第一版在这里又
            # `.get("typed_payload")` 剥了一层，于是恒为 `None`、恒返回 `()`
            # ——阶段永远停在第一个，而没有任何东西会变红
            # （独立审查 2026-09-11，P0-1）。
            content = (proposal or {}).get("content") or {}
            for segment in content.get("segments") or []:
                if (
                    isinstance(segment, dict)
                    and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
                ):
                    ref = str(segment.get("content_or_ref"))
                    if ref not in found:
                        found.append(ref)
        return tuple(found)

    @staticmethod
    def _frame_is_gone(
        connection: Any, *, tenant_id: UUID, scheduled_event_id: UUID, aggregate: dict[str, Any]
    ) -> bool:
        """Has the frame this timer was scheduled for stopped being workable?

        **ADR-0114 / T226.** True when the row's `target_frame_id` names no frame
        on the stack, or names one that is not `active`.

        **Terminal, not "not active"** -- and the first version of this check got
        that wrong. `status != "active"` also matches `suspended`, which is what
        a **human takeover** leaves behind on every resumable frame: the timer
        would have been invalidated because a person picked up the conversation,
        and `test_a_timer_during_takeover_does_not_erase_the_exit_signal` caught
        it in the clean-checkout gate. Takeover is handled where it belongs
        (`_process_under_human_control`), and a suspended frame is a live frame
        that comes back on resume. `TERMINAL_FRAME_STATUSES` is imported rather
        than respelled here for exactly this reason.

        **Both halves are needed and they are different events.** A terminal
        frame is still on the stack (history is not discarded), so "absent"
        alone would miss the ordinary ending; a frame popped from the stack is
        gone entirely, so the status check alone would be reasoning about
        nothing.

        **A row that cannot be found is not treated as gone**: the timer
        envelope is the fact that it existed, and inventing an invalidation for a
        row we failed to read would drop a legitimate event on a transient
        condition.
        """

        row = connection.execute(
            "SELECT target_frame_id FROM scheduled_events WHERE tenant_id = %s AND id = %s",
            (tenant_id, scheduled_event_id),
        ).fetchone()
        if row is None:
            return False
        target = str(row["target_frame_id"])
        for frame in aggregate.get("target_stack") or []:
            if str(frame.get("id")) == target:
                return str(frame.get("status")) in TERMINAL_FRAME_STATUSES
        return True

    @staticmethod
    def _understanding_for_plan(
        prepared: PreparedEvent, understanding: dict[str, Any]
    ) -> dict[str, Any]:
        """The turn's document, with a carried-over exit signal substituted in.

        One helper because **three readers must see the same document**: the
        planning model, the validator, and the transition. Building it twice was
        how the first two versions of this each missed a different one.

        A copy with one field replaced -- the document still goes through
        `validate_inference_contract("understanding-result", …)` inside
        `validate_plan`, so this supplies a value rather than bypassing a check.
        """

        pending_exit = prepared.aggregate.get("pending_exit_signal")
        if not pending_exit:
            return understanding
        return {
            **understanding,
            "interruption": {
                **understanding["interruption"],
                "relation": str(pending_exit["relation"]),
            },
        }

    def _process_under_human_control(self, prepared: PreparedEvent) -> PipelineResult:
        """Understand, but do not plan, while a person holds the conversation.

        **`human control` moved from a pre-inference block to a pre-generation
        one** (ADR-0054 §三.1). It used to return here before any model ran, on
        the reasoning that policy would reject the intent anyway. True of the
        *intent* -- and it threw away the understanding with it, so everything
        the customer said to a human agent was invisible to the machine
        afterwards: slots unrecorded, and an exit signal ("forget it, I do not
        need this any more") understood by nobody, because the step that
        classifies it never ran.

        **What runs and what does not, and none of it is negotiable:**

        | | during takeover |
        |---|---|
        | `understand` | runs |
        | `apply_slot_updates` | runs, recording `source` |
        | `plan` | does not run |
        | target-stack operations | do not happen; the stack stays frozen |
        | `generate` | does not run |
        | intent / outbox / delivery | none produced |
        | `pending_question` / timers | none created; ones invalidated at takeover stay dead |

        **Planning is excluded for a reason beyond cost.** `keep` requires an
        `active` frame and takeover suspended them all, so a plan would reach
        `safe_fallback_plan` on a non-`active` frame -- **T75**, which self
        destructs there. "Maintain the stack while we are at it" is the shape
        ADR-0054 §三.1 rejects by name.

        **Whether the understanding step runs at all is a policy, and the
        carrier has arrived** (`control-policy`, freeze B3 #10/#11, migration
        037). The paragraph that stood here said the switch would come with it;
        this is that switch. The platform default is still **keep running** --
        the column defaults to `true` and the ruling of 2026-09-05 is unchanged
        -- so a tenant that never touches the policy sees no difference.

        **What turning it off does not change**: nothing is sent either way.
        The refusal below is reached on both branches, with the same reason
        code, because "a person is driving" is why no message goes out and that
        is true whether or not the machine was listening. What changes is
        whether the understanding call is made and whether slots move -- and
        that is exactly what a tenant paying per call is turning off.
        """

        if not prepared.understanding_during_takeover:
            # **No call, no writeback, and the same outcome as the branch below**
            # -- so the difference is visible in cost and in `slot_evidence`,
            # not in what the customer experiences.
            #
            # **The exit signal is cleared anyway, and that is not an exception
            # to "no writeback"** (independent review 2026-09-06, P1-1). The
            # branch below writes *and clears* `pending_exit_signal`; returning
            # before it stopped the clearing and did **not** stop the consuming
            # -- `_understanding_for_plan` still substitutes it into the first
            # plan after resume. So a customer who said "never mind" while the
            # switch was on, then changed their mind while it was off, came back
            # to an engine acting on the sentence they took back: `abandon`
            # unlocked (it is legal only under an exit relation) and `repair`
            # refused (T53 rejects a return after an exit signal). Both wrong,
            # in opposite directions.
            #
            # U2 is the rule that settles it -- **the last inbound message's
            # relation wins**. While the switch is off there is no relation,
            # because nobody listened; what there certainly is not is grounds to
            # keep asserting an older one. So an inbound customer message clears
            # it here for the same reason a non-exit relation clears it below.
            # A `timer` does not, which is F7's rule unchanged: a clock may not
            # erase something a person said.
            inbound = str(prepared.envelope["event_type"]) == "customer_message"
            if inbound and prepared.aggregate.get("pending_exit_signal"):
                prepared.aggregate["pending_exit_signal"] = None
                validate_contract("conversation-aggregate", prepared.aggregate)
            return self._persist_no_send(
                prepared,
                plan=None,
                reason_code="HUMAN_CONTROL_ACTIVE",
                detail=(
                    "conversation is controlled by a human; "
                    "understanding disabled by control policy"
                ),
            )

        call_context = CallContext(
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            correlation_id=UUID(str(prepared.envelope["correlation_id"])),
        )
        try:
            understanding = self._inference.understand(
                envelope=prepared.envelope,
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                context=call_context,
                # **判「这几个字有没有信号」要连着上一句看**（2026-09-17）。
                last_outbound=prepared.last_outbound,
                conversation_summary=prepared.conversation_summary,
                summary_lines_not_shown=prepared.summary_lines_not_shown,
                # **记住的那几件事**（2026-09-18，E4-5）。客户说「还是老样子」时，
                # 「老样子」是什么只在记忆里。
                known_memories=prepared.known_memories,
                memories_not_shown=prepared.memories_not_shown,
                # **平台自己那份「讲过哪几段」**（2026-09-18，运维报了三次）。
                # 它此前只喂给选取那一步算阶段，**模型一次都没见过**。
                materials_already_sent=prepared.delivered_materials,
            )
        except CostCeilingExceeded as error:
            # **§三.6's hole, and it is a hole because the obvious path is a
            # no-op.** A cost refusal normally hands the conversation to a
            # person -- suspend the stack, flip `control_mode`, file a task. All
            # three are already true here, so routing this through
            # `HANDOFF_REASON_CODES` would do nothing at all and record nothing
            # anybody reads. Its own reason code files its own task instead.
            return self._persist_no_send(
                prepared,
                plan=None,
                reason_code=UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
                detail=str(error),
            )
        except ProviderError as error:
            # A provider outage during takeover costs the same understanding and
            # is not a governance event: no task, same as the AI-controlled path.
            return self._persist_no_send(
                prepared, plan=None, reason_code=error.reason_code, detail=str(error)
            )
        writeback = apply_slot_updates(
            aggregate=prepared.aggregate,
            profile=prepared.profile,
            slot_updates=list(understanding["slot_updates"]),
            recorded_at=_iso(datetime.now(UTC)),
            is_correction=(str(understanding["interruption"]["relation"]) == "correction"),
        )
        prepared = replace(
            prepared,
            slot_update_refusals=writeback.refusals,
            **_admission(prepared, understanding),
        )
        updated = writeback.aggregate
        # **答案到了就给挂着的问题记上证据**（B1-2）。接管期间也记：
        # 问是 AI 问的，答是客户答的，而这一步只写证据、不改栈
        # （ADR-0054 §三.1：理解步与槽位写回继续）。
        _settle_pending_question(
            replace(prepared, aggregate=updated),
            event_id=str(prepared.envelope["event_id"]),
        )
        # **§三.5: record the exit signal, do not act on it.** The stack is
        # frozen, so there is nothing to act on -- and when the conversation
        # comes back the resume's first plan feeds this in as
        # `interruption_relation`, after which T53's `REPAIR_AFTER_EXIT_SIGNAL`
        # and T54's abandoned-parent rule apply **unchanged**. This batch
        # supplies a value; it does not touch a rule.
        #
        # **Overwritten every inbound message, never accumulated** (U2): a
        # customer who says "never mind" and then "actually, please do" has
        # withdrawn the first, and keeping it would honour the sentence they
        # took back.
        # **Only an inbound customer message may write or clear this**
        # (independent review 2026-09-05, F7). U2 says "the last inbound
        # message's relation wins", and a `timer` event is not a message the
        # customer sent: letting one clear the signal would mean a system clock
        # erasing something a person said, with the person having said nothing
        # further. Today that is mostly unreachable -- takeover is produced by a
        # handoff turn that bumps the generation and makes live timers stale --
        # but B3 #6's agent-initiated takeover does **not** bump it, and then it
        # is reachable.
        relation = str(understanding["interruption"]["relation"])
        if str(prepared.envelope["event_type"]) == "customer_message":
            if relation in EXIT_SIGNAL_RELATIONS:
                updated["pending_exit_signal"] = {
                    "event_id": str(prepared.envelope["event_id"]),
                    "relation": relation,
                    "recorded_at": _iso(datetime.now(UTC)),
                }
            else:
                updated["pending_exit_signal"] = None
        prepared.aggregate.clear()
        prepared.aggregate.update(updated)
        validate_contract("conversation-aggregate", prepared.aggregate)
        return self._persist_no_send(
            prepared,
            plan=None,
            reason_code="HUMAN_CONTROL_ACTIVE",
            detail="conversation is controlled by a human",
        )

    def process_prepared(self, prepared: PreparedEvent) -> PipelineResult:
        if prepared.aggregate["control_mode"] == "human":
            return self._process_under_human_control(prepared)
        call_context = CallContext(
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            correlation_id=UUID(str(prepared.envelope["correlation_id"])),
        )
        silence = self._read_the_silence(prepared, call_context=call_context)
        if silence is not None and str(silence["action"]["do"]) == "wait":
            # **「这一轮不说话」是一个答案，不是一次失败**（ADR-0149）。
            #
            # ⚠️ **它在理解步之前返回，于是这一轮只付一次模型调用** ——
            # 今天同样的一轮付三次（理解 / 计划 / 生成）然后产出一句复述。
            # 所以第四步在 `wait` 那条路上**是省钱的**，不是多花：3 → 1。
            # 跳过理解步在这里是对的，而不只是便宜：客户一个字都没说，
            # 没有东西要理解。
            return self._persist_no_send(
                prepared,
                plan=None,
                reason_code=SILENCE_READING_CHOSE_TO_WAIT,
                detail=str(silence["action"]["because"])[:500],
                rejection_context={"silence_reading": silence["reading"]},
            )
        try:
            understanding = self._inference.understand(
                envelope=prepared.envelope,
                aggregate=prepared.aggregate,
                # T34: the understanding step needs the frame's declared slots
                # to name one. Same profile the writeback below validates
                # against, so the two cannot disagree about what a slot is.
                profile=prepared.profile,
                context=call_context,
                # **两个 understand 调用点都要带它**（2026-09-17）。
                # 只接上一个，另一条路上的回合仍然单看几个字判信号 ——
                # 而那正是本仓「两头都在、中间没接」那一族的形状。
                last_outbound=prepared.last_outbound,
                conversation_summary=prepared.conversation_summary,
                summary_lines_not_shown=prepared.summary_lines_not_shown,
                # **两个 understand 调用点都要带它**，与上一行逐字同一条理由。
                known_memories=prepared.known_memories,
                memories_not_shown=prepared.memories_not_shown,
                # **平台自己那份「讲过哪几段」**（2026-09-18，运维报了三次）。
                # 它此前只喂给选取那一步算阶段，**模型一次都没见过**。
                materials_already_sent=prepared.delivered_materials,
            )
            # T6. `slot_updates` has been in the understanding contract from the
            # start and nothing ever wrote it, so the complete gate (then
            # `_missing_required_slots`, since 2026-09-24 `_unmet_exit_conditions`) read
            # a dict nobody filled and the `complete` exit gate was unreachable
            # outside fixtures.
            #
            # It runs *between* the two inference calls, not after them. The
            # first version wrote back after `plan()`, so only the validator saw
            # the slot the customer had just supplied -- the planning model
            # still saw it missing and would systematically decline to propose
            # `complete` on the very turn the goal became satisfiable, one turn
            # late, every time (independent review 2026-09-03, P2-3).
            writeback = apply_slot_updates(
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                slot_updates=list(understanding["slot_updates"]),
                # Platform clock, not `envelope["occurred_at"]`: that value is
                # reported by the channel callback and only checked for having a
                # timezone (`ingress/adapter.py`), and the whole point of this
                # field is audit evidence -- a sender-controlled timestamp cannot
                # anchor it (independent review 2026-09-03, P3-2).
                recorded_at=_iso(datetime.now(UTC)),
                # RFC 4.3 refuses low-confidence overwrites; a correction is the
                # customer telling us the earlier reading was wrong, so it is not
                # what that rule is about. The understanding step already
                # classifies this and the classification was being thrown away.
                is_correction=(
                    str(understanding["interruption"]["relation"]) == "correction"
                ),
            )
            with_slots, slots_applied = writeback.aggregate, writeback.applied
            # Rebind before anything below can return: every no-send path from
            # here on takes `prepared`, so attaching the refusals here is what
            # makes them reach the writer no matter which exit this turn takes.
            prepared = replace(
                prepared,
                slot_update_refusals=writeback.refusals,
                **_admission(prepared, understanding),
            )
            if slots_applied:
                prepared.aggregate.clear()
                prepared.aggregate.update(with_slots)
                validate_contract("conversation-aggregate", prepared.aggregate)
            # **平台自己的投递记录也能填槽**（2026-09-23，运维缺陷 18）。
            #
            # stage 实测：含「一个月 1888」的那份素材真的发了出去，
            # 而 `quote` 的 `price_quoted`（**该帧 `exit_conditions` 的唯一判据**）
            # 始终是空的 —— 「报过价」在对话里发生了，在状态里没有。
            #
            # ⚠️ **位置在模型写回之后，而那就是「谁赢」这条规则本身。**
            # 同一轮两边都可能写同一个槽，平台那份该赢；靠顺序而不是在
            # `apply_slot_updates` 里再加一个分支，**因为那个分支的正确性
            # 反正取决于调用顺序** —— 一条规则两个定义点，本仓付过账的形状。
            #
            # **喂进去的是与 `frame_to_advance_into` 同一份 `delivered`**：
            # 两者答的是同一个问题（「那条素材真的出去了吗」），
            # 各读一份就是同一个事实两个来源。
            delivery_writeback = record_delivered_slots(
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                delivered=prepared.delivered_materials,
                recorded_at=_iso(datetime.now(UTC)),
                evidence_event_id=str(prepared.envelope["event_id"]),
            )
            if delivery_writeback.applied:
                prepared.aggregate.clear()
                prepared.aggregate.update(delivery_writeback.aggregate)
                validate_contract("conversation-aggregate", prepared.aggregate)
            if delivery_writeback.refusals:
                # **接上去，不是替换。** 模型那一侧这一轮也可能有被拒的，
                # 而 `slot_update_refusals` 是整轮的台账 —— 覆盖它会让
                # 审计行少记一半，且少记的那一半没有任何东西会红。
                prepared = replace(
                    prepared,
                    slot_update_refusals=(
                        prepared.slot_update_refusals + delivery_writeback.refusals
                    ),
                )
            # **平台自己把对话推进到下一个帧**（2026-09-21，用户裁定
            # 「要做成平台级确定性触发，这才是主动性的来源」）。
            #
            # ⚠️ **位置是这一批唯一要紧的设计决定**：它在槽位写回**之后**、
            # 计划步**之前**。所以条件满足的那一轮，计划步与生成步读到的
            # 已经是新帧的 `goal` 与新帧的素材 —— **这一轮就报价**，
            # 而不是下一轮。放到计划之后就退化成「下次再说」，
            # 而用户要的原话是「聊到特定情况**立马**主动推进到下一步」。
            #
            # ⚠️ **接管那一路不经过这里**：它有自己的理解步与写回
            # （本文件上方另一处 `apply_slot_updates`），而那条路上平台
            # 不替正在说话的人改目标。
            advancing_to = frame_to_advance_into(
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                # **平台自己的记录**，不是模型自填的槽 —— 同一份
                # `already_sent` 传给素材选择的那一份。
                delivered=prepared.delivered_materials,
            )
            if advancing_to is not None:
                advanced = advance_into_frame(
                    aggregate=prepared.aggregate,
                    profile=prepared.profile,
                    definition_id=advancing_to,
                    occurred_at=str(prepared.envelope["occurred_at"]),
                )
                prepared.aggregate.clear()
                prepared.aggregate.update(advanced)
                validate_contract("conversation-aggregate", prepared.aggregate)
            # **答案到了就给挂着的问题记上证据**（RFC §3.2 的 `pending_question`，B1-2）。
            # 放在写回之后：判据是槽位**写回之后**带的 `evidence_event_id`，
            # 而不是「有没有 slot_updates」—— 被 RFC 4.3 拒掉的那条不算答。
            _settle_pending_question(prepared, event_id=str(prepared.envelope["event_id"]))
            # **§三.5's consumption side reaches all three readers**
            # (ADR-0058 §二.2, from independent review F2). The exit signal has
            # three: the planning model, the validator, and the transition.
            # Feeding only the last two makes it a veto that can never be a
            # proposal -- and what it *unlocks* is precisely `abandon` and
            # `request_pop`, which the model will never propose because it was
            # never told. The observable cost was in this batch's own end-to-end
            # test: the first customer message after a resume came back
            # `no_send` under `PUSH_WITHOUT_INTERRUPTION`, a code with nothing
            # to do with an exit signal, and the customer got no reply.
            #
            # **Still not a rule change**: the same substituted document, one
            # field, handed to all three.
            #
            # **The first production reader of `time_rules[].strategy`**
            # (ADR-0105 §零.1, which named this batch as its consumer and gave
            # the reason: the field carries the profile author's product
            # decision -- 「沉默 90 秒跟一句」 and 「停滞 3 天转人工」 are two
            # different commercial judgements, and dropping it hands that
            # decision back to the model).
            #
            # **Told to the planner rather than only checked afterwards.**
            # Checking alone would be enforcement without instruction: the model
            # would keep choosing whatever it chooses, every timer turn would
            # fall through to `no_message`, and 「打法固定在模板里」 would be
            # implemented as "the platform says nothing on timers". So the
            # required strategy goes into the plan call **and** into the
            # validator; the second is what makes the first load-bearing.
            required_strategy = self._required_strategy(prepared)
            # **选取挪到计划之前，而且只选这一次**（09-17）。
            #
            # 候选意图的 `typed_payload` 是必填的 —— 提名一个 `send_material`
            # 就得同时给出 ref。计划步此前拿不到清单，于是**从不提名**，
            # 而生成步提不出候选里没有的 kind：十一轮真实对话零次。
            #
            # **用的是换帧之前那个帧**：计划正是在这个帧里做决定的。
            # 换帧之后生成步用的是新帧的清单 —— 两份可能不同，
            # 而那个不一致由下面那道「没递出的 ref 一律拒」兜住：
            # 计划提了一个新帧不给的 ref，生成步照抄也过不了校验。
            planning_frame = (prepared.aggregate.get("target_stack") or [None])[-1]
            planning_assets = (
                select_assets(
                    frame_definitions(prepared.profile).get(
                        str(planning_frame["definition_id"]), {}
                    ),
                    frame=planning_frame,
                    understanding=understanding,
                    delivered=prepared.delivered_materials,
                    emit_kind=self._timer_event_kind(prepared),
                    already_sent=prepared.delivered_assets,
                )
                if planning_frame
                else ()
            )
            proposed_plan = self._inference.plan(
                understanding=self._understanding_for_plan(prepared, understanding),
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                context=call_context,
                required_strategy=required_strategy,
                assets=planning_assets,
                last_outbound=prepared.last_outbound,
                conversation_summary=prepared.conversation_summary,
                summary_lines_not_shown=prepared.summary_lines_not_shown,
                # **计划步要它的理由与理解步不同**：该问的东西如果已经记着，
                # 就不该再问一遍 —— 而 `pending_question` 只管这一通对话。
                known_memories=prepared.known_memories,
                memories_not_shown=prepared.memories_not_shown,
                # **平台自己那份「讲过哪几段」**（2026-09-18，运维报了三次）。
                # 它此前只喂给选取那一步算阶段，**模型一次都没见过**。
                materials_already_sent=prepared.delivered_materials,
                # **那次静默是怎么读的**（ADR-0149）。客户消息那一轮是 `None`。
                #
                # 走到这里意味着第四步**没有**说 `wait`（那一支在理解步之前
                # 就返回了），所以它现在是一份「要说话，而且是这么说」的判断。
                silence_reading=silence,
            )
        except (ProviderError, CostCeilingExceeded) as error:
            # No understanding or no plan means there is nothing to say. The
            # event is still persisted and the reason still recorded; the
            # platform never invents a reply to cover a provider outage.
            return self._persist_no_send(
                prepared, plan=None, reason_code=error.reason_code, detail=str(error)
            )
        # **§三.5's consumption side, and it has to reach two readers.** An exit
        # signal given while a human held the conversation was recorded and not
        # acted on -- the stack was frozen, so there was nothing to act on. This
        # is the first plan after control came back, so it is fed in here.
        #
        # **Both the validator and the transition, or the reuse is not real.**
        # T53's `REPAIR_AFTER_EXIT_SIGNAL` lives in `inference_validation.py`
        # and reads `understanding["interruption"]["relation"]`; the transition
        # in `target_stack.py` reads the argument. Feeding only the second was
        # the first version of this code, and the round-trip test caught it: the
        # plan was rejected for an unrelated reason and T53 never fired at all.
        #
        # **T53 and T54 are not modified.** The work is to supply a value, not to
        # change a rule -- a copy of the document with one field substituted,
        # rather than a new branch inside their rules.
        #
        # **Consumed whether or not the plan survives validation.** If it is
        # rejected *because* of this signal, the signal has done its job; leaving
        # it set would refuse every later turn for the same reason forever.
        pending_exit = prepared.aggregate.get("pending_exit_signal")
        understanding_for_plan = self._understanding_for_plan(prepared, understanding)
        if pending_exit:
            prepared.aggregate["pending_exit_signal"] = None
        interruption_relation = str(
            understanding_for_plan["interruption"]["relation"]
        )
        plan, rejection = plan_or_fallback(
            plan=proposed_plan,
            understanding=understanding_for_plan,
            aggregate=prepared.aggregate,
            profile=prepared.profile,
            required_strategy=required_strategy,
            # **定时回合不发澄清**（ADR-0138 的第四个条件）。
            # 传 `event_type` 而不是让那边读 `required_strategy` ——
            # 后者在「release 被取代、规则不再声明打法」时也是 `None`，
            # 而那仍然是一个定时回合。理由写在 `plan_or_fallback` 里。
            event_type=str(prepared.envelope.get("event_type") or ""),
        )
        if rejection is None:
            transitioned = apply_target_stack_operation(
                aggregate=prepared.aggregate,
                operation=plan.document["target_stack_operation"],
                interruption_relation=interruption_relation,
                profile=prepared.profile,
                occurred_at=str(prepared.envelope["occurred_at"]),
            )
            prepared.aggregate.clear()
            prepared.aggregate.update(transitioned)
            validate_contract("conversation-aggregate", prepared.aggregate)
            disposition = stack_requires_human_disposition(prepared.aggregate)
            if should_enter_closing_frame(
                aggregate=prepared.aggregate, profile=prepared.profile, disposition=disposition
            ):
                # ADR-0031. The root goal is satisfied and the profile names a
                # frame that owns the closing turn, so the turn gets a legal
                # intent owner and does not need a person. The three conditions
                # that make this safe live in `should_enter_closing_frame`, one
                # test each.
                prepared.aggregate.clear()
                prepared.aggregate.update(
                    enter_closing_frame(
                        aggregate=transitioned,
                        profile=prepared.profile,
                        occurred_at=str(prepared.envelope["occurred_at"]),
                    )
                )
                validate_contract("conversation-aggregate", prepared.aggregate)
                # This branch replaced one that wrote `conversation.handoff`, so
                # without a record of its own the batch would have *removed* an
                # audit event and left the stack shape as the only evidence that
                # anything happened (independent review 2026-09-03, P2-2).
                entry = {
                    "kind": "closing",
                    "closing_frame_definition_id": str(
                        prepared.aggregate["target_stack"][-1]["definition_id"]
                    ),
                    "closing_frame_id": str(prepared.aggregate["target_stack"][-1]["id"]),
                    "completed_frame_id": str(prepared.aggregate["target_stack"][-2]["id"]),
                    "slot_updates_applied": slots_applied,
                }
                # `replace`, because `PreparedEvent` is frozen -- the same
                # mechanism `slot_update_refusals` uses a few hundred lines up.
                prepared = replace(prepared, frame_entry=entry)
                disposition = None
            elif should_enter_no_goal_frame(
                aggregate=prepared.aggregate, profile=prepared.profile, disposition=disposition
            ):
                # **ADR-0099, B4-7.** The profile named no closing frame -- or
                # named one and it has now completed too -- so the platform
                # supplies the owner instead of a person. T8 measured what the
                # branch below costs when it is the only road: every
                # successfully finished conversation produced a manual task.
                #
                # `no_message` is in this frame's `allowed_strategy_set`, so
                # "there is an owner for the turn" does not mean "a message goes
                # out"; it means an intent filed here is traceable to a frame,
                # which is what `intent_records.target_frame_id NOT NULL` asks
                # for and what candidate ② of ADR-0031 would have given up.
                prepared.aggregate.clear()
                prepared.aggregate.update(
                    enter_no_goal_frame(
                        aggregate=transitioned,
                        occurred_at=str(prepared.envelope["occurred_at"]),
                    )
                )
                validate_contract("conversation-aggregate", prepared.aggregate)
                # Recorded for the same reason the closing frame's entry is: this
                # branch replaces one that wrote a handoff, and without a row of
                # its own the batch would have *removed* an audit event and left
                # the stack shape as the only evidence.
                entry = {
                    "kind": "no_goal",
                    "no_goal_frame_id": str(prepared.aggregate["target_stack"][-1]["id"]),
                    "completed_frame_id": str(prepared.aggregate["target_stack"][-2]["id"]),
                    "completed_frame_definition_id": str(
                        prepared.aggregate["target_stack"][-2]["definition_id"]
                    ),
                    "profile_declares_closing_frame": bool(
                        prepared.profile.get("closing_frame_ref")
                    ),
                    "slot_updates_applied": slots_applied,
                }
                # `replace`, because `PreparedEvent` is frozen -- the same
                # mechanism `slot_update_refusals` uses a few hundred lines up.
                prepared = replace(prepared, frame_entry=entry)
                disposition = None
            if disposition is not None:
                # Completing or abandoning the root frame leaves nothing on the
                # stack that can accept work. This turn deliberately does not
                # send: the send path binds the intent to `target_stack[-1]`,
                # which is now a terminal frame, and a customer message filed
                # under a completed goal is exactly the untraceable evidence the
                # intent contract exists to prevent. A person takes it from here
                # -- including any closing line the generator had drafted.
                return self._persist_no_send(
                    prepared,
                    plan=plan,
                    reason_code=disposition,
                    detail="no target frame can accept further work",
                    interruption_relation=str(understanding["interruption"]["relation"]),
                )
        # **「拒绝即不发」曾经是写在这一行里的不变式**（T339 第二批之前）：
        # 任何拒绝都在生成与投递之前返回，**与回退选了哪条打法无关** ——
        # 所以 `safe_fallback_plan` 里产出候选意图那一支
        # 与 `fallback_strategy` 这个参数**在它之下从来没有机会生效**。
        # **双重不可达**：两处各自假设了静默，而谁都不知道对方也假设了。
        #
        # **现在的判据（用户 2026-09-13 裁 (c)）**：
        # * 回退**说得出内容**（点名了缺的槽位，于是打法不是静默的），
        #   **而这是本会话第一次撞上这个理由码** → **让它问出去**（往下走生成与投递）；
        # * **上一条 deny 就是同一个码** → 走 `_persist_no_send`，
        #   那里的升级逻辑会建一条人工任务。
        #
        # **为什么是「问过之后仍然同码」而不是「连着被拒两次」**：
        # 第一次静默升级是过度反应（N=2 当初的理由就是「第一次可能是这一轮判断失误」），
        # 而**问过之后仍然同码**排除了「模型只是没想到要问」——
        # 它比「连着被拒两次」是更强的证据。
        #
        # ## 2026-09-15（D4，ADR-0138）：判据去掉 `required_slot_requests`
        #
        # **先前那一项是「说得出内容」的一个代理**，而它在 T339 当时是准确的：
        # 那时唯一非静默的回退路是 `ask_slot`，而它说得出内容**当且仅当**点名了槽位。
        # `ask_clarification` 落地之后代理就不准了 —— 它的 `required_slot_requests`
        # **恰恰是空的**（那正是它被选中的条件），于是
        # **策略选了、候选意图造了、生成结果也造了，最后在这一行全丢掉**：
        # 审计里记着 `ask_clarification`，而客户什么都没收到。
        #
        # **改法不是给代理打补丁，是换回它原本要表达的那件事**：`not plan.is_silent`。
        #
        # **用户 2026-09-15 把问法也纠正了**：不是「这样会不会放行太多」，而是
        # **「一个非静默的回退计划，凭什么不让它出去？」** ——
        # 非静默意味着系统**已经决定这一轮要说话**；说了话却不发出去，
        # 客户那边就是石沉大海。**「默认发出去」才是安全的方向，「默认丢掉」不是。**
        #
        # `_previous_denial_has_the_same_code` 那一项**原样保留** ——
        # 它是 T339 的另一半（同码第二次就升级成人工任务），与本次无关。
        asked_this_turn = (
            rejection is not None
            and not plan.is_silent
            and not self._previous_denial_has_the_same_code(prepared, rejection.reason_code)
        )
        if (rejection is not None or plan.is_silent) and not asked_this_turn:
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code=rejection.reason_code if rejection else "PLAN_SELECTED_SILENCE",
                detail=rejection.detail if rejection else plan.document["strategy_reason_code"],
                # **兜底要留来路**（运维 2026-09-24 顺带②）：先前这里透传
                # `rejection.context`，而 STACK_OP 族的 context 本来就是空 ——
                # 审计里只剩一行 deny，看不出「这一轮被替换成了哪个兜底、
                # 它是兜底」。`ask_clarification` 不在配包 strategies 里，
                # 读审计的人对着配包对不上号，正是那次报告的现场。
                rejection_context=_fallback_denial_context(rejection=rejection, plan=plan),
            )
        # **管线的第四步（裁定三）**，位置不是随手挑的：它在**计划已经过校验**之后
        # （那一步才是决定要不要检索的地方），在**生成之前**（6c 把命中交给生成）。
        #
        # **它也在那条「静默就收口」的返回之后**，而这一句不是排版：一个静默策略
        # （`wait` / `no_message` 一类）的回合**不会走到这里**，所以它不会为一条
        # 没人会读的命中付一次嵌入的钱。6b 登记过这条观察（T306 / T315 的尾注
        # 都写着「接线那一刻它就是一次白花的检索」），**而接线时它恰好落在了
        # 正确的一侧** —— 恰好不等于被守着，所以现在有一条用例钉这个顺序
        # （`tests/e2e/test_knowledge_exit_criteria.py` 里那条静默回合的用例，它连真栈跑）。
        #
        # 失败不停回合（裁定六），库侧异常也不（`KnowledgeRetriever.run` 吃掉，P2-1）。
        #
        # **这一句连它的注释一起放在这里**，是独立审查 P3-4 的处置：先前它插在下面那段
        # 「`.get` rather than a subscript」与它解释的那两行**中间**，把注释和代码隔开了。
        retrieval = self._retrieve_if_requested(
            plan=plan, context=call_context, profile=prepared.profile
        )

        # **Resolved once, here, and handed to all four readers** (independent
        # review 2026-09-10, P1-1). The prompt, the validator and the two send
        # sites were each reaching for the frame themselves, and they did not
        # all get the same one: `plan.definition` is bound at `plan_or_fallback`
        # above, and `apply_target_stack_operation` then **clears and rewrites
        # `prepared.aggregate` in place**, so any plan whose operation is not
        # `keep` moves the stack top out from under it. The model was shown the
        # new frame's materials and the validator judged them against the old
        # frame's list, which refuses the message and sends the customer nothing.
        #
        # **The frame after the transition is the right one**, because it is the
        # frame this turn's message is *filed under*: `intent_records.
        # target_frame_id` below is taken from exactly this stack top. A message
        # archived against one frame while quoting another frame's approved
        # wording is a record that contradicts itself -- and once a closing frame
        # is entered, that frame's material is the only wording this turn should
        # be saying at all.
        #
        # `.get` rather than a subscript: a definition id the profile no longer
        # resolves means no materials, not a `KeyError` that rolls the whole turn
        # back and retries into the same crash.
        current_frame = prepared.aggregate["target_stack"][-1]
        current_definition = frame_definitions(prepared.profile).get(
            str(current_frame["definition_id"]), {}
        )
        # **...and then narrowed to what *this* turn may quote.** `select_materials`
        # is the seam the rule lives behind -- see `material_selection` for why the
        # platform decides this rather than the model, and why replacing the rule
        # later touches that module and nothing here. It runs on the same
        # post-transition frame for the same reason the resolution above does.
        turn_materials = select_materials(
            definition=current_definition,
            declared=materials_for_frame(prepared.profile, current_definition),
            emit_kind=self._timer_event_kind(prepared),
            understanding=understanding,
            delivered=prepared.delivered_materials,
            # **主动话术也只发一次**（2026-09-17）。传的是平台从
            # `intent_records` 现读的那一份 —— **不是模型自填的
            # `material_delivered` 槽**：同一个文件里那条注释写着理由
            # 「Not the model's report… guess wrong, select the wrong
            # materials, and nothing goes red」。
            already_sent=prepared.delivered_materials,
        )
        # **文件那一份与话术分开选，因为它们是两类东西**（E3-4）：
        # 话术进正文、按 ref 逐字引；文件进的是一个 `send_material` 提案。
        # 两者共用同一套可用性语义（`_is_offered`），而**不共用一个集合** ——
        # 合成一个会让「这一轮可以引的话」与「这一轮可以发的文件」
        # 在提示词里长得一样，而模型对它们该做的事不同。
        turn_assets = select_assets(
            current_definition,
            frame=current_frame,
            understanding=understanding,
            delivered=prepared.delivered_materials,
            emit_kind=self._timer_event_kind(prepared),
            already_sent=prepared.delivered_assets,
        )
        # **提示词与校验器看到的是同一个集合**（与 `materials` 同一条理由）：
        # 「这一轮送进去了哪几条知识库」只算一次，两处共用。
        # `sent_chunks` 已经压过额度（ADR-0135），所以这里不再判字节。
        turn_knowledge = (
            {chunk["ref"]: chunk["text"] for chunk in retrieval.sent_chunks}
            if retrieval is not None
            else {}
        )
        # **这一轮递给写字那一步的记忆，算一次**（T363）。
        # 提示词与校验器用同一份 —— 两处各算一遍会让「递过了什么」
        # 与「许引什么」不一致，而那种不一致只在真实回合里显形
        # （与 `turn_materials` / `turn_knowledge` 逐字同一条纪律）。
        turn_memories = remembered_facts_for_prompt(prepared.known_memories)
        dropped_intents: list[dict[str, Any]] = []
        echoed_segments: list[dict[str, Any]] = []
        unsourced_segments: list[dict[str, Any]] = []
        unkept_promises: list[dict[str, Any]] = []
        try:
            generation = self._generate_and_validate(
                prepared=prepared,
                plan=plan,
                understanding=understanding,
                materials=turn_materials,
                knowledge=turn_knowledge,
                assets=turn_assets,
                context=call_context,
                # **记住的那几件事**（T363，2026-09-19）。理解步与计划步
                # 早就收着（E4-5），而**写回复的这一步从没见过** ——
                # 于是「那条回复引了哪几条记忆」在结构上答不出来。
                #
                # **同一份 `shown` 转出来的**（`remembered_facts_for_prompt`），
                # 所以三个提示词对「递了哪几条」是同一个答案。
                remembered_facts=turn_memories,
                dropped=dropped_intents,
                echoed=echoed_segments,
                unsourced=unsourced_segments,
                unkept=unkept_promises,
            )
        except (ProviderError, CostCeilingExceeded, GenerationRejected) as error:
            reason = getattr(error, "reason_code", "GENERATION_FAILED")
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code=reason,
                detail=str(error),
                # **拒绝那一处知道的事实，带下去**（2026-09-18）。
                # 升级出来的那条人工任务要自解释 —— 而
                # 「从 `detail` 那句话里解析回来」是本仓付过账的那一类。
                # `ProviderError` / `CostCeilingExceeded` 没有 `context`，
                # 那时是 `None`，与从前逐字相同。
                rejection_context=getattr(error, "context", None) or None,
            )

        # ------------------------------------------------------------------
        # **许了没兑现 → 再给一次机会**（T367，用户 2026-09-19 裁定）
        # ------------------------------------------------------------------
        #
        # 用户那两句：**「承诺完要兑现，抛掉是没有办法的办法」**、
        # **「重跑计划 + 生成」**。批 G 把兑现那条路打通了（一轮两条消息），
        # 而它只是**提示词的压力** —— 模型不照做时批 F 把那句许诺丢掉。
        # 这里是那之间缺的一步：**先给它一次带着反馈的重来**。
        #
        # ## 为什么必须重跑**计划**，不能只重跑生成
        #
        # 第二条消息**必须由计划先列出来**：`validate_generation` 里
        # `len(produced_kinds) > len(planned_kinds)` → `GENERATION_DUPLICATED_INTENT`
        # （批 G 的 `test_the_plan_has_to_list_the_second_message_first` 钉着）。
        # 闸命中时往往正是「计划只列了一条」，所以只重跑生成，它发不出第二条 ——
        # 只能把兑现塞进那唯一一条里，而用户当面说过那样「显得很奇怪」。
        #
        # ## 而栈操作钉成 `keep`（用户 2026-09-19 裁定「乙」）
        #
        # 目标栈迁移**在第一次尝试里就落下了**（`apply_target_stack_operation`
        # 就地改 `prepared.aggregate`）。重跑那份计划的 `target_stack_operation`
        # 照它再走一遍等于**迁移两次**。所以这里把它钉成 `keep`：
        # **栈已经到位了，这一次只管消息。**
        #
        # ⚠️ **这是「部分采纳模型的计划」，而仓里有先例**：
        # `_validate_scheduled_event_changes` 把模型给的 `due_at` / `semantic_key`
        # 验完就丢，理由写死在那儿（模型挑的时点在虚拟时钟下不可重放）。
        # **两者同族：采纳它会说的那部分，不采纳它无权决定的那部分。**
        #
        # ## 重跑不许把事情变坏
        #
        # 重跑抛了（供应商、成本顶、或者它自己被拒）→ **放弃重跑，留第一次的结果**。
        # 第一次至少有东西发得出去，而一次失败的重试不该把它换成沉默。
        # 重跑仍然没兑现 → 那就是结局，批 F 的丢弃照旧（兜底没变）。
        if unkept_promises and not plan.is_silent:
            # **送回去的是整句，不是动词短语**，而同一句在草稿与载荷里各记一次，
            # 所以按出现顺序去重。
            promised_sentences: list[str] = []
            for item in unkept_promises:
                sentence = str(item.get("sentence") or "")
                if sentence and sentence not in promised_sentences:
                    promised_sentences.append(sentence)
            retried = self._retry_for_an_unkept_promise(
                prepared=prepared,
                understanding=understanding,
                promised_sentences=promised_sentences,
                materials=turn_materials,
                knowledge=turn_knowledge,
                planning_assets=planning_assets,
                assets=turn_assets,
                # **重跑那一次也递记忆**（复核 2026-09-19 点名要决定的那件）——
                # 不递 = 校验器那侧拿到空集 = 静默拒掉每一条引记忆的回复。
                remembered_facts=turn_memories,
                context=call_context,
                required_strategy=required_strategy,
            )
            # **`None` = 放弃重跑，什么都不动** —— 那几个列表里已经是第一次的结果，
            # 所以「留第一次的」不需要还原动作。**这一点写出来，因为一段
            # 「看起来什么都没做」的分支最容易被下一个人当成漏写。**
            if retried is not None:
                generation, plan, retry_lists = retried
                dropped_intents[:] = retry_lists[0]
                echoed_segments[:] = retry_lists[1]
                unsourced_segments[:] = retry_lists[2]
                unkept_promises[:] = retry_lists[3]
            self._record_promise_retry(
                prepared,
                promised=promised_sentences,
                kept_after_retry=retried is not None and not unkept_promises,
                retry_ran=retried is not None,
                correlation_id=call_context.correlation_id,
            )

        if (
            dropped_intents
            or echoed_segments
            or unsourced_segments
            or unkept_promises
            or plan.reply_floor_basis
        ):
            self._record_plan_generation_disagreement(
                prepared,
                plan=plan,
                dropped=dropped_intents,
                echoed=echoed_segments,
                unsourced=unsourced_segments,
                unkept=unkept_promises,
                correlation_id=call_context.correlation_id,
            )

        # **裁定四那三列，生成之后写回**（6c）。
        #
        # 只在这一轮真的检索过时写：没有证据行的回合没有行可 UPDATE，
        # 而**建一行只为填这三列会把「没请求检索」那个反连接算坏**
        # （`050` 的表注释把那件事定成了一次反连接）。
        #
        # **失败的生成不写**：这三列的意思是「模型看到了什么、引了哪边」，
        # 而一次被拒的生成没有「引了哪边」—— `NULL` 在这张表里已经有意思了
        # （「这一轮没走到生成」），所以这里 `return` 之后不补写是对的。
        if retrieval is not None:
            self._record_generation_side(
                context=call_context,
                retrieval=retrieval,
                materials=turn_materials,
                generation=generation,
            )
        proposals = list(generation["proposed_intents"])
        bases = [
            str(candidate["idempotency_basis"])
            for candidate in plan.document["candidate_intents"]
        ]
        if len(set(bases)) != len(bases):
            # Two candidates carrying the same business identity are one
            # candidate written twice, and keying on it would put two outbox
            # rows on one key -- the collision the key exists to prevent, moved
            # one level up. Refusing here names it; letting the INSERT fail
            # would surface as a rolled-back turn with no reason attached.
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code="PLAN_CANDIDATE_BASIS_NOT_UNIQUE",
                detail="two plan candidates share an idempotency basis",
            )
        if not proposals:
            # Zero is its own answer, and it is not "unsupported": the model
            # declined to propose anything. Distinguishing it from a rejection
            # matters because the two have different fixes -- one is a prompt,
            # the other is a policy.
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code="GENERATION_PRODUCED_NO_INTENT",
                detail="the generation step returned no proposed intent",
            )

        # **Every proposal goes through the whole chain, and one refusal does not
        # touch the others** (T44 ruling, 2026-09-04). Before this, a turn with
        # two proposals was refused wholesale with
        # `GENERATION_INTENT_COUNT_UNSUPPORTED` -- and the RFC's intent saga has
        # always allowed a turn to raise several, so the restriction was the
        # implementation deviating from the blueprint, not the other way round.
        # The ruling is therefore to change the code.
        #
        # Not in this step, deliberately: any ordering between accepted intents,
        # partial-failure semantics, and atomicity. What is here is the four
        # things the ruling names and nothing that would have to be unpicked
        # when those arrive.
        # **ProactivityPolicy's `pre_execute` check** (RFC §6.4 / `:570`;
        # ADR-0098 §一 puts this one 「在 pipeline 的 policy 层」, which is here).
        # Computed once for the turn rather than per proposal: the knob answers
        # a question about the *conversation* -- how long since the customer
        # spoke -- and it cannot differ between two proposals of one turn.
        #
        # **A profile this policy cannot evaluate ends the turn as a no-send,
        # not as an exception.** Raising here would roll the turn back and
        # return the event to the retry budget to fail the same way, turning one
        # bad profile into an unattributable retry loop. Nothing has been sent
        # yet, so refusing costs a reply and not a duplicate.
        #
        # **This branch is unreachable today, and that is defence in depth
        # rather than a gap** (independent review 2026-09-09, P3-2).
        # `merchant-profile.schema.json` makes `proactivity_policy` a top-level
        # required property and `active_window_seconds` required within it, so
        # every release that passed contract validation carries the knob. The
        # one path that could reach it is a release published under an older
        # schema and still pinned to a live conversation -- which is exactly the
        # case `load_package` cannot retroactively refuse.
        try:
            proactivity = self._proactivity_verdict(
                prepared,
                understanding=understanding,
                # **The value captured at `:1541`, before the aggregate's copy
                # was cleared** -- see `_proactivity_verdict`. Reading it off
                # `prepared.aggregate` here would be `None` every time.
                pending_exit_signal=pending_exit if isinstance(pending_exit, dict) else None,
            )
        except ProactivityPolicyError as error:
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code="PROACTIVITY_POLICY_UNREADABLE",
                detail=str(error)[:500],
            )
        # **一次生成，一次判定**（ADR-0152，用户 2026-09-23 裁定）。
        #
        # ⚠️ **摊开挪到了策略链之后**。ADR-0151 把它放在之前，于是
        # **每一段各自过了一遍全部 policy** —— 而用户裁定的正是这件事：
        # 「**分段前就判断好能不能发，拆分后语义语境可能完全不同**」。
        # 一个片段单看的判定结论，可能与看整条消息完全不同。
        #
        # 所以这里判的是**整条消息**（模型提的那一条提案，分段还在它里面），
        # 拆是**投递的事**，发生在 `_persist_accepted_proposal`。
        decisions = [
            self._decide_proposal(
                prepared,
                plan=plan,
                proposal=proposal,
                ordinal=ordinal,
                candidate_basis=bases[ordinal] if ordinal < len(bases) else str(ordinal),
                materials=turn_materials,
                proactivity=proactivity,
            )
            for ordinal, proposal in enumerate(proposals)
        ]
        accepted = [decision for decision in decisions if decision.accepted]
        refused = [decision for decision in decisions if not decision.accepted]
        if not accepted:
            # Nothing survived. The turn ends as a no-send, and every refusal is
            # still written down separately -- the wholesale reason code says
            # what happened to the turn, the per-proposal rows say what happened
            # to each proposal.
            first = refused[0]
            return self._persist_no_send(
                prepared,
                plan=plan,
                reason_code=first.reason_code,
                detail=first.detail,
                proposal_denials=tuple(refused),
                # **Taken from the row's own refusal, not from the turn**
                # (independent re-review 2026-09-09, P3-1). `first.reason_code`
                # decides what this row says, so `first` has to decide its
                # expiry too -- `_decide_proposal` ranks
                # `GENERATION_PAYLOAD_INCOMPLETE` ahead of proactivity, so a
                # turn can name that reason while quiet hours also held, and
                # reading the verdict here would attach 08:00 to a refusal that
                # waiting does not fix.
                next_allowed_at=first.next_allowed_at,
            )

        # **这一轮真的把话说出去了** —— 所以打法声明的那几条义务成立了。
        # 一条没被人听见的承诺不是承诺（见 `admit_ai_commitments` 的第 1 条判据）。
        prepared = _with_ai_commitments(
            prepared, strategy_id=plan.strategy_id, said_something=True
        )

        with self._database.transaction(tenant_id=prepared.tenant_id) as connection:
            if rejection is not None:
                # **这一轮的计划被拒了，而它仍然发了话**（T339 第二批的那条路）——
                # 拒绝必须留痕，否则「这次拒绝发生过」就没有任何证据，
                # 而下一轮的「上一行 deny 是不是同码」也就查不到东西
                # （**实测过**：第一版跳过 `_persist_no_send` 之后第一轮 0 行 deny，
                # 于是第二轮也不升级）。
                # **写在这个事务里**，与聚合迁移、帧审计、出站行同一个事务 ——
                # 所以不会出现「拒绝留了痕而消息没发出去」的半截状态。
                self._record_plan_refusal(
                    connection,
                    prepared=prepared,
                    row_id=uuid4(),
                    intent_id=None,
                    reason_code=rejection.reason_code,
                    obligations=audit_only(),
                    next_allowed_at=None,
                    context={
                        "detail": rejection.detail[:256],
                        # **与静默那条路同口径**（`_fallback_denial_context`）：
                        # 两条路的 deny 都答得出「兜底是谁、它是兜底」。
                        "fallback_strategy": plan.strategy_id,
                        "fallback_origin": str(plan.document.get("origin") or ""),
                        "asked": list(plan.document["required_slot_requests"]),
                    },
                )
            # **那条回复引了哪几条记忆**（T363）。**与 `memories_sent` 成对**：
            # 那一行答「递过去了哪几条」，这一行答「引了哪几条」，
            # 两行相减才是「递过去而没被用上的」。
            #
            # **条件与 `memories_sent` 那一处逐字相同**（递过东西或有装不下的），
            # 于是两行要么都在、要么都不在 —— 一行在而另一行不在时，
            # 「没走到生成」与「走到了生成一条都没引」就分不开了。
            if prepared.known_memories or prepared.memories_not_shown:
                self._record_memories_cited(
                    connection,
                    tenant_id=prepared.tenant_id,
                    conversation_id=prepared.conversation_id,
                    correlation_id=call_context.correlation_id,
                    offered=[str(entry["ref"]) for entry in turn_memories],
                    cited=sorted(
                        {
                            str(ref)
                            for claim in generation["claims"]
                            for ref in claim["source_evidence_refs"]
                            if str(ref).startswith(MEMORY_REF_PREFIX)
                        }
                    ),
                )
            # **丢弃要出声。** 这一轮对话题槽发了几条 update，平台只认最后一条
            # （`material_selection.topic_text`，用户 2026-09-16 裁定）——
            # 而一次**安静的**丢弃只是把旧缺陷换成新缺陷：模型多发了，
            # 平台少认了，没有任何地方记下这件事发生过。
            #
            # **写在这个事务里**，与聚合迁移同一条理由：它描述的是这一次迁移
            # 吸收了什么，而一条能在迁移回滚之后活下来的留痕描述的是没发生过的回合。
            #
            # **只记条数与槽名，不记那些值**（2026-09-16 那次脱敏的教训）：
            # 定位这件事不需要值，而留痕会被人贴到别处。
            # **AI 许了一件只有人能做的事**（T365，2026-09-19）。
            #
            # 那句话仍然被丢掉（它是一句兑现不了的预告），**而请求不再消失**：
            # 它变成一条同事看得见的待办。判据在
            # `inference_validation.needs_a_person`，这里只读那一位 ——
            # 两处各判一次就会出现「尺子认得它而这里不认」。
            #
            # ⚠️ **写在这一轮那个事务里**，与别的留痕逐字同一条理由。
            self._file_a_task_for_a_promise_only_a_person_can_keep(
                connection,
                prepared,
                plan=plan,
                unkept=unkept_promises,
            )
            superseded = superseded_topic_updates(
                current_definition, understanding=understanding
            )
            if superseded:
                self._record_plan_refusal(
                    connection,
                    prepared=prepared,
                    row_id=uuid4(),
                    intent_id=None,
                    reason_code="EXTRA_TOPIC_SLOT_UPDATES_DISCARDED",
                    obligations=audit_only(),
                    next_allowed_at=None,
                    context={
                        "slot": str(current_definition.get("material_topic_slot") or ""),
                        "discarded": superseded,
                    },
                )
            _apply_aggregate_transition(connection, prepared)
            _write_frame_entry_audit(connection, prepared, prepared.frame_entry)
            # **这一轮聊了什么，追一行**（T200，2026-09-20）。
            #
            # **在这个事务里**：它与本轮的聚合迁移是同一件事的两半 ——
            # 回合没提交成，那一行也不该留下。
            #
            # ⚠️ **只有入站那条路写它，定时那条路不写**（`:7039` 那一处）：
            # 定时回合没有客户说的话，`understand` 也没有 `turn_summary`
            # 可产 —— 给它编一行「系统自己跟进了一次」会让摘要里混进
            # **不是对话的东西**，而模型读它时分不出哪句是客户说的。
            # `test_the_conversation_remembers_what_was_said.py` 把这一条
            # 钉成断言，免得它看起来像漏了。
            self._append_summary_line(
                connection, prepared, understanding=understanding
            )
            # **The re-engagement quota, and it runs here for the reason the
            # rate gate does**: it is a count of rows, so it has to see the same
            # snapshot the writes below join (ADR-0095 §五's argument, applied to
            # a different cap). **It is the only ProactivityPolicy check that
            # cannot be moved to admission** -- 「这个检查能不能挪到准入处」, and
            # for the classification and the window the answer was yes.
            #
            # **Recorded here rather than appended to `refused`**: the loop
            # further down writes one row per entry in `refused`, so joining
            # these onto it would write each refusal twice. They also carry
            # `quota_context`, which no other refusal in this turn has.
            #
            # **No early return when nothing survives.** An empty `accepted`
            # falls through unchanged: `_rate_gate` returns empty lists, no
            # outbox row is written, `refused` is still recorded, the inbox event
            # is still consumed. That is the shape the rate gate already has when
            # it withholds everything -- one path, not a second one only this
            # gate takes.
            # **按效果分流，再进后面两道闸**（独立审查 2026-09-12，P1-1）。
            #
            # `_reengagement_gate` 与 `_rate_gate` 都是为**出站客户消息**写的：
            # 一个数「这个客户今天被主动打扰过几次」，一个数
            # 「这一分钟这条会话发出去几行」，**两个都按 `outbox_messages` 的客户行计数**。
            # 把一条 `remember` 送进去，两个方向都错：
            #
            #   * **它占名额** —— 一条不产出站行的提案挤掉了一句真要回给客户的话；
            #   * **它会被名额裁掉** —— 而 `_record_rate_limit` 随即
            #     (a) 把 `withheld_content` 写进 `policy_decisions.context`，
            #     **那是客户事实整个进审计面**；
            #     (b) 落一条待办，而那个待办面的语义是「客户在等一句没发出去的话」——
            #     **一条记忆从来不是一句话**；(c) 这条记忆就此丢了，理由码写的是「限流」。
            #
            # **这与 `_persist_accepted_proposal` 里的分派同源，本来就该一起改。**
            # 那里问的是「接受之后写什么」，这里问的是「哪几道闸管得着它」，
            # 两处的答案都来自同一个 `effect`。
            deliverable = [d for d in accepted if _queues_an_outbox_row(d)]
            direct_writes = [d for d in accepted if not _queues_an_outbox_row(d)]

            deliverable, over_quota, quota_context = self._reengagement_gate(
                connection, prepared, deliverable, proactivity=proactivity
            )
            for decision in over_quota:
                self._record_proposal_denial(
                    connection, prepared, plan=plan, decision=decision,
                    extra_context=quota_context,
                )
            # **The rate gate runs here, inside the transaction** (B1-6 /
            # ADR-0095 §五): it counts what this conversation and tenant already
            # sent in the window, so it has to see the same snapshot the writes
            # below join. Withheld proposals are **not** persisted as outbox
            # rows -- they become one manual task and one audit row, because a
            # discarded message is one nobody can look up afterwards and a
            # silently queued one is worse (both refused by the ruling).
            kept, withheld, scope = self._rate_gate(connection, prepared, deliverable)
            # ⚠️ **「一部分发、一部分丢」这个状态 2026-09-23 起不存在了**
            # （ADR-0151，用户裁定「租户也要一起改」）。两道闸现在都按轮原子，
            # 所以 `kept` 与 `withheld` **不可能同时非空** ——
            # 原先挂在 `if withheld and kept:` 上的那段重判（以及
            # `_drop_promises_the_split_no_longer_keeps`）因此是一条够不着的分支。
            #
            # **删掉而不是留着**：旧那条用例的 docstring 自己预告过 ——
            # 「够不着的分支看起来像闸而什么都没守」，本仓点过名。
            #
            # 用户 2026-09-19 裁定的后半「超额的走批 F 丢弃」**仍然成立**，
            # 只是它现在只有一条路：整轮被withheld，下面那个 `if withheld:` 接住它。
            split_promises: list[dict[str, Any]] = []
            if withheld:
                self._record_rate_limit(
                    connection,
                    prepared,
                    plan=plan,
                    withheld=withheld,
                    scope=scope or "conversation",
                    promises_dropped=split_promises,
                )
            # **不排出站行的那些，走它们自己的闸**（独立审查 P1-2）。
            # 它在这里而不在 `_decide_proposal` 里，理由与上面那句
            # 「The rate gate runs here, inside the transaction」逐字相同：
            # **它要看到下面那些写入将要 join 的同一份快照。**
            kept_writes, occupied = self._memory_gate(connection, prepared, direct_writes)
            for decision in occupied:
                self._record_proposal_denial(connection, prepared, plan=plan, decision=decision)
            for decision in [*kept, *kept_writes]:
                self._persist_accepted_proposal(
                    connection,
                    prepared,
                    plan=plan,
                    decision=decision,
                    materials=turn_materials,
                    proactivity=proactivity,
                )
            for decision in refused:
                # A refusal that happened alongside an acceptance is still a
                # refusal, and it is recorded on the same terms: its own row,
                # naming its own proposal. Without this the surviving intents
                # would be the only trace of a turn in which something was
                # turned down.
                self._record_proposal_denial(connection, prepared, plan=plan, decision=decision)
            # **这是用户报的那个形状发生的地方**：`send_message` 被接受、
            # `manual_task` 被拒，于是客户听见 AI 应下一件事，而同事那边
            # 什么都没有。`kept` 传进去是为了把「AI 刚才答应了什么」
            # 附在待办上——那句话是同事要接着往下办的东西。
            self._file_task_for_unsendable_intents(
                connection,
                prepared,
                plan=plan,
                refused=refused,
                promised=kept,
                materials=turn_materials,
            )
            _consume_inbox_event(connection, prepared)
            # **问出去了才算问过**（RFC §3.2 的 `pending_question`，B1-2）：
            # `kept` 是活到出站的那批，被限流闸挡下的回合不写这一条。
            # 排在刷新之前，因为刷新那一句会把整份聚合写回 `state`。
            # `materials` 与出站渲染是同一份 —— 正文判据要与真的发出去的一致。
            _record_pending_question(
                prepared, plan=plan, delivered=kept, materials=turn_materials
            )
            # **本轮收口的最后一句**（RFC §3.2 的那两个引用数组，B1-5）：
            # 放在最后，是因为本轮自己的意图与定时事件都在上面几句里才写进去。
            _refresh_reference_projection(connection, prepared)
        return PipelineResult(
            inbox_id=prepared.inbox_id,
            status="consumed",
            # **`kept`, not `accepted`** (independent review 2026-09-08, P2-1).
            # `_decide_proposal` mints `intent_id` / `outbox_id` at decision
            # time, not at insert time, so a withheld proposal carries a pair of
            # ids for rows that were never written. Reporting `accepted` here
            # handed the caller ids that do not exist in the database --
            # measured: two `outbox_id`s back, zero of them present.
            # `probe_real_provider_pipeline.py` prints this result verbatim, so
            # the first time the gate bit during a real-provider probe the
            # evidence would have carried invented ids.
            intent_ids=tuple(
                decision.intent_id for decision in kept if decision.intent_id is not None
            ),
            outbox_ids=tuple(
                decision.outbox_id for decision in kept if decision.outbox_id is not None
            ),
        )

    @staticmethod
    def _timer_rule_id(prepared: PreparedEvent) -> str | None:
        """The `TimeRule` this timer came from, or `None` for any other event.

        B4-2 wrote `scheduled_events.payload_ref = {"rule_id": …}` and the
        scheduler copies it into the timer envelope's `content.event.payload_ref`
        verbatim, so the rule is recoverable here without a second query. The
        rule itself is **not** copied: `strategy` is read from the pinned
        release, which is the point of `payload_ref` carrying only an id.
        """

        if str(prepared.envelope.get("event_type")) != "timer":
            return None
        content = prepared.envelope.get("content") or {}
        event = content.get("event") if isinstance(content, dict) else None
        payload_ref = event.get("payload_ref") if isinstance(event, dict) else None
        if not isinstance(payload_ref, dict):
            return None
        rule_id = payload_ref.get("rule_id")
        return str(rule_id) if rule_id else None

    def _required_strategy(self, prepared: PreparedEvent) -> str | None:
        """The strategy the rule behind this timer declared, if there is one.

        `None` for a customer message -- the model chooses, as it always has --
        and `None` for a timer whose rule the pinned release no longer declares
        (a release can be superseded while a timer is pending; refusing then
        would turn a publish into a dropped conversation).
        """

        rule_id = self._timer_rule_id(prepared)
        if rule_id is None:
            return None
        return strategy_for_rule(profile=prepared.profile, rule_id=rule_id)

    @staticmethod
    def _outbound_kind_for_this_turn(prepared: PreparedEvent | None, proposed: str) -> str:
        """一条出站提案在**这一轮**里的 kind —— 定时器到期的那种记成 `followup`。

        用户 2026-09-16 裁定：「`followup` 就是定时器到期后主动跟进那一条」。

        ## 为什么由平台判，而不是让模型提名

        **本仓已经为同一个问题裁过一次**：`_PLATFORM_OWNED_ITEM_FIELDS` 把
        `frame_id` 与 `evidence_event_id` 从模型要答的东西里拿掉，理由是
        **「identity, not semantics —— 平台供给身份」**（那次不拿掉的代价是
        `slot_updates` 恒为空，登记 T34）。

        「这一轮是不是定时器到期」同样是身份不是语义：**平台知道**
        （`event_type == "timer"`），而让模型提名它要付四层的账 ——
        提示词多一行（实测线路 **+145 字节**，吃素材预算）· 计划步也要改
        （`GENERATION_ADDED_INTENT`：生成提的 kind 必须在计划的候选里）·
        **三份在架配包的 `allowed_intent_kinds` 都要加一个值**
        （`candidate_intents` 只能用策略允许的 kind）· 而模型还可能提错。

        **判错的方向也不同**：平台判是确定的；模型提名则是「它可能忘了」，
        而忘了的结果是这条记录说它是一次被动回复 —— 一个安静的错。

        ## 它换来的是什么

        在此之前「这条出站是不是定时器到期后的主动跟进」**在 `intent_records`
        里答不出来**：`kind` 是 `send_message`，`proposed_by` 只有
        `{"step": "generate", "version": ...}`，而 `strategy_id` 是**配包自己取的
        名字**。于是这一问只能 join 回 `scheduled_events` 再读 `payload_ref.rule_id`。
        **现在它是 `kind` 的一个值。**

        **只改这一个 kind。** `remember` 在定时器回合里仍然是 `remember` ——
        它不是一次出站，改它的名字只会让「记住了什么」这一问多一个分支。
        """

        proactive = PROACTIVE_EQUIVALENT.get(proposed)
        if proactive is None:
            return proposed
        # `prepared` 在 `test_proposal_decision_unit` 那一族里是 `None` ——
        # 那些用例喂的是 payload 那一半，**它们不该被迫造一个回合**。
        # 没有回合就没有「这一轮是不是定时器」，答案因此是原样。
        if prepared is None:
            return proposed
        if str(prepared.envelope.get("event_type")) != "timer":
            return proposed
        return proactive

    def _read_the_silence(
        self, prepared: PreparedEvent, *, call_context: CallContext
    ) -> dict[str, Any] | None:
        """主动打破静默之前先问一句「他为什么不说话」（ADR-0149）。

        `None` = 这一轮不该问这一句。

        ## 判据是**锚点**，不是 `emit_kind`

        定时器分两族，而分界不是一张写死的 kind 清单：

        | 锚点 | 它为什么响 | 问不问 |
        |---|---|---|
        | `last_outbound_at` / `frame_entered_at` | **因为什么都没发生** | 问 |
        | `commitment_due_at` | **因为一件具体的事到期了** | 不问 |

        **承诺那一族不能问，理由比「省一次调用」硬**：`ai_commitment_due` 是
        「我们答应过要做的事到点了」，而这一步的动作词表里有 `wait` ——
        于是它可能答「再等等」，**而那等于我们毁了自己许下的承诺**。
        那一轮的内容与理由本来就确定（在那条承诺里），
        再分析一次静默是答一个已经有答案的问题，还可能答错。

        **按锚点判而不按 kind 判**，好处是配包加一条新规则时平台不用改：
        它只要选一个锚点，就自动落进正确的那一族。写死一张 kind 清单的话，
        下一个配包作者加一条 `price_drop_due`，平台认不出来。

        ⚠️ **认不出那条规则时不问**（fail-closed 到「今天的行为」）：
        配包在事件排下之后改过、或那条规则被删了，都会走到这里。
        这一步是增强不是闸，认不出就退回三步原样。
        """

        kind = self._timer_event_kind(prepared)
        if kind is None:
            return None
        frame = active_frame(prepared.aggregate)
        if frame is None:
            return None
        definition = frame_definitions(prepared.profile).get(str(frame["definition_id"]))
        if definition is None:
            return None
        anchors = {
            str((rule.get("due") or {}).get("anchor"))
            for rule in definition.get("time_rules") or []
            if isinstance(rule, Mapping) and str(rule.get("emit_kind")) == kind
        }
        if not anchors & SILENCE_ANCHORS:
            return None

        # **这一轮真的被 offer 的话术** —— 与生成步那一处调的是同一个函数、
        # 同一份 `delivered`，所以「能发哪几份」这一问只有一个答案。
        #
        # ⚠️ **`understanding=None`**：那个参数是给话题闸用的（`on_request`
        # 的素材要看客户这一轮提了什么），而这一轮客户什么都没说。
        # 传 `None` 与「没有话题」是同一件事，不是「读不到」。
        offered = select_materials(
            definition=definition,
            declared=materials_for_frame(prepared.profile, definition),
            emit_kind=kind,
            delivered=prepared.delivered_materials,
            already_sent=prepared.delivered_materials,
        )
        reading = self._inference.read_silence(
            envelope=prepared.envelope,
            aggregate=prepared.aggregate,
            profile=prepared.profile,
            context=call_context,
            last_outbound=prepared.last_outbound,
            conversation_summary=prepared.conversation_summary,
            summary_lines_not_shown=prepared.summary_lines_not_shown,
            materials_already_sent=prepared.delivered_materials,
            silent_for_seconds=self._silent_for_seconds(prepared),
            unanswered_rounds=prepared.unanswered_rounds,
            offered_materials=offered,
        )
        return _without_an_unsendable_ref(reading, offered=offered)

    @staticmethod
    def _silent_for_seconds(prepared: PreparedEvent) -> float:
        """他已经多久没说话了 —— **而「算不出来」要留下一个数**（运维缺陷 20）。

        锚点是 `last_outbound_at`，它由投递那一刻写进**列**，
        每轮开头从列刷进聚合（见那一处的留痕）。**空只有一种正当含义**：
        这通对话一条出站都没发过。

        ⚠️ **兜底不改**：`_seconds_since` 读不出回 `0.0`，模型因此倾向 `wait`
        而不是倾向再发一条 —— 运维复核时点名说这个方向选得对。
        **改的是它的沉默**：`0.0` 先前同时表示「刚刚」与「说不上来」，
        而那正是缺陷 20 活下来的方式 —— 输出永远合理，因为输入永远是同一个值。
        """

        anchor = prepared.aggregate.get("last_outbound_at")
        if anchor is None:
            SILENCE_ANCHOR_MISSING_TOTAL.increment("last_outbound_at")
        return _seconds_since(anchor)

    @staticmethod
    def _timer_event_kind(prepared: PreparedEvent) -> str | None:
        """The `emit_kind` of the scheduled event that produced this timer.

        `None` for a customer message. The scheduler copies the kind into the
        timer envelope's `content.event.kind` (`scheduler.enqueue_due`), so this
        is recoverable without a second query.
        """

        if str(prepared.envelope.get("event_type")) != "timer":
            return None
        content = prepared.envelope.get("content") or {}
        event = content.get("event") if isinstance(content, dict) else None
        kind = event.get("kind") if isinstance(event, dict) else None
        return str(kind) if kind else None

    def _proactivity_verdict(
        self,
        prepared: PreparedEvent,
        *,
        understanding: dict[str, Any],
        pending_exit_signal: dict[str, Any] | None,
    ) -> ProactivityVerdict | None:
        """`None` unless this turn is a proactive send.

        **The scope is timer-triggered turns** (RFC §6.4's own framing; the
        argument is in `proactivity.py`'s module docstring). A reply to a message
        the customer just sent is not a proactive send.

        **`pending_exit_signal` is passed in, and it has to be** (B4-4c).
        `process_prepared` **clears it on the aggregate** as soon as it hands the
        signal to the planner, which happens well before this runs -- so reading
        the field here would find `None` on every single turn, and the durable
        half of ADR-0115's mapping would be dead without anything failing. What
        is passed is the value captured before that clear.
        """

        if str(prepared.envelope.get("event_type")) != "timer":
            return None
        return evaluate_proactivity(
            knobs=proactivity_knobs(prepared.profile),
            aggregate=prepared.aggregate,
            event_kind=self._timer_event_kind(prepared),
            now=datetime.now(UTC),
            understanding=understanding,
            pending_exit_signal=pending_exit_signal,
        )

    def _decide_proposal(
        self,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan,
        proposal: dict[str, Any],
        ordinal: int,
        #: 这一条提案的幂等基。**显式传进来，不再按 `ordinal` 去下标计划候选**
        #: （ADR-0151）：分段之后提案条数会多于计划候选条数，
        #: `candidates[ordinal]` 会越界 —— 而那两份文档按位置对齐本来就是
        #: 本仓最不喜欢的那种耦合：**两个模型文档，一个下标**。
        candidate_basis: str,
        #: 这一段发出去之后要停多久，毫秒。非分段行是 0。
        pace_ms: int = 0,
        materials: Mapping[str, str] | None = None,
        proactivity: ProactivityVerdict | None = None,
    ) -> ProposalDecision:
        """Run one proposal through the full policy chain. Decides; writes nothing.

        Separated from the persistence so that a refusal costs the same whether
        it is the only proposal or one of five, and so that the loop above has
        nothing to short-circuit: every proposal reaches this function.
        """

        kind = self._outbound_kind_for_this_turn(prepared, str(proposal["kind"]))
        if kind not in GENERATION_SUPPORTED_KINDS:
            return ProposalDecision(
                ordinal=ordinal,
                kind=kind,
                accepted=False,
                reason_code=UNSUPPORTED_INTENT_REASON,
                detail=f"the send path does not carry the {kind} intent yet",
                proposal=proposal,
            )

        # **A payload that cannot be sent is refused here, where a refusal
        # still leaves a record** (T110; ADR-0095 §六 ruled the shape).
        #
        # A real model returned `typed_payload = {"message_type":
        # "acknowledgement"}` -- schema-valid (`typed_payload` is
        # `{"type": "object"}`), kind `send_message`, so every guard in front of
        # this one passed. The payload then reached
        # `_persist_accepted_proposal`, where `typed_payload["content"]` raised
        # `KeyError`. **That is worse than a refusal**: the exception rolls the
        # whole turn back, so the inbox event is not consumed, no `plan.no_send`
        # is written, no policy decision is recorded -- and the event returns to
        # the retry budget to crash the same way. A model output that is merely
        # incomplete became an unattributable retry loop.
        #
        # **The contract is deliberately not tightened** (ADR-0095 §六): making
        # `content` required in `generation-result` would turn one bad proposal
        # into a whole-plan schema failure, which is coarser and inconsistent
        # with ADR-0084 §三's "fail closed, and count it". The annotation there
        # says what sending needs; this is where it is enforced, per proposal.
        #
        # **Presence, and then readability** -- the second half is
        # 2026-09-10's correction. This comment used to say "presence and type
        # only", on the argument that whether `content` is *sendable* is the
        # channel's question, which it "already answers without crashing".
        #
        # It does answer without crashing. **What it answers is `failed`,
        # terminally, after the row has been queued** -- and `failed` is not
        # retried, because retrying an unsendable payload changes nothing. So
        # "let the channel decide" meant, in the third real window, three
        # customer replies composed, approved, queued and lost: the reply the
        # customer was waiting for never arrived and nothing asked a person
        # about it.
        #
        # A refusal *here* costs a `plan.no_send` with a reason code and leaves
        # the turn attributable -- the same argument T110 made one guard
        # earlier, for the same reason.
        #
        # **Still not a shape requirement.** `customer_text` reads every shape
        # the platform produces; what is refused is content with no words in it
        # at all, which no adapter can carry.
        payload = proposal.get("typed_payload")
        # **每个 kind 问自己的问题**（E3-3）。这里原本写死
        # `if kind == "send_message" and not isinstance(content, dict)`，
        # 于是第二个 kind 会遇到一条**问错问题**的拒绝——
        # 注释里那四条的第 4 条讲的就是它。
        # **绑一次，下面那条二次守卫也读它**——两条守卫曾经用两种问法问同一件事，
        # 而那正是一个边界的两端开始各说各话的方式（这句是原注释，照留）。
        content = payload.get("content") if isinstance(payload, dict) else None
        write_path = GENERATION_WRITE_PATHS[kind]
        refusal = write_path.payload_refusal(payload)
        if refusal is not None:
            return ProposalDecision(
                ordinal=ordinal,
                kind=kind,
                accepted=False,
                reason_code="GENERATION_PAYLOAD_INCOMPLETE",
                detail=refusal,
                proposal=proposal,
            )

        # **payload 齐了，再问这一回合收不收**（E4-2）。
        #
        # **顺序是 payload 在前、处境在后**，而这不是随意排的：
        # 一条 payload 就不全的提案，它的处境问题是**次生的**——
        # 先报「这个字段商家没声明」会让人去改 profile，
        # 而真正的问题是模型连 `field_key` 都没给。**第一条拒绝要指向最根本的原因。**
        if write_path.turn_refusal is not None:
            situation = write_path.turn_refusal(payload, prepared)
            if situation is not None:
                return ProposalDecision(
                    ordinal=ordinal,
                    kind=kind,
                    accepted=False,
                    reason_code=UNRECORDABLE_INTENT_REASON,
                    detail=situation,
                    proposal=proposal,
                )

        # **The turn's mapping, passed in rather than recomputed** (independent
        # review 2026-09-10, P3-2). It used to be derived here and again in
        # `_persist_accepted_proposal`, with a comment asserting the two were
        # equal -- true only while both spellings kept naming the same frame,
        # which is precisely what P1-1 found they had stopped doing.
        # `validate_generation` has already refused a ref that is not in here;
        # this is what turns the ones that are into the merchant's words.
        # **按效果选中**（E4-2，独立审查 2026-09-12 P3-1）。这道闸问的是
        # 「渠道读不读得了这段话」——**那个问题只对会排出站行的 kind 成立**。
        # `remember` 写一行记忆、一个字都不发，它的 payload 里有没有 `content`
        # 与任何渠道无关。
        #
        # 原写法靠一条**关于别的 kind 的假设**成立：「`content` is `None` for
        # every other kind」。那句今天仍为真（`remember` 的 payload 没有 `content`），
        # **但它是巧合不是约束**——下一个 payload 里带 `content` 对象的 kind
        # 会拿到一条替 `send_message` 说话的拒绝，正是 2026-09-07 P2-1 要消灭、
        # 而 E3-3 只在第一条守卫上做到了的那件事。
        if (
            isinstance(write_path.effect, OutboxDelivery)
            and isinstance(content, dict)
            and customer_text(content, materials=materials) is None
        ):
            # **The words, not the wrapper.** `content` is an object, so the
            # guard above passed, and there is still nothing in it a text
            # channel can send. Refused with a record rather than queued to
            # fail terminally at the adapter.
            #
            # **Keyed on `isinstance` rather than on `kind`** (independent
            # review 2026-09-10, P3-3). The two select the same proposals -- the
            # guard above returns for every `send_message` whose `content` is
            # not a dict, and `content` is `None` for every other kind -- but
            # only this spelling lets the `detail` below read the keys without a
            # second `isinstance` whose else-branch can never run. **That dead
            # branch was the finding**: it would have put the model's whole
            # `content` *value* -- the customer-visible text -- into
            # `policy_decisions.context`, which is the exact opposite of the
            # comment sitting on top of it.
            return ProposalDecision(
                ordinal=ordinal,
                kind=kind,
                accepted=False,
                reason_code="GENERATION_CONTENT_UNREADABLE",
                # **Key names only**, and bounded, for the reason the guard
                # above is bounded: the values here are the customer-visible
                # text itself, and `policy_decisions.context` is not where that
                # belongs.
                detail=(
                    # **说自己的 kind**（P3-1 的另一半）。写死 `send_message`
                    # 时，第二个走出站路径的 kind 会得到一条替别人说话的拒绝。
                    f"a {kind} content needs text this channel can read; "
                    f"this one has {sorted(content)[:20]}"
                )[:500],
                proposal=proposal,
            )

        # **ProactivityPolicy, refused here** -- RFC `:570` puts this check at
        # `pre_execute`, and `pre_execute` in this pipeline is this function.
        # The verdict was decided once for the turn; what is per-proposal is the
        # record, and it is the same record every other refusal in this chain
        # gets.
        if proactivity is not None and not proactivity.allowed:
            return ProposalDecision(
                ordinal=ordinal,
                kind=kind,
                accepted=False,
                reason_code=proactivity.reason_code,
                detail=proactivity.detail[:500],
                proposal=proposal,
                next_allowed_at=proactivity.next_allowed_at,
            )


        intent_id = uuid4()
        policy_id = uuid4()
        # **只有真会排出站行的 kind 才领这个号**（E4-2）。
        # `ProposalDecision.outbox_id` 本来就是 `UUID | None`，而在 E4-2 之前
        # 它从来没有 `None` 过——上面那个聚合处 `if ... is not None` 于是是一句
        # **从未为假的条件**。`remember` 是第一个让它有意义的 kind：
        # 给一条不发任何东西的提案发一个 outbox 号，等于在数据里留一个
        # 指向不存在的行的指针。
        outbox_id = uuid4() if isinstance(write_path.effect, OutboxDelivery) else None
        event_id = UUID(str(prepared.envelope["event_id"]))
        target_frame_id = UUID(str(prepared.aggregate["target_stack"][-1]["id"]))
        # **The key is built from the plan's business identity, not from the
        # proposal's position.** `send_message:{event_id}` alone was unique only
        # while a turn could raise one intent -- two proposals from one event
        # collided on the outbox's unique key, and the second insert failed
        # *after* the first was written.
        #
        # The first fix put the array index in the key. That distinguishes them
        # and is stable across a retry, but it is not what the key is *for*:
        # `conversation-engine.md:184` defines `idempotency_key` as "tenant +
        # intent kind + **business identity**", and an index is where the model
        # happened to put this proposal in a JSON array. Two identical turns
        # returned in the opposite order would carry swapped keys -- the shape
        # §7.2 lists as "timeout, then resend under a new idempotency key"
        # (independent review 2026-09-05, P2-1).
        #
        # The plan already carries the field the RFC means: each candidate's
        # `idempotency_basis` (`plan-result.schema.json:112`). The upstream
        # guards make the correspondence usable -- generation may not exceed the
        # plan's candidate count, so proposal *i* has candidate *i* to name it.
        # The ordinal survives only where it belongs: in the audit record, as
        # "which proposal was this".
        idempotency_key = (
            f"{GENERATION_WRITE_PATHS[kind].idempotency_prefix}:{event_id}:{candidate_basis}"
        )
        intent_contract: dict[str, Any] = {
            "id": str(intent_id),
            "tenant_id": str(prepared.tenant_id),
            "conversation_id": str(prepared.conversation_id),
            "kind": GENERATION_WRITE_PATHS[kind].kind,
            "status": "PROPOSED",
            "proposal": proposal["typed_payload"],
            "proposed_by": {"step": "generate", "version": self._inference.version},
            "strategy_id": plan.strategy_id,
            "target_frame_id": str(target_frame_id),
            "idempotency_key": idempotency_key,
            "preconditions": {
                "aggregate_version": prepared.aggregate["state_version"],
                "aggregate_generation": prepared.aggregate["generation"],
                "deadline_at": prepared.envelope["runtime_deadline_at"],
            },
            "policy_evidence": [str(policy_id)],
            "execution_token": None,
            "lease_expires_at": None,
            "external_operation_ref": None,
            "result": None,
            "inverse": None,
            "unknown_reason": None,
            "manual_task_id": None,
        }
        validate_contract("intent-record", intent_contract)
        allowed = prepared.aggregate["control_mode"] == "ai"
        policy_context = {
            "tenant_id": str(prepared.tenant_id),
            "aggregate_generation": prepared.aggregate["generation"],
            "control_mode": prepared.aggregate["control_mode"],
            # What makes a denial answerable: which proposal, and of what kind.
            "proposal_ordinal": ordinal,
            "proposal_kind": kind,
        }
        if proactivity is not None:
            # **Evidence, and RFC §16 (`:925`) asks for exactly this split**:
            # proactivity metrics reported by `in_session_continuation /
            # reengagement`. This row is where that split becomes queryable.
            policy_context["proactivity_classification"] = proactivity.classification
        policy_contract: dict[str, Any] = {
            "input": {
                "hook": "pre_execute",
                "context": policy_context,
                "target_frame": prepared.aggregate["target_stack"][-1],
                "strategy_or_intent": proposal,
            },
            "output": {
                "effect": "allow" if allowed else "deny",
                "reason_code": "M0_AI_CONTROL_ALLOWED" if allowed else "HUMAN_CONTROL_ACTIVE",
                "modifications": {},
                "obligations": ["audit", "redact"],
                "policy_id": "m0-minimum-policy",
                "policy_version": "1.0.0",
                "next_allowed_at": None,
                "suppressed_counter": None,
                "terminal_exit": False,
            },
        }
        validate_contract("policy-hook", policy_contract)
        return ProposalDecision(
            ordinal=ordinal,
            kind=kind,
            accepted=allowed,
            reason_code=str(policy_contract["output"]["reason_code"]),
            detail="pre-execute policy denied AI control" if not allowed else "",
            intent_id=intent_id,
            policy_id=policy_id,
            outbox_id=outbox_id,
            proposal=proposal,
            intent_contract=intent_contract,
            policy_context=policy_context,
            idempotency_key=idempotency_key,
            pace_ms=pace_ms,
        )

    @staticmethod
    def _contact_of(connection: Any, prepared: PreparedEvent) -> UUID | None:
        """The contact this conversation belongs to, or `None`.

        `conversation_aggregates.contact_id` is nullable because a conversation
        can exist before its contact is identified; `prepare` back-fills it when
        a later message carries an identity. Read once so the lock and the count
        agree about which scope they are working in.
        """

        row = connection.execute(
            "SELECT contact_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (prepared.tenant_id, prepared.conversation_id),
        ).fetchone()
        if row is None or row["contact_id"] is None:
            return None
        return UUID(str(row["contact_id"]))

    @staticmethod
    def _memory_gate(
        connection: Any,
        prepared: PreparedEvent,
        accepted: list[ProposalDecision],
    ) -> tuple[list[ProposalDecision], list[ProposalDecision]]:
        """「这个字段已经有一条 active 记忆了吗」——**在写入的那个事务里重问一遍**。

        `_remember_turn_refusal` 的第 4 条问的是 `prepared.remembered_fields`，
        **而那是 `prepare()` 事务里读的一份快照**，写入发生在另一个事务里。
        独立审查 2026-09-12 P1-2 指出中间有两个缺口，这个函数把两个都关掉：

        **① 同一回合内。** `_decide_proposal` 对所有提案跑完才开始写，
        于是两条 `field_key` 相同的 `remember` **都会通过**，
        第二行撞 `contact_memories_one_active_per_field_idx` →
        事务内异常 → 整轮回滚 → inbox 事件不消费 → 退回重试预算再炸一次。
        **那正是自审 §2.3 用来论证这道闸存在理由的那个形状**，
        也是 T44 的原话（「`send_message:{event_id}` 只在『一轮一个 intent』时唯一」）
        换了一张表。下面用 `taken` 累计本回合已接受的字段。

        **② 跨会话。** 一个 contact 可以有多条会话，而
        `conversation_aggregates` 的 `FOR UPDATE` 锁**按会话**，看不见兄弟会话。
        `_reengagement_gate` 为**完全相同**的理由取了同一把咨询锁。

        **锁的成本判断与 `_reengagement_gate` 不同，所以要单独说。**
        那一条的注释说「那条路几乎不走，锁近乎免费」；
        `remember` 走在每一条能记东西的客户消息上，**取锁这件事会经常发生**。
        取它仍然是对的，因为**「经常取」不等于「经常争用」**：
        争用要求同一个 contact 的两条会话在同一瞬间各跑一回合，
        而那正是必须不能错的那种情况。一把无争用的
        `pg_advisory_xact_lock` 是一次内存哈希表操作，随提交或回滚释放。
        **代价是同一 contact 的回合被串行化**——这一点写在这里，
        不藏在「照抄了上面那条」后面。

        **顺序与 `_reengagement_gate` 相同**：在聚合行锁**之后**取，
        且没有任何地方在持有本锁时去取聚合锁，所以两者不成环。

        ## ③ 过期的那一条不算占用（T299，2026-09-20）

        商家给一个字段声明了 `ttl_days` 之后，这条闸问的那一问变了：
        不是「这个字段有没有一行 `active`」，是「**有没有一行按商家今天的声明
        还算数**」。两者在本批之前是同一件事。

        ⚠️ **而过期那一行仍然占着唯一索引** ——
        `contact_memories_one_active_per_field_idx` 的条件是 `status = 'active'`，
        它不认得过期。所以放行的同时必须把那一行 `superseded`，
        否则 `INSERT` 撞索引、整轮回滚。**那个动作写在放行的那一支里面**，
        不在它旁边：只有真的要写新值时才腾位。
        """

        if not accepted:
            # 一条都没有时连锁都不取：绝大多数回合走这一支。
            return [], []

        contact_id = InboxProcessor._contact_of(connection, prepared)
        if contact_id is None:
            # 闸 4 已经拒过这种情况了；能走到这里说明 contact 在两次读之间
            # **消失了**，那不该静默通过。
            #
            # **这里必须 `replace`，而第一版没有**（独立审查复审 2026-09-12，R1）。
            # 把原样的决策当拒绝交出去，它仍然 `accepted=True`、
            # `reason_code` 还是决策链给的那个（`M0_AI_CONTROL_ALLOWED`）、
            # `detail` 是空串；而调用点紧接着 `_record_proposal_denial`，
            # 那个函数写 `effect='deny'` 且 `reason_code=decision.reason_code`。
            # **落到库里的是一条「effect=deny，reason_code=策略放行了，detail 空」
            # 的拒绝记录**——运维读不懂的那一种。
            #
            # 本函数另外两条拒绝路径都改写了决策，**只有这一条没有**，
            # 而守它的用例当时只断言了 `len(occupied) == 1`：
            # **断言强度的缺口正好落在唯一没改写决策的那条分支上。**
            return [], [
                replace(
                    decision,
                    accepted=False,
                    reason_code=UNRECORDABLE_INTENT_REASON,
                    detail=(
                        "the contact this conversation belongs to disappeared between "
                        "the decision and the write, so there is nobody to remember about"
                    ),
                )
                for decision in accepted
            ]

        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (str(prepared.tenant_id), str(contact_id)),
        )
        rows = connection.execute(
            """
            SELECT field_key, created_at FROM contact_memories
             WHERE tenant_id = %s AND contact_id = %s AND status = 'active'
            """,
            (prepared.tenant_id, contact_id),
        ).fetchall()
        # **过期的那几条不算占用**（T299，2026-09-20）。
        # `status = 'active'` 与「这条还算数」在本批之后不是一回事 ——
        # 召回那一处已经不把它们喂给模型了（`_remembered_memories`），
        # 而**只做那一半会造出一个更糟的形状**：模型看不见那条旧事实，
        # 也写不进新的，于是那个字段从此空着，**没有任何东西会变红**。
        lifetimes = ttl_days_by_field(prepared.profile)
        now = datetime.now(UTC)
        live = live_memories([dict(row) for row in rows], now=now, lifetimes=lifetimes)
        taken = {str(row["field_key"]) for row in live}
        expired = {str(row["field_key"]) for row in rows} - taken

        kept: list[ProposalDecision] = []
        occupied: list[ProposalDecision] = []
        for decision in accepted:
            payload = (decision.proposal or {}).get("typed_payload") or {}
            field_key = str(payload.get("field_key", ""))
            if field_key in taken:
                occupied.append(
                    replace(
                        decision,
                        accepted=False,
                        reason_code=UNRECORDABLE_INTENT_REASON,
                        detail=(
                            f"{field_key!r} already has an active memory as of this "
                            "transaction, and the model may only append, never rewrite "
                            "one (ADR-0131 / 裁定八)"
                        )[:500],
                    )
                )
                continue
            if field_key in expired:
                # **过期那一行让位，而让位这件事要真的写进库** ——
                # `contact_memories_one_active_per_field_idx` 是
                # `UNIQUE (tenant_id, contact_id, field_key) WHERE status = 'active'`，
                # 所以那一行**仍然占着这个槽**。不腾，下面那条 `INSERT` 撞唯一索引
                # → 事务内异常 → 整轮回滚 —— **正是这道闸存在的理由那个形状**。
                #
                # ⚠️ **只腾判定已过期的那一行，活着的一行永远不碰**：
                # 裁定八说模型只能追加、不得改写现有记忆，而那一条一个字没松。
                # 判据是上面那个 `expired` 集合，它由 `live_memories` 算出来，
                # **不是「这一轮想写这个字段」**。
                #
                # 这个动作本身不是新语义：控制面的改值路由
                # （`app.py` 那条 `UPDATE … SET status = 'superseded'` 紧跟一条
                # `INSERT`）用的就是它，而那正是「一条事实有了新版本」的样子。
                # ⚠️ 不可逆：`054` 的触发器写着 superseded 不能再变回 active。
                # **用户 2026-09-20 裁定：过期那行写新值时顺手 superseded。**
                connection.execute(
                    """
                    UPDATE contact_memories SET status = 'superseded'
                     WHERE tenant_id = %s AND contact_id = %s
                       AND field_key = %s AND status = 'active'
                    """,
                    (prepared.tenant_id, contact_id, field_key),
                )
                expired.discard(field_key)
            # **本回合内也算占用。** 不加这一行，同一回合的第二条同名提案
            # 会通过，然后在 `INSERT` 上炸掉整轮。
            taken.add(field_key)
            kept.append(decision)
        return kept, occupied

    def _reengagement_gate(
        self,
        connection: Any,
        prepared: PreparedEvent,
        accepted: list[ProposalDecision],
        *,
        proactivity: ProactivityVerdict | None,
    ) -> tuple[list[ProposalDecision], list[ProposalDecision], dict[str, Any]]:
        """Trim a re-engagement turn to the rolling cap the profile declares.

        RFC §6.4: 「默认每客户滚动 **24 小时最多 1 次**，profile 只能收紧；计数以实际
        delivered 的 re-engagement 为准，UNKNOWN 在 reconcile 前占用名额以防重复发送。」

        **In-session continuations are neither counted nor gated** -- 「不计入
        每日主动配额」 -- which is the reason the classification exists at all,
        and the half a test that only checked the refusal would miss.

        **Per customer, not per conversation.** The cap is about how often a
        person is contacted, and one contact can hold several conversations --
        counting per conversation would let two of them each spend the day's one
        message. `conversation_aggregates.contact_id` is the join;
        **when it is NULL the count falls back to this conversation alone**,
        which is the narrowest true statement available (the column is nullable
        because a conversation can exist before its contact is identified, and
        `prepare` back-fills it later).

        Returns `(kept, over_quota, context)`. The context is the same for every
        refusal in the turn, so it is built once and handed to the caller rather
        than rebuilt per row.
        """

        if proactivity is None or proactivity.classification != RE_ENGAGEMENT:
            return accepted, [], {}
        knobs = proactivity_knobs(prepared.profile)
        window = f"{int(knobs.reengagement_window_seconds)} seconds"
        contact_id = self._contact_of(connection, prepared)
        if contact_id is not None:
            # **Serialise the contact for the rest of this transaction**
            # (independent review 2026-09-09, P2-2; user's ruling 2026-09-09).
            #
            # **The first version declined to lock, by analogy with T215 -- and
            # the analogy does not hold.** T215 accepts the tenant-wide
            # overshoot in `_rate_gate` because a tenant-wide lock would
            # serialise **every turn in the tenant**, and `_rate_gate` runs on
            # every turn. **This gate returns above on anything that is not a
            # re-engagement**, and a re-engagement is by definition about one per
            # customer per day -- so the lock is taken on a path that is almost
            # never travelled, and it costs approximately nothing.
            #
            # **What it buys is the whole reason the cap exists**: without it,
            # two conversations of the same contact can each read "one slot
            # free" and each send, and the customer gets two marketing messages
            # on a day the profile allows one. The conversation-level count is
            # already exact for a different reason -- `_apply_aggregate_transition`
            # holds `SELECT … FOR UPDATE` on `conversation_aggregates` -- and
            # **that lock is per conversation, so it is precisely the one that
            # cannot see a sibling.**
            #
            # **An advisory lock rather than `SELECT … FOR UPDATE` on `contacts`**:
            # it needs no privilege the worker does not already have, it works
            # when the contact row is being created concurrently, and it is
            # released by commit or rollback without a second statement.
            # **Lock order is total**: this is taken *after* the aggregate row
            # lock and nothing takes the aggregate lock while holding this, so
            # the two cannot cycle.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (str(prepared.tenant_id), str(contact_id)),
            )
        counted = connection.execute(
            # **No table alias on purpose**: `AUDIENCE_IS_CUSTOMER_SQL` and
            # `RE_ENGAGEMENT_SQL` are the shared spellings of two rules, and
            # rewriting them here to carry a prefix would be a third copy of
            # each -- the thing the constants exist to prevent. The conversation
            # set moves into a subquery instead.
            f"""
            SELECT count(*) AS delivered
              FROM outbox_messages
             WHERE tenant_id = %s
               AND conversation_id IN (
                     SELECT id FROM conversation_aggregates
                      WHERE tenant_id = %s
                        AND (id = %s
                             OR (contact_id IS NOT NULL AND contact_id = (
                                   SELECT contact_id FROM conversation_aggregates
                                    WHERE tenant_id = %s AND id = %s))))
               AND {AUDIENCE_IS_CUSTOMER_SQL}
               AND {RE_ENGAGEMENT_SQL}
               AND status = ANY(%s)
               -- **`updated_at`, not `created_at`** (independent review
               -- 2026-09-09, P3-1). RFC §6.4 counts what the customer was
               -- actually contacted with, and `created_at` is when the platform
               -- *decided* to. `dispatch_one` writes `updated_at = now()` with
               -- the terminal status, so for a `DELIVERED`/`UNKNOWN` row this is
               -- the delivery instant; for one still `PENDING`/`SENDING` it is
               -- the last thing we did with it, which is the closest thing to a
               -- contact time that exists yet. Anchoring on `created_at` freed a
               -- slot early by exactly the queue delay -- seconds normally,
               -- minutes when a lease has to expire first.
               --
               -- **The residual imprecision is named**: `updated_at` also moves
               -- on a re-claim, which can hold a slot slightly *longer*. That is
               -- the conservative direction for a cap whose purpose is to not
               -- over-contact somebody.
               AND updated_at > now() - interval '{window}'
            """,  # noqa: S608 - `window` is an int rendered from the pinned profile release
            (
                prepared.tenant_id,
                prepared.tenant_id,
                prepared.conversation_id,
                prepared.tenant_id,
                prepared.conversation_id,
                list(REENGAGEMENT_OCCUPYING_STATUSES),
            ),
        ).fetchone()
        delivered = int(counted["delivered"])
        room = max(0, knobs.reengagement_max_delivered - delivered)
        context = {
            "proactivity_classification": RE_ENGAGEMENT,
            "reengagement_window_seconds": knobs.reengagement_window_seconds,
            "reengagement_max_delivered": knobs.reengagement_max_delivered,
            "reengagement_already_counted": delivered,
            # **Which of the two count scopes this decision used** (independent
            # review 2026-09-09, P3-3). The fallback to a single conversation
            # when `contact_id` is NULL was silent, so a
            # `PROACTIVITY_REENGAGEMENT_LIMIT` row read a year from now could not
            # say whether it had counted one customer or one conversation.
            "reengagement_scope": "contact" if contact_id is not None else "conversation",
        }
        if room >= len(accepted):
            return accepted, [], context
        over = [
            replace(
                decision,
                accepted=False,
                reason_code="PROACTIVITY_REENGAGEMENT_LIMIT",
                detail=(
                    f"{delivered} re-engagement(s) already occupy the "
                    f"{knobs.reengagement_window_seconds}s window, cap is "
                    f"{knobs.reengagement_max_delivered}"
                ),
            )
            for decision in accepted[room:]
        ]
        return accepted[:room], over, context

    def _rate_gate(
        self,
        connection: Any,
        prepared: PreparedEvent,
        accepted: list[ProposalDecision],
    ) -> tuple[list[ProposalDecision], list[ProposalDecision], str | None]:
        """Trim this turn to what the rate ruling allows, and say which limit bit.

        **Counted from the outbox, not from a counter** (ADR-0095 §五 asks for a
        rate, not for new state): the rows this conversation and this tenant
        already produced inside the window are the rate. A counter would be a
        second source of truth that drifts from the thing it counts, and it
        would have to survive restarts to mean anything.

        **Customer messages only.** The manager notice and the digest are the
        platform talking to the merchant about their own backlog; rate-limiting
        those would silence the report of the problem along with the problem.

        Returns `(kept, withheld, scope)`. `scope` is the limit that bit first,
        and it is what the metric is labelled by (`rate_limited_total{scope}`,
        ADR-0101 §三.2) -- "we are over" is not actionable, "this conversation
        is over while the tenant is fine" is.

        **The two limits are not equally exact, and the difference is
        structural** (independent review 2026-09-08, P3-2):

        * **the conversation limit is exact.** Two turns on one conversation are
          serialised by the `SELECT … FOR UPDATE` on `conversation_aggregates`
          in `_apply_aggregate_transition`, so the second one reads the first
          one's rows;
        * **the tenant limit is a snapshot read and can be overshot.** Nothing
          serialises turns across conversations. N conversations committing
          inside the same window each see the same count and each believe they
          have room, so the tenant total can reach the limit plus up to N-1
          turns' worth. **Not fixed here on purpose**: a tenant-wide lock would
          serialise every turn in the tenant, which costs far more than the
          overshoot, and ADR-0095 §五 has not ruled on it. Registered as
          **T215**, whose due condition is the first real merchant actually
          tripping the tenant limit -- until then the overshoot has no measured
          size.
        """

        window = f"{RATE_LIMIT_WINDOW_SECONDS} seconds"
        counts = connection.execute(
            f"""
            SELECT
              -- **会话这一边数的是轮，不是条**（T368，2026-09-19）。
              -- 一轮的每一行共享 `idempotency_key` 中间那一段（源事件 id）——
              -- 见 `TURN_KEY_SQL` 那段注释，包括为什么不用 `created_at`。
              count(DISTINCT {TURN_KEY_SQL}) FILTER (WHERE conversation_id = %s)
                AS turns_in_conversation,
              -- **租户这一边 2026-09-23 起也数轮**（ADR-0151，用户裁定
              -- 「租户也要一起改」）。在此之前它数条数，而代码注释自己写着
              -- 它是「今天唯一还会把一轮劈开的地方」——
              -- 分段之后那条不能留：**半条分段消息比半对独立消息糟得多**
              -- （「因为…」发出去了，「所以…」被砍掉）。
              count(DISTINCT {TURN_KEY_SQL}) AS in_tenant
              FROM outbox_messages
             WHERE tenant_id = %s
               -- **`coalesce`, and it is the same rule `is_customer_row` reads**
               -- (independent review 2026-09-08, P2-2). A row with no
               -- `audience` key at all is a customer row: the key is younger
               -- than the rows (`35f4770`), which is why `is_customer_row`
               -- defaults to `customer` and why `app.py`'s read path coalesces
               -- too. Written as `command->>'audience' = 'customer'` this
               -- comparison is NULL for those rows, so the gate **skipped**
               -- them -- measured at six rows in the window, three counted, one
               -- more message let out over a limit of three. **The strict half
               -- was on the enforcing side**, which is the direction that lets
               -- messages through.
               AND {AUDIENCE_IS_CUSTOMER_SQL}
               -- **A row nobody was asked to send is not a rate** (B4-4a
               -- re-review P2-1). Without this the proactivity check's own
               -- cancellations spend the conversation's budget, and the message
               -- that gets withheld instead is a real reply.
               AND {PROVIDER_WAS_ASKED_SQL}
               AND created_at > now() - interval '{window}'
            """,  # noqa: S608 - both interpolations are module constants, never caller input
            (prepared.conversation_id, prepared.tenant_id),
        ).fetchone()
        turns_in_conversation = int(counts["turns_in_conversation"])
        in_tenant = int(counts["in_tenant"])

        # **会话这一边：单位是轮，而一轮是原子的**（T368，用户 2026-09-19
        # 「速率闸是用来防止骚扰而不是正常对话」）。
        #
        # 没有「还剩几条」这回事 —— 这一轮要么整个进得去，要么整个不进。
        # 按条数算时「还剩一条」会把许诺与兑现劈开，而那两条是一次回复的两个气泡。
        if turns_in_conversation >= RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE:
            return [], list(accepted), "conversation"

        # **租户这一边仍然数条数**，而它是今天唯一还会把一轮劈开的地方
        # （见常数那一段：那条丢弃需要一条可达路径）。
        # **租户这一边也按轮，也原子**（ADR-0151）。
        #
        # 在此之前这里是 `accepted[:tenant_room]` —— 一轮被从中间切开，
        # 前几条发出去、后几条丢掉。那对两条独立消息只是难看，
        # 而对一条分了段的消息是**半句话**。
        #
        # ⚠️ **代价写明**：租户闸从此更容易被一轮撑过线 ——
        # 一轮算一个单位，而它可能带 8 行。这是被接受的代价，
        # 依据是用户 2026-09-19 那条「**速率闸是用来防止骚扰而不是正常对话**」。
        if in_tenant >= RATE_LIMIT_TENANT_PER_MINUTE:
            return [], list(accepted), "tenant"
        return accepted, [], None

    def _record_rate_limit(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan,
        withheld: list[ProposalDecision],
        scope: str,
        #: 劈开之后被尺子丢掉的那几句许诺（T368）。**只记动词短语，不记正文** ——
        #: 与 `_record_plan_generation_disagreement` 里那一族逐字同一条理由。
        #: 空列表是常态（会话那一边是原子的，劈开只发生在租户那一边）——
        #: **而它没有默认值**：一个「忘了传就当没有」的默认，
        #: 与「这一轮真的没丢过许诺」在审计上长得一样。
        promises_dropped: list[dict[str, Any]],
    ) -> None:
        """One denial row per withheld reply, one audit row, one manual task.

        **Not a discard and not a silent queue** -- ADR-0095 §五 rules out both,
        for the reason ADR-0084 §三 gives: a dropped message is one nobody can
        look up afterwards. What is withheld is therefore recorded in three
        places, and they answer three different questions:

        * `policy_decisions` -- **which proposals were refused, and what they
          said.** One row each, `effect='deny'`, the same shape
          `_record_proposal_denial` writes for every other refusal. Without it,
          rate limiting was the only refusal in this pipeline that left no
          per-proposal row (independent review 2026-09-08, P1-1): the first
          version wrote a count and two limits, and the reply the model had
          already composed was dropped from memory with nothing anywhere. "Turn
          it over to a person" has to mean the person can see what to send.
        * `audit_events` -- **how often this is happening.** It is what
          `rate_limited_total{scope}` counts.
        * `manual_tasks` -- **who is going to deal with these**, carrying the
          withheld ordinals so the task points at the rows above rather than
          making its holder search for them.
        """

        event_id = UUID(str(prepared.envelope["event_id"]))
        correlation_id = UUID(str(prepared.envelope["correlation_id"]))
        for decision in withheld:
            # **The same row every other refusal gets.** Reusing the function
            # rather than writing a fourth INSERT is the point: whatever
            # `proposal_ordinal` / `context` shape denials grow next, rate
            # limiting grows it too.
            #
            # **`plan` is passed, and the first version passing `None` was
            # wrong** (independent re-review 2026-09-08, N3). The reasoning
            # then was "the plan itself was fine -- what ran out was the
            # window"; but `context.strategy_id` does not record whose fault it
            # was, it records **which strategy produced this reply**. ADR-0095
            # §五 names these rows as the calibration input for the two guessed
            # numbers, and "which strategy hits the limit most" is the first
            # question calibration asks.
            self._record_proposal_denial(
                connection,
                prepared,
                plan=plan,
                decision=replace(
                    decision,
                    accepted=False,
                    reason_code=RATE_LIMITED_REASON,
                    detail=f"withheld by the {scope} rate limit",
                ),
                # **The reply itself.** This is the only refusal whose subject
                # still has to happen: the customer asked something and the
                # answer exists, it just may not go out this minute. Anywhere
                # else in this pipeline "refused" means "must not be sent", and
                # those rows carry no payload. The merchant is entitled to see
                # their own conversation's content (`notification.py`'s
                # doctrine, user's ruling 2026-09-04), and this is the
                # platform's own draft rather than anything of ours.
                #
                # **`event_id` because the pointer has to work both ways**
                # (independent re-review 2026-09-08, N2). The manual task
                # carries `withheld_ordinals` + `event_id`; without the same id
                # here, two limit trips on one conversation inside a minute
                # leave four denial rows with ordinals `0,1,0,1` and two tasks
                # each claiming `[0, 1]` -- and the only way to pair them is to
                # guess from `created_at`.
                extra_context={
                    "withheld_content": (decision.proposal or {}).get("typed_payload", {}),
                    "event_id": str(event_id),
                },
            )
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'plan.rate_limited', 'conversation_aggregate',
                      %s, %s, %s, %s)
            """,
            (
                prepared.tenant_id,
                uuid4(),
                prepared.conversation_id,
                correlation_id,
                # **The reason code is the reason, and the scope is evidence**
                # (independent review 2026-09-08, P2-3). The first version put
                # `scope` in this column, so the same event was called
                # `RATE_LIMITED` in `manual_tasks` and `conversation` /`tenant`
                # here; and `stage_observability.py` has a sibling query that
                # filters on `reason_code` **without** an action filter, so a
                # non-reason in this column is noise in a column somebody
                # already reads unqualified. The metric label is unchanged --
                # the collector reads `evidence->>'scope'` now.
                RATE_LIMITED_REASON,
                Jsonb(
                    {
                        "scope": scope,
                        "withheld": len(withheld),
                        # **两个上限的单位不一样，所以键名把单位写出来**
                        # （T368，2026-09-19）。先前两个都叫 `..._limit`，
                        # 而会话这一边从此数**轮**、租户那一边数**条** ——
                        # 两个数并列而单位不同，读审计的人会按同一个单位读它们。
                        "conversation_turn_limit": (
                            RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE
                        ),
                        "tenant_message_limit": RATE_LIMIT_TENANT_PER_MINUTE,
                        "window_seconds": RATE_LIMIT_WINDOW_SECONDS,
                        # **劈开之后那几句许诺去哪了**（T368）。
                        # 没有这一项，「客户收到的那条为什么少了一句」
                        # 在审计上与「模型没写那句」长得一样。
                        "promises_dropped": [
                            {
                                "why": str(item.get("why")),
                                "promised": str(item.get("promised") or ""),
                            }
                            for item in promises_dropped
                        ],
                    }
                ),
            ),
        )

        _file_a_manual_task(
            connection,
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            manual_task_id=uuid4(),
            reason_code=RATE_LIMITED_REASON,
            classification=classify(RATE_LIMITED_REASON),
            source="rate_gate",
            proposal_facts={"scope": scope, "withheld": len(withheld)},
            proposed_by_version="rate-gate-v1",
            strategy_id="rate_gate",
            target_frame_id=UUID(str(prepared.aggregate["target_stack"][-1]["id"])),
            # **One task per event, not per withheld message.** A turn that
            # went four over is one thing that happened, and four tasks
            # naming the same turn is a backlog that reports its own volume.
            idempotency_key=f"manual_task:{RATE_LIMITED_REASON}:{event_id}",
            preconditions={"event_id": str(event_id)},
            policy_evidence=[],
            evidence={
                "scope": scope,
                "withheld": len(withheld),
                # **Which replies, so the holder does not have to find
                # them** (independent review 2026-09-08, P1-1). The
                # ordinals point at this turn's `policy_decisions` rows,
                # which carry the text; the same relationship
                # `_manual_task_for_delivery` has to its `outbox_id`.
                # The count above stays -- it is what the digest
                # sentence is built from.
                "withheld_ordinals": [decision.ordinal for decision in withheld],
                "event_id": str(event_id),
            },
        )

    def _file_a_task_for_a_promise_only_a_person_can_keep(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan | None,
        unkept: Sequence[Mapping[str, Any]],
    ) -> None:
        """AI 说了「报价单这边我来安排」——**那件事本来就该由人做**（T365）。

        ## 它补的是批 F 留下的那个洞

        批 F 按用户裁定建了「许了就要这一轮兑现」那道闸：没兑现就把那一句丢掉。
        对「我说说它具体怎么接」那一族这是对的 —— **模型下一轮能自己兑现**。
        而「安排 / 整理好 / 准备好」这一族 AI **今生今世都兑现不了**，
        丢掉那句话等于**把客户的请求静静吞掉**：
        客户看不到我们答应过，也看不到我们没答应。

        「丢一段是回避，不是答案」（T365 登记时的原话）。

        ## 形状是 ADR-0128 §六 已经裁过的，不是本批新拍的

        > **只落待办 + 推通知，不动 `control_mode`、不挂目标栈**（用户 2026-09-11）
        >
        > 系统做到「提醒商家」为止，人接不接是商家的运营问题。
        >
        > **系统保证通知送达，不保证有人接手。**

        ⚠️ **不挂起是那条裁定的要点**，用户原话：挂起意味着替商家决定这通对话
        交给人了，**而人可能根本不在** —— 那时客户等到的是彻底的沉默。
        所以这里与 `_file_task_for_unsendable_intents` 一样**不做移交**。

        ## 一个回合至多一条待办

        幂等键带 `event_id` —— 同 `_record_rate_limit` 的理由：
        一个回合是一件发生过的事，**同一轮里两句「我来安排」不是两件**。

        ## `promised_text` 是模型的原话，而它只到这里

        ADR-0128 同一条裁定的另一半：**固定模板 + 附模型原话**。
        同事要接着办的不是「平台丢了一句话」，是「AI 答应了客户什么」。
        ⚠️ **那句原话进通知正文，不进审计上下文** ——
        `GenerationRejected` 那边的 `context` 因此是逐字投影
        （见 `inference_validation` 里那段注释）。
        """

        sentences = [
            str(item.get("sentence") or "")
            for item in unkept
            if item.get("needs_a_person")
        ]
        if not any(sentences):
            return

        event_id = UUID(str(prepared.envelope["event_id"]))
        target_frame_id = UUID(str(prepared.aggregate["target_stack"][-1]["id"]))
        phrases = sorted(
            {
                str(item.get("promised") or "")
                for item in unkept
                if item.get("needs_a_person")
            }
        )
        classification = classify(AI_PROMISED_A_HUMAN_ACTION_REASON)
        filed = _file_a_manual_task(
            connection,
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            manual_task_id=uuid4(),
            reason_code=AI_PROMISED_A_HUMAN_ACTION_REASON,
            classification=classification,
            source="generation_promised_a_human_action",
            # **动词短语，不是那句话** —— 与丢弃那一行同一条纪律。
            proposal_facts={"phrases": phrases},
            proposed_by_version="promise-needs-a-person-v1",
            strategy_id=plan.strategy_id if plan else "no_send",
            target_frame_id=target_frame_id,
            idempotency_key=(
                f"manual_task:{AI_PROMISED_A_HUMAN_ACTION_REASON}:{event_id}"
            ),
            preconditions={"event_id": str(event_id)},
            policy_evidence={"policy": "promise-needs-a-person", "version": "v1"},
            evidence={"phrases": phrases, "event_id": str(event_id)},
        )
        # **`None` = 这个回合已经落过一条了**（T276 合并时修好的那一条）。
        # 合并之前这里是：幂等键带着 `event_id`、`ON CONFLICT DO NOTHING` 也在，
        # **而没有任何人读它的答案** —— 于是重放同一个回合会再落一条待办、
        # 再推一条通知，而上面那段 docstring 写着「一个回合至多一条待办」。
        # 2026-09-20 跑出来的，不是读出来的：见 `_file_a_manual_task` 那张表。
        if filed is None:
            return
        if classification.pushes_now:
            self._notify_manager(
                connection,
                prepared=prepared,
                reason_code=AI_PROMISED_A_HUMAN_ACTION_REASON,
                manual_task_id=filed,
                # **模型原话按序，全部** —— 一轮里两句都要给同事看。
                promised_text=" ".join(
                    sentence for sentence in sentences if sentence
                )[:500],
            )

    def _file_task_for_unsendable_intents(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan | None,
        refused: Sequence[ProposalDecision],
        promised: Sequence[ProposalDecision] = (),
        materials: Mapping[str, str] | None = None,
    ) -> None:
        """AI 应下一件它自己做不到的事时，落一条待办——而不是什么都不发生。

        **这是用户 2026-09-11 裁定的「甲」**（取「至少派一条待办」，
        不取「让 `manual_task` 真正走通」）。

        缺陷的形状：生成步提名得出 `manual_task`，`validate_generation` 放行
        （计划提名过就合法），而 `GENERATION_SUPPORTED_KINDS` 只有
        `send_message`——于是那条提案被拒。而**决策是逐条独立的**：同一轮里的
        `send_message` 照发。客户听见「我把您标记一下」，AI 应了，
        **然后什么都不发生**；被拒那条只留下一行 `policy_decisions`，
        而那一行只有查库的人看得见。

        **一个回合至多一条待办**，幂等键带 `event_id`——同 `_record_rate_limit`
        的理由：一个回合是一件发生过的事，同一轮里两个被拒的提案不是两件。

        **不做移交（no handoff transition）**，这是有意的，与 `_persist_no_send`
        的 `handoff` 分支相反：那一支会 `suspend_target_stack_for_human` 并把
        `control_mode` 翻成 `human`，**而那会让 AI 当场闭嘴**。这一条的触发语境
        恰恰是 AI 正在好好说话、只是应下了一件要人做的事（「我把您标记一下」
        不是「我把您转人工」）。**把它做成移交，等于用一个更糟的塌台
        换掉一个塌台。** `TASK_WITHOUT_HANDOFF_REASON_CODES` 的注释
        （「A task without the transition」）就是这个形状的先例。

        **`promised` 是这一轮真发给客户的那些提案。** 同事要接着办的不是
        「平台拒了一个 kind」，是「AI 答应了客户什么」——所以那句话附在待办
        的通知正文里（用户同一条裁定的另一半：**固定模板 + 附模型原话**）。
        no-send 那条路不传它：那一轮没有任何东西发给客户，也就没有答应过什么。

        ~~**本函数是第四处写这一对 INSERT 的地方**（另三处：`_record_rate_limit`、
        `_persist_no_send`、`_manual_task_for_delivery`，`api/app.py` 还有一处）。
        没有在本批做合并——它要动两条活的投递路径，而本批是演示前的一格。
        **登记为 T276。**~~

        **更正（2026-09-20，T276 关闭）**：那次合并做了 ——
        本函数与另外四处一起调 `_file_a_manual_task`，
        引擎侧只剩那一个定义点。**原文不删**：它记着这段代码曾经是第四份副本，
        而「删掉会让下一个人以为它一直不是」。
        ⚠️ 合并时才量出来的那件事，比「有四份副本」严重：
        那五处已经漂成了**三种幂等语义**，而三处的 docstring 写着同一句
        「一个回合至多一条待办」。
        
        **与 `_persist_no_send` 那支 handoff 分支互斥，靠的是另一个函数今天碰巧
        不产某几个码**（独立审查 2026-09-11，P3-3）。两条路的幂等键不同——
        `manual_task:{wholesale_reason}:{event_id}` 对
        `manual_task:INTENT_KIND_NEEDS_A_PERSON:{event_id}`——所以同一轮理论上
        可以各落一条待办、各推一条通知。

        **今天不可达**：`first.reason_code` 只能来自 `_decide_proposal`，
        而它不产 `HANDOFF_REASON_CODES` 里的任何一个码（那些是成本码与栈结构码）。
        **写在这里是给将来给 `_decide_proposal` 加一个成本码的人看的** ——
        那一刻这条路就打开了，而没有任何一道闸会说话。登记在 T285。
        """

        filed_decisions = [
            decision
            for decision in refused
            if decision.kind in TASK_FILING_KINDS
            and decision.reason_code == UNSUPPORTED_INTENT_REASON
        ]
        if not filed_decisions:
            return

        event_id = UUID(str(prepared.envelope["event_id"]))
        target_frame_id = UUID(str(prepared.aggregate["target_stack"][-1]["id"]))
        kinds = sorted({decision.kind for decision in filed_decisions})
        classification = classify(INTENT_NEEDS_A_PERSON_REASON)
        filed = _file_a_manual_task(
            connection,
            tenant_id=prepared.tenant_id,
            conversation_id=prepared.conversation_id,
            manual_task_id=uuid4(),
            reason_code=INTENT_NEEDS_A_PERSON_REASON,
            classification=classification,
            source="generation_intent_unsendable",
            proposal_facts={"kinds": kinds},
            proposed_by_version="intent-needs-a-person-v1",
            strategy_id=plan.strategy_id if plan else "no_send",
            target_frame_id=target_frame_id,
            idempotency_key=f"manual_task:{INTENT_NEEDS_A_PERSON_REASON}:{event_id}",
            preconditions={"event_id": str(event_id)},
            policy_evidence=[],
            evidence={
                "kinds": kinds,
                # **序号指向本轮的 `policy_decisions` 行**，那些行里有
                # 被拒提案的全文；拿到待办的人不必自己去找
                # （同 `_record_rate_limit` 的 `withheld_ordinals`）。
                "refused_ordinals": [decision.ordinal for decision in filed_decisions],
                "event_id": str(event_id),
            },
        )
        if filed is None:
            return

        if classification.pushes_now:
            self._notify_manager(
                connection,
                prepared=prepared,
                reason_code=INTENT_NEEDS_A_PERSON_REASON,
                manual_task_id=filed,
                promised_text=self._promised_words(promised, materials=materials),
            )

    @staticmethod
    def _promised_words(
        promised: Sequence[ProposalDecision], *, materials: Mapping[str, str] | None
    ) -> str | None:
        """这一轮真的发给客户的话，**全部按序**，没有就 `None`。

        **读的是 `typed_payload.content`，不是 `customer_visible_segments`**——
        草稿不是发出去的东西（`_validate_payload_agrees_about_materials` 量过：
        草稿引三条素材而 payload 一条不引，六个臂里六个都是）。
        `customer_text` 是发送路径自己用的那个渲染器，所以同事看到的
        与客户看到的是同一串字。

        **先前它返回第一条读得出文字的，那是错的**（独立审查 2026-09-11，P2-2）。
        一个回合可以携带多条提案——T44 明写、`decisions` 逐条独立、`kept` 是个
        列表——而「先问好、再答应标记一下」是很自然的两条。取第一条时同事读到的
        是**问好那句**，**而且没有任何迹象表明还有别的话**：待办正文读起来就像
        AI 只说了这一句。

        **而「附模型原话」这条裁定的全部用处就在那句承诺上。** 拿错一句不是少给
        信息，是给了一个会让人得出相反结论的信息——同事看到「您好，很高兴为您
        服务」会以为这条待办是误报。

        **先脱敏后截断的顺序不在这里**：本函数只按序拼，脱敏与上限都在
        `notification._quoted` 里，两件事都不碰。
        """

        spoken: list[str] = []
        for decision in promised:
            payload = (decision.proposal or {}).get("typed_payload")
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, dict):
                continue
            text = customer_text(content, materials=materials)
            if text:
                spoken.append(text)
        # 空格连，不是换行：这一串最终进的是 `「…」` 里面，
        # 而通知正文本身是按行读的（`manual_task_notice` 逐行拼）。
        return " ".join(spoken) if spoken else None

    def _queue_outbox_row(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        decision: ProposalDecision,
        delivery: OutboxDelivery,
        materials: Mapping[str, str] | None,
        proactivity: ProactivityVerdict | None,
    ) -> None:
        """排一行出站消息。**只有效果是 `OutboxDelivery` 的 kind 走到这里。**

        E4-2 之前这段代码直接长在 `_persist_accepted_proposal` 里、无条件执行——
        于是「接受一个提案」与「排一行出站消息」是同一件事，**没有第二种可能**。
        `remember` 需要的正是第二种：它写一行记忆，**一个字都不发**。
        """

        assert decision.proposal is not None  # noqa: S101 - accepted implies built
        # `outbox_id` 现在是 `UUID | None`，而走到这里的 kind 必然领过号——
        # `_decide_proposal` 按同一个 `effect` 分支决定领不领。
        assert decision.outbox_id is not None  # noqa: S101 - OutboxDelivery implies an id
        command = {
            # L2a. Every outbox command now names who it is for. Customer
            # messages were the only kind when this was written, so "for the
            # customer" was implicit -- and an implicit audience is what lets a
            # manager notification be delivered to a customer by a dispatcher
            # that never had to ask. The dispatcher reads this field; see
            # `_claim_one`.
            "audience": delivery.audience,
            "tenant_id": str(prepared.tenant_id),
            "conversation_id": str(prepared.conversation_id),
            # **The canonical shape, not the model's** (2026-09-10). What the
            # model returned is kept whole on `intent_records.payload` above;
            # what is queued is what a channel can act on, so every row in
            # `outbox_messages` carries one shape rather than whichever of the
            # three the model happened to pick. The manager-notification path
            # below already builds this shape, and its comment already claims it
            # is "the same shape the customer row carries" -- **that sentence
            # was false until this line existed.**
            #
            # `customer_text` cannot return `None` here: `_decide_proposal`
            # refuses the proposal when it does. That is why the refusal is a
            # guard there and not an assertion here.
            #
            # **`materials` must be the same mapping `_decide_proposal` used**,
            # and it is -- same function, same profile, same `plan.definition`.
            # Passing it here is what makes the queued row carry the merchant's
            # text rather than `material:...:v1`: the row is what the adapter
            # sends, and the adapter resolves nothing (`customer_text` with no
            # materials refuses a ref outright, which is the second lock).
            # **形状由注册表条目给**（E4-2）。原先这里直接读
            # `typed_payload["content"]`——那是 `send_message` 的形状，
            # 却写在一个所有 kind 都要流经的地方。上面第 3 处说的就是它。
            "content": delivery.build_content(
                decision.proposal["typed_payload"], materials
            ),
            "deadline_at": prepared.envelope["runtime_deadline_at"],
            "correlation_id": prepared.envelope["correlation_id"],
        }
        if proactivity is not None:
            # **The classification, and B4-4b is the batch that reads it**
            # (`_reengagement_gate`). It carries both values and both matter:
            # one is counted against the cap, the other is exempt from it
            # (「会话内延续……不计入每日主动配额」).
            command[PROACTIVITY_COMMAND_KEY] = {"classification": proactivity.classification}
        if proactivity is not None and proactivity.window_expires_at is not None:
            # **Q18's field, and this batch is the one that writes it**
            # (ADR-0098 §一; the work order says so in as many words -- B1-3 was
            # supposed to and did not, because before B4-2 nothing could produce
            # the value and it would have been a field that is always `null`,
            # the T118 / T193 shape).
            #
            # **Written only when there is an instant to write** -- the same
            # rule, applied to the same field the rule was written about.
            #
            # **Before B4-5 that meant a re-engagement carried no key at all**,
            # because nothing but the active window could expire a row. B4-5
            # gave it a second source that applies to both classes, so **every
            # proactive row now carries this key** (independent review
            # 2026-09-09, P3-1: this comment used to say the opposite, and B4-5
            # is the batch that made it false).
            command[PROACTIVITY_COMMAND_KEY]["window_expires_at"] = _iso(
                proactivity.window_expires_at
            )
        if proactivity is not None and proactivity.window_lifts_at is not None:
            # **The answer to 「几点可以再发」, carried so the dispatcher never has
            # to ask the profile** (independent review 2026-09-09, P1-1).
            #
            # Since B4-5 a quiet window opening can expire a queued row, and that
            # kind of expiry **does** have an end -- unlike an active window
            # closing, past which the message is a different class rather than a
            # delayed one. Without this the cancellation lands with
            # `next_allowed_at = NULL`, which migration `049` and ADR-0119 §二.1
            # both define as 「这条拒绝没有自动失效的时刻」: **the batch that wrote
            # that definition would have produced the first row contradicting it.**
            command[PROACTIVITY_COMMAND_KEY]["window_lifts_at"] = _iso(
                proactivity.window_lifts_at
            )
        # **一次判定，多行出站**（ADR-0152）。判定已经做完了 ——
        # 这里只把它批准的那条消息按**模型给的切点**铺成几行。
        #
        # ⚠️ **拆在这里，不在策略链之前**：用户 2026-09-23
        # 「分段前就判断好能不能发，**拆分后语义语境可能完全不同**」。
        # 每一行共享同一个 `intent_id`（同一次判定），
        # 靠 `ordinal` 区分 —— 迁移 `076` 把唯一约束放松成三列正为此。
        for index, (segment_command, key, pace) in enumerate(
            _segments_to_queue(
                command,
                proposal=decision.proposal,
                materials=materials,
                basis_key=decision.idempotency_key,
            )
        ):
            connection.execute(
                """
                INSERT INTO outbox_messages(
                    tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                    ordinal, pace_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    prepared.tenant_id,
                    decision.outbox_id if index == 0 else uuid4(),
                    prepared.conversation_id,
                    decision.intent_id,
                    Jsonb(segment_command),
                    key,
                    # **T109.** One turn may raise several proposals, and all of
                    # their rows carry the same `created_at` -- `now()` is the
                    # transaction timestamp. This is the position the model put
                    # this message in, and after migration 044 it is what the
                    # claim orders by. Without it the tiebreak was `uuid4()`:
                    # measured 8 runs, 5 of them delivered in the opposite order.
                    #
                    # **分段之后它还多担一件事**：同一个 `intent_id` 的几行
                    # 靠它区分（`076` 的唯一约束）。所以这里是
                    # 「这一提案的位置 + 这一段的位置」——
                    # 一个回合里两条提案各分两段时，四行的序号必须两两不同。
                    decision.ordinal * SEGMENT_ORDINAL_STRIDE + index,
                    # **ADR-0151.** 这一段发完要停多久 —— 给下一段留的。
                    # 非分段行是 0，取件谓词对它恒真。
                    pace,
                ),
            )

    def _persist_accepted_proposal(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan,
        decision: ProposalDecision,
        materials: Mapping[str, str] | None = None,
        proactivity: ProactivityVerdict | None = None,
    ) -> None:
        """Write one accepted proposal: intent, decision, approval, outbox row."""

        assert decision.intent_contract is not None  # noqa: S101 - accepted implies built
        assert decision.proposal is not None  # noqa: S101
        # **这两条是换成唯一写入点时 mypy 指出来的。** `ProposalDecision` 上这两个字段
        # 是 `| None`（被拒的提案两个都没有，`None` 在那些构造点是真话），而这条路径是
        # 「已接受」那一支：旧的裸 SQL 会把 `None` 写成 `Jsonb(None)` → 一个 `'null'::jsonb`
        # 悄悄落在一列默认 `'{}'` 的 `context` 上，`policy_id` 那边则写成字符串 `"None"`。
        # **两者都过得了 NOT NULL，也过得了任何门禁** —— 直到有人去查那一行为什么是空的。
        assert decision.policy_id is not None  # noqa: S101 - accepted implies decided
        assert decision.policy_context is not None  # noqa: S101
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions, policy_evidence
            ) VALUES (%s, %s, %s, %s, 'PROPOSED', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                prepared.tenant_id,
                decision.intent_id,
                prepared.conversation_id,
                # **不再是字面量**（E3-3）。写死 `'send_message'` 时，一个
                # `send_material` 提案会被**记成** `send_message` 并照样通过合同
                # 校验——注释里那四条的第 2 条。现在它与合同里的 `kind` 同源。
                decision.intent_contract["kind"],
                Jsonb(decision.proposal["typed_payload"]),
                Jsonb(decision.intent_contract["proposed_by"]),
                plan.strategy_id,
                UUID(str(decision.intent_contract["target_frame_id"])),
                decision.idempotency_key,
                Jsonb(decision.intent_contract["preconditions"]),
                Jsonb([str(decision.policy_id)]),
            ),
        )
        record_policy_decision(
            connection,
            tenant_id=prepared.tenant_id,
            row_id=decision.policy_id,
            conversation_id=prepared.conversation_id,
            intent_id=decision.intent_id,
            output=PolicyOutput(
                hook="pre_execute",
                effect="allow",
                reason_code=decision.reason_code,
                policy_id="m0-minimum-policy",
                policy_version="1.0.0",
                obligations=audit_and_redact(),
                context=decision.policy_context,
            ),
        )
        connection.execute(
            """
            UPDATE intent_records SET status = 'APPROVED', updated_at = now()
             WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'
            """,
            (prepared.tenant_id, decision.intent_id),
        )
        # **效果按 kind 分派**（E4-2，本文件上方注释里的第 3 处）。
        # 上面三条写入（intent / decision / approval）对每个 kind 都一样——
        # 它们记的是「一个提案被接受了」。**不一样的是接受之后发生什么。**
        effect = GENERATION_WRITE_PATHS[decision.kind].effect
        if isinstance(effect, DirectWrite):
            effect.write(connection, prepared, decision)
        else:
            self._queue_outbox_row(
                connection,
                prepared,
                decision=decision,
                delivery=effect,
                materials=materials,
                proactivity=proactivity,
            )
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'intent.approve', 'intent_record', %s, %s, %s, %s)
            """,
            (
                prepared.tenant_id,
                uuid4(),
                decision.intent_id,
                UUID(str(prepared.envelope["correlation_id"])),
                "POLICY_ALLOWED",
                Jsonb(
                    {
                        "policy_decision_id": str(decision.policy_id),
                        "proposal_ordinal": decision.ordinal,
                    }
                ),
            ),
        )

    def _record_proposal_denial(
        self,
        connection: Any,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan | None,
        decision: ProposalDecision,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        """One row per refused proposal, naming which proposal it refused.

        `intent_id` is NULL: a refused proposal never became an intent, and
        borrowing an id for it would put a row in `policy_decisions` pointing at
        something that does not exist. What identifies it instead is
        `context.proposal_ordinal` -- the fourth thing the T44 ruling asks for.

        `extra_context` is for facts that only one kind of refusal has. It
        exists because of one (independent review 2026-09-08, P1-1): a reply
        withheld by the rate gate is the only refusal where **the thing refused
        still needs doing**, so its row carries the text a person has to send.
        Every other refusal means "this must not be sent", and for those the
        ordinal and the reason are the whole story.

        **Merged first, so the fixed keys below overwrite it** -- a caller
        cannot redefine `proposal_ordinal` and friends. The first version said
        exactly this while spreading `extra_context` **last**, which is the
        opposite semantics (independent re-review 2026-09-08, N1): a dict
        literal lets later entries win. Nothing was actually overwritten,
        because the only caller passes two keys of its own; the sentence was
        wrong before the code was.
        """

        record_policy_decision(
            connection,
            tenant_id=prepared.tenant_id,
            row_id=uuid4(),
            conversation_id=prepared.conversation_id,
            # **`None`, and the old SQL said so too** (a literal `NULL` in the
            # VALUES list): a refused proposal has no `intent_records` row --
            # the refusal happens *instead of* creating one.
            intent_id=None,
            output=PolicyOutput(
                hook="pre_execute",
                effect="deny",
                reason_code=decision.reason_code,
                policy_id="m0-minimum-policy",
                policy_version="1.0.0",
                obligations=audit_only(),
                next_allowed_at=decision.next_allowed_at,
                context={
                    # **First, so the fixed keys below win** (independent
                    # re-review 2026-09-08, N1). Python's dict literal lets
                    # later entries overwrite earlier ones, so the first
                    # version -- which spread this **last** -- let a caller
                    # silently redefine `proposal_ordinal` and everything
                    # else here, while the docstring claimed the opposite.
                    **(extra_context or {}),
                    "tenant_id": str(prepared.tenant_id),
                    "aggregate_generation": prepared.aggregate["generation"],
                    "control_mode": prepared.aggregate["control_mode"],
                    "proposal_ordinal": decision.ordinal,
                    "proposal_kind": decision.kind,
                    "strategy_id": plan.strategy_id if plan else "none",
                    "detail": decision.detail,
                },
            ),
        )

    def _retrieve_if_requested(
        self, *, plan: ValidatedPlan, context: CallContext, profile: Mapping[str, Any]
    ) -> RetrievalResult | None:
        """裁定三：**计划步说要查资料，才检。**

        **且商家绑了资料库才检**（2026-09-24 适配审查 K1 接线）。
        `knowledge_bindings` 自 KB-5 起就在 schema 里、封版检语法、API 投影回显，
        而运行期**零读者** —— 检索一直按「租户当前 published release」检，
        包声明为 `[]` 的 profile 照样被检索。这里是它的第一个读者，**开关语义**：
        `[]` = 商家说「我没有参考资料」，检索这一步对这包不存在。
        **值仍不解引用**（`knowledge_releases` 没有集合列，按集合过滤 release
        要一条迁移），那半留在 T505。

        读的是 `knowledge_query_as_written` 而**不是** `requested_knowledge_query`：
        后者把不可用的请求读成「这一轮没请求」，而不可用必须留下一条
        `unusable_request` 的审计行（ADR-0133 的第二态）——
        拿它当入口，「模型压根没请求」与「模型写歪了」就在数据里长成一样。

        **返回值现在有人读了**（6c）：`sent_chunks` 是这一轮真的会进提示词的那几条，
        而生成之后还要用同一个对象写回裁定四那三列。
        `None` 有三个意思，而它们在数据上同形（都不留证据行）：
        包没绑资料库、没接检索器（mock 那条路）、或者计划步没请求检索。
        """

        if not (profile.get("knowledge_bindings") or []):
            return None
        if self._retriever is None:
            return None
        query = knowledge_query_as_written(plan.document)
        if query is None:
            return None
        # 检索器自己吃掉供应商错误与上限拒绝（裁定六），所以这里没有 try ——
        # **有 try 才是可疑的**：那意味着有一条它没吃掉的失败路径没被写下来。
        return self._retriever.run(context=context, query=query)

    def _record_generation_side(
        self,
        *,
        context: CallContext,
        retrieval: RetrievalResult,
        materials: Mapping[str, str],
        generation: dict[str, Any],
    ) -> None:
        """裁定四：**共现 + 用了哪边**，三列一起写。

        `quoted_side` 的判据是**客户真正看到的字**：
        一个 `material_ref` 段会被发送路径逐字换成商家审过的话术，
        所以只要有一个，这一轮引的就是 `material` —— 哪怕模型同时引了知识库当依据。
        没有素材段、而 `claims` 里引了这一轮给过的知识 ref，才是 `knowledge`。
        两者都没有就是 `neither`。

        `retrieved_unused` 数的是**这一轮检出来的东西有多少没落地**，
        分母是 `hits` —— **被额度挡下、模型根本没机会看见的那条也算在里面**。

        **所以它与 `hits_sent` 不是互补的两半，在那些回合上是重叠的**
        （独立审查 2026-09-12 **P3-1**：先前这里写的是「后者是平台没送、
        前者是模型没用」，而实现不是那样）。要拆开读：
        **「送了而没被引」= `retrieved_unused − (len(hits) − hits_sent)`**。
        口径本身照裁定四原话（「本轮检出但一条没引的数量」），
        而 `sent < hits` 今天造不出来（额度覆盖契约允许的 top-2），
        所以这个重叠今天是零 —— **`TOP_K` 变大的那天才要用上面那个减法**。

        **`cited_refs` 记的是「引了哪几条」**（`062`）：那个数答不了这件事，
        而三跳追溯的第一跳（回复 → chunk）问的正是它。
        注意它与 `quoted_side` 是两个问题的答案 —— 一个回合可以
        `quoted_side = 'material'` 而引用集非空（逐字引了素材、拿知识库当依据），
        **那是裁定四要留的共现记录，不是矛盾**。
        """

        cited = {
            str(ref)
            for claim in generation["claims"]
            for ref in claim["source_evidence_refs"]
            if str(ref).startswith(KNOWLEDGE_REF_PREFIX)
        }
        quoted_material = any(
            segment["type"] == MATERIAL_REF_SEGMENT_TYPE
            for segment in generation["customer_visible_segments"]
        )
        if quoted_material:
            quoted_side = "material"
        elif cited:
            quoted_side = "knowledge"
        else:
            quoted_side = "neither"
        unused = sum(1 for hit in retrieval.hits if str(hit["ref"]) not in cited)
        # **库侧异常在这里吃掉，不往外抛**（独立审查 2026-09-12 **P1-1**）。
        #
        # 先前这一句是裸的：任何库侧异常都会穿过 `process_prepared`，
        # `process_pointer` 记一次失败尝试后原样再抛，`MAX_INBOX_ATTEMPTS` 之后
        # 这条入站事件终态失败。**而这一刻三次模型调用的钱已经花完、生成也过了校验** ——
        # 丢掉的是一条**已经成文的回复**，理由只是这四列没写进去。
        #
        # **口径不是本批发明的**：6b 在 `KnowledgeRetriever.run` 里为同一个形状
        # 立过（`retrieval.py` 那两处 `except PsycopgError` + 计数器），
        # 而 6c 没把它带过来、6d 还往这条语句上又加了第四列 ——
        # **写得越多，这条路的到达面越大**。
        #
        # 写不下这几列是运维要知道的事（那个计数器），
        # **而让客户这一轮收不到消息不是它的代价**（裁定六同一条理由的推论）。
        try:
            with self._database.transaction(tenant_id=context.tenant_id) as connection:
                record_generation_side(
                    connection,
                    tenant_id=context.tenant_id,
                    correlation_id=context.correlation_id,
                    materials_available=bool(materials),
                    quoted_side=quoted_side,
                    retrieved_unused=unused,
                    cited_refs=sorted(cited),
                )
        except PsycopgError:
            RETRIEVAL_VERDICT_WRITE_FAILED_TOTAL.increment(quoted_side)

    def _record_plan_refusal(
        self,
        # `Any`，照本文件里其余十几处连接形参的写法（`_remember_write` 等）——
        # 连接类型在这一层没有被收窄过，本方法不另立一种。
        connection: Any,
        *,
        prepared: PreparedEvent,
        row_id: UUID,
        intent_id: UUID | None,
        reason_code: str,
        obligations: Sequence[str],
        next_allowed_at: datetime | None,
        context: Mapping[str, Any],
    ) -> None:
        """记一行 `post_plan` 的 deny —— **两条路共用的那一处**（T339 第二批，用户裁 (甲)）。

        **为什么抽出来**：在 (c) 之前，「记下这次拒绝」与「这一轮不发消息」
        同住在 `_persist_no_send` 里。(c) 之后这两件事分开了 ——
        点得出槽位名的第一次拒绝**要留痕、而且要发问** ——
        于是留痕这一半必须能被另一条路调用。

        **为什么不是让说话那条路自己开一个事务写它**（那是被否掉的 (乙)）：
        `policy_decisions` 的 `INSERT` 是**全仓唯一写入点 + 绊线**（裁定二），
        自己开事务要么绕过那个写入点（绊线当场红），
        要么给它一个它不接受的用法。而抽出来之后两条路**仍然走同一个写入点**，
        只是被调用两次 —— 这也正是本仓那条「同一个事实不要两处各写一遍」。

        **两条路各自都在自己那一个事务里**：
        `_persist_no_send` 传它自己开的连接；说话那条路传**投递那一个事务**的连接
        （聚合迁移 / 帧审计 / 出站行都在里面）。
        所以「拒绝留了痕而消息没发出去」这种半截状态**两条路都不会有**。

        > ⚠️ **上面这句话今天没有守卫，只有这段注释**（T342）。
        >
        > 第二批那支反向探针 **B3**（删掉说话那条路的留痕调用）**红了 3 条**，
        > 而它红的是「**deny 行不见了**」——
        > **不是**「deny 行与投递不在同一个事务里」。
        > 两者的区别要造一次**这个事务中途失败**才看得出来，而今天没有那样一条用例。
        > **所以读到「B3 红了 3 条」的人要知道：那 3 条没有覆盖事务边界。**
        >
        > **也不要拿「渠道发送失败」去造它**：出站行走 outbox 模式，
        > 投递是另一个回合的事 —— 渠道失败根本不在这个事务里。
        > 要造的是库这一侧的中途失败（T342 写了形状）。
        >
        > **而「两条路的事务语义并不相同」，见 T343** ——
        > 说话那条路的边界是第二批**新建**的（在投递那个事务内），
        > 不说话那条路照旧（`_persist_no_send` 自己开事务，12 个调用点全在事务外）。
        > **「事务边界没变」这个说法只对后者成立。**

        四个常量（hook / effect / policy_id / policy_version）**只在这里出现一次** ——
        它们曾经在 `_persist_no_send` 里写死，而那时只有一个调用方。
        """

        record_policy_decision(
            connection,
            tenant_id=prepared.tenant_id,
            row_id=row_id,
            conversation_id=prepared.conversation_id,
            intent_id=intent_id,
            output=PolicyOutput(
                hook="post_plan",
                effect="deny",
                reason_code=reason_code,
                policy_id="plan-validator",
                policy_version="1.0.0",
                # **本处的义务是算出来的**（不同拒绝带不同义务），所以过一遍词表
                # 而不是写字面量 —— 写入点校验的价值全在这种「值不是常量」的地方。
                obligations=coerce_obligations(obligations),
                next_allowed_at=next_allowed_at,
                context=context,
            ),
        )

    def _previous_denial_has_the_same_code(
        self, prepared: PreparedEvent, reason_code: str
    ) -> bool:
        """该会话**最近一行** `deny` 是不是同一个理由码。

        **与 `_persist_no_send` 里升级那一段用的是同一个判据**（同一句 SQL 形状）——
        两处必须同源，否则会出现「这一轮被判成第一次、而升级那边判成第二次」
        这种谁都说不清的状态。**这里读的是「问过了没有」，那里读的是「该不该升级」，
        而在 (c) 这个裁定下它们是同一个事实的两面。**

        **为什么不缓存**：这一轮之内两处各查一次，值必然相同（同一个事务之外的只读查询，
        而这一轮的 deny 行还没写）。省一次查询要传一个布尔值穿过三层调用，
        而那正是「同一个事实两处各写一遍」。
        """

        with self._database.transaction(tenant_id=prepared.tenant_id) as probe:
            previous = probe.execute(
                """
                SELECT reason_code FROM policy_decisions
                 WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'
                 ORDER BY created_at DESC LIMIT 1
                """,
                (prepared.tenant_id, prepared.conversation_id),
            ).fetchone()
        return previous is not None and str(previous["reason_code"]) == reason_code

    def _generate_and_validate(
        self,
        *,
        prepared: PreparedEvent,
        plan: ValidatedPlan,
        understanding: dict[str, Any],
        materials: Mapping[str, str],
        knowledge: Mapping[str, str],
        assets: Sequence[Mapping[str, str]],
        context: CallContext,
        #: 这一轮递给写字那一步的记忆，形状 `{ref, value}`（T363，2026-09-19）。
        #:
        #: ⚠️ **记忆这一族只有这一个参数**，另两处都从它现推：
        #: 校验器那一侧的 `remembered`（见 docstring 最后一节），
        #: 以及「装不下几条」那个计数（从 `prepared` 现读 —— 它是回合级的事实，
        #: 两次尝试同一个值）。**参数越少，两个调用点能漂开的地方就越少。**
        remembered_facts: Sequence[Mapping[str, str]],
        dropped: list[dict[str, Any]],
        echoed: list[dict[str, Any]],
        unsourced: list[dict[str, Any]],
        unkept: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """一次生成 + 校验。**两处调用：首次尝试，以及 T367 的那一次重跑。**

        **抽出来是因为它被调两遍**，而这一段的参数里有四个「传了就接下记录的
        责任」的收集器（`dropped` / `echoed` / `unsourced` / `unkept`）——
        两处各写一遍，漂掉的方式是**其中一处少传一个**：
        那时那一族缺陷在那一次尝试上静默放行，而门禁全绿。

        ⚠️ **上一版这句抬头写的是「五个」，而签名里是四个**
        （复核 2026-09-19 数出来的）。**带计数的抬头容易与正文脱节** ——
        这一版把四个的名字逐个写出来，于是下一次增删时它自己对不上。

        所有异常由调用方处置：首次尝试的失败结账成 `no_send`，
        **而重跑的失败只是放弃重跑** —— 两种处置不同，所以不在这里吞。

        ## ⚠️ 记忆那一对为什么是「两个参数进、第三个现推」（T363 + T367）

        写字那一步收 `remembered_facts`，校验器收 `remembered`
        （许它引哪几条，fail-closed）。**它们必须是同一份。**

        把 `remembered` 也做成一个参数，就正好落进上面那句告警的形状：
        **两个调用点各写一遍，而重跑那一处少传一个** ——
        那时 `remembered` 是空集，而空集的语义是
        「这一轮没给过记忆，任何 `memory:` 引用都是编的」，
        于是**重跑会系统性地、静默地拒掉每一条引记忆的回复**。
        门禁全绿，而客户那一侧只是偶尔收不到回复。

        **所以它在这里现推**：调用点只能递一份记忆，
        「递了什么」与「许引什么」在结构上不可能不一致。
        `test_the_prompt_and_the_validator_read_one_list` 钉住这一条。
        """

        return validate_generation(
            generation=self._inference.generate(
                plan=plan.document,
                understanding=understanding,
                envelope=prepared.envelope,
                materials=materials,
                knowledge=knowledge,
                assets=assets,
                context=context,
                # **跟进那一句要接住上次断在哪**（2026-09-18，运维 §11）。
                # 理解步与计划步早就收着它，而**写字这一步在此之前没有**。
                last_outbound=prepared.last_outbound,
                conversation_summary=prepared.conversation_summary,
                summary_lines_not_shown=prepared.summary_lines_not_shown,
                # **「之前的内容」才是跟进的锚点**（用户 2026-09-18 更正）：
                # 对话可能结束在「好的我看看」这种与目标无关的一句上。
                materials_already_sent=prepared.delivered_materials,
                # **记住的那几件事**（T363，2026-09-19）——
                # 写字这一步在此之前看不见它们。
                remembered_facts=remembered_facts,
                # **能记哪几件事，由配包声明** —— 模型看不见这张表就只能猜键名。
                # 运维 2026-09-20 实测：它猜了 `concern_topic`，
                # 形状那道闸拒一次，字段没声明那道闸还会再拒一次，
                # **而客户完全看不见** —— 同一轮的回复照常发出。
                rememberable_fields=prepared.profile.get("memory_fields") or [],
                memories_not_shown=prepared.memories_not_shown,
                # **挂着没答的那个问题**（运维缺陷 19）：计划步早就收着它，
                # 而把问题写进正文的是生成步 —— stage 的现场正是同一条消息的
                # 第 ③ 段把第 ① 段刚用过答案的问题原样又问了一遍。
                pending_question=prepared.aggregate.get("pending_question"),
            ),
            plan=plan,
            materials=materials,
            knowledge=knowledge,
            # **许它引哪几条记忆 —— 从上面那一份现推，不是另一个参数**
            # （理由见 docstring 最后一节）。空集 = 这一轮没给过记忆，
            # 那时任何 `memory:` 引用都是编的。
            remembered={str(entry["ref"]) for entry in remembered_facts},
            # **本轮递出去的那几份** —— 校验器据此拒掉「没递出的 ref」。
            # 与 `materials` 同一个 fail-closed 形状：没送过就不许引。
            assets={asset["ref"] for asset in assets},
            # **计划没提名的那条丢掉，别整轮拒**（用户 2026-09-17 裁定）。
            #
            # 「客户说了话就要有回复。计划与生成谈不拢是我们内部的事，
            # 不该以『对客户沉默』结账。」运维实测这一族九次，
            # **今天最大的单一沉默来源**。
            #
            # 传了这个列表就等于接下了记录的责任 —— 下面那几行写它。
            dropped=dropped,
            # **引了素材就不许再复述一遍**（2026-09-17 实测：
            # 同一条消息里模型先用自己的话说了一遍 `demo-trial-how`，
            # 系统又把原文贴上，逐字重合 20 字）。
            # 传了这个列表就等于接下了记录的责任。
            echoed=echoed,
            # **5.7：说不出来源的那一段丢掉**（运维 2026-09-17 两处偏差）。
            #
            # 一是「您付的那 1888」—— 把标价改写成这位客户已经在付的钱，
            # 而客户第一次说出这个数在五分钟之后；
            # 二是同一句在两轮里逐字重复，中间客户问的是不同的问题。
            #
            # **复核 2026-09-18 §一量过：`no-echo` 那支盖不住它**
            # （那支守 `copy`，最长重合 12；这两句重合只有 7）。
            customer_numbers=prepared.customer_numbers,
            last_outbound=prepared.last_outbound,
            unsourced=unsourced,
            # **许了就要这一轮兑现**（运维 §10.4 判据 2，用户 2026-09-18 裁定）。
            #
            # 实测那一句：「明白，您自己接。那我先把试用怎么开、
            # 您那边要准备什么讲清楚。」——**它把内容推到了下一轮，
            # 而客户可以不给你下一轮**（「话题转开，前面的承诺就没有机会讲了」）。
            #
            # ⚠️ `no_new_commitment` 盖不住它：那条约束七个打法全声明了，
            # 而它的两个信号都是**模型自述的**（`commitment_id`、
            # `safety_markers` 里的 `contains_commitment`）。模型不举手就没人看见。
            unkept=unkept,
        )


    def _retry_for_an_unkept_promise(
        self,
        *,
        prepared: PreparedEvent,
        understanding: dict[str, Any],
        promised_sentences: list[str],
        materials: Mapping[str, str],
        knowledge: Mapping[str, str],
        #: **计划步那一份与生成步那一份是两个清单**（E3-4）——
        #: 第一次尝试就是这么分的，重跑照抄那个分法。
        #: 传成同一个，`test_the_pipeline_hands_the_plan_the_list_it_just_selected`
        #: 当场红并报出「喂的是哪个、选的是哪个」。
        planning_assets: Sequence[Mapping[str, str]],
        assets: Sequence[Mapping[str, str]],
        #: **重跑那一次也递记忆**（T363 + T367，2026-09-19，复核点名要决定的那件）。
        #:
        #: ⚠️ **不递的后果不是「模型少看一点上下文」**：
        #: `_generate_and_validate` 从这一份现推校验器那侧的 `remembered`，
        #: 而空集的语义是「这一轮没给过记忆，任何 `memory:` 引用都是编的」——
        #: 于是**重跑会系统性地、静默地拒掉每一条引记忆的回复**，
        #: 而结局是「放弃重跑、留第一次的结果」，门禁全绿。
        #: **传得进来才对**，而这个形参就是那个决定。
        remembered_facts: Sequence[Mapping[str, str]],
        context: CallContext,
        required_strategy: str | None,
    ) -> tuple[dict[str, Any], ValidatedPlan, tuple[list[dict[str, Any]], ...]] | None:
        """重跑一次计划 + 生成，把「你许了什么而没兑现」喂回去（T367）。

        回 `None` = **放弃重跑，留第一次的结果**。三种情况都回 `None`：
        供应商/成本顶抛了、重跑那份计划被拒、重跑那次生成被拒 ——
        **一次失败的重试不该把一条发得出去的回复换成沉默。**

        ## 栈操作钉成 `keep`（用户 2026-09-19 裁定「乙」）

        迁移在第一次尝试里已经落下。重跑那份计划的 `target_stack_operation`
        照它再走一遍等于迁移两次，所以这里把它钉成 `keep` ——
        **栈已经到位，这一次只管消息**。
        先例：`_validate_scheduled_event_changes` 把模型给的时点验完就丢。

        ## 检索**复用第一次的**，不再检一遍

        客户的问题没变，而再检一遍要再花一次钱、还会多写一行
        `retrieval_evidence`（`050` 把那张表定成了「这一轮检索发生过什么」，
        一轮两行读起来像检索了两次）。所以 `knowledge` 是传进来的。

        ## 素材也复用

        栈操作钉成 `keep`，所以这一轮结束在同一个帧上 ——
        `select_materials` 会算出同一份。**再算一遍不是错，而是白算**，
        而「算两遍」在本仓是一条点过名的形状（素材那一侧同一条纪律）。
        """

        try:
            proposed = self._inference.plan(
                understanding=self._understanding_for_plan(prepared, understanding),
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                context=context,
                required_strategy=required_strategy,
                assets=planning_assets,
                last_outbound=prepared.last_outbound,
                conversation_summary=prepared.conversation_summary,
                summary_lines_not_shown=prepared.summary_lines_not_shown,
                known_memories=prepared.known_memories,
                memories_not_shown=prepared.memories_not_shown,
                materials_already_sent=prepared.delivered_materials,
                # **这一次唯一的新输入** —— 上一次许了什么而没兑现。
                unkept_promises=promised_sentences,
            )
        except (ProviderError, CostCeilingExceeded):
            return None

        # **钉成 `keep`，在校验之前** —— 校验器会按它判目标栈那一族规则，
        # 而我们要它判的是「不动栈」这一份，不是模型提的那一份。
        pinned = {
            **proposed,
            "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        }
        plan, rejection = plan_or_fallback(
            plan=pinned,
            understanding=self._understanding_for_plan(prepared, understanding),
            aggregate=prepared.aggregate,
            profile=prepared.profile,
            required_strategy=required_strategy,
            event_type=str(prepared.envelope.get("event_type") or ""),
        )
        if rejection is not None:
            # 重跑那份计划不合法 —— **不走回落计划**：回落是一条保守路径，
            # 而第一次尝试已经有一份合法的计划与一条发得出去的消息。
            return None

        dropped: list[dict[str, Any]] = []
        echoed: list[dict[str, Any]] = []
        unsourced: list[dict[str, Any]] = []
        unkept: list[dict[str, Any]] = []
        try:
            generation = self._generate_and_validate(
                prepared=prepared,
                plan=plan,
                understanding=understanding,
                materials=materials,
                knowledge=knowledge,
                assets=assets,
                context=context,
                # **重跑那一次递的是同一份记忆** —— 见本方法那个形参的注释。
                remembered_facts=remembered_facts,
                dropped=dropped,
                echoed=echoed,
                unsourced=unsourced,
                unkept=unkept,
            )
        except (ProviderError, CostCeilingExceeded, GenerationRejected):
            return None
        return generation, plan, (dropped, echoed, unsourced, unkept)

    def _record_promise_retry(
        self,
        prepared: PreparedEvent,
        *,
        promised: list[str],
        kept_after_retry: bool,
        retry_ran: bool,
        correlation_id: UUID,
    ) -> None:
        """重跑发生过，以及它有没有用（T367）。

        **这一行是判断 T367 那条路值不值的唯一读数。** 没有它，
        「重跑帮上忙了吗」只能靠比较两个总数去猜 ——
        而那两个数里混着别的原因造成的变化。

        **只记条数与「有没有用」，不记那几句正文**：与
        `_record_plan_generation_disagreement` 逐字同一条理由 ——
        正文是客户可见的话，而审计行会被人翻、被导出。
        ⚠️ **所以这一行答不出「它许的是什么」** —— 那半在那条不一致审计行的
        `unkept_promises[].promised`（动词短语）里，写出来免得下一个人以为它全答了。
        """

        with self._database.transaction(
            tenant_id=prepared.tenant_id, correlation_id=correlation_id
        ) as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'plan.promise_retry',
                          'conversation_aggregate', %s, %s, %s, %s)
                """,
                (
                    prepared.tenant_id,
                    uuid4(),
                    prepared.conversation_id,
                    correlation_id,
                    UNKEPT_PROMISE_REASON_CODE,
                    Jsonb(
                        {
                            "promises": len(promised),
                            # 重跑真的跑起来了吗（供应商抛了 / 计划被拒 → False）。
                            "retry_ran": bool(retry_ran),
                            # **它有没有用** —— 这一个布尔是本条留痕的全部价值。
                            "kept_after_retry": bool(kept_after_retry),
                        }
                    ),
                ),
            )

    def _record_plan_generation_disagreement(
        self,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan,
        dropped: list[dict[str, Any]],
        echoed: list[dict[str, Any]],
        unsourced: list[dict[str, Any]],
        #: 许了而这一轮没兑现、于是被丢掉的那几**句**（批 F）。
        #:
        #: ⚠️ **一条 = 一句，不是一段**（2026-09-19 重交）：隔壁 `echoed` /
        #: `unsourced` 那两档一条 = 一段，而这一档在段内动刀 ——
        #: 那一段其余的字照发。数它的人要知道这个单位不一样。
        unkept: list[dict[str, Any]],
        correlation_id: UUID,
    ) -> None:
        """计划与生成谈不拢的两件，各留一行。

        ## 为什么两件都要留痕

        * **平台补过一条候选** —— 不留痕就等于**悄悄改写了模型的计划**，
          而「这一轮的 `send_message` 是模型提的还是平台补的」
          必须永远答得出来；
        * **丢掉过一条意图** —— 它此前是整轮拒，有一行 `plan.no_send` 记着；
          改成丢掉之后如果什么都不写，**那一条就凭空消失了**，
          而一条悄悄消失的意图与一条被拒的意图，排查成本差一个量级。

        **只记 kind，不记 payload**：payload 是客户可见的话，
        而审计行会被人翻、被导出、被贴进工单。
        """

        with self._database.transaction(
            tenant_id=prepared.tenant_id, correlation_id=correlation_id
        ) as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'plan.generation_disagreed',
                          'conversation_aggregate', %s, %s, %s, %s)
                """,
                (
                    prepared.tenant_id,
                    uuid4(),
                    prepared.conversation_id,
                    correlation_id,
                    GENERATION_DISAGREED_REASON_CODE,
                    Jsonb(
                        {
                            "strategy_id": plan.strategy_id,
                            "reply_floor_basis": plan.reply_floor_basis,
                            # **带上理由码**：这个常量此前 `git grep` 只有
                            # 定义那一行，**零消费方** —— 于是审计里只有一个
                            # 数字，「它为什么被丢」无处可查。
                            "echoed_segments": [
                                {
                                    "why": ECHOED_MATERIAL_REASON_CODE,
                                    "shared_chars": int(item.get("shared_chars") or 0),
                                }
                                for item in echoed
                            ],
                            # **理由码与那个数，不记正文**（与上一行同源）。
                            # 那个数是我们自己的标价 —— 客户事实是「他没说过它」，
                            # 而那件事这一行本身就说清楚了。
                            "unsourced_segments": [
                                {
                                    "why": str(item.get("why")),
                                    "number": item.get("number"),
                                    "shared_chars": item.get("shared_chars"),
                                }
                                for item in unsourced
                            ],
                            # **记那个动词短语，不记正文**（与上两行同源）。
                            #
                            # 排查这条要答的是「它许了什么形状的东西」——
                            # 「发您」「安排」「讲清楚」答得了，
                            # 而把整句许诺抄进审计行只是把客户可见的话又存了一份。
                            "unkept_promises": [
                                {
                                    "why": str(item.get("why")),
                                    "promised": str(item.get("promised") or ""),
                                }
                                for item in unkept
                            ],
                            "dropped_kinds": [
                                str(intent.get("kind")) for intent in dropped
                            ],
                            "planned_kinds": [
                                str(candidate.get("kind"))
                                for candidate in plan.document["candidate_intents"]
                            ],
                        }
                    ),
                ),
            )

    def _persist_no_send(
        self,
        prepared: PreparedEvent,
        *,
        plan: ValidatedPlan | None,
        reason_code: str,
        detail: str,
        interruption_relation: str | None = None,
        proposal_denials: tuple[ProposalDecision, ...] = (),
        next_allowed_at: datetime | None = None,
        rejection_context: Mapping[str, Any] | None = None,
    ) -> PipelineResult:
        """Persist an event that legitimately produces no customer message.

        A rejected plan and a deliberately silent strategy end the same way: the
        aggregate still moves forward, the inbox event is still consumed, and the
        reason is still recorded. What must never happen is the third option --
        dropping the event with nothing written down.
        """

        policy_id = uuid4()
        handoff = reason_code in HANDOFF_REASON_CODES
        # A task without the transition: the transition's three effects have
        # already happened (ADR-0054 §三.6).
        task_only = reason_code in TASK_WITHOUT_HANDOFF_REASON_CODES
        handoff_source = (
            "cost_governor"
            # The takeover-period refusal is a cost-governor event too: it comes
            # from the same ceiling, and filing it under `target_stack` would put
            # it in the wrong pile for anyone counting where cost stops work.
            if reason_code in COST_REFUSAL_REASON_CODES
            or reason_code in TASK_WITHOUT_HANDOFF_REASON_CODES
            else "target_stack"
        )
        manual_task_id = uuid4() if handoff or task_only else None
        # **One task per takeover, not one per message** (ADR-0058 §二.3, from
        # independent review F4). The idempotency key carries the event id, so
        # it is unique per message and de-duplication cannot help: a customer
        # who keeps typing after the ceiling was hit opens one task per message.
        # The classification is `NO_PUSH`, so nobody is woken -- **but the task
        # list is the only channel §三.6 gives this condition**, and burying it
        # there is the same silence by a different route.
        #
        # `suppressed_counter` is the field the policy contract already reserves
        # for exactly this, and it was being sent as `None`.
        # **A string, because the contract says so.** `policy-hook` types this
        # field `string | null`; passing the integer failed validation, and the
        # right move on a frozen contract is to match it, not to widen it for
        # one caller's convenience.
        #
        # **"Still open" is now the task's word, not the intent's** (034). The
        # intent stays `PROPOSED` forever -- resolving a task is not an act on
        # the saga -- so once a person can close one, an intent-side probe would
        # go on suppressing against work that is already done, and the next
        # refusal would leave no task at all.
        suppressed_counter: str | None = None
        if task_only and manual_task_id is not None:
            with self._database.transaction(tenant_id=prepared.tenant_id) as probe:
                open_tasks = probe.execute(
                    """
                    SELECT count(*) AS total FROM manual_tasks
                     WHERE tenant_id = %s AND conversation_id = %s
                       AND status IN ('open', 'claimed') AND reason_code = %s
                    """,
                    (prepared.tenant_id, prepared.conversation_id, reason_code),
                ).fetchone()
            already_open = int(open_tasks["total"]) if open_tasks else 0
            if already_open:
                # Counted, not dropped: "the understanding step was refused N
                # times during this takeover" is one readable fact, and N tasks
                # saying it once each is not.
                manual_task_id = None
                suppressed_counter = str(already_open)
        # **连续第二次同码拒绝 → 升级出一条人工任务**（ADR-0137 裁定一，N=2）。
        #
        # **「连续」= 该会话 `policy_decisions` 里最近一行同码。**
        # 中间隔了一轮**别的**理由码的拒绝**算打断** —— 那说明模型在尝试不同的东西，
        # 不是卡死，而卡死才是这里要抓的形状。
        # **这个语义不在去重键里**（键是「会话 + 理由码 + 未关闭」），
        # 所以它只活在下面那句 `ORDER BY created_at DESC LIMIT 1` 与本段说明里；
        # 守着它的是 `test_a_different_reason_code_in_between_breaks_the_streak`。
        #
        # **去重键照上面 `task_only` 那一族的形状**（`status IN ('open','claimed')`
        # + 同 `reason_code` + 同会话）：**一个循环一条任务，不是一轮一条。**
        loop_escalation: dict[str, Any] | None = None
        if manual_task_id is None and reason_code in LOOP_ESCALATION_REASON_CODES:
            with self._database.transaction(tenant_id=prepared.tenant_id) as probe:
                previous = probe.execute(
                    """
                    SELECT reason_code, id FROM policy_decisions
                     WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'
                     ORDER BY created_at DESC LIMIT 1
                    """,
                    (prepared.tenant_id, prepared.conversation_id),
                ).fetchone()
                open_loop_tasks = probe.execute(
                    """
                    SELECT count(*) AS total FROM manual_tasks
                     WHERE tenant_id = %s AND conversation_id = %s
                       AND status IN ('open', 'claimed') AND reason_code = %s
                    """,
                    (prepared.tenant_id, prepared.conversation_id, reason_code),
                ).fetchone()
            already = int(open_loop_tasks["total"]) if open_loop_tasks else 0
            # `previous` 绑进一个局部名，好让类型收窄跟着走到下面那几行 ——
            # 「最近一行」在这里既是判据（同码？）又是证据（它的 id 进任务）。
            last_denial = previous if previous is not None else None
            consecutive = last_denial is not None and str(last_denial["reason_code"]) == reason_code
            if consecutive and last_denial is not None and not already:
                manual_task_id = uuid4()
                # **任务要自解释**：只写理由码不够 —— 它说的是「跳过了必填槽位」，
                # 而接手的人要知道**哪个槽位、模型连着选了什么策略**。
                # 事实来自拒绝发生的那一处（`PlanRejected.context`），
                # **不是从 `detail` 那句话里解析回来的**。
                empty_slots = list((rejection_context or {}).get("empty_required_slots") or [])
                loop_escalation = {
                    "escalated_after_consecutive_denials": 2,
                    # **两行 deny 都点名**，顺序是「先前那行、这一行」——
                    # 接手的人要能把任务跟回那两次拒绝。这一行此刻**还没写进表**
                    # （`record_policy_decision` 在下面），所以它的 id 只能从
                    # `policy_id` 取，而上一行只能从查询取。
                    "policy_decision_ids": [str(last_denial["id"]), str(policy_id)],
                    "slot": next(iter(empty_slots), None),
                    "empty_required_slots": empty_slots,
                    "strategy_id": (rejection_context or {}).get("strategy_id"),
                }
                # **这一族不止一个码了**（2026-09-18）。上面那四个键是
                # `STRATEGY_SKIPS_REQUIRED_SLOT` 的事实；别的码有别的事实，
                # 而它们同样来自拒绝发生的那一处（`InferenceRejected.context`）。
                #
                # **不覆盖已有键**：上面那几个已经从同一个 context 里取过，
                # 再铺一遍只会把「取过一次」写成两处。
                loop_escalation.update(
                    {
                        key: value
                        for key, value in (rejection_context or {}).items()
                        if key not in loop_escalation
                    }
                )
            elif consecutive and already:
                # 数出来而不是丢掉 —— 与上面那一族同一个理由。
                suppressed_counter = str(already)
        notification_outbox_id: UUID | None = None
        # **落成了待办才有东西可通知**（T276）。此前这一处的条件是
        # `manual_task_id is not None` —— 而那个 id 是本方法自己生成的，
        # **它永远不是 `None`**，于是那半个条件从来没有排除过任何一轮。
        # 合并之后真正的答案由 `_file_a_manual_task` 给：`None` = 这个
        # 幂等键已经落过一条了，那一轮不该再推第二条通知。
        filed_manual_task: UUID | None = None
        # **The one place that decides whether a merchant hears about this.**
        # `ROOT_TARGET_FRAME_ABANDONED` is why the relation is passed down: a
        # customer asking for a person and a customer dropping one side question
        # produce the same reason code, and the ruling (user, 2026-09-04) wants
        # the first pushed immediately and the second not pushed at all.
        # `relation` is the only thing that separates them today -- the scope gap
        # registered with the T53/T54 batch -- and where it is missing the
        # conservative entry wins.
        classification = classify(reason_code, interruption_relation=interruption_relation)
        obligations = (
            ["audit", "create_manual_task"]
            if manual_task_id is not None
            else ["audit"]
        )
        policy_contract: dict[str, Any] = {
            "input": {
                "hook": "post_plan",
                "context": {
                    "tenant_id": str(prepared.tenant_id),
                    "aggregate_generation": prepared.aggregate["generation"],
                    "control_mode": prepared.aggregate["control_mode"],
                },
                "target_frame": prepared.aggregate["target_stack"][-1],
                "strategy_or_intent": {
                    "strategy_id": plan.strategy_id if plan else "none",
                    "origin": plan.origin if plan else "provider_failure",
                },
            },
            "output": {
                "effect": "deny",
                "reason_code": reason_code,
                "modifications": {},
                "obligations": obligations,
                "policy_id": "plan-validator",
                "policy_version": "1.0.0",
                # **Not a literal `None` any more** (ADR-0119). `policy-hook`
                # reserved this field long before anything produced it; quiet
                # hours is the producer, and the value also lands in a column
                # so it can be queried rather than parsed back out of evidence.
                "next_allowed_at": _iso(next_allowed_at) if next_allowed_at else None,
                "suppressed_counter": suppressed_counter,
                "terminal_exit": False,
            },
        }
        validate_contract("policy-hook", policy_contract)

        if handoff:
            # Leaving control with the AI would mean the customer waits on a
            # system that has already decided it will not answer -- because a
            # hard ceiling stopped it, or because no frame on the stack can
            # accept work any more.
            suspended = suspend_target_stack_for_human(
                aggregate=prepared.aggregate,
                occurred_at=str(prepared.envelope["occurred_at"]),
            )
            prepared.aggregate.clear()
            prepared.aggregate.update(suspended)
            prepared.aggregate["control_mode"] = "human"
            validate_contract("conversation-aggregate", prepared.aggregate)

        with self._database.transaction(tenant_id=prepared.tenant_id) as connection:
            _apply_aggregate_transition(connection, prepared)
            _write_frame_entry_audit(connection, prepared, prepared.frame_entry)
            # **静默时段挡下的那次跟进，等时段过了再来一次**（ADR-0148，
            # 用户 2026-09-21：「晚上计划跟进被档，过了静默时间应该触发被档的那次跟进」）。
            #
            # ⚠️ **在此之前它就那样没了**：那一行定时器已经 `CONSUMED`，
            # 而 `next_allowed_at`（静默结束的那一刻，`quiet_hours.lifts_at` 算的）
            # **只被写进 policy hook 当留痕，没有任何东西据它重排**。
            # 运维复测第 7 条记过同一族：「静默时段挡下的跟进正文无留痕，
            # 占那通成本 27%」。
            _reschedule_after_quiet_hours(
                connection,
                prepared,
                reason_code=reason_code,
                next_allowed_at=next_allowed_at,
            )
            if handoff:
                connection.execute(
                    """
                    UPDATE conversation_aggregates SET control_mode = 'human'
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (prepared.tenant_id, prepared.conversation_id),
                )
            if manual_task_id is not None:
                target_frame_id = UUID(str(prepared.aggregate["target_stack"][-1]["id"]))
                event_id = UUID(str(prepared.envelope["event_id"]))
                # **The task itself now lives in `manual_tasks`** (ADR-0048 K18,
                # migration 034). The intent record keeps proposing it -- that is
                # the saga -- but the classification, and everything a person
                # will do with the task, belongs to a resource authorization can
                # name. `owner_employee_id`, `sla_due_at` and `expires_at` are
                # left NULL and that is the honest answer: §8 asks for all three
                # and the engine has no source for any of them (T91). The human
                # side supplies them (freeze B3 #16 makes owner and SLA required
                # there).
                #
                # ⚠️ **合并进 `_file_a_manual_task` 时这一处的幂等语义变了**
                # （T276，2026-09-20）：此前这两条 INSERT **一条 `ON CONFLICT`
                # 都没有**，而 `intent_records` 上有 `UNIQUE (tenant_id,
                # idempotency_key)` —— 重放同一个回合会抛，不是 no-op。
                # 现在它与另外四处一样短路。**这是一次有意的语义改动**，
                # 写进了本批自审的「不可逆 / 改了投递语义」那一格。
                filed_manual_task = _file_a_manual_task(
                    connection,
                    tenant_id=prepared.tenant_id,
                    conversation_id=prepared.conversation_id,
                    manual_task_id=manual_task_id,
                    reason_code=reason_code,
                    classification=classification,
                    source=handoff_source,
                    proposal_facts={},
                    proposed_by_version=f"{handoff_source}-v1",
                    strategy_id=plan.strategy_id if plan else "no_send",
                    target_frame_id=target_frame_id,
                    idempotency_key=f"manual_task:{reason_code}:{event_id}",
                    preconditions={
                        "aggregate_version": prepared.aggregate["state_version"],
                        "aggregate_generation": prepared.aggregate["generation"],
                    },
                    policy_evidence=[str(policy_id)],
                    # **这一列此前没填**，于是这条路上的任务只有一个理由码。
                    # 死循环那一族要它自解释（哪个槽位 / 哪条策略 / 哪两行 deny），
                    # 而那些事实只有拒绝发生的那一处知道。
                    # 非升级的任务照旧只得到 `{}` —— 本批不扩大它们的形状。
                    evidence=loop_escalation or {},
                )
            if filed_manual_task is not None and classification.pushes_now:
                notification_outbox_id = self._notify_manager(
                    connection,
                    prepared=prepared,
                    reason_code=reason_code,
                    # **通知的是真的落成的那一条**（T276）。此前这里传的是
                    # `manual_task_id`，靠外层那个 `manual_task_id is not None`
                    # 替 mypy 收窄；条件换成「落成了吗」之后收窄就没了，
                    # 而 mypy 报出来的正是这件事 ——
                    # **它指的不只是一个类型，是「通知该指哪一条」**。
                    manual_task_id=filed_manual_task,
                )
            self._record_plan_refusal(
                connection,
                prepared=prepared,
                row_id=policy_id,
                # ⚠️ **指这一轮真的立起来的那条意图，不是本方法生成的那个 id**
                # （T276，2026-09-20）。`policy_decisions` 有一条
                # `FOREIGN KEY (tenant_id, intent_id) REFERENCES intent_records`
                # （`001_m0.sql:221`）—— 幂等键撞上时这一轮一条意图都没插进去，
                # 那时再拿 `manual_task_id` 去指，就是一次外键违例。
                # 这一列本来就是 `UUID | None`（形参与建表两处都是），
                # **`None` 一直是「这一轮没有立意图」** —— 现在它说的是实话。
                intent_id=filed_manual_task,
                reason_code=reason_code,
                obligations=obligations,
                next_allowed_at=next_allowed_at,
                context=policy_contract["input"]["context"],
            )
            _consume_inbox_event(connection, prepared)
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'plan.no_send', 'conversation_aggregate', %s, %s, %s, %s)
                """,
                (
                    prepared.tenant_id,
                    uuid4(),
                    prepared.conversation_id,
                    UUID(str(prepared.envelope["correlation_id"])),
                    reason_code,
                    Jsonb(
                        {
                            "policy_decision_id": str(policy_id),
                            "strategy_id": plan.strategy_id if plan else "none",
                            "plan_origin": plan.origin if plan else "provider_failure",
                            "detail": detail[:500],
                            "model_version": self._inference.version,
                            "handed_to_human": handoff,
                            # 同上：报这一轮真的落成的那一条，
                            # 没落成就是 `None`（T276）。
                            "manual_task_id": (
                                str(filed_manual_task) if filed_manual_task else None
                            ),
                        }
                    ),
                ),
            )
            if handoff:
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        tenant_id, id, actor_id, action, resource_type, resource_id,
                        correlation_id, reason_code, evidence
                    ) VALUES (%s, %s, NULL, 'conversation.handoff',
                              'conversation_aggregate', %s, %s, %s, %s)
                    """,
                    (
                        prepared.tenant_id,
                        uuid4(),
                        prepared.conversation_id,
                        UUID(str(prepared.envelope["correlation_id"])),
                        reason_code,
                        Jsonb(
                            {
                                "detail": detail[:500],
                                # L2a. Three states, all of them worth reading
                                # back: an outbox id means a notification was
                                # produced and is queued (**not delivered** in
                                # M1a); `null` means this tenant has no manager
                                # contact configured, which is a fact about
                                # setup rather than a failure to record.
                                "manager_notification_outbox_id": (
                                    str(notification_outbox_id)
                                    if notification_outbox_id is not None
                                    else None
                                ),
                                "manager_notification_delivered": False,
                                # Why there is or is not a notification, without
                                # anyone re-deriving it from the code.
                                "manual_task_class": classification.audience_class,
                                "manual_task_notify": classification.cadence,
                            }
                        ),
                    ),
                )
            for denial in proposal_denials:
                # The turn-level reason code above says what happened to the
                # turn; these say what happened to each proposal. Both are
                # needed once a turn can carry more than one (T44): a single
                # wholesale denial cannot answer "which one", and "which one" is
                # the question somebody reading this later actually has.
                self._record_proposal_denial(connection, prepared, plan=plan, decision=denial)
            # **同一件事的另一半**：`manual_task` 是这一轮唯一的提案时，
            # 整轮走的是 no-send，上面那个循环够不着。没有 `promised=`——
            # 这一轮没有任何东西发给客户，所以也没有「AI 刚才答应」。
            self._file_task_for_unsendable_intents(
                connection, prepared, plan=plan, refused=proposal_denials
            )
            # **本轮收口的最后一句**（RFC §3.2 的那两个引用数组，B1-5）：
            # 放在最后，是因为本轮自己的意图与定时事件都在上面几句里才写进去。
            _refresh_reference_projection(connection, prepared)

        return PipelineResult(inbox_id=prepared.inbox_id, status="no_send")

    def _notify_manager(
        self,
        connection: Any,
        *,
        prepared: PreparedEvent,
        reason_code: str,
        manual_task_id: UUID,
        promised_text: str | None = None,
    ) -> UUID | None:
        """Queue one notification to the tenant's manager. **Queue, not send.**

        Ruling (user, 2026-09-04), and each half matters:

        * it goes through the **existing** chain -- intent record, policy
          decision, outbox row -- so retries, terminal states and receipts are
          the ones already built and tested. There is no second delivery path;
        * and M1a's "nothing is sent" applies to it too. The row is written and
          stops there, because `_claim_one` claims customer rows only. What this
          batch delivers is **produced and queued**, and the last hop is tested
          with customer messages in M1b.

        Returns the outbox id, or `None` when the tenant has no manager
        configured -- in which case nothing is invented: no row, and the audit
        event above says so.

        **What M1b must change before deleting the audience predicate.** The
        first version of this row was not a row anything could have sent, and
        "produced and queued" only means something if the queue can one day be
        drained (independent review 2026-09-04, F3). Three things, together:

        1. **route by `recipient`.** `dispatch_one` reads `command["content"]`
           and posts to the customer channel; nothing reads `recipient` yet. A
           dispatcher that ignores it would send the merchant's notice into the
           customer's conversation.
        2. **`content` is a typed payload**, the same shape the customer row
           carries (`{"type": "text", "text": …}`), not a bare string -- fixed
           here, because a shape mismatch discovered on the day delivery is
           switched on is a shape mismatch discovered in production.
        3. **the deadline is a day, not thirty seconds.** It used to copy the
           customer envelope's `runtime_deadline_at`, so every notice expired
           within half a minute of being written. On the day the predicate is
           removed, the whole backlog would be claimed, immediately fail
           `deadline_exceeded_before_send`, land in `UNKNOWN`, and -- with L1
           merged -- each one would produce a fresh manual task, which this
           batch's own table classifies as `batched`, producing another notice.
           A queue that manufactures work out of its own backlog.
        """

        # The same ruling the digest reads under (ADR-0078, T77), with the
        # 2026-09-08 amendment: the deployment writes this table, **and since
        # ADR-0104 so can the merchant** via
        # `PUT /tenants/current/manager-contact`. A tenant nobody configured is
        # still told nothing -- that half is unchanged, and the branch below is
        # where it takes effect.
        contact = connection.execute(
            """
            SELECT channel, config_id, external_user_id
              FROM tenant_manager_contacts WHERE tenant_id = %s
            """,
            (prepared.tenant_id,),
        ).fetchone()
        if contact is None:
            # **The silence gets a row** (ADR-0104 §五.2, hung on this batch).
            # ADR-0078 wrote this cost down in M1a and it was free then: K17 had
            # moved the notification surface out, so "this tenant is told
            # nothing" described a system that told nobody anything. B1-5 makes
            # delivery real, and from here the same branch means **a person who
            # should have been told was not** -- while nothing errors and no
            # metric moves.
            #
            # An audit row rather than a metric: it is the table that already
            # answers "what did the platform decide about this tenant", the
            # dispatcher writes to it on every other terminal decision, and it
            # can be counted later without deciding today what the alert should
            # be. `PUT /tenants/current/manager-contact` (ADR-0104) is the route
            # that fixes the underlying state; this is how anyone finds out it
            # is needed.
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'manager_notice.undeliverable',
                          'tenant_manager_contact', NULL, %s, %s, %s)
                """,
                (
                    prepared.tenant_id,
                    uuid4(),
                    UUID(str(prepared.envelope["correlation_id"])),
                    "NO_MANAGER_CONTACT",
                    Jsonb({"manual_task_id": str(manual_task_id), "notice_reason": reason_code}),
                ),
            )
            return None

        customer_message = customer_words(prepared.envelope.get("content"))
        # The merchant's own customer's words, kept verbatim; only what belongs
        # to us is taken out. `notify_text` is a separate vocabulary from
        # `redact()` on purpose -- see `runtime/notification.py`.
        body = manual_task_notice(
            reason_code=reason_code,
            conversation_id=str(prepared.conversation_id),
            customer_message=customer_message,
            # **固定模板 + 附模型原话**（用户裁定 2026-09-11），不是二选一。
            # 只有「AI 应下一件它做不到的事」这一类待办有这个东西；
            # 其余调用方不传，正文与先前逐字相同。
            promised_text=promised_text,
            # **我们已经问出来的那几格**（用户 2026-09-17）。
            #
            # 同事最不该做的事是把 AI 刚问过的再问一遍 ——
            # 而不给这份信息就一定会发生。取当前帧的槽位：
            # 那正是这通对话到此为止「我们知道些什么」。
            #
            # **空槽位不会出现在正文里**（`_known_facts_line` 过滤掉），
            # 所以这里不必先挑一遍。
            known_facts=(prepared.aggregate.get("target_stack") or [{}])[-1].get("slots"),
        )

        intent_id = uuid4()
        outbox_id = uuid4()
        notice_policy_id = uuid4()
        event_id = UUID(str(prepared.envelope["event_id"]))
        idempotency_key = f"manager_notice:{reason_code}:{event_id}"
        target_frame_id = UUID(str(prepared.aggregate["target_stack"][-1]["id"]))
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions,
                policy_evidence, manual_task_id
            ) VALUES (%s, %s, %s, 'handoff', 'PROPOSED', %s, %s, 'handoff', %s, %s, %s, %s, %s)
            """,
            (
                prepared.tenant_id,
                intent_id,
                prepared.conversation_id,
                Jsonb({"audience": "tenant_manager", "reason_code": reason_code}),
                Jsonb({"step": "platform", "version": "manual-task-notice-v1"}),
                target_frame_id,
                idempotency_key,
                Jsonb(
                    {
                        "aggregate_version": prepared.aggregate["state_version"],
                        "aggregate_generation": prepared.aggregate["generation"],
                    }
                ),
                Jsonb([str(notice_policy_id)]),
                manual_task_id,
            ),
        )
        # **This notice needs its own decision.** The first version pointed the
        # intent's `policy_evidence` at the `post_plan` **deny** in the same
        # transaction -- an APPROVED delivery intent whose only policy evidence
        # was a refusal (independent review 2026-09-04, P2-1). The design
        # document draws `pre_execute / allow / [audit, redact]` here, and the
        # `redact` obligation is this batch's only audit-trail evidence that the
        # notification vocabulary ran at all.
        record_policy_decision(
            connection,
            tenant_id=prepared.tenant_id,
            row_id=notice_policy_id,
            conversation_id=prepared.conversation_id,
            intent_id=intent_id,
            output=PolicyOutput(
                hook="pre_execute",
                effect="allow",
                reason_code=reason_code,
                policy_id="manager-notice-policy",
                policy_version="1.0.0",
                obligations=audit_and_redact(),
                context={
                    "tenant_id": str(prepared.tenant_id),
                    "audience": "tenant_manager",
                    "manual_task_id": str(manual_task_id),
                },
            ),
        )
        # PROPOSED -> APPROVED, the same two steps the customer path takes. Born
        # approved would mean the row never passed through the state the policy
        # decision above is evidence *for* (P2-1).
        connection.execute(
            """
            UPDATE intent_records SET status = 'APPROVED', updated_at = now()
             WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'
            """,
            (prepared.tenant_id, intent_id),
        )
        connection.execute(
            """
            -- **`0` because this row owns its turn** (independent review
            -- 2026-09-08, P3-4). `_notify_manager` has exactly one caller,
            -- inside `_persist_no_send`: a turn either produces customer
            -- messages or goes no-send and tells the manager -- never both --
            -- so this 0 cannot collide with a customer message's 0. **B1-5 is
            -- the batch that changes it**: it drops the `audience` predicate
            -- and routes by `recipient`. **B1-5 has now landed and this
            -- invariant survived it**: dropping the predicate changed what the
            -- dispatcher *claims*, not what a turn *writes*, and
            -- `_notify_manager` still has exactly one caller. The batch that
            -- breaks it is whichever one first makes a single turn produce both
            -- audiences; until then 0 is not a placeholder, it is the position.
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                ordinal
            ) VALUES (%s, %s, %s, %s, %s, %s, 0)
            """,
            (
                prepared.tenant_id,
                outbox_id,
                prepared.conversation_id,
                intent_id,
                Jsonb(
                    {
                        "audience": "tenant_manager",
                        "recipient": {
                            "channel": contact["channel"],
                            "config_id": contact["config_id"],
                            "external_user_id": contact["external_user_id"],
                        },
                        "tenant_id": str(prepared.tenant_id),
                        "conversation_id": str(prepared.conversation_id),
                        # Same typed shape as the customer row's content --
                        # **and since 2026-09-10 that sentence is true**: the
                        # customer row is normalised on the way in, and both
                        # rows now name the type from one constant (independent
                        # review P3-6).
                        "content": {"type": TEXT_CONTENT_TYPE, "text": body},
                        # **A day, not the customer envelope's thirty seconds.**
                        # A notice that has been queued for a day is stale news
                        # a person should not be woken by; a notice that expires
                        # thirty seconds after it is written was never queued at
                        # all, it was written pre-dead.
                        "deadline_at": _iso(
                            datetime.now(UTC) + timedelta(seconds=NOTICE_DEADLINE_SECONDS)
                        ),
                        "correlation_id": prepared.envelope["correlation_id"],
                    }
                ),
                idempotency_key,
            ),
        )
        return outbox_id

    def _release_lease(self, pointer: InboxPointer) -> None:
        """Hand a leased event back without spending an attempt.

        Losing the aggregate CAS is the normal outcome of two workers meeting on
        one conversation, not a defect of the event, so it must not consume the
        retry budget. This needs its own transaction: the one that raised has
        already rolled back, which is why releasing the lease inside it -- as
        this path did until 2026-09-02 -- never reached the database.

        Both bookkeeping writes are guarded on `lease_owner = self._worker_id`.
        Without it, a slow worker returning here after its lease had expired and
        a second worker had claimed the same row would hand back a lease it no
        longer holds, letting a third worker process the event concurrently with
        the second.
        """

        with self._database.transaction(tenant_id=pointer.tenant_id) as connection:
            connection.execute(
                """
                UPDATE inbox_events
                   SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL
                 WHERE tenant_id = %s AND id = %s AND status = 'processing'
                   AND lease_owner = %s
                """,
                (pointer.tenant_id, pointer.inbox_id, self._worker_id),
            )

    def _record_failed_attempt(self, pointer: InboxPointer, error: BaseException) -> None:
        """Count one failed pass and dead-letter the event once the budget is out.

        `error_code` carries the exception type and nothing else. The message can
        contain provider or customer material, and this row is read by anyone
        with inbox access.

        Guarded on `lease_owner` for the reason in `_release_lease`, and on
        `status = 'processing'` because that is the only state a worker that
        still holds its claim can have left the row in.

        > **Superseded 2026-09-22 (A6). The paragraph that stood here read:**
        >
        > > The consequence is deliberate: a failure raised *inside* `prepare`
        > > rolls its own claim back, so no lease was ever held and no attempt is
        > > counted. Such an event is retried without bound until the underlying
        > > fault is fixed -- registered as a known gap rather than papered over
        > > by a write that could target a row this worker does not own.
        >
        > **The gap was real and it fired.** On 2026-09-22 a contract mismatch
        > made `prepare` raise on every pass for one event: `attempt` stayed 0,
        > `error_code` stayed NULL, `status` stayed `pending`, and the scheduler
        > republished it every ten seconds. Ops stopped it by hand at **70
        > retries** and the customer's message was lost doing so
        > (`docs/handoffs/ops-to-author-two-bounds-on-one-goal-2026-09-22.md`).
        > A configuration fault made the event permanently poisonous, and a
        > poisonous event never came off the rail.

        **The objection in that paragraph is the thing the second statement below
        answers, not something it ignores.** "A write that could target a row
        this worker does not own" is prevented by asking for a row that *nobody*
        owns: `status = 'pending' AND lease_owner IS NULL`. That is exactly the
        state a rolled-back claim leaves behind, and it is never the state of the
        cross-worker steal -- there the row is `processing` and owned by the
        thief, so neither statement matches and this worker still writes nothing
        (`test_a_worker_cannot_write_bookkeeping_onto_a_lease_it_no_longer_holds`).

        The second statement runs only when the first matched nothing, so an
        ordinary failure -- one that kept its lease -- never reaches it.

        **Two workers can both fail in `prepare` on the same row and both count
        a pass.** Over-counting a poisoned event costs nothing: it is a budget,
        and the row is poisoned either way. Under-counting is the defect.
        """

        with self._database.transaction(tenant_id=pointer.tenant_id) as connection:
            kept = connection.execute(
                """
                UPDATE inbox_events
                   SET attempt = attempt + 1,
                       status = CASE WHEN attempt + 1 >= %s THEN 'failed' ELSE 'pending' END,
                       error_code = %s, lease_owner = NULL, lease_expires_at = NULL
                 WHERE tenant_id = %s AND id = %s AND status = 'processing'
                   AND lease_owner = %s
                """,
                (
                    MAX_INBOX_ATTEMPTS,
                    type(error).__name__,
                    pointer.tenant_id,
                    pointer.inbox_id,
                    self._worker_id,
                ),
            )
            if kept.rowcount == 1:
                return

            # The claim rolled back with the work, so there is no lease to guard
            # on. `lease_owner IS NULL` is the guard instead: it is true only of
            # a row nobody holds.
            connection.execute(
                """
                UPDATE inbox_events
                   SET attempt = attempt + 1,
                       status = CASE WHEN attempt + 1 >= %s THEN 'failed' ELSE 'pending' END,
                       error_code = %s
                 WHERE tenant_id = %s AND id = %s AND status = 'pending'
                   AND lease_owner IS NULL
                """,
                (
                    MAX_INBOX_ATTEMPTS,
                    type(error).__name__,
                    pointer.tenant_id,
                    pointer.inbox_id,
                ),
            )

    def process_pointer(self, pointer: InboxPointer) -> PipelineResult | None:
        try:
            prepared = self.prepare(tenant_id=pointer.tenant_id, inbox_id=pointer.inbox_id)
            if prepared is None:
                self._queue.acknowledge(pointer)
                return None
            result = self.process_prepared(prepared)
        except AggregateConflict:
            # PostgreSQL owns the fact and lease. Redis is only a rebuildable
            # wake-up signal; after lease expiry the scheduler republishes it.
            self._queue.acknowledge(pointer)
            with suppress(Exception):
                self._release_lease(pointer)
            raise
        except Exception as error:
            self._queue.acknowledge(pointer)
            # Bookkeeping must not replace the diagnosis: if the database is the
            # thing that is down, recording the attempt fails too, and the
            # original error is still what the operator needs to see.
            with suppress(Exception):
                self._record_failed_attempt(pointer, error)
            raise
        self._queue.acknowledge(pointer)
        return result

    def process_one(self, *, block_ms: int = 1000) -> PipelineResult | None:
        pointer = self._queue.read_one(consumer=self._worker_id, block_ms=block_ms)
        if pointer is None:
            return None
        return self.process_pointer(pointer)


def _segment_pace_ms(segment: Mapping[str, Any]) -> int:
    """这一段发完要停多久 —— 按它自己的字数算，夹在上下限之间。

    素材引用那种段**按 ref 的长度算不合适**（ref 短、正文长），
    所以它一律取下限：客户读到的是商家那段原文，而平台这里手上只有 ref。
    """

    if str(segment.get("type")) != TEXT_CONTENT_TYPE:
        return SEGMENT_PACE_FLOOR_MS
    characters = len(str(segment.get("content_or_ref") or ""))
    paced = characters * SEGMENT_PACE_MS_PER_CHAR
    return max(SEGMENT_PACE_FLOOR_MS, min(SEGMENT_PACE_CEILING_MS, paced))


#: 同一个提案的几段之间，`ordinal` 的步长。
#:
#: `ordinal` 从 `044` 起就是「这条消息在这一轮里的位置」，而分段之后它还要
#: 多担一件事：**同一个 `intent_id` 的几行靠它区分**（迁移 `076` 的唯一约束）。
#: 于是它成了「提案位置 × 步长 + 段位置」。
#:
#: **步长必须大于任何一条消息的段数上限**，否则两条提案的段会撞号 ——
#: 契约给 `customer_visible_segments` 的上界是 `maxItems: 8`，
#: 而计划候选上界是 `maxItems: 4`，所以 100 有充足余量。
#: `test_one_judgement_many_rows.py` 把这条关系钉住：
#: **契约那两个数一变，它就红。**
SEGMENT_ORDINAL_STRIDE = 100


def _segments_to_queue(
    command: Mapping[str, Any],
    *,
    proposal: Mapping[str, Any] | None,
    materials: Mapping[str, str] | None,
    basis_key: str | None,
) -> list[tuple[dict[str, Any], str, int]]:
    """把一条**已经批准**的消息按模型给的切点铺成几行（ADR-0152）。

    返回 `(命令, 幂等键, 停顿毫秒)`，顺序就是出站顺序。

    ## ⚠️ 它与 ADR-0151 那版的区别，是这一批的全部要害

    那一版叫 `_expand_segments`，跑在**策略链之前** —— 于是**每一段各自
    过了一遍全部 policy**。用户 2026-09-23 裁定取消那件事：

    > 「一次生成应该是一次判定 而不是分段之后还要判定
    >   分段前就判断好能不能发 **拆分后语义语境可能完全不同**」

    一个片段单看的判定结论，可能与看整条消息完全不同。
    所以判定读的是整条（分段还在提案里），**而拆是投递的事**。

    ## 平台仍然不选切点

    切点是模型在 `content.segments` 里给的。这一点 ADR-0151 就是对的，
    本条一个字没改 —— 变的只是**什么时候拆**。

    ## 不拆的三种情形，原样返回一行

    * 提案没带过来（`None`）—— 不猜；
    * `content` 不是 `{"segments": [...]}` 那种形状 —— 里面没有模型给的切点；
    * 只有一段 —— 拆与不拆是同一件事，而不拆的路径 `pace_ms` 保持 0。
    """

    payload = proposal.get("typed_payload") if isinstance(proposal, Mapping) else None
    content = payload.get("content") if isinstance(payload, Mapping) else None
    segments = content.get("segments") if isinstance(content, Mapping) else None
    key = str(basis_key)
    if not isinstance(segments, list) or len(segments) < 2:
        return [(dict(command), key, 0)]

    queued: list[tuple[dict[str, Any], str, int]] = []
    for index, segment in enumerate(segments):
        one = deepcopy(dict(command))
        #: **逐段渲染，用与判定时同一份 `materials`。** 渲染出来的形状与
        #: 不分段那条路逐字相同（`_send_message_content`），
        #: 所以渠道那一侧一个字都不用改。
        one["content"] = {
            "type": TEXT_CONTENT_TYPE,
            "text": customer_text({"segments": [deepcopy(segment)]}, materials=materials),
        }
        #: 最后一段不留停顿 —— 它后面没有下一段（ADR-0151）。
        pace = 0 if index == len(segments) - 1 else _segment_pace_ms(segment)
        suffix = "" if index == 0 else f"{SEGMENT_BASIS_INFIX}{index}"
        queued.append((one, f"{key}{suffix}", pace))
    return queued


def _delivery_account(evidence: dict[str, Any]) -> dict[str, Any]:
    """Flatten a delivery receipt's evidence into what a person needs to read.

    Two shapes reach here, and this function exists because the merge of L1's
    re-review fixes silently broke the first version of it. A receipt that never
    went through a reconcile carries the adapter's flat evidence -- today that is
    the **dead-letter sweep**, which builds a task from the last attempt's
    receipt without querying anything. (The first version of this docstring said
    "the terminal `failed` path"; there was no such call site, because terminal
    `failed` produced no task at all -- which was the review's P1-1, now fixed.
    A docstring naming a call site that does not exist sends the next reader
    looking for it: independent review 2026-09-04, P3-2.) A receipt that did go
    through a reconcile carries L1's two namespaces, `send` and `reconcile`
    (L1 review P2-1 -- `reason` and `http_status` are written by both round
    trips, and a flat merge either erases one or mislabels the other).

    The old code read `evidence["http_status"]` unconditionally. After the
    namespaces landed that key is simply absent on the reconciled path, so every
    UNKNOWN manual task would have carried `adapter: null, http_status: null,
    reason: null` -- and nothing would have failed, because a proposal is JSON
    and JSON takes nulls. The person picking up the task would have got a form
    with the three fields they need left blank.

    So both halves are reported under their own names, and `send` alone is
    flattened when there was no query, because there is nothing to distinguish
    it from.
    """

    if "send" not in evidence:
        return {
            "adapter": evidence.get("adapter"),
            "http_status": evidence.get("http_status"),
            "reason": evidence.get("reason"),
            "reconcile_attempted": False,
        }
    send = evidence.get("send") or {}
    reconcile = evidence.get("reconcile") or {}
    account: dict[str, Any] = {
        "adapter": send.get("adapter") or reconcile.get("adapter"),
        "send_http_status": send.get("http_status"),
        "send_reason": send.get("reason"),
        "reconcile_http_status": reconcile.get("http_status"),
        "reconcile_reason": reconcile.get("reason"),
        "reconcile_attempted": bool(evidence.get("reconcile_attempted")),
    }
    if not account["reconcile_attempted"]:
        # The field L1 added so that "we could not even ask" stops reading as
        # "we asked and learned nothing". It is the first thing the person
        # picking this up needs, so it travels with the task.
        account["reconcile_skipped_reason"] = evidence.get("reconcile_skipped_reason")
    return account


#: **The rate gate** (T108, ADR-0095 §五, user ruling 2026-09-07).
#:
#: Three layers, and only two of them are here: the per-event ceiling of 4 is
#: the generation contract's `proposed_intents.maxItems` and needs no runtime
#: check. These two are per minute.
#:
#: **Both numbers are the user's and both are marked for recalibration** against
#: the first real merchant's rhythm. They are deliberately conservative: being
#: over the limit costs manual work, not a lost message. The ADR's reason for
#: shipping a guessed number rather than none is worth keeping next to the
#: constants -- *without* a gate, "the ceiling went from 1 to 4" becomes the
#: baseline the next person widens from.
#:
#: ## ⚠️ 2026-09-19（T368）：会话这一边改成**数轮次**，`3` → `5`
#:
#: **用户当天裁的口径，原话**：
#:
#: > **速率闸是用来防止骚扰而不是正常对话。**
#:
#: 在此之前它数的是**条数**，而一轮可以发好几条
#: （批 G 把「许诺一条 + 紧跟兑现一条」变成了有意为之的形状）。
#: 于是这通对话一分钟内已发过 2 条时 `room = 1`，
#: **许诺那条留下、兑现那条被扣住** —— 客户看到一句光秃秃的许诺，
#: 而批 F 那道闸在生成时已经放行（那一刻两条都在），
#: **没有任何东西会在劈开的那一刻变红**。
#:
#: 一轮里的两条是**一次正常回复的两个气泡**，不该被一个防骚扰的闸切开。
#: 所以会话这一边：**单位是轮，且一轮是原子的** —— 要么都发、要么都不发。
#: 数目由用户 2026-09-19 裁定为 **5 轮/分钟**。
#:
#: ⚠️ **租户那一边仍然数条数，这是有意的**：它是**容量**闸，不是针对某个客户的
#: 防骚扰闸。**而它也是「劈开」今天唯一还会发生的地方** —— 用户同一句裁定的
#: 后半「超额的走批 F 丢弃」挂在它上面才有可达路径。
#: 两边都改成原子的话，那条丢弃就是一条**够不着的分支**，
#: 而「够不着的分支看起来像闸而什么都没守」是本仓点过名的形状。
RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE = 5
RATE_LIMIT_TENANT_PER_MINUTE = 60

#: 「一轮」在 `outbox_messages` 上怎么认出来。
#:
#: **出站行的 `idempotency_key` 是 `{prefix}:{event_id}:{candidate_basis}`**
#: （`_persist_accepted_proposal` 那一处是唯一的构造点），
#: 而 `event_id` 是**这一轮那条源事件** —— 所以一轮的每一行共享中间那一段。
#: `split_part(..., ':', 2)` 取的就是它。
#:
#: **为什么不用 `created_at`**：一轮写的行确实共享事务时间戳（T109 / 迁移 044
#: 那段注释就靠这个性质），**而两个不同的事务可能撞到同一微秒** ——
#: 那会把两轮看成一轮，方向是**放更多消息出去**（fail-open）。
#: 本仓为「严的那一半站在执行侧」付过账（那次六行窗口只数了三行）。
#: **`event_id` 是精确的，而且今天就已经存在，不需要迁移。**
#:
#: **为什么不加一列**：`outbox_messages` 上没有轮次列，加一个要迁移 +
#: 回填语义（老行没有它）；而这把键已经在表里了。
#: ⚠️ **它靠两件事**：三个前缀都不含冒号，且客户可见的出站行只有那三条写入方
#: （管理员通知与摘要是 `tenant_manager`，被 `AUDIENCE_IS_CUSTOMER_SQL` 滤掉）。
#: **两件都由用例钉住** —— 多一个前缀带冒号、或多一个客户行写入方用别的键形状，
#: 都会让多轮**安静地**塌成一轮。
#: 一次判定铺成的几段，投递结果各不相同时，**intent 的终态取最差的那一个**
#: （ADR-0152，用户 2026-09-23 裁定「按最差的那一段算」）。
#:
#: 一条消息只要有一段没到，那次判定就不算完成 —— 而「完成了」这个词
#: 在这张表上是有人读的（`reconcile_costs`、摘要、人工任务）。
#:
#: ⚠️ **数值只用来比大小，不进任何一列。** 它在 SQL 里以 `VALUES` 内联，
#: 而 `test_one_judgement_many_rows.py` 把两处钉成相等 ——
#: **两个定义点分家时，SQL 那份会安静地按旧次序比。**
INTENT_STATUS_SEVERITY = {"APPLIED": 1, "UNKNOWN": 2, "FAILED": 3}

TURN_KEY_SQL = "split_part(idempotency_key, ':', 2)"

#: 一段停多久，按这一段自己的字数算（ADR-0151，用户 2026-09-23
#: 「本来每段话之间就应该有延迟，**人打字还要时间的**」）。
#:
#: ⚠️ **不是去模拟真实打字速度。** 一个人在手机上敲 40 个字要三十秒，
#: 而一个隔三十秒才说下半句的机器人不是「像人」，是坏了。
#: 这三个数要的是**两个气泡不要同一瞬间落地**。
#:
#: `clamp(字数 × 60ms, 800ms, 4000ms)`：40 字的一段约 2.4 秒，
#: 一句「好的。」落到下限 800ms，而再长的段也不超过 4 秒。
#:
#: **三个数今天是平台的**。商家可配那一层**不做** ——
#: 零实现的概念不能作为本条的一部分（`CLAUDE.md` 第四节）。
SEGMENT_PACE_MS_PER_CHAR = 60
SEGMENT_PACE_FLOOR_MS = 800
SEGMENT_PACE_CEILING_MS = 4_000

#: 分出来的第二段起，幂等键在模型给的 `basis` 后面接这个再接序号。
#:
#: **键由平台铸，而不是模型**（ADR-0151 §二）：运维 2026-09-22 量到
#: `PLAN_CANDIDATE_BASIS_NOT_UNIQUE` **0 条出站**，成因是计划步被教了
#: 「装不下就列第二条」而没被教「两条的 basis 必须不同」，
#: 而 `idempotency_basis` 在提示词里 **0 命中**。
#: 一轮只有一条 `send_message` 之后，这个键由序号铸出来，**撞不了**。
#:
#: 形状与 `REPLY_FLOOR_BASIS_PREFIX` 同源：**平台补出来的东西要留得下痕**，
#: 「这一行是模型提的还是平台铸的」永远查得到。
SEGMENT_BASIS_INFIX = ":seg"

#: The window both limits are counted over.
RATE_LIMIT_WINDOW_SECONDS = 60

#: What a withheld message becomes. Classified `MERCHANT_ACTION` / `BATCHED` in
#: `notification.py`: a limiter that trips tends to trip repeatedly, and an
#: immediate notice is itself a message.
RATE_LIMITED_REASON = "RATE_LIMITED"


#: Which terminal delivery states put a manual task in front of a person.
#:
#: `unknown` -- L1 asked the provider and it could not say. `failed` -- the
#: provider looked at the send and refused it. They read differently to whoever
#: picks the task up, so they keep separate reason codes rather than collapsing
#: into "it did not go".
#:
#: A dict rather than a set so the reason code is decided here and not at the
#: call site: two call sites spelling the same state differently is how a digest
#: ends up counting one kind twice under two names.
DELIVERY_MANUAL_TASK_REASONS = {
    "unknown": "DELIVERY_UNCONFIRMED",
    "failed": "DELIVERY_FAILED",
}

#: The same two states **when the message was not for a customer** (user's
#: ruling 2026-09-08; B1-5 independent review P1-1 / P2-3).
#:
#: Kept as a second table rather than a suffix on the first, because the thing
#: that differs is not the state but the audience, and the audience is what
#: `classify()` needs in order to keep these out of the merchant's digest. The
#: long form of why is in `notification._POLICY` next to the two entries; the
#: short form is that a manager notice which fails becomes a merchant task,
#: which becomes the next digest, which fails -- a queue manufacturing work out
#: of its own backlog, and `_notify_manager`'s docstring named that shape long
#: before this batch walked into it.
NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS = {
    "unknown": "MANAGER_NOTICE_UNCONFIRMED",
    "failed": "MANAGER_NOTICE_UNDELIVERED",
}


#: **`is_customer_row`, said in SQL.** The same rule has to exist twice because
#: one caller holds the command in memory and the other counts rows in the
#: database, and the two drifted the first time they were written apart: the
#: rate gate (B1-6) used `command->>'audience' = 'customer'`, which is NULL --
#: and therefore not counted -- for the rows written before `35f4770` added the
#: key (independent review 2026-09-08, P2-2). Keeping the SQL half here, beside
#: the Python half, is the cheapest thing that makes the pair visible; anything
#: reading `outbox_messages` to ask "is this a customer message" uses this
#: constant rather than spelling the comparison again.
#:
#: **It stayed the audience half, and `PROVIDER_WAS_ASKED_SQL` is the other**
#: (B4-4a re-review 2026-09-09, P2-1). The api twin
#: (`app._OUTBOUND_IS_A_MESSAGE`) writes both halves as one expression because
#: that route asks a single question -- "is this on the transcript" -- while the
#: two callers here ask two: who is it for, and did we ever ask a provider to
#: send it. **The pair must still agree on the answer**, and the first version
#: of the `CANCELLED` fix changed only the api side; see the other constant.
#: 计划与生成谈不拢那一行的理由码。
#:
#: ⚠️ **这段话 2026-09-22 被一个真实回合证伪了**（运维缺陷 9）。原文是：
#:
#: > **它不是一次失败**：客户照样收到了回复。它是一条「我们内部两步意见不一，
#: > **而代价没有落在客户那一侧**」的记录。
#:
#: 后半句不成立。实测那一次（15:40:31Z）：计划列了
#: `send_material` / `remember` / `send_message`，而生成步**没产出
#: `send_material`**（`dropped_kinds` 是空的 —— 不是管线丢的）。
#: 于是客户收到的是一句「**我说说它具体怎么接**」，
#: **而讲解的那份资料永远不会来**。
#:
#: **准确的说法**：它记的是计划与生成谈不拢。
#: **生成多做了**一件计划没列的事时，代价落在我们这侧（那件事被丢掉）；
#: **生成少做了**一件计划列了的事时，**代价可以落在客户那一侧** ——
#: 尤其当少做的那件正是另一条消息许诺过的兑现物。
#:
#: 分桶数它，是为了看这一族有没有在涨；而**「少做」那一支要单独看**。
GENERATION_DISAGREED_REASON_CODE = "PLAN_GENERATION_DISAGREED"

#: 「这一轮把哪几条记忆递给了模型」那一行的理由码。
#:
#: ⚠️ **这两个常量补的是一对从上线到 2026-09-22 一行都没写进去过的留痕。**
#: 那两条 INSERT 把第 7 个位置（`reason_code`）写成了**字面 `NULL`**，
#: 而 `audit_events.reason_code` 是 `NOT NULL` —— 每一次执行都抛
#: `NotNullViolation`，而它们写在 `prepare` 的事务上，**整轮回滚**。
#:
#: **它藏了这么久，是因为外面那个 bug 一直遮着它**：触发条件要求联系人
#: 至少有一条 active 记忆，而 `contact_memories` 因为外键指错列
#: **全库 0 行贯穿了这套系统的一生**。迁移 `073` 把外键改对之后，
#: 运维实测 **91 秒**：15:40:31Z 第一次写入成功，15:42:02Z 下一个回合当场炸，
#: 之后任何有记忆的联系人每一轮都死在 `prepare` 里，重试五次进死信。
#:
#: **同族每一行都传一个具名常量**（`GENERATION_DISAGREED_REASON_CODE`、
#: `UNKEPT_PROMISE_REASON_CODE`、`intent.approve` 的 `POLICY_ALLOWED`）——
#: **只有这两行是字面量**，而字面量 `NULL` 与占位符 `%s` 在 SQL 文本里
#: 长得几乎一样，是这一族最容易漏看的那种。
MEMORIES_SENT_REASON_CODE = "MEMORIES_SENT"

#: 「那条回复引了哪几条记忆」那一行的理由码。与上一条是一对：
#: `pipeline` 里那句「两行相减才是『递过去而没被用上的』」要求两行都在。
MEMORIES_CITED_REASON_CODE = "MEMORIES_CITED"

AUDIENCE_IS_CUSTOMER_SQL = "coalesce(command->>'audience', 'customer') = 'customer'"

#: Whether a provider was ever asked to send this row.
#:
#: **`CANCELLED` is the only status where the answer is no** (B4-4a). Every
#: other one means a request went out or is about to: `PENDING`/`SENDING` will
#: be sent, `DELIVERED`/`UNKNOWN` were sent, and `FAILED` **was sent and
#: refused**. The pre-delivery proactivity check retires its row before the
#: adapter contract is even built -- the receipts already carry that distinction
#: as `request_sent: False`, and this is the same line drawn in SQL.
#:
#: **Why the rate gates care** (re-review P2-1; user's ruling 2026-09-09): a
#: rate limit measures the load we created, and a row nobody was asked to send
#: created none. Counting it spends one of the conversation's slots on a message
#: that never existed for the customer -- and when the slots run out, what B1-6
#: withholds is a **reply the customer is actually waiting for**, plus a manual
#: task for a person to send it by hand.
PROVIDER_WAS_ASKED_SQL = "status <> 'CANCELLED'"


def is_customer_row(command: dict[str, Any]) -> bool:
    """Whether this outbox row is a message to the customer in its conversation.

    **One reading of `audience` for three decisions** -- who the address is
    (`unroutable_reason`), which reason code a failure gets, and whether the
    conversation's `last_outbound_at` moved. They were three separate
    expressions until the second and third were found missing (independent
    review P1-1 / P1-2), which is the argument for them sharing one.

    The missing-key default is `customer` for the same reason it is there in
    `unroutable_reason`; see that docstring.
    """

    return str(command.get("audience", "customer")) == "customer"


#: The fields a non-customer row must carry before anything is sent (T90).
#: `012` makes the triple the identity of a delivery target and both readers use
#: it whole, so a row missing any one of them has no address at all.
RECIPIENT_FIELDS = ("channel", "config_id", "external_user_id")


def unroutable_reason(command: dict[str, Any]) -> str | None:
    """Why this row cannot be addressed, or `None` if it can.

    **The check exists so that the answer is never "send it to the
    conversation"** (T90, ADR-0095 §三). The manager digest borrows a
    conversation's id to satisfy a foreign key; an adapter that fell back to it
    would deliver a merchant's backlog summary into that merchant's customer's
    chat. The registered defect says the refusal must be **explicit and carry a
    reason code** -- a silent fallback is precisely the accident.

    Customer rows are addressed by their conversation and need no recipient.

    **The missing key is read as `customer`, and that is a decision rather than
    a default** (independent review P3-2). It is fail-*open*: a future producer
    that forgets `audience` gets its row sent to the conversation, which is the
    fallback this function exists to prevent. It is written this way because the
    key is younger than the rows: the customer producer only began writing
    `"audience": "customer"` in `35f4770` (L2a), so **every outbox row older
    than that commit carries no `audience` at all, and every one of them is a
    customer message**. Failing closed would refuse them by name. Both producers
    today write the field explicitly, and
    `tests/contracts/test_notice_routing.py` asserts this reading, so changing
    it is a decision too rather than a default quietly moving.
    """

    if is_customer_row(command):
        return None
    recipient = command.get("recipient")
    if not isinstance(recipient, dict):
        return "missing"
    missing = [field for field in RECIPIENT_FIELDS if not str(recipient.get(field) or "").strip()]
    if missing:
        return f"incomplete:{','.join(missing)}"
    return None


#: The one key `command` carries for ProactivityPolicy in this batch, and the
#: only thing the dispatcher reads out of it (ADR-0098 §一).
#:
#: **Two keys since B4-4b**, and the second arrived with its reader.
#: `window_expires_at` is what the *dispatcher* compares against its clock --
#: 「写进行里的不是分类结果，是一个到期时刻」 (ADR-0098 §一). `classification` is
#: what the *pipeline* counts in `_reengagement_gate`; the dispatcher never looks
#: at it. B4-4a deliberately shipped without it, because in that batch nothing
#: read it (ADR-0117 §四).
PROACTIVITY_COMMAND_KEY = "proactivity"

#: **`is_re_engagement`, said in SQL** -- the same pairing `AUDIENCE_IS_CUSTOMER_SQL`
#: exists for, and for the same reason: one caller holds the command in memory
#: and the other counts rows in the database, and a rule spelled twice drifts.
#: B1-6 is the precedent -- `command->>'audience' = 'customer'` was NULL for rows
#: written before the key existed, so the gate skipped them.
#:
#: **A row with no `proactivity` block is not a re-engagement**, and here that is
#: a fact rather than a default: only the proactive producer writes the block, so
#: a row without it is a reply to a customer message, which RFC §6.4 does not
#: count against anything.
#:
#: **Two shapes of row predate this key, and only one of them is correctly
#: excluded** (independent review 2026-09-09, P3-2). A B4-4a row that carries
#: the block **without** `classification` is an in-session continuation by
#: construction -- B4-4a wrote the block only when there was a window to close,
#: and only that class has one -- so not counting it is right. A B4-4a
#: **re-engagement** carries no block at all and is **also** not counted, and
#: that one is a miss rather than an exemption. **The window is zero when the
#: two batches enter an environment together**, which is how they are queued;
#: it is not zero if B4-4a reaches Stage or Production alone, so **they must be
#: promoted together**.
RE_ENGAGEMENT_SQL = (
    f"command -> '{PROACTIVITY_COMMAND_KEY}' ->> 'classification' = '{RE_ENGAGEMENT}'"
)

#: Which outbox statuses occupy a re-engagement slot.
#:
#: RFC §6.4 says 「计数以实际 delivered 的 re-engagement 为准，**UNKNOWN 在 reconcile
#: 前占用名额以防重复发送**」. **The stated principle is what is applied here**: a
#: row that *may have reached the customer* takes a slot. `PENDING` and `SENDING`
#: are strictly more likely to reach them than `UNKNOWN` is, so excluding those
#: two while including `UNKNOWN` would contradict the reason the sentence gives
#: for including it.
#:
#: **This is an extension of the RFC's principle beyond its literal list, and it
#: is flagged as such in the self-review.** `FAILED` and `CANCELLED` are
#: excluded: the customer did not get those, and holding a slot for them would
#: silence a merchant over a message nobody received.
REENGAGEMENT_OCCUPYING_STATUSES = ("PENDING", "SENDING", "DELIVERED", "UNKNOWN")


def proactivity_deadline(command: dict[str, Any]) -> datetime | None:
    """The instant after which this row must not be sent, or `None`.

    **The whole of the dispatcher's `pre_delivery` check** (ADR-0098 §一): one
    field, one comparison, no profile read, no contact read, no quota read.
    「到点作废是一次纯本地判断。」

    `None` for every row that is not a proactive send. **Since B4-5 every
    proactive row has one** (independent review 2026-09-09, P3-1): `quiet_hours`
    is a required part of the contract, so a quiet period always opens ahead of
    the row, and the deadline is the earlier of that and the active window
    closing. This used to say 「a re-engagement has no window to close」 and B4-5
    is the batch that made that false -- the re-engagement is exactly the class
    that now gets its first deadline.
    """

    block = command.get(PROACTIVITY_COMMAND_KEY)
    if not isinstance(block, dict):
        return None
    raw = block.get("window_expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A malformed instant is treated as absent rather than as expired: this
        # producer writes the field with `_iso`, so a value that does not parse
        # means something else wrote the row, and cancelling a customer's
        # message on a string nobody can read is worse than sending it.
        return None


def proactivity_window_lifts_at(command: dict[str, Any]) -> datetime | None:
    """When the window that expired this row lifts, or `None`.

    **Written by the producer so the dispatcher never reads a profile** -- the
    same rule `proactivity_deadline` follows, and for the same reason
    (ADR-0098 §一). Non-`None` only when the deadline came from quiet hours: an
    active window closing has no end to name, because past it the message is a
    different class rather than a delayed one.

    A malformed instant is read as absent, exactly as above: the alternative is
    writing a string nobody can read into a column whose whole purpose is to be
    queried.
    """

    block = command.get(PROACTIVITY_COMMAND_KEY)
    if not isinstance(block, dict):
        return None
    raw = block.get("window_lifts_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class OutboxDispatcher:
    """Lease and send committed outbox facts without holding a database transaction open."""

    def __init__(
        self,
        *,
        database: Database,
        channel: ChannelAdapter,
        dispatcher_id: str,
        channel_factory: ChannelFactory | None = None,
    ) -> None:
        self._database = database
        self._channel = channel
        self._dispatcher_id = dispatcher_id
        # **`None` means "keep sending through the one adapter"** -- the M0
        # simulator, whose address is a URL and needs no per-row resolution.
        # When a factory *is* supplied, every send resolves its own address
        # first and builds its own adapter.
        #
        # **The reconcile that follows a failed send uses that same adapter, not
        # `self._channel`.** Asking the simulator what happened to a message
        # WeComProxy was asked to deliver would be a confident answer about the
        # wrong system -- and `_reconcile_before_giving_up` exists precisely to
        # stop "we do not know" being written down without asking. So the
        # adapter is threaded through rather than looked up again there.
        self._channel_factory = channel_factory

    def active_tenant_ids(self) -> list[UUID]:
        """The same list the dispatcher walks, exposed for the hourly digest.

        Public rather than copied: "which tenants are active" is one question,
        and a second implementation of it is a second thing to keep in step.
        """

        return self._tenant_ids()

    def _tenant_ids(self) -> list[UUID]:
        with self._database.transaction(tenant_id=None) as connection:
            rows = connection.execute("SELECT id FROM list_active_tenant_ids()").fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def _dead_letter_exhausted(self, connection: Any, tenant_id: UUID) -> None:
        """Retire outbox rows whose lease has lapsed once too often.

        The lease already returns a crashed send to the queue; without a budget
        it returns it forever. A retired row keeps its receipts and audit trail,
        so the delivery is recorded as failed rather than silently abandoned.
        """

        exhausted = connection.execute(
            """
            UPDATE outbox_messages
               SET status = 'FAILED', lease_owner = NULL, lease_expires_at = NULL,
                   updated_at = now()
             WHERE tenant_id = %s AND status = 'SENDING'
               AND lease_expires_at < now() AND attempt >= %s
            RETURNING id, intent_id, conversation_id, command
            """,
            (tenant_id, MAX_OUTBOX_ATTEMPTS),
        ).fetchall()
        for row in exhausted:
            self._manual_task_for_delivery(
                connection,
                tenant_id=tenant_id,
                # The column, not the JSON copy of it. `command` is a payload
                # the dispatcher hands to a channel; `conversation_id` is a
                # column on this very row, with a foreign key behind it. Reading
                # the copy means a command written without that key -- or with a
                # stale one -- silently attaches the manual task to the wrong
                # conversation, and nothing would ever disagree with it
                # (independent review 2026-09-04, P3-1).
                conversation_id=UUID(str(row["conversation_id"])),
                intent_id=UUID(str(row["intent_id"])),
                outbox_id=UUID(str(row["id"])),
                reason_code="DELIVERY_ATTEMPTS_EXHAUSTED",
                evidence={"adapter": "http", "reason": "attempts_exhausted"},
            )
            connection.execute(
                """
                UPDATE intent_records
                   SET status = 'FAILED', unknown_reason = 'DELIVERY_ATTEMPTS_EXHAUSTED',
                       lease_expires_at = NULL, updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'EXECUTING'
                """,
                (tenant_id, row["intent_id"]),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'intent.execute', 'intent_record', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    row["intent_id"],
                    UUID(str(row["command"]["correlation_id"])),
                    "DELIVERY_ATTEMPTS_EXHAUSTED",
                    Jsonb({"outbox_id": str(row["id"]), "attempts": MAX_OUTBOX_ATTEMPTS}),
                ),
            )

    def _reconcile_before_giving_up(
        self,
        *,
        receipt: SendReceipt,
        idempotency_key: str,
        deadline_at: datetime,
        lease_deadline_at: datetime,
        adapter: ChannelAdapter,
    ) -> SendReceipt:
        """Ask the provider what happened, before anyone writes down "unknown".

        **L1.** RFC `:228` -- "timeout 后先 reconcile；无法确认则 UNKNOWN + manual
        task，禁止盲发" -- and ADR-0006 item 4 put this in M0's scope. The
        function to do it (`ChannelAdapter.reconcile`) shipped in the same
        commit as `DELIVERY_UNCONFIRMED` and then had **no production caller at
        all**: a 5xx or a timeout went straight to UNKNOWN. Same conclusion,
        often -- but the step that was skipped is the only one that can turn "we
        do not know" into "we know", and skipping it silently is what made the
        gap invisible for a milestone.

        Which query is used depends on what the failed send left behind:

        * an operation id (the provider answered, unreadably) -> `reconcile`;
        * nothing at all (timeout, transport error, 5xx) -> `reconcile_by_key`,
          because the idempotency key is the only identifier both sides share.
          Without that second door this rule would be unreachable exactly where
          it matters most, which is how it stayed unimplemented;
        * nothing at all, **from a channel that has declared it cannot be asked
          by key** -> no query, and a receipt that says so (T78 / ADR-0094 §三).

        That third case is the one this method could not express until B2-4.
        **What changes for today's only such channel is the record, not the
        traffic** (independent review 2026-09-09, P2-1): WeComProxy's
        `reconcile_by_key` never sent a packet either -- it returned
        `_cannot_ask` immediately -- so nothing is saved here but a local call
        and a `reconcile` namespace describing a round trip that never
        happened. The gate is a general mechanism, and it will save a real
        round trip for the first adapter whose `reconcile_by_key` does go to
        the wire while declaring it cannot answer usefully. **The value of the
        batch is not the saving**: ADR-0094 §三 rejected "leave it as is"
        precisely because the evidence side was already correct, and what was
        missing was the declaration.

        A reconcile that still cannot tell returns `unknown` -- and now says so
        with evidence, so that "asked and it does not know" stops looking like
        "never asked". That distinction is what the manual-task half (L2b)
        will be triggered by.
        """

        # **Before any of the clock arithmetic: can this channel be asked at
        # all?** (T78 / ADR-0094 §三.) The by-key query is the only door open
        # when the send left no operation id, and a real provider mostly does
        # not offer it -- WeComProxy's permitted surface has no message-status
        # operation of any kind. Calling anyway buys a receipt that says
        # `unknown`, which is the same word as "we asked and it has no record".
        #
        # **Today that costs a local call, not a round trip** (independent
        # review 2026-09-09, P2-1). The one adapter declaring `False` returns
        # `_cannot_ask` without touching the network, so what this branch
        # removes from the record is the `reconcile` namespace -- a description
        # of a second round trip that never happened. Stated plainly here
        # because the opposite claim is easy to write and was written once: the
        # saving is real only for a future adapter that would have gone to the
        # wire to be told nothing.
        #
        # The declaration is consulted **first**, ahead of the lease clamp
        # below, because the two reasons are not equally durable: a lease with
        # no room left is a fact about this attempt, while "this channel cannot
        # be queried by key" is a fact about the channel and will be just as
        # true on the retry. Writing the narrower reason over the permanent one
        # would tell whoever picks up the manual task to try again.
        #
        # Only the by-key door is gated. `reconcile` takes an id the provider
        # itself issued, and ADR-0094 §三 revised the mandatoriness of
        # `reconcile_by_key` alone; an adapter that answers neither still says
        # so through its own `_cannot_ask` receipt, one call later -- and for
        # WeComProxy that door is unreachable in production anyway, because its
        # `send` leaves `provider_operation_id` `None` on every failure path.
        if receipt.provider_operation_id is None and not adapter.capabilities().reconcile_by_key:
            return SendReceipt(
                status=receipt.status,
                provider_operation_id=receipt.provider_operation_id,
                evidence={
                    "send": dict(receipt.evidence),
                    # No `reconcile` namespace: there was no second round trip
                    # to describe. `reconcile_attempted` is False for the same
                    # reason it is False when the lease has no room -- P0-1's
                    # rule is that nobody writes "we asked" unless a packet went
                    # out -- and the reason names the channel, not the attempt.
                    "reconcile_attempted": False,
                    "reconcile_skipped_reason": "channel_cannot_be_queried_by_key",
                },
            )
        # The reconcile gets its **own** clock. `deadline_at` is the turn budget
        # the send just spent -- on a real timeout it spends all of it -- so
        # handing the query what is left of it meant the query never went out in
        # exactly the case RFC `:228` names (review 2026-09-04, P1-1, proved with
        # a real socket: the server saw only the POST). Past the send deadline
        # the reply is late no matter what; the reconcile is not answering the
        # customer, it is deciding what gets written down.
        #
        # Its own clock, but not an unbounded one: the query has to be finished,
        # and the bookkeeping written, before the lease on this row expires. On
        # the timeout path the send has already spent nearly the whole lease, so
        # an unclamped two seconds is exactly what carries this dispatcher past
        # expiry and lets a second one claim the row and send the message again
        # (narrow re-review 2026-09-04). The lease is the ceiling;
        # `RECONCILE_BUDGET_SECONDS` is the wish. See `OUTBOX_LEASE_SECONDS`.
        # **Both terms are on this process's clock** (T183). `lease_deadline_at`
        # was derived in `_claim_one` from a difference measured entirely on the
        # database's clock and then anchored here. What used to sit in this
        # expression was the database's absolute `lease_expires_at`, which made
        # the comparison a subtraction across two clocks -- correct only while
        # they agreed.
        now = datetime.now(deadline_at.tzinfo)
        reconcile_deadline_at = min(
            now + timedelta(seconds=RECONCILE_BUDGET_SECONDS),
            lease_deadline_at - timedelta(seconds=LEASE_SAFETY_MARGIN_SECONDS),
        )
        if reconcile_deadline_at <= now:
            # No room inside the lease to ask anything. Say that in its own words
            # instead of borrowing the adapter's
            # `deadline_exceeded_before_reconcile`, which means the *send's*
            # clock ran out; this one means we still hold the row, just not for
            # long enough to use it. `reconcile_attempted` stays False either
            # way -- P0-1's rule is that nobody writes "we asked" unless a packet
            # went out, and here not even the call is made.
            return SendReceipt(
                status=receipt.status,
                provider_operation_id=receipt.provider_operation_id,
                evidence={
                    "send": dict(receipt.evidence),
                    "reconcile_attempted": False,
                    "reconcile_skipped_reason": "lease_expiring_before_reconcile",
                },
            )
        # **The adapter that sent it, not `self._channel`** (B2-3). Once a
        # factory is wired, the send goes through a per-dispatch adapter; asking
        # the M0 simulator what became of a message WeComProxy was asked to
        # deliver would be a confident answer about the wrong system -- and this
        # method exists precisely so that "we do not know" is never written down
        # without asking the party that would know.
        if receipt.provider_operation_id is not None:
            resolved = adapter.reconcile(
                provider_operation_id=receipt.provider_operation_id,
                deadline_at=reconcile_deadline_at,
            )
        else:
            resolved = adapter.reconcile_by_key(
                idempotency_key=idempotency_key, deadline_at=reconcile_deadline_at
            )
        # Two round trips, two namespaces. The first version merged both
        # evidence dicts into one, where `reason` and `http_status` collide:
        # when both sides wrote them the send's was erased ("this message was
        # never sent at all" vanished from the record), and when only the send
        # wrote them the reader had no way to tell they described the send
        # rather than the query -- an APPLIED receipt carrying `http_status:
        # 500` reads as though the query returned 500 (review 2026-09-04, P2-1).
        # A dict merge cannot express two different round trips; nesting can.
        evidence: dict[str, Any] = {
            "send": dict(receipt.evidence),
            "reconcile": dict(resolved.evidence),
            # **Only true when a query actually went out.** The adapter says so;
            # the dispatcher does not assume. Writing an unconditional `True`
            # here meant a message whose send never left the process still got a
            # receipt claiming the provider had been asked (P0-1) -- and L2b puts
            # this very field in front of the person who picks up the manual
            # task, so the lie would have been read by a human.
            "reconcile_attempted": bool(resolved.evidence.get("query_sent")),
        }
        if not evidence["reconcile_attempted"]:
            # The third state, named rather than hidden inside a `False`: "we
            # could not even ask" is not "we asked and learned nothing".
            evidence["reconcile_skipped_reason"] = str(
                resolved.evidence.get("reason", "unspecified")
            )
        # `send_status_before_reconcile` used to live here. It was a constant:
        # the only call site is guarded by `receipt.status == "unknown"`, so the
        # field could never hold anything else (P2-2). What it claimed to answer
        # -- "was this receipt reached through a reconcile" -- is what
        # `reconcile_attempted` answers, and what was actually missing is the two
        # namespaces above.
        return SendReceipt(
            status=resolved.status,
            provider_operation_id=resolved.provider_operation_id
            or receipt.provider_operation_id,
            evidence=evidence,
        )

    def _schedule_from_outbound(
        self, connection: Any, *, tenant_id: UUID, conversation_id: UUID, stamped: Any
    ) -> None:
        """Create the `last_outbound_at` rules for the message just delivered.

        **B4-2.** The dispatcher is not a transition and does not pretend to be
        one: `created_by_transition_id` is **the last transition on the aggregate
        at the moment of delivery**. With no interleaving that is the transition
        which planned this message; **with interleaving it is not** -- if the
        customer speaks again between planning and delivery, that newer
        transition is already stamped on the aggregate and is what gets written
        here (independent review 2026-09-09, P2-1, correcting an earlier claim
        that this was always the planning transition).

        **The value cannot be made exact today**: neither `outbox_messages` nor
        `intent_records` carries a transition id or a generation, so the planning
        transition is not recoverable at this point. Giving them one is another
        batch (registered as **T227**).

        **It is still enough for what ADR-0096 §四.3 asks the field to prove** --
        that this row was created by a *transition* rather than proposed by a
        model -- and the row is internally consistent: `aggregate_generation`,
        `target_frame_id` and `target_stack_version` are read from the same
        as-of-delivery snapshot, so the four fields describe one moment rather
        than two.

        Reads the profile release pinned on the aggregate rather than any
        current profile: a rule must be evaluated against the release the
        conversation is pinned to, or a mid-conversation publish would silently
        change a customer's timers.
        """

        if stamped is None:
            # The aggregate vanished under us. Nothing to anchor to and nothing
            # to schedule; the delivery record above is still written.
            return
        aggregate = stamped["state"]
        if not isinstance(aggregate, dict):
            return
        profile_row = connection.execute(
            "SELECT profile FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, stamped["profile_release_id"]),
        ).fetchone()
        if profile_row is None:
            # The pinned release is gone. The delivery stands; the timer does
            # not get invented from some other profile.
            return
        last_transition = aggregate.get("last_transition") or {}
        transition_id = last_transition.get("transition_id")
        if transition_id is None:
            return
        try:
            drafts = drafts_for_anchor(
                profile=profile_row["profile"],
                aggregate=aggregate,
                anchor="last_outbound_at",
                anchor_at=stamped["last_outbound_at"],
                now=datetime.now(UTC),
            )
        except TimeRuleError as error:
            # **A profile fault must not undo a delivery that already happened**
            # (independent re-review 2026-09-09, P1-1). This runs in the
            # accounting transaction, *after* `adapter.send()`: an exception here
            # rolls back the receipt, the `DELIVERED` status and the intent, the
            # row returns to `SENDING`, and the next claim sends the same message
            # to the customer again -- up to `MAX_OUTBOX_ATTEMPTS` times.
            #
            # So the timer is dropped and the fault is recorded. `load_package`
            # refuses such a profile at publish time, which is where this is
            # supposed to be caught; this branch is what keeps the cost of
            # getting that wrong away from the customer.
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'scheduled_event.skipped',
                          'conversation_aggregate', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    conversation_id,
                    uuid4(),
                    "TIME_RULE_REFUSED",
                    Jsonb({"error": type(error).__name__, "detail": str(error)}),
                ),
            )
            return
        _persist_scheduled_events(
            connection,
            tenant_id=tenant_id,
            # **The row's key, not the copy inside `state`** (independent review
            # 2026-09-09, P3-3). The two are necessarily equal today, but one of
            # them is the value the `UPDATE` above used to find this row and the
            # other is a duplicate inside a JSON document -- and this batch's own
            # argument for keeping `strategy` out of `payload_ref` is that two
            # copies can end up disagreeing.
            conversation_id=conversation_id,
            profile_release_id=UUID(str(stamped["profile_release_id"])),
            aggregate_generation=int(stamped["generation"]),
            target_stack_version=int(stamped["state_version"]),
            transition_id=UUID(str(transition_id)),
            drafts=drafts,
        )

    def _manual_task_for_delivery(
        self,
        connection: Any,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        intent_id: UUID,
        outbox_id: UUID,
        reason_code: str,
        evidence: dict[str, Any],
    ) -> UUID | None:
        """One manual task for one delivery that ended without a confirmation.

        **L2b, the other half of ADR-0006 item 4.** L1 made the platform *ask*
        before writing `unknown`; this is the half that makes somebody
        responsible for the answer. Until now a delivery could end `UNKNOWN` or
        `FAILED` and produce nothing at all -- the terminal state existed and
        nobody owned it (registered as L2: "有终态、没人看得见").

        Classified through the same table as every other manual task, so the
        merchant hears about these two kinds **in an hourly digest** rather than
        one notification per stuck message (ruling, 2026-09-04). The digest is
        what reads `digested_at`; this function only creates the task.

        Idempotent by construction: the idempotency key names the outbox row and
        the reason, so a re-run of the same terminal transition writes nothing.
        `ON CONFLICT DO NOTHING` rather than a pre-check, because two dispatchers
        may reach the same row's deadline at once.
        """

        classification = classify(reason_code)
        manual_task_id = uuid4()
        # **这一处此前把帧号取在 INSERT 里面**，写成
        # `INSERT … SELECT …, i.target_frame_id, … FROM intent_records i
        #  WHERE i.tenant_id = %s AND i.id = %s` —— 合并成一个定义点时它变成
        # 先读后写两步。**两者等价，而等价靠的是一条外键**：
        # `outbox_messages.intent_id` 引用 `intent_records(tenant_id, id)`
        # （`001_m0.sql`），所以这一行一定在，`SELECT` 那个形状也就从来不会
        # 因为「找不到那条意图」而写不进去 —— 它返回 `None` 的唯一原因是
        # `ON CONFLICT`，而那一条合并之后由 `_file_a_manual_task` 统一回答。
        # ⚠️ **外键要是哪天没了，这里就要重新判一次**：那时「读不到」
        # 会变成一种真实情形，而下面这个 `if` 正是为它留的。
        frame = connection.execute(
            """
            SELECT target_frame_id FROM intent_records
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, intent_id),
        ).fetchone()
        if frame is None:
            return None
        filed = _file_a_manual_task(
            connection,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            manual_task_id=manual_task_id,
            reason_code=reason_code,
            classification=classification,
            source="delivery",
            proposal_facts={
                "outbox_id": str(outbox_id),
                # The receipt vocabulary, not the body: whoever picks
                # this up needs to know whether the provider was asked.
                **_delivery_account(evidence),
            },
            proposed_by_version="delivery-manual-task-v1",
            strategy_id="handoff",
            target_frame_id=frame["target_frame_id"],
            idempotency_key=f"manual_task:{reason_code}:{outbox_id}",
            preconditions={"outbox_id": str(outbox_id), "intent_id": str(intent_id)},
            policy_evidence=[],
            # Which message stalled, so the person picking this up has the
            # thread without re-deriving it from the intent's proposal.
            evidence={"outbox_id": str(outbox_id), "intent_id": str(intent_id)},
        )
        return filed

    def _claim_one(self) -> tuple[UUID, dict[str, Any]] | None:
        for tenant_id in self._tenant_ids():
            with self._database.transaction(tenant_id=tenant_id) as connection:
                self._dead_letter_exhausted(connection, tenant_id)
                # **The M1a no-send boundary used to be enforced here, and B1-5
                # is the batch that removed it** (independent re-review
                # 2026-09-08, P3). What this paragraph said until then was that
                # the filter is positive on the audience the command names, that
                # a row for anyone but the customer is never claimed, and that
                # deleting the predicate would one day be a deliberate act with
                # a test attached. All three were true and none of them is now.
                #
                # **Why it was here**: the manager notification goes into this
                # same table on purpose (one delivery path, not two -- user's
                # ruling 2026-09-04) and M1a delivered nothing, that ruling
                # included. A sentence in a document would not have held -- this
                # loop is alive, and the first notification would have gone out
                # on the next tick.
                #
                # **Why it could go**: `_notify_manager` listed three conditions
                # for removing it and B1-5 met all three -- route by
                # `command.recipient` (T90), typed `content`, a one-day
                # deadline. What replaces it is not another predicate here but
                # two checks further down the path: `unroutable_reason` refuses
                # a non-customer row that carries no address **before any
                # request is built**, and `is_customer_row` decides which reason
                # code its failure gets and whether the conversation's
                # `last_outbound_at` moved.
                #
                # The deliberate act came with tests, three of them:
                # `test_a_manager_notice_is_claimed_and_carries_its_recipient`,
                # `test_a_notice_with_no_recipient_is_refused_and_never_sent`,
                # `test_a_digest_that_cannot_be_delivered_does_not_become_the_next_digest`.
                # The name the old paragraph promised
                # (`test_a_manager_notification_is_never_claimed_for_delivery`)
                # was never written -- it has matched nothing since `35f4770`
                # introduced the sentence, which is its own small lesson about
                # naming a guard in prose instead of in a file.
                row = connection.execute(
                    """
                    SELECT o.id, o.intent_id, o.conversation_id, o.command,
                           o.idempotency_key,
                           -- **Selected so the caller can read it back.**
                           -- Today the only readers are this batch's two cases,
                           -- which ask what the claim handed over. Nothing
                           -- downstream records it: `ordinal` is in neither
                           -- `delivery_receipts.evidence` nor `DispatchResult`.
                           -- (Independent review 2026-09-08, P3-1: the first
                           -- version of this comment said the receipt needed it,
                           -- which was a reason for a thing that does not exist.)
                           o.ordinal
                      FROM outbox_messages o
                     WHERE o.tenant_id = %s
                       AND (o.status = 'PENDING' OR
                            (o.status = 'SENDING' AND o.lease_expires_at < now()))
                       -- **同一轮的分段要排队**（ADR-0151，用户 2026-09-23
                       -- 「本来每段话之间就应该有延迟，人打字还要时间的」）。
                       --
                       -- 两个条件，而它们在同一个 NOT EXISTS 里：
                       --   ① 同轮里序号更小的段必须已经 `DELIVERED`；
                       --   ② 它确认之后还要过够 **那一段自己的** `pace_ms`。
                       --
                       -- ⚠️ **没有这一条时会发生什么，迁移 044 自己写过**：
                       -- ordinal 0 发到一半抛了，它在租约内保持 `SENDING`，
                       -- 下一趟取到 ordinal 1 发了出去，ordinal 0 等租约过期才走 ——
                       -- 「客户读到第二句，三十秒后才读到第一句」。
                       -- 对两条独立消息那是难看；**对同一句话的分段，
                       -- 是「因为…」和「所以…」倒着到**。
                       --
                       -- **同一轮**的标识是幂等键第二段（源事件 id），
                       -- 与 `TURN_KEY_SQL` 是同一个表达式 —— 限流那一侧也读它。
                       -- 两处必须是同一个写法，否则一边认得出同轮、另一边认不出，
                       -- 而认不出的那一边会放行一个本该等着的段。
                       AND NOT EXISTS (
                           SELECT 1
                             FROM outbox_messages earlier
                            WHERE earlier.tenant_id = o.tenant_id
                              AND earlier.conversation_id = o.conversation_id
                              AND split_part(earlier.idempotency_key, ':', 2)
                                  = split_part(o.idempotency_key, ':', 2)
                              AND earlier.ordinal < o.ordinal
                              -- ⚠️ **只有「后面还有段要来」的那些行才拦人。**
                              -- `pace_ms > 0` 恰好就是这个意思：分段里最后
                              -- 一段留 0，而一条独来独往的消息、一条
                              -- `send_material`、一条 `remember` 也都是 0。
                              --
                              -- **第一版漏了这一句，谓词因此宽于 ADR-0151**
                              -- （它写的是「同一轮的**分段**要排队」）：
                              -- 同轮里 `send_message` + `send_material` 那种
                              -- 也被卡住了，全量门禁 7 条红当场指出来 ——
                              -- `both rows of the turn should be claimable`。
                              AND earlier.pace_ms > 0
                              -- ⚠️ **判据是「还在投递中」，不是「送到了」**
                              -- （ADR-0152，用户 2026-09-23 裁定 A）。
                              --
                              -- `074` 第一版写的是 `<> 'DELIVERED'`，于是一个
                              -- `UNKNOWN`（供应商没答复，**而它是终态，不会自己
                              -- 变成 `DELIVERED`**）会把后面所有气泡永久钉死 ——
                              -- 运维 2026-09-23 实测：客户只看到「不会。」三个字。
                              --
                              -- **而那是「分段之后还要判定」的投递层版本**：
                              -- 判定已经说过「这条要发」了，投递层不该再替它
                              -- 判一次「上一段算不算数」。前一段走完（不管
                              -- `DELIVERED` / `FAILED` / `UNKNOWN` / `CANCELLED`），
                              -- 这一段就该走。用户：「**本来就有兜底就行**」——
                              -- 首段没送到那条已经有 `manual_task` 接着。
                              --
                              -- **写成「还在投递中」而不是列举终态**：今天有六个
                              -- 状态（`048` 加了 `CANCELLED`），而列举式会在加
                              -- 第七个时安静地漏掉它。
                              AND (earlier.status IN ('PENDING', 'SENDING')
                                   OR earlier.updated_at
                                      + (earlier.pace_ms || ' milliseconds')::interval
                                      > now())
                       )
                       -- **The `audience` predicate is gone** (B1-5).
                       -- It was the thing keeping manager notifications out of
                       -- the dispatcher while the last hop did not exist; the
                       -- three conditions `_notify_manager` listed for removing
                       -- it are now met (route by `recipient` -- T90, above;
                       -- typed `content`; a one-day deadline). What replaces it
                       -- is `unroutable_reason`: a row that cannot be addressed
                       -- is refused by name instead of being left unclaimed.
                     -- **`ordinal` between the two** (T109, migration 044).
                     -- `created_at` is the transaction timestamp, so every row
                     -- a turn wrote shares it and the sort fell through to
                     -- `id`, which is `uuid4()`. `ordinal` is the position the
                     -- turn actually meant; `id` stays last so the order is
                     -- total even for rows written before 044, which all carry
                     -- ordinal 0 and sort among themselves exactly as before.
                     ORDER BY o.created_at, o.ordinal, o.id
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                    """,
                    (tenant_id,),
                ).fetchone()
                if row is None:
                    continue
                execution_token = str(uuid4())
                # **`RETURNING lease_expires_at, now()`, and the pair is T183's fix.**
                #
                # The comment that used to stand here read: "the database's
                # clock is the only one that counts for that -- this process's
                # may be off by more than the margin." **That sentence
                # describes the condition under which the arithmetic downstream
                # stops working, and it was written as the reason for adopting
                # it.** `lease_expires_at` is an instant on the database's
                # clock; `_reconcile_before_giving_up` compared it against
                # `datetime.now()`, an instant on this process's. Subtracting
                # them leaves the offset between the two clocks inside the
                # answer, and when the database runs ahead by more than
                # `LEASE_SAFETY_MARGIN_SECONDS` this dispatcher believes it
                # still holds a lease that has already expired -- so a second
                # dispatcher claims the row and the customer is sent the
                # message twice.
                #
                # **The offset is not hypothetical.** T186 measured the database
                # clock +0.43s ahead of the process on this deployment, with the
                # margin set to 1.0 -- and T183 was registered on the premise
                # that the two clocks agree because they share a host.
                #
                # Returning the database's own clock alongside makes the
                # remaining lease a *difference between two readings of one
                # clock*, which is the only quantity here that does not carry
                # the offset.
                #
                # **It has to be `clock_timestamp()`, and the first version of
                # this fix used `now()`** (independent review P1-1). `now()` is
                # `transaction_timestamp()` -- the instant the *transaction*
                # opened -- and the same `now()` sets `lease_expires_at` five
                # lines down, so the difference was the identity
                # `OUTBOX_LEASE_SECONDS`: measured 30.000000s, on the nose,
                # forever. It measured nothing. What it produced was
                # `claim_started_at + 30s` from an anchor taken **after** the
                # transaction had already run `_dead_letter_exhausted` and the
                # candidate `SELECT` -- so the deadline landed *later* than the
                # truth by however long those two took. That is the direction
                # that double-sends. And `_dead_letter_exhausted` has no
                # `SKIP LOCKED` (the candidate `SELECT` does), so a second
                # dispatcher waits there on locks for an unbounded time: past
                # `LEASE_SAFETY_MARGIN_SECONDS` this dispatcher would keep
                # reconciling and writing inside a lease it no longer held.
                # **The fix for the double send had reintroduced it**, one
                # keyword away, and the comment standing here asserted the
                # safety it had just lost.
                #
                # `clock_timestamp()` is evaluated when this statement runs, so
                # the difference is the lease *less whatever the transaction has
                # already spent* -- which is the quantity that shrinks under
                # exactly the contention described above. `claim_started_at` is
                # read immediately before the statement goes out, so the round
                # trip can only move the deadline earlier than the truth, never
                # later. That sentence is true now; it was not before.
                claim_started_at = datetime.now(UTC)
                claimed = connection.execute(
                    """
                    UPDATE outbox_messages
                       SET status = 'SENDING', attempt = attempt + 1,
                           lease_owner = %s,
                           lease_expires_at = now() + %s * interval '1 second',
                           updated_at = now()
                     WHERE tenant_id = %s AND id = %s
                       AND (status = 'PENDING' OR
                            (status = 'SENDING' AND lease_expires_at < now()))
                 RETURNING lease_expires_at, clock_timestamp() AS db_now
                    """,
                    (self._dispatcher_id, OUTBOX_LEASE_SECONDS, tenant_id, row["id"]),
                ).fetchone()
                if claimed is None:
                    continue
                row = dict(row)
                # Kept as the evidence value: this is what is actually in the
                # row, and an operator reading a receipt wants the number the
                # database holds rather than one this process derived.
                row["lease_expires_at"] = claimed["lease_expires_at"]
                # The value the arithmetic uses. Same clock on both sides of
                # the subtraction; the anchor is this process's own.
                row["lease_deadline_at"] = _lease_deadline(
                    claim_started_at, claimed["lease_expires_at"], claimed["db_now"]
                )
                connection.execute(
                    """
                    UPDATE intent_records
                       SET status = 'EXECUTING', execution_token = %s,
                           lease_expires_at = now() + %s * interval '1 second',
                           updated_at = now()
                     WHERE tenant_id = %s AND id = %s
                       AND status IN ('APPROVED', 'EXECUTING')
                    """,
                    (execution_token, OUTBOX_LEASE_SECONDS, tenant_id, row["intent_id"]),
                )
                return tenant_id, row
        return None

    def _cancel_expired_proactive(
        self, *, tenant_id: UUID, row: dict[str, Any], command: dict[str, Any],
        expires_at: datetime,
    ) -> DispatchResult | None:
        """Retire a proactive row whose window closed while it waited. **Nothing is sent.**

        RFC §7.1: 「deadline 过期**只能在未开始副作用前**取消」 -- and nothing has
        started here, which is why this method exists at the top of
        `dispatch_one` rather than anywhere below the `adapter.send()` call.
        §7.2 step 7 names the intent's ending: 「在 pre_delivery … 则 DENIED …
        不发送」.

        **No delivery receipt.** All four `delivery_receipts.status` values mean
        the provider was asked; none was. What is written instead is a
        `policy_decisions` row with `hook='pre_delivery'` -- the first row this
        repository writes at that hook, and the reason the enum has had the value
        since `001`.

        **The lease is re-checked in the same statement**, exactly as the success
        path does: if another dispatcher owns the row now, this one writes
        nothing at all. It cannot even claim a duplicate send happened, because
        it never sent.
        """

        # Read out of the command, never out of the profile: ADR-0098 §一 makes
        # this a purely local judgement, and reaching for a profile release here
        # would be the tenant-scoped read that rule exists to forbid.
        lifts_at = proactivity_window_lifts_at(command)

        with self._database.transaction(tenant_id=tenant_id) as connection:
            kept = connection.execute(
                """
                UPDATE outbox_messages
                   SET status = 'CANCELLED', lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'SENDING'
                   AND lease_owner = %s
                """,
                (tenant_id, row["id"], self._dispatcher_id),
            )
            if kept.rowcount != 1:
                return DispatchResult(
                    tenant_id=tenant_id,
                    intent_id=UUID(str(row["intent_id"])),
                    outbox_id=UUID(str(row["id"])),
                    receipt_status="cancelled",
                    lease_lost=True,
                )
            connection.execute(
                """
                UPDATE intent_records
                   SET status = 'DENIED', lease_expires_at = NULL,
                       unknown_reason = NULL, updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'EXECUTING'
                """,
                (tenant_id, row["intent_id"]),
            )
            record_policy_decision(
                connection,
                tenant_id=tenant_id,
                row_id=uuid4(),
                conversation_id=UUID(str(row["conversation_id"])),
                intent_id=UUID(str(row["intent_id"])),
                output=PolicyOutput(
                    hook="pre_delivery",
                    effect="deny",
                    reason_code="PROACTIVITY_WINDOW_EXPIRED",
                    policy_id="proactivity-window",
                    policy_version="1.0.0",
                    obligations=audit_only(),
                    next_allowed_at=lifts_at,
                    context={
                        "outbox_id": str(row["id"]),
                        "window_expires_at": _iso(expires_at),
                        # **Which of the two sources expired this row.**
                        # Before B4-5 there was only one, so the reason code
                        # said everything; now `PROACTIVITY_WINDOW_EXPIRED`
                        # can mean 「不该发」 (the active window closed, the
                        # message is a different class now) or 「该改期发」
                        # (quiet hours opened) -- **opposite operational
                        # meanings under one code**. The column above answers
                        # it queryably; this answers it for anyone reading
                        # the evidence (independent review 2026-09-09, P1-1).
                        "window_source": (
                            "quiet_hours" if lifts_at is not None else "active_window"
                        ),
                    },
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'intent.execute', 'intent_record', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    row["intent_id"],
                    UUID(str(command["correlation_id"])),
                    "PROACTIVITY_WINDOW_EXPIRED",
                    Jsonb({"outbox_id": str(row["id"]), "request_sent": False}),
                ),
            )
        return DispatchResult(
            tenant_id=tenant_id,
            intent_id=UUID(str(row["intent_id"])),
            outbox_id=UUID(str(row["id"])),
            receipt_status="cancelled",
        )

    def dispatch_one(self) -> DispatchResult | None:
        claimed = self._claim_one()
        if claimed is None:
            return None
        tenant_id, row = claimed
        command = row["command"]
        deadline = datetime.fromisoformat(str(command["deadline_at"]).replace("Z", "+00:00"))
        # **ProactivityPolicy's second check, and it is the whole of it**
        # (RFC `:570` 「pre_execute 和 pre_delivery 两次检查」; ADR-0098 §一 rules
        # the form). One field, one comparison against this process's clock.
        #
        # **Before anything is built and before any read** -- the address is not
        # resolved, the adapter is not constructed, the provider is not asked.
        # That placement is the point: a check whose failure would be paid for
        # by a customer belongs at admission, not in the accounting transaction
        # after the send.
        expires_at = proactivity_deadline(command)
        if expires_at is not None and datetime.now(UTC) >= expires_at:
            return self._cancel_expired_proactive(
                tenant_id=tenant_id, row=row, command=command, expires_at=expires_at
            )
        # **The refusal that replaces a silent fallback** (T90 / ADR-0095 §三).
        # Decided before the adapter contract is built, because the point is
        # that nothing is sent -- not that the send fails politely. The row then
        # travels the same terminal path a provider refusal takes, so it gets a
        # receipt, an audit row and a manual task: somebody owns it.
        unroutable = unroutable_reason(command)
        # **The address this row resolves to, when a real provider is wired.**
        # `None` on both counts when no factory is configured: the M0 simulator
        # is addressed by a URL, so there is nothing to resolve and nothing that
        # can refuse -- deliberately leaving that path byte-for-byte as it was.
        address: DispatchAddress | None = None
        unaddressable: str | None = None
        if unroutable is None and self._channel_factory is not None:
            # A short transaction of its own. The send happens outside any
            # transaction (this class's whole premise), and the resolution has
            # to be inside one for RLS to scope it to this tenant -- which is
            # also what makes `CONVERSATION_NOT_VISIBLE` mean something.
            with self._database.transaction(tenant_id=tenant_id) as connection:
                address, unaddressable = resolve_dispatch_address(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=row["conversation_id"],
                    command=command,
                )
        # **文件那一行在这里才被解析。**
        #
        # 出站行里存的是一个引用（`_send_material_content`），四个 `provider_*`
        # 字段在这一刻才从目录里取 —— 取出来只活在下面那次调用的内存里，
        # **不落进任何一张表**。`068` 给 worker 发的那条读权就是为这一步。
        #
        # 与上面的地址解析一样**自己开一个短事务**：发送发生在事务之外
        # （这个类的整个前提），而解析必须在事务里，RLS 才把它圈在本租户内。
        content_to_send: Any = command["content"]
        unsendable_material: str | None = None
        if (
            unroutable is None
            and unaddressable is None
            and isinstance(content_to_send, Mapping)
            and str(content_to_send.get("type")) == ASSET_CONTENT_TYPE
        ):
            with self._database.transaction(tenant_id=tenant_id) as connection:
                resolved, unsendable_material = resolve_asset_for_send(
                    connection,
                    tenant_id=tenant_id,
                    reference=str(content_to_send.get("ref") or ""),
                )
            if resolved is not None:
                content_to_send = resolved

        if unroutable is None and unaddressable is None and unsendable_material is not None:
            # **与 `binding_not_ready` 分开，因为修它的是另一个人。**
            # 那一条是「绑定没就绪」，这一条是「这份资料还没备好」——
            # 而后者的修法是跑导入或上传脚本，不是去看绑定。
            receipt = SendReceipt(
                status="failed",
                provider_operation_id=None,
                evidence={
                    "adapter": "dispatcher",
                    "reason": "material_not_ready",
                    "detail": unsendable_material,
                    "audience": str(command.get("audience", "customer")),
                    "request_sent": False,
                },
            )
        elif unroutable is None and unaddressable is not None:
            # **A supply problem, not a malformed row** -- and the two are kept
            # apart because different people fix them. `unroutable_recipient`
            # means the producer wrote a row nobody can address;
            # `binding_not_ready` means the row is fine and the *deployment* is
            # not: no contact recorded, binding disabled, ceremony unfinished.
            # Collapsing them would send an operator to read producer code over
            # a guid that was never seeded.
            receipt = SendReceipt(
                status="failed",
                provider_operation_id=None,
                evidence={
                    "adapter": "dispatcher",
                    "reason": "binding_not_ready",
                    "detail": unaddressable,
                    "audience": str(command.get("audience", "customer")),
                    # Same rule as below: nothing was asked, so say so.
                    "request_sent": False,
                },
            )
        elif unroutable is not None:
            receipt = SendReceipt(
                status="failed",
                provider_operation_id=None,
                evidence={
                    "adapter": "dispatcher",
                    "reason": "unroutable_recipient",
                    "detail": unroutable,
                    "audience": str(command.get("audience", "customer")),
                    # **Nothing was asked, and the receipt has to say so.** L1's
                    # whole subject is the difference between "we asked and do
                    # not know" and "we never asked".
                    "request_sent": False,
                },
            )
        else:
            adapter_contract: dict[str, Any] = {
                "operation": "send",
                "request": {
                    "tenant_id": str(tenant_id),
                    "conversation_id": str(row["conversation_id"]),
                    "content": command["content"],
                    # T90: present only when the conversation is not the address.
                    **(
                        {"recipient": command["recipient"]}
                        if command.get("recipient") is not None
                        else {}
                    ),
                },
                "response": None,
                "deadline_at": _iso(deadline),
                "idempotency_key": str(row["idempotency_key"]),
            }
            validate_contract("channel-adapter", adapter_contract)
            # **Built per dispatch when a factory is wired** (ADR-0094 §一 Q5).
            # `address` is not `None` here: it is set together with
            # `unaddressable` above, and a refusal took the branch that skips
            # this one.
            #
            # **The `assert` is documentation, not a defence** (independent
            # review 2026-09-09, P3-1). Nothing in this repository runs with
            # `-O` -- the image's `CMD` carries no such flag -- so it is not
            # stripped today; but it is an `assert` in the dispatch path, and if
            # `-O` ever appears it disappears silently. It is written down here
            # rather than turned into a refusal because the invariant was walked
            # branch by branch and holds, and inventing a receipt for a state
            # that cannot occur would add an untested path to the one method
            # this repository is most careful about.
            if self._channel_factory is not None:
                assert address is not None
                adapter: ChannelAdapter = self._channel_factory(
                    guid=address.guid, recipient_id=address.recipient_id
                )
            else:
                adapter = self._channel
            receipt = adapter.send(
                command=SendCommand(
                    tenant_id=str(tenant_id),
                    conversation_id=str(row["conversation_id"]),
                    # **解析之后的那一份**：文字那一行它就是 `command["content"]`，
                    # 文件那一行它是刚从目录里取出来的四个字段。
                    content=content_to_send,
                    # Whoever this row is for; `None` for customer messages,
                    # where the conversation is the address.
                    recipient=command.get("recipient"),
                ),
                idempotency_key=str(row["idempotency_key"]),
                deadline_at=deadline,
            )
            if receipt.status == "unknown":
                receipt = self._reconcile_before_giving_up(
                    receipt=receipt,
                    idempotency_key=str(row["idempotency_key"]),
                    deadline_at=deadline,
                    lease_deadline_at=row["lease_deadline_at"],
                    adapter=adapter,
                )
        receipt_id = uuid4()
        intent_status = {
            "accepted": "APPLIED",
            "delivered": "APPLIED",
            "failed": "FAILED",
            "unknown": "UNKNOWN",
        }[receipt.status]
        outbox_status = "DELIVERED" if receipt.status == "accepted" else receipt.status.upper()
        with self._database.transaction(tenant_id=tenant_id) as connection:
            # **The lease is re-checked before a single word is written down.**
            # This update used to match on `status = 'SENDING'` alone, so a
            # dispatcher whose lease had expired -- and whose row another
            # dispatcher had since claimed and re-sent -- still overwrote the new
            # owner's status and still inserted its own receipt, where it hit the
            # `(tenant_id, outbox_id)` unique index and raised into the worker
            # loop's `except Exception` (narrow re-review 2026-09-04). Guarding on
            # `lease_owner` is the same discipline `_release_lease` and
            # `_fail_permanently` already follow on the inbox side: whoever holds
            # the lease is the only one allowed to write the outcome.
            #
            # It goes first because it is the gate. If the row is not ours, the
            # receipt insert must not be attempted at all.
            kept = connection.execute(
                """
                UPDATE outbox_messages
                   SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                       updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'SENDING'
                   AND lease_owner = %s
                """,
                (outbox_status, tenant_id, row["id"], self._dispatcher_id),
            )
            if kept.rowcount != 1:
                # A normal branch, not an error: we sent, we lost the row, and
                # the dispatcher that owns it now will write what it observes.
                # What must not be lost is the fact that a second send happened,
                # so the audit trail carries it -- otherwise a duplicate delivery
                # is invisible in every table we keep. The intent and the
                # conversation aggregate are left alone for the same reason the
                # outbox row is: they belong to the current owner.
                connection.execute(
                    """
                    INSERT INTO audit_events(
                        tenant_id, id, actor_id, action, resource_type, resource_id,
                        correlation_id, reason_code, evidence
                    ) VALUES (%s, %s, NULL, 'intent.execute', 'intent_record', %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        uuid4(),
                        row["intent_id"],
                        UUID(str(command["correlation_id"])),
                        "DELIVERY_LEASE_LOST",
                        Jsonb(
                            {
                                "outbox_id": str(row["id"]),
                                "dispatcher_id": self._dispatcher_id,
                                "receipt_status": receipt.status,
                                "lease_expires_at": _iso(row["lease_expires_at"]),
                            }
                        ),
                    ),
                )
                return DispatchResult(
                    tenant_id=tenant_id,
                    intent_id=UUID(str(row["intent_id"])),
                    outbox_id=UUID(str(row["id"])),
                    receipt_status=receipt.status,
                    lease_lost=True,
                )
            connection.execute(
                """
                INSERT INTO delivery_receipts(
                    tenant_id, id, conversation_id, intent_id, outbox_id,
                    provider_operation_id, status, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    receipt_id,
                    row["conversation_id"],
                    row["intent_id"],
                    row["id"],
                    receipt.provider_operation_id,
                    receipt.status,
                    Jsonb(receipt.evidence),
                ),
            )
            if receipt.status in DELIVERY_MANUAL_TASK_REASONS:
                # The delivery is over and it did not arrive. `unknown` means
                # nobody knows (L1 guarantees the provider was asked); `failed`
                # means the provider looked at it and refused. Both leave a
                # customer without the message they were owed, and both need a
                # person -- which is the whole of L2: there were terminal states
                # and nobody could see them.
                #
                # `failed` was **not** covered when this was written, and the
                # reviewer found it: the batch had "delivery FAILED -> digest"
                # in its own design note and produced nothing for it. Whether
                # a provider-refused delivery belongs in the merchant's digest
                # was the user's call, not mine (push scope), and the ruling on
                # 2026-09-04 was: it goes in, pushed to the merchant.
                #
                # **Whose message failed decides which code it gets** (user's
                # ruling 2026-09-08, independent review P1-1). A customer
                # message that did not arrive is the merchant's problem and goes
                # in their digest; a *notice to the merchant's manager* that did
                # not arrive would go in the digest that is trying to reach that
                # same manager -- so it takes a platform-internal code and the
                # ring never closes. The task is written either way: nobody
                # loses an owner, they only stop being told twice.
                reasons = (
                    DELIVERY_MANUAL_TASK_REASONS
                    if is_customer_row(command)
                    else NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS
                )
                self._manual_task_for_delivery(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(row["conversation_id"])),
                    intent_id=UUID(str(row["intent_id"])),
                    outbox_id=UUID(str(row["id"])),
                    reason_code=reasons[receipt.status],
                    evidence=dict(receipt.evidence),
                )
            connection.execute(
                """
                UPDATE intent_records
                   SET status = %s, external_operation_ref = %s, result = %s,
                       unknown_reason = %s, lease_expires_at = NULL, updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                   -- **一次判定铺成 N 行之后，这条 UPDATE 会被执行 N 次**
                   -- （ADR-0152）。在此之前一个 intent 只有一行出站，
                   -- `status = 'EXECUTING'` 因此既是锁也是「只写一次」。
                   --
                   -- ⚠️ **只留那半条会丢掉第 2 段以后的投递结果**：
                   -- 第 1 段写完 intent 就进了终态，后面几段的 UPDATE
                   -- 一行都匹配不上 —— 而那正是 T86 / ADR-0095 §四
                   -- 靠旧的 `UNIQUE (tenant_id, intent_id)` 挡住的形状。
                   --
                   -- **用户 2026-09-23 裁定：intent 的终态按「最差的那一段」算。**
                   -- 一条消息只要有一段没到，那次判定就不算完成。
                   -- 次序由 `INTENT_STATUS_SEVERITY` 定，**只许往更坏的方向改**。
                   AND (status = 'EXECUTING'
                        OR %s > coalesce(
                            (SELECT severity FROM (VALUES
                                ('APPLIED', 1), ('UNKNOWN', 2), ('FAILED', 3)
                             ) AS s(name, severity) WHERE s.name = intent_records.status),
                            0))
                """,
                (
                    intent_status,
                    receipt.provider_operation_id,
                    Jsonb({"receipt_id": str(receipt_id), "status": receipt.status}),
                    "DELIVERY_UNCONFIRMED" if intent_status == "UNKNOWN" else None,
                    tenant_id,
                    row["intent_id"],
                    # 这一段的严重度 —— 只有比已记下的更坏才覆盖。
                    INTENT_STATUS_SEVERITY[intent_status],
                ),
            )
            if receipt.status in {"accepted", "delivered"} and is_customer_row(command):
                # **Only a customer row's success is a fact about its
                # conversation** (independent review P1-2). T90 stopped the
                # borrowed `conversation_id` from being used as an *address*;
                # this is the same borrowed id being used as a *fact*. The
                # manager digest hangs itself off "the oldest task's
                # conversation" purely to satisfy a foreign key, so stamping
                # this on a successful manager notice would record that the
                # platform had just sent something outbound to a customer who
                # heard nothing.
                #
                # It is not a cosmetic column: it is in the
                # `conversation-aggregate` contract, `GET /conversations/{id}`
                # returns it, and `conversation-engine.md:558` makes it one of
                # the anchors proactivity computes `due` from -- which is the
                # next batch's subject, so the wrong stamp would arrive as its
                # input.
                stamped = connection.execute(
                    """
                    UPDATE conversation_aggregates
                       SET last_outbound_at = now(), updated_at = now()
                     WHERE tenant_id = %s AND id = %s
                 RETURNING last_outbound_at, state, generation, state_version,
                           profile_release_id
                    """,
                    (tenant_id, row["conversation_id"]),
                ).fetchone()
                # **B4-2: the second half of the producer, and the reason it is
                # here rather than with the transition.** `silence_check` is
                # anchored on `last_outbound_at` (ADR-0105 §零.2), and that
                # value does not exist until the line above writes it. Creating
                # the row back at the transition would have anchored the shipped
                # 75-second rule to the *previous* outbound -- a timer already
                # overdue at birth, reporting silence about a message the
                # customer had not been sent.
                #
                # **The anchor is read back from the row, not recomputed.** It is
                # `now()` on the database's clock, returned by the same
                # statement; deriving it from this process's clock would be the
                # two-clock subtraction T183 was registered for.
                self._schedule_from_outbound(
                    connection,
                    tenant_id=tenant_id,
                    conversation_id=UUID(str(row["conversation_id"])),
                    stamped=stamped,
                )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'intent.execute', 'intent_record', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    row["intent_id"],
                    UUID(str(command["correlation_id"])),
                    f"DELIVERY_{receipt.status.upper()}",
                    Jsonb({"receipt_id": str(receipt_id)}),
                ),
            )
        return DispatchResult(
            tenant_id=tenant_id,
            intent_id=UUID(str(row["intent_id"])),
            outbox_id=UUID(str(row["id"])),
            receipt_status=receipt.status,
        )
