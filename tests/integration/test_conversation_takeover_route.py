"""B3 #6 `POST /conversations/{id}/takeover` (conversation-engine §8, ADR-0054).

**The three cases that carry this batch are
`test_taking_over_suspends_the_stack_and_bumps_the_generation`,
`test_a_claimed_timer_is_invalidated_too` and
`test_a_second_takeover_is_a_conflict`.** §8 names the transition in one
sentence -- generation++, invalidate pending events, stop autonomous sending --
and those three are the parts that can be got wrong without any test noticing.
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


def _seed(seeded: dict[str, Any], *, tenant_key: str = "tenant_a") -> tuple[UUID, UUID]:
    """A conversation the engine could have produced, with one active frame."""

    tenant_id = seeded[tenant_key]
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
        aggregate["generation"] = 3
        aggregate["state_version"] = 5
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


def _timer(
    seeded: dict[str, Any], conversation_id: UUID, frame_id: UUID, *, status: str
) -> UUID:
    timer_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded["tenant_a"],),
        ).fetchone()
        assert release is not None
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id, status
            ) VALUES (%s, %s, %s, now() + interval '1 hour', 'silence_check', %s,
                      %s, 3, %s, 0, %s, %s)
            """,
            (
                seeded["tenant_a"],
                timer_id,
                conversation_id,
                str(uuid4()),
                release["id"],
                frame_id,
                uuid4(),
                status,
            ),
        )
        connection.commit()
    return timer_id


def _aggregate_row(conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT generation, state_version, control_mode, target_stack"
            "  FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return row


def _takeover(client: TestClient, headers: dict[str, str], conversation_id: Any) -> Any:
    return client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def test_taking_over_suspends_the_stack_and_bumps_the_generation(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**§8's sentence, checked clause by clause.**

    「人工接管 transition 必须 generation++、invalidate pending events、阻止自主
    生成与发送」. The third clause is enforced by `control_mode` -- the
    pre-generation and pre-delivery hooks read it -- so what this route has to
    get right is the first two plus the suspend.
    """

    conversation_id, frame_id = _seed(seeded_database)
    timer = _timer(seeded_database, conversation_id, frame_id, status="PENDING")
    client, headers = _client_for(settings, seeded_database, ["conversation_takeover.create"])

    response = _takeover(client, headers, conversation_id)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["result"]["control_mode"] == "human"
    assert body["result"]["generation"] == 4
    assert body["result"]["frames_suspended"] >= 1
    assert body["result"]["events_invalidated"] == 1

    row = _aggregate_row(conversation_id)
    assert row["control_mode"] == "human"
    assert row["generation"] == 4
    # CAS: the write went in against the version it read.
    assert row["state_version"] == 6
    assert {frame["status"] for frame in row["target_stack"]} == {"suspended"}
    with _migration_connection() as connection:
        stopped = connection.execute(
            "SELECT status, invalidation_reason FROM scheduled_events WHERE id = %s", (timer,)
        ).fetchone()
    assert stopped is not None
    assert stopped["status"] == "INVALIDATED"
    assert stopped["invalidation_reason"] == "CONVERSATION_TAKEN_OVER"


def test_a_claimed_timer_is_invalidated_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The clause that is easy to get half right.**

    A timer a worker has already leased (`CLAIMED`) is exactly as stale as one
    still waiting: it was minted against a plan a person has now replaced. The
    repin path invalidates both statuses for the same reason, and a route that
    filtered on `PENDING` alone would pass every other case in this file.
    """

    conversation_id, frame_id = _seed(seeded_database)
    pending = _timer(seeded_database, conversation_id, frame_id, status="PENDING")
    claimed = _timer(seeded_database, conversation_id, frame_id, status="CLAIMED")
    client, headers = _client_for(settings, seeded_database, ["conversation_takeover.create"])

    response = _takeover(client, headers, conversation_id)

    assert response.status_code == 200, response.text
    assert response.json()["result"]["events_invalidated"] == 2
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT id, status FROM scheduled_events WHERE id = ANY(%s)",
            ([pending, claimed],),
        ).fetchall()
    assert {row["status"] for row in rows} == {"INVALIDATED"}


def test_a_second_takeover_is_a_conflict(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409, and the reason is not politeness.

    A second takeover would bump the generation again and invalidate another
    round of timers. Answering 200 and doing nothing would be a lie about what
    happened; answering 200 and repeating the work would be worse.
    """

    conversation_id, _ = _seed(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_takeover.create"])

    first = _takeover(client, headers, conversation_id)
    generation = _aggregate_row(conversation_id)["generation"]
    second = _takeover(client, headers, conversation_id)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "CONVERSATION_ALREADY_HUMAN"
    assert _aggregate_row(conversation_id)["generation"] == generation


def test_the_takeover_is_recorded(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Who took which conversation, and how much stopped when they did."""

    conversation_id, frame_id = _seed(seeded_database)
    _timer(seeded_database, conversation_id, frame_id, status="PENDING")
    client, headers = _client_for(settings, seeded_database, ["conversation_takeover.create"])

    assert _takeover(client, headers, conversation_id).status_code == 200

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT actor_id, resource_id, reason_code, evidence FROM audit_events"
            " WHERE action = 'conversation_takeover.create'",
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["actor_id"] == UUID(str(seeded_database["employee_a"]))
    assert rows[0]["resource_id"] == conversation_id
    assert rows[0]["reason_code"] == "CONVERSATION_TAKEN_OVER"
    assert rows[0]["evidence"]["events_invalidated"] == 1


def test_an_unknown_or_other_tenants_conversation_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    theirs, _ = _seed(seeded_database, tenant_key="tenant_b")
    client, headers = _client_for(settings, seeded_database, ["conversation_takeover.create"])

    across = _takeover(client, headers, theirs)
    absent = _takeover(client, headers, uuid4())

    assert across.status_code == 404, across.text
    assert absent.status_code == 404
    assert across.json()["code"] == absent.json()["code"] == "CONVERSATION_NOT_FOUND"
    # The other tenant's conversation is untouched -- RLS, not a filter this
    # route wrote.
    assert _aggregate_row(theirs)["control_mode"] == "ai"


def test_taking_over_needs_the_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    conversation_id, _ = _seed(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = _takeover(client, headers, conversation_id)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"
    assert _aggregate_row(conversation_id)["control_mode"] == "ai"


def test_the_operator_role_is_the_one_that_can_take_over(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`operator` exists for this; `viewer` reads only.

    Asserted because the three roles are recomputed from the catalogue on every
    deployment, and this route is the reason `conversation_takeover` is a
    command resource of its own (K1).
    """

    del settings, seeded_database
    from fastagent_v2.runtime.system_roles import system_role_permissions

    roles = system_role_permissions()

    assert "conversation_takeover.create" in roles["operator"]
    assert "conversation_takeover.create" in roles["owner"]
    assert "conversation_takeover.create" not in roles["viewer"]
