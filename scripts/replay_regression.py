#!/usr/bin/env python
"""Replay runner for the ACTIVE regression corpus.

`regression/active-cases.json` has carried `"runner_present": false` and a note
forbidding an executor until the step-5 foundations landed. They have: the
merchant profile **registry** and **resolver** exist under
`services/api/src/fastagent_v2/profiles/`, and the target-stack **write path**
is `worker/target_stack.apply_target_stack_operation`. This module is that
executor.

**What it refuses to do is the point.** The corpus is de-identified production
conversation, and its own manifest says an immutable release id is required
before execution. So this runner is built the other way round from a normal
test harness: it computes what is *blocking* first, and every blocker is a hard
refusal rather than a skip. A replay that quietly ran against a stand-in profile
would produce a green number about a system nobody deployed -- which is the
exact failure this repository keeps finding in its own tests.

Three groups of blockers, all fail-closed:

1. **Corpus integrity** -- the case file's SHA-256 must equal the manifest's.
2. **Execution prerequisites** -- the step-5 foundations must be importable, and
   the registered logical profile must be bound to an immutable release id.
3. **Judgement capability** -- rubric assertions need a judge. Without one every
   rubric verdict is `UNKNOWN`, and `assertion-rubric-v1` maps `UNKNOWN` to
   `FAIL` for both `must` and `forbid`. The runner therefore cannot report a
   pass it did not earn, but it refuses up front rather than emitting a wall of
   failures that look like the corpus is broken.

Today, blocker group 2 stops every case: **no `retired-stage` profile package
exists in this repository** and no release id is bound. That is reported, not
worked around.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: The four source roots, kept identical to `pyproject.toml`'s `pythonpath` and
#: `scripts/ci.sh`'s `PYTHONPATH`. This repository is not installed
#: (`pip install -e .` has never been run), so `fastagent_v2` is importable only
#: where one of those two put it on the path.
#:
#: P1-1, independent review 2026-09-03: without this, running the CLI directly
#: reported three `FOUNDATION_MISSING` blockers -- **contradicting the manifest
#: note written in the same commit**. The probe below claimed an `ImportError`
#: was "the honest answer to: has step 5 landed?"; what it actually answered was
#: whether `sys.path` had been configured. The tests could never catch it,
#: because pytest configures the path itself. That failure lands squarely on
#: this batch's own thesis: a refusal has to be true.
SOURCE_ROOTS = (
    "packages/runtime/src",
    "services/api/src",
    "services/worker/src",
    "services/ingress/src",
)


def bootstrap_source_path() -> None:
    """Put the four source roots on `sys.path`, ahead of anything else.

    Idempotent, and never removes anything. Called before the foundation probes
    so that "can this import?" is a question about the repository rather than
    about how the operator happened to invoke the script.
    """

    for relative in reversed(SOURCE_ROOTS):
        root = str(REPOSITORY_ROOT / relative)
        if root not in sys.path:
            sys.path.insert(0, root)
REGRESSION_ROOT = REPOSITORY_ROOT / "regression"
MANIFEST_PATH = REGRESSION_ROOT / "active-cases.json"
RUBRIC_CONTRACT_PATH = REGRESSION_ROOT / "assertion-rubric-v1.contract.json"
EVIDENCE_REGISTRY_PATH = REGRESSION_ROOT / "assertion-evidence-registry.json"

#: The three names `active-cases.json` lists under `step_5_foundations`, mapped
#: to something checkable. A foundation "landing" has to mean an import that
#: succeeds, not a line in a handoff document.
FOUNDATION_PROBES: dict[str, tuple[str, str]] = {
    "target_stack_write_path": (
        "fastagent_v2.worker.target_stack",
        "apply_target_stack_operation",
    ),
    "merchant_profile_registry": ("fastagent_v2.profiles.registry", "ProfileRegistry"),
    "merchant_profile_resolver": ("fastagent_v2.profiles.resolver", "ProfileResolver"),
}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason execution may not proceed. Never a warning, never a skip."""

    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class Preconditions:
    blockers: tuple[Blocker, ...] = field(default_factory=tuple)

    @property
    def clear(self) -> bool:
        return not self.blockers


class ReplayRefused(RuntimeError):
    """Raised instead of executing. Carries every blocker, not just the first.

    Reporting one at a time would make this look like a queue of small problems
    to work through; the useful answer to "can the corpus run?" is the whole
    list at once.
    """

    def __init__(self, preconditions: Preconditions) -> None:
        super().__init__(
            "replay refused; "
            + "; ".join(str(blocker) for blocker in preconditions.blockers)
        )
        self.preconditions = preconditions


