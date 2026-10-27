"""B3 #11's other half: 「更新默认并可套用当前会话」 (ADR-0079).

**The case that carries this batch is
`test_a_transitioned_conversation_went_through_the_takeover_body`.** The
tempting implementation of "apply the policy to existing conversations" is one
`UPDATE ... SET control_mode = 'human'`, and it would pass any test that only
looked at `control_mode`. What it would skip is everything ADR-0054 and ADR-0058
put on taking a conversation over: the stack stays unsuspended, the generation
does not move, and **live timers keep firing into conversations a person now
holds**. So that case asserts the invariants, not the mode.

**The refusal is a case too.** `→ ai` is not missing, it is refused with a
reason (ADR-0079 §二), and the two halves of that reason are different in kind:
safety asymmetry, and a frequency judgement the user made.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import BULK_TRANSITION_LIMIT, create_app
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

WORKING = ["control_policy.update", "control_policy.read", "conversation_takeover.create"]


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


def _profile() -> dict[str, Any]:
    return (
        ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
        .package(profile_id="synthetic-service-desk", version="1.0.4")
        .document
    )


def _conversation(seeded: dict[str, Any], *, tenant_key: str = "tenant_a") -> tuple[UUID, UUID]:
    """One AI-controlled conversation, plus a pending timer on it.

    The timer is what makes the difference between a real takeover and a mode
    flip visible: a shortcut leaves it `PENDING`, and it later fires into a
    conversation somebody is holding.
    """

    tenant_id = seeded[tenant_key]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        aggregate = _initial_aggregate(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            profile_release_id=release["id"],
            profile=_profile(),
        )
        frame_id = UUID(str(aggregate["target_stack"][0]["id"]))
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, state_version, lifecycle, control_mode, generation,
                profile_release_id, target_stack, state
            ) VALUES (%s, %s, %s, 'open', 'ai', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                conversation_id,
                aggregate["state_version"],
                aggregate["generation"],
                release["id"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id, status
            ) VALUES (%s, %s, %s, now() + interval '1 hour', 'silence_check', %s,
                      %s, 0, %s, 0, %s, 'PENDING')
            """,
            (
                tenant_id,
                uuid4(),
                conversation_id,
                f"silence:{conversation_id}",
                release["id"],
                frame_id,
                uuid4(),
            ),
        )
        connection.commit()
    return conversation_id, frame_id


def _put(client: TestClient, headers: dict[str, str], **body: Any) -> Any:
    payload: dict[str, Any] = {
        "default_control_mode": "human",
        "understanding_during_takeover": True,
    }
    payload.update(body)
    return client.put(
        "/control-policy",
        json=payload,
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )


def _aggregate(seeded: dict[str, Any], conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT control_mode, generation, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (seeded["tenant_a"], conversation_id),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_a_transitioned_conversation_went_through_the_takeover_body(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The case this batch is built around** (ADR-0079 §三).

    Four things must all be true, and only the first survives a bulk `UPDATE`:
    the mode is `human`, the target stack is suspended, the generation moved,
    and the pending timer is `INVALIDATED`. The last one is the one with teeth
    -- a timer that fires into a conversation a person now holds acts on a plan
    that person has replaced.

    The audit row carries its own reason code, so somebody reading the trail can
    tell one deliberate takeover from a hundred a policy change produced.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, _ = _conversation(seeded_database)
    before = _aggregate(seeded_database, conversation_id)

    response = _put(client, headers, transition_existing="human")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["conversations_transitioned"] == 1
    assert response.json()["result"]["conversations_remaining"] == 0

    after = _aggregate(seeded_database, conversation_id)
    assert after["control_mode"] == "human"
    assert int(after["generation"]) == int(before["generation"]) + 1
    assert [frame["status"] for frame in after["state"]["target_stack"]] == ["suspended"]

    with _migration_connection() as connection:
        events = connection.execute(
            "SELECT status, invalidation_reason FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (seeded_database["tenant_a"], conversation_id),
        ).fetchall()
        audit = connection.execute(
            "SELECT reason_code, evidence FROM audit_events"
            " WHERE tenant_id = %s AND resource_id = %s"
            " AND action = 'conversation_takeover.create'",
            (seeded_database["tenant_a"], conversation_id),
        ).fetchone()
    assert [row["status"] for row in events] == ["INVALIDATED"]
    assert events[0]["invalidation_reason"] == "CONVERSATION_TAKEN_OVER"
    assert audit is not None
    assert audit["reason_code"] == "CONVERSATION_TAKEN_OVER_BY_POLICY"
    assert audit["evidence"]["events_invalidated"] == 1


def test_returning_conversations_to_the_ai_is_refused_by_name(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0079 §二: the direction is refused, not omitted.

    Leaving `"ai"` out of the request model would make a caller who asked for it
    read a schema error and guess. The route names the refusal instead, and the
    ADR keeps its two reasons apart: **safety is asymmetric** (taking control
    back has to inherit the takeover's facts, and ADR-0054 §三.4 makes that
    two-phase precisely so a person reads the preview), and **frequency is
    asymmetric** -- which is the user's judgement, marked as such so nobody
    later tries to verify it against the code.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, _ = _conversation(seeded_database)

    response = _put(client, headers, transition_existing="ai")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "BULK_TRANSITION_TO_AI_NOT_SUPPORTED"

    # Nothing at all happened -- not the policy either, because the refusal is
    # before the write.
    assert _aggregate(seeded_database, conversation_id)["control_mode"] == "ai"
    assert client.get("/control-policy", headers=headers).json()["default_control_mode"] == "ai"


def test_the_default_is_not_to_touch_conversations_in_flight(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Applying a policy to live conversations is a second decision.

    A caller who only meant to change what new conversations do should not make
    the other decision by omission, so `transition_existing` defaults to
    `"none"` and the answer says plainly that nothing moved.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id, _ = _conversation(seeded_database)

    response = _put(client, headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["conversations_transitioned"] == 0
    assert _aggregate(seeded_database, conversation_id)["control_mode"] == "ai"


def test_a_conversation_a_person_already_holds_is_skipped_not_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The bulk caller named no conversation, so it counts rather than 409s.

    `POST /conversations/{id}/takeover` answers 409 for this, and should: that
    caller named the conversation. Here the same condition is simply "not this
    one" -- and the count is what tells the operator how many actually moved.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    already_held, _ = _conversation(seeded_database)
    still_ai, _ = _conversation(seeded_database)
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], already_held),
        )
        connection.commit()

    response = _put(client, headers, transition_existing="human")
    assert response.status_code == 200, response.text
    assert response.json()["result"]["conversations_transitioned"] == 1
    assert _aggregate(seeded_database, still_ai)["control_mode"] == "human"


def test_more_than_a_page_is_capped_and_the_caller_is_told(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """K13's cap, kept by ADR-0079: 一次操作的影响面要能在一屏内复核.

    The cap is about review, not throughput, so exceeding it is not an error --
    the call transitions a page and reports that more remain. Silently
    truncating would leave an operator believing the tenant was fully handed
    over.

    Seeded with two more than the limit so "a page" and "the rest" are both
    non-empty; the aggregates are written directly because what is under test is
    the cap, not how conversations come into being.

    **`updated_at` runs backwards against insertion order on purpose**
    (independent review P2-3): the row inserted first is the *newest*, so
    "oldest first" and "whatever order the table hands back" disagree. The two
    left after the first call are therefore the two inserted first -- an
    assertion that fails when `ORDER BY updated_at, id` is removed, which a
    review probe showed the previous cases did not notice.

    (The earlier attempt asserted the order of the audit rows instead. It does
    not work: `now()` is the transaction timestamp, so all hundred rows carry
    the same one and "order by created_at" quietly means "order by id".)
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1",
            (seeded_database["tenant_a"],),
        ).fetchone()
        template = _initial_aggregate(
            tenant_id=seeded_database["tenant_a"],
            conversation_id=uuid4(),
            profile_release_id=release["id"],
            profile=_profile(),
        )
        seeded: list[str] = []
        for index in range(BULK_TRANSITION_LIMIT + 2):  # a page, and two more
            conversation_id = uuid4()
            seeded.append(str(conversation_id))
            document = dict(template)
            document["id"] = str(conversation_id)
            connection.execute(
                """
                INSERT INTO conversation_aggregates(
                    tenant_id, id, state_version, lifecycle, control_mode, generation,
                    profile_release_id, target_stack, state, updated_at
                ) VALUES (%s, %s, 0, 'open', 'ai', 0, %s, %s, %s,
                          now() - (%s || ' seconds')::interval)
                """,
                (
                    seeded_database["tenant_a"],
                    conversation_id,
                    release["id"],
                    Jsonb(template["target_stack"]),
                    Jsonb(document),
                    index,
                ),
            )
        connection.commit()

    first = _put(client, headers, transition_existing="human")
    assert first.status_code == 200, first.text
    assert first.json()["result"]["conversations_transitioned"] == BULK_TRANSITION_LIMIT
    # **A count, not a boolean** (independent review P1-2). The candidate
    # query used to fetch `LIMIT + 1` rows, so this field could only ever say
    # 0 or 1 -- 105 waiting reported "1 left", and this very case asserted the
    # wrong value while looking right, because it seeded exactly `LIMIT + 2`.
    assert first.json()["result"]["conversations_remaining"] == 2, "the real number"

    # **The oldest went first**: the two still on the AI are the two inserted
    # first, which are the newest by `updated_at`.
    with _migration_connection() as connection:
        still_ai = connection.execute(
            "SELECT id FROM conversation_aggregates"
            " WHERE tenant_id = %s AND control_mode = 'ai'",
            (seeded_database["tenant_a"],),
        ).fetchall()
    assert {str(row["id"]) for row in still_ai} == set(seeded[:2])

    second = _put(client, headers, transition_existing="human")
    assert second.json()["result"]["conversations_transitioned"] == 2
    assert second.json()["result"]["conversations_remaining"] == 0

    with _migration_connection() as connection:
        left = connection.execute(
            "SELECT count(*) AS n FROM conversation_aggregates"
            " WHERE tenant_id = %s AND control_mode = 'ai'",
            (seeded_database["tenant_a"],),
        ).fetchone()["n"]
    assert left == 0


def test_another_tenants_conversations_are_never_transitioned(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The candidate query is tenant-scoped, and RLS is under it.

    A bulk write is the shape where a missing predicate does the most damage, so
    it gets its own assertion rather than relying on the policy alone.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    mine, _ = _conversation(seeded_database)
    theirs, _ = _conversation(seeded_database, tenant_key="tenant_b")

    assert _put(client, headers, transition_existing="human").status_code == 200

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT control_mode FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_b"], theirs),
        ).fetchone()
    assert row["control_mode"] == "ai", "another tenant's conversation must not move"
    assert _aggregate(seeded_database, mine)["control_mode"] == "human"


def test_a_conversation_the_body_refuses_is_counted_apart(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P2-1 / P2-3**: it used to vanish from the answer.

    `_hand_conversation_to_a_person` refuses a conversation whose document the
    engine cannot process either -- the single-conversation route answers 409
    `CONVERSATION_NOT_RESUMABLE` for exactly that. In the batch it was counted
    as neither moved nor remaining, so the caller was told the tenant had been
    fully handed over while one conversation sat on the AI and would stay
    there through every retry.

    A review probe replaced `if moved is not None:` with `if True:` and all
    six cases stayed green -- "how many actually moved" was a promise in a
    comment. This is the tripwire.
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    healthy, _ = _conversation(seeded_database)
    broken, _ = _conversation(seeded_database)
    with _migration_connection() as connection:
        # A document the engine cannot drive: no `target_stack`. The repin
        # path fails closed on the same shape.
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s"
            " WHERE tenant_id = %s AND id = %s",
            (Jsonb({"id": str(broken)}), seeded_database["tenant_a"], broken),
        )
        connection.commit()

    response = _put(client, headers, transition_existing="human")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["conversations_transitioned"] == 1
    assert result["conversations_skipped"] == 1, "looked at, not moved"
    assert result["conversations_remaining"] == 0, "and it is not waiting for another call"

    assert _aggregate(seeded_database, healthy)["control_mode"] == "human"
    assert _aggregate(seeded_database, broken)["control_mode"] == "ai"


def test_moving_live_conversations_needs_the_takeover_grant_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**`C0B-013`'s third acceptance item** (user ruling 2026-09-06, review P1-1).

    「policy release 与批量 transition 分 action」. The cap and the audit trail
    were delivered; this was not -- an employee holding only
    `control_policy.update` moved conversations, and a review probe measured
    it. The two grants are separately meaningful: changing what new
    conversations do is a settings change, handing a hundred live ones to
    people is the takeover act repeated.

    **Both halves are asserted**, because "needs both" is only half a rule if
    the plain update starts needing takeover rights too.
    """

    client, headers = _client_for(
        settings, seeded_database, ["control_policy.update", "control_policy.read"]
    )
    conversation_id, _ = _conversation(seeded_database)

    refused = _put(client, headers, transition_existing="human")
    assert refused.status_code == 403, refused.text
    assert _aggregate(seeded_database, conversation_id)["control_mode"] == "ai"

    # ...and the policy half still works with the one grant.
    allowed = _put(client, headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["result"]["conversations_transitioned"] == 0
    assert client.get("/control-policy", headers=headers).json()["default_control_mode"] == (
        "human"
    )

    # ...and with both grants the transition goes through.
    both_client, both_headers = _client_for(settings, seeded_database, WORKING)
    moved = _put(both_client, both_headers, transition_existing="human")
    assert moved.status_code == 200, moved.text
    assert moved.json()["result"]["conversations_transitioned"] == 1
