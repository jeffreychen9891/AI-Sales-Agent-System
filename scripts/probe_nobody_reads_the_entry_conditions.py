#!/usr/bin/env python
"""反向探针：批三那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **J1** | schema 把 `entry_conditions` 加回去（= 收掉之前）| 「封版即拒」那条 |
| **J2** | 给 demo 包塞回一条 entry_conditions（连 hash）| 「在架包删净」那条 |
| **J3** | `advance_when` 校验恒放行 | 封版那四条 |
| **J4** | push-指自己那支单独关掉 | 指自己那条 |
| **J5** | delivered-ref 那支单独关掉 | ref 那条 |
| **J6** | slot-本帧那支单独关掉 | slot 那条 |
| **J7** | business_event_type 封版拒恒放行 | 那条拒的用例 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_nobody_reads_the_entry_conditions.py
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
TARGET = "tests/contracts/test_nobody_reads_the_entry_conditions.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 11
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2530
CONTROL_BASELINE_SKIPPED = 53

SCHEMA = ROOT / "packages/contracts/schemas/merchant-profile.schema.json"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (SCHEMA, REGISTRY, PROFILE)

WITHOUT_KEY = '''        "required": [
          "id",
          "goal",
          "exit_conditions",'''
WITH_KEY = '''        "required": [
          "id",
          "goal",
          "entry_conditions",
          "exit_conditions",'''
#: J1 还要把 properties 里的定义加回去 —— 只加 required 会让**三个在架包**
#: 全部封不出（缺键），无差别红。加回 properties + 不加 required 才是
#: 「schema 又认识它了」的最小形状。
NO_PROP = '''          "exit_conditions": {
            "type": "array",
            "items": {
              "type": "object"
            }
          },'''
WITH_PROP = '''          "entry_conditions": {
            "type": "array",
            "items": {
              "type": "object"
            }
          },
          "exit_conditions": {
            "type": "array",
            "items": {
              "type": "object"
            }
          },'''

CHECK_RAISES = '''    if offences:
        raise ProfilePackageError(
            f"profile package {path} has advance_when rules that can never fire "'''
CHECK_OFF = '''    if False:
        raise ProfilePackageError(
            f"profile package {path} has advance_when rules that can never fire "'''

SELF_PUSH = '''            elif target == frame_id:
                offences.append(f"{frame_id}: push points at itself (runtime skips it)")'''
SELF_PUSH_OFF = '''            elif False:
                offences.append(f"{frame_id}: push points at itself (runtime skips it)")'''

REF_CHECK = (
    '            if "delivered" in rule and '
    'str(rule["delivered"]) not in declared_refs:'
)
REF_OFF = '''            if False and "delivered" in rule:'''

SLOT_CHECK = '''            if "slot" in rule and str(rule["slot"]) not in slots:'''
BUSINESS_RAISES = (
    "    if offences:\n"
    "        raise ProfilePackageError(\n"
    '            f"profile package {path} gates time rules on business_event_type, "'
)
BUSINESS_OFF = (
    "    if False:\n"
    "        raise ProfilePackageError(\n"
    '            f"profile package {path} gates time rules on business_event_type, "'
)

SLOT_OFF = '''            if False and "slot" in rule:'''

REJECTS = "test_the_schema_now_rejects_a_frame_that_carries_the_key"
CLEAN = "test_no_shipped_package_carries_the_key_any_more"
NO_READER = "test_the_platform_has_no_reader_left_to_orphan"
BAD_PUSH = "test_a_push_at_a_missing_frame_is_refused"
SELF = "test_a_push_at_itself_is_refused"
GHOST_REF = "test_a_delivered_ref_nobody_declares_is_refused"
WRONG_SLOT = "test_a_slot_from_another_frame_is_refused"
WIRED = "test_a_wired_rule_seals"
BUSINESS = "test_a_rule_gated_on_a_business_event_is_refused_at_seal_time"
SHIPPED = "test_the_shipped_rules_are_all_wired"
#: J1 的连带：schema 与 RFC §3.6 的字段对照（现量现比，职责使然）。
BLUEPRINT = "test_the_blueprint_lists_exactly_the_keys_the_schema_declares"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "J1 · schema 把键加回去（= 收掉之前）",
            [Mutation(SCHEMA, NO_PROP, WITH_PROP)],
            [REJECTS],
            #: 连带正好是 RFC 对照用例 —— schema 多了键而 §3.6 没跟，
            #: 那正是它的职责（量出来的）。
            [BLUEPRINT],
        ),
        #: ⚠️ **J2（给在架包塞回一条）删掉了，实测记录在此**：
        #: 那个变异让包过不了 schema，`load_package` 在**每一条**要读在架包的
        #: 用例上炸 —— 对照面 102 failed / 11 errors。**那份红本身就是
        #: 「additionalProperties 使带键的包封版即拒」的全仓级实证**，
        #: 但一支每次要付 102 条红的探针证不了比 J1 更多的东西
        #: （「键回不来」两支重叠）。`CLEAN` 那条的会红性由该次实测记录：
        #: 变异在本文件 git 历史的第一版里，红集 `[CLEAN]`（SHIPPED 不红 ——
        #: 它直接 `json.loads` 不走 schema）。
        (
            "J3 · advance_when 校验恒放行 —— **本批兑现物**",
            [Mutation(REGISTRY, CHECK_RAISES, CHECK_OFF)],
            [BAD_PUSH, SELF, GHOST_REF, WRONG_SLOT],
            [],
        ),
        (
            "J4 · push-指自己那支单独关掉",
            [Mutation(REGISTRY, SELF_PUSH, SELF_PUSH_OFF)],
            [SELF],
            [],
        ),
        (
            "J5 · delivered-ref 那支单独关掉",
            [Mutation(REGISTRY, REF_CHECK, REF_OFF)],
            [GHOST_REF],
            [],
        ),
        (
            "J6 · slot-本帧那支单独关掉",
            [Mutation(REGISTRY, SLOT_CHECK, SLOT_OFF)],
            [WRONG_SLOT],
            [],
        ),
        (
            "J7 · business_event_type 封版拒恒放行",
            [Mutation(REGISTRY, BUSINESS_RAISES, BUSINESS_OFF)],
            [BUSINESS],
            [],
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
