"""The plan prompt tells the model which `generation_constraints` exist.

**Measured, not supposed.** The second real inbound window (2026-09-10,
`evidence/m1b/first-inbound-2026-09-10/round2/`) put seven events through the
pipeline and sent nothing: six plans were refused with
`GENERATION_CONSTRAINT_NOT_DECLARED` and one was the model legitimately choosing
to wait. `profiles/synthetic-third` declares `["one_question"]` for `ask_slot`;
the model returned `["ask_one_question", "no_emoji_only_reply"]` and
`["ask_for_request_summary", "no_assumptions_about_image"]`.

**`ask_one_question` against `one_question` is a near-miss, and a near-miss is
what guessing looks like.** The prompt required the field, showed `[]` in its
example, and never named a single legal value -- so the model had no vocabulary
and invented one.

What this file pins is the input, not the model: the prompt now carries the
closed set, and the validator that refused those six plans is **unchanged**.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_model import (
    _PLAN_SYSTEM,
    PROMPT_RELEASE,
    ModelBackedInference,
)

ROOT = Path(__file__).resolve().parents[2]


def _profile() -> dict[str, Any]:
    """The shipped profile whose vocabulary the real window tripped over."""

    return json.loads(
        (ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(encoding="utf-8")
    )


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


class _PromptCapturingClient:
    """Stands in for the chat client and keeps what the prompt actually said."""

    def __init__(self) -> None:
        self.user_prompts: list[dict[str, Any]] = []
        self.system_prompts: list[str] = []

    def complete(self, **kwargs: Any) -> Any:  # pragma: no cover - not reached
        raise AssertionError("this double captures the prompt; it never answers")


def _plan_prompt(profile: dict[str, Any], *, capture: _PromptCapturingClient) -> dict[str, Any]:
    """Drive `ModelBackedInference.plan` far enough to read the prompt it built.

    The call is intercepted at `_call`, which is the seam between "compose the
    prompt" and "talk to a provider" -- so this reads the real composition
    rather than a copy of it.
    """

    inference = ModelBackedInference.__new__(ModelBackedInference)
    inference._prompt_release = PROMPT_RELEASE  # type: ignore[attr-defined]

    def _call(*, role: str, system_prompt: str, user_prompt: str, context: Any) -> Any:
        capture.system_prompts.append(system_prompt)
        capture.user_prompts.append(json.loads(user_prompt))
        raise _Captured

    inference._call = _call  # type: ignore[attr-defined]
    understanding = {"interruption": {"relation": "same_goal"}}
    with pytest.raises(_Captured):
        inference.plan(
            understanding=understanding,
            aggregate=_aggregate(profile),
            profile=profile,
            context=None,
        )
    return capture.user_prompts[-1]


class _Captured(Exception):
    """Raised by the double once the prompt has been recorded."""


def test_the_plan_prompt_carries_the_declared_constraints_for_every_allowed_strategy() -> None:
    """The fix, stated over the shipped profile that the real window used."""

    profile = _profile()
    prompt = _plan_prompt(profile, capture=_PromptCapturingClient())

    assert "allowed_generation_constraints_by_strategy" in prompt
    offered = prompt["allowed_generation_constraints_by_strategy"]
    assert offered, "an empty map would tell the model nothing"

    for strategy, constraints in offered.items():
        assert constraints == profile["strategies"][strategy]["generation_constraints"], (
            "the prompt must offer exactly what the profile declares -- widening it here "
            "would make the declaration meaningless, which is the direction ADR-0123 rejects"
        )


def test_the_constraint_the_real_window_tripped_over_is_now_in_the_prompt() -> None:
    """`one_question` is what `synthetic-third` declares for `ask_slot`.

    The model guessed `ask_one_question` because nothing told it this name
    existed. Now something does.
    """

    profile = _profile()
    prompt = _plan_prompt(profile, capture=_PromptCapturingClient())
    offered = prompt["allowed_generation_constraints_by_strategy"]

    assert "ask_slot" in offered, "ask_slot is what the real window selected"
    assert offered["ask_slot"] == ["one_question"]
    assert "ask_one_question" not in offered["ask_slot"], (
        "the near-miss the model invented must not be legitimised by the prompt"
    )


def test_the_two_maps_offer_the_same_strategies() -> None:
    """The constraint map is shaped like the intent-kind map beside it.

    Same comprehension, same guard: a strategy the profile registers no entry
    for contributes no key to either, and the validator refuses such a plan on
    its own grounds rather than on a `KeyError` here.
    """

    prompt = _plan_prompt(_profile(), capture=_PromptCapturingClient())
    assert set(prompt["allowed_generation_constraints_by_strategy"]) == set(
        prompt["allowed_intent_kinds_by_strategy"]
    )


def test_the_system_prompt_forbids_inventing_and_rephrasing() -> None:
    """**Naming the vocabulary is not the whole fix.**

    A closed set in the payload with no instruction is an invitation to treat it
    as a suggestion. The instruction says both halves: choose from the list, and
    do not rephrase -- with the exact near-miss from the real window as the
    example, because that is the failure that happened rather than one imagined.
    """

    # Whitespace-insensitive, because the instruction is line-wrapped in the
    # constant and an assertion that breaks on rewrapping would be a test about
    # formatting rather than about content. (The first version of this case did
    # exactly that and went red on "An empty\n  list".)
    instruction = " ".join(_PLAN_SYSTEM.split())

    assert "allowed_generation_constraints_by_strategy" in instruction
    assert "Do not invent a name and do not rephrase one" in instruction
    assert '"ask_one_question" is not "one_question"' in instruction
    # **Not "always safe"** (independent review 2026-09-10, P1-1). The first
    # version of this instruction told the model an empty list was always the
    # safe answer. It is not: `_validate_constraints` executes exactly the list
    # the plan reports, so `[]` does not mean "be conservative", it means **run
    # none of the constraints the merchant declared** -- and the reviewer
    # measured a plan with `[]` passing validation and producing an
    # unconstrained customer-visible message. The refusal this batch is about
    # was fail-closed; that sentence pointed at a way through that is not.
    assert "An empty list is correct only when the strategy declares none" in instruction
    assert "always safe" not in instruction


def test_the_prompt_release_moved_with_this_change() -> None:
    """A prompt change that keeps its release string mislabels every stored
    `model_evidence`. Two other cases assert the golden moved with it."""

    assert PROMPT_RELEASE == "m1a-prompt-v17"
