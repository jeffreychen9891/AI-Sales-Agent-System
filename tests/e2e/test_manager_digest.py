"""L2b: deliveries that end badly become tasks, and an hour's worth becomes one message.

Two halves of ADR-0006 item 4 meet here. L1 made the platform **ask** before
writing `unknown`; this batch makes somebody **responsible** for the answer --
and then makes sure being responsible does not mean forty notifications.

Everything is driven through the production path: a real dispatch against a
transport that fails, the real dead-letter sweep, the real digest. The counting
rules are the interesting part, and each of them is a way the naive version goes
wrong: an empty window that still sends, a restart that counts twice, an hour
that sends twice.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.digest import DIGEST_INTERVAL, ManagerDigest
from fastagent_v2.worker.pipeline import OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _migration_connection() -> Connection[dict[str, Any]]:
    url = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not url:
        pytest.skip("integration database is not configured")
    return Connection.connect(url, row_factory=dict_row)


def _seed_manager(tenant_id: UUID) -> None:
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO tenant_manager_contacts(
                tenant_id, channel, config_id, external_user_id
            ) VALUES (%s, 'wecom_proxy', 'config-a', 'manager-userid-001')
            ON CONFLICT (tenant_id) DO UPDATE SET external_user_id = excluded.external_user_id
            """,
            (tenant_id,),
        )
        connection.commit()


def _a_delivery_that_ends_unknown(
    *, settings: Settings, seeded_database: dict[str, Any], conversation_id: UUID
) -> None:
    """Drive one send to `unknown` through the real dispatcher.

    The transport refuses the send and then refuses the reconcile, which is the
    honest shape of "we asked and the provider does not know either" -- the only
    shape that is allowed to end in UNKNOWN at all (L1).
    """

    # Imported by bare module name, the way `tests/contracts` already shares its
    # builders: `tests/` is not a package, so a dotted import would depend on
    # pytest's import mode rather than on `pythonpath` (the P3-4 finding from
    # the T53 review). `tests/e2e` is listed there for exactly this reason.
    from test_retry_budget import _deliver_to_outbox

    _deliver_to_outbox(
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
        dispatcher_id="l2b-sender",
    ).dispatch_one()
    assert dispatched is not None and dispatched.receipt_status == "unknown"


