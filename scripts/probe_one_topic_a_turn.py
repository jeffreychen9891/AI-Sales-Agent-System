"""反向探针：一轮只认一类（`tests/contracts/test_one_topic_a_turn.py`）。

## 它上一版演的是「有人把洞堵上」—— 而洞堵上了

上一版叫 `probe_topic_gate_tripwire.py`，配的是一根**钉住缺陷**的绊线：
把 `topic_text` 改成只认最后一条 update，看那几条断言是不是会红。
**它们红了**，于是这一批照那个方向把它改成了实现（用户 2026-09-16 裁定）。

所以这一支现在反过来演：**把那次收紧撤掉**，看新的那几条是不是真的会叫。

    python scripts/probe_one_topic_a_turn.py

| | 变异 | 演的是 |
|---|---|---|
| A | `topic_text` 退回「每条都接上去」 | **收紧被撤掉** —— 与累积串等价的那条路回来了 |
| B | `superseded_topic_updates` 恒回 0 | **丢弃又变安静了** —— 少认了而没人记 |
| C | `on_request` 的话题闸整个失效 | 闸塌了（上一版留下来的那一支） |

**B 是这一支真正的理由**：A 改的是能不能少认，B 改的是**少认了有没有人知道**。
一次安静的丢弃只是把旧缺陷换成新缺陷，而它不会让任何既有用例变红 ——
除非有人专门为它写一条。
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
TARGET = "tests/contracts/test_one_topic_a_turn.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 5
CONTROL_BASELINE_PASSED = 1712

SELECTION = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "material_selection.py"
)

TOUCHED = (SELECTION,)

#: 今天那一段：只把最后一条接上去。
TODAY = """    if updates:
        parts.append(updates[-1])"""

#: 收紧之前那一段：每一条都接上去。**这是这一批改掉的那一行的原样**。
BEFORE_THE_FIX = """    for value in updates:
        parts.append(value)"""

#: 「数出来的那个数」today / 撤掉之后。
COUNTS = "    return max(0, len(matched) - 1)"
COUNTS_NOTHING = "    return 0"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · `topic_text` 退回「每条都接上去」（收紧被撤掉）",
            [Mutation(SELECTION, TODAY, BEFORE_THE_FIX)],
            [
                "test_only_the_last_update_of_the_turn_counts",
                "test_the_accumulated_string_is_no_longer_reachable_through_updates",
            ],
            [],
        ),
        (
            "B · `superseded_topic_updates` 恒回 0（丢弃又变安静了）",
            [Mutation(SELECTION, COUNTS, COUNTS_NOTHING)],
            ["test_the_discarded_updates_are_counted"],
            [],
        ),
        (
            "C · `on_request` 的话题闸失效（当作无条件放行）",
            [
                Mutation(
                    SELECTION,
                    '    topics = declaration.get("topics") or ()\n'
                    "    if not topics:\n        return True",
                    '    topics = declaration.get("topics") or ()\n'
                    "    if True:\n        return True",
                )
            ],
            [
                "test_one_topic_a_turn_is_still_the_small_case",
                "test_the_accumulated_string_is_no_longer_reachable_through_updates",
            ],
            [
                "test_a_frame_with_no_topic_slot_offers_no_topic_gated_material",
                "test_a_reactive_material_is_not_volunteered",
                "test_an_update_to_another_slot_does_not_open_the_gate",
                "test_the_offered_set_does_not_grow_with_the_conversation",
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
