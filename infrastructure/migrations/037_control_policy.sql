-- 037: control policy, as two columns on `tenants` (route freeze B3 #10/#11).
--
-- **Two columns, not a table and not a profile field.** ADR-0048 K13 ruled the
-- carrier: it is an *operational switch*, not business semantics -- and a
-- profile is an immutable release, so it cannot hold something an operator
-- changes on a Tuesday afternoon. `D12` went from "a table is missing" to "two
-- columns are missing" on the strength of that ruling, and this is those two.
--
-- **`default_control_mode` (`C0B-012`).** The legacy route was
-- `GET /conversations/default-status`, and the note that survives into the
-- freeze is 「不把 UI 默认当 aggregate 状态」: whether new conversations start
-- with the AI answering is a tenant's standing decision, not a fact about any
-- one conversation. `'ai'` is the default because that is what
-- `_initial_aggregate` has hard-coded since M0 -- the column starts by
-- describing today rather than changing it.
--
-- **`understanding_during_takeover` (ADR-0054 §三.1).** `pipeline.py`'s
-- human-control branch says in its own docstring that whether the understanding
-- step keeps running is a policy 「whose carrier (`control-policy`, freeze B3
-- #10/#11) does not exist yet」, that the user ruled the default is **keep
-- running** (2026-09-05), and that the switch arrives with the carrier. This is
-- the carrier arriving, so `true` is the default for the reason above: it is
-- what the engine does today.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS default_control_mode text NOT NULL DEFAULT 'ai',
    ADD COLUMN IF NOT EXISTS understanding_during_takeover boolean NOT NULL DEFAULT true;

-- The same two values `conversation_aggregates.control_mode` accepts, and for
-- the same reason: a default the engine cannot start a conversation in would be
-- a switch that breaks the next inbound message rather than one that does
-- nothing.
DO $control_policy_check$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'tenants_default_control_mode_check'
           AND conrelid = 'tenants'::regclass
    ) THEN
        ALTER TABLE tenants ADD CONSTRAINT tenants_default_control_mode_check
            CHECK (default_control_mode IN ('ai', 'human'));
    END IF;
END
$control_policy_check$;

COMMENT ON COLUMN tenants.default_control_mode IS
    'Control mode new conversations start in (C0B-012, ADR-0048 K13). Read by '
    'the worker when it creates an aggregate; written by PUT /control-policy.';
COMMENT ON COLUMN tenants.understanding_during_takeover IS
    'Whether the understanding step keeps running while a person holds the '
    'conversation (ADR-0054 §三.1). Default true = today''s behaviour.';

-- **Two named columns, the shape `032` used.** `fastagent_app` already holds
-- `UPDATE (status, disabled_at, purge_eligible_at)` from that migration; this
-- adds the two this policy owns and nothing else.
--
-- **What this grant does and does not hold** (independent review 2026-09-06,
-- P3-3, which measured it). It holds `code` and every other unlisted column:
-- `fastagent_app` writing `tenants.code` is refused by the database. It does
-- **not** hold `status` -- `032` granted that, and a review probe confirmed the
-- write succeeds. So "a route that sets an operational switch cannot disable
-- the tenant" is true for two other reasons: this route's own SQL writes
-- exactly two columns, and disabling one is `tenant.update`, which ADR-0075
-- makes platform-only. The first version of this comment credited the grant,
-- which would have left the next reader believing in a layer of defence that
-- is not there.
--
-- **Migration numbering**: `034`-`036` are held by the three unmerged K18
-- manual-task branches, so this batch took `037` to avoid a collision. The two
-- sets touch different tables and neither depends on the other, but if K18
-- merges second the applied order will not match the filename order.
GRANT UPDATE (default_control_mode, understanding_during_takeover)
    ON tenants TO fastagent_app;

-- The worker reads both: one when it creates a conversation, one on every event
-- that arrives while a person is holding it. `001` already grants it `SELECT`
-- on the whole table, so there is nothing to add -- noted because the reader is
-- what makes these columns something other than decoration (T175's shape).

INSERT INTO schema_migrations(version) VALUES ('037_control_policy')
ON CONFLICT (version) DO NOTHING;
