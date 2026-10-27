-- 034: manual tasks get their own table (ADR-0048 K18), and the three fields
-- `conversation-engine.md` §8 has always required (T91).
--
-- **Why a table and not two more columns on `intent_records`.** K18's reasoning,
-- restated because it is the whole of the ruling: the six routes want
-- `claim` / `resolve` / `reassign` / `cancel`, so **authorization needs a
-- resource of its own** -- and parasitising on `intent_records.kind='manual_task'`
-- puts that authorization on `intent_record`, whose `execute` action ADR-0047
-- §二.6 had just declared non-grantable to a person. Two rulings would have been
-- in direct conflict.
--
-- **The three columns are moved, not copied.** A row that lives in two places
-- drifts, and the drift is silent: `013`'s CHECK requires every
-- `kind = 'manual_task'` intent to carry both classification columns, so
-- "stop writing them and leave them there" is not even available -- the next
-- task the engine files would violate it. So the data comes across, the CHECK
-- goes, the index goes, and the columns go, in this order and in one migration.
--
-- **`digested_at` comes too.** `014` put it on `intent_records`, and the only
-- code that reads or writes it is the manual-task digest (`digest.py:144, :327`).
-- A column whose sole consumer moves house belongs in the new house.

CREATE TABLE IF NOT EXISTS manual_tasks (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    -- The intent that proposed it, when the engine filed it. **Nullable**,
    -- because `POST /conversations/{id}/manual-tasks` (freeze B3 #16) lets a
    -- person file one directly and there is no intent behind that.
    intent_id uuid,

    -- parity §243 fixes this vocabulary: 「状态只允许 open/claimed/resolved/
    -- cancelled/expired」. `expired` has no producer yet -- nothing sweeps -- and
    -- that is T91's other half, left visible here rather than omitted.
    status text NOT NULL DEFAULT 'open' CHECK (status IN (
        'open', 'claimed', 'resolved', 'cancelled', 'expired'
    )),

    -- Carried over from `013`, same vocabulary and the same reason: the
    -- difference between a merchant action and a platform-internal one decides
    -- who is told, and `notify` decides how loudly.
    task_class text NOT NULL CHECK (task_class IN ('merchant_action', 'platform_internal')),
    notify text NOT NULL CHECK (notify IN ('immediate', 'batched', 'none')),
    reason_code text,

    -- **T91's three, and they are nullable on purpose.** §8 says the
    -- `manual_review` hook creates a task *with* owner, SLA and expiry -- and
    -- the engine has no source for any of them: no drawing states an SLA
    -- duration, and no route assigns an owner at filing time. What the engine
    -- can honestly write is a task with none of the three, which is exactly what
    -- T91 registered as the problem. **The columns exist so the human side can
    -- fill them** (freeze B3 #16 makes owner and SLA required *there*), and so
    -- that "this task has no owner" becomes a queryable fact instead of an
    -- absence nobody can see.
    owner_employee_id uuid,
    sla_due_at timestamptz,
    expires_at timestamptz,

    -- What the task is about. Free-form by kind, so the routes can carry the
    -- `evidence` freeze B3 #16 requires without this migration guessing its shape.
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),

    digested_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    resolved_at timestamptz,

    PRIMARY KEY (tenant_id, id),
    -- The owner is an employee of the same tenant. A composite foreign key with
    -- a nullable column is satisfied by any row that leaves it NULL (MATCH
    -- SIMPLE), which is what "unclaimed" needs.
    FOREIGN KEY (tenant_id, owner_employee_id) REFERENCES employees(tenant_id, id),

    -- **The state and its timestamps travel together**, the shape `019` used for
    -- deactivation: a `claimed` task with no owner, or an owner with no claim
    -- time, are states no route should be able to leave behind.
    CONSTRAINT manual_tasks_claimed_pair CHECK (
        (status <> 'claimed') OR (owner_employee_id IS NOT NULL AND claimed_at IS NOT NULL)
    ),
    CONSTRAINT manual_tasks_resolved_pair CHECK (
        (status <> 'resolved') OR (resolved_at IS NOT NULL)
    )
);

ALTER TABLE manual_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE manual_tasks FORCE ROW LEVEL SECURITY;
DO $manual_task_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'manual_tasks'
           AND policyname = 'manual_tasks_tenant_isolation'
    ) THEN
        CREATE POLICY manual_tasks_tenant_isolation ON manual_tasks
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$manual_task_rls$;

-- The digest's query, moved: `(tenant_id, task_class, notify)` among rows not
-- yet digested. Same partial shape `014` used, on the table that now owns it.
CREATE INDEX IF NOT EXISTS manual_tasks_digest_idx
    ON manual_tasks (tenant_id, task_class, notify)
    WHERE digested_at IS NULL;

-- The backlog query a person will ask: this tenant's open tasks, oldest first.
CREATE INDEX IF NOT EXISTS manual_tasks_open_idx
    ON manual_tasks (tenant_id, status, created_at);

-- **The move.** `manual_task_id` and `id` are the same value on every row the
-- pipeline wrote (it passes the id twice), so the task keeps the identity the
-- notification and the digest already used.
INSERT INTO manual_tasks(
    tenant_id, id, conversation_id, intent_id, task_class, notify,
    reason_code, digested_at, created_at, updated_at
)
SELECT tenant_id,
       manual_task_id,
       conversation_id,
       id,
       manual_task_class,
       manual_task_notify,
       proposal->>'reason_code',
       digested_at,
       created_at,
       updated_at
  FROM intent_records
 WHERE kind = 'manual_task' AND manual_task_id IS NOT NULL
ON CONFLICT (tenant_id, id) DO NOTHING;

-- Now the old home goes, in dependency order: the index that reads the columns,
-- then the constraint that requires them, then the columns.
--
-- **The index is named `intent_records_pending_digest_idx`**, which is what
-- `014` called it. The first version of this line named
-- `intent_records_manual_task_digest_idx` -- a name that has never existed --
-- and `IF EXISTS` swallowed the miss, so the "dependency order" above began
-- with a statement that did nothing (independent review 2026-09-06, P3-1).
-- Dropping the columns takes the index with it either way; the line is kept
-- with the right name because the order is worth stating, and a statement that
-- silently matches nothing is worse than no statement.
DROP INDEX IF EXISTS intent_records_pending_digest_idx;
ALTER TABLE intent_records DROP CONSTRAINT IF EXISTS intent_records_manual_task_class_check;
ALTER TABLE intent_records
    DROP COLUMN IF EXISTS manual_task_class,
    DROP COLUMN IF EXISTS manual_task_notify,
    DROP COLUMN IF EXISTS digested_at;

COMMENT ON TABLE manual_tasks IS
    'Work a person has to do, with the owner/SLA/expiry conversation-engine.md §8 '
    'requires (T91). Filed by the engine''s manual_review hook or by a person '
    'through the control plane; the engine cannot fill owner/SLA/expiry and '
    'leaves them NULL.';

-- The worker files tasks and marks them digested; it does not claim, resolve or
-- reassign -- those are a person's actions and belong to the control plane role.
GRANT SELECT, INSERT, UPDATE ON manual_tasks TO fastagent_worker;

INSERT INTO schema_migrations(version) VALUES ('034_manual_tasks')
ON CONFLICT (version) DO NOTHING;
