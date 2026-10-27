-- 044: the outbox row remembers where it stood in its turn (T109, ADR-0095 §二).
--
-- **The order two messages reach a customer in was decided by `uuid4()`.**
-- `created_at` defaults to `now()`, and PostgreSQL's `now()` is the *transaction*
-- timestamp -- so two rows written by one turn carry **identical** values (the
-- independent review measured 8/8). The dispatcher's `ORDER BY created_at, id`
-- then fell through to the random half of the key: 8 runs, 5 of them reversed.
-- What the model wrote as "here is the answer, and one more thing" arrived the
-- other way round about half the time.
--
-- **A column, not a field in `command`** (ADR-0095 §二): an index can carry a
-- column, and outbox claiming is a hot path -- ordering by a JSON expression
-- would need an expression index and a matching `ORDER BY` spelled the same way
-- in two places.
--
-- **`DEFAULT 0` and `NOT NULL`, and both halves matter.** Rows written before
-- this migration have no position to recover -- the information was never
-- stored -- so they all take 0 and keep sorting by `created_at, id` among
-- themselves, exactly as they do today. Nothing is invented for them. New rows
-- get their real position from the writer.
--
-- **This does not make delivery order safe on its own**, and the condition is
-- narrower than "one dispatcher" (independent review 2026-09-08, P2-2).
--
-- What it makes deterministic is the **claim** order. Delivery follows it only
-- **when every row sends successfully the first time**. The claim predicate is
-- `PENDING OR (SENDING AND lease_expires_at < now())`, so a row that is being
-- sent and still holds its lease is skipped -- and that is reachable in a
-- *single* process: ordinal 0 raises mid-send (the CLI logs and continues), it
-- stays `SENDING` for the rest of its lease, the next pass claims ordinal 1 and
-- sends it, and ordinal 0 goes out when the lease expires. The customer reads
-- the second sentence first and the first one thirty seconds later.
--
-- Ordering retries is new semantics and needs its own ruling, so it is
-- registered (**T211**) rather than improvised here. Two dispatchers interleave
-- for a different reason -- that is the ceiling ADR-0095 §二 accepted, carried
-- in **T196** and now also on the `worker:` block in the Stage compose file,
-- because the action that trips it is `--scale worker=2` and the person doing
-- that reads compose, not a backlog file.

ALTER TABLE outbox_messages
    ADD COLUMN IF NOT EXISTS ordinal integer NOT NULL DEFAULT 0 CHECK (ordinal >= 0);

COMMENT ON COLUMN outbox_messages.ordinal IS
    'Position of this message within the turn that produced it (T109). Rows '
    'written by one transaction share created_at, so this is the only tiebreak '
    'that carries intent rather than accident. 0 for rows that are alone in '
    'their turn and for every row written before migration 044.';

-- The claim predicate reads (tenant_id, status, audience) and now orders by
-- (created_at, ordinal, id). The pre-existing `outbox_pending_idx` is keyed on
-- (status, created_at) without the tenant, so it cannot serve the ordering
-- without a sort; this one can, and stays partial for the same reason that one
-- is -- the table is mostly rows in terminal states that the dispatcher never
-- looks at again.
CREATE INDEX IF NOT EXISTS outbox_claim_order_idx
    ON outbox_messages (tenant_id, created_at, ordinal, id)
    WHERE status IN ('PENDING', 'SENDING');

INSERT INTO schema_migrations(version) VALUES ('044_outbox_ordinal')
ON CONFLICT (version) DO NOTHING;
