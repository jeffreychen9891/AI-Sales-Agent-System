#!/usr/bin/env python
"""反向探针：`cancel_on` 那一格说的是不是还为真（T358）。

## 它守的是一次「承认」，而承认最容易悄悄过期

用户 2026-09-16 裁定：**不改行为，把声明改成它真的是什么**。
于是契约那一格现在写着「平台不读你，这四处一律取消」。

**这种句子的坏法是行为先变、文字留在原地** —— 而那比原来更糟：
原来只是没说，现在是说错了。四支各打一种变法：

    python scripts/probe_cancel_on_truth.py

| | 变异 | 演的是 |
|---|---|---|
| A | 改掉一处取消的原因码 | 那条取消搬了家/没了，而描述还点着它的名 |
| B | 词表里加第六个值 | 又一个没人管的值（T358 那个形状本身） |
| C | 让某处真的把承诺置成 `fulfilled` | **兑现那一步落地了** —— 该去接那条取消 |
| D | 把那一格的描述删掉 | 配包作者又读不到「平台不读你」 |

**C 是这一支真正的理由**：它是一根指向未来的绊线，
而一根从来没有人见过它红的绊线，与不存在没有区别。
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
TARGET = "tests/contracts/test_cancel_on_says_what_really_happens.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 4
CONTROL_BASELINE_PASSED = 1734

APP = ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"
LEDGER = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "commitment_ledger.py"
SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"

TOUCHED = (APP, LEDGER, SCHEMA)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 改掉「人接管」那处的原因码（它搬了家）",
            [
                Mutation(
                    APP,
                    "invalidation_reason = 'CONVERSATION_TAKEN_OVER'",
                    "invalidation_reason = 'TAKEN_OVER_BY_A_PERSON'",
                )
            ],
            ["test_the_four_unconditional_cancellations_are_still_there"],
            [],
        ),
        (
            "B · 词表里加第六个值（又一个没人管的）",
            [
                Mutation(
                    SCHEMA,
                    '"commitment_fulfilled",\n                      "profile_repin"',
                    '"commitment_fulfilled",\n                      "profile_repin",\n'
                    '                      "business_event"',
                )
            ],
            ["test_the_declared_vocabulary_and_the_executors_still_line_up"],
            # 别处一条：批 14 那条普查守卫也按数字钉着这张词表（`== 5`）。
            # **两条各守一半**：那边守「词表没变」，这边守「变了的那个有没有人管」。
            ["test_cancel_on_still_has_no_runtime_consumer"],
        ),
        (
            "C · 让台账真的把一条承诺置成 `fulfilled`（兑现那一步落地了）",
            [
                Mutation(
                    LEDGER,
                    "def current_refs(",
                    "def mark_fulfilled(connection: object, row_id: object) -> None:\n"
                    '    """探针用：演一次「兑现那一步落地了」。"""\n\n'
                    "    connection.execute(  # type: ignore[attr-defined]\n"
                    "        \"UPDATE commitments SET status = 'fulfilled' WHERE id = %s\",\n"
                    "        (row_id,),\n"
                    "    )\n\n\n"
                    "def current_refs(",
                )
            ],
            ["test_nothing_fulfils_a_commitment_yet_so_that_value_still_does_nothing"],
            [],
        ),
        (
            "D · 把那一格的描述删掉",
            [
                Mutation(
                    SCHEMA,
                    '"cancel_on": {\n                  "description":',
                    '"cancel_on": {\n                  "_description":',
                )
            ],
            ["test_the_contract_says_the_platform_does_not_read_this_field"],
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
