-- 032: the three columns tenant deactivation writes, and nothing else.
--
-- **`019` did this for employees and stopped there.** It added `disabled_at`,
-- `purge_eligible_at` and `legal_hold` to *both* subjects, granted
-- `UPDATE (status, disabled_at, purge_eligible_at)` on `employees`, and left
-- `tenants` ungranted because the route that needed it (B1 #11) was not being
-- built yet. So this grant is the second half of a decision already made, not a
-- new one; the column list is copied from `019` deliberately.
--
-- **`legal_hold` is not in the list, for the reason `019` gives**: nothing in
-- M1a may set it (ADR-0048 K6, registered as T118), and a missing grant is the
-- cheapest place to make "may not" true rather than "does not". Purge reads it.
--
-- **`code` is not in the list either.** Renaming a tenant would change the
-- identifier `resolve_login_tenant` matches on and the one B1 #9's 409 keys on;
-- ADR-0074 §二 declined to let a `PATCH` pick that up as a side effect, and a
-- grant this route does not need is a grant the next route inherits.
--
-- **No RLS work is needed and that is worth saying once.** `tenants` is under
-- `tenants_tenant_isolation` (`id = app.tenant_id`), and the route opens its
-- transaction with `Decision.authorized_tenant_id` -- the tenant `authorize()`
-- decided on, which for a cross-tenant reach is the *target* (ADR-0068 §四点五).
-- So the policy is satisfied by the same value that made the reach legitimate,
-- and the definer function `030` and `031` needed -- because they act before any
-- tenant context exists, or on no tenant at all -- has no counterpart here.
--
-- The other two writes this route makes are already granted: `employee_sessions`
-- (`001`, whole-row `UPDATE`) and `channel_bindings.status` (`024`).

GRANT UPDATE (status, disabled_at, purge_eligible_at) ON tenants TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('032_tenant_deactivation')
ON CONFLICT (version) DO NOTHING;
