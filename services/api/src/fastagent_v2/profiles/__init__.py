"""Merchant profile registry, resolver and publish path.

RFC §9.1 gives profiles a controlled hot-publish path:
`draft -> schema/static/golden/simulation -> review -> signed immutable release
-> Stage bind -> user confirm -> Production bind`. This package owns the two
ends of it that the engine depends on:

* the **registry** reads trusted profile packages from disk and refuses anything
  that is not a valid, self-consistent profile;
* the **publisher** turns one such package into an immutable `profile_releases`
  row, idempotently;
* the **resolver** reads a release back by identity, verifying on the way out
  that it still hashes to what was published;
* **binding** points a channel binding at a published release, and -- only when
  asked -- carries live conversations across with the generation bump, stack
  repair, event invalidation and audit RFC §9.1 requires.

There is no default profile and no industry fallback anywhere in this package.
An unresolvable profile raises; it never degrades into a generic one.
"""

from typing import TYPE_CHECKING, Any

from fastagent_v2.profiles.protocol import (
    ProfileError,
    ProfileImmutabilityError,
    ProfileNotFound,
    ProfilePackage,
    ProfilePackageError,
    ProfileRelease,
)
from fastagent_v2.profiles.registry import ProfileRegistry, canonical_hash

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from fastagent_v2.profiles.binding import (
        RepinnedConversation,
        RepinResult,
        repin_channel_binding,
    )
    from fastagent_v2.profiles.publisher import PublishResult, publish_release
    from fastagent_v2.profiles.resolver import ProfileResolver

#: Names re-exported from modules that import `psycopg`, resolved on first use.
_DATABASE_EXPORTS = {
    "ChannelBindingDisabled": "fastagent_v2.profiles.binding",
    "ChannelBindingNotFound": "fastagent_v2.profiles.binding",
    "ProfileReleaseNotFound": "fastagent_v2.profiles.binding",
    "RepinResult": "fastagent_v2.profiles.binding",
    "RepinnedConversation": "fastagent_v2.profiles.binding",
    "repin_channel_binding": "fastagent_v2.profiles.binding",
    "PublishResult": "fastagent_v2.profiles.publisher",
    "publish_release": "fastagent_v2.profiles.publisher",
    "ProfileResolver": "fastagent_v2.profiles.resolver",
}


def __getattr__(name: str) -> Any:
    """Import the database-backed names lazily.

    `protocol.py` says a caller can catch a profile refusal without taking a
    database dependency. Eagerly re-exporting `publisher` and `resolver` here
    made that false -- importing anything from this package pulled in `psycopg`
    (independent review 2026-09-02, P2-4). Deferring the import makes the claim
    true again without changing what the package exports.
    """

    module_name = _DATABASE_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name), name)

__all__ = [
    "ChannelBindingNotFound",
    "ProfileError",
    "ProfileImmutabilityError",
    "ProfileNotFound",
    "ProfilePackage",
    "ProfilePackageError",
    "ProfileRegistry",
    "ProfileRelease",
    "ProfileReleaseNotFound",
    "ProfileResolver",
    "PublishResult",
    "RepinResult",
    "RepinnedConversation",
    "canonical_hash",
    "publish_release",
    "repin_channel_binding",
]
