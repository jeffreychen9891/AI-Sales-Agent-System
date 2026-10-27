"""T276：**落一条人工待办，引擎侧只有一个定义点。**

## 它关掉的是什么

「写一条 `intent_records(kind='manual_task')` + 一条 `manual_tasks`」这一对
INSERT，**在这一批之前引擎侧有五处各写一遍**。登记（T276，2026-09-11）当时的
原话是：**不是同一段代码的五个副本，是同一个概念的五次独立实现** ——
改其中一个的人不会被任何东西提醒还有四个。

## 而它不是一次「为了整洁」的合并 —— 那五处已经漂成了三种幂等语义

**2026-09-20 实测**（跑，不是读；见 `test_a_replayed_turn_files_at_most_one_task`
那条今天真的会红的用例）。五处的幂等键都带 `event_id`，
所以「**一个回合至多一条待办**」是五处共同的**声称**：

* `_record_rate_limit` · `_file_task_for_unsendable_intents` ·
  `_manual_task_for_delivery` —— 不落第二条；
* `_file_a_task_for_a_promise_only_a_person_can_keep` ——
  **落第二条，并推第二条通知**；
* `_persist_no_send` —— 没有 `ON CONFLICT`，撞上唯一约束会抛。

中间那一行是本仓「**多传一个不报错**」那一族的一个新形状，
而它比通常那一种更难看见：**该写的一个字都没少写** ——
`ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` 就在那儿，
幂等键也带着 `event_id`。**少的是读它答案的那一步**：
那条 INSERT 不 `RETURNING`、调用方不 `fetchone`、于是没有任何分支据它短路。

**而 `manual_tasks.intent_id` 没有外键**（`034_manual_tasks.sql` 里只有
`owner_employee_id` 那一条），所以那条指向一行不存在的意图的待办
**不会被任何约束挡下**。它安静地落库，同事收到第二条通知。

> **三处都在 docstring 里写着「一个回合至多一条待办」。**
> 写着同一句话的三处，做的是三件不同的事 —— 这就是五个定义点的代价，
> 而它在任何一次只读其中一处的复核里都看不出来。

## 本文件守的三件

1. **只有一个定义点**（下面第一条，另一半在
   `test_manual_task_owner_sla_expiry.py::ENGINE_INSERT_SITES == 1`）；
2. **每个调用点都把十四个参数传齐了** —— 读 AST，不数字符串；
3. **重放一个回合至多落一条待办**，五条路逐条验，**这一条是行为断言，
   所以它真的把那五条路跑一遍**（AGENTS.md：关于行为的断言，证据只能是跑过一遍）。
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker.pipeline import (
    DELIVERY_MANUAL_TASK_REASONS,
    UNSUPPORTED_INTENT_REASON,
    InboxProcessor,
    OutboxDispatcher,
    ProposalDecision,
)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = REPOSITORY_ROOT / "services" / "worker"
PIPELINE = (
    WORKER / "src" / "fastagent_v2" / "worker" / "pipeline.py"
)

#: 那个唯一定义点的名字。
FILING_METHOD = "_file_a_manual_task"

#: 它收的那些参数，**逐字**。写成等值不是上界：
#: 少一个是「有个调用点漏传了」，多一个是「又有人把差异塞进了参数表」——
#: 后者正是本批要防的那件事的复发形态，所以它也该红。
FILING_KEYWORDS = frozenset(
    {
        "tenant_id",
        "conversation_id",
        "manual_task_id",
        "reason_code",
        "classification",
        "source",
        "proposal_facts",
        "proposed_by_version",
        "strategy_id",
        "target_frame_id",
        "idempotency_key",
        "preconditions",
        "policy_evidence",
        "evidence",
    }
)

#: 今天有几条路调它。**这个数写下来是有理由的**（AGENTS.md「实际有几个就走通几个」）：
#: 下面那条行为用例逐条跑这五条，而「跑了几条」与「一共有几条」是两个数 ——
#: 不把后者写下来，前者永远答不出「都跑到了吗」。
FILING_CALL_SITES = 5


def _tree() -> ast.Module:
    return ast.parse(PIPELINE.read_text(encoding="utf-8"))


def _statements_only(node: ast.FunctionDef) -> str:
    """那个方法的**语句**，docstring 剥掉。

    ⚠️ **本仓踩过七次「守卫读到自己的散文」**，而本文件会是第八次：
    `_file_a_manual_task` 的 docstring 里**逐字写着那两条 SQL 的形状**
    与另外几处的方法名。下面判「除了它以外没有人再写这两条 INSERT」的那条，
    直接 `ast.unparse` 就会命中它自己的说明文字。
    抄自 `test_a_promise_only_a_person_can_keep.py::_statements_only`。
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


