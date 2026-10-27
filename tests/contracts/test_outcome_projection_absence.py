"""`outcome_ref` / `annotation_refs` 今天**没有写者** —— 而这条断言自带重核（批 3）。

## 它守的两件事

1. **`OUTCOME_PROJECTION_AVAILABLE = False` 说的是不是真话**
   —— 一旦生产代码里出现第二处写 `outcome_ref` 的地方，这里红，
   并指着 `api/outcome_display.py` 说「改我」；
2. **`annotation_refs` 仍然零读零写** —— 有人给它加写点时这里红，
   而红的含义**不是「不许写」**，是：
   **它的另一头在 §18.2（本轮范围外），别只落一半。**

`CLAUDE.md` 第二条要的就是这个形状：凡断言「某物今天不存在」，
配一条会在它失效时变红的守卫。已有同源实例
`test_the_claim_that_the_ledger_does_not_exist_is_still_true`。

## 为什么走 AST 而不是 `grep`

本仓两次栽在这上面（`AGENTS.md` / 上一份交接 §五.1）：

* **`grep` 答不了「有几个符号」** —— 名字被折行断开时它看不出那是同一个；
* **注释里提到这个名字会被算成一处写点** —— 而本文件自己就有一堆这样的注释，
  用 `grep` 写这条守卫，它会因为自己而红。

所以这里问的是 **AST**：`{"outcome_ref": …}` 这样的字典字面量键，
以及 `x["outcome_ref"] = …` 这样的下标赋值。

## 判据是**「每一处都被分过类」**，不是「零写点」

第一版写的是「零写点」，**而它当场被自己顶红了**：
`outcome_display` 回显那个字段时也构造 `{"outcome_ref": …}` ——
**显示层的回显与「往聚合里写一个值」在 AST 上长得一模一样，而它们是两件事。**
禁止式的判据只能靠「再加一个例外」活下去，分类式的判据把那个区别写在表里。

而且是**等值集合**，不是计数：写成「不超过 N 处」会让「那一处搬了家」悄悄通过，
而搬家正是这类字段长出第二个落点的常见方式。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 只扫生产代码。用例与脚本本来就会提到这些名字。
PRODUCTION = (ROOT / "services", ROOT / "packages")

FIELDS = ("outcome_ref", "annotation_refs")

#: 今天**提到**这两个字段的每一处，以及它为什么在那里。
#:
#: **判据是「被分过类」，不是「禁止出现」** —— 与批 2 那条守卫同一个形状。
#: 第一版写成「零写点」，结果 `outcome_display` 自己回显那个字段就把它顶红了：
#: **显示层构造 `{"outcome_ref": …}` 与「往聚合里写一个值」在 AST 上长得一样，
#: 而它们是两件事。** 分类比禁止诚实。
KNOWN_SITES: dict[tuple[str, str, str], str] = {
    (
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "outcome_ref",
        "empty",
    ): "声明，不是写：把字段摆进聚合文档，好让契约校验有东西可校",
    (
        "services/worker/src/fastagent_v2/worker/pipeline.py",
        "annotation_refs",
        "empty",
    ): "同上",
    (
        "services/api/src/fastagent_v2/api/outcome_display.py",
        "outcome_ref",
        "written",
    ): "**回显，不是写**：详情页把聚合里那个值原样投影出去，"
    "旁边加 `status` 说清它是哪一种 null（用户 2026-09-13 对承诺那一格的裁定）",
}


def _is_empty(node: ast.AST) -> bool:
    """这个值是不是「空」—— `None` 或 `[]`。"""

    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return isinstance(node, ast.List) and not node.elts


def _sites() -> set[tuple[str, str, str]]:
    """全仓生产代码里，把这两个字段**写出去**的地方。"""

    found: set[tuple[str, str, str]] = set()
    for root in PRODUCTION:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover - 生产代码不该有
                continue
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=False):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in FIELDS
                        ):
                            kind = "empty" if _is_empty(value) else "written"
                            found.add((relative, str(key.value), kind))
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value in FIELDS
                        ):
                            kind = "empty" if _is_empty(node.value) else "written"
                            found.add((relative, str(target.slice.value), kind))
    return found


def test_every_mention_of_these_fields_has_been_classified() -> None:
    """提到这两个字段的地方**等于**已分类的那几处 —— 多一处就红。

    红的时候要答的是：**另一头做了吗？**
    `outcome_ref` 的另一头是结果记录本身，`annotation_refs` 的是人工标注（§18.2）。
    只落这一半，聚合里会多出一个**永远没人读的真值** ——
    而那正是 `AGENTS.md` 判据 2 说的形状。
    """

    sites = _sites()
    extra = sorted(sites - set(KNOWN_SITES))
    missing = sorted(set(KNOWN_SITES) - sites)

    assert not extra, (
        f"这两个字段多了没分过类的落点：{extra}。"
        "**先答「另一头做了吗」** —— 结果记录 / 人工标注都在 §18.2；"
        "只写不读的话，聚合里会多出一个永远没人读的真值。"
        "确实要写的话，同批改 `api/outcome_display.OUTCOME_PROJECTION_AVAILABLE`"
    )
    assert not missing, (
        f"已分类的这几处不见了：{missing}。"
        "字段从那里消失，而这张表还挂着它的理由 —— 删掉，否则下一个人会以为这里还有账"
    )


def test_the_projection_says_which_kind_of_null_it_is() -> None:
    """对外那一格必须说清 `null` 是哪一种 —— 用户 2026-09-13 对承诺那一格的裁定。

    **不断言 `note` 的字面**（那是措辞，会改），断言的是
    **「未启用」这个状态真的到得了客户端**。
    """

    from fastagent_v2.api.outcome_display import (
        OUTCOME_PROJECTION_AVAILABLE,
        outcome_display,
    )

    assert OUTCOME_PROJECTION_AVAILABLE is False, (
        "有人把它翻成 True 了 —— 那要么是结果投影真的落地了（那么上面那条也该红），"
        "要么是这一格开始说假话"
    )

    shown = outcome_display(None)
    assert shown["outcome_ref"] is None, "字段本身要照显 —— 图纸那半句不改"
    assert shown["status"] == "not_enabled", (
        "客户端读到一个光秃秃的 null，分不出「这一轮没有结果」与「这个能力没实现」"
    )
    assert str(shown["note"]).strip(), "状态要带一句人话，否则读的人还要来仓里查"
