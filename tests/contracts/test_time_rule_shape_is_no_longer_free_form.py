"""B4-1's own exit criterion, asserted from the refusing side.

**Found by a reverse probe that came back green.** Loosening
`time_rules.items` back to `{"type": "object"}` -- undoing this batch entirely --
broke nothing: 664 contract cases still passed. Every case in the suite feeds
the schema a *valid* profile, and widening a schema never makes a valid document
invalid. So the tightening was the deliverable and nothing held it.

This is the same shape as the rule ADR-0103 §二.1 states for the ingress: the
assertion that costs something is the one that says **what must be refused**.

`quiet_hours` gets the same treatment for the same reason: it was the second
`{"type": "object"}` ADR-0096 §二.2 named.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.runtime.contracts import validate_contract

ROOT = Path(__file__).resolve().parents[2]
CARRIER = ROOT / "profiles" / "synthetic-carrier" / "profile.json"


@pytest.fixture(scope="module")
def profile() -> dict[str, Any]:
    return json.loads(CARRIER.read_text(encoding="utf-8"))


def rules(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["target_frames"][0]["time_rules"]


def refuses(document: dict[str, Any]) -> None:
    """The contract must reject `document`, and reject it for its own reason."""

    with pytest.raises(Exception) as caught:
        validate_contract("merchant-profile", document)
    # Not a bare `raises`: a document rejected because an unrelated required
    # field went missing would satisfy that while proving nothing.
    assert "time_rules" in str(caught.value) or "quiet_hours" in str(caught.value), caught.value


def test_the_shipped_profile_still_satisfies_the_contract(profile: dict[str, Any]) -> None:
    """The floor. Everything below asserts refusal, so this is what stops the
    whole file from passing by refusing a document that was never valid."""

    validate_contract("merchant-profile", profile)


def test_the_original_three_key_shape_is_refused(profile: dict[str, Any]) -> None:
    """`{kind, after_seconds, strategy}` -- what both shipped profiles carried
    before this batch, and one of the three shapes ADR-0096 §二.2 says must stop
    validating simultaneously."""

    document = copy.deepcopy(profile)
    document["target_frames"][0]["time_rules"] = [
        {"kind": "silence_check", "after_seconds": 90, "strategy": "advance_goal"}
    ]
    refuses(document)


def test_a_half_migrated_rule_is_refused(profile: dict[str, Any]) -> None:
    """The dangerous one: new shape with `after_seconds` left behind.

    A rule carrying both `due.offset_seconds` and `after_seconds` has two
    answers to "when", and nothing downstream would say which it used.
    """

    document = copy.deepcopy(profile)
    rule = copy.deepcopy(rules(document)[0])
    rule["after_seconds"] = 90
    document["target_frames"][0]["time_rules"] = [rule]
    refuses(document)


@pytest.mark.parametrize(
    "field", ["id", "emit_kind", "due", "dedupe_template", "cancel_on", "strategy"]
)
def test_every_required_part_is_actually_required(profile: dict[str, Any], field: str) -> None:
    """Each of the six parts, dropped one at a time.

    Parametrised rather than written as one case with six deletions: one case
    stops at the first assertion, so five of the six would never be exercised.
    `strategy` is in here because it is the part ADR-0105 §零 D1 added, and the
    reason it was kept is that the profile author's product decision lives in it.
    """

    document = copy.deepcopy(profile)
    rule = copy.deepcopy(rules(document)[0])
    del rule[field]
    document["target_frames"][0]["time_rules"] = [rule]
    refuses(document)


@pytest.mark.parametrize(
    "anchor", ["frame_entered_at", "last_outbound_at", "commitment_due_at", "business_event_at"]
)
def test_the_four_rfc_anchors_are_accepted(profile: dict[str, Any], anchor: str) -> None:
    """Guards the guard, in the direction the others cannot.

    An `anchor` enum that had lost a value would pass every refusal case above
    while quietly making a legal profile unshippable -- the failure mode that
    only shows up the day someone writes the rule that needs it.
    """

    document = copy.deepcopy(profile)
    rule = copy.deepcopy(rules(document)[0])
    rule["due"]["anchor"] = anchor
    document["target_frames"][0]["time_rules"] = [rule]
    validate_contract("merchant-profile", document)


def test_an_unknown_anchor_is_refused(profile: dict[str, Any]) -> None:
    document = copy.deepcopy(profile)
    rule = copy.deepcopy(rules(document)[0])
    rule["due"]["anchor"] = "whenever_feels_right"
    document["target_frames"][0]["time_rules"] = [rule]
    refuses(document)


def test_a_free_form_dedupe_template_is_refused(profile: dict[str, Any]) -> None:
    """ADR-0105 §零 D3 pinned the minimal template until B4-2 supplies the
    allowlist. A free-form template is how two rules silently land on one dedupe
    key and one of them stops being emitted."""

    document = copy.deepcopy(profile)
    rule = copy.deepcopy(rules(document)[0])
    rule["dedupe_template"] = "{emit_kind}:{conversation_id}:{whatever}"
    document["target_frames"][0]["time_rules"] = [rule]
    refuses(document)


def test_quiet_hours_is_no_longer_free_form(profile: dict[str, Any]) -> None:
    """The second `{"type": "object"}` ADR-0096 §二.2 named."""

    document = copy.deepcopy(profile)
    document["proactivity_policy"]["quiet_hours"] = {"whenever": "we feel like it"}
    refuses(document)


def test_a_quiet_hours_time_that_is_not_a_wall_clock_is_refused(
    profile: dict[str, Any],
) -> None:
    document = copy.deepcopy(profile)
    document["proactivity_policy"]["quiet_hours"] = {
        "timezone": "Asia/Shanghai",
        "ranges": [{"start": "22:30", "end": "25:00"}],
    }
    refuses(document)


def test_the_midnight_crossing_window_is_still_accepted(profile: dict[str, Any]) -> None:
    """22:30-08:00 is the shipped window and `end` < `start` on purpose.

    Asserted because the obvious tightening -- requiring `start` before `end` --
    would refuse the only window either shipped profile actually declares.
    """

    document = copy.deepcopy(profile)
    document["proactivity_policy"]["quiet_hours"] = {
        "timezone": "Asia/Shanghai",
        "ranges": [{"start": "22:30", "end": "08:00"}],
    }
    validate_contract("merchant-profile", document)
