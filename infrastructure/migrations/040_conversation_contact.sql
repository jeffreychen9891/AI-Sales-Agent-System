-- 040: which customer a conversation is with (route freeze B3 #23), and the
-- control plane's read of `contacts` (B3 #21-#23).
--
-- **Stored, not derived** (user ruling 2026-09-06). `GET /contacts/by-conversation/{id}`
-- can be answered two ways: keep a column, or work it out at read time from
-- `inbox_events` (`config_id` plus the envelope's `actor_id`, which is what
-- `039`'s writer keys on). The second needs no migration and costs a scan of
-- the event table on every read -- and `GET /conversations` (freeze B3 #1) would
-- pay it once per row in the list, which is precisely the read model T173 is
-- waiting on. One column answers both.
--
-- **The composite key, so the contact belongs to the same tenant.** Same shape
-- as `manual_tasks.owner_employee_id` (034): a nullable half satisfies MATCH
-- SIMPLE, so "we do not know who this is" stays representable.
ALTER TABLE conversation_aggregates
    ADD COLUMN IF NOT EXISTS contact_id uuid;

DO $conversation_contact_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'conversation_aggregates_contact_fkey'
           AND conrelid = 'conversation_aggregates'::regclass
    ) THEN
        ALTER TABLE conversation_aggregates
            ADD CONSTRAINT conversation_aggregates_contact_fkey
            FOREIGN KEY (tenant_id, contact_id) REFERENCES contacts(tenant_id, id);
    END IF;
END
$conversation_contact_fk$;

COMMENT ON COLUMN conversation_aggregates.contact_id IS
    'The customer this conversation is with (ADR-0048 K11, freeze B3 #23). '
    'Written by the worker when it creates the aggregate, from the same '
    '(config_id, external_user_id) identity ingress used to create the contact. '
    'NULL when the first message carried no identity, and for conversations '
    'that predate migration 039 -- there is no backfill.';

-- **NULL has two meanings here and the column cannot tell them apart**: a
-- conversation whose first message had no `actor_id`, and a conversation older
-- than `039`. Both are honestly "no contact recorded"; neither is "this
-- customer is anonymous". A read that needs the difference has to ask
-- `created_at`, and no read in M1a does.

-- **The worker writes this column**, so it needs the grant it did not have:
-- `001` gave it `SELECT, INSERT, UPDATE` on `conversation_aggregates` already
-- (it owns the aggregate), and `SELECT` on `contacts` to resolve the identity.
GRANT SELECT ON contacts TO fastagent_worker;

-- The control plane reads contacts for B3 #21-#23 and never writes them: the
-- only writer is ingress (`039`), which is what K11 rules.
GRANT SELECT ON contacts TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('040_conversation_contact')
ON CONFLICT (version) DO NOTHING;
