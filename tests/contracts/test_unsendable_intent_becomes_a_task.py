"""AI 应下一件它做不到的事时，落一条待办——而不是什么都不发生。

**缺陷的形状**（用户 2026-09-11 报，素材链路跑通之后的第一块演示风险）：
生成步提名得出 `manual_task`，计划提名过所以 `validate_generation` 放行，
而 `GENERATION_SUPPORTED_KINDS` 只有 `send_message`——那条提案被拒。
**而决策是逐条独立的**：同一轮里的 `send_message` 照发。
于是客户听见「我把您标记一下」，AI 应了，**然后什么都不发生**。
被拒那条只留下一行 `policy_decisions`，而那一行只有查库的人看得见。

**用户裁定取「甲」**：至少派一条待办（不取「让 `manual_task` 真正走通」，
那要 per-kind write path，是另一个大批次）。

## 为什么这些是单元级的，而不是端到端

`test_proposal_decision_unit.py` 首部把这件事写清楚了，**本文件受同一条约束**：
一个回合要同时拿着一条能发的和一条不能发的提案，得有某个策略同时允许
`send_message` 和第二个 kind——**两份合成 profile 里没有这样的策略**
（每个都是 `['send_message']`、`['manual_task']` 或空）。用户 2026-09-05 的裁定是
**在单元级证明并登记缺口，而不是为了让测试路径存在去改 profile**
——那是让测试牵着产品走。

**登记项 T101 因此不变**：它的触发条件仍然写着「第一个声明了多 kind 策略的
profile 出现的那天」。本批**没有**让那条路变得端到端可达，只是让它到达之后
不再什么都不发生。
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.notification import (
    IMMEDIATE,
    MERCHANT_ACTION,
    UNKNOWN_SENTENCE,
    classify,
    manual_task_notice,
    reason_sentence,
)
from fastagent_v2.worker.pipeline import (
    INTENT_NEEDS_A_PERSON_REASON,
    TASK_FILING_KINDS,
    UNSUPPORTED_INTENT_REASON,
    InboxProcessor,
    ProposalDecision,
)


class _RecordingConnection:
    """记下本该写进库的每一条语句。

    用替身而不是真库，理由与 `test_proposal_decision_unit.py` 的那个替身相同，
    见本文件首部。`fetchone` 要能返回东西：落待办那一步是
    `INSERT … ON CONFLICT DO NOTHING RETURNING id`，**返回 `None` 就表示
    这一个回合已经有过一条待办了**。
    """

    def __init__(self, *, conflicts: bool = False) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._conflicts = conflicts

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        self.statements.append((statement, parameters))
        if "ON CONFLICT" in statement and self._conflicts:
            return SimpleNamespace(fetchone=lambda: None, fetchall=list)
        # **`fetchall` 是 B1-5 之后补的**（2026-09-13）：回合收口那一句
        # （`_refresh_reference_projection`）要把两张权威表里活着的行读回来，
        # 而这个替身此前只会 `fetchone`。**空列表是这里唯一诚实的答案** ——
        # 这一份用例问的是「不发送那条路走没走到落待办那一步」，
        # 它不造意图行，所以「活着的引用」本来就该是空的。
        return SimpleNamespace(fetchone=lambda: {"id": uuid4()}, fetchall=list)


def _processor() -> InboxProcessor:
    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="unit",
    )


def _prepared() -> Any:
    return SimpleNamespace(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        envelope={"event_id": str(uuid4())},
        aggregate={"target_stack": [{"id": str(uuid4())}], "control_mode": "ai"},
    )


def _refused(kind: str, *, ordinal: int = 0, reason: str = UNSUPPORTED_INTENT_REASON) -> Any:
    return ProposalDecision(
        ordinal=ordinal,
        kind=kind,
        accepted=False,
        reason_code=reason,
        detail=f"the send path does not carry the {kind} intent yet",
        proposal={"kind": kind, "typed_payload": {}, "evidence_refs": []},
    )


def _sent(text: str) -> Any:
    return ProposalDecision(
        ordinal=0,
        kind="send_message",
        accepted=True,
        reason_code=None,
        detail="",
        proposal={
            "kind": "send_message",
            "typed_payload": {"content": {"type": "text", "text": text}},
            "evidence_refs": [],
        },
    )


def _file(
    connection: _RecordingConnection,
    *,
    refused: tuple[Any, ...],
    promised: tuple[Any, ...] = (),
    notices: list[dict[str, Any]] | None = None,
) -> None:
    processor = _processor()
    if notices is not None:

        def _capture(_connection: Any, **kwargs: Any) -> None:
            notices.append(kwargs)

        processor._notify_manager = _capture  # type: ignore[method-assign]
    processor._file_task_for_unsendable_intents(
        connection,
        _prepared(),
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        refused=refused,
        promised=promised,
    )


def test_a_refused_manual_task_becomes_one_task_instead_of_nothing() -> None:
    """**这一条是整批的理由。** 被拒之前是一行只有查库的人看得见的记录。"""

    connection = _RecordingConnection()
    _file(connection, refused=(_refused("manual_task"),), notices=[])

    written = [statement for statement, _ in connection.statements]
    assert any("INSERT INTO intent_records" in item for item in written)
    assert any("INSERT INTO manual_tasks" in item for item in written)

    intent = next(p for s, p in connection.statements if "INSERT INTO intent_records" in s)
    assert INTENT_NEEDS_A_PERSON_REASON in str(intent)
    task = next(p for s, p in connection.statements if "INSERT INTO manual_tasks" in s)
    assert INTENT_NEEDS_A_PERSON_REASON in task
    evidence = task[-1].obj
    assert evidence["kinds"] == ["manual_task"]
    # 序号指向本轮的 policy_decisions 行——拿到待办的人不必自己去找。
    assert evidence["refused_ordinals"] == [0]


def test_filing_a_task_does_not_hand_the_conversation_to_a_human() -> None:
    """**不做移交，这是有意的。**

    `_persist_no_send` 的 `handoff` 分支会 `suspend_target_stack_for_human` 并把
    `control_mode` 翻成 `human`——**那会让 AI 当场闭嘴**。而这一条的触发语境
    恰恰是 AI 正在好好说话、只是应下了一件要人做的事：「我把您标记一下」
    不是「我把您转人工」。**做成移交等于用一个更糟的塌台换掉一个塌台。**
    """

    connection = _RecordingConnection()
    _file(connection, refused=(_refused("manual_task"),), notices=[])

    for statement, _ in connection.statements:
        assert "control_mode" not in statement, statement
        assert "UPDATE conversation_aggregates" not in statement, statement


def test_two_refused_task_kinds_in_one_turn_file_one_task() -> None:
    """**一个回合至多一条待办**，同 `_record_rate_limit` 的理由。

    一个回合是一件发生过的事；同一轮里两个被拒的提案不是两件事，
    而两条指着同一个回合的待办是一个会自己报数的积压。
    两条的序号都进 evidence，所以「哪两条」仍然答得出来。
    """

    connection = _RecordingConnection()
    _file(
        connection,
        refused=(_refused("manual_task", ordinal=1), _refused("handoff", ordinal=2)),
        notices=[],
    )

    assert sum("INSERT INTO manual_tasks" in s for s, _ in connection.statements) == 1
    task = next(p for s, p in connection.statements if "INSERT INTO manual_tasks" in s)
    assert task[-1].obj["kinds"] == ["handoff", "manual_task"]
    assert task[-1].obj["refused_ordinals"] == [1, 2]


def test_a_second_turn_that_collides_on_the_key_writes_no_second_task() -> None:
    """幂等键带 `event_id`，撞上了就**只返回，不继续往下写**。

    第二条 INSERT 在 `RETURNING id` 为空之后不该发生——否则 `manual_tasks`
    会多出一条没有对应 intent 的孤行。
    """

    connection = _RecordingConnection(conflicts=True)
    notices: list[dict[str, Any]] = []
    _file(connection, refused=(_refused("manual_task"),), notices=notices)

    assert not any("INSERT INTO manual_tasks" in s for s, _ in connection.statements)
    assert notices == [], "撞键之后也不该再推一次通知"


def test_a_refused_kind_that_is_not_a_task_kind_files_nothing() -> None:
    """**只有这两个 kind。** 被拒的 `remember` / `quote` 不是「有人要去做」。

    没有这一条，任何一个没建的 kind 都会给同事发一条他做不了的待办——
    而那正是把待办变成噪声的路。
    """

    connection = _RecordingConnection()
    _file(connection, refused=(_refused("remember"), _refused("quote")), notices=[])

    assert connection.statements == []
    assert "remember" not in TASK_FILING_KINDS
    assert "quote" not in TASK_FILING_KINDS


def test_a_manual_task_refused_for_another_reason_files_nothing() -> None:
    """筛的是**「这个 kind 没建」**，不是「这条提案被拒了」。

    一条载荷残缺的 `manual_task` 被拒，说明模型给的东西本身不成立——
    照着它派一条待办，同事拿到的是一件没人说得清的事。
    """

    connection = _RecordingConnection()
    _file(
        connection,
        refused=(_refused("manual_task", reason="GENERATION_PAYLOAD_INCOMPLETE"),),
        notices=[],
    )

    assert connection.statements == []


def test_the_reason_code_the_filter_matches_is_the_one_the_guard_writes() -> None:
    """**两处必须是同一个常量，而这一条是它们之间唯一的连接**。

    写它的是 `_decide_proposal`，按它挑的是 `_file_task_for_unsendable_intents`。
    两边各写一遍字面量的话，改名字的人只会改到一边——**而漏掉的那一边不报错，
    只是让待办悄悄不再产生**。这是今天已经在三处付过钱的形状。
    """

    decision = _processor()._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal={
            "kind": "manual_task",
            "typed_payload": {"content": {"type": "text", "text": "..."}},
            "evidence_refs": [f"event:{uuid4()}"],
        },
        ordinal=0,
        candidate_basis="basis-a",
    )

    assert decision.reason_code == UNSUPPORTED_INTENT_REASON
    assert decision.kind in TASK_FILING_KINDS


def test_the_notice_carries_the_fixed_template_and_the_model_s_own_words() -> None:
    """**固定模板 + 附模型原话，不是二选一**（用户裁定 2026-09-11）。

    模板一半让同事扫一眼就知道发生了什么、每次都一样；模型那一半让他能
    **用客户已经听过的那句话**把事接着办下去。只有模板的话，
    待办说「需要人工」却不说答应了什么；只有模型原话的话，
    那是一句没有框的话、不知从哪冒出来。
    """

    notice = manual_task_notice(
        reason_code=INTENT_NEEDS_A_PERSON_REASON,
        conversation_id=str(uuid4()),
        customer_message="那我试试",
        promised_text="好的，我把您标记一下，稍后同事联系您",
    )

    assert reason_sentence(INTENT_NEEDS_A_PERSON_REASON) in notice
    assert "客户最后一句：那我试试" in notice
    assert "AI 刚才答应：好的，我把您标记一下，稍后同事联系您" in notice


def test_the_notice_without_a_promise_is_unchanged() -> None:
    """其余调用方不传 `promised_text`，那一行就不许出现。

    **判据从「最后一行是客户那句」改成「客户那句在、承诺那行不在」**
    （2026-09-17）：通知末尾多了一行固定的「接手方式」，
    而那一行与本条要守的东西无关 —— 本条守的是
    **「AI 刚才答应」只在真的答应过时才出现**。

    拿「最后一行」当判据，是把一个位置当成了一个事实；
    位置会因为别的原因变，而事实不会。
    """

    conversation_id = str(uuid4())
    notice = manual_task_notice(
        reason_code="RATE_LIMITED", conversation_id=conversation_id, customer_message="在吗"
    )

    assert "AI 刚才答应" not in notice
    assert "客户最后一句：在吗" in notice.splitlines()


def test_the_promised_words_come_from_the_payload_not_the_draft() -> None:
    """读的是 `typed_payload.content`——**草稿不是发出去的东西**。

    `_validate_payload_agrees_about_materials` 量过这个差：三个真实回合里
    草稿引 1 / 3 / 1 条素材而 payload 一条不引，六个臂里六个都是。
    同事看到的必须与客户看到的是同一串字。
    """

    words = InboxProcessor._promised_words(
        (_sent("好的，我把您标记一下"),), materials=None
    )
    assert words == "好的，我把您标记一下"

    assert InboxProcessor._promised_words((), materials=None) is None


def test_the_notice_is_pushed_now_and_carries_the_promise() -> None:
    """推送那一跳真的被调用了，而且带着模型那句话。

    没有这一条，「附模型原话」可以在 `manual_task_notice` 里完全正确，
    而调用点根本不传——**常量对不对和有没有人用它是两个问题**。
    """

    notices: list[dict[str, Any]] = []
    _file(
        _RecordingConnection(),
        refused=(_refused("manual_task"),),
        promised=(_sent("好的，我把您标记一下"),),
        notices=notices,
    )

    assert len(notices) == 1
    assert notices[0]["reason_code"] == INTENT_NEEDS_A_PERSON_REASON
    assert notices[0]["promised_text"] == "好的，我把您标记一下"
    assert isinstance(notices[0]["manual_task_id"], UUID)


def test_the_new_reason_code_is_classified_and_has_its_own_sentence() -> None:
    """**一个没进表的 reason code 不会报错，只会拿到通用那句话。**

    2026-09-04 就踩过：`cost.py` 写的是小写 `cost_hard_ceiling`，而句子表里写的是
    大写——于是裁定里要立刻推的那两类拿到了通用兜底句，而为它们写的那句话
    在表里躺着没人够得着。分类对了所以路由是通的，**错的只有话**，
    也就是唯一会到达商家而不会到达别处的那一样东西。
    """

    classification = classify(INTENT_NEEDS_A_PERSON_REASON)
    assert classification.audience_class == MERCHANT_ACTION
    assert classification.cadence == IMMEDIATE
    assert classification.pushes_now is True
    assert reason_sentence(INTENT_NEEDS_A_PERSON_REASON) != UNKNOWN_SENTENCE


# ---------------------------------------------------------------------------
# 接线本身 —— 独立审查 2026-09-11 P1-1
#
# **上面每一条用例都直接调 `_file_task_for_unsendable_intents`。**
# 审查方把两处调用点整段删掉、函数与 12 条用例原样不动，
# `tests/contracts` 与未变异基线**逐位相同**，`ruff` 也全过
# （私有方法没了调用方不报）。**函数被证明了，接线没有。**
#
# 而接线正是这一批的全部价值：删掉它，客户仍然听见 AI 应下一件事、
# 同事那边仍然什么都没有，**而门禁全绿**。
# ---------------------------------------------------------------------------


class _NoSendConnection(_RecordingConnection):
    """`_RecordingConnection` 加上 `_persist_no_send` 一路要读回来的几个值。

    **单独一个子类而不是改上面那个**：上面那 12 条用例靠 `fetchone` 只返回
    `{"id": …}`（以及冲突时的 `None`）来表达「撞键了没有」，
    往那里加字段会让它们的绿变成别的原因的绿。
    """

    def __init__(self, *, state_version: int, generation: int) -> None:
        super().__init__()
        self._row = {
            "id": uuid4(),
            "state_version": state_version,
            "generation": generation,
            # 抑制计数那一步读的（同一轮已经有几条同码待办）。
            "total": 0,
        }

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        self.statements.append((statement, parameters))
        # `rowcount` 是乐观锁那一步读的：更新到 1 行才算没被抢先。
        # `fetchall` 是 B1-5 之后补的（回合收口要读活着的引用）——空列表，
        # 理由与上面那个替身相同：这一份不造意图行。
        return SimpleNamespace(fetchone=lambda: dict(self._row), rowcount=1, fetchall=list)


class _FakeDatabase:
    """`transaction()` 交出同一个记录型连接。

    **不是为了省事**：这条用例要断言的是「`_persist_no_send` 真的会调到落待办
    那一步」，而那一步写的是语句；把所有语句收进同一个流里，断言才读得出来。
    """

    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection
        self.transactions = 0

    @contextmanager
    def transaction(self, **_kwargs: Any) -> Any:
        self.transactions += 1
        yield self.connection


def _prepared_for_no_send(*, state_version: int = 1, generation: int = 0) -> Any:
    return SimpleNamespace(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        inbox_id=uuid4(),
        envelope={
            "event_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "event_type": "customer_message",
        },
        aggregate={
            # `definition_id` / `slots`：B1-1 之后收口那一句要按帧的定义找
            # `material_topic_slot`，而一个没有 `definition_id` 的帧在真实聚合里
            # 不存在（契约里是必填）——替身跟着真实形状走。
            "target_stack": [{"id": str(uuid4()), "definition_id": "welcome", "slots": {}}],
            "control_mode": "ai",
            "generation": generation,
            "state_version": state_version,
            # 下面这几个是 `_persist_no_send` 一路上真的读到的，**一个一个试出来的
            # 不是照着代码猜的** —— 与清除 e2e 那个 fixture 的注释同一条理由：
            # 「A fixture that guesses at column names is not cheaper than reading them.」
            "last_inbound_at": None,
            "profile_release_id": str(uuid4()),
            "last_transition": {"transition_id": str(uuid4())},
        },
        # 乐观锁那一步比的是这两个，与 aggregate 里的一致才算没被人抢先。
        previous_state_version=state_version,
        previous_generation=generation,
        slot_update_refusals=(),
        # **与 `slot_update_refusals` 同一条理由，也同一条教训**（批 11，2026-09-16）：
        # 台账那两格与它一样是「挂在 `prepared` 上、写在状态迁移里」的，
        # 于是 `_apply_aggregate_transition` 一读它们，这个替身就
        # `AttributeError` —— **而那正是这个替身该做的事**：
        # 它是手搭的，所以真实形状一变它就散架，而不是安静地测一个旧形状
        # （本仓记过「自造夹具藏得住形状不匹配」）。两格都给空元组 =
        # 「这一轮没有承诺被收下，也没有被拒」。
        admitted_commitments=(),
        commitment_refusals=(),
        # **台账的 AI 那一半，2026-09-18 加**。与上面那两格逐字同一条理由，
        # 也同一条教训：`_apply_aggregate_transition` 一读它们，
        # 这个替身就 `AttributeError` —— **而那正是它该做的事**。
        # 这一次它真的响了：加完写入方跑用例，报的就是
        # 「'types.SimpleNamespace' object has no attribute 'ai_commitments'」。
        ai_commitments=(),
        ai_commitment_refusals=(),
        frame_entry=None,
        delivered_materials=(),
        # **B1-1 之后这条路也会读它**：回合收口那一句要算 `focus`，
        # 而 contact 那一类取的就是这个字段（可空 —— 客户还没被识别时就是 `None`）。
        # 与上面那段注释同一条纪律：一个一个试出来的，不是照着代码猜的。
        contact_id=None,
        profile={"target_frames": []},
    )


def test_the_no_send_path_really_reaches_the_task_filer() -> None:
    """**走真实调用点，不是直接调那个函数。**

    这是 P1-1 要求的那一条：审查方删掉调用点之后什么都没红，
    因为没有一条用例走过 `_persist_no_send`。

    断言的是**落待办那一对写**出现在语句流里 —— 而它只可能来自
    `_file_task_for_unsendable_intents`，因为 `_persist_no_send` 自己那支
    handoff 分支用的是另一个理由码（`reason_code` 参数，这里给的是拒绝码）。
    """

    connection = _NoSendConnection(state_version=1, generation=0)
    database = _FakeDatabase(connection)
    processor = _processor()
    processor._database = database  # type: ignore[assignment]
    # 审计行里记 `model_version`，读的是 `self._inference.version`。
    processor._inference = SimpleNamespace(version="unit")  # type: ignore[assignment]

    notices: list[dict[str, Any]] = []

    def _capture(_connection: Any, **kwargs: Any) -> None:
        notices.append(kwargs)

    processor._notify_manager = _capture  # type: ignore[method-assign]

    result = processor._persist_no_send(
        _prepared_for_no_send(),
        plan=None,
        reason_code="PLAN_REJECTED",
        detail="the plan produced only an intent the send path cannot carry",
        proposal_denials=(_refused("handoff"),),
    )

    assert result.status == "no_send"

    filed = [
        statement
        for statement, parameters in connection.statements
        if "INSERT INTO manual_tasks" in statement
        and INTENT_NEEDS_A_PERSON_REASON in str(parameters)
    ]
    assert filed, (
        "the no-send path did not file a task for the refused handoff. "
        "This is the case that goes red when the call site is deleted -- "
        "every other case in this file calls the filer directly."
    )
    assert notices, "a filed task with no notification leaves the colleague with nothing"


def test_every_promised_sentence_reaches_the_colleague_not_just_the_first() -> None:
    """独立审查 2026-09-11，P2-2。

    一个回合可以携带多条提案（T44 明写，`decisions` 逐条独立，`kept` 是列表），
    而「先问好、再答应标记一下」是很自然的两条。先前 `_promised_words` 返回
    **第一条**读得出文字的 —— 于是同事读到的是问好那句，
    **而且没有任何迹象表明还有别的话**。

    **这不是少给信息，是给了一个会让人得出相反结论的信息**：
    看到「您好，很高兴为您服务」的同事会以为这条待办是误报。
    而「附模型原话」这条裁定的全部用处，就在那句承诺上。
    """

    notices: list[dict[str, Any]] = []
    _file(
        _RecordingConnection(),
        refused=(_refused("manual_task"),),
        promised=(_sent("您好，很高兴为您服务。"), _sent("这个我帮您标记一下。")),
        notices=notices,
    )

    assert notices, "a filed task with no notification leaves the colleague with nothing"
    promised_text = notices[0]["promised_text"]
    assert "您好，很高兴为您服务。" in promised_text
    assert "这个我帮您标记一下。" in promised_text, (
        "the promise itself was dropped -- this is the sentence the ruling is about"
    )
    # 顺序是客户听见的顺序，不是随手拼的。
    assert promised_text.index("您好") < promised_text.index("标记")
