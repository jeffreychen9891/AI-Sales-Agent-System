"""Retries must end somewhere.

A lease decides *when* the next identical attempt happens, not how many there may
be. Until 2026-09-02 neither the inbox nor the outbox had a budget, so one event
the worker could not process was republished every ten seconds indefinitely --
and with a model provider configured, every pass paid for three model calls.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import InboxPointer, PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker import pipeline
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.cli import run_worker
from fastagent_v2.worker.inference import DeterministicMockInference, inference_for_provider
from fastagent_v2.worker.pipeline import (
    MAX_INBOX_ATTEMPTS,
    MAX_OUTBOX_ATTEMPTS,
    AggregateConflict,
    InboxProcessor,
    OutboxDispatcher,
)
from fastagent_v2.worker.scheduler import Scheduler
from jsonschema import ValidationError
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


class PoisonInference(DeterministicMockInference):
    """Fails in a way the pipeline has no typed answer for.

    `ProviderError` and `CostCeilingExceeded` already route to the no-send path.
    This stands in for the rest: a contract bug, a psycopg error, a plan whose
    deterministic fallback is itself invalid.
    """

    def understand(self, **_: Any) -> dict[str, Any]:
        raise RuntimeError("injected unhandled failure")


def _accept(
    *, settings: Settings, seeded_database: dict[str, object], conversation_id: UUID
) -> Any:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"retry-budget-{uuid4()}",
            sender_id=sender_of(conversation_id),
        ),
    )
    assert accepted.accepted is True
    return accepted


def _inbox_row(*, settings: Settings, tenant_id: UUID, inbox_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT status, attempt, error_code, lease_owner FROM inbox_events WHERE id = %s",
            (inbox_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _migration_connection() -> Connection[Any]:
    return Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    )


def _deliver_to_outbox(
    *, settings: Settings, seeded_database: dict[str, object], conversation_id: UUID
) -> dict[str, Any]:
    """Run one customer message far enough to leave a committed outbox row."""

    _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference_for_provider(settings.model_provider),
        worker_id="retry-budget-worker",
    )
    result = processor.process_one(block_ms=500)
    assert result is not None
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT id, intent_id, status, attempt FROM outbox_messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_a_poisoned_inbox_event_stops_being_retried_once_the_budget_runs_out(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inbox_id = UUID(str(accepted.inbox_id))
    queue = PointerQueue(settings.queue_redis_url)
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=PoisonInference(),  # type: ignore[arg-type]
        worker_id="poison-worker",
    )

    for attempt in range(1, MAX_INBOX_ATTEMPTS + 1):
        if attempt > 1:
            queue.publish(inbox_id=inbox_id, tenant_id=tenant_id)
        with pytest.raises(RuntimeError, match="injected unhandled failure"):
            processor.process_one(block_ms=500)
        row = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
        assert row["attempt"] == attempt
        assert row["error_code"] == "RuntimeError"
        assert row["lease_owner"] is None
        assert row["status"] == ("failed" if attempt == MAX_INBOX_ATTEMPTS else "pending")

    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        scheduler_id="poison-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert scheduler.republish_pending() == 0

    # Even a stray pointer for a retired event must not restart the loop.
    queue.publish(inbox_id=inbox_id, tenant_id=tenant_id)
    assert processor.process_one(block_ms=500) is None
    assert _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)["attempt"] == (
        MAX_INBOX_ATTEMPTS
    )


def test_the_error_code_records_the_failure_type_and_not_its_message(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """`error_code` is readable by anyone with inbox access; messages carry material."""

    class LeakyInference(DeterministicMockInference):
        def understand(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("customer said 13800000000 and the key is sk-abcdefghij")

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=LeakyInference(),  # type: ignore[arg-type]
        worker_id="leaky-worker",
    )
    with pytest.raises(RuntimeError):
        processor.process_one(block_ms=500)
    row = _inbox_row(
        settings=settings, tenant_id=tenant_id, inbox_id=UUID(str(accepted.inbox_id))
    )
    assert row["error_code"] == "RuntimeError"
    assert "13800000000" not in str(row["error_code"])
    assert "sk-" not in str(row["error_code"])


def test_a_lost_aggregate_race_returns_the_event_without_spending_an_attempt(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """Two workers meeting on one conversation is normal, not a defect of the event."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inbox_id = UUID(str(accepted.inbox_id))

    class RacingInference(DeterministicMockInference):
        """Commits a competing aggregate transition while this event is in flight."""

        def generate(self, **kwargs: Any) -> dict[str, Any]:
            database = Database(settings.worker_database_url)
            with database.transaction(tenant_id=tenant_id) as connection:
                connection.execute(
                    """
                    UPDATE conversation_aggregates SET state_version = state_version + 1
                     WHERE tenant_id = %s AND id = %s
                    """,
                    (tenant_id, conversation_id),
                )
            return super().generate(**kwargs)

    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=RacingInference(),  # type: ignore[arg-type]
        worker_id="racing-worker",
    )
    with pytest.raises(AggregateConflict):
        processor.process_one(block_ms=500)
    row = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert row["attempt"] == 0
    assert row["status"] == "pending"
    assert row["lease_owner"] is None


