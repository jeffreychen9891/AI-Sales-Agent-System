#!/usr/bin/env python
"""反向探针：批 2 那两半守卫，逐条验它会红。

**两半守的是两件不同的事**，这支探针的重点就是把那个区别做成可看见的：

* **运行期侧** —— `tests/contracts/test_payload_fields_are_bounded.py`：
  聚合 / understanding / plan 三份契约里，每个无界字段都要被分过类；
* **配包侧** —— `tests/contracts/test_profile_materials_are_trusted.py` 那条
  「固定底装不下就拒包」：配包侧 45 处无界字段**靠这一道整体闸**兜住，
  所以真正要钉的不是每个字段，**是这道闸还在、而且真的会拒**。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **W1** | 往聚合契约里加一个无界字符串字段 | 分类那条 |
| **W2** | 从 `EXEMPT` 里删掉一条仍然无界的路径 | 分类那条 |
| **W3** | 给 `EXEMPT` 加一条**已经有界**的路径（表里挂空位） | 分类那条 |
| **W4** | 把 `slots` 逐值那条 `maxLength` 删掉（回到批 2 之前） | 分类那条 |
| **P1** | 停掉配包侧那道「固定底装不下」的拒绝 | 固定底那条 |

**W4 是批 2 的兑现物**：`slots` 的值先前只有**写入侧**（`understanding-result`
的 `slot_updates[].value = 400`）有界，**存储侧什么都没有** ——
那 400 只在「所有写入方都走 `apply_slot_updates`」这个前提下成立。
删掉存储侧那条，W4 应当红在分类那条上（它变回一个没人分过类的无界字段）。

    python scripts/probe_payload_bounds.py
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
    "tests/contracts/test_payload_fields_are_bounded.py",
    "tests/contracts/test_profile_materials_are_trusted.py",
)

#: 按数断言，不按颜色。3 + 51。
#: **54 而不是 53**：批 11 往 `test_profile_materials_are_trusted.py` 加了一条
#: 跨包相等的守卫（`MAX_MODEL_INPUT_BYTES` 两份），而那份文件正是本探针两个
#: `TARGETS` 之一。**这个数是被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 54

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1636

AGGREGATE = ROOT / "packages/contracts/schemas/conversation-aggregate.schema.json"
GUARD = ROOT / "tests/contracts/test_payload_fields_are_bounded.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"

TOUCHED = (AGGREGATE, GUARD, REGISTRY)

#: 聚合契约里一处好下锚的位置 —— W1 在它旁边插一个无界字段。
ANCHOR_FIELD = '          "definition_id": {"type": "string"},'

#: `slots` 逐值那条界（批 2 加的）。W4 把它删掉。
SLOT_VALUE_BOUND = '\n              "maxLength": 400,'

#: 配包侧那道整体闸的拒绝语。P1 把它改掉，于是那条用例匹配不上。
REFUSAL = "光固定底就装不下"

CLASSIFIED = "test_every_unbounded_runtime_field_has_been_classified"
FLOOR = "test_a_goal_too_long_for_the_plan_request_is_refused"

run = suite_runner(ROOT, PYTHON, list(TARGETS))
control = control_runner(ROOT, PYTHON, list(TARGETS))


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "W1 · 往聚合契约里加一个无界字符串字段",
            [
                Mutation(
                    AGGREGATE,
                    ANCHOR_FIELD,
                    ANCHOR_FIELD + '\n          "a_new_note": {"type": "string"},',
                )
            ],
            [CLASSIFIED],
            [],
        ),
        (
            "W2 · 从 EXEMPT 里删掉一条仍然无界的路径",
            [Mutation(GUARD, '    ".annotation_refs": "不进 payload",\n', "")],
            [CLASSIFIED],
            [],
        ),
        (
            "W3 · 给 EXEMPT 加一条**已经有界**的路径（表里挂一个空位）",
            [
                Mutation(
                    GUARD,
                    '    ".annotation_refs": "不进 payload",\n',
                    '    ".annotation_refs": "不进 payload",\n'
                    '    ".target_stack[items].goal": "这条已经有界了",\n',
                )
            ],
            [CLASSIFIED],
            [],
        ),
        (
            "W4 · 删掉 `slots` 逐值那条 maxLength（回到批 2 之前）—— **本批兑现物**",
            [Mutation(AGGREGATE, SLOT_VALUE_BOUND, "")],
            [CLASSIFIED],
            # **2026-09-16 起这一条不再是空的**，而它变得不空的过程值得记一行：
            #
            # 补上「人工写槽位那道界」的用例之后，W4 的对照第一次跑出来是
            # `1 warning, 1 error` —— 那份新用例在**模块顶层**读契约里的
            # `maxLength`，W4 把它删掉，于是模块 import 就死，pytest 报的是
            # **收集错误、零条 `FAILED`**。探针数的是 `FAILED` 的名字，
            # 于是它读到一个**空集合** —— 与「什么都没红」逐字一样，**还报了 ✅**。
            #
            # 两头都修了：那份用例改成取不到就让**具名的用例**去红；
            # `probe_harness` 给收集错误也起个名字（`collection-error:` 前缀），
            # 从此一个没 import 成的模块不可能再读成「零连带」。
            ["test_the_contract_still_declares_a_per_value_bound"],
        ),
        (
            "P1 · 停掉配包侧那道「固定底装不下」的拒绝",
            [Mutation(REGISTRY, REFUSAL, "（已停用）")],
            [FLOOR],
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
        print("**两半守的是两件事**：W1–W4 钉「运行期每个无界字段都被分过类」，")
        print("P1 钉「配包侧那 45 处靠的那道整体闸还在」。少哪一半都会让另一半看起来够用。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
