-- 053: the material catalogue -- versioned identities for the files E3 sends.
--
-- **Migration number**: `052` was pre-allocated to this batch by
-- `docs/plans/parallel-safety-2026-09-12.md` §四 Y4, and taken instead by the
-- knowledge-base author (`052_knowledge_publish.sql`). This batch yielded the
-- number rather than making that branch re-gate: its migration was already
-- written, this one had not started. See `docs/handoffs/merge-queue.md`.
--
-- **What this table is, and what it deliberately is not.**
--
-- It is a catalogue of *versioned material identities*: which file, which
-- revision, what it is called, how big it is, and whether it may still be
-- offered. **It does not carry the bytes.** That is not an oversight and not a
-- "phase two" hand-wave -- it is the only honest shape available today:
--
--   * `裁定一` (ADR-0130 E1) put the upload path out of the first version:
--     materials are placed by hand, so there is no route that receives bytes;
--   * `裁定二` (ADR-0130 E2) declined object storage and virus scanning,
--     because with no byte intake there is nothing to scan;
--   * **where the bytes must physically live depends on a contract this
--     repository does not have**: `/cloud/cdnBigUploadByUrl` is named exactly
--     once in `docs/wecom-proxy-message-api.md:421`, with no request body, no
--     response body and no error codes. If its semantics are "we hand you a
--     URL and you fetch it", the bytes need a publicly reachable address; if it
--     accepts a body, they do not. **Choosing a carrier before reading that
--     document would be inventing the answer**, and `AGENTS.md`'s second
--     zero-caller question forbids shipping a column whose value cannot yet
--     come from anywhere.
--
-- `content_hash` identifies a version **regardless of where its bytes end up**,
-- so adding the carrier later is an `ALTER TABLE`, not a redesign. The debt is
-- registered (see the T entry named in the batch's self-review) and comes due
-- with E3-4, the batch that reads that vendor document.
--
-- **There is no `bytea` anywhere in this repository today**; this migration
-- does not introduce the first one by accident.

CREATE TABLE IF NOT EXISTS materials (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,

    -- **The join key to the profile side.** A frame declares
    -- `asset:<slug>:v<N>` in `content_assets` (E3-2b); the send step resolves
    -- that slug to a row here. The grammar lives in exactly one place --
    -- `packages/runtime/src/fastagent_v2/runtime/references.py` -- and this
    -- constraint is **deliberately not a second copy of it**: it bounds the
    -- length so a row cannot be unbounded, and leaves the shape to the
    -- validator. A regex here would be a second definition point, and the two
    -- would drift on the day nobody is watching (the same reasoning
    -- `050_knowledge_base.sql` gives for `knowledge_chunks.slug`).
    slug text NOT NULL CHECK (length(slug) BETWEEN 1 AND 128),

    -- **A slug is never reused, and a version is never edited.**
    -- `references.py` §slug writes the first half down: "一个 slug 用过之后,
    -- 即使那条内容被删也不给别的内容", because a reply that quoted it stays in
    -- the conversation record and re-pointing it is silent. The second half is
    -- enforced below by `materials_immutable`.
    version integer NOT NULL CHECK (version > 0),

    -- What the customer sees first. `docs/wecom-proxy-message-api.md` §7 makes
    -- `filename` a required field of `/msg/sendFile`, so this is not metadata
    -- for our own benefit -- it is part of the product surface.
    filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),

    -- IANA media type. **No CHECK against a value list.** The allowed set is an
    -- editorial decision that belongs to the placement spec (E3-2.5) and will
    -- grow; freezing it in a migration would mean a migration per file type --
    -- the same call `050` made for `knowledge_chunks`'s editorial vocabularies.
    media_type text NOT NULL CHECK (length(media_type) BETWEEN 1 AND 128),

    -- Size of the identified file in bytes. **This is a fact about the file,
    -- not a policy.** The product cap belongs to the placement spec, and it
    -- cannot be set here yet: `/msg/sendFile` has a "large file" branch that
    -- needs `fileConvertId`, and **the threshold is not in the vendor document
    -- we hold**. Sizing that bound by guessing is the mistake `cost.py` already
    -- records paying for once ("Sizing a bound on a typical value instead of
    -- the provable one is the mistake").
    byte_size bigint NOT NULL CHECK (byte_size > 0),

    -- sha256 of the file this row identifies, lowercase hex. Same spelling as
    -- `knowledge_releases.content_hash`, on purpose.
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    -- `active` may be offered; `archived` may not. **Archiving is not
    -- deletion** -- freeze B4 #6 says "删除/归档，审计不删", and a reply that
    -- already sent this version must stay explicable afterwards.
    status text NOT NULL CHECK (status IN ('active', 'archived')),
    archived_at timestamptz,
    CHECK ((status = 'archived') = (archived_at IS NOT NULL)),

    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),

    -- One row per (slug, version) per tenant. This is what makes
    -- `asset:price-list:v1` resolve to exactly one thing.
    UNIQUE (tenant_id, slug, version)
);

