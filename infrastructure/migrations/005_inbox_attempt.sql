-- 005: give inbox retries an end.
--
-- `inbox_events` carried a lease but no attempt counter. The lease decides when
-- the next identical attempt happens, not how many there may be, so an event the
-- worker could not process was republished every ten seconds indefinitely -- and
-- with a model provider configured, each pass paid for three model calls. The
-- `failed` terminal state already existed in the status CHECK; what was missing
-- was the counter that lets the worker reach it.
--
-- No down script: this repository has no downgrade runner, and the recovery
-- contract for Stage is restore-from-dump (ADR-0007/0008), not a reverse
-- migration. Dropping the column would silently uncap the retry loop again.

ALTER TABLE inbox_events
    ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0);

-- Added 2026-09-06 with the gate that asserts it. This file had been re-running
-- on every deploy since it landed; it survived only because `ADD COLUMN IF NOT
-- EXISTS` is idempotent, which is luck rather than design.
INSERT INTO schema_migrations(version) VALUES ('005_inbox_attempt')
ON CONFLICT (version) DO NOTHING;
