-- M1a: model invocation cost ledger and per-tenant ceiling overrides.
--
-- Two honesty rules are encoded here rather than left to the reader:
--   * an unpriced call stores NULL cost, never 0, so a ceiling can never look
--     satisfied because the price was missing;
--   * the aggregation views count unpriced calls separately, so a total that
--     omits them is never mistaken for a complete total.

CREATE TABLE IF NOT EXISTS tenant_cost_ceilings (
    tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
    soft_daily_cny numeric(12, 4) CHECK (soft_daily_cny > 0),
    hard_daily_cny numeric(12, 4) CHECK (hard_daily_cny > 0),
    soft_monthly_cny numeric(12, 4) CHECK (soft_monthly_cny > 0),
    hard_monthly_cny numeric(12, 4) CHECK (hard_monthly_cny > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE tenant_cost_ceilings IS
'Per-tenant RMB ceiling overrides. NULL means "inherit the model policy release default".
An override may only tighten a platform ceiling, never exceed it. That rule spans two
sources (this table and the release file) so it is enforced in the application layer,
not by a table constraint; see tests/contracts/test_cost_ledger.py.';

CREATE TABLE IF NOT EXISTS model_invocation_costs (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN (
        'UNDERSTAND_VALUE', 'PLAN_CAPABILITY', 'GENERATE_VALUE',
        'COMMITMENT_CAPABILITY', 'VISION'
    )),
    provider_id text NOT NULL,
    model_id text NOT NULL,
    release_version text NOT NULL,
    release_content_hash text NOT NULL,
    prompt_release text NOT NULL,
    status text NOT NULL CHECK (status IN ('estimated', 'settled', 'failed')),
    estimated_input_tokens integer NOT NULL CHECK (estimated_input_tokens >= 0),
    estimated_output_tokens integer NOT NULL CHECK (estimated_output_tokens >= 0),
    estimated_cost_cny numeric(12, 6) CHECK (estimated_cost_cny >= 0),
    input_tokens integer CHECK (input_tokens >= 0),
    output_tokens integer CHECK (output_tokens >= 0),
    cost_cny numeric(12, 6) CHECK (cost_cny >= 0),
    price_input_snapshot numeric(12, 6) CHECK (price_input_snapshot >= 0),
    price_output_snapshot numeric(12, 6) CHECK (price_output_snapshot >= 0),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES conversation_aggregates(tenant_id, id)
);

COMMENT ON TABLE model_invocation_costs IS
'One row per model call. The row is written before the call with an estimate and settled
afterwards with measured usage, so a call that never returns still leaves a trace.';

COMMENT ON COLUMN model_invocation_costs.estimated_input_tokens IS
'Pre-call estimate of prompt tokens. Measured locally from the prompt text by a
deliberately conservative heuristic (CJK characters count as one token each, other runs
as one token per three characters, rounded up). It is an estimate, not the provider''s
count: the provider''s number arrives only in the response and is stored in input_tokens.';

COMMENT ON COLUMN model_invocation_costs.estimated_output_tokens IS
'Pre-call upper bound of completion tokens, taken from the role binding max_output_tokens
in the model policy release. The bound is used rather than a guess so that the hard
ceiling blocks early: over-blocking is recoverable, over-spending is not.';

COMMENT ON COLUMN model_invocation_costs.cost_cny IS
'Measured RMB cost, or NULL when the release carries no price for this role. NULL is
required: storing 0 would make an unmeasured call look free and a ceiling look satisfied.';

COMMENT ON COLUMN model_invocation_costs.price_input_snapshot IS
'RMB per million input tokens as declared by the release at call time. Snapshotted so a
later price change does not rewrite history.';

CREATE INDEX IF NOT EXISTS model_invocation_costs_tenant_time_idx
    ON model_invocation_costs (tenant_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS model_invocation_costs_conversation_idx
    ON model_invocation_costs (tenant_id, conversation_id);

-- Daily and monthly rollups. security_invoker keeps the caller's RLS in force:
-- without it the view would run as its owner and leak across tenants.
CREATE OR REPLACE VIEW model_cost_daily WITH (security_invoker = true) AS
SELECT
    tenant_id,
    (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS cost_date,
    role,
    model_id,
    release_version,
    count(*) AS invocations,
    count(*) FILTER (WHERE cost_cny IS NULL) AS unpriced_invocations,
    coalesce(sum(cost_cny), 0) AS priced_cost_cny,
    coalesce(sum(input_tokens), 0) AS input_tokens,
    coalesce(sum(output_tokens), 0) AS output_tokens
FROM model_invocation_costs
WHERE status = 'settled'
GROUP BY tenant_id, (occurred_at AT TIME ZONE 'Asia/Shanghai')::date,
         role, model_id, release_version;

COMMENT ON VIEW model_cost_daily IS
'Daily rollup in Asia/Shanghai. priced_cost_cny sums only rows that carry a price;
unpriced_invocations must be read alongside it, otherwise the total understates spend.';

CREATE OR REPLACE VIEW model_cost_monthly WITH (security_invoker = true) AS
SELECT
    tenant_id,
    to_char(occurred_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM') AS cost_month,
    count(*) AS invocations,
    count(*) FILTER (WHERE cost_cny IS NULL) AS unpriced_invocations,
    coalesce(sum(cost_cny), 0) AS priced_cost_cny
FROM model_invocation_costs
WHERE status = 'settled'
GROUP BY tenant_id, to_char(occurred_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM');

COMMENT ON VIEW model_cost_monthly IS
'Monthly rollup in Asia/Shanghai. Same caveat as model_cost_daily: unpriced_invocations
is part of the answer, not a footnote.';

DO $rls$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['tenant_cost_ceilings', 'model_invocation_costs']
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

GRANT SELECT, INSERT, UPDATE ON model_invocation_costs TO fastagent_worker;
GRANT SELECT ON model_invocation_costs TO fastagent_app;
GRANT SELECT ON model_cost_daily, model_cost_monthly TO fastagent_app, fastagent_worker;
GRANT SELECT ON tenant_cost_ceilings TO fastagent_app, fastagent_worker;
GRANT INSERT, UPDATE ON tenant_cost_ceilings TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('002_m1a_cost')
ON CONFLICT (version) DO NOTHING;
