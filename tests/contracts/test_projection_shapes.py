"""几个投影函数产出的形状，必须就是响应模型声明的那个形状 —— **一路比到嵌套层**。

## 它补的是 AST 那条判据够不着的地方

`test_response_models_match_handlers.py` 读的是 handler 的字面量 `return`，
所以它**只看得见顶层键**。而会话列表的一行里有四个嵌套对象
（`contact` / `last_message` / `target` / `next_event`），
`Page<T>` 的 `items` 里那一行更是整族的主体 ——
**只比顶层，等于对这一族什么都没比**。

再扩那条 AST 判据，就要让它开始理解 `IfExp`、理解三元里的两支各是什么形状，
而那是在用静态分析**重新实现一遍这个函数**。

## 判据：调产出方，把结果递归比到模型

`_conversation_summary` / `_message_projection` / `_contact_projection`
是模块级可导入的**纯函数** —— 拿一行合成的 row 调它们，
**不要数据库、不要请求**。这与 `GET /permissions` 那条
（直接调 `assignable_permissions()`）是同一个道理：
**能直接问产出方的时候，就不要去猜。**

## 两件它刻意不做

* **不验值，只验形状。** 「`unread_inbound` 算得对不对」是别的用例的事；
  这里问的是「客户端按模型去读，会不会读到一个不存在的键」。
* **不验 `GET /conversations/{id}`。** 那条回 14 个键、三格各自是一棵嵌套树，
  而**那三格今天没有消费方**（接管页在批 7）。一个没有页面读它的模型，
  是一句没人会发现它错了的话 —— 随批 7 一起写。
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

# **先把 `scripts/` 上 path 并 import `dump_openapi`，再 import `app`。**
#
# `services/api/.../app.py` 的**模块顶层**有一句 `app = create_app()`，
# 所以仅仅 import 它就会走 `Settings.from_env("api")` —— 六个变量缺一不可。
# 而 `scripts/dump_openapi.py` 已经是那个「知道怎么把 app 导进来」的地方。
#
# **在这里抄第二份那六个变量，会让它们有两个真相源**；更要紧的是：
# 不设它们的话，这个文件在门禁里绿（`ci.sh` 导出了环境）、在本地红 ——
# 而「只在别人那里红」正是本仓反复记的那种形状。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import dump_openapi  # noqa: E402,F401  (import 的副作用就是它的用途)
from fastagent_v2.api.app import (  # noqa: E402
    _contact_projection,
    _conversation_summary,
    _message_projection,
)
from fastagent_v2.api.responses import (  # noqa: E402
    ContactDetail,
    ConversationListItem,
    MessageItem,
    _Shape,
)
from pydantic import BaseModel  # noqa: E402


def _nested(model: type[BaseModel], field: str) -> type[BaseModel] | None:
    """字段的类型，若它（或它的 `list[...]` / `| None` 里那一支）是一个响应形状。"""

    info = model.model_fields.get(field)
    if info is None:
        return None
    for candidate in (info.annotation, *get_args(info.annotation)):
        for inner in (candidate, *get_args(candidate)):
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return inner
    return None


def assert_shape(produced: object, model: type[BaseModel], path: str = "") -> None:
    """产出的形状与模型逐层对齐。**多一个键、少一个必填键，都在这里红。**"""

    assert isinstance(produced, dict), f"{path or '<顶层>'}: 产出的不是一个对象"
    declared = set(model.model_fields)
    optional = {name for name, f in model.model_fields.items() if not f.is_required()}
    actual = set(produced)

    extra = actual - declared
    assert not extra, f"{path or '<顶层>'}: 产出多了模型没有的键 {sorted(extra)}"
    missing = (declared - optional) - actual
    assert not missing, f"{path or '<顶层>'}: 产出少了模型声明的必填键 {sorted(missing)}"

    for name, value in produced.items():
        sub = _nested(model, name)
        if sub is None or value is None:
            continue
        where = f"{path}.{name}" if path else name
        if isinstance(value, list):
            for index, element in enumerate(value):
                assert_shape(element, sub, f"{where}[{index}]")
        else:
            assert_shape(value, sub, where)


def _conversation_row(*, populated: bool) -> dict[str, Any]:
    """一行会话列表的原始数据。

    **两种都要试**：四个嵌套对象各自可以是 `null`，而
    「值在的时候形状对」与「值不在的时候不炸」是两件事。
    """

    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "control_mode": "ai",
        "state_version": 3,
        "unread_total": 2,
        "open_manual_tasks": 1,
        "state": {
            "target_stack": (
                [{"frame_id": "f-1", "definition_id": "d-1", "status": "active"}]
                if populated
                else []
            )
        },
        "contact_id": uuid.uuid4() if populated else None,
        "contact_config_id": "cfg-1",
        "contact_external_user_id": "ext-1",
        "contact_profile_data": {},
        "last_message_id": uuid.uuid4() if populated else None,
        "last_message_direction": "inbound",
        "last_message_at": now,
        "last_message_payload": {"content": "你好"},
        "next_event_kind": "follow_up" if populated else None,
        "next_event_due_at": now if populated else None,
    }
    return base


@pytest.mark.parametrize("populated", [True, False], ids=["四格都有值", "四格都是空"])
def test_a_conversation_row_matches_its_model(populated: bool) -> None:
    produced = _conversation_summary(_conversation_row(populated=populated))
    assert_shape(produced, ConversationListItem)


def test_the_four_nullable_slots_are_null_and_not_absent() -> None:
    """**空态是 `null` 不是缺席**（ADR-0076 §四.2）。

    缺席会被读成「系统知道而没告诉你」，`null` 说的是「这条会话没有这件事」。
    这一条与上面那条不同：上面问形状，这一条问**空的表达方式**。
    """

    produced = _conversation_summary(_conversation_row(populated=False))
    for slot in ("contact", "last_message", "target", "next_event"):
        assert slot in produced, f"{slot} 缺席了，而它应当是一个 null"
        assert produced[slot] is None


def _message_row(*, inbound: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": "你好"}
    if inbound:
        # 入站行是一个 MessageEnvelope，带这三样；出站行是一条发送命令，没有。
        payload |= {
            "actor": {"kind": "contact"},
            "occurred_at": datetime.now(UTC).isoformat(),
            "correlation_id": str(uuid.uuid4()),
        }
    return {
        "id": uuid.uuid4(),
        "direction": "inbound" if inbound else "outbound",
        "created_at": datetime.now(UTC),
        "delivery_status": "DELIVERED",
        "payload": payload,
    }


@pytest.mark.parametrize("inbound", [True, False], ids=["入站", "出站"])
def test_a_message_row_matches_its_model(inbound: bool) -> None:
    assert_shape(_message_projection(_message_row(inbound=inbound)), MessageItem)


def test_the_three_direction_dependent_keys_are_absent_not_null_on_an_outbound_row() -> None:
    """**出站行里那三个键是缺席，不是 `null`** —— 而这一条钉的正是那个区别。

    `_message_projection` 的 docstring 逐字：`null` 会声称「我们看了，没有」，
    而一条出站行**没有这样一件事**（它是发送命令，不是信封）。
    """

    outbound = _message_projection(_message_row(inbound=False))
    for key in ("actor", "occurred_at", "correlation_id"):
        assert key not in outbound

    inbound = _message_projection(_message_row(inbound=True))
    for key in ("actor", "occurred_at", "correlation_id"):
        assert key in inbound


def test_revoked_is_false_and_that_is_an_answer() -> None:
    """撤回被 ADR-0048 K15 移出，没有任何 route 能撤回任何东西。

    **所以这个 `False` 是答案不是占位**，而界面据此**不放撤回入口**（不是置灰）。
    哪天撤回回来了，这条会红并提醒人回去改界面那一格。
    """

    assert _message_projection(_message_row(inbound=True))["revoked"] is False


def test_a_contact_row_matches_its_model() -> None:
    assert_shape(
        _contact_projection(
            {
                "id": uuid.uuid4(),
                "config_id": "cfg-1",
                "external_user_id": "ext-1",
                # ⚠️ 恒空（T179：M1a 不调渠道 profile API）。这里给一个空 dict
                # 是**照实**，不是夹具偷懒 —— 界面上那一格要写明这件事。
                "profile_data": {},
                "first_seen_at": datetime.now(UTC),
                "last_seen_at": datetime.now(UTC),
                "message_count": 7,
            }
        ),
        ContactDetail,
    )


def test_the_comparator_itself_would_notice_a_mismatch() -> None:
    """**判据自己不能是空的。**

    上面那些今天绿着，而一条绿着的守卫永远不显示它的报错。
    这里喂它两种已知的不匹配，确认它两种都认得出来。
    """

    class Tiny(_Shape):
        a: str
        b: str | None = None

    assert_shape({"a": "x"}, Tiny)  # 少一个**可选**键是允许的

    with pytest.raises(AssertionError, match="多了模型没有的键"):
        assert_shape({"a": "x", "c": "y"}, Tiny)

    with pytest.raises(AssertionError, match="少了模型声明的必填键"):
        assert_shape({"b": "y"}, Tiny)

    class Outer(_Shape):
        inner: Tiny

    with pytest.raises(AssertionError, match=r"inner: 产出多了"):
        assert_shape({"inner": {"a": "x", "zzz": 1}}, Outer)
