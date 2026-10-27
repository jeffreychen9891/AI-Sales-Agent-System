"""那两个引用数组**每条路径都要刷**，而「活着」的定义要跟着迁移走（B1-5）。

**B1-5（核心图纸对比 2026-09-13）**：`active_intent_refs` / `scheduled_event_refs`
从会话创建那一刻起就是 `[]`，**零写点、零读点** —— RFC §3.2 说它们
「只存引用，事实各自在权威表」，而今天两张权威表都在，差的就是这份引用。

**为什么前两条是源码断言**：刷新是一句**在事务末尾**的调用，
而「末尾」这件事没有类型、没有返回值、漏了不会有任何东西报错 ——
它与 `test_retrieval_wiring.py` 守的是同一形状（构造了但没交给管线，等于没接）。
**行为面在** `tests/integration/test_reference_projection.py`（数组与权威表逐一对上）。

## 第三条被退回过一次，值得写在这里

初版的事件那一半是**自证的**：`event_vocab` 用一个**写死七个名字**的正则
去迁移里找那七个名字，于是**迁移里新增的状态永远进不了这个集合**，
而末句 `assert set(_LIVE_EVENT_STATUSES) == {"PENDING", "CLAIMED", "ENQUEUED"}`
把常量钉给了它自己 —— **docstring 声称防的正是词表漂移，而它一条漂移都拦不住**。
（复核窗口 2026-09-13 退回，卡 `CLAUDE.md` 第三节末段「守卫本身也可能是空的」。）

**现在的写法**：词表**从迁移里解析出来**，谁都不写死；两张表各断言
「活着的 ⊂ 词表」与「词表 − 活着的 == 那几个终态」——
**第二条才是会红的那一条**：迁移里加一个状态，它既不在活着的集合里、
也不在终态集合里，两者的差就变了。

`scheduled_events` 的 `status` 带 `DEFAULT 'PENDING'`、`intent_records` 的不带，
所以解析器按**块**取（先切出 `CREATE TABLE`，再在块内找 `status … CHECK`），
不靠一条能同时匹配两种写法的正则。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"
MIGRATION = ROOT / "infrastructure/migrations/001_m0.sql"

#: 每张表的**终态**，逐个写出来 —— 这是本文件唯一写死的东西，而它写死是有意的：
#: 「哪些状态算结束了」是一条**业务判断**，不能从 SQL 里读出来。
#: 迁移里新增一个状态时，它既不在 `_LIVE_*` 里也不在这里，于是下面那条差集断言会红，
#: 而**红的那一刻正是有人要判断「这个新状态算活着还是结束」的那一刻**。
TERMINAL = {
    "intent_records": {"APPLIED", "DENIED", "COMPENSATED", "FAILED"},
    "scheduled_events": {"CONSUMED", "INVALIDATED", "CANCELLED", "FAILED"},
}


def _source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _create_table_block(sql: str, table: str) -> str:
    """切出一张表的 `CREATE TABLE` 块。

    **按块取，不在整个文件上找**：`001_m0.sql` 里有十几张表，
    而 `scheduled_events` 与 `intent_records` 都有一列叫 `status`。
    一条在整文件上跑的正则会拿到先出现的那一张，**而「先出现」不是判据**。
    """

    start = re.search(rf"CREATE TABLE(?: IF NOT EXISTS)? {table}\s*\(", sql)
    assert start, f"{table} 的 CREATE TABLE 不在 {MIGRATION.name} 里"
    rest = sql[start.end() :]
    end = re.search(r"\n\);", rest)
    assert end, f"{table} 的 CREATE TABLE 没有收尾"
    return rest[: end.start()]


def _status_vocabulary(table: str) -> set[str]:
    """从迁移里**解析**出这张表 `status` 的封闭词表。

    `DEFAULT 'PENDING'` 是可选的一段（两张表写法不同），
    而词表本身由 `CHECK (status IN (...))` 里的引号值决定 ——
    **一个名字都不写死**，否则这条用例就又变成自证的。
    """

    block = _create_table_block(MIGRATION.read_text(encoding="utf-8"), table)
    match = re.search(
        r"status\s+text\s+NOT\s+NULL(?:\s+DEFAULT\s+'[A-Z_]+')?\s*"
        r"CHECK\s*\(\s*status\s+IN\s*\((?P<values>.*?)\)\s*\)",
        block,
        re.S,
    )
    assert match, f"{table} 的 status CHECK 没解析出来 —— 迁移的写法变了，先改这个解析器"
    values = set(re.findall(r"'([A-Z_]+)'", match.group("values")))
    assert len(values) >= 5, (
        f"{table} 只解析出 {sorted(values)} —— 一个空/半空的词表会让下面每条断言都变成摆设"
    )
    return values


def test_both_paths_refresh_the_projection() -> None:
    """发送路径与不发送路径**各调一次**。

    两条路径共用同一个聚合写回（`_apply_aggregate_transition`），
    **而刷新不能放在那里**：那句跑在事务的第一条，
    本轮自己的意图与定时事件都在它之后才写入，放进去会永远漏掉本轮那几条。
    """

    source = _source()

    assert source.count("_refresh_reference_projection(connection, prepared)") == 2, (
        "发送路径与不发送路径各一次 —— 少一次就是那条路上的引用永远停在上一轮"
    )
    assert source.count("def _refresh_reference_projection(") == 1, "只该有一处定义"


def test_it_is_the_last_statement_of_the_transaction_it_sits_in() -> None:
    """**它必须排在本轮所有写入之后**，否则本轮自己的那几条不在里面。

    这一条用缩进读位置：刷新那一句之后，同一个 `with` 块里不许再有别的语句
    （注释与空行不算）。**不是洁癖** —— 不发送那条路径上，
    `_file_task_for_unsendable_intents` 还会再插意图行，
    刷新排在它前面的话，那几条人工任务意图就永远不在引用里。
    """

    lines = _source().splitlines()
    for index, line in enumerate(lines):
        if "_refresh_reference_projection(connection, prepared)" not in line:
            continue
        if line.strip().startswith("#") or "def " in line:
            continue
        indent = len(line) - len(line.lstrip())
        for follower in lines[index + 1 :]:
            if not follower.strip() or follower.strip().startswith("#"):
                continue
            following_indent = len(follower) - len(follower.lstrip())
            assert following_indent < indent, (
                f"第 {index + 1} 行之后同一个块里还有语句：{follower.strip()[:60]!r} —— "
                "刷新必须是本事务的最后一句，否则它之后写入的意图/事件不在引用里"
            )
            break


def test_the_parser_reads_the_migration_and_not_the_constants() -> None:
    """**先证明解析器真的在读迁移**，再让它去判别的事。

    初版那条自证用例的病根是「拿要检的东西当检的依据」。
    这一条把那件事单独钉住：解析出来的词表里**必须有常量里没有的值** ——
    如果解析器（比如正则失配）只回了常量里那几个，这条先红。

    > **这一族有第二层，而本条只覆盖第一层**（2026-09-14 补）：
    > 本条证明的是「**给它源它会读**」（正则没失配、词表不是自证的），
    > **不证明「它今天真的拿到了源」** ——
    > `_status_vocabulary` 读的那个迁移文件一旦被改名或挪走，
    > 读到的就是空，而「空集里没有常量之外的值」会让本条先红 ——
    > **本条这一次恰好红得对**。
    > 但**同形的守卫不一定有这个运气**：
    > 一个「扫一批文件再判」的守卫，文件列表为空时**下面那条差集断言会恒绿**
    > （2026-09-14 那次退回就是这个形状：
    > `test_no_function_binds_evidence_and_action.py` 的扫描根写错会三条全绿）。
    > **补法是给枚举一个有理由的锚，不是一个拍出来的下限数** ——
    > 那一条用的锚是「文件列表里必须有 `pipeline.py`」，
    > 因为那条规则的来源地就在那里。
    """

    from fastagent_v2.worker.pipeline import _LIVE_EVENT_STATUSES, _LIVE_INTENT_STATUSES

    for table, live in (
        ("intent_records", set(_LIVE_INTENT_STATUSES)),
        ("scheduled_events", set(_LIVE_EVENT_STATUSES)),
    ):
        vocabulary = _status_vocabulary(table)
        assert vocabulary - live, (
            f"{table} 解析出来的词表 {sorted(vocabulary)} 里没有任何常量之外的值 —— "
            "解析器很可能没读到迁移，那样下面那条差集断言就是自证的"
        )


def test_a_new_status_in_the_migration_lands_in_neither_set() -> None:
    """**这一条是会因漂移变红的那一条。**

    两张表各断言：`词表 − 活着的 == 那张表的终态集合`。
    于是三种漂移都红：
    * 迁移里**加**一个状态 → 它不在活着的、也不在终态里，差集多一个；
    * `_LIVE_*` 里**去掉**一个 → 差集多一个；
    * `_LIVE_*` 里**加**一个迁移没有的 → 「活着的 ⊂ 词表」那条先红。

    **`UNKNOWN` 在活着的那一侧**，而这不是笔误：§7.1 说它的意思正是
    「副作用已发出、还没对账」—— 一条要对账的意图当然还没结束。

    **那两个终态集合是写死的，而写死是有意的**（取舍留在这里，不只留在交付说明里）：
    「哪些状态算结束了」是一条**业务判断**，从 SQL 里读不出来。
    写死把它变成一个**触发点** —— 迁移里加一个状态时，它既不在 `_LIVE_*` 里、
    也不在 `TERMINAL` 里，这条断言就红，**而红的那一刻正是该有人判断
    「这个新状态算活着还是结束」的那一刻**。
    判据：能从源里解析的就解析（词表），不能的就写死并说明它会在什么时候把人叫来（终态）。
    """

    from fastagent_v2.worker.pipeline import _LIVE_EVENT_STATUSES, _LIVE_INTENT_STATUSES

    for table, live in (
        ("intent_records", set(_LIVE_INTENT_STATUSES)),
        ("scheduled_events", set(_LIVE_EVENT_STATUSES)),
    ):
        vocabulary = _status_vocabulary(table)
        assert live < vocabulary, (
            f"{table}：`_LIVE_*` 里有迁移词表没有的值 —— {sorted(live - vocabulary)}"
        )
        assert vocabulary - live == TERMINAL[table], (
            f"{table} 的终态实测是 {sorted(vocabulary - live)}，"
            f"而本文件声明的是 {sorted(TERMINAL[table])} —— "
            "要么迁移新增了一个状态（那就去判断它算活着还是结束，两处之一加上它），"
            "要么 `_LIVE_*` 漂了"
        )
