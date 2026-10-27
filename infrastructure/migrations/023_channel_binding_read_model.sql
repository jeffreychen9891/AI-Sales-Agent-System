-- 023: let the control plane read a binding's non-secret metadata.
--
-- **B2 #1 (`GET /channel-bindings`) cannot see the columns it must return.**
-- `007` granted the app role `SELECT (tenant_id, config_id, profile_release_id)`
-- and nothing since has widened it, so `status`, `source` and the rotation
-- timestamps are invisible to the route that has to list them. That is the
-- column-level authorisation story of this table working as intended: every new
-- reading route comes and asks, and the asking is reviewable.
--
-- **The omissions are the design, and there are two kinds.**
--
-- *Credentials.* `current_token_hash` and `next_token_hash` are **not** here and
-- must never be. A callback token's hash is not a redacted token -- it is the
-- verifier the ingress path compares against, so anybody holding it can test
-- guesses offline at their own pace.
--
-- **The citation here was wrong until the independent review caught it (P2-1).**
-- The clause is `unified-authorization-layer.md:46` -- "API 只返回存在性、版本、
-- 指纹和更新时间" -- not `control-plane-parity.md:46`, which is a row in a
-- statistics table. parity's own version of the rule is `:126` (`SecretWrite`:
-- "response 只返回 masked metadata/version/status，永不回显原值"). A citation is
-- the anchor of a conclusion; an anchor pointing at a statistics table sends the
-- next reader who checks it somewhere that says nothing.
--
-- A full hash is none of the four things that clause allows.
--
-- *`provider_instance_id`.* Added by `017`, deliberately readable by nobody, and
-- still readable by nobody after this migration. **T123** is explicit that
-- granting it is the outbound wiring batch's decision, made together with the
-- capability declaration T78 still owes -- not something a list route picks up
-- on the way past because the column happened to be adjacent.
--
-- The rotation state is therefore expressed by timestamps alone
-- (`current_valid_from`, `next_valid_from`, `overlap_ends_at`), which is what it
-- is: they say when the current credential started, when the next one begins,
-- and when the overlap ends. A "rotation_pending" boolean would have needed
-- either `next_token_hash` -- the verifier again -- or an inference this schema
-- does not guarantee, and it would have told a caller nothing the timestamps do
-- not already say.

GRANT SELECT (source, status, current_valid_from, next_valid_from, overlap_ends_at, created_at)
    ON channel_bindings TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('023_channel_binding_read_model')
ON CONFLICT (version) DO NOTHING;
