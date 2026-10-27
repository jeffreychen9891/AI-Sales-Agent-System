"""B3-2b's criterion is that the content **arrives**, not that a parser is right.

`tests/contracts/test_inbound_content_parsing.py` proves `parse_content` returns
the right dict. **That is not the same claim.** If `accept()` dropped what the
parser produced and stored a text-only envelope, every one of those cases would
still pass -- the same shape as the two probes that came back green on B4-1: a
test that shows the code is correct without showing it is reached.

So these read the envelope back **out of `inbox_events`**, which is the thing a
worker will later consume, and assert on what is actually stored.

**Every payload here is now the vendor's shape** (`docs/wecom_proxy.md`). The
first version of this file sent hand-written mappings with invented field
names, and the argument above -- correct as far as it goes -- does not reach
that: proving the envelope arrives intact proves nothing when the payload it was
built from is one nobody will ever send. The mixed-message case is the one that
mattered: a real `msgType=123` has an **array** `msgData`, so the parser found
neither half and `accept()` refused the whole message, while this file went
green on a mapping that does not occur.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.ingress.adapter import OUTCOME_REFUSED, WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback_body, callback_item

pytestmark = [pytest.mark.integration]

#: The vendor's `msgType=123` example, opaque values shortened. `msgData` is an
#: **array of parts**, and the words in a part are base64: `"NDQ="` is `44`.
MIXED_PART_IMAGE = {"subMsgType": 14, "subMsgData": {"fileId": "3068a", "fileMd5": "2c58"}}
MIXED_PART_TEXT = {"subMsgType": 2, "subMsgData": {"atList": None, "content": "NDQ="}}
MIXED_PAYLOAD = {"msgType": 123, "msgData": [MIXED_PART_IMAGE, MIXED_PART_TEXT]}


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _ingress(settings: Settings) -> WeComProxyIngress:
    return WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )


def _accept(settings: Settings, seeded: dict[str, Any], payload: dict[str, Any]) -> Any:
    """One callback carrying `payload` **as its `data` element**.

    The cases in this file are all about where content is read from, and every
    one of them names element-level keys (`msgType`, `msgData`,
    `base64RawData`). Until this batch they were merged into the top level of
    the body, which is the level the parser wrongly read -- so the suite and the
    defect agreed with each other.
    """

    overrides = dict(payload)
    item = callback_item(
        external_event_id=overrides.pop("external_event_id", f"content-{uuid4()}"),
        sender_id="customer-1",
        text=None,
    )
    item.update(overrides)
    body = callback_body(item)
    return _ingress(settings).accept(
        config_id="config-a",
        path_token=str(seeded["channel_token"]),
        payload=body,
    )


def _stored_content(event_id: Any) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT envelope FROM inbox_events WHERE event_id = %s",
            (event_id,),
        ).fetchone()
    assert row is not None, "the message did not arrive at all"
    return dict(row["envelope"]["content"])


def test_a_mixed_message_arrives_with_both_halves(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """T201, end to end.

    The registered defect is that one of the two halves goes missing **and the
    loss does not report itself**, so the assertion has to name each half
    separately against what was stored.
    """

    result = _accept(settings, seeded_database, MIXED_PAYLOAD)
    assert result.accepted

    content = _stored_content(result.event_id)
    assert content["type"] == "image"
    assert content["text"] == "44"
    assert content["payload_ref"] == "3068a"


def test_a_picture_with_no_caption_is_admitted(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The rule this batch changed, asserted where the change actually lives.

    `accept()` used to require `text` unconditionally. An uncaptioned image was
    therefore an `invalid callback payload` -- the batch that exists to stop
    half a message being lost would have refused the whole of it. The rule is
    now "content must not be empty", and **this is the only case that holds
    `accept()` to it**; the parser-level cases cannot, because the requirement
    they would be testing was never in the parser.
    """

    result = _accept(
        settings,
        seeded_database,
        {"msgType": 14, "msgData": {"fileId": "3068b", "fileSize": 819, "imageHasHd": True}},
    )
    assert result.accepted

    content = _stored_content(result.event_id)
    assert content["type"] == "image"
    assert content["payload_ref"] == "3068b"
    assert "text" not in content


