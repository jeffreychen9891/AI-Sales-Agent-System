-- 071: what this conversation has been about, one line per turn.
--
-- **Migration number**: `070` is the tip on `main` (`070_read_cursor_ordinal.sql`).
-- `061` was pre-allocated to `feat/rolling-summary-20260912` and **released when
-- that branch was abandoned** (T326) -- it is not reused here, because the
-- numbers in between have shipped and a migration must sort after what it
-- depends on.
--
-- **Why a table and not a column on the aggregate.**
--
-- The aggregate is read and written every single turn. A summary that grows
-- with the conversation would grow that read-modify-write with it, which is the
-- exact shape ADR-0122 §三 asks this batch to bound rather than create. And
-- ruling 8 (user, 2026-09-12) borrows `conversation-engine.md:1146` verbatim --
--
--     自动 STALLED 后人工纠正保留双版本/审计；新事实不静默覆盖人工 revision
--
-- -- which needs somewhere to keep the superseded version. A jsonb field on the
-- aggregate has nowhere to put an audit trail; rows do.
--
-- **Why one row per turn, rather than one row per rewritten summary.**
--
-- Ruling 8: **the model may append, it may not rewrite the existing summary.**
-- Written as "the model emits the whole summary each turn", that rule needs a
-- guard to enforce it, and a guard that compares two free-text blobs cannot say
-- what changed. Written as "the model emits **one line about this turn** and the
-- server concatenates", the rule is **structural**: the model is never handed
-- the previous lines to rewrite, so it cannot rewrite them.
--
-- The rolling part is the reader's job, not a writer's: the prompt takes the
-- most recent lines that fit `SUMMARY_PROMPT_BYTES` and drops the oldest.
-- **Nothing is deleted to make room** -- dropping happens at read time, so
-- raising the budget brings the older lines back.
--
-- **What this table is not**: it is not a transcript. One line per turn, bounded
-- in length, written by the understanding step. The customer's actual words
-- live in `inbox_events` and are never copied here.

CREATE TABLE IF NOT EXISTS conversation_summaries (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,

    -- Append order. **Not `created_at`**: two lines written inside the same
    -- transaction can share a timestamp, and "which came first" is the whole
    -- meaning of a rolling summary. Same call `070_read_cursor_ordinal.sql`
    -- made for the outbox read cursor, and for the same reason.
    ordinal bigint NOT NULL,

    -- One turn, one line. The ceiling is the platform's, not the merchant's:
    -- it is what makes the prompt share bounded (`SUMMARY_PROMPT_BYTES`).
    -- 480 is `SUMMARY_LINE_BYTES` in `runtime/summary_prompt.py` -- **the two
    -- are one number in two places**, and a contract test pins them equal.
    line text NOT NULL CHECK (length(line) BETWEEN 1 AND 480),

    -- **Which inbound turn produced it.** Also the idempotency key: see the
    -- unique index below.
    source_event_id uuid NOT NULL,

    -- `active` -- counts toward the summary the model sees.
    -- `invalidated` -- a person pressed "this summary is wrong" (ruling 9,
    -- option (c)). The row stays: that is the "保留双版本/审计" half.
    status text NOT NULL CHECK (status IN ('active', 'invalidated')),
    invalidated_at timestamptz,
    invalidated_by uuid,

    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES conversation_aggregates(tenant_id, id),
    -- The person who invalidated it is an employee of the same tenant. A
    -- composite foreign key with a nullable column is satisfied by any row that
    -- leaves it NULL (MATCH SIMPLE), which is what "never invalidated" needs --
    -- the same shape `034_manual_tasks.sql` uses for `owner_employee_id`.
    FOREIGN KEY (tenant_id, invalidated_by) REFERENCES employees(tenant_id, id),

    -- **The state and its stamps travel together.** An `invalidated` row with no
    -- time, or a time with no status, are states no route should be able to
    -- leave behind (shape from `019`, same as `manual_tasks_claimed_pair`).
    CONSTRAINT conversation_summaries_invalidated_pair CHECK (
        (status <> 'invalidated')
        OR (invalidated_at IS NOT NULL AND invalidated_by IS NOT NULL)
    )
);

