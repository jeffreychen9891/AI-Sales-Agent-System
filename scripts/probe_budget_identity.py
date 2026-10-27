#!/usr/bin/env python
"""反向探针：预算推导式那条等式，验它在每一项变动时都会红。

守的是 `tests/contracts/test_prompt_bytes.py` 的
`test_the_empty_request_baseline_is_measured_from_the_real_payload` ——
`MAX - 空请求基线 - understanding - 知识库 - 安全余量 == MATERIAL_BYTES_BUDGET`。

**四支变异，各打式子里的一项**：

| 支 | 把什么改掉 | 谁该红 |
|---|---|---|
| **P1** | 往 `generate_prompt` 里加一个键（payload 多一项） | 等式那条 |
| **P2** | `SAFETY_MARGIN_BYTES` 改一个数 | 等式那条 |
| **P3** | `MATERIAL_BYTES_BUDGET` 改一个数（**registry 那一份**） | 等式那条 |
| **P4** | 把空请求基线改回**手抄一份镜像**（回到 2026-09-15 之前） | **不该红** —— 见下 |

**P1 是这支探针存在的理由**，也是「批 1」的兑现物：
2026-09-15 之前这条用例里的 `user_prompt` 是一个**手写的 dict 字面量** ——
`generate` 那份形状的**第四份副本**（生产 / admission / 线路层 / 这里）。
**它漂过一次**：`generate` 加了 `knowledge_chunks` 而镜像没跟上，
基线少量 26 字节，**方向 fail-open**（余量看起来比实际多）。
改成现造之后，**往 payload 里加一项就会在这里红** ——
这正是安全余量那 5,000 的到期条件该待的地方：
一条会在它失效时变红的守卫，而不是一句「等某某批做完再重估」。

**P3 的锚点是本文件调试时量出来的，不是读出来的**：第一版改的是
`worker/material_selection.MATERIAL_BYTES_BUDGET`，**而这条用例照绿** ——
它读的是 `profiles/registry` 那一份。两份跨包各写一遍，
**相等由另一条用例钉**（`services/api` 不从 `services/worker` 导入，
与 `REQUEST_OVERHEAD_BYTES` 同一个理由）。
所以「改预算会红」这句话要说全：**改 registry 那份在这里红，
改 worker 那份在跨包相等那条红** —— 两条各守一半。

**P4 故意期待绿**，而它绿得难看：手抄的镜像**今天与现造的逐字节相同**，
所以把它换回去不会红。**这一支是 P1 的对照** ——
它说明「今天相同」不等于「明天相同」，而副本的代价正是在明天。

    python scripts/probe_budget_identity.py
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
    suite_runner,
)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
TARGET = "tests/contracts/test_prompt_bytes.py"

#: 按数断言，不按颜色。
BASELINE_PASSED = 11

#: 对照那一跑的基线（`tests/contracts` 减去本批自己那份）。**两个数都要报** ——
#: 对照跑的是更大的那个集合，别人留下的变异在那里最藏得住。
CONTROL_BASELINE_PASSED = 1679

SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
SELECTION = ROOT / "services/worker/src/fastagent_v2/worker/material_selection.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
TESTFILE = ROOT / TARGET

TOUCHED = (SHAPE, SELECTION, REGISTRY, TESTFILE)

#: `generate_prompt` 返回的那个 dict 的最后一个键 —— P1 在它后面插一个新键。
LAST_KEY = '        "knowledge_chunks": [dict(entry) for entry in (knowledge_chunks or [])],\n'

#: P4 把现造换回手抄那一份（2026-09-15 之前的原文，逐字节等价）。
DERIVED = """        request_shape.generate_prompt(
            strategy_id="s",
            strategy_reason_code="r",
            event_type="customer_message",
        ),
"""
HAND_WRITTEN = """        {
            "strategy_id": "s",
            "strategy_reason_code": "r",
            "generation_constraints": [],
            "candidate_intents": [],
            "required_slot_requests": [],
            "understanding": {},
            "event_type": "customer_message",
            "content_materials": [],
            "knowledge_chunks": [],
        },
