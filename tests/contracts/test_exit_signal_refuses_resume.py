"""T53: an exit signal refuses the return instead of scheduling one.

RFC `conversation-engine.md:317`: "客户说「不用了 / 别再问 / 转人工」等退出信号时
ProactivityPolicy **拒绝回归**". The implementation had it the other way round.
A child frame the customer had just ended could not leave the stack until the
parent had been walked into `resume_confirmation_pending` -- the state whose
entire purpose is to ask "shall we go back to what we were doing?". The customer
says "don't ask me again", and the machine prepares to ask.

Two paths, one mistake -- treating the confirmation gate as a compulsory stop on
every road back:

* `repair` was legal for an abandoned child, and legal on the very turn the exit
  signal arrived;
* `request_pop` demanded the confirmation for an `abandoned` child exactly as
  for a `completed` one (backlog item 3: the two were equivalent at that gate).

The withdrawn first version (T1) keyed on `interruption_reason`, which `push`
alone writes and which the validator forbids `push` from ever setting to an exit
relation -- an unreachable rule whose tests were green against a hand-built
state (independent review 2026-09-03, P0-1). So every state here is built by the
production transitions, and
`test_the_exit_signal_reaches_the_stack_through_the_real_transitions` makes the
reachability claim a check rather than a promise.

Every case is asserted **twice**, once against the transition and once against
the plan validator. The validator runs first: a rule living in only one of them
is either unreachable or raises through the retry budget instead of landing on
the no-send path with a reason code.

M1a sends nothing, so all of this is state bits today -- and that `confirm_resume`
becomes sendable in M1b is exactly why the bits are worth fixing now. The other
half of RFC `:317`, refusing to *re-engage* proactively, belongs to the
ProactivityPolicy that M1b introduces; what the state machine owes it is not to
have queued a confirmation up in advance.
"""

from __future__ import annotations

import json

import pytest
from fastagent_v2.worker.inference_validation import PUSH_RELATIONS, PlanRejected
from fastagent_v2.worker.target_stack import (
    EXIT_SIGNAL_RELATIONS,
    TargetStackTransitionError,
    stack_requires_human_disposition,
)
from return_path_builders import (
    CHILD,
    CLOSING,
    REPO_ROOT,
    ROOT,
    apply_operation,
    interrupted_conversation,
    profile_with,
    pushed_interruption,
    validate_operation,
)

POLICIES = ["required", "optional", "forbidden"]

#: Every refusal the two layers can produce on the return path, paired: a
#: distinctive fragment of the transition's message, and the validator's reason
#: code for the same rule.
#:
#: The grid below used to assert only "did both layers refuse". 26 of its 48
#: cells were duplicate greens, and none of them could tell whether the two
#: layers refused for the *same* reason -- the asymmetry N1 was, one field read
#: on one side only, would have been caught, but "both refused, for different
#: reasons" would not (independent review 2026-09-04, P3-2). Pairing them costs
#: no extra cells and turns the duplicates into a check nobody had written.
#:
#: A refusal whose message matches no fragment fails the test on purpose: a new
#: rule has to be classified here, where its two halves are named together.
REFUSAL_PAIRS = {
    "after an exit signal": "REPAIR_AFTER_EXIT_SIGNAL",
    "forbids resume confirmation": "RESUME_CONFIRMATION_FORBIDDEN",
    "needs a confirmed return": "POP_BEFORE_RESUME_CONFIRMATION",
    "repair_pending parent": "REPAIR_ON_UNPREPARED_PARENT",
    "cannot remove child in status": "POP_BEFORE_CHILD_RESOLVED",
    "requires a parent frame": "STACK_OP_REQUIRES_PARENT",
}


def test_the_exit_relations_are_the_ones_the_contract_declares() -> None:
    """Who checks the hand-written list? This does.

    `EXIT_SIGNAL_RELATIONS` and `PUSH_RELATIONS` are two hand-written subsets of
    one enum that lives in the contract, and a relation belonging to neither is
    a relation nobody classified -- the shape of the deploy-gate defect of
    2026-09-03, where a hand-kept list quietly stopped covering the thing it was
    checking. Pinning both lists to the schema makes adding a seventh relation
    fail here, in the module that has to decide what it means, instead of
    passing by default.
    """

    schema = json.loads(
        (REPO_ROOT / "packages/contracts/inference/understanding-result.schema.json").read_text(
            encoding="utf-8"
        )
    )
    declared = set(schema["properties"]["interruption"]["properties"]["relation"]["enum"])
    assert EXIT_SIGNAL_RELATIONS <= declared
    assert set(PUSH_RELATIONS) <= declared
    assert EXIT_SIGNAL_RELATIONS.isdisjoint(PUSH_RELATIONS)
    # `same_goal` is the only relation that neither opens a child frame nor ends
    # one; every other member of the enum must be classified by one of the two.
    assert declared - EXIT_SIGNAL_RELATIONS - set(PUSH_RELATIONS) == {"same_goal"}


