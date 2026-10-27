"""`entry_conditions` 收掉了，而收掉要能证明（批三，2026-09-24，用户裁定）。

## 它是什么、为什么收

M0 骨架起的裸占位（`[{"type": "object"}]` 都没有——items 无内层约束）：
平台侧**零读点**、§4.1 给了 `exit_conditions` 一行语义而它一行都没有、
三个在架包写满的 **11 条谓词**点名的六个概念（`customer_asked_about` /
`customer_raised_concern` / `fact:*` …）**全仓零实现** ——
商家写了话，没有任何东西在听。`CLAUDE.md` 第三节的形状，主语是配包。

## 逐帧对账：11 条对 11 条，职能全部有承接方

| 原谓词 | 条数 | 今天谁在干 |
|---|---|---|
| `conversation_started` | 3 | 根帧约定（`_initial_aggregate` 取 `target_frames[0]`）|
| `root_goal` / `goal_resolved` | 3 | `closing_frame_ref` + `should_enter_closing_frame` |
| `slot present / equals` | 2 | `advance_when` 逐字表达 |
| 语义类（问到钱 / 提疑虑） | 3 | 模型判断（goal 明写；用户 2026-09-22）|

## 顺手补的另一半：`advance_when` 自己零封版校验

顶替者不能长出同一种洞：`push` 指错帧运行期 KeyError、指平台帧/自己是
死规则、`delivered` 的 ref 没人声明或 `slot` 不在本帧则条件恒假 ——
四种写错法此前都封得出包。`_check_advance_when_is_wired` 一次钉住。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.profiles.registry import (
    ProfilePackageError,
    _check_advance_when_is_wired,
)
from fastagent_v2.runtime.contracts import validate_contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("internal-demo-sales", "synthetic-carrier", "synthetic-third")


def _profile(name: str) -> dict[str, Any]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles" / name / "profile.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 收掉的那半
# ---------------------------------------------------------------------------


def test_the_schema_now_rejects_a_frame_that_carries_the_key() -> None:
    """**带 `entry_conditions` 的包封版即拒** —— `additionalProperties: false` 兜住。

    这一条是「收掉」的会红守卫：哪天有人把这个键加回 schema 而不加读者，
    这条用例红，并指着 RFC §3.6 那条修订标注问「承接方是谁」。
    """

    document = _profile("synthetic-third")
    document["target_frames"][0]["entry_conditions"] = [
        {"fact": "conversation_started", "equals": True}
    ]

    with pytest.raises(Exception, match="entry_conditions"):
        validate_contract("merchant-profile", document)


def test_no_shipped_package_carries_the_key_any_more() -> None:
    """三个在架包一条都不剩 —— 11 条无人听的谓词全部删净。"""

    for name in PACKAGES:
        for frame in _profile(name)["target_frames"]:
            assert "entry_conditions" not in frame, (name, frame["id"])


def test_the_platform_has_no_reader_left_to_orphan() -> None:
    """**收掉前的前提**：生产代码里没有任何读者会因此断掉。

    量的是生产包（services/ + packages/runtime），不含 tests。

    ⚠️ **判据是带引号的键字面量 `"entry_conditions"`，不是裸词** ——
    代码里访问/写入这个键必带引号，而**讲历史的注释不带**
    （`_check_advance_when_is_wired` 的 docstring 就提了它一次）。
    第一版用裸词 grep，当场抓到的是**在说它没了**的那句话 ——
    本仓三天里第五次撞「某几个字不许出现」分不清两种在场。
    `platform_frames.py` 给平台帧造空列表的写点（带引号的那种）随本批删了。
    """

    hits = subprocess.run(  # noqa: S603 - 固定 argv，无 shell
        [
            shutil.which("git") or "git",
            "grep", "-l", '"entry_conditions"', "--",
            "services/api/src", "services/worker/src", "services/ingress/src",
            "packages/runtime/src", "packages/channels/src",
        ],
        capture_output=True,
        cwd=REPOSITORY_ROOT,
    ).stdout.decode()
    assert hits.strip() == "", (
        f"生产代码里还有 entry_conditions 的读者/写者：{hits} —— "
        "要么它复活了（去看 RFC §3.6 修订标注要求的承接方），要么是残留"
    )


# ---------------------------------------------------------------------------
# 顶替者的封版校验
# ---------------------------------------------------------------------------


def _package_with_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_frames": [
            {
                "id": "a",
                "content_materials": [{"ref": "material:m:v1"}],
                "information_slots": [{"name": "x"}],
                "advance_when": [rule],
            },
            {"id": "b", "content_materials": [], "information_slots": []},
        ]
    }


def test_a_push_at_a_missing_frame_is_refused() -> None:
    """指错帧在运行期是 `advance_into_frame` 的 KeyError —— **在真实回合里炸**。"""

    with pytest.raises(ProfilePackageError, match="is not a frame"):
        _check_advance_when_is_wired(
            _package_with_rule({"slot": "x", "present": True, "push": "typo"}),
            Path("p.json"),
        )


def test_a_push_at_itself_is_refused() -> None:
    """运行期对 `target == current` 直接 `continue` —— 一条永不触发的死规则。"""

    with pytest.raises(ProfilePackageError, match="itself"):
        _check_advance_when_is_wired(
            _package_with_rule({"slot": "x", "present": True, "push": "a"}),
            Path("p.json"),
        )


def test_a_delivered_ref_nobody_declares_is_refused() -> None:
    """没人声明的 ref 永远不会被投递 —— 条件恒假，死规则。"""

    with pytest.raises(ProfilePackageError, match="never be delivered"):
        _check_advance_when_is_wired(
            _package_with_rule({"delivered": "material:ghost:v1", "push": "b"}),
            Path("p.json"),
        )


def test_a_slot_from_another_frame_is_refused() -> None:
    """运行期读的是**当前帧**的 `slots` —— 别的帧的槽在这儿恒 `None`。"""

    with pytest.raises(ProfilePackageError, match="this frame's"):
        _check_advance_when_is_wired(
            _package_with_rule({"slot": "not_here", "present": True, "push": "b"}),
            Path("p.json"),
        )


def test_a_wired_rule_seals() -> None:
    """反侧：四样都指得到真东西就放行 —— 没有这一条，恒拒的检查也让上面全绿。"""

    _check_advance_when_is_wired(
        _package_with_rule({"delivered": "material:m:v1", "push": "b"}),
        Path("p.json"),
    )
    _check_advance_when_is_wired(
        _package_with_rule({"slot": "x", "equals": "认可", "push": "b"}),
        Path("p.json"),
    )


def test_the_shipped_rules_are_all_wired() -> None:
    """在架三包的每一条 `advance_when` 都过得了新检查 —— 存量没有死规则。"""

    for name in PACKAGES:
        _check_advance_when_is_wired(_profile(name), Path(name))

# ---------------------------------------------------------------------------
# 同族顺手钉住：business_event_type（批三扫描的唯一新发现）
# ---------------------------------------------------------------------------


def test_a_rule_gated_on_a_business_event_is_refused_at_seal_time() -> None:
    """**声明即死规则，封版要拦**（此前只有运行期的静默 refuse）。

    整条链三层都空：ingress 把 `business_event` 写死 `None`、
    `time_rules._when_allows` 对它恒 `False`、零配包声明。
    入口 / 词表 / 消费三件在 **T502**；建成之前明确拒绝并告知，
    别让「发货后 2 小时跟进」这样的规则静默不存在。
    """

    from fastagent_v2.profiles.registry import (
        _check_time_rules_do_not_gate_on_business_events,
    )

    document = {
        "target_frames": [
            {
                "id": "a",
                "time_rules": [
                    {"id": "r1", "when": {"business_event_type": "order_shipped"}}
                ],
            }
        ]
    }

    with pytest.raises(ProfilePackageError, match="T502"):
        _check_time_rules_do_not_gate_on_business_events(document, Path("p.json"))


def test_a_rule_without_that_gate_seals() -> None:
    """反侧：不带那个门控的规则照常放行 —— 在架三包全部过得了它。"""

    from fastagent_v2.profiles.registry import (
        _check_time_rules_do_not_gate_on_business_events,
    )

    document = {
        "target_frames": [
            {"id": "a", "time_rules": [{"id": "r1", "when": {"target_active_for_seconds": 60}}]}
        ]
    }
    _check_time_rules_do_not_gate_on_business_events(document, Path("p.json"))
    for name in PACKAGES:
        _check_time_rules_do_not_gate_on_business_events(_profile(name), Path(name))
