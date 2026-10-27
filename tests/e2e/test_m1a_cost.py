"""Ledger persistence and hard-ceiling behaviour against a real database."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.cost import (
    ESTIMATED_HARD_REASON_CODE,
    HARD_REASON_CODE,
    MAX_MODEL_INPUT_BYTES,
    UNSETTLED_BACKLOG_REASON_CODE,
    CallContext,
    CostCeilingExceeded,
    CostGovernor,
)
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.model_policy import (
    MODEL_POLICY_ROOT,
    ModelPolicyRelease,
    active_release,
    canonical_hash,
    load_release,
)
from fastagent_v2.worker.pipeline import InboxProcessor
from fastagent_v2.worker.providers import (
    OpenAICompatibleChatClient,
    ProviderInputTooLarge,
    TransportResponse,
)

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def priced_release(**ceiling_overrides: Any) -> ModelPolicyRelease:
    document = copy.deepcopy(active_release().document)
    for binding in document["roles"].values():
        binding["price_cny_per_million_tokens"] = {"input": 1.0, "output": 2.0}
    document["ceilings_cny"].update(ceiling_overrides)
    document["ceilings_cny"]["enforcement_active"] = True
    document["unverified_fields"] = []
    return ModelPolicyRelease(
        document=document, content_hash=canonical_hash(document), path=Path("in-memory")
    )


class CeilingBlockedInference(DeterministicMockInference):
    """A governor refusal, in whichever of its three forms the test asks for.

    The reason code used to be hard-wired to the class default, so every e2e
    pass through `_persist_no_send` was a `cost_hard_ceiling` pass and the two
    codes ADR-0036 added had only a set-membership assertion behind them
    (independent review 2026-09-03, P3-1). They reach a person by a different
    route than they are declared on, and only walking it proves it.
    """

    def __init__(self, reason_code: str | None = None) -> None:
        self._reason_code = reason_code

    def understand(self, **_: Any) -> dict[str, Any]:
        raise CostCeilingExceeded(
            "GENERATE_VALUE was refused by the conversation ceiling",
            window="conversation",
            limit=Decimal("0.27"),
            projected=Decimal("0.28"),
            reason_code=self._reason_code,
        )


class CountingCeilingBlockedInference(CeilingBlockedInference):
    """Counts understanding calls. **It no longer refuses the second one.**

    It used to raise `AssertionError("human-controlled conversation reached
    inference")` on any call after the first, because human control was a
    pre-inference block. **ADR-0054 §三.1 moved it to pre-generation**, so the
    understanding step is now supposed to run during a takeover -- that is the
    whole point: a customer talking to a human agent must still be understood,
    or the resume plans blind.

    The guard is not simply deleted. What it protected -- "a takeover does not
    quietly turn into a send" -- is asserted directly by the test below, on
    intents and outbox rows rather than on a call count.
    """

    def __init__(self) -> None:
        super().__init__()
        self.understand_calls = 0

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        self.understand_calls += 1
        return super().understand(**kwargs)


class RecordingModelTransport:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls = 0

    def post_json(self, **_: Any) -> TransportResponse:
        self.calls += 1
        return TransportResponse(status_code=200, body=self.body)


def _start_conversation(
    *, settings: Settings, seeded_database: dict[str, object], inference: Any
) -> tuple[UUID, Any]:
    conversation_id = new_conversation(seeded_database["tenant_a"])
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"cost-{uuid4()}", sender_id=sender_of(conversation_id)
        ),
    )
    assert accepted.accepted is True
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="cost-worker",
    )
    return conversation_id, processor.process_one(block_ms=500)


def test_a_call_is_ledgered_before_it_runs_and_settled_after(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, result = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    assert result is not None

    database = Database(settings.worker_database_url)
    governor = CostGovernor(database=database, release=priced_release())
    binding = governor.release.role("GENERATE_VALUE")
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    row_id = governor.open_row(
        binding=binding,
        context=context,
        estimated_input=100,
        estimated_output=binding.max_output_tokens or 0,
        estimated_cost=Decimal("0.5"),
        prompt_release="test-prompt",
    )
    with database.transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT status, cost_cny, price_input_snapshot FROM model_invocation_costs "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_id, row_id),
        ).fetchone()
    assert row["status"] == "estimated"
    assert row["cost_cny"] is None, "an unsettled call has no measured cost yet"
    assert row["price_input_snapshot"] == Decimal("1.000000")

    settled = governor.settle(
        row_id=row_id,
        context=context,
        binding=binding,
        input_tokens=1_000_000,
        output_tokens=500_000,
    )
    assert settled == Decimal("2.0")

    daily, monthly = governor.spent(tenant_id)
    assert daily == Decimal("2.0")
    assert monthly == Decimal("2.0")


def test_an_unpriced_call_settles_to_null_not_zero(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    database = Database(settings.worker_database_url)
    governor = CostGovernor(
        database=database,
        release=load_release(MODEL_POLICY_ROOT / "release-2026.09.02-3.json"),
    )
    binding = governor.release.role("GENERATE_VALUE")
    assert binding.can_price is False
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    row_id = governor.open_row(
        binding=binding,
        context=context,
        estimated_input=10,
        estimated_output=10,
        estimated_cost=None,
        prompt_release="test-prompt",
    )
    assert governor.settle(
        row_id=row_id, context=context, binding=binding, input_tokens=10, output_tokens=10
    ) is None

    with database.transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT status, cost_cny FROM model_invocation_costs WHERE id = %s", (row_id,)
        ).fetchone()
        rollup = connection.execute(
            "SELECT invocations, unpriced_invocations, priced_cost_cny FROM model_cost_daily "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
    assert row["status"] == "settled"
    assert row["cost_cny"] is None
    assert rollup["unpriced_invocations"] == 1
    assert rollup["priced_cost_cny"] == Decimal("0")


def test_mark_failed_closes_a_row_when_something_calls_it(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """What this proves is narrower than its old name claimed.

    It was called `test_a_failed_call_does_not_stay_open_in_the_ledger`, but it
    calls `mark_failed` by hand -- and **nothing in production calls it**. So it
    proved the method works, never that a failed call reaches it. The companion
    test below states the real situation, and T43's count gate exists because of
    it. (Same shape as the 2026-09-03 review finding on T1: a green test over a
    path production does not take.)
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    database = Database(settings.worker_database_url)
    governor = CostGovernor(database=database, release=priced_release())
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )
    row_id = governor.open_row(
        binding=governor.release.role("GENERATE_VALUE"),
        context=context,
        estimated_input=10,
        estimated_output=10,
        estimated_cost=Decimal("0.1"),
        prompt_release="test-prompt",
    )
    governor.mark_failed(row_id=row_id, context=context)
    with database.transaction(tenant_id=tenant_id) as connection:
        status = connection.execute(
            "SELECT status FROM model_invocation_costs WHERE id = %s", (row_id,)
        ).fetchone()["status"]
    assert status == "failed"


