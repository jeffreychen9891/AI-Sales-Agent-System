CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenants (
    id uuid PRIMARY KEY,
    code text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9-]{2,62}$'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employees (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    email text NOT NULL,
    password_hash text NOT NULL,
    permissions jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(permissions) = 'array'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    credential_version integer NOT NULL DEFAULT 1 CHECK (credential_version > 0),
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS employee_sessions (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    employee_id uuid NOT NULL,
    credential_version integer NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS profile_releases (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    profile_id text NOT NULL,
    version text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    profile jsonb NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, profile_id, version),
    UNIQUE (tenant_id, content_hash)
);

CREATE TABLE IF NOT EXISTS channel_bindings (
    config_id text PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    profile_release_id uuid NOT NULL,
    source text NOT NULL DEFAULT 'wecom_proxy',
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    current_token_hash text NOT NULL CHECK (current_token_hash ~ '^[0-9a-f]{64}$'),
    current_valid_from timestamptz NOT NULL DEFAULT now(),
    next_token_hash text CHECK (next_token_hash IS NULL OR next_token_hash ~ '^[0-9a-f]{64}$'),
    next_valid_from timestamptz,
    overlap_ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, config_id),
    FOREIGN KEY (tenant_id, profile_release_id) REFERENCES profile_releases(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS ingress_replay_keys (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    config_id text NOT NULL,
    credential_fingerprint text NOT NULL,
    replay_key_hash text NOT NULL CHECK (replay_key_hash ~ '^[0-9a-f]{64}$'),
    external_event_id text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, config_id, credential_fingerprint, replay_key_hash),
    FOREIGN KEY (tenant_id, config_id) REFERENCES channel_bindings(tenant_id, config_id)
);

CREATE TABLE IF NOT EXISTS conversation_aggregates (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    state_version bigint NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    lifecycle text NOT NULL DEFAULT 'open' CHECK (lifecycle IN ('open', 'closing', 'closed')),
    control_mode text NOT NULL DEFAULT 'ai' CHECK (control_mode IN ('ai', 'human', 'paused', 'blocked')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    profile_release_id uuid NOT NULL,
    target_stack jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(target_stack) = 'array'),
    state jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(state) = 'object'),
    last_inbound_at timestamptz,
    last_outbound_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, profile_release_id) REFERENCES profile_releases(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS inbox_events (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    event_id uuid NOT NULL,
    source text NOT NULL,
    external_event_id text NOT NULL,
    conversation_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'customer_message', 'channel_event', 'timer', 'business_event',
        'delivery_receipt', 'internal'
    )),
    profile_release_id uuid NOT NULL,
    aggregate_generation bigint,
    envelope jsonb NOT NULL CHECK (jsonb_typeof(envelope) = 'object'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'processing', 'consumed', 'stale', 'failed'
    )),
    lease_owner text,
    lease_expires_at timestamptz,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, event_id),
    UNIQUE (tenant_id, source, external_event_id),
    FOREIGN KEY (tenant_id, profile_release_id) REFERENCES profile_releases(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS inbox_pending_idx
    ON inbox_events (tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS scheduled_events (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'silence_check', 'goal_stall', 'followup_due', 'customer_promise_due',
        'dormant_reengagement', 'business_event_followup', 'ai_commitment_due'
    )),
    dedupe_key text NOT NULL,
    profile_release_id uuid NOT NULL,
    aggregate_generation bigint NOT NULL CHECK (aggregate_generation >= 0),
    target_frame_id uuid NOT NULL,
    target_stack_version bigint NOT NULL CHECK (target_stack_version >= 0),
    commitment_id uuid,
    business_event_id uuid,
    payload_ref jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload_ref) = 'object'),
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'CLAIMED', 'ENQUEUED', 'CONSUMED', 'INVALIDATED', 'CANCELLED', 'FAILED'
    )),
    lease_owner text,
    lease_expires_at timestamptz,
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    timer_envelope_id uuid,
    invalidated_at timestamptz,
    invalidation_reason text,
    invalidated_by_event_id uuid,
    created_by_transition_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, conversation_id, aggregate_generation, dedupe_key),
    UNIQUE (tenant_id, timer_envelope_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id),
    FOREIGN KEY (tenant_id, profile_release_id) REFERENCES profile_releases(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS scheduled_due_idx
    ON scheduled_events (status, due_at) WHERE status IN ('PENDING', 'CLAIMED');

CREATE TABLE IF NOT EXISTS intent_records (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'send_message', 'send_material', 'remember', 'schedule_event', 'cancel_event',
        'reschedule_event', 'quote', 'create_order', 'update_order', 'followup',
        'handoff', 'manual_task'
    )),
    status text NOT NULL CHECK (status IN (
        'PROPOSED', 'APPROVED', 'EXECUTING', 'APPLIED', 'DENIED', 'UNKNOWN',
        'COMPENSATING', 'COMPENSATED', 'FAILED'
    )),
    proposal jsonb NOT NULL CHECK (jsonb_typeof(proposal) = 'object'),
    proposed_by jsonb NOT NULL CHECK (jsonb_typeof(proposed_by) = 'object'),
    strategy_id text NOT NULL,
    target_frame_id uuid NOT NULL,
    idempotency_key text NOT NULL,
    preconditions jsonb NOT NULL CHECK (jsonb_typeof(preconditions) = 'object'),
    policy_evidence jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(policy_evidence) = 'array'),
    execution_token text,
    lease_expires_at timestamptz,
    external_operation_ref text,
    result jsonb,
    inverse jsonb,
    unknown_reason text,
    manual_task_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS policy_decisions (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    intent_id uuid,
    hook text NOT NULL CHECK (hook IN (
        'pre_inference', 'post_understanding', 'post_plan', 'pre_generation',
        'pre_execute', 'pre_delivery'
    )),
    effect text NOT NULL CHECK (effect IN ('allow', 'deny', 'modify', 'manual_review')),
    reason_code text NOT NULL,
    modifications jsonb NOT NULL DEFAULT '{}'::jsonb,
    obligations jsonb NOT NULL DEFAULT '[]'::jsonb,
    policy_id text NOT NULL,
    policy_version text NOT NULL,
    context jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id),
    FOREIGN KEY (tenant_id, intent_id) REFERENCES intent_records(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS outbox_messages (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    command jsonb NOT NULL CHECK (jsonb_typeof(command) = 'object'),
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'SENDING', 'DELIVERED', 'FAILED', 'UNKNOWN'
    )),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_owner text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, intent_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id),
    FOREIGN KEY (tenant_id, intent_id) REFERENCES intent_records(tenant_id, id)
);

CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox_messages (status, created_at) WHERE status IN ('PENDING', 'SENDING');

CREATE TABLE IF NOT EXISTS delivery_receipts (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    intent_id uuid NOT NULL,
    outbox_id uuid NOT NULL,
    provider_operation_id text,
    status text NOT NULL CHECK (status IN ('accepted', 'delivered', 'failed', 'unknown')),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, outbox_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id),
    FOREIGN KEY (tenant_id, intent_id) REFERENCES intent_records(tenant_id, id),
    FOREIGN KEY (tenant_id, outbox_id) REFERENCES outbox_messages(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS ws_tickets (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    token_hash text NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    employee_id uuid NOT NULL,
    session_id uuid NOT NULL,
    credential_version integer NOT NULL,
    origin text NOT NULL,
    topics jsonb NOT NULL CHECK (jsonb_typeof(topics) = 'array'),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    connection_nonce text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (token_hash),
    FOREIGN KEY (tenant_id, employee_id) REFERENCES employees(tenant_id, id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES employee_sessions(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    correlation_id uuid NOT NULL,
    reason_code text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

DO $rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'employees', 'employee_sessions', 'profile_releases', 'channel_bindings',
        'ingress_replay_keys', 'conversation_aggregates', 'inbox_events',
        'scheduled_events', 'intent_records', 'policy_decisions', 'outbox_messages',
        'delivery_receipts', 'ws_tickets', 'audit_events'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = table_name
              AND policyname = table_name || '_tenant_isolation'
        ) THEN
            EXECUTE format(
                'CREATE POLICY %I ON %I USING '
                || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) '
                || 'WITH CHECK '
                || '(tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)',
                table_name || '_tenant_isolation', table_name
            );
        END IF;
    END LOOP;
END
$rls$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DO $tenant_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'tenants'
           AND policyname = 'tenants_tenant_isolation'
    ) THEN
        CREATE POLICY tenants_tenant_isolation ON tenants
            USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$tenant_rls$;

CREATE OR REPLACE FUNCTION resolve_login_tenant(candidate_code text)
RETURNS TABLE (id uuid, status text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT tenant.id, tenant.status
      FROM public.tenants AS tenant
     WHERE tenant.code = candidate_code
$function$;

CREATE OR REPLACE FUNCTION list_active_tenant_ids()
RETURNS TABLE (id uuid)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT tenant.id FROM public.tenants AS tenant
     WHERE tenant.status = 'active' ORDER BY tenant.id
$function$;

CREATE OR REPLACE FUNCTION resolve_channel_binding(candidate_config_id text)
RETURNS TABLE (
    config_id text,
    tenant_id uuid,
    profile_release_id uuid,
    source text,
    status text,
    current_token_hash text,
    current_valid_from timestamptz,
    next_token_hash text,
    next_valid_from timestamptz,
    overlap_ends_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    SELECT binding.config_id, binding.tenant_id, binding.profile_release_id,
           binding.source, binding.status, binding.current_token_hash,
           binding.current_valid_from, binding.next_token_hash,
           binding.next_valid_from, binding.overlap_ends_at
      FROM public.channel_bindings AS binding
     WHERE binding.config_id = candidate_config_id
$function$;

REVOKE ALL ON FUNCTION resolve_channel_binding(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION resolve_login_tenant(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION list_active_tenant_ids() FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public
TO fastagent_app, fastagent_ingress, fastagent_worker, fastagent_scheduler;
GRANT SELECT ON tenants TO fastagent_app, fastagent_worker;
GRANT EXECUTE ON FUNCTION resolve_login_tenant(text) TO fastagent_app;
GRANT EXECUTE ON FUNCTION list_active_tenant_ids() TO fastagent_worker, fastagent_scheduler;
GRANT SELECT ON employees TO fastagent_app;
GRANT SELECT, INSERT, UPDATE ON employee_sessions, ws_tickets TO fastagent_app;
GRANT SELECT ON conversation_aggregates TO fastagent_app;
GRANT EXECUTE ON FUNCTION resolve_channel_binding(text) TO fastagent_ingress;
GRANT SELECT, INSERT ON inbox_events, ingress_replay_keys TO fastagent_ingress;
GRANT SELECT ON profile_releases TO fastagent_worker;
GRANT SELECT, INSERT, UPDATE ON
    conversation_aggregates, inbox_events, scheduled_events,
    intent_records, policy_decisions, outbox_messages, delivery_receipts
TO fastagent_worker;
GRANT SELECT, INSERT, UPDATE ON inbox_events, scheduled_events TO fastagent_scheduler;
GRANT INSERT ON audit_events
TO fastagent_app, fastagent_ingress, fastagent_worker, fastagent_scheduler;

INSERT INTO schema_migrations(version) VALUES ('001_m0')
ON CONFLICT (version) DO NOTHING;
