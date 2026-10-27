"""C1 (T81/T87): every control-plane error body is an `ErrorEnvelope`.

The defect this closes was a **shape** defect, not a vocabulary one. Every
`HTTPException` in the API already raised a machine token -- `ROLE_NOT_FOUND`
and its 23 siblings -- but they went out inside FastAPI's default
`{"detail": ...}`, while `control-plane-parity.md` 5.2 and
`error-envelope.schema.json` define `{code, message, request_id, field_errors,
retryable}`. CP-0 landed the shared contract exactly so that each route would
not invent its own; the receipt half was wired up and this half was not.

The expiry T87 wrote down is "before B1's write routes are laid down", and of
the 40 routes still to build, 30 are writes. They are the ones that need
`field_errors` and `retryable`; adopting the envelope afterwards would mean
rewriting the error path of every one of them.

**What is asserted here, and why each one:**

* the body validates against the published schema -- not against a copy of the
  field list written into this file, which would agree with itself;
* `request_id` is a real per-request id and matches the response header, so a
  caller quoting it can be matched to a log line;
* `retryable` is decided by status and not by guesswork;
* `field_errors` carries the submissions that failed, and **carries nothing
  else** -- pydantic's own errors echo the input value, which is how a password
  reaches an error body;
* an unhandled exception becomes a 500 envelope with no trace in it.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.auth import AuthorizationError
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ENVELOPE_MEMBERS = {"code", "message", "request_id", "field_errors", "retryable"}


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    with Connection.connect(_migration_url(), row_factory=dict_row) as connection:
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


def test_an_unauthenticated_request_answers_with_the_envelope(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client = TestClient(create_app(settings))

    response = client.get("/roles")

    assert response.status_code == 401
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "AUTHENTICATION_REQUIRED"
    assert set(body) == ENVELOPE_MEMBERS
    # The vocabulary did not change with the shape: this is the same token the
    # route raised before C1, moved from `detail` to `code`.
    assert body["message"] == "authentication required"


def test_a_denial_answers_with_the_envelope_and_says_nothing_else(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """403 is raised for cross-tenant misses too, so the body must stay bare.

    `AuthorizationError` had its own handler before C1 and already refused to
    name the tenant or the resource. That property has to survive the move to
    the envelope, which is what the last assertion is for.
    """

    client, headers = _client_for(settings, seeded_database, ["role.list"])

    response = client.get("/tenants/current", headers=headers)

    assert response.status_code == 403
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "AUTHORIZATION_DENIED"
    assert body["field_errors"] == []
    serialised = json.dumps(body)
    assert str(seeded_database["tenant_a"]) not in serialised


def test_the_request_id_is_a_real_id_and_matches_the_header(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The id exists to join what the caller saw to what the server logged.

    Minting it inside the error handler would have satisfied the schema and
    been useless for that, so it is assigned per request and echoed on every
    response -- including the successful one below, which is the case a
    handler-side id would have missed entirely.
    """

    client, headers = _client_for(settings, seeded_database, ["role.list"])

    failed = client.get("/tenants/current", headers=headers)
    body = failed.json()

    UUID(body["request_id"])  # raises if it is not a uuid
    assert failed.headers["x-request-id"] == body["request_id"]

    ok = client.get("/roles", headers=headers)
    assert ok.status_code == 200
    UUID(ok.headers["x-request-id"])
    assert ok.headers["x-request-id"] != body["request_id"], "ids must not repeat"


