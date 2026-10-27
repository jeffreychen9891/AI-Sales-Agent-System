"""B3 #17-#20: claim / resolve / reassign / cancel (K18, migration 036).

**The case that carries this batch is
`test_two_people_cannot_both_claim_the_same_task`.** The other seven check that
a verb moves the row where it says it does; that one checks the thing the row
cannot tell you afterwards -- two agents clicking "claim" on the same unowned
task read `open` at the same instant, and without `FOR UPDATE` the second write
wins in silence while the first person keeps working a task the record says
belongs to somebody else.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import OPERATOR_ACTIONS
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

WORKING = [
    "manual_task.claim",
    "manual_task_resolution.create",
    "manual_task_reassignment.create",
    "manual_task.cancel",
]

RESOLUTION_NOTE = "called the customer back and arranged a replacement"
REASSIGN_REASON = "the assignee is on leave until Monday"
CANCEL_REASON = "the customer withdrew the complaint"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings,
    seeded: dict[str, Any],
    permissions: list[str],
    *,
    employee_id: Any = None,
    email: str = "operator-a@example.com",
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), seeded["tenant_a"], employee_id or seeded["employee_a"]),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": email,
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _colleague(seeded: dict[str, Any]) -> UUID:
    """A second employee of tenant A, sharing the seeded password.

    The fixture seeds one employee per tenant, and half of this batch is about
    two people and one task -- who claimed it, who it was taken from, who it
    went to. The row is built by copying the seeded hash rather than by hashing
    again: argon2 costs a tenth of a second and this is not what is being
    tested. Every other column is written the way `POST /employees` writes it.
    """

    employee_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            )
            SELECT tenant_id, %s, 'operator-a2@example.com', password_hash,
                   '[]'::jsonb, 'Operator A2'
              FROM employees WHERE tenant_id = %s AND id = %s
            """,
            (employee_id, seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
    return employee_id


def _task(
    seeded: dict[str, Any],
    *,
    owner: Any = None,
    status: str = "open",
    claimed: bool = False,
) -> UUID:
    """A task in a named state, written the way its producer would write it.

    `resolved_at` is filled in when the state is `resolved` because `034`
    refuses the pair otherwise -- the fixture cannot seed a state the routes
    could not produce, which is the point of having the constraint.
    """

    task_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO manual_tasks(
                tenant_id, id, conversation_id, status, task_class, notify,
                reason_code, owner_employee_id, sla_due_at, claimed_at,
                resolved_at, evidence
            ) VALUES (%s, %s, %s, %s, 'merchant_action', 'batched',
                      'DELIVERY_UNCONFIRMED', %s, %s, %s, %s, %s)
            """,
            (
                seeded["tenant_a"],
                task_id,
                uuid4(),
                status,
                owner,
                datetime.now(UTC) + timedelta(hours=4) if owner is not None else None,
                datetime.now(UTC) if claimed else None,
                datetime.now(UTC) if status == "resolved" else None,
                Jsonb({"outbox_id": str(uuid4())}),
            ),
        )
        connection.commit()
    return task_id


def _row(task_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute("SELECT * FROM manual_tasks WHERE id = %s", (task_id,)).fetchone()
    assert row is not None
    return row


def _audit(task_id: UUID) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT action, resource_type, actor_id, evidence FROM audit_events"
            " WHERE resource_id = %s ORDER BY created_at",
            (task_id,),
        ).fetchall()


def _post(
    client: TestClient, headers: dict[str, str], task_id: UUID, verb: str, **body: Any
) -> Any:
    return client.post(
        f"/manual-tasks/{task_id}/{verb}",
        json=body or None,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def test_claiming_an_unowned_task_makes_the_claimer_its_owner(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #17, on the row the engine actually produces.

    An engine-filed task has no owner (034 §T91). This is the case the verb was
    drawn for: the first person to pick it up becomes the person it belongs to,
    and `claimed_at` is written in the same statement because `034`'s constraint
    will not store one without the other.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database)

    response = _post(client, headers, task_id, "claim")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "claimed"

    row = _row(task_id)
    assert row["status"] == "claimed"
    assert row["owner_employee_id"] == seeded_database["employee_a"]
    assert row["claimed_at"] is not None

    audit = _audit(task_id)
    assert [entry["action"] for entry in audit] == ["manual_task.claim"]
    assert audit[0]["actor_id"] == seeded_database["employee_a"]


def test_two_people_cannot_both_claim_the_same_task(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`FOR UPDATE`, measured by racing two real requests at one row.

    **This is the assertion the batch is built around.** Both callers read the
    task as `open`; one of them must lose. Without the row lock both would write
    themselves in and the second would win silently -- and nothing afterwards
    could tell you it had happened, because the row only remembers the last
    writer.

    The two callers are two different employees, so "who owns it" has a
    checkable answer either way.
    """

    colleague = _colleague(seeded_database)
    client_a, headers_a = _client_for(settings, seeded_database, WORKING)
    client_b, headers_b = _client_for(
        settings,
        seeded_database,
        WORKING,
        employee_id=colleague,
        email="operator-a2@example.com",
    )
    task_id = _task(seeded_database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_post, client_a, headers_a, task_id, "claim")
        second = pool.submit(_post, client_b, headers_b, task_id, "claim")
        results = sorted(
            (first.result().status_code, second.result().status_code),
        )

    assert results == [200, 409], "exactly one claim may win"
    row = _row(task_id)
    assert row["status"] == "claimed"
    assert row["owner_employee_id"] in {seeded_database["employee_a"], colleague}
    # One winner, one audit row: the loser wrote nothing at all.
    assert len(_audit(task_id)) == 1


def test_a_task_assigned_to_somebody_else_cannot_be_claimed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """B3 #16 files a task *with* an owner, so `open` does not mean "free".

    Letting a second person claim it would be a reassignment with no record that
    one happened -- which is the entire job of B3 #19.
    """

    colleague = _colleague(seeded_database)
    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database, owner=colleague)

    response = _post(client, headers, task_id, "claim")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MANUAL_TASK_BELONGS_TO_SOMEBODY_ELSE"
    assert _row(task_id)["owner_employee_id"] == colleague


