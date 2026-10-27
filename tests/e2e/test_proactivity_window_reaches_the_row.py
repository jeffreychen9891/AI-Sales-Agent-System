"""ProactivityPolicy is reached, and its second check refuses a real delivery.

**B4-4a / RFC §6.4 / ADR-0098 §一.** `tests/contracts/test_proactivity_classification.py`
proves the arithmetic. **That is the half that can pass while nothing is
reached** -- the shape this repository keeps paying for -- so this file drives a
real customer message through the pipeline, a real delivery through the
dispatcher, a real timer through the scheduler, and then looks at the row.

The timer is the point: everything ProactivityPolicy does happens on a
timer-triggered turn, and before this batch nothing in the repository produced
one from a rule.
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
from fastagent_v2.worker.proactivity import IN_SESSION_CONTINUATION
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e]

#: `profiles/synthetic-third` is what the suite seeds: `active_window_seconds`
#: is 1800 and its `welcome` frame's one rule fires 75 seconds after an outbound.
#: Quoted rather than derived -- a test that read them from the profile would
#: agree with the producer about a value both took from one place.
ACTIVE_WINDOW_SECONDS = 1800


class AcceptingAdapter:
    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")


class RefusingAdapter(AcceptingAdapter):
    """A channel that must never be asked.

    The `pre_delivery` refusal is about a message **not being sent**, so the
    assertion that matters is not the row's status -- it is that nothing reached
    a provider. A status check alone would still pass if the send happened and
    the row was marked afterwards.
    """

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        raise AssertionError("a cancelled proactive message must never reach the channel")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _processor(settings: Settings, worker_id: str) -> InboxProcessor:
    return InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id=worker_id,
    )


def _dispatcher(settings: Settings, adapter: Any, name: str) -> OutboxDispatcher:
    return OutboxDispatcher(
        database=Database(settings.worker_database_url), channel=adapter, dispatcher_id=name
    )


def _deliver_one(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    assert _dispatcher(settings, AcceptingAdapter(), "b44-seed").dispatch_one() is not None


def _fire_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    """Take the pending timer through the scheduler and consume it.

    `enqueue_due` is given a clock past `due_at` rather than the row being
    back-dated: the injected clock is the mechanism the scheduler already
    exposes for exactly this, and moving `due_at` would be editing the thing
    under test.
    """

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the delivery left no pending timer to fire"
    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="b44-scheduler",
        runtime_deadline_seconds=600,
    )
    enqueued = scheduler.enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    assert any(result.tenant_id == tenant_id for result in enqueued)
    assert _processor(settings, "b44-timer-worker").process_one(block_ms=1000) is not None


def _proactive_row(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT o.id, o.status, o.command, i.strategy_id"
            "  FROM outbox_messages o"
            "  JOIN intent_records i ON i.tenant_id = o.tenant_id AND i.id = o.intent_id"
            " WHERE o.tenant_id = %s AND o.conversation_id = %s"
            "   AND o.command ? 'proactivity'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the timer turn produced no proactive outbox row"
    return dict(row)


def _publish_a_release_whose_quiet_window_is_far_away(
    tenant_id: UUID, seeded: dict[str, Any]
) -> None:
    """Move the quiet window out of the way, so this case is about the other source.

    **Added because B4-5 made this case depend on the time of day** and nobody
    noticed until a gate happened to run at 22:10 Shanghai. The shipped profiles
    declare 22:30-08:00; once `window_expires_at` became
    `min(active window, next quiet window)`, a run starting within 30 minutes of
    22:30 gets the **quiet** instant here and the assertion below -- which is
    about the *active* window, anchored on the customer's clock -- fails on a
    correct implementation.

    So the window is pushed six hours out. A 30-minute active window can then
    never lose to it, and the case tests the thing it names in every timezone
    and at every hour.
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    now = datetime.now(UTC)
    opens = (now + timedelta(hours=6)).replace(second=0, microsecond=0)
    lifts = (now + timedelta(hours=8)).replace(second=0, microsecond=0)
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, UUID(str(seeded["profile_release"]))),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row["profile"])
        profile["proactivity_policy"]["quiet_hours"] = {
            "timezone": "UTC",
            "ranges": [{"start": opens.strftime("%H:%M"), "end": lifts.strftime("%H:%M")}],
        }
        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        profile["identity"]["version"] = "1.0.5"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                release_id,
                row["profile_id"],
                "1.0.5",
                canonical_hash(profile),
                Jsonb(profile),
            ),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, tenant_id),
        )
        connection.commit()


