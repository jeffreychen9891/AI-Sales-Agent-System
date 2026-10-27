#!/usr/bin/env python
"""反向探针：T276 那一批 —— `tests/contracts/test_filing_a_task_happens_in_one_place.py`。

本批的主张有三条，逐条要有一支探针证明**它真的会红**：

1. 引擎侧只有一个落待办的定义点；
2. 五个调用点把那十四个参数传齐了；
3. **重放一个回合至多落一条待办** —— 这一条是本批真正修掉的那个缺陷，
   所以它的探针最要紧：把短路删掉，四条路要一起红。

⚠️ **本批的探针带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去
本文件」，把连带集合**量出来**而不是想出来。本仓记着「连带只能量，不能想」——
上一个窗口凭印象写了四次，对过一次。

    python scripts/probe_filing_a_task_happens_in_one_place.py
"""

from __future__ import annotations

import re
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
TARGET = "tests/contracts/test_filing_a_task_happens_in_one_place.py"

#: 本文件自己那几条。**报数，不报颜色**（harness 规则四）。
BASELINE_PASSED = 7

#: `tests/contracts` 减去本文件之后的那个数。
CONTROL_BASELINE_PASSED = 2174

PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
GUARD = ROOT / "tests" / "contracts" / "test_manual_task_owner_sla_expiry.py"

TOUCHED = (PIPELINE, GUARD)

#: 那个唯一定义点里的短路 —— **本批修的就是它**。
SHORT_CIRCUIT = """    ).fetchone()
    if inserted is None:
        return None"""

#: B 的变异：**真的多开一个写这对 INSERT 的函数**。
#:
#: ⚠️ 第一版是「在 `_file_a_manual_task` 里把那条 INSERT 抄两遍」，而它 **GREEN**：
#: `test_only_one_method_writes_the_pair` 判的是「哪几个**方法**写它」，
#: 同一个方法里抄两遍，writers 仍然只有一个。**探针把这条判据的边界量了出来** ——
#: 那个变异抓不到的东西，由按语句计数的 `ENGINE_INSERT_SITES` 抓到（见它的连带）。
#: 两条断言因此是互补的，不是重复的。
SECOND_DEFINITION_POINT = '''def _file_a_manual_task_somewhere_else(
    connection: Any,
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    manual_task_id: UUID,
    classification: Classification,
    reason_code: str,
    evidence: Mapping[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO manual_tasks(
            tenant_id, id, conversation_id, intent_id,
            task_class, notify, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            manual_task_id,
            conversation_id,
            manual_task_id,
            classification.audience_class,
            classification.cadence,
            reason_code,
            Jsonb(dict(evidence)),
        ),
    )


'''

#: B 变异插在它前面的那一行（注释块的第一行，全文件唯一）。
SECOND_POINT_ANCHOR = (
    "#: **模块级函数，不是 `InboxProcessor` 的方法**：调它的五条路分属两个类\n"
)

#: 投递那一处传的 `evidence=` —— 拿它来演「某个调用点漏传一个参数」。
#: ⚠️ 十四个参数一个默认值都没有，所以漏传是 `TypeError`；这支探针要看的是
#: **那个 TypeError 到底有没有被任何一条用例接住**。
DELIVERY_EVIDENCE = (
    '            evidence={"outbox_id": str(outbox_id), "intent_id": str(intent_id)},\n'
)

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把短路删掉（`inserted is None` 不再返回）—— 本批修的那个缺陷复原",
            [
                Mutation(
                    PIPELINE,
                    SHORT_CIRCUIT,
                    """    ).fetchone()
    if False:
        return None""",
                )
            ],
            # ⚠️ **裸名字**：harness 把 `[...]` 截掉了（`suite_runner` 里
            # `.split("[")[0]`），四个参数化用例在它眼里是同一个名字。
            ["test_a_replayed_turn_files_at_most_one_task"],
            # **实测出来的连带，不是想出来的**：
            # `test_unsendable_intent_becomes_a_task.py` 里早就有一条
            # `..._collides_on_the_key_writes_no_second_task`，
            # 而它只盖 `_file_task_for_unsendable_intents` 那一条路。
            # **这条连带本身就是本批的论据**：有守卫的那条路当时是对的，
            # 没有守卫的那条（T365 那处）就漂了。
            ["test_a_second_turn_that_collides_on_the_key_writes_no_second_task"],
        ),
        (
            "B · 真的多开一个写这对 INSERT 的函数（又一个定义点）",
            [
                Mutation(
                    PIPELINE,
                    SECOND_POINT_ANCHOR,
                    SECOND_DEFINITION_POINT + SECOND_POINT_ANCHOR,
                )
            ],
            ["test_only_one_method_writes_the_pair"],
            # 按语句计数的那一条也红 —— 两条断言互补：
            # 一条数「几个方法」，一条数「几条语句」。
            ["test_the_scanner_found_both_sides"],
        ),
        (
            "C · 某个调用点漏传一个参数（投递那一处的 `evidence=`）",
            [Mutation(PIPELINE, DELIVERY_EVIDENCE, "")],
            [
                "test_a_replayed_turn_files_at_most_one_task",
                "test_every_call_site_passes_every_keyword",
            ],
            # 实测：没有连带。漏传就是 `TypeError`，只炸在跑到它的那条路上。
            (),
        ),
        (
            "D · 把 `_persist_no_send` 的推送条件改回「那个 id 不是 None」",
            [
                Mutation(
                    PIPELINE,
                    "if filed_manual_task is not None and classification.pushes_now:",
                    "if manual_task_id is not None and classification.pushes_now:",
                )
            ],
            ["test_the_fifth_path_is_covered_by_the_shared_definition_point"],
            # 实测：没有连带。
            (),
        ),
        (
            "E · 把 `ENGINE_INSERT_SITES` 改回一个会跟着涨的计数",
            [Mutation(GUARD, "ENGINE_INSERT_SITES = 1", "ENGINE_INSERT_SITES = 2")],
            [],
            ["test_the_scanner_found_both_sides"],
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
            names = _failed_names(result.output)
            hit = set(names) == set(expects)
            control_names = _failed_names(result.control_output or "")
            collateral_hit = set(control_names) == set(collateral)
            verdicts.append((label, result.went_red, hit, collateral_hit))
            print(result)
            if not hit:
                extra = sorted(set(names) - set(expects))
                short = sorted(set(expects) - set(names))
                print(f"    ⚠️ 本文件：多红 {extra}；没红 {short}")
            if not collateral_hit:
                extra = sorted(set(control_names) - set(collateral))
                short = sorted(set(collateral) - set(control_names))
                print(f"    ⚠️ 连带：多红 {extra}；没红 {short}")
            print()

    print("=" * 72)
    bad = [
        label
        for label, went_red, hit, collateral_hit in verdicts
        if not (hit and collateral_hit)
    ]
    for label, went_red, hit, collateral_hit in verdicts:
        if hit and collateral_hit:
            mark = "RED ✅" if went_red else "GREEN ✅（声明如此）"
        else:
            mark = "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
