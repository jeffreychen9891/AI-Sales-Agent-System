-- 052: 发布与版本（第 5 批），以及 T279 / T281 两条登记的到期。
--
-- 本迁移做三件：
--
--   1. `knowledge_chunk_identity` —— 让「同一个 `<slug>:v<N>` 到哪儿都是同一段
--      正文」由**数据库**保证（T279 到期）；
--   2. 一条约束触发器 —— 发布一版之前，**这一版的 chunk 必须都向量化过**
--      （T281 到期）；
--   3. 发布与回滚的状态机本身，在 `050` 已有的部分之上补齐。

-- ---------------------------------------------------------------------------
-- T279：同一个 `<slug>:v<N>`，到哪个 release 都是同一段正文
-- ---------------------------------------------------------------------------
--
-- **登记里给了两条路，而量下来有第三条。** T279 原文：
--
--   > 把 chunk 正文拆成一张按 `(tenant, slug, version)` 唯一的内容表、
--   > release 只存成员关系（规范化，能由数据库保证），还是留在写入方并加一条
--   > 门禁去查。**前者数据库说了算，后者靠纪律** —— 而本仓对「靠纪律」这四个字
--   > 已经有足够多的记录。
--
-- **两条都有代价：**
--
--   * **规范化**要重构 `knowledge_chunks` —— 一张刚合进 `main` 的表，
--     而 RFC `:753` 的字段表正是照它今天这个形状写的。为一条不变量重排一张表，
--     代价落在「下一个读 RFC 的人看到的东西和库里不一样」上；
--   * **靠纪律**是 T279 自己点名不要的那条。
--
-- **第三条：一张只管身份的小表，加一条四列外键。**
--
--     knowledge_chunk_identity(tenant_id, slug, chunk_version) PK
--                              + text_hash
--                              + UNIQUE(tenant_id, slug, chunk_version, text_hash)
--
--     knowledge_chunks FOREIGN KEY (tenant_id, slug, chunk_version, text_hash)
--                   -> knowledge_chunk_identity(同四列)
--
-- **为什么这就够了**：身份表对每个 `(tenant, slug, version)` 只允许**一行**（PK），
-- 而那一行只带**一个** `text_hash`。chunk 那条四列外键要求它的
-- `(slug, version, text_hash)` 能在身份表里找到 —— 于是第二段不同的正文
-- 既不能自己建一行（PK 撞了），也不能引用已有那一行（hash 对不上）。
--
-- **数据库说了算，而 `knowledge_chunks` 一列没动。**
--
-- **它换来的不只是 T279。** 第 4 批的「按 `text_hash` 复用旧向量」是脚本的一条
-- 纪律（`scripts/import_knowledge.py`），而它成立的前提正是这条不变量。
-- 有了身份表，那条复用**有了可依赖的判据**而不再是巧合。

CREATE TABLE IF NOT EXISTS knowledge_chunk_identity (
    tenant_id uuid NOT NULL REFERENCES tenants(id),

    -- 与 `knowledge_chunks` 同名同义：`knowledge:<slug>:v<N>` 的第二、三段。
    -- **这里不重写 ref 的语法**，理由与 `050` 相同：唯一定义点是
    -- `runtime.references.reference_refusal`。
    slug text NOT NULL CHECK (length(slug) BETWEEN 1 AND 128),
    chunk_version integer NOT NULL CHECK (chunk_version > 0),

    -- 这一版正文的指纹。**它是本仓唯一能用来判同的东西** ——
    -- 向量不行：实测同一段文本嵌两次向量不一样
    -- （`evidence/knowledge/embedding-live-client-probe-2026-09-11.md` §2.1）。
    text_hash text NOT NULL CHECK (text_hash ~ '^[0-9a-f]{64}$'),

    first_seen_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, slug, chunk_version),
    -- 外键要指过来，所以这四列要有一个唯一约束。
    UNIQUE (tenant_id, slug, chunk_version, text_hash)
);

-- **回填，而且故意让冲突炸在这里。**
--
-- 干净库上这一句搬 0 行。已经有 chunk 的库上，如果历史数据里真的存在
-- 「同一个 `<slug>:v<N>` 两段不同正文」，`ON CONFLICT DO NOTHING` 会让身份表只留
-- 第一段，**而下面那条外键会拒掉另一段** —— 迁移在这里停下。
--
-- **那正是想要的**：这条不变量今天成不成立是一个事实问题，
-- 而一次静默的「取第一段」会把答案抹掉。
INSERT INTO knowledge_chunk_identity (tenant_id, slug, chunk_version, text_hash)
SELECT DISTINCT tenant_id, slug, chunk_version, text_hash FROM knowledge_chunks
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- `text_hash` 必须真的是 `body` 的 hash（独立审查 2026-09-12，P2-1）
-- ---------------------------------------------------------------------------
--
-- **身份表锁的是 `(tenant, slug, version) → 一个 text_hash`，不是 → 一段正文。**
-- `body` 与 `text_hash` 之间先前没有任何约束，于是「数据库说了算」这句
-- **只保证到 hash 那一层**：写错 hash 的写入方照样能把两段不同的正文
-- 挂在同一条身份上。**仓里就有现成反例** ——
-- `test_knowledge_rls_isolation.py` 给一段真正文写 `'c' * 64`，所有约束都满意。
--
-- 补上这一条之后那句话才成立。`sha256()` 是 PG11+ 内建，
-- 而 `import_knowledge.text_hash` 算的正是 `sha256(body.encode("utf-8"))`。
DO $chunk_body_hash$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'knowledge_chunks_text_hash_matches_body'
    ) THEN
        ALTER TABLE knowledge_chunks
            ADD CONSTRAINT knowledge_chunks_text_hash_matches_body
            CHECK (text_hash = encode(sha256(convert_to(body, 'UTF8')), 'hex'));
    END IF;
