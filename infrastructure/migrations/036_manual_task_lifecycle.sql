-- 036: the four verbs that move a task (route freeze B3 #17-#20).
--
-- **Column-level, the shape `032` used on `tenants`.** Claim, resolve, reassign
-- and cancel all write the same table, and between them they touch six columns.
-- Everything else on `manual_tasks` is deliberately out of reach of the control
-- plane, and each omission is a different sentence:
--
-- * `task_class` / `notify` -- the classification. The engine derives it from
--   the reason a delivery ended where it did, and B3 #16 refuses to let a person
--   assert it even at filing time. Letting them rewrite it afterwards would be
--   the same hole with an extra step;
-- * `digested_at` -- the worker's mark that the merchant has been told. A route
--   that could clear it could make the hourly digest report the same backlog
--   twice; a route that could set it could make a task vanish from the digest
--   without anyone ever being told. Only `digest.py` writes it (034);
-- * `reason_code` / `sla_due_at` / `expires_at` -- what the task is and when it
--   was due. Moving a deadline after the fact is how an SLA report becomes
--   fiction. Reassignment moves *who*, not *when*, and that asymmetry is the
--   point of granting one and not the other;
-- * `conversation_id` / `intent_id` / `tenant_id` / `id` -- identity. A task
--   that can be moved to another conversation is not the same task.
--
-- `updated_at` is granted because every one of the four writes it. **The state
-- timestamps granted are two, not four** -- `claimed_at` and `resolved_at` --
-- because those are the only two `034` has: its CHECK constraints tie each of
-- them to its status, so they must move together with `status`.
--
-- **There is no `cancelled_at`, and that has a consequence worth stating**
-- (independent review 2026-09-06, P3-3): "when was this cancelled" is read off
-- `updated_at`. That is reliable **only because all four verbs refuse a task in
-- a terminal state**, so nothing writes the row again after it is cancelled --
-- a dependency that was holding without being written down. A verb that could
-- touch a closed task would break it silently, and the fix then is a column,
-- not a comment.
GRANT UPDATE (
    status, owner_employee_id, claimed_at, resolved_at, evidence, updated_at
) ON manual_tasks TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('036_manual_task_lifecycle')
ON CONFLICT (version) DO NOTHING;
