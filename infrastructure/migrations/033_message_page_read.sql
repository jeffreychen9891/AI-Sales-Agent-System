-- 033: the two tables the message page reads, and only those two.
--
-- **`fastagent_app` had no privilege on either.** `001` gave `inbox_events` to
-- ingress, scheduler and worker, and `outbox_messages` to worker -- the three
-- roles that move messages. Nothing reads them back for a person, because until
-- B3 #3 no route did.
--
-- **`SELECT` only, and that is the whole grant.** The control plane shows this
-- conversation; it does not re-queue, re-send or edit anything. An `UPDATE` here
-- would let a read route change what the engine is about to do.
--
-- **`delivery_receipts` is deliberately not granted.** parity §6.2 asks the
-- message page for "delivery 状态", and `outbox_messages.status` is that status
-- (`PENDING / SENDING / DELIVERED / FAILED / UNKNOWN`). The receipts table holds
-- the provider's own per-attempt record, which is a different question -- one no
-- route asks yet. `019`'s reasoning applies: a grant this route does not need is
-- a grant the next route inherits.
--
-- **RLS needs no work.** Both tables carry `tenant_id` and both are in `001`'s
-- isolation loop, so the same policy that protects them from the worker's
-- mistakes protects them from this route's; the transaction is opened on the
-- caller's own tenant, which is where a conversation read belongs.
--
-- **What this grant exposes, said plainly**: `inbox_events.envelope` is the
-- customer's message as it arrived. That is the point of the route, and it is
-- why the projection runs `redact()` over the content before it reaches the
-- wire (parity §6.2's 递归脱敏) rather than trusting the column to be harmless.

GRANT SELECT ON inbox_events, outbox_messages TO fastagent_app;

-- **The index the page reads by** (independent review P3-1). This is the first
-- paginated read in the repository, and neither table had an index that could
-- serve it: `inbox_events` is indexed on `(tenant_id, status, created_at)` for
-- the worker's claim loop, and `outbox_messages` on `(status, created_at)` for
-- the dispatcher plus `016`'s partial index for manager summaries. A page
-- would have scanned both tables and sorted, and page *n* would have redone
-- it.
--
-- The column order is the `ORDER BY` and the cursor comparison, in that order:
-- `(created_at, id)` is the total order the cursor walks, and the two equality
-- columns come first.
CREATE INDEX IF NOT EXISTS inbox_events_conversation_page_idx
    ON inbox_events (tenant_id, conversation_id, created_at, id)
    WHERE event_type = 'customer_message';
CREATE INDEX IF NOT EXISTS outbox_messages_conversation_page_idx
    ON outbox_messages (tenant_id, conversation_id, created_at, id);
--
-- **The inbound one is partial and the outbound one is not**, and the asymmetry
-- is deliberate: `event_type = 'customer_message'` is a fixed predicate this
-- route always applies, while the outbound filter is
-- `coalesce(command->>'audience', 'customer') = 'customer'` -- an expression
-- over a JSON key that older rows do not carry. A partial index on that would
-- have to repeat the `coalesce`, and an index whose predicate must be restated
-- exactly to be used is one the next query silently misses.

INSERT INTO schema_migrations(version) VALUES ('033_message_page_read')
ON CONFLICT (version) DO NOTHING;
