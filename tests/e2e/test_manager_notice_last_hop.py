"""The manager notification's last hop (B1-5 / ADR-0095 §三).

**What changed, in one sentence.** `_claim_one` no longer filters on
`command->>'audience' = 'customer'`, so the notices L2a has been queueing since
2026-09-04 are finally claimed and sent — the state the freeze called
「通知路径已建、未投递」 moves one square.

The predicate was load-bearing while it lasted, and its own docstring listed the
three things that had to be true before it could go: route by `recipient`
(T90, this batch), typed `content` (done), a one-day deadline (done). These
cases assert the first of those and the two failure modes it opens: a notice
that cannot be addressed must not be sent **anywhere**, and it must not sit
unclaimed forever pretending to be pending work.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.pipeline import OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _seed_manager(tenant_id: UUID) -> None:
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO tenant_manager_contacts(
                tenant_id, channel, config_id, external_user_id
            ) VALUES (%s, 'wecom_proxy', 'config-a', 'manager-userid-b15')
            ON CONFLICT (tenant_id) DO UPDATE SET external_user_id = excluded.external_user_id
            """,
            (tenant_id,),
        )
        connection.commit()


def _queue_notice(
    *,
    settings: Settings,
    seeded: dict[str, Any],
    recipient: dict[str, Any] | None,
) -> tuple[UUID, UUID]:
    """A manager-audience outbox row, written the way `_notify_manager` writes one.

    Built directly rather than driven through a no-send turn: what is under test
    is the **dispatcher's** treatment of an audience it was forbidden to touch
    until this batch, and driving the producer would make the case fail for
    producer reasons too.
    """

    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - see module docstring

    tenant_id = UUID(str(seeded["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    row = _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    outbox_id = UUID(str(row["id"]))
    command: dict[str, Any] = {
        "audience": "tenant_manager",
        "tenant_id": str(tenant_id),
        "conversation_id": str(conversation_id),
        "content": {"type": "text", "text": "one task is waiting"},
        # A day out, the deadline `_notify_manager` writes: a notice that
        # expired before anyone could deliver it was never queued, it was
        # written pre-dead.
        "deadline_at": (datetime.now(UTC) + timedelta(days=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "correlation_id": str(uuid4()),
    }
    if recipient is not None:
        command["recipient"] = recipient
    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE outbox_messages
               SET command = %s, status = 'PENDING', lease_owner = NULL,
                   lease_expires_at = NULL, attempt = 0
             WHERE tenant_id = %s AND id = %s
            """,
            (Jsonb(command), tenant_id, outbox_id),
        )
        connection.commit()
    return conversation_id, outbox_id


def _outbox(tenant_id: UUID, outbox_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT status, attempt FROM outbox_messages WHERE tenant_id = %s AND id = %s",
            (tenant_id, outbox_id),
        ).fetchone()
    assert row is not None
    return row


def test_a_manager_notice_is_claimed_and_carries_its_recipient(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The predicate is gone, and the recipient reaches the adapter.

    Asserted on **what the adapter was handed**, not on the outbox status: a row
    can reach `DELIVERED` while the payload names nobody, and that is exactly
    the shape T90 describes — a successful send to the wrong person.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    recipient = {
        "channel": "wecom_proxy",
        "config_id": "config-a",
        "external_user_id": "manager-userid-b15",
    }
    _conversation_id, outbox_id = _queue_notice(
        settings=settings, seeded=seeded_database, recipient=recipient
    )

    seen: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted", "provider_operation_id": "op-b15"})

    result = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(capture)
        ),
        dispatcher_id="b15-sender",
    ).dispatch_one()

    assert result is not None, "the notice was not claimed -- is the audience predicate back?"
    assert seen, "nothing was sent"
    assert seen[0]["recipient"] == recipient, (
        "the notice went out without naming the manager; a channel that falls "
        "back to the conversation delivers it to the customer (T90)"
    )
    assert _outbox(tenant_id, outbox_id)["status"] == "DELIVERED"


def test_a_notice_with_no_recipient_is_refused_and_never_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The T90 accident, prevented and recorded.**

    The row still carries a real `conversation_id` — it has to, for the foreign
    key — so an adapter would have somewhere to send it. The assertion that
    matters is `seen == []`: not "it failed politely", but "nothing left the
    building".
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    _conversation_id, outbox_id = _queue_notice(
        settings=settings, seeded=seeded_database, recipient=None
    )

    seen: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url)})
        return httpx.Response(200, json={"status": "accepted", "provider_operation_id": "op-no"})

    result = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(capture)
        ),
        dispatcher_id="b15-refuser",
    ).dispatch_one()

    assert result is not None
    assert seen == [], "an unaddressable notice was sent somewhere -- that somewhere is a customer"
    assert _outbox(tenant_id, outbox_id)["status"] == "FAILED"


def test_the_refusal_is_recorded_with_a_reason_and_somebody_owns_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A silent refusal and a silent misdelivery are equally invisible.

    ADR-0095 §三 asks for the refusal to be explicit **and carry a reason code**.
    So the receipt says why, and the row goes through the same terminal path a
    provider refusal takes — which is what creates the manual task. Otherwise
    the notice simply disappears, which is the L2 defect this whole family
    exists to close: a terminal state nobody can see.

    **The transport is one that fails if it is called at all** (independent
    review P3-3). The earlier version installed a 500 responder that was never
    reached, which is green for the right reason and reads like the wrong one —
    as though the failure came from the provider, when the whole point is that
    no provider was asked. The sibling case says this with `seen == []`; so
    does this one now.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    _conversation_id, outbox_id = _queue_notice(
        settings=settings,
        seeded=seeded_database,
        recipient={"channel": "wecom_proxy", "config_id": "config-a"},
    )

    seen: list[str] = []

    def refuse_to_be_called(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url,
            transport=httpx.MockTransport(refuse_to_be_called),
        ),
        dispatcher_id="b15-reason",
    ).dispatch_one()

    with _migration_connection() as connection:
        receipt = connection.execute(
            "SELECT status, evidence FROM delivery_receipts"
            " WHERE tenant_id = %s AND outbox_id = %s",
            (tenant_id, outbox_id),
        ).fetchone()
        task = connection.execute(
            """
            SELECT task_class, notify, reason_code FROM manual_tasks
             WHERE tenant_id = %s AND evidence->>'outbox_id' = %s
            """,
            (tenant_id, str(outbox_id)),
        ).fetchall()

    assert seen == [], "a row refused for having no address still reached the provider"
    assert receipt is not None, "a refusal with no receipt is a delivery nobody can account for"
    assert receipt["evidence"]["reason"] == "unroutable_recipient"
    assert "external_user_id" in str(receipt["evidence"]["detail"])
    assert receipt["evidence"]["request_sent"] is False, (
        "the receipt must say we never asked -- that is the distinction L1 exists for"
    )
    assert len(task) == 1, "the refused notice reached no owner"
    # **"Never asked" no longer borrows the sentence for "the provider refused
    # it"** (independent review P2-3). `DELIVERY_FAILED` reads to a merchant as
    # 「有 N 条消息被渠道拒收」, and for a notice with no address every word of
    # that is false. The platform-internal class is the user's 2026-09-08
    # ruling and is also what keeps the digest from feeding itself (P1-1).
    assert task[0]["reason_code"] == "MANAGER_NOTICE_UNDELIVERED"
    assert task[0]["task_class"] == "platform_internal"
    assert task[0]["notify"] == "none"


def _last_outbound_at(tenant_id: UUID, conversation_id: UUID) -> Any:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT last_outbound_at FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the borrowed conversation does not exist"
    return row["last_outbound_at"]


def test_a_delivered_manager_notice_does_not_stamp_the_borrowed_conversation(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The borrowed `conversation_id` stopped being an address; it is not a fact either.

    **Same shape as T90, one layer down** (independent review P1-2). A manager
    notice and a digest hang off a customer conversation only to satisfy a
    foreign key — `_notify_manager` borrows the current one, `digest.py` borrows
    the oldest task's. `dispatch_one` used to stamp `last_outbound_at` on that
    conversation for every successful send, so telling a manager about a backlog
    recorded that the platform had just spoken to a customer who heard nothing.

    It is not a decorative column: it is in the `conversation-aggregate`
    contract, `GET /conversations/{id}` returns it, and
    `conversation-engine.md:558` lists it as one of the anchors proactivity
    computes `due` from — the next batch's subject, which would take this stamp
    as input. Before this case, `grep -rn last_outbound_at tests/` found nothing
    at all, so it is also the column's first assertion in this repository.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_manager(tenant_id)
    conversation_id, outbox_id = _queue_notice(
        settings=settings,
        seeded=seeded_database,
        recipient={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-userid-b15",
        },
    )
    before = _last_outbound_at(tenant_id, conversation_id)

    OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url,
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, json={"status": "accepted", "provider_operation_id": "op-b15-stamp"}
                )
            ),
        ),
        dispatcher_id="b15-stamp",
    ).dispatch_one()

    assert _outbox(tenant_id, outbox_id)["status"] == "DELIVERED", (
        "the notice did not go out, so this case would pass for the wrong reason"
    )
    assert _last_outbound_at(tenant_id, conversation_id) == before, (
        "a manager notice moved a customer conversation's last_outbound_at -- "
        "that column says the platform spoke to this customer, and it did not"
    )
