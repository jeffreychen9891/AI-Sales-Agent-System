-- D16 (ADR-0048 §二.4, K7): the M1a minimum set of employee profile fields.
--
-- The ruling names three and only three: `display_name` (required),
-- `employee_no` (optional), `department` (optional). Its reason is worth
-- repeating here because it is what decides "required":
--
--     控制面要显示「谁接管了这个会话」，没有 `display_name` 只能显示 uuid 或邮箱。
--
-- An avatar is **not** here: it needs object storage, which ADR-0048 moved out
-- of M1a with K20.
--
-- `must_change_password` is **not** here either, and that is deliberate. K8
-- adds it, and the route that enforces it is B1 #17 (password reset). A flag
-- that exists and is enforced nowhere is the "record says one thing, system
-- does another" shape this repository keeps paying for -- the same reason
-- ADR-0048 §二.3 demands legal hold's missing setter be registered explicitly.
-- It arrives with its enforcement.

ALTER TABLE employees ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS employee_no text;
ALTER TABLE employees ADD COLUMN IF NOT EXISTS department text;

-- **Backfill before NOT NULL**, in this order and in one transaction: existing
-- rows predate the column, and a bare `SET NOT NULL` would fail on them. The
-- local part of the address is a placeholder chosen because it is derived from
-- data the row already has -- it is not a guess about anybody's name, and an
-- operator replacing it is the expected next step.
--
-- **The backfill is self-contained, and it has to be**: the CHECK added a few
-- lines below validates the rows this statement just wrote, and `email` carries
-- no length or format constraint anywhere in `001_m0`..`019` -- there is no
-- route creating employees yet, so nothing guarantees the local part is 1..128
-- characters. One row with an empty or over-long local part would roll the
-- whole migration back, at the exact moment nobody wants a surprise: a deploy
-- (independent review 2026-09-05, P2-1). `left(..., 128)` and `nullif(..., '')`
-- close both edges, and having to choose a fallback is what settles the second
-- question the packet raised -- if a value must be picked anyway, pick one a
-- person can read.
UPDATE employees
   SET display_name = coalesce(
           nullif(left(split_part(email, '@', 1), 128), ''),
           '未命名'
       )
 WHERE display_name IS NULL;

ALTER TABLE employees ALTER COLUMN display_name SET NOT NULL;

-- Bounds, not validation. The route validates shape; these stop a client that
-- bypasses it -- or a future writer -- from storing something unbounded, and
-- they are the same limits the request model declares.
ALTER TABLE employees
  ADD CONSTRAINT employees_display_name_length
  CHECK (length(display_name) BETWEEN 1 AND 128);
ALTER TABLE employees
  ADD CONSTRAINT employees_employee_no_length
  CHECK (employee_no IS NULL OR length(employee_no) BETWEEN 1 AND 64);
ALTER TABLE employees
  ADD CONSTRAINT employees_department_length
  CHECK (department IS NULL OR length(department) BETWEEN 1 AND 128);

COMMENT ON COLUMN employees.display_name IS
'What the control plane shows instead of a uuid or an email when it has to name a
person -- "who took this conversation over" (ADR-0048 K7). Required: the pages
that name people have no fallback that is not a leak or a number.';
COMMENT ON COLUMN employees.employee_no IS
'The merchant''s own identifier for this person. Optional: not every merchant has one.';
COMMENT ON COLUMN employees.department IS
'The merchant''s own grouping. Optional, and free text: modelling an org chart is
not M1a''s job.';

-- `UPDATE` is new for this role. Until now `fastagent_app` could only read
-- `employees` (`001_m0` granted `SELECT`), which is why B1 #15 needed a
-- migration at all rather than only a route.
--
-- Column-level, not table-level: the row also carries `password_hash`,
-- `permissions`, `status` and `credential_version`, and each of those has its
-- own route and its own authorisation (B1 #16, #17, #18). A table-wide `UPDATE`
-- would let the profile route's connection write all of them, and the only
-- thing standing between it and a privilege change would be the application
-- remembering not to.
GRANT UPDATE (display_name, employee_no, department) ON employees TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('018_employee_profile_fields')
ON CONFLICT (version) DO NOTHING;
