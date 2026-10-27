#!/usr/bin/env python
"""反向探针：上传结果读法的四道闸（`tests/contracts/test_upload_result_reading.py`）。

## 这四道闸守的是「读错了不报错」

读回调这一步没有一处会因为读错而抛：读不到就是空手而归，读多了就是多写一列。
症状全都出现在几天后 —— 目录里那一行停在「传了但没回来」，或者齐得可疑。

    python scripts/probe_upload_claim.py

| | 变异 | 演的是 |
|---|---|---|
| A | 直接伸手进 `msgData` | **T201 那个形状**：数组那一种上空手而归 |
| B | 不再要求 `fileId` | 那一行从「没传过」变成「传过但发不了」 |
| C | 把 `None` 也写进去 | 一次部分回调把已有的值抹掉 |
| D | 顺手把小写 `fileAeskey` 也认了 | 把客户媒体的密钥记成商家资料的密钥 |

**A 是最容易被真的做出来的那一个**：`item["msgData"]` 比
`message_sources(item)` 短、也更"显然"，而它在映射那一种上跑得好好的 ——
**数组那一种不会报错，只会什么都没有**。

## 连库那七条不在这支里

`tests/integration/test_ingress_claims_uploads.py` 要数据库（其中三条验的是
`067` 那两条授权本身）。它们的变异验证随整格门禁做，记在 evidence 里。
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
TARGET = "tests/contracts/test_upload_result_reading.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 11
CONTROL_BASELINE_PASSED = 1761

CLAIM = (
    ROOT / "services/ingress/src/fastagent_v2/ingress/wecom_proxy/upload_claim.py"
)
TOUCHED = (CLAIM,)

#: A —— 伸手进 `msgData`。用一个临时类冒充 `_Source`，语法上完全成立，
#: 而它正是「直接读那个字段」会写出来的东西。
THROUGH_SOURCES = "    for source in message_sources(item):"
STRAIGHT_AT_MSGDATA = (
    '    for source in [type("S", (), {"fields": item.get("msgData") or {}})()]:'
)

REQUIRE_FILE_ID = '    if "provider_file_id" not in found:'
REQUIRE_NOTHING = "    if False:"

SKIP_NONE = (
    "            value = source.fields.get(field)\n"
    "            if value is None or (isinstance(value, str) and not value.strip()):\n"
    "                continue"
)
WRITE_NONE = (
    "            value = source.fields.get(field)\n"
    "            if False:\n"
    "                continue"
)

ASYNC_SPELLING = '    ("provider_file_aes_key", "fileAesKey"),'
BOTH_SPELLINGS = (
    '    ("provider_file_aes_key", "fileAesKey"),\n'
    '    ("provider_file_aes_key", "fileAeskey"),'
)


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 直接伸手进 `msgData`（不穿 `message_sources`）",
            [Mutation(CLAIM, THROUGH_SOURCES, STRAIGHT_AT_MSGDATA)],
            ["test_the_result_is_read_out_of_either_msgdata_shape"],
            [],
        ),
        (
            "B · 不再要求 `fileId`",
            [Mutation(CLAIM, REQUIRE_FILE_ID, REQUIRE_NOTHING)],
            ["test_a_result_without_a_file_id_is_not_a_result"],
            [],
        ),
        (
            "C · 把 `None` 也写进去",
            [Mutation(CLAIM, SKIP_NONE, WRITE_NONE)],
            [
                "test_a_partial_result_leaves_the_missing_columns_alone",
                "test_the_lowercase_spelling_is_not_read_here_and_that_is_deliberate",
                # **这一条是实测加上的，而它指出一层真实的耦合。**
                #
                # 第一版声称这里红的是 `..._unusable_size_...`，探针报「红错了」。
                # 它是对的，而两边都值得写下来：
                #
                # * **`unusable_size` 不红**：`""` / `"not-a-number"` 走到数字那一支
                #   仍然被 `int()` 挡下，`0` / `-5` 被 `<= 0` 挡下 —— 那一列照样不进字典；
                # * **`without_a_file_id` 反而红了**：`None` 一旦被写进去，
                #   `provider_file_id` 就"存在"了，于是「没有 fileId 就回 None」
                #   那道闸**跟着失效**。
                #
                # 也就是说那道闸**依赖着这一道**。两道闸之间的这种依赖，
                # 只有把变异真跑一遍才看得见 —— 读代码时它长得像两件独立的事。
                "test_a_result_without_a_file_id_is_not_a_result",
            ],
            [],
        ),
        (
            "D · 顺手把小写 `fileAeskey` 也认了",
            [Mutation(CLAIM, ASYNC_SPELLING, BOTH_SPELLINGS)],
            ["test_the_lowercase_spelling_is_not_read_here_and_that_is_deliberate"],
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
