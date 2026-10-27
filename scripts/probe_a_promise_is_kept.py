#!/usr/bin/env python
"""反向探针：许下的，这一轮就要兑现（运维 §10.4 判据 2）。

    python scripts/probe_a_promise_is_kept.py

| | 变异 | 演的是 |
|---|---|---|
| A | 整条闸关掉 | **这一批之前的样子**：一句「我先把 X 讲清楚」照发 |
| B | 判据放宽成「后面还有一段字就算兑现」 | **我差点采用的那条判据** |
| C | 「发了文件」不算兑现 | 过严：发了附件还判它空头支票 |
| D | 丢一段改成整份拒 | 过严：客户整轮收不到话 |
| E | 字段名改回 `evidence_refs` | **我第一版就是它**：那条析取项从写下来就是死的 |
| F | 动词短语表少一项（「安排」） | 尺子缺一个口，§2.2 那一轮漏过去 |
| G | **丢的单位改回「段」** | **复核 2026-09-18 退回第一条**：生产一段多句，于是丢一段 = 整份拒 |
| H | **尺子不再挡分句符** | **复核退回第二条**：「我明白，您方便说一下……」被读成许诺 |
| I | 「安排」的名词前瞻去掉 | 退回第二条的另一半：「我看了您的安排」被读成许诺 |
| J | 句界集合缩回只有全角一族 | 集合悄悄变小 —— **上一轮的参数化用例看不见这种改法** |
| K | 拆句把标点吃掉 | 客户读到「明白您自己接」：留下来的字拼不回原样 |
| L | 按句拆多出第二个调用点 | 「只有这一处」那条守卫**自己数错**时看起来仍然在守 |

## G…K 是重交这一轮加的，**而它们要证明的正是复核那两条**

A…F 那六支在退回之前全是红的（实测 25 条基线），**而两条缺陷一条都没被它们碰到**：
六支里没有一支动「丢的单位」，也没有一支动「字符类里有哪些字符」。
**探针全红不等于这一批没洞** —— 洞在没有人去变异的那两个维度上。

## A 够不着「发了文件」那条用例，而那是对的

`test_sending_a_file_counts_as_delivering` 断言的是「**这一段不该被丢**」，
所以闸整条关掉时它照样绿 —— **一条断言「什么都没发生」的用例，
永远探不到「闸没开」**。守住 C 那个方向的是 C 自己。

## 两条边

「许了要兑现」有两种错法：**放过了**（A / B / F）与**误伤了**（C / D / E）。
只有 A 的探针，一个「凡是第一人称的句子一律丢掉」的实现能拿满分。

## B 是这一格的分量所在

「后面还有一段字」是最自然的判据，**而运维 §2.2 那一轮正好过得去**：
「收到，报价单这边我来安排。您方便说一下……吗」—— 许诺 + 一个问题，
什么都没出去。B 把判据换成那一条，看本文件红不红。
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
TARGET = "tests/contracts/test_a_promise_is_kept_in_the_same_turn.py"

#: 实测 2026-09-19（批 F 重交），随门禁回填。
#: **25 → 56 是复核退回那两条的用例代价**（句界与分句符各按族参数化、
#: 丢一句 vs 丢一段两边各钉、拆句无损、半角句界另写一条写死的）。
BASELINE_PASSED = 57
CONTROL_BASELINE_PASSED = 2029

VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
TOUCHED = (VALIDATION,)

#: A —— 整条闸关掉（这一批之前的样子）。
GATE_ON = """    if _this_turn_delivers(generation):
        return

    lists = _customer_visible_segment_lists(generation)"""
GATE_OFF = """    if True:
        return

    lists = _customer_visible_segment_lists(generation)"""

#: B —— 判据放宽成「后面还有一段字就算兑现」。
DELIVERY_HEAD = """    for _, segments in _customer_visible_segment_lists(generation):
        if any(
            isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
            for segment in segments
        ):
            return True"""
#: ⚠️ **重交这一轮重瞄过它。** 先前它是 `len(segments) > 1`（「还有别的**段**」）——
#: 而夹具改成生产的形状（一段）之后，那个变异在这两条实测串上**根本不触发**，
#: 于是 B 变成了一支只红在对照用例上的探针。
#: 生产是单段多句，所以那条「最自然的错判据」在今天的形状下是
#: 「**还有别的句**就算兑现」。
DELIVERY_ANY_SEGMENT = """    if any(
        len(_sentences(str(segment.get("content_or_ref") or ""))) > 1
        for _, segments in _customer_visible_segment_lists(generation)
        for segment in segments
        if isinstance(segment, dict)
    ):
        return True
    for _, segments in _customer_visible_segment_lists(generation):
        if any(
            isinstance(segment, dict)
            and segment.get("type") == MATERIAL_REF_SEGMENT_TYPE
            for segment in segments
        ):
            return True"""

#: C —— 「发了文件」不算兑现。
SEND_MATERIAL_COUNTS = """    if any(
        str(intent.get("kind")) == "send_material"
        for intent in generation["proposed_intents"]
    ):
        return True"""
SEND_MATERIAL_IGNORED = """    if False:
        return True"""

#: D —— 丢一段改成整份拒。
DROP_THE_SEGMENT = """            remainder = "".join(survivors).strip()
            if not remainder:"""
REFUSE_THE_TURN = """            remainder = "".join(survivors).strip()
            if True:
                raise GenerationRejected(UNKEPT_PROMISE_REASON_CODE, "strict")
            if not remainder:"""

#: E —— 字段名改回 `evidence_refs`（**我第一版就是它**）。
REAL_FIELD = (
    '    return any(claim.get("source_evidence_refs") for claim in generation["claims"])'
)
DEAD_FIELD = """    return any(claim.get("evidence_refs") for claim in generation["claims"])"""

#: F —— 动词短语表少一项。
WITH_ARRANGE = """|发您|发给您|发过去|发一份|(?<![的个些这那])安排|整理好|准备好)"""
WITHOUT_ARRANGE = """|发您|发给您|发过去|发一份|整理好|准备好)"""

#: G —— **丢的单位改回「段」**（复核 2026-09-18 退回第一条的那个形态）。
#:
#: 一段里只要有一句命中，整段就没了 —— 而生产的一段是多句的，
#: 纯文本回合又常常只有一段。
BY_SENTENCE = (
    """            for sentence in _sentences(str(segment.get("content_or_ref") or "")):"""
)
BY_SEGMENT = (
    """            for sentence in [str(segment.get("content_or_ref") or "")]:"""
)

#: L —— 按句拆多出第二个调用点。
#:
#: **行为中性的变异**，所以只该红那一条守卫：
#: 「按句拆今天只有这一处」这句话不写成 grep（会自指），写成一条用例 ——
#: 而一条数东西的用例最容易数错，所以它自己也要被探一次。
ONE_CALL_SITE = BY_SENTENCE
A_SECOND_CALL_SITE = (
    """            _ = _sentences("")\n"""
    + BY_SENTENCE
)

#: H —— **尺子不再挡分句符**（退回第二条的那个形态）。
RULER_STOPS_AT_CLAUSES = """    "我(?!们)[^"
    + SENTENCE_BOUNDARY_CHARS
    + _CLAUSE_BOUNDARY_CHARS
    + "]{0,20}?\""""
