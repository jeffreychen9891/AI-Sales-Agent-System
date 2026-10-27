"""话题槽的**合法取值**从哪来 —— 只有这一个推导点。

## 它为什么在 `packages/runtime` 而不在 worker

**因为有两方要读同一个词表，而那两方之间不许互相 import：**

* **生成侧**（`worker.target_stack.apply_slot_updates`）用它**执行** ——
  不在词表里的值整条丢弃；
* **告知侧**（`runtime.request_shape.understand_prompt`）用它**告诉模型** ——
  理解步收到的 `slots_wanted` 里带上这个槽的 `allowed_values`；
* **admission**（`profiles.registry`）算固定底时走的是同一个 `understand_prompt`，
  所以它也要能算出同一份词表。

`services/api` 不能 import `services/worker`（`tests/contracts/
test_deployment_root_and_catalog_gate.py` 守着，部署时 api 镜像里没有 worker 包），
**所以共享的这一份只能落在这里** —— 与 `prompt_bytes`、`request_shape` 同一个理由。

## 这个模块是 2026-09-15「裁 B」的兑现物

在此之前，那八个话题名字**在配包里存在两份**：`content_materials[].topics` 一份，
`goal` 的散文里「`concern_topic` 只填这八类之一：…」又一份。
**两份就会漂**，而漂了之后没有任何东西会红 —— 独立复核 `26ad831c` 实测过：
往 goal 里加第九类、不给它素材，五份相关用例 96 条一条不红，
**而模型会照 goal 去填那一类，每一条都被第五条丢弃规则整条丢掉**。

**修法不是给那份散文加一条正则去核对**（那正是本仓记过的
「用写死七个名字的正则去找那七个名字」那个形状，CLAUDE.md 第三节），
**而是让第二份不存在**：名字只留在素材声明里，由引擎渲染进提示词。

## 它**没有**做到的那一半，写在这里免得自审写宽

「goal 的散文里提到一个没有素材的话题」**今天仍然不会变红**。
本模块改的是**后果**不是**可检测性**：模型每一轮收到的合法集合由素材现推，
所以它不会去填那个没素材的类，**「静默丢数据」这个失败方式没有了**；
而那句散文本身仍然只是一句散文。

**要把它变成会红的东西，只有两条路，两条都已被否**：解析 goal 散文
（写死措辞的正则，失效时静默变空），或另立一张声明表（D2，用户已否）。
**所以这一条作为残账留着，而不是假装被 B 修掉了。**
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 帧上那个键：哪一个 information slot 装着「客户现在关心什么」。
#: 声明了 `topics` 的 `on_request` 素材就是拿这个槽的值去匹配的。
#:
#: **规则（用户 2026-09-15 裁定）：一个话题槽的合法取值 = 有素材对应的话题。**
#: 没有素材对应的话题，模型写进去也没有意义 —— **它不会让任何东西被给出去，只占字节**。
#: 所以取值全集**就是本帧 `content_materials[].topics` 的键集**，**不另立一张表**。
#: 想让一个话题合法，就给它写一段素材。
#: 全文与那次实测见 `merchant-profile.schema.json` 的 `material_topic_slot`。
TOPIC_SLOT_KEY = "material_topic_slot"


def declarations(definition: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """`content_materials` normalised to one shape, whichever way it was written.

    A bare string and `{"ref": …}` are the same declaration; the string form is
    what every profile used before availability existed and stays valid, so that
    adopting this is a per-package decision rather than a flag day.

    **它在这里而不在 `worker.material_selection`，是因为词表推导要用它** ——
    而词表推导要被 `services/api` 读到。`material_selection` 仍然以它原来的名字
    re-export，所有旧的 import 点不动。
    """

    normalised: list[dict[str, Any]] = []
    for item in definition.get("content_materials") or []:
        if isinstance(item, str):
            normalised.append({"ref": item})
        elif isinstance(item, Mapping):
            normalised.append(dict(item))
    return tuple(normalised)


def topic_vocabulary(
    definition: Mapping[str, Any],
) -> tuple[str, frozenset[str]] | None:
    """这个帧的话题槽叫什么、它的合法取值有哪些 —— **现推，不另立表**。

    返回 `(槽位名, 取值集合)`；帧没声明话题槽、或一段带 `topics` 的素材都没有时
    返回 `None`（**那时这条规则整个不施加** —— 一个空词表会把所有值都判非法，
    而「配包没声明」与「配包声明了槽但素材还没标 topics」是两件事，后者是配到一半）。

    那个 `return None` 由 `test_a_package_with_no_topic_materials_can_still_write_the_slot`
    钉住（独立复核 `26ad831c` 开口二：先前它没有守卫，删掉之后那种包的话题槽
    **永远写不进任何值**，而不是「拦得松了」）。
    """

    slot = definition.get(TOPIC_SLOT_KEY)
    if not isinstance(slot, str) or not slot:
        return None
    vocabulary = {
        str(topic)
        for declaration in declarations(definition)
        for topic in (declaration.get("topics") or ())
        if str(topic)
    }
    if not vocabulary:
        return None
    return slot, frozenset(vocabulary)


def allowed_slot_values(definition: Mapping[str, Any]) -> dict[str, list[str]]:
    """喂给 `request_shape.understand_prompt` 的那张「这个槽只收这些值」表。

    今天只有话题槽这一种，所以它要么是空的、要么正好一项。
    **返回排过序的 list**：集合的迭代顺序在进程之间不稳定，而这张表进 payload ——
    顺序一变字节不变但**内容的 diff 会变**，固定底也就不可复现。

    **调用方两处、推导只有这一处**，这正是「裁 B」要的那件事：
    模型被告知的集合与 `apply_slot_updates` 执行的集合**同源**，
    由 `test_the_model_is_told_exactly_what_the_code_enforces` 钉住。
    """

    found = topic_vocabulary(definition)
    if found is None:
        return {}
    slot, vocabulary = found
    return {slot: sorted(vocabulary)}


__all__ = [
    "TOPIC_SLOT_KEY",
    "allowed_slot_values",
    "declarations",
    "topic_vocabulary",
]
