-- 063: 承诺台账（RFC §5）——**表、写入方、读出方同批到位**。
--
-- ---------------------------------------------------------------------------
-- 为什么这三件必须同批，而不是三批
-- ---------------------------------------------------------------------------
--
-- ADR-0139 §二 把「只建表、本批不建写入方」（选项丁）明确排除掉了，理由是
-- `AGENTS.md` 判据 2 的正面：**数据结构建好了，全仓无人写**。
-- 而本批的前身（批 11a，只交契约）**正是因为这一条被复核退回**：
-- 它请求专判的那条豁免——「后继到期时会红的守卫已经在了」——被实测推翻，
-- 复核把后继的两半各换一种合法写法再做了一次：
--
--   * 表名换成 `commitment_book`：那条绊线全绿（它按表名匹配）；
--   * 消费方写成 `validate_contract("commitment-record", row)`：**全量一条不红**
--     ——而那是本仓消费契约的默认写法（生产代码里 23 处 / 7 个文件，
--     `load_schema` 只有 2 处，**而退回的那条守卫 grep 的正是后者**）。
--
-- 用户 2026-09-16 因此裁定 11a + 11b + 11c 合成一批。**表与写入方同时到位之后，
-- 「有没有消费方」这一问不再需要豁免** ——它有写入方（`commitment_ledger.py`）、
-- 有读出方（`commitment_refs` 与接管页那一格），三者一起进 `main`。
--
-- ---------------------------------------------------------------------------
-- 形状照 `packages/contracts/schemas/commitment-record.schema.json`
-- ---------------------------------------------------------------------------
--
-- 十三个字段逐一对应，三个 CHECK 词表与契约的三个 enum **逐字相同**——
-- 两边相等由 `tests/contracts/test_commitment_ledger.py` 按等值集合钉住，
-- 而那条用例同时把契约与 RFC §5 比一遍。**三者一线，改任一处都会红。**
--
-- **`scheduled_event_id` 这一批不接外键。** `scheduled_events.commitment_id`
-- （`001_m0.sql`）那根悬空的列要等 11d：外键一旦有数据在下面就不可逆，
-- 而 ADR-0139 §三 把它挪到了最后一格。这里两侧都是普通列，**有意的**。
--
-- **`dedupe_key` 是投递语义那一半。** 回合是至少一次的，同一条入站事件可能被
-- 重放；没有这一列的话一次重放会把同一条承诺记两遍，而两遍在台账上读起来
-- 是「客户答应了两次」。取值是 `<source_event_id>:<序号>`，
-- 与 `scheduled_events.dedupe_key` 同一个理由、同一个形状。

CREATE TABLE IF NOT EXISTS commitments (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    party text NOT NULL CHECK (party IN ('ai', 'customer', 'employee', 'external')),
    type text NOT NULL CHECK (type IN (
        'quote_or_calculate', 'lookup_or_verify', 'apply_price_or_discount',
        'send_material', 'contact_or_ask_human', 'provide_information', 'take_action'
    )),
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 500),
    source_event_id uuid NOT NULL,
    due_at timestamptz,
    status text NOT NULL CHECK (status IN (
        'proposed', 'active', 'fulfilled', 'overdue', 'withdrawn',
        'superseded', 'remedy_running', 'escalated', 'fail_open'
    )),
    evidence_refs jsonb NOT NULL CHECK (
        jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) BETWEEN 1 AND 8
    ),
    scheduled_event_id uuid,
    generation bigint NOT NULL CHECK (generation >= 0),
    version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
    dedupe_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, conversation_id, dedupe_key),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation_aggregates(tenant_id, id)
);

COMMENT ON TABLE commitments IS
    '承诺台账 (RFC 5)。形状与 packages/contracts/schemas/commitment-record.schema.json '
    '逐字段对应, 三个 CHECK 词表与契约三个 enum 按等值集合钉住。'
    'party=customer 的候选由理解步提取、post_understanding 那道 policy hook 确认入账 '
    '(ADR-0139: 是 policy 不是 planner —— planner 的 scheduled_event_changes 验完就丢)。';

COMMENT ON COLUMN commitments.due_at IS
    '到期时刻; 没有明确时间就是 NULL, 不许猜 (RFC 5 party=customer 那条)。'
    'NULL 时不建 customer_promise_due —— 那条事件的锚点 commitment_due_at '
    '要等 11d 才进 SCHEDULABLE_ANCHORS。';

COMMENT ON COLUMN commitments.scheduled_event_id IS
    '这条承诺排出去的定时事件; 本批不接外键 (11d)。'
    'scheduled_events.commitment_id 是它的反向, 那一根同样还是悬空的列。';

COMMENT ON COLUMN commitments.dedupe_key IS
    '<source_event_id>:<该事件里第几条候选>。回合是至少一次的, '
    '没有这一列时一次重放会把同一条承诺记两遍, 而两遍读起来是「客户答应了两次」。';

-- RLS：与 `001` 建的那十四张表同一个形状（无 `TO` 子句 + `FORCE`，于是对每个角色
-- 都生效，`fastagent_app` 也不例外）。**不抄 `001` 那个 DO 块**：那一块按数组
-- 循环建策略，而这里只有一张表，展开写比循环一行更容易核。
ALTER TABLE commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE commitments FORCE ROW LEVEL SECURITY;

DO $commitments_rls$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'commitments'
          AND policyname = 'commitments_tenant_isolation'
    ) THEN
        CREATE POLICY commitments_tenant_isolation ON commitments
            USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
    END IF;
END
$commitments_rls$;

-- 授权：**worker 写，api 只读**，与 `conversation_aggregates` 那一对完全同形。
--
-- **api 拿的是列级 SELECT，不是整表**：接管页那一格只要数得出「登记了几条」
-- 与显示它们的状态，**不需要 `summary`** —— 而 `summary` 是客户原话的摘要，
-- 是本仓一直在收窄的那一类。给整表 SELECT 等于让读模型那一侧顺手拿到它。
GRANT SELECT, INSERT, UPDATE ON commitments TO fastagent_worker;
GRANT SELECT (tenant_id, conversation_id, id, party, type, status, due_at) ON commitments
    TO fastagent_app;


-- ---------------------------------------------------------------------------
-- 租户清除：**新表必须进这条清单，而这不是靠记得**
-- ---------------------------------------------------------------------------
--
-- `tests/integration/test_tenant_purge_coverage.py` 有两条会红的断言：
-- 「每一张带 `tenant_id` 的表要么被清、要么被点名排除」与「清除顺序满足每一条
-- 外键」。**本批第一次跑全量门禁时两条都红了** —— 契约层那一千六百条一条都没说话，
-- 因为这件事只在连库那一层看得见。
--
-- 位置：**排在 `conversation_aggregates` 之前**。`commitments` 有
-- `(tenant_id, conversation_id) → conversation_aggregates` 的外键，
-- 先删聚合会撞上它。清单本身是按依赖排的，不是按字母排的。
CREATE OR REPLACE FUNCTION purge_tenant_data(target_tenant uuid)
RETURNS TABLE (table_name text, deleted bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ordered_tables text[] := ARRAY[
        'contact_memories',
        'commitments',
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

INSERT INTO schema_migrations(version) VALUES ('063_commitments')
ON CONFLICT (version) DO NOTHING;
