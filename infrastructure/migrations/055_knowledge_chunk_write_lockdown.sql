-- 055: 已发布的 release 里，chunk 不能再被改动。**T296 到期。**
--
-- ---------------------------------------------------------------------------
-- 它补的是哪一个洞
-- ---------------------------------------------------------------------------
--
-- `052` 的发布闸只在**转 `published` 那一刻**查「chunk 都向量化过了吗」。
-- 那样够便宜，而它成立的全部依据是一句话：**发布之后 chunk 不会再变**。
--
-- **那句话当时没有任何东西守着。** `051` 发的是表级 `UPDATE ON
-- knowledge_chunks`（向量化其实只用得上 `UPDATE (embedding)`），`INSERT` 也
-- 不看 release 的状态。于是：
--
--   * 给一个已发布的 release 再插一条没有向量的 chunk —— 没有东西会红；
--   * 把一条已发布 chunk 的 `embedding` 置空 —— 没有东西会红；
--   * 改掉一条已发布 chunk 的正文 —— `text_hash` 那条 CHECK 会拦，
--     **而连 `text_hash` 一起改就不会**。
--
-- **失败的样子与 T281 说的是同一种**，只是从「发布时漏了」变成「发布后被写脏」：
-- 检索少召回，`retrieval_evidence.outcome` 记的仍然是 `'empty'` ——
-- **和「真的没有匹配上」长得一模一样**（裁定六特意要把这两件事分开）。
--
-- ---------------------------------------------------------------------------
-- 两条路都走，因为它们挡的不是同一件事
-- ---------------------------------------------------------------------------
--
-- 登记 T296 时写了两条候选，**这里两条都做**，而理由是它们的作用面不同：
--
--   * **列级 GRANT** 管的是「这个角色被允许碰哪几列」。它挡掉的是
--     `body` / `text_hash` / `ref` / `slug` 被改，**与 release 的状态无关** ——
--     一条 draft 的 chunk 的正文同样不该被 UPDATE 改（要改就重新导入）。
--     这是 `039` 那条规则的直接应用：**角色拿它的调用方用得到的动词**，
--     而 `import_knowledge.py` 对 chunk 只 UPDATE `embedding` 这一列。
--   * **触发器** 管的是「在什么状态下能写」。它挡掉的是**给已发布的 release
--     追加 chunk**（那是 INSERT，列级 GRANT 完全管不着），
--     以及把已发布 chunk 的向量置空。
--
-- **只做列级 GRANT 会留下 INSERT 那个口子；只做触发器会留下「draft 期间
-- 正文可以被原地改掉」那个口子。** 两条都不是对方的超集。
--
-- ---------------------------------------------------------------------------
-- 为什么触发器不是 BEFORE INSERT 一律拒
-- ---------------------------------------------------------------------------
--
-- **第 5 批在这里踩过一次，形状值得抄在这里。** 那一批的发布闸写成
-- 「BEFORE INSERT 时无条件拒」，而 `knowledge_chunks` 对 `knowledge_releases`
-- 有外键 —— 于是「插入一条 `status='published'` 的 release」在那一刻**必然
-- 0 chunk**，36 条既有用例当场变红。
--
-- 这里的方向是反的（查的是 chunk 那一侧的 release 状态），所以不会同形发作；
-- 写下来是因为**下一个改这个文件的人要知道这一族触发器踩过哪个坑**。

-- ---------------------------------------------------------------------------
-- 这道闸曾经可以被一条临时表关掉（独立审查 2026-09-12，P1-1）
-- ---------------------------------------------------------------------------
--
-- 第一版的函数体里写的是不限定 schema 的 `FROM knowledge_releases`，
-- 而函数没有 `SET search_path`。**`pg_temp` 在没被显式写进 `search_path` 时
-- 是隐式最先搜索的**，建临时表只要数据库的 `TEMP` 权限 —— `PUBLIC` 默认就有。
--
-- 于是以 `fastagent_app`（导入路径那个角色，不需要任何额外权限）：
--
--     CREATE TEMP TABLE knowledge_releases (tenant_id uuid, id uuid, status text);
--     INSERT INTO knowledge_chunks(...);   -- 进一个已发布的 release，没有向量
--
-- 触发器此后查到的是那张空表，`release_status IS NULL`，落进第一版那条
-- 「找不到就放行」—— **实测通过**（阳性对照：不建临时表时同一条 INSERT 被拒）。
--
-- **而第一版给那条 fail-open 写的理由，在它唯一被用到的那种情形下恰好是反的。**
-- 原话是「外键会拒它，而且拒得更准」：**外键按 OID 绑真表，不受 `search_path`
-- 影响** —— `public.knowledge_releases` 里那一行是存在的，外键照过；
-- 只有触发器什么都没看到。「触发器看不到而外键看得到」正是那条分支被走到的
-- **全部**情形。
--
-- **对照 `052`**：它的发布闸也是不限定 schema 地 `FROM knowledge_chunks`，
-- 同样没有 `SET search_path` —— 但它的空结果走 `total = 0 → RAISE`，**fail closed**。
-- 所以这不是「本仓一直这么写」，是**本批把一个一直 fail-closed 的形状写成了
-- fail-open**，而这一批的全部意义正是「不变量要由数据库保证」。
--
-- **两处都改，因为它们各自都不够**：
--
--   * schema 限定 + `SET search_path` 关掉遮蔽这条路；
--   * not-found 改成**拒**，让这条分支即使将来又被走到也不再是一次授权。
--
-- **实测过四种，两种没用 —— 其中一种最像对的**：照抄 `006_profile_registry.sql`
-- 那条不可变触发器的 `SET search_path = pg_catalog, public` **关不掉这个口子**，
-- 因为 `pg_temp` 没被列出来时仍然隐式最先搜。有效的是列出 `pg_temp`
-- （把它排在最后，于是真表先被找到）或直接 schema 限定。**这里两条都写上。**

