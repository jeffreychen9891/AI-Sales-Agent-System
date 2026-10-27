"""T186's unfinished half: the token window is decided by the database's clock.

T186 is registered as fixed, and for what it was *about* it is. Its symptom was
a freshly seeded binding reporting itself as a credential fault, and its three
fixes -- splitting the error messages, pinning the fixture's `current_valid_from`
into the past, and failing loudly in `conftest` on a large offset -- all made
that symptom legible.

**None of them moved the comparison.** `accept()` went on testing database
instants (`current_valid_from`, `overlap_ends_at`, `next_valid_from`) against
`datetime.now(UTC)`, in the ingress authentication path. Both directions matter
and only one had ever been written down: a process running behind the database
refuses a binding that has just become valid, and a process running ahead
accepts a token before its window opens and keeps accepting a retired one past
`overlap_ends_at`. T193 measured this host's offset crossing zero in both
directions about every thirty seconds.
"""

from __future__ import annotations

import inspect
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

from tests.wecom_proxy_callbacks import callback

pytestmark = [pytest.mark.integration]


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def test_a_window_that_opened_at_this_instant_is_accepted(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The boundary, which is the only place the two clocks disagreed.

    The fixture pins `current_valid_from` an hour into the past, so it gives the
    same answer whichever clock decides -- which is exactly why it never caught
    this. Setting it to the database's own `now()` means the database has moved
    past it by the time `accept()` reads the row, so "open" is the only correct
    answer.

    **Under the old code this case failed whenever the process ran behind the
    database**, which is about half of every cycle here -- so it is a
    probabilistic detector of the defect and a deterministic guard against its
    return: with the decision made in SQL it cannot fail for clock reasons at
    all.
    """

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET current_valid_from = now(), overlap_ends_at = NULL"
        )
        connection.commit()

    ingress = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    )
    result = ingress.accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"window-boundary-{uuid4()}",
            sender_id="customer-1",
            text="the window opened on the database's clock",
        ),
    )

    assert result.duplicate is False


def test_the_window_is_decided_in_sql_and_not_against_a_process_clock() -> None:
    """Stated over the source because the property is *which clock decided*.

    The behavioural case above can only catch the defect when the offset happens
    to point the wrong way at that moment. This one cannot miss, and it is the
    reason the pair exists: neither half is sufficient alone.
    """

    source = inspect.getsource(WeComProxyIngress.accept)
    assert "AS current_allowed" in source
    assert "AS next_window_open" in source
    # And on the database's *current* instant, not its transaction's start: this
    # is a gate, and `now()` would let a window outlive itself by however long
    # the transaction asking happens to run (the rule `_authenticate` follows).
    assert "clock_timestamp()" in source
    assert "AS MATERIALIZED" in source, (
        "the deciding instant must be taken once; clock_timestamp() advances "
        "within a statement, so an unmaterialised CTE could compare three "
        "different instants and report a fourth"
    )
    assert 'binding["current_allowed"]' in source
    assert 'binding["next_window_open"]' in source
    # The process clock may still be *reported*; it may not decide.
    for decided_by_process in (
        'now >= binding["current_valid_from"]',
        'now <= binding["overlap_ends_at"]',
        'now >= binding["next_valid_from"]',
    ):
        assert decided_by_process not in source, (
            f"{decided_by_process} puts an authentication decision across two clocks"
        )
