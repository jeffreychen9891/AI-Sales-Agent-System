"""契约要一个键，而提示词从没告诉过模型 —— 这一条把两边钉在一起。

## 现场（stage，2026-09-20 UTC，运维交接逐字）

部署 `64eedf7c` 之后**第一条真实客户消息**：

| 时刻 | 事件 |
|---|---|
| 10:52:02.423962 | 理解步调模型（`UNDERSTAND_VALUE` · `deepseek-v4-flash`）|
| 10:52:04.290479 | 该调用结算：2,566 in / 198 out · ¥0.003998 |
| 10:52:04.301108 | **判死** —— `policy_decisions` · `deny` · `PROVIDER_MALFORMED_OUTPUT` |

**计划步与生成步根本没被调用**，`outbox_messages` 该会话 0 行。
`PROVIDER_MALFORMED_OUTPUT` 在那个库里历史上只出现过这一次，就是这一轮。

**根因**：T200 把 `turn_summary` 加进了 `understanding-result` 的 `required`，
**而理解步的系统提示词既没有把它列进 `Required keys`，同一段还写着
`Do not add properties.`** —— 模型被**明确告知不要产**这个键，
校验器随后因为它缺席把整份结果判死。

## ⚠️ 门禁为什么全绿

**确定性替身自己补了这一句**（`inference.py` 里那个 `"turn_summary": …`），
于是所有走替身的用例都通过，**而真模型那条路上没有任何一条用例**。

这是本仓记过的两个形状叠在一起：

* 「contracts 全绿，不构成『这条用例量对了东西』的证据」——
  静态形状对、契约对，而「模型真的会产出这个键吗」只有跑一遍答得了；
* 「那条用例喂的东西必须是生产里会出现的形状」——
  替身产出的文档**两头都是我们自己写的**，所以没有任何一处会打架。

**本文件补的不是那条真链路用例**（那要跑真模型），
**而是把「契约要什么」与「提示词说了什么」这两份我们自己写的东西对上** ——
这一次的缺陷恰好整个落在这两份之间。

## 与 `PROMPT_RELEASE` 那两条守卫的关系

`test_prompt_release_moved_with_the_prompt` 与
`test_prompt_release_and_v4_replay_golden_move_together` 守的是
**「改了提示词要抬版本号」**。

**本条守的是反方向那一半：改了契约要改提示词。**
而在此之前那一半**一条守卫都没有** —— 所以那两条都没响。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from fastagent_v2.worker.inference_model import (
    _GENERATE_SYSTEM,
    _PLAN_SYSTEM,
    _UNDERSTAND_SYSTEM,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts" / "inference"
INFERENCE_MODEL = (
    ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_model.py"
)

#: 契约 ↔ 系统提示词，三对。
PAIRS = (
    ("understanding-result", _UNDERSTAND_SYSTEM),
    ("plan-result", _PLAN_SYSTEM),
    ("generation-result", _GENERATE_SYSTEM),
)

#: **平台自己写进去的那几个键 —— 模型不产，所以提示词也不该要求它产。**
#:
#: 这不是一张「豁免名单」，是 `inference_model.py` 模块 docstring 里
#: 已经写着的那四个（逐字）：「`schema_version`, `event_id`, `origin` and
#: `model_evidence` -- are written here from measured facts, never copied
#: from the model」。
#:
#: ⚠️ **它不能是一张可以随手加一行的名单** —— 那样下一个缺陷只要往这里
#: 补一个名字就消失了。所以下面 `test_every_platform_written_key_is_really_written`
#: 逐个回到源码里确认「确实有一处 `document[<键>] = …`」。
PLATFORM_WRITTEN_KEYS = frozenset(
    {"schema_version", "event_id", "origin", "model_evidence"}
)

_REQUIRED_KEYS_LINE = re.compile(r"Required keys:\s*(.+?)\.\s*$", re.DOTALL | re.MULTILINE)


def _contract_required(name: str) -> frozenset[str]:
    document = json.loads((CONTRACTS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return frozenset(document["required"])


def _prompt_required(prompt: str) -> frozenset[str]:
    """提示词里 `Required keys: a, b, c.` 那一行列出的键。

    ⚠️ **正则只取到第一个句点** —— 那一行后面紧跟着的是逐条规则，
    每条都以 `-` 开头，扫进来就会把 `interruption.relation` 这种也算成键。
    """

    match = _REQUIRED_KEYS_LINE.search(prompt)
    assert match is not None, "提示词里找不到 `Required keys:` 那一行"
    listed = match.group(1)
    assert "\n-" not in listed, f"`Required keys:` 那一行吃进了下面的规则：{listed!r}"
    return frozenset(key.strip() for key in listed.replace("\n", " ").split(",") if key.strip())


def test_the_prompt_asks_for_every_key_the_contract_requires() -> None:
    """**契约必填 − 提示词说的 == 平台自己写的那几个**，三份契约逐份。

    判据写**集合相等**不写包含（本仓「计数类断言写等值，不要写上界」）：
    写包含的话，「提示词多说了一个契约不要的键」与
    「契约多要了一个提示词没说的键」里只有前一种会红，
    **而这一次是后一种。**

    ⚠️ 它红的时候先分清是哪一侧动了：

    | 差集落在 | 发生了什么 | 怎么改 |
    |---|---|---|
    | 契约要、提示词没说 | **就是本文件那个缺陷** | 去 `_*_SYSTEM` 把它写上，并抬 `PROMPT_RELEASE` |
    | 提示词说了、契约不要 | 让模型产一个没人校验的键 | 要么进契约，要么从提示词里删掉 |
    """

    for name, prompt in PAIRS:
        required = _contract_required(name)
        listed = _prompt_required(prompt)

        # **守卫不能是空的**：两侧都得真的解析出东西来。
        assert len(required) >= 7, f"{name} 的 required 只解析出 {sorted(required)}"
        assert len(listed) >= 7, f"{name} 的提示词只解析出 {sorted(listed)}"

        unspoken = required - listed
        assert unspoken == PLATFORM_WRITTEN_KEYS & required, (
            f"{name}：契约要而提示词没说的是 {sorted(unspoken)}，"
            f"而平台自己写的只有 {sorted(PLATFORM_WRITTEN_KEYS & required)}。"
            "多出来的那些键，模型不会知道要产 —— "
            "而校验器会因为它缺席把整份结果判死（stage 2026-09-20 实测）"
        )

        invented = listed - required
        assert invented == frozenset(), (
            f"{name}：提示词让模型产 {sorted(invented)}，而契约不要它们 —— "
            "一个没人校验的键，模型产什么都行"
        )


def test_every_platform_written_key_is_really_written() -> None:
    """**那四个键不是一张名单，是四处赋值。**

    没有这一条，上面那条的豁免集就是一张可以随手加一行的名单：
    下一次「契约要了而提示词没说」，只要把那个键塞进
    `PLATFORM_WRITTEN_KEYS`，缺陷就安静地消失了 ——
    **而它在真模型那条路上照样发生。**

    判据是 AST：在 `inference_model.py` 里找得到 `document[<键>] = …`。
    """

    tree = ast.parse(INFERENCE_MODEL.read_text(encoding="utf-8"))
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "document"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                assigned.add(target.slice.value)

    # 扫描退化成 0 命中时，下面那句本来就会红（`missing` 会是全集）。
    # 这一句只为**把「扫到了什么」写进报错**：
    # 「四个键一个都没赋值」与「这段 AST 扫描瞎了」是两件事，
    # 而它们在同一条 `missing` 上长得一模一样。
    assert assigned, "这段 AST 扫描一处 `document[...] = ...` 都没找到"
    missing = PLATFORM_WRITTEN_KEYS - assigned
    assert missing == frozenset(), (
        f"{sorted(missing)} 说是平台写的，而 `inference_model.py` 里没有一处给它赋值 —— "
        "那它既没人告诉模型、也没人补上，整份结果会被判死"
    )


def test_the_example_shows_every_key_it_asks_for() -> None:
    """**示例 JSON 与 `Required keys` 那一行必须对得上。**

    两处各写一遍同一件事，而模型照着抄的往往是示例。
    T200 那次两处一起漏了，**所以只钉一处是不够的**。
    """

    for name, prompt in PAIRS:
        listed = _prompt_required(prompt)
        # ⚠️ 三份提示词里这句话的**换行位置不同**：理解步是
        # `Example JSON output:` 一行写完，而计划步与生成步在
        # `Example` 与 `JSON output:` 之间换了行。
        # 计划步那份连措辞都不一样：它后面接的是
        # `JSON output for a conservative no-side-effect plan:`。
        # 所以锚到 `Example … JSON output` 为止，**后面那半句由它自己去写**。
        # ⚠️ 这不是把判据放宽：锚点之后整段示例照样逐键核。
        marker = re.search(r"Example\s+JSON output", prompt)
        assert marker is not None, f"{name} 的提示词里找不到示例"
        example = prompt[marker.end() :]
        for key in sorted(listed):
            assert f'"{key}"' in example, (
                f"{name}：`Required keys` 里有 {key}，而示例 JSON 里没有它"
            )


def test_the_understanding_prompt_asks_for_turn_summary() -> None:
    """**这一次那个键，单独钉一条。**

    上面三条是通用判据，而它们通用到读起来不像一次事故。
    这一条对着 stage 那一轮：`turn_summary` 必须在理解步的提示词里，
    **而且不只出现在示例里** —— 模型要知道它是必填的。
    """

    assert "turn_summary" in _prompt_required(_UNDERSTAND_SYSTEM)
    # 还要有一句说它是什么，否则模型只知道键名，不知道往里写什么
    # ⚠️ **2026-09-23（运维缺陷 16）措辞改了，而改的是它的要害。**
    # 原文要「what he wanted **and what we did**」—— 而本字段由**理解步**产出，
    # 那一刻回复还不存在。**后半句在结构上不可能答对**，模型只能猜，
    # 而它猜了「本轮未给出实质回答」，**回答就在同一轮的第一条**。
    # 那句猜测进了滚动摘要，被后面每一轮读到。
    assert "what he said and what he wants" in _UNDERSTAND_SYSTEM
    assert "the reply does not exist yet" in _UNDERSTAND_SYSTEM, (
        "提示词没说清「回复还不存在」—— 少了这半句，模型仍然会去写它猜的结果"
    )
    # ⚠️ **判据不能写成「`what we did` 这几个字不许出现」** —— 第一版就是那么写的，
    # 而它命中的是**新禁令自己那句**（`never say what we did`）。
    # 一个只问「某几个字在不在」的判据，分不出「在教它写」与「在禁止它写」。
    assert "never say what we did" in _UNDERSTAND_SYSTEM, (
        "没有那条禁令 —— 少了它，模型仍然会去写它猜的结果"
    )
    assert "one line on this turn only" not in _UNDERSTAND_SYSTEM, (
        "旧那句「one line on this turn only -- what he wanted and what we did」还在"
        " —— 后半句是它答不了的"
    )


def test_do_not_add_properties_is_still_there() -> None:
    """**`Do not add properties.` 那一句留着，而它正是这次缺陷的另一半。**

    本条不是为了守那句话本身，是为了让读到上面几条的人明白
    **为什么「提示词没说」等于「模型不会产」**：
    同一段里明确禁止了自作主张加键。

    ⚠️ 它哪天被删掉，上面那条判据的**严重性**就变了
    （模型也许会凭 schema 猜出来），而判据本身仍然成立 ——
    所以这一条红了不必慌，去读上面那条的表就行。
    """

    assert "Do not add properties." in _UNDERSTAND_SYSTEM
