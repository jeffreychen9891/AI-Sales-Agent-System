-- L2a. Which manual tasks a merchant is told about, recorded on the task.
--
-- Ruling (user, 2026-09-04): every manual task is either **需商家介入** or
-- **平台内部**, and only the first may ever reach the manager's WeCom. The field
-- is added now, before the scope is settled, exactly so that a later change of
-- scope is one edit to a table in `runtime/notification.py` -- not a change to
-- the mechanism, and not another migration.
--
-- Two columns rather than one because they answer different questions and are
-- changed by different people: *who is this for* is a product decision, *how
-- often do we say it* is an operational one. Collapsing them would make
-- "merchant-facing but deliberately not pushed yet" -- which is where four of
-- today's classified kinds sit -- indistinguishable from "platform internal",
-- and the difference between those two is the whole point of the ruling.
--
-- Nullable, and only meaningful for `kind = 'manual_task'`: every other intent
-- kind leaves them NULL, which the CHECK enforces rather than trusts.

ALTER TABLE intent_records
    ADD COLUMN IF NOT EXISTS manual_task_class text,
    ADD COLUMN IF NOT EXISTS manual_task_notify text;

ALTER TABLE intent_records
    DROP CONSTRAINT IF EXISTS intent_records_manual_task_class_check;

ALTER TABLE intent_records
    -- **The NOT NULLs are load-bearing.** Without them a manual_task row with
    -- both columns NULL evaluates to NULL, not FALSE, and a CHECK only rejects
    -- FALSE -- so the half this constraint exists for (every manual task is
    -- classified) was not enforced at all, while the half it merely documents
    -- (other kinds stay NULL) was. Measured against a throwaway postgres by the
    -- independent review, 2026-09-04, F4. There is no bad data today because
    -- `classify()` never returns None; the constraint exists for the producers
    -- nobody has written yet, and L2b is writing one.
    ADD CONSTRAINT intent_records_manual_task_class_check CHECK (
        (kind = 'manual_task'
             AND manual_task_class IS NOT NULL AND manual_task_notify IS NOT NULL
             AND manual_task_class IN ('merchant_action', 'platform_internal')
             AND manual_task_notify IN ('immediate', 'batched', 'none'))
        OR (kind <> 'manual_task'
             AND manual_task_class IS NULL AND manual_task_notify IS NULL)
    );

COMMENT ON COLUMN intent_records.manual_task_class IS
'Who the task is for. merchant_action tasks may be pushed to the tenant manager;
platform_internal tasks never are. Set from the table in runtime/notification.py,
so widening or narrowing the merchant-facing set is a code review, not a migration.';

COMMENT ON COLUMN intent_records.manual_task_notify IS
'How often the merchant hears about it: immediate, batched (an hourly digest,
produced only when the window is non-empty), or none. "none" on a merchant_action
task means "merchant-facing in principle, deliberately not pushed in this batch"
-- a different state from platform_internal, and one table edit away from being
pushed.';

INSERT INTO schema_migrations(version) VALUES ('013_manual_task_classification')
ON CONFLICT (version) DO NOTHING;
