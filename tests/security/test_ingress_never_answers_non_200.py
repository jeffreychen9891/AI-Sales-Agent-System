"""Every refusal the ingress *means* is a `200` (T204 / ADR-0103 §二.1).

**The reason is irreversible.** WeComProxy deletes a callback address after ten
consecutive failures -- deletes, and it does not come back. A status code on
this endpoint is therefore not a description of an outcome, it is a vote on
whether the provider keeps talking to us, and a refusal we meant must not spend
one of the ten.

So the outcome moved into the body, and non-200 is kept for the two cases where
redelivery is what we want: **we are broken**, and **we are not ready yet**.

**The enumeration is over what `accept()` can raise, not over what `app.py`
says** (independent review 2026-09-07, P1-2). The first version of this file
grepped the source for `HTTPException` and for numeric status codes -- and the
three `500`s the review then measured were composed by *the framework*, from
exceptions this module never named, so the grep could not see them and reported
success. Reading the source is kept below as a supplement; the judgement is the
parametrised case that drives the real app.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastagent_v2.ingress.adapter import (
    IngressAuthenticationError,
    IngressPayloadError,
    IngressWindowNotOpenError,
    WeComProxyIngress,
)
from fastagent_v2.ingress.app import create_app as create_ingress
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from jsonschema import ValidationError
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import callback_body, callback_item

pytestmark = [pytest.mark.integration]

APP_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "services/ingress/src/fastagent_v2/ingress/app.py"
)


def _payload(**overrides: Any) -> dict[str, Any]:
    """One callback in the vendor's shape. `overrides` land on the element.

    The keys a case varies are the vendor's own now: `timestamp` rather than
    `occurred_at`, `requestId` rather than `nonce`. What each case is *about* is
    unchanged -- an unreadable time, an unstorable delivery id -- which is why
    the assertions below did not move.
    """

    item = callback_item(
        external_event_id=overrides.pop("external_event_id", f"t204-{uuid4()}"),
        sender_id="customer-1",
        text=overrides.pop("text", "hello"),
        occurred_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    item.update(overrides)
    return callback_body(item)


def _event_id(payload: dict[str, Any]) -> str:
    """The message id of a single-element callback, for the `_stored` lookups."""

    return str(payload["data"][0]["msgUniqueIdentifier"])


def _post(settings: Settings, token: str, payload: Any, *, config_id: str = "config-a") -> Any:
    client = TestClient(create_ingress(settings))
    if isinstance(payload, (dict, list)):
        return client.post(f"/wecom_proxy/{config_id}/{token}", json=payload)
    return client.post(
        f"/wecom_proxy/{config_id}/{token}",
        content=payload,
        headers={"Content-Type": "application/json"},
    )


def _stored(external_event_id: str) -> int:
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        return connection.execute(
            "SELECT count(*) AS n FROM inbox_events WHERE external_event_id = %s",
            (external_event_id,),
        ).fetchone()["n"]


#: Every class `accept()` can raise, and the answer each one must produce.
#: **This list is the judgement** ADR-0103 §二.1 asks for ("遍历 ingress 所有
#: 能走到的响应分支"): a new failure mode that is not here fails the last case
#: below rather than quietly becoming a 500.
_BRANCHES: tuple[tuple[Exception, int, str | None], ...] = (
    (IngressAuthenticationError("wrong token"), 200, "CHANNEL_AUTHENTICATION_FAILED"),
    (IngressPayloadError("bad payload"), 200, "INVALID_CALLBACK_PAYLOAD"),
    (ValidationError("too long for the envelope"), 200, "INVALID_CALLBACK_PAYLOAD"),
    (psycopg.DataError("unstorable value"), 200, "INVALID_CALLBACK_PAYLOAD"),
    # **Ours, and the one deliberate non-200** (user ruling 2026-09-07).
    (IngressWindowNotOpenError("window not open"), 503, None),
    # Anything we did not anticipate is us being broken, and redelivery is what
    # we want. This is the branch that must *not* be swallowed into a 200.
    (RuntimeError("something we did not foresee"), 500, None),
)


@pytest.mark.parametrize(
    ("raised", "expected_status", "expected_code"),
    _BRANCHES,
    ids=[type(row[0]).__name__ for row in _BRANCHES],
)
def test_every_branch_accept_can_take_answers_the_way_the_adr_requires(
    settings: Settings,
    seeded_database: dict[str, Any],
    channel_sink: str,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    expected_status: int,
    expected_code: str | None,
) -> None:
    assert channel_sink

    def explode(*_: object, **__: object) -> None:
        raise raised

    monkeypatch.setattr(WeComProxyIngress, "accept", explode)
    client = TestClient(create_ingress(settings), raise_server_exceptions=False)

    response = client.post(f"/wecom_proxy/config-a/{uuid4()}", json=_payload())

    assert response.status_code == expected_status, response.text
    if expected_code is not None:
        body = response.json()
        assert body["accepted"] is False
        assert body["error"]["code"] == expected_code


def test_a_token_that_does_not_match_is_refused_with_two_hundred(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    assert channel_sink
    response = _post(settings, "0" * 64, _payload())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is False
    assert body["error"]["code"] == "CHANNEL_AUTHENTICATION_FAILED"


def test_a_payload_that_is_not_usable_is_refused_with_two_hundred(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    assert channel_sink
    token = str(seeded_database["channel_token"])

    for payload in (
        _payload(text=""),
        _payload(timestamp="not-a-timestamp"),
        {"code": 0, "msg": "ok"},
        ["not", "an", "object"],
        "{not json at all",
    ):
        response = _post(settings, token, payload)
        assert response.status_code == 200, response.text
        assert response.json()["error"]["code"] == "INVALID_CALLBACK_PAYLOAD"


def test_a_payload_the_envelope_would_reject_is_refused_at_the_boundary(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    """**The three the review measured as 500s** (P1-1), end to end.

    `message-envelope` bounds `content.text` at 20000 and `external_event_id` at
    255, and `validate_contract` enforces them -- but it runs *after* the
    envelope is built and raises `jsonschema.ValidationError`, which is not a
    `ValueError`. A customer pasting a long message therefore produced a
    framework `500`, and ten of those delete the callback address.

    The `nonce` case is the same shape one layer down: it reached `jsonb` and
    psycopg refused the `\\x00`. **It is the only one of the three that
    degrades** -- the replay key is a hash of the nonce and is computed anyway,
    so what is dropped is a copy kept for evidence, and the message still
    arrives.
    """

    assert channel_sink
    token = str(seeded_database["channel_token"])

    long_text = _payload(text="x" * 20001)
    response = _post(settings, token, long_text)
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == "INVALID_CALLBACK_PAYLOAD"
    assert _stored(_event_id(long_text)) == 0

    long_id = _payload(external_event_id="e" * 256)
    response = _post(settings, token, long_id)
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == "INVALID_CALLBACK_PAYLOAD"

    # The boundary itself still gets in: 20000 and 255 are allowed.
    at_the_limit = _payload(text="y" * 20000, external_event_id="e" * 255)
    response = _post(settings, token, at_the_limit)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True

    unstorable_nonce = _payload(requestId="nonce\x00value")
    response = _post(settings, token, unstorable_nonce)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True, "the nonce degrades; the message arrives"
    assert _stored(_event_id(unstorable_nonce)) == 1


def test_a_value_the_database_cannot_store_is_refused_not_crashed(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    """**T207**, and it is closed here rather than in T180's batch on purpose.

    `actor_id` degrades to "no identity" and the message still arrives, because
    an identity is something we can do without. **`text` and `external_event_id`
    are not**: a message with no text is not a message, and an event with no id
    has no idempotency key. So they are refused -- and T207 said the refusal
    would have to be re-read under T204 before it was written, which is exactly
    what this batch is.
    """

    assert channel_sink
    token = str(seeded_database["channel_token"])

    unstorable_text = _payload(text="hello\x00world")
    response = _post(settings, token, unstorable_text)
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == "INVALID_CALLBACK_PAYLOAD"
    assert _stored(_event_id(unstorable_text)) == 0

    unstorable_id = _payload(external_event_id=f"evt-\x00-{uuid4()}")
    response = _post(settings, token, unstorable_id)
    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == "INVALID_CALLBACK_PAYLOAD"

    # And the neighbouring value still gets in: this refuses the unstorable, not
    # everything that looks unusual.
    accepted = _payload(text="hello中文\U0001f600")
    response = _post(settings, token, accepted)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True
    assert _stored(_event_id(accepted)) == 1


def test_the_accepted_answer_is_also_two_hundred(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    """It used to be `202`, and `202` is not `200` either.

    The vendor's rule counts "failures" without saying what it counts as one. If
    `202` counted, ten ordinary messages would have deleted the address -- and
    nobody has measured which reading is right. Answering `200` everywhere makes
    the question one we do not have to answer.
    """

    assert channel_sink
    payload = _payload()
    response = _post(settings, str(seeded_database["channel_token"]), payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["duplicate"] is False
    assert _stored(_event_id(payload)) == 1


def test_the_source_composes_one_non_two_hundred_and_names_it() -> None:
    """A supplement, **not the judgement** -- see the module docstring.

    It still earns its place: it is the only thing that would notice a *new*
    hand-written 4xx appearing in this file, and the review showed the first
    version could not even see one written as `status.HTTP_401_UNAUTHORIZED`
    (P2-2). So it reads both spellings, and it allows exactly one non-200: the
    503 the user ruled on.
    """

    source = APP_SOURCE.read_text(encoding="utf-8")

    numeric = {code for code in re.findall(r"status_code=(\d{3})", source)}
    named = set(re.findall(r"status\.HTTP_(\d{3})_[A-Z_]+", source))
    composed = numeric | named

    assert composed <= {"200", "503"}, (
        f"the ingress app composes {sorted(composed - {'200', '503'})}. A refusal "
        "we mean must be 200 + a business error body (ADR-0103 §二.1); the only "
        "non-200 this endpoint may compose is the 503 for a window that is not "
        "open yet, which the user ruled on 2026-09-07."
    )
    assert "503" in composed, "the window refusal must still be non-200"


def test_a_refusal_with_no_resolved_binding_records_nothing() -> None:
    """**The `config_id` in the URL is not a label until a binding answers to it.**

    Anyone who can reach this port can put any string in the path. If the
    ingress recorded outcomes keyed on that string, one caller with a loop could
    manufacture unbounded label cardinality in the metrics backend -- the
    failure ADR-0101 §三.1 forbids, reached from outside rather than by our own
    carelessness. So the recorder is keyed on the **resolved** binding, and the
    branch that never resolved one carries no tenant and records nothing.

    Read from the source rather than driven through a request, like the
    supplement below it: the claim is about every branch, and a request
    exercises one.
    """

    adapter = (APP_SOURCE.parent / "adapter.py").read_text(encoding="utf-8")
    app = APP_SOURCE.read_text(encoding="utf-8")

    # The unknown-config refusal is raised bare -- no tenant, no config label.
    unknown = adapter.split("if binding is None:")[1].split("binding_tenant_id = ")[0]
    assert "no channel binding for this config id" in unknown
    assert "binding_tenant_id=" not in unknown, "an unresolved config id must not become a label"

    # And the recorder refuses to write without one rather than inventing it.
    guard = app.split("if exc.binding_tenant_id is None or exc.binding_config_id is None:")
    assert len(guard) == 2, "the recorder lost its guard"
    assert guard[1].lstrip().startswith("return")


def test_recording_an_answer_cannot_turn_a_refusal_into_a_five_hundred() -> None:
    """Bookkeeping may not spend the caller's `errorTimes` budget.

    Every call site has already decided what to answer. If the recorder raised
    -- database gone, say -- the exception would leave the route as a `500`:
    a non-200 **caused by our own metric**, against a deletion threshold of ten.
    ADR-0103 §二.1 reserves non-200 for our genuine faults, and a failed insert
    into an audit table is not one the caller should pay for.
    """

    adapter = (APP_SOURCE.parent / "adapter.py").read_text(encoding="utf-8")
    body = adapter.split("def record_answer(")[1].split("def accept(")[0]
    assert "except Exception:" in body, "the recorder must not propagate"
    assert "LOGGER.warning" in body, "and it must not disappear silently either"
