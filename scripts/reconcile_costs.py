#!/usr/bin/env python
"""Age abandoned cost reservations into `unreconciled` (T55, ADR-0040).

Four exits in the provider adapter leave a reservation in `estimated` and never
return to it -- a timeout, a transport exception, an HTTP >= 400 with no usable
`usage`, and a malformed response with none. Its own comment says the estimate
stays reserved "until reconciliation"; until this script there was no
reconciliation anywhere in the repository.

**What it deliberately does not do.** It does not decide what those calls cost.
Asking the supplier is not merely hard, it is impossible with today's ledger:
usage is read exactly once, from the response body of the call itself, and no
supplier-side identifier is stored -- there is no request id to ask about. So
the row is moved to a state that says "we do not know", which is true, rather
than to `failed` (which would assert the supplier did not bill us) or `settled`
(which would put an unproven number into settled spend, forbidden by ADR-0034).

**Running this is bookkeeping, not enforcement.** The ceiling gates already
treat an over-age `estimated` row as abandoned, computed in SQL from
`occurred_at`, so a deployment where this never runs stays *conservative* rather
than blind. That was deliberate: on 2026-09-03 the Stage metrics collector spent
a day pushing seven metrics while everyone believed it pushed ten, purely
because merging a change is not deploying it. A gate that only works once a
scheduled job is installed is a gate that reports healthy on a system nobody
installed it on.

**One tenant per run, and there is no way to enumerate tenants here.**
`--tenant` is required, and this script offers no `--all`: every read and write
goes through `Database.transaction(tenant_id=...)`, which sets the RLS role, so
"all tenants" is not a thing this layer can express without first stepping
outside the isolation it depends on. Whoever installs the cron has to supply the
list -- from the control plane, or from a query run under a role that is allowed
to see across tenants. Said here rather than left to be discovered at install
time (independent review 2026-09-03, closing note).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    "packages/runtime/src",
    "services/api/src",
    "services/worker/src",
    "services/ingress/src",
)


def bootstrap_source_path() -> None:
    """Same four roots as `pyproject.toml` and `scripts/ci.sh`.

    The repository is not installed, so without this the script reports an
    import failure that has nothing to do with the ledger -- the shape of defect
    the replay runner shipped with (its review's P1-1).
    """

    for relative in reversed(SOURCE_ROOTS):
        root = str(REPOSITORY_ROOT / relative)
        if root not in sys.path:
            sys.path.insert(0, root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Age abandoned cost reservations.")
    parser.add_argument("--tenant", required=True, help="tenant id (UUID)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many rows would move; write nothing",
    )
    arguments = parser.parse_args(argv)

    bootstrap_source_path()
    from fastagent_v2.runtime.db import Database
    from fastagent_v2.runtime.settings import Settings
    from fastagent_v2.worker.cost import RECONCILE_AFTER_SECONDS, CostGovernor
    from fastagent_v2.worker.model_policy import active_release

    tenant_id = UUID(arguments.tenant)
    settings = Settings.from_env()
    governor = CostGovernor(
        database=Database(settings.worker_database_url), release=active_release()
    )

    if arguments.dry_run:
        pending = governor.abandoned_reservations(tenant_id=tenant_id)
        print(
            f"{pending} reservation(s) older than {RECONCILE_AFTER_SECONDS}s are still "
            "'estimated'; --dry-run wrote nothing"
        )
        return 0

    moved = governor.reconcile_abandoned(tenant_id=tenant_id)
    print(f"moved {moved} reservation(s) to 'unreconciled'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
