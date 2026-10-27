"""记忆的四条控制面 route（freeze B4 #8 / #9 / #10 / #11，E4-3）。

**最值钱的一条是 `test_a_revision_does_not_inherit_the_customer_event`。**

B4 #10 的「不覆盖来源证据」通常被读成「别把旧证据擦掉」——那一半由
「写新行不改旧行」自动满足，**一个漏掉它的实现在别处也都是绿的**。
真正需要一道闸的是反方向：**新行不许继承旧行的 `source_event_id`**。
旧的那条是模型从一条客户消息里记下的，新的这条是员工改的；
把事件 id 抄过去，等于让新行声称**客户说过一句其实是员工写的话**。

第二值钱的是 `test_the_authoriser_cannot_be_rewritten_afterwards`：
`060` 给 `fastagent_app` 授了 `UPDATE`，而 `054` 的不可变触发器
**是按列名逐个列出来的**——一个后加的列不在那张清单上就是可写的。
「哪个员工授权记的」若能被后来的人改成别人的名字，consent 这一列就没有意义。
"""

from __future__ import annotations

import copy
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

#: 这份 profile 声明可以记什么。**商家的词表，不是平台的**（ADR-0131 M2）。
DECLARED_FIELDS = ("ring_size", "budget")

#: 校验位对得上的合成身份证号，与 `test_memory_value_tripwire.py` 同一个。
VALID_ID = "110101199003074258"

ALL_MEMORY_PERMISSIONS = [
    "contact_memory.list",
    "contact_memory.create",
    "contact_memory.update",
    "contact_memory.invalidate",
]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _client_for(
    settings: Settings,
    seeded: dict[str, Any],
    permissions: list[str],
    *,
    tenant: str = "a",
) -> tuple[TestClient, dict[str, str]]:
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE employees SET permissions = %s WHERE tenant_id = %s AND id = %s",
            (
                Jsonb(permissions),
                seeded[f"tenant_{tenant}"],
                seeded[f"employee_{tenant}"],
            ),
        )
        connection.commit()
    client = TestClient(create_app(settings))
    login = client.post(
        "/auth/login",
        json={
            "tenant_code": f"tenant-{tenant}",
            "email": f"operator-{tenant}@example.com",
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _declare_memory_fields(
    connection: Connection[Any], tenant_id: Any, keys: tuple[str, ...]
) -> None:
    """让**在架** release 声明这几个记忆字段：发一个新版本，然后重绑。

    **不能原地改那一份**——`profile_releases` 是 append-only，触发器的 HINT
    写着「publish a new release and rebind: releases are never rewritten」。
    做法逐行照 `test_materials_routes.py::_declare_in_live_profile`。

    **绕过了封版时的 `_check_memory_fields`**，这是有意的：那道闸有自己的
    一整个用例文件（`test_memory_fields_admission.py`），
    本文件要的是「运行期读到一份已声明的清单之后怎么办」。
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
    document["memory_fields"] = [{"key": key} for key in keys]

    release_id = uuid.uuid4()
    document["identity"]["release_id"] = str(release_id)
    document["identity"]["version"] = "1.0.9"
    connection.execute(
        "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
        "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            release_id,
            row["profile_id"],
            "1.0.9",
            canonical_hash(document),
            Jsonb(document),
        ),
    )
    connection.execute(
        "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
        (release_id, tenant_id),
    )


def _make_contact(connection: Connection[Any], tenant_id: Any, *, config_id: str) -> uuid.UUID:
    contact_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO contacts(tenant_id, id, config_id, external_user_id)
        VALUES (%s, %s, %s, %s)
        """,
        (tenant_id, contact_id, config_id, f"wx-{contact_id}"),
    )
    return contact_id


def _model_written_memory(
    connection: Connection[Any],
    tenant_id: Any,
    contact_id: uuid.UUID,
    *,
    field_key: str = "ring_size",
    field_value: str = "17",
) -> tuple[uuid.UUID, uuid.UUID]:
    """模型写下的那种记忆：锚在一条真实客户事件上，没有授权员工。

    这是 E4-2 的 `_remember_write` 产的形状，**本文件用它来证明控制面那三条
    route 对它的处理是对的**——尤其是 `PUT` 不把 `source_event_id` 抄到新行上。
    """

    release = connection.execute(
        "SELECT profile_release_id FROM channel_bindings WHERE tenant_id = %s LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert release is not None

    event_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO inbox_events(
            tenant_id, id, event_id, source, external_event_id, conversation_id,
            event_type, profile_release_id, envelope, status
        ) VALUES (%s, %s, %s, 'wecom_proxy', %s, %s, 'customer_message', %s, %s, 'consumed')
        """,
        (
            tenant_id,
            event_id,
            event_id,
            f"memory-{event_id}",
            uuid.uuid4(),
            release["profile_release_id"],
            Jsonb({"event_type": "customer_message"}),
        ),
    )
    memory_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO contact_memories(
            tenant_id, id, contact_id, field_key, field_value, source_event_id,
            revision, supersedes, status
        ) VALUES (%s, %s, %s, %s, %s, %s, 1, NULL, 'active')
        """,
        (tenant_id, memory_id, contact_id, field_key, field_value, event_id),
    )
    return memory_id, event_id


@pytest.fixture()
def contact(seeded_database: dict[str, Any]) -> Any:
    with _migration_connection() as connection:
        _declare_memory_fields(connection, seeded_database["tenant_a"], DECLARED_FIELDS)
        contact_id = _make_contact(connection, seeded_database["tenant_a"], config_id="config-a")
        connection.commit()
        yield contact_id


# ------------------------------------------------------------------ B4 #9 写


def test_the_authoriser_is_the_caller_and_never_the_body(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**freeze B4 #9 的 `consent`，按 ADR-0131 M1 读。**

    它不是「客户同意了」，是「哪个员工授权记的」，而答案取自 bearer token。
    **请求体里给不了它**——`extra="forbid"` 让试图指定别人的那次调用变成 422，
    而不是变成一条挂在同事名下的记录。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"},
    )

    assert response.status_code == 201, response.text
    result = response.json()["result"]
    assert result["authorised_by"] == str(seeded_database["employee_a"])
    assert result["authorisation_note"] == "客户在第 2 轮说的"
    assert result["source_event_id"] is None, (
        "员工手工记的东西没有入站事件；`060` 的 CHECK 让「恰好一个来源」是数据库事实"
    )
    assert result["revision"] == 1
    assert result["status"] == "active"

    smuggled = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "field_key": "budget",
            "field_value": "5000",
            "note": "客户在第 3 轮说的",
            "authorised_by": str(seeded_database["employee_b"]),
        },
    )
    assert smuggled.status_code == 422, smuggled.text


