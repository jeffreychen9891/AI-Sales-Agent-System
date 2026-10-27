-- Correct the non-provable input estimate documented by migration 002 without
-- rewriting already-applied migration history.

COMMENT ON COLUMN model_invocation_costs.estimated_input_tokens IS
'Pre-call hard upper-bound reservation. The complete serialized model request has a
hard input byte limit; the ledger reserves one token per serialized UTF-8 byte for the
entire contract limit plus a fixed provider-framing allowance. This is intentionally
independent of CJK/emoji/tokenizer heuristics. Provider usage is stored only after a
complete valid measurement arrives in the response.';

INSERT INTO schema_migrations(version) VALUES ('004_m1a_cost_input_contract')
ON CONFLICT (version) DO NOTHING;