def test_an_unconfirmed_delivery_becomes_a_task_somebody_owns(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """L2: the terminal state existed and nobody owned it. Now somebody does.

    The task records what the receipt vocabulary knows -- including whether the
    provider was asked, which is the fact L1 exists to produce and the first
    thing a person picking this up needs.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)

    _a_delivery_that_ends_unknown(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    with _migration_connection() as connection:
        task = connection.execute(
            """
            SELECT i.proposal, m.task_class, m.notify, m.digested_at
              FROM intent_records i
              JOIN manual_tasks m ON m.tenant_id = i.tenant_id AND m.intent_id = i.id
             WHERE i.tenant_id = %s AND i.kind = 'manual_task'
               AND i.proposal->>'reason_code' = 'DELIVERY_UNCONFIRMED'
            """,
            (tenant_id,),
        ).fetchone()

    assert task is not None
    assert task["task_class"] == "merchant_action"
    assert task["notify"] == "batched"
    assert task["digested_at"] is None
    assert task["proposal"]["reconcile_attempted"] is True
    assert task["proposal"]["source"] == "delivery"
    # ...and the account of *what happened* actually arrives filled in. Merging
    # L1's re-review fixes moved `http_status` / `reason` into the `send` and
    # `reconcile` namespaces, and this proposal was still reading them off the
    # top level -- three fields that would have quietly become `null` on every
    # unconfirmed delivery. Nothing would have failed: a proposal is JSON and
    # JSON takes nulls. The person picking up the task would have got the form
    # with the three fields they need left blank.
    assert task["proposal"]["adapter"] == "http"
    assert task["proposal"]["send_http_status"] == 500
    assert task["proposal"]["reconcile_http_status"] == 404
    assert "reconcile_skipped_reason" not in task["proposal"]


def test_the_same_terminal_delivery_does_not_produce_two_tasks(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Idempotent by key, because two dispatchers can reach one deadline together.

    A duplicate task would inflate the digest -- "3 deliveries pending" for one
    stuck message -- and the merchant's only way to notice would be to go
    counting, which is the opposite of what a digest is for.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _a_delivery_that_ends_unknown(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT id, conversation_id, target_frame_id FROM intent_records
             WHERE tenant_id = %s AND kind = 'manual_task'
               AND proposal->>'reason_code' = 'DELIVERY_UNCONFIRMED'
            """,
            (tenant_id,),
        ).fetchone()
        outbox_id = connection.execute(
            "SELECT id FROM outbox_messages WHERE tenant_id = %s AND status = 'UNKNOWN'",
            (tenant_id,),
        ).fetchone()["id"]
        # The same insert again, exactly as a second dispatcher would attempt it.
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions,
                policy_evidence, manual_task_id
            ) VALUES (%s, %s, %s, 'manual_task', 'PROPOSED', %s, %s, 'handoff', %s, %s,
                      %s, %s, %s)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            """,
            (
                tenant_id,
                uuid4(),
                row["conversation_id"],
                Jsonb({"reason_code": "DELIVERY_UNCONFIRMED"}),
                Jsonb({"step": "platform", "version": "delivery-manual-task-v1"}),
                row["target_frame_id"],
                f"manual_task:DELIVERY_UNCONFIRMED:{outbox_id}",
                Jsonb({}),
                Jsonb([]),
                uuid4(),
            ),
        )
        connection.commit()
        count = connection.execute(
            """
            SELECT count(*) AS n FROM intent_records
             WHERE tenant_id = %s AND kind = 'manual_task'
               AND proposal->>'reason_code' = 'DELIVERY_UNCONFIRMED'
            """,
            (tenant_id,),
        ).fetchone()["n"]
        # **Two tables now, one idempotency key** (034). The task row is written
        # on the branch that actually filed the intent, so a second dispatcher
        # must leave exactly one of each -- not one intent and two tasks.
        tasks = connection.execute(
            "SELECT count(*) AS n FROM manual_tasks WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()["n"]

    assert count == 1
    assert tasks == 1


def test_a_quiet_hour_produces_no_message_at_all(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The ruling, at the outermost layer: nothing to report, nothing sent.

    Asserted on the outbox rather than on the return value, because "no message"
    has to mean no row -- a caller that logged `DigestResult` and sent anyway
    would pass a weaker test.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)

    result = ManagerDigest(database=Database(settings.worker_database_url)).run_for(
        tenant_id=tenant_id, now=datetime.now(UTC)
    )

    assert result.outbox_id is None
    assert result.reason == "nothing_to_report"
    with _migration_connection() as connection:
        rows = connection.execute(
            """
            SELECT count(*) AS n FROM outbox_messages
             WHERE tenant_id = %s AND command->>'notice_kind' = 'digest'
            """,
            (tenant_id,),
        ).fetchone()["n"]
    assert rows == 0


def test_an_hour_of_stuck_deliveries_is_one_message_and_is_counted_once(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One message for the window, and the tasks marked so the next hour is quiet.

    The marking is the part that matters: a digest that remembered a *window*
    would double-count after a restart and skip after a crash. A task that
    records "I was included" cannot be wrong about itself.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    for _ in range(2):
        _a_delivery_that_ends_unknown(
            settings=settings,
            seeded_database=seeded_database,
            conversation_id=new_conversation(seeded_database["tenant_a"]),
        )

    digest = ManagerDigest(database=Database(settings.worker_database_url))
    now = datetime.now(UTC)
    first = digest.run_for(tenant_id=tenant_id, now=now)

    assert first.outbox_id is not None
    assert first.counted == 2

    with _migration_connection() as connection:
        notice = connection.execute(
            "SELECT command, status FROM outbox_messages WHERE tenant_id = %s AND id = %s",
            (tenant_id, first.outbox_id),
        ).fetchone()
        undigested = connection.execute(
            """
            SELECT count(*) AS n FROM manual_tasks
             WHERE tenant_id = %s AND notify = 'batched' AND digested_at IS NULL
            """,
            (tenant_id,),
        ).fetchone()["n"]

    # One sentence with the count in it, and no per-message noise.
    assert notice["command"]["content"]["type"] == "text"
    assert "2" in notice["command"]["content"]["text"]
    assert notice["command"]["audience"] == "tenant_manager"
    assert notice["command"]["notice_kind"] == "digest"
    # **Queued, and still queued only because this case runs no dispatcher**
    # (independent review P2-2). The line above used to say "queued, not
    # delivered -- the M1a boundary applies here as everywhere", and B1-5 is the
    # batch that stopped making that true: this row is claimable and sendable
    # now. What keeps it `PENDING` is that nothing here claims it. The case that
    # does run a dispatcher over a digest row is
    # `test_a_digest_that_cannot_be_delivered_does_not_become_the_next_digest`
    # below.
    assert notice["status"] == "PENDING"
    assert undigested == 0

    # An hour has not passed, so nothing more is written even though the caller
    # asks again on every worker pass.
    again = digest.run_for(tenant_id=tenant_id, now=now + timedelta(minutes=5))
    assert again.outbox_id is None and again.reason == "interval_not_elapsed"

    # And after the interval, with nothing new, it is still silent.
    later = digest.run_for(tenant_id=tenant_id, now=now + DIGEST_INTERVAL + timedelta(minutes=1))
    assert later.outbox_id is None and later.reason == "nothing_to_report"


def test_without_a_manager_the_backlog_waits_rather_than_being_marked_reported(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A tenant with nobody to tell keeps its news.

    Marking the tasks digested would quietly discard everything that happened
    before the merchant configured a manager -- and the first thing a new
    manager needs is exactly that backlog.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with _migration_connection() as connection:
        connection.execute(
            "DELETE FROM tenant_manager_contacts WHERE tenant_id = %s", (tenant_id,)
        )
        connection.commit()
    _a_delivery_that_ends_unknown(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=new_conversation(seeded_database["tenant_a"]),
    )

    result = ManagerDigest(database=Database(settings.worker_database_url)).run_for(
        tenant_id=tenant_id, now=datetime.now(UTC)
    )

    assert result.reason == "no_manager_contact"
    with _migration_connection() as connection:
        undigested = connection.execute(
            """
            SELECT count(*) AS n FROM manual_tasks
             WHERE tenant_id = %s AND digested_at IS NULL
            """,
            (tenant_id,),
        ).fetchone()["n"]
    assert undigested >= 1


def _a_delivery_the_provider_refuses(
    *, settings: Settings, seeded_database: dict[str, Any], conversation_id: UUID
) -> None:
    """A send the channel looks at and rejects. Terminal `failed`, not `unknown`.

    422 is in `TERMINAL_REFUSAL_STATUSES`: the provider examined the request and
    would refuse an identical retry. That is the half L2b did not cover -- the
    batch produced a task for `unknown` and nothing at all for this.
    """

    from test_retry_budget import _deliver_to_outbox

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "the channel will not carry this"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(refused)
        ),
        dispatcher_id="refused-sender",
    ).dispatch_one()
    assert dispatched is not None and dispatched.receipt_status == "failed"


def _seed_task_for(tenant_id: UUID, *, count: int) -> None:
    """Manual tasks for a tenant with no delivery machinery behind them.

    Written directly because what is under test is the digest's tenant
    predicate, not how the tasks were produced -- producing them through the
    dispatcher would need a seeded conversation and binding per task.
    """

    # A conversation of this tenant's own, because `intent_records` carries a
    # composite foreign key on `(tenant_id, conversation_id)` -- which is itself
    # part of what keeps one tenant's rows out of another's. (My first draft of
    # this helper asserted there was no such key. There is; the schema said so
    # the moment the insert ran.)
    conversation_id = new_conversation(tenant_id)
    frame_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None, "the fixture publishes a release for every seeded tenant"
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
            VALUES (%s, %s, %s)
            """,
            (tenant_id, conversation_id, release["id"]),
        )
        for index in range(count):
            task_id = uuid4()
            connection.execute(
                """
                INSERT INTO intent_records(
                    tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                    strategy_id, target_frame_id, idempotency_key, preconditions,
                    policy_evidence, manual_task_id
                ) VALUES (%s, %s, %s, 'manual_task', 'PROPOSED', %s, %s, 'handoff',
                          %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    task_id,
                    conversation_id,
                    Jsonb({"reason_code": "DELIVERY_UNCONFIRMED", "source": "seed"}),
                    Jsonb({"step": "platform", "version": "seed"}),
                    frame_id,
                    f"seed_task:{tenant_id}:{index}",
                    Jsonb({}),
                    Jsonb([]),
                    task_id,
                ),
            )
            # **Both rows, because production writes both** -- and the digest
            # reads only this one. A fixture seeding the intent alone would be
            # seeding a shape the pipeline cannot produce (the lesson from
            # B3 #3's P1-2), and the digest would then count nothing.
            connection.execute(
                """
                INSERT INTO manual_tasks(
                    tenant_id, id, conversation_id, intent_id,
                    task_class, notify, reason_code
                ) VALUES (%s, %s, %s, %s, 'merchant_action', 'batched',
                          'DELIVERY_UNCONFIRMED')
                """,
                (tenant_id, task_id, conversation_id, task_id),
            )
        connection.commit()


def test_a_delivery_the_channel_refuses_becomes_a_task_and_reaches_the_digest(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The ruling (user, 2026-09-04): `DELIVERY_FAILED` goes in the digest.

    Before this, terminal `failed` produced **nothing** -- no task, no
    classification, no sentence -- while the batch's own design note said
    "delivery FAILED -> digest" (independent review, P1-1). Whether a
    provider-refused delivery belongs in a merchant's digest is push scope and
    therefore the user's call; it was made, and this asserts it.

    The two failure kinds must read differently, because to the person acting on
    the digest they are different work: a refusal will refuse again until
    something changes, an exhausted retry budget may not.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    _a_delivery_the_provider_refuses(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=new_conversation(seeded_database["tenant_a"]),
    )

    with _migration_connection() as connection:
        task = connection.execute(
            """
            SELECT i.proposal, m.task_class, m.notify
              FROM intent_records i
              JOIN manual_tasks m ON m.tenant_id = i.tenant_id AND m.intent_id = i.id
             WHERE i.tenant_id = %s AND i.kind = 'manual_task'
               AND i.proposal->>'reason_code' = 'DELIVERY_FAILED'
            """,
            (tenant_id,),
        ).fetchone()

    assert task is not None, "a refused delivery must leave somebody a task"
    assert task["task_class"] == "merchant_action"
    assert task["notify"] == "batched"
    assert task["proposal"]["http_status"] == 422

    result = ManagerDigest(database=Database(settings.worker_database_url)).run_for(
        tenant_id=tenant_id, now=datetime.now(UTC)
    )

    assert result.outbox_id is not None and result.counted == 1
    with _migration_connection() as connection:
        notice = connection.execute(
            "SELECT command FROM outbox_messages WHERE tenant_id = %s AND id = %s",
            (tenant_id, result.outbox_id),
        ).fetchone()
    text = notice["command"]["content"]["text"]
    assert "拒收" in text, (
        f"a refusal must read as a refusal, not as an exhausted retry budget: {text}"
    )


def test_one_tenants_digest_never_counts_another_tenants_backlog(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P3-5: every digest test lived in `tenant_a`, so isolation had no evidence.

    The digest reads through the app role under RLS like everything else -- but
    "like everything else" is an argument, not a test, and this query groups and
    counts, which is the shape where a missing tenant predicate yields a
    plausible number instead of an error.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    _seed_manager(tenant_a)
    _a_delivery_that_ends_unknown(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=new_conversation(seeded_database["tenant_a"]),
    )
    _seed_task_for(tenant_b, count=3)

    result = ManagerDigest(database=Database(settings.worker_database_url)).run_for(
        tenant_id=tenant_a, now=datetime.now(UTC)
    )

    assert result.counted == 1, "tenant B's three tasks must not be in tenant A's digest"
    with _migration_connection() as connection:
        undigested_b = connection.execute(
            """
            SELECT count(*) AS n FROM manual_tasks
             WHERE tenant_id = %s AND digested_at IS NULL
            """,
            (tenant_b,),
        ).fetchone()["n"]
    assert undigested_b == 3, "and A's digest must not mark B's tasks as reported"


def test_two_workers_cannot_each_write_a_digest_for_the_same_window(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P2-1: the interval check reads, the write creates; between them is a race.

    Two workers read the same "last digest at", both conclude the hour has
    passed, and both write -- the merchant is told twice, with the counts split
    across two messages because each digest marks only the tasks it selected.
    `pg_advisory_xact_lock` serialises the *decision*, and the second holder
    then sees a digest that was not there when it began waiting.

    Driven with two threads and a barrier rather than two sequential calls:
    sequential calls take the interval branch and would pass with no lock at all.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    for _ in range(2):
        _a_delivery_that_ends_unknown(
            settings=settings,
            seeded_database=seeded_database,
            conversation_id=new_conversation(seeded_database["tenant_a"]),
        )

    now = datetime.now(UTC)
    ready = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        digest = ManagerDigest(database=Database(settings.worker_database_url))
        try:
            ready.wait(timeout=10)
            results.append(digest.run_for(tenant_id=tenant_id, now=now))
        except BaseException as error:  # noqa: BLE001 - reported after the join
            errors.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert len(results) == 2
    written = [result for result in results if result.outbox_id is not None]
    assert len(written) == 1, f"exactly one digest may be written, got {results}"
    assert written[0].counted == 2, "and it must cover the whole backlog, not half of it"

    with _migration_connection() as connection:
        notices = connection.execute(
            """
            SELECT count(*) AS n FROM outbox_messages
             WHERE tenant_id = %s AND command->>'notice_kind' = 'digest'
            """,
            (tenant_id,),
        ).fetchone()["n"]
    assert notices == 1


def test_a_digest_that_cannot_be_delivered_does_not_become_the_next_digest(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The ring, and why it is not there** (independent review P1-1).

    Until B1-5 the `audience = 'customer'` predicate in `_claim_one` had two
    jobs. One was keeping manager rows away from a dispatcher that could not
    address them; the other -- which nobody had written down -- was keeping
    their failures out of this digest. B1-5 took over the first job only, and
    the second came due immediately: notice fails -> manual task -> classified
    `MERCHANT_ACTION` + `BATCHED` -> swept into the next window -> another
    manager row -> fails. `DIGEST_INTERVAL` caps that at one an hour per tenant,
    which limits the rate and not the termination, and the count in the digest
    sentence starts counting the digest's own failures.

    On Stage this is not hypothetical: `FASTAGENT_CHANNEL_SEND_URL` points at
    `https://channel-sink.invalid/send`, so every manager row fails.

    The user's ruling (2026-09-08) breaks it by construction rather than by an
    exclusion clause: a non-customer delivery that ends badly is
    `PLATFORM_INTERNAL`, and the query below selects `MERCHANT_ACTION`, so there
    is no row for it to find. This case is the acceptance the reviewer asked
    for: two windows, the manager channel failing throughout, and the second
    window silent. It is also the first case in this file that runs a dispatcher
    over a digest row at all (P2-2) -- `digest.py` is the second and larger
    producer of manager rows, and until now none of them had been delivered.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    for _ in range(2):
        _a_delivery_that_ends_unknown(
            settings=settings,
            seeded_database=seeded_database,
            conversation_id=new_conversation(seeded_database["tenant_a"]),
        )

    digest = ManagerDigest(database=Database(settings.worker_database_url))
    now = datetime.now(UTC)
    first = digest.run_for(tenant_id=tenant_id, now=now)
    assert first.outbox_id is not None and first.counted == 2

    def the_manager_channel_is_gone(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={"detail": "sink is unreachable"})
        return httpx.Response(404, json={"detail": "not found"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(the_manager_channel_is_gone)
        ),
        dispatcher_id="digest-last-hop",
    ).dispatch_one()

    assert dispatched is not None, "the digest row was not claimed"
    assert dispatched.outbox_id == first.outbox_id, (
        "the dispatcher claimed something other than the digest, so this case "
        "proves nothing about the digest's last hop"
    )
    assert dispatched.receipt_status == "unknown"

    with _migration_connection() as connection:
        own_failure = connection.execute(
            """
            SELECT task_class, notify, reason_code FROM manual_tasks
             WHERE tenant_id = %s AND evidence->>'outbox_id' = %s
            """,
            (tenant_id, str(first.outbox_id)),
        ).fetchall()

    assert len(own_failure) == 1, "the failed digest reached no owner"
    assert own_failure[0]["reason_code"] == "MANAGER_NOTICE_UNCONFIRMED"
    assert own_failure[0]["task_class"] == "platform_internal", (
        "the digest's own failure is merchant-facing, so the next window will "
        "report it, and that window's failure will feed the one after it"
    )

    second = digest.run_for(tenant_id=tenant_id, now=now + DIGEST_INTERVAL + timedelta(minutes=1))
    assert second.reason == "nothing_to_report", (
        f"the second window is not silent ({second.reason}) -- the digest is "
        "reporting its own undelivered self"
    )
    assert second.outbox_id is None

    with _migration_connection() as connection:
        notices = connection.execute(
            """
            SELECT count(*) AS n FROM outbox_messages
             WHERE tenant_id = %s AND command->>'notice_kind' = 'digest'
            """,
            (tenant_id,),
        ).fetchone()["n"]
    assert notices == 1, "a second digest row was written out of the first one's failure"
