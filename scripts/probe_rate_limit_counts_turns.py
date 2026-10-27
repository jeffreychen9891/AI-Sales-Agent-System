#!/usr/bin/env python
"""反向探针：速率闸数的是轮不是条（T368，用户 2026-09-19 裁定）。

    python scripts/probe_rate_limit_counts_turns.py

| | 变异 | 演的是 |
|---|---|---|
| A | 会话那一边改回 `count(*)` | **本批之前的样子**：一轮多行被当成多轮 |
| B | 原子性改回按名额截断 | **本批之前的样子**：许诺留下、兑现被扣住 |
| C | 会话那一边报错 `scope` | 待办与指标指错地方，人去错的地方查 |
| D | 劈开之后那一遍丢弃不调了 | 许诺那句照发 —— 客户收到一句空许诺 |
| E | 那一遍丢弃不接住异常 | 一句不剩时**整轮在持久化事务里垮掉** |
| F | 一个前缀加冒号 | 轮次键读到前缀后半 —— 一分钟的回合塌成一轮 |
| G | 审计行里去掉 `promises_dropped` | 「那条为什么少一句」查不到 |
| H | 两个上限的键名改回同名 | 单位不同而并列，读的人按同一个单位读 |

## 两条边

**放过了**：A / B / D / F / G —— 数错、劈开、不丢、塌成一轮、不留痕。
**误伤了**：E（整轮垮掉）、C（指错地方）。
只有一边的探针，一个「凡是超额一律整轮不发」的实现能拿满分。

## ⚠️ 有一样这批**没有**守卫，写出来

**「5」这个数本身没有用例钉它。** 本文件与 e2e 那份的算术都写在常数上 ——
那是有意的（`test_rate_gate.py` 的模块 docstring：
「a recalibration reads as a recalibration and not as a regression」）。
所以把 5 改成 3，**全仓一条都不红**。那是设计，不是洞：
它是用户裁定的数，改它要经过用户，而不是经过用例。
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
    failed_names,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_the_rate_limit_counts_turns.py"

#: 实测 2026-09-19，随门禁回填。
BASELINE_PASSED = 19
CONTROL_BASELINE_PASSED = 2099

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
TOUCHED = (PIPELINE,)

#: A —— 会话那一边改回数条数。
COUNTS_TURNS = """              count(DISTINCT {TURN_KEY_SQL}) FILTER (WHERE conversation_id = %s)
                AS turns_in_conversation,"""
COUNTS_MESSAGES = """              count(*) FILTER (WHERE conversation_id = %s)
                AS turns_in_conversation,"""

#: B —— 原子性改回按名额截断（本批之前的形状）。
ATOMIC_TURN = """        if turns_in_conversation >= RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE:
            return [], list(accepted), "conversation\""""
TRIM_BY_ROOM = """        conversation_room = max(
            0, RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - turns_in_conversation
        )
        if conversation_room < len(accepted):
            return accepted[:conversation_room], accepted[conversation_room:], "conversation\""""

#: C —— 会话那一边报错 scope（待办与指标会指错地方）。
#:
#: ⚠️ **第一版是「把租户那段挪到前面」，而我写成了在末尾加一行注释** ——
#: 语义一个字没变，探针报 GREEN。**无效变异给的是假信号**，
#: 本仓点过名；换成改那个标签，它是同一个缺陷的可达形态。
CONVERSATION_SAYS_SO = """            return [], list(accepted), "conversation\""""
CONVERSATION_BLAMES_THE_TENANT = """            return [], list(accepted), "tenant\""""

#: D —— 劈开之后那一遍丢弃不调了。
CALLS_THE_DROP = """            if withheld and kept:
                kept, also_withheld = self._drop_promises_the_split_no_longer_keeps(
                    kept=kept, generation=generation, dropped=split_promises
                )
                withheld = [*withheld, *also_withheld]"""
SKIPS_THE_DROP = """            if False:
                pass"""

#: E —— 那一遍丢弃不接住异常。
CATCHES_IT = """        except GenerationRejected:"""
LETS_IT_FLY = """        except ZeroDivisionError:"""

#: F —— 一个前缀加冒号。
CLEAN_PREFIX = """        idempotency_prefix="send_material","""
COLON_PREFIX = """        idempotency_prefix="send:material","""

