"""一段话术，一通对话里只逐字发一次 —— **而文件那一侧照旧。**

## 现场（运维 §八，2026-09-18 16:35–16:41 逐字）

一小时里同一段被引用的次数（`intent_records` 现读）：

| 素材 | 次数 |
|---|---|
| `material:demo-price-plain:v1`（「价格是一个月 1888，前十天免费试用……」）| **3** |
| `material:demo-price-flat:v1` | 2 |

三轮连着把同一句报价逐字说了三遍（16:37:50 / 16:38:41 / 16:39:19）。
**按设计放行的** —— `select_materials` 当时对 `on_request` 不滤。

运维同节写下的判据逐字：

> 那句话对**文件**成立（再要一次报价表，再发一份是对的）；
> **对一段话术不成立** —— 同一句话逐字说三遍，客户读到的是复读。

## 而这个仓里早就有两段注释在对同一件事唱反调

* `select_materials` 那一行的注释：「`on_request` 不滤：他又问一次价格，
  就该又拿到那句报价。」
* 同一份文件里 `topic_asked_this_turn` 的 docstring（2026-09-18 写的）：
  「**话术不是这样：同一句话逐字说三遍，客户读到的是复读。**」

**两个定义点都是散文，而跑的是前一个。** 本批把它们对齐到后一个。

## 用户 2026-09-20 裁定的两半

> **「第一次用商家给的固定话术，之后简单报价就行。」**

前半是这一批拦的东西；**后半是它为什么拦得起** ——
帧目标同批改成「清单里没有它就是已讲过，改用自己的话说那个数」。
⚠️ **只做前半会把运维 §5.1 再造出来**（问价没拿到价格）：
goal 还写着「引原文」，而那个 ref 已经不在候选里，模型照做就提名一个
未提供的 ref，整条被 `GENERATION_MATERIAL_REF_UNKNOWN` 拒掉。
"""

from __future__ import annotations

import json
import pathlib
import re

from fastagent_v2.worker.material_selection import select_assets, select_materials

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
VALIDATION = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_validation.py"
)

REF = "material:demo-price-plain:v1"
ASSET_REF = "asset:pricing-sheet:v1"


def _frame() -> dict:
    # ⚠️ **2026-09-21 不再是 `[0]`**：拆帧之后 `[0]` 是主线，而本文件钉的是
    # **问价那一族**（第一次问价拿不拿得到原文、报价表要一次发一次）——
    # 那几条话都搬进了 `quote` 帧，跟着它走。
    document = json.loads(PROFILE.read_text(encoding="utf-8"))
    return next(f for f in document["target_frames"] if f["id"] == "quote")


def _asked_about(topic: str) -> dict:
    """客户**这一轮**问了这个话题 —— 写成 update，不是槽位里黏着的值。"""

    return {"slot_updates": [{"slot": "concern_topic", "value": topic}]}


def test_the_material_is_offered_the_first_time() -> None:
    """**对照，而它是这一组里最要紧的一条。**

    没有它，一个「一律不给话术」的实现会让下面那条绿 ——
    而那正是运维 §5.1 那个缺陷（问价没拿到价格）。
    """

    offered = select_materials(
        definition=_frame(),
        declared={REF: "价格是一个月 1888，前十天免费试用。"},
        emit_kind=None,
        understanding=_asked_about("花钱"),
        already_sent=(),
    )
    assert REF in offered, "第一次问价就拿不到那段原文 —— 运维 §5.1 那个缺陷回来了"


def test_the_same_material_is_not_offered_a_second_time() -> None:
    """**本批的理由。** 同一段话术不再第二次进候选。"""

    offered = select_materials(
        definition=_frame(),
        declared={REF: "价格是一个月 1888，前十天免费试用。"},
        emit_kind=None,
        # 「他又问了一次」—— 与上一条唯一的差别是 `already_sent`
        understanding=_asked_about("花钱"),
        already_sent=(REF,),
    )
    assert REF not in offered, (
        # 1) 看到了什么
        "同一段话术又进候选了。"
        # 2) 为什么这条要红
        "\n运维 §八 实测：一小时里同一句报价被逐字引了 **3 次**。"
        "**客户读到的是复读。**"
        # 3) 到期了该做什么
        "\n要放开它，先读用户 2026-09-20 那条裁定的后半："
        "「之后简单报价就行」—— 放开就等于说那半句不作数了。"
    )


def test_the_file_side_is_deliberately_not_narrowed() -> None:
    """⚠️ **资料那一侧一个字没动，而这是裁定的一部分，不是漏了。**

    运维原话逐字：「再要一次报价表，再发一份**是对的**。」
    一份 xlsx 发两次，客户拿到的是他要的东西；
    同一句话说两次，客户拿到的是复读。**两者不是一件事。**
    """

    offered = select_assets(
        definition=_frame(),
        frame={"slots": {}},
        understanding=_asked_about("花钱"),
        delivered=(),
        emit_kind=None,
        already_sent=(ASSET_REF,),
    )
    assert ASSET_REF in {entry["ref"] for entry in offered} or ASSET_REF in offered, (
        "资料那一侧也被收掉了 —— 他又要一次报价表就该再发一份，"
        "而这条用例守的正是「本批只动话术那一侧」"
    )


