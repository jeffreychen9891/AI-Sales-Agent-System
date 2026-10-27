"""Which materials a turn may quote, and why the rule is data rather than prose.

**The rule this file enforces used to live in a merchant's `goal` text.** That
placed it in the *plan* step's prompt while material selection happens at the
*generate* step -- stated in one prompt, needed in another, enforced nowhere. It
had already drifted the way prose drifts, and both drifts were measured before
this module was written:

* the internal demo's `goal` calls the reactive bucket 「第二桶」 while the
  material document defining the same buckets calls the **timer** bucket
  「第二桶」;
* `demo-opening` is missing from both of the lists `goal` enumerates, and only
  the material document places it.

Nothing could have caught either. These cases are what catches the next one.

Two rules carry the product decision, and the merchant's own document states the
cost of breaking each:

1. **反应式素材不能进主动挑选池** -- 「第三桶混进第一桶，这份 profile 就退回成
   答疑台了」. Volunteering an answer to a doubt nobody raised pushes the
   customer into defending themselves.
2. **定时器素材只在它那两个回合取** -- 「把开场钩子发到跟进回合里就走味了」.

Rule 2 is enforced absolutely. Rule 1 is enforced by a topic gate **with a
deliberate ungated valve**, because the failure direction of a gate is that the
model answers from its own words instead -- see
`test_an_on_request_material_with_no_topics_is_the_ungated_valve`.
"""

from __future__ import annotations

from typing import Any

from fastagent_v2.worker.material_selection import (
    AVAILABILITIES,
    TOPIC_SLOT_KEY,
    current_stage,
    declarations,
    select_materials,
    topic_text,
)

HOOK = "material:hook:v1"
DOUBT = "material:doubt-spend:v1"
VALVE = "material:dont-know:v1"
FOLLOWUP = "material:followup-silence:v1"
DUE = "material:followup-due:v1"

DECLARED = {
    HOOK: "客户下班之后发消息问你，你是不是得爬起来回？",
    DOUBT: "花钱这件事有个上限，到了就停。",
    VALVE: "这个我不确定，就不猜了。",
    FOLLOWUP: "刚才那条就是它自己冒出来的。",
    DUE: "要不要我把刚才说的整理一份给你？",
}


def _definition(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "id": "introduce",
        "information_slots": [{
            "name": "concern_topic",
            "type": "string",
            "required": True,
            "description": "Which kind of loss he cares about this turn.",
        }],
        TOPIC_SLOT_KEY: "concern_topic",
        "time_rules": [
            {"id": "introduce-silence_check", "emit_kind": "silence_check"},
            {"id": "introduce-followup_due", "emit_kind": "followup_due"},
        ],
        "content_materials": [
            {"ref": HOOK, "availability": "proactive"},
            {"ref": DOUBT, "availability": "on_request", "topics": ["花钱"]},
            {"ref": VALVE, "availability": "on_request"},
            {"ref": FOLLOWUP, "availability": "timer", "emit_kinds": ["silence_check"]},
            {"ref": DUE, "availability": "timer", "emit_kinds": ["followup_due"]},
        ],
    }
    definition.update(overrides)
    return definition


def _select(
    *,
    emit_kind: str | None = None,
    slots: dict[str, Any] | None = None,
    updates: list[dict[str, Any]] | None = None,
    definition: dict[str, Any] | None = None,
) -> list[str]:
    return list(
        select_materials(
            definition=definition or _definition(),
            declared=DECLARED,
            emit_kind=emit_kind,
            # ⚠️ **`slots` 不再传给话术那一侧**（用户 2026-09-18 裁定）。
            #
            # `select_materials` 从此不收 `frame` —— 话术的 `on_request`
            # 判据是「**这一轮问了**」，而不是「槽位里还黏着那个值」。
            # 这个 helper 把 `slots` 留着，是为了让下面那条用例还问得出
            # 「黏着的槽位到底还开不开这道门」，**而答案现在是不开**。
            understanding={"slot_updates": updates or []},
        )
    )


# ---------------------------------------------------------------------------
# Rule 2: the timer bucket, pinned
# ---------------------------------------------------------------------------


def test_an_ordinary_turn_is_never_offered_follow_up_wording() -> None:
    """Follow-up wording is written for a silence, not for a live conversation."""

    assert FOLLOWUP not in _select()
    assert DUE not in _select()


