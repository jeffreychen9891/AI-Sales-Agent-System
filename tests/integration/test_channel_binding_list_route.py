"""Freeze B2 #1: `GET /channel-bindings` -- masked metadata, and the omissions.

The freeze note is 「返回 masked metadata，移除 `app_secret` 字段」, and ADR-0048
§二.6 (K9) settled what "masked" means here: the secret is not in the database at
all. So the risk this file guards is not a leaked secret column -- there is none
-- but a projection that quietly widens.

Two cases carry the weight:

* `test_nothing_credential_shaped_reaches_the_wire` asserts against the whole
  serialised body and the exact key set, not a chosen field, because the failure
  this guards is somebody adding a column to the `SELECT` later;
* `test_the_grant_stops_at_the_columns_this_route_returns` is the same property
  one layer down, where the application cannot undo it -- and it is the one that
  pins **T123**: `provider_instance_id` is readable by nobody, and a list route
  is not where that gets decided.
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

#: The seeded binding's callback token, from `tests/conftest.py`. Its **hash** is
#: what lives in the row, and neither must ever reach the wire.
SEEDED_CONFIG_ID = "config-a"

EXPECTED_KEYS = {
    "config_id",
    "source",
    "status",
    "release_id",
    "current_valid_from",
    "next_valid_from",
    "overlap_ends_at",
    "created_at",
    # **K9's masked metadata, added by B2 #4.** Until migration 029 these
    # columns did not exist and this route counted as only half-delivered
    # (ADR-0053); the freeze's condition for calling it delivered is that they
    # appear here. Widening this set is exactly what the case below is built to
    # make somebody argue for -- so the argument is: K9 names these six as the
    # masked metadata field set, and `fingerprint` is eight characters by that
    # ruling, not a verifier.
    "secret_ref",
    "version",
    "fingerprint",
    "updated_at",
    "last_verified_at",
}


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


def _token_hashes() -> list[str]:
    with _connection() as connection:
        return [
            str(row["current_token_hash"])
            for row in connection.execute(
                "SELECT current_token_hash FROM channel_bindings"
            ).fetchall()
        ]


def test_the_page_lists_this_tenants_bindings(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`Page<T>` through the shared constructor, validated as a whole."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = client.get("/channel-bindings", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("page", body)
    assert [item["config_id"] for item in body["items"]] == [SEEDED_CONFIG_ID]
    binding = body["items"][0]
    assert binding["status"] == "active"
    assert binding["source"] == "wecom_proxy"
    assert binding["release_id"] == str(seeded_database["profile_release"])
    assert response.headers["cache-control"] == "no-store, private"


def test_nothing_credential_shaped_reaches_the_wire(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The projection is a fixed key set, and the hash is not in it.

    **A token hash is not a redacted token.** It is the verifier the ingress path
    compares a presented token against, so anybody holding it can test guesses
    offline for as long as they like. Asserted against the whole serialised body
    rather than key by key, because what this guards is a column somebody adds
    to the `SELECT` later -- and an equality on the key set is what turns that
    addition into a failing test rather than a silent widening.

    **It did exactly that in B2 #4**, which is worth recording because the
    widening was legitimate and the case still had to be argued past. K9 names
    the masked metadata field set and the freeze requires it here; the
    fingerprint is eight characters *by that ruling*, which is why it is not a
    verifier and the two token hashes still are. The case below keeps the
    distinction honest: the full hashes must still be absent from the body, and
    that assertion is untouched.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = client.get("/channel-bindings", headers=headers)

    hashes = _token_hashes()
    assert hashes, "the fixture seeds a binding with a token hash"
    for value in hashes:
        assert value not in response.text
    assert seeded_database["channel_token"] not in response.text
    for item in response.json()["items"]:
        assert set(item) == EXPECTED_KEYS
        # The fingerprint column is eight characters by K9's ruling. If a later
        # change ever puts a full digest here, the key set would not notice --
        # the key is the same. The length is the part that makes it metadata
        # rather than a verifier, so the length is asserted.
        if item["fingerprint"] is not None:
            assert len(item["fingerprint"]) == 8


def test_the_provider_instance_id_is_not_in_the_projection(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T123**, asserted rather than trusted to a comment.

    Migration 017 added this column deliberately readable by nobody and wrote,
    in the column comment, that granting it belongs to the outbound wiring batch
    -- together with the capability declaration T78 still owes. The failure this
    guards is not malice; it is a list route picking the column up because it
    was adjacent.

    The value is written here first, so the assertion is about a column that has
    something in it. A test against a `NULL` would pass for the wrong reason.
    """

    with _connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET provider_instance_id = %s WHERE config_id = %s",
            ("guid-that-must-not-travel", SEEDED_CONFIG_ID),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = client.get("/channel-bindings", headers=headers)

    assert response.status_code == 200, response.text
    assert "guid-that-must-not-travel" not in response.text
    assert "provider_instance_id" not in response.text


