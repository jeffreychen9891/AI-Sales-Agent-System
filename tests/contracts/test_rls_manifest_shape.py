"""`rls-manifest.json` carries only what something actually reads.

**The `roles` field was deleted rather than corrected, and this is why.** It
listed, per table, which database roles the RLS policy bound. On
`channel_bindings` it said `["fastagent_ingress"]` -- and `001` creates that
policy with **no `TO` clause** and `FORCE ROW LEVEL SECURITY`, so it binds every
role, `fastagent_app` included. A reverse probe on B2 #3 measured it: removing
the route's own `tenant_id = %s` changed nothing observable, because RLS was
holding the cross-tenant case by itself.

The field had been wrong for a long time and nothing objected, because
**nothing read it**: `check_static.py` loads this manifest and uses `row["table"]`
only. A field with no consumer cannot be kept honest by anything except
attention, and attention is what ran out.

Two ways to fix that were on the table -- give `roles` a gate, or delete it. The
user chose deletion (2026-09-05), and this case is the half that makes deletion
stick: without it, the next person adds the field back "for documentation" and
the same silence resumes. **The authority on which role a policy binds is the
migration**, and `check_static.py` already reads that.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "packages/contracts/manifests/rls-manifest.json"

#: Exactly the keys `check_static.py` consumes, and nothing else.
#:
#: **`tenant_less` and `reason` joined on 2026-09-06 (ADR-0073 §五), and they
#: joined with a consumer**, which is the only thing this file has ever asked
#: for: `check_every_table_is_classified` reads both -- the flag to decide
#: whether a table needs a policy at all, and the reason to refuse an
#: unexplained one. A row declaring `tenant_less` without a reason fails
#: there, so neither field can go stale the way `roles` did.
ALLOWED_KEYS = {"table", "policy", "tenant_less", "reason"}


def test_the_manifest_carries_no_field_without_a_consumer() -> None:
    rows = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert rows, "an empty RLS manifest would pass every check that reads it"
    for row in rows:
        extra = set(row) - ALLOWED_KEYS
        assert not extra, (
            f"{row.get('table')!r} carries {sorted(extra)}, which nothing reads. "
            "A field with no consumer drifts silently -- `roles` did, for months, "
            "and claimed channel_bindings' policy bound only fastagent_ingress "
            "when it binds every role. Either give the field a gate or leave it out."
        )


def test_every_row_names_a_table_and_answers_the_isolation_question() -> None:
    """The deletion must not have taken anything load-bearing with it.

    **A row answers the question one way or the other, never both and never
    neither** (ADR-0073 §五). Before that ruling every row named a policy, so
    "has a policy" and "is classified" were the same sentence. They are not any
    more: `schema_migrations`, `provider_balance_snapshots` and
    `platform_audit_events` have no tenant dimension for a policy to isolate.
    Keeping the assertion as `row["policy"]` would have made the manifest unable
    to state that, and the alternative -- inventing a policy name for a table
    that has none -- is the kind of placeholder `roles` turned into.
    """

    rows = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for row in rows:
        assert row["table"], row
        assert bool(row.get("policy")) != bool(row.get("tenant_less")), row
    assert len({row["table"] for row in rows}) == len(rows), "one row per table"
