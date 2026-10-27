#!/usr/bin/env python
"""反向探针：一段话术只逐字发一次（对话质量那一批，2026-09-20）。

本批的主张有三条，逐条要证明**它真的会红**：

1. **第二次不给** —— 把 `ON_REQUEST` 从那个正向列举里拿掉，复读就回来了；
2. **第一次要给** —— 把整档收掉（不分 availability），
   运维 §5.1「问价没拿到价格」当场回来；
3. **goal 与代码是一对** —— goal 不告诉模型「清单里没有它」是什么意思，
   模型就会以为自己报不了价。

⚠️ **第 2 支尤其要跑**：本批第一版写的就是那个不分档的写法，
两条既有守卫当场抓住（「没声明 availability」是一个合法档）。
**探针把那次真实的错固定成一支会红的变异。**

    python scripts/probe_the_same_line_is_not_read_out_twice.py
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
TARGET = "tests/contracts/test_the_same_line_is_not_read_out_twice.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 5

#: `tests/contracts` 减去本文件之后的那个数。**量出来的，不是算出来的。**
CONTROL_BASELINE_PASSED = 2243

#: 对照那一跑跳过几条。**实测**，不是猜的。
CONTROL_BASELINE_SKIPPED = 32

SELECTION = ROOT / "services/worker/src/fastagent_v2/worker/material_selection.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"

#: 那个正向列举 —— 本批把 `ON_REQUEST` 加了进去。
THE_ENUMERATION = """        if ref in already_sent and declaration.get("availability") in (
            PROACTIVE,
            TIMER,
            ON_REQUEST,
        ):"""

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把 `ON_REQUEST` 从列举里拿掉 —— 复读回来了",
            [
                Mutation(
                    SELECTION,
                    THE_ENUMERATION,
                    '        if ref in already_sent and declaration.get("availability") in (\n'
                    "            PROACTIVE,\n"
                    "            TIMER,\n"
                    "        ):",
                )
            ],
            ["test_the_same_material_is_not_offered_a_second_time"],
            # **实测两条连带，而这是好事**：同一条规则今天有三处钉着 ——
            # 本文件那条、`test_no_echo` 里被本批反过来的那条，
            # 以及那条「列举了哪几档」的形状守卫。
            [
                "test_an_on_request_material_is_not_read_out_a_second_time",
                "test_the_filter_names_the_tiers_it_means",
            ],
        ),
        (
            "B · 不分档全收（本批第一版那个写法）—— 第一次也拿不到了",
            [Mutation(SELECTION, THE_ENUMERATION, "        if ref in already_sent:")],
            # ⚠️ **本文件一条都不红，而这是实测出来的，不是设计出来的。**
            #
            # 我原本声明 `test_the_material_is_offered_the_first_time` 会红 ——
            # **它不会**：那条用例的夹具是 `already_sent=()`，
            # 而这个变异只在 `already_sent` 非空时才改变行为。
            # **一条永远走不到那个分支的用例，证明不了那个分支的事。**
            #
            # 真正接住这一刀的是**既有的两条**（见连带）：
            # 「没声明 `availability`」是一个合法档（schema 与 `_is_offered`
            # 都说它每轮都递），以及那条形状守卫。
            # **本批第一版犯的就是这个错，而抓住它的正是它们。**
            [],
            [
                "test_a_material_without_an_availability_is_offered_every_turn",
                "test_the_filter_names_the_tiers_it_means",
            ],
        ),
        (
            "C · goal 不再说「清单里没有它就是已讲过」",
            [
                Mutation(
                    PROFILE,
                    "**清单里没有它就是已讲过**，改用自己的话说那个数，别说报不了价。",
                    "改用自己的话说那个数。",
                )
            ],
            ["test_the_goal_tells_the_model_what_to_do_when_the_line_is_gone"],
            # **实测一条连带，而它是 `repin` 的边界**：重封版改了 `content_hash`，
            # 而三份 runbook 里抄着那个戳的副本没跟着变 —— 于是那条
            # 「上线说明与包里是同一批数」的守卫红了。
            # ⚠️ **这不是要修的东西**：它恰恰证明那条守卫在盯着副本，
            # 而本批真的改包时**也确实把三份 runbook 一起改了**。
            ["test_the_runbook_quotes_the_stamp_in_the_package"],
        ),
    ]

    def repin() -> None:
        """改过配包就要重算封版戳 —— 否则红的是哈希那条，不是要探的那条。

        `AGENTS.md`「对内容寻址的产物，必须连同地址一起重算」：
        本仓 2026-09-03 为它撞过两次，其中一次变异只让哈希用例变红，
        **被它想探的那条断言毫无反应**。
        """

        import subprocess  # noqa: PLC0415

        subprocess.run(  # noqa: S603
            [str(PYTHON), "scripts/seal_profile_package.py", "profiles/internal-demo-sales"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )

    with ProbeSession(paths=(SELECTION, PROFILE), run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                control_passed=CONTROL_BASELINE_PASSED,
                # ⚠️ **skipped 也要报数**，而它不是 0：商家前端那一批带进来
                # 32 条按条件跳过的用例（响应模型那一族）。
                # harness 拒绝「只报 passed」—— `pytest.mark.skip`
                # 正是让一整个文件闭嘴的手段。
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            touches_profile = any(mutation.path == PROFILE for mutation in mutations)
            result = session.probe(label, mutations, repin=repin if touches_profile else None)
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
        mark = ("RED ✅" if went_red else "GREEN ✅（声明如此）") if hit and coll else "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
