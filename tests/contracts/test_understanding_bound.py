"""`understanding` 的字节上界（ADR-0132，T282 到期）。

**这一批的兑现物是本文件第一条用例，不是 ADR 那段文字**：拿**契约允许的最大**
understanding 走一遍，断言它进得了预算。那条用例在 `understanding_bound`
存在之前是红的 —— 110,887 对 1,580。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.runtime.understanding_bound import (
    UNDERSTANDING_PROMPT_BYTES,
    bound_for_prompt,
    wire_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "packages/contracts/inference/understanding-result.schema.json"


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def _fill(
    node: dict[str, Any], *, filler: str, items: int | None = None, text_len: int | None = None
) -> Any:
    """按 schema 造一个顶到上界的**合法**值。

    **不手写一份「很大的 understanding」**：手写的那份会随契约漂，
    而漂的方向是变小 —— 于是用例会在契约放宽的当天悄悄变得不够严。
    """

    kinds = node.get("type")
    kind = next((k for k in kinds if k != "null"), "null") if isinstance(kinds, list) else kinds

    if kind == "object":
        props = node.get("properties") or {}
        return {
            name: _fill(props[name], filler=filler, items=items, text_len=text_len)
            for name in (node.get("required") or list(props))
            if name in props
        }
    if kind == "array":
        count = items if items is not None else (node.get("maxItems") or 1)
        item = node.get("items") or {"type": "string"}
        return [_fill(item, filler=filler, items=items, text_len=text_len) for _ in range(count)]
    if kind == "string":
        if node.get("enum"):
            return node["enum"][0]
        if node.get("format") == "uuid":
            return "11111111-2222-4333-8444-555555555555"
        if node.get("pattern") and "a-f0-9" in node["pattern"]:
            return "a" * 64
        longest = node.get("maxLength") or 32
        return filler * (min(longest, text_len) if text_len else longest)
    if kind == "number":
        return 0.5
    if kind == "integer":
        return 1
    if kind == "boolean":
        return True
    return None


def _maximal(filler: str = "中") -> dict[str, Any]:
    return _fill(_schema(), filler=filler)


# ---------------------------------------------------------------------------
# 一、保证
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filler", ["中", "x"])
def test_the_largest_legal_understanding_fits_after_bounding(filler: str) -> None:
    """**本批存在的理由。**

    一份完全合法、过得了全部契约校验的 understanding，在本模块之前
    能单独把请求撑到 `MAX_MODEL_INPUT_BYTES` 的 6.8 倍（全中文）。
    """

    document = _maximal(filler)
    assert wire_bytes(document) > UNDERSTANDING_PROMPT_BYTES * 10, (
        "这份样本没到该有的量级，用例就不再证明它声称的事"
    )

    bounded = bound_for_prompt(document)

    assert wire_bytes(bounded.document) <= UNDERSTANDING_PROMPT_BYTES
    assert bounded.anything_dropped


def _projection(document: dict) -> dict:
    """`bound_for_prompt` 眼里的那一份 —— **摘掉不进提示词的字段**。

    ⚠️ **2026-09-20（T200）新增。** `turn_summary` 是理解步的**写侧**产出：
    管线拿它往 `conversation_summaries` 追一行，而 plan / generate 看到的是
    滚动摘要那一份独立份额。它在合同里最长 480 **字符**（中文 1,440 字节），
    **顶着上界会让这个函数的地板从 793 涨到 2,233，而上界是 1,580** ——
    那时它 docstring 里承诺的「不存在丢无可丢仍然超限的输入」就不成立了。

    所以摘除写在 `understanding_bound._NOT_IN_PROMPT` 里，**不在调用方**。
    本文件这两条因此要按投影来算：**保证是关于投影的，不是关于整份文档的。**
    """

    from fastagent_v2.runtime.understanding_bound import _NOT_IN_PROMPT

    return {key: value for key, value in document.items() if key not in _NOT_IN_PROMPT}


def test_nothing_is_dropped_from_a_document_that_already_fits() -> None:
    """**反面，而它是这一组里最要紧的一条。**

    没有它，一个「一律只留 3 条」的实现会让上面那条绿 ——
    而那会让绝大多数正常回合白白丢掉事实。
    """

    # **至少有一个数组长过 3 条** —— 反向探针 H 抓到的：每个数组各 1 条时，
    # 一个「一律只留 3 条」的实现在这条用例上是**绿的**（1 ≤ 3，什么都没丢），
    # 于是它并没有在做 docstring 声称的那件事。
    #
    # 而**不能每个数组都放 5 条**：那是 3,443 字节，本身就超了 1,580 ——
    # 顺带量出来的一件事：**这个上界是紧的**，每个数组各 2 条（1,559）
    # 就几乎顶满。所以取「重字段各 1 条 +  5 条」= 1,163。
    document = _fill(_schema(), filler="中", items=1, text_len=8)
    document["uncertainties"] = [
        {"field": "中中中中", "reason": "中中中中中中中中"} for _ in range(5)
    ]
    assert wire_bytes(document) <= UNDERSTANDING_PROMPT_BYTES, "样本本身就超限了，改小再测"

    bounded = bound_for_prompt(document)

    # **比的是投影，不是原文档**（T200，2026-09-20）：`turn_summary` 按设计
    # 不进这一份，理由见 `_projection`。比原文档的话这一条会红在一次
    # **有意的摘除**上，而它要守的是「装得下就一条都别丢」。
    assert bounded.document == _projection(document)
    assert bounded.dropped == {}
    assert not bounded.anything_dropped


# ---------------------------------------------------------------------------
# 二、丢什么、按什么顺序
# ---------------------------------------------------------------------------


def test_uncertainties_go_before_facts() -> None:
    """`uncertainties` 是第二大的字段，而 T282 原文一个字没提它。

    只盯 `facts` 的上界会在它上面原样重演 —— 这条用例把顺序钉住。
    """

    document = _maximal()
    bounded = bound_for_prompt(document)

    assert bounded.dropped.get("uncertainties") == len(document["uncertainties"]), (
        "uncertainties 没有被先丢干净"
    )
    assert len(bounded.document.get("facts", [])) <= len(document["facts"])


def test_safety_markers_are_never_dropped() -> None:
    """**丢一条安全标记去省几十个字节，是拿错方向换预算。**"""

    document = _maximal()
    bounded = bound_for_prompt(document)

    assert bounded.document["risk_markers"] == document["risk_markers"]
    assert "risk_markers" not in bounded.dropped
    for kept in ("schema_version", "event_id", "interruption", "sentiment"):
        assert bounded.document[kept] == document[kept]


def test_the_lowest_confidence_entries_are_the_ones_dropped() -> None:
    """留下的每一条都完整，而**丢掉的是模型自己最不确定的那些**。"""

    document = _maximal()
    document["uncertainties"] = []
    document["customer_commitments"] = []
    document["intents"] = []
    document["slot_updates"] = []
    facts = document["facts"]
    for position, fact in enumerate(facts):
        fact["confidence"] = round(position / len(facts), 4)

    bounded = bound_for_prompt(document)
    kept = bounded.document["facts"]

    assert kept, "全丢光了，这条用例就没在测顺序"
    assert len(kept) < len(facts)
    lowest_kept = min(fact["confidence"] for fact in kept)
    highest_dropped = max(
        fact["confidence"] for fact in facts if fact not in kept
    )
    assert lowest_kept > highest_dropped


def test_bounding_is_deterministic() -> None:
    """同一份输入两次跑要给出同一个请求。

    不然同一个回合重放会得到两个不同的 payload，
    而「为什么这次超了上次没超」就查不下去了。
    """

    document = _maximal()
    first = bound_for_prompt(document)
    second = bound_for_prompt(document)

    assert first.document == second.document
    assert dict(first.dropped) == dict(second.dropped)


def test_every_kept_entry_is_whole() -> None:
    """**丢弃，不是截断**（ADR-0132 §三）。

    截断会得到一条看起来合法而意思变了的事实，
    下游分不出「客户只说了一半」和「我们砍了一半」。
    """

    document = _maximal()
    bounded = bound_for_prompt(document)

    for field, kept_items in bounded.document.items():
        if not isinstance(kept_items, list):
            continue
        for item in kept_items:
            assert item in document[field], f"{field} 里有一条被改过，而不是被丢掉"


# ---------------------------------------------------------------------------
# 三、这个数与预算表是同一个数
# ---------------------------------------------------------------------------


def test_the_bound_is_the_number_the_budget_reserves() -> None:
    """**账本与闸不是同一把尺子** —— 这个仓踩过三次，这条是不让它第四次。

    `MATERIAL_BYTES_BUDGET` 的推导式里给 understanding 留了一项，
    而**执行那一项的是 `UNDERSTANDING_PROMPT_BYTES`**。两处对不上时，
    预算表按一个数分配、放行按另一个数 —— 而两边都不会报警。

    **第一版是拿正则去扫 `material_selection.py` 的注释**，因为那时那个数
    只活在注释里。字节口径那批的独立审查（2026-09-12 P1-1）把 `1,580` 与
    `1,000` 都变成了具名常数，所以这里改成直接 import ——
    **一条扫注释的用例会在注释换个写法的那天悄悄失效**，
    而那正是它声称要防的那类事。
    """

    from fastagent_v2.worker.material_selection import UNDERSTANDING_BYTES_RESERVED

    assert UNDERSTANDING_PROMPT_BYTES == UNDERSTANDING_BYTES_RESERVED, (
        f"预算表给 understanding 留的是 {UNDERSTANDING_BYTES_RESERVED}，"
        f"而执行者用的是 {UNDERSTANDING_PROMPT_BYTES}"
    )

def test_the_bound_is_measured_with_the_same_ruler_the_request_uses() -> None:
    """**钉尺子，不只钉数字** —— 独立审查 2026-09-12，P1-1。

    上一条钉的是 `1580 == 1580`。**那钉不住这一批真正出错的地方**：
    第一版的 `wire_bytes` 自己写了个 `json.dumps(…, separators=(",", ":"))`，
    数字两边都是 1,580 而**尺子短了 7%–16%**，方向 fail-open ——
    于是兑现物用例绿着，bound 完的文档真实占 1,601 > 1,580。

    这一条拿**真实请求的增量**当参照：把同一份 understanding 塞进
    `providers.request_payload` 造的 payload，量「带它」与「不带它」的差。

    残差恒为 **+2**，而那 2 不是误差：baseline 里 `"understanding": {}`
    那个空对象自己占 2 字节。**钉成 `== 2` 而不是 `<= 某个容差`** ——
    容差正是「平均够接近」那一类，而这个仓已经为它付过三次账。
    """

    import json as _json

    from fastagent_v2.runtime.understanding_bound import wire_bytes
    from fastagent_v2.worker import inference_model
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import _WIRE_JSON, request_payload

    binding = active_release().role("GENERATE_VALUE")

    def request_size(understanding: dict[str, Any]) -> int:
        user_prompt = _json.dumps(
            {
                "strategy_id": "s",
                "strategy_reason_code": "r",
                "generation_constraints": [],
                "candidate_intents": [],
                "required_slot_requests": [],
                "understanding": understanding,
                "event_type": "customer_message",
                "content_materials": [],
            },
            ensure_ascii=False,
        )
        payload = request_payload(
            binding,
            system_prompt=inference_model._GENERATE_SYSTEM,
            user_prompt=user_prompt,
        )
        return len(_json.dumps(payload, **_WIRE_JSON).encode("utf-8"))

    empty = request_size({})
    for filler in ("中", "x"):
        document = bound_for_prompt(_maximal(filler)).document
        real_delta = request_size(document) - empty
        assert wire_bytes(document) - real_delta == 2, (
            f"[{filler}] 尺子对不上真实请求：量得 {wire_bytes(document)}，"
            f"真实增量 {real_delta}"
        )
        # 而这才是「它进得了预算」那句话的真正意思
        assert real_delta <= UNDERSTANDING_PROMPT_BYTES


def test_the_floor_is_the_droppable_ones_empty_plus_the_undroppable_ones_maxed() -> None:
    """终止保证的证明，**用的要是真正的地板**（独立审查 P3-1）。

    模块 docstring 先前写「可丢字段清空之后剩下『合法但全空』那一档（413）」——
    **那是两份不同的文档**：真正的地板是「可丢的全空 **+ 不可丢的顶到契约上限**」。

    保证仍然成立，而**写下来的证明要能被重量**。这条用例同时是一个哨兵：
    `risk_markers.maxItems` 或任何不可丢字段的 `maxLength` 长大时它会红，
    **而今天没有别的东西看着那件事**。
    """

    from fastagent_v2.runtime.understanding_bound import _DROP_ORDER

    for filler in ("中", "x"):
        floor = _projection(_maximal(filler))
        for field in _DROP_ORDER:
            floor[field] = []
        assert wire_bytes(floor) < UNDERSTANDING_PROMPT_BYTES, (
            f"[{filler}] 地板 {wire_bytes(floor)} 已经顶到上界 "
            f"{UNDERSTANDING_PROMPT_BYTES} —— 这个函数不再给得出保证"
        )


def test_the_counter_separates_plan_from_generate() -> None:
    """**不分 stage 的话这个计数器读到的是真实丢弃数的两倍**（独立审查 P2-3）。

    plan 与 generate 各过一次上界（那件事本身是对的：两个请求各自独立比
    `MAX_MODEL_INPUT_BYTES`），而 `pipeline._understanding_for_plan` 只替换
    `interruption.relation` 一个短字符串 —— 两次输入几乎同样大、丢的条数相同。

    而这个计数器存在的全部理由是那道算术题：「模型给了 40 条而平台丢了 38 条」。
    **读成 76 就答不了它。**
    """

    from fastagent_v2.worker.inference_model import _bounded_understanding
    from fastagent_v2.worker.metrics import UNDERSTANDING_ENTRIES_DROPPED_TOTAL

    document = _maximal("中")
    before = {
        stage: UNDERSTANDING_ENTRIES_DROPPED_TOTAL.value(stage, "facts")
        for stage in ("plan", "generate")
    }

    _bounded_understanding(document, stage="plan")
    _bounded_understanding(document, stage="generate")

    after = {
        stage: UNDERSTANDING_ENTRIES_DROPPED_TOTAL.value(stage, "facts")
        for stage in ("plan", "generate")
    }
    delta = {stage: after[stage] - before[stage] for stage in after}

    assert delta["plan"] > 0, "plan 那一侧没计数"
    assert delta["plan"] == delta["generate"], (
        "两侧丢的条数应当一样 —— 它们看的是几乎同一份文档"
    )


def test_the_table_in_the_docstring_is_recomputed_from_todays_limit() -> None:
    """模块 docstring 那张表的**倍数列**，由这一条替它重算。

    ## 它防的是什么

    那张表原本只有一列「对 **16,384**」（2.92 × / 6.96 ×），
    而 2026-09-15 预算链那一批把 `MAX_MODEL_INPUT_BYTES` 抬到 **49,000**。
    **抬完之后没有人回来改这张表** —— 于是它说的 2.92 × 变成了 **0.98 ×**：
    全 ASCII 那一行**已经装得下了**，而表还写着它超了三倍。

    独立复核 2026-09-15（成本闸 §五）列出三处这样的失真数，
    **三处一条都没修，直到 2026-09-16 本笔**。
    `CLAUDE.md` 第二节要的是：这类断言**配一条会在它失效时变红的守卫**。
    这就是那一条 —— 常量再动一次，下面两个字符串就对不上，这里红，
    **并且指着那张表说「改我」**。

    ## 为什么钉的是倍数而不是字节数

    两个字节数（47,855 / 113,967）是**契约允许的最大**，它们随契约的
    `maxItems` / `maxLength` 变，不随上限变；倍数是两者的比，
    **而失效的正是这个比**。上面那条
    `test_the_bound_is_measured_with_the_same_ruler_the_request_uses`
    管的是「尺子是同一把」，这一条管的是「算出来的那个数写对了没有」。
    """

    from fastagent_v2.runtime import understanding_bound
    from fastagent_v2.worker.cost import MAX_MODEL_INPUT_BYTES

    #: 那张表里的两个字节数 —— 它们是被量出来的，这条用例不重量，只重算比值。
    ascii_worst = 47855
    chinese_worst = 113967

    docstring = understanding_bound.__doc__ or ""

    for label, measured in (("全 ASCII", ascii_worst), ("全中文", chinese_worst)):
        ratio = f"{measured / MAX_MODEL_INPUT_BYTES:.2f} ×"
        assert ratio in docstring, (
            f"{label} 那一行按今天的上限 {MAX_MODEL_INPUT_BYTES:,} 应当是 {ratio}，"
            f"而 docstring 里没有这个数。"
            "\n**上限动过了，而那张表没跟着动** —— 去改本模块 docstring 顶上那张表，"
            "并顺带看一眼它下面那句「可以单独把请求撑到上限的 N 倍」。"
        )

    headline = f"{chinese_worst / MAX_MODEL_INPUT_BYTES:.1f} 倍"
    assert headline in docstring, (
        f"表下面那句话按今天的上限应当是「{headline}」。"
        "\n它与上面那张表是同一个数的两种写法，**一起改，不要只改一处**。"
    )
