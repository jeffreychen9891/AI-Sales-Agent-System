"""Freeze B1 #19 + parity §6.4, executed under ADR-0056: purge, never delete.

**The case that carries this batch is
`test_the_subject_is_destroyed_but_the_row_and_its_audit_survive`.** §6.4 asks
for two things that pull against each other -- the subject must be
irreversibly gone, and the append-only audit must not be. A purge that deleted
the row would satisfy the first and break the second; one that only flagged the
row would do the reverse.

The four refusals each have their own case, because they each have their own
code: three of them will never succeed on a retry, and an operator who sees only
"409" cannot tell which.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import sha256_token, subject_fingerprint, verify_password
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]


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


def _seed_subject(
    tenant_id: UUID,
    *,
    email: str = "leaver@example.com",
    disabled: bool = True,
    eligible: bool = True,
    legal_hold: bool = False,
) -> UUID:
    """A colleague who has left, at whatever point of the retention clock we need.

    Written straight through the migration connection: `POST /employees` and
    `POST /employees/{id}/deactivate` would both work, but the ninety-day clock
    cannot be waited out, so the row has to be aged here anyway -- and aging one
    field while creating the rest through routes would be the more confusing
    half-measure.
    """

    employee_id = uuid4()
    disabled_at = datetime.now(UTC) - timedelta(days=91)
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, status,
                display_name, employee_no, department,
                disabled_at, purge_eligible_at, legal_hold
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                employee_id,
                email,
                "argon2-placeholder",
                Jsonb([]),
                "disabled" if disabled else "active",
                "Departing Person",
                "E-0042",
                "Support",
                disabled_at if disabled else None,
                (
                    (disabled_at + timedelta(days=90))
                    if eligible
                    else datetime.now(UTC) + timedelta(days=30)
                )
                if disabled
                else None,
                legal_hold,
            ),
        )
        connection.commit()
    return employee_id


def _row(employee_id: UUID) -> dict[str, Any] | None:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT email, display_name, employee_no, department, password_hash,
                   credential_version, status, purged_at, subject_fingerprint,
                   permissions, legal_hold
              FROM employees WHERE id = %s
            """,
            (employee_id,),
        ).fetchone()


def _purge(
    client: TestClient,
    headers: dict[str, str],
    employee_id: UUID,
    *,
    reason: str = "left the company in June",
) -> Any:
    return client.post(
        f"/employees/{employee_id}/purge",
        json={"reason": reason},
        headers={**headers, "Idempotency-Key": f"purge-{uuid4()}"},
    )


# --------------------------------------------------------------------------
# The thing itself
# --------------------------------------------------------------------------


