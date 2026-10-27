"""T368：速率闸数的是**轮**，不是**条**。

## 裁定，原话

> **速率闸是用来防止骚扰而不是正常对话。**（用户 2026-09-19）

以及两个数：**一分钟 5 轮**，**超额的走批 F 丢弃**。

## 它修的是什么

批 G 把「许诺一条 + 紧跟兑现一条」变成有意为之的形状。而速率闸按**条数**
截断（`accepted[:room]`）：这通对话一分钟内已发过 2 条时 `room = 1`，
**许诺那条留下、兑现那条被扣住** —— 客户看到一句光秃秃的许诺。

⚠️ **批 F 那道闸够不着它**：闸跑在**生成时**，那一刻两条都在、判「有东西出去」
放行。劈开发生在之后的投递编排里，**没有任何东西会在那一刻变红**。

## 分工：哪一半在这里，哪一半在 e2e

* **这里**（不需要库）：轮次键那两条纪律、会话那一边的原子性判定
  （用假 connection 直调闸）、劈开之后那一遍丢弃、以及接线；
* **`tests/e2e/test_rate_gate.py`**（需要库）：两边额度的算术走真链路。

⚠️ **劈开之后那一遍丢弃没有 e2e 覆盖**，写明原因：要造它得让租户额度只剩一个
名额、**同时**让那一轮第一条真的是一句许诺 —— 后者要一个新的推理替身
（在架那个 `TwoSendsInference` 两条都不是许诺）。
**登记在 T368 的状态栏里，而不是留成沉默。**
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.pipeline import (
    GENERATION_WRITE_PATHS,
    RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE,
    RATE_LIMIT_TENANT_PER_MINUTE,
    TURN_KEY_SQL,
    InboxProcessor,
    OutboxDelivery,
    ProposalDecision,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)

#: 逐字取自运维 2026-09-18 `06:31:11` 的实测出站后半 —— 一句许诺。
THE_PROMISE = "那我先把试用怎么开、您那边要准备什么讲清楚。"

#: 兑现那一条：商家审过的原文，由发送路径逐字换上。
MATERIAL_REF = "material:demo-trial-how:v1"


class _Counts:
    """假 connection：只回那一条计数查询。

    **审查方 2026-09-12 用过同一个手法**（`_rate_gate` 的 P1-1 那两支探针）——
    这道闸除了那一次 `execute` 之外不碰库，所以一个只会回计数的替身
    就能把它的判定逼出来，而不必起一个事务。
    """

    def __init__(self, *, turns: int, messages: int) -> None:
        self.turns = turns
        self.messages = messages
        self.sql = ""

    def execute(self, sql: str, _params: Any = None) -> _Counts:
        self.sql = sql
        return self

    def fetchone(self) -> dict[str, int]:
        return {"turns_in_conversation": self.turns, "in_tenant": self.messages}


def _prepared() -> Any:
    return SimpleNamespace(conversation_id=uuid4(), tenant_id=uuid4())


def _message(*segments: dict[str, str]) -> dict[str, Any]:
    return {
        "kind": "send_message",
        "typed_payload": {"content": {"segments": [dict(s) for s in segments]}},
        "evidence_refs": ["event:00000000-0000-4000-8000-000000000000"],
    }


def _text(value: str) -> dict[str, str]:
    return {"type": "text", "content_or_ref": value}


def _decision(ordinal: int, proposal: dict[str, Any]) -> ProposalDecision:
    return ProposalDecision(
        ordinal=ordinal,
        kind="send_message",
        accepted=True,
        reason_code="ACCEPTED",
        detail="",
        proposal=proposal,
    )


def _gate(*, turns: int, messages: int, sends: int) -> tuple[Any, Any, str | None]:
    accepted = [_decision(index, _message(_text("x"))) for index in range(sends)]
    return InboxProcessor._rate_gate(  # noqa: SLF001 - 被测对象就是它
        None,  # type: ignore[arg-type] - 这道闸不用 self
        _Counts(turns=turns, messages=messages),
        _prepared(),
        accepted,
    )


# ---------------------------------------------------------------------------
# 一 · 轮次键：它靠两件事，两件都钉住
# ---------------------------------------------------------------------------


def test_the_turn_key_reads_the_source_event_out_of_the_idempotency_key() -> None:
    """一轮的每一行共享 `idempotency_key` 的**中段**（那一轮的源事件 id）。

    构造点只有一处（`_persist_accepted_proposal`），形状是
    `{prefix}:{event_id}:{candidate_basis}` —— 所以第 2 段就是轮次。
    """

    assert TURN_KEY_SQL == "split_part(idempotency_key, ':', 2)"

    source = PIPELINE.read_text(encoding="utf-8")
    assert (
        'f"{GENERATION_WRITE_PATHS[kind].idempotency_prefix}:{event_id}:{candidate_basis}"'
        in source
    ), "出站行的幂等键不再是 `前缀:事件:基准` 那个形状 —— 轮次键读的就是中间那一段"


def test_the_counting_query_counts_distinct_turn_keys() -> None:
    """⚠️ **这一条补的是本文件自己的一个洞。**

    下面那几支原子性用例喂的是**假 connection 的计数** ——
    它们逼得出闸的判定，**而那条 SQL 数的是什么，它们一个字都看不见**。
    把 `count(DISTINCT 轮次键)` 改回 `count(*)`，那几支照样全绿。

    所以这里按字面钉住那条计数表达式。
    ⚠️ **扫的是字面量**：它证明查询里写着按轮次键去重，
    **不证明数据库真这么数** —— 那一半在 `tests/e2e/test_rate_gate.py` 里走真链路。
    """

    source = PIPELINE.read_text(encoding="utf-8")
    # **钉的是占位符，不是展开后的字面量** —— 那证明查询用的是
    # `TURN_KEY_SQL` 这个常数本身，而不是又抄了一份表达式。
    # 抄一份的那种写法，改了常数而查询不动时是绿的。
    assert "count(DISTINCT {TURN_KEY_SQL}) FILTER (WHERE conversation_id = %s)" in source, (
        "会话那一边不再按轮次键去重 —— 它又在数条数了，或者把表达式抄了一份"
    )
    assert "AS turns_in_conversation" in source, (
        "那一列的名字还叫别的 —— 名字是读它的人唯一的单位提示"
    )


@pytest.mark.parametrize("name", sorted(GENERATION_WRITE_PATHS))
def test_no_write_path_prefix_contains_a_colon(name: str) -> None:
    """⚠️ **前缀里一个冒号就会把轮次键读歪。**

    `split_part(key, ':', 2)` 取第 2 段。前缀带冒号时第 2 段变成前缀的后半 ——
    **那一段在同一种 kind 的所有行上都一样**，于是一整分钟的回合
    会塌成一轮，而闸**放更多消息出去**（fail-open）。

    这条用例是那件事的守卫：加一个带冒号的前缀就红。
    """

    assert ":" not in GENERATION_WRITE_PATHS[name].idempotency_prefix, (
        f"写入路径 {name!r} 的前缀带冒号 —— 轮次键会读到前缀的后半，"
        "而那一段在所有行上相同：一整分钟的回合会塌成一轮"
    )


def test_every_other_outbox_writer_is_not_a_customer_row() -> None:
    """⚠️ **另一半前提**：客户可见的出站行只有那三条写入路径写。

    别的写入方（管理员通知、摘要）用的是别的键形状
    （`manager_notice:{reason_code}:{event_id}` —— **事件 id 在第 3 段**），
    所以它们要是算进窗口，轮次键读到的会是**理由码**，
    而理由码在许多行上相同 —— 又是塌成一轮。

    它们今天被 `AUDIENCE_IS_CUSTOMER_SQL` 滤掉，因为写的是
    `audience: tenant_manager`。**这条用例钉住那件事**。

    ⚠️ **扫的是源码字面量**（本仓「源码扫描看不见代码跑没跑」）：
    它证明那两处写的是 `tenant_manager`，**不证明运行时没有第三种写入方**。
    更强的形式要在库上按 `audience` 分组数一遍，那需要 e2e 的夹具。
    """

    for path in (
        PIPELINE,
        REPOSITORY_ROOT
        / "services/worker/src/fastagent_v2/worker/digest.py",
    ):
        source = path.read_text(encoding="utf-8")
        for index, block in enumerate(source.split("INSERT INTO outbox_messages")[1:]):
            window = block[:2000]
            if "manager_notice" in window or "tenant_manager" in window:
                continue
            assert "GENERATION_WRITE_PATHS" in window or "idempotency_key" in window, (
                f"{path.name} 第 {index + 1} 处 INSERT 既不是管理员行、"
                "也看不出用的是共享那把键 —— 它会被算进窗口而轮次读不出来"
            )


# ---------------------------------------------------------------------------
# 二 · 会话那一边：单位是轮，而一轮是原子的
# ---------------------------------------------------------------------------


def test_a_turn_under_the_limit_passes_whole() -> None:
    """额度没满 —— 这一轮两条都过，`scope` 是 `None`（闸没咬）。"""

    kept, withheld, scope = _gate(
        turns=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - 1, messages=2, sends=2
    )

    assert len(kept) == 2
    assert withheld == []
    assert scope is None


def test_a_full_window_withholds_the_whole_turn() -> None:
    """会话额度满了 —— 这一轮**整个**不发。

    ⚠️ **这一条不能证明原子性，而它先前的名字声称它能**（叫
    `..._never_splits_a_turn`）。探针 B 当场证明了那件事：
    把实现改回「按名额截断」，**这一条照样绿** ——
    额度正好满时两种算法都是 `room = 0`，行为逐字相同。

    **真正证明原子性的是下面那两条「还没满」的**（`..._passes_whole` 与
    `..._still_goes_out_whole`）：按名额算时它们当场红。
    本仓「夹具不能恰好等于错误答案」那一族的近亲 ——
    **用例站的位置决定它量得到什么，而不是它的名字。**
    """

    kept, withheld, scope = _gate(
        turns=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE, messages=2, sends=2
    )

    assert kept == [], "会话额度满了还发出去了东西 —— 一轮不是原子的"
    assert len(withheld) == 2, "一轮里只有一部分被扣住 —— 那就是劈开"
    assert scope == "conversation"


def test_the_last_turn_in_the_window_still_goes_out_whole() -> None:
    """还剩一轮时，那一轮的两条**都**出去 —— **这一条才是原子性的证据**。

    探针 B（把实现改回按名额截断）红的正是这一条与 `..._passes_whole`：
    还剩一轮而按条数算只剩一个名额时，旧实现会**留下第一条、扣住第二条** ——
    许诺留下、兑现被扣住，客户看到一句光秃秃的许诺。

    ⚠️ 只钉「满了不发」那一边的话，一个「凡是两条一律不发」的实现能拿满分，
    而按名额截断的那个旧实现也能。
    """

    kept, withheld, _ = _gate(
        turns=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - 1, messages=0, sends=2
    )

    assert len(kept) == 2
    assert withheld == []


def test_the_tenant_limit_is_atomic_too() -> None:
    """⚠️ **2026-09-23（ADR-0151）：租户这一边也按轮、也原子。**

    在此之前它数条数、而且会**把一轮从中间切开** —— 前几条发出去、
    后几条丢掉。这条用例先前钉的正是那个行为，docstring 逐字写着
    「它是劈开唯一的可达路径」。

    **用户 2026-09-23 裁定「租户也要一起改」**，依据是分段：
    对两条独立消息，切开只是难看；**对一条分了段的消息，那是半句话** ——
    「因为…」发出去了，「所以…」被砍掉。

    ⚠️ **连带后果，一并记在这里**：`kept` 与 `withheld` 从此不可能同时非空，
    于是 `_drop_promises_the_split_no_longer_keeps` 成了够不着的分支，
    连同它的三条用例一起删了。**留着就是一道守不住任何东西的闸** ——
    而那句话正是这条用例先前的 docstring 自己写的。
    """

    # **还剩一个名额、而这一轮要发两条** —— 先前这里是「发一条、丢一条」。
    kept, withheld, scope = _gate(
        turns=0, messages=RATE_LIMIT_TENANT_PER_MINUTE - 1, sends=2
    )
    assert len(kept) == 2, "一轮被切开了 —— 分段消息在这条路上会变成半句话"
    assert withheld == []
    assert scope is None

    # ⚠️ **代价就在上面那三行里，写明**：租户闸从此更容易被一轮撑过线，
    # 一轮算一个单位而它可能带 8 行。依据是用户 2026-09-19
    # 「速率闸是用来防止骚扰而不是正常对话」。
    #
    # 咬住的时候整轮一起扣：
    kept, withheld, scope = _gate(
        turns=0, messages=RATE_LIMIT_TENANT_PER_MINUTE, sends=2
    )
    assert kept == []
    assert len(withheld) == 2, "整轮要一起扣住"
    assert scope == "tenant"


def test_neither_limit_can_split_a_turn() -> None:
    """**两道闸都原子** —— 任何一道咬住时，`kept` 必须是空的。

    这一条是上面那条的推广，而它守的是那个被删掉的分支不要回来：
    一旦有人让某一道闸重新「放一部分」，`kept` 与 `withheld` 会再次同时非空，
    而重判许诺的那段代码已经不在了 —— **许诺句会就这么发出去而兑现物不出去**。
    """

    for turns, messages in (
        (RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE, 0),
        (0, RATE_LIMIT_TENANT_PER_MINUTE),
        (RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE, RATE_LIMIT_TENANT_PER_MINUTE),
    ):
        kept, withheld, scope = _gate(turns=turns, messages=messages, sends=3)
        assert scope is not None
        assert kept == [], f"turns={turns} messages={messages} 时放了 {len(kept)} 条出去"
        assert len(withheld) == 3


def test_the_conversation_limit_wins_a_tie() -> None:
    """两边同时满时报 `conversation` —— 它是更窄、更可行动的那句话。

    （先前是一个 `min()` 加一个三元，今天是两个先后判断 ——
    这条用例钉住那个先后没被换。）
    """

    _kept, _withheld, scope = _gate(
        turns=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE,
        messages=RATE_LIMIT_TENANT_PER_MINUTE,
        sends=2,
    )

    assert scope == "conversation"


# ---------------------------------------------------------------------------
# 三 · 超额的走批 F 丢弃
# ---------------------------------------------------------------------------


def _generation(*, sourced_claim: bool = False) -> dict[str, Any]:
    return {
        "claims": (
            [
                {
                    "text_hash": hashlib.sha256(b"deploy").hexdigest(),
                    "source_evidence_refs": ["knowledge:deploy:1"],
                }
            ]
            if sourced_claim
            else []
        )
    }


def test_the_pipeline_actually_asks_for_this() -> None:
    """闸之后真的调了那一遍丢弃，**而且在记那条审计行之前**。

    顺序反过来，审计行里那份 `promises_dropped` 永远是空的 ——
    **两头都在、中间没接**，而留痕会显示这一轮什么都没丢。
    """

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    names = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in {"_rate_gate", "_record_rate_limit"}
    ]

    # ⚠️ **2026-09-23（ADR-0151）：三者变两者。**
    # 中间那个 `_drop_promises_the_split_no_longer_keeps` 随「一轮不再被切开」
    # 一起删了 —— 它挂在 `if withheld and kept:` 上，而两道闸都原子之后
    # 那两者不可能同时非空。
    assert names == [
        "_rate_gate",
        "_record_rate_limit",
    ], f"两者的调用顺序不对：{names}"


def test_the_audit_row_carries_the_promises_it_dropped() -> None:
    """丢了哪几句要进那条审计行 —— 不然「客户收到的那条为什么少一句」查不到。

    **只记动词短语，不记正文**，与 `_record_plan_generation_disagreement`
    里那一族逐字同一条理由。
    """

    source = PIPELINE.read_text(encoding="utf-8")
    assert '"promises_dropped": [' in source, "审计行里没有这一项"
    assert '"promised": str(item.get("promised") or ""),' in source, (
        "记的不是那个动词短语"
    )
    assert "promises_dropped: list[dict[str, Any]]," in source, (
        "`_record_rate_limit` 没有必填这一项 —— 「忘了传就当没有」"
        "与「这一轮真的没丢过许诺」在审计上长得一样"
    )


def test_the_two_limits_do_not_share_one_unit_in_the_audit_row() -> None:
    """两个上限单位不同，**键名把单位写出来**。

    先前两个都叫 `..._limit`；会话这一边从此数轮、租户那一边数条，
    两个数并列而单位不同，读审计的人会按同一个单位读它们。
    """

    source = PIPELINE.read_text(encoding="utf-8")
    assert '"conversation_turn_limit"' in source
    assert '"tenant_message_limit"' in source
    assert '"conversation_limit"' not in source, "旧键名还在，单位又混在一起了"


def test_the_customer_facing_effect_is_the_one_being_limited() -> None:
    """闸收的是**排出站行**的那些决策，不是混着 kind 的 `accepted`。

    独立审查 2026-09-12 P1-1 修过这件事（一次 `remember` 写入挤掉一条真回复）。
    本批改了闸的单位，**而那条分流不能因此松掉**。
    """

    customer_paths = [
        name
        for name, path in GENERATION_WRITE_PATHS.items()
        if isinstance(path.effect, OutboxDelivery) and path.effect.audience == "customer"
    ]
    assert sorted(customer_paths) == ["followup", "send_material", "send_message"], (
        f"客户可见的出站写入路径变了：{customer_paths} —— 轮次键那两条前提要重核"
    )
