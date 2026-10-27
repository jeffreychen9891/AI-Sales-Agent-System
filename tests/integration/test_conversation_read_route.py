"""B3 #5 `POST /conversations/{id}/read` (ADR-0048 K12, migration 038).

**The case that carries this batch is
`test_the_cursor_never_moves_backwards`.** The others show a cursor being
written and an unread count coming back; that one is the property the freeze
actually asks for (「按 employee read cursor 幂等」) and the only one whose
absence is invisible: a conversation that quietly becomes unread again looks
like new activity, and nobody would suspect the read route of causing it.

**The visible-message set is asserted from both sides** -- a manager
notification cannot be marked read (`test_a_manager_notification_is_not_a_place_you_can_read_up_to`)
and does not appear in the unread count. That set has one definition in
`app.py` and two consumers now; these cases are what stop the two drifting.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import system_role_permissions
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

WORKING = ["conversation_read_cursor.update", "message.list"]


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


def _profile() -> dict[str, Any]:
    return (
        ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
        .package(profile_id="synthetic-service-desk", version="1.0.4")
        .document
    )


def _conversation(seeded: dict[str, Any]) -> UUID:
    tenant_id = seeded["tenant_a"]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        aggregate = _initial_aggregate(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            profile_release_id=release["id"],
            profile=_profile(),
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, generation, state_version,
                target_stack, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                conversation_id,
                release["id"],
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.commit()
    return conversation_id


def _inbound(seeded: dict[str, Any], conversation_id: UUID, *, text: str, minutes: int) -> UUID:
    """One customer message, written the way ingress writes it.

    `created_at` is set explicitly so the order is a fact of the fixture rather
    than of how fast the test ran -- the cursor is about ordering, and two rows
    a microsecond apart would make the assertions depend on the clock.
    """

    event_id = uuid4()
    occurred = datetime.now(UTC) - timedelta(minutes=minutes)
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded["tenant_a"],),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO inbox_events(
                tenant_id, id, event_id, source, external_event_id, conversation_id,
                event_type, profile_release_id, envelope, status, created_at
            ) VALUES (%s, %s, %s, 'wecom_proxy', %s, %s, 'customer_message', %s, %s,
                      'consumed', %s)
            """,
            (
                seeded["tenant_a"],
                event_id,
                event_id,
                f"read-{event_id}",
                conversation_id,
                release["id"],
                Jsonb(
                    {
                        "schema_version": "1.0.0",
                        "event_id": str(event_id),
                        "event_type": "customer_message",
                        "tenant_id": str(seeded["tenant_a"]),
                        "conversation_id": str(conversation_id),
                        "correlation_id": str(uuid4()),
                        "occurred_at": occurred.isoformat().replace("+00:00", "Z"),
                        "aggregate_generation": 0,
                        "actor": {"kind": "customer", "id": "customer-1"},
                        "content": {"type": "text", "text": text},
                    }
                ),
                occurred,
            ),
        )
        connection.commit()
    return event_id


def _manager_notification(seeded: dict[str, Any], conversation_id: UUID) -> UUID:
    """An outbound row the transcript does not show (B3 #3's P1-1).

    Written with `audience: tenant_manager`, which is what the dispatcher reads
    to keep escalations out of the customer's conversation.
    """

    outbox_id = uuid4()
    intent_id = uuid4()
    with _migration_connection() as connection:
        # `outbox_messages.intent_id` has a foreign key into `intent_records`.
        # The seed respects it rather than working around it, for the reason
        # B3 #3's fixture gives: a row shape production cannot produce tests
        # nothing.
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions
            ) VALUES (%s, %s, %s, 'send_message', 'APPLIED', %s, %s, 'seed', %s, %s, %s)
            """,
            (
                seeded["tenant_a"],
                intent_id,
                conversation_id,
                Jsonb({"kind": "text"}),
                Jsonb({"party": "system"}),
                uuid4(),
                str(uuid4()),
                Jsonb({}),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command,
                idempotency_key, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', now())
            """,
            (
                seeded["tenant_a"],
                outbox_id,
                conversation_id,
                intent_id,
                Jsonb(
                    {
                        "audience": "tenant_manager",
                        "tenant_id": str(seeded["tenant_a"]),
                        "conversation_id": str(conversation_id),
                        "content": {"type": "text", "text": "escalation"},
                    }
                ),
                f"notice:{outbox_id}",
            ),
        )
        connection.commit()
    return outbox_id


