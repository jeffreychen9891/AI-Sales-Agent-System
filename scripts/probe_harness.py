#!/usr/bin/env python
"""Reverse-probe harness: the discipline in `AGENTS.md`, enforced by code.

A reverse probe is "undo this guard -> watch the test go red -> restore". The
value is entirely in the *red*, so every way a probe can quietly not-be-red is a
way it lies. In two days of use, three distinct ones showed up, and none of them
looked like a failure at the time:

1. **The anchor did not match.** The script printed one line and carried on. No
   failure, no red -- indistinguishable at a glance from "the mutation went in
   and the test held".
2. **The anchor matched one of several same-kind targets.** A count gate has two
   window filters; mutating the daily one left the conversation one intact, and
   a source-text assertion still found what it was looking for. The probe
   *ran*, and still proved nothing.
3. **The probe crashed after mutating.** `subprocess` died decoding pytest's
   output, the script exited, and the working tree kept the mutation. The
   `git status --porcelain` step is what caught it -- the last step before
   commit.
4. **The baseline was already dirty, and it was green.** A probe run killed
   mid-flight (failure 3's other half: killed, so no `finally`) leaves its
   mutation in the tree. The next run's baseline passes -- **it just passes with
   somebody else's mutation in it** -- and every probe after it measures a tree
   that is not the tree being delivered. Measured 2026-09-13: a killed run left
   `pytest.mark.skip` on one file plus a two-column swap in another; the next
   baseline read `136 passed, 2 skipped` and was accepted as green. It was only
   caught two probes later, when an anchor matched 0 places. **Nothing about a
   green baseline says the baseline is clean** -- `AGENTS.md` step 5
   (`git status --porcelain` empty) covers what a probe leaves behind, and there
   was no check at all on what it starts from.

So this module makes the four impossible rather than remembered:

* every mutation declares how many places it expects to hit, and a mismatch
  **raises before anything is written**;
* restore happens in `finally`, so a crashed probe restores;
* the session verifies the tree is byte-identical to its backups afterwards, and
  says so out loud;
* the baseline is asserted by **count**, not by colour (`assert_baseline`) --
  the caller states how many tests must pass and that none may skip, so a tree
  carrying a leftover mutation fails before any probe runs.

There is a fifth rule that cannot be enforced here, only offered: artifacts
carrying their own hash (`content_hash`, `sha256`) need re-addressing after
mutation, or every probe trips the hash guard first and proves nothing about the
guard it was aiming at. `repin` is the hook for that.

## The sixth: **one file cannot answer "what else did this hit"**

Every probe written up to 2026-09-15 ran `pytest <the batch's own test file>` and
nothing else. That makes the probe's headline claim -- "the red one is *this*
assertion" -- **unfalsifiable by construction**: a run restricted to one file
cannot see a guard in another file, so it cannot tell

* **"this mutation is caught here **and nowhere else**"** from
  **"this mutation was already caught three other places"**, nor
* **"it went red for the reason stated"** from **"it went red because the
  mutated line raises at import/run time and takes a swathe of unrelated tests
  with it"**.

Both happened, and both were found by a reviewer running the control by hand
(`docs/handoffs/roadmap-round-1-2-handoff-2026-09-15.md` §二, findings #3/#5).
Re-measured on `88882184` across all 45 mutations of the nine 2026-09-15 probes:
**13 of them had collateral nobody had declared**, and three probes' stated
conclusions did not survive it --

* `probe_confidence_assessment` F1/F2 each take out **18** further tests: the
  injected read is `{}["calibrated_score"]`, a `KeyError` at run time. They go
  red, but not because "somebody read the field";
* `probe_compensation_chain` C2/C4 are each already caught by
  `test_a_new_status_in_the_migration_lands_in_neither_set`, so the hole the
  batch said it was closing had a guard on it already;
* `probe_topic_slot_vocabulary` V3 -- the line that probe calls its most
  important -- is declared **green on purpose** ("edit one sentence of `goal`,
  nine tests do not move"). Outside its own file it is **84 failed and 11
  errors**: it edits a shipped profile without `repin`, so what it measured was
  the content-hash guard of rule five, which its own file does not collect.

So `ProbeSession` now takes a **second runner**. `control` runs the whole suite
with the probe's own file(s) excluded, and `probe()` reports both. A probe
declares two sets per mutation -- what goes red in its own file, and what else
goes red anywhere -- and both are checked by equality, not containment.
`AGENTS.md` step 3 carries the discipline; this module is what makes it possible
to execute.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType


class ProbeError(RuntimeError):
    """A probe that cannot be trusted. Never raised for a probe that went red."""


@dataclass(frozen=True, slots=True)
class Mutation:
    """One textual edit, with the number of places it is expected to land.

    `expected_hits` has no "any" value on purpose. Saying `2` is a claim that
    there are two same-kind targets and both matter; saying nothing means one.
    Failure 2 above happened because nobody had to state a number.
    """

    path: Path
    old: str
    new: str
    expected_hits: int = 1

    def apply(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        hits = text.count(self.old)
        if hits != self.expected_hits:
            raise ProbeError(
                f"{self.path.name}: anchor matched {hits} place(s), probe expects "
                f"{self.expected_hits}. Nothing was written. Either the anchor is "
                f"stale, or there are more same-kind targets than the probe accounts "
                f"for -- a partial mutation proves nothing."
            )
        self.path.write_text(text.replace(self.old, self.new), encoding="utf-8", newline="\n")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    label: str
    output: str
    #: True when the probe did what a probe is for.
    went_red: bool
    #: The control run's summary, or None when no control runner was declared.
    #: **None is not the same as "nothing else went red"** -- see `collateral`.
    control_output: str | None = None
    control_went_red: bool | None = None

    def __str__(self) -> str:
        verdict = "RED" if self.went_red else "**GREEN -- this probe proved nothing**"
        line = f"{self.label}: {verdict}\n    {self.output.strip()}"
        if self.control_output is not None:
            line += f"\n    control: {self.control_output.strip()}"
        return line

    @property
    def failed(self) -> list[str]:
        """The test names in this probe's own-file run."""

        return failed_names(self.output)

    @property
    def collateral(self) -> list[str]:
        """The test names the control run reported -- **everything else it hit**.

        Empty list and "no control was declared" are different states and must
        not read alike, so this raises rather than returning `[]` when there was
        no control run. A probe that never ran the control has not measured its
        collateral; it has only failed to look.
        """

        if self.control_output is None:
            raise ProbeError(
                f"{self.label}: no control run, so its collateral is unmeasured. "
                "Pass `control=` to ProbeSession (module docstring, failure 6) -- "
                "an empty list would claim something this probe did not check."
            )
        return failed_names(self.control_output)


