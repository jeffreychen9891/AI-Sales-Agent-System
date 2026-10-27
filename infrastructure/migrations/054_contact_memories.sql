-- 054: what the platform remembers about a contact, and who said it.
--
-- **Migration number**: `053` was pre-allocated to this batch by
-- `docs/plans/parallel-safety-2026-09-12.md` §四 Y4. `052` went to the
-- knowledge-base author, E3-2 took `053` in its place, and this batch moved to
-- `054`. The chain is recorded in `docs/handoffs/merge-queue.md`.
--
-- **The field vocabulary is the merchant's, not the platform's** (ADR-0131 M2).
-- A jeweller needs ring size, budget, anniversary; an airline needs document
-- number, frequent cities, cabin preference; a tutoring business needs the
-- child's age, grade and lesson times. **Three sets with no intersection, and
-- the platform can never guess what the next industry needs.** So `field_key`
-- is bounded in length and **has no CHECK against a value list**: the list is
-- declared per profile (`memory_fields`, E4-1b) and enforced at the write path.
--
-- **What the platform does own**: the forbidden categories, the count and
-- length ceilings, and the lifecycle. Those are not editorial choices and they
-- do not vary by industry.
--
-- The same call `050_knowledge_base.sql` made for `knowledge_chunks`'s
-- editorial vocabularies, and for the same reason: freezing a growing
-- vocabulary in a migration means a migration every time it grows.
--
-- **`consent` does not mean "the customer agreed"** (ADR-0131 M1/§二.1).
-- Memories are the merchant's internal record: not disclosed to the customer,
-- not consented to by them; merchant staff can read and delete. The word in
-- freeze B4 #9 therefore reads as **"which employee authorised recording it"**
-- -- and that column is **deliberately not in this migration**, see below.

