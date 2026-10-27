#!/usr/bin/env python
"""反向探针：记下来的那几件事真的回到了提示词里。

    python scripts/probe_memory_reaches_the_prompt.py

| | 变异 | 演的是 |
|---|---|---|
| A | 永远回空 | **这一批之前的样子**：记忆写得进去，模型永远看不见 |
| B | 按字典序排 | 平台替商家重排了优先级 —— 而它猜不出哪一条更重要 |
| C | 不按声明过滤 | 商家删掉的字段照样进提示词 |
| D | 装不下的不数 | **安静地少给**：读起来与「这个客户没有记忆」一模一样 |
| E | 跳过大的去捡小的 | 按大小重排优先级，还是那件事 |
| F | 截断不留记号 | 模型分不出「这就是全部」与「后面还有」 |
| G | schema 上限改回 32 | **T300 的三个数被单独改了一个** |
| H | 份额抬到 2400 | 同上，从另一头改 |
| I | 少接一个判断步 | `last_outbound` 那一批栽过的形状 |
| J | 留痕把值也记上 | 拒绝记录里出现客户事实 —— E4-2 那次事故的形状 |

## B 与 E 是同一件事的两个方向

顺序是商家用**声明顺序**表达的优先级。
「按字典序排」与「跳过装不下的去捡小的」都是平台在替它重排 ——
**两种都不会自己说话**，配包封得了版、对话照跑，只是模型看见的次序不是商家要的。

## G / H 钉的是 T300 那句「三者只能一起定」

份额、由份额反推的条数、schema `maxItems` —— 单改一个，那条算式就不再自洽，
而没有任何东西会红。这两支各从一头改一个数。
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
TARGET = "tests/contracts/test_memory_reaches_the_prompt.py"

#: 实测 2026-09-18，随门禁回填。
BASELINE_PASSED = 15
CONTROL_BASELINE_PASSED = 1928

MEMORY = ROOT / "packages/runtime/src/fastagent_v2/runtime/memory_prompt.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
SCHEMA = ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
TOUCHED = (MEMORY, PIPELINE, SCHEMA)

#: A —— 永远回空（这一批之前的样子）。
RETURNS = "    return shown, missing"
RETURNS_NOTHING = "    return [], 0"

#: B —— 按字典序排。
DECLARED_ORDER = "    for entry in declared or []:"
ALPHABETICAL = (
    "    for entry in sorted(declared or [], key=lambda item: str(item.get('key'))):"
)

#: C —— 不按声明过滤（键只要库里有就带）。
FILTERS = "        if not key or key not in values:"
FILTERS_NOT = "        if not key:"

#: D —— 装不下的不数。
COUNTS = """        if spent + cost > budget:
            stopped = True
            missing += 1
            continue"""
COUNTS_NOT = """        if spent + cost > budget:
            stopped = True
            continue"""

#: E —— 不停下来，跳过大的继续捡小的。
STOPS = """        if stopped:
            missing += 1
            continue"""
KEEPS_PACKING = """        if stopped:
            stopped = False"""

#: F —— 截断不留记号。
MARKS = 'return encoded[:budget].decode("utf-8", errors="ignore") + ELLIPSIS'
MARKS_NOT = 'return encoded[:budget].decode("utf-8", errors="ignore")'

#: G / H —— T300 那三个数，各从一头改一个。
CEILING = '"maxItems": 8,'
CEILING_BACK = '"maxItems": 32,'
SHARE = "REMEMBERED_BYTES = 1_200"
SHARE_DOUBLED = "REMEMBERED_BYTES = 2_400"

#: I —— 少接一个判断步（第二个 `understand` 调用点）。
SECOND_SITE = """                # **两个 understand 调用点都要带它**，与上一行逐字同一条理由。
                known_memories=prepared.known_memories,
                memories_not_shown=prepared.memories_not_shown,"""
SECOND_SITE_NOT = (
    "                # **两个 understand 调用点都要带它**，与上一行逐字同一条理由。"
)

#: J —— 留痕把值也记上。
RECORDS_KEYS = 'Jsonb({"fields": sorted(fields), "not_shown": int(not_shown)}),'
RECORDS_VALUES = (
    'Jsonb({"fields": sorted(fields), "not_shown": int(not_shown), "value": ""}),'
)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 永远回空",
            [Mutation(MEMORY, RETURNS, RETURNS_NOTHING)],
            [
                "test_both_judging_steps_carry_the_values",
                "test_the_order_is_the_merchants_not_the_platforms",
                "test_a_field_the_merchant_no_longer_declares_is_not_shown",
                "test_what_does_not_fit_is_counted_not_silently_dropped",
                # 「停在第一条装不下的」那条也红：回空之后没有「第一条」
                # 可停 —— 它等的是 `hidden == 2`。
                "test_it_stops_at_the_first_one_that_does_not_fit",
                "test_a_long_value_is_clipped_where_the_model_can_see_it",
            ],
            [],
        ),
        (
            "B · 按字典序排",
            [Mutation(MEMORY, DECLARED_ORDER, ALPHABETICAL)],
            ["test_the_order_is_the_merchants_not_the_platforms"],
            [],
        ),
        (
            "C · 不按声明过滤",
            [Mutation(MEMORY, FILTERS, FILTERS_NOT)],
            # 两条：不过滤之后，声明了而**库里没有**的字段也会带一条空值出来，
            # 于是第一条那份「只有 industry」也对不上了。
            [
                "test_a_field_the_merchant_no_longer_declares_is_not_shown",
                "test_both_judging_steps_carry_the_values",
            ],
            [],
        ),
        (
            "D · 装不下的不数",
            [Mutation(MEMORY, COUNTS, COUNTS_NOT)],
            [
                "test_what_does_not_fit_is_counted_not_silently_dropped",
                "test_it_stops_at_the_first_one_that_does_not_fit",
            ],
            [],
        ),
        (
            "E · 跳过大的捡小的",
            [Mutation(MEMORY, STOPS, KEEPS_PACKING)],
            ["test_it_stops_at_the_first_one_that_does_not_fit"],
            [],
        ),
        (
            "F · 截断不留记号",
            [Mutation(MEMORY, MARKS, MARKS_NOT)],
            ["test_a_long_value_is_clipped_where_the_model_can_see_it"],
            [],
        ),
        (
            "G · schema 上限改回 32",
            [Mutation(SCHEMA, CEILING, CEILING_BACK)],
            ["test_the_three_numbers_of_t300_agree"],
            # **连带正是这一支要证的事**：封版那条用例在另一个文件里，
            # 而它与本文件那条钉的是同一个 8 —— 改一处，两处一起红。
            ["test_the_count_ceiling_is_eight_and_it_comes_from_the_share"],
        ),
        (
            "H · 份额抬到 2400",
            [Mutation(MEMORY, SHARE, SHARE_DOUBLED)],
            ["test_the_three_numbers_of_t300_agree"],
            # 同 G，从另一头改：**份额与上限是绑在一起的两个数**。
            ["test_the_count_ceiling_is_eight_and_it_comes_from_the_share"],
        ),
        (
            "I · 少接一个判断步",
            [Mutation(PIPELINE, SECOND_SITE, SECOND_SITE_NOT)],
            ["test_every_judging_call_site_passes_it"],
            [],
        ),
        (
            "J · 留痕把值也记上",
            [Mutation(PIPELINE, RECORDS_KEYS, RECORDS_VALUES)],
            [
                "test_what_was_sent_is_recorded_and_the_record_carries_no_values"
            ],
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
