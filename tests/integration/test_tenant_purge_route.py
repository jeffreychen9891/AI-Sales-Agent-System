"""B1 #12 `POST /admin/tenants/{tenant_id}/purge` (parity §6.4, ADR-0083 / ADR-0092).

**The cases that carry this batch are the five refusals and
`test_the_receipt_says_accepted_not_succeeded`.** The route itself writes two
rows; what makes it correct is that each of the five states it refuses gets its
own answer, and that it never claims the erasure has happened.

The erasing is the worker's, and its own file tests it.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

REASON = "merchant contract ended and retention expired"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    """Straight into the row, for the reason B1 #11's test gives.

    `tenant.delete` is platform-only (ADR-0092 follows ADR-0075 here), so no
    role can carry it and `GET /permissions` does not offer it. The deployment
    writing the column is the sanctioned path.
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


def _operator_a(
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


def _make_purgeable(tenant_id: Any, *, legal_hold: bool = False, eligible: bool = True) -> None:
    """Put a tenant in the state a purge requires: stopped, and out of retention.

    The eligibility boundary is passed as a parameter rather than built into the
    statement: a test helper that interpolates into SQL is the shape the gate
    refuses (`S608`), and here it would also be the *only* place in the suite
    where the retention clock is written by string concatenation.
    """

    boundary = datetime.now(UTC) + (timedelta(days=-1) if eligible else timedelta(days=1))
    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE tenants
               SET status = 'disabled',
                   disabled_at = now() - interval '91 days',
                   purge_eligible_at = %s,
                   legal_hold = %s
             WHERE id = %s
            """,
            (boundary, legal_hold, tenant_id),
        )
        connection.commit()


def _purge(
    client: TestClient, headers: dict[str, str], tenant_id: Any, reason: str = REASON
) -> Any:
    return client.post(
        f"/admin/tenants/{tenant_id}/purge",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"reason": reason},
    )


def _job_rows(tenant_id: Any) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT * FROM tenant_purge_jobs WHERE tenant_id = %s", (tenant_id,)
        ).fetchall()


def test_a_promoted_subject_queues_a_purge_for_an_eligible_tenant(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    response = _purge(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 202, response.text
    jobs = _job_rows(seeded_database["tenant_b"])
    assert len(jobs) == 1
    assert jobs[0]["status"] == "PENDING"
    assert jobs[0]["reason"] == REASON


def test_the_receipt_says_accepted_not_succeeded(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The one word that separates this route from B1 #19.

    ADR-0056 let the employee purge answer `succeeded` because it had already
    done the work. Here the work is queued, and `succeeded` would promise an
    erasure that has not happened -- the shape
    `PUT /channel-bindings/{id}/profile-release` refused, and the reason
    ADR-0083 made the executor a precondition rather than a promise.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    body = _purge(client, headers, seeded_database["tenant_b"]).json()

    assert body["receipt"]["status"] == "accepted"
    assert body["result"]["status"] == "PENDING"


def test_the_response_says_what_the_purge_will_not_destroy(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """ADR-0092 §二.1: told, not implied.

    A caller of something named "purge" should not have to read an ADR to learn
    what it keeps. The `profile_releases` entry deliberately does **not** claim
    the content is free of personal data -- T189 is open on exactly that -- and
    this case pins that wording, because the tempting edit is to shorten it into
    the claim the user rejected.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    retained = _purge(client, headers, seeded_database["tenant_b"]).json()["result"]["retained"]

    assert set(retained) == {"audit_events", "profile_releases", "tenant_row"}
    assert "not a statement about whether profile content carries personal data" in (
        retained["profile_releases"]
    )


@pytest.mark.parametrize(
    ("prepare", "expected"),
    [
        (lambda tid: None, "TENANT_NOT_DISABLED"),
        (lambda tid: _make_purgeable(tid, eligible=False), "TENANT_NOT_YET_ELIGIBLE"),
        (lambda tid: _make_purgeable(tid, legal_hold=True), "TENANT_UNDER_LEGAL_HOLD"),
    ],
)
def test_each_refusal_has_its_own_code(
    settings: Settings,
    seeded_database: dict[str, Any],
    tmp_path: Path,
    prepare: Any,
    expected: str,
) -> None:
    """`409` alone tells an operator to retry, and none of these succeeds on retry.

    The same reason B1 #19 gives for its four codes. An operator who reads
    "conflict" and waits will wait forever on a legal hold.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    prepare(seeded_database["tenant_b"])

    response = _purge(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 409, response.text
    assert response.json()["code"] == expected
    assert _job_rows(seeded_database["tenant_b"]) == []


def test_a_second_request_while_one_is_queued_is_a_conflict(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """One job per tenant. A retry must not queue a second erasure.

    The database constraint is what makes this true under a race; the route
    turns it into an answer instead of an integrity error.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    assert _purge(client, headers, seeded_database["tenant_b"]).status_code == 202
    second = _purge(client, headers, seeded_database["tenant_b"])

    assert second.status_code == 409
    assert second.json()["code"] == "TENANT_PURGE_ALREADY_QUEUED"
    assert len(_job_rows(seeded_database["tenant_b"])) == 1


def test_holding_the_permission_without_promotion_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The permission is never enough -- the deployment has to have promoted them.

    Same shape as B1 #11's equivalent case. Without this, `tenant.delete` would
    be an ordinary permission that any sufficiently privileged tenant
    administrator could be granted, and it erases another merchant's data.
    """

    client, headers = _operator_a(settings, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    response = _purge(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 403, response.text
    assert _job_rows(seeded_database["tenant_b"]) == []


def test_a_reason_too_short_to_mean_anything_is_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    response = _purge(client, headers, seeded_database["tenant_b"], reason="x")

    assert response.status_code == 422
    assert _job_rows(seeded_database["tenant_b"]) == []


def test_the_reach_is_recorded_with_its_reason(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """A cross-tenant erasure that leaves no trace of *why* is what §5.2 prevents."""

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    _purge(client, headers, seeded_database["tenant_b"])

    with _migration_connection() as connection:
        event = connection.execute(
            """
            SELECT action, reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'tenant.delete'
            """,
            (seeded_database["tenant_b"],),
        ).fetchone()
    assert event is not None
    assert event["reason_code"] == "TENANT_PURGE_QUEUED"
    assert event["evidence"]["reason"] == REASON


def test_the_action_is_in_no_role_and_on_no_permission_list(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`tenant.delete` must be unreachable by grant, exactly like `tenant.update`.

    ADR-0075's reasoning applies with more force here: `owner` is *defined* as
    every grantable action, so without the platform-only listing every merchant
    administrator would hold the power to erase their own tenant -- irreversibly,
    with nothing in M1a able to undo it.
    """

    from fastagent_v2.runtime.authz import assignable_permissions, platform_only_actions
    from fastagent_v2.runtime.system_roles import system_role_permissions

    assert "tenant.delete" in platform_only_actions()
    assert "tenant.delete" not in assignable_permissions()
    for role, permissions in system_role_permissions().items():
        assert "tenant.delete" not in permissions, role


def test_purging_a_tenant_that_does_not_exist_says_so(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])

    response = _purge(client, headers, uuid4())

    assert response.status_code in {403, 404}, response.text


