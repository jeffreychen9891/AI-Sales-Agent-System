"""Repin against the real database and the real roles (T15, RFC 9.1).

The exit item "jewellery Stage single target frame" needs a binding that can be
pointed at a published release. Before this, `channel_bindings.profile_release_id`
had no writer outside the test fixture.

Everything here runs as `fastagent_app`, not as the migration role: the claims
are about what the application role may do.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.profiles import ProfileRegistry, canonical_hash
from fastagent_v2.profiles.binding import (
    ChannelBindingDisabled,
    ChannelBindingNotFound,
    ProfileReleaseNotFound,
    _repin_live_conversations,
    repin_channel_binding,
)
from fastagent_v2.profiles.protocol import ProfilePackage
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor, _initial_aggregate
from psycopg import Connection
from psycopg.errors import CheckViolation, InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration]


def _binding_credential(tenant_id: UUID) -> dict[str, Any] | None:
    """`status` and the callback hash, read through the migration connection.

    Not through the application role: 023 withholds `SELECT` on the hash from
    `fastagent_app` on purpose, which is exactly why the guard this feeds has to
    live at the behaviour layer rather than in the grant.
    """

    with _migration_connection() as connection:
        return connection.execute(
            "SELECT status, current_token_hash FROM channel_bindings"
            " WHERE tenant_id = %s AND config_id = 'config-a'",
            (tenant_id,),
        ).fetchone()


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _next_release(*, database: Database, tenant_id: UUID, drop_frame: str | None = None) -> Any:
    """Publish a second release: same profile id, later version.

    `drop_frame` removes a target frame definition, which is how the "the goal
    this conversation is pursuing no longer exists" path is reached.
    """

    package = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    )
    document = copy.deepcopy(package.document)
    document["identity"]["version"] = "1.1.0"
    document["identity"]["release_id"] = str(uuid4())
    if drop_frame is not None:
        document["target_frames"] = [
            frame for frame in document["target_frames"] if frame["id"] != drop_frame
        ]
    document["identity"]["content_hash"] = canonical_hash(document)
    published = publish_release(
        database=database,
        tenant_id=tenant_id,
        package=ProfilePackage(
            document=document,
            content_hash=str(document["identity"]["content_hash"]),
            source_path="tests/in-memory",
        ),
    )
    return published.release


def _seed_conversation(
    *,
    tenant_id: UUID,
    release_id: UUID,
    definition_id: str = "welcome",
    config_id: str | None = "config-a",
) -> UUID:
    """One open conversation with a live frame and one pending timer.

    **Production shape, not a convenient one.** The first version filled only the
    `target_stack` column and left `state` at its `'{}'` default -- and that is
    precisely why eight integration tests and five reverse probes all passed
    while the repin never wrote the document the engine actually reads
    (independent review 2026-09-03, P1-2). A fixture whose shape differs from the
    pipeline's makes the difference between document and projection
    unobservable, which is the one thing these tests had to be able to see.

    The aggregate is therefore built by the pipeline's own `_initial_aggregate`
    and written the way `_apply_aggregate_transition` writes it: the whole
    aggregate into `state`, four fields mirrored into columns.
    """

    # **Derived, not invented** (2026-09-10): the conversation id is a function
    # of who is speaking, so a seeded conversation that a later callback is
    # supposed to land in has to be the one *that customer's* messages derive.
    # A `uuid4()` here would seed a conversation the ingress can never reach.
    conversation_id = new_conversation(tenant_id)
    profile = ProfileRegistry(root=ROOT / "profiles", relative_to=ROOT).package(
        profile_id="synthetic-service-desk", version="1.0.4"
    ).document
    aggregate = _initial_aggregate(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        profile_release_id=release_id,
        profile=profile,
    )
    aggregate["generation"] = 4
    aggregate["state_version"] = 9
    # Production shape includes the binding link since 008; `config_id=None`
    # reproduces a conversation created before it.
    aggregate["config_id"] = config_id
    frame = aggregate["target_stack"][0]
    frame["definition_id"] = definition_id
    frame["goal"] = "seeded goal"
    frame["frame_generation"] = 4
    frame["resume_confirmation_policy"] = "required"
    # T25 provenance, seeded so a repin can be observed preserving it.
    frame["slots"] = {"request_summary": "seeded before the repin"}
    frame["slot_evidence"] = {
        "request_summary": {
            "confidence": 0.95,
            "evidence_event_id": str(uuid4()),
            "recorded_at": "2026-09-03T00:00:00+00:00",
        }
    }
    frame_id = UUID(str(frame["id"]))
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, generation, state_version,
                target_stack, state, config_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                conversation_id,
                release_id,
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                Jsonb(aggregate),
                config_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id, status
            ) VALUES (%s, %s, %s, now() + interval '1 hour', 'silence_check', %s,
                      %s, 4, %s, 9, %s, 'PENDING')
            """,
            (tenant_id, uuid4(), conversation_id, f"dedupe-{conversation_id}",
             release_id, frame_id, uuid4()),
        )
        connection.commit()
    return conversation_id


