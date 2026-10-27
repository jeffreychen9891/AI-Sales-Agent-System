-- 026: let the control plane purge an employee -- tombstone, never delete.
--
-- **Freeze B1 #19 + parity §6.4**, executed under **ADR-0056**: the purge runs
-- synchronously and answers `succeeded`, because the work is bounded and
-- because `accepted` would promise a worker that does not exist.
--
-- §6.4's sentence is「append-only audit 不删除，主体用不可逆 tombstone/fingerprint」.
-- Both halves get a column here.
--
-- **`purged_at` -- the fact, not a naming convention.** After a purge the row
-- still exists (every `audit_events.actor_id`, `employee_sessions.employee_id`
-- and `employee_roles.employee_id` points at it), and its identifying columns
-- are overwritten. Something has to say that this happened, and inferring it
-- from the shape of a tombstoned email address would be reading a string
-- pattern as a state -- the failure `018` refused when it declined to encode
-- meaning in `display_name`.
--
-- **`subject_fingerprint` -- §6.4's other word.** A purge destroys the identity
-- but not the need to answer "were these two records the same person". The
-- fingerprint is `sha256(tenant_id || ':' || lower(original email))`: stable,
-- one-way, and computed **from the address that is being destroyed**, so it can
-- still correlate two purged records without holding what they had in common.
-- It is not a credential and nothing verifies anything against it.
--
-- **What the grant widens, stated plainly.** `email` becomes writable by
-- `fastagent_app`, and that role is shared by every route (**T127**, whose
-- **fifth** instance this is). What stops `PATCH /employees/{id}` from
-- rewriting an address today is that its statement does not name the column --
-- one defence, not two. The alternative is a `SECURITY DEFINER` function, which
-- is T127's third option and a decision bigger than one route; the entry says
-- so and this migration does not pre-empt it.
--
-- **What is deliberately not granted**: `DELETE` on `employees` (§6.4: the old
-- DELETE does not exist in v2), `permissions` (a purge must not be able to mint
-- privilege -- and it has no reason to write it), `tenant_id` / `id` (identity
-- and the RLS key), `created_at`, and `legal_hold` (**T118**: nothing may set
-- it, and the purge route *reads* it as a guard -- a route that could clear its
-- own guard is not a guard).

ALTER TABLE employees ADD COLUMN IF NOT EXISTS purged_at timestamptz;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS subject_fingerprint text
    CHECK (subject_fingerprint IS NULL OR subject_fingerprint ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN employees.purged_at IS
'When this subject was purged (freeze B1 #19, parity §6.4). The row survives --
audit, sessions and role rows all reference it -- while `email`,
`display_name`, `employee_no`, `department` and `password_hash` are overwritten
with values nobody holds. NULL means not purged; the state is not inferable
from the shape of the tombstoned address and must not be read that way.';

COMMENT ON COLUMN employees.subject_fingerprint IS
'sha256(tenant_id || '':'' || lower(original email)), written at purge time.
§6.4 asks for a tombstone *and* a fingerprint: the tombstone destroys the
identity, the fingerprint keeps the one question that survives it -- whether two
purged records were the same person. One-way, not a credential, and nothing
authenticates against it.';

GRANT UPDATE (email, purged_at, subject_fingerprint) ON employees TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('026_employee_purge')
ON CONFLICT (version) DO NOTHING;
