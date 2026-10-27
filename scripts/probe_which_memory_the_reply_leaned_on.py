#!/usr/bin/env python
"""反向探针：那条回复引了哪几条记忆（T363）。

    python scripts/probe_which_memory_the_reply_leaned_on.py

| | 变异 | 演的是 |
|---|---|---|
| A | 生成步不转发记忆 | **本批之前的样子**：写字那一步看不见记忆 |
| B | 形状退回 `{field, value}` | 递过去了，**而模型引不出任何 ref** |
| C | 记忆引用那道闸整条关掉 | 模型编一个 `memory:` ref，平台照发 |
| D | 闸只在「递过东西」时查（默认 fail-open） | 没递过记忆的回合，任何引用都放过 |
| E | 闸不认前缀，所有 ref 都往记忆集里比 | `span:` 那一族被误杀，链路堵死 |
| F | 校验器自己再算一份 | 「递了什么」与「许引什么」两份，只在真实回合里显形 |
| G | 留痕里 `offered` 那一半不过 `field_key_of` | **两个键只有一个转了** —— 相减减不出东西 |
| H | 调用点喂 `entry["value"]` | 客户原话进审计面，**函数体一个字不用改** |
| I | 两行不成对（引用那行换自己的条件） | 「没走到生成」与「一条都没引」分不开 |
| J | ref 带上一个版本号 | 一个**不能回放**的版本号，比不声明更糟 |
| K | 前缀就地拼一次 | 定义点失效，拼错的那一处会拒掉每一条合法引用 |
| L | 系统提示不讲怎么用 | 递过去而不说怎么用，等于没递 |

## G 与 H 是本批自审逼出来的两支

第一版那条留痕用例判的是「`field_key_of` 在函数体里出现过吗」——
**G 演的就是它的盲区**：两个键里只转一个，那条断言照样绿
（本仓记着「数错了的守卫看起来在守」）。

**H 更远一点**：那个函数只认 ref，而**调用点手上有 `turn_memories`**，
`value` 就在 `ref` 旁边。把一个键名写错，函数体一个字不用改，
审计面上就多了一份客户原话 —— 而留痕写得进、格式对得上。
所以本批多了一条只看调用点的用例。

## ⚠️ 探不到的两半，写出来

1. **「引用集真的等于模型引的那几条」要一个真实回合** —— 三次模型调用、
   一个事务、一条审计行。本文件钉的是**形状与接线**，
   行为那一半的证据是门禁（`tests/integration`）。
2. **「那一行写在事务里」没有机械变异**：把它挪出 `with` 要改缩进，
   而改缩进的变异同时改掉别的语句（无效变异给假信号）。
   那条用例的活性有另一份证据：**它的第一版红在自己身上** ——
   第一版判的是「调用者有个叫 `connection` 的形参」，
   而 `process_prepared` 自己开事务，于是判据当场失效。

## 字节账那张网不在本文件里

`MATERIAL_BYTES_BUDGET` / `PLATFORM_PROMPT_BYTES` / 八帧固定底那一族由
`probe_budget_identity.py` 与 `probe_a_promise_is_followed.py`（探针 H）钉着，
本批只是**又付了一次账**（28,868 → 27,385）。
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
TARGET = "tests/contracts/test_which_memory_the_reply_leaned_on.py"

#: 实测 2026-09-19，随门禁回填。
BASELINE_PASSED = 18
CONTROL_BASELINE_PASSED = 2132

MEMORY = ROOT / "packages/runtime/src/fastagent_v2/runtime/memory_prompt.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
VALIDATION = ROOT / "services/worker/src/fastagent_v2/worker/inference_validation.py"
TOUCHED = (MEMORY, MODEL, PIPELINE, VALIDATION)

#: A —— 生成步收到了却不转发。**本批之前的样子。**
FORWARDS = """                remembered_facts=[dict(entry) for entry in remembered_facts],"""
FORWARDS_NOTHING = """                remembered_facts=[],"""

#: B —— 形状退回 `{field, value}`（理解/计划步那一份）。
#:
#: 这是 `CLAUDE.md` 第三节那个陷阱本身：**schema 过得去、请求发得出**，
#: 而模型手上没有任何可引的 ref，于是引用集永远是空的。
SHAPE_IS_REF = """        {"ref": memory_ref(str(entry["field"])), "value": str(entry["value"])}"""
SHAPE_IS_FIELD = """        {"field": str(entry["field"]), "value": str(entry["value"])}"""

#: C —— 那道闸整条关掉。
GATE_CALLED = """    _validate_memory_citations(generation=generation, offered=remembered)"""
GATE_SKIPPED = """    _ = remembered"""

#: D —— 默认从 fail-closed 翻成 fail-open。
#:
#: **这是本批最该被钉住的那个方向**：`None` 意思是「这一轮没走记忆那条路」，
#: 而「没递过」不等于「什么都能引」。
FAILS_CLOSED = """    allowed = set(offered or ())"""
FAILS_OPEN = """    if offered is None:
        return
    allowed = set(offered or ())"""

#: E —— 闸不认前缀，把每一条 ref 都往记忆集里比。
CHECKS_PREFIX = """            if not text.startswith(MEMORY_REF_PREFIX):
                continue"""
CHECKS_EVERYTHING = """            if False:
                continue"""

#: F —— 校验器那一侧不再从提示词那一份现推，而是自己另算一份。
#:
#: ⚠️ **锚点 2026-09-19 重交时换过**：rebase 到批 J 之后的 `main` 上，
#: 那一段被抽成了 `_generate_and_validate`，于是这一行从
#: `…for entry in turn_memories` 变成 `…for entry in remembered_facts`
#: （形参名）。**旧锚点当场 0 命中，而探针框架会拒掉 0 命中的变异** ——
#: 那是它替我挡住的第二次（第一次是「一个锚点命中 15 处」）。
#:
#: 变异造的状态是**「把它改回成一个独立来源」**：
#: 不读自己的形参，而是从 `prepared` 另算一遍 ——
#: 那正是复核点名的那个漂移（两个调用点各写一遍，少传一个的那次静默拒绝）。
READS_ONE_LIST = (
    """            remembered={str(entry["ref"]) for entry in remembered_facts},"""
)
READS_ITS_OWN = """            remembered={
                memory_ref(str(entry["field"]))
                for entry in prepared.known_memories
            },"""

#: G —— 两个键里只有一个过了 `field_key_of`。
BOTH_CONVERTED = (
    '                        "offered": sorted(field_key_of(ref) for ref in offered),'
)
ONE_CONVERTED = '                        "offered": sorted(str(ref) for ref in offered),'

#: H —— 调用点把值喂进去。**函数体一个字不用改。**
FEEDS_REFS = """                    offered=[str(entry["ref"]) for entry in turn_memories],"""
FEEDS_VALUES = """                    offered=[str(entry["value"]) for entry in turn_memories],"""

#: I —— 引用那一行换成自己的条件，两行不再成对。
PAIRED = """            if prepared.known_memories or prepared.memories_not_shown:
                self._record_memories_cited("""
UNPAIRED = """            if turn_memories:
                self._record_memories_cited("""

#: J —— ref 带上一个版本号。**而它不能回放**：`contact_memories` 只存当前值。
NO_VERSION = """    return f"{MEMORY_REF_PREFIX}{field_key}\""""
FAKE_VERSION = """    return f"{MEMORY_REF_PREFIX}{field_key}:v1\""""

#: K —— 前缀就地拼一次，定义点失效。
USES_THE_CONSTANT = """            if not text.startswith(MEMORY_REF_PREFIX):"""
BUILDS_IT_INLINE = """            if not text.startswith(f"{MEMORY_REF_PREFIX}"):"""

#: L —— 系统提示不讲怎么用。**递过去而不说怎么用，等于没递。**
SYSTEM_EXPLAINS = """- remembered_facts are things already known about this customer. Same rule as
  knowledge_chunks: write your own words and cite the entry's ref in
  source_evidence_refs when a sentence rests on one. Never invent a memory ref."""
SYSTEM_SILENT = """- remembered_facts are things already known about this customer."""

#: E 的连带 —— **这一组是实测出来的，而它本身就是一条结论。**
#:
#: 第一版声称 E「没有连带」。实测红了知识库那一族六条：
#: 闸不认前缀之后，`knowledge:<slug>:v1` 这种 ref **落进了记忆那个集合去比**，
#: 于是每一条合法的知识库引用都被记忆闸拒掉
#: （reason_code 还写着 `GENERATION_MEMORY_REF_UNKNOWN`）。
#: **两道闸共用 `source_evidence_refs` 这一个轴** —— 一道闸不认自己的前缀，
#: 拒的就是另一道闸的合法输入。这正是本批为什么要那条
#: `test_a_non_memory_ref_is_not_judged_by_this_gate`。
KNOWLEDGE_FAMILY = [
    "test_a_claim_citing_a_chunk_this_turn_did_not_retrieve_stops_the_turn",
    "test_a_claim_may_cite_a_chunk_this_turn_retrieved",
    "test_a_ref_that_is_not_a_knowledge_ref_is_left_alone",
    "test_a_turn_that_offered_no_knowledge_allows_no_knowledge_ref",
    "test_an_evidenced_claim_counts_as_delivering",
    "test_this_gate_is_what_evidence_required_never_checked",
]

#: L 的连带 —— 改一句平台提示词就动字节账那张网。**逐个声称，不许写「一堆」。**
#:
#: ⚠️ 第一版凭印象写了四个名字，**实测四个里只对了两个**：
#:   * `..._platform_prompt_bytes_match_the_shipped_prompts` —— 这个名字
#:     **仓里不存在**（真名是 `..._platform_prompt_sizes_are_spelled_once_per_package`）；
#:   * `test_we_did_not_buy_this_with_prompt_bytes` —— **存在而没红**，
#:     因为它钉的是 `REQUEST_OVERHEAD_BYTES` 这个**常数**，
#:     而变异改的是提示词。抓这一刀的是
#:     `..._overhead_constants_are_measured_from_the_real_payload`（它现量现比）。
#:     **两条用例名字挨着、判的不是同一件事** —— 这就是「凭印象写连带」的代价。
#: 下面这四个是实测读回来的。
BYTE_WEB = [
    "test_the_empty_request_baseline_is_measured_from_the_real_payload",
    "test_the_floor_equals_the_request_it_claims_to_measure",
    "test_the_overhead_constants_are_measured_from_the_real_payload",
    "test_the_platform_prompt_sizes_are_spelled_once_per_package",
]


def main() -> int:
    run = suite_runner(ROOT, PYTHON, [TARGET])
    control = control_runner(ROOT, PYTHON, [TARGET])

    probes: list[tuple[str, list[Mutation], Sequence[str], Sequence[str]]] = [
        (
            "A · 生成步不转发记忆",
            [Mutation(MODEL, FORWARDS, FORWARDS_NOTHING)],
            ["test_the_generate_request_carries_the_memories_with_their_refs"],
            [],
        ),
        (
            "B · 形状退回 {field, value}",
            [Mutation(MEMORY, SHAPE_IS_REF, SHAPE_IS_FIELD)],
            [
                "test_the_generate_request_carries_the_memories_with_their_refs",
                "test_the_shape_the_generate_step_gets_is_ref_and_value",
                "test_a_memory_this_turn_did_send_passes",
            ],
            [],
        ),
        (
            "C · 记忆引用那道闸整条关掉",
            [Mutation(VALIDATION, GATE_CALLED, GATE_SKIPPED)],
            [
                "test_a_claim_may_not_cite_a_memory_this_turn_did_not_send",
                "test_a_turn_that_sent_no_memory_allows_no_memory_ref",
                "test_the_refusal_carries_the_ref_and_not_the_remembered_value",
            ],
            [],
        ),
        (
            "D · 默认翻成 fail-open",
            [Mutation(VALIDATION, FAILS_CLOSED, FAILS_OPEN)],
            ["test_a_turn_that_sent_no_memory_allows_no_memory_ref"],
            [],
        ),
        (
            "E · 闸不认前缀",
            [Mutation(VALIDATION, CHECKS_PREFIX, CHECKS_EVERYTHING)],
            ["test_a_non_memory_ref_is_not_judged_by_this_gate"],
            KNOWLEDGE_FAMILY,
        ),
        (
            "F · 校验器自己再算一份",
            [Mutation(PIPELINE, READS_ONE_LIST, READS_ITS_OWN)],
            ["test_the_prompt_and_the_validator_read_one_list"],
            [],
        ),
        (
            "G · 留痕两个键只转了一个",
            [Mutation(PIPELINE, BOTH_CONVERTED, ONE_CONVERTED)],
            ["test_the_citation_row_records_keys_and_never_values"],
            [],
        ),
        (
            "H · 调用点喂记忆的值",
            [Mutation(PIPELINE, FEEDS_REFS, FEEDS_VALUES)],
            ["test_the_row_is_handed_refs_and_never_the_values"],
            [],
        ),
        (
            "I · 两行不成对",
            [Mutation(PIPELINE, PAIRED, UNPAIRED)],
            ["test_the_two_rows_are_a_pair"],
            [],
        ),
        (
            "J · ref 带上一个假版本号",
            [Mutation(MEMORY, NO_VERSION, FAKE_VERSION)],
            [
                "test_the_generate_request_carries_the_memories_with_their_refs",
                "test_the_ref_is_built_in_exactly_one_place",
                "test_the_ref_carries_no_version_and_says_so",
                "test_a_memory_this_turn_did_send_passes",
            ],
            [],
        ),
        (
            "K · 前缀就地拼一次",
            [Mutation(VALIDATION, USES_THE_CONSTANT, BUILDS_IT_INLINE)],
            ["test_the_ref_is_built_in_exactly_one_place"],
            [],
        ),
        (
            "L · 系统提示不讲怎么用",
            [Mutation(MODEL, SYSTEM_EXPLAINS, SYSTEM_SILENT)],
            ["test_the_system_prompt_tells_it_to_cite_the_ref"],
            BYTE_WEB,
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
            names = set(failed_names(result.output))
            hit = names == set(expects)
            elsewhere = set(result.collateral) == set(collateral)
            verdicts.append((label, result.went_red, hit, elsewhere))
            print(result)
            if result.went_red and not hit:
                print(
                    "    ⚠️ 红了，但不是声称的那一组："
                    f"多红 {sorted(names - set(expects))}；"
                    f"没红 {sorted(set(expects) - names)}"
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
    return 1 if (silent or misaimed or drifted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
