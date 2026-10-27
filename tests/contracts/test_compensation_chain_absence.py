"""自动补偿那一段今天**零实现** —— 而这条断言自带重核（批 4，B1-6 + B1-7）。

## 先更正一句流传的话

`docs/reviews/core-vs-blueprint-survey-2026-09-13.md` 的 B1-7 写着
**「补偿链没有任何一段」**。**现读不是这样**：图纸自己对「没有 inverse」有明文
（`conversation-engine.md:504`，`runtime/control_plane.py` 的注释逐字引了它）——

> no reliable inverse -> stay UNKNOWN/manual, do not fake a successful rollback

**而那条兜底路径是实现了的**：`worker/channel.py::reconcile_by_key` 刻意区分
「问过了、它不知道」与「我们根本没问」，并写明**只有前者允许把意图落成 `UNKNOWN`**；
`UNKNOWN` 也确实在 `_LIVE_INTENT_STATUSES` 里，含义是「还没完，要对账」。

**所以准确的说法是：兜底那一段在，缺的是「有 inverse 就自动补偿」那一段。**
这个区别不是措辞 —— 它决定这一批该不该建东西
（**不该**：兜底在，没有「今天会出错」的敞口），以及绊线该钉在哪一头。

## 缺的那一段今天长什么样

* **`IntentRecord.inverse`** —— `pipeline.py` 初始化 `None`、契约
  `intent-record.schema.json:25`、DB 列 `001_m0.sql:192`；**零写点**；
* **`COMPENSATING`** —— `pipeline._LIVE_INTENT_STATUSES` 与 DB CHECK；
  **不可达，而有意留着**（理由写在那张表旁边）；
* **`COMPENSATED`** —— **只在 DB CHECK 里**，生产代码零出现。

## 判据仍是「每一处都被分过类」，不是「零写点」

批 2 与批 3 两次都被实跑逼到同一个形状：**禁止式的判据只能靠不断加例外活下去**，
而「显示层回显」「成员表里预留一个名字」与「真的写一个值」
在 AST 上长得一样、在语义上是三件事。分类式的把那个区别写进表里。

**红的含义不是「不许写」，是**：
`inverse` 一旦有了写者，`APPLIED -> COMPENSATING -> COMPENSATED/FAILED`
那几条转换必须同批落地 —— 否则库里会出现一个**有回滚方案却永远回滚不了**的意图，
而那比没有回滚方案更糟：它看起来是可恢复的。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PRODUCTION = (ROOT / "services", ROOT / "packages")

#: 自动补偿那一段的三个名字。
NAMES = ("inverse", "COMPENSATING", "COMPENSATED")

#: 今天提到这三个名字的每一处，以及它为什么在那里。**等值集合。**
KNOWN_SITES: dict[tuple[str, str, str], str] = {
    (
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "inverse",
        "empty",
    ): "声明，不是写：把字段摆进 `intent-record` 文档，好让契约校验有东西可校",
    (
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "COMPENSATING",
        "listed",
    ): "**成员表里的一个名字，不是一条转换**：`_LIVE_INTENT_STATUSES` 有意留着它，"
    "理由写在那张表旁边 —— 删了之后接补偿链的人要记得回来改这里",
}


def _is_empty(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.List) and not node.elts


def _sites() -> set[tuple[str, str, str]]:
    """生产代码里提到这三个名字的地方。

    **三种落点分开记**，因为它们是三件事：

    * `empty` —— 字典字面量里那个键的值是 `None` / `[]`（声明）；
    * `written` —— 同样是那个键，而值不空（**真的写了一个值**）；
    * `listed` —— 一个裸的字符串常量（成员表、词表里的一个名字）。
    """

    found: set[tuple[str, str, str]] = set()
    for root in PRODUCTION:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            relative = path.relative_to(ROOT).as_posix()
            written: set[int] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=False):
                        if isinstance(key, ast.Constant) and key.value in NAMES:
                            found.add(
                                (
                                    relative,
                                    str(key.value),
                                    "empty" if _is_empty(value) else "written",
                                )
                            )
                            written.add(id(key))
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value in NAMES
                        ):
                            found.add(
                                (
                                    relative,
                                    str(target.slice.value),
                                    "empty" if _is_empty(node.value) else "written",
                                )
                            )
            # 裸字符串常量 —— 成员表 / 词表里的名字。
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in NAMES
                    and id(node) not in written
                ):
                    found.add((relative, node.value, "listed"))
    return found


def test_every_mention_of_the_compensation_chain_has_been_classified() -> None:
    """三个名字的落点**等于**已分类的那几处 —— 多一处就红。

    红的时候要答的是：**另外两段落了吗？**
    写 `inverse` 而不落转换，库里会出现一个
    **有回滚方案却永远回滚不了**的意图 —— 它看起来是可恢复的，而它不是。
    """

    sites = _sites()
    extra = sorted(sites - set(KNOWN_SITES))
    missing = sorted(set(KNOWN_SITES) - sites)

    assert not extra, (
        f"自动补偿那一段多了没分过类的落点：{extra}。"
        "**`inverse` 的写者、`COMPENSATING` 的转换、`COMPENSATED` 的终结 —— 三件要一起落**；"
        "只落第一件，库里就会有一个有回滚方案却回滚不了的意图"
    )
    assert not missing, (
        f"已分类的这几处不见了：{missing}。"
        "这张表还挂着它们的理由 —— 删掉，否则下一个人会以为这里还有账"
    )


def test_the_fallback_half_really_is_implemented() -> None:
    """**另一头**：兜底那一段不能跟着一起消失。

    只断言「自动补偿没实现」是不够的 —— 那样把 `UNKNOWN` 那条路**删掉**也会全绿，
    而删掉它才是真的危险：图纸 `:504` 要的正是「没有可靠 inverse 时停在
    UNKNOWN/人工，不要假装回滚成功」。**两头一起断言，这一条才有意义。**
    """

    from fastagent_v2.worker.pipeline import _LIVE_INTENT_STATUSES

    assert "UNKNOWN" in _LIVE_INTENT_STATUSES, (
        "`UNKNOWN` 从「活着的状态」里消失了 —— 那意味着一个没对过账的意图会被当成终态，"
        "而图纸 `:504` 明禁把未确认的结果收敛成已确认的"
    )
    assert "COMPENSATING" in _LIVE_INTENT_STATUSES, (
        "`COMPENSATING` 被顺手删了。它今天不可达是事实，**而留着是有意的** —— "
        "见那张表旁边的注释：删了之后，接补偿链的那一批要记得回来改这里"
    )

    reconcile = (
        ROOT / "services/worker/src/fastagent_v2/worker/channel.py"
    ).read_text(encoding="utf-8")
    assert "reconcile_by_key" in reconcile, (
        "对账那条路没了 —— 「问过了它不知道」与「我们根本没问」的区别是 `UNKNOWN` "
        "这条兜底成立的前提，没有它，兜底就变成了猜"
    )