def test_nothing_in_production_ever_marks_a_reservation_failed() -> None:
    """`mark_failed` still has no production caller, and that is now deliberate.

    **The premise of this test changed on 2026-09-03.** A reconciler now exists
    (`CostGovernor.reconcile_abandoned`, T55 / ADR-0040) -- but it moves rows to
    `unreconciled`, never to `failed`, and the distinction is the whole decision:
    `failed` asserts *the supplier did not bill us*, which nobody here knows.
    The ledger stores no supplier-side identifier, so there is no one to ask.

    So this stays a tripwire, with a narrower meaning than before: if something
    starts writing `failed`, someone has decided an unbilled outcome on evidence
    this system does not have, and the count-gate thresholds -- which were
    derived for money that stays invisible -- need re-deriving.
    """

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    # `scripts/` as well as `services/`. Independent review 2026-09-03, P3-3:
    # operational code in this repository lives in `scripts/`, so a reconciler
    # written there would leave this tripwire green while invalidating the
    # premise the count-gate thresholds were derived from.
    production = [
        path
        for directory in ("services", "scripts")
        for path in (root / directory).rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    callers = [
        path.relative_to(root).as_posix()
        for path in production
        if "mark_failed(" in path.read_text(encoding="utf-8")
        and path.name != "cost.py"  # its own definition
    ]
    assert callers == [], (
        "mark_failed now has a production caller: the T43 count gate was sized "
        f"for a ledger where it had none. Re-derive the limits. Callers: {callers}"
    )


def test_concurrent_workers_cannot_spend_the_same_remaining_hard_ceiling(  # noqa: D103
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    first_conversation, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    second_conversation, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    # ADR-0034 moved the breaker onto settled spend, and neither of these two
    # calls has returned -- so both workers see `settled = 0` and the settled
    # judge cannot separate them. The advisory lock serialises them without
    # changing what they see. What still separates them is ADR-0036's guard:
    # the estimate denies at `hard x K`. That is the whole reason the guard was
    # kept, so this test is where it has to be visible.
    #
    # `hard_daily = 1.5` with `K = 9` puts the guard at 13.5; two reservations
    # of 10 straddle it (10 passes, 10 + 10 does not). The scenario is the one
    # this test always described -- two workers, one remaining budget -- priced
    # for where the threshold now sits.
    release = priced_release(
        soft_daily=1.4,
        hard_daily=1.5,
        soft_monthly=1000,
        hard_monthly=2000,
        soft_conversation=1000,
        hard_conversation=2000,
        estimated_ceiling_factor=9,
    )
    binding = release.role("GENERATE_VALUE")
    occurred_at = datetime.now(UTC)
    start = Barrier(2)

    def attempt(conversation_id: UUID) -> tuple[Any, UUID | None]:
        governor = CostGovernor(
            database=Database(settings.worker_database_url), release=release
        )
        start.wait(timeout=5)
        return governor.reserve(
            binding=binding,
            context=CallContext(tenant_id, conversation_id, uuid4()),
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("10"),
            prompt_release="reservation-test",
            occurred_at=occurred_at,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first_conversation, second_conversation)))

    # "Not denied", not "allow". The worker that gets through is over the hard
    # ceiling on its estimate and therefore *warns* -- which is ADR-0034 working
    # as decided, not a weaker assertion: what this test guards is that exactly
    # one of the two ends up holding a reservation.
    permitted = [(decision, row_id) for decision, row_id in results if not decision.is_deny]
    denied = [(decision, row_id) for decision, row_id in results if decision.is_deny]
    assert len(permitted) == 1
    assert permitted[0][1] is not None
    assert len(denied) == 1
    assert denied[0][0].window == "daily"
    assert denied[0][1] is None, "a denied worker must not create a second reservation"
    # The reason code has to say which judge refused. This one is the guard, not
    # a statement that the tenant spent 13.5 CNY -- they have spent nothing yet.
    assert denied[0][0].reason_code == ESTIMATED_HARD_REASON_CODE


