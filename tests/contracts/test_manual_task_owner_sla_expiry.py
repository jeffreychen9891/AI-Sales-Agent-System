"""**C2：`manual_review` 那条任务要的 owner / SLA / expiry，今天谁填谁不填。**

图纸 §8 要的是「`manual_review` 创建有 **owner/SLA/expiry** 的 manual task」。
`034_manual_tasks.sql` 把三列都建了出来，而**建列不等于有人填**。

## 今天的分工（本文件把它钉住）

| 那一列 | 引擎侧（`services/worker`） | 人工侧（控制面 route，`services/api`） |
|---|---|---|
| `owner_employee_id` | ❌ 不填 | ✅ 建单时填，认领 / 改派时也改 |
| `sla_due_at` | ❌ 不填 | ✅ 建单时填 |
| `expires_at` | ❌ 不填 | ❌ **也不填** |

**`expires_at` 是三列里最值得单独看的那一个**：它**全仓零写者**，
而读模型**照样把它显示出来**（`app.py` 的 `_iso_timestamp_or_none(row["expires_at"])`）。
于是屏幕上有一格「过期时间」，它永远是空的 —— 而「这个任务不过期」与
「没人填过这一格」在屏幕上长得一模一样。
**这与 `commitment_refs` 那个 0 是同一个形状**（`commitments_display.py`：
一个会误导人的真值），只是那一个已经处置过，这一个还没有。

## 为什么引擎不填不是缺陷

`_persist_no_send` 里那段注释写着：**§8 要三样而引擎没有任何一样的来源**
（登记 **T91**；freeze B3 #16 让 owner 与 SLA 在人工那一侧成为必填）。
**这是一次有意的取舍，不是遗漏** —— 而在这一份之前，它只是一段注释。

## 叠加在上面的那件事：这条路今天没有起点

`effect="manual_review"` 的发出点**今天是 0**（下面有断言）。
所以「`manual_review` 创建的任务」这条路**本身还没有起点** ——
引擎建的任务走的都是别的理由码（限流、许了只有人能做的事、需要人、
死循环升级、投递失败）。

⚠️ **2026-09-20（T276）起，引擎侧建单只有一个定义点**
（`pipeline._file_a_manual_task`），上面那五条路都调它。
本文件那条 `ENGINE_INSERT_SITES == 1` 因此从一个计数变成了一条绊线。

⚠️ **顺带覆盖不等于处置完**：这条断言把 `manual_review` 的零发出钉住了，
**而 `effect="modify"` 的零发出（登记 B1-9）不在本批范围** ——
它要的 `modifications: typed bounded patch` 机制根本不存在，那是另一件事。
本文件不碰它，B1-9 **仍然开着**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "services" / "worker"
API = ROOT / "services" / "api"

#: §8 要的那三样，按建表时的列名。
OWNER_SLA_EXPIRY = ("owner_employee_id", "sla_due_at", "expires_at")

#: 引擎侧今天有几处建人工任务。**写成等值，不是上界** ——
#: 写成「≥」的话，一次什么都没扫到的失败会静悄悄通过。
#:
#: ⚠️ **2026-09-20（T276）5 -> 1**：五处合并成了 `pipeline._file_a_manual_task`。
#: **这个数从此不再是「有几处」，而是「只该有一处」** ——
#: 它此前是一个会跟着涨的计数（4 -> 5），现在是一条单一写入点的绊线，
#: 与 `policy_output.py` 那个「`policy_decisions` 的唯一写入点」同一个形状。
#: T276 已关闭，所以再没有一条登记在替这个数记账 —— **绊线就是账本**。
ENGINE_INSERT_SITES = 1

_INSERT_MANUAL_TASKS = re.compile(
    r"INSERT\s+INTO\s+manual_tasks\s*\((?P<columns>[A-Za-z0-9_,\s]+?)\)",
    re.IGNORECASE,
)


def _sources(tree: Path) -> list[Path]:
    return sorted(
        path
        for path in tree.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def _manual_task_inserts(tree: Path) -> list[tuple[str, set[str]]]:
    """`(文件, 这条 INSERT 点名的列集合)`，逐条。"""

    found: list[tuple[str, set[str]]] = []
    for path in _sources(tree):
        source = path.read_text(encoding="utf-8")
        for match in _INSERT_MANUAL_TASKS.finditer(source):
            columns = {
                column.strip()
                for column in match.group("columns").split(",")
                if column.strip()
            }
            found.append((path.relative_to(ROOT).as_posix(), columns))
    return found


def test_the_scanner_found_both_sides() -> None:
    """**守卫的守卫**：下面两条一条断言「引擎不填」、一条断言「人工侧填」。

    前者是「零」，扫描器坏掉时也是零；后者是它的**非零对照** ——
    两条必须一起看，所以先把「两侧都扫到了」钉住。
    """

    engine = _manual_task_inserts(WORKER)
    human = _manual_task_inserts(API)

    assert len(engine) == ENGINE_INSERT_SITES, (
        # 1) 看到了什么
        f"引擎侧建人工任务的地方现在有 {len(engine)} 处（应当只有 {ENGINE_INSERT_SITES} 处）："
        f"{[path for path, _ in engine]}。"
        # 2) 为什么这条要红
        "\n**这个数是 1，不是「现在碰巧有几处」**：T276 把五处合并成了"
        " `pipeline._file_a_manual_task`，而合并的理由是那五处已经漂成了"
        "三种互不相同的幂等语义，"
        "**其中一处的 `ON CONFLICT` 写了却没有人读它的答案**。"
        # 3) 到期了该做什么
        "\n**所以正确的做法不是把这里的数改大** —— 是去调"
        " `_file_a_manual_task`。新那一处真的合不进那个签名时，"
        "说明它要的东西与另外五处不同，那件事本身要先说清楚"
        "（`api/app.py` 那一处就是一个合不进来的例子，理由写在 helper 的 docstring 里）。"
        "\n另：引擎侧不填 owner/SLA/expiry（T91），新那处也不该填。"
    )
    assert len(human) == 1, (
        f"控制面建人工任务的地方现在有 {len(human)} 处（此前 1 处）："
        f"{[path for path, _ in human]}"
    )


def test_the_engine_fills_none_of_the_three() -> None:
    """**引擎侧那一处建单，owner / SLA / expiry 一个都不点名（T91）。**

    这不是缺陷，是一次有意的取舍：§8 要三样而引擎没有任何一样的来源。
    **在这一份之前它只是 `_persist_no_send` 里的一段注释。**

    **它红的时候多半是好事**：有人给引擎找到了这三样的来源 ——
    而那正是该把 T91 关掉的时刻。
    """

    filling = [
        (path, sorted(columns & set(OWNER_SLA_EXPIRY)))
        for path, columns in _manual_task_inserts(WORKER)
        if columns & set(OWNER_SLA_EXPIRY)
    ]
    assert not filling, (
        # 1) 看到了什么
        f"引擎侧这几处开始填那三列了：{filling}。"
        # 2) 为什么这条要红
        "\n此前引擎一列都不填，理由写在 `_persist_no_send` 的注释里："
        "§8 要 owner/SLA/expiry 三样，**而引擎没有任何一样的来源**"
        "（没有一张图纸说过 SLA 该是多久，也没有一条 route 在建单时指派 owner）。"
        # 3) 到期了该做什么
        "\n**如果引擎真的有来源了，那么 T91 到期了**：去把它关掉，"
        "并确认这三样的语义与人工侧那一份一致（freeze B3 #16 让 owner 与 SLA "
        "在那一侧是必填的）。"
    )


def test_the_human_side_supplies_owner_and_sla() -> None:
    """**非零对照，同时也是「谁填」那一半的事实。**

    控制面那条建单 route 点名 `owner_employee_id` 与 `sla_due_at` ——
    这是 T91 说的「人工那一侧供给」，也是上一条断言的对照：
    扫描器要是坏了，这一条会先红。
    """

    human = _manual_task_inserts(API)
    assert human, "控制面那条 `INSERT INTO manual_tasks` 扫不到了"
    columns = human[0][1]
    assert "owner_employee_id" in columns and "sla_due_at" in columns, (
        f"控制面建单不再点名 owner 与 SLA（点名的是 {sorted(columns)}）—— "
        "freeze B3 #16 要求它们在这一侧必填，而这是引擎侧那条「零」的唯一对照"
    )


def test_nothing_anywhere_ever_writes_expires_at() -> None:
    """**三列里 `expires_at` 是两侧都不填的那一个，而它照样显示在屏幕上。**

    读模型有一格 `"expires_at": _iso_timestamp_or_none(row["expires_at"])`，
    而全仓**没有任何 `INSERT` 或 `UPDATE` 写过它** ——
    于是那一格永远是空的，**而「这个任务不过期」与「没人填过这一格」
    在屏幕上长得一模一样**。

    **和 `commitment_refs` 那个 0 是同一个形状**（一个会误导人的真值），
    区别只是那一个已经处置过（显示「未启用」），这一个还没有。
    **本条不处置它，只把它钉成一个事实** —— 处置要先答「任务该不该过期」，
    那是 T91 / B3 那一批的事。
    """

    writers: list[str] = []
    for tree in (WORKER, API):
        for path in _sources(tree):
            source = path.read_text(encoding="utf-8")
            for match in _INSERT_MANUAL_TASKS.finditer(source):
                if "expires_at" in match.group("columns"):
                    writers.append(f"{path.relative_to(ROOT).as_posix()} (INSERT)")
            for match in re.finditer(
                r"UPDATE\s+manual_tasks\s+SET\s+(?P<sets>[^;]{0,400}?)WHERE",
                source,
                re.IGNORECASE | re.DOTALL,
            ):
                if "expires_at" in match.group("sets"):
                    writers.append(f"{path.relative_to(ROOT).as_posix()} (UPDATE)")

    assert not writers, (
        f"`manual_tasks.expires_at` 有写者了：{writers}。"
        "\n此前它全仓零写者，而读模型照样显示它 —— 那一格永远是空的。"
        "\n有人开始填它了，就去把读模型那一格重看一遍（空值现在有两种含义了），"
        "并在 T91 里记下这一列已经有来源。"
    )


def test_the_manual_review_effect_still_has_no_origin() -> None:
    """**「`manual_review` 创建的任务」这条路今天没有起点。**

    `effect="manual_review"` 的发出点是 **0**，所以引擎侧那唯一一处建单
    被调到的五条路走的都是别的理由码（限流 / 许了只有人能做的事 /
    需要人 / 死循环升级 / 投递失败）。
    C2 那条偏移因此是双层的：**三列没人填，而要求填它们的那条路还没开通。**

    ⚠️ **`effect="modify"` 也恒 0，而那是登记 B1-9，不在本批范围。**
    本条只断言 `manual_review`（它是 C2 自己的前置），
    **不把 B1-9 顺带算成处置完了** —— 它要的 `modifications: typed bounded patch`
    机制根本不存在，那是另一件事，**仍然开着**。
    """

    emitters = [
        path.relative_to(ROOT).as_posix()
        for tree in (WORKER, API)
        for path in _sources(tree)
        if 'effect="manual_review"' in path.read_text(encoding="utf-8")
    ]
    assert not emitters, (
        f"{emitters} 开始发 `effect=\"manual_review\"` 了 —— C2 那条路有起点了。"
        "\n那么它创建的任务该带 owner/SLA/expiry（图纸 §8），而引擎今天填不出来："
        "去 T91，先答「SLA 多久、owner 是谁」，再让这条路发出来。"
    )
