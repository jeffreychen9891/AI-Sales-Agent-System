"""The asynchronous half of B1 #12: erase one tenant (parity §6.4, ADR-0092).

**Why this is a worker and B1 #19 is not.** ADR-0056 let the employee purge run
inline because its work is bounded -- four tables carry an employee's identity.
A tenant purge deletes from twenty-one, and ADR-0056's own revision note said
the tenant side 「是真正的异步作业」. ADR-0083 then made the executor a
precondition rather than a promise: `C13` is satisfied because worker and
scheduler now actually run on Stage (S2b, 2026-09-07).

**The deletion itself is not here.** It is `purge_tenant_data`, a
`SECURITY DEFINER` function installed by migration `043`. `fastagent_worker`
holds no `DELETE` on any table -- checked, not assumed -- and granting it
twenty-one of them so that one route can erase a tenant would hand the role
that parses untrusted inbound content a permanent, general erase capability.
This module may execute that one function and nothing else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.auth import subject_fingerprint
from fastagent_v2.runtime.db import Database
from psycopg.types.json import Jsonb

LOGGER = logging.getLogger(__name__)

#: Long enough that a purge of a real tenant finishes inside it, short enough
#: that a worker killed mid-purge does not strand the job for an operator's
#: whole afternoon. The same shape as `OUTBOX_LEASE_SECONDS`, and the same
#: reason: two workers must not both start erasing one tenant.
PURGE_LEASE_SECONDS = 300

#: A purge that has failed this many times stops being retried automatically.
#: It is not silently abandoned -- the row stays `FAILED` with its attempt
#: count, which is what an operator needs in order to ask why.
PURGE_MAX_ATTEMPTS = 3


class TenantPurgeRunner:
    """Claims one queued purge and runs it to a terminal state."""

    def __init__(self, *, database: Database, fingerprint_key: str) -> None:
        self._database = database
        self._fingerprint_key = fingerprint_key
        self._runner_id = f"purge-{uuid4()}"

    def run_one(self) -> UUID | None:
        """Claim and execute at most one purge. Returns the job id, or `None`.

        **Claim and execute are two transactions, deliberately.** The claim must
        be visible to other workers immediately -- that is what the lease is for
        -- and holding one transaction open across the deletion of twenty-one
        tables would keep those locks for the whole job.
        """

        claimed = self._claim_one()
        if claimed is None:
            return None
        tenant_id, job_id, attempt = claimed
        try:
            self._execute(tenant_id=tenant_id, job_id=job_id)
        except Exception:
            # **The tenant is left as it was, not half-erased.** `_execute`
            # runs the deletion and the tombstone in one transaction, so a
            # failure rolls both back; what survives is the job row, and this
            # marks it so an operator sees a stuck purge rather than a silent
            # one.
            self._fail(tenant_id=tenant_id, job_id=job_id, attempt=attempt)
            raise
        return job_id

    def _claim_one(self) -> tuple[UUID, UUID, int] | None:
        """Claim through a definer, because the scan precedes the tenant context.

        **This used to query `tenant_purge_jobs` directly and always found
        nothing** (independent review P0-1). The table is `FORCE ROW LEVEL
        SECURITY` and its policy compares `tenant_id` to `app.tenant_id`; the
        scan runs before the runner knows which tenant it is about to erase, so
        there is no context to set, and with the setting empty the policy hides
        the whole table from `fastagent_worker`. The runner then returned
        `None` -- **the same value it returns when there is genuinely no work**
        -- while the route had already answered `202 accepted`.

        The ten end-to-end cases did not catch it because they built the runner
        from `FASTAGENT_MIGRATION_DATABASE_URL`, a superuser with
        `rolbypassrls`. They now use the worker's own URL.
        """

        with self._database.transaction(tenant_id=None) as connection:
            row = connection.execute(
                """
                SELECT tenant_id, id, attempt
                  FROM claim_tenant_purge_job(%s, %s, %s)
                """,
                (self._runner_id, PURGE_LEASE_SECONDS, PURGE_MAX_ATTEMPTS),
            ).fetchone()
        if row is None:
            return None
        return UUID(str(row["tenant_id"])), UUID(str(row["id"])), int(row["attempt"])

    def _execute(self, *, tenant_id: UUID, job_id: UUID) -> None:
        """Delete, tombstone and settle -- in one per-tenant transaction.

        **Opened on the tenant it is erasing**, so every read here happens
        inside the same RLS the rest of the system obeys: the job row, the
        tenant row and the surviving releases are all visible because the
        context says which tenant this is (independent review P0-1). Only the
        two definers step outside it, and each does exactly one thing.

        **The tombstone and the deletion cannot be separated.** A tenant whose
        rows are gone but whose `code` still reads like a merchant is a record
        that says a purge did not happen; a tombstoned tenant whose data
        survives is the opposite lie. Either both land or neither does.
        """

        with self._database.transaction(tenant_id=tenant_id) as connection:
            # Read the identity *before* destroying it: the fingerprint is
            # computed from the code, and after the update there is nothing
            # left to compute it from. This is a one-way door (T154).
            subject = connection.execute(
                "SELECT code, purged_at, legal_hold FROM tenants WHERE id = %s FOR UPDATE",
                (tenant_id,),
            ).fetchone()
            if subject is None:
                raise RuntimeError("purge job names a tenant that does not exist")
            if subject["legal_hold"]:
                # **Re-read at execution time, not trusted from the queue.** The
                # route refuses a held tenant, but the decision and the erasure
                # are separated in time by design, and a hold placed in between
                # is precisely the case a hold exists for. Nothing in M1a can
                # set the column (T118), so this is unreachable today -- which
                # is the cheapest moment to write it, because the alternative is
                # discovering the gap the first time a hold actually matters.
                #
                # The job is failed rather than left pending: retrying an
                # erasure a hold forbids is the wrong default, and `FAILED`
                # leaves an operator something to look at.
                self._hold(connection, tenant_id=tenant_id, job_id=job_id)
                return
            if subject["purged_at"] is not None:
                # Already tombstoned -- a duplicate claim, not a second erasure.
                self._settle(
                    connection,
                    tenant_id=tenant_id,
                    job_id=job_id,
                    result={"note": "tenant was already purged", "deleted": {}},
                )
                return

            deleted = {
                str(row["table_name"]): int(row["deleted"])
                for row in connection.execute(
                    "SELECT table_name, deleted FROM purge_tenant_data(%s)", (tenant_id,)
                ).fetchall()
            }

            fingerprint = subject_fingerprint(
                key=self._fingerprint_key,
                tenant_id=tenant_id,
                email=str(subject["code"]),
            )
            connection.execute(
                """
                UPDATE tenants
                   SET code = %s,
                       purged_at = now(),
                       subject_fingerprint = %s
                 WHERE id = %s
                """,
                # **`purged-<job id>`, and both halves are constrained.**
                # `tenants.code` is `^[a-z0-9][a-z0-9-]{2,62}$` and `UNIQUE`, so
                # the separator is a hyphen rather than a colon and the job id
                # supplies the uniqueness. **`status` is deliberately not
                # touched**: `026` made `purged_at` the fact precisely so that a
                # purge is not inferred from the shape of a tombstoned string.
                (f"purged-{job_id}", fingerprint, tenant_id),
            )

            # **What survived, addressably** (ADR-0092 §二.2). A residue
            # recorded only in an ADR is a residue nobody can act on when T189
            # is finally ruled.
            retained_releases = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM profile_releases WHERE tenant_id = %s ORDER BY created_at",
                    (tenant_id,),
                ).fetchall()
            ]
            # **Through a definer** (independent review P1-1). `fastagent_worker`
            # holds `INSERT` and nothing else on `audit_events`, deliberately --
            # the role that parses untrusted inbound content has no business
            # reading a tenant's audit trail. Reading it directly raised
            # `permission denied` *after* the deletion and the tombstone had
            # been written, so the whole transaction rolled back and the job
            # failed three times over.
            retained_audit = connection.execute(
                "SELECT tenant_retained_audit_events(%s) AS total", (tenant_id,)
            ).fetchone()
            # The function always returns a row; the assertion is for the type
            # checker and for anyone who later makes it a filtered read.
            assert retained_audit is not None  # noqa: S101
            self._settle(
                connection,
                tenant_id=tenant_id,
                job_id=job_id,
                result={
                    "deleted": deleted,
                    "retained": {
                        # Ids, not a count: T189 will need to reach these rows.
                        "profile_release_ids": retained_releases,
                        "audit_event_count": int(retained_audit["total"]),
                        "tenant_row": "tombstoned",
                    },
                    "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )

    def _settle(
        self, connection: Any, *, tenant_id: UUID, job_id: UUID, result: dict[str, object]
    ) -> None:
        # **Merged, not replaced** (independent review P3-2). The
        # already-purged branch settles with a short note, and replacing the
        # whole column with it would erase the retained-residue list an earlier
        # attempt wrote -- the one thing ADR-0092 §二.2 requires to stay
        # addressable.
        connection.execute(
            """
            UPDATE tenant_purge_jobs
               SET status = 'SUCCEEDED',
                   result = tenant_purge_jobs.result || %s,
                   updated_at = now(),
                   lease_owner = NULL, lease_expires_at = NULL
             WHERE tenant_id = %s AND id = %s
            """,
            (Jsonb(result), tenant_id, job_id),
        )

    def _hold(self, connection: Any, *, tenant_id: UUID, job_id: UUID) -> None:
        """Settle a job the tenant's legal hold forbids.

        `FAILED` rather than `PENDING`: a hold is not a transient condition to
        retry past, and the result says which of the terminal states this is so
        an operator is not left comparing timestamps to work it out.
        """

        connection.execute(
            """
            UPDATE tenant_purge_jobs
               SET status = 'FAILED',
                   result = %s,
                   lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
             WHERE tenant_id = %s AND id = %s
            """,
            (
                Jsonb({"refused": "tenant is under legal hold", "deleted": {}}),
                tenant_id,
                job_id,
            ),
        )

    def _fail(self, *, tenant_id: UUID, job_id: UUID, attempt: int) -> None:
        with self._database.transaction(tenant_id=tenant_id) as connection:
            connection.execute(
                """
                UPDATE tenant_purge_jobs
                   SET status = CASE WHEN %s >= %s THEN 'FAILED' ELSE 'PENDING' END,
                       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (attempt, PURGE_MAX_ATTEMPTS, tenant_id, job_id),
            )
