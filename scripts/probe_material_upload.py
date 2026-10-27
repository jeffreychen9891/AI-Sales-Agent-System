#!/usr/bin/env python
"""反向探针：上传那一次调用的三道闸（`tests/contracts/test_material_upload_call.py`）。

## 这三道闸守的都是同一个形状：**错了不报错**

上传这条路上没有一处会因为写错而当场失败 —— 供应商回 200，
目录里记下一个看上去正常的值，**而错误在几天后发送那一刻才现形**。

    python scripts/probe_material_upload.py

| | 变异 | 演的是 |
|---|---|---|
| A | 请求体里多塞一个字段 | 有人「顺手把 md5 也带上」，schema 不拦 |
| B | 返回 `dict(data)` 而不是那个 id | 把同步响应当成上传结果 |
| C | 空 `requestId` 不再拒 | 存一个空串，这次上传从此认领不回来 |
| D | 绕过 guid 白名单 | 新加操作时最容易漏的一步（**红两条**，见下） |

**D 是最容易被真的做出来的那一个**：`_guarded_guid` 不在方法签名上，
下一个加操作的人照着 `_call` 抄一遍就漏了，**而漏掉它的症状是一次成功的上传**。

## 连库那两条不在这支里

`tests/integration/test_material_upload_claiming.py` 的两条（挑行判据、认领不覆盖）
需要数据库，而 `probe_harness` 的 `scope` 默认 `tests/contracts` —— 理由写在它自己的
docstring 里：integration 的基线没法按数字断言。那两条的变异验证随整格门禁做，
记在 evidence 里。
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
TARGET = "tests/contracts/test_material_upload_call.py"

#: 实测 2026-09-16。
BASELINE_PASSED = 6
CONTROL_BASELINE_PASSED = 1755

CLIENT = (
    ROOT / "packages/channels/src/fastagent_v2/channels/wecom_proxy/client.py"
)
TOUCHED = (CLIENT,)

FOUR_FIELDS = (
    '                "fileType": file_type,\n'
    "            },"
)
FIVE_FIELDS = (
    '                "fileType": file_type,\n'
    '                "fileMd5": "",\n'
    "            },"
)

RETURN_THE_ID = (
    '        self._record("upload_file_by_url", "ok", guid=allowed)\n'
    "        return request_id"
)
RETURN_THE_BODY = (
    '        self._record("upload_file_by_url", "ok", guid=allowed)\n'
    "        return dict(data)  # type: ignore[return-value]"
)

REFUSE_EMPTY = (
    '        if not request_id:\n'
    '            # **Not `_record(..., "ok")` first.**'
)
KEEP_EMPTY = (
    '        if False:\n'
    '            # **Not `_record(..., "ok")` first.**'
)

GUARDED = '        allowed = self._guarded_guid("upload_file_by_url", guid)'
UNGUARDED = "        allowed = guid"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 请求体里多塞一个 `fileMd5`",
            [Mutation(CLIENT, FOUR_FIELDS, FIVE_FIELDS)],
            ["test_the_call_carries_exactly_the_four_fields_the_vendor_declares_required"],
            [],
        ),
        (
            "B · 回整个 `data` 而不是那个 id",
            [Mutation(CLIENT, RETURN_THE_ID, RETURN_THE_BODY)],
            [
                "test_the_call_carries_exactly_the_four_fields_the_vendor_declares_required",
                "test_the_return_value_is_the_request_id_not_the_response_body",
            ],
            [],
        ),
        (
            "C · 空 `requestId` 不再拒",
            [Mutation(CLIENT, REFUSE_EMPTY, KEEP_EMPTY)],
            ["test_an_upload_with_no_request_id_is_refused_not_returned"],
            [],
        ),
        (
            "D · 绕过 guid 白名单",
            [Mutation(CLIENT, GUARDED, UNGUARDED)],
            [
                "test_a_guid_outside_the_allowlist_never_reaches_the_wire",
                # **第二条是实测加上的，不是一开始就声称的。**
                #
                # 第一版只声称上面那一条，探针报「红错了：多红了四字段那条」。
                # 它是对的：`_guarded_guid` 不只是许可，**也是拼写权威** ——
                # 不过它，线上带的就是调用方递进来的原串，而四字段那条钉的是
                # 规范化之后的那个。
                #
                # 所以漏掉这一步有**两个**症状：白名单形同虚设，
                # 以及**发出去的 guid 拼写变了** —— 后者的现场表现是供应商
                # 回一句「找不到这个连接」，而我们这边一切正常。
                "test_the_call_carries_exactly_the_four_fields_the_vendor_declares_required",
            ],
            []
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