#: G —— 审计行里去掉那一项。
RECORDS_THE_DROPS = """                        "promises_dropped": ["""
RECORDS_NOTHING = """                        "promises_never": ["""

#: H —— 两个上限的键名改回同名（单位混在一起）。
UNITS_IN_THE_NAMES = """                        "conversation_turn_limit": ("""
UNITS_LOST = """                        "conversation_limit": ("""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 会话那一边改回数条数",
            [Mutation(PIPELINE, COUNTS_TURNS, COUNTS_MESSAGES)],
            ["test_the_counting_query_counts_distinct_turn_keys"],
            [],
        ),
        (
            "B · 原子性改回按名额截断",
            [Mutation(PIPELINE, ATOMIC_TURN, TRIM_BY_ROOM)],
            # ⚠️ **红的不是那条名字里写着「never splits」的** ——
            # 额度正好满时按轮与按名额都是 `room = 0`，行为逐字相同。
            # 红的是两条「还没满」的：那里旧实现会留下第一条、扣住第二条。
            # **这支探针的产出就是这个发现**，用例的名字与 docstring 已改。
            [
                "test_a_turn_under_the_limit_passes_whole",
                "test_the_last_turn_in_the_window_still_goes_out_whole",
            ],
            [],
        ),
        (
            "C · 会话那一边报错 scope",
            [Mutation(PIPELINE, CONVERSATION_SAYS_SO, CONVERSATION_BLAMES_THE_TENANT)],
            [
                "test_a_full_window_withholds_the_whole_turn",
                "test_the_conversation_limit_wins_a_tie",
            ],
            [],
        ),
        (
            "D · 劈开之后那一遍丢弃不调了",
            [Mutation(PIPELINE, CALLS_THE_DROP, SKIPS_THE_DROP)],
            ["test_the_pipeline_actually_asks_for_this"],
            [],
        ),
        (
            "E · 那一遍丢弃不接住异常",
            [Mutation(PIPELINE, CATCHES_IT, LETS_IT_FLY)],
            ["test_a_split_that_leaves_nothing_but_a_promise_withholds_it_too"],
            [],
        ),
        (
            "F · 一个前缀加冒号",
            [Mutation(PIPELINE, CLEAN_PREFIX, COLON_PREFIX)],
            ["test_no_write_path_prefix_contains_a_colon"],
            [],
        ),
        (
            "G · 审计行里去掉 promises_dropped",
            [Mutation(PIPELINE, RECORDS_THE_DROPS, RECORDS_NOTHING)],
            ["test_the_audit_row_carries_the_promises_it_dropped"],
            [],
        ),
        (
            "H · 两个上限的键名改回同名",
            [Mutation(PIPELINE, UNITS_IN_THE_NAMES, UNITS_LOST)],
            ["test_the_two_limits_do_not_share_one_unit_in_the_audit_row"],
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
            names = set(failed_names(result.output))
            hit = names == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(result)
            if result.went_red and not hit:
                print(
                    "    ⚠️ 红了，但不是声称的那一组："
                    f"多红 {sorted(names - set(expects))}；没红 {sorted(set(expects) - names)}"
                )
            if not elsewhere:
                print(
                    "    ⚠️ 连带集合与声称的不一致："
                    f"实测 {sorted(result.collateral)}，声称 {sorted(collateral)}"
                )
            print()

    print("=" * 72)
    silent = [label for label, went_red, _, _ in verdicts if not went_red]
    misaimed = [label for label, went_red, hit, _ in verdicts if went_red and not hit]
    drifted = [label for label, _, _, elsewhere in verdicts if not elsewhere]
    for label, went_red, hit, elsewhere in verdicts:
        if not went_red:
            mark = "GREEN ❌"
        elif not hit:
            mark = "RED ⚠️ 红错了"
        elif not elsewhere:
            mark = "RED ⚠️ 连带变了"
        else:
            mark = "RED ✅"
        print(f"  {mark:>14}  {label}")
    if silent:
        print(f"\n**{len(silent)} 支探针没红，它们证明不了任何东西**")
    if misaimed:
        print(f"\n**{len(misaimed)} 支红在别的用例上**")
    if drifted:
        print(f"\n**{len(drifted)} 支的连带集合与声称的不一致**")
    return 1 if (silent or misaimed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
