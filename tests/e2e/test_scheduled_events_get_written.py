"""`scheduled_events` finally has a production writer, proved against the table.

**B4-2 / ADR-0096 §二.1.** The batch's exit criterion is literally that the table
the scheduler drains **finally has somebody writing to it** -- so the assertion
has to be a row read back out of PostgreSQL, not a draft object. Until this
batch `INSERT INTO scheduled_events` existed only in test fixtures: the
scheduler was draining a table nobody filled, and the model's proposals were
validated and dropped.

`tests/contracts/test_time_rule_producer.py` proves the rule computes the right
`due_at` and `dedupe_key`. **That is the half that can pass while nothing is
reached** -- the shape this repository keeps paying for -- so this file drives a
real customer message through the pipeline and a real delivery through the
dispatcher, and then looks in the table.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.pipeline import OutboxDispatcher
from fastagent_v2.worker.time_rules import TimeRuleError
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e]

#: `profiles/synthetic-third` is what the suite seeds (`synthetic-service-desk`),
#: and its `welcome` frame carries exactly one rule. The numbers below are that
#: rule's, quoted rather than recomputed: a test that derived them from the
#: profile would agree with the producer about a value both read from the same
#: place.
RULE_ID = "welcome-silence_check"
RULE_KIND = "silence_check"
RULE_OFFSET_SECONDS = 75


class AcceptingAdapter:
    """A channel that accepts, because this file is about what happens after."""

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
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b42-test",
    )
    assert dispatcher.dispatch_one() is not None


def _aggregate_row(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT last_outbound_at, generation, state_version, state"
            "  FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return dict(row)


def _scheduled_rows(tenant_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT kind, due_at, dedupe_key, status, payload_ref, aggregate_generation,"
            "       target_frame_id, target_stack_version, created_by_transition_id"
            "  FROM scheduled_events WHERE tenant_id = %s AND conversation_id = %s"
            " ORDER BY due_at",
            (tenant_id, conversation_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _queue_a_second_outbound(tenant_id: UUID, conversation_id: UUID) -> None:
    """A second intent and a second outbox row -- what a multi-intent turn leaves.

    A second row needs its own intent: `outbox_messages` is `UNIQUE (tenant_id,
    intent_id)` and `delivery_receipts` is `UNIQUE (tenant_id, outbox_id)`, so
    re-delivering the first row is not a thing production can do. The intent is
    copied from the one already there, which keeps every foreign key and every
    NOT NULL satisfied without this test having to know the table's shape.
    """

    with _migration_connection() as connection:
        source = connection.execute(
            "SELECT o.command, i.* FROM outbox_messages o"
            "  JOIN intent_records i ON i.tenant_id = o.tenant_id AND i.id = o.intent_id"
            " WHERE o.tenant_id = %s AND o.conversation_id = %s LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
        assert source is not None, "no first delivery to copy an intent from"
        intent_id = uuid4()
        connection.execute(
            "INSERT INTO intent_records("
            "  tenant_id, id, conversation_id, kind, status, proposal, proposed_by,"
            "  strategy_id, target_frame_id, idempotency_key, preconditions, policy_evidence"
            ") SELECT tenant_id, %s, conversation_id, kind, 'PROPOSED', proposal, proposed_by,"
            "  strategy_id, target_frame_id, %s, preconditions, policy_evidence"
            "  FROM intent_records WHERE tenant_id = %s AND id = %s",
            (intent_id, f"send_message:{uuid4()}", tenant_id, source["id"]),
        )
        connection.execute(
            "INSERT INTO outbox_messages(tenant_id, id, conversation_id, intent_id, command,"
            "  idempotency_key, status, attempt)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', 0)",
            (
                tenant_id,
                uuid4(),
                conversation_id,
                intent_id,
                # `jsonb` comes back as a dict and has to go back as `Jsonb`.
                Jsonb(source["command"]),
                f"send_message:{uuid4()}",
            ),
        )
        connection.commit()


def test_a_delivered_message_leaves_a_pending_timer_the_rule_asked_for(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The criterion, in one assertion block: the row exists and it is the rule's.

    Every field is checked against something outside the producer:
    `due_at` against the `last_outbound_at` the database stamped,
    `created_by_transition_id` against the transition on the aggregate, and the
    dedupe key against the active frame's id. A row that merely existed would
    satisfy "the table has a writer" while saying nothing about *which* rule
    wrote it.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _deliver_one(settings, seeded_database, conversation_id)

    aggregate = _aggregate_row(tenant_id, conversation_id)
    assert aggregate["last_outbound_at"] is not None, "the delivery did not stamp the anchor"
    frame = next(
        entry for entry in aggregate["state"]["target_stack"] if entry["status"] == "active"
    )

    rows = _scheduled_rows(tenant_id, conversation_id)
    assert len(rows) == 1, f"expected exactly the rule's row, got {rows}"
    row = rows[0]
    assert row["kind"] == RULE_KIND
    assert row["status"] == "PENDING"
    assert row["dedupe_key"] == f"{RULE_KIND}:{frame['id']}"
    # **The anchor is the value the database wrote**, not this process's clock:
    # the producer reads `last_outbound_at` back from the same UPDATE.
    assert row["due_at"] == aggregate["last_outbound_at"] + timedelta(
        seconds=RULE_OFFSET_SECONDS
    )
    assert str(row["target_frame_id"]) == str(frame["id"])
    assert row["aggregate_generation"] == aggregate["generation"]
    assert row["target_stack_version"] == aggregate["state_version"]
    assert str(row["created_by_transition_id"]) == str(
        aggregate["state"]["last_transition"]["transition_id"]
    ), "ADR-0096 四.3: the row must name the transition that produced it"
    # The rule id, and nothing copied from the rule: `strategy` stays where it
    # is, in the pinned release.
    assert row["payload_ref"] == {"rule_id": RULE_ID}


def test_a_second_delivery_in_the_same_turn_moves_the_timer_to_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The ruling of 2026-09-09 (a), driven through the real dispatcher twice.**

    A turn can produce more than one outbound -- T44 exists because a turn can
    produce several intents, each with its own outbox row and its own delivery --
    and every delivery moves `conversation_aggregates.last_outbound_at`. Before
    this ruling the timer stayed anchored to the **first** of them, because the
    dedupe key carries no ordinal and the conflict did nothing. ADR-0105 §零.2
    anchors silence on the platform's *last* outbound, so the row has to follow
    it.

    **Two real intents, two real outbox rows, two real dispatch runs** -- the
    previous version of this case called the writer directly with a hand-made
    draft, which proved the constraint and not this path (independent review
    2026-09-09, P1-1).
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _deliver_one(settings, seeded_database, conversation_id)
    first_rows = _scheduled_rows(tenant_id, conversation_id)
    assert len(first_rows) == 1
    first_due_at = first_rows[0]["due_at"]

    _queue_a_second_outbound(tenant_id, conversation_id)
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b42-second",
    )
    assert dispatcher.dispatch_one() is not None

    aggregate = _aggregate_row(tenant_id, conversation_id)
    rows = _scheduled_rows(tenant_id, conversation_id)

    assert len(rows) == 1, f"the second delivery duplicated the timer: {rows}"
    assert rows[0]["due_at"] == aggregate["last_outbound_at"] + timedelta(
        seconds=RULE_OFFSET_SECONDS
    ), "the timer is not anchored on the outbound that actually went last"
    assert rows[0]["due_at"] > first_due_at, (
        "the second delivery left the timer where the first one put it -- that is "
        "the first-wins behaviour the ruling replaced"
    )


def test_a_timer_already_in_flight_is_not_moved_under_its_owner(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`WHERE status = 'PENDING'`, which is the whole of the caution in the ruling.

    A `CLAIMED` row is leased by the scheduler and an `ENQUEUED` one already has
    an inbox row; moving either one's deadline would be a second writer to a row
    somebody else owns. So a later delivery re-anchors a pending timer and leaves
    an in-flight one exactly where it is.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _deliver_one(settings, seeded_database, conversation_id)
    before = _scheduled_rows(tenant_id, conversation_id)
    assert len(before) == 1

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE scheduled_events SET status = 'CLAIMED', lease_owner = 'scheduler-1',"
            " lease_expires_at = now() + interval '30 seconds'"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        )
        connection.commit()

    _queue_a_second_outbound(tenant_id, conversation_id)
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b42-inflight",
    )
    assert dispatcher.dispatch_one() is not None

    after = _scheduled_rows(tenant_id, conversation_id)
    assert len(after) == 1
    assert after[0]["status"] == "CLAIMED"
    assert after[0]["due_at"] == before[0]["due_at"], (
        "a claimed timer was moved underneath the scheduler that holds its lease"
    )


def test_a_time_rule_fault_does_not_undo_a_delivery_that_already_happened(
    settings: Settings, seeded_database: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The re-review's P1-1**, asserted where the cost would have been paid.

    The producer runs in the dispatcher's accounting transaction, and the send
    has already happened by then. Before this branch existed, a
    `TimeRuleError` -- raised for a profile declaring two rules that render one
    dedupe key -- rolled back the receipt, the `DELIVERED` status and the intent
    together with the timer; the row returned to `SENDING`, and the next claim
    sent the same message to the customer again, up to `MAX_OUTBOX_ATTEMPTS`
    times.

    So the fault is recorded and the delivery stands. The profile is not the
    subject here -- `load_package` refuses that one at publish time -- the
    subject is that **nothing computed after the send may undo it**.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    def poisoned(**_: object) -> list[Any]:
        raise TimeRuleError("two rules render the same dedupe key")

    # **Patched between the turn and the delivery, not around both.** The
    # transition-side producer runs first and is *allowed* to raise -- it has no
    # external side effect, and the event goes to dead-letter. What must survive
    # a fault is the dispatcher's side, which runs after the send.
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    monkeypatch.setattr("fastagent_v2.worker.pipeline.drafts_for_anchor", poisoned)
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b42-poisoned",
    )
    assert dispatcher.dispatch_one() is not None

    with _migration_connection() as connection:
        outbox = connection.execute(
            "SELECT status FROM outbox_messages WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchall()
        receipts = connection.execute(
            "SELECT r.status FROM delivery_receipts r"
            "  JOIN outbox_messages o ON o.tenant_id = r.tenant_id AND o.id = r.outbox_id"
            " WHERE r.tenant_id = %s AND o.conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchall()
        audits = connection.execute(
            "SELECT reason_code FROM audit_events"
            " WHERE tenant_id = %s AND resource_id = %s AND action = 'scheduled_event.skipped'",
            (tenant_id, conversation_id),
        ).fetchall()

    assert [row["status"] for row in outbox] == ["DELIVERED"], (
        "the delivery was rolled back by a fault in something computed after it"
    )
    assert [row["status"] for row in receipts] == ["accepted"]
    assert [row["reason_code"] for row in audits] == ["TIME_RULE_REFUSED"], (
        "the dropped timer left no trace"
    )
    assert _scheduled_rows(tenant_id, conversation_id) == []