def _binding_release(tenant_id: UUID) -> UUID:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id FROM channel_bindings"
            " WHERE tenant_id = %s AND config_id = 'config-a'",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return UUID(str(row["profile_release_id"]))


def test_repin_points_the_binding_at_the_new_release_and_audits_it(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The write path `channel_bindings.profile_release_id` never had."""

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    release = _next_release(database=database, tenant_id=tenant_a)
    # Captured rather than compared against a literal, so the assertion below
    # stays true if the fixture ever seeds a different token.
    credential_before = _binding_credential(tenant_a)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
    )

    assert result.changed is True
    assert result.previous_release_id == previous
    assert _binding_release(tenant_a) == release.release_id
    # **The behaviour-layer half of what migration 024 took away** (independent
    # review 2026-09-05, P2-2). `test_the_repin_grant_cannot_rotate_credentials_
    # or_close_conversations` used to assert that this connection *could not*
    # write `status` or `current_token_hash`; 024 grants both, so those two
    # clauses had to go, and with them the only coverage of "a repin does not
    # quietly disable a channel or swap its credential". This asserts the same
    # property one layer down, where no `GRANT` can take it away.
    assert _binding_credential(tenant_a) == credential_before
    with _migration_connection() as connection:
        audit = connection.execute(
            """
            SELECT reason_code, resource_type, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'channel_binding.repin'
            """,
            (tenant_a,),
        ).fetchall()
    assert [row["reason_code"] for row in audit] == ["CHANNEL_BINDING_REPINNED"]
    assert audit[0]["evidence"]["from_release_id"] == str(previous)
    assert audit[0]["evidence"]["to_release_id"] == str(release.release_id)
    assert audit[0]["evidence"]["config_id"] == "config-a"


def test_existing_conversations_keep_their_pin_unless_asked(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """RFC 9.1: "existing conversations keep their pin by default".

    Rebinding the channel decides what *new* conversations get. Moving a live
    conversation onto different goals mid-flight is a separate decision, and the
    default must not make it silently.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
    )

    assert result.conversations == ()
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id, generation, state_version, state"
            " FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
        events = connection.execute(
            "SELECT status FROM scheduled_events WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_a, conversation_id),
        ).fetchall()
    assert row is not None
    assert UUID(str(row["profile_release_id"])) == previous
    assert row["generation"] == 4
    assert row["state_version"] == 9
    # The document is untouched too, not just its projection.
    assert row["state"]["generation"] == 4
    assert UUID(str(row["state"]["profile_release_id"])) == previous
    assert [event["status"] for event in events] == ["PENDING"]


