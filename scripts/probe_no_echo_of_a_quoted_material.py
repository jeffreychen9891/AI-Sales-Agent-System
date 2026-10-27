#!/usr/bin/env python
"""反向探针：引了素材不许复述 · 主动话术只发一次。

    python scripts/probe_no_echo_of_a_quoted_material.py

| | 变异 | 演的是 |
|---|---|---|
| A | 复述检查整个不干 | **用户 2026-09-17 16:40 实测那一条原样**：同一条消息里说两遍 |
| B | 阈值抬到 40 | 实测那条重合 20 字，抬到 40 它就漏过去 —— **一个「看起来在守」的阈值** |
| C | 把复述的片段连素材一起清光 | 把一次复述换成一次沉默 —— 今天刚裁过不许这样 |
| D | 主动话术那道「只发一次」不干 | 一条 `proactive` 话术永远在被递 |
| E | `on_request` 也滤 | 他又问一次价格，拿不到那句报价 |
| F | 过滤写回「不是 `on_request`」 | **复核 2026-09-18 退回的那一条** |

F 演的是：「没声明 `availability`」是一个合法档、含义是「每轮都递」，
而排除式写法把它一并滤掉 —— 包能封、能发、对话照跑，只有客户少收到东西。

## B 与 E 是这一批的两条边

**这一批新立了一个阈值和一条过滤**，而这两样最容易变成洞：
阈值调松 = 守卫还在、而什么都拦不住（B）；过滤放宽 = 该给的不给了（E）。
**两样都不会自己说话。**
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
TARGET = "tests/contracts/test_no_echo_of_a_quoted_material.py"

#: 实测 2026-09-17，随门禁回填。
BASELINE_PASSED = 14
CONTROL_BASELINE_PASSED = 1876

VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
SELECTION = ROOT / "services/worker/src/fastagent_v2/worker/material_selection.py"
TOUCHED = (VALIDATION, SELECTION)

#: A —— 复述检查不干。
RUNS = "    if echoed is not None:"
RUNS_NOT = "    if False:"

#: B —— 阈值抬到实测值之上。
THRESHOLD = "ECHOED_MATERIAL_CHARS = 12"
TOO_LOOSE = "ECHOED_MATERIAL_CHARS = 40"

#: C —— 连素材一起清光。
#:
#: **第一版这个变异是假的**：它把「素材片段直接留下」那道拿掉，
#: 而素材片段的内容是 ref 字符串、与素材正文几乎不重合 ——
#: **于是它照样被留下**，探针全绿。一个什么都没改的变异证明不了任何事。
#: 换成直接把整段清空。
KEEPS_MATERIAL = "        segments[:] = kept"
CLEARS_ALL = "        segments[:] = []"

#: D —— 主动话术那道过滤不干。
FILTERS = (
    '        if ref in already_sent and declaration.get("availability") in (PROACTIVE, TIMER):'
)
FILTERS_NOT = "        if False:"

#: F —— 写回排除式（复核 2026-09-18 退回的那个写法）。
#:
#: `content_materials` 的 schema `required` 只有 `ref`，所以
#: **「没声明 `availability`」是一个合法档**，而 `str(None) != "on_request"`
#: 为真 —— 它被滤掉了，与 schema 和 `_is_offered` 说的「每轮都递」相反。
#:
#: **退回时这个写法自身与 control 全绿**（复核探针 G）——
#: 两种写法只在这一档上给出相反结果，而没有一条用例说得出哪种对。
EXHAUSTIVE = 'declaration.get("availability") in (PROACTIVE, TIMER)'
EXCLUSIVE = 'str(declaration.get("availability")) != ON_REQUEST'

#: E —— `on_request` 也滤。
#: **锚点必须只命中话术那一行** —— 资料那边（`select_assets`）还写着
#: 排除式，第一版的锚点会改到它身上，而那不是这一批要量的东西。
KEEPS_ON_REQUEST = "in (PROACTIVE, TIMER):"
FILTERS_EVERYTHING = "in (PROACTIVE, TIMER, ON_REQUEST):"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 复述检查不干",
            [Mutation(VALIDATION, RUNS, RUNS_NOT)],
            [
                "test_the_sentence_that_restates_a_quoted_material_is_dropped",
                "test_the_merchants_own_words_survive",
            ],
            [],
        ),
        (
            "B · 阈值抬到 40",
            [Mutation(VALIDATION, THRESHOLD, TOO_LOOSE)],
            # 三条：两条行为的，加上那条**专门钉阈值把两种情形分在两边**的。
            [
                "test_the_sentence_that_restates_a_quoted_material_is_dropped",
                "test_the_merchants_own_words_survive",
                "test_the_threshold_separates_the_two_measured_cases",
            ],
            [],
        ),
        (
            "C · 连素材一起清光",
            [Mutation(VALIDATION, KEEPS_MATERIAL, CLEARS_ALL)],
            # **「丢复述、留原文」那条是这一支的全部理由** ——
            # 把一次复述换成一次沉默，今天刚裁过不许这样。
            [
                "test_the_merchants_own_words_survive",
                "test_the_sentence_that_restates_a_quoted_material_is_dropped",
                "test_a_fair_echo_of_a_number_is_not_a_restatement",
            ],
            [],
        ),
        (
            "D · 主动话术不滤",
            [Mutation(SELECTION, FILTERS, FILTERS_NOT)],
            # **形状那条也红，而且该红**：D/E/F 动的是同一行，
            # 而那条钉的正是「这一行要正向列举那两档」。
            [
                "test_a_proactive_material_is_offered_once",
                "test_the_filter_names_the_two_tiers_it_means",
            ],
            [],
        ),
        (
            "E · on_request 也滤",
            [Mutation(SELECTION, KEEPS_ON_REQUEST, FILTERS_EVERYTHING)],
            [
                "test_an_on_request_material_is_offered_again",
                "test_the_filter_names_the_two_tiers_it_means",
            ],
            [],
        ),
        (
            "F · 写回「不是 on_request」",
            [Mutation(SELECTION, EXHAUSTIVE, EXCLUSIVE)],
            # 一条行为的 + 一条形状的。**退回时这两条都不存在**，
            # 所以那个写法自身与 control 全绿。
            [
                "test_a_material_without_an_availability_is_offered_every_turn",
                "test_the_filter_names_the_two_tiers_it_means",
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
    return 1 if (silent or misaimed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
