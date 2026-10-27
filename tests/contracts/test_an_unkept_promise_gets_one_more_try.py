"""T367：许了没兑现，**先给一次带着反馈的重来**，抛掉仍是最后那一步。

## 裁定，原话

> 我们目标不是不做承诺 **而是承诺完要兑现**，抛掉是没有办法的办法。（2026-09-19）

以及两条落法（同日）：**「重跑计划 + 生成」**、
**「只重跑两次模型调用，栈操作钉成 keep」**。

## 它补的是哪一格

批 G 把兑现那条路打通了（一轮两条消息），**而它只是提示词的压力** ——
模型不照做时批 F 把那句许诺丢掉。T367 登记的正是这件事：
「这一切都是压力，不是执行。」

本批在两者之间插一步：**丢掉之前，先带着「你许了什么而没兑现」重来一次。**

## 为什么必须重跑**计划**

第二条消息**必须由计划先列出来**：`validate_generation` 里
`len(produced_kinds) > len(planned_kinds)` → `GENERATION_DUPLICATED_INTENT`
（批 G 的 `test_the_plan_has_to_list_the_second_message_first` 钉着）。
闸命中时往往正是「计划只列了一条」，**所以只重跑生成，它发不出第二条** ——
只能把兑现塞进那唯一一条里，而用户当面说过那样「显得很奇怪」。

## 成本：实测倒算，**不是拍的**

一通**正常**会话实测 `0.274191 CNY`（`ops-rollout-2026-09-18.md` §3.2），
约十轮 → 每轮三次调用约 `0.027`。重跑两次调用 ≈ **`0.02 CNY`，整通对话的 7%**，
而会话软顶 `0.6`（**只告警不拦**）、硬顶 `1.0` —— **还有 3.6 倍余量**。

⚠️ **批 G/H 的自审把成本写重了一档**：那里引的「软顶两小时报警 14 次」
是**旧上限（`0.4`、更早 `0.11`）下的事**，而那一格 release `-6` / `-7`
已经处置过（硬顶 `0.27 → 1.0`、软顶 `0.4 → 0.6`，合并即生效）。
**这一批用的是核准之后的数。**
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from fastagent_v2.runtime import request_shape
from fastagent_v2.worker import pipeline as pipeline_module

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)
MODEL = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_model.py"
)


# ---------------------------------------------------------------------------
# 一 · 反馈那一路：键、提示词、以及它为什么不是 `focus`
# ---------------------------------------------------------------------------


def test_the_plan_prompt_carries_what_the_last_attempt_did_not_keep() -> None:
    """计划步收得到「上一次许了什么而没兑现」。

    **空着也发**，与隔壁那几个键同一个理由：「这一次没有没兑现的许诺」与
    「没人告诉你还有这回事」不该在模型眼里长得一样。
    """

    document = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
    )

    assert document["unkept_promises"] == [], "第一次尝试它必须是空的"

    retried = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        unkept_promises=["那我先把试用怎么开、您那边要准备什么讲清楚。"],
    )
    assert retried["unkept_promises"] == [
        "那我先把试用怎么开、您那边要准备什么讲清楚。"
    ], "重跑那一次要把整句带过去 —— 动词短语说不出「试用怎么开」"


def test_it_is_not_smuggled_into_focus() -> None:
    """⚠️ **`focus` 是用户 2026-09-15 裁定的闭合键集，不能往里塞。**

    `conversation-aggregate` 的 `focus` 是 `additionalProperties: false`，
    而它的描述逐字写着「**有人加第三类键不会红** …… 要加哪一类，
    改这里就是那次裁定的执行者」。

    往 `focus` 里塞一个 `unkept` 会省掉一个新键的字节账 ——
    **而那是绕过一次裁定**。这条用例钉住那个界：
    反馈走自己的键，`focus` 的键集一个字没动。
    """

    import json  # noqa: PLC0415

    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "packages"
            / "contracts"
            / "schemas"
            / "conversation-aggregate.schema.json"
        ).read_text(encoding="utf-8")
    )
    focus = schema["properties"]["focus"]

    assert focus["additionalProperties"] is False, "那次裁定的执行点被打开了"
    # **钉的是「没有反馈用的键」，不是键集的字面清单。**
    #
    # ⚠️ 第一版抄了 `focus` 外层描述里那句「今天只有 `contact` 一类」，
    # 而实测是 `['contact', 'topic']` —— `topic` 是有据可查的**遗留键
    # （`"type": "null"`，只为让已存的聚合校验得过）**。
    # **那句描述相对那个遗留键略旧，而闭合性完好** ——
    # 抄一句描述当断言，钉的是措辞不是性质。
    assert not [key for key in focus["properties"] if "promis" in key or "unkept" in key], (
        f"反馈被加进了 `focus` 的键集：{sorted(focus['properties'])}"
    )

    document = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        unkept_promises=["我先把试用怎么开讲清楚。"],
    )
    assert document["focus"] == {}, "反馈被塞进了 `focus`"


def test_the_plan_prompt_says_what_the_field_means() -> None:
    """模型要被告知那个键在的时候该做什么 —— **列出兑现那一条**。

    键在而提示词不说，模型只会把它当噪声 ——
    本仓「两头都在、中间没接」那一族。
    """

    source = MODEL.read_text(encoding="utf-8")
    plan_system = source.split('_PLAN_SYSTEM = """', 1)[1].split('"""', 1)[0]

    assert "unkept_promises" in plan_system, "计划步没被告知这个键"
    assert "This is the retry" in plan_system, "没说清它非空意味着「这是重跑」"


def test_the_sentence_is_bounded_before_it_goes_back() -> None:
    """送回去的那几句有界 —— **两句、每句 120 字节**。

    无界的输入进提示词等于拿安全余量去赌（与 `last_outbound` 同一条记法）。
    ⚠️ 而截断留头不留尾：一句许诺的信息在前半
    （「那我先把试用怎么开……」），而 `_recent_refs` 那一族留尾是因为
    ref 的版本号在后面 —— **两种留法都在那个文件里，各写了一句为什么**。
    """

    long_promise = "我" + "把试用怎么开讲清楚" * 40
    document = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
        unkept_promises=[long_promise, "第二句。", "第三句。"],
    )

    kept = document["unkept_promises"]
    assert len(kept) == request_shape.UNKEPT_PROMISES_SHOWN == 2, "界是两句"
    assert kept[0].startswith("我把试用怎么开"), "截断把头切掉了"
    assert kept[0].endswith("…"), "截断没有留下可见的记号"
    assert (
        len(kept[0].encode("utf-8"))
        <= request_shape.UNKEPT_PROMISE_BYTES + len("…".encode())
    )


# ---------------------------------------------------------------------------
# 二 · 尺子把整句带出来，而审计面**不许**多出那一句
# ---------------------------------------------------------------------------


def test_the_dropped_item_carries_the_sentence_for_the_retry() -> None:
    """重跑要整句，所以丢弃那一项多带一个 `sentence`。"""

    from fastagent_v2.worker.inference_validation import (  # noqa: PLC0415
        drop_promises_this_turn_does_not_keep,
    )

    document = {
        "customer_visible_segments": [
            {"type": "text", "content_or_ref": "我这就把报价单发您。价格是一个月 1888。"}
        ],
        "proposed_intents": [],
        "claims": [],
    }
    dropped: list[dict[str, object]] = []
    drop_promises_this_turn_does_not_keep(document, dropped=dropped)  # type: ignore[arg-type]

    assert dropped, "那一句许诺没被丢掉"
    assert dropped[0]["sentence"] == "我这就把报价单发您。"
    assert dropped[0]["promised"] == "发您", "动词短语那一半还要在（审计行记它）"


def test_the_refusal_context_still_carries_no_customer_text() -> None:
    """⚠️ **这一条是本批被既有守卫抓到的那处。**

    整句带出来之后，`GenerationRejected` 那个 `context` 原来写的是
    `dict(item)` —— **它把整句抄进了审计面**，而
    `test_a_message_that_is_nothing_but_a_promise_is_refused`
    当场红并报「正文进了审计上下文」。

    **一条按「整个抄过去」写的投影，会跟着上游长出来的字段一起变宽。**
    今天它逐字投影三个键，这条用例钉住那个界。
    """

    from fastagent_v2.worker.inference_validation import (  # noqa: PLC0415
        drop_promises_this_turn_does_not_keep,
    )

    body = inspect.getsource(drop_promises_this_turn_does_not_keep)
    assert '"dropped_segments": [\n' in body, "那个投影又变回一行了？"
    assert "dict(item) for item in dropped" not in body, (
        "`context` 又在整个抄那几项 —— 上游多一个字段就会跟着漏出去"
    )
    assert '"sentence"' not in body.split('context={', 1)[1], (
        "整句进了 `context` —— 审计行里不放客户可见的字"
    )


# ---------------------------------------------------------------------------
# 三 · 重跑的形状：钉 keep、复用检索、不许把事情变坏
# ---------------------------------------------------------------------------


def _retry_body() -> str:
    return inspect.getsource(pipeline_module.InboxProcessor._retry_for_an_unkept_promise)


def test_the_retry_pins_the_stack_operation_to_keep() -> None:
    """**栈操作钉成 `keep`**（用户 2026-09-19 裁定「乙」）。

    目标栈迁移在第一次尝试里就落下了（`apply_target_stack_operation` 就地改
    `prepared.aggregate`）。照重跑那份计划再走一遍等于**迁移两次**。

    ⚠️ **而钉在校验之前**：校验器按这个字段判目标栈那一族规则，
    钉在它之后等于让它judge了一份不会被执行的计划。
    """

    body = _retry_body()
    assert '"target_stack_operation": {"op": "keep", "target_frame_definition_id": None}' in body
    assert body.index("target_stack_operation") < body.index("plan_or_fallback("), (
        "钉在校验之后了 —— 校验器判的就不是真正会执行的那一份"
    )
    # ⚠️ **还要钉住那一份真的被用上了** —— 探针 D 证明了只看赋值顺序不够：
    # 把 `plan=pinned` 换成 `plan=proposed`，赋值还在、顺序还对，
    # **而钉过 `keep` 的那一份被扔了**，这条用例照样绿。
    # **「算出来了」与「被用上了」是两件事。**
    assert "plan=pinned," in body, (
        "钉过 `keep` 的那一份没有被传给校验器 —— 它被算出来又扔掉了"
    )


def test_the_retry_reuses_the_first_retrieval() -> None:
    """检索**不重跑**：客户的问题没变，再检一遍要再花一次钱。

    而且 `050` 把 `retrieval_evidence` 定成了「这一轮检索发生过什么」——
    一轮两行读起来像检索了两次。
    """

    body = _retry_body()
    tree = ast.parse(inspect.getsource(pipeline_module))
    retriever_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_retrieve_if_requested"
    ]
    assert len(retriever_calls) == 1, (
        f"`_retrieve_if_requested` 的调用点不止一个：{len(retriever_calls)} —— "
        "重跑那一次又检了一遍"
    )
    assert "knowledge=knowledge," in body, "重跑没把传进来的那份知识递下去"


def test_a_failed_retry_keeps_the_first_attempt() -> None:
    """**重跑不许把事情变坏。**

    三种失败都回 `None`（供应商/成本顶、计划被拒、生成被拒），
    而调用方在 `None` 时什么都不动 —— 第一次至少有东西发得出去，
    **一次失败的重试不该把它换成沉默**。
    """

    body = _retry_body()
    assert body.count("return None") == 3, (
        "三条放弃路径（供应商/成本顶 · 计划被拒 · 生成被拒）不是三处 —— "
        f"实测 {body.count('return None')} 处"
    )
    assert "plan_or_fallback" in body and "if rejection is not None:" in body, (
        "重跑那份计划被拒时没有放弃 —— 它会走回落计划，而那是一条保守路径"
    )

    caller = inspect.getsource(pipeline_module.InboxProcessor.process_prepared)
    assert "if retried is not None:" in caller, (
        "调用方没有区分「重跑放弃了」与「重跑给出了结果」"
    )


def test_the_pipeline_sends_the_sentence_not_the_verb_phrase() -> None:
    """⚠️ **这一条是探针 B 报 GREEN 逼出来的。**

    上面那条只拿 `plan_prompt` 直调，喂什么它就带什么 ——
    **它看不见管线从那几项里取的是哪个键**。把 `item.get("sentence")`
    换成 `item.get("promised")`，整批照样绿：
    模型收到的会是「讲清楚」三个字，而那说不出「试用怎么开」。

    **一条只验「通道通不通」的用例，验不出「通道里流的是什么」。**
    """

    caller = inspect.getsource(pipeline_module.InboxProcessor.process_prepared)
    section = caller.split("if unkept_promises and not plan.is_silent:", 1)[1][:1200]

    assert 'item.get("sentence")' in section, (
        "管线取的不是整句 —— 「讲清楚」三个字说不出「试用怎么开」，模型补不出来"
    )
    assert 'item.get("promised")' not in section, (
        "管线把动词短语当反馈送回去了"
    )


def test_the_retry_only_runs_once() -> None:
    """一次，不是一个循环。

    ⚠️ 没有「重试到兑现为止」这回事：那会让一个总不兑现的模型把一轮的成本
    推到硬顶，而**客户拿到的仍然是同一条消息**。
    """

    caller = inspect.getsource(pipeline_module.InboxProcessor.process_prepared)
    assert caller.count("_retry_for_an_unkept_promise(") == 1, "重跑的调用点不止一个"
    assert "while" not in caller.split("if unkept_promises and not plan.is_silent:", 1)[1][:2000], (
        "重跑被写成了循环 —— 一次就是一次"
    )


def test_a_silent_strategy_is_not_retried() -> None:
    """沉默打法不重跑：它按设计不产出客户可见内容，没有「兑现」这回事。"""

    caller = inspect.getsource(pipeline_module.InboxProcessor.process_prepared)
    assert "if unkept_promises and not plan.is_silent:" in caller


# ---------------------------------------------------------------------------
# 四 · 它有没有用，要有人数
# ---------------------------------------------------------------------------


def test_someone_records_whether_the_retry_helped() -> None:
    """**这一行是判断 T367 那条路值不值的唯一读数。**

    没有它，「重跑帮上忙了吗」只能靠比较两个总数去猜 ——
    而那两个数里混着别的原因造成的变化。
    """

    source = PIPELINE.read_text(encoding="utf-8")

    assert "'plan.promise_retry'" in source, "重跑没有留痕"
    assert '"kept_after_retry"' in source, "留了痕而答不出「它有没有用」"
    assert '"retry_ran"' in source, (
        "分不出「重跑跑起来了但没兑现」与「重跑根本没跑起来」—— "
        "两者要人做的事不同"
    )
    body = inspect.getsource(pipeline_module.InboxProcessor._record_promise_retry)
    assert '"promises": len(promised)' in body, (
        "记的是那几句正文而不是条数 —— 审计行里不放客户可见的字"
    )
