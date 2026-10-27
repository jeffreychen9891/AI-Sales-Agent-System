"""B3 #2 `GET /conversations/{id}` (parity §6.2, ADR-0054, ADR-0076).

**The three cases that carry this batch are
`test_a_slot_value_never_leaves_the_server`,
`test_the_empty_commitment_ledger_is_stated_not_omitted` and
`test_a_refusal_evidence_value_never_reaches_the_client`.** The route is a few
selects; what makes it correct is what it refuses to say -- customer content it
has no field-level authorisation to filter, an empty state that would be read as
"nothing happened" if omitted, and a field nothing has ever written, which it
does not pretend to have checked.

**Second delivery (T172, user ruling 2026-09-06, ADR-0081)**: migration 042
grants the API role a column-narrow `SELECT` on `audit_events` and
`policy_decisions`, so items (2) and (5) of the takeover-period projection are
here now -- in the detail route and in `resume-preview`, which renders the same
projection. `test_the_grant_is_narrower_than_the_table` is the tripwire on the
"narrow" half -- it goes red the day somebody widens 042 into a table grant.

**The aggregate is seeded through the pipeline's own `_initial_aggregate`**, the
way `test_channel_binding_repin` does it: a hand-written fixture would drift from
the production shape and this route projects that shape field by field.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import (
    REFUSED_SLOT_UPDATE_PAGE,
    TAKEOVER_AUDIT_ACTION,
    create_app,
)
from fastagent_v2.api.commitments_display import (
    COMMITMENT_LEDGER_DISABLED_NOTE,
)
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.contracts import (
    SLOT_UPDATE_REFUSED_ACTION,
    UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
)
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]

#: What a customer typed into a slot. It must not appear anywhere in the
#: response.
#:
#: **Not named `SECRET_...`** -- `check_secrets.py` reads a name like that
#: assigned a literal as a credential and fails the gate, which is correct
#: of it: this is customer content, not a secret, and the two are protected
#: by different rules. The first version of this constant carried the wrong
#: name and the gate said so.
CUSTOMER_SLOT_VALUE = "apartment seven, example road"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _grant(*, tenant_id: Any, employee_id: Any, permissions: list[str]) -> None:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=seeded["tenant_a"],
        employee_id=seeded["employee_a"],
        permissions=permissions,
    )
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


def _seed_conversation(seeded: dict[str, Any], *, tenant_key: str = "tenant_a") -> UUID:
    """A conversation with one human-written slot and one model-written slot."""

    tenant_id = seeded[tenant_key]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        profile = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
            profile_id="synthetic-service-desk", version="1.0.4"
        ).document
        aggregate = _initial_aggregate(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            profile_release_id=release["id"],
            profile=profile,
        )
        aggregate["state_version"] = 7
        aggregate["generation"] = 2
        aggregate["pending_exit_signal"] = None
        frame = aggregate["target_stack"][0]
        frame["goal"] = "seeded goal"
        frame["slots"] = {
            "delivery_address": CUSTOMER_SLOT_VALUE,
            "request_summary": "wants a refund",
        }
        frame["slot_evidence"] = {
            "delivery_address": {
                "confidence": 1.0,
                "evidence_event_id": str(uuid4()),
                "recorded_at": "2026-09-06T00:00:00+00:00",
                "source": "human",
            },
            "request_summary": {
                "confidence": 0.82,
                "evidence_event_id": str(uuid4()),
                "recorded_at": "2026-09-06T00:01:00+00:00",
                "source": "model",
            },
        }
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, generation, state_version,
                target_stack, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                conversation_id,
                release["id"],
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.commit()
    return conversation_id


#: A refusal as `target_stack.py` writes it, plus a field it does **not**
#: write. `value` is here so the whitelist in `_refusal_projection` is under
#: test rather than the writer's promise not to add one: the two services
#: are separately deployable, and a reader that forwards a blob inherits
#: every future decision the writer makes about what goes in it.
REFUSAL_WITH_A_VALUE_THE_WRITER_DOES_NOT_SEND = {
    "frame_id": "frame-1",
    "slot": "delivery_address",
    "reason": "SLOT_WRITTEN_BY_HUMAN",
    "proposed_confidence": 0.91,
    "proposed_evidence_event_id": "11111111-1111-4111-8111-111111111111",
    "recorded_evidence_event_id": "22222222-2222-4222-8222-222222222222",
    "value": CUSTOMER_SLOT_VALUE,
}


def _refuse_a_slot_update(
    seeded: dict[str, Any],
    conversation_id: UUID,
    *,
    tenant_key: str = "tenant_a",
    minutes_ago: int = 0,
    reason_code: str = "HUMAN_WRITTEN_SLOT_UPDATE_REFUSED",
    refusals: list[dict[str, Any]] | None = None,
) -> None:
    """The row `pipeline.py` writes when RFC 4.3 refuses an update."""

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence, created_at
            ) VALUES (%s, %s, NULL, %s, 'conversation_aggregate', %s, %s, %s, %s,
                      now() - (%s || ' minutes')::interval)
            """,
            (
                seeded[tenant_key],
                uuid4(),
                SLOT_UPDATE_REFUSED_ACTION,
                conversation_id,
                uuid4(),
                reason_code,
                Jsonb(
                    {
                        "refusals": (
                            refusals
                            if refusals is not None
                            else [REFUSAL_WITH_A_VALUE_THE_WRITER_DOES_NOT_SEND]
                        )
                    }
                ),
                minutes_ago,
            ),
        )
        connection.commit()