class RubricJudge(Protocol):
    """The semantic half of an assertion.

    Deliberately a port with no default implementation in this module. A stub
    that returned `PRESENT` would turn the whole corpus green, and a stub that
    returned `UNKNOWN` would be indistinguishable from a judge that is merely
    broken. Absence is the honest state, and `preconditions()` reports it.
    """

    def verdict(
        self,
        *,
        rubric_id: str,
        criterion: str,
        kind: str,
        evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def active_case_blockers(root: Path = REGRESSION_ROOT) -> tuple[Blocker, ...]:
    """An empty active set is a refusal, not a clean bill of health.

    Measured on 2026-09-03, before this existed: once ADR-0039 retired the only
    registered case, `active_cases` became `[]` and **every other gate went
    quiet** -- `corpus_blockers` had no file to hash, `release_binding_blockers`
    had no profile to demand, and `preconditions().clear` came back `True`. The
    runner would have reported nothing blocking a replay of nothing.

    That is the vacuous-truth shape this repository keeps finding in its own
    tests: zero cases all passing reads exactly like every case passing. The
    manifest still calls the replay mandatory after step 5, and "mandatory" has
    to survive the set being empty, or it stops meaning anything precisely when
    someone has just removed the last item.
    """

    manifest = load_json(root / "active-cases.json")
    if manifest.get("active_cases"):
        return ()
    archived = len(manifest.get("archived_cases", []))
    return (
        Blocker(
            "NO_ACTIVE_CASES",
            f"the manifest registers no active case ({archived} archived); there is nothing "
            "to replay, which is not the same as nothing being wrong. Register a carrier "
            "before asking for a run",
        ),
    )


def corpus_blockers(root: Path = REGRESSION_ROOT) -> tuple[Blocker, ...]:
    """Content addressing, checked rather than trusted.

    The manifest pins the case file by SHA-256 because the corpus is evidence:
    an assertion set that drifted from the annotation a user signed off on is no
    longer the thing that was approved.

    P3-1: `case_file` is resolved against `root`'s parent, not the module-level
    repository root. The manifest's paths are repository-relative, so pointing
    this function at a copied corpus tree used to check that copy's manifest
    against the *repository's* case file -- a half-wired parameter, which reads
    as configurable while quietly ignoring what it was given.
    """

    manifest = load_json(root / "active-cases.json")
    base = root.parent
    blockers: list[Blocker] = []
    for entry in manifest["active_cases"]:
        case_path = base / entry["case_file"]
        if not case_path.is_file():
            blockers.append(Blocker("CASE_FILE_MISSING", entry["case_file"]))
            continue
        digest = hashlib.sha256(case_path.read_bytes()).hexdigest()
        if digest != entry["sha256"]:
            blockers.append(
                Blocker(
                    "CASE_FILE_HASH_MISMATCH",
                    f"{entry['case_file']} is {digest}, manifest pins {entry['sha256']}",
                )
            )
    return tuple(blockers)


def foundation_blockers() -> tuple[Blocker, ...]:
    """Import-probe each declared foundation.

    An `ImportError` here is the honest answer to "has step 5 landed?"; a
    handoff note claiming it has is not.
    """

    import importlib

    bootstrap_source_path()
    blockers: list[Blocker] = []
    for name, (module_name, attribute) in FOUNDATION_PROBES.items():
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            # One code, with the remediation in the detail. An earlier version of
            # this batch split it into `ENVIRONMENT_NOT_PREPARED` versus
            # `FOUNDATION_MISSING` -- and the repair round's own reverse probe
            # found that split had **no test behind it**: collapsing the two back
            # into one left every test green. After `bootstrap_source_path()` the
            # environment branch is unreachable in any layout this repository
            # ships, so it was an untested branch of exactly the shape P2-1 had
            # just been fixed for. Removed rather than given a contrived test.
            blockers.append(
                Blocker(
                    "FOUNDATION_MISSING",
                    f"{name}: {error}. If this names `fastagent_v2` itself, the source "
                    "roots are not where this script expects them; run it from the "
                    "repository, or set PYTHONPATH as scripts/ci.sh does.",
                )
            )
            continue
        except Exception as error:  # noqa: BLE001 - see below
            # P3-3. A foundation module that fails at import for any other
            # reason (missing configuration raising at module scope, say) used
            # to take the whole CLI down with a traceback, which contradicts
            # `ReplayRefused`'s promise to report every reason at once.
            blockers.append(
                Blocker("FOUNDATION_MISSING", f"{name}: {type(error).__name__}: {error}")
            )
            continue
        if not hasattr(module, attribute):
            blockers.append(
                Blocker("FOUNDATION_MISSING", f"{name}: {module_name} has no {attribute}")
            )
    return tuple(blockers)


def release_binding_blockers(
    *, release_ids: Mapping[str, str] | None, root: Path = REGRESSION_ROOT
) -> tuple[Blocker, ...]:
    """Every logical profile the corpus names must map to an immutable release id.

    `release_ids` comes from the operator, because the binding lives in the
    database rather than in this repository -- the registry publishes a release
    and the resolver reads it back by identity.

    **This check is narrower than it looks, and the narrower reading is the true
    one** (P2-3, independent review 2026-09-03). It verifies only that a binding
    was *declared*; it does not verify that the release id exists, is immutable,
    or belongs to that logical profile. Any non-empty string clears it. An
    earlier version of this docstring said "the runner refuses to guess", which
    overstated what the code does -- it refuses to *invent* a binding, but it
    takes the operator's word for the one it is handed.

    Verifying for real means asking the resolver, which means a database, and
    `run()` stops at `NotImplementedError` long before that matters. **The
    verification is therefore owed by the execution loop, not by this
    function** -- see the TODO beside that raise. What must not happen is the
    loop landing while this gate is trusted as though it had already checked.
    """

    manifest = load_json(root / "active-cases.json")
    supplied = dict(release_ids or {})
    blockers: list[Blocker] = []
    for entry in manifest["active_cases"]:
        constraint = entry["target_profile_release_constraint"]
        logical = str(constraint["logical_profile"])
        if not constraint.get("immutable_release_id_required_before_execution", True):
            continue
        if logical not in supplied:
            blockers.append(
                Blocker(
                    "RELEASE_NOT_BOUND",
                    f"logical profile {logical!r} has no immutable release id; "
                    f"publish one and pass --release {logical}=<uuid>",
                )
            )
    return tuple(blockers)


def preconditions(
    *,
    judge: RubricJudge | None = None,
    release_ids: Mapping[str, str] | None = None,
    root: Path = REGRESSION_ROOT,
) -> Preconditions:
    blockers = [
        *active_case_blockers(root),
        *corpus_blockers(root),
        *foundation_blockers(),
        *release_binding_blockers(release_ids=release_ids, root=root),
    ]
    if judge is None:
        blockers.append(
            Blocker(
                "NO_RUBRIC_JUDGE",
                "rubric assertions need a judge; without one every verdict is UNKNOWN, "
                "which assertion-rubric-v1 maps to FAIL for both must and forbid",
            )
        )
    return Preconditions(tuple(blockers))


#: `forbid` rubrics a deterministic screen can object to, and what it looks for.
#:
#: **The screen may only FAIL.** It never returns a pass, so it cannot be used to
#: turn a corpus green -- an output it does not object to is simply one it has no
#: opinion about, and the judge still decides. That asymmetry is the whole reason
#: it is safe to run before a judge exists.
#:
#: Why it exists at all: `assertion-rubric-v1` maps `UNKNOWN` to `FAIL` for both
#: kinds, so with no judge every assertion fails and the run says nothing. The
#: screen carves out the subset whose clear violations need no semantics -- which
#: also measures how much of the corpus a judge is genuinely load-bearing for.
#:
#: **It used to screen five more, and it was wrong about four of them**
#: (independent review 2026-09-03, P1-1). `no_figures` fails on any digit, and it
#: was hung on rubrics whose criteria never mention figures at all. The sharpest
#: case was `SC01-E09-F1`, which forbids a *new price promise* in a follow-up
#: while its paired `SC01-E09-M1` **requires** reminding the customer of the
#: recorded return date -- so every output that actually did what the corpus asks
#: was screened FAIL, and the one compliant sample on file passed only by leaving
#: the date out. The reviewer's other measured misfires: business hours
#: "9:00-18:00", a lead time of "3 个工作日", an escalation SLA of "1 小时内".
#: Meanwhile "押金两百元，租金每天三十元" -- Chinese numerals, an actual price --
#: sails through. A blunt screen is fine; a blunt screen pointed at a criterion it
#: does not implement is a screen failing correct behaviour.
#:
#: What is left is the two whose criterion text *is* the screen's rule:
#: `SC01-E01-F1` ("Do not ask more than one question in a single turn") and
#: `SC01-E07-F1` ("Do not restate or revise any figure while proposing the
#: return") -- the only `no_figures` entry whose paired `must` can be satisfied
#: without stating a figure.
MECHANICAL_SCREENS: dict[str, str] = {
    "SC01-E01-F1": "one_question",
    "SC01-E07-F1": "no_figures",
}

#: A digit run of one or more, which is all "did this output state a figure?"
#: needs. Deliberately blunt: a screen that tried to recognise prices would start
#: having opinions, and an opinionated screen that may only FAIL is a screen that
#: fails things it misunderstood. **Bluntness is only safe where the rubric
#: forbids every figure**; see `MECHANICAL_SCREENS`.
_FIGURE = re.compile(r"[0-9]")

#: Both question marks, because the corpus is Chinese and a full-width `？` is
#: the one a model actually emits.
_QUESTION = re.compile(r"[?？]")


def mechanical_verdict(*, rubric_id: str, text: str) -> str | None:
    """`"FAIL"` when the screen plainly objects, `None` when it has no opinion.

    Never returns `"PASS"`. See `MECHANICAL_SCREENS` for what it screens and why
    that list is short.

    Non-`str` input is rejected rather than coerced. The `structural` fixtures
    carry dicts, and the only reason passing one here never raised was that no
    structural rubric happened to be screened; hang one on a screen and a caller
    would have got a `TypeError` from deep inside `re` instead of a readable
    complaint (independent review 2026-09-03, P3-4).
    """

    if not isinstance(text, str):
        raise TypeError(
            f"the screen reads text; {rubric_id} was given {type(text).__name__}. "
            "Structural rubrics are decided from the transition, not from a string."
        )
    screen = MECHANICAL_SCREENS.get(rubric_id)
    if screen == "no_figures" and _FIGURE.search(text):
        return "FAIL"
    if screen == "one_question" and len(_QUESTION.findall(text)) > 1:
        return "FAIL"
    return None


def decide(
    *,
    kind: str,
    requested_rubric_id: str,
    response: Mapping[str, Any] | None,
) -> str:
    """Apply `assertion-rubric-v1`'s decision table. Everything unclear is FAIL.

    The table is small and its asymmetry is the whole design:

    * `must`   -- PRESENT passes, ABSENT and UNKNOWN fail;
    * `forbid` -- ABSENT passes, PRESENT and UNKNOWN fail.

    So `UNKNOWN` fails *both* kinds. That is not a conservative default, it is
    the contract's stated rule: evidence that is missing, ambiguous, incomplete
    or inaccessible cannot demonstrate either that a behaviour happened or that
    it did not, and a corpus assertion whose evidence nobody could read has not
    been evaluated.

    The correlation rule matters as much: a response whose `rubric_id` does not
    equal the requested one is FAIL, never silently accepted. Judges are asked
    about many criteria and a mismatched answer is an answer to a different
    question.
    """

    if kind not in {"must", "forbid"}:
        return "FAIL"
    if not isinstance(response, Mapping):
        return "FAIL"
    if response.get("rubric_id") != requested_rubric_id:
        return "FAIL"
    verdict = response.get("verdict")
    if verdict not in {"PRESENT", "ABSENT", "UNKNOWN"}:
        return "FAIL"
    if kind == "must":
        return "PASS" if verdict == "PRESENT" else "FAIL"
    return "PASS" if verdict == "ABSENT" else "FAIL"


def screened_verdict(
    *,
    kind: str,
    requested_rubric_id: str,
    response: Mapping[str, Any] | None,
    output_text: str | None,
) -> str:
    """How the screen and the decision table compose. Three rules, all one-way.

    Left undefined, this was the interesting part of the batch that nobody had
    written down: who wins, whether a screen `FAIL` short-circuits, and where
    `None` lands (independent review 2026-09-03, P2-2). The rules:

    1. **The screen only ever subtracts.** Its `"FAIL"` is final for that
       assertion and skips the judge call -- an output that plainly breaks the
       rubric does not need semantics to confirm it.
    2. **`None` means no opinion, not a pass.** It falls straight through to
       `decide()`, which is the only thing allowed to say `"PASS"`. With no judge
       the response is `None`, `decide()` reads `UNKNOWN` and returns `"FAIL"`;
       that is `assertion-rubric-v1`'s rule, not a default chosen here.
    3. **A screen is never consulted for a rubric outside `MECHANICAL_SCREENS`**,
       and `output_text` of `None` (structural rubrics, where the evidence is a
       transition rather than a string) skips it too.

    The invariant worth stating plainly: **there is no path through this function
    where the screen turns a `FAIL` into a `PASS`.** It is enforced by
    construction -- the screen's own return type has no `"PASS"` in it -- and
    asserted in `tests/contracts/test_carrier_negative_fixtures.py`.
    """

    if output_text is not None and requested_rubric_id in MECHANICAL_SCREENS:
        if mechanical_verdict(rubric_id=requested_rubric_id, text=output_text) == "FAIL":
            return "FAIL"
    return decide(kind=kind, requested_rubric_id=requested_rubric_id, response=response)


def registered_evidence_paths(root: Path = REGRESSION_ROOT) -> frozenset[str]:
    registry = load_json(root / "assertion-evidence-registry.json")
    return frozenset(registry["paths"])


def unregistered_input_paths(case: Mapping[str, Any], *, root: Path = REGRESSION_ROOT) -> list[str]:
    """Assertions may only read evidence the registry names.

    An assertion that reads an unregistered path is reading something whose
    meaning was never agreed, and the judge would be answering about evidence
    the corpus does not define.
    """

    registered = registered_evidence_paths(root)
    unknown: list[str] = []
    for case_entry in case["cases"]:
        for event in case_entry["events"]:
            for kind in ("must", "forbid"):
                for assertion in event["expected"].get(kind, []):
                    for path in assertion["predicate"]["input_paths"]:
                        if path not in registered:
                            unknown.append(f"{assertion['id']}:{path}")
    return unknown


def structural_expectations(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The half of every event's expectation that needs no judge.

    `goal_frame` is a claim about the target stack -- which frame was on top
    before, which operation ran, what the stack looked like afterwards. All of
    it is readable straight off the aggregate, so it is checked by comparison
    and never sent to a model.

    `phase` is deliberately excluded. It is corpus annotation describing where
    the conversation was, and the engine has no phase concept to compare it
    against; asserting it would be asserting the annotation against itself.
    """

    rows: list[dict[str, Any]] = []
    for case_entry in case["cases"]:
        for index, event in enumerate(case_entry["events"]):
            frame = event["expected"]["goal_frame"]
            rows.append(
                {
                    "case_id": case_entry["case_id"],
                    "event_index": index,
                    "before_definition_id": frame["before_definition_id"],
                    "operation": frame["operation"],
                    "target_definition_id": frame["target_definition_id"],
                    "after_definition_id": frame["after_definition_id"],
                    "after_stack_definition_ids": list(frame["after_stack_definition_ids"]),
                }
            )
    return rows


def run(
    *,
    judge: RubricJudge | None = None,
    release_ids: Mapping[str, str] | None = None,
    root: Path = REGRESSION_ROOT,
) -> None:
    """Execute the corpus, or refuse with every reason it cannot be executed."""

    state = preconditions(judge=judge, release_ids=release_ids, root=root)
    if not state.clear:
        raise ReplayRefused(state)
    # TODO(execution loop): before driving a single event, verify the supplied
    # release ids through `ProfileResolver` -- existence, immutability, and that
    # each belongs to its logical profile. `release_binding_blockers()` checks
    # only that a binding was declared (P2-3), and that is not enough once
    # something real runs against it.
    raise NotImplementedError(
        "preconditions are clear but the execution loop is not written yet; "
        "per ADR-0039 its first target is a synthetic carrier, not any named merchant"
    )


def _parse_release_arguments(values: Sequence[str]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for value in values:
        logical, separator, release_id = value.partition("=")
        if not separator or not logical or not release_id:
            raise SystemExit(f"--release expects <logical-profile>=<release-id>, got {value!r}")
        bindings[logical] = release_id
    return bindings


def main(argv: Sequence[str] | None = None) -> int:
    """Report every blocker and exit non-zero. **This CLI never executes.**

    There is no `--check` flag any more (P2-2: it did nothing -- output was
    byte-identical with and without it) and no success branch (P2-1: `judge` was
    hard-wired to `None` here, so `preconditions()` always appended
    `NO_RUBRIC_JUDGE`, `state.clear` was never true, and the `return 0` beside
    it was unreachable by construction -- dead code of exactly the kind this
    repository had just finished scanning for).

    A judge is an in-process object; it cannot arrive through a command line. So
    the honest shape of this entry point is a checker: it answers "what is
    stopping the replay?" and always exits non-zero while something is. That
    non-zero matters -- a CI step that treats "the corpus cannot run" as success
    is how a mandatory replay quietly stops being mandatory.
    """

    parser = argparse.ArgumentParser(
        description="Report what is blocking the regression replay. Never executes."
    )
    parser.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="LOGICAL=RELEASE_ID",
        help="declare a logical profile's release id binding (existence is not verified)",
    )
    arguments = parser.parse_args(argv)
    release_ids = _parse_release_arguments(arguments.release)

    state = preconditions(judge=None, release_ids=release_ids)
    print("replay is blocked:")
    for blocker in state.blockers:
        print(f"  - {blocker}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