def _colleague(seeded: dict[str, Any]) -> UUID:
    """A second employee of tenant A, sharing the seeded password.

    The fixture seeds one employee per tenant (`employee_b` is in **tenant B**),
    so the one dimension this table exists for -- a cursor *per person* -- needs
    a person to be created before it can be tested at all. That is exactly why
    the first version of this file did not test it (independent review
    2026-09-06, P2-1): six of seven cases needed only one seat.
    """

    employee_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            )
            SELECT tenant_id, %s, 'operator-a2@example.com', password_hash,
                   %s, 'Operator A2'
              FROM employees WHERE tenant_id = %s AND id = %s
            """,
            (employee_id, Jsonb(WORKING), seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
    return employee_id


def _login(settings: Settings, seeded: dict[str, Any], email: str) -> dict[str, str]:
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={"tenant_code": "tenant-a", "email": email, "password": seeded["login_password"]},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _outbound(
    seeded: dict[str, Any],
    conversation_id: UUID,
    *,
    minutes: int,
    #: 这一行在它那一轮里的位置（T369 / ADR-0140）。**默认 0**，
    #: 只有「一轮写好几行」那种用例才指定它。
    ordinal: int = 0,
    #: 指定 `created_at`，好让两行落在**同一个事务时间戳**上 ——
    #: 一轮两行的形状靠的正是这一点。
    at: datetime | None = None,
    #: 指定行 id：夹具要站在边界上时，排序退回 `id` 的结果必须是确定的。
    row_id: UUID | None = None,
) -> UUID:
    """A reply the agent sent, written the way `pipeline.py` writes one."""

    outbox_id = row_id or uuid4()
    intent_id = uuid4()
    occurred = at or (datetime.now(UTC) - timedelta(minutes=minutes))
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions
            ) VALUES (%s, %s, %s, 'send_message', 'APPLIED', %s, %s, 'seed', %s, %s, %s)
            """,
            (
                seeded["tenant_a"],
                intent_id,
                conversation_id,
                Jsonb({"kind": "text"}),
                Jsonb({"party": "system"}),
                uuid4(),
                str(uuid4()),
                Jsonb({}),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command,
                idempotency_key, status, created_at, ordinal
            ) VALUES (%s, %s, %s, %s, %s, %s, 'DELIVERED', %s, %s)
            """,
            (
                seeded["tenant_a"],
                outbox_id,
                conversation_id,
                intent_id,
                Jsonb(
                    {
                        "audience": "customer",
                        "tenant_id": str(seeded["tenant_a"]),
                        "conversation_id": str(conversation_id),
                        "content": {"type": "text", "text": "we are on it"},
                    }
                ),
                f"reply:{outbox_id}",
                occurred,
                ordinal,
            ),
        )
        connection.commit()
    return outbox_id


def _mark(client: TestClient, headers: dict[str, str], conversation_id: UUID, ref: UUID) -> Any:
    return client.post(
        f"/conversations/{conversation_id}/read",
        json={"last_read_ref": str(ref)},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def _cursor(seeded: dict[str, Any], conversation_id: UUID) -> dict[str, Any] | None:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT * FROM employee_read_cursors WHERE tenant_id = %s AND conversation_id = %s",
            (seeded["tenant_a"], conversation_id),
        ).fetchone()


def test_marking_read_records_the_place_and_counts_what_is_left(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #5, and the unread count is why the cursor is not write-only.

    `GET /conversations` (B3 #1) is where an unread count belongs and it is not
    built (T173). Shipping the write alone would leave a table nothing reads --
    registered twice in one day as T175 and T177 -- so the answer carries the
    count derived from the cursor just written.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(seeded_database)
    first = _inbound(seeded_database, conversation_id, text="hello", minutes=30)
    _inbound(seeded_database, conversation_id, text="anyone there?", minutes=20)
    _inbound(seeded_database, conversation_id, text="still waiting", minutes=10)

    response = _mark(client, headers, conversation_id, first)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["last_read_ref"] == str(first)
    assert result["unread_inbound_remaining"] == 2

    row = _cursor(seeded_database, conversation_id)
    assert row is not None
    assert row["employee_id"] == seeded_database["employee_a"]
    assert row["last_read_ref"] == first, "the column is `uuid`, so the row is a UUID"


def test_the_cursor_never_moves_backwards(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The property this batch is built around**: 「按 employee read cursor 幂等」.

    Two things must not move the cursor back: the same call arriving twice
    (idempotency, which the primary key provides), and a *later* call naming an
    *earlier* message -- two agents on one account, or a retry that overtook a
    newer request. A conversation that silently becomes unread again looks like
    new activity, and nobody would suspect this route of having caused it.

    The answer is 200 either way and reports where the cursor **is**, not where
    the call tried to put it. Refusing with an error would make an ordinary race
    look like a client bug.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(seeded_database)
    older = _inbound(seeded_database, conversation_id, text="first", minutes=30)
    newer = _inbound(seeded_database, conversation_id, text="second", minutes=10)

    assert _mark(client, headers, conversation_id, newer).status_code == 200
    repeated = _mark(client, headers, conversation_id, newer)
    assert repeated.status_code == 200
    assert repeated.json()["result"]["unread_inbound_remaining"] == 0

    backwards = _mark(client, headers, conversation_id, older)
    assert backwards.status_code == 200, backwards.text
    assert backwards.json()["result"]["last_read_ref"] == str(newer), (
        "an older marker must not make the conversation unread again"
    )
    assert backwards.json()["result"]["unread_inbound_remaining"] == 0
    row = _cursor(seeded_database, conversation_id)
    assert row is not None and row["last_read_ref"] == newer


def test_a_manager_notification_is_not_a_place_you_can_read_up_to(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The visible set has one definition and two consumers; this holds them together.

    A manager notification lives in `outbox_messages` and is deliberately not in
    the transcript (B3 #3's P1-1). If the cursor resolved ids against a wider
    set than the page shows, a client could mark read something it never
    displayed -- and the unread count would be computed over a different set
    than the one the operator is looking at.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(seeded_database)
    visible = _inbound(seeded_database, conversation_id, text="hello", minutes=30)
    notice = _manager_notification(seeded_database, conversation_id)

    refused = _mark(client, headers, conversation_id, notice)
    assert refused.status_code == 404, refused.text
    assert refused.json()["code"] == "MESSAGE_NOT_FOUND"
    assert _cursor(seeded_database, conversation_id) is None, "a refused call writes nothing"

    # ...and it is not counted as unread either, which is the same rule read
    # from the other end.
    marked = _mark(client, headers, conversation_id, visible)
    assert marked.status_code == 200, marked.text
    assert marked.json()["result"]["unread_inbound_remaining"] == 0


def test_a_message_from_another_conversation_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The marker is resolved within the conversation, not globally.

    Without the `conversation_id` predicate a valid id from a neighbouring
    conversation would set this one's cursor to a timestamp that means nothing
    here -- and the unread count would be silently wrong rather than absent.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    mine = _conversation(seeded_database)
    other = _conversation(seeded_database)
    elsewhere = _inbound(seeded_database, other, text="not yours", minutes=5)

    response = _mark(client, headers, mine, elsewhere)
    assert response.status_code == 404, response.text
    assert _cursor(seeded_database, mine) is None


def test_another_tenants_conversation_answers_the_same_404(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS-READ-CURSOR-001: absent and elsewhere are indistinguishable."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    foreign = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_b"],),
        ).fetchone()
        aggregate = _initial_aggregate(
            tenant_id=seeded_database["tenant_b"],
            conversation_id=foreign,
            profile_release_id=release["id"],
            profile=_profile(),
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, generation, state_version,
                target_stack, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                seeded_database["tenant_b"],
                foreign,
                release["id"],
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.commit()

    assert _mark(client, headers, foreign, uuid4()).status_code == 404
    assert _mark(client, headers, uuid4(), uuid4()).status_code == 404


def test_reading_the_transcript_does_not_grant_marking_it_read(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two actions, and the write is not implied by the read.

    `message.list` is what shows the transcript; moving your own cursor is a
    write and has its own action, so a subject granted only the read gets 403
    here rather than silently writing state.
    """

    client, headers = _client_for(settings, seeded_database, ["message.list"])
    conversation_id = _conversation(seeded_database)
    marker = _inbound(seeded_database, conversation_id, text="hello", minutes=5)

    assert _mark(client, headers, conversation_id, marker).status_code == 403
    assert _cursor(seeded_database, conversation_id) is None


