"""承诺台账的**三份副本**必须相等，写入方必须只有一处，而它必须接得到。

## 三份副本

| 在哪 | 形态 |
|---|---|
| `docs/rfcs/conversation-engine.md` §5 | 图纸的三个词表（散文 + yaml） |
| `packages/contracts/schemas/commitment-record.schema.json` | 契约的三个 `enum` |
| `infrastructure/migrations/063_commitments.sql` | 三列的 `CHECK (... IN (...))` |

**图纸↔契约那一半由 `test_commitment_record_contract.py` 钉**，
这里钉的是**契约↔迁移**那一半 —— 合起来三者一线。
写法与理由都照 `test_policy_output.py`（那边是 hook / effect 三份副本）：
**两边都可能是漂的那一头**，所以按等值集合判，不按包含。

## 为什么这份文件在批 11a 被退回之后才出现

11a 只交了契约，并请求专判一条豁免：「它今天没有消费方，而后继到期时会红的
守卫已经在了」。复核把后继的两半各换一种合法写法再做了一次，两半都漏：

* **表名换一个**（`commitment_book`）→ `test_commitment_ledger_absence` 那条
  按表名匹配的正则全绿；
* **消费方换本仓默认写法**（`validate_contract(...)` 而不是 `load_schema(...)`）
  → 全量一条不红。

用户 2026-09-16 因此裁定表与写入方同批。**于是那两个洞变成了另一问** ——
不再是「后继来了没有」，而是「**来了的这个接法对不对**」。下面这几条就是那一问。
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from pathlib import Path

import pytest
from fastagent_v2.runtime.contracts import load_schema
from fastagent_v2.worker.commitment_ledger import (
    NOT_A_CUSTOMER_TURN_REASON_CODE,
    admit_customer_commitments,
)
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
)
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "infrastructure" / "migrations" / "063_commitments.sql"
WRITER = ROOT / "services/worker/src/fastagent_v2/worker/commitment_ledger.py"
PIPELINE = ROOT / "services/worker/src/fastagent_v2/worker/pipeline.py"

#: 契约里那三个 `enum` 对应迁移里哪三列。
COLUMNS = {"party": "party", "type": "type", "status": "status"}


def _check_values(column: str) -> set[str]:
    """`063` 里某一列 `CHECK (col IN (...))` 的取值。

    **按 `commitments` 那个 `CREATE TABLE` 块取**，理由与
    `test_policy_output._migration_check` 逐字相同：在整份文件上跑的正则
    会拿到先出现的那一张表，而「先出现」不是判据。这里文件里只有一张表，
    仍然按块取 —— **因为下一条迁移可能不是**。
    """

    sql = MIGRATION.read_text(encoding="utf-8")
    start = re.search(r"CREATE TABLE(?: IF NOT EXISTS)? commitments\s*\(", sql)
    assert start, "063 里没有 `CREATE TABLE commitments`"
    rest = sql[start.end() :]
    end = re.search(r"\n\);", rest)
    assert end, "那个 CREATE TABLE 没有收尾"
    block = rest[: end.start()]
    match = re.search(
        rf"\b{column}\s+text\s+NOT\s+NULL\s+CHECK\s*\(\s*{column}\s+IN\s*\((?P<values>.*?)\)\s*\)",
        block,
        re.S,
    )
    assert match, f"`{column}` 那一列没有 CHECK —— 三份副本里少了一份"
    values = set(re.findall(r"'([a-z_]+)'", match.group("values")))
    assert values, f"`{column}` 的 CHECK 解析出空集 —— 解析器没读对"
    return values


def test_the_parser_really_read_the_migration() -> None:
    """**守卫的守卫**：先证明解析器读到了东西，再让它去判相等。

    一个回空集的解析器会让下面三条变成「空集等于空集」。
    这一条按**基数**验（4 / 7 / 9），而那三个数正是图纸给的 ——
    所以它同时是「迁移抄对了没有」的一次独立计数。
    """

    assert len(_check_values("party")) == 4
    assert len(_check_values("type")) == 7
    assert len(_check_values("status")) == 9


@pytest.mark.parametrize("field", sorted(COLUMNS))
def test_the_migration_check_matches_the_contract(field: str) -> None:
    """契约的 `enum` == 迁移的 `CHECK`，**两个方向**。

    契约多一个值而库不认：合同校验过了、Python 过了，`INSERT` 在运行时被数据库
    拒掉，**而报错指着的是数据库** —— 这是最难查的一种（`test_policy_output`
    那边逐字记过同一句）。库多一个而契约不认：库里能存下一个读出来过不了
    契约的值，**而校验发生在读路径上**（本轮刚为槽位值付过这笔账）。
    """

    contract = set(load_schema("commitment-record")["properties"][field]["enum"])
    assert contract == _check_values(COLUMNS[field]), (
        f"`{field}`：契约 {sorted(contract)}，迁移 {sorted(_check_values(COLUMNS[field]))}"
    )


def test_the_profile_commitment_types_are_the_ledger_types() -> None:
    """配包 `outcome_rules[].creates_commitment` 的词表 == 台账的 `type`。

    **第四份副本，本批同时定型的那一份**（`merchant-profile.schema.json`）。
    它今天零消费方，而**零消费方不等于可以漂**：配包作者读着它写包，
    而它与台账说的如果不是同一个七类，写出来的包在台账落地那天对不上。
    """

    profile_schema = json.loads(
        (ROOT / "packages/contracts/schemas/merchant-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    declared = set(
        profile_schema["properties"]["outcome_rules"]["items"]["properties"][
            "creates_commitment"
        ]["enum"]
    )
    assert declared == set(load_schema("commitment-record")["properties"]["type"]["enum"])


def test_the_understanding_candidate_type_is_the_ledger_type() -> None:
    """理解步候选的 `type` 词表 == 台账的 `type`。**第五份副本。**

    这一份是**写入侧**：模型报的那个值直接落进台账。两边不等时，
    一条合法的候选会在 `validate_contract` 那一步被拒 ——
    而那发生在回合中间，读起来像模型出错。
    """

    understanding = json.loads(
        (ROOT / "packages/contracts/inference/understanding-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = understanding["properties"]["customer_commitments"]["items"]
    assert "type" in candidate["required"], (
        "候选的 `type` 不再必填了 —— 那台账的 NOT NULL 就没有来源了"
    )
    assert set(candidate["properties"]["type"]["enum"]) == set(
        load_schema("commitment-record")["properties"]["type"]["enum"]
    )


def _sql_string_constants(path: Path) -> list[str]:
    """这份 `.py` 里的字符串常量，**排除 docstring**。

    与 `test_policy_output._writes_a_decision` 同一个判据、同一个理由：
    一条 SQL 是字符串常量，而讲这条 SQL 的话也是 —— 区别在位置上。
    那边是被一次**假红**逼出来的（一句引用纪律的话触发了纪律）；
    这里一开始就照着写，免得重演。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_there_is_exactly_one_insert_into_commitments() -> None:
    """**全仓只许一条 `INSERT INTO commitments`**，在写入方那个模块里。

    与 `policy_decisions` 那条纪律同源（`test_policy_output`）：
    第八处出现的方式不是有人存心绕开，**而是有人照着旁边那处抄**。
    台账比 `policy_decisions` 更该只有一处 —— 它有 `dedupe_key`，
    而第二处写入方多半不会知道有这回事。
    """

    statement = "INSERT INTO commitments"
    hits = sorted(
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "services", ROOT / "packages", ROOT / "scripts")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
        and any(statement in value for value in _sql_string_constants(path))
    )
    assert hits == [WRITER.relative_to(ROOT).as_posix()], (
        f"写 `commitments` 的地方是 {hits} —— 只该有写入方那一个模块；"
        " 新增的那处请改成调用 `write_admitted()`（它带 `dedupe_key` 与 `ON CONFLICT`）"
    )


