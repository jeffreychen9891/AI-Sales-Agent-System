-- 039: the customer on the other end (route freeze B3 #21-#23, ADR-0048 K11).
--
-- **K11 settled the model; this migration is that ruling and nothing else:**
--
-- 1. 「由 ingress 在**首次收到该外部身份的消息时**创建」 -- and creation happens
--    *after* authentication, which is why the writer sits inside the same
--    transaction as the `inbox_events` insert rather than in the worker
--    (`unified-authorization-layer.md:199`: 「认证失败不得写 inbox、创建
--    tenant/contact」);
-- 2. 「标识为 `(tenant_id, config_id, external_user_id)` 三元组唯一」, and
--    **M1a does not merge identities across channels** -- the same person under
--    two bindings is two contacts, because identity resolution is its own
--    problem and guessing at it silently is worse than not doing it;
-- 3. 「`profile_data` allowlist 仅 `display_name` 与 `avatar_url`」, 其余不存.
--
-- **The user ruled on 2026-09-06 (T175) that the writer ships with the table**,
-- rather than leaving three read routes over an empty table -- the shape
-- ADR-0061 registered as C14 and this repository has now met three times.

CREATE TABLE IF NOT EXISTS contacts (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,

    -- **The two columns travel together**, for the reason `012` gives about the
    -- manager's id: an external user id means something only inside the channel
    -- binding it came from, and the same person carries a different id under a
    -- different binding.
    config_id text NOT NULL,
    -- **Bounded, like `012`'s** (independent review 2026-09-06, P2-4). The
    -- comment above cites `012` for why these two columns travel together
    -- and then took neither of the constraints `012` puts on them. A
    -- channel decides the length today, which is exactly why the database
    -- should say what it will store.
    --
    -- **The writer bounds it first** (re-review P2-1): `012`'s copy of this
    -- constraint has never had a live writer, so nobody had met what it does
    -- on a hot path -- a violation here rolls back the whole `accept()`
    -- transaction and the message is lost, not the identity. `adapter.py`
    -- therefore treats an over-long id as *no identity*, which leaves this
    -- line as what it should be: the last line, unreachable from the only
    -- writer, and still worth having for anything that writes here next.
    external_user_id text NOT NULL CHECK (length(external_user_id) BETWEEN 1 AND 128),

    -- **K11's allowlist, enforced by the database rather than by a convention.**
    -- Removing the two permitted keys must leave nothing behind; anything else
    -- is refused. **Keys only** (independent review P3-5): the values may be
    -- any JSON -- a string, an object, `null`, four thousand characters. K11
    -- fixed a key allowlist and deliberately did not fix the shapes, and B3
    -- #22 hands this document out as it is, so a reader of "allowlist" should
    -- not assume the values are guarded too.
    --
    -- Empty today and honestly so: M1a calls no channel profile API, so
    -- nothing can fill these in -- the column exists because K11 named it,
    -- and its emptiness is a fact about what the platform knows.
    profile_data jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(profile_data) = 'object'
        AND profile_data - 'display_name' - 'avatar_url' = '{}'::jsonb
    ),

    -- When this identity first spoke, when it last spoke, and how often.
    --
    -- **Both are the sender's clock, and the writer bounds it**
    -- (independent review 2026-09-06, P2-1). `occurred_at` comes off the
    -- channel payload, and nothing on the way in gives it an upper bound: a
    -- callback claiming a date ten years out would pin both columns there,
    -- `GREATEST` would refuse every later message's honest timestamp, and
    -- `contacts_recent_idx` would keep that contact at the top of every
    -- operator's list forever -- with no route able to correct it, because
    -- there is no route that writes contacts at all.
    --
    -- The writer therefore clamps to the platform clock, which is the
    -- direction `conversation-aggregate.schema.json` already states for
    -- `recorded_at`: a sender-controlled timestamp cannot anchor what we
    -- store.
    --
    -- **Both ends, inside a one-day window** (chain review 2026-09-06,
    -- P3-1). The upper bound came first; `LEAST` on `first_seen_at` then
    -- opened the lower one, where a payload claiming 1970 pinned "first
    -- seen" just as permanently. Out-of-order delivery is honoured **within
    -- that window** -- not in general, which is what this comment used to
    -- promise.
    --
    -- **The lower bound has a cost, and it lands here** (P3-2): a genuine
    -- redelivery from three days ago is recorded as one day ago, so the
    -- contact looks *more* recently active than it was. Bounded by the
    -- window and in the harmless direction -- "slightly too high in the
    -- list" rather than "permanently first" -- but it is the price of the
    -- floor, not a free win.
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    message_count bigint NOT NULL DEFAULT 0 CHECK (message_count >= 0),

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    -- K11's triple. This is what makes the ingress write an upsert rather than
    -- a "select then insert", and therefore safe under two concurrent messages
    -- from the same customer.
    UNIQUE (tenant_id, config_id, external_user_id),

    -- **The binding is a real binding** (independent review P2-4). Three
    -- other tables carry this pair -- `ingress_replay_keys` (001),
    -- `conversation_aggregates` (008), `tenant_manager_contacts` (012) --
    -- and every one of them references `channel_bindings`, which keeps
    -- `UNIQUE (tenant_id, config_id)` for the purpose. Unreachable today,
    -- because the only writer uses the binding it just authenticated; the
    -- point is that this is the cheap moment to say so. M1a deactivates
    -- bindings and never deletes them, so a contact never loses its
    -- referent.
    FOREIGN KEY (tenant_id, config_id) REFERENCES channel_bindings(tenant_id, config_id)
);

ALTER TABLE contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE contacts FORCE ROW LEVEL SECURITY;
DO $contacts_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'contacts'
           AND policyname = 'contacts_tenant_isolation'
    ) THEN
        CREATE POLICY contacts_tenant_isolation ON contacts
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$contacts_rls$;

-- The list B3 #21 pages: this tenant's contacts, most recently active first.
CREATE INDEX IF NOT EXISTS contacts_recent_idx ON contacts (tenant_id, last_seen_at DESC, id);

COMMENT ON TABLE contacts IS
    'One customer identity per channel binding (ADR-0048 K11). Created by '
    'ingress on the first authenticated message from that identity; never '
    'merged across bindings in M1a.';

-- **`fastagent_ingress` writes it, and it is the only writer.** No `DELETE`:
-- a customer who once wrote did, and the conversation rows reference the same
-- period. The control plane's `SELECT` arrives with the read routes
-- (B3 #21-#23), for the reason `035` gives -- a grant that precedes its caller
-- is a permission nothing is testing.
GRANT SELECT, INSERT, UPDATE ON contacts TO fastagent_ingress;

INSERT INTO schema_migrations(version) VALUES ('039_contacts')
ON CONFLICT (version) DO NOTHING;