def test_a_failure_inside_prepare_is_counted_and_eventually_retired(
    settings: Settings,
    seeded_database: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A6: the failure that never spent a pass.

    `prepare` claims the row and does its work in **one** transaction, so an
    exception anywhere inside it rolls the claim back too. The bookkeeping write
    was guarded on `status = 'processing' AND lease_owner = me`, which after that
    rollback matches nothing: `attempt` stayed 0, `error_code` stayed NULL, the
    row stayed `pending`, and the scheduler republished it every ten seconds.

    **This is not hypothetical.** 2026-09-22, a contract mismatch on one event:
    ops stopped it by hand at 70 retries and lost the customer's message doing it
    (`docs/handoffs/ops-to-author-two-bounds-on-one-goal-2026-09-22.md`).
    Registered as A6 on 2026-09-02, open until today.

    **Why the failure is injected at `validate_contract` rather than grown from a
    real package**: what this test is about is the *state a rolled-back claim
    leaves behind*, and that state is real either way. Reaching it from a genuine
    package needs a goal longer than the aggregate contract allows but shorter
    than the profile contract allows -- an interval that exists only while those
    two bounds disagree, which is itself a defect being fixed on another branch.
    Pinning this test to that interval would tie it to a bug's lifetime.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inbox_id = UUID(str(accepted.inbox_id))
    queue = PointerQueue(settings.queue_redis_url)

    honest = pipeline.validate_contract

    def refusing(name: str, document: Any) -> Any:
        if name == "conversation-aggregate":
            raise ValidationError("injected: this frame does not fit the contract")
        return honest(name, document)

    monkeypatch.setattr(pipeline, "validate_contract", refusing)

    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="prepare-failure-worker",
    )

    for attempt in range(1, MAX_INBOX_ATTEMPTS + 1):
        if attempt > 1:
            queue.publish(inbox_id=inbox_id, tenant_id=tenant_id)
        with pytest.raises(ValidationError):
            processor.process_one(block_ms=500)
        row = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
        # Before A6 this read 0 on every pass, for ever.
        assert row["attempt"] == attempt
        assert row["error_code"] == "ValidationError"
        assert row["lease_owner"] is None
        assert row["status"] == ("failed" if attempt == MAX_INBOX_ATTEMPTS else "pending")

    # The whole point: it comes off the rail instead of being republished for ever.
    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        scheduler_id="prepare-failure-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert scheduler.republish_pending() == 0


