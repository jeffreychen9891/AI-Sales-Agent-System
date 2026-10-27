"""Route freeze B2 #14: `PUT /channel-bindings/{config_id}/profile-release`.

**This route is a door, not a room.** T15 built `repin_channel_binding` and the
target-stack repair behind it; T24's follow-up recorded what was missing --
the function had **no production caller at all**, so "no human or process can
trigger a repin today" and the exit item 三·② stayed ❌. Everything asserted here
about *what a repin does* was already true and already tested; what was not
testable before is that somebody can ask for one.

Two cases carry the weight:

* `test_a_second_repin_reports_that_nothing_changed` -- the idempotence that
  makes `PUT` the right verb, and the thing an operator reads to know whether
  their retry did anything;
* `test_live_conversations_are_left_alone_unless_asked_for` -- the default. The
  two operations have separate idempotency bases on purpose (`binding.py`, from
  the 2026-09-03 review): rebind first, decide about live conversations after. A
  route that always did both would take that sequence away, and the sequence is
  the one an operator actually uses.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# **The production-shaped fixture, not a convenient one.** Its docstring records
# why: a version that filled only the columns and left `state` at `'{}'` let
# eight tests and five probes pass while the repin never wrote the document the
# engine reads (independent review 2026-09-03, P1-2). Writing a second, simpler
# one here would have re-created exactly that gap -- and did, for one run.
from test_channel_binding_repin import _seed_conversation  # noqa: E402

CONFIG_ID = "config-a"


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


def _connection() -> Connection[Any]:
    return Connection.connect(_migration_url(), row_factory=dict_row)


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    with _connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded["tenant_a"])),
        employee_id=UUID(str(seeded["employee_a"])),
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


def _repin(
    client: TestClient, headers: dict[str, str], release_id: Any, **extra: Any
) -> Any:
    return client.put(
        f"/channel-bindings/{CONFIG_ID}/profile-release",
        json={"release_id": str(release_id), **extra},
        headers={**headers, "Idempotency-Key": f"repin-{uuid4()}"},
    )


def _binding_release(config_id: str = CONFIG_ID) -> UUID:
    with _connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id FROM channel_bindings WHERE config_id = %s",
            (config_id,),
        ).fetchone()
    return UUID(str(row["profile_release_id"]))


def _publish_second_release(client: TestClient, headers: dict[str, str]) -> UUID:
    """A second release to move to -- otherwise every repin is a no-op."""

    response = client.post(
        "/profiles/releases",
        json={"profile_id": "synthetic-two-axis-desk", "version": "1.0.3"},
        headers={**headers, "Idempotency-Key": f"publish-{uuid4()}"},
    )
    assert response.status_code in {200, 201}, response.text
    return UUID(response.json()["resource_ref"]["resource_id"])


def test_a_repin_moves_the_binding_and_returns_a_receipt(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    target = _publish_second_release(client, headers)
    before = _binding_release()
    assert before != target

    response = _repin(client, headers, target)

    assert response.status_code == 200, response.text
    body = response.json()
    # **The whole receipt object, and the whole body around it** (ADR-0050 §二).
    # The first version of this assertion built a dict out of six keys picked
    # from the body and fed *that* to the contract -- a check that passes
    # whatever else the body carries, which is the one thing that mattered here:
    # the body was `{**receipt, …six domain fields}` and `command-receipt`
    # is `additionalProperties: false`, so the real body did not validate while
    # this test was green. Picking keys out of a body is the only way to write
    # this check that cannot notice.
    assert set(body) == {"receipt", "result"}
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["status"] == "succeeded"
    # The domain half is pinned too, so a seventh field cannot appear without
    # somebody deciding to add it here.
    assert set(body["result"]) == {
        "changed",
        "release_id",
        "previous_release_id",
        "conversations_repinned",
        "conversations_remaining",
        "tenant_conversations_unlinked",
    }
    assert body["result"]["changed"] is True
    assert body["result"]["release_id"] == str(target)
    assert body["result"]["previous_release_id"] == str(before)
    assert _binding_release() == target


def test_a_second_repin_reports_that_nothing_changed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Why `PUT`: the state after two calls is the state after one.

    `changed` is what an operator reads to know whether their retry did
    anything. A route that answered `true` twice would make a retry
    indistinguishable from a real move.
    """

    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    target = _publish_second_release(client, headers)
    assert _repin(client, headers, target).json()["result"]["changed"] is True

    second = _repin(client, headers, target)

    assert second.status_code == 200
    assert second.json()["result"]["changed"] is False
    assert second.json()["result"]["previous_release_id"] == str(target)
    assert _binding_release() == target


