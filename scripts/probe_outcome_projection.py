#!/usr/bin/env python
"""反向探针：批 3 那两条守卫，逐条验它会红。

守的是 `tests/contracts/test_outcome_projection_absence.py` ——
`outcome_ref` / `annotation_refs`（RFC:150，B1-4）今天**没有写者**，
而 `api/app.py` 有一个读点，于是对外那一格**永远是 null**。
处置照抄用户 2026-09-13 对承诺那一格的裁定：**字段照显，旁边说清这是哪一种 null。**

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **O1** | 给 `outcome_ref` 加一个真的写点（模拟「只落一半」） | 分类那条 |
| **O2** | 给 `annotation_refs` 加一个写点 | 分类那条 |
| **O3** | `OUTCOME_PROJECTION_AVAILABLE` 翻成 `True` | 投影那条 |
| **O4** | 把 `status` 那个键从「未启用」那一支里拿掉 | 投影那条 |
| **O5** | 详情页改回 `{"outcome_ref": …}` 裸投影（回到批 3 之前） | **分类那条** —— 见下 |

**O5 是这支探针里最该看一眼的一支**：回到裸投影之后，
红的**不是**「投影那条」（`outcome_display` 那个函数还在、单测照样过），
**而是分类那条** —— 因为 `app.py` 里多出一处没分过类的 `{"outcome_ref": …}`。
**也就是说：那条分类守卫顺带钉住了「详情页有没有真的用上那一格」**，
而这不是设计出来的，是跑出来的。

    python scripts/probe_outcome_projection.py
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
TARGET = "tests/contracts/test_outcome_projection_absence.py"

BASELINE_PASSED = 2

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1688

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
DISPLAY = ROOT / "services/api/src/fastagent_v2/api/outcome_display.py"
APP = ROOT / "services/api/src/fastagent_v2/api/app.py"

TOUCHED = (PIPELINE, DISPLAY, APP)

DECLARE = '        "outcome_ref": None,\n        "annotation_refs": [],\n'
WRITTEN = '        "outcome_ref": "written-somewhere",\n        "annotation_refs": [],\n'
WRITTEN_ANNOTATIONS = '        "outcome_ref": None,\n        "annotation_refs": ["a"],\n'

AVAILABLE = "OUTCOME_PROJECTION_AVAILABLE = False"

STATUS_KEY = '            "status": "not_enabled",\n'

PROJECTION = '            "outcome": outcome_display(state.get("outcome_ref")),'
BARE = '            "outcome": {"outcome_ref": state.get("outcome_ref")},'

CLASSIFIED = "test_every_mention_of_these_fields_has_been_classified"
PROJECTED = "test_the_projection_says_which_kind_of_null_it_is"

#: O1 / O2 的连带（2026-09-16 对照跑，基线 `88882184`）。
#:
#: **写下来是因为它们不是噪音，而是 O1 / O2 的一个限制**：那两支往
#: `pipeline.py` 的聚合初始化里塞一个写点，于是造出来的聚合**不再过
#: `conversation-aggregate` 契约** —— 下面这四条红的是那件事，不是「有人写了
#: `outcome_ref`」。**红的理由与声称的理由不同**，正是批 5 F1/F2 那个形状
#: （`probe_confidence_assessment` docstring），只是这次程度轻得多：
#: 四条而不是十八条，而且分类那条在本文件里**确实**是对的那一条。
#:
#: **留着不改成「更干净的变异」是有意的**：一个真的写点就会长这样 ——
#: 谁来接 `outcome_ref`，谁就要同时回答「聚合契约怎么改」。
#: 这四条提前把那个问题摆出来，比一个绕过契约的假写点有用。
AGGREGATE_BREAKS = [
    "test_a_model_update_cannot_overwrite_a_human_written_slot",
    "test_a_single_frame_stack_resumes_active_not_repair_pending",
    "test_all_seven_contracts_accept_their_m0_shapes",
    "test_resuming_a_deeper_stack_puts_the_top_frame_back_active",
]

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "O1 · 给 `outcome_ref` 加一个真的写点（只落一半的样子）",
            [Mutation(PIPELINE, DECLARE, WRITTEN)],
            [CLASSIFIED],
            AGGREGATE_BREAKS,
        ),
        (
            "O2 · 给 `annotation_refs` 加一个写点",
            [Mutation(PIPELINE, DECLARE, WRITTEN_ANNOTATIONS)],
            [CLASSIFIED],
            AGGREGATE_BREAKS,
        ),
        (
            "O3 · `OUTCOME_PROJECTION_AVAILABLE` 翻成 True（这一格开始说假话）",
            [Mutation(DISPLAY, AVAILABLE, "OUTCOME_PROJECTION_AVAILABLE = True")],
            [PROJECTED],
            [],
        ),
        (
            "O4 · 把 `status` 从「未启用」那一支里拿掉（回到一个光秃秃的 null）",
            [Mutation(DISPLAY, STATUS_KEY, "")],
            [PROJECTED],
            [],
        ),
        (
            "O5 · 详情页改回裸投影 —— **红的是分类那条，不是投影那条**",
            [Mutation(APP, PROJECTION, BARE)],
            [CLASSIFIED],
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
        print("**O5 那一行是跑出来的，不是设计出来的**：详情页改回裸投影时，")
        print("红的是「分类」而不是「投影」—— 那条守卫顺带钉住了详情页有没有真的用上这一格。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
