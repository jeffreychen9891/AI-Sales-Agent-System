"""**计划文档不落库 —— 这是 B1-11 与 B1-12 那一族零读者的根，不是各自缺一条线。**

## 它要钉住的那件事

`plan-result` 合同今天声明 **13 个** 顶层键（12 个必填 + 可选的 `knowledge_query`，
`additionalProperties: false`）。而**校验通过之后，`pipeline` 只从 `ValidatedPlan`
身上读七个**：五个下标加两个 property。

**全仓没有任何一张表装计划文档**（`git grep -c "INSERT INTO plan" -- services packages` → 0），
所以**不在那七个里的键，今天没有任何可能的读者** —— 不是「还没接线」，
是「接线的那一头不存在」。

| 偏移登记 | 那个字段 | 为什么零读者 |
|---|---|---|
| **B1-12** | `manual_review_reason` | 不在那七个里，而文档不落库 |
| **B1-11** | `confidence.calibrated_score` 等三个 | 同上 |

**这个区分决定处置方向**，而它正是 `AGENTS.md`「分类要按处置方式分，
不按表面形态分」那一条：**「只差接线」的修法是写一个读点，
「另一头不存在」的修法是一次架构决定**（计划要不要落库、还是把那几个字段
搬到别处）。把它们当成前者，会有人去加一个读点然后以为接上了。

`inference.safe_fallback_plan` 的 `knowledge_query` 注释里已经写着同一件事
（「全仓没有一张表装 plan 文档，`pipeline` 只从它身上读五个字段」），
**而在这一份之前没有任何东西验它** —— 登记 **T347**。

## ⚠️ 它守不住什么

**「有人把计划文档塞进某张已有表的一个 jsonb 列」这条路，本文件拦不住。**
下面那条按表名扫迁移（`test_commitment_ledger_absence.py` 的手法），
只逮得住「新建一张装计划的表」。

写出来是因为**不写就会被当成全覆盖**：第一条断言（读点集合等值）能逮住
「有人开始读第八个键」，第三条能逮住「有人建表」，
**而「加一个 jsonb 列然后写进去」两条都不碰**。
真要覆盖它，得有一条盯 `Jsonb(plan.document)` 形态的断言 —— 本批没做，
理由是那个形态今天一处都没有，写一条盯着零的断言不如把这段话写清楚。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN_CONTRACT = ROOT / "packages" / "contracts" / "inference" / "plan-result.schema.json"
MIGRATIONS = ROOT / "infrastructure" / "migrations"
PRODUCTION_TREES = ("services", "packages")

#: 校验通过之后，生产代码真正从 `ValidatedPlan` 上读的那七个。
#:
#: 五个下标（`plan.document["..."]`）加两个 property
#: （`ValidatedPlan.strategy_id` / `.origin`，它们读的是 `self.document[...]`）。
#: **写成等值而不是「至少这几个」**：写成下界的话，
#: 一次读不出任何键的扫描会静悄悄通过（`AGENTS.md` 计数类断言写等值）。
READ_AFTER_VALIDATION = frozenset(
    {
        "candidate_intents",
        "generation_constraints",
        "origin",
        "required_slot_requests",
        "selected_strategy_id",
        "strategy_reason_code",
        "target_stack_operation",
    }
)

#: 本文件点名的那几个零读者字段 —— 两条偏移登记各自的那一个。
#: `confidence` 的三个子键是 B1-11，`manual_review_reason` 是 B1-12。
NEVER_READ = (
    "manual_review_reason",
    "calibrated_score",
    "assessment_version",
    "factor_codes",
)

#: **读点扫描器的非零对照。** 上面那几个断言的是「读点为 0」，
#: 而扫描器坏掉时每一个都是 0。这个键今天有真实读点（三处），
#: 它为 0 就说明扫描器坏了，不是业务变了。
READER_SCANNER_CONTROL = "required_slot_requests"

#: 装计划文档的表会叫什么。**按表名匹配，不按文件名** ——
#: 文件叫什么是作者的自由。
_PLAN_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(plan_results|plan_documents|inference_documents|inference_results)\b",
    re.IGNORECASE,
)


def _production_sources() -> list[Path]:
    files: list[Path] = []
    for tree in PRODUCTION_TREES:
        files += [
            path
            for path in (ROOT / tree).rglob("*.py")
            if "__pycache__" not in path.parts and ".venv" not in path.parts
        ]
    return sorted(files)


def _validated_plan_reads() -> dict[str, list[str]]:
    """从 `ValidatedPlan` 上读到的键 → 读它的文件。

    两种形态都要认，**而它们不能用同一条规则**：

    * `plan.document["x"]` —— 调用方那一侧，`plan` 这个名字就是判据；
    * `self.document["x"]` —— **只在 `ValidatedPlan` 这个类里面算**。
      仓里还有三个别的类也有 `self.document`（`model_policy` 的 release、
      `profiles/protocol` 的 identity、`retrieval` 的 embedding），
      不收窄的话会把它们的键一起数进来 ——
      那正是「`grep` 命中不等于有人在守」在这里的形态。
    """

    found: dict[str, list[str]] = {}

    def record(key: str, path: Path) -> None:
        found.setdefault(key, []).append(path.relative_to(ROOT).as_posix())

    def subscript_key(node: ast.AST, owner: str) -> str | None:
        if not isinstance(node, ast.Subscript):
            return None
        target = node.value
        if not (isinstance(target, ast.Attribute) and target.attr == "document"):
            return None
        base = target.value
        if isinstance(base, ast.Name) and base.id == owner:
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                return index.value
        return None

    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            key = subscript_key(node, "plan")
            if key:
                record(key, path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ValidatedPlan":
                for inner in ast.walk(node):
                    key = subscript_key(inner, "self")
                    if key:
                        record(key, path)
    return found


def _reads_of(key: str) -> list[str]:
    """全仓任何形态读这个键的地方（`["key"]` 或 `.get("key")`）。

    **比上一个函数宽得多，故意的**：`manual_review_reason` 那一条要答的是
    「**有没有任何人读它**」，不是「有没有人从 `ValidatedPlan` 上读它」。
    """

    hits: list[str] = []
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                index = node.slice
                if isinstance(index, ast.Constant) and index.value == key:
                    hits.append(path.relative_to(ROOT).as_posix())
            elif isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == key
                ):
                    hits.append(path.relative_to(ROOT).as_posix())
    return sorted(set(hits))


def test_the_reader_scanner_really_found_something() -> None:
    """**守卫的守卫**：下面几条断言的都是「读点为 0」，而扫描器坏掉时也是 0。"""

    control = _reads_of(READER_SCANNER_CONTROL)
    assert control, (
        f"`{READER_SCANNER_CONTROL}` 一处读点都找不到 —— 它今天是有真实读者的。"
        "读点扫描器坏了，下面那几条「零读者」的断言全都不成立"
    )

    reads = _validated_plan_reads()
    assert reads, (
        "从 `ValidatedPlan` 上一个读点都没扫到 —— AST 扫描坏了，"
        "下一条那个等值断言会变成「空集等于一个非空集」而红，"
        "但红的理由会指错地方，所以先在这里说清楚"
    )


def test_only_seven_keys_survive_validation() -> None:
    """**合同声明 13 个键，而校验之后只有七个被读。**

    这一条不是在要求「都得被读」—— 契约里本来就有只在校验期用的键
    （`scheduled_event_changes` 由 `_validate_scheduled_event_changes` 在**原始文档**
    上读，`schema_version` 由 `validate_contract` 读）。
    它要钉住的是**那七个的边界**：多一个，说明有人开始消费计划文档的更多部分，
    **而那通常意味着计划该落库了** —— 那一刻正是 T347 要人到场的时刻。
    """

    reads = _validated_plan_reads()
    assert set(reads) == set(READ_AFTER_VALIDATION), (
        # 1) 看到了什么
        f"从 `ValidatedPlan` 上读的键现在是 {sorted(reads)}，"
        f"此前是 {sorted(READ_AFTER_VALIDATION)}。"
        f"\n多出来的：{sorted(set(reads) - READ_AFTER_VALIDATION)}；"
        f"不见了的：{sorted(READ_AFTER_VALIDATION - set(reads))}。"
        # 2) 为什么这条要红
        "\n这七个键的边界就是「计划文档在这一轮之后还剩多少活着」——"
        "全仓没有一张表装它（下一条断言），所以不在这个集合里的键**没有可能的读者**。"
        # 3) 到期了该做什么
        "\n**如果是有人开始让计划落库了，那么 T347 到期了**："
        "去答「计划要不要落库」，并把 B1-11（confidence 三字段）与 "
        "B1-12（manual_review_reason）一起重判 —— 它们零读者的根就是这件事。"
    )


def test_the_two_registered_fields_still_have_no_reader() -> None:
    """**B1-11 与 B1-12 的直接断言：那四个字段今天一个读点都没有。**

    **这一条比上一条宽**：上一条只看 `ValidatedPlan`，这一条看全仓任何形态的
    `["x"]` 与 `.get("x")`。两条都要，因为它们答的是不同的问题 ——
    上一条问「计划文档还剩多少被消费」，这一条问「**这四个具体的字段有没有人读**」。

    **它红的时候不是坏了，是到期了**：有人给它们接上了读者。
    而那件事要么说明计划落库了，要么说明这几个字段被搬到了别处 ——
    两种都要回 T347 重判，不是把这条断言改宽。
    """

    with_readers = {key: _reads_of(key) for key in NEVER_READ}
    with_readers = {key: files for key, files in with_readers.items() if files}

    assert not with_readers, (
        f"这些字段有读点了：{with_readers}。"
        "\n它们此前零读者，而根因是计划文档不落库（本文件模块 docstring）——"
        "所以「有读者了」意味着根因变了。"
        "\n去 T347：答「计划要不要落库 / 这几个字段搬去哪」，"
        "然后把 B1-11 与 B1-12 从欠账清单上重判，再回来改这条断言。"
    )


def test_no_migration_builds_a_table_for_plan_documents() -> None:
    """**没有任何一条迁移建装计划文档的表。**

    手法与 `test_commitment_ledger_absence.py` 同源：按**表名**扫迁移目录，
    不按文件名 —— 文件叫什么是作者的自由。

    ⚠️ **它只逮得住「新建一张表」**。把计划塞进某张已有表的一个 jsonb 列，
    这一条不碰（模块 docstring 的「它守不住什么」那一节写了为什么本批不做）。
    """

    built = sorted(
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if _PLAN_TABLE.search(path.read_text(encoding="utf-8"))
    )
    assert not built, (
        f"迁移 {built} 建了装计划文档的表 —— T347 到期了。"
        "\n去把本文件那几条「零读者」断言重判（计划落库之后它们的根因就没了），"
        "并同时处理 B1-11 / B1-12 这两条偏移登记。"
    )


def test_the_contract_still_declares_more_than_is_read() -> None:
    """**差额本身要被钉住，否则「只读七个」会被读成「合同就七个」。**

    合同今天 **13 个** 顶层键（`additionalProperties: false`），读七个。
    这个差额不是缺陷 —— 有几个键本来就只在校验期用 —— **而它是 T347 的度量**：
    差额是 0 的那天，「计划文档不落库」这件事就不再有代价。
    """

    contract = json.loads(PLAN_CONTRACT.read_text(encoding="utf-8"))
    declared = set(contract["properties"])

    assert len(declared) == 13, (
        f"`plan-result` 合同现在声明 {len(declared)} 个键：{sorted(declared)}。"
        "此前是 13 个 —— 加了键就要问一句「这个键谁读」，"
        "答不出来的话它一出生就是 B1-11 / B1-12 那一族"
    )
    assert contract.get("additionalProperties") is False, (
        "`plan-result` 不再是封闭的了 —— 那么「合同声明了几个键」就不再可数，"
        "本条与上面那条的差额也就没有意义了"
    )
    assert READ_AFTER_VALIDATION < declared, (
        f"被读的那七个不再是合同的子集：{sorted(READ_AFTER_VALIDATION - declared)} "
        "不在合同里 —— 读点与合同分家了"
    )
