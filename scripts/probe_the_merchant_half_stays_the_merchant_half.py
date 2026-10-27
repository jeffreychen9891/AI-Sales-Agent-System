#!/usr/bin/env python
"""反向探针：T373 那一批 —— `tests/contracts/test_profile_release_projection.py`。

本批的主张有五条，逐条要有一支探针证明**它真的会红**：

1. 投影是**白名单**，不是黑名单 —— 换成黑名单要红；
2. 往投影里加一个「明确不给」的键要红；
3. 那四项在**任何一层**出现都要红（不只是顶层）；
4. schema 长出一个没分类的顶层键要红；
5. `identity` 长出第五个字段要红 —— 它是摊平的前提。

外加两条钉可选键的：投影恒回空值要红（证明「带出去」那条不空转），
可选键缺席时抛 `KeyError` 要红（证明「缺席给空值」那条不空转）。

⚠️ **带 `control=`**：每支变异同时跑一遍「`tests/contracts` 减去本文件」，
把连带**量出来**而不是想出来。

    python scripts/probe_the_merchant_half_stays_the_merchant_half.py
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
# ⚠️ **不是 `ROOT / ".venv"`**，而既有探针都那么写。
# 本仓有多棵 worktree，而 `.venv` 只在其中一棵里 —— 在别的树上那条路径
# 指向一个不存在的文件，`subprocess` 抛 `FileNotFoundError`，
# 而那发生在 baseline 之前，**看起来像探针坏了而不是树不对**（实测 2026-09-20）。
# `sys.executable` 就是正在跑这个脚本的那个解释器，永远存在，
# 且必然是调用者选的那一个。
PYTHON = Path(sys.executable)
TARGET = "tests/contracts/test_profile_release_projection.py"

#: 本文件自己那几条。**报数，不报颜色**（harness 规则四）。
BASELINE_PASSED = 8

#: `tests/contracts` 减去本文件之后的那两个数。
CONTROL_BASELINE_PASSED = 2294
CONTROL_BASELINE_SKIPPED = 45

APP = ROOT / "services" / "api" / "src" / "fastagent_v2" / "api" / "app.py"
SCHEMA = ROOT / "packages" / "contracts" / "schemas" / "merchant-profile.schema.json"

TOUCHED = (APP, SCHEMA)

#: 白名单那一段 —— 探针 A 把它整个换成黑名单过滤。
ALLOWLIST = '''        "target_frames": document["target_frames"],
        "proactivity_policy": document["proactivity_policy"],'''

#: 黑名单版：**看起来完全合理**，四项一个不漏地被排除掉了，
#: 而它会把五个「还没裁」的键连同将来任何新键一起发出去。
DENYLIST = '''        **{
            key: value
            for key, value in document.items()
            if key
            not in {
                "identity",
                "policy_bindings",
                "confidence_gate_policy_ref",
                "manipulation_context_refs",
                "golden_manifest",
            }
        },'''

#: 探针 B 插在这一行前面：多给一个明确不给的键。
ONE_MORE_KEY_ANCHOR = '        "target_frames": document["target_frames"],\n'
ONE_MORE_KEY = '        "golden_manifest": document["golden_manifest"],\n'

#: 探针 C：那四项**藏进嵌套层**。顶层键集合一个字不变，
#: 所以只看顶层的断言会继续绿着。
NESTED_LEAK = (
    '        "proactivity_policy": {\n'
    '            **document["proactivity_policy"],\n'
    '            "policy_bindings": document["policy_bindings"],\n'
    "        },\n"
)
NESTED_ANCHOR = '        "proactivity_policy": document["proactivity_policy"],\n'

#: 探针 D：schema 长出第 16 个顶层键，而没有人给它分类。
NEW_SCHEMA_KEY_ANCHOR = '    "identity": {'
NEW_SCHEMA_KEY = '    "escalation_policy": {\n      "type": "object"\n    },\n    "identity": {'

#: 探针 E：`identity` 长出第五个字段 —— 摊平的前提没了。
IDENTITY_ANCHOR = '''        "content_hash": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        }'''
IDENTITY_PLUS_ONE = '''        "content_hash": {
          "type": "string",
          "pattern": "^[0-9a-f]{64}$"
        },
        "published_by": {
          "type": "string"
        }'''

#: 探针 F：`memory_fields` 恒回空 —— 钉「有的时候带出去」那一条。
ALWAYS_EMPTY = '        "memory_fields": document.get("memory_fields", []),\n'
ALWAYS_EMPTY_NEW = '        "memory_fields": [],\n'

#: 探针 G：可选键当必填读 —— 钉「缺席时给空值」那一条。
OPTIONAL_AS_REQUIRED = (
    '        "content_material_texts": document.get("content_material_texts", {}),\n'
)
OPTIONAL_AS_REQUIRED_NEW = (
    '        "content_material_texts": document["content_material_texts"],\n'
)

_FAILED = re.compile(r"'([\w\[\]\-]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 把白名单换成黑名单过滤（那四项一个不漏，却放行了没裁的五个）",
            [Mutation(APP, ALLOWLIST, DENYLIST + "\n" + ALLOWLIST)],
            [
                "test_the_projection_gives_exactly_the_model_fields",
                "test_no_withheld_key_appears_anywhere_in_the_projection",
            ],
            # **实测出来的连带，不是想出来的。** 两条，各说明一件事：
            #
            # * `test_the_model_names_exactly_the_keys_the_handler_returns`
            #   是 ADR-0142 那条 AST 判据 —— 它**独立地**也抓到了这次变异。
            #   两条互补而非重复：那一条比的是「模型说的键 == handler 返回的键」，
            #   本文件比的是「那些键里有没有不该给的」。**A 两条都红，C 只红本文件**
            #   （见 C 的连带：空）—— 差别正是「藏进嵌套层」，
            #   而那恰好是只看顶层的 AST 判据看不见的地方。
            # * `test_every_registered_route_is_one_this_check_can_actually_read`
            #   红，是因为黑名单写法用了 `**{...}` 字典推导 ——
            #   AST 解析器读不出这个 handler 返回什么了。**它红得有道理**：
            #   一个静态判据读不懂的返回，正是它该拒绝的形状。
            [
                "test_every_registered_route_is_one_this_check_can_actually_read",
                "test_the_model_names_exactly_the_keys_the_handler_returns",
            ],
        ),
        (
            "B · 往投影里多给一个明确不给的键（`golden_manifest`）",
            [Mutation(APP, ONE_MORE_KEY_ANCHOR, ONE_MORE_KEY + ONE_MORE_KEY_ANCHOR)],
            [
                "test_the_projection_gives_exactly_the_model_fields",
                "test_no_withheld_key_appears_anywhere_in_the_projection",
            ],
            # 实测一条：ADR-0142 那条 AST 判据也红。多给一个**顶层**键，
            # 它看得见 —— 与 C 对照着读。
            ["test_the_model_names_exactly_the_keys_the_handler_returns"],
        ),
        (
            "C · 把 `policy_bindings` 藏进嵌套层（顶层键集合一个字不变）",
            [Mutation(APP, NESTED_ANCHOR, NESTED_LEAK)],
            ["test_no_withheld_key_appears_anywhere_in_the_projection"],
            # **实测：没有连带 —— 而这一条空正是本文件存在的理由。**
            # A 与 B 那两支里，ADR-0142 的 AST 判据跟着红了；这一支它一声不吭，
            # 因为顶层键集合一个字没变。**只看顶层的判据在这里是空的**，
            # 那四项已经发出去了而没有任何东西知道。
            (),
        ),
        (
            "D · schema 长出第 16 个顶层键，没人给它分类",
            [Mutation(SCHEMA, NEW_SCHEMA_KEY_ANCHOR, NEW_SCHEMA_KEY)],
            ["test_every_top_level_key_is_classified"],
            [],
        ),
        (
            "E · `identity` 长出第五个字段（摊平的前提没了）",
            [Mutation(SCHEMA, IDENTITY_ANCHOR, IDENTITY_PLUS_ONE)],
            ["test_identity_is_exactly_the_four_keys_the_projection_flattens"],
            [],
        ),
        (
            "F · `memory_fields` 恒回空（钉「有的时候原样带出去」那一条）",
            [Mutation(APP, ALWAYS_EMPTY, ALWAYS_EMPTY_NEW)],
            ["test_the_optional_keys_carry_through_when_the_package_has_them"],
            [],
        ),
        (
            "G · 把可选键当必填读（钉「缺席时给空值」那一条）",
            [Mutation(APP, OPTIONAL_AS_REQUIRED, OPTIONAL_AS_REQUIRED_NEW)],
            [
                "test_the_projection_gives_exactly_the_model_fields",
                "test_no_withheld_key_appears_anywhere_in_the_projection",
                "test_the_optional_keys_are_present_and_empty_when_the_package_has_none",
            ],
            [],
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
    ok = True
    for label, went_red, hit, collateral_hit in verdicts:
        mark = "OK " if (went_red and hit and collateral_hit) else "BAD"
        ok = ok and went_red and hit and collateral_hit
        print(f"{mark}  {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
