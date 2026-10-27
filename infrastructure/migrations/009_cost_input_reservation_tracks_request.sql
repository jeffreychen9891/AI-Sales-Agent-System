-- T43. Migration 004 corrected this column's comment once already, and T43 made
-- that correction stale: the ledger no longer reserves the entire contract
-- limit for every call, it reserves the request's own serialized size.
--
-- Following 004's own precedent, already-applied migration history is not
-- rewritten; the comment is corrected by a new migration instead.
--
-- Why this is worth a migration rather than a code comment (independent review
-- 2026-09-03, P2-3): the database's own documentation outlives the code that
-- wrote it, and the person reading `\d+ model_invocation_costs` has no way to
-- know the sentence describes behaviour that was replaced. The contract test
-- that guards this text only grepped two substrings and could not see a
-- semantic inversion -- the same shape as the assertion that let this batch's
-- own probe pass while doing nothing.

COMMENT ON COLUMN model_invocation_costs.estimated_input_tokens IS
'Pre-call hard upper-bound reservation for this request. The complete serialized model
request has a hard input byte limit, and the ledger reserves one token per serialized
UTF-8 byte of the request itself plus a fixed provider-framing allowance. One token
covers at least one input byte, so this cannot under-reserve; it is intentionally
independent of CJK/emoji/tokenizer heuristics. It previously reserved the entire
contract limit regardless of request size, which over-reserved a measured call by about
11.7x and is why ADR-0034 moved ceiling enforcement onto settled cost. Provider usage is
stored only after a complete valid measurement arrives in the response.';

INSERT INTO schema_migrations(version) VALUES ('009_cost_input_reservation_tracks_request')
ON CONFLICT (version) DO NOTHING;
