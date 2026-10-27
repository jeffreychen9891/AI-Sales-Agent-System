"""The generation step can see the merchant's materials, and can only quote them.

**What this file is about.** `ModelBackedInference.generate` took a `profile`
argument and never read it. The step that writes every customer-visible sentence
received the strategy name, a reason code, constraint names, candidate intents,
pending slots, the understanding and the event type -- and not one word the
merchant wrote. So「素材只能由 profile 声明、经受信 registry 引用，模型不得现场
编造」described nothing at all: there was no material in front of the model to
not-invent from, and every sentence it produced was its own.

Three properties are asserted here, and they are not the same property:

1. **The materials arrive.** The frame's `content_materials`, ref and text
   together, are in the prompt. `test_removing_the_prompt_materials_makes_this
   _file_red` is the reverse probe: it names what has to go red if the change is
   backed out.
2. **A quote is the merchant's string.** A `material_ref` segment is resolved
   from the same mapping, so what reaches the customer is the merchant's bytes
   whatever wording the model had in mind.
3. **A ref this frame does not declare stops the turn**, at the validator and
   again at the channel.

**What is not asserted, because it is not true.** A model that ignores the refs
and paraphrases a material into a plain text segment is **not detected** --
see `test_a_paraphrased_material_is_not_detected_today` for the measurement and
T268 for the registration. Quoting is *prevented from being a rewrite*; writing
your own prose that resembles a material is not distinguishable from writing
your own prose.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.channel import MATERIAL_REF_SEGMENT_TYPE, customer_text
from fastagent_v2.worker.inference_model import ModelBackedInference
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
)
from fastagent_v2.worker.material_selection import select_materials
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.platform_frames import MATERIAL_TEXTS_KEY, materials_for_frame
from fastagent_v2.worker.providers import OpenAICompatibleChatClient, TransportResponse

ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_ENV = "FASTAGENT_MODEL_API_KEY_DEEPSEEK"

DEMO_MATERIALS: dict[str, str] = json.loads(
    (ROOT / "tests" / "fixtures" / "internal-demo-materials.json").read_text(encoding="utf-8")
)["materials"]

OPENING = "material:demo-opening:v1"
DONT_KNOW = "material:demo-dont-know:v1"
CLOSING_REF = "material:demo-traceable:v1"


class _RecordingTransport:
    """Test double. It never opens a socket."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append({"url": url, "payload": payload})
        return TransportResponse(status_code=200, body=self.body)


def _model_reply(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(content, ensure_ascii=False)}
                ],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _acknowledgement() -> dict[str, Any]:
    return {
        "strategy_id": "acknowledge",
        "customer_visible_segments": [{"type": "text", "content_or_ref": "收到。"}],
        "proposed_intents": [],
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "claims": [],
        "return_confirmation": {"included": False, "tone_material_ref": None},
        "safety_markers": ["none"],
    }


def _profile(*, frame_materials: dict[str, list[str]]) -> dict[str, Any]:
    """A profile carrying only what this file's questions need.

    Deliberately not a real package: `load_package` is what proves a package is
    well formed, and `tests/contracts/test_profile_materials_are_trusted.py`
    proves it there. What matters here is the shape the worker reads -- frames
    with `content_materials`, and the merged text key beside them.
    """

    used = {ref for refs in frame_materials.values() for ref in refs}
    return {
        "target_frames": [
            {
                "id": frame_id,
                "goal": f"goal of {frame_id}",
                "content_materials": list(refs),
                "allowed_strategy_set": ["acknowledge"],
                "information_slots": [],
                "exit_conditions": [],
                "time_rules": [],
            }
            for frame_id, refs in frame_materials.items()
        ],
        "strategies": {"acknowledge": {"allowed_intent_kinds": [], "generation_constraints": []}},
        MATERIAL_TEXTS_KEY: {ref: DEMO_MATERIALS[ref] for ref in used},
    }


def _aggregate(definition_id: str) -> dict[str, Any]:
    return {"target_stack": [{"id": "frame-1", "definition_id": definition_id, "slots": {}}]}


