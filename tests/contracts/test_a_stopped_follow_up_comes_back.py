"""静默时段拒掉的那次跟进，时段一过要再来一次（ADR-0148）。

## 在此之前它就那样没了

那一行定时器已经 `CONSUMED`，而 `next_allowed_at`
（静默结束的那一刻，`quiet_hours.lifts_at` 算的）
**只被写进 policy hook 当留痕 —— 算出来了，没有任何东西据它行动**。

运维复测第 7 条记过同一族：
「静默时段挡下的跟进正文无留痕，占那通成本 27%」。

用户 2026-09-21：「**晚上计划跟进被档，过了静默时间应该触发被档的那次跟进**」。

## ⚠️ 这一族的错是安静的

补排没做、或者只对某个理由码做错了，**没有任何东西会红**：
那一轮照常记 no-send、照常写 policy hook、照常消费掉 inbox 事件。
**只有客户第二天早上什么都没收到。**
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastagent_v2.worker.proactivity import QUIET_HOURS_REASON

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)


def _is_docstring(node: ast.stmt) -> bool:
    """那一段散文 —— **判据不能读它**（本仓「守卫读到自己的散文」那一族）。"""

    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def _function(name: str) -> ast.FunctionDef:
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`{name}` 不在 pipeline.py 里了 —— 补排机制被删了")


def test_the_reschedule_only_fires_for_quiet_hours() -> None:
    """⚠️ **只对 `PROACTIVITY_QUIET_HOURS` 补排，另外两种拒绝都不重排。**

    退出信号是一条关于这通对话的**常驻**拒绝 —— 重排它等于无视客户说的「别再发了」；
    一个超窗的 `silence_check` **等下去也不会变得可发**
    （RFC §6.4 那条绕行禁令：不得借旧 silence event 绕过再触达配额）。

    `quiet_hours` 是三者里唯一「不是现在」的那一个，
    **也是唯一说得出自己何时不再为真的那一个**。
    """

    source = ast.unparse(_function("_reschedule_after_quiet_hours"))
    assert "QUIET_HOURS_REASON" in source, (
        "补排不再按理由码筛了 —— 退出信号与超窗的 silence_check 会被一起重排"
    )
    assert "next_allowed_at is None" in source, (
        "没有守住「算不出那一刻就不排」—— 会排出一行 due_at 为空的定时器"
    )


def test_the_new_row_cannot_collide_with_the_one_that_was_consumed() -> None:
    """⚠️ **`dedupe_key` 必须带后缀，否则那一行根本插不进去。**

    唯一键是 `(tenant, conversation, generation, dedupe_key)`，而原来那一行
    **还在**（`CONSUMED`）。照原键插会 `ON CONFLICT` 命中它，
    而写入方的 `DO UPDATE … WHERE status = 'PENDING'` 不成立 ——
    **于是什么都不会发生，而且一声不吭。**

    这一条钉的正是那个后缀。
    """

    # ⚠️ **把 docstring 摘掉再看** —— 第一版读的是 `ast.unparse(整个函数)`，
    # 而那个函数的 docstring 里**逐字写着** `:after_quiet_hours` 后缀。
    # 反向探针 E 支当场证明那是空的：把 `dedupe_key` 换成原键，
    # 判据在散文里照样找得到那个词，**一声不吭**。
    # 本仓「守卫读到自己的散文」那一族。
    function = _function("_reschedule_after_quiet_hours")
    body = [node for node in function.body if not _is_docstring(node)]
    source = "\n".join(ast.unparse(node) for node in body)
    assert "after_quiet_hours" in source, (
        "补排那一行用的是原 `dedupe_key` —— 它会静默地插不进去"
    )


def test_it_runs_inside_the_no_send_transaction() -> None:
    """**补排与那一轮的 no-send 在同一个事务里。**

    分两个事务的话，进程在中间挂掉就会出现两种半截状态：
    记了 no-send 而没有补排（跟进丢了），或者补排了而 no-send 没记
    （那一轮在账上从没发生过）。
    """

    persist = _function("_persist_no_send")
    called = {
        node.func.id
        for node in ast.walk(persist)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_reschedule_after_quiet_hours" in called, (
        "`_persist_no_send` 不再补排了 —— 静默时段挡下的跟进又没了"
    )


def test_the_reason_code_is_the_one_proactivity_actually_emits() -> None:
    """⚠️ **不是抄一份字符串来核字符串。**

    补排那一侧筛的理由码，必须就是 `proactivity` 真正发出的那一个 ——
    两边各写一份字面量时，改了一边另一边不会响。
    """

    assert QUIET_HOURS_REASON == "PROACTIVITY_QUIET_HOURS", (
        f"静默时段的理由码变成了 {QUIET_HOURS_REASON!r} —— 补排那一侧要跟着改"
    )
