"""话题词表要覆盖**整条销售流程走到成交**（用户 2026-09-18 裁定）。

## 补之前：十二个话题里一个流程类都没有

八个值全是**疑虑类** —— 花钱 / 能省几个人 / 团队的损耗 / 瞎答应 /
找真人 / 查问题 / 我们这行能不能用 / 和以前那个有什么不一样。

而**流程类**（开场 → 摸底 → 试用 → 落地）全靠 `proactive` + 阶段门推着走：
**客户主动问流程的时候，一段可引的话都没有。**

运维 §10 那一轮正是这个形状 —— 模型承诺「我先把试用怎么开讲清楚」，
而这一刻它手边只有价格那三段，于是贴出了价格。

## 词表是推导出来的，不是写在某张表里的

`runtime.topic_vocabulary`：**合法取值 = 本帧 `content_materials[].topics` 的键集**。
所以「补一个话题值」在这个仓里的意思就是**写一段素材**；
schema 的原话：「想让一个话题合法，就给它写一段素材。」

## 这份文件钉的四件

1. 那四个新话题**真的在词表里**（而词表是现推的，所以等价于「真的有素材」）；
2. 每一个新话题**至少有一段话术**（一个点亮 0 段的话题是一道永远不开的闸）；
3. **不是把旧素材改了个门** —— 旧那 14 段的话题一个没动；
4. `asset:trial-setup-guide` 改档之后**仍然发得出去**，而阶段推进不受影响。
"""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any

from fastagent_v2.runtime.topic_vocabulary import topic_vocabulary

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales"

#: 用户 2026-09-18 裁定要补的四类，**按「到成交为止」挑的**。
ADDED = ("怎么开通", "多久能上", "怎么付款", "数据安全")

#: 补之前那八类，**全是疑虑类**。一个都不许在这一批里掉。
KEPT = (
    "花钱",
    "能省几个人",
    "团队的损耗",
    "瞎答应",
    "找真人",
    "查问题",
    "我们这行能不能用",
    "和以前那个有什么不一样",
)


def _profile() -> dict[str, Any]:
    return json.loads((PACKAGE / "profile.json").read_text(encoding="utf-8"))


def _frame() -> dict[str, Any]:
    """**本包全部业务帧合起来的那一份** —— 不是某一个帧。

    ⚠️ **2026-09-21 拆帧之前这里读的是 `introduce`，而那时它是唯一的业务帧**，
    所以「那一帧的词表」与「这份包的词表」是同一个东西。拆帧之后
    十四类散进了四个帧（八类疑虑在 `objection`、花钱/怎么付款在 `quote`、
    怎么开通/多久能上在 `trial_close`），**没有任何一个帧还装得下这份名单**。

    本文件钉的从来是「这几类在这份包里有词可说、有闸可过」，
    所以改成取并集。**这不是放宽**：拆帧前后这个集合逐字相同 ——
    一个帧的时候它就是全部，四个帧的时候它是四份之和。
    """

    document = _profile()
    business = [
        f for f in document["target_frames"]
        if f["id"] not in {"closing"}
    ]
    merged: dict[str, Any] = {
        "id": "+".join(f["id"] for f in business),
        "content_materials": [m for f in business for m in f.get("content_materials", [])],
        "content_assets": [a for f in business for a in f.get("content_assets", [])],
        "material_stages": [s for f in business for s in f.get("material_stages", [])],
        "material_topic_slot": "concern_topic",
    }
    return merged


def _materials() -> dict[str, str]:
    return json.loads((PACKAGE / "materials.json").read_text(encoding="utf-8"))[
        "materials"
    ]


def test_the_close_side_topics_are_legal_values_now() -> None:
    """**词表是现推的**，所以这一条等价于「那四类真的有素材」。

    `topic_vocabulary` 是唯一推导点：合法取值 = 本帧
    `content_materials[].topics` 的键集 —— **告知模型的集合与
    `apply_slot_updates` 执行的集合同源**（2026-09-15「裁 B」）。
    """

    found = topic_vocabulary(_frame())
    assert found is not None, "这一帧没有话题词表 —— 那四类一个都进不去"
    slot, vocabulary = found

    assert slot == "concern_topic"
    missing = [topic for topic in ADDED if topic not in vocabulary]
    assert not missing, f"这几类不在词表里：{missing}"


