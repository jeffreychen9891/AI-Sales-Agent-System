"""A `TimeRule` decides when a scheduled event is due. Not the model, not `now()`.

**B4-2 / ADR-0096 §二.1.** The ruling this file guards is that proactive events
are produced by deterministic rules: RFC §17.2 asks for acceptance scenarios
that are reproducible under a virtual clock, and RFC §6.4 says the consumer may
not trust the classification the scheduler made at creation time -- a re-check
is only possible against something that can be recomputed.

The end-to-end half -- that a delivered message really does leave a row in
`scheduled_events` -- lives in `tests/e2e/test_scheduled_events_get_written.py`.
Everything here is about *what* the rule computes, and every case is one the
producer decides without touching a database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker.time_rules import (
    DEDUPE_PLACEHOLDERS,
    DEFERRED_KINDS,
    SCHEDULABLE_ANCHORS,
    TimeRuleError,
    drafts_for_anchor,
    render_dedupe_key,
    unschedulable_rules,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRAME_ID = UUID("11111111-1111-4111-8111-111111111111")
ANCHOR_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _profile(*rules: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_frames": [
            {"id": "welcome", "goal": "greet", "time_rules": list(rules)},
            {"id": "other", "goal": "elsewhere", "time_rules": [_rule(rule_id="other-rule")]},
        ]
    }


def _rule(
    *,
    rule_id: str = "welcome-silence_check",
    emit_kind: str = "silence_check",
    anchor: str = "last_outbound_at",
    offset: int = 75,
    when: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "id": rule_id,
        "emit_kind": emit_kind,
        "due": {"anchor": anchor, "offset_seconds": offset},
        "dedupe_template": "{emit_kind}:{target_frame_id}",
        "cancel_on": ["customer_message"],
        "strategy": "advance_goal",
    }
    if when is not None:
        rule["when"] = when
    return rule


def _aggregate(
    *,
    frame_status: str = "active",
    entered_at: str = "2026-09-09T11:00:00+00:00",
    last_inbound_at: str | None = None,
    slots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "last_inbound_at": last_inbound_at,
        "target_stack": [
            {
                "id": str(FRAME_ID),
                "definition_id": "welcome",
                "status": frame_status,
                "entered_at": entered_at,
                "slots": slots or {},
            }
        ],
    }


def _drafts(profile: dict[str, Any], aggregate: dict[str, Any], **kwargs: Any) -> list[Any]:
    """**多传一个就抛**，而不是安静地忽略它。

    11d 之前这个 helper `pop` 完三个键就不管剩下的了 —— 于是一条写
    `commitment_id=...` 的用例会得到一个**没带承诺**的调用，
    然后因为「它确实抛了」而变绿。判据对，理由错。
    这正是 `CLAUDE.md` 第三节那个形状，出现在验它的那个工具里。
    """

    anchor = kwargs.pop("anchor", "last_outbound_at")
    anchor_at = kwargs.pop("anchor_at", ANCHOR_AT)
    now = kwargs.pop("now", ANCHOR_AT)
    commitment_id = kwargs.pop("commitment_id", None)
    if kwargs:
        raise AssertionError(f"_drafts 不认识这些参数：{sorted(kwargs)}")
    return drafts_for_anchor(
        profile=profile,
        aggregate=aggregate,
        anchor=anchor,
        anchor_at=anchor_at,
        now=now,
        commitment_id=commitment_id,
    )


def test_the_due_time_is_the_anchor_plus_the_offset_and_nothing_else() -> None:
    """The reproducibility RFC §17.2 asks for, at its narrowest.

    `due_at` is a function of two inputs the caller supplies. `now` is passed in
    separately and is **not** part of this arithmetic -- if it were, replaying
    the same conversation under a virtual clock would produce a different
    timeline every run, which is exactly what the ruling rejected the model's
    `due_at` for.
    """

    drafts = _drafts(_profile(_rule()), _aggregate(), now=ANCHOR_AT + timedelta(hours=3))

    assert len(drafts) == 1
    assert drafts[0].due_at == ANCHOR_AT + timedelta(seconds=75)
    assert drafts[0].kind == "silence_check"
    assert drafts[0].rule_id == "welcome-silence_check"


def test_the_dedupe_key_is_rendered_from_the_allowlist() -> None:
    assert render_dedupe_key(
        "{emit_kind}:{target_frame_id}", emit_kind="silence_check", target_frame_id=FRAME_ID
    ) == f"silence_check:{FRAME_ID}"


def test_a_template_naming_anything_else_is_refused_by_name() -> None:
    """The allowlist RFC §6.2 refers to, landing here (ADR-0105 §零 D3).

    A dedupe key is not a place to be inventive: the unique constraint is
    `(tenant_id, conversation_id, aggregate_generation, dedupe_key)`, so two
    rules whose templates render alike do not collide loudly -- **the second row
    is absorbed**. Refusing an unknown placeholder is how that stays impossible
    rather than unlikely, and the error names the offender so the profile author
    is not left guessing.
    """

    with pytest.raises(TimeRuleError) as refusal:
        render_dedupe_key(
            "{emit_kind}:{conversation_id}", emit_kind="silence_check", target_frame_id=FRAME_ID
        )

    assert "conversation_id" in str(refusal.value)
    # **三个，不是两个** —— 11d 加了 `commitment_id`（用户 2026-09-16 裁定）。
    # 等值断言而不是包含：词表变宽是一次要有人点头的改动，
    # 而「多一个不报错」正是这条用例存在的形状。
    assert sorted(DEDUPE_PLACEHOLDERS) == ["commitment_id", "emit_kind", "target_frame_id"]


def test_a_template_naming_a_commitment_with_none_supplied_is_refused() -> None:
    """**空段不是一个键，是同一个键。**

    `{commitment_id}` 渲染成空串会得到 `customer_promise_due:<frame>:` ——
    看起来对，而同一帧里每一条承诺都渲染成它，
    于是这个占位符要解决的那次碰撞原样回来，只是走的是失败路径。
    """

    with pytest.raises(TimeRuleError) as refusal:
        render_dedupe_key(
            "{emit_kind}:{target_frame_id}:{commitment_id}",
            emit_kind="customer_promise_due",
            target_frame_id=FRAME_ID,
        )

    # **断言的是它抛的哪一句，不只是「它抛了」** —— 反向探针实测：
    # 把 `commitment_id` 从词表里删掉，这条用例照绿，
    # 因为那时它走的是「未知占位符」那一支，报错里同样有 commitment_id 这个词。
    # **一条「只要抛就算过」的用例，在功能被整个删掉时最绿。**
    assert "no commitment was supplied" in str(refusal.value), (
        f"抛了，而抛的不是「没给承诺」那一句：{refusal.value}"
    )


def test_two_commitments_in_one_frame_render_two_different_keys() -> None:
    """**这一条就是 11d 存在的理由，正面写一遍。**

    两条承诺、同一帧、同一个 `emit_kind`。11d 之前它们渲染出同一个键，
    而写入方是 `ON CONFLICT ... DO UPDATE SET due_at = GREATEST(...)`：
    第二条不建新行，**它把第一条的到期时间往后推**。
    早的那个死线从此不会响，而没有任何东西会红。
    """

    template = "{emit_kind}:{target_frame_id}:{commitment_id}"
    first = render_dedupe_key(
        template,
        emit_kind="customer_promise_due",
        target_frame_id=FRAME_ID,
        commitment_id=UUID("11111111-1111-4111-8111-111111111111"),
    )
    second = render_dedupe_key(
        template,
        emit_kind="customer_promise_due",
        target_frame_id=FRAME_ID,
        commitment_id=UUID("22222222-2222-4222-8222-222222222222"),
    )

    assert first != second
    # 而**帧仍然在键里**：换一帧同一条承诺也不该撞。
    assert first.startswith("customer_promise_due:" + str(FRAME_ID))


def test_only_the_rules_of_the_anchor_being_established_are_created() -> None:
    """Each rule is materialised where its own anchor becomes true.

    A transition that has just written `entered_at` must not also create the
    `last_outbound_at` rules: their anchor at that moment is the **previous**
    turn's outbound, which for the shipped 75-second rule means a timer overdue
    at birth. This is the batch's one design decision, asserted rather than
    described.
    """

    profile = _profile(
        _rule(rule_id="on-outbound", anchor="last_outbound_at"),
        _rule(rule_id="on-entry", emit_kind="goal_stall", anchor="frame_entered_at", offset=600),
    )

    outbound = _drafts(profile, _aggregate(), anchor="last_outbound_at")
    entry = _drafts(profile, _aggregate(), anchor="frame_entered_at")

    assert [draft.rule_id for draft in outbound] == ["on-outbound"]
    assert [draft.rule_id for draft in entry] == ["on-entry"]


def test_an_anchor_this_milestone_cannot_resolve_is_refused_at_the_call() -> None:
    """`business_event_at` still has no value to anchor to. **One left, not two.**

    Refused as a call, not skipped as a rule: a caller asking for an anchor
    nothing can supply has made a mistake, while a *profile* declaring one is
    merely early (that case is the skip, covered below).

    **`commitment_due_at` left this list in 11d**, once migration 063 gave it a
    ledger to read. The set assertion is equality on purpose: an anchor becoming
    schedulable is a batch of work, and a fourth one appearing quietly is the
    thing this catches.
    """

    with pytest.raises(TimeRuleError):
        _drafts(_profile(_rule()), _aggregate(), anchor="business_event_at")
    assert SCHEDULABLE_ANCHORS == {
        "frame_entered_at",
        "last_outbound_at",
        "commitment_due_at",
    }


def test_a_commitment_anchor_without_a_commitment_is_refused_at_the_call() -> None:
    """**两个方向都是调用方的错**，而第二个方向是真正要钉的那个。

    少给：`commitment_due_at` 是每条承诺一个时刻，不给承诺就没有时刻。
    多给：给一个会话级锚点塞一条承诺，会写出一行 `silence_check`
    指着一条与它无关的承诺 —— **而 064 的外键会愉快地接受它**，
    因为那条承诺真的存在。一行真的引用、错的含义。
    """

    # 同样断言到句子：反向探针实测，把 `commitment_due_at` 从可调度锚点里
    # 拿掉之后，下面第一条照样抛（抛的是「这个里程碑调度不了它」），
    # 于是这条用例在**功能被退回去**的那次变异里是绿的。
    with pytest.raises(TimeRuleError) as missing:
        _drafts(_profile(_rule()), _aggregate(), anchor="commitment_due_at")
    assert "per-commitment" in str(missing.value), f"抛的不是「少给」那一句：{missing.value}"

    with pytest.raises(TimeRuleError) as extra:
        _drafts(
            _profile(_rule()),
            _aggregate(),
            anchor="frame_entered_at",
            commitment_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
    assert "conversation-wide" in str(extra.value), f"抛的不是「多给」那一句：{extra.value}"


def test_rules_of_a_frame_that_is_not_active_do_not_fire() -> None:
    """A suspended frame keeps its `entered_at`, and it is not the current goal.

    Without this, an interrupted frame would still produce plausible-looking
    timers -- the value is there, it is just no longer about what the
    conversation is doing.
    """

    assert _drafts(_profile(_rule()), _aggregate(frame_status="interrupted")) == []


@pytest.mark.parametrize(
    ("when", "aggregate_kwargs", "expected"),
    [
        ({"target_active_for_seconds": 600}, {}, 1),
        ({"target_active_for_seconds": 7200}, {}, 0),
        ({"no_customer_reply_for_seconds": 60}, {}, 1),
        (
            {"no_customer_reply_for_seconds": 3600},
            {"last_inbound_at": "2026-09-09T11:59:00+00:00"},
            0,
        ),
        ({"pending_slot": "budget"}, {}, 1),
        ({"pending_slot": "budget"}, {"slots": {"budget": "10k"}}, 0),
        ({"business_event_type": "order_paid"}, {}, 0),
    ],
    ids=[
        "active-long-enough",
        "not-active-long-enough",
        "never-replied",
        "replied-recently",
        "slot-still-pending",
        "slot-already-filled",
        "business-event-gate-has-no-input",
    ],
)
def test_the_when_clause_decides_whether_the_rule_is_created(
    when: dict[str, Any], aggregate_kwargs: dict[str, Any], expected: int
) -> None:
    """Four selectors, and the last one refuses rather than admits.

    `business_event_type` has no input on this path, and a rule gated on it must
    not fire on every ordinary turn merely because nothing contradicted it --
    "no evidence against" is the shape that turns a narrow rule into a
    broadcast.
    """

    drafts = _drafts(_profile(_rule(when=when)), _aggregate(**aggregate_kwargs))

    assert len(drafts) == expected


def test_no_shipped_rule_is_one_this_milestone_can_never_create() -> None:
    """**空列表，全部配包 —— 而这条断言在 11d 之前是不成立的。**

    在此之前 `synthetic-carrier` 的两条 `commitment_due_at` 规则
    （`rental-customer_promise_due` / `rental-ai_commitment_due`）确实进不了
    调度：ADR-0100 把这两个 kind 移出 M1b，那时的判据因此写成
    「跳过的必须都是 ADR-0100 延后的那两个 kind」，`DEFERRED_KINDS` 就是那个豁免口。

    063 建了台账、11d 接了锚点，**那个豁免口空了**，于是判据回到最直的那一句：
    在架配包里不许有任何一条永远建不出来的规则。

    ## 这条用例现在覆盖两种「永远建不出来」

    1. **锚点解析不了** —— `business_event_at` 今天仍然没有生产方；
    2. **模板认不出两条**（11d 新增）—— `commitment_due_at` 的规则若不带
       `{commitment_id}`，锚点解析得好好的，而它渲染出的键在同一帧里
       对每条承诺都一样，于是生产方跳过它。

    第二种是**放宽 schema 换来的**：pattern 允许老模板（不能收紧，
    那是对已存 release 的追溯收紧），所以「必须带第三段」这条要求
    落在这里和封版那一侧，不落在 pattern 上。

    ## 一处更正

    这条用例的上一版 docstring 说那两条规则在 `synthetic-third` ——
    **不是**，它们在 `synthetic-carrier`（现读：`synthetic-third` 的两条规则
    都锚在 `last_outbound_at`）。名字错了三个月没人发现，
    因为断言本身不读配包名。
    """

    for path in sorted((REPOSITORY_ROOT / "profiles").glob("*/profile.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        surprises = unschedulable_rules(profile)
        assert surprises == [], (
            f"{path.name} declares rules that can never be created: {surprises}. "
            "Either the anchor has no producer, or a per-commitment rule's "
            "dedupe_template cannot tell two commitments apart."
        )


def test_the_deferral_list_is_empty_and_that_is_the_assertion() -> None:
    """**`DEFERRED_KINDS` 空了，而空不等于没用。**

    它曾经装着 `customer_promise_due` / `ai_commitment_due`，意思是
    「这两类被跳过是预期的」。11d 之后被跳过不再是预期的，是缺陷。

    保留这个空集合而不是删掉它，是因为读它的那些地方**问的是一个问题**：
    「这条被跳过，是意料之中的吗」。删掉名字会让每一处各自悄悄不再问；
    留一个空集合让它们统一答「这里没有任何一类是预期被跳过的」。

    **而空集合天然是一条空守卫**（`CLAUDE.md` 第三节），
    所以空这件事本身要有人钉：往里塞回一个 kind 会红在这里，
    需要一句解释，而不是一次静默的放行。
    """

    assert DEFERRED_KINDS == frozenset(), (
        f"有人往 DEFERRED_KINDS 里放了 {sorted(DEFERRED_KINDS)}。"
        " 放回去意味着「这一类被跳过是预期的」—— 那是一次要写下理由的裁决，"
        " 而不是一次顺手的豁免。"
    )