def test_the_pipeline_actually_calls_the_writer() -> None:
    """`pipeline` 真的调得到写入方 —— **不是「导入了」，是「调了」**。

    批 11a 那条被推翻的豁免正是栽在这上面：一条只问「有没有人提到它」的守卫，
    答不了「那个人是不是真的在用它」。这里用 AST 数**调用**。

    两个函数各一次：`admit_customer_commitments`（算出收下哪些）与
    `write_admitted`（写进去）。**少任一个都不是一条通路** ——
    只算不写是把判定丢掉，只写不算写的是空。
    """

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    missing = {"admit_customer_commitments", "write_admitted"} - called
    assert not missing, (
        f"`pipeline` 没有调用 {sorted(missing)} —— 台账那条通路断在这里。"
        " 断了的话，理解步照样提取候选、契约照样合法，而库里一行都不会有。"
    )


# ---------------------------------------------------------------------------
# 下面三条是**反向探针找出来的**：本批第一轮探针里，它们各自对应的那个变异
# 全绿 —— 也就是说这三件事当时一条守卫都没有。
#
# 三件里有两件是本仓这一轮刚付过学费的形状（极性、无约束词表），
# 第三件是用户 2026-09-16 的裁定本身。**裁定也要有守卫，否则它只是一句话。**
# ---------------------------------------------------------------------------


