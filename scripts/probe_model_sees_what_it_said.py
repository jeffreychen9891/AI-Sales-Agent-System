#!/usr/bin/env python
"""反向探针：模型看得见它自己已经讲过哪几段。

    python scripts/probe_model_sees_what_it_said.py

| | 变异 | 演的是 |
|---|---|---|
| A | 这份清单永远是空的 | **这一批之前的样子**：模型每一轮都像第一次看到这些素材 |
| B | 少接一个判断步 | `last_outbound` 那一批栽过的形状：只接上一个 |
| C | 改成读模型自填的那个槽 | 拿模型的记账去答「讲过哪几段」—— 猜错了没有任何东西会红 |
| D | 留头不留尾 | 「上次讲到哪」问的是最近那几段，从头截留下一串开场白 |
| E | 上限压到 2 | 一个**开始咬在架配包**的界 —— 而它不会自己说话 |

## E 是这一批唯一一个新立的数

有界是对的（清单长度 ≤ 本帧素材条数，而顶满约 160 段 ≈ 每轮 14KB），
**而一个咬得到在架配包的界是另一回事**：它每一轮都在悄悄少给。
`test_the_cap_is_above_every_shipped_frame` 钉的就是这条边。
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
TARGET = "tests/contracts/test_the_model_sees_what_it_has_already_said.py"

#: 实测 2026-09-18，随门禁回填。
#:
#: **批 E 之后 control 从 1,980 变成 2,006** —— 那一批往 `tests/contracts`
#: 加了用例。骨架要求当场改这个数：一个「绿但对不上」的基线，
#: 与「被杀掉的探针留了半个变异在树上」长得一模一样。
BASELINE_PASSED = 8
CONTROL_BASELINE_PASSED = 2006

SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
TOUCHED = (SHAPE, PIPELINE)

#: A —— 永远空。
#:
#: **锚点取 `_recent_refs` 自己，不取那个键** —— 那个键在文件里出现两次
#: （理解步与计划步各一），而探针骨架拒绝非唯一锚点：
#: 「a partial mutation proves nothing」。
SENDS = "    return [str(ref) for ref in list(refs)[-MATERIALS_SENT_SHOWN:]]"
SENDS_NOTHING = "    return []"

#: B —— 少接一个判断步（计划步那一处）。
PLAN_SITE = """                materials_already_sent=prepared.delivered_materials,
            )
        except (ProviderError, CostCeilingExceeded) as error:"""
PLAN_SITE_NOT = """            )
        except (ProviderError, CostCeilingExceeded) as error:"""

#: C —— 改成读模型自填的那个槽（只动理解步第一处）。
#:
#: **锚点带上后面那一行**：`materials_already_sent=prepared.delivered_materials,`
#: 在文件里有三处，三处的下一行各不相同 —— 这是把它变唯一的最短办法。
PLATFORM_RECORD = """                materials_already_sent=prepared.delivered_materials,
            )
        except CostCeilingExceeded as error:"""
MODEL_RECORD = """                materials_already_sent=(
                    prepared.aggregate["target_stack"][-1]["slots"].get(
                        "material_delivered"
                    )
                    or (),
                ),
            )
        except CostCeilingExceeded as error:"""

#: D —— 留头不留尾。与 A 同一个锚点、不同的替换，各自一支。
KEEPS_TAIL = "list(refs)[-MATERIALS_SENT_SHOWN:]"
KEEPS_HEAD = "list(refs)[:MATERIALS_SENT_SHOWN]"

#: E —— 上限压到咬得到在架配包的数。
CAP = "MATERIALS_SENT_SHOWN = 24"
CAP_TOO_LOW = "MATERIALS_SENT_SHOWN = 2"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 清单永远是空的",
            [Mutation(SHAPE, SENDS, SENDS_NOTHING)],
            [
                "test_both_judging_steps_see_what_was_already_said",
                "test_it_keeps_the_tail_not_the_head",
                "test_only_the_refs_travel_not_the_words",
            ],
            [],
        ),
        (
            "B · 少接一个判断步",
            [Mutation(PIPELINE, PLAN_SITE, PLAN_SITE_NOT)],
            [
                "test_every_judging_call_site_passes_it",
                "test_it_comes_from_the_platforms_own_record",
            ],
            [],
        ),
        (
            "C · 改读模型自填的槽",
            [Mutation(PIPELINE, PLATFORM_RECORD, MODEL_RECORD)],
            ["test_it_comes_from_the_platforms_own_record"],
            [],
        ),
        (
            "D · 留头不留尾",
            [Mutation(SHAPE, KEEPS_TAIL, KEEPS_HEAD)],
            ["test_it_keeps_the_tail_not_the_head"],
            [],
        ),
        (
            "E · 上限压到 2",
            [Mutation(SHAPE, CAP, CAP_TOO_LOW)],
            # **只有一条**：留尾那条用例按 `MATERIALS_SENT_SHOWN` 现读造夹具，
            # 所以上限压到 2 它照样成立 —— 它守的是「留哪一头」，不是「界多大」。
            ["test_the_cap_is_above_every_shipped_frame"],
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
