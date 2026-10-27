"""Freeze B1 #17 (`POST /employees/{id}/password-reset`) and B1 #30 (`POST /auth/change-password`).

The two routes are one subject and are tested together on purpose: #17 is the
only thing in the repository that sets `must_change_password`, and #30 is the
only thing that clears it. Split across two batches, `main` would have carried a
route that can lock somebody out with no way back -- which is the loop ADR-0052
was written about.

Three cases carry the weight:

* `test_the_reset_leaves_exactly_one_door_open` walks the whole state: reset ->
  log in -> everything 403 -> change password -> log in -> the 403 that remains
  is a *different* 403. Asserting the code rather than the status is the point;
  both are 403 and only one of them is K8's.
* `test_changing_your_password_needs_no_permission_at_all` is the reason #30
  does not call `authorize()`. It runs as an employee holding an empty
  permission array -- the normal product of `POST /employees` -- and that caller
  must be able to reach this route or K8 has locked out the account it was
  written to protect.
* `test_the_update_grant_is_narrower_than_the_table` is the second defence,
  read out of the catalog: the routes here may write four credential columns and
  the connection they use cannot write `permissions` no matter what the code says.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import hash_password
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

#: What the seeded target starts with, and what must stop working after a reset.
TARGET_PASSWORD = "local-only-target-password"
#: What the administrator sets. Never appears in a response or an audit row.
RESET_PASSWORD = "local-only-reset-password"
#: What the target chooses for themselves afterwards.
CHOSEN_PASSWORD = "local-only-chosen-password"

TARGET_EMAIL = "target-a@example.com"


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


def _seed_target(
    seeded: dict[str, Any], *, permissions: list[str] | None = None, email: str = TARGET_EMAIL
) -> UUID:
    """A second employee in tenant A, written the way the fixture writes its own.

    Not created through `POST /employees`, deliberately: that route now sets
    `must_change_password = true`, and a target that already owes a change
    cannot show that the reset is what caused it.
    """

    employee_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            ) VALUES (%s, %s, %s, %s, %s, 'Target')
            """,
            (
                seeded["tenant_a"],
                employee_id,
                email,
                hash_password(TARGET_PASSWORD),
                Jsonb(permissions or []),
            ),
        )
        connection.commit()
    return employee_id


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


def _login(client: TestClient, email: str, password: str) -> Any:
    return client.post(
        "/auth/login",
        json={"tenant_code": "tenant-a", "email": email, "password": password},
    )


def _reset(
    client: TestClient,
    headers: dict[str, str],
    employee_id: UUID,
    password: str = RESET_PASSWORD,
) -> Any:
    return client.post(
        f"/employees/{employee_id}/password-reset",
        json={"new_password": password},
        headers={**headers, "Idempotency-Key": f"password-reset-{uuid4()}"},
    )


def _row(employee_id: UUID) -> dict[str, Any] | None:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT email, password_hash, permissions, status, credential_version,
                   password_changed_at, must_change_password
              FROM employees WHERE id = %s
            """,
            (employee_id,),
        ).fetchone()


def _audit_rows(employee_id: UUID) -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT actor_id, action, resource_type, resource_id, reason_code, evidence
              FROM audit_events WHERE resource_id = %s ORDER BY created_at
            """,
            (employee_id,),
        ).fetchall()


# --------------------------------------------------------------------------
# B1 #17 -- POST /employees/{employee_id}/password-reset
# --------------------------------------------------------------------------


