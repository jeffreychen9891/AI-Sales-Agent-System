"""The rule's declared strategy reaches the intent. **B4-4d / ADR-0105 §零.1.**

`tests/contracts/test_time_rule_strategy_binds_the_timer_turn.py` proves the
planner is told and the validator holds it there. **It cannot prove the rule id
survives the trip**: `payload_ref` is written by the transition, copied into the
timer envelope by the scheduler, and read back out here -- three hops, none of
them exercised by a contract case.

So this drives a real delivery, a real timer, and looks at what
`intent_records.strategy_id` ended up saying.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]


class AcceptingAdapter:
    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _deliver_one(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    assert (
        OutboxDispatcher(
            database=Database(settings.worker_database_url),
            channel=AcceptingAdapter(),
            dispatcher_id="b44d-seed",
        ).dispatch_one()
        is not None
    )


class PrefersItsOwnStrategyInference(DeterministicMockInference):
    """Ignores the narrowing and picks a play the **frame** allows.

    `acknowledge` is in `welcome.allowed_strategy_set`, so the frame allowlist
    cannot refuse it and neither can the strategy catalog. **The only thing in
    the repository that can refuse this plan is the rule check**, which is what
    makes it a probe of the wiring rather than of the validator.
    """

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["selected_strategy_id"] = "acknowledge"
        document["candidate_intents"] = []
        return document


def _fire_the_timer(
    settings: Settings,
    tenant_id: UUID,
    conversation_id: UUID,
    inference: DeterministicMockInference | None = None,
) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at, payload_ref FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the delivery left no pending timer to fire"
    assert row["payload_ref"] == {"rule_id": "welcome-silence_check"}, (
        "the row must carry the rule id and nothing copied from the rule -- that is what "
        "makes the strategy readable from the pinned release instead of from a second copy"
    )
    assert Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="b44d-scheduler",
        runtime_deadline_seconds=600,
    ).enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference or DeterministicMockInference(),
        worker_id="b44d-worker",
    ).process_one(block_ms=1000)


def test_a_timer_turn_plays_the_strategy_its_rule_declared(
    quiet_hours_out_of_the_way: None,
    settings: Settings,
    seeded_database: dict[str, Any],
) -> None:
    """`synthetic-third`'s `welcome-silence_check` declares `advance_goal`.

    **The assertion is the difference between two strategies, and that is the
    whole point**: the mock planner prefers `acknowledge` whenever the frame
    allows it, and `acknowledge` **is** allowed in this frame. So a
    `strategy_id` of `advance_goal` can only have come from the rule -- it is
    not what the model would have chosen, and it is not a value this test could
    get by accident.

    **Three hops are under test and none of them is visible to a contract
    case**: the transition writes `payload_ref = {"rule_id": …}`, the scheduler
    copies it into the timer envelope, and `_required_strategy` reads the rule
    back out of the **pinned release** -- never from a copy on the row (T205's
    lesson, applied before it could be repeated).
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)
    _fire_the_timer(settings, tenant_id, conversation_id)

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT i.strategy_id FROM outbox_messages o"
            "  JOIN intent_records i ON i.tenant_id = o.tenant_id AND i.id = o.intent_id"
            " WHERE o.tenant_id = %s AND o.conversation_id = %s"
            "   AND o.command ? 'proactivity'",
            (tenant_id, conversation_id),
        ).fetchall()
    assert rows, "the timer turn produced no proactive message"
    assert [row["strategy_id"] for row in rows] == ["advance_goal"], (
        "the timer turn used the model's default instead of the strategy its rule declared"
    )


def test_a_timer_plan_that_ignores_the_rule_ends_in_the_safe_fallback(
    quiet_hours_out_of_the_way: None,
    settings: Settings,
    seeded_database: dict[str, Any],
) -> None:
    """**The other half of the wiring, and the half nothing was watching.**

    B4-4d does two things: it **tells** the planner the rule's strategy and it
    **holds** the plan to it in the validator. The case above only covers the
    first -- it asserts `strategy_id == "advance_goal"`, and that value is
    produced by the telling half all on its own. Delete
    `required_strategy=required_strategy` from the `plan_or_fallback` call and
    that case stays green: `required_strategy` becomes `None`, the frame
    allowlist takes over, and `advance_goal` is in it (independent review
    2026-09-09, P2-2). **The contract cases cannot see this either** -- they
    call `validate_plan` / `plan_or_fallback` directly and pass the argument
    themselves, so they exercise the check and never the wire that feeds it.

    So this drives a real timer turn with a planner that **ignores** the
    narrowing and answers `acknowledge` -- legal for the frame, legal in the
    catalog, and **not** what `welcome-silence_check` declared. The only thing
    in the repository that can refuse it is the rule check, reached only
    through that argument. With the argument gone, the message goes out.

    The observable is `policy_decisions`: a rejected plan is persisted by
    `_persist_no_send`, and `STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED` is in
    neither the handoff nor the task-only set, so there is no manual task and
    no intent record to read -- the deny row and the absent message are the
    whole record.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)
    _fire_the_timer(
        settings, tenant_id, conversation_id, inference=PrefersItsOwnStrategyInference()
    )

    with _migration_connection() as connection:
        denials = connection.execute(
            "SELECT reason_code FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s"
            "   AND hook = 'post_plan' AND effect = 'deny'",
            (tenant_id, conversation_id),
        ).fetchall()
        proactive = connection.execute(
            "SELECT id FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND command ? 'proactivity'",
            (tenant_id, conversation_id),
        ).fetchall()

    assert [row["reason_code"] for row in denials] == [
        "STRATEGY_NOT_THE_ONE_THE_TIME_RULE_DECLARED"
    ], "the rule check did not refuse a plan that named another strategy"
    assert not proactive, (
        "a timer turn that ignored the rule still produced a proactive message -- "
        "the narrowing became a suggestion"
    )
