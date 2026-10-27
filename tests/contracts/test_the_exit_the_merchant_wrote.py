"""商家写的出口判据，终于有人执行（exit/tone 批，2026-09-24，用户裁定两项）。

## 一、exit_conditions 建执行

理由码从第一天起就叫 `EXIT_CONDITION_NOT_SATISFIED` —— **名字一直在说
该核出口条件，核的却是另一套**（`information_slots.required` 全满）。
两套在 carrier 包上双向分家（量出来的）：

| 帧 | 商家声明的出口 | 闸实际要求 |
|---|---|---|
| carrier rental | 只要 `condition_on_return` | 四个别的 required 槽全满（**比声明严**）|
| demo/third closing | `closing_acknowledged`（非 required）| 视而不见（**模型自判**，§4.1 禁止）|

今天起两套各归各：**required = 要收集什么**（ask 链 /
`STRATEGY_SKIPS_REQUIRED_SLOT`，一字未动），**exit = 什么时候算完**
（complete 闸读谓词：present / equals，schema 钉形状，封版校验 slot 在本帧）。

## 二、tone_material_ref 收掉

三包 11 帧全配了它，**指向不存在的素材**（封版只检语法不检存在），
运行期**零消费** —— 双重空。用户裁定：回归确认那句话由模型按上下文
自己写（与静默唤醒同一条哲学），旋钮不该存在。`policy` 保留且活着。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_exit_conditions_point_at_this_frames_slots,
)
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.worker.inference_validation import _unmet_exit_conditions

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("internal-demo-sales", "synthetic-carrier", "synthetic-third")


def _profile(name: str) -> dict[str, Any]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles" / name / "profile.json").read_text(encoding="utf-8")
    )


def _definition(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "a", "exit_conditions": conditions}


def _frame(slots: dict[str, Any]) -> dict[str, Any]:
    return {"definition_id": "a", "slots": slots}


# ---------------------------------------------------------------------------
# 一、complete 闸读的是商家的谓词
# ---------------------------------------------------------------------------


def test_a_present_condition_is_unmet_until_the_slot_has_a_value() -> None:
    """`{slot, present: true}`：空串与 `None` 都算没有 —— 与写回侧拒 `null` 同口径。"""

    definition = _definition([{"slot": "price_quoted", "present": True}])

    assert _unmet_exit_conditions(frame=_frame({}), definition=definition)
    assert _unmet_exit_conditions(frame=_frame({"price_quoted": ""}), definition=definition)
    assert _unmet_exit_conditions(
        frame=_frame({"price_quoted": None}), definition=definition
    )
    assert not _unmet_exit_conditions(
        frame=_frame({"price_quoted": "1888 包月"}), definition=definition
    )


def test_an_equals_condition_wants_that_value_not_any_value() -> None:
    """`{slot, equals: 值}`：有值但不是那个值，仍然没完 —— 与 `advance_when` 同读法。

    **修复前这种谓词整个不存在**：闸只会问「required 槽满了吗」，
    「usage_intent 必须是认可」这类出口写了也没人执行。
    """

    definition = _definition([{"slot": "usage_intent", "equals": "认可"}])

    assert _unmet_exit_conditions(frame=_frame({"usage_intent": "保留"}), definition=definition)
    assert _unmet_exit_conditions(frame=_frame({}), definition=definition)
    assert not _unmet_exit_conditions(
        frame=_frame({"usage_intent": "认可"}), definition=definition
    )


def test_the_gate_no_longer_asks_about_required_slots() -> None:
    """**carrier rental 的形状**：出口只点一个槽，别的 required 空着也算完。

    修复前闸要求四个 required 全满 —— 比商家声明的严，
    「记下归还状态就完事」这句配包写了等于没写。
    required 的收集语义由 ask 链管，与「什么时候算完」不再混同。
    """

    definition = {
        "id": "rental",
        "exit_conditions": [{"slot": "condition_on_return", "present": True}],
        "information_slots": [
            {"name": "condition_on_return", "required": False},
            {"name": "item_category", "required": True},
            {"name": "deposit_amount", "required": True},
        ],
    }

    assert not _unmet_exit_conditions(
        frame=_frame({"condition_on_return": "无破损"}), definition=definition
    ), "出口条件满足了还拦 —— 闸又在读 required 那张表了"


def test_a_non_required_exit_slot_is_finally_enforced() -> None:
    """**closing 的形状**：出口点了非 required 槽，修复前闸视而不见（模型自判）。"""

    definition = {
        "id": "closing",
        "exit_conditions": [{"slot": "closing_acknowledged", "present": True}],
        "information_slots": [{"name": "closing_acknowledged", "required": False}],
    }

    assert _unmet_exit_conditions(frame=_frame({}), definition=definition), (
        "closing_acknowledged 空着而闸放行 —— 又回到「模型觉得完成了」"
    )


# ---------------------------------------------------------------------------
# 二、封版：形状 + 跨引用
# ---------------------------------------------------------------------------


def test_a_predicate_pointing_at_a_missing_slot_is_refused() -> None:
    """点一个本帧没有的槽 = 这一帧永远 complete 不了 —— 死锁检查第三条。"""

    document = {
        "target_frames": [
            {
                "id": "a",
                "information_slots": [{"name": "x"}],
                "exit_conditions": [{"slot": "typo", "present": True}],
            }
        ]
    }

    with pytest.raises(ProfilePackageError, match="can never complete"):
        _check_exit_conditions_point_at_this_frames_slots(document, Path("p.json"))


def test_a_wired_predicate_seals_and_the_shipped_packages_pass() -> None:
    """反侧 + 存量：三包 11 帧的谓词全部指得到本帧的槽。"""

    document = {
        "target_frames": [
            {
                "id": "a",
                "information_slots": [{"name": "x"}],
                "exit_conditions": [{"slot": "x", "present": True}],
            }
        ]
    }
    _check_exit_conditions_point_at_this_frames_slots(document, Path("p.json"))
    for name in PACKAGES:
        _check_exit_conditions_point_at_this_frames_slots(_profile(name), Path(name))


def test_a_free_shaped_predicate_is_refused_by_the_contract() -> None:
    """形状闭合了：`{fact: …}`、`{all_slots_filled: …}` 这类自由谓词封版即拒。

    修复前 `exit_conditions` 的 items 是裸 `{"type": "object"}` ——
    与 `entry_conditions`（批三收掉的那个）同族的无约束占位。
    """

    document = _profile("synthetic-third")
    # 载荷是**纯自由键**（不带 equals）—— 反向探针实测：带 `equals: true` 时
    # 就算把 additionalProperties/required 都放开，properties 里残留的
    # `equals: string` 约束仍会拒它，这条用例就分不出「形状闭合」与
    # 「碰巧踩中别的约束」。
    document["target_frames"][0]["exit_conditions"] = [{"fact": "goal_resolved"}]

    with pytest.raises(Exception, match="exit_conditions|additional"):
        validate_contract("merchant-profile", document)


def test_an_empty_exit_conditions_array_is_refused() -> None:
    """`minItems: 1`：空数组 = 模型随时可收口 —— RFC §4.1 明令禁止的形状。"""

    document = _profile("synthetic-third")
    document["target_frames"][0]["exit_conditions"] = []

    with pytest.raises(Exception, match="exit_conditions|minItems|too short"):
        validate_contract("merchant-profile", document)


# ---------------------------------------------------------------------------
# 三、tone_material_ref 删净
# ---------------------------------------------------------------------------


def test_a_tone_material_ref_no_longer_seals() -> None:
    """带着它的包封版即拒 —— `additionalProperties: false` 兜住，回不来。"""

    document = _profile("synthetic-third")
    document["target_frames"][0]["return_confirmation"]["tone_material_ref"] = (
        "material:neutral-return:v1"
    )

    with pytest.raises(Exception, match="tone_material_ref|additional"):
        validate_contract("merchant-profile", document)


def test_no_shipped_package_carries_a_tone_ref() -> None:
    """三包 11 帧删净；`policy` 那半原样都在（它活着，N1 起读写同源）。"""

    for name in PACKAGES:
        for frame in _profile(name)["target_frames"]:
            confirmation = frame["return_confirmation"]
            assert "tone_material_ref" not in confirmation, (name, frame["id"])
            assert confirmation["policy"] in ("required", "optional", "forbidden")
