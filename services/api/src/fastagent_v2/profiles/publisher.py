"""Turning a trusted package into an immutable release row.

This is the production write path that `profile_releases` did not have: before
this module the only INSERT into the table in the whole repository lived in
`tests/conftest.py`.

The publish is idempotent by content and refuses by identity:

* same profile_id/version, same content    -> no-op, `created=False`;
* same profile_id/version, different bytes -> `ProfileImmutabilityError`;
* reused release id, anything else changed -> `ProfileImmutabilityError`.

There is no separate same-content-different-identity check: the content hash
covers `identity`, so identical bytes always arrive as the same profile_id and
version. `profile_releases` is unique on (tenant_id, content_hash) regardless.

Rebinding a channel to a different release is a separate operation and is NOT
done here: RFC §9.1 requires a repin to bump the aggregate generation, repair the
target stack and invalidate pending events. Publishing a release is safe on its
own precisely because nothing starts using it until something binds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.profiles.protocol import (
    ProfileImmutabilityError,
    ProfilePackage,
    ProfileRelease,
)
from fastagent_v2.profiles.resolver import release_from_row
from fastagent_v2.runtime.db import Database
from psycopg import Connection
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class PublishResult:
    release: ProfileRelease
    #: False when the release was already published with identical content.
    created: bool


def publish_release(
    *,
    database: Database,
    tenant_id: UUID,
    package: ProfilePackage,
    published_by: UUID | None = None,
    correlation_id: UUID | None = None,
) -> PublishResult:
    """Publish one trusted package for one tenant, idempotently."""

    correlation = correlation_id or uuid4()
    with database.transaction(
        tenant_id=tenant_id,
        subject_id=published_by,
        correlation_id=correlation,
    ) as connection:
        existing = connection.execute(
            """
            SELECT tenant_id, id, profile_id, version, content_hash, profile
              FROM profile_releases
             WHERE tenant_id = %s
               AND ((profile_id = %s AND version = %s) OR id = %s)
            """,
            (tenant_id, package.profile_id, package.version, package.release_id),
        ).fetchall()
        for row in existing:
            release = release_from_row(row)
            if (
                release.release_id == package.release_id
                and release.profile_id == package.profile_id
                and release.version == package.version
                and release.content_hash == package.content_hash
            ):
                _audit(
                    connection=connection,
                    tenant_id=tenant_id,
                    actor_id=published_by,
                    correlation_id=correlation,
                    package=package,
                    reason_code="PROFILE_RELEASE_ALREADY_PUBLISHED",
                )
                return PublishResult(release=release, created=False)
            raise ProfileImmutabilityError(
                f"publishing {package.profile_id} {package.version} "
                f"({package.content_hash}) would change published release "
                f"{release.release_id}: it is {release.profile_id} {release.version} "
                f"({release.content_hash}). Publish a new version instead."
            )

        connection.execute(
            """
            INSERT INTO profile_releases(
                tenant_id, id, profile_id, version, content_hash, profile,
                published_by, source_path
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                package.release_id,
                package.profile_id,
                package.version,
                package.content_hash,
                Jsonb(package.document),
                published_by,
                package.source_path,
            ),
        )
        _audit(
            connection=connection,
            tenant_id=tenant_id,
            actor_id=published_by,
            correlation_id=correlation,
            package=package,
            reason_code="PROFILE_RELEASE_PUBLISHED",
        )
        row = connection.execute(
            """
            SELECT tenant_id, id, profile_id, version, content_hash, profile
              FROM profile_releases WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, package.release_id),
        ).fetchone()
    if row is None:  # pragma: no cover - the INSERT above is in the same transaction
        raise ProfileImmutabilityError("published release disappeared before it could be read")
    return PublishResult(release=release_from_row(row), created=True)


def _audit(
    *,
    connection: Connection[Any],
    tenant_id: UUID,
    actor_id: UUID | None,
    correlation_id: UUID,
    package: ProfilePackage,
    reason_code: str,
) -> None:
    """Record the publish attempt.

    The evidence carries identity and provenance only. The profile document
    itself is customer-facing material and already stored once; copying it into
    the audit log would duplicate it into a table nothing redacts.
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, 'profile_release.create', 'profile_release', %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            actor_id,
            package.release_id,
            correlation_id,
            reason_code,
            Jsonb(
                {
                    "profile_id": package.profile_id,
                    "version": package.version,
                    "content_hash": package.content_hash,
                    "source_path": package.source_path,
                }
            ),
        ),
    )
