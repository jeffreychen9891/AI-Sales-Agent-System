#!/usr/bin/env python
"""反向探针：运维缺陷 18 那几条守卫，逐条验它会红。

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **F1** | 示例换回 `we quoted it`（= 修复前） | 示例那条 |
| **F2** | 规则里那句「他以为我们讲过」拿掉 | 假预设那条 |
| **F3** | 语言那半句拿掉 | 语言那条 |
| **F4** | **平台不写槽**（`record_delivered_slots` 恒不写）= 修复前 | 铺开那几条 |
| **F5** | 不看投递记录，**帧声明了就写** | 反侧那条 |
| **F6** | 不比值就写（每轮追加证据） | 幂等那条 |
| **F7** | 来源记 `model` 不记 `platform` | 来源那条 |
| **F8** | 人写的也照盖 | 人写优先那条 |
| **F9** | 没有原文时把 **ref 本身**写进槽 | 「包没供词」那条 |
| **F10** | 平台写回挪到**模型写回之前** | 顺序那条 |
| **F11** | 封版检查恒放行 | 封版那两条 |
| **F12** | 配包 goal 换回「清单里没有它就是已讲过」 | 配包那几条 |

## ⚠️ F10 是本批唯一一条「只能读顺序」的判据

「平台的值有没有赢」在单元层面看不出来 —— 两种顺序下
`record_delivered_slots` 的行为一模一样。**区别在于它在谁之后被调用。**
而那条用例第一版把顺序判据写松了（`min(早于它的写回)`），
**这一路上有两处 `apply_slot_updates`，于是挪到客户那一处之前它照样绿** ——
F10 就是为了量出这件事而存在的。

## 跑法

```sh
.venv/Scripts/python.exe scripts/probe_a_number_that_was_never_said.py
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
TARGET = "tests/contracts/test_a_number_that_was_never_said.py"

#: 按数断言，不按颜色。**被 `assert_baseline` 顶出来的，不是算出来的。**
BASELINE_PASSED = 19
BASELINE_SKIPPED = 0
CONTROL_BASELINE_PASSED = 2491
CONTROL_BASELINE_SKIPPED = 53

MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
STACK = ROOT / "services/worker/src/fastagent_v2/worker/target_stack.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
PROFILE = ROOT / "profiles/internal-demo-sales/profile.json"
TOUCHED = (MODEL, STACK, PIPELINE, REGISTRY, PROFILE)

GOOD_EXAMPLE = '"turn_summary": "example: he asked what the price covers and wants a number."'
BAD_EXAMPLE = '"turn_summary": "example: he asked what the price covers; we quoted it."'

ASSUMES = (
    "  -- **including when he assumes we already did**: write that assumption down as\n"
    "  his, never as something that happened. Never restate earlier turns; write one\n"
    "  even when little happened. Write it in the language he wrote in."
)
NO_ASSUMES = (
    ".\n  Never restate earlier turns; write one\n"
    "  even when little happened. Write it in the language he wrote in."
)
NO_LANGUAGE = (
    "  -- **including when he assumes we already did**: write that assumption down as\n"
    "  his, never as something that happened. Never restate earlier turns; write one\n"
    "  even when little happened."
)

WRITES = """            if not reference or reference not in delivered_refs:
                continue"""
NEVER_WRITES = """            if True:
                continue"""
#: F5 的变异要真的「不看投递记录」，而不是把判据写反 ——
#: 写反会让声明了 ref 的帧在**没发**时写、发了时不写，两条都红，分不出是哪一条。
IGNORES_DELIVERY = """            if not reference:
                continue"""

COMPARES = """            if frame["slots"].get(name) == text:
                continue"""
ALWAYS_WRITES = """            if False:
                continue"""

PLATFORM_SOURCE = '                "source": "platform",'
MODEL_SOURCE = '                "source": "model",'

HUMAN_WINS = (
    '            if recorded is not None and '
    'str(recorded.get("source", "model")) == "human":'
)
HUMAN_LOSES = "            if False:"

NO_TEXT_NO_WRITE = """            text = texts.get(reference)
            if not text:
                continue"""
REF_AS_TEXT = """            text = texts.get(reference) or reference"""

#: F10：把平台那次调用**真的挪到模型写回之前**，函数体一个字不改。
#:
#: ⚠️ **第一版写的是把调用改名**（`_ = record_delivered_slots`）—— 那既不是合法
#: Python（后面几行参数就悬空了），红起来也是「语法坏了」而不是「顺序错了」。
#: **无效的变异给的是假信号**（本仓记法）。这一版是两步的真搬家：
#: 删掉原处，再插到 `writeback = apply_slot_updates(` 之前。
#:
#: 两个锚点都唯一：接管那一路的 `apply_slot_updates` 缩进是 8 格，
#: 客户这一路是 12 格，所以带缩进的锚点只命中一处。
CALL_BLOCK = """            delivery_writeback = record_delivered_slots(
                aggregate=prepared.aggregate,
                profile=prepared.profile,
                delivered=prepared.delivered_materials,
                recorded_at=_iso(datetime.now(UTC)),
                evidence_event_id=str(prepared.envelope["event_id"]),
            )
            if delivery_writeback.applied:
                prepared.aggregate.clear()
                prepared.aggregate.update(delivery_writeback.aggregate)
                validate_contract("conversation-aggregate", prepared.aggregate)
