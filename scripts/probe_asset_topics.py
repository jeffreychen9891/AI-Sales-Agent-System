#!/usr/bin/env python
"""反向探针：资料的话题必须可达（`tests/contracts/test_asset_topics_are_reachable.py`）。

    python scripts/probe_asset_topics.py

| | 变异 | 演的是 |
|---|---|---|
| A | 把不可达那道闸停掉 | **回到这一批之前**：封得了版、永远不触发、没东西会红 |
| B | 空词表那一支并进「不可达」那条报文 | 作者去改那个值，而真正要做的是先给话术标 topics |
| C | 报文里不再列出词表 | 拒绝了，但作者读完还是不知道该写什么 |

**C 是这支里最容易被当成小事的一条**：闸照样拦得住，
**只是拦完之后没人知道下一步做什么** —— 而这张词表是从作者自己的话术
推出来的，他未必知道它在哪。
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
TARGET = "tests/contracts/test_asset_topics_are_reachable.py"

#: 实测 2026-09-17 填。
BASELINE_PASSED = 3
CONTROL_BASELINE_PASSED = 1799

REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
TOUCHED = (REGISTRY,)

GATE_ON = "            if asset_topics and topic_slot:"
GATE_OFF = "            if False:"

EMPTY_BRANCH_ON = "                if not vocabulary:"
EMPTY_BRANCH_OFF = "                if False:"

NAMES_VOCAB = 'f"{sorted(vocabulary)}. **An asset gated on a value outside it seals, "'
HIDES_VOCAB = 'f"（词表略）. **An asset gated on a value outside it seals, "'


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 不可达那道闸停掉（回到这一批之前）",
            [Mutation(REGISTRY, GATE_ON, GATE_OFF)],
            [
                "test_a_topic_outside_the_vocabulary_is_refused_with_the_vocabulary_named",
                "test_a_frame_whose_wording_carries_no_topics_at_all_is_refused_differently",
            ],
            [],
        ),
        (
            "B · 空词表那一支并进「不可达」那条",
            [Mutation(REGISTRY, EMPTY_BRANCH_ON, EMPTY_BRANCH_OFF)],
            ["test_a_frame_whose_wording_carries_no_topics_at_all_is_refused_differently"],
            [],
        ),
        (
            "C · 报文里不再列出词表",
            [Mutation(REGISTRY, NAMES_VOCAB, HIDES_VOCAB)],
            ["test_a_topic_outside_the_vocabulary_is_refused_with_the_vocabulary_named"],
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
