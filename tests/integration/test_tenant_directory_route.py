"""B1 #8 `GET /admin/tenants` + migration 031 (ADR-0066 / ADR-0069 / ADR-0073).

**The three cases that carry this batch are
`test_the_directory_shows_a_tenant_the_caller_does_not_belong_to`,
`test_the_read_leaves_a_platform_audit_row` and
`test_the_row_does_not_go_to_the_tenant_audit_table`.** The route is a short
select; what makes it correct is that a platform read crosses the tenant
boundary *by design* while every other read may not, and that a read which
discloses the whole directory leaves a record -- in the table that can hold one.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import MAX_PAGE_ITEMS, create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

#: The four columns ADR-0073 §四 ruled, and nothing else. Written as a set so a
#: fifth key added by a later `SELECT *` fails here rather than reaching a
#: client.
DIRECTORY_KEYS = {"id", "code", "status", "created_at"}


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
    """The deployment's allowlist, naming the seeded operator (K5 / C4)."""

    allowlist = tmp_path / "superuser.allowlist"
    allowlist.write_text(f"{seeded['tenant_a']}:{seeded['employee_a']}\n", encoding="utf-8")
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


def _platform_audit_rows(action: str) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT actor_id, action, resource_type, resource_id, reason_code, evidence"
            "  FROM platform_audit_events WHERE action = %s",
            (action,),
        ).fetchall()


