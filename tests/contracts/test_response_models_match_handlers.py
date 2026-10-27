"""`ROUTE_RESPONSES` 里每个模型的字段集合，必须就是 handler 真的返回的那些键。

## 没有这条用例，那些模型可以说谎

ADR-0142 决策 1 选的是 `responses=`（只做文档）而不是 `response_model`
（会过滤掉模型里没有的键，于是写漏一个字段会**安静地把它从响应里删掉**）。
代价写在那条裁定里，逐字：

> 只做文档，就意味着**它可以说谎**：模型写错了，OpenAPI 就是错的，
> 而没有东西会红。因此本条要求：**每一个响应模型都要有一条用例，
> 拿 handler 真实的返回去比它的字段集合。没有那条用例的模型不许合入。**

这就是那条用例。**它是 `responses=` 这个选择能成立的唯一理由** ——
删掉它，那批模型就退化成一组没人核过的注释。

## 判据：比字段集合，不比语义

与 `scripts/check_openapi_drift.py` 同源。一个语义比对器要自己回答
「什么算等价」，而本仓记过那类自造校验器只证明它自己那一层。

## 读的是 AST，不是运行时

因为要在**不起数据库**的前提下问「这个 handler 写了 `return {...}` 里的哪几个键」。
`grep` 答不了这个问题（折行、嵌套字典、`**` 展开都看不出来），
而本仓记过「数错了的守卫看起来在守」。

**它覆盖不到的那一半，写在这里而不是不写**：handler 若把响应交给一个函数
去拼（`_page_or_refuse(...)` / `as_dict()` / `_role_projection(...)`），
本用例**认不出那些键**，于是那条 route 不许进 `ROUTE_RESPONSES` ——
`test_every_registered_route_is_one_this_check_can_actually_read` 把这条规矩
钉成一条会红的用例，而不是一句口头约定。那些 route 的模型要等一个
能读到投影的判据（或者把投影改成在 handler 里展开）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_args

import pytest
from fastagent_v2.api.responses import ROUTE_RESPONSES, success_status
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
#: 控制面通用信封的权威。**从 schema 读，不在本文件写第二遍。**
CONTRACTS = REPO_ROOT / "packages" / "contracts" / "control-plane"

APP_SOURCE = (
    REPO_ROOT
    / "services"
    / "api"
    / "src"
    / "fastagent_v2"
    / "api"
    / "app.py"
)


def _route_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str] | None:
    for decorator in fn.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        func = decorator.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id != "app" or not decorator.args:
            continue
        first = decorator.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return func.attr.upper(), first.value
    return None


#: `Page<T>` 的外层键。**从契约 schema 读，不写第二遍** ——
#: `packages/contracts/control-plane/page.schema.json` 是那个形状的权威。
def _page_keys() -> frozenset[str]:
    schema = json.loads((CONTRACTS / "page.schema.json").read_text(encoding="utf-8"))
    return frozenset(schema["properties"])


def _command_receipt_keys() -> frozenset[str]:
    schema = json.loads((CONTRACTS / "command-receipt.schema.json").read_text(encoding="utf-8"))
    return frozenset(schema["properties"])


#: 把响应拼出来的那几个包装器。**名字是白名单，不是启发式** ——
#: 一个「名字里带 page 就当成分页」的判据，会把任何新函数默默算对。
_PAGE_WRAPPERS = {"_page_or_refuse", "page"}


def _dict_returning_functions(tree: ast.Module) -> dict[str, set[str]]:
    """模块里那些「只返回字面量 dict」的函数 → 它们返回哪些键。

    **一遍不够，要跑到不动点**：`_role_projection` 返回字面量，而别处写
    `{**_role_projection(row), "correlation_id": ...}` —— 后者要等前者先解出来。
    三轮足够本仓今天的深度，而**轮数是有上限的**：一个循环引用不会让它转不停。
    """

    bodies: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = [r.value for r in ast.walk(node) if isinstance(r, ast.Return) and r.value]
            if returns:
                bodies[node.name] = returns

    known: dict[str, set[str]] = {}
    for _ in range(3):
        for name, returns in bodies.items():
            if name in known:
                continue
            resolved = [_keys_of(expr, known) for expr in returns]
            usable = [r for r in resolved if r is not None]
            # 所有分支必须说同一件事，否则这个函数没有一个确定的形状。
            if usable and len(usable) == len(resolved) and all(u == usable[0] for u in usable):
                known[name] = usable[0]
    return known


def _keys_of(expr: ast.expr, known: dict[str, set[str]]) -> set[str] | None:
    """一个表达式作为响应体时，它有哪些顶层键。读不出来返回 `None`。

    **读不出来就说读不出来**，不猜 —— 一个猜出来的偏小集合会让守卫
    报一个假的不匹配，而一个猜出来的偏大集合会让它假绿。
    """

    if isinstance(expr, ast.Dict):
        keys: set[str] = set()
        for key, value in zip(expr.keys, expr.values, strict=True):
            if key is None:  # `{**other, ...}`
                spread = _keys_of(value, known)
                if spread is None:
                    return None
                keys |= spread
            elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            else:
                return None
        return keys

    if isinstance(expr, ast.Call):
        func = expr.func
        # `<something>(...).as_dict()` —— **按接收方分辨，不按方法名。**
        #
        # 第一版把**任何** `.as_dict()` 都当成 `CommandReceipt`，
        # 于是 `page(items=...).as_dict()` 那三条列表 route 被解成了回执的六个键。
        # 那不是漏报是**错报**：它会给一个完全不对的模型盖章，
        # 而盖完之后前端据它生成的类型会把整页列表读成一张回执。
        # 排批 6 时实测撞到（`GET /conversations` / `GET /contacts` / 消息页）。
        if isinstance(func, ast.Attribute) and func.attr == "as_dict":
            receiver = func.value
            if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
                if receiver.func.id in _PAGE_WRAPPERS:
                    return set(_page_keys())
                if receiver.func.id == "command_receipt":
                    return set(_command_receipt_keys())
            return None
        if isinstance(func, ast.Name):
            if func.id in _PAGE_WRAPPERS:
                return set(_page_keys())
            if func.id in known:
                return set(known[func.id])
    return None


def _page_item_keys(expr: ast.expr, known: dict[str, set[str]]) -> set[str] | None:
    """分页响应里**一个条目**的键。不是分页响应就返回 `None`。"""

    # `page(items=[...]).as_dict()` 与 `_page_or_refuse([...])` 两种写法都要认。
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
        if expr.func.attr != "as_dict":
            return None
        expr = expr.func.value  # type: ignore[assignment]
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Name):
        return None
    if expr.func.id not in _PAGE_WRAPPERS:
        return None
    items: ast.expr | None = None
    if expr.args:
        items = expr.args[0]
    else:
        for keyword in expr.keywords:
            if keyword.arg == "items":
                items = keyword.value
    if items is None:
        return None
    if isinstance(items, ast.Subscript):  # `listing[:MAX_PAGE_ITEMS]`
        items = items.value
    element: ast.expr | None = None
    if isinstance(items, ast.ListComp):
        element = items.elt
    elif isinstance(items, ast.List) and items.elts:
        element = items.elts[0]
    elif isinstance(items, ast.Name):
        # `listing` 这种先算好再传进来的，**本判据读不出来** —— 说不出来就说。
        return None
    if element is None:
        return None
    if isinstance(element, ast.Subscript):
        element = element.value
    return _keys_of(element, known)


def _returned_key_sets(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[set[str]]:
    """这个 handler 的每一条 `return` 各自给出哪些顶层键。

    **已经不只收字面量 dict 了**（2026-09-20 扩判据）：B 面 18 条里只有
    10 条写字面量，其余走 `_page_or_refuse` / `.as_dict()` / 投影函数 /
    `**` 展开。第一版把这些一律判成「读不出来」，于是那 8 条拿不到模型 ——
    **而拿不到模型就意味着前端对它们的响应只能手写**，正是 T378 要修的那件事。

    仍然读不出来的照旧返回空：`Response(...)`（204 无响应体）、
    以及先在别处算好再传进分页包装器的列表。
    """

    known = _dict_returning_functions(_MODULE)
    found: list[set[str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        keys = _keys_of(node.value, known)
        if keys is not None:
            found.append(keys)
    return found


def _returned_item_key_sets(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[set[str]]:
    """分页 handler 的**条目**键。非分页的返回空列表。"""

    known = _dict_returning_functions(_MODULE)
    found: list[set[str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        keys = _page_item_keys(node.value, known)
        if keys is not None:
            found.append(keys)
    return found


_MODULE = ast.parse(APP_SOURCE.read_text(encoding="utf-8"), filename=str(APP_SOURCE))


def _handlers() -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = _MODULE
    out: dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            route = _route_of(node)
            if route is not None:
                out[route] = node
    return out


HANDLERS = _handlers()
REGISTERED = sorted(ROUTE_RESPONSES)


@pytest.mark.parametrize("route", REGISTERED, ids=lambda r: f"{r[0]} {r[1]}")
def test_the_model_names_exactly_the_keys_the_handler_returns(route: tuple[str, str]) -> None:
    handler = HANDLERS.get(route)
    assert handler is not None, (
        f"{route[0]} {route[1]} 在 ROUTE_RESPONSES 里，而 app.py 里没有这条 route。"
        "一张指向不存在 route 的表比没有表更糟 —— 读的人会以为它被覆盖着。"
    )
    declared = set(ROUTE_RESPONSES[route].model_fields)
    returned = _returned_key_sets(handler)
    assert returned, (
        f"{route[0]} {route[1]} 的 handler 没有任何字面量 `return {{...}}`，"
        "本用例读不出它的键 —— 这条 route 不该进 ROUTE_RESPONSES。"
    )
    for keys in returned:
        assert keys == declared, (
            f"{route[0]} {route[1]}：模型说 {sorted(declared)}，"
            f"而 handler 返回 {sorted(keys)}。"
            f"模型多出来的：{sorted(declared - keys)}；handler 多出来的：{sorted(keys - declared)}"
        )


def _nested_model(model: type, field: str) -> type | None:
    """模型某个字段的类型，若它本身是一个响应形状。`list[X]` 取 `X`。"""

    info = model.model_fields.get(field)
    if info is None:
        return None
    annotation = info.annotation
    args = get_args(annotation)
    if args:
        annotation = args[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _returned_result_key_sets(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[set[str]]:
    """`{"receipt": ..., "result": {...}}` 里 `result` 那一半的键。"""

    known = _dict_returning_functions(_MODULE)
    found: list[set[str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "result":
                keys = _keys_of(value, known)
                if keys is not None:
                    found.append(keys)
    return found


@pytest.mark.parametrize("route", REGISTERED, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_pages_item_model_names_exactly_the_keys_one_row_has(route: tuple[str, str]) -> None:
    """**顶层对上不等于里面对上。**

    `Page<T>` 的四个外层键对每条分页 route 都一样 —— 只比它们，等于
    什么都没比。真正会漂的是 `items` 里那一行，而它正是列表页每一列
    的来源：后端改一个字段名，**整列空白**，与「这个租户确实没数据」
    在屏幕上一模一样。
    """

    item_model = _nested_model(ROUTE_RESPONSES[route], "items")
    if item_model is None:
        pytest.skip("不是分页响应")
    handler = HANDLERS[route]
    returned = _returned_item_key_sets(handler)
    if not returned:
        pytest.skip(
            "条目在别处算好再传进包装器，本判据读不出 —— "
            "这种情况由一条专门的用例顶（见 test_the_permission_catalogue_rows_match_their_model）"
        )
    declared = set(item_model.model_fields)
    for keys in returned:
        assert keys == declared, (
            f"{route[0]} {route[1]} 的条目：模型说 {sorted(declared)}，"
            f"而 handler 给 {sorted(keys)}"
        )


@pytest.mark.parametrize("route", REGISTERED, ids=lambda r: f"{r[0]} {r[1]}")
def test_a_receipts_result_model_names_exactly_the_keys_the_handler_puts_there(
    route: tuple[str, str],
) -> None:
    """`{receipt, result}` 里的 `result` 也要比。

    顶层永远是那两个键 —— 一个只比顶层的守卫，对这六条命令 route
    **等于没有守卫**。
    """

    result_model = _nested_model(ROUTE_RESPONSES[route], "result")
    if result_model is None:
        pytest.skip("这条响应没有 result 那一格")
    returned = _returned_result_key_sets(HANDLERS[route])
    assert returned, f"{route[0]} {route[1]} 声明了 result 模型，而 handler 那一格读不出来"
    declared = set(result_model.model_fields)
    for keys in returned:
        assert keys == declared, (
            f"{route[0]} {route[1]} 的 result：模型说 {sorted(declared)}，"
            f"而 handler 给 {sorted(keys)}"
        )


def test_the_permission_catalogue_rows_match_their_model() -> None:
    """`GET /permissions` 的条目，用**产它的那个函数**来比。

    它的 handler 先把列表算好再传进分页包装器，所以 AST 判据读不出条目 ——
    而 `assignable_permissions()` 是一个纯函数，不需要数据库也不需要请求。
    **能直接问产出方的时候，就不要去猜。**
    """

    from fastagent_v2.api.responses import PermissionItem
    from fastagent_v2.runtime.authz import assignable_permissions

    rows = assignable_permissions()
    assert rows, "目录是空的 —— 那这条用例什么都没验"
    declared = set(PermissionItem.model_fields)
    for row in rows:
        assert set(row) == declared, f"目录行 {sorted(row)} 与模型 {sorted(declared)} 对不上"


def _declared_status(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """装饰器上写的 `status_code=`，没写就是 FastAPI 的默认 200。"""

    for decorator in fn.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant):
                return int(keyword.value.value)
    return 200


@pytest.mark.parametrize("route", REGISTERED, ids=lambda r: f"{r[0]} {r[1]}")
def test_the_documented_status_is_the_one_the_route_returns(route: tuple[str, str]) -> None:
    """模型要挂在这条 route **真的会返回**的那个状态上。

    **这一条是肉眼先发现、守卫后补的**，留痕：2026-09-20 写
    `POST .../messages` 时，它 `status_code=202` 而
    `openapi_responses` 把模型挂在 200 —— 于是生成的 TS
    **给一个永远不会发生的 200 标了 `CommandReceiptShape`，
    而真正的 202 是 `unknown`**。

    键对不对由上面那条查，**状态对不对此前没有任何东西查** ——
    而两者错一个，前端拿到的类型就都是假的。
    """

    handler = HANDLERS[route]
    declared = _declared_status(handler)
    documented = success_status(*route)
    assert declared == documented, (
        f"{route[0]} {route[1]}：route 回 {declared}，而模型挂在 {documented} 上。"
        "改 `ROUTE_SUCCESS_STATUS`（`responses.py`）让两者一致 —— "
        "挂错状态等于给一个不存在的响应标类型。"
    )


def test_every_registered_route_is_one_this_check_can_actually_read() -> None:
    """**把「覆盖不到的那一半」钉成一条会红的用例。**

    若有人把一条 `return _page_or_refuse(...)` 的 route 加进 `ROUTE_RESPONSES`，
    上面那条会因为「没有字面量 return」而红 —— 但它红的位置在参数化用例里，
    读起来像是那条 route 坏了。这一条说的是另一句话：
    **那张表只许放本检查读得懂的 route**，而这是一条规矩，不是一次故障。
    """

    unreadable = [
        f"{method} {path}"
        for (method, path) in REGISTERED
        if (handler := HANDLERS.get((method, path))) is not None
        and not _returned_key_sets(handler)
    ]
    assert not unreadable, (
        "这些 route 的响应是由函数拼出来的（`_page_or_refuse` / `as_dict` / 投影），"
        f"本检查读不出它们的键：{unreadable}。"
        "在有一个能读到投影的判据之前，它们不许进 ROUTE_RESPONSES —— "
        "否则那张表会看起来被覆盖着，而实际上没有。"
    )


def test_the_check_would_notice_a_model_that_drifted() -> None:
    """**守卫自己不能是空的。**

    上面那些今天绿着，而一条绿着的守卫永远不显示它的报错。
    这里喂它一份已知对不上的输入，确认判据真的会分辨。
    """

    handler = HANDLERS[("POST", "/auth/login")]
    returned = _returned_key_sets(handler)
    assert returned, "夹具选错了 route —— 它没有字面量 return，那这条用例什么都没验"
    actual = returned[0]
    assert actual == set(ROUTE_RESPONSES[("POST", "/auth/login")].model_fields)
    # 去掉一个键之后必须不相等。若这里也相等，说明上面那个 `==` 比的不是集合。
    assert actual != (actual - {"must_change_password"})