def test_the_eight_concern_topics_are_all_still_there() -> None:
    """**这一批是补，不是换。**

    八类疑虑是这个销售今天真正在答的东西；
    一次「补流程类」顺手挤掉一类疑虑，客户问到那一类时
    **商家审过的话一条都没有**，而模型会自己编。
    """

    found = topic_vocabulary(_frame())
    assert found is not None
    _slot, vocabulary = found

    lost = [topic for topic in KEPT if topic not in vocabulary]
    assert not lost, f"补的时候把这几类挤掉了：{lost}"
    assert len(vocabulary) == len(ADDED) + len(KEPT), (
        f"词表现读 {len(vocabulary)} 个，而补四类之后应当是 "
        f"{len(ADDED) + len(KEPT)} 个：{sorted(vocabulary)}"
    )


def test_every_new_topic_has_words_to_say() -> None:
    """**一个点亮 0 段的话题是一道永远不开的闸。**

    它比没有这个话题更坏：模型收到这个合法值、往槽里写了它，
    而那一轮**一段素材都不到场** —— 于是它自己编一段。
    """

    frame = _frame()
    texts = _materials()
    by_topic: dict[str, list[str]] = collections.defaultdict(list)
    for item in frame["content_materials"]:
        for topic in item.get("topics") or []:
            by_topic[str(topic)].append(str(item["ref"]))

    for topic in ADDED:
        refs = by_topic.get(topic, [])
        assert refs, f"「{topic}」一段素材都没有 —— 那它不该是一个合法值"
        for ref in refs:
            assert texts.get(ref, "").strip(), f"{ref} 声明了而正文是空的"


def test_the_new_materials_are_on_request_not_proactive() -> None:
    """**新那几段是「被问到才答」，不是主动推。**

    主动推那一路今天已经有了（`proactive` + 阶段门），
    这一批补的是**它问了我们答得上来** —— 两条路不是一回事，
    而 schema 也不许非 `on_request` 的素材带 `topics`。
    """

    frame = _frame()
    for item in frame["content_materials"]:
        topics = set(item.get("topics") or [])
        if topics & set(ADDED):
            assert item.get("availability") == "on_request", (
                f"{item['ref']} 带了新话题却不是 on_request"
            )


def test_the_old_fourteen_kept_their_gates() -> None:
    """**没有把旧素材改个门来凑数。**

    这一条防的是一种很省事的做法：把某段旧话术的 `topics` 改成新话题，
    词表就「补上了」—— 而客户问原来那一类时反而拿不到了。
    """

    frame = _frame()
    old = {
        "material:demo-price-plain:v1": ["花钱"],
        "material:demo-price-flat:v1": ["花钱"],
        "material:demo-price-metered-cost:v1": ["花钱"],
        "material:demo-limit-replace-no:v1": ["能省几个人"],
        "material:demo-limit-metrics-none:v1": ["能省几个人"],
        "material:demo-limit-not-a-salesperson:v1": ["能省几个人", "团队的损耗"],
        "material:demo-promise-gate:v1": ["瞎答应"],
        "material:demo-promise-gate-limit:v1": ["瞎答应"],
        "material:demo-human-takeover:v1": ["找真人"],
        "material:demo-human-not-self-escalate:v1": ["找真人"],
        "material:demo-audit-what:v1": ["查问题"],
        "material:demo-audit-boundary:v1": ["查问题"],
        "material:demo-industry-config:v1": ["我们这行能不能用"],
        "material:demo-vs-previous:v1": ["和以前那个有什么不一样"],
    }
    current = {
        str(item["ref"]): list(item.get("topics") or [])
        for item in frame["content_materials"]
    }

    for ref, topics in old.items():
        assert current.get(ref) == topics, (
            f"{ref} 的话题被改了：{topics} → {current.get(ref)}"
        )


