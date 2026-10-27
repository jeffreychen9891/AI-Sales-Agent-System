"""`ConfidenceAssessment` 今天是**装饰性的** —— 而这条断言自带重核（批 5，B1-11）。

## 两句更正

**一、不是三个字段，是四个。** survey（`core-vs-blueprint-survey-2026-09-13.md`
B1-11）写的是「`calibrated_score / assessment_version / factor_codes` … 真实读点 0」。
AST 现读：**`raw_score` 同样零读点** —— 全仓 9 处读 `confidence` 的地方
**全是槽位级那个标量**（`slot_updates[].confidence` / `slot_evidence[].confidence`），
与这个对象无关。**整个结构四个字段，一个都没人读。**

**二、这三批里 survey 的行连续三次比实际窄**（B1-7「没有任何一段」/
B1-4 两个形状不同的字段并成一行 / 本条「三个字段」）。
**那份 survey 是 2026-09-13 的快照，它的每一行开工前都要重核，而不是当判据用。**
`CLAUDE.md` 第四条说的是「概念要先确认有实现」，**这里多一层**：
有实现之后还要问**「实现的范围是不是它说的那个范围」**。

## 它今天不只是「没人读」

**那三个字段连值都是假的**：

* `calibrated_score` —— 三个构造点**全部写死 `None`**（校准器不存在）；
* `assessment_version` —— 一个字面量（`self.version` / `"safe-fallback-v1"`）；
* `factor_codes` —— 一个字面量表（`["deterministic_output"]` / `["fallback_applied"]`）。

**「有值没人读」与「连值都是假的」不是一回事**：后者会让读的人以为那里有信息。
`raw_score` 不同 —— 它是模型真的产出的那个数，只是今天没人用。

## 为什么不拆（用户 2026-09-15 裁定 (乙)）

量过它的代价：`_PLAN_SYSTEM` 里 199 字节 + `_GENERATE_SYSTEM` 里 260 字节 +
`confidence.schema.json` 964 字节**按 `$ref` 展开进 plan 与 generate 两份响应 schema**，
**合计约 2,387 字节/回合**，而没有任何东西读它。

**拆掉是有收益的，而代价压过了它**：那是 RFC:500-504 / 523-527 明文要求的字段，
摘掉等于单方面把图纸那一条划掉；而且动响应契约要重量三个角色的开销常数、
并回答存量响应怎么办。**所以留着 + 绊线**，而上面那个数留在这里，
是**下一次重裁的输入**，不是本批的处置。

## 这条守卫钉两件，合起来才说得清「它是装饰性的」

1. **没人读** —— 有人开始读时红；
2. **写进去的是常量** —— 有人真建出校准器时红，
   而红的含义是「这个字段开始带信息了，回来更新登记与上面那段话」。

**只钉第 1 条不够**：校准器建起来之后那三个字段仍然可以没人读，
而那时这段 docstring 说的「连值都是假的」就成了假话，**却不会有任何东西红**。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = (ROOT / "services", ROOT / "packages")
INFERENCE = ROOT / "services/worker/src/fastagent_v2/worker/inference.py"

#: `ConfidenceAssessment` 的四个字段（`confidence.schema.json`）。
FIELDS = ("raw_score", "calibrated_score", "assessment_version", "factor_codes")


def _reads() -> set[tuple[str, int, str]]:
    """生产代码里**取值**读这四个字段的地方 —— 今天应当一处都没有。

    **只算 `Load` 上下文的下标与 `.get(...)`** —— 字典字面量里的那个键是
    「写」，不是「读」，而它们在源码里长得像。
    """

    found: set[tuple[str, int, str]] = set()
    for root in PRODUCTION:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Constant)
                    and node.slice.value in FIELDS
                    and isinstance(node.ctx, ast.Load)
                ):
                    found.add((relative, node.lineno, str(node.slice.value)))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in FIELDS
                ):
                    found.add((relative, node.lineno, str(node.args[0].value)))
    return found


def _confidence_literals() -> list[dict[str, ast.AST]]:
    """`inference.py` 里那几处 `"confidence": {...}` 字面量，各自的键 -> 值节点。"""

    tree = ast.parse(INFERENCE.read_text(encoding="utf-8"))
    blocks: list[dict[str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if (
                isinstance(key, ast.Constant)
                and key.value == "confidence"
                and isinstance(value, ast.Dict)
            ):
                block: dict[str, ast.AST] = {}
                for inner_key, inner_value in zip(
                    value.keys, value.values, strict=False
                ):
                    if isinstance(inner_key, ast.Constant):
                        block[str(inner_key.value)] = inner_value
                blocks.append(block)
    return blocks


def test_nothing_reads_the_confidence_assessment() -> None:
    """四个字段**一处都没人读** —— 有人开始读时红。

    红的含义**不是「不许读」**，是：**读它之前先确认它带信息** ——
    今天 `calibrated_score` 恒 `None`、`factor_codes` 是个字面量表，
    照这些值去做判断，判的是一个常数。
    """

    reads = sorted(_reads())
    assert not reads, (
        f"有人开始读 `ConfidenceAssessment` 了：{reads}。"
        "**先确认它带信息**：`calibrated_score` 今天恒 `None`（校准器不存在）、"
        "`factor_codes` 是字面量表 —— 照它们判断等于照一个常数判断。"
        "确实要读的话，同批更新本文件 docstring 与登记（B1-11）"
    )


def test_three_of_the_four_are_still_literals() -> None:
    """**另一头**：那三个字段写进去的仍然是常量。

    **只钉「没人读」是不够的** —— 校准器建起来之后那三个字段仍然可以没人读，
    而那时上面 docstring 里「连值都是假的」这句话就成了假话，
    **却不会有任何东西红**。这一条就是那句话的守卫。
    """

    blocks = _confidence_literals()
    assert blocks, (
        "`inference.py` 里找不到 `\"confidence\": {...}` 字面量了 —— "
        "要么它搬了家（那么这条守卫在守一个空位），要么结构变了"
    )

    for block in blocks:
        calibrated = block.get("calibrated_score")
        assert isinstance(calibrated, ast.Constant) and calibrated.value is None, (
            "`calibrated_score` 不再是写死的 `None` —— **校准器出现了吗？**"
            "出现了就更新本文件 docstring 与 B1-11 的登记："
            "这个字段从此带信息，那时「没人读它」才真的成为一笔欠账"
        )
        version = block.get("assessment_version")
        assert isinstance(version, (ast.Constant, ast.Attribute)), (
            "`assessment_version` 开始由什么东西算出来了 —— 同上，更新登记"
        )
        codes = block.get("factor_codes")
        assert isinstance(codes, ast.List) and all(
            isinstance(item, ast.Constant) for item in codes.elts
        ), (
            "`factor_codes` 不再是字面量表 —— 有东西开始真的评估因子了，更新登记"
        )
