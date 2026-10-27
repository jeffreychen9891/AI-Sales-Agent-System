"""The two surfaces that verify a plaintext password now have a limit.

**The bypass.** ADR-0048 §二.5 (K8) puts a person who owes a password change
behind a 403 on every route -- except `POST /auth/change-password`, which is the
one route they must be able to reach, and which takes the **current password**
in its body. So the single door the lockout leaves open was a door that checks a
secret, with no limit on how often. B1 #17's independent review recorded it as a
residual risk; the user ruled it be fixed rather than registered, on the grounds
that a security mechanism was being walked around.

`POST /auth/login` is here for the same reason and has never had a limit at all.

The case that matters most is
`test_a_locked_out_token_cannot_grind_the_one_route_it_can_still_reach`: it is
the bypass, written down.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import hash_password
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.throttle import (
    LOGIN_POLICY,
    PASSWORD_CHANGE_POLICY,
    AttemptThrottle,
    ThrottleExceeded,
)
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from redis import Redis

TARGET_PASSWORD = "local-only-target-password"
TARGET_EMAIL = "throttled-a@example.com"
WRONG_PASSWORD = "local-only-wrong-password"

ROOT = Path(__file__).resolve().parents[2]


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


def _connection() -> Connection[Any]:
    return Connection.connect(_migration_url(), row_factory=dict_row)


@pytest.fixture()
def clean_throttle(settings: Settings) -> Any:
    """Every case starts with an empty window.

    The counters outlive a test by design -- fifteen minutes -- so a suite that
    did not clear them would have its second case fail because of its first, and
    the failure would read like a limiter bug rather than a fixture one.
    """

    client = Redis.from_url(settings.cache_redis_url, decode_responses=True)
    for key in client.scan_iter("fastagent:v2:throttle:*"):
        client.delete(key)
    yield client
    for key in client.scan_iter("fastagent:v2:throttle:*"):
        client.delete(key)
    client.close()


def _seed_employee(seeded: dict[str, Any]) -> UUID:
    employee_id = uuid4()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            ) VALUES (%s, %s, %s, %s, '[]'::jsonb, 'Throttled')
            """,
            (seeded["tenant_a"], employee_id, TARGET_EMAIL, hash_password(TARGET_PASSWORD)),
        )
        connection.commit()
    return employee_id


def _login(client: TestClient, password: str, email: str = TARGET_EMAIL) -> Any:
    return client.post(
        "/auth/login",
        json={"tenant_code": "tenant-a", "email": email, "password": password},
    )


# --------------------------------------------------------------------------
# POST /auth/change-password -- the surface K8 leaves open
# --------------------------------------------------------------------------


def test_a_locked_out_token_cannot_grind_the_one_route_it_can_still_reach(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """**The bypass, written down.**

    The employee is put in exactly the state K8 creates -- `must_change_password`
    true, so every route but this one answers 403 -- and then guesses. Before
    this batch the guessing was unbounded: the lockout removed every route
    *except* the one that checks a secret.

    Asserted through the wire, not through the limiter: what matters is that the
    sixth attempt is refused **without the password being checked**, and the
    only way to see that from outside is the status code and the `retry-after`.
    """

    employee_id = _seed_employee(seeded_database)
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET must_change_password = true WHERE id = %s", (employee_id,)
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_PASSWORD)
    assert login.status_code == 200, login.text
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    # The lockout is real: everything else is closed to this token.
    assert client.get("/auth/me", headers=theirs).status_code == 403

    refusals = []
    for _ in range(PASSWORD_CHANGE_POLICY.limit + 1):
        refusals.append(
            client.post(
                "/auth/change-password",
                json={"current_password": WRONG_PASSWORD, "new_password": "local-only-new-one"},
                headers=theirs,
            )
        )

    assert [response.status_code for response in refusals[:-1]] == [401] * (
        PASSWORD_CHANGE_POLICY.limit
    )
    stopped = refusals[-1]
    assert stopped.status_code == 429
    assert stopped.json()["code"] == "TOO_MANY_PASSWORD_ATTEMPTS"
    assert stopped.json()["retryable"] is True
    # A 429 that does not say how long is a 429 every client turns into a guess.
    assert 0 < int(stopped.headers["retry-after"]) <= PASSWORD_CHANGE_POLICY.window_seconds


