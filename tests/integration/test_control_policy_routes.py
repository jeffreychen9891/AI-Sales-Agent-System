"""B3 #10 / #11 `GET` and `PUT /control-policy` (ADR-0048 K13, migration 037).

**The cases that carry this batch are the two engine ones**
(`test_a_tenant_whose_default_is_human_gets_human_conversations` and
`test_turning_understanding_off_stops_the_call_and_leaves_the_slots_alone`).
The route tests only show that two columns can be read and written; a policy
whose switches nothing reads is a column with no consumer, which is the shape
T175 was registered for on the same day. So the two switches are asserted
through the pipeline that consumes them, on a real database.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import VIEWER_SNAPSHOT, system_role_permissions
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

pytestmark = [pytest.mark.integration]

WORKING = ["control_policy.read", "control_policy.update"]


class CountingInference(DeterministicMockInference):
    """The contract-shaped mock with `relation` scriptable, plus a call count.

    Subclassed rather than hand-rolled for the reason
    `test_human_takeover_roundtrip` gives: a bespoke double drifts from the
    document a real adapter must emit, and `relation` is the one field these
    two switches interact with.
    """

    def __init__(self) -> None:
        self.relation = "same_goal"
        self.understand_calls = 0

    def understand(self, **kwargs: Any) -> dict[str, Any]:
        self.understand_calls += 1
        document = super().understand(**kwargs)
        document["interruption"] = {
            "relation": self.relation,
            "suggested_frame_definition_id": None,
        }
        return document


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _set_policy(tenant_id: Any, *, mode: str = "ai", understanding: bool = True) -> None:
    """Straight to the column, because the route is not what is under test here.

    The two engine cases are about what the worker does with the value; driving
    them through `PUT` as well would make a pipeline test fail when a route
    regressed, which is the coupling that makes a red suite unreadable.
    """

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenants SET default_control_mode = %s,"
            " understanding_during_takeover = %s WHERE id = %s",
            (mode, understanding, tenant_id),
        )
        connection.commit()


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
            external_event_id=f"policy-{uuid4()}",
            sender_id=sender_of(conversation_id),
            text=text,
        ),
    )
    assert accepted.accepted is True


def _drain(*, settings: Settings, inference: Any) -> Any:
    return InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=inference,
        worker_id="control-policy-worker",
    ).process_one(block_ms=500)


def _aggregate(settings: Settings, tenant_id: Any, conversation_id: UUID) -> dict[str, Any]:
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        row = connection.execute(
            "SELECT control_mode, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_the_policy_reads_back_the_platform_defaults(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #10, and the defaults are the point.

    `ai` and `true` are what the engine did before this migration existed, so a
    tenant that never touches the policy cannot tell it was added. A migration
    that changed behaviour on deploy would be a switch and a silent change of
    product in one commit.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)

    response = client.get("/control-policy", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "default_control_mode": "ai",
        "understanding_during_takeover": True,
    }


def test_writing_the_policy_records_both_states(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Freeze B3 #11's policy half: the change, and what it changed from.

    Unlike a slot value or a task note, both switches are safe to copy into an
    append-only table -- they are two enums about how a tenant runs, and
    "since when" is the question an audit row is here to answer.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)

    response = client.put(
        "/control-policy",
        json={"default_control_mode": "human", "understanding_during_takeover": False},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["default_control_mode"] == "human"
    assert response.json()["result"]["understanding_during_takeover"] is False
    # **Declared, not implied**: this call did not touch existing conversations,
    # and the answer says so rather than leaving the caller to notice (T176).
    assert response.json()["result"]["conversations_transitioned"] == 0

    assert client.get("/control-policy", headers=headers).json() == {
        "default_control_mode": "human",
        "understanding_during_takeover": False,
    }

    with _migration_connection() as connection:
        audit = connection.execute(
            "SELECT evidence, actor_id FROM audit_events WHERE tenant_id = %s"
            " AND action = 'control_policy.update'",
            (seeded_database["tenant_a"],),
        ).fetchone()
    assert audit is not None
    assert audit["actor_id"] == seeded_database["employee_a"]
    assert audit["evidence"]["before"] == {
        "default_control_mode": "ai",
        "understanding_during_takeover": True,
    }
    assert audit["evidence"]["after"]["default_control_mode"] == "human"


def test_a_mode_the_engine_could_not_start_a_conversation_in_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """422 from the model, not 500 from the constraint.

    The column's CHECK holds the same two values; being refused by name at the
    edge is what makes the two agree readably rather than only agreeing.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    response = client.put(
        "/control-policy",
        json={"default_control_mode": "paused", "understanding_during_takeover": True},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422, response.text
    assert client.get("/control-policy", headers=headers).json()["default_control_mode"] == "ai"


def test_a_partial_policy_is_refused(settings: Settings, seeded_database: dict[str, Any]) -> None:
    """Both fields, because the two switches interact.

    A tenant whose default is `human` and whose understanding is off has an
    engine that does nothing at all -- a legitimate configuration and a terrible
    accident. `PUT` makes the combination something somebody stated.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    response = client.put(
        "/control-policy",
        json={"default_control_mode": "human"},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422, response.text


def test_reading_the_policy_does_not_grant_writing_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two actions, because they are two decisions to grant.

    Everyone in a tenant may reasonably need to know how their conversations
    start; changing it for the whole tenant is not the same permission.
    """

    client, headers = _client_for(settings, seeded_database, ["control_policy.read"])
    assert client.get("/control-policy", headers=headers).status_code == 200
    assert (
        client.put(
            "/control-policy",
            json={"default_control_mode": "human", "understanding_during_takeover": True},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        ).status_code
        == 403
    )


def test_the_policy_read_is_a_viewers_and_the_write_is_not() -> None:
    """The `VIEWER_SNAPSHOT` tripwire fired here, and this is the decision.

    `control_policy.read` is a read, so `viewer` gains it by derivation unless
    the exclusion criterion applies -- and it does not: two operational switches
    are neither conversation content nor credential metadata. The write stays
    out of `operator` for a different reason: it is tenant-wide, and an operator
    works conversations rather than configuring the tenant.
    """

    assert "control_policy.read" in VIEWER_SNAPSHOT
    roles = system_role_permissions()
    assert "control_policy.read" in roles["viewer"]
    assert "control_policy.update" not in roles["viewer"]
    assert "control_policy.update" not in roles["operator"]
    # `owner` is every grantable action, so the write is reachable at all.
    assert "control_policy.update" in roles["owner"]


def test_a_tenant_whose_default_is_human_gets_human_conversations(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The switch, through the engine that reads it.**

    `_initial_aggregate` hard-coded `"ai"` from M0 until this batch, and the
    freeze note for #10 is 「不把 UI 默认当 aggregate 状态」 -- the default is a
    tenant's standing decision, and the only way to show it is one is to create
    a conversation and look at the mode it starts in.

    Nothing is sent on that turn, which is the existing human-control behaviour
    and not something this batch changed.
    """

    _set_policy(seeded_database["tenant_a"], mode="human")
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = CountingInference()
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="hello, is anybody there",
    )
    assert _drain(settings=settings, inference=inference) is not None

    aggregate = _aggregate(settings, seeded_database["tenant_a"], conversation_id)
    assert aggregate["control_mode"] == "human"
    # **Both places**, because the worker reads `state["control_mode"]` and the
    # column is what the routes filter on; a conversation whose two copies
    # disagree is one the takeover route would refuse for the wrong reason.
    assert aggregate["state"]["control_mode"] == "human"

    with _migration_connection() as connection:
        outbox = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()["n"]
    assert outbox == 0


def test_turning_understanding_off_stops_the_call_and_leaves_the_slots_alone(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The other switch, and the paragraph it replaces.**

    `_process_under_human_control`'s docstring said the switch would arrive with
    its carrier (freeze B3 #10/#11) and that until then the platform default was
    "keep running". This asserts both halves of that becoming real: with the
    policy off no understanding call is made, and the outcome the customer sees
    is unchanged -- nothing was going to be sent either way.

    Driven with `default_control_mode = 'human'` so the conversation is under
    human control from its first event, which is the branch the switch guards.
    """

    _set_policy(seeded_database["tenant_a"], mode="human", understanding=False)
    conversation_id = new_conversation(seeded_database["tenant_a"])
    inference = CountingInference()
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="my order number is 12345",
    )
    assert _drain(settings=settings, inference=inference) is not None
    assert inference.understand_calls == 0, "the switch is off; the model must not be called"

    frames = _aggregate(settings, seeded_database["tenant_a"], conversation_id)["state"][
        "target_stack"
    ]
    assert all(not frame.get("slots") for frame in frames), "no writeback without understanding"

    # ...and with the switch on, the same message does reach the model. Same
    # fixture, same path, one column different -- which is what makes the first
    # half a measurement rather than a claim about a mock that was never called.
    _set_policy(seeded_database["tenant_a"], mode="human", understanding=True)
    second = new_conversation(seeded_database["tenant_a"])
    _send(settings=settings, seeded=seeded_database, conversation_id=second, text="and my name")
    assert _drain(settings=settings, inference=inference) is not None
    assert inference.understand_calls == 1


def _pending_exit(settings: Settings, tenant_id: Any, conversation_id: UUID) -> Any:
    return _aggregate(settings, tenant_id, conversation_id)["state"].get("pending_exit_signal")


def test_turning_the_switch_off_does_not_leave_a_sentence_the_customer_took_back(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-06, P1-1** -- reproduced on a real database.

    The branch that runs the understanding step both **writes and clears**
    `pending_exit_signal`. Returning before it stopped the clearing and did not
    stop the consuming: `_understanding_for_plan` still substitutes the signal
    into the first plan after the conversation is resumed. So this order --
    switch on, customer says "forget it" (signal recorded), operator turns the
    switch off, customer changes their mind, conversation resumes -- had the
    engine acting on a sentence that had been withdrawn. It breaks both ways:
    `abandon` is legal *only* under an exit relation, so a stale one unlocks a
    refusal that should stand; and `repair` is refused under one (T53), so it
    also blocks the ordinary way back to the parent goal.

    **U2 settles it**: the last inbound message's relation wins. While the
    switch is off there is no relation, because nobody listened -- and that is
    not grounds to keep asserting an older one.

    **Both directions are asserted**, because "the signal is gone" would
    otherwise be indistinguishable from a path that never ran: the switch-on
    case is the control, and it clears the signal through the branch that has
    always cleared it.
    """

    tenant_id = seeded_database["tenant_a"]
    _set_policy(tenant_id, mode="human", understanding=True)
    inference = CountingInference()
    conversation_id = new_conversation(seeded_database["tenant_a"])

    # Born under human control, so every event takes the branch the switch guards.
    _send(settings=settings, seeded=seeded_database, conversation_id=conversation_id, text="hello")
    assert _drain(settings=settings, inference=inference) is not None

    # The customer withdraws, while the machine is still listening.
    inference.relation = "exit"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="forget it, I do not need this any more",
    )
    assert _drain(settings=settings, inference=inference) is not None
    recorded = _pending_exit(settings, tenant_id, conversation_id)
    assert recorded is not None and recorded["relation"] == "exit", "precondition"

    # The operator turns understanding off. The customer changes their mind --
    # and nobody reads it.
    _set_policy(tenant_id, mode="human", understanding=False)
    calls_before = inference.understand_calls
    inference.relation = "same_goal"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="actually, please do go ahead",
    )
    assert _drain(settings=settings, inference=inference) is not None
    assert inference.understand_calls == calls_before, "the switch is off"
    assert _pending_exit(settings, tenant_id, conversation_id) is None, (
        "an unread inbound message must not leave the withdrawn sentence standing"
    )

    # **The control.** Same fixture, same message, switch on: the branch that
    # has always cleared a non-exit relation still does, so the assertion above
    # is about the off branch rather than about a signal that was never set.
    _set_policy(tenant_id, mode="human", understanding=True)
    inference.relation = "exit"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="no, cancel it after all",
    )
    assert _drain(settings=settings, inference=inference) is not None
    assert _pending_exit(settings, tenant_id, conversation_id) is not None
    inference.relation = "same_goal"
    _send(
        settings=settings,
        seeded=seeded_database,
        conversation_id=conversation_id,
        text="sorry, keep going",
    )
    assert _drain(settings=settings, inference=inference) is not None
    assert _pending_exit(settings, tenant_id, conversation_id) is None
