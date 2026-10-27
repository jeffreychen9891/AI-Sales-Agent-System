"""`ScriptedInference` 回放的那些文档，**平台自己的校验器接不接受**。

照 `test_synthetic_carrier_executability.py` 的形状写，理由也是同一个：
那份语料当初「schema 合法、自洽、哈希钉死」而**没有人执行过它**，
于是两条让主路径不可执行的缺陷对一整套绿用例是隐形的。

一个演示场景文件有完全一样的风险，而且后果更难看：
**它会在面试官面前失败**，而失败的样子是「系统决定不回话」
（计划被拒 → 安全阀 → 不发），看起来像产品不行，而真因是场景文件与配包对不上号。

所以本文件做三件事：

1. **把每一轮录的计划与生成，逐轮驱动真实的 `validate_plan` /
   `validate_generation`**，聚合在轮次之间**carry forward**；
2. 钉住那几条「平台填身份、文件不许填」的性质；
3. 钉住那几条**拒绝**：脚本用完了拒、重试同一个事件不前进、
   没有出处的文件拒、录了一份真实供应商产不出来的文档拒。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.worker.inference_scripted import (
    STEP_CONTRACTS,
    ScenarioError,
    ScriptedInference,
)
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    PlanRejected,
    validate_generation,
    validate_plan,
)
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.providers import ProviderMalformedOutput

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = ROOT / "demo" / "scenarios"
WALKTHROUGH = SCENARIO_ROOT / "carrier-rental-walkthrough.json"


def _scenario_files() -> list[Path]:
    return sorted(SCENARIO_ROOT.glob("*.json"))


@pytest.fixture(scope="module")
def profile() -> dict[str, Any]:
    return (
        ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT)
        .package(profile_id="synthetic-two-axis-desk", version="1.0.3")
        .document
    )


def _envelope(*, event_type: str = "customer_message", text: str = "测试入站") -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "content": {"type": "text", "text": text},
    }


# --------------------------------------------------------------- 加载期


def test_the_shipped_scenarios_are_all_loadable() -> None:
    """仓里带的每一份场景文件都得装得进来。

    **参数化在文件上而不是写死一个名字**：加一份新场景时，
    它要么跟着被验，要么根本不在这个目录 —— 没有「加了但没人验」的第三种。
    """

    files = _scenario_files()
    assert files, f"no scenario files under {SCENARIO_ROOT}"
    for path in files:
        double = ScriptedInference.from_path(path)
        assert double.scenario_id
        assert double.provenance
        assert double.turn_labels()


def test_the_version_is_content_addressed(tmp_path: Path) -> None:
    """改一个字，`version` 就变 —— 而它会进 `model_evidence`。

    与配包的 `content_hash` 同一条纪律：**证据要认得出是哪一份文件的哪一版**。
    没有这一条，一次演示的留痕说不出它回放的是改之前还是改之后那一版。
    """

    before = ScriptedInference.from_path(WALKTHROUGH)
    document = json.loads(WALKTHROUGH.read_text(encoding="utf-8"))
    document["turns"][0]["label"] = document["turns"][0]["label"] + "（改过）"
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    after = ScriptedInference.from_path(edited)
    assert before.version != after.version
    assert before.content_sha256 != after.content_sha256


def test_a_scenario_without_a_stated_origin_is_refused(tmp_path: Path) -> None:
    """`provenance` 必填，而这不是形式主义。

    一份没有出处的录制件正是 `AGENTS.md`「跨边界的数据结构」那节点名的
    「**不算满足的**」第一种：自己凭空写的字面量。
    把它写成加载期必填，等于给那条纪律配了一个执行者。
    """

    document = json.loads(WALKTHROUGH.read_text(encoding="utf-8"))
    del document["provenance"]
    path = tmp_path / "no-provenance.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioError, match="provenance"):
        ScriptedInference.from_path(path)


def test_a_document_the_real_provider_could_not_have_returned_is_refused(
    tmp_path: Path,
) -> None:
    """录的文档按**真实供应商那份 schema** 验，不是按「能解析就行」。

    这一条是整个方案的承重墙：它让一份场景文件**不可能漂成生产产不出来的形状**。
    没有它，演示就会变成 `AGENTS.md`「那条用例喂的东西必须是生产里会出现的形状」
    那一节的实例 —— 一份绿着的演示，证明的是一件不会发生的事。
    """

    document = json.loads(WALKTHROUGH.read_text(encoding="utf-8"))
    # `sentiment` 是枚举；`shrug` 不在表里，真实模型在严格 response_format 下产不出来。
    document["turns"][0]["steps"]["understand"]["sentiment"] = "shrug"
    path = tmp_path / "impossible.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioError) as refused:
        ScriptedInference.from_path(path)
    assert "sentiment" in str(refused.value)


def test_every_problem_is_reported_not_just_the_first(tmp_path: Path) -> None:
    """两处坏的要**一次全报出来**。

    `AGENTS.md`「任何『报第一个』的工具，读它的输出不等于读到了全部」
    点名的成员里就有 `jsonschema` 的第一条 violation ——
    「修一条再跑一遍」不是该交给写场景文件的人的循环。
    """

    document = json.loads(WALKTHROUGH.read_text(encoding="utf-8"))
    document["turns"][0]["steps"]["understand"]["sentiment"] = "shrug"
    document["turns"][1]["steps"]["plan"]["strategy_reason_code"] = "lower case"
    path = tmp_path / "two-problems.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioError) as refused:
        ScriptedInference.from_path(path)
    message = str(refused.value)
    assert "turns[0]" in message
    assert "turns[1]" in message
    assert "2 problem(s)" in message


def test_a_scenario_file_records_only_known_steps(tmp_path: Path) -> None:
    """拼错一个步骤名要红，不要静默忽略。

    `steps` 里写了 `generation`（真实名字是 `generate`）时，
    静默忽略的后果是那一轮**没有生成步** —— 而那在演示里长得像
    「模型这一轮没说话」。
    """

    document = json.loads(WALKTHROUGH.read_text(encoding="utf-8"))
    steps = document["turns"][0]["steps"]
    steps["generation"] = steps.pop("generate")
    path = tmp_path / "typo.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioError, match="unknown step"):
        ScriptedInference.from_path(path)


# --------------------------------------------------------------- 回放性质


def test_the_platform_fills_identity_and_the_file_does_not(profile: dict[str, Any]) -> None:
    """`event_id` 与 `slot_updates` 里那两个 uuid 由**平台**盖章。

    与 `inference_model.py:730-747` 逐字同一条路。场景文件写不出运行期的 uuid，
    而这正是 T34 记下的那个形状：问模型要两个它从没见过的 uuid，
    于是 `slot_updates` 在实践上不可填，真实调用里每次都回 `[]`。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    envelope = _envelope(text="想租个投影仪")

    understanding = double.understand(
        envelope=envelope, aggregate=aggregate, profile=profile
    )

    assert understanding["event_id"] == envelope["event_id"]
    assert understanding["schema_version"] == "1.0.0"
    frame_id = str(aggregate["target_stack"][-1]["id"])
    assert understanding["slot_updates"], "这一轮本来录了槽位更新，夹具别改空了"
    for update in understanding["slot_updates"]:
        assert update["frame_id"] == frame_id
        assert update["evidence_event_id"] == envelope["event_id"]
        # 盖的是 uuid，不是随便一个字符串 —— 契约的 format 就是这么要求的。
        UUID(update["frame_id"])


