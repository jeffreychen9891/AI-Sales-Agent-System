#!/usr/bin/env python3
"""Daily account-balance reconciliation (T71, ADR-0041).

Three things, and deliberately nothing else: **ask the supplier for the account
balance, read the ledger's settled sum for the day that just ended, compare the
two.** The balance is the only number the supplier will vouch for. It cannot say
*which* call is wrong -- it is an aggregate -- but it covers every call,
including the two kinds no per-call scheme can ever see because they produce no
response at all: timeouts and transport failures (ADR-0040 measured that; T70
closed the per-call route as impossible on this supplier).

**It alerts. It never stops anything.** ADR-0041: using an aggregate signal to
halt a single conversation would be punishing a named party on evidence that
cannot name anyone. The verdict lands in `provider_balance_snapshots`; the
metrics collector reads it, and a Grafana rule does the shouting.

## The credential, and why this file is written the way it is

The supplier has no read-only key: its whole API surface is chat completions,
list models, get user balance -- no credential management, no scopes (measured
2026-09-03, `docs/reviews/t70-provider-identifier-feasibility-2026-09-03.md`).
So **this job holds a key that can also spend money.** We wanted separation; the
platform does not offer it (ADR-0041 §三). Three compensations, all visible in
the code below rather than promised in a document:

1. **Zero input surface.** `main` refuses to run with *any* argument -- not a
   tenant, not a date, not a dry-run flag. Nothing it queries can be steered
   from outside: the provider is a constant, the day comes from the database's
   own clock, the ledger sum comes from a literal query. A task that accepts no
   input has no input to be abused through. Losing `--dry-run` is the price, and
   it is worth paying: "an input surface of zero" is a property a test can
   check, "only harmless parameters" is not.
2. **The credential travels the repository's ordinary redaction path.** It is
   read once, used once as a header value, and never placed in any structure
   this file prints. `summary()` builds the only thing printed and it is built
   from a fixed key list.
3. **Nothing echoes a body.** The balance response is account-level money. It is
   allowed to decide a verdict; it is not allowed into a log line. What is
   recorded is the delivery-receipt vocabulary and nothing else -- `adapter`,
   `http_status`, `reason` (`channel.py`, whose own comment reads "Never echo
   the body").

## Where it runs, and why not in the worker

The comparison needs the settled sum **across all tenants**, and no application
role can express that: `model_invocation_costs` has FORCE ROW LEVEL SECURITY
with a `tenant_id = current_setting('app.tenant_id')` policy, so
`fastagent_worker` sees one tenant at a time by construction (which is why
`scripts/reconcile_costs.py` requires `--tenant` and offers no `--all`).

Rather than widen a tenant-scoped role to see across tenants -- a hole that
would stay open for everything else that role does -- this job runs where the
platform-wide numbers are already read: on the Stage host, as the database owner
inside the postgres container, exactly like `scripts/stage_observability.py`.
The aggregate never becomes reachable from the application.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

#: Populated by the operator, root-only, and holding one credential: the same
#: provider key the worker uses, because the supplier offers no other kind
#: (ADR-0041 §三). Deliberately its own file rather than the worker's env file:
#: this job should be installable without handing it everything the worker has.
ENV_FILE = Path("/etc/fastagent-v2-stage/balance-reconciliation.env")
POSTGRES_CONTAINER = "fastagent-v2-stage-postgres-1"

PROVIDER_ID = "deepseek"
BALANCE_URL = "https://api.deepseek.com/user/balance"
#: The worker's rule, copied because this job runs on the host, outside the
#: application image, and cannot import `worker.providers`.
#:
#: **Copied verbatim, including the upper-casing.** The first version dropped
#: that step and derived `..._API_KEY_deepseek`, an environment variable nobody
#: sets -- the job would have reported `no_credential_configured` forever, which
#: reads exactly like "not installed yet". The secret scanner had actually
#: pointed at this: the upper-case form is on its exemption list because it is
#: recognisably an env var *name*, and the lower-case one was not. The first fix
#: read that as a formatting complaint and kept the divergence
#: (independent review 2026-09-04, T71 F2).
#:
#: `test_the_credential_env_name_matches_the_worker` pins this to
#: `providers.credential_env_name` -- the rule itself, not a recomputation of
#: this file's own answer.
CREDENTIAL_ENV_TEMPLATE = "FASTAGENT_MODEL_API_KEY_{provider}"
CREDENTIAL_ENV = CREDENTIAL_ENV_TEMPLATE.format(
    provider=PROVIDER_ID.upper().replace("-", "_")
)
REQUEST_TIMEOUT_SECONDS = 20.0

#: How far the gap between two snapshots may stray from 24h and still describe
#: one ledger day. A run that fires late is fine; a run that missed a day is not
#: -- summing one calendar day against a two-day balance delta would manufacture
#: a difference out of nothing but a missed cron.
MIN_INTERVAL_SECONDS = 23 * 3600
MAX_INTERVAL_SECONDS = 25 * 3600

#: **Derived from bounds, not from a measured distribution -- and that is the
#: weakness to know about.**
#:
#: A call in flight when the snapshot is taken can be charged by the supplier on
#: one side of the boundary and recorded by the ledger on the other. That skew
#: is the smallest difference this comparison can ever produce without anything
#: being wrong, so the tolerance has to clear it.
#:
#: * a single call's **provable** ceiling is 0.070656 CNY -- input 16 KiB + 1024
#:   framing = 17408 tokens, output capped at the role's `max_output_tokens`
#:   (2048 for `PLAN_CAPABILITY`), at peak uncached rates. Not a typical call:
#:   the bound the reservation is allowed to reach (ADR-0036, as corrected by
#:   its independent review -- the first draft of that number was 2.4x low);
#: * **4** is the in-flight bound the conversation ceiling gate already uses,
#:   derived there from serial per-conversation processing;
#: * 4 x 0.070656 = 0.282624, rounded up to leave room for whatever rounding the
#:   supplier applies to its own published balance, which we have not measured.
#:
#: **Re-derive this when a real call distribution exists (T56).** Too small
#: means crying wolf every morning; too large means a quiet leak stays quiet.
#: The failure this is really for -- calls the ledger never recorded at all --
#: produces differences far above any value in this range, so the exact figure
#: is not load-bearing for the main signal.
TOLERANCE_CNY = Decimal("0.30")

MATCHED = "matched"
OVER_TOLERANCE = "over_tolerance"
INCOMPARABLE = "incomparable"
UNAVAILABLE = "unavailable"


class BalanceUnavailable(RuntimeError):
    """The supplier did not answer with a usable CNY balance.

    Carries the receipt vocabulary and nothing else -- never a response body.
    """

    def __init__(self, reason: str, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class Comparison:
    """One day's verdict, and every number that produced it."""

    verdict: str
    reason: str
    balance_delta_cny: Decimal | None = None
    ledger_settled_cny: Decimal | None = None
    difference_cny: Decimal | None = None


