#!/usr/bin/env python
"""反向探针：修完尺子之后那两份守卫，逐条验它会红。

守的是 2026-09-15 那个 **639–840 字节 fail-open** 缺口的修法：
固定底改从 `runtime.request_shape` 的唯一定义点算，
由 `test_request_floor_equals_the_real_request`（字节）与
`test_the_plan_floor_counts_what_the_prompt_sends`（结构）两份钉住。

**九支变异**：前三支对应修法里的三笔，中间三支是**复核指出的三种绕过写法**，
R8 / R9 是 2026-09-15「裁 B」加的（把话题槽的合法取值告诉模型那一处）：

| 支 | 把什么改回去 | 谁该红 |
|---|---|---|
| **R1** | 口径退回**单层**（`separators=(",", ":")`，不算外层转义） | 字节那份 |
| **R2** | `REQUEST_OVERHEAD_BYTES` 少 1 个字节 | 字节那份 |
| **R3** | 从唯一那份组装里删掉一张策略表 | **结构那份**（字节那份**不会红** —— 见下） |
| **R4** | 报告器塞回一个本地式子（下标） | 结构那份 |
| **R5** | 同上，`.get(r, 0)` —— **上一版守卫对它全绿** | 结构那份 |
| **R6** | 同上，`import ... as _p` —— **上一版同样全绿** | 结构那份 |
| **R7** | 删掉报告器顶部的 `reconfigure` 两行 | 编码那条（复核 ④ 指出它先前没有守卫） |
| **R8** | **生产**不再把合法取值传给理解步 | 调用点那条（**字节那份看不见**） |
| **R9** | **admission** 漏传同一个实参 | 调用点那条 + 字节那份 |

**R3 那一行是本文件最要紧的一句**：删掉一个键之后，闸与"真实请求"**都**少那个键
（两边调的是同一个函数），**所以字节那份仍然全绿**。
这正是"唯一定义点"这个修法自带的盲区 —— 而它由结构那份补上。
**两份缺一不可，而这支探针是那句话的证据，不是它的复述。**

    python scripts/probe_request_floor.py
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
TARGETS = (
    "tests/contracts/test_request_floor_equals_the_real_request.py",
    "tests/contracts/test_the_plan_floor_counts_what_the_prompt_sends.py",
)

#: 按数断言，不按颜色。八帧参数化 + 3 + 6。
BASELINE_PASSED = 17

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1673

PROMPT_BYTES = ROOT / "packages/runtime/src/fastagent_v2/runtime/prompt_bytes.py"
SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
REPORTER = ROOT / "scripts/check_material_dimensions.py"

TOUCHED = (PROMPT_BYTES, SHAPE, REGISTRY, MODEL, REPORTER)

#: `document_cost` 的函数体 —— 两层那一句。harness 强制锚点命中数为 1。
#: R1 的连带（2026-09-16 对照跑，基线 `88882184`）。把口径退回单层，
#: **知识库那两条也跟着红** —— 它们量的是同一把尺子。写下来是因为这正是本批
#: 「一处算法一份形状」要的证据：尺子只有一份，所以改它的人会同时听见三个地方。
RULER_SHARED_BY_TWO_MORE = [
    "test_a_turn_at_the_material_budget_still_fits_the_limit",
    "test_the_bound_is_measured_with_the_same_ruler_the_request_uses",
]

#: R3 的连带。删掉唯一那份组装里的一张策略表，**字节那份看不见、结构那份看得见**
#: —— 而「看得见」的不止本文件那一条：另外五条都在读 `plan_prompt` 的键集合，
#: 其中 `...replay_script...` 那条是回放脚本与生产的对齐。
#: **这五条是 R3 那句话的实际覆盖面**，本文件那一条只是其中最直接的一条。
PLAN_PROMPT_CONSUMERS = [
    "test_a_strategy_omitting_the_field_entirely_does_not_crash_the_turn",
    "test_a_strategy_that_declares_none_still_gets_a_key",
    "test_each_allowed_strategy_brings_its_required_slots",
    "test_the_plan_prompt_has_exactly_these_keys",
    "test_the_prompt_assembles_when_a_strategy_omits_every_optional_key",
    "test_the_replay_script_sends_the_same_plan_prompt_keys_as_production",
]

TWO_LAYER = "    return _outer_bytes(_inner(document)) - _EMPTY_STRING_BYTES"

#: 唯一那份 plan 组装里的第三张策略表，整块。R3 把它整块删掉。
THIRD_STRATEGY_TABLE = (
    '        "required_slots_by_strategy": {\n'
    '            name: strategies[name].get("required_slots", []) for name in named\n'
    "        },\n"
)

#: R8 / R9 摘掉的那个实参 —— 「裁 B」把话题槽的合法取值告诉模型的那一处。
#: 两支分打生产与 admission，因为**一边漏传另一边照常**是这个修法自带的出口。
TELL_MODEL = "                allowed_slot_values=allowed_slot_values(definition),\n"
TELL_ADMISSION = "            allowed_slot_values=allowed_slot_values(frame),\n"

#: R7 删掉的那两行 —— 报告器自己把 stdout 转成 UTF-8 的那处。
RECONFIGURE = (
    'if hasattr(sys.stdout, "reconfigure"):\n'
    '    sys.stdout.reconfigure(encoding="utf-8")\n'
)

#: R4–R6 共用的那两行 import —— 三支只差取值写法（下标 / `.get` / alias）。
IMPORT_TABLE = "        from fastagent_v2.profiles.registry import PLATFORM_PROMPT_BYTES\n"
IMPORT_TABLE_ALIASED = (
    "        from fastagent_v2.profiles.registry import PLATFORM_PROMPT_BYTES as _p\n"
)

run = suite_runner(ROOT, PYTHON, list(TARGETS))
control = control_runner(ROOT, PYTHON, list(TARGETS))


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "R1 · 口径退回单层（不算外层那次转义）",
            [
                Mutation(
                    PROMPT_BYTES,
                    TWO_LAYER,
                    '    import json as _j\n'
                    '    return len(_j.dumps(document, ensure_ascii=False,'
                    ' separators=(",", ":")).encode("utf-8")) - 2',
                )
            ],
            [
                "test_the_addition_is_an_identity_not_an_approximation",
                "test_the_floor_equals_the_request_it_claims_to_measure",
            ],
            RULER_SHARED_BY_TWO_MORE,
        ),
        (
            "R2 · REQUEST_OVERHEAD_BYTES 少一个字节",
            [Mutation(REGISTRY, '"PLAN_CAPABILITY": 8073,', '"PLAN_CAPABILITY": 8072,')],
            [
                "test_the_floor_equals_the_request_it_claims_to_measure",
                "test_the_overhead_constants_are_measured_from_the_real_payload",
            ],
            [],
        ),
        (
            "R3 · 从唯一那份组装里删掉一张策略表（字节那份看不见，结构那份看得见）",
            [
                Mutation(
                    SHAPE,
                    THIRD_STRATEGY_TABLE,
                    "",
                )
            ],
            ["test_the_plan_prompt_is_built_in_exactly_one_place"],
            PLAN_PROMPT_CONSUMERS,
        ),
        (
            "R4 · 报告器塞回一个本地式子（下标写法）",
            [
                Mutation(
                    REPORTER,
                    "        floors = request_floor_bytes(frame, document=document)",
                    IMPORT_TABLE
                    + "        floors = {r: PLATFORM_PROMPT_BYTES[r] for r in"
                    ' ("UNDERSTAND_VALUE", "PLAN_CAPABILITY")}',
                )
            ],
            [
                "test_the_reporter_prints_the_same_numbers_the_gate_computes",
                "test_the_two_byte_tables_have_no_computational_consumer",
            ],
            [],
        ),
        (
            "R5 · 同上，但改成 `.get(r, 0)` —— **上一版守卫对这种写法是全绿的**",
            [
                Mutation(
                    REPORTER,
                    "        floors = request_floor_bytes(frame, document=document)",
                    IMPORT_TABLE
                    + "        floors = {r: PLATFORM_PROMPT_BYTES.get(r, 0) for r in"
                    ' ("UNDERSTAND_VALUE", "PLAN_CAPABILITY")}',
                )
            ],
            [
                "test_the_reporter_prints_the_same_numbers_the_gate_computes",
                "test_the_two_byte_tables_have_no_computational_consumer",
            ],
            [],
        ),
        (
            "R6 · 同上，但 `import ... as _p` 之后 `_p[r]` —— **上一版同样全绿**",
            [
                Mutation(
                    REPORTER,
                    "        floors = request_floor_bytes(frame, document=document)",
                    IMPORT_TABLE_ALIASED
                    + "        floors = {r: _p[r] for r in"
                    ' ("UNDERSTAND_VALUE", "PLAN_CAPABILITY")}',
                )
            ],
            [
                "test_the_reporter_prints_the_same_numbers_the_gate_computes",
                "test_the_two_byte_tables_have_no_computational_consumer",
            ],
            [],
        ),
        (
            "R7 · 删掉报告器顶部那两行 reconfigure（复核 ④：这一处先前没有守卫）",
            [Mutation(REPORTER, RECONFIGURE, "")],
            ["test_the_reporter_runs_without_an_inherited_encoding_variable"],
            [],
        ),
        (
            "R8 · **生产**不再把合法取值告诉模型（字节那份看不见）",
            [Mutation(MODEL, TELL_MODEL, "")],
            ["test_both_sides_tell_the_model_the_allowed_values"],
            # **零连带，而这一支是全仓唯一守它的地方**：生产不再把合法取值
            # 告诉模型，除了这一条，没有任何东西会说话。R9（admission 那一半）
            # 同样。裁 B 那条「告知的集合 == 执行的集合」就挂在这两支上。
            [],
        ),
        (
            "R9 · **admission** 漏传同一个实参 —— 固定底比生产真实要付的少一截",
            [Mutation(REGISTRY, TELL_ADMISSION, "")],
            [
                "test_both_sides_tell_the_model_the_allowed_values",
                "test_the_floor_equals_the_request_it_claims_to_measure",
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
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
