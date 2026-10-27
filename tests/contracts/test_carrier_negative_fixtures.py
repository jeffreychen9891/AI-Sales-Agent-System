"""Negative fixtures: is every `forbid` in the carrier corpus falsifiable?

A `forbid` nobody can produce a violation for is not a rule, it is a sentence.
The corpus shipped with ten of them and **not one had ever been violated by
anything** -- the decision table's `forbid` branch was unit-tested, but no
recorded output had ever been run against a real criterion.

Writing the violations turned out to measure something else as well: how much of
this corpus a rubric judge is actually load-bearing for. Three groups fell out,
and the split is the useful product of the batch:

* **screenable** -- a deterministic screen catches clear violations with no model
  at all;
* **structural** -- decidable from the transition, and already asserted by the
  corpus's own `goal_frame` block;
* **judge_only** -- needs semantics no screen can reach.

The screen **may only FAIL**. It never returns a pass, so it cannot turn a
corpus green; an output it does not object to is one it has no opinion about,
and the judge still decides. That asymmetry is what makes it safe to run before
any judge exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGRESSION = ROOT / "regression"
CASE_FILE = "regression/cases/synthetic-carrier-v1-2026-09-03.json"
FIXTURES_PATH = REGRESSION / "cases" / "synthetic-carrier-negatives-2026-09-03.json"
REGISTRY_PATH = REGRESSION / "assertion-rubric-registry-carrier.json"
MANIFEST_PATH = REGRESSION / "active-cases.json"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixtures() -> list[dict[str, Any]]:
    return load(FIXTURES_PATH)["fixtures"]


def test_every_forbid_rubric_has_a_violation_someone_could_produce(
    fixtures: list[dict[str, Any]],
) -> None:
    """Falsifiability, one rubric at a time.

    If a `forbid` has no example that breaks it, either the rule is unreachable
    or nobody understands it well enough to break it on purpose. Both mean the
    assertion is not testing anything.
    """

    registry = load(REGISTRY_PATH)["rubrics"]
    forbids = {key for key, value in registry.items() if value["kind"] == "forbid"}
    covered = {item["rubric_id"] for item in fixtures}
    assert covered == forbids, f"uncovered: {sorted(forbids - covered)}"

    for item in fixtures:
        assert item["violating"], item["rubric_id"]
        assert item["compliant"], item["rubric_id"]


def registration_for(case_file: str) -> dict[str, Any]:
    """Find the manifest entry by what it is about, not by where it sits.

    This used to be `["active_cases"][0]`. One case exists today; a second one
    that did not land at index 0 would have sent this test off to check some
    other case's fixtures, or to a `KeyError`, while claiming to guard these
    (independent review 2026-09-03, P3-1).
    """

    return select_registration(load(MANIFEST_PATH)["active_cases"], case_file)


def select_registration(cases: list[dict[str, Any]], case_file: str) -> dict[str, Any]:
    """The selection itself, separated so it can be tested with more than one case.

    With a single active case, "find by `case_file`" and "take index 0" cannot be
    told apart -- a probe that swapped one for the other stayed green. Splitting
    the lookup out is what makes the fix falsifiable today rather than on the day
    a second case lands.
    """

    matches = [case for case in cases if case["case_file"] == case_file]
    assert len(matches) == 1, f"expected exactly one active case for {case_file}"
    return matches[0]


def test_the_registration_is_found_by_what_it_is_about_not_by_position() -> None:
    """A second active case, placed anywhere but index 0."""

    cases = [
        {"case_file": "regression/cases/some-other-case.json", "negative_fixtures": {}},
        {"case_file": CASE_FILE, "negative_fixtures": {"file": "right one"}},
    ]
    assert select_registration(cases, CASE_FILE)["negative_fixtures"]["file"] == "right one"

    with pytest.raises(AssertionError):
        select_registration(cases[:1], CASE_FILE)


def test_the_fixtures_are_pinned_by_hash_like_the_corpus() -> None:
    registration = registration_for(CASE_FILE)["negative_fixtures"]
    assert registration["file"] == (
        "regression/cases/synthetic-carrier-negatives-2026-09-03.json"
    )
    assert registration["sha256"] == hashlib.sha256(FIXTURES_PATH.read_bytes()).hexdigest()


def test_the_pointers_in_the_fixtures_file_point_at_something() -> None:
    """Two paths nobody verified, either of which a rename would leave dangling.

    `for_case` and `rubric_registry` are written into the artifact as strings.
    The registry the tests actually load is a hardcoded constant, so renaming
    either file left a dangling pointer with the whole suite green (independent
    review 2026-09-03, P3-2).
    """

    document = load(FIXTURES_PATH)
    assert (ROOT / document["for_case"]).exists(), document["for_case"]
    assert document["for_case"] == CASE_FILE
    assert (ROOT / document["rubric_registry"]) == REGISTRY_PATH


def test_nothing_in_the_fixtures_is_real() -> None:
    source = load(FIXTURES_PATH)["source"]
    assert source["system"] == "synthetic"
    assert "No customer, merchant or production data" in source["method"]


@pytest.mark.parametrize(
    "rubric_id",
    [
        item["rubric_id"]
        for item in load(FIXTURES_PATH)["fixtures"]
        if item["decidability"] == "screenable"
    ],
)
def test_the_screen_objects_to_every_violation_and_to_no_compliant_output(
    fixtures: list[dict[str, Any]], rubric_id: str, runner: Any
) -> None:
    """The non-circular half of this batch.

    The screen is blunt on purpose -- "does this contain a digit", "is there
    more than one question mark" -- and was written from the rubric texts, not
    from these strings. It has to catch every violating example (or the rubric
    is not screenable after all) and object to none of the compliant ones (or it
    would be raising false alarms on correct behaviour).
    """

    item = next(entry for entry in fixtures if entry["rubric_id"] == rubric_id)

    for text in item["violating"]:
        assert runner.mechanical_verdict(rubric_id=rubric_id, text=text) == "FAIL", text
    for text in item["compliant"]:
        assert runner.mechanical_verdict(rubric_id=rubric_id, text=text) is None, text


def test_the_screen_says_nothing_about_outputs_that_do_what_the_corpus_asks(
    fixtures: list[dict[str, Any]], runner: Any
) -> None:
    """The test the first round did not have, and the reason it shipped wrong.

    Each sample here is derived from the rubric's **paired `must`** -- written to
    satisfy that criterion as literally as it can be, including any figure the
    criterion forces into the output. The screen must have no opinion about a
    single one of them.

    That rule is what the original scope failed. `SC01-E09-M1` requires reminding
    the customer of the recorded return date; a date is digits; so a `no_figures`
    screen on `SC01-E09-F1` failed every output that did what the corpus asks,
    and the one compliant sample on file passed only by leaving the date out. The
    reviewer measured four more: service hours, a lead time, an escalation SLA, a
    restated period. Meanwhile "押金两百元" -- an actual price in Chinese numerals
    -- said nothing at all (independent review 2026-09-03, P1-1).

    Samples are derived rather than chosen because a hand-picked set is written
    by the same person who wrote the screen, and will share its blind spots.
    """

    for item in fixtures:
        for text in item.get("satisfying_the_paired_must", []):
            verdict = runner.mechanical_verdict(rubric_id=item["rubric_id"], text=text)
            assert verdict is None, (
                f"{item['rubric_id']}: the screen objects to an output written to "
                f"satisfy {item['must_pair']}: {text}"
            )


def test_every_forbid_with_a_text_paired_must_carries_a_derived_sample(
    fixtures: list[dict[str, Any]],
) -> None:
    """No entry may quietly opt out of the check above.

    Without this, the way to make the previous test pass is to delete the
    samples -- which is exactly the failure mode it exists to catch.
    """

    registry = load(REGISTRY_PATH)["rubrics"]
    for item in fixtures:
        if item["decidability"] == "structural":
            continue
        must_pair = item.get("must_pair")
        assert must_pair, f"{item['rubric_id']} has no paired must recorded"
        assert must_pair in registry, must_pair
        assert registry[must_pair]["kind"] == "must"
        assert item["satisfying_the_paired_must"], item["rubric_id"]


def test_the_screen_can_only_fail_never_pass(
    fixtures: list[dict[str, Any]], runner: Any
) -> None:
    """The property that makes it safe to run without a judge.

    A screen that could pass an assertion would be a second evaluator with no
    contract behind it, and `assertion-rubric-v1` is the only thing allowed to
    say an assertion is satisfied. This one either objects or says nothing.
    """

    verdicts = {
        runner.mechanical_verdict(rubric_id=item["rubric_id"], text=text)
        for item in fixtures
        if item["decidability"] != "structural"
        for text in item["violating"] + item["compliant"]
    }
    assert verdicts <= {"FAIL", None}
    assert "PASS" not in verdicts


def test_the_screen_refuses_input_that_is_not_text(runner: Any) -> None:
    """A structural fixture is a dict, and the screen reads strings.

    Passing one used to sail through only because no structural rubric happened
    to be screened; hang one on a screen and the caller got a `TypeError` out of
    `re`'s internals instead of a readable complaint. The old test papered over
    the asymmetry by calling `str()` on one side and not the other (independent
    review 2026-09-03, P3-4).
    """

    with pytest.raises(TypeError, match="decided from the transition"):
        runner.mechanical_verdict(
            rubric_id="SC01-E07-F1", text={"operation": "push"}
        )


def test_the_screen_stays_out_of_what_it_cannot_decide(
    fixtures: list[dict[str, Any]], runner: Any
) -> None:
    """A screen with an opinion about a `judge_only` rubric is a screen guessing.

    `SC01-E02-F1` is the sharp case: a figure in that turn is not a violation --
    quoting is exactly what the turn is for -- so a digit screen would fail
    correct behaviour. The screen is therefore silent there by construction, not
    by luck.
    """

    screened = set(runner.MECHANICAL_SCREENS)
    declared = {
        item["rubric_id"] for item in fixtures if item["decidability"] == "screenable"
    }
    assert screened == declared

    for item in fixtures:
        if item["decidability"] in {"screenable", "structural"}:
            continue
        for text in item["violating"] + item["compliant"]:
            assert runner.mechanical_verdict(rubric_id=item["rubric_id"], text=text) is None, (
                f"{item['rubric_id']} is {item['decidability']} but the screen has an "
                "opinion about it"
            )


def test_how_much_of_this_corpus_needs_a_judge_is_stated_and_pinned(
    fixtures: list[dict[str, Any]],
) -> None:
    """The measurement, pinned so that a change to it has to be deliberate.

    **It moved, and the direction is the point.** The first round said six of ten
    forbids were screenable; an independent review measured four of those six
    screening correct outputs as failures, because a digit screen had been hung
    on criteria that never mention figures. The honest count is **two**: one
    question-mark screen and one figure screen whose paired `must` can be
    satisfied without a figure.

    So: two screenable and **six that cannot say anything until a judge
    exists**. That last number is the one worth watching, and it tripled. The
    measurement's direction was right -- writing negative fixtures is what
    reveals how much of a corpus a judge carries -- but the first answer was
    optimistic by three entries.

    The `structural` pair is gone entirely: user ruling 2026-09-03 deleted both
    rubrics as duplicates of the `goal_frame` block. Eight forbids remain, not
    ten, and the denominator moved with them.
    """

    tally: dict[str, int] = {}
    for item in fixtures:
        tally[item["decidability"]] = tally.get(item["decidability"], 0) + 1
    assert tally == {"screenable": 2, "judge_only": 6}

    meanings = load(FIXTURES_PATH)["decidability_meanings"]
    assert set(meanings) == set(tally)
    assert "may only FAIL, never PASS" in meanings["screenable"]


def test_the_two_structural_rubrics_are_gone_and_left_no_trace(
    fixtures: list[dict[str, Any]],
) -> None:
    """`SC01-E03-F1` and `SC01-E06-F1` were deleted by user ruling 2026-09-03.

    They said nothing about what the customer sees -- they described stack
    operations, which the corpus's own `goal_frame` block already asserts
    exactly. Duplicated coverage in a form that cost a judge call each to
    re-answer a decided question. The independent review verified the
    duplication field by field and supported deleting them.

    What they covered did **not** go away; see
    `test_the_transitions_those_deleted_rubrics_described_are_still_asserted`
    in the corpus tests. This one only checks the deletion is complete: a rubric
    half-removed is worse than one left alone.
    """

    registry = load(REGISTRY_PATH)["rubrics"]
    assert "SC01-E03-F1" not in registry
    assert "SC01-E06-F1" not in registry
    assert not [item for item in fixtures if item["decidability"] == "structural"]
    assert "structural" not in load(FIXTURES_PATH)["decidability_meanings"]


def test_a_screen_fail_short_circuits_and_a_none_falls_through(runner: Any) -> None:
    """The composition rule, which used to exist only as an intention.

    Nothing said who wins between the screen and the decision table, whether a
    screen `FAIL` skips the judge, or where `None` lands. `screened_verdict`
    states it: the screen only ever subtracts, `None` means no opinion rather
    than a pass, and a rubric outside `MECHANICAL_SCREENS` is never screened
    (independent review 2026-09-03, P2-2).
    """

    passing = {"rubric_id": "SC01-E07-F1", "verdict": "ABSENT"}

    # Without the screen this is a PASS; the screen's objection is final.
    assert (
        runner.screened_verdict(
            kind="forbid",
            requested_rubric_id="SC01-E07-F1",
            response=passing,
            output_text="回到刚才的借用，押金还是 200。",
        )
        == "FAIL"
    )
    # Silence is not a pass, and it is not a failure either -- the table decides.
    assert (
        runner.screened_verdict(
            kind="forbid",
            requested_rubric_id="SC01-E07-F1",
            response=passing,
            output_text="回到刚才的借用吧？",
        )
        == "PASS"
    )
    # No judge at all: the table reads UNKNOWN and fails, which is the contract's
    # rule rather than a default chosen by the screen.
    assert (
        runner.screened_verdict(
            kind="forbid",
            requested_rubric_id="SC01-E07-F1",
            response=None,
            output_text="回到刚才的借用吧？",
        )
        == "FAIL"
    )


def test_no_input_lets_the_screen_turn_an_assertion_green(runner: Any) -> None:
    """The invariant the earlier test could not reach.

    `test_the_screen_can_only_fail_never_pass` inspected the screen's own return
    values, so it could only have gone red if somebody typed the literal
    `"PASS"` into it. What actually matters is that no path through the
    composition produces a `PASS` the decision table did not produce -- checked
    here across both verdict kinds and both screened rubrics.
    """

    for rubric_id, text in (
        ("SC01-E01-F1", "你想借什么？大概用几天？需要开发票吗？"),
        ("SC01-E07-F1", "回到刚才的借用，押金还是 200。"),
    ):
        for kind, verdict in (("forbid", "ABSENT"), ("must", "PRESENT")):
            composed = runner.screened_verdict(
                kind=kind,
                requested_rubric_id=rubric_id,
                response={"rubric_id": rubric_id, "verdict": verdict},
                output_text=text,
            )
            unscreened = runner.decide(
                kind=kind,
                requested_rubric_id=rubric_id,
                response={"rubric_id": rubric_id, "verdict": verdict},
            )
            assert unscreened == "PASS"
            assert composed == "FAIL", "the screen may subtract, never add"
