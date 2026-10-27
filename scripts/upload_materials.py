"""把目录里还没传过的资料交给供应商去拉，并把 `requestId` 记回那一行。

## 它在这条线的哪一格

```
import_materials.py   →  这个脚本         →  入站认领          →  发送
（目录：去哪儿拉）        （交给供应商拉）      （回填 fileId / 密钥）    （sendFile）
```

**字节一个都不经过这个进程。** 供应商自己去 `source_url` 取 ——
所以目录存的是地址而不是内容（`065`），而这个脚本只是把那个地址递过去。

## 为什么是脚本而不是一条 route，也不是 worker 的一步

与 `import_materials.py` 同源：资料是**人工放置**的，没有接收字节的 route
（ADR-0130 E1）。上传是那之后的一个运维动作，做一次管很久 ——
v1 实测把一次上传的结果**缓存 30 天**复用（`VIDEO_UPLOAD_CACHE_TTL_SECONDS`），
也就是这件事本来就不属于「每轮对话」那个时间尺度。

## ⚠️ 每行各自提交，不是最后一起提交

**因为上传在供应商那边已经真的发生了。** 一次 `requestId` 丢掉的后果不是
「这行没传成」，而是**传成了却认不回来**：回调照样会来，而没有任何一行认领它，
那份文件在供应商那边成了孤儿，而目录里那一行看上去像「从没传过」——
下一次跑这个脚本会**再传一遍**。

所以每成功一行就 `commit` 一行。整批回滚在这里不是安全，是丢证据。

## guid 由运维给，脚本不去库里找

`channel_bindings.provider_instance_id` 今天**没有授予 `fastagent_app`** ——
`017` 把「谁读得到 guid」留给接线批（T123），而那是发送那一格的决定，不是这一格的。
这个脚本按现有的运维形状要：`--guid` 由调用者给，客户端再拿它去核**同一份
guid 白名单文件**（`FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE`）。
**没有新授权，也没有新的凭据取用路径。**

## 退出码

`0` = 该传的都传了（或没有要传的）· `1` = 有行传失败（逐行说明，**已成功的已提交**）·
`2` = 连不上、缺配置、或者那张表不在。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[1]
for part in ("packages/runtime/src", "packages/channels/src", "services/worker/src"):
    sys.path.insert(0, str(REPO / part))

from fastagent_v2.channels.wecom_proxy import (  # noqa: E402
    DEFAULT_CREDENTIALS_PATH,
    GuidAllowlist,
    RestrictedWeComProxyClient,
    load_credentials,
)

sys.path.insert(0, str(REPO / "scripts"))
from import_materials import (  # noqa: E402
    provider_file_type,
    unsupported_tier_refusal,
)

#: 一次上传给供应商多少秒。
#:
#: **这是「把地址递过去」的超时，不是「把文件传完」的超时** —— 那个接口是异步的，
#: 同步那一半只回一个 `requestId`。所以这个数字小是对的：它变大只会让一次
#: 本来就失败了的调用挂得更久。
UPLOAD_TIMEOUT_SECONDS = 30.0


def rows_needing_upload(
    connection: Connection[Any], *, tenant_id: UUID
) -> list[Mapping[str, Any]]:
    """还没交给供应商的那些行。

    ## 三个条件，各挡一件

    * **`provider_file_id IS NULL`** —— 已经认领成功的不再传；
    * **`upload_request_id IS NULL`** —— 没传过的要传。
      **单独用它不够**，见下一条；
    * **`OR upload_failed_at IS NOT NULL`** —— 传过但**失败了**的要重传。

    ## 第三个条件是 2026-09-17 补的，而在它之前重试是不可能的

    运维实测：十二份全部上传失败之后，重跑只会说「没有要传的」——
    因为失败那一刻 `upload_request_id` 已经写上了。而那个 `requestId`
    **已经没有任何用处**：它对应的上传在供应商那边终结于失败，
    不会再有第二条回调。**但它挡着重试。**

    当时唯一的出路是手改库（把那一列清回 NULL），而运维不自行改数据 ——
    这一条就是为了让那条路不必存在。

    ## 那个「在途」窗口仍然挡着

    `upload_request_id` 有值**而** `upload_failed_at` 为空 = 传了、回调还没回来。
    这一行不入选 —— 那正是第一版写这条判据时要防的：
    每跑一次脚本就在供应商那边多一份孤儿文件。

    `status = 'active'` 一起过滤：归档掉的版本不该再往供应商那边送。
    """

    return list(
        connection.execute(
            """
            SELECT id, slug, version, filename, media_type, byte_size, source_url,
                   upload_failed_at, upload_failure_code
              FROM materials
             WHERE tenant_id = %s
               AND status = 'active'
               AND source_url IS NOT NULL
               AND provider_file_id IS NULL
               AND (upload_request_id IS NULL OR upload_failed_at IS NOT NULL)
             ORDER BY slug, version
            """,
            (tenant_id,),
        ).fetchall()
    )


def claim(
    connection: Connection[Any], *, tenant_id: UUID, material_id: UUID, request_id: str
) -> int:
    """把 `requestId` 记到那一行上，**只在它还空着的时候**。

    那个 `WHERE` 不是多余的：两个人同时跑这个脚本时，后一个的 `UPDATE`
    应当**改不到任何行**而不是覆盖前一个的 id —— 被覆盖掉的那个 id
    对应的上传就再也认领不回来了。回 0 表示「这行已经被别人认领了」，
    调用方据此报告，而不是当成成功。

    **`OR upload_failed_at IS NOT NULL` 是 2026-09-17 补的重试口**：
    上一次失败留下的那个 `requestId` 可以被覆盖 —— 它已经终结于失败，
    不会再有回调来认领它。而**只有失败过的才让覆盖**：在途的那一行照旧挡着。

    **同时把两列失败痕迹清掉**：重传开始的那一刻，上一次的失败就成了历史。
    留着它会让「这一行现在是什么状态」变成一道要读两列并推理的题
    —— 而下一次真失败时它照样会被写上。
    """

    return connection.execute(
        """
        UPDATE materials
           SET upload_request_id = %s, upload_failed_at = NULL, upload_failure_code = NULL
         WHERE tenant_id = %s AND id = %s
           AND (upload_request_id IS NULL OR upload_failed_at IS NOT NULL)
        """,
        (request_id, tenant_id, material_id),
    ).rowcount


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把目录里还没传过的资料交给供应商去拉，并记回 requestId"
    )
    parser.add_argument("--tenant", required=True, help="租户 uuid")
    parser.add_argument("--guid", required=True, help="这个租户的连接 guid（要在白名单文件里）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只列出会传哪几行与各自的 fileType，不调用供应商"
    )
    arguments = parser.parse_args()

    url = os.getenv("FASTAGENT_DATABASE_URL", "").strip()
    if not url:
        print("FASTAGENT_DATABASE_URL 没设 —— 这个脚本连的是 fastagent_app，不是迁移角色")
        return 2

    allowlist_file = os.getenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", "").strip()
    if not allowlist_file and not arguments.dry_run:
        # 与 `worker/cli.py` 逐字同源的理由：空路径**不是** default-deny，
        # `Path("")` 是当前目录、它存在、于是白名单在第一次调用时才抛
        # ——那时人已经不在屏幕前了。这里当场拒。
        print("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE 没设 —— 没有它就没有 guid 白名单可核")
        return 2

    tenant_id = UUID(arguments.tenant)

    with Connection.connect(url, row_factory=dict_row) as connection:
        pending = rows_needing_upload(connection, tenant_id=tenant_id)
        if not pending:
            print(
                "没有要传的：这个租户的 active 资料要么已经认领成功，"
                "要么正在等回调（传过且没失败）"
            )
            return 0

        plan = [
            (row, provider_file_type(str(row["media_type"]), int(row["byte_size"])))
            for row in pending
        ]

        if arguments.dry_run:
            # **先把 fileType 算完再打印**：`provider_file_type` 会在 media_type
            # 不在词表里时抛 `KeyError`，而那正是运维要在真调用之前看见的一条 ——
            # 上面那个列表推导已经替他撞过了。
            for row, file_type in plan:
                # **重传的那些要标出来** —— 运维看着这份清单决定要不要真跑，
                # 而「这是第一次传」与「上次失败了再传一次」是两件事：
                # 后者要先确认上次的失败原因已经排掉了，否则这一次也会失败。
                again = (
                    f" · 重传（上次：{row['upload_failure_code'] or '失败但供应商没给 code'}）"
                    if row["upload_failed_at"]
                    else ""
                )
                # **走不了的那一档在这里就说话**（T362）——
                # 运维看着这份清单决定要不要真跑，而一行看起来正常的
                # `WOULD … → fileType 22` 读起来就是「这份会传上去」。
                blocked = unsupported_tier_refusal(file_type)
                print(
                    f"{'BLOCKED' if blocked else 'WOULD'} "
                    f"{row['slug']}:v{row['version']} → fileType {file_type}"
                    f" · {row['byte_size']} 字节 · {row['source_url']}{again}"
                )
                if blocked:
                    print(f"        ↳ {blocked}")
            unsupported = sum(
                1 for _, file_type in plan if unsupported_tier_refusal(file_type)
            )
            print(
                f"共 {len(plan)} 份待传（--dry-run，一个字节都没动）"
                f"，其中 **{unsupported} 份这条路今天走不了**"
            )
            # **有走不了的就非零退出**：dry-run 的全部用处是「真跑之前先看一眼」，
            # 而一个只在正文里提了一句的告警，读它的人可能正在管道后面。
            return 1 if unsupported else 0

        client = RestrictedWeComProxyClient(
            credentials=load_credentials(
                os.getenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE") or str(DEFAULT_CREDENTIALS_PATH)
            ),
            allowlist=GuidAllowlist(allowlist_file),
        )

        failures: list[str] = []
        uploaded = 0
        for row, file_type in plan:
            name = f"{row['slug']}:v{row['version']}"
            # **在打电话给供应商之前**（T362）。
            #
            # 不拦的代价不是「失败一次」：供应商会**把整份文件拉完**再拒
            # （运维 2026-09-17 实测，access.log 有那条 `200 48219856`
            # —— 48MB 走完了才回 `500100 文件上传限制:20MB`）。
            # 那一次是我们让它白拉的。
            blocked = unsupported_tier_refusal(file_type)
            if blocked is not None:
                failures.append(f"{name} 没传：{blocked}")
                continue
            try:
                request_id = client.upload_file_by_url(
                    guid=arguments.guid,
                    filename=str(row["filename"]),
                    file_url=str(row["source_url"]),
                    file_type=file_type,
                    timeout=UPLOAD_TIMEOUT_SECONDS,
                )
            except Exception as error:  # noqa: BLE001 - 一行失败不该中断其余行
                failures.append(f"{name} 上传失败：{type(error).__name__}: {error}")
                continue

            changed = claim(
                connection, tenant_id=tenant_id, material_id=row["id"], request_id=request_id
            )
            # **成功一行提交一行** —— 见模块 docstring：丢掉一个 requestId
            # 不是「没传成」，是「传成了却认不回来」。
            connection.commit()
            if changed == 0:
                failures.append(
                    f"{name} 已经被别人认领了（requestId {request_id} 记不进去）"
                    " —— 这次的上传在供应商那边成了孤儿，别再跑第二个实例"
                )
                continue
            uploaded += 1
            print(f"OK   {name} → fileType {file_type} · requestId {request_id}")

        for reason in failures:
            print(f"FAIL {reason}")
        print(f"传出去 {uploaded} 份 · 失败 {len(failures)} 份 · 共 {len(plan)} 份待传")
    return 1 if failures else 0


if __name__ == "__main__":
    # 与 `import_materials.py` 逐字同源：Windows 控制台是 cp1252。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