def test_a_callback_carrying_nothing_usable_is_refused_not_stored(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other side of the same rule: empty content is still a refusal.

    Refused as a payload error, which `app.py` answers `200` + business error
    (ADR-0103 §二.1) rather than a non-200 that would spend the callback
    address's deletion budget.
    """

    # **A refusal of that element, not of the request** (2026-09-10). One
    # callback may carry several messages, so a payload fault stopped being an
    # exception out of `accept()` and became one element's outcome -- the
    # refusal itself is unchanged, and `app.py` still answers `200` with the
    # business error. `IngressPayloadError` is still what `_accept_item` raises;
    # it is the loop that catches it.
    result = _accept(settings, seeded_database, {"msgType": 123, "msgData": {}})
    assert result.accepted is False
    assert [item.outcome for item in result.items] == [OUTCOME_REFUSED]
    assert result.items[0].reason_code == "INVALID_CALLBACK_PAYLOAD"
    assert _inbox_rows(result.items[0].event_id) == []


def test_the_plain_text_path_is_unchanged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The shape every existing fixture sends still arrives as text.

    Here because this batch rewrote how `content` is built for **all** inbound,
    not just for the new types -- so the old path needs an assertion that it did
    not become collateral.
    """

    result = _accept(settings, seeded_database, {"text": "hello"})
    assert result.accepted
    assert _stored_content(result.event_id) == {"type": "text", "text": "hello"}


def test_an_unstorable_picture_reference_does_not_take_the_message_with_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**P2-1, asserted where it actually bit**: through `accept()`, into the row.

    The parser-level case proves the degradation; only this one proves the
    message *survives* it. Before the fix a media reference carrying a NUL
    raised `UntranslatableCharacter` out of `accept()` and the customer's
    sentence went with the picture.
    """

    broken_image = {"subMsgType": 14, "subMsgData": {"fileId": "306\x008a"}}
    result = _accept(
        settings,
        seeded_database,
        {"msgType": 123, "msgData": [broken_image, MIXED_PART_TEXT]},
    )
    assert result.accepted

    content = _stored_content(result.event_id)
    assert content == {"type": "text", "text": "44"}

def _audit_rows(action: str) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT * FROM audit_events WHERE action = %s ORDER BY created_at",
            (action,),
        ).fetchall()


def _inbox_rows(event_id: Any) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT * FROM inbox_events WHERE event_id = %s", (event_id,)
        ).fetchall()


def test_an_unstorable_message_id_with_no_sender_is_refused_rather_than_crashing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-10, P1-1**, asserted where it actually bit.

    The conversation id is derived, and when no sender is usable the derivation
    falls back on the *message* id -- into `uuid5`, which encodes its name as
    UTF-8. A surrogate there raised `UnicodeEncodeError` **out of `accept()`**:
    not a refusal with a reason code but a framework 500, a vendor redelivery,
    and ten of those delete the callback address (ADR-0103 §二.1).

    The contract-level case pins the values; this one pins the **order** -- the
    id is checked before anything derives from it -- which is the half a unit
    test cannot see and the half that was wrong.
    """

    item = callback_item(external_event_id="wd-" + chr(0xD800) + "-bad", text="hello")
    del item["senderId"]
    good = callback_item(external_event_id=f"good-{uuid4()}", text="and this one arrives")

    # **It must not raise**, and the healthy element of the same request must
    # still land: one unreadable element may not cost the others.
    result = _ingress(settings).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback_body(item, good),
    )
    outcomes = [outcome.outcome for outcome in result.items]
    assert outcomes == [OUTCOME_REFUSED, "customer_message"], outcomes
    assert result.accepted is False
    assert _inbox_rows(result.items[1].event_id) != []


