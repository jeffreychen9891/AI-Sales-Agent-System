"""T43: the three judges the cost ceiling now runs, and what each one means.

ADR-0034 split one number into two, and ADR-0036 put one narrow denial back:

* **settled spend** may deny -- it is the only judge whose refusal is a claim
  about money a supplier actually charged;
* **the unsettled count** may deny -- it says settlement has stopped, which is
  the one condition under which the settled judge is reading a total that is
  missing an unknown amount;
* **the estimate** may only warn, *except* at `hard x K`, where it is the
  concurrency guard: two workers whose calls have not returned both see
  `settled = 0`, and the advisory lock serialises them without changing that.

The distinction is not cosmetic and neither are the reason codes. Before this,
every refusal said `cost_hard_ceiling`, and two of the three now say something
else -- one means "a guard fired", the other means "settlement is broken", and
reading either as "the budget ran out" sends whoever is paged to look for a
tenant who spent the money. Nobody spent it.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from fastagent_v2.worker.cost import (
    ESTIMATED_HARD_REASON_CODE,
    ESTIMATED_REASON_CODE,
    HARD_REASON_CODE,
    UNSETTLED_BACKLOG_REASON_CODE,
    CallContext,
    CeilingDecision,
    CostCeilingExceeded,
    CostGovernor,
)
from fastagent_v2.worker.model_policy import ModelPolicyRelease, canonical_hash
from fastagent_v2.worker.pipeline import COST_REFUSAL_REASON_CODES, HANDOFF_REASON_CODES

from tests.contracts.test_cost_ledger import FakeDatabase, priced_release


def _context() -> CallContext:
    return CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    )


def _release(**ceilings: Any) -> ModelPolicyRelease:
    document = copy.deepcopy(priced_release().document)
    document["ceilings_cny"].update(ceilings)
    return ModelPolicyRelease(
        document=document, content_hash=canonical_hash(document), path=Path("in-memory")
    )


def _decide(database: FakeDatabase, release: ModelPolicyRelease | None = None) -> CeilingDecision:
    governor = CostGovernor(
        database=database,  # type: ignore[arg-type]
        release=release or priced_release(),
    )
    return governor.precheck(
        binding=(release or priced_release()).role("GENERATE_VALUE"),
        context=_context(),
        estimated_cost=Decimal("0.01"),
    )


# ---------------------------------------------------------------------------
# Judge 3 -- the estimate as a concurrency guard (ADR-0036)
# ---------------------------------------------------------------------------


def test_the_estimate_denies_only_once_it_passes_the_amplified_threshold() -> None:
    """`hard` warns; `hard x K` denies. The gap is the whole of ADR-0036.

    Both cases have settled spend of zero: nothing has been charged, and the
    only reason to refuse is that too much is in flight to be sure.
    """

    release = _release(estimated_ceiling_factor=9)

    just_over_hard = _decide(FakeDatabase(daily="0", projected_daily="91.5"), release)
    assert just_over_hard.effect == "warn"
    assert just_over_hard.reason_code == ESTIMATED_REASON_CODE

    far_over_hard = _decide(FakeDatabase(daily="0", projected_daily="819.5"), release)
    assert far_over_hard.effect == "deny"
    assert far_over_hard.reason_code == ESTIMATED_HARD_REASON_CODE
    assert far_over_hard.window == "daily"
    assert far_over_hard.limit == Decimal("91") * Decimal("9")


def test_the_amplification_factor_comes_from_the_release() -> None:
    """K is configurable because it is not calibrated.

    ADR-0036 fits the default to a single measured call and says so. A factor
    baked into the code would make the next measurement a code change, and the
    number most likely to be wrong is the worst one to freeze.
    """

    strict = _release(estimated_ceiling_factor=1)
    decision = _decide(FakeDatabase(daily="0", projected_daily="91.5"), strict)
    assert decision.effect == "deny"
    assert decision.reason_code == ESTIMATED_HARD_REASON_CODE


def test_settled_spend_is_judged_before_the_estimate_is_consulted() -> None:
    """Order matters for the reason code, not just for the answer.

    Here both judges would refuse. The record has to say the settled one did,
    because that is the refusal that means money was actually spent.
    """

    decision = _decide(FakeDatabase(daily="91.5", projected_daily="900"))
    assert decision.effect == "deny"
    assert decision.reason_code == HARD_REASON_CODE


# ---------------------------------------------------------------------------
# Judge 2 -- the count gate
# ---------------------------------------------------------------------------


def test_a_backlog_of_unsettled_calls_denies_under_its_own_reason_code() -> None:
    """The condition is "settlement stopped", not "the budget ran out".

    Four code paths leave a row in `estimated` forever -- a timeout, a transport
    failure, an HTTP >= 400 with no usable `usage` block, and a malformed
    response with none -- and no reconciler exists to clear any of them. While
    that is true the settled judge is reading a total missing an unknown amount,
    and this is the only bound on how much.
    """

    release = _release(max_unsettled_calls_conversation=20)
    under = _decide(FakeDatabase(abandoned_conversation=20), release)
    assert under.effect == "allow"

    over = _decide(FakeDatabase(abandoned_conversation=21), release)
    assert over.effect == "deny"
    assert over.reason_code == UNSETTLED_BACKLOG_REASON_CODE
    assert over.window == "conversation"
    assert over.projected == Decimal(21)
    assert over.limit == Decimal(20)


def test_the_default_conversation_gate_is_sized_to_the_conversation_ceiling() -> None:
    """计数闸放行的「看不见的钱」不该超过它守的那个窗口的硬顶。

    ## 这一条量的必须是**生产跑的那份 release**，不是夹具

    2026-09-15 这条差点被改错。天真的「改成现算」是让它按 `priced_release()` 算 ——
    **而那是夹具，价目 1/2 元每百万；真实 release 是 3/9。**
    按夹具算，可证单次最坏 0.05412，`4 x = 0.216 <= 0.324`，**这一条会绿**；
    按真实 release 算是 0.168504，`4 x = 0.674`，**超标准 2.08 倍**。

    **也就是说：拿夹具现算，会把一个诚实的过期常数换成一个永远绿的数** ——
    方向 fail-open，比过期更坏。所以这里两半都读 `active_release()`。
    （`hard_conversation` 两边今天都是 **1.0**，**仍然恰好一致** ——
    夹具是深拷贝在架那份再改价目，所以上限跟着走。这处混用至今没露过馅，
    而它不该继续靠巧合。2026-09-17 上限从 0.27 抬到 1.0 时这句话跟着改了一次，
    **而那正说明它是一句会过期的话**。）

    ## 历史

    独立审查 2026-09-03 P2-2 把默认值从 20 改成 4，因为
    `20 x 0.070656 = 1.41` 是会话硬顶的五倍。**那个 4 本身也是被写死的推导结果**，
    2026-09-15 抬上限之后一起过期（独立复核指出 `_PROVABLE_MAX_CALL_CNY` 那一处，
    而 4 与它同源）。用户裁定：两个都改成现算。
    """

    from fastagent_v2.worker.cost import (
        derived_unsettled_conversation_limit,
        provable_max_call_cny,
    )
    from fastagent_v2.worker.model_policy import active_release

    production = active_release()
    worst = provable_max_call_cny(production)
    limit = derived_unsettled_conversation_limit(production)
    hard_conversation = Decimal(str(production.ceilings_cny["hard_conversation"]))

    assert worst > 0, "release 定不了价，这一条量不了它声称量的那件事"
    invisible = limit * worst
    assert invisible <= hard_conversation * Decimal("1.2"), (
        f"the conversation count gate lets {invisible} CNY go unseen against a "
        f"ceiling of {hard_conversation}; size it to the window it guards"
    )

    # **另一头**：不能因为闸小就绿。多放一笔必须真的越线 ——
    # 否则把它写死成 `return 1` 也能让上面那条永远过。
    #
    # **下界比的是硬顶本身，不是硬顶 × 1.2**（2026-09-17 改）。
    # 上面那条的 `1.2` 是**容差**：允许放行的钱略微超过它守的窗口。
    # 把同一个 1.2 拿来当下界，就把一个容差变成了**要求** ——
    # 等于要求这道闸必须去吃掉那 20% 的余量，而
    # `derived_unsettled_conversation_limit` 的政策白纸黑字是
    # `floor(hard / worst)`，从来不是 `floor(hard * 1.2 / worst)`。
    #
    # **它在旧上限下没露馅是因为险过 4%**：`hard = 0.27` 时 limit = 1，
    # `2 x 0.168504 = 0.337008` 对 `0.324`。抬到 1.0 之后 limit = 5，
    # `6 x 0.168504 = 1.011024` 对 `1.2` —— 当场红，而红的是这条用例的标准，
    # 不是那道闸。
    #
    # **改完它照样杀得掉 `return 1`**：`hard = 1.0` 时
    # `2 x 0.168504 = 0.337008`，不到 `1.0`，红。
    assert (limit + 1) * worst > hard_conversation, (
        f"再多放一笔才 {(limit + 1) * worst}，仍在硬顶 {hard_conversation} 之内 —— "
        f"这道闸比它需要的更紧，{limit} 不是那条推导给出的答案"
    )

    # And the default is what production runs, so it is what gets asserted.
    # **release 要跟着传** —— 夹具那份的价目不同，推出来的闸也不同。
    decision = _decide(FakeDatabase(abandoned_conversation=limit + 1), production)
    assert decision.effect == "deny"
    assert decision.reason_code == UNSETTLED_BACKLOG_REASON_CODE
    assert decision.window == "conversation"


def test_the_daily_count_gate_is_separate_from_the_conversation_one() -> None:
    release = _release(max_unsettled_calls_conversation=20, max_unsettled_calls_daily=200)
    decision = _decide(FakeDatabase(abandoned_conversation=3, abandoned_daily=201), release)
    assert decision.effect == "deny"
    assert decision.reason_code == UNSETTLED_BACKLOG_REASON_CODE
    assert decision.window == "daily"


def test_the_count_gate_is_a_count_and_not_a_sum_of_estimates() -> None:
    """Deliberate: a sum here would be a third judge built on the same number.

    The estimate is what ADR-0034 removed from the breaker. Reintroducing it as
    "the value of what is unsettled" would put it straight back. A count says
    the one thing the estimate's inaccuracy cannot make wrong: this many calls
    left and never came back.
    """

    release = _release(max_unsettled_calls_conversation=20)
    # A large unsettled *value* with a small count does not trip the count gate.
    decision = _decide(
        FakeDatabase(daily="0", projected_daily="90", abandoned_conversation=2), release
    )
    assert decision.reason_code != UNSETTLED_BACKLOG_REASON_CODE


# ---------------------------------------------------------------------------
# What the rest of the machine does with a refusal
# ---------------------------------------------------------------------------


def test_the_exception_carries_the_judge_that_refused() -> None:
    """The pipeline reads `error.reason_code` into `audit_events`.

    `CostCeilingExceeded` used to answer for every refusal with the class-level
    `cost_hard_ceiling`. That is the value the Stage collector counts and alerts
    on, so a guard firing would have paged as a hard ceiling being hit.
    """

    default = CostCeilingExceeded("x", window="daily", limit=Decimal(1), projected=Decimal(2))
    assert default.reason_code == HARD_REASON_CODE

    guarded = CostCeilingExceeded(
        "x",
        window="daily",
        limit=Decimal(1),
        projected=Decimal(2),
        reason_code=ESTIMATED_HARD_REASON_CODE,
    )
    assert guarded.reason_code == ESTIMATED_HARD_REASON_CODE
    assert CostCeilingExceeded.reason_code == HARD_REASON_CODE, "class default not mutated"


def test_every_cost_refusal_hands_the_conversation_to_a_person() -> None:
    """All three refusals stop the machine, so all three must reach a human.

    The handoff set used to name the hard ceiling alone. Adding two more ways to
    refuse without adding them here would have produced a conversation the
    platform silently stopped serving.
    """

    assert COST_REFUSAL_REASON_CODES == {
        HARD_REASON_CODE,
        ESTIMATED_HARD_REASON_CODE,
        UNSETTLED_BACKLOG_REASON_CODE,
    }
    assert COST_REFUSAL_REASON_CODES <= HANDOFF_REASON_CODES
    # A warning is not a refusal and must never reach the handoff set.
    assert ESTIMATED_REASON_CODE not in HANDOFF_REASON_CODES


# ---------------------------------------------------------------------------
# The alert path: a warning nobody can count is not a warning
# ---------------------------------------------------------------------------


class _RecordingConnection:
    def __init__(self, database: RecordingDatabase) -> None:
        self._database = database
        self._statement = ""

    def execute(self, statement: str, params: Any = None) -> _RecordingConnection:
        self._statement = statement
        self._database.statements.append((statement, params))
        return self

    def fetchone(self) -> Any:
        return _FakeConnectionShim(self._database.spend, self._statement).fetchone()


class _FakeConnectionShim:
    """Reuses the ledger fake's row shapes without its transaction plumbing."""

    def __init__(self, database: FakeDatabase, statement: str) -> None:
        self._database = database
        self._statement = statement

    def fetchone(self) -> Any:
        from tests.contracts.test_cost_ledger import _FakeConnection

        connection = _FakeConnection(self._database)
        connection.execute(self._statement)
        return connection.fetchone()


