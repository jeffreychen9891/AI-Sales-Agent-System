-- 043: the tenant tombstone, the purge work queue, and the one function that
-- does the deleting (route freeze B1 #12, parity §6.4, ADR-0092).
--
-- **Three things, and the third is the security decision of this migration.**
--
-- `026` gave `employees` its two tombstone columns; this gives `tenants` the
-- same pair, for the same reason and with the same shapes. `019`/`032` already
-- put `disabled_at`, `purge_eligible_at` and `legal_hold` on the tenant, so the
-- retention clock and the hold this route reads are both here already.
--
-- **What a purge does not destroy is recorded, not implied** (ADR-0092 §二):
-- `profile_releases` is append-only by `006`'s trigger, and `audit_events`
-- survives by §6.4. So the job row keeps the ids of what it left behind --
-- a residue nobody can address later is a residue nobody can act on.

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS purged_at timestamptz;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS subject_fingerprint text
    CHECK (subject_fingerprint IS NULL OR subject_fingerprint ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN tenants.purged_at IS
    'When the tenant purge completed (parity §6.4). The row survives because '
    'audit_events.tenant_id references it and §6.4 forbids deleting the audit; '
    'what is destroyed is the identity, not the reference.';
COMMENT ON COLUMN tenants.subject_fingerprint IS
    'Keyed HMAC over the pre-purge code (T154 shape, same as employees). '
    'Answers only whether two purged records were the same subject. The code '
    'is destroyed, so this can never be recomputed.';

-- **The work queue.** ADR-0083 §四.2 left the shape to this batch: a table of
-- its own rather than `scheduled_events` (that is a conversation timer, keyed
-- to an aggregate a purge is about to delete) and rather than the Redis queue
-- (a purge must survive a queue flush -- it is the only operation here whose
-- loss is silently irreversible in the other direction: the caller was told
-- the tenant would be erased).
CREATE TABLE IF NOT EXISTS tenant_purge_jobs (
    -- **Tenant-owned, and that is not a formality.** The job names exactly one
    -- tenant and is read back through the same RLS every other row obeys.
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,

    -- Who asked, and why. §6.4 requires the reason to be submitted again at
    -- purge time rather than inherited from the deactivation, so it is stored
    -- with the job that acted on it, not looked up from an earlier row.
    requested_by uuid NOT NULL,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),

    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED'
    )),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),

    -- The same lease shape `outbox_messages` uses, for the same reason: two
    -- workers must not both start erasing one tenant.
    lease_owner text,
    lease_expires_at timestamptz,

    -- **What was destroyed and what was kept** (ADR-0092 §二.2). Written by the
    -- worker when the job settles; the retained ids are the address a future
    -- ruling on T189 needs.
    result jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id)
);

-- **One *live* job per tenant, not one job ever** (independent review P2-1).
--
-- A blanket `UNIQUE (tenant_id)` also pinned the tenant after the job reached a
-- terminal state: a purge that failed three times left a `FAILED` row that the
-- route's `ON CONFLICT` then answered as "already queued" -- while nothing was
-- queued, and no role in M1a can delete or reset that row (`fastagent_app` has
-- `SELECT, INSERT`; `fastagent_worker` has `SELECT, UPDATE`). The legal-hold
-- path made it plainest: hold refuses the purge, the job fails, the hold is
-- later lifted, and the tenant can never be queued again.
--
-- The partial index keeps the property that mattered -- two purges of one
-- tenant cannot be in flight together -- and lets the history accumulate, which
-- is also what keeps the `result` of an earlier attempt (its retained-residue
-- list, ADR-0092 §二.2) from being overwritten by a later one.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_purge_jobs_live_idx
    ON tenant_purge_jobs (tenant_id) WHERE status IN ('PENDING', 'RUNNING');

ALTER TABLE tenant_purge_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_purge_jobs FORCE ROW LEVEL SECURITY;
DO $tenant_purge_jobs_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'tenant_purge_jobs'
           AND policyname = 'tenant_purge_jobs_tenant_isolation'
    ) THEN
        CREATE POLICY tenant_purge_jobs_tenant_isolation ON tenant_purge_jobs
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$tenant_purge_jobs_rls$;

-- The worker's claim scan: the oldest unclaimed job, across tenants.
CREATE INDEX IF NOT EXISTS tenant_purge_jobs_claimable_idx
    ON tenant_purge_jobs (status, created_at) WHERE status IN ('PENDING', 'RUNNING');

COMMENT ON TABLE tenant_purge_jobs IS
    'One asynchronous tenant purge (route freeze B1 #12, parity §6.4). Its own '
    'table rather than scheduled_events or the Redis queue: a purge must '
    'survive a queue flush, and the aggregate a conversation timer hangs from '
    'is one of the things being deleted.';