def test_the_unowned_write_does_not_fire_when_the_lease_was_merely_lost(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """A6's other side: counting the rolled-back claim must not count a stolen row.

    `test_a_worker_cannot_write_bookkeeping_onto_a_lease_it_no_longer_holds`
    already drives the steal. This one states the distinction A6 turns on, so
    that widening the guard later has something to break:

    * **rolled-back claim** -> `pending` and unowned -> counted;
    * **stolen row** -> `processing` and owned by someone else -> not counted.

    A guard written as "not owned by me" instead of "owned by nobody" satisfies
    the first and breaks the second, and the two are one word apart.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inbox_id = UUID(str(accepted.inbox_id))
    pointer = InboxPointer(stream_id="0-0", inbox_id=inbox_id, tenant_id=tenant_id)

    def processor(worker_id: str) -> InboxProcessor:
        return InboxProcessor(
            database=Database(settings.worker_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            inference=inference_for_provider(settings.model_provider),
            worker_id=worker_id,
        )

    # 1. Nobody holds it -- the row is exactly what a rolled-back claim leaves.
    before = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert before["status"] == "pending"
    assert before["lease_owner"] is None

    processor("late-worker")._record_failed_attempt(pointer, RuntimeError("prepare blew up"))
    counted = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert counted["attempt"] == before["attempt"] + 1
    assert counted["error_code"] == "RuntimeError"

    # 2. Someone else holds it -- neither statement may touch the row.
    holder = processor("holder-worker")
    assert holder.prepare(tenant_id=tenant_id, inbox_id=inbox_id) is not None
    held = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert held["lease_owner"] == "holder-worker"

    processor("late-worker")._record_failed_attempt(pointer, RuntimeError("too late"))
    after = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert after == held


def test_a_channel_server_error_records_an_unconfirmed_delivery_instead_of_crashing(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def always_500(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "upstream exploded"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(always_500)
        ),
        dispatcher_id="five-hundred-sender",
    ).dispatch_one()
    assert dispatched is not None
    assert dispatched.receipt_status == "unknown"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        message = connection.execute(
            "SELECT status FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()
        intent = connection.execute(
            "SELECT status, unknown_reason FROM intent_records WHERE id = %s",
            (outbox["intent_id"],),
        ).fetchone()
    assert message is not None and message["status"] == "UNKNOWN"
    assert intent is not None
    assert intent["status"] == "UNKNOWN"
    assert intent["unknown_reason"] == "DELIVERY_UNCONFIRMED"


def test_a_lost_response_is_resolved_by_asking_rather_than_by_guessing(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """L1, end to end: the send fails, the query succeeds, the intent applies.

    This is the case the missing wiring turned into a false `UNKNOWN` for a whole
    milestone. The provider took the message and the response was lost -- a 5xx
    on the way back, a dropped connection, a timeout. Nothing about that means
    the message was not delivered, and RFC `:228` says so: **ask first**.

    The transport answers 500 to the send and serves the receipt to the by-key
    lookup, which is exactly the shape of "the message landed, the answer did
    not". Before this batch the dispatcher wrote UNKNOWN here and stopped.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def lost_response_but_delivered(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"detail": "the answer never came back"})
        assert "/send/by-key/" in str(request.url), "the timeout path must ask by key"
        return httpx.Response(
            200, json={"provider_operation_id": "op-recovered", "status": "delivered"}
        )

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url,
            transport=httpx.MockTransport(lost_response_but_delivered),
        ),
        dispatcher_id="reconciling-sender",
    ).dispatch_one()

    assert dispatched is not None
    assert dispatched.receipt_status == "delivered"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        message = connection.execute(
            "SELECT status FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()
        intent = connection.execute(
            "SELECT status, external_operation_ref FROM intent_records WHERE id = %s",
            (outbox["intent_id"],),
        ).fetchone()
        receipt = connection.execute(
            "SELECT status, evidence FROM delivery_receipts WHERE intent_id = %s",
            (outbox["intent_id"],),
        ).fetchone()

    assert message is not None and message["status"] == "DELIVERED"
    assert intent is not None and intent["status"] == "APPLIED"
    assert intent["external_operation_ref"] == "op-recovered"
    assert receipt is not None
    # The receipt keeps both halves under their own names: what the send ran
    # into, and what the query came back with. Flat merging lost one of them
    # (P2-1) -- here the send's 500 must still be readable as the *send's*.
    assert receipt["evidence"]["reconcile_attempted"] is True
    assert receipt["evidence"]["send"]["http_status"] == 500
    assert receipt["evidence"]["reconcile"]["query_sent"] is True


def test_an_unknown_that_survives_the_question_says_that_it_was_asked(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The other outcome, and the one that must stop being ambiguous.

    The provider has no record of the key, so the delivery really is unknown --
    same status as before this batch, different evidence. `reconcile_attempted`
    is what tells the next reader (and the manual-task half, L2b) that the
    question was put and came back empty, rather than never having been asked.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def nothing_landed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"detail": "upstream exploded"})
        return httpx.Response(404, json={"detail": "not found"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(nothing_landed)
        ),
        dispatcher_id="asked-and-told-nothing",
    ).dispatch_one()

    assert dispatched is not None and dispatched.receipt_status == "unknown"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        intent = connection.execute(
            "SELECT status, unknown_reason FROM intent_records WHERE id = %s",
            (outbox["intent_id"],),
        ).fetchone()
        receipt = connection.execute(
            "SELECT evidence FROM delivery_receipts WHERE intent_id = %s",
            (outbox["intent_id"],),
        ).fetchone()

    assert intent is not None and intent["status"] == "UNKNOWN"
    assert intent["unknown_reason"] == "DELIVERY_UNCONFIRMED"
    assert receipt is not None and receipt["evidence"]["reconcile_attempted"] is True
    assert "reconcile_skipped_reason" not in receipt["evidence"]


