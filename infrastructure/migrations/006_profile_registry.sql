-- 006: give profile releases a production write path, and make them immutable.
--
-- `profile_releases` has existed since 001, but nothing outside the test harness
-- could write to it: `fastagent_app` held no privilege on the table at all, so
-- the only INSERT in the repository was `tests/conftest.py`. A release that only
-- the test fixture can create is not a release; it is a fixture. This migration
-- adds the missing grant and, with it, the two guards RFC §9.1 requires of a
-- published release:
--
--   * immutable -- a row may be inserted and read, never updated or deleted.
--     Rollback is "bind the previous release", not "rewrite this one"; evidence
--     recorded against a release id must keep resolving to the same bytes.
--   * attributable -- who published it, and which trusted package it came from.
--
-- **immutable != signed.** RFC §9.1's chain is
-- draft -> validation -> *review* -> *signed* immutable release -> Stage bind ->
-- user confirmation -> Production bind. This migration delivers the immutable
-- and attributable parts only. There is no signature over the release and no
-- review gate in front of it; `source_path` plus `published_by` are the whole
-- provenance story today, and both are written by the publishing process rather
-- than proven to a verifier. Registered as T17 (signature) and T18 (review
-- gate) -- see docs/backlog/ideas.md. Independent review 2026-09-02, P1-2.
--
-- **immutable != signed.** RFC §9.1's chain is
-- draft -> validation -> *review* -> *signed* immutable release -> Stage bind ->
-- user confirmation -> Production bind. This migration delivers the immutable
-- and attributable parts only. There is no signature over the release and no
-- review gate in front of it; `source_path` plus `published_by` are the whole
-- provenance story today, and both are written by the publishing process rather
-- than proven to a verifier. Registered as T17 (signature) and T18 (review
-- gate) -- see docs/backlog/ideas.md. Independent review 2026-09-02, P1-2.
--
-- The content hash is deliberately NOT verified here. Canonical JSON is not
-- reproducible inside PostgreSQL (jsonb has its own key order and number
-- normalisation), so a CHECK constraint would either be wrong or would pin a
-- second, divergent canonicalisation. The rule lives in exactly one place --
-- `fastagent_v2.profiles.registry.canonical_hash` -- and is enforced on the way
-- in (publisher) and on the way out (resolver), with tests on both directions.
--
-- `published_by` carries no foreign key on purpose. `employees` runs with FORCE
-- ROW LEVEL SECURITY, so a referential-integrity check against it would depend
-- on `app.tenant_id` being set inside whatever transaction happens to touch the
-- row. An attribution column that can fail for a reason unrelated to attribution
-- is worse than one the application fills honestly and a test pins.
--
-- The trigger binds every role, superusers included -- but it is a row trigger,
-- and a table owner or superuser can step around any row trigger. Three ways,
-- all measured on a live database by the independent review (2026-09-02):
--
--   * TRUNCATE                                  -- row triggers do not fire on it;
--   * ALTER TABLE ... DISABLE TRIGGER           -- then UPDATE/DELETE succeeds;
--   * SET session_replication_role = 'replica'  -- then UPDATE/DELETE succeeds.
--
-- All three require table ownership or superuser, which **no runtime role has**
-- (`fastagent_app` holds SELECT + INSERT and nothing else; see the GRANT below).
-- They are therefore outside the runtime threat model and are listed here so the
-- comment reads as what it is -- a bounded statement about runtime roles -- and
-- not as an exhaustive claim about the trigger. The test harness uses the first
-- of them deliberately: it resets by truncating as the migration role.
--
-- No down script: this repository has no downgrade runner, and the recovery
-- contract for Stage is restore-from-dump (ADR-0007/0008), not a reverse
-- migration. Dropping the trigger would silently make releases mutable again.

ALTER TABLE profile_releases
    ADD COLUMN IF NOT EXISTS published_by uuid,
    ADD COLUMN IF NOT EXISTS source_path text;

COMMENT ON COLUMN profile_releases.published_by IS
'Employee who published the release, when the publish came from an authenticated
operator. NULL means the publish came from the operator CLI without an employee
identity (Stage bootstrap, before any employee exists). The audit_events row is
the authoritative record either way; this column exists so the release itself
answers "who" without a join.';

COMMENT ON COLUMN profile_releases.source_path IS
'Repository-relative path of the trusted profile package this release was built
from, e.g. "profiles/synthetic-third/profile.json". Provenance, not a load path:
nothing reads the file back through it.';

CREATE OR REPLACE FUNCTION profile_releases_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION
        'profile_releases is append-only; % is not permitted on release %',
        TG_OP, COALESCE(OLD.id::text, '<unknown>')
        USING ERRCODE = 'restrict_violation',
              HINT = 'publish a new release and rebind: releases are never rewritten';
END
$function$;

DROP TRIGGER IF EXISTS profile_releases_immutable ON profile_releases;
CREATE TRIGGER profile_releases_immutable
    BEFORE UPDATE OR DELETE ON profile_releases
    FOR EACH ROW EXECUTE FUNCTION profile_releases_reject_mutation();

-- The publish path runs as the API role. INSERT only: the trigger above already
-- refuses UPDATE and DELETE, and withholding the privilege as well means the
-- refusal does not depend on the trigger surviving a future migration.
GRANT SELECT, INSERT ON profile_releases TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('006_profile_registry')
ON CONFLICT (version) DO NOTHING;
