"""`_delivered_materials` 读的，必须是 `_persist_accepted_proposal` 写的那个形状。

**独立审查 2026-09-11 的 P0-1 / P0-2 / P1-2。** 那两条 P0 合起来的效果是：
阶段过滤在生产里不工作，而且以最安静的方式不工作——`delivered` 恒空 →
阶段永远停在第一个 → 后续阶段的主动素材一段都发不出去，**没有异常、没有拒发、
没有红用例**。

**为什么以前没有任何东西发现。** 乙半边的用例全部停在纯函数层：`delivered`
由用例直接传字面量元组，从数据库到 `current_stage` 这一整段没有任何东西验过。
审查方的探针 I 把 `raise` 插进 `_delivered_materials` 首行跑全量 `tests/contracts`，
**一次都没被触发**。

**所以这一条刻意跨库**，而且两头都用生产代码：
写用 `_persist_accepted_proposal`，读用 `_delivered_materials`。
**用例里不手搓 `proposal` 那一列的形状**——手搓正是这个缺陷躲过审查的方式。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.worker.channel import MATERIAL_REF_SEGMENT_TYPE
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

from test_m1a_no_send import _accept_customer_message, _drive  # noqa: E402

from tests.wecom_proxy_callbacks import new_conversation  # noqa: E402

QUOTED = "material:demo-opening:v1"


def _processor() -> InboxProcessor:
    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=DeterministicMockInference(),
        worker_id="delivered-materials",
    )


def _prepared(tenant_id: UUID, conversation_id: UUID, aggregate: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        profile={"target_frames": [{"id": "welcome", "content_materials": [QUOTED]}]},
        envelope={
            "event_id": str(uuid4()),
            "event_type": "customer_message",
            "correlation_id": str(uuid4()),
            "runtime_deadline_at": "2036-09-10T12:00:00Z",
        },
        aggregate=aggregate,
    )


def _plan(definition: dict[str, Any], frame: dict[str, Any]) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        document={"candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}]},
        definition=definition,
        frame=frame,
        strategy_id="acknowledge",
    )


def _conversation_with_an_aggregate(
    settings: Any, seeded_database: dict[str, Any]
) -> tuple[UUID, UUID]:
    """一通**真的被处理过**的会话。

    `new_conversation` 只建会话，不建 `conversation_aggregates` 那一行——
    那是流水线处理第一条消息时才写的。第一版这个用例直接读 aggregate，
    于是两条都挂在 `assert aggregate_row is not None`：**那是搭建失败，
    不是缺陷的红**，差点被我当成 P0 的证据记进处置。
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    result = _drive(settings=settings, inference=DeterministicMockInference())
    assert result is not None and result.status == "consumed"
    return tenant_id, conversation_id


