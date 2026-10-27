#!/usr/bin/env python
"""反向探针：`tests/contracts/test_policy_hook_shape.py` 那九条断言，逐条验它真的会红。

**这一份守的全是「零」**（两个时点零写者、一个 obligation 零发出、
obligations 列零读者、十二个 hook 名零定义），而
**一条「恒为 0」的断言在扫描器失效时回的也是 0** —— 它会静悄悄通过。
所以这些断言比一般的更需要被证明能红：**绿着的它们和坏掉的它们长得一样。**

## 它比一般探针多报一样东西：**红的是哪一条**

`AGENTS.md`「红了之后，要确认红的是**你要测的那一条**」——
那一节记的三次事故里，三次都「红得很响，而响声来自别处」
（`FileNotFoundError` / 合同层 / 另一道闸）。
本脚本因此不用 `probe_harness.pytest_runner`（它只回最后那行汇总），
而是自带一个 runner，**把 `FAILED` 那几行的用例名逐条读回来**，
再与每支探针声明的 `expects` 比对。**名字对不上就算这支探针没验到它要验的东西。**

## 跑法

    python scripts/probe_policy_hook_shape.py

前置：`git status --porcelain` 干净（否则基线可能带着别人的变异，
见 `probe_harness` 模块 docstring 的失败 4）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import Mutation, ProbeSession  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGETS = (
    "tests/contracts/test_policy_hook_shape.py",
    "tests/contracts/test_policy_output.py",
)

#: 基线必须是这个数。**按数断言，不按颜色** —— 一棵带着别人变异的树会静悄悄全绿
#: （`probe_harness` 失败 4；2026-09-13 实测过一次 `136 passed, 2 skipped`，
#: 而那 2 skipped 是上一支被 kill 的探针留下的）。
BASELINE_PASSED = 23

WORKER = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker"
PIPELINE = WORKER / "pipeline.py"
DIGEST = WORKER / "digest.py"
POLICY_OUTPUT = WORKER / "policy_output.py"

TOUCHED = (PIPELINE, DIGEST, POLICY_OUTPUT)

_FAILED = re.compile(r"^FAILED (?P<target>\S+?)::(?P<name>[\w\[\]\-]+)", re.MULTILINE)


def run() -> tuple[str, bool]:
    """跑一遍，回「FAILED 的用例名 + 汇总行」与「红了没有」。

    **`errors="replace"` 不是装饰**：pytest 会把中文断言文案回显进失败输出，
    而这台机器的控制台是 cp1252 —— 读线程死掉时 `stdout` 回 `None`，
    脚本在**变异之后**崩掉（`probe_harness` 失败 3）。
    """

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            str(PYTHON),
            "-m",
            "pytest",
            *TARGETS,
            "-q",
            "--no-header",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = completed.stdout or ""
    names = sorted({match.group("name") for match in _FAILED.finditer(stdout)})
    lines = [line for line in stdout.splitlines() if line.strip()]
    summary = lines[-1] if lines else "(no output)"
    rendered = f"{summary} | FAILED: {names or '（无）'}"
    return rendered, completed.returncode != 0


def _failed_names(output: str) -> list[str]:
    inside = output.split("FAILED: ", 1)[-1]
    return re.findall(r"'([\w\[\]\-]+)'", inside)


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str]]] = [
        (
            "P1 · 给 post_understanding 造一个写者（B1-8 / B2-4）",
            [Mutation(DIGEST, 'hook="pre_execute"', 'hook="post_understanding"')],
            ["test_only_the_four_wired_hook_points_have_writers"],
        ),
        (
            "P2 · 把一处 hook= 写成非字面量（数不出来时必须红在「数不出来」上）",
            [Mutation(DIGEST, 'hook="pre_execute"', 'hook=str("pre_execute")')],
            [
                "test_every_policy_output_names_its_hook_literally",
                "test_only_the_four_wired_hook_points_have_writers",
            ],
        ),
        (
            "P3 · 发一次 require_evidence（B1-10 的发出那一半）",
            [
                Mutation(
                    PIPELINE,
                    '["audit", "create_manual_task"]',
                    '["audit", "require_evidence"]',
                )
            ],
            ["test_require_evidence_is_never_emitted"],
        ),
        (
            "P4 · 让一条查询去读 obligations 列（B1-10 的消费那一半）",
            [
                Mutation(
                    PIPELINE,
                    "SELECT reason_code FROM policy_decisions",
                    "SELECT reason_code, obligations FROM policy_decisions",
                )
            ],
            ["test_nothing_reads_the_obligations_column"],
        ),
        (
            "P5 · 建一个叫 PricingPolicy 的符号（A5 / D1，裁定二「不改结构」）",
            [
                Mutation(
                    POLICY_OUTPUT,
                    "EFFECTS: tuple[str, ...] = (",
                    "PricingPolicy = None\nEFFECTS: tuple[str, ...] = (",
                )
            ],
            ["test_none_of_the_twelve_fixed_hooks_exists_as_a_symbol"],
        ),
        (
            "P6 · 让两张 obligation 词表开始合流",
            # `expected_hits=1`，而初版写的是 2 —— harness 当场拒绝写入并说
            # 「anchor matched 1 place(s), probe expects 2」。数错的原因很具体：
            # `require_evidence` 在这个模块里出现两次，**而只有一次带引号**
            # （另一次在注释里写作 `` `require_evidence` ``）。
            # 这正是那条闸防的方向之一 —— 没有它，这支探针会按一个错的预期跑过去。
            [Mutation(POLICY_OUTPUT, '"require_evidence"', '"manual_review"')],
            # 后两条是**既有**守卫，不是本批新增的 —— 声明出来，
            # 是因为「它们也会红」正是这支探针要证明的另一半：
            # 这个变异**不是独守的**，词表那一族已有三重守卫。
            [
                "test_the_two_obligation_vocabularies_are_not_the_same_one",
                "test_require_evidence_is_never_emitted",
                "test_the_vocabularies_match_the_contract[obligations]",
            ],
        ),
        (
            "P7 · 把零写者的时点从词表里「清理」掉（反方向那一半）",
            [Mutation(POLICY_OUTPUT, '    "post_understanding",\n', "")],
            # 同上：三条既有守卫一起红，说明这个方向被守得很厚。
            [
                "test_the_unwired_points_are_still_in_the_vocabulary[post_understanding]",
                "test_hook_and_effect_also_match_the_column_checks",
                "test_only_the_four_wired_hook_points_have_writers",
                "test_the_vocabularies_match_the_contract[hook]",
            ],
        ),
        (
            "P8 · 多出第八个 PolicyOutput 构造点（复核窗口 2026-09-14 指出的那条恒真式）",
            # **这一支是补的，而它补的是一条曾经什么都没守的断言。**
            # 初版写的是 `sum(counts.values()) == len(_policy_output_calls())` ——
            # 循环每转一圈恰好 +1，左右两边都是「跑了几次」，**恒真**；
            # 而它声称要抓的「有构造点没被归到任何时点上」，
            # 在循环里的 `assert hook in counts` 就已经红了。
            # 对常量之后它才成为一条断言，而这一支证明它真的会红。
            [
                Mutation(
                    DIGEST,
                    "        record_policy_decision(",
                    "        _probe = PolicyOutput(\n"
                    '            hook="pre_execute",\n'
                    '            effect="allow",\n'
                    '            reason_code="PROBE",\n'
                    '            policy_id="probe",\n'
                    '            policy_version="1.0.0",\n'
                    '            obligations=("audit",),\n'
                    "            context={},\n"
                    "        )\n"
                    "        record_policy_decision(",
                )
            ],
            [
                "test_only_the_four_wired_hook_points_have_writers",
                "test_the_scanner_really_read_the_sources",
            ],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run) as session:
        print("baseline:", session.assert_baseline(passed=BASELINE_PASSED))
        print()
        verdicts: list[tuple[str, bool, bool]] = []
        for label, mutations, expects in probes:
            result = session.probe(label, mutations)
            names = _failed_names(result.output)
            # **集合相等，不是「包含」** —— 这一版是被隔壁那支探针
            # （`probe_plan_document.py`）逼出来的：那里一处缩进写错让被变异的模块
            # 成了 `IndentationError`，三条 AST 断言一起红，而「包含」判定为命中，
            # 因为期待的那个名字**确实在**失败清单里。
            # **多红出来的那几条是信号**：它们说明变异的影响面比声称的大。
            # 所以每支探针都要把「它会连带红哪几条既有用例」也声明出来 ——
            # 下面 P6 / P7 的 `expects` 里那几条既有词表断言就是这么来的。
            hit = set(names) == set(expects)
            verdicts.append((label, result.went_red, hit))
            print(result)
            if result.went_red and not hit:
                extra = sorted(set(names) - set(expects))
                short = sorted(set(expects) - set(names))
                print(f"    ⚠️ 红了，但不是声称的那一组：多红 {extra}；没红 {short}")
            print()

    print("=" * 72)
    silent = [label for label, went_red, _ in verdicts if not went_red]
    misaimed = [label for label, went_red, hit in verdicts if went_red and not hit]
    for label, went_red, hit in verdicts:
        mark = "RED ✅" if went_red and hit else ("RED ⚠️ 红错了" if went_red else "GREEN ❌")
        print(f"  {mark:>14}  {label}")
    if silent:
        print(f"\n**{len(silent)} 支探针没红，它们证明不了任何东西**")
    if misaimed:
        print(f"\n**{len(misaimed)} 支红在别的用例上**")
    return 1 if silent or misaimed else 0


if __name__ == "__main__":
    raise SystemExit(main())