def test_a_timer_turn_is_offered_only_its_own_rules_material() -> None:
    """**The same split ADR-0116 made for strategies**, made for materials.

    A timer turn already gets one strategy rather than a menu, because the rule
    that armed it carries the merchant's product decision. The wording is the
    other half of that decision, and leaving it to the model means the 240-second
    silence check can open with a first-contact hook.
    """

    assert _select(emit_kind="silence_check") == [FOLLOWUP]
    assert _select(emit_kind="followup_due") == [DUE]


def test_a_timer_turn_is_offered_no_proactive_or_reactive_material() -> None:
    """The converse, and it is the half the merchant's document is loudest about:
    「把开场钩子发到跟进回合里就走味了」."""

    offered = _select(emit_kind="silence_check")

    assert HOOK not in offered
    assert DOUBT not in offered
    assert VALVE not in offered


def test_a_timer_kind_no_material_claims_offers_nothing_rather_than_everything() -> None:
    """Fail-closed. An unrecognised kind must not fall through to the whole list."""

    assert _select(emit_kind="goal_stall") == []


# ---------------------------------------------------------------------------
# Rule 1: the reactive bucket, gated on the topic the customer raised
# ---------------------------------------------------------------------------


def test_a_reactive_material_is_not_volunteered() -> None:
    """The one mistake the merchant's document says must not happen."""

    offered = _select()

    assert HOOK in offered
    assert DOUBT not in offered


def test_a_sticky_slot_no_longer_opens_the_reactive_gate() -> None:
    """**这条用例是反过来的**，而那是一次裁定（用户 2026-09-18）。

    上一版叫 `test_a_reactive_material_arrives_once_its_topic_is_on_the_slot`，
    断言的正是「槽位现值命中就到场」。

    运维实测那是一条真的毛病：`concern_topic` 在 2026-09-17 06:31 被写成
    「花钱」之后**每一轮三段报价话术都在候选里**，于是客户提一句
    「5000条」这个量，模型手边就有价格，顺手说了 —— 而他从没问过。

    > 用户原话：「我没问价格，AI 自己报了价格；报价表没发，我具体要才给。」

    **今天开这道门的只有下一条用例里那件事：这一轮真的问了。**
    """

    assert DOUBT not in _select(slots={"concern_topic": "已覆盖：自己的麻烦；当前：花钱"})


def test_this_turns_slot_update_counts_and_not_only_the_stored_value() -> None:
    """**The half that is easy to miss, and it is the one that matters.**

    A question asked *in this turn* is not on the frame yet: understanding
    produced the update a moment ago and nothing has written it down. Reading
    only the stored value would gate out the material that answers the question
    the customer just asked -- every time, on the only turn where it matters.
    """

    assert DOUBT in _select(updates=[{"slot": "concern_topic", "value": "花钱"}])


def test_an_update_to_another_slot_does_not_open_the_gate() -> None:
    assert DOUBT not in _select(updates=[{"slot": "usage_intent", "value": "花钱"}])


def test_an_on_request_material_with_no_topics_is_the_ungated_valve() -> None:
    """**Deliberate, and the reason is the failure direction of the gate itself.**

    A mis-filled topic slot hides the material that answers the question, and the
    model then writes its own answer -- the exact harm this mechanism exists to
    prevent. A profile that declares an ungated "I am not sure" material turns
    that failure into saying so rather than inventing. It is available on every
    ordinary turn, including one where no topic has been raised at all.
    """

    assert VALVE in _select()
    # 无 `topics` 的那一段是**兜底**，它与话题判据无关 ——
    # 判据从「槽位现值」改成「这一轮问了」之后它照样每轮到场。
    assert VALVE in _select(slots={"concern_topic": "当前：花钱"})
    assert VALVE in _select(updates=[{"slot": "concern_topic", "value": "花钱"}])


def test_a_frame_with_no_topic_slot_offers_no_topic_gated_material() -> None:
    """Fail-closed, and admission refuses this package -- so this is the second
    lock, describing what the code does if one ever gets past the first."""

    definition = _definition()
    definition.pop(TOPIC_SLOT_KEY)

    assert topic_text(definition, frame={"slots": {"concern_topic": "花钱"}},
                      understanding=None) == ""
    assert DOUBT not in _select(definition=definition,
                                slots={"concern_topic": "花钱"})


# ---------------------------------------------------------------------------
# Shape and compatibility
# ---------------------------------------------------------------------------


def test_a_bare_ref_is_declared_without_restriction() -> None:
    """Every profile written before availability existed uses this shape.

    Reading it as "unrestricted" rather than as "proactive" is the choice that
    makes adopting this a per-package decision instead of a flag day: a package
    that never heard of buckets keeps behaving identically on every kind of turn.
    """

    definition = _definition(content_materials=[HOOK, DOUBT])

    assert _select(definition=definition) == [HOOK, DOUBT]
    assert _select(definition=definition, emit_kind="silence_check") == [HOOK, DOUBT]


