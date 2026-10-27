-- D13 (ADR-0048 §二.3, K6; parity §6.4): deactivation, purge eligibility, legal hold.
--
-- parity §6.4 is the shape being built to:
--
--     tenant/employee 的旧 DELETE 不存在于 v2。停用立即撤销 session、channel/binding
--     并阻止新 inbox；记录 `disabled_at` 与 `purge_eligible_at=+90d`。
--     purge 仅 superuser、到期、无 legal hold、再次提交 reason。
--
-- Both subjects get all three columns, because both are purgeable subjects and
-- the routes that read them (#11/#12 for tenants, #18/#19 for employees) share
-- one rule. `status` already exists on both and already has its two values, so
-- deactivation writes a timestamp beside a state that was always there rather
-- than inventing a second way to say "disabled".

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS purge_eligible_at timestamptz;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS legal_hold boolean NOT NULL DEFAULT false;

ALTER TABLE employees ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS purge_eligible_at timestamptz;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS legal_hold boolean NOT NULL DEFAULT false;

-- **The two timestamps travel together or not at all.** A row with a
-- `purge_eligible_at` and no `disabled_at` would be eligible for purge without
-- anybody having disabled it; the reverse would be disabled with no clock
-- running. Either is reachable from a partial UPDATE, and neither is something
-- an application should be trusted to avoid.
ALTER TABLE tenants
  ADD CONSTRAINT tenants_disabled_pair
  CHECK ((disabled_at IS NULL) = (purge_eligible_at IS NULL));
ALTER TABLE employees
  ADD CONSTRAINT employees_disabled_pair
  CHECK ((disabled_at IS NULL) = (purge_eligible_at IS NULL));

COMMENT ON COLUMN tenants.legal_hold IS
'Blocks purge regardless of eligibility (parity §6.4). **There is no route that
sets it** -- ADR-0048 K6 put the setter outside M1a and required the gap be
registered explicitly, because a hold that is always false makes purge *look*
guarded while guarding nothing. Registered as T118, and purge must read this
column rather than assume it (K6 again: "purge 必须真读它").';
COMMENT ON COLUMN employees.legal_hold IS
'Same as tenants.legal_hold: read by purge, set by nothing in M1a (T118).';
COMMENT ON COLUMN employees.disabled_at IS
'When this person was deactivated. Paired with purge_eligible_at by
employees_disabled_pair: a retention clock with no start, or a start with no
clock, are both states no route should be able to leave behind.';

-- Deactivation writes `status` and the two timestamps. It does **not** get
-- `legal_hold`: nothing in M1a may set that, and a grant is the cheapest place
-- to make "may not" true rather than "does not".
--
-- Column-level for the reason 018 gives: this row also carries `password_hash`,
-- `permissions` and `credential_version`, each with its own route.
GRANT UPDATE (status, disabled_at, purge_eligible_at) ON employees TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('019_deactivation_and_purge_eligibility')
ON CONFLICT (version) DO NOTHING;
