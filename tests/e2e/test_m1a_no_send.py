"""The no-send path: an event that produces no message must still leave a record.

The failure this guards against is the quiet one -- a rejected plan or a provider
outage causing a customer message to disappear with nothing written down.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import AggregateConflict, InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.platform_frames import (
    NO_GOAL_FRAME_DEFINITION_ID as PLATFORM_NO_GOAL_FRAME,
)
from fastagent_v2.worker.providers import ProviderTimeout
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


class OffAllowlistPlanInference(DeterministicMockInference):
    """Proposes a strategy the target frame does not allow."""

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["selected_strategy_id"] = "propose_order"
        document["candidate_intents"] = []
        return document


class OffAllowlistAndUnderstandsNothing(OffAllowlistPlanInference):
    """同上，**而且理解步什么都没提取出来** —— 回退因此仍然静默。

    **ADR-0138 之后这个区别是承重的**：计划被拒时的回退分三支，
    而「理解步也空着」那一支仍然是 `no_message`
    （那时一句「我确认一下」没有可确认的对象，只能是套话）。
    """

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        for key in ("facts", "intents", "slot_updates"):
            document[key] = []
        return document


class TimingOutInference(DeterministicMockInference):
    def understand(self, **_: Any) -> dict[str, Any]:
        raise ProviderTimeout("injected provider timeout", role="UNDERSTAND_VALUE")


class EmptyGenerationInference(DeterministicMockInference):
    """Returns a schema-valid draft that the current executor cannot send."""

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        document = super().generate(**kwargs)
        document["proposed_intents"] = []
        return document


class ContentlessGenerationInference(DeterministicMockInference):
    """The payload a real model returned: `send_message` with no `content` (T110)."""

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        document = super().generate(**kwargs)
        for proposal in document["proposed_intents"]:
            proposal["typed_payload"] = {"message_type": "acknowledgement"}
        return document


class ModelContentShapeInference(DeterministicMockInference):
    """The shapes the **real** model returned in the third real window.

    The deterministic double builds `{"type": "text", "text": ...}` and every
    other case in this file inherits it -- which is precisely why 2000+ cases
    were green while the real model's two other shapes were refused by the
    adapter and three customer replies were lost. This double exists so that
    the producer is driven by something it did not invent.
    """

    def __init__(self, content: dict[str, Any]) -> None:
        self._content = content

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        document = super().generate(**kwargs)
        for proposal in document["proposed_intents"]:
            proposal["typed_payload"] = {"content": dict(self._content)}
        return document


class PushThenWaitInference(DeterministicMockInference):
    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        document["interruption"]["relation"] = "side_question"
        document["interruption"]["suggested_frame_definition_id"] = "clarify_request"
        return document

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["target_stack_operation"] = {
            "op": "push",
            "target_frame_definition_id": "clarify_request",
        }
        document["selected_strategy_id"] = "wait"
        document["candidate_intents"] = []
        document["generation_constraints"] = []
        return document


class StackOperationThenWaitInference(DeterministicMockInference):
    def __init__(self, operation: str) -> None:
        self._operation = operation

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        document["target_stack_operation"] = {
            "op": self._operation,
            "target_frame_definition_id": None,
        }
        document["selected_strategy_id"] = "wait"
        document["candidate_intents"] = []
        document["generation_constraints"] = []
        return document


class CustomerAsksForAPersonInference(StackOperationThenWaitInference):
    """`abandon` on a `human_request` -- the customer asking for a human.

    The relation matters as much as the operation here: `abandon` is legal only
    on an exit signal, and `human_request` versus `exit` is the only thing that
    separates "put me through to someone" from "never mind this one question".
    The 2026-09-04 ruling pushes the first to the manager immediately and holds
    the second back, so a test about pushing has to drive the first.
    """

    def __init__(self) -> None:
        super().__init__("abandon")

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        document["interruption"] = {
            "relation": "human_request",
            "suggested_frame_definition_id": None,
        }
        return document


class CustomerDropsTheQuestionInference(StackOperationThenWaitInference):
    """`abandon` on an `exit` relation -- "never mind this one question".

    The sibling of `CustomerAsksForAPersonInference`, and the distinction is the
    2026-09-04 ruling's: asking for a person is pushed to the manager
    immediately, dropping the question is **produced and held back**. A test
    about holding back has to drive this one.
    """

    def __init__(self) -> None:
        super().__init__("abandon")

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        document["interruption"] = {
            "relation": "exit",
            "suggested_frame_definition_id": None,
        }
        return document


class SlotReportingInference(StackOperationThenWaitInference):
    """Understanding that actually reports a slot, which nothing used to consume."""

    def __init__(
        self, operation: str, slot: str, value: str, confidence: float = 0.95
    ) -> None:
        super().__init__(operation)
        self._slot = slot
        self._value = value
        self._confidence = confidence

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        document = super().understand(**kwargs)
        frame = kwargs["aggregate"]["target_stack"][-1]
        document["slot_updates"] = [
            {
                "frame_id": str(frame["id"]),
                "slot": self._slot,
                "value": self._value,
                "confidence": self._confidence,
                "evidence_event_id": str(kwargs["envelope"]["event_id"]),
            }
        ]
        return document


def _accept_customer_message(
    *,
    settings: Settings,
    seeded_database: dict[str, object],
    conversation_id: UUID,
    occurred_at: str | None = None,
) -> None:
    queue = PointerQueue(settings.queue_redis_url)
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    accepted = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"no-send-{uuid4()}",
            sender_id=sender_of(conversation_id),
            occurred_at=(
                datetime.fromisoformat(occurred_at) if occurred_at else None
            ),
        ),
    )
    assert accepted.accepted is True


def _drive(*, settings: Settings, inference: Any) -> Any:
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="no-send-worker",
    )
    return processor.process_one(block_ms=500)


def _facts(*, settings: Settings, tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        return {
            "inbox_pending": connection.execute(
                "SELECT count(*) AS n FROM inbox_events WHERE status <> 'consumed'"
            ).fetchone()["n"],
            "intents": connection.execute(
                "SELECT count(*) AS n FROM intent_records WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()["n"],
            "outbox": connection.execute(
                "SELECT count(*) AS n FROM outbox_messages WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()["n"],
            "denials": connection.execute(
                """
                SELECT reason_code, intent_id FROM policy_decisions
                 WHERE conversation_id = %s AND effect = 'deny' AND hook = 'post_plan'
                """,
                (conversation_id,),
            ).fetchall(),
            "state_version": connection.execute(
                "SELECT state_version FROM conversation_aggregates WHERE id = %s",
                (conversation_id,),
            ).fetchone()["state_version"],
            "aggregate": connection.execute(
                "SELECT state FROM conversation_aggregates WHERE id = %s",
                (conversation_id,),
            ).fetchone()["state"],
        }


def _audit_rows(conversation_id: UUID) -> list[dict[str, Any]]:
    """Read audit rows with the migration role.

    Runtime roles hold INSERT and nothing else on `audit_events`; that
    append-only grant is a security birth condition, so the test reads through
    the owner rather than relaxing it.
    """

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        return connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE action = 'plan.no_send' AND resource_id = %s
            """,
            (conversation_id,),
        ).fetchall()


