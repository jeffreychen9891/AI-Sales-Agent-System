"""CP-2 first cluster: the three profile routes (route freeze B1 #27–#29).

The publishing logic is T10's and already had tests; what is new here is the
HTTP surface, so what is asserted here is what the surface adds: authorization,
tenant scope, the `Page` shape, the idempotency-key requirement, and the two
things the route must never emit -- a profile document in a list, and a
filesystem path in an error.

`POST /profiles/releases` is the route the CLI's own docstring said it was
waiting for: "adding a route ahead of that freeze would decide by accident what
the freeze is supposed to decide". The freeze (T9) is done, so the route exists,
and it publishes only from the trusted registry that ships in the image.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.types.json import Jsonb

CARRIER = {"profile_id": "synthetic-two-axis-desk", "version": "1.0.3"}
SEEDED = {"profile_id": "synthetic-service-desk", "version": "1.0.4"}
IDEMPOTENCY = {"Idempotency-Key": "0123456789abcdef"}


def _grant(*, tenant_id: UUID, employee_id: UUID, permissions: list[str]) -> None:
    """Give one seeded operator the permissions a case needs, before login.

    The token carries the permissions read at login time, so this has to happen
    first. Done per case rather than in the shared fixture on purpose: two of
    the cases below are about being **refused**, and a fixture that granted
    everything to everyone would quietly delete them.
    """

    url = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not url:
        pytest.skip("integration database is not configured")
    with Connection.connect(url) as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), tenant_id, employee_id),
        )
        connection.commit()


def _login(client: TestClient, seeded: dict[str, object], *, tenant: str = "a") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={
            "tenant_code": f"tenant-{tenant}",
            "email": f"operator-{tenant}@example.com",
            "password": seeded["login_password"],
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _client_for(
    settings: Settings,
    seeded: dict[str, object],
    permissions: list[str],
    *,
    tenant: str = "a",
) -> tuple[TestClient, dict[str, str]]:
    _grant(
        tenant_id=UUID(str(seeded[f"tenant_{tenant}"])),
        employee_id=UUID(str(seeded[f"employee_{tenant}"])),
        permissions=permissions,
    )
    client = TestClient(create_app(settings))
    return client, _login(client, seeded, tenant=tenant)


def test_the_release_list_is_a_page_and_carries_no_document(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Shape first: `Page<T>` (parity 5.2), and metadata only.

    The document is the whole merchant profile. A list that returned it would
    hand every reader the prompts and materials of a tenant on a route whose
    purpose is "what is published here", and it would quietly answer a question
    the freeze did not grant a route for (single-release read).
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])

    response = client.get("/profiles/releases", headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store, private"
    body = response.json()
    validate_control_plane_contract("page", body)
    assert body["has_more"] is False and body["next_cursor"] is None
    assert [item["profile_id"] for item in body["items"]] == [SEEDED["profile_id"]]
    item = body["items"][0]
    assert set(item) == {"release_id", "profile_id", "version", "content_hash"}
    assert str(seeded_database["profile_release"]) == item["release_id"]


def test_the_release_projection_has_a_fixed_key_set(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Pinned, because the interesting failure is a key that gets *added*.

    `_release_projection` decides what a release looks like on the wire, and the
    key that must never appear there is the profile document itself. A test that
    only checked the four expected keys were present would pass the day someone
    adds a fifth.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])

    item = client.get("/profiles/releases", headers=headers).json()["items"][0]

    assert set(item) == {"release_id", "profile_id", "version", "content_hash"}
    assert "document" not in item and "profile" not in item


def test_publishing_answers_with_a_command_receipt(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Every create command on this surface returns a `CommandReceipt` (ruling, 2026-09-04).

    The first version returned a domain body with a bespoke `created` boolean.
    parity §5.2 only *mandates* a receipt for six kinds of command and profile
    publishing is not among them -- which is precisely why it mattered: this is
    the first create route, and whatever it returned was going to be copied by
    the ~70 that follow (independent review 2026-09-04, P2-4).

    `status` is `succeeded` rather than `accepted`: the release is durable
    before the response is written, and `accepted` means "will be worked".
    """

    client, headers = _client_for(
        settings, seeded_database, ["profile_release.read", "profile_release.create"]
    )

    first = client.post("/profiles/releases", headers={**headers, **IDEMPOTENCY}, json=CARRIER)

    assert first.status_code == 201, first.text
    body = first.json()
    validate_control_plane_contract("command-receipt", body)
    assert body["status"] == "succeeded"
    assert body["resource_ref"]["resource_type"] == "profile_release"
    assert body["resource_ref"]["tenant_id"] == str(seeded_database["tenant_a"])
    assert body["idempotency_key"] == IDEMPOTENCY["Idempotency-Key"]
    assert first.headers["cache-control"] == "no-store, private"
    # The document, and anything derived from it, stays out of a command receipt.
    assert "profile" not in body and "content_hash" not in body


