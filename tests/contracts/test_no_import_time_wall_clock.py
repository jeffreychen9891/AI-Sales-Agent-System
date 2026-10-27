"""仓里不许有「import 时就固定下来的墙钟」—— 测试与生产都算。

## 它防的那个缺陷，本仓 2026-09-20 真的撞上了

`tests/contracts/test_reconcile_before_unknown.py` 里有过两行：

    DEADLINE = datetime.now(UTC) + timedelta(seconds=30)
    LEASE    = datetime.now(UTC) + timedelta(seconds=25)

语义是「一个还没到的时刻」，而它们是在 **collection** 那一刻算出来的。
单跑那个文件 17 条全绿；**整棵树跑起来时**，从收集到这些用例真正执行之间
超过 25 秒，`LEASE` 已经过期，被测代码于是走「租约将尽」那条分支 ——
**12 条红，而每一条读起来都像是逻辑错了**。

实测两次，同一天：

* 一次全量门禁：`14 failed, 3661 passed`，其中 12 条在那个文件里；
* 复现：在 `pytest_collection_finish` 里插 26 秒再单跑该文件 → **同样那 12 条红**；
  不插 → 17 条全绿。

## 为什么它值得一条守卫，而不是「改完就算」

**这是一颗按运行时长触发的雷。** 它不因为谁改错了代码而爆，
而因为**有人往树上加了足够多的用例**——也就是说，触发它的人
与造成它的人永远不是同一个，而且会在一个与它毫无关系的批次里挨这一炮。
本仓管这类叫「错的方向是安静的」（`CLAUDE.md` §三）：
它能建成、能跑过、只有在别人那里才红。

## 扫哪几棵

`tests` / `scripts` / `packages` / `services` 四棵（2026-09-20 扩，T380 的到期条件）。
**生产代码里同形的更贵**：模块级的 `datetime.now()` 在长跑进程里捕获的是
进程启动那一刻，于是「今天」会停在启动那天。

## 判据走 AST，不走 `grep`

`grep` 答不了「这是一个模块级赋值吗」——函数体里的 `datetime.now()` 是对的，
模块级的才是错的；而折行写法 `grep` 也看不出来。
本仓记过这条（「数错了的守卫看起来在守」）。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 扫哪几棵。**不只是 `tests/`，而这是 T380 自己写死的到期条件**：
#: 初版只扫测试，而同一个形状在生产代码里更贵 —— 一个模块级的
#: `datetime.now()` 在长跑进程里捕获的是**进程启动那一刻**，
#: 于是「今天」这个概念会在一个跑了三天的 worker 里停在三天前。
#:
#: **扩的时候实测是 0 命中**（2026-09-20，`scripts` / `packages` / `services`
#: 三棵全扫）—— 也就是说这条用例从第一天起就没有靠豁免活着。
SCAN_ROOTS = ("tests", "scripts", "packages", "services")

#: 这些调用取的是「现在」。模块级把它们的结果存成常量，就是把
#: 「现在」钉死在 collection 那一刻。
_CLOCK_SUFFIXES = (
    "datetime.now",
    "datetime.utcnow",
    "time.time",
    "time.monotonic",
)


def _dotted(func: ast.expr) -> str | None:
    """`a.b.c` → `"a.b.c"`。不是纯属性链就返回 `None`。

    **要整条链，不是最后两段** —— 而这一格是一次反向探针换来的：
    判据原本只认 `Name.attr` 两段，于是 `import datetime as _dt` 之后的
    `_dt.datetime.now(...)` **它看不见**，塞进 `scripts/` 的那处违规照样绿。
    **一条认不出别名导入的守卫，在别人写别名的那天就是空的。**
    """

    parts: list[str] = []
    node: ast.expr = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _calls_the_clock(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        dotted = _dotted(child.func)
        if dotted is None:
            continue
        # **按结尾匹配**：`datetime.now` / `_dt.datetime.now` /
        # `stdlib.datetime.datetime.now` 都算同一件事。
        if any(dotted == suffix or dotted.endswith("." + suffix) for suffix in _CLOCK_SUFFIXES):
            return True
        # `from time import time` 之后的裸 `time()`。**这一支不是凑数**：
        # 只看属性链会漏掉 import 进来的裸函数名。
        if dotted in {"time", "monotonic", "utcnow"}:
            return True
    return False


def _offenders() -> list[str]:
    found: list[str] = []
    candidates = [
        path
        for root in SCAN_ROOTS
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
    ]
    for path in candidates:
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # **只看模块级的那一层**，不 walk 进函数/类 —— 函数体里现取是对的。
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None or not _calls_the_clock(value):
                continue
            targets = (
                [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
            )
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            found.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                f"{', '.join(names) or '<解构赋值>'}"
            )
    return found


def test_no_module_freezes_the_clock_at_import() -> None:
    offenders = _offenders()
    assert not offenders, (
        "这些模块级常量在 import（collection）那一刻就把「现在」钉死了，"
        "整棵树跑得够久它们就会过期，而红的会是一批与之无关的用例：\n  "
        + "\n  ".join(offenders)
        + "\n改法：写成一个调用时现取的函数（样板见 "
        "tests/contracts/test_reconcile_before_unknown.py 的 `_deadline()`）。"
    )


def test_the_detector_would_catch_the_shape_it_is_named_for() -> None:
    """**守卫自己不能是空的。**

    上面那条今天绿着，而一条绿着的守卫永远不显示它的报错 ——
    它可能是因为「没有违规」绿，也可能是因为**它什么都没检查**。
    这里喂它一份已知有违规的源码，确认它认得出来。
    """

    offending = ast.parse(
        "from datetime import UTC, datetime, timedelta\n"
        "DEADLINE = datetime.now(UTC) + timedelta(seconds=30)\n"
    )
    module_level_assigns = [n for n in offending.body if isinstance(n, ast.Assign)]
    assert module_level_assigns, "夹具本身就没有模块级赋值，那这条用例什么都没验"
    assert _calls_the_clock(module_level_assigns[0].value)

    clean = ast.parse(
        "from datetime import UTC, datetime, timedelta\n"
        "def _deadline():\n"
        "    return datetime.now(UTC) + timedelta(seconds=30)\n"
    )
    assert not [n for n in clean.body if isinstance(n, ast.Assign)], (
        "改好之后模块级不该再有那个赋值 —— 若这里有，说明上面那条的判据选错了层"
    )
