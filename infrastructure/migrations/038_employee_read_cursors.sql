-- 038: where each person has read up to (route freeze B3 #5, `C0B-006`).
--
-- **The shape is ADR-0048 K12's, word for word**: 「已读游标新建
-- `employee_read_cursors`：`(employee_id, conversation_id, last_read_at,
-- last_read_ref)`」. That ruling is mostly about the *message* read model --
-- a merged projection over `inbox_events` and `outbox_messages`, no third
-- table, because both are append-only facts and double writing is a new
-- consistency problem. The read cursor is the one thing it says **does** need
-- a table of its own, and this is it: where a person has read up to is not a
-- fact about the conversation, and no projection can compute it.
--
-- `tenant_id` joins K12's four columns because every table here is
-- tenant-scoped and the classification gate (B1 #8) requires a table to answer
-- the isolation question one way or the other.

CREATE TABLE IF NOT EXISTS employee_read_cursors (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    employee_id uuid NOT NULL,
    conversation_id uuid NOT NULL,

    -- **K12's three-part position, stored as three columns.** ADR-0048 §二.9
    -- fixes the stable key as `(created_at, source_table, id)`, and this is a
    -- position in exactly that ordering: `last_read_at` is what the unread count
    -- compares against, `last_read_source` says which table the row came from,
    -- and `last_read_ref` is the row. The first version of this table carried
    -- two of the three and cited K12 for the rest of its shape (independent
    -- review 2026-09-06, P2-2).
    last_read_at timestamptz NOT NULL,
    last_read_source text NOT NULL CHECK (
        last_read_source IN ('inbox_events', 'outbox_messages')
    ),
    -- **`uuid`, and still not a foreign key** (independent review P3-1). No
    -- constraint can express "a row in one of two tables", which is why there
    -- is no reference -- but that argument was about the *foreign key*, and the
    -- first version took it as an argument for `text` as well. It is not: with
    -- a `text` column the monotonic comparison below sorted uuids as strings
    -- while every other comparison in this feature sorted them as uuids. The
    -- two agree today only because the writer always produces the lowercase
    -- canonical form; `uuid` makes them agree by construction.
    last_read_ref uuid NOT NULL,

    updated_at timestamptz NOT NULL DEFAULT now(),

    -- One cursor per person per conversation. The primary key **is** the
    -- idempotency the freeze asks for (「按 employee read cursor 幂等」): a
    -- second call for the same message finds the row and moves nothing.
    PRIMARY KEY (tenant_id, employee_id, conversation_id),
    FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id)
);

ALTER TABLE employee_read_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE employee_read_cursors FORCE ROW LEVEL SECURITY;
DO $read_cursor_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'employee_read_cursors'
           AND policyname = 'employee_read_cursors_tenant_isolation'
    ) THEN
        CREATE POLICY employee_read_cursors_tenant_isolation ON employee_read_cursors
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$read_cursor_rls$;

COMMENT ON TABLE employee_read_cursors IS
    'Where each employee has read up to in each conversation (ADR-0048 K12, '
    'freeze B3 #5). Written only by POST /conversations/{id}/read, and only '
    'ever for the caller themselves.';

-- **The control plane only, and no `DELETE`.** The worker has no business
-- knowing what a person has read -- it never reads this table, so it is not
-- granted `SELECT` either (the narrower grant is the one that stays true).
-- Nothing removes a cursor: a person who read a conversation once did, and the
-- row is a few bytes against a fact that does not stop being true.
GRANT SELECT, INSERT, UPDATE ON employee_read_cursors TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('038_employee_read_cursors')
ON CONFLICT (version) DO NOTHING;