def test_a_rejected_plan_is_persisted_as_a_no_send_rather_than_dropped(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    # **理解步也空着的那一支**（ADR-0138 三分的第三支）—— 回退仍然静默。
    # 换成有内容的理解，同一个场景现在会**问出一句**，那条由下面一条用例钉。
    result = _drive(settings=settings, inference=OffAllowlistAndUnderstandsNothing())
    assert result is not None
    assert result.status == "no_send"
    assert result.intent_ids == ()
    assert result.outbox_ids == ()

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["inbox_pending"] == 0, "the event must be consumed, not left to retry forever"
    assert facts["intents"] == 0
    assert facts["outbox"] == 0
    assert facts["state_version"] == 1, "the aggregate still moves forward"

    assert len(facts["denials"]) == 1
    assert facts["denials"][0]["reason_code"] == "STRATEGY_NOT_IN_FRAME_ALLOWLIST"
    assert facts["denials"][0]["intent_id"] is None

    audits = _audit_rows(conversation_id)
    assert len(audits) == 1
    evidence = audits[0]["evidence"]
    assert evidence["plan_origin"] == "safe_fallback"
    assert evidence["strategy_id"] == "no_message"


def test_a_rejected_plan_with_something_understood_asks_instead_of_going_silent(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**ADR-0138 的兑现物，连真栈跑**：听懂了东西却拼不出合法计划时，说一句话。

    ## 为什么这一条必须在 e2e 层

    接线要改**四处**（策略词表 / 按数据选 / 候选意图的 kind / **管线的「让它问出去」**），
    而**第四处的后果只有连库跑才看得见**：前三处全改对时，
    契约层 1,634 条全绿，**而真链路上客户仍然什么都没收到** ——
    策略选了、候选意图造了、生成结果也造了，最后在管线那一行被丢掉，
    **审计里记着 `ask_clarification`，而 `intents == 0`、`outbox == 0`。**

    **那一行的判据先前写的是「点名了缺的槽位」**，它在 T339 当时是
    「说得出内容」的准确代理（那时唯一非静默的回退是 `ask_slot`）；
    `ask_clarification` 的 `required_slot_requests` 恰恰是空的，代理就不准了。
    改成 `not plan.is_silent` —— 用户 2026-09-15 的问法：
    **「一个非静默的回退计划，凭什么不让它出去？」**
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=OffAllowlistPlanInference())
    assert result is not None

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["inbox_pending"] == 0, "事件仍然要被消费掉"
    assert facts["outbox"] == 1, (
        "听懂了东西、计划被拒，而客户什么都没收到 —— 那正是 ADR-0138 要修的形状"
    )

    # **不读 `_audit_rows`** —— 它过滤 `action = 'plan.no_send'`，
    # 而这一条走的正是**不是** no_send 的那条路。第一版在这里 `IndexError`，
    # **而那个红是对的**：它恰恰说明这一轮真的没走静默那条路。
    #
    # 证据改成两头：**计划确实被拒**（理由码留在 `policy_decisions` 里）
    # **而客户确实收到了**（上面那条 `outbox == 1`）。
    # 两头一起才说得清 ADR-0138 修的那件事：**被拒 ≠ 石沉大海**。
    assert any(
        denial["reason_code"] == "STRATEGY_NOT_IN_FRAME_ALLOWLIST"
        for denial in facts["denials"]
    ), f"计划被拒这件事没留痕：{facts['denials']}"


def test_a_provider_failure_is_persisted_as_a_no_send_with_its_reason(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=TimingOutInference())
    assert result is not None
    assert result.status == "no_send"

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["outbox"] == 0
    assert facts["inbox_pending"] == 0
    assert len(facts["denials"]) == 1
    assert facts["denials"][0]["reason_code"] == "PROVIDER_TIMEOUT"
    assert _audit_rows(conversation_id)[0]["evidence"]["plan_origin"] == "provider_failure"


def test_a_schema_valid_but_unsendable_generation_is_consumed_as_no_send(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=EmptyGenerationInference())
    assert result is not None
    assert result.status == "no_send"

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["inbox_pending"] == 0, "an unsupported draft must not retry forever"
    assert facts["intents"] == 0
    assert facts["outbox"] == 0
    assert len(facts["denials"]) == 1
    # T44 changed what an empty generation means. It used to be folded into
    # "the send path requires exactly one proposed intent", which described a
    # restriction; now zero proposals is its own answer -- the model declined to
    # propose anything, which has a different fix from a policy refusal.
    assert facts["denials"][0]["reason_code"] == "GENERATION_PRODUCED_NO_INTENT"


def _queued_content(*, settings: Settings, tenant_id: UUID, conversation_id: UUID) -> Any:
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT command FROM outbox_messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()
    return None if row is None else row["command"]["content"]


@pytest.mark.parametrize(
    "produced",
    [
        pytest.param({"text": "收到，您回复的确认我们已记录。"}, id="bare-text"),
        pytest.param(
            {"segments": [{"type": "text", "content_or_ref": "收到您的消息。"}]},
            id="segments",
        ),
        pytest.param({"type": "text", "text": "您好，已经收到。"}, id="already-canonical"),
    ],
)
def test_what_is_queued_is_the_canonical_shape_whatever_the_model_returned(
    settings: Settings, seeded_database: dict[str, object], produced: dict[str, Any]
) -> None:
    """**The 2026-09-10 outbound defect, from the producer's end.**

    Three shapes went into `outbox_messages` and the adapter accepted one of
    them, so the third real window sent nothing at all. Reading every shape in
    the adapter fixes the symptom; **queueing one shape is what stops the next
    adapter inheriting the problem** -- and it makes true a sentence the
    manager-notification path had been asserting for two batches ("the same
    shape the customer row carries").

    The model's own words are not lost: `intent_records.payload` still holds
    what it returned.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=ModelContentShapeInference(produced))

    assert result is not None
    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["outbox"] == 1, "the reply is queued, not refused"

    queued = _queued_content(
        settings=settings, tenant_id=tenant_id, conversation_id=conversation_id
    )
    assert queued["type"] == "text"
    assert queued["text"], "and it carries the words, not an empty string"
    assert sorted(queued) == ["text", "type"], "one shape on the wire, no leftovers"


def test_content_with_no_words_is_refused_at_the_plan_step_not_at_the_adapter(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**Where an unsendable payload should die.**

    It used to be queued and then refused terminally by the adapter --
    `status='failed'`, which is never retried -- so the customer's reply simply
    never arrived and no manual task said so. `_decide_proposal` refusing it
    costs a `plan.no_send` with a reason code instead, which is the same
    argument T110 made for the guard one step earlier.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(
        settings=settings,
        inference=ModelContentShapeInference({"message_type": "acknowledgement"}),
    )

    assert result is not None
    assert result.status == "no_send"
    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["outbox"] == 0, "nothing may be queued that cannot be sent"
    assert facts["inbox_pending"] == 0, "and the turn still finishes"
    assert [row["reason_code"] for row in facts["denials"]] == ["GENERATION_CONTENT_UNREADABLE"]


def test_a_send_payload_without_content_is_refused_with_a_record(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**T110 end to end**: the turn finishes, and the refusal is written down.

    Before the guard this proposal reached `_persist_accepted_proposal`, whose
    `typed_payload["content"]` raised `KeyError` -- **the whole transaction
    rolled back**, so `inbox_pending` stayed at 1 and the event came back
    through the retry budget to crash again. The assertions that carry this case
    are therefore `inbox_pending == 0` (it was consumed) and the denial row (it
    was explained); `outbox == 0` only says nothing was sent.

    The deterministic mock always returns a `content`, which is exactly why 900+
    cases never touched this path -- so the double here is the fixture that
    makes the real answer reachable.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=ContentlessGenerationInference())

    assert result is not None
    assert result.status == "no_send"

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["inbox_pending"] == 0, "the turn finished; it did not roll back"
    assert facts["intents"] == 0
    assert facts["outbox"] == 0

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        denials = connection.execute(
            """
            SELECT reason_code, context FROM policy_decisions
             WHERE conversation_id = %s AND effect = 'deny' AND hook = 'pre_execute'
            """,
            (conversation_id,),
        ).fetchall()

    assert [row["reason_code"] for row in denials] == ["GENERATION_PAYLOAD_INCOMPLETE"]
    assert denials[0]["context"]["proposal_kind"] == "send_message"

    # **And the row that can actually be counted** (independent review
    # 2026-09-07, P3-2). ADR-0084 Q4's ruling -- fail closed, do not retry, do
    # not repair, **but count it** -- rests on `_persist_no_send` writing an
    # `audit_events` row whose `reason_code` the Stage collector groups by. The
    # `policy_decisions` row above is the explanation; this one is the whole of
    # today's observability, and until B0-5 gives it a series of its own it is
    # the only thing standing behind that "counted".
    # **Read as the migration role, not the worker's.** The worker only holds
    # `INSERT` on `audit_events` (`001`), which is the point of that grant: the
    # engine writes the record and cannot go back and read or edit it. So the
    # test reads it the way an operator would.
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        counted = connection.execute(
            """
            SELECT count(*) AS n FROM audit_events
             WHERE action = 'plan.no_send' AND reason_code = %s
            """,
            ("GENERATION_PAYLOAD_INCOMPLETE",),
        ).fetchone()["n"]

    assert counted == 1, "a refusal that cannot be counted is not 'counted in metrics'"


def test_a_silent_push_is_persisted_through_the_no_send_path(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    result = _drive(settings=settings, inference=PushThenWaitInference())
    assert result is not None and result.status == "no_send"

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    aggregate = facts["aggregate"]
    assert facts["state_version"] == 1
    assert aggregate["generation"] == 1
    assert [frame["status"] for frame in aggregate["target_stack"]] == [
        "suspended",
        "active",
    ]
    assert aggregate["target_stack"][-1]["definition_id"] == "clarify_request"
    assert aggregate["target_stack"][-1]["frame_generation"] == 1
    assert facts["intents"] == 0
    assert facts["outbox"] == 0


def _fill_required_slots(
    *, settings: Settings, tenant_id: UUID, conversation_id: UUID, values: dict[str, str]
) -> None:
    """Fill the top frame's required slots directly.

    Kept after T6 made `slot_updates` writeback real, because this file drives
    the pipeline with `StackOperationThenWaitInference`, whose understanding
    output reports no slots. The tests here are about what the *target stack*
    does once a goal is satisfiable; the writeback itself is covered by
    `tests/contracts/test_slot_writeback_and_closing_frame.py` and by
    `test_slot_updates_from_understanding_reach_the_frame` below.
    """

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = row["state"]
        state["target_stack"][-1]["slots"].update(values)
        connection.execute(
            """
            UPDATE conversation_aggregates SET state = %s, target_stack = %s
             WHERE tenant_id = %s AND id = %s
            """,
            (Jsonb(state), Jsonb(state["target_stack"]), tenant_id, conversation_id),
        )
        connection.commit()


def _reasons(facts: dict[str, Any]) -> set[str]:
    return {str(row["reason_code"]) for row in facts["denials"]}


def test_the_rfc_return_chain_is_persisted_through_successive_no_send_events(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """push -> complete -> repair -> request_pop, as RFC section 4.3 defines it.

    The predecessor of this test drove push -> repair -> request_pop and asserted
    that `repair` left the stack at ["repair_pending", "completed"] -- it had
    frozen the defect (repair as a synonym for complete) into the regression net.
    The corrected chain needs `complete` first, and `repair` now advances the
    parent to resume_confirmation_pending instead of completing the child.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    pushed = _drive(settings=settings, inference=PushThenWaitInference())
    assert pushed is not None and pushed.status == "no_send"

    # `repair` before `complete` is refused, and refused on the no-send path
    # with a reason code rather than raised into the retry budget.
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    premature = _drive(settings=settings, inference=StackOperationThenWaitInference("repair"))
    # **2026-09-15（ADR-0138）：这一段的结局从 `no_send` 变成「回一句澄清」。**
    # 计划仍然被拒、理由码仍然留痕（下面那条断言才是这一段的判据）；
    # 变的是**被拒之后说不说话** —— 理解步这一轮听懂了东西，
    # 于是回退落在 `ask_clarification` 那一支而不是静默。
    #
    # **上面那一段（`PushThenWaitInference`）不受影响**：它的计划没被拒，
    # 是**策略本身静默**（`wait`），而静默那条路一个字没改 ——
    # 这两段并排放着，正好把「被拒」与「静默」这两件事分开。
    assert premature is not None
    premature_facts = _facts(
        settings=settings, tenant_id=tenant_id, conversation_id=conversation_id
    )
    assert "REPAIR_ON_UNPREPARED_PARENT" in _reasons(premature_facts)
    assert [
        frame["status"] for frame in premature_facts["aggregate"]["target_stack"]
    ] == ["suspended", "active"]
    # The double CAS is observable as a strict progression: a refused stack
    # operation still advances the aggregate, because the customer's message was
    # consumed. Restored here -- the rewrite to the RFC chain had dropped it.
    assert premature_facts["state_version"] == 2
    assert premature_facts["aggregate"]["generation"] == 2

    _fill_required_slots(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        values={"routing_detail": "billing"},
    )
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    completed = _drive(settings=settings, inference=StackOperationThenWaitInference("complete"))
    assert completed is not None and completed.status == "no_send"
    completed_facts = _facts(
        settings=settings, tenant_id=tenant_id, conversation_id=conversation_id
    )
    assert [
        frame["status"] for frame in completed_facts["aggregate"]["target_stack"]
    ] == ["repair_pending", "completed"]
    assert completed_facts["state_version"] == 3
    assert completed_facts["aggregate"]["generation"] == 3

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    repaired = _drive(settings=settings, inference=StackOperationThenWaitInference("repair"))
    assert repaired is not None and repaired.status == "no_send"
    repaired_facts = _facts(
        settings=settings, tenant_id=tenant_id, conversation_id=conversation_id
    )
    assert [
        frame["status"] for frame in repaired_facts["aggregate"]["target_stack"]
    ] == ["resume_confirmation_pending", "completed"]
    assert repaired_facts["state_version"] == 4
    assert repaired_facts["aggregate"]["generation"] == 4

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    popped = _drive(settings=settings, inference=StackOperationThenWaitInference("request_pop"))
    assert popped is not None and popped.status == "no_send"
    popped_facts = _facts(
        settings=settings, tenant_id=tenant_id, conversation_id=conversation_id
    )
    assert [
        frame["status"] for frame in popped_facts["aggregate"]["target_stack"]
    ] == ["active"]
    assert popped_facts["state_version"] == 5
    assert popped_facts["aggregate"]["generation"] == 5
    # **这两个是整条会话的累计数，2026-09-15（ADR-0138）从 0 变成 1。**
    #
    # 多出来的那一条来自**上面 `repair` 过早那一轮**：它的计划被拒，
    # 而理解步听懂了东西，于是回退落在 `ask_clarification` 那一支、回了一句话。
    # 其余三轮（`push` / `complete` / `repair` / `request_pop` 成功的那几个）
    # **计划都没被拒**，是策略本身静默（`wait`），那条路一个字没改。
    #
    # **写等值 1 而不是放宽成 `<= 1`**：这一条因此仍然说得出
    # 「整条链上恰好有一轮说了话，而它是被拒的那一轮」——
    # 放宽之后它就只剩「没说太多话」这么一句没什么内容的话了。
    assert popped_facts["intents"] == 1
    assert popped_facts["outbox"] == 1


def _strip_closing_frame_ref(*, settings: Settings, tenant_id: UUID) -> None:
    """Remove the profile's closing frame declaration for this conversation.

    Rewrites the published release row rather than publishing a second release,
    because the point under test is the pipeline's branch, not the publish path.
    The immutability trigger blocks `UPDATE` for runtime roles, so this runs as
    the migration role -- the same escape hatch `_fill_required_slots` uses.
    """

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT id, profile FROM profile_releases WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        assert row is not None
        document = row["profile"]
        document.pop("closing_frame_ref", None)
        connection.execute(
            "ALTER TABLE profile_releases DISABLE TRIGGER USER",
        )
        connection.execute(
            "UPDATE profile_releases SET profile = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(document), tenant_id, row["id"]),
        )
        connection.execute("ALTER TABLE profile_releases ENABLE TRIGGER USER")
        connection.commit()


def test_completing_the_root_frame_enters_the_profiles_closing_frame(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """ADR-0031 changed what a completed root does, and this is the change.

    The original assertion here was that a completed root hands the conversation
    to a person. That was correct *and* free only while `complete` was
    unreachable in the real pipeline (T6): the handoff had never actually fired
    outside this test. Making slot writeback work makes it reachable, and then
    every successfully finished conversation would produce a manual task -- the
    cost the user's T8 decision was taken to avoid.

    The guarantee that must survive is not "always hand off"; it is **never send
    a customer-visible message with no legal intent owner**. A declared closing
    frame supplies that owner, so the handoff is no longer needed here. The
    profile that declares none still hands off -- ADR-0031's hard boundary --
    and that is the test below this one.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    # The aggregate is created lazily on the first processed event, so one
    # `keep` turn has to run before there is a frame whose slots can be seeded.
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    opened = _drive(settings=settings, inference=StackOperationThenWaitInference("keep"))
    assert opened is not None and opened.status == "no_send"
    _fill_required_slots(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        values={"request_summary": "handled"},
    )
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    result = _drive(settings=settings, inference=StackOperationThenWaitInference("complete"))
    assert result is not None

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    stack = facts["aggregate"]["target_stack"]
    assert [frame["status"] for frame in stack] == ["completed", "active"]
    assert stack[-1]["definition_id"] == "closing"
    assert stack[-1]["parent_frame_id"] == stack[0]["id"]

    # **The audit row for *this* branch** (independent review 2026-09-10, P3-2).
    # `conversation.closing_frame_entered` had no assertion anywhere in the
    # repository: removing the closing half of `_write_frame_entry_audit` turned
    # nothing red. The no-goal half was asserted; this is the other one.
    #
    # It also covers P2-1: this turn is **silent** (`StackOperationThenWaitInference`
    # plans `wait`), so it returns through `_persist_no_send` -- which is where
    # the row was being lost while the entry itself was persisted.
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        entered = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s
               AND action = 'conversation.closing_frame_entered'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
    assert len(entered) == 1, "the frame was entered, so the row explaining it must exist"
    assert entered[0]["reason_code"] == "ROOT_TARGET_FRAME_COMPLETED_CLOSING_FRAME_ENTERED"
    assert entered[0]["evidence"]["closing_frame_definition_id"] == "closing"
    # The conversation stays with the machine, and no manual task was produced.
    assert facts["aggregate"]["control_mode"] == "ai"
    assert "ROOT_TARGET_FRAME_COMPLETED" not in _reasons(facts)

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        manual = connection.execute(
            """
            SELECT proposal FROM intent_records
             WHERE tenant_id = %s AND conversation_id = %s AND kind = 'manual_task'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        mode = connection.execute(
            "SELECT control_mode FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert manual == []
    assert mode is not None and mode["control_mode"] == "ai"


def test_a_profile_without_a_closing_frame_enters_the_platforms_no_goal_frame(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**T8's cost, measured end to end at zero** (ADR-0099, B4-7).

    This case used to assert the opposite, and it was right to: ADR-0031's hard
    boundary said a profile with no declared closing frame hands the terminal
    turn to a person rather than sending a message with no legal owner. What
    made that boundary expensive is that `complete` became reachable -- **every
    successfully finished conversation produced a manual task**, 0 to 100%, and
    T8 measured it.

    ADR-0099 keeps the guarantee and drops the cost: the *platform* supplies the
    owner. So the assertions invert -- no manual task, `control_mode` stays
    `ai`, and the stack gains a frame nobody declared -- while the thing the
    guarantee is actually about is asserted directly: **the intent that owns
    the turn is filed under a frame that is on this conversation's stack**.

    The abandoned road is untouched (ADR-0099 §五.3) and has its own case.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    opened = _drive(settings=settings, inference=StackOperationThenWaitInference("keep"))
    assert opened is not None and opened.status == "no_send"
    _fill_required_slots(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        values={"request_summary": "handled"},
    )
    _strip_closing_frame_ref(settings=settings, tenant_id=tenant_id)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    result = _drive(settings=settings, inference=StackOperationThenWaitInference("complete"))
    assert result is not None

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    stack = facts["aggregate"]["target_stack"]
    assert [frame["status"] for frame in stack] == ["completed", "active"]
    assert stack[-1]["definition_id"] == PLATFORM_NO_GOAL_FRAME
    assert stack[-1]["parent_frame_id"] == stack[0]["id"]
    # The conversation stays with the machine, and nobody was handed anything.
    assert facts["aggregate"]["control_mode"] == "ai"
    assert "ROOT_TARGET_FRAME_COMPLETED" not in _reasons(facts)

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        manual = connection.execute(
            """
            SELECT proposal FROM intent_records
             WHERE tenant_id = %s AND conversation_id = %s AND kind = 'manual_task'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        entered = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s
               AND action = 'conversation.no_goal_frame_entered'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        owners = connection.execute(
            """
            SELECT DISTINCT target_frame_id FROM intent_records
             WHERE tenant_id = %s AND conversation_id = %s
            """,
            (tenant_id, conversation_id),
        ).fetchall()
    assert manual == [], "this is the manual task T8 measured, and it is gone"

    # **The audit row, because this branch replaced one that wrote a handoff.**
    # Without it the batch would have removed an audit event and left the stack
    # shape as the only evidence that anything happened.
    assert len(entered) == 1
    assert entered[0]["reason_code"] == "ROOT_TARGET_FRAME_COMPLETED_NO_GOAL_FRAME_ENTERED"
    assert entered[0]["evidence"]["profile_declares_closing_frame"] is False
    assert entered[0]["evidence"]["completed_frame_id"] == stack[0]["id"]

    # **The guarantee ADR-0031 actually made**, asserted rather than assumed:
    # every intent this conversation produced names a frame that is on its
    # stack. Candidate ② would have relaxed this to nullable; this batch does
    # not, and the platform frame is what makes keeping it affordable.
    frame_ids = {str(frame["id"]) for frame in stack}
    for row in owners:
        assert str(row["target_frame_id"]) in frame_ids


def _seed_manager_contact(*, tenant_id: UUID, config_id: str = "config-a") -> None:
    with Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]) as connection:
        connection.execute(
            """
            INSERT INTO tenant_manager_contacts(
                tenant_id, channel, config_id, external_user_id, display_name
            ) VALUES (%s, 'wecom_proxy', %s, 'manager-userid-001', '店长')
            ON CONFLICT (tenant_id) DO UPDATE SET external_user_id = excluded.external_user_id
            """,
            (tenant_id, config_id),
        )
        connection.commit()


def _clear_manager_contact(*, tenant_id: UUID) -> None:
    with Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]) as connection:
        connection.execute(
            "DELETE FROM tenant_manager_contacts WHERE tenant_id = %s", (tenant_id,)
        )
        connection.commit()


def _customer_asks_for_a_person(
    *, settings: Settings, seeded_database: dict[str, object], conversation_id: UUID
) -> None:
    """Drive the one handoff the ruling says to push immediately.

    The customer opens a conversation and then asks for a human: the root frame
    is abandoned on a `human_request`, the stack has nothing workable left, and
    the conversation goes to a person -- with someone waiting at the other end,
    which is why this kind is pushed rather than batched.
    """

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    assert _drive(settings=settings, inference=StackOperationThenWaitInference("keep")) is not None
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    result = _drive(settings=settings, inference=CustomerAsksForAPersonInference())
    assert result is not None and result.status == "no_send"


def test_a_handoff_queues_a_notice_and_it_is_delivered_to_the_manager(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """L2a, both halves of the ruling in one case.

    **Produced**: the manual task now comes with a notice to the merchant's
    manager, and it travels the same chain as a customer message -- intent
    record, outbox row -- rather than a second delivery path built beside it.

    **Delivered, as of B1-5.** This case used to assert the opposite --
    「M1a delivers nothing, internal notices included」 -- and it was right while
    it lasted: the dispatcher was fenced off from this audience by the
    `audience` predicate in `_claim_one`. B1-5 removed that fence, because the
    three conditions the predicate's own docstring set are now met (route by
    `recipient` -- T90; typed `content`; a one-day deadline).

    **The half that did not change is the half worth keeping**: the notice
    travels the same chain as a customer message -- intent record, its own
    policy decision, outbox row -- rather than a second delivery path built
    beside it. Everything above the dispatcher call below is untouched.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _seed_manager_contact(tenant_id=tenant_id)

    _customer_asks_for_a_person(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        notices = connection.execute(
            """
            SELECT o.id, o.status, o.command, i.kind, i.status AS intent_status
              FROM outbox_messages o
              JOIN intent_records i ON i.tenant_id = o.tenant_id AND i.id = o.intent_id
             WHERE o.tenant_id = %s AND o.conversation_id = %s
               AND o.command->>'audience' = 'tenant_manager'
            """,
            (tenant_id, conversation_id),
        ).fetchall()

    assert len(notices) == 1, "one handoff, one notice"

    # P2-1: the notice's own policy decision, not the refusal that produced the
    # manual task. An APPROVED delivery intent whose only evidence is a `deny`
    # is an audit trail that says the opposite of what happened.
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        decision = connection.execute(
            """
            SELECT hook, effect, obligations FROM policy_decisions
             WHERE tenant_id = %s AND intent_id = (
                SELECT intent_id FROM outbox_messages
                 WHERE tenant_id = %s AND id = %s
             )
            """,
            (tenant_id, tenant_id, notices[0]["id"]),
        ).fetchone()
    assert decision is not None, "the notice has no policy decision of its own"
    assert (decision["hook"], decision["effect"]) == ("pre_execute", "allow")
    assert "redact" in decision["obligations"]
    notice = notices[0]
    assert notice["kind"] == "handoff"
    assert notice["intent_status"] == "APPROVED"
    assert notice["status"] == "PENDING"
    recipient = notice["command"]["recipient"]
    assert recipient == {
        "channel": "wecom_proxy",
        "config_id": "config-a",
        "external_user_id": "manager-userid-001",
    }
    # The body is for a person: a sentence, not a reason code, and a short
    # reference rather than the full conversation id.
    # The content is a typed payload now, the same shape the customer row
    # carries -- a bare string was a row no dispatcher could have sent (F3).
    assert notice["command"]["content"]["type"] == "text"
    body = notice["command"]["content"]["text"]
    assert "ROOT_TARGET_FRAME_ABANDONED" not in body
    assert str(conversation_id) not in body
    assert body.splitlines()[0].endswith("。")
    # And the deadline is a day out, not the customer envelope's thirty seconds:
    # a notice that expires before anyone could deliver it was never queued.
    from datetime import datetime
    deadline = datetime.fromisoformat(notice["command"]["deadline_at"])
    assert (deadline - datetime.now(deadline.tzinfo)).total_seconds() > 3600

    # And now the part B1-5 turned around: the dispatcher takes it, and what
    # goes out names the manager rather than the conversation (T90).
    import httpx

    sent: list[dict[str, Any]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted", "provider_operation_id": "op-l2a"})

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(
            settings.channel_send_url, transport=httpx.MockTransport(capture)
        ),
        dispatcher_id="l2a-dispatcher",
    ).dispatch_one()
    assert dispatched is not None, "the notice was not claimed -- is the audience predicate back?"
    assert sent and sent[0]["recipient"] == recipient, (
        "the notice went out without naming the manager, which is T90's accident: "
        "a channel that falls back to `conversation_id` delivers it to the customer"
    )

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        after = connection.execute(
            "SELECT status, attempt FROM outbox_messages WHERE tenant_id = %s AND id = %s",
            (tenant_id, notice["id"]),
        ).fetchone()
        receipts = connection.execute(
            "SELECT count(*) AS n FROM delivery_receipts WHERE tenant_id = %s", (tenant_id,)
        ).fetchone()
    assert after["status"] == "DELIVERED" and after["attempt"] == 1
    assert receipts["n"] == 1


def test_a_tenant_with_no_manager_configured_records_that_rather_than_inventing_one(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """No contact, no notice -- and the absence is written down where it is read.

    The alternative would be a notice addressed to nobody sitting in the outbox
    for ever, or silence indistinguishable from a bug. The handoff audit event
    carries `manager_notification_outbox_id: null`, which is a fact about setup
    that an operator can query.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _clear_manager_contact(tenant_id=tenant_id)

    _customer_asks_for_a_person(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        notices = connection.execute(
            """
            SELECT 1 FROM outbox_messages
             WHERE tenant_id = %s AND conversation_id = %s
               AND command->>'audience' = 'tenant_manager'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        handoff = connection.execute(
            """
            SELECT evidence FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s AND action = 'conversation.handoff'
            """,
            (tenant_id, conversation_id),
        ).fetchone()

    assert notices == []
    assert handoff is not None
    assert handoff["evidence"]["manager_notification_outbox_id"] is None
    assert handoff["evidence"]["manager_notification_delivered"] is False


def test_a_handoff_the_ruling_holds_back_produces_a_task_and_no_notice(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """Classified merchant-facing, deliberately silent -- and both facts recorded.

    A conversation the customer dropped ("never mind this one question") reaches
    a person, but the 2026-09-04 ruling does not push it: a merchant told "a
    conversation ended without an answer" has nothing to do about it. What must
    not happen is that this looks the same as an unclassified task or a missing
    contact, so the audit event carries the classification that decided it.

    **The scenario moved in B4-7** (ADR-0099) and the property did not. This
    case used to drive "root goal completes with no closing frame", which was
    the other `MERCHANT_ACTION` / `none` pair -- and that road no longer reaches
    a person at all: the platform's no-goal frame owns the terminal turn, which
    is the whole point of the batch. The abandoned road is untouched by it
    (ADR-0099 §五.3), so the classification under test is driven from there.
    `("ROOT_TARGET_FRAME_ABANDONED", "exit")` is the pair that is held back;
    `human_request` is the one that is pushed, and it has its own case.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _seed_manager_contact(tenant_id=tenant_id)

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    assert _drive(settings=settings, inference=StackOperationThenWaitInference("keep")) is not None
    _fill_required_slots(
        settings=settings,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        values={"request_summary": "handled"},
    )
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    assert _drive(settings=settings, inference=CustomerDropsTheQuestionInference())

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        notices = connection.execute(
            """
            SELECT 1 FROM outbox_messages
             WHERE tenant_id = %s AND conversation_id = %s
               AND command->>'audience' = 'tenant_manager'
            """,
            (tenant_id, conversation_id),
        ).fetchall()
        task = connection.execute(
            """
            SELECT task_class, notify FROM manual_tasks
             WHERE tenant_id = %s AND conversation_id = %s
            """,
            (tenant_id, conversation_id),
        ).fetchone()
        handoff = connection.execute(
            """
            SELECT evidence FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s AND action = 'conversation.handoff'
            """,
            (tenant_id, conversation_id),
        ).fetchone()

    assert notices == [], "this kind is produced, not pushed"
    assert task is not None
    assert task["task_class"] == "merchant_action"
    assert task["notify"] == "none"
    assert handoff is not None
    assert handoff["evidence"]["manual_task_notify"] == "none"
    assert handoff["evidence"]["manager_notification_outbox_id"] is None


def test_generation_drift_rejects_target_stack_cas(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    queue = PointerQueue(settings.queue_redis_url)
    pointer = queue.read_one(consumer="target-stack-cas", block_ms=500)
    assert pointer is not None
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=PushThenWaitInference(),
        worker_id="target-stack-cas",
    )
    prepared = processor.prepare(tenant_id=pointer.tenant_id, inbox_id=pointer.inbox_id)
    assert prepared is not None and prepared.previous_generation == 0

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET generation = generation + 1 WHERE id = %s",
            (conversation_id,),
        )

    with pytest.raises(AggregateConflict, match="aggregate changed during inference"):
        processor.process_prepared(prepared)


def test_slot_updates_from_understanding_reach_the_frame(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """T6, end to end and without seeding.

    `slot_updates` was part of the understanding contract from the start and no
    code path anywhere wrote it, so `_missing_required_slots` read a dict nobody
    filled and `complete` could only be satisfied by a fixture reaching into the
    database. This drives the real pipeline and lets the understanding step do
    it: first turn reports the slot, second turn completes on the strength of it.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    started_at = datetime.now(UTC)
    _accept_customer_message(
        settings=settings,
        seeded_database=seeded_database,
        conversation_id=conversation_id,
        occurred_at="2019-01-01T00:00:00+00:00",
    )
    opened = _drive(
        settings=settings,
        inference=SlotReportingInference("keep", "request_summary", "reported by the model"),
    )
    assert opened is not None

    facts = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert facts["aggregate"]["target_stack"][-1]["slots"] == {
        "request_summary": "reported by the model"
    }
    # T25, and the reason this assertion is here rather than at the function
    # boundary: the provenance is only worth anything if it survives the round
    # trip through `state`. The unit tests stop where `apply_slot_updates`
    # returns (independent review 2026-09-03, P3-3).
    evidence = facts["aggregate"]["target_stack"][-1]["slot_evidence"]["request_summary"]
    assert evidence["confidence"] == 0.95
    # `recorded_at` is the platform clock. The message above was accepted with a
    # channel-reported `occurred_at` of 2019 -- a value the sender controls and
    # ingress only checks for having a timezone -- so an implementation that
    # anchored audit evidence to it would stamp 2019 here (P3-2).
    assert evidence["recorded_at"] > "2020"
    assert datetime.fromisoformat(evidence["recorded_at"]) >= started_at

    # And the gate now opens on that slot alone: no `_fill_required_slots` here.
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    completed = _drive(settings=settings, inference=StackOperationThenWaitInference("complete"))
    assert completed is not None
    after = _facts(settings=settings, tenant_id=tenant_id, conversation_id=conversation_id)
    assert [frame["status"] for frame in after["aggregate"]["target_stack"]] == [
        "completed",
        "active",
    ]


@pytest.mark.security
def test_a_refused_slot_update_is_recorded_where_someone_can_find_it(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """T25, independent review P2-2. A rule nobody can see is a rule nobody can appeal.

    RFC 4.3 makes the engine drop a value the customer may well have just
    supplied. Before this, the only trace was a smaller `applied` count that
    never left the function -- indistinguishable from the model finding nothing,
    so "the customer said it and it did not go in" was unanswerable at every
    layer. RFC 18.3 states the principle for the confidence gate as a whole:
    recommended and applied are recorded separately.

    The audit row must also not become a copy of what the customer said: it
    names the slot and the two events, which is what an operator needs, and
    carries no value.
    """

    conversation_id = new_conversation(seeded_database["tenant_a"])
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    assert (
        _drive(
            settings=settings,
            inference=SlotReportingInference("keep", "request_summary", "read confidently", 0.95),
        )
        is not None
    )

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    assert (
        _drive(
            settings=settings,
            inference=SlotReportingInference("keep", "request_summary", "read hesitantly", 0.2),
        )
        is not None
    )

    # Read as the migrator: `audit_events` is deliberately write-only for the
    # services (001 grants `fastagent_worker` INSERT and no SELECT), which is
    # itself worth knowing -- the worker can leave the trace and cannot go back
    # and edit or read it.
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        rows = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE resource_id = %s AND action = 'conversation.slot_update_refused'
            """,
            (conversation_id,),
        ).fetchall()
        aggregate = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()["state"]

    assert len(rows) == 1, "the refusal left no trace in audit_events"
    assert rows[0]["reason_code"] == "LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED"
    refusal = rows[0]["evidence"]["refusals"][0]
    assert refusal["slot"] == "request_summary"
    assert refusal["proposed_confidence"] == 0.2
    assert refusal["recorded_confidence"] == 0.95
    # No customer content in the audit trail, and the frame kept the older value.
    assert "read hesitantly" not in str(rows[0]["evidence"])
    frame = aggregate["target_stack"][-1]
    assert frame["slots"]["request_summary"] == "read confidently"

