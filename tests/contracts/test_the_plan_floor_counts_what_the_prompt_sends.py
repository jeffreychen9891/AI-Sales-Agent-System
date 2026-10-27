"""plan 的固定底必须把提示词真的发出去的每一项都算进去。

## 它为什么存在：一支全绿的探针

裁定一 ① 往 plan 的 `user_prompt` 加了第三张策略表
（`required_slots_by_strategy`），**而 `_check_request_floors` 的
`PLAN_CAPABILITY` 那一项没跟着加** —— 于是一个**进了提示词却没进预算**的项
存在了一整批。补上之后做了一支反向探针：**把它删掉，八十五条用例全绿** ——
当时没有任何东西看着「固定底与提示词是不是在数同一些东西」。
本文件是那支探针的产物（`AGENTS.md`「探针的价值在它绿的时候」）。

## 2026-09-15：它守的那件事从「比对」变成了「结构上不可能」

先前两侧各自拼一份 `user_prompt`，所以本文件**解析两边的源码、比键集**。
那个做法自己带着一个洞，而那个洞在 2026-09-15 发作了：**键集相等不蕴含字节相等。**
实测在架八帧，闸比真实请求少算 **639–840** 字节，方向 fail-open ——
三笔成因里有两笔（单层对两层、`separators` 方向反了）**键集完全相等**，
第三笔（`user_prompt` 自己那一圈键名没人记账）更是**两边都没有那个键可比**。

**修法是把形状收成唯一定义点**（`runtime/request_shape.py`），
两侧都调同一个函数。于是「两边在数同一些东西」**不再需要比对 —— 它是恒真的**。

## 所以本文件改守两件别的，而这两件正是新的失败面

1. **谁都不许再在本地拼一份 plan 的 `user_prompt`。**
   这是「单一写入点 + 绊线」那条纪律的直接应用：
   先前那个洞的**唯一成因**就是有两处在各拼各的。
2. **固定底必须等于真实请求**（`test_request_floor_equals_the_real_request`，
   另一份文件）—— 那一条管字节，本文件管结构。**两条缺一不可**：
   只有字节那条时，有人重新拼一份局部副本、而那一刻两边碰巧相等，它照样绿。

**唯一定义点让两边永远一致 —— 包括一致地错。**
所以「两份缺一不可」**有证据，不是设计时的谨慎**：探针 R3
（`scripts/probe_request_floor.py`）从那份唯一的组装里删掉一张策略表，
**闸与「真实请求」都少那个键**（两边调的是同一个函数），
**于是字节那份仍然全绿** —— 红的只有结构那份。
一般化成一条挂在 `AGENTS.md`「单一写入点 + 绊线」下：
**唯一定义点要配一条不依赖它的检查。**

**一条守卫如果要靠抄一遍被守的逻辑才能工作，它自己就是下一个要分家的副本** ——
这句话是上一版本文件的结论，而它这一次说的是它自己。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHAPE = ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
MODEL = ROOT / "services/worker/src/fastagent_v2/worker/inference_model.py"
REGISTRY = ROOT / "services/api/src/fastagent_v2/profiles/registry.py"
REPORTER = ROOT / "scripts/check_material_dimensions.py"

#: plan 的 `user_prompt` 里那几个**按策略键**的表。少一张就是「提示词发了而预算没算」。
STRATEGY_TABLES = frozenset(
    {
        "allowed_intent_kinds_by_strategy",
        "allowed_generation_constraints_by_strategy",
        "required_slots_by_strategy",
    }
)


def _dict_literals_with(source: Path, *, marker: str) -> list[set[str]]:
    """`source` 里所有**含 `marker` 这个键**的 dict 字面量，各自的键集。

    走 AST 而不是 `grep`：`grep` 分不出「一个 dict 字面量里有这个键」与
    「某一行散文提到这个名字」，而本文件两种都会遇到
    （那几个键名在注释里被反复讨论）。
    """

    found: list[set[str]] = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if marker in keys:
            found.append(keys)
    return found


def test_the_plan_prompt_is_built_in_exactly_one_place() -> None:
    """**唯一定义点**：全仓只有 `request_shape.plan_prompt` 拼这份 dict。

    判据取 `allowed_strategies` 那个键 —— 它是 plan `user_prompt` 独有的，
    而且从第一版起就在，所以「含它的 dict 字面量」等价于「一份 plan 提示词组装」。
    """

    shape = _dict_literals_with(SHAPE, marker="allowed_strategies")
    assert len(shape) == 1, (
        f"`request_shape.py` 里有 {len(shape)} 份 plan 提示词组装，应当恰好 1 份"
    )

    assert shape[0] >= STRATEGY_TABLES, (
        f"唯一那份组装少了策略表：缺 {sorted(STRATEGY_TABLES - shape[0])} —— "
        "提示词少发一张表，模型就少一份词表；而这正是 "
        "`GENERATION_CONSTRAINT_NOT_DECLARED` 那次事故的形状"
    )

    for other in (MODEL, REGISTRY):
        extra = _dict_literals_with(other, marker="allowed_strategies")
        assert not extra, (
            f"{other.name} 里又出现了 {len(extra)} 份本地的 plan 提示词组装。"
            "**那正是 2026-09-15 那个 639–840 字节 fail-open 缺口的唯一成因** —— "
            "两处各拼各的，键集相等而字节不等。组装只许在 "
            "`runtime/request_shape.py` 一处，两边都调它。"
        )


def test_both_sides_reach_the_shared_shape() -> None:
    """生产与 admission **都**从那一处取形状 —— 不是只有一边。

    只断言「别处没有副本」是不够的：一边改成不调它、自己算一个数，
    上面那条照样绿。
    """

    for path in (MODEL, REGISTRY):
        text = path.read_text(encoding="utf-8")
        assert "request_shape" in text, (
            f"{path.name} 不再引用 `request_shape` —— 固定底与提示词又分家了"
        )


def test_the_reporter_prints_the_same_numbers_the_gate_computes() -> None:
    """报告器打印的固定底，**逐帧等于闸算的** —— 跑它，不是读它的源码。

    ## 这一条是被一次复核换掉的（独立复核 2026-09-15，④）

    上一版禁的是**源码里的一个子串**（`PLATFORM_PROMPT_BYTES[`）。
    复核方拿三种等价写法逐个试，实测：

    | 变异 | 上一版 |
    |---|---|
    | `PLATFORM_PROMPT_BYTES[r] + PLATFORM_RESPONSE_SCHEMA_BYTES[r]` | 红 ✅ |
    | 同上改成 `.get(r, 0)` | **全绿** ❌ |
    | `import ... as _p` 之后 `_p[r]` | **全绿** ❌ |

    还有一处更难看的：上一版另一条断言是 `"request_floor_bytes" in text`，
    而那个名字在报告器的 **docstring** 与那句 **import** 里各有一处 ——
    **唯一那次调用被整行换掉之后，它依然为真。**

    **所以判据换成行为**：把报告器**跑起来**，把它打印的那几个数与
    `request_floor_bytes` 现算的比。**怎么算出来的不重要，算得对不对才重要** ——
    三种变异写法算出来的数都与闸不同，于是三种都红。

    **这也是「唯一定义点要配一条不依赖它的检查」那条纪律的第二个实例**：
    本文件另外两条验结构（形状只有一处、两边都调它），这一条验**结果**，
    而它连报告器有没有调那个函数都不看。
    """

    import json as _json
    import re
    import subprocess
    import sys

    from fastagent_v2.profiles.registry import request_floor_bytes

    package = ROOT / "profiles" / "internal-demo-sales"
    import os

    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [sys.executable, str(ROOT / "scripts" / "check_material_dimensions.py"), str(package)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        # **不继承调用方的编码设定。** 本条第一版没传 `env`，于是它
        # **本地绿、门禁红**：我的 shell 里 export 过 `PYTHONIOENCODING=utf-8`，
        # 门禁的环境里没有，子进程在第一句中文上 `UnicodeEncodeError`、
        # stdout 0 字节。**脚本那一侧也修了**（它自己 reconfigure stdout），
        # 这里再钉一道：**一条用例不该依赖调用方的环境变量**。
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert completed.returncode == 0, (
        f"报告器跑挂了（exit {completed.returncode}）—— 先看它自己的报错，再看这条断言。"
        f"stderr 尾部：{completed.stderr[-400:]!r}"
    )
    printed = dict(
        re.findall(
            r"帧 (\S+)\s+understand ([\d,]+)（余", completed.stdout
        )
    )
    assert printed, (
        "报告器没有打印固定底那一段 —— 它要么挂了，要么不再报这个数。"
        f"stdout 头部：{completed.stdout[:400]!r}"
    )

    document = _json.loads((package / "profile.json").read_text(encoding="utf-8"))
    for frame in document["target_frames"]:
        name = str(frame.get("id"))
        assert name in printed, f"报告器没报帧 {name}"
        expected = request_floor_bytes(frame, document=document)["UNDERSTAND_VALUE"]
        assert int(printed[name].replace(",", "")) == expected, (
            f"帧 {name}：报告器打印 {printed[name]}，而闸算的是 {expected:,}。"
            "**报告器又有自己的账了** —— 它的 docstring 自己写着「A number here that "
            "disagrees with admission is a bug in this script」。"
            "固定底只有 `registry.request_floor_bytes` 一处。"
        )


def test_the_two_byte_tables_have_no_computational_consumer() -> None:
    """两张表**只剩定义点**：`services` / `packages` / `scripts` 下零处读它们。

    与上一条分工：那一条验**报告器算得对不对**，本条验
    **有没有第二个地方把这两张表拿去算什么** —— 包括 `registry.py` 自己，
    以及任何新写的消费方（上一版只读报告器一个文件，那是复核指出的第三处窄）。

    **走 AST 数 `Name` 的 Load**，不是数子串：`.get(r, 0)` 与
    `import ... as _p` 两种写法都逃不掉（前者名字仍在，后者 alias 会被
    `import` 那一支认出来）。
    """

    import ast

    tables = {"PLATFORM_PROMPT_BYTES", "PLATFORM_RESPONSE_SCHEMA_BYTES"}
    offenders: list[str] = []
    for root in ("services", "packages", "scripts"):
        for path in (ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # 定义点本身不算：registry.py 里那两个赋值
            assigned_here = {
                t.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Name)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in tables
                    and node.id not in assigned_here
                ):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.name in tables:
                            offenders.append(
                                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}（import）"
                            )

    assert not offenders, (
        f"这两张表又被拿去算东西了：{offenders}。"
        "它们是**说明钱花在哪**的文档，不是固定底的算法 —— "
        "固定底只有 `registry.request_floor_bytes` 一处。"
        "（`AGENTS.md`「退役一个东西之前，先问它今天还在证明什么」记着为什么它们留着。）"
    )


def test_the_reporter_runs_without_an_inherited_encoding_variable() -> None:
    """报告器**不带 `PYTHONIOENCODING` 也要跑得起来** —— 守的是脚本那一侧的修法。

    ## 它为什么单独一条（独立复核 2026-09-15，④ 指出）

    上面那条用例把子进程的环境钉死成 `PYTHONIOENCODING=utf-8`，
    **于是脚本顶部那两行 `sys.stdout.reconfigure` 没有任何会红的检查** ——
    把它们删掉，上面那条照绿。复核方实测过这张表：

    | | 提交态 | 把脚本那两行删掉 |
    |---|---|---|
    | 直接跑报告器（环境里没有该变量） | exit=0，中文正常 | **exit=1，stdout 0 字节** |
    | 上面那条用例 | 1 passed | **1 passed** ← 看不见 |

    **它今天不影响门禁**（门禁里那条用例自带环境），
    **影响的是下一个手工跑这个工具的人** —— 而那正是那次修法要保护的人。
    「一条断言『在』不等于它『能红』」，这一条就是那个『能红』。

    **所以这里把变量从环境里剥掉**，而不是设成 utf-8。
    """

    import os
    import re
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    completed = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            sys.executable,
            str(ROOT / "scripts" / "check_material_dimensions.py"),
            str(ROOT / "profiles" / "internal-demo-sales"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        env=env,
    )

    assert completed.returncode == 0, (
        "不带 PYTHONIOENCODING 跑报告器挂了 —— 脚本顶部那两行 "
        "`sys.stdout.reconfigure(encoding=\"utf-8\")` 是不是被删了？"
        f"（exit {completed.returncode}）stderr 尾部：{completed.stderr[-300:]!r}"
    )
    # **断言它真的打印了中文**，不只是「没崩」：cp1252 下第一句中文就会炸，
    # 所以「stdout 里有中文」与「它没有退回那个编码」是同一件事。
    assert re.search(r"[一-鿿]", completed.stdout), (
        "报告器没打印出中文 —— 编码那一侧退回去了，而退出码没说"
    )



def test_both_sides_tell_the_model_the_allowed_values() -> None:
    """生产与 admission **都**把话题槽的合法取值传进理解步 —— 不是只有一边。

    ## 为什么要这一条，而不是靠那两条用例

    `test_the_model_is_told_exactly_what_the_code_enforces` 自己调
    `request_shape.understand_prompt`，**所以它量的是形状函数，不是调用点**：
    生产那边把 `allowed_slot_values=` 摘掉，那一条照绿，
    **而真实请求里模型就再也收不到那份名单了**。

    这正是探针 R3 记下的那个形状 —— **唯一定义点让两边永远一致，包括一致地错**，
    而"一边干脆不调它"是它的另一个出口。

    ## 它还守着固定底

    admission 那边漏传，闸算的固定底就比生产真实要付的**少一截**，
    方向是 fail-open（看起来余量更多）—— 2026-09-15 那次 639–840 字节的缺口
    就是这个方向。字节那条用例会跟着红，而这一条指得更准。
    """

    for path in (MODEL, REGISTRY):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == "understand_prompt")
                or (isinstance(node.func, ast.Name) and node.func.id == "understand_prompt")
            )
        ]
        assert calls, f"{path.name} 不再调 `understand_prompt` —— 形状又分家了"
        for call in calls:
            keywords = {keyword.arg for keyword in call.keywords}
            assert "allowed_slot_values" in keywords, (
                f"{path.name}:{call.lineno} 调 `understand_prompt` 时没传 "
                "`allowed_slot_values` —— 模型收不到那份合法取值名单，"
                "于是它只能照 goal 的散文猜，而第五条丢弃规则会把猜错的整条丢掉"
            )
