"""`PUT /tenants/current/manager-contact` — the writer `012` never had (ADR-0104).

**What makes this route worth its own file.** `tenant_manager_contacts` has had
`SELECT, INSERT, UPDATE` granted to `fastagent_app` since migration `012`, two
production readers (`digest.py`, `pipeline.py`) and **zero writers**: no route,
no script, no seed, only test fixtures reaching into the table. ADR-0078 ruled in
M1a that the deployment would write it by hand and wrote down the cost — a tenant
nobody configured is told nothing, and nothing errors. ADR-0104 reopened that on
2026-09-08, because B1-5 makes delivery real and the silence starts costing a
merchant something.

The cases are about the three ways a configuration write goes wrong: the wrong
caller can use it, it can reach the wrong tenant's row, or it can accept a shape
the readers cannot use.
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

pytestmark = [pytest.mark.integration]

#: The owner of this configuration. `tenant_manager_contact.update` is a write on
#: a **tenant-owned** resource, so it is grantable -- unlike `tenant.update`,
#: which `platform_only_actions` reserves for the deployment (ADR-0075). That
#: distinction is why ADR-0104's first draft was wrong, and why the two cases
#: below (a writer succeeds, a reader is refused) only mean something together.
WRITER = ["tenant_manager_contact.update"]

#: Someone who may read their console but not redirect where notifications go.
READER = ["tenant.read"]


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


def _contact_row(tenant_id: UUID) -> dict[str, Any] | None:
    with _migration_connection() as connection:
        return connection.execute(
            """
            SELECT channel, config_id, external_user_id
              FROM tenant_manager_contacts WHERE tenant_id = %s
            """,
            (tenant_id,),
        ).fetchone()


def test_a_writer_can_set_the_contact_and_the_readers_can_use_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The gap ADR-0078 left open, closed: a caller writes the row.

    Asserted against the **table**, not the response body, because the response
    is this route's own words while the two production readers read the row. A
    route that answered `200` and wrote nothing would satisfy an assertion about
    what it said.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-userid-adr104",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-1"},
    )

    assert response.status_code == 200, response.text
    row = _contact_row(tenant_id)
    assert row is not None, "the route answered 200 and wrote nothing"
    assert row["channel"] == "wecom_proxy"
    assert row["config_id"] == "config-a"
    assert row["external_user_id"] == "manager-userid-adr104"


def test_writing_twice_replaces_rather_than_duplicates(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """One row per tenant is the schema's rule -- `012`'s primary key is the tenant.

    A second write is a correction: somebody left, or the first attempt named the
    wrong person. It has to land, and it must not leave the old address behind
    for the digest to choose between.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, WRITER)
    for user_id in ("manager-first", "manager-second"):
        response = client.put(
            "/tenants/current/manager-contact",
            json={
                "channel": "wecom_proxy",
                "config_id": "config-a",
                "external_user_id": user_id,
            },
            headers={**headers, "Idempotency-Key": f"manager-contact-{user_id}"},
        )
        assert response.status_code == 200, response.text

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT external_user_id FROM tenant_manager_contacts WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
    assert [row["external_user_id"] for row in rows] == ["manager-second"]


def test_a_reader_without_the_write_action_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A read permission does not carry the right to redirect notifications.

    **Paired with the first case on purpose.** Under ADR-0104's first draft the
    action was `tenant.update`, which `platform_only_actions` makes ungrantable
    to any merchant role -- that route would have refused *everybody*, and a
    file that only asserted "the reader is refused" would have passed while the
    feature did not exist.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, READER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-should-not-land",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-reader"},
    )

    assert response.status_code == 403, response.text
    row = _contact_row(tenant_id)
    assert row is None or row["external_user_id"] != "manager-should-not-land"