def test_a_timer_turn_writes_the_window_deadline_into_the_command(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Q18's field, on a row that a real timer produced.

    **The work order says this batch has to put it there**: B1-3 was supposed to
    and could not, because before B4-2 nothing produced the value and it would
    have been a field that is always `null` -- the T118 / T193 shape ADR-0098 §一
    was amended over.

    **The instant is checked against the customer's clock**, not against "now
    plus the window": an implementation anchored on the wrong moment would still
    write a plausible timestamp.

    **And the row carries exactly the keys something reads** (ADR-0117 §四).
    In B4-4a that was one: `classification` had no reader until the quota
    arrived. **B4-4b brought the reader and the key together**, and this
    assertion went red on that commit -- which is the guard doing its job rather
    than a test that needed loosening. Widening it is only legal because
    `_reengagement_gate` now counts on it (`RE_ENGAGEMENT_SQL`).
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_release_whose_quiet_window_is_far_away(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _fire_the_timer(settings, tenant_id, conversation_id)

    row = _proactive_row(tenant_id, conversation_id)
    block = row["command"]["proactivity"]
    # **`window_lifts_at` is absent here, and that is the assertion.** It is
    # written only when the quiet window is what expires the row; the active
    # window closing has no end to name (B4-5). Keeping this an exact set is
    # what makes that a statement rather than an omission.
    assert set(block) == {"window_expires_at", "classification"}, (
        "a key with no reader in this batch is the thing ADR-0117 §四 rules out"
    )
    assert block["classification"] == IN_SESSION_CONTINUATION

    with _migration_connection() as connection:
        aggregate = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert aggregate is not None
    last_inbound_at = datetime.fromisoformat(
        str(aggregate["state"]["last_inbound_at"]).replace("Z", "+00:00")
    )
    assert datetime.fromisoformat(block["window_expires_at"].replace("Z", "+00:00")) == (
        last_inbound_at + timedelta(seconds=ACTIVE_WINDOW_SECONDS)
    )


def test_a_proactive_row_whose_window_closed_is_cancelled_and_never_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The `pre_delivery` half, refused before anything is built. **ADR-0098 §一.**

    The deadline is moved into the past rather than the clock being moved,
    because the deadline is what the row carries and what a real queue delay
    would outlive. What is then asserted is in four parts, and the first is the
    one that matters:

    * the adapter was **never asked** -- `RefusingAdapter.send` raises;
    * the row ends `CANCELLED`, a status that is not a failure and therefore
      leaves nobody a manual task (migration `048`'s argument);
    * no delivery receipt: all four `delivery_receipts.status` values mean the
      provider was asked, and none was;
    * a `policy_decisions` row at `hook='pre_delivery'` -- the first row this
      repository writes at that hook, and the reason the enum has carried the
      value since `001`.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    # Quiet hours is noise for this case; without this it only passes
    # outside 22:30-08:00 Shanghai (B4-5).
    _publish_a_release_whose_quiet_window_is_far_away(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _fire_the_timer(settings, tenant_id, conversation_id)
    row = _proactive_row(tenant_id, conversation_id)

    command = dict(row["command"])
    command["proactivity"] = {
        "window_expires_at": (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
    }
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE outbox_messages SET command = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(command), tenant_id, row["id"]),
        )
        connection.commit()

    result = _dispatcher(settings, RefusingAdapter(), "b44a-refuser").dispatch_one()
    assert result is not None
    assert result.receipt_status == "cancelled"

    with _migration_connection() as connection:
        outbox = connection.execute(
            "SELECT status FROM outbox_messages WHERE tenant_id = %s AND id = %s",
            (tenant_id, row["id"]),
        ).fetchone()
        receipts = connection.execute(
            "SELECT count(*) AS n FROM delivery_receipts WHERE tenant_id = %s AND outbox_id = %s",
            (tenant_id, row["id"]),
        ).fetchone()
        decision = connection.execute(
            "SELECT reason_code, effect FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND hook = 'pre_delivery'",
            (tenant_id, conversation_id),
        ).fetchone()
        tasks = connection.execute(
            "SELECT count(*) AS n FROM manual_tasks WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert outbox is not None and outbox["status"] == "CANCELLED"
    assert receipts is not None and receipts["n"] == 0, (
        "nothing was asked, so nothing was receipted"
    )
    assert decision is not None
    assert decision["reason_code"] == "PROACTIVITY_WINDOW_EXPIRED"
    assert decision["effect"] == "deny"
    assert tasks is not None and tasks["n"] == 0, (
        "an expired window is a policy outcome, not work for a person"
    )

    # **And it is not in the transcript** (independent review 2026-09-09,
    # P1-1). This is the assertion the batch was missing, and the reviewer found
    # its absence with a probe rather than by reading: the outbound visibility
    # predicate looked only at `audience`, and a `CANCELLED` row's audience is
    # `customer`, so a message deliberately never sent sat on the timeline next
    # to one that was. **A takeover operator reads that timeline.**
    #
    # The predicate is imported rather than restated -- a second copy of a
    # visibility rule is how the page and the read-marker drift apart, which is
    # the reason the constant exists at all.
    from fastagent_v2.api.app import _OUTBOUND_IS_A_MESSAGE  # noqa: PLC0415 - test-only

    with _migration_connection() as connection:
        visible = connection.execute(
            "SELECT id FROM outbox_messages"  # noqa: S608 - module constant, never caller input
            f" WHERE tenant_id = %s AND conversation_id = %s AND {_OUTBOUND_IS_A_MESSAGE}",
            (tenant_id, conversation_id),
        ).fetchall()
    ids = {str(item["id"]) for item in visible}
    assert str(row["id"]) not in ids, (
        "a message that was deliberately never sent is shown as one the customer received"
    )
    assert ids, "the real delivered message must still be on the transcript"

    # **And it does not spend a rate-limit slot** (re-review 2026-09-09, P2-1;
    # user's ruling 2026-09-09). The first version of the `CANCELLED` fix
    # changed only the api-side predicate, and the two same-source rules --
    # written together precisely so they could not drift -- stopped agreeing:
    # the rate gate went on counting a row nobody was asked to send. When the
    # budget runs out, what B1-6 withholds is a **real reply**.
    #
    # Both constants are imported rather than restated, for the reason they
    # exist at all.
    from fastagent_v2.worker.pipeline import (  # noqa: PLC0415 - test-only
        AUDIENCE_IS_CUSTOMER_SQL,
        PROVIDER_WAS_ASKED_SQL,
    )

    with _migration_connection() as connection:
        counted = connection.execute(
            "SELECT id FROM outbox_messages"  # noqa: S608 - module constants, never caller input
            f" WHERE tenant_id = %s AND conversation_id = %s"
            f"   AND {AUDIENCE_IS_CUSTOMER_SQL} AND {PROVIDER_WAS_ASKED_SQL}",
            (tenant_id, conversation_id),
        ).fetchall()
    assert str(row["id"]) not in {str(item["id"]) for item in counted}, (
        "a message nobody was asked to send spent the conversation's rate budget"
    )


def test_a_row_expired_by_quiet_hours_records_when_it_may_be_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The path B4-5 created, and the row it would otherwise have written wrongly.

    **Independent review 2026-09-09, P1-1.** Before B4-5 only one thing could
    expire a queued proactive row -- the active window closing -- and that kind
    of expiry has no end: past it the same message is a *re-engagement* carrying
    rules nothing applied to it, so waiting fixes nothing. B4-5 added a second
    source, a quiet window opening ahead of the row, and **that kind does have
    an end**.

    The two therefore land in the same `PROACTIVITY_WINDOW_EXPIRED` row with
    **opposite operational meanings** -- 「不该发」 versus 「该改期发」 -- and the
    column that tells them apart is the one this batch introduced. Without this,
    a quiet-hours cancellation would store `next_allowed_at = NULL`, which
    migration `049` and ADR-0119 §二.1 both define as 「这条拒绝没有自动失效的
    时刻」: **the batch defining the semantics would have produced the first row
    breaking them.**

    The user ruled 2026-09-09 to fix the record and leave the behaviour alone --
    cancelling stays cancelling. Rescheduling instead is registered as **T238**
    with its two open questions written down.

    The deadline is moved into the past rather than the clock forward, the same
    way the case above does it, because the deadline is what the row carries.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    # Quiet hours is noise for this case; without this it only passes
    # outside 22:30-08:00 Shanghai (B4-5).
    _publish_a_release_whose_quiet_window_is_far_away(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _fire_the_timer(settings, tenant_id, conversation_id)
    row = _proactive_row(tenant_id, conversation_id)

    lifts_at = (datetime.now(UTC) + timedelta(hours=8)).replace(microsecond=0)
    command = dict(row["command"])
    command["proactivity"] = {
        "window_expires_at": (
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        ),
        # Present exactly when the expiry came from quiet hours -- the producer
        # writes it only then, so its presence *is* the source.
        "window_lifts_at": lifts_at.isoformat().replace("+00:00", "Z"),
    }
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE outbox_messages SET command = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(command), tenant_id, row["id"]),
        )
        connection.commit()

    result = _dispatcher(settings, RefusingAdapter(), "b45-refuser").dispatch_one()
    assert result is not None
    assert result.receipt_status == "cancelled", "the behaviour does not change; only the record"

    with _migration_connection() as connection:
        decision = connection.execute(
            "SELECT reason_code, next_allowed_at, context FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND hook = 'pre_delivery'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert decision is not None
    assert decision["reason_code"] == "PROACTIVITY_WINDOW_EXPIRED"
    assert decision["next_allowed_at"] == lifts_at, (
        "a cancellation that does have an end stored NULL, which this column "
        "defines as having none"
    )
    assert decision["context"]["window_source"] == "quiet_hours", (
        "the evidence does not say which of the two sources expired the row"
    )