def test_a_quoted_material_written_by_the_send_path_is_read_back(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """**修之前这一条必须是红的。** 它同时是 P0-1 与 P0-2 的反向探针。

    P0-1：`proposal` 那一列装的是 `typed_payload` **本身**
    （`Jsonb(decision.proposal["typed_payload"])`），读的时候又剥了一层。
    P0-2：`APPROVED` 是瞬态，出站认领时就变成 `EXECUTING`——
    **真正发出去的那些，状态已经不是 `APPROVED` 了**。
    """

    tenant_id, conversation_id = _conversation_with_an_aggregate(settings, seeded_database)
    processor = _processor()

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        aggregate_row = connection.execute(
            "SELECT * FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert aggregate_row is not None
        aggregate = dict(aggregate_row)
        frame = dict(aggregate["target_stack"][-1])
        definition = {"id": str(frame["definition_id"]), "content_materials": [QUOTED]}

        prepared = _prepared(tenant_id, conversation_id, aggregate)
        plan = _plan(definition, frame)
        proposal = {
            "kind": "send_message",
            "evidence_refs": [f"event:{uuid4()}"],
            "typed_payload": {
                "content": {
                    "segments": [
                        {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": QUOTED},
                        {"type": "text", "content_or_ref": "还有一句我自己的。"},
                    ]
                }
            },
        }
        materials = {QUOTED: "你好，我是这边新上的助手。"}

        # 决策与落库都走生产代码：用例不碰 `proposal` 那一列的形状。
        decision = processor._decide_proposal(
            prepared,
            plan=plan,
            proposal=proposal,
            ordinal=0,
            candidate_basis="basis-a",
            materials=materials,
        )
        assert decision.accepted is True, decision.reason_code
        processor._persist_accepted_proposal(
            connection, prepared, plan=plan, decision=decision, materials=materials
        )

        # **出站认领把它推出 APPROVED**，这正是 P0-2 说的那一步。
        connection.execute(
            """
            UPDATE intent_records SET status = 'EXECUTING'
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, decision.intent_id),
        )

        delivered = processor._delivered_materials(
            connection, tenant_id=tenant_id, conversation_id=conversation_id
        )

    assert QUOTED in delivered, (
        "写进去的 material_ref 读不回来。P0-1（多剥一层 typed_payload）或 "
        "P0-2（只认 APPROVED，而发出去的已经是 EXECUTING）"
    )


def test_a_text_only_message_contributes_no_material(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """没有这一条，上面那条在一个「什么都返回」的实现上也会绿。"""

    tenant_id, conversation_id = _conversation_with_an_aggregate(settings, seeded_database)
    processor = _processor()

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        aggregate_row = connection.execute(
            "SELECT * FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert aggregate_row is not None
        aggregate = dict(aggregate_row)
        frame = dict(aggregate["target_stack"][-1])
        definition = {"id": str(frame["definition_id"]), "content_materials": []}

        prepared = _prepared(tenant_id, conversation_id, aggregate)
        plan = _plan(definition, frame)
        decision = processor._decide_proposal(
            prepared,
            plan=plan,
            proposal={
                "kind": "send_message",
                "evidence_refs": [f"event:{uuid4()}"],
                "typed_payload": {
                    "content": {"segments": [{"type": "text", "content_or_ref": "只有文本。"}]}
                },
            },
            ordinal=0,
            candidate_basis="basis-a",
            materials={},
        )
        assert decision.accepted is True, decision.reason_code
        processor._persist_accepted_proposal(
            connection, prepared, plan=plan, decision=decision, materials={}
        )
        connection.execute(
            "UPDATE intent_records SET status = 'APPLIED' WHERE tenant_id = %s AND id = %s",
            (tenant_id, decision.intent_id),
        )

        delivered = processor._delivered_materials(
            connection, tenant_id=tenant_id, conversation_id=conversation_id
        )

    assert delivered == ()


@pytest.mark.parametrize("status", ["APPROVED", "UNKNOWN", "FAILED", "DENIED"])
def test_a_status_the_ruling_excludes_does_not_advance_the_stage(
    settings: Any, seeded_database: dict[str, Any], status: str
) -> None:
    """**ADR-0125 §6.1 的排除项，直到这一条为止没有任何东西守着**
    （独立复审 2026-09-11，P3-5）。

    上面两条覆盖的是「算」的那两个状态。把过滤放宽成 `status != 'PROPOSED'`
    不会让它们中的任何一条变红——**而「`UNKNOWN` 不算」恰恰是用户 2026-09-11
    亲自做的那个产品判断**：结果未知的算作已发，若它其实失败了，阶段就跳过去、
    对话从中间开始；不算，最多把开场重复一遍。

    这与本批自己的论点同形（ADR-0125 §三）：**能变成门禁的约束，不留在散文里。**
    """

    tenant_id, conversation_id = _conversation_with_an_aggregate(settings, seeded_database)
    processor = _processor()

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        aggregate_row = connection.execute(
            "SELECT * FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert aggregate_row is not None
        aggregate = dict(aggregate_row)
        frame = dict(aggregate["target_stack"][-1])
        definition = {"id": str(frame["definition_id"]), "content_materials": [QUOTED]}

        prepared = _prepared(tenant_id, conversation_id, aggregate)
        plan = _plan(definition, frame)
        decision = processor._decide_proposal(
            prepared,
            plan=plan,
            proposal={
                "kind": "send_message",
                "evidence_refs": [f"event:{uuid4()}"],
                "typed_payload": {
                    "content": {
                        "segments": [
                            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": QUOTED}
                        ]
                    }
                },
            },
            ordinal=0,
            candidate_basis="basis-a",
            materials={QUOTED: "你好，我是这边新上的助手。"},
        )
        assert decision.accepted is True, decision.reason_code
        processor._persist_accepted_proposal(
            connection,
            prepared,
            plan=plan,
            decision=decision,
            materials={QUOTED: "你好，我是这边新上的助手。"},
        )
        connection.execute(
            "UPDATE intent_records SET status = %s WHERE tenant_id = %s AND id = %s",
            (status, tenant_id, decision.intent_id),
        )

        delivered = processor._delivered_materials(
            connection, tenant_id=tenant_id, conversation_id=conversation_id
        )

    assert QUOTED not in delivered, (
        f"状态 {status} 不该推进阶段（ADR-0125 §6.1）——"
        "客户没看见的东西，不构成「这通对话谈到哪一步」"
    )
