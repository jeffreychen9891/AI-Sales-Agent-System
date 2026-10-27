"""The callback-deletion gauge, run against a real database rather than read.

**Why this file exists rather than another source-level assertion.** The streak
is computed in SQL (`stage_observability.INGRESS_SQL`), and the first version of
its case asserted that the query *mentioned* `inbox_events`. A reverse probe
then broke the accepted-message branch -- changed what it selects and inverted
its `WHERE` -- and the case stayed green, because the table name was still in
the string. An assertion that a query names a table is not an assertion that the
query uses it correctly.

So this drives the real query against the gate's Postgres. It is the only place
the arithmetic ADR-0103 §二.2 depends on is actually exercised: **how many
non-200s since the last 200**, where the provider deletes the callback address
at ten consecutive and the rule pages a person at five.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import Connection
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[2]


def _collector() -> Any:
    path = ROOT / "scripts" / "stage_observability.py"
    spec = importlib.util.spec_from_file_location("stage_observability", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _connection() -> Connection[Any]:
    # **`tuple_row`, not `dict_row`.** The query selects string literals with
    # no aliases, so every branch of the `UNION ALL` comes back under the same
    # `?column?` name and a dict would keep one of the four. Positions are what
    # the collector reads too -- it splits `psql -A` output on `|`.
    return Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=tuple_row
    )


def _answer(connection: Connection[Any], tenant_id: UUID, config_id: str, action: str) -> None:
    """One recorded answer, the shape `WeComProxyIngress.record_answer` writes."""

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES (%s, %s, NULL, %s, 'channel_binding', NULL, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            action,
            uuid4(),
            "WINDOW_NOT_OPEN" if action == "ingress.non_2xx" else "CHANNEL_AUTHENTICATION_FAILED",
            Jsonb({"config_id": config_id, "http_status": 503 if "non_2xx" in action else 200}),
        ),
    )


def _streak(connection: Connection[Any], config_id: str) -> float | None:
    for row in connection.execute(_collector().INGRESS_SQL).fetchall():
        if row[0] == "ingress_streak" and row[1] == config_id:
            return float(row[3])
    return None


def test_the_streak_counts_failures_since_the_last_two_hundred(
    settings: Any, seeded_database: dict[str, object]
) -> None:
    """Three failures, then a 200, then one more: the answer is 1, not 4.

    **Total and streak differ only after a recovery**, which is exactly the
    interval a rule built on the wrong one gets wrong: a system that failed
    nine times last week and is healthy now must not be sitting one failure
    away from an alert.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    config_id = f"streak-{uuid4()}"

    with _connection() as connection:
        for _ in range(3):
            _answer(connection, tenant_id, config_id, "ingress.non_2xx")
        connection.commit()
        assert _streak(connection, config_id) == 3.0

        # A refusal we mean is answered `200`, so it clears the streak even
        # though it is a refusal -- the provider counts status codes, not moods.
        _answer(connection, tenant_id, config_id, "ingress.refused")
        connection.commit()
        assert _streak(connection, config_id) == 0.0

        _answer(connection, tenant_id, config_id, "ingress.non_2xx")
        connection.commit()
        assert _streak(connection, config_id) == 1.0


def test_an_accepted_message_clears_the_streak_too(
    settings: Any, seeded_database: dict[str, object]
) -> None:
    """The commonest 200 of all leaves no audit row of its own.

    An accepted callback writes `inbox_events`, not an `ingress.answered` row,
    so a streak query that only reads the audit table would keep counting
    through a callback that is delivering messages perfectly well. **This is the
    branch a reverse probe broke while the source-level case stayed green**, and
    the reason this file exists.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    config_id = f"streak-{uuid4()}"

    with _connection() as connection:
        for _ in range(2):
            _answer(connection, tenant_id, config_id, "ingress.non_2xx")
        connection.commit()
        assert _streak(connection, config_id) == 2.0

        connection.execute(
            """
            INSERT INTO inbox_events(
                tenant_id, id, event_id, source, external_event_id, conversation_id,
                event_type, profile_release_id, aggregate_generation, envelope, config_id
            ) VALUES (%s, %s, %s, 'wecom_proxy', %s, %s, 'customer_message', %s, NULL, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                uuid4(),
                f"accepted-{uuid4()}",
                uuid4(),
                UUID(str(seeded_database["profile_release"])),
                Jsonb({"note": "streak fixture"}),
                config_id,
            ),
        )
        connection.commit()
        assert _streak(connection, config_id) == 0.0


def test_a_config_nobody_has_answered_for_has_no_series(
    settings: Any, seeded_database: dict[str, object]
) -> None:
    """No rows, no series -- not a zero.

    ADR-0101 §三.1: absent, not calm. A gauge that reported `0` for every
    `config_id` anybody ever asked about would also be a way to invent labels.
    """

    with _connection() as connection:
        assert _streak(connection, f"never-answered-{uuid4()}") is None
