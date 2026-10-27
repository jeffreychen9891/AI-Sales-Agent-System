#!/usr/bin/env python
"""反向探针：D3（`evidence_event_ids` 复数）那几条守卫，逐条验它会红。

守的是 `tests/contracts/test_slot_writeback_and_closing_frame.py` 里
2026-09-15 新加的三条，外加那条 `@pytest.mark.security` 的旧用例。

| 支 | 把什么改回去 | 谁该红 |
|---|---|---|
| **E1** | 写回改回单值（回到 D3 之前） | 四条一起 |
| **E2** | 无条件追加（**不看值变没变**） | **那条 `@pytest.mark.security` 的** |
| **E3** | 值变了也追加 —— 同 E2，换一种写法 | 同上 |
| **E4** | 读存量行时不认单数键 | 存量兼容那条 |
| **E5** | 上界从 8 改成 9（只改代码那一份） | 两份跨包相等那条 |

**E2 / E3 是这支探针的重点。** 第一版实现就是无条件追加，
而那条带 `@pytest.mark.security` 的用例**当场把它顶红了** —— **它红得对**：

> The evidence describes the value that is there now, **not a history of it**.
> Keeping the first would make provenance point at a message
> **whose value has since been overwritten** -- an answer that is confidently wrong.

图纸要数组（一个槽位可由多条证据支撑）与那条用例**不矛盾**，
是第一版把两件事混了：**值变了 -> 重置**（旧消息支撑的是另一个值），
**值没变 -> 追加**（同一个值被再次确认）。
**判据因此是「值变没变」，而不是「有没有已记录的证据」** ——
这一条是被那条用例的**论证**逼出来的，不是被它的断言逼出来的。

    python scripts/probe_slot_evidence_array.py
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
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_slot_writeback_and_closing_frame.py"

BASELINE_PASSED = 23

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1667

STACK = ROOT / "services/worker/src/fastagent_v2/worker/target_stack.py"

TOUCHED = (STACK,)

WRITE = (
    '            "evidence_event_ids": _appended_evidence(\n'
    "                recorded, str(update[\"evidence_event_id\"]), value_changed=value_changed\n"
    "            ),\n"
)
WRITE_SINGLE = '            "evidence_event_id": str(update["evidence_event_id"]),\n'

RESET = "    if value_changed:\n        return [event_id]\n"

CHANGED = '        value_changed = frame["slots"].get(name) != update["value"]'
NEVER_CHANGED = "        value_changed = False"

LEGACY = (
    '    single = recorded.get("evidence_event_id")\n'
    "    return [str(single)] if single else []"
)
LEGACY_BLIND = "    return []"

BOUND = "EVIDENCE_IDS_MAX = 8"

SECURITY = "test_a_higher_or_equal_confidence_update_replaces_the_earlier_evidence"
BOTH = "test_the_same_value_confirmed_twice_keeps_both_pieces_of_evidence"
LEGACY_TEST = "test_evidence_read_from_a_row_written_before_the_array_existed"
BOUND_TEST = "test_the_evidence_list_stops_at_its_declared_bound"
LANDS = "test_each_applied_slot_records_where_it_came_from"
LOWER = "test_a_lower_confidence_update_does_not_overwrite_stronger_evidence"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "E1 · 写回改回单值（回到 D3 之前）",
            [Mutation(STACK, WRITE, WRITE_SINGLE)],
            [BOTH, LEGACY_TEST, BOUND_TEST, LANDS, LOWER, SECURITY],
            # 退回单值会让**帧上那条落点**也说话（2026-09-16 对照跑）。
            # E1 是唯一有连带的一支，另外四支都是零 —— 也就是说
            # 「值变了就重置」这条判据，全仓只有本文件在守。
            ["test_a_slot_update_from_the_model_actually_lands_on_the_frame"],
        ),
        (
            "E2 · 无条件追加（不看值变没变）—— **第一版实现就是这个**",
            [Mutation(STACK, RESET, "")],
            [SECURITY],
            [],
        ),
        (
            "E3 · 同上，换一种写法：`value_changed` 恒 False",
            [Mutation(STACK, CHANGED, NEVER_CHANGED)],
            [SECURITY],
            [],
        ),
        (
            "E4 · 读存量行时不认单数键",
            [Mutation(STACK, LEGACY, LEGACY_BLIND)],
            [LEGACY_TEST],
            [],
        ),
        (
            "E5 · 上界只改代码那一份（跨包两个数不再相等）",
            [Mutation(STACK, BOUND, "EVIDENCE_IDS_MAX = 9")],
            [BOUND_TEST],
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
            # **两边都按集合相等判**，不是「包含」：多红出来的那几条是信号，
            # 不是噪音 —— 它们说明这次变异的影响面比声称的大。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    ok = all(red and hit and other for _, red, hit, other in verdicts)
    print("ALL RED AND AIMED" if ok else "SOMETHING DID NOT GO RED (or went red elsewhere)")
    if ok:
        print()
        print("**E2 / E3 是重点**：第一版实现就是无条件追加，而那条带 security 标记的")
        print("旧用例当场把它顶红了 —— 它的**论证**（不是断言）给出了正确的判据：")
        print("值变了就重置，值没变才追加。图纸要的数组与它不矛盾，是我把两件事混了。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
