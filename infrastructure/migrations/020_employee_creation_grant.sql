-- 020: let the control plane create employees -- and only create them.
--
-- **B1 #14 needs one privilege the app role does not have.** `001` granted
-- `SELECT` on `employees`; `018` and `019` added `UPDATE` on six named columns.
-- There is no `INSERT` anywhere, so `POST /employees` cannot write a row at all
-- until this migration runs. That is the column-level authorisation story of
-- this table working as intended: every new writing route has to come and ask.
--
-- **Column-level, and the omissions are the point.** The route may write who
-- somebody is (`email`, the three profile columns) and the hash of what they
-- typed. It may **not** write:
--
--   * `permissions` -- a create route that could mint privileges would be a
--     privilege-escalation route wearing a different name. It defaults to
--     `'[]'`, so a new employee starts able to do nothing, and freeze B1 #16
--     (`PUT /employees/{id}/roles`) is the only door to more;
--   * `status` -- `'active'` by default. Deactivation is #18's, and a create
--     route that could set `'disabled'` would be a second, untested way into
--     the state #18 is careful about;
--   * `credential_version` / `password_changed_at` -- defaults, and they belong
--     to the credential lifecycle (#17). A route that could set
--     `credential_version` could mint a value that silently invalidates every
--     live session, or one that fails to;
--   * `disabled_at` / `purge_eligible_at` / `legal_hold` -- the retention clock
--     and the hold that stops it. Nothing about creating a person should be
--     able to touch either.
--
-- **No `INSERT` for the other roles.** `fastagent_worker`, `fastagent_ingress`
-- and `fastagent_scheduler` have no business creating people, and none of them
-- holds any privilege on this table today.
--
-- Re-run safety comes from `migrate.py` skipping applied versions
-- (`schema_migrations`); `GRANT` happens to be idempotent, but the file is not
-- relying on that.

GRANT INSERT (tenant_id, id, email, password_hash, display_name, employee_no, department)
    ON employees TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('020_employee_creation_grant')
ON CONFLICT (version) DO NOTHING;
