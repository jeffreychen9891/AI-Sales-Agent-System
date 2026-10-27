-- 070: 已读游标带上 `ordinal` —— 一轮两条消息的先后，游标要分得清。
--
-- ---------------------------------------------------------------------------
-- 为什么这一列必须存在
-- ---------------------------------------------------------------------------
--
-- 一轮可以写好几行出站，而**一轮写的行 `created_at` 相同**（它是事务时间戳）。
-- `038` 建这张表时游标是三分量 `(last_read_at, last_read_source, last_read_ref)`，
-- 照 ADR-0048 §二.9（K12）「稳定 cursor 用 `(created_at, source_table, id)`」。
--
-- 那个键**对唯一性够用**（uuid 全局唯一），**而它不是投递顺序**：
-- 同一轮的两行在 `created_at` 与 `source_table` 上都相等，于是排序落到 `id` ——
-- 而 `id` 是 `uuid4()`。**逐字记录页里一轮两条消息的先后因此是抛硬币的**：
-- 人读记录时可能看到「兑现」排在「许诺」前面，而每次翻页都可能不同。
--
-- 投递那一侧早就修过同一件事：`044` 给 `outbox_messages` 加了 `ordinal`，
-- 并把领取查询的排序改成 `(created_at, ordinal, id)`，
-- 那段注释逐字写着「`created_at` 是事务时间戳，所以每一行都一样，
-- 排序落到了 `id`，而 `ordinal` 才是这一轮真正想要的位置」。
-- **一处修了，读取那一侧没跟上** —— 2026-09-19 在三个读取方上撞到它（T369）。
--
-- ---------------------------------------------------------------------------
-- 为什么要改游标而不是只改展示序
-- ---------------------------------------------------------------------------
--
-- **只改 `ORDER BY` 不改 keyset 比较，页边界上会漏行或重行。** 分页的比较式
-- 与排序必须是同一个序，所以游标本身要多一个分量 —— 这就是这一列。
--
-- 用户 2026-09-19 裁定：改，带迁移与 ADR 修订（ADR-0140 修订 ADR-0048 §二.9）。
--
-- ---------------------------------------------------------------------------
-- 回填是精确的，不是默认 0
-- ---------------------------------------------------------------------------
--
-- `DEFAULT 0` 只是为了让这一列能 `NOT NULL` 地加上去。**加完立刻按真值回填**：
-- 指向出站行的游标从 `outbox_messages` 连回去取它真正的 `ordinal`。
--
-- 不回填会怎样：一条指向「某轮第二条」的游标会被当成指向「第一条」，
-- 于是那一条消息在那个员工眼里又变成未读。方向是 fail-safe（多显示未读、
-- 不隐藏未读），**而它仍然是错的**，而且是可以精确避免的 ——
-- 与 `022` 那次「能算准就别拍一个默认值」同源。
--
-- 指向入站事件的游标留在 0：`inbox_events` 没有这一列，
-- 而一条入站事件自己占一个事务，它没有「这一轮第几条」这回事。
ALTER TABLE employee_read_cursors
    ADD COLUMN IF NOT EXISTS last_read_ordinal integer NOT NULL DEFAULT 0
        CHECK (last_read_ordinal >= 0);

UPDATE employee_read_cursors AS c
   SET last_read_ordinal = o.ordinal
  FROM outbox_messages AS o
 WHERE c.last_read_source = 'outbox_messages'
   AND o.tenant_id = c.tenant_id
   AND o.id = c.last_read_ref
   AND c.last_read_ordinal <> o.ordinal;

COMMENT ON COLUMN employee_read_cursors.last_read_ordinal IS
    '这条游标指的那一行在它那一轮里的位置 (outbox_messages.ordinal; 入站事件恒 0)。 '
    '游标的第三个分量, 插在 last_read_source 与 last_read_ref 之间 —— '
    '一轮写的行 created_at 与 source_table 都相同, 没有它排序就落到 uuid4() 上。 '
    'ADR-0140 修订 ADR-0048 §二.9 的三分量为四分量。';

INSERT INTO schema_migrations(version) VALUES ('070_read_cursor_ordinal')
ON CONFLICT (version) DO NOTHING;
