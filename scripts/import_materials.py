"""把人工整理好的资料目录收进 `materials`。**全仓第一个 `INSERT INTO materials`。**

## 它为什么是一个脚本而不是一条 route

**ADR-0130 E1 照旧成立**：素材由人工放置，**没有接收字节的 route**。
这个脚本一个字节都不接收 —— 它写的是一份目录：这份资料叫什么、多大、
什么类型、**去哪儿拉**。字节在那个地址后面，由供应商自己去取
（`cdnUploadByUrlAsync` 的请求体里只有 `fileUrl`）。

**E2 也照旧**：不做对象存储、不做病毒扫描。`source_url` 指向哪里是运维选择 ——
商家自有的地址、一台静态文件服务、或者别的，这张表不管。

## 它连的是 `fastagent_app`，不是迁移角色

与 `import_knowledge.py` 逐字同源的理由：迁移角色是超级用户，**整个绕过 RLS**，
用它导入等于那条隔离策略一次都不会被走到。`065` 为此发出 `materials` 上的第一条
`INSERT` 授权，调用方就是这个脚本。

## 输入形状

一份 JSON，顶层 `{"materials": [ ... ]}`，每条五个必填字段：

    {
      "slug": "price-list",          # 与 asset:<slug>:v<N> 的第二段一致
      "version": 1,                  # 内容改一个字就 +1，**永不复用**
      "filename": "2026 价格表.pdf",
      "media_type": "application/pdf",
      "source_url": "https://…/price-list-v1.pdf",
      "byte_size": 182345,           # 可选：给了就用，没给就去 HEAD 一次
      "content_hash": "…"            # 可选：同上，没给就下载一次算
    }

**`byte_size` 与 `content_hash` 为什么可以省**：那两个数要么整理的人已经有，
要么只能从那个地址取一次。**脚本不替你猜** —— 省了它就去取，取不到就拒，
而不是写一个 0 进去。

## 退出码

`0` = 全部写进去了（或已存在且内容一致）· `1` = 有条目被拒（逐条说明理由）·
`2` = 连不上、没有那张表、或者 `--file` 读不了。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[1]
for part in ("packages/runtime/src", "services/api/src", "services/worker/src"):
    sys.path.insert(0, str(REPO / part))

from fastagent_v2.runtime.references import reference_refusal  # noqa: E402

#: 必填五项。`byte_size` / `content_hash` 不在里面 —— 见模块 docstring。
REQUIRED_FIELDS = ("slug", "version", "filename", "media_type", "source_url")

#: 媒体类型 → 供应商 `fileType`（**`cdnUploadByUrlAsync` 这一条路**）。
#:
#: 供应商文档只写了三个值（`1: jpg图片, 4: mp4视频, 5: 文件(也包括语音silk文件)`），
#: 而 **v1 生产代码里实测用过第四个**：大视频走 `22`（见 `BIG_VIDEO_FILE_TYPE`）。
#: 用户 2026-09-16 裁定：**按 v1 实测补，不按文档的三值封闭**。
#:
#: ## ⚠️ v1 还用过 `2` 与 `3`，而它们**不属于这张表**
#:
#: 逐处核过 v1 的调用点（`backend/app/integrations/wechat_channel.py`）：
#:
#: | fileType | v1 走的方法 | 是不是这条路 |
#: |---|---|---|
#: | 1 / 4 / 5 | `upload_file_by_url` | ✅ 就是这条 |
#: | **2（图片）· 3（语音）** | **`c2c_upload`**（先 `get_cdn_info` 再传） | ❌ **另一个端点** |
#: | 22 | `upload_big_file_multipart`（`/cloud/cdnBigFileUpload`） | ✅ 同一族，大文件那一档 |
#:
#: **两套词表属于两个端点**，混成一张会让一份图片带着 `2` 发去
#: `cdnUploadByUrlAsync` —— 而那个失败只有 `code`/`msg`，没有错误码表。
#:
#: **这张表在这里，不在上传客户端里**：导入时就要判「这个 media_type 供应商收不收」，
#: 否则一份收不了的资料会一路躺到发送那一刻才失败 —— 而那时客户已经在等了。
PROVIDER_FILE_TYPES: Mapping[str, int] = {
    "image/jpeg": 1,
    "image/jpg": 1,
    "video/mp4": 4,
    "application/pdf": 5,
    "application/msword": 5,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 5,
    "application/vnd.ms-excel": 5,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": 5,
    "text/plain": 5,
}

#: 大视频那一档：**超过这个大小的 mp4 走 `22`，不走 `4`**。
#:
#: 两个数都是 **v1 生产代码实测**（`wecom_proxy_client.upload_video_file`）：
#: `> 20 * 1024 * 1024` 时改走 `/cloud/cdnBigFileUpload` 并把 `fileType` 换成 `22`。
#: **供应商文档里没有这两个数** —— 这也是为什么它们标着来源：
#: 下一个人要改它时，得知道这不是谁拍的，是从一份跑过生产的实现里读出来的。
BIG_VIDEO_FILE_TYPE = 22
BIG_VIDEO_THRESHOLD_BYTES = 20 * 1024 * 1024

FETCH_TIMEOUT_SECONDS = 30


def provider_file_type(media_type: str, byte_size: int) -> int:
    """这份资料送给供应商时的 `fileType`。**唯一定义点。**

    上传那一批会照着它选端点（`22` 那一档走 `cdnBigFileUpload`），
    所以「选哪个值」只能有一处 —— 两处各写一遍，迟早一处补了大视频那一档、
    另一处没补，而那次失败发生在客户等着的时候。
    """

    base = PROVIDER_FILE_TYPES[media_type]
    if base == 4 and byte_size > BIG_VIDEO_THRESHOLD_BYTES:
        return BIG_VIDEO_FILE_TYPE
    return base


def unsupported_tier_refusal(file_type: int) -> str | None:
    """这个 `fileType` 今天有没有人在走。`None` = 有。

    ## 它防的是一条**安静**的路

    `provider_file_type()` 在 20MB 两侧真的给出不同的值（`4` / `22`），
    **而 `22` 之后走的还是唯一那个端点** ——
    `upload_file_by_url` → `/cloud/cdnUploadByUrlAsync`。
    `upload_big_file_multipart`（`/cloud/cdnBigFileUpload`）**全仓 0 个实现**。

    于是今天这条路长这样：算得出 `22` · 发得出去 · 等一个永远不会成功的回调。
    运维 2026-09-17 实测那一次，**供应商把整份 48MB 拉走了才拒**
    （access.log 有那条 `200 48219856`），而我们这边只看到一个
    `500100 文件上传限制:20MB`。

    ## 它不是那个端点的实现，也不假装是

    用户 2026-09-18 裁定**不实现** `cdnBigFileUpload`（演示改用 ≤20MB 的片子）。
    **换片是把那一次的路让开，不是把这一格关上** —— 下一份 >20MB 的文件
    还会撞同一堵墙，而在这个函数之前**没有任何东西会在那之前变红**。

    **登记见 T362。** 端点补上的那天，这个函数连同它那两个调用点一起删。
    """

    if file_type != BIG_VIDEO_FILE_TYPE:
        return None
    return (
        f"这份要走 fileType {BIG_VIDEO_FILE_TYPE}（> "
        f"{BIG_VIDEO_THRESHOLD_BYTES // (1024 * 1024)}MB 的视频），"
        "而那一档要换端点（`/cloud/cdnBigFileUpload`），**本仓今天没有实现**。"
        "照现在这条路传出去，供应商会把整份文件拉完再回 "
        "`500100 文件上传限制:20MB`。先把文件压到门槛以下，或见 T362。"
    )


def entry_refusals(entry: Mapping[str, Any]) -> list[str]:
    """一条目录不合规的**全部**理由，合规则空列表。

    **返回全部而不是第一条**，与 `knowledge_authoring.chunk_refusals` 同一个理由：
    整理的人改一遍就想全改完。
    """

    found: list[str] = []

    for field in REQUIRED_FIELDS:
        value = entry.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            found.append(f"`{field}` 缺失或为空；五个字段都要有值")

    slug = str(entry.get("slug") or "")
    version = entry.get("version")
    if slug and isinstance(version, int):
        # **ref 的合法性调唯一定义点，不在这里重写一遍正则。**
        refusal = reference_refusal(f"asset:{slug}:v{version}", expected_kind="asset")
        if refusal is not None:
            found.append(f"asset:{slug}:v{version} —— {refusal}")
    elif version is not None and not isinstance(version, int):
        found.append(f"`version` 要是整数，给的是 {type(version).__name__}")

    media_type = str(entry.get("media_type") or "")
    if media_type and media_type not in PROVIDER_FILE_TYPES:
        found.append(
            f"`media_type` 是 {media_type!r}，而这条上传路的 `fileType` 只有"
            f" 图片 / 视频 / 文件 三类（大视频是视频那一类按大小换的档，不是第四类）；"
            f"本仓认得的写法：{sorted(PROVIDER_FILE_TYPES)}"
        )

    source_url = str(entry.get("source_url") or "")
    if source_url and not source_url.startswith(("http://", "https://")):
        found.append("`source_url` 要是一个 http(s) 地址 —— 供应商要自己去拉它")

    # **目录自己报了大小的，在这里就拒**（T362）。
    #
    # 这条与上面那条 `media_type` 判据同一个理由，那条注释写着：
    # 「导入时就要判『这个 media_type 供应商收不收』，否则一份收不了的资料
    # 会一路躺到发送那一刻才失败 —— 而那时客户已经在等了。」
    # **大小是同一个问题的另一半**：收不收不只看类型，还看多大。
    #
    # 目录没报大小的那些在 `import_entries` 里量完再判 —— 真身只有那时才知道。
    byte_size = entry.get("byte_size")
    if media_type in PROVIDER_FILE_TYPES and isinstance(byte_size, int):
        refusal = unsupported_tier_refusal(provider_file_type(media_type, byte_size))
        if refusal is not None:
            found.append(refusal)

    return found


def measure(source_url: str) -> tuple[int, str]:
    """去那个地址取一次，回 `(字节数, sha256)`。

    **取整份而不是只 HEAD**：`content_hash` 只能从内容算，而 `byte_size`
    要与它来自同一次读取 —— 分两次取会得到一对描述两个版本的数。
    """

    with urllib.request.urlopen(source_url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        body = response.read()
    return len(body), hashlib.sha256(body).hexdigest()


def import_entries(
    connection: Connection[Any], *, tenant_id: UUID, entries: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    """把合规的那些写进去。**这是全仓唯一一处 `INSERT INTO materials`。**

    重跑同一份 JSON 不该写第二遍：`(tenant_id, slug, version)` 已经在的那些
    直接跳过 —— 目录是版本化的，同一个版本的内容不该有两行。
    """

    counts = {"inserted": 0, "skipped": 0, "measured": 0, "refused": 0}
    for entry in entries:
        slug = str(entry["slug"])
        version = int(entry["version"])
        existing = connection.execute(
            "SELECT id FROM materials WHERE tenant_id = %s AND slug = %s AND version = %s",
            (tenant_id, slug, version),
        ).fetchone()
        if existing is not None:
            counts["skipped"] += 1
            continue

        byte_size = entry.get("byte_size")
        content_hash = entry.get("content_hash")
        if byte_size is None or content_hash is None:
            byte_size, content_hash = measure(str(entry["source_url"]))
            counts["measured"] += 1

        # **量完再判一次**（T362）。目录没报大小时，真身只有这一刻才知道 ——
        # 而 `entry_refusals` 那一道只看得见目录里写着的数。
        #
        # **跳过而不是抛**：一份进不去的资料不该把同一份目录里其余的也带走
        # （与 `entry_refusals` 返回全部理由同一个理由：整理的人改一遍就想全改完）。
        refusal = unsupported_tier_refusal(
            provider_file_type(str(entry["media_type"]), int(byte_size))
        )
        if refusal is not None:
            print(f"拒 {slug}:v{version} —— {refusal}")
            counts["refused"] += 1
            continue

        connection.execute(
            """
            INSERT INTO materials(
                tenant_id, id, slug, version, filename, media_type,
                byte_size, content_hash, status, source_url
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
            """,
            (
                tenant_id,
                uuid4(),
                slug,
                version,
                str(entry["filename"]),
                str(entry["media_type"]),
                int(byte_size),
                str(content_hash),
                str(entry["source_url"]),
            ),
        )
        counts["inserted"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="把人工整理好的资料目录收进 materials")
    parser.add_argument("file", help="目录 JSON，顶层 {\"materials\": [...]}")
    parser.add_argument("--tenant", required=True, help="租户 uuid")
    arguments = parser.parse_args()

    url = os.getenv("FASTAGENT_DATABASE_URL", "").strip()
    if not url:
        print("FASTAGENT_DATABASE_URL 没设 —— 这个脚本连的是 fastagent_app，不是迁移角色")
        return 2

    try:
        document = json.loads(Path(arguments.file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"读不了 {arguments.file}：{error}")
        return 2
    entries = document.get("materials") if isinstance(document, dict) else document
    if not isinstance(entries, list) or not entries:
        print("这份 JSON 里没有 `materials` 列表，或者它是空的")
        return 2

    refused = {
        str(entry.get("slug") or f"#{index}"): entry_refusals(entry)
        for index, entry in enumerate(entries)
        if entry_refusals(entry)
    }
    if refused:
        for name, reasons in refused.items():
            print(f"FAIL {name}")
            for reason in reasons:
                print(f"       {reason}")
        print(f"{len(refused)}/{len(entries)} 条被拒，**一条都没有写进去**")
        return 1

    with Connection.connect(url, row_factory=dict_row) as connection:
        counts = import_entries(
            connection, tenant_id=UUID(arguments.tenant), entries=entries
        )
        connection.commit()

    print(
        f"写入 {counts['inserted']} 条 · 已存在跳过 {counts['skipped']} 条 · "
        f"现取字节数与哈希 {counts['measured']} 次 · "
        f"**因大文件那一档没实现而拒 {counts['refused']} 条**"
    )
    # **拒过就非零退出**（T362）。为零的那一格印在上面一行，
    # 而一个整理的人只看退出码 —— 印了却仍然回 0，等于没拒。
    return 1 if counts["refused"] else 0


if __name__ == "__main__":
    # 与 `check_adr_references.py` 逐字同源：Windows 控制台是 cp1252，
    # 而这个脚本每一行输出都是中文 —— 没有这一句它会以 `UnicodeEncodeError`
    # 结束，**退出码非零而一个字都没报出来**。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