def _plan_document() -> dict[str, Any]:
    return {
        "selected_strategy_id": "acknowledge",
        "strategy_reason_code": "TEST",
        "generation_constraints": [],
        "candidate_intents": [],
        "required_slot_requests": [],
    }


def _offered(
    profile: dict[str, Any],
    definition_id: str,
    *,
    emit_kind: str | None = None,
    understanding: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The turn's selection, computed the way the pipeline computes it.

    Going through the real seam rather than handing the prompt a dict: a test
    that builds the selection itself proves the prompt formats what it is given,
    which was never in doubt.
    """

    definition = next(
        item for item in profile["target_frames"] if item["id"] == definition_id
    )
    return select_materials(
        definition=definition,
        declared=materials_for_frame(profile, definition),
        emit_kind=emit_kind,
        # ⚠️ **不再传 `frame`**（2026-09-18）：话术的 `on_request` 判据
        # 从「槽位现值」改成「这一轮问了」，`select_materials` 连这个参数都不收了。
        # 这个 helper 的 docstring 说它「computed the way the pipeline computes it」——
        # 管线不传，它就不能传，否则那句话就不成立了。
        understanding=understanding,
    )


def _generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: dict[str, Any],
    definition_id: str,
    emit_kind: str | None = None,
    understanding: dict[str, Any] | None = None,
    frame: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], _RecordingTransport]:
    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    transport = _RecordingTransport(_model_reply(_acknowledgement()))
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    )
    document = inference.generate(
        plan=_plan_document(),
        understanding=understanding
        or {"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        materials=_offered(
            profile,
            definition_id,
            emit_kind=emit_kind,
            understanding=understanding,
        ),
    )
    return document, transport


def _sent_prompt(transport: _RecordingTransport) -> dict[str, Any]:
    """The user prompt the provider received, parsed back into the object it is.

    **Not a substring search over the serialised request**, which is how this
    was first written and which was wrong in a way worth keeping written down:
    the prompt is itself a JSON document, so a material containing a newline
    appears in it as `\n`. A single-line material matched a raw containment
    check and a multi-line one did not -- a test that passes for short material
    and fails for long material is measuring the serialiser, not the prompt.
    Parsing asks the question that was meant: is this ref, with these exact
    words, in the object the model was handed.
    """

    assert transport.calls, "the provider was never called"
    return dict(json.loads(str(transport.calls[0]["payload"]["input"])))


def _sent_materials(transport: _RecordingTransport) -> dict[str, str]:
    prompt = _sent_prompt(transport)
    assert "content_materials" in prompt, (
        "the key must be present even when empty: 'this goal has no approved "
        "wording' and 'nobody mentioned materials' are different instructions"
    )
    return {item["ref"]: item["text"] for item in prompt["content_materials"]}


def _validated_plan(profile: dict[str, Any], definition_id: str) -> ValidatedPlan:
    definition = next(
        frame for frame in profile["target_frames"] if frame["id"] == definition_id
    )
    return ValidatedPlan(
        document={
            **_plan_document(),
            "origin": "model",
            "candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}],
        },
        frame={"id": "frame-1", "definition_id": definition_id},
        definition=definition,
    )


def _generation(segments: list[dict[str, Any]], intents: list[dict[str, Any]]) -> dict[str, Any]:
    """A generation result as `validate_generation` sees it -- platform stamps included.

    `_acknowledgement` is what the *model* returns; the three identity fields
    below are what `ModelBackedInference` writes onto it from measured facts.
    Validating the model's half alone would validate a document that never
    exists.
    """

    document = _acknowledgement()
    document["customer_visible_segments"] = segments
    document["proposed_intents"] = intents
    document["schema_version"] = "1.0.0"
    document["origin"] = "model"
    document["model_evidence"] = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "prompt_release": "m1a-prompt-v6",
        "latency_ms": 1,
        "token_usage": {"input": 11, "output": 7},
    }
    return document


# ---------------------------------------------------------------------------
# 1. The materials arrive
# ---------------------------------------------------------------------------


def test_the_prompt_carries_this_frames_materials_ref_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The measurement this batch exists to make true.**

    Both halves are asserted. The ref alone would be a name the model cannot
    read; the text alone would be words it cannot point at, and pointing is what
    makes a quote resolvable rather than a rewrite.
    """

    profile = _profile(frame_materials={"introduce": [OPENING, DONT_KNOW]})

    _, transport = _generate(monkeypatch, profile=profile, definition_id="introduce")

    offered = _sent_materials(transport)

    assert offered == {
        OPENING: DEMO_MATERIALS[OPENING],
        DONT_KNOW: DEMO_MATERIALS[DONT_KNOW],
    }


def test_removing_the_prompt_materials_makes_this_file_red() -> None:
    """**The reverse probe, named rather than described.**

    The probe is: delete the `content_materials` entry from the user prompt in
    `ModelBackedInference.generate`. **Measured 2026-09-10 -- 5 failed**, all of
    them in this file, and `tests/contracts` was otherwise unchanged (the same
    nine environment failures before and after). That second half is the
    finding: **nothing else in this repository can tell whether the generation
    step receives merchant material at all.** It is why this file exists rather
    than an assertion bolted onto a pipeline test.

    A second probe removed `materials=` from `pipeline`'s two `customer_text`
    call sites instead. **Every test in this file stayed green** -- the reader
    resolves, and nothing checked that anyone hands it the materials. The guards
    for that live in `test_proposal_decision_unit.py`, added because of the
    probe rather than before it.

    This case asserts the invariant that makes the probe meaningful: the prompt
    key and the resolver are one statement, so backing either one out takes the
    other's tests with it.
    """

    # **2026-09-15：这个键搬了家，而探针跟着搬。**
    # 三种 `user_prompt` 的形状收进了 `runtime/request_shape.py`（唯一定义点，
    # 理由见那个模块的 docstring：先前 admission 与生产各拼各的，
    # 差出来的那圈骨架被数在零处）。**探针指的是「那个键在不在组装里」，
    # 而组装现在在那边** —— 指着旧文件会让这条探针变成一条恒绿的断言。
    source = (
        ROOT / "packages/runtime/src/fastagent_v2/runtime/request_shape.py"
    ).read_text(encoding="utf-8")
    assert '"content_materials": [' in source


def test_a_frame_with_no_materials_is_told_so_rather_than_told_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list and a missing key are different statements to a model.

    "This goal has no approved wording, write your own" is an instruction.
    "Nobody mentioned materials" is the state every turn was in before this
    batch, and the model filled the silence by composing.
    """

    profile = _profile(frame_materials={"introduce": []})

    _, transport = _generate(monkeypatch, profile=profile, definition_id="introduce")

    assert _sent_materials(transport) == {}


def test_a_frame_is_offered_its_own_materials_and_not_its_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`content_materials` is per frame, and the prompt has to keep it that way.

    A profile that separates "the introduction" from "the closing turn" is
    making a product decision about which words belong where. Handing the model
    every material the profile owns would quietly discard it.
    """

    profile = _profile(
        frame_materials={"introduce": [OPENING], "closing": [CLOSING_REF]},
    )

    _, transport = _generate(monkeypatch, profile=profile, definition_id="closing")

    assert _sent_materials(transport) == {CLOSING_REF: DEMO_MATERIALS[CLOSING_REF]}


def test_a_release_published_before_its_package_supplied_text_offers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared refs with no merged text resolve to nothing, not to a bare name.

    Admission refuses this combination for a *package*, but a release published
    before the mechanism existed is already in the database with refs and no
    text. Offering the ref alone would be handing the model a name whose words
    it would then have to supply itself -- the exact defect, wearing the new
    mechanism's clothes.
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    del profile[MATERIAL_TEXTS_KEY]

    _, transport = _generate(monkeypatch, profile=profile, definition_id="introduce")

    assert _sent_materials(transport) == {}


def test_the_material_texts_key_is_spelled_once_per_package() -> None:
    """Two packages, one string. The worker may not import from `services/api`,
    so the key is written twice; a silent disagreement would mean the loader
    stores words the generation step never finds."""

    from fastagent_v2.profiles.registry import MATERIAL_TEXTS_KEY as registry_key

    assert registry_key == MATERIAL_TEXTS_KEY


def test_materials_for_frame_keeps_the_profile_authors_order() -> None:
    """The order materials are written in is a statement about which to lead with."""

    profile = _profile(frame_materials={"introduce": [DONT_KNOW, OPENING]})
    definition = profile["target_frames"][0]

    assert list(materials_for_frame(profile, definition)) == [DONT_KNOW, OPENING]


# ---------------------------------------------------------------------------
# 2. A quote is the merchant's string
# ---------------------------------------------------------------------------


def test_a_quoted_material_reaches_the_channel_as_the_merchant_wrote_it() -> None:
    """**This is what makes "quote, do not rewrite" a property and not a request.**

    The model supplies the ref; the platform supplies the bytes. Nothing the
    model can put in a `material_ref` segment changes the words that go out.
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    materials = materials_for_frame(profile, profile["target_frames"][0])
    content = {
        "segments": [
            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING},
            {"type": "text", "content_or_ref": "您那边现在是谁在接客户消息？"},
        ]
    }

    assert customer_text(content, materials=materials) == (
        DEMO_MATERIALS[OPENING] + "\n您那边现在是谁在接客户消息？"
    )


def test_a_ref_with_no_materials_supplied_refuses_the_whole_message() -> None:
    """**`channel.py`'s blanket refusal is kept, deliberately** -- the third of
    the three questions this grid was asked to answer.

    The adapter calls `customer_text` with no materials, because by then the
    outbox row already holds resolved text. A ref arriving there means
    resolution did not happen, and sending the text half of a message whose
    quoted half is missing is the "silently loses half" failure this platform
    has now met on both the inbound and the outbound side. So the answer is: the
    refusal does not change, it becomes the *second* lock rather than the only
    one.
    """

    content = {
        "segments": [
            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING},
            {"type": "text", "content_or_ref": "还有一句。"},
        ]
    }

    assert customer_text(content) is None


def test_a_ref_outside_the_supplied_materials_refuses_the_whole_message() -> None:
    profile = _profile(frame_materials={"introduce": [OPENING]})
    materials = materials_for_frame(profile, profile["target_frames"][0])
    content = {
        "segments": [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": DONT_KNOW}]
    }

    assert customer_text(content, materials=materials) is None


# ---------------------------------------------------------------------------
# 3. An undeclared ref stops the turn
# ---------------------------------------------------------------------------


def test_a_draft_quoting_a_material_the_frame_does_not_declare_is_refused() -> None:
    profile = _profile(frame_materials={"introduce": [OPENING], "closing": [CLOSING_REF]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": CLOSING_REF}], []
    )

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=generation, plan=plan, materials=allowed
        )

    assert error.value.reason_code == "GENERATION_MATERIAL_REF_UNKNOWN"
    assert CLOSING_REF in error.value.detail


def test_the_refusal_names_the_ref_and_never_the_words() -> None:
    """A reason code without the ref is unactionable; the material's text is
    customer-visible content and does not belong in a decision row."""

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": DONT_KNOW}], []
    )

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=generation, plan=plan, materials=allowed
        )

    assert DONT_KNOW in error.value.detail
    assert DEMO_MATERIALS[DONT_KNOW] not in error.value.detail


def test_the_sent_payload_is_checked_and_not_only_the_draft() -> None:
    """**Two documents, and only one of them is sent.**

    `customer_visible_segments` is the draft; `typed_payload.content` of a
    `send_message` intent is what `_persist_accepted_proposal` turns into an
    outbox row. Checking the draft alone would leave the ref that actually
    reaches the customer unvalidated -- the T265 question answered the wrong way
    by omission.
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")
    generation = _generation(
        [{"type": "text", "content_or_ref": "开场。"}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [
                            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": DONT_KNOW}
                        ]
                    }
                },
            }
        ],
    )

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=generation, plan=plan, materials=allowed
        )

    assert error.value.reason_code == "GENERATION_MATERIAL_REF_UNKNOWN"
    assert "send_message" in error.value.detail


