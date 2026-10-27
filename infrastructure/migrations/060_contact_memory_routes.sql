-- 060: the control plane's half of `contact_memories` — who authorised a
-- memory, why, and the permission that lets four routes supersede and
-- tombstone rows.
--
-- **Migration number**: `main` carried `059` when this batch opened, not `058`
-- as the outgoing handoff said. `059_embedding_cost_role.sql` is the knowledge
-- base's sixth batch after it renumbered off a collision (`00a785fd`,
-- 「迁移 057 → 059」). **Re-checking the tip is what the handoff asked for and it
-- was not a formality**: taking `058` on faith would have collided with the
-- migration written *because of* the previous collision. Claimed in
-- `docs/handoffs/merge-queue.md` before a line of it was written.
--
-- ---------------------------------------------------------------------------
-- 一、`054` planned this column and said so
-- ---------------------------------------------------------------------------
--
-- That migration left the authorising-employee column out on purpose, in as
-- many words:
--
--   > **Deliberately absent: the authorising-employee column.** … its only
--   > producer is the control-plane route (E4-3). The `remember` path (E4-2) is
--   > written by the worker, where **no employee authorised anything**.
--   > Shipping the column now would make it `NULL` on every row that can exist
--   > … It arrives with E4-3, the batch that can fill it.
--
-- This is that batch. **`consent` means "which employee authorised recording
-- it", not "the customer agreed"** (ADR-0131 M1/§二.1; freeze B4 #9 carries the
-- same note): memories are the merchant's internal record, not disclosed to the
-- customer and not consented to by them.
--
-- ---------------------------------------------------------------------------
-- 二、Two columns, because freeze B4 #9 asks for five things and one of them
--     has nowhere to live
-- ---------------------------------------------------------------------------
--
-- B4 #9's basis column lists: field allowlist, consent, evidence, PII,
-- provenance. Four of those already have a home — the allowlist is the
-- profile's `memory_fields` declaration checked at the write path, PII is the
-- denylist at seal time (E4-1b) plus the value tripwire this batch adds,
-- consent and provenance are `authorised_by` below.
--
-- **`evidence` had none.** The `remember` path's evidence is
-- `source_event_id` — the customer's own words. A memory an employee typed has
-- no inbound event; its evidence is *what the employee says they are going on*,
-- and there was no column holding it. `authorisation_note` is that column.
--
-- **Why a column rather than the audit row.** The audit event carries actor,
-- action, correlation and reason code already, and putting the note only there
-- was the first design. It fails one of this route family's own acceptance
-- criteria: B4 #11 requires 「引用回答可解释」 — a reply that used a memory stays
-- explicable after the memory is removed. **No route in the freeze reads
-- `audit_events`** (「the control plane's first audit read」 is still listed as
-- unbuilt in §四), so an explanation that lives only there is an explanation
-- nobody can reach. It lives on the row, and `GET …/memories` returns it.
ALTER TABLE contact_memories
    ADD COLUMN IF NOT EXISTS authorised_by uuid,
    ADD COLUMN IF NOT EXISTS authorisation_note text;

-- **The employee is a real employee of this tenant**, and the composite key is
-- the whole enforcement — **there is no route-side pre-check, and none is
-- owed**. `034_manual_tasks.sql` needed one because `owner_employee_id` comes
-- out of a request body, so "no such employee" is a caller mistake that
-- deserves a 404 rather than a constraint violation surfacing as a 500. Here
-- the only producer is `context.subject.id`, and the whole repository
-- constructs `Subject` in exactly one place (`runtime/auth.py`), from the `sub`
-- claim of a token that login issues against an `employees` row. `superuser` is
-- not a different kind of identity, it is such an employee whose
-- `(tenant_id, id)` appears in the allowlist file.
--
-- So this key cannot be violated from these four routes; it is here for the
-- writer that does not exist yet. **Checked when an independent review asked
-- (2026-09-12), rather than deferred as "somebody else's family of routes".**
--
-- `IF NOT EXISTS` is not available for `ADD CONSTRAINT`; the `DO` block is the
-- shape `054` used for its policy.
DO $contact_memories_authoriser_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'contact_memories_authorised_by_fkey'
           AND conrelid = 'contact_memories'::regclass
    ) THEN
        ALTER TABLE contact_memories
            ADD CONSTRAINT contact_memories_authorised_by_fkey
            FOREIGN KEY (tenant_id, authorised_by)
            REFERENCES employees (tenant_id, id);
    END IF;
END
$contact_memories_authoriser_fk$;

-- **Exactly one origin per row, and the database says so.**
--
-- `054` described two producers with two different truths — the worker fills
-- `source_event_id`, the route fills the employee — and left that as prose. It
-- is an invariant, so it is written as one: a memory is anchored either to a
-- customer event or to an employee, never both and never neither.
--
-- **"Never both" is the load-bearing half.** Without it the `PUT` route could
-- carry the superseded row's `source_event_id` onto the correction, and the new
-- row would then claim the customer said something the employee wrote. Freeze
-- B4 #10's 「不覆盖来源证据」 is usually read as "don't erase it"; the shape that
-- actually threatens it here is the opposite — *copying* it onto a row it does
-- not belong to.
--
-- **"Never neither" is why existing rows validate.** Every row `_remember_write`
-- can produce has `source_event_id` set unconditionally
-- (`pipeline.py`, `UUID(str(prepared.envelope["event_id"]))`), so this
-- constraint is checked against existing data and passes rather than being
-- added `NOT VALID` and hoped about.
DO $contact_memories_one_origin$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'contact_memories_exactly_one_origin'
           AND conrelid = 'contact_memories'::regclass
    ) THEN
        ALTER TABLE contact_memories
            ADD CONSTRAINT contact_memories_exactly_one_origin
            CHECK (num_nonnulls(source_event_id, authorised_by) = 1);
    END IF;