def test_the_exit_signal_reaches_the_stack_through_the_real_transitions() -> None:
    """The claim the withdrawn T1 could not make: production can build this state.

    `abandoned` has **two** writers -- `abandon`, and the `request_pop` this same
    batch added (T54) -- and both require an exit signal, which is why the
    implication "abandoned therefore the customer said no" still holds. The
    first version of this docstring said `abandon` was the only writer, which
    was false the moment T54 landed and was also the entire justification for
    the durable criterion (independent review 2026-09-04, P2-1). Uniqueness is
    now a check rather than a sentence: see
    `test_a_parent_abandoned_by_the_pop_is_still_read_as_an_exit_signal`.

    The negative half matters as much as the positive: the same `abandon` plan
    without an exit relation is refused, so the status cannot arrive from a
    customer who never asked to leave.
    """

    profile = profile_with(child_policy="required")
    interrupted = pushed_interruption(profile=profile)
    assert interrupted["target_stack"][-1]["status"] == "active"
    assert interrupted["target_stack"][-2]["status"] == "suspended"

    validate_operation(interrupted, "abandon", profile=profile, relation="exit")
    validate_operation(interrupted, "abandon", profile=profile, relation="human_request")
    with pytest.raises(PlanRejected) as rejected:
        validate_operation(interrupted, "abandon", profile=profile, relation="side_question")
    assert rejected.value.reason_code == "ABANDON_WITHOUT_EXIT_SIGNAL"

    abandoned = apply_operation(interrupted, "abandon", profile=profile, relation="exit")
    assert abandoned["target_stack"][-1]["status"] == "abandoned"
    assert abandoned["target_stack"][-2]["status"] == "repair_pending"


@pytest.mark.parametrize("policy", POLICIES)
def test_repair_refuses_a_return_after_the_child_was_abandoned(policy: str) -> None:
    """T53's first path, on the durable criterion.

    The relation on *this* turn is `same_goal` -- the customer's "不用了" was a
    turn ago and the aggregate is all that remembers it. That is the point: an
    `abandoned` child is the record of the exit signal, so the refusal has to
    outlive the turn that carried it.
    """

    aggregate, profile = interrupted_conversation(child_policy=policy, resolution="abandon")

    with pytest.raises(TargetStackTransitionError, match="after an exit signal"):
        apply_operation(aggregate, "repair", profile=profile, relation="same_goal")
    with pytest.raises(PlanRejected) as rejected:
        validate_operation(aggregate, "repair", profile=profile, relation="same_goal")
    assert rejected.value.reason_code == "REPAIR_AFTER_EXIT_SIGNAL"


@pytest.mark.parametrize("relation", sorted(EXIT_SIGNAL_RELATIONS))
def test_repair_refuses_a_return_on_the_turn_the_exit_signal_arrives(relation: str) -> None:
    """T53's second path: the child ended normally, the customer ends the rest.

    The side question was answered, so the child is `completed` and nothing in
    the aggregate says the customer wants out -- only this turn's relation does.
    Proposing `confirm_resume` here is RFC `:317` verbatim: the answer to "转人工"
    must not be "shall we return to the previous topic?".
    """

    aggregate, profile = interrupted_conversation(child_policy="required")

    with pytest.raises(TargetStackTransitionError, match="after an exit signal"):
        apply_operation(aggregate, "repair", profile=profile, relation=relation)
    with pytest.raises(PlanRejected) as rejected:
        validate_operation(aggregate, "repair", profile=profile, relation=relation)
    assert rejected.value.reason_code == "REPAIR_AFTER_EXIT_SIGNAL"


def test_an_abandoned_child_leaves_the_stack_without_a_confirmation() -> None:
    """Refusing `repair` must not lock the frame onto the stack.

    `required` + `abandoned` is the deadlock this fix would otherwise have
    created: the only producer of `resume_confirmation_pending` is `repair`, and
    `repair` is now refused here, so a gate demanding that state would never
    open again. Backlog item 3 asked for the same thing from the other side --
    `abandoned` and `completed` must stop being equivalent in front of the gate.
    """

    aggregate, profile = interrupted_conversation(child_policy="required", resolution="abandon")

    popped = apply_operation(aggregate, "request_pop", profile=profile, relation="same_goal")
    assert len(popped["target_stack"]) == 1
    assert popped["target_stack"][-1]["definition_id"] == ROOT
    validate_operation(aggregate, "request_pop", profile=profile, relation="same_goal")


