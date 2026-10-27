-- 072: 控制面第一次获得「把一条消息排进出站队列」的权限。
--
-- ---------------------------------------------------------------------------
-- 这条迁移改的是授权边界，而那是本仓点名要单列的那一格
-- ---------------------------------------------------------------------------
--
-- `CLAUDE.md` §一：「改迁移、改投递语义或状态机、改权限/凭据/脱敏边界」
-- 要在自审里单列。**这一条同时占了其中三格**，所以理由写长一点。
--
-- 在这条之前，`fastagent_app`（api 角色）对投递那条链**一个写权限都没有**：
--
--     intent_records     —— 无
--     policy_decisions   —— 无
--     outbox_messages    —— 只有 `033` 给的 SELECT（消息页要读它）
--
-- 于是「人工接管之后坐席回一句话」这件事**做不出来**：接管能接，
-- 接了之后说不了话。盘点把它列为商家页面的第一条硬前置（H1）。
--
-- ---------------------------------------------------------------------------
-- 为什么是这四条授权，不多不少
-- ---------------------------------------------------------------------------
--
-- `control-plane-parity.md` §6.2 写死：「员工发送仍走 intent/policy/outbox/
-- delivery」。那条链在库层面是**被外键强制**的，不是一句约定 ——
-- `outbox_messages` 有 `FOREIGN KEY (tenant_id, intent_id) REFERENCES
-- intent_records`（`001:243`），所以一条出站行**必须**先有一条意图。
--
-- 因此人工发送要走与引擎完全相同的四步：
--
--     1. INSERT intent_records          status = 'PROPOSED'
--     2. INSERT policy_decisions        hook = 'pre_execute', effect = 'allow'
--     3. UPDATE intent_records          status -> 'APPROVED'
--     4. INSERT outbox_messages         command + ordinal
--
-- 四条授权逐条对应这四步。**`UPDATE` 收窄到两列**：api 角色不需要能改
-- 意图的任何别的字段（`result` / `inverse` / `execution_token` 是执行者的）。
--
-- ---------------------------------------------------------------------------
-- 它**没有**放开的东西
-- ---------------------------------------------------------------------------
--
-- * **没有 DELETE**，一条都没有。这三张表是台账，删一行等于让一次投递
--   从历史里消失；
-- * **没有 `delivery_receipts`**：回执由 dispatcher 写，控制面只读（消息页）。
--   UI 上那个「已发送」只能来自回执，而不是来自按钮点击成功
--   （parity §6.2 逐字），所以控制面**不该**能写它；
-- * **`outbox_messages` 没有 UPDATE**：状态机是 dispatcher 的。
--   控制面能把一行排进去，但不能替它改命。
--
-- ---------------------------------------------------------------------------
-- RLS 不变，而这一句要说清楚
-- ---------------------------------------------------------------------------
--
-- `001` 已经给这三张表开了 RLS，策略按 `app.tenant_id` GUC 隔离租户。
-- 本条**不碰任何策略**：新增的写入照样落在同一道 RLS 后面 ——
-- 一个 api 连接只能往自己那个租户写。
--
-- ---------------------------------------------------------------------------
-- 撤回它会怎样
-- ---------------------------------------------------------------------------
--
-- `REVOKE` 掉这四条，`POST /conversations/{id}/messages` 当场开始报权限错误，
-- 而**已经排进队列的行照常投递** —— dispatcher 用的是 worker 角色。
-- 也就是说这条迁移是可撤的，代价是那条 route 不可用，不是数据不一致。

-- ⚠️ **这里没有 `BEGIN` / `COMMIT`，而那不是漏了。**
-- `scripts/migrate.py:95` 已经把每条迁移各包在一个事务里
-- （「Each migration is its own transaction」）。在迁移里再写一个 `COMMIT`
-- 会**把外层那个事务结束掉**，于是 runner 随后的 `RELEASE SAVEPOINT` 报
-- `NoActiveSqlTransaction` —— 本文件初稿就是这样，门禁在跑任何用例之前就红了。
-- 全仓 71 条迁移没有一条自己写 `BEGIN`。

-- 第 1 步与第 3 步：立意图、把它标成已批准。
--
-- ⚠️ **`SELECT` 不是顺手加的，它是第 3 步能跑起来的前提。**
--
-- 第 3 步是 `UPDATE intent_records SET status = 'APPROVED'
--          WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'`，
-- 而 PostgreSQL 对**列级 `UPDATE`** 的规则是：`WHERE` 里读到的每一列
-- 还要有 `SELECT` 权限。只给 `UPDATE (status, updated_at)` 的话，
-- 那条语句报的是**表级** `permission denied for table intent_records` ——
-- 一个看起来像「授权整个没加」的错。本文件初稿就是这样，
-- 门禁上两条集成用例红在这里。
--
-- 给的是**表级 `SELECT`** 而不是列级：`WHERE` 里的列会随实现变，
-- 一份列清单是一个会悄悄过期的东西；而这张表本来就没有敏感字段
-- （提案正文是商家自己的话，控制面的消息页早就读得到）。
GRANT SELECT, INSERT ON intent_records TO fastagent_app;
GRANT UPDATE (status, updated_at) ON intent_records TO fastagent_app;

-- 第 2 步：记下那次策略判定。**人工发送也留策略痕**，
-- 否则「这条消息为什么发得出去」在台账上是一个空。
GRANT INSERT ON policy_decisions TO fastagent_app;

-- 第 4 步：排队。⚠️ 只有 INSERT —— 状态机归 dispatcher。
GRANT INSERT ON outbox_messages TO fastagent_app;

INSERT INTO schema_migrations(version) VALUES ('072_human_send_grants')
ON CONFLICT (version) DO NOTHING;
