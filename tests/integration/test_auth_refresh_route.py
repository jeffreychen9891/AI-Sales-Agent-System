"""Freeze B1 #5 (`C0A-002`): 一次性 rotation + reuse detection.

`unified-authorization-layer.md:151` is one sentence and this file is that
sentence, case by case: the server stores hash / family / rotation counter /
expiry / revoke reason, **every use rotates, and reuse revokes the whole
family**.

**The case that carries the batch is `test_replaying_a_used_token_kills_the
_whole_family`.** Rotation alone would be a nice-to-have; what makes it a
security property is that a stolen copy stops working *and takes the thief's
session down with it* -- and that the honest client, who cannot be told apart
from the thief, is logged out too. That last part is the cost, and it is
deliberate.
"""

from __future__ import annotations

import os
from datetime import timedelta
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import REFRESH_COOKIE_NAME, create_app
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


def _login(settings: Settings, seeded: dict[str, Any]) -> tuple[TestClient, str]:
    client = TestClient(create_app(settings))
    response = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded["login_password"],
        },
    )
    assert response.status_code == 200, response.text
    cookie = response.cookies.get(REFRESH_COOKIE_NAME)
    assert cookie, "login must issue a refresh cookie"
    return client, cookie


def _refresh(client: TestClient, cookie: str) -> Any:
    # The cookie is passed explicitly rather than relying on the client jar, so
    # every case says which token it is presenting -- which is the whole subject
    # of the reuse cases below.
    return client.post("/auth/refresh", cookies={REFRESH_COOKIE_NAME: cookie})


def _family_rows() -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT family_id, rotation_counter, used_at, revoked_at, revoke_reason,"
            " credential_version, expires_at"
            " FROM refresh_tokens ORDER BY rotation_counter"
        ).fetchall()


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------


