-- 028: the rotation window on `channel_bindings` becomes a schema guarantee.
--
-- **Source: `D18`'s standing debt (B2 #1 independent review, Q3).** Migration
-- 001 gave `channel_bindings` three nullable rotation columns and bound none of
-- them to each other, so "this binding is mid-rotation" has been a state kept
-- correct by hand. `D18` records that the constraint must land with B2 #4 and
-- 「不得再往后挪」; the route was held pending a ruling, so the constraint is
-- split out rather than deferred again.
--
-- **What the columns actually mean**, read off the two consumers rather than
-- off the names:
--
--   * `services/ingress/src/fastagent_v2/ingress/adapter.py:62` -- the *current*
--     token is accepted from `current_valid_from` until `overlap_ends_at`, and
--     `NULL` there means "no end". So `overlap_ends_at` bounds the **outgoing**
--     token, not the incoming one.
--   * `adapter.py:67` -- the *next* token is accepted once **both**
--     `next_token_hash` and `next_valid_from` are present and `now` has reached
--     the latter. Either column alone authenticates nothing.
--
-- Three invariants follow, and each one is a state the ingress path would
-- mis-serve today.

ALTER TABLE channel_bindings
    -- **A half-written rotation is not a rotation.** `next_token_hash` without
    -- `next_valid_from` never authenticates (`adapter.py:67` requires both), and
    -- `next_valid_from` without a hash is a promise with nothing behind it.
    --
    -- This one has a second beneficiary. `POST /channel-bindings/{id}/deactivate`
    -- records whether it destroyed a scheduled rotation, and reads
    -- `next_valid_from IS NOT NULL` as a **proxy** because migration 023
    -- withholds `SELECT` on both hashes; `app.py:1760` names the state that
    -- proxy misses -- "hash present, no valid_from". After this constraint the
    -- proxy is not a proxy any more: the two columns cannot disagree.
    ADD CONSTRAINT channel_bindings_rotation_is_all_or_nothing
        CHECK ((next_token_hash IS NULL) = (next_valid_from IS NULL)),

    -- **An end date for the outgoing token, with nothing incoming, is a channel
    -- switching itself off.** That is what `status = 'disabled'` is for, and it
    -- is reversible and audited; this is neither. Without the constraint the
    -- row reads as a healthy binding right up until the moment ingress starts
    -- refusing every request.
    ADD CONSTRAINT channel_bindings_overlap_requires_a_successor
        CHECK (overlap_ends_at IS NULL OR next_token_hash IS NOT NULL),

    -- **The overlap has to overlap.** If the outgoing token's end precedes the
    -- incoming token's start, neither is accepted in between: `current_allowed`
    -- has gone false and `now >= next_valid_from` is not yet true. The window
    -- between them is a dead channel, and nothing in the system reports it --
    -- the binding still says `active`. The `>=` is deliberate: an instantaneous
    -- handover is a legitimate, if brave, choice; a gap is not.
    ADD CONSTRAINT channel_bindings_rotation_window_is_ordered
        CHECK (
            (next_valid_from IS NULL OR next_valid_from >= current_valid_from)
            AND (
                overlap_ends_at IS NULL
                OR next_valid_from IS NULL
                OR overlap_ends_at >= next_valid_from
            )
        );

-- **No `NOT VALID`.** These are added as validating constraints on purpose: no
-- rotation route exists yet (that is what `D18` is about), so any row that
-- violates one was written by hand and is exactly the case worth stopping the
-- deployment for. A `NOT VALID` constraint would let the bad row survive the
-- migration and surface later as an ingress outage with no cause attached.

-- **Without this the migration runs again on the next deploy.** `migrate.py`
-- skips a file whose stem is in `schema_migrations`, and nothing in that loop
-- writes the row -- each migration records itself. This one did not, and
-- `ALTER TABLE ... ADD CONSTRAINT` has no `IF NOT EXISTS`, so the second deploy
-- would have failed on `duplicate_object`.
--
-- **The gate could not have caught it**: `ci.sh` builds a fresh database every
-- run, so every migration runs exactly once there and a missing version row is
-- invisible. It surfaced in the local loop, which reuses its database -- the
-- handoff's §3.2c, "the gate's own environment hides some defects", with a
-- second instance. `tests/contracts/test_migration_versions.py` now asserts the
-- line exists, because the next author will not think of it either.
INSERT INTO schema_migrations(version) VALUES ('028_channel_rotation_invariants')
ON CONFLICT (version) DO NOTHING;
