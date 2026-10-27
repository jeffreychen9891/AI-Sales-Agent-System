"""裁定二（ADR-0137 §二）「只统一输出，不改结构」的**执行者**。

**这一份守的是几件「有意不做」的事，而有意不做的事最容易被顺手做掉。**
`policy_output.py` 那份用例守的是「统一输出」那一半（唯一写入点、三张词表对得上）；
本文件守的是**另一半** —— 裁定二明写不做的三件：

1. **不新增发出点** —— `post_understanding` / `pre_generation` 零写者（B1-8 / B2-4）
   → `test_only_the_four_wired_hook_points_have_writers`
2. **不发 `require_evidence`** —— 四个 obligation 里唯一零发出的那个（B1-10）
   → `test_require_evidence_is_never_emitted`
   与 `test_nothing_reads_the_obligations_column`
3. **不把内联闸收回成 hook 对象** —— 十二个固定 hook 名（A5 / D1）
   → `test_none_of_the_twelve_fixed_hooks_exists_as_a_symbol`

## 它们全都是「零」，所以扫描失效会静悄悄通过

**这是本文件最要紧的一条设计**：一条「某某恒为 0」的断言，在扫描器失效
（路径写错、glob 没匹配上、AST 解析全抛）时**回的也是 0**，于是它通过。
`AGENTS.md`「计数类断言写等值，不要写上界」与「解析器真的读到了源」记的就是这件事，
而这个仓已经踩过两次（裁定二那条绊线第一版用 `git grep` 回了 0 个文件；
`test_reference_projection_wiring.py` 初版用写死七个名字的正则去找那七个名字）。

**所以每一条零断言旁边都钉着一个非零**：

* 扫到的文件集合里**必须有三个点名的模块**（三个包各一个）；
* `PolicyOutput(` 构造点数 == 7，而**六个时点的发出点数之和必须等于它** ——
  两个 0 与四个非零加起来要正好是全部，少一个都说明有一处没被数到；
* `require_evidence` 的零发出旁边，是另外三个 obligation 的非零发出；
* 十二个 hook 名的零定义旁边，是 `ProactivityPolicyError` 与 `CostGovernor`
  两个真实符号 —— **这一格是写本文件时才变成「十二个」的**，
  见 `test_none_of_the_twelve_fixed_hooks_exists_as_a_symbol` 的 docstring：
  「只有 `ProactivityPolicy` 按名字存在」那句话被抄了四遍，而它数错了。

## 为什么数 `PolicyOutput(...)` 的 `hook=`，而不是 `git grep 'hook="..."'`

字符串扫描会把**注释、docstring、契约字典**一起数进来 ——
`pipeline.py` 里 `policy_contract["input"]["hook"]` 那几处是**喂给 `validate_contract`
的合同字典**，不是发出点；而 `policy_output.py` 的 `HOOK_POINTS` 元组里
六个名字全在。三者在 `grep` 眼里长得一样。

本文件走 AST，只认 `PolicyOutput(hook=<字面量>)`。
**而「字面量」这个前提本身要断言** —— 哪天有人写 `PolicyOutput(hook=some_var)`，
这里数不出来，**那时该红的是「数不出来了」，不是静静少算一个**。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastagent_v2.runtime.authz import ObligationKind
from fastagent_v2.worker.policy_output import HOOK_POINTS, OBLIGATIONS

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_TREES = ("services", "packages")

#: 裁定二明写不补发出点的两个时点（ADR-0137 §二 第 1 条）。
#: RFC §3.4 与 §8 各有一条修订标注写着同一件事。
UNWIRED_HOOKS = ("pre_generation",)

#: RFC §3.4「固定 hooks」那一行的十二个名字。
#: **十二个今天一个都不是符号**（实测见那条用例的 docstring）——
#: 功能以内联闸的形态存在，而裁定二明写不把它们收回成 hook 对象。
FIXED_HOOK_NAMES = (
    "UnifiedAuthorization",
    "ProfileCapability",
    "PricingPolicy",
    "CommitmentPolicy",
    "CompliancePolicy",
    "HumanControlPolicy",
    "ChannelCapabilityPolicy",
    "DeliveryPolicy",
    "ProactivityPolicy",
    "ConfidenceGatePolicy",
    "InboundManipulationPolicy",
    "CostBudgetPolicy",
)

#: `SELECT <列> FROM policy_decisions` —— **列名的字符集是收窄的**（只许
#: 字母/数字/下划线/逗号/空白/括号/`*`），而这不是洁癖：用 `.+?` 配 `re.S` 时，
#: 它会从上方某处无关的 `SELECT` 一路吃到这里，把整段注释当成「列清单」——
#: 实测第一版就是这么把一段 docstring 读成了「在读 obligations 列」。
#: 注释里必然有 `#` / `.` / `-`，收窄字符集就把它们挡在外面。
_SELECT_FROM_POLICY_DECISIONS = re.compile(
    r"SELECT\s+(?P<columns>[A-Za-z0-9_,\s()*]+?)\s+FROM\s+policy_decisions",
    re.IGNORECASE,
)

#: `PolicyOutput(` 的构造点数。**这个数只写在这里一处**，两条断言各自引用它 ——
#: 一条守「扫描器读到东西了吗」，一条守「逐时点数出来的和还对得上吗」。
#:
#: **它必须是一个常量，不能写成 `len(_policy_output_calls())`**：后者会让那条
#: 求和断言变成恒真式（循环每转一圈恰好 +1，两边都是「跑了几次」）。
#: 详见那条断言上方的注释。
#: **2026-09-16（批 11）7 -> 10**：`commitment_ledger.py` 里三处 ——
#: 收下一条候选（`allow`）、拒掉一批（`deny`）、投影被截（`modify`）。
#: 三处都在 `post_understanding`，而那个时点在此之前**零发出** ——
#: 所以它同时从 `UNWIRED_HOOKS` 里挑了出去。
#:
#: **2026-09-18（批 D）10 -> 13**：`commitment_ledger.write_ai_admitted` 里三处 ——
#: 台账 `party=ai` 那一半的写入方（收下 / 拒掉 / 投影被截），形状与客户侧逐字相同。
#: **三处都在 `post_plan`**：AI 的义务由**打法**决定，而打法要到计划步才定下来。
#:
#: ⚠️ 按这条用例自己的指示核过：**裁定二明写不补的那两个时点一个没碰** ——
#: `post_understanding` 仍是 3、`pre_generation` 仍是 0。
#:
#: ⚠️ 也核过**为什么不是给 `write_admitted` 加一个 `hook` 参数**：
#: 那样 `PolicyOutput(hook=hook)` 在 AST 里读不出值，
#: `post_understanding` 会被少算三个 —— 而少算的方向正好让「恒为 0」通过。
#: **那条守卫挡对了**，所以两侧各写各的字面量，共用的只有 `_insert_rows`。
EXPECTED_CALL_SITES = 14  # 13 -> 14：`api/app.py` 的人工发送
#: 那一处（2026-09-20，ADR-0141 决策 2 裁开 K15）。
#: **它的 hook 是 `pre_execute`**，不是裁定二明写不补的
#: `post_understanding` / `pre_generation` —— 所以这个数该加一，
#: 而不是这条判据该放宽。

#: 扫描结果里**必须有这三个**，三个分属三个不同的包。
#: 它们是「扫描器真的读到了源」的锚点 —— 比一个文件数门槛硬：
#: 门槛要么定高了当场红，要么定低了在扫描退化时照样通过。
MUST_BE_SCANNED = (
    "services/worker/src/fastagent_v2/worker/policy_output.py",
    "packages/runtime/src/fastagent_v2/runtime/authz.py",
    "services/api/src/fastagent_v2/api/app.py",
)

#: **符号扫描器的非零对照。** 十二个固定 hook 全是 0（见那条用例的 docstring），
#: 所以「零」那一侧自己给不出对照，要从旁边借两个真的存在的符号。
#:
#: 挑这两个不是随手取的：
#: * `ProactivityPolicyError` —— 正是它的 17 处子串命中，把
#:   「只有 `ProactivityPolicy` 按名字存在」那句话撑成了「26 处」；
#: * `CostGovernor` —— 「功能在、而形态是内联闸不是 hook 对象」那一族的代表。
SYMBOL_SCANNER_CONTROLS = ("ProactivityPolicyError", "CostGovernor")


def _production_sources() -> list[Path]:
    """`services/` 与 `packages/` 下所有 `.py`。

    **不走 `git grep`**：它只看被跟踪的文件，而一个刚写出来还没 `git add`
    的新发出点**正是这几条断言最该逮住的东西**（裁定二那条绊线第一版就栽在这里，
    回了 0 个文件而「只许一处」被 0 ≤ 1 静悄悄满足）。
    """

    files: list[Path] = []
    for tree in PRODUCTION_TREES:
        files += [
            path
            for path in (ROOT / tree).rglob("*.py")
            if "__pycache__" not in path.parts and ".venv" not in path.parts
        ]
    return sorted(files)


def _policy_output_calls() -> list[tuple[Path, ast.Call]]:
    """全部 `PolicyOutput(...)` 构造点，按文件排序。"""

    calls: list[tuple[Path, ast.Call]] = []
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "PolicyOutput":
                    calls.append((path, node))
    return calls


def _literal_keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def _string_constants(path: Path, wanted: str) -> int:
    """某个字符串在这个模块里作为**字面量**出现几次。

    **注释与 docstring 之外的 docstring 也算**（它们是 `Constant`），
    而 `#` 注释不算 —— 这正是想要的：`cost.py` 里那句提到四个 obligation 名字的
    注释**不是一次发出**，而一条 `grep` 会把它数进来。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sum(
        1 for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value == wanted
    )


