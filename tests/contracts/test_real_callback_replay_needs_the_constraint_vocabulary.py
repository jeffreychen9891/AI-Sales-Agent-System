"""Replay a real callback against a model that behaves the way the real one did.

**The failure this batch fixes was measured, so the case that proves the fix is
driven by the measurement rather than by an invented example.** The payload is
one of the eight captured on 2026-09-10 (second window,
`evidence/m1b/first-inbound-2026-09-10/round2/`), and the model double follows
the rule the real model demonstrably followed:

* **told which constraint names exist** -> use one of them;
* **not told** -> invent a plausible one, which is what a model asked for a
  field it has no vocabulary for actually does. The real model produced
  `ask_one_question` against a declared `one_question`.

So the case is a **conditional**: with this batch's prompt the plan validates,
without it the plan is refused as `GENERATION_CONSTRAINT_NOT_DECLARED` -- which
is the reverse probe the user asked for, and it is expressed here as a test
rather than left as a one-off script.

**What this is not**: it is not evidence about what a real model will do next
time. It is evidence that *the input the model was missing is now present*, and
that the platform's refusal still stands when it is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.ingress.wecom_proxy.inbound import parse_content
from fastagent_v2.worker.inference_model import ModelBackedInference
from fastagent_v2.worker.inference_validation import PlanRejected, validate_plan
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import ChatCompletion

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "wecom-proxy-round2"

#: The near-miss the real model returned when it had no vocabulary to draw on.
INVENTED = "ask_one_question"


def _profile() -> dict[str, Any]:
    return json.loads(
        (ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


def _captured_text() -> str:
    """A real customer message from the second window, read through the parser."""

    body = json.loads((FIXTURES / "001.json").read_text(encoding="utf-8"))
    content = parse_content(body["data"][0])
    assert content is not None and content.get("text"), "fixture must carry real text"
    return str(content["text"])


def _aggregate(profile: dict[str, Any]) -> dict[str, Any]:
    definition = profile["target_frames"][0]
    return {
        "generation": 1,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
                "slot_evidence": {},
                "entered_at": "2026-09-10T00:00:00Z",
                "last_progress_at": "2026-09-10T00:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": "optional",
                "frame_generation": 1,
            }
        ],
    }


class _ModelWithoutItsOwnVocabulary:
    """Answers `ask_slot`, and names its constraint from whatever it was told.

    The whole behavioural claim of this batch is that the second branch is what
    a model does when the first input is missing, so the double makes that
    branch explicit instead of hiding it behind a fixed answer.
    """

    def __init__(self) -> None:
        self.saw_vocabulary: bool | None = None

    def complete(
        self, *, binding: Any, system_prompt: str, user_prompt: str, governor: Any, context: Any
    ) -> ChatCompletion:
        prompt = json.loads(user_prompt)
        offered = prompt.get("allowed_generation_constraints_by_strategy") or {}
        constraints = list(offered.get("ask_slot") or [])
        self.saw_vocabulary = bool(constraints)
        return ChatCompletion(
            content={
                "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
                "selected_strategy_id": "ask_slot",
                "strategy_reason_code": "ASK_FOR_THE_MISSING_SLOT",
                "confidence": {
                    "raw_score": 0.8,
                    "calibrated_score": None,
                    "assessment_version": "model-v1",
                    "factor_codes": [],
                },
                "required_slot_requests": [],
                "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
                "candidate_intents": [],
                "generation_constraints": constraints[:1] or [INVENTED],
                "manual_review_reason": None,
            },
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=0,
            latency_ms=7,
        )


def _understanding(text: str) -> dict[str, Any]:  # noqa: ARG001 - see `facts` below
    return {
        "schema_version": "1.0.0",
        "event_id": str(uuid4()),
        # Empty rather than a fabricated fact entry: `understanding-result`
        # pins the shape of a fact, and inventing one here would make the case
        # about this file's guess at that shape. The captured text's job is to
        # prove the fixture is a real message, which the case asserts directly.
        "facts": [],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "用例夹具：这一轮的摘要。",
        "model_evidence": {
            "provider": "test",
            "model": "test",
            "prompt_release": "test",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def _plan_for_the_real_message() -> tuple[dict[str, Any], _ModelWithoutItsOwnVocabulary]:
    profile = _profile()
    client = _ModelWithoutItsOwnVocabulary()
    inference = ModelBackedInference(release=active_release(), client=client, governor=None)
    plan = inference.plan(
        understanding=_understanding(_captured_text()),
        aggregate=_aggregate(profile),
        profile=profile,
        context=None,
    )
    return plan, client


def test_the_real_message_now_produces_a_plan_the_validator_accepts() -> None:
    """End of the chain the second window could not get through.

    With the vocabulary in the prompt the model names a declared constraint, and
    `validate_plan` -- unchanged by this batch -- accepts it.
    """

    profile = _profile()
    plan, client = _plan_for_the_real_message()

    assert client.saw_vocabulary is True, "the prompt must carry the closed set"
    assert plan["generation_constraints"] == ["one_question"]
    # The payload really is one of the captured messages, not a stand-in.
    assert _captured_text() == "你好吗[惊讶][憨笑][骷髅][敲打]"

    validated = validate_plan(
        plan=plan,
        understanding=_understanding(_captured_text()),
        aggregate=_aggregate(profile),
        profile=profile,
    )
    assert validated.document["selected_strategy_id"] == "ask_slot"


class _StrippingClient(_ModelWithoutItsOwnVocabulary):
    """The same model, with this batch's addition taken back out of the prompt.

    Stripping it here rather than editing the source is what makes the probe a
    *case*: it runs on every gate, beside the one that asserts the fix.
    """

    def complete(self, *, user_prompt: str, **kwargs: Any) -> ChatCompletion:
        prompt = json.loads(user_prompt)
        prompt.pop("allowed_generation_constraints_by_strategy", None)
        return super().complete(user_prompt=json.dumps(prompt), **kwargs)


def test_without_the_vocabulary_the_same_message_reproduces_the_refusal() -> None:
    """**The reverse probe, as a case.**

    Strip the constraint set back out of the prompt and replay the same captured
    message: the model invents a name again, and the validator refuses it with
    the exact reason code recorded on 2026-09-10. If this stops reproducing,
    either the prompt addition stopped being load-bearing or the refusal stopped
    being real -- and this batch claims both.
    """

    profile = _profile()
    client = _StrippingClient()
    inference = ModelBackedInference(release=active_release(), client=client, governor=None)
    plan = inference.plan(
        understanding=_understanding(_captured_text()),
        aggregate=_aggregate(profile),
        profile=profile,
        context=None,
    )

    assert client.saw_vocabulary is False
    assert plan["generation_constraints"] == [INVENTED]

    with pytest.raises(PlanRejected) as refused:
        validate_plan(
            plan=plan,
            understanding=_understanding(_captured_text()),
            aggregate=_aggregate(profile),
            profile=profile,
        )
    assert refused.value.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"