def test_declarations_reads_both_spellings_as_one_shape() -> None:
    definition = _definition(content_materials=[HOOK, {"ref": DOUBT}])

    assert [item["ref"] for item in declarations(definition)] == [HOOK, DOUBT]


def test_the_profile_authors_order_survives_selection() -> None:
    """`content_materials` is a list, and the sequence says which to lead with."""

    definition = _definition(
        content_materials=[
            {"ref": VALVE, "availability": "on_request"},
            {"ref": HOOK, "availability": "proactive"},
        ]
    )

    assert _select(definition=definition) == [VALVE, HOOK]


def test_a_declared_ref_the_package_supplies_no_text_for_is_not_offered() -> None:
    """Admission refuses this for a package; a release published before its
    package supplied words is already in the database."""

    definition = _definition(content_materials=[{"ref": "material:absent:v1"}])

    assert (
        select_materials(
            definition=definition,
            declared=DECLARED,
            emit_kind=None,
        )
        == {}
    )


def test_no_category_vocabulary_is_written_into_the_platform() -> None:
    """**A merchant's topics are theirs**, differ by trade, and change more often
    than code. The demo's seven categories exist only in its profile; a list of
    them anywhere in this module would make changing one a code change."""

    import fastagent_v2.worker.material_selection as module

    source = module.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    for category in ("瞎答应", "找真人", "花钱", "查问题", "concern_topic"):
        assert category not in text.split('"""')[2], (
            f"{category!r} appears in the module's code; topics are the "
            "merchant's vocabulary, matched as opaque strings"
        )


def test_the_availability_vocabulary_matches_the_contract() -> None:
    """The schema's enum and this module's branches are one vocabulary.

    Two lists of legal values is how a profile comes to declare an availability
    that admission accepts and selection has never heard of. `_is_offered` fails
    closed on an unknown value, so the symptom would be a material that is
    offered on no turn at all while every gate says the package is fine.
    """

    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (
            _Path(__file__).resolve().parents[2]
            / "packages/contracts/schemas/merchant-profile.schema.json"
        ).read_text(encoding="utf-8")
    )
    frame = schema["properties"]["target_frames"]["items"]["properties"]
    declaration = frame["content_materials"]["items"]["oneOf"][1]

    assert set(declaration["properties"]["availability"]["enum"]) == AVAILABILITIES


def test_an_availability_nobody_defined_is_offered_on_no_turn() -> None:
    """Fail closed rather than fall through to the least restrictive reading."""

    definition = _definition(
        content_materials=[{"ref": HOOK, "availability": "whenever"}]
    )

    assert _select(definition=definition) == []
    assert _select(definition=definition, emit_kind="silence_check") == []


# ---------------------------------------------------------------------------
# 阶段：主动桶的闸（用户 2026-09-11 裁定）
# ---------------------------------------------------------------------------
#
# **实测出来的问题**：301 段那一批里，topic 闸只管**反应式**那 124 段
# （22 组，每组 4–9，中位 6）；**主动桶 164 段今天完全没有闸**。
# 整个给出去约 53 KB，而 `cost.MAX_MODEL_INPUT_BYTES` 是 16 KB——
# **本地直接拒，不是变贵**。一个回合的余量只够约 24 段。
#
# **阶段是推导的，不是问出来的**（用户裁定）：模型填的阶段槽与被否掉的
# 「角色」同源——猜错了选出错素材，而且不会有任何东西变红。平台能自己知道的是
# **它已经发出去了哪几段**，所以阶段声明成「这几段已经出去了」。

STAGE_EARLY = "material:stage-early:v1"
STAGE_PRICED = "material:stage-priced:v1"
STAGE_TRIAL = "material:stage-trial:v1"

STAGE_DECLARED = DECLARED | {
    STAGE_EARLY: "刚接触时说的话。",
    STAGE_PRICED: "报过价之后说的话。",
    STAGE_TRIAL: "试用中说的话。",
}


def _staged_definition() -> dict[str, Any]:
    return _definition(
        material_stages=[
            {"name": "刚接触", "after": []},
            {"name": "报过价", "after": [STAGE_EARLY]},
            {"name": "试用中", "after": [STAGE_EARLY, STAGE_PRICED]},
        ],
        content_materials=[
            {"ref": STAGE_EARLY, "availability": "proactive", "stages": ["刚接触"]},
            {"ref": STAGE_PRICED, "availability": "proactive", "stages": ["报过价"]},
            {"ref": STAGE_TRIAL, "availability": "proactive", "stages": ["试用中"]},
            {"ref": HOOK, "availability": "proactive"},
            {"ref": DOUBT, "availability": "on_request", "topics": ["花钱"]},
        ],
    )