def test_the_subject_is_destroyed_but_the_row_and_its_audit_survive(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§6.4's two halves, which pull against each other.

    Everything that identifies the person is overwritten with a value nobody
    holds; the row, its id, and the audit trail that points at it all remain.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    before = _row(subject)
    assert before is not None
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 200, response.text
    after = _row(subject)
    assert after is not None
    assert after["email"] == f"purged+{subject}@purged.invalid"
    assert after["display_name"] == "(purged)"
    assert after["employee_no"] is None
    assert after["department"] is None
    assert after["purged_at"] is not None
    # The credential is not merely unusable -- it is a hash of something that
    # was generated and thrown away inside the request.
    assert after["password_hash"] != before["password_hash"]
    assert not verify_password(str(seeded_database["login_password"]), after["password_hash"])
    assert after["credential_version"] == before["credential_version"] + 1
    # §6.4's fingerprint: computed from the address being destroyed, so two
    # purged records of the same person still answer "same person" -- and
    # **keyed**, so nobody with a list of candidate addresses can ask it the
    # other way round (T154).
    assert after["subject_fingerprint"] == subject_fingerprint(
        key=str(settings.purge_fingerprint_key),
        tenant_id=tenant_id,
        email="leaver@example.com",
    )
    # The point of the change, asserted rather than described: the unkeyed
    # digest that used to land here must not.
    assert after["subject_fingerprint"] != sha256_token(f"{tenant_id}:leaver@example.com")


def test_the_original_address_becomes_reusable(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T126's other half.** A disabled account holds its address forever.

    Deactivation cannot release it -- the person may come back, and the audit
    trail needs the row. A purge is the point at which it can, and this is the
    assertion that says it does: the tenant can create a new employee at the
    same address afterwards.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(
        settings, seeded_database, ["employee.delete", "employee.create"]
    )
    assert _purge(client, headers, subject).status_code == 200

    recreated = client.post(
        "/employees",
        json={
            "email": "leaver@example.com",
            "display_name": "A New Person",
            "password": "a-sufficiently-long-password",
        },
        headers={**headers, "Idempotency-Key": f"create-{uuid4()}"},
    )

    assert recreated.status_code in {200, 201}, recreated.text


def test_roles_are_removed_and_sessions_revoked(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Authorization is not history; the audit row is where the history goes."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    role_id = uuid4()
    with _connection() as connection:
        # The role is created here rather than taken from the seed: system roles
        # are planted by `ensure_system_roles()` at migrate time and this
        # fixture's tenants have none, so reaching for one would have made the
        # case depend on a seeding detail instead of on the purge.
        connection.execute(
            "INSERT INTO roles(tenant_id, id, name, permissions) VALUES (%s, %s, %s, %s)",
            (tenant_id, role_id, f"support-{role_id}", Jsonb(["employee.read"])),
        )
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, subject, role_id),
        )
        connection.execute(
            "INSERT INTO employee_sessions("
            "tenant_id, id, employee_id, credential_version, expires_at)"
            " VALUES (%s, %s, %s, 1, now() + interval '1 hour')",
            (tenant_id, uuid4(), subject),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 200, response.text
    assert response.json()["result"]["roles_removed"] == 1
    assert response.json()["result"]["sessions_revoked"] == 1
    with _connection() as connection:
        roles_left = connection.execute(
            "SELECT count(*) AS total FROM employee_roles WHERE employee_id = %s", (subject,)
        ).fetchone()
        open_sessions = connection.execute(
            "SELECT count(*) AS total FROM employee_sessions"
            " WHERE employee_id = %s AND revoked_at IS NULL",
            (subject,),
        ).fetchone()
    assert roles_left is not None and roles_left["total"] == 0
    assert open_sessions is not None and open_sessions["total"] == 0


def test_the_answer_is_a_receipt_that_says_succeeded(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0056.** §6.4 asks for an asynchronous receipt and there is no
    executor for one (`C13`), so the work is done before this returns and the
    receipt says so. `accepted` would promise a worker that does not exist.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["status"] == "succeeded"
    assert body["receipt"]["resource_ref"]["resource_id"] == str(subject)
    assert response.headers["cache-control"] == "no-store, private"


def test_the_reason_is_recorded_and_the_fingerprint_is_not(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0055's criterion, applied to two values in one evidence document.**

    The reason passes all three tests -- operator's own words, not a verifier,
    and nothing else can answer "why" once the identity is gone. The fingerprint
    fails the second by name (「token / hash / fingerprint / 签名」), and an
    append-only table is the last place to put one.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    _purge(client, headers, subject, reason="requested erasure under GDPR")

    with _connection() as connection:
        rows = connection.execute(
            "SELECT action, reason_code, evidence FROM audit_events"
            " WHERE resource_type = 'employee' AND action = 'employee.delete'",
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "EMPLOYEE_PURGED"
    assert rows[0]["evidence"]["reason"] == "requested erasure under GDPR"
    stored = _row(subject)
    assert stored is not None
    assert str(stored["subject_fingerprint"]) not in str(rows[0]["evidence"])


def test_a_tombstone_cannot_be_given_an_identity_again(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F4.** The claim 026 makes must stay true afterwards.

    The migration's comment says the identifying columns are left holding values
    nobody holds. `PATCH /employees/{id}` had no guard at all, so one call put a
    real name back on a tombstone and made that sentence false. #16 and #17 both
    check `status`; this was the one employee-writing route that checked nothing.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(
        settings, seeded_database, ["employee.delete", "employee.update"]
    )
    assert _purge(client, headers, subject).status_code == 200

    revived = client.patch(
        f"/employees/{subject}",
        json={"display_name": "Back From The Dead", "department": "Support"},
        headers={**headers, "Idempotency-Key": f"revive-{uuid4()}"},
    )

    assert revived.status_code == 404, revived.text
    stored = _row(subject)
    assert stored is not None
    assert stored["display_name"] == "(purged)"
    assert stored["department"] is None


def test_the_audit_action_is_the_action_that_was_authorised(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F5.** `employee.delete`, not `employee.purge`.

    `employee.purge` is in no catalog and can never be granted -- the very thing
    this batch corrected ADR-0056 about. An append-only row whose `action`
    matches nothing an operator can search for is the wrong place to leave that
    string.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    _purge(client, headers, subject)

    with _connection() as connection:
        rows = connection.execute(
            "SELECT action, reason_code FROM audit_events"
            " WHERE resource_type = 'employee' AND reason_code = 'EMPLOYEE_PURGED'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "employee.delete"


# --------------------------------------------------------------------------
# The four refusals, each with its own code
# --------------------------------------------------------------------------


def test_an_active_employee_cannot_be_purged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§6.4 orders the two: deactivation starts the clock, purge ends it."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id, disabled=False, email="still-here@example.com")
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 409
    assert response.json()["code"] == "EMPLOYEE_NOT_DISABLED"
    assert _row(subject)["purged_at"] is None


def test_an_employee_still_inside_the_retention_window_cannot_be_purged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The ninety days exist to be observed, not to be skipped by an operator."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id, eligible=False, email="recent@example.com")
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 409
    assert response.json()["code"] == "PURGE_NOT_YET_ELIGIBLE"
    assert _row(subject)["purged_at"] is None


def test_a_legal_hold_refuses_the_purge(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T118 says nothing can set this flag today**, which is exactly why the
    guard has to be asserted rather than assumed: it is a guard over a field
    that is always `false` in production, so only a test can prove it works at
    all. Migration 026 deliberately withholds `UPDATE` on the column, so this
    route cannot clear its own guard.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id, legal_hold=True, email="held@example.com")
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 409
    assert response.json()["code"] == "EMPLOYEE_UNDER_LEGAL_HOLD"
    stored = _row(subject)
    assert stored["purged_at"] is None
    assert stored["legal_hold"] is True, "the route must not be able to clear its own guard"


def test_purging_twice_is_a_conflict(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The second call must not re-stamp `purged_at` or mint a second tombstone."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])
    assert _purge(client, headers, subject).status_code == 200
    first = _row(subject)

    second = _purge(client, headers, subject)

    assert second.status_code == 409
    assert second.json()["code"] == "EMPLOYEE_ALREADY_PURGED"
    assert _row(subject)["purged_at"] == first["purged_at"]


def test_purging_yourself_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The subject does not get to decide their own record is gone."""

    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, UUID(str(seeded_database["employee_a"])))

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_PURGE_SELF"


def test_a_reason_is_required_and_must_say_something(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§6.4 asks for the reason by name; a single character answers nothing."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    assert _purge(client, headers, subject, reason="x").status_code == 422
    empty = client.post(
        f"/employees/{subject}/purge",
        json={},
        headers={**headers, "Idempotency-Key": f"purge-{uuid4()}"},
    )
    assert empty.status_code == 422
    assert _row(subject)["purged_at"] is None


def test_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, not 403: a distinguishable answer is a cross-tenant existence oracle."""

    other = _seed_subject(
        UUID(str(seeded_database["tenant_b"])), email="elsewhere@example.com"
    )
    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, other)

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"
    assert _row(other)["purged_at"] is None, "another tenant's record was not touched"


def test_employee_update_alone_does_not_authorise_a_purge(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§6.4: deactivate and purge are **different actions**, not one flag.

    Whoever can rename a colleague must not thereby be able to erase one.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id)
    client, headers = _client_for(settings, seeded_database, ["employee.update"])

    response = _purge(client, headers, subject)

    assert response.status_code == 403
    assert _row(subject)["purged_at"] is None


@pytest.mark.security
def test_the_grant_stops_at_the_columns_this_route_writes(
    seeded_database: dict[str, Any],
) -> None:
    """The second defence, read out of the catalog rather than trusted.

    **`legal_hold` is the one to look at**: the route reads it as a guard, and
    the connection it runs on cannot write it. `permissions` is the other -- a
    purge has no business minting privilege, and 026 does not let it.
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

    assert {"email", "purged_at", "subject_fingerprint"} <= granted
    assert not granted & {"legal_hold", "permissions", "tenant_id", "id", "created_at"}


def test_a_deployment_without_the_key_refuses_instead_of_falling_back(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T154's failure mode, and the reason it is a 503 and not a fallback.**

    An unkeyed digest of `tenant_id:email` is reversible by hashing a list of
    candidate addresses -- both halves are guessable -- and §6.4 asks for an
    irreversible fingerprint. A route that quietly fell back to one would write
    a tombstone that looks correct and is not.

    **And it could never be corrected.** The purge destroys the address in the
    same statement that writes the fingerprint, so the value can never be
    recomputed: a row written under a fallback stays weak forever. Refusing
    loudly costs one failed request; falling back costs a permanent record.
    """

    from dataclasses import replace

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    scoped = replace(settings, purge_fingerprint_key=None)
    client, headers = _client_for(scoped, seeded_database, ["employee.delete"])
    leaver = _seed_subject(tenant_id, email="keyless@example.com")

    response = _purge(client, headers, leaver)

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "PURGE_FINGERPRINT_KEY_MISSING"
    row = _row(leaver)
    # Nothing was written: a refused purge must not leave a half-purged row.
    assert row is not None
    assert row["purged_at"] is None and row["subject_fingerprint"] is None


def test_the_same_person_in_two_tenants_gets_two_fingerprints() -> None:
    """The tenant is inside the message, not only the key.

    Without it, one address purged in two tenants would produce one value, and
    "were these the same person" would answer yes across a tenant boundary --
    which is a cross-tenant correlation this system is not entitled to make.
    """

    key = "a" * 32
    first = subject_fingerprint(key=key, tenant_id=uuid4(), email="same@example.com")
    second = subject_fingerprint(key=key, tenant_id=uuid4(), email="same@example.com")

    assert first != second


def test_the_fingerprint_still_fits_the_column() -> None:
    """026's `CHECK` is `^[0-9a-f]{64}$`, and HMAC-SHA256 hex is exactly that.

    Asserted because the change is to *how* the value is derived, not to what it
    looks like -- and a derivation that outgrew the column would fail at the
    moment of a purge, which is the worst possible moment to find out.
    """

    import re

    value = subject_fingerprint(key="k" * 32, tenant_id=uuid4(), email="x@example.com")

    assert re.fullmatch(r"[0-9a-f]{64}", value)


def test_an_employee_with_no_retention_clock_is_refused_and_not_crashed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The tenant guard's twin, and it has to be fixed in the same batch.

    **Independent review 2026-09-07, P2**: this branch used to be held by
    `purge_eligible_at is None or ...` in Python, and moving the comparison into
    SQL dropped it. `%s IS NOT NULL` gives PostgreSQL nothing to infer a type
    from, psycopg sends `None` with OID 0, and the statement fails to plan --
    `IndeterminateDatatype` -- so a refusal with a defined code became an
    unhandled 500. Still fail-closed, in that nothing is erased, but "refuse"
    and "crash" are not the same answer.

    The row this builds is not reachable through any route (both deactivation
    paths write `status`, `disabled_at` and `purge_eligible_at` together, and
    `employees_disabled_pair` pairs the last two). It is written straight to the
    database on purpose: **a defensive branch nobody can reach is exactly the
    kind that rots**, and this one had already rotted once.

    The two guards are literally identical SQL -- `test_the_eligibility_is_
    decided_in_sql_and_not_against_a_process_clock` counts the statement and
    requires exactly two of it -- so a fix to one that skipped the other would
    break the property that makes the rule readable as a rule.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    subject = _seed_subject(tenant_id, email="no-retention-clock@example.com")
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET disabled_at = NULL, purge_eligible_at = NULL WHERE id = %s",
            (subject,),
        )
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["employee.delete"])

    response = _purge(client, headers, subject)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PURGE_NOT_YET_ELIGIBLE"
    assert _row(subject)["purged_at"] is None
