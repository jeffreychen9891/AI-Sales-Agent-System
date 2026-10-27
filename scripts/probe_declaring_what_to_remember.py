#!/usr/bin/env python
"""反向探针：声明了要记，就得有哪个打法真的能记。

    python scripts/probe_declaring_what_to_remember.py

| | 变异 | 演的是 |
|---|---|---|
| A | 这一道永不触发 | **这一批之前的样子**：声明封得了版，而它一条都不会被写进去 |

## 只有一支，而这是量出来的结论不是省事

**收紧方向的变异做不出干净的探针**，而原因本身值得记下来：

| 试过的变异 | 实测连带 |
|---|---|
| 见声明就拒 | 全套 admission 用例 |
| 把「没声明就不查」那道去掉 | **144 条红 + 11 个收集错误** |

**因为演示包从这一批起真的声明了六个字段** —— 任何一个「见声明就拒」方向的
变异都会让在架那份包封不了版，于是全仓读它的用例一起红。

**那个 144 本身就是这道闸真的在咬的证据**：它不是一条只在夹具上成立的规则，
它站在一份正在发布的配包的路上。

收紧那一侧改由两条用例正面钉：
`test_a_merchant_vocabulary_publishes`（正常声明发得出去）与
`test_an_absent_declaration_publishes`（不记东西的包不必允许 `remember`）——
**它们正是上面那两个变异会打红的那两条。**
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
TARGET = "tests/contracts/test_memory_fields_admission.py"

#: 实测 2026-09-18，随门禁回填。
BASELINE_PASSED = 11
CONTROL_BASELINE_PASSED = 1919

REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
TOUCHED = (REGISTRY,)

#: A —— 永不触发。
FIRES = """    if not any(
        "remember" in ((body or {}).get("allowed_intent_kinds") or [])
        for body in (document.get("strategies") or {}).values()
    ):"""
FIRES_NEVER = "    if False:"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 这一道永不触发",
            [Mutation(REGISTRY, FIRES, FIRES_NEVER)],
            [
                "test_declaring_what_to_remember_without_anyone_able_to_remember_is_refused"
            ],
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