def test_a_definite_receipt_is_not_second_guessed_by_a_query(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The gate in `dispatch_one`, asserted through `dispatch_one`.

    Its contract test used to build a channel, call nothing, and check that
    nothing had been called -- vacuously true. The review's mutation made the
    point: changing the gate to `if True:` left the whole non-database suite
    green (P2-3). A guard nobody can break is not a guard.

    So the assertion lives here, where the gate actually is. A send that
    succeeds must produce exactly one HTTP call: the POST. A second round trip
    per message would cost the provider double, and worse, would give it a
    chance to contradict a receipt already written down -- RFC 7.2 step 6 has
    `delivered -> APPLIED` going straight through, with no room for a second
    opinion.
    """

    conversation_id = new_conversation(seeded_database["tenant_a"])
    _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    methods: list[str] = []

    def accepted(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"provider_operation_id": "op-sure", "status": "accepted"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(accepted)
        ),
        dispatcher_id="no-second-guessing",
    ).dispatch_one()

    assert dispatched is not None and dispatched.receipt_status == "accepted"
    assert methods == ["POST"], "a definite receipt must not be followed by a query"


def test_a_timeout_still_reaches_the_query_after_it_has_eaten_the_budget(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """P1-1, end to end: the case RFC `:228` names, which the wiring could not serve.

    The send's HTTP timeout is the whole remaining turn budget, so a genuine
    timeout spends all of it and the query that followed found nothing left --
    the review's socket probe saw only the POST reach the server. The query now
    runs on its own clock, so the shape below (send times out, provider actually
    has the message) resolves instead of being written off as unknown.

    **The budget is really spent here.** The first version of this test raised
    `ReadTimeout` immediately, so the wall clock barely moved and the send
    deadline was still alive when the query went out -- which means it passed
    just as well with the bug in place: the narrow re-review put the old
    `deadline_at` back and this test stayed green (2026-09-04). It was asserting
    that a timeout takes the by-key door, not that the query has its own clock.
    Now the row's deadline is moved to a third of a second out and the transport
    sleeps past it before failing, so at query time the send's budget is
    genuinely gone and the GET can only happen on the reconcile's own clock.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    spent_at = datetime.now(UTC) + timedelta(seconds=0.3)
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            """
            UPDATE outbox_messages
               SET command = jsonb_set(command, '{deadline_at}', to_jsonb(%s::text))
             WHERE tenant_id = %s AND id = %s
            """,
            (spent_at.isoformat().replace("+00:00", "Z"), tenant_id, outbox["id"]),
        )
    methods: list[str] = []

    def timed_out_but_landed(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            # Sleeps past the send deadline first: the point under test is that
            # the query still gets a live clock after the send's is gone.
            time.sleep(0.35)
            raise httpx.ReadTimeout("the turn budget is gone", request=request)
        return httpx.Response(
            200, json={"provider_operation_id": "op-late", "status": "delivered"}
        )

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(timed_out_but_landed)
        ),
        dispatcher_id="timed-out-sender",
    ).dispatch_one()

    assert datetime.now(UTC) > spent_at, "the send budget was not actually spent"
    assert methods == ["POST", "GET"], "the timeout path must still get to ask"
    assert dispatched is not None and dispatched.receipt_status == "delivered"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        receipt = connection.execute(
            "SELECT evidence FROM delivery_receipts WHERE intent_id = %s",
            (outbox["intent_id"],),
        ).fetchone()
    assert receipt is not None
    assert receipt["evidence"]["reconcile_attempted"] is True