def test_a_reset_answers_with_a_receipt_and_the_facts_it_creates(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`{"receipt": …, "result": …}` (ADR-0050 §三), and the row underneath it.

    The receipt half is validated as a whole against the contract rather than
    key by key: the contract is `additionalProperties: false`, and picking keys
    is the one way of writing this check that stays green no matter what else
    the body grows.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database)
    before = _row(target)
    assert before is not None

    response = _reset(client, headers, target)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["status"] == "succeeded"
    assert body["receipt"]["resource_ref"]["resource_type"] == "employee"
    assert body["receipt"]["resource_ref"]["resource_id"] == str(target)
    assert body["result"]["employee_id"] == str(target)
    assert body["result"]["must_change_password"] is True

    stored = _row(target)
    assert stored is not None
    assert stored["password_hash"] != before["password_hash"], "the credential changed"
    assert stored["must_change_password"] is True
    assert stored["password_changed_at"] is not None
    assert int(stored["credential_version"]) == int(before["credential_version"]) + 1
    # The columns this route must not touch, checked on the same row rather than
    # trusted: a reset is not a way to change what somebody may do.
    assert stored["permissions"] == before["permissions"]
    assert stored["status"] == before["status"]


def test_the_new_password_is_nowhere_in_the_answer(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`C0A-024`'s replacement clause: 写入即哈希、不回显.

    The route it replaces returned `new_password` in its body. Asserted against
    the whole serialised response and the whole header block, not a chosen key,
    and against the audit row too -- an evidence blob is the other place a
    credential can end up without anybody deciding to put it there.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database)

    response = _reset(client, headers, target)

    assert RESET_PASSWORD not in response.text
    assert RESET_PASSWORD not in str(dict(response.headers))
    stored = _row(target)
    assert stored is not None
    assert stored["password_hash"] != RESET_PASSWORD
    assert RESET_PASSWORD not in stored["password_hash"]
    assert RESET_PASSWORD not in str(_audit_rows(target))


