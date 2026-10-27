"""Route freeze B1 #14 (`C0A-018`): `POST /employees`.

The freeze note for this route is one line -- 「密码只写、响应永不含密码」-- and two
cases here are that line: `test_the_password_is_nowhere_in_the_answer` looks at
the whole response and the whole stored row, and
`test_the_new_employee_can_log_in_and_then_can_do_nothing_at_all` is its other
half, because a route that stored garbage would also pass the first one. That
second case also pins a surprise the gate found: the account authenticates and
then cannot call anything at all, `GET /auth/me` included (T125).

The third case worth reading is
`test_the_route_cannot_mint_privilege_and_is_stopped_twice_over`: a create route
that could write `permissions` would be a privilege-escalation route under
another name, and this batch defends that twice -- the request model forbids
unknown keys, and migration 020's `INSERT` grant names seven columns that do not
include it.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

PASSWORD = "local-only-strong-password"
#: The same password with whitespace an administrator can easily paste in. It
#: must survive the round trip **byte for byte**: see
#: `test_a_password_is_stored_exactly_as_it_was_given`.
PADDED_PASSWORD = f"  {PASSWORD}  "


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


def _create(client: TestClient, headers: dict[str, str], **body: Any) -> Any:
    payload = {
        "email": "newcomer@example.com",
        "password": PASSWORD,
        "display_name": "Newcomer",
        **body,
    }
    return client.post(
        "/employees",
        json=payload,
        headers={**headers, "Idempotency-Key": f"create-employee-{uuid4()}"},
    )


def _row(employee_id: UUID) -> dict[str, Any] | None:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT tenant_id, email, password_hash, permissions, status,
                   credential_version, display_name, employee_no, department,
                   disabled_at, purge_eligible_at, legal_hold, must_change_password
              FROM employees WHERE id = %s
            """,
            (employee_id,),
        ).fetchone()


