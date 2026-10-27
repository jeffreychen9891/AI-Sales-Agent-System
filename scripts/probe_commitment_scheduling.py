#!/usr/bin/env python
"""反向探针：11d 那两族守卫（承诺锚点 · 每条承诺一个定时器）。

## 为什么这一支非有不可

11d 改的东西有一个共同的形状：**做错了不会报错，只会少发生一件事。**

* 锚点从 `SCHEDULABLE_ANCHORS` 里掉出去 → 规则被**静默跳过**；
* 模板里少了 `{commitment_id}` → 键撞上，而写入方是
  `ON CONFLICT ... DO UPDATE SET due_at = GREATEST(...)`：
  **第二条承诺把第一条的死线往后推**，早的那个从此不响；
* `INSERT` 的列清单里少了 `commitment_id` → 那一行照样写进去，
  只是**外键变成一条不可能被违反的约束**。

三条都不会红。所以每一条都得有人证明它今天**会**红。

    python scripts/probe_commitment_scheduling.py

## C 支为什么要 `repin`

在架配包是内容寻址的（`identity.content_hash`）。动了 `profile.json` 不重新盖章，
先红的是哈希那道闸 —— 那时量到的是「哈希守卫会红」，不是本支想问的东西。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeError,
    ProbeSession,
    control_runner,
    failed_names,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGETS = [
    "tests/contracts/test_commitment_scheduling.py",
    "tests/contracts/test_time_rule_producer.py",
]

#: 实测 2026-09-16。
BASELINE_PASSED = 22
CONTROL_BASELINE_PASSED = 1676

TIME_RULES = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "time_rules.py"
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
PROFILE = ROOT / "profiles" / "synthetic-carrier" / "profile.json"

TOUCHED = (TIME_RULES, PIPELINE, PROFILE)


def reseal() -> None:
    """重新盖章 —— 否则 C 支量到的是哈希守卫。"""

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            str(PYTHON),
            str(ROOT / "scripts" / "seal_profile_package.py"),
            "profiles/synthetic-carrier",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ProbeError(f"重新盖章失败，C 支的结论不可信：{completed.stdout}{completed.stderr}")


def main() -> int:
    run = suite_runner(ROOT, PYTHON, TARGETS)
    control = control_runner(ROOT, PYTHON, TARGETS)

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str], bool]] = [
        (
            "A · 把 `commitment_due_at` 从可调度锚点里拿掉（退回 11d 之前）",
            [
                Mutation(
                    TIME_RULES,
                    "SCHEDULABLE_ANCHORS = frozenset(\n"
                    '    {"frame_entered_at", "last_outbound_at", "commitment_due_at"}\n'
                    ")",
                    'SCHEDULABLE_ANCHORS = frozenset({"frame_entered_at", "last_outbound_at"})',
                )
            ],
            [
                "test_a_commitment_anchor_without_a_commitment_is_refused_at_the_call",
                "test_an_anchor_this_milestone_cannot_resolve_is_refused_at_the_call",
                "test_no_shipped_rule_is_one_this_milestone_can_never_create",
            ],
            [],
            False,
        ),
        (
            "B · 把 `commitment_id` 从占位符词表里拿掉",
            [
                Mutation(
                    TIME_RULES,
                    "DEDUPE_PLACEHOLDERS = frozenset("
                    '{"emit_kind", "target_frame_id", "commitment_id"})',
                    'DEDUPE_PLACEHOLDERS = frozenset({"emit_kind", "target_frame_id"})',
                )
            ],
            # **`test_no_shipped_rule...` 不在这一组**，而这是量出来的：
            # `unschedulable_rules` 判的是「模板里有没有 `{commitment_id}` 这个字面」，
            # 不读占位符词表 —— 词表被删之后在架配包那条仍然绿。
            # 两条判据各管一半，写清楚谁管哪一半比让它们看起来重叠有用。
            [
                "test_a_template_naming_a_commitment_with_none_supplied_is_refused",
                "test_a_template_naming_anything_else_is_refused_by_name",
                "test_two_commitments_in_one_frame_get_two_keys",
                "test_two_commitments_in_one_frame_render_two_different_keys",
            ],
            [],
            False,
        ),
        (
            "C · 在架配包的承诺规则退回两段式模板（键认不出两条承诺）",
            [
                Mutation(
                    PROFILE,
                    '"dedupe_template": "{emit_kind}:{target_frame_id}:{commitment_id}"',
                    '"dedupe_template": "{emit_kind}:{target_frame_id}"',
                    expected_hits=2,
                )
            ],
            [
                "test_no_shipped_rule_is_one_this_milestone_can_never_create",
                "test_the_shipped_commitment_rules_can_tell_two_commitments_apart",
            ],
            [],
            True,
        ),
        (
            "D · 往 `DEFERRED_KINDS` 里放回一个 kind（「被跳过是预期的」）",
            [
                Mutation(
                    TIME_RULES,
                    "DEFERRED_KINDS: frozenset[str] = frozenset()",
                    'DEFERRED_KINDS: frozenset[str] = frozenset({"customer_promise_due"})',
                )
            ],
            ["test_the_deferral_list_is_empty_and_that_is_the_assertion"],
            [],
            False,
        ),
        (
            "E · 从 `INSERT INTO scheduled_events` 的列清单里拿掉 `commitment_id`",
            [
                Mutation(
                    PIPELINE,
                    "                target_stack_version, commitment_id, payload_ref, status,",
                    "                target_stack_version, payload_ref, status,",
                )
            ],
            ["test_the_scheduled_events_writer_names_the_commitment_column"],
            [],
            False,
        ),
        (
            "F · 定时器改挂内存里那份行（重放时 id 在库里不存在）",
            [
                Mutation(
                    PIPELINE,
                    """            stored_due_rows(
                connection,
                tenant_id=prepared.tenant_id,
                conversation_id=prepared.conversation_id,
                dedupe_keys=[
                    str(row["_dedupe_key"])
                    for row in prepared.admitted_commitments
                    if row.get("_dedupe_key")
                ],
            ),""",
                    """            [dict(row) for row in prepared.admitted_commitments],""",
                )
            ],
            ["test_the_timer_hangs_off_the_row_the_database_has"],
            [],
            False,
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(passed=BASELINE_PASSED, control_passed=CONTROL_BASELINE_PASSED),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral, needs_reseal in probes:
            result = session.probe(label, mutations, repin=reseal if needs_reseal else None)
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
        # C 支改完还原之后哈希也要跟着还原，否则树是脏的。
        reseal()

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
