#!/usr/bin/env python
"""反向探针：没问价就不报价（话术按「这一轮问了」，文件不动）。

    python scripts/probe_only_quote_when_asked.py

| | 变异 | 演的是 |
|---|---|---|
| A | 话术改回看槽位现值 | **这一批之前的样子**：槽黏着「花钱」，每一轮报价都在候选里 |
| B | 话术改成一个都不认 | **过严那一侧**：他问了也拿不到 —— 一段都不给同样能让 A 变绿 |
| C | 词表那一道不过滤 | 一条 update 里塞累积串，**两组素材一起点亮** |
| D | 「配到一半」判成一个都不认 | **我第一版就是它**：空词表把每个值都判非法 |
| E | 文件那一侧也改成「这一轮问了」 | 裁定说文件不动 —— 「他又问一次就该又拿到价目表」 |

## A 与 B 是这一格的两条边

「没问不给」有两种错法：**给多了**（A，改之前的样子）与
**给没了**（B）。只有 A 的探针，一个「一段都不给」的实现能拿满分。
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
TARGET = "tests/contracts/test_only_quote_a_price_when_asked.py"

#: 实测 2026-09-18，随门禁回填。
BASELINE_PASSED = 8
CONTROL_BASELINE_PASSED = 1980

SELECTION = ROOT / "services/worker/src/fastagent_v2/worker/material_selection.py"
TOUCHED = (SELECTION,)

#: A —— 话术改回看槽位现值。**不能只改调用点**（那样 `frame` 没了会 NameError），
#: 所以改的是那个函数的返回：让「这一轮问了」把存着的值也算进来。
ASKED_ONLY = """    updates = _legal_update_values(definition, understanding=understanding)
    return updates[-1] if updates else \"\""""
ASKED_PLUS_STORED = """    updates = _legal_update_values(definition, understanding=understanding)
    if updates:
        return updates[-1]
    return str((definition.get("__sticky__") or {}).get("value") or "花钱")"""

#: B —— 话术一个都不认。
NEVER_ASKED = """    updates = _legal_update_values(definition, understanding=understanding)
    return \"\""""

#: C —— 词表那一道不过滤。
FILTERS = "    return [value for value in values if value in found[1]]"
FILTERS_NOT = "    return values"

#: D —— 「配到一半」判成一个都不认（**我第一版就是它**）。
HALF_CONFIGURED_OK = """    found = topic_vocabulary(definition)
    if found is None:
        return values"""
HALF_CONFIGURED_LOCKED = """    found = topic_vocabulary(definition)
    if found is None:
        return []"""

#: E —— 文件那一侧也改成「这一轮问了」。
ASSET_KEEPS_STORED = """    stored = (frame or {}).get("slots") or {}"""
ASSET_DROPS_STORED = """    stored: dict[str, object] = {}
    _unused = (frame or {}).get("slots") or {}"""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 话术改回看槽位现值",
            [Mutation(SELECTION, ASKED_ONLY, ASKED_PLUS_STORED)],
            [
                "test_a_half_configured_package_is_not_locked_shut",
                "test_a_sticky_slot_no_longer_puts_the_price_in_front_of_the_model",
                "test_an_accumulated_string_cannot_light_two_topic_groups",
                "test_files_still_follow_the_stored_slot",
            ],
            # **连带很宽，而那正是这一格的分量**：话题判据是全仓读得最多的一处，
            # 这里改一个字，另外几个文件里守同一件事的用例一起说话。
            [
                "test_a_frame_with_no_topic_slot_offers_no_topic_gated_material",
                "test_a_reactive_material_is_not_volunteered",
                "test_a_sticky_slot_no_longer_opens_the_reactive_gate",
                "test_an_accumulated_string_in_one_update_lights_nothing",
                "test_an_update_to_another_slot_does_not_open_the_gate",
                "test_the_offered_set_does_not_grow_with_the_conversation",
            ],
        ),
        (
            "B · 话术一个都不认",
            [Mutation(SELECTION, ASKED_ONLY, NEVER_ASKED)],
            [
                "test_a_half_configured_package_is_not_locked_shut",
                "test_an_accumulated_string_cannot_light_two_topic_groups",
                "test_asking_this_turn_puts_it_back",
            ],
            # **连带很宽，而那正是这一格的分量**：话题判据是全仓读得最多的一处，
            # 这里改一个字，另外几个文件里守同一件事的用例一起说话。
            [
                "test_an_accumulated_string_in_one_update_lights_nothing",
                "test_an_on_request_material_is_offered_again",
                "test_the_offered_set_does_not_grow_with_the_conversation",
                "test_the_stage_gate_does_not_touch_the_reactive_bucket",
                "test_this_turns_slot_update_counts_and_not_only_the_stored_value",
            ],
        ),
        (
            "C · 词表那一道不过滤",
            [Mutation(SELECTION, FILTERS, FILTERS_NOT)],
            # ⚠️ **这一支第一次跑是全绿的**：词表那一道是本批改的，
            # 而本文件当时一条都没钉它（我把它留在了 `test_one_topic_a_turn`）。
            # **一支打不到自己这一批改动的探针，证明的是这一批的用例有洞。**
            ["test_an_accumulated_string_cannot_light_two_topic_groups"],
            [
                "test_an_accumulated_string_in_one_update_lights_nothing",
                "test_the_offered_set_does_not_grow_with_the_conversation",
            ],
        ),
        (
            "D · 配到一半判成一个都不认",
            [Mutation(SELECTION, HALF_CONFIGURED_OK, HALF_CONFIGURED_LOCKED)],
            ["test_a_half_configured_package_is_not_locked_shut"],
            # 连带在资料那一侧：两边共用 `_legal_update_values`，
            # 所以「配到一半」判错了，文件那一档也跟着拿不到。
            ["test_an_on_request_asset_is_offered_again_because_he_asked_again"],
        ),
        (
            "E · 文件那一侧也丢掉槽位现值",
            [Mutation(SELECTION, ASSET_KEEPS_STORED, ASSET_DROPS_STORED)],
            ["test_files_still_follow_the_stored_slot"],
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
