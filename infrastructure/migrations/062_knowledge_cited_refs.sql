-- 062: 三跳追溯的第一跳 —— **引了哪几条**，不是「有几条没被引」。
--
-- ---------------------------------------------------------------------------
-- 为什么这一列必须存在
-- ---------------------------------------------------------------------------
--
-- `050` 建这张表时，它自己的 `hits` 列注释写的是：
--
--   > the three-hop traceback has to survive the chunk being superseded ——
--   > a citation from last week must still resolve to **the passage that was
--   > actually quoted**.
--
-- 而 `050` 给的三列里，与「引了哪一条」有关的只有 `retrieved_unused` ——
-- **一个数**。于是一个 top-2 的回合引了其中一条时，库里记的是「1 条没被引」，
-- **而不是哪一条被引了**。第一跳（回复 → chunk）因此只到「这一轮」这个粒度。
--
-- 出口判据（`knowledge-base-schedule-2026-09-11.md` §6.1 第 4 条）要求的是
-- **三跳都真能反查**。第二跳（chunk → 原文的哪一段）在 `hits[].source_spans`
-- 上已经成立（`050` 抄进来那一份），第三跳在人工整理规格上。
-- **缺的只有第一跳的精度**，而它缺得很安静：`retrieved_unused = 1` 读起来
-- 像一条真实测量，只是它答不了那个要紧的问题。
--
-- **为什么不能靠 `claims` 反查**：`claims` 今天**根本没有落库**
-- （全仓唯一读它的地方是本批的 `_record_generation_side`，在内存里）。
-- 它也不该顺手落库：`claims[].text_hash` 是正文的哈希，
-- 把整份 `claims` 抄进审计表会把「模型说了什么」的副本散到第二处，
-- 而这一列只要**它引了哪几个 ref**。
--
-- ---------------------------------------------------------------------------
-- 形状
-- ---------------------------------------------------------------------------
--
-- **可空，且与裁定四那三列同生同灭**（`050` 那条「两列有值一列空是一次部分写入，
-- 而一次部分写入在这张表里读起来像一次真实测量」同一条纪律）。
-- `NULL` = 这一轮没走到生成；`[]` = 走到了生成而一条都没引 ——
-- 后者是一个真实结局（检索花了钱、模型没用），也正是最该被数出来的那一种。
ALTER TABLE retrieval_evidence
    ADD COLUMN IF NOT EXISTS cited_refs jsonb;

ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_cited_refs_is_array;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_cited_refs_is_array
    CHECK (cited_refs IS NULL OR jsonb_typeof(cited_refs) = 'array');

-- 与那三列同生同灭。**四列一起写**，写者只有一个（生成之后那次 UPDATE）。
ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_cited_refs_arrives_with_the_verdict;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_cited_refs_arrives_with_the_verdict
    CHECK ((cited_refs IS NULL) = (quoted_side IS NULL));

-- 引不出没检出来的东西。**这一条与 `050` 的 (b) 同族**
-- （那一条管 `quoted_side = 'knowledge'` 却零命中），只是这一条按条数算：
-- 引用集必须装得进命中集。
ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_cited_refs_fit_the_hits;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_cited_refs_fit_the_hits
    CHECK (
        cited_refs IS NULL
        OR jsonb_array_length(cited_refs) <= jsonb_array_length(hits)
    );

-- `quoted_side = 'knowledge'` 的意思就是「这一轮引了知识库」——
-- 那时引用集不许是空的。**反过来不成立**：`quoted_side = 'material'` 的回合
-- **可以**有非空引用集（模型逐字引了素材、同时把知识库当依据），
-- 而那正是裁定四要留的「共现」记录，不是矛盾。
ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_knowledge_verdict_needs_a_citation;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_knowledge_verdict_needs_a_citation
    CHECK (
        quoted_side IS DISTINCT FROM 'knowledge'
        OR jsonb_array_length(cited_refs) > 0
    );

COMMENT ON COLUMN retrieval_evidence.cited_refs IS
    '这一轮模型在 claims[].source_evidence_refs 里引到的知识库 ref, 升序去重 '
    '(出口判据 §6.1 第 4 条的第一跳)。NULL = 没走到生成; [] = 走到了生成一条都没引。 '
    'retrieved_unused 是它的补数的条数, 两列一起才答得了「引了哪条、剩几条没用」。 '
    'claims 本身不落库 —— 这一列只要 ref, 不要正文的哈希。';

-- **仍然是列级**（与 `056` / `061` 同一条理由）：worker 改得动这一列，
-- 而 `outcome` / `hits` / `query_hash` 改不动 —— 那些是检索当时的事实。
GRANT UPDATE (cited_refs) ON retrieval_evidence TO fastagent_worker;

INSERT INTO schema_migrations(version) VALUES ('062_knowledge_cited_refs')
ON CONFLICT (version) DO NOTHING;
