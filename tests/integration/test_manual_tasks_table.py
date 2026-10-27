"""Migration 034: the table a person's work now lives on (ADR-0048 K18).

**Why these assertions and not the routes'.** Batches B and C add six routes on
top of this table, and every one of them will rely on the database refusing the
states it cannot represent -- `claimed` with nobody holding it, an owner from
another tenant, a row one tenant can read from another's session. Those refusals
are written in `034`, so they are tested here, once, rather than re-tested from
six route bodies that would each only exercise the paths they happen to take.

**The engine's write is not tested here** -- it is tested where it happens, by
the e2e cases that already read the classification back (`test_manager_digest`,
`test_retry_budget`, `test_m1a_no_send`, `test_m1a_cost`); after 034 they read
it through a join onto this table, so a pipeline that stopped filing tasks fails
them all.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection
from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _open_task(*, tenant_id: UUID, task_id: UUID | None = None) -> UUID:
    """One filed task, written the way the pipeline writes it: no owner, no SLA."""

    task_id = task_id or uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO manual_tasks(
                tenant_id, id, conversation_id, task_class, notify, reason_code
            ) VALUES (%s, %s, %s, 'merchant_action', 'batched', 'DELIVERY_UNCONFIRMED')
            """,
            (tenant_id, task_id, uuid4()),
        )
        connection.commit()
    return task_id


def test_a_claimed_task_cannot_be_claimed_by_nobody(seeded_database: dict[str, object]) -> None:
    """`claimed` and its owner travel together, or `claim` is a no-op with a label.

    B3 #17 sets `status`, `owner_employee_id` and `claimed_at` in one statement.
    Nothing forces a later route -- or a repair script, or a support session with
    psql -- to keep those three together, and a `claimed` task with no owner is
    invisible to every "who is on this?" query while still being off the open
    list. It is the shape that loses work silently, so the database refuses it.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    task_id = _open_task(tenant_id=tenant_a)
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                "UPDATE manual_tasks SET status = 'claimed' WHERE tenant_id = %s AND id = %s",
                (tenant_a, task_id),
            )
        connection.rollback()
        # ...and the whole move is accepted, which is what makes the refusal a
        # constraint on shape rather than on the transition.
        connection.execute(
            """
            UPDATE manual_tasks
               SET status = 'claimed', owner_employee_id = %s, claimed_at = now()
             WHERE tenant_id = %s AND id = %s
            """,
            (UUID(str(seeded_database["employee_a"])), tenant_a, task_id),
        )
        connection.commit()


def test_a_resolved_task_carries_the_time_it_was_resolved(
    seeded_database: dict[str, object],
) -> None:
    """The same rule for the other terminal state B3 #18 writes.

    `resolved_at` is what an SLA report subtracts from; a resolved row without it
    is a task that was done at no particular time, and the report cannot tell
    that from a task still open.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    task_id = _open_task(tenant_id=tenant_a)
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                "UPDATE manual_tasks SET status = 'resolved' WHERE tenant_id = %s AND id = %s",
                (tenant_a, task_id),
            )
        connection.rollback()


def test_an_unowned_task_is_allowed_and_a_foreign_owner_is_not(
    seeded_database: dict[str, object],
) -> None:
    """Both halves of the composite foreign key, in one case because they are one rule.

    **Unowned must stay legal**: the engine has no source for an owner (T91), so
    a constraint that required one would stop the `manual_review` hook filing
    anything at all. `MATCH SIMPLE` gives that for free -- a row with NULL in the
    nullable half satisfies the key -- and this asserts it is actually what the
    column does rather than what I expect it to do.

    **An owner from another tenant must not be**: `(tenant_id, owner_employee_id)`
    references `employees(tenant_id, id)`, so assigning tenant B's employee to
    tenant A's task fails in the database. Without the pair, `owner_employee_id`
    alone would reference a globally unique employee id and B3 #19's reassign
    route would be one typo away from handing a task across a tenant boundary.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    employee_b = UUID(str(seeded_database["employee_b"]))
    task_id = _open_task(tenant_id=tenant_a)
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT owner_employee_id, sla_due_at, expires_at, status FROM manual_tasks"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, task_id),
        ).fetchone()
        assert row is not None
        assert row["owner_employee_id"] is None
        assert row["sla_due_at"] is None
        assert row["expires_at"] is None
        assert row["status"] == "open"

        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                """
                UPDATE manual_tasks
                   SET status = 'claimed', owner_employee_id = %s, claimed_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (employee_b, tenant_a, task_id),
            )
        connection.rollback()


def test_the_worker_sees_only_its_own_tenants_tasks(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """`FORCE ROW LEVEL SECURITY`, measured rather than declared.

    The lesson is B1 #8's, one batch old: I justified `FORCE` on
    `platform_audit_events` with a claim about the owner that the review
    measured false. So this reads across the boundary through the role that
    actually files tasks, with the session variable the policy reads set to the
    other tenant, and asserts the row is not there.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    task_id = _open_task(tenant_id=tenant_a)

    worker = Database(settings.worker_database_url)
    with worker.transaction(tenant_id=tenant_a) as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM manual_tasks WHERE id = %s", (task_id,)
            ).fetchone()["n"]
            == 1
        )
    with worker.transaction(tenant_id=tenant_b) as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS n FROM manual_tasks WHERE id = %s", (task_id,)
            ).fetchone()["n"]
            == 0
        )


def test_the_worker_files_and_digests_but_never_deletes(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """`GRANT SELECT, INSERT, UPDATE` -- and the omission is the point.

    A task is the record that somebody had to do something. The worker files it
    and marks it digested; removing one is a person's decision, made through
    `cancel` (B3 #20), which leaves the row and its reason behind. `DELETE` is
    withheld so that "the digest ran and the backlog emptied" cannot become a
    thing this service is able to do by accident.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    task_id = _open_task(tenant_id=tenant_a)

    worker = Database(settings.worker_database_url)
    with worker.transaction(tenant_id=tenant_a) as connection:
        connection.execute(
            "UPDATE manual_tasks SET digested_at = now() WHERE tenant_id = %s AND id = %s",
            (tenant_a, task_id),
        )
    with pytest.raises(InsufficientPrivilege):  # noqa: PT012
        with worker.transaction(tenant_id=tenant_a) as connection:
            connection.execute(
                "DELETE FROM manual_tasks WHERE tenant_id = %s AND id = %s", (tenant_a, task_id)
            )
