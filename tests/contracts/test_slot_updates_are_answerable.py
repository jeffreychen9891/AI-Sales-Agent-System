"""T34: the understanding step can actually produce a slot update.

`slot_updates` has been in the contract since the beginning and came back `[]`
from every real provider call. The cause was not the model: the UNDERSTAND_VALUE
binding runs under a strict `json_schema` response format, and the contract
requires `frame_id` and `evidence_event_id` -- two uuids the model is never
shown. Under a strict schema the only compliant answer to "give me a uuid you
have never seen" is to send no items at all.

Downstream that meant `_missing_required_slots` never emptied, so the `complete`
exit gate was unreachable outside fixtures, and with it the closing frame
(ADR-0031) and RFC §4.3's provenance rule (ADR-0032). Every existing test
reached `complete` by seeding slots directly, which is why nothing went red.

The fix keeps the stored document exactly as RFC `:325` specifies and changes
only who fills which half: the model supplies `slot` / `value` / `confidence`,
the platform supplies the two identities it already holds -- the same division
already applied to `event_id` and `model_evidence`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.runtime.contracts import (
    load_inference_schema,
    model_output_schema,
    validate_inference_contract,
)
from fastagent_v2.worker.inference_model import PROMPT_RELEASE, ModelBackedInference
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import ChatCompletion
from fastagent_v2.worker.target_stack import apply_slot_updates

ROOT = Path(__file__).resolve().parents[2]
FRAME_ID = UUID("f1111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("e2222222-2222-4222-8222-222222222222")


def _profile() -> dict[str, Any]:
    from fastagent_v2.profiles.registry import ProfileRegistry

    return ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    ).document


def _aggregate(profile: dict[str, Any]) -> dict[str, Any]:
    definition = profile["target_frames"][0]
    return {
        "target_stack": [
            {
                "id": str(FRAME_ID),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
                "slot_evidence": {},
                "parent_frame_id": None,
                "frame_generation": 0,
                "entered_at": "2026-09-03T00:00:00Z",
                "last_progress_at": "2026-09-03T00:00:00Z",
                "interruption_reason": None,
                "resume_confirmation_policy": "required",
            }
        ]
    }


def _envelope() -> dict[str, Any]:
    return {
        "event_id": str(EVENT_ID),
        "event_type": "customer_message",
        "content": {"text": "I need help with my order"},
    }


class _CapturingClient:
    """Stands in for the provider, and records what it was asked.

    Returns only what a model is now asked for -- `slot`, `value`, `confidence`.
    If the platform stopped filling the identity fields, the document would fail
    its own contract here rather than silently reaching the database.
    """

    def __init__(self, slot_updates: list[dict[str, Any]]) -> None:
        self.slot_updates = slot_updates
        self.system_prompt: str = ""
        self.user_prompt: str = ""

    def complete(
        self, *, binding: Any, system_prompt: str, user_prompt: str, governor: Any, context: Any
    ) -> ChatCompletion:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return ChatCompletion(
            content={
                "facts": [],
                "intents": [{"intent_label": "ask_information", "confidence": 0.9}],
                "slot_updates": [dict(item) for item in self.slot_updates],
                "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
                "sentiment": "neutral",
                "risk_markers": ["none"],
                "customer_commitments": [],
                "uncertainties": [],
                "turn_summary": "用例夹具：这一轮的摘要。",
            },
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=0,
            latency_ms=7,
        )


def _understand(slot_updates: list[dict[str, Any]]) -> tuple[dict[str, Any], _CapturingClient]:
    profile = _profile()
    client = _CapturingClient(slot_updates)
    inference = ModelBackedInference(release=active_release(), client=client, governor=None)
    document = inference.understand(
        envelope=_envelope(), aggregate=_aggregate(profile), profile=profile
    )
    return document, client


def test_the_model_is_not_asked_for_uuids_it_has_never_seen() -> None:
    """The root cause, stated as an assertion.

    The stored contract keeps all five fields (RFC `:325`); what the model is
    asked for drops the two the platform already knows.
    """

    asked = model_output_schema("understanding-result")["properties"]["slot_updates"]["items"]
    stored = load_inference_schema("understanding-result")["properties"]["slot_updates"]["items"]

    assert set(asked["required"]) == {"slot", "value", "confidence"}
    assert "frame_id" not in asked["properties"]
    assert "evidence_event_id" not in asked["properties"]
    # The contract itself is unchanged -- this is a division of labour, not a
    # relaxation of what gets stored.
    assert set(stored["required"]) == {
        "frame_id",
        "slot",
        "value",
        "confidence",
        "evidence_event_id",
    }


def test_the_platform_supplies_the_two_identities_and_the_document_validates() -> None:
    """A model answer of three fields becomes a five-field contract document."""

    document, _ = _understand(
        [{"slot": "request_summary", "value": "help with my order", "confidence": 0.9}]
    )
    validate_inference_contract("understanding-result", document)
    update = document["slot_updates"][0]
    assert update["frame_id"] == str(FRAME_ID)
    assert update["evidence_event_id"] == str(EVENT_ID)
    # Measured, not claimed: the frame is the one the model was asked about and
    # the event is the envelope being processed.
    assert update["slot"] == "request_summary"


def test_the_model_is_told_which_slots_the_goal_wants() -> None:
    """Without the vocabulary the model can only invent names, and
    `apply_slot_updates` drops every name the definition does not declare -- so
    `slot_updates` was unfillable in practice even before the uuids made it
    unfillable in principle."""

    _, client = _understand([])
    sent = json.loads(client.user_prompt)
    assert [slot["name"] for slot in sent["slots_wanted"]] == ["request_summary"]
    assert sent["slots_wanted"][0]["required"] is True
    assert "slots_wanted" in client.system_prompt


def test_a_slot_update_from_the_model_actually_lands_on_the_frame() -> None:
    """The payoff, end to end through the real writeback.

    This is the assertion that was impossible before: a model-produced update
    reaching `frame["slots"]`, which is what `_missing_required_slots` reads and
    therefore what makes the `complete` exit gate reachable outside fixtures.
    """

    profile = _profile()
    aggregate = _aggregate(profile)
    client = _CapturingClient(
        [{"slot": "request_summary", "value": "help with my order", "confidence": 0.9}]
    )
    inference = ModelBackedInference(release=active_release(), client=client, governor=None)
    document = inference.understand(
        envelope=_envelope(), aggregate=aggregate, profile=profile
    )

    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=list(document["slot_updates"]),
        recorded_at="2026-09-03T00:00:00Z",
    )
    frame = writeback.aggregate["target_stack"][-1]
    assert writeback.applied == 1
    assert frame["slots"]["request_summary"] == "help with my order"
    # Provenance survives too (T25), and its evidence id is the platform's.
    assert frame["slot_evidence"]["request_summary"]["evidence_event_ids"] == [str(EVENT_ID)]


def test_prompt_release_moved_with_the_prompt() -> None:
    """A prompt change that does not move `prompt_release` makes every stored
    `model_evidence` point at the wrong prompt."""

    golden = json.loads(
        (ROOT / "packages/contracts/inference/golden/v4-replay-cases.json").read_text(
            encoding="utf-8"
        )
    )
    assert PROMPT_RELEASE == "m1a-prompt-v17"
    assert golden["prompt_release"] == PROMPT_RELEASE


def test_a_bad_slot_name_is_stopped_by_whichever_layer_owns_it() -> None:
    """Telling the model the vocabulary is not the same as trusting it.

    Two different names fail in two different places, and the distinction is
    worth pinning because it says which layer owns which failure:

    * a **well-formed but undeclared** name passes the contract (the schema has
      no idea what this profile declares) and is dropped by `apply_slot_updates`;
    * an **empty** name never gets that far -- `minLength: 1` rejects the whole
      document at the model boundary, so the writeback is never reached.

    The first version of this test asserted both went through the writeback and
    was wrong about the second: it fails earlier, and better.
    """

    from fastagent_v2.worker.providers import ProviderMalformedOutput

    profile = _profile()
    aggregate = _aggregate(profile)

    # Undeclared but well-formed: contract accepts, writeback refuses.
    document, _ = _understand([{"slot": "invented_slot", "value": "x", "confidence": 0.9}])
    validate_inference_contract("understanding-result", document)
    writeback = apply_slot_updates(
        aggregate=aggregate,
        profile=profile,
        slot_updates=list(document["slot_updates"]),
        recorded_at="2026-09-03T00:00:00Z",
    )
    assert writeback.applied == 0
    assert writeback.aggregate["target_stack"][-1]["slots"] == {}

    # Empty: rejected at the boundary, so nothing downstream ever sees it.
    with pytest.raises(ProviderMalformedOutput):
        _understand([{"slot": "", "value": "x", "confidence": 0.9}])
