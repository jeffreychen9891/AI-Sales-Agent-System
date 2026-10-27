#!/usr/bin/env python
"""反向探针：ADR-0150 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **C1** | 弹栈时不归档（去掉那一行）= 修复前 | 带得过去那几条 |
| **C2** | 种新帧时不看归档（恒返回两个空字典） | 同上，**而它与 C1 不是一回事** |
| **C3** | **一律全带**（`_slots_that_carry` 不看 `on_revisit`） | 「他这次的反应不许带」那条 |
| **C4** | 种的那一头不再核 `carry` | 「商家改回 `reset` 立刻生效」那条 |
| **C5** | 把去掉的那道闸**加回来** | 「推进恰好一次」那条 |
| **C6** | 配包里那一格改回 `reset`（**要重新盖章**） | 在架声明那两条 + 钉章的两条 |

## 实测：**六支全红在该红的地方，而我预测的集合有三处不全**

C1 / C6 各多一条（`RESET_STAYS_RESET`：它的前提是「归档里确实有东西」，
而那两支之后那一格不存在）、C3 多一条（`SURVIVES` 按等值断言归档里是哪几格）、
C5 的对照多一条（`GO_AND_QUOTE`）。**脚本里写的是量到的那几条，不是想出来的。**

⚠️ **C5 对照里那条 `test_two_sections_told_means_go_and_quote` 是最有说服力的一条**：
把去掉的那道闸加回来，「两段讲完，平台自己进报价帧」当场不成立 ——
因为在架那份今天把 `quote` 声明成了 `reenter`。
**ADR-0150 §五说的「配包照样封得出、只是不会走到报价了」，这里是它的可见形态。**

## ⚠️ C1 与 C2 分开，因为它们**不是**同一件事

两条路都能让「第二次进报价帧没带上那个数」发生，而它们坏的地方不同：
C1 是**那一刻没存下来**，C2 是**存下来了但没人去取**。
一条只覆盖其中一头的实现会让另一头悄悄没有守卫 —— **让探针去量，量到什么写什么。**

## ⚠️ C6 要 `repin`

它改的是 `profiles/internal-demo-sales/profile.json`，改完
`identity.content_hash` 就对不上了。**不重新盖章的话，量到的是哈希守卫，
不是这条规则**（`probe_topic_slot_vocabulary` 的 V3 就是这么栽的：
本文件看不出来，对照一开 84 failed / 11 errors）。

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_a_second_visit_is_not_the_first.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8**，不要求调用方 export `PYTHONIOENCODING`。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeError,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_a_second_visit_is_not_the_first.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 10
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2442
CONTROL_BASELINE_SKIPPED = 53

STACK = ROOT / "services/worker/src/fastagent_v2/worker/target_stack.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (STACK, PROFILE)

ARCHIVE_CALL = "        _archive_carried_slots(updated, profile, child)\n"

SEED_BODY = """    store = aggregate.get("carried_slots")
    entry = store.get(definition_id) if isinstance(store, dict) else None
    if not isinstance(entry, dict):
        return {}, {}
    carried = _slots_that_carry(profile, definition_id)"""
SEED_BLIND = """    store = aggregate.get("carried_slots")
    entry = store.get(definition_id) if isinstance(store, dict) else None
    if not isinstance(entry, dict) or True:
        return {}, {}
    carried = _slots_that_carry(profile, definition_id)"""
SEED_UNFILTERED = """    store = aggregate.get("carried_slots")
    entry = store.get(definition_id) if isinstance(store, dict) else None
    if not isinstance(entry, dict):
        return {}, {}
    carried = frozenset(entry.get("slots") or {})"""

CARRY_FILTER = '        if str(slot.get("on_revisit") or RESET_ON_REVISIT) == CARRY_ON_REVISIT'
CARRY_EVERYTHING = "        if True"

ADVANCE_GATE = """        if target in ever_entered or target == str(current["definition_id"]):
            continue"""
_OLD_GATE_HEAD = '        if str(_definition(profile, target).get("revisit")'
_OLD_GATE_TAIL = ' or "reenter") != "brief_answer":'
ADVANCE_GATE_WITH_THE_OLD_ONE = f"""{_OLD_GATE_HEAD}{_OLD_GATE_TAIL}
            continue
        if target in ever_entered or target == str(current["definition_id"]):
            continue"""

PROFILE_CARRY = '          "on_revisit": "carry"'
PROFILE_RESET = '          "on_revisit": "reset"'

SURVIVES = "test_a_carry_slot_survives_the_pop_with_its_evidence"
RESET_STAYS_RESET = "test_a_reset_slot_does_not_survive"
EXIT_GATE = "test_the_carried_value_satisfies_the_platform_exit_gate"
DECLARATION_NOW = "test_changing_the_declaration_takes_effect_without_waiting"
NO_TRACE = "test_a_frame_with_nothing_to_carry_leaves_no_trace"
THE_PAIR = "test_the_shipped_package_declares_the_pair"
ONLY_ONE = "test_no_other_shipped_frame_carries_anything_yet"
ADVANCE_ONCE = "test_a_reenter_target_is_advanced_into_once_and_not_twice"
#: ⚠️ **C5 的第二条连带，量出来的 —— 我预测时漏了它，而它是最有说服力的一条**：
#: 「两段讲完，平台自己进报价帧」。把那道闸加回来之后它当场红，
#: 因为在架那份今天把 `quote` 声明成了 `reenter` ——
#: **那正是 ADR-0150 §五说的「安静地不走到报价」，这里是它的可见形态**。
GO_AND_QUOTE = "test_two_sections_told_means_go_and_quote"
#: C6 改的是**盖过章的产物**，所以那两条钉章的守卫必然跟着红。
#: `repin` 让它们红在**新的**哈希上（不重新盖章的话红的是「章对不上」，
#: 那就什么也没验到）。两条都是量出来的。
STAMP_MOVES = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_STAMP = "test_the_runbook_quotes_the_stamp_in_the_package"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def reseal() -> None:
    """C6 的 `repin` —— 改完配包，**把章重新盖上**。

    **不自己算哈希**：`seal_profile_package.py` 的 docstring 逐字写着
    「有一份实现，另一个窗口去跑它，而不是自己算一遍」。
    """

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [str(PYTHON), str(ROOT / "scripts" / "seal_profile_package.py"), str(PROFILE.parent)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ProbeError(
            "reseal failed after the mutation, so every later run would be measuring "
            f"the hash guard instead of the rule: {(completed.stderr or '').strip()}"
        )


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str], bool]] = [
        (
            "C1 · 弹栈时不归档（= 修复前）—— **本批兑现物**",
            [Mutation(STACK, ARCHIVE_CALL, "")],
            #: ⚠️ `RESET_STAYS_RESET` 与 `DECLARATION_NOW` 是**量出来的**，
            #: 我预测时只写了另外两条。它们红是因为**各自的前提不再成立**：
            #: 两条都先断言「归档里确实有东西」，而不归档之后那一格不存在。
            #: **那个前提是有意留着的** —— 没有它，这两条会在
            #: 「什么都没带」的实现下变成永远为真的空守卫
            #: （`CLAUDE.md` 第三节末尾那句「守卫本身也可能是空的」）。
            [SURVIVES, EXIT_GATE, DECLARATION_NOW, RESET_STAYS_RESET],
            [],
            False,
        ),
        (
            "C2 · 存下来了，而种新帧时不去取",
            [Mutation(STACK, SEED_BODY, SEED_BLIND)],
            [SURVIVES, EXIT_GATE],
            [],
            False,
        ),
        (
            "C3 · 一律全带（不看 `on_revisit`）",
            [Mutation(STACK, CARRY_FILTER, CARRY_EVERYTHING)],
            #: ⚠️ `SURVIVES` 也红，量出来的：它按**等值**断言归档里是哪几格，
            #: 而全带之后那里多了 `price_feedback`。
            #: **等值而不是包含，本仓为这个差别付过钱。**
            [RESET_STAYS_RESET, NO_TRACE, DECLARATION_NOW, SURVIVES],
            [],
            False,
        ),
        (
            "C4 · 种的那一头不再按当时的配包过滤",
            [Mutation(STACK, SEED_BODY, SEED_UNFILTERED)],
            [DECLARATION_NOW],
            [],
            False,
        ),
        (
            "C5 · 把去掉的那道闸加回来（`advance_when` 目标必须 `brief_answer`）",
            [Mutation(STACK, ADVANCE_GATE, ADVANCE_GATE_WITH_THE_OLD_ONE)],
            #: 本文件一条都不红 —— **这一支的产出全在对照里**，
            #: 所以期望值写空是有意的，不是没想。
            [],
            [ADVANCE_ONCE, GO_AND_QUOTE],
            False,
        ),
        (
            "C6 · 在架那格改回 `reset`（**带 repin**）",
            [Mutation(PROFILE, PROFILE_CARRY, PROFILE_RESET)],
            #: `RESET_STAYS_RESET` 同 C1：前提不再成立（没有任何一格声明带）。
            [SURVIVES, EXIT_GATE, THE_PAIR, ONLY_ONE, DECLARATION_NOW, RESET_STAYS_RESET],
            [STAMP_MOVES, RUNBOOK_STAMP],
            True,
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
        for label, mutations, expects, collateral, needs_reseal in probes:
            result = session.probe(label, mutations, repin=reseal if needs_reseal else None)
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
