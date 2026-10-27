"""封版侧为文件候选预留字节 —— **而在这之前它一条都没算**。

## 这条补的是哪个洞

`registry.py` 逐个阶段算「这一格最多会往提示词里塞多少字节」，此前只数
`content_materials`。一份包声明 asset 之后，每个能提它的回合都会多出几条
`{"ref": …, "label": …}` —— **admission 从没算过它们**。

症状与那三次已经记在案的发作逐字相同：**封得了版、部署得上去，
然后某一个真实回合的 generate 请求超限，而每一道门禁都是绿的。**

## 三件

1. **配上 asset 之后那个阶段的账真的变大了** —— 不是「代码跑过了」，
   是那个数字动了。少了这一条，整段循环删掉也不会有东西红；
2. **挤爆时封版当场拒**，并说出是哪个阶段；
3. **标签的取法两边相等** —— 封版这边与 `select_assets` 各写一遍
   （跨包不 import），写两遍就得钉死。
"""

from __future__ import annotations

import re

import pytest
from fastagent_v2.profiles.registry import MATERIAL_BYTES_BUDGET, ProfilePackageError
from fastagent_v2.runtime.prompt_bytes import budgeted_asset_entry_cost
from fastagent_v2.worker.material_selection import select_assets

pytest.importorskip("fastagent_v2.profiles.registry")

STAGE = "01-开场"


def _frame(assets: list[dict[str, object]], materials: list[dict[str, object]]) -> dict:
    return {
        "id": "f1",
        "material_stages": [{"name": STAGE, "after": []}],
        "content_materials": materials,
        "content_assets": assets,
    }


def _stage_bytes(frame: dict, texts: dict[str, str]) -> int:
    """那个阶段被预留了多少字节 —— **从拒绝的报文里读回来**。

    `_check_stage_budget` 不回数字，它只在超了的时候抛并把数字写进报文。
    所以这里用一个**预算设成 0** 的旁路是不成的（常数是模块级的）；
    改用二分或读私有函数都比这更脆。

    做法：把一条巨大的话术塞进同一个阶段，让它必然超，
    报文里那个数字就是「这个阶段一共算了多少」。两次的差就是 asset 那部分。
    """

    from fastagent_v2.profiles.registry import _check_stage_budget  # noqa: PLC0415

    filler = "占" * (MATERIAL_BYTES_BUDGET // 3)
    frame = {
        **frame,
        "content_materials": [
            *frame["content_materials"],
            {"ref": "material:filler:v1", "availability": "proactive", "stages": [STAGE]},
        ],
    }
    try:
        _check_stage_budget(
            frame,
            texts={**texts, "material:filler:v1": filler},
            path="probe",
            frame_id="f1",
        )
    except ProfilePackageError as error:
        # **按阶段名锚定取**，不是「找第一个数字」。
        # 第一版是后者，而报文里先出现的是「topic 准备金（0 字节」——
        # 于是两次都读回 0，差值恒为 0，**这条用例会永远失败或永远通过，
        # 取决于期望值写成几**。锚点没命中就当场说出来，不返回一个看着像数的东西。
        found = re.search(rf"{re.escape(STAGE)} ([\d,]+) 字节", str(error))
        if found is None:
            raise AssertionError(f"报文里锚不到这个阶段的数字：{error}") from error
        return int(found.group(1).replace(",", ""))
    raise AssertionError("塞了一条巨大的话术却没超 —— 这条辅助函数的前提不成立")


def test_an_asset_makes_the_stage_account_bigger() -> None:
    """**那个数字真的动了。**

    **它与下面那条分工不同，而这一点是探针实测出来的。**

    我原本写的是「少了它，整段循环删掉下面那条照样会过」—— **那句话是错的**：
    下面那条只用 asset 不用话术，所以循环停掉它也会红。

    真正的分工在**量级**：下面那条只问「够不够挤爆」，
    而这一条问**那个数字是不是正好那么多**。把成本函数换成
    `budgeted_entry_cost`（每条少算 1 字节）时只有这一条会红 ——
    那 1 字节不改变「挤不挤得爆」，只改变账对不对。
    """

    without = _stage_bytes(_frame([], []), texts={})
    with_asset = _stage_bytes(
        _frame(
            [{"ref": "asset:price-list:v1", "availability": "proactive", "stages": [STAGE]}],
            [],
        ),
        texts={},
    )

    expected = budgeted_asset_entry_cost("asset:price-list:v1", "price-list")
    assert with_asset - without == expected, (
        f"配上一份文件，这个阶段的账只多了 {with_asset - without} 字节，"
        f"而那一条候选实际要 {expected} —— asset 那段循环没在算，或者算错了口径"
    )


def test_a_frame_whose_assets_overflow_the_stage_is_refused() -> None:
    """**挤爆时封版当场拒，并说出是哪个阶段。**

    这正是要的那句话：拒绝发生在封版，不是发生在某个真实回合。
    """

    many = [
        {
            "ref": f"asset:padding-{index:04d}:v1",
            "availability": "proactive",
            "stages": [STAGE],
        }
        for index in range(MATERIAL_BYTES_BUDGET // 60)
    ]

    from fastagent_v2.profiles.registry import _check_stage_budget  # noqa: PLC0415

    with pytest.raises(ProfilePackageError) as raised:
        _check_stage_budget(_frame(many, []), texts={}, path="probe", frame_id="f1")

    assert STAGE in str(raised.value), f"报文没说是哪个阶段：{raised.value}"


def test_the_label_admission_prices_is_the_label_the_turn_will_send() -> None:
    """**两边算的是同一个标签。**

    封版这边与 `select_assets` 各写一遍 slug 的取法（跨包不 import，
    与 `MATERIAL_BYTES_BUDGET` 同一条边界）。写两遍就得钉死相等 ——
    否则 admission 按一个短标签算账，而真实回合送出去的是另一个长的，
    **差额没有任何东西在看**。
    """

    reference = "asset:headcount-impact-onepager:v1"
    offered = select_assets(
        {
            "content_assets": [
                {"ref": reference, "availability": "proactive", "stages": [STAGE]}
            ],
            "material_stages": [{"name": STAGE, "after": []}],
        },
        understanding={},
    )

    assert len(offered) == 1
    sent_label = offered[0]["label"]
    priced_label = reference.split(":")[1]

    assert sent_label == priced_label, (
        f"真实回合送的标签是 {sent_label!r}，而 admission 按 {priced_label!r} 算账"
    )
