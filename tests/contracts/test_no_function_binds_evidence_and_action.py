"""没有哪个函数**同时**直接写「证据表」与「动作表」。

## 它与裁定二那条绊线**不是重复，是同一件事的另一半**

两条从不同角度看同一个东西 —— `policy_decisions` 那个唯一写入点：

* **`test_policy_output.py::test_there_is_exactly_one_write_point`** 数的是
  **`INSERT INTO policy_decisions` 在全仓出现几次**（扫文件系统，
  断言命中的文件清单恰好等于那一个路径）。
  **它的缺口**：数得出「只有一处」，**数不出「那一处所在的函数还干了什么」**。
* **本文件**问的是**有没有函数同时直接写证据表与动作表**。
  **它的缺口**：认得出「绑在一起」，**认不出「有几个入口」**。

**两条都绿，才说明那个唯一写入点真的是唯一入口。**
单看任何一条都留着一个缺口 —— 所以**这两条谁也不能替代谁**。

## 判据是从一次实测的失败里剥出来的，不是「职责太多」

2026-09-13（T339 第二批）：说话那条路只想要 `_persist_no_send` 的一半
（「记下这次拒绝」），不想要另一半（「这一轮不发消息」）——
**于是它跳过了整个调用，把证据一起跳过了**，第一轮 0 行 deny。

剥到最里面是一句可机器判的话：

> **想要它一半的调用方，只能整个不调它。**

而它的代理是：**一个函数同时写「证据表」与「动作表」** ——
证据行说「这件事发生过」，动作行说「因此做了什么」，
**两者的调用方可以是不同的人**。

## 盲区，写清了才算数

本条按**函数体内的 SQL 字面量**认表，所以：

* **读不到经辅助函数间接写的表**（`_apply_aggregate_transition` 那一类）；
* **认不出「本身不写表、只调两个各写一类表的辅助函数」** ——
  `_persist_no_send` 今天正是那一种，**所以它不在本条的命中里**
  （T341 的可行性报告里那份「第二刀」列了它，而**第二刀有意不做成门禁**：
  它今天 4 处而三处是有意的，一道要豁免名单的门禁会被往名单里加东西）。

**所以本条给的是下界。** 它钉住的是「**没有人直接把两类表写在一个函数里**」，
而那正好等于「证据表只经唯一写入点写」的另一面。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: 证据表：行的含义是「这件事发生过」
EVIDENCE_TABLES = frozenset(
    {"policy_decisions", "audits", "model_invocation_costs", "frame_entry_audits"}
)

#: 动作表：行的含义是「因此做了什么」
ACTION_TABLES = frozenset(
    {
        "outbox_messages",
        "manual_tasks",
        "scheduled_events",
        "conversation_aggregates",
        "inbox_events",
        "profile_releases",
        "channel_bindings",
    }
)

_WRITE = re.compile(r"(?:INSERT\s+INTO|UPDATE)\s+([a-z_]+)", re.IGNORECASE)


def _tables_written(body: str) -> set[str]:
    return {table.lower() for table in _WRITE.findall(body)}


def _functions(path: Path) -> list[tuple[str, str]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    out: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = node.end_lineno or node.lineno
            out.append((node.name, "\n".join(lines[node.lineno - 1 : end])))
    return out


def _production_files() -> list[Path]:
    roots = (
        _ROOT / "services/worker/src/fastagent_v2",
        _ROOT / "services/api/src/fastagent_v2",
        _ROOT / "services/ingress/src/fastagent_v2",
        _ROOT / "packages/runtime/src/fastagent_v2",
    )
    return [
        path
        for root in roots
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in str(path)
    ]


def test_the_scanner_reads_source_and_would_catch_the_shape() -> None:
    """**解析器的守卫，两层。**

    **第一层 —— 给它源它会读**：喂一段**仓里不存在的**代码，要求它认出那个形状；
    再喂一段只写一类表的，要求它**不**报。这一层覆盖的是**正则与词表**。

    **第二层 —— 它今天真的拿到了源**（2026-09-14 退回补的）。
    第一层有一个自己的盲区：**它喂的是字符串字面量，
    所以它证明了「给它源它会读」，没证明「它今天读到的是仓里的东西」。**
    `_production_files()` 里任何一个扫描根写错、或那个目录被挪走，
    `rglob` **静默地返回空**，于是下面那条差集断言在空集上恒绿 ——
    **三条全绿，而什么都没扫。**

    **锚取 `pipeline.py`，不取一个下限数**：
    `assert len(files) > 100` 是**拍出来的**数，而且扫描面缩小一半也不会红。
    而 `pipeline.py` 是**有理由的锚** —— 本条这条规则的**来源地**就在那里
    （`_persist_no_send` 绑着两件事那次）。
    **哪天 `pipeline.py` 被拆了它红得也对**：扫描面确实变了，该有人来看一眼，
    而不是让一条拍出来的下限继续绿着。
    """

    bound = """
    def _fabricated(connection):
        connection.execute("INSERT INTO policy_decisions(a) VALUES (1)")
        connection.execute("INSERT INTO manual_tasks(b) VALUES (2)")
    """
    tables = _tables_written(bound)
    assert tables & EVIDENCE_TABLES and tables & ACTION_TABLES, (
        "扫描器认不出「两类都写」这个形状 —— 那它报的 0 不算数"
    )

    only_action = 'connection.execute("INSERT INTO manual_tasks(b) VALUES (2)")'
    assert not (_tables_written(only_action) & EVIDENCE_TABLES), (
        "只写动作表的代码被算成了证据表 —— 词表或正则错了"
    )

    names = {path.name for path in _production_files()}
    assert "pipeline.py" in names, (
        "扫描范围里没有 `pipeline.py` —— 而本条这条规则的来源地就在那里"
        "（`_persist_no_send` 绑着「记下这次拒绝」与「这一轮不发消息」那次）。\n"
        "**所以这是「什么都没扫到」而不是「扫到了没问题」**："
        "一个扫描根写错、或那个文件被挪走，`rglob` 会静默返回空，"
        "而下面那条差集断言在空集上恒绿。\n"
        "如果 `pipeline.py` 真的被拆了，本条红得对 —— 扫描面变了，"
        "换一个同样有理由的锚（拆出去之后 `_persist_no_send` 在哪个文件里，锚就在那里），"
        "**不要换成一个拍出来的文件数下限**。"
    )


def test_the_two_table_families_do_not_overlap() -> None:
    """一张表不能既是证据又是动作 —— 那样「绑在一起」就没有定义了。"""

    assert not (EVIDENCE_TABLES & ACTION_TABLES)


def test_no_production_function_writes_both_families() -> None:
    """**本条钉住的是 0。**

    非空时红，而报错要说清**该怎么读它**：
    这不是「这个函数太长」，是「**想要它一半的调用方只能整个不调它**」。
    """

    offenders: list[str] = []
    for path in _production_files():
        for name, body in _functions(path):
            tables = _tables_written(body)
            evidence = sorted(tables & EVIDENCE_TABLES)
            action = sorted(tables & ACTION_TABLES)
            if evidence and action:
                offenders.append(f"{path.name}::{name} 证据{evidence} + 动作{action}")

    assert not offenders, (
        "这些函数同时直接写「证据表」与「动作表」：\n    " + "\n    ".join(offenders) + "\n"
        "证据行说「这件事发生过」，动作行说「因此做了什么」——"
        "**两者的调用方可以是不同的人**，而绑在一个函数里，"
        "想要一半的调用方只能整个不调它。\n"
        "2026-09-13 为这件事付过一次账：`_persist_no_send` 绑着"
        "「记下这次拒绝」与「这一轮不发消息」，"
        "于是说话那条路跳过它时把证据一起跳过了（第一轮 0 行 deny，第二轮也不升级）。\n"
        "常见成因：新写的函数顺手把留痕与动作写在一起；"
        "或某处绕过了 `policy_output.record_policy_decision` 直接 INSERT"
        "（那种情况 `test_policy_output.py` 那条绊线会同时红 —— "
        "**两条都红说明是后者，只有本条红说明是前者**）。"
    )
