-- B2-2 / T123: let the dispatcher read the guid, and nothing else.
--
-- `017` added `channel_bindings.provider_instance_id` and `025` gave
-- `fastagent_app` `UPDATE` on it, so the value can be written. **Nobody could
-- read it** -- that is the half of T123 still open, and its expiry was written
-- as "出站接线批次……**必须在同一次里给出授权，且按 `007` 的列级口径给，不得顺手
-- 给表级**". This is that batch, and this is that grant.
--
-- ## Why these four columns and not the table
--
-- ADR-0094 §二 ruled that the dispatcher resolves the binding itself, rather
-- than the outbox row carrying the guid along. That decision is what makes a
-- read grant necessary at all, and it is also what bounds it: the dispatcher
-- needs to answer one question -- "which provider instance sends for this
-- tenant's config, and may it send at all" -- so it gets exactly the columns
-- that question needs.
--
--   * `tenant_id`, `config_id` -- the lookup key. A predicate needs `SELECT` on
--     the columns it filters, so these are not decoration.
--   * `provider_instance_id` -- the guid. The point of the batch.
--   * `status` -- because `001` declares `active | disabled`, and a dispatcher
--     that cannot see it would happily send through a binding somebody
--     deliberately disabled. Reading it is cheaper than the incident.
--
-- **Deliberately not granted**: `current_token_hash` and `next_token_hash` --
-- those are the channel credentials, and the dispatcher has no business
-- reading them; `profile_release_id`, the rotation timestamps and `source` --
-- the dispatcher does not decide any of those. A table-level grant would have
-- handed over all seven, which is why `007` set the column-level precedent and
-- why T123 wrote it into the expiry condition rather than leaving it to taste.
--
-- ## What this does not do
--
-- **No write of any kind.** `PATCH /channel-bindings/{config_id}` stays the
-- only writer of `provider_instance_id` (`025`), and the dispatcher is a
-- reader. Nothing here touches RLS: `channel_bindings` keeps the policies
-- `001` gave it, so a worker still only ever sees its own tenant's rows.
GRANT SELECT (tenant_id, config_id, provider_instance_id, status)
    ON channel_bindings TO fastagent_worker;

-- **`017`'s comment on this column stops being true at this line, so it is
-- rewritten here** (independent review 2026-09-08, P2-2). It ended with "NOT
-- READABLE BY ANY RUNTIME ROLE YET … the grant is part of the wiring batch
-- (T123)", and this is that batch. That sentence is not decoration: **T123,
-- T135 and T158 all cite this column comment as their authority**, and T158's
-- open question is worded around it. Leaving it would make the database's own
-- documentation contradict its catalog.
--
-- **The distinction the new text has to keep** is the one that decides whether
-- T135 and T158 have come due: the grant goes to `fastagent_worker` only.
-- `fastagent_app` -- which serves `PATCH /channel-bindings/{config_id}` and
-- `GET /channel-bindings/instances` -- gained nothing, so those two registry
-- items' triggers have *not* fired.
COMMENT ON COLUMN channel_bindings.provider_instance_id IS
'The WeComProxy guid this binding sends through. Nullable on purpose: a guid is
produced by create_client and then bound by a human scanning a QR code, so a
binding row exists before a guid does. Written by fastagent_app through
PATCH /channel-bindings/{config_id} (025); seeded by an operator until then.
READABLE BY fastagent_worker SINCE 046 (T123): the dispatcher resolves its own
binding at dispatch time (ADR-0094 二). Still NOT readable by fastagent_app --
that role has no SELECT on this column, which is what keeps T135''s question
(should the route echo the value it just wrote?) open and untriggered.';

INSERT INTO schema_migrations(version) VALUES ('046_worker_reads_binding_guid')
ON CONFLICT (version) DO NOTHING;