-- **Deliberately absent: `created_by`.**
-- There is no route that writes this table in the first version (materials are
-- placed by hand), so an actor column would be `NULL` on every row that exists
-- -- `AGENTS.md`'s second zero-caller question names exactly this shape, and
-- `T118`'s always-`false` `legal_hold` is the registered precedent. The column
-- arrives with the write path that can fill it.

-- **No separate slug index.** `UNIQUE (tenant_id, slug, version)` already builds
-- one, btree scans it in either direction, and `DESC` only buys anything when
-- columns are mixed-direction -- which these are not. The first draft carried
-- `materials_tenant_slug_idx ON (tenant_id, slug, version DESC)`; independent
-- review 2026-09-12 P2-1 showed it was a duplicate of the unique index with no
-- added capability, and that "latest version" is not even the main lookup:
-- `asset:<slug>:v<N>` names the version (`references.py`).

-- **A version is immutable; the lifecycle column is the one exception.**
--
-- Shape copied from `006_profile_registry.sql`'s `profile_releases_immutable`
-- rather than invented: a `BEFORE UPDATE` trigger that refuses, so the rule is
-- in the database and not in the hope that every writer remembers it.
--
-- **The exception is narrow and it is the whole reason this is a trigger and
-- not a plain "no UPDATE" rule**: archiving must be able to move `status`
-- `active -> archived` and stamp `archived_at`. Everything else -- slug,
-- version, filename, media type, size, hash, and un-archiving -- is refused.
CREATE OR REPLACE FUNCTION materials_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $materials_reject_mutation$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.slug IS DISTINCT FROM OLD.slug
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.filename IS DISTINCT FROM OLD.filename
       OR NEW.media_type IS DISTINCT FROM OLD.media_type
       OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'materials rows are immutable; publish a new version instead'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- **Once stamped, the archive time is a fact and not a field.**
    -- Independent review 2026-09-12 P2-2: the comment above claimed "everything
    -- else is refused", and `archived_at` was not in that list -- an archived
    -- row could have its timestamp rewritten, and that timestamp *is* the
    -- answer to "from which moment may this version no longer be sent".
    -- `CHECK` only asks that it be non-null; the branch below only forbids
    -- un-archiving.
    IF OLD.archived_at IS NOT NULL
       AND NEW.archived_at IS DISTINCT FROM OLD.archived_at
    THEN
        RAISE EXCEPTION
            'materials.archived_at is set once and never rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- One-way. Un-archiving would make "this version may no longer be offered"
    -- a statement with no fixed truth value, and replies already sent under the
    -- archived version could not be explained by reading the row.
    IF OLD.status = 'archived' AND NEW.status <> 'archived' THEN
        RAISE EXCEPTION
            'materials rows cannot be un-archived'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END
$materials_reject_mutation$;

DROP TRIGGER IF EXISTS materials_immutable ON materials;
CREATE TRIGGER materials_immutable
    BEFORE UPDATE ON materials
    FOR EACH ROW
    EXECUTE FUNCTION materials_reject_mutation();

