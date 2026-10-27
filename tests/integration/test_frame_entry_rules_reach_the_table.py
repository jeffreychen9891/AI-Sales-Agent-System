"""The transition-side producer is reached, and it writes what the rule asked for.

**B4-2, independent review 2026-09-09 P2-2.** The batch creates
`scheduled_events` rows from two places -- the transition (for rules anchored on
`frame_entered_at`) and the dispatcher (for `last_outbound_at`) -- and only the
second had a test that went through the call site. The first was covered by
contract cases that call `drafts_for_anchor` directly, which proves the rule
computes correctly and proves **nothing about the wiring**: an anchor string
typed wrong, an `anchor_at` read from the wrong field, or an exception in
`_frame_entry_drafts` would all stay green, and the first profile to declare a
`frame_entered_at` rule would silently get no row.

That is the same argument the author wrote for the dispatcher side ("a test that
shows the code is correct without showing it is reached"), applied to the half
it did not cover.

**Why the profile is doctored here.** Neither shipped profile declares a
`frame_entered_at` rule -- that is exactly why this call site produces nothing
in production today -- so a test that used them could only assert the absence.
The `PreparedEvent` is built against a real aggregate row and the transition
runs its real SQL; the only thing substituted is the rule the profile declares.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.pipeline import PreparedEvent, _apply_aggregate_transition
from psycopg import Connection
from psycopg.rows import dict_row

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]

STALL_OFFSET_SECONDS = 600


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _seed_conversation(settings: Settings, seeded: dict[str, Any]) -> tuple[UUID, dict[str, Any]]:
    """One real customer message, so the aggregate exists with a real stack."""

    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    conversation_id = new_conversation(seeded["tenant_a"])
    _deliver_to_outbox(
        settings=settings, seeded_database=seeded, conversation_id=conversation_id
    )
    tenant_id = UUID(str(seeded["tenant_a"]))
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state, state_version, generation, profile_release_id"
            "  FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return conversation_id, dict(row)


def _profile_with_a_frame_entry_rule(definition_id: str) -> dict[str, Any]:
    return {
        "identity": {"version": "1.0.0"},
        "target_frames": [
            {
                "id": definition_id,
                "goal": "whatever the release says",
                "time_rules": [
                    {
                        "id": "entry-goal_stall",
                        "emit_kind": "goal_stall",
                        "due": {
                            "anchor": "frame_entered_at",
                            "offset_seconds": STALL_OFFSET_SECONDS,
                        },
                        "dedupe_template": "{emit_kind}:{target_frame_id}",
                        "cancel_on": ["customer_message"],
                        "strategy": "advance_goal",
                    }
                ],
            }
        ],
    }


def test_a_transition_writes_the_frame_entry_rules_row(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """Call site A, reached: the row exists and its `due_at` is off `entered_at`.

    The anchor is the assertion that matters. `frame_entered_at` and
    `last_outbound_at` are different instants, and a wiring mistake that read the
    wrong one would still produce a plausible row -- so this checks the value
    against the frame's own `entered_at`, which is in the stack this transition
    just wrote.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, row = _seed_conversation(settings, seeded_database)

    aggregate = dict(row["state"])
    frame = next(entry for entry in aggregate["target_stack"] if entry["status"] == "active")
    entered_at = datetime.fromisoformat(str(frame["entered_at"]).replace("Z", "+00:00"))
    previous_state_version = int(row["state_version"])
    previous_generation = int(row["generation"])
    aggregate["state_version"] = previous_state_version + 1
    aggregate["last_transition"] = dict(aggregate["last_transition"])
    aggregate["last_transition"]["transition_id"] = str(uuid4())

    prepared = PreparedEvent(
        inbox_id=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        # A timer envelope, so the transition does not bump the generation and
        # does not invalidate anything: this case is about the write, and a
        # customer message would mix the two.
        envelope={"event_type": "timer", "event_id": str(uuid4())},
        profile=_profile_with_a_frame_entry_rule(str(frame["definition_id"])),
        aggregate=aggregate,
        previous_state_version=previous_state_version,
        previous_generation=previous_generation,
    )

    database = Database(settings.worker_database_url)
    with database.transaction(tenant_id=tenant_id) as connection:
        _apply_aggregate_transition(connection, prepared)

    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT kind, due_at, dedupe_key, status, payload_ref, created_by_transition_id"
            "  FROM scheduled_events WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchall()

    assert len(rows) == 1, f"the transition-side producer wrote {len(rows)} rows: {rows}"
    written = dict(rows[0])
    assert written["kind"] == "goal_stall"
    assert written["status"] == "PENDING"
    assert written["dedupe_key"] == f"goal_stall:{frame['id']}"
    assert written["due_at"] == entered_at + timedelta(seconds=STALL_OFFSET_SECONDS), (
        "the row is anchored on something other than the frame's entered_at"
    )
    assert written["payload_ref"] == {"rule_id": "entry-goal_stall"}
    assert str(written["created_by_transition_id"]) == str(
        aggregate["last_transition"]["transition_id"]
    )


def test_a_profile_without_such_a_rule_writes_nothing_there(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The production case today, asserted rather than assumed.

    Both shipped profiles anchor every rule on `last_outbound_at`, so this call
    site writes nothing -- and the batch's claim that it is dormant rather than
    broken only means something if the two cases sit next to each other.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id, row = _seed_conversation(settings, seeded_database)

    aggregate = dict(row["state"])
    frame = next(entry for entry in aggregate["target_stack"] if entry["status"] == "active")
    profile = _profile_with_a_frame_entry_rule(str(frame["definition_id"]))
    profile["target_frames"][0]["time_rules"][0]["due"]["anchor"] = "last_outbound_at"
    previous_state_version = int(row["state_version"])
    aggregate["state_version"] = previous_state_version + 1

    prepared = PreparedEvent(
        inbox_id=uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        envelope={"event_type": "timer", "event_id": str(uuid4())},
        profile=profile,
        aggregate=aggregate,
        previous_state_version=previous_state_version,
        previous_generation=int(row["generation"]),
    )

    database = Database(settings.worker_database_url)
    with database.transaction(tenant_id=tenant_id) as connection:
        _apply_aggregate_transition(connection, prepared)

    with _migration_connection() as connection:
        count = connection.execute(
            "SELECT count(*) AS total FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_id, conversation_id),
        ).fetchone()

    assert count is not None and count["total"] == 0, (
        "a `last_outbound_at` rule must not be materialised by the transition -- "
        "that anchor does not exist until a message has been delivered"
    )
