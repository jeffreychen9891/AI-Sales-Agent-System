"""Freeze B2 #3: `PATCH /channel-bindings/{config_id}`.

The freeze note is 「仅非 secret 元数据」, and after the split of `C0A-033`
(`guid/key/secret/active/provider`) that leaves exactly one column:
`provider_instance_id`, the guid `017` added with the comment "Written by route
freeze B2 when it lands".

**The case that carries this batch is
`test_the_response_cannot_show_the_field_it_just_wrote`.** Migration `025`
grants `UPDATE` and deliberately not `SELECT`, because **T123** reserves the
read for the outbound wiring batch -- so this route can write a column that no
route and no runtime role can read back. That is a real cost, it is registered
as **T135**, and it is asserted here rather than described in a comment, so that
the batch which closes T123 finds a case that fails and tells it why.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SEEDED_CONFIG_ID = "config-a"
A_GUID = "wewe-guid-0001"


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


def _patch(
    client: TestClient,
    headers: dict[str, str],
    body: dict[str, Any],
    config_id: str = SEEDED_CONFIG_ID,
) -> Any:
    return client.patch(
        f"/channel-bindings/{config_id}",
        json=body,
        headers={**headers, "Idempotency-Key": f"update-binding-{uuid4()}"},
    )


def _stored(config_id: str = SEEDED_CONFIG_ID) -> dict[str, Any] | None:
    """Read through the **migration** connection.

    The application role cannot read `provider_instance_id` at all (T123), which
    is the property this file spends most of its cases on. Every assertion about
    what was actually written therefore has to come from a connection that is
    not the one under test.
    """

    with _connection() as connection:
        return connection.execute(
            "SELECT provider_instance_id, status, source FROM channel_bindings"
            " WHERE config_id = %s",
            (config_id,),
        ).fetchone()


def _audit_rows() -> list[dict[str, Any]]:
    with _connection() as connection:
        return connection.execute(
            "SELECT actor_id, action, resource_type, resource_id, reason_code, evidence"
            " FROM audit_events WHERE resource_type = 'channel_binding' ORDER BY created_at"
        ).fetchall()


# --------------------------------------------------------------------------
# The one field
# --------------------------------------------------------------------------


def test_the_guid_is_written(settings: Settings, seeded_database: dict[str, Any]) -> None:
    """`017`'s column comment names this batch as its writer; here it writes."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _patch(client, headers, {"provider_instance_id": A_GUID})

    assert response.status_code == 200, response.text
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] == A_GUID


def test_the_response_cannot_show_the_field_it_just_wrote(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**T135**, and the reason it is a registration rather than a bug.

    `PATCH /employees/{id}` echoes stored values because "what did my partial
    update leave here" is the question a partial update creates. This route
    cannot: `025` grants `UPDATE (provider_instance_id)` and no `SELECT`,
    because granting the read is half of **T123**'s decision and a response body
    is not where that gets taken.

    Two things are asserted, because only together do they mean anything: the
    key is absent, **and** the value the caller just sent does not appear
    anywhere in the body under another name.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _patch(client, headers, {"provider_instance_id": A_GUID})

    assert response.status_code == 200, response.text
    assert "provider_instance_id" not in response.json()
    assert A_GUID not in response.text


def test_the_body_is_the_shape_the_list_route_publishes(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The two routes describe one binding, so they must describe it the same way.

    `test_the_listed_binding_is_the_one_the_repin_route_names` exists because a
    list route and a write route built two days apart disagreed about
    `release_id` vs `profile_release_id`. This is the same assertion for the
    same reason, made against the live pair rather than against a literal.
    """

    client, headers = _client_for(
        settings, seeded_database, ["channel_config.update", "channel_config.list"]
    )

    listed = client.get("/channel-bindings", headers=headers)
    patched = _patch(client, headers, {"provider_instance_id": A_GUID})

    assert listed.status_code == 200, listed.text
    assert patched.status_code == 200, patched.text
    item = next(
        row for row in listed.json()["items"] if row["config_id"] == SEEDED_CONFIG_ID
    )
    assert set(patched.json()) == set(item) | {"correlation_id"}
    assert patched.headers["cache-control"] == "no-store, private"


def test_an_explicit_null_clears_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The sibling PATCH's rule, deliberately unchanged here.

    `017` made the column nullable because a binding row exists before a guid
    does, so "this binding has no instance" is a state the schema models and an
    operator re-provisioning a binding needs to be able to express.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    assert _patch(client, headers, {"provider_instance_id": A_GUID}).status_code == 200
    cleared = _patch(client, headers, {"provider_instance_id": None})

    assert cleared.status_code == 200, cleared.text
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] is None


def test_an_empty_body_changes_nothing(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """422 `NO_FIELDS_SUBMITTED`, and the stored value untouched.

    With one field, "omitted" and "empty body" are the same request -- and the
    distinction `model_fields_set` draws is what keeps the answer 422 rather
    than a write of `NULL`. The day a second field lands, this case is what
    catches an implementation that started treating absence as clearing.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])
    assert _patch(client, headers, {"provider_instance_id": A_GUID}).status_code == 200

    response = _patch(client, headers, {})

    assert response.status_code == 422
    assert response.json()["code"] == "NO_FIELDS_SUBMITTED"
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] == A_GUID


