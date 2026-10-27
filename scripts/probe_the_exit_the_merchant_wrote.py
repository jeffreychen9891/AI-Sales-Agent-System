#!/usr/bin/env python
"""反向探针：exit/tone 批那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **L1** | complete 闸换回「required 槽全满」（= 修复前）| 各归各那两条 |
| **L2** | equals 支整个当 present 处理 | equals 那条 |
| **L3** | 空串算「有值」 | present 那条 |
| **L4** | 封版死锁检查恒放行 | 死锁那条 |
| **L5** | schema 给谓词开回自由形状 | 形状那条 + 空数组那条 |
| **L6** | demo 包塞回一个 tone_material_ref（连 hash）| 「删净」那条 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_the_exit_the_merchant_wrote.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
⚠️ **配包变异连 `content_hash` 一起改**（缺陷 18 那支量过不改的代价）。
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
TARGET = "tests/contracts/test_the_exit_the_merchant_wrote.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 10
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2548
CONTROL_BASELINE_SKIPPED = 53

VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
SCHEMA = ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (VALIDATION, REGISTRY, SCHEMA, PROFILE)

READS_EXIT = '''    slots = frame.get("slots") or {}
    unmet: list[str] = []
    for condition in definition.get("exit_conditions") or []:
        name = str(condition.get("slot"))
        value = slots.get(name)
        if "equals" in condition:
            if value is None or str(value) != str(condition["equals"]):
                unmet.append(f"{name} != {condition['equals']!r}")
        elif value in (None, ""):
            unmet.append(f"{name} is empty")
    return unmet'''
#: L1 = 修复前：核 required 槽全满，出口谓词整个不看。
READS_REQUIRED = '''    values = (frame.get("slots") or {}).items()
    present = {n for n, v in values if v not in (None, "")}
    required = {
        s["name"] for s in definition.get("information_slots") or [] if s.get("required")
    }
    return [f"{name} is empty" for name in sorted(required - present)]'''
#: L2 = equals 退化成 present（有值就算相等）。
EQUALS_AS_PRESENT = '''    slots = frame.get("slots") or {}
    unmet: list[str] = []
    for condition in definition.get("exit_conditions") or []:
        name = str(condition.get("slot"))
        value = slots.get(name)
        if value in (None, ""):
            unmet.append(f"{name} is empty")
    return unmet'''
#: L3 = 空串算「有值」。
EMPTY_COUNTS = READS_EXIT.replace(
    'elif value in (None, ""):', "elif value is None:"
)

DEADLOCK_RAISES = '''    if offences:
        raise ProfilePackageError(
            f"profile package {path} has exit conditions pointing at slots the "'''
DEADLOCK_OFF = '''    if False:
        raise ProfilePackageError(
            f"profile package {path} has exit conditions pointing at slots the "'''

CLOSED_ITEMS = '''"items": {
              "type": "object",
              "additionalProperties": false,
              "required": [
                "slot"
              ],'''
#: L5a：自由形状 = additionalProperties 与 required 一起放开 ——
#: 只放开前者时 `{fact: …}` 仍因缺 `slot` 被 required 拒，守卫照绿，探针证不了事。
OPEN_ITEMS = '''"items": {
              "type": "object",'''
HAS_MIN = '''"exit_conditions": {
            "type": "array",
            "minItems": 1,'''
NO_MIN = '''"exit_conditions": {
            "type": "array",'''

PRESENT = "test_a_present_condition_is_unmet_until_the_slot_has_a_value"
EQUALS = "test_an_equals_condition_wants_that_value_not_any_value"
NOT_REQUIRED = "test_the_gate_no_longer_asks_about_required_slots"
NON_REQ_EXIT = "test_a_non_required_exit_slot_is_finally_enforced"
DEADLOCK = "test_a_predicate_pointing_at_a_missing_slot_is_refused"
WIRED = "test_a_wired_predicate_seals_and_the_shipped_packages_pass"
FREE_SHAPE = "test_a_free_shaped_predicate_is_refused_by_the_contract"
EMPTY_ARRAY = "test_an_empty_exit_conditions_array_is_refused"
TONE_GONE = "test_a_tone_material_ref_no_longer_seals"
CLEAN = "test_no_shipped_package_carries_a_tone_ref"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "L1 · complete 闸换回「required 槽全满」（= 修复前）",
            [Mutation(VALIDATION, READS_EXIT, READS_REQUIRED)],
            [PRESENT, EQUALS, NOT_REQUIRED, NON_REQ_EXIT],
            [],
        ),
        (
            "L2 · equals 退化成 present",
            [Mutation(VALIDATION, READS_EXIT, EQUALS_AS_PRESENT)],
            [EQUALS],
            [],
        ),
        (
            "L3 · 空串算「有值」",
            [Mutation(VALIDATION, READS_EXIT, EMPTY_COUNTS)],
            [PRESENT],
            [],
        ),
        (
            "L4 · 封版死锁检查恒放行",
            [Mutation(REGISTRY, DEADLOCK_RAISES, DEADLOCK_OFF)],
            [DEADLOCK],
            [],
        ),
        (
            "L5a · schema 给谓词开回自由形状",
            #: 单变异就够 —— CLOSED_ITEMS 本身含 required 块，一步删净。
            #: 第一版拆了两步，第二步找 required 时它已被第一步删掉（0 命中）。
            [Mutation(SCHEMA, CLOSED_ITEMS, OPEN_ITEMS)],
            [FREE_SHAPE],
            [],
        ),
        (
            "L5b · minItems 删掉（空数组 = 随时可收口）",
            [Mutation(SCHEMA, HAS_MIN, NO_MIN)],
            [EMPTY_ARRAY],
            [],
        ),
        #: ⚠️ **L6（给在架包塞回 tone）删掉了，实测记录在此**：
        #: 塞回的键被 additionalProperties 拒，包不再自洽，`load_package`
        #: 在每条读在架包的用例上炸 —— 对照面 **102 failed / 11 errors**
        #: （与缺陷 18 的 J2 同型）。那份红本身就是「键回不来」的全仓级
        #: 实证；`CLEAN` 那条的会红性由该次实测记录（红集 [CLEAN] + 上述连带），
        #: 「回不来」由 `TONE_GONE` 每次运行都在证。
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
