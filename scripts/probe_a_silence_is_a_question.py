#!/usr/bin/env python
"""反向探针：ADR-0149 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **S1** | `silence_prompt` 不再委托 `understand_prompt`（自己拼一份） | 「上下文按构造一致」那条 |
| **S2** | 多出来的三个键删掉一个（`unanswered_rounds`） | 「多出来的正好那三样」那条 |
| **S3** | `_without_an_unsendable_ref` 一律摘掉 ref | **反侧**那条（发得出的原样保留） |
| **S4** | `_without_an_unsendable_ref` 直接原样返回 | 「点名发不出照常说话」那条 |
| **S5** | 锚点集合加上 `commitment_due_at` | **量出来的** —— 承诺到期那一轮会被问 |

**S3 与 S4 是一对**：一个验「该改的改了」，一个验「不该改的没改」。
`AGENTS.md`：放宽一条判据时正反两侧都要验 —— 而这里两侧各有一支。

**S5 的期望留空是有意的**：连库那两条在 `tests/e2e/`，而本探针的对照跑
`tests/contracts`（默认 scope），**contracts 层碰不到承诺锚点那条路**。
它绿就说明「承诺那一族不该被问」这件事**今天只有 ADR 正文在守，没有用例**——
而那正是这支探针要报出来的东西，不是要藏起来的东西。

    python scripts/probe_a_silence_is_a_question.py
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: 自己把 stdout 转成 UTF-8 —— 不要求调用方 export（`AGENTS.md`：修工具本身）。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_a_silence_is_a_question.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的。**
BASELINE_PASSED = 7
CONTROL_BASELINE_PASSED = 2418
CONTROL_BASELINE_SKIPPED = 53

SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"

TOUCHED = (SHAPE, PIPELINE)

DELEGATE = "    prompt = understand_prompt(\n"
EXTRA_KEY = '    prompt["unanswered_rounds"] = int(unanswered_rounds)\n'
KEEP_REF = "    if ref is not None and str(ref) in offered:\n        return reading\n"
GUARD_HEAD = '    if not isinstance(action, dict) or str(action.get("do")) != "send_material":\n'
ANCHORS = 'SILENCE_ANCHORS = frozenset({"last_outbound_at", "frame_entered_at"})'

SAME_CONTEXT = "test_the_context_is_the_understanding_step_s_own"
THREE_EXTRAS = "test_the_three_extra_facts_are_the_only_extras"
UNSENDABLE = "test_an_unsendable_ref_does_not_silence_the_turn"
SENDABLE = "test_a_sendable_ref_is_left_alone"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "S1 · `silence_prompt` 自己拼一份，不再委托理解步",
            [Mutation(SHAPE, DELEGATE, "    prompt = dict(\n")],
            [SAME_CONTEXT, THREE_EXTRAS],
            [],
        ),
        (
            "S2 · 三个多出来的键删掉一个",
            [Mutation(SHAPE, EXTRA_KEY, "")],
            [THREE_EXTRAS],
            [],
        ),
        (
            "S3 · 一律摘掉 ref（发得出的也摘）—— **反侧**",
            [Mutation(PIPELINE, KEEP_REF, "")],
            [SENDABLE],
            [],
        ),
        (
            "S4 · 原样返回，从不摘 ref",
            [Mutation(PIPELINE, GUARD_HEAD, "    if True:\n")],
            [UNSENDABLE],
            [],
        ),
        (
            # **期望留空是有意的** —— 见模块 docstring。
            "S5 · 锚点集合加上 `commitment_due_at`（承诺到期也去问）",
            [
                Mutation(
                    PIPELINE,
                    ANCHORS,
                    "SILENCE_ANCHORS = frozenset(\n"
                    '    {"last_outbound_at", "frame_entered_at", "commitment_due_at"}\n)',
                )
            ],
            [],
            [],
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
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    aimed = all(hit and other for _, _, hit, other in verdicts)
    print("ALL AIMED" if aimed else "SOMETHING WENT RED SOMEWHERE ELSE (or did not go red)")
    return 0 if aimed else 1


if __name__ == "__main__":
    raise SystemExit(main())