"""

IDENTITY = "test_the_empty_request_baseline_is_measured_from_the_real_payload"

#: P1 的连带（2026-09-16 对照跑，基线 `88882184`）。
#:
#: **往 `generate_prompt` 加一个键不止预算那条会红** —— 全仓还有一条等值键集合
#: 守卫。而它**只在 generate 上**：
#:
#: ```sh
#: grep -rn 'exactly_these_keys' --include=*.py tests/   # 1 命中，都在 generate
#: ```
#:
#: 这两条合起来正是复核（批 1 §三）那一处开口的形状：**三种请求里只有一种
#: 既有预算等式、又有键集合守卫**。`plan` 只有回放脚本键对齐那一条（与字节无关），
#: `understand` 一条都没有。见 `material_selection.py` 到期条件那一段。
#: **两条，不是一条**（2026-09-16 补 `test_request_shape_key_sets.py` 之后重量）：
#: 一条对着**真实调用**数键（`test_generation_receives_knowledge.py`），
#: 一条对着**定义点**数键。两者问的是相邻但不同的问题，都在这一支下面红。
KEY_SET = [
    "test_the_generate_prompt_has_exactly_these_keys",
    "test_the_generate_prompt_has_exactly_these_keys_at_the_definition_point",
]

#: P3 的连带。改 registry 那份预算，跨包相等那条同时红 —— 两半各守一半，
#: 而这一行是「各守一半」这句话第一次被量出来而不是被声称。
CROSS_PACKAGE = "test_the_budget_is_spelled_once_per_package"

run = suite_runner(ROOT, PYTHON, [TARGET])
control = control_runner(ROOT, PYTHON, [TARGET])


def main() -> int:
    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "P1 · 往 `generate_prompt` 里加一个键（payload 多一项）",
            [Mutation(SHAPE, LAST_KEY, LAST_KEY + '        "a_new_thing": [],\n')],
            [IDENTITY],
            KEY_SET,
        ),
        (
            "P2 · 安全余量改一个数",
            [Mutation(SELECTION, "SAFETY_MARGIN_BYTES = 5000", "SAFETY_MARGIN_BYTES = 4999")],
            [IDENTITY],
            # **零连带，而这正是它该有的样子**：那 5,000 的到期条件挂在这一条
            # 等式上，全仓没有第二处在看它。
            [],
        ),
        (
            "P3 · MATERIAL_BYTES_BUDGET 改一个数（**改 registry 那一份**，见 docstring）",
            [
                Mutation(
                    REGISTRY,
                    "MATERIAL_BYTES_BUDGET = 32420",
                    "MATERIAL_BYTES_BUDGET = 32421",
                )
            ],
            [IDENTITY],
            [CROSS_PACKAGE],
        ),
        (
            "P4 · 把基线换回**手抄的镜像** —— **该绿**，而它绿得难看（见 docstring）",
            [Mutation(TESTFILE, DERIVED, HAND_WRITTEN)],
            [],
            # 对照里也该是空的：这支变异改的是**用例文件自己**，而对照把它排除了。
            # 两边都空，说明「今天两份逐字节相同」这句话在全量上也成立。
            [],
        ),
    ]

    with ProbeSession(paths=TOUCHED, run=run, control=control) as session:
        print(
            "baseline:",
            session.assert_baseline(
                passed=BASELINE_PASSED, control_passed=CONTROL_BASELINE_PASSED
            ),
        )
        print()
        verdicts: list[tuple[str, bool, bool, bool]] = []
        for label, mutations, expects, collateral in probes:
            result = session.probe(label, mutations)
            hit = set(result.failed) == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, bool(expects) == result.went_red, hit, elsewhere))
            want = "该红" if expects else "**该绿**"
            print(
                f"{label}\n  [{want}] {result.output}\n  命中期待：{'✅' if hit else '❌'}"
                f"\n  对照 {result.control_output}"
                f"\n  连带的是那几条：{'✅' if elsewhere else '❌'}\n"
            )

    ok = all(shape and hit and other for _, shape, hit, other in verdicts)
    print("ALL AS EXPECTED" if ok else "SOMETHING BEHAVED DIFFERENTLY")
    if ok:
        print()
        print("**P1 是这支探针的重点**：往 payload 里加一项，预算那条等式当场红 ——")
        print("安全余量那 5,000 的到期条件从此挂在一条会变红的东西上，不再挂在某一批上。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
