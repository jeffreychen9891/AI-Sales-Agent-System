#!/usr/bin/env python
"""反向探针：发文件那条路的四道闸（`tests/contracts/test_sending_a_file.py`）。

    python scripts/probe_sending_a_file.py

| | 变异 | 演的是 |
|---|---|---|
| A | 条件必填那道闸拆掉 | 小文件没密钥照发 —— **供应商回成功，收件人打不开** |
| B | 「大」改成按字节数判 | 立第二个「大」的定义，两个定义迟早不一致 |
| C | 大文件那一档也送一个空密钥 | 抓包的人把 `""` 当成真值 |
| D | 适配器没接上时改成直接调 | `AttributeError` 打穿派发循环 |

**A 是这条路上最贵的一个**：它不报错、不失败、供应商回成功 ——
唯一的现场证据是客户说「打不开」。

## 连库那六条不在这支里

`tests/integration/test_sending_a_material.py`（版本解析 + `068` 的边界）要数据库。
它们的变异验证随整格门禁做，记在 evidence 里。
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
TARGET = "tests/contracts/test_sending_a_file.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 14
CONTROL_BASELINE_PASSED = 1773

CLIENT = ROOT / "packages/channels/src/fastagent_v2/channels/wecom_proxy/client.py"
ADAPTER = ROOT / "services/worker/src/fastagent_v2/worker/wecom_proxy_channel.py"
TOUCHED = (CLIENT, ADAPTER)

REFUSE_NO_KEY = "        if not key and not is_big_file(file_id):"
ALLOW_NO_KEY = "        if False:"

BY_SHAPE = '    return file_id.startswith(BIG_FILE_ID_PREFIX)'
BY_SIZE = '    return len(file_id) > 40  # 「大」改成按长度猜'

ONLY_WHEN_PRESENT = '        if key:\n            # **Only when there is one.**'
ALWAYS_SEND = '        if True:\n            # **Only when there is one.**'

WIRED_CHECK = "            if self._send_file is None:"
NO_CHECK = "            if False:"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 条件必填那道闸拆掉",
            [Mutation(CLIENT, REFUSE_NO_KEY, ALLOW_NO_KEY)],
            ["test_a_small_file_without_a_key_is_refused_before_anything_is_sent"],
            [],
        ),
        (
            "B · 「大」改成按长度猜",
            [Mutation(CLIENT, BY_SHAPE, BY_SIZE)],
            [
                "test_big_is_decided_by_the_file_id_and_nothing_else",
                "test_a_big_file_may_go_without_a_key_and_the_field_is_then_absent",
            ],
            [],
        ),
        (
            "C · 大文件那一档也送一个空密钥",
            [Mutation(CLIENT, ONLY_WHEN_PRESENT, ALWAYS_SEND)],
            ["test_a_big_file_may_go_without_a_key_and_the_field_is_then_absent"],
            [],
        ),
        (
            "D · 适配器不再检查文件那条路接没接上",
            [Mutation(ADAPTER, WIRED_CHECK, NO_CHECK)],
            ["test_an_adapter_without_the_file_path_answers_with_a_receipt_not_a_crash"],
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
