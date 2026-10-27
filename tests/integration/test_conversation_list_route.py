"""B3 #1 `GET /conversations` — parity §6.2's first read model, all nine fields.

**The case that carries this batch is `test_every_field_of_the_read_model_has_its_own_source`.**
T173 registered this route as blocked because three of the nine had no data
source at all; they arrived in three separate batches (contacts, the read
cursor, manual tasks). So the case that matters is not "the list returns rows"
-- it is that each of the nine comes from the thing it claims to come from, and
in particular that the three formerly-missing ones are not zeros that happen to
look right.

**The unread count is per caller**, so a second employee reads the same
conversation with a different number: `test_the_unread_count_is_the_callers_own`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import MAX_PAGE_ITEMS, create_app
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import VIEWER_EXCLUSIONS, system_role_permissions
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import (
    callback,
    conversation_for,
    new_conversation,
    sender_of,
)

pytestmark = [pytest.mark.integration]

WORKING = ["conversation_aggregate.list", "conversation_read_cursor.update"]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings,
    seeded: dict[str, Any],
    permissions: list[str],
    *,
    employee_id: Any = None,
    email: str = "operator-a@example.com",
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), seeded["tenant_a"], employee_id or seeded["employee_a"]),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={"tenant_code": "tenant-a", "email": email, "password": seeded["login_password"]},
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _colleague(seeded: dict[str, Any]) -> UUID:
    employee_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO employees(
                tenant_id, id, email, password_hash, permissions, display_name
            )
            SELECT tenant_id, %s, 'operator-a2@example.com', password_hash,
                   '[]'::jsonb, 'Operator A2'
              FROM employees WHERE tenant_id = %s AND id = %s
            """,
            (employee_id, seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
    return employee_id


def _say(
    settings: Settings,
    seeded: dict[str, Any],
    *,
    conversation_id: UUID,
    actor_id: str | None = None,
    text: str = "hello",
    occurred_at: datetime | None = None,
) -> None:
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    assert ingress.accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=callback(
            external_event_id=f"list-{uuid4()}",
            # **Who is speaking decides which conversation this lands in**
            # (the id is derived, not carried). A caller that names an actor is
            # naming the customer; one that does not gets the customer this
            # conversation was opened with.
            sender_id=actor_id or sender_of(conversation_id),
            text=text,
            occurred_at=occurred_at,
        ),
    ).accepted is True


def _drain(settings: Settings) -> None:
    assert (
        InboxProcessor(
            database=Database(settings.worker_database_url),
            pointer_queue=PointerQueue(settings.queue_redis_url),
            inference=DeterministicMockInference(),
            worker_id="list-worker",
        ).process_one(block_ms=500)
        is not None
    )


def _conversation(settings: Settings, seeded: dict[str, Any], **kwargs: Any) -> UUID:
    """One conversation, made the way production makes one: a customer writes."""

    actor_id = kwargs.get("actor_id")
    tenant_id = UUID(str(seeded["tenant_a"]))
    conversation_id = (
        conversation_for(tenant_id=tenant_id, sender_id=actor_id)
        if actor_id
        else new_conversation(tenant_id)
    )
    _say(settings, seeded, conversation_id=conversation_id, **kwargs)
    _drain(settings)
    return conversation_id