RULER_CROSSES_CLAUSES = """    "我(?!们)[^"
    + SENTENCE_BOUNDARY_CHARS
    + "]{0,20}?\""""

#: I —— 「安排」的名词前瞻去掉。
ARRANGE_IS_A_VERB_ONLY = """|(?<![的个些这那])安排|"""
ARRANGE_ANYWHERE = """|安排|"""

#: J —— 句界集合缩回只有全角一族。
#:
#: **上一轮那条参数化用例看不见这种改法**（参数表就取自这个常数），
#: 所以本批另写了一条用写死串钉半角的。这支探针验的就是那一条。
BOUNDARIES_WITH_HALF_WIDTH = '''SENTENCE_BOUNDARY_CHARS = "。！？；!?;\\n"'''
BOUNDARIES_FULL_WIDTH_ONLY = '''SENTENCE_BOUNDARY_CHARS = "。！？；\\n"'''

#: K —— 拆句把标点吃掉。
SPLIT_KEEPS_THE_STOP = """_SENTENCE = re.compile(
    f"[^{SENTENCE_BOUNDARY_CHARS}]*[{SENTENCE_BOUNDARY_CHARS}]"
    f"|[^{SENTENCE_BOUNDARY_CHARS}]+"
)"""
SPLIT_EATS_THE_STOP = """_SENTENCE = re.compile(
    f"[^{SENTENCE_BOUNDARY_CHARS}]+"
)"""


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 整条闸关掉",
            [Mutation(VALIDATION, GATE_ON, GATE_OFF)],
            [
                "test_a_claim_without_evidence_does_not_count",
                "test_a_list_separator_used_as_a_clause_separator_still_matches",
                "test_a_message_that_is_nothing_but_a_promise_is_refused",
                "test_a_promise_followed_by_a_question_is_still_unkept",
                "test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped",
                "test_the_same_paragraph_split_into_two_segments_behaves_the_same",
                "test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise",
                "test_two_promises_in_one_paragraph_are_both_recorded",
            ],
            [],
        ),
        (
            "B · 「后面还有别的句」就算兑现",
            [Mutation(VALIDATION, DELIVERY_HEAD, DELIVERY_ANY_SEGMENT)],
            # ⚠️ **单句的那几条用例这一支够不着，而那是对的**：
            # 「还有别的句」这条错判据在一条只有一句的消息上不触发。
            # 守那个方向的是 A 与 D。
            #
            # ⚠️ 它**连带**红掉「只有一处按句拆」那条守卫 —— 这个变异为了
            # 数句子在 `_this_turn_delivers` 里加了第二个调用点。
            # 声称出来：那条守卫对**任何**新调用点敏感，不只对 L 那一种。
            [
                "test_a_promise_followed_by_a_question_is_still_unkept",
                "test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped",
                "test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise",
                "test_this_is_still_the_only_place_that_splits_by_sentence",
                "test_two_promises_in_one_paragraph_are_both_recorded",
            ],
            [],
        ),
        (
            "C · 发了文件不算兑现",
            [Mutation(VALIDATION, SEND_MATERIAL_COUNTS, SEND_MATERIAL_IGNORED)],
            ["test_sending_a_file_counts_as_delivering"],
            [],
        ),
        (
            "D · 丢一句改成整份拒",
            [Mutation(VALIDATION, DROP_THE_SEGMENT, REFUSE_THE_TURN)],
            [
                "test_a_claim_without_evidence_does_not_count",
                "test_a_message_that_is_nothing_but_a_promise_is_refused",
                "test_a_promise_followed_by_a_question_is_still_unkept",
                "test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped",
                "test_the_same_paragraph_split_into_two_segments_behaves_the_same",
                "test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise",
                "test_two_promises_in_one_paragraph_are_both_recorded",
            ],
            [],
        ),
        (
            "E · 字段名改回 evidence_refs",
            [Mutation(VALIDATION, REAL_FIELD, DEAD_FIELD)],
            ["test_an_evidenced_claim_counts_as_delivering"],
            [],
        ),
        (
            "F · 动词短语表少「安排」",
            [Mutation(VALIDATION, WITH_ARRANGE, WITHOUT_ARRANGE)],
            [
                "test_a_promise_followed_by_a_question_is_still_unkept",
                "test_the_lookbehind_is_only_on_arrange",
                "test_these_are_promises",
            ],
            [],
        ),
        (
            "G · 丢的单位改回「段」（复核退回第一条）",
            [Mutation(VALIDATION, BY_SENTENCE, BY_SEGMENT)],
            # ⚠️ 这一支**连带**红掉那条「只有一处」的守卫：变异把唯一那个
            # 调用点换成了一个列表字面量，于是调用点数从 1 掉到 0。
            # 声称出来而不是当成噪声 —— 它正说明那条守卫在数真东西。
            [
                "test_a_promise_followed_by_a_question_is_still_unkept",
                "test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped",
                "test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise",
                "test_this_is_still_the_only_place_that_splits_by_sentence",
                "test_two_promises_in_one_paragraph_are_both_recorded",
            ],
            [],
        ),
        (
            "L · 按句拆多出第二个调用点",
            [Mutation(VALIDATION, ONE_CALL_SITE, A_SECOND_CALL_SITE)],
            ["test_this_is_still_the_only_place_that_splits_by_sentence"],
            [],
        ),
        (
            "H · 尺子不再挡分句符（复核退回第二条）",
            [Mutation(VALIDATION, RULER_STOPS_AT_CLAUSES, RULER_CROSSES_CLAUSES)],
            ["test_the_ruler_stops_at_a_clause_boundary_too"],
            [],
        ),
        (
            "I · 「安排」的名词前瞻去掉",
            [Mutation(VALIDATION, ARRANGE_IS_A_VERB_ONLY, ARRANGE_ANYWHERE)],
            ["test_the_verb_and_the_noun_called_arrange_are_told_apart"],
            [],
        ),
        (
            "J · 句界集合缩回只有全角一族",
            [
                Mutation(
                    VALIDATION, BOUNDARIES_WITH_HALF_WIDTH, BOUNDARIES_FULL_WIDTH_ONLY
                )
            ],
            ["test_the_half_width_terminators_are_in_the_boundary_set"],
            [],
        ),
        (
            "K · 拆句把标点吃掉",
            [Mutation(VALIDATION, SPLIT_KEEPS_THE_STOP, SPLIT_EATS_THE_STOP)],
            [
                "test_splitting_a_paragraph_loses_nothing",
                "test_the_half_width_terminators_are_in_the_boundary_set",
                "test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped",
                "test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise",
                "test_two_promises_in_one_paragraph_are_both_recorded",
            ],
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
