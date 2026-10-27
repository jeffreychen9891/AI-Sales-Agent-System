"""Tests for the capability gate, tier parameters and the JSON repair layer.

These exist because of a specific incident: a reasoning model spent its whole
output budget on reasoning, returned `finish_reason=length` with nothing
parseable, and nothing in the system would have refused that binding. The gate
turns that lesson into something a future release cannot repeat by accident.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.metrics import (
    SCHEMA_REPAIR_TOTAL,
    SCHEMA_UNREPAIRABLE_TOTAL,
    repair_report,
)
from fastagent_v2.worker.model_policy import (
    MODEL_POLICY_ROOT,
    REALTIME_TIERS,
    ModelPolicyError,
    active_release,
    capabilities,
    capability_for,
    check_realtime_bindings,
    load_release,
)
from fastagent_v2.worker.providers import ProviderMalformedOutput, parse_model_json


@pytest.fixture(autouse=True)
def _reset_counters() -> Any:
    SCHEMA_REPAIR_TOTAL.reset()
    SCHEMA_UNREPAIRABLE_TOTAL.reset()
    yield
    SCHEMA_REPAIR_TOTAL.reset()
    SCHEMA_UNREPAIRABLE_TOTAL.reset()


def test_the_active_release_binds_the_chat_model_to_every_realtime_tier() -> None:
    release = active_release()
    # **2026-09-22：`-7` → `-8`**（ADR-0149）。`-8` 只比 `-7` 多一个角色
    # `SILENCE_CAPABILITY`，绑定逐字复刻 `UNDERSTAND_VALUE`、只换 `output_contract`
    # —— 所以下面那几条「每个实时档都绑同一个聊天模型」的断言**对新角色同样成立**，
    # 而这正是要它们逐档跑一遍的理由：新加一档时，这里是它第一次被验。
    assert release.version == "2026.09.02-8"
    for tier in REALTIME_TIERS:
        binding = release.role(tier)
        assert binding.model_id == "deepseek-v4-flash"
        assert binding.temperature is not None


def test_tiers_are_differentiated_by_parameters_not_by_model() -> None:
    release = active_release()
    assert release.role("PLAN_CAPABILITY").temperature == 0.0
    assert release.role("GENERATE_VALUE").temperature == 0.7
    assert release.role("COMMITMENT_CAPABILITY").max_output_tokens == 768
    assert release.role("PLAN_CAPABILITY").max_output_tokens == 2048


def test_the_release_says_plainly_that_tiers_share_v4_flash() -> None:
    provenance = active_release().document["provenance"]
    assert "V4 Flash" in provenance
    assert "每角色6个多样样本" in provenance


def test_the_binding_records_the_alias_the_ruling_named() -> None:
    """An alias can be repointed by the provider, so the binding names the
    concrete model and keeps the alias only as provenance."""

    binding = active_release().document["roles"]["PLAN_CAPABILITY"]
    assert binding["requested_alias"] == "deepseek-chat"
    assert binding["model_id"] == "deepseek-v4-flash"
    entry = capability_for("deepseek-v4-flash")
    assert entry is not None
    assert "deepseek-chat" in entry["aliases"]


def test_every_realtime_binding_has_a_capability_annotation() -> None:
    check_realtime_bindings(active_release())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("thinking_mode", "enabled", "explicitly disable thinking"),
        ("structured_output", "json_object", "replay-validated protocol"),
        ("output_contract", "plan-result", "must use output_contract"),
    ],
)
def test_an_active_responses_binding_must_lock_its_realtime_contract(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    document = copy.deepcopy(active_release().document)
    document["roles"]["GENERATE_VALUE"][field] = value
    path = tmp_path / "release-9999.01.01-4.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match=message):
        load_release(path)


def test_an_active_v4_binding_cannot_fall_back_to_the_zero_of_six_chat_contract(
    tmp_path: Path,
) -> None:
    document = copy.deepcopy(active_release().document)
    binding = document["roles"]["GENERATE_VALUE"]
    binding["api_compatibility"] = "openai-chat-completions-v1"
    binding["structured_output"] = "json_object"
    binding.pop("output_contract")
    path = tmp_path / "release-9999.01.01-5.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match="replay-validated protocol"):
        load_release(path)


def test_a_release_binding_an_ineligible_model_is_refused_at_load(tmp_path: Path) -> None:
    document = copy.deepcopy(active_release().document)
    document["roles"]["PLAN_CAPABILITY"]["model_id"] = "deepseek-v4-pro"
    path = tmp_path / "release-9999.01.01-1.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match="not eligible for the request path"):
        load_release(path)


def test_a_release_binding_an_unannotated_model_is_refused_at_load(tmp_path: Path) -> None:
    document = copy.deepcopy(active_release().document)
    document["roles"]["GENERATE_VALUE"]["model_id"] = "some-model-nobody-tested"
    path = tmp_path / "release-9999.01.01-2.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match="no capability annotation"):
        load_release(path)


def test_the_ineligible_model_annotation_cites_its_evidence() -> None:
    entry = capability_for("deepseek-v4-pro")
    assert entry is not None
    assert entry["realtime_eligible"] is False
    assert entry["observed_at"] == "2026-09-02"
    assert "finish_reason=length" in entry["observation"]


def test_v4_flash_cannot_activate_until_the_six_of_six_replay_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = copy.deepcopy(capabilities())
    table["deepseek-v4-flash"]["replay_gate_passed"] = False
    monkeypatch.setattr("fastagent_v2.worker.model_policy.capabilities", lambda: table)
    candidate = MODEL_POLICY_ROOT / "release-2026.09.02-4.json"
    document = json.loads(candidate.read_text(encoding="utf-8"))
    document["status"] = "active"
    path = tmp_path / "release-9999.01.01-3.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match="6/6 replay gate"):
        load_release(path)


def test_every_annotation_carries_an_observation() -> None:
    for model_id, entry in capabilities().items():
        assert entry["observation"], f"{model_id} has no observation"
        if entry["realtime_eligible"]:
            assert entry["observed_at"], f"{model_id} is eligible without an observation date"


def test_a_superseded_release_is_not_gated_on_current_capability() -> None:
    """Retired releases stay loadable so old evidence remains resolvable."""

    for name in (
        "release-2026.09.02-1.json",
        "release-2026.09.02-2.json",
        "release-2026.09.02-3.json",
    ):
        release = load_release(MODEL_POLICY_ROOT / name)
        assert release.status == "superseded"


def test_clean_json_needs_no_repair_and_counts_nothing() -> None:
    assert parse_model_json('{"a": 1}') == {"a": 1}
    assert SCHEMA_REPAIR_TOTAL.total() == 0


def test_a_markdown_fence_is_stripped_and_counted() -> None:
    assert parse_model_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert SCHEMA_REPAIR_TOTAL.total() == 1
    assert SCHEMA_REPAIR_TOTAL.value("unknown", "unknown", "unknown", "markdown_fence") == 1


def test_surrounding_prose_is_stripped_and_counted_separately() -> None:
    assert parse_model_json('Sure!\n```\n{"a": 3}\n```\nAnything else?') == {"a": 3}
    assert SCHEMA_REPAIR_TOTAL.value("unknown", "unknown", "unknown", "surrounding_prose") == 1


def test_unrepairable_output_is_refused_and_counted() -> None:
    with pytest.raises(ProviderMalformedOutput):
        parse_model_json("I am afraid I cannot do that")
    assert SCHEMA_UNREPAIRABLE_TOTAL.total() == 1
    assert SCHEMA_REPAIR_TOTAL.total() == 0


def test_a_json_array_is_not_accepted_as_an_object() -> None:
    with pytest.raises(ProviderMalformedOutput):
        parse_model_json('[{"a": 1}]')


def test_repair_never_edits_the_parsed_structure() -> None:
    """Repairs act on the envelope only; no key is added, removed or renamed."""

    document = parse_model_json('```json\n{"a": 1, "b": {"c": 2}}\n```')
    assert document == {"a": 1, "b": {"c": 2}}


def test_the_repair_report_is_shaped_for_the_daily_sync() -> None:
    parse_model_json('```json\n{"a": 2}\n```')
    report = repair_report()
    assert report["schema_repair_total"] == 1
    assert report["schema_unrepairable_total"] == 0
    assert isinstance(report["by_label"], dict)


def test_metric_labels_stay_bounded() -> None:
    """No conversation or tenant identifier may become a metric label."""

    assert SCHEMA_REPAIR_TOTAL.label_names == ("provider", "model", "role", "repair")
    assert "tenant" not in SCHEMA_REPAIR_TOTAL.label_names
    assert "conversation" not in SCHEMA_REPAIR_TOTAL.label_names
