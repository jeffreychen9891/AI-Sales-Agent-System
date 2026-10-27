"""B3 #25 `PUT /conversations/{id}/frames/{frame_id}/slots` (ADR-0054 §三.2).

**The case that carries this batch is
`test_the_model_cannot_overwrite_what_a_person_wrote`.** Everything else here
checks a write; that one checks the reason the write exists. `apply_slot_updates`
has refused model updates onto `source: human` slots since ADR-0054 landed, and
**nothing could produce such a slot** -- the engine's writeback hard-codes
`source: "model"` and its comment names this route as the missing writer. Until
now the defence had never been executed once.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.target_stack import apply_slot_updates
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

WORKING = ["conversation_takeover.create", "conversation_slot.update"]

#: What the customer told the agent on the phone. A value, so it must not reach
#: the audit table (ADR-0055 §二: `audit_events` is append-only).
DICTATED_VALUE = "the blue one, size 42"


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


def _seed(seeded: dict[str, Any]) -> tuple[UUID, UUID]:
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
    return conversation_id, UUID(str(aggregate["target_stack"][0]["id"]))


def _state(conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE id = %s", (conversation_id,)
        ).fetchone()
    assert row is not None
    return row["state"]


def _held(client: TestClient, headers: dict[str, str], seeded: dict[str, Any]) -> tuple[UUID, UUID]:
    conversation_id, frame_id = _seed(seeded)
    taken = client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert taken.status_code == 200, taken.text
    return conversation_id, frame_id


def _write(
    client: TestClient,
    headers: dict[str, str],
    conversation_id: UUID,
    frame_id: Any,
    slots: dict[str, str],
) -> Any:
    return client.put(
        f"/conversations/{conversation_id}/frames/{frame_id}/slots",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"slots": slots},
    )


def test_a_person_writes_a_slot_and_it_is_marked_as_theirs(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The write, and the three fields the evidence contract requires."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)

    response = _write(
        client, headers, conversation_id, frame_id, {"request_summary": DICTATED_VALUE}
    )

    assert response.status_code == 200, response.text
    validate_control_plane_contract("command-receipt", response.json()["receipt"])
    frame = _state(conversation_id)["target_stack"][0]
    assert frame["slots"]["request_summary"] == DICTATED_VALUE
    evidence = frame["slot_evidence"]["request_summary"]
    assert evidence["source"] == "human"
    # A person asserting a value is not a probability.
    assert evidence["confidence"] == 1.0
    # The only evidence a human write has is the record of who made it.
    #
    # **D3（2026-09-15）：这一项是数组了**，而人工写是**替换**语义
    # （一个人明确写下这个值），所以恰好一条 —— 不追加。
    assert len(evidence["evidence_event_ids"]) == 1
    assert UUID(evidence["evidence_event_ids"][0])


def test_the_model_cannot_overwrite_what_a_person_wrote(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The defence, executed for the first time.**

    `apply_slot_updates` refuses a model update onto a `source: human` slot, and
    ADR-0054 §三.2 puts that check **before** the `correction` exemption on
    purpose: `correction` is a flag the model sets about itself, with no second
    party checking it, so a defence placed after it could be walked around by
    the very party it defends against.

    This asserts both halves: an ordinary model update is refused, and a model
    update claiming `is_correction` is refused **as well**. The second half is
    the one that would have been empty if the ordering were wrong.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)
    assert (
        _write(
            client, headers, conversation_id, frame_id, {"request_summary": DICTATED_VALUE}
        ).status_code
        == 200
    )
    aggregate = _state(conversation_id)
    update = [
        {
            "frame_id": str(frame_id),
            "slot": "request_summary",
            "value": "what the model guessed instead",
            "confidence": 0.99,
            "evidence_event_id": str(uuid4()),
        }
    ]

    ordinary = apply_slot_updates(
        aggregate=aggregate,
        profile=_profile(),
        slot_updates=update,
        recorded_at="2026-09-06T12:00:00Z",
    )
    corrective = apply_slot_updates(
        aggregate=aggregate,
        profile=_profile(),
        slot_updates=update,
        recorded_at="2026-09-06T12:00:00Z",
        is_correction=True,
    )

    assert ordinary.applied == 0
    assert corrective.applied == 0, "the correction exemption walked past the human slot"
    for outcome in (ordinary, corrective):
        frame = outcome.aggregate["target_stack"][0]
        assert frame["slots"]["request_summary"] == DICTATED_VALUE
        assert frame["slot_evidence"]["request_summary"]["source"] == "human"


def test_writing_a_slot_does_not_disturb_the_others(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """An upsert by name, not a replacement of the frame's slot set.

    `PUT` on a collection often means "make it look like this"; here that would
    delete everything the understanding step had written, which the drawing does
    not ask for and no verb in it provides.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)
    assert _write(client, headers, conversation_id, frame_id, {"first": "one"}).status_code == 200

    assert _write(client, headers, conversation_id, frame_id, {"second": "two"}).status_code == 200

    slots = _state(conversation_id)["target_stack"][0]["slots"]
    assert slots["first"] == "one"
    assert slots["second"] == "two"


