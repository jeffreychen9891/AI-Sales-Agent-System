"""B3 #24 + #7: the two-phase hand-back (ADR-0054 §三.4).

**The three cases that carry this batch are
`test_resuming_puts_the_stack_back_to_work`,
`test_the_state_document_learns_the_takeover_ended` and
`test_a_digest_from_before_a_change_is_refused`.** The first is T4 -- the bug
where `resume` answered 200 and left every frame suspended; the second is the
half of ADR-0054's write (1) that the column alone does not cover; the third is
what makes the two phases a compare-and-set rather than a ceremony.

**The setup goes through `POST .../takeover`** rather than hand-seeding
`control_mode = 'human'`: these two routes are each other's other half, and a
fixture that invented the intermediate state could not catch a takeover and a
resume that disagree about what it looks like.
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
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

#: What an agent working conversations holds. Three actions, because taking
#: over, previewing and handing back are three decisions somebody grants.
WORKING = [
    "conversation_takeover.create",
    "conversation_resume.create",
    "conversation_aggregate.read",
]


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


def _seed(seeded: dict[str, Any]) -> UUID:
    tenant_id = seeded["tenant_a"]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        profile = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
            profile_id="synthetic-service-desk", version="1.0.4"
        ).document
        aggregate = _initial_aggregate(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            profile_release_id=release["id"],
            profile=profile,
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


def _row(conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT generation, state_version, control_mode, target_stack, state"
            "  FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return row


def _held_by_a_person(client: TestClient, headers: dict[str, str], seeded: dict[str, Any]) -> UUID:
    conversation_id = _seed(seeded)
    taken = client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert taken.status_code == 200, taken.text
    return conversation_id


def _preview(client: TestClient, headers: dict[str, str], conversation_id: UUID) -> Any:
    return client.get(f"/conversations/{conversation_id}/resume-preview", headers=headers)


def _resume(
    client: TestClient, headers: dict[str, str], conversation_id: UUID, digest: str
) -> Any:
    return client.post(
        f"/conversations/{conversation_id}/resume",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"acknowledged_change_digest": digest},
    )


def test_the_preview_shows_the_takeover_period_and_hands_back_a_digest(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Phase one. The digest is stable while nothing changes -- otherwise the
    second phase could never succeed."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)

    first = _preview(client, headers, conversation_id)
    second = _preview(client, headers, conversation_id)

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["control_mode"] == "human"
    assert "slots_written" in body["takeover_projection"]
    assert len(body["change_digest"]) == 64
    assert second.json()["change_digest"] == body["change_digest"]
    assert first.headers["cache-control"] == "no-store, private"


def test_resuming_puts_the_stack_back_to_work(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T4.** `suspend_target_stack_for_human` had existed since takeover
    landed and nothing had ever undone it: `resume` answered 200, `control_mode`
    went back to `ai`, and every frame stayed `suspended` -- after which `keep`
    and `push` are refused forever, silently.

    So the assertion that matters is not the 200. It is that no frame is left
    suspended and at least one is active again.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)
    before = _row(conversation_id)
    digest = _preview(client, headers, conversation_id).json()["change_digest"]

    response = _resume(client, headers, conversation_id, digest)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["result"]["control_mode"] == "ai"
    assert body["result"]["frames_active"] >= 1
    assert body["result"]["generation"] == before["generation"] + 1

    row = _row(conversation_id)
    assert row["control_mode"] == "ai"
    statuses = {frame["status"] for frame in row["target_stack"]}
    assert "suspended" not in statuses, "T4: resume left the stack suspended"
    assert "active" in statuses


def test_the_state_document_learns_the_takeover_ended(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0054's write (1), the half a column alone does not cover.**

    The worker reads `aggregate["control_mode"]` out of the `state` document
    (`pipeline.py:750`), not out of the column. A resume that wrote only the
    column would answer 200 while the engine kept believing a person still held
    the conversation -- and would refuse to generate, forever.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)
    digest = _preview(client, headers, conversation_id).json()["change_digest"]

    assert _resume(client, headers, conversation_id, digest).status_code == 200

    row = _row(conversation_id)
    assert row["control_mode"] == "ai"
    assert row["state"]["control_mode"] == "ai", "the worker reads the document, not the column"


def test_a_digest_from_before_a_change_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**What makes the two phases a compare-and-set.**

    The operator previews, a customer message lands, and the picture they were
    shown is no longer the picture. The digest covers `state_version` precisely
    so that *any* engine write invalidates it -- not only the ones that happen
    to change a field the projection renders.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)
    stale = _preview(client, headers, conversation_id).json()["change_digest"]
    with _migration_connection() as connection:
        # The engine's own kind of write: the version moves, the projection may
        # not. Both must invalidate the token.
        connection.execute(
            "UPDATE conversation_aggregates SET state_version = state_version + 1"
            " WHERE id = %s",
            (conversation_id,),
        )
        connection.commit()

    response = _resume(client, headers, conversation_id, stale)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CHANGE_DIGEST_STALE"
    assert _row(conversation_id)["control_mode"] == "human", "a refused resume changed something"


def test_a_conversation_no_person_holds_cannot_be_resumed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409. Resuming an `ai` conversation would bump the generation and
    un-suspend nothing -- a write with no meaning."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)
    digest = _preview(client, headers, conversation_id).json()["change_digest"]

    response = _resume(client, headers, conversation_id, digest)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CONVERSATION_NOT_HELD_BY_A_PERSON"


def test_old_timers_are_not_resurrected(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0054's write (3): 「旧 timer 不复活」.

    The takeover invalidated them because they were minted against a plan a
    person replaced. Resuming does not undo that -- the engine schedules new
    ones from the resumed stack.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_a"],),
        ).fetchone()
        frame = _row(conversation_id)["target_stack"][0]["id"]
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id
            ) VALUES (%s, %s, %s, now() + interval '1 hour', 'silence_check', %s, %s, 0, %s, 0, %s)
            """,
            (
                seeded_database["tenant_a"],
                uuid4(),
                conversation_id,
                str(uuid4()),
                release["id"],
                frame,
                uuid4(),
            ),
        )
        connection.commit()
    taken = client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert taken.status_code == 200, taken.text
    digest = _preview(client, headers, conversation_id).json()["change_digest"]

    resumed = _resume(client, headers, conversation_id, digest)

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["result"]["timers_restored"] == 0
    with _migration_connection() as connection:
        statuses = connection.execute(
            "SELECT status FROM scheduled_events WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchall()
    assert {row["status"] for row in statuses} == {"INVALIDATED"}


def test_the_hand_back_records_what_was_acknowledged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The digest is a hash of state, not state -- and recording it is the only
    way "what did they say they had read" stays answerable."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)
    digest = _preview(client, headers, conversation_id).json()["change_digest"]

    assert _resume(client, headers, conversation_id, digest).status_code == 200

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT actor_id, reason_code, evidence FROM audit_events"
            " WHERE action = 'conversation_resume.create'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["actor_id"] == UUID(str(seeded_database["employee_a"]))
    assert rows[0]["reason_code"] == "CONVERSATION_RESUMED"
    assert rows[0]["evidence"]["acknowledged_change_digest"] == digest


def test_resuming_needs_its_own_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Taking over and handing back are different actions (K1's command
    resources), so holding one does not grant the other."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _held_by_a_person(client, headers, seeded_database)
    digest = _preview(client, headers, conversation_id).json()["change_digest"]
    narrow, narrow_headers = _client_for(
        settings, seeded_database, ["conversation_takeover.create", "conversation_aggregate.read"]
    )

    response = _resume(narrow, narrow_headers, conversation_id, digest)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"
    assert _row(conversation_id)["control_mode"] == "human"
