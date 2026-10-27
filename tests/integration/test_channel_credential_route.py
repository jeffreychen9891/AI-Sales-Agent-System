"""B2 #4: `POST /channel-bindings/{config_id}/credentials`, per ADR-0061.

**The case that carries this batch is
`test_the_request_body_has_no_field_for_the_secret_itself`.** Everything else
here is a route doing route things; what makes this batch what it is, is that
the drawing asked for a request carrying the credential and the deployment
cannot receive one -- so the shape of the request body *is* the decision.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import (
    RegisterChannelCredentialRequest,
    _channel_secret_fingerprint,
    create_app,
)
from fastagent_v2.runtime.settings import Settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

SEEDED_CONFIG_ID = "config-a"
SEEDED_TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
# **Named `PLACED_*`, not `SECRET_*`** -- see the self-review's last section.
# `check_secrets.py` keys on the name to the left of an assignment, so a
# constant called `PLACED_REF` holding any twelve-character string is a finding
# regardless of what it holds. This value is a *reference* and the material is a
# fixture; neither is a credential.
PLACED_REF = "wecom-app.v3"
PLACED_MATERIAL = b"a-placed-credential\n"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _settings_with_store(
    settings: Settings, tmp_path: Path, *, place: bool = True
) -> Settings:
    """The store is `<root>/<tenant_id>/<ref>` (independent review F2).

    The tenant segment is composed by the route and never supplied by the
    caller, so a `secret_ref` cannot name another tenant's file.
    """

    store = tmp_path / "channel-secrets" / SEEDED_TENANT
    store.mkdir(parents=True, exist_ok=True)
    if place:
        (store / PLACED_REF).write_bytes(PLACED_MATERIAL)
    return replace(settings, channel_secret_directory=str(tmp_path / "channel-secrets"))


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
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


def test_the_request_body_has_no_field_for_the_secret_itself() -> None:
    """**ADR-0061, asserted against the model rather than described in prose.**

    parity §5.2 defines `SecretWrite` as carrying the secret. It cannot here:
    K9 refuses a second credential store in the database and the Stage secret
    directory is not writable from this container. The deviation is the batch,
    so it is pinned where a future edit would have to notice it -- adding a
    `secret` field back would turn this red, and `extra="forbid"` means a caller
    cannot smuggle one through either.
    """

    fields = set(RegisterChannelCredentialRequest.model_fields)

    assert fields == {"secret_ref", "version"}
    for forbidden in ("secret", "app_secret", "credential", "material", "value"):
        assert forbidden not in fields
    with pytest.raises(ValueError, match="secret"):
        RegisterChannelCredentialRequest(secret_ref=PLACED_REF, version=1, secret="x")


def test_registering_a_placed_credential_records_the_metadata_and_not_the_secret(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The happy path, and the assertion that matters is the last one.

    The response carries K9's masked metadata field names -- not the column
    names, which are prefixed so `updated_at` cannot be read as the row's own
    `created_at`. The fingerprint is eight hex characters of the digest of the
    file the operator placed, computed by the server, which is what makes
    `last_verified_at` a fact rather than a claim.
    """

    scoped = _settings_with_store(settings, tmp_path)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 3},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["secret_ref"] == PLACED_REF
    assert body["version"] == 3
    assert len(body["fingerprint"]) == 8
    assert body["updated_at"] is not None and body["last_verified_at"] is not None
    # **The secret never appears anywhere in the answer.** Not masked, not
    # truncated -- absent.
    assert PLACED_MATERIAL.decode().strip() not in response.text


def test_a_reference_the_operator_never_placed_is_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """422, and the row is left alone.

    Recording a `secret_ref` that points at nothing would produce a binding that
    reads as configured and holds no credential -- the shape this repository
    keeps paying for. The server can tell, because it reads the file; a route
    that trusted the caller's word could not.
    """

    scoped = _settings_with_store(settings, tmp_path, place=False)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "CHANNEL_SECRET_NOT_PLACED"
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT secret_ref FROM channel_bindings WHERE config_id = %s",
            (SEEDED_CONFIG_ID,),
        ).fetchone()
    assert row is not None and row["secret_ref"] is None


