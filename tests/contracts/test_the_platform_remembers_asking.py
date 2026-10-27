"""同一个问题问了两次，而三个记账位置一个都没记（运维缺陷 19，2026-09-23）。

## 现场

```
08:56:34  AI    …您那边现在客户消息主要是谁在盯？          ← 第一次问
08:56:55  客户  肯定是销售啊                              ← 答了
08:57:01  AI    ① 您自己做销售…（用上了这个答案）
          AI    ③ 您那边现在客户消息主要是谁在盯？          ← 又问一遍
```

两轮都 `APPLIED`，没有任何拒绝或告警。

## 根因链（运维逐步现读，本批逐步核过）

`request_shape` 的 `required_slots_by_strategy` 直接读配包的
`strategies[].required_slots` → **出事的包八个策略全是 `[]`** → 模型的
`required_slot_requests` 恒 `[]` → `_record_pending_question` 第一行恒早退 →
**`pending_question` 结构性地永远是 `None`** —— 而它正是
「别再问第二遍」的那个机制。连带 `STRATEGY_SKIPS_REQUIRED_SLOT` 永不触发，
它在 `LOOP_ESCALATION_REASON_CODES` 里的升级路径也是死的。

## 用户裁定：平台据真的发出去的正文记

判据从「计划点名了槽位」（模型的声称）改成**发出去的客户可见正文里有问号**
（确定性）。这同时关掉 T331：先前记的是「点名了 + 发了消息」——
**它记的是声称不是问**。

## 本批三层，各有守卫

| 层 | 改动 |
|---|---|
| 平台 | `_record` 判正文；`_settle` 加 null-slot 支；生成步透传 `pending_question` |
| 提示词 | 计划步 + 生成步各一条规则（此前**两个 system prompt 里 `pending` 零命中**，T200 那一族）|
| 配包 | demo 声明 `current_handling`；third 声明 `request_summary`；封版守卫两道 |

⚠️ **carrier 不动**：它本来就有两条非空声明（`propose_quote` / `propose_followup`）
—— 「三个包每一个策略全空」是作者第一版读扫描表读漏的，全空的是 demo 与 third。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_required_slots_exist_in_every_allowed_frame,
    _check_some_strategy_declares_a_required_slot,
)
from fastagent_v2.worker.inference_model import _GENERATE_SYSTEM, _PLAN_SYSTEM
from fastagent_v2.worker.pipeline import (
    ProposalDecision,
    _record_pending_question,
    _settle_pending_question,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WHEN_EVIDENCE = "19191919-1919-4919-8919-191919191919"


class _Prepared:
    """`_record_pending_question` / `_settle` 只读这两格，轻量替身够用。"""

    def __init__(self, *, event_type: str = "customer_message") -> None:
        self.aggregate: dict[str, Any] = {
            "target_stack": [{"id": "frame-1", "definition_id": "introduce"}],
            "pending_question": None,
        }
        self.envelope: dict[str, Any] = {"event_type": event_type}


class _Plan:
    def __init__(self, requested: list[str] | None = None) -> None:
        self.document = {"required_slot_requests": requested or []}


def _sent(text_content: dict[str, Any]) -> ProposalDecision:
    return ProposalDecision(
        ordinal=0,
        kind="send_message",
        accepted=True,
        reason_code="OK",
        detail="",
        intent_id=uuid4(),
        proposal={"typed_payload": {"content": text_content}},
    )


def _record(
    prepared: _Prepared,
    decision: ProposalDecision,
    *,
    requested: list[str] | None = None,
    materials: dict[str, str] | None = None,
) -> None:
    _record_pending_question(
        prepared,  # type: ignore[arg-type]
        plan=_Plan(requested),  # type: ignore[arg-type]
        delivered=[decision],
        materials=materials,
    )


# ---------------------------------------------------------------------------
# record：判据是正文，不是声称
# ---------------------------------------------------------------------------


def test_a_question_in_the_sent_text_is_recorded_without_any_slot_naming() -> None:
    """**正文里有问句就记，计划一个槽都没点名也记** —— 本批的兑现物。

    修复前这正是恒早退的那条路：出事的包八个策略 `required_slots` 全空，
    `required_slot_requests` 恒 `[]`，于是「问了」这件事没有任何地方记得。
    """

    prepared = _Prepared()
    _record(prepared, _sent({"type": "text", "text": "您那边现在客户消息主要是谁在盯？"}))

    pending = prepared.aggregate["pending_question"]
    assert pending is not None, "问出去了而没记 —— 缺陷 19 的原样"
    assert pending["slot"] is None
    assert pending["question_text"].endswith("谁在盯？")
    assert pending["answer_evidence"] is None


def test_a_statement_is_not_recorded_as_a_question() -> None:
    """**反侧：没有问句就不记。**

    没有这一条，一个「发了就记」的实现会让上面那条绿 ——
    而那样每一轮都「挂着一个问题」，防重复规则会拦住正常的推进。
    """

    prepared = _Prepared()
    _record(prepared, _sent({"type": "text", "text": "价格是一个月 1888，前十天免费试用。"}))

    assert prepared.aggregate["pending_question"] is None


def test_a_question_inside_a_quoted_material_counts() -> None:
    """素材原文里带的问句也算问了 —— 正文按发送路径同一个读法渲染。

    `material_ref` 段在 `customer_text` 里换成商家原文；商家话术以问句
    收尾是常态（demo 包的开场段就是），读提案原样会漏掉它们。
    """

    prepared = _Prepared()
    _record(
        prepared,
        _sent({"segments": [{"type": "material_ref", "content_or_ref": "material:m:v1"}]}),
        materials={"material:m:v1": "我们做智能客服。您那边谁在接消息？"},
    )

    pending = prepared.aggregate["pending_question"]
    assert pending is not None
    assert "谁在接消息？" in pending["question_text"]


def test_a_named_slot_rides_along_as_an_annotation() -> None:
    """计划点名了槽位就带上 —— 降级为附注，不再是记账的前提。"""

    prepared = _Prepared()
    _record(
        prepared,
        _sent({"type": "text", "text": "他们现在怎么处理客户消息？"}),
        requested=["current_handling"],
    )

    assert prepared.aggregate["pending_question"]["slot"] == "current_handling"


def test_the_question_text_keeps_the_tail_not_the_head() -> None:
    """**截尾 200 字符** —— 与 `LAST_OUTBOUND_BYTES` 同一条理由：问句落在末尾。"""

    prepared = _Prepared()
    long_text = "铺" * 300 + "最后这句才是问题？"
    _record(prepared, _sent({"type": "text", "text": long_text}))

    kept = prepared.aggregate["pending_question"]["question_text"]
    assert len(kept) == 200
    assert kept.endswith("最后这句才是问题？")


def test_a_turn_with_no_outbox_row_records_nothing() -> None:
    """被闸挡下的回合不算问过 —— 否则下一轮会以为问题在客户那里。"""

    prepared = _Prepared()
    refused = ProposalDecision(
        ordinal=0, kind="send_message", accepted=False, reason_code="X", detail=""
    )
    _record_pending_question(
        prepared,  # type: ignore[arg-type]
        plan=_Plan(),  # type: ignore[arg-type]
        delivered=[refused],
        materials=None,
    )

    assert prepared.aggregate["pending_question"] is None


# ---------------------------------------------------------------------------
# settle：null-slot 支
# ---------------------------------------------------------------------------


def _asked(prepared: _Prepared, *, slot: str | None = None) -> None:
    prepared.aggregate["pending_question"] = {
        "question_id": str(uuid4()),
        "frame_id": "frame-1",
        "slot": slot,
        "asked_at": "2026-09-23T08:56:34+00:00",
        "question_text": "您那边谁在盯？",
        "answer_evidence": None,
    }


def test_a_customer_reply_settles_a_slotless_question() -> None:
    """不绑槽位的问题，客户回话就是答 —— 没有槽就没有「答到点上」可核。"""

    prepared = _Prepared()
    _asked(prepared)
    _settle_pending_question(prepared, event_id=WHEN_EVIDENCE)  # type: ignore[arg-type]

    assert prepared.aggregate["pending_question"]["answer_evidence"] == WHEN_EVIDENCE


def test_a_timer_turn_does_not_settle_it() -> None:
    """⚠️ **定时器轮次不算答** —— 静默唤醒不是客户在说话。

    没有这一条，静默唤醒那一轮会用自己的 event_id 把问题结掉：
    「他一直没答」被写成「答了」，**正反两头都错** ——
    防重复规则从此放行重问，而审计里那个问题显示已答。
    """

    prepared = _Prepared(event_type="timer")
    _asked(prepared)
    _settle_pending_question(prepared, event_id=WHEN_EVIDENCE)  # type: ignore[arg-type]

    assert prepared.aggregate["pending_question"]["answer_evidence"] is None


def test_a_slot_bound_question_still_waits_for_the_slot() -> None:
    """**有槽的问题不走 null 支** —— 客户回了话但没给出槽值，问题还挂着。

    「回了话但没答到点上」正是该追问的情形，结掉它会让模型以为问完了。
    """

    prepared = _Prepared()
    _asked(prepared, slot="current_handling")
    # 帧上没有这个槽的值与证据 —— 原有判据（槽位证据 == 本轮事件）不满足。
    prepared.aggregate["target_stack"][-1]["slots"] = {}
    _settle_pending_question(prepared, event_id=WHEN_EVIDENCE)  # type: ignore[arg-type]

    assert prepared.aggregate["pending_question"]["answer_evidence"] is None


# ---------------------------------------------------------------------------
# 提示词：两个 system prompt 都要讲这个键（改前 `pending` 零命中）
# ---------------------------------------------------------------------------


def test_both_prompts_now_explain_the_key() -> None:
    """模型收着一个键，就得有人教过它 —— T200 那一族。

    改动前实测：`pending_question` 进了 plan 的 `user_prompt`，
    而**两个 system prompt 里 `pending` 零命中**。
    生成步单独教是因为缺陷现场是**生成步写的**第 ③ 段 ——
    只教计划步拦不住写字的手。
    """

    assert "pending_question" in _PLAN_SYSTEM
    assert "do not plan a turn whose point is asking it again" in _PLAN_SYSTEM.lower()
    assert "pending_question" in _GENERATE_SYSTEM
    assert "never write it again" in _GENERATE_SYSTEM.lower()


# ---------------------------------------------------------------------------
# 封版：两道守卫
# ---------------------------------------------------------------------------


def _package(strategies: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    return {"strategies": strategies, "target_frames": frames}


def test_a_package_where_no_strategy_declares_anything_is_refused() -> None:
    """全空 → 拒。与缺陷 13 同族：一个无害的空值让一整条机制静默不存在。"""

    document = _package(
        {"wait": {"required_slots": []}, "ask_slot": {"required_slots": []}},
        [{"id": "a", "allowed_strategy_set": ["wait"], "information_slots": []}],
    )

    with pytest.raises(ProfilePackageError, match="no strategy at all"):
        _check_some_strategy_declares_a_required_slot(document, Path("p.json"))


def test_one_declaration_is_enough() -> None:
    """反侧：有一条就过 —— `wait` 这类策略声明必填槽位本来就没有意义。"""

    document = _package(
        {"wait": {"required_slots": []}, "ask_slot": {"required_slots": ["x"]}},
        [
            {
                "id": "a",
                "allowed_strategy_set": ["ask_slot"],
                "information_slots": [{"name": "x"}],
            }
        ],
    )

    _check_some_strategy_declares_a_required_slot(document, Path("p.json"))
    _check_required_slots_exist_in_every_allowed_frame(document, Path("p.json"))


def test_a_frame_that_allows_the_strategy_but_lacks_the_slot_is_refused() -> None:
    """⚠️ **死锁检查**：运行期那道闸读的是**当前帧**的槽位表。

    「A、B 两帧都允许，槽只在 A」能过老检查（它只要求某个帧里存在），
    而在 B 帧里该策略每一轮都被拒、点名那个槽又被 `SLOT_NOT_IN_FRAME` 拒 ——
    **包封得出、对话跑得动，只是那条策略在那个帧永远选不中。**
    """

    document = _package(
        {"ask_slot": {"required_slots": ["x"]}},
        [
            {
                "id": "a",
                "allowed_strategy_set": ["ask_slot"],
                "information_slots": [{"name": "x"}],
            },
            {"id": "b", "allowed_strategy_set": ["ask_slot"], "information_slots": []},
        ],
    )

    with pytest.raises(ProfilePackageError, match="b allows ask_slot but lacks"):
        _check_required_slots_exist_in_every_allowed_frame(document, Path("p.json"))


# ---------------------------------------------------------------------------
# 在架的包真的用上了 —— 否则整套机制零消费方
# ---------------------------------------------------------------------------


def test_the_shipped_packages_now_declare_what_their_goals_ask_for() -> None:
    """demo 的 `ask_slot` 声明 `current_handling`（goal 明写要记它，而 stage
    实测它一直是空的）；third 的 `advance_goal` 声明 `request_summary`。

    ⚠️ **carrier 不在断言里，而那是有意的**：它本来就有两条非空声明，
    本批一个字没动它 —— 把它写进断言等于替它声称一次本批没做过的改动。
    """

    demo = json.loads(
        (REPOSITORY_ROOT / "profiles/internal-demo-sales/profile.json").read_text(
            encoding="utf-8"
        )
    )
    assert demo["strategies"]["ask_slot"]["required_slots"] == ["current_handling"]
    assert demo["identity"]["version"] == "1.4.12"

    third = json.loads(
        (REPOSITORY_ROOT / "profiles/synthetic-third/profile.json").read_text(
            encoding="utf-8"
        )
    )
    assert third["strategies"]["advance_goal"]["required_slots"] == ["request_summary"]
    assert third["identity"]["version"] == "1.0.4"
