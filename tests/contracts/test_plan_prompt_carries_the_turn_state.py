"""计划步的提示词里**真的带着**那两样每回合状态（B1-2 / B1-1）。

`pending_question` 与 `focus` 在此之前都是「写了没人读」——
核心图纸对比 2026-09-13 把它们归在 B1：**两头都在，中间没接**。
接上写者之后，**「模型有没有看见」是另一件事**，而这一份问的就是它。

**不是在序列化后的请求里搜子串**：提示词自己是一份 JSON，
一段带换行的值在里面长的是 `\\n` —— 于是「短值过、长值不过」，
量的就成了序列化器而不是提示词（素材那一份的同名 helper 记着这次踩坑）。
这里把 `user_prompt` **解析回对象**再数键。

**为什么这两键在 `user_prompt` 而不是 system prompt**（用户 2026-09-13 定的理由）：
(1) 它们**每回合都不同**，而 system prompt 描述的是这个角色**恒定**的规则；
(2) system prompt 是 prompt caching 的**稳定前缀**，
把逐轮变化的数据塞进去会让那段前缀每轮失效。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_model import ModelBackedInference
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import OpenAICompatibleChatClient, TransportResponse

CREDENTIAL_ENV = "FASTAGENT_MODEL_API_KEY_DEEPSEEK"
FRAME_ID = str(uuid4())


class _RecordingTransport:
    """Test double. It never opens a socket."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append({"payload": payload})
        return TransportResponse(status_code=200, body=self.body)


def _plan_reply() -> dict[str, Any]:
    document = {
        "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        "selected_strategy_id": "acknowledge",
        "strategy_reason_code": "TEST",
        "confidence": {
            "raw_score": 0.9,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": [],
        "manual_review_reason": None,
    }
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(document, ensure_ascii=False)}
                ],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _profile() -> dict[str, Any]:
    return {
        "target_frames": [
            {
                "id": "welcome",
                "goal": "greet",
                "entry_conditions": [],
                "exit_conditions": [],
                "information_slots": [
                    {
                        "name": "request_summary",
                        "type": "string",
                        "required": True,
                        "description": "What this slot records.",
                    }
                ],
                "allowed_strategy_set": ["acknowledge"],
                "content_materials": [],
                "time_rules": [],
                "return_confirmation": {"policy": "optional", "tone_material_ref": None},
                "material_topic_slot": "request_summary",
            }
        ],
        "strategies": {
            "acknowledge": {"allowed_intent_kinds": [], "generation_constraints": []}
        },
    }


def _aggregate(**overrides: Any) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "target_stack": [
            {
                "id": FRAME_ID,
                "definition_id": "welcome",
                "goal": "greet",
                "status": "active",
                "slots": {},
            }
        ],
        "pending_question": None,
        "focus": {},
    }
    aggregate.update(overrides)
    return aggregate