def _defined_symbols() -> dict[str, list[str]]:
    """生产树里**被定义过的名字** → 定义它的文件。

    定义点算四种：`class` / `def`（含 `async def`）/ 赋值目标 / `import` 别名。
    **注释与 docstring 不算** —— 那正是与文本扫描的全部差别，
    也是「描述一个东西的行为，不要产生那个东西」在这里的落点：
    一份讨论 `PricingPolicy` 的文档不该让守卫变红。
    """

    defined: dict[str, list[str]] = {}
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                name = node.name
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.asname or node.name.split(".")[-1]
            if name:
                defined.setdefault(name, []).append(path.relative_to(ROOT).as_posix())
    return defined


def test_the_scanner_really_read_the_sources() -> None:
    """**守卫的守卫：先证明扫描器读到了东西，再让它去断言那些「零」。**

    下面每一条都断言某个东西恒为 0，而**扫描失效回的也是 0**。
    这一条把「扫到了」单独钉住，所以后面那几条不会拿一个空集去证明一件事。

    **锚点是三个点名的文件，不是一个文件数门槛，而这是改过一版的**：
    初版写的是 `len(sources) > 100`，实测只有 **68** 个 —— 那个 100 是拍的
    （抄自 `test_policy_output.py`，而那一份连 `.sql` 与 `scripts/` 一起扫）。
    一个拍出来的门槛有两种坏法：定高了当场红（就是这次），
    定低了则**在扫描退化到只剩几个文件时照样通过**。
    点名三个分属三个包的文件没有这个问题：glob 坏了、树挪了、
    `PRODUCTION_TREES` 写错了，至少有一个会缺席。

    `test_policy_output.py::test_the_sources_were_really_read` 是同一个手法，
    守的是另一组源（schema 与迁移）。两条互相点名。
    """

    found = {path.relative_to(ROOT).as_posix() for path in _production_sources()}
    missing = [path for path in MUST_BE_SCANNED if path not in found]
    assert not missing, (
        f"{missing} 不在扫描结果里（一共扫到 {len(found)} 个 .py）—— 扫描的位置不对。"
        "在修好它之前，本文件其余每一条「恒为 0」的断言都不成立。"
        "\n这几个文件如果是被**改名或搬走**了，改 `MUST_BE_SCANNED`；"
        "如果它们还在原地而这里说找不到，那是 `rglob` 或 `PRODUCTION_TREES` 坏了"
    )

    calls = _policy_output_calls()
    assert len(calls) == EXPECTED_CALL_SITES, (
        f"`PolicyOutput(` 构造点现在有 {len(calls)} 个"
        f"（收编裁定二时是 {EXPECTED_CALL_SITES} 个）："
        f"{sorted({path.relative_to(ROOT).as_posix() for path, _ in calls})}。"
        "\n这个数本身不是规矩 —— 加一处判定是正常的。要办的事是："
        "确认新那处的 hook 不是 post_understanding / pre_generation"
        "（那是裁定二明写不补的两个时点），然后改 `EXPECTED_CALL_SITES`。"
    )


