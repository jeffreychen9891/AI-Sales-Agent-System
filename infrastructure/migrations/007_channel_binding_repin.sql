-- 007: let the application role repin a channel binding, and nothing more.
--
-- 006 gave `profile_releases` a production write path. Nothing could make a
-- binding *use* one: `fastagent_app` held no privilege on `channel_bindings` at
-- all, so the only writer of `channel_bindings.profile_release_id` in the whole
-- repository was `tests/conftest.py`. That is why the M1a exit item "jewellery
-- Stage single target frame" stayed unreachable after T10 -- publishing was only
-- half of it (registered as T15).
--
-- RFC §9.1 makes an explicit repin four things at once: generation++, repair the
-- target stack, invalidate pending events, audit. This migration grants exactly
-- the privileges those four need and not one column more.
--
-- **Every grant here is column-scoped on purpose -- reads included.** A
-- table-level UPDATE on `channel_bindings` would let the repin path rewrite
-- `current_token_hash`, `next_token_hash` or `status`: that is channel
-- credential rotation and deactivation, two operations with their own routes,
-- their own audit reasons and their own blast radius. The application role must
-- not acquire them as a side effect of being allowed to point a binding at a
-- different release.
--
-- The same applies to SELECT, which the first version of this migration granted
-- at table level (independent review 2026-09-03, P1-1). That handed
-- `current_token_hash` to the API role -- exactly what 001 avoided by giving
-- ingress EXECUTE on the `resolve_channel_binding` SECURITY DEFINER function
-- instead of a table read. The repin path reads three columns; it now has three.
--
--   * `conversation_aggregates`: the repin moves the pin, the generation, the
--     state version, the stack and (when a goal disappeared) the control mode.
--     **`state` is included, and has to be**: `state` *is* the aggregate the
--     engine reads (`pipeline.py`: `aggregate = aggregate_row["state"]`), and
--     the scalar columns are its projection. The first version withheld `state`
--     on the reasoning that it meant "accumulated facts the app must not edit",
--     which had the relationship backwards -- and the result was a repin the
--     worker could not see, a document permanently disagreeing with its own
--     projection, and every later event on that conversation failing its CAS
--     forever (independent review, P0-1). What stays withheld is `lifecycle`:
--     closing a conversation is not part of changing which profile governs it.
--   * `scheduled_events`: the repin invalidates. It cannot create a timer, and
--     it cannot move `due_at`, `payload_ref` or the lease columns.
--
-- Row-level security is unchanged and needs no addition: 001 already enables and
-- FORCEs `tenant_id = current_setting('app.tenant_id')` on all three tables for
-- every role, so a column grant cannot reach another tenant's row.
--
-- Not in this migration, deliberately: no new table, no new column, no trigger,
-- no policy change. If a later batch needs `config_id` on
-- `conversation_aggregates` to scope a repin to one binding rather than to one
-- release (registered as T24), that is a schema change and belongs in its own
-- migration with its own review.

GRANT SELECT (tenant_id, config_id, profile_release_id) ON channel_bindings TO fastagent_app;
GRANT UPDATE (profile_release_id) ON channel_bindings TO fastagent_app;

GRANT UPDATE (
    profile_release_id, generation, state_version, target_stack, control_mode, state, updated_at
) ON conversation_aggregates TO fastagent_app;

GRANT SELECT (tenant_id, conversation_id, status) ON scheduled_events TO fastagent_app;
GRANT UPDATE (
    status, invalidated_at, invalidation_reason
) ON scheduled_events TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('007_channel_binding_repin')
ON CONFLICT DO NOTHING;
