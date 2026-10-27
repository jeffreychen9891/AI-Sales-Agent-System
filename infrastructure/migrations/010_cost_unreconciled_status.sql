-- T55. A fourth ledger state for money we cannot account for.
--
-- Four exits in `OpenAICompatibleChatClient` leave a reservation in `estimated`
-- and never return to it: a timeout, a transport exception, an HTTP >= 400 whose
-- body carries no usable `usage`, and a malformed response with none. Its own
-- comment says the estimate stays reserved "until reconciliation" -- and there
-- was no reconciliation, anywhere in the repository.
--
-- **Why a new state rather than reusing the two that exist** (user decision
-- 2026-09-03, option b3 of three that were measured and reported):
--
--   * `failed` would say *the supplier did not bill us*. We do not know that,
--     and asserting it releases the reservation -- which silences the only
--     signal that anything is wrong.
--   * `settled` would say *the supplier billed us this much*. We do not know
--     that either, and it would put an unproven number into settled spend,
--     which ADR-0034 boundary 1 forbids outright.
--   * `unreconciled` says *we do not know*. That one is true.
--
-- Asking the supplier is not an option today and this is measured, not assumed:
-- usage is read exactly once, from the response body of the call itself
-- (`providers.py:379`), and the ledger stores **no supplier-side identifier at
-- all** -- no request id, no trace id. We could not name the call we wanted to
-- ask about. Adding such an identifier is a prerequisite for any real
-- compensating reconciliation and is registered separately.

ALTER TABLE model_invocation_costs
    DROP CONSTRAINT IF EXISTS model_invocation_costs_status_check;

ALTER TABLE model_invocation_costs
    ADD CONSTRAINT model_invocation_costs_status_check
    CHECK (status IN ('estimated', 'settled', 'failed', 'unreconciled'));

-- When we gave up, as distinct from `settled_at`, which means the supplier told
-- us what it cost. A row that has one and not the other is unambiguous about
-- which of the two happened.
ALTER TABLE model_invocation_costs
    ADD COLUMN IF NOT EXISTS reconciled_at timestamptz;

COMMENT ON COLUMN model_invocation_costs.status IS
'estimated: the call is in flight, or left and has not yet been aged out.
settled: the supplier reported usage and cost_cny is measured.
failed: something marked this reservation as never having been charged. Note that
nothing in production writes this today.
unreconciled: the call left and never came back, and no reconciliation is
possible because the ledger holds no supplier-side identifier. The amount is
unknown and is deliberately excluded from both settled spend and in-flight
counts -- it is counted on its own, because "we do not know" is a different
operational condition from either "in flight" or "charged".';

COMMENT ON COLUMN model_invocation_costs.reconciled_at IS
'When this row was aged out of estimated into unreconciled. Distinct from
settled_at: settled_at means the supplier answered, reconciled_at means we
stopped waiting.';

INSERT INTO schema_migrations(version) VALUES ('010_cost_unreconciled_status')
ON CONFLICT (version) DO NOTHING;
