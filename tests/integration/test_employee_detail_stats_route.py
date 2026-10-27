"""Freeze B1 #21 (`C0A-023`): `GET /employees/{employee_id}/stats`.

Two of these are the ones worth reading.

`test_an_employee_of_another_tenant_is_not_found_rather_than_forbidden` is the
boundary: the caller holds `employee.read` in their own tenant, so a route that
answered `403` for a neighbour's employee -- or `200` -- would be telling them
something about a row they may not see. `404` for both "does not exist" and
"exists elsewhere" is what keeps the route from being an existence oracle.

`test_the_projection_does_not_report_a_role_count` pins an **omission**, which
is unusual and deliberate. `employee_roles` is one join away and a `role_count`
would look like an obvious field to add; it is not, because roles grant nothing
until freeze B1 #16 (**T92**), and a projection is the one place where "3 roles"
is read as "3 roles' worth of capability". A future batch that adds the field
should have to delete this test and read why it existed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
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


def _seed_session(
    *,
    tenant_id: UUID,
    employee_id: UUID,
    expires_in: timedelta,
    revoked: bool = False,
    created_at: datetime | None = None,
) -> UUID:
    """Insert `employee_sessions` directly.

    Logging in would create one too, but only a live one: the expired and the
    revoked shapes -- the two this route has to *not* count -- have no route
    that produces them today.
    """

    session_id = uuid4()
    now = datetime.now(UTC)
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employee_sessions(
                tenant_id, id, employee_id, credential_version, expires_at,
                revoked_at, created_at
            ) VALUES (%s, %s, %s, 1, %s, %s, %s)
            """,
            (
                tenant_id,
                session_id,
                employee_id,
                now + expires_in,
                now if revoked else None,
                created_at or now,
            ),
        )
        connection.commit()
    return session_id


def _client_for(
    settings: Settings,
    seeded: dict[str, Any],
    permissions: list[str],
    *,
    tenant: str = "a",
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded[f"tenant_{tenant}"])),
        employee_id=UUID(str(seeded[f"employee_{tenant}"])),
        permissions=permissions,
    )
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": f"tenant-{tenant}",
            "email": f"operator-{tenant}@example.com",
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_the_projection_reports_this_employees_own_row(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = client.get(f"/employees/{employee_id}/stats", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == str(employee_id)
    assert body["email"] == "operator-a@example.com"
    assert body["status"] == "active"
    # `_client_for` granted exactly one permission, so this is the permission
    # count of a known set rather than "some number".
    assert body["permission_count"] == 1
    assert body["created_at"].endswith("+00:00")
    assert body["as_of"].endswith("+00:00")


def test_the_login_that_fetched_the_page_is_itself_an_active_session(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The count is of real sessions, not of a constant.

    `_client_for` logs in, and login writes `employee_sessions`. So the caller
    asking about themselves must see at least their own session, and
    `last_session_started_at` cannot be null.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    body = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    assert body["active_session_count"] >= 1
    assert body["last_session_started_at"] is not None


def test_revoked_and_expired_sessions_are_not_counted_as_active(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])
    before = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    _seed_session(
        tenant_id=tenant_id,
        employee_id=employee_id,
        expires_in=timedelta(hours=1),
        revoked=True,
    )
    _seed_session(
        tenant_id=tenant_id, employee_id=employee_id, expires_in=-timedelta(hours=1)
    )

    after = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    # Two more rows exist; neither may move the count.
    assert after["active_session_count"] == before["active_session_count"]


def test_a_live_session_does_move_the_count(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The negative above proves nothing on its own.

    A route that hard-coded the count, or filtered everything away, would pass
    it. This is the case that separates "the filter is right" from "the filter
    rejects everything".
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])
    before = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    _seed_session(
        tenant_id=tenant_id, employee_id=employee_id, expires_in=timedelta(hours=1)
    )

    after = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    assert after["active_session_count"] == before["active_session_count"] + 1


def test_last_session_started_at_reports_the_most_recent_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    newest = datetime.now(UTC) + timedelta(minutes=5)
    _seed_session(
        tenant_id=tenant_id,
        employee_id=employee_id,
        expires_in=timedelta(hours=1),
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    _seed_session(
        tenant_id=tenant_id,
        employee_id=employee_id,
        expires_in=timedelta(hours=1),
        created_at=newest,
    )

    body = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    assert body["last_session_started_at"] == newest.isoformat()


def test_an_unknown_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = client.get(f"/employees/{uuid4()}/stats", headers=headers)

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"


def test_an_employee_of_another_tenant_is_not_found_rather_than_forbidden(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The tenant boundary, stated as a status code.

    Tenant B's employee exists. The caller is authenticated in tenant A and
    holds `employee.read` there, so authorization alone would let the handler
    run -- what stops it is RLS, and what the caller learns must be exactly what
    they would learn about an id that was never issued.
    """

    other = UUID(str(seeded_database["employee_b"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = client.get(f"/employees/{other}/stats", headers=headers)

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"


def test_the_route_refuses_a_caller_without_employee_read(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.list"])

    response = client.get(f"/employees/{employee_id}/stats", headers=headers)

    assert response.status_code == 403


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.get(f"/employees/{seeded_database['employee_a']}/stats")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_the_projection_does_not_report_a_role_count(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T92: a role grants nothing until freeze B1 #16 wires assignment.

    Reporting a count of them on a stats page would read as capability the
    person does not have. Asserted against the served body's keys, so adding the
    field anywhere in the handler fails here.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    body = client.get(f"/employees/{employee_id}/stats", headers=headers).json()

    assert "role_count" not in body
    assert not any("role" in key for key in body)


def test_the_response_is_not_cacheable(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = client.get(f"/employees/{employee_id}/stats", headers=headers)

    assert response.headers["cache-control"] == "no-store, private"


def test_the_by_id_route_does_not_shadow_the_tenant_projection(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`/employees/stats` must keep meaning the aggregate.

    FastAPI matches in declaration order and both paths start `/employees/`.
    This one is cheap and it is the failure the sibling's docstring warns
    about, one shape over.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    aggregate = client.get("/employees/stats", headers=headers).json()

    assert set(aggregate) == {"total", "active", "disabled", "as_of"}