def test_a_field_this_route_does_not_own_cannot_be_named(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`extra="forbid"`, against the two columns the freeze gave to other routes.

    `status` is B2 #5's and `source` is settled at creation; mass assignment
    through a partial-update route is exactly what parity's 「宽泛 PUT/partial
    mass assignment 拆成有限 action」 is about. Three defences answer this --
    the model, the statement, and `025`'s column-level grant -- and this case
    exercises the first while `test_the_grant_stops_at_the_columns_this_route_writes`
    holds the third.

    **The first version of this case could not fail, and a reverse probe found
    it.** It sent `{"status": "disabled"}` alone and asserted 422 -- which is
    also what `extra="ignore"` produces, by a different route: nothing is bound,
    `submitted` is empty, and the answer is `NO_FIELDS_SUBMITTED`. So the code
    is asserted, not just the status, and the pairing below is the case that
    actually discriminates: with the field forbidden it is a 422 and nothing is
    written, with it ignored it is a **200** and the guid lands.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    for body in ({"status": "disabled"}, {"source": "other"}, {"config_id": "renamed"}):
        response = _patch(client, headers, body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "VALIDATION_FAILED", body
    paired = _patch(client, headers, {"provider_instance_id": A_GUID, "status": "disabled"})
    assert paired.status_code == 422
    assert paired.json()["code"] == "VALIDATION_FAILED"
    stored = _stored()
    assert stored is not None
    assert stored["status"] == "active"
    assert stored["source"] == "wecom_proxy"
    assert stored["provider_instance_id"] is None, "a refused body writes none of its fields"


def test_a_guid_made_of_nothing_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`" "` passes `017`'s CHECK and is still not a guid.

    `str_strip_whitespace` runs before `min_length=1`, so the blank is rejected
    by the model rather than stored and puzzled over later -- B1 #15's P3-3,
    applied to the column this route owns.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    assert _patch(client, headers, {"provider_instance_id": "   "}).status_code == 422
    assert _patch(client, headers, {"provider_instance_id": ""}).status_code == 422
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] is None


def test_the_length_bound_is_the_columns_own(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """128 in, 129 out -- `017`'s CHECK, not a number invented in the route.

    A stricter bound here would reject values the column accepts and an operator
    may already have seeded; a looser one would answer with a constraint name.
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    assert _patch(client, headers, {"provider_instance_id": "g" * 128}).status_code == 200
    assert _patch(client, headers, {"provider_instance_id": "g" * 129}).status_code == 422
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] == "g" * 128


def test_the_change_is_audited_with_the_value(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The value is in the evidence, and the sibling's rule says it should not be.

    `PATCH /employees/{id}` records field names only: a display name is
    somebody's name, and `GET /employees/{id}` can answer "what is it now".
    Neither holds here -- a provider instance id is infrastructure identity, and
    **no route and no runtime role can read this column** -- so an audit row
    without the value is a row nothing in the system can join back to a value.

    `resource_id` stays `NULL` and the identity travels in the evidence, for the
    reason the repin path paid for once (independent review 2026-09-03, P3-1).
    """

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    _patch(client, headers, {"provider_instance_id": A_GUID})

    rows = [row for row in _audit_rows() if row["action"] == "channel_binding.update"]
    assert len(rows) == 1
    row = rows[0]
    assert UUID(str(row["actor_id"])) == UUID(str(seeded_database["employee_a"]))
    assert row["resource_id"] is None
    assert row["reason_code"] == "CHANNEL_BINDING_METADATA_UPDATED"
    assert row["evidence"] == {
        "config_id": SEEDED_CONFIG_ID,
        "fields": ["provider_instance_id"],
        "provider_instance_id": A_GUID,
    }


# --------------------------------------------------------------------------
# The refusals
# --------------------------------------------------------------------------


def test_a_disabled_binding_is_a_conflict(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """409 `CHANNEL_BINDING_DISABLED`, and nothing written.

    B2 #5 destroys the callback token and M1a has no reactivation route, so a
    disabled binding can never send again: retargeting one writes state that can
    never take effect, and leaves a binding that looks configured and holds no
    credential if a reactivation route ever lands.

    The row is disabled through the migration connection rather than through the
    deactivate route, so this case says what it means even if that route changes.
    """

    with _connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET status = 'disabled' WHERE config_id = %s",
            (SEEDED_CONFIG_ID,),
        )
        connection.commit()
    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _patch(client, headers, {"provider_instance_id": A_GUID})

    assert response.status_code == 409
    assert response.json()["code"] == "CHANNEL_BINDING_DISABLED"
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] is None


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

    response = _patch(client, headers, {"provider_instance_id": A_GUID}, config_id="config-b")

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_NOT_FOUND"
    other = _stored("config-b")
    assert other is not None
    assert other["provider_instance_id"] is None, "another tenant's binding was not touched"


def test_a_binding_that_does_not_exist_is_the_same_404(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other half of the oracle argument: the two cases must be one answer."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = _patch(
        client, headers, {"provider_instance_id": A_GUID}, config_id="config-does-not-exist"
    )

    assert response.status_code == 404
    assert response.json()["code"] == "CHANNEL_BINDING_NOT_FOUND"


def test_a_caller_without_the_permission_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`channel_config.list` is the neighbour this route must not accept."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.list"])

    response = _patch(client, headers, {"provider_instance_id": A_GUID})

    assert response.status_code == 403
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] is None, "a refusal writes nothing"


