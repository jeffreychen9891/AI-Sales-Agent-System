"""Every shipped time rule declares a play its profile can actually use. **B4-4d.**

This is the check that would have caught the defect this batch was built around:
`synthetic-third`'s `clarify_request-followup_due` declared `propose_followup`
while the profile **did not register it** and the frame did not allow it. Nothing
reported that, because `strategy` had no reader -- the rule could never have
produced a message and would have failed silently into the safe fallback forever.

That is exactly what ADR-0105 §零.1 said the field's zero-reader state was
costing, and ADR-0116 is the ruling that made the rule usable without also
widening the model's ordinary-turn menu.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.strategies import time_rule_strategy_set

REPO_ROOT = Path(__file__).resolve().parents[2]


def _shipped(name: str) -> dict[str, Any]:
    return json.loads((REPO_ROOT / "profiles" / name / "profile.json").read_text("utf-8"))


@pytest.mark.parametrize("name", ["synthetic-carrier", "synthetic-third"])
def test_every_shipped_rule_declares_a_strategy_the_profile_can_actually_use(name: str) -> None:
    """The check that would have caught the defect this batch found.

    `synthetic-third`'s `clarify_request-followup_due` declared
    `propose_followup` while the profile **did not register it** and the frame
    did not allow it. Nothing reported that, because `strategy` had no reader:
    the rule could never have produced a message, and would have failed silently
    into the safe fallback forever.

    That is exactly what ADR-0105 §零.1 said the field's zero-reader state was
    costing. The fix is two rulings deep -- ADR-0116 gave the frame a separate
    `time_rule_strategy_set` so making the rule usable did not also widen the
    model's ordinary-turn menu -- and this is the guard that keeps the next one
    from being silent.
    """

    profile = _shipped(name)
    registered = set(profile["strategies"])
    for definition in profile["target_frames"]:
        usable = set(time_rule_strategy_set(definition))
        for rule in definition["time_rules"]:
            strategy = str(rule["strategy"])
            assert strategy in registered, (
                f"{name}/{definition['id']}/{rule['id']} declares {strategy!r}, "
                "which the profile does not register"
            )
            assert strategy in usable, (
                f"{name}/{definition['id']}/{rule['id']} declares {strategy!r}, "
                f"which this frame's time rules may not use ({sorted(usable)})"
            )
