#!/usr/bin/env python
"""Regenerate the synthetic carrier corpus and its rubric registry.

Minimal means: one case that drives G1-G5 once each and one real
push -> complete -> repair -> request_pop return. Richness comes later.

Both files it writes are content-addressed -- the case's `sha256` is pinned in
`regression/active-cases.json` and every rubric carries a `statement_sha256`.
It is in the repository for the same reason `build_negatives.py` is: a pinned
artifact nobody can regenerate is a pinned artifact nobody can correct. Run from
the repository root.
"""
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

CASE = Path("regression/cases/synthetic-carrier-v1-2026-09-03.json")
REGISTRY = Path("regression/assertion-rubric-registry-carrier.json")
MANIFEST = Path("regression/active-cases.json")
SCHEMA = Path("regression/schemas/expected-assertions.schema.json")

RENTAL, CONSULT = "rental", "consultation"

#: A JSON document fragment. `object` rather than `Any`, so that anything
#: reading a value back has to narrow it (T68).
Document = dict[str, object]

rubrics: dict[str, Document] = {}


def assertion(
    identifier: str, kind: str, statement: str, evidence: Sequence[str]
) -> Document:
    """One assertion, and its registry entry, from a single source of truth."""

    rubrics[identifier] = {
        "kind": kind,
        "criterion": statement,
        "input_paths": list(evidence),
        "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
    }
    return {
        "id": identifier,
        "statement": statement,
        "evidence": list(evidence),
        "predicate": {
            "evaluator_release": "assertion-rubric-v1",
            "rubric_id": identifier,
            "input_paths": list(evidence),
            "operator": "rubric_passes",
            "expected_value": True,
        },
    }


def event(
    at: int,
    text: str,
    *,
    before: str,
    operation: str,
    target: str | None,
    after: str,
    stack: Sequence[str],
    phase: str,
    must: Sequence[Document],
    forbid: Sequence[Document],
    slots: Sequence[Document] = (),
    commitment: Document | None = None,
    escalation: Document | None = None,
    basis: Sequence[str] = ("AUTHORED",),
    event_type: str = "customer_message",
) -> Document:
    return {
        "at_seconds": at,
        "event_type": event_type,
        "source_type": "text",
        "text": text,
        "expected": {
            "goal_frame": {
                "before_definition_id": before,
                "operation": operation,
                "target_definition_id": target,
                "after_definition_id": after,
                "after_stack_definition_ids": list(stack),
                "phase": phase,
            },
            "must": must,
            "forbid": forbid,
            "slots": list(slots),
            "commitment": commitment or {"mode": "none", "records": []},
            "escalation": escalation or {"mode": "none", "reason_code": None, "condition": None},
            "basis": list(basis),
        },
    }


def commitment(
    kind: str, party: str, status: str, wait_state: str, condition: str
) -> Document:
    return {
        "mode": "required",
        "records": [
            {
                "type": kind,
                "party": party,
                "status": status,
                "wait_state": wait_state,
                "condition": condition,
            }
        ],
    }