def test_two_elements_sharing_a_request_id_are_two_messages(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**Independent review 2026-09-10, P1-2.**

    `requestId` is not per message -- the vendor's own document has three pairs
    of different messages sharing one. While it was the replay key, the second
    such message hit "this key is recorded against another event id", which
    raises `IngressAuthenticationError`; the per-element loop did not catch it,
    so the **whole request** aborted with `CHANNEL_AUTHENTICATION_FAILED` after
    the first element had already been written -- and the vendor does not
    redeliver a `200`.

    Both elements have to land, and this is the case that says so.
    """

    shared = f"delivery-{uuid4()}"
    first_id, second_id = f"msg-A-{uuid4()}", f"msg-B-{uuid4()}"
    result = _ingress(settings).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback_body(
            callback_item(external_event_id=first_id, request_id=shared, text="first"),
            callback_item(external_event_id=second_id, request_id=shared, text="second"),
        ),
    )

    assert result.accepted is True, "a shared requestId is ordinary vendor traffic"
    assert [outcome.outcome for outcome in result.items] == [
        "customer_message",
        "customer_message",
    ]
    with _migration_connection() as connection:
        stored = connection.execute(
            "SELECT external_event_id FROM inbox_events WHERE external_event_id = ANY(%s)",
            ([first_id, second_id],),
        ).fetchall()
    assert sorted(row["external_event_id"] for row in stored) == sorted([first_id, second_id])


def test_a_withdrawal_is_never_a_new_customer_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The criterion ADR-0107 says does not relax when candidate (a) is taken.**

    Taking (a) decided how much a withdrawal *changes* -- nothing. It did not
    soften the other half: a 2063 must not arrive as customer input. That is
    the failure ADR-0107 §一 calls the worst default, because a withdrawal read
    as a new message does not look like a bug from anywhere: the conversation
    simply gains a turn the customer never sent.

    Asserted as an absence -- **no inbox row, no queue pointer** -- because the
    defect it guards has no positive symptom.
    """

    before = len(_audit_rows("channel.message_withdrawn"))

    result = _accept(
        settings,
        seeded_database,
        {"msgType": 2063, "msgData": {"revokeMsgUniqueIdentifier": "abc-123"}},
    )

    assert result.accepted, "the callback is answered, not refused"
    assert result.inbox_id is None, "a withdrawal must not become an inbox event"
    assert result.pointer_published is False, "no worker may be woken for it"
    assert _inbox_rows(result.event_id) == []

    after = _audit_rows("channel.message_withdrawn")
    assert len(after) == before + 1, "the withdrawal must leave an audit row"


def test_the_withdrawal_audit_records_identifiers_and_not_the_withdrawn_words(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0107 §二.2 wants the audit to say *which* message was taken back.

    And it must say it **without keeping the message**. The withdrawn text is
    precisely what the customer asked us to stop having; an audit row that
    copied the payload in to be thorough would mean withdrawing something
    created a durable copy of it.
    """

    result = _accept(
        settings,
        seeded_database,
        {
            "msgType": 2063,
            "msgData": {"revokeMsgUniqueIdentifier": "target-9", "content": "请忽略这句话"},
        },
    )

    row = [r for r in _audit_rows("channel.message_withdrawn") if r["id"] == result.event_id]
    assert len(row) == 1
    evidence = row[0]["evidence"]

    assert evidence["withdrawn_reference"] == "target-9"
    assert row[0]["reason_code"] == "CUSTOMER_WITHDREW_MESSAGE"
    # The words the customer took back are nowhere in the row.
    assert "请忽略这句话" not in json.dumps(evidence, ensure_ascii=False)


def test_a_withdrawal_that_names_nothing_is_still_recorded(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**No real 2063 has ever arrived**, so the reference field name is a guess.

    A row saying "a withdrawal came and did not say what for" is worth strictly
    more than no row -- and refusing it would be the `INVALID_CALLBACK_PAYLOAD`
    misreading ADR-0107 §一 names.
    """

    result = _accept(settings, seeded_database, {"msgType": 2063, "msgData": {}})

    assert result.accepted
    assert result.inbox_id is None
    row = [r for r in _audit_rows("channel.message_withdrawn") if r["id"] == result.event_id]
    assert len(row) == 1
    assert row[0]["evidence"]["withdrawn_reference"] is None


def _streak(config_id: str = "config-a") -> int:
    """The consecutive-failure count exactly as `INGRESS_SQL` computes it."""

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT count(*) AS streak FROM audit_events a
             WHERE a.action = 'ingress.non_2xx'
               AND a.created_at > COALESCE((
                     SELECT max(b.created_at) FROM audit_events b
                      WHERE b.action IN ('ingress.answered', 'ingress.refused')
                        AND b.evidence->>'config_id' = %s), '-infinity'::timestamptz)
               AND a.evidence->>'config_id' = %s
            """,
            (config_id, config_id),
        ).fetchone()
    return int(row["streak"])


def test_a_withdrawal_answered_200_clears_the_failure_streak(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**P2-1.** The gauge this feeds guards an irreversible deletion.

    A provider deletes the callback address after ten consecutive failures
    (ADR-0103 §二.1), and the streak is supposed to reset on any `200`. The
    withdrawal path wrote none of the three rows `INGRESS_SQL` reads as "we
    answered", so a healthy answer left the streak standing and the gauge
    reported a channel about to be deleted while it was in fact being answered
    normally -- **the same failure the duplicate branch already carried a
    comment about**.
    """

    with _migration_connection() as connection:
        for _ in range(3):
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (%s, %s, NULL, 'ingress.non_2xx', 'channel_binding', NULL,
                          %s, 'WINDOW_NOT_OPEN', %s)
                """,
                (
                    seeded_database["tenant_a"],
                    uuid4(),
                    uuid4(),
                    Jsonb({"config_id": "config-a", "http_status": 503}),
                ),
            )
        connection.commit()
    assert _streak() == 3, "the fixture did not establish a streak"

    result = _accept(
        settings,
        seeded_database,
        {"msgType": 2063, "msgData": {"revokeMsgUniqueIdentifier": "t-1"}},
    )
    assert result.accepted
    assert _streak() == 0, "a withdrawal answered 200 must clear the streak"


def test_a_redelivered_withdrawal_is_recorded_once(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**P2-2.** Redelivery is expected, not exceptional.

    ADR-0103 §二.1 says the provider redelivers on transport failure, and the
    ordinary path answers that with `ON CONFLICT DO NOTHING` + `duplicate=True`.
    The withdrawal path returned before the replay guard, so three deliveries of
    one withdrawal became three audit rows -- and the audit row is this batch's
    only deliverable, so the miscount lands on the deliverable itself.
    """

    body = callback_body(
        callback_item(
            external_event_id=f"wd-{uuid4()}",
            sender_id="customer-1",
            request_id=f"n-{uuid4()}",
            msg_type=2063,
            msg_data={"revokeMsgUniqueIdentifier": "t-2"},
        )
    )
    before = len(_audit_rows("channel.message_withdrawn"))

    first = _ingress(settings).accept(
        config_id="config-a", path_token=str(seeded_database["channel_token"]), payload=body
    )
    second = _ingress(settings).accept(
        config_id="config-a", path_token=str(seeded_database["channel_token"]), payload=body
    )

    assert first.accepted and second.accepted
    assert first.duplicate is False
    assert second.duplicate is True, "a redelivery must say so"
    assert len(_audit_rows("channel.message_withdrawn")) == before + 1, "recorded once, not twice"


def test_a_withdrawal_with_an_unstorable_event_id_is_refused_not_dropped(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**P3-1.** It used to escape as `UntranslatableCharacter` and leave no row.

    Answered `200` by `app.py`'s mapping either way -- but silently, with
    nothing written, which contradicts this batch's own argument that a
    withdrawal that names nothing still beats no record. Refused explicitly is
    what the ordinary path does with an id it cannot store.
    """

    result = _accept(
        settings,
        seeded_database,
        {"external_event_id": "wd-\x00-bad", "msgType": 2063, "msgData": {}},
    )
    assert result.accepted is False
    assert [item.outcome for item in result.items] == [OUTCOME_REFUSED]


def test_a_2063_carrying_text_still_does_not_become_a_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**P3-2**, asserted where the guarantee actually lives.

    The parser-level case cannot carry this: `parse_content` on a 2063 with a
    `content` field returns text quite happily, so "a withdrawal never arrives
    as customer input" is **not** a property of the parser -- it is a property
    of the order of the branches in `accept()`. Move the withdrawal branch below
    the content rule and this case goes red; the parser cases would not.
    """

    result = _accept(
        settings,
        seeded_database,
        {
            "msgType": 2063,
            "msgData": {"revokeMsgUniqueIdentifier": "t-3", "content": "请忽略这句话"},
        },
    )

    assert result.accepted
    assert result.inbox_id is None
    assert _inbox_rows(result.event_id) == []
