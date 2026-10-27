#!/usr/bin/env python
"""反向探针：`tests/contracts/test_the_platform_advances_the_conversation.py`。

**最要紧的三支**：

* A · **把防循环那一条删掉**（不再查栈历史）—— 它必须红，
  因为那条缺失的后果是一个**不会停的循环**：`quote` 完成 pop 回主线后
  条件依然成立，每一轮都推一次、发一条消息给客户；
* B · **把推进挪到计划步之后** —— 那一轮的回复仍按旧帧生成，
  「立马」退化成「下次再说」，而这一批存在的全部理由就是那个「立马」；
* C · **把接线整条删掉** —— 机制写了没人读，而那种失败**配包里一个字都不缺**。

其余：

* D · 深度上限与模型 `push` 分家；
* E · 条件反过来（没填也推）；
* F · 平台推进被记成打岔（`interruption_reason` 填了值）；
* G · 父帧不挂起；
* H · 配包把推进指到收尾帧（平台帧，进了解不开）；
* I · 在架那条链被改；
* J · **把「进过没有」退回读栈** —— 这一支钉的是本批最贵的那个发现：
  `request_pop` 会把子帧**真的弹掉**，一个正常完成并返回的帧在栈里
  一点痕迹都不留，**平台会立刻再推它一次**（每一轮给客户重报一次价）。
  历史因此存在 `conversation_aggregate.frames_entered` 上。

    python scripts/probe_the_platform_advances_the_conversation.py
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_harness import (  # noqa: E402
    Mutation,
    ProbeSession,
    control_runner,
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_the_platform_advances_the_conversation.py"

#: 本文件自己那几条。**报数，不报颜色。**
BASELINE_PASSED = 10

#: `tests/contracts` 减去本文件。**量出来的。**
CONTROL_BASELINE_PASSED = 2390
CONTROL_BASELINE_SKIPPED = 53

STACK = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "target_stack.py"
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
VALIDATION = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "inference_validation.py"
)
PACKAGE = ROOT / "profiles" / "internal-demo-sales" / "profile.json"

TOUCHED = (STACK, PIPELINE, VALIDATION, PACKAGE)

_FAILED = re.compile(r"'([\w\[\]\-:.]+)'")


def _failed_names(output: str) -> list[str]:
    return _FAILED.findall(output.split("FAILED: ", 1)[-1])


ONCE = "test_a_frame_is_entered_once_in_a_lifetime"
BEFORE_PLAN = "test_the_advance_runs_before_the_plan_step"
ADVANCES = "test_two_sections_told_means_go_and_quote"
STAYS = "test_nothing_told_yet_means_stay"
DEPTH = "test_a_full_stack_does_not_advance"
SAME_CAP = "test_the_platform_advance_obeys_the_same_depth_cap"
SHAPE = "test_the_pushed_frame_is_active_and_the_parent_waits"
TARGETS = "test_every_declared_target_exists_and_is_not_a_platform_frame"
CHAIN = "test_the_shipped_chain_is_the_one_the_user_asked_for"
HALFWAY = "test_the_return_path_does_not_advance_halfway_through"

#: ⚠️ **配包一动就掀封版戳**：`identity.content_hash` 对不上 → 包加载不了 →
#: 一路红到收集期。逐名登记那一片没有意义（它们与推进链无关），
#: **改按性质断言**：封版那一条必须在里面，而规模必须是「一片」。
SEAL_BLAST = "<封版戳被掀翻：那一片与推进链无关>"
SEAL_TEST = "test_the_package_loads_through_the_registry_and_hashes_to_its_declaration"


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str] | str]] = [
        (
            "A · 删掉防循环那一条（不再查栈历史）",
            [
                Mutation(
                    STACK,
                    # 只拿掉「进过就不再进」那半句，`str(...)` 那半句原样留着。
                    "if target in ever_entered or target ==",
                    "if target ==",
                )
            ],
            [ONCE],
            (),
        ),
        (
            "B · 把推进挪到计划步之后",
            [
                Mutation(
                    PIPELINE,
                    "            advancing_to = frame_to_advance_into(\n",
                    "            advancing_to = None and frame_to_advance_into(\n",
                )
            ],
            # ⚠️ **只有 AST 那条红，行为那条不红 —— 而那是实测出来的**：
            # `two_sections_told_means_go_and_quote` 直接调 `frame_to_advance_into`，
            # 不走管线，所以管线里的短路动不了它。
            # **`contracts` 这一层没有任何东西能看见管线跑没跑**，
            # 顶到这里的只剩 AST 那一条。
            [BEFORE_PLAN],
            (),
        ),
        (
            "C · 接线整条删掉（写了没人读）",
            [
                Mutation(
                    PIPELINE,
                    # B 支短路掉调用的结果；这一支让**没有人用**那个结果。
                    # 两支合起来钉住「算出来了，还得有人据它行动」。
                    #
                    # ⚠️ 上一版换掉的是调用那一行本身，剩下的参数行就成了语法错，
                    # 连带红一片 `collection-error` —— **那是无效变异**。
                    "            if advancing_to is not None:",
                    "            if False:",
                )
            ],
            [BEFORE_PLAN],
            (),
        ),
        (
            "D · 深度上限与模型 push 分家",
            [Mutation(STACK, "PLATFORM_ADVANCE_DEPTH_CAP = 3", "PLATFORM_ADVANCE_DEPTH_CAP = 9")],
            [DEPTH, SAME_CAP],
            (),
        ),
        (
            "E · 条件反过来（没填也推）",
            [
                Mutation(
                    STACK,
                    # 条件谓词 2026-09-21 从 `slot` 改成了 `delivered`（ADR-0146）。
                    '        if str(rule["delivered"]) in delivered:',
                    '        if str(rule["delivered"]) not in delivered:',
                )
            ],
            # 条件反过来，「两段讲完就推进」那条自然也红 —— 实测。
            [STAYS, ADVANCES],
            (),
        ),
        (
            "F · 平台推进被记成打岔",
            [
                Mutation(
                    STACK,
                    # ⚠️ 这三行注释是它的**唯一**锚点：光凭
                    # `"interruption_reason": None` 会命中三个 push 函数。
                    "            #: 顺序往下走了一步。\n"
                    '            "interruption_reason": None,\n',
                    "            #: 顺序往下走了一步。\n"
                    '            "interruption_reason": "side_question",\n',
                )
            ],
            [SHAPE],
            (),
        ),
        (
            "G · 父帧不挂起",
            [Mutation(STACK, '    parent["status"] = "suspended"\n', "")],
            # 插问那一条也检查了「挂起的是主线」，所以它跟着红 —— 实测。
            [SHAPE],
            (),
        ),
        (
            "H · 配包把推进指到收尾帧",
            [Mutation(PACKAGE, '"push": "trial_close"', '"push": "closing"')],
            [TARGETS, CHAIN],
            SEAL_BLAST,
        ),
        (
            "J · 「进过没有」退回读栈（request_pop 一弹就忘）",
            [
                Mutation(
                    STACK,
                    '    recorded = aggregate.get("frames_entered")',
                    "    recorded = None",
                )
            ],
            # ⚠️ **本文件一条都不红 —— 而那恰恰说明了问题所在**：
            # `ONCE` 那条的夹具把帧留在栈里（`history=`），所以退回读栈也读得到。
            # 看得见这个缺陷的是 `revisit` 那一族 —— 它们的夹具照真实返回路径摆：
            # 帧已经被 `request_pop` 弹掉了。
            [],
            (
                "test_a_brief_answer_frame_is_not_entered_twice",
                "test_the_validator_and_the_transition_agree",
            ),
        ),
        (
            "I · 在架那条链被改",
            [
                Mutation(
                    PACKAGE,
                    # 条件 2026-09-21 改成了平台自己的记录（ADR-0146）。
                    '"delivered": "material:demo-price-plain:v1"',
                    '"delivered": "material:demo-trial-how:v1"',
                )
            ],
            [CHAIN],
            SEAL_BLAST,
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED,
                control_passed=CONTROL_BASELINE_PASSED,
                control_skipped=CONTROL_BASELINE_SKIPPED,
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            names = _failed_names(result.output)
            hit = set(names) == set(expects)
            control_names = _failed_names(result.control_output or "")
            if collateral == SEAL_BLAST:
                collateral_hit = SEAL_TEST in control_names and len(control_names) > 30
            else:
                collateral_hit = set(control_names) == set(collateral)
            verdicts.append((label, result.went_red, hit, collateral_hit))
            print(result)
            if not hit:
                print(
                    f"    ⚠️ 本文件：多红 {sorted(set(names) - set(expects))}；"
                    f"没红 {sorted(set(expects) - set(names))}"
                )
            if not collateral_hit and collateral == SEAL_BLAST:
                print(
                    f"    ⚠️ 连带：封版那一条在不在 = {SEAL_TEST in control_names}，"
                    f"红了 {len(control_names)} 条（要 > 30）"
                )
            elif not collateral_hit:
                print(
                    f"    ⚠️ 连带：多红 {sorted(set(control_names) - set(collateral))}；"
                    f"没红 {sorted(set(collateral) - set(control_names))}"
                )
            print()

    print("=" * 72)
    bad = [label for label, _, hit, coll in verdicts if not (hit and coll)]
    for label, went_red, hit, coll in verdicts:
        mark = ("RED ✅" if went_red else "GREEN ✅（声明如此）") if hit and coll else "❌ 对不上"
        print(f"  {mark:>18}  {label}")
    if bad:
        print(f"\n**{len(bad)} 支与声明的集合对不上**")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
