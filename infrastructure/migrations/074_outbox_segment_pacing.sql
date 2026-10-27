-- 074: 一条分段消息的后一段，要等前一段送到、再停一会儿（ADR-0151，T211 的一半）。
--
-- 用户 2026-09-23 裁定：「本来每段话之间就应该有延迟，**人打字还要时间的**」。
--
-- **两个条件，不是一个。** 第一个（前一段必须已 `DELIVERED`）是取件谓词的事，
-- 不需要列。第二个需要一个数，而它**逐段不同** —— 一段的停顿按它自己的字数算，
-- 所以它落在行上。
--
-- **`pace_ms` 是一列，不是 `command` 里的一格。** 迁移 `044` 为同一个问题写过
-- 理由，这里逐字照办：一个索引带得动列，而出站取件是热路径 ——
-- 按 JSON 表达式排序/过滤要表达式索引，而且同一个写法要在两个地方拼对。
--
-- **`NOT NULL DEFAULT 0`，两半都要紧**（同 `044` 的 `ordinal`）：
-- 本迁移之前写的行没有「该停多久」可恢复 —— 那个信息从来没有被存过 ——
-- 所以它们全取 0，而取件谓词对 `pace_ms = 0` 的行**恒真**，
-- 它们的行为与今天逐字相同。**不给它们发明任何东西。**
--
-- ⚠️ **这一列不是「延迟发送」的通用机制。** 它只被同一轮内的分段读到：
-- 谓词先问「同轮里有没有序号更小、还没 `DELIVERED` 的行」，
-- 没有才轮到 `pace_ms`。一条独来独往的消息 `pace_ms = 0`，立刻可取。
-- 跨轮的重试排序是 **T211 的另一半**，本条不碰（那要它自己的裁定）。

ALTER TABLE outbox_messages
    ADD COLUMN IF NOT EXISTS pace_ms integer NOT NULL DEFAULT 0
        CHECK (pace_ms >= 0);

-- 取件谓词要按「同一轮」把同伴找出来，而「同一轮」的标识是幂等键的第二段
-- （源事件 id）—— 与 `pipeline.TURN_KEY_SQL` 是同一个表达式，
-- 限流那一侧也读它。**两处必须是同一个写法**，否则一边认得出同轮、
-- 另一边认不出，而认不出的那一边会放行一个本该等着的段。
--
-- 部分索引：只有真的分过段的行才需要被这样找（`ordinal > 0`）。
CREATE INDEX IF NOT EXISTS outbox_messages_turn_segment_idx
    ON outbox_messages (tenant_id, conversation_id, split_part(idempotency_key, ':', 2), ordinal)
    WHERE ordinal > 0;

INSERT INTO schema_migrations(version) VALUES ('074_outbox_segment_pacing')
ON CONFLICT (version) DO NOTHING;
