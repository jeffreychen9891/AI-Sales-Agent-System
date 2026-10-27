"""A timer whose target frame has finished is invalidated. **B4-4e / ADR-0114 / T226.**

It needs a real `scheduled_events` row that a real rule produced, so it drives
the seeded conversation through the pipeline and the dispatcher first -- B4-2 is
what made that possible; before it a test of this kind had to insert its own
fixture row and would have proved nothing about the path.

**`cancel_on: frame_exit` was the one value with no cancellation path at all**
(T226, as corrected by that batch's independent review -- `human_takeover` and
`profile_repin` both had one, and `commitment_fulfilled` is unreachable in M1b).
The user ruled it be collected by this consumption-side re-check.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]



class AcceptingAdapter:
    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _deliver_one(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=AcceptingAdapter(),
        dispatcher_id="b44e-seed",
    )
    assert dispatcher.dispatch_one() is not None


def _fire_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the delivery left no pending timer to fire"
    scheduler = Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="b44e-scheduler",
        runtime_deadline_seconds=600,
    )
    assert scheduler.enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id="b44e-worker",
    ).process_one(block_ms=1000)


def _timer_row(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    """The conversation's one scheduled event, and it asserts there is only one.

    **`fetchone()` without an ordering picks an arbitrary row** (independent
    review 2026-09-09, P3-2), and a conversation may carry several -- the shipped
    `rental` frame declares three `time_rules`. It happens to be one row here;
    asserting that is cheaper than an `ORDER BY` that would still be arbitrary,
    and it fails loudly the day a fixture grows a second timer.

    **Same shape B4-4c hit in its own §5.5**, where a `fetchone()` over two rows
    sharing a reason code returned the one without the field being asserted.
    """

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT status, invalidation_reason FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchall()
    assert len(rows) == 1, f"expected exactly one timer for this conversation, got {rows}"
    return dict(rows[0])


def test_a_timer_whose_target_frame_stopped_being_active_is_invalidated(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T226's `frame_exit`, collected here by the user's ruling of 2026-09-09.**

    The generation check that already existed catches a customer who spoke
    again. It does **not** catch a frame that finished: a `goal_stall` armed
    three days ago against a goal the conversation completed yesterday would
    wake up, plan, and chase it.

    **Keyed on `target_frame_id`, per ADR-0114**, which revises ADR-0098 §二's
    「按 pin 判」: a frame id is a fresh `uuid4()` per frame, so it already
    answers 「这个 timer 是为哪一代帧排的」, and `scheduled_events` has no
    frame-generation column to compare against anyway.

    The frame is moved to `completed` rather than removed, because that is the
    ordinary ending and it is the half a "does the id still appear" check would
    miss -- history stays on the stack.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        frame = next(item for item in state["target_stack"] if item["status"] == "active")
        frame["status"] = "completed"
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()

    _fire_the_timer(settings, tenant_id, conversation_id)

    timer = _timer_row(tenant_id, conversation_id)
    assert timer["status"] == "INVALIDATED"
    assert timer["invalidation_reason"] == "TARGET_FRAME_EXITED"
    with _migration_connection() as connection:
        outbox = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND command ? 'proactivity'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert outbox is not None and outbox["n"] == 0, (
        "an invalidated timer must not reach the model, let alone the outbox"
    )


def test_a_frame_a_human_suspended_is_not_a_frame_that_exited(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half of the predicate, and the half a first attempt got wrong.

    An earlier version of this check asked `status != "active"` -- **which is
    what ADR-0114 §二.1 literally says** -- and that also matches `suspended`,
    the state a human takeover leaves on every resumable frame. A timer would
    then be invalidated because a person picked up the conversation, and the
    customer's own exit signal would go with it. The clean-checkout gate caught
    it via `test_a_timer_during_takeover_does_not_erase_the_exit_signal`, an
    existing case about something else entirely.

    **`suspended` is a live state**: the conversation resumes and the frame comes
    back. Only `TERMINAL_FRAME_STATUSES` -- and disappearing from the stack --
    mean the frame is done.

    **The state is arranged the way production arranges it** (independent review
    2026-09-09, P1-2): `suspended` is written by `suspend_target_stack_for_human`,
    which runs **together with** `control_mode = 'human'`. The first version of
    this case set `suspended` while leaving control on `ai` -- a combination
    production never produces -- and it blew up two layers downstream, at
    `KEEP_ON_INACTIVE_FRAME`, which has nothing to do with what it claims to
    prove. **The assertion below is on `scheduled_events`, so it holds whether
    the turn is handed to `_process_under_human_control` or not**: what it says
    is that this predicate did not fire.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        frame = next(item for item in state["target_stack"] if item["status"] == "active")
        frame["status"] = "suspended"
        # **Both halves of what a takeover writes**, because writing one without
        # the other is a state the engine cannot reach.
        state["control_mode"] = "human"
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s, control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()

    _fire_the_timer(settings, tenant_id, conversation_id)

    timer = _timer_row(tenant_id, conversation_id)
    assert timer["status"] != "INVALIDATED", (
        "a suspended frame is a live frame; the conversation resumes and it comes back"
    )
    assert timer["invalidation_reason"] is None


def test_a_frame_that_left_the_stack_is_gone_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The half `frame_exit` is literally named after, and it had no case.

    The predicate has two halves and the self-review says so: a **terminal**
    frame is still on the stack (history is not discarded), while a **popped**
    frame is not there at all. Both new cases left the frame on the stack
    (independent review 2026-09-09, P2-1), so the `return True` branch -- the
    one that matches the words 「帧退出」 most directly -- was never executed.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _deliver_one(settings, seeded_database, conversation_id)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        target = next(item for item in state["target_stack"] if item["status"] == "active")
        # Pop it and leave a live frame behind, so the turn has somewhere to go
        # and the only thing that changed is that **this** frame is gone.
        state["target_stack"] = [
            dict(item, status="active") for item in state["target_stack"] if item is not target
        ] or [dict(target, id=str(uuid4()))]
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()

    _fire_the_timer(settings, tenant_id, conversation_id)

    timer = _timer_row(tenant_id, conversation_id)
    assert timer["status"] == "INVALIDATED"
    assert timer["invalidation_reason"] == "TARGET_FRAME_EXITED"