def test_the_trial_guide_can_be_asked_for_now() -> None:
    """**`asset:trial-setup-guide` 改档**（用户 2026-09-18 裁定）。

    `proactive` + 阶段 03 → `on_request` + 话题「怎么开通」。

    **阶段推进不受影响**：`material_stages.after` 指的是**素材**，不是文件 ——
    这一条把那个理由钉住，免得下一个人以为改档会把阶段卡死。
    """

    frame = _frame()
    guide = next(
        a for a in frame["content_assets"] if a["ref"] == "asset:trial-setup-guide:v1"
    )

    assert guide["availability"] == "on_request"
    assert guide.get("topics") == ["怎么开通"]
    assert "stages" not in guide

    after = [
        ref
        for stage in frame["material_stages"]
        for ref in (stage.get("after") or [])
    ]
    assert all(ref.startswith("material:") for ref in after), (
        f"阶段推进指着一个不是素材的东西：{after} —— 那时改文件的档就会卡住推进"
    )


def test_the_package_version_moved_with_the_content() -> None:
    """内容变了，版本号跟着抬 —— **这个仓为它付过账**（`release_id` 撞号那次）。"""

    identity = _profile()["identity"]

    # **2026-09-20：1.2.4 -> 1.2.5**，内容改的是帧目标里报价那一句
    # （话术只逐字发一次之后，第二次问价改用自己的话说那个数）。
    # **这条用例照它该有的样子红了一次** —— 改内容不抬号，它就是本仓
    # 付过五次钱的那个撞号。
    #
    # **2026-09-20 晚：1.2.5 -> 1.3.0**，而这一次是 **minor 不是 patch** ——
    # 加了两个 `information_slots`（`current_handling` / `raised_concern`），
    # 那是**多了一种能力**，不是修一句话。`release_id` 末 12 位按
    # major/minor/patch 各四位编码，所以它同时从 `…00020005` 变成 `…00030000`。
    # 改动本身：`goal` 给开场与桥接两段点名 ref、加「销售不说废话」那条、
    # 加两个槽，并砍掉三处铺陈才装得下（上限 1,100，现 1,099）。
    # **2026-09-21：1.3.1** —— 六个记忆字段各加了一句描述，
    # 内容变了就得抬号（`release_id` 末段跟着走）。
    # **2026-09-21：1.3.2** —— 时间规则 2 条 → 12 条（情况一到就动）。
    # **2026-09-21：1.4.0** —— 拆帧（主线 + 报价 / 异议 / 推成交 + 收尾）。
    # ⚠️ **2026-09-21：1.4.1 —— 这一次是被判退逼出来的。**
    # 1.4.0 已随 `d62377f6` 进了 `main`，本支再改内容就是
    # **同一个 `release_id` 下两份不同的内容**。
    # **2026-09-22：1.4.3**（ADR-0149）—— 五段素材 `proactive` → `any`
    # （定时器轮次也拿得到，而阶段阶梯照走），`goal_stall` / `silence_check`
    # 的零延迟改成 120 秒。内容变了就抬号，`release_id` 末段跟着走。
    # **2026-09-22：1.4.4**（ADR-0150）—— `quote` 的 `revisit`
    # `brief_answer` → `reenter`（用户：「有些销售场景需要反复报价」），
    # 同一帧的 `price_quoted` 声明 `on_revisit: "carry"`。**两格是一对**：
    # 可以再进来，而再进来时「已经报过的那个数」还算数。
    # **2026-09-23：1.4.6**（运维缺陷 18）—— `quote` 的 goal 去掉了
    # 「**清单里没有它就是已讲过**」与「**别说报不了价**」这两句，
    # 换成三条出路（引原文 / 照 `price_quoted` 里的数说 / 取不到就不报），
    # 同一帧的 `price_quoted` 声明 `filled_by_delivery`。
    # **两处是一对**：出路二要有个地方拿那个数，而那个地方现在由平台的
    # 投递记录填。用户裁定：「手上没那个数就不准报」。
    # **2026-09-23：1.4.7**（运维缺陷 19）—— `ask_slot` 声明
    # `required_slots: ["current_handling"]`（先前八个策略全空，两条机制恒不触发），
    # introduce 的防重复规则从「摘要里问过」改挂 `pending_question`
    # （摘要那个挂点被 v15 的「只写客户那半」拆掉了，是结构性失效）。
    # **2026-09-24：1.4.8**（批三，用户裁定）—— 五个帧的 `entry_conditions`
    # 整键收掉：平台零读点、点名的六个概念零实现，职能已由根帧约定 /
    # `closing_frame_ref` / `advance_when` / 模型判断四路承接（逐帧对过账）。
    # **2026-09-24：1.4.9**（缺陷 21 + 全量素材审核，用户指示）——
    # bridge 素材从 36 字的「我这就讲」补成 115 字的真讲解（桥后有路）；
    # price-flat / price-metered-cost 的「这个数/那个月费」指代自含化
    # （单独被引时不再复现缺陷 18 的症状）；func-close-negative 删
    # 「我记一下」（回执族边缘）。29 份全审，其余 25 份通过。
    # **2026-09-24：1.4.12**（缺陷 23/24/25 批）—— 四个 goal 改写：
    # 反例模板拆掉（「这话我接住了」与回执五连整套被模型当措辞样本借走，
    # 缺陷 18「规则被示例拆了」第二例）、补「你的待办不是话题」、
    # 报价不外推。此前 **1.4.11**（K4 批，适配审查裁定）—— 五帧的
    # `ai_commitment_due` time_rule 整条收掉：AI 承诺唯一入账路是
    # `outcome_rules`（demo 是 0 条且策略清一色 `no_new_commitment`），
    # per-row 锚 `commitment_due_at` 永无行可挂，五条规则从发布起
    # 一次都没可能响过。封版新检 `_check_commitment_timer_rules_have_a_registrar`
    # 从此拒这种全死形状。此前 **1.4.10**（exit/tone 批，用户裁定两项）——
    # `return_confirmation.tone_material_ref` 整键收掉（三包悬空 + 运行期零消费；
    # 回归确认那句话由模型按上下文自己写，与静默唤醒同一条哲学）。
    # 同批 `exit_conditions` 建执行（complete 闸终于读商家写的出口判据），
    # 配包的谓词原值合规、一字未动。
    assert identity["version"] == "1.4.12"
    assert identity["release_id"].endswith("000100040012"), (
        "release_id 末 12 位按 major/minor/patch 各四位编码，1.3.0 → 000100030000"
    )


