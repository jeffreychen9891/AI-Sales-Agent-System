from __future__ import annotations

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_login_auth_context_and_one_time_websocket_ticket(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    assert login.headers["cache-control"] == "no-store, private"
    access_token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["tenant_id"] == str(seeded_database["tenant_a"])

    ticket_response = client.post(
        "/ws/tickets",
        headers=headers,
        json={"origin": "http://localhost:3000", "topics": ["conversation.events"]},
    )
    assert ticket_response.status_code == 200
    assert ticket_response.headers["cache-control"] == "no-store, private"
    ticket = ticket_response.json()["ticket"]
    assert access_token not in ticket

    with client.websocket_connect(
        f"/ws?ticket={ticket}&nonce=0123456789abcdef",
        headers={"origin": "http://localhost:3000"},
    ) as websocket:
        authenticated = websocket.receive_json()
        assert authenticated["type"] == "authenticated"
        assert authenticated["tenant_id"] == str(seeded_database["tenant_a"])

    with pytest.raises(WebSocketDisconnect) as replay_error:
        with client.websocket_connect(
            f"/ws?ticket={ticket}&nonce=fedcba9876543210",
            headers={"origin": "http://localhost:3000"},
        ):
            pass
    assert replay_error.value.code == 4401


@pytest.mark.security
@pytest.mark.parametrize(
    ("path", "payload", "withheld"),
    [
        # **`/auth/me` is no longer in this table** (B1 #16, T125): it asks for
        # no permission, so there is no permission to withhold. Its replacement
        # here is `GET /employees`, which is the same shape -- a plain read
        # behind a tenant-scoped permission -- and keeps the table testing what
        # it was written to test rather than shrinking by one row.
        ("/employees", None, "employee.list"),
        (
            "/ws/tickets",
            {"origin": "http://localhost:3000", "topics": ["conversation.events"]},
            "websocket_ticket.create",
        ),
    ],
)
def test_a_token_without_the_action_gets_403_and_learns_nothing_else(
    settings: Settings,
    seeded_database: dict[str, object],
    path: str,
    payload: dict[str, object] | None,
    withheld: str,
) -> None:
    """T36 / independent review P2-4: prove the decision point at the HTTP edge.

    The only route-level assertion this batch shipped with counted occurrences
    of `authorize(` in `app.py`'s **source text**, and the reverse probe that
    "caught" a route regressing to a bare permission check was really catching
    that grep. So "the routes go through the decision point" was guaranteed by
    string matching rather than by a token without the permission being refused.

    This drives the real thing: strip one action from the employee's
    permissions, log in for real, and require a 403 whose body says the reason
    code and nothing else. `/auth/me` in particular had no `except` at all and
    returned **500** before the global handler landed (P2-3).
    """

    import json
    import os

    from psycopg import Connection
    from psycopg.types.json import Jsonb

    tenant_a = seeded_database["tenant_a"]
    with Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]) as connection:
        row = connection.execute(
            "SELECT permissions FROM employees WHERE tenant_id = %s AND email = %s",
            (tenant_a, "operator-a@example.com"),
        ).fetchone()
        assert row is not None
        remaining = [item for item in row[0] if item != withheld]
        assert len(remaining) < len(row[0]), "the seed no longer grants " + withheld
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND email = %s",
            (Jsonb(remaining), tenant_a, "operator-a@example.com"),
        )
        connection.commit()

    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = (
        client.get(path, headers=headers)
        if payload is None
        else client.post(path, headers=headers, json=payload)
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"
    # Nothing about which tenant or which resource: a deny must not confirm that
    # the caller's guess named something real (§3.3).
    body = json.dumps(response.json())
    assert str(tenant_a) not in body
    assert "session" not in body and "websocket_ticket" not in body
