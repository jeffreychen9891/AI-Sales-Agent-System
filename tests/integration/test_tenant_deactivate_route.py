"""B1 #11 `POST /admin/tenants/{tenant_id}/deactivate` (ADR-0068 / ADR-0075).

**The three cases that carry this batch are
`test_a_promoted_subject_stops_another_tenant`,
`test_the_owner_of_a_tenant_cannot_stop_it` and
`test_the_other_tenant_is_untouched`.** The route is three `UPDATE`s; what makes
it correct is that the reach crosses a tenant boundary *because a deployment
promoted somebody and they gave a reason*, that holding the permission is never
enough, and that the blast radius stops at the named tenant.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
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

REASON = "merchant asked us to stop the account"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    """Write permissions straight into the row.

    **This is not a shortcut around a route** -- for `tenant.update` it is the
    only way there is. ADR-0075 makes it platform-only, so no role can carry it
    and `GET /permissions` does not offer it; the deployment writing the column
    is the sanctioned path, and this is that path.
    """

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _promoted(settings: Settings, tmp_path: Path, seeded: dict[str, Any]) -> Settings:
    allowlist = tmp_path / "superuser.allowlist"
    allowlist.write_text(f"{seeded['tenant_a']}:{seeded['employee_a']}\n", encoding="utf-8")
    return replace(settings, superuser_allowlist_file=str(allowlist))


def _login(client: TestClient, seeded: dict[str, Any], code: str, email: str) -> dict[str, str]:
    login = client.post(
        "/auth/login",
        json={"tenant_code": code, "email": email, "password": seeded["login_password"]},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _operator_a(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded["tenant_a"])),
        employee_id=UUID(str(seeded["employee_a"])),
        permissions=permissions,
    )
    client = TestClient(create_app(settings))
    return client, _login(client, seeded, "tenant-a", "operator-a@example.com")


def _seed_binding_for_b(seeded: dict[str, Any]) -> None:
    """`config-a` belongs to tenant-a; tenant-b needs a binding of its own.

    The fixture already publishes a profile release for **both** tenants, so
    this reuses tenant-b's rather than copying one across -- `channel_bindings`
    has a composite foreign key on `(tenant_id, profile_release_id)`, and a
    release borrowed from the other tenant would not satisfy it.
    """

    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded["tenant_b"],),
        ).fetchone()
        assert release is not None, "tenant-b has no profile release to bind to"
        connection.execute(
            "INSERT INTO channel_bindings(config_id, tenant_id, profile_release_id,"
            " current_token_hash) VALUES ('config-b', %s, %s, %s)",
            (seeded["tenant_b"], release["id"], "0" * 64),
        )
        connection.commit()


def _tenant_row(tenant_id: Any) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT status, disabled_at, purge_eligible_at, legal_hold FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return row


def _deactivate(
    client: TestClient, headers: dict[str, str], tenant_id: Any, reason: str = REASON
) -> Any:
    return client.post(
        f"/admin/tenants/{tenant_id}/deactivate",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"reason": reason},
    )


def test_a_promoted_subject_stops_another_tenant(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The first real consumer of ADR-0068's channel.**

    Everything the batch claims is visible in one response: the tenant is
    `disabled`, the retention clock starts 90 days out, and the counts say how
    much stopped.
    """

    _seed_binding_for_b(seeded_database)
    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])
    # A live session for the tenant being stopped, so "sessions revoked" is a
    # measurement rather than a zero that would pass either way.
    _login(client, seeded_database, "tenant-b", "operator-b@example.com")

    response = _deactivate(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    # **The receipt names the target, not the caller** -- the whole point of
    # taking it from the decision (ADR-0068 §四点五).
    assert body["receipt"]["resource_ref"]["tenant_id"] == str(seeded_database["tenant_b"])
    assert body["result"]["status"] == "disabled"
    assert body["result"]["sessions_revoked"] == 1
    assert body["result"]["bindings_stopped"] == 1

    row = _tenant_row(seeded_database["tenant_b"])
    assert row["status"] == "disabled"
    assert row["disabled_at"] is not None
    # §6.4's 90 days, checked as a span rather than a literal date.
    assert row["purge_eligible_at"] - row["disabled_at"] == timedelta(days=90)
    # **`legal_hold` is untouched** -- nothing in M1a may set it (T118), and a
    # deactivation quietly flipping it would be the shape that registration
    # exists to prevent.
    assert row["legal_hold"] is False


def test_the_owner_of_a_tenant_cannot_stop_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0075's whole reason for existing.**

    `tenant` is tenant-owned and `owner` is defined as every grantable action,
    so without a platform-only declaration this permission would land in every
    merchant administrator's role -- and a merchant could disable their own
    tenant, revoking their own sessions, with nothing in M1a able to undo it
    (B1 #10 left the milestone in ADR-0074).

    The caller here holds the permission and is **not** promoted, and is acting
    on their **own** tenant, so nothing about scope or reach is in play: the
    refusal is about identity alone.
    """

    client, headers = _operator_a(settings, seeded_database, ["tenant.update"])

    response = _deactivate(client, headers, seeded_database["tenant_a"])

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PLATFORM_ACTION_REQUIRES_SUPERUSER"
    assert _tenant_row(seeded_database["tenant_a"])["status"] == "active"


def test_promotion_without_the_permission_is_also_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The other half; a rule needing one of the two would pass a single case."""

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.read"])

    response = _deactivate(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_the_other_tenant_is_untouched(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The blast radius stops at the named tenant.**

    Three `UPDATE`s run in a transaction pinned to the target, and each carries
    its own `WHERE tenant_id = %s`. Either the pin or the predicates would be
    enough on a good day; this case is what notices when one of them is dropped
    -- the caller's own tenant is the one a mistake would reach first, because
    it is the tenant the connection used to be pinned to before ADR-0068.
    """

    _seed_binding_for_b(seeded_database)
    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    assert _deactivate(client, headers, seeded_database["tenant_b"]).status_code == 200

    assert _tenant_row(seeded_database["tenant_a"])["status"] == "active"
    with _migration_connection() as connection:
        live = connection.execute(
            "SELECT count(*) AS total FROM employee_sessions"
            " WHERE tenant_id = %s AND revoked_at IS NULL",
            (seeded_database["tenant_a"],),
        ).fetchone()
        binding = connection.execute(
            "SELECT status FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-a'",
            (seeded_database["tenant_a"],),
        ).fetchone()
    # The caller's own session is still live -- they are mid-request, and
    # stopping another tenant must not log them out.
    assert live is not None and live["total"] >= 1
    assert binding is not None and binding["status"] == "active"


def test_the_binding_is_stopped_but_its_credential_survives(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The narrow reading of 「撤销 channel/binding」, asserted so it is visible.**

    `POST /channel-bindings/{id}/deactivate` destroys the callback token,
    because stopping one binding *is* an external-channel action. Deactivating a
    tenant is a 90-day holding state that ends in a separate purge, so this
    route flips `status` -- which is where "no new inbox" actually lives, the
    ingress adapter refuses anything not `active` -- and leaves the credential
    alone.

    **Written down as a case rather than a comment** because it is the reversible
    half of a choice that cannot be reversed if taken the other way, and the
    batch's self-review registers the question rather than assuming the answer.
    """

    _seed_binding_for_b(seeded_database)
    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    assert _deactivate(client, headers, seeded_database["tenant_b"]).status_code == 200

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT status, current_token_hash FROM channel_bindings"
            " WHERE tenant_id = %s AND config_id = 'config-b'",
            (seeded_database["tenant_b"],),
        ).fetchone()
    assert row is not None
    assert row["status"] == "disabled"
    assert row["current_token_hash"] == "0" * 64


def test_a_second_call_is_a_conflict_and_moves_nothing(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """409, and -- the part worth asserting -- the retention clock does not move.

    Re-stamping `disabled_at` would quietly extend how long a merchant's data is
    kept, which is the reason B1 #18 made the same choice for employees.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    first = _deactivate(client, headers, seeded_database["tenant_b"])
    stamped = _tenant_row(seeded_database["tenant_b"])["disabled_at"]
    second = _deactivate(client, headers, seeded_database["tenant_b"])

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "TENANT_ALREADY_DISABLED"
    assert _tenant_row(seeded_database["tenant_b"])["disabled_at"] == stamped


def test_the_reach_is_recorded_with_its_reason(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**T163's other half: a cross-tenant reach discharged for the first time.**

    The audit row belongs to the tenant it happened *to*, the actor travels in
    `actor_id`, and the reason is in the evidence -- a reach that leaves no
    trace of why is the shape parity §5.2 exists to prevent.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    assert _deactivate(client, headers, seeded_database["tenant_b"]).status_code == 200

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT actor_id, action, resource_type, resource_id, reason_code, evidence"
            "  FROM audit_events WHERE tenant_id = %s AND action = 'tenant.update'",
            (seeded_database["tenant_b"],),
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["actor_id"] == UUID(str(seeded_database["employee_a"]))
    assert row["resource_type"] == "tenant"
    assert row["resource_id"] == UUID(str(seeded_database["tenant_b"]))
    assert row["reason_code"] == "TENANT_DEACTIVATED"
    assert row["evidence"]["reason"] == REASON
    assert row["evidence"]["actor_tenant"] == str(seeded_database["tenant_a"])


def test_a_reason_too_short_to_mean_anything_is_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """422 before the database sees it, and the tenant stays up."""

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    response = _deactivate(client, headers, seeded_database["tenant_b"], reason="x")

    assert response.status_code == 422, response.text
    assert _tenant_row(seeded_database["tenant_b"])["status"] == "active"


def test_the_action_is_not_in_any_tenant_role_or_on_the_permission_list(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**Both doors, the way ADR-0070 §四 shut them for platform resources.**

    `authorize()` refusing is the safety net; not offering the permission is the
    honest answer. A role carrying a permission its holder can never use is
    T98's shape -- stored, shown to an administrator, denied on use.
    """

    from fastagent_v2.runtime.system_roles import system_role_permissions

    for name, actions in system_role_permissions().items():
        assert "tenant.update" not in actions, name

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["permission.list"])
    listing = client.get("/permissions", headers=headers)
    assert listing.status_code == 200, listing.text
    offered = {item["action"] for item in listing.json()["items"]}
    assert "tenant.update" not in offered
    # The neighbouring read is still offered -- this is an action-level rule,
    # not a resource-level one, and `GET /tenants/current` depends on that.
    assert "tenant.read" in offered


def test_a_stopped_tenant_can_neither_stay_in_nor_come_back(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The first of §6.4's three effects, and nothing was watching it.**

    Independent review probe M3: deleting the `status != "active"` half of the
    tenant check in `/auth/login` left **all 1308 cases green**. That branch is
    older than this batch, but before this batch `status` was always `active`,
    so the property it guards was unreachable -- and a login check nobody tests
    is what the next refactor deletes.

    Everything else in this file measures the *inside* of the operation: row
    counts in the response, columns in the database. These two assertions are
    what the merchant actually experiences, and they are the whole content of
    "停用立即撤销 session ... 并阻止新 inbox".
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])
    # A token issued **before** the tenant is stopped, so the second assertion
    # is about revocation rather than about login.
    before = _login(client, seeded_database, "tenant-b", "operator-b@example.com")

    assert _deactivate(client, headers, seeded_database["tenant_b"]).status_code == 200

    still_in = client.get("/auth/me", headers=before)
    back_in = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-b",
            "email": "operator-b@example.com",
            "password": seeded_database["login_password"],
        },
    )

    assert still_in.status_code == 401, still_in.text
    assert back_in.status_code == 401, back_in.text


