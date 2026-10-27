-- 051: the first GRANT on the knowledge tables, and the reason it is this role.
--
-- `050` issued none, deliberately, and said why: 「a grant that precedes its
-- caller is a permission nothing is testing」. **This migration arrives with the
-- caller** -- `scripts/import_knowledge.py`, the第 4 批 importer.
--
-- ---------------------------------------------------------------------------
-- Why `fastagent_app` and not the migration role
-- ---------------------------------------------------------------------------
--
-- The importer's first draft connected as `fastagent_migrator`, on the
-- reasoning that importing is an administrative action and the migration role
-- already reaches every table. **That reasoning was wrong, and the way it was
-- wrong is worth keeping:**
--
-- `050` marks all three tables `FORCE ROW LEVEL SECURITY`, and `FORCE` exists
-- precisely so that the table's *owner* stops being exempt. So the draft
-- assumed the migrator would be subject to the policies like anyone else --
-- and wrote a cross-tenant case on that connection to repay the debt
-- 第 3 批自审 §六.1 registered.
--
-- **It measured otherwise**: the case saw both tenants' rows with no
-- `app.tenant_id` set at all. `infrastructure/local/compose.yml:15` sets
-- `POSTGRES_USER: fastagent_migrator`, so that role is the cluster
-- **superuser** -- and a superuser bypasses row level security outright,
-- `FORCE` or not. `FORCE` lifts the *owner* exemption; it cannot lift the
-- superuser one, because that one is not an exemption, it is the absence of
-- the check.
--
-- **So 第 3 批's self-review was right the first time** (「用例走的是
-- `fastagent_migrator`，它不受策略约束……本批没有证明隔离真的隔离」) and the
-- importer's first draft was the thing that was wrong. It is recorded here
-- because the mistaken version is the plausible one: `FORCE` really does mean
-- what the draft thought it meant, just not about this role.
--
-- `fastagent_app` is `NOSUPERUSER` (`scripts/migrate.py:58`), so it is subject
-- to the policies, and `tests/integration/test_knowledge_rls_isolation.py`
-- drives isolation over it. **That is 第 3 批's debt, actually repaid.**
--
-- ---------------------------------------------------------------------------
-- What is NOT granted
-- ---------------------------------------------------------------------------
--
--   * **`retrieval_evidence` -- nothing.** Its writer is the worker, in 第 6 批
--     (检索层). Granting it now would repeat exactly the thing `050` refused to
--     do, one table over.
--   * **No `DELETE`.** A release is superseded, never deleted (裁定五 / 裁定七);
--     the only deletion is `purge_tenant_data`, which is a definer function and
--     needs no grant here. Following `039`'s rule: a role gets the verbs its
--     caller uses, not the verbs it might one day use.
--   * **No grant to `fastagent_worker`.** It is the role that parses untrusted
--     inbound content; it reads knowledge in 第 6 批 and gets `SELECT` then.

-- **`knowledge_releases` 只发 `SELECT, INSERT`**（独立审查 2026-09-12，P2-4）。
--
-- 第一版连 `UPDATE` 一起发了，而**这个文件自己刚写下的规则正是要禁这件事**：
-- 「a role gets the verbs its caller uses, not the verbs it might one day use」。
-- 导入脚本对 `knowledge_releases` 只有 `INSERT`；`UPDATE` 落在
-- `knowledge_chunks`（补向量）上。**那个 verb 的调用方是第 5 批的
-- publish / rollback**，所以它随那一批一起到。
--
-- **一条规则写在文件里而自己没照做，比没写还贵** —— 下一个人会照着那句话
-- 推断这里已经这么做了。
GRANT SELECT, INSERT ON knowledge_releases TO fastagent_app;
GRANT SELECT, INSERT, UPDATE ON knowledge_chunks TO fastagent_app;

COMMENT ON TABLE knowledge_releases IS
    'One published version of a tenant''s reference material (裁定五). Its '
    'lifecycle is independent of profile_releases: adding a piece of material '
    'does not republish the merchant''s scripted words. At most one release is '
    'published at a time, and every retrieval reads that one (裁定七). '
    'Written by the import path as fastagent_app (051).';

INSERT INTO schema_migrations(version) VALUES ('051_knowledge_ingest_grant')
ON CONFLICT (version) DO NOTHING;
