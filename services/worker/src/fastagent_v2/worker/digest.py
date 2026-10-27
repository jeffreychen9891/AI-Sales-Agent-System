"""The hourly digest: many stuck deliveries, one sentence.

Two of the manual-task kinds are classified `batched` (ruling, 2026-09-04):
deliveries whose outcome is unconfirmed, and deliveries that finally failed. A
merchant with forty stuck messages needs one sentence, not forty notifications
-- and the reason is not politeness. A channel that produces forty
notifications for one incident is a channel people mute, and a muted channel is
worth nothing on the day it carries the one message that mattered.

Three properties this module exists to hold, each of which is a way the naive
version goes wrong:

* **A quiet window says nothing at all.** Not "0 pending" -- nothing. The user's
  ruling names the failure directly: an empty notice teaches the reader that
  this source is usually noise, and that lesson is paid for later.
* **Nothing is counted twice.** Not by remembering a window, which a restart
  loses and a crash between sending and recording corrupts, but by marking each
  task as it is included. A task cannot be wrong about whether it was reported.
* **The notice is queued, not delivered.** Same as every other manager
  notification in M1a: `_claim_one` claims customer rows only, so this one waits
  in `PENDING` with the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.notification import BATCHED, MERCHANT_ACTION, digest_notice
from fastagent_v2.worker.channel import TEXT_CONTENT_TYPE
from fastagent_v2.worker.pipeline import NOTICE_DEADLINE_SECONDS
from fastagent_v2.worker.policy_output import (
    PolicyOutput,
    audit_and_redact,
    record_policy_decision,
)
from psycopg.types.json import Jsonb

#: How often one tenant may receive a digest. The ruling says hourly; the
#: interval is enforced against the **last digest actually written**, not
#: against a timer in this process, so a restart cannot produce a second one and
#: a stopped worker cannot silently skip the interval either -- the next run
#: simply covers everything that accumulated.
DIGEST_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DigestResult:
    tenant_id: UUID
    outbox_id: UUID | None
    counted: int
    reason: str


#: Namespace for the digest's advisory lock. An arbitrary constant, but a named
#: one: two features picking the same pair of integers by accident would
#: serialise against each other for no reason anybody could find later.
DIGEST_LOCK_NAMESPACE = 0x0D16


def _tenant_lock_key(tenant_id: UUID) -> int:
    """A stable 32-bit key for one tenant.

    `pg_advisory_xact_lock(int, int)` takes two 32-bit integers, and a tenant is
    a 128-bit UUID, so this is a hash and hashes collide. A collision costs two
    tenants serialising behind each other for the length of one digest
    transaction -- not a wrong answer, just a wait -- which is why a lossy key is
    acceptable here and would not be if it decided *what* gets written.
    """

    return int.from_bytes(tenant_id.bytes[:4], "big", signed=True)


LOGGER = logging.getLogger(__name__)


class ManagerDigest:
    """Writes at most one digest per tenant per interval, and only when there is news."""

    def __init__(self, *, database: Database) -> None:
        self._database = database

    def run_for(self, *, tenant_id: UUID, now: datetime) -> DigestResult:
        with self._database.transaction(tenant_id=tenant_id) as connection:
            # **One digest per tenant per window, and the lock is what says so.**
            # The interval check reads the last digest and the write creates the
            # next one; between those two statements a second worker can read
            # the same "last" and reach the same conclusion. Both then write,
            # and the merchant is told about the same backlog twice -- with the
            # counts double-reported, because each digest marks only the tasks
            # it selected (independent review 2026-09-04, P2-1).
            #
            # `pg_advisory_xact_lock` rather than a table lock or a unique
            # constraint: the thing being serialised is *the decision*, not a
            # row, and there is no row yet when the decision is made. It is
            # released with the transaction, so a worker that dies holding it
            # blocks nobody past its own connection.
            #
            # Two workers today is not hypothetical -- `_claim_one` has carried
            # a lease and `FOR UPDATE SKIP LOCKED` since M0 precisely because
            # more than one dispatcher is the expected shape.
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (DIGEST_LOCK_NAMESPACE, _tenant_lock_key(tenant_id)),
            )
            # **No row here is a ruling, not a bug** (ADR-0078, user
            # 2026-09-06; registered as T77 since 2026-09-04).
            #
            # **Two ways a contact gets configured, as of 2026-09-08**: the
            # deployment writes this table directly (ADR-0078, still valid), or
            # the merchant calls `PUT /tenants/current/manager-contact`
            # (ADR-0104, which reopened ADR-0078 §二 once B1-5 made delivery
            # real). What has not changed: **a tenant nobody configured gets no
            # digest**, and `contact is None` below is where that takes effect.
            # This comment used to say no route could configure one, which was
            # true until it wasn't.
            contact = connection.execute(
                """
                SELECT channel, config_id, external_user_id
                  FROM tenant_manager_contacts WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
            if contact is None:
                # No recipient. The tasks stay undigested rather than being
                # marked as reported to nobody -- so the day a manager is
                # configured, the backlog is what they are told about.
                return DigestResult(tenant_id, None, 0, "no_manager_contact")

            recent = connection.execute(
                """
                SELECT max(created_at) AS last_at FROM outbox_messages
                 WHERE tenant_id = %s AND command->>'audience' = 'tenant_manager'
                   AND command->>'notice_kind' = 'digest'
                """,
                (tenant_id,),
            ).fetchone()
            last_at = recent["last_at"] if recent is not None else None
            if last_at is not None and now - last_at < DIGEST_INTERVAL:
                return DigestResult(tenant_id, None, 0, "interval_not_elapsed")
            # From here on the database's clock is the only one used. `now`
            # arrives from the caller's process and was being compared against
            # `created_at` (database) while also being *written* to
            # `digested_at` -- two clocks in one decision, and the skew between
            # them decides whether an hourly digest is early or late
            # (independent review 2026-09-04, P3-4). The deadline in the outbox
            # command still uses `now`, because that one is about how long a
            # dispatcher may hold the row, not about the digest window.

            pending = connection.execute(
                """
                SELECT reason_code, count(*) AS n, array_agg(id) AS ids
                  FROM manual_tasks
                 WHERE tenant_id = %s AND task_class = %s AND notify = %s
                   AND digested_at IS NULL
                 GROUP BY 1
                """,
                (tenant_id, MERCHANT_ACTION, BATCHED),
            ).fetchall()

            lines: list[str] = []
            included: list[UUID] = []
            for row in pending:
                notice = digest_notice(kind=str(row["reason_code"]), count=int(row["n"]))
                if notice is None:
                    # A batched kind with no sentence written for it is not
                    # reported at all rather than reported badly -- and its
                    # tasks stay undigested, so nothing is lost. But "not lost"
                    # is not "visible": before this line the skip left no trace
                    # anywhere, so a kind added to `_POLICY` without a sentence
                    # would be dropped for ever and silently (independent review
                    # 2026-09-04, P2-4). `batched_kinds_without_a_sentence()`
                    # makes it a test failure; this makes it a log line for the
                    # case that slips past anyway.
                    LOGGER.warning(
                        "manager digest skipped a batched kind with no sentence; kind=%s",
                        row["reason_code"],
                    )
                    continue
                lines.append(notice)
                included.extend(row["ids"])

            if not included:
                # **The empty window.** No row, no message, nothing marked.
                return DigestResult(tenant_id, None, 0, "nothing_to_report")

            outbox_id = uuid4()
            self._write(
                connection,
                tenant_id=tenant_id,
                contact=contact,
                body="\n".join(lines),
                outbox_id=outbox_id,
                included=included,
                now=now,
            )
            return DigestResult(tenant_id, outbox_id, len(included), "queued")

    def _write(
        self,
        connection: Any,
        *,
        tenant_id: UUID,
        contact: dict[str, Any],
        body: str,
        outbox_id: UUID,
        included: list[UUID],
        now: datetime,
    ) -> None:
        """One intent, one outbox row, and the tasks marked in the same transaction.

        Same transaction on purpose: a digest that was sent but not recorded
        would repeat itself hourly for ever, and one that was recorded but not
        sent would drop the news permanently. Neither is recoverable from
        outside, so neither is allowed to happen.
        """

        intent_id = uuid4()
        policy_id = uuid4()
        # The digest belongs to no single conversation, so it borrows the frame
        # of the oldest task it covers -- the schema requires a conversation and
        # a frame, and inventing a synthetic one would put a row in the table
        # that no conversation query could explain.
        anchor = connection.execute(
            """
            SELECT conversation_id, target_frame_id FROM intent_records
             WHERE tenant_id = %s AND id = ANY(%s) ORDER BY created_at LIMIT 1
            """,
            (tenant_id, included),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions, policy_evidence
            ) VALUES (%s, %s, %s, 'handoff', 'PROPOSED', %s, %s, 'handoff', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                intent_id,
                anchor["conversation_id"],
                Jsonb(
                    {
                        "audience": "tenant_manager",
                        "notice_kind": "digest",
                        "covered": len(included),
                    }
                ),
                Jsonb({"step": "platform", "version": "manager-digest-v1"}),
                anchor["target_frame_id"],
                f"manager_digest:{outbox_id}",
                Jsonb({"covered_task_ids": [str(task) for task in included]}),
                Jsonb([str(policy_id)]),
            ),
        )
        # **The digest is a delivery intent and needs its own decision.**
        # It was born `APPROVED` with empty `policy_evidence`, no
        # `policy_decisions` row and no audit line -- while the per-event notice
        # next door (L2a `_queue_manager_notice`) goes PROPOSED -> allow ->
        # APPROVED and records both. Same table, same audience, same last hop,
        # two different amounts of evidence (independent review 2026-09-04,
        # P1-2). The `redact` obligation is this batch's only audit-trail proof
        # that the notification vocabulary ran at all, and the digest had none.
        record_policy_decision(
            connection,
            tenant_id=tenant_id,
            row_id=policy_id,
            conversation_id=anchor["conversation_id"],
            intent_id=intent_id,
            output=PolicyOutput(
                hook="pre_execute",
                effect="allow",
                reason_code="MANAGER_DIGEST_ALLOWED",
                policy_id="manager-digest-policy",
                policy_version="1.0.0",
                obligations=audit_and_redact(),
                context={
                    "tenant_id": str(tenant_id),
                    "audience": "tenant_manager",
                    "notice_kind": "digest",
                    "covered": len(included),
                },
            ),
        )
        connection.execute(
            """
            UPDATE intent_records SET status = 'APPROVED', updated_at = now()
             WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'
            """,
            (tenant_id, intent_id),
        )
        connection.execute(
            """
            -- **`0` because the digest is alone in its transaction** (P3-4,
            -- independent review 2026-09-08): `_write` runs once per tenant
            -- per interval and writes exactly this row, so there is no second
            -- position for it to be relative to.
            INSERT INTO outbox_messages(
                tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                ordinal
            ) VALUES (%s, %s, %s, %s, %s, %s, 0)
            """,
            (
                tenant_id,
                outbox_id,
                anchor["conversation_id"],
                intent_id,
                Jsonb(
                    {
                        "audience": "tenant_manager",
                        "notice_kind": "digest",
                        "recipient": {
                            "channel": contact["channel"],
                            "config_id": contact["config_id"],
                            "external_user_id": contact["external_user_id"],
                        },
                        "tenant_id": str(tenant_id),
                        "conversation_id": str(anchor["conversation_id"]),
                        # Same typed shape and the same one-day life as the
                        # per-event notice: the digest is one more row a future
                        # dispatcher has to be able to send, and a row that
                        # expires in an hour would be claimed dead on the day
                        # delivery is switched on (L2a's F3, same defect, same
                        # file family).
                        # One constant for the type, like the other two outbox
                        # producers (independent review 2026-09-10, P3-6):
                        # a constant that only two of three callers use is a
                        # constant with a back door.
                        "content": {"type": TEXT_CONTENT_TYPE, "text": body},
                        "deadline_at": (
                            now + timedelta(seconds=NOTICE_DEADLINE_SECONDS)
                        ).isoformat(),
                        "correlation_id": str(uuid4()),
                    }
                ),
                f"manager_digest:{outbox_id}",
            ),
        )
        connection.execute(
            """
            UPDATE manual_tasks SET digested_at = now(), updated_at = now()
             WHERE tenant_id = %s AND id = ANY(%s) AND digested_at IS NULL
            """,
            (tenant_id, included),
        )
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'intent.approve', 'intent_record', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                intent_id,
                uuid4(),
                "MANAGER_DIGEST_ALLOWED",
                Jsonb(
                    {
                        "policy_decision_id": str(policy_id),
                        "covered": len(included),
                        # Not the body: the digest text quotes nothing from a
                        # customer, but "it does not today" is not a reason to
                        # put a message into an append-only table.
                    }
                ),
            ),
        )
