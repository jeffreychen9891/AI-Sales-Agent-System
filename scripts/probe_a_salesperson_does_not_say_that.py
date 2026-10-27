#!/usr/bin/env python
"""反向探针：`tests/contracts/test_a_salesperson_does_not_say_that.py`。

本批全部改在配包里，所以每一支变异都改 `profile.json` —— 而那正是它该被改的地方：
**一条只写在 `goal` 散文里的规则，删掉它没有任何东西会红**（本仓 T384 那一族）。
这七条判据要证明的就是「本批加的这几句不是散文」。

* A / B · 两个点名的 ref 各拿掉一个；
* C · 点名一个**包里没有**的 ref（本仓有过 `materials/03` 的先例）；
* D · 把开场那段素材改成 `on_request`（点了名却推不出去）；
* E · 「不要重复对方的话」整条删掉；
* F · **把「铺垫、共情、点评可以有」删掉** —— 规则会变得比用户裁的更宽；
* G / H · 禁令里删掉「我收到了」「我知道了」/「看懂了不用说出来」；
* I / J · 「跟着他的关注点走」删掉 / 疑虑改回「最后才碰」；
* K / L · 新槽删掉一个 / 改成 `required: true`；
* M · 删掉「填了就不要再问第二遍」。

⚠️ **带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去本文件」，
把连带集合**量出来**而不是想出来。**配包是很多用例的共同夹具，连带不会是空集。**

    python scripts/probe_a_salesperson_does_not_say_that.py
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
TARGET = "tests/contracts/test_a_salesperson_does_not_say_that.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 9

#: `tests/contracts` 减去本文件之后那两个数。**量出来的，不是算出来的。**
#: ⚠️ 2026-09-21 重量：ADR-0144 决策 3 加了 6 条判据，2,324 -> 2,330。
CONTROL_BASELINE_PASSED = 2335
CONTROL_BASELINE_SKIPPED = 46

PACKAGE = ROOT / "profiles" / "internal-demo-sales" / "profile.json"

TOUCHED = (PACKAGE,)

#: ⚠️ **`:` 与 `.` 要在字符集里**：收集失败时 harness 报的是
#: `collection-error:test_x.py::test_y`，用不带这两个字符的正则去解，
#: 那个名字会被切成四段，于是声明的连带**永远对不上**。
#: 本批实测撞到一次：13 支全是「没红 11 个 collection-error」，
#: 而它们其实每一支都红了。
_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


#: ⚠️ **配包是半个仓的夹具，而这 39 条是量出来的，不是想出来的。**
#:
#: 第一版我声明的是一条（`test_the_demo_profile_still_seals`），实测
#: **39 条，而那一条恰好不在里面**。机制比「封版戳过期」深一层：
#: 改 `profile.json` 的任何一个字节，`content_hash` 就对不上，
#: **注册表直接拒绝加载整个包** —— 于是每一条拿这个包当夹具的用例
#: 要么红、要么连收集都过不去（下面 11 条 `collection-error:` 就是后者）。
#:
#: **留着这张表而不是把判据放宽**，是因为它本身是一句有用的事实：
#: 配包不是「一个配置文件」，它是半个 contracts 层的夹具。
PACKAGE_UNLOADABLE = (
    "collection-error:test_human_takeover_semantics.py::test_a_model_update_cannot_overwrite_a_human_written_slot",
    "collection-error:test_human_takeover_semantics.py::test_a_model_written_slot_is_still_governed_by_the_confidence_rule_alone",
    "collection-error:test_human_takeover_semantics.py::test_a_resumed_stack_has_at_least_one_legal_operation[one]",
    "collection-error:test_human_takeover_semantics.py::test_a_resumed_stack_has_at_least_one_legal_operation[two]",
    "collection-error:test_human_takeover_semantics.py::test_a_single_frame_stack_resumes_active_not_repair_pending",
    "collection-error:test_human_takeover_semantics.py::test_resuming_a_deeper_stack_puts_the_top_frame_back_active",
    "collection-error:test_human_takeover_semantics.py::test_resuming_a_stack_that_was_never_suspended_changes_nothing",
    "collection-error:test_human_takeover_semantics.py::test_resuming_an_already_resumed_stack_changes_nothing",
    "collection-error:test_human_takeover_semantics.py::test_resuming_never_disturbs_a_terminal_frame",
    "collection-error:test_human_takeover_semantics.py::test_the_correction_flag_does_not_unlock_a_human_written_slot",
    "collection-error:test_synthetic_carrier_executability.py::test_every_recorded_transition_is_one_the_validator_accepts",
    "test_a_bad_slot_name_is_stopped_by_whichever_layer_owns_it",
    "test_a_completed_child_still_needs_the_confirmation_its_profile_requires",
    "test_a_forbidden_child_can_still_be_popped_directly",
    "test_a_frame_abandoned_by_the_pop_is_still_read_as_an_exit_signal",
    "test_a_registry_root_outside_the_repository_is_refused",
    "test_a_slot_update_from_the_model_actually_lands_on_the_frame",
    "test_an_abandoned_child_leaves_the_stack_without_a_confirmation",
    "test_at_depth_three_the_exit_signal_reaches_only_the_immediate_parent",
    "test_every_frame_id_the_corpus_names_exists_in_the_profile",
    "test_every_package_shipping_today_seals_under_the_new_rule",
    "test_every_shipped_package_still_publishes",
    "test_no_withheld_key_appears_anywhere_in_the_projection",
    "test_registry_records_provenance_relative_to_the_repository_root",
    "test_repair_refuses_a_return_after_the_child_was_abandoned",
    "test_repair_refuses_a_return_on_the_turn_the_exit_signal_arrives",
    "test_repair_refuses_when_the_profile_forbids_resume_confirmation",
    "test_repair_still_works_for_the_case_it_was_written_for",
    "test_the_exit_signal_reaches_the_stack_through_the_real_transitions",
    "test_the_model_is_told_which_slots_the_goal_wants",
    "test_the_optional_keys_are_present_and_empty_when_the_package_has_none",
    "test_the_optional_keys_carry_through_when_the_package_has_them",
    "test_the_ordinary_return_still_works",
    "test_the_package_loads_through_the_registry_and_hashes_to_its_declaration",
    "test_the_parent_goal_is_not_handed_back_as_active_after_an_exit",
    "test_the_platform_supplies_the_two_identities_and_the_document_validates",
    "test_the_projection_gives_exactly_the_model_fields",
    "test_the_registry_trusts_the_deployment_it_runs_in_not_the_tree_it_was_built_from",
    "test_the_validator_and_the_transition_never_disagree",
)

#: ⚠️ **这里曾经有一条 `GOAL_ROOM`，同批被删掉了。**
#: `goal` 当时是 1,099 / 1,100，**任何加长的变异都顺带顶到那条上限**，
#: 于是 C 与 J 各多红一条「离合同上限还有多少」。
#: 同一批把合同抬到 1,500、守卫抬到 1,400（用户裁定），
#: 余量从 1 字变成 301 字，那两支就够不着它了 ——
#: **留这段注释是因为「为什么少了一条连带」比「少了一条」本身更值得记。**

#: 改 `availability` 那一支额外顶两条 —— 它动的是素材档，不只是 `goal`。
AVAILABILITY_EXTRA = (
    "test_the_healthy_package_still_seals",
    "test_the_shipped_packages_pass_even_with_no_topic_concurrency_declared",
)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 开场那个 ref 从 goal 里拿掉",
            [Mutation(PACKAGE, "——引 `material:demo-open-what-we-do:v1` 原文。", "。")],
            ["test_the_goal_names_which_material_to_open_with"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "B · 桥接那个 ref 拿掉",
            [
                Mutation(
                    PACKAGE,
                    "接着讲对应的能力——引 `material:demo-bridge-to-capability:v1`；",
                    "接着讲对应的能力；",
                )
            ],
            ["test_the_goal_names_which_material_to_open_with"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "C · 点名一个包里没有的 ref",
            [
                Mutation(
                    PACKAGE,
                    "material:demo-open-what-we-do:v1` 原文。",
                    "material:demo-open-there-is-no-such-thing:v1` 原文。",
                )
            ],
            # ⚠️ **`…pushable_at_those_moments` 不红，而那是对的**：
            # 它读的是常量表对包，这一刀换的是 `goal` 里那个号。
            # 「点名的号存不存在」由中间那条守 —— 它本身也是被这一支
            # 逼着从「拿常量核常量」改成「从 `goal` 里现读」的。
            [
                "test_the_goal_names_which_material_to_open_with",
                "test_the_named_materials_are_really_in_the_package",
            ],
            PACKAGE_UNLOADABLE,
        ),
        (
            "D · 开场那段素材改成 on_request（点了名却推不出去）",
            [
                Mutation(
                    PACKAGE,
                    '"ref": "material:demo-open-what-we-do:v1",\n'
                    '          "availability": "proactive"',
                    '"ref": "material:demo-open-what-we-do:v1",\n'
                    '          "availability": "on_request"',
                )
            ],
            ["test_the_two_named_materials_are_pushable_at_those_moments"],
            PACKAGE_UNLOADABLE + AVAILABILITY_EXTRA,
        ),
        (
            "E · 「不要重复对方的话」整条删掉",
            [
                Mutation(
                    PACKAGE,
                    "**不要重复对方的话**：复述一遍没必要（「您说没人管——这话我接住了」是白说）。",
                    "",
                )
            ],
            ["test_the_goal_bans_restating_and_only_that"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "F · 把「铺垫、共情、点评可以有」删掉（规则变得比裁定宽）",
            [
                Mutation(
                    PACKAGE,
                    "铺垫、共情、点评可以有，但**说人话别端着**",
                    "**说人话别端着**",
                )
            ],
            ["test_the_goal_bans_restating_and_only_that"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "G · 禁令里删掉「我收到了」「我知道了」",
            [
                Mutation(
                    PACKAGE,
                    "「我记下了」「我听到了」「我收到了」「我知道了」「我来安排」",
                    "「我记下了」「我听到了」「我来安排」",
                )
            ],
            ["test_the_goal_says_understanding_is_not_announced"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "H · 删掉「看懂了不用说出来」",
            [Mutation(PACKAGE, "，**看懂了不用说出来**；", "；")],
            ["test_the_goal_says_understanding_is_not_announced"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "I · 「跟着他的关注点走」整条删掉",
            [
                Mutation(
                    PACKAGE,
                    "**跟着他的关注点走**：他更在意哪件就讲哪件，"
                    "不必把当前这件讲完才换（有人不在意价格，更想知道功能）。",
                    "",
                )
            ],
            ["test_the_goal_follows_him_rather_than_a_script"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "J · 疑虑改回「最后才碰」（与跟着他走打架）",
            [
                Mutation(
                    PACKAGE,
                    "疑虑他没提就不要主动提，",
                    "最后才碰疑虑，他没提就不要主动提，",
                )
            ],
            ["test_the_goal_follows_him_rather_than_a_script"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "K · 两个新槽删掉一个",
            [
                Mutation(
                    PACKAGE,
                    ',\n'
                    '        {\n'
                    '          "name": "raised_concern",\n'
                    '          "type": "string",\n'
                    '          "required": false,\n'
                    '          "description": "他自己提出来的疑虑。"\n'
                    '        }',
                    "",
                )
            ],
            [
                "test_the_new_slots_do_not_make_the_frame_harder_to_leave",
                "test_the_thing_he_was_asked_three_times_now_has_a_slot",
            ],
            # ⚠️ **多的那一条是 ADR-0144 决策 3 带来的，而它是对的**：
            # 删掉一个槽，`test_every_shipped_slot_has_one` 钉的那个 19 就不成立。
            # **连带只能量** —— 这一条是 rebase 之后重跑才出现的。
            PACKAGE_UNLOADABLE + ("test_every_shipped_slot_has_one",),
        ),
        (
            "L · 新槽改成 required: true（这一帧更出不去）",
            [
                Mutation(
                    PACKAGE,
                    '"name": "current_handling",\n'
                    '          "type": "string",\n'
                    '          "required": false,',
                    '"name": "current_handling",\n'
                    '          "type": "string",\n'
                    '          "required": true,',
                )
            ],
            ["test_the_new_slots_do_not_make_the_frame_harder_to_leave"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "M · 把那条通则删掉（「别再问一遍」又只剩槽位那一种）",
            [
                Mutation(
                    PACKAGE,
                    "**摘要里问过、他也答过的，别再问第二遍** —— 不管那件事有没有槽位。",
                    "",
                )
            ],
            ["test_the_thing_he_was_asked_three_times_now_has_a_slot"],
            PACKAGE_UNLOADABLE,
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
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
                print(
                    f"    ⚠️ 本文件：多红 {sorted(set(names) - set(expects))}；"
                    f"没红 {sorted(set(expects) - set(names))}"
                )
            if not collateral_hit:
                print(
                    f"    ⚠️ 连带：多红 {sorted(set(control_names) - set(collateral))}；"
                    f"没红 {sorted(set(collateral) - set(control_names))}"
                )
            print()

    print("=" * 72)
    bad = [label for label, _, hit, coll in verdicts if not (hit and coll)]
    for label, went_red, hit, coll in verdicts:
        if hit and coll:
            mark = "RED ✅" if went_red else "GREEN ✅（声明如此）"
        else:
            mark = "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
