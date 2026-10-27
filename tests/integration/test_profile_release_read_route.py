"""`GET /profiles/releases/{id}` —— 在真库上问四件投影守卫问不到的事。

`tests/contracts/test_profile_release_projection.py` 钉的是**那个函数**：
给了哪些键、没给哪些。它够不到的是这条 route 作为一条 route 的行为 ——
谁能问、问不到的时候回什么、**别的租户的 release id 问过来回什么**。

最后那一条是这里最要紧的：一条「你没权限看它」与一条「没有这个东西」
在语义上差着一个**跨租户的存在性探针**。RLS 让两种情况一模一样地到达
handler，所以这条用例验的是那个设计真的成立，而不是注释里写着它成立。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.api.app import create_app
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]

#: ADR-0143 §二 点名不给的四项。
WITHHELD = (
    "policy_bindings",
    "confidence_gate_policy_ref",
    "manipulation_context_refs",
    "golden_manifest",
)


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


def _a_release_only_tenant_b_has(settings: Settings, seeded: dict[str, Any]) -> UUID:
    """给 B 租户发**另一个 package**，走的是生产那条路（publisher）。

    ## 这个辅助函数写错过两次，两次都是夹具的形状和我想的不一样

    **第一次**：「给 B 发一版 A 的文档」→ `UniqueViolation`。
    `seeded_database` **给两个租户都发了**同一个 package，我以为它只给 A 发。

    **第二次**：「读 B 已经有的那一行」→ 读出来的 id 和 A 的**一模一样**。
    `publish_release` 的 release id 来自 **package 自己的
    `identity.release_id`**，而 `profile_releases` 的主键是
    `(tenant_id, id)` —— 同一个包在两个租户下同号，完全合法。

    ⚠️ **那条 `!=` 断言是给读失败报告的人看的**：没有它，这个辅助函数
    会安静地把 **A 自己的 id** 递回去，而 A 读自己的当然是 200 ——
    用例照样红，**但红出来的样子是「这条 route 把别的租户的话术发出去了」**。
    一条指错方向的红比不红更贵：会有人去查 RLS，而 RLS 没问题。

    所以这里发的是**另一个 package** —— 它有自己的 `identity.release_id`，
    于是那个 id 只有 B 有、A 没有。仍然走 publisher：
    **RLS 拦的是真实写入路径产生的数据**，一条手写的 INSERT 验不出这一点。

    **第三次**（合并树上）：写死 `("internal-demo-sales", "1.2.4")` ——
    主作者同一天把那个包抬成了 **1.2.5**。两边各自的门禁都绿，
    **合起来才红**：我的夹具钉在了一个别人随时会动的数上，
    而 `profiles/` 下的 demo 包不归这条用例管。
    所以这里按**性质**挑：任何一个 release id 与 A 那一版不同的包都行。
    """

    seeded_release = UUID(str(seeded["profile_release"]))
    registry = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
    for package in registry.packages():
        if UUID(str(package.document["identity"]["release_id"])) != seeded_release:
            break
    else:
        raise AssertionError(
            "`profiles/` 里每一个包的 release id 都等于夹具给 A 发的那一版 —— "
            "那样就造不出「只有 B 有」的 id，这条用例问的也就不是跨租户了。"
        )

    published = publish_release(
        database=Database(settings.database_url),
        tenant_id=seeded["tenant_b"],
        package=package,
    )
    release_id = published.release.release_id
    assert release_id != seeded_release, (
        "两个租户的 release id 必须不同，否则这条用例问的根本不是跨租户"
    )
    return release_id


def test_reading_a_release_without_the_permission_is_refused(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """没有 `profile_release.read` 就看不了 —— 与列表同一个 action（ADR-0143 §二）。"""

    client, headers = _client_for(settings, seeded_database, [])
    refused = client.get(
        f"/profiles/releases/{seeded_database['profile_release']}", headers=headers
    )
    assert refused.status_code == 403, refused.text


def test_an_unknown_release_is_not_found(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])
    missing = client.get(f"/profiles/releases/{uuid4()}", headers=headers)
    assert missing.status_code == 404, missing.text
    assert missing.json()["code"] == "PROFILE_RELEASE_NOT_FOUND"


def test_another_tenants_release_is_not_found_rather_than_forbidden(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**404，和一个根本不存在的 id 一模一样。**

    403 会说出「这个 id 是真的，只是不归你」——那就是一个跨租户的存在性
    探针，而 release id 不是随机到问不出来的东西（它进过别人的 URL、
    进过别人的截图）。RLS 让 A 看 B 的行与看一个空 id 在 handler 眼里
    完全相同，所以这条用例同时也在验 RLS 真的拦住了。
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])
    other = _a_release_only_tenant_b_has(settings, seeded_database)

    answer = client.get(f"/profiles/releases/{other}", headers=headers)

    assert answer.status_code == 404, answer.text
    assert answer.json()["code"] == "PROFILE_RELEASE_NOT_FOUND"


def test_the_merchant_half_comes_back_and_the_platform_half_does_not(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """走完整条 HTTP 路径，再问一次那四项在不在。

    投影守卫已经问过同样的话了 —— **这里再问一次不是重复**：
    中间还隔着 FastAPI 的序列化，而 `responses=` 那一型是只做文档、
    不参与运行期过滤的（ADR-0142）。如果哪天有人把它改成
    `response_model`，一个写漏字段的模型会**安静地删键**，
    而那一刻只有走真路径的用例看得见。
    """

    client, headers = _client_for(settings, seeded_database, ["profile_release.read"])

    answer = client.get(
        f"/profiles/releases/{seeded_database['profile_release']}", headers=headers
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["release_id"] == str(seeded_database["profile_release"])
    assert body["target_frames"] != []
    for key in WITHHELD:
        assert key not in body, f"ADR-0143 §二 点名不给的 {key} 出现在响应里"
    # 话术正文不该进缓存 —— 与别的租户敏感读同口径。
    assert answer.headers["cache-control"] == "no-store, private"