-- **One line per inbound turn, enforced by the database.**
--
-- The worker's write is inside the turn's transaction, and a turn can be
-- replayed (the inbox is at-least-once). Without this index a replay appends a
-- second line saying the same thing, and the summary slowly fills with
-- duplicates that no reader can tell apart from a customer repeating himself.
-- `ON CONFLICT DO NOTHING` at the write site is the other half.
CREATE UNIQUE INDEX IF NOT EXISTS conversation_summaries_one_line_per_turn_idx
    ON conversation_summaries (tenant_id, conversation_id, source_event_id);

-- The read path is "the most recent live lines of this conversation".
CREATE INDEX IF NOT EXISTS conversation_summaries_reading_idx
    ON conversation_summaries (tenant_id, conversation_id, status, ordinal DESC);

-- **A summary line's substance is immutable; its lifecycle column is not.**
--
-- Shape copied from `054_contact_memories.sql`'s trigger rather than invented.
-- The only permitted move is `active -> invalidated` with its two stamps.
-- Rewriting `line` in place is refused, and that refusal **is** ruling 8: a
-- person invalidating a summary is a recorded act, not an edit that leaves no
-- trace of what the model had said.
CREATE OR REPLACE FUNCTION conversation_summaries_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $conversation_summaries_reject_mutation$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
       OR NEW.ordinal IS DISTINCT FROM OLD.ordinal
       OR NEW.line IS DISTINCT FROM OLD.line
       OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'conversation_summaries rows are immutable; invalidate and let the '
            'next turn write a new line instead'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.status = 'invalidated' AND NEW.status <> 'invalidated' THEN
        RAISE EXCEPTION
            'an invalidated summary line cannot become active again'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.invalidated_at IS NOT NULL
       AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at
    THEN
        RAISE EXCEPTION
            'conversation_summaries.invalidated_at is set once and never rewritten'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$conversation_summaries_reject_mutation$;

DROP TRIGGER IF EXISTS conversation_summaries_immutable ON conversation_summaries;
CREATE TRIGGER conversation_summaries_immutable
    BEFORE UPDATE ON conversation_summaries
    FOR EACH ROW EXECUTE FUNCTION conversation_summaries_reject_mutation();

ALTER TABLE conversation_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_summaries FORCE ROW LEVEL SECURITY;
DO $conversation_summaries_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public' AND tablename = 'conversation_summaries'
           AND policyname = 'conversation_summaries_tenant_isolation'
    ) THEN
        CREATE POLICY conversation_summaries_tenant_isolation ON conversation_summaries
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$conversation_summaries_rls$;

-- **谁读、谁写。**
--
-- worker: 读（喂提示词）+ 追一行。**没有 UPDATE** -- 它不改已有的行，
-- 那正是裁定八「模型只能追加」在权限层的样子：就算有人在 worker 里写了一条
-- `UPDATE conversation_summaries`，数据库也会拒。
GRANT SELECT, INSERT ON conversation_summaries TO fastagent_worker;

-- 控制面: 读（`resume-preview` 给人看）+ **只改那三个生命周期列**。
--
-- **列级，与 `061` 同一种记法**（那里逐字写着「仍然是列级：`GRANT UPDATE`
-- 不带列表会让 worker 改得动…」）。不带列表的话控制面改得动 `line` --
-- 而「人工改写摘要正文」正是裁定九**明确没有做**的那件事（取 (c)：只作废）。
-- 触发器已经拒它一次；这里是第二道，**而两道防的是不同的错**：
-- 触发器防「代码写错了」，列级 GRANT 防「有人以为这是允许的」。
GRANT SELECT ON conversation_summaries TO fastagent_app;
GRANT UPDATE (status, invalidated_at, invalidated_by)
    ON conversation_summaries TO fastagent_app;

-- **整租户清理要认得它。**
--
-- `tests/integration/test_tenant_purge_coverage.py` 有两条：一条数
-- 「每张带 tenant_id 的表要么被清、要么写明为什么不清」，另一条按外键验删除顺序。
-- **本批第一版两条都红了** —— 新表默认既不在清单里、也不在豁免里。
--
-- 位置**不是随手放的**：`conversation_summaries` 外键指向
-- `conversation_aggregates` 与 `employees`，所以它必须排在两者**之前**，
-- 否则 purge 跑到那两行就撞外键中断。放在 `contact_memories` 之后。
--
-- 整函数重贴一遍（`CREATE OR REPLACE`），与 `063` / `064` 同样的做法 --
-- 只加一行。
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'contact_memories',
        'conversation_summaries',
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

INSERT INTO schema_migrations(version) VALUES ('071_conversation_summaries')
ON CONFLICT (version) DO NOTHING;
