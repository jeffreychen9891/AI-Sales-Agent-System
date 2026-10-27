"""`policy_decisions` 只许一处写入，那三个词表只许有一份（裁定二 / ADR-0137 §二）。

**裁定二取的是折中 ③：只统一输出，不改结构。** 收编之前，七处各自手写同一条
12 列 `INSERT`，每处自带字面量的 hook / effect / obligations / `policy_id` /
`policy_version`。每一处单独看都对 —— **错的是它们之间的差**，而 diff 一次
只给你看一处（`AGENTS.md`「单一写入点 + 绊线」）。

## 三个词表有三份权威副本，这一份用例负责让它们对上

| 在哪 | 形态 |
|---|---|
| `packages/contracts/schemas/policy-hook.schema.json` | 契约的 `enum`（`validate_contract` 用它） |
| `infrastructure/migrations/001_m0.sql` | `hook` / `effect` 两列的 `CHECK`（`obligations` 没有） |
| `worker/policy_output.py` | 写入点的封闭词表（**构造时**就抛） |

**`obligations` 那一列今天连 CHECK 都没有** —— T35 的另一半正是这个：
一个不在枚举里的 `"alert"` 从 `cost.py` 写了进去，库没拦，也没人读。
本文件最后一条把「没有 CHECK」这件事**也断言下来**：哪天有人加了 CHECK，
它会红，而红的那一刻正是该判断「两道闸要不要都留着」的那一刻。

## 为什么词表是**解析**出来的，不是在这里再抄一遍

抄第四遍会让这份用例变成自证的：它会拿要检的东西当检的依据
（`test_reference_projection_wiring.py` 因为这个被退回过一次）。
所以 schema 与迁移都现读现解析，而 `test_the_sources_were_really_read`
先证明解析器真的读到了东西 —— **守卫的守卫**。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from fastagent_v2.worker.policy_output import (
    EFFECTS,
    HOOK_POINTS,
    OBLIGATIONS,
    PolicyOutput,
    coerce_obligations,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "packages/contracts/schemas/policy-hook.schema.json"
MIGRATION = ROOT / "infrastructure/migrations/001_m0.sql"
WRITE_POINT = "services/worker/src/fastagent_v2/worker/policy_output.py"


def _schema_enum(*path: str) -> set[str]:
    """顺着 JSON 指针取一个 `enum`，取不到就抛 —— **不静默回退**。

    回退成空集会让下面每条等值断言变成「空集等于空集」。
    """

    node = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    values = node["enum"]
    assert isinstance(values, list) and values, f"{path} 的 enum 是空的"
    return set(values)


def _migration_check(column: str) -> set[str] | None:
    """`policy_decisions` 里某一列 `CHECK (col IN (...))` 的取值；没有 CHECK 时回 `None`。

    **按块取**：`001_m0.sql` 里十几张表都有叫 `effect`、`status` 的列，
    而一条在整文件上跑的正则会拿到先出现的那一张 —— 「先出现」不是判据。
    """

    sql = MIGRATION.read_text(encoding="utf-8")
    start = re.search(r"CREATE TABLE(?: IF NOT EXISTS)? policy_decisions\s*\(", sql)
    assert start, "policy_decisions 的 CREATE TABLE 不在 001_m0.sql 里"
    rest = sql[start.end() :]
    end = re.search(r"\n\);", rest)
    assert end, "policy_decisions 的 CREATE TABLE 没有收尾"
    block = rest[: end.start()]
    assert re.search(rf"\b{column}\b", block), f"policy_decisions 没有 {column} 这一列"
    match = re.search(
        rf"{column}\s+\w+(?:\s+NOT\s+NULL)?(?:\s+DEFAULT\s+\S+)?\s*"
        rf"CHECK\s*\(\s*{column}\s+IN\s*\((?P<values>.*?)\)\s*\)",
        block,
        re.S,
    )
    if match is None:
        return None
    return set(re.findall(r"'([a-z_]+)'", match.group("values")))


def test_the_sources_were_really_read() -> None:
    """**先证明两个解析器真的读到了源**，再让它们去判别的事。

    自证的守卫不是假想的失败模式：`test_reference_projection_wiring.py` 初版
    就是用一个写死了七个名字的正则去迁移里找那七个名字，于是它一条漂移都拦不住，
    而 docstring 声称防的正是漂移（复核窗口 2026-09-13 退回）。

    这一条把那件事单独钉住。**这里用的不是那份用例的手法** ——
    那边能断言「解析出来的词表里有常量之外的值」，因为活着的状态本就是词表的真子集；
    而这边三份副本应当**完全相等**，那条手法在这里恒假。
    所以换成基数：六个时点、四个 effect 是**数出来的**，
    正则或 JSON 指针失配回了空集、半集（比如只匹到第一行里的三个），这条先红，
    而下面那几条等值断言就不会拿一个空集去「对上」另一个空集。
    """

    hooks = _schema_enum("properties", "input", "properties", "hook")
    effects = _schema_enum("properties", "output", "properties", "effect")
    assert len(hooks) == 6, f"schema 解析出 {sorted(hooks)} —— 不是六个时点，解析器没读对"
    assert len(effects) == 4, f"schema 解析出 {sorted(effects)}"

    migration_hooks = _migration_check("hook")
    assert migration_hooks is not None, "hook 那一列的 CHECK 没解析出来（迁移写法变了？）"
    assert len(migration_hooks) == 6, f"迁移解析出 {sorted(migration_hooks)}"


@pytest.mark.parametrize(
    ("constant", "schema_path"),
    [
        (HOOK_POINTS, ("properties", "input", "properties", "hook")),
        (EFFECTS, ("properties", "output", "properties", "effect")),
        (
            OBLIGATIONS,
            ("properties", "output", "properties", "obligations", "items"),
        ),
    ],
    ids=["hook", "effect", "obligations"],
)
def test_the_vocabularies_match_the_contract(
    constant: tuple[str, ...], schema_path: tuple[str, ...]
) -> None:
    """写入点的三个词表 == 契约 schema 的三个 `enum`。

    **两边都可能是漂的那一头**：schema 里加一个 effect 而写入点不认，
    等于契约允许的东西写不进去；写入点多一个而 schema 不认，
    等于我们能写出过不了 `validate_contract` 的行 —— 相等断言两个方向都盖到了。
    """

    assert set(constant) == _schema_enum(*schema_path)


def test_hook_and_effect_also_match_the_column_checks() -> None:
    """再对一遍库里的 `CHECK` —— 第三份副本。

    契约与写入点一致而**库**不一致，是最难查的一种：合同校验过了、
    Python 也过了，`INSERT` 在运行时被数据库拒掉，而报错指着的是数据库。
    """

    assert _migration_check("hook") == set(HOOK_POINTS)
    assert _migration_check("effect") == set(EFFECTS)


def test_obligations_still_has_no_check_in_the_database() -> None:
    """**T35 没关掉的那一半：`obligations` 这一列没有 CHECK。**

    这不是在庆祝一个缺口，而是把「唯一那道闸在 Python 里」这件事写下来：
    `PolicyOutput.__post_init__` 拦不住的话，今天**没有第二个人**拦得住。

    哪天有人给这一列加了 CHECK，这条会红 —— 而那正是该判断
    「两道闸都留着，还是把 Python 那道降成注释」的时刻。
    """

    assert _migration_check("obligations") is None, (
        "`obligations` 现在有 CHECK 了 —— 去决定这两道闸的关系，别让它们各自漂"
    )


def _sources() -> list[Path]:
    """要扫的那几棵树里所有 `.py` / `.sql`。

    **走文件系统，不走 `git grep`**：第一版用了 `git grep`，而它只看
    **被跟踪的**文件 —— 写入点自己刚建出来还没 `git add`，于是它回了个空集。
    空集配上「只许一处」这种写法会**静悄悄通过**（0 ≤ 1）；
    这里的等值断言当场红了，而那正是「守卫本身也可能是空的」那一类。
    """

    files: list[Path] = []
    for tree in ("services", "packages", "scripts", "infrastructure"):
        for pattern in ("*.py", "*.sql"):
            files += [
                path
                for path in (ROOT / tree).rglob(pattern)
                if "__pycache__" not in path.parts and ".venv" not in path.parts
            ]
    assert len(files) > 100, f"只扫到 {len(files)} 个文件 —— 这个数太小，扫的位置不对"
    return files


#: 那条语句本身。写成常量，是为了让下面那个判据与报错说的是同一个东西。
_STATEMENT = "INSERT INTO policy_decisions"


def _writes_a_decision(path: Path) -> bool:
    """这份文件里**真的有**那条语句吗 —— 不是「提到过这几个字」。

    ## 为什么不是一句 `in text`

    第一版是。2026-09-16 它**假红了一次**：一个新模块的 docstring 里写着
    「与 `policy_output` 那条『`INSERT INTO policy_decisions` 只许一处』
    同一条纪律」—— **一句引用这条纪律的话，触发了这条纪律**。
    那个模块一行 SQL 都没有，它调的正是 `record_policy_decision()`。

    这是 `CLAUDE.md` 第三节那张表里「**对一行注释假红**」那一格
    （批 6 §三 D 在另一条守卫上量到过同一个形状）。
    **假红与漏报是一对**：一条会为注释叫的守卫，下一次真被它拦住的人
    会先怀疑守卫，而不是怀疑自己。

    ## 判据

    * `.py`：走 `ast`，只看**字符串常量**，并且**排除 docstring** ——
      一条 SQL 是字符串常量，而讲这条 SQL 的话也是。两者的区别在位置上，
      而 `ast` 正好看得见位置。语法坏掉的文件按「含有」处理（宁可假红，
      不可因为读不动而放过）；
    * `.sql`：先去掉 `--` 注释再找。迁移文件里那条语句只可能出现在语句里。
    """

    text = path.read_text(encoding="utf-8")
    if _STATEMENT not in text:
        return False
    if path.suffix == ".sql":
        stripped = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
        return _STATEMENT in stripped

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _STATEMENT in node.value
        and id(node) not in docstrings
        for node in ast.walk(tree)
    )


def test_there_is_exactly_one_write_point() -> None:
    """**绊线：全仓只许一条 `INSERT INTO policy_decisions`**（测试与 e2e 夹具除外）。

    收编前是 7 处。第八处出现的方式不是有人存心绕开它，
    而是有人**照着旁边那处抄**——所以这条红的时候，
    要办的事是把新那处接到写入点上，不是把这条断言的数字改大。

    迁移文件也在扫描范围里，而它只有 `CREATE TABLE`，没有 `INSERT` ——
    哪天有人往迁移里塞一行判定（数据补写），这条同样会红。
    """

    hits = sorted(
        path.relative_to(ROOT).as_posix() for path in _sources() if _writes_a_decision(path)
    )

    assert hits == [WRITE_POINT], (
        f"写 policy_decisions 的地方是 {hits} —— 只该有 {WRITE_POINT}；"
        "新增的那处请改成调用 record_policy_decision()"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hook", "pre_commit"),
        ("hook", "PRE_EXECUTE"),
        ("effect", "warn"),
        ("effect", "allowed"),
    ],
    ids=["unknown-hook", "wrong-case-hook", "warn-is-not-an-effect", "typo-effect"],
)
def test_a_value_outside_the_vocabulary_is_refused_at_construction(
    field: str, value: str
) -> None:
    """**不合法的值在写它的那一行抛**，不是在数据库里抛。

    `"warn"` 是这里最值得点名的一个：`CeilingDecision.effect` 真的有
    `allow / warn / deny` 三值，而 `policy_decisions.effect` 只有四个别的值 ——
    **两套词表长得像**，把成本判定的 effect 直接当 policy effect 传是一步很自然的错。
    """

    kwargs: dict[str, object] = {
        "hook": "pre_execute",
        "effect": "allow",
        "reason_code": "TEST",
        "policy_id": "test-policy",
        "policy_version": "1.0.0",
        "obligations": ("audit",),
        "context": {},
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=value):
        PolicyOutput(**kwargs)  # type: ignore[arg-type]


def test_the_alert_obligation_from_t35_cannot_be_written_again() -> None:
    """**T35 的具体那次，做成一条用例。**

    `"alert"` 不在契约的义务枚举里，从 `cost.py` 静悄悄写进了 `obligations`，
    而这一列没有 CHECK、那一处又是全仓唯一跳过 `validate_contract` 的写点 ——
    三件事凑齐，于是一个没人读的值在库里躺了下来。

    两条路都堵：构造 `PolicyOutput` 时，以及把一个**算出来的**列表
    交给 `coerce_obligations()` 时（`plan-validator` 那一处的义务就是算出来的）。
    """

    with pytest.raises(ValueError, match="alert"):
        PolicyOutput(
            hook="pre_inference",
            effect="allow",
            reason_code="COST_SOFT_CEILING_BREACHED",
            policy_id="cost-governor",
            policy_version="1.0.0",
            obligations=("audit", "alert"),
            context={},
        )

    with pytest.raises(ValueError, match="alert"):
        coerce_obligations(["audit", "alert"])


def test_an_anonymous_decision_is_refused() -> None:
    """空理由码 = 一行查不到的证据。

    `policy_decisions.reason_code` 是 `text NOT NULL`，而 `''` 过得了 NOT NULL ——
    库拦得住 `NULL`，拦不住空串，所以这一条在这里。
    """

    with pytest.raises(ValueError, match="reason_code"):
        PolicyOutput(
            hook="pre_execute",
            effect="deny",
            reason_code="",
            policy_id="m0-minimum-policy",
            policy_version="1.0.0",
            obligations=("audit",),
            context={},
        )


def test_a_legal_decision_keeps_its_defaults_empty_not_null() -> None:
    """没传 `modifications` 时它是 `{}`，**不是 `None`**。

    这不是洁癖：`context` / `modifications` 两列都是 `jsonb NOT NULL DEFAULT '{}'`，
    而 `Jsonb(None)` 写出来的是 `'null'::jsonb` —— **它过得了 NOT NULL**。
    于是「没传」与「传了个空」在库里长得不一样，
    而查那一行的人分不出是哪种（行为面在
    `tests/integration/test_policy_decision_write_point.py`）。
    """

    output = PolicyOutput(
        hook="pre_delivery",
        effect="deny",
        reason_code="PROACTIVITY_WINDOW_EXPIRED",
        policy_id="proactivity-window",
        policy_version="1.0.0",
        obligations=("audit",),
        context={"outbox_id": "x"},
    )

    assert output.modifications == {}
    assert output.next_allowed_at is None