@pytest.mark.parametrize(
    "reason_code",
    [HARD_REASON_CODE, ESTIMATED_HARD_REASON_CODE, UNSETTLED_BACKLOG_REASON_CODE],
)
def test_a_hard_ceiling_produces_a_no_send_and_hands_the_conversation_to_a_human(
    settings: Settings, seeded_database: dict[str, object], reason_code: str
) -> None:
    """All three governor refusals must end the same way: nobody left waiting.

    Each stops the machine mid-conversation, so each has to reach a person --
    and `cost_unsettled_backlog` most of all, because its daily gate is
    *tenant-wide*: one broken settlement path pushes every conversation of that
    tenant to manual handling at once.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, result = _start_conversation(
        settings=settings,
        seeded_database=seeded_database,
        inference=CeilingBlockedInference(reason_code),
    )
    assert result is not None
    assert result.status == "no_send"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        decision = connection.execute(
            """
            SELECT reason_code, intent_id, effect FROM policy_decisions
             WHERE conversation_id = %s AND hook = 'post_plan'
            """,
            (conversation_id,),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT control_mode, state FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
        # **Customer-audience rows only.** A hard ceiling now also queues a
        # notice to the tenant's manager when one is configured (L2a, ruling of
        # 2026-09-04) -- a row in this same table, addressed to someone else.
        # The property this test is about is unchanged and is the one worth
        # keeping precise: the customer is sent nothing.
        outbox = connection.execute(
            """
            SELECT count(*) AS n FROM outbox_messages
             WHERE conversation_id = %s AND command->>'audience' = 'customer'
            """,
            (conversation_id,),
        ).fetchone()["n"]
        manual_task = connection.execute(
            """SELECT id, status, proposal, manual_task_id FROM intent_records
                 WHERE conversation_id = %s AND kind = 'manual_task'""",
            (conversation_id,),
        ).fetchone()

    assert decision["reason_code"] == reason_code
    assert decision["effect"] == "deny"
    assert decision["intent_id"] == manual_task["id"]
    assert outbox == 0
    assert aggregate["control_mode"] == "human", "the customer must not be left waiting on the AI"
    assert [frame["status"] for frame in aggregate["state"]["target_stack"]] == ["suspended"]
    assert manual_task["status"] == "PROPOSED"
    assert manual_task["manual_task_id"] == manual_task["id"]
    # The manual task a person picks up has to name the specific refusal too.
    # This line was the second hard-coded `cost_hard_ceiling` in the test, and
    # parametrising the first is what surfaced it: the proposal is the thing the
    # human actually reads, so "the budget ran out" appearing there for a
    # settlement fault would send them looking for a tenant who spent nothing.
    assert manual_task["proposal"]["reason_code"] == reason_code


def test_a_cost_refusal_during_takeover_files_its_own_task_instead_of_a_no_op(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """ADR-0054 §三.6, and the fixture below builds the hole exactly.

    A hard ceiling hands the conversation to a person. The customer then sends
    another message, the understanding step runs (§三.1) and is refused by the
    same ceiling -- and **the obvious path does nothing**: a cost refusal
    normally suspends the stack, writes `control_mode = 'human'` and files a
    task, all three of which already happened. Before this batch that meant the
    understanding step went quiet and **nobody was told**, which is precisely
    what §三.6 calls a hole rather than a design.

    So the second refusal carries its own reason code and files its own task.

    **This test used to assert the opposite of its first half** -- that a
    human-controlled conversation never reaches inference. That was true of the
    pre-inference block and is false by design now; the half that still matters
    (the event is consumed, the worker is not poisoned, nothing is sent) is
    asserted below on rows rather than on a call count.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    inference = CountingCeilingBlockedInference()
    conversation_id, first = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=inference
    )
    assert first is not None and first.status == "no_send"

    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"after-hard-block-{uuid4()}",
            sender_id=sender_of(conversation_id),
            text="is a human there?",
        ),
    )
    assert accepted.accepted is True
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="post-hard-block-worker",
    )
    second = processor.process_one(block_ms=500)
    assert second is not None and second.status == "no_send"
    # **Runs now, and that is the change** (§三.1).
    assert inference.understand_calls == 2

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        statuses = connection.execute(
            "SELECT status FROM inbox_events WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        reasons = connection.execute(
            "SELECT reason_code FROM policy_decisions "
            "WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        tasks = connection.execute(
            # The reason lives in the proposal document, not a column; the
            # classification lives on the task, which is its own row now (034).
            "SELECT i.proposal->>'reason_code' AS reason_code,"
            " i.proposal->>'source' AS source, m.notify FROM intent_records i"
            " JOIN manual_tasks m ON m.tenant_id = i.tenant_id AND m.intent_id = i.id"
            " WHERE i.conversation_id = %s AND i.kind = 'manual_task'"
            " ORDER BY i.created_at",
            (conversation_id,),
        ).fetchall()
        intents = connection.execute(
            "SELECT count(*) AS total FROM intent_records"
            " WHERE conversation_id = %s AND kind <> 'manual_task'",
            (conversation_id,),
        ).fetchone()
        outbox = connection.execute(
            "SELECT count(*) AS total FROM outbox_messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
    assert statuses == [{"status": "consumed"}, {"status": "consumed"}]
    # Its own code, so the record can tell "the machine stopped and handed over"
    # apart from "the machine stopped again while a person was already here".
    assert reasons[-1]["reason_code"] == "UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL"
    # **Two tasks, not one.** The first is the handoff; the second is this
    # batch's, and without it the second refusal leaves no trace anyone reads.
    assert len(tasks) == 2
    assert tasks[-1]["reason_code"] == "UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL"
    # Filed under the governor that actually stopped the work, not under the
    # target stack -- otherwise it lands in the wrong pile for anyone counting
    # where cost stops conversations.
    assert tasks[-1]["source"] == "cost_governor"
    # Merchant-facing, deliberately not pushed: somebody is already answering
    # this customer, so there is nothing to page a manager about right now.
    assert tasks[-1]["notify"] == "none"
    # The half of the old assertion that still holds: understanding may run, but
    # nothing is planned, generated or sent.
    assert intents is not None and intents["total"] == 0
    assert outbox is not None and outbox["total"] == 0

    # **A third message must not open a third task** (ADR-0058 §二.3, from
    # independent review F4). The idempotency key carries the event id, so
    # de-duplication cannot help; without suppression a long takeover buries the
    # merchant's task list, and that list is the only channel §三.6 gives this
    # condition.
    accepted_again = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"third-{uuid4()}",
            sender_id=sender_of(conversation_id),
            text="hello? anyone?",
        ),
    )
    assert accepted_again.accepted is True
    third = processor.process_one(block_ms=500)
    assert third is not None and third.status == "no_send"

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        tasks_after = connection.execute(
            "SELECT count(*) AS total FROM intent_records"
            " WHERE conversation_id = %s AND kind = 'manual_task'",
            (conversation_id,),
        ).fetchone()
        suppressed = connection.execute(
            "SELECT context, modifications, obligations FROM policy_decisions"
            " WHERE conversation_id = %s ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
    assert tasks_after is not None and tasks_after["total"] == 2, (
        "the third message must be suppressed, not filed"
    )
    # Suppressed, not dropped: the obligation is gone for this turn because the
    # task already exists, and the count is what says how often it happened.
    assert suppressed is not None
    assert "create_manual_task" not in list(suppressed["obligations"])


def test_a_tenant_override_is_stored_and_read_back(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with Database(settings.database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            """
            INSERT INTO tenant_cost_ceilings(tenant_id, hard_daily_cny)
            VALUES (%s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET hard_daily_cny = EXCLUDED.hard_daily_cny
            """,
            (tenant_id, Decimal("7.5")),
        )
    governor = CostGovernor(
        database=Database(settings.worker_database_url), release=priced_release()
    )
    override = governor.tenant_override(tenant_id)
    assert override is not None
    assert override["hard_daily_cny"] == Decimal("7.5000")


def test_oversize_model_input_is_rejected_before_transport_or_ledger_reservation_e2e(
    settings: Settings,
    seeded_database: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FASTAGENT_MODEL_API_KEY_DEEPSEEK", "test-only-not-a-real-key")
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings,
        seeded_database=seeded_database,
        inference=DeterministicMockInference(),
    )
    governor = CostGovernor(
        database=Database(settings.worker_database_url), release=priced_release()
    )
    transport = RecordingModelTransport({})
    client = OpenAICompatibleChatClient(transport=transport)
    context = CallContext(tenant_id, conversation_id, uuid4())

    with pytest.raises(ProviderInputTooLarge, match="contract limit"):
        client.complete(
            binding=governor.release.role("GENERATE_VALUE"),
            system_prompt="system",
            user_prompt="🚀" * MAX_MODEL_INPUT_BYTES,
            governor=governor,
            context=context,
        )

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        ledger_rows = connection.execute(
            "SELECT count(*) AS n FROM model_invocation_costs WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()["n"]
    assert transport.calls == 0
    assert ledger_rows == 0


def test_partial_provider_usage_keeps_real_ledger_reservation_unknown_e2e(
    settings: Settings,
    seeded_database: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FASTAGENT_MODEL_API_KEY_DEEPSEEK", "test-only-not-a-real-key")
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings,
        seeded_database=seeded_database,
        inference=DeterministicMockInference(),
    )
    governor = CostGovernor(
        database=Database(settings.worker_database_url), release=priced_release()
    )
    transport = RecordingModelTransport(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok": true}'}],
                }
            ],
            "usage": {"input_tokens": 23},
        }
    )
    client = OpenAICompatibleChatClient(transport=transport)
    client.complete(
        binding=governor.release.role("GENERATE_VALUE"),
        system_prompt="system",
        user_prompt="user",
        governor=governor,
        context=CallContext(tenant_id, conversation_id, uuid4()),
    )

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            """SELECT status, input_tokens, output_tokens, cost_cny
                 FROM model_invocation_costs WHERE conversation_id = %s""",
            (conversation_id,),
        ).fetchone()
    assert transport.calls == 1
    assert row == {
        "status": "estimated",
        "input_tokens": None,
        "output_tokens": None,
        "cost_cny": None,
    }