def test_the_reset_ends_every_session_the_target_was_holding(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """撤销既有 session, and the belt beside that braces.

    Two things stop the old token: the session row is revoked, and
    `credential_version` moves past the value inside the token. Only the first
    is visible in `sessions_revoked`, so the assertion that matters is that the
    token itself has stopped working.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database, permissions=["session.read"])
    theirs = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    assert theirs.status_code == 200, theirs.text
    their_headers = {"Authorization": f"Bearer {theirs.json()['access_token']}"}
    assert client.get("/auth/me", headers=their_headers).status_code == 200

    response = _reset(client, headers, target)

    assert response.json()["result"]["sessions_revoked"] == 1
    assert client.get("/auth/me", headers=their_headers).status_code == 401
    assert _login(client, TARGET_EMAIL, TARGET_PASSWORD).status_code == 401, (
        "the old password is gone too"
    )


def test_the_reset_leaves_exactly_one_door_open(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0048 §二.5 end to end, and ADR-0052's reason for existing.

    The walk is: reset -> log in with the temporary password -> **every route
    answers 403 `PASSWORD_CHANGE_REQUIRED`** -> change the password -> log in
    again -> the flag is gone.

    **The last assertion is about the code, not the status.** `GET /auth/me`
    answers 403 for this account either way: before the change because K8 says
    so, and after it because a seeded target holds `session.read` but the
    checked route needs it -- so a test that only compared statuses would pass
    with the change-password route removed entirely. What must be shown is that
    the *reason* changed.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database, permissions=["session.read"])
    _reset(client, headers, target)

    temporary = _login(client, TARGET_EMAIL, RESET_PASSWORD)
    assert temporary.status_code == 200, "the temporary password is a working credential"
    # **T130, the half that used to be missing.** The 200 below is a token that
    # works on exactly one route, and before this key the body said nothing
    # about it -- the only way to find out was the 403 three lines down.
    assert temporary.json()["must_change_password"] is True, (
        "登录响应要自己说清这件事，而不是让客户端去撞下一条 route 的 403"
    )
    theirs = {"Authorization": f"Bearer {temporary.json()['access_token']}"}

    locked = client.get("/auth/me", headers=theirs)
    assert locked.status_code == 403
    assert locked.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    listed = client.get("/employees", headers=theirs)
    assert listed.status_code == 403
    assert listed.json()["code"] == "PASSWORD_CHANGE_REQUIRED"

    changed = client.post(
        "/auth/change-password",
        json={"current_password": RESET_PASSWORD, "new_password": CHOSEN_PASSWORD},
        headers=theirs,
    )
    assert changed.status_code == 204, changed.text

    stored = _row(target)
    assert stored is not None
    assert stored["must_change_password"] is False

    again = _login(client, TARGET_EMAIL, CHOSEN_PASSWORD)
    assert again.status_code == 200, again.text
    # **两个方向都要跑过。** 只断言 `True` 的那一半，会让一个恒为 `True`
    # 的实现照样绿 —— 本仓记过那种「看起来在守」的用例。
    assert again.json()["must_change_password"] is False
    now_theirs = {"Authorization": f"Bearer {again.json()['access_token']}"}
    assert client.get("/auth/me", headers=now_theirs).status_code == 200
    # Still 403, and that is correct -- but for the ordinary reason.
    denied = client.get("/employees", headers=now_theirs)
    assert denied.status_code == 403
    assert denied.json()["code"] != "PASSWORD_CHANGE_REQUIRED"


def test_resetting_your_own_password_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409, and nothing written.

    `must_change_password` means "somebody else set this". A self-reset would
    make that false while revoking the caller's own session mid-request; the
    door to your own credential is `POST /auth/change-password`.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    caller = UUID(str(seeded_database["employee_a"]))
    before = _row(caller)
    assert before is not None

    response = _reset(client, headers, caller)

    assert response.status_code == 409
    assert response.json()["code"] == "CANNOT_RESET_OWN_PASSWORD"
    after = _row(caller)
    assert after is not None
    assert after["password_hash"] == before["password_hash"]
    assert after["must_change_password"] is False


def test_resetting_a_disabled_employee_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409 `EMPLOYEE_DISABLED`, told apart from 404 inside the transaction."""

    client, headers = _client_for(
        settings,
        seeded_database,
        ["employee_password_reset.create", "employee.update"],
    )
    target = _seed_target(seeded_database)
    deactivated = client.post(
        f"/employees/{target}/deactivate",
        headers={**headers, "Idempotency-Key": f"deactivate-{uuid4()}"},
    )
    assert deactivated.status_code == 200, deactivated.text

    response = _reset(client, headers, target)

    assert response.status_code == 409
    assert response.json()["code"] == "EMPLOYEE_DISABLED"


def test_another_tenants_employee_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, not 403: a distinguishable answer is a cross-tenant existence oracle."""

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    other = UUID(str(seeded_database["employee_b"]))
    before = _row(other)
    assert before is not None

    response = _reset(client, headers, other)

    assert response.status_code == 404
    assert response.json()["code"] == "EMPLOYEE_NOT_FOUND"
    after = _row(other)
    assert after is not None
    assert after["password_hash"] == before["password_hash"]


def test_a_caller_without_the_command_permission_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The command resource earns its keep: `employee.update` is not enough.

    K1 (ADR-0048 §二.1) made password reset a **command resource** rather than
    mapping it onto `employee.update` -- which is what #15 and #18 share. This
    is that decision as a fact: a caller who can rename people and deactivate
    them still cannot set their passwords.
    """

    client, headers = _client_for(settings, seeded_database, ["employee.update"])
    target = _seed_target(seeded_database)
    before = _row(target)
    assert before is not None

    response = _reset(client, headers, target)

    assert response.status_code == 403
    assert response.json()["code"] != "PASSWORD_CHANGE_REQUIRED"
    after = _row(target)
    assert after is not None
    assert after["password_hash"] == before["password_hash"], "a refusal writes nothing"


def test_the_reset_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: every command carries one."""

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database)

    response = client.post(
        f"/employees/{target}/password-reset",
        json={"new_password": RESET_PASSWORD},
        headers=headers,
    )

    assert response.status_code == 422


def test_the_reset_refuses_a_password_login_would_refuse(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A reset that set a password nobody can log in with creates a 401 with no cause."""

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database)

    response = _reset(client, headers, target, password="short")

    assert response.status_code == 422


