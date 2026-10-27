"""Pointing a channel binding at a published release, and what that costs.

`publisher.py` makes a release exist. Nothing made a binding *use* one: the only
writer of `channel_bindings.profile_release_id` in the repository was the test
fixture, which is why the M1a exit item "jewellery Stage single target frame"
was structurally unreachable even after T10 (registered as **T15**).

Two operations live here because RFC §9.1 draws the line between them, not
because it is convenient:

* **Rebinding the channel** decides which release *new* conversations pin. It is
  cheap and reversible -- "rollback is binding to the previous immutable
  release, not overwriting release content".
* **Repinning live conversations** is the expensive one. §9.1: "new conversations
  pin the new release; existing conversations keep their pin by default. An
  explicit repin must generation++, repair the target stack, invalidate events
  and audit." All four, or none: a generation bump without invalidation leaves
  timers that fire against a stack they no longer describe.

The default is therefore `repin_live_conversations=False`. Moving a live
conversation onto a different set of goals mid-flight is a decision, and the
caller has to make it explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.profiles.protocol import ProfileRelease
from fastagent_v2.profiles.resolver import release_from_row
from fastagent_v2.runtime.contracts import validate_contract
from fastagent_v2.runtime.db import Database
from fastagent_v2.worker.target_stack import repair_target_stack_for_repin
from psycopg import Connection
from psycopg.types.json import Jsonb

#: How many live conversations one repin call will move. The selection takes
#: `FOR UPDATE` on every matching row and the worker's `prepare()` takes the same
#: lock, so an unbounded repin blocks one tenant's whole conversation processing
#: for its full duration, and the transaction grows with it (independent review
#: 2026-09-03, P2-2). The cap makes that blast radius a decision rather than a
#: function of how busy the tenant happens to be.
DEFAULT_REPIN_LIMIT = 200


class ChannelBindingNotFound(RuntimeError):
    """The binding does not exist for this tenant."""


class ChannelBindingDisabled(RuntimeError):
    """The binding exists but has been stopped, so it cannot be repinned.

    **Its own type, not a `ChannelBindingNotFound`.** The two mean different
    things to the caller -- one is "you named something that is not here", the
    other is "you named something that is here and finished" -- and the repin
    route already has to tell two *other* cases apart by what it knows rather
    than by what it caught (T115). Adding a third meaning to that exception
    would have made that worse, not cheaper.
    """


class ProfileReleaseNotFound(RuntimeError):
    """The release does not exist for this tenant.

    Raised in preference to letting the foreign key fail: the constraint would
    report a violated key name, and an operator repinning a channel needs to be
    told that the release id is wrong, not that a constraint fired.
    """


@dataclass(frozen=True, slots=True)
class RepinnedConversation:
    conversation_id: UUID
    generation: int
    #: True when a frame on the stack referenced a target frame definition the
    #: new profile does not contain, so the conversation went to a human.
    handed_to_human: bool
    invalidated_events: int


@dataclass(frozen=True, slots=True)
class RepinResult:
    config_id: str
    release: ProfileRelease
    previous_release_id: UUID
    #: False when the binding already pointed at this release.
    changed: bool
    conversations: tuple[RepinnedConversation, ...]
    #: Conversations that matched but were left for a later call (see
    #: `DEFAULT_REPIN_LIMIT`). Non-zero means the repin is not finished.
    remaining_conversations: int = 0
    #: Open conversations on some other release that carry no binding link at
    #: all -- created before migration 008, so the aggregate does not record
    #: which binding they arrived through (T24). They are **not** moved.
    #: Reported rather than silently skipped: an operator who sees
    #: `changed=True, remaining=0` and a non-zero count here knows the repin
    #: finished its work and that these were never in scope, instead of assuming
    #: everything moved.
    #:
    #: **This count is tenant-wide, not binding-wide.** A pre-008 row carries no
    #: binding, so it cannot be attributed to one: with N bindings on a tenant,
    #: every one of the N repins reports the same rows. Read it as "this tenant
    #: still has n conversations from before the link existed", not as "this
    #: binding has n left to move" -- `remaining_conversations` is the field that
    #: means the latter.
    #:
    #: These rows are **not** unrecoverable. `ingress_replay_keys` has recorded
    #: `(tenant_id, config_id, external_event_id)` since 001 and joins to
    #: `inbox_events` per conversation; migration 008 carries the exact statement
    #: and T30 carries it as a runbook entry. No environment holds such rows
    #: today, which is why nothing runs it automatically.
    unlinked_conversations: int = 0


def repin_channel_binding(
    *,
    database: Database,
    tenant_id: UUID,
    config_id: str,
    release_id: UUID,
    repinned_by: UUID | None = None,
    correlation_id: UUID | None = None,
    repin_live_conversations: bool = False,
    limit: int = DEFAULT_REPIN_LIMIT,
) -> RepinResult:
    """Point one binding at one published release, idempotently.

    **The two operations have separate idempotency bases**, which the first
    version conflated (independent review 2026-09-03, P2-1). Rebinding is
    idempotent against *the binding*: pointing it at the release it already
    holds writes nothing. Repinning live conversations is idempotent against
    *each conversation*: the ones that matter are those still pinned to some
    other release, and whether the binding moved in this same call has nothing
    to do with them.

    Conflating the two broke the ordinary operator sequence -- rebind first,
    decide to move the live ones afterwards. The second call took the early
    return, reported `changed=False, conversations=()`, and left every live
    conversation stranded on the old release with no other writer in the
    repository able to move them. It looked exactly like success.
    """

    correlation = correlation_id or uuid4()
    occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    conversations: list[RepinnedConversation] = []
    remaining = 0
    unlinked = 0
    with database.transaction(
        tenant_id=tenant_id,
        subject_id=repinned_by,
        correlation_id=correlation,
    ) as connection:
        # FOR UPDATE: two concurrent repins of the same binding must serialise,
        # or the second one computes its "previous release" from a row the first
        # one has already replaced and audits a transition that never happened.
        binding = connection.execute(
            """
            SELECT config_id, profile_release_id, status
              FROM channel_bindings
             WHERE tenant_id = %s AND config_id = %s
             FOR UPDATE
            """,
            (tenant_id, config_id),
        ).fetchone()
        if binding is None:
            raise ChannelBindingNotFound(
                f"no channel binding {config_id!r} for this tenant"
            )
        # **T142, ruled by the user on 2026-09-05.** Three routes share
        # `channel_config.update` and, until this line, they disagreed about a
        # disabled binding: `PATCH /channel-bindings/{config_id}` refuses one
        # with 409, and repin did not look at `status` at all -- so live
        # conversations could be moved onto a binding whose callback token B2 #5
        # had destroyed, and which M1a has no route to bring back.
        #
        # **The disagreement was the defect, not either answer.** The strict one
        # is right here: a repin onto a dead binding is a write that can never
        # take effect, and it would leave the conversations pointing somewhere
        # that cannot answer them.
        #
        # Read under the same `FOR UPDATE` as everything else in this
        # transaction, so a deactivation cannot land between the check and the
        # move.
        if str(binding["status"]) != "active":
            raise ChannelBindingDisabled(
                f"channel binding {config_id!r} is {binding['status']!r} and cannot be repinned"
            )
        previous_release_id = UUID(str(binding["profile_release_id"]))

        release_row = connection.execute(
            """
            SELECT tenant_id, id, profile_id, version, content_hash, profile
              FROM profile_releases
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, release_id),
        ).fetchone()
        if release_row is None:
            raise ProfileReleaseNotFound(
                f"no published release {release_id} for this tenant; publish it first"
            )
        release = release_from_row(release_row)

        changed = previous_release_id != release_id
        if changed:
            connection.execute(
                """
                UPDATE channel_bindings SET profile_release_id = %s
                 WHERE tenant_id = %s AND config_id = %s
                """,
                (release_id, tenant_id, config_id),
            )

        if repin_live_conversations:
            conversations, remaining, unlinked = _repin_live_conversations(
                connection=connection,
                tenant_id=tenant_id,
                actor_id=repinned_by,
                correlation_id=correlation,
                previous_release_id=previous_release_id,
                release=release,
                occurred_at=occurred_at,
                limit=limit,
                config_id=config_id,
            )

        _audit_binding(
            connection=connection,
            tenant_id=tenant_id,
            actor_id=repinned_by,
            correlation_id=correlation,
            config_id=config_id,
            previous_release_id=previous_release_id,
            release=release,
            reason_code=(
                "CHANNEL_BINDING_REPINNED" if changed else "CHANNEL_BINDING_ALREADY_PINNED"
            ),
            repinned_conversations=len(conversations),
        )
    return RepinResult(
        config_id=config_id,
        release=release,
        previous_release_id=previous_release_id,
        changed=changed,
        conversations=tuple(conversations),
        remaining_conversations=remaining,
        unlinked_conversations=unlinked,
    )