def _methods() -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(_tree())
        if isinstance(node, ast.FunctionDef)
    }


def test_only_one_method_writes_the_pair() -> None:
    """**那两条 INSERT 只出现在 `_file_a_manual_task` 的语句里。**

    ⚠️ **判的是语句，不是文件文本** —— 见 `_statements_only` 的说明：
    那个方法自己的 docstring 里就有这两条 SQL 的字样，而另外几处的注释里
    也还提着它们（「此前这一处把帧号取在 INSERT 里面」）。
    **按文件文本判，这条用例会红在一堆散文上；按语句判，它判的是代码。**
    """

    methods = _methods()
    assert FILING_METHOD in methods, (
        f"`{FILING_METHOD}` 不见了 —— 它是引擎侧落待办的唯一定义点（T276）。"
        "\n如果它是被改名了，把本文件的 `FILING_METHOD` 一起改；"
        "如果是被拆回多处，那正是 T276 关掉的那件事又回来了。"
    )

    writers = {
        name: node
        for name, node in methods.items()
        if "INSERT INTO manual_tasks" in _statements_only(node)
    }
    assert set(writers) == {FILING_METHOD}, (
        # 1) 看到了什么
        f"引擎侧写 `manual_tasks` 的方法现在是 {sorted(writers)}，"
        f"而它只该是 {{{FILING_METHOD!r}}}。"
        # 2) 为什么这条要红
        "\n**T276 合并那五处，不是为了少几行** —— 是因为那五处当时已经漂成了"
        "三种互不相同的幂等语义，而三处的 docstring 写着同一句"
        "「一个回合至多一条待办」。**再开一个定义点，那件事就会再发生一次。**"
        # 3) 到期了该做什么
        f"\n**要落待办就调 `{FILING_METHOD}`。** 新那一处真的合不进那个签名时，"
        "先说清楚它要的东西为什么与另外五处不同 —— 那句话本身就是这条用例要的东西。"
    )


def test_every_call_site_passes_every_keyword() -> None:
    """**十四个参数，每个调用点都要传齐。**

    ## 读 AST，不数字符串

    本仓记着「数错了的守卫看起来在守」：数
    `classification=classification,` 出现几次是空的 ——
    那一行在 `_notify_manager` 的调用里也出现过（合并这一批时，
    一次 `str.replace` 就真的把它误插进了那个调用，见交付说明）。

    ## 为什么「传齐」是这条用例要判的那件事

    `_file_a_manual_task` 的十四个参数**一个默认值都没有**，所以
    「漏传」本身是 `TypeError`，轮不到这条用例去抓。
    **它抓的是另一半：有人给那个签名加了一个带默认值的参数。**
    那一刻漏传不再报错，而一条 `evidence={}` 的待办与一条
    「这件事本来就没有上下文」的待办，在拿到它的同事那边长得一模一样。
    """

    calls: list[tuple[int, frozenset[str]]] = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # **模块级函数**，所以是 `ast.Name` 不是 `ast.Attribute` ——
        # 它挂在模块上而不是某个类上，理由在 `pipeline.py` 里它上面那段注释：
        # 调它的五条路分属 `InboxProcessor` 与 `OutboxDispatcher` 两个类。
        if not (isinstance(func, ast.Name) and func.id == FILING_METHOD):
            continue
        calls.append((node.lineno, frozenset(k.arg for k in node.keywords if k.arg)))

    assert len(calls) == FILING_CALL_SITES, (
        f"调 `{FILING_METHOD}` 的地方现在有 {len(calls)} 处（此前 {FILING_CALL_SITES} 处），"
        f"行号 {[line for line, _ in calls]}。"
        "\n加一条落待办的路不是错 —— 要办的是把这个数改掉，"
        "并给下面那条行为用例补上这一条路（它逐条跑，漏一条就等于没验）。"
    )

    off = [
        (line, sorted(FILING_KEYWORDS ^ keywords))
        for line, keywords in calls
        if keywords != FILING_KEYWORDS
    ]
    assert not off, (
        f"这些调用点传的参数与那十四个对不上（行号, 差集）：{off}。"
        "\n差集里的名字要么是漏传的，要么是新加的 —— "
        "**新加一个带默认值的参数，是这条用例真正在防的那件事**："
        "那一刻「漏传」不再是 `TypeError`，而是一条内容安静地少了一半的待办。"
    )


