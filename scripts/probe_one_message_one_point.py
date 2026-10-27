#!/usr/bin/env python
"""反向探针：`tests/contracts/test_one_message_one_point.py`。

九条判据里，**最要紧的两支是 C 与 D**：

* C · 把「上限」写成「目标」（`at most 60` → `around 60`）——
  用户 2026-09-21 当场更正过这一点，而判据得真的接得住那种措辞；
* D · **抽掉「不许超过计划列的条数」那半句** —— 少了它，模型照着分段
  就可能撞 `GENERATION_DUPLICATED_INTENT`，**整份拒、客户零字**。

其余各支：

* A · 上限整条删掉；
* B · 「短了更好」删掉（上限会被当成目标）；
* E · 计划步那句删掉（候选产不出第二个，规则就做不到）；
* F · 在 `pipeline.py` 里加一个平台切的实现（裁定明确不要）；
* G · 版本号不跟着走；
* H · 预算从安全余量里出，而不是从池子里扣。

⚠️ **带 `control=`**：连带集合量出来 —— 改系统提示词会连带顶到字节表那一族。

    python scripts/probe_one_message_one_point.py
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
TARGET = "tests/contracts/test_one_message_one_point.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 9

#: `tests/contracts` 减去本文件。**量出来的。**
#: ⚠️ 2026-09-21 rebase 到 `092f0567` 后重量：ADR-0144 + 配包 1.3.0 那批
#: 合进 main，contracts 多了 15 条（2,348 -> 2,363）。
CONTROL_BASELINE_PASSED = 2363
CONTROL_BASELINE_SKIPPED = 53

MODEL = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference_model.py"
)
PIPELINE = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
)
REGISTRY = ROOT / "services" / "api" / "src" / "fastagent_v2" / "profiles" / "registry.py"
SELECTION = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "material_selection.py"
)

TOUCHED = (MODEL, PIPELINE, REGISTRY, SELECTION)

#: ⚠️ **改一个字的系统提示词，就顶到这四条字节表** —— 实测，不是想出来的。
#:
#: `PLATFORM_PROMPT_BYTES` 与 `REQUEST_OVERHEAD_BYTES` 都是从提示词正文
#: **现量现比**的，而 admission 的每帧固定底又是从它们算的。
#: **这是本仓早就有的那道闸**：提示词一改它就响，逼着人把账付掉。
PROMPT_BYTES = (
    "test_the_empty_request_baseline_is_measured_from_the_real_payload",
    "test_the_floor_equals_the_request_it_claims_to_measure",
    "test_the_overhead_constants_are_measured_from_the_real_payload",
    "test_the_platform_prompt_sizes_are_spelled_once_per_package",
)

#: A 支额外多一条：它把那五行整条删掉，生成步的请求缩得够多，
#: 连「素材塞满预算时那一轮还装得下吗」都跟着变了。
BUDGET_HEADROOM = ("test_a_turn_at_the_material_budget_still_fits_the_limit",)

#: G 支（版本号不跟着走）顶的是**另一族**：本仓原有的两条
#: 「改提示词要抬版本号」加上两条读 golden 的。
RELEASE_PINS = (
    "test_prompt_release_and_v4_replay_golden_move_together",
    "test_prompt_release_moved_with_the_prompt",
    "test_the_prompt_now_shows_a_payload_carrying_content",
    "test_the_prompt_release_moved_with_this_change",
)

#: H 支（这笔钱从安全余量里出）顶的是**账本那一族** ——
#: 其中两条是语料：改了预算数而 `kb-013` 的正文/版本没跟上。
BUDGET_ACCOUNTING = (
    "test_the_empty_request_baseline_is_measured_from_the_real_payload",
    "test_the_new_bytes_came_out_of_the_pool_not_the_margin",
    "test_the_number_the_corpus_quotes_moved_too",
    "test_the_numbers_the_corpus_quotes_are_still_todays_numbers",
)

#: ⚠️ **F 支的连带是 0 条，而那正是它存在的理由。**
#: 在 `pipeline.py` 里加一个按长度切段的实现，**全仓一条都不会红** ——
#: 那条裁定（「不能设硬线，长段落也比说错话强」）今天只靠
#: `test_there_is_no_platform_side_cut` 一条守着。

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 上限整条删掉",
            [
                Mutation(
                    MODEL,
                    "- Keep each send_message **at most 60 characters. "
                    "Shorter is better** -- this\n"
                    "  is a ceiling, not a target. The merchant's own "
                    "approved wording runs 17 to 86\n"
                    "  characters, median 41, and the customer writes "
                    "about 10. If the answer does\n"
                    "  not fit, continue in the next send_message "
                    "**the plan listed**; never write\n"
                    "  more messages than it listed.\n",
                    "",
                )
            ],
            [
                "test_sixty_is_a_ceiling_not_a_target",
                "test_the_ceiling_matches_what_the_merchant_actually_writes",
                "test_the_generate_prompt_caps_each_message",
                "test_the_rule_names_the_plans_list_so_it_cannot_cause_a_refusal",
            ],
            PROMPT_BYTES + BUDGET_HEADROOM,
        ),
        (
            "B · 「短了更好」删掉（上限会被当成目标）",
            [
                Mutation(
                    MODEL,
                    "**at most 60 characters. Shorter is better**",
                    "**at most 60 characters**",
                )
            ],
            ["test_sixty_is_a_ceiling_not_a_target"],
            PROMPT_BYTES,
        ),
        (
            "C · 把上限写成目标（at most -> around）",
            [
                Mutation(
                    MODEL,
                    "**at most 60 characters. Shorter is better**",
                    "**around 60 characters**",
                )
            ],
            [
                "test_sixty_is_a_ceiling_not_a_target",
                "test_the_generate_prompt_caps_each_message",
            ],
            PROMPT_BYTES,
        ),
        (
            "D · 抽掉「不许超过计划列的条数」那半句",
            [
                Mutation(
                    MODEL,
                    " **the plan listed**; never write\n  more messages than it listed.",
                    ".",
                )
            ],
            ["test_the_rule_names_the_plans_list_so_it_cannot_cause_a_refusal"],
            PROMPT_BYTES,
        ),
        (
            "E · 计划步那句删掉（产不出第二个候选）",
            [
                Mutation(
                    MODEL,
                    " **Also list a second one when the\n"
                    "  answer will not fit in one short message** -- "
                    "the writer keeps each one under\n"
                    "  60 characters and cannot add a message you did not list.",
                    "",
                )
            ],
            ["test_the_plan_is_told_to_list_the_second_one"],
            tuple(name for name in PROMPT_BYTES if "empty_request" not in name),
        ),
        (
            "F · 在 pipeline 里加一个平台切的实现",
            [
                Mutation(
                    PIPELINE,
                    "    def _persist_accepted_proposal(",
                    "    def _split_long_message(self, text: str) -> list[str]:\n"
                    "        return [text]\n\n"
                    "    def _persist_accepted_proposal(",
                )
            ],
            ["test_there_is_no_platform_side_cut"],
            (),
        ),
        (
            "G · 版本号不跟着走",
            [
                Mutation(
                    MODEL,
                    'PROMPT_RELEASE = "m1a-prompt-v10"',
                    'PROMPT_RELEASE = "m1a-prompt-v9"',
                )
            ],
            ["test_the_release_moved_with_the_prompt"],
            RELEASE_PINS,
        ),
        (
            "H · 这笔钱从安全余量里出，而不是从池子里扣",
            [
                Mutation(
                    REGISTRY,
                    "MATERIAL_BYTES_BUDGET = 24572",
                    "MATERIAL_BYTES_BUDGET = 24923",
                ),
                Mutation(
                    SELECTION,
                    "MATERIAL_BYTES_BUDGET = 24572",
                    "MATERIAL_BYTES_BUDGET = 24923",
                ),
            ],
            ["test_the_cost_of_these_lines_was_paid_out_of_the_pool"],
            BUDGET_ACCOUNTING,
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
