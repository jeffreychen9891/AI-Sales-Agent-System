-- 059: 嵌入调用进成本台账。**6b-2 的那一半。**
--
-- ---------------------------------------------------------------------------
-- 为什么要动这张表
-- ---------------------------------------------------------------------------
--
-- 检索要调一次嵌入端点，而 `model_invocation_costs.role` 是**五个值的闭合词表**
-- （`002_m1a_cost.sql`：UNDERSTAND_VALUE / PLAN_CAPABILITY / GENERATE_VALUE /
-- COMMITMENT_CAPABILITY / VISION）。**没有这一条，检索接进管线就等于在每回合的
-- 路径上放一次没有账的供应商调用** —— 而那不是「少了一行日志」：
-- 它让「这个租户这个月花了多少」从此偏低，**而且偏低得账面自洽**
-- （`ceilings_cny` 的判定、`reconcile_costs.py` 的对账、余额快照，全都读这张表）。
--
-- **价目本来就在活着那一版 release 里**（`embedding.pricing.text_input`，
-- 用户提供的豆包 2026-09 官网价目），`embedding.embed_one` 也一直把
-- `prompt_tokens` 带回来 —— 缺的只是让台账认这一态。
--
-- 写法与 `010_cost_unreconciled_status.sql` 同形（那一批给 `status` 加
-- `'unreconciled'`），约束名用 PostgreSQL 给内联 CHECK 的默认名。

ALTER TABLE model_invocation_costs
    DROP CONSTRAINT IF EXISTS model_invocation_costs_role_check;
ALTER TABLE model_invocation_costs
    ADD CONSTRAINT model_invocation_costs_role_check
    CHECK (role IN (
        'UNDERSTAND_VALUE', 'PLAN_CAPABILITY', 'GENERATE_VALUE',
        'COMMITMENT_CAPABILITY', 'VISION', 'EMBEDDING'
    ));

COMMENT ON COLUMN model_invocation_costs.role IS
    '调用角色。前五个来自 model policy 的 roles 块；EMBEDDING (059, 6b-2) 来自它的 '
    'embedding 块 —— 检索层每回合最多调一次，价目是扁平的 text_input, '
    'output_tokens 恒为 0 (嵌入没有输出 token, 这是事实不是「没量到」)。';

-- **`prompt_release` 对一次嵌入调用是什么**，写在列注释里而不是靠下一个人猜。
--
-- 这一列 NOT NULL，而嵌入调用没有提示词。填的是**检索器的版本**
-- （`retrieval.RETRIEVER_VERSION`，今天是 `kb-retrieval-v1`）：这一列答的是
-- 「这一行的请求是由哪一版的东西造出来的」，而对一次嵌入调用来说那就是检索器 ——
-- 换了 top-k、换了距离度量、换了查询串的预处理，同一个查询就会得到不同结果，
-- 那正是这一列该跟着动的时候。
--
-- **不填模型提示词的版本串**（`m1a-prompt-v7` 一类）：那会让「按 prompt_release
-- 分组看某一版提示词花了多少」把检索的账算进模型提示词的头上。
COMMENT ON COLUMN model_invocation_costs.prompt_release IS
    '造出这一行的请求的那个版本。模型角色填提示词发布串 (inference_model.PROMPT_RELEASE); '
    'EMBEDDING 那一态填检索器版本 (retrieval.RETRIEVER_VERSION) —— 嵌入调用没有提示词, '
    '而这一列问的是「请求是由哪一版的东西造出来的」。';

INSERT INTO schema_migrations(version) VALUES ('059_embedding_cost_role')
ON CONFLICT (version) DO NOTHING;