def test_a_promoted_subject_reads_the_directory(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The happy path: a `Page<T>` of directory entries, four columns each."""

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.list"])

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("page", body)
    assert body["items"]
    for item in body["items"]:
        assert set(item) == DIRECTORY_KEYS, item
    codes = {item["code"] for item in body["items"]}
    assert {"tenant-a", "tenant-b"} <= codes
    # A directory of every tenant on the deployment does not belong in a shared
    # cache.
    assert response.headers["cache-control"] == "no-store, private"


def test_the_directory_shows_a_tenant_the_caller_does_not_belong_to(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**What makes this a platform read rather than a tenant read** (ADR-0066).

    The caller is authenticated into `tenant-a` and their `tenant_scope` covers
    exactly that one tenant. Every other read on this surface is filtered by
    that scope; this one deliberately is not, because the resource is the
    registry rather than any tenant in it.

    Written as its own case because the obvious wrong implementation -- filter
    by the caller's tenant, as every neighbouring list route does -- would
    answer `200` with a one-row page and pass a happy-path assertion that only
    checked the shape.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.list"])

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 200, response.text
    entries = {item["code"]: item for item in response.json()["items"]}
    assert "tenant-b" in entries
    assert entries["tenant-b"]["id"] == str(seeded_database["tenant_b"])


def test_holding_the_permission_is_not_enough_without_promotion(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The platform rule, on the read side.

    `tenant_registry.list` is a platform action, so it is not in any tenant role
    and cannot be granted through `PUT /roles/{id}/permissions` (ADR-0070 §四).
    A subject can still hold it -- the deployment can write it straight into
    `employees.permissions`, which is what this case does -- and the refusal
    must still stand, keyed on the identity rather than the grant.
    """

    client, headers = _client_for(settings, seeded_database, ["tenant_registry.list"])

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PLATFORM_ACTION_REQUIRES_SUPERUSER"


def test_promotion_without_the_permission_is_also_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The other half. Both exist because a rule needing only one of the two
    conditions would pass whichever single case was written for it."""

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant.read"])

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_the_read_leaves_a_platform_audit_row(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**ADR-0066 §三, discharged -- and the row lands in `platform_audit_events`.**

    The evidence carries `returned`, which is the half that says how much was
    disclosed. Auditing a read without it records that somebody looked but not
    at how much, and the count is not recoverable afterwards: no route can say
    how many tenants existed at that moment (ADR-0055 §二, third condition).
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.list"])

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 200, response.text
    rows = _platform_audit_rows("tenant_registry.list")
    assert len(rows) == 1
    row = rows[0]
    assert row["resource_type"] == "tenant_registry"
    # The directory has no id of its own; naming one of its rows would be a guess.
    assert row["resource_id"] is None
    assert row["reason_code"] == "TENANT_REGISTRY_LISTED"
    assert row["actor_id"] == UUID(str(seeded_database["employee_a"]))
    assert row["evidence"]["actor_tenant"] == str(seeded_database["tenant_a"])
    assert row["evidence"]["returned"] == len(response.json()["items"])


def test_the_row_does_not_go_to_the_tenant_audit_table(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**ADR-0073 §三 and §六, asserted rather than argued.**

    The rejected alternatives both end with a row in `audit_events`: widen its
    `tenant_id` to nullable, or file the event under the actor's own tenant. A
    later edit that quietly takes either road would leave every other case in
    this file green -- the route would still answer 200, and an audit row would
    still exist somewhere.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.list"])

    assert client.get("/admin/tenants", headers=headers).status_code == 200
    with _migration_connection() as connection:
        stray = connection.execute(
            "SELECT count(*) AS total FROM audit_events WHERE action = %s",
            ("tenant_registry.list",),
        ).fetchone()

    assert stray is not None and stray["total"] == 0


def test_a_refused_page_leaves_no_audit_row(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**409 writes nothing, and this is what holds the two statements in order.**

    The route's docstring says a refusal must not leave a record, because "a
    record saying the directory was read would be false" -- the caller received
    nothing. Independent review probe M3 moved the audit write *above*
    `_page_or_refuse` and **all nine cases stayed green**: the ordering was a
    property the code happened to have, not one anything held.

    After that mutation a 409 leaves a row claiming `returned = 200`, which is
    the exact shape ADR-0055 §五 refuses -- an audit entry asserting something
    that did not happen.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _client_for(scoped, seeded_database, ["tenant_registry.list"])
    with _migration_connection() as connection:
        # One more than a page. The directory already holds the two seeded
        # tenants, so this is comfortably over the limit either way.
        connection.execute(
            "INSERT INTO tenants(id, code)"
            " SELECT gen_random_uuid(), 'bulk-' || step"
            "   FROM generate_series(1, %s) AS step",
            (MAX_PAGE_ITEMS,),
        )
        connection.commit()

    response = client.get("/admin/tenants", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PAGE_LIMIT_EXCEEDED"
    assert _platform_audit_rows("tenant_registry.list") == []


def test_the_application_role_cannot_touch_the_new_table_directly(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0073 §二 says the only writer is a named function. This is the proof.**

    `platform_audit_events` grants `fastagent_app` nothing at all -- not
    `INSERT`, and deliberately not `SELECT`, `UPDATE` or `DELETE` either: an
    audit table the application can rewrite is not append-only
    (`unified-authorization-layer.md` §312), and one it can read back is a way to
    ask "who has been looking" without holding any permission to.

    Asserted as four separate statements because a single `INSERT` case would
    stay green if a later migration granted the other three -- and "the table
    denies everything" is the property the design leans on, not "the table
    denies inserts".

    **And asserted twice, on purpose.** A reverse probe granted `INSERT` on the
    live database and every statement below still raised: with RLS enabled and
    no policy, a row this role inserts violates row-level security, and
    PostgreSQL reports *that* as `42501` too -- the same class as a missing
    grant. So the loop alone cannot tell "not granted" from "granted and refused
    by RLS", which is two defences producing one symptom. The property it claims
    is still true, and the second half below is what makes the grant itself
    falsifiable: `has_table_privilege` answers about the grant and about nothing
    else.
    """

    del seeded_database
    with _migration_connection() as connection:
        granted = connection.execute(
            "SELECT privilege,"
            "       has_table_privilege('fastagent_app', 'platform_audit_events', privilege)"
            "         AS held"
            "  FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS privilege",
        ).fetchall()
    assert granted and all(not row["held"] for row in granted), granted

    database = Database(settings.database_url)
    forbidden = (
        "SELECT count(*) FROM platform_audit_events",
        "INSERT INTO platform_audit_events(id, action, resource_type, correlation_id,"
        " reason_code) VALUES (gen_random_uuid(), 'x.y', 'x', gen_random_uuid(), 'X')",
        "UPDATE platform_audit_events SET reason_code = 'X'",
        "DELETE FROM platform_audit_events",
    )
    for statement in forbidden:
        with pytest.raises(InsufficientPrivilege), database.transaction(
            tenant_id=None
        ) as connection:
            connection.execute(statement)


def test_the_listing_action_is_not_in_any_tenant_role(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The complement of the route-level refusal, for the read action.

    B1 #9 asserted this for `tenant_registry.create`; the same claim about
    `.list` had no case, and `owner` is recomputed from the catalogue on every
    deployment -- so this is the tripwire for a future change that makes a
    platform action grantable again.
    """

    del settings, seeded_database
    from fastagent_v2.runtime.system_roles import system_role_permissions

    for name, actions in system_role_permissions().items():
        assert "tenant_registry.list" not in actions, name


def test_the_listing_action_really_carries_an_audit_obligation() -> None:
    """The discharge above is a claim; this is what makes it mean something.

    `require_obligations_discharged` cannot verify that a row was written -- and
    symmetrically, a route can discharge an obligation the authorizer never
    attached. B1 #9's reviewer proved that by deleting the discharge call and
    watching nothing turn red. Asserted against `authorize()`, because that is
    where the obligation is decided.
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
        permissions=frozenset({"tenant_registry.list"}),
        auth_method="bearer",
        session_id=uuid4(),
        correlation_id=uuid4(),
        authenticated_at=now,
        deadline_at=now + timedelta(seconds=30),
    )

    decision = authorize(
        context=context,
        action="tenant_registry.list",
        resource=platform_resource_ref(resource_type="tenant_registry"),
    )

    assert decision.allowed
    assert "audit" in {item.kind for item in decision.obligations}
