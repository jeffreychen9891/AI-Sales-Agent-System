-- 027: the refresh token family -- `D17`, and freeze B1 #5's only precondition.
--
-- `unified-authorization-layer.md:151` says what has to exist, in one sentence:
-- 「服务端存 hash、family、rotation counter、expiry 和 revoke reason；
--   每次使用旋转，重用即撤销整族」. `employee_sessions` carries none of it --
-- it has `credential_version` and `revoked_at` and nothing else -- which is why
-- `D17` reads 「表/列不存在」 rather than 「差一个字段」.
--
-- **A table rather than columns on `employee_sessions`.** A family is a chain
-- of tokens, and a chain does not fit in the row of the thing it belongs to:
-- rotation means one row per use, and reuse detection means the *old* rows have
-- to still be there to be recognised. Widening `employee_sessions` would have
-- forced either one row per rotation there (a session table that is no longer
-- about sessions) or an array (which cannot be indexed by hash, which is the
-- one lookup this table exists for).
--
-- **`token_hash` is unique across the table, not per family.** The lookup is
-- "somebody presented this token" and it has to answer without being told which
-- family to look in -- that is precisely the reuse case, where the presenter is
-- an attacker who does not announce themselves.
--
-- **`credential_version` is copied onto the row on purpose.** `:152` requires
-- that a password reset, a disable or a credential bump stops old refresh
-- tokens from extending privilege. Comparing the row's copy against the
-- employee's current value makes that a single condition rather than a promise
-- that some other route remembers to revoke; and it keeps working for tokens
-- issued before whatever changed.
--
-- **`revoke_reason` is free text and deliberately not a CHECK.** The values it
-- will hold (`reuse-detected`, `rotated`, `credential-version-changed`,
-- `logout`) are the vocabulary of routes that do not all exist yet, and a CHECK
-- constraint fixed today would be a migration owed by every one of them.

CREATE TABLE IF NOT EXISTS refresh_tokens (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    family_id uuid NOT NULL,
    employee_id uuid NOT NULL,
    session_id uuid NOT NULL,
    token_hash text NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    credential_version integer NOT NULL CHECK (credential_version > 0),
    rotation_counter integer NOT NULL DEFAULT 0 CHECK (rotation_counter >= 0),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    revoked_at timestamptz,
    revoke_reason text CHECK (revoke_reason IS NULL OR length(revoke_reason) <= 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (token_hash),
    FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES employee_sessions(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS refresh_tokens_family_idx
    ON refresh_tokens (tenant_id, family_id);

-- **At most one live token per family, guaranteed by the database.**
-- The route makes its rotation conditional (`used_at IS NULL`) so that two
-- racing refreshes cannot both succeed; this index is the same rule stated
-- where it cannot be forgotten. A family with two live tokens is precisely the
-- state `:151` exists to prevent -- an attacker and the real client each
-- holding a working chain -- and an independent review produced it with two
-- threads before the conditional write was added.
CREATE UNIQUE INDEX IF NOT EXISTS refresh_tokens_one_live_per_family_idx
    ON refresh_tokens (tenant_id, family_id)
 WHERE used_at IS NULL AND revoked_at IS NULL;

COMMENT ON TABLE refresh_tokens IS
'One row per issued refresh token; a family is every row sharing `family_id`.
Rotation writes a new row and stamps `used_at` on the old one, so presenting a
token whose `used_at` is already set is the reuse case -- and the answer to it
is to revoke the whole family, not just the token presented
(`unified-authorization-layer.md:151`). The rows are kept after use because
they are what makes that detection possible: deleting them would make a replayed
token indistinguishable from an unknown one.';

COMMENT ON COLUMN refresh_tokens.token_hash IS
'sha256 of the refresh token. **The verifier, not a redacted token** -- the same
reason `channel_bindings.current_token_hash` is readable by nobody who does not
need it. The plaintext exists for one response and is never stored.';

-- The `001` pattern, applied to one more table: enable, force (so the owner is
-- not exempt), and a policy keyed on the same GUC.
ALTER TABLE refresh_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_tokens FORCE ROW LEVEL SECURITY;
DO $refresh_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'refresh_tokens'
           AND policyname = 'refresh_tokens_tenant_isolation'
    ) THEN
        CREATE POLICY refresh_tokens_tenant_isolation ON refresh_tokens
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$refresh_rls$;

-- `SELECT` is needed to recognise a presented token, `INSERT` to rotate, and
-- `UPDATE` to stamp `used_at` / `revoked_at`. **No `DELETE`**: a deleted row is
-- a replay this table can no longer detect.
GRANT SELECT, INSERT, UPDATE ON refresh_tokens TO fastagent_app;

-- **The lookup happens before any tenant is known, so it cannot be an ordinary
-- SELECT.** `POST /auth/refresh` arrives with no bearer token -- the one it
-- exists to replace has expired -- so there is no `AuthContext` and nothing to
-- set `app.tenant_id` to. Under the policy above, a tenant-less read sees zero
-- rows: `tenant_id = NULL` is NULL, not true. **The token is what names the
-- tenant**, and something has to read it first.
--
-- `001` already met this exact shape and answered it the same way:
-- `resolve_channel_binding` exists because ingress must resolve a binding from a
-- path token before any subject or tenant exists, and it is `SECURITY DEFINER`
-- with a fixed column list rather than a table grant. This is that pattern, for
-- the same reason, with the same restraint:
--
-- * **a fixed column list**, so widening what the caller can see is a migration
--   somebody reviews, not a change of `SELECT *`;
-- * **`token_hash` is an argument, never returned** -- a function that handed
--   back the verifier would undo the point of storing a hash;
-- * **it returns at most one row** (`token_hash` is unique), so it cannot be
--   used to enumerate anything.
--
-- Everything after this lookup runs in a normal tenant-scoped transaction: the
-- function answers "whose token is this", and the policy governs the rest.
CREATE OR REPLACE FUNCTION resolve_refresh_token(candidate_token_hash text)
RETURNS TABLE (
    id uuid,
    tenant_id uuid,
    family_id uuid,
    employee_id uuid,
    session_id uuid,
    credential_version integer,
    rotation_counter integer,
    expires_at timestamptz,
    used_at timestamptz,
    revoked_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT token.id, token.tenant_id, token.family_id, token.employee_id,
           token.session_id, token.credential_version, token.rotation_counter,
           token.expires_at, token.used_at, token.revoked_at
      FROM public.refresh_tokens AS token
     WHERE token.token_hash = candidate_token_hash
$function$;

-- **`REVOKE` first.** PostgreSQL grants `EXECUTE` on a new function to
-- `PUBLIC`, so without this line every role in the cluster can call a
-- `SECURITY DEFINER` function that reads past RLS -- including `fastagent_ingress`
-- and `fastagent_worker`, whose whole point is that compromising one of them
-- does not hand over the others. `001` writes this line after each of its three
-- resolvers; 027 omitted it while claiming to have copied their restraint
-- (independent review 2026-09-05, F4).
REVOKE ALL ON FUNCTION resolve_refresh_token(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_refresh_token(text) TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('027_refresh_token_family')
ON CONFLICT (version) DO NOTHING;
