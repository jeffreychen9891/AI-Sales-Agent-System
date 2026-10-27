"""Route freeze B1 #15 (`C0A-019` split): `PATCH /employees/{employee_id}`.

**The split is the design.** `C0A-019` was one endpoint that wrote whatever the
client sent; the freeze cut it into four so that changing what somebody *may do*
cannot ride along with changing what they are *called*. So the case that matters
most here is `test_the_route_cannot_reach_anything_but_the_three_fields` -- it
asserts the negative that the split exists for, and it asserts it three times
over, because the batch defends it three times over: the request model forbids
unknown keys, the statement names three columns, and migration 018 grants
`UPDATE` on exactly those three.

The second one worth reading is
`test_not_sending_a_field_is_different_from_sending_null`. A PATCH that cannot
tell those apart either makes clearing a field impossible or makes every request
clear everything it did not mention, and both are silent.
"""

from __future__ import annotations

import os
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


def _patch(
    client: TestClient, headers: dict[str, str], employee_id: Any, **body: Any
) -> Any:
    return client.patch(
        f"/employees/{employee_id}",
        json=body,
        headers={**headers, "Idempotency-Key": f"profile-{uuid4()}"},
    )


def _row(employee_id: UUID) -> dict[str, Any]:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT display_name, employee_no, department, permissions, status,
                   credential_version, password_hash
              FROM employees WHERE id = %s
            """,
            (employee_id,),
        ).fetchone()


def test_the_column_is_required_and_bounded(seeded_database: dict[str, Any]) -> None:
    """What the migration leaves behind, asserted; the backfill itself cannot be.

    Migration 018 adds the column, backfills existing rows from the local part
    of their email, and only then sets `NOT NULL` -- that order is what lets it
    run on a database that already has employees. **The backfill step cannot be
    observed from here**: the test database is migrated before any fixture row
    exists, so there is never a row that predates the column. Asserting it would
    need a harness that seeds pre-018 and then migrates, which this repository
    does not have; saying so is better than a test that appears to cover it.

    What *is* observable is the state the migration leaves: required, and
    bounded. The bounds are a second line behind the request model -- they hold
    for a writer that never goes through the route.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with _connection() as connection:
        with pytest.raises(Exception, match="not-null|violates"):
            connection.execute(
                """
                INSERT INTO employees(tenant_id, id, email, password_hash, permissions)
                VALUES (%s, %s, 'no-name@example.com', 'x', '[]'::jsonb)
                """,
                (tenant_id, uuid4()),
            )
    with _connection() as connection:
        with pytest.raises(Exception, match="employees_display_name_length|violates"):
            connection.execute(
                """
                INSERT INTO employees(
                    tenant_id, id, email, password_hash, permissions, display_name
                ) VALUES (%s, %s, 'too-long@example.com', 'x', '[]'::jsonb, %s)
                """,
                (tenant_id, uuid4(), "n" * 129),
            )


