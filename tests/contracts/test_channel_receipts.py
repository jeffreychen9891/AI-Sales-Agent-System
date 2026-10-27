"""Every channel outcome must come back as a receipt, never as an exception.

The dispatcher loop has no useful answer to an exception from the adapter: it can
only drop the worker process. Until 2026-09-02 `raise_for_status()` made a plain
HTTP 500 from the channel do exactly that, so a provider having a bad hour became
a crash-restart loop instead of a recorded delivery state.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastagent_v2.worker.channel import (
    TERMINAL_REFUSAL_STATUSES,
    HttpChannelAdapter,
    SendCommand,
)

COMMAND = SendCommand(
    tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    conversation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    content={"type": "text", "text": "the customer visible reply"},
)


def _adapter(handler: object) -> HttpChannelAdapter:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HttpChannelAdapter("http://channel.invalid/send", transport=transport)


def _deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=5)


def _responder(status_code: int, body: object = None, *, text: str | None = None):
    def handler(_: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=body)

    return handler


def _send(handler: object):
    return _adapter(handler).send(
        command=COMMAND, idempotency_key="idem-1", deadline_at=_deadline()
    )


def test_a_channel_server_error_is_an_unconfirmed_receipt_not_an_exception() -> None:
    receipt = _send(_responder(500, {"error": "upstream exploded"}))
    assert receipt.status == "unknown"
    assert receipt.provider_operation_id is None
    assert receipt.evidence["http_status"] == 500
    assert receipt.evidence["reason"] == "provider_server_error"


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_no_server_status_can_escape_the_adapter(status_code: int) -> None:
    assert _send(_responder(status_code, {})).status == "unknown"


@pytest.mark.parametrize("status_code", sorted(TERMINAL_REFUSAL_STATUSES))
def test_an_enumerated_refusal_is_terminal(status_code: int) -> None:
    """These codes describe a request the provider examined and rejected.

    Malformed, unknown route, too large, wrong media type, unprocessable: an
    identical retry is refused identically, so the send is terminally failed.
    """

    receipt = _send(_responder(status_code, {"detail": "refused"}))
    assert receipt.status == "failed"
    assert receipt.evidence["http_status"] == status_code
    assert receipt.evidence["reason"] == "provider_rejected_request"


@pytest.mark.parametrize("status_code", [401, 402, 403, 404, 405, 408, 409, 423, 424, 429, 451])
def test_a_client_error_outside_the_enumeration_stays_unconfirmed(status_code: int) -> None:
    """The dangerous half of 4xx.

    An expired credential, a rate limit, a lock or a legal block can all arrive
    *after* the provider accepted the send. The first version of this adapter
    resolved every non-5xx failure to `failed` and would have thrown those
    messages away with no trace. Under-claiming costs a reconcile; over-claiming
    loses a delivered message.
    """

    receipt = _send(_responder(status_code, {"detail": "not a refusal"}))
    assert receipt.status == "unknown"
    assert receipt.evidence["http_status"] == status_code
    assert receipt.evidence["reason"] == "provider_response_inconclusive"


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_a_redirect_is_unconfirmed_with_its_own_reason(status_code: int) -> None:
    """The client does not follow redirects, so nothing reached the endpoint --
    but that is a configuration fault, not a rejected message, and the receipt
    should say which."""

    receipt = _send(_responder(status_code, {}))
    assert receipt.status == "unknown"
    assert receipt.evidence["reason"] == "provider_redirected"


def test_the_terminal_enumeration_is_exactly_these_four_codes() -> None:
    """Pin the literal, not the constant.

    Comparing the sweep against `TERMINAL_REFUSAL_STATUSES` made the test agree
    with whatever the enumeration happened to say. Every change to the set of
    codes that can permanently discard a customer message should have to edit a
    test -- that friction is the point.
    """

    assert TERMINAL_REFUSAL_STATUSES == frozenset({400, 413, 415, 422})


def test_only_the_enumerated_codes_are_ever_terminal() -> None:
    """Sweep every status the adapter can meet; `failed` must be the exception."""

    terminal = {
        code
        for code in list(range(300, 452))
        if _send(_responder(code, {"detail": "x"})).status == "failed"
    }
    assert terminal == {400, 413, 415, 422}


def test_a_missing_route_is_not_a_refused_message() -> None:
    """404 says the request was never examined -- the endpoint is not there.

    That is a configuration fault, like the 3xx case, not a message the provider
    looked at and rejected. With a mistyped send URL, treating it as terminal
    would mark every message permanently FAILED: exactly the silent loss this
    enumeration exists to prevent. Both `send` and `reconcile` must agree.
    """

    receipt = _send(_responder(404, {"detail": "no such route"}))
    assert receipt.status == "unknown"
    assert receipt.evidence["http_status"] == 404
    assert receipt.evidence["reason"] == "provider_response_inconclusive"

    reconciled = _adapter(_responder(404, {"detail": "no such operation"})).reconcile(
        provider_operation_id="op-1", deadline_at=_deadline()
    )
    assert reconciled.status == "unknown"
    assert reconciled.evidence["http_status"] == 404
    assert reconciled.evidence["reconciled"] is True


def test_an_accepted_send_that_cannot_be_read_back_is_unconfirmed() -> None:
    receipt = _send(_responder(200, text="<html>login</html>"))
    assert receipt.status == "unknown"
    assert receipt.evidence["reason"] == "unreadable_provider_receipt"


def test_a_receipt_missing_its_operation_id_is_unconfirmed() -> None:
    receipt = _send(_responder(200, {"status": "delivered"}))
    assert receipt.status == "unknown"
    assert receipt.evidence["reason"] == "unreadable_provider_receipt"


def test_a_status_the_outbox_cannot_record_becomes_unknown() -> None:
    """`dispatch_one` indexes a fixed map with this value; an unlisted one crashed it."""

    receipt = _send(_responder(200, {"status": "queued_for_tuesday", "provider_operation_id": "1"}))
    assert receipt.status == "unknown"
    assert receipt.provider_operation_id == "1"
    assert receipt.evidence["reason"] == "unknown_provider_status"


def test_a_delivered_send_still_reports_normally() -> None:
    receipt = _send(_responder(200, {"status": "delivered", "provider_operation_id": "op-1"}))
    assert receipt.status == "delivered"
    assert receipt.provider_operation_id == "op-1"
    assert receipt.evidence == {"adapter": "http", "http_status": 200}


def test_failure_evidence_never_carries_the_response_body() -> None:
    """Receipt evidence is persisted and widely readable; bodies carry customer text.

    The query paths are in the loop for the same reason the send paths are: a
    lookup response carries the message it is answering about (the sink's own
    receipt echoes `command`), and the guard used to watch only `send` while the
    reconcile side grew six new return paths under it (narrow re-review
    2026-09-04, P3).
    """

    # Not named `secret_*`: a credential-shaped assignment is what the secret
    # scan looks for, and this file is not exempt from it.
    leaky_body = "customer phone 13800000000 and Authorization: Bearer abc"
    for receipt in (
        _send(_responder(500, {"detail": leaky_body})),
        _send(_responder(422, {"detail": leaky_body})),
        _send(_responder(429, {"detail": leaky_body})),
        _send(_responder(200, text=leaky_body)),
        _adapter(_responder(503, {"detail": leaky_body})).reconcile_by_key(
            idempotency_key="k", deadline_at=_deadline()
        ),
        _adapter(_responder(200, text=leaky_body)).reconcile_by_key(
            idempotency_key="k", deadline_at=_deadline()
        ),
        _adapter(_responder(200, {"status": leaky_body, "provider_operation_id": "op"}))
        .reconcile_by_key(idempotency_key="k", deadline_at=_deadline()),
        _adapter(_responder(422, {"detail": leaky_body})).reconcile(
            provider_operation_id="op", deadline_at=_deadline()
        ),
        _adapter(_responder(200, text=leaky_body)).reconcile(
            provider_operation_id="op", deadline_at=_deadline()
        ),
        _adapter(_responder(200, {"status": leaky_body})).reconcile(
            provider_operation_id="op", deadline_at=_deadline()
        ),
    ):
        serialized = json.dumps(receipt.evidence)
        assert "13800000000" not in serialized
        assert "Bearer" not in serialized


def test_a_network_failure_stays_unconfirmed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to channel", request=request)

    assert _send(handler).status == "unknown"


def test_reconcile_survives_the_same_failures() -> None:
    adapter_500 = _adapter(_responder(500, {}))
    assert (
        adapter_500.reconcile(provider_operation_id="op-1", deadline_at=_deadline()).status
        == "unknown"
    )
    adapter_garbage = _adapter(_responder(200, text="not json"))
    assert (
        adapter_garbage.reconcile(provider_operation_id="op-1", deadline_at=_deadline()).status
        == "unknown"
    )
    adapter_ok = _adapter(_responder(200, {"status": "delivered"}))
    reconciled = adapter_ok.reconcile(provider_operation_id="op-1", deadline_at=_deadline())
    assert reconciled.status == "delivered"
    assert reconciled.evidence["reconciled"] is True


# --------------------------------------------------------------------------
# The query side. A reconcile asks *about* a message; how the question is
# received says nothing about what happened to the message.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", sorted(TERMINAL_REFUSAL_STATUSES))
def test_a_refused_query_never_becomes_a_terminal_answer(status_code: int) -> None:
    """P1-2. These codes are terminal for a send and meaningless for a lookup.

    `TERMINAL_REFUSAL_STATUSES` means "the provider examined this **send** and
    rejected it on its own terms". Both reconcile paths used to run their
    responses through the same classifier, so a 422 on the `GET` -- a malformed
    *question* -- ended the intent in `FAILED`. The independent review walked it:
    `POST 500` then `GET 422` wrote off a message that may well have been
    delivered.

    Parametrised over the set itself rather than over a literal, so adding a
    code to the send's terminal list cannot quietly skip this guard.
    """

    adapter = _adapter(_responder(status_code, {"detail": "no"}))

    by_key = adapter.reconcile_by_key(idempotency_key="send_message:1", deadline_at=_deadline())
    by_id = adapter.reconcile(provider_operation_id="op-1", deadline_at=_deadline())

    assert by_key.status == "unknown"
    assert by_id.status == "unknown"
    assert by_key.evidence["reason"] == "provider_rejected_query"
    assert by_key.evidence["http_status"] == status_code


def test_a_query_may_narrow_uncertainty_but_never_create_it() -> None:
    """The same rule stated from the other end: only a readable 2xx decides.

    A `failed` in the **body** of a 200 is the provider answering the question
    and is honoured -- that is the provider talking about the message. The
    refusal codes above are the transport talking about the query.
    """

    delivered = _adapter(_responder(200, {"provider_operation_id": "op-9", "status": "failed"}))

    resolved = delivered.reconcile(provider_operation_id="op-9", deadline_at=_deadline())

    assert resolved.status == "failed", "a provider that answers is still allowed to say failed"


def test_the_idempotency_key_cannot_steer_the_query_to_another_endpoint() -> None:
    """P3-1. Written for the key shape nobody has chosen yet.

    Today's keys are `send_message:{uuid}` and hold no slash, so this is not
    reachable now. But httpx normalises `..` segments, so a key shape that ever
    admits one would send the lookup to a different path -- and the answer would
    be about something else entirely, while the receipt says the provider was
    asked about this message.
    """

    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={"detail": "no such key"})

    adapter = _adapter(record)

    adapter.reconcile_by_key(idempotency_key="intent/../healthz", deadline_at=_deadline())

    assert seen == ["http://channel.invalid/send/by-key/intent%2F..%2Fhealthz"]
    assert "/send/healthz" not in seen[0]


def test_every_reconcile_return_path_says_whether_the_query_went_out() -> None:
    """The flag the dispatcher trusts, pinned on every exit of both methods.

    `_reconcile_before_giving_up` reads `query_sent` to decide whether it may
    record that the provider was asked (P0-1), and its default is `False`. An
    adapter path that forgets the flag therefore under-claims rather than lies
    -- but it still loses a true fact, so every path is checked here.

    "Every" used to be a claim rather than a check: the list below held ten of
    the twelve return statements, and deleting the flag from `reconcile`'s
    unreadable-body path left the whole contract suite green (narrow re-review
    2026-09-04). The two missing shapes are in the list now, and the count guard
    at the end is what keeps the sentence true -- adding a return path to either
    method fails this test until the path is covered here.
    """

    spent = datetime.now(UTC) - timedelta(seconds=1)
    live = _deadline()

    def unreachable(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    receipts = [
        _adapter(_responder(200, {"provider_operation_id": "op", "status": "delivered"}))
        .reconcile_by_key(idempotency_key="k", deadline_at=live),
        _adapter(_responder(200, {"provider_operation_id": "op", "status": "sideways"}))
        .reconcile_by_key(idempotency_key="k", deadline_at=live),
        _adapter(_responder(200, text="not json")).reconcile_by_key(
            idempotency_key="k", deadline_at=live
        ),
        _adapter(_responder(503, {"detail": "later"})).reconcile_by_key(
            idempotency_key="k", deadline_at=live
        ),
        _adapter(unreachable).reconcile_by_key(idempotency_key="k", deadline_at=live),
        _adapter(_responder(200, {"status": "delivered"})).reconcile(
            provider_operation_id="op", deadline_at=live
        ),
        _adapter(_responder(200, {"status": "sideways"})).reconcile(
            provider_operation_id="op", deadline_at=live
        ),
        _adapter(_responder(200, text="not json")).reconcile(
            provider_operation_id="op", deadline_at=live
        ),
        _adapter(_responder(422, {"detail": "no"})).reconcile(
            provider_operation_id="op", deadline_at=live
        ),
        _adapter(unreachable).reconcile(provider_operation_id="op", deadline_at=live),
    ]

    assert all(r.evidence["query_sent"] is True for r in receipts), [
        r.evidence for r in receipts if r.evidence.get("query_sent") is not True
    ]

    # ...and the two paths that genuinely send nothing say so.
    skipped = [
        _adapter(_responder(200, {})).reconcile_by_key(idempotency_key="k", deadline_at=spent),
        _adapter(_responder(200, {})).reconcile(provider_operation_id="op", deadline_at=spent),
    ]
    assert all(r.evidence["query_sent"] is False for r in skipped)
    assert all(r.evidence["reason"] == "deadline_exceeded_before_reconcile" for r in skipped)

    # The guard that keeps "every" honest: five live shapes plus one skipped
    # shape per method, counted against the `return` statements the method
    # actually has. A new return path fails here until it is added above, which
    # is the only way this test can notice a path nobody thought to list.
    for method, covered in (("reconcile_by_key", 6), ("reconcile", 6)):
        source = textwrap.dedent(inspect.getsource(getattr(HttpChannelAdapter, method)))
        returns = sum(isinstance(node, ast.Return) for node in ast.walk(ast.parse(source)))
        assert returns == covered, (
            f"{method} has {returns} return paths and this test covers {covered}; "
            "add the new shape to the lists above and update the count"
        )


def test_a_lookup_404_is_recorded_as_an_answer_not_as_a_refusal() -> None:
    """404 is the answer this batch went to collect; 422 is us asking badly.

    Both stay `unknown` -- a query may only narrow uncertainty, and neither of
    these narrows anything. But the first classifier gave them the same reason
    string, `provider_rejected_query`, and L2b puts that string in front of the
    person who picks up the manual task (narrow re-review 2026-09-04). "The
    provider refused our query" and "the provider has never heard of this
    message" are different facts, and only one of them is about the message.
    """

    no_record = _adapter(_responder(404, {"detail": "no such key"})).reconcile_by_key(
        idempotency_key="k", deadline_at=_deadline()
    )
    malformed = _adapter(_responder(422, {"detail": "bad key"})).reconcile_by_key(
        idempotency_key="k", deadline_at=_deadline()
    )

    assert no_record.status == "unknown" and malformed.status == "unknown"
    assert no_record.evidence["reason"] == "provider_has_no_record"
    assert no_record.evidence["http_status"] == 404
    assert malformed.evidence["reason"] == "provider_rejected_query"
    assert no_record.evidence["reason"] != malformed.evidence["reason"], (
        "the answer and the refusal must not read alike in the record"
    )
    # And the id lookup classifies the same way -- it is the same classifier,
    # and a person reading either receipt sees the same vocabulary.
    by_id = _adapter(_responder(404, {"detail": "gone"})).reconcile(
        provider_operation_id="op", deadline_at=_deadline()
    )
    assert by_id.evidence["reason"] == "provider_has_no_record"


def test_a_query_spends_its_budget_once_not_once_per_phase() -> None:
    """The two seconds have to be two seconds for the lease clamp to hold.

    httpx reads a bare float as the limit for each of connect, read, write and
    pool, so `timeout=2.0` bounds a round trip at four times what the caller
    thinks. The dispatcher now clamps the query to what is left of the outbox
    lease and that arithmetic assumes the number means what it says; an overrun
    is another dispatcher claiming the row and sending the message twice.
    """

    seen: list[dict[str, float | None]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json={"provider_operation_id": "op", "status": "delivered"})

    adapter = _adapter(record)
    adapter.reconcile_by_key(
        idempotency_key="k", deadline_at=datetime.now(UTC) + timedelta(seconds=2)
    )
    adapter.reconcile(
        provider_operation_id="op", deadline_at=datetime.now(UTC) + timedelta(seconds=2)
    )

    assert len(seen) == 2
    for timeout in seen:
        phases = [timeout[name] for name in ("connect", "read", "write", "pool")]
        assert all(phase is not None for phase in phases), phases
        assert sum(phase for phase in phases if phase is not None) <= 2.05, (
            f"the phases together outlive the budget: {phases}"
        )
