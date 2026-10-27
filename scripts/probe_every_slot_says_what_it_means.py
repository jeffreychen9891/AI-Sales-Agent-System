#!/usr/bin/env python
"""反向探针：`tests/contracts/test_every_slot_says_what_it_means.py`（ADR-0144 决策 3）。

六条判据各要一支证明它真的会红：

* A · 抽掉一段描述（合同必填，所以它**在合同那一层**就该被拦下来）；
* B · **把描述留着但不下发** —— `request_shape` 不再放它进 `slots_wanted`。
  **这一支是本文件最要紧的那一支**：写了没人读等于没写，
  而那种失败**配包里一个字都不缺**，只有模型看不见；
* C · 把行为约束抄进描述（用户 2026-09-21：行为约束归 `goal`）；
* D · 三份配包的 `closing_acknowledged` 统一成一句（同名不同义就没了）；
* E · 合同把 `maxLength` 放到很大（它每轮付两遍）；
* F · 合同去掉 `additionalProperties: false`（拼错的键会悄悄过）。

⚠️ **带 `control=`**：改配包会让 `content_hash` 对不上、整包加载不了，
所以连带集合很大 —— **量出来，不是想出来**。

    python scripts/probe_every_slot_says_what_it_means.py
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
TARGET = "tests/contracts/test_every_slot_says_what_it_means.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 6

#: `tests/contracts` 减去本文件。**量出来的。**
CONTROL_BASELINE_PASSED = 2338
CONTROL_BASELINE_SKIPPED = 46

DEMO = ROOT / "profiles" / "internal-demo-sales" / "profile.json"
CARRIER = ROOT / "profiles" / "synthetic-carrier" / "profile.json"
THIRD = ROOT / "profiles" / "synthetic-third" / "profile.json"
SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"
SHAPE = ROOT / "packages" / "runtime" / "src" / "fastagent_v2" / "runtime" / "request_shape.py"

TOUCHED = (DEMO, CARRIER, THIRD, SCHEMA, SHAPE)

#: ⚠️ **改配包正文的那几支，连带是这 39 条 —— 量出来的。**
#:
#: 机制：动 `profile.json` 一个字节，`content_hash` 就对不上，
#: **注册表直接拒绝加载整个包**，于是每一条拿它当夹具的用例要么红、
#: 要么连收集都过不去（下面 11 条 `collection-error:` 是后者）。
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

#: A 支额外多三条 —— 它不只改了正文，**还把合同必填的键抽掉了**。
SCHEMA_ALSO = (
    "test_a_package_whose_material_can_never_be_delivered_is_refused",
    "test_the_healthy_package_still_seals",
    "test_the_shipped_packages_pass_even_with_no_topic_concurrency_declared",
)

#: ⚠️ **B 与 E 的连带是空集，而那是这份测量里最有意思的一条。**
#: 它们改的是 `request_shape.py` 与 schema 里的一个数 —— **配包一个字节没动**，
#: 于是全仓只有本文件那一条判据红。
#:
#: **「描述没下发」正是这种形状**：配包里一个字都不缺、封版戳对得上、
#: 别的用例全绿，**只有模型看不见它**。B 支存在的理由就是它。

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 抽掉一段描述",
            [
                Mutation(
                    DEMO,
                    ',\n          "description": "他自己提出来的疑虑。"',
                    "",
                )
            ],
            [
                "test_behavioural_rules_stayed_in_the_goal",
                "test_every_shipped_slot_has_one",
                "test_the_description_is_bounded_because_it_is_paid_twice",
                "test_the_description_reaches_the_model",
            ],
            PACKAGE_UNLOADABLE + SCHEMA_ALSO,
        ),
        (
            "B · 描述留着但不下发（写了没人读）",
            [
                Mutation(
                    SHAPE,
                    '        # **紧挨着名字**：模型读到一个槽名，下一眼就该是它的意思。\n'
                    '        "description": slot.get("description"),\n',
                    "",
                )
            ],
            ["test_the_description_reaches_the_model"],
            (),
        ),
        (
            "C · 把行为约束抄进描述",
            [
                Mutation(
                    DEMO,
                    '"description": "他这一轮在意的是哪一类损耗。"',
                    '"description": "他这一轮在意的是哪一类损耗。只填一个，不要拼几类。"',
                )
            ],
            ["test_behavioural_rules_stayed_in_the_goal"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "D · 三份的 closing_acknowledged 统一成一句",
            [
                Mutation(
                    CARRIER,
                    '"description": "The customer acknowledged how the loan ended."',
                    '"description": "The customer agreed the exchange can end."',
                )
            ],
            ["test_the_same_slot_name_in_three_packages_says_three_different_things"],
            PACKAGE_UNLOADABLE,
        ),
        (
            "E · 合同把 maxLength 放到很大",
            [Mutation(SCHEMA, '"maxLength": 120,', '"maxLength": 1200,')],
            ["test_the_description_is_bounded_because_it_is_paid_twice"],
            (),
        ),
        (
            "F · 合同去掉闭合性",
            [
                Mutation(
                    SCHEMA,
                    '                "name",\n'
                    '                "type",\n'
                    '                "required",\n'
                    '                "description"\n',
                    '                "name",\n'
                    '                "type",\n'
                    '                "required"\n',
                )
            ],
            ["test_the_description_is_bounded_because_it_is_paid_twice"],
            (),
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