def test_a_field_the_profile_did_not_declare_is_refused(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**清单归 profile，而「必须有一份封闭的清单」归平台**（ADR-0131 §三）。

    平台不判断 `shoe_size` 合不合理——它只问这家商家声明过它没有。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "shoe_size", "field_value": "39", "note": "她顺口提到的，先记一下"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "MEMORY_FIELD_NOT_DECLARED"


def test_a_forbidden_value_is_refused_even_in_a_declared_field(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**T298 在控制面这一侧的兑现。**

    `budget` 是商家自己声明的、名字里没有任何禁止词的字段——
    E4-1b 那道按**列名**判的闸对它一个字都说不上。
    取值层这道闸看的是值本身，所以它拦得住。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "budget", "field_value": VALID_ID, "note": "她把证件号念给我了"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "MEMORY_VALUE_FORBIDDEN"
    assert VALID_ID not in response.text, (
        "拒绝身份证号的错误信封不能把身份证号带出去 —— E4-2 的 `withheld_content` 付过这笔钱"
    )


def test_posting_over_an_active_field_is_refused_not_silently_superseded(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**`POST` 是「新记一件」，`PUT` 是「改一件」，freeze 把它们列成两行。**

    让 `POST` 顺手覆盖，等于让一次手滑的重放把上一条证据换掉——
    而 B4 #10 的「不覆盖来源证据」正是要挡这个。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    body = {"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"}
    first = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json=body,
    )
    assert first.status_code == 201, first.text

    again = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={**body, "field_value": "19"},
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "MEMORY_FIELD_ALREADY_ACTIVE"

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT field_value FROM contact_memories WHERE tenant_id = %s AND status = 'active'",
            (seeded_database["tenant_a"],),
        ).fetchone()
    assert row is not None
    assert row["field_value"] == "18", "被拒的那次不得改动已有的那一条"


# ------------------------------------------------------------------ B4 #10 改


def test_a_revision_does_not_inherit_the_customer_event(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**本文件的主判据。**

    模型记下「17」，锚在一条真实的客户消息上。员工改成「18」。
    改完之后：

    * 旧行还在，`superseded`，**它自己的 `source_event_id` 一个字没变**
      ——这是「不覆盖来源证据」通常被读到的那一半；
    * 新行 `revision = 2`、`supersedes` 指回旧行，
      **而它的 `source_event_id` 是 `NULL`**——这是硬的那一半。
      抄过去的话，新行就会声称「客户说他戴 18 号」，
      而客户说的是 17，说 18 的是员工。
    """

    with _migration_connection() as connection:
        memory_id, event_id = _model_written_memory(
            connection, seeded_database["tenant_a"], contact
        )
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.put(
        f"/contacts/{contact}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_value": "18", "note": "客户后来改口说是 18"},
    )

    assert response.status_code == 200, response.text
    revised = response.json()["result"]
    assert revised["revision"] == 2
    assert revised["supersedes"] == str(memory_id)
    assert revised["field_key"] == "ring_size", "修订必须留在同一个字段上（054 的复合外键）"
    assert revised["source_event_id"] is None, (
        "新行不得继承旧行的客户事件 —— 那会让员工写的话挂上客户的名义"
    )
    assert revised["authorised_by"] == str(seeded_database["employee_a"])

    listed = client.get(f"/contacts/{contact}/memories", headers=headers).json()["items"]
    by_id = {item["id"]: item for item in listed}
    assert by_id[str(memory_id)]["status"] == "superseded"
    assert by_id[str(memory_id)]["source_event_id"] == str(event_id), (
        "旧行的来源证据原样留着"
    )
    assert by_id[str(memory_id)]["field_value"] == "17"


def test_revising_a_memory_that_is_not_active_is_refused(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """改一个已经不作数的版本，语义上不是一次修订。

    与 `archive_material` 第二次调用答 409 同一条理由：
    **重试不得制造出一次新的修订。**
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    created = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"},
    ).json()["result"]
    client.put(
        f"/contacts/{contact}/memories/{created['id']}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_value": "19", "note": "量过之后是 19"},
    )

    again = client.put(
        f"/contacts/{contact}/memories/{created['id']}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_value": "20", "note": "再量一次是 20"},
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "MEMORY_NOT_ACTIVE"


