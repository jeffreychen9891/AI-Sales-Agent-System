"""资料的三条控制面 route（freeze B4 #2 / #5 / #6）。

**最值钱的一条是「仍被在架 profile 声明的资料，拒绝归档」。**

裁定五定了「引用在**发送时**解析」。于是归档一份仍被声明的资料
**在归档这一刻不报任何错**——它会在下一次有人触发那一帧、闸去解析那条引用时才炸，
**而那时炸的是一次真实对话**。这道检查把缺口从运行时搬回到调用的那一刻，
**而一个忘了做这道检查的实现，在其余每一条用例里都是绿的。**

**另外两条 route 是纯读**，它们的风险在别处：
跨租户可见性（RLS）与「归档过的还看不看得见」。
"""

from __future__ import annotations

import copy
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.system_roles import system_role_permissions
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

SLUG = "price-list"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings, seeded: dict[str, Any], permissions: list[str]
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(permissions), seeded["tenant_a"], seeded["employee_a"]),
        )
        connection.commit()
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


def _place(
    connection: Connection[Any],
    tenant_id: Any,
    *,
    slug: str = SLUG,
    version: int = 1,
    status: str = "active",
) -> uuid.UUID:
    """人工投放一行——**这是今天唯一把资料放进去的方式**（裁定一）。

    三条 route 没有一条能 `INSERT`，所以用例也只能像投放者那样直接写库。
    """

    material_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO materials(
            tenant_id, id, slug, version, filename, media_type, byte_size,
            content_hash, status, archived_at
        ) VALUES (%s, %s, %s, %s, %s, 'application/pdf', 12345, %s, %s, %s)
        """,
        (
            tenant_id,
            material_id,
            slug,
            version,
            f"{slug}-v{version}.pdf",
            "a" * 64,
            status,
            None if status == "active" else datetime.now(UTC),
        ),
    )
    return material_id


def _declare_in_live_profile(
    connection: Connection[Any], tenant_id: Any, reference: str
) -> uuid.UUID:
    """让**在架** release 声明一条 `asset:` 引用：**发一个新版本，然后重绑**。
    返回新 release 的 id。

    「在架」= 有绑定指着它 **∪** 有非 closed 会话 pin 着它
    （独立审查 2026-09-12 P1-2，用户裁定取并集）。
    这个辅助走的是前一半；**后一半由
    `test_a_release_only_a_live_conversation_still_points_at_counts_as_live`
    用返回的这个 id 自己造**。

    ## 不能改那一份，而这不是本用例的怪癖

    第一版直接 `UPDATE profile_releases SET profile = …`，门禁给的是
    `RestrictViolation: profile_releases is append-only; UPDATE is not
    permitted`，触发器连该怎么做都写在 HINT 里了：
    **「publish a new release and rebind: releases are never rewritten」**。

    **那条约束正是本批那道检查的前提**：一份 release 的内容不会变，
    所以「这份资料还被谁声明着」只会因为**重绑**而改变。
    一个能原地改 release 的世界里，归档前的检查会读到一份随时可能变的快照。

    做法照 `tests/conftest.py` 的 `quiet window` 夹具，逐行同源。
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    row = connection.execute(
        """
        SELECT r.id, r.profile, r.profile_id FROM profile_releases r
          JOIN channel_bindings b
            ON b.tenant_id = r.tenant_id AND b.profile_release_id = r.id
         WHERE r.tenant_id = %s LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None, "播种夹具应当已经有一条指向 release 的绑定"

    document = copy.deepcopy(row["profile"])
    frames = document.get("target_frames") or []
    assert frames, "这份 profile 没有 target_frames，本用例的前提变了"
    frames[0]["content_assets"] = [{"ref": reference}]

    release_id = uuid.uuid4()
    document["identity"]["release_id"] = str(release_id)
    document["identity"]["version"] = "1.0.8"
    connection.execute(
        "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
        "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            release_id,
            row["profile_id"],
            "1.0.8",
            canonical_hash(document),
            Jsonb(document),
        ),
    )
    connection.execute(
        "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
        (release_id, tenant_id),
    )
    return release_id


@pytest.fixture()
def placed(seeded_database: dict[str, Any]) -> Any:
    with _migration_connection() as connection:
        material_id = _place(connection, seeded_database["tenant_a"])
        connection.commit()
        yield material_id


# ---------------------------------------------------------------- 读


def test_the_catalogue_lists_what_was_placed(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    client, headers = _client_for(settings, seeded_database, ["material.list"])
    response = client.get("/materials", headers=headers)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(placed)]
    assert items[0]["reference"] == f"asset:{SLUG}:v1", (
        "返回里要带引用——那才是这份资料在 profile 与提案里被称呼的名字；"
        "拿着 id 去 profile 里搜是搜不到的"
    )


def test_an_archived_version_is_still_listed(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**归档不是隐藏。**

    「这一版从哪一刻起不再能发」是运维会问的问题，而答案在行上；
    把它从默认视图里藏掉，只会让人以为它不存在。
    """

    with _migration_connection() as connection:
        _place(connection, seeded_database["tenant_a"], slug="old-flyer", status="archived")
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.list"])
    slugs = {item["slug"] for item in client.get("/materials", headers=headers).json()["items"]}
    assert "old-flyer" in slugs

    only_active = client.get("/materials?status=active", headers=headers).json()["items"]
    assert "old-flyer" not in {item["slug"] for item in only_active}


def test_a_status_filter_nobody_defined_is_refused_not_silently_empty(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """打错的过滤词返回空页时，读的人会以为这个租户没有资料。"""

    client, headers = _client_for(settings, seeded_database, ["material.list"])
    response = client.get("/materials?status=retired", headers=headers)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_MATERIAL_STATUS", (
        "错误体的机器码在 `code` 上，不在 `detail` 上 —— `_http_error` 把 `detail` "
        "重写成了信封，本文件第一版按 `detail` 断言，门禁给了四条 KeyError"
    )


def test_one_material_reads_back_and_an_unknown_one_is_a_404(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    client, headers = _client_for(settings, seeded_database, ["material.read"])

    found = client.get(f"/materials/{placed}", headers=headers)
    assert found.status_code == 200, found.text
    assert found.json()["slug"] == SLUG

    missing = client.get(f"/materials/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "MATERIAL_NOT_FOUND"


def test_another_tenants_material_is_a_404_not_a_row(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**跨租户**：不是「看得见但没权限」，是**根本不存在**。

    RLS 在 `053` 上是 `FORCE ROW LEVEL SECURITY`，而这条用例走的是真正的
    API 连接（`fastagent_app`），不是迁移超级用户——**那个区别正是
    E4-1 的 RLS 用例踩过的坑**：超级用户绕过 RLS，用它测等于什么都没测。
    """

    with _migration_connection() as connection:
        theirs = _place(connection, seeded_database["tenant_b"], slug="their-list")
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.read", "material.list"])
    assert client.get(f"/materials/{theirs}", headers=headers).status_code == 404
    assert client.get("/materials", headers=headers).json()["items"] == []


# ---------------------------------------------------------------- 归档


def test_archiving_marks_the_row_and_answers_with_it(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    response = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["status"] == "archived"
    assert result["archived_at"] is not None
    assert response.json()["receipt"]["status"] == "succeeded"


def test_archiving_twice_is_a_conflict_not_a_second_success(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """重试不得制造出一次新的下架——与 `POST /channel-bindings/{id}/deactivate` 同理。"""

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    first = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "MATERIAL_ALREADY_ARCHIVED"


def test_a_material_a_live_profile_still_declares_cannot_be_archived(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**本文件最值钱的一条。**

    裁定五让引用在**发送时**解析，于是归档一份仍被声明的资料
    **在这一刻不会报任何错**——它会在下一次有人触发那一帧时才炸，
    **而那时炸的是一次真实对话**。

    **一个忘了做这道检查的实现，在本文件其余每一条用例里都是绿的。**

    拒绝里要说清**是谁在声明它**：只说「不行」会把操作员逼回数据库里翻。
    """

    with _migration_connection() as connection:
        _declare_in_live_profile(
            connection, seeded_database["tenant_a"], f"asset:{SLUG}:v1"
        )
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    response = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MATERIAL_STILL_DECLARED"

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT status FROM materials WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], placed),
        ).fetchone()
    assert row is not None
    assert row["status"] == "active", "被拒之后这一行必须原样不动"


def test_the_read_says_which_releases_still_declare_it(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**「谁在声明它」在读那一面，因为拒绝里带不了它。**

    归档被拒时错误信封只放行一个 `code` —— `_http_error` 把结构化 `detail`
    收成 `code` + `field_errors` 两个成员，理由是
    「so that "detail" cannot become a place to attach arbitrary internals」。
    **那条约束是对的，本批第一版的设计越过了它**，是门禁顶回来的。

    所以这条清单挂在资源上：操作员在按下归档**之前**就看得到该改哪一份 profile，
    而不是被拒之后再回来查。
    """

    client, headers = _client_for(settings, seeded_database, ["material.read"])
    before = client.get(f"/materials/{placed}", headers=headers).json()
    assert before["declared_by_releases"] == [], "还没有人声明它的时候，这里是空的"

    with _migration_connection() as connection:
        _declare_in_live_profile(connection, seeded_database["tenant_a"], f"asset:{SLUG}:v1")
        connection.commit()

    after = client.get(f"/materials/{placed}", headers=headers).json()
    assert after["declared_by_releases"], "在架 profile 声明之后，读要说得出是谁"


def test_an_older_version_may_be_archived_while_another_stays_active(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**换新版本之后下架旧版本——投放规格里的标准做法，要放行。**

    ## 这条用例的前一版把与图纸相反的读法钉成了回归基线

    它当时叫 `..._a_different_version_being_declared_does_not_block_this_one`，
    docstring 写的是「**声明是按版本算的，不是按 slug 算的**」——
    而图纸三处写死的恰好相反（freeze B4 #5 / ADR-0130 §二.2 /
    `references.py`：「profile 声明 slug 不声明版本，版本段只是语法占位」）。
    **一个与图纸相反的读法被写成了用例，它就不会随实现变化而变红**
    （独立审查 2026-09-12，P1-1）。

    **现在钉的是用户裁定的判据**：按 slug 判，**只有在归档之后这个 slug
    一个 active 都不剩时才拒**。所以这里放行的理由变了——
    不是「声明的是别的版本」，而是「**还剩一个 active 版本**」。
    """

    with _migration_connection() as connection:
        _place(connection, seeded_database["tenant_a"], version=2)
        _declare_in_live_profile(connection, seeded_database["tenant_a"], f"asset:{SLUG}:v1")
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    response = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text


def test_archiving_the_version_send_would_resolve_to_is_refused_by_slug(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**独立审查 P1-1 报的那个漏，钉在这里。**

    失败场景（审查方的原话）：在架 release 声明 `asset:price-list:v1`；
    商家按投放规格放进了 `price-list` 的 **v2**；发送时按 slug 解析当前那一版 = v2。
    操作员归档 **v2** → 旧实现算出的字符串是 `asset:price-list:v2`，
    profile 里没有这个字符串 → **放行**，
    而那正是 ADR-0130 §二.3 要挡的那一格。

    **这里 v2 是唯一的 active 版本**（`placed` 那条先归档掉），
    所以归档它会把这个 slug 打空——按 slug 判必须拒。
    **按整条 ref 判则会放行**，两种判据在这条用例上答案相反。
    """

    with _migration_connection() as connection:
        second = _place(connection, seeded_database["tenant_a"], version=2)
        connection.execute(
            "UPDATE materials SET status = 'archived', archived_at = now() "
            " WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], placed),
        )
        _declare_in_live_profile(connection, seeded_database["tenant_a"], f"asset:{SLUG}:v1")
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    response = client.post(
        f"/materials/{second}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MATERIAL_STILL_DECLARED"


def test_a_release_only_a_live_conversation_still_points_at_counts_as_live(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**独立审查 P1-2：「在架」不止是「有绑定指着它」。**

    重绑有 `LIMIT`、有 `repin_live_conversations` 开关、**只按 `config_id` 选**，
    所以「一次重绑搬不走的会话仍 pin 在旧 release 上」是常态而不是边角
    （`profiles/binding.py` 显式返回 `remaining` 与 `unlinked` 两个计数）。

    这里造出那个状态：让声明了这份资料的 release **只被一条非 closed 会话指着**，
    绑定已经挪到别处。**只看绑定的实现会放行**，而那些在飞会话下一次走到
    那一帧时会炸在真实对话里。
    """

    tenant = seeded_database["tenant_a"]
    with _migration_connection() as connection:
        declaring = _declare_in_live_profile(connection, tenant, f"asset:{SLUG}:v1")
        # 把绑定挪回原来那份 release —— 于是 declaring 只剩会话指着它。
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (seeded_database["profile_release"], tenant),
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, state_version, lifecycle, control_mode, generation,
                profile_release_id, target_stack, state, config_id
            ) VALUES (%s, %s, 0, 'open', 'ai', 0, %s, %s, %s, 'config-a')
            """,
            (tenant, uuid.uuid4(), declaring, Jsonb([]), Jsonb({})),
        )
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    response = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "MATERIAL_STILL_DECLARED"


def test_archiving_writes_an_audit_row_naming_the_reference(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """审计行要能拿去和 profile 对照，而 profile 里写的是引用不是 id。"""

    client, headers = _client_for(settings, seeded_database, ["material.invalidate"])
    client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT action, resource_type, reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND resource_id = %s
            """,
            (seeded_database["tenant_a"], placed),
        ).fetchone()

    assert row is not None, "下架一份资料没有留下审计行"
    assert row["action"] == "material.invalidate"
    assert row["resource_type"] == "material"
    assert row["reason_code"] == "MATERIAL_ARCHIVED"
    assert row["evidence"]["reference"] == f"asset:{SLUG}:v1"


# ---------------------------------------------------------------- 权限


def test_reading_does_not_carry_archiving(
    settings: Settings, seeded_database: dict[str, Any], placed: uuid.UUID
) -> None:
    """**读和下架是两个 action**，拿着读的权限下不了架。"""

    client, headers = _client_for(settings, seeded_database, ["material.list", "material.read"])
    response = client.post(
        f"/materials/{placed}/archive",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 403


def test_viewer_may_read_the_catalogue_but_not_archive() -> None:
    """三个系统角色上的落点，**不碰数据库，只问派生出来的权限集合**。

    `viewer` 拿到两个读：资料的元数据既不是对话内容、不是凭据元数据、
    也不是客户身份，三条排除判据一条都不命中。
    `material.invalidate` 不是读，所以不在里面；
    它也不在 `operator` 里——操作员做的是会话里的活，
    而「哪几份资料还能发给客户」是商家的内容决定。
    """

    permissions = system_role_permissions()
    assert {"material.list", "material.read"} <= permissions["viewer"]
    assert "material.invalidate" not in permissions["viewer"]
    assert "material.invalidate" not in permissions["operator"]
    assert "material.invalidate" in permissions["owner"]