def test_recorded_usage_reaches_the_evidence(profile: dict[str, Any]) -> None:
    """录下来的 token 与延迟进 `model_evidence`，**不是全零**。

    全零的账会让看演示的人以为这个系统不花钱、也不花时间 ——
    而「它多快、多贵」是这类系统第一个被问到的两件事。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    understanding = double.understand(
        envelope=_envelope(), aggregate=aggregate, profile=profile
    )

    evidence = understanding["model_evidence"]
    assert evidence["provider"] == "scripted"
    assert evidence["model"] == double.version
    assert evidence["latency_ms"] > 0
    assert evidence["token_usage"]["input"] > 0
    assert evidence["token_usage"]["output"] > 0


def test_a_retried_event_replays_the_same_turn(profile: dict[str, Any]) -> None:
    """同一个 `event_id` 重试多少次都落在同一轮。

    **这是不用自增计数器的全部理由。** 这个系统有重试
    （`tests/e2e/test_retry_budget.py`），而一个「每调一次 +1」的计数器
    会让一次重试静默地取到下一轮的脚本 —— 那种错不会红，
    它只是让演示从第二轮开始全部错位。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    envelope = _envelope()

    first = double.understand(envelope=envelope, aggregate=aggregate, profile=profile)
    again = double.understand(envelope=envelope, aggregate=aggregate, profile=profile)

    assert first["turn_summary"] == again["turn_summary"]
    assert first["slot_updates"] == again["slot_updates"]