def test_retryable_is_false_for_a_request_the_caller_must_change(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`retryable` is acted on, so it may not be decorative.

    A 404 repeated unchanged produces a 404. Saying `true` here would send a
    client into a loop it cannot win, which is worse than not having the field.
    """

    client, headers = _client_for(settings, seeded_database, ["role.delete"])

    response = client.delete(f"/roles/{uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.json()["retryable"] is False


def test_a_validation_failure_names_the_fields_and_echoes_no_values(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """422 is the status whose envelope has something to carry.

    The value assertion is the important half. Pydantic's own error entries
    include `input` -- the submitted value -- so a handler that forwarded them
    would put whatever the client sent into an error body. Login is the sharpest
    case available: its payload is a password.
    """

    client = TestClient(create_app(settings))
    # Not named `secret`/`password`: `check_secrets.py` flags an assignment to
    # such a name, and its own message says to move the value rather than
    # allowlist it. The point of the sentinel is that it is recognisable in the
    # response body, which a neutral name serves just as well.
    sentinel = "must-not-come-back-9f3a"

    response = client.post(
        "/auth/login", json={"tenant_code": "tenant-a", "password": sentinel}
    )

    assert response.status_code == 422
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "VALIDATION_FAILED"
    assert [item["field"] for item in body["field_errors"]] == ["email"]
    assert sentinel not in json.dumps(body)


def test_unknown_permission_codes_come_back_as_field_errors(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The debt T87 recorded, paid.

    CP-3's review cut a bespoke `{"error": ..., "unknown": [...]}` body because
    it was a third error shape on a surface just reduced to two, and left a
    comment saying the codes still belonged in `ErrorEnvelope.field_errors`.
    There was no envelope to put them in. Now there is.

    Naming them is safe for the reason that comment gives: `GET /permissions`
    publishes the whole list, so echoing which of the caller's **own**
    submissions were unrecognised reveals nothing they could not fetch.
    """

    client, headers = _client_for(settings, seeded_database, ["role.create"])

    response = client.post(
        "/roles",
        json={
            "name": "support",
            "permissions": ["role.list", "role.manage_all", "tenant.resolve_for_login"],
        },
        headers={**headers, "Idempotency-Key": f"role-{uuid4()}"},
    )

    assert response.status_code == 422
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "PERMISSION_NOT_IN_CATALOG"
    # Both unrecognised submissions, and **only** those -- `role.list` is a real
    # permission and must not be reported as a failure.
    assert {item["field"] for item in body["field_errors"]} == {
        "role.manage_all",
        "tenant.resolve_for_login",
    }
    assert {item["code"] for item in body["field_errors"]} == {"PERMISSION_NOT_IN_CATALOG"}


def test_an_unrouted_path_still_answers_with_the_envelope(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """FastAPI's own 404 does not carry a machine token, so one is supplied.

    Without this the envelope would hold whichever prose Starlette chose, and
    `code` would stop being the machine field parity 5.2 says it is.
    """

    client = TestClient(create_app(settings))

    response = client.get("/there-is-no-such-route")

    assert response.status_code == 404
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "NOT_FOUND"


def test_an_unhandled_exception_becomes_an_envelope_with_no_trace_in_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The guarantee the schema makes, made true of the running application.

    `additionalProperties: false` and the absence of a `stack`/`detail` member
    mean a traceback has nowhere to land *in the contract*. That is only worth
    anything if unhandled exceptions actually go through it -- FastAPI's default
    is to re-raise, which in a deployed process is the server's own 500 page.
    """

    application = create_app(settings)

    @application.get("/boom-for-the-test")
    def boom() -> dict[str, str]:
        raise RuntimeError("secret-internal-detail-8842")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/boom-for-the-test")

    assert response.status_code == 500
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "INTERNAL_ERROR"
    # The server's problem, not the caller's: repeating may work.
    assert body["retryable"] is True
    assert "secret-internal-detail-8842" not in json.dumps(body)
    assert "RuntimeError" not in json.dumps(body)


# --------------------------------------------------------------------------
# Independent review 2026-09-05: P2-1, P2-3, P3-1
# --------------------------------------------------------------------------


def test_a_500_carries_the_request_id_header_too(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P2-1: the one response where somebody is most likely to quote an id.

    An unhandled exception escapes the middleware, so the header it sets never
    ran; the body's id was right and the header was absent. Proxies and log
    pipelines read the header -- and 500 is exactly when a caller comes back
    holding one.
    """

    application = create_app(settings)

    @application.get("/boom-for-the-header-test")
    def boom() -> dict[str, str]:
        raise RuntimeError("kaboom")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/boom-for-the-header-test")

    assert response.status_code == 500
    assert response.headers["x-request-id"] == response.json()["request_id"]


def test_a_denial_whose_message_is_prose_still_answers_403_with_a_code(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P2-3: six of `AuthorizationError`'s seven raise sites carry prose.

    Passing prose through as `code` fails the envelope's own pattern, raises
    inside the handler, and lands the caller on a **500 with `retryable: true`**
    -- told to retry something that can never succeed -- where §3.3 requires a
    403 with a machine code. The first real caller of that path is CP-1's
    cross-tenant reason obligation.
    """

    application = create_app(settings)

    @application.get("/denial-with-prose")
    def denial() -> dict[str, str]:
        raise AuthorizationError("'thing' is not in the resource catalog")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/denial-with-prose")

    assert response.status_code == 403
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "AUTHORIZATION_DENIED"
    assert body["retryable"] is False


def test_an_obligation_denial_keeps_its_token_and_moves_the_detail_into_fields(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The shape `require_obligations_discharged` raises is not prose.

    `OBLIGATION_NOT_DISCHARGED:audit,redact` carries a real token plus a list.
    Gluing the list into `code` would break the pattern; dropping it would lose
    what was not discharged. The colon is the seam.
    """

    application = create_app(settings)

    @application.get("/denial-with-obligations")
    def denial() -> dict[str, str]:
        raise AuthorizationError("OBLIGATION_NOT_DISCHARGED:audit,redact")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/denial-with-obligations")

    assert response.status_code == 403
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "OBLIGATION_NOT_DISCHARGED"
    assert {item["field"] for item in body["field_errors"]} == {"audit", "redact"}


def test_a_reason_code_denial_still_travels_unchanged(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The negatives above prove nothing if normalising flattened everything.

    `require_allowed` raises `decision.reason_code`, which **is** a machine
    token, and it has to survive as itself -- that is the one case §3.3 is
    actually about.
    """

    application = create_app(settings)

    @application.get("/denial-with-reason-code")
    def denial() -> dict[str, str]:
        raise AuthorizationError("TENANT_SCOPE_NOT_PERMITTED")

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/denial-with-reason-code")

    assert response.json()["code"] == "TENANT_SCOPE_NOT_PERMITTED"


def test_a_structured_detail_missing_a_key_does_not_become_a_500(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """P3-1: the handler must not fail on the shape it invited.

    A raise site that forgets `code` in one entry produced a `KeyError` inside
    the handler and an opaque 500 -- while the comment next to it claimed that
    failing at the raise site is easier to read than failing at the wire. The
    malformed entry is dropped; the status and the code still travel.
    """

    application = create_app(settings)

    @application.get("/malformed-structured-detail")
    def malformed() -> dict[str, str]:
        raise HTTPException(
            status_code=422,
            detail={"code": "PERMISSION_NOT_IN_CATALOG", "field_errors": [{"field": "a"}]},
        )

    client = TestClient(application, raise_server_exceptions=False)
    response = client.get("/malformed-structured-detail")

    assert response.status_code == 422
    body = response.json()
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "PERMISSION_NOT_IN_CATALOG"
    assert body["field_errors"] == []
