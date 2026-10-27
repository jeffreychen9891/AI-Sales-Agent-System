"""「配得过去，而那份资料永远发不出去」的七种坏法，封版都拒。

## 为什么有这份

用户 2026-09-17：「查查其他发资料情况会不会遇到同样的问题，
**相同类型的问题不要反复出现**。」

那天撞的是「素材改了名而阶段的 `after` 没跟上」。
审计逐个坏给封版看，**七种全被拦住了** —— 也就是这一族在包这一层是关着的。

**而『今天是关着的』与『有东西钉着它』是两件事。**
这份把那次审计的结论钉下来：下一个人不必再从头查一遍，
而其中任何一道哪天被放松，会在这里红，不是在 stage 上红。

## 这七种为什么危险

**错的方向是安静的**：这些包都能封版、能加载、能发布、能绑定，
对话也照常跑 —— 只有那份资料永远不出现，而没有任何东西说过一句话。
症状要到「客户问了报价单却没收到」那一刻才有人发现，
而那时排查的人会从链路的另一头开始找。

## 它不守什么

**不守「这份资料今天真的发出去过」。** 那要一次真实对话，
判据在运维那边。这里守的是「**发不出去的形状进不了包**」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPOSITORY_ROOT / "profiles" / "internal-demo-sales"
SEALER = REPOSITORY_ROOT / "scripts" / "seal_profile_package.py"


def _document() -> dict[str, Any]:
    return json.loads((PACKAGE / "profile.json").read_text(encoding="utf-8"))


def _introduce(document: dict[str, Any]) -> dict[str, Any]:
    # ⚠️ **2026-09-21 从 `introduce` 改到 `trial_close`**：拆帧之后
    # 三种 `availability`（proactive / on_request / timer）齐全的只剩它一个 ——
    # 本文件要的正是「每一种形状都送得出去」，所以跟着那三种走。
    return next(f for f in document["target_frames"] if f["id"] == "trial_close")


def _seal(package: Path) -> subprocess.CompletedProcess[str]:
    """跑一次封版。

    ## `PYTHONIOENCODING` 这一行是必需的，不是保险

    封版器对这七种里的两种（阶段永远到不了）**用中文报拒绝理由**，
    而 Windows 上子进程默认按 cp1252 写 stdout —— 读取线程随后
    按 utf-8 解，当场抛在线程里，`result.stdout` **变成 `None`**，
    用例红在 `TypeError: can only concatenate str (not "NoneType")`，
    离真正的原因很远。

    **2026-09-17 实测**：本机跑绿（我的环境里带着这个变量），
    门禁里红，而且只红那两条中文的 —— 又一次「在我机器上是绿的」。
    """

    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(  # noqa: S603 - 固定命令，参数由本用例给
        [sys.executable, str(SEALER), str(package)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        cwd=REPOSITORY_ROOT,
        check=False,
    )


def _package_with(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """在临时目录里造一份坏掉的包。

    **不在仓里的那份上改**：一次被打断的用例会留下一个坏掉的配包，
    而下一个人看到的是「封版戳对不上」这种离现场很远的症状。
    """

    package = tmp_path / "pkg"
    package.mkdir()
    document = _document()
    mutate(document)
    (package / "profile.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (package / "materials.json").write_text(
        (PACKAGE / "materials.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return package


def _stage_after_names_a_material_that_is_not_offered(document: dict[str, Any]) -> None:
    _introduce(document)["material_stages"][1]["after"] = ["material:demo-no-such-thing:v1"]


def _asset_waits_on_a_stage_that_does_not_exist(document: dict[str, Any]) -> None:
    for item in _introduce(document)["content_assets"]:
        if item.get("availability") == "proactive":
            item["stages"] = ["99-根本没有这个阶段"]
            return
    raise AssertionError("这个帧里没有 proactive 资料了，这条要跟着改")


def _asset_waits_on_a_topic_outside_the_slot_vocabulary(document: dict[str, Any]) -> None:
    for item in _introduce(document)["content_assets"]:
        if item.get("availability") == "on_request":
            item["topics"] = ["这个话题槽位里没有"]
            return
    raise AssertionError("这个帧里没有 on_request 资料了，这条要跟着改")


def _asset_waits_on_an_event_the_frame_never_emits(document: dict[str, Any]) -> None:
    for item in _introduce(document)["content_assets"]:
        if item.get("availability") == "timer":
            item["emit_kinds"] = ["never_emitted_kind"]
            return
    raise AssertionError("这个帧里没有 timer 资料了，这条要跟着改")


def _the_first_stage_waits_on_something(document: dict[str, Any]) -> None:
    frame = _introduce(document)
    frame["material_stages"][0]["after"] = list(frame["material_stages"][1]["after"])


def _two_stages_wait_on_the_same_material(document: dict[str, Any]) -> None:
    stages = _introduce(document)["material_stages"]
    stages[2]["after"] = list(stages[1]["after"])


def _materials_declare_stages_but_the_frame_has_none(document: dict[str, Any]) -> None:
    _introduce(document)["material_stages"] = []


#: 每一条都是「配得过去、而那份资料永远发不出去」。
UNDELIVERABLE: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    (
        "阶段等一条本帧不提供的素材",
        _stage_after_names_a_material_that_is_not_offered,
        "waits on materials",
    ),
    ("资料挂在不存在的阶段上", _asset_waits_on_a_stage_that_does_not_exist, "waits on stag"),
    (
        "资料的话题不在槽位词表里",
        _asset_waits_on_a_topic_outside_the_slot_vocabulary,
        "waits on topics",
    ),
    ("timer 档等一个本帧不发的事件", _asset_waits_on_an_event_the_frame_never_emits, "waits on"),
    ("第一档阶段也要等素材", _the_first_stage_waits_on_something, "永远到不了"),
    ("两档等同一条素材", _two_stages_wait_on_the_same_material, "永远到不了"),
    ("素材声明阶段而帧没有阶段", _materials_declare_stages_but_the_frame_has_none, "contract"),
]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [(mutate, expected) for _, mutate, expected in UNDELIVERABLE],
    ids=[name for name, _, _ in UNDELIVERABLE],
)
def test_a_package_whose_material_can_never_be_delivered_is_refused(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], expected: str
) -> None:
    """**七种坏法，封版一律拒**，而且拒的理由指得出是哪一处。"""

    result = _seal(_package_with(tmp_path, mutate))
    output = result.stdout + result.stderr

    assert result.returncode != 0, f"这份包封住了，而它的资料发不出去：\n{output}"
    assert expected in output, f"拒了，但理由里找不到 {expected!r}：\n{output}"


def test_the_healthy_package_still_seals(tmp_path: Path) -> None:
    """**不是把什么都拒掉** —— 否则上面七条证明不了任何事。"""

    result = _seal(_package_with(tmp_path, lambda _: None))
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"


def test_every_timer_tier_asset_has_a_strategy_that_may_send_it() -> None:
    """**timer 档那一轮得有人能发得出去** —— 这一条封版查不到。

    定时器那一轮只允许 `time_rule_strategy_set` 里的策略，
    而资料要靠一个允许 `send_material` 的策略才发得出去。
    两处都在配包里，**而没有任何地方把它们对起来看** ——
    两边各自合法，合在一起是「这份资料永远轮不到」。

    今天是通的（`advance_goal` / `propose_followup` 两个都允许），
    **而『今天是通的』不等于有东西钉着它**。
    """

    document = _document()
    strategies = document["strategies"]

    for frame in document["target_frames"]:
        timer_refs = [
            item["ref"]
            for item in (frame.get("content_assets") or [])
            if item.get("availability") == "timer"
        ]
        if not timer_refs:
            continue

        senders = [
            name
            for name in (frame.get("time_rule_strategy_set") or [])
            if "send_material" in (strategies.get(name) or {}).get("allowed_intent_kinds", [])
        ]

        assert senders, (
            f"帧 {frame['id']} 有 timer 档资料 {timer_refs}，"
            f"而定时器那一轮允许的策略 {frame.get('time_rule_strategy_set')} "
            "里没有一个发得出资料 —— 它永远轮不到"
        )
