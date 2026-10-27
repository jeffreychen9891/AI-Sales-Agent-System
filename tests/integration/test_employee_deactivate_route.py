"""Route freeze B1 #18 (`C0A-021` + parity §6.4): deactivate, never delete.

parity §6.4 gives this route three obligations and one prohibition. The three:
revoke sessions immediately, record `disabled_at`, start the retention clock at
`+90d`. The prohibition: purge is a **different route, action and test set** --
"不得由一个布尔参数切换" -- which is why nothing here takes a flag.

The cases that carry the weight:

* `test_the_targets_session_stops_working_immediately` -- the obligation that
  makes deactivation mean something. A route that flips `status` and leaves the
  person logged in has disabled an account that still works.
* `test_a_second_deactivation_does_not_move_the_retention_clock` -- re-stamping
  `disabled_at` would quietly extend how long somebody's data is kept, and a
  retry is the most ordinary way to do it by accident.
* `test_the_audit_row_names_both_the_actor_and_the_subject` -- T117's whole
  point. This is the first route where the person ending a session is not the
  person whose session it is.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import hash_password
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


def _connection() -> Connection[Any]:
    return Connection.connect(_migration_url(), row_factory=dict_row)


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _seed_colleague(tenant_id: UUID, email: str = "colleague@example.com") -> UUID:
    """A second employee to deactivate -- the route refuses to do it to yourself."""

    employee_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            ) VALUES (%s, %s, %s, %s, %s, 'Colleague')
            """,
            (
                tenant_id,
                employee_id,
                email,
                hash_password("local-only-strong-password"),
                # `session.delete` as well as `session.read`: the colleague has
                # to be able to end their **own** session, which is what
                # `test_a_session_the_target_already_ended_is_not_re_revoked`
                # needs and what any employee can do. Without it that test's
                # logout answers 403 and the test fails before reaching what it
                # is about -- which is how it first went red in the gate.
                Jsonb(["session.read", "session.delete"]),
            ),
        )
        connection.commit()
    return employee_id


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded["tenant_a"])),
        employee_id=UUID(str(seeded["employee_a"])),
        permissions=permissions,
    )
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


