"""The rolling cap, against the database. **B4-4b / RFC §6.4.**

The quota is a `count(*)` over `outbox_messages` joined to the contact, so
almost none of it is testable without a database -- and the half that is (the
arithmetic) is the half that cannot be wrong in an interesting way. What can be
wrong is the query: which rows it counts, whose rows they are, and whether the
class that is *exempt* is really exempt.

**Both directions are here on purpose.** A file that only proved the refusal
would leave 「会话内延续……不计入每日主动配额」 unguarded -- and that is the half a
merchant notices, because it is the half that stops replies going out.
"""

from __future__ import annotations

import copy
import os
import threading
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
from fastagent_v2.worker.proactivity import IN_SESSION_CONTINUATION, RE_ENGAGEMENT
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]

#: `profiles/synthetic-third`'s `welcome` frame declares one rule: a
#: `silence_check` 75 seconds after an outbound. Its `reengagement_limits` is
#: `{86400, 1}` -- RFC §6.4's default said out loud.
RULE_ID = "welcome-silence_check"


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
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b44b-seed",
    )
    assert dispatcher.dispatch_one() is not None


def _arm_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    """Take the pending timer through the scheduler, stopping before consumption.

    Split out of `_fire_the_timer` so the concurrency case can arm both
    conversations' timers first and then consume them **at the same instant** --
    the scheduler claims one row per tenant per pass, so arming has to happen
    before either turn starts.
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
        scheduler_id="b44b-scheduler",
        runtime_deadline_seconds=600,
    )
    assert scheduler.enqueue_due(now=row["due_at"] + timedelta(seconds=1))


def _fire_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    _arm_the_timer(settings, tenant_id, conversation_id)
    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id="b44b-worker",
    ).process_one(block_ms=1000)


def _publish_a_re_engaging_release(tenant_id: UUID, seeded: dict[str, Any]) -> None:
    """Bind the channel to a **second** release whose rule produces a re-engagement.

    **Why a second release rather than an edited one**: `profile_releases` is
    append-only -- migration `006` puts a trigger on it and withholds UPDATE from
    every runtime role, because 「evidence recorded against a release id must keep
    resolving to the same bytes」. The production answer to "this release is
    wrong" is the one taken here: **publish another and rebind.**

    **Why doctoring at all**: RFC §6.4 says a `silence_check` may only ever
    produce an in-session continuation -- 「消费时已超窗则只能 `no_message`、作废」
    -- and `synthetic-third`'s `welcome` frame declares nothing else, so the
    shipped profile **cannot** reach the quota. That is the honest reason, not
    convenience. Only `emit_kind` moves.
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, UUID(str(seeded["profile_release"]))),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row["profile"])
        rule = next(
            item
            for definition in profile["target_frames"]
            for item in definition["time_rules"]
            if item["id"] == RULE_ID
        )
        rule["emit_kind"] = "goal_stall"
        # **And push the quiet window out of the way** (B4-5). This helper copies
        # the shipped profile, whose window is 22:30-08:00 Asia/Shanghai; since
        # B4-5 reads it, a timer fired inside it sends nothing and every case
        # here would fail after 22:30 local. Quiet hours has its own tests --
        # here it is noise, and leaving it in would make these results depend on
        # what time the gate runs.
        now = datetime.now(UTC)
        profile["proactivity_policy"]["quiet_hours"] = {
            "timezone": "UTC",
            "ranges": [
                {
                    "start": (now + timedelta(hours=6)).strftime("%H:%M"),
                    "end": (now + timedelta(hours=8)).strftime("%H:%M"),
                }
            ],
        }
        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        # **`^\\d+\\.\\d+\\.\\d+$`** -- `merchant-profile.schema.json` pins the
        # shape and `prepare` validates the profile it just loaded, so a
        # descriptive suffix here would fail the *pipeline*, not the insert.
        profile["identity"]["version"] = "1.0.5"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                release_id,
                row["profile_id"],
                "1.0.5",
                # Recomputed rather than reused: `UNIQUE (tenant_id, content_hash)`
                # would refuse a copy of the shipped hash, and a release whose
                # declared hash is not its content is what the registry refuses
                # on the way out.
                canonical_hash(profile),
                Jsonb(profile),
            ),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, tenant_id),
        )
        connection.commit()


