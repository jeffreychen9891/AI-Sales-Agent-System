-- T96, outbound half. The two columns without which the platform cannot name
-- who a message goes to.
--
-- Found by reading the code before writing any: `grep -rn "guid"
-- infrastructure/migrations/*.sql` returns nothing, and the only
-- `external_user_id` in the repository belongs to `tenant_manager_contacts`
-- -- the *manager*, not the customer. So `send_text(guid=?, recipient_id=?)`
-- had neither argument available anywhere in the schema.
--
-- Numbered 017: 015 belongs to `feat/cp3-role-permission-routes-20260904`
-- (written, gated, waiting on its window) and 016 is L2b's digest index.


-- ------------------------------------------------- the provider instance ----
--
-- The WeComProxy `guid`. On `channel_bindings` by ruling (user, 2026-09-04):
-- that table already *is* "one tenant's one channel connection", and the guid
-- is the provider's name for the same thing.
--
-- **Nullable, and it will stay nullable.** A guid is not configured, it is
-- *earned*: `create_client` produces one and a human then scans a QR code to
-- bind it (user's supplement, 2026-09-04 -- WeComProxy is a scan-to-bind
-- provider, not a credentials-only one). A binding row therefore legitimately
-- exists before any guid does, and a NOT NULL column would force operators to
-- invent a value during the window between creating the binding and completing
-- the ceremony. That is how placeholder guids get into production.
--
-- Route freeze B2 (`C0A-032`..`C0A-041`) is the surface that will write this
-- column: `create-instance` returns the guid, `verify-qrcode` completes the
-- bind. **None of B2 is built**, so in this batch an operator seeds it.
--
-- **Seeding it is not enough to make it readable, and this batch does not make
-- it readable** (independent review 2026-09-05, P2-1). `007` defined the
-- authorisation on this table as **column-level** --
-- `GRANT SELECT (tenant_id, config_id, profile_release_id)` -- so a new column
-- is granted to nobody by construction. `fastagent_worker` holds no privilege
-- on `channel_bindings` at all; ingress reads through the
-- `resolve_channel_binding` SECURITY DEFINER function, whose `RETURNS TABLE` is
-- a fixed ten columns and does not include this one. So: **worker cannot read
-- it, app cannot read it, and the ingress function does not return it.**
--
-- That is deliberate rather than forgotten. Deciding *who* reads a guid is the
-- wiring batch's decision -- it depends on whether the dispatcher resolves the
-- binding itself or is handed the guid by whatever claims the outbox row -- and
-- guessing now would mean granting a column to a role that turns out not to
-- need it, on a table whose whole authorisation story is that it grants
-- narrowly. **Registered as T123 with the trigger written down**, so that the
-- next person cannot read "the column is there" as "the column is readable".
--
-- **The other column added below is the opposite case, and the difference is
-- worth stating**: `001` granted `conversation_aggregates` to the worker at
-- **table** level, so `peer_external_user_id` is readable the moment it exists.
-- Two new columns in one migration, two opposite visibility outcomes.
ALTER TABLE channel_bindings
    ADD COLUMN IF NOT EXISTS provider_instance_id text
        CHECK (provider_instance_id IS NULL OR length(provider_instance_id) BETWEEN 1 AND 128);

COMMENT ON COLUMN channel_bindings.provider_instance_id IS
'The WeComProxy guid this binding sends through. Nullable on purpose: a guid is
produced by create_client and then bound by a human scanning a QR code, so a
binding row exists before a guid does. Written by route freeze B2 when it lands;
seeded by an operator until then. NOT READABLE BY ANY RUNTIME ROLE YET: this
table is granted column by column (007) and resolve_channel_binding returns a
fixed column list, so the grant is part of the wiring batch (T123).';


-- ------------------------------------------------------- who to send to ----
--
-- **This one is interim and says so.** The right home for a conversation's
-- counterparty is the inbound path: a message arrives from someone, and that
-- someone is a property of the *event*. But `message-envelope` has no such
-- field -- it carries a `party` enum (`customer` / `employee` / `system` /
-- `external`), which records *that* a customer spoke and not *which* customer.
-- Adding it is a change to a frozen contract and needs an ADR.
--
-- The inbound half is deferred entirely (user ruling, 2026-09-04: no public
-- callback address exists, so the webhook cannot arrive). This batch is
-- outbound only, with the conversation and its counterparty seeded by hand --
-- and a hand-seeded counterparty still has to live somewhere.
--
-- So: a nullable column on the aggregate, explicitly interim. When the inbound
-- half lands, the envelope carries the identity and this column is either
-- backfilled from it or dropped. Registered as T97 with that trigger, because
-- an interim column with no expiry is just a column.
ALTER TABLE conversation_aggregates
    ADD COLUMN IF NOT EXISTS peer_external_user_id text
        CHECK (peer_external_user_id IS NULL OR length(peer_external_user_id) BETWEEN 1 AND 128);

COMMENT ON COLUMN conversation_aggregates.peer_external_user_id IS
'Interim (T97): the counterparty this conversation sends to, seeded by an
operator while the inbound half is deferred. The durable home is the inbound
envelope -- who spoke is a property of the event, not of the conversation, and
the same conversation can change counterparty across a rebind.';

INSERT INTO schema_migrations(version) VALUES ('017_outbound_channel_addressing')
ON CONFLICT (version) DO NOTHING;