def test_a_patch_updates_the_named_field_and_echoes_the_row(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _patch(client, headers, employee_id, display_name="Mei", department="Support")

    assert response.status_code == 200, response.text
    body = response.json()
    # **The whole body, not a subset of it** (ADR-0050): the first version
    # returned `{**receipt, "employee": {...}}` and validated six keys picked
    # out of it -- a check that agrees with itself when the body is a receipt,
    # and the only check that misses when it is not. This route carries no
    # receipt at all, so what is asserted is the projection's exact shape.
    assert set(body) == {
        "employee_id",
        "email",
        "status",
        "display_name",
        "employee_no",
        "department",
        "correlation_id",
    }
    assert body["display_name"] == "Mei"
    assert body["department"] == "Support"
    assert body["employee_id"] == str(employee_id)
    UUID(body["correlation_id"])
    stored = _row(employee_id)
    assert stored["display_name"] == "Mei"
    assert stored["department"] == "Support"


def test_not_sending_a_field_is_different_from_sending_null(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """"Absent" and "null" are two requests, and a PATCH must not merge them.

    Merged one way, a field can never be cleared. Merged the other, every
    request clears everything it did not mention. Both are silent, and both are
    the kind of thing a caller only discovers from the data afterwards.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    _patch(client, headers, employee_id, employee_no="E-1", department="Support")

    # Absent: leaves both alone.
    _patch(client, headers, employee_id, display_name="Mei")
    kept = _row(employee_id)
    assert kept["employee_no"] == "E-1"
    assert kept["department"] == "Support"

    # Explicit null: clears that one, and only that one.
    _patch(client, headers, employee_id, department=None)
    cleared = _row(employee_id)
    assert cleared["department"] is None
    assert cleared["employee_no"] == "E-1"
    assert cleared["display_name"] == "Mei"


def test_display_name_cannot_be_cleared(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The column is `NOT NULL`, and the route says why rather than the constraint.

    Letting it reach the database would answer with a constraint name, which
    tells a caller what broke and not what to do.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _patch(client, headers, employee_id, display_name=None)

    assert response.status_code == 422
    assert response.json()["code"] == "DISPLAY_NAME_REQUIRED"
    assert _row(employee_id)["display_name"] is not None


def test_an_empty_patch_is_refused_rather_than_answered_with_success(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`{}` asks for nothing. Answering 200 would report a change that never happened."""

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _patch(client, headers, employee_id)

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_SUBMITTED"


def test_the_route_cannot_reach_anything_but_the_three_fields(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The negative the whole split exists for.

    `C0A-019` used to be one endpoint writing whatever arrived. Here a request
    naming `permissions`, `status` or `password_hash` is refused outright by the
    model -- and even if it were not, the statement names three columns and
    migration 018 grants `UPDATE` on exactly those three. Three defences, and
    this asserts the outcome all three exist for.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    before = _row(employee_id)

    for forbidden in ("permissions", "status", "credential_version", "password_hash"):
        response = _patch(client, headers, employee_id, **{forbidden: "anything"})
        assert response.status_code == 422, f"{forbidden} was accepted"
        assert response.json()["code"] == "VALIDATION_FAILED"

    after = _row(employee_id)
    assert after["permissions"] == before["permissions"]
    assert after["status"] == before["status"]
    assert after["credential_version"] == before["credential_version"]
    assert after["password_hash"] == before["password_hash"]


def test_the_column_grant_is_narrower_than_the_table(
    seeded_database: dict[str, Any],
) -> None:
    """The third defence, asserted where it lives rather than trusted.

    A table-wide `GRANT UPDATE` would let this route's connection write
    `permissions` -- and the only thing between it and a privilege change would
    be the application remembering not to. This reads the catalog: the app role
    holds `UPDATE` on this route's three columns, and on none of the ones that
    carry privilege. **Not an equality** -- later batches add columns here
    legitimately, and an equality would turn each of those into a false alarm.
    """

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

    # **Two halves, and neither is an equality** (independent review P2-2). An
    # equality asserts a fact about the whole catalog rather than this route's
    # invariant: freeze #17 will legitimately add `password_changed_at` and #18
    # adds `status` plus two timestamps, and each of those would land this
    # assertion red while being correct -- which is the usual way a guard like
    # this gets loosened. The property worth holding is "this route's columns
    # are granted, and the privilege-bearing ones are not".
    assert {"display_name", "employee_no", "department"} <= granted
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
        "permissions",
        "legal_hold",
        "tenant_id",
        "id",
    }


def test_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    other = UUID(str(seeded_database["employee_b"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _patch(client, headers, other, display_name="Mei")

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"
    assert _row(other)["display_name"] == "Operator B", "the other tenant's row is untouched"


def test_a_caller_without_employee_update_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.read"])
    before = _row(employee_id)

    response = _patch(client, headers, employee_id, display_name="Mei")

    assert response.status_code == 403
    assert _row(employee_id)["display_name"] == before["display_name"]


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = client.patch(
        f"/employees/{seeded_database['employee_a']}",
        json={"display_name": "Mei"},
        headers=headers,
    )

    assert response.status_code == 422


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.patch(
        f"/employees/{seeded_database['employee_a']}",
        json={"display_name": "Mei"},
        headers={"Idempotency-Key": f"profile-{uuid4()}"},
    )

    assert response.status_code == 401


def test_a_name_that_is_only_whitespace_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`" "` passes `min_length=1` and the column CHECK, and shows as blank.

    K7 made `display_name` required so the control plane never has to fall back
    to a uuid or an email. A name made of spaces defeats that just as
    completely, and more quietly -- nothing anywhere reports an error.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    before = _row(employee_id)["display_name"]

    response = _patch(client, headers, employee_id, display_name="   ")

    assert response.status_code == 422
    assert _row(employee_id)["display_name"] == before


def test_a_name_with_surrounding_whitespace_is_stored_trimmed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half: trimming must not reject a real name, only bare space."""

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _patch(client, headers, employee_id, display_name="  Mei  ")

    assert response.status_code == 200, response.text
    assert _row(employee_id)["display_name"] == "Mei"
