"""Types and refusals shared by the registry, publisher and resolver.

Kept separate from the modules that use them so that a caller can catch a
profile refusal without importing a database dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


class ProfileError(RuntimeError):
    """Base class for every refusal raised by this package."""


class ProfilePackageError(ProfileError):
    """A profile package on disk is not publishable.

    Raised for a document that fails the `merchant-profile` contract, whose
    declared `identity.content_hash` does not match its bytes, or that collides
    with another package on identity.
    """


class ProfileNotFound(ProfileError):
    """Nothing matches the requested profile identity.

    This is the failure that has no fallback. The legacy resolver answered an
    unknown merchant with a default industry profile; v2 refuses, because a
    conversation driven by a profile nobody chose is worse than a conversation
    that does not start (`gen2-program-plan.md` §6, P4).
    """


class ProfileImmutabilityError(ProfileError):
    """A published release would have to change for this operation to succeed.

    Raised both on the way in (republishing an existing profile_id/version with
    different content) and on the way out (a stored release that no longer
    hashes to its recorded content hash).
    """


@dataclass(frozen=True, slots=True)
class ProfilePackage:
    """A validated profile as it exists on disk, before it is published."""

    document: dict[str, Any]
    content_hash: str
    source_path: str

    @property
    def profile_id(self) -> str:
        return str(self.document["identity"]["profile_id"])

    @property
    def version(self) -> str:
        return str(self.document["identity"]["version"])

    @property
    def release_id(self) -> UUID:
        return UUID(str(self.document["identity"]["release_id"]))


@dataclass(frozen=True, slots=True)
class ProfileRelease:
    """A validated profile as it exists in the database: tenant-owned, immutable."""

    tenant_id: UUID
    release_id: UUID
    profile_id: str
    version: str
    content_hash: str
    document: dict[str, Any]