def test_the_reset_records_who_reset_whom(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T117's rule, on the route that made it matter.

    This is the case T117 was registered for: **the actor is not the subject**.
    A row that cannot answer "who set this person's password" is not an audit
    trail for a credential change, and the evidence carries counts and ids only.
    """

    client, headers = _client_for(settings, seeded_database, ["employee_password_reset.create"])
    target = _seed_target(seeded_database)

    _reset(client, headers, target)

    rows = _audit_rows(target)
    assert len(rows) == 1
    row = rows[0]
    assert UUID(str(row["actor_id"])) == UUID(str(seeded_database["employee_a"]))
    assert UUID(str(row["resource_id"])) == target
    assert row["action"] == "employee_password_reset.create"
    assert row["reason_code"] == "EMPLOYEE_PASSWORD_RESET"
    assert set(row["evidence"]) == {"sessions_revoked", "credential_version"}


# --------------------------------------------------------------------------
# B1 #30 -- POST /auth/change-password
# --------------------------------------------------------------------------


def test_changing_your_password_needs_no_permission_at_all(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0052 §四.3, as a fact rather than a paragraph.

    The caller holds an empty permission array -- what `POST /employees` leaves
    behind, and what a reset target routinely has. If this route went through
    `authorize()` they would be refused, and K8 would have locked out the
    account it exists to protect.
    """

    _seed_target(seeded_database, permissions=[])
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    assert login.status_code == 200, login.text
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # No permission at all: the ordinary routes are closed to them. **`/auth/me`
    # is no longer one of those** -- B1 #16 answered T125 by making it reachable
    # to any authenticated subject -- so the closed route named here is one that
    # really does need a permission.
    assert client.get("/employees", headers=theirs).status_code == 403

    response = client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": CHOSEN_PASSWORD},
        headers=theirs,
    )

    assert response.status_code == 204, response.text
    assert _login(client, TARGET_EMAIL, CHOSEN_PASSWORD).status_code == 200


def test_a_wrong_current_password_changes_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """401, and the credential is untouched.

    The current password *is* the authorisation here, so getting it wrong is an
    authentication failure and answers like every other one -- no hint about
    which half was wrong.
    """

    target = _seed_target(seeded_database, permissions=["session.read"])
    before = _row(target)
    assert before is not None
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.post(
        "/auth/change-password",
        json={"current_password": "local-only-wrong-password", "new_password": CHOSEN_PASSWORD},
        headers=theirs,
    )

    assert response.status_code == 401
    after = _row(target)
    assert after is not None
    assert after["password_hash"] == before["password_hash"]
    assert int(after["credential_version"]) == int(before["credential_version"])
    assert client.get("/auth/me", headers=theirs).status_code == 200, (
        "the refusal did not revoke the session either"
    )


def test_the_new_password_must_actually_be_new(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """422. Under K8 this is the shape that would satisfy the flag while changing nothing."""

    _seed_target(seeded_database, permissions=["session.read"])
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": TARGET_PASSWORD},
        headers=theirs,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "NEW_PASSWORD_MUST_DIFFER"


def test_changing_your_password_ends_your_own_session_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The token that made the call stops working, and **both** reasons are pinned.

    If the old password leaked, the sessions it opened are what an attacker
    still holds. 204 and log in again.

    Two independent mechanisms produce that 401 -- the session row is revoked
    and `credential_version` moves past the value inside the token -- and either
    one alone is enough. So asserting only the 401 would leave both unpinned: a
    probe that removed either would stay green. Each is therefore asserted where
    it is written, in the row.
    """

    target = _seed_target(seeded_database, permissions=["session.read"])
    before = _row(target)
    assert before is not None
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}

    changed = client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": CHOSEN_PASSWORD},
        headers=theirs,
    )

    assert changed.status_code == 204
    assert client.get("/auth/me", headers=theirs).status_code == 401
    after = _row(target)
    assert after is not None
    assert int(after["credential_version"]) == int(before["credential_version"]) + 1
    with _connection() as connection:
        live = connection.execute(
            """
            SELECT count(*) AS n FROM employee_sessions
             WHERE employee_id = %s AND revoked_at IS NULL
            """,
            (target,),
        ).fetchone()["n"]
    assert live == 0, "every session the old password opened is closed"


def test_changing_your_password_records_that_you_did_it_yourself(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T117 again, with actor and subject the same person -- which is the fact.

    The rows are filtered by action rather than counted, because logging in is
    now a row too: this batch wired the two authentication call sites the rule
    had been missing since B1 #18 wrote it.
    """

    target = _seed_target(seeded_database, permissions=["session.read"])
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}

    client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": CHOSEN_PASSWORD},
        headers=theirs,
    )

    rows = [row for row in _audit_rows(target) if row["action"] == "employee.change_own_password"]
    assert len(rows) == 1
    assert UUID(str(rows[0]["actor_id"])) == target
    assert rows[0]["reason_code"] == "PASSWORD_CHANGED_BY_OWNER"
    assert CHOSEN_PASSWORD not in str(rows[0])


