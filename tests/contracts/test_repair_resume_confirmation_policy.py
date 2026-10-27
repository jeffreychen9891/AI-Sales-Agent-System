"""N1: `repair` reads the `resume_confirmation_policy` it used to ignore.

`request_pop` has always honoured the field; `repair` never looked at it. So a
child frame whose profile says `forbidden` still drove the parent into
`resume_confirmation_pending` -- **the one state whose entire purpose is to send
the message the profile forbids**. Read and write disagreed about the same
field, and the write side won.

M1a sends nothing, so today this is only a state bit. That is precisely why it
is worth closing now: the moment M1b can send `confirm_resume`, the bit becomes
a message the merchant explicitly said not to send.

**Every state here is built by the production transitions**, never hand-written
-- see `return_path_builders`, which holds the construction shared with
`test_exit_signal_refuses_resume` (T53). `resume_confirmation_policy` reaches a
frame exactly one way -- `push` copies it from
`definition["return_confirmation"]["policy"]` -- so the profile is what these
tests vary.

Every case is asserted **twice**, once against the transition and once against
the plan validator. The validator runs first: a rule living in only one of them
is either unreachable or raises through the retry budget instead of landing on
the no-send path with a reason code.
"""

from __future__ import annotations

import json

import pytest
from fastagent_v2.worker.inference_validation import PlanRejected
from fastagent_v2.worker.target_stack import TargetStackTransitionError
from return_path_builders import (
    REPO_ROOT,
    ROOT,
    apply_operation,
    interrupted_conversation,
    validate_operation,
)


def test_the_forbidden_policy_is_reachable_by_configuration() -> None:
    """Why this rule is not dead code, stated as a check rather than a claim.

    No profile in `profiles/` declares `forbidden` today, so nothing in the
    repository exercises it -- and "unreachable" is exactly the verdict the
    2026-09-03 review reached about this batch's sibling. The difference is
    where the limit lives: `forbidden` is absent from today's *data*, while the
    contract governing every merchant profile enumerates it. Any merchant who
    sets it gets this behaviour with no code change.
    """

    schema = json.loads(
        (REPO_ROOT / "packages/contracts/schemas/merchant-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )
    frame = schema["properties"]["target_frames"]["items"]["properties"]
    assert "forbidden" in frame["return_confirmation"]["properties"]["policy"]["enum"]


def test_repair_refuses_when_the_profile_forbids_resume_confirmation() -> None:
    """N1 itself.

    `resume_confirmation_pending` exists to ask "shall we go back?", so entering
    it for a `forbidden` child is proposing the one message the profile forbids.
    """

    aggregate, profile = interrupted_conversation(child_policy="forbidden")

    with pytest.raises(TargetStackTransitionError, match="forbids resume confirmation"):
        apply_operation(aggregate, "repair", profile=profile)
    with pytest.raises(PlanRejected) as rejected:
        validate_operation(aggregate, "repair", profile=profile)
    assert rejected.value.reason_code == "RESUME_CONFIRMATION_FORBIDDEN"


def test_a_forbidden_child_can_still_be_popped_directly() -> None:
    """Refusing the confirmation must not strand the child on the stack.

    `forbidden` means "return without asking", not "never return". `request_pop`
    has only ever gated on `required`, so the direct route stays open -- this is
    the check that the refusal above did not turn into a trap.
    """

    aggregate, profile = interrupted_conversation(child_policy="forbidden")

    updated = apply_operation(aggregate, "request_pop", profile=profile)
    assert len(updated["target_stack"]) == 1
    assert updated["target_stack"][-1]["definition_id"] == ROOT
    assert updated["target_stack"][-1]["status"] == "active"
    validate_operation(aggregate, "request_pop", profile=profile)


@pytest.mark.parametrize("policy", ["required", "optional"])
def test_repair_still_works_for_the_case_it_was_written_for(policy: str) -> None:
    """A fix that quietly disabled `repair` would pass every assertion above.

    `required` and `optional` are the two policies the shipped profiles actually
    use, and the RFC section 4.2 `:311` row still needs its writer.
    """

    aggregate, profile = interrupted_conversation(child_policy=policy)

    updated = apply_operation(aggregate, "repair", profile=profile)
    assert updated["target_stack"][-2]["status"] == "resume_confirmation_pending"
    validate_operation(aggregate, "repair", profile=profile)


@pytest.mark.parametrize("op", ["repair", "request_pop"])
@pytest.mark.parametrize("policy", ["required", "optional", "forbidden"])
def test_the_validator_and_the_transition_never_disagree(policy: str, op: str) -> None:
    """N1 was a read/write asymmetry, so asymmetry is what this watches for.

    The validator runs first. If it accepts what the transition refuses, the
    refusal surfaces as an exception through the retry budget instead of a
    no-send with a reason code; if it refuses what the transition accepts, a
    legal plan is thrown away. The pair is what matters, not either one alone.
    """

    aggregate, profile = interrupted_conversation(child_policy=policy)

    transition_refused = False
    try:
        apply_operation(aggregate, op, profile=profile)
    except TargetStackTransitionError:
        transition_refused = True

    validator_refused = False
    try:
        validate_operation(aggregate, op, profile=profile)
    except PlanRejected:
        validator_refused = True

    assert transition_refused == validator_refused, (
        f"{op} on a {policy!r} child: transition refused={transition_refused}, "
        f"validator refused={validator_refused}"
    )
