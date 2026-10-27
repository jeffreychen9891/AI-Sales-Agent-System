-- CP-3. The role catalog, and the table that will one day say who holds what.
--
-- **Numbered 015 because 014 was taken when this was written**, by
-- `feat/l2b-delivery-manual-tasks-20260904`. That branch has since merged, so
-- 014 and 016 are both in the tree and this file sits between them -- which
-- makes the original note stale and hides the residue that actually matters
-- (independent review 2026-09-04, P3-3):
--
-- **A fresh database applies 014 → 015 → 016. An environment that already ran
-- `main` applies 015 *after* 016**, because `migrate.py` sorts by filename and
-- skips versions already recorded. Stage is such an environment: it is at 016
-- today and will pick this up next.
--
-- Harmless here -- 015 adds two nullable columns to tables 014 and 016 never
-- touch, so no ordering between them exists to violate. Written down because
-- "filename order is application order" stops being true across environments
-- the moment a number is skipped, and that assumption is exactly what the
-- original note claimed to be protecting.
--
-- **What this migration does not do.** It does not touch `employees.permissions`
-- and it does not touch `authorize()`. A role created through the routes in this
-- batch grants **nothing** to **nobody** until `PUT /employees/{employee_id}/roles`
-- (route freeze B1 #16) is built. That is the freeze's own sequencing -- #16 is
-- its own row, with its own "授权变更必审计" requirement -- and this batch keeps
-- to it rather than quietly wiring authorization to a new table.
--
-- Saying so here matters because the opposite belief is the dangerous one: an
-- operator who creates a role called "read-only" and assumes it has taken effect
-- has been handed a false sense of a boundary. The same statement is repeated in
-- the route docstrings, the self-review and the backlog, for the same reason the
-- M1a no-send boundary is.


-- ---------------------------------------------------------------- roles -----
--
-- `is_system` is a column rather than a naming convention because the routes
-- must refuse to modify or delete these rows, and a refusal that depends on a
-- string prefix is one rename away from not refusing.
--
-- `permissions` is a jsonb array of action strings, the same vocabulary
-- `employees.permissions` already uses and the same one `authorize()` reads.
-- Storing the codes rather than a join to a permission table is deliberate: the
-- authoritative list of permissions is `resource-catalog.json`, which ships with
-- the image. A permissions table in the database would be a second copy of it,
-- and two copies of an authorization vocabulary disagree eventually. The routes
-- validate every code against the catalog on the way in.
CREATE TABLE IF NOT EXISTS roles (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    description text CHECK (description IS NULL OR length(description) <= 256),
    permissions jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(permissions) = 'array'),
    is_system boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, name)
);

COMMENT ON TABLE roles IS
'A tenant''s named permission sets. Creating one grants nothing to anyone until
route freeze B1 #16 (PUT /employees/{employee_id}/roles) exists: authorize()
still reads employees.permissions and nothing else.';

COMMENT ON COLUMN roles.is_system IS
'System roles are refused by the update and delete routes. A column rather than a
name prefix, because a refusal that depends on a string is one rename away from
not refusing.';


-- ------------------------------------------------------ employee_roles -----
--
-- Empty in M1a, and created anyway. Two of this batch's guards -- "a role still
-- held by somebody cannot be deleted" and "the last administrator cannot be
-- stripped" -- are questions *about this table*. Without it they would have to
-- be written against nothing, which is how a guard comes to be tested by a case
-- that cannot fail.
--
-- The composite foreign keys carry the tenant into both references, so a row
-- cannot pair one tenant's employee with another tenant's role even if RLS were
-- somehow bypassed. That is belt and braces on purpose: this is the table that
-- will decide who can do what.
CREATE TABLE IF NOT EXISTS employee_roles (
    tenant_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    role_id uuid NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT now(),
    granted_by uuid,
    PRIMARY KEY (tenant_id, employee_id, role_id),
    FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id),
    FOREIGN KEY (tenant_id, role_id) REFERENCES roles(tenant_id, id)
);

COMMENT ON TABLE employee_roles IS
'Who holds which role. Written by no route in M1a -- freeze B1 #16 is the route
that will fill it. It exists now so that the delete and last-administrator
guards in CP-3 are asked of a real table rather than of nothing.';

CREATE INDEX IF NOT EXISTS employee_roles_by_role
    ON employee_roles (tenant_id, role_id);


DO $rls$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY['roles', 'employee_roles'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = target
              AND policyname = target || '_tenant_isolation'
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON %I USING '
                || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
                || 'WITH CHECK '
                || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)',
                target || '_tenant_isolation', target
            );
        END IF;
    END LOOP;
END
$rls$;

GRANT SELECT, INSERT, UPDATE, DELETE ON roles TO fastagent_app;
GRANT SELECT ON employee_roles TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('015_roles_and_assignments')
ON CONFLICT (version) DO NOTHING;