def test_every_policy_output_names_its_hook_literally() -> None:
    """**每个构造点的 `hook=` 都必须是字面量，否则下一条就数不出来了。**

    这一条不守业务，守的是**下一条断言还成不成立**：
    `PolicyOutput(hook=some_var)` 在 AST 里读不出值，于是那个时点会被**少算一个** ——
    而少算的方向正好是让「恒为 0」通过。**报错要指向原因**：
    问题不在那个时点，在这里数不出来了。
    """

    unreadable = [
        path.relative_to(ROOT).as_posix()
        for path, call in _policy_output_calls()
        if _literal_keyword(call, "hook") is None
    ]
    assert not unreadable, (
        f"{unreadable} 里有 `PolicyOutput(hook=...)` 的 hook 不是字符串字面量。"
        "\n本文件靠读这个字面量数每个时点有几个发出点；读不出来时那个时点会被少算，"
        "而少算正好让「post_understanding / pre_generation 恒为 0」通过。"
        "\n要么把 hook 写成字面量，要么改掉本文件的数法 —— 别让它继续数一个数不全的数。"
    )


def test_only_the_four_wired_hook_points_have_writers() -> None:
    """**`post_understanding` 与 `pre_generation` 今天零写者，而那是裁定二明写的。**

    ADR-0137 §二 第 1 条：「**不新增发出点**：`post_understanding` 与 `pre_generation`
    今天零写者（B1-8 / B2-4），本条一行都不补 —— 补它们要先答『那两个时点各自要检什么』，
    **那是另一次裁定**。」RFC §3.4 与 §8 各有一条修订标注写着同一件事。

    **两个 0 与四个非零一起断言，而且要求它们加起来等于构造点总数。**
    只断言那两个 0 的话，一次读不出任何 hook 的扫描会让这条通过
    （`AGENTS.md`「计数类断言写等值，不要写上界」）。
    """

    counts = dict.fromkeys(HOOK_POINTS, 0)
    for _, call in _policy_output_calls():
        hook = _literal_keyword(call, "hook")
        assert hook in counts, f"{hook!r} 不在 `HOOK_POINTS` 里 —— 词表与发出点分家了"
        counts[hook] += 1

    wired = [point for point in HOOK_POINTS if point not in UNWIRED_HOOKS]

    for point in UNWIRED_HOOKS:
        assert counts[point] == 0, (
            # 1) 看到了什么
            f"`{point}` 现在有 {counts[point]} 个发出点（此前是 0）。"
            # 2) 为什么不允许
            "\nADR-0137 §二 第 1 条明写不补这两个时点的发出点，"
            "理由是「补它们要先答『那个时点各自要检什么』」——"
            "一行 hook 记录不等于一个 hook 契约（RFC §8 那条修订标注）。"
            # 3) 到期了该做什么
            "\n**如果这一行是有意加的，那么它到期了**：去做那一次裁定，"
            "把结论写进 ADR 并点名修订 §3.4 / §8 那两条标注，然后把 "
            f"`{point}` 从本文件的 `UNWIRED_HOOKS` 里挪走。"
        )

    for point in wired:
        assert counts[point] > 0, (
            f"`{point}` 的发出点变成 0 了 —— 它此前是有写者的。"
            "\n要么是有一处判定被删掉了，要么是本文件的数法失效了（先看上一条）。"
        )

    # **对常量，不对 `len(_policy_output_calls())`** —— 后者是一条恒真式。
    #
    # 初版写的是 `sum(counts.values()) == len(_policy_output_calls())`，
    # 而上面那个循环**每转一圈恰好 `+1`**，所以左右两边都是「循环跑了几次」，
    # 只是从两个方向数同一件事。它声称要抓「有构造点没被归到任何时点上」，
    # 而那个情形在循环里的 `assert hook in counts` 就已经红了 ——
    # **它不只是恒真，它要防的那件事已经有别的东西在防了。**
    # 复核窗口 2026-09-14 换四组桩实跑（空扫描 / 全塌到一个时点 / 掺 bogus / 真实），
    # 四种红的都不是它。
    #
    # **对常量之后它才是一条断言**：构造点增减时它会红，而那是一件真该有人
    # 看一眼的事 —— 多了一处构造点，意味着有人在新地方发 policy 输出。
    #
    # **它与上面 `test_the_scanner_really_read_the_sources` 里那条是同一个期望，
    # 而两处都留是有意的**：那一条守「扫描器读到东西了吗」，这一条守
    # 「逐时点数出来的和还对得上吗」——**同一个数，两个角色**
    # （`AGENTS.md`「两个事实今天必须相等」那一档）。
    # 数只写在 `EXPECTED_CALL_SITES` 一处，所以它们不会分家。
    assert sum(counts.values()) == EXPECTED_CALL_SITES, (
        f"六个时点数出来 {sum(counts.values())} 个发出点，而期望是 "
        f"{EXPECTED_CALL_SITES} 个。"
        "\n构造点增减了就改 `EXPECTED_CALL_SITES`（上面那条会先红并列出文件）；"
        "\n两个数对不上而构造点没变，说明本文件的数法失效了。"
    )


