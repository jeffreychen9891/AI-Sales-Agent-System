#!/usr/bin/env python
"""反向探针：批 6 那条守卫，逐条验它会红。

守的是 `tests/contracts/test_intent_transitions_share_one_principal.py` ——
图纸 RFC:192 要「模型 / policy / executor 三个 principal 不能兼任」，
而今天三段状态迁移**同一个进程、同一个 DB 角色**。
**用户 2026-09-15 裁定 (乙)：登记 + 绊线，而「不是决定不做，是前置未备」。**

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **N1** | 新增一处 `'PROPOSED'` 创建（清单悄悄变长） | 落点那条 |
| **N2** | 新增一处 `-> APPROVED` | 落点那条 |
| **N3** | 把 `-> EXECUTING` 那处挪走（清单变短） | 落点那条 |
| **N4** | worker 进程里引入第三种连接设置（分离真的开始了） | 连接那条 |

**这支探针钉的东西和别的不一样，值得说明白。**
别的批次钉的是「某个缺陷不要复发」；**这一支钉的是一张清单不要悄悄变长或变短** ——
因为做权限分离的那一批要照着它逐处改，而**这一批已经为「清单会漂」付过学费**：
survey 的 C1 行写的两个坐标今天分别是 handoff 理由码与 reconcile 时限。

**N4 是「好消息也要红」的那一支**：有人真开始做分离时它红 ——
红不代表错，**代表该回来把登记改了**。一条只在坏事发生时说话的守卫，
会让好事发生时的那次更新被忘掉。

    python scripts/probe_intent_principal.py
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
TARGET = "tests/contracts/test_intent_transitions_share_one_principal.py"

BASELINE_PASSED = 2

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1688

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
CLI = ROOT / "services/worker/src/fastagent_v2/worker/cli.py"

TOUCHED = (PIPELINE, CLI)

#: `digest` 之外那处 `-> APPROVED` 的整块（`pipeline.py`）。锚点唯一。
#: **两处 `-> APPROVED` 的 SQL 逐字相同**，所以锚点要带上各自的尾参数才唯一 ——
#: harness 强制命中数为 1，而它拒绝部分变异是对的：**只改两处中的一处，证不了东西**。
APPROVED = "            (prepared.tenant_id, decision.intent_id),\n"
APPROVED_TWICE = (
    "            (prepared.tenant_id, decision.intent_id),\n"
    "        )\n"
    "        connection.execute(\n"
    '            """\n'
    "            UPDATE intent_records SET status = 'APPROVED', updated_at = now()\n"
    "             WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'\n"
    '            """,\n'
    "            (prepared.tenant_id, decision.intent_id),\n"
)

EXECUTING = "                       SET status = 'EXECUTING', execution_token = %s,\n"

#: N1 在一条注释里插一段新的 `VALUES (… 'PROPOSED', …)`。
#: **必须带尾逗号** —— 守卫按 `'PROPOSED'\\s*,` 找「创建」，
#: 而 `WHERE status = 'PROPOSED'` 没有尾逗号，两者正是靠这个区分的。
PROPOSED_ANCHOR = (
    "            ) VALUES (%s, %s, %s, %s, 'PROPOSED', %s, %s, %s, %s, %s, %s, %s)\n"
)
PROPOSED_TWICE = (
    "            ) VALUES (%s, %s, %s, %s, 'PROPOSED', %s, %s, %s, %s, %s, %s, %s)\n"
    "            -- probe: ) VALUES (%s, 'PROPOSED', %s)\n"
)

CONNECTION = "    database = Database(settings.worker_database_url)"
CONNECTION_THIRD = (
    "    database = Database(settings.worker_database_url)\n"
    "    _probe = Database(settings.executor_database_url)"
)

SITES = "test_the_transition_sites_are_still_the_ones_we_counted"
ONE_CONNECTION = "test_all_three_stages_still_run_on_one_connection"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "N1 · 新增一处 `'PROPOSED'` 创建（清单悄悄变长）",
            [Mutation(PIPELINE, PROPOSED_ANCHOR, PROPOSED_TWICE)],
            [SITES],
            # **四支的连带集合全是空的**（2026-09-16 对照跑，基线 `88882184`）。
            # 那是这条登记的全部处境：三段状态迁移的落点，除了这张清单，
            # 全仓没有第二处在看。**它也是那张清单的弱点的另一面** ——
            # 见本文件 docstring 末尾那三行（换文件 / 换写法 / 一行注释）。
            [],
        ),
        (
            "N2 · 新增一处 `-> APPROVED`",
            [Mutation(PIPELINE, APPROVED, APPROVED_TWICE)],
            [SITES],
            [],
        ),
        (
            "N3 · 把 `-> EXECUTING` 那处挪走（清单变短）",
            [Mutation(PIPELINE, EXECUTING, "                       SET execution_token = %s,\n")],
            [SITES],
            [],
        ),
        (
            "N4 · worker 里引入第三种连接 —— **好消息也要红**",
            [Mutation(CLI, CONNECTION, CONNECTION_THIRD)],
            [ONE_CONNECTION],
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
        print("**N4 是「好消息也要红」的那一支**：有人真开始做权限分离时它红 ——")
        print("红不代表错，代表该回来把登记改了。只在坏事发生时说话的守卫，")
        print("会让好事发生时的那次更新被忘掉。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
