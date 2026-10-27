"""Freeze B2 #5: `POST /channel-bindings/{config_id}/deactivate`.

The freeze note is two clauses -- 「停用 + revoke callback token」 -- and the
second is the one that needed thinking about. Flipping `status` stops callbacks
now, because the ingress adapter refuses a non-`active` binding before it looks
at the token. It does not stop them later: the hash stays in the row, so
anything that re-activates the binding restores a credential that may have been
the reason for disabling it.

So the two cases that carry the weight are
`test_the_callback_stops_being_accepted_immediately` (the first clause, asserted
through the real ingress path rather than through `status`) and
`test_the_old_token_does_not_come_back_if_the_binding_does` (the second, which
is the only one that can tell a revocation from a flag).
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import sha256_token
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback

SEEDED_CONFIG_ID = "config-a"


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


def _deactivate(
    client: TestClient, headers: dict[str, str], config_id: str = SEEDED_CONFIG_ID
) -> Any:
    return client.post(
        f"/channel-bindings/{config_id}/deactivate",
        headers={**headers, "Idempotency-Key": f"deactivate-binding-{uuid4()}"},
    )


def _binding(config_id: str = SEEDED_CONFIG_ID) -> dict[str, Any] | None:
    with _connection() as connection:
        return connection.execute(
            """
            SELECT status, current_token_hash, next_token_hash, next_valid_from,
                   overlap_ends_at, profile_release_id, source
              FROM channel_bindings WHERE config_id = %s
            """,
            (config_id,),
        ).fetchone()


def _audit_rows() -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT actor_id, action, resource_type, resource_id, reason_code, evidence"
            " FROM audit_events WHERE resource_type = 'channel_binding' ORDER BY created_at"
        ).fetchall()


# --------------------------------------------------------------------------
# The two clauses
# --------------------------------------------------------------------------


def test_the_callback_stops_being_accepted_immediately(
    settings: Settings, seeded_database: dict[str, Any], channel_sink: str
) -> None:
    """Clause one, asserted through the **real ingress path**.

    Asserting `status == 'disabled'` would only show that a column moved. What a
    merchant is buying is that the channel stops accepting traffic, and the only
    thing that can show that is the callback itself being refused with the token
    that worked a moment earlier.
    """

    assert channel_sink  # the ingress app needs the sink fixture's environment
    from fastagent_v2.ingress.app import create_app as create_ingress

    ingress = TestClient(create_ingress(settings))
    token = str(seeded_database["channel_token"])
    payload = callback(external_event_id=f"evt-{uuid4()}", sender_id="customer-1")
    before = ingress.post(f"/wecom_proxy/{SEEDED_CONFIG_ID}/{token}", json=payload)
    assert before.status_code == 200, before.text
    assert before.json()["accepted"] is True

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])
    assert _deactivate(client, headers).status_code == 200

    after = ingress.post(
        f"/wecom_proxy/{SEEDED_CONFIG_ID}/{token}",
        json=callback(external_event_id=f"evt-{uuid4()}", sender_id="customer-1"),
    )
    # **The refusal moved from the status code into the body** (T204 /
    # ADR-0103): ten consecutive non-200 answers make WeComProxy delete the
    # callback address, so a refusal we mean may not be one of them. What a
    # merchant is buying is unchanged and is still asserted here -- the channel
    # stops accepting traffic with the token that worked a moment ago.
    assert after.status_code == 200
    assert after.json()["accepted"] is False
    assert after.json()["error"]["code"] == "CHANNEL_AUTHENTICATION_FAILED"


def test_the_old_token_does_not_come_back_if_the_binding_does(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Clause two, and the only case that tells a **revocation** from a flag.

    `status` alone would let a re-activation restore the original credential --
    and the reason somebody disabled a channel is quite often that its token
    leaked. So the hash is overwritten with one nobody holds, and this case
    proves it by putting the binding back to `active` **directly in the
    database** and presenting the token that used to work.

    Re-activated by SQL rather than by a route on purpose: there is no
    re-activation route in M1a, and writing this case against a route that does
    not exist would mean not writing it at all.
    """

    original = _binding()
    assert original is not None
    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])
    assert _deactivate(client, headers).status_code == 200
    revoked = _binding()
    assert revoked is not None
    assert revoked["current_token_hash"] != original["current_token_hash"]
    assert revoked["current_token_hash"] != sha256_token(str(seeded_database["channel_token"]))

    with _connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET status = 'active' WHERE config_id = %s",
            (SEEDED_CONFIG_ID,),
        )
        connection.commit()

    from fastagent_v2.ingress.app import create_app as create_ingress

    ingress = TestClient(create_ingress(settings))
    response = ingress.post(
        f"/wecom_proxy/{SEEDED_CONFIG_ID}/{seeded_database['channel_token']}",
        json=callback(external_event_id=f"evt-{uuid4()}", sender_id="customer-1"),
    )

    # The refusal is in the body now (T204 / ADR-0103): a status code on this
    # endpoint is a vote on whether WeComProxy keeps talking to us, and ten
    # consecutive non-200 answers delete the callback address. What this case
    # asserts is unchanged -- the revoked token does not work.
    assert response.status_code == 200, "a refusal we mean must not spend one of the ten"
    assert response.json()["accepted"] is False, (
        "the revoked token must not survive re-activation"
    )
    assert response.json()["error"]["code"] == "CHANNEL_AUTHENTICATION_FAILED"