def _staged(delivered: tuple[str, ...]) -> list[str]:
    return list(
        select_materials(
            definition=_staged_definition(),
            declared=STAGE_DECLARED,
            emit_kind=None,
            understanding={"slot_updates": []},
            delivered=delivered,
        )
    )


def test_the_stage_comes_from_what_actually_went_out() -> None:
    """开场时只有开场那段；发过它之后，下一阶段那段才出现。"""

    assert current_stage(_staged_definition(), delivered=()) == "刚接触"
    assert current_stage(_staged_definition(), delivered=(STAGE_EARLY,)) == "报过价"
    assert (
        current_stage(_staged_definition(), delivered=(STAGE_EARLY, STAGE_PRICED))
        == "试用中"
    )


def test_a_later_stages_material_is_not_offered_early() -> None:
    """**这条就是那 164 段的闸。** 没有它，主动桶整个进提示词。"""

    early = _staged(())

    assert STAGE_EARLY in early
    assert STAGE_PRICED not in early
    assert STAGE_TRIAL not in early


def test_the_previous_stages_material_drops_out_once_it_has_been_sent() -> None:
    """阶段是**这一个**阶段，不是「到目前为止的全部」——否则闸等于没关。"""

    priced = _staged((STAGE_EARLY,))

    assert STAGE_PRICED in priced
    assert STAGE_EARLY not in priced


def test_a_proactive_material_is_unrestricted_when_the_frame_has_no_stages() -> None:
    """**用没有声明阶段的帧来表达这条语义**（独立审查 2026-09-11，P2-2）。

    第一版拿一个**声明了阶段的**帧来断言「不声明阶段就是不受限」——
    而 ADR-0125 §四 正是推翻了那个旧默认，admission 今天会拒掉那个包。
    那等于把一个发布不出去的形状钉成了正确行为：将来有人把选取层也改成
    fail closed，这条会以「回归」的面目变红。

    `_is_offered` 那个 `if not stages: return True` 分支真正覆盖的，
    是**整帧都没声明阶段**的情形——那才是它该被钉住的地方。
    """

    definition = _definition(content_materials=[{"ref": HOOK, "availability": "proactive"}])

    assert _select(definition=definition) == [HOOK]
    assert _select(definition=definition, emit_kind=None) == [HOOK]


def test_a_frame_declaring_no_stages_filters_nothing() -> None:
    """没听说过阶段的 profile 行为一字不变。"""

    assert current_stage(_definition(), delivered=()) is None
    assert STAGE_EARLY not in _select()  # 那个帧根本没声明它


def test_the_stage_gate_does_not_touch_the_reactive_bucket() -> None:
    """两个闸各管各的：topic 管反应式，阶段管主动。

    混起来会让「他问了花钱」这件事被阶段挡掉——而客户问什么与谈到哪一步无关。
    """

    definition = _staged_definition()
    offered = list(
        select_materials(
            definition=definition,
            declared=STAGE_DECLARED,
            emit_kind=None,
            # ⚠️ 「他问了花钱」写成**这一轮的 update**（2026-09-18 裁定）。
            #
            # 上一版写的是 `frame={"slots": {"concern_topic": "当前：花钱"}}` ——
            # 一个存着的值。而这条用例自己的话是「**他问了**花钱」：
            # 那是这一轮发生的事。**旧夹具正是把这两件混在一起的那个写法。**
            #
            # 顺带：那个值也从「当前：花钱」换成了词表里的「花钱」——
            # 累积串从今天起过不了词表那一道。
            understanding={"slot_updates": [{"slot": "concern_topic", "value": "花钱"}]},
            delivered=(),
        )
    )

    assert DOUBT in offered, "反应式命中 topic 就该给，与阶段无关"
    assert STAGE_PRICED not in offered, "主动桶仍然按阶段关着"


def test_no_stage_vocabulary_is_written_into_the_platform() -> None:
    """阶段名是商家的，和 topic 一样。代码里出现任何一个就是平台在替商家定义流程。"""

    import fastagent_v2.worker.material_selection as module

    source = module.__file__
    assert source is not None
    body = open(source, encoding="utf-8").read().split('"""')[2]
    for name in ("刚接触", "报过价", "试用中", "试用结束"):
        assert name not in body
