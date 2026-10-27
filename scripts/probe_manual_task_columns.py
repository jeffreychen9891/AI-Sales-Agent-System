#!/usr/bin/env python
"""反向探针：`tests/contracts/test_manual_task_owner_sla_expiry.py` 那五条（C2 / T91）。

与另外两支同一个结构：守的全是「零」，所以要逐支证明它会红，
**并逐支核红的是哪一条**（判据是集合相等，不是包含 —— 见 `probe_plan_document.py`
里那次缩进误伤）。

    python scripts/probe_manual_task_columns.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import Mutation, ProbeSession  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_manual_task_owner_sla_expiry.py"

BASELINE_PASSED = 5

PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
APP = ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"
DIGEST = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "digest.py"

TOUCHED = (PIPELINE, APP, DIGEST)

#: 引擎侧那条建单的列清单。
#:
#: ⚠️ **2026-09-20（T276）锚点换过一次**：此前它靠「四处里唯一带 `evidence`
#: 且缩进到 20 空格的那条」来保证只命中一处 —— 而 T276 把那五处合并成了
#: `pipeline._file_a_manual_task`，缩进从 20 变成 8，**旧锚点命中 0 处**。
#: 好在 harness 对 0 命中是当场抛错而不是跳过（「锚点没命中不是通过」），
#: 所以它不会变成一支安静地什么都没验的探针。
#: **今天它只出现一次的理由更硬了**：引擎侧只剩这一处，
#: 而 `test_manual_task_owner_sla_expiry.py::ENGINE_INSERT_SITES == 1` 守着它。
ENGINE_INSERT = """        INSERT INTO manual_tasks(
            tenant_id, id, conversation_id, intent_id,
            task_class, notify, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""

_FAILED = re.compile(r"^FAILED (?P<target>\S+?)::(?P<name>[\w\[\]\-]+)", re.MULTILINE)


def run() -> tuple[str, bool]:
    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            str(PYTHON),
            "-m",
            "pytest",
            TARGET,
            "-q",
            "--no-header",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = completed.stdout or ""
    names = sorted({match.group("name") for match in _FAILED.finditer(stdout)})
    lines = [line for line in stdout.splitlines() if line.strip()]
    summary = lines[-1] if lines else "(no output)"
    return f"{summary} | FAILED: {names or '（无）'}", completed.returncode != 0


def _failed_names(output: str) -> list[str]:
    return re.findall(r"'([\w\[\]\-]+)'", output.split("FAILED: ", 1)[-1])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str]]] = [
        (
            "R1 · 让引擎在建单时填 owner 与 SLA（T91 的到期条件）",
            [
                Mutation(
                    PIPELINE,
                    ENGINE_INSERT,
                    ENGINE_INSERT.replace(
                        "task_class, notify, reason_code, evidence",
                        "task_class, notify, reason_code, evidence, owner_employee_id, sla_due_at",
                    ),
                )
            ],
            ["test_the_engine_fills_none_of_the_three"],
        ),
        (
            "R2 · 给 expires_at 接一个写者",
            [
                Mutation(
                    PIPELINE,
                    ENGINE_INSERT,
                    ENGINE_INSERT.replace(
                        "task_class, notify, reason_code, evidence",
                        "task_class, notify, reason_code, evidence, expires_at",
                    ),
                )
            ],
            [
                "test_nothing_anywhere_ever_writes_expires_at",
                "test_the_engine_fills_none_of_the_three",
            ],
        ),
        (
            'R3 · 发一次 effect="manual_review"（C2 那条路有了起点）',
            [Mutation(DIGEST, 'effect="allow"', 'effect="manual_review"')],
            ["test_the_manual_review_effect_still_has_no_origin"],
        ),
        (
            "R4 · 让控制面不再填 owner/SLA（非零对照没了）",
            [
                Mutation(
                    APP,
                    "                    owner_employee_id, sla_due_at, evidence",
                    "                    evidence",
                )
            ],
            ["test_the_human_side_supplies_owner_and_sla"],
        ),
        (
            "R5 · 引擎侧多出第二处建单（等值断言，不是上界 —— T276 之后这个数是 1）",
            [Mutation(PIPELINE, ENGINE_INSERT, ENGINE_INSERT + "\n" + ENGINE_INSERT)],
            ["test_the_scanner_found_both_sides"],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run) as session:
        print("baseline:", session.assert_baseline(passed=BASELINE_PASSED))
        print()
        verdicts: list[tuple[str, bool, bool]] = []
        for label, mutations, expects in probes:
            result = session.probe(label, mutations)
            names = _failed_names(result.output)
            hit = set(names) == set(expects)
            verdicts.append((label, result.went_red, hit))
            print(result)
            if result.went_red and not hit:
                extra = sorted(set(names) - set(expects))
                short = sorted(set(expects) - set(names))
                print(f"    ⚠️ 红了，但不是声称的那一组：多红 {extra}；没红 {short}")
            print()

    print("=" * 72)
    silent = [label for label, went_red, _ in verdicts if not went_red]
    misaimed = [label for label, went_red, hit in verdicts if went_red and not hit]
    for label, went_red, hit in verdicts:
        mark = "RED ✅" if went_red and hit else ("RED ⚠️ 红错了" if went_red else "GREEN ❌")
        print(f"  {mark:>14}  {label}")
    if silent:
        print(f"\n**{len(silent)} 支探针没红，它们证明不了任何东西**")
    if misaimed:
        print(f"\n**{len(misaimed)} 支红在别的用例上**")
    return 1 if silent or misaimed else 0


if __name__ == "__main__":
    raise SystemExit(main())
