"""图纸里「下次照这个办」的条款，点名的坐标必须还在。

**为什么有这个脚本**：同一形状的东西，图纸记了三遍，实现犯了三遍。
最清楚的一次是 ADR-0113 §五——它自己写着「规则被提成纯函数，**这是本条最该被
继承的部分**」，并留了一句「这一次是我们在读过那条 ADR 之后又造了一遍」；
而 2026-09-11 写审计文件落盘权限时，**又手写了一遍**，第三次。

**问题不是没写下来，是写下来的东西没有机制被带到下一次。** 仓里已经有一条纪律
（「开工任何一格之前，先拿它的判据去代码里搜一遍」，排期 §3.1），
这个脚本是它的另一半：**让图纸点名的那个可复用坐标，在它消失的那一刻变红**，
而不是等下一个人重造一遍之后才被发现。

它守的是**坐标还在**，守不住「有人搜到了却还是重造」——后者要靠
`AGENTS.md`「继承项」那一节的开工纪律。两半都要，缺一半都回到今天这个状态。

## 标记怎么写

图纸（`docs/**/*.md`）里，一段「下次照这个办」的条款加一行：

    **【继承项】** 一句话说它是什么 —— 坐标：`some.module.path` 或 `path/to/file.py`

坐标可以有多个，各自用反引号括起来。点号形式按 Python 符号解析
（模块 / 模块里的名字），带斜杠或点扩展名的按仓内路径解析。

**标记必须起行**（允许 `>`、`-`、`#` 这类 Markdown 前缀）。这一条不是洁癖，
是第一次拿真内容跑出来的：一份**谈论这个约定**的文档——分类清单里那句
「判进去的加 `**【继承项】**` 标记」，以及一张表里**被截断到 150 字符**的
ADR-0113 摘录——**两条都被算成了声明**，一条说「有标记没坐标」，
一条说「坐标后面没有反引号」。**两条都是假的。**

**引用一个标记和声明一个标记，必须在形状上分得开**，否则这个门禁会因为
「有人写文档提到了它」而变红，而一个会因为无关原因变红的门禁，
下一个人学会的是绕过它。所以：**起行的是声明，行中的是提及**，
提及**计数并打印**（不是静默跳过——静默会让一条写在表格里的真声明凭空消失）。

用法：

    python scripts/check_inheritance_anchors.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 源码根，与 `pyproject.toml` 的 `pythonpath` 同源。写在这里而不是导入它，
#: 是因为这个脚本要能在只检出 `docs/` 与 `scripts/` 的地方跑。
SOURCE_ROOTS = (
    "packages/runtime/src",
    "packages/channels/src",
    "services/api/src",
    "services/worker/src",
    "services/ingress/src",
)

MARKER = "【继承项】"

#: **声明**：标记起行，允许 Markdown 的块引用 / 列表 / 标题前缀。
_ANCHOR_LINE = re.compile(rf"^\s*(?:(?:[>*+-]|#{{1,6}})\s*)*\*\*{re.escape(MARKER)}\*\*")
_COORDINATE_LINE = re.compile(r"坐标：(.+)$")
_BACKTICKED = re.compile(r"`([^`]+)`")


@dataclass(frozen=True, slots=True)
class Anchor:
    """One 【继承项】 declaration: where it is, and what it points at."""

    where: str
    coordinates: tuple[str, ...]


def parse_anchors(text: str, *, where: str = "<text>") -> tuple[list[Anchor], list[str], int]:
    """Split a document into declarations, malformed declarations, and mentions.

    **A pure function on purpose**, which is this batch inheriting the very
    thing it is built to protect (ADR-0113 §五: 「规则被提成纯函数，这是本条
    最该被继承的部分」). The rule「一行是声明还是提及」was inline in `main()`
    in the first version, reachable only by putting a file on disk under
    `docs/` -- so the two false positives below could not be written as cases
    at all. Split out so they can.

    Returns `(anchors, malformed, mentions)`. `mentions` counts lines that name
    the marker without declaring one; they are reported rather than dropped,
    because a silent skip would make a declaration written inside a table
    disappear without anybody noticing.
    """

    anchors: list[Anchor] = []
    malformed: list[str] = []
    mentions = 0
    fenced = False

    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if MARKER not in line:
            continue
        # **代码块里的是示例，不是声明。**
        #
        # 这条是合并后门禁红出来的（2026-09-12）：本批自己的自审 §2.1 用一段
        # 缩进代码块演示标记长什么样，而 `_ANCHOR_LINE` 的 `^\s*` 把它当成了
        # 一条真的继承项——于是示例里那两个占位坐标被拿去仓里找，当然找不到。
        #
        # **分支门禁当时是绿的，而绿得没有意义**：它跑在 `9aa21d1`，
        # 那棵树里还没有那份自审。**一份文档在它自己描述的检查器之后才写下来，
        # 于是检查器从没见过它。**
        #
        # 两种代码块都跳过，用的是 Markdown 自己的定义：
        # 围栏（```）与缩进四格。**块引用（`>`）不跳过** —— 那是散文在引用，
        # 而一条写在引文里的继承项仍然是一条继承项。
        #
        # **实测（改这一版时量的）**：全仓被当成声明的行只有两处，
        # 一处缩进 0（真的），一处缩进 4（就是那个示例）。
        # 所以这条规则今天不会误伤任何真声明；**而它可能误伤缩进四格以上的
        # 嵌套列表里的声明**，所以跳过的行仍然计进 `mentions`、仍然被报出来,
        # 不是静默丢掉。
        if fenced or line.startswith("    ") or line.startswith("	"):
            mentions += 1
            continue
        if not _ANCHOR_LINE.match(line):
            mentions += 1
            continue
        position = f"{where}:{number}"
        match = _COORDINATE_LINE.search(line)
        if match is None:
            malformed.append(f"{position}: 有 {MARKER} 但没有「坐标：」")
            continue
        coordinates = _BACKTICKED.findall(match.group(1))
        if not coordinates:
            malformed.append(f"{position}: 「坐标：」后面没有反引号括起来的坐标")
            continue
        anchors.append(Anchor(where=position, coordinates=tuple(coordinates)))

    return anchors, malformed, mentions


def _documents(root: Path = ROOT) -> list[Path]:
    return sorted(p for p in (root / "docs").rglob("*.md") if p.is_file())


def _resolves_as_path(coordinate: str, root: Path = ROOT) -> bool:
    return (root / coordinate).exists()


def _resolves_as_symbol(coordinate: str, root: Path = ROOT) -> bool:
    """Whether a dotted path names a module, or a name inside one.

    Walks from the longest module prefix inwards rather than importing: a module
    that needs a database connection to import is still a coordinate, and this
    check must not need the deployment to run.
    """

    for source_root in SOURCE_ROOTS:
        base = root / source_root
        if not base.is_dir():
            continue
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))

    parts = coordinate.split(".")
    for cut in range(len(parts), 0, -1):
        module_name = ".".join(parts[:cut])
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, ModuleNotFoundError):
            spec = None
        if spec is None or spec.origin in (None, "built-in"):
            continue
        remainder = parts[cut:]
        if not remainder:
            return True
        source = Path(spec.origin).read_text(encoding="utf-8")
        # A name check by text, not by import: importing a worker module pulls in
        # settings that a docs-only checkout does not have.
        return any(
            re.search(rf"^\s*(?:def|class)\s+{re.escape(name)}\b", source, re.M)
            or re.search(rf"^\s*{re.escape(name)}\s*[:=]", source, re.M)
            for name in remainder[-1:]
        )
    return False


def _resolves(coordinate: str, root: Path = ROOT) -> bool:
    if "/" in coordinate or coordinate.endswith((".py", ".sql", ".json", ".md", ".yml")):
        return _resolves_as_path(coordinate, root)
    return _resolves_as_symbol(coordinate, root)


def main(root: Path = ROOT) -> int:
    anchors: list[Anchor] = []
    malformed: list[str] = []
    mentions = 0

    for document in _documents(root):
        found, broken, mentioned = parse_anchors(
            document.read_text(encoding="utf-8"),
            where=document.relative_to(root).as_posix(),
        )
        anchors.extend(found)
        malformed.extend(broken)
        mentions += mentioned

    missing: list[str] = []
    for anchor in anchors:
        for coordinate in anchor.coordinates:
            if not _resolves(coordinate, root):
                missing.append(f"{anchor.where}: 坐标 `{coordinate}` 今天在仓里找不到")

    total = sum(len(anchor.coordinates) for anchor in anchors)
    print(f"inheritance anchors: {len(anchors)} 条，{total} 个坐标")
    if mentions:
        # **打印而不是静默**：提及本身是正常的（文档会谈论这个约定），
        # 但一条写在表格里、因此不起行的**真声明**也会落进这个计数，
        # 而那种情况只有把数字摆出来才看得见。
        print(f"另有 {mentions} 行提到这个标记但不起行，按「在谈论这个约定」处理")
    if malformed:
        print("\n标记写坏了：")
        for item in malformed:
            print(f"  {item}")
    if missing:
        print("\n坐标不在了：")
        for item in missing:
            print(f"  {item}")
        print(
            "\n一条【继承项】指着一个不存在的坐标，比没有标记更糟——"
            "下一个人会照着它去找，找不到，然后自己造一遍。"
            "\n要么把坐标改到它今天所在的位置，要么把这条继承项连同它的理由一起删掉。"
        )
    if malformed or missing:
        return 1
    print("每一条继承项点名的坐标都还在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
