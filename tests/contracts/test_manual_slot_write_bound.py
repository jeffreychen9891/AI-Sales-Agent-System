"""人工写槽位值那道界 —— **它落地时没有用例，这一份是补上的那个**。

## 来处

批 2（`2c9a9410`）给 `conversation-aggregate` 的 `slots` 加了逐值
`maxLength: 400`，**而人工写入那条路没跟着加**：`slots: dict[str, str]` 上那个
`max_length=32` 是**字典大小**，不是值长度。于是当时仍可写进一个 1,000 字的
槽位值、落进聚合，**然后在读路径上炸** —— 接管、交回、`pipeline` 多处都是
「读出一行再就地校验」，一行不合规 = **那条会话的那条路径 500**，
而不是写入时的 422。

收尾那一笔（`52738da6`）把两侧对齐了，**而它的自审逐字写着**：

> `SLOT_VALUE_MAX_LENGTH` 那一处**没有新用例**，而它由契约那条 `maxLength`
> 与 FastAPI 的 422 共同执行；**这一点如实写在这里，不假装它有守卫**。

**这一份就是那个守卫**（2026-09-16）。

## 三件事，三条用例，**而它们证的强度不一样 —— 写明**

1. **那个数不是第三份副本**：`app.py` 从 `load_schema("conversation-aggregate")`
   现读，与契约同一个数。——`test_the_bound_is_read_from_the_contract`；
2. **写入侧真的按「每个值」收**，不是按字典大小：AST 现读那个字段的标注。
   ——`test_the_per_value_bound_is_on_the_value_not_on_the_dict`；
3. **超界确实变成 422 而不是 500**：用同一个数造一个等价模型挂进 FastAPI 跑。
   ——`test_a_value_over_the_bound_is_a_422_not_a_500`。

**第 3 条跑的不是真路由**（`app.py` 一 import 就要 `Settings`，
契约层起不了那半个服务 —— 这正是 `commitments_display.py` 单独成模块的理由）。
它证的是「**这种字段声明 + FastAPI = 422**」；
「真路由用的是这种声明」由第 1、2 条证。**两半合起来才是那句话，分开都不是。**
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parents[2]
APP_SOURCE = ROOT / "services/api/src/fastagent_v2/api/app.py"
AGGREGATE_SCHEMA = ROOT / "packages/contracts/schemas/conversation-aggregate.schema.json"


#: 契约里那条界不见了的时候，`_contract_bound` 回这个，**而不是抛**。
MISSING = -1


def _contract_bound() -> int:
    """契约里那条逐值上界 —— **这条用例也从契约读，不抄第四遍**。

    ## 它为什么**不抛**

    第一版直接下标取 `maxLength`，而 `BOUND` 是模块顶层求值的。
    于是把契约那条界删掉（`probe_payload_bounds` 的 W4 干的正是这件事）时，
    **本模块 import 就炸** —— pytest 报的是 `1 error`（收集错误），
    **一条 `FAILED` 都没有**。而反向探针数的是 `FAILED` 的名字，
    于是它读到一个**空集合**，与「什么都没红」逐字一样。

    **一次真的回归，在探针眼里长得像「零连带」。** 实测于 2026-09-16，
    由本批自己那条对照跑抓到 —— 它正是本批在修的那个形状的第三个实例。

    所以：取不到就回 `MISSING`，让**具名的用例**去红，不让 import 去死。
    """

    schema = json.loads(AGGREGATE_SCHEMA.read_text(encoding="utf-8"))
    slots = schema["properties"]["target_stack"]["items"]["properties"]["slots"]
    declared = (slots.get("additionalProperties") or {}).get("maxLength")
    return int(declared) if isinstance(declared, int) else MISSING


def _slots_field() -> ast.AnnAssign:
    """`WriteConversationSlotsRequest.slots` 那一条标注，从 AST 取。

    **不 import `app.py`**：它一 import 就要 `Settings`（签名密钥等），
    而「那个字段是怎么声明的」这个问题不需要一个配好的进程。
    """

    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WriteConversationSlotsRequest"
    ]
    assert len(classes) == 1, f"`WriteConversationSlotsRequest` 有 {len(classes)} 份定义"
    fields = [
        node
        for node in classes[0].body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "slots"
    ]
    assert len(fields) == 1, "那个类里 `slots` 字段不是恰好一条"
    return fields[0]


#: 与 `app.py` 那个字段**同一个形状**，而数从契约读 —— 见下面那条用例的说明。
#:
#: **定义在模块顶层不是风格问题**：FastAPI 用 `get_type_hints` 解析
#: 处理函数的标注，一个定义在函数体里的模型它解析不到，
#: 于是那个参数被当成 query 而不是 body —— 第一版就这样，
#: 拿到的 422 是「缺少查询参数」，**一个与本条要测的东西无关的 422**。
#: **红得对不对，要看它红在什么上。**
BOUND = _contract_bound()

#: 契约里那条界不在时给模型一个占位数，**只为了本模块仍然 import 得进来**。
#: 「那条界还在不在」由 `test_the_contract_still_declares_a_per_value_bound` 说话。
_MODEL_BOUND = BOUND if BOUND > 0 else 1


class _ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: dict[str, Annotated[str, Field(max_length=_MODEL_BOUND)]] = Field(
        min_length=1, max_length=32
    )


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.put("/probe")
    def _write(payload: _ProbeRequest) -> dict[str, int]:
        return {"count": len(payload.slots)}

    return app


def test_the_contract_still_declares_a_per_value_bound() -> None:
    """契约里那条逐值 `maxLength` 还在 —— **这一条是另外三条的前提**。

    它拆出来单独站着，是因为「前提没了」与「实现错了」要给出不同的报错。
    删掉那条界时红的应当是这一句话，**而不是一个收集错误**。
    """

    assert BOUND > 0, (
        "`conversation-aggregate` 的 `slots.additionalProperties.maxLength` 不见了 —— "
        "那是批 2（`2c9a9410`）补的那道界，删掉它等于把人工写入侧那道 422 也一起拿走 —— "
        "于是一个超长的槽位值会落进聚合，**然后在接管 / 交回 / pipeline 的读路径上 500**。"
    )


def test_the_bound_is_read_from_the_contract() -> None:
    """`SLOT_VALUE_MAX_LENGTH` 是**算出来的**，不是写死的。

    批 1 刚为「第四份副本」付过一次账（那份副本藏在守卫自己身上）。
    这里同一个形状：写死一个 400 在 `app.py` 就是第三份，
    而三份数之间没有任何东西会在它们漂开时出声。
    """

    tree = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
    assigned = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "SLOT_VALUE_MAX_LENGTH"
    ]
    assert len(assigned) == 1, "`SLOT_VALUE_MAX_LENGTH` 不是恰好一处顶层定义"

    value = assigned[0].value
    assert value is not None
    source = ast.dump(value)
    assert "load_schema" in source, (
        "`SLOT_VALUE_MAX_LENGTH` 不再从契约读了 —— 写死一个数在这里就是第三份副本，"
        f"而契约今天是 {_contract_bound()}。两份数漂开时不会有任何东西报错。"
    )
    assert not any(isinstance(node, ast.Constant) and node.value == _contract_bound()
                   for node in ast.walk(value)), (
        "那个数被顺手写回表达式里了；它要么整个从契约读，要么就是一份副本"
    )


def test_the_per_value_bound_is_on_the_value_not_on_the_dict() -> None:
    """**`max_length=32` 是字典大小，`SLOT_VALUE_MAX_LENGTH` 才是每个值的长度。**

    这两个数曾经只有一个存在，而缺的正是后者。判据读 AST：
    值那一侧的标注里必须出现 `SLOT_VALUE_MAX_LENGTH` 这个名字。
    """

    field = _slots_field()
    annotation = ast.dump(field.annotation)

    assert "SLOT_VALUE_MAX_LENGTH" in annotation, (
        "`slots` 的**值**不再受逐值上界约束了 —— 回到了批 2 之前：\n"
        "人工可以写进一个 1,000 字的槽位值，写入侧 200，"
        "**然后接管 / 交回 / pipeline 的读路径上 500**。"
    )
    assert "Annotated" in annotation, "逐值的界要挂在值的类型上，不是挂在字典上"


def test_a_value_over_the_bound_is_a_422_not_a_500() -> None:
    """超界在**写入时**被拒，而不是在下一次读的时候炸。

    **这一条跑的不是真路由**（见模块 docstring）：它用同一个数造一个等价的模型，
    证明「这种声明 + FastAPI = 422」。真路由用的是这种声明，由上面两条证。

    **契约那条界不在时这一条跳过**，而不是跟着红：那时该说话的是
    `test_the_contract_still_declares_a_per_value_bound`，一个原因红一条，
    **多红一条只会让人去查一个没有错的地方**（成本闸 §三 记的正是这个形状）。
    """

    if BOUND <= 0:
        pytest.skip("契约里那条界不在了 —— 前提那条会红，这一条没有可测的数")

    bound = BOUND

    # 模型这一层：正好到界过得去，多一个字过不去。
    _ProbeRequest(slots={"concern_topic": "值" * bound})
    with pytest.raises(ValidationError):
        _ProbeRequest(slots={"concern_topic": "值" * (bound + 1)})

    with TestClient(_probe_app()) as client:
        ok = client.put("/probe", json={"slots": {"concern_topic": "值" * bound}})
        assert ok.status_code == 200, ok.text

        too_long = client.put("/probe", json={"slots": {"concern_topic": "值" * (bound + 1)}})
        assert too_long.status_code == 422, (
            f"超界得到的是 {too_long.status_code}，不是 422 —— "
            "写入侧不拒的话，这一行会落进聚合，**然后在读路径上 500**"
        )
