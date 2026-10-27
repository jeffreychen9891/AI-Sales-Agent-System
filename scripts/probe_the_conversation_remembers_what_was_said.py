#!/usr/bin/env python
"""反向探针：T200 / E4 第 4 批 —— 滚动摘要 + 上下文预算。

本批的主张逐条要有一支探针证明**它真的会红**：

1. **预算有界**（排期表 §4.6 那条硬判据）—— 把收口去掉，请求就随 N 长；
2. **预留与界是一个数** —— 改一个，另一个不跟着，账本就与闸不是一把尺子；
3. **裁定八靠结构** —— 让写摘要那一处去读旧行，结构保证就没了；
4. **一个回合一行** —— 去掉唯一索引，重放会塞重复；
5. **作废的行不进提示词** —— 去掉 `status` 过滤，人工判断被静默推翻；
6. **摘要不进下游那两个提示词** —— 让它进去，理解步预算被吃掉一大块。

⚠️ **带 `control=`**：连带集合量出来，不是想出来。

    python scripts/probe_the_conversation_remembers_what_was_said.py
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
TARGET = "tests/contracts/test_the_conversation_remembers_what_was_said.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 13

#: `tests/contracts` 减去本文件之后的那个数。**量出来的，不是算出来的** ——
#: 上一批我拿「总数减本文件」算过一次，harness 当场拒绝开跑。
#: ⚠️ **2026-09-20 rebase 到 T299 合入之后的 main，2181 -> 2192**（实测）——
#: T299 那 11 条进来了。**这个数每次 rebase 都要重量**，
#: 而 harness 会在开跑前替我发现它过期（它拒绝「声明的数与实测不符」）。
CONTROL_BASELINE_PASSED = 2192

SUMMARY = ROOT / "packages/runtime/src/fastagent_v2/runtime/summary_prompt.py"
SELECTION = ROOT / "services/worker/src/fastagent_v2/worker/material_selection.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
MIGRATION = ROOT / "infrastructure/migrations/071_conversation_summaries.sql"
BOUND = ROOT / "packages/runtime/src/fastagent_v2/runtime/understanding_bound.py"

TOUCHED = (SUMMARY, SELECTION, PIPELINE, MIGRATION, BOUND)

#: 收口那一行 —— 没有它，摘要就随会话长度一起长。
THE_BOUND = """        if used + cost > budget:
            break"""

#: 写摘要那一处读模型那一句的那一行。
THE_ONLY_READ = (
    '        line = clip_line(str(understanding.get("turn_summary")'
    ' or "").strip())'
)

#: 「一个回合一行」那条唯一索引的抬头。
THE_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "conversation_summaries_one_line_per_turn_idx"
)

#: 读摘要时按 `status` 过滤的那一行。
THE_STATUS_FILTER = (
    "             WHERE tenant_id = %s AND conversation_id = %s"
    " AND status = 'active'\n"
)

#: 探针 C 注进去的那一行：让写摘要那一处去读旧行。
#: ⚠️ `noqa: S608` —— 这是**要注进被测文件的字面量**，本脚本自己不拼 SQL。
THE_READ_OF_OLD_LINES = (
    '        connection.execute('
    '"SELECT line FROM conversation_summaries")\n'  # noqa: S608
)

#: 摘掉 `turn_summary` 的那一行。
THE_STRIP = '_NOT_IN_PROMPT = frozenset({"turn_summary"})'

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把收口去掉 —— 请求重新随会话长度增长（ADR-0122 §三 那件事）",
            [Mutation(SUMMARY, THE_BOUND, "        if False:\n            break")],
            # **两条一起红，而第二条不是噪音**：把收口去掉之后「丢最旧的」
            # 也不再发生（一行都不丢），所以那一条同样失去依据。
            # 两条钉的是同一个机制的两面。
            [
                "test_the_oldest_lines_are_the_ones_dropped",
                "test_the_request_does_not_grow_without_bound",
            ],
            (),
        ),
        (
            "B · 预留与界不再是一个数（2400 -> 2000）",
            [Mutation(SELECTION, "SUMMARY_BYTES_RESERVED = 2400", "SUMMARY_BYTES_RESERVED = 2000")],
            ["test_the_budget_is_one_number_in_two_places"],
            # **实测出来的连带，而它恰好证明这一行真的在预算链里**：
            # 改掉预留，推导式那条与「新字节从池子里出」那条一起红。
            # 声明空集才是错的 —— 那等于说这个数没有下游。
            [
                "test_the_empty_request_baseline_is_measured_from_the_real_payload",
                "test_the_new_bytes_came_out_of_the_pool_not_the_margin",
            ],
        ),
        (
            "C · 让写摘要那一处去读旧行 —— 裁定八的结构保证没了",
            [
                Mutation(
                    PIPELINE,
                    THE_ONLY_READ,
                    THE_READ_OF_OLD_LINES + THE_ONLY_READ,
                )
            ],
            ["test_the_model_never_gets_the_earlier_lines_to_rewrite"],
            (),
        ),
        (
            "D · 去掉「一个回合一行」那条唯一索引",
            [
                Mutation(
                    MIGRATION,
                    THE_UNIQUE_INDEX,
                    THE_UNIQUE_INDEX.replace("UNIQUE INDEX", "INDEX"),
                )
            ],
            ["test_one_turn_appends_at_most_one_line"],
            (),
        ),
        (
            "E · 读摘要不再按 `status` 过滤 —— 作废过的行重新进提示词",
            [
                Mutation(
                    PIPELINE,
                    THE_STATUS_FILTER,
                    "             WHERE tenant_id = %s AND conversation_id = %s\n",
                )
            ],
            ["test_an_invalidated_line_never_reaches_the_prompt"],
            (),
        ),
        (
            "F · 让 `turn_summary` 进下游提示词 —— 理解步预算被吃掉一大块",
            [Mutation(BOUND, THE_STRIP, "_NOT_IN_PROMPT = frozenset()")],
            ["test_the_turn_summary_never_rides_into_the_downstream_prompts"],
            # **四条，而这四条就是「为什么必须摘掉」的证明本身**：
            # 让 `turn_summary` 进去之后，`bound_for_prompt` 的地板顶穿 1,580,
            # 于是它 docstring 里那条「不存在丢无可丢仍然超限的输入」不成立，
            # 连带「最大的合法 understanding 装得下」「丢的是低置信度那些」一起塌。
            # **我原本声明空集 —— 那会让这支探针看起来只是在守一个偏好。**
            [
                "test_the_bound_is_measured_with_the_same_ruler_the_request_uses",
                "test_the_floor_is_the_droppable_ones_empty_plus_the_undroppable_ones_maxed",
                "test_the_largest_legal_understanding_fits_after_bounding",
                "test_the_lowest_confidence_entries_are_the_ones_dropped",
            ],
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
            names = _failed_names(result.output)
            hit = set(names) == set(expects)
            control_names = _failed_names(result.control_output or "")
            collateral_hit = set(control_names) == set(collateral)
            verdicts.append((label, result.went_red, hit, collateral_hit))
            print(result)
            if not hit:
                print(
                    f"    ⚠️ 本文件：多红 {sorted(set(names) - set(expects))}；"
                    f"没红 {sorted(set(expects) - set(names))}"
                )
            if not collateral_hit:
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
