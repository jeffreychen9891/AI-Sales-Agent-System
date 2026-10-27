"""T183: the lease deadline must not carry the offset between two clocks.

**The registered defect, in one sentence.** `lease_expires_at` is an instant on
the database's clock and `datetime.now()` is an instant on this process's;
`_reconcile_before_giving_up` subtracted one from the other, so the answer
carried the offset between them. When the database runs ahead by more than
`LEASE_SAFETY_MARGIN_SECONDS`, the dispatcher believes it still holds a lease
that has already expired -- a second dispatcher then claims the row and the
customer is sent the same message twice.

**T183 was registered on the premise that the two clocks agree**, because worker
and postgres share a host. T186 then measured the database +0.43s ahead of the
process on this deployment, with the margin at 1.0 -- so the premise was already
false when it was written down.

**These cases used to prove nothing** (independent review P2-1). They ran
against a `_lease_deadline` helper defined in *this file*, which built the two
database readings as `db_now` and `db_now + OUTBOX_LEASE_SECONDS` and then
subtracted them -- an identity. Reverting `_claim_one` to the pre-T183
expression left all of them green. They asserted a property of the copy.

They now call `pipeline._lease_deadline`, the function `_claim_one` calls. What
this file still cannot see is which SQL produced the readings; that is pinned by
the source assertions at the bottom, and behaviourally by
`tests/e2e/test_lease_deadline_from_database_clock.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastagent_v2.worker.pipeline import (
    LEASE_SAFETY_MARGIN_SECONDS,
    OUTBOX_LEASE_SECONDS,
    _lease_deadline,
)


def _readings(
    *, skew_seconds: float, transaction_spent_seconds: float = 0.0
) -> tuple[datetime, datetime, datetime]:
    """Model what the database returns, so production can do the arithmetic.

    Returns `(anchor, lease_expires_at, db_now)` -- the three values
    `_claim_one` holds after the claim statement comes back.

    The database's clock runs `skew` ahead of this process's, and the claim
    statement runs `transaction_spent_seconds` after the transaction opened.
    That second parameter is the one the first version of this file had no way
    to express, and it is where the defect lived: `lease_expires_at` is set from
    `now()`, which is the *transaction's* start, so a lease granted at
    transaction start has already been running for that long by the time the
    statement returns.
    """

    anchor = datetime.now(UTC)
    db_transaction_started = anchor + timedelta(seconds=skew_seconds)
    db_now = db_transaction_started + timedelta(seconds=transaction_spent_seconds)
    lease_expires_at = db_transaction_started + timedelta(seconds=OUTBOX_LEASE_SECONDS)
    return anchor, lease_expires_at, db_now


def test_the_derived_deadline_does_not_move_when_the_database_clock_does() -> None:
    """The whole point: the offset cancels.

    Under any skew the derived deadline stays one lease away from *this
    process's* clock, which is the clock the comparison downstream uses.
    """

    baseline = _lease_deadline(*_readings(skew_seconds=0.0))
    for skew in (-5.0, -0.43, 0.0, 0.43, 5.0, 60.0):
        derived = _lease_deadline(*_readings(skew_seconds=skew))
        drift = abs((derived - baseline).total_seconds())
        assert drift < 0.5, f"skew {skew}s moved the derived deadline by {drift}s"


def test_the_old_expression_moved_with_the_database_clock() -> None:
    """The defect, stated so that the fix has something to be better than.

    Without this the case above would pass just as well against the broken
    code -- it asserts a property the old expression never had, and a reader
    would have to take on trust that it ever mattered.
    """

    _, baseline_expiry, _ = _readings(skew_seconds=0.0)
    _, skewed_expiry, _ = _readings(skew_seconds=5.0)
    drift = (skewed_expiry - baseline_expiry).total_seconds()
    assert drift > 4.5, "the database-absolute value must be the one that moves"


def test_time_the_transaction_already_spent_comes_off_the_deadline() -> None:
    """P1-1, as a property rather than a keyword.

    The first fix returned `now()` as the second reading. `now()` is
    `transaction_timestamp()`, and the *same* `now()` sets `lease_expires_at` --
    so the difference was the constant `OUTBOX_LEASE_SECONDS` no matter how long
    the transaction had been running, and the deadline came out
    `anchor + 30s` from an anchor read after `_dead_letter_exhausted` and the
    candidate `SELECT`. It overstated the remaining lease by exactly the time
    those had taken, which is the direction that double-sends, and
    `_dead_letter_exhausted` has no `SKIP LOCKED` -- so under a second
    dispatcher that time is unbounded.

    With `clock_timestamp()` the spent time comes off, and the deadline lands on
    the true expiry instead of past it. This case fails against the first fix.
    """

    spent = 5.0
    anchor, lease_expires_at, db_now = _readings(
        skew_seconds=0.0, transaction_spent_seconds=spent
    )
    remaining = (_lease_deadline(anchor, lease_expires_at, db_now) - anchor).total_seconds()

    assert remaining < OUTBOX_LEASE_SECONDS - spent + 0.5, (
        f"a transaction that had already run {spent}s must not still be handed a "
        f"full lease; got {remaining}s of {OUTBOX_LEASE_SECONDS}"
    )
    assert remaining > OUTBOX_LEASE_SECONDS - spent - 0.5, (
        "and it must not lose more than it spent either"
    )


def test_a_database_clock_ahead_by_more_than_the_margin_used_to_hide_an_expired_lease() -> None:
    """The failure mode in the terms that make it a defect rather than a wobble.

    The comparison is between two expressions, not between a value and itself:
    the pre-T183 one (`lease_expires_at` against this process's clock, offset
    and all) and the one production now computes. The old one overstates the
    remaining lease by the full skew; past the safety margin the dispatcher goes
    on to reconcile and write inside a lease it no longer holds, and the guard
    that makes a lost race survivable is `lease_owner`, checked *after* the send.
    """

    skew = LEASE_SAFETY_MARGIN_SECONDS + 1.0
    anchor, lease_expires_at, db_now = _readings(skew_seconds=skew)

    # Pre-T183: the database's absolute instant, minus this process's clock.
    old_remaining = (lease_expires_at - anchor).total_seconds()
    new_remaining = (_lease_deadline(anchor, lease_expires_at, db_now) - anchor).total_seconds()

    overstatement = old_remaining - new_remaining
    assert overstatement > LEASE_SAFETY_MARGIN_SECONDS, (
        f"the old expression must overstate by more than the margin or the margin "
        f"would absorb it; overstated by {overstatement}s"
    )
    assert abs(new_remaining - OUTBOX_LEASE_SECONDS) < 0.5, (
        "and the new one must be unaffected by the skew"
    )


def test_the_anchor_is_taken_before_the_statement_not_after() -> None:
    """The round trip may only make the deadline earlier, never later.

    This and the case below are the only things tying the *SQL* to the property
    -- the arithmetic cases above are handed their readings, so they cannot see
    which function produced them. They assert spelling, which is weak; the
    behavioural cover is `test_lease_deadline_from_database_clock.py`.
    """

    import inspect

    from fastagent_v2.worker.pipeline import OutboxDispatcher

    source = inspect.getsource(OutboxDispatcher._claim_one)
    anchor = source.index("claim_started_at = datetime.now(UTC)")
    statement = source.index("UPDATE outbox_messages")
    assert anchor < statement, "the process-clock anchor must precede the claim statement"
    assert "RETURNING lease_expires_at, clock_timestamp() AS db_now" in source, (
        "`now()` is the transaction's start, not the statement's -- see P1-1"
    )
    assert "RETURNING lease_expires_at, now() AS db_now" not in source
    assert "_lease_deadline(" in source


def test_no_lease_is_written_from_this_process_clock() -> None:
    """Every lease in the repository, which is what this name claims.

    It used to assert only `InboxProcessor.prepare` while `Scheduler.enqueue_due`
    still wrote `clock + timedelta(seconds=30)` (independent review P2-2). That
    was not a live defect -- one process wrote and compared against its own
    clock, so the offset cancelled -- but the name promised a rule and the body
    checked one site, which is how an exception survives into the next reader's
    copy of it.

    `prepare` stated over the source because the defect is *where a value comes
    from*, not what it evaluates to. `enqueue_due` the same, and note it still
    uses `timedelta` legitimately for the envelope's `runtime_deadline_at` --
    that is a deadline carried in a message, not a lease anyone claims against.
    """

    import inspect

    from fastagent_v2.worker.pipeline import InboxProcessor
    from fastagent_v2.worker.scheduler import Scheduler

    prepare = inspect.getsource(InboxProcessor.prepare)
    assert "lease_expires_at = now() + %s * interval '1 second'" in prepare
    assert "timedelta(" not in prepare, (
        "any process-clock interval here is a new place for the two clocks to "
        "be subtracted, whatever the seconds are spelled as"
    )
    assert "datetime.now(" not in prepare

    enqueue = inspect.getsource(Scheduler.enqueue_due)
    assert "lease_expires_at = now() + %s * interval '1 second'" in enqueue
    assert "lease_expires_at = %s" not in enqueue, (
        "the lease must not be a value this process computed and passed in"
    )
    assert "lease_expires_at < now()" in enqueue, (
        "and the expiry test must be on the same clock that wrote it"
    )
