"""The kept copy is the one that arrived, read back out of the table.

**T205 / B3-4d.** `tests/contracts/test_raw_payload_has_no_consumer.py` proves
`retained_raw_payload()` picks the right value and that nothing reads the
column. Neither claim reaches the one that matters here: `accept()` could
compute the value perfectly and never put it in the INSERT, and every case over
there would still pass. That is the shape B3-2b's own integration file was
written for -- "a test that shows the code is correct without showing it is
reached" -- and this is the same argument applied to a field that, by design,
has no reader to notice its absence.

**The absence of a reader is exactly why this file has to exist.** A stored
value nobody consumes cannot fail loudly later; if the INSERT drops it, the
first symptom is the day someone goes looking for the original of a
mis-parsed message and finds NULLs going back months.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import callback_body, callback_item

pytestmark = [pytest.mark.integration]

#: The vendor's own example value, from the `msgType=2001` read receipt in
#: `docs/wecom_proxy.md`. Twelve base64 characters -- which is the measurement
#: that made ADR-0103 §五 item 4 rule out the two expensive storage tiers.
VENDOR_SAMPLE = "CAMQ0e+yBA=="


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _accept(settings: Settings, seeded: dict[str, Any], extra: dict[str, Any]) -> Any:
    # `extra` lands on the **element**, which is where `base64RawData` lives
    # in a real callback -- the top level of the body has only `code` / `msg` /
    # `data`, so retaining "the raw payload" from there retained nothing.
    body = callback_body(
        callback_item(
            external_event_id=f"raw-{uuid4()}",
            sender_id="customer-1",
            msg_type=0,
            msg_data={"content": "hello"},
            **extra,
        )
    )
    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    return ingress.accept(
        config_id="config-a", path_token=str(seeded["channel_token"]), payload=body
    )


def _stored_row(event_id: Any) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT provider_raw_data, envelope FROM inbox_events WHERE event_id = %s",
            (event_id,),
        ).fetchone()
    assert row is not None, "the message did not arrive at all"
    return dict(row)


def test_the_raw_copy_is_stored_verbatim(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    result = _accept(settings, seeded_database, {"base64RawData": VENDOR_SAMPLE})
    assert result.accepted

    row = _stored_row(result.event_id)
    assert row["provider_raw_data"] == VENDOR_SAMPLE
    # And it did **not** take the other route into the record. The envelope is
    # what the message page selects (033) and what the model sees; an unread
    # blob riding there is the arrangement the user's 2026-09-09 ruling
    # rejected.
    assert "base64RawData" not in row["envelope"]


def test_a_callback_without_it_stores_null_and_is_still_a_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Most callbacks will not carry it, and that is not a degraded message.

    NULL therefore has to mean "the provider sent none", not "something went
    wrong" -- which is what the column comment says and what this pins.
    """

    result = _accept(settings, seeded_database, {})
    assert result.accepted

    row = _stored_row(result.event_id)
    assert row["provider_raw_data"] is None
    assert row["envelope"]["content"]["text"] == "hello"


def test_a_value_the_database_cannot_hold_costs_the_copy_and_not_the_message(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The degradation asserted where the database is real.

    A NUL byte in a `text` value makes `psycopg` raise
    `UntranslatableCharacter`. Unhandled, that leaves `accept()` as a 500, the
    provider redelivers into the same crash, and ten of those delete the
    callback address (T204). The unit case proves the helper returns `None`;
    only this one proves the message still lands, with its content intact, in a
    real PostgreSQL.
    """

    result = _accept(settings, seeded_database, {"base64RawData": "CAMQ\x00BA=="})
    assert result.accepted

    row = _stored_row(result.event_id)
    assert row["provider_raw_data"] is None
    assert row["envelope"]["content"]["text"] == "hello"


def test_the_disclaimer_is_on_the_column_in_the_database(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Not in the migration file -- in the catalogue a person actually reads.

    `\\d+ inbox_events` reads `pg_description`, not
    `047_inbound_raw_payload.sql`. The contract test asserts the migration says
    it; this asserts the database does, which is the only version anybody meets
    by accident.
    """

    with _migration_connection() as connection:
        row = connection.execute(
            """
            SELECT col_description(attrelid, attnum) AS comment
              FROM pg_attribute
             WHERE attrelid = 'public.inbox_events'::regclass
               AND attname = 'provider_raw_data'
            """
        ).fetchone()

    assert row is not None, "the column is not there at all"
    comment = str(row["comment"] or "")
    assert "ZERO CONSUMERS TODAY, BY DESIGN" in comment
    assert "T205" in comment
    # **The third anchor** (independent review 2026-09-09, P3-3): the reader who
    # meets this column in `\d+` must learn that NULL has a third meaning --
    # a withdrawal never reaches this table at all -- or they will read
    # "withdrawals keep no copy" as "withdrawals never carried the field".
    assert "NEVER REACHED THIS TABLE" in comment