def _halt_understanding(
    seeded: dict[str, Any],
    conversation_id: UUID,
    *,
    tenant_key: str = "tenant_a",
    reason_code: str = UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
) -> None:
    """The `policy_decisions` row the cost governor writes under ADR-0054 §三.6."""

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO policy_decisions(
                tenant_id, id, conversation_id, intent_id, hook, effect,
                reason_code, policy_id, policy_version
            ) VALUES (%s, %s, %s, NULL, 'pre_inference', 'allow', %s,
                      'cost-governor', '1.0.0')
            """,
            (
                seeded[tenant_key],
                uuid4(),
                conversation_id,
                reason_code,
            ),
        )
        connection.commit()


#: The confidence refusal, which `target_stack.py` writes with one field the
#: human-written one does not have: `recorded_confidence`, the number the
#: proposal was compared against. Independent review P2-1 found the whitelist
#: written against the other shape only, so every fixture in this file used to
#: be the shape that could not notice.
LOW_CONFIDENCE_REFUSAL = {
    "frame_id": "frame-1",
    "slot": "request_summary",
    "reason": "LOW_CONFIDENCE_OVER_HIGH",
    "proposed_confidence": 0.31,
    "recorded_confidence": 0.97,
    "proposed_evidence_event_id": "33333333-3333-4333-8333-333333333333",
    "recorded_evidence_event_id": "44444444-4444-4444-8444-444444444444",
}


def _take_over(
    seeded: dict[str, Any],
    conversation_id: UUID,
    *,
    tenant_key: str = "tenant_a",
    minutes_ago: int = 30,
) -> None:
    """Put the conversation in a person's hands, the way the route does.

    The audit row is what matters: its `created_at` **is** the start of the
    takeover period, and the projection bounds both of its queries by it
    (independent review P1-1). `control_mode` is flipped alongside because a
    row that claims a takeover happened while the column says otherwise is a
    state the route never produces.
    """

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence, created_at
            ) VALUES (%s, %s, NULL, %s, 'conversation_aggregate', %s, %s,
                      'CONVERSATION_TAKEN_OVER', '{}'::jsonb,
                      now() - (%s || ' minutes')::interval)
            """,
            (
                seeded[tenant_key],
                uuid4(),
                TAKEOVER_AUDIT_ACTION,
                conversation_id,
                uuid4(),
                minutes_ago,
            ),
        )
        connection.execute(
            "UPDATE conversation_aggregates SET control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (seeded[tenant_key], conversation_id),
        )
        connection.commit()


def _governance(client: TestClient, headers: dict[str, str], conversation_id: UUID) -> Any:
    body = client.get(f"/conversations/{conversation_id}", headers=headers)
    assert body.status_code == 200, body.text
    return body.json()["takeover_projection"]


