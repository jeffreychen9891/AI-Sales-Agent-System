#!/usr/bin/env python
"""反向探针：会话成本上限抬到 1 元那一批。

    python scripts/probe_conversation_cost_ceiling.py

| | 变异 | 演的是 |
|---|---|---|
| A | 硬顶改回 0.27 | **一通正常对话就撞线**（复核实测 0.274191） |
| B | 软顶改回 0.11 | 每通正常对话都告警一次 —— 一个永远在响的告警 |
| C | 会话计数闸写死 `return 1` | 那道闸不再按上限推导 |

## 为什么没有「把 release-5 也标 active」这一支

试过，红了 **50 条 + 连带 60 条** —— 那不是在量这一批的守卫，
是在量「全仓有多少地方读 `active_release()`」。它本来就有专门的闸
（`test_exactly_one_release_is_active`），而一支要声称 110 个名字的探针
读的人得不到任何东西。

## C 单独说一句

**它在这一批里格外要紧。** 抬上限会把那道闸推导出的值从 1 抬到 5，
而钉它的那条用例 2026-09-17 之前用「硬顶 × 1.2」当下界 ——
那是把一个**容差**当成了**要求**。改成比硬顶本身之后，
`return 1` 仍然被杀得掉，**这一支就是证明**。
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
    failed_names,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGETS = [
    "tests/contracts/test_cost_ledger.py",
    "tests/contracts/test_cost_ceiling_judges.py",
    "tests/contracts/test_cost_reconciler.py",
    "tests/contracts/test_model_policy_and_providers.py",
]

#: 实测 2026-09-17，随门禁回填。
BASELINE_PASSED = 90
CONTROL_BASELINE_PASSED = 1739

RELEASE6 = ROOT / "packages/contracts/model-policy/release-2026.09.02-6.json"
COST = ROOT / "services/worker/src/fastagent_v2/worker/cost.py"
TOUCHED = (RELEASE6, COST)

#: A / B —— 两个数各自改回旧值。
HARD_NOW = '"hard_conversation": 1.0'
HARD_OLD = '"hard_conversation": 0.27'
SOFT_NOW = '"soft_conversation": 0.4'
SOFT_OLD = '"soft_conversation": 0.11'

#: C —— 那道闸不再按上限推导。
DERIVED = "    return max(1, int(ceiling / worst))"
HARDCODED = "    return 1"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, TARGETS)
    control = control_runner(ROOT, PYTHON, TARGETS)

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 硬顶改回 0.27",
            [Mutation(RELEASE6, HARD_NOW, HARD_OLD)],
            # **两条**：一条钉着数值，一条问「它拦不拦正常对话」。
            # 第二条是这一批新加的 —— 在它之前，把上限改成任何值都只会
            # 红那条「等于 0.27」，而那只证明没人手滑，不证明这个数是对的。
            [
                "test_release_json_and_ceiling_defaults_agree",
                "test_the_conversation_ceiling_does_not_catch_a_normal_conversation",
            ],
            [],
        ),
        (
            "B · 软顶改回 0.11",
            [Mutation(RELEASE6, SOFT_NOW, SOFT_OLD)],
            [
                "test_release_json_and_ceiling_defaults_agree",
                "test_the_warning_ceiling_does_not_fire_on_every_normal_conversation",
            ],
            [],
        ),
        (
            "C · 会话计数闸写死 return 1",
            [Mutation(COST, DERIVED, HARDCODED)],
            # **这一支就是「改了下界还杀得掉 return 1」的证明。**
            ["test_the_default_conversation_gate_is_sized_to_the_conversation_ceiling"],
            [],
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
            names = set(failed_names(result.output))
            hit = names == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(result)
            if result.went_red and not hit:
                print(
                    "    ⚠️ 红了，但不是声称的那一组："
                    f"多红 {sorted(names - set(expects))}；没红 {sorted(set(expects) - names)}"
                )
            if not elsewhere:
                print(
                    "    ⚠️ 连带集合与声称的不一致："
                    f"实测 {sorted(result.collateral)}，声称 {sorted(collateral)}"
                )
            print()

    print("=" * 72)
    silent = [label for label, went_red, _, _ in verdicts if not went_red]
    misaimed = [label for label, went_red, hit, _ in verdicts if went_red and not hit]
    drifted = [label for label, _, _, elsewhere in verdicts if not elsewhere]
    for label, went_red, hit, elsewhere in verdicts:
        if not went_red:
            mark = "GREEN ❌"
        elif not hit:
            mark = "RED ⚠️ 红错了"
        elif not elsewhere:
            mark = "RED ⚠️ 连带变了"
        else:
            mark = "RED ✅"
        print(f"  {mark:>14}  {label}")
    if silent:
        print(f"\n**{len(silent)} 支探针没红，它们证明不了任何东西**")
    if misaimed:
        print(f"\n**{len(misaimed)} 支红在别的用例上**")
    if drifted:
        print(f"\n**{len(drifted)} 支的连带集合与声称的不一致**")
    return 1 if (silent or misaimed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
