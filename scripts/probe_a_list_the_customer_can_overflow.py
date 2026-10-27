#!/usr/bin/env python
"""反向探针：`frames_entered` 那个上界的四条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **A1** | 收尾那个 `while` 改成 `while False` = 修复前 | 「话多的客户」+「还记得进过」|
| **A2** | 到顶时丢最旧的一笔，而不是最旧的那笔**重复**记录 | 「进过没有」那两条 |
| **A3** | 代码里的上界改成 32 | 「一个数」那条 |
| **A4** | 配包那格 `maxItems` 抬到 64（= 不给平台帧留位） | 「留位」那条 |

**A2 是这四条里最要紧的一条。** 「丢最旧的一笔」与「丢最旧的那笔重复记录」
在「对话还写得进去吗」这个问题上**给出一模一样的答案**——两种写法 A1 那条都绿。
它们分家的地方只有一处：**被挤掉的那个帧还算不算进过**。
所以这一支要是绿的，说明本批真正的那个判据今天没人钉，**绿的那一支才是产出**。

## 跑法

本批的守卫都在 `tests/contracts`，不连库，所以直接跑得起来：

```sh
FASTAGENT_PYTHON=.venv/Scripts/python.exe .venv/Scripts/python.exe \\
    scripts/probe_a_list_the_customer_can_overflow.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）：一棵带着别人变异的树
跑出来的绿，与一棵干净的树跑出来的绿，在颜色上分不开。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8**，不要求调用方 export `PYTHONIOENCODING` ——
#: Windows 控制台默认 cp1252，这支探针整篇打印中文。
#: `AGENTS.md`：「一个要靠调用方设环境变量才能工作的工具，**修工具本身**」。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_a_list_the_customer_can_overflow.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 9
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2433
CONTROL_BASELINE_SKIPPED = 53

STACK = ROOT / "services/worker/src/fastagent_v2/worker/target_stack.py"
PACKAGE_SCHEMA = ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
TOUCHED = (STACK, PACKAGE_SCHEMA)

TRIM = "while len(history) > FRAMES_ENTERED_CAP:"
FORGET_A_REPEAT = """    seen = Counter(history)
    for index, entry in enumerate(history):
        if seen[entry] > 1:
            return index
    return 0"""
FORGET_THE_OLDEST = """    _ = Counter(history)
    return 0"""
CAP = "FRAMES_ENTERED_CAP = 64"
ROOM = '"maxItems": 63,'

#: ⚠️ **带参用例在 `failed` 里是一个名字，没有 `[63]` 那一截。**
#: 2026-09-22 本窗口在另一批上按带参名写过期望，四条里错了三条。
TALKATIVE = "test_a_talkative_customer_cannot_break_the_conversation"
KEEPS_THE_VISIT = "test_overflow_forgets_the_count_and_not_the_visit"
STILL_REFUSED = "test_a_brief_answer_frame_is_still_refused_after_overflow"
ONE_NUMBER = "test_the_cap_is_one_number"
LEAVES_ROOM = "test_the_package_bound_leaves_room_for_the_platforms_own_frames"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A1 · 写入侧不再收尾（= 修复前，第 65 笔写进去）—— **本批兑现物**",
            [Mutation(STACK, TRIM, "while False:")],
            #: ⚠️ `KEEPS_THE_VISIT` 是**量出来的，我预测时漏了它**：
            #: 它最后一句断言 `count("objection") < 120`，而不收尾的话
            #: 120 笔全在表里，那一句同样红。两条一起写在这里。
            [TALKATIVE, KEEPS_THE_VISIT],
            [],
        ),
        (
            "A2 · 到顶时丢最旧的一笔，而不是最旧的那笔**重复**记录",
            [Mutation(STACK, FORGET_A_REPEAT, FORGET_THE_OLDEST)],
            [KEEPS_THE_VISIT, STILL_REFUSED],
            [],
        ),
        (
            "A3 · 代码里的上界改成 32（与契约那格分家）",
            [Mutation(STACK, CAP, "FRAMES_ENTERED_CAP = 32")],
            [ONE_NUMBER, LEAVES_ROOM],
            [],
        ),
        (
            "A4 · 配包那格抬到 64 —— 不给平台帧留位",
            [Mutation(PACKAGE_SCHEMA, ROOM, '"maxItems": 64,')],
            [LEAVES_ROOM],
            [],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                skipped=BASELINE_SKIPPED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            # **两边都按集合相等判**，不是「包含」。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    aimed = all(hit and other for _, _, hit, other in verdicts)
    print("ALL AIMED" if aimed else "SOMETHING WENT RED SOMEWHERE ELSE (or did not go red)")
    return 0 if aimed else 1


if __name__ == "__main__":
    raise SystemExit(main())