def test_require_evidence_is_never_emitted() -> None:
    """**四个 obligation 里，`require_evidence` 是唯一零发出的那个（B1-10）。**

    `policy_output.py` 的注释自己写着这句话，而在此之前**没有任何东西验它**。

    **为什么不接线**：`policy_decisions.obligations` **整列零读者**
    （下一条断言），所以把它发出来，产出的是本仓「五条判据」第 2 类
    ——「数据生产了，全仓无人消费」。**T35 的到期条件正是这件事**：
    「接第一个真实 policy 消费方之前」。

    **数法是 AST 的字符串字面量，不是 `grep`**：`cost.py` 里有一句注释逐个提到了
    四个 obligation 的名字，`git grep` 会把它数成一次命中。
    """

    named_in = {
        path.relative_to(ROOT).as_posix(): _string_constants(path, "require_evidence")
        for path in _production_sources()
    }
    named_in = {path: count for path, count in named_in.items() if count}

    assert named_in == {"services/worker/src/fastagent_v2/worker/policy_output.py": 1}, (
        f"`require_evidence` 作为字符串字面量出现在 {named_in} —— "
        "此前只在 `policy_output.py` 的 `OBLIGATIONS` 词表里出现一次。"
        "\n多出来的那一处如果是一次发出：`policy_decisions.obligations` 今天零读者，"
        "发出它就是「数据生产了，全仓无人消费」。**先去 T35**（到期条件就是"
        "「接第一个真实 policy 消费方之前」），把消费方那一头一起裁掉，"
        "再回来改这条断言。"
    )

    # **非零对照**：另外三个是真的在发。少了这一半，一次「什么都没扫到」的失败
    # 会让上面那条等值断言变成「空字典 == 空字典」……而它此刻恰好不是空字典，
    # 所以这里改成正面验证另外三个确实被命名过。
    emitted = OBLIGATIONS[0], OBLIGATIONS[1], OBLIGATIONS[3]
    for obligation in emitted:
        total = sum(_string_constants(path, obligation) for path in _production_sources())
        assert total > 0, (
            f"`{obligation}` 在生产里一次都没被命名 —— 它此前是在发的。"
            "四个 obligation 全部零发出，说明本文件的数法失效了，不是业务变了"
        )


