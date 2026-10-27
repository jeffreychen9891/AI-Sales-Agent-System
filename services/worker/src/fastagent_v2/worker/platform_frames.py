"""The platform's built-in target frames. **ADR-0099 (Q20), landing in B4-7.**

A merchant profile declares the frames its goals live in. This module declares
the one frame **the platform owns**: the frame a conversation moves into when
there is no goal left to pursue.

**Why it exists.** ADR-0031 gave the terminal turn a legal intent owner by
having the profile *declare* a closing frame, and kept a hard boundary for the
profiles that declare none: hand off to a person rather than send a message with
no owner. T8 measured what that boundary costs once `complete` is reachable --
**every successfully finished conversation produces a manual task**, 0 to 100%.
ADR-0099 takes the third road: the platform supplies the owner, so "the profile
did not declare a closing frame" stops being a state a person has to resolve.

**What it is not.** It is not candidate ② from ADR-0031 (`target_frame_id`
relaxed to nullable). Attribution stays mandatory -- `intent_records.
target_frame_id` is `NOT NULL` in `001` and this batch does not touch it. What
changes is *who supplies the frame*, not whether one is required.

**Reserved namespace.** Profile frame ids beginning with `PLATFORM_FRAME_ID_
PREFIX` are refused at admission (`profiles.registry.load_package`). Without
that, a profile could declare a frame that shadows this one and quietly redefine
what the platform promises -- `frame_definitions` below resolves the platform
entry last, so a shadowing id would win.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastagent_v2.runtime.contracts import PLATFORM_FRAME_ID_PREFIX, is_platform_frame
from fastagent_v2.worker.material_selection import declarations

#: Re-exported so that "which ids are the platform's" reads as one import for
#: everything on the worker side, while the rule itself stays where both
#: services can reach it.
__all__ = [
    "NO_GOAL_FRAME_DEFINITION",
    "NO_GOAL_FRAME_DEFINITION_ID",
    "NO_GOAL_FRAME_STRATEGIES",
    "PLATFORM_FRAME_DEFINITIONS",
    "PLATFORM_FRAME_ID_PREFIX",
    "PLATFORM_STRATEGY_REGISTRY",
    "frame_definitions",
    "is_platform_frame",
    "strategy_registration",
]

#: Reserved prefix for platform-owned frame definition ids. A merchant profile
#: may not declare one; admission refuses it.
#:
#: **Defined in `runtime.contracts` and re-exported here**, because the rule has
#: two enforcers in two services: the api refuses a profile that declares such
#: an id, the worker resolves them. The api is deployed without the worker
#: package on its path, so the shared half cannot live in this module -- which
#: is what `test_the_registry_trusts_the_deployment_it_runs_in...` measured the
#: moment the registry imported from here.

#: The frame that owns a customer-visible turn belonging to no goal.
NO_GOAL_FRAME_DEFINITION_ID = f"{PLATFORM_FRAME_ID_PREFIX}no_goal"

#: **ADR-0099 §五.1**: the built-in frame's `allowed_strategy_set` "at least has
#: to hold `acknowledge` / `no_message`". It holds exactly those two.
#:
#: The floor is also the ceiling on purpose. Every other play in the catalog
#: pursues something -- `answer_question`, `advance_goal`, `ask_slot`, `close_
#: goal` -- and this frame is where a conversation sits when there is nothing to
#: pursue. Widening it is a product decision about what the machine may do after
#: the goal is met, not an implementation detail, so it is left to be made
#: rather than assumed here.
#:
#: **`handoff` is deliberately absent and still reachable**: it is a
#: `SAFE_FALLBACK_STRATEGIES` member, and `origin == "safe_fallback"` plans skip
#: the frame allowlist entirely (`inference_validation.validate_plan`). The road
#: to a person is not closed by keeping this list short.
NO_GOAL_FRAME_STRATEGIES: tuple[str, ...] = ("acknowledge", "no_message")

#: The definition itself, shaped exactly like a `merchant-profile` frame so that
#: every consumer that reads a frame definition reads this one the same way.
#:
#: * **no `information_slots`** -- a frame with no goal asks for nothing, and
#:   this is also what makes `complete` meaningless on it: the exit gate for
#:   `complete` is "are the required slots filled", which is vacuously true for
#:   an empty list. That is why the refusal is explicit in both layers rather
#:   than left to the slot check (see `is_platform_frame` callers);
#: * **no `time_rules`** -- it arms no timers. A conversation that has finished
#:   should not acquire new proactive reasons to speak by finishing;
#: * **`return_confirmation.policy = "forbidden"`** -- `resume_confirmation_
#:   policy` is copied from here onto the stack frame, and the confirmation it
#:   governs is the question "shall we go back to what we were doing?". There is
#:   nothing to go back to, so the question must never be asked.
NO_GOAL_FRAME_DEFINITION: Mapping[str, Any] = {
    "id": NO_GOAL_FRAME_DEFINITION_ID,
    "goal": "对话已无进行中的目标：为收尾、问候、告别与离题消息提供归属",
    "exit_conditions": [],
    "information_slots": [],
    "allowed_strategy_set": list(NO_GOAL_FRAME_STRATEGIES),
    "content_materials": [],
    "time_rules": [],
    "return_confirmation": {"policy": "forbidden"},
}

#: Every platform-owned definition, keyed the way profiles key theirs.
PLATFORM_FRAME_DEFINITIONS: tuple[Mapping[str, Any], ...] = (NO_GOAL_FRAME_DEFINITION,)


#: What the platform's own strategies are registered to do, in the shape
#: `merchant-profile`'s `strategies` map uses.
#:
#: **A platform frame cannot borrow the profile's registry**, which is the whole
#: reason this exists. `validate_plan` requires `strategy in
#: profile["strategies"]`, and `inference` reads that entry's
#: `generation_constraints` -- so under a profile that never registered
#: `acknowledge` (`profiles/synthetic-third` registers no `no_message` at all),
#: the model's only legal play inside the no-goal frame would be refused and the
#: turn would fall to a safe fallback. That is "the profile has to remember to
#: declare it" arriving through a second door, and ADR-0099 §二 closes that door:
#: **每个租户都有，不需要记得声明**.
#:
#: The values are the conservative ones the shipped profiles use for the same
#: two plays: an acknowledgement may send one message and may promise nothing;
#: `no_message` may do neither.
PLATFORM_STRATEGY_REGISTRY: Mapping[str, Mapping[str, Any]] = {
    "acknowledge": {
        "required_slots": [],
        "allowed_intent_kinds": ["send_message"],
        "generation_constraints": ["no_new_commitment"],
    },
    "no_message": {
        "required_slots": [],
        "allowed_intent_kinds": [],
        "generation_constraints": [],
    },
}


def strategy_registration(
    profile: Mapping[str, Any], definition_id: Any, strategy: Any
) -> dict[str, Any] | None:
    """What this strategy is registered to do in the frame it was selected in.

    Returns `None` when nothing registers it, and **both callers treat that as
    a refusal to act rather than as an error** (review P3-3, correcting this
    docstring): the validator raises `STRATEGY_NOT_IN_PROFILE_REGISTRY`, and the
    inference double falls back to empty constraints and intent kinds, which
    makes the turn produce nothing rather than raise. The earlier wording called
    the second one a "fault", which it is not -- before B4-7 that path was a
    `KeyError`, and the double deliberately no longer has one.

    **The frame decides which registry answers.** Inside a platform frame the
    platform's registry does, because the frame is the platform's; everywhere
    else the profile's does, unchanged. The two are not merged: a profile must
    not be able to widen what `acknowledge` may do *inside the platform's frame*
    by registering a laxer version of it, and equally the platform's entry must
    not leak into a merchant frame where the profile's own registration is the
    contract.
    """

    name = str(strategy)
    if is_platform_frame(definition_id):
        registered = PLATFORM_STRATEGY_REGISTRY.get(name)
        return dict(registered) if registered is not None else None
    registered = (profile.get("strategies") or {}).get(name)
    return dict(registered) if registered is not None else None


def frame_definitions(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Every frame definition this conversation may be standing in, by id.

    **The one place the merge happens.** The alternative -- appending the
    platform frames to `profile["target_frames"]` at load time -- was rejected
    for two reasons: the profile document is content-addressed
    (`profiles.registry.canonical_hash` is checked against `identity.content_
    hash` at admission and again on read in `profiles.resolver`), so a mutated
    copy is a document that hashes to something nobody published; and it would
    have to be re-applied at every load site, which is a rule a future load site
    can forget. Resolving through this function cannot be forgotten by anyone
    who needs a definition, because getting a definition is what it is for.

    Platform entries are written **after** the profile's, so this function alone
    decides the winner if an id ever collides. Admission refuses the collision
    (`PLATFORM_FRAME_ID_PREFIX`), and the order here is the second lock: a
    profile published before that guard existed still cannot shadow a platform
    frame.
    """

    definitions: dict[str, Any] = {
        str(item["id"]): dict(item) for item in profile.get("target_frames") or []
    }
    for definition in PLATFORM_FRAME_DEFINITIONS:
        definitions[str(definition["id"])] = dict(definition)
    return definitions