def cny_balance(payload: Any) -> Decimal:
    """Pick the CNY total out of a balance response.

    The supplier reports one entry per currency. A missing CNY entry is not an
    error to paper over with a zero: the ledger prices everything in CNY
    (`cost_cny`), so without a CNY balance there is nothing to compare and the
    honest verdict is `unavailable`.
    """

    if not isinstance(payload, dict):
        raise BalanceUnavailable("balance_response_not_an_object")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        raise BalanceUnavailable("balance_response_missing_balance_infos")
    for info in infos:
        if not isinstance(info, dict) or str(info.get("currency", "")).upper() != "CNY":
            continue
        try:
            return Decimal(str(info["total_balance"]))
        except (LookupError, InvalidOperation, TypeError):
            raise BalanceUnavailable("balance_response_unreadable_total") from None
    raise BalanceUnavailable("balance_response_has_no_cny_entry")


def compare(
    *,
    previous_balance: Decimal | None,
    current_balance: Decimal,
    interval_seconds: float | None,
    ledger_settled: Decimal,
    covers_one_ledger_day: bool = True,
    tolerance: Decimal = TOLERANCE_CNY,
) -> Comparison:
    """Compare what the supplier charged against what the ledger recorded.

    Every "cannot compare" case is a named verdict rather than a silent zero.
    A comparison that quietly reports `matched` when it had nothing to compare
    is worse than no comparison at all: it manufactures the reassurance the
    whole job exists to earn.
    """

    if previous_balance is None or interval_seconds is None:
        return Comparison(INCOMPARABLE, "no_previous_snapshot")
    if not MIN_INTERVAL_SECONDS <= interval_seconds <= MAX_INTERVAL_SECONDS:
        # A missed day, or a run fired far off schedule. The ledger sum covers
        # one calendar day; the balance delta would cover however long the gap
        # was. Comparing them would invent a difference.
        return Comparison(INCOMPARABLE, "snapshot_interval_outside_one_day")
    if not covers_one_ledger_day:
        # (The caller decides this; see `_reconcile`. It now means "the two
        # readings bracket that day", not merely "they are a day apart".)
        # **A 24-hour gap is not the same claim as "these two readings bracket
        # the day being summed."** This file installs a 0755, argument-less
        # executable in `/usr/local/sbin`, so "just run it and see" is a natural
        # thing for an operator to do: a manual run at 23:50 on day D sits 23.75h
        # after the 00:05 run, passes the interval test, and would compare a
        # balance delta covering *D* against a ledger sum for *D-1*. Two
        # different days subtracted from each other can report a false `matched`,
        # which this job says elsewhere is worse than not comparing at all
        # (independent review 2026-09-04, T71 F6).
        return Comparison(INCOMPARABLE, "window_does_not_bracket_the_ledger_day")
    delta = previous_balance - current_balance
    if delta < 0:
        # Spending cannot raise a balance. A top-up or a grant landed inside the
        # window, and this day cannot be reconciled. Grant *expiry* moves the
        # balance the same way spending does and is indistinguishable from it --
        # recorded in ADR-0041 as a residual limit, not solved here.
        return Comparison(
            INCOMPARABLE, "balance_increased_top_up_or_grant", balance_delta_cny=delta
        )
    difference = delta - ledger_settled
    verdict = MATCHED if abs(difference) <= tolerance else OVER_TOLERANCE
    return Comparison(
        verdict,
        "within_tolerance" if verdict == MATCHED else "difference_exceeds_tolerance",
        balance_delta_cny=delta,
        ledger_settled_cny=ledger_settled,
        difference_cny=difference,
    )