def test_the_cursor_write_is_an_operators_and_not_a_viewers() -> None:
    """`viewer` is reads-only by construction, and this is a write.

    Stated as an assertion rather than left to the derivation, because the
    consequence is user-visible: a viewer can read conversations and cannot
    clear their own unread count. That is a decision about what `viewer` means,
    and it should fail loudly if somebody changes it by accident (T114).
    """

    roles = system_role_permissions()
    assert "conversation_read_cursor.update" in roles["operator"]
    assert "conversation_read_cursor.update" not in roles["viewer"]
    assert "conversation_read_cursor.update" in roles["owner"]


def test_each_person_has_their_own_cursor(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The dimension this table exists for** (independent review P2-1).

    A probe in that review deleted the `employee_id` filter from the read-back
    and the whole suite stayed green: seven cases, one seat. So this is the
    tripwire for it -- two people, one conversation, and three things that must
    all be true: B reads back their own position, A is unaffected by B having
    read something, and the table holds two rows.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    colleague = _colleague(seeded_database)
    other_headers = _login(settings, seeded_database, "operator-a2@example.com")
    conversation_id = _conversation(seeded_database)
    first = _inbound(seeded_database, conversation_id, text="one", minutes=30)
    second = _inbound(seeded_database, conversation_id, text="two", minutes=20)
    _inbound(seeded_database, conversation_id, text="three", minutes=10)

    mine = _mark(client, headers, conversation_id, first)
    assert mine.status_code == 200, mine.text
    assert mine.json()["result"]["unread_inbound_remaining"] == 2

    theirs = _mark(client, other_headers, conversation_id, second)
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["result"]["last_read_ref"] == str(second)
    assert theirs.json()["result"]["unread_inbound_remaining"] == 1

    # A reads again and is exactly where A left off: B moving did not touch it.
    again = _mark(client, headers, conversation_id, first)
    assert again.json()["result"]["last_read_ref"] == str(first)
    assert again.json()["result"]["unread_inbound_remaining"] == 2

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT employee_id, last_read_ref FROM employee_read_cursors"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (seeded_database["tenant_a"], conversation_id),
        ).fetchall()
    assert len(rows) == 2
    assert {str(row["employee_id"]) for row in rows} == {
        str(seeded_database["employee_a"]),
        str(colleague),
    }


