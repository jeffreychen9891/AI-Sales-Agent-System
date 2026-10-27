"""The three-stage inference boundary.

Each step returns a contract document validated against
`packages/contracts/inference/*.schema.json`; the schema is the single source of
truth for the shape, so this module keeps no parallel dataclass definition of it.

The boundary is deliberately narrow: understanding never selects a strategy,
planning never writes customer-visible text, and generation never adds an intent
the plan did not already list. `fastagent_v2.worker.inference_validation` enforces all
three, and no step result may be used before it passes that validator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

from fastagent_v2.runtime.contracts import validate_inference_contract
from fastagent_v2.worker.cost import CallContext
from fastagent_v2.worker.platform_frames import frame_definitions, strategy_registration
from fastagent_v2.worker.strategies import STRATEGY_CATALOG, time_rule_strategy_set

InferenceDocument = dict[str, Any]


def _empty_model_evidence(*, provider: str, model: str, prompt_release: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "prompt_release": prompt_release,
        "latency_ms": 0,
        "token_usage": {"input": 0, "output": 0},
    }


class InferencePort(Protocol):
    """Model boundary. Implementations must not perform business side effects."""

    version: str

    def understand(
        self,
        *,
        envelope: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        #: 上一轮我们对这个客户说的那句话。**理解步与计划步收同一份** ——
        #: 单看几个字读不出意思（运维 2026-09-17 实测那一轮的沉默根因）。
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这一轮带给模型的记忆，**以及装不下的有几条**。
        #:
        #: **管线算一次，两步收同一份**（与 `turn_materials` 同一条纪律）：
        #: 两处各算一遍会让理解步与计划步看见不同的记忆，
        #: 而那种不一致只在真实回合里显形。
        #: 管线也据此留痕 —— 「那条回复看见过哪几条记忆」必须答得出来。
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: 这通对话已经真的发出去过的话术 ref，按发出顺序（平台从
        #: `intent_records` 现读，**不是模型自己报的**）。
        materials_already_sent: Sequence[str] = (),
    ) -> InferenceDocument: ...

    def read_silence(
        self,
        *,
        #: **上面那一串与 `understand` 逐字相同，这是有意的**（ADR-0149）。
        #:
        #: 这一步与理解步的区别**不在输入，在被问的那个问题**：理解步问
        #: 「客户刚说了什么」，这一步问「**什么都没发生，为什么；这时候做什么有用**」。
        #:
        #: ⚠️ **不给它一份「静默画像」**。第一版设计挑了七样事实
        #: （挂着的问题、沉默多久、最后说的那句……）交给它，而用户 2026-09-22
        #: 指出「**静默情况也是无法穷尽的**」—— 一张手挑的输入清单**编码了一套
        #: 「沉默为什么发生」的理论**，而那套理论对下一个行业照样不够用。
        #: 挑输入和挑词表是同一个错，只是换了一层。
        #:
        #: 所以上下文照搬理解步那一份，平台不预先判断哪几样事实解释得了一次沉默。
        envelope: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        materials_already_sent: Sequence[str] = (),
        #: 下面三样是**主动轮次客观多出来的事实**，不是对沉默的理论：
        #: 客户消息那一轮根本没有「已沉默多久」这个量。
        #:
        #: `unanswered_rounds` —— **连着几轮没回音**。它在这里是因为
        #: 「一次有策略的追问」与「一直追问」**长得一模一样**，除非知道这是第几次
        #: （用户 2026-09-22：「追问也可能是一种销售策略，不一定真的要客人回答」）。
        silent_for_seconds: float = 0.0,
        unanswered_rounds: int = 0,
        #: 这一轮**真的被 offer** 的素材，ref → 原文。由 `select_materials` 按
        #: 商家的声明算出来，**平台不放宽**。空集合是常态而不是异常：
        #: 在架配包今天一条 `availability: timer` 的素材都没有。
        offered_materials: Mapping[str, str] = MappingProxyType({}),
    ) -> InferenceDocument: ...

    def plan(
        self,
        *,
        understanding: InferenceDocument,
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        #: The strategy the `TimeRule` behind this turn declared, when the turn
        #: is a timer (B4-4, ADR-0105 §零.1). `None` for customer messages, where
        #: the model chooses. An implementation must narrow its own choice to
        #: this value: the validator refuses a model plan that names another one,
        #: so ignoring it here would send every timer turn to the safe fallback.
        required_strategy: str | None = None,
        #: 这一轮可以发的文件，**与生成步收到的是同一份**。
        #:
        #: 候选意图的 `typed_payload` 是**必填**的，所以提名一个 `send_material`
        #: 就得同时给出 `{"ref": …}` —— ref 只能从这里来。
        #: 这个参数缺席时，计划步从不把 `send_material` 列进候选，
        #: 而生成步提不出候选里没有的 kind：**十一轮真实对话零次提名**。
        assets: Sequence[Mapping[str, str]] = (),
        #: 上一轮我们对这个客户说的那句话 —— 与理解步收的是同一份。
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这一轮带给模型的记忆，**以及装不下的有几条**。
        #:
        #: **管线算一次，两步收同一份**（与 `turn_materials` 同一条纪律）：
        #: 两处各算一遍会让理解步与计划步看见不同的记忆，
        #: 而那种不一致只在真实回合里显形。
        #: 管线也据此留痕 —— 「那条回复看见过哪几条记忆」必须答得出来。
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: 这通对话已经真的发出去过的话术 ref，按发出顺序（平台从
        #: `intent_records` 现读，**不是模型自己报的**）。
        materials_already_sent: Sequence[str] = (),
        #: 上一次许了而没兑现的那几句（T367）。第一次尝试恒为空。
        unkept_promises: Sequence[str] = (),
        #: 这一轮那次静默怎么读的（ADR-0149）。**只有主动轮次非空。**
        #:
        #: 计划步要它，是因为**第四步已经判过一次了**：为什么对面不说话、
        #: 这一轮该做什么、要发就发哪一份。计划步看不见它的话，那次判断
        #: 就没有消费方 —— 本仓管那叫「数据生产了，全仓无人消费」。
        #:
        #: ⚠️ **它是一份判断，不是命令**：选策略仍然是计划步的事，
        #: `allowed_strategy_set` / `time_rule_strategy_set` 两道闸一个没松。
        silence_reading: Mapping[str, Any] | None = None,
    ) -> InferenceDocument: ...

    def generate(
        self,
        *,
        plan: InferenceDocument,
        understanding: InferenceDocument,
        envelope: dict[str, Any],
        #: The merchant's approved wording this turn may quote, ref -> text,
        #: **already narrowed** by `material_selection.select_materials`.
        #:
        #: Selected by the platform and handed over, rather than derived here
        #: from the aggregate: the same set has to reach the prompt and the
        #: validator, and two derivations of one answer is how the two ends of a
        #: boundary come to disagree. It is also what keeps the rule replaceable
        #: -- an implementation of this port formats a prompt, it does not decide
        #: what the turn is allowed to say.
        materials: Mapping[str, str],
        #: 这一轮**真的送进提示词**的知识库命中，`ref -> text`，
        #: 已经由 `runtime.knowledge_bound.bound_for_prompt` 压过额度（ADR-0135）。
        #:
        #: 与 `materials` 同一条理由：平台选好再交过来，不在这里派生 ——
        #: 提示词与校验器必须看到同一个集合，而「装不下丢了一条」的回合上
        #: 「检到的」与「送进去的」不是一个集合。
        knowledge: Mapping[str, str] = MappingProxyType({}),
        #: 这一回合可以提的文件，**已经由 `select_assets` narrowed**。
        #: 与 `materials` 是两个参数而不是一个：话术进正文、按 ref 逐字引，
        #: 文件进的是一个 `send_material` 提案 —— 模型对它们该做的事不同。
        assets: Sequence[Mapping[str, str]] = (),
        context: CallContext | None = None,
        #: 上一轮我们对这个客户说的那句话。**三步至此收同一份。**
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这通对话已经讲过哪几段（平台现读）。**跟进那一句的锚点是它** ——
        #: 用户 2026-09-18：「上一句不一定是关键，之前的内容才是」。
        materials_already_sent: Sequence[str] = (),
        #: 记住的那几件事，形状 `{ref, value}`（T363）。
        remembered_facts: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: **这个商家允许记哪几件事**，形状 `{key, description}`（2026-09-21）。
        #:
        #: 与 `remembered_facts` 是两回事：那一个是**已经知道的**，
        #: 这一个是**允许记的**。运维 2026-09-20 实测：模型只看得见前者时，
        #: 它把键名猜成了 `concern_topic`，而那次记忆写入被拒且客户看不见。
        rememberable_fields: Sequence[Mapping[str, Any]] = (),
        #: **挂着没答的那个问题**（运维缺陷 19）。写字的手要看得见它。
        pending_question: Any = None,
    ) -> InferenceDocument: ...


class DeterministicMockInference:
    """Local/CI model boundary double; it cannot perform side effects.

    It emits fully shaped step contracts so that contract, validator and
    pipeline tests exercise the same documents a real provider adapter must
    produce. It is not a behaviour model: it always acknowledges.
    """

    version = "mock-three-step-v2"
    provider = "mock"
    prompt_release = "mock-prompt-v2"

    def understand(
        self,
        *,
        envelope: dict[str, Any],
        aggregate: dict[str, Any],  # noqa: ARG002 - 应答替身：它有意不看这些，docstring 写明了理由
        profile: dict[str, Any],  # noqa: ARG002 - 同上，应答替身
        context: CallContext | None = None,  # noqa: ARG002 - 同上，应答替身
        last_outbound: str = "",  # noqa: ARG002 - 替身不读上下文
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 替身不读上下文
        memories_not_shown: int = 0,  # noqa: ARG002 - 替身不读上下文
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 替身不读上下文
    ) -> InferenceDocument:
        # `profile` carries the frame's declared slots. The mock does not read
        # them -- it produces no slot updates -- but the parameter is on the
        # port because the model-backed implementation cannot build its prompt
        # without them (T34), and a port whose two implementations take
        # different arguments is not a port.
        content = envelope.get("content") or {}
        text = str(content.get("text") or content.get("event") or "event")
        document: InferenceDocument = {
            "schema_version": "1.0.0",
            "event_id": str(envelope["event_id"]),
            "facts": [],
            "intents": [{"intent_label": "unclassified", "confidence": 1.0}],
            "slot_updates": [],
            "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
            "sentiment": "neutral",
            "risk_markers": ["none"],
            "customer_commitments": [],
            "uncertainties": [{"field": "intent_label", "reason": "deterministic double"}],
            # **替身也要产这一句** —— 合同里 `turn_summary` 是必填的
            # （理由见它的 description：一个「这一轮没产出」与「忘了产」
            # 在线路上长得一样）。替身不读上下文，所以它说的就是这件事本身。
            "turn_summary": "确定性替身：这一轮没有可摘要的内容。",
            "model_evidence": _empty_model_evidence(
                provider=self.provider, model=self.version, prompt_release=self.prompt_release
            ),
        }
        if text:
            document["facts"].append(
                {
                    "key": "inbound_text_present",
                    "typed_value": True,
                    "confidence": 1.0,
                }
            )
        validate_inference_contract("understanding-result", document)
        return document

    def read_silence(
        self,
        *,
        envelope: dict[str, Any],  # noqa: ARG002 - 应答替身：它有意不看这些
        aggregate: dict[str, Any],  # noqa: ARG002 - 同上
        profile: dict[str, Any],  # noqa: ARG002 - 同上
        context: CallContext | None = None,  # noqa: ARG002 - 同上
        last_outbound: str = "",  # noqa: ARG002 - 替身不读上下文
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同上
        memories_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
        silent_for_seconds: float = 0.0,  # noqa: ARG002 - 同上
        unanswered_rounds: int = 0,  # noqa: ARG002 - 同上
        offered_materials: Mapping[str, str] = MappingProxyType({}),  # noqa: ARG002 - 同上
    ) -> InferenceDocument:
        """**替身一律答 `ask_again`，而挑这一个花了一次全量门禁。**

        替身的判据是「**不改变别人的断言**」：它不读上下文，所以它的产出必须
        让管线的行为与这一步不存在时**逐字相同**。

        ## 第一版挑了 `wait`，而那是错的

        当时的推理是「哪个动作会**多**出东西」：`send_material` 要编一个 ref
        （每条走那条路的用例都会在素材那一层炸），`ask_again` / `hand_to_human`
        会多一条出站或一个人工任务 —— 于是 `wait` 看起来是唯一中性的。

        **那个推理少了一半：没问「哪个动作会把本来有的东西拿掉」。**
        `wait` 在理解步之前就返回，于是**每一条碰到定时器轮次的既有用例
        都不再产出出站** —— 全量门禁实测 **148 条红**，其中一族整片报的是
        `the timer turn produced no proactive outbox row`。

        **`wait` 不是中性的，它只是「不增」而已。** 真正中性的是
        `ask_again`：管线照旧走完理解 / 计划 / 生成，产出与这一步不存在时一样。

        ⚠️ **这不等于「`ask_again` 是生产的默认」**：生产里四个动作由模型判断
        （用户 2026-09-22：「让模型发挥」）。**想验 `wait` 或 `send_material`
        那两支的用例，自己给一个替身** —— `tests/e2e/` 里那条就是这么做的。
        """

        document: InferenceDocument = {
            "schema_version": "1.0.0",
            "reading": "确定性替身：它不读上下文，所以说不出这次静默是为什么。",
            "action": {
                "do": "ask_again",
                "material_ref": None,
                "because": "替身不判断；照旧走完这一轮是唯一不改变别人断言的动作。",
            },
            "model_evidence": _empty_model_evidence(
                provider=self.provider, model=self.version, prompt_release=self.prompt_release
            ),
        }
        validate_inference_contract("silence-reading-result", document)
        return document

    def plan(
        self,
        *,
        understanding: InferenceDocument,
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,  # noqa: ARG002 - 同上，应答替身
        required_strategy: str | None = None,
        assets: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 替身不提文件
        last_outbound: str = "",  # noqa: ARG002 - 替身不读上下文
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        known_memories: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 替身不读上下文
        memories_not_shown: int = 0,  # noqa: ARG002 - 替身不读上下文
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 替身不读上下文
        #: 上一次许了而没兑现的那几句（T367）。第一次尝试恒为空。
        unkept_promises: Sequence[str] = (),  # noqa: ARG002 - 替身不读上下文
        silence_reading: Mapping[str, Any] | None = None,  # noqa: ARG002 - 替身不读上下文
    ) -> InferenceDocument:
        current = aggregate["target_stack"][-1]
        # `frame_definitions` rather than a scan of `profile["target_frames"]`:
        # since B4-7 the frame the conversation is standing in may be the
        # platform's, and a scan would `StopIteration` on it -- inside the
        # pipeline's inference call, where the exception is not a rejection with
        # a reason code but a failed turn.
        definition = frame_definitions(profile)[str(current["definition_id"])]
        allowed = tuple(definition["allowed_strategy_set"])
        # **The rule's strategy wins when there is one** (B4-4d). The double is
        # not a behaviour model, but it has to obey the same constraint the
        # model-backed implementation is given, or every timer turn in the test
        # suite would exercise the safe-fallback path instead of the one
        # production takes.
        #
        # **The double checks the profile registered the strategy, which the
        # model-backed path does not have to** (independent review 2026-09-09,
        # P3-2). Two lines below this reads `profile["strategies"][strategy]`,
        # so a `time_rule_strategy_set` naming an unregistered strategy would
        # raise `KeyError` here instead of being refused -- and
        # `time_rule_strategy_set` is a list this batch introduced that nothing
        # but the shipped-profile cases validates. `inference_for_provider(
        # "mock")` is reachable in production, so the double must fail the same
        # way the real one does: fall through to the frame's own choice and let
        # the validator refuse the plan with a reason code.
        if (
            required_strategy is not None
            and required_strategy in STRATEGY_CATALOG
            and strategy_registration(profile, definition["id"], required_strategy) is not None
            and required_strategy in time_rule_strategy_set(definition)
        ):
            strategy = required_strategy
        else:
            strategy = "acknowledge" if "acknowledge" in allowed else allowed[0]
        # The platform's frames carry their own registrations (B4-7); asking the
        # profile for them would `KeyError` under a profile that never
        # registered `acknowledge`, which is the state ADR-0099 says a tenant
        # must be allowed to be in.
        registered = strategy_registration(profile, definition["id"], strategy) or {}
        constraints = list(registered.get("generation_constraints") or [])
        intent_kinds = tuple(registered.get("allowed_intent_kinds") or [])
        # **策略声明了必填槽位，替身就点名还空着的那些**（运维缺陷 19）。
        #
        # 在此之前这里恒 `[]`，而那时没有任何在架策略声明过 `required_slots`，
        # 恒空恰好合法。缺陷 19 那批给在架包补了声明，`STRATEGY_SKIPS_REQUIRED_SLOT`
        # 那道闸随之活了 —— **一个不点名的计划在有空槽时会被它拒**，
        # 替身要跟真实模型守同一条约束，否则每一个走到这些策略的连库用例
        # 都改走回退路径（实测 17 条红，症状是「用了别的策略」）。
        # 判据与闸逐字同侧：点名**还空着的**那些（填了的不用问）。
        still_empty = [
            str(name)
            for name in (registered.get("required_slots") or [])
            if not str((current.get("slots") or {}).get(str(name)) or "").strip()
        ]
        candidates = [
            {
                "kind": kind,
                "typed_payload": {},
                "idempotency_basis": f"{kind}:{understanding['event_id']}",
            }
            for kind in intent_kinds
            if kind == "send_message"
        ]
        document: InferenceDocument = {
            "schema_version": "1.0.0",
            "origin": "model",
            "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
            "selected_strategy_id": strategy,
            "strategy_reason_code": "DETERMINISTIC_ACKNOWLEDGE",
            "confidence": {
                "raw_score": 1.0,
                "calibrated_score": None,
                "assessment_version": self.version,
                "factor_codes": ["deterministic_output"],
            },
            "required_slot_requests": still_empty,
            "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
            "candidate_intents": candidates,
            "generation_constraints": constraints,
            "manual_review_reason": None,
            "model_evidence": _empty_model_evidence(
                provider=self.provider, model=self.version, prompt_release=self.prompt_release
            ),
        }
        validate_inference_contract("plan-result", document)
        return document

    def generate(
        self,
        *,
        plan: InferenceDocument,
        understanding: InferenceDocument,
        envelope: dict[str, Any],
        materials: Mapping[str, str],  # noqa: ARG002 - 同上，应答替身；它**故意**不读 materials，docstring 说明了
        knowledge: Mapping[str, str] = MappingProxyType({}),  # noqa: ARG002 - 同理，它也不读知识库
        assets: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 同理；替身永远只应答，不提文件
        context: CallContext | None = None,  # noqa: ARG002 - 同上，应答替身
        last_outbound: str = "",  # noqa: ARG002 - 同上，替身不读上下文
        conversation_summary: Sequence[str] = (),  # noqa: ARG002 - 同上
        summary_lines_not_shown: int = 0,  # noqa: ARG002 - 同上
        materials_already_sent: Sequence[str] = (),  # noqa: ARG002 - 同上
        #: 记住的那几件事，形状 `{ref, value}`（T363）。
        remembered_facts: Sequence[Mapping[str, str]] = (),  # noqa: ARG002 - 替身不读上下文
        memories_not_shown: int = 0,  # noqa: ARG002 - 替身不读上下文
        #: **这个商家允许记哪几件事**（2026-09-21）。替身不读它，但**必须收得下** ——
        #: 生产那一侧加了一个关键字参数而这里没跟上时，
        #: `contracts` 全绿而门禁 123 条红，一条都不在 `contracts` 里。
        rememberable_fields: Sequence[Mapping[str, Any]] = (),  # noqa: ARG002 - 替身不读上下文
        #: **挂着没答的那个问题**（运维缺陷 19）。替身不读它，但**必须收得下**。
        pending_question: Any = None,  # noqa: ARG002 - 替身不读上下文
    ) -> InferenceDocument:
        # **This double deliberately ignores `materials`.** It always
        # acknowledges; it is not a behaviour model, and a double that started
        # quoting materials would make every pipeline test look like evidence
        # that the real prompt carries them. What proves that is
        # `ModelBackedInference.generate` and the tests aimed at it.
        text = (
            "Scheduled event received."
            if envelope["event_type"] == "timer"
            else "Message received."
        )
        intents = [
            {"kind": candidate["kind"], "typed_payload": _send_payload(candidate, text)}
            | {"evidence_refs": [f"event:{understanding['event_id']}"]}
            for candidate in plan["candidate_intents"]
        ]
        document: InferenceDocument = {
            "schema_version": "1.0.0",
            "origin": "model",
            "strategy_id": plan["selected_strategy_id"],
            "customer_visible_segments": [{"type": "text", "content_or_ref": text}],
            "proposed_intents": intents,
            "confidence": {
                "raw_score": 1.0,
                "calibrated_score": None,
                "assessment_version": self.version,
                "factor_codes": ["deterministic_output"],
            },
            "claims": [],
            "return_confirmation": {"included": False, "tone_material_ref": None},
            "safety_markers": ["none"],
            "model_evidence": _empty_model_evidence(
                provider=self.provider, model=self.version, prompt_release=self.prompt_release
            ),
        }
        validate_inference_contract("generation-result", document)
        return document


def _send_payload(candidate: dict[str, Any], text: str) -> dict[str, Any]:
    if candidate["kind"] != "send_message":
        return dict(candidate["typed_payload"])
    return {"content": {"type": "text", "text": text}}


def safe_fallback_plan(
    *,
    reason_code: str,
    strategy_id: str,
    aggregate: dict[str, Any],
    rejected: InferenceDocument | None = None,
    manual_review_reason: str | None = None,
    slot_requests: Sequence[str] = (),
) -> InferenceDocument:
    """Deterministic platform plan used when a model plan is rejected.

    The generator is never asked to repair a plan itself; the platform decides,
    and the result is marked `origin=safe_fallback` so evidence never conflates
    it with a model decision.

    **`slot_requests` 是「这一轮还缺哪些槽位」**，由拒绝那一处带过来
    （闸二的 `PlanRejected.context["empty_required_slots"]`）。
    在它之前这份文档的 `required_slot_requests` **恒为空**，
    而那让 `ask_slot` 那条回退**能发消息却说不出内容** ——
    `_record_pending_question` 的第一个前提就是这个列表非空，
    于是「问了什么」从来没被记下过，模型也不知道该问哪个槽位（T339）。

    **它不改变今天的行为**：生产里唯一的调用方取默认 `strategy_id="no_message"`，
    那一支不产出候选意图，于是没有出站行、`_record_pending_question` 照旧早退。
    **改默认是另一批（T339 第二批）。**
    """

    document: InferenceDocument = {
        "schema_version": "1.0.0",
        "origin": "safe_fallback",
        "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        "selected_strategy_id": strategy_id,
        "strategy_reason_code": reason_code,
        "confidence": {
            "raw_score": 0.0,
            "calibrated_score": None,
            "assessment_version": "safe-fallback-v1",
            "factor_codes": ["fallback_applied"],
        },
        "required_slot_requests": list(slot_requests),
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": [],
        # **确定性的安全阀不请求检索**，而这一处写出来而不是靠缺省：
        # 缺席与 null 在读法上同义（`requested_knowledge_query`），
        # 明写 null 是为了让「平台决定不查」与「没人做过这个决定」在**读这份文档的人
        # 眼里**分得开。
        #
        # **不要把它说成「决定被记录下来了」**（独立审查 2026-09-12，P2-1）：
        # 裁定三 §4 那半条好处的前提是「plan 结果本来就落库」，而**今天不落** ——
        # 全仓没有一张表装 plan 文档，`pipeline` 只从它身上读五个字段。
        # 所以这一处是**给 6b / 6c 的证据面预留**的，兑现不了的那半条好处
        # 归用户裁决（补一条 plan 落库，还是把口径收敛到 `retrieval_evidence` 上）。
        # 校验器另有一道闸拦带着查询串的 safe_fallback（`FALLBACK_REQUESTS_RETRIEVAL`）。
        "knowledge_query": None,
        "manual_review_reason": manual_review_reason,
        "model_evidence": _rejected_evidence(rejected),
    }
    if strategy_id != "no_message":
        document["candidate_intents"] = [
            {
                # **两个「问」的打法都发消息**：漏掉 `ask_clarification`
                # 它会静默地变成 `handoff` —— 一个本该问一句话的回合转了人工。
                "kind": (
                    "send_message"
                    if strategy_id in ("ask_slot", "ask_clarification")
                    else "handoff"
                ),
                "typed_payload": {},
                "idempotency_basis": (
                    f"{reason_code}:{aggregate['id']}:{aggregate['state_version']}"
                ),
            }
        ]
    validate_inference_contract("plan-result", document)
    return document


def _rejected_evidence(rejected: InferenceDocument | None) -> dict[str, Any]:
    if rejected is None:
        return _empty_model_evidence(
            provider="platform", model="safe-fallback", prompt_release="safe-fallback-v1"
        )
    evidence = dict(rejected["model_evidence"])
    return evidence


def inference_for_provider(
    provider: str,
    *,
    transport: Any | None = None,
    environment: str = "local",
    governor: Any | None = None,
) -> InferencePort:
    """Resolve the configured provider to an inference implementation.

    A non-mock provider is served from the active model policy release, which
    refuses Stage and Production while it still carries unverified fields. That
    refusal is the point: filling a model id from the legacy system is enough to
    build against, and not enough to serve customers with.

    **三支，而前两支都不开 socket**：

    * `mock` —— `DeterministicMockInference`，判据是「不改变别人的断言」。
      **它永远应答、不读上下文**，所以它适合给用例当替身，
      **不适合演示**（演示会看成一个只会「好的」的机器人）；
    * `scripted` —— `ScriptedInference`，按「场景 + 轮次序号」回放录下来的
      四步文档。**演示用的是这一支**，理由与边界写在那个模块的 docstring 里；
    * 其余 —— 真供应商，从在架 model policy release 解析绑定。

    `mock` 与 `scripted` 两支都由 `Settings.from_env` 挡在 local / ci 之外
    （`runtime/settings.py`），所以这里不重复那道闸 —— **重复一道闸会让
    「它由谁挡着」有两个答案**，而改的时候只会改一处。
    """

    if provider == "mock":
        return DeterministicMockInference()

    if provider == "scripted":
        from fastagent_v2.worker.inference_scripted import ScriptedInference

        return ScriptedInference.from_env()

    from fastagent_v2.worker.inference_model import ModelBackedInference
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import HttpxChatTransport, OpenAICompatibleChatClient

    release = active_release()
    if provider not in {release.role(role).provider_id for role in release.document["roles"]}:
        raise RuntimeError(
            f"provider {provider!r} is not bound by model policy release {release.version}"
        )
    release.require_servable(environment)

    if transport is None:
        import httpx

        transport = HttpxChatTransport(httpx.Client())
    from fastagent_v2.worker.inference_model import PROMPT_RELEASE

    return ModelBackedInference(
        release=release,
        client=OpenAICompatibleChatClient(
            transport=transport, prompt_release=PROMPT_RELEASE
        ),
        governor=governor,
    )
