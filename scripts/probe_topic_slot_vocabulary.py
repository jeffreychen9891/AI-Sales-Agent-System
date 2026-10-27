#!/usr/bin/env python
"""反向探针：C-1 那条第五丢弃规则，逐条验它会红。

守的是 `tests/contracts/test_topic_slot_vocabulary.py`。
V4 / V5 是 2026-09-15「裁 B」加的：那份话题名单从 goal 的散文里删掉，
改由 `runtime.topic_vocabulary` 渲染进理解步的 `slots_wanted.allowed_values`。
**五支变异，各打一处**：

| 支 | 把什么改回去 | 谁该红 |
|---|---|---|
| **V1** | 整条第五规则删掉（回到 C-1 之前） | 执行者那一半 + 效果那一半 |
| **V2** | 去掉「空词表就不施加规则」那一判 | **该红** —— 复核补的那条守卫 |
| **V3** | goal 那句「不要累积」改回「可累积」 | **不该红** —— 证明「说明那一半没执行力」 |
| **V4** | 告知侧多说一个词表里没有的值（「裁 B」） | 告知==执行那条 |
| **V5** | 把那八个名字整份抄回 goal | 「不得有第二份清单」那条 |

**V2 从「该绿」翻成「该红」是复核 `26ad831c` 开口二的处置**：
在架配包词表非空，所以这一支原先绿得没错、但也证不了那个 `return None` 有用；
新那条用例自己造一个空词表的包，于是它现在红得准。

**V3 是这支探针的重点**：它变异的是**一句自然语言**，而三条用例**全绿**。
那正是用户 2026-09-15 那句「只做说明那一半，等于把一条约束交给一句话去执行」——
**这支探针是那句话的证据**。它绿得对，不是漏。

    python scripts/probe_topic_slot_vocabulary.py
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeError,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_topic_slot_vocabulary.py"

#: 按数断言，不按颜色。6 + 3 条参数化。
BASELINE_PASSED = 9

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1681

STACK = ROOT / "services/worker/src/fastagent_v2/worker/target_stack.py"
VOCAB = ROOT / "packages/runtime/src/fastagent_v2/runtime/topic_vocabulary.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"

TOUCHED = (STACK, VOCAB, PROFILE)

#: 第五条规则的判定那两行 —— harness 强制锚点命中数为 1。
FIFTH_RULE = (
    '        if topic_slot_vocabulary is not None and name == topic_slot_vocabulary[0]:\n'
    '            if str(update["value"]) not in topic_slot_vocabulary[1]:\n'
)

#: 「裁 B」的告知侧：`allowed_slot_values` 返回的那一行。V4 让它多说一个值。
TOLD = "    return {slot: sorted(vocabulary)}\n"

#: V5 往 goal 里抄回去的那份清单 —— 2026-09-15「裁 B」之前它原文就在那儿。
SECOND_COPY = (
    "concern_topic 只填这八类之一：团队的损耗 / 瞎答应 / 找真人 / 花钱 / "
    "查问题 / 能省几个人 / 我们这行能不能用 / 和以前那个有什么不一样。"
)

#: `topic_vocabulary` 里「空词表就不施加规则」那一判。
EMPTY_GUARD = "    if not vocabulary:\n        return None\n"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def reseal() -> None:
    """V3 / V5 的 `repin` —— 改完配包，**把章重新盖上**。

    `AGENTS.md` 那条「对内容寻址的产物必须连同地址一起重算」讲的正是这里，
    **而这支探针在 2026-09-16 之前没有用它**：V3 / V5 改的是
    `profiles/internal-demo-sales/profile.json`，改完 `identity.content_hash`
    就对不上了。本文件的用例不收集那条哈希守卫，所以在本文件里看不出来；
    对照一开就是 **84 failed / 11 errors**。

    **不自己算哈希**：`seal_profile_package.py` 的 docstring 逐字写着
    「有一份实现，另一个窗口去跑它，而不是自己算一遍」。这里照做。
    """

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [str(PYTHON), str(ROOT / "scripts" / "seal_profile_package.py"), str(PROFILE.parent)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ProbeError(
            "reseal failed after the mutation, so every later run would be measuring "
            f"the hash guard instead of the rule: {(completed.stderr or '').strip()}"
        )


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str], bool]] = [
        (
            "V1 · 删掉第五条丢弃规则（回到 C-1 之前）",
            [Mutation(STACK, FIFTH_RULE, '        if False:\n            if False:\n')],
            [
                "test_an_accumulated_value_is_refused",
                "test_the_offered_set_does_not_grow_with_the_conversation",
            ],
            [],
            False,
        ),
        (
            "V2 · 空词表也施加规则（去掉那个 `return None`）——**拦死，不是拦松**",
            [Mutation(VOCAB, EMPTY_GUARD, "")],
            # 只有自造空词表那条会红：在架配包词表非空，另外三条照绿。
            ["test_a_package_with_no_topic_materials_can_still_write_the_slot"],
            [],
            False,
        ),
        (
            "V3 · 只把 goal 那句「不要累积」改回「可累积」——**说明那一半没有执行力**",
            [Mutation(PROFILE, "**只填当前这一类，不要累积**", "可累积")],
            [],  # 故意期待「全绿」
            # **对照也必须是空的，否则这一支什么都没证明** —— 见 docstring。
            [],
            True,
        ),
        (
            "V4 · 告知侧多说一个词表里没有的值 —— **告知与执行分家**",
            [
                Mutation(
                    VOCAB,
                    TOLD,
                    '    return {slot: sorted({*vocabulary, "退款怎么算"})}\n',
                )
            ],
            ["test_the_model_is_told_exactly_what_the_code_enforces"],
            [],
            False,
        ),
        (
            "V5 · 把那八个名字整份抄回 goal —— **第二份清单又出现了**",
            [Mutation(PROFILE, "槽位四个。", "槽位四个。" + SECOND_COPY)],
            ["test_the_goal_no_longer_carries_a_second_copy_of_the_list"],
            [],
            True,
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED, control_passed=CONTROL_BASELINE_PASSED
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral, needs_reseal in probes:
            result = session.probe(label, mutations, repin=reseal if needs_reseal else None)
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            # **V3 期待的是「不红」** —— 所以这里判的是「命中期待」，不是「红了」。
            verdicts.append((label, bool(expects) == result.went_red, hit, elsewhere))
            want = "该红" if expects else "**该绿**"
            print(
                f"{label}\n  [{want}] {result.output}\n  命中期待：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    ok = all(shape and hit and other for _, shape, hit, other in verdicts)
    print("ALL AS EXPECTED" if ok else "SOMETHING BEHAVED DIFFERENTLY")
    if ok:
        print()
        print("**V3 全绿是这支探针最要紧的一行**，而它现在是全量意义上的全绿：")
        print("改一句 goal（并重新盖章），`tests/contracts` 一条都不动 ——")
        print("那就是「把一条约束交给一句自然语言去执行」的样子。执行者在代码里，不在 goal 里。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
