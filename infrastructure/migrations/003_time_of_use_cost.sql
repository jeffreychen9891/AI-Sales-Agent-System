-- Time-of-use billing evidence. Estimates use peak prices; settlement records
-- the price period at the original call instant and measured cache usage.

ALTER TABLE model_invocation_costs
    ADD COLUMN IF NOT EXISTS cached_input_tokens integer CHECK (cached_input_tokens >= 0),
    ADD COLUMN IF NOT EXISTS price_cached_input_snapshot numeric(12, 6)
        CHECK (price_cached_input_snapshot >= 0),
    ADD COLUMN IF NOT EXISTS pricing_period text CHECK (pricing_period IN ('flat', 'offpeak', 'peak'));

COMMENT ON COLUMN model_invocation_costs.cached_input_tokens IS
'Provider-reported prompt cache-hit tokens. They are settled at the release cache-hit rate.';

COMMENT ON COLUMN model_invocation_costs.pricing_period IS
'Supplier price period selected from occurred_at, never from settlement time. Pre-call estimates always use peak.';

INSERT INTO schema_migrations(version) VALUES ('003_time_of_use_cost')
ON CONFLICT (version) DO NOTHING;