def _event_types() -> list[str]:
    """`event_type` 的值域，**从契约读**。

    手抄一份正是 `test_remember_write_path` 那条守卫失效的原因
    （抄的四个名字里一个不在 enum 里，enum 里真有的一个不在清单里，
    于是它迭代的集合恰好等于反向列举会列的集合）。
    """

    schema = json.loads(
        (ROOT / "packages/contracts/schemas/message-envelope.schema.json").read_text(
            encoding="utf-8"
        )
    )
    return [str(value) for value in schema["properties"]["event_type"]["enum"]]


@pytest.mark.parametrize("event_type", _event_types())
def test_only_a_customer_message_turn_admits_a_customer_commitment(event_type: str) -> None:
    """**只有客户回合能记下客户承诺** —— 判据正向，不是反向列举。

    本批的确认闸第一版写的是 `!= "customer_message"`（正向），而**没有任何
    用例钉住它**：反向探针把它改写成列举那四个非客户值，全量 1,680 条一条不红。

    这与 D4 的安全回退、`_remember_turn_refusal` 那道闸是**同一个形状的第三次**，
    而前两次都是别人写的、本轮才修的。第三次是我自己写的 —— 所以这条用例
    与它们一样，**值域从契约读**，契约里新增一个 event_type 它自动跑上。
    """

    candidate = {
        "summary": "客户说下周三之前把材料发过来",
        "type": "take_action",
        "due_at": None,
        "evidence_event_id": str(uuid.uuid4()),
        "confidence": 0.9,
    }
    rows, refusals = admit_customer_commitments(
        understanding={"customer_commitments": [candidate]},
        event_type=event_type,
        source_event_id=str(uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        generation=0,
    )

    if event_type == "customer_message":
        assert len(rows) == 1 and not refusals
        assert rows[0]["party"] == "customer"
        assert rows[0]["status"] == "proposed"
    else:
        assert not rows, (
            f"`{event_type}` 回合上记下了一条客户承诺 —— 那一轮客户没有说话。"
            " 判据要正向写（`== \"customer_message\"`），不是列举要拒的那几种"
        )
        assert [code for code, _ in refusals] == [NOT_A_CUSTOMER_TURN_REASON_CODE], (
            "拒掉了而没留痕 —— 一条没被记下来的承诺，在台账上与「客户没说过」一样"
        )



def _carrier_profile() -> dict[str, object]:
    return json.loads(
        (ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def _aggregate_for_advance_goal() -> dict[str, object]:
    """一个够 `DeterministicMockInference` 用的聚合，**照真实形状**。"""

    profile = _carrier_profile()
    definition = next(
        frame for frame in profile["target_frames"] if frame["id"] == "welcome"
    )
    return {
        "id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "state_version": 1,
        "generation": 0,
        "profile_release_id": str(uuid.uuid4()),
        "target_stack": [
            {
                "id": str(uuid.uuid4()),
                "definition_id": "welcome",
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
                "entered_at": "2026-09-01T00:00:00Z",
                "last_progress_at": "2026-09-01T00:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": definition["return_confirmation"]["policy"],
                "frame_generation": 0,
            }
        ],
    }


def test_advance_goal_is_bound_by_no_new_commitment_even_when_the_profile_is_silent() -> None:
    """**T332（用户 2026-09-16 裁定）：`advance_goal` 上这一条由平台无条件施加。**

    图纸 §4.1 那句是**这条策略的性质**，不是配包可以选的一个选项。
    现读三份在架配包的 `strategies["advance_goal"].generation_constraints`：
    `internal-demo-sales` 有 `no_new_commitment`，另两份**没有**。

    这条用例给的计划**明确不声明任何约束** —— 也就是那两份配包的处境 ——
    而它仍然必须被拒。**反向探针实测：把那两行平台强制去掉，全量一条不红。**
    一条没有守卫的裁定，与没有裁定过是同一件事。
    """

    # **生成结果用生产的那个构造器造，不手搭**（本仓记过「自造夹具藏得住
    # 形状不匹配」）：`validate_generation` 先过一遍 `generation-result` 契约，
    # 手写的那一份第一次就没过 —— 而那种红指着的是夹具，不是被测的东西。
    inference = DeterministicMockInference()
    state = _aggregate_for_advance_goal()
    understanding = inference.understand(
        envelope={
            "event_id": str(uuid.uuid4()),
            "event_type": "customer_message",
            "content": {"type": "text", "text": "hello"},
        },
        aggregate=state,
        profile=_carrier_profile(),
    )
    planned = inference.plan(
        understanding=understanding, aggregate=state, profile=_carrier_profile()
    )
    generation = inference.generate(
        plan=planned,
        understanding=understanding,
        # `generate` 读 `envelope["event_type"]` —— 给它真实的那一个，
        # 而不是一个空 dict（第一版就是，`KeyError` 当场拦住）。
        envelope={"event_type": "customer_message"},
        materials={},
    )

    # **计划一个约束都不声明** —— 也就是 `synthetic-carrier` / `synthetic-third`
    # 的 `advance_goal` 今天的处境。
    planned["selected_strategy_id"] = "advance_goal"
    planned["generation_constraints"] = []
    plan = ValidatedPlan(document=planned, frame={}, definition={})
    # 生成结果也要报同一条策略，否则先撞上 `GENERATION_CHANGED_STRATEGY`，
    # **而那一红指的是另一件事**（红得对不对，要看它红在什么上）。
    generation["strategy_id"] = "advance_goal"
    generation["safety_markers"] = ["contains_commitment"]

    with pytest.raises(GenerationRejected) as rejection:
        validate_generation(generation=generation, plan=plan)
    assert rejection.value.reason_code == "UNREGISTERED_COMMITMENT", (
        "计划一个约束都没声明，而 `advance_goal` 仍然不许构造未登记承诺 ——"
        " 那是图纸那句话的意思，不是配包的一个开关"
    )


@pytest.mark.parametrize(
    ("label", "row"),
    [
        ("少一个必填键", {"party": "ai", "creates_commitment": "take_action"}),
        (
            "creates_commitment 不在词表里",
            {
                "strategy": "advance_goal",
                "party": "ai",
                "creates_commitment": "make_something_up",
                "fulfilment": "same_turn",
            },
        ),
        (
            "多一个键",
            {
                "strategy": "advance_goal",
                "party": "ai",
                "creates_commitment": "take_action",
                "fulfilment": "same_turn",
                "extra": "多传一个",
            },
        ),
    ],
    ids=["missing", "unknown-value", "extra-key"],
)
def test_a_malformed_outcome_rule_is_refused(label: str, row: dict[str, str]) -> None:
    """`outcome_rules` 定型之后**真的会拒** —— 不是「在架三份包恰好合法」。

    本批把它从 `{"type": "object"}` 定成了带三个词表的结构。
    **反向探针把它退回无约束，全量一条不红** —— 因为在架那三份配包在松的
    schema 下同样合法。**「今天没人违反」不等于「有东西在拦」**，
    而这正是 `CLAUDE.md` 第三节那句：多传一个不报错，安静地给出一个
    看起来正常的结果。

    三支各打一种走法：少一个键、值不在词表里、多一个键。
    """

    schema = json.loads(
        (ROOT / "packages/contracts/schemas/merchant-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema["properties"]["outcome_rules"])
    errors = list(validator.iter_errors([row]))
    assert errors, f"{label}：这一行过了 —— 那 `outcome_rules` 的定型没有在拦任何东西"
