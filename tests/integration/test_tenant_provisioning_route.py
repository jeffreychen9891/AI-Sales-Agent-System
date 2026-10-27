"""B1 #9 `POST /admin/tenants` + migration 030 (ADR-0069).

**The two cases that carry this batch are
`test_holding_the_permission_is_not_enough_without_promotion` and
`test_the_tenant_and_its_audit_row_arrive_together`.** The route itself is a
short insert; what makes it correct is that a platform action is closed to
tenant administrators who happen to hold the permission, and that provisioning
cannot happen without leaving a record.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _promoted(settings: Settings, tmp_path: Path, seeded: dict[str, Any]) -> Settings:
    """The deployment's allowlist, naming the seeded operator.

    A file on the host is the only thing that promotes anybody (K5 / C4), which
    is precisely the property the platform rule leans on -- nothing in a request
    can claim it.
    """

    allowlist = tmp_path / "superuser.allowlist"
    allowlist.write_text(
        f"{seeded['tenant_a']}:{seeded['employee_a']}\n", encoding="utf-8"
    )
    return replace(settings, superuser_allowlist_file=str(allowlist))


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


def _provision(client: TestClient, headers: dict[str, str], code: str) -> Any:
    return client.post(
        "/admin/tenants",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"code": code},
    )


def test_a_promoted_subject_provisions_a_tenant(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The happy path, and the id is the server's to choose.

    `201` and **no `Location`** -- see the assertion below. The caller cannot
    supply an id, so the body is the only place it can learn one.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.create"])
    code = f"acme-{uuid4().hex[:8]}"

    response = _provision(client, headers, code)

    assert response.status_code == 201, response.text
    receipt = response.json()
    validate_control_plane_contract("command-receipt", receipt)
    assert receipt["status"] == "succeeded"
    # **The receipt names what was made** (ADR-0070 §二). The code is not echoed
    # -- the caller supplied it -- and the id is the one thing only the server
    # knows, which is exactly what `resource_ref` is for.
    assert receipt["resource_ref"]["resource_type"] == "tenant_registry"
    assert UUID(receipt["resource_ref"]["resource_id"])
    # **A global resource carries a null tenant, and the key is present**
    # (ADR-0070 §三): an omitted key would be indistinguishable from a version
    # that forgot to send it.
    assert receipt["resource_ref"]["tenant_id"] is None
    # **No `Location`** (independent review P3-1): M1a has no item-level read
    # for a tenant, so the header would have named an address that 404s.
    assert "location" not in {name.lower() for name in response.headers}


def test_holding_the_permission_is_not_enough_without_promotion(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The case ADR-0069 §五 exists for.**

    `owner` is defined as every grantable action, so the first tenant
    administrator seeded after this catalog change would hold
    `tenant_registry.create` -- and without `PLATFORM_RESOURCES` they could
    provision tenants. The refusal is keyed on the *identity* the deployment
    granted, not on the permission, and it says so in its own code: an
    administrator who holds the grant and is still refused must not conclude
    that granting it again would help.
    """

    client, headers = _client_for(settings, seeded_database, ["tenant_registry.create"])

    response = _provision(client, headers, f"nope-{uuid4().hex[:8]}")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PLATFORM_ACTION_REQUIRES_SUPERUSER"