END
$chunk_body_hash$;

DO $chunk_identity_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'knowledge_chunks_identity_fkey'
    ) THEN
        ALTER TABLE knowledge_chunks
            ADD CONSTRAINT knowledge_chunks_identity_fkey
            FOREIGN KEY (tenant_id, slug, chunk_version, text_hash)
            REFERENCES knowledge_chunk_identity (tenant_id, slug, chunk_version, text_hash);
    END IF;
END
$chunk_identity_fk$;

ALTER TABLE knowledge_chunk_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunk_identity FORCE ROW LEVEL SECURITY;
DO $chunk_identity_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'knowledge_chunk_identity'
           AND policyname = 'knowledge_chunk_identity_tenant_isolation'
    ) THEN
        CREATE POLICY knowledge_chunk_identity_tenant_isolation ON knowledge_chunk_identity
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$chunk_identity_rls$;

COMMENT ON TABLE knowledge_chunk_identity IS
    'One row per (tenant, slug, chunk_version), carrying that revision''s text '
    'hash. A four-column foreign key from knowledge_chunks makes "the same '
    'slug:vN is the same words in every release" a database guarantee rather '
    'than a convention (T279).';

GRANT SELECT, INSERT ON knowledge_chunk_identity TO fastagent_app;

-- ---------------------------------------------------------------------------
-- `UPDATE ON knowledge_releases` —— **它的调用方到了**
-- ---------------------------------------------------------------------------
--
-- `051` 第一版把这个 verb 和 `SELECT, INSERT` 一起发了，而独立审查
-- （2026-09-12，P2-4）指出**那正是 `051` 自己刚写下的规则要禁的**：
-- 「a role gets the verbs its caller uses, not the verbs it might one day use」。
-- 当时导入脚本对 `knowledge_releases` 只有 `INSERT`。
--
-- **现在它有调用方了**：`scripts/publish_knowledge.py` 的发布与回滚
-- ——把一版转 `published`、把上一版转 `superseded`，两句都是 `UPDATE`。
--
-- `tests/integration/test_knowledge_grants.py` 那张摊开的授权面表**必须同步**，
-- 否则它会红 —— 而那是它存在的理由：**多一个动词要有人解释它的调用方是谁。**
-- **列级，不是表级**（复审 2026-09-12，N-4）。发布与回滚只改这四列；
-- 表级 `UPDATE` 会顺带让应用角色能改 `version` / `content_hash` /
-- `embedding_model` —— 而那三列正是「这一版是什么」的身份。`007` 有现成写法。
--
-- **这一条在本批收得起，而 `knowledge_chunks` 那一条收不起**（T296）：
-- 后者要动已经合入的 `051`，属改安全边界；这一条是本批自己发的。
GRANT UPDATE (status, published_at, superseded_at, rolled_back_from)
    ON knowledge_releases TO fastagent_app;


