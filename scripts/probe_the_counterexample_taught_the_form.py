#!/usr/bin/env python
"""反向探针：缺陷 23/24/25 批那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **R1** | 反例整句塞回 introduce goal（连 hash） | 「无可借用模板」那条 |
| **R2** | 兜底留痕辅助函数只回拒绝现场（provenance 丢掉） | 「兜底要留来路」那条 |
| **R3** | quote 的「报价不外推」整句删掉（连 hash） | 「报价在我们这边」那条 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_the_counterexample_taught_the_form.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
⚠️ **配包变异连 `content_hash` 一起改**（缺陷 18 那支量过不改的代价）——
两个变体 hash 都是把变异后的文档实算出来的，不是编的。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8**，不要求调用方 export `PYTHONIOENCODING`。
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
TARGET = "tests/contracts/test_the_counterexample_taught_the_form.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 5
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2568
CONTROL_BASELINE_SKIPPED = 53

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (PIPELINE, PROFILE)

HASH_NOW = "48c1fc39c2d53e569df0bb0e837aede2181dd0c835c8bcb049f1fcf9b123aea8"
#: 变体 hash 都是实算的（跑一遍变异后的文档过 `canonical_hash`），不是编的。
HASH_WITH_TEMPLATE = "bed3085764d21b6466ff09309500591f887d7d3bbb97f8bfdb048dc5a560c12e"
HASH_SANS_QUOTE_RULE = "fe8ef7fa210ccb22707ccf7f41919551ba37e8775807442ee581063140879a0c"

CLEAN_TAIL = "接上就走。"
TEMPLATE_BACK = "接上就走（「这话我接住了」是白说）。"
QUOTE_RULE = "**报价是你的事**：他问价你就报数，**不要把报价这件事反过来推给他**。"

MERGES = '''    return {
        **dict(rejection.context),
        "fallback_strategy": plan.strategy_id,
        "fallback_origin": str(plan.document.get("origin") or ""),
    }'''
DROPS = '''    return dict(rejection.context)'''

NO_TEMPLATE = "test_no_goal_carries_a_borrowable_template"
POSITIVE_PATH = "test_the_positive_path_replaced_the_template"
TODO_RULE = "test_the_todo_is_not_a_topic"
QUOTE_SIDE = "test_the_quote_stays_on_our_side_of_the_table"
FALLBACK_NAMED = "test_a_fallback_denial_names_its_fallback"

#: 配包 hash 换掉时对照面的连带：章戳钉 + runbook 钉（3 份 rollout 文档参数化）。
STAMP_PIN = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_PIN = "test_the_runbook_quotes_the_stamp_in_the_package"
#: R1 独有的第二类连带：goal 正好压在 1400 字上，塞回 18 字的反例就撑破
#: 两道字数守卫 —— 红在职责上（「加东西前先砍铺陈」正是它们的规则）。
CEILING_OWN = "test_the_goal_still_fits_under_its_own_ceiling"
CEILING_CONTRACT = "test_the_goal_still_fits_the_contract_with_room_to_spare"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "R1 · 反例整句塞回 introduce goal（连 hash）",
            [
                Mutation(PROFILE, CLEAN_TAIL, TEMPLATE_BACK),
                Mutation(PROFILE, HASH_NOW, HASH_WITH_TEMPLATE),
            ],
            [NO_TEMPLATE],
            [STAMP_PIN, RUNBOOK_PIN, CEILING_OWN, CEILING_CONTRACT],
        ),
        (
            "R2 · 兜底留痕只回拒绝现场（provenance 丢掉）",
            [Mutation(PIPELINE, MERGES, DROPS)],
            [FALLBACK_NAMED],
            [],
        ),
        (
            "R3 · quote 的「报价不外推」整句删掉（连 hash）",
            [
                Mutation(PROFILE, QUOTE_RULE, ""),
                Mutation(PROFILE, HASH_NOW, HASH_SANS_QUOTE_RULE),
            ],
            [QUOTE_SIDE],
            [STAMP_PIN, RUNBOOK_PIN],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                skipped=BASELINE_SKIPPED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            # **两边都按集合相等判**，不是「包含」。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    aimed = all(hit and other for _, hit, other in verdicts)
    print("ALL AIMED" if aimed else "SOMETHING WENT RED SOMEWHERE ELSE (or did not go red)")
    return 0 if aimed else 1


if __name__ == "__main__":
    raise SystemExit(main())
