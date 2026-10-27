-- 076: 一次判定可以铺成多行出站（ADR-0152，运维缺陷 14）。
--
-- ---------------------------------------------------------------------------
-- 用户 2026-09-23 的裁定
-- ---------------------------------------------------------------------------
--
--     「一次生成应该是一次判定 而不是分段之后还要判定
--       分段前就判断好能不能发 拆分后语义语境可能完全不同」
--
-- ADR-0151 把分段摊在**策略链之前**，于是**每一段各自过了一遍全部 policy** ——
-- 而那正是裁定要取消的那件事：一个片段单看的判定结论，
-- 可能与看整条消息完全不同。
--
-- 改成「整条判一次、批准之后再拆」之后，**一个 `intent_record` 要对应多行出站**，
-- 而 `001_m0.sql` 的 `UNIQUE (tenant_id, intent_id)` 恰好禁止这件事。
--
-- ---------------------------------------------------------------------------
-- 放松，而不是取消
-- ---------------------------------------------------------------------------
--
-- 那条唯一约束保护的是「同一个意图不许排两次」。**这条保护仍然要**，
-- 只是「同一个意图的同一段」才是今天的判据 —— 所以它变成三列。
--
-- ⚠️ **`ordinal` 已经是 `NOT NULL DEFAULT 0`**（`044`），所以：
--
-- * 本迁移之前的每一行 `ordinal` 都是 0 → 新约束对它们退化成
--   `UNIQUE (tenant_id, intent_id)`，**与今天逐字等价**；
-- * 存量数据里不可能有冲突 —— 旧约束已经保证了这一点。
--
-- ⚠️ **`UNIQUE (tenant_id, idempotency_key)` 一个字不动。** 它才是防重复投递的
-- 那一道，而分段的键带段序号（`SEGMENT_BASIS_INFIX`，ADR-0151），本来就不撞。
--
-- ---------------------------------------------------------------------------
-- 这条与 074 的关系
-- ---------------------------------------------------------------------------
--
-- `074` 给分段排了队（等前一段送到、再停 `pace_ms`），**而它排的是
-- 「各自判过的 N 条」**。本条之后排的是「一次判定铺出来的 N 段」，
-- 谓词一个字不用改 —— 它读的是 `ordinal` 与 `pace_ms`，两者都还在。

ALTER TABLE outbox_messages
    DROP CONSTRAINT IF EXISTS outbox_messages_tenant_id_intent_id_key;

ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_tenant_id_intent_id_ordinal_key
    UNIQUE (tenant_id, intent_id, ordinal);

COMMENT ON CONSTRAINT outbox_messages_tenant_id_intent_id_ordinal_key
    ON outbox_messages IS
    '一个意图的一段只许排一行（076，ADR-0152）。在此之前是 '
    'UNIQUE (tenant_id, intent_id) —— 一个意图一行，而那条禁止了'
    '「整条判一次、批准之后拆成几个气泡」。ordinal 是 NOT NULL DEFAULT 0，'
    '所以对本迁移之前的行这条约束与旧的逐字等价。';

INSERT INTO schema_migrations(version) VALUES ('076_one_judgement_many_rows')
ON CONFLICT (version) DO NOTHING;