def _seed_a_delivered_proactive(
    tenant_id: UUID,
    conversation_id: UUID,
    *,
    classification: str,
    status: str = "DELIVERED",
    created_ago: timedelta = timedelta(0),
    updated_ago: timedelta = timedelta(0),
) -> None:
    """One proactive row already in the window, copied from a real one.

    A second outbox row needs its own intent: `outbox_messages` is
    `UNIQUE (tenant_id, intent_id)`, so re-labelling the existing row is not
    something production could do either.
    """

    with _migration_connection() as connection:
        source = connection.execute(
            "SELECT command, intent_id FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
        assert source is not None
        command = copy.deepcopy(source["command"])
        command["proactivity"] = {"classification": classification}
        intent_id = uuid4()
        connection.execute(
            "INSERT INTO intent_records("
            "  tenant_id, id, conversation_id, kind, status, proposal, proposed_by,"
            "  strategy_id, target_frame_id, idempotency_key, preconditions, policy_evidence"
            ") SELECT tenant_id, %s, conversation_id, kind, 'APPLIED', proposal, proposed_by,"
            "  strategy_id, target_frame_id, %s, preconditions, policy_evidence"
            "  FROM intent_records WHERE tenant_id = %s AND id = %s",
            (intent_id, f"send_message:{uuid4()}", tenant_id, source["intent_id"]),
        )
        connection.execute(
            "INSERT INTO outbox_messages(tenant_id, id, conversation_id, intent_id, command,"
            "  idempotency_key, status, attempt, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, now() - %s, now() - %s)",
            (
                tenant_id,
                uuid4(),
                conversation_id,
                intent_id,
                Jsonb(command),
                f"send_message:{uuid4()}",
                status,
                created_ago,
                updated_ago,
            ),
        )
        connection.commit()


def _age_the_last_inbound(tenant_id: UUID, conversation_id: UUID) -> None:
    """Push the customer's last message past the active window.

    Written on the aggregate because that is where the classification reads it;
    moving the clock instead would also move `due_at` and the lease arithmetic.
    """

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        state["last_inbound_at"] = (
            (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        )
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()


def _denial(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any] | None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT context FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'"
            "   AND reason_code = 'PROACTIVITY_REENGAGEMENT_LIMIT'",
            (tenant_id, conversation_id),
        ).fetchone()
    return dict(row) if row is not None else None


def _pending_count(tenant_id: UUID, conversation_id: UUID) -> int:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_the_second_re_engagement_inside_the_window_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RFC §6.4's cap: 「默认每客户滚动 24 小时最多 1 次」, and the profile declares 1.

    Three things are arranged and each is load-bearing: the rule emits a kind
    that *may* re-engage, the customer's last message is old enough for the turn
    to classify as one, and one re-engagement is already delivered in the window.

    **The assertion is the reason code, not just the absence of a row.** A turn
    that produced nothing because the plan failed, the model returned no intent,
    or the frame was gone would also leave no outbox row -- and those have
    different fixes. What the cap is required to leave behind is a refusal that
    names itself, and a count that says what it counted.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_re_engaging_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _seed_a_delivered_proactive(tenant_id, conversation_id, classification=RE_ENGAGEMENT)
    _age_the_last_inbound(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    denial = _denial(tenant_id, conversation_id)
    assert denial is not None, "the cap refused nothing, or refused it anonymously"
    assert denial["context"]["reengagement_already_counted"] == 1
    assert denial["context"]["reengagement_max_delivered"] == 1
    assert _pending_count(tenant_id, conversation_id) == 0, "a refused re-engagement was queued"


def test_an_in_session_continuation_is_exempt_from_the_cap(
    quiet_hours_out_of_the_way: None,
    settings: Settings,
    seeded_database: dict[str, Any],
) -> None:
    """「会话内延续……**不计入每日主动配额**」 -- the half that stops replies going out.

    Same arrangement as above minus one thing: the customer's last message is
    **recent**, so the timer turn is an in-session continuation. A quota that
    counted the wrong class, or gated the exempt one, would refuse this send --
    and the shipped profile fires a `silence_check` 75 seconds after every
    outbound, so that failure would reach every conversation this platform has.

    **The seeded row is a delivered re-engagement**, so the cap is genuinely
    full. What makes this send legal is the class of *this* turn, not an empty
    window.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)
    _seed_a_delivered_proactive(tenant_id, conversation_id, classification=RE_ENGAGEMENT)

    _fire_the_timer(settings, tenant_id, conversation_id)

    assert _denial(tenant_id, conversation_id) is None, "the exempt class was gated"
    assert _pending_count(tenant_id, conversation_id) == 1, (
        "the in-session continuation did not produce a message"
    )
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT command FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    assert row["command"]["proactivity"]["classification"] == IN_SESSION_CONTINUATION


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_a_message_the_customer_never_got_does_not_occupy_a_slot(
    settings: Settings, seeded_database: dict[str, Any], status: str
) -> None:
    """`FAILED` and `CANCELLED` are excluded, and the reason is whose problem it is.

    **`CANCELLED` is parametrised in rather than assumed** (independent review
    2026-09-09, P3-4): it is the status **B4-4a just invented**, so of the four
    entries in `REENGAGEMENT_OCCUPYING_STATUSES` it is the one whose exclusion is
    a **new** judgement rather than an inherited one -- and it was the one with
    no case. Adding it to the tuple used to leave all three green.

    RFC §6.4 names `DELIVERED` and 「UNKNOWN 在 reconcile 前占用名额以防重复发送」;
    the principle behind both is that a row which **may have reached the
    customer** takes a slot. A `FAILED` row did not reach them, and holding the
    day's one message against it would silence a merchant over something nobody
    received.

    Same arrangement as the refusal case, with the seeded row `FAILED` instead
    of `DELIVERED` -- so the only difference between green here and red there is
    the status column.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_re_engaging_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _seed_a_delivered_proactive(
        tenant_id, conversation_id, classification=RE_ENGAGEMENT, status=status
    )
    _age_the_last_inbound(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    assert _denial(tenant_id, conversation_id) is None, (
        "a message nobody received spent the customer's daily slot"
    )
    assert _pending_count(tenant_id, conversation_id) == 1


def _sibling_conversation_on_the_same_contact(
    settings: Settings,
    seeded: dict[str, Any],
    tenant_id: UUID,
    conversation_id: UUID,
    *,
    seed_a_delivery: bool = True,
) -> UUID:
    """A second conversation bound to the same `contact_id`, with the day's one
    re-engagement already spent on it.

    **This is the case the batch's own design paragraph is about and had no
    coverage for** (independent review 2026-09-09, P2-1 -- the reviewer wrote a
    probe, it went **green**, so the SQL was right and only the proof was
    missing). Without it, the correlated subquery that widens the count from one
    conversation to one contact is never exercised: every other case in this
    file has a single conversation, where the contact clause and its absence
    produce the same number.
    """

    # **A second customer's conversation, re-pointed at the first's contact.**
    # Since 2026-09-10 the conversation id is derived from the sender, so one
    # contact and one binding mean one conversation: this state is *constructed*
    # by the `UPDATE` below rather than reachable through the ingress (T242).
    # That is what the case needs -- the SQL under test counts by contact, and
    # the rows it counts can outlive the shape that made them.
    sibling = new_conversation(tenant_id)
    _deliver_one(settings, seeded, sibling)
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT contact_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        contact_id = row["contact_id"]
        assert contact_id is not None, (
            "the seeded conversation has no contact, so this case cannot say anything"
        )
        connection.execute(
            "UPDATE conversation_aggregates SET contact_id = %s WHERE tenant_id = %s AND id = %s",
            (contact_id, tenant_id, sibling),
        )
        connection.commit()
    if seed_a_delivery:
        _seed_a_delivered_proactive(tenant_id, sibling, classification=RE_ENGAGEMENT)
    return sibling


def test_the_cap_is_per_customer_not_per_conversation(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One contact, two conversations, one message a day -- spent on the other one.

    RFC §6.4 caps how often **a person** is contacted. Counting per conversation
    would let two conversations of one customer each spend the day's message, and
    a customer with three open threads would get three.

    **The day's message is delivered on the sibling and nothing at all is
    delivered on this conversation**, so a per-conversation count would find zero
    here and allow the send. The refusal is therefore only explicable by the
    contact clause -- which is what makes this a test of that clause rather than
    of the cap in general.

    `reengagement_scope` is asserted too (P3-3): a refusal that counted a
    conversation and one that counted a customer are different facts, and until
    this batch the record could not tell them apart.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_re_engaging_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _sibling_conversation_on_the_same_contact(
        settings, seeded_database, tenant_id, conversation_id
    )
    _age_the_last_inbound(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    denial = _denial(tenant_id, conversation_id)
    assert denial is not None, "the sibling conversation's message did not count"
    assert denial["context"]["reengagement_already_counted"] == 1
    assert denial["context"]["reengagement_scope"] == "contact"
    assert _pending_count(tenant_id, conversation_id) == 0


def test_the_window_is_measured_from_when_we_acted_not_from_when_we_decided(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P3-1: the rolling window is anchored on `updated_at`, not `created_at`.

    RFC §6.4 caps how often the **customer is contacted**; `created_at` is when
    the platform *decided* to contact them, and the gap between the two is queue
    delay -- seconds normally, minutes when a lease has to expire before another
    dispatcher reclaims the row.

    **The seeded row is outside the window by `created_at` and inside it by
    `updated_at`**, so the two anchors give opposite answers and only one of
    them can produce the refusal asserted here. `updated_at` is written by
    `dispatch_one` together with the terminal status, which is the closest
    instant to "the customer was contacted" this schema has.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _publish_a_re_engaging_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _seed_a_delivered_proactive(
        tenant_id,
        conversation_id,
        classification=RE_ENGAGEMENT,
        # 25 hours ago decided, one hour ago delivered: the profile's window is
        # 86400s, so `created_at` says "expired" and `updated_at` says "counts".
        created_ago=timedelta(hours=25),
        updated_ago=timedelta(hours=1),
    )
    _age_the_last_inbound(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    denial = _denial(tenant_id, conversation_id)
    assert denial is not None, (
        "the window was measured from the decision instead of from the delivery"
    )
    assert denial["context"]["reengagement_already_counted"] == 1


def test_two_concurrent_re_engagements_on_one_contact_produce_one_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The lock, driven the way the two existing samples in this repo drive theirs.

    **This case exists because the first version of the batch said it could not
    be written** (independent re-review 2026-09-09, P2-rr-1). It can:
    `test_two_workers_cannot_each_write_a_digest_for_the_same_window` and the
    two-worker budget case in `test_m1a_cost.py` -- **the second of which guards
    a `pg_advisory_xact_lock`** -- are the same shape, two threads on one
    process with their own `Database(...)`, no new fixture.

    **Sequential calls would pass with no lock at all**, which is why a barrier
    is used: the second turn would simply read the first turn's row. What the
    lock does is stop both turns from reading "one slot free" at the same
    instant.

    **The two regressions this is aimed at** (named by the reviewer): moving the
    lock to *after* the count, and keying it on `conversation_id` instead of
    `contact_id`. Both still look like "there is a lock" in a diff, and both
    make it do nothing -- so the assertion is on the outcome, not on the lock.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    first = new_conversation(tenant_id)
    _publish_a_re_engaging_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, first)
    second = _sibling_conversation_on_the_same_contact(
        settings, seeded_database, tenant_id, first, seed_a_delivery=False
    )
    for conversation_id in (first, second):
        _age_the_last_inbound(tenant_id, conversation_id)
        _arm_the_timer(settings, tenant_id, conversation_id)

    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def consume(conversation_id: UUID) -> None:
        processor = InboxProcessor(
            database=Database(settings.worker_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            inference=DeterministicMockInference(),
            worker_id=f"b44b-race-{conversation_id}",
        )
        try:
            ready.wait(timeout=10)
            processor.process_one(block_ms=2000)
        except BaseException as error:  # noqa: BLE001 - reported after the join
            errors.append(error)

    threads = [threading.Thread(target=consume, args=(item,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors

    # **The cap is one per contact per window, so one of the two turns must have
    # produced nothing** -- and the other must have produced exactly one message.
    queued = _pending_count(tenant_id, first) + _pending_count(tenant_id, second)
    assert queued == 1, (
        f"one contact, cap of 1, two simultaneous re-engagements -- {queued} queued"
    )
    refused = [_denial(tenant_id, item) for item in (first, second)]
    assert sum(item is not None for item in refused) == 1, (
        "exactly one of the two turns must have been refused, and say so"
    )
