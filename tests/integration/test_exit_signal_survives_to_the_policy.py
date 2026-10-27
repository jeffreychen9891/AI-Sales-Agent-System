"""The durable exit signal is still readable when the policy runs. **B4-4c.**

`tests/contracts/test_exit_signals_stop_proactive_sending.py` proves the mapping.
**It cannot prove this**, because this is not about the mapping at all -- it is
about *when* the value is read.

`process_prepared` **clears `pending_exit_signal` on the aggregate** the moment
it hands the signal to the planner (T53/T54's consumption rule: 「Consumed
whether or not the plan survives validation」), and the proactivity policy runs
**after** that. So an implementation that reads `aggregate["pending_exit_signal"]`
inside the policy finds `None` on every single turn -- **every contract case
still passes**, because they pass the value in directly, and the durable half of
ADR-0115's mapping is dead with nothing failing.

The wiring is what has to be tested, so this drives the real pipeline.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.proactivity import EXIT_PERMANENT_REASON
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
            dispatcher_id="b44c-seed",
        ).dispatch_one()
        is not None
    )


def _record_a_customer_opt_out(tenant_id: UUID, conversation_id: UUID) -> None:
    """Put a durable `exit` signal on the aggregate, the way a takeover does.

    `_process_under_human_control` is the only writer: during a takeover the
    understanding step still runs, so a 「别再发了」 said to a human agent is
    recorded rather than dropped (ADR-0054 §三.5). Written directly here because
    driving a full takeover-and-resume would test that path, not this one.
    """

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        state["pending_exit_signal"] = {
            "event_id": str(uuid4()),
            "relation": "exit",
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()


def _fire_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the delivery left no pending timer to fire"
    assert Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="b44c-scheduler",
        runtime_deadline_seconds=600,
    ).enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id="b44c-worker",
    ).process_one(block_ms=1000)


def test_a_customer_who_asked_us_to_stop_is_not_messaged_by_a_timer(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The whole of B4-4c, on the path production takes.

    The customer said 「别再发了」 to a human; the conversation came back to the
    AI; a `silence_check` fell due. **Nothing may go out**, and the refusal has
    to name itself as the permanent kind.

    **The assertion that makes this test about wiring rather than mapping** is
    the last one: the signal must have been read *and* consumed. If the policy
    read `aggregate["pending_exit_signal"]` itself it would have seen `None` --
    `process_prepared` clears it before the policy runs -- and the timer would
    have produced a message with every contract case still green.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)
    _record_a_customer_opt_out(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    with _migration_connection() as connection:
        queued = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
        # **Every row, not `fetchone()`** -- a turn where nothing survives
        # writes the reason code **twice**: once at turn level
        # (`_persist_no_send`) and once per proposal (`_record_proposal_denial`),
        # and only the second carries `detail`. The first version of this case
        # took whichever row came back first and died on a `KeyError`, which is
        # a test bug rather than a defect, but it is also the reason the
        # assertion below asks "does any row name the signal" instead of
        # "does that row".
        denials = connection.execute(
            "SELECT reason_code, context FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'"
            "   AND reason_code = %s",
            (tenant_id, conversation_id, EXIT_PERMANENT_REASON),
        ).fetchall()
    assert queued is not None and queued["n"] == 0, (
        "a customer who asked us to stop was sent a proactive message"
    )
    assert denials, "the refusal did not name itself as the permanent kind"
    assert any(
        "explicit_opt_out" in str(row["context"].get("detail", "")) for row in denials
    ), "no refusal row says which signal stopped the send"
