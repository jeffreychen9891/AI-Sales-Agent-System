"""CP-3: the role/permission cluster (freeze B1 #22-#26).

The cluster's whole subject is authorization, so what most of these assert is a
**refusal**: an unknown permission code, a system role, the last role that can
hand out permissions, a role somebody still holds.

The one that is not a refusal is
`test_creating_a_role_does_not_change_what_anybody_may_do`, and it is the most
important one here. A role created by these routes grants nothing to nobody --
`authorize()` reads `employees.permissions` and nothing reads `roles` -- and
that will stay true until route freeze B1 #16 wires assignment. An operator who
believes otherwise has been handed a boundary that is not there, so the belief
is pinned by a test rather than only by a docstring.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.authz import assignable_permissions
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

CATALOG = (
    Path(__file__).resolve().parents[2]
    / "packages/contracts/manifests/resource-catalog.json"
)
ADMIN_PERMISSIONS = [
    "role.list",
    "role.create",
    "role.update",
    "role.delete",
    "permission.list",
]


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


def _seed_role(
    *,
    tenant_id: UUID,
    name: str,
    permissions: list[str],
    is_system: bool = False,
) -> UUID:
    """Insert a role directly.

    Used for the cases the routes cannot produce: a **system** role (`is_system`
    is not settable from the wire on purpose -- a client that could mint one
    could mint a role the delete route refuses to remove) and the starting state
    for the last-administrator guard.
    """

    role_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO roles(tenant_id, id, name, permissions, is_system)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (tenant_id, role_id, name, Jsonb(sorted(permissions)), is_system),
        )
        connection.commit()
    return role_id


def _assign(*, tenant_id: UUID, employee_id: UUID, role_id: UUID) -> None:
    """Write `employee_roles` directly.

    **A route does this now** -- freeze B1 #16, `PUT /employees/{id}/roles` --
    and its own file tests it end to end. This stays because the cases here are
    about the *role* routes, and reaching their guards through a second route
    would make a failure in either one look like a failure in the other.
    """

    with _connection() as connection:
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, employee_id, role_id),
        )
        connection.commit()


def _seed_colleague(tenant_id: UUID) -> UUID:
    """A second employee in this tenant, so a role can be held by somebody else.

    Since B1 #16 the last-administrator guard counts **people**, so "there is
    another administrator" needs another person to be true. Written directly
    rather than through `POST /employees` for this file's reason: these cases
    are about the role routes, and reaching their setup through a second route
    would make a failure in either look like a failure in the other.
    """

    employee_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            ) VALUES (%s, %s, %s, 'not-a-real-hash', '[]'::jsonb, 'Colleague')
            """,
            (tenant_id, employee_id, f"colleague-{employee_id}@example.com"),
        )
        connection.commit()
    return employee_id


def _client_holding_role(
    settings: Settings, seeded: dict[str, Any], role_id: UUID
) -> tuple[TestClient, dict[str, str]]:
    """Log in as somebody whose permissions come **only** from a role.

    Needed by every last-administrator case since B1 #16, and the reason is the
    guard's new definition. `_administrator_exists` asks whether a *person*
    still holds `role.update`; a caller who holds it in `employees.permissions`
    is such a person, so stripping the role they were aiming at leaves them an
    administrator and the guard correctly does not fire. To reach the refusal
    the caller's only claim to `role.update` has to be the thing being taken
    away -- which is also the real lockout, rather than a simulation of it.

    The column is emptied on purpose: leaving yesterday's permissions there is
    what would make these cases pass without testing anything.
    """

    tenant_id = UUID(str(seeded["tenant_a"]))
    employee_id = UUID(str(seeded["employee_a"]))
    _grant(tenant_id=tenant_id, employee_id=employee_id, permissions=[])
    _assign(tenant_id=tenant_id, employee_id=employee_id, role_id=role_id)
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


def _create(client: TestClient, headers: dict[str, str], **body: Any):
    return client.post(
        "/roles", json=body, headers={**headers, "Idempotency-Key": f"role-{uuid4()}"}
    )


# --------------------------------------------------------------------------
# GET /permissions -- freeze B1 #26
# --------------------------------------------------------------------------


