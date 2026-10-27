-- 008: record which channel binding a conversation arrived through.
--
-- Nothing anywhere held this link. `conversation_aggregates` recorded which
-- *release* a conversation pinned, so `repin_channel_binding` could only select
-- by release: one tenant with two bindings on the same release moved both sets,
-- and after T15's P2-1 fix the selection widened further to "everything not
-- already on the target release" (registered as T24). The over-capture was never
-- a rule anyone chose -- it was the only query the schema could answer.
--
-- The fact is available at ingress: `WeComProxyIngress.accept` authenticates a
-- `config_id` before it writes anything. It is carried on `inbox_events` so the
-- worker can copy it onto the aggregate it creates from that event, which is the
-- only moment a conversation's binding is known for certain.
--
-- **Both columns are nullable, and that is not laziness.**
--   * `inbox_events`: the scheduler also inserts here, for timers. A timer has a
--     conversation but no binding of its own -- the conversation already carries
--     one -- so those rows leave it NULL rather than guessing. (A timer can only
--     exist for an aggregate that already exists, so this is the only NULL
--     producer here in practice; it is not a route by which an aggregate gets
--     created without a binding.)
--   * `conversation_aggregates`: rows created before this migration carry no
--     binding. They are *not* silently swept into a repin;
--     `repin_channel_binding` counts them and reports them as
--     `unlinked_conversations` so an operator sees that something was skipped
--     rather than believing the repin finished.
--
-- **Those pre-008 rows are recoverable, and an earlier draft of this file said
-- they were not.** Since 001, `ingress_replay_keys` has recorded
-- `(tenant_id, config_id, external_event_id)` for every inbound event, and
-- `inbox_events` records `(tenant_id, external_event_id, conversation_id)` --
-- both written in the same ingress transaction (`ingress/adapter.py`), so the
-- two join per conversation. No job prunes either table. This migration does not
-- run the backfill because there is nothing to back-fill: no environment holds
-- pre-008 conversations today (Stage has no real ones; local and CI rebuild
-- every run), and a backfill written against zero rows is a statement nobody has
-- ever seen work. It is written down instead, so that an operator who ever does
-- see `unlinked_conversations > 0` knows the link is derivable rather than lost:
--
--     UPDATE conversation_aggregates a
--        SET config_id = link.config_id
--       FROM (SELECT DISTINCT ON (e.tenant_id, e.conversation_id)
--                    e.tenant_id, e.conversation_id, r.config_id
--               FROM inbox_events e
--               JOIN ingress_replay_keys r
--                 ON r.tenant_id = e.tenant_id
--                AND r.external_event_id = e.external_event_id
--              ORDER BY e.tenant_id, e.conversation_id, e.created_at) AS link
--      WHERE a.tenant_id = link.tenant_id
--        AND a.id = link.conversation_id
--        AND a.config_id IS NULL;
--
-- Rows whose join is empty or ambiguous stay NULL -- the same fail-closed side
-- the nullable column was chosen for. Registered as T30 (operator runbook entry)
-- rather than left in this comment alone.
--
-- The foreign key is on `conversation_aggregates` only. That is the column a
-- repin selects on, so it is the one that must not be able to name a binding
-- that does not exist. `inbox_events.config_id` is a carrier: ingress has
-- already resolved and authenticated the binding through
-- `resolve_channel_binding` before it writes the row, and adding a second
-- constraint on the hot inbound path buys nothing that check does not.
--
-- No new grants. `fastagent_ingress` already holds table-level INSERT on
-- `inbox_events`; `fastagent_worker` already holds INSERT/UPDATE on
-- `conversation_aggregates`; `fastagent_app` only *reads* this column and has
-- held table-level SELECT on that table since 001. The column-scoped UPDATE
-- grants added by 007 are deliberately not extended -- a repin reads the link,
-- it does not rewrite it.

-- The aggregate contract declares `config_id` at `maxLength: 128` and the
-- database bounded it nowhere, so a longer binding could be created, authenticate
-- through ingress, and then fail `validate_contract` in the worker on every
-- attempt -- a conversation wedged by a limit only one side believed in. Enforce
-- it where a `config_id` enters the system instead. (Independent review P3-3.)
DO $config_id_length$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'channel_bindings_config_id_length'
    ) THEN
        ALTER TABLE channel_bindings
            ADD CONSTRAINT channel_bindings_config_id_length
            CHECK (char_length(config_id) <= 128);
    END IF;
END
$config_id_length$;

ALTER TABLE inbox_events ADD COLUMN IF NOT EXISTS config_id text;

ALTER TABLE conversation_aggregates ADD COLUMN IF NOT EXISTS config_id text;

DO $link$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'conversation_aggregates_config_id_fkey'
    ) THEN
        ALTER TABLE conversation_aggregates
            ADD CONSTRAINT conversation_aggregates_config_id_fkey
            FOREIGN KEY (tenant_id, config_id)
            REFERENCES channel_bindings(tenant_id, config_id);
    END IF;
END
$link$;

-- Repin selects on (tenant_id, config_id) and filters by release; the partial
-- index skips the pre-008 rows, which that query never returns anyway.
CREATE INDEX IF NOT EXISTS conversation_aggregates_binding_idx
    ON conversation_aggregates (tenant_id, config_id)
    WHERE config_id IS NOT NULL;

INSERT INTO schema_migrations(version) VALUES ('008_conversation_binding_link')
ON CONFLICT DO NOTHING;
