"""「就是按这个数走的」—— 而那个数还没被说出来过（运维缺陷 18，2026-09-23）。

## 现场

```
03:13:49  客户  门店数量
03:13:55  AI    报价按门店数量来算。
03:13:55  AI    您这边12家门店，就是按这个数走的。   ← 整通对话没出现过任何价格
03:14:11  客户  刚才那个价格再说一下                 ← 他以为报过了
03:14:17  AI    价格是一个月 1888，…                ← 22 秒后才第一次给出那个数
```

**「这个数」在它发出的那一刻指不到任何东西**，而它有可观察的后果：
客户据此认为价格已经给过，于是**要求重复一句从没说过的话**。

## 四件事，两个层面

| # | 事 | 落在哪 |
|---|---|---|
| 18-1 | 摘要相邻两行互相打架 | 理解步提示词：**规则被它自己的示例拆了** |
| 18-2 | 摘要第 7 行是英文，其余七行是中文 | 理解步提示词：**语言零规则** |
| 18-3 | 配包教模型「清单里没有它就是已讲过」 | 演示包 `quote` 的 goal |
| 18-4 | 素材发出去了而 `price_quoted` 是空的 | 平台：`filled_by_delivery` |

18-3 与 18-4 是一对：去掉「凭印象复述」这条路之后，
**得有一个地方真的存着那个数** —— 那个地方就是平台的投递记录。

## ⚠️ 18-1 与运维缺陷 15 是同一个形状，隔了一批

缺陷 15：配包 goal 明文禁止「我记下了」，而生成步示例逐字写着「收到。」。
那一批的留痕当场写下了判据 —— **范例的说服力强过散文禁令** ——
**然后在同一个文件里留下了理解步的同一处**：v15 新写的
「never say what we did」下面四行，示例是 `"...; we quoted it."`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_every_delivery_filled_slot_is_declared,
)
from fastagent_v2.worker.inference_model import _UNDERSTAND_SYSTEM
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.platform_frames import MATERIAL_TEXTS_KEY
from fastagent_v2.worker.target_stack import (
    DELIVERY_FILLED_SLOT_KEY,
    apply_slot_updates,
    record_delivered_slots,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales"
WHEN = "2026-09-23T04:00:00+00:00"
EVIDENCE = "18181818-1818-4818-8818-181818181818"
PRICE_REF = "material:demo-price-plain:v1"


def _profile() -> dict[str, Any]:
    """整份包，**含素材原文** —— 平台写进槽的就是那段原文。

    ⚠️ 只读 `profile.json` 会得到一份 `content_material_texts` 缺席的文档，
    于是 `materials_for_frame` 返回 `{}`，**本文件每一条都会安静地变成
    「什么都没写」也通过**。`registry._merge_material_texts` 在封版时做的
    就是这一步，这里照它做。
    """

    document = json.loads((PACKAGE / "profile.json").read_text(encoding="utf-8"))
    texts = json.loads((PACKAGE / "materials.json").read_text(encoding="utf-8"))
    document[MATERIAL_TEXTS_KEY] = texts["materials"]
    return document


def _quote_frame(profile: dict[str, Any]) -> dict[str, Any]:
    return next(f for f in profile["target_frames"] if f["id"] == "quote")


def _aggregate_in_quote(profile: dict[str, Any]) -> dict[str, Any]:
    """一个栈顶是 `quote` 的聚合 —— 根构造器造完再把定义换掉。

    手搭的聚合过不了契约，所以起点用 `pipeline._initial_aggregate`。
    """

    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    aggregate["target_stack"][-1]["definition_id"] = "quote"
    return aggregate


# ---------------------------------------------------------------------------
# 18-1 / 18-2：理解步的提示词
# ---------------------------------------------------------------------------


def _example_document() -> dict[str, Any]:
    """理解步提示词里那份示例 JSON，解出来。

    **判据读的是解析之后的那个对象，不是提示词的散文** —— 这一条守的正是
    「示例与规则说了相反的话」，所以它必须能把两者分开看。
    """

    start = _UNDERSTAND_SYSTEM.index("Example JSON output:")
    body = _UNDERSTAND_SYSTEM[_UNDERSTAND_SYSTEM.index("{", start) :]
    depth = 0
    for index, character in enumerate(body):
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return json.loads(body[: index + 1])
    raise AssertionError("示例 JSON 没闭合 —— 提示词的形状变了")


def test_the_example_obeys_the_rule_it_illustrates() -> None:
    """**`turn_summary` 的示例不许示范它自己那条规则禁止的事**（缺陷 18-1）。

    v15 写下的规则是「**never say what we did, what we answered**」，
    而同一段下面四行的示例是：

        "turn_summary": "example: he asked what the price covers; we quoted it."

    `we quoted it` 逐字就是那条规则禁的那一句。stage 2026-09-23 的摘要第 7 行
    是它的翻版：「He asked us to repeat the price **we had mentioned**」——
    而那个价格从没说出来过。

    ## 为什么这一条用词表判是成立的，而缺陷 15 那一批的教训不适用

    本仓栽过三次的那个形状是：拿「某几个字不许出现」去查一段**会引用自己历史的
    文本**，于是命中的是新规则里引的那句话。这里不同 ——
    **查的是解析出来的示例值本身**（`_example_document()`），
    那是一个短句，不是一份留痕，它不会为了解释而复述禁语。
    """

    summary = _example_document()["turn_summary"]

    assert isinstance(summary, str) and summary, "示例里没有 turn_summary"
    lowered = summary.lower()
    for forbidden in ("we ", "we'", "our "):
        assert forbidden not in lowered, (
            f"`turn_summary` 的示例在示范「说我们做了什么」：{summary!r} —— "
            "而同一段 bullet 明令禁止它。范例的说服力强过散文禁令（运维缺陷 15 的判据）"
        )


def test_the_rule_covers_what_he_merely_assumes() -> None:
    """**他以为我们讲过，不等于我们讲过**（缺陷 18-1 的后半）。

    客户那一句是「刚才那个价格再说一下」——一个带假预设的请求。
    模型把那个预设当事实抄进了摘要。规则要点名这一种，
    因为「never say what we did」单看时，**转述他的话像是一条例外**。
    """

    assert "including when he assumes we already did" in _UNDERSTAND_SYSTEM, (
        "规则没点名「他以为我们做过」这一种 —— 而转述客户的假预设"
        "看起来像是在写他那一半，于是绕过了上半句"
    )


def test_the_summary_is_told_which_language_to_write_in() -> None:
    """**摘要的语言先前零规则**（缺陷 18-2）。

    改动前 `grep "语言\\|language"` 在这份提示词与
    `understanding-result.schema.json` 里都是 0 命中。
    stage 实测：八行摘要里第 7 行是英文，其余是中文。
    """

    assert "in the language he wrote in" in _UNDERSTAND_SYSTEM, (
        "没有任何一句告诉模型摘要用哪种语言 —— 今天靠的是"
        "「输入是中文所以输出多半是中文」"
    )


# ---------------------------------------------------------------------------
# 18-4：平台据投递记录填槽
# ---------------------------------------------------------------------------


def test_a_delivered_material_fills_the_slot_that_declares_it() -> None:
    """素材真的发出去了，声明了它的那个槽就被写上 —— 写的是**原文**。

    这一条是本批的兑现物。stage 实测：`material:demo-price-plain:v1`
    在 03:14:17 真的发给了客户，而 `price_quoted` 始终是空的 ——
    **「报过价」这件事在对话里发生了，在状态里没有发生。**
    """

    profile = _profile()
    aggregate = _aggregate_in_quote(profile)
    assert aggregate["target_stack"][-1]["slots"].get("price_quoted") is None

    writeback = record_delivered_slots(
        aggregate=aggregate,
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    assert writeback.applied == 1
    frame = writeback.aggregate["target_stack"][-1]
    assert frame["slots"]["price_quoted"] == profile[MATERIAL_TEXTS_KEY][PRICE_REF]
    assert "1888" in frame["slots"]["price_quoted"], (
        "写进槽的不是那段原文 —— 而槽的用处正是让后面几轮读得到那个数"
    )


def test_the_evidence_says_platform_not_model() -> None:
    """**来源记 `platform`，而那不是修辞。**

    `apply_slot_updates` 的 RFC 4.3 规则按来源与置信度判谁能覆盖谁，
    而这一笔的依据是 `intent_records` 里一行真的离开过 `APPROVED` 的记录 ——
    它不是一次读理解。把它记成 `model` 会让审计分不出
    「模型说报过价」与「平台看见那条素材出去了」。
    """

    profile = _profile()
    writeback = record_delivered_slots(
        aggregate=_aggregate_in_quote(profile),
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    evidence = writeback.aggregate["target_stack"][-1]["slot_evidence"]["price_quoted"]
    assert evidence["source"] == "platform"
    assert evidence["confidence"] == 1.0
    assert evidence["evidence_event_ids"] == [EVIDENCE]
    assert evidence["recorded_at"] == WHEN


def test_nothing_is_written_before_the_material_goes_out() -> None:
    """**反侧：没发出去就不写。**

    没有这一条，一个「只要帧声明了就写」的实现会让上面两条绿 ——
    而那样 `price_quoted` 从帧一开张就是满的，`exit_conditions` 立刻满足，
    **报价这一帧变成一个永远不用报价就能出去的帧**。
    """

    profile = _profile()
    writeback = record_delivered_slots(
        aggregate=_aggregate_in_quote(profile),
        profile=profile,
        delivered=(),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    assert writeback.applied == 0
    assert writeback.aggregate["target_stack"][-1]["slots"].get("price_quoted") is None


def test_a_slot_that_declares_nothing_is_left_alone() -> None:
    """同一个帧的别的槽一个字不动 —— `price_feedback` 是模型的活。"""

    profile = _profile()
    writeback = record_delivered_slots(
        aggregate=_aggregate_in_quote(profile),
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    frame = writeback.aggregate["target_stack"][-1]
    assert set(frame["slots"]) == {"price_quoted"}


def test_the_same_delivery_does_not_pile_up_evidence() -> None:
    """**`delivered` 是累积的，每一轮都会再命中一次。**

    不比值就写，证据链每回合多一条，而 `evidence_event_ids` 的上界是 8 ——
    八轮之后开始丢最旧的那几条，**丢掉的是这个槽真正的来源**。
    """

    profile = _profile()
    first = record_delivered_slots(
        aggregate=_aggregate_in_quote(profile),
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )
    second = record_delivered_slots(
        aggregate=first.aggregate,
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at="2026-09-23T05:00:00+00:00",
        evidence_event_id="18181818-1818-4818-8818-999999999999",
    )

    assert second.applied == 0
    assert (
        second.aggregate["target_stack"][-1]["slot_evidence"]["price_quoted"]
        == first.aggregate["target_stack"][-1]["slot_evidence"]["price_quoted"]
    )


def test_a_human_written_slot_is_refused_not_overwritten() -> None:
    """**人写的赢过平台**，与 `apply_slot_updates` 逐字同一条规则、同一个理由码。

    复用 `SLOT_WRITTEN_BY_HUMAN` 不是省事：审计按理由码检索，
    「模型想改人写的」与「平台想改人写的」分成两个码，检索的人会漏掉一半。
    ⚠️ **同时也意味着这一条不需要迁移** —— `audit_events.reason_code`
    那一侧一个新值都没加。
    """

    profile = _profile()
    aggregate = _aggregate_in_quote(profile)
    frame = aggregate["target_stack"][-1]
    frame["slots"]["price_quoted"] = "运维手填的口径"
    frame["slot_evidence"] = {
        "price_quoted": {
            "confidence": 1.0,
            "evidence_event_ids": [EVIDENCE],
            "recorded_at": WHEN,
            "source": "human",
        }
    }

    writeback = record_delivered_slots(
        aggregate=aggregate,
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    assert writeback.applied == 0
    assert writeback.aggregate["target_stack"][-1]["slots"]["price_quoted"] == "运维手填的口径"
    assert [refusal["reason"] for refusal in writeback.refusals] == [
        "SLOT_WRITTEN_BY_HUMAN"
    ]


def test_a_package_with_no_words_writes_nothing() -> None:
    """**包没供词就什么都不写，而不是把 ref 本身写进槽。**

    `materials_for_frame` 要求帧声明了 ref **且**包供了词。把 ref 写进去会得到
    一个「看起来填上了」的槽：出口条件就此满足，而模型读到的是一个
    它没法复述的字符串。**能存、能过闸、只有结果是错的** —— 本仓最贵的方向。
    """

    profile = _profile()
    profile.pop(MATERIAL_TEXTS_KEY)
    writeback = record_delivered_slots(
        aggregate=_aggregate_in_quote(profile),
        profile=profile,
        delivered=(PRICE_REF,),
        recorded_at=WHEN,
        evidence_event_id=EVIDENCE,
    )

    assert writeback.applied == 0
    assert writeback.aggregate["target_stack"][-1]["slots"] == {}


def test_the_platform_writes_after_the_model_not_before() -> None:
    """⚠️ **「谁赢」这条规则就是调用顺序本身，所以这一条读顺序。**

    同一轮里模型的写回与平台的写回都可能落在同一个槽上，而平台那份该赢。
    做法是让平台跑在后面，**而不是在 `apply_slot_updates` 里再加一个
    「平台写的模型不许改」的分支** —— 那个分支的正确性反正取决于调用顺序，
    于是一条规则又有了两个定义点。

    **源码扫描看不见代码跑没跑**（本仓记法），所以这一条不声称
    「平台的值真的覆盖了模型的」；它声称的是更窄、而正是这次要钉住的那一件：
    **在那段函数体里，平台那一处出现在模型写回之后。**
    """

    source = (
        REPOSITORY_ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
    ).read_text(encoding="utf-8")

    calls = [
        index
        for index, line in enumerate(source.splitlines())
        if "record_delivered_slots(" in line and not line.lstrip().startswith("def ")
    ]
    assert len(calls) == 1, f"平台写回被调用了 {len(calls)} 处"

    writebacks = [
        index
        for index, line in enumerate(source.splitlines())
        if "writeback = apply_slot_updates(" in line
    ]
    advances = [
        index
        for index, line in enumerate(source.splitlines())
        if "advancing_to = frame_to_advance_into(" in line
    ]
    assert writebacks and len(advances) == 1, (
        "顺序判据的两个参照点变了 —— 先核这个文件，再改这条断言"
    )

    # ⚠️ **钉的是区间，不是「更早处有一个写回」。**
    # 第一版写的是 `min(calls) > min(早于它的那些写回)` —— 而这一路上有**两处**
    # `apply_slot_updates`（接管那一路一处、客户那一路一处），于是把平台写回
    # 挪到客户那一处**之前**，接管那一处仍然更早，**那条断言照样绿**。
    # 本仓记过这个形状：数错了的守卫看起来在守。
    assert max(writebacks) < calls[0] < advances[0], (
        "平台写回不在「模型写回之后、平台推进之前」那一段里 —— "
        f"写回 {writebacks}、平台 {calls}、推进 {advances}。"
        "跑在写回之前，这一轮模型的值会盖掉平台的记录；"
        "跑在推进之后，推进读到的还是上一轮的槽"
    )


# ---------------------------------------------------------------------------
# 封版：一个永远填不上的必填槽
# ---------------------------------------------------------------------------


def _one_frame(**slot: Any) -> dict[str, Any]:
    return {
        "target_frames": [
            {
                "id": "quote",
                "content_materials": [{"ref": PRICE_REF}],
                "information_slots": [
                    {"name": "price_quoted", "type": "string", "required": True, **slot}
                ],
            }
        ]
    }


def test_a_ref_this_frame_never_declares_is_refused_at_seal_time() -> None:
    """**ref 写错一个字 = 一个永远填不上的必填槽。**

    那个槽是 `exit_conditions` 的判据，于是这一帧从此出不去 ——
    **包封得出、对话跑得动、没有任何东西变红**，只是那个目标永远完不成。
    """

    document = _one_frame(**{DELIVERY_FILLED_SLOT_KEY: "material:demo-price-plian:v1"})

    with pytest.raises(ProfilePackageError, match="filled by a delivery"):
        _check_every_delivery_filled_slot_is_declared(document, Path("p.json"))


def test_the_right_ref_seals() -> None:
    """**反侧：写对了就放行。** 没有这一条，一条恒拒的检查也会让上面那条绿。"""

    _check_every_delivery_filled_slot_is_declared(
        _one_frame(**{DELIVERY_FILLED_SLOT_KEY: PRICE_REF}), Path("p.json")
    )


def test_a_non_string_slot_is_refused() -> None:
    """写进去的是素材原文 —— 声明在 `boolean` / `money` / `date` 上对不上类型。

    **而没有任何一处在运行期校验槽值的类型**（`apply_slot_updates` 不校验，
    契约里 `slots` 是 `additionalProperties: true`），所以只能封版期判。
    """

    document = _one_frame(**{DELIVERY_FILLED_SLOT_KEY: PRICE_REF})
    document["target_frames"][0]["information_slots"][0]["type"] = "money"

    with pytest.raises(ProfilePackageError, match="filled by a delivery"):
        _check_every_delivery_filled_slot_is_declared(document, Path("p.json"))


def test_the_shipped_package_declares_it_on_the_price_slot() -> None:
    """在架的演示包真的用上了这一格 —— 否则整套机制零消费方。

    ⚠️ **判据是「有机制据它行动」，不是「配包里有这个名字」**：
    上面那几条钉的是 `record_delivered_slots` 真的读它，这一条只补
    「今天真的有一个包声明了它」。
    """

    slot = next(
        s
        for s in _quote_frame(_profile())["information_slots"]
        if s["name"] == "price_quoted"
    )
    assert slot[DELIVERY_FILLED_SLOT_KEY] == PRICE_REF


# ---------------------------------------------------------------------------
# 18-3：配包不再教它凭印象报价
# ---------------------------------------------------------------------------


def test_the_goal_no_longer_says_an_empty_list_means_it_was_said() -> None:
    """**「清单里没有它就是已讲过」是一句假话，而模型照着它做了。**

    这几段素材是 `on_request`，`material_selection.topic_text` 读的是
    「帧里存的话题槽 + 这一轮的 update」——**他这一轮没问到价，清单就是空的**。
    把这种情况读成「已讲过」，再配上「别说报不了价」，
    模型手上没有那个数时唯一合规的说法就是一个指不到东西的「这个数」。

    用户 2026-09-23 裁定：「**手上没那个数就不准报**」。
    """

    goal = _quote_frame(_profile())["goal"]

    assert "清单里没有它就是已讲过" not in goal
    assert "两边都没有就别报价" in goal, "没有「取不到就不报」这条出路"
    assert "`price_quoted`" in goal, "没告诉模型清单空时去哪儿找那个数"


def test_the_slot_and_the_goal_point_at_the_same_material() -> None:
    """**goal 让模型去读的那个槽，正是平台会填的那个。**

    两处分家的后果是安静的：goal 说「看 `price_quoted`」，而平台填的是别的槽，
    于是那一格永远是空的，模型每一轮都读到「没有」并照 18-3 那条路说
    「这一轮取不到」——**功能没坏，只是永远走兜底**。
    """

    frame = _quote_frame(_profile())
    slot = next(s for s in frame["information_slots"] if s["name"] == "price_quoted")

    assert slot[DELIVERY_FILLED_SLOT_KEY] in {
        item["ref"] for item in frame["content_materials"]
    }
    assert slot[DELIVERY_FILLED_SLOT_KEY] in frame["goal"], (
        "goal 点的 ref 与槽声明的 ref 不是同一条"
    )


def test_the_slot_still_carries_across_a_revisit() -> None:
    """⚠️ **`on_revisit: carry` 与本批是一对，别在改这一格时把它弄丢。**

    ADR-0150 让 `quote` 可以反复进（用户：「有些销售场景需要反复报价」），
    而 `price_quoted` 带过去 —— 二次进入不用再报一次价。
    平台填这个槽之后那条仍然成立：带过去的是平台写下的那段原文。
    """

    slot = next(
        s
        for s in _quote_frame(_profile())["information_slots"]
        if s["name"] == "price_quoted"
    )
    assert slot["on_revisit"] == "carry"


def test_the_model_can_still_write_the_slot_itself() -> None:
    """**平台填不等于模型不许填。**

    平台只有在那条素材真的出去之后才知道；而商家哪天把报价改成模型现说
    （不引原文），模型报出的那个数仍然该落进这个槽。
    这一条钉住本批没有顺手把那条路关掉。
    """

    profile = _profile()
    aggregate = _aggregate_in_quote(profile)
    frame_id = aggregate["target_stack"][-1]["id"]

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=[
            {
                "frame_id": frame_id,
                "slot": "price_quoted",
                "value": "按人头 199/月",
                "confidence": 0.9,
                "evidence_event_id": EVIDENCE,
            }
        ],
        recorded_at=WHEN,
    )

    assert writeback.applied == 1
    evidence = writeback.aggregate["target_stack"][-1]["slot_evidence"]["price_quoted"]
    assert evidence["source"] == "model"
