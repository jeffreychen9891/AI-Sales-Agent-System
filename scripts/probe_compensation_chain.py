#!/usr/bin/env python
"""反向探针：批 4 那条守卫，逐条验它会红。

守的是 `tests/contracts/test_compensation_chain_absence.py` ——
自动补偿那一段（`inverse` 的写者 · `APPLIED -> COMPENSATING -> COMPENSATED`）
今天零实现，**而兜底那一段（`UNKNOWN` / 人工）是实现了的**。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **C1** | 给 `inverse` 写一个真值（只落三件里的第一件） | 分类那条 |
| **C2** | 把 `COMPENSATING` 从成员表里删掉（「顺手清理」） | **两条都红** |
| **C3** | 在别处新写一个 `COMPENSATED` 常量 | 分类那条 |
| **C4** | 把 `UNKNOWN` 从成员表里删掉（**兜底那一段**） | 兜底那条 |

**C4 是这支探针的重点。** 只钉「自动补偿没实现」是不够的 ——
那样把 `UNKNOWN` 那条路删掉也会全绿，**而删掉它才是真的危险**：
图纸 `conversation-engine.md:504` 要的正是「没有可靠 inverse 时停在 UNKNOWN/人工，
不要假装回滚成功」。**一条只断言「某物不存在」的守卫，
默认不会保护「那个物的替代方案还在」。**

**C2 两条都红是对的**，而它说明那两条不是重复：
一条问「这个名字为什么在这里」，另一条问「兜底还在不在」。

    python scripts/probe_compensation_chain.py
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_compensation_chain_absence.py"

BASELINE_PASSED = 2

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1688

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"

TOUCHED = (PIPELINE,)

DECLARE = '            "inverse": None,\n'
WRITTEN = '            "inverse": {"kind": "undo"},\n'

LISTED = '    "COMPENSATING",\n'
UNKNOWN = '    "UNKNOWN",\n'

#: C3 在成员表旁边新加一个常量 —— 模拟「另一处开始用这个名字」。
NEW_CONSTANT = '_LIVE_EVENT_STATUSES: tuple[str, ...] = ("PENDING", "CLAIMED", "ENQUEUED")'
NEW_CONSTANT_PLUS = (
    '_TERMINAL_COMPENSATED = "COMPENSATED"\n'
    '_LIVE_EVENT_STATUSES: tuple[str, ...] = ("PENDING", "CLAIMED", "ENQUEUED")'
)

CLASSIFIED = "test_every_mention_of_the_compensation_chain_has_been_classified"
FALLBACK = "test_the_fallback_half_really_is_implemented"

#: **本批立的那句话的证据，量出来之后不成立** —— 提交说明与守卫的 docstring 写着
#: 「只钉『自动补偿没实现』是不够的：**把 `UNKNOWN` 那条兜底删掉也会全绿**」。
#: 对照跑（2026-09-16，基线 `88882184`）：C2 / C4 排除本文件后**各红一条** ——
#: 既有的 `test_a_new_status_in_the_migration_lands_in_neither_set`，
#: 它的判据是「迁移词表 − 活着的 == 写死的终态集合」，所以成员表少一个就红。
#:
#: **那句话作为原则仍然成立**（断言某物不存在，默认不保护它的替代方案还在），
#: **而本批这个实例不是它的证据**：兜底那一半早有守卫。
#: 新守卫真正新增的是**分类**那一半 —— C1 / C3 的连带集合是空的，
#: 没有第二处看得见「`inverse` 有了写者」或「别处多了一个 `COMPENSATED`」。
MIGRATION_SET = "test_a_new_status_in_the_migration_lands_in_neither_set"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "C1 · 给 `inverse` 写一个真值（三件里只落第一件）",
            [Mutation(PIPELINE, DECLARE, WRITTEN)],
            [CLASSIFIED],
            [],  # 全仓没有第二处守它 —— 分类那条是它唯一的守卫
        ),
        (
            "C2 · 把 `COMPENSATING` 从成员表里「顺手清理」掉",
            [Mutation(PIPELINE, LISTED, "")],
            [CLASSIFIED, FALLBACK],
            [MIGRATION_SET],  # **既有覆盖**，见 docstring「本批立的那句话」
        ),
        (
            "C3 · 在别处新写一个 `COMPENSATED` 常量",
            [Mutation(PIPELINE, NEW_CONSTANT, NEW_CONSTANT_PLUS)],
            [CLASSIFIED],
            [],  # 同 C1
        ),
        (
            "C4 · 把 `UNKNOWN` 从成员表里删掉 —— **兜底那一段**",
            [Mutation(PIPELINE, UNKNOWN, "")],
            [FALLBACK],
            [MIGRATION_SET],  # **既有覆盖**
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
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            # **两边都按集合相等判**，不是「包含」：多红出来的那几条是信号，
            # 不是噪音 —— 它们说明这次变异的影响面比声称的大。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    ok = all(red and hit and other for _, red, hit, other in verdicts)
    print("ALL RED AND AIMED" if ok else "SOMETHING DID NOT GO RED (or went red elsewhere)")
    if ok:
        print()
        print("**C4 是重点**：只钉「自动补偿没实现」的话，把兜底那条路删掉也会全绿 ——")
        print("而删掉兜底才是真的危险。断言某物不存在，默认不保护它的替代方案还在。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