@pytest.mark.parametrize(
    ("resolution", "relation"),
    [("abandon", "same_goal"), ("complete", "exit"), ("complete", "human_request")],
)
def test_the_parent_goal_is_not_handed_back_as_active_after_an_exit(
    resolution: str, relation: str
) -> None:
    """T54, ruled by the user on 2026-09-03 and done in this batch with T53.

    `request_pop` used to write `active` onto the parent unconditionally. Under
    M1b's active window `active` is a licence to advance the goal the customer
    just refused, which is the state table read backwards: RFC `:313` sends
    `repair_pending` + customer exit to abandon/complete the parent -> next
    goal/closed, and the mandatory scenario at `:751` names the end state
    outright -- "客户拒绝回归: exit signal deny，原目标 abandoned/suspended".

    Both parametrisations of the signal are covered: the child the customer
    ended, and the child that ended normally before the customer did.
    """

    aggregate, profile = interrupted_conversation(child_policy="required", resolution=resolution)

    popped = apply_operation(aggregate, "request_pop", profile=profile, relation=relation)
    assert [frame["status"] for frame in popped["target_stack"]] == ["abandoned"]
    # And the consequence that makes it more than a status string: a stack with
    # nothing workable left is the conversation going to a person, by the route
    # that already existed. `suspended` -- the other word at `:751` -- would not
    # do this; it is not terminal, so nothing would be reported while `keep`,
    # the safe fallback's own operation, would reject every later turn.
    assert stack_requires_human_disposition(popped) == "ROOT_TARGET_FRAME_ABANDONED"


def test_a_frame_abandoned_by_the_pop_is_still_read_as_an_exit_signal() -> None:
    """The second writer of `abandoned`, checked instead of asserted in prose.

    T54 made `request_pop` write `abandoned` onto the parent, so the durable
    criterion's justification -- "only `abandon` writes it, and `abandon`
    requires an exit relation" -- stopped being true inside this very batch
    (independent review 2026-09-04, P2-1). The implication survives only because
    the new writer also requires an exit signal; what follows checks that a
    frame *this batch* abandoned is read the same way as one the customer
    abandoned, on a later turn carrying no signal of its own.

    The review suggested probing with `repair`. That combination turns out to be
    unreachable, and the assertion below says so rather than leaving it as an
    unexplained omission: `repair` demands a `repair_pending` parent, and the
    frame under a pop-written `abandoned` one is always still `suspended` --
    only `complete`/`abandon` ever writes `repair_pending`, and they write it on
    the frame directly below the one they act on. So the reachable probe is the
    pop gate, which is where the criterion is actually read a second time.
    """

    profile = profile_with(child_policy="required")
    stack = pushed_interruption(profile=profile)
    stack = apply_operation(
        stack, "push", profile=profile, definition_id=CLOSING, relation="side_question"
    )
    stack = apply_operation(stack, "abandon", profile=profile, relation="exit")
    stack = apply_operation(stack, "request_pop", profile=profile, relation="exit")

    top = stack["target_stack"][-1]
    assert (top["definition_id"], top["status"]) == (CHILD, "abandoned")
    assert top["resume_confirmation_policy"] == "required"
    with pytest.raises(TargetStackTransitionError, match="repair_pending parent"):
        apply_operation(stack, "repair", profile=profile, relation="same_goal")

    # The reachable read: `required` would normally demand a confirmed return,
    # and this frame is exempt because the status it carries means the customer
    # already left -- on a turn that says nothing about leaving.
    popped = apply_operation(stack, "request_pop", profile=profile, relation="same_goal")
    validate_operation(stack, "request_pop", profile=profile, relation="same_goal")
    assert [frame["status"] for frame in popped["target_stack"]] == ["abandoned"]


