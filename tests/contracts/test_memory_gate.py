"""不排出站行的提案，**不走出站那两道闸**；而它自己那道闸问的是写入时的真相。

两条 P1 的守卫（E4-2 独立审查 2026-09-12）：

* **P1-1** —— `remember` 被送进了 `_rate_gate` / `_reengagement_gate`，
  两个方向都错：它占名额，它也会被名额裁掉。
  被裁掉那一支尤其贵：`_record_rate_limit` 会把 `withheld_content`
  （**客户事实本身**）写进 `policy_decisions.context`，
  落一条语义不对的待办，然后把这条记忆丢掉、理由码写「限流」。
* **P1-2** —— 「这个字段已经有记忆了吗」问的是 `prepare()` 事务里的旧快照，
  而写入在另一个事务里。同一回合两条同名提案会双双通过，
  第二行撞唯一索引 → 整轮回滚 → 重试再炸。

**这些用例都不需要数据库**：闸的逻辑是纯的，要的只是一个会答话的连接替身。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.worker.pipeline import (
    GENERATION_WRITE_PATHS,
    UNRECORDABLE_INTENT_REASON,
    DirectWrite,
    InboxProcessor,
    OutboxDelivery,
    PreparedEvent,
    ProposalDecision,
    _queues_an_outbox_row,
)

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services/worker/src/fastagent_v2/worker/pipeline.py"
)

CONTACT = UUID("11111111-1111-4111-8111-111111111111")


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Connection:
    """一个只会答三句话的连接替身，**并且记下被问了什么**。

    锁那一句要被断言**确实发过**——一道「取了锁」的闸和一道「忘了取锁」的闸
    在所有单元用例里长得一模一样，除非有人去看它发了哪些语句。
    """

    def __init__(
        self, *, active_fields: tuple[str, ...], contact_id: UUID | None = CONTACT
    ) -> None:
        self.active_fields = active_fields
        self.contact_id = contact_id
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> _Rows:
        self.statements.append(" ".join(sql.split()))
        if "pg_advisory_xact_lock" in sql:
            return _Rows([])
        if "FROM conversation_aggregates" in sql:
            return _Rows([{"contact_id": self.contact_id}])
        if "FROM contact_memories" in sql:
            return _Rows([{"field_key": key} for key in self.active_fields])
        raise AssertionError(f"替身没有为这句准备答案：{sql[:80]}")


def _prepared() -> PreparedEvent:
    return PreparedEvent(
        inbox_id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        envelope={"event_type": "customer_message", "event_id": str(uuid4())},
        profile={},
        aggregate={},
        previous_state_version=0,
        previous_generation=0,
        contact_id=CONTACT,
    )


def _remember(field_key: str, ordinal: int = 0) -> ProposalDecision:
    return ProposalDecision(
        ordinal=ordinal,
        kind="remember",
        accepted=True,
        reason_code="",
        detail="",
        proposal={
            "kind": "remember",
            "typed_payload": {"field_key": field_key, "field_value": "18 号"},
        },
    )


# ------------------------------------------------------------------ P1-1


def test_a_memory_is_not_deliverable_and_a_message_is() -> None:
    """分流谓词本身。**两个调用点共用它**，所以它只有一个定义。"""

    assert _queues_an_outbox_row(_remember("ring_size")) is False
    assert isinstance(GENERATION_WRITE_PATHS["remember"].effect, DirectWrite)

    message = ProposalDecision(
        ordinal=0, kind="send_message", accepted=True, reason_code="", detail=""
    )
    assert _queues_an_outbox_row(message) is True
    assert isinstance(GENERATION_WRITE_PATHS["send_message"].effect, OutboxDelivery)


def test_the_outbound_gates_are_handed_the_split_list_not_everything() -> None:
    """**`remember` 不占名额，也不会出现在 `withheld` 里** —— 审查要求的那两条。

    扫源码，因为要证的是「某个变量**没有**被传进去」——
    把 `deliverable` 改回 `accepted` 不会让任何运行期用例变红，
    除非恰好有一条用例在同一回合里同时提了一条记忆和一句话**并且撞上了名额上限**。
    **那是一条要靠巧合才存在的用例**，而这条不靠巧合。
    """

    text = "\n".join(
        line
        for line in SOURCE.read_text(encoding="utf-8").split("\n")
        if not line.lstrip().startswith("#")
    )

    rate = re.search(r"self\._rate_gate\(\s*connection,\s*prepared,\s*(\w+)\)", text)
    assert rate is not None, "找不到 `_rate_gate` 的调用点，本条的前提变了"
    assert rate.group(1) == "deliverable", (
        f"`_rate_gate` 收到的是 {rate.group(1)!r}。它按 outbox 的客户行算名额，"
        "而一条不产出站行的提案既会占掉名额，也会被名额裁掉 —— "
        "被裁掉那一支还会把客户事实写进 policy_decisions.context（P1-1）"
    )

    reengagement = re.search(
        r"self\._reengagement_gate\(\s*connection,\s*prepared,\s*(\w+),", text
    )
    assert reengagement is not None, "找不到 `_reengagement_gate` 的调用点"
    assert reengagement.group(1) == "deliverable", (
        f"`_reengagement_gate` 收到的是 {reengagement.group(1)!r}；同 P1-1。"
    )


# ------------------------------------------------------------------ P1-2


def test_two_proposals_for_one_field_in_one_turn_do_not_both_pass() -> None:
    """**同一回合内的缺口**（P1-2 ①）。

    修之前两条都通过，然后第二行撞 `contact_memories_one_active_per_field_idx`：
    事务内异常 → 整轮回滚 → inbox 事件不消费 → 退回重试预算再炸一次。
    **那正是自审 §2.3 用来论证这道闸存在理由的那个形状**，
    也是 T44 的原话换了一张表。
    """

    connection = _Connection(active_fields=())
    kept, occupied = InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [_remember("ring_size", 0), _remember("ring_size", 1)],
    )

    assert [d.ordinal for d in kept] == [0]
    assert [d.ordinal for d in occupied] == [1]
    assert occupied[0].accepted is False
    assert occupied[0].reason_code == UNRECORDABLE_INTENT_REASON
    assert "ring_size" in occupied[0].detail


def test_a_field_already_active_in_the_write_transaction_is_refused() -> None:
    """**跨事务的缺口**（P1-2 ②）：闸重读的是写入事务里的真相，不是 `prepare()` 的快照。

    `PreparedEvent.remembered_fields` 在这里**故意留空**——
    一个仍然只看那份快照的实现会让这条通过。
    """

    connection = _Connection(active_fields=("ring_size",))
    kept, occupied = InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [_remember("ring_size")],
    )

    assert kept == []
    assert len(occupied) == 1
    assert occupied[0].reason_code == UNRECORDABLE_INTENT_REASON


def test_different_fields_are_not_blocked_by_each_other() -> None:
    """占用按**字段**算，不按 contact 算。

    一个把去重写成「这个 contact 这回合已经记过了」的实现，
    在上面两条里照样绿。
    """

    connection = _Connection(active_fields=("ring_size",))
    kept, occupied = InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [_remember("anniversary", 0), _remember("budget", 1)],
    )

    assert [d.ordinal for d in kept] == [0, 1]
    assert occupied == []


def test_the_contact_lock_is_actually_taken() -> None:
    """**跨会话那一半靠的是这把锁**，而忘了取它的实现在别的用例里全绿。

    `conversation_aggregates` 的 `FOR UPDATE` 按会话锁，看不见兄弟会话；
    `_reengagement_gate` 为完全相同的理由取了同一把咨询锁。
    """

    connection = _Connection(active_fields=())
    InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [_remember("ring_size")],
    )

    locks = [s for s in connection.statements if "pg_advisory_xact_lock" in s]
    assert len(locks) == 1, f"取了 {len(locks)} 次锁，期望 1 次：{connection.statements}"
    # **锁在重读之前**——反过来取等于读完才上锁，中间那段仍然是敞开的。
    assert connection.statements.index(locks[0]) < next(
        i for i, s in enumerate(connection.statements) if "FROM contact_memories" in s
    )


def test_nothing_to_write_means_no_lock_at_all() -> None:
    """**绝大多数回合一条记忆都不提**，那些回合不该为此取一把锁。

    这一条守的是上面那条注释里承认的代价：
    「取锁会经常发生」——**只在真有东西要写时才发生**。
    """

    connection = _Connection(active_fields=())
    kept, occupied = InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [],
    )

    assert (kept, occupied) == ([], [])
    assert connection.statements == []


def test_a_contact_that_vanished_between_the_two_reads_is_refused_not_written() -> None:
    """闸 4 已经拒过「没有 contact」，能走到这里说明它在两次读之间没了。

    **静默通过会撞 NOT NULL**，而那又是一次整轮回滚。

    ## 断言强度补齐（独立审查复审 2026-09-12，R1）

    这条用例第一版**只断言了 `len(occupied) == 1`**，
    而它的两条兄弟用例都断言了 `accepted is False` 与 reason code。
    **那个缺口正好落在唯一没改写决策的那条分支上**：
    交回去的决策仍然 `accepted=True`、`reason_code='M0_AI_CONTROL_ALLOWED'`、
    `detail=''`，而调用点紧接着 `_record_proposal_denial` ——
    **落库的是一条「effect=deny，理由是策略放行了，没有理由文字」的拒绝记录。**

    **一条只数数量的用例，数对了也证明不了那一条是什么。**
    """

    connection = _Connection(active_fields=(), contact_id=None)
    kept, occupied = InboxProcessor._memory_gate(
        connection,
        _prepared(),
        [_remember("ring_size")],
    )

    assert kept == []
    assert len(occupied) == 1
    assert occupied[0].accepted is False, (
        "交回去的决策仍然是 accepted=True —— `_record_proposal_denial` 会把它写成"
        "一条自相矛盾的拒绝记录"
    )
    assert occupied[0].reason_code == UNRECORDABLE_INTENT_REASON, (
        f"reason_code 是 {occupied[0].reason_code!r}，那是决策链给的值，不是拒绝的理由"
    )
    assert occupied[0].detail, "拒绝要带理由文字，否则运维读到的是一条空拒绝"
    assert "contact" in occupied[0].detail
