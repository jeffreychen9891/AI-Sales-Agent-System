"""The probe harness has to fail closed, because a probe that lies is worse than none.

Every test here is one of the ways a reverse probe quietly proved nothing.
They are not hypotheticals; each happened, and each looked fine at the time:

1. the anchor did not match and the script carried on;
2. the anchor matched one of two same-kind targets, so the mutation was partial;
3. the probe crashed after mutating and left the working tree changed;
4. **the baseline was already dirty, and it was green** (2026-09-13) -- a run
   killed mid-flight left `pytest.mark.skip` on one file and a two-column swap
   in another, the next baseline read `136 passed, 2 skipped`, and every probe
   after it measured a tree nobody was delivering.

A harness that could still do any of these would be the same defect with more
ceremony.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_harness() -> Any:
    if "probe_harness" in sys.modules:
        return sys.modules["probe_harness"]
    path = ROOT / "scripts" / "probe_harness.py"
    spec = importlib.util.spec_from_file_location("probe_harness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_harness"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guarded(tmp_path: Path) -> Path:
    """A file with **two** same-kind targets, which is the interesting shape."""

    path = tmp_path / "guard.py"
    path.write_text(
        "if value > daily_limit:\n    deny()\nif value > conversation_limit:\n    deny()\n",
        encoding="utf-8",
    )
    return path


def test_a_stale_anchor_raises_and_writes_nothing(guarded: Path) -> None:
    """Failure 1: the anchor did not match.

    The old script printed `anchor not found, skipped` and moved on -- no
    failure, no red, and at a glance identical to "the guard held". Here it is
    an error, and the file is untouched.
    """

    harness = load_harness()
    before = guarded.read_bytes()
    mutation = harness.Mutation(path=guarded, old="if value > nonexistent:", new="if False:")

    with pytest.raises(harness.ProbeError, match="anchor matched 0 place"):
        mutation.apply()
    assert guarded.read_bytes() == before


def test_a_partial_mutation_raises_rather_than_running(guarded: Path) -> None:
    """Failure 2, and the subtle one.

    Two windows, one anchor shape. Mutating "the first `deny()`" leaves the
    other intact, the probe runs, something even goes red -- and the guard it
    was aiming at was never disabled. The count has to be declared, and a
    mismatch stops the probe before anything is written.
    """

    harness = load_harness()
    before = guarded.read_bytes()
    mutation = harness.Mutation(path=guarded, old="    deny()", new="    pass")

    with pytest.raises(harness.ProbeError, match="expects 1"):
        mutation.apply()
    assert guarded.read_bytes() == before

    # Declaring the real number is what makes it legal.
    harness.Mutation(path=guarded, old="    deny()", new="    pass", expected_hits=2).apply()
    assert guarded.read_text(encoding="utf-8").count("    pass") == 2


def test_a_crash_after_mutating_still_restores(guarded: Path) -> None:
    """Failure 3: the tree kept the mutation.

    The old script died decoding subprocess output *after* writing, and only
    `git status --porcelain` caught it -- the last step before commit. Restore
    lives in `finally` now.
    """

    harness = load_harness()
    before = guarded.read_bytes()

    def exploding_run() -> tuple[str, bool]:
        raise UnicodeDecodeError("cp1252", b"\x90", 0, 1, "as it actually happened")

    with harness.ProbeSession(paths=[guarded], run=exploding_run) as session:
        with pytest.raises(UnicodeDecodeError):
            session.probe(
                "crashes",
                [harness.Mutation(path=guarded, old="deny()", new="pass", expected_hits=2)],
            )
        assert guarded.read_bytes() == before


def test_the_session_restores_on_the_way_out_too(guarded: Path) -> None:
    """Even a probe that never ran leaves the tree as it found it."""

    harness = load_harness()
    before = guarded.read_bytes()

    with harness.ProbeSession(paths=[guarded], run=lambda: ("", False)):
        guarded.write_text("something else entirely\n", encoding="utf-8")

    assert guarded.read_bytes() == before


def test_a_green_probe_is_reported_as_proving_nothing(guarded: Path) -> None:
    """The wording matters more than it looks.

    A probe that stays green is not a pass -- it is a probe that told you
    nothing, and the two read identically in a scroll of output unless the tool
    says so. Both of this week's real findings came from probes that were green.
    """

    harness = load_harness()

    with harness.ProbeSession(paths=[guarded], run=lambda: ("5 passed", False)) as session:
        result = session.probe(
            "guard removed",
            [harness.Mutation(path=guarded, old="deny()", new="pass", expected_hits=2)],
        )

    assert result.went_red is False
    assert "proved nothing" in str(result)

    with harness.ProbeSession(paths=[guarded], run=lambda: ("1 failed", True)) as session:
        red = session.probe(
            "guard removed",
            [harness.Mutation(path=guarded, old="deny()", new="pass", expected_hits=2)],
        )
    assert red.went_red is True
    assert "RED" in str(red)


def test_the_repin_hook_runs_after_mutating_and_before_the_command(
    tmp_path: Path,
) -> None:
    """Content-addressed artifacts, the fourth rule.

    Mutating a file that carries its own hash trips the hash guard first, so
    every probe reports red for the wrong reason. `repin` re-addresses the
    artifact in between -- and the order is the whole point, so it is asserted.
    """

    harness = load_harness()
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"value": "one", "hash": "PIN-one"}\n', encoding="utf-8")
    events: list[str] = []

    def repin() -> None:
        text = artifact.read_text(encoding="utf-8")
        value = text.split('"value": "')[1].split('"')[0]
        artifact.write_text(
            text.replace("PIN-one", f"PIN-{value}"), encoding="utf-8"
        )
        events.append("repin")

    def run() -> tuple[str, bool]:
        events.append("run")
        assert "PIN-two" in artifact.read_text(encoding="utf-8"), (
            "the command must see the re-addressed artifact, or it measures the hash guard"
        )
        return "1 failed", True

    with harness.ProbeSession(paths=[artifact], run=run) as session:
        result = session.probe(
            "value changed",
            [harness.Mutation(path=artifact, old='"value": "one"', new='"value": "two"')],
            repin=repin,
        )

    assert events == ["repin", "run"]
    assert result.went_red is True


def test_a_restore_that_did_not_restore_is_an_error(guarded: Path) -> None:
    """The check is on bytes, and it refuses to be optimistic.

    `AGENTS.md` step 5 asks for an empty `git status --porcelain` before
    committing. This is the same question asked without git, and asked
    automatically -- the version a person can forget is the version that failed.
    """

    harness = load_harness()
    session = harness.ProbeSession(paths=[guarded], run=lambda: ("", False))
    session.__enter__()
    try:
        guarded.write_text("drifted\n", encoding="utf-8")
        with pytest.raises(harness.ProbeError, match="different from their backups"):
            session.verify_restored()
    finally:
        session.restore()
        session.__exit__(None, None, None)

    assert guarded.read_text(encoding="utf-8").startswith("if value >")


def test_backups_live_outside_the_tree_being_probed(guarded: Path) -> None:
    """`AGENTS.md` step 1. A backup inside the tree is one more thing a careless
    restore can leave behind -- which is how a batch once lost an uncommitted
    line of `requirements.in` to `git checkout -- .`."""

    harness = load_harness()
    with harness.ProbeSession(paths=[guarded], run=lambda: ("", False)) as session:
        backup = next(iter(session._backups.values()))
        assert guarded.parent not in backup.parents


# --------------------------------------------------------------------------
# Failure 4: a green baseline is not a clean baseline (2026-09-13)
# --------------------------------------------------------------------------


def test_a_baseline_with_an_unexpected_skip_is_refused(guarded: Path) -> None:
    """**This is the one that was missing.**

    The real sequence: a probe run was killed, so its `finally` never ran and
    its `pytest.mark.skip` stayed on a file. The next run's baseline was green
    -- `136 passed, 2 skipped` -- and green was the whole check, so it was
    accepted. Two probes later an anchor matched 0 places, which is the only
    reason anyone noticed.

    A skip is how a mutation silences an entire file, so the count that matters
    most here is `skipped == 0`.

    **The passed count matches on purpose.** The first version of this test used
    the real incident's numbers (`136 passed, 2 skipped` against a declared 138),
    and a reverse probe showed it up: deleting the `skipped` comparison entirely
    left this test **green**, because it was tripping on the passed count the
    whole time. It asserted in its name and its docstring something it never
    measured -- `AGENTS.md`「守卫本身也可能是空的」. So the skip here arrives with
    the declared number of passes beside it, and nothing else to fail on.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("138 passed, 2 skipped in 39.19s", False)
    ) as session:
        with pytest.raises(harness.ProbeError) as raised:
            session.assert_baseline(passed=138)

    assert "not a" in str(raised.value)
    assert "138" in str(raised.value), "the message has to name the declared number"


