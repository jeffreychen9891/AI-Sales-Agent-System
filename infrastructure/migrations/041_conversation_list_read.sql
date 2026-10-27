-- 041: the two columns `GET /conversations` needs and did not have
-- (route freeze B3 #1, `C0B-001` + `029`; read model = parity §6.2 第一条).
--
-- **Nine fields, and eight of them were already readable.** The list is
-- conversation id, contact summary, last message summary/time, unread count,
-- control mode, target summary, next scheduled event summary, manual task
-- count, projection version. Everything but the seventh reads tables this role
-- already holds `SELECT` on -- `conversation_aggregates` (001), `contacts`
-- (040), `inbox_events` / `outbox_messages` (033), `employee_read_cursors`
-- (038), `manual_tasks` (035).
--
-- **`scheduled_events` is the exception, and only by two columns.** `007` gave
-- `fastagent_app` a column-level `SELECT (tenant_id, conversation_id, status)`
-- -- exactly what a repin needed to invalidate timers, and not one column more.
-- The list needs to say *what* is going to happen and *when*, so it needs
-- `kind` and `due_at`. Everything else on that table stays out of reach:
-- `payload_ref`, `dedupe_key`, the frame and generation bookkeeping, the
-- commitment and business-event links. A summary is what the read model asks
-- for; the rest is the scheduler's working state, and the control plane has no
-- business reading it.
GRANT SELECT (kind, due_at) ON scheduled_events TO fastagent_app;

-- **The list orders by the conversation's last message**, so it asks the same
-- union `GET /conversations/{id}/messages` pages -- once per conversation on
-- the page, through a lateral join. These two indexes are what keep that from
-- being a scan per row; `033` added the same pair for the message page's own
-- `(tenant_id, conversation_id, created_at, id)` ordering, and this is the
-- other direction (newest first).
CREATE INDEX IF NOT EXISTS inbox_events_recent_idx
    ON inbox_events (tenant_id, conversation_id, created_at DESC, id DESC)
    WHERE event_type = 'customer_message';
CREATE INDEX IF NOT EXISTS outbox_messages_recent_idx
    ON outbox_messages (tenant_id, conversation_id, created_at DESC, id DESC);

INSERT INTO schema_migrations(version) VALUES ('041_conversation_list_read')
ON CONFLICT (version) DO NOTHING;