CREATE TABLE IF NOT EXISTS contact_memories (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,

    contact_id uuid NOT NULL,
    FOREIGN KEY (tenant_id, contact_id) REFERENCES contacts (tenant_id, id),

    -- **Merchant-declared, not platform-declared.** Length only. The profile
    -- names the legal keys; this column refuses nothing but an unbounded one.
    field_key text NOT NULL CHECK (length(field_key) BETWEEN 1 AND 128),
    field_value text NOT NULL CHECK (length(field_value) BETWEEN 1 AND 1024),

    -- **Where this came from.** Freeze B4 #9 requires evidence and provenance;
    -- B4 #10 requires that a new revision **not overwrite the source
    -- evidence**. The inbound event is the anchor because the source of a
    -- memory is *the customer's own words* -- unlike knowledge or materials,
    -- whose source is an external document.
    --
    -- **Nullable on purpose, and this is not the always-NULL shape**
    -- `AGENTS.md`'s second zero-caller question forbids: the control-plane
    -- write path (E4-3) records a fact an employee typed, which has no inbound
    -- event, while the `remember` path (E4-2) always has one. Two producers,
    -- two different truths -- not one producer that never fills it.
    source_event_id uuid,
    FOREIGN KEY (tenant_id, source_event_id) REFERENCES inbox_events (tenant_id, id),

    -- **Revision / supersede, not update** (freeze B4 #10). A correction is a
    -- new row that points at the one it replaces; the replaced row keeps its
    -- own evidence.
    --
    -- **There is no inverse link column.** The first draft's comment named a
    -- `superseded_by` that does not exist on this table (independent review
    -- 2026-09-12 P2-1), and a comment naming a column that is not there is the
    -- next author's map. The only mark on a replaced row is
    -- `status = 'superseded'`; walking forward means scanning `supersedes`.
    revision integer NOT NULL CHECK (revision > 0),
    supersedes uuid,
    CHECK (supersedes IS NULL OR supersedes <> id),
    CHECK ((revision = 1) = (supersedes IS NULL)),

    -- **Tombstone, not delete** (freeze B4 #11): "引用回答可解释" -- a reply
    -- that already used this memory must stay explicable after it is removed.
    status text NOT NULL CHECK (status IN ('active', 'superseded', 'tombstoned')),
    tombstoned_at timestamptz,
    CHECK ((status = 'tombstoned') = (tombstoned_at IS NOT NULL)),

    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),

    -- **The revision chain is a database fact, not a discipline**
    -- (independent review 2026-09-12 P2-2). A plain self-reference on
    -- `(tenant_id, supersedes)` let a revision point at **another contact's, or
    -- another field's** memory: "what was the previous version of this" could
    -- answer across customers, and nothing would go red. B4 #10 exists to keep a
    -- correction from losing the original's evidence, and a link that points
    -- somewhere else makes that sentence meaningless.
    --
    -- The shape is `052`'s: widen the key so the foreign key can carry the
    -- invariant. The extra `UNIQUE` is what the composite reference needs as its
    -- target; it is implied by the primary key and costs one index.
    UNIQUE (tenant_id, id, contact_id, field_key),
    FOREIGN KEY (tenant_id, supersedes, contact_id, field_key)
        REFERENCES contact_memories (tenant_id, id, contact_id, field_key)
);

-- **Deliberately absent: the authorising-employee column.**
--
-- ADR-0131 §二 lists `consent` in the first version, and it is -- but its only
-- producer is the control-plane route (E4-3). The `remember` path (E4-2) is
-- written by the worker, where **no employee authorised anything**. Shipping
-- the column now would make it `NULL` on every row that can exist, which is the
-- shape `AGENTS.md`'s second zero-caller question names and `T118`'s
-- always-`false` `legal_hold` is the registered precedent for.
-- It arrives with E4-3, the batch that can fill it.
--
-- **This is the same call E3-2 made about `created_by`**, on purpose: two
-- batches, one rule, so the next reader does not have to work out whether the
-- difference meant something.

-- One active memory per (contact, field) -- otherwise "what do we remember
-- about their ring size" is a question with no answer and the read path would
-- resolve it by whatever `ORDER BY` it happened to carry. (Same reasoning
-- `050_knowledge_base.sql` gives for its one-published-release index.)
CREATE UNIQUE INDEX IF NOT EXISTS contact_memories_one_active_per_field_idx
    ON contact_memories (tenant_id, contact_id, field_key)
    WHERE status = 'active';

-- The read path is "everything we remember about this contact".
CREATE INDEX IF NOT EXISTS contact_memories_contact_idx
    ON contact_memories (tenant_id, contact_id, status);

-- **A memory's substance is immutable; its lifecycle column is not.**
--
-- Shape copied from `006_profile_registry.sql`'s `profile_releases_immutable`
-- rather than invented. The narrow exceptions are the two lifecycle moves:
-- `active -> superseded` and `active|superseded -> tombstoned` (with its
-- stamp). Everything else -- contact, key, value, evidence, revision, the
-- supersede link, `created_at` -- is refused, because **a correction is a new
-- revision, not an edit** (B4 #10), and rewriting the value in place would
-- silently change what an already-sent reply was based on.
CREATE OR REPLACE FUNCTION contact_memories_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $contact_memories_reject_mutation$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.contact_id IS DISTINCT FROM OLD.contact_id
       OR NEW.field_key IS DISTINCT FROM OLD.field_key
       OR NEW.field_value IS DISTINCT FROM OLD.field_value
       OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
       OR NEW.revision IS DISTINCT FROM OLD.revision
       OR NEW.supersedes IS DISTINCT FROM OLD.supersedes
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'contact_memories rows are immutable; write a new revision instead'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Both lifecycle moves are one-way. A tombstoned memory that could come
    -- back would make "we no longer remember this" a statement with no fixed
    -- truth value, and B4 #11 requires the opposite.
    IF OLD.status = 'tombstoned' AND NEW.status <> 'tombstoned' THEN
        RAISE EXCEPTION
            'contact_memories rows cannot be un-tombstoned'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.status = 'superseded' AND NEW.status = 'active' THEN
        RAISE EXCEPTION
            'a superseded memory cannot become active again'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- **Once stamped, the moment we forgot is a fact and not a field**
    -- (independent review 2026-09-12 P3-1). Both CHECKs stay satisfied and the
    -- branch above only forbids un-tombstoning, so without this an already
    -- tombstoned row could have its timestamp rewritten -- and that timestamp is
    -- the answer to "from when did we stop using this".
    IF OLD.tombstoned_at IS NOT NULL
       AND NEW.tombstoned_at IS DISTINCT FROM OLD.tombstoned_at
    THEN
        RAISE EXCEPTION
            'contact_memories.tombstoned_at is set once and never rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END
$contact_memories_reject_mutation$;

DROP TRIGGER IF EXISTS contact_memories_immutable ON contact_memories;
CREATE TRIGGER contact_memories_immutable
    BEFORE UPDATE ON contact_memories
    FOR EACH ROW
    EXECUTE FUNCTION contact_memories_reject_mutation();

-- **No delete-refusing trigger** -- same correction E3-2 made after the gate
-- caught it there (`053_materials.sql` carries the full reasoning).
-- `purge_tenant_data` issues `DELETE FROM contact_memories WHERE tenant_id = $1`;
-- a trigger that refuses would make a tenant erasure fail, and
-- `DOCUMENTED_EXCLUSIONS` is not the right answer here either: these rows are
-- **what a tenant asked us to forget about their customers**, so keeping them
-- after an erasure request is the opposite of what the purge is for.
--
-- "Tombstone, never delete" is a rule about the product, enforced where deletes
-- could originate -- E4-3 ships `DELETE /contacts/{id}/memories/{id}` as a
-- tombstone, not a row removal. A whole-tenant erasure is not that path.

ALTER TABLE contact_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_memories FORCE ROW LEVEL SECURITY;
DO $contact_memories_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'contact_memories'
           AND policyname = 'contact_memories_tenant_isolation'
    ) THEN
        CREATE POLICY contact_memories_tenant_isolation ON contact_memories
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$contact_memories_rls$;

-- **No GRANT**, same as `050` and `053`: nothing reads or writes this table
-- yet. E4-2 brings the `remember` write path and E4-3 the routes; each grants
-- what it needs. A permission with no holder is harder to notice than a
-- missing one.

-- **Join the tenant purge** (B1 #12, ADR-0092). The list below is
-- **the previous definition's list plus `'contact_memories'`** -- not a copy of
-- an older one. As of 2026-09-12 that predecessor is `053_materials.sql`,
-- which in turn regenerated from `052_knowledge_publish.sql`
-- (`knowledge_chunk_identity`) after the knowledge base's fifth batch merged; the reasoning for every other line lives in `043`.
--
-- **The first draft copied `050`'s body and therefore dropped `'materials'`,
-- which `053` had added** (independent review 2026-09-12 P1-1). Three in-flight
-- batches each rewrite this whole function, migrations apply in filename order,
-- and **the highest number silently wins** -- with no git conflict, because they
-- are different files. Worse: a clean database applies by filename while an
-- already-migrated one applies by arrival, so **CI and Stage can end up with
-- different purge functions**, and CI only ever builds clean.
--
-- The failure is not a red build. It is a tenant asking to be erased and their
-- customers' memories staying in the database.
--
-- **This comment is not the guard**: `tests/contracts/test_purge_is_monotonic.py`
-- (added with `053`) refuses any redefinition that drops a table an earlier
-- migration purged. It caught this one.
--
-- **Order matters here, unlike for `materials`.** `contact_memories` has
-- foreign keys to `contacts` and `inbox_events`, so it must be deleted before
-- both. Placed at the head of the list, which satisfies that and keeps it away
-- from the tail two authors would otherwise both append to.
-- `test_the_purge_order_satisfies_every_foreign_key` re-derives the real
-- constraint from `pg_constraint`, so this is checked rather than reasoned out.
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'contact_memories',
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

INSERT INTO schema_migrations(version) VALUES ('054_contact_memories')
ON CONFLICT (version) DO NOTHING;
