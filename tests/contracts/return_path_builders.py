"""Builders for the tests about returning from an interruption.

Two test modules ask what the engine does on the way back from a child frame --
`test_repair_resume_confirmation_policy` (N1, the profile's `forbidden` policy)
and `test_exit_signal_refuses_resume` (T53, RFC `:317`). They need the same
state built the same way, and a previous review already flagged verbatim
duplication between contract modules as a defect in its own right
(independent review 2026-09-03, P3-5); the builders live here so the two
modules cannot drift into testing subtly different states.

**Every state is built by the production transitions**, never hand-written:
`_initial_aggregate` (the pipeline's own root builder) -> `push` -> `complete`
or `abandon`. The 2026-09-03 independent review of the T1/N1 batch found the
withdrawn T1's tests asserting against a state `push` can never produce, which
made a green suite prove nothing. Constructing through the real functions is
what stops that from recurring: if a state cannot be reached, the builder fails
here rather than the assertions passing over a fiction.
"""

from __future__ import annotations

import pathlib
from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastagent_v2.worker.inference_validation import validate_plan
from fastagent_v2.worker.pipeline import _initial_aggregate
from fastagent_v2.worker.target_stack import apply_target_stack_operation

OCCURRED_AT = "2026-09-03T09:00:00Z"
ROOT = "welcome"
CHILD = "clarify_request"
#: The third frame the shipped profile declares. Used only to build a depth-3
#: stack: `MAX_STACK_DEPTH` is 3 and `PUSH_ONTO_SAME_FRAME` forbids repeating a
#: definition, so a third frame is the only way to reach the depth where the
#: exit signal's reach stops being obvious (independent review 2026-09-04, P2-2).
CLOSING = "closing"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def profile_with(*, child_policy: str) -> dict[str, Any]:
    """The shipped profile, with the child frame's return policy varied.

    `validate_plan` reads far more of a profile than these tests care about
    (`time_rules`, strategies, slot declarations), so a convenient stub would
    have to grow a field every time the validator does. Varying the real package
    also keeps the mechanism honest: the policy has to travel
    profile -> `push` -> frame, which is the only path it has in production.
    """

    from fastagent_v2.profiles.registry import ProfileRegistry

    document = (
        ProfileRegistry(root=REPO_ROOT / "profiles", relative_to=REPO_ROOT)
        .package(profile_id="synthetic-service-desk", version="1.0.4")
        .document
    )
    varied = deepcopy(document)
    for definition in varied["target_frames"]:
        if definition["id"] == CHILD:
            definition["return_confirmation"]["policy"] = child_policy
    return varied


def apply_operation(
    aggregate: dict[str, Any],
    op: str,
    *,
    profile: dict[str, Any],
    definition_id: str | None = None,
    relation: str = "same_goal",
) -> dict[str, Any]:
    """Apply one operation through the production transition.

    The default relation matches `validate_operation`'s on purpose. They used to
    differ -- `side_question` here, `same_goal` there -- which was harmless
    because neither is an exit signal, and wrong in the way that matters for
    this module: its entire job is to ask **the same question of the same
    state** on both layers, and a pair of calls that omitted `relation` was
    quietly comparing two different worlds (independent review 2026-09-04,
    P3-3). `push` is the one operation that needs a relation of its own, and it
    now says so at its call site.
    """

    return apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": op, "target_frame_definition_id": definition_id},
        interruption_relation=relation,
        profile=profile,
        occurred_at=OCCURRED_AT,
    )


def pushed_interruption(*, profile: dict[str, Any]) -> dict[str, Any]:
    """Mid-interruption: the child frame active, the parent suspended.

    The root comes from the pipeline's own `_initial_aggregate` and the child
    from `push`, so this is the same stack a real conversation holds between the
    customer's side question and its resolution.
    """

    aggregate = _initial_aggregate(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile_release_id=uuid4(),
        profile=profile,
    )
    return apply_operation(
        aggregate, "push", profile=profile, definition_id=CHILD, relation="side_question"
    )


def interrupted_conversation(
    *, child_policy: str, resolution: str = "complete"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A side question that is over, with the parent waiting to be resumed.

    `resolution="complete"` is the question answered; `resolution="abandon"` is
    the customer ending it -- "不用了 / 别再问 / 转人工" -- which is the only way
    a frame ever reaches `abandoned` (the validator refuses `abandon` on any
    other relation, `ABANDON_WITHOUT_EXIT_SIGNAL`). Both leave the parent in
    `repair_pending`, the RFC §4.2 row at `:311`, and that shared shape is the
    point: the two resolutions differ by exactly one thing, whether the customer
    said no.
    """

    if resolution not in ("complete", "abandon"):
        raise ValueError(f"unsupported resolution {resolution!r}")
    profile = profile_with(child_policy=child_policy)
    aggregate = apply_operation(
        pushed_interruption(profile=profile),
        resolution,
        profile=profile,
        relation="exit" if resolution == "abandon" else "same_goal",
    )
    child = aggregate["target_stack"][-1]
    assert child["definition_id"] == CHILD
    assert child["status"] == ("abandoned" if resolution == "abandon" else "completed")
    assert child["resume_confirmation_policy"] == child_policy
    assert aggregate["target_stack"][-2]["status"] == "repair_pending"
    return aggregate, profile


def plan_for(op: str, *, profile: dict[str, Any]) -> dict[str, Any]:
    # The strategy has to be one the *child* frame allows, and the constraints
    # have to be that strategy's own -- the validator checks both. `repair` is
    # the state half of `confirm_resume`, so that is the strategy a real plan
    # would carry here; popping and abandoning just wait.
    strategy = "confirm_resume" if op == "repair" else "wait"
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "target_stack_operation": {"op": op, "target_frame_definition_id": None},
        "selected_strategy_id": strategy,
        "strategy_reason_code": "DETERMINISTIC_TEST",
        "confidence": {
            "raw_score": 1.0,
            "calibrated_score": None,
            "assessment_version": "test",
            "factor_codes": ["deterministic_output"],
        },
        "required_slot_requests": [],
        "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
        "candidate_intents": [],
        "generation_constraints": list(profile["strategies"][strategy]["generation_constraints"]),
        "manual_review_reason": None,
        "model_evidence": {
            "provider": "test",
            "model": "test",
            "prompt_release": "test",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def understanding_with(*, relation: str = "same_goal") -> dict[str, Any]:
    """A minimal document that satisfies the understanding contract.

    `validate_plan` validates it too, so a short stub is rejected before the
    target-stack rules are ever reached.
    """

    return {
        "schema_version": "1.0.0",
        "event_id": str(uuid4()),
        "facts": [],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": relation, "suggested_frame_definition_id": None},
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


def validate_operation(
    aggregate: dict[str, Any],
    op: str,
    *,
    profile: dict[str, Any],
    relation: str = "same_goal",
) -> None:
    validate_plan(
        plan=plan_for(op, profile=profile),
        understanding=understanding_with(relation=relation),
        aggregate=deepcopy(aggregate),
        profile=profile,
    )