def test_signing_in_and_out_finally_leaves_a_trace(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T117 closed.** Every transition in a session's life is now a row.

    The rule and the helper were written by B1 #18, which wired one of the five
    call sites; the entry's remaining expiry was "B1 #17 落地时". Reset and
    change-password are two more, and they end sessions in bulk -- so a table
    that recorded those while login and logout wrote nothing would look complete
    and still be unable to answer "when did this person last sign in?".

    Asserted as an ordered pair rather than a count: what the row is *for* is
    reconstructing a sequence, and `session.create` followed by `session.delete`
    is the smallest sequence there is.
    """

    target = _seed_target(seeded_database, permissions=["session.delete"])
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_EMAIL, TARGET_PASSWORD)
    assert login.status_code == 200, login.text
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}

    logout = client.post("/auth/logout", headers=theirs)

    assert logout.status_code == 204, logout.text
    rows = _audit_rows(target)
    assert [row["action"] for row in rows] == ["session.create", "session.delete"]
    assert [row["reason_code"] for row in rows] == ["EMPLOYEE_LOGGED_IN", "EMPLOYEE_LOGGED_OUT"]
    for row in rows:
        # Actor and subject are the same person here, unlike the reset row
        # above. That contrast is what makes both worth keeping.
        assert UUID(str(row["actor_id"])) == target
        assert UUID(str(row["resource_id"])) == target
        assert set(row["evidence"]) == {"session_id"}
    assert TARGET_PASSWORD not in str(rows)


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """No permission is needed; a subject still is."""

    client = TestClient(create_app(settings))

    response = client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": CHOSEN_PASSWORD},
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# The second defence
# --------------------------------------------------------------------------


def test_the_update_grant_is_narrower_than_the_table(
    seeded_database: dict[str, Any],
) -> None:
    """Read out of the catalog rather than trusted, the way 020's test is.

    A table-wide `GRANT UPDATE` would let these two routes' connection write
    `permissions` even though neither statement names it -- and then the only
    thing between a credential route and a privilege change would be the
    application remembering not to.

    **Not an equality**: later batches add updatable columns legitimately, and an
    equality here turns each of those into a false alarm, which is how a guard
    like this gets loosened (B1 #15's independent review, P2-2).
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

    assert {
        "password_hash",
        "password_changed_at",
        "credential_version",
        "must_change_password",
    } <= granted
    # **`email` left this list in B1 #19 (migration 026).** The purge route has
    # to overwrite the address -- that *is* the tombstone -- so `fastagent_app`
    # now holds `UPDATE (email)` and this route's connection can write it too.
    # **What this case is about is unchanged**: `legal_hold` and `permissions`
    # are still unwritable, and those are the two that matter here -- one is a
    # guard the purge route reads, the other is privilege. T127's fifth
    # instance, and the same disposition as the previous four: narrow the
    # assertion, say why, leave the registration open.
    assert not granted & {"permissions", "legal_hold", "tenant_id", "id"}