def test_an_outbox_message_is_retired_once_its_delivery_attempts_run_out(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE outbox_messages
               SET status = 'SENDING', attempt = %s, lease_owner = 'crashed-sender',
                   lease_expires_at = %s
             WHERE id = %s
            """,
            (MAX_OUTBOX_ATTEMPTS, datetime.now(UTC) - timedelta(seconds=1), outbox["id"]),
        )
        connection.execute(
            "UPDATE intent_records SET status = 'EXECUTING' WHERE id = %s",
            (outbox["intent_id"],),
        )
        connection.commit()

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(settings.channel_send_url),
        dispatcher_id="retiring-sender",
    ).dispatch_one()
    assert dispatched is None

    with _migration_connection() as connection:
        message = connection.execute(
            "SELECT status, lease_owner FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()
        intent = connection.execute(
            "SELECT status, unknown_reason FROM intent_records WHERE id = %s",
            (outbox["intent_id"],),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE resource_id = %s AND reason_code = 'DELIVERY_ATTEMPTS_EXHAUSTED'
            """,
            (outbox["intent_id"],),
        ).fetchall()
    assert message is not None
    assert message["status"] == "FAILED"
    assert message["lease_owner"] is None
    assert intent is not None
    assert intent["status"] == "FAILED"
    assert intent["unknown_reason"] == "DELIVERY_ATTEMPTS_EXHAUSTED"
    assert len(audit) == 1
    assert audit[0]["evidence"]["outbox_id"] == str(outbox["id"])

    # **The second producer of manual tasks, which had no assertion at all**
    # (independent review 2026-09-04, P2-5). `DELIVERY_ATTEMPTS_EXHAUSTED` is
    # classified `merchant_action / batched` in `_POLICY` and has a digest
    # sentence, so the whole path was in place and nothing checked that this
    # branch actually walked it -- the classification could have been deleted
    # and every test would have stayed green.
    with _migration_connection() as connection:
        task = connection.execute(
            """
            SELECT m.task_class, m.notify, m.digested_at, i.proposal
              FROM intent_records i
              JOIN manual_tasks m ON m.tenant_id = i.tenant_id AND m.intent_id = i.id
             WHERE i.kind = 'manual_task' AND i.proposal->>'outbox_id' = %s
            """,
            (str(outbox["id"]),),
        ).fetchone()
    assert task is not None, "a retired delivery must leave somebody a task"
    assert task["task_class"] == "merchant_action"
    assert task["notify"] == "batched"
    assert task["digested_at"] is None, "not yet reported -- the digest is what reports it"


