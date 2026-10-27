-- 031: where a platform-level event is recorded, and how the registry is read.
--
-- **Both halves exist because a platform action has no single tenant** (ADR-0073).
--
-- **`platform_audit_events`, and why not a nullable column on `audit_events`.**
-- `001` gave `audit_events.tenant_id` `NOT NULL REFERENCES tenants(id)` and made
-- it half the primary key. A platform-level read names no tenant at all --
-- `authorize()` hands the route an audit obligation whose `target_tenant_id` is
-- `None` -- so the row does not fit. Widening that column to nullable would buy
-- one route's convenience with a constraint that is silently doing work at more
-- than a dozen *tenant-level* write sites: today, a write that forgets its
-- tenant is refused by the database. After the widening, nothing would say a
-- word. So the platform gets its own table rather than the tenant table getting
-- looser (ADR-0073 §三).
--
-- **`B1 #9` does not use this table and should not**: provisioning creates a
-- real tenant, and its audit row belongs to that tenant (`030`). The rule is
-- "can this event name a tenant?", not "is the actor a superuser?" -- which is
-- why B1 #10-#12, cross-tenant reaches that name exactly one tenant, will keep
-- writing to `audit_events`.
--
-- **Writes come through the definer function only.** The table grants
-- `fastagent_app` nothing at all; the application can reach it solely through
-- `record_platform_audit_event`. `ENABLE ROW LEVEL SECURITY` with **no policy**
-- is the same statement said a second way: with no tenant column there is no
-- isolation predicate to write, and a table that denies everyone by default is
-- the honest shape for one whose only legitimate writer is a named function.
--
-- **`ENABLE` and `FORCE`, like every other table here.** What makes the definer
-- function's write work is not the absence of `FORCE`: it is that the function
-- runs as `fastagent_migrator`, which is `rolsuper = t`, and **a superuser
-- bypasses row-level security unconditionally** -- `FORCE` only reaches an
-- ordinary owner. The repository already proves this every day: `tenants` and
-- `audit_events` both carry `FORCE`, and `030`'s definer function writes to both.
--
-- > **The first version of this header said the opposite** -- that `FORCE`
-- > would "deny the one writer we mean to allow" -- and it said so in the very
-- > paragraph that corrects `030` for the same confusion. Independent review
-- > measured it: a table with `ENABLE + FORCE`, no policy, owned by the
-- > migrator, accepts both a direct owner insert and a write through a definer
-- > function called by `fastagent_app`. The claim was false, and it had bought
-- > this table an exemption -- **the only table in the repository without
-- > `FORCE`** -- on a reason that did not hold. Recorded rather than quietly
-- > deleted, because "the reason was wrong but the code happened to be safe" is
-- > the interesting half.
--
-- > Footnote for the next reader of `030`: its header says `FORCE ROW LEVEL
-- > SECURITY` "does not apply to a table's owner". That is not what `FORCE`
-- > does -- it is what plain `ENABLE` does. What actually carries `030` is the
-- > same `rolsuper = t` named above. `030`'s behaviour is correct; only that one
-- > sentence reads wrong, and ADR-0073 §二 records it rather than editing an
-- > applied migration.
--
-- **So what actually keeps `fastagent_app` out of this table** is stated once,
-- here, and asserted by `test_the_application_role_cannot_touch_the_new_table_directly`:
-- it holds no privilege on the table (`has_table_privilege` says so for all
-- four), and it does not own it. RLS is a third layer behind those two, not the
-- one doing the work.
--
-- **No `UPDATE` / `DELETE` to anyone** -- `unified-authorization-layer.md` §312
-- makes audit tables append-only, and that applies to this one word for word.
--
-- **`list_tenant_registry()`, and why a definer function for a plain read.**
-- `001` already grants `fastagent_app` `SELECT` on `tenants`; what stops the
-- list is the RLS policy `tenants_tenant_isolation`, whose predicate is
-- `id = app.tenant_id`. A tenant-less transaction sets no `app.tenant_id`, so
-- the predicate is `id = NULL` and the read returns nothing at all -- not an
-- error, an empty page, which is the worst of the three possible outcomes. The
-- narrow definer function is how this repository has crossed that line four
-- times already (`resolve_login_tenant`, `list_active_tenant_ids`,
-- `resolve_channel_binding`, `resolve_refresh_token`), and it keeps the policy
-- untouched for every other reader.
--
-- **Four columns, because four were ruled** (ADR-0073 §四): id, code, status and
-- `created_at`. Not `SELECT *` -- a column added to `tenants` later would
-- otherwise reach this list on the day it is created, which is the shape T114
-- registered about roles and the same answer applies here.

CREATE TABLE IF NOT EXISTS platform_audit_events (
    id uuid PRIMARY KEY,
    -- No foreign key, for the same reason `audit_events.actor_id` has none: the
    -- actor may belong to another tenant, or -- for a subject the deployment
    -- promoted -- effectively nowhere.
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    correlation_id uuid NOT NULL,
    reason_code text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE platform_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_audit_events FORCE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS platform_audit_events_action_time_idx
    ON platform_audit_events (action, created_at DESC);

COMMENT ON TABLE platform_audit_events IS
    'Audit rows for events that belong to no single tenant (ADR-0073). Events '
    'that can name a tenant belong in audit_events instead; append-only.';

CREATE OR REPLACE FUNCTION record_platform_audit_event(
    event_id uuid,
    actor uuid,
    event_action text,
    event_resource_type text,
    event_resource_id uuid,
    correlation uuid,
    event_reason_code text,
    event_evidence jsonb
)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    INSERT INTO public.platform_audit_events(
        id, actor_id, action, resource_type,
        resource_id, correlation_id, reason_code, evidence
    )
    VALUES (
        event_id, actor, event_action, event_resource_type,
        event_resource_id, correlation, event_reason_code, event_evidence
    );
$function$;

CREATE OR REPLACE FUNCTION list_tenant_registry(row_limit integer)
RETURNS TABLE (id uuid, code text, status text, created_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT tenant.id, tenant.code, tenant.status, tenant.created_at
      FROM public.tenants AS tenant
     -- Ordered oldest first and tie-broken by id, so two calls with the same
     -- data return the same page. `created_at` alone is not a total order: two
     -- tenants provisioned in the same transaction share it exactly.
     ORDER BY tenant.created_at, tenant.id
     LIMIT row_limit
$function$;

-- The closing move every definer function here makes: executable by the
-- application role and by nobody else. Without the REVOKE, PUBLIC keeps its
-- default EXECUTE and the function becomes a way past RLS for any role that can
-- reach the database at all.
REVOKE ALL ON FUNCTION record_platform_audit_event(uuid, uuid, text, text, uuid, uuid, text, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION list_tenant_registry(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION record_platform_audit_event(uuid, uuid, text, text, uuid, uuid, text, jsonb)
    TO fastagent_app;
GRANT EXECUTE ON FUNCTION list_tenant_registry(integer) TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('031_platform_audit_and_tenant_registry_list')
ON CONFLICT (version) DO NOTHING;
