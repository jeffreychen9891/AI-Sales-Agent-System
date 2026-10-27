#!/usr/bin/env python
"""反向探针：`tests/contracts/test_plan_document_is_never_persisted.py` 那五条，逐条验它会红。

这一份守的是 **B1-11 / B1-12 的根**（计划文档不落库，所以那几个字段没有可能的读者）。
与 `probe_policy_hook_shape.py` 同一个结构，理由也一样：**它守的全是「零」**，
而一条「恒为 0」的断言在扫描器失效时回的也是 0。

**同样逐支核「红的是哪一条」**（`AGENTS.md`「红了之后，要确认红的是你要测的那一条」），
runner 把 `FAILED` 行的用例名读回来与 `expects` 比对。

    python scripts/probe_plan_document.py
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
TARGET = "tests/contracts/test_plan_document_is_never_persisted.py"

#: 按数断言，不按颜色。
BASELINE_PASSED = 5

PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
LAST_MIGRATION = ROOT / "infrastructure" / "migrations" / "062_knowledge_cited_refs.sql"
PLAN_CONTRACT = ROOT / "packages" / "contracts" / "inference" / "plan-result.schema.json"

TOUCHED = (PIPELINE, LAST_MIGRATION, PLAN_CONTRACT)

#: `pipeline.py` 里那句读 `required_slot_requests` 的，用作几支探针的锚点。
#: 挑它是因为它**只出现一次**，而 harness 强制锚点命中数为 1。
ANCHOR = 'requested = [str(slot) for slot in plan.document["required_slot_requests"]]'

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
            "Q1 · 给 manual_review_reason 接一个读点（B1-12）",
            # **缩进必须与锚点同层（4 空格）**，而这是改过一版的：初版写了 8 空格，
            # `pipeline.py` 当场变成 `IndentationError`，于是三条 AST 断言**全都红了**
            # —— 包括那条「守卫的守卫」。它红得很响，而响声来自 `ast.parse`，
            # 不是来自任何一条断言（`AGENTS.md`「红了之后，要确认红的是你要测的那一条」）。
            # 发现它的不是 `expects` 那一关（期待的名字确实在失败清单里），
            # 是 `test_the_reader_scanner_really_found_something` 跟着红了 ——
            # **一条不该红的用例红了，本身就是信号**。下面的判据因此改成集合相等。
            [
                Mutation(
                    PIPELINE,
                    ANCHOR,
                    ANCHOR + '\n    _ = plan.document["manual_review_reason"]',
                )
            ],
            [
                "test_the_two_registered_fields_still_have_no_reader",
                "test_only_seven_keys_survive_validation",
            ],
        ),
        (
            "Q2 · 从 ValidatedPlan 上多读一个键（第八个）",
            [Mutation(PIPELINE, ANCHOR, ANCHOR + '\n    _ = plan.document["confidence"]')],
            ["test_only_seven_keys_survive_validation"],
        ),
        (
            "Q3 · 建一张装计划文档的表（T347 的到期条件）",
            [
                Mutation(
                    LAST_MIGRATION,
                    "-- 062: 三跳追溯的第一跳",
                    "CREATE TABLE plan_results (id uuid PRIMARY KEY);\n-- 062: 三跳追溯的第一跳",
                )
            ],
            ["test_no_migration_builds_a_table_for_plan_documents"],
        ),
        (
            "Q4 · 往 plan-result 合同里加第 14 个键",
            [
                Mutation(
                    PLAN_CONTRACT,
                    '"knowledge_query"',
                    '"a_new_key": {"type": "string"},\n    "knowledge_query"',
                )
            ],
            ["test_the_contract_still_declares_more_than_is_read"],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run) as session:
        print("baseline:", session.assert_baseline(passed=BASELINE_PASSED))
        print()
        verdicts: list[tuple[str, bool, bool]] = []
        for label, mutations, expects in probes:
            result = session.probe(label, mutations)
            names = _failed_names(result.output)
            # **集合相等，不是「包含」。** 初版用的是「期待的每个名字都在失败清单里」，
            # 而它放过了一次真实的误伤：一处缩进写错让 `pipeline.py` 成了
            # `IndentationError`，三条 AST 断言一起红 —— 期待的那个名字**在**清单里，
            # 于是判定为「命中」。**多红出来的那几条是信号，不是噪音**：
            # 它们说明变异的影响面比声称的大。
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
