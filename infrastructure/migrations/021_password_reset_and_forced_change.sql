-- 021: the credential lifecycle -- `must_change_password`, and the privileges
-- the two routes that own it need.
--
-- **D16's last missing column, and it arrives with its enforcement.** Migration
-- 018 refused to add `must_change_password` and said why:
--
--     A flag that exists and is enforced nowhere is the "record says one thing,
--     system does another" shape this repository keeps paying for … It arrives
--     with its enforcement.
--
-- This is that batch. The flag is written by B1 #17
-- (`POST /employees/{id}/password-reset`) and by `POST /employees`; it is read
-- by the authentication dependency, which answers **403
-- `PASSWORD_CHANGE_REQUIRED`** to every route while it is true; and it is
-- cleared by exactly one route, `POST /auth/change-password` -- freeze B1 #30,
-- added to the freeze by **ADR-0052** because ADR-0048 §二.5 (K8) named a
-- "改密路由" that no drawing had ever contained.

ALTER TABLE employees
  ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;

-- **The default is `false`, and existing rows keep it.** The column means "this
-- password was set by somebody other than the person it belongs to" -- that is
-- what makes forcing a change reasonable rather than officious. For rows that
-- predate the column there is no record of who set the password, so the honest
-- backfill is the one that does not assert something unrecorded. The rows that
-- exist today are test seeds; there is no deployed tenant, and `POST
-- /employees` -- the only route that has ever written `password_hash` -- landed
-- earlier the same day. Both writers added by this batch set the flag
-- explicitly, so `false` is never inherited by a password somebody else chose.
COMMENT ON COLUMN employees.must_change_password IS
'True when this password was set by somebody other than its owner -- an
administrator creating the account (freeze B1 #14) or resetting it (B1 #17).
While true, every authenticated route answers 403 PASSWORD_CHANGE_REQUIRED
except POST /auth/change-password (freeze B1 #30, ADR-0052), which is the only
statement in the repository that sets it back to false. ADR-0048 §二.5 (K8): a
temporary password handed over through a third-party channel must not stay
valid indefinitely.';

-- `POST /employees` writes it at creation. The seven-column `INSERT` grant from
-- 020 becomes eight -- and this is the widening 020's test anticipated in so
-- many words ("later batches add insertable columns legitimately; #17's
-- `must_change_password` will be one"), which is why that test asserts a subset
-- rather than an equality.
GRANT INSERT (must_change_password) ON employees TO fastagent_app;

-- The reset route writes all four; the change-password route writes the same
-- four minus none of them. Column-level for the reason 018 and 019 give: this
-- row also carries `permissions`, `status`, `legal_hold` and the retention
-- clock, and each of those has its own route and its own authorisation.
--
-- **`credential_version` is new for this role** and it is the load-bearing one:
-- the authentication dependency compares the value in the presented token
-- against the column, so incrementing it is what makes every token minted
-- before the password change stop authenticating -- including tokens whose
-- session row a `revoked_at` sweep might miss.
-- `unified-authorization-layer.md:160` asks for exactly this ("reset 后撤销
-- refresh family，递增 credential version").
GRANT UPDATE (password_hash, password_changed_at, credential_version, must_change_password)
    ON employees TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('021_password_reset_and_forced_change')
ON CONFLICT (version) DO NOTHING;
