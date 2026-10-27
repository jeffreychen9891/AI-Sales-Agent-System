"""`interruption.relation` 那六个词，各按后果教了一句（ADR-0147）。

## 在此之前它只有一行枚举

```
- interruption.relation is one of: same_goal, side_question, new_goal,
  correction, exit, human_request.
```

**六个词，一个解释都没有。** 而它们不是标签 —— 每一个都会**安静地改变平台
接下来能做什么**：

| 词 | 它实际决定了什么 |
|---|---|
| `side_question` / `new_goal` / `correction` | **只有这三个能开子帧**（`PUSH_RELATIONS`）|
| `correction` | **解锁覆盖一个槽位** —— RFC 4.3 本来会按置信度拒掉那次改写 |
| `exit` / `human_request` | **留痕**、**是放弃目标的唯一依据**、**停掉主动跟进** |

**这正是 `CLAUDE.md` 第三条点名的形状**：传错了不报错，
而是安静地给出一个看起来正常的结果 —— 能建、能过合同、能返回 200，
**只有行为是错的**。

## ⚠️ 这份钉的是「教了什么」，不是「模型判得对不对」

后者要一次真实对话，判据在运维那边。这里能量的是
**这套配置有没有在要求它这么做** —— 而 2026-09-21 之前它什么都没要求。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastagent_v2.worker.inference_model import _UNDERSTAND_SYSTEM, PROMPT_RELEASE
from fastagent_v2.worker.inference_validation import PUSH_RELATIONS
from fastagent_v2.worker.target_stack import EXIT_SIGNAL_RELATIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (
    REPOSITORY_ROOT
    / "packages"
    / "contracts"
    / "inference"
    / "understanding-result.schema.json"
)
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)


def _declared_relations() -> list[str]:
    schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return list(schema["properties"]["interruption"]["properties"]["relation"]["enum"])


def test_every_word_the_contract_allows_is_explained() -> None:
    """**契约允许几个词，提示词就要解释几个** —— 这是本文件的第一条腿。

    ⚠️ 它红的时候多半是有人往枚举里加了一个词而没告诉模型那个词是什么意思。
    **那种缺陷是安静的**：schema 过得去、管线跑得通，
    而模型面对一个没人解释过的词只能猜。
    """

    for relation in _declared_relations():
        assert f"`{relation}`" in _UNDERSTAND_SYSTEM, (
            f"契约允许 {relation!r} 而提示词没有解释它 —— 模型只能猜，而猜错是安静的"
        )


def test_the_contract_still_declares_exactly_those_six() -> None:
    """⚠️ **上面那条读的是契约，这一条钉住契约本身没被悄悄改。**

    没有这一条，删掉枚举里的五个词、只留一个，上面那条照样绿。
    """

    assert _declared_relations() == [
        "same_goal",
        "side_question",
        "new_goal",
        "correction",
        "exit",
        "human_request",
    ], f"那六个词变了：{_declared_relations()}"


def test_the_three_that_open_a_child_frame_are_named_as_such() -> None:
    """**只有三个词能开子帧，而提示词要说出是哪三个。**

    判据从 `PUSH_RELATIONS` 现读 —— **不是抄一份常量来核常量**：
    有人往那个元组里加一个词时，这一条会红并指着提示词说「你没教这个」。
    """

    assert set(PUSH_RELATIONS) == {"side_question", "new_goal", "correction"}, (
        f"能开子帧的那几个变了：{PUSH_RELATIONS} —— 提示词那一段要跟着改"
    )
    assert "child frame" in _UNDERSTAND_SYSTEM, (
        "提示词没说哪个词能开子帧 —— 模型不知道 `side_question` 与 `same_goal` 的区别在哪"
    )


def test_correction_is_told_that_it_unlocks_a_slot_overwrite() -> None:
    """⚠️ **`correction` 不只是一个标签，它解锁覆盖槽位。**

    RFC 4.3 会按置信度拒掉一次改写，而 `is_correction=True` 让那条规则让路
    （`pipeline.py` 的 `apply_slot_updates(is_correction=…)`）。
    **模型不知道这件事的话，它会把「他补充了一句」也判成 correction** ——
    而那会让一个低置信的值覆盖掉一个高置信的值，**没有任何东西会红**。
    """

    # ⚠️ **不能只问「文件里有没有这个串」** —— 它有**两处**：
    # 接管那一路一处、正常那一路一处。探针 E 把正常那一处换成 `False` 时，
    # 接管那处仍在，**这一条照样绿** —— 那是一条空判据。
    # 改成读 `is_correction=` 传进去的到底是什么。
    source = PIPELINE.read_text(encoding="utf-8")
    wired = [
        block.split(",")[0].strip()
        for block in source.split("is_correction=")[1:]
    ]
    assert len(wired) == 2, f"`is_correction=` 的传入点不是两处了：{len(wired)}"
    assert all('== "correction"' in expression for expression in wired), (
        f"有一处不再按 `correction` 传了：{wired} —— 提示词那一句要跟着改"
    )
    lowered = _UNDERSTAND_SYSTEM
    assert "unlocks overwriting a slot" in lowered or "overwrit" in lowered, (
        "提示词没说 `correction` 会解锁覆盖槽位 —— 模型会把「补充一句」也判成它"
    )


def test_the_two_durable_ones_are_marked_as_durable() -> None:
    """⚠️ **`exit` / `human_request` 判错的代价最大，所以单独点名。**

    它们三件事同时发生：**留痕**（`pending_exit_signal`）、
    **成为放弃目标的唯一依据**、**停掉主动跟进** ——
    而三件里没有一件会红。

    所以提示词里要有一句反例：**「你们怎么这么慢」不是 `exit`。**
    """

    assert set(EXIT_SIGNAL_RELATIONS) == {"exit", "human_request"}, (
        f"退出信号那两个变了：{EXIT_SIGNAL_RELATIONS}"
    )
    assert "durable" in _UNDERSTAND_SYSTEM, "提示词没说这两个是会留痕的"
    assert "ordinary impatience" in _UNDERSTAND_SYSTEM, (
        "没有那句反例 —— 「你们怎么这么慢」会被判成 exit，而那会停掉主动跟进"
    )


def test_asking_again_is_told_to_stay_and_answer_briefly() -> None:
    """**他又问起聊过的事 → `same_goal`，留在当前帧用自己的话简答。**

    用户 2026-09-21 裁定。ADR-0146 给了 `brief_answer` 的帧一道护栏
    （再 `push` 会被拒、那一轮落到可能静默的兜底上），
    **而判成什么仍然是模型的事** —— 这一句是那道护栏的另一半。
    """

    assert "asks again" in _UNDERSTAND_SYSTEM, (
        "提示词没交代「他又问起聊过的事」该判哪个词 —— "
        "判成 side_question 就会去 push 一个进过的帧，然后那一轮可能静默"
    )
    assert "briefly" in _UNDERSTAND_SYSTEM


def test_the_release_moved_with_this_teaching() -> None:
    """改了提示词就要抬版本号。"""

    assert PROMPT_RELEASE == "m1a-prompt-v17"