@dataclass
class ProbeSession:
    """Owns the backups. Every probe restores from them, including on failure.

    Backups live outside the repository (`AGENTS.md` step 1): a backup inside
    the tree is one more thing a careless restore can leave behind.
    """

    paths: Sequence[Path]
    run: Callable[[], tuple[str, bool]]
    #: The second runner: the whole suite **minus this probe's own file(s)**.
    #:
    #: Optional only so that probes predating 2026-09-16 keep working while they
    #: are converted one at a time. It is not optional in the discipline:
    #: `tests/contracts/test_every_probe_declares_its_collateral.py` holds the
    #: set of probes that pass one, so dropping it from a probe that has it goes
    #: red, and a new probe that never had one has to be added to a table by
    #: hand rather than sliding in.
    control: Callable[[], tuple[str, bool]] | None = None
    _backups: dict[Path, Path] = field(default_factory=dict, init=False)
    _directory: Path | None = field(default=None, init=False)

    def __enter__(self) -> ProbeSession:
        self._directory = Path(tempfile.mkdtemp(prefix="probe-backup-"))
        for index, path in enumerate(self.paths):
            backup = self._directory / f"{index}-{path.name}"
            shutil.copy2(path, backup)
            self._backups[path] = backup
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.restore()
        self.verify_restored()
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)

    def restore(self) -> None:
        for path, backup in self._backups.items():
            shutil.copy2(backup, path)

    def verify_restored(self) -> None:
        """Byte-for-byte, not "looks fine".

        `AGENTS.md` step 5 asks for `git status --porcelain` to be empty. This is
        the same check without needing git, and it runs whether the session ended
        normally or by exception.
        """

        drifted = [
            path.name
            for path, backup in self._backups.items()
            if path.read_bytes() != backup.read_bytes()
        ]
        if drifted:
            raise ProbeError(
                f"restore left these files different from their backups: {drifted}. "
                "Do not commit; find the difference first."
            )

    def assert_baseline(
        self,
        *,
        passed: int,
        skipped: int = 0,
        control_passed: int | None = None,
        control_skipped: int = 0,
    ) -> str:
        """Run the suite once **before any mutation** and hold it to a count.

        **Green is not the assertion; the number is.** Failure 4 in the module
        docstring is the whole reason this method exists: a killed probe leaves
        its mutation behind, and the next baseline passes *with that mutation in
        it*. Every probe after that measures a tree nobody is delivering, and
        there is no signal at all -- the summary line says `passed` either way.

        `skipped` defaults to 0 and is a parameter rather than a hard 0 because
        some suites legitimately skip (the full gate skips 2). **Declaring the
        number is the point**: `pytest.mark.skip` is how a mutation silences a
        whole file, so an unexplained skip is exactly what this catches.

        **`control_passed` is the same assertion for the control run**, and it
        is required whenever a control runner was declared. Without it the
        control half would be the one thing in this module that reports a colour
        rather than a number -- and the control run is the *bigger* of the two,
        so it is the one where somebody else's leftover mutation hides best.

        Returns the summary line so the caller can print it.
        """

        summary = self._baseline_of(self.run, passed=passed, skipped=skipped, half="baseline")
        if self.control is None:
            if control_passed is not None:
                raise ProbeError(
                    "control_passed was declared but no control runner was passed to "
                    "ProbeSession -- the number would never be checked against anything."
                )
            return summary
        if control_passed is None:
            raise ProbeError(
                "a control runner was declared but `control_passed` was not. "
                "**Declare the number** (module docstring, failure 4 applies to "
                "both halves); the control run is the larger suite and the easier "
                "place for a leftover mutation to sit unnoticed."
            )
        self._baseline_of(
            self.control, passed=control_passed, skipped=control_skipped, half="control baseline"
        )
        return summary

    def _baseline_of(
        self, runner: Callable[[], tuple[str, bool]], *, passed: int, skipped: int, half: str
    ) -> str:
        summary, went_red = runner()
        if went_red:
            raise ProbeError(
                f"{half} is red before any mutation: {summary.strip()}. "
                "Fix the tree first -- every probe below would be measuring a "
                "broken baseline, and a red baseline makes every probe red."
            )
        counted = {
            name: int(value)
            for value, name in re.findall(r"(\d+) (passed|skipped|failed|error|xfailed)", summary)
        }
        if counted.get("passed") != passed or counted.get("skipped", 0) != skipped:
            raise ProbeError(
                f"{half} is {summary.strip()}, but this probe run declares "
                f"passed={passed}, skipped={skipped}. **A green baseline is not a "
                f"clean baseline** -- the usual cause is a killed probe that left "
                f"its mutation in the tree (module docstring, failure 4). "
                f"Check `git status --porcelain` and `git diff` before re-running; "
                f"if the count changed legitimately, update the declared number."
            )
        return summary

    def probe(
        self,
        label: str,
        mutations: Sequence[Mutation],
        *,
        repin: Callable[[], None] | None = None,
    ) -> ProbeResult:
        """Mutate, run, and restore -- restoring even if anything raises.

        `repin` runs after the mutations and before the command, for artifacts
        that carry their own hash. Without it the hash guard fails first and the
        probe measures the hash guard rather than its target.

        **Both runners run under the same mutation**, one after the other,
        before the restore. Running them as two separate probes would mutate
        twice, and two mutations of "the same" edit are two edits until
        something proves otherwise -- which is the whole business of this file.
        """

        try:
            for mutation in mutations:
                mutation.apply()
            if repin is not None:
                repin()
            output, went_red = self.run()
            control_output: str | None = None
            control_went_red: bool | None = None
            if self.control is not None:
                control_output, control_went_red = self.control()
        finally:
            self.restore()
        self.verify_restored()
        return ProbeResult(
            label=label,
            output=output,
            went_red=went_red,
            control_output=control_output,
            control_went_red=control_went_red,
        )


