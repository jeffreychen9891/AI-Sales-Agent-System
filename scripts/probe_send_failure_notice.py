#!/usr/bin/env python
"""反向探针：发送失败那一类分得出来（`tests/contracts/test_wecom_proxy_callback_envelope.py`）。

    python scripts/probe_send_failure_notice.py

| | 变异 | 演的是 |
|---|---|---|
| A | 只按后缀查 | **本仓注释里那句错话的直接后果**：`spam_block_` 那一整类漏掉 |
| B | 只按前缀查 | 反过来，`_blacklist` 那一类漏掉 |
| C | 一律回真 | 每一条灰字提示都被记成发送失败 |
| D | 那一支整个不返回 | 回到这一批之前：两类共用一个原因码 |

**C 是最容易被当成小事的一支**：它让「发送失败」这个计数涨得比真实情况多，
而那个计数正是用来判断**要不要有人去看**的。
一个永远在响的告警与没有告警等价。
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
TARGET = "tests/contracts/test_wecom_proxy_callback_envelope.py"

#: 实测 2026-09-17。
BASELINE_PASSED = 102
CONTROL_BASELINE_PASSED = 1700

CALLBACK = ROOT / "services/ingress/src/fastagent_v2/ingress/wecom_proxy/callback.py"
TOUCHED = (CALLBACK,)

SUFFIX_ON = '        if marker.startswith("_") and external_event_id.endswith(marker):'
SUFFIX_OFF = "        if False:"

PREFIX_ON = '        if marker.endswith("_") and external_event_id.startswith(marker):'
PREFIX_OFF = "        if True:"

RETURN_NONE = "            return OUTCOME_IGNORED, IGNORED_SEND_FAILED"
RETURN_HINT = "            pass"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 只按前缀查（后缀那一类漏掉）",
            [Mutation(CALLBACK, SUFFIX_ON, SUFFIX_OFF)],
            [
                "test_both_documented_failure_shapes_are_recognised_one_prefix_one_suffix",
                # **实测加上的**：那一条也在守同一件事 —— 09-17 给它补的
                # 「不带标记的 1011 照旧是灰字提示」那半，正好是这三个变异
                # 各自打破的东西。两条一起红说明补那一半补对了。
                "test_a_failed_send_notice_is_not_customer_input",
            ],
            [],
        ),
        (
            "C · 一律回真（灰字提示全被记成发送失败）",
            [Mutation(CALLBACK, PREFIX_ON, PREFIX_OFF)],
            [
                # **四条，而我第一版声称一条。**
                #
                # 「不带标记的 1011 仍然是灰字提示」这件事**被钉在四个地方** ——
                # 比我以为的多两处，而多出来那两处是这一批之前就有的
                # （它们各自造的 1011 样本都不带标记）。
                #
                # 这说明本仓对「1011 那个桶」的既有覆盖比我读到的厚，
                # **而一律回真会把那四条一起打翻** —— 正是这个变异要演的东西。
                "test_both_documented_failure_shapes_are_recognised_one_prefix_one_suffix",
                "test_a_failed_send_notice_from_our_own_sender_id_is_still_a_failure_notice",
                "test_the_failed_send_notice_is_decided_before_the_cmd_is_read",
                # **实测加上的**：那一条也在守同一件事 —— 09-17 给它补的
                # 「不带标记的 1011 照旧是灰字提示」那半，正好是这三个变异
                # 各自打破的东西。两条一起红说明补那一半补对了。
                "test_a_failed_send_notice_is_not_customer_input",
            ],
            [],
        ),
        (
            "D · 那一支不再返回（回到这一批之前）",
            [Mutation(CALLBACK, RETURN_NONE, RETURN_HINT)],
            [
                "test_both_documented_failure_shapes_are_recognised_one_prefix_one_suffix",
                # **实测加上的**：那一条也在守同一件事 —— 09-17 给它补的
                # 「不带标记的 1011 照旧是灰字提示」那半，正好是这三个变异
                # 各自打破的东西。两条一起红说明补那一半补对了。
                "test_a_failed_send_notice_is_not_customer_input",
            ],
            [],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(passed=BASELINE_PASSED, control_passed=CONTROL_BASELINE_PASSED),
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
    return 1 if silent or misaimed or drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
