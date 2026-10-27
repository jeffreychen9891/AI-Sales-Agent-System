-- B4-5 / ADR-0119: 「几点可以再发」落成一列，不落 `context`。
--
-- ## 为什么是一列
--
-- `policy-hook.schema.json` 早就为 `next_allowed_at` 留了位置，而落地之前它在
-- `pipeline.py` 里是**两处字面量 `None`**——contract 对象校验完即丢，库里没有任何
-- 地方存过它。ADR-0117 §四 立的通则正是为这件事：**一个批次不得交付「算了但恒为空」
-- 的字段**，字段与它的消费方同批到达。
--
-- 另一条路是塞进 `policy_decisions.context`（jsonb）。ADR-0117 §四 的补充标注确实
-- 豁免了证据键，**但豁免的边界是「不追究」，不是「算达标」**：塞进 jsonb 之后它仍然
-- 没有类型、没有索引，查询要 `->>` 之后再 cast——**而那正是立这条通则时点名的那个病**
-- （「真实的值以自由文本落在 `context.detail` 里……为它准备的那个类型化字段是装饰」）。
-- **用同一个病去满足为治它而立的判据，不成立。** 用户 2026-09-09 裁定取列。
--
-- ## `NULL` 在这里有语义，不是「还没实现」
--
-- `NULL` = **这次拒绝没有一个自动失效的时刻**。退出信号的拒绝就是 `NULL`：客户说了
-- 别再发，不存在「几点以后可以」。静默时段的拒绝则一定非 `NULL`——窗口有边界。
-- **一正一反两条路径本批都有用例**，这是 ADR-0117 §四 裁定 4 那一问
-- （「本批填写的每个字段，有没有一条取值不是常量的路径？」）的标准答法。
--
-- ## 权限与隔离随表，不另开
--
-- `001` 已经 `GRANT` 到表级并给 `policy_decisions` 开了 RLS
-- （`policy_decisions_tenant_isolation`），`043` 的 `purge_tenant_data` 按 tenant 删本表。
-- 新列**继承这三样**，所以本迁移不加 `GRANT`、不改 RLS、不改清除逻辑。
-- 这与 `047` 选择把原始载荷放在既有表上的理由相同：**新表要把这三样重新推导一遍，
-- 而最可能的结果是清除忘了它。**
--
-- ## 它不改任何既有行的含义
--
-- 加列可空、无默认值，既有行读出来是 `NULL`——按上面的语义，那正确地表示
-- 「这些拒绝没有一个自动失效的时刻」，**不需要回填**。

ALTER TABLE policy_decisions
    ADD COLUMN IF NOT EXISTS next_allowed_at timestamptz;

COMMENT ON COLUMN policy_decisions.next_allowed_at IS
    'When this refusal stops being true, for refusals that expire by the clock '
    '(B4-5 quiet hours, ADR-0119). NULL means this refusal has no automatic '
    'expiry -- an exit signal is NULL, a quiet-hours refusal never is. The '
    'value is computed in the profile IANA zone (RFC 6.2) and stored UTC; the '
    'DST reasoning behind it is kept in `context`, which answers a different '
    'question: this column says when, `context` says how it was worked out.';

INSERT INTO schema_migrations(version) VALUES ('049_policy_decision_next_allowed_at')
ON CONFLICT (version) DO NOTHING;
