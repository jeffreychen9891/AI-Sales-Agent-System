"""Freeze B1 #16: `PUT /employees/{employee_id}/roles`.

**This is the route migration 015 spent its whole comment pointing at.** Roles
existed, five routes could create and edit them, and `authorize()` read
`employees.permissions` and nothing else -- so a role granted nothing to nobody,
and the repository said so in a migration comment, two table comments, three
route docstrings and a test that asserted it. Registered as **T92**; its expiry
condition was "B1 #16 落地时，同批".

Three cases carry the weight:

* `test_a_role_takes_effect_at_the_next_login_and_not_before` is the wiring
  itself, and the half nobody would guess: permissions are minted into the
  access token, so a **granted** role starts working at the next login while a
  **revoked** one has to be made to stop immediately -- which is why this route
  revokes the target's sessions;
* `test_stripping_the_last_administrator_is_refused_and_writes_nothing` is the
  guard T92 asked to be upgraded from "another role carries `role.update`" to
  "somebody still holds it";
* `test_holding_employee_update_is_not_enough` is the freeze's 「角色变更单列」:
  whoever may rename a colleague must not thereby be able to grant themselves
  the catalog.
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

TARGET_PASSWORD = "local-only-target-password"
TARGET_EMAIL = "rolee-a@example.com"

#: Everything an administrator needs to be one, by this repository's mechanical
#: definition: `role.update` is what `_administrator_exists` counts.
ADMIN_PERMISSIONS = ["role.update", "employee_role.update"]


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


def _seed_role(*, tenant_id: UUID, name: str, permissions: list[str]) -> UUID:
    role_id = uuid4()
    with _connection() as connection:
        connection.execute(
            "INSERT INTO roles(tenant_id, id, name, permissions) VALUES (%s, %s, %s, %s)",
            (tenant_id, role_id, name, Jsonb(sorted(permissions))),
        )
        connection.commit()
    return role_id


def _seed_target(seeded: dict[str, Any], *, email: str = TARGET_EMAIL) -> UUID:
    employee_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            ) VALUES (%s, %s, %s, %s, '[]'::jsonb, 'Target')
            """,
            (seeded["tenant_a"], employee_id, email, hash_password(TARGET_PASSWORD)),
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


def _replace(
    client: TestClient, headers: dict[str, str], employee_id: UUID, role_ids: list[UUID]
) -> Any:
    return client.put(
        f"/employees/{employee_id}/roles",
        json={"role_ids": [str(role_id) for role_id in role_ids]},
        headers={**headers, "Idempotency-Key": f"roles-{uuid4()}"},
    )


def _held(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT role_id, granted_by FROM employee_roles WHERE employee_id = %s"
            " ORDER BY role_id",
            (employee_id,),
        ).fetchall()


def _held_with_time(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT role_id, granted_by, granted_at FROM employee_roles"
            " WHERE employee_id = %s ORDER BY role_id",
            (employee_id,),
        ).fetchall()


def _audit_rows(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT actor_id, action, resource_id, reason_code, evidence FROM audit_events"
            " WHERE resource_id = %s ORDER BY created_at",
            (employee_id,),
        ).fetchall()


def _login(client: TestClient, email: str, password: str) -> Any:
    return client.post(
        "/auth/login",
        json={"tenant_code": "tenant-a", "email": email, "password": password},
    )


# --------------------------------------------------------------------------
# The wiring
# --------------------------------------------------------------------------