def test_login_starts_a_family_and_the_token_never_reaches_the_body(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`:151` names the container, `:153` closes the alternatives.

    The frontends may not put a refresh token in `localStorage`, a URL or a query
    cache -- so handing one to script in a JSON body would leave it nowhere legal
    to go. The server sets an `HttpOnly` cookie instead, and the body carries the
    access token only.
    """

    client = TestClient(create_app(settings))
    response = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )

    assert response.status_code == 200
    assert "refresh" not in response.text.lower()
    header = response.headers["set-cookie"]
    assert REFRESH_COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=strict" in header.replace("samesite", "SameSite")
    rows = _family_rows()
    assert len(rows) == 1
    assert rows[0]["rotation_counter"] == 0
    assert rows[0]["used_at"] is None


def test_every_use_rotates_and_the_old_token_stops_working(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One-shot, which is what makes a stolen copy expire on the next honest use."""

    client, first = _login(settings, seeded_database)

    rotated = _refresh(client, first)

    assert rotated.status_code == 200, rotated.text
    second = rotated.cookies.get(REFRESH_COOKIE_NAME)
    assert second and second != first
    # **The rotated token leaves the same way the first one did.** A reverse
    # probe found this missing: the login case asserted "no refresh token in the
    # body", the refresh case did not, so a route that returned the rotated
    # token in its JSON would have passed. The container rule is about every
    # response that carries one, not just the first.
    assert second not in rotated.text
    assert "refresh_token" not in rotated.text
    header = rotated.headers["set-cookie"]
    assert "HttpOnly" in header and "Secure" in header
    rows = _family_rows()
    assert [row["rotation_counter"] for row in rows] == [0, 1]
    assert len({str(row["family_id"]) for row in rows}) == 1, "a rotation stays in its family"
    assert rows[0]["used_at"] is not None
    assert rows[0]["revoke_reason"] == "rotated"
    assert rows[1]["used_at"] is None


def test_a_rotation_does_not_extend_the_familys_life(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Otherwise a chain refreshed often would never expire, which is the
    opposite of what an expiry is for."""

    client, first = _login(settings, seeded_database)

    assert _refresh(client, first).status_code == 200

    rows = _family_rows()
    assert rows[0]["expires_at"] == rows[1]["expires_at"]


# --------------------------------------------------------------------------
# Reuse
# --------------------------------------------------------------------------


def test_replaying_a_used_token_kills_the_whole_family(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The case this table exists for.**

    A token that arrives already used means two parties hold the chain, and
    nothing here can say which is the thief -- the honest client is quite likely
    the one that just refreshed. So the family dies and both must log in again.
    """

    client, first = _login(settings, seeded_database)
    rotated = _refresh(client, first)
    second = rotated.cookies.get(REFRESH_COOKIE_NAME)
    assert second

    replay = _refresh(client, first)

    assert replay.status_code == 401
    assert replay.json()["code"] == "AUTHENTICATION_REQUIRED"
    # The still-unused token is dead too -- that is what "revoke the family"
    # means, and it is the half that costs the honest client something.
    assert _refresh(client, second).status_code == 401
    rows = _family_rows()
    assert all(row["revoked_at"] is not None for row in rows)
    assert any(row["revoke_reason"] == "reuse-detected" for row in rows)


def test_two_simultaneous_refreshes_cannot_both_succeed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F2, and it was measured before it was fixed.**

    The snapshot came from a transaction that had already closed, and the write
    that stamped `used_at` carried no condition -- so two threads presenting the
    same cookie both saw `used_at IS NULL` and both rotated. The reviewer got
    **two 200s and one family holding two live tokens**, which is exactly the
    state `:151` exists to prevent: an attacker and the real client each holding
    a working chain, with reuse never detected.

    The fix makes the claim the check (`UPDATE ... WHERE used_at IS NULL
    RETURNING`), so the database serialises the racers, and a partial unique
    index states the same rule where it cannot be forgotten.
    """

    from concurrent.futures import ThreadPoolExecutor

    client, cookie = _login(settings, seeded_database)
    barrier = Barrier(2)

    def attempt() -> int:
        racer = TestClient(create_app(settings))
        barrier.wait(timeout=10)
        return int(_refresh(racer, cookie).status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(future.result() for future in [pool.submit(attempt), pool.submit(attempt)])

    # One wins, one is refused. **Never two 200s.**
    assert codes == [401, 200] or codes == [200, 401] or codes == [401, 401], codes
    assert codes.count(200) <= 1, "a family must never hand out two live tokens"
    with _connection() as connection:
        live = connection.execute(
            "SELECT count(*) AS total FROM refresh_tokens"
            " WHERE used_at IS NULL AND revoked_at IS NULL"
        ).fetchone()
    assert live is not None and live["total"] <= 1


def test_an_unknown_token_answers_the_same_401_as_a_replayed_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """"Which of unknown / expired / revoked / reused" is the question an
    attacker with a stolen cookie wants answered, so all four are one answer."""

    client, _ = _login(settings, seeded_database)

    unknown = _refresh(client, "0" * 64)

    assert unknown.status_code == 401
    assert unknown.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_no_cookie_is_the_same_401(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.post("/auth/refresh")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


def test_logging_out_ends_the_refresh_family(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**User ruling 2026-09-05**, on the gap B1 #5 itself introduced.

    Before a refresh token existed, "log out" and "the session is over" were the
    same statement. Adding a credential that outlives the access token made them
    different, and the difference is the one that matters on a shared machine:
    without this, the cookie left behind still mints access tokens for a
    fortnight.

    **Two sessions, so the case also says what logout does *not* do**: it ends
    the family belonging to the session that called it, and no other.
    """

    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(["session.delete"]), seeded_database["tenant_a"], seeded_database["employee_a"]),
        )
        connection.commit()
    staying, staying_cookie = _login(settings, seeded_database)
    leaving = TestClient(create_app(settings))
    login = leaving.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    leaving_cookie = login.cookies.get(REFRESH_COOKIE_NAME)
    assert leaving_cookie

    logout = leaving.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert logout.status_code == 204, logout.text
    assert _refresh(leaving, leaving_cookie).status_code == 401
    with _connection() as connection:
        reasons = connection.execute(
            "SELECT count(*) AS total FROM refresh_tokens WHERE revoke_reason = 'logout'"
        ).fetchone()
    assert reasons is not None and reasons["total"] >= 1
    # The other session is untouched: one logout ends one family.
    assert _refresh(staying, staying_cookie).status_code == 200


# --------------------------------------------------------------------------
# `:152` -- a credential change ends the family
# --------------------------------------------------------------------------