def test_the_route_requires_an_idempotency_key(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """parity 5.2: 「所有 create/update command 默认需要 `Idempotency-Key`」."""

    client, headers = _client_for(settings, seeded_database, ["channel_config.update"])

    response = client.patch(
        f"/channel-bindings/{SEEDED_CONFIG_ID}",
        json={"provider_instance_id": A_GUID},
        headers=headers,
    )

    assert response.status_code == 422
    stored = _stored()
    assert stored is not None
    assert stored["provider_instance_id"] is None


@pytest.mark.security
def test_the_grant_stops_at_the_columns_this_route_writes(
    seeded_database: dict[str, Any],
) -> None:
    """The second defence, read out of the catalog rather than trusted.

    **The `SELECT` half is the one to read.** `025` widens the `UPDATE` face of
    this table by exactly one column and leaves the `SELECT` face alone, which
    is what keeps **T123** an open decision rather than a side effect of this
    batch. If a later batch grants the read, it should be because it decided to.

    **Not an equality on the granted sets**: later batches add columns
    legitimately -- B2 #4's rotation, B2 #2's K9 six -- and an equality turns
    each of those into a false alarm, which is how a guard like this gets
    loosened (B1 #15's independent review, P2-2).
    """

    with _connection() as connection:
        privileges = connection.execute(
            """
            SELECT privilege_type, column_name FROM information_schema.column_privileges
             WHERE table_name = 'channel_bindings' AND grantee = 'fastagent_app'
            """
        ).fetchall()
    writable = {row["column_name"] for row in privileges if row["privilege_type"] == "UPDATE"}
    readable = {row["column_name"] for row in privileges if row["privilege_type"] == "SELECT"}

    assert "provider_instance_id" in writable, "025 grants this route its one column"
    assert not writable & {
        "config_id",
        "tenant_id",
        # Settled at creation: a credential, a callback token and an instance id
        # are issued by the provider they belong to.
        "source",
        "created_at",
        "current_valid_from",
    }
    # **T123 is untouched.** The verifier pair is permanent; this one is a
    # registration whose expiry is the outbound wiring batch, and B2 #3 is not
    # that batch.
    assert not readable & {
        "current_token_hash",
        "next_token_hash",
        "provider_instance_id",
    }
