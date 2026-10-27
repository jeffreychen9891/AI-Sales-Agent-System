"""Operator entry point for the profile publish path.

`publish` is deliberately a CLI and not an HTTP route. RFC §9.1 puts publishing
behind review and a Stage-then-Production bind; the control-plane route surface
for M1a has not been frozen yet (backlog T9), and adding a route ahead of that
freeze would decide by accident what the freeze is supposed to decide. The CLI
runs with the API database role, so it inherits the same RLS and the same
privilege split an eventual route would.

    python -m fastagent_v2.profiles.cli list
    python -m fastagent_v2.profiles.cli publish --tenant <uuid> \
        --profile-id synthetic-service-desk --version 1.0.0 [--actor <uuid>]
    python -m fastagent_v2.profiles.cli show --tenant <uuid> [--release <uuid>]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import UUID

from fastagent_v2.profiles.protocol import ProfileError
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.profiles.resolver import ProfileResolver, UnreadableRelease
from fastagent_v2.runtime.contracts import DEPLOYMENT_ROOT
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings

#: Absolute, and from the same source as the trust root it will be checked
#: against. A bare relative `Path("profiles")` resolved against whatever the
#: caller's working directory happened to be, which is a second way for the
#: registry root and the trust root to disagree (independent review 2026-09-04,
#: P1-1).
DEFAULT_ROOT = DEPLOYMENT_ROOT / "profiles"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FastAgent v2 merchant profile registry")
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=(
            "registry root holding <package>/profile.json (default: profiles). "
            "Must be inside this repository -- a root outside it is refused, "
            "not published"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="list trusted profile packages on disk")

    publish = subparsers.add_parser("publish", help="publish one package as an immutable release")
    publish.add_argument("--tenant", type=UUID, required=True)
    publish.add_argument("--profile-id", required=True)
    publish.add_argument("--version", required=True)
    publish.add_argument(
        "--actor", type=UUID, default=None, help="employee id to attribute the publish to"
    )

    show = subparsers.add_parser("show", help="show published releases for a tenant")
    show.add_argument("--tenant", type=UUID, required=True)
    show.add_argument("--release", type=UUID, default=None)
    return parser


def _describe(profile_id: str, version: str, content_hash: str, extra: str) -> str:
    return f"{profile_id} {version} {content_hash[:12]}... {extra}"


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    registry = ProfileRegistry(root=arguments.root)
    try:
        if arguments.command == "list":
            for package in registry.packages():
                print(
                    _describe(
                        package.profile_id,
                        package.version,
                        package.content_hash,
                        package.source_path,
                    )
                )
            return 0

        database = Database(Settings.from_env("api").database_url)
        if arguments.command == "publish":
            package = registry.package(
                profile_id=arguments.profile_id, version=arguments.version
            )
            result = publish_release(
                database=database,
                tenant_id=arguments.tenant,
                package=package,
                published_by=arguments.actor,
            )
            state = "published" if result.created else "already published, unchanged"
            print(
                _describe(
                    result.release.profile_id,
                    result.release.version,
                    result.release.content_hash,
                    f"release={result.release.release_id} {state}",
                )
            )
            return 0

        resolver = ProfileResolver(database=database)
        releases = (
            (resolver.release(tenant_id=arguments.tenant, release_id=arguments.release),)
            if arguments.release is not None
            else resolver.releases(tenant_id=arguments.tenant)
        )
        for release in releases:
            if isinstance(release, UnreadableRelease):
                # **列出来，而不是跳过。** 运维数「有几版」时看见的条数要和库里
                # 一致；一行读不出来就少一行，等于用一个更小的谎言换掉一次崩溃。
                print(
                    _describe(
                        release.profile_id,
                        release.version,
                        release.content_hash,
                        f"release={release.release_id} **今天过不了合同**：{release.reason}",
                    )
                )
                continue
            frames = ",".join(str(frame["id"]) for frame in release.document["target_frames"])
            print(
                _describe(
                    release.profile_id,
                    release.version,
                    release.content_hash,
                    f"release={release.release_id} frames={frames}",
                )
            )
        return 0
    except ProfileError as error:
        # The message names identities and hashes, never profile content.
        print(f"refused: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