"""
MODEL_WRITEBACK = "            writeback = apply_slot_updates("

SEALS = """    if offences:
        raise ProfilePackageError(
            f"profile package {path} has slots filled by a delivery that cannot \""""
NEVER_SEALS = """    if False:
        raise ProfilePackageError(
            f"profile package {path} has slots filled by a delivery that cannot \""""

NEW_GOAL = "**清单里没有它，就看 `price_quoted`**"
OLD_GOAL = "**清单里没有它就是已讲过**"

DECLARES = """          "on_revisit": "carry",
          "filled_by_delivery": "material:demo-price-plain:v1"
"""
DECLARES_NOT = """          "on_revisit": "carry"
"""

#: ⚠️ **配包的每一处变异都要把 `content_hash` 一起改，否则包封不出。**
#:
#: 第一版没改，实测连带 **91 failed / 11 errors** —— 包不再自洽，
#: 于是每一条读得到它的用例都红，**而那与本支要证的那一条毫无关系**。
#: 一个红了九十一条的探针证明不了任何一条（本仓记法：红得多不等于写得好）。
#:
#: 三个数都是现算的（`canonical_hash(合并素材之后的文档)`），不是抄的。
STAMP = "00cdfc0e4416d864d6b6e2f825fa71c3a320ca09aa5330e35bc6fd39104a0923"
STAMP_WITH_OLD_GOAL = "a551d567693d9d66e04c32989f0faf6bf55fbfda4d209a06f001b241016bc67b"
STAMP_WITHOUT_SLOT = "c9aa3a7e6a70dc2b2aa80df8d9a9be3a59f1e7944995802c5746399cc59bc7be"

EXAMPLE = "test_the_example_obeys_the_rule_it_illustrates"
ASSUMPTION = "test_the_rule_covers_what_he_merely_assumes"
LANGUAGE = "test_the_summary_is_told_which_language_to_write_in"
FILLS = "test_a_delivered_material_fills_the_slot_that_declares_it"
SOURCE = "test_the_evidence_says_platform_not_model"
NOT_YET = "test_nothing_is_written_before_the_material_goes_out"
ALONE = "test_a_slot_that_declares_nothing_is_left_alone"
IDEMPOTENT = "test_the_same_delivery_does_not_pile_up_evidence"
HUMAN = "test_a_human_written_slot_is_refused_not_overwritten"
NO_WORDS = "test_a_package_with_no_words_writes_nothing"
ORDER = "test_the_platform_writes_after_the_model_not_before"
WRONG_REF = "test_a_ref_this_frame_never_declares_is_refused_at_seal_time"
RIGHT_REF = "test_the_right_ref_seals"
NON_STRING = "test_a_non_string_slot_is_refused"
SHIPPED = "test_the_shipped_package_declares_it_on_the_price_slot"
GOAL = "test_the_goal_no_longer_says_an_empty_list_means_it_was_said"
SAME_REF = "test_the_slot_and_the_goal_point_at_the_same_material"
MODEL_MAY = "test_the_model_can_still_write_the_slot_itself"

#: 改了任何一个平台提示词，这三条就红 —— 它们**现量现比**那三张字节表。
#: 这不是意外的连带，是那三条用例存在的理由。
FLOOR = "test_the_floor_equals_the_request_it_claims_to_measure"
OVERHEAD = "test_the_overhead_constants_are_measured_from_the_real_payload"
SPELLED_ONCE = "test_the_platform_prompt_sizes_are_spelled_once_per_package"
BYTES_MOVED = (FLOOR, OVERHEAD, SPELLED_ONCE)

#: 改了配包内容，钉住那个戳的那两条就红 —— 同理，那正是它们的职责。
#: **这几个名字是量出来的**，不是猜的（第一版写的是空元组）。
#: ⚠️ **不叫 `STAMP`** —— 上面那个哈希常量就叫 `STAMP`，
#: 第一版在这里重定义了它，于是 F12 拿着一个**测试函数名**
#: 去 profile.json 里找哈希，`anchor matched 0 place(s)`。
STAMP_TEST = "test_the_package_stamp_moves_with_its_version"
RUNBOOK_STAMP = "test_the_runbook_quotes_the_stamp_in_the_package"
STAMP_MOVED = (STAMP_TEST, RUNBOOK_STAMP)

#: F12 还多连一条**兄弟守卫**：同一件事在另一份用例里也有一半
#: （`test_the_same_line_is_not_read_out_twice`），它断言 goal 里点了
#: `price_quoted`。换回旧 goal 那一句就把它一起带红了 —— 这是对的，
#: **一条规则在两处被钉住，改一处就该两处都说话**。
SIBLING_GOAL = "test_the_goal_tells_the_model_what_to_do_when_the_line_is_gone"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "F1 · 示例换回 `we quoted it`（= 修复前）—— **本批兑现物之一**",
            [Mutation(MODEL, GOOD_EXAMPLE, BAD_EXAMPLE)],
            [EXAMPLE],
            BYTES_MOVED,
        ),
        (
            "F2 · 规则不点名「他以为我们讲过」",
            [Mutation(MODEL, ASSUMES, NO_ASSUMES)],
            [ASSUMPTION],
            BYTES_MOVED,
        ),
        (
            "F3 · 语言那半句拿掉",
            [Mutation(MODEL, ASSUMES, NO_LANGUAGE)],
            [LANGUAGE],
            BYTES_MOVED,
        ),
        (
            "F4 · **平台从不写槽**（= 修复前）—— **本批兑现物**",
            [Mutation(STACK, WRITES, NEVER_WRITES)],
            #: ⚠️ `ALONE` 与 `HUMAN` **也红，量出来的**：一个字都不写时
            #: 「别的槽没被碰」那条读到的是空字典（它先断言 `price_quoted` 在里面），
            #: 而「人写的被拒」那条断言的是**有一条拒绝记录**，不写就没有拒绝。
            #: 预测时两条都漏了。
            [FILLS, SOURCE, IDEMPOTENT, ALONE, HUMAN],
            [],
        ),
        (
            "F5 · 不看投递记录，帧声明了就写（反侧）",
            [Mutation(STACK, WRITES, IGNORES_DELIVERY)],
            [NOT_YET],
            [],
        ),
        (
            "F6 · 不比值就写",
            [Mutation(STACK, COMPARES, ALWAYS_WRITES)],
            [IDEMPOTENT],
            [],
        ),
        (
            "F7 · 来源记 `model`",
            [Mutation(STACK, PLATFORM_SOURCE, MODEL_SOURCE)],
            [SOURCE],
            [],
        ),
        (
            "F8 · 人写的也照盖",
            [Mutation(STACK, HUMAN_WINS, HUMAN_LOSES)],
            [HUMAN],
            [],
        ),
        (
            "F9 · 没有原文时把 ref 本身写进槽",
            [Mutation(STACK, NO_TEXT_NO_WRITE, REF_AS_TEXT)],
            [NO_WORDS],
            [],
        ),
        (
            "F10 · 平台写回挪到模型写回之前（真搬家，两步）",
            [
                Mutation(PIPELINE, CALL_BLOCK, ""),
                Mutation(PIPELINE, MODEL_WRITEBACK, CALL_BLOCK + MODEL_WRITEBACK),
            ],
            [ORDER],
            [],
        ),
        (
            "F11 · 封版检查恒放行",
            [Mutation(REGISTRY, SEALS, NEVER_SEALS)],
            [WRONG_REF, NON_STRING],
            [],
        ),
        (
            "F12 · 配包 goal 换回「清单里没有它就是已讲过」",
            [
                Mutation(PROFILE, NEW_GOAL, OLD_GOAL),
                Mutation(PROFILE, STAMP, STAMP_WITH_OLD_GOAL),
            ],
            [GOAL],
            (SIBLING_GOAL, *STAMP_MOVED),
        ),
        (
            "F13 · 配包不再声明 `filled_by_delivery`",
            [
                Mutation(PROFILE, DECLARES, DECLARES_NOT),
                Mutation(PROFILE, STAMP, STAMP_WITHOUT_SLOT),
            ],
            #: ⚠️ **量出来是七条，不是两条**：那五条行为用例读的就是在架的包，
            #: 声明一撤，它们观察的那件事整个不发生了。
            #: **所以 F13 与 F4 的红集重叠** —— F13 证的不是「写槽这件事还在」，
            #: 是**「今天真的有一个包声明了它」这件事是承重的**。
            [SHIPPED, SAME_REF, FILLS, SOURCE, ALONE, IDEMPOTENT, HUMAN],
            STAMP_MOVED,
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
