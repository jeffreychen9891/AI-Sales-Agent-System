"""B3 #16 / #15 `POST` and `GET /conversations/{id}/manual-tasks` (K18).

**The case that carries this batch is
`test_one_list_shows_the_engines_task_and_the_persons_task`.** The freeze note
for #15 is 「复用人工任务状态机，不建第二套」, and the way that sentence fails is
not with an error -- it is with a reader that quietly shows one producer's rows
and looks complete. So the list is asked to answer with both, and the engine's
row is written the way `034` says the engine writes one: no owner, no SLA.
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
from fastagent_v2.runtime.system_roles import OPERATOR_ACTIONS, VIEWER_EXCLUSIONS
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

WORKING = ["manual_task.create", "manual_task.list"]

#: What the customer said, written down by the person filing the task. A value,
#: so it must not reach `audit_events` (ADR-0055 §二).
DICTATED_NOTE = "customer says the parcel arrived crushed"


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


def _seed_conversation(seeded: dict[str, Any]) -> UUID:
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


def _engine_filed_task(*, tenant_id: Any, conversation_id: UUID) -> UUID:
    """A task the way `pipeline.py` writes one: classified, and nothing else.

    No owner, no SLA, no expiry -- that is not this fixture being lazy, it is
    what the `manual_review` hook can honestly write (034 §T91). A fixture that
    filled them in would be seeding a row production cannot produce.
    """

    task_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO manual_tasks(
                tenant_id, id, conversation_id, task_class, notify, reason_code, evidence
            ) VALUES (%s, %s, %s, 'merchant_action', 'batched', 'DELIVERY_UNCONFIRMED', %s)
            """,
            (tenant_id, task_id, conversation_id, Jsonb({"outbox_id": str(uuid4())})),
        )
        connection.commit()
    return task_id


def _file(
    client: TestClient,
    headers: dict[str, str],
    conversation_id: UUID,
    seeded: dict[str, Any],
    **overrides: Any,
) -> Any:
    body: dict[str, Any] = {
        "owner_employee_id": str(seeded["employee_a"]),
        "sla_due_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
        "reason_code": "CUSTOMER_NEEDS_A_CALLBACK",
        "evidence": {"note": DICTATED_NOTE},
    }
    body.update(overrides)
    return client.post(
        f"/conversations/{conversation_id}/manual-tasks",
        json=body,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def test_a_person_files_a_task_with_an_owner_and_a_deadline(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #16: owner / SLA / evidence required, and actually stored.

    The columns are nullable (034) because the engine cannot fill them. This
    asserts the other half of that ruling: when a person files the task, all
    three are on the row.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)

    response = _file(client, headers, conversation_id, seeded_database)
    assert response.status_code == 201, response.text
    task_id = UUID(response.json()["resource_ref"]["resource_id"])

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT * FROM manual_tasks WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], task_id),
        ).fetchone()
    assert row is not None
    assert row["status"] == "open"
    assert row["owner_employee_id"] == seeded_database["employee_a"]
    assert row["sla_due_at"] is not None
    assert row["evidence"] == {"note": DICTATED_NOTE}
    # Not from the body, and neither is offered there: a merchant's employee
    # cannot classify their own work as the platform's, and a task somebody
    # filed themselves is not news to be pushed back at them.
    assert row["task_class"] == "merchant_action"
    assert row["notify"] == "none"
    # The engine's column, empty: nothing proposed this.
    assert row["intent_id"] is None


def test_the_audit_row_names_the_evidence_without_copying_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0055 §二 applied a second time, and it is why `evidence_keys` exists.

    `audit_events` is append-only. What a person typed into `evidence` is what a
    customer told them, so a copy taken here could never be withdrawn -- while
    "who filed it, for whom, by when, and which fields they filled" is what
    makes the act reviewable.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)
    response = _file(client, headers, conversation_id, seeded_database)
    assert response.status_code == 201, response.text
    task_id = response.json()["resource_ref"]["resource_id"]

    with _migration_connection() as connection:
        audit = connection.execute(
            """
            SELECT actor_id, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'manual_task.create' AND resource_id = %s
            """,
            (seeded_database["tenant_a"], task_id),
        ).fetchone()
    assert audit is not None
    assert audit["actor_id"] == seeded_database["employee_a"]
    assert audit["evidence"]["evidence_keys"] == ["note"]
    assert audit["evidence"]["owner_employee_id"] == str(seeded_database["employee_a"])
    assert DICTATED_NOTE not in str(audit["evidence"])