def test_a_credential_version_bump_ends_the_family(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`:152`: 「密码重置、角色降权或 credential version 增加后，旧 refresh 不得继续扩权」.

    Enforced **here**, against the version the row was issued under, rather than
    by asking every route that changes a credential to remember this table. A
    reset that forgot to revoke would otherwise leave a working chain behind.
    """

    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET credential_version = credential_version + 1"
            " WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], seeded_database["employee_a"]),
        )
        connection.commit()

    response = _refresh(client, cookie)

    assert response.status_code == 401
    rows = _family_rows()
    assert all(row["revoked_at"] is not None for row in rows)
    assert any(row["revoke_reason"] == "credential-changed" for row in rows)


def test_a_disabled_employee_cannot_refresh(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The same clause's other half: a disabled account's chain stops too."""

    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET status = 'disabled' WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], seeded_database["employee_a"]),
        )
        connection.commit()

    assert _refresh(client, cookie).status_code == 401


def test_an_expired_token_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Aged in the database because the TTL is fourteen days and cannot be waited.

    **The one second used to be a race.** This case ages the row with the
    database's `now()`, and the route compared `expires_at` against the API
    process's clock -- so whenever the database ran more than a second ahead of
    the process, the token that had just been aged into the past was still in
    the future by the clock doing the comparing, and this returned 200. The
    conftest skew guard only trips above two seconds, which left a whole
    1.0-2.0s band where this case was guaranteed red and nothing said why.

    It went red on 2026-09-07 and read as a flake. It was not: it was the one
    case in the suite whose two halves ran on different clocks.
    """

    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute("UPDATE refresh_tokens SET expires_at = now() - interval '1 second'")
        connection.commit()

    assert _refresh(client, cookie).status_code == 401


def test_a_token_that_expires_at_this_instant_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The same property with the margin taken away.

    Aged to exactly the database's current instant rather than a second before
    it, so there is no interval for a clock offset to hide in: by the time the
    route reads the row, `clock_timestamp()` has moved past it, and the answer
    can only be 401 if the comparison is happening on the clock that wrote the
    value.

    The case above would pass on a machine whose clocks happen to agree. This
    one fails on *any* offset in the permissive direction, which is what makes
    it the real guard.
    """

    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute("UPDATE refresh_tokens SET expires_at = now()")
        connection.commit()

    assert _refresh(client, cookie).status_code == 401


def test_the_login_writes_both_expiries_with_the_database_clock(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One column, one clock -- asserted on the rows rather than on the source.

    `employee_sessions.expires_at` was written from the API process's clock and
    then read against the database's in three of its four readers (the ws-ticket
    claim, the tenant-deactivation revoke, the session count) and against the
    process's in the fourth. At most one of those readers could have been right.

    **The assertion is `expires_at - created_at`, and it is exact**
    (independent review F2). `created_at` defaults to `now()` and `expires_at`
    is written as `now() + ttl` in the *same transaction*, so a database write
    makes the difference equal to the TTL to the microsecond, while a write from
    the process clock cannot be: it lands one clock offset away, whatever that
    offset happens to be that second.

    The first version of this case asserted `expires_at - now()` inside a
    sixty-second window and that the two rows ended at the same instant. Neither
    could fail: the window was twenty-five times the whole 2.396s swing T193
    measured, and the two rows were already equal before this change because one
    Python value was passed twice. **A case that cannot go red is not a case**
    -- and the equality half is pinned anyway by
    `test_the_session_row_outlives_one_access_token`.
    """

    _login(settings, seeded_database)
    with _connection() as connection:
        row = connection.execute(
            """
            SELECT s.expires_at - s.created_at AS session_span,
                   r.expires_at - r.created_at AS token_span
              FROM employee_sessions s
              JOIN refresh_tokens r ON r.session_id = s.id
             ORDER BY s.created_at DESC
             LIMIT 1
            """
        ).fetchone()

    assert row is not None
    expected = timedelta(seconds=settings.refresh_token_ttl_seconds)
    assert row["session_span"] == expected, (
        f"the session must expire exactly one TTL after the database stamped "
        f"its own `created_at`; the gap is {row['session_span']}"
    )
    assert row["token_span"] == expected, (
        f"the refresh family must expire exactly one TTL after the database "
        f"stamped its own `created_at`; the gap is {row['token_span']}"
    )


def test_an_expired_session_refuses_an_access_token_it_already_issued(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F1**: the branch nothing in the suite could reach.

    `_authenticate` refuses a request whose session row has expired. Nothing
    made that true anywhere in the repository -- the three ways to get there are
    to age the row (the one `UPDATE` that touches this column writes a minute
    into the *future*), to insert an already-expired session (two cases do, one
    for an employee that never authenticates and one an hour into the future),
    or to shrink the fourteen-day TTL and wait, which no case does. **Deleting
    the whole `session_expired` clause left the gate green.**

    That clause is what makes the design hold: an access token is good for
    fifteen minutes and a session for a fortnight, so without it a token minted
    one minute before the session ends keeps working for fourteen minutes after.

    It is also the only case that can say *which clock* decides. The row is aged
    by the database's `now()`; a comparison that went back to this process's
    clock would let the request through whenever the database ran ahead -- the
    same shape as `test_an_expired_token_is_refused`, one column over.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
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
    # Establishes that the only thing the UPDATE below changes is the session
    # row: the same bearer token, on the same route, answered 200 first.
    assert client.get("/auth/me", headers=headers).status_code == 200

    with _connection() as connection:
        connection.execute(
            "UPDATE employee_sessions SET expires_at = now() WHERE tenant_id = %s",
            (tenant_id,),
        )
        connection.commit()

    refused = client.get("/auth/me", headers=headers)

    assert refused.status_code == 401, (
        "an access token outliving its session is exactly what the session row "
        f"exists to stop; got {refused.status_code}"
    )


def test_the_new_access_token_works_and_carries_the_same_session(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A refresh that returned a token nothing accepts would be theatre.

    The session id is carried across the rotation on purpose: a refresh
    continues a session, it does not start one -- otherwise `POST /auth/logout`
    would revoke a session the caller is no longer using.
    """

    client, cookie = _login(settings, seeded_database)

    rotated = _refresh(client, cookie)

    access = rotated.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200, me.text
    assert UUID(str(me.json()["employee_id"])) == UUID(str(seeded_database["employee_a"]))
    with _connection() as connection:
        sessions = connection.execute(
            "SELECT DISTINCT session_id FROM refresh_tokens"
        ).fetchall()
    assert len(sessions) == 1, "a rotation continues the session it was issued for"


