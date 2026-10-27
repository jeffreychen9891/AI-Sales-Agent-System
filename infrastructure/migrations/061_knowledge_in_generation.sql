-- 061: 知识库进生成 —— 送进去几条要可数，裁定四那三列由 worker 写。
--
-- **本文件原名 `060`，2026-09-12 晚改号。** `060` 被 E4-3（`feat/memory-routes-20260912`）
-- 先领了，而两个作者窗口各自在自己那棵旧 `main` 上领号是今天撞号五次的唯一成因。
-- 规则是「后来者改号」，本批是后来者。**`main` 上今天最高仍是 `059`** ——
-- 于是 `060` 未合之前会短暂缺号，仓里没有任何东西要求连号
-- （`tests/contracts/test_migration_versions.py` 只要求每条迁移记下自己的版本）。
--
-- ---------------------------------------------------------------------------
-- 一、`hits_sent`：检出几条与**送进提示词**几条不是一个数
-- ---------------------------------------------------------------------------
--
-- **ADR-0135 第 3 条要求的那一列。** 额度（1,826）可能装不下 top-2 的第二条，
-- 而上界的行为是**整条丢弃、不截断**（截一半的 chunk 是一条会让模型引错的来源）。
--
-- 没有这一列，**「检出 2 条只送了 1 条」与「只检出 1 条」在数据里长得一模一样** ——
-- 而这两件事该修的地方不同：前者是预算不够（该重推 ADR-0132 §八 那张表），
-- 后者是语料不够（该去补语料）。**与 `056` 把 `unusable_request` 从 `failed` 里
-- 拆出来是同一条判据**（那张表自己的 CHECK (c) 注释：该修的地方不同的两件事
-- 不许共用一个值）。
--
-- **可空**：`056` 之前的行、以及 `unusable_request` / `empty` / `failed` / `timeout`
-- 那些回合没有「送了几条」可言。下面那条 CHECK 因此只约束它与 `hits` 的关系。
ALTER TABLE retrieval_evidence
    ADD COLUMN IF NOT EXISTS hits_sent integer;

ALTER TABLE retrieval_evidence
    DROP CONSTRAINT IF EXISTS retrieval_evidence_hits_sent_shape;
ALTER TABLE retrieval_evidence
    ADD CONSTRAINT retrieval_evidence_hits_sent_shape
    CHECK (
        hits_sent IS NULL
        OR (hits_sent >= 0 AND hits_sent <= jsonb_array_length(hits))
    );

COMMENT ON COLUMN retrieval_evidence.hits_sent IS
    '这一轮真的送进 generate 提示词的命中条数 (ADR-0135)。与 jsonb_array_length(hits) '
    '的差就是被字节额度挡下来的条数 —— 而「检出 2 条送了 1 条」与「只检出 1 条」'
    '该修的地方不同 (前者重推预算表, 后者补语料)。NULL = 没走到生成那一步。';

-- ---------------------------------------------------------------------------
-- 二、裁定四那三列：worker 要 `UPDATE`，而且只要这四列
-- ---------------------------------------------------------------------------
--
-- 裁定四（`knowledge-base-schedule-2026-09-11.md` §5.2.2）：素材优先，但要留记录，
-- 而记录的形状是「**共现 + 用了哪边**」，不是「冲突」——
-- 判断两段自然语言矛不矛盾要理解它们，那是模型的活，
-- **平台把它当成一次判断去做，就是在没有来源的情况下生成一个事实结论**。
--
-- 这三列在 `050` 就建好了，`056` 刻意**没有**发 `UPDATE`：
-- 它们在生成之后才有值，而 6b 的写者不该有能力在检索那一刻填一个当时不可能知道的值。
-- **6c 是「生成之后」那一批**，所以这里发。
--
-- **仍然是列级**：`GRANT UPDATE` 不带列表会让 worker 改得动 `outcome` / `hits` /
-- `query_hash` —— 那些是检索当时的事实，**事后改它们等于改一条已经发生过的记录**。
GRANT UPDATE (
    materials_available, quoted_side, retrieved_unused, hits_sent
) ON retrieval_evidence TO fastagent_worker;

-- `hits_sent` 在 INSERT 那一刻就知道（上界是纯函数，跑在写这行之前），
-- 所以它也进 INSERT 的列级授权 —— 不靠一次事后 UPDATE 补。
GRANT INSERT (hits_sent) ON retrieval_evidence TO fastagent_worker;

INSERT INTO schema_migrations(version) VALUES ('061_knowledge_in_generation')
ON CONFLICT (version) DO NOTHING;
