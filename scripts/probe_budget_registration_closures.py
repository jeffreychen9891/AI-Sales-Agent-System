#!/usr/bin/env python
"""反向探针：T345 / T352 **收口所依据的那两条守卫**，今天真的会红吗。

## 这一支守的是两句「已经不成立了」

2026-09-16 按现读关掉了两条登记：

* **T345**（「去掉并发度声明，演示包超限 23 字节、第二紧只剩 29」）——
  今天不超了，因为预算从 5,630 抬到了 32,420；
* **T352**（「推导式里从来没有知识库那一项」）—— 今天有了
  （`763b477b feat(预算链): ... 推导式补上知识库那一项`）。

**关掉一条登记就是写下一句「这个问题不存在了」** —— 而本仓对这种句子有硬要求
（`CLAUDE.md` 第二节）：要有一条**会在它重新成立时变红**的守卫。

所以这里把那两条守卫各自推回旧世界，看它们是不是真的会叫：

    python scripts/probe_budget_registration_closures.py

| | 变异 | 打的是哪条收口 |
|---|---|---|
| A | `MATERIAL_BYTES_BUDGET` 32,420 → **5,630**（两份副本一起） | T345 |
| B | `MAX_MODEL_INPUT_BYTES` 49,000 → **16,384**（两份副本一起） | T352 |

**两份副本必须一起改**：两个常量各写两遍、由用例钉死相等，
只改一份红的是那条相等用例 —— 那是一次无效变异，量到的是别的东西。
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
    "tests/contracts/test_profile_materials_are_trusted.py",
    "tests/contracts/test_knowledge_bound.py",
]

#: 实测 2026-09-16。
BASELINE_PASSED = 60
CONTROL_BASELINE_PASSED = 1647

REGISTRY = ROOT / "services" / "api" / "src" / "fastagent_v2" / "profiles" / "registry.py"
SELECTION = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "material_selection.py"
)
COST = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "cost.py"

TOUCHED = (REGISTRY, SELECTION, COST)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, TARGETS)
    control = control_runner(ROOT, PYTHON, TARGETS)

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 素材预算退回 5,630（T345 那笔账的旧世界）",
            [
                Mutation(REGISTRY, "MATERIAL_BYTES_BUDGET = 32420", "MATERIAL_BYTES_BUDGET = 5630"),
                Mutation(
                    SELECTION, "MATERIAL_BYTES_BUDGET = 32420", "MATERIAL_BYTES_BUDGET = 5630"
                ),
            ],
            # 自己那两份文件里红两条：收口那一条，**外加顶格恒等式那一条** ——
            # 预算变小之后「顶满预算的回合」也跟着变小，于是那条断言
            # 「再多一点就超限」的第三头不再成立。我原本只声称前者。
            [
                "test_a_turn_at_the_material_budget_still_fits_the_limit",
                "test_the_shipped_packages_pass_even_with_no_topic_concurrency_declared",
            ],
            # 别处一条：请求底噪那条恒等式 —— **素材预算是它的输入之一**。
            ["test_the_empty_request_baseline_is_measured_from_the_real_payload"],
        ),
        (
            "B · 请求上限退回 16,384（T352 那笔账的旧世界）",
            [
                Mutation(
                    REGISTRY, "MAX_MODEL_INPUT_BYTES = 49_000", "MAX_MODEL_INPUT_BYTES = 16_384"
                ),
                Mutation(COST, "MAX_MODEL_INPUT_BYTES = 49_000", "MAX_MODEL_INPUT_BYTES = 16_384"),
            ],
            ["test_a_turn_at_the_material_budget_still_fits_the_limit"],
            # **别处五条，而这五条本身就是一条结论**：`MAX_MODEL_INPUT_BYTES`
            # 不只是提示词预算的分母 —— 并发闸的容量、拒绝话术里的数字、
            # 以及一张「按今天的上限现算」的注释表都挂在它身上。
            # 退回 16,384 会让这五条一起红，也就是说 T352 收口所依据的那条守卫
            # **不是孤立的一条**。我原本声称 0，这是量出来的。
            [
                "test_the_default_conversation_gate_is_sized_to_the_conversation_ceiling",
                "test_the_empty_request_baseline_is_measured_from_the_real_payload",
                "test_the_gate_no_longer_confuses_in_flight_with_abandoned",
                "test_the_refusal_says_how_many_are_still_in_flight",
                "test_the_table_in_the_docstring_is_recomputed_from_todays_limit",
            ],
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
