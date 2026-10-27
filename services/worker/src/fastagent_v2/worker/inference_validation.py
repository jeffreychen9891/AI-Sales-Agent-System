"""Validation of the three-stage inference results.

A model result is a *proposal*. Nothing downstream may consume one before it has
passed through here. Two rules drive the whole module:

1. The platform, not the model, decides what is legal. Strategy, intent kind,
   scheduled event kind and target-stack operation are checked against the
   profile and the state machine, never against the model's own claim.
2. A rejected plan is never repaired by the generator. It is replaced by a
   deterministic `safe_fallback` plan, and the rejection reason survives into
   evidence.

Conversation-engine RFC sections 4.1 through 4.5 are the authority for the rules
below; where the RFC states a rule in prose, the reason code names it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastagent_v2.runtime.contracts import validate_inference_contract
from fastagent_v2.runtime.memory_prompt import MEMORY_REF_PREFIX
from fastagent_v2.worker.channel import MATERIAL_REF_SEGMENT_TYPE
from fastagent_v2.worker.inference import InferenceDocument, safe_fallback_plan
from fastagent_v2.worker.platform_frames import (
    frame_definitions,
    is_platform_frame,
    strategy_registration,
)
from fastagent_v2.worker.strategies import (
    IN_FRAME_ONLY_STRATEGIES,
    SAFE_FALLBACK_STRATEGIES,
    SCHEDULED_EVENT_KINDS,
    SILENT_STRATEGIES,
    STRATEGY_CATALOG,
    TARGET_STACK_OPERATIONS,
    time_rule_strategy_set,
)
from fastagent_v2.worker.target_stack import (
    EXIT_SIGNAL_RELATIONS,
    TERMINAL_FRAME_STATUSES,
    frames_ever_entered,
)

MAX_STACK_DEPTH = 3

#: Interruption relations that may justify pushing a child frame.
PUSH_RELATIONS = ("side_question", "new_goal", "correction")


class InferenceRejected(RuntimeError):
    """Base class for a rejected step result. Carries a stable reason code.

    **`context` 是给下游留痕用的结构化事实，不是给人读的文字**（2026-09-13）。
    `detail` 是一句话，而一条人工任务要答「卡在哪个槽位、模型选了什么策略」——
    **从 `detail` 里解析回来是本仓付过账的那一类**
    （R3 那次：门禁改成问数据库，不再解析 ACL 字符串）。
    拒绝发生的那一处知道这些事实，所以它把事实带出来，而不是把句子带出来。
    """

    def __init__(
        self, reason_code: str, detail: str, context: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail
        self.context: dict[str, Any] = dict(context or {})


class PlanRejected(InferenceRejected):
    pass


class GenerationRejected(InferenceRejected):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    document: InferenceDocument
    frame: dict[str, Any]
    definition: dict[str, Any]
    #: 平台补过一条 `send_message` 候选时，这里是它的幂等基准；否则 `None`。
    #:
    #: **管线据它写一行审计** —— 「这一轮的 send_message 是模型提的
    #: 还是平台补的」必须永远查得到。
    reply_floor_basis: str | None = None

    @property
    def strategy_id(self) -> str:
        return str(self.document["selected_strategy_id"])

    @property
    def is_silent(self) -> bool:
        return self.strategy_id in SILENT_STRATEGIES

    @property
    def origin(self) -> str:
        return str(self.document["origin"])



#: 平台补的那条候选用这个前缀做幂等基准，**一眼看得出是谁补的**。
REPLY_FLOOR_BASIS_PREFIX = "platform-reply-floor"

#: 补底那件事的理由码 —— 审计行按它分桶。
REPLY_FLOOR_REASON_CODE = "PLAN_CANDIDATE_FLOORED"


def _floor_a_reply_candidate(
    plan: InferenceDocument, *, strategy: str, profile: dict[str, Any]
) -> str | None:
    """打法允许回一句话，而计划没提名它 —— 平台补上。返回补的那个幂等基准。

    ## 为什么

    运维 2026-09-17 实测：`GENERATION_ADDED_INTENT` 九次里**六次是
    `'send_message' was not a planned candidate intent`** ——
    模型在生成步想回客户一句话，而它自己在计划步没提名这个 kind，
    于是整轮作废、**客户一个字都没收到**。

    **「回客户一句话」不是需要模型许可的事**：打法的 `allowed_intent_kinds`
    已经授权了这个 kind，计划步没把它写进候选是一次疏漏，不是一次决定。

    ## 它不碰的两处

    * **静默打法**（`wait` / `no_message`）**不补** —— 它们的全部意思
      就是这一轮不说话，补一条候选等于把那个决定推翻；
    * **打法没授权 `send_message` 的**不补 —— 那是配包的边界，不是疏漏。

    ## 留痕

    补出来的候选带 `REPLY_FLOOR_BASIS_PREFIX` 前缀，
    并由 `ValidatedPlan.reply_floor_basis` 带给管线写一行审计。
    **「这一轮的 send_message 是模型提的还是平台补的」永远查得到** ——
    不留痕就等于悄悄改写了模型的计划。
    """

    if strategy in SILENT_STRATEGIES:
        return None
    allowed = ((profile.get("strategies") or {}).get(strategy) or {}).get(
        "allowed_intent_kinds"
    ) or []
    if "send_message" not in allowed:
        return None
    candidates = plan["candidate_intents"]
    if any(candidate["kind"] == "send_message" for candidate in candidates):
        return None

    # **基准只需在一份计划内唯一，不需要带轮次** ——
    # 出站的幂等键是 `f"{前缀}:{event_id}:{basis}"`（`pipeline`），
    # **`event_id` 已经把轮次锁住了**。往这里再塞一个 `state_version`
    # 只会让它依赖聚合的字段，而这个函数不需要聚合。
    #
    # 同一份计划里若真有别的候选用了这个字面量，
    # `PLAN_CANDIDATE_BASIS_NOT_UNIQUE` 会当场响 —— 那道闸已经在了。
    basis = REPLY_FLOOR_BASIS_PREFIX
    candidates.append(
        {"kind": "send_message", "typed_payload": {}, "idempotency_basis": basis}
    )
    # **补完再过一遍合同**：补错了在这里响，而不是在出站那一刻。
    validate_inference_contract("plan-result", plan)
    return basis


def current_frame(aggregate: dict[str, Any]) -> dict[str, Any]:
    stack = aggregate["target_stack"]
    if not stack:
        raise PlanRejected("EMPTY_TARGET_STACK", "aggregate has no target frame")
    return dict(stack[-1])


def frame_definition(profile: dict[str, Any], definition_id: str) -> dict[str, Any]:
    """The definition of one frame, profile-declared or platform-owned (B4-7)."""

    definition: dict[str, Any] | None = frame_definitions(profile).get(str(definition_id))
    if definition is None:
        raise PlanRejected("UNKNOWN_FRAME_DEFINITION", f"profile has no frame {definition_id!r}")
    return definition


def validate_plan(
    *,
    plan: InferenceDocument,
    understanding: InferenceDocument,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    required_strategy: str | None = None,
) -> ValidatedPlan:
    """Prove a plan against the profile and the target-stack state machine.

    Raises `PlanRejected` with a reason code; callers turn that into a
    deterministic fallback via `plan_or_fallback`.
    """

    validate_inference_contract("plan-result", plan)
    validate_inference_contract("understanding-result", understanding)

    frame = current_frame(aggregate)
    definition = frame_definition(profile, str(frame["definition_id"]))
    strategy = str(plan["selected_strategy_id"])
    origin = str(plan["origin"])

    if strategy not in STRATEGY_CATALOG:
        raise PlanRejected("STRATEGY_NOT_IN_CATALOG", f"{strategy!r} is not a platform strategy")

    if origin == "safe_fallback":
        if strategy not in SAFE_FALLBACK_STRATEGIES:
            raise PlanRejected(
                "FALLBACK_STRATEGY_NOT_ALLOWED", f"{strategy!r} is not a safe fallback"
            )
    else:
        # **The `TimeRule`'s declared strategy binds the turn it fired**
        # (B4-4d; ADR-0105 §零.1 rules why: the field carries the profile
        # author's product decision, and 「删掉等于把这个决定交回给模型」 --
        # so does letting the model overrule it).
        #
        # **Checked before the frame list below**, because it is the more
        # specific statement and gives the more useful reason code: with a
        # narrow `time_rule_strategy_set` the allowlist would refuse a
        # wandering plan first, and 「不是规则声明的那个」 would never be said.
        #
        # **Only a model plan.** `origin == "safe_fallback"` never reaches here:
        # the fallbacks are deterministic safety valves and the frame allowlist
        # already exempts them, so binding them to a rule's strategy would leave
        # a rejected timer plan with no legal replacement at all.
        if required_strategy is not None and strategy != required_strategy:
            raise PlanRejected(
                "STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED",
                f"the rule declared {required_strategy!r}; the plan selected {strategy!r}",
            )

        # **Which list applies depends on what started the turn** (ADR-0116).
        # `allowed_strategy_set` is what the model may choose on an ordinary
        # turn; `time_rule_strategy_set` is what this frame's rules may declare,
        # and it defaults to the first. Before the fork the same list answered
        # both, which meant permitting a follow-up play at the 24-hour mark also
        # offered it to the model mid-conversation.
        frame_allowlist = (
            time_rule_strategy_set(definition)
            if required_strategy is not None
            else tuple(definition["allowed_strategy_set"])
        )
        if strategy not in frame_allowlist:
            raise PlanRejected(
                "STRATEGY_NOT_IN_FRAME_ALLOWLIST",
                f"{strategy!r} is not allowed by frame {definition['id']!r}",
            )
        # **Which registry answers depends on whose frame this is** (B4-7).
        # Inside the platform's no-goal frame the platform registers its own two
        # plays, because requiring the profile to register them would put "the
        # profile has to remember" back in the path ADR-0099 took it out of.
        registered = strategy_registration(profile, definition["id"], strategy)
        if registered is None:
            raise PlanRejected(
                "STRATEGY_NOT_IN_PROFILE_REGISTRY",
                f"{strategy!r} is not registered by the profile",
            )
        _validate_model_plan_against_profile(
            plan=plan, registered=registered, definition=definition, frame=frame
        )

    _validate_knowledge_query(plan=plan, origin=origin)
    _validate_scheduled_event_changes(plan=plan, aggregate=aggregate, definition=definition)
    _validate_stack_operation(
        plan=plan,
        understanding=understanding,
        aggregate=aggregate,
        profile=profile,
        frame=frame,
        definition=definition,
    )

    if strategy in SILENT_STRATEGIES:
        sending = [
            candidate
            for candidate in plan["candidate_intents"]
            if candidate["kind"] in ("send_message", "send_material")
        ]
        if sending:
            raise PlanRejected(
                "SILENT_STRATEGY_PROPOSES_SEND",
                f"{strategy!r} may not propose {sending[0]['kind']}",
            )

    floored = _floor_a_reply_candidate(plan, strategy=strategy, profile=profile)
    return ValidatedPlan(
        document=plan, frame=frame, definition=definition, reply_floor_basis=floored
    )


def knowledge_query_as_written(plan: InferenceDocument) -> str | None:
    """字段里**原样**的那个串；缺席与 `null` 都是 `None`。

    **6b 的检索层读的是这一个，不是下面那个**，理由是一行也不能少的：
    「请求串不可用」必须留下一条 `unusable_request` 的审计行（ADR-0133 的四态之二），
    而下面那个函数把不可用读成 `None` —— 拿它当入口，
    「模型压根没请求」与「模型请求了但写歪了」就在数据里长成了一模一样。
    """

    value = plan.get("knowledge_query")
    return None if value is None else str(value)


def is_usable_query(text: str) -> bool:
    """一个请求串能不能用 —— **空白这条判据的唯一实现**。

    检索层还要加它自己的一条（长度上界，`retrieval.QUERY_MAX_CHARS`）：
    **界归花那笔字节的那一层**，契约层刻意不设（ADR-0133 / 独立审查 P1-1）。
    而「空白算不算能用」是契约层的事，所以它在这里，**两个调用方共用这一处**，
    不是各写一遍 `.strip()`。
    """

    return bool(text.strip())


def requested_knowledge_query(plan: InferenceDocument) -> str | None:
    """本轮请求检索的查询串；没请求就是 `None`。

    **「这一轮要不要检索」的判据只有这一处**（裁定三，`knowledge-base-schedule-2026-09-11.md`
    §5.2.1）。它存在的理由不是省一行 `plan.get(...)`，是**缺席、`null`、空白串
    三种写法必须收敛成一个答案**：分散在检索层、出口计数、审计行三处各读一遍，
    就是三处会各自漂移的判断，而其中任意两处不一致时，
    「模型没请求检索」与「请求了但平台没看见」在数据里长得一模一样
    （同形状的代价已经付过：T302 那条「understanding 被砍过没有按会话可查的记录」）。

    **「不可用的请求」在这里降级，不在校验器里变成拒绝**（独立审查 2026-09-12，P1-1）：
    空白串、只有空格的串，一律读成「这一轮不检索」。契约层也**不再**给这个字段设长度界 ——
    界在契约里意味着模型把提示词写长一点就让整份计划作废、客户这一轮收不到消息，
    而**裁定六**（`knowledge-base-schedule-2026-09-11.md` §5.3.2）说的是
    检索失败照常生成并发消息。**长度归花那笔字节的人管**（6b 的检索层）。

    **它答的是「这一轮有没有一个能用的查询串」**，所以不可用的一律读成 `None`。
    **6b 的检索层不用它** —— 那一层要把「不可用」写成一条审计行，
    因此读的是 `knowledge_query_as_written`。两个函数答两个不同的问题，
    而**空白的判据只有 `is_usable_query` 一处**。
    """

    written = knowledge_query_as_written(plan)
    if written is None or not is_usable_query(written):
        return None
    return written.strip()


def _validate_knowledge_query(*, plan: InferenceDocument, origin: str) -> None:
    """**一条闸，而且它只冲平台自己。**

    `origin == "safe_fallback"` 的计划是平台在模型计划被拒之后自己造的确定性安全阀，
    它没有「要查什么」的判断可言。允许它带查询串，等于让一条平台自己写的记录
    声称模型做过一个决定 —— 那是平台的 bug，**响亮地拒是对的**。

    **模型那一侧一条闸都没有，这是处置独立审查 P1-1 之后的样子。**
    第一版在这里为「空白查询串」抛 `PlanRejected`，理由是「请求了检索但没检到」
    与「模型没想好要查什么」该分得开 —— 那个区分要，**但代价定错了**：
    计划被拒 → 回落保守计划 → `no_message` → **客户这一轮收不到消息**，
    而它换来的只是一个更好看的理由码。审查方逐条指出这与本批自己论证
    「不设必填」的理由同源（模型少写一个键不该吃掉回复，那么把它写歪也不该），
    与**裁定六**（检索失败照常生成并发消息）正面冲突。

    **那个区分挪到了证据面上**：`requested_knowledge_query` 把不可用的请求读成
    「这一轮不检索」，而「请求串不可用」是 6b 的 `retrieval_evidence` 要分的
    第四态（前三态：没请求、请求了没检到、understanding 被砍过）。
    """

    value = plan.get("knowledge_query")
    if value is None:
        return
    if origin == "safe_fallback":
        raise PlanRejected(
            "FALLBACK_REQUESTS_RETRIEVAL",
            "a safe_fallback plan is platform-authored and never requests retrieval",
        )


def _validate_model_plan_against_profile(
    *,
    plan: InferenceDocument,
    registered: dict[str, Any],
    definition: dict[str, Any],
    frame: dict[str, Any],
) -> None:
    # `registered` is passed in rather than looked up here because the caller is
    # the one that knows which registry applies to this frame (B4-7).
    strategy = str(plan["selected_strategy_id"])
    # **`.get` 而不是下标，四处一起改（2026-09-13）。既有隐患，不是本批引入的。**
    #
    # `merchant-profile.schema.json` 给 `strategies` 补形状时**没有加 `required`**
    # （ADR-0137 裁定一步骤 1，收紧封闭性划在那一步之外），所以三个键都是**可选的**：
    # 一份只写了 `allowed_intent_kinds` 的 profile 完全合法、封得版。
    # 而这几处一直用下标 —— **schema 允许缺，代码假设有。**
    #
    # 三份现存配包都写全了三个键，所以今天碰不到。**而本批的两道新闸要读它们**，
    # 缺键就必须落在「闸拒绝这份计划」那条路上，不能落在「闸自己抛 `KeyError`」那条路上：
    # 前者留下一行带理由码的 `policy_decisions`，后者是一条 500 和一份没人看得懂的堆栈，
    # **两者在日志里长得不一样，排查成本差很多**。
    #
    # 缺席的语义是真话：`allowed_intent_kinds` 缺 = 这个策略不允许任何意图
    # （于是任何 candidate 都被拒，理由码正确）；`generation_constraints` 缺 =
    # 它不声明约束（于是计划里带任何约束都被拒，同上）。
    allowed_kinds = set(registered.get("allowed_intent_kinds", []))
    for candidate in plan["candidate_intents"]:
        if candidate["kind"] not in allowed_kinds:
            raise PlanRejected(
                "INTENT_KIND_NOT_ALLOWED_BY_STRATEGY",
                f"{candidate['kind']!r} is not allowed by strategy {strategy!r}",
            )

    declared_constraints = set(registered.get("generation_constraints", []))
    unknown = set(plan["generation_constraints"]) - declared_constraints
    if unknown:
        raise PlanRejected(
            "GENERATION_CONSTRAINT_NOT_DECLARED",
            f"strategy {strategy!r} does not declare {sorted(unknown)}",
        )

    # **闸二（ADR-0137 裁定一，2026-09-13）：声明的必填槽位空着，而计划既不填也不问 → 拒。**
    #
    # 图纸 §4.1：`advance_goal`「不能…跳过必填槽位」。ADR 把判据定成
    # **「不是跳过」的判据是「问了」，不是「填满了」** —— 一个槽位还空着而计划
    # **点名去问它**是合法的（那正是「根据未完成槽位选择下一要点」），
    # **空着又不问才是跳过**。
    #
    # **数据源是 `strategies[<选中策略>].required_slots`**，不是
    # `definition["information_slots"][].required` —— 后者由 `op == "complete"`
    # 那道闸读（2026-09-24 起读的是 `exit_conditions` 谓词，
    # 见 `_unmet_exit_conditions`）。**两个源、两个时点、两件事**：
    # 这一道每一次模型计划都跑，那一道只在收口时跑。
    #
    # **判据只看「点名了没有」，不看选了哪条打法**（用户 2026-09-13 重裁）。
    # 初版三条边界里写着「选了 `ask_slot` 且点名了那个槽位才算问了」，
    # 而那把**判据**（这一轮有没有去问）和**证据**（`required_slot_requests`
    # 有没有点名它）搞混了：实测 `ask_slot` 自己的 `required_slots` 在三份配包里
    # 都是 `[]`，于是那条边界描述的场景**这道闸根本走不到** ——
    # 选了 `ask_slot` 闸不触发，选了别的又永远不算「问了」，
    # `required_slot_requests` 变得与本闸无关，而闸退化成「一律拒」。
    #
    # **三条边界（重裁后）**：
    # 1. `required_slot_requests` 点名了那个缺的槽位 → **算问了**；
    # 2. 点名了**别的**槽位 → 不算（下面那条 `SLOT_NOT_IN_FRAME` 管的是另一件事：
    #    点名了本帧根本没有的槽位）；
    # 3. **自由文本里捎带问一句 → 不算** —— 平台读不出来，读出来也是猜。
    #
    # **「缺几个点名几个」取松**：点名**任意一个**空着的就放行。**这是一次取舍。**
    # 另一侧是「严」= 缺 N 个必须点名 N 个，于是「这一轮先问最要紧的那个」会被拒。
    #
    # **依据是实测，不是偏好**：严的成本是一次拒绝（拒绝路径不产出出站行，
    # 客户这一轮沉默），收益是零 —— `pending_question` **只记第一个被点名的槽位**，
    # 把 N 个都列出来不会给另外几个留下证据；而剩下那几个下一轮仍在提示词的
    # `current_frame.required_slots` 里。两种写法在**下游行为上没有差别**。
    #
    # **不要用「点名两个违反 `one_question`」当理由** —— 实测不成立
    # （这条依据在裁定与退回里各被提过一次，所以留痕在此，免得它被捡回来）：
    # 契约给 `required_slot_requests` 的是 `maxItems: 8`，**点名两个连合同都不违反**；
    # 而同一处还写着 `uniqueItems: true` —— 那条约束对一个**预期只有一项**的数组
    # 不可能被违反（没有第二项能与第一项重复），所以它的存在本身就是
    # **「预期多项」的证据**：点名多个是契约明确允许的形状，不是钻空子。
    # `one_question` 由 `ask_slot` 声明（而本闸对它不触发，它的 `required_slots` 是 `[]`），
    # 在 generation 步执行、量的是那句话里有几个问号；而 `required_slot_requests`
    # 的语义是「还缺这些」不是「这一轮全问」。
    #
    # 要改成严，改的是下面那个 `&`；`tests/contracts/`
    # `test_the_loose_reading_naming_one_missing_slot_is_enough` 会红，读它的 docstring。
    #
    # **只在模型计划上跑**：本函数只被 `origin != "safe_fallback"` 那一支调用。
    # 回退计划的 `required_slot_requests` 恒为空，跑这道闸会让它永远被拒 ——
    # 而它是被拒之后唯一的出路。
    #
    # ⚠️ **这道闸放行之后还有一个洞，今天没有检测方**（登记 T331）：
    # 计划**点名**了那个槽位，而发出去的那句话**没真问**。
    # `pending_question` 记的是「点名了 + 这一轮发了消息」，**它记的是声称不是问** ——
    # 于是模型可以每轮点名同一个槽位、每轮发别的话，
    # 而 `pending_question` 会一路显示「问过、没答」，**看起来像客户不回答**。
    required_slots = tuple(registered.get("required_slots", []))
    if required_slots:
        empty = tuple(
            name
            for name in required_slots
            if not str(frame["slots"].get(name) or "").strip()
        )
        asked = {str(slot) for slot in plan["required_slot_requests"]}
        if empty and not (set(empty) & asked):
            raise PlanRejected(
                "STRATEGY_SKIPS_REQUIRED_SLOT",
                f"strategy {strategy!r} declares required slots {sorted(empty)} that are "
                f"still empty, and the plan asks for none of them "
                f"(required_slot_requests={sorted(asked)})",
                # **结构化带出来，不让下游解析那句话**：连续第二次同码拒绝会建一条
                # 人工任务，而接手的人要知道卡在哪个槽位、模型选了哪条策略。
                context={
                    "empty_required_slots": sorted(empty),
                    "strategy_id": strategy,
                    "asked": sorted(asked),
                },
            )

    slot_names = {slot["name"] for slot in definition["information_slots"]}
    unknown_slots = set(plan["required_slot_requests"]) - slot_names
    if unknown_slots:
        raise PlanRejected(
            "SLOT_NOT_IN_FRAME",
            f"frame {definition['id']!r} has no slot {sorted(unknown_slots)}",
        )


def _validate_scheduled_event_changes(
    *, plan: InferenceDocument, aggregate: dict[str, Any], definition: dict[str, Any]
) -> None:
    """Check the model's **proposal**, and do not let that read as "handled".

    **ADR-0096 §四.2, kept deliberately after B4-2 changed what it means.** This
    used to be the only thing that happened to `scheduled_event_changes`: it was
    validated and then dropped, and the ADR's judgement of that was blunt --
    「校验一个从不持久化的东西，比不校验更糟：它让人以为那条路通了。」

    B4-2 built the producer, and it is **not this**: a `TimeRule` derives every
    `scheduled_events` row (`time_rules.py`). Nothing here reaches a table --
    the model's `due_at` and `semantic_key` are still dropped, now on purpose
    rather than by omission, because a proactive send whose timing the model
    chose cannot be reproduced under a virtual clock (RFC §17.2) or re-checked
    at consumption (RFC §6.4).

    **What the check is still worth**: a plan proposing an event of a kind the
    frame never declared is a plan that has misunderstood the frame, and that is
    worth rejecting whether or not anyone was going to act on the proposal.
    """

    changes = plan["scheduled_event_changes"]
    # `emit_kind`, not `kind`: B4-1 tightened `time_rules` to RFC 6.2, where the
    # rule's own identity (`id`) and the kind it emits became separate fields.
    allowlisted = {rule["emit_kind"] for rule in definition["time_rules"]}
    frame_ids = {str(frame["id"]) for frame in aggregate["target_stack"]}
    for creation in changes["create"]:
        kind = creation["kind"]
        if kind not in SCHEDULED_EVENT_KINDS:
            raise PlanRejected("EVENT_KIND_NOT_IN_CATALOG", f"{kind!r} is not a platform event")
        if kind not in allowlisted:
            raise PlanRejected(
                "EVENT_KIND_NOT_ALLOWED_BY_FRAME",
                f"frame {definition['id']!r} declares no time rule of kind {kind!r}",
            )
        if str(creation["target_frame_id"]) not in frame_ids:
            raise PlanRejected(
                "EVENT_TARGETS_UNKNOWN_FRAME",
                "scheduled event targets a frame outside the current stack",
            )


def _validate_stack_operation(
    *,
    plan: InferenceDocument,
    understanding: InferenceDocument,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    frame: dict[str, Any],
    definition: dict[str, Any],
) -> None:
    operation = plan["target_stack_operation"]
    op = str(operation["op"])
    if op not in TARGET_STACK_OPERATIONS:
        raise PlanRejected("STACK_OP_NOT_IN_CATALOG", f"{op!r} is not a stack operation")

    # **闸一（ADR-0137 裁定一，2026-09-13）：选了只许在帧内推进的打法，栈操作只能 `keep`。**
    #
    # 在这一行之前，图纸 §4.1 那句「`advance_goal` 不能切换目标」**没有任何执行点**：
    # 实测 `advance_goal` 在生产里只有三处 —— 词表、提示词示例、一句注释；
    # 而本函数**只看 `op`，从不看 strategy**，于是 `advance_goal` + `push` 一路放行。
    # 那是本仓「五条判据」第 1 类（约束声明了，运行期无人执行）的一个标本。
    #
    # **排在每个 op 的分支之前**，与本文件已有的那条理由同源
    # （`STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED` 排在 allowlist 之前）：
    # 它是更具体的那句话，也给出更有用的理由码。放在后面的话，
    # `push` 缺 `target_frame_definition_id` 之类的通用错会先说话，
    # 而那句话指不到「这条打法根本不该换帧」。
    #
    # **对 `safe_fallback` 也生效，而这不会堵死安全阀**：实测
    # `safe_fallback_plan`（`inference.py`）产出的 `op` 恒为 `keep`、
    # `selected_strategy_id` 取自 `SAFE_FALLBACK_STRATEGIES`（不含 `advance_goal`），
    # 所以这条闸对回退路径恒真。**不加 `origin` 判断是有意的**：
    # 加了就等于声明「平台自己的计划可以换帧」，而那句话没人裁定过。
    if str(plan["selected_strategy_id"]) in IN_FRAME_ONLY_STRATEGIES and op != "keep":
        raise PlanRejected(
            "ADVANCE_GOAL_MUST_STAY_IN_FRAME",
            f"strategy {plan['selected_strategy_id']!r} may only advance inside the current "
            f"frame; op {op!r} leaves it",
        )

    stack = aggregate["target_stack"]
    status = str(frame["status"])
    relation = str(understanding["interruption"]["relation"])
    target_definition_id = operation["target_frame_definition_id"]

    if op in ("keep", "complete", "abandon") and target_definition_id is not None:
        raise PlanRejected(
            "STACK_OP_TARGET_NOT_APPLICABLE", f"op {op!r} must not name a frame definition"
        )

    if op == "keep":
        if status != "active":
            raise PlanRejected("KEEP_ON_INACTIVE_FRAME", f"current frame status is {status!r}")
        return

    if op == "push":
        if status != "active":
            raise PlanRejected("PUSH_ON_INACTIVE_FRAME", f"current frame status is {status!r}")
        if target_definition_id is None:
            raise PlanRejected("PUSH_WITHOUT_TARGET", "push must name a frame definition")
        if relation not in PUSH_RELATIONS:
            raise PlanRejected(
                "PUSH_WITHOUT_INTERRUPTION",
                f"interruption relation {relation!r} does not justify a child frame",
            )
        if str(target_definition_id) == str(frame["definition_id"]):
            raise PlanRejected("PUSH_ONTO_SAME_FRAME", "child frame repeats the parent definition")
        # **The platform's frames are entered by the platform, never proposed**
        # (B4-7). `frame_definition` resolves them since this batch, so without
        # this the model could push the no-goal frame as an ordinary child --
        # a frame whose entire meaning is "there is no goal here" sitting on top
        # of a live goal, and one that `complete` is refused on, so the stack
        # could not be unwound past it either.
        if is_platform_frame(target_definition_id):
            raise PlanRejected(
                "PUSH_ONTO_PLATFORM_FRAME",
                f"{str(target_definition_id)!r} is a platform frame and cannot be pushed",
            )
        definition = frame_definition(profile, str(target_definition_id))
        # **进过就不许再进 —— 如果商家把这个帧声明成 `once`**（ADR-0146）。
        #
        # ⚠️ **在此之前模型的 `push` 没有任何重复限制**：上面那三条检查的是
        # 「不是平台帧、定义存在、栈没满」，**没有一条问过「这个帧进过没有」**。
        # 于是一个「报价」帧可以被推进去第二次、第三次，而挡着它的只有 `goal`
        # 里的一句话 —— 运维 2026-09-20 §八 实测过同一段话术一小时被引 3 次。
        #
        # **判据与平台推进那一侧是同一个**（`target_stack.frame_to_advance_into`
        # 的 `ever_entered`）：`definition_id` 在目标栈里出现过，**含已终结的帧**。
        # 两处一个判据，不是两套规则。
        #
        # 缺省 `repeatable` —— 存量包一个字不改，行为与今天逐字相同。
        # 异议帧正是要这一种：他可能提好几条疑虑，每一条都要再进一次。
        if str(definition.get("revisit") or "reenter") == "brief_answer" and (
            str(target_definition_id) in frames_ever_entered(aggregate)
        ):
            raise PlanRejected(
                "FRAME_ALREADY_ENTERED",
                f"{str(target_definition_id)!r} is declared `once` and this conversation "
                "has already been in it",
            )
        # **The cap counts frames that can still be worked, not history**
        # (independent review 2026-09-10, P0-1, third consequence).
        #
        # `MAX_STACK_DEPTH` bounds how deep an *interruption* may nest -- how
        # many live goals one conversation juggles. Counting terminal frames
        # made it bound something else: with B4-7 a normally finished
        # conversation sits at `[root completed, closing completed, no-goal
        # active]` = 3, so **every** later `push` was refused and the customer
        # could never open a new goal in that conversation again. That state is
        # reached by every conversation that ends tidily, with no exit signal
        # involved -- and it is worse than the handoff B4-7 replaced, because
        # there at least a person picked it up.
        #
        # Terminal frames are history: `complete` and `abandon` are final for
        # the frame that holds them, and nothing can move one back into play.
        live = [frame for frame in stack if frame["status"] not in TERMINAL_FRAME_STATUSES]
        if len(live) >= MAX_STACK_DEPTH:
            raise PlanRejected(
                "STACK_DEPTH_EXCEEDED",
                f"stack already holds {len(live)} frames that can still be worked",
            )
        return

    if op == "complete":
        # **A platform frame has no goal to complete** (B4-7, ADR-0099 §五.2).
        # Mirrored from `target_stack.apply_target_stack_operation`, and the
        # mirror is what makes it useful: refused here it becomes a reason code
        # on the no-send path, refused only there it would raise through the
        # retry budget. The slot gate below cannot do this job -- the no-goal
        # frame declares no slots, so "every required slot is filled" is
        # vacuously true for it.
        if is_platform_frame(frame["definition_id"]):
            raise PlanRejected(
                "COMPLETE_ON_PLATFORM_FRAME",
                f"{str(frame['definition_id'])!r} has no goal to complete",
            )
        # **核的终于是商家写的出口判据**（2026-09-24 批，用户裁定「建执行」）。
        #
        # 这个理由码从第一天起就叫 `EXIT_CONDITION_NOT_SATISFIED` ——
        # **名字一直在说该核出口条件，核的却是另一套**
        # （`information_slots.required` 全满）。两套在 carrier 包上双向分家：
        # 商家的出口只点一个槽，闸却要求四个 required 全满（比声明严）；
        # demo/third 的 closing 出口点了一个**非 required** 槽，闸对它视而不见
        # （模型自判 —— RFC §4.1 明令禁止的那种）。
        #
        # 今天起两套各归各：**required = 要收集什么**（ask 链 /
        # `STRATEGY_SKIPS_REQUIRED_SLOT` 读它，一个字没动），
        # **exit_conditions = 什么时候算完**（本闸读它）。
        unmet = _unmet_exit_conditions(frame=frame, definition=definition)
        if unmet:
            raise PlanRejected(
                "EXIT_CONDITION_NOT_SATISFIED",
                f"frame {definition['id']!r} has unmet exit conditions: {unmet}",
            )
        return

    if op == "abandon":
        # The same set the transition layer refuses `repair` on (T53). Written
        # once, in `target_stack`, so that "which relations are an exit signal"
        # cannot come to mean two different things in the two layers.
        if relation not in EXIT_SIGNAL_RELATIONS:
            raise PlanRejected(
                "ABANDON_WITHOUT_EXIT_SIGNAL",
                f"interruption relation {relation!r} does not justify abandoning the goal",
            )
        return

    if op in ("repair", "request_pop"):
        if len(stack) < 2:
            raise PlanRejected(
                "STACK_OP_REQUIRES_PARENT", f"op {op!r} needs a parent frame on the stack"
            )
        parent = stack[-2]
        parent_status = str(parent["status"])
        if op == "repair":
            # `repair` advances the parent out of repair_pending. Any other
            # parent status means the plan is describing a transition that does
            # not exist in the RFC state table.
            if parent_status != "repair_pending":
                raise PlanRejected(
                    "REPAIR_ON_UNPREPARED_PARENT",
                    f"parent frame status is {parent_status!r}, expected 'repair_pending'",
                )
            # T53, mirrored from `target_stack.repair`: RFC `:317` refuses a
            # return after an exit signal, read both from the durable status of
            # the child (`abandon` is written only on an exit relation, checked
            # above) and from this turn's relation. The reasoning for the two
            # criteria, and for why the withdrawn T1's `interruption_reason` was
            # not one of them, is in that function.
            if str(frame["status"]) == "abandoned" or relation in EXIT_SIGNAL_RELATIONS:
                raise PlanRejected(
                    "REPAIR_AFTER_EXIT_SIGNAL",
                    "the customer's exit signal refuses a return to the parent goal",
                )
            # N1, mirrored from `target_stack.repair`. **Both places or
            # neither**: this validator runs first, so a rule living only in the
            # transition would never be reached -- the plan would be accepted
            # here and then raise through the retry budget instead of landing on
            # the no-send path with a reason code.
            if str(frame.get("resume_confirmation_policy", "optional")) == "forbidden":
                raise PlanRejected(
                    "RESUME_CONFIRMATION_FORBIDDEN",
                    "the child frame's profile forbids resume confirmation",
                )
            return
        if status not in ("completed", "abandoned"):
            raise PlanRejected(
                "POP_BEFORE_CHILD_RESOLVED", f"child frame status is {status!r}"
            )
        # The gate the RFC puts on the return path: a child whose profile
        # declares `required` confirmation may not be popped until `repair` has
        # moved the parent to resume_confirmation_pending. Rejecting here rather
        # than in the transition keeps a bad proposal on the no-send path with a
        # reason code, instead of raising through the retry budget.
        # T53, mirrored from `target_stack.request_pop`: an exit signal exempts
        # the pop from the confirmation gate, because `repair` -- the only
        # producer of `resume_confirmation_pending` -- is now refused after one.
        # Without the exemption the frame could never leave the stack. Both
        # readings of the signal are needed here for the same reason they are
        # needed there: the durable one for a child the customer ended, the
        # this-turn one for a child that ended normally before the customer did.
        exit_signal = (
            str(frame["status"]) == "abandoned" or relation in EXIT_SIGNAL_RELATIONS
        )
        if (
            str(frame.get("resume_confirmation_policy", "optional")) == "required"
            and not exit_signal
            and parent_status != "resume_confirmation_pending"
        ):
            raise PlanRejected(
                "POP_BEFORE_RESUME_CONFIRMATION",
                f"child requires confirmation but parent is {parent_status!r}",
            )
        return


def _unmet_exit_conditions(*, frame: dict[str, Any], definition: dict[str, Any]) -> list[str]:
    """商家写的出口判据里，这一刻还没满足的那些（2026-09-24，用户裁定）。

    谓词是闭合的两种（schema 钉着形状，写错封版即拒）：

    * `{slot, present: true}` —— 槽有值（`None` 与空串算没有，
      与 `apply_slot_updates` 拒 `null` 的口径一侧）；
    * `{slot, equals: 值}` —— 槽等于某值（字符串比较，
      与 `advance_when` 的 `equals` 逐字同一个读法）。

    **前身叫 `_missing_required_slots`**：核 `information_slots.required` 全满 ——
    那是「要收集什么」的表，不是「什么时候算完」的判据，
    两者恰好在 demo 包上大体重合，于是这个错活了很久。
    required 那半的读者（ask 链 / `STRATEGY_SKIPS_REQUIRED_SLOT`）一个没动。
    """

    slots = frame.get("slots") or {}
    unmet: list[str] = []
    for condition in definition.get("exit_conditions") or []:
        name = str(condition.get("slot"))
        value = slots.get(name)
        if "equals" in condition:
            if value is None or str(value) != str(condition["equals"]):
                unmet.append(f"{name} != {condition['equals']!r}")
        elif value in (None, ""):
            unmet.append(f"{name} is empty")
    return unmet


def _slots_this_frame_has(
    names: Sequence[object], *, aggregate: dict[str, Any], profile: dict[str, Any]
) -> tuple[str, ...]:
    """`names` 里**当前帧真有**的那些槽位，保持传入顺序。

    **为什么要过滤，而不是原样传过去**：回退计划的 `origin == "safe_fallback"`，
    而 `SLOT_NOT_IN_FRAME` 与闸二**在同一个函数里**
    （`_validate_model_plan_against_profile`），那个函数对回退整条豁免 ——
    **所以回退点名一个本帧没有的槽位，没有任何东西会拦它**，
    它会一路写进 `pending_question.slot`。

    而这种情形是可能的：闸二的「空着」判据读的是帧上的槽位值字典，
    **一个策略声明的槽位可以存在于「允许它的另一个帧」里**
    （§4.1 那道封版闸要求的正是「某个允许它的帧」，不是「每个帧」）。
    于是那个名字在本帧永远为空、永远被算作缺，而本帧并没有这个槽位。

    **过滤而不是拒绝**：拒绝会让安全阀自己失效，而安全阀是被拒之后唯一的出路。
    过滤之后列表可能变空 —— 那就退回到接线之前的形态（说不出内容），
    **而那是正确的降级**：与其点名一个本帧不存在的槽位，不如什么都不点名。
    """

    stack = aggregate.get("target_stack") or []
    if not stack:
        return ()
    frame = stack[-1]
    definition = frame_definition(profile, str(frame["definition_id"]))
    available = {str(slot["name"]) for slot in definition["information_slots"]}
    return tuple(str(name) for name in names if str(name) in available)


def _understanding_has_content(understanding: Mapping[str, Any] | None) -> bool:
    """理解步这一轮**听懂了东西吗** —— `ask_clarification` 那一支的判据（ADR-0138）。

    「有内容」= `facts` / `intents` / `slot_updates` **任意一项非空**。
    三项都空意味着模型这一轮什么都没提取出来，
    **那时一句「我确认一下」没有可确认的对象，只能是套话** ——
    而套话正是 T339 判定的那个坏形状。

    **不看 `confidence`**：那个结构四个字段今天零读点、其中三个连值都是常量
    （批 5 / B1-11），拿它当判据等于拿一个常数当判据。
    """

    if not understanding:
        return False
    return any(understanding.get(key) for key in ("facts", "intents", "slot_updates"))


def plan_or_fallback(
    *,
    plan: InferenceDocument,
    understanding: InferenceDocument,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    fallback_strategy: str | None = None,
    required_strategy: str | None = None,
    event_type: str = "",
) -> tuple[ValidatedPlan, PlanRejected | None]:
    """Return a usable plan, replacing a rejected one with a deterministic plan.

    The fallback is validated by the same rules, so a fallback that is itself
    illegal fails loudly here rather than reaching the outbox.

    **`fallback_strategy=None` 的意思是「按数据选」**（T339 第二批，用户裁 (c)）：
    拒绝那一处点得出缺哪些槽位 → `ask_slot`（这一轮把问题问出去）；
    点不出 → `no_message`（照旧静默）。
    **不是「默认改成 ask_slot」** —— 那会让 59 个点不出槽位名的理由码
    「发一条消息而不说问什么」，而那是 T339 认定的坏形状本身。

    显式传一个打法仍然有效，那条路没有变。
    """

    try:
        return validate_plan(
            plan=plan,
            understanding=understanding,
            aggregate=aggregate,
            profile=profile,
            required_strategy=required_strategy,
        ), None
    except PlanRejected as rejection:
        slot_requests = _slots_this_frame_has(
            rejection.context.get("empty_required_slots") or [],
            aggregate=aggregate,
            profile=profile,
        )
        # **打法按「说得出内容吗」选，不是一个固定默认**（T339 第二批）。
        #
        # 第一批把「缺哪些槽位」接到了 `required_slot_requests` 上，
        # 而**只有点得出名字的那种拒绝才有话可说** —— 拒绝共 60 个理由码里，
        # 今天只有闸二那一条带得出槽位名（它的 `PlanRejected.context`）。
        # **给别的拒绝也选 `ask_slot`，就是让它「发一条消息而不说问什么」** ——
        # `_record_pending_question` 会早退，模型只能自己编要问什么，
        # 而那正是 T339 判定「今天的默认不是一次选择」时说的那个坏形状。
        #
        # 调用方仍可显式指定（时间规则那条路要的就是这个）。
        #
        # ## 2026-09-15（D4，ADR-0138）：二分改三分
        #
        # T339 第二批定的是二分「点得出 -> `ask_slot`，点不出 -> 静默」，
        # **而静默是当时唯一诚实的选项** —— 词表里没有「请客户澄清」这个打法。
        # 现在有了（`ask_clarification`），于是那一支拆成两种：
        #
        #     点得出槽位                       -> ask_slot
        #     点不出 · 而 understanding 有内容  -> ask_clarification
        #     understanding 也空               -> no_message
        #
        # **第三支保留了 T339 的判断，而不是推翻它** —— 只是把它缩小到
        # 它真正成立的那个范围：「连听懂了什么都没有」时没有可确认的对象，
        # 那句话**只能**是套话，而套话正是 T339 判定的坏形状。
        # 「听懂了个大概、只是拼不出合法计划」那一种，沉默是过度的。
        #
        # **判据为什么是 `understanding` 有没有内容**：那句澄清由生成步写，
        # 而生成步在这条路上拿得到 `understanding`
        # （`inference_model.generate` 无条件传 `_bounded_understanding`，不分计划来源）。
        # 所以模型手里有「它听懂了什么」，写得出一句具体的确认；
        # **`understanding` 空着时它手里什么都没有。**
        #
        # ## 第四个条件是跑出来的，不是设计出来的：**定时回合不澄清**
        #
        # 上面三分在客户回合上成立，**而定时回合没有可澄清的对象** ——
        # 那一轮是平台自己发起的（静默检查之类），**客户刚才什么都没说**。
        # 在那里发一句「您刚才是说…？」不是澄清，是**无来由的主动消息**。
        #
        # 它是 D4 接线时被 `test_a_timer_plan_that_names_another_strategy_is_refused`
        # 抓出来的：那条用例红在 `KeyError: 'id'`，因为新路径开始产出候选意图了。
        # **红的表象是夹具缺一个键，真因是「定时回合不该说这句话」。**
        #
        # ## 判据是 `event_type`，**不是 `required_strategy`** —— 第一版取错了
        #
        # 第一版用 `required_strategy is None` 当「这是客户回合」，
        # 依据是调用点那段注释（「every timer turn would fall through to
        # `no_message`」）。**那句话不是判据的定义**：
        # `pipeline._required_strategy` 的 docstring 写明它在**两种**情况下返回 `None` ——
        # 客户消息，**以及「定时器挂着时 release 被取代、那条规则不再声明打法」**。
        # 于是第二种情况下 `required_strategy` 也是 `None`，
        # **而那仍然是一个定时回合** —— 第一版会在那里发出那句澄清，
        # 正是这个条件要防的事，**而它在生产里可达**（release 可以在定时器挂着时被取代）。
        #
        # 权威信号是封包自己的 `event_type`，与 `pipeline._timer_event_kind` 用的同一个。
        #
        # ## 判据是**正向的**：`== "customer_message"`，不是 `!= "timer"`
        #
        # **2026-09-16 改。** D4 落地时写的是 `!= "timer"`，而
        # `message-envelope.schema.json` 把 `event_type` 声明成**六个值**：
        # `customer_message · channel_event · timer · business_event ·
        # delivery_receipt · internal`。逐个取值实跑过（独立复核 2026-09-15，
        # D4 §三）：**那四种非 timer 非客户的值，每一种都选到 `ask_clarification`** ——
        # 而它们**没有一种是客户在说话**，这条路的后果是**平台主动对客户
        # 说一句「您刚才是说…？」**。
        #
        # **同一个文件三天前刚写下过这条教训**：`pipeline.py` 那段
        # （独立审查 2026-09-12 P3-2 留下的）逐字写着「**正向写，不是列举要拒的**
        # …… 今天够不着，**但极性是反的 —— 下一个 event_type 会默认被放行**」。
        # D4 在新代码里把它重现了一次，**而这一次的后果比那一次重**：
        # 那次是一条记忆被写下来，这次是平台开口。
        #
        # **今天仍然够不着**（生产只有 `ingress/adapter.py` 产 `customer_message`、
        # `worker/scheduler.py` 产 `timer`），所以这不是修一个线上缺陷，
        # **是修一个默认值**：正向写时，新增的 event_type 默认落到 `no_message`，
        # 那是「不说话」，是安全的那一侧。
        if fallback_strategy:
            chosen = fallback_strategy
        elif slot_requests:
            chosen = "ask_slot"
        elif event_type == "customer_message" and _understanding_has_content(understanding):
            chosen = "ask_clarification"
        else:
            chosen = "no_message"
        fallback = safe_fallback_plan(
            reason_code=rejection.reason_code,
            strategy_id=chosen,
            aggregate=aggregate,
            rejected=plan,
            manual_review_reason=rejection.detail[:256],
            # **拒绝那一处知道缺哪些槽位，回退就点名它们**（T339 第一批）。
            # 只取**本帧真有**的那些 —— 详见 `_slots_this_frame_has` 的 docstring：
            # 回退计划不经过 `SLOT_NOT_IN_FRAME`，所以这里是唯一的过滤点。
            slot_requests=slot_requests,
        )
        return (
            # **The fallback is validated without `required_strategy`**, and
            # that is not a loophole: `safe_fallback_plan` sets
            # `origin="safe_fallback"`, which takes the branch above that the
            # rule check does not live in. Passing it would be a statement that
            # a rule can constrain the platform's own safety valve, which it
            # cannot -- the valve exists for the case where the model's answer
            # was refused.
            validate_plan(
                plan=fallback,
                understanding=understanding,
                aggregate=aggregate,
                profile=profile,
            ),
            rejection,
        )


def validate_generation(
    *,
    generation: InferenceDocument,
    plan: ValidatedPlan,
    materials: Mapping[str, str] | None = None,
    knowledge: Mapping[str, str] | None = None,
    assets: Collection[str] | None = None,
    #: 传一个列表进来 = **「计划没提名的那条丢掉，别整轮拒」**。
    #:
    #: 丢掉的意图会被 `extend` 进去，**调用方必须记录它们** ——
    #: 一条被悄悄丢掉的意图与一条被拒的意图，排查成本差一个量级。
    #: 不传（默认）保持原样：整份拒。
    dropped: list[dict[str, Any]] | None = None,
    #: 传一个列表进来 = **「复述了素材的那个文本片段丢掉」**。
    #:
    #: 与 `dropped` 同一个形状：不传（默认）就不查 —— 现有调用方一点不变，
    #: 而想要这件事的**必须接下记录的责任**。
    echoed: list[dict[str, Any]] | None = None,
    #: 客户在这通对话里**自己说出过的那些数**（`{"1888", "10"}` 这种）。
    #:
    #: 由管线从 `inbox_events` 现读 —— **不是模型报的**，与 `delivered_materials`
    #: 同一条理由：模型猜错了没有任何东西会红。
    customer_numbers: Collection[str] | None = None,
    #: 上一轮我们对这个客户说的那句话（管线已经为提示词读过一次，这里复用）。
    last_outbound: str = "",
    #: 传一个列表进来 = **「说不出来源的那一段丢掉」**（5.7 两件）。
    #:
    #: 与 `dropped` / `echoed` 同一个形状：不传（默认）就不查。
    #: **而传了它就必须一起传 `customer_numbers`** —— 见下面那个 `ValueError`。
    unsourced: list[dict[str, Any]] | None = None,
    #: **这一轮真的递给写字那一步的那几条记忆的 ref**（T363）。
    #:
    #: 默认 `None`：**没递过记忆的回合，任何 `memory:` 引用都是编的**
    #: （fail-closed，与 `knowledge` 那一族逐字同一个默认）。
    remembered: Collection[str] | None = None,
    #: 传一个列表进来 = **「许了而这一轮没兑现的那一段丢掉」**（运维 §10.4 判据 2）。
    #:
    #: 与 `dropped` / `echoed` / `unsourced` 同一个形状：不传（默认）就不查，
    #: 传了就接下记录的责任。
    unkept: list[dict[str, Any]] | None = None,
) -> InferenceDocument:
    """Prove a generation result against the plan that authorised it.

    `knowledge` 是**这一轮真的送进提示词**的那几条知识库命中（`ref → text`），
    由管线从 `runtime.knowledge_bound.bound_for_prompt` 的返回值建 ——
    **不是检索层返回的全部命中**。两者在「装不下丢了一条」的回合上不同，
    而闸要按模型**看见过什么**判，不是按平台**检到过什么**判（ADR-0135）。

    默认 `None`：**没送过知识库的回合，任何 `knowledge:` 引用都是编的**（fail-closed）。
    """

    validate_inference_contract("generation-result", generation)

    if str(generation["strategy_id"]) != plan.strategy_id:
        raise GenerationRejected(
            "GENERATION_CHANGED_STRATEGY",
            f"plan selected {plan.strategy_id!r}, generation used {generation['strategy_id']!r}",
        )

    planned_kinds = [candidate["kind"] for candidate in plan.document["candidate_intents"]]
    # **计划没提名的那些：丢掉，还是整轮拒？**
    #
    # 用户 2026-09-17 裁定：「客户说了话就要有回复。计划与生成谈不拢是
    # 我们内部的事，不该以『对客户沉默』结账。」运维实测这一族九次，
    # **今天最大的单一沉默来源**。
    #
    # **默认仍然整份拒**（`dropped is None`）—— 现有每个调用方一点不变。
    # 只有显式传一个收集器的调用方才走丢弃这条路，而那样它**必须接住并记录**，
    # 拿不到「悄悄变宽松」这个结果。
    if dropped is not None:
        # ⚠️ **按条数留，不只按 kind 留**（运维缺陷 11，2026-09-23）。
        #
        # **第一版只问「这个 kind 在计划里吗」，于是它接不住重复。**
        # 计划列 `[send_message, remember]`、生成产出
        # `[send_message, send_message, remember]` 时 —— 每个 kind 都在计划里，
        # **一条都丢不掉**，而紧接着那条 `len(produced) > len(planned)` 无条件抛，
        # 整份拒、**0 条出站、客户侧没反应**。
        #
        # stage 实测必现：同一句客户消息发两次，两次 `detail` 逐字相同
        # （`plan allowed 2 intents, generation produced 3`），
        # 而两次模型输出并不相同 —— **文字每次都不一样，形状每次都一样**。
        #
        # 这一支属于「**想多回一句话**」，不是「引了没递给它的东西」。
        # `test_disagreement_is_not_silence` 的 docstring 自己把这条线画得很清楚，
        # 而代码把它放在了整份拒那一侧。用户 2026-09-17 的裁定管的正是这一类：
        # 「计划与生成谈不拢是我们内部的事，**不该以『对客户沉默』结账**。」
        allowance = Counter(planned_kinds)
        kept = []
        for intent in generation["proposed_intents"]:
            kind = intent["kind"]
            if allowance[kind] > 0:
                allowance[kind] -= 1
                kept.append(intent)
            else:
                # **超出计划条数的那几条**，以及计划里根本没有的那些 kind，
                # 走同一条路：丢掉并记下来。**保留的是靠前的那几条** ——
                # 提示词说的是「they go out in the order listed」，
                # 所以丢尾巴才是丢「多出来的那一句」。
                dropped.append(intent)
        generation["proposed_intents"] = kept
    produced_kinds = [intent["kind"] for intent in generation["proposed_intents"]]
    for kind in produced_kinds:
        if kind not in planned_kinds:
            raise GenerationRejected(
                "GENERATION_ADDED_INTENT", f"{kind!r} was not a planned candidate intent"
            )
    if len(produced_kinds) > len(planned_kinds):
        # **传了收集器的调用方到不了这里** —— 上面那段已经按条数收过。
        # 留着它是给 `dropped is None` 的调用方：那条路上「悄悄变宽松」
        # 不是一个可选项，而整份拒是它唯一能给的答案。
        #
        # ⚠️ **报里带上两张 kind 清单**（运维 2026-09-23 点名要的）：
        # 光报数说不出「多的是第二条 `send_message`」还是
        # 「多的是第二条 `remember`」，而那两种对应完全不同的处置。
        raise GenerationRejected(
            "GENERATION_DUPLICATED_INTENT",
            f"plan allowed {len(planned_kinds)} intents, generation produced "
            f"{len(produced_kinds)}; planned={planned_kinds} produced={produced_kinds}",
        )

    if plan.is_silent and generation["customer_visible_segments"]:
        raise GenerationRejected(
            "SILENT_STRATEGY_PRODUCED_CONTENT",
            f"strategy {plan.strategy_id!r} must not produce customer-visible content",
        )

    _validate_material_refs(generation=generation, plan=plan, materials=materials)
    # **记忆那一族的引用检查**（T363）—— 与知识库那条并排，同一个 fail-closed。
    _validate_memory_citations(generation=generation, offered=remembered)
    if echoed is not None:
        # **在 ref 校验之后**：先证明引的那几条是这一轮递过的，
        # 再拿它们的正文去比 —— 顺序反过来会拿一条不该出现的素材当尺子。
        drop_echoed_text_segments(generation, materials=materials, dropped=echoed)
    if unsourced is not None:
        # **传了 `unsourced` 就必须给来源集合。** 这不是图严格：
        # 少传一个参数而默认「空集」，等于把每一个数都判成没来源，
        # 会把整轮拒成沉默；默认「不查」又会让调用方以为查了而其实没查。
        # **两个默认都是错的，所以这里不给默认 —— 让它当场报错。**
        # （`CLAUDE.md` 第三条：多传/少传一个不报错而安静给出看起来正常的结果。）
        if customer_numbers is None:
            raise ValueError(
                "validate_generation(unsourced=[...]) also needs customer_numbers=...; "
                "neither 'assume nothing was said' nor 'skip the check' is a safe default"
            )
        drop_claims_without_a_source(
            generation,
            customer_numbers=customer_numbers,
            last_outbound=last_outbound,
            dropped=unsourced,
        )
    if unkept is not None:
        # **在 5.7 那一趟之后**：兑现与否按**活下来的那几段**判。
        # 顺序反过来会拿一段马上要被丢掉的 `material_ref` 当兑现的证据 ——
        # 而那一段不会发出去。（今天 5.7 不丢素材段，所以两种顺序结果相同；
        # 写成这个顺序是因为**它在那条前提变了的时候仍然对**。）
        drop_promises_this_turn_does_not_keep(generation, dropped=unkept)
    _validate_knowledge_citations(generation=generation, offered=knowledge)
    _validate_constraints(generation=generation, plan=plan, materials=materials or {})
    # **这一轮没递给它的文件，一律不许发**（09-17 补），
    # 与上面那条 `GENERATION_MATERIAL_REF_UNKNOWN` 逐字同源。
    #
    # 在此之前这条路是敞的：`_send_material_payload_refusal` 只查 ref 语法，
    # 派发时按 slug 直接查目录 —— **没有任何东西问过「这一轮递给它了吗」**。
    #
    # 它一次兜住三件：
    #
    # * **「主动只发一次」此前只滤候选、不拦发送** —— 模型记着上一轮那个 ref
    #   就能再发一次，而那正是用户 09-17 裁定要防的；
    # * 计划在换帧**之前**的帧里挑候选、生成用换帧**之后**的清单，两份不一致时拒；
    # * 本帧没声明而目录里有的（别的帧的资料），发不出去。
    #
    # **`None` 与空集是两件事**（与 `materials` 那条同源）：`None` = 调用方
    # 没走资料这条路，不施加；空集 = 这一轮递出去零份，那么任何 ref 都是编的。
    if assets is not None:
        for intent in generation.get("proposed_intents", []) or []:
            if str((intent or {}).get("kind")) != "send_material":
                continue
            reference = str(((intent or {}).get("typed_payload") or {}).get("ref") or "")
            if reference not in assets:
                raise GenerationRejected(
                    "GENERATION_ASSET_NOT_OFFERED",
                    f"asset {reference!r} was not offered this turn "
                    f"(offered: {sorted(assets)})",
                )

    return generation



#: 一条文本片段与同一条消息里引用的素材共享这么多**连续**字符，就算复述。
#:
#: **实测 20**（用户 2026-09-17 16:40：模型那句与 `demo-trial-how` 逐字重合
#: 「：企微账号授权、话术终审、对外资料原件。」）。而合理的部分呼应
#: （模型说「一个月 1888」而素材里也有这四个字）只有 8 ——
#: **12 把这两种分开**。中文里 12 个连续字逐字相同，不抄是写不出来的。
ECHOED_MATERIAL_CHARS = 12

#: 复述那件事的理由码。
ECHOED_MATERIAL_REASON_CODE = "GENERATION_ECHOED_A_QUOTED_MATERIAL"


def _longest_shared_run(left: str, right: str) -> str:
    """两段文字共享的最长**连续**片段。

    连续而不是词集：「配合三件」与「配合三件事」这种改写正是要抓的，
    而按词集算会被一个「事」字骗过去。
    """

    best = ""
    for start in range(len(left)):
        end = start + len(best) + 1
        while end <= len(left) and left[start:end] in right:
            best = left[start:end]
            end += 1
    return best


def drop_echoed_text_segments(
    generation: InferenceDocument,
    *,
    materials: Mapping[str, str] | None,
    dropped: list[dict[str, Any]],
) -> None:
    """引了素材，就不许再用自己的话把同一件事说一遍。

    ## 它抓的是一条已经存在的规则

    生成步的系统提示逐字写着「do not copy, shorten, translate, merge or
    reword a material into a text segment」—— **而违反它此前不会红**。
    2026-09-17 实测：同一条消息里模型先复述了一遍 `demo-trial-how`，
    系统又把原文贴上，客户读到同一件事两遍。

    ## 丢片段，不退回沉默

    与「谈不拢逐条拒」同一条纪律：**客户拿到的是商家审过的那句原文**，
    而不是零字（用户 2026-09-17 裁定）。

    **只在同一条消息里既引了素材又有文本片段时才比** —— 没有引用就没有复述，
    而那时模型用自己的话回答正是我们要的。

    ## 草稿与载荷都查

    `customer_visible_segments` 是草稿，`typed_payload.content.segments`
    是真的进出站的那份 —— 与 `_validate_material_refs` 同一条理由：
    **只查草稿会让真正发出去的那份漏网**。
    """

    approved = materials or {}

    def prune(segments: list[Any], where: str) -> None:
        quoted = [
            approved.get(str(segment.get("content_or_ref")), "")
            for segment in segments
            if isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
        ]
        quoted = [text for text in quoted if text]
        if not quoted:
            return
        kept: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict) or segment.get("type") == MATERIAL_REF_SEGMENT_TYPE:
                kept.append(segment)
                continue
            own = str(segment.get("content_or_ref") or "")
            run = max((_longest_shared_run(own, text) for text in quoted), key=len, default="")
            if len(run) >= ECHOED_MATERIAL_CHARS:
                # **记 ref 与重合长度，不记正文** —— 正文是客户可见的话，
                # 而审计行会被人翻、被导出。
                dropped.append({"where": where, "shared_chars": len(run)})
                continue
            kept.append(segment)
        segments[:] = kept

    prune(generation["customer_visible_segments"], "draft")
    for intent in generation["proposed_intents"]:
        payload = intent.get("typed_payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        segments = content.get("segments") if isinstance(content, dict) else None
        if isinstance(segments, list):
            prune(segments, str(intent.get("kind")))


#: 「我们把上一条原样又说了一遍」的门槛：连续 20 个字。
#:
#: **比 `ECHOED_MATERIAL_CHARS`（12）高，因为尺子的另一端不同。**
#: 那一把量的是素材原文与我们自己的话；这一把量的是我们**自己上一条**与这一条，
#: 而两条相邻的话本来就共享更多（产品名、「前十天免费」、同一个称呼）。
#:
#: **实测**：运维 §5.7 那两轮（06:18:58 / 06:30:49）是**整句逐字相同**，
#: 连续重合 40 字；而同一份记录里那条**合理**的跨轮呼应
#: （「企微账号授权、话术终审、对外资料原件」）是 18 字。**20 把这两种分开。**
REPEATED_OUTBOUND_CHARS = 20

#: 5.7 两件各自的理由码。**分开两个，因为修法不同**：
#: 一个要改的是「别替客户认事实」，一个要改的是「别把上一条再说一遍」。
UNFOUNDED_CUSTOMER_FACT_REASON_CODE = "GENERATION_ASSERTED_AN_UNFOUNDED_CUSTOMER_FACT"
REPEATED_OUTBOUND_REASON_CODE = "GENERATION_REPEATED_THE_LAST_OUTBOUND"

#: 模型自己写的那几段，用了另一种语言（T258）。
#:
#: **2026-09-10 实测**：同一轮三条回复里一条英文两条中文，英文那条逐字是
#: `"I couldn't read the attached file, so I don't have your request yet."`
#: 根因是**零约束** —— 配包里没有语言字段，闭合词表里也没有对应的词。
REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE = "GENERATION_REPLIED_IN_ANOTHER_LANGUAGE"

#: 判它要几个拉丁词。**四个**，而这个数是为了不误伤定下的。
#:
#: **下面每个数都是跑出来的**（同一条判据：摘链接、再数 `[A-Za-z]{2,}`）：
#:
#: | 真实会出现的 | 词数 |
#: |---|---:|
#: | `1888` | 0 |
#: | `https://example.com/pricing-sheet` | **0**（摘掉链接之后；不摘是 5） |
#: | `OK` / `Excel` / `FastAgent v2` | 1 |
#: | 「用 Excel 打开那份 pricing sheet」 | 3 —— **而它有汉字，前一半就短路了** |
#: | 实测那句英文 | **11** |
#:
#: ⚠️ `FastAgent v2` 是 **1** 不是 2：`v2` 只有一个字母，够不上一个「词」。
#: 这张表第一版把它写成 2、把那句英文写成 12，**两个都是照直觉写的** ——
#: 阈值是照着这张表定的，所以表里的数错了，阈值就没有依据。
#:
#: **宁可漏一句短的，不可拦一句该发的** —— 与本模块那条
#: 「说错比不说贵」相反的方向：那一条管的是**替客户认账**（说错伤客户），
#: 这一条管的是**用错语言**（拦错伤的是一条本来能发的回复）。
#: 两种错的代价不对称，所以两条闸的松紧方向也不同。
LATIN_WORDS_FOR_ANOTHER_LANGUAGE = 4

#: 一段里有没有中日韩汉字。`\u4e00-\u9fff` 是中文常用区。
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: 连续两个以上的拉丁字母算一个「词」。
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

#: 链接先摘掉再数 —— `https://example.com/pricing-sheet` 拆出来是五个「词」，
#: 而它**不是一句英文**。不摘的话一条带链接的中文回复会被误判。
_URL = re.compile(r"https?://\S+|www\.\S+")


def _is_another_language(text: str) -> bool:
    """这一段是不是用另一种语言写的。

    **判据是「一个汉字都没有，而拉丁词够多」** —— 两半都要，缺一不可：

    * 只看「没有汉字」：`1888`、`OK`、一个 ref 都会中；
    * 只看「拉丁词多」：一句中英混排的正常回复（「用 Excel 打开那份
      pricing sheet」）会中，而它是中文。

    ⚠️ **商家自己的素材不走这里** —— 调用方那个循环对
    `material_ref` 段直接放行。所以一份英文素材照样发得出去，
    **本闸管的只是模型自己写的字**。
    """

    if _CJK.search(text):
        return False
    return len(_LATIN_WORD.findall(_URL.sub(" ", text))) >= LATIN_WORDS_FOR_ANOTHER_LANGUAGE

#: 一句「这位客户已经在付的钱」长什么样。
#:
#: **只抓带「的 / 了 / 过」的那几种**（您付的、您已经交了、您花过）。
#: 「您需要付 1888」「一个月 1888」**不抓** —— 那是报价，是销售该说的话。
#: 分界线就在这里：**报一个价 ≠ 替客户认下一笔支出。**
#:
#: 必须带一个数，因为**数是唯一能回去核的东西** ——
#: 「您付的钱」核不了，「您付的那 1888」能核（客户说过这个数吗）。
#: 这条限制是自觉的，写在这里而不是留给下一个人去猜。
_CUSTOMER_PAYMENT_CLAIM = re.compile(
    r"[您你][^。！？；\n]{0,6}?(?:已经|正在|在)?(?:付|交|花|掏)[的了过]"
    r"[^。！？；\n]{0,8}?(\d[\d,，.]*)"
)


def _normalised_number(text: str) -> str:
    """`1,888.` 与 `1888` 是同一个数。"""

    return re.sub(r"[,，]", "", text).rstrip(".")


def _customer_visible_segment_lists(
    generation: InferenceDocument,
) -> list[tuple[str, list[Any]]]:
    """客户真的会看到的那几张段列表：草稿一张，每个提案载荷各一张。

    **草稿与载荷都要走**，与 `drop_echoed_text_segments` 逐字同一条理由：
    `customer_visible_segments` 是草稿，`typed_payload.content.segments`
    是真的进出站的那份 —— **只查草稿会让真正发出去的那份漏网**。
    """

    lists: list[tuple[str, list[Any]]] = [
        ("draft", generation["customer_visible_segments"])
    ]
    for intent in generation["proposed_intents"]:
        payload = intent.get("typed_payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        segments = content.get("segments") if isinstance(content, dict) else None
        if isinstance(segments, list):
            lists.append((str(intent.get("kind")), segments))
    return lists


def drop_claims_without_a_source(
    generation: InferenceDocument,
    *,
    customer_numbers: Collection[str],
    last_outbound: str,
    dropped: list[dict[str, Any]],
) -> None:
    """运维 §5.7 两件：替客户认下没说过的支出 · 把上一条原样再说一遍。

    ## 它抓的不是复述

    复核 2026-09-18 §一实测过：`no-echo` 那支**盖不住这一条**。
    生成步的系统提示禁的是五件（`copy, shorten, translate, merge or reword`），
    **那一支守的是 `copy`**（连续重合 ≥12 字），而 §5.7 那句与三条价格素材的
    最长重合分别是 **7 / 5 / 2** —— 靠连续子串这把尺子量不出来。
    **这里量的是另一样东西：这句话断言了一件关于客户的事，而它没有来源。**

    ## 为什么门槛是「客户自己说过这个数」

    因为**我们这边的来源救不了它**。1888 确实在配包素材里 ——
    素材说的是「价格是一个月 1888」，一句标价；
    发出去的是「**您付的那** 1888」，一句关于这位客户的事实。
    **同一个数，来源合法，断言不合法。** 能把两者分开的只有一件事：
    客户自己说没说过。

    ## 一条不剩时整份拒，而这与「不许以沉默结账」不矛盾

    「客户说了话就要有回复」那条裁定说的是**计划与生成谈不拢** ——
    那是我们内部的事，不该让客户替我们的分歧买单。
    **这里不是内部分歧，是内容错了**：
    发出去的那句话替客户认下了一笔他没说过的支出。

    **说错比不说贵。** 所以丢到一条不剩时，这里整份拒（留痕、可查），
    而不是发一条空消息，也不是把那句话放过去。
    ⚠️ **这是本仓唯一一处「宁可沉默」的判断**，写在这里等着被推翻。
    """

    said = {_normalised_number(str(number)) for number in customer_numbers}
    lists = _customer_visible_segment_lists(generation)
    had = sum(len(segments) for _, segments in lists)

    for where, segments in lists:
        kept: list[Any] = []
        for segment in segments:
            if (
                not isinstance(segment, dict)
                or segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
            ):
                # **素材原文一律留着**：它是商家审过的那句话，
                # 而本函数抓的是模型自己写的那几段。
                kept.append(segment)
                continue
            own = str(segment.get("content_or_ref") or "")
            claim = next(
                (
                    match
                    for match in _CUSTOMER_PAYMENT_CLAIM.finditer(own)
                    if _normalised_number(match.group(1)) not in said
                ),
                None,
            )
            if claim is not None:
                # **记那个数与理由码，不记正文** —— 与 `echoed_segments`
                # 逐字同一条理由：正文是客户可见的话，而审计行会被人翻、被导出。
                # 那个数本身是我们自己的标价，不是客户事实。
                dropped.append(
                    {
                        "where": where,
                        "why": UNFOUNDED_CUSTOMER_FACT_REASON_CODE,
                        "number": _normalised_number(claim.group(1)),
                    }
                )
                continue
            if _is_another_language(own):
                # **一通对话一种语言**（T258，用户 2026-09-20 裁「平台写死：
                # 一律中文」）。
                #
                # ⚠️ **这一条是平台的，不是配包声明的** —— 所以它**没有**
                # 进那张闭合词表（ADR-0123）：词表里的每一个词都是
                # 「某个打法声明它要什么」，而这一条不问打法、不问商家。
                # 用户裁的是「一律中文」，登记 T258 里那个「商家自定」的
                # 分支因此没有走 —— **连带 schema / `content_hash` /
                # 重发 release 三样都不需要**，那正是那条登记说它贵的地方。
                #
                # **记那个词数，不记正文** —— 与上面两条逐字同一条理由：
                # 正文是客户可见的话，而审计行会被人翻、被导出。
                dropped.append(
                    {
                        "where": where,
                        "why": REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE,
                        "latin_words": len(_LATIN_WORD.findall(_URL.sub(" ", own))),
                    }
                )
                continue
            run = _longest_shared_run(own, last_outbound) if last_outbound else ""
            if len(run) >= REPEATED_OUTBOUND_CHARS:
                dropped.append(
                    {
                        "where": where,
                        "why": REPEATED_OUTBOUND_REASON_CODE,
                        "shared_chars": len(run),
                    }
                )
                continue
            kept.append(segment)
        segments[:] = kept

    if had and not any(len(segments) for _, segments in lists):
        # **理由码说的是「为什么一条都不剩」，而不是恒等于其中一条。**
        #
        # 这一行原本写死 `UNFOUNDED_CUSTOMER_FACT_REASON_CODE` ——
        # 在只有「替客户认账」一种丢法的时候那是对的。
        # 到了两种（加上「逐字重复上一句」）它已经会说错话，
        # **而 T258 加的是第三种**：一条纯英文的回复被整份丢掉时，
        # 审计行会写着「替客户认了一笔没说过的账」——
        # 接手的人照它去查，查的是一件没发生过的事。
        #
        # 判据是**全体一致**，不是「取第一个」：丢法混在一起时
        # 没有哪一个码说得全，那时仍然用原来那个 —— 它的 `context`
        # 里带着 `refused_because` 全集，读得出真实构成。
        reasons = {str(item.get("why")) for item in dropped}
        raise GenerationRejected(
            reasons.pop() if len(reasons) == 1 else UNFOUNDED_CUSTOMER_FACT_REASON_CODE,
            "every customer-visible segment had to be dropped "
            f"({[item['why'] for item in dropped]}); "
            "saying something wrong about the customer costs more than saying nothing",
            # **事实带出来，不留给下游从那句话里解析回来**
            # （`InferenceRejected.context` 的 docstring 记着为什么：
            # 「从 `detail` 里解析回来是本仓付过账的那一类」）。
            #
            # 接手这条任务的人要答的是「它到底想说什么而被拦了」——
            # 理由码与那个数答得了，**而正文答不了也不该出现在这里**：
            # 正文是客户可见的话，审计行会被人翻、被导出。
            context={
                "dropped_segments": [dict(item) for item in dropped],
                "refused_because": sorted({str(item.get("why")) for item in dropped}),
            },
        )


#: 许了而这一轮没兑现的那一段，理由码。
UNKEPT_PROMISE_REASON_CODE = "GENERATION_PROMISED_WITHOUT_DELIVERING"

#: 哪些字符算**句界**。
#:
#: **它有两个消费方，而它们必须是同一个集合**：尺子不跨它
#: （`_PROMISE_TO_DELIVER` 的字符类由它拼出来），丢一句时也按它切
#: （`_sentences`）。两处各写一份就是这道闸漂掉的那种方式 ——
#: **复核 2026-09-18 抓到的正是那个形状**：docstring 声称「不跨句」，
#: 而字符类里少了几个字符，于是那句话在它自己的解释上就是错的。
#:
#: **半角句点 `.` 不在里面，这是有意的**：`material:demo-x:v1`、
#: 「1.2.3」、「20.5 万」都带点，按它切会把一个 ref 切成两半，
#: 而尺子跨不过它就够不着「我把 20.5 万那档整理好」。半角 `!?;`
#: 没有这个问题，所以它们在里面。
SENTENCE_BOUNDARY_CHARS = "。！？；!?;\n"

#: 分句符：尺子**不跨**它们，**而它们不是句界** —— 丢一句时不在这里切开。
#:
#: **复核 2026-09-18 退回的第二条**：字符类先前只挡句号一族，于是
#: 「我明白**，**您方便说一下这份报价是给哪块业务看的吗」被读成一句许诺。
#: 那句话里**动词的主语是客户**，只是同句前面多了一个「我」——
#: 与已经钉住的 `A_QUESTION` 同一个理由，差别只在有没有跨过一个逗号。
#: 实测这一族八条（`，` `、` `：` 以及半角 `,`）。
#:
#: ⚠️ **顿号 `、` 不在里面，这是量出来的，不是漏的。**
#: 运维 §10.1 那句真实出站「那我先把试用怎么开**、**您那边要准备什么讲清楚。」
#: **正是靠跨过一个顿号才读得出** —— 顿号在中文里是**列举**符号
#: （「把 A、把 B 讲清楚」），不是分句符。把它加进来，本批的主证据
#: 当场漏过去；`test_the_ruler_still_crosses_a_list_separator` 钉住这件事，
#: **所以下一个照着「、也该算分句」去改的人会先看到它红**。
#:
#: 代价如实记在这里，**不含糊**：「我这边有两个问题、您先说一下量级」这种
#: **拿顿号当分句符**的写法仍然会中，而丢的单位是句
#: （见 `drop_promises_this_turn_does_not_keep`）——
#: 所以代价是**那一句**，那一句之外的话照发。
#: ⚠️ **而整条消息只有那一句时，它仍然是整份拒。** 实测过
#: （`test_a_list_separator_used_as_a_clause_separator_still_matches`）。
#:
#: **没有去修它，理由是判据本身**：上面那条真实出站是逐字取自 `intent_records`
#: 的生产串，而「我这边有两个问题、您先说一下量级」是**本窗口自己编的** ——
#: 拿一个编出来的形状去调尺子，正是 T349 那条纪律（`AGENTS.md`：喂进用例的
#: 必须是生产里会出现的形状）反过来。两个候选修法都试算过：
#: 把 `、` 加进本集合会让主证据漏过去；「跨顿号要求 `我` 与动词之间有一个
#: `把`」只有**一个数据点**支撑它。登记为 **T366**，到期条件是
#: 「运维实测到一条这种形状的真实出站」。
_CLAUSE_BOUNDARY_CHARS = "，,：:"

#: 一句话。**终止符跟着它那一句**，所以拼回去是无损的。
_SENTENCE = re.compile(
    f"[^{SENTENCE_BOUNDARY_CHARS}]*[{SENTENCE_BOUNDARY_CHARS}]"
    f"|[^{SENTENCE_BOUNDARY_CHARS}]+"
)


def _sentences(text: str) -> list[str]:
    """把一段文本按句界拆开。**`"".join(_sentences(t)) == t` 对任何 `t` 成立。**

    **无损是丢一句的前提**：留下来的那几句要拼回同一段字，而一个把标点
    吃掉的拆法会让客户读到「明白您自己接」。用例 `test_splitting_a_paragraph_loses_nothing`
    按这条等式钉住它。

    **本函数是全仓第一个按句拆段的地方，而那正是退回第一条的根**：
    没有它，「丢掉那一段」丢掉的是**整段多句话**。

    复核 2026-09-18 在 `1843da7c` 上现读按句拆的那几种写法，**0 命中**。

    ⚠️ **「只有这一处」这句话不写成一条 grep，写成一条会红的用例**：
    `test_this_is_still_the_only_place_that_splits_by_sentence` 在
    `services/` 与 `packages/` 里数定义点与调用点，多一处就红。
    **写成 grep 会自指** —— 把那条命令抄进本 docstring，它就命中自己，
    于是「应 0 命中」当场是假的。本仓为这种形状付过账（「数错了的守卫
    看起来在守」），所以这里不留那条命令。

    多出第二个拆法时那条用例会红，**而它红的时候要想清楚的是**：
    两处拆法要不要共用同一个句界集合。
    """

    return _SENTENCE.findall(text)


#: 一句「我这就给您 X」长什么样。
#:
#: **动词短语本身必须是前瞻的**，所以这里列的是短语不是字：
#: 「说说 / 讲清楚 / 发您 / 安排 / 整理好」许的是接下来要做的事，
#: 而「我说的是标准版的价格」「我这边先确认一下」不是 ——
#: 前者在陈述，后者是我们内部的动作，**客户不会等它**。
#:
#: `我(?!们)`：「我们支持私有化部署」是产品事实，不是一句许诺。
#:
#: **字符类由上面那两个常数拼出来** —— 句界一族 **加上**分句符一族。
#: 先前它只有句界一族，而 docstring 写的是「不跨句」：
#: **那句话比它的实现严，于是它钉的是措辞**（复核退回第二条）。
#:
#: `(?<![的个些这那])安排`：**「安排」既是动词也是名词。**
#: 「报价单这边我来**安排**」是许诺，而「我看了您**的**安排」
#: 「我这边没有别**的**安排」「我觉得这**个**安排挺好」里它是名词 ——
#: 实测这一族三条。**判据是左边那个字**：名词用法前面是
#: 「的 / 个 / 些 / 这 / 那」之一。
#: **只给「安排」加这道前瞻**，不给整个组加：「我把这**个**讲清楚」
#: 里「个」同样在左边，而那是一句真许诺 —— 给整组加会漏掉它。
#:
#: 实测串钉在 `test_a_promise_is_kept_in_the_same_turn.py` 里，全中全不中。
#: **不该中的按族钉，不按条数钉** —— 条数会漂，而族说得出它防的是什么：
#: 句界一族（逐个字符参数化）、分句符一族、「安排」作名词一族，
#: 外加原有那七条（第一人称复数 / 内部动作 / 在陈述 / 主语是客户……）。
#: 「这一轮就能兑现」的那一族 —— **许了就该现在讲/现在发**（T365，2026-09-19 分出）。
#:
#: 批 F 对这一族的处置是对的：这一轮没有内容出去，那句预告就丢掉。
#: **模型下一轮能自己兑现它**，而它这一轮没兑现只是没照做。
_DELIVERABLE_THIS_TURN = (
    "讲清楚",
    "说清楚",
    "讲一下",
    "说一下",
    "说说",
    "讲讲",
    "介绍一下",
    "介绍下",
    "发您",
    "发给您",
    "发过去",
    "发一份",
)

#: **那个动作本来就该由人做**的那一族（T365，2026-09-19）。
#:
#: ## 为什么它不能与上面那一族同样处置
#:
#: 「报价单这边我来安排」——**约个会、走个内部审批、把报价单做出来**：
#: 这些事 AI 今生今世都兑现不了，**下一轮也不行**。
#: 批 F 只是把那句话丢掉，于是
#: **客户看不到我们答应过，也看不到我们没答应** —— 请求被静静吞掉了。
#:
#: 「丢一段是回避，不是答案」（T365 登记时的原话）。
#: 正确的落法是 **ADR-0128 §六已经裁过的那条**：
#: **只落待办 + 推通知，不动 `control_mode`、不挂目标栈** ——
#: 那一节里用户的原话是「系统做到『提醒商家』为止」，
#: 以及那句反复被引的 **「系统保证通知送达，不保证有人接手」**。
#:
#: ⚠️ **不挂起是那条裁定的要点**：挂起意味着替商家决定这通对话交给人了，
#: 而人可能根本不在 —— 那时客户等到的是彻底的沉默。
#: 所以这一族的那句话**照样丢**（它是一句兑现不了的预告），
#: **而请求不再消失** —— 它变成一条同事看得见的待办。
_ONLY_A_PERSON_CAN_KEEP = ("安排", "整理好", "准备好")

#: 短语 -> 这一族要不要人。**唯一判据点** —— 两处各写一份，
#: 就会出现「尺子认得它而落待办那一步不认」，
#: 而那种不一致的形状正是「丢掉了而没有人知道」。
_NEEDS_A_PERSON = {phrase: True for phrase in _ONLY_A_PERSON_CAN_KEEP} | {
    phrase: False for phrase in _DELIVERABLE_THIS_TURN
}

_PROMISE_TO_DELIVER = re.compile(
    "我(?!们)[^"
    + SENTENCE_BOUNDARY_CHARS
    + _CLAUSE_BOUNDARY_CHARS
    + "]{0,20}?"
    # **动词短语单独一个组**：审计行记的是它（「讲清楚」「安排」「说说」），
    # 不是整段匹配的尾巴 —— `group(0)[-4:]` 会切出「么讲清楚」这种半个词。
    #
    # **两族拼在一个组里**（T365）：一条正则、一个组，
    # 于是 `group(1)` 拿到的那个短语可以直接问 `_NEEDS_A_PERSON`。
    # ⚠️ 拆成两条正则试过并否掉了：一句话里两族各命中一次时，
    # 谁先谁后就决定了这一句被记成哪一族 —— 而顺序不是判据。
    #
    # ⚠️ **`安排` 那个 lookbehind 留在这里，不进上面那个元组**：
    # 元组里那几个是**短语本身**（`group(1)` 会等于它们），
    # 而 lookbehind 是匹配条件，不是短语的一部分 ——
    # 混进去会让 `_NEEDS_A_PERSON` 查不到键。
    r"(讲清楚|说清楚|讲一下|说一下|说说|讲讲|介绍一下|介绍下"
    r"|发您|发给您|发过去|发一份|(?<![的个些这那])安排|整理好|准备好)"
)


def needs_a_person(phrase: str) -> bool:
    """这个动词短语许下的事，**要人做吗**（T365）。

    认不出的短语算「不要人」——**fail-open 的那一侧，而这是有意的**：
    认错方向的代价不对称。误判成「要人」会给同事派一条莫须有的待办
    （ADR-0128 §七 刚把「提醒这一环不能是尽力而为」写成一条边界，
    而一条假待办正好在消耗那条边界的可信度）；
    误判成「不要人」只是回到批 F 的行为 —— 那句话丢掉，别的照发。

    ⚠️ **而「认不出」今天不可能发生**：这个表的键与正则里那个组
    **按等值集合钉住**（`test_every_phrase_the_ruler_knows_has_a_family`），
    所以真正防的是「有人往正则里加了一个短语而没说它属于哪一族」。
    """

    return _NEEDS_A_PERSON.get(phrase, False)


def _this_turn_delivers(generation: InferenceDocument) -> bool:
    """这一轮**真的有东西出去**吗。

    用户 2026-09-18 裁定的判据逐字是「**发出来就算兑现了**」——
    不做语义匹配，不问「讲的是不是承诺的那一件」。所以这里只问三件，
    有一件成立就算：

    | 算 | 为什么 |
    |---|---|
    | 有 `material_ref` 段 | 商家审过的正文真的出去了 |
    | 有 `send_material` 提案 | 文件真的出去了 |
    | 有一条**带来源**的 claim | 有来源的散文回答也是内容（知识库那条路）|

    第三项读的字段叫 `source_evidence_refs` —— 不是 `evidence_refs`，见下面那行注释。

    **第三项不能省。** 省了它，一句「我说说它具体怎么接」加一段引自知识库的
    解释会被判成空头支票 —— 那是一个**好回合**，而闸把它丢了。

    **「后面还有一段字」不算，这一条是量出来的。** 运维 §2.2 那一轮是
    「收到，报价单这边我来安排。您方便说一下这份报价是给哪块业务看的吗」：
    **许诺 + 一个问题，什么都没出去。** 按「还有一段」判它合格，
    这条闸就只剩「消息里只有孤零零一句许诺」那一种形状 —— 而真实记录里
    没有那一种。
    """

    for _, segments in _customer_visible_segment_lists(generation):
        if any(
            isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
            for segment in segments
        ):
            return True
    if any(
        str(intent.get("kind")) == "send_material"
        for intent in generation["proposed_intents"]
    ):
        return True
    # **字段名是 `source_evidence_refs`** —— 第一版写的是 `evidence_refs`，
    # 那个键在这份 schema 里不存在（`additionalProperties: false`），
    # `.get` 恒为 `None`，**这一条析取项从写下来就是死的**。
    return any(claim.get("source_evidence_refs") for claim in generation["claims"])


def drop_promises_this_turn_does_not_keep(
    generation: InferenceDocument,
    *,
    dropped: list[dict[str, Any]],
) -> None:
    """运维 §10.4 判据 2：**许下的，这一轮就要兑现**。

    ## 用户 2026-09-18 裁的是「轮次」，不是「顺序」

    运维那句话逐字：

    > 「明白，您自己接。那我先把试用怎么开、您那边要准备什么讲清楚。」
    > **这个后面应该就接上后面的承诺，而不是等客人说话再做答** ——
    > 因为他不一定想了解我们给出的承诺，**话题转开，前面的承诺就没有机会讲了**。

    **所以它比「下一段就要是它」硬**：不是排版问题，是**这一轮发不发得出去**。
    一句推到下一轮的话，客户可以不给你下一轮。

    ## 为什么 `no_new_commitment` 顶不住它

    那条约束**早就有**，演示包七个打法全声明，`advance_goal` 上还由平台
    无条件施加（T332）。而它的两个信号 —— `claims[].commitment_id` 与
    `safety_markers` 里的 `contains_commitment` —— **都是模型自述的**。
    模型不举手，闸就看不见。**本函数不问模型任何问题，只量我们自己写的那几段字。**

    ## 素材原文一律留着，**而这里不为它写分支**

    演示包里就有一句「真要往下走，我把对接清单发您」—— 商家这么写是商家的
    选择。而**一段 `material_ref` 在场本身就让这一轮算「有东西出去」**，
    于是上面那个 `return` 直接把整份放过。那就是这条规矩的全部实现；
    在循环里另写一个 `material_ref` 分支够不着，等于摆一道空闸。

    ## 整份判，不逐张判 —— **这一条改过一次又撤回来，理由写在这里**

    中途改成过「逐张判」，怕的是「草稿贴了素材而载荷没贴，客户收到一句
    光秃秃的许诺」。**那件事上游已经拦了，而且拦在本函数前面**：
    `_validate_material_refs` 里的 `GENERATION_PAYLOAD_DROPPED_MATERIAL`
    保证**草稿引的 ref ⊆ 载荷引的 ref**。

    **而逐张判会真的造出一个新问题**：载荷贴了素材、草稿没贴（这个方向上游
    不拦），逐张判会从草稿里丢掉那一段而载荷里留着 —— **两份文本从此不一样**。
    本函数没有理由制造这种分叉。

    ## 丢的单位是**句**，不是段 —— **复核 2026-09-18 退回第一条，这里是它的修正**

    **第一版按段丢，而生产的一段是多句的。** 运维 §10.1 那条真实出站的
    `text` 段逐字是「明白，您自己接。那我先把试用怎么开、您那边要准备什么讲清楚。」
    —— **两句话在同一段里**，而本批之前全仓没有任何地方按句拆段
    （见 `_sentences` 的 docstring：复核现读 0 命中）。

    于是「丢掉那一段」丢掉的是整段多句话；而纯文本回合（没有素材、没有文件、
    没有带来源的 claim —— 正是这道闸唯一会跑的那种回合）常常**只有一段**，
    运维同一份记录里写着十一轮对话 `send_material` 出现 **0 次**。
    **段 = 整条消息，于是「丢一段」= 整份拒 = 客户这一轮收不到东西。**

    自审与四处注释当时一致地写着「整份拒是窄支」，**而那个估计在生产的分段
    形态下不成立** —— 它只在用例夹具里成立，因为夹具把那句话拆成了两段。
    **那正是本仓「自造夹具藏得住形状不匹配」那一族**（`AGENTS.md` T349：
    喂进用例的必须是生产里会出现的形状）。

    改成按句之后，用户那条裁定第一次真的成立：

    > **「只丢掉那一段，其余照发」** —— 一条不剩时才整份拒。

    ## 一句不剩时整份拒

    与 5.7 同一条路径。**而这里的代价比那边低**：那边拦的是一句错话，
    这边拦的只是一句没兑现的预告。**这一支现在真的窄**：它要求这条消息的
    **每一句**都是一句没兑现的许诺（`test_a_message_that_is_nothing_but_a_promise_is_refused`
    喂的就是那种 —— 单段、单句）。
    """

    if _this_turn_delivers(generation):
        return

    lists = _customer_visible_segment_lists(generation)
    had = sum(len(segments) for _, segments in lists)

    for where, segments in lists:
        kept: list[Any] = []
        for segment in segments:
            if not isinstance(segment, dict):
                kept.append(segment)
                continue
            # **丢的单位是句，不是段** —— 见本函数 docstring 的那一节。
            survivors: list[str] = []
            # **(动词短语, 那一句) 成对收**，而不是只收短语。
            # ⚠️ 第一版只收短语、在下面那个循环里读 `sentence` ——
            # 而那时 `sentence` 已经是这一段**最后**一句，不是命中的那一句。
            # `test_the_dropped_item_carries_the_sentence_for_the_retry` 当场红。
            # **一个循环变量在它的循环之外就不再是它了。**
            promises: list[tuple[str, str]] = []
            for sentence in _sentences(str(segment.get("content_or_ref") or "")):
                match = _PROMISE_TO_DELIVER.search(sentence)
                if match is None:
                    survivors.append(sentence)
                    continue
                promises.append((match.group(1), sentence))
            if not promises:
                # **一个字都没动的段原样传下去**，不是它的一个副本：
                # 下游按段做过身份比较（草稿与载荷两份要说同一句话），
                # 而无谓地换一个 dict 会让那种比较无从下手。
                kept.append(segment)
                continue
            for promised, promised_sentence in promises:
                # **记命中的那个动词短语，不记正文** —— 与 `echoed_segments`、
                # `unsourced_segments` 逐字同一条理由：正文是客户可见的话，
                # 而审计行会被人翻、被导出。
                #
                # 排查这条时要答的是「它许了什么形状的东西」，
                # 而那个短语答得了。
                #
                # **一条 = 一句**（复核 2026-09-18 之后）。先前一条 = 一段，
                # 而一段可以装好几句许诺 —— 那时只记得下第一句那个短语。
                dropped.append(
                    {
                        "where": where,
                        "why": UNKEPT_PROMISE_REASON_CODE,
                        "promised": promised,
                        # **要不要人做**（T365，2026-09-19）——
                        # 管线据它决定落不落待办。判据在 `needs_a_person`，
                        # 不在这里：读词表的那一处与落待办的那一处
                        # 必须是同一个答案。
                        #
                        # ⚠️ **它进得了审计上下文**（下面那个投影带着它）——
                        # 它是一个布尔，不是客户可见的字，
                        # 而「这一句要不要人接手」正是排查时要答的那件。
                        "needs_a_person": needs_a_person(promised),
                        # **整句也带出来，而它有意不进审计行**（T367，2026-09-19）。
                        #
                        # 重跑那一次要把「你许了什么」喂回给计划步，
                        # 而**「讲清楚」三个字说不出「试用怎么开」** ——
                        # 动词短语够写审计，不够让模型补出那一条。
                        #
                        # ⚠️ **T365 之后它还有第二个读者**：落待办那一步把它
                        # 当成「AI 刚才答应：…」放进通知正文（ADR-0128 §六
                        # 的另一半：**固定模板 + 附模型原话**）。
                        # **通知正文给同事看，审计行会被导出** —— 两个面不同，
                        # 所以下面那个投影仍然不带它。
                        "sentence": promised_sentence,
                    }
                )
            remainder = "".join(survivors).strip()
            if not remainder:
                # 这一段整个都是许诺 —— 段没了。**一条不剩时下面那个 `raise`
                # 接手**，而那一支现在真的窄：它要求这条消息**每一句**
                # 都是一句没兑现的许诺。
                continue
            kept.append({**segment, "content_or_ref": remainder})
        segments[:] = kept

    if had and not any(len(segments) for _, segments in lists):
        raise GenerationRejected(
            UNKEPT_PROMISE_REASON_CODE,
            "every customer-visible sentence was a promise this turn does not keep "
            f"({[item['promised'] for item in dropped]}); "
            "a promise pushed to the next turn may never get one",
            context={
                # ⚠️ **逐字投影，不是 `dict(item)`**（T367 抓到的）。
                # 那几个 item 从 2026-09-19 起多带一个 `sentence`
                # （重跑要把整句喂回给计划步），而 `dict(item)` 会把
                # **客户可见的整句**抄进审计面。
                # `test_a_message_that_is_nothing_but_a_promise_is_refused`
                # 当场红并报「正文进了审计上下文」——
                # **一条按「整个抄过去」写的投影，会跟着上游长出来的字段一起变宽。**
                #
                # **T365 往上游又加了一个字段**（`needs_a_person`），
                # 而这条投影是逐字写的 —— 所以那次加字段**没有**把任何东西
                # 带进审计面，要带的那一个是这里显式列出来的。
                # **这就是逐字投影值那几行的地方。**
                "dropped_segments": [
                    {
                        "where": item["where"],
                        "why": item["why"],
                        "promised": item["promised"],
                        "needs_a_person": item["needs_a_person"],
                    }
                    for item in dropped
                ],
                "refused_because": [UNKEPT_PROMISE_REASON_CODE],
            },
        )


def _validate_material_refs(
    *, generation: InferenceDocument, plan: ValidatedPlan, materials: Mapping[str, str] | None
) -> None:
    """Every material the draft quotes must be one **this turn** was offered.

    **The set is the turn's, not the frame's, and it is computed once.** A frame
    declares materials for every turn it will ever see: the follow-up wording for
    its timers, the answers to doubts the customer may raise. Checking against
    the frame's whole declaration would let a live conversation quote follow-up
    wording, or volunteer an answer to a question nobody asked -- the one mistake
    the merchant's own material document says turns this profile back into a help
    desk. `materials` is what `material_selection.select_materials` returned for
    this turn.

    **Deriving it here instead was the defect the independent review found**
    (2026-09-10, P1-1): this check reached for `plan.definition`, bound before
    the target-stack transition, while the prompt reached for the stack top
    after it -- so any plan that was not `keep` offered the model one frame's
    materials and judged them against another's. The pipeline now resolves the
    frame the turn *ends* on, selects once, and hands the same mapping to the
    prompt, to this check and to the send path.

    **`None` means no selection was supplied**, and then nothing may be quoted.
    That is fail-closed on purpose: a caller that forgot to select has not
    thereby permitted everything.

    Refused rather than dropped. A ref the platform cannot resolve becomes no
    words at all (`channel.customer_text` returns `None` for it), so dropping the
    segment quietly would send the rest of a message whose quoted half never
    arrived -- the "silently loses half" failure this platform has now met on
    both the inbound and the outbound side. A refusal costs one `plan.no_send`
    carrying a reason code.
    """

    allowed = materials or {}
    frame_id = str(plan.definition.get("id"))
    for segment in generation["customer_visible_segments"]:
        if segment["type"] != MATERIAL_REF_SEGMENT_TYPE:
            continue
        ref = str(segment["content_or_ref"])
        if ref not in allowed:
            raise GenerationRejected(
                "GENERATION_MATERIAL_REF_UNKNOWN",
                # The ref, never the text. A ref is a name the profile chose and
                # the reason code is useless without it; the material's words are
                # customer-visible content and do not belong in a decision row.
                f"material {ref!r} is not available to this turn of frame {frame_id!r}",
            )
    # **Checked here as well, because these are two different documents.**
    # `customer_visible_segments` is the draft; the `content` of a `send_message`
    # payload is what actually reaches the outbox (`_persist_accepted_proposal`
    # builds the row from it), and the model fills both. Validating only the
    # draft would leave the ref that gets *sent* unchecked -- the T265 question
    # ("which of the two is authoritative") answered the wrong way by omission.
    for intent in generation["proposed_intents"]:
        payload = intent.get("typed_payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        segments = content.get("segments") if isinstance(content, dict) else None
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if segment.get("type") != MATERIAL_REF_SEGMENT_TYPE:
                continue
            ref = str(segment.get("content_or_ref"))
            if ref not in allowed:
                raise GenerationRejected(
                    "GENERATION_MATERIAL_REF_UNKNOWN",
                    f"intent {intent['kind']!r} quotes material {ref!r}, which this turn "
                    f"of frame {frame_id!r} was not offered",
                )

    _validate_payload_agrees_about_materials(generation=generation)


def _validate_payload_agrees_about_materials(*, generation: InferenceDocument) -> None:
    """The draft and the payload may not disagree about which materials are used.

    **The draft is not what ships.** `_persist_accepted_proposal` builds the
    outbox row from a `send_message` intent's `typed_payload.content`;
    `customer_visible_segments` has no reader on the send path at all. Until this
    check existed, a model could quote three merchant materials in the draft and
    write its own prose in the payload, and the platform sent the prose -- with
    every gate green, because the draft side was compliant. Measured over three
    real turns before the fix: the draft quoted 1 / 3 / 1 materials and the
    payload quoted **none**, in six arms out of six.

    **Why the rule is "must not disagree" rather than "must quote".** The obvious
    stronger form -- "materials were offered, so the payload must quote one" --
    collides with the ungated safety valve: an `on_request` material declaring no
    topics is offered on *every* ordinary turn (it is the "I am not sure" line
    that keeps a mis-filled topic gate from pushing the model into inventing).
    Under the stronger rule, a turn the model can answer perfectly well would
    still have to quote something, and the only thing always available to quote
    is that valve. Forcing "I am not sure" onto a confident answer is worse than
    the gap it closes.

    **The unescapable half is declared instead**: a strategy that must quote says
    so with the `must_quote_material` generation constraint, which the profile
    author owns and `_validate_constraints` enforces. That keeps "the model can
    escape by quoting nowhere" closed for the plays where it matters, without the
    platform forcing a quote onto every play everywhere.
    """

    drafted = {
        str(segment["content_or_ref"])
        for segment in generation["customer_visible_segments"]
        if segment["type"] == MATERIAL_REF_SEGMENT_TYPE
    }
    if not drafted:
        return

    sends = [
        intent for intent in generation["proposed_intents"] if intent["kind"] == "send_message"
    ]
    if not sends:
        # Nothing is being sent, so there is nothing for the draft to disagree
        # with. A turn that drafts content and proposes no send is refused
        # elsewhere or is legitimately silent; either way it is not this rule's
        # business.
        return

    # **载荷那一侧按并集算，不是逐条算**（批 G，2026-09-19）。
    #
    # ## 逐条算在「一轮两条消息」上是错的，而它错得很响
    #
    # 用户 2026-09-19 裁定兑现落在**紧跟着的下一条消息**里
    # （「许了 紧跟下一条来讲完 不用在一条里面完成显得很奇怪」）。
    # 那种回合长这样：第一条是那句许诺（纯文本），第二条把商家原文发出去。
    #
    # 而 `customer_visible_segments` 是**一张**扁平的草稿 —— 它装着两条消息的段，
    # 所以 `drafted` 是两条的并集。**逐条算时第一条消息必须也引那段素材**，
    # 于是这种回合**整份被拒**：`GENERATION_PAYLOAD_DROPPED_MATERIAL`，
    # **客户这一轮什么都收不到**。
    #
    # **实测到的**：批 G 那条核心用例（许诺一条 + 素材一条）在改这里之前
    # 就是这么被拒的，而它是用户裁定的那个形状。
    #
    # ## 并集才是这条规则本来要守的那件事
    #
    # 它守的是「草稿引的素材不会在客户真收到的那份里被换成模型自己的话」——
    # 判据是**那段原文有没有真的出去**，而不是「有没有出现在每一条消息里」。
    # 一轮里有一条消息逐字引了它，它就到了客户那儿。
    #
    # **一轮一条消息时两种算法逐字等价**（并集就是那一条自己），
    # 所以这一改不放松任何既有回合：草稿引了而唯一那条载荷没引，照旧整份拒
    # （`test_the_gate_this_one_leans_on_still_runs_first` 钉着那一支）。
    quoted: set[str] = set()
    for intent in sends:
        payload = intent.get("typed_payload")
        content = payload.get("content") if isinstance(payload, dict) else None
        raw = content.get("segments") if isinstance(content, dict) else None
        # **A payload with no segments at all is the case that was actually
        # observed**, not a defensive branch: the model returned its own prose
        # under a different content shape, so the sibling check had nothing to
        # compare and simply said nothing. Treating that as "quotes none" is what
        # makes this rule speak to the failure that happened.
        segments = raw if isinstance(raw, list) else []
        quoted |= {
            str(segment.get("content_or_ref"))
            for segment in segments
            if isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
        }
    dropped = sorted(drafted - quoted)
    if dropped:
        raise GenerationRejected(
            "GENERATION_PAYLOAD_DROPPED_MATERIAL",
            # Refs, never the words -- the same rule the sibling check follows.
            f"the draft quotes {sorted(drafted)} but this turn's send_message payloads "
            f"quote {sorted(quoted)}; {dropped} would be replaced by the model's own "
            "wording in the message the customer actually receives",
        )


#: 一条知识库来源的 ref 长什么样。**与 `import_knowledge.py` 写进
#: `knowledge_chunks.ref` 的形状同源**（`knowledge:<slug>:v<版本>`）。
KNOWLEDGE_REF_PREFIX = "knowledge:"


def _validate_knowledge_citations(
    *, generation: InferenceDocument, offered: Mapping[str, str] | None
) -> None:
    """模型引的每一条知识库来源，必须是**这一轮**给过它的。

    **这道闸补的是 `evidence_required` 一直没检的那一半**：那条约束只检
    `source_evidence_refs` **非空**，从不检里面是什么 —— 于是一条编出来的出处
    （模型没见过的 ref、或它自己拼的一个）照样通过。知识库进提示词之后，
    这件事从「理论上难看」变成「可判且必须判」：**本轮给过哪几条 ref，平台自己知道。**

    **为什么是 refuse 而不是 drop**：一条引错来源的事实声明，比一条没有来源的
    更糟 —— 后者会被 `evidence_required` 拦住并让模型说「不确定」，
    前者**读起来像有据可查**。与 `_validate_material_refs` 同一条理由（那里也是 refuse）。

    **`offered` 为 `None` 或空表示这一轮没给过知识库** —— 那时任何 `knowledge:`
    前缀的引用都是编的。fail-closed：没给过不等于什么都能引。

    **非 `knowledge:` 前缀的 ref 这里不管**：它们是 understanding 的
    `evidence_span_hash` 一类（模型自己那一侧的证据），本条不改它们的口径。

    **「知识库不能当素材逐字引」这条不在提示词里说，因为它已经被执行了**：
    一个 `material_ref` 段的 ref 必须在这一轮的 `materials` 里
    （`_validate_material_refs`），而知识库的 ref 不在那张表里 —— 所以那条路
    本来就走不通。提示词里每一个字节都是**每次 generate 都付**的，
    而 ADR-0132 §八 那张预算表今天没有富余：**一条已经有闸的规则不值得再花一遍钱**
    （6c 的自审 §字节 记了这次取舍与它的算式）。
    """

    allowed = offered or {}
    for claim in generation["claims"]:
        for ref in claim["source_evidence_refs"]:
            text = str(ref)
            if not text.startswith(KNOWLEDGE_REF_PREFIX):
                continue
            if text not in allowed:
                raise GenerationRejected(
                    "GENERATION_KNOWLEDGE_REF_UNKNOWN",
                    # ref，不是正文 —— 与隔壁那条同一条纪律：
                    # 一条决策行里不放客户可见的字。
                    f"claim cites {text!r}, which this turn did not retrieve",
                )


def _validate_memory_citations(
    *, generation: InferenceDocument, offered: Collection[str] | None
) -> None:
    """模型引的每一条记忆来源，必须是**这一轮**递给它的（T363）。

    **与 `_validate_knowledge_citations` 逐字同形**，理由也同：
    一条引错来源的事实声明，比一条没有来源的更糟 ——
    后者会被 `evidence_required` 拦住并让模型说「不确定」，
    **前者读起来像有据可查**。

    **`offered` 为空表示这一轮没递过记忆** —— 那时任何 `memory:` 前缀的引用
    都是编的。fail-closed：没给过不等于什么都能引。

    ⚠️ **ref 是平台给的，不是模型拼的**：生成步收到的
    `remembered_facts` 就是 `{ref, value}`，模型只要把那个 ref 吐回来。
    **所以拒一条拼错的 ref 是公平的** —— 与知识库那一族同一个前提
    （那边的 ref 也是逐字递过去的）。
    """

    allowed = set(offered or ())
    for claim in generation["claims"]:
        for ref in claim["source_evidence_refs"]:
            text = str(ref)
            if not text.startswith(MEMORY_REF_PREFIX):
                continue
            if text not in allowed:
                raise GenerationRejected(
                    "GENERATION_MEMORY_REF_UNKNOWN",
                    # ref，不是值 —— 记忆的值是客户事实，
                    # 而一条决策行里不放客户可见的字。
                    f"claim cites {text!r}, which this turn did not remember",
                )


def _validate_constraints(
    *, generation: InferenceDocument, plan: ValidatedPlan, materials: Mapping[str, str]
) -> None:
    constraints = set(plan.document["generation_constraints"])

    if "evidence_required" in constraints:
        for claim in generation["claims"]:
            if not claim["source_evidence_refs"]:
                raise GenerationRejected(
                    "CLAIM_WITHOUT_EVIDENCE", "a factual claim carries no source evidence"
                )
        for intent in generation["proposed_intents"]:
            if not intent["evidence_refs"]:
                raise GenerationRejected(
                    "INTENT_WITHOUT_EVIDENCE",
                    f"intent {intent['kind']!r} carries no evidence reference",
                )

    # **`advance_goal` 上这一条由平台无条件施加**（T332，用户 2026-09-16 裁定）。
    #
    # 图纸 §4.1 那句「它不能构造未登记承诺」说的是**这条策略的性质**，
    # 不是「配包可以要求它」。而在此之前它的唯一执行条件是
    # 「`no_new_commitment` 出现在这一轮的 `generation_constraints` 里」，
    # 那份清单来自配包 —— **于是一条策略边界变成了一个配包选项，而选项的缺省是
    # 「不管」**。现读三份在架配包的 `strategies["advance_goal"]`：
    #
    #     internal-demo-sales  ["current_goal_only", "no_new_commitment"]
    #     synthetic-carrier    ["current_goal_only"]
    #     synthetic-third      ["current_goal_only"]
    #
    # **三份里两份没声明它**，而 `advance_goal` 在那两份的帧 allowlist 里 ——
    # 真模型选中它就走这条路，且在约束层面完全不受约束。
    #
    # **承诺台账落地那一刻这件事变得要紧**：在此之前「未登记」无从判定
    # （没有台账，什么都算未登记，也什么都不算）；有了台账之后，
    # 一条模型自己报的 `commitment_id` 指不指得到一行真承诺，是可判的。
    #
    # **只加在 `advance_goal` 上，不改其余策略**：图纸对别的策略没有这句话，
    # 把它推广到全部等于替图纸多裁一条。
    if plan.strategy_id in IN_FRAME_ONLY_STRATEGIES:
        constraints = constraints | {"no_new_commitment"}

    if "no_new_commitment" in constraints:
        for claim in generation["claims"]:
            if claim.get("commitment_id"):
                raise GenerationRejected(
                    "UNREGISTERED_COMMITMENT",
                    f"strategy {plan.strategy_id!r} may not create a commitment",
                )
        if "contains_commitment" in generation["safety_markers"]:
            raise GenerationRejected(
                "UNREGISTERED_COMMITMENT",
                f"strategy {plan.strategy_id!r} may not create a commitment",
            )

    if "one_question" in constraints and _question_count(generation) > 1:
        raise GenerationRejected(
            "MORE_THAN_ONE_QUESTION", "the strategy permits a single question per turn"
        )

    if "confirmation_only" in constraints and not generation["return_confirmation"]["included"]:
        raise GenerationRejected(
            "RETURN_CONFIRMATION_MISSING", "the strategy requires a return confirmation"
        )

    if "must_quote_material" in constraints:
        # **The unescapable half of the material rule, and it is declared rather
        # than imposed.** `_validate_payload_agrees_about_materials` stops the
        # draft and the payload disagreeing, but a model that simply quotes
        # nowhere satisfies it -- and a platform-wide "you must always quote"
        # cannot be the answer, because the ungated safety valve is offered on
        # every ordinary turn and forcing a quote would mean forcing "I am not
        # sure" onto answers the model has.
        #
        # So the profile author says which plays must quote. It costs them one
        # name in `generation_constraints`, which `validate_plan` already
        # refuses when undeclared, and it is checked on the payload -- the
        # document that ships -- not on the draft.
        quoted = {
            str(segment.get("content_or_ref"))
            for intent in generation["proposed_intents"]
            if intent["kind"] == "send_message"
            for segment in (
                ((intent.get("typed_payload") or {}).get("content") or {}).get("segments") or []
            )
            if isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
        }
        if materials and not quoted:
            raise GenerationRejected(
                "MATERIAL_QUOTE_REQUIRED",
                f"strategy {plan.strategy_id!r} must quote one of the merchant's materials, "
                f"and the send_message payload quotes none (offered: {sorted(materials)})",
            )


def _question_count(generation: InferenceDocument) -> int:
    total = 0
    for segment in generation["customer_visible_segments"]:
        if segment["type"] != "text":
            continue
        text = str(segment["content_or_ref"])
        total += text.count("?") + text.count("？")
    return total
