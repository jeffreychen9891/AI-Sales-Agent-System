"""话题槽只收词表里的值 —— C-1 的兑现物（用户 2026-09-15 裁定）。

## 它守的那件事

`on_request` 素材的匹配是**包含式**，对的是话题槽的**整条文本**
（`material_selection._is_offered` → `topic_text`）。所以模型一旦把话题
**累积**写进那个槽（演示包的 goal 原来就是这么要求的：「可累积写成
『已覆盖：X、Y；当前：Z』」），那条串会**同时命中多组** ——
素材集合**随对话单调增长、不回落**（**T351**）。

实测过一次（基线 `37dbde2a`，生产的 `select_materials`）：
七个话题都被填过时一个回合给出 **24 段 / 5,653 字节**，而预算是 **5,630**。

## 修法：写回时丢掉不在词表里的值，而**词表是现推的**

`target_stack._topic_vocabulary` 取本帧 `content_materials[].topics` 的键集 ——
**不另立一张表**。规则与理由写在 `merchant-profile.schema.json` 的
`material_topic_slot` 上，判据一句话：

> **一个话题槽的合法取值 = 有素材对应的话题。**
> 没有素材对应的话题，模型写进去也没有意义 —— 它不会让任何东西被给出去，只占字节。

## 两条断言，各守一半

1. **`test_an_accumulated_value_is_refused`** —— 执行者那一半：累积串被整条丢弃；
2. **`test_the_offered_set_does_not_grow_with_the_conversation`** —— 效果那一半：
   两轮之后素材集合**不含上一轮那一组**。

**两条缺一不可**：只有第 1 条时，丢弃规则可能拦了值而匹配那一侧仍在增长
（比如词表里有一个本身就是拼接的值）；只有第 2 条时，它可能因为别的原因而绿。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.topic_vocabulary import allowed_slot_values
from fastagent_v2.worker.material_selection import (
    TOPIC_SLOT_KEY,
    declarations,
    select_materials,
)
from fastagent_v2.worker.target_stack import apply_slot_updates

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "profiles" / "internal-demo-sales"
#: ⚠️ **2026-09-21 从 `introduce` 改到 `objection`**：拆帧那一批把带 `topics`
#: 的十三段素材搬进了异议帧，而**词表是从素材的 `topics` 派生的** ——
#: 留在主线上它就是一个没有词表的槽。这里跟着那份词表走。
FRAME_ID = "objection"


def _profile() -> dict[str, Any]:
    document = json.loads((PACKAGE / "profile.json").read_text(encoding="utf-8"))
    texts = json.loads((PACKAGE / "materials.json").read_text(encoding="utf-8"))
    document["material_texts"] = texts.get("materials", texts)
    return document


def _definition(profile: dict[str, Any]) -> dict[str, Any]:
    return next(f for f in profile["target_frames"] if f.get("id") == FRAME_ID)


def _aggregate(profile: dict[str, Any], *, slots: dict[str, Any]) -> dict[str, Any]:
    definition = _definition(profile)
    return {
        "target_stack": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": dict(slots),
                "slot_evidence": {},
            }
        ]
    }


def _update(slot: str, value: str) -> dict[str, Any]:
    return {
        "frame_id": "11111111-1111-4111-8111-111111111111",
        "slot": slot,
        "value": value,
        "confidence": 0.9,
        "evidence_event_id": "22222222-2222-4222-8222-222222222222",
    }


def _vocabulary(definition: dict[str, Any]) -> set[str]:
    return {
        str(topic)
        for declaration in declarations(definition)
        for topic in (declaration.get("topics") or ())
    }


def test_the_vocabulary_is_the_declared_topics_not_a_second_table() -> None:
    """词表就是素材声明的 `topics` 键集 —— 而它今天与 goal 逐个对得上。

    **这一条先跑**：下面两条都建立在「词表非空且完整」上，
    而一个空词表会让那条规则**整个不生效**（`_topic_vocabulary` 返回 `None`）——
    那时下面两条会为了错的理由绿。
    """

    definition = _definition(_profile())
    vocabulary = _vocabulary(definition)

    assert definition.get(TOPIC_SLOT_KEY), "这个帧没声明话题槽，下面两条无从谈起"
    assert len(vocabulary) >= 8, f"词表只有 {len(vocabulary)} 个：{sorted(vocabulary)}"
    assert "团队的损耗" in vocabulary, (
        "goal 要求「第一个填「团队的损耗」」，而词表里没有它 —— "
        "那条规则会把每通对话的第一个值丢掉。"
        "按 `material_topic_slot` 那条规则：要么给它写一段素材，要么从 goal 里拿掉"
    )


def test_an_accumulated_value_is_refused() -> None:
    """**执行者那一半**：累积串不在词表里，整条丢弃，槽位保持上一轮的值。"""

    profile = _profile()
    definition = _definition(profile)
    slot = str(definition[TOPIC_SLOT_KEY])
    aggregate = _aggregate(profile, slots={slot: "花钱"})

    written = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[_update(slot, "已覆盖：花钱；当前：查问题")],
        recorded_at="2026-09-15T00:00:00Z",
    )

    assert written.applied == 0, "累积串被写进去了 —— 第五条丢弃规则没生效"
    assert written.aggregate["target_stack"][0]["slots"][slot] == "花钱", (
        "槽位被累积串覆盖了。丢弃的语义是**保持上一轮那个真值**，不是清空"
    )
    reasons = [r["reason"] for r in written.refusals]
    assert reasons == ["SLOT_VALUE_NOT_IN_TOPIC_VOCABULARY"], (
        f"拒绝没有被记下来，或理由码不对：{written.refusals}。"
        "**丢弃必须留痕** —— 否则「模型没填」与「平台丢了」在数据里长得一样"
    )


# ⚠️ **2026-09-21：「花钱」换成「数据安全」** —— 拆帧之后价格那几段进了
# `quote` 帧，「花钱」在那一帧的词表里；本文件跟的是 `objection` 那一份。
@pytest.mark.parametrize("legal", ["数据安全", "查问题", "团队的损耗"])
def test_a_value_in_the_vocabulary_is_written(legal: str) -> None:
    """反面：词表里的值照写不误 —— 那条规则不能把合法的也拦掉。"""

    profile = _profile()
    slot = str(_definition(profile)[TOPIC_SLOT_KEY])
    written = apply_slot_updates(
        aggregate=_aggregate(profile, slots={}),
        profile=profile,
        slot_updates=[_update(slot, legal)],
        recorded_at="2026-09-15T00:00:00Z",
    )

    assert written.applied == 1, f"{legal!r} 在词表里却被丢了：{written.refusals}"
    assert written.aggregate["target_stack"][0]["slots"][slot] == legal


def test_the_offered_set_does_not_grow_with_the_conversation() -> None:
    """**效果那一半**：两轮之后素材集合**不含**上一轮那一组。

    这是 **T351** 那个形状的反面：「闸开合看的是这个话题有没有被填过」
    变成「看这一轮在问它」。

    ## 它必须走真实写回，否则它守不住自己声称的那件事

    **第一版没走**：它直接拿两个干净的值喂 `select_materials`，
    断言第二轮不含第一轮那组。**反向探针当场证伪** ——
    把第五条丢弃规则整条删掉，这一条**照绿**（V1 只红了另一条）。
    因为「值是干净的」正是那条规则保证的，而这一版自己把它当成了前提。

    **现在喂的是模型会写的那种累积串**，并让它**走 `apply_slot_updates`** ——
    于是这一条真正串起了「丢弃规则 → 槽位里是什么 → 给出去哪几段」。
    """

    profile = _profile()
    definition = _definition(profile)
    slot = str(definition[TOPIC_SLOT_KEY])
    declared = {
        str(d["ref"]): profile["material_texts"][str(d["ref"])]
        for d in declarations(definition)
        if str(d["ref"]) in profile["material_texts"]
    }

    def offered_after(updates: list[dict[str, Any]]) -> tuple[set[str], str]:
        """走一遍真实写回，**再按这一轮问的那个话题选素材**。

        ⚠️ **2026-09-18 改了后半截。** 上一版是拿**槽位现值**去选 ——
        那时它只能间接守住本用例声称的那件事（靠 C-1 把槽保持成单值）。
        话术的判据改成「这一轮问了」之后，**那句话是字面成立的**：
        `select_materials` 连 `frame` 都不再收。

        **写回那一半一个字没动** —— 它守的是第五条丢弃规则，
        而那条规则今天照样在：下面两句 `*_value` 断言就是它。
        """

        aggregate = _aggregate(profile, slots={})
        for update in updates:
            aggregate = apply_slot_updates(
                aggregate=aggregate,
                profile=profile,
                slot_updates=[update],
                recorded_at="2026-09-15T00:00:00Z",
            ).aggregate
        value = aggregate["target_stack"][0]["slots"].get(slot, "")
        return set(
            select_materials(
                definition=definition,
                declared=declared,
                emit_kind=None,
                understanding={"slot_updates": updates[-1:]},
                delivered=(),
            )
        ), str(value)

    baseline, _ = offered_after([])
    first, first_value = offered_after([_update(slot, "数据安全")])
    # **模型第二轮会写的那种串** —— 累积，而这正是要被拦下的形状。
    second, second_value = offered_after(
        [_update(slot, "数据安全"), _update(slot, "已覆盖：数据安全；当前：查问题")]
    )

    assert first_value == "数据安全"
    assert second_value == "数据安全", (
        f"累积串被写进去了（槽位现在是 {second_value!r}）—— 第五条丢弃规则没生效"
    )

    only_first = first - baseline
    assert only_first, "「花钱」那一组本身就没素材，这条用例证不了东西"

    # **第二轮那条 update 是累积串**：第五条丢弃规则不让它进槽（上面两句已经钉住），
    # 而从 2026-09-18 起它也**过不了词表那一道** —— 于是这一轮等于没问任何话题。
    #
    # 这比上一版那句 `second == first` 更强：上一版说的是「不会更多」，
    # 靠的是槽里还留着上一轮那个真值；**今天说的是「一段都不给」** ——
    # 「闸开合看这一轮在问它」这句话从此是字面成立的。
    assert second == baseline, (
        f"累积串那一轮给出去的集合是 {sorted(second)}，而一个话题都没问是 {sorted(baseline)} —— "
        "那个串又点亮了话题素材"
    )
    assert len(second) <= len(first), "换一轮之后给出去的段数反而更多了，那不是「按需」"


def test_a_package_with_no_topic_materials_can_still_write_the_slot() -> None:
    """**空词表那一支**：声明了话题槽却一段带 `topics` 的素材都没有时，这条规则不施加。

    复核 2026-09-15（`26ad831c` 开口二）指出 `_topic_vocabulary` 那个
    `return None` **没有守卫**：在架配包的词表非空，所以探针 V2 删掉它照旧全绿。
    它被删掉的后果不是「拦得松了」而是**拦死** ——
    空集会把每一个值都判成非法，那个包的话题槽**永远写不进任何值**，
    而模型每轮照填、每轮被整条丢弃。

    「配包没声明话题槽」与「配包声明了话题槽但还没配素材」是两件事；
    后者是**配到一半**，正确处置是不施加规则，不是把它锁死。
    """

    profile = _profile()
    definition = _definition(profile)
    slot = str(definition[TOPIC_SLOT_KEY])
    # 只摘掉 `topics`，**保留 `material_topic_slot` 与素材本身** ——
    # 要造的是「声明了槽、素材还没标话题」，不是「没声明槽」。
    for declaration in definition["content_materials"]:
        if isinstance(declaration, dict):
            declaration.pop("topics", None)
    assert definition.get(TOPIC_SLOT_KEY), "摘 topics 不该把槽位声明也摘掉"
    assert not _vocabulary(definition), "词表没被摘空，这条用例证不了东西"

    written = apply_slot_updates(
        aggregate=_aggregate(profile, slots={}),
        profile=profile,
        slot_updates=[_update(slot, "词表以外的随便什么值")],
        recorded_at="2026-09-15T00:00:00Z",
    )

    assert written.applied == 1, (
        f"词表为空时这条规则仍然施加了，于是把值丢了：{written.refusals}。"
        "空词表的语义是**不施加**——否则这个包的话题槽永远写不进任何值"
    )
    assert written.aggregate["target_stack"][0]["slots"][slot] == "词表以外的随便什么值"


def test_the_model_is_told_exactly_what_the_code_enforces() -> None:
    """**「裁 B」的兑现物**：告诉模型的那个集合 == `apply_slot_updates` 执行的那个集合。

    理解步的 `slots_wanted` 里，话题槽那一项带 `allowed_values`，
    它与第五条丢弃规则用的词表**出自同一个函数**
    （`runtime.topic_vocabulary.topic_vocabulary`）。

    ## 它替掉了什么

    先前那八个名字在配包里**存在两份**：`content_materials[].topics` 一份，
    goal 的散文里「只填这八类之一：…」又一份。**两份就会漂**，
    而漂了之后没有任何东西会红 —— 独立复核 `26ad831c` 实测：
    往 goal 里加第九类、不给它素材，五份相关用例 96 条一条不红，
    **而模型会照 goal 去填那一类，每一条都被整条丢弃**。

    ## 它**没有**做到的那一半

    「goal 的散文里提到一个没有素材的话题」**今天仍然不会变红**，
    见 `runtime.topic_vocabulary` 的 docstring：要让它变红只有解析散文
    或另立一张声明表，两条都已被否。**这一条改的是后果，不是可检测性** ——
    模型每轮收到的合法集合由素材现推，所以它不会去填那个没素材的类。
    """

    profile = _profile()
    definition = _definition(profile)
    slot = str(definition[TOPIC_SLOT_KEY])

    prompt = request_shape.understand_prompt(
        goal=definition["goal"],
        information_slots=definition["information_slots"],
        allowed_slot_values=allowed_slot_values(definition),
    )
    told = next(
        entry for entry in prompt["slots_wanted"] if entry["name"] == slot
    ).get("allowed_values")

    assert told is not None, (
        "理解步没被告知这个槽的合法取值 —— 那就回到了「模型靠 goal 的散文猜」"
    )
    assert set(told) == _vocabulary(definition), (
        f"告诉模型的集合与代码执行的集合不一样：多说了 "
        f"{sorted(set(told) - _vocabulary(definition))}、"
        f"少说了 {sorted(_vocabulary(definition) - set(told))}。"
        "**多说的那几个模型会填，然后每一条都被第五条规则整条丢弃**"
    )
    assert told == sorted(told), (
        "这张表进 payload，而集合的迭代顺序进程间不稳定 —— 不排序则固定底不可复现"
    )

    # **执行侧**：被告知合法的每一个值，写回时都真的写得进去。
    for value in told:
        written = apply_slot_updates(
            aggregate=_aggregate(profile, slots={}),
            profile=profile,
            slot_updates=[_update(slot, value)],
            recorded_at="2026-09-15T00:00:00Z",
        )
        assert written.applied == 1, (
            f"{value!r} 被告知是合法取值，写回时却被丢了：{written.refusals}"
        )


def test_the_goal_no_longer_carries_a_second_copy_of_the_list() -> None:
    """goal 里不得再出现**整份**词表 —— 那就是被删掉的那第二份。

    **不是写死名字的正则**：断言的是「词表成员不会全部出现在 goal 里」，
    词表变了它跟着变。本仓记过那个反例 ——
    用写死七个名字的正则去迁移里找那七个名字，新增的状态永远进不了那个集合
    （CLAUDE.md 第三节）。

    **允许 goal 点名个别话题**（今天是「团队的损耗」的填写顺序与
    「能省几个人」的归类规则），那是配包策略，不是一份清单。
    **不允许的是把八个名字整份抄回去** —— 那一刻两份又开始各自漂。
    """

    definition = _definition(_profile())
    vocabulary = _vocabulary(definition)
    goal = str(definition["goal"])
    mentioned = {topic for topic in vocabulary if topic in goal}

    assert not vocabulary <= mentioned, (
        f"goal 里又出现了整份词表（{len(mentioned)}/{len(vocabulary)} 个都在）。"
        "那份名单的唯一定义点是 `content_materials[].topics`，"
        "由 `runtime.topic_vocabulary` 渲染进 `slots_wanted.allowed_values`；"
        "goal 里再抄一份，两份就会漂，而漂了不会有任何东西变红"
    )
