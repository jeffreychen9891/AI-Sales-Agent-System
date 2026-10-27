-- 035: the control plane reaches `manual_tasks` (route freeze B3 #15 / #16).
--
-- `034` gave the worker `SELECT, INSERT, UPDATE` because the engine files tasks
-- and marks them digested. This is the other side: a person filing one, and a
-- person reading the backlog.
--
-- **`SELECT, INSERT` and no more.** Claiming, resolving, reassigning and
-- cancelling are B3 #17-#20, they are all `UPDATE`s, and none of them exists
-- yet. Granting `UPDATE` now would mean the grant answers "what can this
-- service do?" with a route set that will not exist until the next batch --
-- and a permission that arrives before its caller is a permission nobody is
-- testing. `DELETE` is withheld permanently, for `034`'s reason: cancelling
-- leaves the row and its reason behind, which is the whole point of `cancel`
-- rather than `DELETE` (freeze B3 #20).
--
-- RLS is unchanged and still does the tenant work: `manual_tasks` carries
-- `ENABLE + FORCE` with `manual_tasks_tenant_isolation`, and `fastagent_app`
-- gets no exemption from it here.
GRANT SELECT, INSERT ON manual_tasks TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('035_manual_task_control_plane')
ON CONFLICT (version) DO NOTHING;
