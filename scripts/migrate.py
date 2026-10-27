"""Apply the SQL migrations, then re-sync each tenant's system roles.

**This script is no longer standalone.** It imports
`fastagent_v2.runtime.system_roles`, so it needs `packages/runtime/src` on
`PYTHONPATH` -- which the gate scripts and `stage-deploy.sh` already set, and
which a bare `python scripts/migrate.py` from a fresh shell does not
(independent review 2026-09-05, P3-2). The import is deliberate: the seeding
rule reads the resource catalog, and SQL cannot.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastagent_v2.runtime.system_roles import ensure_system_roles
from psycopg import Connection, sql
from psycopg.rows import TupleRow

ROOT = Path(__file__).resolve().parents[1]
ROLE_PASSWORD_ENV = {
    "fastagent_app": "FASTAGENT_APP_DB_PASSWORD",
    "fastagent_ingress": "FASTAGENT_INGRESS_DB_PASSWORD",
    "fastagent_worker": "FASTAGENT_WORKER_DB_PASSWORD",
    "fastagent_scheduler": "FASTAGENT_SCHEDULER_DB_PASSWORD",
}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"required migration setting is missing: {name}")
    return value


def required_secret(name: str) -> str:
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError(f"required migration secret file is empty: {name}_FILE")
        return value
    return required(name)


# `Connection[object]` described the rows as `object`, so `row[0]` was a type
# error at every call site -- the default row factory yields tuples, and saying
# so is what lets `fetchone()[0]` be checked rather than merely tolerated (T68).
def ensure_roles(connection: Connection[TupleRow]) -> None:
    for role, environment_name in ROLE_PASSWORD_ENV.items():
        password = required_secret(environment_name)
        exists = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", (role,)
        ).fetchone()
        if exists is None or not bool(exists[0]):
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        else:
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )


def applied_versions(connection: Connection[TupleRow]) -> set[str]:
    exists = connection.execute(
        "SELECT to_regclass('public.schema_migrations') IS NOT NULL AS present"
    ).fetchone()
    if exists is None or not bool(exists[0]):
        return set()
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {str(row[0]) for row in rows}


def main() -> None:
    migration_url = required("FASTAGENT_MIGRATION_DATABASE_URL")
    migrations = sorted((ROOT / "infrastructure" / "migrations").glob("*.sql"))
    if not migrations:
        raise RuntimeError("no migration files found")
    with Connection.connect(migration_url) as connection:
        with connection.transaction():
            ensure_roles(connection)
        already = applied_versions(connection)
        for path in migrations:
            version = path.stem
            if version in already:
                continue
            # Each migration is its own transaction: a later failure must not
            # roll back an earlier migration that already succeeded.
            with connection.transaction():
                connection.execute(path.read_text(encoding="utf-8"))
        seed_system_roles(connection)


def seed_system_roles(connection: Connection[TupleRow]) -> None:
    """K3: every tenant has `owner`, `operator` and `viewer`, re-synced each deploy.

    **Here rather than in a migration file, and the reason is drift.** SQL cannot
    read `resource-catalog.json`, so a migration seeding literal permission
    arrays would freeze `owner` ("every grantable permission") and `viewer`
    ("everything read-only") at the moment it was written -- and there are forty
    routes still to build, each of which may add catalogued actions. Every one
    of them would then be missing from the roles that are *defined* as covering
    them, silently. That is the shape T92 and T98 were both written for.

    Running the rule on every migrate keeps the two computed roles true of the
    code actually deployed. It is idempotent: the upsert rewrites permissions,
    so a tenant created before an action existed picks it up on the next deploy.

    Runs after the SQL migrations because `roles` has to exist first (015), and
    in its own transaction per tenant so that one tenant's failure does not roll
    back the others' -- the same reasoning as the per-migration transaction
    above.
    """

    tenants = connection.execute("SELECT id FROM tenants ORDER BY id").fetchall()
    for row in tenants:
        with connection.transaction():
            counts = ensure_system_roles(connection, tenant_id=row[0])
        # A `-1` means a role of that name exists and is **not** the platform's,
        # so this deploy left it alone. Printed because it is the only signal:
        # the skip is deliberate, and a silent skip would look identical to a
        # successful sync.
        for name, size in sorted(counts.items()):
            if size < 0:
                print(
                    f"system role {name!r} skipped for tenant {row[0]}: "
                    "a role of that name exists and is not a system role"
                )


if __name__ == "__main__":
    main()
