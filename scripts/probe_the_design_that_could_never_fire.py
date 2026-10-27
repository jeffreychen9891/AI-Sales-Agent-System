#!/usr/bin/env python
"""反向探针：适配审查批（K4/K1/K3）那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **P1** | K4 封版检恒放行（无 registrar 也封） | 无 registrar 那条 |
| **P2** | K1 检索闸恒放行（不绑也检） | 不绑不检那条 |
| **P3** | K3 封版检恒放行（无词也封） | 无词即拒那条 |
| **P4** | demo 包的绑定删回 `[]`（连 hash 一起回退） | demo 声明那条 + 对照面的钉戳 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_the_design_that_could_never_fire.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
⚠️ **配包变异连 `content_hash` 一起改**（缺陷 18 那支量过不改的代价）——
P4 的旧 hash（HASH_SANS）不是编的：它是「当前包删回绑定」变体的实算值，
所以「删回绑定」的文档恰好落回那个已量过的旧章。
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
TARGET = "tests/contracts/test_the_design_that_could_never_fire.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 10
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2558
CONTROL_BASELINE_SKIPPED = 53

REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (REGISTRY, PIPELINE, PROFILE)

K4_GUARDS = '''    if not registrars:
        raise ProfilePackageError(
            f"profile package {path} declares ai_commitment_due time rules "'''
K4_OFF = '''    if False:
        raise ProfilePackageError(
            f"profile package {path} declares ai_commitment_due time rules "'''

K1_GATES = '''        if not (profile.get("knowledge_bindings") or []):
            return None
        if self._retriever is None:'''
K1_OFF = '''        if self._retriever is None:'''

K3_GUARDS = '''    if offences:
        raise ProfilePackageError(
            f"profile package {path} allows strategies that must quote a material "'''
K3_OFF = '''    if False:
        raise ProfilePackageError(
            f"profile package {path} allows strategies that must quote a material "'''

BOUND = '''"knowledge_bindings": [
    "knowledge:demo-internal-corpus:v1"
  ],'''
UNBOUND = '''"knowledge_bindings": [],'''
HASH_NOW = "48c1fc39c2d53e569df0bb0e837aede2181dd0c835c8bcb049f1fcf9b123aea8"
HASH_SANS = "3383ea4b0e75a73b1aec3a183ad41e8dc847445b2a5f348b4a9ca1a6a89677a9"

NO_REGISTRAR = "test_a_commitment_timer_without_a_registrar_is_refused"
REGISTRAR_OK = "test_a_registrar_seals_and_the_shipped_packages_pass"
DEMO_CLEAN = "test_demo_no_longer_declares_the_dead_timer_rules"
UNBOUND_SILENT = "test_a_profile_with_no_bindings_never_retrieves"
BOUND_RETRIEVES = "test_a_bound_profile_still_retrieves"
DEMO_BOUND = "test_demo_now_declares_its_corpus_binding"
ALL_BOUND = "test_every_package_whose_tenant_retrieves_declares_a_binding"
WORDLESS_REFUSED = "test_a_must_quote_strategy_with_wordless_frames_is_refused"
WORDED_SEALS = "test_words_supplied_seals_and_a_non_quoting_frame_needs_none"
CARRIER_TEETH = "test_carrier_now_supplies_words_and_the_quote_gate_has_teeth"

#: P4 的对照面连带：demo 的「章随版走」钉戳读同一份 identity 三元组；
#: runbook 钉戳把同一枚章抄给运维（按 3 份 rollout 文档参数化，
#: 所以对照面数出 4 红、去重后是这两个名字）。
STAMP_PIN = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_PIN = "test_the_runbook_quotes_the_stamp_in_the_package"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "P1 · K4 封版检恒放行（无 registrar 也封）",
            [Mutation(REGISTRY, K4_GUARDS, K4_OFF)],
            [NO_REGISTRAR],
            [],
        ),
        (
            "P2 · K1 检索闸恒放行（不绑也检）",
            [Mutation(PIPELINE, K1_GATES, K1_OFF)],
            [UNBOUND_SILENT],
            [],
        ),
        (
            "P3 · K3 封版检恒放行（无词也封）",
            [Mutation(REGISTRY, K3_GUARDS, K3_OFF)],
            [WORDLESS_REFUSED],
            [],
        ),
        (
            "P4 · demo 绑定删回 []（hash 同步回退到量过的旧章）",
            [
                Mutation(PROFILE, BOUND, UNBOUND),
                Mutation(PROFILE, HASH_NOW, HASH_SANS),
            ],
            #: 三包全绑那条也断言 demo 的值，所以它一起红 —— 两条红都在职责上。
            [DEMO_BOUND, ALL_BOUND],
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
