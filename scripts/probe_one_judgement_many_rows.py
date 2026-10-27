#!/usr/bin/env python
"""反向探针：ADR-0152 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **E1** | 不拆（`_segments_to_queue` 恒返回一行）= 修复前 | 铺开那几条 |
| **E2** | 拆了，而每一段沿用原键（不铸序号） | 撞键那条 |
| **E3** | 最后一段也留停顿 | 「最后一段不留停顿」那条 |
| **E4** | 停顿不夹上下限 | 上下限那条 |
| **E5** | **一律拆**（不看有没有模型给的切点） | 反侧那条 |
| **E6** | 就地改原件（不 `deepcopy`） | 「不改原件」那条 |
| **E7** | 步长改成 4（小于段数上界 8） | 步长那条 |
| **E8** | 逐段渲染时不套规范形状 | 「渠道形状」那条 |

## ⚠️ E1 是本批的兑现物，而它验的是**时机**不是**功能**

ADR-0151 就已经会拆了。本批改的是**在哪儿拆** —— 判定之前变判定之后。
所以 E1 把拆整个关掉，验的是「拆还在」；
**而「拆得晚」那一半由一条读源码顺序的用例钉**（见目标文件末尾那条），
因为「判定读到的是整条还是一段」在单元层面看不出来 ——
它是两个函数的调用顺序。

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_one_judgement_many_rows.py
```

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
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
TARGET = "tests/contracts/test_one_judgement_many_rows.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 11
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2480
CONTROL_BASELINE_SKIPPED = 53

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
TOUCHED = (PIPELINE, MODEL)

NO_SPLIT = """    if not isinstance(segments, list) or len(segments) < 2:
        return [(dict(command), key, 0)]"""
NEVER_SPLIT = """    if True:
        return [(dict(command), key, 0)]"""
#: ⚠️ **E5 的变异要真的「一律拆」，而那不是把 `< 2` 改小。**
#: ADR-0151 那一批的 D5 第一版就是那么写的 —— 拆**一段**与不拆结果逐字相同
#: （同样的键、同样的 0 停顿），于是它既不是一律拆，也红不了任何东西。
#: 这一版让**没有切点的 payload** 也走进拆的分支。
ALWAYS_SPLIT = """    if not isinstance(segments, list) and False:
        return [(dict(command), key, 0)]
    segments = segments if isinstance(segments, list) else [{}, {}]"""

MINTED_KEY = '        suffix = "" if index == 0 else f"{SEGMENT_BASIS_INFIX}{index}"'
REUSED_KEY = '        suffix = ""'

LAST_ONE_FREE = "        pace = 0 if index == len(segments) - 1 else _segment_pace_ms(segment)"
LAST_ONE_PAUSED = "        pace = _segment_pace_ms(segment)"

CLAMPED = "    return max(SEGMENT_PACE_FLOOR_MS, min(SEGMENT_PACE_CEILING_MS, paced))"
UNCLAMPED = "    return paced"

COPIES = "        one = deepcopy(dict(command))"
IN_PLACE = "        one = dict(command)"

STRIDE = "SEGMENT_ORDINAL_STRIDE = 100"
NARROW_STRIDE = "SEGMENT_ORDINAL_STRIDE = 4"

CANONICAL = """        one["content"] = {
            "type": TEXT_CONTENT_TYPE,
            "text": customer_text({"segments": [deepcopy(segment)]}, materials=materials),
        }"""
RAW_SHAPE = """        one["content"] = {"segments": [deepcopy(segment)]}"""

ONE_ALONE = "test_one_segment_is_left_alone"
THREE_ROWS = "test_three_segments_become_three_rows_in_order"
SHAPE = "test_every_row_keeps_the_shape_a_channel_can_act_on"
KEYS = "test_the_keys_cannot_collide_and_the_first_one_does_not_move"
LAST_PAUSE = "test_the_last_segment_leaves_no_pause"
BOUNDS = "test_the_pause_is_bounded_at_both_ends"
FLOOR_REF = "test_a_material_ref_segment_takes_the_floor"
PASSTHROUGH = "test_a_payload_without_segments_passes_through"
NO_MUTATION = "test_the_split_does_not_mutate_what_it_was_given"
STRIDE_TEST = "test_the_ordinal_stride_clears_both_contract_bounds"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "E1 · 从不拆（= 修复前）—— **本批兑现物**",
            [Mutation(PIPELINE, NO_SPLIT, NEVER_SPLIT)],
            #: ⚠️ `SHAPE` **不红，量出来的**：不拆时返回的是原样的 `command`，
            #: 而那本来就是规范形状 —— 那条用例守的是「拆出来的每行仍是那一种」，
            #: 一行都没拆时它无话可说。预测时写了它，错了。
            [THREE_ROWS, KEYS, LAST_PAUSE],
            [],
        ),
        (
            "E2 · 拆了，而每一段沿用原键",
            [Mutation(PIPELINE, MINTED_KEY, REUSED_KEY)],
            [KEYS],
            [],
        ),
        (
            "E3 · 最后一段也留停顿",
            [Mutation(PIPELINE, LAST_ONE_FREE, LAST_ONE_PAUSED)],
            [LAST_PAUSE],
            [],
        ),
        (
            "E4 · 停顿不夹上下限",
            [Mutation(PIPELINE, CLAMPED, UNCLAMPED)],
            [BOUNDS],
            [],
        ),
        (
            "E5 · **一律拆**（反侧，与 E1 相反方向）",
            [Mutation(PIPELINE, NO_SPLIT, ALWAYS_SPLIT)],
            #: ⚠️ `ONE_ALONE` 也红，量出来的：一律拆之后**单段那条路也被重渲染**，
            #: 而那条用例断言的是「原样排进去」（`command == COMMAND`）。
            #: **这与 ADR-0151 那批的 D5 不同** —— 那时单段拆与不拆结果逐字相同；
            #: 本批多了一条「单段不重渲染」的断言，于是它分得开了。
            [PASSTHROUGH, ONE_ALONE],
            [],
        ),
        (
            "E6 · 就地改原件，不拷贝",
            [Mutation(PIPELINE, COPIES, IN_PLACE)],
            [],
            [],
        ),
        (
            "E7 · 步长改成 4（小于段数上界 8）",
            [Mutation(PIPELINE, STRIDE, NARROW_STRIDE)],
            [STRIDE_TEST],
            [],
        ),
        (
            "E8 · 逐段渲染时不套规范形状",
            [Mutation(PIPELINE, CANONICAL, RAW_SHAPE)],
            #: ⚠️ `THREE_ROWS` 也红，量出来的：它读的是
            #: `command["content"]["text"]`，而不套规范形状之后那一格不存在。
            [SHAPE, THREE_ROWS],
            [],
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