def test_a_create_answers_with_a_command_receipt_and_nothing_else(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """201 + a receipt whose `resource_ref` names the new employee.

    The whole body is validated against the contract rather than six keys picked
    out of it (ADR-0050 §二): picking keys is the one way of writing this check
    that passes no matter what else the body carries -- and what this route must
    never carry is the password.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])

    response = _create(client, headers)

    assert response.status_code == 201, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body)
    assert body["status"] == "succeeded"
    assert body["resource_ref"]["resource_type"] == "employee"
    stored = _row(UUID(body["resource_ref"]["resource_id"]))
    assert stored is not None
    assert stored["email"] == "newcomer@example.com"
    assert stored["display_name"] == "Newcomer"
    assert UUID(str(stored["tenant_id"])) == UUID(str(seeded_database["tenant_a"]))


def test_the_password_is_nowhere_in_the_answer(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-018`'s whole freeze note: 密码只写、响应永不含密码.

    Asserted against the **serialised** body rather than key by key, because the
    failure this guards is a field somebody adds later without thinking about
    it. The stored column is checked too: it must be a hash, and a hash is not
    the thing that was typed.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])

    response = _create(client, headers)

    assert PASSWORD not in response.text
    assert PASSWORD not in str(dict(response.headers))
    stored = _row(UUID(response.json()["resource_ref"]["resource_id"]))
    assert stored is not None
    assert stored["password_hash"] != PASSWORD
    assert PASSWORD not in stored["password_hash"]


def test_the_new_employee_can_log_in_and_then_owes_a_password_change(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half of "write-only", and the state B1 #17 put the account in.

    A hash nobody can authenticate against would satisfy every assertion above
    and be useless, so this logs in with the password that was set. That half
    passes.

    **The second half changed when #17 landed.** It used to be that the account
    authenticated and then hit 403 everywhere because `permissions` defaults to
    `[]` (registered as **T125**, still open and still true). Now the *first*
    thing standing in the way is K8: the administrator chose this password and
    handed it over through a channel outside this system, so the row is written
    with `must_change_password = true` and every route answers **403
    `PASSWORD_CHANGE_REQUIRED`** until `POST /auth/change-password` replaces it.

    Asserting the **code** rather than the status is the whole value of this
    case: both reasons produce 403, and a test that compared statuses would have
    passed unchanged through a batch that altered what the account can do.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])
    _create(client, headers)

    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "newcomer@example.com",
            "password": PASSWORD,
        },
    )

    assert login.status_code == 200, login.text
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # Authentication succeeded; K8 says this credential is good for one route.
    denied = client.get("/auth/me", headers=theirs)
    assert denied.status_code == 403
    assert denied.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert client.get("/employees", headers=theirs).status_code == 403


def test_the_route_cannot_mint_privilege_and_is_stopped_twice_over(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A create route that could write `permissions` is an escalation route.

    Two defences, asserted separately because they fail separately: the model
    refuses to accept the key at all, and the row that does get written starts
    with no permissions and an active status it did not choose.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])

    refused = _create(client, headers, permissions=["role.create"])
    assert refused.status_code == 422
    assert _create(client, headers, must_change_password=False).status_code == 422
    for extra in (
        {"status": "disabled"},
        {"credential_version": 99},
        {"legal_hold": True},
        # **`tenant_id` belongs in this list and was missing from it**
        # (independent review 2026-09-05, P2-2). It is the one column here
        # whose second defence is a *different mechanism*: migration 020 has to
        # grant `INSERT` on it, so what refuses a foreign tenant is RLS's
        # `WITH CHECK`, not the absence of a grant. The route never passes a
        # client-supplied value -- `tenant_id` comes from the `AuthContext` --
        # and this asserts the key cannot even be named.
        {"tenant_id": str(seeded_database["tenant_b"])},
    ):
        assert _create(client, headers, **extra).status_code == 422

    created = _create(client, headers)
    stored = _row(UUID(created.json()["resource_ref"]["resource_id"]))
    assert stored is not None
    assert stored["permissions"] == []
    assert stored["status"] == "active"
    assert stored["credential_version"] == 1
    assert stored["legal_hold"] is False
    assert stored["disabled_at"] is None
    # Written by this route rather than defaulted (B1 #17, T124): the password
    # was chosen by somebody other than its owner, and the flag is the record of
    # that. It is not a field the client may set -- the model refuses the key
    # above with everything else it refuses.
    assert stored["must_change_password"] is True


def test_the_insert_grant_is_narrower_than_the_table(
    seeded_database: dict[str, Any],
) -> None:
    """The second defence, read out of the catalog rather than trusted.

    A table-wide `GRANT INSERT` would let this route's connection write
    `permissions` even though the model refuses to name it -- and then the only
    thing between a client and a privilege change would be the application
    remembering not to.

    **Not an equality**: later batches add insertable columns legitimately
    (#17's `must_change_password` will be one), and an equality here would turn
    each of those into a false alarm -- which is how a guard like this gets
    loosened (B1 #15's independent review, P2-2).
    """

    with _connection() as connection:
        granted = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.column_privileges
                 WHERE table_name = 'employees' AND grantee = 'fastagent_app'
                   AND privilege_type = 'INSERT'
                """
            ).fetchall()
        }

    assert {"tenant_id", "id", "email", "password_hash", "display_name"} <= granted
    assert not granted & {
        "permissions",
        "status",
        "credential_version",
        "password_changed_at",
        "legal_hold",
        "disabled_at",
        "purge_eligible_at",
    }


def test_the_same_address_twice_is_a_conflict_and_writes_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409 from the unique constraint, not from a read-then-write.

    A pre-check would let two simultaneous requests for the same address both
    through -- `POST /roles` refuses the same way and for the same reason.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])
    first = _create(client, headers)
    assert first.status_code == 201, first.text

    second = _create(client, headers, display_name="Impostor")

    assert second.status_code == 409
    assert second.json()["code"] == "EMPLOYEE_EMAIL_ALREADY_USED"
    with _connection() as connection:
        count = connection.execute(
            "SELECT count(*) AS n FROM employees WHERE email = %s",
            ("newcomer@example.com",),
        ).fetchone()["n"]
    assert count == 1
    stored = _row(UUID(first.json()["resource_ref"]["resource_id"]))
    assert stored is not None
    assert stored["display_name"] == "Newcomer", "the refused call changed nothing"