def test_the_script_does_not_wrap_around(profile: dict[str, Any]) -> None:
    """脚本演完就拒，**不重播最后一轮**。

    一个会静默回绕的演示会让看的人以为系统在回应，而它只是在复读。
    抛的是 `ProviderMalformedOutput`，因为那就是它的性质：
    这一层产出了一份管线不能用的东西 —— 走的是现成那条路
    （安全阀 + 带理由码的留痕），不是一条新的失败路径。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    recorded_turns = len(double.turn_labels())

    for _ in range(recorded_turns):
        double.understand(envelope=_envelope(), aggregate=aggregate, profile=profile)

    with pytest.raises(ProviderMalformedOutput) as exhausted:
        double.understand(envelope=_envelope(), aggregate=aggregate, profile=profile)
    assert "does not wrap around" in str(exhausted.value)


def test_a_timer_turn_whose_rule_declares_another_strategy_is_refused(
    profile: dict[str, Any],
) -> None:
    """定时器轮次的策略由规则定（B4-4d）—— 对不上号就报出来。

    不报的后果是校验器把它拒成安全阀，而那在演示里长得像
    「系统决定不回话」。真因是场景文件与配包对不上号，
    两件事该去看的地方不同（`AGENTS.md`「一句正确的报错，可以指向错误的原因」）。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    envelope = _envelope()
    understanding = double.understand(
        envelope=envelope, aggregate=aggregate, profile=profile
    )

    with pytest.raises(ProviderMalformedOutput, match="declares"):
        double.plan(
            understanding=understanding,
            aggregate=aggregate,
            profile=profile,
            # 第 1 轮录的是 `ask_slot`；假装这一轮是一条声明了 `handoff` 的规则点的。
            required_strategy="handoff",
        )


# --------------------------------------------------- 真实校验器驱动每一轮


def _apply_slot_updates(aggregate: dict[str, Any], understanding: dict[str, Any]) -> None:
    """把这一轮录的槽位值写进栈顶帧。

    直接写而不是走 `apply_slot_updates`，与
    `test_synthetic_carrier_executability._apply_recorded_slots` 同一个取舍：
    本用例断言的是「录的那份计划与生成，校验器接不接受」，
    写回路径另有用例钉它。
    """

    frame = aggregate["target_stack"][-1]
    slots = frame.setdefault("slots", {})
    for update in understanding.get("slot_updates", []):
        slots[str(update["slot"])] = update["value"]


def test_every_recorded_turn_is_one_the_validators_accept(profile: dict[str, Any]) -> None:
    """**本文件的主用例**：每一轮的计划与生成，过真实的那两道校验。

    聚合在轮次之间 carry forward —— 这是「一段对话」与「四个各写一半的状态」
    的区别，也是 `test_synthetic_carrier_executability` 当初发现那两条缺陷的办法。

    它红的时候会打出**轮次序号、标题、理由码** —— 三样都要有，
    因为读到它的人下一步要去改场景文件里的那一轮。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    assert aggregate["target_stack"][-1]["definition_id"] == "rental"

    labels = dict(double.turn_labels())
    for ordinal in sorted(labels):
        label = labels[ordinal]
        # 第 3 轮是定时器轮次（那一份录了 `read_silence`）。
        is_timer = "read_silence" in double.recorded_steps(ordinal)
        envelope = _envelope(event_type="timer" if is_timer else "customer_message")

        if is_timer:
            silence = double.read_silence(
                envelope=envelope, aggregate=aggregate, profile=profile
            )
            assert silence["action"]["do"] in {
                "wait",
                "ask_again",
                "send_material",
                "hand_to_human",
            }

        understanding = double.understand(
            envelope=envelope, aggregate=aggregate, profile=profile
        )
        _apply_slot_updates(aggregate, understanding)

        plan = double.plan(
            understanding=understanding, aggregate=aggregate, profile=profile
        )
        try:
            validated = validate_plan(
                plan=plan,
                understanding=understanding,
                aggregate=json.loads(json.dumps(aggregate)),
                profile=profile,
            )
        except PlanRejected as rejected:
            pytest.fail(
                f"turn {ordinal} ({label!r}): the platform refuses the recorded plan -- "
                f"{rejected.reason_code}: {rejected}. Fix that turn in "
                f"{WALKTHROUGH.name}, not the validator."
            )

        generation = double.generate(
            plan=plan,
            understanding=understanding,
            envelope=envelope,
            materials={},
        )
        try:
            validate_generation(generation=generation, plan=validated)
        except GenerationRejected as rejected:
            pytest.fail(
                f"turn {ordinal} ({label!r}): the platform refuses the recorded "
                f"generation -- {rejected.reason_code}: {rejected}. Fix that turn in "
                f"{WALKTHROUGH.name}, not the validator."
            )


def test_every_recorded_step_name_is_one_of_the_four() -> None:
    """`recorded_steps` 只会回四步里的名字 —— 它是演示界面的判据来源。

    界面靠「这一轮录了 `read_silence` 吗」决定要不要画第四步那一格。
    那个集合里混进一个别的名字时，界面画的就是一格不存在的推理。
    """

    double = ScriptedInference.from_path(WALKTHROUGH)
    for ordinal, _ in double.turn_labels():
        assert double.recorded_steps(ordinal) <= frozenset(STEP_CONTRACTS)
        assert double.recorded_steps(ordinal), f"turn {ordinal} records no step"
    # 不存在的轮次回空集，而不是抛 —— 界面问「第 99 轮录了什么」是个合法的问法。
    assert double.recorded_steps(99) == frozenset()
