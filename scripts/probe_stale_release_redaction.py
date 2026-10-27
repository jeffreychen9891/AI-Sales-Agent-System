#!/usr/bin/env python
"""反向探针：脱敏那条守卫（`test_stale_release_report_is_redacted.py`）。

## 它证明的那件事，2026-09-16 之前是假的

`check_stored_releases.py` 的 docstring 写着「不打正文是有意的」，
而那一版打的是 `error.message[:160]` —— jsonschema 在 `maxLength` 上的消息体
**就是那个超长的值本身**，于是真跑时两行 FAIL 各打出了约 160 字商家 goal 原文。

**所以这一支把那一版原样演一遍**：把 `describe()` 换回 `error.message[:160]`，
看那十三条是不是真的会叫。**它是这条守卫唯一一次在「失败」状态下被人看见。**

    python scripts/probe_stale_release_redaction.py

**变异是一次合法改写**，不是把语法弄坏：`error.message[:160]` 是上一版真写过的
一行代码，在今天的树上照样跑得动 —— 只是它会泄露。
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
TARGET = "tests/contracts/test_stale_release_report_is_redacted.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 13
CONTROL_BASELINE_PASSED = 1712

SCRIPT = ROOT / "scripts" / "check_stored_releases.py"
TOUCHED = (SCRIPT,)

TODAYS_BODY = """    instance = error.instance"""

THE_OLD_LEAKING_LINE = """    return error.message[:160]

    instance = error.instance"""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · `describe()` 换回 `error.message[:160]`（2026-09-16 泄露的那一版）",
            [Mutation(SCRIPT, TODAYS_BODY, THE_OLD_LEAKING_LINE)],
            [
                "test_it_still_says_enough_to_act_on",
                "test_no_keyword_leaks_the_offending_value",
                "test_the_promise_and_the_code_are_in_the_same_file",
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
