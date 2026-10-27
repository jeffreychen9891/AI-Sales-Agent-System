"""Migration 028: the rotation window on `channel_bindings` is now schema.

**`D18`'s standing debt** (B2 #1 independent review, Q3). Migration 001 gave the
table three nullable rotation columns and bound none of them together, so
"this binding is mid-rotation" was a state kept correct by hand.

Every case below writes a row that is legal without 028 and shows what the
ingress path would do with it -- these are not shapes invented to have something
to assert. The reference for each is `services/ingress/.../adapter.py`, which is
the only code that decides whether a callback token is accepted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Connection
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration]

HASH = "a" * 64
OTHER_HASH = "b" * 64


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _insert(connection: Connection[Any], tenant_id: UUID, release_id: UUID, **columns: Any) -> None:
    fields = {
        "config_id": f"rotation-{uuid4().hex[:12]}",
        "tenant_id": tenant_id,
        "profile_release_id": release_id,
        "current_token_hash": HASH,
        **columns,
    }
    names = ", ".join(fields)
    placeholders = ", ".join(["%s"] * len(fields))
    connection.execute(
        f"INSERT INTO channel_bindings({names}) VALUES ({placeholders})",  # noqa: S608
        tuple(fields.values()),
    )


def test_a_rotation_hash_without_a_start_time_is_refused(
    seeded_database: dict[str, Any],
) -> None:
    """`adapter.py:67` requires **both** columns, so this hash authenticates nothing.

    It is also the state `POST /channel-bindings/{id}/deactivate` cannot see:
    that route reports whether it destroyed a scheduled rotation, and reads
    `next_valid_from IS NOT NULL` as a proxy because 023 withholds `SELECT` on
    the hashes. `app.py:1760` names this exact row as the proxy's blind spot.
    After 028 the proxy is exact, because the two columns cannot disagree.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            _insert(connection, tenant_id, release_id, next_token_hash=OTHER_HASH)
        connection.rollback()


def test_a_rotation_start_time_without_a_hash_is_refused(
    seeded_database: dict[str, Any],
) -> None:
    """The other half: a promise with nothing behind it.

    Both halves are asserted because the constraint is an equality between two
    `IS NULL` tests, and a version of it that only caught one direction would
    still pass a case that only wrote one direction.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            _insert(
                connection,
                tenant_id,
                release_id,
                next_valid_from=datetime.now(UTC) + timedelta(hours=1),
            )
        connection.rollback()


def test_an_end_for_the_current_token_with_no_successor_is_refused(
    seeded_database: dict[str, Any],
) -> None:
    """A channel switching itself off while still reporting `active`.

    `overlap_ends_at` bounds the **outgoing** token (`adapter.py:62`), so this
    row serves traffic normally until that timestamp and then refuses every
    request, with nothing to take over and `status` still saying `active`.
    Turning a channel off is what `status = 'disabled'` is for; that path is
    reversible and audited, and this one is neither.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            _insert(
                connection,
                tenant_id,
                release_id,
                overlap_ends_at=datetime.now(UTC) + timedelta(days=1),
            )
        connection.rollback()


def test_a_gap_between_the_outgoing_and_incoming_tokens_is_refused(
    seeded_database: dict[str, Any],
) -> None:
    """**The case the constraint is named for: an overlap that does not overlap.**

    The outgoing token stops being accepted at `overlap_ends_at`; the incoming
    one starts at `next_valid_from`. Order them the wrong way round and there is
    an interval in which `current_allowed` is already false and
    `now >= next_valid_from` is not yet true -- **neither token authenticates**,
    the channel is dead, and the binding still reads `active`.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    now = datetime.now(UTC)
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            _insert(
                connection,
                tenant_id,
                release_id,
                next_token_hash=OTHER_HASH,
                next_valid_from=now + timedelta(days=2),
                overlap_ends_at=now + timedelta(days=1),
            )
        connection.rollback()


def test_a_successor_that_starts_before_the_current_token_did_is_refused(
    seeded_database: dict[str, Any],
) -> None:
    """A replacement cannot become valid before the thing it replaces did."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    now = datetime.now(UTC)
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            _insert(
                connection,
                tenant_id,
                release_id,
                current_valid_from=now,
                next_token_hash=OTHER_HASH,
                next_valid_from=now - timedelta(hours=1),
            )
        connection.rollback()


def test_a_well_formed_rotation_is_still_accepted(
    seeded_database: dict[str, Any],
) -> None:
    """**The half that keeps the constraint honest.**

    Four refusals and no acceptance would pass just as well against a constraint
    that rejected every rotation, and that constraint would be a worse bug than
    the gap it replaced -- it would make rotation impossible instead of merely
    unguarded. This is a real overlap: the incoming token starts before the
    outgoing one ends, which is the arrangement the columns exist to express.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    release_id = UUID(str(seeded_database["profile_release"]))
    now = datetime.now(UTC)
    with _migration_connection() as connection:
        _insert(
            connection,
            tenant_id,
            release_id,
            current_valid_from=now,
            next_token_hash=OTHER_HASH,
            next_valid_from=now + timedelta(days=1),
            overlap_ends_at=now + timedelta(days=2),
        )
        # And a binding with no rotation at all, which is every binding today.
        _insert(connection, tenant_id, release_id, current_valid_from=now)
        connection.rollback()
