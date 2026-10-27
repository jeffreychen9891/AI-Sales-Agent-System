#!/usr/bin/env python
"""反向探针：`tests/contracts/test_the_prompt_asks_for_what_the_contract_requires.py`。

**最要紧的一支是 A**：把提示词退回事故那天的样子（`Required keys` 里没有
`turn_summary`）—— 它必须红。**本批存在的全部理由就是它当时没红。**

其余各支钉住这条守卫的每一半：

* B · 示例里抽掉那个键（两处各写一遍，只钉一处不够）；
* C · **把那个键塞进豁免名单** —— 缺陷会安静地消失，而这正是
  「那不是一张名单，是四处赋值」要拦的；
* D · 让那段 AST 扫描瞎掉；
* E · 提示词多说一个契约不要的键（判据写集合相等，不写包含）；
* F · 换掉那句说明（模型只知道键名，不知道往里写什么）。

⚠️ **带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去本文件」，
把连带集合**量出来**而不是想出来。

    python scripts/probe_the_prompt_asks_for_what_the_contract_requires.py
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
TARGET = "tests/contracts/test_the_prompt_asks_for_what_the_contract_requires.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 5

#: `tests/contracts` 减去本文件之后那两个数。**量出来的，不是算出来的。**
#: ⚠️ **rebase 之后这两个数都会变**：本支 2026-09-20 rebase 到 `da7ee3b7`
#: （批 8 与 T258 合进来），2,297 -> 2,324、43 -> 46。**重量，不要推算。**
CONTROL_BASELINE_PASSED = 2324
CONTROL_BASELINE_SKIPPED = 46

MODEL = (
    ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_model.py"
)
GUARD = ROOT / "tests" / "contracts" / (
    "test_the_prompt_asks_for_what_the_contract_requires.py"
)

TOUCHED = (MODEL, GUARD)

#: 事故那天 `Required keys` 那一行逐字长什么样。
THE_LINE_BEFORE = (
    "Required keys: facts, intents, slot_updates, interruption, sentiment,\n"
    "risk_markers, customer_commitments, uncertainties, turn_summary.\n"
)
THE_LINE_AFTER = (
    "Required keys: facts, intents, slot_updates, interruption, sentiment,\n"
    "risk_markers, customer_commitments, uncertainties.\n"
)

#: **动一个字的提示词，就动了三张字节表** —— 实测，不是想出来的。
#:
#: 本批六支变异里有五支改的是 `_UNDERSTAND_SYSTEM` 的正文，而
#: `PLATFORM_PROMPT_BYTES` / `REQUEST_OVERHEAD_BYTES` 是从那份正文**现量现比**的，
#: 所以它们每一支都跟着红（parametrize 展开是 10 条，去重之后是这三个名字）。
#:
#: ⚠️ **这本身是一条值得写下来的事实**：本仓**早就有**一道会在提示词被改动时
#: 变红的闸 —— 只不过它守的是**字节数**，不是**内容**。
#: 事故那天提示词**一个字都没改**（改的是契约），所以它一声没吭。
#: **本批补的正是内容那一侧。**
PROMPT_BYTE_TABLES = (
    "test_the_floor_equals_the_request_it_claims_to_measure",
    "test_the_overhead_constants_are_measured_from_the_real_payload",
    "test_the_platform_prompt_sizes_are_spelled_once_per_package",
)

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把提示词退回事故那天（`Required keys` 里没有 turn_summary）",
            [Mutation(MODEL, THE_LINE_BEFORE, THE_LINE_AFTER)],
            [
                # ⚠️ **`test_the_example_shows_every_key_it_asks_for` 不红，
                # 而那是对的**：它遍历的是**提示词列出的**键，
                # 这一刀把 `turn_summary` 从那张单子上拿掉了，于是它无可遍历。
                # 那一条守的是另一件事（列了而示例里没有），由 B 支证明。
                "test_the_prompt_asks_for_every_key_the_contract_requires",
                "test_the_understanding_prompt_asks_for_turn_summary",
            ],
            PROMPT_BYTE_TABLES,
        ),
        (
            "B · 示例 JSON 里抽掉那个键（`Required keys` 那一行照旧）",
            [
                Mutation(
                    MODEL,
                    '  "uncertainties": [],\n'
                    '  "turn_summary": "example: he asked what the price covers; '
                    'we quoted it."\n',
                    '  "uncertainties": []\n',
                )
            ],
            ["test_the_example_shows_every_key_it_asks_for"],
            PROMPT_BYTE_TABLES,
        ),
        (
            "C · 把那个键塞进豁免名单（缺陷会安静地消失）",
            [
                Mutation(
                    MODEL,
                    THE_LINE_BEFORE,
                    THE_LINE_AFTER,
                ),
                Mutation(
                    GUARD,
                    '    {"schema_version", "event_id", "origin", "model_evidence"}',
                    '    {"schema_version", "event_id", "origin", "model_evidence",\n'
                    '     "turn_summary"}',
                ),
            ],
            # **不红的那一条才是重点**：塞进名单之后
            # `test_the_prompt_asks_for_every_key_the_contract_requires` **不再红**，
            # 而 `test_every_platform_written_key_is_really_written` 接住了它 ——
            # 因为 `turn_summary` 在 `inference_model.py` 里没有一处赋值。
            [
                # 同 A：示例那条无可遍历，不红。
                "test_every_platform_written_key_is_really_written",
                "test_the_understanding_prompt_asks_for_turn_summary",
            ],
            PROMPT_BYTE_TABLES,
        ),
        (
            "D · 让那段 AST 扫描瞎掉（只认 `payload[...]`）",
            [
                Mutation(
                    GUARD,
                    '                and target.value.id == "document"',
                    '                and target.value.id == "payload"',
                )
            ],
            ["test_every_platform_written_key_is_really_written"],
            # ⚠️ D 只动判据文件，**不动提示词** —— 所以连带是空集。
            (),
        ),
        (
            "E · 提示词多说一个契约不要的键",
            [
                Mutation(
                    MODEL,
                    "risk_markers, customer_commitments, uncertainties, turn_summary.\n",
                    "risk_markers, customer_commitments, uncertainties, turn_summary,\n"
                    "invented_key.\n",
                )
            ],
            [
                "test_the_prompt_asks_for_every_key_the_contract_requires",
                "test_the_example_shows_every_key_it_asks_for",
            ],
            PROMPT_BYTE_TABLES,
        ),
        (
            "F · 抽掉那句说明（模型只知道键名，不知道往里写什么）",
            [
                Mutation(
                    MODEL,
                    "- turn_summary: one line on this turn only (<=480 chars) -- "
                    "what he wanted and\n  what we did. Never restate earlier turns; "
                    "write one even when little happened.\n",
                    "",
                )
            ],
            ["test_the_understanding_prompt_asks_for_turn_summary"],
            PROMPT_BYTE_TABLES,
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