def test_stopping_a_tenant_that_does_not_exist_says_so(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**404, not "already disabled"** (independent review P2-1).

    A random id used to answer 409 `TENANT_ALREADY_DISABLED`, which is a
    sentence about a row that never existed. The cost is not disclosure -- it is
    an operator who mistypes an id, reads "I already stopped that one", stops
    looking, and leaves the tenant they meant to stop serving.

    Telling the two apart is safe for this audience and nowhere else: reaching
    the branch means the deployment promoted this subject by writing a file on
    the host, and `GET /admin/tenants` already lists every tenant to exactly
    that audience.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    response = _deactivate(client, headers, uuid4())

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "TENANT_NOT_FOUND"


def test_a_promoted_subject_stopping_their_own_tenant_logs_themselves_out(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The path ADR-0075 §二 allows and nothing covered** (review P3-2).

    Stopping one's *own* tenant is not a cross-tenant reach, so `authorize()`
    attaches no `reason_required` -- which means the route's
    `require_obligations_discharged(decision, {"audit", "reason_required"})`
    goes down its "discharged set is a superset" branch for the first time.

    And the operator disappears with the tenant: their own session is among the
    ones revoked, so the 200 they receive is the last thing that token does.
    **That is the property ADR-0075 §四.3 used to refuse "let merchants stop
    themselves"** -- unchanged here except that the deployment promoted this one.
    """

    scoped = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(scoped, seeded_database, ["tenant.update"])

    response = _deactivate(client, headers, seeded_database["tenant_a"])

    assert response.status_code == 200, response.text
    # The caller's own live session is in the count -- they are mid-request.
    assert response.json()["result"]["sessions_revoked"] >= 1
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_the_reach_carries_both_obligations() -> None:
    """A discharge is a claim; this is what makes the claim mean something.

    B1 #9's reviewer deleted a discharge call and watched nothing turn red.
    Asserted against `authorize()`, where the obligations are decided.
    """

    from datetime import UTC, datetime

    from fastagent_v2.runtime.auth import AuthContext, Subject, TenantScope
    from fastagent_v2.runtime.authz import authorize, tenant_resource_ref

    now = datetime.now(UTC)
    home, target = uuid4(), uuid4()
    context = AuthContext(
        subject=Subject(
            id=uuid4(),
            type="superuser",
            status="active",
            credential_version=1,
            home_tenant_id=home,
        ),
        tenant_scope=TenantScope(kind="one", tenant_ids=frozenset({home})),
        permissions=frozenset({"tenant.update"}),
        auth_method="bearer",
        session_id=uuid4(),
        correlation_id=uuid4(),
        authenticated_at=now,
        deadline_at=now + timedelta(seconds=30),
    )
    resource = tenant_resource_ref(resource_type="tenant", tenant_id=target)

    allowed = authorize(
        context=context,
        action="tenant.update",
        resource=resource,
        request_facts={"target_tenant_id": str(target), "reason": REASON},
    )
    refused = authorize(
        context=context,
        action="tenant.update",
        resource=resource,
        request_facts={"target_tenant_id": str(target)},
    )

    assert allowed.allowed
    assert {item.kind for item in allowed.obligations} == {"audit", "reason_required"}
    # **The tenant the route must open its transaction with** (ADR-0068 §四点五).
    assert allowed.authorized_tenant_id == target
    # Without a reason the same reach is refused -- the route cannot reach this
    # branch because its request model demands one, so it is asserted here.
    assert not refused.allowed
    assert refused.reason_code == "CROSS_TENANT_REASON_REQUIRED"