def test_the_right_password_still_works_up_to_the_limit_and_clears_it(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """A window is spent by failures and ended by a success.

    Both halves matter. Without the first, somebody who mistypes once is
    unaffected -- which is the point. Without the second, a person who mistyped
    four times and then got it right would carry those four for fifteen minutes
    and be locked out by their next single slip.
    """

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))
    login = _login(client, TARGET_PASSWORD)
    theirs = {"Authorization": f"Bearer {login.json()['access_token']}"}
    for _ in range(PASSWORD_CHANGE_POLICY.limit - 1):
        assert (
            client.post(
                "/auth/change-password",
                json={"current_password": WRONG_PASSWORD, "new_password": "local-only-new-one"},
                headers=theirs,
            ).status_code
            == 401
        )

    changed = client.post(
        "/auth/change-password",
        json={"current_password": TARGET_PASSWORD, "new_password": "local-only-chosen-one"},
        headers=theirs,
    )

    assert changed.status_code == 204, changed.text
    assert not list(clean_throttle.scan_iter("fastagent:v2:throttle:password-change:*"))


def test_one_persons_attempts_do_not_lock_out_another(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """The counter is keyed on the subject in the token, not on the route.

    A limiter that shared one counter across the surface would let any employee
    lock out the whole tenant by guessing at their own password -- which is a
    denial of service dressed as a security control.
    """

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))
    theirs = {
        "Authorization": f"Bearer {_login(client, TARGET_PASSWORD).json()['access_token']}"
    }
    for _ in range(PASSWORD_CHANGE_POLICY.limit + 1):
        client.post(
            "/auth/change-password",
            json={"current_password": WRONG_PASSWORD, "new_password": "local-only-new-one"},
            headers=theirs,
        )

    others = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )
    assert others.status_code == 200, others.text
    mine = client.post(
        "/auth/change-password",
        json={
            "current_password": seeded_database["login_password"],
            "new_password": "local-only-x2",
        },
        headers={"Authorization": f"Bearer {others.json()['access_token']}"},
    )
    assert mine.status_code == 204, mine.text


# --------------------------------------------------------------------------
# POST /auth/login
# --------------------------------------------------------------------------


def test_login_stops_answering_after_enough_wrong_passwords(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """The other surface, which has never had a limit at all."""

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))

    for _ in range(LOGIN_POLICY.limit):
        assert _login(client, WRONG_PASSWORD).status_code == 401

    stopped = _login(client, WRONG_PASSWORD)
    assert stopped.status_code == 429
    assert stopped.json()["code"] == "TOO_MANY_PASSWORD_ATTEMPTS"
    # **And the right password does not get through either.** A limit that let a
    # correct guess past would be a limit that tells an attacker when they have
    # won, which is the one answer it must not give.
    assert _login(client, TARGET_PASSWORD).status_code == 429


def test_a_missing_account_costs_an_attempt_too(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """Otherwise account enumeration is the one unlimited question.

    An address that does not exist answers the same 401 and spends the same
    attempt as a wrong password, so "does this account exist" is no cheaper to
    ask than "is this the password".
    """

    client = TestClient(create_app(settings))

    for _ in range(LOGIN_POLICY.limit):
        assert _login(client, WRONG_PASSWORD, email="nobody@example.com").status_code == 401

    assert _login(client, WRONG_PASSWORD, email="nobody@example.com").status_code == 429


def test_one_accounts_window_does_not_touch_another(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """Keyed per identity: locking one address must not lock the tenant out."""

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))
    for _ in range(LOGIN_POLICY.limit + 1):
        _login(client, WRONG_PASSWORD)

    other = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded_database["login_password"],
        },
    )

    assert other.status_code == 200, other.text


def test_a_successful_login_clears_the_window(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """Four slips and then the right password leaves no debt behind."""

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))
    for _ in range(LOGIN_POLICY.limit - 1):
        assert _login(client, WRONG_PASSWORD).status_code == 401

    assert _login(client, TARGET_PASSWORD).status_code == 200
    assert not list(clean_throttle.scan_iter("fastagent:v2:throttle:login:*"))


# --------------------------------------------------------------------------
# The limiter itself
# --------------------------------------------------------------------------