def test_a_declared_ref_passes_both_documents() -> None:
    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [
                            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}
                        ]
                    }
                },
            }
        ],
    )

    assert validate_generation(
            generation=generation, plan=plan, materials=allowed
        ) is generation


# ---------------------------------------------------------------------------
# What this batch cannot do
# ---------------------------------------------------------------------------


def test_a_paraphrased_material_is_not_detected_today() -> None:
    """**Registered as T268; asserted here so the gap is pinned, not assumed.**

    A model that ignores the refs and writes its own version of a material into
    a plain `text` segment passes every check in this batch. Nothing here can
    tell that prose apart from prose the model was entitled to write: the whole
    point of a text segment is that the model composes it.

    **What the batch does instead is prevent, not detect**: a material quoted by
    reference is delivered as the merchant's bytes, so rewriting is not one of
    the things a quote can be. That leaves exactly one uncovered path -- the
    model declining to quote at all -- and this case is what says so out loud.

    A cheap partial answer was considered and not built: flag a text segment
    that is byte-identical to a material (a copy in the wrong shape). It catches
    the one case that is harmless and misses every case that is not, which is a
    check that would read like coverage without being any.
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")
    paraphrase = DEMO_MATERIALS[OPENING][:12] + "……大概就是这个意思。"
    generation = _generation([{"type": "text", "content_or_ref": paraphrase}], [])

    assert validate_generation(
            generation=generation, plan=plan, materials=allowed
        ) is generation

    materials = materials_for_frame(profile, profile["target_frames"][0])
    assert customer_text({"segments": generation["customer_visible_segments"]},
                         materials=materials) == paraphrase
    assert paraphrase != DEMO_MATERIALS[OPENING]


def test_the_deterministic_double_still_ignores_materials() -> None:
    """A double that quoted materials would make every pipeline test look like
    evidence that the real prompt carries them. It is not, and must not become
    so by accident."""

    source = (
        ROOT / "services/worker/src/fastagent_v2/worker/inference.py"
    ).read_text(encoding="utf-8")
    double = source.split("class DeterministicMockInference")[1]
    assert "materials_for_frame" not in double
    assert copy.copy(MATERIAL_TEXTS_KEY) not in double


# ---------------------------------------------------------------------------
# One turn, one frame (independent review 2026-09-10, P1-1)
# ---------------------------------------------------------------------------


def test_the_prompt_and_the_validator_stand_in_the_same_frame_after_a_push() -> None:
    """**Every case above writes one frame id twice, so none of them could see this.**

    `process_prepared` binds `plan.definition` at `plan_or_fallback`, then
    `apply_target_stack_operation` **clears and rewrites `prepared.aggregate` in
    place**. Any plan whose operation is not `keep` therefore moves the stack top
    out from under the plan: the generation step reached for the new frame's
    materials while the validator judged them against the old frame's list, and
    the customer received nothing at all.

    So this case runs the pipeline's own order -- plan against frame A, apply a
    `push`, then resolve materials -- and asserts the two readers agree. It is
    driven through `apply_target_stack_operation` rather than through two helper
    arguments, because writing the two frame ids into a fixture is writing the
    conclusion into the fixture.
    """

    from fastagent_v2.worker.platform_frames import frame_definitions  # noqa: PLC0415
    from fastagent_v2.worker.target_stack import (  # noqa: PLC0415
        apply_target_stack_operation,
    )

    profile = _profile(frame_materials={"introduce": [OPENING], "closing": [CLOSING_REF]})
    for target in profile["target_frames"]:
        target["return_confirmation"] = {"policy": "optional", "tone_material_ref": ""}
    aggregate = {
        "id": "11111111-1111-4111-8111-111111111111",
        "tenant_id": "22222222-2222-4222-8222-222222222222",
        "state_version": 1,
        "generation": 0,
        "control_mode": "ai",
        "profile_release_id": "33333333-3333-4333-8333-333333333333",
        "target_stack": [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "definition_id": "introduce",
                "goal": "goal of introduce",
                "status": "active",
                "slots": {},
                "entered_at": "2026-09-01T00:00:00Z",
                "last_progress_at": "2026-09-01T00:00:00Z",
                "parent_frame_id": None,
                "interruption_reason": None,
                "resume_confirmation_policy": "optional",
            }
        ],
    }
    # The plan was validated while standing in `introduce` -- this is the frame
    # `ValidatedPlan.definition` freezes.
    plan_definition = frame_definitions(profile)["introduce"]

    transitioned = apply_target_stack_operation(
        aggregate=aggregate,
        operation={"op": "push", "target_frame_definition_id": "closing"},
        interruption_relation="side_question",
        profile=profile,
        occurred_at="2026-09-01T00:01:00Z",
    )

    top = transitioned["target_stack"][-1]
    assert str(top["definition_id"]) == "closing", "the push has to move the stack top"

    # What `process_prepared` computes once and hands to all four readers.
    turn_definition = frame_definitions(profile).get(str(top["definition_id"]), {})
    turn_materials = materials_for_frame(profile, turn_definition)

    # The frame the message will be *filed under* is this one -- the pipeline
    # takes `intent_records.target_frame_id` from exactly this stack top -- so
    # these are the words it may quote.
    assert turn_materials == {CLOSING_REF: DEMO_MATERIALS[CLOSING_REF]}
    assert materials_for_frame(profile, plan_definition) != turn_materials, (
        "the two frames must genuinely differ, or this case proves nothing"
    )

    # And the validator, given that same mapping, accepts the material the model
    # was shown. Under the defect it was handed `plan.definition`'s list instead
    # and refused this exact document with GENERATION_MATERIAL_REF_UNKNOWN.
    plan = ValidatedPlan(
        document={
            **_plan_document(),
            "origin": "model",
            "candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}],
        },
        frame=dict(top),
        definition=plan_definition,
    )
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": CLOSING_REF}], []
    )

    assert (
        validate_generation(
            generation=generation, plan=plan, materials=turn_materials
        )
        is generation
    )


def test_a_resolved_material_is_trimmed_like_every_other_segment() -> None:
    """**P3-1: the claim was "the merchant's bytes"; it is the merchant's text.**

    `_usable` drops leading and trailing whitespace for every segment, and a
    resolved material is not exempt. Pinned rather than left implicit because
    the docstring next to it asserted byte-for-byte delivery, which is a
    different promise -- and one a material stored with an indent would break.
    """

    padded = "  \n开场白。\n  "
    content = {"segments": [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}]}

    assert customer_text(content, materials={OPENING: padded}) == "开场白。"


# ---------------------------------------------------------------------------
# The draft is not what ships (2026-09-10, 事后更正)
# ---------------------------------------------------------------------------
#
# **这一节存在的理由是它以前不存在。** 素材那一批把「引用即不可改写」实现在
# `customer_visible_segments` 上、并且断言「草稿与 payload 两份文档都查」——
# 而那句只在 payload 自带 segments 时为真。真实对照三组六个臂：草稿引用 1/3/1 段，
# 出站那一侧全是 0 段。**没有一条用例能红**，因为没有一条造过「两边不一致」的输出。


def _drafted_but_not_sent() -> dict[str, Any]:
    """草稿规规矩矩引用，payload 写自己的话——实测到的那个形态。"""

    return _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [
                            {"type": "text", "content_or_ref": "您那边现在是谁在接客户消息？"}
                        ]
                    }
                },
            }
        ],
    )


def test_quoting_in_the_draft_and_writing_your_own_in_the_payload_is_refused() -> None:
    """**发出去的是 payload，所以两边不一致就是「商家原文没出去」。**

    拒发而不是照发：照发的结果是客户收到模型的散文、商家的原文一个字都没出去，
    而每一道门禁都是绿的——那正是这条规则要消灭的形态。
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    allowed = _offered(profile, "introduce")

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=_drafted_but_not_sent(), plan=plan, materials=allowed
        )

    assert error.value.reason_code == "GENERATION_PAYLOAD_DROPPED_MATERIAL"
    assert OPENING in error.value.detail
    # ref 进理由，正文不进——与兄弟那条同一个口径。
    assert DEMO_MATERIALS[OPENING] not in error.value.detail