def _sent_prompt(
    monkeypatch: pytest.MonkeyPatch,
    aggregate: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`profile` 可选，默认用 `_profile()` —— 加它是为了让策略侧的键也能被驱动。"""

    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    transport = _RecordingTransport(_plan_reply())
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    )
    inference.plan(
        understanding={
            "event_id": "00000000-0000-4000-8000-000000000000",
            "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        },
        aggregate=aggregate,
        profile=profile if profile is not None else _profile(),
    )
    assert transport.calls, "the provider was never called"
    return dict(json.loads(str(transport.calls[0]["payload"]["input"])))


def test_an_unanswered_question_reaches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这一条是 B1-2 的「读」那一半。**

    悬着的问题原样进提示词：模型据此不必把同一个槽位再问一遍。
    """

    pending = {
        "question_id": str(uuid4()),
        "frame_id": FRAME_ID,
        "slot": "request_summary",
        "asked_at": "2026-09-13T00:00:00+00:00",
        "answer_evidence": None,
    }

    prompt = _sent_prompt(monkeypatch, _aggregate(pending_question=pending))

    assert prompt["pending_question"] == pending, (
        "上一轮问过什么、答了没有 —— 这一整条要到模型面前，"
        "只给一个布尔值它就分不出「问过且已答」与「问过还没答」"
    )


def test_the_key_is_there_even_when_nothing_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有悬着的问题时**发 `null`，不省略这个键**。

    与 `content_materials` 空着也发同一条理由：**缺键**在模型看来是
    「这个系统没有「上一轮问过什么」这回事」，而 `null` 是「这一轮没有悬着的」。
    前者会让它更敢重复提问。
    """

    prompt = _sent_prompt(monkeypatch, _aggregate())

    assert "pending_question" in prompt
    assert prompt["pending_question"] is None


def test_the_focus_reaches_the_model_with_its_two_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`focus` 的两类原样进提示词（B1-1）。

    product / order **不在**这个投影里 —— 那条边界由
    `tests/contracts/test_focus_projection.py` 钉住，这里只验它传到了模型面前。
    """

    focus = {
        "contact": {"contact_id": str(uuid4())},
        "topic": {"slot": "request_summary", "value": "退货"},
    }

    prompt = _sent_prompt(monkeypatch, _aggregate(focus=focus))

    assert prompt["focus"] == focus


def test_the_system_prompt_did_not_grow_for_either_of_them() -> None:
    """**两键都在 `user_prompt` 里，system prompt 一个字没动。**

    理由是这一批的设计决定（用户 2026-09-13）：逐轮变化的数据放进
    **稳定前缀**里，既在语义上是错的，也会让 prompt caching 每轮失效。

    这一条顺带守着一件事：`PLATFORM_PROMPT_BYTES["PLAN_CAPABILITY"]` 是
    **按 system prompt 实测的**，它没变，说明我们确实没往那一侧加字。
    """

    from fastagent_v2.worker.inference_model import _PLAN_SYSTEM

    # ⚠️ **这两条原来写成 `"pending_question" not in _PLAN_SYSTEM`** ——
    # 「某几个字不许出现」的判据，分不清「逐轮的数据进了稳定前缀」（要禁）
    # 与「多了一条讲这个键的恒定规则」（运维缺陷 19 补的，恰恰该有：
    # 在此之前模型收着这个键而两个 system prompt 里 `pending` 零命中，
    # T200 那一族）。本仓同一个坑当月已经踩了四次。
    # 改钉真正的判据：**数据形状**（JSON 键 `"pending_question":`）不出现 ——
    # 规则句提名字可以，示例或内插数据不行。
    assert '"pending_question":' not in _PLAN_SYSTEM, (
        "system prompt 里出现了 pending_question 的数据形状 —— "
        "逐轮变化的数据放进稳定前缀会让 prompt caching 每轮失效"
    )
    assert "focus" not in _PLAN_SYSTEM

    # **字节那一侧不在这里再钉一遍**：`PLATFORM_PROMPT_BYTES` 与三个 system prompt
    # 的相等由 `test_profile_materials_are_trusted.py::
    # test_the_platform_prompt_table_matches_the_prompts` 钉着，**而它量的是线路口径**
    # （`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`）。
    # 我第一版在这里用 `len(text.encode())` 又量了一遍，当场红在 2600 != 2703 ——
    # **那不是提示词变了，是我拿了第二把尺子**。一把尺子只该有一处
    # （`AGENTS.md`「每处都要写成同一个形状」）。


# --------------------------------------------------------------------------
# 裁定一步骤 3 的硬前置：每个策略声明的必填槽位要到模型面前（2026-09-13）
# --------------------------------------------------------------------------


def test_each_allowed_strategy_brings_its_required_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**这一条是那道闸能不能成立的前提。**

    `STRATEGY_SKIPS_REQUIRED_SLOT` 判的是「这个策略声明的必填槽位空着，
    而计划既不填也不问」。而在这个键之前，**模型从来没有被告知过
    每个策略声明了哪些必填槽位** —— 提示词里的 `current_frame.required_slots`
    给的是帧的 `information_slots[].required`，是**另一个**数据源。

    先例的代价是实测过的（`inference_model.py` 那段注释，2026-09-10）：
    `generation_constraints` 校验而不告知 → **七个事件、六份计划被拒、零条消息发出**，
    而编出来的名字是声明值的近似。**判一个人跳过了他没被告知的要求，是不公平的。**
    """

    profile = _profile()
    profile["strategies"]["acknowledge"]["required_slots"] = ["request_summary"]

    prompt = _sent_prompt(monkeypatch, _aggregate(), profile=profile)

    assert prompt["required_slots_by_strategy"] == {"acknowledge": ["request_summary"]}


def test_a_strategy_that_declares_none_still_gets_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声明了空列表时发 `[]`，**不省略这个策略**。

    与 `content_materials` 空着也发同一条理由：**缺席在模型看来是
    「这里没有这回事」**，而 `[]` 是「这个策略不要求任何槽位」。
    前者会让它去猜 —— 而猜的样子就是给出一个近似（`AGENTS.md` 同名一节）。
    """

    profile = _profile()
    profile["strategies"]["acknowledge"]["required_slots"] = []

    prompt = _sent_prompt(monkeypatch, _aggregate(), profile=profile)

    assert prompt["required_slots_by_strategy"] == {"acknowledge": []}


def test_a_strategy_omitting_the_field_entirely_does_not_crash_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**schema 没把这个键设成 `required`，所以生产必须容得下它缺席。**

    ADR-0137 裁定一步骤 1 给 `strategies` 补形状时**没有加 `required`**
    （收紧封闭性划在那一步之外），于是一个策略可以只写 `allowed_intent_kinds`。
    缺席的意思是「这个策略不声明必填槽位」—— 那是一句真话，不是一个错误。

    **这条用例来自一次真实的当场失败**：本批第一版用的是下标
    （`profile["strategies"][name]["required_slots"]`），
    而本文件的 `_profile()` 夹具从来没写过这个键 —— 三条既有用例当场
    `KeyError`。**夹具比我先知道 schema 允许什么。**
    """

    profile = _profile()
    assert "required_slots" not in profile["strategies"]["acknowledge"], (
        "这条用例的前提是夹具不写这个键；夹具改了就把断言换成 pop"
    )

    prompt = _sent_prompt(monkeypatch, _aggregate(), profile=profile)

    assert prompt["required_slots_by_strategy"] == {"acknowledge": []}


def test_the_system_prompt_tells_the_model_what_to_do_with_the_key() -> None:
    """**数据与指令两半都在。** 这条用例是翻过面的，翻面本身要留痕。

    **上一版断言的是反面**：「系统提示词里**没有**这个键」——
    那时数据那一半刚落地（裁定一 ①），而指令那一半还没有，
    于是它是一条**有意会过期**的用例，报错里写着「这是指令那一半落地的信号，
    把本条换成断言那句指令在」。

    指令当天就落地了（用户 2026-09-13 裁定：加），于是它红了 ——
    **红得对，它不是坏了，是到期了**（`AGENTS.md` 同名一节）。
    现在它守的是相反的事：**这句指令别被人删掉。**

    为什么值得守：没有它，`STRATEGY_SKIPS_REQUIRED_SLOT` 判的是「模型没点名
    它从未被要求去点名的东西」。先例的账是实测的 —— `generation_constraints`
    校验而不告知，七个事件、六份计划被拒、零条消息发出。
    **数据给了而指令没给，只是把那次事故推迟到下一个不看提示词的人。**
    """

    from fastagent_v2.worker.inference_model import _PLAN_SYSTEM

    assert "required_slots_by_strategy" in _PLAN_SYSTEM, (
        "plan 的系统提示词不再点名这个键 —— 那道 STRATEGY_SKIPS_REQUIRED_SLOT 闸"
        "从此在判「模型没做一件没人要求过它做的事」。要么把这句指令加回去，"
        "要么先撤掉那道闸"
    )
    # **点名那个键不够，还要说清「怎么算点名了」**：三条边界里最容易被写漏的是
    # 「问了别的槽位不算」——少了它，这句指令会被读成「问点什么就行」。
    assert "does not count" in _PLAN_SYSTEM, (
        "那句指令不再说明『什么不算点名了』—— 三条边界（点名那个槽位才算 / "
        "问别的不算 / 正文里捎带不算）里至少丢了一条"
    )
    # 对照项：那个键从一开始就是两半都有的，本条靠它说明「两半」是什么意思。
    assert "allowed_generation_constraints_by_strategy" in _PLAN_SYSTEM


def test_the_prompt_assembles_when_a_strategy_omits_every_optional_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个只写了名字的策略，**提示词照样组装得出来**。

    三个键在 schema 里都是可选的（步骤 1 没加 `required`），所以
    `{"acknowledge": {}}` 是一份合法声明 —— 它的意思是「这条打法不许带意图、
    不带约束、不要求槽位」。提示词组装读这三个键，缺一个就抛 `KeyError` 的话，
    **一份合法的 profile 会让整个回合在推理之前就死掉**，
    而那条异常里没有任何东西指向「是 profile 少写了键」。

    **这条用例来自一支全绿的探针**：把提示词侧那两处 `.get` 改回下标时，
    二十一条用例全绿 —— 也就是说那两处的 `.get` 当时没有任何东西守着。
    `AGENTS.md`「探针的价值在它绿的时候」说的正是这种。
    """

    profile = _profile()
    profile["strategies"]["acknowledge"] = {}

    prompt = _sent_prompt(monkeypatch, _aggregate(), profile=profile)

    assert prompt["allowed_intent_kinds_by_strategy"] == {"acknowledge": []}
    assert prompt["allowed_generation_constraints_by_strategy"] == {"acknowledge": []}
    assert prompt["required_slots_by_strategy"] == {"acknowledge": []}