def test_live_conversations_are_left_alone_unless_asked_for(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The default is `false`, and the default is the point.

    Rebinding and moving live conversations have separate idempotency bases so
    that an operator can rebind now and decide about the open conversations
    afterwards. This asserts the route preserves that choice rather than
    performing both.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    original = _binding_release()
    conversation_id = _seed_conversation(tenant_id=tenant_id, release_id=original)
    target = _publish_second_release(client, headers)

    body = _repin(client, headers, target).json()

    assert body["result"]["changed"] is True
    assert (
        body["result"]["conversations_repinned"] == 0
    ), "the default must not move anything"
    with _connection() as connection:
        still = connection.execute(
            "SELECT profile_release_id FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert UUID(str(still["profile_release_id"])) == original


def test_asking_for_it_moves_the_live_conversation_and_says_how_many(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The negative above proves nothing on its own.

    A route that ignored the flag entirely would pass it. This is the case that
    separates "the default is false" from "the flag does nothing".
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    original = _binding_release()
    conversation_id = _seed_conversation(tenant_id=tenant_id, release_id=original)
    with _connection() as connection:
        before = connection.execute(
            "SELECT generation FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()["generation"]
    target = _publish_second_release(client, headers)

    raw = _repin(client, headers, target, repin_live_conversations=True)
    assert raw.status_code == 200, raw.text
    body = raw.json()

    assert body["result"]["conversations_repinned"] == 1
    with _connection() as connection:
        moved = connection.execute(
            "SELECT profile_release_id, generation FROM conversation_aggregates WHERE id = %s",
            (conversation_id,),
        ).fetchone()
    assert UUID(str(moved["profile_release_id"])) == target
    # RFC 9.1: an explicit repin bumps the generation, which is what invalidates
    # the events already in flight for that conversation. Asserted as "advanced"
    # rather than as a literal: the starting value belongs to the fixture, and a
    # test that hard-codes it breaks when the fixture legitimately changes while
    # saying nothing about the property it was written for.
    assert moved["generation"] > before


def test_an_unknown_binding_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    target = _publish_second_release(client, headers)

    response = client.put(
        "/channel-bindings/config-that-does-not-exist/profile-release",
        json={"release_id": str(target)},
        headers={**headers, "Idempotency-Key": f"repin-{uuid4()}"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_OR_RELEASE_NOT_FOUND"


def test_an_unknown_release_is_the_same_answer_as_an_unknown_binding(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One code for both, so the route cannot be used to probe existence.

    Telling "that binding is not yours" apart from "that release is not yours"
    would let a caller enumerate either one.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _repin(client, headers, uuid4())

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_OR_RELEASE_NOT_FOUND"


def test_a_caller_without_channel_config_update_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(settings, seeded_database, ["profile_release.create"])
    before = _binding_release()

    response = _repin(client, headers, uuid4())

    assert response.status_code == 403
    assert _binding_release() == before, "a refusal writes nothing"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: every create/update command carries one."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = client.put(
        f"/channel-bindings/{CONFIG_ID}/profile-release",
        json={"release_id": str(uuid4())},
        headers=headers,
    )

    assert response.status_code == 422


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.put(
        f"/channel-bindings/{CONFIG_ID}/profile-release",
        json={"release_id": str(uuid4())},
        headers={"Idempotency-Key": f"repin-{uuid4()}"},
    )

    assert response.status_code == 401


def test_a_conversation_without_a_document_is_a_fault_not_a_missing_binding(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The 404 this route used to answer for a data-integrity fault.

    `binding.py` raises `ChannelBindingNotFound` from two places: the binding is
    not this tenant's, and -- in its fail-closed branch -- a conversation row
    carries no aggregate document. The route mapped both onto
    `404 CHANNEL_BINDING_OR_RELEASE_NOT_FOUND`, which sends an operator looking
    for a binding that is sitting right there, with `retryable: false` telling
    them to stop trying.

    **This is not hypothetical**: the first draft of this file seeded a
    conversation with `state = '{}'` and got exactly that 404, and it was read
    as "my fixture is wrong" (self-review §五.1). The route now reads the
    binding first, so the only `ChannelBindingNotFound` left is this one.

    Nothing here is half-written: `repin_channel_binding` does its work in one
    transaction, so the binding still points where it did.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "profile_release.create"]
    )
    original = _binding_release()
    conversation_id = _seed_conversation(tenant_id=tenant_id, release_id=original)
    with _connection() as connection:
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE id = %s",
            (Jsonb({}), conversation_id),
        )
        connection.commit()
    target = _publish_second_release(client, headers)

    response = _repin(client, headers, target, repin_live_conversations=True)

    assert response.status_code == 500, response.text
    assert response.json()["code"] == "CONVERSATION_DOCUMENT_MISSING"
    assert _binding_release() == original, "the transaction rolled the repin back"
