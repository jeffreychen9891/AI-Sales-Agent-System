#!/usr/bin/env python
"""反向探针：走不了的那一档当场说话（T362）。

    python scripts/probe_big_file_tier_refusal.py

| | 变异 | 演的是 |
|---|---|---|
| A | 这道拒绝永不触发 | **这一批之前的样子**：算得出 `22`、发得出去、等一个不会成功的回调 |
| B | 见档就拒 | 图片与小视频也传不上去 —— **过严那一侧，整批资料停摆** |
| C | 门槛写成 `>=` | 刚好 20MB 的那一份被误拦 |
| D | 导入那一道不干 | 一份收不了的资料躺到上传那一刻才失败 |
| E | 上传那一道不干 | 供应商**把整份文件拉完**再拒 —— 那一次是我们让它白拉的 |
| F | dry-run 永远回 0 | 「真跑之前先看一眼」看见了却不说 |
| G | 导入永远回 0 | 印了却仍然回 0，等于没拒 |
| H | 大小判据不看类型 | 每一份大图都被拦下，而它们今天传得上去 |
| I | 那一档不再算得出来 | 拒绝成了零触发的代码，而它还挂着 |

## B / C / H 是这一批的三条「过严」边

一道多拒一档的闸，**与「该拒的拒了」在绿色上长得一模一样**，
而代价是整批资料传不上去。这三支各演一种拒错的方式。

## A 与 I 是这道拒绝的两个立足点

它立在两个事实上：**那个端点今天没有实现**（A 方向）、
**而那一档今天仍然算得出来**（I 方向）。
两个事实哪一个变了，要改的都是这道拒绝本身 —— 所以两头都要有东西会红。
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
TARGET = "tests/contracts/test_the_big_file_tier_refuses_loudly.py"

#: 实测 2026-09-18，随门禁回填。
BASELINE_PASSED = 11
CONTROL_BASELINE_PASSED = 1928

IMPORTER = ROOT / "scripts/import_materials.py"
UPLOADER = ROOT / "scripts/upload_materials.py"
TOUCHED = (IMPORTER, UPLOADER)

#: A —— 永不触发。
FIRES = "    if file_type != BIG_VIDEO_FILE_TYPE:\n        return None"
FIRES_NEVER = "    if True:\n        return None"

#: B —— 见档就拒。
REFUSES_ONE = "    if file_type != BIG_VIDEO_FILE_TYPE:"
REFUSES_ALL = "    if False:"

#: C —— 门槛写成 `>=`。
THRESHOLD = "if base == 4 and byte_size > BIG_VIDEO_THRESHOLD_BYTES:"
INCLUSIVE = "if base == 4 and byte_size >= BIG_VIDEO_THRESHOLD_BYTES:"

#: H —— 大小判据不看类型。
TYPE_AWARE = "if base == 4 and byte_size > BIG_VIDEO_THRESHOLD_BYTES:"
TYPE_BLIND = "if byte_size > BIG_VIDEO_THRESHOLD_BYTES:"

#: D —— 导入量完之后那一道不干。
MEASURED_CHECK = """        refusal = unsupported_tier_refusal(
            provider_file_type(str(entry["media_type"]), int(byte_size))
        )
        if refusal is not None:"""
MEASURED_CHECK_NOT = """        refusal = unsupported_tier_refusal(
            provider_file_type(str(entry["media_type"]), int(byte_size))
        )
        if False:"""

#: E —— 上传那一道不干。
PRE_CALL = """            blocked = unsupported_tier_refusal(file_type)
            if blocked is not None:
                failures.append(f"{name} 没传：{blocked}")
                continue"""
PRE_CALL_NOT = "            pass"

#: F / G —— 两个退出码。
DRY_EXIT = "            return 1 if unsupported else 0"
DRY_EXIT_ZERO = "            return 0"
IMPORT_EXIT = '    return 1 if counts["refused"] else 0'
IMPORT_EXIT_ZERO = "    return 0"

#: I —— 那一档不再算得出来。
COMPUTES = "        return BIG_VIDEO_FILE_TYPE"
COMPUTES_NOT = "        return base"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 拒绝永不触发",
            [Mutation(IMPORTER, FIRES, FIRES_NEVER)],
            [
                "test_only_the_tier_without_an_endpoint_is_refused",
                "test_the_refusal_says_why_and_where_to_look",
                "test_a_video_over_the_threshold_is_refused_at_import",
                "test_the_size_is_judged_again_after_it_is_measured",
            ],
            [],
        ),
        (
            "B · 见档就拒",
            [Mutation(IMPORTER, REFUSES_ONE, REFUSES_ALL)],
            [
                "test_only_the_tier_without_an_endpoint_is_refused",
                "test_exactly_at_the_threshold_still_goes_through",
                "test_a_big_image_is_not_the_big_video_tier",
            ],
            [],
        ),
        (
            "C · 门槛写成 >=",
            [Mutation(IMPORTER, THRESHOLD, INCLUSIVE)],
            ["test_exactly_at_the_threshold_still_goes_through"],
            # 连带：`provider_file_type` 的门槛在另一个文件里也被钉着，
            # 那条用例记的是 **v1 生产代码实测出来的那两个数**。
            ["test_a_big_video_switches_to_the_file_type_v1_measured"],
        ),
        (
            "D · 导入量完之后不判",
            [Mutation(IMPORTER, MEASURED_CHECK, MEASURED_CHECK_NOT)],
            # ⚠️ **这一支第一次跑是全绿的**：那条用例当时是一条源码扫描
            # （「正文里出现过 `unsupported_tier_refusal`，且在 `measure` 之后」），
            # 而把 `if` 改成 `if False:` 之后那两句话仍然全对。
            # 现在它真的跑一遍 `import_entries`。
            ["test_the_size_is_judged_again_after_it_is_measured"],
            [],
        ),
        (
            "E · 上传那一道不干",
            [Mutation(UPLOADER, PRE_CALL, PRE_CALL_NOT)],
            ["test_the_upload_script_refuses_before_it_calls_the_provider"],
            [],
        ),
        (
            "F · dry-run 永远回 0",
            [Mutation(UPLOADER, DRY_EXIT, DRY_EXIT_ZERO)],
            ["test_the_dry_run_marks_them_and_exits_non_zero"],
            [],
        ),
        (
            "G · 导入永远回 0",
            [Mutation(IMPORTER, IMPORT_EXIT, IMPORT_EXIT_ZERO)],
            ["test_import_exits_non_zero_when_it_refused"],
            [],
        ),
        (
            "H · 大小判据不看类型",
            [Mutation(IMPORTER, TYPE_AWARE, TYPE_BLIND)],
            ["test_a_big_image_is_not_the_big_video_tier"],
            # 连带：`provider_file_type` 的门槛在另一个文件里也被钉着，
            # 那条用例记的是 **v1 生产代码实测出来的那两个数**。
            ["test_a_big_video_switches_to_the_file_type_v1_measured"],
        ),
        (
            "I · 那一档不再算得出来",
            [Mutation(IMPORTER, COMPUTES, COMPUTES_NOT)],
            [
                "test_the_tier_is_still_computable_so_the_refusal_is_not_dead_code",
                "test_a_video_over_the_threshold_is_refused_at_import",
                "test_the_size_is_judged_again_after_it_is_measured",
            ],
            # 连带：`provider_file_type` 的门槛在另一个文件里也被钉着，
            # 那条用例记的是 **v1 生产代码实测出来的那两个数**。
            ["test_a_big_video_switches_to_the_file_type_v1_measured"],
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
