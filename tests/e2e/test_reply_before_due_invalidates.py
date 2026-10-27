"""A customer reply before the timer comes due ends that timer, on the real chain.

**B4-3's exit criterion**, and the reason it could not be written before now:
until B4-2 there was **no production writer** for `scheduled_events`, so every
existing test of this behaviour started by inserting a fixture row. A fixture
proves the `UPDATE` matches rows; it cannot prove that the row a real
conversation produces is one of them -- the generation it carries, the status it
is in, and the moment it appears are all things the fixture chose.

So this drives the whole chain: a customer message is accepted and processed, the
reply is delivered (which is what creates the timer), and then **the customer
speaks again before the 75 seconds are up**. The assertion is on the timer row
that the first turn really produced.

**T3** is the registered item this closes on the semantics side: `frame_generation`
is now a creation pin (ADR-0098 §二), and the invalidation this file exercises is
the one that keys on the **aggregate** generation -- the two counters answer
different questions, which is exactly why the ADR refused to let one field mean
both.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import inference_for_provider
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e]


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


def _turn(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    """One customer message, processed, and its reply delivered."""

    from test_retry_budget import _accept  # noqa: PLC0415 - suite-local helper

    _accept(settings=settings, seeded_database=seeded, conversation_id=conversation_id)
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference_for_provider(settings.model_provider),
        worker_id="b43-worker",
    )
    assert processor.process_one(block_ms=500) is not None


def _deliver(settings: Settings) -> None:
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b43-dispatcher",
    )
    assert dispatcher.dispatch_one() is not None


def _timers(tenant_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT status, due_at, invalidation_reason, invalidated_at, aggregate_generation,"
            "       invalidated_by_event_id"
            "  FROM scheduled_events WHERE tenant_id = %s AND conversation_id = %s"
            " ORDER BY aggregate_generation",
            (tenant_id, conversation_id),
        ).fetchall()
    return [dict(row) for row in rows]


def test_a_reply_before_the_timer_is_due_ends_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The whole point of a silence timer: silence ended, so the timer is over.

    The timer is 75 seconds out and the reply arrives immediately, so this is the
    ordinary case rather than a race -- and before B4-2 it was untestable end to
    end, because nothing produced the row being invalidated.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _turn(settings, seeded_database, conversation_id)
    _deliver(settings)

    created = _timers(tenant_id, conversation_id)
    assert len(created) == 1, f"the first turn did not leave a timer: {created}"
    assert created[0]["status"] == "PENDING"
    assert created[0]["due_at"] > datetime.now(created[0]["due_at"].tzinfo), (
        "the timer was already due when it was written; this case would then be "
        "asserting nothing about *before* the deadline"
    )

    # The customer replies. A customer message bumps the aggregate generation,
    # and the invalidation keys on exactly that.
    _turn(settings, seeded_database, conversation_id)

    after = _timers(tenant_id, conversation_id)
    ended = after[0]
    assert ended["status"] == "INVALIDATED", (
        f"the timer survived the reply it was waiting for: {ended}"
    )
    assert ended["invalidation_reason"] == "CUSTOMER_MESSAGE_NEW_GENERATION"
    assert ended["invalidated_at"] is not None
    assert ended["invalidated_by_event_id"] is not None, (
        "an invalidated row must name the event that ended it, or nobody can tell "
        "which reply this was about"
    )
    # Still one row: the reply's own timer is not created until its reply is
    # *delivered*, and this case stops before that (the second case below picks
    # it up). Asserting the count keeps "invalidated" from being satisfied by a
    # second, live row that happened to be written in the meantime.
    assert len(after) == 1, after


def test_the_next_turn_gets_its_own_timer_rather_than_reviving_the_old_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Invalidation is not deletion, and the new generation starts clean.

    Two rows with the same dedupe key coexist because the unique key includes
    `aggregate_generation` -- the old one as a record of what was cancelled, the
    new one as the live timer. Without this case, "the timer was invalidated"
    would be satisfied by a conversation that then never gets another one.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _turn(settings, seeded_database, conversation_id)
    _deliver(settings)
    _turn(settings, seeded_database, conversation_id)
    _deliver(settings)

    rows = _timers(tenant_id, conversation_id)

    assert len(rows) == 2, f"expected the cancelled row and the live one: {rows}"
    old, live = rows
    assert old["status"] == "INVALIDATED"
    assert live["status"] == "PENDING"
    assert live["aggregate_generation"] > old["aggregate_generation"]
    assert live["due_at"] > old["due_at"], (
        "the second turn's timer is anchored on the second turn's outbound"
    )
