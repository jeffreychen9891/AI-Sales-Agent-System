-- L2b. Which manual tasks an hourly digest has already told the merchant about.
--
-- The batched kinds (delivery unconfirmed, delivery finally failed) are summed
-- into one message an hour rather than one message each -- a merchant with
-- forty stuck deliveries needs one sentence, not forty notifications. That
-- turns "which of these have been reported" into a fact somebody has to keep,
-- and keeping it on the task is what makes the job restart-safe: no window
-- table, no in-memory cursor, no double counting after a deploy.
--
-- **Why not a timestamp on a digest table.** A digest that recorded "I covered
-- 14:00 to 15:00" would have to be trusted about what happened inside that
-- window; a task that records "I was included" cannot be wrong about itself.
-- The difference shows up exactly when it matters: a task created while the
-- digest is running, and a job that crashed between sending and recording.

ALTER TABLE intent_records
    ADD COLUMN IF NOT EXISTS digested_at timestamptz;

COMMENT ON COLUMN intent_records.digested_at IS
'When an hourly manager digest first included this manual task. NULL means the
merchant has not been told yet -- which is what the next digest counts. Only ever
set on kind = manual_task rows whose manual_task_notify is batched; immediate and
none never enter a digest.';

CREATE INDEX IF NOT EXISTS intent_records_pending_digest_idx
    ON intent_records (tenant_id, manual_task_notify)
    WHERE kind = 'manual_task' AND digested_at IS NULL;

INSERT INTO schema_migrations(version) VALUES ('014_manual_task_digest')
ON CONFLICT (version) DO NOTHING;