def test_a_role_takes_effect_at_the_next_login_and_not_before(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T92, end to end -- and the timing, which is the part worth pinning.

    `POST /auth/login` mints the token from the **union** of
    `employees.permissions` and the permissions of the roles the person holds.
    So a newly granted role starts working when they next log in, and a token
    issued a moment earlier does not carry it.

    That asymmetry is only acceptable in one direction, which is why the route
    revokes the target's sessions: **granting** may be slow, **revoking** may
    not. The revocation half is asserted in
    `test_the_targets_sessions_are_revoked_so_a_removal_is_immediate`.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    reader = _seed_role(tenant_id=tenant_id, name="reader", permissions=["employee.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    before = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    assert before.status_code == 200, before.text
    stale = {"Authorization": f"Bearer {before.json()['access_token']}"}
    assert client.get("/employees", headers=stale).status_code == 403

    assert _replace(client, headers, target, [reader]).status_code == 200

    after = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    assert after.status_code == 200, after.text
    fresh = {"Authorization": f"Bearer {after.json()['access_token']}"}
    assert client.get("/employees", headers=fresh).status_code == 200
    # Named, not merely observed: `/auth/me` echoes what the token carries.
    assert "employee.list" in client.get("/auth/me", headers=fresh).json()["permissions"]


def test_the_union_keeps_what_the_column_already_granted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A union, not a replacement.

    `employees.permissions` is the tenant's per-person exception and the roles
    are its reusable answer; a login that took only one of them would silently
    revoke the other. Asserted from both sides so that a query returning the
    wrong half cannot pass.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    _grant(tenant_id=tenant_id, employee_id=target, permissions=["employee.list"])
    ticketer = _seed_role(
        tenant_id=tenant_id, name="ticketer", permissions=["websocket_ticket.create"]
    )
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    assert _replace(client, headers, target, [ticketer]).status_code == 200

    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    permissions = client.get("/auth/me", headers=theirs).json()["permissions"]
    assert "employee.list" in permissions, "the column survived"
    assert "websocket_ticket.create" in permissions, "the role arrived"


def test_the_targets_sessions_are_revoked_so_a_removal_is_immediate(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The direction that must not be slow.

    Permissions live in the access token, so a role removed from somebody would
    keep working until that token expired. Revoking the session is what makes
    the removal take effect now -- and it is asserted through the token rather
    than through a row, because the token is what an attacker would still hold.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    reader = _seed_role(tenant_id=tenant_id, name="reader", permissions=["employee.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    _replace(client, headers, target, [reader])
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/employees", headers=theirs).status_code == 200

    response = _replace(client, headers, target, [])

    assert response.status_code == 200, response.text
    assert response.json()["result"]["sessions_revoked"] == 1
    assert client.get("/employees", headers=theirs).status_code == 401


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_the_answer_is_a_receipt_and_the_stored_set_records_who_granted_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`{"receipt": …, "result": …}` (ADR-0050 §三), validated whole.

    `granted_by` is checked here rather than left to the audit row: audit
    retention is a policy and employment is longer than most of them, so "who
    gave this person this role" has to survive on the assignment itself.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    first = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    second = _seed_role(tenant_id=tenant_id, name="two", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    response = _replace(client, headers, target, [first, second])

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["resource_ref"]["resource_id"] == str(target)
    assert sorted(body["result"]["role_ids"]) == sorted([str(first), str(second)])
    rows = _held(target)
    assert {UUID(str(row["role_id"])) for row in rows} == {first, second}
    assert all(
        UUID(str(row["granted_by"])) == UUID(str(seeded_database["employee_a"])) for row in rows
    )


def test_the_set_is_replaced_not_merged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`PUT` means "this is the whole answer", for `PUT /roles/{id}/permissions`'s reason.

    A delta against an authorization set has an effect the client cannot
    predict, and the audit row recording it cannot be read afterwards without
    the state it was applied to.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    first = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    second = _seed_role(tenant_id=tenant_id, name="two", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    _replace(client, headers, target, [first, second])

    assert _replace(client, headers, target, [second]).status_code == 200

    assert [UUID(str(row["role_id"])) for row in _held(target)] == [second]


def test_an_empty_array_strips_every_role(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Not confused with "not submitted": the field is required, so `[]` means `[]`."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    role_id = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    _replace(client, headers, target, [role_id])

    assert _replace(client, headers, target, []).status_code == 200

    assert _held(target) == []


def test_the_change_records_both_sides(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """「授权变更必审计」, and the row answers *what changed*.

    Both sides, for `_audit_role_change`'s reason: a row holding only the new
    set sends the reader to find an old one that is by then gone.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    first = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    second = _seed_role(tenant_id=tenant_id, name="two", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    _replace(client, headers, target, [first])

    _replace(client, headers, target, [second])

    rows = [row for row in _audit_rows(target) if row["action"] == "employee_role.update"]
    assert len(rows) == 2
    latest = rows[-1]
    assert UUID(str(latest["actor_id"])) == UUID(str(seeded_database["employee_a"]))
    assert latest["reason_code"] == "EMPLOYEE_ROLES_REPLACED"
    assert latest["evidence"]["roles_before"] == [str(first)]
    assert latest["evidence"]["roles_after"] == [str(second)]
    # **Widened once, by T161, and argued rather than quietly extended.** The
    # equality is the whole point of this assertion: an audit payload must not
    # grow without somebody saying why. `purely_additive` is the reason the
    # sessions survived, and ADR-0055's third clause applies -- once this row is
    # written, the two permission sets the decision compared are gone, so
    # nothing else in the system can answer it.
    assert set(latest["evidence"]) == {
        "roles_before",
        "roles_after",
        "sessions_revoked",
        "purely_additive",
    }


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_holding_employee_update_is_not_enough(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The freeze's 「角色变更单列」, as a fact.

    #15 (profile) and #18 (deactivate) share `employee.update`. If this route
    shared it too, whoever may rename a colleague could grant themselves every
    permission in the catalog -- so it has its own action, and a caller holding
    the neighbouring one is refused.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    role_id = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _replace(client, headers, target, [role_id])

    assert response.status_code == 403
    assert _held(target) == [], "a refusal writes nothing"


def test_stripping_the_last_administrator_is_refused_and_writes_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T92's third landing item, on the route that made it reachable.

    Until this route existed there was no way to take `role.update` away from a
    **person**; the guard could only ask whether another *role* carried it. Here
    the caller's only claim to it is the role they are stripping from
    themselves, which is the real lockout rather than a simulation of one.

    The rollback matters as much as the status: the delete runs before the check
    (the check asks the database what the write left behind), so a transaction
    that did not unwind would leave the tenant in exactly the state the 409 says
    it refused to create.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    caller = UUID(str(seeded_database["employee_a"]))
    owner = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    _grant(tenant_id=tenant_id, employee_id=caller, permissions=[])
    with _connection() as connection:
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, caller, owner),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = _login(client, "operator-a@example.com", seeded_database["login_password"])
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = _replace(client, headers, caller, [])

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMINISTRATOR_PROTECTED"
    assert [UUID(str(row["role_id"])) for row in _held(caller)] == [owner], (
        "the transaction unwound"
    )


def test_a_role_this_tenant_does_not_have_is_named_and_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """422 with the ids in `field_errors`, and another tenant's role among them.

    A role belonging to tenant B arrives here indistinguishable from one that
    does not exist, because the read that decides is tenant-scoped by RLS. That
    is what stops this route being a cross-tenant existence oracle -- and it is
    asserted with a **real** role id from the other tenant, not an invented one,
    because an invented id would be unknown for the boring reason.
    """

    tenant_b = UUID(str(seeded_database["tenant_b"]))
    target = _seed_target(seeded_database)
    theirs = _seed_role(tenant_id=tenant_b, name="theirs", permissions=["employee.list"])
    stranger = uuid4()
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    response = _replace(client, headers, target, [theirs, stranger])

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "ROLE_NOT_IN_TENANT"
    assert {item["field"] for item in body["field_errors"]} == {str(theirs), str(stranger)}
    assert _held(target) == []


def test_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, not 403 -- the same reasoning every route in this cluster uses."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    response = _replace(client, headers, UUID(str(seeded_database["employee_b"])), [role_id])

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"


def test_a_disabled_employee_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409: granting roles to somebody who cannot log in is a write with no effect."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    role_id = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    client, headers = _client_for(
        settings, seeded_database, ["employee_role.update", "employee.update"]
    )
    assert (
        client.post(
            f"/employees/{target}/deactivate",
            headers={**headers, "Idempotency-Key": f"deactivate-{uuid4()}"},
        ).status_code
        == 200
    )

    response = _replace(client, headers, target, [role_id])

    assert response.status_code == 409
    assert response.json()["code"] == "EMPLOYEE_DISABLED"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: every command carries one."""

    target = _seed_target(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])

    response = client.put(
        f"/employees/{target}/roles", json={"role_ids": []}, headers=headers
    )

    assert response.status_code == 422


def test_the_grant_on_the_assignment_table_has_no_update(
    seeded_database: dict[str, Any],
) -> None:
    """Migration 022, read out of the catalog rather than trusted.

    An assignment row has no mutable part: who holds what, and when it was
    granted. `UPDATE` would let a statement move `role_id` under an existing
    `granted_at` / `granted_by` -- changing what somebody may do while the row
    still claims somebody else granted it at some other time.
    """

    with _connection() as connection:
        granted = {
            row["privilege_type"]
            for row in connection.execute(
                """
                SELECT privilege_type FROM information_schema.table_privileges
                 WHERE table_name = 'employee_roles' AND grantee = 'fastagent_app'
                """
            ).fetchall()
        }

    assert {"SELECT", "INSERT", "DELETE"} <= granted
    assert "UPDATE" not in granted


def test_changing_one_role_does_not_restamp_the_others(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`granted_by` / `granted_at` answer what migration 022 says they answer.

    The first version deleted the whole set and re-inserted `requested`, so a
    call that **added** one role rewrote `granted_by` and `granted_at` on every
    role the person already held -- with this caller and this moment. 022's
    column comment promises "who gave this person this role" survives on the
    row; what that shape could answer was "who last touched this person's role
    set", a weaker fact wearing the stronger one's name (independent review
    2026-09-05, P2-1).

    Asserted on `granted_at` rather than only on `granted_by`, because in this
    test both writers would be the same employee -- the timestamp is what
    distinguishes "left alone" from "rewritten identically".
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    first = _seed_role(tenant_id=tenant_id, name="one", permissions=["employee.list"])
    second = _seed_role(tenant_id=tenant_id, name="two", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    assert _replace(client, headers, target, [first]).status_code == 200
    stamped = {
        UUID(str(row["role_id"])): row["granted_at"] for row in _held_with_time(target)
    }

    assert _replace(client, headers, target, [first, second]).status_code == 200

    after = {UUID(str(row["role_id"])): row["granted_at"] for row in _held_with_time(target)}
    assert set(after) == {first, second}
    assert after[first] == stamped[first], "an untouched assignment kept its own history"


def test_deactivating_the_last_administrator_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T120**, whose expiry condition was "角色接线 T92 落地时" -- this batch.

    The hole this closes is not hypothetical and does not need an administrator
    to open: a caller holding only `employee.update` can disable somebody, and
    if that somebody is the tenant's only holder of `role.update`, nothing
    afterwards can grant a role or edit a permission set. There is no route back
    to `active` either (#19 is not built), so the state is terminal.

    It was unreachable as a *guard* before this batch in the sense that mattered:
    "is there another administrator" had no answer while `employee_roles` was
    empty. Now `_administrator_exists` answers it, and deactivation is precisely
    an action that can flip it -- the predicate counts only `active` people.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    owner = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    with _connection() as connection:
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, target, owner),
        )
        connection.commit()
    # The caller is **not** an administrator: they can disable people and nothing else.
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = client.post(
        f"/employees/{target}/deactivate",
        headers={**headers, "Idempotency-Key": f"deactivate-{uuid4()}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMINISTRATOR_PROTECTED"
    with _connection() as connection:
        status = connection.execute(
            "SELECT status FROM employees WHERE id = %s", (target,)
        ).fetchone()["status"]
    assert status == "active", "the transaction unwound"


def test_deactivating_one_of_two_administrators_is_allowed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half, and the half that proves it is not a blanket refusal.

    A guard that refused every deactivation of anybody holding `role.update`
    would be indistinguishable from "administrators can never be disabled",
    which is not the rule -- the rule is about the **last** one.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    owner = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    with _connection() as connection:
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, target, owner),
        )
        connection.commit()
    # A second administrator, this one through the column.
    client, headers = _client_for(
        settings, seeded_database, ["employee.update", "role.update"]
    )

    response = client.post(
        f"/employees/{target}/deactivate",
        headers={**headers, "Idempotency-Key": f"deactivate-{uuid4()}"},
    )

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------
# T161: an assignment that only adds does not end the target's sessions.
#
# Until B1 #5 it had to -- permissions were minted at login and never
# recomputed, so a new role could not take effect without a logout.
# `POST /auth/refresh` now recomputes the union, so an added permission arrives
# on its own within one refresh and the forced logout bought nothing but
# interruption. **Removal still ends them immediately**, and that asymmetry is
# deliberate (T129): granting late is an inconvenience, revoking late is a
# security problem.
# --------------------------------------------------------------------------


def _live_sessions(employee_id: UUID) -> int:
    with _connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS total FROM employee_sessions"
            " WHERE employee_id = %s AND revoked_at IS NULL",
            (employee_id,),
        ).fetchone()
    return int(row["total"]) if row is not None else 0


def _open_session(client: TestClient, email: str = TARGET_EMAIL) -> None:
    """A real login, because a hand-written session row proves less.

    The property is "this person is not logged out", and the way a person is
    logged in is `POST /auth/login`.
    """

    assert _login(client, email, TARGET_PASSWORD).status_code == 200


def test_a_purely_additive_assignment_keeps_the_sessions(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The change itself, asserted on the session rows rather than the response.

    A response field could report `sessions_revoked: 0` while the rows were
    revoked anyway; the rows are what a person actually notices.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    reader = _seed_role(tenant_id=tenant_id, name=f"reader-{uuid4().hex[:8]}",
                        permissions=["employee.read"])
    _open_session(client)
    assert _live_sessions(target) == 1

    response = _replace(client, headers, target, [reader])

    assert response.status_code == 200, response.text
    assert _live_sessions(target) == 1, "an additive assignment must not log anybody out"
    assert response.json()["result"]["sessions_revoked"] == 0


def test_removing_a_role_still_ends_the_sessions_immediately(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The half that must not change.**

    Written beside the case above because a rule that skipped revocation
    unconditionally would pass that one. Revocation is what makes "you no longer
    have this" true now rather than at the next refresh.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    reader = _seed_role(tenant_id=tenant_id, name=f"reader-{uuid4().hex[:8]}",
                        permissions=["employee.read"])
    assert _replace(client, headers, target, [reader]).status_code == 200
    _open_session(client)
    assert _live_sessions(target) == 1

    response = _replace(client, headers, target, [])

    assert response.status_code == 200, response.text
    assert _live_sessions(target) == 0
    assert response.json()["result"]["sessions_revoked"] >= 1


def test_swapping_roles_that_grant_the_same_permission_keeps_the_sessions(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Losing a role and losing a permission are different events.**

    This is why both sides are read as permission *sets* rather than inferred
    from which role ids moved. Swap a role for a different one granting exactly
    the same permission and nothing was taken away -- an implementation that
    compared role ids would log the person out over a change that never reached
    them.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    first = _seed_role(tenant_id=tenant_id, name=f"first-{uuid4().hex[:8]}",
                       permissions=["employee.read"])
    second = _seed_role(tenant_id=tenant_id, name=f"second-{uuid4().hex[:8]}",
                        permissions=["employee.read"])
    assert _replace(client, headers, target, [first]).status_code == 200
    _open_session(client)

    response = _replace(client, headers, target, [second])

    assert response.status_code == 200, response.text
    assert _live_sessions(target) == 1


def test_the_audit_row_records_whether_it_was_additive(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0055's third clause: after this row, nothing else can answer it.

    The two permission sets the decision compared are gone once the assignment
    lands, so "were the sessions kept on purpose" is answerable only from here.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    target = _seed_target(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee_role.update"])
    reader = _seed_role(tenant_id=tenant_id, name=f"reader-{uuid4().hex[:8]}",
                        permissions=["employee.read"])

    assert _replace(client, headers, target, [reader]).status_code == 200

    rows = _audit_rows(target)
    assert rows, "the assignment must leave an audit row"
    # Read as a value rather than as rendered text: `evidence` comes back
    # decoded, so a substring test would depend on how a bool happens to print.
    assert rows[-1]["evidence"]["purely_additive"] is True