def test_resolving_records_what_was_done_on_the_task_and_not_in_the_audit(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #18, plus the note requirement this batch added.

    The note lands in `evidence` beside what the engine put there -- `||` merges,
    so the outbox id it was filed with survives. It is deliberately **not** copied
    into `audit_events`: that table cannot be edited, and this is free text
    written about a customer (the rule B3 #16 and #25 both applied).
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database, owner=seeded_database["employee_a"], status="open")

    response = _post(client, headers, task_id, "resolve", note=RESOLUTION_NOTE)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "resolved"

    row = _row(task_id)
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None
    assert row["evidence"]["resolution"] == RESOLUTION_NOTE
    assert "outbox_id" in row["evidence"], "merged, not replaced"

    audit = _audit(task_id)
    assert audit[0]["action"] == "manual_task_resolution.create"
    assert audit[0]["resource_type"] == "manual_task_resolution"
    assert RESOLUTION_NOTE not in str(audit[0]["evidence"])


def test_a_short_note_is_refused(settings: Settings, seeded_database: dict[str, Any]) -> None:
    """Eight characters, the floor `POST /employees/{id}/purge` uses.

    "ok" satisfies a required field and answers nothing, and a resolved task
    whose account is "ok" is the shape T91 registered at the other end.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database)
    assert _post(client, headers, task_id, "resolve", note="ok").status_code == 422
    assert _row(task_id)["status"] == "open"


def test_reassigning_returns_the_task_to_open_and_names_both_ends(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #19: the owner moves, the claim does not travel with it.

    The new owner has not claimed the task -- they may not know about it yet --
    so `claimed_at` is cleared with the status. Keeping it would leave a
    `claimed` row carrying somebody else's timestamp, which `034`'s constraint
    would store quite happily because it only checks that the pair is present.

    **The SLA does not move.** `036` grants `owner_employee_id` and withholds
    `sla_due_at` precisely so that handing work around cannot reset a deadline.
    """

    colleague = _colleague(seeded_database)
    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(
        seeded_database, owner=seeded_database["employee_a"], status="claimed", claimed=True
    )
    before = _row(task_id)["sla_due_at"]

    response = _post(
        client,
        headers,
        task_id,
        "reassign",
        owner_employee_id=str(colleague),
        reason=REASSIGN_REASON,
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "open"

    row = _row(task_id)
    assert row["status"] == "open"
    assert row["owner_employee_id"] == colleague
    assert row["claimed_at"] is None
    assert row["sla_due_at"] == before

    evidence = _audit(task_id)[0]["evidence"]
    assert evidence["previous_owner_employee_id"] == str(seeded_database["employee_a"])
    assert evidence["owner_employee_id"] == str(colleague)
    assert evidence["reason"] == REASSIGN_REASON


def test_reassigning_to_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The same 404 an unknown id gets, and the row is untouched.

    The composite foreign key is what makes a cross-tenant owner impossible;
    this asserts the route never reaches it, and answers without revealing that
    the id means something somewhere else.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database)

    response = _post(
        client,
        headers,
        task_id,
        "reassign",
        owner_employee_id=str(seeded_database["employee_b"]),
        reason=REASSIGN_REASON,
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "OWNER_NOT_FOUND"
    assert _row(task_id)["owner_employee_id"] is None


def test_cancelling_keeps_the_row_and_the_reason(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #20: 「审计事实不物理删除」.

    The row survives, which is the whole reason the verb is `cancel` rather than
    `DELETE` -- and `036` never grants `DELETE`, so this route could not have
    removed it even if it wanted to. The reason is what makes the surviving row
    worth having.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    task_id = _task(seeded_database, owner=seeded_database["employee_a"])

    response = _post(client, headers, task_id, "cancel", reason=CANCEL_REASON)
    assert response.status_code == 200, response.text

    row = _row(task_id)
    assert row["status"] == "cancelled"
    assert row["owner_employee_id"] == seeded_database["employee_a"], "who had it is still recorded"
    assert _audit(task_id)[0]["evidence"]["reason"] == CANCEL_REASON


def test_a_closed_task_refuses_every_verb(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Resolved and cancelled are terminal, and all four verbs say so alike.

    A second resolve would move `resolved_at` and quietly rewrite when the work
    was finished; a cancel after a resolve would erase that it ever was.
    """

    colleague = _colleague(seeded_database)
    client, headers = _client_for(settings, seeded_database, WORKING)
    for terminal in ("resolved", "cancelled"):
        task_id = _task(
            seeded_database,
            owner=seeded_database["employee_a"],
            status=terminal,
        )
        assert _post(client, headers, task_id, "claim").status_code == 409
        assert _post(client, headers, task_id, "resolve", note=RESOLUTION_NOTE).status_code == 409
        assert (
            _post(
                client,
                headers,
                task_id,
                "reassign",
                owner_employee_id=str(colleague),
                reason=REASSIGN_REASON,
            ).status_code
            == 409
        )
        assert _post(client, headers, task_id, "cancel", reason=CANCEL_REASON).status_code == 409
        assert _row(task_id)["status"] == terminal


def test_another_tenants_task_is_not_found_by_any_verb(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS-MANUAL-TASK-003..006: absent and elsewhere answer identically."""

    client, headers = _client_for(settings, seeded_database, WORKING)
    foreign = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO manual_tasks(
                tenant_id, id, conversation_id, task_class, notify, reason_code
            ) VALUES (%s, %s, %s, 'merchant_action', 'batched', 'DELIVERY_UNCONFIRMED')
            """,
            (seeded_database["tenant_b"], foreign, uuid4()),
        )
        connection.commit()
    absent = uuid4()

    for task_id in (foreign, absent):
        assert _post(client, headers, task_id, "claim").status_code == 404
        assert _post(client, headers, task_id, "resolve", note=RESOLUTION_NOTE).status_code == 404
        assert _post(client, headers, task_id, "cancel", reason=CANCEL_REASON).status_code == 404
    assert _row(foreign)["status"] == "open", "tenant B's task is untouched"


def test_each_verb_needs_its_own_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Four grants, not one -- which is why two of them are command resources.

    K1's criterion is whether the permission needs granting on its own. This is
    that criterion turned into a test: an employee holding only `manual_task.claim`
    can claim and can do nothing else, so `resolve` cannot be reached by
    somebody granted `claim`.
    """

    colleague = _colleague(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["manual_task.claim"])
    task_id = _task(seeded_database)

    assert _post(client, headers, task_id, "claim").status_code == 200
    assert _post(client, headers, task_id, "resolve", note=RESOLUTION_NOTE).status_code == 403
    assert (
        _post(
            client,
            headers,
            task_id,
            "reassign",
            owner_employee_id=str(colleague),
            reason=REASSIGN_REASON,
        ).status_code
        == 403
    )
    assert _post(client, headers, task_id, "cancel", reason=CANCEL_REASON).status_code == 403
    assert _row(task_id)["status"] == "claimed", "only the permitted verb moved it"


def test_operator_works_tasks_but_does_not_reassign_or_cancel_them() -> None:
    """The role split, asserted against the set rather than through a login.

    Working a task -- taking it, finishing it -- is what an operator does.
    Reassigning and cancelling are decisions about somebody else's queue, so a
    lead is granted them by name. `operator` is enumerated precisely so this is
    a decision and not a derivation.
    """

    assert "manual_task.claim" in OPERATOR_ACTIONS
    assert "manual_task_resolution.create" in OPERATOR_ACTIONS
    assert "manual_task_reassignment.create" not in OPERATOR_ACTIONS
    assert "manual_task.cancel" not in OPERATOR_ACTIONS