def test_publishing_twice_is_the_same_release_and_the_status_code_says_so(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """201 the first time, 200 the second, and the second is not a new release.

    The `created` boolean did not survive the move to `CommandReceipt` -- the
    status enum has no word for "already existed" -- so the distinction is
    carried by the status code, which is where HTTP puts it. Worth a test of its
    own: a client retrying a publish has to be able to tell whether its retry
    did anything, and that answer moved.
    """

    client, headers = _client_for(
        settings, seeded_database, ["profile_release.read", "profile_release.create"]
    )

    first = client.post("/profiles/releases", headers={**headers, **IDEMPOTENCY}, json=CARRIER)
    assert first.status_code == 201, first.text

    second = client.post("/profiles/releases", headers={**headers, **IDEMPOTENCY}, json=CARRIER)
    assert second.status_code == 200, second.text
    assert (
        second.json()["resource_ref"]["resource_id"]
        == first.json()["resource_ref"]["resource_id"]
    ), "a repeat publish must name the release that already exists, not a new one"

    listed = client.get("/profiles/releases", headers=headers).json()["items"]
    assert sorted(item["profile_id"] for item in listed) == sorted(
        [CARRIER["profile_id"], SEEDED["profile_id"]]
    )


def test_a_publish_without_an_idempotency_key_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Parity 5.2 requires the header on create commands, so its absence is a 422.

    The route's own docstring is explicit that the header is not what makes the
    route idempotent; requiring it is still the contract, and a contract nobody
    enforces is a contract nobody keeps.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.create"])

    response = client.post("/profiles/releases", headers=headers, json=CARRIER)

    assert response.status_code == 422, response.text


def test_an_untrusted_package_is_refused_without_echoing_anything_about_it(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """A client may name a package; it may not learn anything by naming one.

    The registry's own errors carry filesystem paths -- that is right for an
    operator reading a CLI failure and wrong for an HTTP body, so the route
    answers with a code and nothing else.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.create"])

    response = client.post(
        "/profiles/releases",
        headers={**headers, **IDEMPOTENCY},
        json={"profile_id": "not-a-real-package", "version": "9.9.9"},
    )

    assert response.status_code == 422
    body = response.json()
    # C1: the body is an `ErrorEnvelope` now, so "a code and nothing else" is
    # asserted as "the envelope's five members and nothing else" -- the schema's
    # `additionalProperties: false` leaves a filesystem path nowhere to land,
    # and this pins that the route did not smuggle one into `message` either.
    validate_control_plane_contract("error-envelope", body)
    assert body["code"] == "PROFILE_PACKAGE_NOT_TRUSTED"
    assert set(body) == {"code", "message", "request_id", "field_errors", "retryable"}
    assert "not-a-real-package" not in json.dumps(body)
    assert "profiles" not in json.dumps(body)


def test_reading_releases_does_not_grant_publishing_them(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Two actions, two permissions -- the reason the freeze split this route in two.

    `C0A-012` was one raw-prompt PUT. Anyone who could read the prompt could
    replace it, because there was one permission behind both.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])

    response = client.post("/profiles/releases", headers={**headers, **IDEMPOTENCY}, json=CARRIER)

    assert response.status_code == 403
    assert "profile_release.create" not in response.text


def test_one_tenant_cannot_see_what_another_published(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """RLS, asserted through the route rather than through the repository.

    Both tenants are seeded with the same package, so a test that only compared
    release ids would pass with the isolation broken -- the ids are
    content-addressed and identical. Publishing a *second* package to tenant A
    only is what makes the two lists distinguishable.
    """

    publisher, publisher_headers = _client_for(
        settings, seeded_database, ["profile_release.read", "profile_release.create"]
    )
    assert (
        publisher.post(
            "/profiles/releases", headers={**publisher_headers, **IDEMPOTENCY}, json=CARRIER
        ).status_code
        == 201
    )

    other, other_headers = _client_for(
        settings, seeded_database, ["profile_release.read"], tenant="b"
    )
    listed = other.get("/profiles/releases", headers=other_headers).json()["items"]

    assert [item["profile_id"] for item in listed] == [SEEDED["profile_id"]]


def test_the_schema_route_answers_with_the_contract_and_the_catalogue(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """What a caller needs to name a package, and nothing that smuggles one in.

    Platform-scoped: the schema and the shipped packages are the same for every
    tenant. It returns identities and content hashes, never a document -- the
    freeze's wording for this route is "no free-prompt back door", and a route
    that echoed package bodies would be one.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_schema.read"])

    response = client.get("/profiles/schema", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"]["title"] or body["schema"]["$id"]
    identities = {(item["profile_id"], item["version"]) for item in body["packages"]}
    assert (CARRIER["profile_id"], CARRIER["version"]) in identities
    assert (SEEDED["profile_id"], SEEDED["version"]) in identities
    for item in body["packages"]:
        assert set(item) == {"profile_id", "version", "content_hash"}


def test_the_schema_route_still_requires_a_permission(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Platform-scoped is not public.

    The schema says what a merchant profile may contain -- prompt slots, policy
    hooks, time rules. That is the platform's design surface, and the route sits
    behind a permission like every other authenticated route on it.
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])

    assert client.get("/profiles/schema", headers=headers).status_code == 403
