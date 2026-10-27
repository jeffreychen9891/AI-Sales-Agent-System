"""`POST /conversations/{id}/messages` —— 坐席说一句话，在真库上走完四步。

## 它验的是什么

`control-plane-parity.md` §6.2：「员工发送仍走 intent/policy/outbox/delivery」。
那条链**在库层面是被外键强制的**（`outbox_messages` 的 `intent_id` 指向
`intent_records`），所以「走没走完」不是读代码能答的问题 —— 要看库里
是不是真的落了那四行。

另一半是 ADR-0143 §一：**那条命令里不许有 `proactivity` 块**。
`tests/contracts/test_human_send_skips_proactivity.py` 从源码那一侧钉它；
这里从**落库那一侧**再钉一次 —— 两侧都钉，是因为源码判据看不见
一个在运行期拼出来的键。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.api.app import create_app
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.contracts import validate_control_plane_contract
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]


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
            # 与 `test_conversation_takeover_route.py` 逐字相同 —— 这两个值
            # 是夹具里写死的常量，不在 `seeded_database` 的返回里。
            "tenant_code": "tenant-a",
            "email": "operator-a@example.com",
            "password": seeded["login_password"],
        },
    )
    assert login.status_code == 200, login.text
    return client, {"Authorization": f"Bearer {login.json()['access_token']}"}


def _seed(seeded: dict[str, Any]) -> UUID:
    """一条引擎可能产出的会话，带一个活跃帧。"""

    tenant_id = seeded["tenant_a"]
    conversation_id = uuid4()
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_id,)
        ).fetchone()
        assert release is not None
        profile = (
            ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
            .package(profile_id="synthetic-service-desk", version="1.0.4")
            .document
        )
        aggregate = _initial_aggregate(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            profile_release_id=release["id"],
            profile=profile,
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, generation, state_version,
                target_stack, state
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                conversation_id,
                release["id"],
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
            ),
        )
        connection.commit()
    return conversation_id


def _send(client: TestClient, headers: dict[str, str], conversation_id: UUID, text: str):
    return client.post(
        f"/conversations/{conversation_id}/messages",
        json={"text": text},
        headers={**headers, "Idempotency-Key": f"human-send:{uuid4()}"},
    )


def _rows(tenant_id: UUID, conversation_id: UUID) -> dict[str, list[dict[str, Any]]]:
    with _migration_connection() as connection:
        return {
            name: connection.execute(
                f"SELECT * FROM {name} WHERE tenant_id = %s AND conversation_id = %s",  # noqa: S608
                (tenant_id, conversation_id),
            ).fetchall()
            for name in ("intent_records", "policy_decisions", "outbox_messages")
        }


def test_sending_before_taking_over_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**409，而不是 403。**

    这不是「你没有权限」，是「这条会话现在不归你说话」—— 一个状态冲突，
    先接管就能解决。理由来自 ADR-0054：接管被做成一等命令，正是为了让
    「谁在说话」无歧义；允许未接管就发，等于有两个说话者而没有交接记录。
    """

    client, headers = _client_for(
        settings, seeded_database, ["conversation_message.create"]
    )
    conversation_id = _seed(seeded_database)

    refused = _send(client, headers, conversation_id, "在的")

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "CONVERSATION_NOT_TAKEN_OVER"
    # **一行都不许落。** 一次被拒的发送在台账上留下半条链，比拒绝本身更糟。
    rows = _rows(seeded_database["tenant_a"], conversation_id)
    assert rows["intent_records"] == []
    assert rows["outbox_messages"] == []


def test_a_taken_over_conversation_can_be_spoken_in(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """接管之后那四步真的落进四张表，而回执说的是 `accepted`。"""

    client, headers = _client_for(
        settings,
        seeded_database,
        ["conversation_takeover.create", "conversation_message.create"],
    )
    conversation_id = _seed(seeded_database)

    taken = client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": f"takeover:{uuid4()}"},
    )
    assert taken.status_code == 200, taken.text

    sent = _send(client, headers, conversation_id, "在的，我看一下")

    assert sent.status_code == 202, sent.text
    body = sent.json()
    validate_control_plane_contract("command-receipt", body)
    # **`accepted` 不是 `succeeded`**：这一刻发生的是排进队列，不是送达。
    # 界面上那个「已发送」只能来自投递回执（parity §6.2）。
    assert body["status"] == "accepted"

    rows = _rows(seeded_database["tenant_a"], conversation_id)
    assert len(rows["intent_records"]) == 1
    intent = rows["intent_records"][0]
    assert intent["kind"] == "send_message"
    # 立意图 → 记策略痕 → **标已批准** → 排队：第三步落在这个字段上。
    assert intent["status"] == "APPROVED"
    assert intent["proposed_by"]["step"] == "human"
    assert intent["proposed_by"]["employee_id"] == str(seeded_database["employee_a"])

    assert len(rows["policy_decisions"]) == 1
    assert rows["policy_decisions"][0]["hook"] == "pre_execute"

    assert len(rows["outbox_messages"]) == 1
    outbox = rows["outbox_messages"][0]
    assert outbox["intent_id"] == intent["id"], "出站行必须挂在那条意图上"
    assert outbox["command"]["content"]["text"] == "在的，我看一下"
    assert outbox["command"]["audience"] == "customer"


def test_the_queued_command_has_no_proactivity_block(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**ADR-0143 §一，从落库那一侧再钉一次。**

    源码那一侧由 `tests/contracts/test_human_send_skips_proactivity.py` 钉着，
    而那条判据**看不见一个在运行期拼出来的键**。这里看的是真的写进去的
    那一行 —— 两侧都钉，因为这条裁定被反转的方式不止一种。

    块在，`proactivity_deadline` 就从 `None` 变成一个真实时刻，
    **于是四道闸里的静默时段在 dispatcher 那一侧重新生效** ——
    而那正是裁定说不要的。
    """

    client, headers = _client_for(
        settings,
        seeded_database,
        ["conversation_takeover.create", "conversation_message.create"],
    )
    conversation_id = _seed(seeded_database)
    client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": f"takeover:{uuid4()}"},
    )
    assert _send(client, headers, conversation_id, "在的").status_code == 202

    command = _rows(seeded_database["tenant_a"], conversation_id)["outbox_messages"][0][
        "command"
    ]
    assert "proactivity" not in command
    assert set(command) == {"audience", "content"}


@pytest.mark.parametrize("text", ["", "x" * 2001], ids=["空", "超上限"])
def test_the_text_is_bounded_on_both_ends(
    settings: Settings, seeded_database: dict[str, Any], text: str
) -> None:
    """空串与超长都拒。

    ⚠️ **2000 这个上限是我们的，不是供应商的**（`wecom-proxy-message-api.md`
    里没有任何文长限制）。登记 T381 —— 真实上限要等第一次真实发送撞到它。
    """

    client, headers = _client_for(
        settings,
        seeded_database,
        ["conversation_takeover.create", "conversation_message.create"],
    )
    conversation_id = _seed(seeded_database)
    client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": f"takeover:{uuid4()}"},
    )

    assert _send(client, headers, conversation_id, text).status_code == 422
