#!/usr/bin/env python
"""反向探针：T258 那一批 —— `tests/contracts/test_one_conversation_one_language.py`。

本批的主张逐条要有一支探针证明**它真的会红**：

1. 阈值就是那个 4（松了会误伤）；
2. **链接先摘掉再数**（不摘的话一条带链接的中文回复被判成英文）；
3. 「没有汉字」那一半真的在把关（去掉它，中英混排被判成英文）；
4. **素材段放行不是纸上的** —— `material:demo-price-plain:v1` 这个 ref
   本身就有四个拉丁词，**恰好等于阈值**；
5. 整份拒时报的是**实际那个码**，不是写死的那一个；
6. 它在看板上数得到；
7. 审计行记词数**不记正文**；
8. **T384 那条「今天没有执行者」的断言会在它失效时变红**；
9. **它落在 `LOOP_ESCALATION_REASON_CODES` 里不是顺手** ——
   拿掉它，一条整份拒就回到「客户每轮收不到东西而没人被叫醒」。

⚠️ **带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去本文件」，
把连带集合**量出来**而不是想出来。

    python scripts/probe_one_conversation_one_language.py
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
TARGET = "tests/contracts/test_one_conversation_one_language.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 14

#: `tests/contracts` 减去本文件之后的那两个数。**量出来的，不是算出来的。**
#:
#: ⚠️ 跳过数也要声明 —— harness 那句「**绿的基线不等于干净的基线**」
#: 说的就是它：一个漏声明的 skip 可能是上一支探针没还原干净。
# ⚠️ **rebase 之后这个数会变**：2026-09-20 本支 rebase 到 `64eedf7c`
# （话术那一批合进来，contracts 多 5 条），2292 -> 2297。**重量，不要推算。**
CONTROL_BASELINE_PASSED = 2297
CONTROL_BASELINE_SKIPPED = 43

VALIDATION = (
    ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_validation.py"
)
OBSERVABILITY = ROOT / "scripts" / "stage_observability.py"
PIPELINE = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
)

TOUCHED = (VALIDATION, OBSERVABILITY, PIPELINE)

#: 素材段放行的那个分支。
EXEMPT = """            if (
                not isinstance(segment, dict)
                or segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
            ):"""

#: 整份拒那一行。
THE_RAISE = (
    "            reasons.pop() if len(reasons) == 1 "
    "else UNFOUNDED_CUSTOMER_FACT_REASON_CODE,"
)

#: 看板上那一行。
BOARD = (
    "    \"UNION ALL SELECT 'another_language_refusal', '', '', \"\n"
)

#: 升级表里的那一行（**连表尾一起锚**，否则这个名字在 import 里也命中）。
ESCALATION_ENTRY = (
    "        REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE,\n"
    "    }\n"
    ")"
)

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 阈值从 4 放到 2（短句开始被误伤）",
            [
                Mutation(
                    VALIDATION,
                    "LATIN_WORDS_FOR_ANOTHER_LANGUAGE = 4",
                    "LATIN_WORDS_FOR_ANOTHER_LANGUAGE = 2",
                )
            ],
            [
                "test_both_halves_are_required",
                "test_the_threshold_is_a_named_number_with_a_reason",
            ],
            (),
        ),
        (
            "B · 不再先摘链接（一条带链接的中文回复被判成英文）",
            [
                Mutation(
                    VALIDATION,
                    '    return len(_LATIN_WORD.findall(_URL.sub(" ", text)))',
                    "    return len(_LATIN_WORD.findall(text))",
                )
            ],
            ["test_the_false_positives_that_would_cost_a_real_reply"],
            (),
        ),
        (
            "C · 「没有汉字」那一半失效（中英混排被判成英文）",
            [
                Mutation(
                    VALIDATION,
                    "    if _CJK.search(text):\n        return False",
                    "    if False:\n        return False",
                )
            ],
            ["test_both_halves_are_required"],
            (),
        ),
        (
            "D · 素材段不再放行（ref 自己那四个拉丁词把它打掉）",
            [
                Mutation(
                    VALIDATION,
                    EXEMPT,
                    "            if not isinstance(segment, dict):",
                )
            ],
            [
                "test_the_english_sentence_goes_and_the_chinese_one_stays",
                "test_the_merchants_own_material_is_exempt",
            ],
            # ⚠️ **连带只能量，不能想。** 第一版这里我按「5.7 那一族拿同一个
            # ref 做夹具」推了两条，**实测是七条** —— 那一族每一条用例的夹具
            # 都在末尾挂一个 `material_ref` 段（`quote=True` 是默认），
            # 所以放行一没，整族的断言全塌。
            #
            # **这七条同时也是这支变异的价值所在**：素材段放行不是纸上的，
            # 它撑着的是隔壁一整个文件。
            (
                "test_a_natural_callback_to_our_last_message_survives",
                "test_once_the_customer_has_said_the_number_we_may_speak_of_it",
                "test_quoting_a_price_is_not_claiming_the_customer_pays_it",
                "test_the_first_turn_has_no_last_outbound_and_that_is_not_a_repeat",
                "test_the_merchants_own_words_survive",
                "test_the_same_sentence_in_two_turns_is_dropped",
                "test_the_sentence_that_charges_the_customer_is_dropped",
            ),
        ),
        (
            "E · 整份拒退回写死那个码",
            [
                Mutation(
                    VALIDATION,
                    THE_RAISE,
                    "            UNFOUNDED_CUSTOMER_FACT_REASON_CODE,",
                )
            ],
            ["test_a_refusal_that_is_all_one_reason_says_that_reason"],
            (),
        ),
        (
            "F · 看板上不再数它",
            [Mutation(OBSERVABILITY, BOARD, "")],
            ["test_the_refusal_is_countable_on_the_board"],
            (),
        ),
        (
            "G · 审计行改成记正文",
            [
                Mutation(
                    VALIDATION,
                    '                        "latin_words": '
                    'len(_LATIN_WORD.findall(_URL.sub(" ", own))),',
                    '                        "text": own,',
                )
            ],
            [
                "test_the_drop_records_the_count_and_not_the_words",
                "test_the_english_sentence_goes_and_the_chinese_one_stays",
            ],
            (),
        ),
        (
            "H · 让 T384 那条断言失效（生产代码里出现「回执不是答案」）",
            [
                Mutation(
                    OBSERVABILITY,
                    "    # **一整条回复被判成另一种语言而整份拒**（T258，2026-09-20）。",
                    "    # 假装有人在这里给「回执不是答案」写了执行者。\n"
                    "    # **一整条回复被判成另一种语言而整份拒**（T258，2026-09-20）。",
                )
            ],
            ["test_those_rules_still_have_no_enforcer"],
            (),
        ),
        (
            "I · 把它从 LOOP_ESCALATION_REASON_CODES 里拿掉（拒绝回到完全无声）",
            [
                Mutation(
                    PIPELINE,
                    ESCALATION_ENTRY,
                    "    }\n)",
                )
            ],
            ["test_a_refusal_nobody_hears_about_is_not_an_option"],
            (),
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
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
        if hit and coll:
            mark = "RED ✅" if went_red else "GREEN ✅（声明如此）"
        else:
            mark = "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