def test_nothing_reads_the_obligations_column() -> None:
    """**`policy_decisions.obligations` 整列零读者 —— 上一条断言的另一半。**

    这一条是「为什么不接线」的证据。全仓读这张表的地方今天只有三处，
    读的都是 `reason_code`（以及 `id` 与 `count(*)`）：
    死循环升级判「上一行 deny 是不是同一个码」，接管页数「被拦下几次」。

    **它红的时候不是坏了，是到期了**：第一个真实 policy 消费方出现了，
    而那正是 T35 与 B1-10 一起等着的那一刻。
    """

    selects: list[tuple[str, str]] = []
    for path in _production_sources():
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(_SELECT_FROM_POLICY_DECISIONS, source):
            selects.append((path.relative_to(ROOT).as_posix(), match.group("columns")))

    assert len(selects) >= 3, (
        f"只扫到 {len(selects)} 条读 `policy_decisions` 的查询（此前是 3 条）——"
        "少于 3 条时这条断言可能只是什么都没扫到"
    )

    reading_obligations = [
        (path, columns) for path, columns in selects if "obligations" in columns.lower()
    ]
    assert not reading_obligations, (
        f"{reading_obligations} 开始读 `obligations` 列了 —— 这是第一个真实的 policy "
        "消费方，也就是 T35 与 B1-10 一起等的那一刻。"
        "\n去把 T35 关掉（它的到期条件就是这个），并重新判 `require_evidence` "
        "该不该发；然后把上一条断言改成新的形状。"
    )