def test_a_failing_dispatch_does_not_take_the_worker_loop_down(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop must survive a failure the adapter cannot turn into a receipt.

    `dispatch_one` was the one call in `run_worker` with nothing around it, so a
    database or contract error there ended the process rather than the delivery.
    """

    def explode(_: OutboxDispatcher) -> None:
        raise RuntimeError("injected dispatch failure")

    monkeypatch.setattr(OutboxDispatcher, "dispatch_one", explode)
    run_worker(settings, once=True)


@pytest.mark.parametrize(
    ("status_code", "outbox_status", "intent_status", "unknown_reason"),
    [
        (422, "FAILED", "FAILED", None),
        (429, "UNKNOWN", "UNKNOWN", "DELIVERY_UNCONFIRMED"),
    ],
)
def test_a_client_error_reaches_the_outbox_as_the_status_it_actually_proves(
    settings: Settings,
    seeded_database: dict[str, object],
    status_code: int,
    outbox_status: str,
    intent_status: str,
    unknown_reason: str | None,
) -> None:
    """422 is an enumerated refusal; 429 is not.

    Both are 4xx. Resolving them to the same terminal state -- as the first
    version of this adapter did -- would discard a message the provider may well
    have accepted before rate-limiting the connection.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "refused"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(respond)
        ),
        dispatcher_id=f"client-error-{status_code}-sender",
    ).dispatch_one()
    assert dispatched is not None

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        message = connection.execute(
            "SELECT status FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()
        intent = connection.execute(
            "SELECT status, unknown_reason FROM intent_records WHERE id = %s",
            (outbox["intent_id"],),
        ).fetchone()
    assert message is not None and message["status"] == outbox_status
    assert intent is not None
    assert intent["status"] == intent_status
    assert intent["unknown_reason"] == unknown_reason


def test_a_worker_cannot_write_bookkeeping_onto_a_lease_it_no_longer_holds(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The cross-worker steal.

    A slow worker whose lease expired, returning to record its failure after a
    second worker has claimed the same row, must not hand that row back or
    advance its attempt counter. Doing so would release a lease it does not own
    and let a third worker run the event concurrently with the second.

    This drives the two bookkeeping writes directly: reaching this interleaving
    through `process_one` would require suspending one worker mid-inference.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    accepted = _accept(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    inbox_id = UUID(str(accepted.inbox_id))

    def processor(worker_id: str) -> InboxProcessor:
        return InboxProcessor(
            database=Database(settings.worker_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            inference=inference_for_provider(settings.model_provider),
            worker_id=worker_id,
        )

    slow, fast = processor("slow-worker"), processor("fast-worker")
    assert slow.prepare(tenant_id=tenant_id, inbox_id=inbox_id) is not None

    # The slow worker stalls past its lease; the fast worker takes the row.
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE inbox_events SET lease_expires_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(seconds=1), inbox_id),
        )
        connection.commit()
    assert fast.prepare(tenant_id=tenant_id, inbox_id=inbox_id) is not None
    before = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert before["lease_owner"] == "fast-worker"

    pointer = InboxPointer(stream_id="0-0", inbox_id=inbox_id, tenant_id=tenant_id)
    slow._record_failed_attempt(pointer, RuntimeError("late failure"))
    slow._release_lease(pointer)

    after = _inbox_row(settings=settings, tenant_id=tenant_id, inbox_id=inbox_id)
    assert after == before
    assert after["lease_owner"] == "fast-worker"
    assert after["attempt"] == before["attempt"]
    assert after["status"] == "processing"


def test_a_dispatcher_that_lost_its_lease_writes_nothing_down(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The row stops being ours the moment the lease does, writes included.

    The narrow re-review of L1 (2026-09-04) found the closing writes guarded on
    `status = 'SENDING'` alone. A dispatcher that came back after its lease had
    expired -- which the reconcile's two extra seconds made likely, since the
    send has usually eaten the other twenty-nine -- still overwrote the status of
    whoever had claimed the row since, and still inserted its own receipt, where
    it hit the `(tenant_id, outbox_id)` unique index and raised into the worker
    loop's `except Exception`. The probe watched the second dispatcher re-POST
    the message: a duplicate delivery, recorded nowhere.

    Here the steal happens inside the send, which is the real window. Afterwards
    this dispatcher must have written no receipt, changed no status, and left one
    audit event saying a send went out on a row it no longer owned -- otherwise
    the duplicate is invisible in every table we keep.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    outbox = _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def steal_the_row_mid_send(request: httpx.Request) -> httpx.Response:
        with Database(settings.worker_database_url).transaction(
            tenant_id=tenant_id
        ) as connection:
            connection.execute(
                """
                UPDATE outbox_messages
                   SET lease_owner = 'the-other-dispatcher',
                       lease_expires_at = now() + interval '30 seconds', updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, outbox["id"]),
            )
        return httpx.Response(200, json={"provider_operation_id": "op-dup", "status": "accepted"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(steal_the_row_mid_send)
        ),
        dispatcher_id="lost-the-lease",
    ).dispatch_one()

    assert dispatched is not None
    assert dispatched.receipt_status == "accepted", "the send itself did happen"
    assert dispatched.lease_lost is True

    # Read through the migration role: runtime roles hold INSERT and nothing
    # else on `audit_events`, and that append-only grant is not relaxed for a
    # test (same reasoning as `_audit_rows` in `test_m1a_no_send.py`).
    with _migration_connection() as connection:
        receipts = connection.execute(
            "SELECT id FROM delivery_receipts WHERE outbox_id = %s", (outbox["id"],)
        ).fetchall()
        row = connection.execute(
            "SELECT status, lease_owner FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()
        intent = connection.execute(
            "SELECT status FROM intent_records WHERE id = %s", (outbox["intent_id"],)
        ).fetchone()
        events = connection.execute(
            '''
            SELECT reason_code, evidence FROM audit_events
             WHERE resource_id = %s AND reason_code = %s
            ''',
            (outbox["intent_id"], "DELIVERY_LEASE_LOST"),
        ).fetchall()

    assert receipts == [], "a dispatcher without the lease must not write a receipt"
    assert row is not None
    assert row["status"] == "SENDING" and row["lease_owner"] == "the-other-dispatcher", (
        "the new owner's row was overwritten by the dispatcher that lost it"
    )
    assert intent is not None and intent["status"] == "EXECUTING", (
        "the intent belongs to whoever holds the lease now"
    )
    assert len(events) == 1, "a send on a row we no longer own has to leave a trace"
    assert events[0]["evidence"]["dispatcher_id"] == "lost-the-lease"
    assert events[0]["evidence"]["receipt_status"] == "accepted"
