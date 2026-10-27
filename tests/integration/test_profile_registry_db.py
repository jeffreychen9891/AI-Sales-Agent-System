"""The publish path and the immutability guards, against the real roles.

Everything here runs as `fastagent_app` or `fastagent_worker` -- never as the
migration role -- because the claims being made are about what those roles can
and cannot do.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.profiles import (
    ProfileImmutabilityError,
    ProfileNotFound,
    ProfileRegistry,
    ProfileResolver,
    canonical_hash,
)
from fastagent_v2.profiles.protocol import ProfilePackage
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege, RestrictViolation
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration]


def _package() -> ProfilePackage:
    return ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    )


def _repackage(document: dict[str, Any]) -> ProfilePackage:
    document["identity"]["content_hash"] = canonical_hash(document)
    return ProfilePackage(
        document=document,
        content_hash=str(document["identity"]["content_hash"]),
        source_path="tests/in-memory",
    )


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"])


def test_seeded_release_came_from_the_publish_path_and_is_attributed(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    resolver = ProfileResolver(database=Database(settings.database_url))

    release = resolver.release(tenant_id=tenant_a, release_id=release_id)
    assert release.profile_id == "synthetic-service-desk"
    assert release.content_hash == canonical_hash(release.document)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT source_path FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_a, release_id),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT reason_code, resource_type, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'profile_release.create'
            """,
            (tenant_a,),
        ).fetchall()
    assert row is not None and row[0] == "profiles/synthetic-third/profile.json"
    assert [entry[0] for entry in audit] == ["PROFILE_RELEASE_PUBLISHED"]
    assert audit[0][1] == "profile_release"
    # Identity and provenance only: the profile document is customer-facing
    # material and copying it here would duplicate it into a table nothing
    # redacts.
    assert set(audit[0][2]) == {"profile_id", "version", "content_hash", "source_path"}


def test_republishing_identical_content_is_a_no_op_and_a_changed_one_is_refused(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)

    again = publish_release(database=database, tenant_id=tenant_a, package=_package())
    assert again.created is False
    assert again.release.release_id == UUID(str(seeded_database["profile_release"]))

    changed = copy.deepcopy(_package().document)
    changed["proactivity_policy"]["active_window_seconds"] = 900
    with pytest.raises(ProfileImmutabilityError, match="would change published release"):
        publish_release(database=database, tenant_id=tenant_a, package=_repackage(changed))

    # A new version that reuses the published release id: refused on the id,
    # before it can reach the primary key.
    reused = copy.deepcopy(_package().document)
    reused["identity"]["version"] = "1.0.5"
    # **与 `test_profile_registry.py:293` 同一处形状**（独立审查 2026-09-11，P3-1）：
    # 这里只要一处「改过了」让文档不同，而 `collection:changed` 是个随手写的
    # 两段值，封版时会被三段语法拒掉。今天它不红——`_repackage()` 直接构造
    # `ProfilePackage`，不走 `load_package`，语法闸根本不在这条路上——
    # **而一个「封版时会被拒的值」当 fixture 用，正是新语法要消灭的那件事。**
    reused["knowledge_bindings"] = ["knowledge:changed-collection:v1"]
    with pytest.raises(ProfileImmutabilityError, match="would change published release"):
        publish_release(database=database, tenant_id=tenant_a, package=_repackage(reused))


