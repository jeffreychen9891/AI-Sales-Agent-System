#!/usr/bin/env python
"""反向探针：定时器回合那条出站记成 `followup`（`test_followup_is_the_timer_turn_outbound.py`）。

## 为什么这一支非有不可

这个 kind 的全部内容是**一次分类**，而分类错了不会报错 ——
它只会让 `intent_records` 里那一行说一件不真的事：
一次主动跟进被记成被动回复，或者反过来。**能写入、能查询、能对账，只有含义是错的。**

四支各打一种错法：

    python scripts/probe_followup_classification.py

| | 变异 | 演的是 |
|---|---|---|
| A | 分类器直接返回原值 | **它被摘掉了** —— 一个没被调用的分类器与不存在没区别 |
| B | 判据写成「不是客户消息就算主动」 | 列举要拒的那种写法（`manager_comment` 等四种全被误记） |
| C | 注册表里拿掉 `turn_refusal` | 第二道锁没了，`followup` 与它的邻居在语义上无法区分 |
| D | 那张表再加一条（`remember` 也映射） | 把一个不是出站的 kind 也改了名 |

**B 是这一支真正的理由**：那个错法写出来最顺手（`!= "customer_message"`），
而本仓刚好为同一个形状付过钱 —— `_remember_turn_refusal` 第 1 条的注释逐字记着。
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
TARGET = "tests/contracts/test_followup_is_the_timer_turn_outbound.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 8
CONTROL_BASELINE_PASSED = 1734

PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
TOUCHED = (PIPELINE,)

TODAY = """        if str(prepared.envelope.get("event_type")) != "timer":
            return proposed
        return proactive"""

MISSING = """        return proposed
        return proactive"""

BY_EXCLUSION = """        if str(prepared.envelope.get("event_type")) == "customer_message":
            return proposed
        return proactive"""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 分类器直接返回原值（它被摘掉了）",
            [Mutation(PIPELINE, TODAY, MISSING)],
            [
                "test_a_timer_turn_records_the_outbound_as_a_followup",
                "test_the_decision_path_carries_the_reclassified_kind",
            ],
            [],
        ),
        (
            "B · 判据写成「不是客户消息就算主动」（列举要拒的那种）",
            [Mutation(PIPELINE, TODAY, BY_EXCLUSION)],
            ["test_the_other_event_types_are_untouched_too"],
            [],
        ),
        (
            "C · 注册表里拿掉 `turn_refusal`",
            [
                Mutation(
                    PIPELINE,
                    "        turn_refusal=_followup_turn_refusal,\n",
                    "",
                )
            ],
            ["test_the_turn_refusal_is_the_second_lock"],
            [],
        ),
        (
            "D · 那张表再加一条（`remember` 也被改名）",
            [
                Mutation(
                    PIPELINE,
                    'PROACTIVE_EQUIVALENT: Mapping[str, str] = {"send_message": "followup"}',
                    "PROACTIVE_EQUIVALENT: Mapping[str, str] = "
                    '{"send_message": "followup", "remember": "followup"}',
                )
            ],
            [
                "test_only_the_outbound_kind_is_reclassified",
                "test_the_map_has_exactly_one_entry_and_both_ends_are_registered",
            ],
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