def test_a_payload_carrying_no_segments_at_all_is_the_same_refusal() -> None:
    """**实测到的正是这一种**：payload 用了另一种 content 形状，于是原来那条检查
    「没有 segments 可查」，就什么都没说。"""

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {"content": {"type": "text", "text": "我自己写的一句。"}},
            }
        ],
    )

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=generation,
            plan=plan,
            materials=_offered(profile, "introduce"),
        )

    assert error.value.reason_code == "GENERATION_PAYLOAD_DROPPED_MATERIAL"


def test_a_payload_that_quotes_what_the_draft_quotes_passes() -> None:
    """没有这一条，上面两条在一个「拒绝一切」的实现上也会绿。"""

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    generation = _generation(
        [{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [
                            {"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": OPENING},
                            {"type": "text", "content_or_ref": "还有一句我自己的。"},
                        ]
                    }
                },
            }
        ],
    )

    assert (
        validate_generation(
            generation=generation,
            plan=plan,
            materials=_offered(profile, "introduce"),
        )
        is generation
    )


def test_a_draft_that_quotes_nothing_leaves_this_rule_silent() -> None:
    """**这条规则可以被「两边都不引用」绕过，而那是有意的。**

    平台一刀切的「有素材就必须引用」会撞上不设闸的兜底——它在每个普通回合都可用，
    于是模型在一个答得上来的回合也只能引用「这个我不确定」。**不可绕的那一半由
    profile 用 `must_quote_material` 声明**，见下一条。
    """

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    generation = _generation(
        [{"type": "text", "content_or_ref": "我自己写的一句。"}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [{"type": "text", "content_or_ref": "我自己写的一句。"}]
                    }
                },
            }
        ],
    )

    assert (
        validate_generation(
            generation=generation,
            plan=plan,
            materials=_offered(profile, "introduce"),
        )
        is generation
    )


