-- 050: the knowledge base's three tables (知识库第 3 批).
--
-- **What this migration is, and what it deliberately is not.** The platform's
-- first version does four things (裁定二, `docs/plans/knowledge-base-schedule-2026-09-11.md`
-- §0.1): receive already-chunked material, vectorise it, publish and version it,
-- and retrieve it while leaving evidence. **All content preparation is done by
-- hand** -- no import pipeline, no object store, no chunker, no quality gate.
-- This file is the storage those four things sit on, and nothing else: **there
-- is no writer yet**, and therefore no GRANT (see the bottom of this file).
--
-- `KnowledgeDocument` from the RFC's data contract (`conversation-engine.md:751`)
-- **is not built** -- 裁定二 removed the thing it would describe. Its two useful
-- columns survive as `knowledge_chunks.document_ref` / `document_version`,
-- filled from the authoring spec (§四.1 of the schedule: "留空是最坏的选择
-- ——第二版建表时就没有回填的依据了").

-- ---------------------------------------------------------------------------
-- 整理规格的「可机检五条」，逐条写明谁守 —— 以及今天没人守的那几条
-- ---------------------------------------------------------------------------
--
-- `docs/plans/knowledge-chunk-authoring-spec-2026-09-11.md` §一 列了五条
-- 「全部能被脚本判定，所以它们是『拒绝』而不是『建议』」。
-- **独立审查 2026-09-11 P2-4 点出本文件先前只提了其中三条，而且用的是现在时**
-- —— 读起来像那些规则已经有人守，实际没有。逐条列在这里，
-- **每条要么有归属，要么有到期批次**：
--
--   #  规则                      谁守
--   ─  ────────────────────────  ────────────────────────────────────────────
--   1  正文 ≤ 861 字节（线路）    **今天没人守**。本文件那条 CHECK 是有意偏弱的
--                                下界（见 `body` 那一段）；真规则要量真正的线路
--                                串，归属是**第 4 批的导入器**
--   2  自足（五个禁用词）         **三个在本文件**（`如前所述` / `见上一节` /
--                                `前面提到`，见 `body` 下面那条 CHECK）；
--                                `上文` / `如上` 是子串、没有分词器分不开，
--                                归属**第 4 批的导入器**
--   3  四个字段齐全               **本文件**：`question` / `content_type` /
--                                `confidence` 都是 NOT NULL 且有长度下界，
--                                `source` 见第 4 条
--   4  至少一个 source，带         **本文件**：`source_spans` 那条 CHECK，
--      `file` 与 `section`        逐元素查形状与非空
--   5  ref 三段合法               **`runtime.references.reference_refusal`**，
--                                唯一定义点。本文件只查 `ref`/`slug`/
--                                `chunk_version` 三列自洽，**不重写语法**
--
-- **第 1 与第 2 条那半边今天真的没有执行者**，而不是「在别处守着」。
-- 写清楚的理由与整份文件一致：一个读者会据此以为规则成立，并在它之上做决定。
--
-- 规格里**不**可机检的部分（资料从哪来、该不该进库、内容对不对）本来就是
-- 内容窗口的判断，不在这张表里。

-- ---------------------------------------------------------------------------
-- pgvector: what 2048 dimensions actually allow, measured rather than assumed
-- ---------------------------------------------------------------------------
--
-- **`001_m0.sql` line 1 has created the `vector` extension since M0 and nothing
-- has ever used it.** This is its first user, so the constraints were measured
-- on the image this repository actually runs (`pgvector/pgvector:pg16`,
-- extension version **0.8.6**) before a column type was chosen:
--
--   CREATE INDEX … USING hnsw    (e vector_cosine_ops)  → ERROR: column cannot
--                                   have more than 2000 dimensions for hnsw index
--   CREATE INDEX … USING ivfflat (e vector_cosine_ops)  → ERROR: … for ivfflat index
--   CREATE INDEX … USING hnsw    (h halfvec_cosine_ops) → OK   (halfvec limit 4000)
--   CREATE INDEX … USING hnsw    ((e::halfvec(2048)) halfvec_cosine_ops) → OK,
--                                   and `EXPLAIN` uses it (Index Scan using …)
--
-- **The embedding is 2048-dimensional** -- measured, not read off a document:
-- `evidence/knowledge/embedding-dimension-2026-09-11.md` records the live call
-- that returned a 2048-float vector from `doubao-embedding-vision-251215`.
--
-- So the RFC's §11.3 plan -- "先 exact search，数据量和 latency 证据达到阈值后才
-- 启用 HNSW" -- **is not a choice that stays available for free at this
-- dimension**: an approximate index over a `vector(2048)` column is refused
-- outright by pgvector. The RFC anticipated exactly this at `:728`
-- ("维度必须能由当前 PostgreSQL + pgvector 列类型及目标索引方法支持"), which is
-- why it is worth writing down where the column is declared rather than
-- discovering it in the batch that needs the index.
--
-- **The column stays `vector(2048)`, full precision, and the door stays open.**
-- The third measurement above is the reason: the documented pgvector escape for
-- >2000 dimensions is an *expression* index over a `halfvec` cast, and it needs
-- no column change and no re-embedding. The alternative -- storing `halfvec`
-- (float16) now -- would buy an index nobody has measured a need for and pay
-- for it by throwing away precision on every row, permanently, before a single
-- recall measurement exists.
--
-- Storage, also measured: `pg_column_size` is **8200 bytes** for
-- `vector(2048)` and 4104 for `halfvec(2048)`. pgvector declares both types
-- `EXTERNAL` (`pg_type.typstorage = 'e'`), so the vector always lives out of
-- line in TOAST and never competes with the row for the 8160-byte tuple limit.

CREATE TABLE IF NOT EXISTS knowledge_releases (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,

    -- **An independent lifecycle, deliberately unlike `profile_releases`**
    -- (裁定五). Editing one piece of reference material must not republish the
    -- merchant's scripted words: `materials.json` folds into the profile's
    -- `content_hash` (`registry.py`, `MATERIALS_FILENAME` / `MATERIAL_TEXTS_KEY`),
    -- so changing one word there supersedes the profile release. That is right
    -- for scripts -- a merchant approved them word by word, and changing one is
    -- a product decision -- and wrong for reference material, which is added to
    -- continuously. Folding them together would make the script's version
    -- number follow the material's rhythm.
    version integer NOT NULL CHECK (version > 0),

    status text NOT NULL CHECK (status IN ('draft', 'published', 'superseded')),

    -- The four things an embedding release must freeze (RFC `:728`). They are
    -- columns rather than a comment because "any model or dimension change
    -- creates a new KnowledgeRelease and rebuilds the index" is only checkable
    -- if the old value is written down.
    embedding_model text NOT NULL CHECK (length(embedding_model) BETWEEN 1 AND 128),
    -- **2048 is not a policy here, it is the column type restated.**
    -- `knowledge_chunks.embedding` is `vector(2048)`, a fixed-width type: a
    -- release declaring any other dimension could not store its own chunks.
    -- Saying so here turns that contradiction from latent into impossible, and
    -- names the fact that changing the embedding model to one of another
    -- dimension is a *migration*, not a data change.
    embedding_dimension integer NOT NULL CHECK (embedding_dimension = 2048),
    distance_metric text NOT NULL CHECK (distance_metric IN ('cosine', 'l2', 'inner_product')),
    normalization text NOT NULL CHECK (normalization IN ('none', 'l2')),
    truncation_policy text NOT NULL CHECK (length(truncation_policy) BETWEEN 1 AND 256),

    -- **The chunker, which today is a person plus a written specification**
    -- (裁定二). `docs/plans/knowledge-chunk-authoring-spec-2026-09-11.md` is that
    -- specification; this column records which version of it a release was cut
    -- under. When the second version automates chunking, what changes is where
    -- this value comes from, not whether it exists.
    authoring_spec text NOT NULL CHECK (length(authoring_spec) BETWEEN 1 AND 256),

    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),

    -- Publish / rollback evidence (RFC `:752`). Free-shaped on purpose: what a
    -- publication needs to prove is not settled, and a column per field would
    -- freeze a guess. **`object`, not `any`** -- the same bound `001` puts on
    -- every other jsonb here, so a reader can index into it without checking
    -- the type first.
    publication_evidence jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(publication_evidence) = 'object'),

    -- **Which release this publish displaced, when the publish was a rollback.**
    -- Rollback is "re-point the current release at an older one" (裁定七 §5.4.1
    -- 第 2 条), so it leaves no other trace: the old release simply becomes
    -- published again and nothing records that it was a step backwards rather
    -- than a step forwards. Without this column, "we rolled back on the 11th"
    -- is answerable only by reading timestamps and guessing.
    rolled_back_from uuid,

    published_at timestamptz,
    superseded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, version),

    -- **一个租户永远不能再发一个内容相同的 release，这是有意的**
    -- （独立审查 2026-09-11 P3-3 要求写明）。它抄的是 `profile_releases`
    -- （`001`）的同名约束，理由也相同：两个内容一样的 release 之间没有任何
    -- 事实上的差别，而它们会让「当时读的是哪一版」多出一个无意义的分支。
    --
    -- **第 5 批会撞上它，而撞上时正确的做法是回滚不是重发**：改了一版又改回来，
    -- 内容与某个历史 release 相同 —— 那一刻要的本来就是
    -- 「把旧的那一版重新置为当前」（裁定七 §5.4.1 第 2 条），
    -- 那条路不产生新行，所以这条约束拦不着它。**发布路径要把这件事认出来**，
    -- 而不是绕过约束去造一个内容重复的新 release。
    UNIQUE (tenant_id, content_hash),
    FOREIGN KEY (tenant_id, rolled_back_from) REFERENCES knowledge_releases(tenant_id, id),

    -- Anything past draft has been published once, and a superseded release is
    -- one that was. **`superseded_at` is cleared by a rollback**, which is what
    -- makes the pair a state rather than a log -- the log is `rolled_back_from`.
    CHECK (status = 'draft' OR published_at IS NOT NULL),
    CHECK ((status = 'superseded') = (superseded_at IS NOT NULL)),
    CHECK (rolled_back_from IS NULL OR rolled_back_from <> id)
);

