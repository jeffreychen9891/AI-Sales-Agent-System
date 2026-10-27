"""B3 #21–#23: the three contact reads (freeze `C0B-033` / `031` / `032`).

**The case that carries this batch is
`test_the_conversation_link_is_the_identity_ingress_recorded`.** The other cases
show rows coming back; that one checks the thing a stored column can silently
get wrong -- that the contact on a conversation is the customer who actually
wrote in it, rather than whichever contact happened to exist. It is driven
end to end: a real ingress call creates the contact, the real worker creates the
aggregate, and the route joins what they wrote.

**These routes read a table this batch does not write.** The writer is ingress
(batch A, ADR-0048 K11), so every fixture here goes through `accept()` rather
than inserting contacts directly -- a contact this repository cannot produce is
not worth asserting about.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import MAX_PAGE_ITEMS, create_app
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import VIEWER_EXCLUSIONS, system_role_permissions
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import (
    callback,
    callback_body,
    callback_item,
    conversation_for,
)

pytestmark = [pytest.mark.integration]

WORKING = ["contact.list", "contact.read"]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _binding(seeded: dict[str, Any], config_id: str, *, tenant_key: str = "tenant_a") -> str:
    """A channel binding, because `039` references one.

    Batch A gained `FOREIGN KEY (tenant_id, config_id) REFERENCES
    channel_bindings` in its own review (P2-4), and it refused two fixtures
    here on the first run afterwards -- both had invented a `config_id`. That
    is the constraint doing its job: a contact under a binding that does not
    exist is a row production cannot produce.

    Tenant B has no binding in the seed at all, so one is built from its own
    profile release rather than borrowing tenant A's.
    """

    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded[tenant_key],),
        ).fetchone()
        assert release is not None
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (config_id, seeded[tenant_key], release["id"], "0" * 64),
        )
        connection.commit()
    return config_id


def _say(
    settings: Settings,
    seeded: dict[str, Any],
    *,
    actor_id: str,
    occurred_at: datetime | None = None,
) -> None:
    """One customer message, through the real ingress -- the only contact writer."""

    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=callback(
            external_event_id=f"contact-route-{uuid4()}",
            sender_id=actor_id,
            occurred_at=occurred_at,
        ),
    )
    assert accepted.accepted is True


def _drain(settings: Settings) -> None:
    """Let the worker create the aggregate, which is what records the link."""

    processed = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id="contact-route-worker",
    ).process_one(block_ms=500)
    assert processed is not None


def test_the_list_shows_who_wrote_and_orders_by_recency(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #21, and the ordering is the one the list is for.

    Who wrote recently is what somebody picking up a queue needs; it is also the
    only ordering `039`'s index supports, so the two agree by construction.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    now = datetime.now(UTC)
    _say(settings, seeded_database, actor_id="older",
         occurred_at=now - timedelta(hours=2))
    _say(settings, seeded_database, actor_id="newer",
         occurred_at=now - timedelta(minutes=5))

    response = client.get("/contacts", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["external_user_id"] for item in items] == ["newer", "older"]
    assert items[0]["config_id"] == "config-a"
    assert items[0]["message_count"] == 1
    # Empty, and that is an answer: nothing in M1a can fill a profile (K11 §3).
    assert items[0]["profile_data"] == {}
    assert response.json()["next_cursor"] is None


def test_the_filter_is_an_equality_on_the_binding(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """「bounded filters」: one filter, and it is `=`.

    An open filter language over a customer table is a way to ask "who wrote in
    about X" without anybody granting that. `config_id` is bounded by being an
    equality on a value the tenant already knows, and it is the one split that
    matters: the same person under two bindings is two contacts (K11 §2).
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    _say(settings, seeded_database, actor_id="customer-1")
    _binding(seeded_database, "config-b")
    with _migration_connection() as connection:
        connection.execute(
            "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
            " VALUES (%s, %s, 'config-b', 'customer-1')",
            (seeded_database["tenant_a"], uuid4()),
        )
        connection.commit()

    both = client.get("/contacts", headers=headers).json()["items"]
    assert len(both) == 2

    filtered = client.get("/contacts?config_id=config-b", headers=headers).json()["items"]
    assert [item["config_id"] for item in filtered] == ["config-b"]


