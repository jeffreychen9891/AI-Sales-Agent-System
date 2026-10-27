"""认领一次上传的结果：`cmd=20000` 回来了，它是我们哪一份资料。

## 分流的唯一判据是 `requestId`

`cmd=20000` 不是一种东西 —— 同一个 `cmd` 底下至少有两种结构
（ADR-0126 §三）：我们**上传**一份资料的结果，和我们**取件**一条入站媒体的结果。
两者的 `msgData` 长得几乎一样（都带 `fileAesKey` / `fileId` / `fileMd5`）。

**区分它们的不是字段，是那个 `requestId` 在不在目录里**：
只有我们自己发起的上传才把 `requestId` 写进了 `materials.upload_request_id`
（`065` 那条唯一索引）。取件结果的 `requestId` 一行都匹配不到，认领是个空操作 ——
**这正是想要的**，而且不需要这里多认识一种结构。

## 它不改分类

`classify()` 照旧把这类元素判成 `OUTCOME_IGNORED`（它确实不是客户在说话）。
这里做的是那之后的一件副作用：**回调照样被"答复、记录、不喂给流水线"**，
只是顺带把目录里那一行补齐了。

## 读字段穿过 `message_sources`，不伸手进 `msgData`

`msgData` 可能是一个映射、也可能是 `{subMsgType, subMsgData}` 的数组、
还可能根本没有。`message_sources` 已经知道这三种 —— 而它知道，
是因为某一批假设了映射那一种，**丢掉了一整条真实消息**（T201）。
在这里直接读 `item["msgData"]` 就是把那个假设重新做一遍。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .inbound import message_sources

#: 认领要写的四列，与回调里的字段名一一对应。
#:
#: **`fileAesKey` 的大写 K 是 `cmd=20000` 这一族的拼法**（`inbound.py` 把两种
#: 拼法都记了下来：普通消息写 `fileAeskey`，API 异步消息写 `fileAesKey`）。
#: 这里查的是异步那一族，所以只认大写 K 那个 —— 而这句话是**读出来的，不是推的**：
#: `docs/wecom-proxy-cdn-upload-api.md` 的异步样本里逐字就是 `fileAesKey`。
PROVIDER_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider_file_id", "fileId"),
    ("provider_file_aes_key", "fileAesKey"),
    ("provider_file_md5", "fileMd5"),
    ("provider_file_size", "fileSize"),
)

#: `provider_file_size` 是 `bigint`，其余三列是 `text`。
_NUMERIC_COLUMNS = frozenset({"provider_file_size"})


def upload_result(item: dict[str, Any]) -> dict[str, Any] | None:
    """这个元素带回的上传结果，或者 `None` —— 没有 `fileId` 就不算一个结果。

    **判据是 `fileId` 在不在，不是"有没有任何一个字段"。** 少了它，
    这一行在发送那一刻仍然发不出去（`sendFile` 的必填），
    而目录里那一行会从"没传过"变成"传过但发不了" —— 后者更难查：
    它看上去是齐的。

    **一个 `None` 不写**：只带回三个字段的回调，写进去的那三列是真的，
    没带回的那一列保持 NULL，下一次回调（如果有）还能补。
    把 `None` 写进去会把已有的值抹掉。
    """

    found: dict[str, Any] = {}
    for source in message_sources(item):
        for column, field in PROVIDER_FIELDS:
            if column in found:
                continue
            value = source.fields.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if column in _NUMERIC_COLUMNS:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    # 供应商给了一个不是数的大小。**跳过这一列而不是整条丢掉** ——
                    # `fileId` 与密钥仍然是可用的，而大小这一列本来就允许为 NULL。
                    continue
                if number <= 0:
                    continue
                found[column] = number
            else:
                found[column] = str(value).strip()

    if "provider_file_id" not in found:
        return None
    return found


def claim_upload(
    connection: Any, *, tenant_id: UUID, request_id: str, result: dict[str, Any]
) -> int:
    """把结果写到 `upload_request_id` 匹配的那一行上，回改了几行。

    **`WHERE provider_file_id IS NULL` 是幂等那一半。** 供应商会重投回调，
    而重投一次不该覆盖已经认领好的那一行：改 0 行表示"这次回调来晚了"，
    调用方据此记一笔而不是当成失败。

    **回 0 有两种可能，而这里不去分辨它们**：这个 `requestId` 不是我们发起的
    （取件结果就属于这种，它每次都回 0），或者已经认领过了。
    **分辨它们要读那一行的其余部分，而 ingress 读不到** —— `067` 给它的
    `SELECT` 只有四列（`tenant_id` / `id` / `upload_request_id` /
    `provider_file_id`），slug 与文件名都不在里面。
    也不需要分辨：两种的处置都一样 —— 答复、记一笔、不喂流水线。

    （归档的行**不在**这两种里：它照样认领得到，见
    `tests/integration/test_ingress_claims_uploads.py` 最后一条。
    上传是在它归档之前发起的，拒绝它只会让那次上传成为孤儿。）

    列名来自 `PROVIDER_FIELDS` 这张表，**不在这里第二次写出来**：
    两处各写一遍，迟早一处加了列而另一处没加，而那时症状是某一列永远是 NULL。
    """

    columns = [column for column, _ in PROVIDER_FIELDS if column in result]
    if not columns:
        return 0
    assignments = ", ".join(f"{column} = %({column})s" for column in columns)
    parameters: dict[str, Any] = {column: result[column] for column in columns}
    parameters["tenant"] = tenant_id
    parameters["request"] = request_id
    cursor = connection.execute(
        f"""
        UPDATE materials SET {assignments}
         WHERE tenant_id = %(tenant)s
           AND upload_request_id = %(request)s
           AND provider_file_id IS NULL
        """,  # noqa: S608 - `assignments` 由 PROVIDER_FIELDS 拼出，值全部走参数
        parameters,
    )
    # `connection` 是 `Any`（这个模块不依赖具体的驱动类型），所以 `rowcount`
    # 也是 `Any` —— 显式收成 `int`，否则调用方拿到的 `claimed` 是个
    # 什么都能做的值，而它唯一的用处是 `if claimed:`。
    return int(cursor.rowcount)


def record_upload_failure(
    connection: Any, *, tenant_id: UUID, request_id: str, detail: str | None
) -> int:
    """那次上传失败了，写在对应的那一行上，回改了几行。

    **只改我们自己发起的那一行**：`WHERE upload_request_id = %s` ——
    不是我们发起的（取件结果那一类）匹配不到，回 0，调用方据此什么都不记。
    这与认领用的是同一个判据，所以两边不会对同一条回调各说各话。

    **`provider_file_id IS NULL` 也在 `WHERE` 里**：已经认领成功的那一行不该
    被后到的一条失败回调翻回去。供应商重投是常态，而**一次成功之后的失败
    多半是重投的旧消息**，不是新事实。

    **原因记原文**（`detail` 已在 `envelope_status` 里截断到 200）。
    没有 `detail` 时只盖时点：知道它失败过，比什么都不知道强，
    而编一个原因比不记更坏。
    """

    return int(
        connection.execute(
            """
            UPDATE materials
               SET upload_failed_at = %(now)s, upload_failure_code = %(detail)s
             WHERE tenant_id = %(tenant)s
               AND upload_request_id = %(request)s
               AND provider_file_id IS NULL
            """,
            {
                "now": datetime.now(UTC),
                "detail": detail,
                "tenant": tenant_id,
                "request": request_id,
            },
        ).rowcount
    )