-- **At most one published release per tenant, enforced rather than agreed.**
-- 裁定七 made every retrieval read "the currently published one", so two of them
-- is not a degraded state -- it is a question with no answer, and the retrieval
-- layer would resolve it by whatever `ORDER BY` it happened to carry.
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_releases_one_published_idx
    ON knowledge_releases (tenant_id) WHERE status = 'published';

ALTER TABLE knowledge_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_releases FORCE ROW LEVEL SECURITY;
DO $knowledge_releases_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'knowledge_releases'
           AND policyname = 'knowledge_releases_tenant_isolation'
    ) THEN
        CREATE POLICY knowledge_releases_tenant_isolation ON knowledge_releases
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$knowledge_releases_rls$;

COMMENT ON TABLE knowledge_releases IS
    'One published version of a tenant''s reference material (裁定五). Its '
    'lifecycle is independent of profile_releases: adding a piece of material '
    'does not republish the merchant''s scripted words. At most one release is '
    'published at a time, and every retrieval reads that one (裁定七).';


CREATE TABLE IF NOT EXISTS knowledge_chunks (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    release_id uuid NOT NULL,

    -- RFC `:753`'s 「document/version」. There is no `KnowledgeDocument` table
    -- (裁定二) and these are **not** left empty: a hand-written piece of
    -- material still came from some original at some version, and today that
    -- fact is recorded in the authoring spec rather than in a table. When the
    -- table arrives, these are what it is back-filled from.
    document_ref text NOT NULL CHECK (length(document_ref) BETWEEN 1 AND 512),
    document_version text NOT NULL CHECK (length(document_version) BETWEEN 1 AND 64),

    ordinal integer NOT NULL CHECK (ordinal >= 0),

    -- ---------------------------------------------------------------------
    -- The reference, and why its grammar is **not** repeated here
    -- ---------------------------------------------------------------------
    --
    -- `ref` is the whole three-segment string `knowledge:<slug>:v<N>`
    -- (`docs/plans/knowledge-chunk-authoring-spec-2026-09-11.md` §四), and it is
    -- stored whole because that is what it is used as: a dictionary key, the
    -- thing the model writes back when it cites a chunk.
    --
    -- **There is no CHECK spelling out the grammar, and that is the same
    -- decision `runtime/references.py` already made** about JSON Schema: 「写进去
    -- 就是两个定义点 …… `pattern` 与本函数一旦漂开，漂开的那一刻没有任何东西会红」.
    -- A regex here would be a third copy of a rule that has exactly one owner
    -- (`reference_refusal`), and the copy in the database is the one nobody
    -- would think to update.
    --
    -- **What the database can own honestly is that the three columns agree.**
    -- `slug` and `chunk_version` are stored separately because the retrieval
    -- layer groups by slug and the evidence row records "which revision of the
    -- text was quoted" -- neither should have to re-parse the string. The CHECK
    -- below says only that they were split from this `ref` and not from some
    -- other one; it states no opinion about what a legal slug looks like.
    --
    -- It does forbid `v01` as a side effect (`1::text` is `'1'`, so
    -- `'…:v' || 1` can never equal `'…:v01'`). **That is a consequence, not a
    -- second copy of §四's no-leading-zeros rule** -- if the spec ever allowed
    -- `v01`, this constraint would not silently disagree, it would make the two
    -- spellings impossible to tell apart, which is the failure the spec names.
    ref text NOT NULL,
    slug text NOT NULL CHECK (length(slug) BETWEEN 1 AND 128),
    chunk_version integer NOT NULL CHECK (chunk_version > 0),

    -- The four fields the authoring spec §一 requires of every entry. They are
    -- columns because otherwise the importer has nowhere to put four of the
    -- seven fields every authored chunk already carries.
    --
    -- **No CHECK against a value list**, though `kb-v1.json` declares one for
    -- each (`type_values`, `confidence_values`). That vocabulary belongs to the
    -- content window and will grow; freezing it in a migration would mean a
    -- migration every time an editorial category is added. The validator on the
    -- way in owns it -- the same split as the wire budget below.
    --
    -- `confidence` is not decoration: 「当时的说法，可能已过期」 and 「已实测」
    -- are chunks the retrieval layer should not treat alike.
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 512),
    content_type text NOT NULL CHECK (length(content_type) BETWEEN 1 AND 64),
    confidence text NOT NULL CHECK (length(confidence) BETWEEN 1 AND 64),

    -- ---------------------------------------------------------------------
    -- `bounded text`, and the bound this column can and cannot be
    -- ---------------------------------------------------------------------
    --
    -- The ruling is **900 bytes on the wire, top-2** (裁定八), where "the wire"
    -- is the exact string that enters the prompt:
    --
    --     json.dumps({"ref": ref, "text": body},
    --                ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    --
    -- **The database cannot compute that string**, and must not pretend to:
    -- PostgreSQL's JSON output uses its own separators, and JSON escaping (`"`,
    -- `\`, newline each cost an extra byte) depends on the body's contents.
    -- So the authority belongs where the spec put it (§2.3: 「只写一处，别把 861
    -- 抄进第二个地方」) -- in one Python validator that measures the real string.
    --
    -- **That validator does not exist yet, and this comment used to say it did**
    -- (独立审查 2026-09-11 P2-4). 现在全仓没有任何一处算 900 或线路预算 --
    -- `references.py` 只管三段语法。**它的归属是第 4 批的导入器**（接收 chunk +
    -- 向量化），那一批是第一个有写入方的批次。
    -- 在那之前，这条规则的唯一执行者是下面这个**有意偏弱的下界**，
    -- 而「有人守着」与「没人守着」对读这段注释的人是完全不同的两件事。
    --
    -- **What this CHECK is, precisely: a lower bound on the wire size.** The
    -- skeleton `{"ref":"","text":""}` is 20 bytes -- measured, not counted by
    -- eye -- so any wire-legal chunk satisfies `20 + ref + body <= 900`.
    -- Escaping only ever adds, so this constraint **cannot reject a chunk the
    -- real rule permits**, which is the property that matters: 「一条新的
    -- admission 规则如果拒掉在架的包，代价是封不了版」.
    --
    -- **And it derives rather than copies.** The spec's 861 is `900 - 39` where
    -- 39 assumes a 19-byte ref; past `kb-999` the ref grows and 861 becomes
    -- wrong. This expression uses the row's own ref, so it moves with it.
    --
    -- **It is therefore weaker than the real rule, on purpose, and a body full
    -- of quotes can pass here and still be over 900 on the wire.** Measured on
    -- the 55 authored chunks today: escaping costs exactly 0 bytes on all 55,
    -- so nothing currently distinguishes the two rules -- which is precisely
    -- why the difference has to be written down instead of discovered later.
    --
    -- **`octet_length`, not `length`.** `length('请问你们这个产品怎么收费')` is
    -- 12 and `octet_length` is 36: the neighbouring migrations bound text in
    -- characters (`039_contacts.sql`'s `external_user_id`), and copying that
    -- idiom here would have permitted a body three times over budget.
    body text NOT NULL CHECK (octet_length(body) >= 1),
    CHECK (20 + octet_length(ref) + octet_length(body) <= 900),

    text_hash text NOT NULL CHECK (text_hash ~ '^[0-9a-f]{64}$'),

    -- ---------------------------------------------------------------------
    -- Full-text, and the half of it that does not work in Chinese
    -- ---------------------------------------------------------------------
    --
    -- RFC §11.3 pairs vector search with 「PostgreSQL full-text」. Measured on
    -- this image, with no segmenter installed:
    --
    --   to_tsvector('simple', '请问你们这个产品怎么收费？发货一般要几天？')
    --     → '发货一般要几天':2 '请问你们这个产品怎么收费':1
    --   … @@ plainto_tsquery('simple', '收费')                    → false
    --   … @@ plainto_tsquery('simple', '请问你们这个产品怎么收费')  → true
    --
    -- **A run of Chinese between two punctuation marks becomes one token**, so
    -- for Chinese prose this is whole-sentence exact match, not retrieval.
    -- `english` behaves identically -- the stemmer never sees a word boundary
    -- to work with.
    --
    -- **It is kept because the other half does work**, and this corpus is full
    -- of it: these chunks quote file paths and symbol names, and those tokenise
    -- properly --
    --   to_tsvector('simple', 'profile 的 content_hash 由 registry.py 算出')
    --     → 'content':3 'hash':4 'profile':1 'registry.py':6 '由':5 '的':2 '算出':7
    -- so "which chunk mentions `content_hash`" is a query this column answers
    -- and the vector does not. **What it must not be read as is the Chinese
    -- half of hybrid retrieval**; that needs a segmenter, and buying one is a
    -- decision with evidence behind it, not a default.
    --
    -- **`'simple'` is spelled out, and it has to be**: the one-argument
    -- `to_tsvector(body)` is only STABLE (it reads `default_text_search_config`)
    -- and PostgreSQL refuses it in a generated column -- measured:
    -- `ERROR: generation expression is not immutable`. Pinning the config is
    -- therefore not a preference; a stored tsvector has to name one.
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', body)) STORED,

    -- **Nullable, because batch 3 is storage and batch 4 is vectorisation.**
    -- A chunk exists from the moment it is received; it acquires an embedding
    -- when something calls the model. NOT NULL here would mean the receiving
    -- half could not ship without the vectorising half, and `evidence/knowledge/
    -- embedding-dimension-2026-09-11.md` measured why that is not a small job:
    -- the endpoint takes **one chunk per HTTP call**, so N chunks is N calls.
    embedding vector(2048),

    -- RFC `:753`'s 「source span」, in the hand-kept form the authoring spec §六
    -- fixes: `[{"file": …, "section": …}]`, repo-relative path and a string that
    -- can be found inside that file.
    --
    -- **At least one, and both keys present and non-empty** -- spec §一 第 4 條
    -- ("没有来源的条目不能进库"). This *is* checked here, unlike the grammar and
    -- the wire budget, because it has no other owner and the database can
    -- compute it exactly. It is the last hop of the exit criterion's three-hop
    -- traceback (回复 → chunk → 原文的哪一段); an entry that cannot answer the
    -- third hop cannot support a citation at all.
    --
    -- **每个元素必须自己就是对象** -- 独立审查 2026-09-11 P3-1。先前
    -- `[[{"file":"a.py","section":"s"}]]`（嵌套数组）**照收**：jsonpath 的 lax
    -- 模式会把内层数组摊平，于是里面那个对象通过了检查，而**元素本身不是对象**。
    --
    -- **`strict` 前缀不是修饰，它就是修法**（实测，两种模式逐形状比过）：
    --
    --     '$[*] ? (@.type() != "object")'          嵌套数组 → f（漏）
    --     'strict $[*] ? (@.type() != "object")'   嵌套数组 → t（拦住）
    --     两种模式对 `[1,2,3]` / `["a"]` 都是 t，对合法形状都是 f
    --
    -- **下面那条 file/section 的检查仍然是 lax，这是有意的**：strict 模式下
    -- 「元素缺 `file` 键」是一个结构性错误，而 `@?` 会把错误吞掉当成 false --
    -- 缺键那一种反而不拦了。上面这条 strict 先把非对象元素挡在外面，
    -- 所以轮到下面那条时数组里只剩对象，lax 的摊平没有东西可摊。
    -- 注释当时写的是「At least one, and both keys present and non-empty」，
    -- 读起来是对每个元素的形状要求 -- **而它不是**。
    -- 第 4 批的导入器按 `spans[0]["file"]` 读就会炸。
    --
    -- What is **not** checked: that the `section` string is actually findable in
    -- that file. That requires reading the repository, which a CHECK cannot do
    -- and a migration should not want to.
    source_spans jsonb NOT NULL CHECK (
        jsonb_typeof(source_spans) = 'array'
        AND jsonb_array_length(source_spans) >= 1
        AND NOT (source_spans @? 'strict $[*] ? (@.type() != "object")')
        AND NOT (source_spans @? '$[*] ? (!(@.file.type() == "string") || !(@.section.type() == "string") || @.file == "" || @.section == "")')
    ),

    -- ---------------------------------------------------------------------
    -- 自足：整理规格 §一 第 2 条，**五个词里守得住三个**
    -- ---------------------------------------------------------------------
    --
    -- 「正文不得出现『如前所述 / 见上一节 / 上文 / 前面提到 / 如上』-- 检索按条
    -- 取，取到一条就要能用」。独立审查 2026-09-11 P2-4 点名它：它是规格自列的
    -- 「可机检五条」之一，而它先前**既不在库里、也不在下面那张「不由数据库守」
    -- 的表里**。
    --
    -- **三个守得住，两个守不住，而这条界线是量出来的不是估的。**
    -- `如前所述` / `见上一节` / `前面提到` 都是四字定语，拿不出一个把它们当子串
    -- 含进去的正常词。**`上文` 与 `如上` 不行**：「以上文档说明……」含 `上文`，
    -- 「如上海分公司……」含 `如上`。中文没有词边界，而 PostgreSQL 这一侧没有
    -- 分词器（同一个文件上面那段 tsvector 注释量过同一件事）。
    -- **拒掉一条合法 chunk 的代价是发不了版**，所以这两个留给导入器 --
    -- 那里有分词，也有整句上下文。
    --
    -- **在架 55 条实测：五个词全部零命中**，所以这条闸今天不拒任何东西。
    CHECK (body !~ '如前所述|见上一节|前面提到'),

    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, release_id) REFERENCES knowledge_releases(tenant_id, id),

    -- One revision of each slug per release, and one chunk per position.
    UNIQUE (tenant_id, release_id, slug),
    UNIQUE (tenant_id, release_id, ordinal),

    CHECK (ref = 'knowledge:' || slug || ':v' || chunk_version::text)
);

-- Every retrieval is 「this tenant's currently published release」 (裁定七), so
-- that is the leading pair. Exact search reads all of a release's chunks, which
-- makes this the index that keeps it from reading every release's.
CREATE INDEX IF NOT EXISTS knowledge_chunks_release_idx
    ON knowledge_chunks (tenant_id, release_id, ordinal);

-- GIN, not GiST: this is a stored tsvector that is written once per release and
-- read by every retrieval.
CREATE INDEX IF NOT EXISTS knowledge_chunks_tsv_idx
    ON knowledge_chunks USING gin (tsv);

-- **No vector index, and the header says why**: pgvector refuses both HNSW and
-- ivfflat above 2000 dimensions. Exact search over one release's few dozen rows
-- is what RFC §11.3 asks for first in any case; the measurement that opens the
-- approximate path when there is evidence for it is in the header.

ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_chunks FORCE ROW LEVEL SECURITY;
DO $knowledge_chunks_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'knowledge_chunks'
           AND policyname = 'knowledge_chunks_tenant_isolation'
    ) THEN
        CREATE POLICY knowledge_chunks_tenant_isolation ON knowledge_chunks
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$knowledge_chunks_rls$;

COMMENT ON TABLE knowledge_chunks IS
    'One hand-authored piece of reference material, already chunked (裁定二). '
    'Carries every field RFC :753 names, including the ones a KnowledgeDocument '
    'table would otherwise supply. The 900-byte wire budget (裁定八) is only '
    'lower-bounded here; its authority is the validator that measures the real '
    'prompt string.';

COMMENT ON COLUMN knowledge_chunks.tsv IS
    'Stored full-text vector over the body, config pinned to ''simple''. '
    'Useful for the ASCII identifiers these chunks quote; for Chinese prose it '
    'tokenises a whole punctuation-delimited run as one term and is not a '
    'retrieval mechanism. See the migration comment.';


CREATE TABLE IF NOT EXISTS retrieval_evidence (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,

    -- **One row per turn in which the plan step asked for a retrieval**
    -- (裁定三: retrieval is the pipeline's fourth step, decided during plan).
    -- `correlation_id` is the turn, the same one `model_invocation_costs` and
    -- `inbox_events` carry, which is what makes the two counts 裁定三 §5.2.1 第 3
    -- 条 and 裁定六 ask for computable:
    --
    --   * 「本轮没请求检索」 = a turn whose `correlation_id` has a
    --     `PLAN_CAPABILITY` cost row and **no row here**;
    --   * 「请求了却没成」   = a row here with `outcome IN ('failed','timeout')`.
    --
    -- Together those explain why a conversation cited nothing -- and they have
    -- to be separable, because one is fixed in the prompt and the other in the
    -- retriever.
    --
    -- **The alternative was a row on every turn**, which would make the first
    -- count a `WHERE` instead of an anti-join. It was not taken: retrieval is
    -- expected to fire on a minority of turns, and a row per turn charges every
    -- conversation for a feature most turns do not use. The cost of the choice
    -- is written here so the next person can reverse it knowingly: **absence of
    -- a row cannot distinguish "the plan did not ask" from "the worker died
    -- before writing one"**.
    correlation_id uuid NOT NULL,

    -- 裁定三 第 2 条: the plan step supplies the query string, because only it
    -- knows what to look for -- the customer's own words are often useless as a
    -- query (「那这个呢」 retrieves nothing). The hash rather than the text: this
    -- is an audit row, and the query is derived from customer content.
    query_hash text NOT NULL CHECK (query_hash ~ '^[0-9a-f]{64}$'),

    -- **RFC `:754` calls this 「release pin」 and after 裁定七 nothing is
    -- pinned.** The name is kept -- renaming it would mean editing the RFC's
    -- field table for no behavioural gain -- but its meaning changed and must
    -- be stated, or the next reader will assume a pin exists and conclude that
    -- a retrieval can be replayed. **It records which release this retrieval
    -- read**: a fact after the event, not a constraint before it. The same
    -- conversation may retrieve against a different release tomorrow, and
    -- 「当时答的是什么」 is answerable while 「今天重放得到同样结果」 is not.
    --
    -- Nullable: a retrieval that timed out before reading a release has none.
    release_id uuid,

    retriever_version text NOT NULL CHECK (length(retriever_version) BETWEEN 1 AND 64),

    -- 裁定六: a failed or timed-out retrieval **does not stop the turn** -- the
    -- customer still gets a message, without sources, and `evidence_required`
    -- makes it say it is not sure. This row is the other half of that ruling:
    -- without it, that turn is indistinguishable from one where the model never
    -- thought to look.
    outcome text NOT NULL CHECK (outcome IN ('hit', 'empty', 'failed', 'timeout')),

    -- RFC `:754`'s 「deadline/degradation」.
    deadline_ms integer NOT NULL CHECK (deadline_ms > 0),
    elapsed_ms integer NOT NULL CHECK (elapsed_ms >= 0),
    degradation text CHECK (degradation IS NULL OR length(degradation) BETWEEN 1 AND 256),

    -- RFC `:754`'s 「chunk IDs/scores/source spans」. The source spans are copied
    -- in rather than joined out, because the three-hop traceback has to survive
    -- the chunk being superseded -- a citation from last week must still resolve
    -- to the passage that was actually quoted.
    hits jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(hits) = 'array'),

    -- ---------------------------------------------------------------------
    -- 裁定四 · 素材优先，但要留记录
    -- ---------------------------------------------------------------------
    --
    -- **Three columns, and the shape is 「co-occurrence + which side was
    -- quoted」, not 「conflict」.** The platform can record that both sources were
    -- available and which one the model cited; it cannot record whether they
    -- contradict each other. Deciding that means understanding two pieces of
    -- prose, which is the model's job -- and a platform that did it anyway
    -- would be generating a factual conclusion with no source, the exact thing
    -- this whole mechanism exists to forbid.
    --
    -- These three are worth their columns because they are jointly actionable:
    -- `quoted_side = 'material'` together with `retrieved_unused > 0`, seen
    -- repeatedly, means either retrieval is doing useless work or the scripted
    -- words are suppressing something that should be said -- both need a person
    -- to look, and today there is not even the evidence to look at.
    --
    -- **All three are nullable and arrive together, after generation.** This row
    -- is written when the retrieval happens, which is before the model has
    -- chosen what to quote. NULL therefore means "this turn did not reach
    -- generation", which is a real outcome and not a missing write.
    materials_available boolean,
    quoted_side text CHECK (quoted_side IN ('material', 'knowledge', 'neither')),
    retrieved_unused integer CHECK (retrieved_unused >= 0),

    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    -- One retrieval per turn -- 裁定三 puts the decision in the plan step, which
    -- runs once. This is also what makes "turns with no row" a countable thing
    -- rather than an approximation.
    UNIQUE (tenant_id, correlation_id),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id),
    FOREIGN KEY (tenant_id, release_id) REFERENCES knowledge_releases(tenant_id, id),

    -- The outcome and the hits must agree, in both directions. A 'hit' with an
    -- empty array and an 'empty' with results are each a writer bug that would
    -- otherwise corrupt exactly the counts this table exists to produce.
    CHECK (outcome <> 'hit' OR jsonb_array_length(hits) > 0),
    CHECK (outcome <> 'empty' OR jsonb_array_length(hits) = 0),
    CHECK (outcome NOT IN ('failed', 'timeout') OR jsonb_array_length(hits) = 0),
    CHECK (outcome NOT IN ('failed', 'timeout') OR degradation IS NOT NULL),

    -- The three 裁定四 columns are one observation; two of three is a partial
    -- write, and a partial write here reads as a real measurement.
    CHECK (
        (materials_available IS NULL) = (quoted_side IS NULL)
        AND (quoted_side IS NULL) = (retrieved_unused IS NULL)
    ),
    -- Cannot leave more results unused than were returned.
    CHECK (retrieved_unused IS NULL OR retrieved_unused <= jsonb_array_length(hits)),

    -- ---------------------------------------------------------------------
    -- 四条同族约束，独立审查 2026-09-11 P2-2 量出来的
    -- ---------------------------------------------------------------------
    --
    -- **审查方把四种自相矛盾的行直接插进来，四种全部收下。** 上面那四条 CHECK
    -- 已经为同一类问题存在，注释也写了判据 --「a writer bug that would otherwise
    -- corrupt exactly the counts this table exists to produce」-- 而这四种同样
    -- 命中那条判据，只是当时没被想到。**探针没红就是这一轮的产出。**
    --
    -- **(a) 一次命中却没记读了哪一版。** 这一条最重，因为它砍断的正是这张表
    -- 存在的理由：三跳追溯（回复 → chunk → 原文）的中间那一跳就是
    -- `release_id`。`release_id` 仍然可空 -- 超时到读不到 release 的那一行必须
    -- 留得下来（裁定六），**列的形状不动**，只是「命中」这一种结局不许缺它。
    CHECK (outcome <> 'hit' OR release_id IS NOT NULL),

    -- **(b) 一条都没检出来，却声称引了知识库。** `quoted_side` 是本轮模型实际
    -- 引了哪一边，而没有命中就没有 knowledge 那一边可引。
    CHECK (
        quoted_side IS DISTINCT FROM 'knowledge'
        OR jsonb_array_length(hits) > 0
    ),

    -- **(c) 说超时，却没跑到期限。** `deadline_ms` 是本轮给检索的期限，
    -- `outcome='timeout'` 的意思就是它被走完了。
    --
    -- **供应商侧几毫秒就返回的那种超时错误记成 `failed` 不是 `timeout`** --
    -- 两者该修的地方不同（一个调期限或检索层，一个查供应商/网络），
    -- 而把它们混在一个值里正是裁定六要拆开的那件事。
    CHECK (outcome <> 'timeout' OR elapsed_ms >= deadline_ms),

    -- **(d) `hits` 的元素必须是对象。** `'[1,2,3]'` 先前照收 -- 而隔壁
    -- `source_spans` 对元素形状严到连嵌套数组都逐个查。两张表同一份数据的
    -- 两种严格度，靠的是谁写的时候想到了，这不是标准。
    CHECK (
        NOT (hits @? 'strict $[*] ? (@.type() != "object")')
    )
);

-- The two questions this table is read for: one conversation's citations, and a
-- tenant's counts over a period.
CREATE INDEX IF NOT EXISTS retrieval_evidence_conversation_idx
    ON retrieval_evidence (tenant_id, conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS retrieval_evidence_outcome_idx
    ON retrieval_evidence (tenant_id, created_at DESC, outcome);

ALTER TABLE retrieval_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_evidence FORCE ROW LEVEL SECURITY;
DO $retrieval_evidence_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'retrieval_evidence'
           AND policyname = 'retrieval_evidence_tenant_isolation'
    ) THEN
        CREATE POLICY retrieval_evidence_tenant_isolation ON retrieval_evidence
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$retrieval_evidence_rls$;

COMMENT ON TABLE retrieval_evidence IS
    'One row per turn whose plan step asked for a retrieval (裁定三). Records '
    'which release was read, what came back, and -- after generation -- which '
    'side the model actually quoted (裁定四). A failed or timed-out retrieval '
    'still gets a row (裁定六): without it, "the retriever broke" and "the model '
    'never looked" are the same absence.';

COMMENT ON COLUMN retrieval_evidence.release_id IS
    'Which knowledge release this retrieval read. RFC :754 calls this a '
    '"release pin"; after 裁定七 nothing is pinned and this is an after-the-fact '
    'record, not a constraint. What was answered at the time is recoverable; '
    'replaying it and getting the same result is not guaranteed.';


-- ---------------------------------------------------------------------------
-- No GRANT, deliberately
-- ---------------------------------------------------------------------------
--
-- **This batch is storage; the writers arrive in batches 4 and 6.** `035` and
-- `039` both state the rule these three tables follow: 「a grant that precedes
-- its caller is a permission nothing is testing」. Granting `fastagent_worker`
-- INSERT on `retrieval_evidence` today would be a privilege on the role that
-- parses untrusted inbound content, held for an unknown number of weeks, with
-- no test exercising it and nothing that would notice if it were wrong.
--
-- The RLS policies above are therefore unexercised by any runtime role for now,
-- which is the honest state and not a gap: they are `FORCE`d and carry no `TO`
-- clause, so they bind every role the moment one is granted anything -- rather
-- than the reverse order, where a grant lands first and isolation follows.

-- ---------------------------------------------------------------------------
-- The purge has to be told about them, because it refuses to guess
-- ---------------------------------------------------------------------------
--
-- `043_tenant_purge.sql` keeps its table list explicit on purpose: 「a table
-- added later must be *classified* by a person -- deleted with the tenant, or
-- deliberately kept -- and an unreviewed new table joining a purge by accident
-- is worse than one being missed loudly」. All three of these are classified as
-- **deleted with the tenant**:
--
--   * `knowledge_chunks` and `knowledge_releases` are the tenant's own
--     reference material. There is no argument of `profile_releases`' kind for
--     keeping them: no audit row records a decision made *under* a knowledge
--     release, because nothing reads them yet.
--   * `retrieval_evidence` references `conversation_aggregates`, which the
--     purge deletes. Keeping it would both strand the reference and retain
--     per-conversation records of a tenant that asked to be erased. It is
--     evidence, not the audit log -- `audit_events` is the row that survives.
--
-- **Order**: `retrieval_evidence` before `conversation_aggregates` and before
-- `knowledge_releases`; `knowledge_chunks` before `knowledge_releases`. The
-- whole ordering is re-derived from `pg_constraint` by
-- `test_the_purge_order_satisfies_every_foreign_key`, so it is checked rather
-- than reasoned out -- which is what `043`'s own comment says the first
-- hand-ordered version got wrong in four places.
--
-- The body below is `043`'s, unchanged except for the three new entries; the
-- reasoning for every other line lives there and is not copied.
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

INSERT INTO schema_migrations(version) VALUES ('050_knowledge_base')
ON CONFLICT (version) DO NOTHING;
