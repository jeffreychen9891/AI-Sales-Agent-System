-- 064: `scheduled_events.commitment_id` 接上外键 —— **这一条不可逆**。
--
-- ---------------------------------------------------------------------------
-- 它为什么排在最后，以及为什么现在才排得上
-- ---------------------------------------------------------------------------
--
-- `commitment_id` 这根列从 `001_m0.sql:143` 起就在那儿，**而它一直没有写入方**：
-- 建表那一批写下它的时候，承诺台账还不存在（T138 逐字记过这件事：
-- 「`scheduled_events.commitment_id` 是一根悬空的列」）。
-- ADR-0139 §三 因此把这一笔放到最后一格 —— **外键一旦有数据在下面就不可逆**，
-- 而在没有写入方的列上立一条外键，是一条**今天不可能被违反**的约束：
-- 它绿着，什么都不证明。
--
-- 所以这一批把两件一起做（用户 2026-09-16 裁定）：**外键 + 写入方**。
-- 写入方在 `worker/pipeline.py` 那条 `INSERT INTO scheduled_events` 里
-- （在此之前它的列清单里根本没有 `commitment_id`），
-- 承诺由 `commitment_due_at` 那类规则带进来。
--
-- ---------------------------------------------------------------------------
-- 这一条会拒掉什么
-- ---------------------------------------------------------------------------
--
-- 复合键 `(tenant_id, commitment_id)` 而不是单列 `commitment_id`：
-- 本仓每一条跨表引用都带 `tenant_id`，**这不是风格** —— 单列外键允许
-- A 租户的定时器指向 B 租户的承诺，而 RLS 只管读，不管这一次插入引用了谁。
--
-- `commitment_id` 仍然可空，**而可空是有意义的那一半**：
-- `silence_check` / `goal_stall` 这些规则真的没有承诺，
-- 不是「还没填」。`MATCH SIMPLE`（默认）下 NULL 不参与外键检查，正是要的语义。
--
-- **`ON DELETE` 一条都不给**（既不 CASCADE 也不 SET NULL）：
-- `commitments` 今天没有任何删除路径 —— 台账是追加+改状态的，
-- 而唯一的整租户清理走 `purge_tenant_data`（`063` 已经把 `commitments`
-- 排在 `conversation_aggregates` 前面）。给一条 `ON DELETE` 会让
-- 「承诺可以被删」读起来像是支持的，而它不是。
-- 真需要删的那天，那是一次要专门裁决的改动。
--
-- ---------------------------------------------------------------------------
-- 跑之前
-- ---------------------------------------------------------------------------
--
-- **这一条会在库里已有脏引用时当场失败**，而那正是要的行为。
-- 跑之前先问一遍（零行才可以跑）：
--
--   SELECT count(*) FROM scheduled_events s
--    WHERE s.commitment_id IS NOT NULL
--      AND NOT EXISTS (SELECT 1 FROM commitments c
--                       WHERE c.tenant_id = s.tenant_id AND c.id = s.commitment_id);
--
-- 合并时刻的预期是 0：在 `main` 上这根列**从来没有过写入方**。
-- 但预期不是测量，所以这句留在这里让跑的人现问一遍。

ALTER TABLE scheduled_events
    ADD CONSTRAINT scheduled_events_commitment_fk
    FOREIGN KEY (tenant_id, commitment_id)
    REFERENCES commitments(tenant_id, id);

COMMENT ON COLUMN scheduled_events.commitment_id IS
    '这一行挂在哪一条承诺上。NULL = 这条规则本来就没有承诺（silence_check 一类），'
    '不是「还没填」。064 起有外键 (tenant_id, commitment_id) -> commitments，'
    '写入方是 worker/pipeline.py 里 commitment_due_at 那条路。';

-- 只在承诺那一侧建索引：外键的**被引用**侧已经有主键，
-- 而「这条承诺有哪些定时器」是真实的读法（取消承诺时要找到它们）。
CREATE INDEX IF NOT EXISTS scheduled_events_commitment_idx
    ON scheduled_events (tenant_id, commitment_id)
    WHERE commitment_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 清租户那条顺序也得跟着改 —— **这是门禁抓的，不是我想到的**
-- ---------------------------------------------------------------------------
--
-- `063` 把 `commitments` 排在 `contact_memories` 后面、`scheduled_events` **前面**。
-- 那时两张表之间没有外键，顺序随意。**这一条一立，顺序就有了方向**：
-- `scheduled_events` 引用 `commitments`，所以**定时器必须先删**，
-- 否则 `purge_tenant_data` 跑到 `commitments` 那一行就中断，
-- 整个清租户作业失败在中途 —— 一次已经删了一半的清理。
--
-- 抓住它的是 `tests/integration/test_tenant_purge_coverage.py`：
-- 它不按注释里的说理判，**它去 `pg_constraint` 里把每一条外键读出来**，
-- 逐对比较删除次序。那条用例的 docstring 记着它的来由：
-- 第一版 `purge_tenant_data` 违反了四条外键，而它的注释信誓旦旦地写着
-- 「先子后父」——**说的那几对是对的，没看的那四对全错**。
--
-- 所以这里整函数重贴一遍（`CREATE OR REPLACE`，与 `063` 同样的做法），
-- 只挪一行：`commitments` 挪到 `scheduled_events` 之后。
--
-- **两条断言，跑之前可以现验**：
--
--   -- ① 这一行真的在定时器之后
--   SELECT array_position(<ordered_tables>, 'commitments')
--        > array_position(<ordered_tables>, 'scheduled_events');
--   -- ② 或者直接跑那条用例：
--   pytest tests/integration/test_tenant_purge_coverage.py

CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'contact_memories',
        'delivery_receipts',
        'employee_read_cursors',
        'employee_roles',
        'inbox_events',
        'ingress_replay_keys',
        'manual_tasks',
        'materials',
        'model_invocation_costs',
        'outbox_messages',
        'policy_decisions',
        'intent_records',
        'refresh_tokens',
        'retrieval_evidence',
        'roles',
        'scheduled_events',
        'commitments',
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

INSERT INTO schema_migrations(version) VALUES ('064_scheduled_events_commitment_fk')
ON CONFLICT (version) DO NOTHING;
