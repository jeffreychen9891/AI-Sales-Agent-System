"""Characterization gate tying prompt changes to the replay golden."""

from __future__ import annotations

import json
from pathlib import Path

from fastagent_v2.runtime.contracts import model_output_schema
from fastagent_v2.worker.inference_model import (
    _GENERATE_SYSTEM,
    _PLAN_SYSTEM,
    _UNDERSTAND_SYSTEM,
    PROMPT_RELEASE,
)

ROOT = Path(__file__).resolve().parents[2]


def test_prompt_release_and_v4_replay_golden_move_together() -> None:
    golden = json.loads(
        (ROOT / "packages/contracts/inference/golden/v4-replay-cases.json").read_text(
            encoding="utf-8"
        )
    )
    assert golden["prompt_release"] == PROMPT_RELEASE
    assert len(golden["cases"]) == 6
    assert {item["event_type"] for item in golden["cases"]} == {
        "customer_message",
        "timer",
    }


def test_prompts_characterize_the_nested_contract_shapes() -> None:
    # 2026-09-24（缺陷 22）：`evidence_span_hash` 从契约与提示词一并删掉。
    for field in ("typed_value", "suggested_frame_definition_id"):
        assert field in _UNDERSTAND_SYSTEM
    for field in ("target_stack_operation", "scheduled_event_changes", "factor_codes"):
        assert field in _PLAN_SYSTEM
    for field in ("content_or_ref", "return_confirmation", "safety_markers"):
        assert field in _GENERATE_SYSTEM
    assert "price_pressure" in _UNDERSTAND_SYSTEM
    assert "first response character must be {" in _UNDERSTAND_SYSTEM
    # 2026-09-24（缺陷 22）：那句话的断行随删字段挪了位置 —— 判内容不判断行。
    assert "item contains exactly field and reason" in _UNDERSTAND_SYSTEM
    # T34. The prompt must keep describing what a slot update is and where
    # slot names come from. Before this, `slot_updates` was listed as required
    # and never described, and the only example showed `[]` -- which is exactly
    # what every real provider call returned.
    assert "slots_wanted" in _UNDERSTAND_SYSTEM
    assert "{slot, value, confidence}" in _UNDERSTAND_SYSTEM
    assert '"slot_updates": [{"slot"' in _UNDERSTAND_SYSTEM
    assert "evidence_complete" in _PLAN_SYSTEM
    assert "evidence_complete" in _GENERATE_SYSTEM


def test_responses_schema_excludes_platform_owned_identity_and_inlines_refs() -> None:
    schema = model_output_schema("plan-result")
    assert not {"schema_version", "origin", "model_evidence"} & set(schema["properties"])
    confidence = schema["properties"]["confidence"]
    assert "$ref" not in confidence
    assert "factor_codes" in confidence["properties"]