def _repin_live_conversations(
    *,
    connection: Connection[Any],
    tenant_id: UUID,
    actor_id: UUID | None,
    correlation_id: UUID,
    previous_release_id: UUID,
    release: ProfileRelease,
    occurred_at: str,
    limit: int,
    config_id: str,
) -> tuple[list[RepinnedConversation], int, int]:
    """Move every open conversation still pinned to the old release.

    **`state` is the aggregate; the scalar columns are its projection.** The
    engine reads `aggregate_row["state"]` (`pipeline.py`), and
    `_apply_aggregate_transition` writes the whole aggregate into `state` while
    mirroring four fields into columns. The first version of this function wrote
    only the columns, which the independent review (2026-09-03, P0-1) showed
    meant: the generation bump and the stack repair never reached the worker at
    all; the `control_mode='human'` safety valve for an unknown frame never
    reached it either; and, worst, the document and the projection disagreed
    forever after, so every later event failed its CAS with `AggregateConflict`
    -- an exception that deliberately does *not* consume the retry budget, so the
    scheduler would republish the event forever, paying for a full inference pass
    each time while the customer waited for a reply that could never come.

    Selection is by *release*, not by config: `conversation_aggregates` records
    which release a conversation pinned, and nothing anywhere records which
    binding it arrived through. Every open conversation of this tenant that is
    not already on the target release moves. With two bindings under one tenant
    that means repinning either one moves both sets, and it also means a
    conversation left behind on some third release is swept along. Both are the
    same missing link, both are real over-capture, and both are registered as
    **T24** rather than papered over here.

    `closed` conversations are excluded: repairing a stack nobody will work
    again only invents state.
    """

    # Two predicates, and both matter.
    #
    # `config_id = %s` is the T24 fix: this is a repin of *one binding*, and
    # since migration 008 the aggregate records which binding it arrived
    # through. Before that the only answerable query was "by release", which
    # moved every conversation of the tenant that shared one -- an over-capture
    # nobody chose, it was just the only thing the schema could express.
    #
    # `profile_release_id <> %s` keeps the per-conversation idempotency the
    # independent review asked for (P2-1): the conversations that matter are the
    # ones not already on the target, whether or not the binding moved in this
    # same call, so the ordinary "rebind now, move the live ones afterwards"
    # sequence works and a second call finds only what is left.
    # `LIMIT` is on the statement, not on the loop. The first version selected
    # every matching row `FOR UPDATE` and then sliced `rows[:limit]` in Python,
    # which capped how much was *written* and left the lock footprint exactly
    # where it was: one repin still locked every open conversation of the tenant
    # that was not already on the target release. P2-2 named the blocking as the
    # mechanism -- the worker's `prepare()` wants those same rows -- so bounding
    # the writes shortened the outage without shrinking it (independent review
    # close-out 2026-09-03, T26).
    rows = connection.execute(
        """
        SELECT id, state_version, generation, control_mode, state
          FROM conversation_aggregates
         WHERE tenant_id = %s AND config_id = %s
           AND profile_release_id <> %s AND lifecycle <> 'closed'
         ORDER BY id
         LIMIT %s
         FOR UPDATE
        """,
        (tenant_id, config_id, release.release_id, limit),
    ).fetchall()
    repinned: list[RepinnedConversation] = []
    for row in rows:
        conversation_id = UUID(str(row["id"]))
        document = row["state"]
        if not isinstance(document, dict) or "target_stack" not in document:
            # Fail closed. A conversation row whose document is missing is one
            # the engine cannot process either; silently skipping it would leave
            # it pinned to a release this function has just reported as moved.
            raise ChannelBindingNotFound(
                f"conversation {conversation_id} has no aggregate document to repin"
            )
        repair = repair_target_stack_for_repin(
            aggregate=document, profile=release.document, occurred_at=occurred_at
        )
        handed_to_human = bool(repair.unknown_definition_ids)
        aggregate = repair.aggregate
        aggregate["profile_release_id"] = str(release.release_id)
        aggregate["state_version"] = int(row["state_version"]) + 1
        if handed_to_human:
            aggregate["control_mode"] = "human"
        validate_contract("conversation-aggregate", aggregate)
        connection.execute(
            """
            UPDATE conversation_aggregates
               SET profile_release_id = %s,
                   generation = %s,
                   state_version = %s,
                   target_stack = %s,
                   control_mode = %s,
                   state = %s,
                   updated_at = now()
             WHERE tenant_id = %s AND id = %s AND state_version = %s
            """,
            (
                release.release_id,
                aggregate["generation"],
                aggregate["state_version"],
                Jsonb(aggregate["target_stack"]),
                aggregate["control_mode"],
                Jsonb(aggregate),
                tenant_id,
                conversation_id,
                int(row["state_version"]),
            ),
        )

        # Every pending or claimed timer was scheduled against the stack that
        # just stopped being in force. `ENQUEUED` timers are deliberately not in
        # this set: they have already become inbox events, and the worker's
        # generation staleness check is what retires them -- which is inference,
        # not a recorded reason. Recorded as a known residual in the self-review.
        # **不读规则的 `cancel_on`，一律取消**（T358）：重绑之后那些定时器属于
        # 上一份 release，而它们的规则可能已经不在新 release 里。
        invalidated = connection.execute(
            """
            UPDATE scheduled_events
               SET status = 'INVALIDATED', invalidated_at = now(),
                   invalidation_reason = 'PROFILE_RELEASE_REPINNED'
             WHERE tenant_id = %s AND conversation_id = %s
               AND status IN ('PENDING', 'CLAIMED')
            """,
            (tenant_id, conversation_id),
        ).rowcount

        _audit_conversation(
            connection=connection,
            tenant_id=tenant_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            previous_release_id=previous_release_id,
            release=release,
            generation=int(aggregate["generation"]),
            unknown_definition_ids=repair.unknown_definition_ids,
            invalidated_events=invalidated,
        )
        repinned.append(
            RepinnedConversation(
                conversation_id=conversation_id,
                generation=int(aggregate["generation"]),
                handed_to_human=handed_to_human,
                invalidated_events=invalidated,
            )
        )

    # Counted after the writes, so it is what is genuinely left rather than
    # arithmetic on a page size. It reads no more rows than it counts and takes
    # no locks, which is the point of moving the cap into the statement above.
    outstanding = connection.execute(
        """
        SELECT count(*) FILTER (WHERE config_id = %s) AS remaining,
               count(*) FILTER (WHERE config_id IS NULL) AS unlinked
          FROM conversation_aggregates
         WHERE tenant_id = %s AND profile_release_id <> %s AND lifecycle <> 'closed'
        """,
        (config_id, tenant_id, release.release_id),
    ).fetchone()
    remaining = int(outstanding["remaining"]) if outstanding else 0
    unlinked = int(outstanding["unlinked"]) if outstanding else 0
    return repinned, remaining, unlinked