def test_the_permission_list_is_generated_from_the_catalog_not_from_a_second_list(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-029`'s whole requirement: generated, not copied.

    Asserted against the catalog **file** rather than against
    `assignable_permissions()`, which is the function under test. Comparing a
    function to itself is the shape this repository has already shipped twice
    and caught twice.
    """

    client, headers = _client_for(settings, seeded_database, ["permission.list"])
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    body = client.get("/permissions", headers=headers).json()

    validate_control_plane_contract("page", body)
    served = {item["action"] for item in body["items"]}
    # Every served permission is a catalogued action of the resource it names.
    for item in body["items"]:
        assert item["action"] in catalog[item["resource"]]["actions"]
        assert item["tenant_owned"] == catalog[item["resource"]]["tenant_owned"]
    # ...and this batch's own actions are in it, which is what makes the
    # comparison above more than vacuously true on an empty list.
    assert {"role.create", "role.update", "role.delete", "permission.list"} <= served


def test_permissions_that_authorize_could_never_allow_are_not_offered(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The bootstrap actions stay out.

    `tenant.resolve_for_login` and friends have verbs outside `ACTION_VERBS` on
    purpose: they name paths that run before there is a subject to authorize,
    and `authorize()` refuses them with `ACTION_UNKNOWN`. Offering one as a
    permission a role may hold would put a capability in an administrator's list
    that can never take effect -- which reads as granted and behaves as denied.
    """

    client, headers = _client_for(settings, seeded_database, ["permission.list"])

    listing = client.get("/permissions", headers=headers).json()["items"]
    served = {item["action"] for item in listing}

    assert "tenant.resolve_for_login" not in served
    assert "channel_config.resolve_for_auth" not in served
    assert "inbox_event.consume" not in served
    # The catalog does hold them, so their absence is this route's doing.
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    assert "tenant.resolve_for_login" in catalog["tenant"]["actions"]


# --------------------------------------------------------------------------
# POST /roles -- freeze B1 #23
# --------------------------------------------------------------------------


def test_a_permission_the_catalog_does_not_know_is_refused_and_named(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-026`: "不能写未知 permission".

    A stored permission the catalog does not know reads to an administrator as a
    granted capability and behaves as a denied one, and nothing surfaces the
    difference until somebody relies on it.

    The unknown codes are named back. Safe here specifically because the full
    list is already readable at `GET /permissions`: the error reveals nothing a
    caller could not fetch, and swallowing it would leave them guessing at a typo.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create"])

    response = _create(
        client,
        headers,
        name="support",
        permissions=["role.list", "role.manage_all", "tenant.resolve_for_login"],
    )

    assert response.status_code == 422
    # A bare code. The first version returned an object naming the unrecognised
    # entries, which was a third error shape on a surface that had just been cut
    # down to two (independent review 2026-09-04, P2-3). Naming them is still
    # right and still owed -- as `ErrorEnvelope.field_errors`, recorded on T87.
    assert response.json()["code"] == "PERMISSION_NOT_IN_CATALOG"


def test_a_created_role_stores_a_sorted_deduplicated_set(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Submitting a code twice is a client's redundancy, not a claim.

    Sorting is what makes "did this change anything" answerable later: two
    equivalent permission sets have to compare equal in the database, or every
    audit of a permission change starts with normalising it by hand.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create"])

    created = _create(
        client, headers, name="support", permissions=["role.list", "role.list", "permission.list"]
    )

    # The create command answers with a `CommandReceipt` (ruling, 2026-09-04),
    # so what was stored is read back rather than echoed. That is the point of
    # the ruling: a receipt names what was created, and the row is the row.
    validate_control_plane_contract("command-receipt", created.json())
    assert created.json()["status"] == "succeeded"
    assert created.json()["resource_ref"]["resource_type"] == "role"

    client_read, headers_read = _client_for(settings, seeded_database, ["role.list"])
    body = client_read.get("/roles", headers=headers_read).json()["items"][0]

    assert body["permissions"] == ["permission.list", "role.list"]
    assert body["is_system"] is False
    assert set(body) == {
        "role_id",
        "name",
        "description",
        "permissions",
        "is_system",
        "created_at",
        "updated_at",
    }


def test_a_client_cannot_mint_a_system_role(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`is_system` is not on the wire, and `extra="forbid"` is what says so.

    A client that could set it could create a role the update and delete routes
    refuse to touch -- a permission set nobody can withdraw.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create"])

    response = _create(client, headers, name="sneaky", permissions=[], is_system=True)

    assert response.status_code == 422


def test_two_roles_cannot_share_a_name(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The unique constraint refuses, not a pre-check.

    Two requests naming the same role can arrive together; a read before the
    write lets both through and leaves the tenant with two roles whose names an
    administrator cannot tell apart.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create"])
    assert _create(client, headers, name="support", permissions=[]).status_code == 201

    second = _create(client, headers, name="support", permissions=["role.list"])

    assert second.status_code == 409
    assert second.json()["code"] == "ROLE_NAME_ALREADY_USED"


# --------------------------------------------------------------------------
# PUT /roles/{role_id}/permissions -- freeze B1 #24
# --------------------------------------------------------------------------


def test_the_permission_set_is_replaced_rather_than_merged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A partial update of an authorization set has an effect the client cannot predict.

    `PUT` with the whole set means the client is stating what it believes the
    answer should be -- which is also what makes the resulting audit line
    readable a year later.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create", "role.update"])
    # The create command answers with a receipt, so the new role's id comes from
    # `resource_ref` -- which is the receipt's job: naming what was created.
    role_id = _create(
        client, headers, name="support", permissions=["role.list", "permission.list"]
    ).json()["resource_ref"]["resource_id"]

    body = client.put(
        f"/roles/{role_id}/permissions",
        json={"permissions": ["role.list"]},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    ).json()

    assert body["permissions"] == ["role.list"], "permission.list must be gone, not merged"


def test_a_system_role_cannot_be_rewritten(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Refused on a column, not on a name prefix.

    A refusal that depends on a string is one rename away from not refusing, and
    the rename would look like housekeeping.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(
        tenant_id=tenant_id, name="platform-admin", permissions=ADMIN_PERMISSIONS, is_system=True
    )
    client, headers = _client_for(settings, seeded_database, ["role.update"])

    response = client.put(
        f"/roles/{role_id}/permissions",
        json={"permissions": []},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "SYSTEM_ROLE_IS_IMMUTABLE"


def test_the_last_role_that_can_hand_out_permissions_cannot_be_stripped(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """"保护最后管理员", and since B1 #16 the definition is a **person**.

    Whoever holds `role.update` can rewrite any role, their own included. Remove
    the last copy and the tenant can no longer grant anything to anybody -- a
    lockout that takes direct database access to undo.

    **What changed with B1 #16 (T92).** The guard used to ask "does another
    *role* carry `role.update`", because `employee_roles` was empty by
    construction and "who holds it" had no answer. Now it asks whether anybody
    actually holds it, through the column or through a role -- the union login
    resolves. So this case has to arrange the real lockout: the caller's only
    claim to `role.update` is the role they are stripping. Under the old guard
    this setup was untestable; under the new one, a caller holding `role.update`
    in their column would (correctly) not be refused at all.

    By capability rather than by a role named "admin", because a tenant may have
    called it something else and the guard has to hold anyway.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    client, headers = _client_holding_role(settings, seeded_database, role_id)

    response = client.put(
        f"/roles/{role_id}/permissions",
        json={"permissions": ["role.list"]},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_ROLE_PROTECTED"


def test_it_is_the_last_one_that_is_protected_not_every_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half of the guard, and the half that proves it is not a blanket refusal.

    With a second role still carrying `role.update`, stripping the first is an
    ordinary permission change. A guard that refused both would be indistinguishable
    from "administrators can never be demoted", which is not the rule.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    first = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    second = _seed_role(tenant_id=tenant_id, name="co-owner", permissions=ADMIN_PERMISSIONS)
    # The second role is **held by somebody** (B1 #16, T92). A role nobody holds
    # is no longer a second administrator, so seeding it and leaving it
    # unassigned would now make this case test the refusal rather than the
    # permission it was written for.
    _assign(tenant_id=tenant_id, employee_id=_seed_colleague(tenant_id), role_id=second)
    client, headers = _client_holding_role(settings, seeded_database, first)

    response = client.put(
        f"/roles/{first}/permissions",
        json={"permissions": ["role.list"]},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 200
    assert response.json()["permissions"] == ["role.list"]


def test_another_tenants_admin_role_does_not_count_as_a_second_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS through the guard, which is where an isolation bug would be invisible.

    If the "is there another admin role" query escaped the tenant, this tenant
    would be allowed to strip its last one because somebody *else* still had
    theirs -- and the resulting lockout would look like a correct decision.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    role_a = _seed_role(tenant_id=tenant_a, name="owner", permissions=ADMIN_PERMISSIONS)
    role_b = _seed_role(tenant_id=tenant_b, name="owner", permissions=ADMIN_PERMISSIONS)
    # **Held**, by tenant B's own employee: since B1 #16 the guard counts people,
    # so an unheld role in the other tenant could not be mistaken for one here
    # even by a broken query. Assigning it is what keeps this case able to fail.
    _assign(
        tenant_id=tenant_b,
        employee_id=UUID(str(seeded_database["employee_b"])),
        role_id=role_b,
    )
    client, headers = _client_holding_role(settings, seeded_database, role_a)

    response = client.put(
        f"/roles/{role_a}/permissions",
        json={"permissions": []},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "LAST_ADMIN_ROLE_PROTECTED"


# --------------------------------------------------------------------------
# DELETE /roles/{role_id} -- freeze B1 #25
# --------------------------------------------------------------------------


def test_a_role_somebody_still_holds_is_not_deleted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-028`'s reference check, asked of the real table.

    `employee_roles` is written by no route in M1a -- freeze B1 #16 is the route
    that will -- so this check finds nothing in production today. It is written
    now rather than deferred because a delete route built against an empty world
    goes into the assignment batch as an already-passing route nobody re-reads.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    role_id = _seed_role(tenant_id=tenant_id, name="support", permissions=["role.list"])
    _assign(tenant_id=tenant_id, employee_id=employee_id, role_id=role_id)
    client, headers = _client_for(settings, seeded_database, ["role.delete"])

    response = client.delete(f"/roles/{role_id}", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "ROLE_STILL_ASSIGNED"


def test_an_unheld_role_is_deleted_and_the_row_is_gone(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Deleting is a delete, not a flag.

    §6.4's stop/purge distinction is about tenants and employees -- things with
    history worth keeping. A role is a permission set; one nobody holds has
    nothing to preserve, and a tombstone would only be a name that cannot be
    reused.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(tenant_id=tenant_id, name="support", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["role.delete", "role.create"])

    assert client.delete(f"/roles/{role_id}", headers=headers).status_code == 204

    with _connection() as connection:
        remaining = connection.execute(
            "SELECT 1 FROM roles WHERE tenant_id = %s AND id = %s", (tenant_id, role_id)
        ).fetchone()
    assert remaining is None
    # ...and the name is free again, which a tombstone would not have left.
    assert _create(client, headers, name="support", permissions=[]).status_code == 201


def test_a_system_role_cannot_be_deleted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(
        tenant_id=tenant_id, name="platform-admin", permissions=["role.list"], is_system=True
    )
    client, headers = _client_for(settings, seeded_database, ["role.delete"])

    response = client.delete(f"/roles/{role_id}", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "SYSTEM_ROLE_IS_IMMUTABLE"


def test_deleting_an_admin_role_nobody_holds_is_allowed_and_that_is_the_change(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**This case used to assert a 409, and B1 #16 turned it into a 204** (T92).

    The old rule was "a role carrying `role.update` cannot be deleted if it is
    the last one carrying it", and it was the best available while
    `employee_roles` was empty: with nobody able to hold anything, a role was
    the only thing a capability could live in. It protected a tenant against
    losing a capability **nobody had**.

    Now the delete route refuses any role somebody holds -- `ROLE_STILL_ASSIGNED`,
    asserted by `test_a_role_somebody_still_holds_is_not_deleted` -- so a role
    that reaches the deletion is provably held by nobody, and deleting it cannot
    take `role.update` away from a person. The last-administrator check was
    therefore **removed from this route** rather than left as a branch that can
    never fire, which would read like protection and be tested by a case that
    cannot fail.

    What still protects the tenant is asserted two cases above and in
    `test_two_concurrent_strips_cannot_leave_a_tenant_without_an_administrator`:
    the guard now lives where a permission can actually be taken away from
    somebody.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(tenant_id=tenant_id, name="owner", permissions=ADMIN_PERMISSIONS)
    client, headers = _client_for(settings, seeded_database, ["role.delete", "role.update"])

    response = client.delete(f"/roles/{role_id}", headers=headers)

    assert response.status_code == 204, response.text
    with _connection() as connection:
        gone = connection.execute(
            "SELECT count(*) AS n FROM roles WHERE tenant_id = %s AND id = %s",
            (tenant_id, role_id),
        ).fetchone()["n"]
    assert gone == 0
    # And the tenant is not locked out, because the caller was never relying on
    # that role: they hold `role.update` in their own column. That is the fact
    # the old guard could not see.


def test_a_role_that_is_not_there_is_a_404_not_a_204(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A delete that silently succeeds on a missing row hides a typo in an id."""

    client, headers = _client_for(settings, seeded_database, ["role.delete"])

    assert client.delete(f"/roles/{uuid4()}", headers=headers).status_code == 404


# --------------------------------------------------------------------------
# Isolation, permission separation, and the honesty test
# --------------------------------------------------------------------------


def test_one_tenants_roles_are_not_in_another_tenants_catalog(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    _seed_role(
        tenant_id=UUID(str(seeded_database["tenant_a"])), name="a-only", permissions=["role.list"]
    )
    _seed_role(
        tenant_id=UUID(str(seeded_database["tenant_b"])), name="b-only", permissions=["role.list"]
    )
    client_a, headers_a = _client_for(settings, seeded_database, ["role.list"])
    client_b, headers_b = _client_for(settings, seeded_database, ["role.list"], tenant="b")

    names_a = [item["name"] for item in client_a.get("/roles", headers=headers_a).json()["items"]]
    names_b = [item["name"] for item in client_b.get("/roles", headers=headers_b).json()["items"]]

    assert names_a == ["a-only"]
    assert names_b == ["b-only"]


def test_another_tenants_role_id_is_a_404_not_a_403(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Naming something real must not be distinguishable from naming nothing.

    A 403 here would confirm the id exists, which is a working oracle for
    enumerating another tenant's roles one guess at a time.
    """

    other = _seed_role(
        tenant_id=UUID(str(seeded_database["tenant_b"])), name="b-only", permissions=["role.list"]
    )
    client, headers = _client_for(settings, seeded_database, ["role.update", "role.delete"])

    assert client.delete(f"/roles/{other}", headers=headers).status_code == 404
    assert (
        client.put(
        f"/roles/{other}/permissions",
        json={"permissions": []},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("GET", "/permissions", "role.list"),
        ("GET", "/roles", "permission.list"),
        ("POST", "/roles", "role.list"),
        ("PUT", "/roles/{role}/permissions", "role.delete"),
        ("DELETE", "/roles/{role}", "role.update"),
    ],
)
def test_each_route_needs_its_own_permission(
    settings: Settings,
    seeded_database: dict[str, Any],
    method: str,
    path: str,
    permission: str,
) -> None:
    """Holding a neighbouring permission grants none of the others.

    The pairs are chosen to be the tempting conflations: read implies list,
    update implies delete. In an authorization cluster those are not near
    misses -- `role.update` standing in for `role.delete` is exactly the
    over-broad grant the whole batch is about.
    """

    role_id = _seed_role(
        tenant_id=UUID(str(seeded_database["tenant_a"])), name="target", permissions=["role.list"]
    )
    client, headers = _client_for(settings, seeded_database, [permission])
    url = path.format(role=role_id)

    # Each body has to be *valid* for its own route: FastAPI validates the body
    # before the handler runs, so an invalid one returns 422 and the route's
    # authorization check is never reached -- a green 422 would look like a
    # passing permission test while proving nothing about permissions.
    body = {"POST": {"name": "x", "permissions": []}, "PUT": {"permissions": []}}.get(method)

    response = client.request(
        method,
        url,
        json=body,
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 403, response.text


def test_a_role_now_changes_what_somebody_may_do_and_only_from_the_next_login(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The honesty test, deliberately inverted** (T92's second landing item).

    Its previous form asserted the opposite -- that holding a role changed
    nothing -- and it was the most important case in this file for as long as
    that was true. T92 required it be *rewritten* when B1 #16 landed rather than
    left green by accident, because a test asserting "roles grant nothing" that
    somehow still passed after roles started granting things would be the
    quietest possible failure.

    So it now pins both halves of what actually happens:

    * the same session, holding a role that carries `role.delete`, is **still
      refused** -- permissions are minted into the access token at login, and
      that token was minted before the assignment existed;
    * a **new login** succeeds, because `POST /auth/login` resolves the union of
      `employees.permissions` and the roles the person holds.

    The second half is the wiring. The first half is the cost of putting
    permissions in a token, and it is why `PUT /employees/{id}/roles` revokes
    the target's sessions: without that, a *revoked* role would keep working
    until the token expired, which is the direction that must not be slow.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["role.create"])
    role_id = _create(
        client, headers, name="deleter", permissions=["role.delete"]
    ).json()["resource_ref"]["resource_id"]
    _assign(tenant_id=tenant_id, employee_id=employee_id, role_id=UUID(role_id))
    target = _seed_role(tenant_id=tenant_id, name="target", permissions=["role.list"])

    # The token predates the assignment, so it does not carry `role.delete`.
    assert client.delete(f"/roles/{target}", headers=headers).status_code == 403

    fresh = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert fresh.status_code == 200, fresh.text
    now_theirs = {"Authorization": f"Bearer {fresh.json()['access_token']}"}
    # `GET /auth/me` echoes what the token carries, so this names the union
    # rather than only observing its effect.
    assert "role.delete" in client.get("/auth/me", headers=now_theirs).json()["permissions"]

    assert client.delete(f"/roles/{target}", headers=now_theirs).status_code == 204


def test_the_assignable_list_and_the_route_agree(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One vocabulary, two consumers: the route that publishes it and the validator.

    They are the same function today. This asserts that a permission the route
    offers is a permission `POST /roles` will accept -- the failure mode being a
    list that advertises codes the create route then rejects, which reads to an
    administrator as a bug in their own request.
    """

    client, headers = _client_for(
        settings, seeded_database, ["permission.list", "role.create", "role.list"]
    )

    listing = client.get("/permissions", headers=headers).json()["items"]
    offered = [item["action"] for item in listing]

    assert offered == [item["action"] for item in assignable_permissions()]
    created = _create(client, headers, name="everything", permissions=offered)
    assert created.status_code == 201, created.text

    stored = client.get("/roles", headers=headers).json()["items"][0]
    assert stored["permissions"] == sorted(offered)


# --------------------------------------------------------------------------
# The audit trail (review round 1, P1-1) and the guard's concurrency
# (P1-2). Both fixes shipped without a single assertion -- the "25 passed"
# reported alongside them was the pre-existing 25, and deleting all three
# `_serialise_role_changes` calls would have left them green (round 2, R-1).
# --------------------------------------------------------------------------


def _audit_rows(tenant_id: UUID, role_id: UUID) -> list[dict[str, Any]]:
    """Read `audit_events` with the migration role.

    Runtime roles hold INSERT and nothing else on this table -- the append-only
    grant is a security birth condition -- so the test reads through the owner
    rather than relaxing it.
    """

    with _connection() as connection:
        return connection.execute(
            """
            SELECT actor_id, action, resource_type, resource_id, reason_code, evidence
              FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s
             ORDER BY created_at
            """,
            (tenant_id, role_id),
        ).fetchall()


def test_creating_a_role_is_written_down_with_what_it_granted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-027`/`C0A-028` require the audit; the first version wrote none.

    The evidence carries the permission set, not merely "a role was created".
    The question asked after the fact is always *what did they grant*, and a row
    that cannot answer it cost the same to write.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["role.create"])

    created = _create(client, headers, name="support", permissions=["role.list"])
    role_id = UUID(created.json()["resource_ref"]["resource_id"])

    rows = _audit_rows(tenant_id, role_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "role.create"
    assert row["resource_type"] == "role"
    assert UUID(str(row["actor_id"])) == employee_id
    assert row["evidence"]["permissions"] == ["role.list"]
    assert row["evidence"]["name"] == "support"


def test_replacing_permissions_records_both_sides_of_the_change(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Only the new set would make the reader go and find the old one -- which by
    then does not exist anywhere."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = _seed_role(
        tenant_id=tenant_id, name="support", permissions=["role.list", "permission.list"]
    )
    client, headers = _client_for(settings, seeded_database, ["role.update"])

    client.put(
        f"/roles/{role_id}/permissions",
        json={"permissions": ["role.list"]},
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    rows = _audit_rows(tenant_id, role_id)
    assert len(rows) == 1
    evidence = rows[0]["evidence"]
    assert rows[0]["action"] == "role.update"
    assert evidence["permissions_before"] == ["permission.list", "role.list"]
    assert evidence["permissions_after"] == ["role.list"]
    assert evidence["permissions_before"] != evidence["permissions_after"]


def test_a_deleted_role_leaves_behind_what_it_used_to_hold(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The row is gone after the DELETE. If the audit did not carry the
    permission set, nothing would."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_role(tenant_id=tenant_id, name="keeper", permissions=ADMIN_PERMISSIONS)
    role_id = _seed_role(tenant_id=tenant_id, name="doomed", permissions=["role.list"])
    client, headers = _client_for(settings, seeded_database, ["role.delete"])

    assert client.delete(f"/roles/{role_id}", headers=headers).status_code == 204

    with _connection() as connection:
        gone = connection.execute(
            "SELECT 1 FROM roles WHERE tenant_id = %s AND id = %s", (tenant_id, role_id)
        ).fetchone()
    assert gone is None

    rows = _audit_rows(tenant_id, role_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "role.delete"
    assert rows[0]["evidence"]["name"] == "doomed"
    assert rows[0]["evidence"]["permissions"] == ["role.list"]


def test_two_concurrent_strips_cannot_leave_a_tenant_without_an_administrator(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P1-2, driven the way the defect happens -- **on the route that can still cause it**.

    This case used to race two *deletions*. B1 #16 took the last-administrator
    check off the delete route (a role that reaches deletion is held by nobody),
    so racing deletions no longer races the guard: both would correctly succeed
    and the case would assert nothing. Racing two **strips** is the same defect
    on the route that still has the decision.

    Two admin roles, each held by a different person, two connections, one
    barrier. Without `_serialise_role_changes` each request's guard sees the
    other's still-uncommitted row, concludes "somebody else still holds
    `role.update`", and both commit -- leaving **zero administrators**.
    Sequential calls cannot show this: the second sees the first's committed
    write and refuses correctly with no lock at all.

    Shaped after `tests/e2e/test_m1a_cost.py`'s concurrent ceiling case, which is
    the repository's existing proof that this discipline is executable here.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    first = _seed_role(tenant_id=tenant_id, name="owner-a", permissions=ADMIN_PERMISSIONS)
    second = _seed_role(tenant_id=tenant_id, name="owner-b", permissions=ADMIN_PERMISSIONS)
    _assign(tenant_id=tenant_id, employee_id=_seed_colleague(tenant_id), role_id=second)
    # The caller's own claim to `role.update` is `first`, so each strip removes
    # one of the tenant's two administrators and the second must be refused.
    client, headers = _client_holding_role(settings, seeded_database, first)

    ready = threading.Barrier(2)
    codes: list[int] = []
    errors: list[BaseException] = []

    def attempt(role_id: UUID) -> None:
        # A client of its own: `TestClient` is threadsafe enough to share, but a
        # shared one would also share a connection pool slot and could serialise
        # the two requests by accident -- which is the thing under test.
        own = TestClient(create_app(settings))
        try:
            ready.wait(timeout=10)
            codes.append(
                own.put(
                    f"/roles/{role_id}/permissions",
                    json={"permissions": ["role.list"]},
                    headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
                ).status_code
            )
        except BaseException as error:  # noqa: BLE001 - reported after the join
            errors.append(error)

    threads = [threading.Thread(target=attempt, args=(role,)) for role in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert sorted(codes) == [200, 409], f"one strip must be refused, got {codes}"

    with _connection() as connection:
        remaining = connection.execute(
            """
            SELECT count(*) AS n
              FROM employees e
             WHERE e.tenant_id = %s AND e.status = 'active'
               AND EXISTS (
                     SELECT 1 FROM employee_roles er JOIN roles r
                       ON r.tenant_id = er.tenant_id AND r.id = er.role_id
                      WHERE er.tenant_id = e.tenant_id AND er.employee_id = e.id
                        AND r.permissions ? %s
                   )
            """,
            (tenant_id, "role.update"),
        ).fetchone()["n"]
    assert remaining >= 1, "a tenant must never be left with nobody who can grant anything"