def test_the_audit_row_names_the_slots_and_not_their_values(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0055 §二**, and `audit_events` is append-only.

    A slot's value is what a customer told somebody. Written here it could never
    be taken back -- and the three conditions for recording a value are not met,
    because `GET /conversations/{id}` can answer what a slot holds today.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)

    assert (
        _write(
            client, headers, conversation_id, frame_id, {"request_summary": DICTATED_VALUE}
        ).status_code
        == 200
    )

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT evidence, reason_code FROM audit_events"
            " WHERE action = 'conversation_slot.update'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["reason_code"] == "SLOTS_WRITTEN_BY_A_PERSON"
    assert rows[0]["evidence"]["slots"] == ["request_summary"]
    assert DICTATED_VALUE not in str(rows[0]["evidence"])


def test_a_slot_cannot_be_written_while_the_ai_is_driving(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The narrow reading, asserted so it is visible.**

    ADR-0054 §三 is about the takeover period. A human slot written while the AI
    holds the conversation would freeze that slot against the model forever --
    the defence working exactly as designed, protecting a value nobody meant to
    protect. Widening this later costs a line; the conversations left with
    uncorrectable slots could not be found afterwards.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _seed(seeded_database)

    response = _write(client, headers, conversation_id, frame_id, {"request_summary": "x"})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONVERSATION_NOT_HELD_BY_A_PERSON"


def test_a_frame_that_is_not_on_this_stack_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The engine ignores an update naming a frame that is not on the stack
    rather than redirecting it to the nearest one. A person gets the same rule
    said louder: 404, not a value landing on a goal they did not name."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, _ = _held(client, headers, seeded_database)

    response = _write(client, headers, conversation_id, uuid4(), {"request_summary": "x"})

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "TARGET_FRAME_NOT_FOUND"


def test_a_terminal_frame_is_history(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`completed` / `abandoned` frames are the evidence of what happened.
    The engine refuses to write their slots; so does this."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)
    with _migration_connection() as connection:
        document = _state(conversation_id)
        document["target_stack"][0]["status"] = "completed"
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s, target_stack = %s WHERE id = %s",
            (Jsonb(document), Jsonb(document["target_stack"]), conversation_id),
        )
        connection.commit()

    response = _write(client, headers, conversation_id, frame_id, {"request_summary": "x"})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "TARGET_FRAME_IS_TERMINAL"


def test_writing_slots_needs_its_own_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`conversation_aggregate.update` is a service action -- no person may
    trigger it -- so writing one slot by hand is a different grant (K1)."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, frame_id = _held(client, headers, seeded_database)
    narrow, narrow_headers = _client_for(
        settings, seeded_database, ["conversation_takeover.create"]
    )

    response = _write(narrow, narrow_headers, conversation_id, frame_id, {"a": "b"})

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_the_operator_can_write_slots_and_a_viewer_cannot(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    del settings, seeded_database
    from fastagent_v2.runtime.system_roles import system_role_permissions

    roles = system_role_permissions()

    assert "conversation_slot.update" in roles["operator"]
    assert "conversation_slot.update" in roles["owner"]
    assert "conversation_slot.update" not in roles["viewer"]