def test_the_goal_tells_the_model_what_to_do_when_the_line_is_gone() -> None:
    """**代码把那一段拿走了，goal 就得告诉模型这时候做什么** —— 而第一版答错了。

    ## 这一条本来断言的两句话，2026-09-23 被运维缺陷 18 证明是错的

    原来的两句是「**清单里没有它就是已讲过**」与「**别说报不了价**」。
    它们合起来给模型留下的唯一出路，是**凭印象复述一个数**。

    而「清单里没有它」有第二个成因，**而且是常见的那个**：
    这几段素材都是 `on_request`，`material_selection.topic_text` 读的是
    「帧里存的话题槽 + 这一轮的 update」—— **他这一轮没问到价，清单就是空的**。
    此时「就是已讲过」是一句假话，而「别说报不了价」堵死了说实话那条路。

    ## stage 实测的后果（运维缺陷 18，2026-09-23）

    ```
    03:13:55  AI  报价按门店数量来算。
    03:13:55  AI  您这边12家门店，就是按这个数走的。   ← 整通对话还没出现过任何价格
    03:14:11  客户 刚才那个价格再说一下                ← 他以为报过了
    03:14:17  AI  价格是一个月 1888，…                ← 22 秒后才第一次给出那个数
    ```

    **「这个数」在它发出的那一刻指不到任何东西**，而客户据此认为价格已经给过 ——
    下一句是「**要求重复一句从没说过的话**」。

    ## 今天的三条出路，第三条是新的

    1. 清单里有原文 → 引；
    2. 清单里没有，而 `price_quoted` 有值 → 照那个数说
       （**那个槽现在由平台的投递记录填**，`filled_by_delivery`）；
    3. **两边都没有 → 不报价**，说这一轮取不到、下一句补上。

    用户 2026-09-23 裁定：「**手上没那个数就不准报**」。

    ⚠️ **第三条不会造成 `GENERATION_MATERIAL_REF_UNKNOWN`** —— 那正是这条用例
    原来担心的失败形态。它担心的是「goal 说引原文而 ref 不在候选里」，
    而第三条根本不提 ref。
    """

    goal = _frame()["goal"]

    assert "`price_quoted`" in goal, (
        "goal 没告诉模型「清单里没有原文时去哪儿找那个数」—— "
        "而那个槽今天由平台的投递记录填，是唯一一个真实来源"
    )
    assert "两边都没有就别报价" in goal, (
        "goal 没给出「取不到就不报」这条路 —— 少了它，模型手上没有那个数时"
        "唯一合规的说法就是一个指不到任何东西的「这个数」（运维缺陷 18）"
    )
    # **查的是那条禁令的原话，而它不会在 goal 里以别的身份出现** ——
    # goal 是给模型读的商家话术，不是一份会引用自己历史的文档。
    # 同一天在三处犯过反过来的那个错（用一句「某几个字不许出现」去查，
    # 而命中的是新规则里引的那句话），所以这里点明为什么这一处是安全的。
    assert "别说报不了价" not in goal, (
        "那条堵死诚实出路的禁令还在 —— 它与上面那条「取不到就不报」互斥"
    )


def test_plainly_saying_the_price_is_not_a_claim_about_the_customer() -> None:
    """**「之后简单报价」拿得出口，而这一条钉住它拿得出口。**

    数字闸（`_CUSTOMER_PAYMENT_CLAIM`）抓的是**替客户认下一笔支出**
    （「您付的那 1888」），不是「一个月 1888」这种平铺直叙的标价。
    ⚠️ **本批依赖这个边界** —— 有人哪天把它收紧成「任何没出现过的数都丢」，
    用户裁定的后半（之后简单报价就行）当场作废，而**没有别的东西会说话**。
    """

    source = VALIDATION.read_text(encoding="utf-8")
    pattern = re.search(r"_CUSTOMER_PAYMENT_CLAIM = re\.compile\(\n(.*?)\n\)", source, re.S)
    assert pattern, "那条数字闸的形状找不到了"
    expression = pattern.group(1)

    assert "[您你]" in expression, (
        "数字闸不再要求那句话是**说给客户的**了 —— 它现在会抓任何数字，"
        "包括「一个月 1888」这种标价。**用户 2026-09-20 裁定的后半依赖它不抓。**"
    )
    assert re.search(r"付\|交\|花\|掏", expression) or "付" in expression, (
        "数字闸不再要求那是一个**支付**动作了 —— 同上"
    )
