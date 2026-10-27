"""资料等的那个话题，槽位必须真取得到 —— 否则它**一次都不会被递出去**。

## 这条补的洞

话题槽的合法取值是从**话术**的 `topics` 推出来的（`topic_vocabulary` 读的是
`content_materials`），而它同时是模型被告知的「这个槽只收这些值」——两者同源。

所以一个**只被资料用过**的话题，此前：

* 封版**通过**（那边只检查「帧有没有话题槽」，不检查取值）；
* 而模型永远填不进那个值，`_is_offered` 的 `any(topic in topic_text)` 永远不成立；
* **那份资料一次都不会被递出去。**

**封得了版、部署得上去、永远不触发，而没有任何东西会红。**
发现于 2026-09-17 写内部演示租户的资料草案时。

## 三个方向

1. **词表里的值照旧放行** —— 正向那条。少了它，把这道闸写成「一律拒」
   也会让另外两条过，而那会让所有 `on_request` 资料都封不了版；
2. 词表外的值当场拒，**并把词表列出来**；
3. 帧声明了槽但**一条话术都没带 topics** —— 词表是空的，
   这时任何值都取不到，另给一条说得清的理由。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastagent_v2.profiles.registry import ProfilePackageError, _check_material_availability


def _document(asset_topics: list[str], material_topics: list[str] | None) -> dict[str, Any]:
    """一个最小的包：一条话术、一份资料、一个话题槽。"""

    material: dict[str, Any] = {"ref": "material:m1:v1"}
    if material_topics is not None:
        material["availability"] = "on_request"
        material["topics"] = material_topics
    return {
        "target_frames": [
            {
                "id": "introduce",
                # **话题槽必须是这个帧自己的一个信息槽** —— 另一条更早的闸。
                # 第一版漏了它，正向那条用例以「the value would always be empty」
                # 被拒 —— 也就是**它当时红的不是本文件要守的东西**。
                "information_slots": [
                    {
                        "name": "concern_topic",
                        "type": "string",
                        "required": True,
                        "description": "Which kind of loss he cares about this turn.",
                    }
                ],
                "material_topic_slot": "concern_topic",
                "content_materials": [material],
                "content_assets": [
                    {
                        "ref": "asset:pricing-sheet:v1",
                        "availability": "on_request",
                        "topics": asset_topics,
                    }
                ],
            }
        ]
    }


def test_a_topic_the_wording_already_uses_is_accepted() -> None:
    """**正向那条。**

    少了它，把这道闸写成「一律拒」也能让下面两条通过 ——
    而那会让每一份 `on_request` 资料都封不了版。
    """

    _check_material_availability(_document(["花钱"], ["花钱", "找真人"]), path="probe")


def test_a_topic_outside_the_vocabulary_is_refused_with_the_vocabulary_named() -> None:
    """词表外的值当场拒，**并把词表列出来**。

    报文要答的是「那我该写什么」—— 只说「不合法」会让作者去翻代码，
    而这张词表是从他自己的话术推出来的，他未必知道它在哪。
    """

    with pytest.raises(ProfilePackageError) as raised:
        _check_material_availability(_document(["数据安全"], ["花钱", "找真人"]), path="probe")

    detail = str(raised.value)
    assert "数据安全" in detail, "报文没说是哪个话题不可达"
    assert "花钱" in detail and "找真人" in detail, (
        f"报文没把词表列出来，作者读完还是不知道该写什么：{detail}"
    )


def test_a_frame_whose_wording_carries_no_topics_at_all_is_refused_differently() -> None:
    """**词表是空的**那一种，另给一条理由。

    帧声明了话题槽，而一条话术都没带 `topics` —— 这时词表是空的，
    任何值都取不到。与「写错了一个值」混成同一条报文会让作者去改那个值，
    而真正要做的是**先给话术标 topics**。
    """

    with pytest.raises(ProfilePackageError) as raised:
        _check_material_availability(_document(["花钱"], None), path="probe")

    detail = str(raised.value)
    assert "no material of this frame carries" in detail, (
        f"这一种该有它自己的理由，而不是复用「不可达」那条：{detail}"
    )
