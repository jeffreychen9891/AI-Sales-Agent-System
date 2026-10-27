"""一条消息说一件事 —— **而「一件事」有多长，2026-09-23 起由文本说了算**。

## ⚠️ 本文件原来的那条规则（60 字上限）已被 ADR-0151 推翻

原文保留在下面，因为**推翻它的那条论证正是从它这里读出来的**。
它当时写着：

> 出站行的幂等键是 `{prefix}:{event_id}:{candidate_basis}`，带
> `UNIQUE (tenant_id, idempotency_key)` —— 一个提案、一行、一个计划候选，
> **平台切出来的第二行没有键可用**。

**那句话今天仍然对，而它挡的是「平台自己找切点」。**
ADR-0151 不找切点 —— 切点是模型在 `content.segments` 里给的，
序号因此是确定的，键由序号铸得出来（`SEGMENT_BASIS_INFIX`）。
**原来那条反对意见不适用于一个有序号的切点。**

而 60 字上限本身的成因，也在下面那段现场数据里：它是给
「计划步得先猜这一轮要几个气泡」打的补丁。猜错了就得把答案压进 60 字，
**而用户 2026-09-23 裁定：「有很多情况不是 60 个字能讲明白的」**。

---

## 以下是被推翻的那一版的原文，保留

一条消息说一件事：60 字**上限**，分段由模型做，**平台不切**。

## 现场（运维复测 2026-09-20，实测五条出站）

| | 条数 | 最短 | 最长 | 平均 |
|---|---|---|---|---|
| **AI 出站** | 5 | 60 | 155 | **101 字** |
| 客户入站 | 7 | 2 | 31 | **10 字** |
| 商家自己审过的素材 | 29 | 17 | **115** | 49（中位 45）|

**AI 每一条都比商家写过的最长那段还长。** 60 这个数因此不是拍的：
它是商家话术的 P90（63 是 P95），而**商家的话就是这个商家愿意让客户读的长度**。

## 三条裁定，以及它们各自挡掉了什么

1. **「太长就分多段发送」** —— 投递侧本来就支持一轮多条
   （`outbox_messages.ordinal`，T109 / 迁移 044），所以不用丢、不用拒；
2. ⚠️ **「不能设硬线，长段落也比说错话强」** —— **没有平台切**。
   两个理由，第二个是结构性的：机械切会把「因为…所以…」拦腰切断，
   **而且出站行的幂等键是 `{prefix}:{event_id}:{candidate_basis}`**，
   带 `UNIQUE (tenant_id, idempotency_key)` —— 一个提案、一行、一个计划候选，
   **平台切出来的第二行没有键可用**；
3. ⚠️ **「生成不是每次都要 60 字，60 字是上限」** —— 措辞不许把上限读成目标。
   本文件为此单立一条判据（下面 `…is_a_ceiling_not_a_target`）。

## ⚠️ 为什么规则要点名「计划列了几条」

生成步提的提案数超过计划列的候选数会抛 `GENERATION_DUPLICATED_INTENT`，
**那是整份拒 —— 客户这一轮收到零字**。
一句光秃秃的「讲不完就分两条」会把长回答变成沉默，
**而沉默正是上面第 2 条裁定要避免的方向**。所以两件一起做：
计划步被告知「讲不完就列第二个候选」，生成步被告知「不许超过计划列的条数」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastagent_v2.worker.inference_model import (
    _GENERATE_SYSTEM,
    _PLAN_SYSTEM,
    PROMPT_RELEASE,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "profile.json"
MATERIALS = REPOSITORY_ROOT / "profiles" / "internal-demo-sales" / "materials.json"

#: 上限。**它是商家话术的 P90**，不是一个好看的整数。
CEILING = 60

#: 把上限读成目标的那几种写法。**一个都不许出现在提示词里。**
#:
#: ⚠️ 这张表是窄的：它挡的是「**顺手把 ceiling 写成 around/about**」，
#: 不是「绝无目标色彩」。用别的说法写成目标它抓不到 ——
#: 本条与 `…says_it_is_a_ceiling` 一起才完整。
TARGET_SHAPED = ("around 60", "about 60", "roughly 60", "60 characters long", "aim for 60")


def test_the_generate_prompt_caps_each_message() -> None:
    """**每条 60 字上限，写进生成步。**"""

    assert "There is no character limit" in _GENERATE_SYSTEM, (
        "生成步还压着字数 —— 而用户 2026-09-23 裁定「有很多情况不是 60 个字能讲明白的」"
    )
    assert "at most 60 characters" not in _GENERATE_SYSTEM, (
        "那条上限还在 —— 它是给「计划步先猜几个气泡」打的补丁，而那个猜测已经取消了"
    )
    assert "its own message" in _GENERATE_SYSTEM, (
        "生成步没被告知「每段各成一个气泡」—— 不分段的话长答案会挤成一条"
    )


def test_the_prompt_no_longer_reads_the_ceiling_as_a_target() -> None:
    """⚠️ **这一条从「上限不许读成目标」改成了「上限整个不在了」。**

    原来它守的是措辞：用户 2026-09-21 当场更正过「60 是上限，不是每次都要写到」。
    ADR-0151 把上限本身取消了，所以那几种「读成目标」的写法更不许留 ——
    它们会以另一种方式复活同一个约束。
    """

    for shape in TARGET_SHAPED:
        assert shape not in _GENERATE_SYSTEM, f"「{shape}」把一个已经取消的上限写了回来"
    assert "ceiling, not a target" not in _GENERATE_SYSTEM, (
        "那句话还在 —— 它描述的是一条已经不存在的上限"
    )


def test_the_merchants_own_lengths_are_offered_as_a_size_not_a_rule() -> None:
    """**商家话术的长度仍然告诉模型，而它现在是「一个气泡通常多大」，不是闸。**

    那组数是量出来的，取消上限不等于扔掉它 —— 扔掉的话模型会退回
    实测那种「每条平均 101 字、比商家写过的最长一段还长」的习惯。
    这一条钉住它还在，并且**被明说成不是规则**。
    """

    texts = list(json.loads(MATERIALS.read_text(encoding="utf-8"))["materials"].values())
    lengths = sorted(len(text) for text in texts)
    assert len(lengths) >= 20, f"素材只剩 {len(lengths)} 段，这组数不再有意义"

    assert "median 45" in _GENERATE_SYSTEM, "商家自己的长度分布不在提示词里了"
    assert "not a rule" in _GENERATE_SYSTEM, (
        "那组数没被明说「不是规则」—— 模型会把它读回成上限"
    )
    assert str(lengths[-1]) in _GENERATE_SYSTEM, (
        f"提示词引的最长值与包里现读的 {lengths[-1]} 对不上 —— 素材改了就要重量"
    )


def test_the_generate_step_is_not_capped_by_what_the_plan_listed() -> None:
    """⚠️ **最要紧的一条，而它的方向反过来了。**

    原来：「讲不完就再来一条」必须同时说「不许超过计划列的条数」，
    否则撞 `GENERATION_DUPLICATED_INTENT` —— 整份拒、客户收到零字。

    今天：**计划只列一条，生成也只提一条**，条数由分段决定。
    那条「不许超过计划列的」因此不该再出现在生成步 ——
    它会把模型推回「要不要再列一条」那个已经取消的判断。
    """

    assert "the plan listed" not in _GENERATE_SYSTEM, (
        "生成步还在让模型对齐计划列的条数 —— 而条数已经不由计划决定了"
    )
    assert "more messages than it listed" not in _GENERATE_SYSTEM


def test_the_plan_is_told_to_list_exactly_one() -> None:
    """**规则写在产候选的那一层** —— 计划步只列一条 `send_message`。

    ⚠️ 原来这里钉的是反面（「讲不完就列第二个候选」）。
    用户 2026-09-23 裁定「**收掉，一个统一的地方发声**」。
    """

    assert "List at most one send_message" in _PLAN_SYSTEM, (
        "计划步没被告知只列一条 —— 它会继续猜这一轮要几个气泡"
    )
    assert "Also list a second one" not in _PLAN_SYSTEM
    assert "A turn may carry more than one send_message" not in _PLAN_SYSTEM


def test_there_is_no_platform_side_cut() -> None:
    """⚠️ **平台不切，而这一条钉的是「它仍然没被加回来」。**

    用户 2026-09-21：「**不能设硬线，长段落也比说错话强**」。

    这一条红的时候多半是有人加了一个按长度切段的实现。那时先回来读这段：
    机械切会把「因为…所以…」拦腰切断，**而且切出来的第二行没有幂等键可用**
    —— 一个提案、一行、一个计划候选，三者锁死。
    """

    pipeline = (
        REPOSITORY_ROOT
        / "services"
        / "worker"
        / "src"
        / "fastagent_v2"
        / "worker"
        / "pipeline.py"
    ).read_text(encoding="utf-8")
    for shape in ("def _split_long", "split_into_messages", "chunk_outbound"):
        assert shape not in pipeline, f"出现了平台切的实现（{shape}）—— 见本条 docstring"


def test_the_release_moved_with_the_prompt() -> None:
    """改了提示词就要抬版本号 —— 本仓原有的两条也守这件事，这里只是点个名。

    ⚠️ **这一条会跟着每一次提示词改动走**，本条落地时是 v10；
    同日的「教模型怎么记」那一批 rebase 上来之后是 v11。
    它钉的从来不是某个数字，而是**「正文动了，版本就得动」**。
    """

    assert PROMPT_RELEASE == "m1a-prompt-v17"


def test_the_cost_of_these_lines_was_paid_out_of_the_pool() -> None:
    """**新增的字节从素材预算里出，不靠安全余量兜。**

    生成步系统提示每长一点，空请求基线就跟着涨，而素材预算是从那个基线推的。
    本仓记过：5.7 那一批往生成步加 245 字节**没有**扣预算，
    安全余量当场掉到 4,790，那一次撤了。
    """

    from fastagent_v2.profiles.registry import (  # noqa: PLC0415
        MATERIAL_BYTES_BUDGET,
        PLATFORM_PROMPT_BYTES,
    )
    from fastagent_v2.worker.material_selection import (  # noqa: PLC0415
        MATERIAL_BYTES_BUDGET as WORKER_BUDGET,
    )
    from fastagent_v2.worker.material_selection import (  # noqa: PLC0415
        SAFETY_MARGIN_BYTES,
    )

    # ⚠️ 本条落地时是 (24572, 6341)；同日的「教模型怎么记」那一批又从池子里
    # 扣了 588、把生成步提示推到 6,900。**两笔的记法相同：从池子里出，
    # 安全余量一个字没动** —— 下面那条断言才是本条真正守的东西。
    # 2026-09-23（ADR-0151）：去掉 60 字上限、换成分段那一段，生成步 +345，
    # **同样从池子里扣**：23,984 -> 23,639，安全余量一个字没动。
    # 本条守的从来不是某个数，是「新增的字节从哪儿出」。
    # 2026-09-23（运维缺陷 19）22,579 -> 21,214：`pending_question` 三笔
    # （生成步规则 +337、空键 +28、变长项新扣一行 1,000），照旧从池子里出。
    assert MATERIAL_BYTES_BUDGET == WORKER_BUDGET == 21213
    assert SAFETY_MARGIN_BYTES == 5000, "安全余量动了 —— 这一批的账应当从池子里出"
    assert PLATFORM_PROMPT_BYTES["GENERATE_VALUE"] == 8643
    # 2026-09-21（ADR-0146）3,688 -> 4,542：计划步多了「frames_entered 是什么、
    # 进过的帧别再 push、他再问起就在当前帧用自己的话简答」那一段。
    # 同上，计划步「至多列一条」那一段 +28。
    # 2026-09-23（运维缺陷 19）4,570 -> 4,834：计划步的 `pending_question` 规则。
    assert PLATFORM_PROMPT_BYTES["PLAN_CAPABILITY"] == 4834


def test_the_measured_numbers_in_this_file_are_still_the_packages_numbers() -> None:
    """本文件 docstring 里引的那三个素材数，**是从包里现读的**。

    ⚠️ 写下来的数会过期，而过期的数比没有数更坏 —— 它读起来像有依据。
    """

    lengths = sorted(
        len(text)
        for text in json.loads(MATERIALS.read_text(encoding="utf-8"))["materials"].values()
    )
    header = Path(__file__).read_text(encoding="utf-8").split('"""')[1]
    quoted = re.search(r"\| 商家自己审过的素材 \| (\d+) \| (\d+) \| \*\*(\d+)\*\*", header)
    assert quoted is not None, "docstring 里那一行表格改了形状"
    count, shortest, longest = (int(value) for value in quoted.groups())
    assert (count, shortest, longest) == (len(lengths), lengths[0], lengths[-1]), (
        f"docstring 写的是 {count}/{shortest}/{longest}，"
        f"而包里现读是 {len(lengths)}/{lengths[0]}/{lengths[-1]}"
    )