def test_a_pending_rotation_is_cleared_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A scheduled next credential outliving the binding it belonged to.

    `next_token_hash` describes a credential that is about to become valid. Left
    behind, a re-enabled binding would accept a token whose rotation nobody
    remembers scheduling -- the same defect as clause two, one field over.
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
            ("c" * 64, SEEDED_CONFIG_ID),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    assert _deactivate(client, headers).status_code == 200

    stored = _binding()
    assert stored is not None
    assert stored["next_token_hash"] is None
    assert stored["next_valid_from"] is None
    assert stored["overlap_ends_at"] is None
    # **And the audit says it happened** (independent review 2026-09-05, P3-1).
    # Migration 024 spends a paragraph on why this clearing is necessary; a row
    # that does not record whether a second credential was destroyed is missing
    # the fact that paragraph is about.
    rows = [row for row in _audit_rows() if row["action"] == "channel_binding.deactivate"]
    assert len(rows) == 1
    assert rows[0]["evidence"]["pending_rotation_cleared"] is True


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_the_answer_is_a_receipt_and_says_the_token_is_gone(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`{"receipt": …, "result": …}` (ADR-0050 §三), validated whole.

    `callback_token_revoked` is in the result rather than left implied: the
    caller asked to stop a channel, and "the credential is gone as well" is the
    half they cannot infer from `status`.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _deactivate(client, headers)

    assert response.status_code == 200, response.text
    body = response.json()
    validate_control_plane_contract("command-receipt", body["receipt"])
    assert body["receipt"]["resource_ref"]["resource_type"] == "channel_config"
    assert body["receipt"]["resource_ref"]["resource_id"] == SEEDED_CONFIG_ID
    assert body["result"] == {
        "config_id": SEEDED_CONFIG_ID,
        "status": "disabled",
        "callback_token_revoked": True,
    }
    assert response.headers["cache-control"] == "no-store, private"


def test_no_hash_reaches_the_wire(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Neither the old verifier nor the new one.

    The new hash is of a token nobody holds, so publishing it leaks nothing
    usable -- but it is still a verifier, and B2 #1 refuses to publish one for
    reasons that do not stop applying because this particular token is
    unreachable. Asserted against the whole serialised body and the audit row.
    """

    original = _binding()
    assert original is not None
    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _deactivate(client, headers)

    revoked = _binding()
    assert revoked is not None
    for value in (original["current_token_hash"], revoked["current_token_hash"]):
        assert value not in response.text
        assert value not in str(_audit_rows())
    assert str(seeded_database["channel_token"]) not in response.text


def test_the_change_is_audited_against_the_binding(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`resource_id` stays NULL and the identity travels in the evidence.

    A binding's identity is `config_id`, which is `text`; the repin path put a
    release id in that column once and taught this codebase the difference
    (independent review 2026-09-03, P3-1).
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    _deactivate(client, headers)

    rows = [row for row in _audit_rows() if row["action"] == "channel_binding.deactivate"]
    assert len(rows) == 1
    row = rows[0]
    assert UUID(str(row["actor_id"])) == UUID(str(seeded_database["employee_a"]))
    assert row["resource_id"] is None
    assert row["reason_code"] == "CHANNEL_BINDING_DEACTIVATED"
    assert row["evidence"] == {
        "config_id": SEEDED_CONFIG_ID,
        "callback_token_revoked": True,
        # `False` here and `True` in `test_a_pending_rotation_is_cleared_too`:
        # a key that is only ever asserted true would say no more than the
        # constant beside it.
        "pending_rotation_cleared": False,
    }


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_a_second_call_is_a_conflict_and_mints_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409, and -- the part that matters -- **no second token is generated**.

    Re-running would overwrite the hash again, which is a write with no meaning
    and would let a retry manufacture the appearance of a fresh revocation.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])
    assert _deactivate(client, headers).status_code == 200
    after_first = _binding()
    assert after_first is not None

    second = _deactivate(client, headers)

    assert second.status_code == 409
    assert second.json()["code"] == "CHANNEL_BINDING_ALREADY_DISABLED"
    assert _binding() == after_first, "the refused call changed nothing"
    assert len([row for row in _audit_rows() if row["action"] == "channel_binding.deactivate"]) == 1


def test_another_tenants_binding_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """404, not 403: a distinguishable answer is a cross-tenant existence oracle."""

    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES ('config-b', %s, %s, %s)
            """,
            (seeded_database["tenant_b"], seeded_database["profile_release"], "b" * 64),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _deactivate(client, headers, config_id="config-b")

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_NOT_FOUND"
    other = _binding("config-b")
    assert other is not None
    assert other["status"] == "active", "another tenant's binding was not touched"


def test_a_binding_that_does_not_exist_is_the_same_404(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half of the oracle argument: the two cases must be one answer."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _deactivate(client, headers, config_id="config-does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_NOT_FOUND"


def test_a_caller_without_the_permission_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`channel_config.list` is the neighbour this route must not accept."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = _deactivate(client, headers)

    assert response.status_code == 403
    stored = _binding()
    assert stored is not None
    assert stored["status"] == "active", "a refusal writes nothing"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: every command carries one."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/deactivate", headers=headers
    )

    assert response.status_code == 422


def test_the_grant_stops_at_the_columns_this_route_writes(
    seeded_database: dict[str, Any],
) -> None:
    """The second defence, read out of the catalog rather than trusted.

    **Not an equality**: later batches add writable columns legitimately, and an
    equality turns each of those into a false alarm -- which is how a guard like
    this gets loosened (B1 #15's independent review, P2-2).
    """

    with _connection() as connection:
        granted = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.column_privileges
                 WHERE table_name = 'channel_bindings' AND grantee = 'fastagent_app'
                   AND privilege_type = 'UPDATE'
                """
            ).fetchall()
        }

    assert {
        "status",
        "current_token_hash",
        "next_token_hash",
        "next_valid_from",
        "overlap_ends_at",
        # 007's grant, still there.
        "profile_release_id",
    } <= granted
    assert not granted & {
        "config_id",
        "tenant_id",
        "source",
        "created_at",
    }
    # **`provider_instance_id` left this list in B2 #3 (migration 025), and the
    # removal is not a weakening of what this case is about.** The line above
    # used to say "T123: still writable by nobody, and a deactivation route is
    # not where that gets decided" -- and a `PATCH` on the binding itself is
    # where it gets decided, for the writable half only. `017`'s column comment
    # named that batch in advance ("Written by route freeze B2 when it lands").
    #
    # What this case exists for is that a **deactivation** route writes the
    # binding's state and its credential and nothing else, and that is unchanged.
    # The half of T123 that is still open is the **read**, which no migration
    # grants and which
    # `test_channel_binding_update_route.py::test_the_grant_stops_at_the_columns_this_route_writes`
    # asserts.
