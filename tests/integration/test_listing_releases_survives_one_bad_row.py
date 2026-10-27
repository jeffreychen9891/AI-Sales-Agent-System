"""一行今天读不出来的 release，**不该让运维连好的那几版也看不见**。

## 这份文件钉的是什么

2026-09-16 实测：stage 上两行存量 release 的 `target_frames[0].goal`
超了 `maxLength 1200`（合同后来收紧过，而 `profile_releases` 是 append-only，
`006` 的触发器对 UPDATE/DELETE 无条件抛 —— 存进去的读不出来了也删不掉），
于是 `python -m fastagent_v2.profiles.cli show --tenant <真租户>` **整条命令抛**，
而它正是「现在钉的是哪一版」唯一的查法。

**这不是一次性的**：合同每收紧一次就会再产生一批这样的行。

## 三条各守一半，**第三条是最要紧的那条**

1. 列表不抛，且坏行**仍然出现在结果里**（跳过它等于用一个更小的谎言换掉崩溃）；
2. 降级理由里**没有正文** —— 逐段比对，不是「`message` 不在里面」；
3. **单版那条路照旧抛。** 这一条守的是「我没有顺手把每回合那道闸也放宽」——
   `release()` / `pinned()` / 重钉 / 发布四个调用方都要拿这一版去**用**，
   而一版过不了合同却被用下去，正是那几道闸拦的事。
   **有人把容错挪进 `release_from_row` 的那天，红的是它。**
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.profiles import ProfileRegistry, ProfileResolver, canonical_hash
from fastagent_v2.profiles.protocol import ProfilePackage
from fastagent_v2.profiles.resolver import UnreadableRelease
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from jsonschema.exceptions import ValidationError
from psycopg import Connection
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration]

#: 一段**能认出来**的假「商家话术」。判据是它的片段一个都不许出现在理由里，
#: 所以它必须足够长（越过 `maxLength`）且内容独特。
FAKE_GOAL = "商家的话术在这里" * 200


def _package() -> ProfilePackage:
    return ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    )


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"])


def _insert_row_that_fails_todays_contract(tenant_id: UUID) -> UUID:
    """按迁移角色塞一行过不了合同的 release，**这正是存量那两行的来路**。

    合同收紧之后，库里原来合规的行就变成了这个样子 —— 用迁移角色直接写，
    是因为发布路径会拒绝它（那条路是对的，这里要造的是**已经在库里**的状态）。
    """

    release_id = uuid4()
    document = copy.deepcopy(_package().document)
    document["identity"]["release_id"] = str(release_id)
    document["identity"]["version"] = "9.9.9"
    document["target_frames"][0]["goal"] = FAKE_GOAL
    document["identity"]["content_hash"] = canonical_hash(document)

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO profile_releases(
                tenant_id, id, profile_id, version, content_hash, profile
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                release_id,
                document["identity"]["profile_id"],
                "9.9.9",
                document["identity"]["content_hash"],
                Jsonb(document),
            ),
        )
        connection.commit()
    return release_id


def test_one_unreadable_row_does_not_hide_the_readable_ones(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """列表照常返回，坏行降级成一条记录**留在结果里**。"""

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    resolver = ProfileResolver(database=Database(settings.database_url))
    before = resolver.releases(tenant_id=tenant_a)

    bad_id = _insert_row_that_fails_todays_contract(tenant_a)

    after = resolver.releases(tenant_id=tenant_a)
    assert len(after) == len(before) + 1, (
        "坏行被跳过了 —— 运维数「有几版」时看见的条数就和库里对不上，"
        "而那是他判断「是不是漏了一版」的唯一依据。"
    )

    degraded = [item for item in after if isinstance(item, UnreadableRelease)]
    assert [item.release_id for item in degraded] == [bad_id]
    assert degraded[0].version == "9.9.9", "降级记录要说得出是哪一版"

    readable = [item for item in after if not isinstance(item, UnreadableRelease)]
    assert len(readable) == len(before), "好的那几版一版都不能少"


def test_the_degraded_reason_carries_no_material_text(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """理由里**没有正文** —— 逐段比对，不是「`message` 不在里面」。

    **为什么要逐段**：上一次这个洞就是靠「截断」以为堵上了
    （`error.message[:160]`），而 jsonschema 在 `maxLength` 上的消息体
    就是那个超长的值本身 —— **截掉的是尾巴，留下的是正文**。
    所以判据是「原文的任何一段都不在理由里」，一个「不含某个整串」的断言
    对截断式泄漏是看不见的。
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    _insert_row_that_fails_todays_contract(tenant_a)

    resolver = ProfileResolver(database=Database(settings.database_url))
    degraded = [
        item
        for item in resolver.releases(tenant_id=tenant_a)
        if isinstance(item, UnreadableRelease)
    ]
    assert degraded, "没造出降级行，这条用例什么都没验"
    reason = degraded[0].reason

    window = 12
    leaked = [
        FAKE_GOAL[index : index + window]
        for index in range(0, len(FAKE_GOAL) - window, window)
        if FAKE_GOAL[index : index + window] in reason
    ]
    assert not leaked, f"理由里漏出了正文片段：{leaked[:3]}"

    # **同时要真的说清了事** —— 一个什么都不说的理由也能通过上面那条。
    assert "maxLength" in reason, f"理由没说是哪条约束：{reason}"
    assert "goal" in reason, f"理由没说是哪一格：{reason}"


def test_loading_one_release_still_refuses_a_row_that_fails_the_contract(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**单版那条路照旧抛。**

    这是本文件最要紧的一条：`releases()` 的容错是**只给列表的**。
    `release()` / `pinned()`（每次装载、每个入站回合）· 重钉 · 发布 ——
    四个调用方都要拿这一版去**用**。
    **有人把那段 `try` 挪进 `release_from_row` 的那天，红的是这一条。**
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    bad_id = _insert_row_that_fails_todays_contract(tenant_a)

    resolver = ProfileResolver(database=Database(settings.database_url))
    with pytest.raises(ValidationError):
        resolver.release(tenant_id=tenant_a, release_id=bad_id)
