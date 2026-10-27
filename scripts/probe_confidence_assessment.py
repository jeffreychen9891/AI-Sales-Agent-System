#!/usr/bin/env python
"""反向探针：批 5 那条守卫，逐条验它会红。

守的是 `tests/contracts/test_confidence_assessment_is_decorative.py` ——
`ConfidenceAssessment` 四个字段今天**零读点**，而其中三个**连值都是常量**。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **F1** | 在生产代码里读一次 `calibrated_score` | 没人读那条 |
| **F2** | 用 `.get("factor_codes")` 读一次 | 没人读那条 |
| **F3** | `calibrated_score` 改成由一个函数算出来（模拟校准器落地） | **常量那条** |
| **F4** | `factor_codes` 改成由一个表达式生成 | **常量那条** |

**F3 / F4 是这支探针的重点。** 只钉「没人读」的话，
**校准器建起来之后那三个字段仍然可以没人读，而守卫照旧全绿** ——
那时那份 docstring 里「连值都是假的」这句话已经成了假话，
**却没有任何东西会红**。这一支就是那句话的守卫的证据。

**F1 第一版是一个无效变异，记在这里**：它插的表达式里带了个 `{"confidence": {}}`，
被「常量那条」当成了**第二个 confidence 块**，于是两条一起红 —— 而真实的读者不会那么写。
**无效的变异给的是假信号**（本仓记过这条）：它红了，但红的理由不是它声称的那个。
改成一个不制造 `"confidence"` 字面量的下标读法之后，它才只红在该红的那一条上。

**它与批 4 那条是同一个形状的两面**：
批 4 是「断言某物不存在时，别忘了保护它的替代方案」；
这里是「断言某物是假的时，别忘了保护『它还是假的』这件事本身」。
**两条都在说：一条断言配一条守卫，而那条守卫要守的是断言的每一半。**

    python scripts/probe_confidence_assessment.py
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
TARGET = "tests/contracts/test_confidence_assessment_is_decorative.py"

BASELINE_PASSED = 2

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1688

INFERENCE = ROOT / "services/worker/src/fastagent_v2/worker/inference.py"

TOUCHED = (INFERENCE,)

#: `safe fallback` 那一处的整块 —— 三支都在它上面动手，锚点唯一。
FALLBACK_BLOCK = (
    '        "confidence": {\n'
    '            "raw_score": 0.0,\n'
    '            "calibrated_score": None,\n'
    '            "assessment_version": "safe-fallback-v1",\n'
    '            "factor_codes": ["fallback_applied"],\n'
    "        },\n"
)

READ_IT = (
    '        "confidence": {\n'
    '            "raw_score": 0.0,\n'
    '            "calibrated_score": None,\n'
    '            "assessment_version": "safe-fallback-v1",\n'
    '            "factor_codes": ["fallback_applied"],\n'
    "        },\n"
    '        "_probe": {}["calibrated_score"],\n'
)

READ_CODES = (
    '        "confidence": {\n'
    '            "raw_score": 0.0,\n'
    '            "calibrated_score": None,\n'
    '            "assessment_version": "safe-fallback-v1",\n'
    '            "factor_codes": ["fallback_applied"],\n'
    "        },\n"
    '        "_probe": {}.get("factor_codes"),\n'
)

CALIBRATED_COMPUTED = (
    '        "confidence": {\n'
    '            "raw_score": 0.0,\n'
    '            "calibrated_score": min(1.0, 0.0),\n'
    '            "assessment_version": "safe-fallback-v1",\n'
    '            "factor_codes": ["fallback_applied"],\n'
    "        },\n"
)

CODES_COMPUTED = (
    '        "confidence": {\n'
    '            "raw_score": 0.0,\n'
    '            "calibrated_score": None,\n'
    '            "assessment_version": "safe-fallback-v1",\n'
    '            "factor_codes": list(["fallback_applied"]),\n'
    "        },\n"
)

NO_READER = "test_nothing_reads_the_confidence_assessment"
LITERALS = "test_three_of_the_four_are_still_literals"

#: ⚠️ **F1 / F2 是无效变异，而这张表是它们的判决书。**
#:
#: 两支往那个 fallback 块里插的是 `{}["calibrated_score"]` —— **一个运行期必抛
#: `KeyError` 的表达式**，而那个块是可达的。于是它们红**不是因为「有人读了这个
#: 字段」**，是因为那一行一执行就炸。独立复核 2026-09-15（批 5 §三）用「排除本
#: 文件」量到 16 条；2026-09-16 在 `88882184` 上重量是 **18 条** —— 数会随
#: 用例增减而变，**形状不会**。
#:
#: **为什么不换一个「干净」的变异**：换掉之后这支探针就只证明「AST 扫描器认得
#: 一次读」，而那是扫描器的用例该证明的事。**留着，并把连带声明出来**，因为
#: 这张表本身就是这支探针最有用的产出：它写着「这里没有真正的读者可以模仿」——
#: 真有读者的那天，F1/F2 要被换成那个读者的样子，而这张表会因为不再相等而红。
#:
#: 名字逐条列出（不是计数），因为**计数会把「换了一条」读成「没变」**。
KEYERROR_BLAST = [
    "test_a_fallback_may_not_choose_an_arbitrary_strategy",
    "test_a_refused_plan_falls_back_and_the_fallback_itself_passes_the_gate",
    "test_a_refused_plan_falls_back_to_no_message_and_sends_nothing",
    "test_a_rejected_plan_becomes_a_deterministic_fallback",
    "test_a_rejection_that_can_name_the_slot_now_asks",
    "test_a_rejection_that_cannot_name_a_slot_is_still_silent",
    "test_a_rejection_with_nothing_understood_is_still_silent",
    "test_a_safe_fallback_is_not_bound_by_the_rule_s_strategy",
    "test_a_safe_fallback_plan_may_not_request_retrieval",
    "test_a_slot_this_frame_does_not_have_is_filtered_out",
    "test_a_timer_plan_that_names_another_strategy_is_refused",
    "test_a_timer_turn_does_not_ask_the_customer_to_clarify",
    "test_only_a_customer_message_turn_may_ask_the_customer_to_clarify",
    "test_the_fallback_is_not_subject_to_this_gate",
    "test_the_fallback_is_still_exempt_from_the_gate_it_answers",
    "test_the_fallback_names_the_slots_the_gate_reported",
    "test_the_generation_prompt_carries_the_slots_the_fallback_named",
    "test_the_platform_fallback_writes_null_rather_than_omitting_it",
    "test_the_two_registered_fields_still_have_no_reader",
]

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "F1 · 在生产代码里读一次 `calibrated_score`",
            [Mutation(INFERENCE, FALLBACK_BLOCK, READ_IT)],
            [NO_READER],
            KEYERROR_BLAST,
        ),
        (
            "F2 · 用 `.get(\"factor_codes\")` 读一次",
            [Mutation(INFERENCE, FALLBACK_BLOCK, READ_CODES)],
            [NO_READER],
            KEYERROR_BLAST,
        ),
        (
            "F3 · `calibrated_score` 改成算出来的（校准器落地的样子）",
            [Mutation(INFERENCE, FALLBACK_BLOCK, CALIBRATED_COMPUTED)],
            [LITERALS],
            [],  # **干净的那两支**：零连带，红的理由就是声称的那个
        ),
        (
            "F4 · `factor_codes` 改成表达式生成",
            [Mutation(INFERENCE, FALLBACK_BLOCK, CODES_COMPUTED)],
            [LITERALS],
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
        print("**F3 / F4 是重点**：只钉「没人读」的话，校准器建起来之后守卫照旧全绿 ——")
        print("而那时「连值都是假的」这句话已经成了假话。断言的每一半都要有守卫。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
