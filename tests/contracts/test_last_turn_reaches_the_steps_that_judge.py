"""上一轮我们说的那句话，送到判它的那两步面前。

## 为什么有这份

运维 2026-09-17 复测：**22 条入站，14 条出站 —— 八轮客户什么都没收到**。
其中一轮逐字是这样的：

| 我们上一轮 | 客户 | 我们 |
|---|---|---|
| 「…现在是您和同事轮流看消息，**还是有人专门负责这一块**？」 | **有人专门负责** | —— |

客户回的是**我们自己那道选择题的答案**，四个字干干净净，
被判 `AMBIGUOUS_LOW_SIGNAL_INPUT` → `wait` → 客户那侧完全沉默。

**判它的是理解步**，而在此之前 `understand_prompt` 收到的五样
（`event_type` / `content` / `current_goal` / `known_slots` / `slots_wanted`）
里，**没有一样告诉过它我们上一轮说了什么**。

> 用户 2026-09-17：「沉默主要来自于没读懂上下文，单看几个字没法理解。」

## 计划步要它的理由是另一个

模型上一轮说「**我说说它具体怎么接**」，下一轮换成了一个问句 ——
**今天没有任何东西记着它答应过什么**。`pending_question` 记的是
`slot` 与 `asked_at`，**没有正文**，所以就算计划步收到它，
也看不出上一句许了什么。

## 它不守什么

**不守模型真的读懂了。** 那要一次真实对话，判据在运维那边
（`ops-to-author-silence-report-2026-09-17.md` §五）。
这里守的是「**那句话真的到了它们面前**」——而此前它根本没被送过去。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastagent_v2.runtime import request_shape

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
)

LAST = "…还是有人专门负责这一块？"


def test_the_understand_prompt_carries_what_we_said_last_turn() -> None:
    """**判低信号的那一步收到了它** —— 这是那一轮沉默的根因。"""

    document = request_shape.understand_prompt(
        goal="g", information_slots=[], content={"text": "有人专门负责"}, last_outbound=LAST
    )

    assert document["last_outbound"] == LAST


def test_the_plan_prompt_carries_it_too() -> None:
    """计划步也收，且**与理解步是同一份**。"""

    plan = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        last_outbound=LAST,
    )
    understand = request_shape.understand_prompt(goal="g", information_slots=[], last_outbound=LAST)

    assert plan["last_outbound"] == understand["last_outbound"] == LAST


@pytest.mark.parametrize(
    "build",
    [
        lambda: request_shape.understand_prompt(goal="g", information_slots=[]),
        lambda: request_shape.plan_prompt(
            goal="g",
            exit_conditions=[],
            information_slots=[],
            allowed_strategies=[],
            strategies={},
            event_kinds=[],
        ),
    ],
    ids=["理解步", "计划步"],
)
def test_the_key_is_sent_even_on_the_first_turn(build) -> None:  # noqa: ANN001
    """**空着也发。**

    「这通对话我们还没说过话」与「没人告诉你我们说过话」
    不该在模型眼里长得一样 —— 与 `content_materials` 那几个同一个理由。
    """

    assert build()["last_outbound"] == ""


# ---------------------------------------------------------------------------
# 读它的那一步
# ---------------------------------------------------------------------------


def _pipeline_source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def test_the_text_is_read_by_the_one_reader_and_not_a_second_one() -> None:
    """**正文由 `channel.customer_text` 读，不另写一个。**

    那个函数的 docstring 记着 2026-09-10 的事故：平台有三种出站内容形状、
    **没有单一读者**，于是一整个窗口零送达 —— 三行 `unsendable_content_type`，
    而两边的单元测试全程是绿的。

    **加第四个读者就是把那件事再演一遍**，所以这条钉住它没有被绕开。
    """

    source = _pipeline_source()
    start = source.index("def _last_outbound(")
    body = source[start : source.index("\n    @staticmethod", start)]

    assert "customer_text(" in body, "没走那个唯一的读取器"
    assert '"text"' not in body, "像是在这里又认了一遍内容形状"
    assert "segments" not in body, "像是在这里又认了一遍内容形状"


def test_only_what_the_customer_could_see_counts() -> None:
    """**只取发给客户的那些行。**

    发给管理员的通知不是这个客户看见的话；拿它当上下文，
    会让模型以为自己说过一句从没发到客户那里的话。
    """

    source = _pipeline_source()
    start = source.index("def _last_outbound(")
    body = source[start : source.index("\n    @staticmethod", start)]

    assert "AUDIENCE_IS_CUSTOMER_SQL" in body, "没有按受众过滤"
    assert "ORDER BY created_at DESC" in body, "取的不是最近那一条"
    assert "LIMIT 1" in body


def test_every_understand_call_site_passes_it() -> None:
    """**每一个 `understand` 调用点都要带它。**

    只接上一个，另一条路上的回合仍然单看几个字判信号 ——
    而那正是本仓「两头都在、中间没接」那一族的形状
    （`proactivity_policy` 整块生产代码零消费方那次）。

    ## 读 AST，不数字符串

    第一版数的是 `last_outbound=prepared.last_outbound,` 出现几次，
    **而计划步那个调用点也有这一行** —— 于是删掉一个 `understand` 的
    还剩两处，判据照样成立。**反向探针实测全绿**，它当场证明了
    那条用例什么都没守。数错了的守卫比没有守卫更坏：它看起来在守。
    """

    import ast  # noqa: PLC0415

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    missing: list[int] = []
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "understand"):
            continue
        found += 1
        if not any(keyword.arg == "last_outbound" for keyword in node.keywords):
            missing.append(node.lineno)

    assert found >= 2, f"只找到 {found} 个 understand 调用点，这条用例要跟着改"
    assert not missing, (
        f"这些 understand 调用点没把上一轮那句话传进去（行号 {missing}）—— "
        "那几条路上的回合仍然单看几个字判信号"
    )


def test_the_prepared_event_carries_it_with_an_empty_default() -> None:
    """默认空串 —— **第一轮没有上一句，这不是缺失**。"""

    from fastagent_v2.worker.pipeline import PreparedEvent  # noqa: PLC0415

    assert "last_outbound" in inspect.signature(PreparedEvent).parameters
    assert PreparedEvent.__dataclass_fields__["last_outbound"].default == ""


def test_it_is_bounded_and_keeps_the_tail() -> None:
    """**有界，而且留尾不留头。**

    问句（「…还是有人专门负责这一块？」）与「我下一句给您讲 X」都落在末尾，
    而那正是让一个四字回答变得可读的部分。从头截会把它切掉，
    留下一段与这一轮无关的开场白。
    """

    from fastagent_v2.worker.pipeline import InboxProcessor  # noqa: PLC0415

    cap = InboxProcessor.LAST_OUTBOUND_BYTES
    assert cap > 0

    source = _pipeline_source()
    start = source.index("def _last_outbound(")
    body = source[start : source.index("\n    @staticmethod", start)]

    assert "encoded[-InboxProcessor.LAST_OUTBOUND_BYTES :]" in body, (
        "不是留尾 —— 问句在末尾，从头截会把它切掉"
    )
    assert "len(encoded) <= InboxProcessor.LAST_OUTBOUND_BYTES" in body, "没有按字节判"


def test_the_cap_cannot_blow_the_model_input_budget() -> None:
    """这一格**不会把哪一轮顶过输入上限**。

    ## 为什么要专门证一次

    运维那份报告里有六次 `PROVIDER_INPUT_TOO_LARGE`（106KB 级别）。
    **他们 2026-09-17 晚更正过一次**：那六条全在 **2026-09-11**，此后 0 次，
    成因也查清了 —— 当时序列化用 `ensure_ascii=True`，中文按六字节的转义
    序列渲染，**106KB 是尺子的读数、不是包裹的重量**（真实 body 约一半）；
    加上上限已从 16,384 抬到 49,000、配包从 44KB 瘦到 10KB。

    **也就是说这一族今天不发生了。** 这条仍然在，是因为
    **往提示词里加东西的那一刻，得当场证明自己是小的**，
    而不是引一句「那件事已经好了」当理由 —— 那种理由会随下一次变化过期。
    """

    from fastagent_v2.worker.cost import MAX_MODEL_INPUT_BYTES  # noqa: PLC0415
    from fastagent_v2.worker.pipeline import InboxProcessor  # noqa: PLC0415

    cap = InboxProcessor.LAST_OUTBOUND_BYTES

    # 两步各收一份，所以按两倍算；仍要远小于整份请求的上限。
    assert cap * 2 < MAX_MODEL_INPUT_BYTES // 10, (
        f"上一轮那句话最多 {cap} 字节 × 2 步，对 {MAX_MODEL_INPUT_BYTES} 的输入上限"
        "不够小 —— 它有资格把一轮顶过上限了"
    )
