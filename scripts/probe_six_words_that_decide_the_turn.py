#!/usr/bin/env python
"""反向探针：`tests/contracts/test_six_words_that_decide_the_turn.py`。

**最要紧的一支**：

* A · **把那一段整条删掉，退回原来那一行枚举** —— 它必须红，
  因为本批存在的全部理由就是那一行当时没红。

其余：

* B · 契约里加一个词而提示词没教它（**加词是安静的**）；
* C · 契约里删掉五个词只留一个（钉住第一条不是读自己）；
* D · `PUSH_RELATIONS` 多一个词而提示词没跟上；
* E · `correction` 不再解锁覆盖槽位，而提示词还那么写着；
* F · `EXIT_SIGNAL_RELATIONS` 变了；
* G · 那句反例（「不是 `exit`」）被删掉；
* H · 「他又问起聊过的事」那一句被删掉；
* I · 版本号不跟着走。

    python scripts/probe_six_words_that_decide_the_turn.py
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
TARGET = "tests/contracts/test_six_words_that_decide_the_turn.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 7

#: `tests/contracts` 减去本文件。**量出来的。**
CONTROL_BASELINE_PASSED = 2400
CONTROL_BASELINE_SKIPPED = 53

MODEL = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference_model.py"
VALIDATION = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference_validation.py"
)
STACK = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "target_stack.py"
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
CONTRACT = (
    ROOT / "packages" / "contracts" / "inference" / "understanding-result.schema.json"
)

TOUCHED = (MODEL, VALIDATION, STACK, PIPELINE, CONTRACT)

_MODEL_SOURCE = MODEL.read_text(encoding="utf-8")

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


EXPLAINED = "test_every_word_the_contract_allows_is_explained"
SIX = "test_the_contract_still_declares_exactly_those_six"
CHILD = "test_the_three_that_open_a_child_frame_are_named_as_such"
CORRECTION = "test_correction_is_told_that_it_unlocks_a_slot_overwrite"
DURABLE = "test_the_two_durable_ones_are_marked_as_durable"
AGAIN = "test_asking_again_is_told_to_stay_and_answer_briefly"
RELEASE = "test_the_release_moved_with_this_teaching"

#: 理解步正文一动，这几条字节表就响 —— 与前几支实测的是同一族。
PROMPT_BYTES = (
    "test_the_platform_prompt_sizes_are_spelled_once_per_package",
    "test_the_overhead_constants_are_measured_from_the_real_payload",
    "test_the_floor_equals_the_request_it_claims_to_measure",
)

#: ⚠️ **契约那个枚举一动，这一条就响** —— 它把合同里那六个词与
#: `EXIT_SIGNAL_RELATIONS` 对着看。预判时我没想到它。
CONTRACT_GUARD = "test_the_exit_relations_are_the_ones_the_contract_declares"

#: C 支（契约只留一个词）掀掉的那一片 —— **量出来的**：
#: 少五个词，读那张枚举的每一处都跟着红。
CONTRACT_BLAST = (
    *PROMPT_BYTES,
    CONTRACT_GUARD,
    "test_advance_goal_with_any_leaving_op_is_refused",
    "test_repair_refuses_a_return_on_the_turn_the_exit_signal_arrives",
    "test_the_deepest_stack_the_platform_allows_is_five",
    "test_the_exit_signal_reaches_the_stack_through_the_real_transitions",
    "test_the_platform_frame_cannot_be_pushed_by_a_plan",
    "test_the_validator_and_the_transition_never_disagree",
    "test_this_gate_never_fires_for_another_strategy",
)

#: F 支（退出信号那两个变了）—— 量出来的。
EXIT_FAMILY = (
    CONTRACT_GUARD,
    "test_the_exit_signal_reaches_the_stack_through_the_real_transitions",
    "test_the_parent_goal_is_not_handed_back_as_active_after_an_exit",
)

#: 四处 release pin。
RELEASE_PINS = (
    "test_the_prompt_now_shows_a_payload_carrying_content",
    "test_the_prompt_release_moved_with_this_change",
    "test_prompt_release_moved_with_the_prompt",
    "test_prompt_release_and_v4_replay_golden_move_together",
    "test_the_release_moved_with_this_teaching",
    # 隔壁那批（一条说一件事）的同名条 —— 实测。
    "test_the_release_moved_with_the_prompt",
)

#: 整段的首行 —— 用来定位。
TEACHING_HEAD = "- interruption.relation says **what this message does to the goal we are on**."

#: ⚠️ **A 支要换掉的是整段。** 首行换掉不够：后面十几行还在，
#: 读它们的那几条断言**一条都不红** —— 实测过。
TEACHING_BLOCK = _MODEL_SOURCE[
    _MODEL_SOURCE.index(TEACHING_HEAD) : _MODEL_SOURCE.index("- sentiment is one of:")
]


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 退回原来那一行枚举（整段删掉）",
            [
                Mutation(
                    MODEL,
                    TEACHING_BLOCK,
                    "- interruption.relation is one of the six.\n",
                )
            ],
            [EXPLAINED, CHILD, CORRECTION, DURABLE, AGAIN],
            PROMPT_BYTES,
        ),
        (
            "B · 契约加一个词而提示词没教它",
            [
                Mutation(
                    CONTRACT,
                    '"same_goal",',
                    '"same_goal",\n            "small_talk",',
                )
            ],
            [EXPLAINED, SIX],
            (*PROMPT_BYTES, CONTRACT_GUARD),
        ),
        (
            "C · 契约只留一个词（钉住第一条不是读自己）",
            [
                Mutation(
                    CONTRACT,
                    # 契约里那个 enum 是**单行**的 —— 多行锚点会 0 命中。
                    '"side_question", "new_goal", "correction", "exit", "human_request"',
                    '"side_question"',
                )
            ],
            [SIX],
            CONTRACT_BLAST,
        ),
        (
            "D · PUSH_RELATIONS 多一个词",
            [
                Mutation(
                    VALIDATION,
                    'PUSH_RELATIONS = ("side_question", "new_goal", "correction")',
                    'PUSH_RELATIONS = ("side_question", "new_goal", "correction", "same_goal")',
                )
            ],
            [CHILD],
            ("test_push_requires_an_interruption", CONTRACT_GUARD),
        ),
        (
            "E · correction 不再解锁覆盖槽位",
            [
                Mutation(
                    PIPELINE,
                    # 缩进是它的唯一性来源：`== "correction"` 在这个文件里有两处，
                    # 接管那一路缩进 12 格，这一处 20 格。
                    '                    str(understanding["interruption"]'
                    '["relation"]) == "correction"',
                    "                    False",
                )
            ],
            [CORRECTION],
            (),
        ),
        (
            "F · 退出信号那两个变了",
            [
                Mutation(
                    STACK,
                    'EXIT_SIGNAL_RELATIONS = frozenset({"exit", "human_request"})',
                    'EXIT_SIGNAL_RELATIONS = frozenset({"exit"})',
                )
            ],
            [DURABLE],
            EXIT_FAMILY,
        ),
        (
            "G · 那句反例被删掉（「不是 exit」）",
            [
                Mutation(
                    MODEL,
                    "**Never use them for ordinary impatience**",
                    "**Use them when he is unhappy**",
                )
            ],
            [DURABLE],
            PROMPT_BYTES,
        ),
        (
            "H · 「他又问起聊过的事」那一句被删掉",
            [
                Mutation(
                    MODEL,
                    "Also use it when he asks again about",
                    "Ignore the case where he mentions again",
                )
            ],
            [AGAIN],
            PROMPT_BYTES,
        ),
        (
            "I · 版本号不跟着走",
            [
                Mutation(
                    MODEL,
                    'PROMPT_RELEASE = "m1a-prompt-v17"',
                    'PROMPT_RELEASE = "m1a-prompt-v12"',
                )
            ],
            [RELEASE],
            RELEASE_PINS,
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
        mark = ("RED ✅" if went_red else "GREEN ✅（声明如此）") if hit and coll else "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