def test_a_baseline_short_of_the_declared_count_is_refused(guarded: Path) -> None:
    """Fewer passes than declared, with nothing red -- **deselection**.

    `-k` left over from a debugging run, a target path that no longer matches, a
    file renamed: all of them shrink the suite silently. The colour is green in
    every one of those, and the probes below would be measuring whatever subset
    survived.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("12 passed in 1.20s", False)
    ) as session:
        with pytest.raises(harness.ProbeError):
            session.assert_baseline(passed=138)


def test_a_red_baseline_says_so_rather_than_counting(guarded: Path) -> None:
    """A red baseline gets its own message, because its fix is different.

    Counting first would report 「declared 138, got 137」 for what is really
    「something is broken before you started」 -- and then every probe below
    goes red for a reason that has nothing to do with the guard being probed.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("1 failed, 137 passed in 40.31s", True)
    ) as session:
        with pytest.raises(harness.ProbeError, match="red before any mutation"):
            session.assert_baseline(passed=138)


def test_the_declared_baseline_passes_and_returns_the_summary(guarded: Path) -> None:
    """The honest case, including a declared non-zero skip count.

    **`skipped` is a parameter, not a hard 0**: the full gate skips 2 legitimately,
    and a check that cannot express that gets deleted by the first person it
    inconveniences. Declaring the number is what keeps it honest.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("2876 passed, 2 skipped in 502.10s", False)
    ) as session:
        summary = session.assert_baseline(passed=2876, skipped=2)

    assert summary == "2876 passed, 2 skipped in 502.10s"


# ---------------------------------------------------------------------------
# Failure 6: one file cannot answer "what else did this mutation hit".
#
# The four above are about a probe that quietly does not run. These are about a
# probe that runs, goes red, reports a name -- and is still wrong, because the
# name it reports is the only one it was ever in a position to see.
# ---------------------------------------------------------------------------


def test_the_control_runs_under_the_same_mutation_and_is_reported(guarded: Path) -> None:
    """Both runners fire, one mutation, and the result carries both answers.

    The mutation has to be live for the control too. Running the control as a
    second probe would mutate twice, and "the same edit applied twice" is an
    assumption this module exists to refuse.
    """

    harness = load_harness()
    seen: list[str] = []

    def own() -> tuple[str, bool]:
        seen.append(guarded.read_text(encoding="utf-8"))
        return "1 failed, 1 passed in 0.4s | FAILED: ['test_the_gate_denies']", True

    def control() -> tuple[str, bool]:
        seen.append(guarded.read_text(encoding="utf-8"))
        return "2 failed, 99 passed in 9.0s | FAILED: ['test_a', 'test_b']", True

    with harness.ProbeSession(paths=[guarded], run=own, control=control) as session:
        result = session.probe(
            "mutate",
            [harness.Mutation(guarded, "    deny()\n", "    allow()\n", expected_hits=2)],
        )

    assert all("allow()" in text for text in seen), "both runners must see the mutation"
    assert result.failed == ["test_the_gate_denies"]
    assert result.collateral == ["test_a", "test_b"]
    assert result.control_went_red is True
    assert "control:" in str(result)


def test_a_probe_without_a_control_refuses_to_report_an_empty_collateral(
    guarded: Path,
) -> None:
    """**"Nothing else went red" and "nobody looked" must not read alike.**

    Returning `[]` here would be the most expensive kind of wrong this file
    guards: a probe that never ran the control would publish the same empty set
    as one that ran it and found nothing.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("1 failed in 0.1s | FAILED: ['test_x']", True)
    ) as session:
        result = session.probe(
            "no control",
            [harness.Mutation(guarded, "    deny()\n", "    allow()\n", expected_hits=2)],
        )

    assert result.control_output is None
    assert result.failed == ["test_x"]
    with pytest.raises(harness.ProbeError, match="collateral is unmeasured"):
        _ = result.collateral