def test_the_dimensions_reporter_no_longer_says_something_false() -> None:
    """**那条 `[孤]` 提示今天是假的，本批改掉了它。**

    它原来写着「**模型不会把这个词写进槽**」，判据是话题名在不在 `goal`
    那段散文里，理由写着「模型往槽里写什么由 goal 那句话规定」。

    **那句话在 2026-09-15「裁 B」之前成立。** 之后不成立了：
    词表由 `runtime.topic_vocabulary` 现推，作为
    `slots_wanted[].allowed_values` **直接渲染进理解步的提示词**。

    ## 实测（2026-09-18）

    `internal-demo-sales` 1.2.4 的十二个话题里 **十个**被标 `[孤]`，
    **其中包括「花钱」** —— 而运维实测里 `concern_topic` 从 09-17 06:31 起
    **连着两天就黏在「花钱」上**。

    > **一条说「模型不会写这个词」的提示，指着模型写了两天的那个词。**

    那个脚本自己的 docstring 早写过这条罪：
    「a reporter that disagrees with the gate is worse than no reporter,
    **because it is believed**」。
    """

    reporter = (
        REPOSITORY_ROOT / "scripts" / "check_material_dimensions.py"
    ).read_text(encoding="utf-8")

    assert "模型不会把这个词写进槽" not in reporter.split('"""')[-1], (
        "那句假话又回到输出里了"
    )
    assert "模型仍然收得到这个值" in reporter, (
        "提示没说清「goal 没点名」不等于「模型收不到」"
    )