def test_a_promoted_reader_sees_the_conversation_state(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The happy path: state, not messages."""

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = client.get(f"/conversations/{conversation_id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["conversation_id"] == str(conversation_id)
    assert body["lifecycle"] == "open"
    assert body["control_mode"] == "ai"
    # §6.2 calls it the projection version; the column is `state_version`.
    assert body["projection_version"] == 7
    assert body["generation"] == 2
    assert body["target_stack"], "the seeded frame should be projected"
    assert response.headers["cache-control"] == "no-store, private"


def test_a_slot_value_never_leaves_the_server(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The narrow reading of 信息槽摘要, asserted against the whole body.**

    parity §6.2 asks for a slot *summary* and says in the next clause that
    detailed sensitive evidence needs an extra action. A slot's value is what
    the customer typed, and this repository has no field-level authorisation to
    separate a harmless slot from an address (T164 registered that gap). So the
    projection carries provenance -- name, confidence, who wrote it, which event
    it came from -- and never the value.

    Asserted on the **serialised body** rather than on one field, because the
    interesting failure is a value reaching the wire through some path nobody
    listed: a nested `state` blob, an echoed frame, a debug field.
    """

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = client.get(f"/conversations/{conversation_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert CUSTOMER_SLOT_VALUE not in response.text
    slots = {slot["name"]: slot for slot in response.json()["target_stack"][0]["slots"]}
    assert set(slots) == {"delivery_address", "request_summary"}
    assert slots["delivery_address"]["source"] == "human"
    assert slots["delivery_address"]["confidence"] == 1.0
    assert "value" not in slots["delivery_address"]


def test_the_takeover_projection_splits_slots_by_who_wrote_them(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0054 §三.2: a human-written slot is not evidence the model may revise.

    So "which of these did a person put there" is the first question a returning
    agent has to answer, and the projection answers it without being asked.
    """

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    written = client.get(f"/conversations/{conversation_id}", headers=headers).json()[
        "takeover_projection"
    ]["slots_written"]

    assert [slot["name"] for slot in written["human"]] == ["delivery_address"]
    assert [slot["name"] for slot in written["model"]] == ["request_summary"]
    assert written["unattributed"] == []
    assert CUSTOMER_SLOT_VALUE not in str(written)


def test_the_empty_commitment_ledger_is_stated_not_omitted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0054's revision to §6.2, in as many words.**

    「必须显式显示「未登记任何承诺」，不得省略。空态被省略会被读成『系统记住了』」.
    The ledger does not exist in M1a, so this is the only value this field can
    have -- which is exactly when a developer is most tempted to drop it.
    """

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    body = client.get(f"/conversations/{conversation_id}", headers=headers).json()

    assert body["commitments"]["registered"] == 0
    assert body["commitments"]["note"]
    assert body["takeover_projection"]["commitments_registered"]["count"] == 0
    assert body["takeover_projection"]["commitments_registered"]["note"]

    # **「显示」与「显示成什么」是两件事**（用户 2026-09-13）。
    # 图纸那半句一个字没动 —— 上面四条断言原样保留：字段照显、不省略。
    #
    # **2026-09-16（批 11）这个 0 换了意思。** 先前台账压根不存在，
    # 于是那个 0 是一个**会误导人的真值**（「这个位置数不出任何东西」），
    # `status` 那一格说清它是哪一种 0。现在表在、写入方在、读出方在 ——
    # **这一轮真的找过了，而这条会话确实还没有登记任何承诺**。
    #
    # 所以断言从「必须是 `not_enabled`」翻成「**不许再是** `not_enabled`」：
    # 前者在本批之后会变成一句假话，而假话比缺一条断言更贵。
    for shown in (body["commitments"], body["takeover_projection"]["commitments_registered"]):
        assert shown["status"] == "enabled", (
            "台账已经落地（表 + 写入方 + 读出方同批），这个 0 现在是「真的没有」，"
            "不该再说「未启用」"
        )
        assert str(shown["note"]) != COMMITMENT_LEDGER_DISABLED_NOTE, (
            "「未启用」那句话不该再出现 —— 它今天说的是假话"
        )
        # **两处仍然共用同一句话**：措辞只有一处
        # （`commitments_display`），用例跟着它走，不在这里抄第二遍。
        assert str(shown["note"]) == str(
            body["commitments"]["note"]
        ), "两处显示要共用同一句话"


def test_an_absent_exit_signal_is_a_sentence(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Empty means the customer has not said they are leaving -- a finding."""

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    signal = client.get(f"/conversations/{conversation_id}", headers=headers).json()[
        "takeover_projection"
    ]["pending_exit_signal"]

    assert signal["present"] is False
    assert signal["note"]


def test_the_unwritten_projection_is_absent_and_the_looked_up_ones_are_zero(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0076 §四.2 still holds, and 042 moved two items across it.**

    The rule was never "omit what is empty" -- it was "do not claim to have
    looked". Manipulation observation has no writer anywhere, so it is still
    absent; a `null` there would tell a client "checked, nothing there", which
    is the same falsehood B2 #1 refused when four columns had no writer.

    The other two are now looked up (042), so their empty state is a **finding**
    and is stated: no refused slot update means the model stayed inside the
    lines, and that is something an operator reviewing a takeover wants to read
    rather than infer from a missing key.
    """

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    projection = client.get(f"/conversations/{conversation_id}", headers=headers).json()[
        "takeover_projection"
    ]

    assert "manipulation_observation" not in projection
    # **Which kind of empty** (independent review P1-1): nobody has ever held
    # this conversation, so there is no takeover period to be empty *of*.
    assert projection["takeover_period"]["source"] == "none"
    assert projection["refused_slot_updates"]["total"] == 0
    assert projection["refused_slot_updates"]["items"] == []
    assert projection["refused_slot_updates"]["note"]
    assert projection["understanding_halted_by_cost_gate"]["halted"] is False
    assert projection["understanding_halted_by_cost_gate"]["occurrences"] == 0
    assert projection["understanding_halted_by_cost_gate"]["last_at"] is None


def test_another_tenants_conversation_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, and the same 404 a missing id gets.

    A row this tenant cannot see and a row that does not exist have to be
    indistinguishable, or the response becomes an existence oracle for another
    merchant's conversations. RLS makes both arrive at the same branch.
    """

    theirs = _seed_conversation(seeded_database, tenant_key="tenant_b")
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    across = client.get(f"/conversations/{theirs}", headers=headers)
    absent = client.get(f"/conversations/{uuid4()}", headers=headers)

    assert across.status_code == 404, across.text
    assert absent.status_code == 404
    # The **code** has to match, not the whole envelope: `request_id`
    # differs per request by design, and comparing bodies would make this
    # case fail for a reason that has nothing to do with the oracle it
    # exists to prevent.
    assert across.json()["code"] == absent.json()["code"] == "CONVERSATION_NOT_FOUND"


def test_reading_a_conversation_needs_the_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The action is a normal tenant read -- and it is still a permission."""

    conversation_id = _seed_conversation(seeded_database)
    client, headers = _client_for(settings, seeded_database, ["employee.list"])

    response = client.get(f"/conversations/{conversation_id}", headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "AUTHORIZATION_DENIED"


def test_a_refused_slot_update_reaches_the_operator(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T172 item (2), the whole point of migration 042.**

    ADR-0054 §三.2 makes a human-written slot something the model may not
    revise. The refusal is the record that it tried -- and until 042 the
    operator reviewing the takeover could not see it, because the row lives in
    `audit_events` and the API role held `INSERT` and nothing else.

    Newest first, and the reason code is projected rather than flattened: the
    writer keeps `HUMAN_WRITTEN_...` apart from `LOWER_CONFIDENCE_...` because
    "an operator who cannot tell them apart will eventually fix the second by
    loosening the first" -- its words -- and collapsing them here would undo
    that at the last step.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=9)
    _refuse_a_slot_update(
        seeded_database,
        conversation_id,
        minutes_ago=1,
        reason_code="LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED",
    )
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    refused = _governance(client, headers, conversation_id)["refused_slot_updates"]

    assert refused["total"] == 2
    assert refused["shown"] == 2
    assert [item["reason_code"] for item in refused["items"]] == [
        "LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED",
        "HUMAN_WRITTEN_SLOT_UPDATE_REFUSED",
    ], "newest first"
    detail = refused["items"][0]["refusals"][0]
    assert detail["slot"] == "delivery_address"
    assert detail["reason"] == "SLOT_WRITTEN_BY_HUMAN"
    assert detail["proposed_confidence"] == 0.91


def test_a_refusal_evidence_value_never_reaches_the_client(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The reader owns its boundary, not the writer.**

    `target_stack.py` deliberately records refusals without slot values. This
    route could forward `evidence` unchanged and be correct today -- and would
    silently start publishing customer content the first time any audit writer
    added a field. The seeded evidence carries a `value` the real writer never
    sends, and it must not appear anywhere in the response.

    Replace `_REFUSAL_FIELDS` filtering with a plain forward of `evidence` and
    this is the case that goes red.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(seeded_database, conversation_id)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = client.get(f"/conversations/{conversation_id}", headers=headers)

    assert CUSTOMER_SLOT_VALUE not in response.text
    projected = response.json()["takeover_projection"]["refused_slot_updates"]
    assert "value" not in projected["items"][0]["refusals"][0]


def test_the_refusal_list_is_capped_and_says_how_many_there_are(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`total` is a count, not "twenty or more".

    B3 #11 shipped `conversations_remaining` as a `LIMIT + 1` probe -- a boolean
    wearing a count's name -- and an independent review had to find it. Here the
    total comes from `count(*) OVER ()`, so seeding one more than the cap
    distinguishes the two implementations: a `LIMIT + 1` version would report
    `21` here and `21` for a hundred refusals too.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    for minute in range(REFUSED_SLOT_UPDATE_PAGE + 3):
        _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=minute)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    refused = _governance(client, headers, conversation_id)["refused_slot_updates"]

    assert refused["total"] == REFUSED_SLOT_UPDATE_PAGE + 3
    assert refused["shown"] == REFUSED_SLOT_UPDATE_PAGE
    assert len(refused["items"]) == REFUSED_SLOT_UPDATE_PAGE


def test_the_cost_gate_interruption_is_counted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T172 item (5).**

    ADR-0054 §三.6: a cost ceiling can stop the understanding step of a
    conversation a person already holds, and the handoff that would normally
    follow is a no-op because the conversation is already there. The only trace
    is a `policy_decisions` row, which is why the operator could not see it
    before 042 -- the API role had no privilege on that table at all.

    A count rather than a flag: twice means the customer kept typing into a
    conversation whose understanding is dark, which is a different situation
    from once.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _halt_understanding(seeded_database, conversation_id)
    _halt_understanding(seeded_database, conversation_id)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    halted = _governance(client, headers, conversation_id)["understanding_halted_by_cost_gate"]

    assert halted["halted"] is True
    assert halted["occurrences"] == 2
    assert halted["last_at"] is not None


def test_another_tenants_governance_rows_are_not_projected(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The first audit read is also the first way to leak one.**

    The refusal row is seeded under tenant B while naming tenant A's
    conversation -- the shape a bug produces, and the one RLS exists to stop.
    `audit_events.resource_id` is a bare `uuid` with no foreign key, precisely
    because audit outlives the rows it describes, so nothing but the tenant
    predicate and the policy (`001:299`, ENABLE + FORCE) stands between the two
    merchants here.

    **`policy_decisions` cannot be seeded that way at all**, and finding that
    out is worth as much as the assertion: the table carries a composite
    `(tenant_id, conversation_id)` foreign key, so a decision row about another
    tenant's conversation is unconstructible rather than merely unreadable. The
    gate refused the first version of this case for exactly that reason. So the
    halt row goes on tenant B's own conversation, which is the strongest shape
    the schema permits.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    theirs = _seed_conversation(seeded_database, tenant_key="tenant_b")
    _take_over(seeded_database, theirs, tenant_key="tenant_b")
    _refuse_a_slot_update(seeded_database, conversation_id, tenant_key="tenant_b")
    _halt_understanding(seeded_database, theirs, tenant_key="tenant_b")
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    projection = _governance(client, headers, conversation_id)

    assert projection["refused_slot_updates"]["total"] == 0
    assert projection["understanding_halted_by_cost_gate"]["occurrences"] == 0
    # And tenant B's own conversation is a 404 here, so there is no route that
    # returns the row it did write -- the oracle case next door proves the 404.
    assert client.get(f"/conversations/{theirs}", headers=headers).status_code == 404


def test_the_grant_is_narrower_than_the_table(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**042 is column-level, and this is the tripwire on that word.**

    The user ruled 「发窄 `SELECT`」 (2026-09-06); ADR-0081 §三 says what "narrow"
    bought and what it did not. A later `GRANT SELECT ON audit_events` -- one
    line shorter, and the obvious thing to write when a route needs one more
    column -- would pass every other case in this file. It fails here.

    `correlation_id` is the audit trail's cross-resource key and
    `policy_decisions.context` carries the engine's ceilings and model ids;
    neither is needed to answer §6.2, so neither was granted.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    api = Database(settings.database_url)

    with api.transaction(tenant_id=tenant_a) as connection:
        connection.execute(
            "SELECT reason_code, evidence FROM audit_events WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchall()

    for statement in (
        "SELECT correlation_id FROM audit_events",
        "SELECT * FROM audit_events",
        "SELECT context FROM policy_decisions",
        "SELECT * FROM policy_decisions",
    ):
        with pytest.raises(InsufficientPrivilege):  # noqa: PT012
            with api.transaction(tenant_id=tenant_a) as connection:
                connection.execute(statement).fetchall()


def test_the_resume_preview_digest_does_not_move_when_a_refusal_lands(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The digest covers the document; the projection covers both.**

    Two rules pull in opposite directions here and both are kept.

    The preview route says 「接管期变化投影」 has one definition and that two
    routes rendering it two ways is how they drift -- so the preview shows the
    same five items the detail page does, refusals included.

    But `_change_digest` is the token `POST .../resume` compares against what
    the operator acknowledged, and it is computed over the aggregate document
    alone. Hashing rows the worker writes would make the token go stale while
    the conversation sat still -- a 409 telling an operator to re-read a
    picture that did not change. Hash the merged dict instead and this case
    goes red on its first assertion.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])
    before = client.get(f"/conversations/{conversation_id}/resume-preview", headers=headers)
    assert before.status_code == 200, before.text

    _refuse_a_slot_update(seeded_database, conversation_id)
    after = client.get(f"/conversations/{conversation_id}/resume-preview", headers=headers)

    assert after.status_code == 200, after.text
    assert after.json()["change_digest"] == before.json()["change_digest"]
    # ...and the operator still sees it, in the same shape the detail page uses.
    assert after.json()["takeover_projection"]["refused_slot_updates"]["total"] == 1
    assert before.json()["takeover_projection"]["refused_slot_updates"]["total"] == 0


def test_only_the_two_named_kinds_of_row_are_projected(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The narrow grant is narrow in columns; the narrowing by *row* is here.**

    042 lets this role read `audit_events.evidence` for **any** action inside
    its own tenant (ADR-0081 §三 says so plainly, and T181 registers it). What
    keeps the conversation detail from showing an unrelated audit row is one
    `action = %s` in the query and one `reason_code = %s` in the other -- a
    query, not a privilege, which is exactly why they are asserted.

    The unrelated audit row carries a customer value in its evidence, the way a
    future writer might. Drop the `action` predicate and this case goes red
    twice: on the count, and on the value reaching the client.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(seeded_database, conversation_id)
    _halt_understanding(seeded_database, conversation_id)
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'conversation.taken_over',
                      'conversation_aggregate', %s, %s, 'CONVERSATION_TAKEN_OVER', %s)
            """,
            (
                seeded_database["tenant_a"],
                uuid4(),
                conversation_id,
                uuid4(),
                Jsonb({"note": CUSTOMER_SLOT_VALUE}),
            ),
        )
        connection.commit()
    # A policy decision on the same conversation that is not the cost halt.
    _halt_understanding(seeded_database, conversation_id, reason_code="PROPOSAL_REFUSED")
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    response = client.get(f"/conversations/{conversation_id}", headers=headers)
    projection = response.json()["takeover_projection"]

    assert projection["refused_slot_updates"]["total"] == 1, "the takeover row is not a refusal"
    assert projection["understanding_halted_by_cost_gate"]["occurrences"] == 1
    assert CUSTOMER_SLOT_VALUE not in response.text


def test_a_refusal_from_before_the_takeover_is_not_in_the_takeover_projection(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P1-1, the defect this case exists for.**

    The projection used to read every refusal the conversation had ever
    collected, under a key called 「接管期变化投影」. A conversation nobody had
    ever taken over reported refusals; one taken over this morning reported
    last month's confidence refusals as if the person holding it had caused
    them.

    The bound is real and was in the table 042 had just opened: the
    `conversation_takeover.create` audit row's `created_at`, readable with the
    four columns 042 grants. The docstring, the self-review and ADR-0081 all
    said no such column existed, which is why the review calls the wrong
    reasoning the worse half of the finding.
    """

    conversation_id = _seed_conversation(seeded_database)
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=90)  # before
    _take_over(seeded_database, conversation_id, minutes_ago=60)
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=30)  # during
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    projection = _governance(client, headers, conversation_id)

    assert projection["takeover_period"]["source"] == "takeover"
    assert projection["takeover_period"]["started_at"] is not None
    assert projection["refused_slot_updates"]["total"] == 1, "the older one is outside"
    assert projection["refused_slot_updates"]["shown"] == 1


def test_the_second_takeover_starts_a_new_period(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`max(created_at)`, not `min`: 「接管期」 is the one being looked at.

    A conversation can be handed over, resumed and handed over again. Taking
    the earliest row would make the projection describe a period that ended,
    and would grow monotonically for the rest of the conversation's life.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id, minutes_ago=120)
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=100)
    _take_over(seeded_database, conversation_id, minutes_ago=10)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    refused = _governance(client, headers, conversation_id)["refused_slot_updates"]

    assert refused["total"] == 0, "the first period is over"


def test_a_conversation_born_in_human_mode_has_no_takeover_row_and_still_has_a_period(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The edge the review named** (P1-1, third bullet of its fix).

    Migration 037 lets a tenant put every new conversation in `human`
    (ADR-0077). Those conversations are held by a person and **no takeover row
    will ever exist for them**, so a bound of "the last takeover" would either
    fall back to no bound silently or report them as never held. The period is
    the whole conversation, `started_at` is `None` because
    `conversation_aggregates` has no birth timestamp, and `source` says which
    of the three cases this is.
    """

    conversation_id = _seed_conversation(seeded_database)
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET control_mode = 'human'"
            " WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], conversation_id),
        )
        connection.commit()
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=5)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    projection = _governance(client, headers, conversation_id)

    assert projection["takeover_period"]["source"] == "conversation_start"
    assert projection["takeover_period"]["started_at"] is None
    assert projection["refused_slot_updates"]["total"] == 1, "the whole conversation counts"


def test_the_two_refusal_kinds_are_counted_apart(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P1-1, second half**: one total cannot carry both.

    `SLOT_WRITTEN_BY_HUMAN` is ADR-0054 §三.2 -- the model trying to revise
    what a person wrote. `LOW_CONFIDENCE_OVER_HIGH` is RFC 4.3's ordinary
    confidence rule and happens on any turn. ADR-0081 read "the model stayed
    inside §三.2" off their **sum**, which is wrong in both directions: zero
    can hide the absence of the rule that matters, and non-zero can be entirely
    confidence refusals.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(seeded_database, conversation_id, minutes_ago=5)
    _refuse_a_slot_update(
        seeded_database,
        conversation_id,
        minutes_ago=4,
        reason_code="LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED",
        refusals=[LOW_CONFIDENCE_REFUSAL],
    )
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    refused = _governance(client, headers, conversation_id)["refused_slot_updates"]

    assert refused["total"] == 2
    assert refused["by_reason_code"] == {
        "HUMAN_WRITTEN_SLOT_UPDATE_REFUSED": 1,
        "LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED": 1,
    }


def test_a_confidence_refusal_keeps_the_number_it_was_compared_against(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P2-1**: the whitelist was written against one shape.

    The confidence rule *is* the comparison of two numbers. An operator shown
    `proposed 0.31` without `recorded 0.97` cannot tell a sane refusal from a
    customer correction that should have gone through -- and `target_stack.py`
    argues at length that the second kind is worth revisiting.

    A review probe added the field to `_REFUSAL_FIELDS` and all sixteen cases
    stayed green: its absence was not a decision anything defended. This is
    that assertion.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(
        seeded_database,
        conversation_id,
        reason_code="LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED",
        refusals=[LOW_CONFIDENCE_REFUSAL],
    )
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    detail = _governance(client, headers, conversation_id)["refused_slot_updates"]
    projected = detail["items"][0]["refusals"][0]

    assert projected["proposed_confidence"] == 0.31
    assert projected["recorded_confidence"] == 0.97, "the baseline the rule compares to"


def test_the_detail_page_and_the_preview_render_the_same_projection(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review P3-4**: the core argument had no case behind it.

    `conversation_resume_preview`'s docstring and ADR-0081 §四 both say two
    routes rendering 「接管期变化投影」 differently is how they drift -- and the
    merge happens **twice**, once in each route. Adding a third item to one of
    them left every case green.
    """

    conversation_id = _seed_conversation(seeded_database)
    _take_over(seeded_database, conversation_id)
    _refuse_a_slot_update(seeded_database, conversation_id)
    _halt_understanding(seeded_database, conversation_id)
    client, headers = _client_for(settings, seeded_database, ["conversation_aggregate.read"])

    detail = client.get(f"/conversations/{conversation_id}", headers=headers)
    preview = client.get(f"/conversations/{conversation_id}/resume-preview", headers=headers)

    assert detail.status_code == 200, detail.text
    assert preview.status_code == 200, preview.text
    assert detail.json()["takeover_projection"] == preview.json()["takeover_projection"]