def load_env(path: Path) -> dict[str, str]:
    """Same shape as the collector's loader, same file format, same host."""

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def psql(sql: str, **variables: str) -> str:
    r"""Run one statement as the database owner inside the Stage container.

    Every SQL string in this file is a literal, and values reach it as **psql
    variables** (`\set name 'value'`, read back as `:'name'`) rather than by
    formatting. The collector states the reason next to its own query and it is
    worth repeating: building SQL by f-string is the wrong reflex even where
    every value is a module constant, because the next value will not be one.

    `:'name'` renders as a quoted string, so numeric columns are written
    `nullif(:'name','')::numeric` -- which also gives NULL its own spelling
    (the empty string) instead of a bare word that quoting would ruin.

    **The script goes in on stdin, and that is a confidentiality property, not a
    style choice.** The first version passed values as `-v name=value` on the
    command line, where two things could read them: `ps`, for any user on the
    host, and `CalledProcessError`, whose `__str__` prints the whole argv --
    straight into the cron log this job's own line redirects to disk. The
    account balance is money. It decides the verdict; it does not go on a
    command line (independent review 2026-09-04, T71 F3).
    """

    # **`ON_ERROR_STOP` first, and it is not a formality.** psql exits 1 when a
    # `-c` statement fails, and **0 when a script (`-f`) fails** unless this is
    # set. Moving values off argv (F3) moved the SQL onto stdin, and that quietly
    # turned every database error -- missing table, missing column, CHECK
    # violation, lock timeout, permission -- into a silent success: `run(check=True)`
    # saw exit 0, and `main`'s `except SubprocessError` became unreachable.
    #
    # The worst shape it produced is the one this whole batch keeps warning
    # about: the settled-sum query fails, `row` comes back empty, `settled`
    # defaults to 0, and on a low-traffic day the difference is ~0 -- so the job
    # writes a fresh, comparable, **`matched`** row while knowing nothing at
    # all, and all three Grafana rules stay green (independent re-review
    # 2026-09-04, R1, measured against a throwaway `postgres:17`).
    #
    # `test_the_generated_script_stops_on_the_first_error` asserts the shape,
    # the way F1's guard simulates the installer's own grep.
    script = "\\set ON_ERROR_STOP on\n" + "".join(
        f"\\set {name} {_quote(value)}\n" for name, value in variables.items()
    )
    command = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq -f -'
    return run(
        ["docker", "exec", "-i", POSTGRES_CONTAINER, "sh", "-ec", command],
        stdin=script + sql,
    )


def _quote(value: str) -> str:
    r"""Single-quote a psql `\set` value the way psql itself reads it."""

    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def run(command: list[str], timeout: float = 30.0, stdin: str | None = None) -> str:
    """One place that starts a process, same shape as the collector's.

    `SubprocessError` is deliberately **not** caught here: a failure has to
    reach `main`, which is the one place that knows how to say so without
    saying anything else (see `_sanitised`).
    """

    completed = subprocess.run(  # noqa: S603 - fixed commands, no user input
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
    )
    return completed.stdout.strip()