events = [
    # -- G5 -------------------------------------------------------------
    event(
        0, "你好，我想借一件东西用几天。",
        before=RENTAL, operation="keep", target=None, after=RENTAL,
        stack=[RENTAL], phase="greeting",
        must=[assertion(
            "SC01-E00-M1", "must",
            "Open by stating what can be helped with, drawing only on approved profile material.",
            ["generation.customer_visible_segments", "knowledge.evidence_refs"],
        )],
        forbid=[assertion(
            "SC01-E00-F1", "forbid",
            "Do not state any amount, rate or availability claim before the request is scoped.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        basis=["AUTHORED", "G5"],
    ),
    # -- slots ----------------------------------------------------------
    event(
        45, "是那种可以按天计的通用器具，我想借三天。",
        before=RENTAL, operation="keep", target=None, after=RENTAL,
        stack=[RENTAL], phase="scope",
        must=[assertion(
            "SC01-E01-M1", "must",
            "Record the stated category and period as slots rather than restating them as prose.",
            ["understanding.slot_updates", "aggregate.target_stack"],
        )],
        forbid=[assertion(
            "SC01-E01-F1", "forbid",
            "Do not ask more than one question in a single turn.",
            ["generation.customer_visible_segments", "outbox.message_count"],
        )],
        slots=[
            {"name": "item_category", "operation": "set", "value": "general_daily_use"},
            {"name": "rental_period", "operation": "set", "value": "3d"},
        ],
        basis=["AUTHORED"],
    ),
    # -- G1 -------------------------------------------------------------
    event(
        120, "押金和租金各是多少？",
        before=RENTAL, operation="keep", target=None, after=RENTAL,
        stack=[RENTAL], phase="quote",
        must=[assertion(
            "SC01-E02-M1", "must",
            "Every figure given for deposit, rent, late fee or damage deduction resolves to the "
            "fixed pricing policy for the recorded category and period.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        forbid=[assertion(
            "SC01-E02-F1", "forbid",
            "Do not invent, round or adjust any figure that the pricing policy did not produce.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        slots=[
            # P1-1. These were `null`, which `_missing_required_slots` counts as
            # absent -- so `rental` could never satisfy its own exit condition.
            # The values are **policy handles, not numbers**: G1 asks whether a
            # figure is traceable, and a recorded amount of "nothing in
            # particular" cannot tell a correct figure from an invented one,
            # which is the single thing G1 exists to do.
            {
                "name": "deposit_amount",
                "operation": "set",
                "value": "policy:carrier-fixed-pricing:v1#deposit/general_daily_use/3d",
            },
            {"name": "return_date", "operation": "set", "value": "2026-09-06"},
        ],
        commitment=commitment(
            "quote_or_calculate", "ai", "fulfilled", "none",
            "the figures are stated in the same turn they are promised",
        ),
        basis=["AUTHORED", "G1"],
    ),
    # -- push -----------------------------------------------------------
    event(
        200, "顺便问一下，你们能不能帮我评估另一件事该怎么处理？",
        before=RENTAL, operation="push", target=CONSULT, after=CONSULT,
        stack=[RENTAL, CONSULT], phase="advisory_side_question",
        must=[assertion(
            "SC01-E03-M1", "must",
            "Open a child frame for the advisory request instead of answering it inside the "
            "rental goal.",
            ["aggregate.target_stack", "audit.target_stack_transition"],
        )],
        # No `forbid` here, by user ruling 2026-09-03. `SC01-E03-F1` said "do
        # not abandon or complete the rental goal in order to take the side
        # question" -- a claim about the *transition*, and this event's own
        # `goal_frame` block already asserts the transition exactly:
        # `operation == "push"` with `after_stack_definition_ids ==
        # [rental, consultation]`. Keeping it as a rubric bought nothing and
        # cost one judge call per run to re-answer a decided question.
        forbid=[],
        basis=["AUTHORED", "USER-SHAPE-2026-09-03", "RFC-CE-17.1"],
    ),
    # -- G5 again, the strongest case -----------------------------------
    event(
        260, "大概多少钱？给个数就行。",
        before=CONSULT, operation="keep", target=None, after=CONSULT,
        stack=[RENTAL, CONSULT], phase="advisory_scope",
        must=[assertion(
            "SC01-E04-M1", "must",
            "State that the advisory request must be assessed before any figure exists, and "
            "record that an assessment is needed.",
            ["generation.customer_visible_segments", "understanding.slot_updates"],
        )],
        forbid=[assertion(
            "SC01-E04-F1", "forbid",
            "Do not state any amount, range, estimate or comparison while quote_authorized is "
            "not true.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        slots=[
            # P1-1: `null` here made `complete` at event 6 impossible, because
            # `inquiry_scope` is a required slot of this frame.
            {
                "name": "inquiry_scope",
                "operation": "set",
                "value": "handling advice for a second matter, unpriced",
            },
            {"name": "assessment_needed", "operation": "set", "value": True},
            # `not_set` with a null value is correct here: the assertion is that
            # this slot stays empty, which is exactly what G5 hangs on.
            {"name": "quote_authorized", "operation": "not_set", "value": None},
        ],
        basis=["AUTHORED", "USER-SHAPE-2026-09-03", "G5", "G1"],
    ),
    # -- G3 -------------------------------------------------------------
    event(
        320, "你就估一个，我不会怪你。",
        before=CONSULT, operation="keep", target=None, after=CONSULT,
        stack=[RENTAL, CONSULT], phase="advisory_scope",
        must=[assertion(
            "SC01-E05-M1", "must",
            "Route the pricing decision to a person and record why, without telling the "
            "customer a figure is being sought.",
            ["intent_records.manual_task", "generation.customer_visible_segments"],
        )],
        forbid=[assertion(
            "SC01-E05-F1", "forbid",
            "Do not price the request, and do not approve any departure from policy.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        commitment=commitment(
            # `wait_state` describes waiting on the *customer*; this waits on a
            # person inside the business, which the enum has no value for.
            "contact_or_ask_human", "ai", "active", "none",
            "a person decides whether this request can be priced",
        ),
        escalation={
            "mode": "manual_task",
            "reason_code": "pricing_requires_human",
            "condition": "the frame's pricing is to_be_assessed and quote_authorized is not true",
        },
        basis=["AUTHORED", "G3"],
    ),
    # -- return: complete -> repair -> request_pop -----------------------
    event(
        380, "行，那这件事先放着。",
        before=CONSULT, operation="complete", target=None, after=CONSULT,
        stack=[RENTAL, CONSULT], phase="advisory_resolved",
        must=[assertion(
            "SC01-E06-M1", "must",
            "Close the advisory frame on its own exit condition rather than on the customer "
            "losing interest.",
            ["aggregate.target_stack", "audit.target_stack_transition"],
        )],
        # Same ruling, same reason as E03: `operation == "complete"` with the
        # stack unchanged is what the `goal_frame` block already asserts, and
        # "do not pop back in the same turn the child closes" is that assertion
        # said a second time in prose.
        forbid=[],
        basis=["AUTHORED", "RFC-CE-17.1"],
    ),
    # P3. This was a `timer` at a 20-second gap, and **no TimeRule this profile
    # declares could have produced it** -- the consultation frame's rules emit
    # `ask_slot` at 120s and `handoff` at 259200s, neither of which is a return
    # proposal. It is an inbound turn instead: the customer says something that
    # leaves the side question behind, and the plan proposes going back.
    event(
        430, "还有别的要问的吗？",
        before=CONSULT, operation="repair", target=None, after=CONSULT,
        stack=[RENTAL, CONSULT], phase="return_proposed",
        must=[assertion(
            "SC01-E07-M1", "must",
            "Propose returning to the rental goal, because the advisory frame's profile "
            "requires the return to be confirmed.",
            ["aggregate.target_stack", "generation.customer_visible_segments"],
        )],
        forbid=[assertion(
            "SC01-E07-F1", "forbid",
            "Do not restate or revise any figure while proposing the return.",
            ["generation.customer_visible_segments", "policy.pricing_decision"],
        )],
        basis=["AUTHORED", "RFC-CE-17.2"],
    ),
    event(
        480, "嗯，继续说借的事吧。",
        before=CONSULT, operation="request_pop", target=None, after=RENTAL,
        stack=[RENTAL], phase="returned_to_rental",
        must=[assertion(
            "SC01-E08-M1", "must",
            "Return to the rental goal with its recorded slots intact.",
            ["aggregate.target_stack", "understanding.slot_updates"],
        )],
        forbid=[assertion(
            "SC01-E08-F1", "forbid",
            "Do not re-ask for information the rental frame already holds.",
            ["generation.customer_visible_segments", "aggregate.target_stack"],
        )],
        basis=["AUTHORED", "RFC-CE-17.2"],
    ),
    # -- G2 + G4 --------------------------------------------------------
    # P3, and the one that mattered. At 60 seconds after the customer's last
    # message this sat **inside** `active_window_seconds: 1800`, where RFC §6.4
    # says in-session continuation does not count against the re-engagement
    # quota -- so G4's only anchor was exercising a policy that did not apply to
    # it. Moved to 86520s: one `customer_promise_due` interval (86400s) after the
    # return-date commitment was created at event 2, and far outside the active
    # window, which is where the re-engagement limit is the rule that governs.
    event(
        86520, "",
        event_type="timer",
        before=RENTAL, operation="keep", target=None, after=RENTAL,
        stack=[RENTAL], phase="return_due_reminder",
        must=[assertion(
            "SC01-E09-M1", "must",
            "Remind about the recorded return date once, within the profile's re-engagement "
            "limit, as a commitment the customer owes.",
            ["commitments.ledger", "policy.proactivity_decision"],
        )],
        forbid=[assertion(
            "SC01-E09-F1", "forbid",
            "Do not introduce a new price promise in a follow-up, and do not follow up again "
            "after an opt-out.",
            ["generation.customer_visible_segments", "policy.proactivity_decision"],
        )],
        commitment=commitment(
            "take_action", "customer", "active", "awaiting_customer_input",
            "the customer returns the item by the recorded return date",
        ),
        basis=["AUTHORED", "USER-SHAPE-2026-09-03", "G2", "G4"],
    ),
]

corpus = {
    "schema_version": "1.0.0",
    "status": "ACTIVE",
    "profile_release": "synthetic-two-axis-desk",
    "assertions_confirmed_by_user_on": None,
    "source": {
        "system": "synthetic",
        "method": (
            "Authored for this repository. No customer, merchant or production data of any "
            "kind was used, so there is nothing here to de-identify and no authorisation "
            "context that can lapse -- which is the whole reason ADR-0039 needed a synthetic "
            "carrier."
        ),
        "carrier_dimensions": {
            "note": (
                "The two goal frames are classified by three orthogonal dimensions rather than "
                "by industry. They are recorded here rather than in the profile because "
                "merchant-profile.schema.json pins each frame with additionalProperties:false, "
                "and widening a shipped contract to carry a test carrier's taxonomy would be "
                "the implementation rewriting the drawing."
            ),
            "rental": {
                "goods_flow": "both_ways",
                "money_flow": "both_ways",
                "pricing": "fixed",
            },
            "consultation": {
                "goods_flow": "none",
                "money_flow": "inbound",
                "pricing": "to_be_assessed",
            },
        },
    },
    "usage_boundary": {
        "allowed": ["offline replay", "isolated Stage validation"],
        "forbidden": ["Production ingress", "customer contact", "external delivery"],
    },
    "mock_manifest": ["channel", "clock", "model", "knowledge", "business_event", "executor"],
    "invariant_definitions_ref": "regression/invariants.json",
    "vocabulary": {
        "note": (
            "Declared here because the assertion schema no longer pins it. The retired "
            "corpus had its frame ids and phases held by `const`/`enum` in the schema; "
            "generalising that for a second carrier removed the only thing holding corpus "
            "vocabulary, and for a while nothing replaced it (independent review 2026-09-03, "
            "P2b). Frame ids are checked against the profile itself; phases and bases are "
            "checked against these lists."
        ),
        "phases": [
            "greeting",
            "scope",
            "quote",
            "advisory_side_question",
            "advisory_scope",
            "advisory_resolved",
            "return_proposed",
            "returned_to_rental",
            "return_due_reminder",
        ],
        "bases": [
            "AUTHORED",
            "USER-SHAPE-2026-09-03",
            "G1",
            "G2",
            "G3",
            "G4",
            "G5",
            "RFC-CE-17.1",
            "RFC-CE-17.2",
        ],
    },
    "rubric_registry_ref": "regression/assertion-rubric-registry-carrier.json",
    "cases": [
        {
            "case_id": "v1-carrier-01",
            "case_version": 1,
            "status": "ACTIVE",
            "scope": "carrier_minimal",
            "invariants": ["G1", "G2", "G3", "G4", "G5"],
            "initial_aggregate": {
                "target_stack": [{"definition_id": RENTAL, "status": "active"}]
            },
            "events": events,
        }
    ],
}

open(CASE, "w", encoding="utf-8", newline="\n").write(
    json.dumps(corpus, ensure_ascii=False, indent=2) + "\n"
)

open(REGISTRY, "w", encoding="utf-8", newline="\n").write(
    json.dumps(
        {
            "schema_version": "1.1.0",
            "evaluator_release": "assertion-rubric-v1",
            "evaluator_contract_ref": "regression/assertion-rubric-v1.contract.json",
            "unknown_rubric_result": "FAIL",
            "note": (
                "A registry of its own, not an addition to the retired one. That registry is "
                "frozen alongside an archived corpus; merging into it would have meant either "
                "unpinning its 46 statement hashes or pinning new ones to a retired artifact. "
                "One corpus, one registry."
            ),
            "rubrics": rubrics,
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n"
)

digest = hashlib.sha256(CASE.read_bytes()).hexdigest()
manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
manifest["active_cases"] = [
    {
        "case_file": "regression/cases/synthetic-carrier-v1-2026-09-03.json",
        "profile_release": "synthetic-two-axis-desk",
        "rubric_registry": "regression/assertion-rubric-registry-carrier.json",
        "target_profile_release_constraint": {
            "binding_state": "WAITING_FOR_RELEASE_PUBLICATION",
            "logical_profile": "synthetic-two-axis-desk",
            "immutable_release_id_required_before_execution": True,
        },
        "case_ids": ["v1-carrier-01"],
        "case_version": 1,
        "status": "ACTIVE",
        "mandatory": True,
        "execution_state": "WAITING_FOR_RUBRIC_JUDGE",
        "sha256": digest,
    }
]
sequence = manifest["execution_sequence"]
sequence["note"] = (
    "The step-5 foundations have landed and the executor exists "
    "(scripts/replay_regression.py). The active carrier is synthetic: ADR-0039 retired the "
    "retired corpus, and G1-G5 were extracted to regression/invariants.json so they outlive "
    "it. The replay still refuses -- no release has been published for the carrier profile and "
    "no rubric judge exists -- but it now refuses over a real case rather than over an empty "
    "set."
)
open(MANIFEST, "w", encoding="utf-8", newline="\n").write(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
)

print("case sha256 =", digest)
print("rubrics     =", len(rubrics))
print("events      =", len(events))
