-- 075: 第四步「读静默」的调用进成本台账（ADR-0149 漏的那一半，运维缺陷 12）。
--
-- ---------------------------------------------------------------------------
-- 为什么它到今天才炸
-- ---------------------------------------------------------------------------
--
-- ADR-0149 给推理加了第四种请求角色 `SILENCE_CAPABILITY`
-- （`runtime/request_shape.py` 定义、`worker/inference_model.py` 传它、
-- `worker/model_policy.py` 也列了它），**而没有配套迁移把它加进
-- `model_invocation_costs.role` 的 CHECK**。那张表的词表是闭合的
-- （`002_m1a_cost.sql` 五个值，`059` 加了第六个 `EMBEDDING`）。
--
-- **这条路走过一次，这次漏了。**
--
-- 它一直没炸，是因为触发条件要有一次真的静默 —— 而在 ADR-0149 上线之前
-- 那一步根本不存在。运维 2026-09-23 的复测第一次走完 8 个回合，
-- **撞了 2 次**（`silence_check` 是 `SILENCE_ANCHORS` 里的常规锚点，
-- 对话里出现一次静默就会走到）。
--
-- ---------------------------------------------------------------------------
-- 破坏面：整轮回滚 + 钱花了而账上没有行
-- ---------------------------------------------------------------------------
--
-- 成本行写在回合事务里，所以 CHECK 违例**整轮回滚**，重试五次后进死信：
--
--     03:14:38  timer | failed | CheckViolation | attempt=5
--     03:16:37  timer | failed | CheckViolation | attempt=5
--
-- ⚠️ **而模型调用发生在写成本行之前。** 每一次重试都真的发出了一次
-- `SILENCE_CAPABILITY` 请求、被供应商计了费，然后那一行成本被回滚掉。
-- 运维本次实测 **10 次隔离 ≈ 10 次付了钱而账上没有行**。
--
-- `model_invocation_costs` 是对账的唯一来源（`ceilings_cny` 的判定、
-- `reconcile_costs.py`、余额快照都读它），所以这不是「少了一行日志」——
-- 它让「这个租户这个月花了多少」偏低，**而且偏低得账面自洽**。
-- 这一句与 `059` 那段逐字同源，因为它是同一个错的第二次。
--
-- ⚠️ **本迁移补不回已经花掉的那十次。** 那些调用没有留下任何行，
-- 无法从任何地方重建；此处言明，免得下一个人以为跑完迁移账就齐了。
--
-- 写法与 `059` 同形。

ALTER TABLE model_invocation_costs
    DROP CONSTRAINT IF EXISTS model_invocation_costs_role_check;

ALTER TABLE model_invocation_costs
    ADD CONSTRAINT model_invocation_costs_role_check
    CHECK (role IN (
        'UNDERSTAND_VALUE', 'PLAN_CAPABILITY', 'GENERATE_VALUE',
        'COMMITMENT_CAPABILITY', 'VISION', 'EMBEDDING', 'SILENCE_CAPABILITY'
    ));

COMMENT ON COLUMN model_invocation_costs.role IS
    '调用角色。前五个来自 model policy 的 roles 块；EMBEDDING (059, 6b-2) 来自它的 '
    'embedding 块；SILENCE_CAPABILITY (075, ADR-0149) 是主动轮次在理解步之前多问的 '
    '那一步 —— 只在静默类锚点的定时器轮次发生，一轮最多一次。';

INSERT INTO schema_migrations(version) VALUES ('075_silence_cost_role')
ON CONFLICT (version) DO NOTHING;