def test_the_two_obligation_vocabularies_are_not_the_same_one() -> None:
    """**仓里有两张 obligation 词表，它们长得像而不是同一张。**

    | 词表 | 独有的那两个 |
    |---|---|
    | `runtime.authz.ObligationKind` | `manual_review` · `reason_required` |
    | `worker.policy_output.OBLIGATIONS` | `require_evidence` · `create_manual_task` |

    两张都有 `audit` 与 `redact`，差别全在上面那两列。

    **这是 `CLAUDE.md` 第三节那个形状**：两个词表在 `audit` / `redact` 上重叠，
    把一张的值传给另一张**不会报错**，只是那一行证据从此说的是别的事。

    **而它已经骗过一次人**：2026-09-14 的核心欠账重核里，B1-10 那行写
    「另外三个真的在发，`require_obligations_discharged` 核 `audit`」——
    而 `require_obligations_discharged` 消费的是**上面那张**（authz），
    不是 `policy_decisions` 那张。两张表的 `audit` 长得一模一样，所以那句话
    每个字都对，指向的却是另一个系统。

    这一条不改任何一张表，只把「它们不是同一张」钉住。
    """

    authz_kinds = set(ObligationKind.__args__)  # type: ignore[attr-defined]
    policy_kinds = set(OBLIGATIONS)

    assert authz_kinds & policy_kinds == {"audit", "redact"}, (
        f"两张词表的交集变成了 {sorted(authz_kinds & policy_kinds)} —— 此前是 "
        "`audit` / `redact` 两个。交集变大意味着它们正在合流，"
        "而合流之前要先答「一条证据行到底记的是哪一套义务」"
    )
    assert authz_kinds - policy_kinds == {"manual_review", "reason_required"}, (
        f"authz 独有的义务变成了 {sorted(authz_kinds - policy_kinds)}"
    )
    assert policy_kinds - authz_kinds == {"require_evidence", "create_manual_task"}, (
        f"policy-hook 独有的义务变成了 {sorted(policy_kinds - authz_kinds)}"
    )