-- **The deletion runs as a definer function, and the alternative is why.**
--
-- `fastagent_worker` holds no DELETE on any table today -- checked, not
-- assumed. Granting it DELETE across the twenty-one tenant-owned tables it
-- would need (24 carry `tenant_id`; three are deliberately kept)
-- would hand the role that parses untrusted inbound content a general erase
-- capability over the whole database, permanently, so that one route can use
-- it. A definer function keeps the privilege attached to a fixed, reviewed
-- body: the worker may run *this* deletion and gains nothing else.
--
-- **`SET search_path` is not decoration.** A definer function without it
-- resolves names against the caller's path, which is how a definer becomes a
-- privilege escalation. Every definer in this repository carries it (`030`,
-- `031`) and this one is deleting rows.
--
-- **The table list is explicit and ordered, not derived.** A loop over
-- `information_schema` would silently pick up whatever exists at run time,
-- which is the opposite of what a destructive operation should do: a table
-- added later must be *classified* by a person -- deleted with the tenant, or
-- deliberately kept -- and an unreviewed new table joining a purge by accident
-- is worse than one being missed loudly.
--
-- **What is deliberately absent from the list:**
--   * `audit_events`      -- §6.4: the audit is not deleted;
--   * `profile_releases`  -- append-only by `006`'s trigger; a DELETE here
--                            raises. Kept, and ADR-0092 §一 states the real
--                            reason: the surviving audit records decisions made
--                            *under a release*, so deleting releases leaves the
--                            audit pointing at nothing. **That is not a claim
--                            that profile content is free of personal data**
--                            (T189 is open on exactly that);
--   * `tenants`           -- tombstoned by the caller, not deleted: the audit
--                            rows reference it.
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    -- Children before parents. `outbox_messages` and `delivery_receipts`
    -- reference `intent_records` and `conversation_aggregates`; `employee_roles`
    -- references `employees` and `roles`; `contacts` references
    -- `channel_bindings`. Getting this order wrong surfaces as a foreign key
    -- violation, which is loud -- but it would abort a partially completed
    -- purge, and a purge that half-ran is the worst of the three outcomes.
    -- **This order is derived from `pg_constraint`, not reasoned out**
    -- (independent review P0-2). The first version was hand-ordered with a
    -- comment claiming "children before parents", and it violated four foreign
    -- keys -- `policy_decisions` after `intent_records`, `contacts` before
    -- `conversation_aggregates`, `conversation_aggregates` before
    -- `model_invocation_costs`, `channel_bindings` before
    -- `tenant_manager_contacts`. All four are `NO ACTION` and not deferrable,
    -- and every one of them is triggered by a row an ordinary serving tenant
    -- has. The comment had reasoned correctly about the pairs it looked at and
    -- said nothing about the rest, which is the same failure as T184 and T185 --
    -- this time inside the least reversible function in the repository.
    --
    -- `test_the_purge_order_satisfies_every_foreign_key` now recomputes this
    -- from the catalogue on every run, so the next table cannot be placed by
    -- eye either.
    ordered_tables text[] := ARRAY[
        'delivery_receipts',
        'employee_read_cursors',
        'employee_roles',
        'inbox_events',
        'ingress_replay_keys',
        'manual_tasks',
        'model_invocation_costs',
        'outbox_messages',
        'policy_decisions',
        'intent_records',
        'refresh_tokens',
        'roles',
        'scheduled_events',
        'conversation_aggregates',
        'contacts',
        'tenant_cost_ceilings',
        'tenant_manager_contacts',
        'channel_bindings',
        'ws_tickets',
        'employee_sessions',
        'employees'
    ];
    current_table text;
    removed bigint;
BEGIN
    FOREACH current_table IN ARRAY ordered_tables LOOP
        EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1', current_table)
            USING target_tenant;
        GET DIAGNOSTICS removed = ROW_COUNT;
        table_name := current_table;
        deleted := removed;
        RETURN NEXT;
    END LOOP;
END
$function$;

COMMENT ON FUNCTION purge_tenant_data(uuid) IS
    'Deletes one tenant''s rows from every tenant-owned table except '
    'audit_events (§6.4), profile_releases (append-only, ADR-0092) and tenants '
    'itself (tombstoned by the caller). Returns per-table counts so the job can '
    'record what it destroyed.';