END
$contact_memories_one_origin$;

-- **An employee-authored memory always says what it is going on.** The note is
-- required exactly when the employee is, and forbidden when they are not —
-- a worker-written row has no employee to explain themselves.
DO $contact_memories_note_pairs$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'contact_memories_note_accompanies_authoriser'
           AND conrelid = 'contact_memories'::regclass
    ) THEN
        ALTER TABLE contact_memories
            ADD CONSTRAINT contact_memories_note_accompanies_authoriser
            CHECK (
                (authorised_by IS NULL) = (authorisation_note IS NULL)
                AND (authorisation_note IS NULL
                     OR length(authorisation_note) BETWEEN 8 AND 500)
            );
    END IF;
END
$contact_memories_note_pairs$;

-- ---------------------------------------------------------------------------
-- 三、The immutability trigger has to learn about the two new columns
-- ---------------------------------------------------------------------------
--
-- **This is the part a new column silently breaks.** `054`'s trigger refuses to
-- let an `UPDATE` change a memory's substance, and it does that by listing the
-- columns one at a time. A column added later is not on that list, so it is
-- **writable** — and this migration hands `fastagent_app` the `UPDATE` verb in
-- the same file. Without these two lines, 「哪个员工授权记的」 would be a field any
-- later employee could rewrite to somebody else's name, which is the one thing
-- a consent record must not be.
--
-- The function is replaced whole rather than patched, because that is the only
-- way `CREATE OR REPLACE FUNCTION` works; everything else below is `054`'s body
-- unchanged. **The list-shaped guard is the hazard here** — the next column
-- added to this table has the same trap waiting, and
-- `test_every_substantive_column_is_refused_by_the_trigger` (this batch) now
-- derives the column list from `information_schema` instead of trusting it.
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
       -- **060**: who authorised it and what they were going on are as
       -- substantive as the value itself. A correction is a new revision.
       OR NEW.authorised_by IS DISTINCT FROM OLD.authorised_by
       OR NEW.authorisation_note IS DISTINCT FROM OLD.authorisation_note
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

-- ---------------------------------------------------------------------------
-- 四、The grant, and why `UPDATE` here does not contradict 裁定八
-- ---------------------------------------------------------------------------
--
-- `057` granted the worker `SELECT, INSERT` and **withheld `UPDATE`**, with the
-- reasoning spelled out: 裁定八 (ADR-0131 M3) gives the model the right to
-- append and withholds the right to rewrite what is already there. It closed by
-- naming this migration: 「**E4-3's `PUT` / `DELETE` routes are what supersede and
-- tombstone rows**, and that batch grants the `UPDATE` it needs — to
-- `fastagent_app`, with its own reasoning, in its own migration.」
--
-- **裁定八 has two halves and they point in opposite directions**:
-- 「摘要人工可编辑，模型不能自改」 — *people* may correct, the *model* may not.
-- `fastagent_worker` is where the model writes; `fastagent_app` is where a
-- logged-in employee does. Granting `UPDATE` to the second role is the first
-- half of the ruling, not an exception to the second.
--
-- **So `057`'s two cases part ways here, and only one of them goes red.**
-- `test_contact_memories_grants.py` asserts the whole surface — every verb ×
-- every role — so the parametrised case for `fastagent_app` fails until its
-- `EXPECTED` gains this row, and that is the case doing its job: it forces
-- whoever changes the grant to come back and say so.
-- `test_the_model_cannot_rewrite_a_memory_because_the_role_cannot` asks only
-- about `fastagent_worker` and **stays green**, which is correct and worth
-- stating: the outgoing handoff predicted 「那条用例会因此变红」, and if it had, the
-- right response would have been to stop — a green there is the evidence that
-- this grant did not reach the model's role.
--
-- **What each verb is for**, so a later reader can tell a used permission from
-- a precautionary one:
--
--   * `SELECT` — `GET /contacts/{id}/memories` (B4 #8), and the pre-checks the
--     three writers run before they touch a row.
--   * `INSERT` — `POST` (B4 #9) writes revision 1; `PUT` (B4 #10) writes the
--     *next* revision, because a correction is a new row, not an edit.
--   * `UPDATE` — the two lifecycle moves and nothing else, because the trigger
--     above refuses everything else at the database: `PUT` marks the replaced
--     row `superseded`, `DELETE` (B4 #11) marks a row `tombstoned` and stamps
--     the moment.
--
-- **No `DELETE` verb for anybody**, unchanged from `057`: removal goes through
-- `purge_tenant_data`, whose permission is attached to an audited
-- `SECURITY DEFINER` body (`043`'s words). B4 #11's "delete" is a tombstone —
-- `054` says so — and a route that could really remove the row would take
-- 「引用回答可解释」 with it.
--
-- **No RLS change**, same as `057`: `054` set the policy and `FORCE ROW LEVEL
-- SECURITY`, and `fastagent_app` still sees only the tenant its session is set
-- to. Granting and isolating are different questions.
GRANT SELECT, INSERT, UPDATE ON contact_memories TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('060_contact_memory_routes')
ON CONFLICT (version) DO NOTHING;
