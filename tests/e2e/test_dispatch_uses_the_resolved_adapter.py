"""B2-3, driven through `dispatch_one` — because nothing else was driving it.

**This file exists because two probes came back green** (independent review
2026-09-09, P1-1):

* revert `receipt = adapter.send(...)` to `self._channel.send(...)` → **761
  passed, nothing red**. The batch's headline behaviour — per-dispatch adapter
  construction, ADR-0094 §一 Q5 — could be deleted whole and nothing noticed;
* revert the two reconcile calls to `self._channel.` → **761 passed, nothing
  red**. That is the change the batch argued hardest for.

The 20 cases the batch shipped covered the **resolver** (called in isolation)
and the **settings / CLI / guard**. Not one of them drove `dispatch_one`, where
all of the new control flow lives.

**Why the old cases could not catch it**: the 12 updated reconcile cases pass
`adapter=channel`, and `channel` *is* `self._channel`, so they are green either
way. That is the same thing B2-5's self-review said about a different batch —
"every existing case would pass just as well against two copies" — and the
answer is the same: **the two adapters have to be distinguishable**.

So the dispatcher here gets a `channel=` recorder and a `channel_factory=`
recorder that are **different objects**, and every assertion is about *which one
was used*.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import ChannelCapabilities, SendCommand, SendReceipt
from fastagent_v2.worker.pipeline import OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

GUID = "D05FCC41-0000-0000-0000-00000000B23A"
CONTACT = "wxid_customer_b23"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


class RecordingAdapter:
    """A `ChannelAdapter` that says which one it is.

    `name` is the whole point: the assertions below are never "a send happened",
    they are "**this** adapter was the one that sent".
    """

    def __init__(self, name: str, *, status: str = "accepted") -> None:
        self.name = name
        self.sent: list[SendCommand] = []
        self.reconciled: list[str] = []
        self._status = status

    def capabilities(self) -> ChannelCapabilities:
        """Yes -- so that these tests keep exercising the query path they exist for.

        A double that declared otherwise would make the dispatcher skip the
        by-key call, and every assertion below about *which* adapter was asked
        would pass by never asking anyone.
        """

        return ChannelCapabilities(reconcile_by_key=True)

    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        self.sent.append(command)
        return SendReceipt(
            status=self._status,
            provider_operation_id=None if self._status == "unknown" else f"op-{self.name}",
            evidence={"adapter": self.name},
        )

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        self.reconciled.append("reconcile")
        return SendReceipt("unknown", provider_operation_id, {"adapter": self.name})

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        self.reconciled.append("reconcile_by_key")
        return SendReceipt("unknown", None, {"adapter": self.name, "query_sent": True})


def _bind_guid(tenant_id: UUID, config_id: str) -> None:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET provider_instance_id = %s WHERE tenant_id = %s"
            " AND config_id = %s",
            (GUID, tenant_id, config_id),
        )
        connection.commit()


def _give_the_conversation_a_contact(
    tenant_id: UUID, conversation_id: UUID, config_id: str
) -> None:
    contact_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            "INSERT INTO contacts(id, tenant_id, config_id, external_user_id)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (contact_id, tenant_id, config_id, CONTACT),
        )
        connection.execute(
            "UPDATE conversation_aggregates SET contact_id = %s"
            " WHERE tenant_id = %s AND id = %s",
            (contact_id, tenant_id, conversation_id),
        )
        connection.commit()


def _clear_contact(tenant_id: UUID, conversation_id: UUID) -> None:
    """Make the conversation genuinely unaddressable, and prove it.

    `040` allows `contact_id` to be NULL -- that is the state this asserts on --
    but the shared fixture populates one, so "I did not add a contact" is not
    the same as "there is no contact". The read-back is the whole point.
    """

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET contact_id = NULL"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        )
        connection.commit()
        row = connection.execute(
            "SELECT contact_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None and row["contact_id"] is None


def _queue_customer_row(*, settings: Settings, seeded: dict[str, Any]) -> tuple[UUID, UUID]:
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    tenant_id = UUID(str(seeded["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    row = _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    outbox_id = UUID(str(row["id"]))
    command = {
        "audience": "customer",
        "tenant_id": str(tenant_id),
        "conversation_id": str(conversation_id),
        "content": {"type": "text", "text": "hello from b2-3"},
        "deadline_at": (datetime.now(UTC) + timedelta(days=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "correlation_id": str(uuid4()),
    }
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE outbox_messages SET command = %s, status = 'PENDING', lease_owner = NULL,"
            " lease_expires_at = NULL, attempt = 0 WHERE tenant_id = %s AND id = %s",
            (Jsonb(command), tenant_id, outbox_id),
        )
        connection.commit()
    return conversation_id, outbox_id


def _dispatcher(settings: Settings, sink: RecordingAdapter, built: list[RecordingAdapter]):
    def factory(*, guid: str, recipient_id: str) -> RecordingAdapter:
        adapter = RecordingAdapter("provider")
        adapter.guid = guid  # type: ignore[attr-defined]
        adapter.recipient_id = recipient_id  # type: ignore[attr-defined]
        built.append(adapter)
        return adapter

    return OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=sink,
        dispatcher_id="b23-test",
        channel_factory=factory,
    )


def _config_id(seeded: dict[str, Any]) -> str:
    return str(seeded.get("config_id", "config-a"))


def test_the_send_goes_through_the_factorys_adapter_with_the_resolved_address(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Probe R1's answer.** Reverting to `self._channel.send` must go red here.

    Three things at once, because they fail independently: the factory was used
    at all, the injected `channel` was *not*, and the adapter was built with the
    address the row resolved to rather than with something the command carried.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    config_id = _config_id(seeded_database)
    _bind_guid(tenant_id, config_id)
    conversation_id, _outbox_id = _queue_customer_row(settings=settings, seeded=seeded_database)
    _give_the_conversation_a_contact(tenant_id, conversation_id, config_id)

    sink = RecordingAdapter("sink")
    built: list[RecordingAdapter] = []
    result = _dispatcher(settings, sink, built).dispatch_one()

    assert result is not None
    assert len(built) == 1, "the factory was not asked for an adapter"
    assert sink.sent == [], "the send went through the injected channel, not the factory's adapter"
    assert len(built[0].sent) == 1
    assert built[0].guid == GUID  # type: ignore[attr-defined]
    assert built[0].recipient_id == CONTACT  # type: ignore[attr-defined]


def test_the_reconcile_asks_the_adapter_that_sent_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Probe R2's answer.** Reverting the two reconcile calls must go red here.

    An `unknown` send triggers `_reconcile_before_giving_up`. Asking
    `self._channel` there would be a confident answer about the wrong system --
    the M0 simulator has no idea what WeComProxy did with the message. The whole
    argument was written down and nothing asserted it.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    config_id = _config_id(seeded_database)
    _bind_guid(tenant_id, config_id)
    conversation_id, _outbox_id = _queue_customer_row(settings=settings, seeded=seeded_database)
    _give_the_conversation_a_contact(tenant_id, conversation_id, config_id)

    sink = RecordingAdapter("sink")
    built: list[RecordingAdapter] = []

    def factory(*, guid: str, recipient_id: str) -> RecordingAdapter:
        adapter = RecordingAdapter("provider", status="unknown")
        built.append(adapter)
        return adapter

    OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=sink,
        dispatcher_id="b23-test",
        channel_factory=factory,
    ).dispatch_one()

    assert len(built) == 1
    assert built[0].reconciled, "the adapter that sent it was never asked what happened"
    assert sink.reconciled == [], "the reconcile went to the injected channel"


def test_an_unresolvable_address_produces_a_binding_not_ready_receipt(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The refusal's **full shape**, which had zero assertions anywhere.

    `binding_not_ready` is a word the user ruled on 2026-09-09 goes into
    `delivery_receipts` and is not rewritable, so what it is made of is worth
    pinning: the reason, the specific detail, and `request_sent: False` --
    because L1's whole subject is that "we never asked" must not read like "we
    asked and do not know".

    The conversation here has no contact, which is one of the five reasons and
    the one that is a message we can never address rather than a supply problem.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _bind_guid(tenant_id, _config_id(seeded_database))
    conversation_id, outbox_id = _queue_customer_row(settings=settings, seeded=seeded_database)
    # **Cleared explicitly, and asserted.** The first version of this case just
    # did not add a contact and called that "no contact" -- and the gate showed
    # the fixture supplies one, so the address resolved and an adapter was
    # built. A precondition a case depends on has to be established by the case,
    # not inherited from a fixture it does not own.
    _clear_contact(tenant_id, conversation_id)

    sink = RecordingAdapter("sink")
    built: list[RecordingAdapter] = []
    _dispatcher(settings, sink, built).dispatch_one()

    assert built == [], "an address that does not resolve must not build an adapter"
    assert sink.sent == [], "nothing may be sent when there is no address"

    # **Keyed on the row this case queued, not "the newest one".** `ORDER BY
    # ... LIMIT 1` would read whatever another case left behind if the suite
    # ever ran these in one transaction scope -- and `delivery_receipts` is
    # `UNIQUE (tenant_id, outbox_id)`, so there is an exact key to use.
    with _migration_connection() as connection:
        receipt = connection.execute(
            "SELECT evidence, status FROM delivery_receipts"
            " WHERE tenant_id = %s AND outbox_id = %s",
            (tenant_id, outbox_id),
        ).fetchone()
    assert receipt is not None
    evidence = receipt["evidence"]
    assert evidence["reason"] == "binding_not_ready"
    assert evidence["detail"] == "conversation_has_no_contact"
    assert evidence["request_sent"] is False