def test_must_quote_material_closes_the_quote_nowhere_escape() -> None:
    """商家声明的那一半：这个打法必须引用，两边都不引用也不行。"""

    profile = _profile(frame_materials={"introduce": [OPENING]})
    plan = _validated_plan(profile, "introduce")
    plan.document["generation_constraints"] = ["must_quote_material"]
    generation = _generation(
        [{"type": "text", "content_or_ref": "我自己写的一句。"}],
        [
            {
                "kind": "send_message",
                "evidence_refs": ["event:1"],
                "typed_payload": {
                    "content": {
                        "segments": [{"type": "text", "content_or_ref": "我自己写的一句。"}]
                    }
                },
            }
        ],
    )

    with pytest.raises(GenerationRejected) as error:
        validate_generation(
            generation=generation,
            plan=plan,
            materials=_offered(profile, "introduce"),
        )

    assert error.value.reason_code == "MATERIAL_QUOTE_REQUIRED"


def test_the_prompt_now_shows_a_payload_carrying_content() -> None:
    """**成因那一条，钉住。**

    v5 的两个示例都写着 `"proposed_intents": []`——从没展示过 payload 装内容，
    而 payload 才是发出去的那份。模型做的正是它被教的事。
    """

    from fastagent_v2.worker.inference_model import (  # noqa: PLC0415
        _GENERATE_SYSTEM,
        PROMPT_RELEASE,
    )

    assert PROMPT_RELEASE == "m1a-prompt-v17"
    assert '"proposed_intents": []' not in _GENERATE_SYSTEM, (
        "两个示例都必须展示 payload 装内容——那是这次缺陷的直接成因"
    )
    assert "typed_payload" in _GENERATE_SYSTEM
    assert "not customer_visible_segments" in _GENERATE_SYSTEM
