"""B3 #3 `GET /conversations/{id}/messages` + migration 033 (parity §6.2).

**The three cases that carry this batch are
`test_the_page_walks_the_whole_timeline_without_skipping_or_repeating`,
`test_credential_shaped_content_is_redacted_on_the_way_out` and
`test_only_things_somebody_said_are_messages`.** This is the repository's first
paginated read, so the cursor is the part with somewhere new to go wrong; the
other two are the promises §6.2 makes about what a message page may contain.
"""

from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import MESSAGE_PAGE_SIZE, create_app
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

#: A password-shaped string, kept apart from the DSN it goes into.
#:
#: **Assembled rather than written out** -- `check_secrets.py` reads a literal
#: `user:password@host` as a credential in the repository and fails the gate,
#: correctly: it cannot know this one is a fixture. The concatenation is the
#: shape `tests/contracts/test_control_plane_contracts.py` already uses for
#: the same reason, and the gate's own advice is to move the value out
#: rather than to add it to an allowlist.
PASSWORD_SHAPED = "not-a-real-password"

#: What a customer might paste. The point is the *shape*, which is what
#: `redact()` keys on, and it must not survive the projection.
DSN_IN_A_MESSAGE = "here is the link postgresql://app:" + PASSWORD_SHAPED + "@db.internal/fastagent"

BASE = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


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


def _conversation(seeded: dict[str, Any], *, tenant_key: str = "tenant_a") -> tuple[UUID, UUID]:
    """An empty conversation, and the release it is pinned to."""

    tenant_id = seeded[tenant_key]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        connection.execute(
            "INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id)"
            " VALUES (%s, %s, %s)",
            (tenant_id, conversation_id, release["id"]),
        )
        connection.commit()
    return conversation_id, release["id"]


def _inbound(
    seeded: dict[str, Any],
    conversation_id: UUID,
    release_id: UUID,
    *,
    at: datetime,
    text: str = "hello",
    event_type: str = "customer_message",
    tenant_key: str = "tenant_a",
) -> None:
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO inbox_events(
                tenant_id, id, event_id, source, external_event_id, conversation_id,
                event_type, profile_release_id, envelope, created_at
            ) VALUES (%s, %s, %s, 'wecom_proxy', %s, %s, %s, %s, %s, %s)
            """,
            (
                seeded[tenant_key],
                uuid4(),
                uuid4(),
                str(uuid4()),
                conversation_id,
                event_type,
                release_id,
                Jsonb(
                    {
                        # **`type`, not `kind`** (independent review P2-1). The
                        # envelope contract's `content` is
                        # `additionalProperties: false` with `type` required, so
                        # the old fixture asserted against a payload the schema
                        # forbids -- which made these cases immune to a change
                        # in the very contract they carry.
                        "content": {"type": "text", "text": text},
                        "actor": {"party": "customer"},
                        "occurred_at": at.isoformat(),
                        "correlation_id": str(uuid4()),
                    }
                ),
                at,
            ),
        )
        connection.commit()


def _outbound(
    seeded: dict[str, Any],
    conversation_id: UUID,
    *,
    at: datetime,
    text: str = "hi back",
    audience: str = "customer",
    #: 这一行在它那一轮里的位置。**默认 0，与迁移 `044` 的默认一致** ——
    #: 只有「一轮写好几行」那种用例才需要指定它（T369 / ADR-0140）。
    ordinal: int = 0,
    #: 指定行 id。**只有要让夹具站在边界上的用例才用它**：
    #: 排序退回 `id` 时结果由 `uuid4()` 决定，而那种用例要它是确定的。
    row_id: UUID | None = None,
) -> None:
    intent_id = uuid4()
    with _migration_connection() as connection:
        # `outbox_messages.intent_id` has a foreign key into `intent_records`:
        # nothing goes out that a policy decision did not propose. The seed
        # respects that rather than working around it, because the route joins
        # nothing here and a fixture that skipped the intent would be testing a
        # row shape production cannot produce.
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions
            ) VALUES (%s, %s, %s, 'send_message', 'APPLIED', %s, %s, 'seed', %s, %s, %s)
            """,
            (
                seeded["tenant_a"],
                intent_id,
                conversation_id,
                Jsonb({"kind": "text"}),
                Jsonb({"party": "system"}),
                uuid4(),
                str(uuid4()),
                Jsonb({}),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                status, created_at, ordinal
            ) VALUES (%s, %s, %s, %s, %s, %s, 'DELIVERED', %s, %s)
            """,
            (
                seeded["tenant_a"],
                row_id or uuid4(),
                conversation_id,
                intent_id,
                Jsonb(
                    {
                        # **The shape `pipeline.py` actually writes** (review
                        # P1-2). The old fixture invented `actor` and
                        # `occurred_at`, which no send command carries -- so two
                        # projection fields were green here and `null` in
                        # production. `audience` is the field this page filters
                        # on, so a fixture without it tests nothing.
                        "audience": audience,
                        "tenant_id": str(seeded["tenant_a"]),
                        "conversation_id": str(conversation_id),
                        "content": {"type": "text", "text": text},
                        "deadline_at": at.isoformat(),
                        "correlation_id": str(uuid4()),
                    }
                ),
                str(uuid4()),
                at,
                ordinal,
            ),
        )
        connection.commit()


def test_the_page_shows_both_directions_in_one_timeline(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Inbound and outbound live in two tables and read as one conversation."""

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="first")
    _outbound(seeded_database, conversation_id, at=BASE + timedelta(seconds=1), text="second")
    _inbound(
        seeded_database, conversation_id, release_id, at=BASE + timedelta(seconds=2), text="third"
    )
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    response = client.get(f"/conversations/{conversation_id}/messages", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["direction"] for item in body["items"]] == ["inbound", "outbound", "inbound"]
    assert [item["content"]["text"] for item in body["items"]] == ["first", "second", "third"]
    # Outbound carries a delivery state; inbound has none of its own -- it arrived.
    assert body["items"][1]["delivery_status"] == "DELIVERED"
    assert body["items"][0]["delivery_status"] is None
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert response.headers["cache-control"] == "no-store, private"