def test_an_explicit_repin_bumps_repairs_invalidates_and_audits(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """All four of RFC 9.1's requirements, or the operation is not a repin.

    A generation bump without invalidation leaves timers that will fire against
    a stack they no longer describe; invalidation without the bump leaves the
    next timer to be scheduled under a generation that already moved.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )

    assert [item.conversation_id for item in result.conversations] == [conversation_id]
    moved = result.conversations[0]
    assert moved.generation == 5
    assert moved.handed_to_human is False
    assert moved.invalidated_events == 1

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id, generation, state_version, control_mode,"
            " target_stack, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
        events = connection.execute(
            "SELECT status, invalidation_reason FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_a, conversation_id),
        ).fetchall()
        audit = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'conversation.repin'
            """,
            (tenant_a,),
        ).fetchall()
    assert row is not None
    assert UUID(str(row["profile_release_id"])) == release.release_id
    assert row["generation"] == 5
    assert row["state_version"] == 10
    assert row["control_mode"] == "ai"
    # P0-2: the top frame keeps `active`. `repair_pending` on the top frame has
    # no legal transition back -- the validator rejects every operation, and
    # `safe_fallback_plan` hard-codes the one it rejects first.
    assert [frame["status"] for frame in row["target_stack"]] == ["active"]
    # **The frame pin does not move** (ADR-0098 §二, B4-3). This read `== [5]`
    # while `frame_generation` was a last-touched counter; it is now written once
    # when the frame is created and never again, and a repin re-grounds the
    # frames that exist rather than creating new ones. What the repin does move
    # is the **aggregate** generation, asserted above, and that is what the
    # invalidation below keys on.
    assert [frame["frame_generation"] for frame in row["target_stack"]] == [4]
    # P0-1: the document is what the engine reads, so every claim above has to
    # hold there as well. A projection-only repin is invisible to the worker.
    document = row["state"]
    assert document["generation"] == 5
    assert document["state_version"] == 10
    assert UUID(str(document["profile_release_id"])) == release.release_id
    assert document["control_mode"] == "ai"
    assert [frame["status"] for frame in document["target_stack"]] == ["active"]
    # Same pin, read out of the document the engine actually consumes.
    assert [frame["frame_generation"] for frame in document["target_stack"]] == [4]
    assert [(e["status"], e["invalidation_reason"]) for e in events] == [
        ("INVALIDATED", "PROFILE_RELEASE_REPINNED")
    ]
    assert [entry["reason_code"] for entry in audit] == ["PROFILE_RELEASE_REPINNED"]
    assert audit[0]["evidence"]["invalidated_events"] == 1
    assert audit[0]["evidence"]["generation"] == 5


def test_a_conversation_whose_goal_the_new_release_dropped_goes_to_a_human(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """There is no honest automatic answer, so the conversation gets a person."""

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(
        tenant_id=tenant_a, release_id=previous, definition_id="clarify_request"
    )
    release = _next_release(
        database=database, tenant_id=tenant_a, drop_frame="clarify_request"
    )

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )

    assert result.conversations[0].handed_to_human is True
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT control_mode, target_stack, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT reason_code, evidence FROM audit_events
             WHERE tenant_id = %s AND action = 'conversation.repin'
            """,
            (tenant_a,),
        ).fetchall()
    assert row is not None
    assert row["control_mode"] == "human"
    # The frame is left as it was: the human receives the state that actually
    # existed, not a repaired version of a goal the profile no longer defines.
    assert [frame["status"] for frame in row["target_stack"]] == ["active"]
    # And the handoff has to be in the document, because `process_prepared`
    # reads `state["control_mode"]` -- a projection-only handoff does not stop
    # the model from being asked for an intent (P0-1).
    assert row["state"]["control_mode"] == "human"
    assert audit[0]["reason_code"] == "PROFILE_RELEASE_REPINNED_UNKNOWN_FRAME"
    assert audit[0]["evidence"]["unknown_definition_ids"] == ["clarify_request"]


def test_repinning_to_the_release_already_in_force_changes_nothing(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """An operator retrying after a timeout must not bump every generation again."""

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(tenant_id=tenant_a, release_id=previous)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=previous,
        repin_live_conversations=True,
    )

    assert result.changed is False
    assert result.conversations == ()
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT generation, state_version, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
        audit = connection.execute(
            "SELECT reason_code FROM audit_events WHERE tenant_id = %s"
            " AND action = 'channel_binding.repin'",
            (tenant_a,),
        ).fetchall()
    assert row is not None
    assert (row["generation"], row["state_version"]) == (4, 9)
    assert (row["state"]["generation"], row["state"]["state_version"]) == (4, 9)
    # The no-op is still recorded: "somebody tried" is a fact worth keeping.
    assert [entry["reason_code"] for entry in audit] == ["CHANNEL_BINDING_ALREADY_PINNED"]


def test_an_unknown_binding_or_release_is_refused_by_name(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The operator is told which of the two ids was wrong, not which constraint fired."""

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    release_id = UUID(str(seeded_database["profile_release"]))

    with pytest.raises(ChannelBindingNotFound):
        repin_channel_binding(
            database=database,
            tenant_id=tenant_a,
            config_id="config-does-not-exist",
            release_id=release_id,
        )
    with pytest.raises(ProfileReleaseNotFound):
        repin_channel_binding(
            database=database,
            tenant_id=tenant_a,
            config_id="config-a",
            release_id=uuid4(),
        )