def test_a_refreshed_token_still_works_after_the_access_ttl_has_passed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F1, the P0 -- and the case whose absence hid it.**

    `_authenticate` refuses a request whose **session row** has expired. That row
    used to be stamped `now + access_token_ttl` (fifteen minutes) and nothing
    ever moved it, so fifteen minutes after login `POST /auth/refresh` answered
    **200 with a token nothing would accept**. Every case in this file ran inside
    that window and saw nothing.

    So this one ages the session row to the edge of the old boundary **before**
    refreshing, then uses the returned token on a real route. A session now lives
    as long as its refresh family.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute(
            "UPDATE employee_sessions SET expires_at = now() + interval '1 minute'"
            " WHERE tenant_id = %s",
            (tenant_id,),
        )
        connection.commit()

    rotated = _refresh(client, cookie)

    assert rotated.status_code == 200, rotated.text
    me = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {rotated.json()['access_token']}"},
    )
    assert me.status_code == 200, "a refreshed token no route accepts is not a refresh"


def test_the_session_row_outlives_one_access_token(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The decision F1 forced, asserted on the rows.

    A session lives as long as the family that can renew it. Pinned here so the
    two clocks cannot drift apart again without a case going red.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _login(settings, seeded_database)

    with _connection() as connection:
        row = connection.execute(
            "SELECT s.expires_at AS session_expiry, r.expires_at AS family_expiry"
            "  FROM employee_sessions s"
            "  JOIN refresh_tokens r ON r.session_id = s.id"
            " WHERE s.tenant_id = %s ORDER BY s.created_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()

    assert row is not None
    assert row["session_expiry"] == row["family_expiry"]


def test_a_revoked_session_cannot_be_refreshed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F5.** `PUT /employees/{id}/roles` revokes sessions
    *deliberately without* touching `credential_version`, so a chain that only
    checked the employee stayed alive for a fortnight after a role change.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, cookie = _login(settings, seeded_database)
    with _connection() as connection:
        connection.execute(
            "UPDATE employee_sessions SET revoked_at = now() WHERE tenant_id = %s",
            (tenant_id,),
        )
        connection.commit()

    assert _refresh(client, cookie).status_code == 401
    with _connection() as connection:
        alive = connection.execute(
            "SELECT count(*) AS total FROM refresh_tokens WHERE revoked_at IS NULL"
        ).fetchone()
    assert alive is not None and alive["total"] == 0, "the family must not outlive its session"


def test_a_role_granted_permission_survives_a_refresh(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F3.** Login mints the column *union the roles*;
    refresh read the column alone, so every role-derived permission disappeared
    on the first rotation -- silently, and in the direction that removes access.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    role_id = uuid4()
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(["employee.list"]), tenant_id, seeded_database["employee_a"]),
        )
        connection.execute(
            "INSERT INTO roles(tenant_id, id, name, permissions) VALUES (%s, %s, %s, %s)",
            (tenant_id, role_id, f"reader-{role_id}", Jsonb(["role.list"])),
        )
        connection.execute(
            "INSERT INTO employee_roles(tenant_id, employee_id, role_id) VALUES (%s, %s, %s)",
            (tenant_id, seeded_database["employee_a"], role_id),
        )
        connection.commit()
    client, cookie = _login(settings, seeded_database)

    rotated = _refresh(client, cookie)

    assert rotated.status_code == 200, rotated.text
    me = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {rotated.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert "role.list" in me.json()["permissions"], "a role's permission must survive rotation"


def test_reuse_ends_the_session_as_well_as_the_family(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review F6.** The docstring promised "both parties have to
    log in again"; revoking only the family left the access token minted moments
    earlier valid for its full fifteen minutes. On a *known theft* that is the
    one path where the session should end at once.
    """

    client, first = _login(settings, seeded_database)
    rotated = _refresh(client, first)
    access = rotated.json()["access_token"]
    live = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert live.status_code == 200

    assert _refresh(client, first).status_code == 401

    after = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert after.status_code == 401, "a known theft must not leave a working access token"


@pytest.mark.security
def test_the_resolver_is_not_executable_by_public(
    seeded_database: dict[str, Any],
) -> None:
    """**Independent review F4.** PostgreSQL grants `EXECUTE` to `PUBLIC` on a
    new function, and this one reads past RLS. `001` revokes it after each of
    its three resolvers; 027 claimed to have copied that restraint and had not.

    Asserted for the roles whose isolation is the point: compromising ingress
    must not hand over anything else.
    """

    with _connection() as connection:
        row = connection.execute(
            """
            SELECT has_function_privilege('public', 'resolve_refresh_token(text)', 'EXECUTE')
                       AS public_exec,
                   has_function_privilege('fastagent_ingress',
                       'resolve_refresh_token(text)', 'EXECUTE') AS ingress_exec,
                   has_function_privilege('fastagent_app',
                       'resolve_refresh_token(text)', 'EXECUTE') AS app_exec
            """
        ).fetchone()

    assert row is not None
    assert row["public_exec"] is False
    assert row["ingress_exec"] is False
    assert row["app_exec"] is True


@pytest.mark.security
def test_the_stored_row_is_a_hash_and_never_the_token(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The verifier, not a redacted token -- the same rule as the callback hash."""

    client, cookie = _login(settings, seeded_database)

    with _connection() as connection:
        rows = connection.execute("SELECT token_hash FROM refresh_tokens").fetchall()

    assert rows
    for row in rows:
        assert row["token_hash"] != cookie
        assert len(str(row["token_hash"])) == 64


@pytest.mark.security
def test_the_grant_on_the_family_table_has_no_delete(
    seeded_database: dict[str, Any],
) -> None:
    """A deleted row is a replay this table can no longer recognise.

    Reuse detection works by the *old* rows still being there; `DELETE` would
    turn a replayed token back into an unknown one, which answers the same 401
    while leaving the family alive.
    """

    with _connection() as connection:
        granted = {
            row["privilege_type"]
            for row in connection.execute(
                """
                SELECT privilege_type FROM information_schema.table_privileges
                 WHERE table_name = 'refresh_tokens' AND grantee = 'fastagent_app'
                """
            ).fetchall()
        }

    assert {"SELECT", "INSERT", "UPDATE"} <= granted
    assert "DELETE" not in granted


@pytest.mark.security
def test_another_tenants_family_is_not_reachable(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS on the new table, asserted rather than assumed.

    The refresh lookup runs without a tenant (the token is what names one), so
    the policy is what stops a row from one tenant being read while another
    tenant's id is set on the session.
    """

    from fastagent_v2.runtime.db import Database

    _login(settings, seeded_database)
    database = Database(settings.database_url)
    other = UUID(str(seeded_database["tenant_b"]))

    with database.transaction(tenant_id=other) as connection:
        visible = connection.execute("SELECT count(*) AS total FROM refresh_tokens").fetchone()

    assert visible is not None and visible["total"] == 0
