"""意图状态机的三段今天**共用一个 principal** —— 而这条事实自带重核（批 6，C1）。

## 图纸要的那一条

RFC:192 逐字：

> 模型只能创建 `PROPOSED`；policy service 只能批准/拒绝；executor 才能进入
> EXECUTING。**一个 service principal 不能兼任三者的权限**

**三段状态迁移都实现了，而三者是同一个进程、同一个 DB 角色**
（全部走 `settings.worker_database_url`）。所以图纸要的那条权限分离**没有**。

## 这一条守的不是「分离」，是「今天没有分离」这件事的完整性

**用户 2026-09-15 裁定 (乙)：登记 + 绊线，而「不是决定不做，是前置未备」。**
前置是一份分阶段方案（先建角色并授权 -> 再改代码换连接 -> 最后才 `REVOKE`），
而那份方案今天不存在。**在它存在之前，这条守卫要做的事只有一件**：
**让「三段迁移的落点」保持可数、可核**，
这样做 (甲) 的那一批不必重新找一遍坐标 —— 而这一批**已经为此付过一次学费**：
`core-vs-blueprint-survey-2026-09-13.md` 的 C1 行写的两个坐标
（`pipeline.py:4112` / `:5948`）**今天分别是 handoff 理由码与 reconcile 时限** ——
**代码搬了家，而那份 survey 不会跟着走。**

> 这是那份 survey 第四次对不上，而**前三次是范围、这一次是坐标**。
> **坐标写进文档会漂；写成一条会红的用例不会。**

## 数出来之后冒出的一件事，它让「前置未备」更具体

~~**创建 `PROPOSED` 的地方有 7 处**（`pipeline.py` 6 + `digest.py` 1）~~

> **更正（2026-09-20，T276）。** 上面那句话有两处不对，而第二处是本批造成的：
>
> 1. **它与本文件自己的 `EXPECTED` 表打架，而打架的那天就打了** ——
>    表里 `("pipeline.py", "create_proposed")` 写的是 **7**，
>    加上 `digest.py` 的 1 是 **8**，不是散文里那个 7（散文把 `pipeline.py`
>    写成了 6）。**两个数写在同一份文件里，隔了四十行，没有任何东西会红** ——
>    正是本仓「带计数的抬头容易与正文脱节」那一条。2026-09-20 现数 `HEAD`
>    的基线，`pipeline.py` 确实是 **7**：`_record_rate_limit` ·
>    `_file_a_task_for_a_promise_only_a_person_can_keep` ·
>    `_file_task_for_unsendable_intents` · `_persist_accepted_proposal` ·
>    `_persist_no_send` · `_notify_manager` · `_manual_task_for_delivery`。
>    **所以红的那一方是散文，不是表。**
> 2. **T276 把其中五处合并成了一个** `pipeline._file_a_manual_task`，
>    于是 `pipeline.py` 从 7 变成 **3**，全仓创建点从 8 变成 **4**。

**今天创建 `PROPOSED` 的地方有 4 处**，而其中多数**不是模型**：

| 那一处 | 谁建的 |
|---|---|
| `pipeline._persist_accepted_proposal` | **模型** —— 这是图纸那句话说的那一个 |
| `pipeline._file_a_manual_task` | 平台 —— **五条路共用它**（限流 /
  许了只有人能做的事 / 需要人 / 死循环升级 / 投递失败）|
| `pipeline._notify_manager` | 平台 |
| `digest.py` 那一处 | 平台 |

**图纸那句「模型只能创建 `PROPOSED`」在代码里仍然没有一个干净的对应**，
所以做权限分离之前要先答的那一问不变：**这 4 个创建者里，哪些算「模型」
那个 principal？** 把平台自建的那几处也划给「模型」，等于给模型一个它本来
没有的身份；划给别的 principal，就要先定那个 principal 是谁。
**这一问今天没有答案，而它正是「前置未备」的具体内容之一。**

⚠️ **T276 让这一问变小了，没有让它消失**：平台自建的那几处从 6 收敛到 3，
而**「哪个是模型」这一问一个字都没答**。

## 判据

把**八个**落点钉成**等值集合**。多一处就红，红的含义是：
**又多了一段「以同一个身份改意图状态」的代码，而 (甲) 那一批要多改一处。**
少一处也红 —— 那说明某一段搬了家或没了，这张表在守一个空位。

**2026-09-20 实测它按后一种红过一次**：T276 那次合并让
`create_proposed` 从 7 掉到 3，这条用例当场红并报出了两个清单的差 ——
**而本批没有一行代码是冲着它去写的。** 它是那次合并的连带，
而连带只能量、不能想（本仓那一条）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WORKER = ROOT / "services/worker/src/fastagent_v2/worker"
SOURCES = (WORKER / "pipeline.py", WORKER / "digest.py")

#: 意图状态机那三段在 SQL 里的样子。**按语句找，不按行号找** ——
#: 行号正是那份 survey 漂掉的东西。
PATTERNS = {
    "create_proposed": re.compile(r"'PROPOSED'\s*,"),
    "to_approved": re.compile(r"SET\s+status\s*=\s*'APPROVED'"),
    "to_executing": re.compile(r"SET\s+status\s*=\s*'EXECUTING'"),
}

#: 今天每个文件里每一段各有几处。**等值，不是上界。**
#:
#: 这几个数是 2026-09-15 现数的，而它们的用处是给做 (甲) 的那一批一张
#: **不会漂的清单** —— 那一批要把每一处都换成对应 principal 的连接。
#:
#: ⚠️ **2026-09-20（T276）`create_proposed` 7 -> 3**：五处落人工待办的
#: 创建点合并成了 `pipeline._file_a_manual_task` 一处（5 去 1 回，净 -4）。
#: **那一批不是冲着这条用例来的，是这条用例把它拦下来报的数** ——
#: 合并之后要改 principal 的地方少了四处，而「哪一处算模型」仍然没答。
EXPECTED = {
    ("pipeline.py", "create_proposed"): 3,
    ("pipeline.py", "to_approved"): 2,
    ("pipeline.py", "to_executing"): 1,
    ("digest.py", "create_proposed"): 1,
    ("digest.py", "to_approved"): 1,
}

#: worker 进程里出现的连接设置，以及它为什么在那里。
#:
#: **`scheduler_database_url` 在 `cli.py` 里不是分离的证据** —— 同一个模块
#: 同时是 scheduler 的入口，而 scheduler 不改意图状态。分类比禁止诚实
#: （批 2 / 批 3 / 批 4 都收敛到了这个形状）。
KNOWN_CONNECTIONS = {
    "worker_database_url": "三段意图迁移全都跑在它上面 —— 这就是 C1 说的那一个 principal",
    "scheduler_database_url": "**同一个 `cli.py` 也是 scheduler 的入口**，"
    "而 scheduler 不碰 `intent_records`；它在这里不是权限分离的证据",
}


def _counts() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for source in SOURCES:
        text = source.read_text(encoding="utf-8")
        for name, pattern in PATTERNS.items():
            hits = len(pattern.findall(text))
            if hits:
                found[(source.name, name)] = hits
    return found


def test_the_transition_sites_are_still_the_ones_we_counted() -> None:
    """八个落点，逐处等值。

    红的时候要答的是：**这一处也要跟着换 principal 吗？**
    —— 而那个问题只有在 (甲) 那一批才真正被处理；在那之前，
    这条用例的作用是**不让清单悄悄变长**。
    """

    counts = _counts()
    assert counts == EXPECTED, (
        f"意图状态机的落点变了：现读 {sorted(counts.items())}，"
        f"登记的是 {sorted(EXPECTED.items())}。"
        "**多一处 = 又多了一段以同一个身份改意图状态的代码**，"
        "做权限分离的那一批要多改一处；少一处 = 某段搬了家，这张表在守空位。"
        "两种都请更新这里与 C1 的登记"
    )


def test_all_three_stages_still_run_on_one_connection() -> None:
    """**另一头**：三段确实还在共用一个连接设置。

    **只钉「落点有几处」是不够的** —— 那样即使有人真的给某一段换了连接，
    上面那条照旧全绿，而这份 docstring 说的「共用一个 principal」就成了假话。
    **这一条是那句话自己的守卫**（批 4 / 批 5 那条纪律的第三次应用：
    一条断言配一条守卫，而那条守卫要守断言的每一半）。

    **它反过来也有用**：真做分离的那一批，这一条会红 ——
    而那正是「该回来改登记」的信号。
    """

    cli = (WORKER / "cli.py").read_text(encoding="utf-8")
    assert "worker_database_url" in cli, (
        "worker 不再用 `worker_database_url` 了 —— 要么换了名字，"
        "要么权限分离真的开始了。两种都请更新 C1 的登记"
    )

    others = set(re.findall(r"settings\.(\w*database_url)", cli))
    assert others == set(KNOWN_CONNECTIONS), (
        f"worker 进程里的连接设置变了：现读 {sorted(others)}，"
        f"已分类的是 {sorted(KNOWN_CONNECTIONS)}。"
        "**如果这是权限分离的第一步，那很好** —— 请连同 C1 的登记、"
        "以及那份分阶段方案（先建角色授权 -> 再换连接 -> 最后 REVOKE）一起更新"
    )
