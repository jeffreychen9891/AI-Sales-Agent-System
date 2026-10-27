"""运行期那几份契约里，**每一个无界字段都必须被分过类**（批 2，2026-09-15）。

## 这条守卫为什么长成「豁免表」而不是「一律要有界」

批 2 普查的结论**和开工时的预期相反**，而形状是被那个结论逼出来的：

* **配包侧（`merchant-profile`）45 处无界，而它们不是洞** ——
  admission 的固定底检查（`registry.request_floor_bytes` 比 `MAX_MODEL_INPUT_BYTES`）
  **整体**兜住它们：封版期算出来装不下就拒包。
  逐个给配包字段加界既无必要，**又会压死旧守卫**（见下）。
* **运行期侧才需要逐字段看** —— 固定底检查算的是**空回合**，
  运行期才有的值（槽位值、understanding、focus …）它看不见。

所以这条守卫只扫运行期那三份，而且判的不是「有没有界」，
是**「这一项有没有被人分过类」**：要么契约上有界，要么列进下面这张表并写明
它为什么不需要 —— **不进 payload / 有执行器兜住 / 今天零实现而落地时必须同批给界**。

**新加一个无界字段就会红**，红的时候人要做的是回答「它进不进 payload」，
而不是顺手加一个 `maxLength`。

## 加一条界会让旧守卫变空 —— 本批实测

给 `goal` 加 `maxLength: 1200` 之后，`test_a_goal_too_long_for_the_plan_request_is_refused`
的夹具**在契约校验就被拒**，走不到那道字节闸 ——
它会从「守固定底」悄悄退化成「守契约拒超长字符串」，**而它照旧是红的**。
处置是换一个今天仍无界的杠杆（`exit_conditions`），不是放宽那条界。
**这一条记在这里，是因为下一个往表里加界的人会再撞一次。**

## 扫描器自己的两个盲区，都是交叉核对才发现的

1. **联合类型** `{"type": ["string", "null"]}` —— 第一版只判 `t == "string"`，
   于是 `typed_value` / `knowledge_query` / `memory_cursor` 全漏了；
2. **`pattern` 是界但不写成 `maxLength`** —— `strategy_reason_code` 的
   `^[A-Z][A-Z0-9_]{2,63}$` 上界 64，第一版把它误报成无界。

**第三个盲区没有修，而是写下来并钉住**：本扫描器**不跟进 `$ref`**。
下面 `KNOWN_REFS` 把今天那几处 `$ref` 钉成等值集合 ——
**新出现一个 `$ref` 也会红**，因为那一处的边界这只扫描器看不见。
（`AGENTS.md`：一条「没有任何 X」式的断言，今天为真，明天也可能只是因为它什么都没检查。）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "packages" / "contracts"

#: 只扫**运行期**那三份 —— 配包侧由 admission 的固定底检查整体兜住，见 docstring。
SCANNED = (
    CONTRACTS / "schemas" / "conversation-aggregate.schema.json",
    CONTRACTS / "inference" / "understanding-result.schema.json",
    CONTRACTS / "inference" / "plan-result.schema.json",
    #: **第四步的结果也进 payload**（ADR-0149）：它的 `reading` 与
    #: `action.because` 都被计划步的提示词读走。落地时三个字段都有界，
    #: 所以进这张表**今天一条豁免都不需要** —— 它守的是**下一个**往里面
    #: 加字段的人：不进这张表的话，那时候没有任何东西会红。
    CONTRACTS / "inference" / "silence-reading-result.schema.json",
)

#: 无界但**已被分类**的字段：路径 -> 它为什么不需要界。
#:
#: 每一条的理由是照着 `runtime.request_shape` 那三个函数的实际字段逐条对出来的，
#: **不是照着字段名猜的** —— 「进不进 payload」这件事只有那三个函数说了算。
EXEMPT: dict[str, str] = {
    # ---- conversation-aggregate：只有**栈顶那一帧**的几个字段进请求 ----
    ".target_stack": "不进 payload：只有栈顶那一帧进，栈的深度以 `stack_depth` 一个整数进",
    ".target_stack[items].definition_id": "不进 payload：三个 user_prompt 都不带它",
    ".target_stack[items].slot_evidence": "不进 payload：证据留在聚合里，模型看到的是 `slots` 的值",
    ".target_stack[items].interruption_reason": "不进 payload",
    ".annotation_refs": "不进 payload",
    ".active_intent_refs": "不进 payload",
    ".scheduled_event_refs": "不进 payload",
    ".pending_exit_signal.relation": "不进 payload",
    ".last_transition.reason": "不进 payload",
    ".last_transition.policy_version": "不进 payload",
    ".last_transition.model_version": "不进 payload",
    ".last_transition.profile_version": "不进 payload",
    # ---- 今天零实现，而落地那一批必须同批给界 ----
    ".memory_cursor": "B1-3 零实现，今天不进 payload。**落地时请求会随对话长度增长，"
    "那一批必须同批给它界** —— 它正是安全余量那 5,000 留格的第一项",
    ".summary_ref": "同 `memory_cursor`：rolling summary 的产物今天不存在",
    # ---- 进 payload，而有执行器兜住 ----
    ".facts[items].typed_value": "进 plan + generate，**而由 `runtime.understanding_bound` "
    "执行**（1,580 字节，丢而不截）—— 界在执行器上，不在契约上",
    ".knowledge_query": "不进 payload：它交给检索，而 `retrieval.QUERY_MAX_CHARS = 200`",
}

#: 本扫描器**不跟进**的 `$ref`。钉成等值集合 —— 新出现一个就红。
KNOWN_REFS: dict[str, str] = {
    ".model_evidence -> model-evidence.schema.json": "进 plan + generate 的 understanding 里，"
    "同样由 `understanding_bound` 兜住",
    ".confidence -> confidence.schema.json": "一个 number，没有长度可言",
    #: ADR-0150 的 `carried_slots`：**这两处 `$ref` 指的是同一份文件里
    #: `target_stack[items]` 的那两格**，所以它们的界就是那两格的界
    #: （`slots` 有 `maxProperties: 10` + 逐值 `maxLength: 400`，
    #: `slot_evidence` 是第 71 行那条「不进 payload」）。
    #: **用 `$ref` 而不是抄一遍，正是为了不让那两处将来分家** ——
    #: 本仓「同一条规则两个定义点」那一族。
    ".carried_slots[additionalProperties].slots -> slots": "指向 `target_stack[items].slots`，"
    "界随它（`maxProperties: 10`，逐值 `maxLength: 400`）",
    ".carried_slots[additionalProperties].slot_evidence -> slot_evidence": "指向 "
    "`target_stack[items].slot_evidence`，同样不进 payload",
}


def _bounded_string(node: dict[str, Any]) -> bool:
    """字符串有没有界。**`pattern` / `enum` / `format` / `const` 都算界。**"""

    return any(key in node for key in ("maxLength", "enum", "format", "pattern", "const"))


def _is_string(declared: Any) -> bool:
    """**联合类型也算字符串** —— `{"type": ["string", "null"]}` 是本扫描器第一个盲区。"""

    return declared == "string" or (isinstance(declared, list) and "string" in declared)


def _walk(
    node: Any, path: str, unbounded: dict[str, str], refs: dict[str, str]
) -> None:
    if not isinstance(node, dict):
        return
    if "$ref" in node:
        refs[f"{path} -> {str(node['$ref']).rsplit('/', 1)[-1]}"] = ""
    declared = node.get("type")
    if _is_string(declared) and not _bounded_string(node):
        unbounded[path] = "string 无 maxLength"
    if declared == "array" and "maxItems" not in node:
        unbounded[path] = "array 无 maxItems"
    if (
        declared == "object"
        and isinstance(node.get("additionalProperties"), dict)
        and "maxProperties" not in node
    ):
        unbounded[path] = "开放 object 无 maxProperties"

    for name, sub in (node.get("properties") or {}).items():
        _walk(sub, f"{path}.{name}", unbounded, refs)
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            _walk(node[key], f"{path}[{key}]", unbounded, refs)
    for key in ("oneOf", "anyOf", "allOf"):
        for index, sub in enumerate(node.get(key) or []):
            _walk(sub, f"{path}<{index}>", unbounded, refs)
    for name, sub in (node.get("$defs") or {}).items():
        _walk(sub, f"{path}#{name}", unbounded, refs)


def _scan() -> tuple[dict[str, str], dict[str, str]]:
    unbounded: dict[str, str] = {}
    refs: dict[str, str] = {}
    for path in SCANNED:
        _walk(json.loads(path.read_text(encoding="utf-8")), "", unbounded, refs)
    return unbounded, refs


def test_every_unbounded_runtime_field_has_been_classified() -> None:
    """无界字段的集合 **等于** 豁免表的键集 —— 不是「包含于」。

    **等值而不是包含**：多出来的是新加的无界字段（要分类），
    少掉的是豁免表里挂着一条已经不存在的路径（那条理由在守一个空位）。
    两个方向都该红，而「包含」只看得见一个。
    """

    unbounded, _ = _scan()
    extra = sorted(set(unbounded) - set(EXEMPT))
    stale = sorted(set(EXEMPT) - set(unbounded))

    assert not extra, (
        f"这几个运行期字段无界，而没有人分过类：{extra}。"
        "**先答「它进不进 payload」，再决定是给界还是写进 EXEMPT** —— "
        "顺手加一个 maxLength 可能会让某条旧守卫的夹具失效（见本文件 docstring）"
    )
    assert not stale, (
        f"EXEMPT 里这几条已经不无界了：{stale}。"
        "它们的理由现在守着一个空位 —— 删掉，否则下一个人会以为这里还有账"
    )


def test_every_exemption_carries_a_reason() -> None:
    """豁免要写理由 —— 一张只有路径的表，等于把「为什么」留在某个人的记忆里。"""

    empty = sorted(path for path, reason in EXEMPT.items() if not reason.strip())
    assert not empty, f"这几条豁免没写理由：{empty}"


def test_the_refs_this_scanner_cannot_follow_are_pinned() -> None:
    """**扫描器的盲区自己也要被钉住。**

    本扫描器不跟进 `$ref`，所以被引的那几份 schema 里的边界它看不见。
    把今天这几处钉成等值集合：新出现一个 `$ref`，这一条红 ——
    红的含义是「这里有一块本守卫看不见的地方，去人工看一眼」。

    **不这么写的话，这份扫描会在它最该说话的时候保持沉默** ——
    而本仓记过那个形状（一条「没有任何 X」的断言，可能只是因为它什么都没检查）。
    """

    _, refs = _scan()
    assert set(refs) == set(KNOWN_REFS), (
        f"本扫描器不跟进的 `$ref` 变了：新增 {sorted(set(refs) - set(KNOWN_REFS))}、"
        f"消失 {sorted(set(KNOWN_REFS) - set(refs))}"
    )
