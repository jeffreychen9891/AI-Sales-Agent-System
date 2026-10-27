"""ADR-0144 决策 3：每个槽带一句「它是什么意思」，而那句话要真的到模型手上。

## 在它之前

一个槽到模型那里**只有名字和类型**：`{"name": "deposit_amount", "type": "money",
"required": true}`。`deposit_amount` 是一个模型得自己猜意思的词，
而**猜错的方向是安静的** —— 它能填、能过校验、能写进库，只有值是错的。

实测（2026-09-21，加描述之前）：三份在架配包 **19 个槽**，
其中 **12 个**（`synthetic-carrier` 9 + `synthetic-third` 3）在它们的 `goal` 里
**一个字都没被提过** —— 那两份 `goal` 各只有 30–57 字，一句话。

## 别人的做法，以及我们照搬了哪一半

Google 的 Schema-Guided Dialogue（arXiv:1909.05855）给每个 slot / intent
配一段**自然语言描述**，模型读描述来填，于是**新增一个槽不需要重新训练、
也不需要预先见过**。本批照搬的就是这一件。

⚠️ **没照搬的那一半，以及为什么**：后续基准 SGD-X（AAAI 2022，arXiv:2110.06800）
用五套「语义相同、措辞不同」的 schema 变体量出，两个当时最好的模型
**仅因描述换了措辞**就掉 17.6% / 11.9% 联合目标准确率（最差 28% / 19%）。
作者窗口据此建议先出一份「描述怎么写」的规范再开工，
**用户 2026-09-21 裁定不写**：「不用写规范，先看看这样效果好不好」，
理由是那两个数来自 2021–22 年的**微调**模型，而「5 年了大模型稳定多了」。

**所以这一批是一次有意的试** —— 留这段是为了让下一个读到
「为什么明知有 SGD-X 还是没规范」的人看到：**有人想过，并且给了理由。**
"""

from __future__ import annotations

import json
from pathlib import Path

from fastagent_v2.runtime import request_shape

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILES = REPOSITORY_ROOT / "profiles"
SCHEMA = REPOSITORY_ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"

#: 2026-09-21 现读。**钉等值不钉上界** —— 多一份配包、多一个槽，
#: 这条先红，于是「这一批有没有给新槽写描述」变成一次决定。
# ⚠️ **2026-09-21：19 -> 25**。拆帧那一批把 `internal-demo-sales` 从两个帧
# 拆成五个（主线 + 报价 / 异议 / 推成交 + 收尾），每个新帧各带自己的槽。
SHIPPED_SLOTS = 27

#: 描述里**不该**出现的那几类词：它们是行为约束，归 `goal`
#: （用户 2026-09-21：「行为约束 goal 来做」）。
#:
#: ⚠️ **这张表是窄的，而且有意窄**：它只认几个最直白的祈使词。
#: 一条用别的说法写成的行为约束它抓不到 —— 本条守的是
#: 「**别顺手把 goal 那一半抄进来**」，不是「描述里绝无行为色彩」。
BEHAVIOURAL_WORDS = ("不要", "只填", "不替", "不为填", "Do not ", "Never ", "must not")


