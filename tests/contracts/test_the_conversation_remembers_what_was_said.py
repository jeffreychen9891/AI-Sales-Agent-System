"""T200 / E4 第 4 批：**这通对话聊过什么，终于进了提示词 —— 连同它的预算。**

## 在本批之前，模型每一轮都不知道之前聊过什么

`pipeline.py` 里那段注释逐字写着（本批已划掉更正）：

> nothing anywhere puts prior turns into a prompt, so a model request does not
> grow with the length of the conversation

**而 ADR-0122 §二.2 正是据它判 B4-6「上下文预算」已交付并关格**：
风险源不存在，就没有东西需要预算。§6.1 同时写好了它自己的失效条件 ——
「**谁把历史写进提示词，谁就让这段实测失效**」。**本批就是那一笔。**

## 所以这一批不可拆，而「不可拆」是一条写死的继承项

ADR-0122 §三：

> **谁把历史 / 摘要写进提示词，谁在同一批里建上下文预算。**
> 写死，不得再写成任何一种自指。

排期表 §4.4 第 5 项同样写着「与 4 **同批**」，并注明
「**不同批 = 违反一条写死的继承项**」。
所以本文件里**摘要那几条与预算那几条是同一批的两半**，
拆开任何一半，另一半就失去了它存在的理由。

## 三个裁定，各自落在哪一条用例上

* **裁定八**（用户 09-12）：摘要人工可编辑，**模型不能自改** ——
  `test_the_model_never_gets_the_earlier_lines_to_rewrite`；
* **裁定九**（作者提、用户 09-12 确认）：第一版**只作废**，不逐字改、
  不新增 route —— `test_invalidating_is_the_only_edit_and_it_rides_an_existing_route`；
* **用户 09-20**：每轮一句 delta · 新表 · **2,400 字节** ——
  `test_the_budget_is_one_number_in_two_places` 等。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

from fastagent_v2.runtime.summary_prompt import (
    SUMMARY_LINE_BYTES,
    SUMMARY_PROMPT_BYTES,
    clip_line,
    summary_for_prompt,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
APP = ROOT / "services/api/src/fastagent_v2/api/app.py"
MIGRATION = ROOT / "infrastructure/migrations/071_conversation_summaries.sql"
CONTRACT = ROOT / "packages/contracts/inference/understanding-result.schema.json"


def _statements_only(node: ast.FunctionDef) -> str:
    """那个函数的**语句**，docstring 剥掉。

    ⚠️ 本仓踩过八次「守卫读到自己的散文」，而本文件会是第九次：
    下面判「写摘要那一处不读已有的行」，而那个函数的 docstring 里
    **逐字写着**「它既不读旧行也不写旧行」。
    """

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _functions(path: pathlib.Path) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
    }


# ---------------------------------------------------------------------------
# 一、预算 —— ADR-0122 §三 那条继承项的兑现物
# ---------------------------------------------------------------------------


def test_the_request_does_not_grow_without_bound() -> None:
    """**排期表 §4.6 那条「预算判据（硬）」的兑现物。**

    原文逐字：

    > 摘要进提示词之后，同一条会话跑到第 N 轮，`worst_case_input_tokens()`
    > 仍算得出一个**不随 N 无界增长**的上界。

    所以这一条按 N 量，而不是量一个固定样本 ——
    **一个固定样本证明不了「不随 N 增长」这件事**。
    """

    line = "运" * 40
    sizes = []
    for turns in (1, 10, 100, 1_000, 10_000):
        kept, dropped = summary_for_prompt([line] * turns)
        used = sum(len(item.encode("utf-8")) for item in kept) + max(0, len(kept) - 1)
        sizes.append(used)
        assert used <= SUMMARY_PROMPT_BYTES, (
            f"N={turns} 时摘要占 {used} 字节，超过了 {SUMMARY_PROMPT_BYTES} —— "
            "**这正是 ADR-0122 §三 要这一批建预算的那件事**：请求随会话长度增长。"
        )
        if turns >= 100:
            assert dropped > 0, f"N={turns} 还一行都没丢，样本没到该有的量级"

    assert max(sizes) - min(sizes[2:]) < len(line.encode("utf-8")) + 1, (
        f"N 从 100 涨到 10,000，摘要字节还在变（{sizes}）—— 那不叫有界"
    )


def test_the_budget_is_one_number_in_two_places() -> None:
    """**摘要那一行的预留，与它真的送进去的那个界，必须相等。**

    两处各写一个数，就是那条推导式漂掉的方式，而漂的方向是 **fail-open**：
    预算以为自己扣够了，而实际送进去的更多。
    与 `REMEMBERED_BYTES_RESERVED` / `LAST_OUTBOUND_BYTES_RESERVED` 同一种守法。
    """

    from fastagent_v2.worker.material_selection import SUMMARY_BYTES_RESERVED

    assert SUMMARY_BYTES_RESERVED == SUMMARY_PROMPT_BYTES, (
        f"推导式里预留 {SUMMARY_BYTES_RESERVED}，而真送进去的界是 "
        f"{SUMMARY_PROMPT_BYTES} —— 不等就有一个是谎话，"
        "而差额那部分**不属于任何一格账**（`AGENTS.md` 判据 2 的反面）。"
    )


def test_the_line_ceiling_is_one_number_in_two_places() -> None:
    """一行的上界：代码按**字节**、迁移按**字符**，而库那一侧必须更宽。

    ⚠️ **反过来（库更严）会让一条合法的行写不进去**，
    而那时报错来自数据库、不来自校验器，排查要绕一圈。
    """

    check = re.search(
        r"CHECK \(length\(line\) BETWEEN 1 AND (\d+)\)",
        MIGRATION.read_text(encoding="utf-8"),
    )
    assert check, "迁移里那条 `line` 的 CHECK 不见了"
    characters = int(check.group(1))

    assert characters == SUMMARY_LINE_BYTES, (
        f"迁移写 {characters}，`SUMMARY_LINE_BYTES` 是 {SUMMARY_LINE_BYTES} —— "
        "两处是同一个数（单位不同：那边字符、这边字节，所以库更宽）"
    )
    # 而「库更宽」这件事本身也要成立：一行 480 汉字 = 1,440 字节 > 480。
    assert len(("运" * characters).encode("utf-8")) > SUMMARY_LINE_BYTES


def test_a_long_line_is_clipped_by_bytes_not_characters() -> None:
    """**按字节裁**。本仓为这个差别付过一次钱（把字符当字节，一份三条中文声明
    就超了整个请求预算）。"""

    clipped = clip_line("运" * 1000)
    assert len(clipped.encode("utf-8")) <= SUMMARY_LINE_BYTES
    # 而且不能裁出半个字
    assert clipped == clipped.encode("utf-8").decode("utf-8")


def test_the_oldest_lines_are_the_ones_dropped() -> None:
    """留最近的，丢最旧的 —— **而这不是无代价的，代价写在函数的 docstring 里。**"""

    # ⚠️ **样本要真的超过预算，否则这条用例什么都没验**。
    # 第一版用的是 `f"第{index}轮"`（约 10 字节）× 200 = 约 2,000 字节，
    # **装得下**，于是 `dropped` 是 0 而断言当场红 —— 本仓那条
    # 「夹具不能恰好等于错误答案」的又一次。这里一行 120 字节 × 100 = 12,000。
    lines = [f"第{index}轮：" + "运" * 38 for index in range(100)]
    assert sum(len(item.encode("utf-8")) for item in lines) > SUMMARY_PROMPT_BYTES * 4

    kept, dropped = summary_for_prompt(lines)

    assert dropped > 0
    assert kept[-1] == lines[-1], "最新那一行没留住"
    assert kept[0] != lines[0], "最旧那一行还在 —— 那说明丢的不是最旧的一端"
    assert list(kept) == lines[len(lines) - len(kept) :], "顺序被打乱了"


# ---------------------------------------------------------------------------
# 二、裁定八：模型只能追加
# ---------------------------------------------------------------------------


def test_the_model_never_gets_the_earlier_lines_to_rewrite() -> None:
    """**裁定八靠结构成立，不靠一道闸。**

    写摘要那一处只读 `understanding["turn_summary"]` —— 模型交回来的那**一句**。
    它**不读已有的那些行**，所以「模型重写了摘要」在结构上不可能发生。

    ⚠️ **判的是语句，不是文件文本**：那个函数的 docstring 里逐字写着
    「它既不读旧行也不写旧行」，按文件文本判，这一条会为了错的理由绿。
    """

    body = _statements_only(_functions(PIPELINE)["_append_summary_line"])

    assert "turn_summary" in body, "写摘要那一处不再读模型交回来的那一句了"
    assert "SELECT" not in body.upper(), (
        # 1) 看到了什么
        "写摘要那一处开始读库了。"
        # 2) 为什么这条要红
        "\n**裁定八（模型只能追加、不能重写已有的那一份）今天是靠结构成立的**："
        "模型拿不到已有的那些行，所以想改也改不到。"
        "一旦这里读回旧行并把它们交给模型，那条裁定就要改由一道闸来守 —— "
        "而一道比对两份自由文本的闸**说不出改了哪里**。"
        # 3) 到期了该做什么
        "\n真要那么做，先回去读裁定八与裁定九（排期表 §4.3.4），"
        "并说清楚新的那道闸怎么判「模型改写了摘要」。"
    )
    assert "UPDATE" not in body.upper(), "写摘要那一处开始改已有的行了 —— 同上"


def test_one_turn_appends_at_most_one_line() -> None:
    """**一个回合一行**，而它由数据库保证，不是由调用顺序保证。

    入站是至少一次，同一个事件重放会再走一遍那一处。
    """

    migration = MIGRATION.read_text(encoding="utf-8")
    assert "conversation_summaries_one_line_per_turn_idx" in migration
    assert re.search(
        r"CREATE UNIQUE INDEX[^;]+ON conversation_summaries\s*\(\s*tenant_id,\s*"
        r"conversation_id,\s*source_event_id\s*\)",
        migration,
    ), "一个回合一行那条唯一索引不见了 —— 重放会往摘要里塞重复的行"

    body = _statements_only(_functions(PIPELINE)["_append_summary_line"])
    assert "ON CONFLICT" in body, (
        "写摘要那一处不再让唯一索引兜底了 —— 重放会抛，而那会让整轮回滚"
    )


def test_the_turn_summary_never_rides_into_the_downstream_prompts() -> None:
    """**它是写侧产出，不进 plan / generate 的 understanding 那一份。**

    ⚠️ **不摘掉会付两次账**，而且贵的是理解步那一侧：合同允许它 480 **字符**，
    中文 1,440 字节 —— 顶着上界会让 `bound_for_prompt` 的地板顶穿 1,580，
    那时它 docstring 里承诺的那条保证就不成立了。
    """

    from fastagent_v2.runtime.understanding_bound import _NOT_IN_PROMPT, bound_for_prompt

    assert "turn_summary" in _NOT_IN_PROMPT

    document = {
        "schema_version": "1.0.0",
        "event_id": "11111111-2222-4333-8444-555555555555",
        "facts": [],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "运" * 400,
        "model_evidence": {},
    }
    bounded = bound_for_prompt(document)
    assert "turn_summary" not in bounded.document


def test_the_contract_requires_a_line_every_turn() -> None:
    """**必填**，因为「这一轮没产出」与「忘了产」在线路上长得一样。"""

    schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert "turn_summary" in schema["required"], (
        "`turn_summary` 变成可选的了 —— 那一刻「模型这一轮没写摘要」"
        "不再有任何东西说得出来（CLAUDE.md §三）"
    )
    assert schema["properties"]["turn_summary"]["maxLength"] == SUMMARY_LINE_BYTES


# ---------------------------------------------------------------------------
# 三、裁定九：只作废，不逐字改，不新增 route
# ---------------------------------------------------------------------------


def test_invalidating_is_the_only_edit_and_it_rides_an_existing_route() -> None:
    """**裁定九取 (c)：不新增 route、不动 freeze。**

    作废挂在 `POST /conversations/{id}/resume` 的 `invalidate_summary` 上 ——
    **那是「人看过之后交还给 AI」的那一刻**，而摘要就在
    `resume-preview` 里（人得看得见它，才谈得上作废它）。
    """

    source = APP.read_text(encoding="utf-8")

    assert "invalidate_summary: bool = False" in source, (
        "作废那个字段不见了 —— 裁定八要的「人能改」没有入口了"
    )
    # **没有新开一条会话 route**：K21 四条之外的第五条要在 ADR 里点名（裁定九）。
    assert '/summary' not in source, (
        "出现了一条带 `/summary` 的 route —— 裁定九明写第一版**不新增 route**，"
        "要新增得先按 (a) 在 ADR 里点名「本格增了一条 freeze 之外的 route」"
    )

    body = _statements_only(_functions(APP)["resume_conversation"])
    assert "conversation_summaries" in body and "'invalidated'" in body
    # **行不删** —— 那是「保留双版本 / 审计」那一半。
    assert "DELETE" not in body.upper(), (
        "作废改成删行了 —— `conversation-engine.md:1146`（裁定八逐字引的那条）"
        "要的是「保留双版本/审计」，删掉就没有第二个版本了"
    )


def test_an_invalidated_line_never_reaches_the_prompt() -> None:
    """作废之后它不再进提示词，**而它还在库里**。"""

    body = _statements_only(_functions(PIPELINE)["_conversation_summary"])
    assert "'active'" in body, (
        "读摘要那一处不再按 `status` 过滤了 —— 被人作废的行会重新进提示词，"
        "而那正是裁定八说的「新事实静默覆盖人工 revision」的反面"
    )


def test_the_person_can_see_the_summary_before_invalidating_it() -> None:
    """**一个看不见的东西上不该有作废按钮。**"""

    body = _statements_only(_functions(APP)["conversation_resume_preview"])
    assert "conversation_summaries" in body and "conversation_summary" in body


# ---------------------------------------------------------------------------
# 四、只有入站那条路写它
# ---------------------------------------------------------------------------


def test_only_the_inbound_path_appends_a_line() -> None:
    """**定时那条路不写摘要，而这是有意的，不是漏了。**

    定时回合没有客户说的话 —— 给它编一行「系统自己跟进了一次」
    会让摘要里混进**不是对话的东西**，而模型读它时分不出哪句是客户说的。
    """

    calls = [
        node.lineno
        for node in ast.walk(ast.parse(PIPELINE.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_append_summary_line"
    ]
    assert len(calls) == 1, (
        f"追摘要的地方现在有 {len(calls)} 处（行号 {calls}），而它只该有一处。"
        "\n多一处多半是定时那条路也开始写了 —— 先读上面那段理由。"
    )