def fetch_balance(credential: str) -> Decimal:
    """Ask the supplier. Never returns, logs or stores the body."""

    request = urllib.request.Request(  # noqa: S310 - constant https URL
        BALANCE_URL,
        headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - constant HTTPS endpoint
            request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            body = response.read().decode("utf-8")
            status = int(response.status)
    except urllib.error.HTTPError as error:
        raise BalanceUnavailable("provider_rejected_request", int(error.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        # No status to report: the request never got an answer. The receipt
        # vocabulary has a word for exactly this shape and it is not "failed".
        raise BalanceUnavailable("provider_unreachable") from None
    if status != 200:
        raise BalanceUnavailable("provider_rejected_request", status)
    try:
        payload = json.loads(body)
    except ValueError:
        raise BalanceUnavailable("balance_response_unparseable", status) from None
    return cny_balance(payload)


def summary(
    *,
    comparison: Comparison,
    balance_read: bool,
    http_status: int | None,
    ledger_date: str | None,
    unreconciled_rows: int | None = None,
) -> dict[str, Any]:
    """The only thing this program prints, built from a fixed key list.

    Fixed on purpose: a summary assembled from whatever happens to be in scope
    is how a credential or a response body ends up in a log. Amounts are
    rendered as strings so that no float rounding is introduced between the
    database and the operator's screen.
    """

    def amount(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    return {
        "provider_id": PROVIDER_ID,
        "ledger_date": ledger_date,
        "verdict": comparison.verdict,
        "reason": comparison.reason,
        "adapter": "http",
        "http_status": http_status,
        "balance_read": balance_read,
        "balance_delta_cny": amount(comparison.balance_delta_cny),
        "ledger_settled_cny": amount(comparison.ledger_settled_cny),
        "difference_cny": amount(comparison.difference_cny),
        "tolerance_cny": str(TOLERANCE_CNY),
        # How much of a difference is already explained. Not the amount -- the
        # count. The estimate is on the row for whoever queries it; the printed
        # line answers "is this difference the unreconciled backlog?" without
        # putting another money figure into a log (T71 F8).
        "unreconciled_rows": unreconciled_rows,
    }


def _amount(value: Decimal | None) -> str:
    """A psql variable value: the empty string is how this file spells NULL."""

    return "" if value is None else str(value)


#: How far past midnight a reading may sit and still count as "on the day
#: boundary". One hour, chosen to contain the 00:05 cron plus a late start,
#: and no wider: every extra minute is a minute of the wrong day's spending
#: inside the window. Together with the interval and the date difference, this
#: is what makes "these two readings bracket that day" true rather than
#: approximately true (re-review 2026-09-04, R2).
DAY_BOUNDARY_WINDOW_SECONDS = 3600.0


def _hugs_the_day_boundary(seconds_past_midnight: float) -> bool:
    return 0.0 <= seconds_past_midnight < DAY_BOUNDARY_WINDOW_SECONDS


def _sanitised(error: BaseException) -> str:
    """A failure reason that carries no value from the database call.

    `CalledProcessError.__str__` prints the whole argv, and this job's cron line
    sends stderr to a file on disk. Values no longer travel on argv (see
    `psql`), and this is the second lock on the same door: whatever the database
    layer raises, what leaves this program is the exception's *type*.
    """

    return f"database_call_failed:{type(error).__name__}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare the supplier's account balance against the ledger (ADR-0041).",
        epilog=(
            "Takes no arguments by design: nothing this job queries may be steered from "
            "outside. See the module docstring."
        ),
    )
    parser.parse_args(argv)

    environment = load_env(ENV_FILE)
    credential = environment.get(CREDENTIAL_ENV, "")
    if not credential:
        print(json.dumps({"verdict": UNAVAILABLE, "reason": "no_credential_configured"}))
        return 1

    try:
        return _reconcile(credential)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        # Nothing was written and nothing is claimed. The next run's freshness
        # metric is what makes a run of these visible; see the collector.
        #
        # `RuntimeError` is in the tuple because `record()` raises one when the
        # insert returns no row -- and without it that check turned this job's
        # single line of JSON into a traceback. The observable surface of this
        # job **is** that line (re-review 2026-09-04, P3).
        print(json.dumps({"verdict": UNAVAILABLE, "reason": _sanitised(error)}))
        return 1


def _reconcile(credential: str) -> int:
    # The predecessor decides which day is being reconciled. **Not `now()`**: a
    # 24-hour gap and "these two readings bracket the day we are summing" are
    # different claims, and only the second one licenses a subtraction. The day
    # is the calendar day the previous snapshot fell on; today must be the day
    # after it (independent review 2026-09-04, T71 F6).
    previous = psql(
        "SELECT coalesce(balance_cny::text, ''), "
        "extract(epoch FROM now() - taken_at)::text, "
        "(taken_at AT TIME ZONE 'Asia/Shanghai')::date::text, "
        "((now() AT TIME ZONE 'Asia/Shanghai')::date "
        "- (taken_at AT TIME ZONE 'Asia/Shanghai')::date)::text, "
        # Both ends, as seconds past midnight in the ledger's timezone. A day
        # apart is not the same claim as "these two readings bracket that day":
        # a predecessor at 14:00 and a run at 14:00 next day satisfy both the
        # interval and the date difference while the window sits **14 hours off**
        # the day being summed (re-review 2026-09-04, R2 -- and its path is the
        # ordinary one: a manual run, a cron that failed on a supplier 5xx, a
        # second manual run).
        "extract(epoch FROM (taken_at AT TIME ZONE 'Asia/Shanghai')::time)::text, "
        "extract(epoch FROM (now() AT TIME ZONE 'Asia/Shanghai')::time)::text "
        "FROM provider_balance_snapshots "
        "WHERE provider_id = :'provider' AND balance_cny IS NOT NULL "
        "ORDER BY taken_at DESC LIMIT 1;",
        provider=PROVIDER_ID,
    ).strip()

    previous_balance: Decimal | None = None
    interval_seconds: float | None = None
    ledger_date: str | None = None
    covers_one_ledger_day = False
    if previous:
        fields = (previous.split("|") + ["", "", "", "", ""])[:6]
        raw_balance, raw_interval, raw_date, raw_day_gap, raw_prev_time, raw_now_time = fields
        if raw_balance:
            previous_balance = Decimal(raw_balance)
            interval_seconds = float(raw_interval)
            ledger_date = raw_date
            covers_one_ledger_day = (
                raw_day_gap == "1"
                and _hugs_the_day_boundary(float(raw_prev_time))
                and _hugs_the_day_boundary(float(raw_now_time))
            )

    try:
        current_balance = fetch_balance(credential)
    except BalanceUnavailable as error:
        comparison = Comparison(UNAVAILABLE, error.reason)
        record(
            comparison=comparison,
            balance=None,
            http_status=error.http_status,
            previous_available=previous_balance is not None,
            ledger_date=None,
        )
        print(
            json.dumps(
                summary(
                    comparison=comparison,
                    balance_read=False,
                    http_status=error.http_status,
                    ledger_date=None,
                )
            )
        )
        return 1

    http_status = 200
    settled = Decimal("0")
    unreconciled_rows = 0
    unreconciled_estimate = Decimal("0")
    if ledger_date is not None:
        # Two numbers about the same day, read in one round trip. The second one
        # is what keeps a difference readable: money the reconciler moved to
        # `unreconciled` (ADR-0040) was very likely charged by the supplier and
        # will never enter any day's settled sum, so it is a *known* contributor
        # to the difference. Without it beside the difference, an operator
        # cannot tell "we failed to record a call" from "we already know we
        # cannot account for this one" (independent review 2026-09-04, T71 F8).
        row = psql(
            "SELECT coalesce(sum(cost_cny) FILTER (WHERE status = 'settled'), 0)::text, "
            "count(*) FILTER (WHERE status = 'unreconciled')::text, "
            "coalesce(sum(estimated_cost_cny) FILTER (WHERE status = 'unreconciled'), 0)::text "
            "FROM model_invocation_costs "
            "WHERE (occurred_at AT TIME ZONE 'Asia/Shanghai')::date = :'ledger_date'::date;",
            ledger_date=ledger_date,
        ).strip()
        raw_settled, raw_rows, raw_estimate = (row.split("|") + ["", ""])[:3]
        settled = Decimal(raw_settled or "0")
        unreconciled_rows = int(raw_rows or "0")
        unreconciled_estimate = Decimal(raw_estimate or "0")

    comparison = compare(
        previous_balance=previous_balance,
        current_balance=current_balance,
        interval_seconds=interval_seconds,
        ledger_settled=settled,
        covers_one_ledger_day=covers_one_ledger_day,
    )
    comparable = comparison.difference_cny is not None
    record(
        comparison=comparison,
        balance=current_balance,
        http_status=http_status,
        previous_available=previous_balance is not None,
        ledger_date=ledger_date if comparable else None,
        unreconciled_rows=unreconciled_rows if comparable else None,
        unreconciled_estimate_cny=unreconciled_estimate if comparable else None,
    )
    print(
        json.dumps(
            summary(
                comparison=comparison,
                balance_read=True,
                http_status=http_status,
                ledger_date=ledger_date if comparable else None,
                unreconciled_rows=unreconciled_rows if comparable else None,
            )
        )
    )
    # `over_tolerance` is not a failure of this program: it did its job and the
    # answer was bad news. Exit codes report whether the check ran, and the
    # Grafana rule reports what it found -- ADR-0041 wants an alert, and an
    # alert that also fails a cron job invites someone to "fix" the cron.
    return 0


def record(
    *,
    comparison: Comparison,
    balance: Decimal | None,
    http_status: int | None,
    previous_available: bool,
    ledger_date: str | None,
    unreconciled_rows: int | None = None,
    unreconciled_estimate_cny: Decimal | None = None,
) -> None:
    """Write the snapshot, whatever the verdict.

    A day we could not check is a fact the next run needs: without the row, a
    supplier outage would look exactly like a job nobody installed.
    """

    # Two complete literals rather than one assembled from a fragment. The
    # fragment was a constant, and the reason to write it out anyway is the same
    # one the collector gives for its query: an f-string next to SQL is a habit,
    # and the next value to be interpolated will not be a constant.
    with_previous = (
        "INSERT INTO provider_balance_snapshots ("
        "provider_id, balance_cny, http_status, reason, previous_snapshot_id, "
        "ledger_date, balance_delta_cny, ledger_settled_cny, difference_cny, "
        "tolerance_cny, verdict, unreconciled_rows, unreconciled_estimate_cny) VALUES ("
        ":'provider', nullif(:'balance','')::numeric, "
        "nullif(:'http_status','')::integer, :'reason', "
        "(SELECT id FROM provider_balance_snapshots "
        "WHERE provider_id = :'provider' AND balance_cny IS NOT NULL "
        "ORDER BY taken_at DESC LIMIT 1), "
        "nullif(:'ledger_date','')::date, nullif(:'delta','')::numeric, "
        "nullif(:'settled','')::numeric, nullif(:'difference','')::numeric, "
        "nullif(:'tolerance','')::numeric, :'verdict', "
        "nullif(:'unreconciled_rows','')::integer, "
        "nullif(:'unreconciled_estimate','')::numeric) RETURNING id;"
    )
    without_previous = (
        "INSERT INTO provider_balance_snapshots ("
        "provider_id, balance_cny, http_status, reason, previous_snapshot_id, "
        "ledger_date, balance_delta_cny, ledger_settled_cny, difference_cny, "
        "tolerance_cny, verdict, unreconciled_rows, unreconciled_estimate_cny) VALUES ("
        ":'provider', nullif(:'balance','')::numeric, "
        "nullif(:'http_status','')::integer, :'reason', "
        "NULL, "
        "nullif(:'ledger_date','')::date, nullif(:'delta','')::numeric, "
        "nullif(:'settled','')::numeric, nullif(:'difference','')::numeric, "
        "nullif(:'tolerance','')::numeric, :'verdict', "
        "nullif(:'unreconciled_rows','')::integer, "
        "nullif(:'unreconciled_estimate','')::numeric) RETURNING id;"
    )
    # `RETURNING id` plus this check is the one proof that does not depend on an
    # exit code at all: a row came back, therefore a row went in (R1's second
    # recommendation).
    written = psql(
        with_previous if previous_available else without_previous,
        provider=PROVIDER_ID,
        balance=_amount(balance),
        http_status="" if http_status is None else str(http_status),
        reason=comparison.reason,
        ledger_date=ledger_date or "",
        delta=_amount(comparison.balance_delta_cny),
        settled=_amount(comparison.ledger_settled_cny),
        difference=_amount(comparison.difference_cny),
        tolerance=str(TOLERANCE_CNY),
        verdict=comparison.verdict,
        unreconciled_rows="" if unreconciled_rows is None else str(unreconciled_rows),
        unreconciled_estimate=_amount(unreconciled_estimate_cny),
    )
    if not written.strip():
        raise RuntimeError("snapshot insert returned no row")


if __name__ == "__main__":
    sys.exit(main())
