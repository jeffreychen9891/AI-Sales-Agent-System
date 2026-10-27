"""运维 2026-09-20 复测那通七回合对话里的四条，本批在配包这一侧修掉的部分。

## 现场（stage，会话 `6e756704-…`，逐字）

| 时刻 | 谁 | 说了什么 |
|---|---|---|
| 12:56:53 | AI | ……**您那边现在客户消息主要是谁在盯？** |
| 13:07:14 | 客户 | 我们肯定是销售在盯啊 |
| 13:07:19 | AI | ……**您团队现在客户消息具体是怎么分的？** |
| 13:08:44 | 客户 | 没人管 |
| 13:08:50 | AI | 您说没人管——**这话我接住了**。…… |
| 13:11:21 | AI | ……**您团队现在这块是怎么处理的？** |
| 13:12:42 | 客户 | **就是没处理啊**，销售既跟用户又谈单 |

第三次那个「**啊**」是被同一件事问第三遍的反应。

## 这一批改的是配包，而那是量出来的，不是选出来的

开工前逐条量过，三个结果都写在这里，因为**它们否掉的方案比留下的方案更值钱**：

1. **把重复闸加宽到看全历史 —— 一条也抓不到。** 五条出站两两比
   `_longest_shared_run`，最大 **13 字**，而阈值是 20。
   模型不是逐字复读，是**换着说法复读**；
2. **换成字符 n-gram 相似度 —— 正负例倒挂。** n=2/3/4 都试过，
   正例最低 0.0%–7.7%，**而反例最高 45.5%–51.4%** ——
   那个反例是仓里自己的「合理回指」夹具
   （`test_a_natural_callback_to_our_last_message_survives`）。
   **合理回指本来就是故意大段重合**，所以没有哪个阈值分得开；
3. **「复述了多少」也判不出回执。** 按 AI↔客户这个轴量：
   被点名的那条回执 33.3%，而**正常的那条 40.0%** —— 比它还高。

**所以第 1 / 3 / 6 条的可执行杠杆都不在「判这句话像不像」上。**
它们的共同根因是**模型在自己写字**：包里那两段话本来就有
（`material:demo-open-what-we-do:v1` / `material:demo-bridge-to-capability:v1`，
都是 `availability: proactive`、挂在 `01-开场` / `02-摸底` 两个阶段上），
`advance_goal` 的 `allowed_intent_kinds` **也包含** `send_material`,
而那通对话里 `send_material` **一次没提**。

⚠️ **`must_quote_material` 本批不加。** 它是已经在执行的码，而八个打法一个都没声明它 ——
但它引不到就是**整份拒**（`MATERIAL_QUOTE_REQUIRED`），客户这一轮收到零字。
**那比一条啰嗦的回复重得多**，与长度那条同一个不对称。
本批改的是「告诉它引哪一段」，不是「不引就拒」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"

#: `goal` 里点名的那两段 —— **它们必须真的在包里**。
#: `goal` 曾经指着 `materials/03` 与 `materials.md` 两个不存在的东西，
#: 那两处今天由 `test_the_opening_line_introduces_the_product` 的 not-in 守着。
NAMED_MATERIALS = (
    "material:demo-open-what-we-do:v1",
    "material:demo-bridge-to-capability:v1",
)

#: 本批新加的两个槽。**都是「别问第二遍」的记录，不是出帧条件。**
NEW_SLOTS = ("current_handling", "raised_concern")

#: 禁的那一件的反例，**逐字取自那通对话**。
#: 写成常量是因为它是**实测原话**，编一句像样的复述会随判据一起漂。
#:
#: ⚠️ **这条规则被用户当场收窄过两次，两次都记在这里**：
#: 第一版我写成「不要铺垫、不要复述、不要安慰」三条，
#: 用户 2026-09-20 更正：「**复述在一对一聊天是没必要的；铺垫、共情、评论
#: 在销售中是可以有的**」，随后又确认「**就是重复对方的话，完全没必要**」。
#: **所以禁的只有复述这一件**，另外三样明写允许 ——
#: 一条比裁定更宽的规则，下一个人读不出它宽在哪里。
THE_RESTATEMENT_MEASURED = "您说没人管——这话我接住了"

#: 看懂了不用说出来。用户 2026-09-20：「**还有什么我收到了、我知道了 ——
#: 看明白知道对方什么意思不用说出来**」。
#: **2026-09-24（缺陷 25）身份翻面**：先前这五个逐个写在 goal 的禁令里，
#: 复测实测点名的例句会被模型当措辞样本借走 —— 现在它们是 goal 的**禁入表**。
THE_ACKNOWLEDGEMENTS = ("我记下了", "我听到了", "我收到了", "我知道了", "我来安排")

#: **明写允许的三样。** 它们在 `goal` 里必须出现，否则下一个读到
#: 「不要重复对方的话」的人会顺手把铺垫和共情一起禁掉。
THE_ALLOWED = ("铺垫", "共情", "点评")


def _profile() -> dict:
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def _introduce() -> dict:
    return next(f for f in _profile()["target_frames"] if f["id"] == "introduce")


def test_the_goal_names_which_material_to_open_with() -> None:
    """**第一句要引哪一段，`goal` 得说出名字来。**

    在此之前 `goal` 只说「第一句就要说清楚我们是做什么的」——
    **没说引哪一段**，于是模型自己编了一段，并在后面两轮换着说法又讲了两遍。

    ⚠️ 这与「问价先引 `material:demo-price-plain:v1`」**逐字同形**：
    那一条点了名，所以它是可照做的；这一条先前没点名。
    """

    goal = _introduce()["goal"]
    for ref in NAMED_MATERIALS:
        assert ref in goal, f"`goal` 没点名 {ref} —— 模型只能自己编一段"


def test_the_named_materials_are_really_in_the_package() -> None:
    """**点名一个不存在的 ref，比不点名更糟** —— 模型会照着编一个。

    这一条是上面那条的另一半：`goal` 里出现的每个素材号都要真的在
    `content_materials` 里。本仓有过先例（`materials/03` / `materials.md`）。
    """

    frame = _introduce()
    declared = {m["ref"] for m in frame["content_materials"]}

    # ⚠️ **从 `goal` 里现读，不读上面那张常量表。**
    # 第一版读的是 `NAMED_MATERIALS`，而那等于拿常量去核常量 ——
    # 探针把 `goal` 里的 ref 换成一个不存在的号，这一条**纹丝不动**。
    # 真正要守的是「**`goal` 里写着的**每个素材号都存在」。
    named_in_goal = set(re.findall(r"material:[a-z0-9-]+:v\d+", frame["goal"]))
    assert named_in_goal, "`goal` 里一个素材号都没点名"
    for ref in sorted(named_in_goal):
        assert ref in declared, f"{ref} 在 goal 里点了名，而 content_materials 里没有它"


def test_the_two_named_materials_are_pushable_at_those_moments() -> None:
    """**点了名还得推得出去。**

    两段都必须是**客户没开口也能发**的那一档 —— 若它们是 `on_request`，
    开场那一轮**拿不到**，`goal` 那句话就是一条做不到的指令。

    ⚠️ 运维 2026-09-18 那一轮正是这个形状的反面：承诺「先讲开通」，
    而那几段当时是 `proactive` + 阶段门推着走的，**客户主动问的时候拿不到**。
    两个方向的同一个错。

    ## 判据按**性质**写，不按拼法写（2026-09-22，ADR-0149）

    原文断言的是 `== "proactive"`。**那一天它是对的，因为当时只有一个拼法
    满足「客户没开口也能发」** —— 而 `any` 落地之后它同样满足，
    这条却会把一个完全正确的配包判红。

    所以改成集合：`PUSHABLE_WITHOUT_A_QUESTION`。
    **它仍然拒 `on_request` 与 `timer`**（两者都要等一个条件），
    只是不再要求那一个字怎么拼。本仓「结论仍然对而理由已不成立」那一条：
    结论（要推得出去）没变，而「proactive 是唯一那一档」这个前提变了。
    """

    #: 客户没开口也发得出去的那几档。`on_request` 要等他提起话题，
    #: `timer` 要等一个定时事件 —— 两者都不满足开场那一轮的要求。
    pushable_without_a_question = {"proactive", "any"}

    frame = _introduce()
    by_ref = {m["ref"]: m for m in frame["content_materials"]}
    for ref in NAMED_MATERIALS:
        availability = by_ref[ref].get("availability")
        assert availability in pushable_without_a_question, (
            f"{ref} 的 availability 是 {availability!r}，不在 "
            f"{sorted(pushable_without_a_question)} 里 —— "
            "开场那一轮拿不到它，而 goal 让模型引它"
        )


def test_the_goal_bans_restating_and_only_that() -> None:
    """**禁的是「重复对方的话」，而且只有这一件。**

    先前 `goal` 禁的是三个**短语**（「我记下了」「我听到了」「这边我来安排」），
    而模型产出的是第四种说法（「这话我接住了」）——
    **用措辞写禁令，换个说法就绕过去了。** 所以改成一条规则 + 一句逐字反例。

    ⚠️ **另一半同样要钉**：用户当场收窄过——铺垫、共情、点评**在销售里是可以有的**。
    一条禁得比裁定宽的规则，下一个人读不出它本来该宽在哪里，
    而模型会照着那条宽的做。
    """

    goal = _introduce()["goal"]
    assert "不要重复对方的话" in goal
    # ⚠️ **2026-09-24（缺陷 25）反例整句从 goal 里拆掉了，这里跟着翻面。**
    # 这条用例当年的理由是「用措辞写禁令，换个说法就绕过去了」——
    # 于是补了一句逐字反例。复测证明反例自己也是措辞：模型把例句里的词
    # 借去当业务概念（「接住了」问了两轮），最后照着例句的形状复述了回去。
    # **禁令保留、反例没收**，正路指令（把答案用进下一句）顶上。
    assert THE_RESTATEMENT_MEASURED not in goal, (
        "那句实测反例又回到 goal 里了 —— 缺陷 25 实测它会被当措辞样本借走"
    )
    assert "也不要把他答的先复述一遍" in goal, "拆掉反例后正路指令必须在"
    for allowed in THE_ALLOWED:
        assert allowed in goal, (
            f"`goal` 没写明「{allowed}」是可以有的 —— "
            "规则会被读成比用户裁的更宽"
        )


def test_the_goal_says_understanding_is_not_announced() -> None:
    """**看懂了不用说出来** —— 用户 2026-09-20 当场补的那一条。

    「我收到了」「我知道了」与原来那三个短语是同一族：
    **它们把一轮回复花在宣告自己听懂了上**，而对面要的是答案。
    """

    goal = _introduce()["goal"]

    # ⚠️ **判据收在那一句上，不是整份 `goal`。**
    # `answered_confirmed` 那句里有「行**我知道了**」——
    # 拿整份 `goal` 找「我知道了」，禁令整条删掉它照样绿。
    # 本仓「守卫读到自己的散文」那一族，本批当场撞到一次。
    start = goal.index("**他提了问题或抱怨就回答它**")
    sentence = goal[start : goal.index("。", start) + 1]

    # **2026-09-24（缺陷 25）：点名改描述。** 那五个短语先前逐个写在这句禁令里，
    # 复测实测它们对模型同时是措辞样本 —— 现在禁令说的是「回执式短句」这个行为，
    # 五个字面量反过来一个都不许出现（整份 goal，不只这一句）。
    assert "回执式短句" in sentence and "不是答案" in sentence, sentence
    assert "看懂了不用说出来" in sentence
    for phrase in THE_ACKNOWLEDGEMENTS:
        assert phrase not in goal, (
            f"「{phrase}」又回到 goal 里了 —— 点名的例句会被当措辞样本借走"
        )


def _business_frames() -> list[dict]:
    """四个业务帧。**拆帧之前只有 `introduce` 一个**，所以「那一帧」与「这份包」
    当时是同一件事；现在一条规则可能落在它该落的那个帧上。"""

    return [f for f in _profile()["target_frames"] if f["id"] != "closing"]


def _every_business_goal() -> str:
    return "\n".join(f["goal"] for f in _business_frames())


def _every_business_slot() -> dict[str, dict]:
    return {s["name"]: s for f in _business_frames() for s in f["information_slots"]}


def test_the_thing_he_was_asked_three_times_now_has_a_slot() -> None:
    """**AI 把同一件事问了三遍，因为那件事没有地方记。**

    `concern_topic` 填上之后它**一次都没再问过** —— 机制是在的，缺的是那个槽。
    """

    # ⚠️ **2026-09-21 拆帧**：`current_handling` 留在主线，
    # `raised_concern` 跟着「处理疑虑」那件事进了 `objection` 帧。
    # 两个槽都还在，只是不在同一个帧里了 —— **这一条钉的是它们有地方记**。
    slots = _every_business_slot()
    assert set(NEW_SLOTS) <= set(slots), f"两个新槽不全：{sorted(slots)}"
    # 九个**不重名**的槽（`concern_topic` 在三个帧里各有一份，槽是帧内的）。
    assert len(slots) == 11, f"四个业务帧的槽位不是十一个：{sorted(slots)}"
    goal = _every_business_goal()
    for name in NEW_SLOTS:
        assert name in goal, (
            f"{name} 声明了而 `goal` 一个字没说它该记什么 —— "
            "模型不知道往里填什么（与 `turn_summary` 那次同形）"
        )
    # ⚠️ **这条规则搬了三次家，每一次都是上一个挂点被证明是空的。**
    #
    # 1. 2026-09-21 从**槽位**解绑（ADR-0144）：绑槽意味着每多一种
    #    「问过的事」就多一个槽、多一次抬版本，而对话的形状是开放的；
    # 2. 改挂**滚动摘要** —— 而运维缺陷 19（2026-09-23）证明那个挂点也是空的：
    #    v15/v16 把 `turn_summary` 改成**只写客户那一半**（那是对的，
    #    缺陷 16 修的就是摘要瞎猜我们做了什么），于是「摘要里问过」
    #    这个前提**在结构上不可能成立** —— 规则挂在一个恒缺席的事实上；
    # 3. 现在挂 **`pending_question`**：平台据真的发出去的正文记
    #    （用户裁定），三个提示词都收得到，且它记的是**发生过的问**，
    #    不是模型的声称（T331）。
    assert "`pending_question` 挂着的问题别再问" in goal, (
        "那条通则没了 —— 「别再问一遍」又没有了可靠的挂点"
    )
    assert "以它为准" in goal and "摘要只记他那侧" in goal, (
        "goal 没告诉模型「我们问过什么」的权威来源在哪 —— "
        "它会退回去读摘要，而摘要在结构上只记客户那一侧（缺陷 19 §②）"
    )
    assert "摘要里问过、他也答过的" not in goal, (
        "挂在摘要上的旧句回来了 —— 它的前提（摘要记我们问过）"
        "自 v15 起在结构上不成立，留着只会误导配包作者"
    )
    assert "填了就不要再问第二遍" not in goal, (
        "绑在槽位上的那句回来了 —— 与通则重复，且把读的人引回老路"
    )


def test_the_new_slots_do_not_make_the_frame_harder_to_leave() -> None:
    """⚠️ **这一帧今天已经出不去了，本批不再给它加门槛。**

    运维复测实测：`concern_topic` 填了，而 `material_delivered` 与
    `answered_confirmed` **两个必填都是空的** —— 七个回合、0 份资料、0 次报价。
    用户 2026-09-20 的原话：「**而且现在迟迟不能推进到下一步**」。

    所以两个新槽 `required: false` 且**不进 `exit_conditions`**。
    它们是「别问第二遍」的记录，不是「目标达成」的判据。
    """

    # ⚠️ **2026-09-21 拆帧**：这两个槽分落在两个帧上，而这一条问的是
    # 「它们有没有把自己所在的那一帧变得更难出去」—— 所以逐帧看，
    # 而不是把它们搬到一起看。
    slots = _every_business_slot()
    for name in NEW_SLOTS:
        assert slots[name]["required"] is False, f"{name} 成了必填 —— 那一帧会更出不去"

    gated = {
        condition["slot"]
        for frame in _business_frames()
        for condition in frame["exit_conditions"]
    }
    # ⚠️ **2026-09-21 拆帧动了这一格，而那是这一批的全部内容。**
    # 本条原来的断言写着「本批有意一个字不动它」——**那说的是当时那一批**。
    # 拆帧之后每个帧有自己的出帧条件：主线两条（知道他在意什么、知道他的态度）、
    # 报价帧「报过价」、异议帧「他这一条听明白了」、推成交帧「推过一次」。
    #
    # **这一条真正守的东西一个字没变**：上面那句仍然要求
    # `current_handling` / `raised_concern` 两个「别问第二遍」的记录
    # **不出现在这个集合里** —— 它们是记录，不是目标达成的判据。
    # ⚠️ **五个出帧条件里四个是「我们做过了」，只有 `usage_intent` 是判断。**
    # 用户 2026-09-21：「用户真听明白了也不一定表态」——
    # 一个等他反应的出帧条件，会把一通对话卡在那儿。
    assert gated == {
        "material_delivered",
        "usage_intent",
        "price_quoted",
        "concern_answered",
        "trial_offered",
    }, (
        f"出帧条件变了：{sorted(gated)} —— 改了就把上面那张表改过来，"
        "并核一遍两个「别问第二遍」的记录仍然不在里面"
    )
    for name in NEW_SLOTS:
        assert name not in gated, (
            f"{name} 成了出帧条件 —— 它是「别问第二遍」的记录，不是目标达成的判据"
        )


def test_the_goal_follows_him_rather_than_a_script() -> None:
    """**不必把当前这件讲完才换** —— 用户 2026-09-20 当场裁的。

    原话：「不是非得当前槽位东西说完才能进下个槽，可能客户就知道一部分就够了，
    他可能更想了解其他东西……**有些人对价格不是那么敏感，更关心功能**。」

    先前 `goal` 写的是「一段讲完、他也回应了，就引向下一件能少损耗的事」——
    **那是一条按我们的顺序走的规则**，而他关心的顺序不一定是我们的。
    同一条裁定也把疑虑从「最后才碰」放开成「他提了就接」。

    ⚠️ 举那个例子（价格 vs 功能）不是装饰：抽象的「跟着他走」模型读不出边界，
    一个真实的取舍读得出。
    """

    goal = _every_business_goal()
    assert "跟着他的关注点走" in goal
    assert "不必把当前这件讲完才换" in goal
    assert "更想知道功能" in goal, "那个具体的取舍例子没了 —— 只剩一句抽象的「跟着他走」"
    # 疑虑不再排在最后
    assert "最后才碰疑虑" not in goal, "「最后才碰疑虑」回来了 —— 与「跟着他的关注点走」打架"
    assert "raised_concern" in goal


def test_the_goal_still_fits_under_its_own_ceiling() -> None:
    """**本批是那条守卫预告的「下一条规则」，而余量是砍出来的。**

    那条守卫的报错原话：「下一条规则加不进去了，先把出处或铺陈砍掉」。
    用户 2026-09-20 裁过这件事：「**等下次要加规则时再砍**」——本批就是那一次。

    ⚠️ **砍到 1,099 / 1,100 之后用户改裁了：抬上限。**
    合同 1200 -> **1500**，守卫 1100 -> **1400**。理由是砍到后面
    剩下的每一处都不再是铺陈、是规则本身，**那时候再砍就是拿规则换余量**。
    代价量过：`goal` 进理解步与计划步**两个**请求
    （`inference_model.py:395` 与 `:560`，生成步不收它），
    所以它每轮付两遍 —— 今天 2,349 字节 × 2 = 4,698，
    上界抬到 1,400 字之后是 8,400，**每轮多 3,702 字节**。

    砍掉的都是「为什么」或与新规则重复的措辞，**一条规则都没少**：
    「不许只回你好/收到/在的再反问一个问题」（新规则是「引 ref 原文」，被它包含）、
    「不要停在原地等他提问」（与前半句同义）、
    「不确定的就说不知道，不要编」（与「答不上来就说不知道」重复），
    以及四处把话说长了的地方（「完全没必要」→「没必要」这一类）。
    """

    goal = _introduce()["goal"]
    assert len(goal) <= 1400, f"goal {len(goal)} 字 —— 又该砍了"
    # ⚠️ **下界 2026-09-21 从 1,000 降到 900，而降的理由不是「砍过头了没关系」。**
    # ADR-0144 决策 3 把「每个槽是什么意思」从 `goal` 搬进了
    # `information_slots[].description`，`goal` 因此**合法地短了 136 字**
    #（1,120 -> 984）：那五句没有消失，它们跟着槽走了。
    # **下界仍然在**，它防的还是那件事：一条规则被整条砍掉。
    assert len(goal) >= 900, (
        f"goal 只剩 {len(goal)} 字 —— 砍过头了，回去看是不是把一条规则整条砍掉了"
    )
