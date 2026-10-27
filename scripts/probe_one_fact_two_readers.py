#!/usr/bin/env python
"""反向探针：运维缺陷 20 那几条守卫，逐条验它会红。

## 它跑在门禁的栈里

本批**真正会红的那一条是连库的 e2e**（把列往回拨 600 秒，看那一步被告知多久），
所以 `own_targets` 跨两层，控制面也跨两层。`control_runner` 的 docstring 要求
「守卫在 `tests/contracts` 之外时自己传 `scope` 并说明，不许一个都不传」——
这里不用它，直接拿 `suite_runner` 拼两个 scope，**理由写在 `control` 那一行**。

跑法与 `probe_a_prepare_failure_is_counted.py` 逐字相同（栈由 `ci.sh` 提供）：

```sh
python - <<'EOF'
from pathlib import Path
src = Path('scripts/ci.sh').read_text(encoding='utf-8')
cut = src.split('"$python_bin" -m ruff check')[0]
out = cut.replace(
    '"$python_bin" -m pytest\n',
    '"$python_bin" "$repo_root/scripts/probe_one_fact_two_readers.py"\n',
)
Path('scripts/_probe_runner.sh').write_text(out, encoding='utf-8', newline='')
EOF
FASTAGENT_PYTHON=.venv/Scripts/python.exe sh scripts/_probe_runner.sh    # ⚠️ 这个变量不能省
rm scripts/_probe_runner.sh
```

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **G1** | **删掉那一句刷新**（= 修复前，JSONB 那份恒 `None`） | e2e 那条 |
| **G2** | 刷新改成读 JSONB 自己（反侧：证明红的是「读了列」） | e2e 那条 |
| **G3** | 空列刷成「现在」 | 「空不是失败」那条 |
| **G4** | 刷新时不转字符串，直接塞 `datetime` | 形状那条 |
| **G5** | 兜底改成抛异常 | 兜底那条 |
| **G6** | 锚点为空时不记那个数 | 计数那条 |
| **G7** | `_seconds_since` 恒回 `0.0`（= 症状本身） | 反侧那条 |

⚠️ **基线按数断言，不按颜色**（`assert_baseline`）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: **自己把 stdout 转成 UTF-8**，不要求调用方 export `PYTHONIOENCODING`。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import Mutation, ProbeSession, suite_runner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
UNIT = "tests/contracts/test_one_fact_two_readers.py"
WIRED = "tests/e2e/test_the_silence_question_runs_on_a_real_turn.py"
TARGETS = [UNIT, WIRED]

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 9
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2640
CONTROL_BASELINE_SKIPPED = 53

PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
TOUCHED = (PIPELINE,)

REFRESH = """                aggregate["last_outbound_at"] = _iso_or_none(
                    aggregate_row["last_outbound_at"]
                )"""
NO_REFRESH = """                pass  # 修复前：JSONB 那一份从建会话起就是 None，零写点"""
#: G2 的反侧：**照样写一句，只是读回 JSONB 自己**。
#: 没有它，G1 只证明「少了一句就红」，证不出「红的是因为读了列」——
#: 一个把常量写进去的实现同样能让 G1 绿。
SELF_REFRESH = """                aggregate["last_outbound_at"] = _iso_or_none(
                    aggregate.get("last_outbound_at")
                )"""

EMPTY_STAYS_EMPTY = """    if value is None:
        return None"""
EMPTY_BECOMES_NOW = """    if value is None:
        return _iso(datetime.now(UTC))"""

CONVERTS = """    if isinstance(value, datetime):
        return _iso(value)"""
KEEPS_DATETIME = """    if isinstance(value, datetime):
        return value  # type: ignore[return-value]"""

FALLS_OPEN = """    parsed = _parse_moment(moment)
    if parsed is None:
        return 0.0"""
RAISES = """    parsed = _parse_moment(moment)
    if parsed is None:
        raise ValueError("读不出锚点")"""
ALWAYS_ZERO = """    parsed = _parse_moment(moment)
    if True:
        return 0.0"""

COUNTS = """        if anchor is None:
            SILENCE_ANCHOR_MISSING_TOTAL.increment("last_outbound_at")"""
COUNTS_NOT = """        if False:
            SILENCE_ANCHOR_MISSING_TOTAL.increment("last_outbound_at")"""

