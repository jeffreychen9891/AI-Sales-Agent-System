"""Reading a published release back out of the database.

The resolver is the only supported way to turn a release identity into a profile
document. It verifies immutability on the way out: a stored release that no
longer hashes to its recorded content hash is refused, not returned. Evidence
recorded against a release id is only worth anything if that id still resolves
to the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from fastagent_v2.profiles.protocol import (
    ProfileImmutabilityError,
    ProfileNotFound,
    ProfileRelease,
)
from fastagent_v2.profiles.registry import canonical_hash
from fastagent_v2.runtime.contracts import describe_validation_error, validate_contract
from fastagent_v2.runtime.db import Database
from jsonschema.exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class UnreadableRelease:
    """一行今天读不出来的 `profile_releases`，**说清是哪一行、为什么，不带正文**。

    它只从 `ProfileResolver.releases()` 出来，**别处一律照旧硬失败** ——
    见那个方法的 docstring。

    `reason` 走 `describe_validation_error()` 的正向白名单，
    **不是 `str(error)`**：jsonschema 在 `maxLength` 上的消息体就是那个超长的值
    本身，而这里那个值是商家的话术。
    """

    tenant_id: UUID
    release_id: UUID
    profile_id: str
    version: str
    content_hash: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProfileResolver:
    """Resolves published releases for one database role.

    Every query is tenant-scoped through `Database.transaction`, so RLS -- not a
    WHERE clause the caller has to remember -- is what keeps one tenant's
    releases out of another's answers.
    """

    database: Database

    def release(self, *, tenant_id: UUID, release_id: UUID) -> ProfileRelease:
        with self.database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT tenant_id, id, profile_id, version, content_hash, profile
                  FROM profile_releases WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, release_id),
            ).fetchone()
        if row is None:
            raise ProfileNotFound(f"no profile release {release_id} for this tenant")
        return release_from_row(row)

    def release_for_version(
        self, *, tenant_id: UUID, profile_id: str, version: str
    ) -> ProfileRelease:
        with self.database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT tenant_id, id, profile_id, version, content_hash, profile
                  FROM profile_releases
                 WHERE tenant_id = %s AND profile_id = %s AND version = %s
                """,
                (tenant_id, profile_id, version),
            ).fetchone()
        if row is None:
            raise ProfileNotFound(
                f"no profile release for {profile_id} {version} for this tenant"
            )
        return release_from_row(row)

    def release_summaries(
        self, *, tenant_id: UUID, limit: int
    ) -> tuple[tuple[dict[str, Any], ...], bool]:
        """Metadata for a directory page, and whether there is more than `limit`.

        `releases()` selects the whole `profile` column and then runs every row
        through `release_from_row`, which validates it against the schema and
        recomputes its sha256. For a page that prints four fields per row, that
        is the entire corpus read, parsed and hashed to be thrown away -- and
        worse, one row whose stored hash has drifted raises
        `ProfileImmutabilityError` and takes the *whole page* down for that
        tenant (independent review 2026-09-04, P2-5). Integrity checking belongs
        to the path that hands out a document, not to the one that lists names.

        `limit + 1` rows are fetched so the caller can tell "exactly a pageful"
        from "more than a pageful" without a second query. The flag is returned
        rather than swallowed because `Page<T>` may not claim `has_more: false`
        when it does not know (P2-6).
        """

        with self.database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, id, profile_id, version, content_hash
                  FROM profile_releases
                 WHERE tenant_id = %s ORDER BY profile_id, version
                 LIMIT %s
                """,
                (tenant_id, limit + 1),
            ).fetchall()
        return tuple(rows[:limit]), len(rows) > limit

    def releases(
        self, *, tenant_id: UUID
    ) -> tuple[ProfileRelease | UnreadableRelease, ...]:
        """这个租户的每一版，**读不出来的那几行降级成一条记录而不是抛**。

        ## 为什么只有这一个方法这样

        `release_from_row` 的另外四个调用方 —— `release()` / `pinned()`
        （每次装载、每个入站回合）· `binding.repin_channel_binding` ·
        `publisher.publish_release` —— **一律照旧硬失败**，那几道闸是要的：
        它们各自要拿这一版去**用**，而一版过不了合同却被用下去，
        正是那几道闸拦的事。

        这个方法不一样：它是**运维在问「这个租户有哪些版」**
        （`profiles/cli.py` 的 `show`）。一行坏数据让整条列表抛，
        换来的不是安全，是**运维连好的那几版也看不见** ——
        2026-09-16 实测：stage 上两行存量 release（`goal` 超 `maxLength 1200`，
        合同后来收紧过）让 `show` 对那个真租户直接崩，
        而它正是「现在钉的是哪一版」唯一的查法。

        ## 这不是一次性的

        **合同每收紧一次，就会再产生一批这样的行**（`outcome_rules` 那次就是
        追溯性收紧），而 `profile_releases` 是 append-only（`006` 的触发器对
        UPDATE/DELETE 无条件抛）—— 已存进去的读不出来了也删不掉。
        所以这里治的是一类，不是那两行。

        **顺序保持不变**：坏行留在它原来的位置上，不排到末尾、不跳过 ——
        运维数「有几版」时，看见的条数要和库里一致。
        """

        with self.database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT tenant_id, id, profile_id, version, content_hash, profile
                  FROM profile_releases
                 WHERE tenant_id = %s ORDER BY profile_id, version
                """,
                (tenant_id,),
            ).fetchall()

        listed: list[ProfileRelease | UnreadableRelease] = []
        for row in rows:
            try:
                listed.append(release_from_row(row))
            except ValidationError as error:
                listed.append(
                    UnreadableRelease(
                        tenant_id=UUID(str(row["tenant_id"])),
                        release_id=UUID(str(row["id"])),
                        profile_id=str(row["profile_id"]),
                        version=str(row["version"]),
                        content_hash=str(row["content_hash"]),
                        reason=describe_validation_error(error),
                    )
                )
            except ProfileImmutabilityError as error:
                # **哈希对不上也降级，但理由分开写。** 这一类与合同收紧不同源：
                # 它说的是这一行**被改过**，而那是审计问题不是合同问题。
                # 它照样不该让整条列表抛，否则同样是「一行坏数据遮住其余全部」。
                listed.append(
                    UnreadableRelease(
                        tenant_id=UUID(str(row["tenant_id"])),
                        release_id=UUID(str(row["id"])),
                        profile_id=str(row["profile_id"]),
                        version=str(row["version"]),
                        content_hash=str(row["content_hash"]),
                        reason=f"immutability: {error}",
                    )
                )
        return tuple(listed)


def release_from_row(row: dict[str, Any]) -> ProfileRelease:
    document = cast(dict[str, Any], row["profile"])
    validate_contract("merchant-profile", document)
    stored_hash = str(row["content_hash"])
    calculated = canonical_hash(document)
    if calculated != stored_hash:
        raise ProfileImmutabilityError(
            f"stored profile release {row['id']} no longer hashes to its recorded "
            f"content hash: recorded {stored_hash}, stored document hashes to {calculated}"
        )
    declared = str(document["identity"]["content_hash"])
    if declared != stored_hash:
        raise ProfileImmutabilityError(
            f"stored profile release {row['id']} disagrees with its own identity: "
            f"row records {stored_hash}, document declares {declared}"
        )
    return ProfileRelease(
        tenant_id=UUID(str(row["tenant_id"])),
        release_id=UUID(str(row["id"])),
        profile_id=str(row["profile_id"]),
        version=str(row["version"]),
        content_hash=stored_hash,
        document=document,
    )
