#!/usr/bin/env python
"""反向探针：运维缺陷 19 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **H1** | record 恢复「计划点名了才记」的早退（= 修复前） | 记账那几条 |
| **H2** | 不判正文，发了就记 | 反侧那条 |
| **H3** | question_text 截头不截尾 | 截尾那条 |
| **H4** | settle 的 null 支不看 event_type | timer 那条 |
| **H5** | settle 的 null 支整个拿掉 | 客户回话那条 |
| **H6** | 计划步那条规则拿掉 | 提示词那条 |
| **H7** | 生成步那条规则拿掉 | 提示词那条 |
| **H8** | 全空守卫恒放行 | 全空那条 |
| **H9** | 死锁守卫恒放行 | 死锁那条 |
| **H10** | demo 包撤掉声明（连 hash 一起改） | 在架声明那条 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_the_platform_remembers_asking.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
⚠️ **配包变异必须连 `content_hash` 一起改** —— 缺陷 18 那支实测过不改的代价
（91 failed / 11 errors 的无差别连带，一个红成那样的探针证明不了任何一条）。
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
TARGET = "tests/contracts/test_the_platform_remembers_asking.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 14
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2516
CONTROL_BASELINE_SKIPPED = 53

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (PIPELINE, MODEL, REGISTRY, PROFILE)

READS_TEXT = '''    content = ((asked.proposal or {}).get("typed_payload") or {}).get("content")
    text = customer_text(content, materials=materials) or ""
    if "？" not in text and "?" not in text:
        return
    requested = [str(slot) for slot in plan.document["required_slot_requests"]]'''
#: H1 = 修复前：计划没点名就早退（正文判据整个不跑）。
CLAIM_GATED = '''    requested = [str(slot) for slot in plan.document["required_slot_requests"]]
    if not requested:
        return
    content = ((asked.proposal or {}).get("typed_payload") or {}).get("content")
    text = customer_text(content, materials=materials) or ""'''
#: H2 = 反向的坏实现：发了就记，不看正文。
SENT_MEANS_ASKED = (
    '    content = ((asked.proposal or {}).get("typed_payload") or {})'
    '.get("content")\n'
    '    text = customer_text(content, materials=materials) or ""\n'
    '    requested = [str(slot) for slot in plan.document["required_slot_requests"]]'
)

TAIL = '        "question_text": text[-200:],'
HEAD = '        "question_text": text[:200],'

CHECKS_EVENT = '''        if str(prepared.envelope.get("event_type")) != "customer_message":
            return
        pending["answer_evidence"] = str(event_id)'''
ANY_EVENT = '''        if False:
            return
        pending["answer_evidence"] = str(event_id)'''

NULL_BRANCH = '''    if pending.get("slot") is None:'''
NO_NULL_BRANCH = '''    if False and pending.get("slot") is None:'''

PLAN_RULE = '''- pending_question, when it is not null and its answer_evidence is null, is a
  question we already sent (question_text) that the customer has not answered.
  He heard it: do not plan a turn whose point is asking it again. Wait, or
  move forward another way.
'''
GENERATE_RULE = '''- pending_question, when it is not null and its answer_evidence is null, is a
  question we already sent (question_text) that the customer has not answered.
  Never write it again, in these words or others -- he heard it. When he
  answered it this turn, use the answer; asking a question he just answered
  reads as not listening.
'''

EMPTY_GUARD = '''    if strategies and not any(declared.values()):
        raise ProfilePackageError('''
EMPTY_GUARD_OFF = '''    if False:
        raise ProfilePackageError('''

DEADLOCK_RAISES = '''    if offences:
        raise ProfilePackageError(
            f"profile package {path} declares required_slots a frame cannot "'''
DEADLOCK_OFF = '''    if False:
        raise ProfilePackageError(
            f"profile package {path} declares required_slots a frame cannot "'''

DECLARED = '"required_slots": [\n        "current_handling"\n      ],'
MISDECLARED = '"required_slots": [\n        "usage_intent"\n      ],'
#: 配包变异的 stamp 在 `main()` 里**现算**（`canonical_hash(合并素材后的文档)`），
#: 不写死在这里 —— 写死的号会在 demo 包下一次抬版本时静默过期。

FILLS = "test_a_question_in_the_sent_text_is_recorded_without_any_slot_naming"
NOT_A_QUESTION = "test_a_statement_is_not_recorded_as_a_question"
MATERIAL = "test_a_question_inside_a_quoted_material_counts"
ANNOTATION = "test_a_named_slot_rides_along_as_an_annotation"
TAIL_TEST = "test_the_question_text_keeps_the_tail_not_the_head"
NO_ROW = "test_a_turn_with_no_outbox_row_records_nothing"
SETTLES = "test_a_customer_reply_settles_a_slotless_question"
TIMER = "test_a_timer_turn_does_not_settle_it"
SLOT_WAITS = "test_a_slot_bound_question_still_waits_for_the_slot"
PROMPTS = "test_both_prompts_now_explain_the_key"
ALL_EMPTY = "test_a_package_where_no_strategy_declares_anything_is_refused"
ONE_ENOUGH = "test_one_declaration_is_enough"
DEADLOCK = "test_a_frame_that_allows_the_strategy_but_lacks_the_slot_is_refused"
SHIPPED = "test_the_shipped_packages_now_declare_what_their_goals_ask_for"

#: 改了平台提示词 / demo 内容之后连带红的那几条 —— **量出来的，预测写的是空**。
FLOOR = "test_the_floor_equals_the_request_it_claims_to_measure"
OVERHEAD = "test_the_overhead_constants_are_measured_from_the_real_payload"
SPELLED_ONCE = "test_the_platform_prompt_sizes_are_spelled_once_per_package"
PLAN_FLOOR = "test_what_the_field_costs_per_plan_turn_is_measured_not_estimated"
BUDGET_IDENTITY = "test_the_empty_request_baseline_is_measured_from_the_real_payload"
AT_BUDGET = "test_a_turn_at_the_material_budget_still_fits_the_limit"
STAMP_TEST = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_STAMP = "test_the_runbook_quotes_the_stamp_in_the_package"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    import json
    import subprocess

    # **变异后的 demo hash 现算**，不手抄。
    compute = subprocess.run(
        [str(PYTHON), "-c", (
            "import sys,json;"
            "sys.path[:0]=['services/api/src','services/worker/src','packages/runtime/src'];"
            "from fastagent_v2.profiles.registry import _merge_material_texts, canonical_hash;"
            "import pathlib;"
            "t=pathlib.Path('profiles/internal-demo-sales/profile.json').read_text(encoding='utf-8');"
            f"t=t.replace({DECLARED!r}, {MISDECLARED!r});"
            "d=json.loads(t); d['identity'].pop('content_hash');"
            "_merge_material_texts(d, pathlib.Path('profiles/internal-demo-sales/profile.json'));"
            "print(canonical_hash(d))"
        )],
        capture_output=True, cwd=ROOT, check=True,
    )
    mutated_stamp = compute.stdout.decode().strip()
    current_stamp = json.loads(PROFILE.read_text(encoding="utf-8"))["identity"][
        "content_hash"
    ]

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "H1 · record 恢复「计划点名了才记」（= 修复前）—— **本批兑现物**",
            [Mutation(PIPELINE, READS_TEXT, CLAIM_GATED)],
            [FILLS, MATERIAL, TAIL_TEST],
            [],
        ),
        (
            "H2 · 不判正文，发了就记（反侧）",
            [Mutation(PIPELINE, READS_TEXT, SENT_MEANS_ASKED)],
            [NOT_A_QUESTION],
            [],
        ),
        (
            "H3 · question_text 截头不截尾",
            [Mutation(PIPELINE, TAIL, HEAD)],
            [TAIL_TEST],
            [],
        ),
        (
            "H4 · settle 的 null 支不看 event_type",
            [Mutation(PIPELINE, CHECKS_EVENT, ANY_EVENT)],
            [TIMER],
            [],
        ),
        (
            "H5 · settle 的 null 支整个拿掉",
            [Mutation(PIPELINE, NULL_BRANCH, NO_NULL_BRANCH)],
            #: ⚠️ `TIMER` 也红，量出来的：支拿掉后 `slot=None` 落进有槽逻辑，
            #: `str(None)` 成了槽名 `"None"`，帧上没有 `slots` 键当场 KeyError ——
            #: 两条 null 支用例一起炸。这正说明该支不是锦上添花，是必经的岔口。
            [SETTLES, TIMER],
            [],
        ),
        (
            "H6 · 计划步那条规则拿掉",
            [Mutation(MODEL, PLAN_RULE, "")],
            [PROMPTS],
            #: 改了计划步 system prompt，四条**现量现比**字节表的用例就红 ——
            #: 那正是它们的职责，与缺陷 18 那支 F1-F3 的连带同一个道理。
            [FLOOR, OVERHEAD, SPELLED_ONCE, PLAN_FLOOR],
        ),
        (
            "H7 · 生成步那条规则拿掉",
            [Mutation(MODEL, GENERATE_RULE, "")],
            [PROMPTS],
            #: 生成步那侧多两条：素材预算恒等式与「顶格仍然装得下」——
            #: 生成步的 system prompt 正是那两条式子的加数。
            [FLOOR, OVERHEAD, SPELLED_ONCE, BUDGET_IDENTITY, AT_BUDGET],
        ),
        (
            "H8 · 全空守卫恒放行",
            [Mutation(REGISTRY, EMPTY_GUARD, EMPTY_GUARD_OFF)],
            [ALL_EMPTY],
            [],
        ),
        (
            "H9 · 死锁守卫恒放行",
            [Mutation(REGISTRY, DEADLOCK_RAISES, DEADLOCK_OFF)],
            [DEADLOCK],
            [],
        ),
        (
            "H10 · demo 包把声明改指到别的槽（连 hash 一起改）",
            #: ⚠️ **不是撤掉** —— demo 只有这一条声明，撤掉会让全空守卫在
            #: 真包上开火，整包封不出，实测 102 failed / 11 errors 的无差别
            #: 连带（守卫工作了，而那样的红证不了「这条声明是承重的」）。
            #: 改指到 `usage_intent`（introduce 里真实存在的另一个槽）：
            #: 两道守卫都不触发，只有钉着声明内容的那条红。
            [
                Mutation(PROFILE, DECLARED, MISDECLARED),
                Mutation(PROFILE, current_stamp, mutated_stamp),
            ],
            [SHIPPED],
            #: 改了包内容 hash 就变，钉着戳的那两条红 —— 职责使然（量出来的）。
            [STAMP_TEST, RUNBOOK_STAMP],
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
