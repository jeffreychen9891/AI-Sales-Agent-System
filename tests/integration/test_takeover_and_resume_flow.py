"""接管那一族的响应，**与它声明的模型逐键对上** —— 在真库上。

## 它不重复既有那两个文件

`test_conversation_takeover_route.py` 与 `test_conversation_resume_route.py`
已经把**行为**盖得很全：挂起栈、抬 generation、digest 的 CAS、
旧定时器不复活、权限。这里一条行为都不再验。

这里问的是另一件事：**装配出来的那份响应，键集合是不是模型说的那一个。**
批 7 给这五条 route 写了响应模型（ADR-0142），而
`tests/contracts/test_response_models_match_handlers.py` 只读得到
handler 里的**字面量** `return` —— `takeover_projection` 那一格是
两个函数合并出来的，其中 `governance` 那三项是**查库查出来的**，
静态判据一个字也看不见。

## 为什么必须是真库

`_takeover_governance_projection` 读 `audit_events` 与 `policy_decisions`
（迁移 `042` 的窄 SELECT）。给它编几行假数据喂进去，
是把「我以为库里的行长什么样」当成事实 —— 本仓为那种形状付过账。

`takeover_period` 的分支同理：它**按库里有没有那条接管记录**分三种，
而那三种正是前端要分别说人话的地方（「一开始就归人」不是「不知道」）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.api.app import create_app
from fastagent_v2.api.responses import (
    ConversationDetail,
    ResumePreview,
    ResumeResult,
    TakeoverProjectionShape,
    TakeoverResult,
)
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]

#: 接管与交还各要一个 action，详情读要第三个。
WORKING = [
    "conversation_aggregate.read",
    "conversation_takeover.create",
    "conversation_resume.create",
]


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


def _seed(seeded: dict[str, Any]) -> UUID:
    """一条引擎可能产出的会话。**走 `_initial_aggregate`，不手搓状态文档。**"""

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


def _assert_keys(body: dict[str, Any], model: type, where: str) -> None:
    assert set(body) == set(model.model_fields), (  # type: ignore[attr-defined]
        f"{where}：模型说 {sorted(model.model_fields)}，"  # type: ignore[attr-defined]
        f"而 route 给 {sorted(body)}"
    )


def test_the_detail_response_matches_its_model_on_a_real_database(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """`GET /conversations/{id}` —— 十四个键，**包括查库查出来的那一格**。

    批 6 把这个模型留给了本批，理由是「没有消费方的模型是一句没人会
    发现它错了的话」。接管页现在读它，所以它进表了 —— 而进表就要有
    这一条：**装配出来的那份响应到底长什么样**。
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)

    answer = client.get(f"/conversations/{conversation_id}", headers=headers)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    _assert_keys(body, ConversationDetail, "GET /conversations/{id}")
    _assert_keys(
        body["takeover_projection"], TakeoverProjectionShape, "takeover_projection"
    )


def test_the_preview_and_its_projection_match_their_models(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """交还预览 —— **`governance` 那三项只有在真库上才装得齐。**"""

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)
    taken = client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert taken.status_code == 200, taken.text
    _assert_keys(taken.json()["result"], TakeoverResult, "takeover 的 result")

    preview = client.get(
        f"/conversations/{conversation_id}/resume-preview", headers=headers
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    _assert_keys(body, ResumePreview, "resume-preview")
    _assert_keys(
        body["takeover_projection"], TakeoverProjectionShape, "takeover_projection"
    )


def test_the_takeover_period_says_which_of_the_three_it_is(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**三种答案，不是两种** —— 而前端要为它们说三句不同的人话。

    没人接管过是 `none`；接管过是 `takeover` 并带一个时刻。
    第三种（`conversation_start`，这通对话一出生就归人）要租户策略
    把新会话直接交给人，**本条不造那个策略** —— 它由
    `_takeover_period` 自己的 docstring 与那条策略的用例覆盖，
    这里只钉「这一格总是说得出自己是哪一种」。
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)

    before = client.get(f"/conversations/{conversation_id}", headers=headers).json()
    period_before = before["takeover_projection"]["takeover_period"]
    assert period_before["source"] == "none"
    assert period_before["started_at"] is None

    client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )

    after = client.get(f"/conversations/{conversation_id}", headers=headers).json()
    period_after = after["takeover_projection"]["takeover_period"]
    assert period_after["source"] == "takeover"
    assert isinstance(period_after["started_at"], str), (
        "接管过就必须给得出从什么时候算起 —— 一个 None 会让界面显示"
        "「还没有人接管过」，而那与屏幕上「归你」那个标签直接打架"
    )


def test_the_resume_result_matches_its_model(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """交还的 `result` —— **`timers_restored` 在模型里，而它恒为 0。**

    那个 0 是 ADR-0054 §三.4 的一条裁定（旧定时器是照着一份已经不存在的
    计划铸的），不是占位符。它在模型里，界面才能把这件事说出来。
    """

    client, headers = _client_for(settings, seeded_database, WORKING)
    conversation_id = _seed(seeded_database)
    client.post(
        f"/conversations/{conversation_id}/takeover",
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    digest = client.get(
        f"/conversations/{conversation_id}/resume-preview", headers=headers
    ).json()["change_digest"]

    resumed = client.post(
        f"/conversations/{conversation_id}/resume",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={"acknowledged_change_digest": digest, "invalidate_summary": False},
    )

    assert resumed.status_code == 200, resumed.text
    _assert_keys(resumed.json()["result"], ResumeResult, "resume 的 result")
    assert resumed.json()["result"]["timers_restored"] == 0
