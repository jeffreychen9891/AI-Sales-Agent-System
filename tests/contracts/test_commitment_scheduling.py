"""承诺 → 定时器那条路：**每条承诺一个定时器，而它挂的是库里那一行。**

## 这份文件钉的四件

1. **写入方真的填 `commitment_id`** —— T138 关闭的依据就是这一句，
   而一句「已执行」的断言必须配一条失效时会红的守卫（`CLAUDE.md` 第二节）；
2. **定时器的 id 来自库，不来自内存那一份** —— 这一条是结构断言，
   理由在 `commitment_ledger.stored_due_rows` 的 docstring 里：
   重放那一轮内存里的 id 在库里不存在，而 `064` 的外键会拒掉整条事务；
3. **在架配包的两条承诺规则带得动第三段** —— 不带的话生产方跳过它们；
4. **同一帧两条承诺渲染出两个键** —— 这一批存在的全部理由，
   正面写一遍（负面那一半在 `test_time_rule_producer.py`）。

第 1、2 条是**源码结构**断言。它们不好看，而替代品更糟：
这条路要真跑起来需要一个真库 + 一次完整回合，那一层的用例
（`tests/integration/test_scheduled_event_commitment_fk.py`）验的是**外键**，
验不到「谁传了什么给谁」。
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.worker.pipeline import KIND_BY_PARTY, PreparedEvent, _commitment_drafts
from fastagent_v2.worker.time_rules import render_dedupe_key

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "pipeline.py"
FRAME_ID = UUID("f0000000-0000-4000-8000-00000000f001")


def _pipeline_source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def test_the_scheduled_events_writer_names_the_commitment_column() -> None:
    """**T138 那句「一根悬空的列」今天不成立，而这条用例是它的凭据。**

    `scheduled_events.commitment_id` 从 `001_m0.sql:143` 起存在，
    在 11d 之前**没有任何写入方** —— 连列清单里都没有它。
    在这种列上立外键是一条今天不可能被违反的约束（ADR-0139 §三 因此把它排在最后）。

    判据是「那条 `INSERT` 的列清单里有它」，**不是「文件里出现过这个词」**：
    后者在注释里也算数，而注释写不进数据库。
    """

    source = _pipeline_source()
    start = source.index("INSERT INTO scheduled_events(")
    columns = source[start : source.index(")", start)]

    assert "commitment_id" in columns, (
        "`INSERT INTO scheduled_events` 的列清单里没有 commitment_id —— "
        "那根列又变回没有写入方的状态，而 064 的外键会因此变成一条恒绿的约束。"
    )


def test_the_timer_hangs_off_the_row_the_database_has() -> None:
    """**定时器上的承诺 id 来自 `stored_due_rows`，不来自 `admitted_commitments`。**

    `admit_customer_commitments` 每轮给每条候选现生成一个 `uuid4()`，而去重靠
    `(tenant_id, conversation_id, dedupe_key)`：**重放同一条入站事件时**
    `ON CONFLICT DO NOTHING` 什么都不写，内存里那一份带着一个库里不存在的 id。

    拿它建定时器，外键会拒掉整条事务 —— 一次本该幂等的重放变成死循环。
    所以这里钉的是**调用的形状**：`_commitment_drafts` 的第二个实参必须是
    一次 `stored_due_rows(...)` 调用。
    """

    tree = ast.parse(_pipeline_source())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_commitment_drafts"
    ]

    assert calls, "没有任何地方调用 _commitment_drafts —— 承诺那一族定时器没人建"
    for call in calls:
        assert len(call.args) == 2, (
            f"_commitment_drafts 的调用形状变了：{len(call.args)} 个位置参数"
        )
        source_of_rows = call.args[1]
        assert (
            isinstance(source_of_rows, ast.Call)
            and isinstance(source_of_rows.func, ast.Name)
            and source_of_rows.func.id == "stored_due_rows"
        ), (
            "传给 _commitment_drafts 的那一份不是 stored_due_rows(...) 读回来的。"
            " 内存里那份行在重放那一轮带着库里不存在的 id —— 064 的外键会拒掉整条事务。"
        )


def test_the_shipped_commitment_rules_can_tell_two_commitments_apart() -> None:
    """在架配包里每一条 `commitment_due_at` 规则都带 `{commitment_id}`。

    不带的话生产方会跳过它（不是报错 —— 报错会让 11d 之前发布的 release
    每轮 500，那是对已存数据的追溯收紧）。跳过是安静的，
    所以在架那几份必须在这里被钉住。
    """

    seen = 0
    for path in sorted((ROOT / "profiles").glob("*/profile.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        for frame in profile.get("target_frames", []):
            for rule in frame.get("time_rules", []):
                if (rule.get("due") or {}).get("anchor") != "commitment_due_at":
                    continue
                seen += 1
                assert "{commitment_id}" in str(rule.get("dedupe_template") or ""), (
                    f"{path.parent.name} 的 {rule.get('id')} 锚在 commitment_due_at 而模板"
                    " 认不出两条承诺 —— 这条规则会被生产方静默跳过，永远不会有人收到它。"
                )

    # **把「今天有几条」显式写下来**（T364，2026-09-19 加）。
    #
    # ⚠️ 这一行的由来值得记：我先前**量错了一次**，用
    # `rule["anchor"]` 去数（而锚点嵌在 `rule["due"]["anchor"]` 里），
    # 得到「零规则」，并差一点据此写下一条「这条用例是空的」的结论。
    # **实测是 2 条**，都在 `synthetic-carrier` 的 `rental` 帧上。
    #
    # 所以这一行钉的不是「它非空」——它本来就非空——
    # 而是**那个数**：`{commitment_id}` 那条断言到底跑到了几条规则上。
    # 加了规则要把这个数改过来，掉到 0 则说明有人把规则删了，
    # 而那时上面那条断言会静默地什么都不查。
    # **2026-09-21：2 -> 6。** `internal-demo-sales` 的两个帧各加了
    # `customer_promise_due` / `ai_commitment_due` 一对 —— 在此之前
    # 这份包一条承诺规则都没有，T364/T365 刚接上的锚点它一格没用。
    # **2026-09-21 拆帧：6 -> 12。** 五个帧里四个业务帧各带一对
    # `customer_promise_due` / `ai_commitment_due`，加 `synthetic-carrier` 原有两条。
    assert seen == 7, (
        f"在架配包现在有 {seen} 条 commitment_due_at 规则"
        "（2026-09-24 K4 批删掉 demo 五条死 ai_commitment_due 后实测 7："
        "demo 5 条 customer_promise_due + carrier 一对）—— "
        "把这个数改过来，并核一遍上面那条 `{commitment_id}` 断言真的跑到了它们"
    )


def test_two_commitments_in_one_frame_get_two_keys() -> None:
    """**这一批存在的理由，正面一句。**

    11d 之前这两条渲染出同一个键，而写入方是
    `ON CONFLICT ... DO UPDATE SET due_at = GREATEST(...)`：
    第二条不建新行，它把第一条的死线往后推。
    """

    template = "{emit_kind}:{target_frame_id}:{commitment_id}"
    keys = {
        render_dedupe_key(
            template,
            emit_kind="customer_promise_due",
            target_frame_id=FRAME_ID,
            commitment_id=UUID(int=index),
        )
        for index in (1, 2)
    }

    assert len(keys) == 2, f"两条承诺渲染出同一个键：{keys}"


# ---------------------------------------------------------------------------
# 五 · 一条承诺只实现它自己那一党的那种 kind（T364，2026-09-19）
# ---------------------------------------------------------------------------

_ANCHOR_FRAME = UUID("f0000000-0000-4000-8000-00000000f002")


def _both_kinds_profile() -> dict[str, Any]:
    """一份在**同一个帧**上同时声明两种 `emit_kind` 的配包。

    ⚠️ **这不是一份奇怪的配包** —— `commitment_due_at` 这个锚点上两种
    `emit_kind` 都合法（schema 的枚举里都有），而一个既催客户也催自己的商家
    正好两条都要声明。**同一帧两条是这道闸唯一能被观察到的形状。**
    """

    def rule(kind: str) -> dict[str, Any]:
        return {
            "id": f"welcome-{kind}",
            "emit_kind": kind,
            "due": {"anchor": "commitment_due_at", "offset_seconds": 60},
            "dedupe_template": "{emit_kind}:{target_frame_id}:{commitment_id}",
            "cancel_on": ["customer_message"],
            "strategy": "advance_goal",
        }

    return {
        "target_frames": [
            {
                "id": "welcome",
                "goal": "greet",
                "time_rules": [rule("customer_promise_due"), rule("ai_commitment_due")],
            }
        ]
    }


def _prepared(profile: dict[str, Any]) -> Any:
    """`_commitment_drafts` 只读 `prepared` 的两个字段，所以给它一个替身。

    **用 `PreparedEvent` 真的造一个**，而不是一个 `SimpleNamespace`：
    那个 dataclass 哪天多一个必填字段，这里会红而不是静默地继续测一个
    已经不存在的形状（自造夹具藏得住形状不匹配）。
    """

    return PreparedEvent(
        inbox_id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        envelope={"event_id": str(uuid4()), "event_type": "customer_message"},
        profile=profile,
        previous_state_version=1,
        previous_generation=3,
        aggregate={
            "id": str(uuid4()),
            "last_inbound_at": None,
            "target_stack": [
                {
                    "id": str(_ANCHOR_FRAME),
                    "definition_id": "welcome",
                    "status": "active",
                    "entered_at": "2026-09-19T10:00:00+00:00",
                    "slots": {},
                }
            ],
        },
    )


def _kinds(party: str) -> list[str]:
    row = {
        "id": str(uuid4()),
        "due_at": datetime(2026, 9, 19, 11, 0, tzinfo=UTC),
        "party": party,
    }
    return [draft.kind for draft in _commitment_drafts(_prepared(_both_kinds_profile()), [row])]


def test_an_ai_commitment_does_not_materialise_the_customer_rule() -> None:
    """**我们自己欠的东西，不该排出一条「催客户」的定时器。**

    在这道闸之前：那一行写得进、`064` 的外键过得去、`commitment_id` 也是真的，
    **只有 `kind` 是错的** —— 到期之后跑的是催客户那条路，而欠东西的是我们。
    **没有任何东西会红**，这正是 `CLAUDE.md` 第三节那个形状。

    ⚠️ **两个方向都要**：只断言 AI 那边，一道「一律只出 `ai_commitment_due`」
    的实现也能全绿 —— 而那会把客户侧那一族整个吞掉。
    """

    assert _kinds("ai") == ["ai_commitment_due"]
    assert _kinds("customer") == ["customer_promise_due"]


def test_a_party_with_no_kind_of_its_own_schedules_nothing() -> None:
    """**认不出党派就一条都不实现**（fail-closed）。

    `commitments.party` 的枚举有四个值，而 `emit_kind` 里只有两个与承诺对应。
    `employee` / `external` 今天没有写入方 ——
    **而给它们默认一种 kind 等于替商家决定「员工欠的东西到期该催谁」。**
    """

    for party in ("employee", "external", "", "AI"):
        assert _kinds(party) == [], f"{party!r} 排出了定时器，而它没有对应的 kind"


def test_the_party_to_kind_table_covers_exactly_the_two_that_have_a_kind() -> None:
    """**那张表与 `emit_kind` 的枚举对得上，而且只对得上那两个。**

    判据从两份 schema 现取：`commitments.party` 的枚举（四个）
    与 `scheduled-event` 的 `kind` 枚举。**表里写了一个枚举外的 kind**
    就是一条写得出、排得出、而数据库当场拒掉的定时器。
    """

    kinds = set(
        json.loads(
            (
                ROOT
                / "packages"
                / "contracts"
                / "schemas"
                / "scheduled-event.schema.json"
            ).read_text(encoding="utf-8")
        )["properties"]["kind"]["enum"]
    )
    parties = set(
        json.loads(
            (
                ROOT
                / "packages"
                / "contracts"
                / "schemas"
                / "commitment-record.schema.json"
            ).read_text(encoding="utf-8")
        )["properties"]["party"]["enum"]
    )

    assert set(KIND_BY_PARTY) <= parties, (
        f"表里有不是党派的键：{sorted(set(KIND_BY_PARTY) - parties)}"
    )
    assert set(KIND_BY_PARTY.values()) <= kinds, (
        f"表里有不是 kind 的值：{sorted(set(KIND_BY_PARTY.values()) - kinds)}"
    )
    # **少写一个才是危险的那一侧**：那一党会静默地一条定时器都不建。
    assert set(KIND_BY_PARTY) == {"customer", "ai"}, (
        f"表覆盖的党派变成了 {sorted(KIND_BY_PARTY)} —— "
        "加进来的那一党要有人判「它欠的东西到期该催谁」，别默认"
    )
