-- L2b review P2-2. The digest's interval probe had no index behind it.
--
-- `cli.py` calls `ManagerDigest.run_for` once per active tenant on **every**
-- pass of the worker loop, and the first thing `run_for` does after taking its
-- lock is ask "when did I last write a digest for this tenant":
--
--     SELECT max(created_at) FROM outbox_messages
--      WHERE tenant_id = %s AND command->>'audience' = 'tenant_manager'
--        AND command->>'notice_kind' = 'digest'
--
-- Nothing covered those two JSON keys, so that was a scan of the tenant's whole
-- outbox history -- which is append-only and grows for ever -- several times a
-- second. The comment in `cli.py` said "costs one indexed query"; the reviewer
-- checked and it did not. **A comment claiming a property the schema does not
-- provide is the same failure this repository keeps finding**, and the cheaper
-- half of the fix is to make the claim true.
--
-- Partial, and the predicate is written to match the query character for
-- character: a partial index whose WHERE clause is *nearly* the query's is an
-- index the planner will not use, which is the same as not having one while
-- looking like having one.
--
-- Numbered 016: 014 is L2b's own digest migration, 015 belongs to
-- `feat/cp3-role-permission-routes-20260904` (written, gated, waiting on CP-2's
-- window). Reusing 015 would be a collision that survives a clean merge and
-- fails once, in whichever environment applies them in the other order.

CREATE INDEX IF NOT EXISTS outbox_manager_digest_by_tenant
    ON outbox_messages (tenant_id, created_at DESC)
 WHERE command->>'audience' = 'tenant_manager'
   AND command->>'notice_kind' = 'digest';

COMMENT ON INDEX outbox_manager_digest_by_tenant IS
'Backs the hourly digest''s interval probe, which runs once per active tenant per
worker pass. Partial on the same two JSON keys the probe filters on -- a
near-miss predicate would leave the planner scanning while the schema looked
covered.';

INSERT INTO schema_migrations(version) VALUES ('016_manager_digest_interval_index')
ON CONFLICT (version) DO NOTHING;