def test_the_contact_in_the_path_has_to_own_the_memory(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """`/contacts/A/memories/{B 的记忆}` 是 404，**不是悄悄改了 B 的那一条**。

    路径里有两个 id，两个都要算数。**一个只按 `memory_id` 查的实现在其余
    每一条用例里都是绿的**——所有那些用例的两个 id 本来就是配套的。
    """

    with _migration_connection() as connection:
        other = _make_contact(connection, seeded_database["tenant_a"], config_id="config-a")
        memory_id, _ = _model_written_memory(connection, seeded_database["tenant_a"], other)
        connection.commit()

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.put(
        f"/contacts/{contact}/memories/{memory_id}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_value": "18", "note": "这条不该被我改到"},
    )
    assert response.status_code == 404, response.text

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT field_value, status FROM contact_memories WHERE tenant_id = %s AND id = %s",
            (seeded_database["tenant_a"], memory_id),
        ).fetchone()
    assert row is not None
    assert (row["field_value"], row["status"]) == ("17", "active")


# ------------------------------------------------------------------ B4 #11 删


def test_a_tombstoned_memory_stays_visible_and_stops_being_active(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**墓碑，不是删行**——B4 #11 的「引用回答可解释」要求行还在。

    两件一起断言，因为少任何一件这条 route 就不是 B4 #11 说的那个东西：

    * 行还在、`GET` 默认仍返回它、`tombstoned_at` 盖上了；
    * 它不再是 `active`——而 `prepare` 每回合读的正是 `status = 'active'`
      （`pipeline.py::_remembered_memories`），所以模型立刻不再看见它。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    created = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"},
    ).json()["result"]

    removed = client.request(
        "DELETE",
        f"/contacts/{contact}/memories/{created['id']}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"note": "记错了，那是她妈妈的尺寸"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["result"]["status"] == "tombstoned"
    assert removed.json()["result"]["tombstoned_at"] is not None

    listed = client.get(f"/contacts/{contact}/memories", headers=headers).json()["items"]
    assert [item["id"] for item in listed] == [created["id"]], "墓碑仍要看得见"

    only_active = client.get(
        f"/contacts/{contact}/memories?status=active", headers=headers
    ).json()["items"]
    assert only_active == []

    again = client.request(
        "DELETE",
        f"/contacts/{contact}/memories/{created['id']}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"note": "再删一次，看它答什么"},
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "MEMORY_ALREADY_TOMBSTONED"


def test_the_field_is_free_again_after_a_tombstone(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """作废之后，同一个字段可以重新记——**这是「商家可删」的实际意思**（M1）。

    `054` 的部分唯一索引只管 `active`，所以墓碑不占位。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    created = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"},
    ).json()["result"]
    client.request(
        "DELETE",
        f"/contacts/{contact}/memories/{created['id']}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"note": "记错了，重新问过"},
    )

    again = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "19", "note": "重新问过是 19"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["result"]["revision"] == 1, (
        "重新记是一条新的第一版，不是接着旧链走 —— 旧链已经被作废了"
    )


# ------------------------------------------------------------------ B4 #8 读


def test_an_unknown_contact_is_a_404_not_an_empty_page(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """空清单会被读成「我们对这位客户什么都不记得」，**而那是一句关于客户的错话**。"""

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.get(f"/contacts/{uuid.uuid4()}/memories", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "CONTACT_NOT_FOUND"


def test_a_status_filter_nobody_defined_is_refused(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    response = client.get(f"/contacts/{contact}/memories?status=forgotten", headers=headers)

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_MEMORY_STATUS"


def test_another_tenant_cannot_read_these_memories(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """RLS：B 租户拿着 A 租户的 contact id 问，得到的是 404，不是别人的记忆。"""

    with _migration_connection() as connection:
        _model_written_memory(connection, seeded_database["tenant_a"], contact)
        connection.commit()

    client, headers = _client_for(
        settings, seeded_database, ALL_MEMORY_PERMISSIONS, tenant="b"
    )
    response = client.get(f"/contacts/{contact}/memories", headers=headers)
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    ("method", "path_suffix", "body", "permission"),
    [
        ("GET", "", None, "contact_memory.list"),
        (
            "POST",
            "",
            {"field_key": "ring_size", "field_value": "18", "note": "客户自己说的，第 2 轮"},
            "contact_memory.create",
        ),
    ],
)
def test_each_route_refuses_without_its_own_permission(
    settings: Settings,
    seeded_database: dict[str, Any],
    contact: uuid.UUID,
    method: str,
    path_suffix: str,
    body: dict[str, Any] | None,
    permission: str,
) -> None:
    """**四个 action 不共享**，K1 的判据：这个权限需不需要单独授予。

    「看我们记了这位客户什么」与「替我们记下一件事」是两件不同的授权。
    """

    others = [name for name in ALL_MEMORY_PERMISSIONS if name != permission]
    client, headers = _client_for(settings, seeded_database, others)
    response = client.request(
        method,
        f"/contacts/{contact}/memories{path_suffix}",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json=body,
    )
    assert response.status_code == 403, response.text


# -------------------------------------------------------- 060 的那两列本身


def test_the_authoriser_cannot_be_rewritten_afterwards(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**`060` 授了 `UPDATE`，所以后加的列必须进不可变清单。**

    `054` 的触发器是**按列名逐个列出来**的，一个后加的列不在那张清单上就是可写的。
    「哪个员工授权记的」若能被后来的人改成别人的名字，consent 这一列就没有意义
    ——**而它不会在任何 route 的用例里变红**，因为没有 route 会去改它。
    所以这一条直接打数据库。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    created = client.post(
        f"/contacts/{contact}/memories",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"},
    ).json()["result"]

    with _migration_connection() as connection:
        for column, value in (
            ("authorised_by", seeded_database["employee_b"]),
            ("authorisation_note", "另一个说法，改过的"),
        ):
            with pytest.raises(Exception, match="immutable"):  # noqa: B017, PT011
                connection.execute(
                    f"UPDATE contact_memories SET {column} = %s "  # noqa: S608
                    "WHERE tenant_id = %s AND id = %s",
                    (value, seeded_database["tenant_a"], created["id"]),
                )
            connection.rollback()


def test_every_substantive_column_is_refused_by_the_trigger(
    seeded_database: dict[str, Any],
) -> None:
    """清单从 `information_schema` 推，**不从触发器的源码里抄**。

    上面那条用例钉的是 `060` 加的两列；**这一条钉的是下一个人加的那一列**。
    `054` 的守卫是列名清单形状的，而这个形状的陷阱一直在原地等着——
    本批就差点踩进去。一条把清单写死的用例会和触发器一起漏掉同一列；
    从元数据反推的这一条不会。

    生命周期那两列（`status` / `tombstoned_at`）不在里面——
    它们正是触发器唯一放行的那两格。

    ## 读的是函数体，不是行为，**而那是一次退让，写下来**

    第一版对每一列跑一次 `SET <列> = NULL` 并要求它炸。
    **门禁给的是 `DID NOT RAISE`**：一列本来就是 `NULL`，把它设成 `NULL`
    在 `IS DISTINCT FROM` 下是一次无变化的更新，触发器不该拦、也确实没拦。
    要做成真行为判据，得为每一列造一个类型正确且确实不同的值——
    那需要一张列名到取值的表，**而那张表正是本条要防的那种手写清单**。

    所以这里改成读 `pg_get_functiondef` 的函数体，**按
    `NEW.<列> IS DISTINCT FROM OLD.<列>` 这个精确形状匹配**，
    不是「函数体里出现过这个列名」——后者一句注释就能满足，
    而本批的迁移注释里恰好就提到了那两列的名字。

    **它比行为判据弱一层**：一个把整个 `IF` 拆散重写的实现能同时骗过它。
    换来的是它真的覆盖每一列。**行为那一层由上面那条用例在 `060` 的两列上守着**，
    两条一起读。
    """

    assert seeded_database["tenant_a"], "要的是一个已迁移的库，夹具负责这件事"

    lifecycle = {"status", "tombstoned_at"}
    with _migration_connection() as connection:
        columns = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'contact_memories'
                """
            ).fetchall()
        }
        body = connection.execute(
            "SELECT pg_get_functiondef(oid) AS src FROM pg_proc "
            "WHERE proname = 'contact_memories_reject_mutation'"
        ).fetchone()
    assert body is not None, "不可变触发器的函数不在了"
    assert columns - lifecycle, "contact_memories 没有实质列，前提变了"

    missing = sorted(
        column
        for column in columns - lifecycle
        if f"NEW.{column} IS DISTINCT FROM OLD.{column}" not in body["src"]
    )
    assert not missing, (
        f"这些列不在不可变守卫里：{missing}。"
        "`054` 的守卫是列名清单形状的，而 `060` 授了 `UPDATE` —— "
        "不在清单上的列就是可就地改写的。加列的那一批要同时改这个函数"
    )


def test_a_row_cannot_claim_both_origins_or_neither(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """`060` 的 `CHECK (num_nonnulls(source_event_id, authorised_by) = 1)`。

    **「两个都有」是要挡的那一格**：它正是 `PUT` 把旧行的客户事件抄到新行上
    会产生的形状。route 那一侧不抄，**而这一条保证就算有人改了 route 也抄不成**。
    """

    with _migration_connection() as connection:
        memory_id, event_id = _model_written_memory(
            connection, seeded_database["tenant_a"], contact
        )
        connection.commit()

    with _migration_connection() as connection:
        with pytest.raises(Exception, match="exactly_one_origin"):  # noqa: B017, PT011
            connection.execute(
                """
                INSERT INTO contact_memories(
                    tenant_id, id, contact_id, field_key, field_value,
                    source_event_id, revision, supersedes, status,
                    authorised_by, authorisation_note
                ) VALUES (%s, %s, %s, 'budget', '5000', %s, 1, NULL, 'active', %s, %s)
                """,
                (
                    seeded_database["tenant_a"],
                    uuid.uuid4(),
                    contact,
                    event_id,
                    seeded_database["employee_a"],
                    "两个来源都写上，应当被拒",
                ),
            )
        connection.rollback()

        with pytest.raises(Exception, match="exactly_one_origin"):  # noqa: B017, PT011
            connection.execute(
                """
                INSERT INTO contact_memories(
                    tenant_id, id, contact_id, field_key, field_value,
                    source_event_id, revision, supersedes, status
                ) VALUES (%s, %s, %s, 'budget', '5000', NULL, 1, NULL, 'active')
                """,
                (seeded_database["tenant_a"], uuid.uuid4(), contact),
            )
        connection.rollback()
    assert memory_id is not None


# ---------------------------------------------------- 与模型那条写入路径的竞态


def test_a_write_waits_on_the_same_lock_the_remember_path_takes(
    settings: Settings, seeded_database: dict[str, Any], contact: uuid.UUID
) -> None:
    """**`POST` 必须停在那把咨询锁后面。**

    `054` 的 `contact_memories_one_active_per_field_idx` 是 partial unique index，
    而占用检查与插入之间有一个窗口。E4-2 为**一模一样**的竞态取了
    `pg_advisory_xact_lock(hashtext(tenant), hashtext(contact))`
    （`pipeline.py::_memory_gate`）；本批第一版一把锁都没取。

    **撞索引的后果不对称，这是它值一条用例的原因**：撞在 route 这一侧是一个 500；
    撞在 worker 那一侧是整轮回滚、inbox 事件不消费、退回重试预算里再炸一次。

    做法：另一条连接先拿住同一把锁，然后在别的线程里发请求，
    **要求它在锁被拿着的时候不返回**，锁一放就返回 201。
    一个不取锁的实现会立刻返回，于是第一个 `result(timeout=…)` 不抛，用例红。
    """

    client, headers = _client_for(settings, seeded_database, ALL_MEMORY_PERMISSIONS)
    body = {"field_key": "ring_size", "field_value": "18", "note": "客户在第 2 轮说的"}

    holder = _migration_connection()
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (str(seeded_database["tenant_a"]), str(contact)),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post,
                f"/contacts/{contact}/memories",
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
                json=body,
            )
            with pytest.raises(FutureTimeout):
                # 锁拿在别人手里，这次写不该完成。
                pending.result(timeout=3)

            holder.rollback()  # 放锁
            response = pending.result(timeout=30)

    finally:
        holder.close()

    assert response.status_code == 201, response.text


def test_both_write_paths_derive_the_lock_key_the_same_way() -> None:
    """**两条写入路径必须取到同一把锁，而这件事在一个进程里看不出来。**

    上面那条用例证明 route 会等——**但它等的是用例自己写下的那把锁**。
    route 与 worker 是否等在同一把锁上，只有把两处的键放在一起看才知道，
    **而键写错了不会让任何东西变红**：两个写入方各自安全、彼此不可见，
    一直到某一天它们真的同时落在同一个 `(contact, field_key)` 上。

    **「取了锁但没锁住对面」比不取锁更坏，因为它读起来像安全的。**

    所以这里比对源码。**知道这是一条弱判据**——它认的是一个字符串形状，
    一次等价改写（换成 `pg_advisory_xact_lock(bigint)`、或把键算在 Python 里）
    能让两边仍然正确而本条变红。**那时该改的是这条用例，不是绕过它**，
    而它变红这件事本身就会逼着改的人去确认两边还锁在一起——那正是要的效果。
    """

    expression = "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))"
    root = Path(__file__).resolve().parents[2]
    holders = {
        "route": root / "services/api/src/fastagent_v2/api/app.py",
        "remember 写入路径": root / "services/worker/src/fastagent_v2/worker/pipeline.py",
    }

    missing = [
        name
        for name, path in holders.items()
        if expression not in path.read_text(encoding="utf-8")
    ]
    assert not missing, (
        f"这些写入方没有按同一个形状取 contact 咨询锁：{missing}。"
        "两边的键必须逐字相同，否则它们各自安全而彼此不可见"
    )