def test_a_name_that_is_only_whitespace_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`" "` passes `min_length=1` and the column CHECK, and shows as blank."""

    client, headers = _client_for(settings, seeded_database, ["employee.create"])

    response = _create(client, headers, display_name="   ")

    assert response.status_code == 422


def test_a_caller_without_employee_create_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _create(client, headers)

    assert response.status_code == 403
    with _connection() as connection:
        found = connection.execute(
            "SELECT count(*) AS n FROM employees WHERE email = %s",
            ("newcomer@example.com",),
        ).fetchone()["n"]
    assert found == 0, "a refusal writes nothing"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: every create command carries one."""

    client, headers = _client_for(settings, seeded_database, ["employee.create"])

    response = client.post(
        "/employees",
        json={"email": "n@example.com", "password": PASSWORD, "display_name": "N"},
        headers=headers,
    )

    assert response.status_code == 422


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.post(
        "/employees",
        json={"email": "n@example.com", "password": PASSWORD, "display_name": "N"},
        headers={"Idempotency-Key": f"create-employee-{uuid4()}"},
    )

    assert response.status_code == 401


def test_a_password_is_stored_exactly_as_it_was_given(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The credential that goes in is the credential that logs in (P2-1).

    The first version of `CreateEmployeeRequest` carried
    `str_strip_whitespace=True` on the **model**, which pydantic applies to
    every `str` field on it -- `password` included -- while `LoginRequest` has
    no such switch and hands `verify_password` the login input untouched. So an
    administrator who set `"  pw  "` stored the hash of `"pw"`; the person they
    handed it to typed it back exactly, got a 401 that says nothing, and there
    is **no route in this repository that can change a password** to get out of
    it.

    The existing cases could not see it: `PASSWORD` has no whitespace in it, so
    stripping was the identity function on every value they used. This one uses
    a value where the two differ, and asserts the direction that matters --
    **what was given logs in** -- rather than only that something logs in.

    The second half is the same property from the other side: the stripped
    form is a *different* password and must be refused. Without it, a later
    "normalise both sides" change would still pass the first half.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.create"])
    created = _create(client, headers, password=PADDED_PASSWORD)
    assert created.status_code == 201, created.text

    exact = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "newcomer@example.com",
            "password": PADDED_PASSWORD,
        },
    )
    stripped = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "newcomer@example.com",
            "password": PADDED_PASSWORD.strip(),
        },
    )

    assert exact.status_code == 200, exact.text
    assert stripped.status_code == 401, "the stripped form is a different password"


def test_a_row_cannot_be_written_into_another_tenant_even_by_the_app_role(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`RLS-EMPLOYEE-007`, asserted rather than promised (P2-2).

    Every other column this route must not write is stopped twice: the model
    refuses the key, and migration 020's `INSERT` grant does not name the
    column. **`tenant_id` is the exception** -- the grant *must* include it, so
    the only thing standing between a bug in this route and a row landing in
    another tenant is the RLS policy's `WITH CHECK`. That is a different
    mechanism from the one the rest of the batch relies on, and the manifest
    row named the assertion for it while nothing asserted it.

    So this goes underneath the route: it takes the application role's own
    connection, sets `app.tenant_id` to tenant A -- the same setting
    `Database.transaction` writes before it hands a connection to a route --
    and tries to insert a row belonging to tenant B. The policy refuses it.
    Nothing about the route is involved, which is the point: the guarantee has
    to hold for whatever writes this table next.

    `autocommit=True` and a session-scoped `set_config` rather than the
    transaction-scoped one, for one mechanical reason: a statement that fails
    inside an open transaction leaves it aborted, and unwinding that around a
    `pytest.raises` is noise in a test whose whole subject is one refusal.
    """

    with psycopg.connect(
        settings.database_url, row_factory=dict_row, autocommit=True
    ) as connection:
        connection.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            (str(seeded_database["tenant_a"]),),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """
                INSERT INTO employees(
                    tenant_id, id, email, password_hash, display_name
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    seeded_database["tenant_b"],
                    uuid4(),
                    "smuggled@example.com",
                    "not-a-real-hash",
                    "Smuggled",
                ),
            )

    with _connection() as connection:
        found = connection.execute(
            "SELECT count(*) AS n FROM employees WHERE email = %s",
            ("smuggled@example.com",),
        ).fetchone()["n"]
    assert found == 0
