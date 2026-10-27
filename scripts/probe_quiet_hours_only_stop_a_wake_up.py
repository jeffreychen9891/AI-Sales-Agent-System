#!/usr/bin/env python
"""反向探针：静默时段那一批（ADR-0148）。

守两份判据：
`tests/contracts/test_quiet_hours_refuse_and_say_when.py` ·
`tests/contracts/test_a_stopped_follow_up_comes_back.py`。

**最要紧的两支**：

* A · **把那道闸改回「不分 classification」** —— 它必须红，
  因为本批存在的全部理由就是深夜的会话内延续当时被挡了；
* D · **补排整条删掉** —— 静默时段挡下的跟进就那样没了，
  而那种失败**一声不吭**：no-send 照常记、policy hook 照常写、
  inbox 事件照常消费，**只有客户第二天早上什么都没收到**。

其余：

* B · 活跃窗口退回 30 分钟（「会话内延续在 2 分钟内才有意义」）；
* C · 补排不再按理由码筛（退出信号也被重排 —— 那是无视客户说的「别再发了」）；
* E · `dedupe_key` 不带后缀（**那一行会静默地插不进去**）；
* F · 补排挪出 no-send 的事务；
* G · 静默时段的理由码变了而补排那一侧没跟上。

    python scripts/probe_quiet_hours_only_stop_a_wake_up.py
"""

from __future__ import annotations

import re
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
TARGETS = [
    "tests/contracts/test_quiet_hours_refuse_and_say_when.py",
    "tests/contracts/test_a_stopped_follow_up_comes_back.py",
]

#: 本批那两份合起来。**报数，不报颜色。**
BASELINE_PASSED = 19

#: `tests/contracts` 减去那两份。**量出来的。**
CONTROL_BASELINE_PASSED = 2394
CONTROL_BASELINE_SKIPPED = 53

PROACTIVITY = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "proactivity.py"
)
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
PACKAGE = ROOT / "profiles" / "internal-demo-sales" / "profile.json"

TOUCHED = (PROACTIVITY, PIPELINE, PACKAGE)

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


NIGHT_OK = "test_a_continuation_at_night_is_not_stopped"
WINDOW = "test_an_active_window_closing_first_names_no_instant"
SHIPPED_WINDOW = "test_the_shipped_window_is_the_two_minutes_the_user_ruled"
ONLY_QUIET = "test_the_reschedule_only_fires_for_quiet_hours"
SUFFIX = "test_the_new_row_cannot_collide_with_the_one_that_was_consumed"
SAME_TXN = "test_it_runs_inside_the_no_send_transaction"
CODE = "test_the_reason_code_is_the_one_proactivity_actually_emits"

#: ⚠️ **配包一动就掀封版戳** —— 逐名登记那一片没有意义，改按性质断言。
SEAL_BLAST = "<封版戳被掀翻：那一片与静默时段无关>"
SEAL_TEST = "test_the_package_loads_through_the_registry_and_hashes_to_its_declaration"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, TARGETS)
    control = control_runner(ROOT, PYTHON, TARGETS)

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str] | str]] = [
        (
            "A · 那道闸改回「不分 classification」",
            [
                Mutation(
                    PROACTIVITY,
                    "    if classification == RE_ENGAGEMENT and knobs.quiet_hours.covers(now):",
                    "    if knobs.quiet_hours.covers(now):",
                )
            ],
            [NIGHT_OK],
            (),
        ),
        (
            "B · 活跃窗口退回 30 分钟",
            [Mutation(PACKAGE, '"active_window_seconds": 120', '"active_window_seconds": 1800')],
            # ⚠️ 在架那条自核也红 —— 它钉的正是这个数（实测）。
            [SHIPPED_WINDOW],
            SEAL_BLAST,
        ),
        (
            "C · 补排不再按理由码筛",
            [
                Mutation(
                    PIPELINE,
                    "    if reason_code != QUIET_HOURS_REASON or next_allowed_at is None:",
                    "    if next_allowed_at is None:",
                )
            ],
            [ONLY_QUIET],
            (),
        ),
        (
            "D · 补排整条删掉（跟进就那样没了）",
            [
                Mutation(
                    PIPELINE,
                    "            _reschedule_after_quiet_hours(",
                    "            _noop_instead_of_rescheduling(",
                )
            ],
            # no-send 那一路上另有一条在看这个函数体 —— 实测。
            [SAME_TXN],
            ("test_the_no_send_path_really_reaches_the_task_filer",),
        ),
        (
            "E · dedupe_key 不带后缀（那一行会静默地插不进去）",
            [
                Mutation(
                    PIPELINE,
                    'dedupe_key=f"{row[\'dedupe_key\']}:after_quiet_hours",',
                    "dedupe_key=str(row['dedupe_key']),",
                )
            ],
            [SUFFIX],
            (),
        ),
        (
            "F · 静默时段的理由码变了",
            [
                Mutation(
                    PROACTIVITY,
                    'QUIET_HOURS_REASON = "PROACTIVITY_QUIET_HOURS"',
                    'QUIET_HOURS_REASON = "PROACTIVITY_NIGHT"',
                )
            ],
            [CODE],
            (),
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            names = _failed_names(result.output)
            hit = set(names) == set(expects)
            control_names = _failed_names(result.control_output or "")
            if collateral == SEAL_BLAST:
                collateral_hit = SEAL_TEST in control_names and len(control_names) > 30
            else:
                collateral_hit = set(control_names) == set(collateral)
            verdicts.append((label, result.went_red, hit, collateral_hit))
            print(result)
            if not hit:
                print(
                    f"    ⚠️ 本批：多红 {sorted(set(names) - set(expects))}；"
                    f"没红 {sorted(set(expects) - set(names))}"
                )
            if not collateral_hit and collateral == SEAL_BLAST:
                print(
                    f"    ⚠️ 连带：封版那一条在不在 = {SEAL_TEST in control_names}，"
                    f"红了 {len(control_names)} 条（要 > 30）"
                )
            elif not collateral_hit:
                print(
                    f"    ⚠️ 连带：多红 {sorted(set(control_names) - set(collateral))}；"
                    f"没红 {sorted(set(collateral) - set(control_names))}"
                )
            print()

    print("=" * 72)
    bad = [label for label, _, hit, coll in verdicts if not (hit and coll)]
    for label, went_red, hit, coll in verdicts:
        mark = ("RED ✅" if went_red else "GREEN ✅（声明如此）") if hit and coll else "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
