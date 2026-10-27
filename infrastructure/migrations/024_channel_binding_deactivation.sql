-- 024: let the control plane disable a binding and destroy its callback token.
--
-- **Freeze B2 #5 (`C0A-034`) is two clauses, not one**: 「停用 + revoke callback
-- token」. The first is a `status` flip; the second is the reason this migration
-- exists, because `007` granted `fastagent_app` `UPDATE` on `profile_release_id`
-- and nothing else on this table.
--
-- **Why the token has to be destroyed rather than merely refused.** The ingress
-- adapter already rejects a disabled binding (`adapter.py`: `binding["status"]
-- != "active"` -> `IngressAuthenticationError`), so a `status` flip alone stops
-- callbacks today. What it does not do is stop them *tomorrow*: the hash stays
-- in the row, so anything that ever sets `status` back to `'active'` silently
-- restores a credential that may well be the reason somebody disabled the
-- binding in the first place. That is the shape ADR-0048 K8 refused for
-- passwords -- a credential that stays valid because nobody removed it -- and
-- the freeze's second clause is asking for the same answer here.
--
-- **`current_token_hash` is `NOT NULL`, so revocation is an overwrite.** The
-- route writes the hash of a token it generates and immediately discards, so
-- the column keeps a well-formed value that **no one holds and no one can
-- present**. The alternative -- making the column nullable -- would weaken a
-- constraint that has held since `001` for every row, in order to express a
-- state that an unmatchable hash already expresses.
--
-- **The pending rotation is cleared too.** `next_token_hash` /
-- `next_valid_from` / `overlap_ends_at` describe a credential that is about to
-- become valid; leaving them behind on a disabled binding would mean a
-- re-enabled binding could accept a token whose rotation nobody remembers
-- scheduling.
--
-- **What is deliberately *not* granted**: `config_id`, `tenant_id`,
-- `profile_release_id` (that is B2 #14's, and it has its own route),
-- `source`, `created_at`, `current_valid_from`, and `provider_instance_id`
-- (**T123** -- still readable and writable by nobody, and a deactivation route
-- is not where that gets decided).
--
-- **No retention clock here, unlike `019`.** parity §6.4's `disabled_at` /
-- `purge_eligible_at` pair is written about **tenant and employee** -- subjects
-- with history worth keeping and a purge route to become eligible for. A
-- channel binding has neither: there is no B2 purge route in the freeze, and a
-- clock nothing reads is the "flag enforced nowhere" shape `018` refused to add.
-- `status` answers "is this channel live"; `GET /channel-bindings` publishes it.

GRANT UPDATE (status, current_token_hash, next_token_hash, next_valid_from, overlap_ends_at)
    ON channel_bindings TO fastagent_app;

-- **Somebody reading this table needs to be told which hashes are tombstones.**
-- `017` set the precedent one column over: a value whose meaning depends on
-- state is worth a `COMMENT`, because the column type cannot carry it. A
-- well-formed 64-hex string on a `disabled` row looks exactly like a live
-- verifier and is the opposite of one -- nobody holds the token it hashes.
COMMENT ON COLUMN channel_bindings.current_token_hash IS
'sha256 of the callback token the ingress path compares a presented token
against. NOT a redacted token: publishing it publishes the ability to test
guesses offline, which is why no runtime role holds SELECT on it (007 grants
this table column by column; 023 deliberately left both hashes out).
**When status = ''disabled'' this is a tombstone**: the deactivate route (freeze
B2 #5) overwrites it with the hash of a token generated and discarded inside the
request, so the column keeps a well-formed value that nobody holds and nobody
can present. Re-activating such a row does not restore a working credential --
it has to be re-provisioned.';

INSERT INTO schema_migrations(version) VALUES ('024_channel_binding_deactivation')
ON CONFLICT (version) DO NOTHING;