class RecordingDatabase:
    """Captures every statement `reserve` issues, so the writes can be asserted."""

    def __init__(self, spend: FakeDatabase) -> None:
        self.spend = spend
        self.statements: list[tuple[str, Any]] = []

    def transaction(self, *, tenant_id: Any) -> Any:  # noqa: ARG002
        database = self

        class _Transaction:
            def __enter__(self) -> _RecordingConnection:
                return _RecordingConnection(database)

            def __exit__(self, *_: Any) -> None:
                return None

        return _Transaction()


def test_a_warning_is_written_where_the_alert_path_can_count_it() -> None:
    """ADR-0034's premise, checked rather than asserted in prose.

    The decision says the estimate may only warn *and* that the warning must
    actually go out, or it amounts to doing nothing. On Stage the alert path is
    a collector that counts `audit_events` by `reason_code` and a Grafana rule
    on the increase. `policy_decisions` -- where the governor already wrote --
    is read by nothing, so a row there was not a warning at all.

    Both rows are expected: the policy decision is the governance record, the
    audit event is the signal.
    """

    from datetime import UTC, datetime

    database = RecordingDatabase(FakeDatabase(daily="0", projected_daily="90.999"))
    release = _release(estimated_ceiling_factor=9)
    governor = CostGovernor(database=database, release=release)  # type: ignore[arg-type]
    decision, row_id = governor.reserve(
        binding=release.role("GENERATE_VALUE"),
        context=_context(),
        estimated_input=10,
        estimated_output=10,
        estimated_cost=Decimal("0.01"),
        prompt_release="alert-path-test",
        occurred_at=datetime.now(UTC),
    )
    assert decision.effect == "warn"
    assert decision.reason_code == ESTIMATED_REASON_CODE
    assert row_id is not None, "a warning still reserves; the call proceeds"

    audit = [
        params
        for statement, params in database.statements
        if "INSERT INTO audit_events(" in statement
    ]
    assert len(audit) == 1, "exactly one countable row per warning, no more and no less"
    assert ESTIMATED_REASON_CODE in audit[0]
    assert any(
        "INSERT INTO policy_decisions(" in statement for statement, _ in database.statements
    ), "the governance record is kept as well, not replaced"