CREATE OR REPLACE FUNCTION knowledge_chunk_write_lockdown()
RETURNS trigger
LANGUAGE plpgsql
-- **`pg_temp` 必须显式列出来，而且要排在最后。** 不写它不等于不搜它。
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    release_status text;
BEGIN
    -- **`public.` 不能省**，理由见本文件上方那段。`SET search_path` 已经够了，
    -- 这一条是第二道 —— 一个将来被改掉的 `SET` 子句不该能悄悄重开这个口子。
    SELECT status INTO release_status
      FROM public.knowledge_releases
     WHERE tenant_id = NEW.tenant_id AND id = NEW.release_id;

    IF release_status IS NULL THEN
        -- **找不到就拒，不是放行**（P1-1）。真的找不到时外键本来就会拒，
        -- 所以改成拒**只改变报错文字**；而留着放行，就是**把一次查找失败
        -- 翻译成一次授权** —— 上面那条临时表正是这么走进来的。
        RAISE EXCEPTION
            'knowledge_chunks 引用的 release % 查不到（chunk %）',
            NEW.release_id, NEW.ref
            USING ERRCODE = 'raise_exception',
                  HINT = '外键本该先拒掉它。走到这里说明这个函数看到的不是'
                         ' public.knowledge_releases —— 检查 search_path 与临时表遮蔽。';
    END IF;

    IF release_status <> 'draft' THEN
        RAISE EXCEPTION
            'knowledge_chunks 只能在 draft release 上写；release % 是 %（chunk %）',
            NEW.release_id, release_status, NEW.ref
            USING ERRCODE = 'raise_exception',
                  HINT = '内容改了就发新版本：导入一个新 release 再 publish。'
                         '052 的发布闸假设发布后 chunk 不再变，这一条是那个假设的执行者。';
    END IF;

    -- **UPDATE 还要看 `OLD` 那一侧**（独立审查 2026-09-12，P2-1）。
    -- 只看 `NEW` 的话，「把一条**已发布**的 chunk 挪进一个 draft release」
    -- 是过得去的 —— 实测：一个 2 条 chunk 的已发布 release 变成 1 条，
    -- **失败方向与本批要防的一模一样**。
    --
    -- `fastagent_app` 走不到这条路（列级 GRANT 不给 `release_id`，实测
    -- `InsufficientPrivilege`）；**可达的是 owner / migrator，也就是将来的任何一条迁移**。
    -- 而触发器在本批里的职责恰恰是「GRANT 那一半变了之后它还守得住」——
    -- 本批 §二 就是这么论证两条闸互不覆盖的。
    IF TG_OP = 'UPDATE' AND OLD.release_id IS DISTINCT FROM NEW.release_id THEN
        SELECT status INTO release_status
          FROM public.knowledge_releases
         WHERE tenant_id = OLD.tenant_id AND id = OLD.release_id;

        IF release_status IS NULL OR release_status <> 'draft' THEN
            RAISE EXCEPTION
                'chunk % 不能从 release %（%）搬走',
                OLD.ref, OLD.release_id, coalesce(release_status, '查不到')
                USING ERRCODE = 'raise_exception',
                      HINT = '没有任何正当操作需要把 chunk 在 release 之间搬：'
                             '改内容的办法是导入一个新 release。';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION knowledge_chunk_write_lockdown() IS
    'T296: chunks may only be written while their release is draft, on both '
    'sides of an UPDATE. 052''s publish guard checks embeddings once, at the '
    'transition to published; this is what makes that one check remain true '
    'afterwards. search_path is pinned and the lookup is schema-qualified: '
    'without both, a temp table named knowledge_releases silently disables it.';

-- **`DROP ... IF EXISTS` 在前**（独立审查 2026-09-12，P3-1）。本仓两处先例
-- （`006:95`、`052:248`）都这么写，而 `052` 是同一张表族、上一批写的。
-- 同一个文件里函数是 `CREATE OR REPLACE`、marker 是 `ON CONFLICT DO NOTHING`，
-- **三件东西里唯独触发器不是可重跑的**。
--
-- **不是今天会红**（`migrate.py` 按 `schema_migrations` 跳过已应用的版本），
-- 而成本不对称：现在一行，合并之后要补就是一整个 `056`。
DROP TRIGGER IF EXISTS knowledge_chunks_only_writable_while_draft ON knowledge_chunks;
CREATE TRIGGER knowledge_chunks_only_writable_while_draft
    BEFORE INSERT OR UPDATE ON knowledge_chunks
    FOR EACH ROW
    EXECUTE FUNCTION knowledge_chunk_write_lockdown();

-- **列级，替换 `051` 的表级。** `REVOKE` 在前：`GRANT UPDATE (embedding)`
-- 不会把已经发出去的表级 UPDATE 收回来，两者是并存的，
-- **只加不减等于什么都没改**。
REVOKE UPDATE ON knowledge_chunks FROM fastagent_app;
GRANT UPDATE (embedding) ON knowledge_chunks TO fastagent_app;

COMMENT ON TABLE knowledge_chunks IS
    'One retrievable piece of a release (裁定八: 900 wire bytes, top-2). '
    'Written by the import path as fastagent_app while its release is draft. '
    'After publication 055 refuses INSERT and UPDATE -- but NOT DELETE, which '
    'stays open on purpose so purge_tenant_data can delete a whole tenant. '
    'fastagent_app holds no DELETE at all, which is why that exception is safe '
    'today; a future role that gets DELETE would reopen it.';

INSERT INTO schema_migrations(version) VALUES ('055_knowledge_chunk_write_lockdown')
ON CONFLICT (version) DO NOTHING;
