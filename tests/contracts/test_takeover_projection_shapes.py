"""接管那几格投影的形状，**一路比到嵌套层**。

## 它补的是哪一块

`test_response_models_match_handlers.py` 读的是 handler 的字面量 `return`，
所以它**只看得见顶层键**（外加 `{receipt, result}` 里的 `result`）。
而 `GET /conversations/{id}` 的 `target_stack` 一行里嵌着 `slots`，
`takeover_projection` 那一格底下还有六格 —— **只比顶层，等于对这一族什么都没比**。

`test_projection_shapes.py`（批 6）对会话列表那一族做了同样的事，
判据同源：**拿真输入调产出方，把结果递归比到模型**。

## 这里比哪几个，为什么不是全部

能**不要数据库**调出来的都在这里：`_frame_projection`、`_slot_projection`、
`_takeover_projection`、`commitment_display`、`outcome_display`。

**`takeover_period` 与整格 governance 不在这里**，它们要连库
（`audit_events` / `policy_decisions` 的窄 SELECT，迁移 `042`）。
给它们编几行假数据喂进去，是把「我以为库里的行长什么样」当成事实 ——
本仓为那种形状付过账。它们在 `tests/integration/test_takeover_and_resume_flow.py`
里**在真库上走出来**。

## 两件它刻意不做

* **不验值，只验形状。** 「挂起了几帧算得对不对」是别的用例的事；
  这里问的是「客户端按模型去读，会不会读到一个不存在的键」。
* **不给 `pending_exit_signal` / `takeover_period` 写封闭模型。**
  它们有多种键集合，而**变体本身就是信息**（见 `TakeoverProjectionShape`）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]

# 先 import `dump_openapi` 把环境备齐，理由见 `test_projection_shapes.py`。
sys.path.insert(0, str(ROOT / "scripts"))
import dump_openapi  # noqa: E402,F401
from fastagent_v2.api.app import (  # noqa: E402
    _frame_projection,
    _takeover_projection,
)
from fastagent_v2.api.commitments_display import commitment_display  # noqa: E402
from fastagent_v2.api.outcome_display import outcome_display  # noqa: E402
from fastagent_v2.api.responses import (  # noqa: E402
    CommitmentDisplay,
    FrameItem,
    OutcomeDisplay,
    SlotItem,
    SlotsWritten,
)


def _assert_shape(produced: dict[str, Any], model: type[BaseModel], where: str) -> None:
    """产出的键集合 == 模型声明的键集合。**报错带路径**，否则嵌套层红了没法定位。"""

    assert set(produced) == set(model.model_fields), (
        f"{where}：模型说 {sorted(model.model_fields)}，"
        f"而产出方给 {sorted(produced)}"
    )


#: 一帧引擎可能产出的样子。
#:
#: ⚠️ **槽位的值在 `slots`，出处在另一个字典 `slot_evidence`，按名字对齐。**
#: 第一版把 `source` 写进了 `slots[name]` 里，于是三个桶全空 ——
#: **这条用例当场红了**，而那正是自造夹具最容易藏住的一种形状错：
#: 结构长得像，产出方按另一处读，结果安静地全落进 `unattributed`。
A_FRAME: dict[str, Any] = {
    "id": "greeting",
    "definition_id": "greeting@1",
    "goal": "先把客户想买什么问清楚",
    "status": "active",
    "entered_at": "2026-09-20T01:00:00Z",
    "last_progress_at": "2026-09-20T01:05:00Z",
    "parent_frame_id": None,
    "interruption_reason": None,
    "resume_confirmation_policy": "always",
    "frame_generation": 3,
    "slots": {"budget": "三万"},
    "slot_evidence": {
        "budget": {
            "confidence": 0.9,
            "source": "human",
            "recorded_at": "2026-09-20T01:04:00Z",
            "evidence_event_ids": ["m-1", "m-2"],
        }
    },
}


def test_a_frame_matches_its_model_down_to_the_slots() -> None:
    """一帧与它的模型逐键对上，**包括 `slots` 里那一层**。

    `slots` 是**列表**不是字典 —— 顺序是投影的一部分，
    而一个把它声明成 `dict` 的模型会让前端写出一段按 key 取值的代码，
    在真数据上永远取不到。
    """

    frame = _frame_projection(A_FRAME)
    _assert_shape(frame, FrameItem, "target_stack[]")

    assert isinstance(frame["slots"], list), "`slots` 是列表，不是字典"
    assert frame["slots"], "这条用例要至少一个槽位，否则它什么都没比"
    for slot in frame["slots"]:
        _assert_shape(slot, SlotItem, "target_stack[].slots[]")


def test_a_frame_with_no_slots_still_matches() -> None:
    """没有槽位的帧也要是同一个形状 —— 空列表，不是缺键。"""

    frame = _frame_projection({"id": "empty"})
    _assert_shape(frame, FrameItem, "target_stack[]（无槽位）")
    assert frame["slots"] == []


def test_the_takeover_projection_matches_the_three_blocks_it_owns() -> None:
    """`_takeover_projection` 自己那三格，逐格比到底。

    `governance` 那三格是另一个函数产的（要连库），合并发生在 route 里 ——
    所以这里只比这个函数**真正负责**的那三格。
    """

    projection = _takeover_projection([A_FRAME], None, [])

    assert set(projection) == {
        "slots_written",
        "pending_exit_signal",
        "commitments_registered",
    }
    _assert_shape(projection["slots_written"], SlotsWritten, "slots_written")
    _assert_shape(
        projection["commitments_registered"], CommitmentDisplay, "commitments_registered"
    )


def test_slots_written_splits_by_provenance_and_keeps_the_third_bucket() -> None:
    """人写的、模型写的、**说不清的** —— 第三格不是凑数。

    ADR-0054 §三.2 加出处之前写下的行没有 `source`，
    把它们算进任何一边都是在编。这条用例喂三种行，看三格各接住一个。
    """

    frame = {
        "id": "f",
        "slots": {"a": "x", "b": "y", "c": "z"},
        # **`c` 在这里故意没有 evidence 行** —— 那是 ADR-0054 §三.2 之前
        # 写下的槽位的真实样子：有值，没有出处。
        "slot_evidence": {"a": {"source": "human"}, "b": {"source": "model"}},
    }
    written = _takeover_projection([frame], None, [])["slots_written"]

    assert len(written["human"]) == 1
    assert len(written["model"]) == 1
    assert len(written["unattributed"]) == 1, (
        "没有 `source` 的槽位必须落进 unattributed —— "
        "把它算进 human 或 model 都是在替一条读不出来的记录作证"
    )


def test_the_exit_signal_has_two_shapes_and_both_are_answers() -> None:
    """**没有信号时也给一格，而那一格说清它是哪一种没有。**

    缺席会被读成「系统看了但选择不显示」。这里两支都走一遍：
    没有信号 → `{present, note}`；有信号 → **原样带出引擎写下的那一份**。
    """

    absent = _takeover_projection([], None, [])["pending_exit_signal"]
    assert set(absent) == {"present", "note"}
    assert absent["present"] is False

    # 引擎真写下一个信号时，这一格是它那份文档本身 —— 键集合不归我们定，
    # 所以模型那一侧是开放 dict（见 `TakeoverProjectionShape` 的 docstring）。
    engine_wrote = {"kind": "off_topic", "observed_at": "2026-09-20T02:00:00Z"}
    present = _takeover_projection([], engine_wrote, [])["pending_exit_signal"]
    assert present == engine_wrote, "有信号时不许重新包装 —— 原样带出"


def test_the_two_display_helpers_match_their_models() -> None:
    """`commitment_display` / `outcome_display` —— 两个纯函数，直接问它们。

    两者都属于「**字段照显，旁边说清这个空是哪一种空**」那一类裁定
    （用户 2026-09-13）：光秃秃的 `0` 与 `null` 是会误导人的真值。
    """

    _assert_shape(commitment_display([]), CommitmentDisplay, "commitments_registered")
    _assert_shape(outcome_display(None), OutcomeDisplay, "outcome")
