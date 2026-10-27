#!/usr/bin/env python
"""反向探针：T299 那一批 —— `tests/contracts/test_a_remembered_fact_has_a_lifetime.py`。

本批的主张逐条要有一支探针证明**它真的会红**：

1. 两处的 365 是同一个数；
2. **省略 = 永不过期**（改掉这个默认，一条真实字段就会凭空过期）；
3. 边界判据是 `>` 不是 `>=`；
4. **引擎两处都过滤** —— 只留一处会造出「那个字段从此空着」；
5. 过期那行**写新值时让位**，否则撞唯一索引；
6. 控制面把 `expired` 显示出来。

⚠️ **带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去本文件」，
把连带集合**量出来**而不是想出来。

    python scripts/probe_a_remembered_fact_has_a_lifetime.py
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
TARGET = "tests/contracts/test_a_remembered_fact_has_a_lifetime.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 11

#: `tests/contracts` 减去本文件之后的那个数。
#:
#: ⚠️ 第一版写 2149，是我拿「contracts 总数 2160 减本文件 11」算的 ——
#: 而那个 2160 是本文件**还只有 10 条**时量的。harness 当场拒绝开跑并报出
#: 实测 2150（「报数，不报颜色」那条闸）。**这个数只能量，不能算。**
CONTROL_BASELINE_PASSED = 2150

LIFETIME = ROOT / "packages" / "runtime" / "src" / "fastagent_v2" / "runtime" / "memory_lifetime.py"
SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
APP = ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"

TOUCHED = (LIFETIME, SCHEMA, PIPELINE, APP)

#: 「省略 = 永不过期」在代码里就是这两行。
NEVER_EXPIRES = """    ttl = lifetimes.get(field_key)
    if ttl is None:"""

#: 边界那一行。
BOUNDARY = "    return now - created_at > timedelta(days=ttl)"

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把「省略 = 永不过期」改成「省略 = 30 天」",
            [
                Mutation(
                    LIFETIME,
                    NEVER_EXPIRES,
                    "    ttl = lifetimes.get(field_key, 30)\n    if ttl is None:",
                )
            ],
            ["test_omitting_ttl_days_means_never_expires"],
            (),
        ),
        (
            "B · 边界改成 `>=`（整整 30 天就算过期）",
            [
                Mutation(
                    LIFETIME,
                    BOUNDARY,
                    "    return now - created_at >= timedelta(days=ttl)",
                )
            ],
            ["test_the_boundary_is_strictly_greater_than"],
            (),
        ),
        (
            "C · schema 的 `maximum` 与常量不一致（365 -> 400）",
            [
                Mutation(
                    SCHEMA,
                    '"ttl_days": {"type": "integer", "minimum": 1, "maximum": 365}',
                    '"ttl_days": {"type": "integer", "minimum": 1, "maximum": 400}',
                )
            ],
            ["test_the_cap_is_one_number_in_two_places"],
            (),
        ),
        (
            "D · 召回那一处不再过滤（模型又看得见过期的事实）",
            [
                Mutation(
                    PIPELINE,
                    """        live = live_memories(
            [dict(row) for row in rows],
            now=datetime.now(UTC),
            lifetimes=lifetimes,
        )""",
                    "        live = [dict(row) for row in rows]",
                )
            ],
            ["test_both_engine_readers_filter_by_lifetime"],
            (),
        ),
        (
            "E · 过期那行不再让位（新值写不进去，且会撞唯一索引）",
            [
                Mutation(
                    PIPELINE,
                    "            if field_key in expired:",
                    "            if False:",
                )
            ],
            ["test_an_expired_memory_does_not_keep_its_slot_forever"],
            (),
        ),
        (
            "F · 控制面不再显示 `expired`",
            [
                Mutation(
                    APP,
                    '            "expired": expired,\n',
                    "",
                )
            ],
            ["test_the_control_plane_says_which_ones_expired"],
            (),
        ),
        (
            "G · `_MEMORY_COLUMNS` 丢掉 `created_at`（守卫的守卫该红）",
            [
                Mutation(
                    APP,
                    '"source_event_id, authorised_by, authorisation_note, '
                    'created_at, tombstoned_at"',
                    '"source_event_id, authorised_by, authorisation_note, '
                    'tombstoned_at"',
                )
            ],
            # **实测只红一条，而那正是这一对断言的分工，不是漏了。**
            # `test_every_reader_asks_for_created_at` 允许一处用
            # `_MEMORY_COLUMNS` 来证明它取了 `created_at` —— 常量名还在函数体里，
            # 所以把常量掏空它照样绿。**接住这一刀的是守卫的守卫。**
            # 这支探针因此证明了两条断言**谁都不能单独存在**：
            # 少了那一条，掏空常量就是一次静默的全线失效。
            ["test_the_shared_column_list_carries_created_at"],
            (),
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
