"""上传那一次调用的形状：**四个字段、一个 `requestId`、而没有 id 就是失败**。

## 这条路上「传错了不报错」的地方有三个

本仓那条纪律（`CLAUDE.md` 三）说的是：**传错了或多传了不报错、而是安静地给出一个
看起来正常的结果**的地方，必须有一条会红的用例。这个接口一次占了三处：

1. **同步响应里只有 `requestId`** —— 没有 `fileId`、没有密钥、没有大小。
   把它当成「上传结果」去读 `fileId`，拿到的是 `None`，
   **而 HTTP 是 200、没有任何东西报错**。所以方法回的是那一个字符串，
   不是一个 dict —— 让这个错误**写不出来**比让它变红更早一步；
2. **`requestId` 为空** —— 那份文件在供应商那边照样传了，而回调回来时
   没有任何东西认领得了它。安静地存一个空串，等于目录里那一行永远停在
   「传了但没回来」；
3. **忘了过 guid 白名单** —— 新加一个操作时最容易漏的一步。漏了不会报错：
   它会带着任何一个 guid 把请求发出去，而那个 guid 可能根本不是我们的连接。

三条各有一条用例。
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.channels.wecom_proxy.allowlist import (
    GuidAllowlist,
    GuidNotAllowedError,
    normalize_guid,
)
from fastagent_v2.channels.wecom_proxy.client import (
    RestrictedWeComProxyClient,
    WeComProxyCredentials,
    WeComProxyError,
)

#: 供应商形状的 guid（`GuidAllowlist` 只认十六进制与连字符）。
REAL_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

REQUEST_ID = "73226010-fe3c-42c0-b960-191c9c2d93f0"


def _client(
    directory: str, transport: Any, *, records: list[dict[str, Any]] | None = None
) -> RestrictedWeComProxyClient:
    allowlist = GuidAllowlist(Path(directory) / "allowlist.json")
    allowlist.record(REAL_GUID, device_name="probe")
    client = RestrictedWeComProxyClient(
        credentials=WeComProxyCredentials(
            base_url="https://provider.example.test",
            app_id="app-id-placeholder",
            app_secret="app-secret-placeholder",
        ),
        allowlist=allowlist,
        audit=(records.append if records is not None else (lambda record: None)),
        confirm=lambda operation, details: True,
        transport=transport,
    )
    client._token = "test-token"  # noqa: SLF001
    client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001
    return client


def test_the_call_carries_exactly_the_four_fields_the_vendor_declares_required() -> None:
    """`guid` / `filename` / `fileUrl` / `fileType`，一个不少，也不多塞。

    多塞一个**是这条纪律的正形状**：schema 不会拦，供应商多半照样回 200，
    而读词表的下一个人会以为那个字段有意义。
    """

    seen: dict[str, Any] = {}

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        seen["path"] = path
        seen["payload"] = payload
        return {"code": 0, "data": {"requestId": REQUEST_ID}}

    with tempfile.TemporaryDirectory() as directory:
        returned = _client(directory, transport).upload_file_by_url(
            guid=REAL_GUID,
            filename="2026 价格表.pdf",
            file_url="https://files.example.test/price-list-v1.pdf",
            file_type=5,
        )

    assert seen["path"] == "/cloud/cdnUploadByUrlAsync"
    # **线上带的是规范化之后的 guid**，不是调用方递进来的那个字符串。
    # 这正是这一步走 `_guarded_guid` 而不是直接用参数的意义：白名单既是许可，
    # 也是**唯一的拼写权威** —— 两处各自规范化迟早会有一处漏掉，
    # 而那时的症状是供应商回一句「找不到这个连接」，不是我们这边报错。
    assert seen["payload"] == {
        "guid": normalize_guid(REAL_GUID),
        "filename": "2026 价格表.pdf",
        "fileUrl": "https://files.example.test/price-list-v1.pdf",
        "fileType": 5,
    }
    assert returned == REQUEST_ID


def test_the_return_value_is_the_request_id_not_the_response_body() -> None:
    """**回一个字符串，不是 dict** —— 让「读 `fileId`」这个错误写不出来。

    同步那一半没有 `fileId`：真正的结果走 `cmd=20000` 回调。一个 dict 型的返回值
    会让调用方 `data.get("fileId")` 拿到 `None` 并存进去，而 HTTP 是 200。
    """

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        # 供应商真回的那个信封里就是没有 fileId —— 这里照着它写。
        return {"code": 0, "data": {"requestId": REQUEST_ID}}

    with tempfile.TemporaryDirectory() as directory:
        returned = _client(directory, transport).upload_file_by_url(
            guid=REAL_GUID, filename="a.pdf", file_url="https://x.test/a.pdf", file_type=5
        )

    assert isinstance(returned, str), (
        "上传回了一个非字符串 —— 只要它是个映射，调用方就写得出 `.get('fileId')`，"
        "而那一句永远拿到 None 且永远不报错"
    )


@pytest.mark.parametrize(
    ("data", "why"),
    [
        ({}, "根本没有 requestId"),
        ({"requestId": ""}, "requestId 是空串"),
        ({"requestId": "   "}, "requestId 只有空白"),
    ],
    ids=["missing", "empty", "blank"],
)
def test_an_upload_with_no_request_id_is_refused_not_returned(
    data: dict[str, Any], why: str
) -> None:
    """没有 id 就等于**认领不回来**，所以抛，而不是回一个空串。

    文件在供应商那边照样传了。存一个空串下去，回调回来时没有任何一行认领得了它，
    而目录里那一行看上去像「从没传过」—— 下一次跑脚本会再传一遍。
    """

    records: list[dict[str, Any]] = []

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        return {"code": 0, "data": data}

    with tempfile.TemporaryDirectory() as directory:
        client = _client(directory, transport, records=records)
        with pytest.raises(WeComProxyError) as raised:
            client.upload_file_by_url(
                guid=REAL_GUID, filename="a.pdf", file_url="https://x.test/a.pdf", file_type=5
            )

    assert "requestId" in str(raised.value), f"报文没说清是 requestId 的问题（{why}）"
    outcomes = [record.get("outcome") for record in records]
    assert "ok" not in outcomes, (
        f"没有 requestId 的上传被记成了 ok（{why}）—— 审计里一条 ok 会让"
        "排障的人以为这份资料传出去了"
    )
    assert "no_request_id" in outcomes, f"审计里没有留下这次失败（{why}）"


def test_a_guid_outside_the_allowlist_never_reaches_the_wire() -> None:
    """**新操作最容易漏的一步**：漏了不报错，它会带着任何 guid 把请求发出去。

    这条用例不看返回值，看的是 `transport` **有没有被碰过** —— 断言「抛了异常」
    区分不出「白名单拦下了」和「拦之前已经发出去了」。
    """

    calls: list[str] = []

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        calls.append(path)
        return {"code": 0, "data": {"requestId": REQUEST_ID}}

    with tempfile.TemporaryDirectory() as directory:
        client = _client(directory, transport)
        with pytest.raises(GuidNotAllowedError):
            client.upload_file_by_url(
                guid="00000000-0000-0000-0000-000000000000",
                filename="a.pdf",
                file_url="https://x.test/a.pdf",
                file_type=5,
            )

    assert calls == [], (
        "白名单外的 guid 已经把请求发出去了 —— `_guarded_guid` 漏了，"
        "而漏掉它的症状是一条成功的上传，不是一个错误"
    )