def test_the_rotation_timestamps_are_the_rotation_state(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """No `rotation_pending` flag: the timestamps say it, and they are not secret.

    A boolean would have needed either `next_token_hash` -- the verifier -- or an
    inference the schema does not guarantee. Here the two nullable timestamps are
    set and come back, which is the whole of what a caller needs to know about an
    in-flight rotation.

    **The last clause stopped being true with migration 028**, and the fixture
    below is why the change was worth making. It used to set the two timestamps
    and no `next_token_hash` -- a row the ingress path treats as *no rotation at
    all* (`adapter.py:67` requires the hash), while this route reported one. The
    constraint refuses that row now, so the fixture has to write a rotation that
    could really exist, and "the timestamps are the rotation state" is a schema
    guarantee rather than an inference.
    """

    with _connection() as connection:
        connection.execute(
            """
            UPDATE channel_bindings
               SET next_token_hash = %s,
                   next_valid_from = now() + interval '1 day',
                   overlap_ends_at = now() + interval '2 days'
             WHERE config_id = %s
            """,
            ("b" * 64, SEEDED_CONFIG_ID),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    binding = client.get("/channel-bindings", headers=headers).json()["items"][0]

    assert binding["current_valid_from"] is not None
    assert binding["next_valid_from"] is not None
    assert binding["overlap_ends_at"] is not None


def test_a_binding_with_no_rotation_in_flight_answers_null(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half: the fields exist and are `null`, not absent.

    A key that disappears when there is nothing to say makes every client write
    a `get()` with a default, and makes the page's shape depend on data.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    binding = client.get("/channel-bindings", headers=headers).json()["items"][0]

    assert binding["next_valid_from"] is None
    assert binding["overlap_ends_at"] is None


def test_another_tenants_bindings_are_not_listed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS, asserted where a leak would be quiet.

    Tenant B gets a binding of its own so that "the page is empty for the other
    tenant" cannot pass because there was nothing to leak in the first place.
    """

    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES ('config-b', %s, %s, %s)
            """,
            (
                seeded_database["tenant_b"],
                seeded_database["profile_release"],
                "b" * 64,
            ),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    body = client.get("/channel-bindings", headers=headers).json()

    assert [item["config_id"] for item in body["items"]] == [SEEDED_CONFIG_ID]
    assert "config-b" not in client.get("/channel-bindings", headers=headers).text


def test_a_caller_without_the_permission_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`channel_config.update` is the neighbour, and it is not enough."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = client.get("/channel-bindings", headers=headers)

    assert response.status_code == 403
    assert SEEDED_CONFIG_ID not in response.text


def test_the_route_requires_authentication(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    assert client.get("/channel-bindings").status_code == 401


def test_the_grant_stops_at_the_columns_this_route_returns(
    seeded_database: dict[str, Any],
) -> None:
    """The second defence, read out of the catalog rather than trusted.

    The application decides what to put in the `SELECT`; the grant decides what
    the connection could read if somebody changed their mind. Both hashes and
    `provider_instance_id` are absent from the grant, so a widened projection
    fails at the database rather than on the wire.

    **Not an equality on the granted set**: later batches add readable columns
    legitimately -- the six K9 columns are coming with B2 #2/#4 -- and an
    equality would turn each of those into a false alarm, which is how a guard
    like this gets loosened (B1 #15's independent review, P2-2).
    """

    with _connection() as connection:
        granted = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.column_privileges
                 WHERE table_name = 'channel_bindings' AND grantee = 'fastagent_app'
                   AND privilege_type = 'SELECT'
                """
            ).fetchall()
        }

    assert {
        "config_id",
        "source",
        "status",
        "profile_release_id",
        "current_valid_from",
        "next_valid_from",
        "overlap_ends_at",
        "created_at",
    } <= granted
    assert not granted & {
        # The verifier, twice.
        "current_token_hash",
        "next_token_hash",
        # T123: granting this is the outbound wiring batch's decision.
        "provider_instance_id",
    }


def test_a_page_larger_than_the_limit_refuses_rather_than_lying(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409 `PAGE_LIMIT_EXCEEDED`, the shape `_page_or_refuse` exists for.

    `Page` derives `has_more` from `next_cursor`, so a route that silently
    truncated would be claiming there is nothing more while never having looked.
    Cursors are not built yet, so the honest answer is a refusal -- and it is
    deterministic, which is why it is a 409 and not a 503.
    """

    from fastagent_v2.api.app import MAX_PAGE_ITEMS

    with _connection() as connection:
        for index in range(MAX_PAGE_ITEMS + 1):
            connection.execute(
                """
                INSERT INTO channel_bindings(
                    config_id, tenant_id, profile_release_id, current_token_hash
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    f"config-bulk-{index:04d}",
                    seeded_database["tenant_a"],
                    seeded_database["profile_release"],
                    f"{index:064x}",
                ),
            )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = client.get("/channel-bindings", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "PAGE_LIMIT_EXCEEDED"


def test_the_listed_binding_is_the_one_the_repin_route_names(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The page and the write route agree about what a binding is called.

    `PUT /channel-bindings/{config_id}/profile-release` takes the `config_id`
    this page publishes, and the release id is the field it changes. A listing
    whose identifiers the write route does not accept is a listing nobody can
    act on -- and the two routes were built two days apart.

    **This case earned its keep on the first run.** The page published
    `profile_release_id`, the column's name; the whole wire vocabulary --
    `GET /profiles/releases`, the repin request, the repin response -- says
    `release_id`. A client reading a binding here and posting it back got a 422
    naming a field it had never heard of.
    """

    client, headers = _client_for(
        settings, seeded_database, ["channel_config.list", "channel_config.update"]
    )
    listed = client.get("/channel-bindings", headers=headers).json()["items"][0]

    repinned = client.put(
        f"/channel-bindings/{listed['config_id']}/profile-release",
        json={"release_id": listed["release_id"]},
        headers={**headers, "Idempotency-Key": f"repin-{uuid4()}"},
    )

    assert repinned.status_code in {200, 409}, repinned.text
    assert repinned.status_code != 404, "the page published an id the write route rejects"