def _login_as(client: TestClient, seeded: dict[str, Any], email: str) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": email,
            "password": seeded["login_password"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _deactivate(client: TestClient, headers: dict[str, str], employee_id: Any) -> Any:
    return client.post(
        f"/employees/{employee_id}/deactivate",
        headers={**headers, "Idempotency-Key": f"deactivate-{uuid4()}"},
    )


def _row(employee_id: UUID) -> dict[str, Any]:
    with _connection() as connection:
        return connection.execute(
            "SELECT status, disabled_at, purge_eligible_at, legal_hold"
            "  FROM employees WHERE id = %s",
            (employee_id,),
        ).fetchone()


def _audit_rows(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT actor_id, action, resource_type, resource_id, reason_code, evidence
              FROM audit_events WHERE resource_id = %s ORDER BY created_at
            """,
            (employee_id,),
        ).fetchall()


def test_deactivation_records_the_state_and_starts_the_retention_clock(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity §6.4: `disabled_at`, and `purge_eligible_at = +90d`."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _deactivate(client, headers, colleague)

    assert response.status_code == 200, response.text
    body = response.json()
    # **The whole body, and the receipt validated as a receipt** (ADR-0050 §二).
    # Picking six keys out of a body and feeding those to the contract is the
    # one way of writing this that cannot notice the defect it exists to catch:
    # it passes no matter what else the body carries.
    assert set(body) == {"receipt", "result"}
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["status"] == "succeeded"
    assert set(body["result"]) == {
        "employee_id",
        "status",
        "disabled_at",
        "purge_eligible_at",
        "sessions_revoked",
    }
    assert body["result"]["status"] == "disabled"
    stored = _row(colleague)
    assert stored["status"] == "disabled"
    assert stored["disabled_at"] is not None
    # 90 days, asserted as an interval rather than a literal date so the test
    # says what the rule is instead of restating today's clock.
    assert (stored["purge_eligible_at"] - stored["disabled_at"]).days == 90


def test_the_targets_session_stops_working_immediately(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A disabled account that still works is not disabled.

    §6.4 says "停用立即撤销 session". This logs the colleague in first, so there
    is a live session for the route to end -- otherwise the assertion would pass
    against a route that revokes nothing.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    theirs = _login_as(client, seeded_database, "colleague@example.com")
    assert client.get("/auth/me", headers=theirs).status_code == 200

    body = _deactivate(client, headers, colleague).json()

    assert body["result"]["sessions_revoked"] == 1
    assert client.get("/auth/me", headers=theirs).status_code == 401
    # **The other half of "disabled"** (independent review P3-1): revoking the
    # session they hold is worth nothing if they can log straight back in. The
    # `status != 'active'` check in `POST /auth/login` is B1 #6-era code and not
    # this batch's, but nothing anywhere pinned "deactivation actually reaches
    # it" -- so the day the login path is rewritten, this is what falls over
    # rather than any existing assertion.
    again = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "colleague@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert again.status_code == 401


def test_a_second_deactivation_does_not_move_the_retention_clock(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409, not a silent re-stamp.

    Re-stamping `disabled_at` would push `purge_eligible_at` out by however long
    the retry took -- quietly extending how long a person's data is kept, which
    is the opposite of what a retention clock is for.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    assert _deactivate(client, headers, colleague).status_code == 200
    first = _row(colleague)

    second = _deactivate(client, headers, colleague)

    assert second.status_code == 409
    assert second.json()["code"] == "EMPLOYEE_ALREADY_DISABLED"
    assert _row(colleague)["disabled_at"] == first["disabled_at"]
    assert _row(colleague)["purge_eligible_at"] == first["purge_eligible_at"]


def test_the_audit_row_names_both_the_actor_and_the_subject(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T117: the first route where the two are different people.

    Logging out records a person ending their own session. Here somebody ends
    **another** person's, and without a row nobody can answer who signed whom
    out. The evidence carries counts and timestamps -- never a token or a hash.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    actor = UUID(str(seeded_database["employee_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    _login_as(client, seeded_database, "colleague@example.com")

    _deactivate(client, headers, colleague)

    rows = [row for row in _audit_rows(colleague) if row["reason_code"] == "EMPLOYEE_DEACTIVATED"]
    assert len(rows) == 1
    row = rows[0]
    assert UUID(str(row["actor_id"])) == actor, "who did it"
    assert UUID(str(row["resource_id"])) == colleague, "who it happened to"
    assert row["actor_id"] != row["resource_id"], "the point of T117"
    assert row["evidence"]["sessions_revoked"] == 1
    assert set(row["evidence"]) == {
        "sessions_revoked",
        "disabled_at",
        "purge_eligible_at",
    }, "no token, hash or address in an append-only table"


def test_deactivating_yourself_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """It would revoke the requesting session and can leave a tenant locked out.

    The cheap version of CP-3's last-administrator guard: it needs to know
    nothing about who else holds what.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _deactivate(client, headers, employee_id)

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_DEACTIVATE_SELF"
    assert _row(employee_id)["status"] == "active"


def test_legal_hold_defaults_to_false_and_this_route_cannot_write_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T118: the column exists, nothing in M1a sets it, and purge must read it.

    Asserted here rather than only registered, because "the field is always
    false" is exactly what makes a purge guard *look* present. The grant is the
    mechanism: deactivation may write three columns, and `legal_hold` is not one
    of them.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    _deactivate(client, headers, colleague)

    assert _row(colleague)["legal_hold"] is False
    with _connection() as connection:
        granted = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.column_privileges
                 WHERE table_name = 'employees' AND grantee = 'fastagent_app'
                   AND privilege_type = 'UPDATE'
                """
            ).fetchall()
        }
    # **Subset and disjoint, not an equality** -- B1 #15's independent review
    # (P2-2) made this point about the sibling assertion in
    # `test_employee_profile_route.py`, and it applies here for the same reason:
    # an equality states a fact about the whole catalog, so freeze #17 adding
    # `password_changed_at` would land it red while being correct. What this
    # route needs is that its three columns are writable and the
    # privilege-bearing ones are not.
    assert {"status", "disabled_at", "purge_eligible_at"} <= granted
    # **2026-09-05, B1 #17.** `password_hash` and `credential_version` left this
    # list, and the reason is worth more than the two lines it takes. A
    # column-level `GRANT` is held by a **role**, not by a route -- and migration
    # 021 had to give `fastagent_app` `UPDATE` on both so that password reset and
    # `POST /auth/change-password` could do their work. From that moment the
    # connection *this* route uses can also write them; what stops it is that its
    # statement does not name them, which is one defence rather than two.
    # Registered as **T127** rather than asserted away here: pretending the grant
    # still excludes them would be a green test asserting something untrue.
    # **`email` left this list in B1 #19 (migration 026).** The purge route has
    # to overwrite the address -- that *is* the tombstone -- so `fastagent_app`
    # now holds `UPDATE (email)` and this route's connection can write it too.
    # **What this case is about is unchanged**: `legal_hold` and `permissions`
    # are still unwritable, and those are the two that matter here -- one is a
    # guard the purge route reads, the other is privilege. T127's fifth
    # instance, and the same disposition as the previous four: narrow the
    # assertion, say why, leave the registration open.
    assert not granted & {
        # `legal_hold` above all: a route that could clear it could purge
        # somebody who is under a hold (ADR-0048 K6, and T118) -- and B1 #19
        # now *is* that route, which is why 026 still withholds this column
        # from every route including the purge itself.
        "legal_hold",
        "permissions",
        "tenant_id",
        "id",
    }


def test_the_two_timestamps_cannot_be_left_half_written(
    seeded_database: dict[str, Any],
) -> None:
    """A retention clock with no start, or a start with no clock, are both states
    no route should be able to leave behind -- so the constraint holds them
    together rather than the application remembering to.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id, email="pair@example.com")

    with _connection() as connection:
        with pytest.raises(Exception, match="employees_disabled_pair|violates"):
            connection.execute(
                "UPDATE employees SET disabled_at = now() WHERE id = %s",
                (colleague,),
            )


def test_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    other = UUID(str(seeded_database["employee_b"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _deactivate(client, headers, other)

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"
    assert _row(other)["status"] == "active"


def test_a_caller_without_employee_update_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = _deactivate(client, headers, colleague)

    assert response.status_code == 403
    assert _row(colleague)["status"] == "active", "a refusal writes nothing"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = client.post(f"/employees/{colleague}/deactivate", headers=headers)

    assert response.status_code == 422


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.post(
        f"/employees/{uuid4()}/deactivate",
        headers={"Idempotency-Key": f"deactivate-{uuid4()}"},
    )

    assert response.status_code == 401


def test_a_session_the_target_already_ended_is_not_re_revoked(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The test B1 #6 handed to whichever of #17/#18 arrived first.

    `POST /auth/logout`'s statement carries `AND revoked_at IS NULL`, and its
    comment said the guard was unreachable through that route and untested:
    the token stops authenticating the moment the row is revoked, so a second
    logout never reaches the statement. **Deactivation is what makes the second
    write possible** -- it revokes sessions it did not issue, including ones the
    employee already ended themselves.

    What the guard protects is an audit fact: *when* a session ended. Moving
    `revoked_at` forward would say the employee was signed out by the
    administrator when they had in fact signed themselves out earlier, and
    `sessions_revoked` would count a session that was not live.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    colleague = _seed_colleague(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    first = _login_as(client, seeded_database, "colleague@example.com")
    assert client.post("/auth/logout", headers=first).status_code == 204
    second = _login_as(client, seeded_database, "colleague@example.com")
    assert client.get("/auth/me", headers=second).status_code == 200
    with _connection() as connection:
        ended_at = connection.execute(
            """
            SELECT revoked_at FROM employee_sessions
             WHERE employee_id = %s AND revoked_at IS NOT NULL
            """,
            (colleague,),
        ).fetchone()["revoked_at"]

    body = _deactivate(client, headers, colleague).json()

    assert body["result"]["sessions_revoked"] == 1, "the ended session is not live"
    with _connection() as connection:
        rows = connection.execute(
            "SELECT revoked_at FROM employee_sessions WHERE employee_id = %s",
            (colleague,),
        ).fetchall()
    assert ended_at in [row["revoked_at"] for row in rows], "the first stamp moved"
    assert all(row["revoked_at"] is not None for row in rows)