def test_every_field_of_the_read_model_has_its_own_source(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**parity §6.2 第一条, nine fields, and the three T173 was waiting for.**

    Contact summary, unread count and manual-task count each arrived in a
    different batch, and each is asserted here against a value that could only
    have come from its own table -- a scheduled event with a distinctive kind,
    two open tasks rather than one, a contact whose external id is unique to
    this test. Zeros that happen to look right are the failure this case is
    written against.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(settings, seeded_database, actor_id="customer-nine")
    _say(settings, seeded_database, conversation_id=conversation_id, text="and another thing")
    _drain(settings)

    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_a"],),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], conversation_id),
        ).fetchone()
        frame_id = aggregate["state"]["target_stack"][0]["id"]
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id, status
            ) VALUES (%s, %s, %s, %s, 'followup_due', %s, %s, 0, %s, 0, %s, 'PENDING')
            """,
            (
                seeded_database["tenant_a"],
                uuid4(),
                conversation_id,
                datetime.now(UTC) + timedelta(hours=3),
                f"followup:{conversation_id}",
                release["id"],
                frame_id,
                # NOT NULL: every scheduled event was minted by some
                # transition. Same shape `test_channel_binding_repin` seeds.
                uuid4(),
            ),
        )
        for _ in range(2):
            connection.execute(
                """
                INSERT INTO manual_tasks(
                    tenant_id, id, conversation_id, task_class, notify, reason_code
                ) VALUES (%s, %s, %s, 'merchant_action', 'batched', 'DELIVERY_UNCONFIRMED')
                """,
                (seeded_database["tenant_a"], uuid4(), conversation_id),
            )
        # A third task that is already done: the count is open work, not history.
        connection.execute(
            """
            INSERT INTO manual_tasks(
                tenant_id, id, conversation_id, task_class, notify, reason_code,
                status, resolved_at
            ) VALUES (%s, %s, %s, 'merchant_action', 'batched', 'DELIVERY_UNCONFIRMED',
                      'resolved', now())
            """,
            (seeded_database["tenant_a"], uuid4(), conversation_id),
        )
        connection.commit()

    response = client.get("/conversations", headers=headers)
    assert response.status_code == 200, response.text
    item = next(
        row for row in response.json()["items"] if row["conversation_id"] == str(conversation_id)
    )

    assert item["contact"]["external_user_id"] == "customer-nine"
    assert item["contact"]["profile_data"] == {}
    # **The engine answered, so the last message is ours, not theirs** --
    # and that is exactly why "last message" and "unread" are two fields.
    # A list that showed the customer's last line as the summary would be
    # showing a stale conversation as if nobody had replied.
    assert item["last_message"]["direction"] == "outbound"
    assert item["unread_inbound"] == 2, "both customer messages are still unread"
    assert item["control_mode"] == "ai"
    assert item["target"]["frame_id"] == str(frame_id)
    assert item["next_event"] == {
        "kind": "followup_due",
        "due_at": item["next_event"]["due_at"],
    }
    assert item["open_manual_tasks"] == 2, "resolved work is not open work"
    assert item["projection_version"] >= 1


def test_the_unread_count_is_the_callers_own(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The list is personal, and this is the field that makes it so.

    The read cursor is per person (B3 #5), so two operators see the same
    conversations in the same order with different numbers. Everything else on
    the row is a fact about the tenant; this one is a fact about the reader.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    colleague = _colleague(seeded_database)
    conversation_id = _conversation(settings, seeded_database)
    _say(settings, seeded_database, conversation_id=conversation_id, text="second")
    _drain(settings)

    with _migration_connection() as connection:
        first = connection.execute(
            "SELECT id FROM inbox_events WHERE tenant_id = %s AND conversation_id = %s"
            " ORDER BY created_at LIMIT 1",
            (seeded_database["tenant_a"], conversation_id),
        ).fetchone()["id"]
    marked = client.post(
        f"/conversations/{conversation_id}/read",
        json={"last_read_ref": str(first)},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert marked.status_code == 200, marked.text

    mine = client.get("/conversations", headers=headers).json()["items"][0]
    assert mine["unread_inbound"] == 1

    other_client, other_headers = _client_for(
        settings,
        seeded_database,
        WORKING,
        employee_id=colleague,
        email="operator-a2@example.com",
    )
    theirs = other_client.get("/conversations", headers=other_headers).json()["items"][0]
    assert theirs["conversation_id"] == mine["conversation_id"]
    assert theirs["unread_inbound"] == 2, "nobody read anything on this account"


def test_the_order_is_the_last_message_and_the_silent_one_sorts_last(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Ordering, and the conversation with nothing in it.

    Sorting on the aggregate's `updated_at` would float conversations the engine
    touched -- a slot write, a generation bump -- above ones a customer actually
    wrote in. And a conversation with no visible message sorts last rather than
    disappearing: hiding it would make "my list" and "the conversations that
    exist" two different sets without saying so.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    now = datetime.now(UTC)
    older = _conversation(
        settings, seeded_database, actor_id="older", occurred_at=now - timedelta(hours=2)
    )
    newer = _conversation(
        settings, seeded_database, actor_id="newer", occurred_at=now - timedelta(minutes=1)
    )

    # A conversation the engine created but nobody has spoken in: the aggregate
    # exists, the transcript is empty.
    silent = uuid4()
    with _migration_connection() as connection:
        template = connection.execute(
            "SELECT profile_release_id, state, target_stack, config_id"
            "  FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], older),
        ).fetchone()
        state = dict(template["state"])
        state["id"] = str(silent)
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, state_version, lifecycle, control_mode, generation,
                profile_release_id, target_stack, state, config_id
            ) VALUES (%s, %s, 0, 'open', 'ai', 0, %s, %s, %s, %s)
            """,
            (
                seeded_database["tenant_a"],
                silent,
                template["profile_release_id"],
                Jsonb(template["target_stack"]),
                Jsonb(state),
                template["config_id"],
            ),
        )
        connection.commit()

    items = client.get("/conversations", headers=headers).json()["items"]
    order = [row["conversation_id"] for row in items]
    assert order == [str(newer), str(older), str(silent)]
    assert items[-1]["last_message"] is None
    assert items[-1]["unread_inbound"] == 0
    assert items[-1]["contact"] is None, "nobody wrote, so nobody is recorded"