@pytest.mark.security
def test_the_repin_grant_cannot_rotate_credentials_or_close_conversations(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """007 claims its grants are column-scoped. This is what holds that claim.

    Being allowed to point a binding at a different release must not carry
    conversation closure, timer creation or row deletion along with it. Those are
    separate operations with their own routes, audit reasons and blast radius.
    Without this test the migration's comment is the only thing standing between
    a repin grant and a table-level UPDATE.

    **Two statements left this list in B2 #5 (migration 024), and the removal is
    the third instance of one pattern this repository now has a name for.**
    `UPDATE ... current_token_hash` and `UPDATE ... status` are exactly what
    `POST /channel-bindings/{config_id}/deactivate` has to do, so the grant had
    to be issued -- and **a column-level `GRANT` belongs to a role, not to a
    route**. `fastagent_app` is the role every API route shares, so from 024 the
    connection *this* route uses can also write those two columns; what stops it
    is that its statement does not name them, which is one defence rather than
    two. Registered as **T127**, which now has three instances: `employees`'
    credential columns (021), and these two.

    What is asserted below is therefore what is still **structurally**
    impossible, not what today's SQL happens to avoid.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    forbidden = (
        ("conversation_aggregates", "UPDATE conversation_aggregates SET lifecycle = 'closed'"
         " WHERE tenant_id = %s", (tenant_a,)),
        ("channel_bindings", "DELETE FROM channel_bindings"
         " WHERE tenant_id = %s AND config_id = 'config-a'", (tenant_a,)),
    )
    for table, statement, parameters in forbidden:
        with pytest.raises(InsufficientPrivilege), database.transaction(
            tenant_id=tenant_a
        ) as connection:
            connection.execute(statement, parameters)
        assert table  # names the surface each statement is about

    # Invalidating a timer is granted; creating one is not.
    with pytest.raises(InsufficientPrivilege), database.transaction(
        tenant_id=tenant_a
    ) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_events(
                tenant_id, id, conversation_id, due_at, kind, dedupe_key,
                profile_release_id, aggregate_generation, target_frame_id,
                target_stack_version, created_by_transition_id
            ) VALUES (%s, %s, %s, now(), 'silence_check', 'x', %s, 0, %s, 0, %s)
            """,
            (tenant_a, uuid4(), uuid4(), UUID(str(seeded_database["profile_release"])),
             uuid4(), uuid4()),
        )


