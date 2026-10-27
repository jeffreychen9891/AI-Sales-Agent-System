-- 022: let the control plane write `employee_roles` -- the table CP-3 created
-- and no route could fill.
--
-- **This is the migration that makes roles mean something.** `015` created
-- `roles` and `employee_roles`, granted `SELECT` on the second, and said so in
-- its own comment:
--
--     A role created through the routes in this batch grants **nothing** to
--     **nobody** until `PUT /employees/{employee_id}/roles` (route freeze B1
--     #16) is built.
--
-- That route is this batch, so the grant is here. Registered as **T92**, whose
-- expiry condition was exactly "B1 #16 落地时，同批".
--
-- **`INSERT` and `DELETE`, not `UPDATE`.** A row in this table is a fact with no
-- mutable part: who holds which role, and when it was granted. Changing an
-- assignment means removing one row and adding another, and the route replaces
-- the whole set inside one transaction. An `UPDATE` grant would let a statement
-- move `role_id` under an existing `granted_at` / `granted_by`, which is a way
-- to change what somebody may do while the row still claims it was granted by
-- somebody else at some other time.
GRANT INSERT, DELETE ON employee_roles TO fastagent_app;

-- **`granted_by` is written by the route and is the audit trail's other half.**
-- The `audit_events` row says who changed the set; this column survives on the
-- row itself, so "who gave this person this role" is answerable from the
-- assignment even if audit retention is shorter than employment.
COMMENT ON COLUMN employee_roles.granted_by IS
'The employee who granted this assignment (freeze B1 #16). Nullable because the
platform seeds nothing here today; every route-written row carries it.';

-- 015's table comment says this table is written by no route in M1a. That
-- stopped being true with this migration, and a comment that describes the
-- previous world is the "record says one thing, system does another" shape T92
-- was registered for.
COMMENT ON TABLE employee_roles IS
'Who holds which role. Written by freeze B1 #16 (PUT /employees/{id}/roles),
which replaces the whole set in one transaction and revokes the target''s
sessions so the change takes effect immediately rather than at token expiry.
authorize() reads the union of employees.permissions and the permissions of the
roles named here -- the union is resolved at login and travels in the token.';

INSERT INTO schema_migrations(version) VALUES ('022_role_assignment')
ON CONFLICT (version) DO NOTHING;