def test_the_filter_is_the_two_values_the_column_accepts(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One filter, bounded by the column's own vocabulary.

    "Who is waiting on a person" is the question an operator actually has; a
    filter language over customer data is not something this route hands out.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    ai_side = _conversation(settings, seeded_database, actor_id="ai-side")
    human_side = _conversation(settings, seeded_database, actor_id="human-side")
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], human_side),
        )
        connection.commit()

    held = client.get("/conversations?control_mode=human", headers=headers).json()["items"]
    assert [row["conversation_id"] for row in held] == [str(human_side)]
    machine = client.get("/conversations?control_mode=ai", headers=headers).json()["items"]
    assert [row["conversation_id"] for row in machine] == [str(ai_side)]

    refused = client.get("/conversations?control_mode=paused", headers=headers)
    assert refused.status_code == 422
    assert refused.json()["code"] == "UNKNOWN_CONTROL_MODE"


def test_another_tenants_conversations_are_not_listed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS-AGGREGATE-006, and the joins inherit it.

    Every table this query touches carries the same policy, so a row from
    another tenant cannot appear through the contact, the messages, the cursor,
    the schedule or the tasks either.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    mine = _conversation(settings, seeded_database)
    foreign = uuid4()
    with _migration_connection() as connection:
        template = connection.execute(
            "SELECT profile_release_id, state, target_stack, config_id"
            "  FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], mine),
        ).fetchone()
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_b"],),
        ).fetchone()
        state = dict(template["state"])
        state["id"] = str(foreign)
        state["tenant_id"] = str(seeded_database["tenant_b"])
        state["config_id"] = None
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, state_version, lifecycle, control_mode, generation,
                profile_release_id, target_stack, state
            ) VALUES (%s, %s, 0, 'open', 'ai', 0, %s, %s, %s)
            """,
            (
                # **No `config_id`**: the fixture seeds a binding for tenant
                # A only, and the column has a composite foreign key into
                # `channel_bindings`. A tenant-B conversation therefore has
                # no binding here -- which is also the shape of any
                # conversation created before T24 added the link.
                seeded_database["tenant_b"],
                foreign,
                release["id"],
                Jsonb(template["target_stack"]),
                Jsonb(state),
            ),
        )
        connection.commit()

    listed = client.get("/conversations", headers=headers).json()["items"]
    assert [row["conversation_id"] for row in listed] == [str(mine)]


def test_the_list_is_not_a_viewers_read() -> None:
    """The ninth field decides it.

    Eight of the nine would be fine for a read-only role. The last-message
    summary is redacted customer text, and a projection cannot be half-granted
    -- which is T174's registered gap, met here as a decision rather than as a
    surprise.
    """

    assert "conversation_aggregate.list" in VIEWER_EXCLUSIONS
    roles = system_role_permissions()
    assert "conversation_aggregate.list" not in roles["viewer"]
    assert "conversation_aggregate.list" in roles["operator"]
    assert "conversation_aggregate.list" in roles["owner"]
    # ...while the single-conversation read stays a viewer's, unchanged.
    assert "conversation_aggregate.read" in roles["viewer"]


def test_the_cursor_walks_every_conversation_including_the_silent_one(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-06, P1-1** -- a whole class of rows was unreachable.

    The keyset predicate compared `(last_message.created_at, id)` against the
    cursor. A row comparison containing `NULL` **is** `NULL`, and `WHERE`
    drops it -- so a conversation with no visible message never appeared on
    any cursor page, while `NULLS LAST` guaranteed it could only ever appear
    on one. Worse, a page ending on such a row answered `next_cursor: null`,
    so the client was told the list had ended with rows still to come.

    The page-one case existed and passed -- it asserted the silent
    conversation sorts last -- **and never turned a page**. That is why this
    walks the whole list and counts.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    expected: set[str] = set()
    now = datetime.now(UTC)
    for index in range(MAX_PAGE_ITEMS + 2):
        expected.add(
            str(
                _conversation(
                    settings,
                    seeded_database,
                    actor_id=f"c{index:04d}",
                    occurred_at=now - timedelta(minutes=index + 1),
                )
            )
        )

    # ...and one the engine created that nobody has spoken in: the row that
    # used to disappear.
    silent = uuid4()
    with _migration_connection() as connection:
        template = connection.execute(
            "SELECT profile_release_id, state, target_stack, config_id"
            "  FROM conversation_aggregates WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_a"],),
        ).fetchone()
        state = dict(template["state"])
        state["id"] = str(silent)
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, state_version, lifecycle, control_mode, generation,
                profile_release_id, target_stack, state, config_id
            ) VALUES (%s, %s, 0, 'open', 'ai', 0, %s, %s, %s, %s)
            """,
            (
                seeded_database["tenant_a"],
                silent,
                template["profile_release_id"],
                Jsonb(template["target_stack"]),
                Jsonb(state),
                template["config_id"],
            ),
        )
        connection.commit()
    expected.add(str(silent))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        query = f"?cursor={cursor}" if cursor else ""
        body = client.get(f"/conversations{query}", headers=headers).json()
        seen.extend(item["conversation_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "the walk terminates"
    assert len(seen) == len(set(seen)), "no conversation appears twice"
    assert set(seen) == expected, "and none is skipped -- the silent one included"
    assert seen[-1] == str(silent), "it still sorts last"


def test_a_cursor_it_did_not_issue_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """400, not an empty page (P2-1).

    An empty page and "you have seen everything" are the same response, so a
    client walking `next_cursor` would stop early and never learn it had.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    for bad in ("nonsense", "2026-09-06T12:00:00+00:00"):
        response = client.get(f"/conversations?cursor={bad}", headers=headers)
        assert response.status_code == 400, (bad, response.text)
        assert response.json()["code"] == "INVALID_CURSOR"


def test_the_preview_of_a_two_message_turn_is_the_last_one_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T369：**一轮写两行出站时，预览要是投递顺序上的最后一条。**

    一轮写的行 `created_at` **相同**（它是事务时间戳），所以排序落到后面的分量。
    先前这条 LATERAL 排的是 `created_at DESC, id DESC` —— `id` 是 `uuid4()`，
    **于是「最后一条」是抛硬币**：会话列表的预览每次刷新都可能换一条，
    而运维照它排查「我们最后说了什么」时看到的是随机的那一条。

    投递那一侧早就修过同一件事（T109 / 迁移 044）；**这个读取方没跟上**。

    ## ⚠️ 夹具站在边界上，所以这条用例在旧实现下**必红**

    两行同一个事务（`created_at` 相同），而**较大的那个 uuid 给 ordinal 0**：

    * 旧实现（`id DESC`）→ 取到较大的 id = **ordinal 0**，预览是第一条；
    * 今天（`ordinal DESC` 在前）→ 取到 **ordinal 1**，预览是最后发出去的那条。

    随便挑两个 uuid 的话，这条用例有一半概率在旧实现下也绿 ——
    本仓「夹具不能恰好等于错误答案」那一族。
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _conversation(settings, seeded_database, actor_id="customer-two-bubbles")
    tenant_id = seeded_database["tenant_a"]

    first, second = sorted((uuid4(), uuid4()), reverse=True)
    assert first > second, "夹具要求 ordinal 0 拿到较大的那个 id"

    with _migration_connection() as connection:
        aggregate = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        frame_id = UUID(str(aggregate["state"]["target_stack"][0]["id"]))
        # **两行写在同一个事务里** —— 那正是生产里一轮两条消息的形状，
        # 也正是 `created_at` 相同的原因。
        for row_id, ordinal, text in ((first, 0, "先说的那句"), (second, 1, "后说的那句")):
            intent_id = uuid4()
            connection.execute(
                """
                INSERT INTO intent_records(
                    tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                    strategy_id, target_frame_id, idempotency_key, preconditions,
                    policy_evidence
                ) VALUES (%s, %s, %s, 'send_message', 'APPROVED', %s, %s, 't369',
                          %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    intent_id,
                    conversation_id,
                    Jsonb({"fixture": True}),
                    Jsonb({"step": "fixture", "version": "1"}),
                    frame_id,
                    f"send_message:t369-{conversation_id}:{ordinal}",
                    Jsonb({}),
                    Jsonb([]),
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox_messages(
                    tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                    ordinal
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    row_id,
                    conversation_id,
                    intent_id,
                    Jsonb({"audience": "customer", "content": {"type": "text", "text": text}}),
                    f"outbox:t369-{conversation_id}:{ordinal}",
                    ordinal,
                ),
            )
        connection.commit()

    item = next(
        row
        for row in client.get("/conversations", headers=headers).json()["items"]
        if row["conversation_id"] == str(conversation_id)
    )

    # ⚠️ **键名是 `message_id` 与 `content`，不是 `id` / `payload`。**
    # 第一版按 SQL 的列名（`last_message.id` / `last_message.payload`）写断言，
    # 门禁里红成 `KeyError: 'id'` —— **投影层把它们改了名**
    # （`message_id` / `content`，见 `_conversation_projection`）。
    # 本仓「自造夹具藏得住形状不匹配」的近亲：断言读的是**响应**，
    # 而我按**查询**的名字写。
    assert item["last_message"]["message_id"] == str(second), (
        "预览取的不是这一轮最后发出去的那条 —— 排序落到 uuid4() 上了"
    )
    assert item["last_message"]["content"]["text"] == "后说的那句"