def pytest_runner(
    repository_root: Path, python: Path, targets: Sequence[str]
) -> Callable[[], tuple[str, bool]]:
    """A runner that reports the last summary line and whether anything failed.

    `errors="replace"` is not decoration: pytest echoes corpus text into failure
    output, and on a cp1252 host the reader thread dies, `stdout` comes back
    `None`, and the probe crashes *after* mutating. That is failure 3 above.
    """

    import subprocess

    def run() -> tuple[str, bool]:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(python), "-m", "pytest", *targets, "-q", "--no-header", "-p", "no:cacheprovider"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
        summary = lines[-1] if lines else "(no output)"
        return summary, completed.returncode != 0

    return run


#: `FAILED tests/contracts/x.py::test_name[param]` -- the name without its
#: parameters, because a parametrised case that fails eight times is one guard
#: failing, not eight.
_FAILED_LINE = re.compile(r"^FAILED (?P<target>\S+?)::(?P<name>[\w\[\]\-/.]+)", re.MULTILINE)

#: `ERROR tests/contracts/x.py - KeyError: 'maxLength'` -- a module that did not
#: **import**, so none of its tests ran and none of them are named on a `FAILED`
#: line.
#:
#: **This is the empty-set failure again, one level down.** Measured 2026-09-16:
#: `probe_payload_bounds` W4 deletes a `maxLength` from a contract, and a test
#: module that read that key at import time died collecting. pytest said
#: `1 error`, printed no `FAILED`, and the probe's collateral set came back `[]`
#: -- **byte-for-byte what "nothing else went red" looks like**, for a mutation
#: that had just broken a whole file.
#:
#: So a collection error gets a name too. It will never match a declared set, so
#: it goes red, and the name says what to look at.
_ERROR_LINE = re.compile(r"^ERROR (?P<target>\S+)", re.MULTILINE)