def test_one_list_shows_the_engines_task_and_the_persons_task(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #15: 「复用人工任务状态机，不建第二套」.

    **The failure this rules out is a silent one.** Both producers write the
    same table by K18's ruling; a reader that filtered to one of them would
    look complete and show half a backlog. So the engine's row is seeded the
    way the engine writes one, a person files another, and the list must carry
    both -- with the engine's three T91 fields answered `null` rather than
    omitted, because "nobody is on this" is the fact the column exists to make
    visible.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)
    engine_task = _engine_filed_task(
        tenant_id=seeded_database["tenant_a"], conversation_id=conversation_id
    )
    filed = _file(client, headers, conversation_id, seeded_database)
    assert filed.status_code == 201, filed.text
    person_task = filed.json()["resource_ref"]["resource_id"]

    response = client.get(f"/conversations/{conversation_id}/manual-tasks", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {item["task_id"] for item in items} == {str(engine_task), person_task}

    by_id = {item["task_id"]: item for item in items}
    engine = by_id[str(engine_task)]
    assert engine["owner_employee_id"] is None
    assert engine["sla_due_at"] is None
    assert engine["expires_at"] is None
    assert engine["notify"] == "batched"
    assert engine["status"] == "open"

    person = by_id[person_task]
    assert person["owner_employee_id"] == str(seeded_database["employee_a"])
    assert person["sla_due_at"] is not None
    assert person["evidence"]["note"] == DICTATED_NOTE


def test_another_tenants_conversation_is_not_found_from_either_route(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS-MANUAL-TASK-001 / -002: the same 404 for absent and for elsewhere.

    A conversation belonging to tenant B is invisible under tenant A's session,
    so both routes answer exactly as they do for an id that never existed. A
    distinguishable answer would be a cross-tenant existence oracle.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    tenant_b_conversation = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_b"],),
        ).fetchone()
        assert release is not None
        aggregate = _initial_aggregate(
            tenant_id=seeded_database["tenant_b"],
            conversation_id=tenant_b_conversation,
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
                tenant_b_conversation,
                release["id"],
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.commit()

    never_existed = uuid4()
    assert client.get(
        f"/conversations/{tenant_b_conversation}/manual-tasks", headers=headers
    ).status_code == 404
    assert client.get(
        f"/conversations/{never_existed}/manual-tasks", headers=headers
    ).status_code == 404
    assert (
        _file(client, headers, tenant_b_conversation, seeded_database).status_code == 404
    )
    assert _file(client, headers, never_existed, seeded_database).status_code == 404


def test_an_owner_from_another_tenant_cannot_be_given_the_task(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The composite foreign key's reason, reached through the route.

    034 tests the constraint; this tests that the route never gets far enough to
    need it -- tenant B's employee is simply not there under tenant A's session,
    and the answer is the same 404 a random uuid gets.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)

    foreign = _file(
        client,
        headers,
        conversation_id,
        seeded_database,
        owner_employee_id=str(seeded_database["employee_b"]),
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "OWNER_NOT_FOUND"

    with _migration_connection() as connection:
        count = connection.execute(
            "SELECT count(*) AS n FROM manual_tasks WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()["n"]
    assert count == 0, "a refused filing must leave no row"


def test_a_deadline_already_past_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """An SLA in the past is not a deadline.

    Accepting it would make a task that is born late indistinguishable, in every
    report, from one that somebody missed.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)
    response = _file(
        client,
        headers,
        conversation_id,
        seeded_database,
        sla_due_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "SLA_DUE_AT_IS_IN_THE_PAST"


def test_prose_in_the_reason_code_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The column is a code column, and the digest groups by it.

    A sentence here would put two vocabularies in one column; the sentence
    belongs in `evidence`, which is what `evidence` is for.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed_conversation(seeded_database)
    response = _file(
        client,
        headers,
        conversation_id,
        seeded_database,
        reason_code="customer needs a callback",
    )
    assert response.status_code == 422, response.text


def test_neither_route_answers_without_its_own_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Reading the backlog and adding to it are two grants, not one.

    The employee below can list and cannot file, which is the shape a support
    lead handing out permissions will actually want -- and it fails closed the
    other way round too, since neither route falls back to the other's action.
    """

    client, headers = _client_for(settings, seeded_database, ["manual_task.list"])
    conversation_id = _seed_conversation(seeded_database)
    _engine_filed_task(tenant_id=seeded_database["tenant_a"], conversation_id=conversation_id)

    listed = client.get(f"/conversations/{conversation_id}/manual-tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    assert _file(client, headers, conversation_id, seeded_database).status_code == 403


def test_the_backlog_read_is_not_a_viewers(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`manual_task.list` is excluded from `viewer` and enumerated for `operator`.

    **The criterion, not a preference**: the list returns `evidence`, and
    `evidence` is where a person writes what the customer said. That is the
    first bullet of the exclusion rule word for word -- the same sentence that
    kept `message.list` out (B3 #3).

    Asserted against the role sets rather than through a login, because the
    hazard is a role *growing by derivation*: `viewer` is every read minus these
    exclusions, so a read added without a decision walks in silently (T114).
    """

    assert "manual_task.list" in VIEWER_EXCLUSIONS
    assert "manual_task.list" in OPERATOR_ACTIONS
    assert "manual_task.create" in OPERATOR_ACTIONS
    # **This clause was "nothing is pre-granted for B3 #17-#20" when this batch
    # was written, and the next batch built them.** It fired on the first run
    # after theirs, which is what it was for: the two verbs an operator got
    # (`claim`, and resolving through the command resource) arrived with a
    # decision attached, and the two they did not are still refused.
    #
    # **Closed, not a list of names** (independent review 2026-09-06, P3-2).
    # The first repair enumerated the two exclusions, which quietly retired the
    # tripwire: a *new* manual-task action -- the producer `expired` still
    # lacks, for one -- would have walked into `operator` without anybody
    # answering for it. That is T174's shape, and this file is the wrong place
    # to reproduce it. The set is stated exactly, so the next verb has to be
    # answered here or the test says so.
    assert {
        action for action in OPERATOR_ACTIONS if action.startswith("manual_task")
    } == {
        "manual_task.list",
        "manual_task.create",
        "manual_task.claim",
        "manual_task_resolution.create",
    }