def test_a_refusal_does_not_write_a_second_audit_row() -> None:
    """The hard-ceiling counter feeds a live alert; double counting is not free.

    A denial travels as `CostCeilingExceeded` and the pipeline's no-send path
    writes the audit row. Writing one here too would double every count the
    Stage rule sees.
    """

    from datetime import UTC, datetime

    database = RecordingDatabase(FakeDatabase(daily="91.5"))
    release = _release()
    governor = CostGovernor(database=database, release=release)  # type: ignore[arg-type]
    decision, row_id = governor.reserve(
        binding=release.role("GENERATE_VALUE"),
        context=_context(),
        estimated_input=10,
        estimated_output=10,
        estimated_cost=Decimal("0.01"),
        prompt_release="alert-path-test",
        occurred_at=datetime.now(UTC),
    )
    assert decision.effect == "deny"
    assert row_id is None
    assert not [
        params
        for statement, params in database.statements
        if "INSERT INTO audit_events(" in statement
    ]


#: 复核 2026-09-17 在 Stage 上现读的一通**正常**对话的预计成本（CNY）。
#:
#: 它不是压力测试、不是最坏情况 —— 就是一通有人在正常说话的会话。
#: 当天的硬顶是 `0.27`，于是它超 1.5%、`UNDERSTAND_VALUE` 被拒、
#: 自动交接给人、跟进定时器到期时按设计沉默。链上每一步都对，**错的是那个数**。
OBSERVED_NORMAL_CONVERSATION_CNY = Decimal("0.274191")


