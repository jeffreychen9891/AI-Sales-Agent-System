#!/usr/bin/env python
"""反向探针：A6 那两条守卫，逐条验它会红。

## 它与别的探针不一样的地方：**它跑在门禁的栈里**

被验的两条用例是**连库的 e2e**，而 `control_runner` 的 docstring 写着
`tests/integration` 与 `tests/e2e` 要数据库、默认 scope 才取 `tests/contracts`，
并且明说「**探针的守卫在那一层之外时，自己传 `scope` 并说明，不许一个都不传**」。

照办：`scope="tests/e2e"`。而栈与那一整套环境变量由 **`ci.sh` 自己**提供 ——
跑法是把 `ci.sh` 的 pytest 那一行换成跑本文件，于是本文件（以及它拉起的每个
pytest 子进程，`suite_runner` 用的是继承环境的 `subprocess.run`）都在门禁
自己的栈里。**不自己拼那套环境变量**：那正是「证据取自与真实不同的环境」
那一族，本窗口今天已经在一个 scratchpad 副本上踩过一次
（`repo_root` 被推成了 scratchpad，横幅 `not a git checkout`）。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **B1** | `if kept.rowcount == 1:` -> `if True:`（回落那条写永不执行 = 修复前） | A6 那两条 |
| **B2** | 回落那条去掉 `status = 'pending'`，只留 `lease_owner IS NULL` | **量出来的** |
| **B3** | 回落那条去掉 `lease_owner IS NULL`，只留 `status = 'pending'` | **量出来的** |
| **B4** | `if kept.rowcount == 1:` -> `if False:`（两条写都跑 = 重复计数） | 既有那条毒事件用例 |

**B2 / B3 的期望留空是有意的，不是没想。** 两个子句对「跨 worker 抢占」
那条路各自都够用（被抢的行是 `processing` **且**有主），所以**单独删掉任何
一个，抢占那条用例都不会红** —— 这是读代码就能推出来的，而推出来的结论
在这个仓被实跑推翻过两次。**所以让探针去量，量到什么写什么。**
它们要是绿的，那说明这两个子句今天各自没有用例钉住，**而绿的那一支才是产出**。

## 跑法（**直接 `python scripts/probe_...py` 跑不起来：要栈**）

把 `ci.sh` 的 pytest 那一行换成跑本文件，生成的脚本**必须放在 `scripts/` 下** ——
`ci.sh` 的 `repo_root` 是从 `$0` 的 dirname 推的，放别处它会把 repo_root
指成别处（实测横幅变成 `gate tree: /tmp/...` + `not a git checkout`）。

```sh
python - <<'EOF'
from pathlib import Path
src = Path('scripts/ci.sh').read_text(encoding='utf-8')
cut = src.split('"$python_bin" -m ruff check')[0]
out = cut.replace(
    '"$python_bin" -m pytest\n',
    '"$python_bin" "$repo_root/scripts/probe_a_prepare_failure_is_counted.py"\n',
)
Path('scripts/_probe_runner.sh').write_text(out, encoding='utf-8', newline='')
EOF
FASTAGENT_PYTHON=.venv/Scripts/python.exe sh scripts/_probe_runner.sh    # ⚠️ 这个变量不能省
rm scripts/_probe_runner.sh
```

⚠️ **`FASTAGENT_PYTHON` 省掉会得到「四行输出 + `EXIT=49`」** —— 那是
`python3` 那个应用商店桩，什么都不打印就退出，与树脏不脏无关（`AGENTS.md` 有这条）。

## 实测（2026-09-22，基线 `fc807214`）

跑了**两次**，因为第一次 B1 的对照多红一条
`test_an_exit_signal_given_under_takeover_reaches_the_first_plan_after_resume`，
而 B1 的变异**就是 `main` 今天的行为**（门禁在 `main` 上绿）。
第二次 112 全绿，**没有复现 —— 判为抖动**，两次都记在这里而不是只留下好看的那次。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8，不要求调用方 export `PYTHONIOENCODING`。**
#: 这支探针整篇打印中文，而 Windows 控制台默认 cp1252 —— 在门禁的环境里
#: （没有那个变量）它会在第一句上 `UnicodeEncodeError`，实测过一次。
#: `AGENTS.md`：「一个要靠调用方设环境变量才能工作的工具，**修工具本身**」，
#: 先例是 `check_material_dimensions.py`。
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
TARGET = "tests/e2e/test_retry_budget.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 16
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 112
CONTROL_BASELINE_SKIPPED = 0

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
TOUCHED = (PIPELINE,)

EARLY_RETURN = "if kept.rowcount == 1:"
TWO_CLAUSE = "AND status = 'pending'\n                   AND lease_owner IS NULL"

COUNTED = "test_a_failure_inside_prepare_is_counted_and_eventually_retired"
UNOWNED = "test_the_unowned_write_does_not_fire_when_the_lease_was_merely_lost"
POISONED = "test_a_poisoned_inbox_event_stops_being_retried_once_the_budget_runs_out"

run = suite_runner(ROOT, PYTHON, [TARGET])
#: **`tests/e2e` 减去本文件**，不是 `tests/contracts` —— 被验的守卫在这一层。
control = control_runner(ROOT, PYTHON, [TARGET], scope="tests/e2e")


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "B1 · 回落那条写永不执行（= 修复前）—— **本批兑现物**",
            [Mutation(PIPELINE, EARLY_RETURN, "if True:")],
            [COUNTED, UNOWNED],
            [],
        ),
        (
            "B2 · 回落那条只留 `lease_owner IS NULL`（去掉 status 子句）",
            [Mutation(PIPELINE, TWO_CLAUSE, "AND lease_owner IS NULL")],
            [],
            [],
        ),
        (
            "B3 · 回落那条只留 `status = 'pending'`（去掉 lease_owner 子句）",
            [Mutation(PIPELINE, TWO_CLAUSE, "AND status = 'pending'")],
            [],
            [],
        ),
        (
            "B4 · 两条写都跑（去掉那个早返回）—— 一次失败记两次",
            [Mutation(PIPELINE, EARLY_RETURN, "if False:")],
            [POISONED],
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
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            # **两边都按集合相等判**，不是「包含」。
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(
                f"{label}\n  {result.output}\n  红的是那一条：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    aimed = all(hit and other for _, _, hit, other in verdicts)
    print("ALL AIMED" if aimed else "SOMETHING WENT RED SOMEWHERE ELSE (or did not go red)")
    return 0 if aimed else 1


if __name__ == "__main__":
    raise SystemExit(main())