def test_at_depth_three_the_exit_signal_reaches_only_the_immediate_parent() -> None:
    """What actually happens deeper than two frames -- including who is *not* told.

    The comment on this transition once said an exit signal makes "the stack"
    terminal. It does at depth 2. At depth 3 the grandparent is still
    `suspended`, which `stack_requires_human_disposition` counts as workable, so
    **nobody is notified**, `control_mode` stays `ai`, and the only legal plan is
    another `request_pop` -- the signal walks up one frame per turn.

    Pinned as it is rather than as it should be. Whether an exit should collapse
    the whole stack at once is a semantic question RFC `:751` does not settle
    ("原目标" names one frame), and this batch deliberately did not widen it
    (independent review 2026-09-04, P2-2; registered as backlog T74).
    """

    profile = profile_with(child_policy="required")
    stack = pushed_interruption(profile=profile)
    stack = apply_operation(
        stack, "push", profile=profile, definition_id=CLOSING, relation="side_question"
    )
    stack = apply_operation(stack, "abandon", profile=profile, relation="exit")

    popped = apply_operation(stack, "request_pop", profile=profile, relation="exit")

    assert [(frame["definition_id"], frame["status"]) for frame in popped["target_stack"]] == [
        (ROOT, "suspended"),
        (CHILD, "abandoned"),
    ]
    assert stack_requires_human_disposition(popped) is None, (
        "at depth 3 the exit signal does not reach a person on this turn -- "
        "the next request_pop is what abandons the root"
    )

    # And the next turn does finish the walk, with no new signal required.
    finished = apply_operation(popped, "request_pop", profile=profile, relation="same_goal")
    assert [frame["status"] for frame in finished["target_stack"]] == ["abandoned"]
    assert stack_requires_human_disposition(finished) == "ROOT_TARGET_FRAME_ABANDONED"


def test_a_completed_child_still_needs_the_confirmation_its_profile_requires() -> None:
    """The exemption is for the exit path only, not a deletion of the gate.

    Same policy, same parent status, one difference: the customer did not say
    no. The `required` gate is still in force, still with its own reason code.
    """

    aggregate, profile = interrupted_conversation(child_policy="required")

    with pytest.raises(TargetStackTransitionError, match="confirmed return"):
        apply_operation(aggregate, "request_pop", profile=profile, relation="same_goal")
    with pytest.raises(PlanRejected) as rejected:
        validate_operation(aggregate, "request_pop", profile=profile, relation="same_goal")
    assert rejected.value.reason_code == "POP_BEFORE_RESUME_CONFIRMATION"


@pytest.mark.parametrize("relation", ["same_goal", "side_question"])
def test_the_ordinary_return_still_works(relation: str) -> None:
    """A fix that disabled `repair` outright would pass every refusal above.

    The RFC `:311` row still needs its writer: an answered side question, no
    exit signal, a profile that asks before returning -- confirmation state,
    then pop, then the parent is active again.
    """

    aggregate, profile = interrupted_conversation(child_policy="required")

    repaired = apply_operation(aggregate, "repair", profile=profile, relation=relation)
    assert repaired["target_stack"][-2]["status"] == "resume_confirmation_pending"
    validate_operation(aggregate, "repair", profile=profile, relation=relation)

    popped = apply_operation(repaired, "request_pop", profile=profile, relation=relation)
    assert [frame["status"] for frame in popped["target_stack"]] == ["active"]
    assert stack_requires_human_disposition(popped) is None


@pytest.mark.parametrize("op", ["repair", "request_pop"])
@pytest.mark.parametrize("resolution", ["complete", "abandon"])
@pytest.mark.parametrize("relation", ["same_goal", "side_question", "exit", "human_request"])
@pytest.mark.parametrize("policy", POLICIES)
def test_the_validator_and_the_transition_never_disagree(
    policy: str, relation: str, resolution: str, op: str
) -> None:
    """Both places or neither, over the whole grid this batch touches.

    The validator runs first. If it accepts what the transition refuses, the
    refusal surfaces as an exception through the retry budget instead of a
    no-send with a reason code; if it refuses what the transition accepts, a
    legal plan is thrown away. N1 was exactly such an asymmetry -- one field read
    on one side only -- so the grid is the guard that this batch did not add a
    second one.
    """

    aggregate, profile = interrupted_conversation(child_policy=policy, resolution=resolution)
    where = f"{op} on a {resolution}d {policy!r} child, relation {relation!r}"

    transition_message: str | None = None
    try:
        apply_operation(aggregate, op, profile=profile, relation=relation)
    except TargetStackTransitionError as refusal:
        transition_message = str(refusal)

    validator_code: str | None = None
    try:
        validate_operation(aggregate, op, profile=profile, relation=relation)
    except PlanRejected as rejection:
        validator_code = rejection.reason_code

    assert (transition_message is None) == (validator_code is None), (
        f"{where}: transition refused={transition_message is not None}, "
        f"validator refused={validator_code is not None}"
    )
    if transition_message is None:
        return

    matched = [
        code for fragment, code in REFUSAL_PAIRS.items() if fragment in transition_message
    ]
    assert len(matched) == 1, (
        f"{where}: the transition's refusal {transition_message!r} matches "
        f"{len(matched)} entries in REFUSAL_PAIRS -- a new rule has to be "
        f"classified there, beside the reason code that is its other half"
    )
    assert validator_code == matched[0], (
        f"{where}: the two layers refused for different reasons -- transition "
        f"{transition_message!r}, validator {validator_code!r}"
    )