def test_promotion_without_the_permission_is_also_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The other half: being promoted is not a permission either.

    Both cases exist because a rule that required only one of the two would pass
    whichever single case was written for it.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant.read"])

    response = _provision(client, headers, f"nope-{uuid4().hex[:8]}")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_the_tenant_and_its_audit_row_arrive_together(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**T163, discharged for the first time -- and the database enforces it.**

    `require_obligations_discharged` had no caller until this route. What makes
    the discharge honest is that migration 030 writes the tenant and its audit
    row in one statement: there is no interleaving in which a tenant exists and
    its provisioning record does not.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.create"])
    code = f"audited-{uuid4().hex[:8]}"

    response = _provision(client, headers, code)

    assert response.status_code == 201, response.text
    created = UUID(response.json()["resource_ref"]["resource_id"])
    with _migration_connection() as connection:
        audit = connection.execute(
            "SELECT actor_id, action, resource_type, resource_id, reason_code, evidence::text"
            "  FROM audit_events WHERE tenant_id = %s",
            (created,),
        ).fetchall()

    assert len(audit) == 1
    row = audit[0]
    assert row["action"] == "tenant_registry.create"
    assert row["resource_type"] == "tenant_registry"
    assert row["resource_id"] == created
    assert row["actor_id"] == UUID(str(seeded_database["employee_a"]))
    # The record belongs to the tenant it describes, and names who reached in.
    assert code in row["evidence"]
    assert str(seeded_database["tenant_a"]) in row["evidence"]


def test_a_duplicate_code_is_a_conflict_and_creates_nothing(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """409, and -- the part worth asserting -- no orphan audit row.

    The tenant insert and the audit insert are one statement, so a refused
    provisioning must leave neither. A version that wrote the audit first would
    pass the happy path and leave a record of a tenant that does not exist.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.create"])
    code = f"twice-{uuid4().hex[:8]}"

    first = _provision(client, headers, code)
    second = _provision(client, headers, code)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "TENANT_CODE_TAKEN"
    # **The id is deliberately not here** (T167): `ErrorEnvelope` is
    # `additionalProperties: false` on purpose, so a refusal carries a code and
    # nothing else. The recovery gap that leaves is registered, not hidden.
    assert "tenant_id" not in second.json()
    with _migration_connection() as connection:
        tenants = connection.execute(
            "SELECT count(*) AS total FROM tenants WHERE code = %s", (code,)
        ).fetchone()
        audits = connection.execute(
            "SELECT count(*) AS total FROM audit_events WHERE action = 'tenant_registry.create'"
            "   AND evidence->>'code' = %s",
            (code,),
        ).fetchone()
    assert tenants is not None and tenants["total"] == 1
    assert audits is not None and audits["total"] == 1


def test_a_malformed_code_is_refused_before_the_database_sees_it(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """422 with the field named, not a 500 from `001`'s check constraint.

    The pattern is restated on the request model rather than left to the column,
    because the difference between the two is what the caller is told.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.create"])

    for bad in ("AB", "-leading", "Uppercase", "with space"):
        response = _provision(client, headers, bad)
        assert response.status_code == 422, (bad, response.text)


def test_the_platform_action_is_not_in_any_tenant_role(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The complement of the route-level refusal.**

    `authorize()` refusing is the safety net; not granting it is the honest
    answer. A role carrying a permission its holder can never use is T98's
    shape -- stored, shown to an administrator, denied on use.
    """

    del settings, seeded_database
    from fastagent_v2.runtime.system_roles import system_role_permissions

    roles = system_role_permissions()

    for name, actions in roles.items():
        assert "tenant_registry.create" not in actions, name


def test_the_action_really_carries_an_audit_obligation() -> None:
    """**Independent review P2-3**, and a probe is why this is here.

    The route calls `require_obligations_discharged(decision, {"audit"})` and
    the batch's headline claim is that it discharges T163's obligation for the
    first time. The reviewer deleted that call and **all eight cases stayed
    green** -- because nothing asserted that the decision carries the obligation
    at all. If `authorize()` ever stopped attaching it, the discharge would
    become a no-op and every case would still pass.

    Asserted against the authorizer rather than the route, because that is where
    the obligation is decided; the route's job is only to claim it.
    """

    from datetime import UTC, datetime, timedelta

    from fastagent_v2.runtime.auth import AuthContext, Subject, TenantScope
    from fastagent_v2.runtime.authz import authorize, platform_resource_ref

    now = datetime.now(UTC)
    context = AuthContext(
        subject=Subject(
            id=uuid4(),
            type="superuser",
            status="active",
            credential_version=1,
            home_tenant_id=None,
        ),
        tenant_scope=TenantScope(kind="one", tenant_ids=frozenset({uuid4()})),
        permissions=frozenset({"tenant_registry.create"}),
        auth_method="bearer",
        session_id=uuid4(),
        correlation_id=uuid4(),
        authenticated_at=now,
        deadline_at=now + timedelta(seconds=30),
    )

    decision = authorize(
        context=context,
        action="tenant_registry.create",
        resource=platform_resource_ref(resource_type="tenant_registry"),
    )

    assert decision.allowed
    assert "audit" in {item.kind for item in decision.obligations}
