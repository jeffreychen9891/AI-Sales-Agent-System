"""**一轮只认一类话题** —— 而这份文件上一版钉的是它的反面。

## 它换掉了什么，以及为什么留着这段话

上一版叫 `test_topic_gate_is_still_per_turn_open.py`，是一根**钉住缺陷**的绊线：
它断言「一轮对同一个槽发多条 `slot_update`，与 C-1 禁掉的累积串逐字节等价」，
并在报错里写着「红了就去关 T274 / T351」。

**它红了，因为那个洞被堵上了**（用户 2026-09-16 裁定：一轮只认最后一条）。
那正是它设计出来的作用，所以这份文件换成钉住**修好之后的那个形状**。
**不删这段来由**：下一个读到 `ideas.md` 里 T351 / T274 的人，
需要知道那两条描述的状态是什么时候、被哪一笔改掉的。

## 今天钉四件

1. **一轮多发几条 update，只有最后一条算数**；
2. 因此**多发与只发一条给出同一个结果** —— 那个逐字节等价的洞不在了；
3. **被丢掉的那几条有人数着**（`superseded_topic_updates`），
   而 `pipeline` 拿这个数落一行 `policy_decisions`：
   **一次安静的丢弃只是把旧缺陷换成新缺陷**；
4. 单个话题那一格仍然是一位数量级 —— T351 重写时量的 8–11 段。

## 它与 `test_topic_slot_vocabulary.py` 的分工没变

那边走真实写回（`apply_slot_updates`），钉的是**跨回合**不累积；
这边钉**一个回合之内**那条不经过写回的路。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from fastagent_v2.worker.material_selection import (
    MATERIAL_BYTES_BUDGET,
    select_materials,
    superseded_topic_updates,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "profiles" / "internal-demo-sales"
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"


def _frame_and_texts() -> tuple[dict[str, Any], dict[str, str]]:
    document = json.loads((PACKAGE / "profile.json").read_text(encoding="utf-8"))
    sidecar = json.loads((PACKAGE / "materials.json").read_text(encoding="utf-8"))
    texts: dict[str, str] = dict(sidecar["materials"])
    # ⚠️ **2026-09-21：拆帧之后有三个帧声明了 `material_topic_slot`**，
    # 而本文件要的是**话题最多的那一个**（八类疑虑，在 `objection`）——
    # 「一轮只算一个话题」这条对比，两类话题撑不起来。
    frame = max(
        (f for f in document["target_frames"] if f.get("material_topic_slot")),
        key=lambda f: len({t for m in f.get("content_materials", [])
                           for t in m.get("topics", [])}),
    )
    declared = {
        str(declaration["ref"]): texts[str(declaration["ref"])]
        for declaration in frame.get("content_materials", [])
        if str(declaration.get("ref")) in texts
    }
    return frame, declared


def _topics(frame: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for declaration in frame.get("content_materials", []):
        for topic in declaration.get("topics") or ():
            if str(topic) not in seen:
                seen.append(str(topic))
    return seen


def _understanding(frame: dict[str, Any], values: list[str]) -> dict[str, Any]:
    slot = str(frame["material_topic_slot"])
    return {"slot_updates": [{"slot": slot, "value": value} for value in values]}


def _turn(
    frame: dict[str, Any],
    declared: dict[str, str],
    *,
    updates: list[str],
) -> tuple[int, int]:
    """这一轮给出去几段、多少字节。

    ⚠️ **`stored` 那个参数 2026-09-18 去掉了。** 话术的 `on_request` 判据
    从「槽位现值」改成「**这一轮问了**」（用户裁定），
    `select_materials` 连 `frame` 都不再收 —— 一个存着的值从此开不了这道门。
    """

    selected = select_materials(
        definition=frame,
        declared=declared,
        emit_kind=None,
        understanding=_understanding(frame, updates),
    )
    return len(selected), sum(len(text.encode("utf-8")) for text in selected.values())


def test_only_the_last_update_of_the_turn_counts() -> None:
    """**多发几条与只发最后那一条，结果相同。**

    这是这一批改的那一行：在此之前每一条都接上去，
    于是一轮发七条与 C-1 禁掉的累积串**逐字节等价**（实测 25 段 / 4,233 字节）。

    判据是「多发 == 只发最后一条」而不是某个具体的数 ——
    数会随配包改，而这个等式是这次收紧的内容本身。
    """

    frame, declared = _frame_and_texts()
    topics = _topics(frame)
    assert len(topics) >= 4, f"只有 {len(topics)} 类话题，撑不起这条对比"

    many = _turn(frame, declared, updates=topics[1:])
    only_last = _turn(frame, declared, updates=[topics[-1]])

    assert many == only_last, (
        f"发 {len(topics) - 1} 条给出 {many}，只发最后一条给出 {only_last} —— "
        "前面那几条又算数了，那个洞回来了（T351 / T274 的来由）"
    )


def test_an_accumulated_string_in_one_update_lights_nothing() -> None:
    """**累积串走 update 这条路，从 2026-09-18 起一段话题素材都点不亮。**

    ## 这条用例换掉了哪一条，以及为什么

    上一版叫 `test_the_accumulated_string_is_no_longer_reachable_through_updates`，
    比的是「多发几条 update 那一轮」与「**槽里存着累积串**那一轮」不相等。
    它那时成立，靠的是 C-1：累积串写不进槽。

    **而那个对比在本批之后没有意义了** —— 话术根本不看槽位现值。
    于是真正要问的变成：**把累积串塞进一条 update 会怎样？**

    ## 本批当场量出来的：在这一行之前它两组一起点亮

    ```python
    topic_text(definition, frame={"slots": {}}, understanding={"slot_updates": [
        {"slot": "concern_topic", "value": "已覆盖：花钱；当前：查问题"}]})
    # → '已覆盖：花钱；当前：查问题'    两组素材一起点亮
    ```

    三道闸各关了一半，而这半没人关：**C-1** 关存储那条路、
    **T351 / T274** 关「一轮多发几条」，
    而「**一条 update 里塞一个累积串**」在此之前没有任何东西拦 ——
    它从前藏在「槽位现值」后面，**本批把话术改成只看这一轮那半之后，
    它成了唯一那条路**。

    修法是让这一轮那条 update 也过一遍词表
    （`runtime.topic_vocabulary`，与 `apply_slot_updates` 同源）。
    """

    frame, declared = _frame_and_texts()
    topics = _topics(frame)

    by_updates = _turn(frame, declared, updates=topics[1:])
    accumulated = "已覆盖：" + "、".join(topics[:-1]) + "；当前：" + topics[-1]
    by_string = _turn(frame, declared, updates=[accumulated])
    nothing_asked = _turn(frame, declared, updates=[])

    assert by_string == nothing_asked, (
        f"累积串那一轮给出 {by_string}，而一个话题都没问那一轮给出 {nothing_asked} —— "
        "那个串又点亮了话题素材，词表那一道没拦住"
    )
    assert by_updates[0] > by_string[0], (
        f"问一个合法话题给出 {by_updates[0]} 段，而累积串给出 {by_string[0]} 段 —— "
        "合法的那条应当更多，否则这条用例证不了东西"
    )


def test_the_discarded_updates_are_counted() -> None:
    """**丢弃出声**：被丢掉的那几条有人数着。

    一次安静的丢弃只是把旧缺陷换成新缺陷 —— 模型多发了，平台少认了，
    而没有任何地方记下这件事发生过。

    **只数条数，不回那些值**（2026-09-16 那次脱敏的教训）：
    定位这件事不需要值，而留痕会被人贴到别处。
    """

    frame, _ = _frame_and_texts()
    topics = _topics(frame)

    assert superseded_topic_updates(frame, understanding=_understanding(frame, [])) == 0
    assert superseded_topic_updates(frame, understanding=_understanding(frame, topics[:1])) == 0
    assert superseded_topic_updates(frame, understanding=_understanding(frame, topics[:4])) == 3
    assert superseded_topic_updates({}, understanding=_understanding(frame, topics)) == 0


def test_the_pipeline_records_the_discard() -> None:
    """**数出来的那个数要有人写下来**，否则它只是一个没人读的返回值。

    判据是源码结构：`pipeline` 调 `superseded_topic_updates`，
    并在同一段里用 `EXTRA_TOPIC_SLOT_UPDATES_DISCARDED` 落一行留痕。

    结构断言不好看，而替代品是跑一整个真实回合 ——
    那一层的用例（`tests/integration/`）要真库、要模型，
    它们验的是别的东西。**这一条至少让「接线被删掉」会红。**
    """

    source = PIPELINE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    called = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "superseded_topic_updates"
    ]
    assert called, "pipeline 不再调用 superseded_topic_updates —— 丢弃又变安静了"

    assert "EXTRA_TOPIC_SLOT_UPDATES_DISCARDED" in source, (
        "那个 reason_code 不在 pipeline 里了 —— 丢弃没有留痕，"
        "而留痕正是这次收紧与「安静地少认一条」的全部区别"
    )


def test_one_topic_a_turn_is_still_the_small_case() -> None:
    """每个话题桶都还有话可说，而没有哪个话题把一轮塞满。

    **2026-09-17 重设过下界**，理由写在断言旁边：那次裁定把素材收到关键节点，
    每桶从 8–11 段变成 1–3 段。上界没动。
    """

    frame, declared = _frame_and_texts()
    for topic in _topics(frame):
        # ⚠️ 「客户问到这一类」写成**这一轮的 update**（2026-09-18 裁定）：
        # 上一版写的是 `stored=topic` —— 一个存着的值，而它从此开不了这道门。
        # **产品不变量一个字没变**：每个话题桶还有话可说、没有哪个桶把一轮塞满。
        count, size = _turn(frame, declared, updates=[topic])
        # **下界 2026-09-17 从 5 降到 1，上界原样留着。**
        #
        # 那个 5 来自 147 段语料的密度（当时每桶 8–11）。用户裁定
        # 「素材只做关键节点」之后每桶是 1–3 段 —— 而**真正的产品不变量
        # 不是「每桶有几段」，是「每个话题桶还有话可说」**：
        # 一个点亮 0 段的桶意味着客户问到那一类时，商家审过的话一条都没有。
        #
        # **上界 14 一个字没动**：它守的是「一个话题把一轮塞满」，
        # 与语料多大无关，而那正是这条用例当初存在的理由。
        assert 1 <= count <= 14, (
            f"单个话题「{topic}」点亮 {count} 段 —— "
            "0 段意味着这个桶没有商家审过的话，14 段以上意味着一个话题塞满了一轮"
        )
        assert size < MATERIAL_BYTES_BUDGET // 4, (
            f"单个话题「{topic}」{size:,} 字节 —— T351 重写时它是 1,763–1,967"
        )
