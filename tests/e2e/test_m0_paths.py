from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter, SendCommand
from fastagent_v2.worker.inference import DeterministicMockInference, inference_for_provider
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.scheduler import Scheduler
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of


class FailingInference(DeterministicMockInference):
    def understand(self, **_: object) -> object:
        raise RuntimeError("injected inference failure")


class UnavailablePointerQueue:
    def publish(self, **_: object) -> str:
        raise RuntimeError("injected Redis outage")


def drive_customer_message(
    *,
    settings: Settings,
    seeded_database: dict[str, object],
    conversation_id: UUID,
    external_event_id: str,
) -> None:
    queue = PointerQueue(settings.queue_redis_url)
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=external_event_id,
            sender_id=sender_of(conversation_id),
        ),
    )
    assert accepted.accepted is True
    assert accepted.pointer_published is True
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="e2e-worker",
    )
    result = processor.process_one(block_ms=500)
    assert result is not None
    assert result.status == "consumed"
    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(settings.channel_send_url),
        dispatcher_id="e2e-sender",
    ).dispatch_one()
    assert dispatched is not None
    assert dispatched.receipt_status == "delivered"


def insert_scheduled_event(
    *, settings: Settings, tenant_id: UUID, conversation_id: UUID, due_at: datetime
) -> UUID:
    event_id = uuid4()
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        aggregate = connection.execute(
            """
            SELECT state, generation, profile_release_id
              FROM conversation_aggregates
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, conversation_id),
        ).fetchone()
        state = aggregate["state"]
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, payload_ref, created_by_transition_id
            ) VALUES (%s, %s, %s, %s, 'silence_check', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                event_id,
                conversation_id,
                due_at,
                f"silence:{event_id}",
                aggregate["profile_release_id"],
                aggregate["generation"],
                UUID(state["target_stack"][-1]["id"]),
                state["state_version"],
                Jsonb({"probe": "m0"}),
                UUID(state["last_transition"]["transition_id"]),
            ),
        )
    return event_id


def test_message_path_is_durable_before_pointer_and_reaches_delivery(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    drive_customer_message(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=conversation_id,
        external_event_id="customer-message-1",
    )
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        facts = {
            "inbox": connection.execute("SELECT count(*) AS n FROM inbox_events").fetchone()["n"],
            "aggregate": connection.execute(
                "SELECT count(*) AS n FROM conversation_aggregates"
            ).fetchone()["n"],
            "intent": connection.execute(
                "SELECT count(*) AS n FROM intent_records WHERE status = 'APPLIED'"
            ).fetchone()["n"],
            "outbox": connection.execute(
                "SELECT count(*) AS n FROM outbox_messages WHERE status = 'DELIVERED'"
            ).fetchone()["n"],
            "receipt": connection.execute(
                "SELECT count(*) AS n FROM delivery_receipts WHERE status = 'delivered'"
            ).fetchone()["n"],
        }
    assert facts == {"inbox": 1, "aggregate": 1, "intent": 1, "outbox": 1, "receipt": 1}


def test_timer_path_uses_scheduler_lease_and_the_same_inbox_pipeline(
    quiet_hours_out_of_the_way: None,
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    drive_customer_message(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=conversation_id,
        external_event_id="before-timer",
    )
    scheduled_id = insert_scheduled_event(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        due_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    queue = PointerQueue(settings.queue_redis_url)
    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        scheduler_id="e2e-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    enqueued = scheduler.enqueue_due()
    assert len(enqueued) == 1
    assert enqueued[0].scheduled_event_id == scheduled_id

    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="e2e-worker",
    )
    result = processor.process_one(block_ms=500)
    assert result is not None
    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(settings.channel_send_url),
        dispatcher_id="e2e-sender",
    ).dispatch_one()
    assert dispatched is not None
    assert dispatched.receipt_status == "delivered"
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        scheduled = connection.execute(
            "SELECT status, attempt, timer_envelope_id FROM scheduled_events WHERE id = %s",
            (scheduled_id,),
        ).fetchone()
        timer_inbox = connection.execute(
            "SELECT status FROM inbox_events WHERE event_type = 'timer'"
        ).fetchone()
    assert scheduled["status"] == "CONSUMED"
    assert scheduled["attempt"] == 1
    assert scheduled["timer_envelope_id"] is not None
    assert timer_inbox["status"] == "consumed"


def test_new_customer_message_invalidates_old_generation_timer(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    drive_customer_message(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=conversation_id,
        external_event_id="generation-1",
    )
    scheduled_id = insert_scheduled_event(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        due_at=datetime.now(UTC) + timedelta(hours=1),
    )
    drive_customer_message(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=conversation_id,
        external_event_id="generation-2",
    )
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        event = connection.execute(
            "SELECT status, invalidation_reason FROM scheduled_events WHERE id = %s",
            (scheduled_id,),
        ).fetchone()
    assert event["status"] == "INVALIDATED"
    assert event["invalidation_reason"] == "CUSTOMER_MESSAGE_NEW_GENERATION"


def test_durable_inbox_survives_pointer_publish_failure(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=UnavailablePointerQueue(),  # type: ignore[arg-type]
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    result = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id="redis-unavailable",
            sender_id="customer-1",
            text="persist first",
        ),
    )
    assert result.accepted is True
    assert result.pointer_published is False
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        inbox = connection.execute(
            "SELECT status FROM inbox_events WHERE id = %s", (result.inbox_id,)
        ).fetchone()
    assert inbox["status"] == "pending"


def test_inference_failure_does_not_double_apply_and_expired_lease_is_republished(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    queue = PointerQueue(settings.queue_redis_url)
    accepted = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id="inference-retry",
            sender_id=sender_of(conversation_id),
            text="retry me once",
        ),
    )
    failing = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=FailingInference(),  # type: ignore[arg-type]
        worker_id="failing-worker",
    )
    with pytest.raises(RuntimeError, match="injected inference failure"):
        failing.process_one(block_ms=500)
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        before_retry = connection.execute(
            "SELECT state_version, generation FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE inbox_events SET lease_expires_at = now() - interval '1 second'
             WHERE id = %s
            """,
            (accepted.inbox_id,),
        )
    assert before_retry == {"state_version": 0, "generation": 0}

    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        scheduler_id="recovery-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert scheduler.republish_pending() == 1
    recovered = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="recovery-worker",
    ).process_one(block_ms=500)
    assert recovered is not None
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        after_retry = connection.execute(
            "SELECT state_version, generation FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert after_retry == {"state_version": 1, "generation": 1}


def test_expired_outbox_lease_reuses_provider_idempotency_key(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    queue = PointerQueue(settings.queue_redis_url)
    WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id="outbox-crash-window",
            sender_id=sender_of(conversation_id),
            text="deliver exactly once",
        ),
    )
    persisted = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="outbox-worker",
    ).process_one(block_ms=500)
    assert persisted is not None

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        outbox = connection.execute(
            "SELECT id, intent_id, command, idempotency_key FROM outbox_messages"
        ).fetchone()
    command = outbox["command"]
    adapter = HttpChannelAdapter(settings.channel_send_url)
    provider_receipt = adapter.send(
        command=SendCommand(
            tenant_id=str(tenant_id),
            conversation_id=str(conversation_id),
            content=command["content"],
        ),
        idempotency_key=outbox["idempotency_key"],
        deadline_at=datetime.fromisoformat(command["deadline_at"].replace("Z", "+00:00")),
    )
    assert provider_receipt.status == "delivered"
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            """
            UPDATE outbox_messages
               SET status = 'SENDING', attempt = 1,
                   lease_owner = 'crashed-sender',
                   lease_expires_at = now() - interval '1 second'
             WHERE id = %s
            """,
            (outbox["id"],),
        )
        connection.execute(
            """
            UPDATE intent_records
               SET status = 'EXECUTING', lease_expires_at = now() - interval '1 second'
             WHERE id = %s
            """,
            (outbox["intent_id"],),
        )
    recovered = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=adapter,
        dispatcher_id="recovery-sender",
    ).dispatch_one()
    assert recovered is not None
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        receipt = connection.execute(
            "SELECT provider_operation_id FROM delivery_receipts WHERE outbox_id = %s",
            (outbox["id"],),
        ).fetchone()
        attempt = connection.execute(
            "SELECT attempt FROM outbox_messages WHERE id = %s", (outbox["id"],)
        ).fetchone()["attempt"]
    assert receipt["provider_operation_id"] == provider_receipt.provider_operation_id
    assert attempt == 2