-- ---------------------------------------------------------------------------
-- T281：发布之前，这一版的 chunk 必须都向量化过
-- ---------------------------------------------------------------------------
--
-- **失败是安静的，这是这条登记的全部理由**：一个只嵌了一半的 release 被置为
-- published 之后，检索少召回或零召回，而 `retrieval_evidence.outcome` 记的是
-- `'empty'` —— **和「真的没有匹配上」长得一模一样**。裁定六特意把
-- 「检索坏了」和「模型没想到要查」拆开，而这一条会造出第三种混进 `'empty'`
-- 里的原因：**资料在库里，但它没有向量。**
--
-- **单行 CHECK 表达不了**（跨行），所以是触发器。**发布是廉价的那一刻**：
-- 它极少发生，而那时整批 chunk 都在手上 —— 换成在检索时查，
-- 就是每个回合都为一次发布期的疏忽付钱。
--
-- **只在「转成 published」那一刻查**，不是每次 UPDATE：
-- 在每次 UPDATE 上查会让回滚也去扫一遍它上次发布时已经扫过的东西。
--
-- **而「已发布的 release 不会中途掉向量」是今天没有写入方那么做，不是数据库事实**
-- （独立审查 2026-09-12，P2-2）：`051` 发的是表级 `UPDATE ON knowledge_chunks`
-- （向量化其实只要 `UPDATE (embedding)`），`INSERT` 也不按 release 状态设限。
-- **一个已发布的 release 事后加一条没向量的 chunk，今天没有任何东西会红** ——
-- 而那正是 T281 说的那种安静失败。**收成列级授权或加一条「非 draft 拒写 chunk」
-- 的触发器会动到已合入的 `051`（改安全边界），本批不做，登记 T296。**
--
-- ---------------------------------------------------------------------------
-- `BEFORE INSERT` 那一支**恒拒，而这是有意的**（独立审查 2026-09-12，P0-2）
-- ---------------------------------------------------------------------------
--
-- `knowledge_chunks` 对本表有外键，所以「插入一条 `status='published'` 的 release」
-- 在那一刻**必然 0 chunk**，必定撞上 has no chunks。**审查方读作缺陷，
-- 而它是这条状态机想要的形状** —— 只是当时既没写下来，也没有用例钉住：
--
--   一版 release 的生命是 **draft → 灌 chunk → 向量化 → publish**。
--   「一步生出一个已发布的版本」在这条生命里不存在，
--   而它在库里的样子恰好就是「一个空的已发布版本」—— **T281 要拦的正是这个**。
--
-- **代价是真的，本批付了**：`main` 上 `test_knowledge_base_tables.py` 的
-- `_release()` 默认 `status='published'`，一条插入就建好一版 ——
-- 实测**36 条当场变红**（全部 `has no chunks`），
-- `test_tenant_purge_worker.py` 的 `_give_the_tenant_a_life` 同一形状再红一条。
--
-- **那些 fixture 走的是一条产品里不存在的捷径**，所以本批把它们改成
-- draft → chunk → publish，而不是把闸让开。**选 (a) 不选 (b)**：
-- (b)（只挂 `UPDATE OF status`）会留下「直接插一条空的已发布版本」这个口子，
-- 而那正是这道闸存在的全部理由。

CREATE OR REPLACE FUNCTION knowledge_release_requires_embeddings()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    missing bigint;
    total bigint;
BEGIN
    SELECT count(*) FILTER (WHERE embedding IS NULL), count(*)
      INTO missing, total
      FROM knowledge_chunks
     WHERE tenant_id = NEW.tenant_id AND release_id = NEW.id;

    -- **一个 chunk 都没有的 release 也拒。** 「发布一个空版本」不是一个想做的
    -- 动作，而它的后果与「只嵌了一半」同源：检索照常跑、照常返回零条。
    IF total = 0 THEN
        RAISE EXCEPTION
            'knowledge release % has no chunks; publishing it would make every '
            'retrieval return nothing while looking healthy', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;

    IF missing > 0 THEN
        RAISE EXCEPTION
            'knowledge release % has % of % chunks without an embedding; '
            'publishing it would silently reduce recall -- run the vectoriser '
            'first (scripts/import_knowledge.py --embed)', NEW.id, missing, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS knowledge_release_publish_guard ON knowledge_releases;
CREATE TRIGGER knowledge_release_publish_guard
    BEFORE INSERT OR UPDATE OF status ON knowledge_releases
    FOR EACH ROW
    WHEN (NEW.status = 'published')
    EXECUTE FUNCTION knowledge_release_requires_embeddings();

COMMENT ON FUNCTION knowledge_release_requires_embeddings() IS
    'Refuses to publish a knowledge release whose chunks are not all embedded '
    '(T281). Runs only on the transition into published: a published release '
    'cannot lose embeddings, and checking on every UPDATE would make rollback '
    'rescan what publishing already scanned.';


-- ---------------------------------------------------------------------------
-- 清除函数：新表要被分类
-- ---------------------------------------------------------------------------
--
-- `043` 的表清单刻意手写（「a table added later must be *classified* by a
-- person」），而 `test_tenant_purge_coverage.py` 会因为漏掉它而红。
--
-- `knowledge_chunk_identity` **随租户删**：它是租户自己的资料的一部分，
-- 没有任何审计行记录「在某个身份之下做的决定」。
--
-- **顺序**：它要排在 `knowledge_chunks` **之后** —— chunk 指向它。
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'delivery_receipts',
        'employee_read_cursors',
        'employee_roles',
        'inbox_events',
        'ingress_replay_keys',
        'manual_tasks',
        'model_invocation_costs',
        'outbox_messages',
        'policy_decisions',
        'intent_records',
        'refresh_tokens',
        'retrieval_evidence',
        'roles',
        'scheduled_events',
        'conversation_aggregates',
        'knowledge_chunks',
        'knowledge_chunk_identity',
        'knowledge_releases',
        'contacts',
        'tenant_cost_ceilings',
        'tenant_manager_contacts',
        'channel_bindings',
        'ws_tickets',
        'employee_sessions',
        'employees'
    ];
    current_table text;
    removed bigint;
BEGIN
    FOREACH current_table IN ARRAY ordered_tables LOOP
        EXECUTE format('DELETE FROM public.%I WHERE tenant_id = $1', current_table)
            USING target_tenant;
        GET DIAGNOSTICS removed = ROW_COUNT;
        table_name := current_table;
        deleted := removed;
        RETURN NEXT;
    END LOOP;
END
$function$;

INSERT INTO schema_migrations(version) VALUES ('052_knowledge_publish')
ON CONFLICT (version) DO NOTHING;