def test_a_control_without_a_declared_count_is_refused(guarded: Path) -> None:
    """Failure 4 applies to the **larger** half too, so it needs its own number."""

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded],
        run=lambda: ("11 passed in 0.5s | FAILED: （无）", False),
        control=lambda: ("1626 passed in 17.2s | FAILED: （无）", False),
    ) as session:
        with pytest.raises(harness.ProbeError, match="control_passed"):
            session.assert_baseline(passed=11)


def test_a_declared_count_with_no_control_is_refused(guarded: Path) -> None:
    """The other direction: a number nothing would ever check is not a check."""

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded], run=lambda: ("11 passed in 0.5s | FAILED: （无）", False)
    ) as session:
        with pytest.raises(harness.ProbeError, match="no control runner"):
            session.assert_baseline(passed=11, control_passed=1626)


def test_a_dirty_control_baseline_says_which_half_it_was(guarded: Path) -> None:
    """The message has to name the half, or the reader re-runs the wrong one.

    Both halves go through the same counter, and a probe run prints one
    `baseline:` line; without the label, "declares passed=1626" sends somebody to
    look at the own-file number, which is fine.
    """

    harness = load_harness()

    with harness.ProbeSession(
        paths=[guarded],
        run=lambda: ("11 passed in 0.5s | FAILED: （无）", False),
        control=lambda: ("1625 passed in 17.2s | FAILED: （无）", False),
    ) as session:
        with pytest.raises(harness.ProbeError, match="control baseline is 1625 passed"):
            session.assert_baseline(passed=11, control_passed=1626)


