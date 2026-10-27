"""`scripts/demo_drive.py` 的驱动计划：**场景与驱动侧对不上时会不会红**。

驱动脚本本体要一套栈才能跑（它存在的意义就是驱动真实链路），
但它最容易坏的地方不在栈上，在**两份输入的同步**：
场景文件改了一轮（加一轮、把客户轮次改成定时器轮次），
`INBOUND_TEXTS` 没跟上 —— 这正是 CLAUDE.md 第三节那一族：
错的方向是安静的，演示会少驱动一轮而不是报错。

所以 `plan_turns` 把「对不上就拒」写成了行为，本文件用反向探针钉住它：
每一种对不上的形状都要**真的抛**，而对上的那份（在架场景 + 在架文本表）
必须**真的过** —— 后者同时钉住了「演示今天是可驱动的」。

quiet hours 预检同理：它拒的方向（在窗口里）与放的方向（在窗口外）都要验。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.worker.inference_scripted import ScriptedInference

from scripts.demo_drive import INBOUND_TEXTS, plan_turns, refuse_if_quiet_hours

ROOT = Path(__file__).resolve().parents[2]
WALKTHROUGH = ROOT / "demo" / "scenarios" / "carrier-rental-walkthrough.json"


@pytest.fixture(scope="module")
def scripted() -> ScriptedInference:
    return ScriptedInference.from_path(WALKTHROUGH)


@pytest.fixture(scope="module")
def profile_document() -> dict[str, Any]:
    return (
        ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
        .package(profile_id="synthetic-two-axis-desk", version="1.0.3")
        .document
    )


def test_the_shipped_scenario_and_the_shipped_texts_agree(
    scripted: ScriptedInference,
) -> None:
    """在架的组合必须规划得出来 —— 这一条红了，演示就是跑不起来的。"""

    actions = plan_turns(scripted, INBOUND_TEXTS)
    assert [action.ordinal for action in actions] == [1, 2, 3, 4]
    # 定时器轮次恰好是第 3 轮，且只有它没有入站文本。
    assert [action.ordinal for action in actions if action.inbound_text is None] == [3]


def test_a_text_aimed_at_the_timer_turn_is_refused(scripted: ScriptedInference) -> None:
    """反向探针：给定时器轮次塞文本，守卫必须真的抛，而不是安静吞掉。"""

    with pytest.raises(ValueError, match="read_silence"):
        plan_turns(scripted, {**INBOUND_TEXTS, 3: "这条不该存在"})


def test_a_customer_turn_without_a_text_is_refused(scripted: ScriptedInference) -> None:
    missing = {ordinal: text for ordinal, text in INBOUND_TEXTS.items() if ordinal != 2}
    with pytest.raises(ValueError, match="客户轮次"):
        plan_turns(scripted, missing)


def test_a_text_for_a_turn_the_scenario_does_not_have_is_refused(
    scripted: ScriptedInference,
) -> None:
    with pytest.raises(ValueError, match="只有"):
        plan_turns(scripted, {**INBOUND_TEXTS, 9: "没有第 9 轮"})


def test_quiet_hours_refuses_inside_and_admits_outside(
    profile_document: dict[str, Any],
) -> None:
    """两个方向都验（放宽一条判据时正反两侧都要验，同一条纪律）。

    配包声明 22:30–08:00 Asia/Shanghai；UTC+8 下 23:00 = UTC 15:00（在窗内），
    12:00 = UTC 04:00（在窗外）。用固定时刻而不是挂钟，免得这条用例按时段变红。
    """

    inside = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
    outside = datetime(2026, 10, 27, 4, 0, tzinfo=UTC)
    with pytest.raises(SystemExit, match="quiet hours"):
        refuse_if_quiet_hours(profile_document, inside)
    refuse_if_quiet_hours(profile_document, outside)
