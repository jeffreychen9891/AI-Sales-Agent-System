"""ADR-0048 K11 / migration 039: the contact ingress creates, and when it does not.

**The case that carries this batch is
`test_a_replayed_delivery_is_not_the_customer_speaking_again`.**
The others show a row appearing and a counter moving; that one is the property
whose absence is invisible -- a channel that retries a webhook would inflate
every customer's message count and drag `last_seen_at` forward, and the numbers
would look plausible forever.

**The writer lives in ingress, and that is K11 rather than convenience**:
「由 ingress 在首次收到该外部身份的消息时创建」, after authentication
(`unified-authorization-layer.md:199`).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import (
    CONTACT_CLOCK_WINDOW,
    IngressAuthenticationError,
    WeComProxyIngress,
)
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection
from psycopg.errors import CheckViolation, ForeignKeyViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback_body, callback_item

pytestmark = [pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _ingress(settings: Settings) -> WeComProxyIngress:
    return WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )


def _binding(seeded: dict[str, Any], config_id: str) -> str:
    """A second channel binding for tenant A.

    **Needed because `039` now references `channel_bindings`** (independent
    review P2-4). The two fixtures below used to invent a `config_id` out of
    thin air; the foreign key refused them on the first run after it was
    added, which is the constraint doing its job -- a contact under a binding
    that does not exist is a row production cannot produce.

    Built by copying the seeded binding, so the release and the token hash are
    real rather than fabricated.
    """

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            )
            SELECT %s, tenant_id, profile_release_id, current_token_hash
              FROM channel_bindings WHERE tenant_id = %s AND config_id = %s
            ON CONFLICT DO NOTHING
            """,
            (config_id, seeded["tenant_a"], "config-a"),
        )
        connection.commit()
    return config_id


def _send(
    settings: Settings,
    seeded: dict[str, Any],
    *,
    actor_id: str | None = "customer-1",
    conversation_id: UUID | None = None,
    external_event_id: str | None = None,
    occurred_at: datetime | None = None,
    token: str | None = None,
) -> Any:
    # **`senderId` is both the identity and the conversation key now.** The
    # `conversation_id` parameter stays for the callers that assert on a
    # conversation, and it is *derived* from the sender rather than sent -- a
    # caller passing one is naming the customer it was derived from.
    item = callback_item(
        external_event_id=external_event_id or f"contact-{uuid4()}",
        sender_id=actor_id if isinstance(actor_id, str) else "customer-1",
        occurred_at=occurred_at,
    )
    if actor_id is None:
        del item["senderId"]
    elif not isinstance(actor_id, str):
        item["senderId"] = actor_id
    payload = callback_body(item)
    return _ingress(settings).accept(
        config_id="config-a",
        path_token=token or str(seeded["channel_token"]),
        payload=payload,
    )


def _contacts(seeded: dict[str, Any]) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT * FROM contacts WHERE tenant_id = %s ORDER BY created_at",
            (seeded["tenant_a"],),
        ).fetchall()