class _RecordingConnection:
    """记下本该写进库的每一条语句。

    `conflicts=True` 演的是**这个幂等键已经落过一条待办了**：
    `INSERT … ON CONFLICT DO NOTHING RETURNING id` 返回 `None`。
    形状抄自 `test_unsendable_intent_becomes_a_task.py` 里那一个。
    """

    def __init__(self, *, conflicts: bool) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._conflicts = conflicts

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        self.statements.append((statement, parameters))
        if "ON CONFLICT" in statement and self._conflicts:
            return SimpleNamespace(fetchone=lambda: None, fetchall=list)
        return SimpleNamespace(
            fetchone=lambda: {"id": uuid4(), "target_frame_id": uuid4()},
            fetchall=list,
        )

    def tasks_written(self) -> int:
        return sum("INSERT INTO manual_tasks" in s for s, _ in self.statements)


def _prepared() -> Any:
    return SimpleNamespace(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        # `correlation_id` 与 `event_id` 一样是信封里本来就有的键 ——
        # `_record_rate_limit` 读它写审计行。**不是为了让用例过而加的**。
        envelope={"event_id": str(uuid4()), "correlation_id": str(uuid4())},
        aggregate={
            "target_stack": [{"id": str(uuid4())}],
            "control_mode": "ai",
            "state_version": 1,
            "generation": 1,
        },
    )


def _refused(kind: str = "manual_task") -> ProposalDecision:
    return ProposalDecision(
        ordinal=0,
        kind=kind,
        accepted=False,
        reason_code=UNSUPPORTED_INTENT_REASON,
        detail="the send path does not carry this intent yet",
        proposal={"kind": kind, "typed_payload": {}, "evidence_refs": []},
    )


def _processor(notices: list[dict[str, Any]]) -> InboxProcessor:
    processor = InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="unit",
    )

    def _capture(_connection: Any, **kwargs: Any) -> UUID:
        notices.append(kwargs)
        return uuid4()

    processor._notify_manager = _capture  # type: ignore[method-assign]
    return processor


def _drive_rate_limit(processor: InboxProcessor, connection: Any) -> None:
    processor._record_rate_limit(
        connection,
        _prepared(),
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        withheld=[_refused("send_message")],
        scope="conversation",
        promises_dropped=[],
    )


def _drive_promise(processor: InboxProcessor, connection: Any) -> None:
    processor._file_a_task_for_a_promise_only_a_person_can_keep(
        connection,
        _prepared(),
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        unkept=[
            {
                "sentence": "报价单这边我来安排。",
                "promised": "安排",
                "needs_a_person": True,
            }
        ],
    )


def _drive_unsendable(processor: InboxProcessor, connection: Any) -> None:
    processor._file_task_for_unsendable_intents(
        connection,
        _prepared(),
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        refused=(_refused(),),
        promised=(),
    )


def _drive_delivery(_processor: InboxProcessor, connection: Any) -> None:
    """⚠️ **这一条走的是另一个类**：`_manual_task_for_delivery` 在
    `OutboxDispatcher` 上，不在 `InboxProcessor` 上。

    **而这正是 `_file_a_manual_task` 必须是模块级函数的原因** ——
    合并的第一版把它挂成了 `InboxProcessor` 的方法，于是这一条路上的
    `self._file_a_manual_task` 是一个 `AttributeError`。
    **读源码的断言一条都没红**（方法在、调用点在、参数也传齐了），
    红的是这条真的把那条路跑一遍的用例。
    """

    dispatcher = OutboxDispatcher.__new__(OutboxDispatcher)
    dispatcher._manual_task_for_delivery(
        connection,
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        intent_id=uuid4(),
        outbox_id=uuid4(),
        # 真的那两个码之一（`DELIVERY_MANUAL_TASK_REASONS` 现取，不写字面量）——
        # 一个编出来的码会走到 `classify()` 的 `UNCLASSIFIED` 兜底上，
        # 那时这条用例验的是兜底，不是这条路。
        reason_code=DELIVERY_MANUAL_TASK_REASONS["unknown"],
        evidence={},
    )


#: 五条路里的四条能在单元级驱动到。**第五条 `_persist_no_send` 不在这里**，
#: 理由写在 `test_the_fifth_path_is_covered_by_the_shared_definition_point` 里 ——
#: **留一个诚实的空格，比让这张表全绿有用**（AGENTS.md 那一条）。
DRIVEN_PATHS = {
    "_record_rate_limit": _drive_rate_limit,
    "_file_a_task_for_a_promise_only_a_person_can_keep": _drive_promise,
    "_file_task_for_unsendable_intents": _drive_unsendable,
    "_manual_task_for_delivery": _drive_delivery,
}