-- **No delete-refusing trigger, and that is a correction the gate forced.**
--
-- The first draft of this migration carried one ("materials rows are never
-- deleted; archive instead"). It was wrong, and
-- `test_every_tenant_owned_table_is_purged_or_named_as_an_exclusion` caught it:
-- `purge_tenant_data` issues `DELETE FROM materials WHERE tenant_id = $1`, and
-- a trigger that refuses would make a tenant erasure fail.
--
-- **The exclusion route was available and is wrong here.** `profile_releases`
-- sits in `DOCUMENTED_EXCLUSIONS` for exactly this reason -- 006's trigger
-- raises on DELETE -- but its justification is that surviving audit rows record
-- decisions made *under a release*. `050` already ruled that this argument does
-- **not** transfer to tables nothing reads yet, and declined the exclusion for
-- all three knowledge tables. **Materials are in the same position**, and
-- keeping a tenant's files after they asked to be erased is the opposite of
-- what the purge is for.
--
-- **"Archive, never delete" is a rule about the product, not about the
-- database.** It is enforced where deletes could originate: E3-6 ships
-- `archive` and no delete route. A whole-tenant erasure is not that path.

ALTER TABLE materials ENABLE ROW LEVEL SECURITY;
ALTER TABLE materials FORCE ROW LEVEL SECURITY;
DO $materials_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'materials'
           AND policyname = 'materials_tenant_isolation'
    ) THEN
        CREATE POLICY materials_tenant_isolation ON materials
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$materials_rls$;

-- **No GRANT.** Same call `050` made and for the same reason: nothing reads or
-- writes this table yet. The routes arrive in E3-6, the send path in E3-4, and
-- each grants what it needs. A grant issued now would be a permission with no
-- holder, which is harder to notice than a missing one.

-- **Join the tenant purge** (B1 #12, ADR-0092). The body below is regenerated
-- from **the `purge_tenant_data` definition on the `main` this branch is
-- actually merging into** (`052_knowledge_publish.sql` as of 2026-09-12,
-- which added `knowledge_chunk_identity`), plus `'materials'`; the reasoning for every other
-- line lives in `043` and is not copied.
--
-- **Why that sentence is load-bearing (independent review 2026-09-12 P1-1).**
-- Three in-flight batches each rewrite this whole function with
-- `CREATE OR REPLACE`, and their lists do not contain one another. Migrations
-- apply in **filename order** (`scripts/migrate.py`), so the highest number
-- wins and **silently drops** whatever a lower-numbered sibling added --
-- and because they are different files, **git reports no conflict at all**.
-- Worse, a clean database and an already-migrated one disagree: CI always
-- builds clean and applies by filename, while Stage applies by arrival, so the
-- two can end up with different purge functions and **the gate only ever sees
-- the clean one**. The failure is not a red build; it is a tenant asking to be
-- erased and their data staying.
--
-- **This comment is not the guard.** `tests/contracts/test_purge_is_monotonic.py`
-- refuses any redefinition that drops a table an earlier migration purged.
--
-- **Order**: `materials` has no foreign key to any other tenant-owned table
-- (only to `tenants`), so nothing constrains where it sits. It is placed
-- **after its nearest alphabetical predecessor** (`manual_tasks`) rather than
-- appended -- **the list as a whole is not alphabetical** (`policy_decisions`
-- precedes `intent_records`, `scheduled_events` precedes
-- `conversation_aggregates`), so "alphabetical position" would be a claim this
-- file cannot support; independent review 2026-09-12 P3-1. The reason for not
-- appending is the same one the RLS manifest entry gives: two authors
-- appending to the same tail collide, and
-- `test_the_purge_order_satisfies_every_foreign_key` re-derives the real
-- constraint from `pg_constraint` anyway, so position is a readability choice
-- and not a correctness one.
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'delivery_receipts',
        'employee_read_cursors',
        'employee_roles',
        'inbox_events',
        'ingress_replay_keys',
        'manual_tasks',
        'materials',
        'model_invocation_costs',
        'outbox_messages',
        'policy_decisions',
        'intent_records',
        'refresh_tokens',
        'retrieval_evidence',
        'roles',
        'scheduled_events',
        'conversation_aggregates',
        'knowledge_chunks',
        'knowledge_chunk_identity',
        'knowledge_releases',
        'contacts',
        'tenant_cost_ceilings',
        'tenant_manager_contacts',
        'channel_bindings',
        'ws_tickets',
        'employee_sessions',
        'employees'
    ];
    current_table text;
    removed bigint;
BEGIN
    FOREACH current_table IN ARRAY ordered_tables LOOP
        EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1', current_table)
            USING target_tenant;
        GET DIAGNOSTICS removed = ROW_COUNT;
        table_name := current_table;
        deleted := removed;
        RETURN NEXT;
    END LOOP;
END
$function$;

INSERT INTO schema_migrations(version) VALUES ('053_materials')
ON CONFLICT (version) DO NOTHING;
