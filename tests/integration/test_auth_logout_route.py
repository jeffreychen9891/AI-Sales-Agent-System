"""Route freeze B1 #6: `POST /auth/logout`.

Two of these carry the weight.

`test_the_token_stops_working_immediately_after_logout` is the whole point: a
logout that returns 204 and leaves the token working is a button, not a
boundary. It passes because `authenticated()` already refuses a session with
`revoked_at` set -- this route's job is to set it, and the test asserts the
consequence rather than the write.

`test_logging_out_does_not_end_another_session_of_the_same_employee` is the
scope. One person can hold several sessions (two browsers, a phone); ending the
one you presented must not end the others, or "log out" silently becomes "log
out everywhere" -- a different command, with a different reason to exist.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastapi import WebSocketDisconnect
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


def _login(client: TestClient, seeded: dict[str, Any], tenant: str = "a") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={
            "tenant_code": f"tenant-{tenant}",
            "email": f"operator-{tenant}@example.com",
            "password": seeded["login_password"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded["tenant_a"])),
        employee_id=UUID(str(seeded["employee_a"])),
        permissions=permissions,
    )
    client = TestClient(create_app(settings))
    return client, _login(client, seeded)


def _sessions_of(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT id, revoked_at FROM employee_sessions WHERE employee_id = %s"
            " ORDER BY created_at",
            (employee_id,),
        ).fetchall()


def test_logout_answers_204_and_revokes_the_session(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["session.delete"])

    response = client.post("/auth/logout", headers=headers)

    assert response.status_code == 204
    assert response.content == b"", "204 carries no body"
    assert response.headers["cache-control"] == "no-store, private"
    sessions = _sessions_of(employee_id)
    assert len(sessions) == 1
    assert sessions[0]["revoked_at"] is not None


def test_the_token_stops_working_immediately_after_logout(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A logout that leaves the token working is a button, not a boundary."""

    client, headers = _client_for(settings, seeded_database, ["session.delete", "role.list"])
    assert client.get("/roles", headers=headers).status_code == 200

    assert client.post("/auth/logout", headers=headers).status_code == 204

    assert client.get("/roles", headers=headers).status_code == 401


def test_calling_it_twice_is_a_401_rather_than_a_second_success(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Not "idempotent" -- the second call never reaches the handler.

    Worth pinning as its own case because "idempotent" is the word somebody
    would reach for, and it would set the wrong expectation: the second call
    fails authentication, which is a different answer from "already done".
    """

    client, headers = _client_for(settings, seeded_database, ["session.delete"])
    assert client.post("/auth/logout", headers=headers).status_code == 204

    assert client.post("/auth/logout", headers=headers).status_code == 401


def test_the_revocation_timestamp_is_recorded_once_and_is_in_the_past(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """What the route's write actually guarantees, and nothing more.

    **An earlier version of this test claimed more than it proved.** It ran its
    own hand-written `UPDATE ... WHERE revoked_at IS NULL` and asserted the
    timestamp did not move -- which tests the SQL *in the test*, not the SQL in
    the route. Probe P3 (deleting the guard from the route) left it **green**,
    which is how the circularity surfaced.

    The route's `revoked_at IS NULL` guard is **unreachable through the route**:
    the dependency rejects a revoked token, so a second call never arrives. It
    stays in the statement because the next callers of this shape (B1 #17
    password reset, #18 deactivate) revoke sessions they did not issue, and
    there the guard is reachable -- but it is honest to say that today nothing
    can exercise it, rather than to keep a test that appears to.

    What is left is what the route does guarantee: one revocation, stamped at a
    time that has already happened -- **read from the same clock that wrote it**,
    see the comment below.
    """

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["session.delete"])

    assert client.post("/auth/logout", headers=headers).status_code == 204

    rows = [row for row in _sessions_of(employee_id) if row["revoked_at"] is not None]
    assert len(rows) == 1
    # **Compared against the database's own clock, not the host's.** The column
    # is written with Postgres `now()`, and Postgres runs in a container; the
    # test process runs on the host. The two clocks are close but not equal, and
    # when the container was the faster one this assertion failed -- roughly one
    # run in a few dozen, which is the worst frequency a flake can have: rare
    # enough that a red gate looks like bad luck and gets re-run, common enough
    # to keep happening. **A test that teaches people to re-run gates costs more
    # than the property it checks.**
    #
    # What the case is about is unchanged: the stamp is not in the future.
    with _connection() as connection:
        database_now = connection.execute("SELECT now() AS now").fetchone()
    assert database_now is not None
    assert rows[0]["revoked_at"] <= database_now["now"]


def test_logging_out_does_not_end_another_session_of_the_same_employee(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """"Log out" is not "log out everywhere"; that would be another command."""

    employee_id = UUID(str(seeded_database["employee_a"]))
    client, first_headers = _client_for(settings, seeded_database, ["session.delete", "role.list"])
    second_headers = _login(client, seeded_database)
    assert len(_sessions_of(employee_id)) == 2

    assert client.post("/auth/logout", headers=first_headers).status_code == 204

    revoked = [row for row in _sessions_of(employee_id) if row["revoked_at"] is not None]
    assert len(revoked) == 1, "exactly one session ends"
    # ...and the other one still works.
    assert client.get("/roles", headers=second_headers).status_code == 200


def test_a_caller_without_session_delete_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    employee_id = UUID(str(seeded_database["employee_a"]))
    client, headers = _client_for(settings, seeded_database, ["role.list"])

    response = client.post("/auth/logout", headers=headers)

    assert response.status_code == 403
    assert _sessions_of(employee_id)[0]["revoked_at"] is None, "a refusal writes nothing"


def test_logout_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    assert client.post("/auth/logout").status_code == 401


def test_a_websocket_ticket_issued_before_logout_cannot_be_used_after(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Ending the session has to end what the session already handed out.

    A ticket minted before logout would otherwise open a socket after it -- the
    session is gone and the connection is not, which is the gap that makes a
    logout button decorative. The handshake already requires
    `revoked_at IS NULL` on the ticket's session; this asserts that the two
    facts are joined rather than merely both true.
    """

    client, headers = _client_for(
        settings, seeded_database, ["session.delete", "websocket_ticket.create"]
    )
    ticket = client.post(
        "/ws/tickets",
        headers=headers,
        json={"origin": "http://localhost:3000", "topics": ["conversation.events"]},
    )
    assert ticket.status_code == 200, ticket.text
    token = ticket.json()["ticket"]

    assert client.post("/auth/logout", headers=headers).status_code == 204

    # `WebSocketDisconnect`, not bare `Exception`: the wide form would have gone
    # green on a typo in the URL or a missing `origin` header just as happily as
    # on a refused handshake (independent review 2026-09-05, P3-2). The review
    # also checked the anchor from the other side -- without the logout the same
    # ticket does connect -- so this case is not vacuous.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws?ticket={token}&nonce=0123456789abcdef",
            headers={"origin": "http://localhost:3000"},
        ):
            pass