@pytest.mark.parametrize("path", sorted(DRIVEN_PATHS))
def test_a_replayed_turn_files_at_most_one_task(path: str) -> None:
    """**重放一个回合，至多落一条待办，至多推一条通知。**

    ⚠️ **这一条在合并之前是红的**，而红的只有
    `_file_a_task_for_a_promise_only_a_person_can_keep` 一支：
    那一处的 `ON CONFLICT` 写了、没有人读它的答案，于是重放会再落一条待办、
    再推一条通知（2026-09-20 实测 `manual_tasks=1 notices=1`，
    而同样条件下另外几处是 `0 / 0`）。

    **删掉 `_file_a_manual_task` 里那个 `if inserted is None: return None`，
    这四条会一起红** —— 那是本批「删掉会红哪条」的答案。
    """

    notices: list[dict[str, Any]] = []
    processor = _processor(notices)

    first = _RecordingConnection(conflicts=False)
    DRIVEN_PATHS[path](processor, first)
    assert first.tasks_written() == 1, (
        f"`{path}` 第一次就没落成待办 —— 这条用例的对照塌了，"
        "下面那个「重放不落第二条」于是什么都没证明"
    )
    assert len(notices) <= 1, f"`{path}` 一轮推了 {len(notices)} 条通知"
    first_round = len(notices)

    notices.clear()
    replayed = _RecordingConnection(conflicts=True)
    DRIVEN_PATHS[path](processor, replayed)

    assert replayed.tasks_written() == 0, (
        # 1) 看到了什么
        f"`{path}` 在幂等键已经用过的情况下又落了一条待办。"
        # 2) 为什么这条要红
        "\n五条路的幂等键都带 `event_id`，意思是**一个回合是一件发生过的事**；"
        "而 `manual_tasks.intent_id` 没有外键，所以这条指向一行不存在的意图的"
        "待办不会被任何约束挡下 —— 它安静地落库。"
        # 3) 到期了该做什么
        f"\n`{FILING_METHOD}` 拿回 `None` 时必须直接返回，调用方拿到 `None` 时"
        "必须既不落待办也不推通知。**这正是 T276 合并那五处修掉的那件事。**"
    )
    assert not notices, (
        f"`{path}` 在没落成待办的那一轮推了 {len(notices)} 条通知（第一轮推了 {first_round} 条）。"
        "\n通知是待办的附属品 —— 没有待办就没有东西可通知，"
        "而同事收到的第二条通知指向的是一条不存在的任务。"
    )


def test_the_fifth_path_is_covered_by_the_shared_definition_point() -> None:
    """**第五条路（`_persist_no_send`）没有在上面逐条跑，这里说清楚为什么。**

    ⚠️ **不是「顺带覆盖了」**：`_persist_no_send` 要一整个
    `PreparedEvent` + 一份 plan + 一条 policy 行才走得到落待办那一步，
    在单元级把它摆出来，摆出来的是一个**作者想象的形状**，
    而本仓记着「**那条用例喂的东西必须是生产里会出现的形状**」——
    一条自造夹具驱动的用例会绿，且为了错的理由绿。

    **它被覆盖的方式是另一种，而这种是可判的**：它与上面四条走的是
    **同一个** `_file_a_manual_task`（`test_only_one_method_writes_the_pair`
    判住了「只有一个」，`test_every_call_site_passes_every_keyword` 判住了
    「五个调用点都传齐了」）。**短路发生在那个函数里，不在调用点里** ——
    所以上面四条跑绿，等于那一个短路分支跑绿。

    **这条用例判的就是这件事本身**：那一处确实在调它、且确实据它的答案
    决定推不推通知。⚠️ 它是**读**的，不是跑的 —— 本仓要求这种区分写出来。
    """

    method = _methods()["_persist_no_send"]
    body = _statements_only(method)

    assert FILING_METHOD in body, (
        f"`_persist_no_send` 不再调 `{FILING_METHOD}` 了 —— 它自己又写了一遍那两条"
        "INSERT 的话，`test_only_one_method_writes_the_pair` 会先红"
    )
    assert "filed_manual_task is not None and classification.pushes_now" in body, (
        # 1) 看到了什么
        "`_persist_no_send` 推通知的条件不再是「待办真的落成了」。"
        # 2) 为什么这条要红
        "\n此前那个条件是 `manual_task_id is not None`，而那个 id 是本方法自己"
        "`uuid4()` 出来的 —— **它永远不是 `None`**，那半个条件从来没有排除过任何一轮。"
        "T276 合并之后真正的答案由 `_file_a_manual_task` 给。"
        # 3) 到期了该做什么
        "\n要改推送条件就连着这条一起改，并说清楚「没落成待办却要通知」是什么情形。"
    )