-- **The claim scan has to be a definer too** (independent review P0-1).
--
-- `tenant_purge_jobs` is `FORCE ROW LEVEL SECURITY` and its policy compares
-- `tenant_id` against `app.tenant_id`. The worker's claim scan runs *before* it
-- knows which tenant it is about to work on, so there is no context to set --
-- and with the setting empty the policy's right-hand side is NULL, which makes
-- the entire table invisible to `fastagent_worker` (`NOSUPERUSER
-- NOBYPASSRLS`). **The runner therefore saw no work and said so by returning
-- the same value it returns when there genuinely is none.** The route answered
-- `202 accepted`, the row stayed `PENDING`, and nothing anywhere said the
-- erasure was not happening.
--
-- The shape is the one `list_active_tenant_ids()` already uses for the other
-- cross-tenant scan in this system: a definer picks the row, and the caller
-- then opens an ordinary per-tenant transaction to do the work inside RLS.
--
-- Claiming is done here rather than in Python so that the select and the lease
-- write are one statement: two runners must not both walk away with the same
-- job, and `FOR UPDATE SKIP LOCKED` only holds that if nothing sits between
-- them.
CREATE OR REPLACE FUNCTION claim_tenant_purge_job(
    runner text, lease_seconds integer, max_attempts integer
)
RETURNS TABLE (tenant_id uuid, id uuid, attempt integer)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate public.tenant_purge_jobs%ROWTYPE;
BEGIN
    SELECT * INTO candidate
      FROM public.tenant_purge_jobs job
     WHERE job.status = 'PENDING'
        OR (job.status = 'RUNNING' AND job.lease_expires_at < now())
     ORDER BY job.created_at
       FOR UPDATE SKIP LOCKED
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF candidate.attempt >= max_attempts THEN
        -- Out of attempts: settle it rather than handing it out forever. The
        -- row keeps its count, so "why is this tenant still here" has an answer.
        UPDATE public.tenant_purge_jobs
           SET status = 'FAILED', updated_at = now(),
               lease_owner = NULL, lease_expires_at = NULL
         WHERE tenant_purge_jobs.tenant_id = candidate.tenant_id
           AND tenant_purge_jobs.id = candidate.id;
        RETURN;
    END IF;
    UPDATE public.tenant_purge_jobs
       SET status = 'RUNNING',
           attempt = tenant_purge_jobs.attempt + 1,
           lease_owner = runner,
           lease_expires_at = now() + make_interval(secs => lease_seconds),
           updated_at = now()
     WHERE tenant_purge_jobs.tenant_id = candidate.tenant_id
       AND tenant_purge_jobs.id = candidate.id;
    tenant_id := candidate.tenant_id;
    id := candidate.id;
    attempt := candidate.attempt + 1;
    RETURN NEXT;
END
$function$;

COMMENT ON FUNCTION claim_tenant_purge_job(text, integer, integer) IS
    'Claims the oldest claimable tenant purge job and returns it. A definer '
    'because the scan precedes knowing the tenant, so no RLS context exists '
    'yet (independent review P0-1); the caller then works inside a per-tenant '
    'transaction, as list_active_tenant_ids() callers do.';

-- **The retained-residue read is a definer as well** (independent review P1-1).
--
-- `_execute` records how many audit rows survived, and `fastagent_worker` holds
-- only `INSERT` on `audit_events` -- by design: the role that parses untrusted
-- inbound content has no business reading a tenant's audit trail. Granting it
-- `SELECT` to satisfy one count would undo the reason this batch chose a
-- definer in the first place, so the count comes back through one instead.
CREATE OR REPLACE FUNCTION tenant_retained_audit_events(target_tenant uuid)
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT count(*) FROM public.audit_events WHERE tenant_id = target_tenant;
$function$;

COMMENT ON FUNCTION tenant_retained_audit_events(uuid) IS
    'How many audit rows survived a tenant purge (parity §6.4). A definer so '
    'that fastagent_worker keeps INSERT-only access to audit_events.';

-- PUBLIC keeps EXECUTE on a new function unless it is taken away, and a
-- definer that everyone may call is the definer being the vulnerability.
REVOKE ALL ON FUNCTION purge_tenant_data(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION claim_tenant_purge_job(text, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION tenant_retained_audit_events(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION purge_tenant_data(uuid) TO fastagent_worker;
GRANT EXECUTE ON FUNCTION claim_tenant_purge_job(text, integer, integer) TO fastagent_worker;
GRANT EXECUTE ON FUNCTION tenant_retained_audit_events(uuid) TO fastagent_worker;

-- **The api writes the job and the tombstone; the worker runs the job.**
--
-- The api gets no DELETE anywhere from this migration: it queues work and
-- stamps the tenant, and the erasing belongs to the function above.
GRANT SELECT, INSERT ON tenant_purge_jobs TO fastagent_app;
GRANT SELECT, UPDATE ON tenant_purge_jobs TO fastagent_worker;
-- **`status` is not in this list, and `026` is why.** The employee purge
-- leaves `status` alone: `purged_at` is the fact, and inferring a purge from
-- the shape of a tombstoned identifier would be reading a string pattern as
-- a state. The tenant tombstone follows that precedent exactly, so the
-- `status` CHECK needs no new value and this grant needs no `status`.
GRANT UPDATE (code, purged_at, subject_fingerprint) ON tenants TO fastagent_worker;

INSERT INTO schema_migrations(version) VALUES ('043_tenant_purge')
ON CONFLICT (version) DO NOTHING;