def test_the_other_tenant_is_untouched(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The blast radius stops at the named tenant -- checked, not assumed."""

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    _purge(client, headers, seeded_database["tenant_b"])

    assert _job_rows(seeded_database["tenant_a"]) == []
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT status, purged_at FROM tenants WHERE id = %s",
            (seeded_database["tenant_a"],),
        ).fetchone()
    assert row is not None
    assert row["status"] == "active"
    assert row["purged_at"] is None


def test_the_route_does_not_erase_anything_itself(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """Queuing is the whole of the route's effect.

    The tenant is still there, still holding its employees, until the worker
    runs. A route that started deleting inline would be B1 #19's shape applied
    to work ADR-0056 said is unbounded.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    _make_purgeable(seeded_database["tenant_b"])

    _purge(client, headers, seeded_database["tenant_b"])

    with _migration_connection() as connection:
        tenant = connection.execute(
            "SELECT status, purged_at, code FROM tenants WHERE id = %s",
            (seeded_database["tenant_b"],),
        ).fetchone()
        employees = connection.execute(
            "SELECT count(*) AS total FROM employees WHERE tenant_id = %s",
            (seeded_database["tenant_b"],),
        ).fetchone()
    assert tenant is not None and employees is not None
    assert tenant["purged_at"] is None
    assert tenant["code"] == "tenant-b"
    assert employees["total"] >= 1


def test_a_tenant_eligible_as_of_this_instant_may_be_purged(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The boundary itself, which is where the two clocks used to disagree.

    `_make_purgeable` puts the boundary a whole day to one side, so it gives the
    same answer whichever clock decides. This sets `purge_eligible_at` to the
    database's own `now()`: by the time the route reads the row the database has
    moved past it, so "eligible" is the only correct answer.

    The guard used to compare that column against `datetime.now(UTC)`. Whenever
    this process ran behind the database -- half of every thirty-second cycle on
    this host, per T193 -- the boundary still looked future and the purge was
    refused. The comment there argued the crossing was safe because a process
    behind the database only ever refuses; **it never said what happens when the
    process runs ahead, which is that a tenant becomes erasable early.**
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE tenants
               SET status = 'disabled',
                   disabled_at = now() - interval '91 days',
                   purge_eligible_at = now(),
                   legal_hold = false
             WHERE id = %s
            """,
            (seeded_database["tenant_b"],),
        )
        connection.commit()

    response = _purge(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 202, response.text


def test_the_eligibility_is_decided_in_sql_and_not_against_a_process_clock() -> None:
    """The half the behavioural case above cannot cover.

    `test_a_tenant_eligible_as_of_this_instant_may_be_purged` only goes red when
    the offset happens to point one way at that moment -- it catches a process
    clock running *behind* the database and says nothing when it runs ahead,
    which is the direction that authorises an early erasure. This one cannot
    miss, and the pair is the point: the ingress half of this batch is guarded
    the same way, for the same reason.

    The count is part of the assertion. There are **two** retention guards of
    this shape -- the tenant purge and the employee purge -- and the whole
    argument for touching the tenant one is that a rule with one exception reads
    as a rule with an exception. If a third appears, or one of these two drifts
    back, this case is where it surfaces.
    """

    source = Path(inspect.getfile(create_app)).read_text(encoding="utf-8")

    assert source.count(
        "SELECT %s::timestamptz IS NOT NULL AND %s::timestamptz <= now() AS due"
    ) == 2, (
        "both retention guards -- tenant purge and employee purge -- decide "
        "eligibility in SQL, on the clock that wrote the column"
    )
    assert 'subject["purge_eligible_at"] > datetime.now(' not in source, (
        "comparing a database instant to this process's clock is the shape "
        "T183, F8 (2026-09-05) and this batch all exist to remove"
    )


def test_a_tenant_with_no_retention_clock_is_refused_and_not_crashed(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """A NULL `purge_eligible_at` must answer 409, not 500.

    **Independent review 2026-09-07, P2**: this branch used to be held by
    `purge_eligible_at is None or ...` in Python, and moving the comparison into
    SQL dropped it. `%s IS NOT NULL` gives PostgreSQL nothing to infer a type
    from, psycopg sends `None` with OID 0, and the statement fails to plan --
    `IndeterminateDatatype` -- so a refusal with a defined code became an
    unhandled 500. Still fail-closed, in that nothing is erased, but "refuse"
    and "crash" are not the same answer.

    The row this builds is not reachable through any route (both deactivation
    paths write `status`, `disabled_at` and `purge_eligible_at` together, and
    `tenants_disabled_pair` pairs the last two). It is written straight to the
    database on purpose: **a defensive branch nobody can reach is exactly the
    kind that rots**, and this one had already rotted once.
    """

    promoted = _promoted(settings, tmp_path, seeded_database)
    client, headers = _operator_a(promoted, seeded_database, ["tenant.delete"])
    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE tenants
               SET status = 'disabled',
                   disabled_at = NULL,
                   purge_eligible_at = NULL,
                   legal_hold = false
             WHERE id = %s
            """,
            (seeded_database["tenant_b"],),
        )
        connection.commit()

    response = _purge(client, headers, seeded_database["tenant_b"])

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "TENANT_NOT_YET_ELIGIBLE"
