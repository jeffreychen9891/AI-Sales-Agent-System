-- 029: K9's credential metadata columns on `channel_bindings`.
--
-- **ADR-0048 §二.6 (K9)**: the channel secret does not live in this database.
-- What lives here is the metadata that lets somebody answer "which credential
-- is this binding using, and when was it last checked" without being able to
-- use it -- and that set is, by K9, the same six fields `SecretWrite` returns:
-- `secret_ref`, `version`, `status`, `updated_at`, `last_verified_at`,
-- `fingerprint`.
--
-- **Five columns, not six.** `status` is already here and already delivered
-- (001, `active` / `disabled`); the freeze row says so in as many words. Adding
-- a second `status` for the credential would put two answers to one question in
-- one row.
--
-- **The columns are prefixed and the response fields are not.** K9 names a
-- *response* field set, not column names. A bare `updated_at` next to the
-- existing `created_at` would read as the row's own timestamp -- it is the
-- secret's. The route maps `secret_updated_at` -> `updated_at` on the way out.

ALTER TABLE channel_bindings
    -- Which secret, in the deployment's secret store. **Not the secret**, and
    -- not a path the application chooses: the operator places the file and this
    -- column records which one was placed. ADR-0061 for why the route does not
    -- receive the secret itself.
    ADD COLUMN secret_ref text
        CHECK (secret_ref IS NULL OR secret_ref ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),

    -- The operator's version for that secret. Monotonic by convention, not by
    -- constraint: the deployment names its own versions and this table is not
    -- the place to argue with them. Positive, because "version 0" and "version
    -- -1" are both ways of saying the field was never really filled in.
    ADD COLUMN secret_version integer CHECK (secret_version IS NULL OR secret_version > 0),

    -- When the registration was last written, and when the server last read the
    -- file and confirmed the fingerprint still matched. **They are different
    -- facts**: a registration can be years old and verified this morning.
    ADD COLUMN secret_updated_at timestamptz,
    ADD COLUMN secret_last_verified_at timestamptz,

    -- **Eight hex characters, per K9** (「hash 前 8 位」). Short on purpose: it
    -- is a human-comparable label for "is this the credential I think it is",
    -- not a verifier. A full hash in a readable column would be a verifier, and
    -- 023 withholds `SELECT` on the two token hashes for exactly that reason.
    --
    -- **The criterion, written down because it is not the length** (independent
    -- review, round 2). A readable fingerprint is safe when **the preimage is
    -- high-entropy and issued by somebody else** -- here, an WeComProxy
    -- appSecret, which cannot be guessed, so a 32-bit prefix does not turn "is
    -- my guess right" into a feasible check.
    --
    -- Change the preimage and the same code becomes a verifier. That is not
    -- hypothetical in this repository: `employees.subject_fingerprint` was an
    -- unsalted digest over `tenant_id:email` -- a low-entropy, enumerable
    -- preimage -- and it took ADR-0064 to make it keyed. **Two sides of one
    -- criterion.** Anybody copying this column to a new preimage has to check
    -- which side they are on.
    ADD COLUMN secret_fingerprint text
        CHECK (secret_fingerprint IS NULL OR secret_fingerprint ~ '^[0-9a-f]{8}$'),

    -- **A registration is all of its parts or none of them.** The same argument
    -- as 028's rotation constraint, applied to the columns 028 could not cover
    -- because they did not exist yet: a `secret_ref` with no fingerprint is a
    -- binding that claims a credential nobody checked, and a fingerprint with no
    -- `secret_ref` cannot be re-verified because nothing records which file it
    -- came from.
    --
    -- `secret_last_verified_at` is deliberately **not** in this group. It is the
    -- one fact that legitimately arrives later and separately -- it is what a
    -- verification writes, and a registration that has not been verified since
    -- is honestly represented by a null.
    ADD CONSTRAINT channel_bindings_credential_registration_is_whole
        CHECK (
            (secret_ref IS NULL) = (secret_fingerprint IS NULL)
            AND (secret_ref IS NULL) = (secret_version IS NULL)
            AND (secret_ref IS NULL) = (secret_updated_at IS NULL)
        ),

    -- Verification cannot predate the registration it verifies.
    ADD CONSTRAINT channel_bindings_verification_follows_registration
        CHECK (
            secret_last_verified_at IS NULL
            OR (secret_updated_at IS NOT NULL AND secret_last_verified_at >= secret_updated_at)
        );

-- **Column grants, and the reason they are spelled out again.** This is the
-- sixth time (T127): a `GRANT` on a column belongs to a **role**, and
-- `fastagent_app` is the role every API route runs as -- so granting these
-- columns to it grants them to every route, not to this one. The route-level
-- restriction is `authorize()`'s business and lives in the application.
--
-- `SELECT` is granted on all five, including the fingerprint: it is masked
-- metadata that the route exists to return. That is a deliberate difference
-- from 023, which withholds `SELECT` on `current_token_hash` and
-- `next_token_hash` -- those are verifiers, and eight characters of a digest
-- is not.
GRANT SELECT (secret_ref, secret_version, secret_updated_at,
              secret_last_verified_at, secret_fingerprint)
    ON channel_bindings TO fastagent_app;

GRANT UPDATE (secret_ref, secret_version, secret_updated_at,
              secret_last_verified_at, secret_fingerprint)
    ON channel_bindings TO fastagent_app;

-- **`fastagent_ingress` is given nothing here.** It resolves a binding through
-- `resolve_channel_binding()` and needs the token hashes to authenticate a
-- callback; it has no use for which file the secret came from, and the ingress
-- role is the one exposed to the public internet.

-- Recording this file as applied. See `tests/contracts/test_migration_versions.py`
-- for why the gate exists: `migrate.py` skips a file whose stem is already in
-- `schema_migrations`, and each migration writes that row itself. 029 was
-- missing it for the same reason 028 was -- the clean-checkout gate builds a
-- fresh database every run, so it cannot see the omission at all.
INSERT INTO schema_migrations(version) VALUES ('029_channel_credential_metadata')
ON CONFLICT (version) DO NOTHING;