SHAPE = "test_a_column_timestamp_becomes_a_contract_shaped_string"
EMPTY = "test_an_empty_column_stays_empty"
FALLBACK = "test_the_fallback_still_opens_the_safe_way"
REVERSE = "test_a_moment_in_the_past_reads_as_time_having_passed"
COUNTED = "test_a_missing_anchor_leaves_a_number_behind"
OTHER_ANCHOR = "test_the_other_anchor_does_not_have_the_same_fork"
TOLD = "test_the_silence_step_is_told_how_long_the_silence_actually_was"
ASKED = "test_the_question_is_asked_on_a_silence_turn_and_not_on_a_customer_message"
WAITS = "test_a_wait_sends_nothing"

#: G4 把 `datetime` 塞进聚合之后连带红的那一族 —— **量出来的，预测写的是空**。
#: 它们不是「别的地方坏了」，是同一件事的下游：聚合那一份要过
#: `validate_contract("conversation-aggregate", …)`，而 `format: date-time`
#: 要的是字符串，**于是每一个读存量聚合的定时器回合都过不去**。
#: 这七条的存在反过来说明那一次转换是承重的，不是修辞。
TIMER_FAMILY = (
    "test_a_proactive_row_whose_window_closed_is_cancelled_and_never_sent",
    "test_a_reply_before_the_timer_is_due_ends_it",
    "test_a_row_expired_by_quiet_hours_records_when_it_may_be_sent",
    "test_a_timer_turn_writes_the_window_deadline_into_the_command",
    "test_new_customer_message_invalidates_old_generation_timer",
    "test_the_next_turn_gets_its_own_timer_rather_than_reviving_the_old_one",
    "test_timer_path_uses_scheduler_lease_and_the_same_inbox_pipeline",
)

run = suite_runner(ROOT, PYTHON, TARGETS)
#: **控制面跨两层，而不是默认那一层。**
#: 本批的守卫一半在 `tests/contracts`、一半在 `tests/e2e`，只量前者会漏掉
#: 「改了 `pipeline.py` 把别的连库用例带红了」这整类连带 —— 而那正是
#: 「连带的是那几条」这一问存在的理由。两层都跑，慢，而它答得对。
control = suite_runner(ROOT, PYTHON, ["tests/contracts", "tests/e2e"], ignore=TARGETS)


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "G1 · 删掉那一句刷新（= 修复前）—— **本批兑现物**",
            [Mutation(PIPELINE, REFRESH, NO_REFRESH)],
            [TOLD],
            [],
        ),
        (
            "G2 · 刷新改成读 JSONB 自己（反侧）",
            [Mutation(PIPELINE, REFRESH, SELF_REFRESH)],
            [TOLD],
            [],
        ),
        (
            "G3 · 空列刷成「现在」",
            [Mutation(PIPELINE, EMPTY_STAYS_EMPTY, EMPTY_BECOMES_NOW)],
            [EMPTY],
            [],
        ),
        (
            "G4 · 刷新时不转字符串",
            [Mutation(PIPELINE, CONVERTS, KEEPS_DATETIME)],
            #: ⚠️ **三条 e2e 全红，而且连带七条 —— 两边都是量出来的。**
            #: 一个 `datetime` 进了聚合，契约校验就拦下**每一个读存量聚合的回合**，
            #: 于是这个文件里的三条连库用例与定时器那一族一起倒。
            #: 预测写的是「只有形状那条」，错了。
            [SHAPE, TOLD, ASKED, WAITS],
            TIMER_FAMILY,
        ),
        (
            "G5 · 兜底改成抛异常",
            [Mutation(PIPELINE, FALLS_OPEN, RAISES)],
            #: ⚠️ `COUNTED` 也红，量出来的：那条用例自己就调
            #: `_silent_for_seconds(空锚点)`，而空锚点这时会抛。
            [FALLBACK, COUNTED],
            [],
        ),
        (
            "G6 · 锚点为空时不记那个数",
            [Mutation(PIPELINE, COUNTS, COUNTS_NOT)],
            [COUNTED],
            [],
        ),
        (
            "G7 · `_seconds_since` 恒回 0.0（= 症状本身）",
            [Mutation(PIPELINE, FALLS_OPEN, ALWAYS_ZERO)],
            #: ⚠️ `COUNTED` 也红，量出来的：它的后半句断言
            #: 「锚点给得出时算得出秒数」（`> 600`），而恒回 0.0 正好打掉它。
            [REVERSE, TOLD, COUNTED],
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
