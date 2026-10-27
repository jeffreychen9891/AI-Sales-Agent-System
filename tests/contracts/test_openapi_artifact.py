"""仓里那份 `packages/contracts/openapi.json` 与 app 说的是不是同一件事。

## 为什么这条用例比那个脚本重要

`scripts/check_openapi_drift.py` 的判据是**字符串相等**。
字符串相等有一个天生的空法：**两边一起坏掉**——
若 `render()` 哪天返回一份空文档，而产物也是空的，它照样绿。
本仓记过这个形状（「空守卫有两种空法」、「数错了的守卫看起来在守」）。

所以这里不只问「两份一样吗」，还问**「产物里的 path 集合是不是就是 app
自己声明的那个集合」**——右边那个集合从 `create_app().routes` 现取，
**不从产物读**。一条新 route 加进 app 而没人重新导出，这条用例会红，
且红的理由指名道姓。

## 它不需要数据库

`create_app()` 把连接串交给 `Database` / `AttemptThrottle` 存着，连接是惰性的
（这也是 `dump_openapi` 能用一个指向 `*.example.invalid` 的配置跑通的原因）。
所以本文件放在 `tests/contracts/` 而不是 `tests/integration/`。

## 一处**声明出来的空白**

`WS /ws` 不在这份产物里——FastAPI 不把 websocket 写进 OpenAPI。
于是「前端 client 由 OpenAPI 生成」这句话**对实时层不成立**，
而下一个人很容易以为它成立。`test_the_artifact_says_nothing_about_the_websocket`
把这个空白钉成一条会红的用例：哪天 `/ws` 真的进了 OpenAPI，
它会红并提醒人回来改这段话，而不是让一个过期的空白声明继续留在这里。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dump_openapi import ARTIFACT, render, schema_settings  # noqa: E402


@pytest.fixture(scope="module")
def committed() -> dict[str, object]:
    assert ARTIFACT.exists(), (
        f"{ARTIFACT.relative_to(ROOT)} 不存在。生成它：python scripts/dump_openapi.py"
    )
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def declared_paths() -> set[str]:
    """app **现在**声明的、会进 OpenAPI 的那些 path。

    **从 `create_app()` 现取，不从产物读** —— 这是本文件存在的理由。
    过滤条件与 FastAPI 自己进 schema 的条件同源：有 `methods` 的 HTTP route、
    且 `include_in_schema`。websocket route 没有 `methods`，自然落在外面，
    而那正是 `/ws` 缺席的机制。
    """

    from fastagent_v2.api.app import create_app

    app = create_app(schema_settings())
    return {
        route.path
        for route in app.routes
        if getattr(route, "include_in_schema", False) and getattr(route, "methods", None)
    }


def test_the_committed_artifact_is_what_the_app_renders_today(committed: dict[str, object]) -> None:
    """真正的漂移断言。**红了就是有人改了 route 没重新导出。**"""

    assert ARTIFACT.read_text(encoding="utf-8") == render(), (
        "仓里那份 openapi.json 与今天的 app 不一致。"
        "改了 route 就重新导出并把产物一起提交：python scripts/dump_openapi.py"
    )


def test_rendering_twice_gives_the_same_bytes() -> None:
    """**漂移闸的前提。**

    若导出不可重放，上面那条断言会变成一个随机红——而一个会随机红的门禁
    会在几轮之后被人当成噪音关掉。这里把「可重放」本身钉住。
    """

    assert render() == render()


def test_the_artifact_covers_every_path_the_app_declares(
    committed: dict[str, object], declared_paths: set[str]
) -> None:
    """**防两边一起坏掉。**

    右边这个集合从 `create_app().routes` 现取。产物少一条、多一条，都在这里红。
    """

    in_artifact = set(committed["paths"])  # type: ignore[arg-type]
    assert in_artifact == declared_paths, (
        f"产物里有而 app 没有：{sorted(in_artifact - declared_paths)}；"
        f"app 有而产物里没有：{sorted(declared_paths - in_artifact)}"
    )


def test_the_artifact_is_not_empty(committed: dict[str, object]) -> None:
    """一份空产物会让字符串比对恒绿。这里给它一个下界。

    **下界写成一个真实的数而不是 `> 0`**：`> 0` 的守卫在一份只剩一条
    `/healthz` 的产物上照样绿，而那恰恰是「导出跑了一半」的样子。
    50 是本批实测 53 往下留的一点余量——它不是一个要跟着 route 数走的数，
    它只回答「这份东西是不是整个塌了」。
    """

    assert len(committed["paths"]) >= 50  # type: ignore[arg-type]


def test_the_artifact_says_nothing_about_the_websocket(committed: dict[str, object]) -> None:
    """`WS /ws` 不在 OpenAPI 里，而这是一处**声明出来的**空白。

    哪天它进来了，这条会红——那时要回来改的是
    `scripts/check_openapi_drift.py` 的「它不守什么」那一节，
    以及前端「client 全部由 OpenAPI 生成」那句话。
    """

    assert "/ws" not in committed["paths"]  # type: ignore[operator]
