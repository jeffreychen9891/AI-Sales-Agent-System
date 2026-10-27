"""P3-3: assert against the database what `045` actually delivered.

The contract-level cases in `tests/contracts/test_peer_identity_lives_on_the_event.py`
all scan files. They keep the column out of the *code*, which is worth doing --
but the thing this batch delivers is that the column is out of the *database*,
and no file scan can see that. Independent review 2026-09-08, P3-3.

The gate already stands the database up and runs every migration, so this costs
one query.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from psycopg import Connection
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def test_the_interim_column_is_absent_after_the_migrations_run() -> None:
    """What `045` is for, asked of the catalog rather than of a file."""

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT count(*) AS present FROM information_schema.columns
             WHERE table_name = 'conversation_aggregates'
               AND column_name = 'peer_external_user_id'
            """
        ).fetchone()

    assert row is not None and row["present"] == 0, (
        "045 did not remove conversation_aggregates.peer_external_user_id"
    )


def test_the_migration_recorded_itself() -> None:
    """A drop that did not record its version re-runs on every deploy.

    `IF EXISTS` makes the re-run harmless, which is exactly why the missing
    marker would go unnoticed -- so it is asserted rather than assumed.
    """

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS applied FROM schema_migrations WHERE version = %s",
            ("045_drop_interim_peer_identity",),
        ).fetchone()

    assert row is not None and row["applied"] == 1


def test_nothing_in_the_catalog_still_names_the_dropped_column() -> None:
    """A constraint or view left naming it would make the drop partial.

    `017` attached a `CHECK` to the column; a dependent object that survived
    would mean the column is gone but something still expects it.
    """

    with _migration_connection() as connection:
        constraints = connection.execute(
            """
            SELECT count(*) AS n FROM pg_constraint
             WHERE pg_get_constraintdef(oid) LIKE %s
            """,
            ("%peer_external_user_id%",),
        ).fetchone()
        views = connection.execute(
            """
            SELECT count(*) AS n FROM pg_views
             WHERE definition LIKE %s AND schemaname NOT IN ('pg_catalog', 'information_schema')
            """,
            ("%peer_external_user_id%",),
        ).fetchone()

    assert constraints is not None and constraints["n"] == 0
    assert views is not None and views["n"] == 0
