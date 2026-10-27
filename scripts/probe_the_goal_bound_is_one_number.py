#!/usr/bin/env python
"""反向探针：`goal` 上限那三条守卫，逐条验它会红。

**这一批守的是两件不同的事**，探针的重点是把那个区别做成可看见的：

* **两个数相等** —— 抬一边不抬另一边，在**任何配包超限之前**就该红；
* **那个数真的被执行** —— 两个数可以一致地错（契约写着 1,500 而校验器没跑），
  所以要有一条不依赖那个数的检查；
* **在架配包真的走一遍那条路** —— 2026-09-22 那个洞唯一的直接证人。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **A1** | 聚合侧 1,500 -> 1,200（重演 09-20 那一漏） | 相等那条 + 在架配包那条 |
| **A2** | 源头侧 1,500 -> 1,600（只抬一边，与 09-20 同向） | 相等那条 |
| **A3** | 聚合侧 `maxLength` 整条删掉 | 「真的被执行」那条 + 相等那条 |
| **A4** | `_initial_aggregate` 不再搬 goal | 在架配包那条 + 「真的被执行」那条 |

**A2 为什么往松里抬、而不是往紧里收**：往紧里收（1,500 -> 1,200）在架配包
当场违反源头契约，对照实测 98 failed / 11 errors —— 那支证明的是
「源头那一侧有很多执行者」，不是「分家会被抓到」。往松里抬才把变量
收在「两个数不相等」这一件事上，而且与 09-20 那一笔同向。

**A1 是本批的兑现物**：它重演的就是 `5b17f16f` 那一笔漏掉的半边，
而它红在**两条**上 —— 相等那条在配包还没超限时就会红，
在架配包那条则是今天这个洞的直接证人。**两条各挡一半**。

**A4 在守一条「别让上面那条变空」的断言**：`_initial_aggregate` 一旦不再把
goal 搬进栈，在架配包那条用例就是在校验一个空字段 —— 而一个恒空的字段
永远不会超限（`AGENTS.md`：它等的那个条件根本不会发生）。

    python scripts/probe_the_goal_bound_is_one_number.py
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
TARGET = "tests/contracts/test_the_goal_bound_is_one_number.py"

#: 按数断言，不按颜色。2 条具名 + 3 份在架配包的参数化。
#: **这个数是被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 5

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报**。
CONTROL_BASELINE_PASSED = 2413

#: `skipped` 是参数而不是写死的 0 —— 全量本来就跳这几条，
#: 而 `pytest.mark.skip` 正是让一整个文件闭嘴的手段，所以它也要报数。
CONTROL_BASELINE_SKIPPED = 53

AGGREGATE = ROOT / "packages/contracts/schemas/conversation-aggregate.schema.json"
PROFILE_SCHEMA = ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"

TOUCHED = (AGGREGATE, PROFILE_SCHEMA, PIPELINE)

BOUND = '"maxLength": 1500,'
#: 聚合侧那一整行（含缩进与换行）—— A3 把它整条删掉。
BOUND_LINE = '            "maxLength": 1500,\n'
#: `_initial_aggregate` 搬 goal 的那一行。
CARRY = '"goal": str(definition["goal"]),'

PINNED = "test_the_two_schemas_declare_the_same_goal_bound"
ENFORCED = "test_the_bound_is_actually_enforced_on_the_write_path"
#: ⚠️ **参数化的三条在这里是一个名字。** `probe_harness.failed_names` 报的是
#: 函数名，不带 `[internal-demo-sales]` 那一段 —— 所以「红了几份配包」要去读
#: `result.output` 里的 `N failed`，不能从这个集合里数。
#: 第一版按 `name[param]` 写期望，四支里两支判成 ❌，而它们其实红对了。
SHIPPED = "test_every_shipped_package_can_open_a_conversation"


run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            # **本批兑现物，而它的产出在对照那一栏**：把今天这个洞原样装回去，
            # `tests/contracts` 里**一条都不红**（对照 0 failed）。
            # 本文件那两条就是全仓仅有的证人 —— 这正是「删掉会红哪条」的答案。
            "A1 · 聚合侧 1,500 -> 1,200（重演 2026-09-20 那一漏）—— **本批兑现物**",
            [Mutation(AGGREGATE, BOUND, '"maxLength": 1200,')],
            # 本文件 2 failed / 3 passed：钉死那条 + 三份配包里的 internal-demo-sales
            [PINNED, SHIPPED],
            [],
        ),
        (
            # **与 09-20 同向**：只动源头、且往**松**里动。
            # 往紧里动（1,500 -> 1,200）也会红钉死那条，而在架配包当场违反源头契约，
            # 对照实测 **98 failed / 11 errors**（封版、注册表、投影、栈那一片全打穿）——
            # 那个变异证明的是「源头那一侧有很多执行者」，不是「分家会被抓到」。
            # 所以取这一支：它把变量收在「两个数不相等」这一件事上。
            "A2 · 源头侧 1,500 -> 1,600（只抬一边，与 09-20 同向）",
            [Mutation(PROFILE_SCHEMA, BOUND, '"maxLength": 1600,')],
            [PINNED],
            [],
        ),
        (
            "A3 · 聚合侧 maxLength 整条删掉",
            [Mutation(AGGREGATE, BOUND_LINE, "")],
            [PINNED, ENFORCED],
            # goal 变回一个没人分过类的无界字段 —— 批 2 那条分类守卫接住它。
            # **这一条是预判之外的，实测出来的**：本批与批 2 在这个字段上是两重。
            ["test_every_unbounded_runtime_field_has_been_classified"],
        ),
        (
            # 三份配包的「搬过去了」断言 + `ENFORCED` 也依赖这一行。
            # 本文件 4 failed（三份参数化各一 + ENFORCED），去重后两个名字。
            "A4 · `_initial_aggregate` 不再搬 goal",
            [Mutation(PIPELINE, CARRY, '"goal": "",')],
            [SHIPPED, ENFORCED],
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
            # **两边都按集合相等判**，不是「包含」。
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