def _packages() -> dict[str, dict]:
    out = {}
    for directory in sorted(PROFILES.iterdir()):
        path = directory / "profile.json"
        if path.exists():
            out[directory.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def _slots() -> list[tuple[str, str, dict]]:
    return [
        (name, frame["id"], slot)
        for name, document in _packages().items()
        for frame in document["target_frames"]
        for slot in frame.get("information_slots", [])
    ]


def test_every_shipped_slot_has_one() -> None:
    """**三份配包 19 个槽，一个不少。**

    合同已经把 `description` 设成必填，所以这一条不是重复那件事 ——
    它钉的是**数**：多一个槽而没写描述，合同会拦；
    而**多一份配包**时，这一条会先提醒你去数。
    """

    slots = _slots()
    assert len(slots) == SHIPPED_SLOTS, (
        f"在架槽位 {len(slots)} 个，而本条钉的是 {SHIPPED_SLOTS} —— "
        "加了配包或加了槽就把这个数改掉，顺便确认每一个都写了描述"
    )
    missing = [
        f"{package}/{frame}/{slot['name']}"
        for package, frame, slot in slots
        if not str(slot.get("description", "")).strip()
    ]
    assert missing == [], f"这些槽没有描述：{missing}"


def test_the_description_reaches_the_model() -> None:
    """**写了没人读，等于没写。**

    本仓「零调用方」那一族：一个字段写在配包里、而没有任何东西把它送出去，
    读的人会以为模型看得见它。这一条走 `request_shape` —— **生产装配
    `slots_wanted` 的同一个函数**，不另写一份镜像。
    """

    for package, frame_id, slot in _slots():
        document = _packages()[package]
        frame = next(f for f in document["target_frames"] if f["id"] == frame_id)
        prompt = request_shape.understand_prompt(
            goal=frame["goal"], information_slots=frame["information_slots"]
        )
        entry = next(
            item for item in prompt["slots_wanted"] if item["name"] == slot["name"]
        )
        assert entry.get("description") == slot["description"], (
            f"{package}/{frame_id}/{slot['name']} 的描述没进 slots_wanted"
        )


def test_the_description_is_bounded_because_it_is_paid_twice() -> None:
    """**它每轮付两遍** —— `slots_wanted` 进理解步与计划步，与 `goal` 同一条路。

    合同的 `maxLength` 是 120。这一条量的是**今天实际用了多少**，
    好让「描述开始变长」这件事在合同拦下它之前就看得见。
    """

    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    items = document["properties"]["target_frames"]["items"]["properties"][
        "information_slots"
    ]["items"]
    assert items["properties"]["description"]["maxLength"] == 120
    assert "description" in items["required"]
    assert items["additionalProperties"] is False, (
        "闭合性没了 —— 一个拼错的键会悄悄过 admission（那条注释写了理由）"
    )

    longest = max(_slots(), key=lambda row: len(row[2]["description"]))
    assert len(longest[2]["description"]) <= 60, (
        f"最长那段是 {longest[0]}/{longest[2]['name']}，{len(longest[2]['description'])} 字 —— "
        "离合同上限 120 不远了，而它每轮付两遍"
    )


def test_behavioural_rules_stayed_in_the_goal() -> None:
    """**用户 2026-09-21：「行为约束 goal 来做」。**

    描述答的是「这个槽是什么意思」，不是「该怎么做」。
    第一稿我把「只填 allowed_values 里的一个」「不替他判断，也不为填它追问」
    写进了描述，**那是把 `goal` 的活搬错了地方**。

    ⚠️ 那两条**今天仍然在 `goal` 里**，本条只保证它们没有被复制到描述里。
    """

    leaked = [
        (package, slot["name"], word)
        for package, _frame, slot in _slots()
        for word in BEHAVIOURAL_WORDS
        if word in slot["description"]
    ]
    assert leaked == [], f"行为约束漏进了描述：{leaked} —— 那一半归 goal"


def test_the_same_slot_name_in_three_packages_says_three_different_things() -> None:
    """**同名不同义，正是描述要说清楚的事**（用户 2026-09-21：「3 份分开」）。

    `closing_acknowledged` 在三份配包里都叫这个名字，而它们收的不是一件事：
    演示包收的是一次接待的收尾，carrier 收的是一次出借怎么结束，
    third 收的是这次交流可以结束了。

    ⚠️ 第一稿我想过把三段统一成一句 —— **那会把描述变回一个名字**，
    而这个名字正是它们唯一相同的地方。
    """

    same_name = {
        package: slot["description"]
        for package, _frame, slot in _slots()
        if slot["name"] == "closing_acknowledged"
    }
    assert len(same_name) == 3, f"`closing_acknowledged` 不在三份配包里了：{sorted(same_name)}"
    assert len(set(same_name.values())) == 3, (
        f"三段描述里有重复：{same_name} —— 同名不同义，统一了就等于没写"
    )


def test_the_twelve_slots_that_had_nothing_now_have_something() -> None:
    """**本批真正从零到有的那 12 个。**

    两份合成配包的 `goal` 各只有一句话（30–57 字），
    **它们的槽在 `goal` 里一个字都没被提过**。
    演示包那 7 个是**搬家**（那几句本来就在 `goal` 里），这 12 个不是。
    """

    for package in ("synthetic-carrier", "synthetic-third"):
        document = _packages()[package]
        for frame in document["target_frames"]:
            goal = frame["goal"]
            for slot in frame.get("information_slots", []):
                assert slot["name"] not in goal, (
                    f"{package}/{frame['id']}/{slot['name']} 现在出现在 goal 里了 —— "
                    "本条记的是「描述之前这些槽没有任何说明」这个事实，"
                    "goal 里开始提它们的话，这条记录要跟着改"
                )
                assert slot["description"].strip()
