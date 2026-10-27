-- 056: 检索的读路径 —— worker 拿得到知识、写得下证据，以及 T303 到期。
--
-- ---------------------------------------------------------------------------
-- 三件事，每件都有自己的理由
-- ---------------------------------------------------------------------------
--
-- **一、worker 今天碰不到知识库。** `051` 只发给 `fastagent_app`
-- （导入侧），它自己写着「**`retrieval_evidence` —— nothing。它的写者是 worker，
-- 在第 6 批**」。这一批就是那个第 6 批的读路径半边。
--
-- **二、`outcome` 的词表少一态。** ADR-0133（用户 2026-09-12 裁定 (b)）把
-- 「这一轮为什么没有来源」的落点定在 `retrieval_evidence` 上，要求它分得开四态。
-- 今天的词表 `('hit','empty','failed','timeout')` 表达不了其中一态：
-- **「计划步给了一个不可用的请求串」**。
--
-- 为什么不把它记成 `failed` + `degradation`（那样不用动迁移）：
-- **本表自己的 CHECK (c) 已经裁过同型的问题** ——
-- 「供应商侧几毫秒就返回的那种超时错误记成 `failed` 不是 `timeout`，
-- 因为两者该修的地方不同（一个调期限或检索层，一个查供应商/网络），
-- 而把它们混在一个值里正是裁定六要拆开的那件事」。
-- 同一条判据落在这里：**「请求串不可用」修的是提示词，「检索失败」修的是检索层或供应商**。
-- 混进 `failed` 会让「按 outcome 分组」这个最常用的读法把两件事算成一件。
--
-- **三、T303 到期。** `052` 的发布闸函数 `knowledge_release_requires_embeddings()`
-- 里写的是不限定 schema 的 `FROM knowledge_chunks`，函数也没有 `SET search_path`
-- （`055` 的同族函数已经两样都有）。T303 的到期条件逐字是「**第 6 批（检索层）开工时，
-- 或下一次往这一族加触发器时**」—— 就是现在。它今天不是缺陷（空结果走
-- `total = 0 → RAISE`，fail closed），**而它的安全性全靠当时那条分支恰好写对了方向**。
--
-- ---------------------------------------------------------------------------
-- 这一批**不做**什么
-- ---------------------------------------------------------------------------
--
-- **原打算不动成本台账、也不接管线**（那样这一条迁移就只有读路径这一半）。
-- **用户 2026-09-12 要求「计量和接线一批交」，所以 `059` 与 6b-2 同批落了** ——
-- `059` 给 `model_invocation_costs.role` 加 `'EMBEDDING'`，嵌入调用走同一套台账，
-- 管线在 plan 与 generate 之间调检索。**这一段先前写的是「本批不做这两件」，
-- 而本批做了**（独立审查 2026-09-12，P2-3：迁移文件是这个仓里最常被后来者
-- 当成事实读的东西，一段说「本批不做 X」而本批做了 X 的文件头，下一个人会照它办）。
-- 缘由与残留登记在 **T315**（已关闭）。
--
-- **不发 `UPDATE` 给 worker。** 裁定四那三列（`materials_available` /
-- `quoted_side` / `retrieved_unused`）是生成之后才知道的，归 6c；
-- 那一批要 `UPDATE`，那一批再发。

-- ---------------------------------------------------------------------------
-- 一、`outcome` 加 'unusable_request'
-- ---------------------------------------------------------------------------
--
-- 用 `DROP CONSTRAINT IF EXISTS` + `ADD CONSTRAINT` 的写法，与
-- `010_cost_unreconciled_status.sql` 同一个形状（那一批给 `status` 加
-- `'unreconciled'` 时就是这么做的），约束名用 PostgreSQL 给内联 CHECK 的默认名。
ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_outcome_check;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_outcome_check
    CHECK (outcome IN ('hit', 'empty', 'failed', 'timeout', 'unusable_request'));