def test_the_scope_comes_from_the_token_not_from_a_parameter(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other tenant's row is untouched, and there is no parameter to touch it with.

    `GET /tenants/current` froze this shape for reads (freeze B1 #7): scope comes
    from the token, never from a parameter. A write inherits the rule and raises
    the stakes -- reading another tenant's configuration is a leak, writing it
    redirects their notifications to somebody of the caller's choosing.
    """

    other = UUID(str(seeded_database["tenant_b"]))
    before = _contact_row(other)
    client, headers = _client_for(settings, seeded_database, WRITER)

    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-for-tenant-a",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-scope"},
    )
    assert response.status_code == 200, response.text

    after = _contact_row(other)
    assert after == before, "tenant B's contact changed while tenant A wrote its own"


@pytest.mark.parametrize(
    "body",
    [
        {"channel": "sms", "config_id": "config-a", "external_user_id": "x"},
        {"channel": "wecom_proxy", "config_id": "", "external_user_id": "x"},
        {"channel": "wecom_proxy", "config_id": "config-a", "external_user_id": ""},
        {"channel": "wecom_proxy", "config_id": "config-a"},
        {
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "x",
            "extra": "no",
        },
    ],
    ids=["unknown-channel", "empty-config", "empty-user", "missing-field", "extra-field"],
)
def test_a_shape_the_readers_could_not_use_is_refused_at_the_edge(
    settings: Settings, seeded_database: dict[str, Any], body: dict[str, Any]
) -> None:
    """422 naming the field, rather than a 500 naming a database constraint.

    Half an address is not an address: both readers use the triple whole, so a
    row with an empty `config_id` reaches nobody **while looking configured** --
    which is worse than the empty state ADR-0078 described, because the empty
    state at least tells an operator that nothing was set.
    """

    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json=body,
        headers={**headers, "Idempotency-Key": "manager-contact-invalid"},
    )
    assert response.status_code == 422, response.text

def test_an_unknown_config_id_is_refused_by_name_not_by_constraint(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The P1-1 path.** A caller copies a `config_id` and the binding is gone.

    This is not exotic input: `config_id` comes from `GET /channel-bindings`,
    and a binding can be retired or re-pinned between reading it and writing
    here. The first version of this route let the foreign key surface as
    `INTERNAL_ERROR` with **`retryable: true`** -- which is false twice over,
    since retrying cannot make a binding exist, and the model's own docstring
    already says a 422 naming the field beats a 500 naming a constraint.
    """

    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": f"not-a-binding-{uuid4()}",
            "external_user_id": "manager-userid",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-unknown-binding"},
    )

    assert response.status_code == 422, response.text
    assert "config_id" in response.text


def test_an_over_long_user_id_is_refused_at_the_edge(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The other P1-1 path**: the model said 200, the column says 128.

    Nothing had aligned those two numbers, so a 129-character id passed
    validation and died in the database. The number here is deliberately
    `128 + 1` rather than something huge -- the bug lived in the 72-character
    gap between the two limits, and a 10,000-character id would have been
    refused by either of them.
    """

    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "u" * 129,
        },
        headers={**headers, "Idempotency-Key": "manager-contact-long-user"},
    )

    assert response.status_code == 422, response.text
    assert "external_user_id" in response.text


def test_a_correction_moves_updated_at_and_names_who_did_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The P2-1 path**: this route is the table's only writer, so if it does
    not maintain the row's own bookkeeping, nothing does.

    `updated_at`'s `DEFAULT now()` fires on INSERT only. Without the `SET`, the
    column froze at the first write for ever while the row kept changing --
    a field that is present, meaningful, and lying, which is the shape T118 and
    T193 are about. `updated_by` was never written at all, though the route puts
    the same subject id into its audit row.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, WRITER)

    def write(user_id: str, key: str) -> None:
        response = client.put(
            "/tenants/current/manager-contact",
            json={
                "channel": "wecom_proxy",
                "config_id": "config-a",
                "external_user_id": user_id,
            },
            headers={**headers, "Idempotency-Key": key},
        )
        assert response.status_code == 200, response.text

    def bookkeeping() -> dict[str, Any]:
        with _migration_connection() as connection:
            row = connection.execute(
                """
                SELECT updated_at, updated_by, display_name
                  FROM tenant_manager_contacts WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
        assert row is not None
        return row

    write("manager-first", "manager-contact-book-1")
    first = bookkeeping()
    write("manager-second", "manager-contact-book-2")
    second = bookkeeping()

    assert second["updated_at"] > first["updated_at"], (
        "updated_at did not move on a correction -- it is frozen at the first write"
    )
    assert second["updated_by"] is not None, "nobody is recorded as having changed it"
    assert second["display_name"] is None, (
        "a new address kept the previous person's name"
    )


def test_the_audit_row_names_the_channel_and_not_the_person(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The redaction promise, finally guarded** (P3-1).

    The route's comment says it records the channel and the config but not the
    `external_user_id`, because an audit row is read by more people than the row
    it describes. That was a promise with nothing holding it: nine cases and not
    one of them read `audit_events`.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-userid-secret",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-audit"},
    )
    assert response.status_code == 200, response.text

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT reason_code, resource_type, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'tenant_manager_contact.update'
             ORDER BY created_at DESC LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()

    assert row is not None, "the write left no audit row"
    assert row["reason_code"] == "MANAGER_CONTACT_SET"
    assert row["resource_type"] == "tenant_manager_contact"
    assert set(row["evidence"]) == {"channel", "config_id"}, (
        "the audit evidence carries something other than the channel and the "
        f"config -- the user id must not be in here: {sorted(row['evidence'])}"
    )


def test_the_answer_echoes_what_it_stored_and_carries_a_receipt(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """ADR-0104 §五.1 builds no `GET`, so this answer is the only readback there is.

    The first version answered `{tenant_id, channel}` -- one of the three things
    it was handed -- which meant a merchant who mistyped an `external_user_id`
    had no surface anywhere in the system that could tell them what landed
    (independent review 2026-09-08, P3-3). Echoing is not a read surface: it
    answers "what did *this call* do", which is the one question that needs no
    `GET`.

    The `{receipt, result}` envelope is ADR-0050 §三 and the shape
    `PUT /control-policy` uses -- the route this batch named as its template
    twice.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    client, headers = _client_for(settings, seeded_database, WRITER)
    response = client.put(
        "/tenants/current/manager-contact",
        json={
            "channel": "wecom_proxy",
            "config_id": "config-a",
            "external_user_id": "manager-userid-echoed",
        },
        headers={**headers, "Idempotency-Key": "manager-contact-echo"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"receipt", "result"}, (
        f"the command envelope is not {{receipt, result}}: {sorted(body)}"
    )
    receipt = body["receipt"]
    assert receipt["status"] == "succeeded"
    assert receipt["idempotency_key"] == "manager-contact-echo"
    assert receipt["resource_ref"]["resource_type"] == "tenant_manager_contact"
    assert body["result"] == {
        "tenant_id": str(tenant_id),
        "channel": "wecom_proxy",
        "config_id": "config-a",
        "external_user_id": "manager-userid-echoed",
    }, "the answer does not echo the triple it was given"
