#!/usr/bin/env python
"""反向探针：许了没兑现先重来一次（T367，用户 2026-09-19 裁定）。

    python scripts/probe_unkept_promise_retry.py

| | 变异 | 演的是 |
|---|---|---|
| A | 重跑整条关掉 | **本批之前的样子**：许了没兑现直接丢 |
| B | 反馈不送整句，只送动词短语 | 「讲清楚」三个字说不出「试用怎么开」，模型补不出来 |
| C | 栈操作不钉 `keep` | **迁移两次** —— 栈被推/弹两遍 |
| D | 钉 `keep` 挪到校验之后 | 校验器判的不是真正会执行的那一份 |
| E | 重跑失败时走回落计划 | 把一条发得出去的回复换成一条保守的 |
| F | 重跑再检索一遍 | 再花一次钱，还多写一行 `retrieval_evidence` |
| G | 留痕里去掉「有没有用」 | 这条路值不值就量不出来了 |
| H | 整句抄进 `GenerationRejected` 的 `context` | **客户可见的字进审计面** |
| I | `sentence` 取循环变量（本批真犯过的那个） | 记下的是那段**最后**一句，不是命中的那一句 |

## 两条边

**放过了**：A / B / G —— 不重来、反馈没用、不记账。
**误伤了**：C（迁移两次）· D · E（把回复换成保守计划）· F（白花钱）· H（漏正文）。
只有一边的探针，一个「凡是命中就整轮重来两次」的实现能拿满分。

## I 是这一批自己犯过的那个缺陷

第一版只收动词短语、在下一个循环里读 `sentence` —— 而那时它已经是这一段
**最后**一句。`test_the_dropped_item_carries_the_sentence_for_the_retry` 当场红。
**一个循环变量在它的循环之外就不再是它了。**

## ⚠️ 本批有一半探不到，写出来

「重跑真的跑起来了、而且第二次的结果替换了第一次」这件事**要真库**
（要三个模型调用、一个事务、一条审计行），而探针只跑 `tests/contracts`。
本文件钉的是**形状与接线**；行为那一半的证据是门禁里的 e2e / integration。
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
TARGET = "tests/contracts/test_an_unkept_promise_gets_one_more_try.py"

#: 实测 2026-09-19，随门禁回填。
BASELINE_PASSED = 13
CONTROL_BASELINE_PASSED = 2118

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
TOUCHED = (PIPELINE, VALIDATION, SHAPE, MODEL)

#: A —— 重跑整条关掉。
RETRY_ON = """        if unkept_promises and not plan.is_silent:"""
RETRY_OFF = """        if False and unkept_promises and not plan.is_silent:"""

#: B —— 反馈只送动词短语。
SENDS_THE_SENTENCE = """                sentence = str(item.get("sentence") or "")"""
SENDS_THE_PHRASE = """                sentence = str(item.get("promised") or "")"""

#: C —— 栈操作不钉 `keep`。
PINS_KEEP = """        pinned = {
            **proposed,
            "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
        }"""
PINS_NOTHING = """        pinned = {**proposed}"""

#: D —— 钉 `keep` 挪到校验之后（形状上仍在，而它已经不管用了）。
PIN_THEN_VALIDATE = """        plan, rejection = plan_or_fallback(
            plan=pinned,"""
VALIDATE_THEN_PIN = """        plan, rejection = plan_or_fallback(
            plan=proposed,"""

#: E —— 重跑那份计划被拒时走回落计划。
#:
#: ⚠️ **锚点带上后面那一行**：`if rejection is not None:` 在 `pipeline.py` 里有两处
#: （第一次尝试那一处、重跑这一处），只写它会 `anchor matched 2 place(s)`。
ABANDON_ON_REJECTION = (
    "        if rejection is not None:\n            # 重跑那份计划不合法"
)
FALL_BACK_INSTEAD = (
    "        if False and rejection is not None:\n            # 重跑那份计划不合法"
)

#: F —— 重跑再检索一遍。
REUSES_RETRIEVAL = """                knowledge=knowledge,
                assets=assets,
                context=context,
                dropped=dropped,"""
RETRIEVES_AGAIN = """                knowledge=dict(
                    (self._retrieve_if_requested(plan=plan, context=context) or {})
                    and knowledge
                ),
                assets=assets,
                context=context,
                dropped=dropped,"""

#: G —— 留痕里去掉「有没有用」。
RECORDS_WHETHER_IT_HELPED = (
    '                            "kept_after_retry": bool(kept_after_retry),'
)
RECORDS_NOTHING_USEFUL = '                            "attempted": True,'

#: H —— 整句抄进 `context`。
PROJECTS_THREE_KEYS = """                "dropped_segments": [
                    {"where": item["where"], "why": item["why"], "promised": item["promised"]}
                    for item in dropped
                ],"""
COPIES_THE_WHOLE_ITEM = """                "dropped_segments": [dict(item) for item in dropped],"""

#: I —— `sentence` 取循环变量（本批真犯过的那个）。
PAIRS_THE_SENTENCE = """                promises.append((match.group(1), sentence))"""
LOSES_THE_SENTENCE = """                promises.append((match.group(1), "占位"))"""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 重跑整条关掉",
            [Mutation(PIPELINE, RETRY_ON, RETRY_OFF)],
            # 两条：那个 `if` 的字面（沉默打法那条钉着它）与「只跑一次」那条 ——
            # 后者按那个 `if` 之后的正文找 `while`，而 `if False and …` 改了它的字面。
            # 三条：那个 `if` 的字面（沉默打法那条）、「只跑一次」那条、
            # 以及「送的是整句」那条 —— 后两条都按那个 `if` 之后的正文定位，
            # 而 `if False and …` 改了它的字面，于是它们找不到那一段。
            # **声称出来，因为「定位不到就绿」才是危险的那一种** ——
            # 它们现在是红的，说明定位是按字面走的、而不是按碰巧存在的东西走的。
            [
                "test_a_silent_strategy_is_not_retried",
                "test_the_pipeline_sends_the_sentence_not_the_verb_phrase",
                "test_the_retry_only_runs_once",
            ],
            [],
        ),
        (
            "B · 反馈只送动词短语",
            [Mutation(PIPELINE, SENDS_THE_SENTENCE, SENDS_THE_PHRASE)],
            # ⚠️ **第一版这里声称 `[]`，而那不是「它探不到」，是我的用例有洞** ——
            # 那时整批全绿。补了 `..._sends_the_sentence_not_the_verb_phrase` 之后
            # 它才有东西可红。
            ["test_the_pipeline_sends_the_sentence_not_the_verb_phrase"],
            [],
        ),
        (
            "C · 栈操作不钉 keep",
            [Mutation(PIPELINE, PINS_KEEP, PINS_NOTHING)],
            ["test_the_retry_pins_the_stack_operation_to_keep"],
            [],
        ),
        (
            # ⚠️ **这一支第一版报 GREEN，而那是用例的洞、不是变异的错**：
            # 那条用例只看「赋值在校验之前」，看不见那一份**有没有被用上**。
            # 用例因此多了一条 `plan=pinned,` 的断言。
            "D · 钉 keep 算出来了却没用上",
            [Mutation(PIPELINE, PIN_THEN_VALIDATE, VALIDATE_THEN_PIN)],
            ["test_the_retry_pins_the_stack_operation_to_keep"],
            [],
        ),
        (
            "E · 重跑被拒时走回落计划",
            [Mutation(PIPELINE, ABANDON_ON_REJECTION, FALL_BACK_INSTEAD)],
            ["test_a_failed_retry_keeps_the_first_attempt"],
            [],
        ),
        (
            "F · 重跑再检索一遍",
            [Mutation(PIPELINE, REUSES_RETRIEVAL, RETRIEVES_AGAIN)],
            ["test_the_retry_reuses_the_first_retrieval"],
            [],
        ),
        (
            "G · 留痕里去掉「有没有用」",
            [Mutation(PIPELINE, RECORDS_WHETHER_IT_HELPED, RECORDS_NOTHING_USEFUL)],
            ["test_someone_records_whether_the_retry_helped"],
            [],
        ),
        (
            "H · 整句抄进 context",
            [Mutation(VALIDATION, PROJECTS_THREE_KEYS, COPIES_THE_WHOLE_ITEM)],
            ["test_the_refusal_context_still_carries_no_customer_text"],
            # **仓里本来就有的那一条**：批 F 那条「正文进了审计上下文」。
            ["test_a_message_that_is_nothing_but_a_promise_is_refused"],
        ),
        (
            "I · sentence 取循环变量（本批真犯过的）",
            [Mutation(VALIDATION, PAIRS_THE_SENTENCE, LOSES_THE_SENTENCE)],
            ["test_the_dropped_item_carries_the_sentence_for_the_retry"],
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
            went_red = result.went_red or bool(result.collateral)
            verdicts.append((label, went_red, hit, elsewhere))
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
        elif not elsewhere:
            mark = "RED ⚠️ 连带变了"
        elif not hit:
            mark = "RED ⚠️ 红错了"
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