def _audit_binding(
    *,
    connection: Connection[Any],
    tenant_id: UUID,
    actor_id: UUID | None,
    correlation_id: UUID,
    config_id: str,
    previous_release_id: UUID,
    release: ProfileRelease,
    reason_code: str,
    repinned_conversations: int,
) -> None:
    """Record the binding-level transition.

    `resource_id` is deliberately NULL. The resource being audited is the
    binding, and a binding's identity is `config_id`, which is text -- it does
    not fit a `uuid` column. The first version put the *release* id there under
    `resource_type='channel_binding'`, so anyone querying by
    `(resource_type, resource_id)` would have been handed a release and told it
    was a binding (independent review 2026-09-03, P3-1). An empty column that
    says nothing is better than a populated one that says something false;
    `evidence.config_id` carries the identity, alongside both release ids.
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, 'channel_binding.repin', 'channel_binding', NULL, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            actor_id,
            correlation_id,
            reason_code,
            Jsonb(
                {
                    "config_id": config_id,
                    "from_release_id": str(previous_release_id),
                    "to_release_id": str(release.release_id),
                    "profile_id": release.profile_id,
                    "version": release.version,
                    "content_hash": release.content_hash,
                    "repinned_conversations": repinned_conversations,
                }
            ),
        ),
    )


def _audit_conversation(
    *,
    connection: Connection[Any],
    tenant_id: UUID,
    actor_id: UUID | None,
    correlation_id: UUID,
    conversation_id: UUID,
    previous_release_id: UUID,
    release: ProfileRelease,
    generation: int,
    unknown_definition_ids: tuple[str, ...],
    invalidated_events: int,
) -> None:
    """One row per moved conversation.

    The binding-level row says a repin happened; this says what it did to each
    conversation. Without it, "generation 7 became generation 8" has no recorded
    cause, and the stack repair is indistinguishable from a bug.
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type, resource_id,
            correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, 'conversation.repin', 'conversation_aggregate', %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            actor_id,
            conversation_id,
            correlation_id,
            "PROFILE_RELEASE_REPINNED_UNKNOWN_FRAME"
            if unknown_definition_ids
            else "PROFILE_RELEASE_REPINNED",
            Jsonb(
                {
                    "from_release_id": str(previous_release_id),
                    "to_release_id": str(release.release_id),
                    "generation": generation,
                    "invalidated_events": invalidated_events,
                    "unknown_definition_ids": list(unknown_definition_ids),
                    "handed_to_human": bool(unknown_definition_ids),
                }
            ),
        ),
    )
