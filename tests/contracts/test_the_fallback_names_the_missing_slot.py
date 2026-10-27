"""被拒之后的回退计划**点名闸二报出来的那些空槽位**（T339 第一批）。

## 这一批做了什么，没做什么

**做了**：把闸二已经带出来的事实（`PlanRejected.context["empty_required_slots"]`）
接到回退计划的 `required_slot_requests` 上。
**没做**：不改默认。生产里唯一的调用方仍取 `fallback_strategy="no_message"`，
那一支不产出候选意图 —— **所以本批行为一个字不变**，
而下面 `test_the_default_fallback_still_makes_no_outbound_candidate` 就是这句话的断言。

## 消费方清单（按「接上才准进」那条纪律：先列清，再逐个走通）

`required_slot_requests` 被填上之后，**今天有四处会读它**：

| 消费方 | 本批走通了吗 |
|---|---|
| ① 这份文档自己（回退计划产出它） | ✅ `test_the_fallback_names_the_slots_the_gate_reported` |
| ② 本帧槽位过滤（`_slots_this_frame_has`） | ✅ `test_a_slot_this_frame_does_not_have_…` |
| ③ `_record_pending_question` 的第一个前提（非空才记） | ✅ **第二批走通**（真驱动，见下） |
| ④ 生成提示词里的 `required_slot_requests` 那一项 | ✅ `test_the_generation_prompt_carries_…` |
| ⑤ 闸二自己 / `SLOT_NOT_IN_FRAME` | **有意不读** —— 见下 |

**③④ 在第二批走通，那张表现在闭环了。** 第一批留的是 ⬜ 而不是造一条路径 ——
理由写在当时：它们**要求这一轮真的发出一条消息**，而那在默认改掉之前
在生产里不可达，**造一条路径就是造一个生产里不存在的形状**。
现在那条路真的可达了，两条断言才落在真行为上：

* **③** 在 `tests/e2e/test_required_slot_loop_escalates.py` 里 ——
  三轮真驱动，第一轮之后 `pending_question.slot` 就是那个空着的槽位，
  `answer_evidence is None`（「问了、还没答」）；
* **④** 在本文件，**计划来自生产那条缝**（真拒绝 → 真回退）而不是手写的字典。

**五个消费方现在的结论**：① ✅ · ② ✅ · ③ ✅ · ④ ✅ · ⑤ **有意不读**。

**⑤ 是「有意不读」，不是漏了**：`SLOT_NOT_IN_FRAME` 与闸二**在同一个函数里**
（`_validate_model_plan_against_profile`），而那个函数对
`origin == "safe_fallback"` 整条豁免（ADR-0137 裁定一写下的适用范围）。
**豁免在填了槽位之后仍然必要** —— 回退计划不该被它自己要去问的那个槽位拦住，
否则安全阀会永远被拒，而它是被拒之后唯一的出路。
`test_the_fallback_is_still_exempt_from_the_gate_it_answers` 钉住这一点，
**免得下一个人看到「现在它点名槽位了」就以为那条豁免可以去掉。**
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastagent_v2.worker.inference_validation import plan_or_fallback

# **夹具直接复用闸二那份用例的**，不自己再造一份 —— 第一版自造的
# `_understanding()` 多了三个 schema 不认的键（`candidate_intents` / `confidence` /
# `exit_signals`），四条当场 `ValidationError`。
# 「自造夹具藏得住形状不匹配」是本仓付过账的那一类：**跨边界的结构要用上游的构造点**。
from test_strategy_does_not_skip_required_slots import (  # noqa: PLC0415
    STRATEGY,
    _aggregate,
    _plan,
    _profile,
    _understanding,
)


def _production_shaped(aggregate: dict[str, Any]) -> dict[str, Any]:
    """补上 `safe_fallback_plan` 真正会读的那两个键。

    `idempotency_basis` 读 `aggregate['id']` 与 `['state_version']`，
    而 contracts 那份聚合夹具**没有它们** —— T340 就是这么被发现的
    （2026-09-13 探针第一次跑抛 `KeyError: 'id'`，
    说明那条回退分支从来没有被任何用例走过）。
    生产里这两个键来自 `conversation_aggregates` 那一行。
    """

    aggregate.setdefault("id", "11111111-1111-4111-8111-111111111111")
    aggregate.setdefault("state_version", 7)
    return aggregate


def _reject_and_fall_back(
    profile: dict[str, Any], *, fallback_strategy: str | None = "no_message"
) -> dict[str, Any]:
    """跑一次真实拒绝，返回回退计划。

    **`fallback_strategy=None` 才是「让被测代码自己选」** ——
    本 helper 的那个默认值是为了让不关心这件事的用例保持稳定，
    **而任何要断言「它选了什么」的用例必须显式传 `None`**：
    一条用例不能一边声明那个值、一边断言它（见
    `test_a_rejection_that_can_name_the_slot_now_asks` 的 docstring）。
    """
    validated, rejection = plan_or_fallback(
        plan=_plan(profile, requests=[]),
        understanding=_understanding(),
        aggregate=_production_shaped(_aggregate(profile)),
        profile=profile,
        fallback_strategy=fallback_strategy,
    )
    assert rejection is not None, "那份计划该被闸二拒 —— 夹具坏了，下面的断言都不算"
    assert rejection.reason_code == "STRATEGY_SKIPS_REQUIRED_SLOT", (
        f"红的不是要测的那一条：{rejection.reason_code}"
    )
    return validated.document


def test_the_fallback_names_the_slots_the_gate_reported() -> None:
    """消费方① —— **回退计划点名那两个空槽位，而它们来自拒绝那一处**。

    在这一批之前这个列表恒为空，于是 `ask_slot` 那条回退
    「能发消息却说不出内容」（T339）。
    """

    profile = _profile()
    declared = list(profile["strategies"][STRATEGY]["required_slots"])
    assert len(declared) >= 2, (
        f"这条用例依赖 {STRATEGY!r} 声明至少两个必填槽位，实测 {declared}"
    )

    document = _reject_and_fall_back(profile)

    assert document["required_slot_requests"] == declared, (
        "回退该点名闸二报出来的那些槽位，顺序照它给的来"
    )


def test_a_slot_this_frame_does_not_have_is_filtered_out() -> None:
    """消费方② —— **本帧没有的槽位名不进那个列表**。

    这种情形是可能的：§4.1 那道封版闸要求的是「**某个**允许该策略的帧里有这个槽位」，
    不是「每个帧都有」。而回退计划**不经过** `SLOT_NOT_IN_FRAME`
    （它与闸二同在一个对回退豁免的函数里），**所以这里是唯一的过滤点** ——
    不过滤的话，那个名字会一路写进 `pending_question.slot`。
    """

    profile = _profile()
    ghost = "a_slot_this_frame_does_not_declare"
    profile["strategies"][STRATEGY]["required_slots"] = [
        *profile["strategies"][STRATEGY]["required_slots"],
        ghost,
    ]

    document = _reject_and_fall_back(profile)

    assert ghost not in document["required_slot_requests"], (
        "本帧没有这个槽位，回退不该点名它 —— 点了名就会写进 pending_question.slot，"
        "而那一格从此指着一个这个帧里不存在的东西"
    )
    assert document["required_slot_requests"], (
        "过滤不该把整个列表清空 —— 另外两个槽位本帧是有的"
    )


def test_a_rejection_that_can_name_the_slot_now_asks() -> None:
    """**第二批的行为断言**：点得出槽位名的拒绝，回退选 `ask_slot` 并带一条出站候选。

    > ⚠️ **这条用例替换的那一条，是一个「重新声明了被测值」的标本。**
    >
    > 第一批写的是 `test_the_default_fallback_still_makes_no_outbound_candidate`，
    > 它断言 `selected_strategy_id == "no_message"` —— 看起来在钉住那个默认。
    > **而它钉不住任何东西**：本文件的 `_reject_and_fall_back` 自己声明了
    > `fallback_strategy: str = "no_message"`，**于是它拿到的是它自己给的值**。
    > 2026-09-13 把生产默认翻掉时，**这条用例没红** ——
    > 那次「它该红而没红」才是发现它的唯一途径。
    >
    > 所以本条**不传** `fallback_strategy`：让 `plan_or_fallback` 自己按数据选。
    """

    document = _reject_and_fall_back(_profile(), fallback_strategy=None)

    assert document["selected_strategy_id"] == "ask_slot", (
        "点得出槽位名的拒绝该问出去 —— 而这个值必须来自被测代码，不是本用例给的"
    )
    assert [item["kind"] for item in document["candidate_intents"]] == ["send_message"]
    assert document["required_slot_requests"], "问什么也要带上"


def test_a_rejection_that_cannot_name_a_slot_is_still_silent() -> None:
    """**另一半，而它是 59 个理由码的那一半**：点不出槽位名 → 仍然 `no_message`。

    只有闸二那一条拒绝带得出槽位名（它的 `PlanRejected.context`）。
    **给别的拒绝也选 `ask_slot`，就是「发一条消息而不说问什么」** ——
    那正是 T339 判定「今天的默认不是一次选择」时说的坏形状。
    证法：把那条策略声明的必填槽位换成一个本帧没有的名字，
    于是过滤之后没有可点名的 —— 回退必须退回静默。
    """

    profile = _profile()
    profile["strategies"][STRATEGY]["required_slots"] = ["a_slot_this_frame_lacks"]

    document = _reject_and_fall_back(profile, fallback_strategy=None)

    assert document["required_slot_requests"] == []
    assert document["selected_strategy_id"] == "no_message", (
        "说不出内容就不该说话 —— 一条「问了而不说问什么」的消息比沉默更糟"
    )
    assert document["candidate_intents"] == []


def test_the_fallback_is_still_exempt_from_the_gate_it_answers() -> None:
    """消费方⑤ —— **有意不读**：回退计划仍然不经过闸二，而那仍然是必要的。

    回退的 `required_slot_requests` 现在非空了，**但那不是它能过闸的原因** ——
    它能过是因为 `origin == "safe_fallback"` 整条豁免。
    证法：把那两个槽位从回退会点名的集合里去掉（配包改成声明一个本帧没有的槽位，
    于是过滤之后列表为空），**回退计划照样过** ——
    如果它靠点名槽位过闸，这一下就会被拒。
    """

    profile = _profile()
    profile["strategies"][STRATEGY]["required_slots"] = ["only_a_slot_this_frame_lacks"]

    document = _reject_and_fall_back(profile)

    assert document["required_slot_requests"] == [], "过滤之后该是空的，这一步是前提"
    assert document["origin"] == "safe_fallback", (
        "而它照样是一份通过校验的回退计划 —— 豁免仍然在，不是靠点名槽位过的"
    )


def test_the_two_keys_the_fallback_reads_are_load_bearing() -> None:
    """T340 那件事的守卫：**那两个键是承重的，而上游夹具没有它们**。

    `safe_fallback_plan` 的 `idempotency_basis` 读 `aggregate['id']` 与
    `['state_version']`。**闸二那份用例的聚合夹具没有这两个键** ——
    2026-09-13 的探针第一次跑当场 `KeyError: 'id'`，
    而那说明**那条回退分支从来没有被任何用例走过**（T340）。

    这一条把三件事一起钉住：上游夹具确实缺；`_production_shaped` 补上了；
    **而缺了真的会炸** —— 最后这一句是「承重」的证据，
    不然下一个人会以为那两行 `setdefault` 是多余的防御。
    """

    # **上游夹具现在带着这两个键了**（T339 第二批补的，那一处写了为什么）——
    # 所以本条的证法从「上游缺 → 会炸」改成「**拿掉它们 → 会炸**」。
    # 证的事情没变：**那两个键是承重的**。
    stripped = _aggregate(_profile())
    assert "id" in stripped, "上游夹具不再带这个键了 —— 本条的前提变了，跟着改，别留着空转"
    del stripped["id"]

    # **要用会读那两个键的那一支** —— `no_message` 不读它们
    # （`idempotency_basis` 只在产出候选意图时才拼）。
    # 第一版这里没传 `fallback_strategy`，于是 `pytest.raises` 空等了一场 ——
    # **而那恰好证明了「哪一支读它」这件事本身需要被断言。**
    with pytest.raises(KeyError):
        plan_or_fallback(
            plan=_plan(_profile(), requests=[]),
            understanding=_understanding(),
            aggregate=stripped,
            profile=_profile(),
            fallback_strategy="ask_slot",
        )


def test_the_generation_prompt_carries_the_slots_the_fallback_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**消费方④**：那些槽位名真的进了生成提示词。

    第一批把这一条留成 ⬜ 并写死在第二批 —— 因为在默认改掉之前，
    生产里没有哪一轮会拿着一份**非静默**的回退计划去生成。现在有了。

    **计划来自生产那条缝**（真拒绝 → 真回退），不是手写的字典：
    一条把提示词该收到什么自己拼出来的用例，只证明「提示词会格式化它收到的东西」，
    而那件事从来没有人怀疑过。
    """

    from fastagent_v2.worker.inference_model import (  # noqa: PLC0415
        ModelBackedInference,
        OpenAICompatibleChatClient,
    )
    from fastagent_v2.worker.model_policy import active_release  # noqa: PLC0415
    from test_generation_receives_materials import (  # noqa: PLC0415 - suite-local harness
        CREDENTIAL_ENV,
        _acknowledgement,
        _model_reply,
        _RecordingTransport,
    )

    document = _reject_and_fall_back(_profile(), fallback_strategy=None)
    assert document["required_slot_requests"], "前提：这份回退点名了槽位"

    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    transport = _RecordingTransport(_model_reply(_acknowledgement()))
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    )
    inference.generate(
        plan=document,
        understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        materials={},
    )

    assert transport.calls, "替身没被调用 —— 那这条用例什么都没看到"
    sent = json.dumps(transport.calls[0]["payload"], ensure_ascii=False)
    for slot in document["required_slot_requests"]:
        assert slot in sent, (
            f"模型该收到要问的槽位名 {slot!r} —— 收不到的话它只能自己编要问什么，"
            "而那正是 T339 说的「能发消息却说不出内容」"
        )
