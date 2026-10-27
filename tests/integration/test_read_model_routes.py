"""CP-2 second cluster: three read models (freeze B1 #7, #13, #20).

Read models are where an over-broad answer is cheapest to ship and hardest to
take back, so what is asserted here is mostly what these routes **do not**
return: another tenant's rows, an employee's permission list, a tenant id the
caller did not already have.

`GET /tenants/current` exists because the old system had an agent read the
tenant *list* and take the first row to learn which tenant it was in -- the
over-broad semantics §7.2 froze on purpose. The v2 shape can only answer for the
caller.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.api.app import MAX_PAGE_ITEMS, create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.types.json import Jsonb


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    url = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not url:
        pytest.skip("integration database is not configured")
    with Connection.connect(url) as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


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


def test_the_current_tenant_route_answers_only_for_the_caller(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two tenants, two tokens, two different answers -- and no way to ask for the other.

    There is no path parameter to tamper with: the scope comes from the token.
    That is the whole difference from the interface this replaces.
    """

    client_a, headers_a = _client_for(settings, seeded_database, ["tenant.read"])
    client_b, headers_b = _client_for(settings, seeded_database, ["tenant.read"], tenant="b")

    body_a = client_a.get("/tenants/current", headers=headers_a).json()
    body_b = client_b.get("/tenants/current", headers=headers_b).json()

    assert body_a["tenant_id"] == str(seeded_database["tenant_a"])
    assert body_a["code"] == "tenant-a"
    assert body_b["tenant_id"] == str(seeded_database["tenant_b"])
    assert set(body_a) == {"tenant_id", "code", "status", "created_at"}
    # Pinned because the first draft rendered this one field with a `Z` while
    # every other timestamp on this surface is `+00:00`. One divergent format is
    # what a client hard-codes around.
    assert body_a["created_at"].endswith("+00:00")


def test_the_employee_list_is_a_page_and_never_publishes_permissions(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A directory page needs who exists, not who can do what.

    The permission *count* is the compromise: it answers "is this account
    configured at all" without turning a staff list into an authorization map.
    The surface that should present permissions properly (B1 #22-#26) is not
    built, and a list route is not the place to decide that question early.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.list"])

    response = client.get("/employees", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("page", body)
    assert [item["email"] for item in body["items"]] == ["operator-a@example.com"]
    item = body["items"][0]
    assert set(item) == {"employee_id", "email", "status", "permission_count", "purged_at"}
    assert item["permission_count"] == 1
    assert "permissions" not in item and "password_hash" not in item
    # **T155.** `026` 的列注释明写这个状态「不可从墓碑地址的形状反推，
    # 也不得那样读」—— 而在这个键出现之前，那是客户端**唯一可行**的读法。
    # `null` 是一个答案（没人 purge 过这一行），不是一处缺失。
    assert item["purged_at"] is None


def test_one_tenants_staff_list_never_contains_another_tenants_people(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS through the route. Both tenants have exactly one operator, seeded alike."""

    client_a, headers_a = _client_for(settings, seeded_database, ["employee.list"])
    client_b, headers_b = _client_for(settings, seeded_database, ["employee.list"], tenant="b")

    listed_a = client_a.get("/employees", headers=headers_a).json()["items"]
    listed_b = client_b.get("/employees", headers=headers_b).json()["items"]
    emails_a = {item["email"] for item in listed_a}
    emails_b = {item["email"] for item in listed_b}

    assert emails_a == {"operator-a@example.com"}
    assert emails_b == {"operator-b@example.com"}


def test_the_stats_route_separates_who_can_still_get_in(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`total` alone would make an operator go and count.

    The question behind this page is "how many people can still get in", and
    that is `active` -- so the projection names it rather than leaving the
    subtraction to the reader.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    body = client.get("/employees/stats", headers=headers).json()

    assert body["total"] == 1
    assert body["active"] == 1
    assert body["disabled"] == 0
    assert set(body) == {"total", "active", "disabled", "as_of"}


def test_the_stats_route_is_declared_before_any_employee_id_route(
    settings: Settings,
) -> None:
    """The ordering guard, saying what it actually guards.

    The first version called `GET /employees/stats` and asserted a 200 -- which
    is byte-for-byte what the test above it asserts, so a reader could not tell
    from the assertion that ordering was the subject, and the day a by-id route
    lands ahead of it the failure would read as "stats broke" (independent
    review 2026-09-04, P3-2).

    This looks at declaration order directly. It also no longer depends on the
    future route happening to use a typed path parameter that fails to match
    `stats`.
    """

    paths = [getattr(route, "path", "") for route in create_app(settings).routes]
    stats_at = paths.index("/employees/stats")
    by_id = [
        index
        for index, path in enumerate(paths)
        if path.startswith("/employees/{")
    ]

    assert all(index > stats_at for index in by_id), (
        "a /employees/{...} route declared before /employees/stats makes FastAPI "
        f"read 'stats' as an employee id; declaration order was {paths}"
    )


def test_a_page_that_cannot_fit_is_refused_rather_than_silently_truncated(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`has_more: false` may not be a guess.

    `page.schema.json` makes `has_more: true` require a string `next_cursor`, so
    "ship the shape now, add the cursor later" is not open: with a second page
    and no cursor, the only reachable answer is a false one. Neither list route
    had a `LIMIT` at all (independent review 2026-09-04, P2-6).

    Until there is a real cursor, exceeding the cap is an error. A route that
    refuses is a bug report; one that quietly drops rows is a wrong answer that
    looks right.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    url = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not url:
        pytest.skip("integration database is not configured")
    with Connection.connect(url) as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            )
            SELECT %s, gen_random_uuid(), 'filler-' || i || '@example.com', 'x',
                   '[]'::jsonb, 'Filler ' || i
              FROM generate_series(1, %s) AS i
            """,
            (tenant_id, MAX_PAGE_ITEMS),
        )
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["employee.list"])

    response = client.get("/employees", headers=headers)

    # 409, not 503: this refusal is deterministic and permanent -- rows do not
    # become fewer -- so "try again later" would be the wrong thing to tell a
    # client and reads to monitoring as an availability incident (independent
    # review round 2, R-5). The detail is a bare code, like every other error on
    # this surface.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "PAGE_LIMIT_EXCEEDED"


def test_stats_is_not_swallowed_by_a_future_employee_id_route(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The ordering trap, pinned before the route that would spring it exists.

    Freeze B1 #21 is `GET /employees/{employee_id}/stats`, and #15 will bring
    `PATCH /employees/{employee_id}`. FastAPI matches in declaration order, so a
    by-id route declared before this one turns `stats` into an employee id and
    this page starts 404ing -- or worse, answering about an employee that does
    not exist. The channel sink already carries the same guard for the same
    reason; this one is written while it is still cheap.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.read"])

    response = client.get("/employees/stats", headers=headers)

    assert response.status_code == 200
    assert "total" in response.json()


@pytest.mark.parametrize(
    ("path", "permission"),
    [
        ("/tenants/current", "employee.list"),
        ("/employees", "tenant.read"),
        ("/employees/stats", "employee.list"),
    ],
)
def test_each_read_model_needs_its_own_permission(
    settings: Settings, seeded_database: dict[str, Any], path: str, permission: str
) -> None:
    """Holding one of these permissions must not grant the others.

    Parametrised across the cluster because the cheap mistake here is one
    permission covering "the read-only pages" as a group -- which is how a staff
    directory ends up visible to anyone who can read a tenant's own name.
    """

    client, headers = _client_for(settings, seeded_database, [permission])

    assert client.get(path, headers=headers).status_code == 403
