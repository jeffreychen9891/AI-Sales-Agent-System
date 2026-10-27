-- 048: an outbox row can end without having been sent (B4-4, ADR-0098 §一).
--
-- **The `pre_delivery` half of ProactivityPolicy needs an ending that is not a
-- failure.** ADR-0098 §一 rules that the dispatcher re-checks a proactive
-- message against one instant carried in its `command` and 「到点即作废」. The
-- statuses this table had -- PENDING / SENDING / DELIVERED / FAILED / UNKNOWN --
-- offer no word for that.
--
-- **Reusing FAILED would have been wrong twice.** `dispatch_one` turns FAILED
-- into a manual task (`DELIVERY_MANUAL_TASK_REASONS`), so every expired window
-- would put a person in front of a message the policy deliberately chose not to
-- send: a queue manufacturing work out of its own rules. And a receipt says what
-- a provider answered -- `delivery_receipts.status` has four values and every one
-- of them means the provider was asked. Nothing was asked here.
--
-- **RFC §7.1 already allows this**: 「deadline 过期只能在未开始副作用前取消；副作用已
-- 发出后必须 reconcile」. The cancel is decided **before** the adapter contract is
-- built, in the same place `unroutable_reason` refuses a row -- which is the
-- discipline the B4-2 review wrote down as 「这个检查能不能挪到准入处」. And §7.2
-- step 7 gives the intent side its word: 「在 pre_delivery … 则 DENIED/INVALIDATED,
-- 不发送」. `intent_records.status` already carries DENIED, so only this CHECK
-- needs widening.
--
-- **Widened, not replaced.** No existing row changes and no existing predicate
-- moves: the claim filter is `PENDING OR (SENDING AND lease expired)`, so a
-- CANCELLED row is simply never claimed again -- the same way DELIVERED is not.

ALTER TABLE outbox_messages
    DROP CONSTRAINT IF EXISTS outbox_messages_status_check;

ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_status_check CHECK (status IN (
        'PENDING', 'SENDING', 'DELIVERED', 'FAILED', 'UNKNOWN', 'CANCELLED'
    ));

COMMENT ON COLUMN outbox_messages.status IS
    'PENDING/SENDING/DELIVERED/FAILED/UNKNOWN as before, plus CANCELLED: the '
    'pre_delivery proactivity re-check refused this row before anything was '
    'sent (B4-4, ADR-0098 §一). CANCELLED carries no delivery receipt because '
    'no provider was asked; the reason lives in a policy_decisions row with '
    'hook=pre_delivery.';

INSERT INTO schema_migrations(version) VALUES ('048_outbox_cancelled')
ON CONFLICT (version) DO NOTHING;