def test_none_of_the_twelve_fixed_hooks_exists_as_a_symbol() -> None:
    """**裁定二「不改结构」那一半：十二个固定 hook 一个都不是符号（A5 / D1）。**

    ## 「十二个里只有 `ProactivityPolicy` 按名字存在」这句话，写这条用例时被推翻了

    那句话在仓里有四处（ADR-0137 §二、RFC §3.4 修订标注、`policy_output.py`
    的模块 docstring、2026-09-14 的核心欠账重核），依据都是
    `git grep -c "ProactivityPolicy" -- services packages` → **26**。

    **实测（2026-09-14，基线 `06c399d5`）：那 26 里有 17 处是
    `ProactivityPolicyError` 的子串命中。** 去掉它们剩 **9 处，全部是注释与
    docstring 的散文**。仓里**没有任何一个叫 `ProactivityPolicy` 的符号** ——
    有的是一个模块 `proactivity.py` 和一个异常类 `ProactivityPolicyError`。

    所以本条断言的是**十二个全为 0**，不是「十一个为 0」。
    这是 `AGENTS.md`「`grep` 命中不等于有人在守」与「`-c` 数命中，`-l` 数文件」
    的又一个实例，**而这一次被数错的是一句被抄了四遍的话**。

    ## 为什么数「被定义的符号」，不数文本命中

    文本命中会把散文数进来 —— 上面那 9 处正是。更要命的是
    `AGENTS.md`「描述一个东西的行为，不要产生那个东西」记的三次事故：
    **一份讨论这条规矩的文档会让这条用例变红**，而
    「一个会因为『有人写文档提到了它』而变红的门禁，下一个人学会的是绕过它」。

    走 AST 只认**定义点**（`class` / `def` / 赋值目标 / `import` 别名）之后，
    注释怎么写都不会碰它，而真的建出一个 `class PricingPolicy` 会当场红。

    ## ⚠️ 它守的是符号，不是能力 —— 这个方向是承重的

    有人把定价策略实现成一个**不叫 `PricingPolicy`** 的模块，这条照绿。
    **能力那一侧没有执行者**，只有带坐标与计数的登记
    （ADR-0137 §五 的【D1 关闭】那一块就是那份坐标与计数）。
    所以这条用例的名字里带着 `as_a_symbol`：`AGENTS.md`「描述性质的词，
    在它只于某个方向成立时必须带方向」。

    **它红的时候多半不是坏了，是有人开始建 hook 对象了** ——
    而那一刻正是该重看裁定二的时刻。
    """

    defined = _defined_symbols()

    # **非零对照**：符号扫描器真的在工作。挑这两个是有讲究的 ——
    # `ProactivityPolicyError` 正是把上面那句话数错的那个子串，
    # `CostGovernor` 是「功能在、形态是内联闸」的那一族里最具代表性的一个。
    for control in SYMBOL_SCANNER_CONTROLS:
        assert defined.get(control), (
            f"`{control}` 作为符号找不到了 —— 它是本条那十二个「零」的非零对照。"
            "对照没了，下面那条断言就可能只是符号扫描器坏了"
        )

    present = {name: defined[name] for name in FIXED_HOOK_NAMES if defined.get(name)}
    assert not present, (
        # 1) 看到了什么
        f"这些名字现在是生产代码里的符号：{present}。"
        # 2) 为什么不允许
        "\nADR-0137 §二（裁定二）取的是「只统一输出，不改结构」——"
        "十二个固定 hook 今天一个都不是符号，功能以内联闸的形态存在，"
        "**而那是一次裁定，不是一个遗漏**。"
        # 3) 到期了该做什么
        "\n**如果是有意开始建 hook 对象了，那么这条到期了**："
        "去推翻或收窄裁定二（新 ADR 点名修订 ADR-0137 §二），"
        "并同时改 RFC §3.4 那条修订标注（它今天写着「固定 hooks 那一行是"
        "尚未兑现的结构」），然后把这个名字从 `FIXED_HOOK_NAMES` 里挪走。"
    )


@pytest.mark.parametrize("point", UNWIRED_HOOKS)
def test_the_unwired_points_are_still_in_the_vocabulary(point: str) -> None:
    """**零写者不等于该从词表里删掉** —— 这是反方向的那一半。

    `policy_output.py` 的注释写着：「词表的边界是图纸定的，不是『今天谁在用』定的」。
    有人看到两个时点零写者而去「清理」词表，会让 §3.4 的六时点契约与实现分家 ——
    而那正是 `HOOK_POINTS` 那三份副本相等断言（`test_policy_output.py`）要防的事。

    **这一条与上面那条是一对**：那条不许它们有写者，这条不许它们被删掉。
    """

    assert point in HOOK_POINTS, (
        f"`{point}` 从 `HOOK_POINTS` 里消失了 —— 零写者是裁定二有意留的状态，"
        "不是该被清理掉的死值。RFC §3.4 的 hook 枚举有六个，词表按图纸写全"
    )