def test_reading_one_contact_by_id(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #22, plus the 404 that keeps the id from being an oracle."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    _say(settings, seeded_database, actor_id="customer-1")
    listed = client.get("/contacts", headers=headers).json()["items"][0]

    response = client.get(f"/contacts/{listed['contact_id']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == listed

    assert client.get(f"/contacts/{uuid4()}", headers=headers).status_code == 404


def test_another_tenants_contact_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS-CONTACT-002: elsewhere and absent answer identically."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    foreign = uuid4()
    tenant_b_binding = _binding(seeded_database, "config-b-tenant", tenant_key="tenant_b")
    with _migration_connection() as connection:
        connection.execute(
            "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
            " VALUES (%s, %s, %s, 'not-yours')",
            (seeded_database["tenant_b"], foreign, tenant_b_binding),
        )
        connection.commit()

    assert client.get(f"/contacts/{foreign}", headers=headers).status_code == 404
    assert client.get("/contacts", headers=headers).json()["items"] == []


def test_the_conversation_link_is_the_identity_ingress_recorded(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The case this batch is built around** (freeze B3 #23, migration 040).

    A stored link can be wrong in a way a derived one cannot: it can name *a*
    contact rather than *this conversation's* contact, and nothing downstream
    would notice. So this drives the whole chain -- ingress creates two contacts
    from two identities, the worker creates each aggregate, and the route is
    asked for each conversation in turn.

    The second customer exists precisely so that "returns a contact" and
    "returns the right contact" are different assertions.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    first_conversation = conversation_for(tenant_id=tenant_id, sender_id="customer-1")
    second_conversation = conversation_for(tenant_id=tenant_id, sender_id="customer-2")
    _say(settings, seeded_database, actor_id="customer-1")
    _drain(settings)
    _say(settings, seeded_database, actor_id="customer-2")
    _drain(settings)

    first = client.get(f"/contacts/by-conversation/{first_conversation}", headers=headers)
    second = client.get(f"/contacts/by-conversation/{second_conversation}", headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["external_user_id"] == "customer-1"
    assert second.json()["external_user_id"] == "customer-2"
    assert first.json()["contact_id"] != second.json()["contact_id"]

    # ...and the route reads the same row `GET /contacts/{id}` does.
    direct = client.get(f"/contacts/{first.json()['contact_id']}", headers=headers)
    assert direct.json() == first.json()


def test_a_conversation_with_no_recorded_contact_is_a_flat_404(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Three causes, one answer, and that is deliberate.

    A conversation with no contact (its first message carried no identity), one
    that predates migration 039 (no backfill), and one belonging to another
    tenant are all "no contact recorded here". Distinguishing them would mean
    publishing a fact nobody can derive without asking `created_at`.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    # **A callback with no `senderId`.** The conversation is derived, and there
    # is no identity to derive it from, so the ingress keys it on the message
    # itself -- one conversation for that one message, rather than a shared
    # thread every stranger writes into (`adapter._accept_item`).
    event_id = f"anon-{uuid4()}"
    anonymous = conversation_for(
        tenant_id=UUID(str(seeded_database["tenant_a"])),
        sender_id=f"anonymous:{event_id}",
    )
    item = callback_item(external_event_id=event_id, text="no actor id on this one")
    del item["senderId"]
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback_body(item),
    ).accepted is True
    _drain(settings)

    assert client.get(f"/contacts/by-conversation/{anonymous}", headers=headers).status_code == 404
    assert client.get(f"/contacts/by-conversation/{uuid4()}", headers=headers).status_code == 404

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT contact_id FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], anonymous),
        ).fetchone()
    assert row is not None and row["contact_id"] is None, "no identity, no link"


def test_listing_contacts_does_not_grant_reading_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two actions, and the pair that shares one says so in the manifest.

    `GET /contacts/{id}` and `GET /contacts/by-conversation/{id}` share
    `contact.read` -- the same fact reached two ways -- and the manifest
    declares it, which is what ADR-0048 K1 requires of its second class. The
    list is a different action, so this holds.
    """

    client, headers = _client_for(settings, seeded_database, ["contact.list"])
    _say(settings, seeded_database, actor_id="customer-1")
    listed = client.get("/contacts", headers=headers)
    assert listed.status_code == 200

    contact_id = listed.json()["items"][0]["contact_id"]
    assert client.get(f"/contacts/{contact_id}", headers=headers).status_code == 403


def test_contacts_are_not_a_viewers_read() -> None:
    """The ruling of 2026-09-06, asserted where it takes effect.

    The freeze asks for a field-level gate on the sensitive fields; M1a puts the
    whole read behind the action instead, because `profile_data` is empty until
    something can fill it and a base tier without `external_user_id` shows an
    internal uuid and two counters. Registered as T179 with its due date.
    """

    assert "contact.list" in VIEWER_EXCLUSIONS
    assert "contact.read" in VIEWER_EXCLUSIONS
    roles = system_role_permissions()
    assert "contact.list" not in roles["viewer"]
    assert "contact.read" not in roles["viewer"]
    assert {"contact.list", "contact.read"} <= roles["operator"]
    assert {"contact.list", "contact.read"} <= roles["owner"]


def test_the_cursor_walks_every_contact_once(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-06, P2-2**: 「cursor」 is freeze #21's first word.

    Eight cases and the only line touching the cursor was
    `assert next_cursor is None`. The implementation was right -- the review
    measured that -- but nothing was watching it, and B3 #5 had just been
    caught with a cursor missing a component.

    Every contact here shares one `last_seen_at`, which is the case the
    tie-break exists for: without `id` in the key the walk either repeats a
    page forever or skips rows.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    at = datetime.now(UTC) - timedelta(hours=1)
    for index in range(MAX_PAGE_ITEMS + 3):
        _say(
            settings,
            seeded_database,
            actor_id=f"customer-{index:04d}",
            occurred_at=at,
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        query = f"?cursor={cursor}" if cursor else ""
        body = client.get(f"/contacts{query}", headers=headers).json()
        seen.extend(item["contact_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert cursor is None, "the walk terminates"
    assert len(seen) == MAX_PAGE_ITEMS + 3
    assert len(set(seen)) == len(seen), "no contact appears twice"


def test_a_cursor_it_did_not_issue_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """400, not an empty page (P2-2).

    An empty page reads as "you have seen everything", so a client walking
    `next_cursor` would stop early and never know it had.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    for bad in ("nonsense", "2026-09-06T12:00:00+00:00"):
        response = client.get(f"/contacts?cursor={bad}", headers=headers)
        assert response.status_code == 400, (bad, response.text)
        assert response.json()["code"] == "INVALID_CURSOR"


def test_by_conversation_joins_on_the_tenant_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P2-4**: 「tenant 组合外键一致」 had no assertion.

    The join carries `c.tenant_id = a.tenant_id`, and a review probe removed
    it with the whole file staying green. Two tenants holding a contact with
    **the same id** is what makes the missing predicate observable -- RLS
    would still be there, but this asserts the layer freeze #23 names.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    # The conversation follows from who spoke, so it is derived rather than
    # chosen -- the same value the ingress derives for this sender.
    conversation_id = conversation_for(
        tenant_id=UUID(str(seeded_database["tenant_a"])), sender_id="mine"
    )
    _say(settings, seeded_database, actor_id="mine")
    _drain(settings)

    mine = client.get(f"/contacts/by-conversation/{conversation_id}", headers=headers)
    assert mine.status_code == 200, mine.text
    shared_id = UUID(mine.json()["contact_id"])

    # The other tenant gets a contact carrying the *same* primary key.
    other_binding = _binding(seeded_database, "config-b-shared", tenant_key="tenant_b")
    with _migration_connection() as connection:
        connection.execute(
            "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
            " VALUES (%s, %s, %s, 'theirs')",
            (seeded_database["tenant_b"], shared_id, other_binding),
        )
        connection.commit()

    again = client.get(f"/contacts/by-conversation/{conversation_id}", headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["external_user_id"] == "mine", "the tenant half of the key holds"


def test_a_conversation_that_opened_anonymously_stays_its_own_conversation(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Rewritten by the callback-envelope batch, and the rewrite is the point.**

    This case used to assert a repair: a conversation opened without an identity
    kept `contact_id` NULL, and the *next* message from the same conversation --
    now carrying an identity -- filled it in (independent review P2-5).

    That scenario is no longer reachable through the ingress, because the
    conversation is **derived from who is speaking** (user ruling 2026-09-10).
    A message with no usable `senderId` is keyed on the message itself; the same
    person's identified message is keyed on their id. They are two conversations
    by construction, and no later message can move one into the other.

    So this asserts what production now does, rather than a repair that can no
    longer happen:

    * the anonymous message opens a conversation with no contact, and it stays
      404 -- **it does not silently acquire somebody else's identity**;
    * the identified message opens the identified customer's own conversation,
      which is linked from its first message.

    **The repair code is deliberately left in place** and is not dead: rows that
    predate migration 040 still reach it (`test_conversation_binding_backfill`).
    Whether an anonymous conversation should ever be merged into an identified
    one is identity resolution, which K11 §2 keeps out of M1a -- registered
    rather than decided here.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    event_id = f"anon-{uuid4()}"
    anonymous = conversation_for(
        tenant_id=tenant_id, sender_id=f"anonymous:{event_id}"
    )
    item = callback_item(external_event_id=event_id, text="hello, no name on this one")
    del item["senderId"]
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback_body(item),
    ).accepted is True
    _drain(settings)
    assert (
        client.get(f"/contacts/by-conversation/{anonymous}", headers=headers).status_code == 404
    )

    # The same person writes again, this time identified. It is their own
    # conversation, and it is linked from the first message.
    _say(settings, seeded_database, actor_id="known-now")
    _drain(settings)
    identified = conversation_for(tenant_id=tenant_id, sender_id="known-now")
    assert identified != anonymous
    linked = client.get(f"/contacts/by-conversation/{identified}", headers=headers)
    assert linked.status_code == 200, linked.text
    assert linked.json()["external_user_id"] == "known-now"

    # And the anonymous one did not follow it: no identity was invented for it.
    assert (
        client.get(f"/contacts/by-conversation/{anonymous}", headers=headers).status_code == 404
    )
