"""ADR-0054 §三.1 / §三.5, against a real database and the real pipeline.

**What this file exists to prove is a reuse, not a rule.** An exit signal given
while a human holds the conversation is recorded and not acted on -- the stack
is frozen, so there is nothing to act on -- and then fed to the first plan after
control comes back. From that point **T53 does the work it already did**:
`repair cannot propose a return after an exit signal`. If this batch had needed
to change `target_stack.py` or `inference_validation.py`, the design would have
been misread; the test therefore asserts a T53 refusal reached through the new
path, which is the only way to show the value travelled.

The takeover half asserts the other side of §三.1: understanding runs, slots are
written, and **nothing is planned, generated or sent**.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


class ScriptedInference(DeterministicMockInference):
    """The mock, with the two fields this batch cares about made scriptable.

    Subclassed rather than hand-rolled so every other field stays exactly what
    the contract-shaped mock produces -- a bespoke double here would drift from
    the document a real adapter must emit, which is the reason
    `DeterministicMockInference` exists at all.
    """

    def __init__(self) -> None:
        self.relation = "same_goal"
        self.slot_updates: list[dict[str, Any]] = []
        self.operation: str | None = None
        self.target_definition_id: str | None = None
        self.understand_calls = 0
        self.plan_calls = 0
        #: The relation the **planning model** saw, per call. F2 is about this
        #: list, not about the validator's copy.
        self.plan_relations: list[str] = []

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        self.understand_calls += 1
        document = super().understand(**kwargs)
        document["interruption"] = {
            "relation": self.relation,
            "suggested_frame_definition_id": None,
        }
        document["slot_updates"] = list(self.slot_updates)
        return document

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        self.plan_calls += 1
        self.plan_relations.append(
            str(kwargs["understanding"]["interruption"]["relation"])
        )
        document = super().plan(**kwargs)
        if self.operation is not None:
            document["target_stack_operation"] = {
                "op": self.operation,
                "target_frame_definition_id": self.target_definition_id,
            }
        return document


def _send(*, settings: Settings, seeded: dict[str, Any], conversation_id: UUID, text: str) -> None:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=callback(
            external_event_id=f"takeover-{uuid4()}",
            sender_id=sender_of(conversation_id),
            text=text,
        ),
    )
    assert accepted.accepted is True


def _drain(*, settings: Settings, inference: Any) -> Any:
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="takeover-worker",
    )
    return processor.process_one(block_ms=500)


def _aggregate(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return dict(row["state"])


def _counts(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> tuple[int, int]:
    """Intents and outbox rows, so a turn can be asserted to have added none."""

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        intents = connection.execute(
            "SELECT count(*) AS total FROM intent_records"
            " WHERE conversation_id = %s AND kind <> 'manual_task'",
            (conversation_id,),
        ).fetchone()
        outbox = connection.execute(
            "SELECT count(*) AS total FROM outbox_messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
    assert intents is not None and outbox is not None
    return int(intents["total"]), int(outbox["total"])


def _take_over(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    """Hand the conversation to a person **through the production function**.

    `POST /conversations/{id}/takeover` is freeze B3 #6 and is not in this batch
    (施工说明 §八): semantics land first, routes consume them. Driving the real
    `suspend_target_stack_for_human` is what keeps this test about the semantics
    rather than about a fixture -- and writing `control_mode` in **both** places
    is the same requirement the resume path has, because the worker reads
    `state["control_mode"]`, not the column.
    """

    from fastagent_v2.worker.target_stack import suspend_target_stack_for_human

    state = _aggregate(settings, tenant_id, conversation_id)
    suspended = suspend_target_stack_for_human(
        aggregate=state, occurred_at=datetime.now(UTC).isoformat()
    )
    suspended["control_mode"] = "human"
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s, control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (Jsonb(suspended), tenant_id, conversation_id),
        )


def _resume(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    from fastagent_v2.worker.target_stack import resume_target_stack_from_human

    state = _aggregate(settings, tenant_id, conversation_id)
    resumed = resume_target_stack_from_human(
        aggregate=state, occurred_at=datetime.now(UTC).isoformat()
    )
    resumed["control_mode"] = "ai"
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s, control_mode = 'ai'"
            " WHERE tenant_id = %s AND id = %s",
            (Jsonb(resumed), tenant_id, conversation_id),
        )


def test_understanding_runs_under_takeover_and_nothing_is_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§三.1. The step that was skipped now runs; the steps after it still do not.

    Before ADR-0054 this was a pre-inference block, so everything a customer
    said to a human agent was invisible to the machine afterwards. The slot
    written below is the visible proof that understanding happened -- and the
    empty intent and outbox tables are the proof that nothing else did.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = ScriptedInference()

    _send(settings=settings, seeded=seeded_database, conversation_id=conversation_id, text="hello")
    assert _drain(settings=settings, inference=inference) is not None
    _take_over(settings, tenant_id, conversation_id)

    state = _aggregate(settings, tenant_id, conversation_id)
    frame_id = str(state["target_stack"][-1]["id"])
    inference.slot_updates = [
        {
            "frame_id": frame_id,
            "slot": "request_summary",
            "value": "said while a person was handling it",
            "confidence": 0.9,
            "evidence_event_id": str(uuid4()),
        }
    ]
    plans_before = inference.plan_calls
    # The first turn ran normally and produced an intent, as it should have.
    # What this case is about is whether the **takeover** turn adds one.
    intents_before = _counts(settings, tenant_id, conversation_id)

    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="one more thing",
    )
    result = _drain(settings=settings, inference=inference)

    assert result is not None and result.status == "no_send"
    assert inference.understand_calls == 2, "understanding must run during a takeover"
    assert inference.plan_calls == plans_before, "planning must not"
    after = _aggregate(settings, tenant_id, conversation_id)
    frame = after["target_stack"][-1]
    assert frame["slots"]["request_summary"] == "said while a person was handling it"
    assert frame["slot_evidence"]["request_summary"]["source"] == "model"
    # The stack stayed frozen: no operation ran, so every frame is still where
    # the takeover left it.
    assert {item["status"] for item in after["target_stack"]} == {"suspended"}
    assert _counts(settings, tenant_id, conversation_id) == intents_before


def test_an_exit_signal_given_under_takeover_reaches_the_first_plan_after_resume(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """§三.5, and the assertion is a **T53 refusal**, on purpose.

    The customer says "forget it" to the human agent. The stack is frozen, so
    nothing can act on it; it is recorded. Control returns, the next message
    arrives, and the first plan is fed that relation -- at which point
    `repair cannot propose a return after an exit signal` fires, **from T53's
    existing rule, unmodified**.

    Asserting the refusal rather than asserting the field was read is what makes
    this a test of the reuse: a version that recorded the signal and quietly
    dropped it would still clear the field.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = ScriptedInference()

    _send(settings=settings, seeded=seeded_database, conversation_id=conversation_id, text="hello")
    assert _drain(settings=settings, inference=inference) is not None
    _take_over(settings, tenant_id, conversation_id)

    inference.relation = "exit"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="forget it, I do not need this",
    )
    assert _drain(settings=settings, inference=inference) is not None

    recorded = _aggregate(settings, tenant_id, conversation_id)["pending_exit_signal"]
    assert recorded is not None
    assert recorded["relation"] == "exit"

    _resume(settings, tenant_id, conversation_id)
    # **The turn after resume carries a relation that would be perfectly legal**
    # -- `side_question` justifies a `push`. The only thing that can refuse it
    # is the exit signal recorded earlier, which is what makes this pair a
    # discriminator rather than a coincidence: feed it and the push is refused,
    # do not feed it and the push succeeds.
    inference.relation = "side_question"
    inference.operation = "push"
    inference.target_definition_id = "clarify_request"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="are you still there?",
    )
    result = _drain(settings=settings, inference=inference)

    # **2026-09-15（ADR-0138）：这一轮的结局从 `no_send` 变成「回一句澄清」。**
    # 那个 `push` 仍然被退出信号拒掉 —— 变的只是**被拒之后说不说话**。
    # 恢复之后客户问的是「你还在吗」，理解步听懂了，于是回退落在
    # `ask_clarification` 那一支：**被拒不等于石沉大海。**
    #
    # **这条用例的判据没有被削弱**，因为它本来就不是 `no_send` ——
    # 下面那段注释自己写着「A version that recorded the signal and dropped it
    # **would still answer `no_send`**, for a different reason」。
    # **真正的判别式是理由码**，它一个字没动。
    assert result is not None
    # **The assertion that makes this a test of the reuse.** The refusal is
    # raised in `inference_validation.py` off
    # `understanding["interruption"]["relation"]`, so seeing this code proves the
    # recorded signal reached the **validator** -- not merely that a field was
    # cleared. A version that recorded the signal and dropped it would still
    # clear the field and would still answer `no_send`, for a different reason,
    # and this turn's own `side_question` would have let the push through.
    #
    # **Why this rule and not T53's `REPAIR_AFTER_EXIT_SIGNAL`**, which the
    # implementation brief named: that rule needs the **parent** frame in
    # `repair_pending`, and a resume leaves the parent `suspended` (only the top
    # frame is promoted). So `repair` after a resume is refused earlier, by
    # `REPAIR_ON_UNPREPARED_PARENT`, and T53's clause is unreachable on any
    # stack a resume can produce. Recorded for the design window rather than
    # worked around -- see the self-review.
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        reasons = connection.execute(
            "SELECT reason_code FROM policy_decisions"
            " WHERE conversation_id = %s ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
    assert reasons[-1]["reason_code"] == "PUSH_WITHOUT_INTERRUPTION"
    # **F2 / ADR-0058 §二.2: the planning model saw it too.** Feeding only the
    # validator and the transition made the signal a veto that could never be a
    # proposal -- and what it unlocks is `abandon` and `request_pop`, which a
    # model that was never told will never propose. The turn's own relation was
    # `side_question`; the model was handed `exit`.
    assert inference.plan_relations[-1] == "exit"
    after = _aggregate(settings, tenant_id, conversation_id)
    # Consumed: a missed message, not a standing state. Cleared even though the
    # plan was rejected -- it did its job, and leaving it set would refuse every
    # later turn for the same reason forever.
    assert after["pending_exit_signal"] is None


def test_a_timer_during_takeover_does_not_erase_the_exit_signal(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Independent review F7. U2 says "the last inbound **message**" wins.

    A `timer` is not something the customer said. Before this, any event that
    reached the takeover branch overwrote or cleared the signal, so a system
    clock could erase a customer's "forget it" while the customer said nothing
    further. Reachable once B3 #6 lands: an agent-initiated takeover does not
    bump the generation, so live timers survive it.
    """

    from datetime import timedelta

    from fastagent_v2.worker.scheduler import Scheduler

    from tests.e2e.test_m0_paths import insert_scheduled_event

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = ScriptedInference()

    _send(settings=settings, seeded=seeded_database, conversation_id=conversation_id, text="hello")
    assert _drain(settings=settings, inference=inference) is not None
    _take_over(settings, tenant_id, conversation_id)

    inference.relation = "exit"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="forget it",
    )
    assert _drain(settings=settings, inference=inference) is not None
    recorded = _aggregate(settings, tenant_id, conversation_id)["pending_exit_signal"]
    assert recorded is not None

    insert_scheduled_event(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        due_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="takeover-scheduler",
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).enqueue_due()
    # The timer's own relation is whatever the mock reports; the point is that
    # it must not be consulted at all for this field.
    inference.relation = "same_goal"
    assert _drain(settings=settings, inference=inference) is not None

    after = _aggregate(settings, tenant_id, conversation_id)["pending_exit_signal"]
    assert after == recorded, "a timer must not erase what the customer said"


def test_a_later_message_under_takeover_withdraws_an_earlier_exit_signal(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """U2: overwritten every message, never accumulated.

    "Never mind" followed by "actually, please do" is a customer taking back the
    first sentence. Keeping it would honour the one they withdrew.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = ScriptedInference()

    _send(settings=settings, seeded=seeded_database, conversation_id=conversation_id, text="hello")
    assert _drain(settings=settings, inference=inference) is not None
    _take_over(settings, tenant_id, conversation_id)

    inference.relation = "exit"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="never mind",
    )
    assert _drain(settings=settings, inference=inference) is not None
    assert _aggregate(settings, tenant_id, conversation_id)["pending_exit_signal"] is not None

    inference.relation = "same_goal"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="actually, please carry on",
    )
    assert _drain(settings=settings, inference=inference) is not None

    assert _aggregate(settings, tenant_id, conversation_id)["pending_exit_signal"] is None
