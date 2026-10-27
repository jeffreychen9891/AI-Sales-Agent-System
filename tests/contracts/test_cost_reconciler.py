"""T55: the ledger gets a state for money it cannot account for.

Four exits in the provider adapter leave a reservation in `estimated` and never
return to it. Its own comment says the estimate stays reserved "until
reconciliation" -- and there was none, anywhere in the repository.

**Why a fourth state instead of the two that already exist** (user decision
2026-09-03, from three options that were measured before any was chosen):

* `failed` asserts *the supplier did not bill us*;
* `settled` asserts *the supplier billed us this much*;
* `unreconciled` asserts *we do not know*.

Only the third is true. And asking the supplier is not available as a fallback:
usage is read exactly once, from the response body of the call itself, and the
ledger stores **no supplier-side identifier** -- there is no request id to ask
about. That measurement is what removed the compensating-reconcile option.

The other half of the batch is a defect T55 found in T43's own count gate: it
counted every `estimated` row, so a tenant with four calls in flight and a
tenant whose settlement path broke yesterday tripped it identically.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.worker.cost import (
    RECONCILE_AFTER_SECONDS,
    UNSETTLED_BACKLOG_REASON_CODE,
    CallContext,
    CostGovernor,
    derived_unsettled_conversation_limit,
)

from tests.contracts.test_cost_ledger import FakeDatabase, priced_release

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "infrastructure" / "migrations" / "010_cost_unreconciled_status.sql").read_text(
    encoding="utf-8"
)
COST_SOURCE = (
    ROOT / "services" / "worker" / "src" / "fastagent_v2" / "worker" / "cost.py"
).read_text(encoding="utf-8")


def _context() -> CallContext:
    return CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    )


def _over_the_gate() -> int:
    """比那道会话计数闸多放一笔。

    **不写死一个数。** 这两条用例问的是「在飞的与走了不回来的分不分得清」
    与「拒绝的话说不说得清在飞几笔」—— 它们的主语不是某个具体计数，
    而是「越过了闸」。

    写死的代价 2026-09-17 付过：原来写的是 `5`，那是照
    `hard_conversation = 0.27` 时推出来的闸（夹具价目下是 4）选的。
    上限抬到 1 元之后闸变成 18，**`5` 成了一个完全正常的数**，
    两条用例一起红 —— 而它们要守的东西一点没变。
    """

    return derived_unsettled_conversation_limit(priced_release()) + 1


def _decide(database: FakeDatabase) -> Any:
    governor = CostGovernor(database=database, release=priced_release())  # type: ignore[arg-type]
    return governor.precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=_context(),
        estimated_cost=Decimal("0.01"),
    )


def test_the_migration_adds_the_state_and_says_why_the_other_two_would_lie() -> None:
    """The reasoning belongs in the database, not only in a review document.

    Whoever runs `\\d+ model_invocation_costs` in two years is the reader this
    is for, and they will not have the review.
    """

    assert "'estimated', 'settled', 'failed', 'unreconciled'" in MIGRATION
    assert "reconciled_at" in MIGRATION
    # The three-way argument, stated where it survives.
    assert "did not bill us" in MIGRATION
    assert "billed us this much" in MIGRATION
    assert "we do not know" in MIGRATION
    # And the measurement that removed the fourth option.
    assert "no supplier-side identifier" in MIGRATION


def test_reconciled_at_is_not_settled_at() -> None:
    """Two timestamps that mean different things, kept apart.

    `settled_at` means the supplier answered. `reconciled_at` means we stopped
    waiting. A row carrying one and not the other is unambiguous about which
    happened; one column would have made them the same event.
    """

    assert "settled_at means the supplier answered" in MIGRATION
    assert "reconciled_at means we\nstopped waiting" in MIGRATION


def test_the_age_threshold_is_derived_from_the_deadlines_not_chosen() -> None:
    """The derivation, checked against the release -- not against a comment.

    This used to assert `"deadline_ms = 8000" in COST_SOURCE`: two strings from
    `cost.py`'s own comments. Raising a role's `deadline_ms` to 900000, or
    setting `FASTAGENT_RUNTIME_DEADLINE_SECONDS=3600`, would have broken the
    derivation while the test stayed green -- and the consequence is legitimately
    in-flight rows being judged abandoned and moved by the reconciler
    (independent review 2026-09-03, P2-2).

    So it reads the real sources and requires real headroom.
    """

    from fastagent_v2.worker.model_policy import active_release

    release = active_release()
    slowest_call = max(role["deadline_ms"] for role in release.document["roles"].values())
    whole_event = release.envelope_deadline_ms

    assert RECONCILE_AFTER_SECONDS * 1000 >= whole_event * 10, (
        f"an over-age row must be unambiguously abandoned: {RECONCILE_AFTER_SECONDS}s "
        f"against an event deadline of {whole_event}ms is not enough headroom"
    )
    assert whole_event >= slowest_call, "an event cannot be shorter than one call within it"


def test_the_gate_no_longer_confuses_in_flight_with_abandoned() -> None:
    """The defect T55 found in T43's own gate.

    Four calls in flight is a busy conversation. Four rows that left yesterday
    and never came back is a broken settlement path. The old gate counted every
    `estimated` row and could not tell them apart -- so it would refuse the
    healthy tenant and say "settlement stopped" about a system that was fine.
    """

    busy = _decide(
        FakeDatabase(in_flight_conversation=_over_the_gate() * 8, abandoned_conversation=0)
    )
    assert busy.effect == "allow", "calls in flight are not a backlog"

    broken = _decide(
        FakeDatabase(in_flight_conversation=0, abandoned_conversation=_over_the_gate())
    )
    assert broken.effect == "deny"
    assert broken.reason_code == UNSETTLED_BACKLOG_REASON_CODE


def test_the_refusal_says_how_many_are_still_in_flight() -> None:
    """So the first person to read it is not left guessing which it is.

    The count in `projected` is calls that left and never returned; the detail
    carries the in-flight number beside it, because "5 abandoned, 0 in flight"
    and "5 abandoned, 30 in flight" call for different first moves.
    """

    decision = _decide(
        FakeDatabase(in_flight_conversation=3, abandoned_conversation=_over_the_gate())
    )
    assert decision.effect == "deny"
    assert "3 call(s) still in flight" in str(decision.inactive_reason)
    assert "left and never returned" in str(decision.inactive_reason)


def test_the_gate_works_whether_or_not_the_reconciler_ever_ran() -> None:
    """The age test is in SQL, not a dependency on a scheduled job.

    A gate that only works once a cron entry is installed reports "healthy" on
    every system where nobody installed it. On 2026-09-03 the Stage collector
    spent a day pushing seven metrics while everyone believed it pushed ten,
    purely because merging is not deploying -- so this gate counts an over-age
    `estimated` row as abandoned itself, and a deployment without the reconciler
    is conservative rather than blind.
    """

    # Asserted on **both** windows, not on a slice that happens to contain one.
    # The batch's own reverse probe disabled the daily half and this test stayed
    # green, because the slice still reached the conversation half -- the probe
    # was weak *and* the assertion was loose, and either alone would have hidden
    # it. The behavioural proof against a real database is in
    # `tests/e2e/test_m1a_cost.py`, in the test named
    # `test_an_over_age_reservation_counts_as_abandoned_without_the_reconciler`.
    abandoned_filters = COST_SOURCE.count(
        "status = 'estimated' AND occurred_at <= %s - %s * interval '1 second'"
    )
    assert abandoned_filters == 2, "both the daily and conversation windows must age rows"
    assert COST_SOURCE.count("status = 'unreconciled'") >= 2


def test_the_reconciler_refuses_to_invent_an_amount() -> None:
    """It moves state and stamps a time. It never writes a cost.

    Deciding an amount is the one thing this cannot do honestly, so the code
    must not contain the temptation: no `cost_cny`, no `estimated_cost_cny`, no
    `settled` in the statement it runs.
    """

    body = COST_SOURCE[COST_SOURCE.index("def reconcile_abandoned") :]
    body = body[: body.index("def mark_failed")]
    statement = re.search(r'"""\n(.*?)"""', body, re.S)
    assert statement is not None
    update = body[body.index("UPDATE model_invocation_costs") : body.index("(moment, tenant_id")]
    assert "cost_cny" not in update
    assert "'settled'" not in update
    assert "status = 'unreconciled'" in update
    assert "reconciled_at" in update


def test_the_reconciler_has_a_production_caller() -> None:
    """ADR-0037's question, answered for this batch's new function.

    A reconciler nobody calls is the same defect it was written to fix, one
    level up. `scripts/reconcile_costs.py` is the entry point; installing it on
    Stage is a deployment step, and this test at least keeps the function from
    being orphaned in the repository.
    """

    script = (ROOT / "scripts" / "reconcile_costs.py").read_text(encoding="utf-8")
    assert "governor.reconcile_abandoned(" in script
    assert "governor.abandoned_reservations(" in script
    # And it bootstraps its own path, so running it directly does not report an
    # import failure that has nothing to do with the ledger.
    assert "bootstrap_source_path()" in script


def test_a_dry_run_exists_because_this_writes_to_the_ledger() -> None:
    script = (ROOT / "scripts" / "reconcile_costs.py").read_text(encoding="utf-8")
    assert "--dry-run" in script
    assert "wrote nothing" in script


def test_the_boundary_is_stated_the_same_way_in_the_sql_and_the_docstring() -> None:
    """The previous version of this test proved nothing at all.

    It computed `(now - occurred).total_seconds() > RECONCILE_AFTER_SECONDS` and
    then asserted that equalled `age_seconds > RECONCILE_AFTER_SECONDS` -- the
    same expression on both sides, passing for any implementation. It also
    documented the opposite of the SQL: the query uses `occurred_at <= now - 900s`,
    so a row of **exactly** that age is already abandoned, while the docstring
    said such a row was still in flight. Nothing noticed, because the test never
    touched either (independent review 2026-09-03, P2-3).

    The behavioural boundary is now checked against a real database in
    `tests/e2e/test_m1a_cost.py`. What is left here is the cheap half: the SQL
    and the prose must agree about which side of the boundary is which.
    """

    assert "occurred_at <= %s - %s * interval '1 second'" in COST_SOURCE
    assert "exactly** that age is already abandoned" in COST_SOURCE
    assert (
        "still in flight"
        not in COST_SOURCE.split("def reconcile_abandoned")[1].split("def mark_failed")[0]
    )


def test_the_in_flight_count_reaches_the_message_a_person_reads(monkeypatch) -> None:
    """P2-1. Carrying a number on a dataclass nobody reads is not carrying it.

    The refusal travels as `CostCeilingExceeded`, whose message the pipeline
    stores into the no-send record and from there into `audit_events`. Before
    this, `in_flight_conversation` was set on the decision and dropped on the
    floor: the exception text was built from `projected` / `limit` / `window`
    only. The self-review's justification -- "5 abandoned, 0 in flight" and
    "5 abandoned, 30 in flight" call for different first moves -- was true and
    unimplemented.

    So this asserts the finished string, not the field.
    """

    from unittest.mock import Mock
    from uuid import uuid4

    # The credential is read before the ceiling is consulted; the refusal under
    # test happens after. Any value will do -- it is never sent, because the
    # governor denies before the transport is touched.
    monkeypatch.setenv("FASTAGENT_MODEL_API_KEY_DEEPSEEK", "test-only-not-a-real-key")

    from fastagent_v2.worker.cost import CeilingDecision, CostCeilingExceeded
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import OpenAICompatibleChatClient

    governor = Mock()
    governor.estimate.return_value = (10, 10, Decimal("0.01"))
    governor.reserve.return_value = (
        CeilingDecision(
            effect="deny",
            reason_code=UNSETTLED_BACKLOG_REASON_CODE,
            window="conversation",
            limit=Decimal(4),
            projected=Decimal(5),
            enforcement_active=True,
            inactive_reason="3 call(s) still in flight on this conversation; the "
            "conversation count above is calls that left and never returned",
        ),
        None,
    )
    context = CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        correlation_id=uuid4(),
    )

    client = OpenAICompatibleChatClient(transport=Mock())
    with pytest.raises(CostCeilingExceeded) as refused:
        client.complete(
            binding=active_release().role("GENERATE_VALUE"),
            system_prompt="s",
            user_prompt="u",
            governor=governor,
            context=context,
        )

    message = str(refused.value)
    assert "3 call(s) still in flight" in message
    assert "abandoned calls" in message
    assert refused.value.reason_code == UNSETTLED_BACKLOG_REASON_CODE
