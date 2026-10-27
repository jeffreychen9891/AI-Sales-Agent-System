-- 025: let the control plane retarget a binding's provider instance.
--
-- **Freeze B2 #3 (`C0A-033` 拆): 「仅非 secret 元数据」.** The inventory row this
-- was split out of describes the old endpoint as `guid/key/secret/active/
-- provider -> config`, and the freeze cut that into four:
--
--   * `key` / `secret`  -> B2 #4 (`POST .../credentials`, §5.2 `SecretWrite`);
--   * `active`          -> B2 #5 (`POST .../deactivate`, migration `024`);
--   * `provider`        -> set when the binding is created (B2 #2). Changing
--     which provider a live binding speaks is not a metadata edit: the
--     credential, the callback token and the instance id all belong to the
--     provider they were issued by, so "switch this row to another provider"
--     is a new binding wearing an old row's name;
--   * `guid`            -> **this migration**.
--
-- So the non-secret metadata this route can write is one column, and `017`
-- already said whose job it was, in the column comment: "Written by route
-- freeze B2 when it lands; seeded by an operator until then." This is that.
--
-- **`UPDATE` only. Deliberately not `SELECT`.** `T123` reserves the *read*
-- grant on this column for the outbound wiring batch, together with the
-- capability declaration `T78` still owes -- because who reads a guid depends
-- on where the dispatcher gets it, and `007` made this table a column-by-column
-- table precisely so that nobody grants a column to a role on the way past.
-- `023` refused the read for a list route; this migration refuses it for a
-- write route, which costs something real and visible: **the PATCH cannot echo
-- the one field it writes** (registered as `T135`). Paying that is the point --
-- the alternative is to take half of `T123`'s decision as a side effect of a
-- response body, which is the exact move both earlier migrations declined.
--
-- `024` wrote, of this column, "still readable and writable by nobody, and a
-- deactivation route is not where that gets decided". **A `PATCH` route on the
-- binding itself is where it gets decided, and only for the writable half.**
--
-- **What is deliberately not granted here**: `config_id` / `tenant_id` (a
-- binding's identity, and RLS's key), `source` (above), `created_at`,
-- `profile_release_id` (B2 #14's, `007`), `status` (B2 #5's, `024`), and the
-- credential columns (`024` for the destroying half, B2 #4 for rotation). Four
-- migrations now grant `UPDATE` on this one table, each for one route's
-- statement -- and the grant is held by `fastagent_app`, which every route
-- shares. That is `T127`, and this is its fourth instance.

GRANT UPDATE (provider_instance_id) ON channel_bindings TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('025_channel_binding_metadata_update')
ON CONFLICT (version) DO NOTHING;
