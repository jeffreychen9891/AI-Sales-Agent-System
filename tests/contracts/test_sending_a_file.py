"""发一份文件这条路上的五处「错了不报错」。

| 处 | 错了会怎样 |
|---|---|
| `fileAesKey` 条件必填 | 供应商**回成功**，而收件人打不开 |
| 「大」的判据 | 用字节数判会立第二个定义，两个定义迟早不一致 |
| 出站行带凭据 | 凭据进读面更宽的表，**没有任何东西变红** |
| 版本按引用取 | 发得出去、回成功、**内容是旧版本** |
| 适配器没接上文件 | `AttributeError` 打穿派发循环 |

前三处与最后一处在本文件；「版本按引用取」在派发那一层，
由 `tests/integration/test_sending_a_material.py` 连库验。
「出站行带凭据」另有一条在 `test_generation_write_path_registry.py`。

**最后一条尤其**：端口的契约是「失败是回执不是异常」，
因为派发循环对异常唯一的答复是让进程掉线 —— 而那一行本来只该是一次投递失败。
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.channels.wecom_proxy.allowlist import GuidAllowlist
from fastagent_v2.channels.wecom_proxy.client import (
    RestrictedWeComProxyClient,
    WeComProxyCredentials,
    WeComProxyError,
    is_big_file,
)
from fastagent_v2.worker.channel import FILE_CONTENT_TYPE, SendCommand, customer_file
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter

REAL_GUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
# **不是模块级常量，而这一格是本仓踩过的。**
#
# 它原本写成 `_deadline() = datetime.now(UTC) + timedelta(seconds=30)` ——
# 一个**在 import（collection）那一刻**就固定下来的时刻。整棵树跑起来时，
# 从收集到这条用例真正执行之间超过那个窗口，它就已经过期了，
# 于是被测代码走的是「时间不够」那条分支，而用例读起来像是逻辑错了。
#
# 守卫在 `tests/contracts/test_no_import_time_wall_clock.py`，登记 T380。
def _deadline() -> datetime:
    """调用时现取。理由见上。"""

    return datetime.now(UTC) + timedelta(seconds=30)

SMALL_FILE = {
    "type": FILE_CONTENT_TYPE,
    "file_id": "306b0201020460400",
    "file_size": 2899681,
    "file_md5": "5c6ebc096b9390da5",
    "filename": "2026 价格表.pdf",
    "aes_key": "8e1987e241",
}


def _client(directory: str, transport: Any) -> RestrictedWeComProxyClient:
    allowlist = GuidAllowlist(Path(directory) / "allowlist.json")
    allowlist.record(REAL_GUID, device_name="probe")
    client = RestrictedWeComProxyClient(
        credentials=WeComProxyCredentials(
            base_url="https://provider.example.test",
            app_id="app-id-placeholder",
            app_secret="app-secret-placeholder",
        ),
        allowlist=allowlist,
        audit=lambda record: None,
        confirm=lambda operation, details: True,
        transport=transport,
    )
    client._token = "test-token"  # noqa: SLF001
    client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001
    return client


def test_a_small_file_without_a_key_is_refused_before_anything_is_sent() -> None:
    """**没有密钥的小文件不发出去** —— 判据是 `transport` 有没有被碰过。

    供应商的 `required` 里没有 `fileAesKey`，所以它**会收下这一请求并回成功**，
    而收件人拿到一份解不开的文件。断言「抛了异常」区分不出
    「拦下了」与「拦之前已经发出去了」。
    """

    calls: list[str] = []

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        calls.append(path)
        return {"code": 0, "data": {"msgId": 1}}

    with tempfile.TemporaryDirectory() as directory:
        client = _client(directory, transport)
        with pytest.raises(WeComProxyError) as raised:
            client.send_file(
                guid=REAL_GUID,
                recipient_id="cust-1",
                file_id="306b02010204",
                file_size=1024,
                file_md5="m",
                filename="a.pdf",
                file_aes_key=None,
            )

    assert calls == [], "没有密钥的小文件已经发出去了 —— 而供应商会回成功"
    assert "fileAesKey" in str(raised.value)


def test_a_big_file_may_go_without_a_key_and_the_field_is_then_absent() -> None:
    """大文件那一档允许没有密钥，**而那时这个键整个不出现**。

    送 `"fileAesKey": ""` 与不送不是一回事：供应商在那一档把它记为「可为空」，
    而一个空串是下一个读抓包的人会当成真值的东西。
    """

    seen: dict[str, Any] = {}

    def transport(path: str, payload: dict[str, Any], token: str, **_: object) -> dict[str, Any]:
        seen.update(payload)
        return {"code": 0, "data": {"msgId": 1}}

    with tempfile.TemporaryDirectory() as directory:
        _client(directory, transport).send_file(
            guid=REAL_GUID,
            recipient_id="cust-1",
            file_id="*fileConvertId-0001",
            file_size=99_000_000,
            file_md5="m",
            filename="big.mp4",
            file_aes_key=None,
        )

    assert "fileAesKey" not in seen, f"大文件那一档送了一个空密钥：{seen}"
    assert seen["fileId"] == "*fileConvertId-0001"


@pytest.mark.parametrize(
    ("file_id", "big"),
    [("*abc", True), ("306b0201", False), ("", False), (" *abc", False)],
    ids=["convert-id", "ordinary", "empty", "leading-space"],
)
def test_big_is_decided_by_the_file_id_and_nothing_else(file_id: str, big: bool) -> None:
    """**判据是 `fileId` 的形状，不是字节数。**

    `" *abc"` 回 `False` 是有意的：供应商给的是一个以 `*` 开头的 id，
    带前导空格的那个不是它 —— 而「宽容地 strip 一下」等于悄悄接受一个
    我们没见过的形状。
    """

    assert is_big_file(file_id) is big


def test_an_adapter_without_the_file_path_answers_with_a_receipt_not_a_crash() -> None:
    """**没接上文件那条路时是一条回执** —— 端口的契约。

    派发循环对异常唯一的答复是让进程掉线，而这一行本来只该是一次投递失败。
    """

    adapter = WeComProxyChannelAdapter(
        send_text=lambda **_: {}, guid=REAL_GUID, recipient_id="cust-1"
    )

    receipt = adapter.send(
        command=SendCommand(
            tenant_id="t", conversation_id="c", content=dict(SMALL_FILE), recipient=None
        ),
        idempotency_key="send_material:e1:0",
        deadline_at=_deadline(),
    )

    assert receipt.status == "failed"
    assert receipt.evidence["reason"] == "file_sending_not_wired"


def test_the_adapter_sends_the_file_when_the_path_is_wired() -> None:
    """接上之后它**真的走文件那条路**，而不是回落到文字。"""

    sent: dict[str, Any] = {}

    def send_file(**kwargs: Any) -> dict[str, object]:
        sent.update(kwargs)
        return {"msgId": 4242}

    adapter = WeComProxyChannelAdapter(
        send_text=lambda **_: {"msgId": 1},
        send_file=send_file,
        guid=REAL_GUID,
        recipient_id="cust-1",
    )

    receipt = adapter.send(
        command=SendCommand(
            tenant_id="t", conversation_id="c", content=dict(SMALL_FILE), recipient=None
        ),
        idempotency_key="send_material:e1:0",
        deadline_at=_deadline(),
    )

    assert receipt.status == "accepted"
    assert sent["file_id"] == SMALL_FILE["file_id"]
    assert sent["filename"] == SMALL_FILE["filename"]


def test_an_unresolved_asset_row_never_reaches_the_provider() -> None:
    """**库里那个只带引用的形状到不了适配器，而万一到了，它不发。**

    派发点会把它换成解析后的那份。没换成（哪天有人加了一条绕过解析的路），
    这里回 `None` → 一条 `unsendable_content_type` 的失败回执 ——
    好过带着一个 `asset:price-list:v1` 字样发给客户。
    """

    assert customer_file({"type": "asset", "ref": "asset:price-list:v1"}) is None


@pytest.mark.parametrize(
    "broken",
    [
        {**SMALL_FILE, "file_id": ""},
        {**SMALL_FILE, "file_md5": ""},
        {**SMALL_FILE, "filename": ""},
        {**SMALL_FILE, "file_size": 0},
        {**SMALL_FILE, "file_size": "not-a-number"},
    ],
    ids=["no-id", "no-md5", "no-filename", "zero-size", "text-size"],
)
def test_a_half_resolved_file_is_not_sendable(broken: dict[str, Any]) -> None:
    """缺任何一个必填就不是一份能发的文件 —— **在这里回 `None`，不在供应商那里换一句 code**。"""

    assert customer_file(broken) is None