def test_closing_a_task_lets_the_next_refusal_file_a_new_one(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**The seam between three batches, and it had nothing on it** (K18 链级独立审查 P1-1).

    Migration 034 moved the suppressor's question from "is the *intent* still
    `PROPOSED`" to "is the *task* still `open` or `claimed`", and the reason is
    written in `pipeline.py`: an intent stays `PROPOSED` forever, because
    resolving a task is not an act on the saga. So an intent-side probe would go
    on suppressing against work that is already done, and the next refusal would
    leave no task at all.

    **Nothing asserted that.** The independent review mutated the predicate back
    -- letting terminal tasks keep suppressing -- and the full suite stayed
    green at 1380. It could not have been caught in the batch that made the
    change: **nothing could put a task into a terminal state until `resolve` and
    `cancel` shipped two batches later**, which is exactly how a three-batch
    chain loses an assertion in the gap between its parts.

    The terminal state is written straight to the table rather than through
    `POST /manual-tasks/{id}/resolve`. The route has its own tests; what has
    never been tested is the **engine** reading the state a person left behind,
    and driving it through the API would make this case fail for the route's
    reasons as well as its own.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    inference = CountingCeilingBlockedInference()
    conversation_id, first = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=inference
    )
    assert first is not None and first.status == "no_send"

    def _say(text: str) -> None:
        ingress = WeComProxyIngress(
            database=Database(settings.ingress_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            runtime_deadline_seconds=settings.runtime_deadline_seconds,
        )
        assert (
            ingress.accept(
                config_id="config-a",
                path_token=str(seeded_database["channel_token"]),
                payload=callback(
                    external_event_id=f"suppressor-{uuid4()}",
                    sender_id=sender_of(conversation_id),
                    text=text,
                ),
            ).accepted
            is True
        )
        result = InboxProcessor(
            database=Database(settings.worker_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            inference=inference,
            worker_id="suppressor-worker",
        ).process_one(block_ms=500)
        assert result is not None and result.status == "no_send"

    def _halted_tasks() -> list[dict[str, Any]]:
        with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
            return connection.execute(
                """
                SELECT id, status FROM manual_tasks
                 WHERE tenant_id = %s AND conversation_id = %s AND reason_code = %s
                 ORDER BY created_at
                """,
                (tenant_id, conversation_id, "UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL"),
            ).fetchall()

    # One refusal under human control, one task. (ADR-0058 §二.3: one task per
    # takeover, not one per message.)
    _say("is a human there?")
    assert len(_halted_tasks()) == 1

    # The second refusal is suppressed *because the task is still open* -- the
    # existing behaviour, asserted here so the third step means something.
    _say("hello? anyone?")
    open_tasks = _halted_tasks()
    assert len(open_tasks) == 1, "an open task suppresses the next refusal"

    # A person deals with it. Written directly, for the reason in the docstring.
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            """
            UPDATE manual_tasks
               SET status = 'resolved', resolved_at = now(), updated_at = now()
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, open_tasks[0]["id"]),
        )

    # **The assertion the chain was missing.** With nothing open, the next
    # refusal must reach somebody again -- and under the pre-034 question, or
    # under a predicate that counts terminal tasks, this stays at one and the
    # customer waits on a queue nobody is looking at.
    _say("still waiting")
    after = _halted_tasks()
    assert len(after) == 2, "a resolved task must not suppress the next refusal"
    assert [row["status"] for row in after] == ["resolved", "open"]


def test_an_over_age_reservation_counts_as_abandoned_without_the_reconciler(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """T55 / ADR-0040, against a real database, and the point of the design.

    The gate ages rows in SQL rather than waiting for `reconcile_abandoned` to
    have moved them. A gate that only worked once a cron entry existed would
    report "healthy" on every deployment where nobody installed it -- which is
    exactly how the Stage collector spent a day pushing seven metrics while
    everyone believed it pushed ten.

    So: insert reservations old enough to be abandoned, run **no reconciler**,
    and the ceiling must already refuse.
    """

    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS, UNSETTLED_BACKLOG_REASON_CODE

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    database = Database(settings.worker_database_url)
    release = priced_release(max_unsettled_calls_conversation=2)
    governor = CostGovernor(database=database, release=release)
    binding = release.role("GENERATE_VALUE")
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    stale = datetime.now(UTC) - timedelta(seconds=RECONCILE_AFTER_SECONDS + 60)
    for _ in range(3):
        governor.open_row(
            binding=binding,
            context=context,
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("0.01"),
            prompt_release="reconciler-test",
            occurred_at=stale,
        )

    decision = governor.precheck(
        binding=binding, context=context, estimated_cost=Decimal("0.01")
    )
    assert decision.effect == "deny", "three abandoned rows must trip a limit of two"
    assert decision.reason_code == UNSETTLED_BACKLOG_REASON_CODE
    assert "0 call(s) still in flight" in str(decision.inactive_reason)

    # A fresh reservation is in flight, not abandoned, and does not add to the
    # count -- which is the distinction the whole batch exists to draw.
    governor.open_row(
        binding=binding,
        context=context,
        estimated_input=10,
        estimated_output=10,
        estimated_cost=Decimal("0.01"),
        prompt_release="reconciler-test",
        occurred_at=datetime.now(UTC),
    )
    with_fresh = governor.precheck(
        binding=binding, context=context, estimated_cost=Decimal("0.01")
    )
    assert "1 call(s) still in flight" in str(with_fresh.inactive_reason)


def test_the_age_boundary_is_exactly_where_the_constant_says(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """899 / 900 / 901 seconds, against the real query.

    The contract test that was meant to guard this compared an expression with
    itself and documented the opposite of the SQL. `occurred_at <= now - 900s`
    means a row of **exactly** 900 seconds is already abandoned -- which is
    checkable only by asking the database.
    """

    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    governor = CostGovernor(
        database=Database(settings.worker_database_url), release=priced_release()
    )
    binding = governor.release.role("GENERATE_VALUE")
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    now = datetime.now(UTC)
    ages = (
        RECONCILE_AFTER_SECONDS - 1,
        RECONCILE_AFTER_SECONDS,
        RECONCILE_AFTER_SECONDS + 1,
    )
    for offset in ages:
        governor.open_row(
            binding=binding,
            context=context,
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("0.01"),
            prompt_release="boundary-test",
            occurred_at=now - timedelta(seconds=offset),
        )

    # The row at exactly the threshold counts as abandoned; only the younger one
    # is still in flight.
    assert governor.abandoned_reservations(tenant_id=tenant_id, now=now) == 2


def test_the_reconciler_leaves_other_tenants_alone(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """Tenant B's rows are not tenant A's to move.

    The write path is guarded twice -- RLS through `transaction(tenant_id=...)`
    and a `tenant_id = %s` inside the UPDATE -- so the risk is low. This is the
    evidence, which the full-process checklist asks for and the batch shipped
    without.
    """

    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    database = Database(settings.worker_database_url)
    governor = CostGovernor(database=database, release=priced_release())
    binding = governor.release.role("GENERATE_VALUE")
    stale = datetime.now(UTC) - timedelta(seconds=RECONCILE_AFTER_SECONDS + 60)

    # `_start_conversation` goes through the ingress, and the ingress needs a
    # channel binding -- the fixture gives one only to tenant A. The aggregate is
    # inserted directly instead of widening a fixture every other e2e depends on;
    # what this test needs from a conversation is only that the ledger's foreign
    # key resolves.
    release_id = UUID(str(seeded_database["profile_release"]))
    conversations = {tenant_a: uuid4(), tenant_b: uuid4()}
    for tenant_id, conversation_id in conversations.items():
        with database.transaction(tenant_id=tenant_id) as connection:
            connection.execute(
                """
                INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)
                VALUES (%s, %s, %s)
                """,
                (tenant_id, conversation_id, release_id),
            )

    rows: dict[UUID, Any] = {}
    for tenant_id in (tenant_a, tenant_b):
        conversation_id = conversations[tenant_id]
        rows[tenant_id] = governor.open_row(
            binding=binding,
            context=CallContext(tenant_id, conversation_id, uuid4()),
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("0.01"),
            prompt_release="tenant-isolation-test",
            occurred_at=stale,
        )

    moved = governor.reconcile_abandoned(tenant_id=tenant_a)

    with database.transaction(tenant_id=tenant_b) as connection:
        status = connection.execute(
            "SELECT status FROM model_invocation_costs WHERE id = %s", (rows[tenant_b],)
        ).fetchone()["status"]
    assert status == "estimated", "reconciling one tenant must not touch another's ledger"
    assert moved >= 1


def test_the_projected_sums_do_not_change_when_rows_are_reconciled(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """P1-2: whether the reconciler has run must not move the money judgement.

    Before the fix, an abandoned row's estimate vanished from `projected_*` the
    moment it was moved -- so running the reconciler could turn a warning into
    silence. Four abandoned rows at the provable per-call maximum come to
    0.283 CNY against a `hard_conversation` of 0.27, which is precisely a
    warning that would have disappeared.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    database = Database(settings.worker_database_url)
    governor = CostGovernor(database=database, release=priced_release())
    binding = governor.release.role("GENERATE_VALUE")
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS

    stale = datetime.now(UTC) - timedelta(seconds=RECONCILE_AFTER_SECONDS + 60)
    for _ in range(3):
        governor.open_row(
            binding=binding,
            context=context,
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("0.07"),
            prompt_release="projection-test",
            occurred_at=stale,
        )

    with database.transaction(tenant_id=tenant_id) as connection:
        before = governor._snapshot(
            connection, context=context, occurred_at=datetime.now(UTC)
        )
    governor.reconcile_abandoned(tenant_id=tenant_id)
    with database.transaction(tenant_id=tenant_id) as connection:
        after = governor._snapshot(
            connection, context=context, occurred_at=datetime.now(UTC)
        )

    assert before.projected_conversation == after.projected_conversation
    assert before.projected_daily == after.projected_daily
    assert before.projected_monthly == after.projected_monthly
    assert before.abandoned_conversation == after.abandoned_conversation


def test_the_reconciler_moves_only_the_over_age_rows_and_invents_no_amount(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The UPDATE itself, run for real.

    It must move the abandoned rows, leave the in-flight one alone, stamp
    `reconciled_at`, and **write no cost** -- the amount is the one thing this
    system cannot honestly decide.
    """

    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, _ = _start_conversation(
        settings=settings, seeded_database=seeded_database, inference=DeterministicMockInference()
    )
    database = Database(settings.worker_database_url)
    governor = CostGovernor(database=database, release=priced_release())
    binding = governor.release.role("GENERATE_VALUE")
    context = CallContext(
        tenant_id=tenant_id, conversation_id=conversation_id, correlation_id=uuid4()
    )

    stale = datetime.now(UTC) - timedelta(seconds=RECONCILE_AFTER_SECONDS + 60)
    stale_rows = [
        governor.open_row(
            binding=binding,
            context=context,
            estimated_input=10,
            estimated_output=10,
            estimated_cost=Decimal("0.01"),
            prompt_release="reconciler-test",
            occurred_at=stale,
        )
        for _ in range(2)
    ]
    fresh_row = governor.open_row(
        binding=binding,
        context=context,
        estimated_input=10,
        estimated_output=10,
        estimated_cost=Decimal("0.01"),
        prompt_release="reconciler-test",
        occurred_at=datetime.now(UTC),
    )

    assert governor.abandoned_reservations(tenant_id=tenant_id) == 2
    assert governor.reconcile_abandoned(tenant_id=tenant_id) == 2
    # Idempotent: a second run has nothing left to move.
    assert governor.reconcile_abandoned(tenant_id=tenant_id) == 0

    with database.transaction(tenant_id=tenant_id) as connection:
        rows = {
            row["id"]: row
            for row in connection.execute(
                """SELECT id, status, cost_cny, settled_at, reconciled_at
                     FROM model_invocation_costs WHERE conversation_id = %s""",
                (conversation_id,),
            ).fetchall()
        }

    for row_id in stale_rows:
        row = rows[row_id]
        assert row["status"] == "unreconciled"
        assert row["reconciled_at"] is not None
        assert row["cost_cny"] is None, "the amount is unknown; recording one would be a lie"
        assert row["settled_at"] is None, "settled_at means the supplier answered"

    assert rows[fresh_row]["status"] == "estimated"
    assert rows[fresh_row]["reconciled_at"] is None
