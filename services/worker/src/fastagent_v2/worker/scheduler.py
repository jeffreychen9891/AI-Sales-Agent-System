from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from psycopg.types.json import Jsonb

#: How long a due scheduled event stays claimed. Same shape and same reason as
#: `INBOX_LEASE_SECONDS` in `pipeline.py`: the number lives next to the clock it
#: is measured on, and that clock is the database's.
#:
#: This was the repo's last lease written from a process clock (independent
#: review P2-2). It was not a defect -- `enqueue_due` wrote `clock + 30s` and
#: compared against the same `clock`, so within one process the offset cancelled
#: -- but T183 made "leases belong to the database's clock" a rule, and a rule
#: with one exception left in it is what the next reader will find and copy. A
#: second scheduler replica would have made the subtraction span two process
#: clocks with no common reference, which is harder to diagnose than the outbox
#: case that started all of this, because nothing here would look wrong.
SCHEDULED_LEASE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ScheduledEnqueueResult:
    tenant_id: UUID
    scheduled_event_id: UUID
    inbox_id: UUID
    event_id: UUID


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class Scheduler:
    def __init__(
        self,
        *,
        database: Database,
        pointer_queue: PointerQueue,
        scheduler_id: str,
        runtime_deadline_seconds: int,
    ) -> None:
        self._database = database
        self._queue = pointer_queue
        self._scheduler_id = scheduler_id
        self._runtime_deadline_seconds = runtime_deadline_seconds

    def _tenant_ids(self) -> list[UUID]:
        with self._database.transaction(tenant_id=None) as connection:
            rows = connection.execute("SELECT id FROM list_active_tenant_ids()").fetchall()
        return [UUID(str(row["id"])) for row in rows]

    def enqueue_due(self, *, now: datetime | None = None) -> list[ScheduledEnqueueResult]:
        clock = now or datetime.now(UTC)
        results: list[ScheduledEnqueueResult] = []
        for tenant_id in self._tenant_ids():
            with self._database.transaction(tenant_id=tenant_id) as connection:
                # `due_at` is compared against the caller's `clock` on
                # purpose -- that is the injected one, and it is how the tests
                # decide what is due -- while the *lease* is compared against
                # the database's `now()`. The two are different questions:
                # "should this have fired by now" is about the schedule, and the
                # caller owns it; "does someone else still hold this row" is
                # about a value another process wrote, and only the database's
                # clock can answer it without an offset in the answer.
                claimed = connection.execute(
                    """
                    SELECT * FROM scheduled_events
                     WHERE tenant_id = %s AND due_at <= %s
                       AND (status = 'PENDING' OR
                            (status = 'CLAIMED' AND lease_expires_at < now()))
                     ORDER BY due_at, id
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                    """,
                    (tenant_id, clock),
                ).fetchone()
                if claimed is None:
                    continue
                changed = connection.execute(
                    """
                    UPDATE scheduled_events
                       SET status = 'CLAIMED', lease_owner = %s,
                           lease_expires_at = now() + %s * interval '1 second',
                           attempt = attempt + 1
                     WHERE tenant_id = %s AND id = %s
                       AND (status = 'PENDING' OR
                            (status = 'CLAIMED' AND lease_expires_at < now()))
                    """,
                    (
                        self._scheduler_id,
                        SCHEDULED_LEASE_SECONDS,
                        tenant_id,
                        claimed["id"],
                    ),
                )
                if changed.rowcount != 1:
                    continue

                event_id = uuid4()
                inbox_id = uuid4()
                correlation_id = uuid4()
                envelope: dict[str, object] = {
                    "schema_version": "1.0.0",
                    "event_id": str(event_id),
                    "external_event_id": f"timer:{claimed['id']}",
                    "event_type": "timer",
                    "actor": {"party": "system", "service_id": "scheduler"},
                    "tenant_id": str(tenant_id),
                    "conversation_id": str(claimed["conversation_id"]),
                    "occurred_at": _iso(clock),
                    "received_at": _iso(clock),
                    "correlation_id": str(correlation_id),
                    "causation_id": str(claimed["created_by_transition_id"]),
                    "runtime_deadline_at": _iso(
                        clock + timedelta(seconds=self._runtime_deadline_seconds)
                    ),
                    "profile_release_id": str(claimed["profile_release_id"]),
                    "aggregate_generation": int(claimed["aggregate_generation"]),
                    "content": {
                        "type": "event",
                        "event": {
                            "kind": str(claimed["kind"]),
                            "payload_ref": claimed["payload_ref"],
                        },
                    },
                    "auth_evidence": {
                        "verifier_id": "scheduled-event-lease-cas",
                        "verifier_version": "1.0.0",
                        "credential_fingerprint": self._scheduler_id,
                        "nonce": None,
                        "timestamp": _iso(clock),
                    },
                    "scheduled_event_id": str(claimed["id"]),
                    "business_event": None,
                    "raw_payload_ref": None,
                }
                validate_contract("message-envelope", envelope)
                connection.execute(
                    """
                    INSERT INTO inbox_events(
                        tenant_id, id, event_id, source, external_event_id, conversation_id,
                        event_type, profile_release_id, aggregate_generation, envelope
                    ) VALUES (%s, %s, %s, 'scheduler', %s, %s, 'timer', %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        inbox_id,
                        event_id,
                        f"timer:{claimed['id']}",
                        claimed["conversation_id"],
                        claimed["profile_release_id"],
                        claimed["aggregate_generation"],
                        Jsonb(envelope),
                    ),
                )
                enqueued = connection.execute(
                    """
                    UPDATE scheduled_events
                       SET status = 'ENQUEUED', timer_envelope_id = %s,
                           lease_owner = NULL, lease_expires_at = NULL
                     WHERE tenant_id = %s AND id = %s
                       AND status = 'CLAIMED' AND lease_owner = %s
                    """,
                    (event_id, tenant_id, claimed["id"], self._scheduler_id),
                )
                if enqueued.rowcount != 1:
                    raise RuntimeError("scheduled event enqueue CAS failed")
                results.append(
                    ScheduledEnqueueResult(
                        tenant_id=tenant_id,
                        scheduled_event_id=UUID(str(claimed["id"])),
                        inbox_id=inbox_id,
                        event_id=event_id,
                    )
                )

        for result in results:
            self._queue.publish(inbox_id=result.inbox_id, tenant_id=result.tenant_id)
        return results

    def republish_pending(self, *, limit_per_tenant: int = 100) -> int:
        count = 0
        for tenant_id in self._tenant_ids():
            with self._database.transaction(tenant_id=tenant_id) as connection:
                rows = connection.execute(
                    """
                    SELECT id FROM inbox_events
                     WHERE tenant_id = %s
                       AND (status = 'pending' OR
                            (status = 'processing' AND lease_expires_at < now()))
                     ORDER BY created_at LIMIT %s
                    """,
                    (tenant_id, limit_per_tenant),
                ).fetchall()
            for row in rows:
                self._queue.publish(inbox_id=UUID(str(row["id"])), tenant_id=tenant_id)
                count += 1
        return count