def test_an_unwired_deployment_answers_503_rather_than_recording_a_guess(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """503, not 422: the caller did nothing wrong, the mount is missing (`C14`).

    Telling the two apart is the point. A 422 would send an operator looking for
    a typo in a `secret_ref` that is perfectly correct.
    """

    scoped = replace(settings, channel_secret_directory=None)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "CHANNEL_SECRET_STORE_UNAVAILABLE"


def test_the_permission_is_manage_secret_and_not_update(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """K9 gave this route its own action, and `channel_config.update` is not it.

    Three routes share `channel_config.update` (the manifest names them and
    says what that costs). This one does not, because granting somebody the
    right to repin a profile release should not also grant the right to say
    which credential a channel uses.
    """

    scoped = _settings_with_store(settings, tmp_path)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.update"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert response.status_code == 403, response.text


def test_the_audit_row_records_the_reference_and_never_the_fingerprint(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**ADR-0055's criterion, applied field by field.**

    `secret_ref` and the version are recorded: once the next rotation overwrites
    the row, nothing in the system can answer which file this binding used, and
    that is the criterion's third clause. The fingerprint is not: it is a
    credential-derived value, which the criterion's second clause excludes, and
    the audit log is read by more people than the column is.
    """

    scoped = _settings_with_store(settings, tmp_path)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])
    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 7},
    )
    assert response.status_code == 200, response.text
    fingerprint = response.json()["fingerprint"]

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT evidence::text AS evidence FROM audit_events"
            " WHERE resource_type = 'channel_binding' AND action = %s"
            " ORDER BY created_at DESC LIMIT 1",
            ("channel_binding.register_credential",),
        ).fetchone()

    assert row is not None
    assert PLACED_REF in row["evidence"]
    assert '"version": 7' in row["evidence"] or '"version":7' in row["evidence"]
    assert fingerprint not in row["evidence"]


# --------------------------------------------------------------------------
# Migration 029's two constraints
#
# **These exist because a reverse probe went green.** The probe mutated the
# `CHECK` in 029 and the suite stayed green -- which proved nothing about the
# constraint, because the database had already been migrated and editing the
# file changes nothing that is running. What it did prove is that **029's
# constraints had no cases at all**: the route was tested, the schema under it
# was not. A migration-level invariant has to be asserted against the database
# directly, the way 028's are, and that is what these do.
# --------------------------------------------------------------------------


def test_a_half_written_registration_is_refused(seeded_database: dict[str, Any]) -> None:
    """A `secret_ref` with no fingerprint claims a credential nobody checked.

    Both directions, because the constraint is an equality between `IS NULL`
    tests and a version catching only one direction would still pass a case that
    only wrote one direction.
    """

    from psycopg.errors import CheckViolation

    with _migration_connection() as connection:
        for column, value in (("secret_ref", PLACED_REF), ("secret_fingerprint", "0" * 8)):
            with pytest.raises(CheckViolation):
                connection.execute(
                    f"UPDATE channel_bindings SET {column} = %s WHERE config_id = %s",  # noqa: S608
                    (value, SEEDED_CONFIG_ID),
                )
            connection.rollback()


def test_a_verification_cannot_predate_the_registration_it_verifies(
    seeded_database: dict[str, Any],
) -> None:
    """`last_verified_at` is the one column that legitimately arrives later.

    Legitimately *later* -- not earlier. A verification stamped before the
    registration it verifies describes something that did not happen.
    """

    from psycopg.errors import CheckViolation

    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                """
                UPDATE channel_bindings
                   SET secret_ref = %s, secret_version = 1,
                       secret_fingerprint = %s,
                       secret_updated_at = now(),
                       secret_last_verified_at = now() - interval '1 hour'
                 WHERE config_id = %s
                """,
                (PLACED_REF, "0" * 8, SEEDED_CONFIG_ID),
            )
        connection.rollback()


def test_a_registration_with_no_verification_yet_is_accepted(
    seeded_database: dict[str, Any],
) -> None:
    """**The half that keeps the constraints honest.**

    Three refusals and no acceptance would pass equally well against a
    constraint that rejected every registration -- a worse bug than the gap it
    replaced. `secret_last_verified_at` is deliberately outside the all-or-
    nothing group, so a registration that has not been verified since is a legal
    row, and this is the case that says so.
    """

    with _migration_connection() as connection:
        connection.execute(
            """
            UPDATE channel_bindings
               SET secret_ref = %s, secret_version = 2,
                   secret_fingerprint = %s, secret_updated_at = now()
             WHERE config_id = %s
            """,
            (PLACED_REF, "0" * 8, SEEDED_CONFIG_ID),
        )
        connection.rollback()


# --------------------------------------------------------------------------
# Independent review F3: the branches the docstrings promised and nothing
# asserted -- plus the two properties F2 and F5 introduced.
# --------------------------------------------------------------------------


def test_another_tenants_binding_answers_the_same_404_as_a_missing_one(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**The case four sibling route files have and this one did not.**

    The route's docstring argues that another tenant's binding and a missing one
    must share one 404, or the route is a cross-tenant existence oracle, and
    `route-manifest.json` declares `RLS-CHANNEL-005` for it. Neither was backed
    by an assertion: `tenant_b` appeared zero times in this file.
    """

    scoped = _settings_with_store(settings, tmp_path)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    other = client.post(
        "/channel-bindings/config-b/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )
    missing = client.post(
        "/channel-bindings/config-does-not-exist/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert other.status_code == 404, other.text
    assert missing.status_code == 404
    # Identical, code and all: any difference here is the oracle.
    assert other.json()["code"] == missing.json()["code"] == "CHANNEL_BINDING_NOT_FOUND"


def test_another_tenants_placed_file_cannot_be_named(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """**Independent review F2**, and the assertion is about what is absent.

    The store used to be one flat directory: A could submit the name of B's
    placed credential, get back eight characters of its digest, and leave A's
    row pointing at B's file -- a pointer whose whole purpose is "load the
    credential from here later". The route now composes `<root>/<tenant>/<ref>`,
    so B's file is not reachable by any `secret_ref` A can write.
    """

    scoped = _settings_with_store(settings, tmp_path)
    elsewhere = Path(str(scoped.channel_secret_directory)) / str(uuid4())
    elsewhere.mkdir(parents=True, exist_ok=True)
    (elsewhere / "b-only.v1").write_bytes(b"another tenants credential\n")
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": "b-only.v1", "version": 1},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "CHANNEL_SECRET_NOT_PLACED"
    assert "another tenants credential" not in response.text


def test_an_empty_placed_file_is_refused(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """422 `CHANNEL_SECRET_EMPTY` -- promised in the self-review, never asserted.

    Without it the fingerprint is the digest of nothing, which compares *equal*
    across every binding that made the same mistake: the one value a fingerprint
    must never be able to take.
    """

    scoped = _settings_with_store(settings, tmp_path, place=False)
    store = Path(str(scoped.channel_secret_directory)) / SEEDED_TENANT
    (store / PLACED_REF).write_bytes(b"   \n")
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "CHANNEL_SECRET_EMPTY"


def test_a_reference_that_escapes_the_tenant_directory_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """Asked of the helper directly, as the reviewer suggested.

    Through the route the request model's pattern refuses first, so the
    containment check would never execute and the case would pass without
    exercising it -- a test that cannot fail. The helper is a module function,
    so it can be asked the question the pattern normally prevents.
    """

    scoped = _settings_with_store(settings, tmp_path)

    with pytest.raises(HTTPException) as refusal:
        _channel_secret_fingerprint(scoped, UUID(SEEDED_TENANT), "../" + PLACED_REF)

    assert refusal.value.status_code == 422
    assert refusal.value.detail == "CHANNEL_SECRET_REF_INVALID"


def test_a_trailing_newline_does_not_change_the_fingerprint(
    settings: Settings, tmp_path: Path
) -> None:
    """**Independent review F5.**

    `echo secret > f` and `printf secret > f` are the same credential to anybody
    who reads the file the way this repository reads its other secret files, and
    they used to produce different fingerprints -- emptiness was judged on the
    stripped bytes while the digest was taken over the raw ones. A fingerprint
    that moves with a trailing newline cannot support what `last_verified_at`
    claims.
    """

    scoped = _settings_with_store(settings, tmp_path, place=False)
    store = Path(str(scoped.channel_secret_directory)) / SEEDED_TENANT

    (store / PLACED_REF).write_bytes(b"the-credential")
    bare = _channel_secret_fingerprint(scoped, UUID(SEEDED_TENANT), PLACED_REF)
    (store / PLACED_REF).write_bytes(b"the-credential\n")
    trailing = _channel_secret_fingerprint(scoped, UUID(SEEDED_TENANT), PLACED_REF)

    assert bare == trailing


def test_a_disabled_binding_is_refused_with_409(
    settings: Settings, seeded_database: dict[str, Any], tmp_path: Path
) -> None:
    """The docstring promises 409 and nothing asserted it.

    B2 #5 destroys the callback token and M1a has no reactivation route, so a
    credential registered against a disabled binding is state that can never
    take effect.
    """

    scoped = _settings_with_store(settings, tmp_path)
    client, headers = _client_for(scoped, seeded_database, ["channel_config.manage_secret"])
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET status = 'disabled' WHERE config_id = %s",
            (SEEDED_CONFIG_ID,),
        )
        connection.commit()

    response = client.post(
        f"/channel-bindings/{SEEDED_CONFIG_ID}/credentials",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"secret_ref": PLACED_REF, "version": 1},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CHANNEL_BINDING_DISABLED"