def test_the_conversation_ceiling_does_not_catch_a_normal_conversation() -> None:
    """**上限是防止出问题的，不是用来影响正常对话的**（用户 2026-09-17 裁定）。

    ## 为什么这条得存在

    在此之前，钉着这两个数的只有一条「它们等于 0.11 / 0.27」——
    **那条只证明没人手滑改过它，不证明这个数是对的**。
    于是 0.27 这个会在正常会话上撞线的值安安静静地跑了半个月，
    直到运维报「跟进没触发」、复核去量才露出来。

    一个会在正常会话上撞线的上限不是防炸的闸，**它就是那个炸点**。

    ## 判据

    硬顶至少要是实测正常会话的 **2 倍**。留 2 倍而不是刚好盖住，
    是因为那个 0.274191 是**某一通**会话的数，不是上界：
    换一通长一点的、多引两条知识的，它就更高。

    ## 它不管的事

    **它不判断这个数够不够防炸**（那是另一头，由日窗与月窗看）。
    这条只管一件：别拦住正常说话的人。
    """

    from fastagent_v2.worker.model_policy import active_release  # noqa: PLC0415

    hard = Decimal(str(active_release().ceilings_cny["hard_conversation"]))

    assert hard >= OBSERVED_NORMAL_CONVERSATION_CNY * 2, (
        f"会话硬顶 {hard} 对实测正常会话 {OBSERVED_NORMAL_CONVERSATION_CNY} 只有 "
        f"{hard / OBSERVED_NORMAL_CONVERSATION_CNY:.2f} 倍 —— "
        "这不是防炸的闸，它会拦住正常对话（2026-09-17 就拦过一次）"
    )


def test_the_warning_ceiling_does_not_fire_on_every_normal_conversation() -> None:
    """软顶同理 —— **一个永远在响的告警等于没有告警**。

    `soft` 在 `cost.py` 里只出现在「warnings only」那两段，所以它拦不住谁。
    但它留在 0.11 的后果是每通正常对话都告警一次，而那样的告警
    没有人会再看第二眼 —— 真出事的那次也就淹在里面。
    """

    from fastagent_v2.worker.model_policy import active_release  # noqa: PLC0415

    soft = Decimal(str(active_release().ceilings_cny["soft_conversation"]))

    assert soft >= OBSERVED_NORMAL_CONVERSATION_CNY, (
        f"软顶 {soft} 低于实测正常会话 {OBSERVED_NORMAL_CONVERSATION_CNY} —— "
        "每通正常对话都会告警一次"
    )