#: How `suite_runner` spells the name list into its summary, and the only thing
#: `failed_names` knows how to read back. One spelling, one reader.
_FAILED_TAIL = "FAILED: "

#: Printed where a name list would go when nothing failed. It is a word rather
#: than `[]` so that a summary line reads the same to a person and to
#: `failed_names`, which returns `[]` for it either way.
NOTHING_FAILED = "（无）"

#: Prefix for a module that failed to collect. It is deliberately not a valid
#: test name: nobody should be able to declare it by copying a name they saw.
COLLECTION_ERROR = "collection-error:"


def failed_names(output: str) -> list[str]:
    """The test names out of a `suite_runner` summary.

    **This used to exist nine times**, once per 2026-09-15 probe, as a private
    `_failed_names` beside a private copy of the runner that produced the string
    it parsed. Writer and reader were duplicated together, which is the shape
    that hides drift best: change the format in one probe and only that probe's
    reader keeps up, and every other probe still parses -- just something else.
    """

    # `:` is in the class so that `collection-error:test_x.py` survives the
    # round trip through the summary string.
    return re.findall(r"'([\w\[\]\-/.:]+)'", output.split(_FAILED_TAIL, 1)[-1])


def suite_runner(
    repository_root: Path,
    python: Path,
    targets: Sequence[str],
    *,
    ignore: Sequence[str] = (),
) -> Callable[[], tuple[str, bool]]:
    """`pytest_runner` plus the names of what failed, and an `--ignore` list.

    The summary is `"<pytest's last line> | FAILED: [...]"` -- the format the
    nine 2026-09-15 probes each built for themselves. It lives here now because
    the control run has to produce the *same* string the own-file run does:
    two formats would mean two parsers, and a probe comparing two sets parsed by
    two readers is comparing the readers as much as the runs.

    `--tb=no` is deliberate. A probe wants the summary and the names; a
    traceback for eighty failing tests is megabytes of output whose only effect
    is to make the cp1252 decode path (failure 3) more likely to be exercised.
    """

    import subprocess

    argv = [str(python), "-m", "pytest", *targets]
    argv += [f"--ignore={path}" for path in ignore]
    argv += ["-q", "--no-header", "--tb=no", "-p", "no:cacheprovider"]

    def run() -> tuple[str, bool]:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = completed.stdout or ""
        failed = {m.group("name").split("[")[0] for m in _FAILED_LINE.finditer(stdout)}
        # A module that never imported names nothing; give it a name anyway.
        broken = {
            f"{COLLECTION_ERROR}{m.group('target').split('/')[-1]}"
            for m in _ERROR_LINE.finditer(stdout)
        }
        names = sorted(failed | broken)
        lines = [line for line in stdout.splitlines() if line.strip()]
        last = lines[-1] if lines else "(no output)"
        return f"{last} | {_FAILED_TAIL}{names or NOTHING_FAILED}", completed.returncode != 0

    return run


def control_runner(
    repository_root: Path,
    python: Path,
    own_targets: Sequence[str],
    *,
    scope: str = "tests/contracts",
) -> Callable[[], tuple[str, bool]]:
    """The second runner: **`scope`, minus the files this probe is about**.

    Excluding the probe's own file rather than running the whole suite is what
    makes the second number answer a different question from the first. Running
    everything would answer "does anything go red", which the own-file run
    already answers and which is not the open question; **"what else"** is.

    `scope` defaults to `tests/contracts` because that is the layer a probe can
    hold to a count -- `tests/integration` and `tests/e2e` need a database, and
    a baseline that cannot be asserted by number is failure 4 waiting to happen.
    A probe whose guard lives outside that layer passes its own `scope` and says
    so; **it does not get to pass none**.
    """

    return suite_runner(repository_root, python, [scope], ignore=own_targets)