def test_the_page_walks_the_whole_timeline_without_skipping_or_repeating(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The cursor, and the reason it carries four parts.**

    Every message here shares one timestamp: they are written in a loop, and two
    rows from two tables really can land in the same microsecond in production
    too. A cursor on the timestamp alone would either stop early or loop
    forever.

    The key is `(created_at, source_table, ordinal, id)` — 前三个分量的由来是
    **ADR-0048 K12，由 B3 #5 的独立审查（P2-2）补回**（那次发现这条路由只发了
    三分量里的两个却引着那条裁定）。两个分量就足以让序是全序，
    而那正是当初把第三个省掉的论证；裁定仍然是裁定，
    且一个说得出自己来自哪张表的游标不必查 union 就能解析。

    **第四个分量 `ordinal` 是 ADR-0140 加的（T369，2026-09-19）**：
    一轮可以写好几行出站，而一轮写的行 `created_at` 与 `source_table` 都相同 ——
    于是排序落到 `id`（`uuid4()`），**一轮两条消息的先后是抛硬币的**。
    站在这条性质上的用例是 `..._paged_in_the_order_they_were_sent`。
    """

    conversation_id, release_id = _conversation(seeded_database)
    total = MESSAGE_PAGE_SIZE + 3
    for index in range(total):
        _inbound(seeded_database, conversation_id, release_id, at=BASE, text=f"m{index}")
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        query = f"?cursor={cursor}" if cursor else ""
        body = client.get(
            f"/conversations/{conversation_id}/messages{query}", headers=headers
        ).json()
        seen.extend(item["message_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "the walk did not terminate"
    assert len(seen) == total
    assert len(set(seen)) == total, "a message was returned twice"


def test_a_cursor_it_did_not_issue_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """400, not an empty page.

    An empty page is indistinguishable from "you have read everything", so a
    client looping on `has_more` would stop early and never know.
    """

    conversation_id, _ = _conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    # **The old two-part token is in this list on purpose** (B3 #5 P2-2): the key
    # gained `source_table`, and a cursor issued under the previous shape is
    # refused rather than accommodated -- carrying two orderings at once is the
    # drift the correction removed. Cursors are opaque and live for one scroll.
    two_part = (
        urlsafe_b64encode(f"2026-09-06T12:00:00+00:00|{uuid4()}".encode()).decode().rstrip("=")
    )
    # **三段那一种也在这张表里，理由与两段那一种逐字相同**
    # （T369 / ADR-0140，2026-09-19）：这把键又多了一个分量（`ordinal`），
    # 而按旧形状发出去的游标**照样拒，不迁就** ——
    # 同时背两套排序正是这次改动要去掉的漂移。
    # ⚠️ **这次被拒的是上一版自己**，所以它比两段那一条更容易被人「顺手兼容」。
    three_part = (
        urlsafe_b64encode(
            f"2026-09-06T12:00:00+00:00|outbox_messages|{uuid4()}".encode()
        )
        .decode()
        .rstrip("=")
    )
    for bad in (
        "nonsense",
        "2026-09-06T12:00:00+00:00",
        "not-a-date|" + str(uuid4()),
        two_part,
        three_part,
    ):
        response = client.get(
            f"/conversations/{conversation_id}/messages?cursor={bad}", headers=headers
        )
        assert response.status_code == 400, (bad, response.text)
        assert response.json()["code"] == "INVALID_CURSOR"


def test_credential_shaped_content_is_redacted_on_the_way_out(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**§6.2's 递归脱敏, on the path that carries customer text.**

    A customer can paste anything, including something that looks like a
    connection string -- which is how the 2026-09-01 leak happened on a
    different surface. `redact()` keys on the *shape*, so the projection runs it
    over the whole payload rather than over a list of fields somebody remembered.
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text=DSN_IN_A_MESSAGE)
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    response = client.get(f"/conversations/{conversation_id}/messages", headers=headers)

    assert response.status_code == 200, response.text
    assert PASSWORD_SHAPED not in response.text
    assert "[REDACTED]" in response.text


def test_only_things_somebody_said_are_messages(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`inbox_events` also carries timers, channel events and business events.

    They are things that happened, not things anybody said. A page that showed
    them would make the customer look like they had sent a timer.
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="a real message")
    for kind in ("timer", "channel_event", "business_event", "internal"):
        _inbound(
            seeded_database,
            conversation_id,
            release_id,
            at=BASE + timedelta(seconds=1),
            text=f"not a message: {kind}",
            event_type=kind,
        )
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    body = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()

    assert [item["content"]["text"] for item in body["items"]] == ["a real message"]


def test_a_manager_notification_is_not_part_of_the_customer_conversation(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The outbound half of "only things somebody said"** (review P1-1).

    `outbox_messages` carries two kinds of row: replies to the customer, and
    escalations the dispatcher delivers to the tenant's manager. `audience` is
    the field that tells them apart -- `016` even indexes on it -- and without a
    filter the manager's escalation shows up in the customer's transcript as an
    outbound message, indistinguishable from a reply.

    The cost is not cosmetic: an operator reads a conversation that never
    happened, and B3 #2's takeover is built on that reading.
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="I am angry")
    _outbound(seeded_database, conversation_id, at=BASE + timedelta(seconds=1), text="sorry")
    _outbound(
        seeded_database,
        conversation_id,
        at=BASE + timedelta(seconds=2),
        text="ESCALATION: please step in",
        audience="tenant_manager",
    )
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    body = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()

    assert [item["content"]["text"] for item in body["items"]] == ["I am angry", "sorry"]
    assert "ESCALATION" not in str(body)


def test_a_row_written_before_audience_existed_is_still_the_customers(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The `coalesce` half, and it is the half that fails silently.**

    `audience` arrived with the manager-notification work; rows written before
    it have no such key. A filter of `command->>'audience' = 'customer'` would
    drop every one of them -- an older conversation would simply lose its
    replies, with no error anywhere.
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="hello")
    with _migration_connection() as connection:
        intent_id = uuid4()
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions
            ) VALUES (%s, %s, %s, 'send_message', 'APPLIED', %s, %s, 'seed', %s, %s, %s)
            """,
            (
                seeded_database["tenant_a"],
                intent_id,
                conversation_id,
                Jsonb({"kind": "text"}),
                Jsonb({"party": "system"}),
                uuid4(),
                str(uuid4()),
                Jsonb({}),
            ),
        )
        connection.execute(
            """
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'DELIVERED', %s)
            """,
            (
                seeded_database["tenant_a"],
                uuid4(),
                conversation_id,
                intent_id,
                # No `audience` key at all -- the pre-L2a shape.
                Jsonb({"content": {"type": "text", "text": "an older reply"}}),
                str(uuid4()),
                BASE + timedelta(seconds=1),
            ),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    body = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()

    assert [item["content"]["text"] for item in body["items"]] == ["hello", "an older reply"]


def test_an_outbound_row_does_not_invent_an_actor(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Absent rather than null** (review P1-2, ADR-0076 §四.2's rule again).

    A send command carries `audience`, `tenant_id`, `conversation_id`,
    `content`, `deadline_at`, `correlation_id` -- no `actor`, no `occurred_at`.
    The old fixture invented both, so two projection fields were green in tests
    and `null` in production. They are now simply not there on an outbound row,
    and `direction` already says who spoke.
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="in")
    _outbound(seeded_database, conversation_id, at=BASE + timedelta(seconds=1), text="out")
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    items = client.get(f"/conversations/{conversation_id}/messages", headers=headers).json()[
        "items"
    ]

    inbound, outbound = items
    assert inbound["actor"] == {"party": "customer"}
    assert inbound["occurred_at"]
    assert "actor" not in outbound
    assert "occurred_at" not in outbound
    # Both directions still carry what the command does have.
    assert outbound["correlation_id"]


def test_an_unknown_conversation_is_not_an_empty_page(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, because "nothing was said" and "no such conversation" are different.

    A conversation with no messages yet is a real answer; an id that names
    nothing is not, and returning the same empty page for both leaves the caller
    unable to tell.
    """

    empty, _ = _conversation(seeded_database)
    theirs, _ = _conversation(seeded_database, tenant_key="tenant_b")
    client, headers = _client_for(settings, seeded_database, ["message.list"])

    quiet = client.get(f"/conversations/{empty}/messages", headers=headers)
    unknown = client.get(f"/conversations/{uuid4()}/messages", headers=headers)
    across = client.get(f"/conversations/{theirs}/messages", headers=headers)

    assert quiet.status_code == 200, quiet.text
    assert quiet.json()["items"] == []
    assert unknown.status_code == 404
    # Another tenant's conversation is the same 404 -- RLS hides it, and the two
    # must stay indistinguishable or the route becomes an existence oracle.
    assert across.status_code == 404
    assert across.json()["code"] == unknown.json()["code"] == "CONVERSATION_NOT_FOUND"


def test_reading_messages_needs_the_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    conversation_id, _ = _conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = client.get(f"/conversations/{conversation_id}/messages", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_message_content_is_not_a_viewers_to_read(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The criterion in `system_roles.py`, applied and asserted.**

    "A read action joins `viewer` unless it exposes conversation or message
    content" -- `message.list` is the first action to hit that first bullet, and
    the `VIEWER_SNAPSHOT` tripwire is what forced the question to be answered in
    this commit rather than by default.

    `operator` gains it in the same commit: the difference between the two roles
    is exactly that an operator is trusted with conversation content.
    """

    del settings, seeded_database
    from fastagent_v2.runtime.system_roles import system_role_permissions

    roles = system_role_permissions()

    assert "message.list" not in roles["viewer"]
    assert "message.list" in roles["operator"]
    assert "message.list" in roles["owner"]
    # The neighbouring state read stays where it was -- B3 #2 publishes no slot
    # values, so `conversation_aggregate.read` still satisfies the criterion.
    assert "conversation_aggregate.read" in roles["viewer"]


def test_a_turns_two_messages_are_paged_in_the_order_they_were_sent(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T369 / ADR-0140：**一轮两行出站，逐字记录里的先后要是投递顺序。**

    一轮写的行 `created_at` 与 `source_table` 都相同（前者是事务时间戳），
    所以排序落到最后一个分量。先前那个分量是 `id` —— `uuid4()`，
    **于是人读记录时可能看到「兑现」排在「许诺」前面，而每次翻页都可能不同**。

    ## ⚠️ 夹具站在边界上，所以这条用例在旧实现下**必红**

    两行同一个 `created_at`，而**较大的那个 uuid 给 ordinal 0**：

    * 旧序（`…, id`）→ ordinal 1 排在前面，记录里「后说的」在「先说的」上面；
    * 今天（`…, ordinal, id`）→ 按投递顺序。

    随便挑两个 uuid 的话这条用例有一半概率在旧实现下也绿 ——
    本仓「夹具不能恰好等于错误答案」那一族。
    """

    conversation_id, release_id = _conversation(seeded_database)
    _inbound(seeded_database, conversation_id, release_id, at=BASE, text="讲呗")

    # 一轮两行：同一个 `created_at`，而较大的 uuid 给 ordinal 0。
    first, second = sorted((uuid4(), uuid4()), reverse=True)
    assert first > second, "夹具要求 ordinal 0 拿到较大的那个 id"
    turn_at = BASE + timedelta(seconds=1)
    _outbound(
        seeded_database, conversation_id, at=turn_at, text="先说的那句",
        ordinal=0, row_id=first,
    )
    _outbound(
        seeded_database, conversation_id, at=turn_at, text="后说的那句",
        ordinal=1, row_id=second,
    )

    client, headers = _client_for(settings, seeded_database, ["message.list"])
    items = client.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()["items"]

    outbound = [item["message_id"] for item in items if item["direction"] == "outbound"]
    assert outbound == [str(first), str(second)], (
        "一轮两条消息的先后不是投递顺序 —— 排序落到 uuid4() 上了"
    )