def test_the_agents_own_reply_is_not_unread(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Unread means what was said *to* the reader (independent review P3-2).

    The first version counted every visible row after the cursor, so answering a
    customer raised your own unread count. The count is what a conversation list
    sorts somebody's day by, and B3 #1 will copy this definition -- in this
    repository a precedent is a contract, so it is worth one assertion now.

    The outbound row is still **markable**: it is in the transcript, so a person
    can legitimately say they have read up to it. Only the counting is one-sided.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(seeded_database)
    asked = _inbound(seeded_database, conversation_id, text="where is my order", minutes=30)
    replied = _outbound(seeded_database, conversation_id, minutes=20)

    marked = _mark(client, headers, conversation_id, asked)
    assert marked.status_code == 200, marked.text
    assert marked.json()["result"]["unread_inbound_remaining"] == 0, (
        "the only thing after the cursor is our own reply"
    )

    onto_the_reply = _mark(client, headers, conversation_id, replied)
    assert onto_the_reply.status_code == 200, onto_the_reply.text
    assert onto_the_reply.json()["result"]["last_read_source"] == "outbox_messages"
    assert onto_the_reply.json()["result"]["unread_inbound_remaining"] == 0


def test_the_cursor_can_move_within_one_turn(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T369 / ADR-0140：**游标要分得出一轮里的第一条与第二条。**

    一轮写的行 `created_at` 与 `source_table` 都相同，所以在加上 `ordinal`
    之前，那两行在游标的序里**只差一个 `uuid4()`** —— 于是「只许前进」那条
    规则在一轮之内是抛硬币的：把游标从第一条挪到第二条，有一半概率被判成
    「往回走」而被拒绝，标记已读**静默地不生效**。

    ## ⚠️ 夹具站在边界上，所以这条用例在旧实现下**必红**

    两行同一个 `created_at`，而**较大的那个 uuid 给 ordinal 0**：
    旧序按 `id` 比，ordinal 1 的那一行反而「更小」，于是挪过去会被拒。
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(seeded_database)

    turn_at = datetime.now(UTC) - timedelta(minutes=10)
    first, second = sorted((uuid4(), uuid4()), reverse=True)
    assert first > second, "夹具要求 ordinal 0 拿到较大的那个 id"
    _outbound(seeded_database, conversation_id, minutes=0, at=turn_at, ordinal=0, row_id=first)
    _outbound(seeded_database, conversation_id, minutes=0, at=turn_at, ordinal=1, row_id=second)

    onto_the_first = _mark(client, headers, conversation_id, first)
    assert onto_the_first.status_code == 200, onto_the_first.text
    assert onto_the_first.json()["result"]["last_read_ordinal"] == 0
    assert onto_the_first.json()["result"]["last_read_ref"] == str(first)

    onto_the_second = _mark(client, headers, conversation_id, second)
    assert onto_the_second.status_code == 200, onto_the_second.text
    assert onto_the_second.json()["result"]["last_read_ref"] == str(second), (
        "游标没能在一轮之内前进 —— 它按 uuid4() 比，把第二条判成了往回走"
    )
    assert onto_the_second.json()["result"]["last_read_ordinal"] == 1

    # **反过来仍然拒**：一轮之内往回走也是往回走。
    back = _mark(client, headers, conversation_id, first)
    assert back.status_code == 200, back.text
    assert back.json()["result"]["last_read_ref"] == str(second), (
        "游标被挪回了一轮里更早的那一条 —— 那会让一条已读消息重新变成未读"
    )
