-- L2a. Who to tell when a conversation lands on a person's desk.
--
-- One row per tenant, and the primary key says so: "一个商家一个管理人" is the
-- user's ruling (2026-09-04), written as a constraint rather than as an
-- application check, so a second manager cannot arrive by accident. Making it
-- many would be a migration and a decision, which is the right price.
--
-- **Why a table rather than a column on `tenants`.** `tenants` is the M0
-- platform table (id / code / status) and carries no RLS policy -- it is read
-- during login, before any tenant scope exists. This datum is tenant-owned:
-- every tenant must read its own row and only its own. It therefore belongs
-- where the same `tenant_id = current_setting('app.tenant_id')` policy as every
-- other tenant table can be applied to it.
--
-- **Why `config_id` is here.** A WeCom `userid` is meaningful only inside one
-- corp/app -- the same person has a different id under a different binding. A
-- table holding the id without the binding would hold a string with nowhere to
-- deliver it. The foreign key also means a binding cannot be removed while a
-- contact still points into it.
--
-- **No route writes this today, deliberately.** The frozen M1a route manifest
-- has no "set the manager" route; the natural home is the tenant's own
-- self-service surface (B1), and adding a route to a frozen list is a change to
-- the blueprint, not a change a batch may make on its own. Registered as T77.
-- Until then an operator seeds it. The shape will not change when the route
-- arrives: those four columns are what the route would write.

CREATE TABLE IF NOT EXISTS tenant_manager_contacts (
    tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
    channel text NOT NULL CHECK (channel IN ('wecom_proxy')),
    config_id text NOT NULL,
    external_user_id text NOT NULL CHECK (length(external_user_id) BETWEEN 1 AND 128),
    display_name text CHECK (display_name IS NULL OR length(display_name) <= 128),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid,
    FOREIGN KEY (tenant_id, config_id) REFERENCES channel_bindings(tenant_id, config_id)
);

COMMENT ON TABLE tenant_manager_contacts IS
'The one person at a merchant who is told when a conversation needs human hands.
One row per tenant by primary key, not by convention. Seeded by an operator in
M1a: the frozen route manifest has no route for it (T77, **ruled 2026-09-06 ->
ADR-0078**: M1a builds no configuration route; the deployment writes this table).';

COMMENT ON COLUMN tenant_manager_contacts.external_user_id IS
'The manager''s WeCom userid. Meaningful only inside the channel binding named by
config_id -- the same person carries a different id under a different binding,
which is why the two columns travel together.';

DO $rls$
BEGIN
    EXECUTE 'ALTER TABLE tenant_manager_contacts ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE tenant_manager_contacts FORCE ROW LEVEL SECURITY';
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'tenant_manager_contacts'
          AND policyname = 'tenant_manager_contacts_tenant_isolation'
    ) THEN
        EXECUTE 'CREATE POLICY tenant_manager_contacts_tenant_isolation '
             || 'ON tenant_manager_contacts USING '
             || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
             || 'WITH CHECK '
             || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)';
    END IF;
END
$rls$;

GRANT SELECT ON tenant_manager_contacts TO fastagent_worker;
GRANT SELECT, INSERT, UPDATE ON tenant_manager_contacts TO fastagent_app;

-- P3-1 (independent review 2026-09-04). The dispatcher now claims only rows
-- whose command names `audience = 'customer'`, and rows written before that
-- field existed have no `audience` key at all: `->>` returns NULL, the equality
-- is NULL, and they are never claimed **and** never dead-lettered
-- (`_dead_letter_exhausted` only touches SENDING). Stage has never run M1a so
-- production is unaffected; this line costs nothing and saves the afternoon
-- somebody would otherwise spend on "messages stuck, no error anywhere" in a
-- development database.
UPDATE outbox_messages
   SET command = command || '{"audience":"customer"}'::jsonb
 WHERE command->>'audience' IS NULL;

INSERT INTO schema_migrations(version) VALUES ('012_tenant_manager_contacts')
ON CONFLICT (version) DO NOTHING;
