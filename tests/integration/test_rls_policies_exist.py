"""Every table `rls-manifest.json` claims is isolated really is, **in the database**.

**This gate did not exist, and an independent review proved it by deleting the
policies** (2026-09-11, P2-3). The probe removed all six `ENABLE`/`FORCE` lines
and all three `CREATE POLICY` blocks for the knowledge-base tables — anchors
counted, nine hits — and then:

* `scripts/check_static.py` printed `static birth gates: PASS` and exited 0;
* `tests/contracts` came back byte-identical to the unmutated baseline.

**Nothing anywhere went red.** The reason is that `check_rls_migration` asks
only whether the quoted table name appears *somewhere in the migration text* —
and `'knowledge_chunks'` also appears in the same file's purge table list, so
deleting the entire policy could not change the answer. That check reads SQL as
prose; this one asks PostgreSQL.

**The weakness is older than the batch that exposed it.** Every other
tenant-owned table had been covered by accident: they have routes, the routes
have cross-tenant cases, and those cases fail if isolation is missing. The
knowledge-base tables have no routes yet (`050` grants nothing on purpose), so
they were the first three to sit behind a claim that nothing tested — which is
exactly the shape `test_rls_manifest_shape.py` describes the deleted `roles`
field having, for months.

So this case is deliberately **manifest-driven rather than table-by-table**: it
covers every row that exists today and every row added later, including the ones
whose routes would otherwise have covered them by luck.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection

from tests.conftest import _migration_url

pytestmark = [pytest.mark.security]

REPO = Path(__file__).resolve().parents[2]
MANIFEST = json.loads(
    (REPO / "packages/contracts/manifests/rls-manifest.json").read_text(encoding="utf-8")
)

ISOLATED = [row for row in MANIFEST if row.get("policy")]
TENANT_LESS = [row for row in MANIFEST if row.get("tenant_less")]


@pytest.fixture(scope="module")
def db() -> Any:
    with Connection.connect(_migration_url()) as connection:
        yield connection


def test_the_manifest_is_not_empty_and_names_isolated_tables() -> None:
    """An empty manifest would make every case below vacuously true.

    The same guard `test_rls_manifest_shape.py` opens with, for the same reason:
    a loop over nothing passes.
    """

    assert len(ISOLATED) >= 20, f"only {len(ISOLATED)} isolated tables; the manifest shrank"


@pytest.mark.parametrize("row", ISOLATED, ids=lambda row: str(row["table"]))
def test_an_isolated_table_has_row_security_enabled_and_forced(row: dict, db: Any) -> None:
    """`ENABLE` alone is not isolation.

    Without `FORCE`, the table's **owner** bypasses every policy — and the owner
    is `fastagent_migrator`, which is the role the migrations and several test
    fixtures connect as. A table with `ENABLE` and no `FORCE` looks isolated in
    `pg_policies` and is not.
    """

    found = db.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = to_regclass(%s)",
        (row["table"],),
    ).fetchone()

    assert found is not None, f"{row['table']}: the manifest names a table that does not exist"
    enabled, forced = found
    assert enabled, f"{row['table']}: row level security is not enabled"
    assert forced, (
        f"{row['table']}: row level security is enabled but not FORCEd, so the owning "
        "role still reads every tenant's rows"
    )


@pytest.mark.parametrize("row", ISOLATED, ids=lambda row: str(row["table"]))
def test_the_policy_the_manifest_names_exists_on_that_table(row: dict, db: Any) -> None:
    """The manifest's `policy` field is a claim about the database; check it there.

    `check_static.py` verifies the table name appears in some migration's text.
    That is satisfied by a purge list, a comment, or a `GRANT` — which is how
    nine deleted lines changed nothing.
    """

    policies = {
        name
        for (name,) in db.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
            (row["table"],),
        ).fetchall()
    }
    assert row["policy"] in policies, (
        f"{row['table']}: the manifest names policy {row['policy']!r}, and the database "
        f"has {sorted(policies) or 'none'}"
    )


@pytest.mark.parametrize("row", ISOLATED, ids=lambda row: str(row["table"]))
def test_the_policy_actually_restricts_by_the_tenant_setting(row: dict, db: Any) -> None:
    """A policy named correctly and written `USING (true)` would pass everything above.

    **This is the half that makes the other two more than bookkeeping.** The
    expression is not parsed or compared to a golden string -- a policy may
    legitimately be written several ways -- it is only required to consult
    `app.tenant_id`, which is the one thing a policy that isolates by tenant
    cannot avoid doing.
    """

    expressions = db.execute(
        "SELECT coalesce(qual, ''), coalesce(with_check, '') FROM pg_policies "
        "WHERE schemaname = 'public' AND tablename = %s AND policyname = %s",
        (row["table"], row["policy"]),
    ).fetchone()

    assert expressions is not None, f"{row['table']}: policy {row['policy']!r} is missing"
    using, with_check = expressions
    assert "app.tenant_id" in using, (
        f"{row['table']}: the read side of {row['policy']!r} does not consult "
        f"app.tenant_id: {using!r}"
    )
    assert "app.tenant_id" in with_check, (
        f"{row['table']}: the write side of {row['policy']!r} does not consult "
        f"app.tenant_id, so a row can be written into another tenant: {with_check!r}"
    )


@pytest.mark.parametrize("row", TENANT_LESS, ids=lambda row: str(row["table"]))
def test_a_tenant_less_table_really_has_no_tenant_column(row: dict, db: Any) -> None:
    """The declaration checked against the database, not against the migration text.

    `check_every_table_is_classified` already refuses a `tenant_less` row for a
    table whose `CREATE TABLE` carries `tenant_id`. It reads the migration, so a
    column added later by `ALTER TABLE` would slip past it -- and the row would
    go on declaring that this table has no tenant dimension.
    """

    columns = {
        name
        for (name,) in db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (row["table"],),
        ).fetchall()
    }
    assert columns, f"{row['table']}: the manifest names a table that does not exist"
    assert "tenant_id" not in columns, (
        f"{row['table']}: declared tenant_less but the table has a tenant_id column. "
        "Either the declaration is stale or the table now wants a policy"
    )


def test_every_tenant_owned_table_in_the_database_is_in_the_manifest(db: Any) -> None:
    """The reverse direction, asked of the database rather than of the SQL text.

    `check_every_table_is_classified` scans `CREATE TABLE` statements. A table
    created some other way -- or one that gained `tenant_id` through an
    `ALTER TABLE` -- is invisible to it, and an unclassified table with a tenant
    column is reachable across tenants with nothing saying so.
    """

    carrying = {
        name
        for (name,) in db.execute(
            "SELECT c.table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_name = c.table_name "
            " AND t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'"
        ).fetchall()
    }
    declared = {str(row["table"]) for row in MANIFEST}
    assert not carrying - declared, (
        f"tables carry tenant_id and are not in rls-manifest.json: "
        f"{sorted(carrying - declared)}"
    )