def test_the_shared_runner_spells_the_ignore_list_into_its_argv(tmp_path: Path) -> None:
    """`control_runner` is `suite_runner` with the probe's own files excluded.

    Measured through the argv rather than by running pytest: the point of the
    control is *which* files it leaves out, and that is a fact about the command
    line, not about a suite that takes twenty seconds.
    """

    harness = load_harness()
    captured: dict[str, Any] = {}

    class _Completed:
        stdout = "FAILED tests/contracts/test_a.py::test_one[case]\n1 failed in 1.0s"
        returncode = 1

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return _Completed()

    import subprocess

    original = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        runner = harness.control_runner(
            tmp_path, tmp_path / "python.exe", ["tests/contracts/test_own.py"]
        )
        summary, went_red = runner()
    finally:
        subprocess.run = original  # type: ignore[assignment]

    assert "tests/contracts" in captured["argv"]
    assert "--ignore=tests/contracts/test_own.py" in captured["argv"]
    assert went_red is True
    # The parametrisation is dropped: one guard failing eight times is one name.
    assert harness.failed_names(summary) == ["test_one"]


def test_a_module_that_did_not_import_is_named_rather_than_silently_absent(
    tmp_path: Path,
) -> None:
    """A collection error must not come back as an empty collateral set.

    **Measured 2026-09-16, and it was a real regression at the time.** A probe
    deleted a `maxLength` from a contract; a test module read that key at import
    time and died collecting. pytest printed `1 error` and no `FAILED` line, so
    the name set was `[]` -- indistinguishable from "this mutation hit nothing
    else", for a mutation that had just taken out a whole file.
    """

    harness = load_harness()

    class _Completed:
        stdout = (
            "ERROR tests/contracts/test_reads_a_key_at_import.py - KeyError: 'maxLength'\n"
            "1 warning, 1 error in 0.53s"
        )
        returncode = 2

    import subprocess

    original = subprocess.run
    subprocess.run = lambda *a, **k: _Completed()  # type: ignore[assignment]
    try:
        runner = harness.suite_runner(tmp_path, tmp_path / "python.exe", ["tests/contracts"])
        summary, went_red = runner()
    finally:
        subprocess.run = original  # type: ignore[assignment]

    assert went_red is True
    assert harness.failed_names(summary) == [
        f"{harness.COLLECTION_ERROR}test_reads_a_key_at_import.py"
    ], "一个没 import 成的模块必须有名字，否则它与「什么都没红」逐字一样"
    assert harness.NOTHING_FAILED not in summary