def test_a_published_release_cannot_be_updated_or_deleted_by_any_runtime_role(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    document = copy.deepcopy(_package().document)
    document["proactivity_policy"]["active_window_seconds"] = 900

    # The API role does not hold UPDATE or DELETE at all.
    app = Database(settings.database_url)
    for statement, parameters in (
        (
            "UPDATE profile_releases SET profile = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(document), tenant_a, release_id),
        ),
        (
            "DELETE FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_a, release_id),
        ),
    ):
        with pytest.raises(InsufficientPrivilege):
            with app.transaction(tenant_id=tenant_a) as connection:
                connection.execute(statement, parameters)

    # And the migration role, which does hold them, is refused by the trigger.
    # Note the name says *runtime* role: a table owner or superuser can still step
    # around a row trigger (TRUNCATE, DISABLE TRIGGER, session_replication_role).
    # Those are documented in 006 and are outside the runtime threat model.
    with pytest.raises(RestrictViolation, match="append-only"):
        with _migration_connection() as connection:
            connection.execute(
                "UPDATE profile_releases SET version = '9.9.9' WHERE id = %s", (release_id,)
            )
    with pytest.raises(RestrictViolation, match="append-only"):
        with _migration_connection() as connection:
            connection.execute("DELETE FROM profile_releases WHERE id = %s", (release_id,))

    survivor = ProfileResolver(database=app).release(
        tenant_id=tenant_a, release_id=release_id
    )
    assert survivor.version == "1.0.4"
    assert survivor.content_hash == _package().content_hash


def test_resolver_is_tenant_scoped_and_has_no_fallback(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    resolver = ProfileResolver(database=Database(settings.database_url))

    # Both tenants published the same package, so the two rows differ only by
    # owner: reading B's release must return B's row, not A's.
    assert resolver.release(tenant_id=tenant_a, release_id=release_id).tenant_id == tenant_a
    assert resolver.release(tenant_id=tenant_b, release_id=release_id).tenant_id == tenant_b
    assert len(resolver.releases(tenant_id=tenant_a)) == 1

    with pytest.raises(ProfileNotFound):
        resolver.release(tenant_id=tenant_a, release_id=uuid4())
    with pytest.raises(ProfileNotFound):
        resolver.release_for_version(
            tenant_id=tenant_a, profile_id="unknown-merchant", version="1.0.0"
        )
    with pytest.raises(ProfileNotFound):
        resolver.release_for_version(
            tenant_id=tenant_a, profile_id="synthetic-service-desk", version="9.9.9"
        )


def test_resolver_refuses_a_release_whose_stored_document_drifted(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """Immutability is enforced on the way out, not only on the way in.

    The drifted row is inserted as a *new* release by the migration role -- the
    published one cannot be edited -- which is the shape a restore from a stale
    dump, or a future migration carrying a data fix, would produce.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    drifted_id = uuid4()
    document = copy.deepcopy(_package().document)
    document["identity"]["release_id"] = str(drifted_id)
    # ⚠️ 在架 third 自缺陷 19 批起就是 1.0.1 —— 夹具发的「新版」要避开它。
    document["identity"]["version"] = "1.0.5"
    document["identity"]["content_hash"] = canonical_hash(document)
    document["proactivity_policy"]["active_window_seconds"] = 900  # drift, after hashing

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO profile_releases(
                tenant_id, id, profile_id, version, content_hash, profile
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_a,
                drifted_id,
                document["identity"]["profile_id"],
                "1.0.5",
                document["identity"]["content_hash"],
                Jsonb(document),
            ),
        )
        connection.commit()

    resolver = ProfileResolver(database=Database(settings.database_url))
    with pytest.raises(ProfileImmutabilityError, match="no longer hashes"):
        resolver.release(tenant_id=tenant_a, release_id=drifted_id)


def test_the_worker_role_can_read_releases_but_not_publish_them(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_a = UUID(str(seeded_database["tenant_a"]))
    worker = Database(settings.worker_database_url)
    release_id = UUID(str(seeded_database["profile_release"]))

    read = ProfileResolver(database=worker).release(tenant_id=tenant_a, release_id=release_id)
    assert read.profile_id == "synthetic-service-desk"

    fresh = copy.deepcopy(_package().document)
    fresh["identity"]["release_id"] = str(uuid4())
    fresh["identity"]["version"] = "2.0.0"
    with pytest.raises(InsufficientPrivilege):
        publish_release(database=worker, tenant_id=tenant_a, package=_repackage(fresh))
