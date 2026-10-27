-- 030: provisioning a tenant, and its audit row, as one statement.
--
-- **Why a `SECURITY DEFINER` function and not a `GRANT INSERT`.** `001` enables
-- *and forces* row-level security on `tenants` with a policy that has no `TO`
-- clause, so it binds every role including `fastagent_app`. A row for a tenant
-- that does not exist yet cannot satisfy a tenant-isolation policy -- the
-- session has no tenant to be, and the row's own tenant is the thing being
-- created. This is the same shape `resolve_login_tenant`, `resolve_channel_binding`
-- and `resolve_refresh_token` already have: a narrow definer function is how
-- this repository crosses "before a tenant context exists", and widening RLS
-- instead would trade one route's needs against every other route's isolation.
--
-- **Why this function is not itself stopped by the same RLS** (independent
-- review P3-2, measured): `FORCE ROW LEVEL SECURITY` does not apply to a
-- table's owner, and this function runs as its own owner -- `fastagent_migrator`,
-- which migrations run as. So the bypass here is **ownership**, not
-- `BYPASSRLS` and not `row_security = off`; `check_rls_migration` greps for
-- those two and an ownership bypass walks underneath it. Written down because
-- the next reader should not have to go to `pg_proc` to find out why the insert
-- above works when the one this migration argues against does not.
--
-- **This is also the repository's first definer function that writes** -- the
-- three that exist (`resolve_login_tenant`, `resolve_channel_binding`,
-- `resolve_refresh_token`) only read. A definer function that writes is a
-- larger grant of trust, which is why the `REVOKE ALL` below is not optional.
--
-- **The audit row is written by the same statement, and that is the point**
-- (T163). §3.1 says an obligation that fails must fail the operation, and
-- ADR-0068 §四 put the target tenant into the audit obligation's payload so the
-- discharging route would not have to reconstruct it. Here the two writes
-- cannot come apart at all: there is no interleaving in which a tenant exists
-- and its provisioning record does not, because a single `INSERT ... WITH`
-- either commits both or neither. "Succeed now, audit later" is not a
-- discipline the route has to keep -- it is unrepresentable.
--
-- **`audit_events.tenant_id` is the new tenant**, not the actor's. The row
-- records something that happened *to* that tenant, the foreign key requires a
-- tenant that exists, and the actor travels in `actor_id` -- which carries no
-- foreign key precisely because an actor may belong somewhere else, or, for a
-- promoted subject reaching in from another tenant, effectively nowhere.

CREATE OR REPLACE FUNCTION provision_tenant(
    new_tenant_id uuid,
    new_code text,
    actor uuid,
    correlation uuid,
    audit_event_id uuid,
    audit_evidence jsonb
)
RETURNS TABLE (id uuid, code text, status text, created_at timestamptz, created boolean)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    WITH inserted AS (
        INSERT INTO public.tenants(id, code)
        VALUES (new_tenant_id, new_code)
        -- **The taken code is answered here, not by a second query.** The route
        -- used to catch `UniqueViolation` and then look the existing tenant up,
        -- which cannot work twice over: the transaction is already aborted, and
        -- `tenants` is under RLS that a tenant-less session sees nothing
        -- through. This function is `SECURITY DEFINER`, so it is the one place
        -- that can answer "who holds that code" at all.
        ON CONFLICT (code) DO NOTHING
        RETURNING tenants.id, tenants.code, tenants.status, tenants.created_at
    ), audited AS (
        -- Driven by `inserted`, so a conflict writes **no audit row**: nothing
        -- happened, and a record saying otherwise would be worse than none.
        INSERT INTO public.audit_events(
            tenant_id, id, actor_id, action, resource_type,
            resource_id, correlation_id, reason_code, evidence
        )
        SELECT inserted.id, audit_event_id, actor, 'tenant_registry.create',
               'tenant_registry', inserted.id, correlation, 'TENANT_PROVISIONED',
               audit_evidence
          FROM inserted
    )
    SELECT inserted.id, inserted.code, inserted.status, inserted.created_at, true
      FROM inserted
    UNION ALL
    -- Exactly one row either way: the caller always learns which tenant holds
    -- the code, and `created` says whether this call is why.
    SELECT existing.id, existing.code, existing.status, existing.created_at, false
      FROM public.tenants AS existing
     WHERE existing.code = new_code
       AND NOT EXISTS (SELECT 1 FROM inserted);
$function$;

-- The same closing move as every other definer function here: it is executable
-- by the application role and by nobody else. Without the `REVOKE`, `PUBLIC`
-- keeps the default `EXECUTE` and the function becomes a way around RLS for
-- any role that can reach the database at all.
REVOKE ALL ON FUNCTION provision_tenant(uuid, text, uuid, uuid, uuid, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION provision_tenant(uuid, text, uuid, uuid, uuid, jsonb) TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('030_tenant_provisioning')
ON CONFLICT (version) DO NOTHING;
