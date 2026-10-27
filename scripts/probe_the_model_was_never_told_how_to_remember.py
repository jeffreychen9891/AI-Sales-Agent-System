#!/usr/bin/env python
"""反向探针：`tests/contracts/test_the_model_was_never_told_how_to_remember.py`。

**最要紧的两支**：

* A · **把提示词退回事故那天**（那几行整条删掉）—— 它必须红，
  因为本批存在的全部理由就是它当时没红；
* C · **让那张表不再下发**（`generate_prompt` 不放 `rememberable_fields`）——
  写了没人读等于没写，而那种失败**配包里一个字都不缺**。

其余：

* B · 只删掉「键从那张表里取」那半句（形状教了、来源没教）——
  ⚠️ **它第一次跑是全绿的**，那条判据是被这支探针逼出来的；
* D · 量纲写成 bytes（本仓为这件事撤过一道闸）；
* E · 把 `ttl_days` 也发出去（多给一个能被写错的东西）；
* F · 闸多要一个键而表没跟上 —— **钉「表守的不是它自己」**。
  ⚠️ 实测：只有对着源码那一条红，「教没教过」那一条**不红** ——
  而那是对的：后者读的是表，前者才是钉住漂移的那一条。
  **两条腿各管一段，不重叠。**
* G · 配包里抽掉一个字段的描述 —— 连带是封版戳那一片（见 `SEAL_BLAST`）；
* H · 版本号不跟着走；
* I · 描述在下发路上被抹成空串（**配包一个字没动**）——
  G 的干净版本：它只动下发那一步，所以连带是空的；
* J · **把版本号退回 1.3.0 而内容不动** —— 本批自己踩出来的那个洞：
  改内容不抬版本，在此之前全仓一条都不红；
* K · **把替身那一侧的参数拿掉** —— 本批栽过的第二个洞：
  生产签名多一个关键字参数而替身没跟上时，
  **`contracts` 2,381 条全绿，而门禁 123 条红**。

⚠️ **带 `control=`**：连带量出来 —— 改提示词会顶到字节表那一族。

    python scripts/probe_the_model_was_never_told_how_to_remember.py
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
TARGET = "tests/contracts/test_the_model_was_never_told_how_to_remember.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 10

#: `tests/contracts` 减去本文件。**量出来的。**
#:
#: ⚠️ **2,363 -> 2,372**：rebase 到 `5fe784a5` 之后，同日先落地的
#: 「一条说一件事」那一批带进来九条。
CONTROL_BASELINE_PASSED = 2372
CONTROL_BASELINE_SKIPPED = 53

MODEL = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference_model.py"
)
SHAPE = ROOT / "packages" / "runtime" / "src" / "fastagent_v2" / "runtime" / "request_shape.py"
PIPELINE = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
)
PORT = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference.py"
)
PACKAGE = ROOT / "profiles" / "internal-demo-sales" / "profile.json"

TOUCHED = (MODEL, SHAPE, PIPELINE, PACKAGE, PORT)

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


#: 提示词正文一动，这四条字节表就响 —— 与上一支实测的是同一族。
PROMPT_BYTES = (
    "test_the_empty_request_baseline_is_measured_from_the_real_payload",
    "test_the_floor_equals_the_request_it_claims_to_measure",
    "test_the_overhead_constants_are_measured_from_the_real_payload",
    "test_the_platform_prompt_sizes_are_spelled_once_per_package",
)

#: ⚠️ **只有 A 多这一条**，而那正是它与 D 的区别：
#: D 只动五个字节，A 删掉整整 559 —— **省下来的那一截落进素材池**，
#: 于是「按预算装满的那一轮还装得下吗」也跟着响。
BUDGET_FIT = ("test_a_turn_at_the_material_budget_still_fits_the_limit",)

#: ⚠️ **包着整个配包的封版戳。** 它不是本批的连带，而是**配包里任何一个字节**
#: 的连带：`identity.content_hash` 对不上 → 包加载不了 → 一路红到收集期。
#: G 支实测 **98 failed + 11 errors**，其中十一条是 collection error。
#: 逐名登记这 98 条没有意义（它们与「描述」无关），**改按性质断言**：
#: 封版那一条必须在里面，而规模必须是「一片」。
SEAL_BLAST = "<封版戳被掀翻：那 98 条与描述无关>"
SEAL_TEST = "test_the_package_loads_through_the_registry_and_hashes_to_its_declaration"

#: 本文件那八条的短名。
TAUGHT = "test_every_payload_the_platform_checks_is_taught_in_the_prompt"
GATES = "test_the_gates_still_check_exactly_those_keys"
TOLD = "test_the_model_is_told_which_fields_it_may_remember"
FROM_LIST = "test_the_model_is_told_the_key_must_come_from_that_list"
TTL = "test_the_field_table_carries_no_ttl"
DECLARED = "test_every_declared_field_says_what_it_holds"
BOUNDS = "test_the_bounds_are_in_the_prompt_and_say_characters"
RELEASE = "test_the_release_moved_with_this_teaching"
STAMP = "test_the_package_stamp_moves_with_its_version"
BOUNDARY = "test_every_side_of_the_model_boundary_takes_the_same_keywords"

#: ⚠️ 把那个键从请求里拿掉，响的**只有空请求基线那一条**——
#: `floor` 与 `overhead` 那两条都不响（实测）。它们量的是别的东西，
#: **预判时我把这三条当成一族，是错的。**
EMPTY_REQUEST = ("test_the_empty_request_baseline_is_measured_from_the_real_payload",)

#: 生成步的键集写了**三处** —— 定义点、消费侧，外加重放脚本自己拼的那一份。
KEY_SETS = (
    "test_the_generate_prompt_has_exactly_these_keys",
    "test_the_generate_prompt_has_exactly_these_keys_at_the_definition_point",
    "test_the_replay_script_sends_the_same_generate_prompt_keys_as_production",
)

#: **五处** release pin —— 三条 pin、golden 那一条，加上隔壁那批的同名条。
RELEASE_PINS = (
    "test_the_prompt_now_shows_a_payload_carrying_content",
    "test_the_prompt_release_moved_with_this_change",
    "test_prompt_release_moved_with_the_prompt",
    "test_prompt_release_and_v4_replay_golden_move_together",
    # ⚠️ 隔壁那批（一条说一件事）的同名条 —— **本文件那一条因此改了名**，
    # 否则探针只拿得到函数名，分不出红的是哪一个。
    "test_the_release_moved_with_the_prompt",
)

#: 闸本身的两条 —— 它们量的是闸的行为，本文件量的是「提示词教没教」。
GATE_BEHAVIOUR = (
    "test_the_payload_needs_both_halves",
    "test_the_payload_refusal_is_bounded_and_counts_characters",
)

TEACHING = (
    "- `rememberable_fields` is what this merchant lets you remember about the\n"
    "  customer, one line each saying what it holds. To remember something, propose\n"
    "  a `remember` intent whose payload is exactly\n"
    '  `{"field_key": "<one of those keys>", "field_value": "<what he said>"}` --\n'
    "  both non-empty strings, the key at most 128 characters and the value at most\n"
    "  1024, **counted in characters**. Use a key from that list and no other; an\n"
    "  invented key is dropped and the fact is lost. Remember only what the\n"
    "  customer said himself, in this turn.\n"
)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str] | str]] = [
        (
            "A · 把提示词退回事故那天（那几行整条删掉）",
            [Mutation(MODEL, TEACHING, "")],
            [TAUGHT, BOUNDS, FROM_LIST],
            PROMPT_BYTES + BUDGET_FIT,
        ),
        (
            "B · 只删「键从那张表里取」那半句",
            [
                Mutation(
                    MODEL,
                    " Use a key from that list and no other; an\n"
                    "  invented key is dropped and the fact is lost.",
                    "",
                )
            ],
            [FROM_LIST],
            PROMPT_BYTES,
        ),
        (
            "C · 那张表不再下发（写了没人读）",
            [
                Mutation(
                    SHAPE,
                    '        "rememberable_fields": memory_fields_wanted(rememberable_fields),\n',
                    "",
                )
            ],
            [TOLD, TTL],
            KEY_SETS + EMPTY_REQUEST,
        ),
        (
            "D · 量纲写成 bytes",
            [Mutation(MODEL, "**counted in characters**", "**counted in bytes**")],
            [BOUNDS],
            PROMPT_BYTES,
        ),
        (
            "E · 把 ttl_days 也发出去",
            [
                Mutation(
                    SHAPE,
                    '{"key": str(field["key"]), "description": str(field.get("description", ""))}',
                    "dict(field)",
                )
            ],
            [TTL],
            (),
        ),
        (
            "F · 闸多要一个键而表没跟上",
            [
                Mutation(
                    PIPELINE,
                    'for name in ("field_key", "field_value")',
                    'for name in ("field_key", "field_value", "source_note")',
                )
            ],
            [GATES],
            GATE_BEHAVIOUR,
        ),
        (
            "G · 抽掉一个字段的描述",
            [
                Mutation(
                    PACKAGE,
                    ',\n      "description": "他这家公司做哪一行。"',
                    "",
                )
            ],
            # ⚠️ **STAMP 不在这里，而那是对的**：它读的是**文件里存着的**戳，
            # 删掉一句描述并不会动它 —— 先红的是封版检查（在那一片里）。
            # 两条各管一段：不重封版 → 封版检查红；重封了版 → STAMP 红。
            [TOLD, DECLARED],
            SEAL_BLAST,
        ),
        (
            "H · 版本号不跟着走",
            [
                Mutation(
                    MODEL,
                    'PROMPT_RELEASE = "m1a-prompt-v11"',
                    'PROMPT_RELEASE = "m1a-prompt-v9"',
                )
            ],
            [RELEASE],
            RELEASE_PINS,
        ),
        (
            "I · 描述在下发路上被抹成空串（配包一个字没动）",
            [Mutation(SHAPE, 'str(field.get("description", ""))', '""')],
            [TOLD],
            (),
        ),
        (
            "J · 版本号退回 1.3.0，而内容一个字没动",
            [
                Mutation(PACKAGE, '"version": "1.3.1"', '"version": "1.3.0"'),
                Mutation(PACKAGE, "000100030001", "000100030000"),
            ],
            [STAMP],
            # 改配包 = 掀封版戳，和 G 一样。
            SEAL_BLAST,
        ),
        (
            "K · 替身收不下那个参数（生产加了，端口与替身没跟上）",
            [
                Mutation(
                    PORT,
                    "        rememberable_fields: Sequence[Mapping[str, Any]] = (),  "
                    "# noqa: ARG002 - 替身不读上下文\n",
                    "",
                )
            ],
            [BOUNDARY],
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
            if collateral == SEAL_BLAST:
                # **按性质断言，不按名单**：封版那一条在里面，且红的是一片。
                collateral_hit = SEAL_TEST in control_names and len(control_names) > 30
            else:
                collateral_hit = set(control_names) == set(collateral)
            verdicts.append((label, result.went_red, hit, collateral_hit))
            print(result)
            if not hit:
                print(
                    f"    ⚠️ 本文件：多红 {sorted(set(names) - set(expects))}；"
                    f"没红 {sorted(set(expects) - set(names))}"
                )
            if not collateral_hit and collateral == SEAL_BLAST:
                print(
                    f"    ⚠️ 连带：封版那一条在不在 = {SEAL_TEST in control_names}，"
                    f"红了 {len(control_names)} 条（要 > 30）"
                )
            elif not collateral_hit:
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
