"""把 api 的 OpenAPI 文档导出成仓内产物。

## 为什么要有这个文件

`docs/rfcs/control-plane-parity.md` §4.3 把 OpenAPI 定成**HTTP 合同的权威**：

> OpenAPI 是 HTTP 合同权威；生成 TypeScript client 后，两前端不得手写 method/path。

而在本文件之前，**那份权威不存在于仓里**——它只存在于一个跑起来的进程的
`/openapi.json` 上。一条「权威」如果没有产物，就没有东西可以和它比对，
于是「前端和后端说的是不是同一件事」这个问题**没有一个便宜的答案**。

## 它和它的闸是两件事

本脚本只负责**导出**。比对由 `scripts/check_openapi_drift.py` 做，
它跑在门禁里：**重新导出一次，和仓里那份逐字节比**，不一致就红。

**为什么要比字节而不是比语义**：一个语义比对器要自己实现「什么算等价」，
而那正是本仓记过的那类自造校验器——它只证明它自己那一层。
字节比对的判据是「重新生成一次会不会得到同一份文件」，
这个判据不需要任何人去定义等价。

## 设置值是当场拼的，不是写死的

`Settings` 的七个字段里有六个是带密码的连接串、一个是签名密钥。
**本脚本一个字面量都不写**：连接串用明显不可用的占位主机名，
签名密钥用 `secrets.token_urlsafe` 当场生成。

**理由不是洁癖**：本仓已经三次把像凭据的字面量写进代码（含测试夹具），
而一个写死在脚本里的 `access_signing_key` 会被密钥扫描当成真的，
也会被下一个人当成「原来这里可以写一个」。

**schema 导出不读这些值**：`create_app()` 只是把它们交给 `Database` /
`AttemptThrottle` 构造器存起来，连接是惰性的。若哪天不再惰性，
本脚本会当场炸——那是对的，比悄悄导出一份半截的 schema 好。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

for source_root in (
    "packages/runtime/src",
    "packages/channels/src",
    "services/api/src",
    "services/worker/src",
    "services/ingress/src",
):
    sys.path.insert(0, str(REPO_ROOT / source_root))

# **环境要在 import 之前设好，而这不是可以整理掉的顺序。**
# `services/api/.../app.py` 的**模块顶层**有一句 `app = create_app()`
# （`:10733`），所以仅仅 import 它就会走 `Settings.from_env("api")`，
# 而那条路径对六个变量是 `_required`。把赋值挪到 import 之后，
# 本脚本会在 import 那一行炸——实测过一次，留这句免得下一个人"顺手整理"。
_HOST = "unused.example.invalid"
_ENVIRONMENT = "ci"
# `from_env` 只认五个名字（`settings.py:156`）：local / ci / stage-m0 /
# stage / production。**"schema-dump" 不在里面**，第一版写了它并当场炸。
# 取 `ci`：这份产物本来就是给门禁比对用的。
os.environ.setdefault("FASTAGENT_ENVIRONMENT", _ENVIRONMENT)
os.environ.setdefault(
    "FASTAGENT_DATABASE_URL", f"postgresql://schema-dump@{_HOST}:5432/schema-dump"
)
os.environ.setdefault("FASTAGENT_CACHE_REDIS_URL", f"redis://{_HOST}:6379/0")
# `role="api"` 这条路径上 `_required` 的一共五个：ENVIRONMENT、
# ACCESS_SIGNING_KEY、ALLOWED_WS_ORIGINS、DATABASE_URL、CACHE_REDIS_URL
# （`settings.py:151-212`）。MODEL / CHANNEL_PROVIDER 只对 worker 必需，
# 设了也无害，留着是因为本脚本若哪天改成 role="all" 就会需要它们。
os.environ.setdefault("FASTAGENT_MODEL_PROVIDER", "mock")
os.environ.setdefault("FASTAGENT_CHANNEL_PROVIDER", "sink")
os.environ.setdefault(
    "FASTAGENT_ALLOWED_WS_ORIGINS", "https://schema-dump.example.invalid"
)
# 当场生成，不写字面量 —— 理由见模块 docstring。
os.environ.setdefault("FASTAGENT_ACCESS_SIGNING_KEY", secrets.token_urlsafe(32))

from fastagent_v2.api.app import create_app  # noqa: E402
from fastagent_v2.runtime.settings import Settings  # noqa: E402

#: 产物落点。**放在 `packages/contracts/` 下，与 manifests 并排**——
#: 它和 `route-manifest.json` 是同一族东西：对外合同的仓内副本。
ARTIFACT = REPO_ROOT / "packages" / "contracts" / "openapi.json"

#: 一个**明显不可用**的主机名。用 `example.invalid`（RFC 2606 保留，永不解析）
#: 而不是 `localhost`：后者在开发机上是可能连上的，于是一份配错的导出
#: 可能悄悄连到某个真库。
_UNUSABLE_HOST = _HOST


def schema_settings() -> Settings:
    """一份只够把 app 构造出来的配置。**每个值都当场拼。**"""

    dsn = f"postgresql://schema-dump@{_UNUSABLE_HOST}:5432/schema-dump"
    redis = f"redis://{_UNUSABLE_HOST}:6379/0"
    return Settings(
        environment=_ENVIRONMENT,
        database_url=dsn,
        ingress_database_url=dsn,
        worker_database_url=dsn,
        scheduler_database_url=dsn,
        queue_redis_url=redis,
        cache_redis_url=redis,
        # 当场生成。这个值不进产物，也不该有人从这里抄走一个形状。
        access_signing_key=secrets.token_urlsafe(32),
        model_provider="mock",
        channel_send_url=f"http://{_UNUSABLE_HOST}/send",
        allowed_ws_origins=frozenset({"https://schema-dump.example.invalid"}),
    )


def render() -> str:
    """导出成一个**确定性**的字符串。

    `sort_keys=True` 与固定缩进是漂移闸的前提：两次导出之间任何不来自
    route 变化的差异，都会让闸报一个假红。末尾那个换行是给 POSIX 工具的。
    """

    document = create_app(schema_settings()).openapi()
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(render(), encoding="utf-8", newline="\n")
    paths = len(json.loads(ARTIFACT.read_text(encoding="utf-8"))["paths"])
    print(f"openapi: wrote {ARTIFACT.relative_to(REPO_ROOT)} ({paths} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
