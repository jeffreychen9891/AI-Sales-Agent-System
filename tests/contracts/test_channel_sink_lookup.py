"""The simulator's by-key lookup, and the route ordering it depends on.

The sink is test infrastructure, and it had no tests -- which was fine while it
only had to accept a POST. L1 gives it a second job: answering "what happened to
the send with this key", which is the query a timed-out dispatch now makes
before it may write down `unknown`. A sink that answers that question wrongly
would make the reconcile look like it ran and found nothing, which is the exact
false negative the batch exists to remove.
"""

from __future__ import annotations

import pytest
from fastagent_v2.worker import channel_sink
from fastapi.testclient import TestClient

COMMAND = {
    "tenant_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "conversation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "content": {"type": "text", "text": "hello"},
}


@pytest.fixture()
def client() -> TestClient:
    with TestClient(channel_sink.app) as test_client:
        test_client.post("/test/reset")
        yield test_client
        test_client.post("/test/reset")


def test_a_send_can_be_found_again_by_the_key_it_was_sent_with(client: TestClient) -> None:
    sent = client.post("/send", json=COMMAND, headers={"Idempotency-Key": "key-001"})
    assert sent.status_code == 200

    looked_up = client.get("/send/by-key/key-001")

    assert looked_up.status_code == 200
    assert looked_up.json() == sent.json()


def test_a_key_nobody_sent_is_a_404_and_that_is_a_real_answer(client: TestClient) -> None:
    """"No record of this key" is what lets a dispatcher stop guessing.

    It still ends in `unknown`, but an `unknown` that was *asked for* -- which
    is the distinction L1 exists to create.
    """

    assert client.get("/send/by-key/never-sent").status_code == 404


def test_a_by_key_lookup_is_not_swallowed_by_the_operation_id_route(client: TestClient) -> None:
    """The ordering guard, and why it is worth a test of its own.

    `/send/{operation_id}` matches `/send/by-key` too. Declared in the wrong
    order, every by-key lookup would be a lookup for an operation whose id is
    literally "by-key", would 404, and would be indistinguishable from "the
    provider has no record of it" -- a silent, permanent false negative in the
    one query that decides whether a message is lost.
    """

    client.post("/send", json=COMMAND, headers={"Idempotency-Key": "key-002"})
    operation_id = client.get("/send/by-key/key-002").json()["provider_operation_id"]

    # Both doors open, and they open onto the same receipt.
    assert client.get(f"/send/{operation_id}").json()["provider_operation_id"] == operation_id
    assert client.get("/send/by-key/key-002").json()["provider_operation_id"] == operation_id
    # And the trap: the literal segment is not treated as an operation id.
    assert client.get("/send/by-key").status_code == 404


def test_the_same_key_twice_is_the_same_receipt(client: TestClient) -> None:
    """Idempotency, restated here because the lookup now depends on it.

    The dispatcher's whole reconcile story assumes the key identifies one send
    on the provider's side. If a second POST minted a second operation id, a
    by-key lookup after a timeout could report a *different* send's outcome.
    """

    first = client.post("/send", json=COMMAND, headers={"Idempotency-Key": "key-003"})
    second = client.post("/send", json=COMMAND, headers={"Idempotency-Key": "key-003"})

    assert first.json() == second.json()
    assert client.get("/send/by-key/key-003").json() == first.json()
