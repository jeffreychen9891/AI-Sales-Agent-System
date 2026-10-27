#!/usr/bin/env python
"""反向探针：封版侧为文件候选预留字节（`tests/contracts/test_asset_bytes_are_reserved.py`）。

    python scripts/probe_asset_bytes.py

| | 变异 | 演的是 |
|---|---|---|
| A | 把 asset 那整段循环停掉 | **回到这一批之前**：一份包声明了文件而没人算它的账 |
| B | `proactive` 那一档不入阶段桶 | 只漏一档 —— 而漏掉的那一档正是「主动发」那种 |
| C | 用 `budgeted_entry_cost` 顶替 | 每条少算 1 字节，**方向是 fail-open** |

**A 是这支的主证**：它要证明的不是「代码跑过了」，而是**那个数字会跟着动** ——
少了这条闸，整段循环删掉也不会有任何东西红。

## 为什么 C 只差一个字节也要单独一支

`entry_cost` 与 `asset_entry_cost` 的差别只有键名两个字母。顶替它跑起来不报错、
算出来的数看上去也正常 —— **只是每条都少 1，而少算的方向是封版说得下、
真实回合超出去**。这个仓为 fail-open 的少算付过账（76 字节那次）。
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
TARGET = "tests/contracts/test_asset_bytes_are_reserved.py"

#: 实测 2026-09-17。
BASELINE_PASSED = 3
CONTROL_BASELINE_PASSED = 1796

REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
TOUCHED = (REGISTRY,)

LOOP_ON = "    for asset in frame.get(CONTENT_ASSETS_KEY, []) or []:"
LOOP_OFF = "    for asset in []:"

PROACTIVE_ON = '        elif asset_availability == "proactive":'
PROACTIVE_OFF = "        elif False:"

RIGHT_COST = "        asset_cost = budgeted_asset_entry_cost(asset_ref, asset_label)"
WRONG_COST = "        asset_cost = budgeted_entry_cost(asset_ref, asset_label)"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · asset 那整段循环停掉（回到这一批之前）",
            [Mutation(REGISTRY, LOOP_ON, LOOP_OFF)],
            [
                "test_an_asset_makes_the_stage_account_bigger",
                # **实测加上的**：那条「挤爆会拒」只用 asset 不用话术，
                # 所以 asset 不算账它就不再挤爆 —— 两条一起红。
                "test_a_frame_whose_assets_overflow_the_stage_is_refused",
            ],
            [],
        ),
        (
            "B · `proactive` 那一档不入阶段桶",
            [Mutation(REGISTRY, PROACTIVE_ON, PROACTIVE_OFF)],
            [
                "test_an_asset_makes_the_stage_account_bigger",
                # **实测加上的**：那条「挤爆会拒」只用 asset 不用话术，
                # 所以 asset 不算账它就不再挤爆 —— 两条一起红。
                "test_a_frame_whose_assets_overflow_the_stage_is_refused",
            ],
            [],
        ),
        (
            "C · 用 `budgeted_entry_cost` 顶替（每条少算 1 字节）",
            [Mutation(REGISTRY, RIGHT_COST, WRONG_COST)],
            ["test_an_asset_makes_the_stage_account_bigger"],
            [],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(passed=BASELINE_PASSED, control_passed=CONTROL_BASELINE_PASSED),
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
    return 1 if silent or misaimed or drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