-- **新的一态要和既有那几条 CHECK 一样严。** 一条 `unusable_request` 的行说的是
-- 「平台根本没去检」，所以三件事必须同时成立，否则它就是一条会污染计数的脏行：
--
--   * `hits` 空 —— 没去检，不可能有命中；
--   * `degradation` 非空 —— 「为什么不可用」是这一态唯一的信息量，
--     少了它这条行只说得出「有事发生过」（与 `failed` / `timeout` 同一条理由）；
--   * `release_id` 空 —— 没去检就没读过任何一版。
--     **这一条与 CHECK (a) 是对偶的**：那一条说「命中必须记读了哪一版」，
--     这一条说「没去检不许记读过哪一版」。
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_unusable_request_shape
    CHECK (
        outcome <> 'unusable_request'
        OR (
            jsonb_array_length(hits) = 0
            AND degradation IS NOT NULL
            AND release_id IS NULL
        )
    );

COMMENT ON COLUMN retrieval_evidence.outcome IS
    'hit / empty / failed / timeout / unusable_request. 第五态由 ADR-0133 '
    '(2026-09-12) 加入: 计划步给了一个平台用不了的请求串 (空白、只有空格、'
    '超出检索层自己的长度上界), 平台因此没有去检。它与 failed 分开, 因为两者该修的'
    '地方不同 -- 这一态修提示词, failed 修检索层或供应商 -- 与本表 CHECK (c) '
    '把 timeout 从 failed 里拆出来是同一条判据。'
    ' **empty 的两种读法, 判别式是 release_id** (独立审查 2026-09-12 P2-2): '
    'release_id IS NULL = 这个租户压根没有已发布的语料 (去导入 + 发布); '
    'release_id IS NOT NULL = 检了没检到 (补语料或调检索)。'
    '**后者今天不可达**: 052 的发布闸拒收「有 chunk 没向量化」与「零 chunk」的 release, '
    '而检索不设分数下界 -- 所以一版 published release 必然至少返回一条。'
    '哪天有人给检索加了分数下界 (6c 可能做), 它就可达了, 而判别式已经在数据里。';

-- ---------------------------------------------------------------------------
-- 二、worker 的读路径与写证据
-- ---------------------------------------------------------------------------
--
-- **读侧只发 SELECT。** worker 不该能改知识库的任何一行：导入与发布是 `app`
-- 那一侧的事（`051` / `052` / `055` 三批已经把那一侧收到列级）。
GRANT SELECT ON knowledge_releases TO fastagent_worker;
GRANT SELECT ON knowledge_chunks TO fastagent_worker;

-- **写侧是列级 INSERT，不是表级。** 与 `055` 同一条理由：**表级 GRANT 发的是
-- 「这张表你能写」，列级发的是「这几列你能写」**，而裁定四那三列此刻**不该**
-- 被写者碰到 —— 它们在生成之后才有值，归 6c。
-- 一条表级 `INSERT` 会让 6b 的写者有能力在检索那一刻就把
-- `quoted_side` 填上一个当时还不可能知道的值，**而那条行读起来是一次真实测量**。
GRANT SELECT ON retrieval_evidence TO fastagent_worker;
GRANT INSERT (
    tenant_id, id, conversation_id, correlation_id,
    query_hash, release_id, retriever_version,
    outcome, deadline_ms, elapsed_ms, degradation, hits
) ON retrieval_evidence TO fastagent_worker;

-- ---------------------------------------------------------------------------
-- 三、T303：`052` 那个函数钉住 search_path 并限定 schema
-- ---------------------------------------------------------------------------
--
-- 函数体一个字没改，只加 `SET search_path` 与 `public.` 限定。
-- **`pg_temp` 显式列在最后**：不写它等于让 PostgreSQL 把它隐式放在**最前**，
-- 而建临时表只要 `TEMP` 权限（`PUBLIC` 默认就有，全仓没有任何一条
-- `REVOKE TEMPORARY` 收过）。**这与 `055` 的那个函数逐字同形。**
CREATE OR REPLACE FUNCTION knowledge_release_requires_embeddings()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $function$
DECLARE
    missing bigint;
    total bigint;
BEGIN
    SELECT count(*) FILTER (WHERE embedding IS NULL), count(*)
      INTO missing, total
      FROM public.knowledge_chunks
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

INSERT INTO schema_migrations(version) VALUES ('056_knowledge_retrieval_read_path')
ON CONFLICT (version) DO NOTHING;
