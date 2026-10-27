"""上传结果从回调里读出来的那一步 —— **三处「读错了不报错」**。

1. **`msgData` 不只有一种形状。** 它可能是映射、可能是
   `{subMsgType, subMsgData}` 的数组、也可能没有。直接 `item["msgData"]`
   在数组那一种上什么都读不到 —— **不抛，只是空手而归**，
   于是那一行永远停在「传了但没回来」。某一批就是这么丢掉一整条真实消息的（T201）；

2. **没有 `fileId` 的结果不算结果。** 只写进密钥与大小，目录里那一行会从
   「没传过」变成「传过但发不了」—— 后者更难查，**因为它看上去是齐的**；

3. **`None` 不写。** 只带回三个字段的回调，第四列该保持 NULL 等下一次，
   而不是被一个 `None` 抹掉。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastagent_v2.ingress.wecom_proxy.upload_claim import PROVIDER_FIELDS, upload_result

#: 供应商异步样本里的那一组（`docs/wecom-proxy-cdn-upload-api.md` 逐字）。
SAMPLE: dict[str, Any] = {
    "fileAesKey": "8e1987e241",
    "fileId": "306b0201020460400",
    "fileKey": "8e17ee-dd03-4ee2-91dc-9341",
    "fileMd5": "5c6ebc096b9390da5",
    "fileSize": 2899681,
    "fileThumbSize": 7485,
    "durationTime": 0,
}


def _as_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {"cmd": 20000, "requestId": "r-1", "msgData": dict(data)}


def _as_parts(data: dict[str, Any]) -> dict[str, Any]:
    """同样的内容，装在 `{subMsgType, subMsgData}` 的数组里。"""

    return {
        "cmd": 20000,
        "requestId": "r-1",
        "msgData": [{"subMsgType": 2001, "subMsgData": dict(data)}],
    }


@pytest.mark.parametrize("build", [_as_mapping, _as_parts], ids=["mapping", "parts"])
def test_the_result_is_read_out_of_either_msgdata_shape(build: Any) -> None:
    """**两种形状读出同一组值** —— 这条就是 T201 那个形状。

    数组那一种上直接读 `item["msgData"]` 会空手而归，**而且不抛**：
    目录里那一行于是永远停在「传了但没回来」，没有任何东西变红。
    """

    found = upload_result(build(SAMPLE))

    assert found == {
        "provider_file_id": "306b0201020460400",
        "provider_file_aes_key": "8e1987e241",
        "provider_file_md5": "5c6ebc096b9390da5",
        "provider_file_size": 2899681,
    }


def test_a_result_without_a_file_id_is_not_a_result() -> None:
    """没有 `fileId` 就回 `None` —— 那一行不该从「没传过」变成「传过但发不了」。"""

    without = {key: value for key, value in SAMPLE.items() if key != "fileId"}

    assert upload_result(_as_mapping(without)) is None


def test_a_partial_result_leaves_the_missing_columns_alone() -> None:
    """只带回两个字段就只写两列 —— **没带回的那一列不进这个字典**。

    写一个 `None` 进去会把已有的值抹掉，而下一次回调（如果有）本来还能补上。
    """

    found = upload_result(_as_mapping({"fileId": "f-1", "fileMd5": "m-1"}))

    assert found == {"provider_file_id": "f-1", "provider_file_md5": "m-1"}
    assert "provider_file_aes_key" not in found
    assert "provider_file_size" not in found


@pytest.mark.parametrize("size", ["", "  ", "not-a-number", 0, -5], ids=
                         ["empty", "blank", "text", "zero", "negative"])
def test_an_unusable_size_is_skipped_and_the_rest_is_kept(size: Any) -> None:
    """大小读不成一个正整数就跳过那一列，**不丢掉整条结果**。

    `fileId` 与密钥仍然可用，而 `provider_file_size` 本来就允许为 NULL。
    整条丢掉的代价大得多：那一行发不出去，而原因只是供应商多给了一个空串。
    """

    found = upload_result(_as_mapping({**SAMPLE, "fileSize": size}))

    assert found is not None
    assert "provider_file_size" not in found
    assert found["provider_file_id"] == SAMPLE["fileId"]


def test_the_lowercase_spelling_is_not_read_here_and_that_is_deliberate() -> None:
    """`fileAeskey`（小写 k）**不在这一族里**，所以这里不认它。

    `inbound.py` 把两种拼法都记了下来：普通消息写 `fileAeskey`，
    **API 异步消息写 `fileAesKey`**。这个模块查的是异步那一族 ——
    供应商文档的异步样本里逐字就是大写 K。

    **顺手把小写那个也认了会是一次猜测**：它来自另一条回调路，
    而那条路上的密钥属于**客户发来的媒体**，不是我们上传的资料。
    把它写进目录，等于把一个客户的媒体密钥记成了商家资料的密钥。
    """

    found = upload_result(_as_mapping({"fileId": "f-1", "fileAeskey": "lower"}))

    assert found == {"provider_file_id": "f-1"}


def test_every_column_this_module_writes_is_one_the_migrations_created() -> None:
    """**列名与迁移对得上** —— 写错一个列名的症状是运行时 `UndefinedColumn`，
    而那发生在真实回调回来的时候。

    这里比对的是 `066` 建的那三列加 `065` 的 `provider_file_id`，
    逐字写出来而不是从生产代码里读回来 —— **从被测对象里取判据，
    等于让它自己定义自己对**。
    """

    assert {column for column, _ in PROVIDER_FIELDS} == {
        "provider_file_id",
        "provider_file_aes_key",
        "provider_file_md5",
        "provider_file_size",
    }