#: Where `profiles.registry.load_package` puts the words behind a frame's
#: `content_materials`. Spelled here as well as there because the worker does not
#: import from `services/api`; the two spellings are pinned equal by
#: `test_the_material_texts_key_is_spelled_once_per_package`.
MATERIAL_TEXTS_KEY = "content_material_texts"


def materials_for_frame(
    profile: Mapping[str, Any], definition: Mapping[str, Any]
) -> dict[str, str]:
    """The materials this frame may use, ref -> text, in the frame's declared order.

    **Two statements have to agree before a material is in play**: the frame
    declares the ref (`content_materials`), and the package supplies its words
    (`materials.json`, merged onto the profile document at admission). This
    returns the intersection, so neither statement alone can put words in front
    of the model.

    **Empty is the normal answer today.** Every profile currently published ships
    no materials file, so every frame resolves to `{}` and the generation step is
    told there are no materials -- exactly the behaviour that existed before this
    function. That is deliberate: the mechanism is adopted one package at a time,
    and `load_package` refuses a package that adopts it halfway.

    **Order is the profile author's**, not the mapping's. `content_materials` is
    a list, and the sequence a merchant wrote their materials in is a statement
    about which one to lead with; rebuilding it from the JSON object's key order
    would substitute the editor's typing order for the author's intent.

    **This is every material the frame declares, not the ones this turn may
    quote.** `material_selection.select_materials` answers the second question,
    and the two are deliberately separate: what a turn is *allowed* to say is
    decided once, up front, while resolving a ref that already passed that check
    into its words is all the send path has left to do.
    """

    texts = profile.get(MATERIAL_TEXTS_KEY)
    if not isinstance(texts, Mapping):
        return {}
    resolved: dict[str, str] = {}
    for declaration in declarations(definition):
        key = str(declaration["ref"])
        text = texts.get(key)
        # A declared ref with no text is refused at admission, so reaching this
        # branch means a release published before its package supplied the
        # words. Skipping is right for that case and stays right afterwards: the
        # model is told about the materials that exist, never about a name whose
        # words it would then have to supply itself.
        if isinstance(text, str) and text.strip():
            resolved[key] = text
    return resolved


#: **`frame_definition()` was deleted here** (independent review 2026-09-10,
#: P2-2). It was written as the single-lookup companion to `frame_definitions`,
#: with a docstring saying "callers raise their own layer's error" -- and it had
#: no callers in production, tests or scripts. Each layer already has its own:
#: `target_stack._definition` raises `TargetStackTransitionError`,
#: `inference_validation.frame_definition` raises `PlanRejected`. A third
#: spelling that raises nothing was a function waiting for a use rather than one
#: answering a need, which `AGENTS.md`'s zero-caller check calls dead code.