def test_the_first_message_from_an_identity_creates_the_contact(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """K11 §1, and the fields are only the ones the platform actually knows.

    `profile_data` is empty and that is honest: M1a calls no channel profile
    API, so nothing can fill in `display_name` or `avatar_url`. The column
    exists because K11 named the allowlist; its emptiness is a fact about what
    the platform knows, not a placeholder.
    """

    assert _send(settings, seeded_database).accepted is True

    rows = _contacts(seeded_database)
    assert len(rows) == 1
    contact = rows[0]
    assert contact["external_user_id"] == "customer-1"
    assert contact["config_id"] == "config-a"
    assert contact["message_count"] == 1
    assert contact["profile_data"] == {}
    assert contact["first_seen_at"] == contact["last_seen_at"]


def test_a_second_message_moves_the_mark_without_making_a_second_contact(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """K11 §2's triple is unique, so the writer is an upsert.

    Two messages from one customer -- in two different conversations, which is
    the case that would tempt a per-conversation row -- must produce one contact
    whose count is two.
    """

    earlier = datetime.now(UTC) - timedelta(hours=1)
    _send(settings, seeded_database, occurred_at=earlier)
    _send(settings, seeded_database, occurred_at=datetime.now(UTC))

    rows = _contacts(seeded_database)
    assert len(rows) == 1
    assert rows[0]["message_count"] == 2
    assert rows[0]["last_seen_at"] > rows[0]["first_seen_at"]


def test_a_late_arriving_older_message_does_not_drag_the_mark_backwards(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`GREATEST`, because delivery order is not send order.

    `last_seen_at` is the customer's clock (`occurred_at`), and a webhook that
    arrives out of order would otherwise make a contact look quieter than they
    are -- which is exactly what somebody triaging a backlog sorts on.
    """

    _send(settings, seeded_database, occurred_at=datetime.now(UTC))
    latest = _contacts(seeded_database)[0]["last_seen_at"]

    _send(settings, seeded_database, occurred_at=datetime.now(UTC) - timedelta(days=1))

    rows = _contacts(seeded_database)
    assert len(rows) == 1
    assert rows[0]["last_seen_at"] == latest, "an older message must not move the mark back"
    assert rows[0]["message_count"] == 2, "...but it is still a message they sent"
    # **And the other column moves the other way** (independent review P3-1).
    # `first_seen_at` says when this identity first spoke; without `LEAST` it
    # said "the first row we happened to insert", which is exactly what an
    # out-of-order delivery gets wrong. Same scenario, opposite direction.
    assert rows[0]["first_seen_at"] < latest, "the older message is now the first one"


def test_a_replayed_delivery_is_not_the_customer_speaking_again(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The property this batch is built around.**

    The channel retries; `inbox_events` already refuses the duplicate by
    `(tenant, source, external_event_id)`. The contact write is on the same
    branch as that insert, so a replay changes nothing -- and if it were not,
    every retry would inflate the count and pull `last_seen_at` forward, with
    numbers that stay plausible forever because nothing else records how many
    times a customer actually wrote.
    """

    same_event = f"replay-{uuid4()}"
    first = _send(settings, seeded_database, external_event_id=same_event)
    assert first.accepted is True and first.duplicate is False

    before = _contacts(seeded_database)[0]
    second = _send(settings, seeded_database, external_event_id=same_event)
    assert second.accepted is True and second.duplicate is True

    after = _contacts(seeded_database)
    assert len(after) == 1
    assert after[0]["message_count"] == before["message_count"] == 1
    assert after[0]["last_seen_at"] == before["last_seen_at"]


def test_a_sender_with_no_identity_creates_no_contact(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """"Unknown" is the envelope's placeholder, and it is not a person.

    The envelope requires `actor_id` and has filled it with `"unknown"` since
    M0. Treating that as an identity would give every unidentified sender one
    shared contact per binding -- a row that looks like a customer who never
    stops talking. The message is still accepted and still processed: not
    knowing who sent something is not a reason to drop it.
    """

    result = _send(settings, seeded_database, actor_id=None)
    assert result.accepted is True
    assert _contacts(seeded_database) == []

    with _migration_connection() as connection:
        envelope = connection.execute(
            "SELECT envelope FROM inbox_events WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], result.inbox_id),
        ).fetchone()["envelope"]
    assert envelope["actor"]["actor_id"] == "unknown", "the envelope keeps its placeholder"


def test_a_failed_authentication_creates_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`unified-authorization-layer.md:199`, and it is why the writer is here.

    A request that fails channel authentication must write no inbox row and
    create no contact. The write sits below every check in `accept`, so this is
    a property of where it is rather than of a guard around it.
    """

    with pytest.raises(IngressAuthenticationError):
        _send(settings, seeded_database, token=str(uuid4()))

    assert _contacts(seeded_database) == []


def test_one_customer_under_two_bindings_is_two_contacts(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """K11 §2: **M1a does not merge identities across channels.**

    An external id means something only inside its binding, and deciding that
    two ids are one person is identity resolution -- its own problem. Guessing
    silently would merge two strangers; keeping them apart is honest and
    reversible.

    The second binding is seeded directly because `POST /channel-bindings` is
    not this batch's subject; what is being asserted is the uniqueness triple.
    """

    _send(settings, seeded_database)
    _binding(seeded_database, "config-b")
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO contacts(tenant_id, id, config_id, external_user_id)
            VALUES (%s, %s, 'config-b', 'customer-1')
            """,
            (seeded_database["tenant_a"], uuid4()),
        )
        connection.commit()

    rows = _contacts(seeded_database)
    assert len(rows) == 2
    assert {row["config_id"] for row in rows} == {"config-a", "config-b"}
    assert {row["external_user_id"] for row in rows} == {"customer-1"}


def test_the_profile_allowlist_is_the_databases_rule_not_a_convention(
    seeded_database: dict[str, Any]
) -> None:
    """K11 §3: only `display_name` and `avatar_url`, 其余不存.

    Enforced by a CHECK rather than by whoever writes the row next, because the
    reason for the allowlist -- 「将来销冠语料会带来更多字段需求，现在定死会返工」
    -- cuts both ways: fields will be *proposed*, and the refusal has to be
    somewhere that a new writer cannot skip.
    """

    config_id = _binding(seeded_database, "config-allowlist")
    with _migration_connection() as connection:
        for index, allowed in enumerate(
            ({"display_name": "A"}, {"avatar_url": "https://x/y.png"}, {})
        ):
            connection.execute(
                "INSERT INTO contacts(tenant_id, id, config_id, external_user_id, profile_data)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    seeded_database["tenant_a"],
                    uuid4(),
                    config_id,
                    f"customer-x{index}",
                    Jsonb(allowed),
                ),
            )
        connection.commit()
        with pytest.raises(CheckViolation):
            connection.execute(
                "INSERT INTO contacts(tenant_id, id, config_id, external_user_id, profile_data)"
                " VALUES (%s, %s, %s, 'customer-y', %s)",
                (
                    seeded_database["tenant_a"],
                    uuid4(),
                    config_id,
                    Jsonb({"phone": "13800000000"}),
                ),
            )
        connection.rollback()


def test_a_payload_claiming_the_future_cannot_pin_the_contact(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-06, P2-1** -- and it was reachable.

    `occurred_at` is whatever the channel says. A callback claiming a date ten
    years out used to pin both timestamps there, and `GREATEST` then refused
    every later message's honest one -- permanently, because `contacts_recent_idx`
    orders the operator list by `last_seen_at DESC` and **no route can write a
    contact at all**. One bad payload, one contact stuck at the top of
    everybody's list forever.

    The writer clamps to the platform clock, which is the direction
    `conversation-aggregate.schema.json` already states for `recorded_at`: a
    sender-controlled timestamp cannot anchor what we store.
    """

    before = datetime.now(UTC)
    _send(
        settings,
        seeded_database,
        actor_id="time-traveller",
        occurred_at=datetime.now(UTC) + timedelta(days=3650),
    )
    rows = _contacts(seeded_database)
    assert len(rows) == 1
    assert rows[0]["last_seen_at"] <= datetime.now(UTC), "clamped to our clock"
    assert rows[0]["last_seen_at"] >= before

    # ...and an honest message afterwards still moves the mark, which is the
    # half that was broken: `GREATEST` against a future value never moves.
    pinned = rows[0]["last_seen_at"]
    _send(settings, seeded_database, actor_id="time-traveller")
    after = _contacts(seeded_database)[0]
    assert after["last_seen_at"] >= pinned
    assert after["message_count"] == 2


def test_an_identity_that_is_not_a_string_is_no_identity(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P2-2**: this field became a key, so it is checked like one.

    `str(payload.get("actor_id"))` accepted anything -- a dict arrived as the
    identity `"{'id': 'x'}"` on a unique index, and it would have stayed there.
    A non-string is now treated the way an absent field already was: **no
    identity**, message still accepted and still processed.

    Trimming is normalisation, not tidiness: without it `" c1 "` and `"c1"`
    are two contacts for one person, and K11's triple makes that permanent.

    The literal placeholder is excluded for the same reason (P3-2): a payload
    that says `actor_id: "unknown"` would otherwise build the one row the
    writer's comment exists to prevent.
    """

    ingress = _ingress(settings)
    for actor_id in ({"id": "x"}, ["a"], 0, False, "", "   ", "unknown"):
        assert ingress.accept(
            config_id="config-a",
            path_token=str(seeded_database["channel_token"]),
            payload=callback_body(
                callback_item(external_event_id=f"shape-{uuid4()}", senderId=actor_id)
            ),
        ).accepted is True, actor_id
    assert _contacts(seeded_database) == [], "none of those is a person"

    # ...and the whitespace around a real one is normalised rather than
    # producing a second contact.
    _send(settings, seeded_database, actor_id="customer-1")
    _send(settings, seeded_database, actor_id="  customer-1  ")
    rows = _contacts(seeded_database)
    assert len(rows) == 1 and rows[0]["message_count"] == 2


def test_the_isolation_policy_holds_for_the_role_that_writes(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P3-3**: nothing was looking at the RLS policy.

    Every other case here reads through the migration connection, and that
    role is the container superuser -- it bypasses RLS, `FORCE` included. So a
    review probe could replace the policy with `USING (true)` and the whole
    suite stayed green. The behaviour was right; the coverage was not.

    This reads as `fastagent_ingress`, the role that actually writes contacts,
    with the session variable the policy reads set to the other tenant.
    """

    _send(settings, seeded_database, actor_id="customer-1")
    ingress_db = Database(settings.ingress_database_url)
    with ingress_db.transaction(tenant_id=seeded_database["tenant_a"]) as connection:
        assert connection.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"] == 1
    with ingress_db.transaction(tenant_id=seeded_database["tenant_b"]) as connection:
        assert connection.execute("SELECT count(*) AS n FROM contacts").fetchone()["n"] == 0


def test_an_over_long_identity_costs_the_contact_not_the_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Re-review 2026-09-06, P2-1** -- the constraint I added had a write path.

    `039` bounds `external_user_id` at 128. With the check only in the
    database, an id one character over did not produce "no identity": the
    `CheckViolation` escaped `accept()`, **the whole transaction rolled back,
    no `inbox_events` row was written**, and the channel got a 500 -- which it
    retries, hitting the same constraint forever. This batch's own carrying
    case is called "a replayed delivery", so the retry loop is not
    hypothetical.

    It also contradicted two things this batch says out loud: "not knowing who
    sent something is not a reason to drop it", and the P2-2 disposition's
    "nothing new is refused". Type was checked in the writer; length was not.

    Now an over-long id is **no identity**, exactly like a non-string.
    """

    result = _send(settings, seeded_database, actor_id="x" * 129)

    assert result.accepted is True, "the message still arrives"
    with _migration_connection() as connection:
        stored = connection.execute(
            "SELECT count(*) AS n FROM inbox_events WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], result.inbox_id),
        ).fetchone()["n"]
    assert stored == 1, "and it is stored -- the identity is what was dropped"
    assert _contacts(seeded_database) == []

    # The boundary itself is an identity: the bound is `<= 128`, not `< 128`.
    _send(settings, seeded_database, actor_id="y" * 128)
    assert [row["external_user_id"] for row in _contacts(seeded_database)] == ["y" * 128]


def test_an_identity_the_database_cannot_store_costs_the_contact_not_the_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T180 / ADR-0095 §八** -- the third member of the family above.

    A value fails to become a contact in three ways: it is not a string, it is
    too long, or **the database cannot store it**. The first two were handled
    in the writer and degrade to "no identity, message accepted". The third did
    not: one `\x00` in `actor_id` made `psycopg` raise `UntranslatableCharacter`
    straight out of `accept()`, so `inbox_events` got **no row**, the callback
    got a **500**, and the channel redelivered into the same crash.

    **The message was being lost over a field we were already prepared to do
    without** -- which is why the ruling took (a), degrade, rather than (b),
    answer 4xx: a 4xx would also drop a message that is perfectly acceptable.

    The lone surrogate is the same defect from the other side: legal in a
    Python `str`, not encodable as UTF-8, so it fails in the client rather than
    the server. One check covers both because both answer the same question --
    *can this be stored at all*.
    """

    result = _send(settings, seeded_database, actor_id="customer\x00-1")

    assert result.accepted is True, "the message still arrives"
    with _migration_connection() as connection:
        stored = connection.execute(
            "SELECT count(*) AS n FROM inbox_events WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], result.inbox_id),
        ).fetchone()["n"]
    assert stored == 1, "and it is stored -- the identity is what was dropped"
    assert _contacts(seeded_database) == []

    # Not encodable rather than not storable, and the same answer.
    surrogate = _send(settings, seeded_database, actor_id="customer\ud800-1")
    assert surrogate.accepted is True
    assert _contacts(seeded_database) == []

    # And the neighbouring value still is an identity: the check refuses the
    # unstorable, not everything that looks unusual.
    _send(settings, seeded_database, actor_id="customer-\u00e9\u4e2d")
    assert [row["external_user_id"] for row in _contacts(seeded_database)] == [
        "customer-\u00e9\u4e2d"
    ]


def test_a_payload_claiming_the_distant_past_cannot_pin_first_seen(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Re-review P3-1** -- capping the top opened the bottom.

    Before the `LEAST` that the first review asked for, only the *first*
    message could write `first_seen_at` wrongly. With it, **any** message can:
    one claiming 1970 pins "first seen" there permanently, and `GET
    /contacts/{id}` hands that number out with no route able to correct it.

    So the sender's clock is bounded at both ends now. Out-of-order delivery
    inside the window is still honoured -- that is what `GREATEST` / `LEAST`
    are for -- and a timestamp a day out is treated as a clock rather than a
    delivery. **Clamped, never rejected**: the message arrives either way.
    """

    floor = datetime.now(UTC) - CONTACT_CLOCK_WINDOW
    _send(settings, seeded_database, actor_id="ancient")
    _send(
        settings,
        seeded_database,
        actor_id="ancient",
        occurred_at=datetime(1970, 1, 2, tzinfo=UTC),
    )

    rows = _contacts(seeded_database)
    assert len(rows) == 1
    assert rows[0]["message_count"] == 2, "the message was accepted, not refused"
    assert rows[0]["first_seen_at"] >= floor, "1970 is a clock, not a first sighting"

    # ...and an honest older message inside the window still moves it back,
    # which is the behaviour `LEAST` was added for.
    honest = datetime.now(UTC) - timedelta(hours=6)
    _send(settings, seeded_database, actor_id="ancient", occurred_at=honest)
    moved = _contacts(seeded_database)[0]
    assert moved["first_seen_at"] <= honest + timedelta(seconds=1)


def test_the_two_database_constraints_have_their_own_tripwires(
    seeded_database: dict[str, Any]
) -> None:
    """**Re-review P3-2**: deleting either constraint cost nothing.

    Both are exercised by every insert, and the foreign key even caught two of
    my own fixtures -- but "exercised" is not "watched": review probes removed
    each of them and this file stayed green. These two assertions are the
    tripwires.

    The foreign key case borrows the probe's shape on purpose: tenant B
    borrowing tenant A's binding. That is a **cross-tenant** property nobody
    claimed when the key was added -- which makes it exactly the kind of thing
    a later reader would delete as redundant.
    """

    with _migration_connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
                " VALUES (%s, %s, 'config-a', 'borrowed')",
                (seeded_database["tenant_b"], uuid4()),
            )
        connection.rollback()

        with pytest.raises(CheckViolation):
            connection.execute(
                "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
                " VALUES (%s, %s, 'config-a', %s)",
                (seeded_database["tenant_a"], uuid4(), "z" * 129),
            )
        connection.rollback()
