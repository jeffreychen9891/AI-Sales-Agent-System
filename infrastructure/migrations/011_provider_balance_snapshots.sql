-- T71 / ADR-0041. Daily account-balance snapshots, and what each one was
-- compared against.
--
-- The supplier gives us exactly one number we can check ourselves: the account
-- balance (`/user/balance`). It cannot say which call is wrong -- it is an
-- aggregate -- but it covers **every** call, including the two that no per-call
-- scheme can ever see because they produce no response at all: timeouts and
-- transport failures. ADR-0040 named this the only remaining route and left it
-- to T71; ADR-0041 decided the shape: one snapshot a day, aligned to the
-- ledger's own Asia/Shanghai day boundary, **alerting only, never a breaker**.
--
-- **No `tenant_id`, and that is not an oversight.** A balance is an account
-- fact, not a tenant fact: it is the sum of everything the account spent, and
-- v2 owns the account outright (user confirmed 2026-09-03). So this table holds
-- no tenant-owned data, carries no RLS policy, and is granted to no application
-- role -- the daily job and the metrics collector both read it as the database
-- owner from inside the Stage postgres container, the same way every other
-- platform-wide operational number is already read
-- (`scripts/stage_observability.py`). Nothing here widens what `fastagent_app`
-- or `fastagent_worker` can see, which is the property to preserve: a
-- cross-tenant sum must not become reachable from a tenant-scoped role.
--
-- **Every column that could carry a credential or a response body is absent by
-- design.** `reason` holds the same vocabulary the delivery receipts use --
-- `adapter` / `http_status` / `reason`, never the body (`channel.py`, and
-- ADR-0041 §四.3). The balance response is account-level money; it is allowed to
-- decide a verdict and it is not allowed into a log line.

-- **Amended in place on 2026-09-04** (the fix round for this batch's own
-- independent review): `unreconciled_rows` and `unreconciled_estimate_cny` were
-- added below. `scripts/migrate.py` records only the *version* -- there is no
-- checksum anywhere -- so a database that already applied the earlier version of
-- this file will skip it for ever and then fail on the missing columns. Stage
-- has never applied it (it is still at `001_m0`), so production is unaffected;
-- **a local database that ran the earlier version must be recreated or
-- hand-altered** (re-review 2026-09-04, R4).

CREATE TABLE IF NOT EXISTS provider_balance_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id text NOT NULL,
    taken_at timestamptz NOT NULL DEFAULT now(),

    -- What the supplier said. NULL when it did not answer with a usable CNY
    -- balance -- which is a fact worth storing, not an error to swallow.
    balance_cny numeric(14, 6),
    http_status integer,
    reason text,

    -- What this snapshot was compared against. All NULL on the first snapshot:
    -- a delta needs two points, and the first one honestly has nothing to say.
    previous_snapshot_id uuid REFERENCES provider_balance_snapshots(id),
    ledger_date date,
    balance_delta_cny numeric(14, 6),
    ledger_settled_cny numeric(14, 6),
    difference_cny numeric(14, 6),
    tolerance_cny numeric(14, 6),

    -- How much of the difference is already accounted for as "we do not know".
    -- ADR-0040 moves abandoned reservations to `unreconciled`, and the supplier
    -- very likely did charge for them -- they are excluded from every day's
    -- settled sum, for ever. So they are a *guaranteed* contributor to
    -- `difference_cny`, and without them beside it an operator cannot tell
    -- "a call we failed to record" from "the backlog we already know about"
    -- (independent review 2026-09-04, T71 F8).
    unreconciled_rows integer,
    unreconciled_estimate_cny numeric(14, 6),

    verdict text NOT NULL CHECK (
        verdict IN ('matched', 'over_tolerance', 'incomparable', 'unavailable')
    )
);

CREATE INDEX IF NOT EXISTS provider_balance_snapshots_provider_time_idx
    ON provider_balance_snapshots (provider_id, taken_at DESC);

COMMENT ON TABLE provider_balance_snapshots IS
'One row per daily balance reading, plus the comparison it supported. Account-level,
not tenant-level: no tenant_id, no RLS policy, no grant to any application role.
Read and written by the Stage daily job and the metrics collector as the database
owner, the same context every other platform-wide operational number already uses.';

COMMENT ON COLUMN provider_balance_snapshots.balance_delta_cny IS
'previous.balance_cny - this.balance_cny: what the supplier says was spent between
the two readings. Negative means the balance went *up*, which spending cannot do --
a top-up or a grant landed in the window, and the day is not comparable.';

COMMENT ON COLUMN provider_balance_snapshots.ledger_settled_cny IS
'sum(cost_cny) over model_invocation_costs for ledger_date, status = settled, across
all tenants. Deliberately settled-only: estimated is not yet a cost and unreconciled
is money we have already said we cannot account for (ADR-0040), so including either
would make the comparison assert something the ledger itself refuses to assert.';

COMMENT ON COLUMN provider_balance_snapshots.difference_cny IS
'balance_delta_cny - ledger_settled_cny. Positive means the supplier charged more
than the ledger recorded. Read it with unreconciled_rows beside it: a call the
reconciler aged into `unreconciled` was written down, just not as `settled`, and
it contributes here permanently. So a positive difference has at least three
possible causes -- a call never recorded, the known unreconciled backlog, and an
expiring grant -- and only the second one is visible from this row alone.';

COMMENT ON COLUMN provider_balance_snapshots.verdict IS
'matched: |difference| <= tolerance.
over_tolerance: |difference| > tolerance. This is the alert condition, and it is
only ever an alert -- ADR-0041 refuses to let an aggregate signal stop a single
conversation.
incomparable: a balance was read but no comparison was possible (first snapshot,
missing predecessor, or the balance went up).
unavailable: the supplier did not answer with a usable balance. Distinct from
incomparable on purpose: one means we could not ask, the other means the answer
could not be used.';

COMMENT ON COLUMN provider_balance_snapshots.reason IS
'Short reason code, same vocabulary as delivery receipts (adapter / http_status /
reason). Never a response body: the balance response carries account-level money.';

INSERT INTO schema_migrations(version) VALUES ('011_provider_balance_snapshots')
ON CONFLICT (version) DO NOTHING;