def test_an_unreachable_limiter_refuses_rather_than_waves_through(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """**Fail closed**, and the cost of that choice is this test's subject.

    A limiter that carried on when it could not be consulted would stop
    protecting anything exactly when nobody was watching, and the system would
    still report itself as protected. So the answer is 503 -- and the cost,
    registered as **T132**, is that while the cache Redis is unreachable nobody
    can log in.

    Pointed at a port nothing is listening on rather than by mocking the client:
    what is under test is the behaviour when Redis is genuinely unreachable, and
    a mock would only prove that a raised exception is handled.
    """

    _seed_employee(seeded_database)
    # `dataclasses.replace`, because `Settings` is `slots=True` and has no
    # `__dict__` to splat -- and `repr=False`, so a failure here will not print
    # the six connection URLs it carries (T13).
    broken = replace(settings, cache_redis_url="redis://127.0.0.1:1/0")
    client = TestClient(create_app(broken))

    response = _login(client, TARGET_PASSWORD)

    assert response.status_code == 503
    assert response.json()["code"] == "THROTTLE_UNAVAILABLE"
    assert response.json()["retryable"] is True


def test_the_counter_is_not_refreshed_by_further_failures(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """`expire(..., nx=True)`, and why it is not a bare `expire`.

    Refreshing the TTL on every failure would let a slow trickle of guesses hold
    the window open indefinitely: the block would never begin because the window
    would never end.

    **The first version of this case could not fail** (independent review
    2026-09-05, P1-2). It recorded two failures milliseconds apart and asserted
    `second <= first` -- but `ttl` answers in whole seconds, so both reads were
    900 and the assertion held whether or not the TTL had been pushed back. The
    reviewer deleted `nx=True` and all nine cases stayed green.

    So the window is **made old** before the second failure. With `nx=True` the
    TTL stays where it was put; without it, it jumps back to the full 900. Five
    seconds is far enough from 900 that no rounding can hide the difference.
    """

    throttle = AttemptThrottle(settings.cache_redis_url)
    key = "fastagent:v2:throttle:login:ttl-case"
    throttle.record_failure(scope="login", identity="ttl-case", policy=LOGIN_POLICY)
    clean_throttle.expire(key, 5)

    throttle.record_failure(scope="login", identity="ttl-case", policy=LOGIN_POLICY)

    assert clean_throttle.ttl(key) <= 5, (
        "a further failure must not push the window back to full length"
    )
    assert clean_throttle.get(key) == "2", "the failure was still counted"


def test_a_key_that_lost_its_expiry_does_not_lock_the_account_forever(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """`ttl` -1 is "no expiry", not "no time left" (independent review P3-2).

    Reachable when `incr` succeeds and `expire` does not -- the process is
    killed, Redis blips. Once the counter is at the limit `guard` raises before
    `record_failure` runs, so the `expire(..., nx=True)` that would have fixed it
    never gets another chance: the account is stuck while every answer keeps
    promising fifteen minutes.

    `guard` therefore repairs the window rather than refusing over it. The
    counter is real and those failures did happen; only the clock is missing.
    """

    throttle = AttemptThrottle(settings.cache_redis_url)
    key = "fastagent:v2:throttle:login:orphan"
    clean_throttle.set(key, LOGIN_POLICY.limit)
    clean_throttle.persist(key)
    assert clean_throttle.ttl(key) == -1

    with pytest.raises(ThrottleExceeded) as blocked:
        throttle.guard(scope="login", identity="orphan", policy=LOGIN_POLICY)

    assert 0 < blocked.value.retry_after_seconds <= LOGIN_POLICY.window_seconds
    assert clean_throttle.ttl(key) > 0, "the window was repaired, not left open-ended"


def test_a_full_limiter_refuses_instead_of_answering_500(
    settings: Settings, seeded_database: dict[str, Any], clean_throttle: Redis
) -> None:
    """P3-1, on the path P1-1's fix made reachable.

    `_guard_password_attempts` wrapped `guard()`; `record_failure` was a bare
    call that raises on `RedisError`. One fault therefore had two answers: 503
    from the read side, a generic 500 from the write side. The window used to be
    narrow -- Redis had to die between the two calls -- and **switching this
    instance to `noeviction` widened it**, because a full Redis answers reads and
    refuses writes.

    Simulated the way it actually happens: `maxmemory` is set to a byte, so the
    `INCR` is refused while the preceding `GET` succeeds. Restored in a `finally`
    -- leaving a one-byte cache behind would fail every case that follows, and
    the failure would look like anything but this test.
    """

    _seed_employee(seeded_database)
    client = TestClient(create_app(settings))
    clean_throttle.config_set("maxmemory", "1")
    try:
        response = _login(client, WRONG_PASSWORD)
    finally:
        clean_throttle.config_set("maxmemory", "0")

    assert response.status_code == 503
    assert response.json()["code"] == "THROTTLE_UNAVAILABLE"


def test_the_local_stack_does_not_evict_the_counters(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The policy is asserted where the policy lives (independent review P1-1).

    The counters were in a Redis configured `allkeys-lru`; the reviewer filled
    the cache and **the victim's counter was evicted, after which the blocked
    account could log in again** -- the guard vanished with nothing recording
    that it had. Stage is covered by `scripts/stage_preflight.py`; this is the
    local half, so a change to one file cannot quietly re-open it.
    """

    compose = (ROOT / "infrastructure" / "local" / "compose.yml").read_text(encoding="utf-8")

    # The **directive**, not the bare word: the file also explains in a comment
    # why the policy is not `allkeys-lru`, and a test that forbade the word would
    # forbid saying why.
    assert "--maxmemory-policy allkeys-lru" not in compose, (
        "the counters must not live in an eviction domain"
    )
    assert compose.count("--maxmemory-policy noeviction") == 2
