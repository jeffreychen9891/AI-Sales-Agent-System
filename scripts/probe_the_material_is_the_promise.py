#!/usr/bin/env python
"""反向探针：缺陷 21/22 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **K1** | bridge 素材换回旧承诺文（连 hash）| 桥后有路那条 |
| **K2** | price-flat 去掉 1888、换回悬空指代（连 hash）| 自含那条 |
| **K3** | 契约把 evidence_span_hash 加回 properties（不加 required）| 「字段回不来」那条 |
| **K4** | 提示词把 64 位 hex 规则句加回 | 提示词那条 |

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_the_material_is_the_promise.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
⚠️ **materials.json 也进 `content_hash`** —— 素材变异连 profile.json 的
stamp 一起改（缺陷 18 那支量过不改的代价：91 条无差别红）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8**，不要求调用方 export `PYTHONIOENCODING`。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_the_material_is_the_promise.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 7
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2541
CONTROL_BASELINE_SKIPPED = 53

MATERIALS = ROOT / "profiles/internal-demo-sales/materials.json"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
CONTRACT = ROOT / "packages/contracts/inference/understanding-result.schema.json"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
TOUCHED = (MATERIALS, PROFILE, CONTRACT, MODEL)

NEW_BRIDGE = (
    "您说的这件它是这么接的：消息一来先回上去，晚上和忙的时候也一样，不让客户等；"
    "您或同事回头接手，前面聊了什么都在，不用客户再讲一遍；该跟进的到点主动跟一句。"
    "说出去的话全是您审过的那套，没审过的发不出去。您看这套对得上您那边的情况吗？"
)
OLD_BRIDGE = "您说的这件，正好是它接得住的那一类。我说说它具体怎么接，您看对不对得上。"

NEW_FLAT = "费用是包月的，一个月 1888，不按消息条数走——量涨上去它也不跟着涨。"
OLD_FLAT = "这个数不按消息条数走——量涨上去它也不跟着涨。"

NO_HASH_PROPS = '''        "required": [
          "key",
          "typed_value",
          "confidence"
        ],
        "properties": {
          "key": {'''
WITH_HASH_PROPS = '''        "required": [
          "key",
          "typed_value",
          "confidence"
        ],
        "properties": {
          "evidence_span_hash": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          },
          "key": {'''

NO_RULE = '''Every facts item must contain only key, typed_value and confidence. Every'''
WITH_RULE = '''Every facts item must contain only key, typed_value, confidence and a 64-character
lowercase hexadecimal evidence_span_hash. Every'''

BRIDGE_OK = "test_the_bridge_material_carries_content_beyond_its_promise"
SELF_CONTAINED = "test_the_price_materials_are_self_contained"
NO_RECEIPT = "test_the_negative_close_does_not_open_with_a_receipt"
CHECKLIST = "test_the_promised_checklist_actually_exists"
VALID_NOW = "test_a_fact_without_the_hash_is_valid_now"
REFUSED = "test_a_fact_carrying_the_hash_is_refused"
PROMPT_CLEAN = "test_the_prompt_no_longer_asks_for_a_hash"

#: 连带 —— 全是现量现比的守卫红在自己的职责上（量出来的，预测写的是空）。
STAMP_TEST = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_STAMP = "test_the_runbook_quotes_the_stamp_in_the_package"
LENGTHS_TABLE = "test_the_measured_numbers_in_this_file_are_still_the_packages_numbers"
LENGTHS_PROMPT = "test_the_merchants_own_lengths_are_offered_as_a_size_not_a_rule"
FLOOR = "test_the_floor_equals_the_request_it_claims_to_measure"
OVERHEAD = "test_the_overhead_constants_are_measured_from_the_real_payload"
SPELLED_ONCE = "test_the_platform_prompt_sizes_are_spelled_once_per_package"
BYTES_MOVED = (FLOOR, OVERHEAD, SPELLED_ONCE)

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    import json
    import subprocess

    def mutated_stamp(old: str, new: str) -> str:
        """素材变异后的 content_hash 现算 —— materials.json 也进 stamp。"""

        code = (
            "import sys,json;"
            "sys.path[:0]=['services/api/src','services/worker/src','packages/runtime/src'];"
            "from fastagent_v2.profiles.registry import _merge_material_texts, canonical_hash;"
            "import pathlib;"
            "d=json.loads(pathlib.Path('profiles/internal-demo-sales/profile.json')"
            ".read_text(encoding='utf-8'));"
            "d['identity'].pop('content_hash');"
            "_merge_material_texts(d, pathlib.Path('profiles/internal-demo-sales/profile.json'));"
            f"key=[k for k,v in d['content_material_texts'].items() if v=={old!r}];"
            "assert len(key)==1, key;"
            f"d['content_material_texts'][key[0]]={new!r};"
            "print(canonical_hash(d))"
        )
        out = subprocess.run(
            [str(PYTHON), "-c", code], capture_output=True, cwd=ROOT, check=True
        )
        return out.stdout.decode().strip()

    current_stamp = json.loads(PROFILE.read_text(encoding="utf-8"))["identity"][
        "content_hash"
    ]

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "K1 · bridge 换回旧承诺文（= 修复前，连 hash）",
            [
                Mutation(MATERIALS, NEW_BRIDGE, OLD_BRIDGE),
                Mutation(PROFILE, current_stamp, mutated_stamp(NEW_BRIDGE, OLD_BRIDGE)),
            ],
            [BRIDGE_OK],
            #: bridge 115 字回 36，最长素材回到 86 —— 词长两条 + 钉戳两条。
            [LENGTHS_TABLE, LENGTHS_PROMPT, STAMP_TEST, RUNBOOK_STAMP],
        ),
        (
            "K2 · price-flat 换回悬空指代（连 hash）",
            [
                Mutation(MATERIALS, NEW_FLAT, OLD_FLAT),
                Mutation(PROFILE, current_stamp, mutated_stamp(NEW_FLAT, OLD_FLAT)),
            ],
            [SELF_CONTAINED],
            [STAMP_TEST, RUNBOOK_STAMP],
        ),
        (
            "K3 · 契约把字段加回 properties",
            [Mutation(CONTRACT, NO_HASH_PROPS, WITH_HASH_PROPS)],
            [REFUSED],
            #: 响应 schema 变大，三条字节守卫红 —— 职责使然。
            BYTES_MOVED,
        ),
        (
            "K4 · 提示词把 64 位 hex 规则句加回",
            [Mutation(MODEL, NO_RULE, WITH_RULE)],
            [PROMPT_CLEAN],
            BYTES_MOVED,
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                skipped=BASELINE_SKIPPED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            # **两边都按集合相等判**，不是「包含」。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    aimed = all(hit and other for _, hit, other in verdicts)
    print("ALL AIMED" if aimed else "SOMETHING WENT RED SOMEWHERE ELSE (or did not go red)")
    return 0 if aimed else 1


if __name__ == "__main__":
    raise SystemExit(main())