def test_a_tenant_cannot_repin_another_tenants_binding(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """RLS, not application filtering, is what makes this true.

    Tenant B holds a binding-shaped row of its own only in the sense that
    `config-a` belongs to tenant A; asking as tenant B must not find it.
    """

    tenant_b = UUID(str(seeded_database["tenant_b"]))
    database = Database(settings.database_url)
    release_id = UUID(str(seeded_database["profile_release"]))

    with pytest.raises(ChannelBindingNotFound):
        repin_channel_binding(
            database=database,
            tenant_id=tenant_b,
            config_id="config-a",
            release_id=release_id,
        )


def test_the_worker_can_process_an_event_after_a_repin(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """The one evidence shape that pins P0-1 and P0-2 at the same time.

    Both defects were invisible to every other test in this file. P0-1 (writing
    only the projection) shows up as an `AggregateConflict` on the next event,
    forever, because the CAS compares the document's version against the
    column's. P0-2 (a `repair_pending` top frame) shows up as a plan rejection no
    fallback can satisfy, burning the retry budget until the event is `failed`.
    Neither is observable by reading rows back; both are observable by letting
    the pipeline take one more event.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )

    queue = PointerQueue(settings.queue_redis_url)
    WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"after-repin-{conversation_id}",
            sender_id=sender_of(conversation_id),
            text="still here after the repin",
        ),
    )
    result = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=DeterministicMockInference(),
        worker_id="repin-followup",
    ).process_one(block_ms=2000)

    assert result is not None
    with _migration_connection() as connection:
        inbox = connection.execute(
            "SELECT status, attempt, error_code FROM inbox_events"
            " WHERE tenant_id = %s AND conversation_id = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_a, conversation_id),
        ).fetchone()
        row = connection.execute(
            "SELECT generation, state_version, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
    assert inbox is not None
    # Not left pending with a bumped attempt, and not stuck: it was consumed.
    assert inbox["status"] == "consumed"
    assert inbox["error_code"] is None
    assert row is not None
    # The document moved again, which is only possible if the CAS matched --
    # that is, if the repin left document and projection agreeing.
    assert row["state"]["state_version"] == row["state_version"]
    assert row["state"]["generation"] == row["generation"]
    assert row["state_version"] > 10


def test_live_conversations_can_be_repinned_after_the_binding_already_moved(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """P2-1: the two operations have separate idempotency bases.

    The ordinary operator sequence is "rebind now, decide about the live ones
    afterwards". The first version answered the second call with
    `changed=False, conversations=()` and left them stranded, because it treated
    "the binding already points there" as "there is nothing to do".
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    conversation_id = _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    first = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
    )
    assert first.changed is True
    assert first.conversations == ()

    second = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )
    assert second.changed is False
    assert [item.conversation_id for item in second.conversations] == [conversation_id]

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
    assert row is not None
    assert UUID(str(row["profile_release_id"])) == release.release_id
    assert UUID(str(row["state"]["profile_release_id"])) == release.release_id


