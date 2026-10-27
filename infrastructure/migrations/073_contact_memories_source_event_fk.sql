-- 073: `contact_memories.source_event_id` 的外键指错了两个 id 里的哪一个。
--
-- ---------------------------------------------------------------------------
-- 这条迁移改的是外键，而那是本仓点名要单列的那一格
-- ---------------------------------------------------------------------------
--
-- `CLAUDE.md` §一：「改迁移、改投递语义或状态机、改权限/凭据/脱敏边界」
-- 要在自审里单列。这一条占第一格。
--
-- ---------------------------------------------------------------------------
-- 它必然违反，而且它把整轮一起带走
-- ---------------------------------------------------------------------------
--
-- `054` 写的是：
--
--     FOREIGN KEY (tenant_id, source_event_id) REFERENCES inbox_events (tenant_id, id)
--
-- 而唯一的生产写入方 `worker/pipeline.py` 的 `_remember_write` 盖的是
-- `prepared.envelope["event_id"]`。**`inbox_events.id` 与
-- `inbox_events.event_id` 是两个不同的值**，信封里那个落在 `event_id` 列。
-- 于是从管线写记忆**一次都不可能成功**。
--
-- 运维 2026-09-22 在 stage 上实测到了它（
-- `docs/handoffs/ops-to-author-the-memory-write-takes-the-turn-with-it-2026-09-22.md`）：
--
--     ERROR:  insert or update on table "contact_memories" violates foreign key
--             constraint "contact_memories_tenant_id_source_event_id_fkey"
--     DETAIL: Key is not present in table "inbox_events".
--
-- **代价不是丢一条记忆，是丢一整轮**：这条 INSERT 在回合事务的末尾，
-- understand / plan / generate 都跑完了、回复已经生成出来，然后整笔回滚，
-- **客户一个字都没收到**。同一句话重发两次，两次都是同样的结局；
-- 7 次模型调用，0 条出站。
--
-- 它也解释了一条挂了好几班的账：`contact_memories` **全库 0 行**。
-- 连带地，`request_shape` 喂给模型的三样历史里 `known_memories` 取自这张表，
-- 所以它**结构性地恒为空** —— 那三个输入里有一个从来就是空的。
--
-- ---------------------------------------------------------------------------
-- 为什么改外键，不改写入方（用户 2026-09-22 裁定）
-- ---------------------------------------------------------------------------
--
-- 两个都能让它通，**而它们让 `source_event_id` 这个名字含义不同**：
--
--   * 改写入方传 `inbox_events.id` —— 一行代码、零迁移。**而本仓另有一张表
--     也叫这个名字**：`commitments.source_event_id`（`063`），它的两个生产者
--     （`pipeline.py` 里 `_record_customer_commitments` / `_record_ai_commitments`
--     那两处调用）传的都是 `prepared.envelope["event_id"]`。
--     改写入方之后，**同名两列含义分叉，而没有任何东西会报错** ——
--     正是 `CLAUDE.md` §三那一族「多传一个不报错」的形状；
--   * 改外键指 `event_id` —— 一条迁移。`inbox_events` 上**本来就有**
--     `UNIQUE (tenant_id, event_id)`（`001_m0.sql:121`），指得合法；
--     改完之后两张表的 `source_event_id` 口径一致，
--     **而外键第一次真的在守一件成立的事**。
--
-- **所以这不是「少了个约束」，是两个都能指而指错了一个。**
--
-- ---------------------------------------------------------------------------
-- 数据
-- ---------------------------------------------------------------------------
--
-- `contact_memories` 全库 **0 行**（运维 2026-09-22T07:24Z 实测
-- `select count(*)` = 0，而那正是本缺陷的后果），所以**没有既有行需要回填或校验**。
-- 控制面那条写入路径（`060`）写的是 `authorised_by` 那一半，
-- `source_event_id` 为 `NULL` —— `NULL` 不受外键约束，不受本条影响。
--
-- ---------------------------------------------------------------------------
-- 为什么 DROP 不带 IF EXISTS
-- ---------------------------------------------------------------------------
--
-- 那个名字是 `054` 里的内联外键由 PostgreSQL 自动生成的，本文件是照着
-- stage 上那条报错原文抄的。**带上 `IF EXISTS`，名字一旦对不上就会静默跳过，
-- 旧外键原样留着，而迁移报成功** —— 本仓管这叫 fail-open。
-- 不带它，名字错了当场炸，指着这一行说「名字不是这个」。
ALTER TABLE contact_memories
    DROP CONSTRAINT contact_memories_tenant_id_source_event_id_fkey;

-- 新的这条**显式命名**：下一个人 grep 得到它，而不是去猜 PostgreSQL 会生成什么。
ALTER TABLE contact_memories
    ADD CONSTRAINT contact_memories_source_event_fkey
    FOREIGN KEY (tenant_id, source_event_id)
    REFERENCES inbox_events (tenant_id, event_id);

INSERT INTO schema_migrations(version) VALUES ('073_contact_memories_source_event_fk')
ON CONFLICT (version) DO NOTHING;
