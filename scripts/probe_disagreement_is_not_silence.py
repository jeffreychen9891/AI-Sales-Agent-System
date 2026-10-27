#!/usr/bin/env python
"""反向探针：谈不拢不以对客户沉默结账。

    python scripts/probe_disagreement_is_not_silence.py

| | 变异 | 演的是 |
|---|---|---|
| A | 补底那条整个不干 | **运维实测那六次的原样**：模型想回话，计划没提名，整轮作废 |
| B | 静默打法也补 | 一个本该沉默的回合突然开口 —— **这个放宽最容易变成的那个洞** |
| C | 模型提名过了也照补 | 平台覆盖模型的计划，而且是悄悄覆盖 |
| D | 丢弃变成默认（不传收集器也丢） | 每个不知情的调用方悄悄换掉语义 |

## B 与 D 是这一批的两条边

**这一批做的是一次放宽**，而放宽最容易在两处变成洞：
把不该开口的回合弄开口（B），把「必须显式接下记录责任」变成默认（D）。
**它们都不会自己说话**，所以这两支比 A 更要紧。
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
TARGET = "tests/contracts/test_disagreement_is_not_silence.py"

#: 实测 2026-09-17，随门禁回填。
BASELINE_PASSED = 10
CONTROL_BASELINE_PASSED = 1866

VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
TOUCHED = (VALIDATION,)

#: A —— 补底整个不干。
FLOORS = "    floored = _floor_a_reply_candidate(plan, strategy=strategy, profile=profile)"
FLOORS_NOT = "    floored = None"

#: B —— 静默打法也补。
SKIPS_SILENT = "    if strategy in SILENT_STRATEGIES:\n        return None\n    allowed ="
SKIPS_NOTHING = "    if False:\n        return None\n    allowed ="

#: C —— 模型提名过了也照补。
ALREADY_THERE = (
    '    if any(candidate["kind"] == "send_message" for candidate in candidates):\n'
    "        return None\n"
)
IGNORES_IT = "    if False:\n        return None\n"

#: D —— 丢弃变成默认。
OPT_IN = "    if dropped is not None:"
ALWAYS = "    if True:\n        dropped = [] if dropped is None else dropped"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 补底不干",
            [Mutation(VALIDATION, FLOORS, FLOORS_NOT)],
            ["test_a_plan_that_forgot_to_nominate_a_reply_gets_one"],
            [],
        ),
        (
            "B · 静默打法也补",
            [Mutation(VALIDATION, SKIPS_SILENT, SKIPS_NOTHING)],
            # **这一支抓到过一个空守卫**：第一版直接拿在架配包的 `wait` 跑，
            # 而它的 `allowed_intent_kinds` 是 `[]`，后一道闸先返回、
            # 把静默那道遮住了 —— 把静默那道整个删掉，用例照样绿。
            # 改成「构造一个静默打法却授权发消息的配包」之后它才红。
            ["test_a_silent_strategy_is_never_given_a_reply_candidate"],
            [],
        ),
        (
            "C · 模型提名过也照补",
            [Mutation(VALIDATION, ALREADY_THERE, IGNORES_IT)],
            ["test_the_platform_does_not_overwrite_a_reply_the_model_did_nominate"],
            # **连带两条，都该红**：重复候选那条（平台补出了第二个
            # `send_message`）与那条按槽位重问的 —— 它们钉的正是
            # 「候选不许多出来一条」。
            [
                "test_generation_may_not_duplicate_a_planned_intent",
                "test_a_rejection_that_can_name_the_slot_now_asks",
            ],
        ),
        (
            "D · 丢弃变成默认",
            [Mutation(VALIDATION, OPT_IN, ALWAYS)],
            ["test_the_default_is_still_to_refuse_the_whole_document"],
            # **这条连带是这支最该看的**：`test_generation_may_not_add_an_intent`
            # 是这道闸原本的守卫。丢弃一变默认，**它当场红** ——
            # 也就是说「悄悄把语义换掉」这件事不会无声无息。
            ["test_generation_may_not_add_an_intent"],
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
    return 1 if (silent or misaimed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