def test_one_repin_moves_at_most_its_limit_and_reports_the_remainder(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """P2-2: the lock footprint is a decision, not a function of tenant size.

    The selection takes `FOR UPDATE` on every matching row and the worker's
    `prepare()` wants those same rows, so an unbounded repin stops that tenant's
    processing for its whole duration.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    for _ in range(3):
        _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    first = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
        limit=2,
    )
    assert len(first.conversations) == 2
    assert first.remaining_conversations == 1

    second = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
        limit=2,
    )
    assert len(second.conversations) == 1
    assert second.remaining_conversations == 0


@pytest.mark.security
def test_the_repin_grant_cannot_read_channel_credentials(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """P1-1: the SELECT face of 007, which had no assertion at all.

    001 deliberately gave ingress EXECUTE on a SECURITY DEFINER function rather
    than a table read, so that no role-level read would reach the token hashes.
    A table-level GRANT SELECT in 007 quietly undid that for the API role, and
    both the security test and reverse probe O only looked at the UPDATE face.

    **`status` left this list in B2 #1 (migration 023), and the removal is not a
    weakening of what this case is about.** `GET /channel-bindings` has to
    publish whether a binding is active, so the app role now holds
    `SELECT (status)` -- along with `source`, the rotation timestamps and
    `created_at`. What this case exists for is the **credential** face, and that
    is unchanged and unchangeable: the two token hashes are not readable by this
    role, and they are not redacted tokens but the verifier the ingress path
    compares against. `provider_instance_id` is unreadable too (T123) and is
    asserted by
    `tests/integration/test_channel_binding_list_route.py::test_the_grant_stops_at_the_columns_this_route_returns`.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    # **Two kinds, and the difference has a half-life** (B2 #1 审查 P3-2).
    #
    # `never_readable` is permanent: these two are the verifier the ingress path
    # compares a presented token against, so no batch may ever grant them, and a
    # red here is a defect.
    #
    # `not_today` is a registration, not a law. **T123** says granting
    # `provider_instance_id` is the outbound wiring batch's decision -- so that
    # batch may legitimately make this line red, and the person who sees it
    # should know they are looking at an expiring entry rather than at the
    # credential guarantee. Without this split they would face the same choice
    # B2 #1 faced with `status`, and the two cases look identical in a diff.
    never_readable = ("current_token_hash", "next_token_hash")
    not_today = ("provider_instance_id",)
    for column in (*never_readable, *not_today):
        with pytest.raises(InsufficientPrivilege), database.transaction(
            tenant_id=tenant_a
        ) as connection:
            connection.execute(
                f"SELECT {column} FROM channel_bindings WHERE tenant_id = %s",  # noqa: S608
                (tenant_a,),
            )
    # The columns the repin needs, plus the ones B2 #1's list route added.
    with database.transaction(tenant_id=tenant_a) as connection:
        readable = connection.execute(
            "SELECT tenant_id, config_id, profile_release_id, status, source,"
            " current_valid_from FROM channel_bindings WHERE tenant_id = %s",
            (tenant_a,),
        ).fetchall()
    assert readable


def test_a_disabled_binding_cannot_be_repinned(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """**T142**, ruled by the user on 2026-09-05.

    Three routes share `channel_config.update`, and until this they disagreed
    about a stopped binding: `PATCH /channel-bindings/{config_id}` answers 409,
    repin did not look at `status` at all. The disagreement was the defect --
    a repin onto a binding whose callback token B2 #5 destroyed is a write that
    can never take effect, and M1a has no route to bring the binding back.

    The row is disabled through the migration connection rather than through the
    deactivate route, so this case says what it means even if that route changes.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    release = _next_release(database=database, tenant_id=tenant_a)
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE channel_bindings SET status = 'disabled' WHERE config_id = 'config-a'"
        )
        connection.commit()
    before = _binding_release(tenant_a)

    with pytest.raises(ChannelBindingDisabled):
        repin_channel_binding(
            database=database,
            tenant_id=tenant_a,
            config_id="config-a",
            release_id=release.release_id,
        )

    assert _binding_release(tenant_a) == before, "a refused repin writes nothing"


@pytest.mark.security
def test_a_capped_repin_leaves_the_conversations_it_skipped_unlocked(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """T26. The cap has to be on the statement, not on the loop.

    The first version selected every matching row `FOR UPDATE` and sliced in
    Python, so bounding the writes shortened the outage without shrinking it:
    one repin still locked every open conversation of the tenant that was not
    already on the target release, and the worker's `prepare()` wants exactly
    those rows.

    This drives the **real** `_repin_live_conversations` inside a transaction the
    test holds open, then asks a second connection to lock the conversation the
    repin skipped. `NOWAIT` turns "would block" into an error rather than a hang,
    which is what makes this an assertion instead of a timeout.

    The first version of this test built its own `SELECT ... LIMIT 1 FOR UPDATE`
    inline and locked the skipped row from another connection -- which tested
    PostgreSQL, not this module, and stayed green when the `LIMIT` was removed
    from the code. Same defect this session kept finding elsewhere: asserting
    against a state the test constructed rather than one the code produced.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    ids = sorted(
        _seed_conversation(tenant_id=tenant_a, release_id=previous) for _ in range(2)
    )
    skipped = ids[1]  # `ORDER BY id` moves the smaller one first
    release = _next_release(database=database, tenant_id=tenant_a)

    with database.transaction(tenant_id=tenant_a) as connection:
        moved, remaining, unlinked = _repin_live_conversations(
            connection=connection,
            tenant_id=tenant_a,
            actor_id=None,
            correlation_id=uuid4(),
            previous_release_id=previous,
            release=release,
            occurred_at="2026-09-03T00:00:00Z",
            limit=1,
            config_id="config-a",
        )
        assert [item.conversation_id for item in moved] == [ids[0]]
        assert remaining == 1
        assert unlinked == 0

        # Still inside the repin transaction, so its locks are still held.
        with Connection.connect(
            os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
        ) as other:
            locked = other.execute(
                "SELECT id FROM conversation_aggregates"
                " WHERE tenant_id = %s AND id = %s FOR UPDATE NOWAIT",
                (tenant_a, skipped),
            ).fetchone()
            assert locked is not None
            assert UUID(str(locked["id"])) == skipped


def test_the_remainder_is_counted_rather_than_derived_from_the_page_size(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """With the cap in SQL there is no full row set to subtract from.

    The remainder is counted after the writes, so it reports what is genuinely
    left rather than arithmetic on a page that was never fetched.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    for _ in range(3):
        _seed_conversation(tenant_id=tenant_a, release_id=previous)
    release = _next_release(database=database, tenant_id=tenant_a)

    first = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
        limit=1,
    )
    assert len(first.conversations) == 1
    assert first.remaining_conversations == 2

    second = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
        limit=5,
    )
    assert len(second.conversations) == 2
    assert second.remaining_conversations == 0


@pytest.mark.security
def test_a_repin_moves_only_the_conversations_of_the_binding_it_names(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """T24. The over-capture was never a rule anyone chose.

    `conversation_aggregates` recorded which *release* a conversation pinned and
    nothing recorded which binding it arrived through, so "by release" was the
    only query the schema could answer -- and after P2-1 widened it to "anything
    not already on the target", one binding's repin swept the whole tenant.
    Migration 008 records the link; this is the rule it makes expressible.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    mine = _seed_conversation(tenant_id=tenant_a, release_id=previous, config_id="config-a")
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES ('config-b', %s, %s, %s)
            """,
            (tenant_a, previous, "b" * 64),
        )
        connection.commit()
    theirs = _seed_conversation(tenant_id=tenant_a, release_id=previous, config_id="config-b")
    release = _next_release(database=database, tenant_id=tenant_a)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )

    assert [item.conversation_id for item in result.conversations] == [mine]
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT id, profile_release_id, config_id, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = ANY(%s)",
            (tenant_a, [mine, theirs]),
        ).fetchall()
    pinned = {UUID(str(row["id"])): UUID(str(row["profile_release_id"])) for row in rows}
    assert pinned[mine] == release.release_id
    # The other binding's conversation did not move. Before 008 it would have.
    assert pinned[theirs] == previous
    # The link survives the repin in *both* places. The repin's UPDATE does not
    # write `config_id`; the document keeps it only because
    # `repair_target_stack_for_repin` deep-copies the state it was handed. That
    # is true today and nothing was watching it -- rewriting the repair to
    # rebuild the dict would drop the link from the document, leave the column
    # standing, and pass every test in this file (independent review P3-5).
    # P0-1 of T15 was exactly this divergence, found in production behaviour
    # rather than by a test.
    for row in rows:
        assert row["state"]["config_id"] == row["config_id"]
        # T25/P3-3: a repin re-grounds the stack, and the provenance survives it
        # only because `repair_target_stack_for_repin` deep-copies the frame.
        # Rebuilding the frame field by field would wipe the whole evidence
        # chain without a single test going red.
        evidence = row["state"]["target_stack"][0]["slot_evidence"]
        assert evidence["request_summary"]["confidence"] == 0.95


@pytest.mark.security
def test_a_conversation_with_no_binding_link_is_reported_not_swept(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """Rows created before 008 carry no link and cannot be attributed.

    Moving them would be the same guess this batch removes; skipping them
    silently would let an operator read `remaining=0` as "everything moved".
    They are counted and reported instead.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    database = Database(settings.database_url)
    previous = UUID(str(seeded_database["profile_release"]))
    linked = _seed_conversation(tenant_id=tenant_a, release_id=previous, config_id="config-a")
    legacy = _seed_conversation(tenant_id=tenant_a, release_id=previous, config_id=None)
    release = _next_release(database=database, tenant_id=tenant_a)

    result = repin_channel_binding(
        database=database,
        tenant_id=tenant_a,
        config_id="config-a",
        release_id=release.release_id,
        repin_live_conversations=True,
    )

    assert [item.conversation_id for item in result.conversations] == [linked]
    assert result.remaining_conversations == 0
    assert result.unlinked_conversations == 1
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile_release_id FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, legacy),
        ).fetchone()
    assert row is not None
    assert UUID(str(row["profile_release_id"])) == previous


@pytest.mark.security
def test_the_binding_link_is_carried_from_ingress_to_the_aggregate(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    """End to end: the link is written where it is known and read where it is needed.

    Ingress authenticates a `config_id` and puts it on the inbox event; the
    worker copies it onto the aggregate it creates from that event. Asserting
    the two ends separately would leave the join untested -- which is the gap
    that made this whole registration necessary.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_a)
    queue = PointerQueue(settings.queue_redis_url)
    WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id=f"link-{conversation_id}",
            sender_id=sender_of(conversation_id),
        ),
    )
    with _migration_connection() as connection:
        inbox = connection.execute(
            "SELECT config_id FROM inbox_events WHERE tenant_id = %s AND conversation_id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
    assert inbox is not None
    assert inbox["config_id"] == "config-a"

    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=DeterministicMockInference(),
        worker_id="link-carrier",
    ).process_one(block_ms=2000)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT config_id, state FROM conversation_aggregates"
            " WHERE tenant_id = %s AND id = %s",
            (tenant_a, conversation_id),
        ).fetchone()
    assert row is not None
    assert row["config_id"] == "config-a"
    # Column and document agree -- the T15/P0-1 rule.
    assert row["state"]["config_id"] == "config-a"


@pytest.mark.security
def test_a_config_id_longer_than_the_contract_allows_is_rejected_at_creation(
    seeded_database: dict[str, object],
) -> None:
    """P3-3. A limit only one side believes in wedges conversations.

    The aggregate contract bounds `config_id` at 128 and the database bounded it
    nowhere, so a longer binding could be created, authenticate through ingress,
    and then fail `validate_contract` in the worker on every attempt -- forever,
    because `InboxProcessor.prepare` does not catch it. Migration 008 puts the
    bound where a `config_id` enters the system, which is the only place it can
    still be refused cheaply.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    previous = UUID(str(seeded_database["profile_release"]))
    with _migration_connection() as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                """
                INSERT INTO channel_bindings(
                    config_id, tenant_id, profile_release_id, current_token_hash
                ) VALUES (%s, %s, %s, %s)
                """,
                ("c" * 129, tenant_a, previous, "c" * 64),
            )
        connection.rollback()
        # The boundary itself is accepted -- the check is `<= 128`, not `< 128`.
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES (%s, %s, %s, %s)
            """,
            ("d" * 128, tenant_a, previous, "d" * 64),
        )
        connection.rollback()
