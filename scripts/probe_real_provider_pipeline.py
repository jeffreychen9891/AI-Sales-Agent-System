"""T11: drive one real customer event through the whole durable path.

**Not a curl probe.** The nine real model calls made before 2026-09-03 were all
server-side `curl` against the vendor; none of them touched
inbox -> target stack -> intent -> outbox, which is what the M1a exit item
actually asks for. This script uses the production seams end to end: the event
enters through `WeComProxyIngress.accept`, is processed by the real
`InboxProcessor` wired by `inference_for_provider` (the same call `cli.py`
makes), and leaves through `OutboxDispatcher`. Nothing is stubbed. What the
pipeline wrote is then read back out of PostgreSQL and printed.

Usage (the caller supplies the whole environment the worker role needs, plus
`FASTAGENT_MODEL_PROVIDER` and the matching `FASTAGENT_MODEL_API_KEY_*`):

    python scripts/probe_real_provider_pipeline.py <repo-root> <message-file>

It **destroys and reseeds** the tenant tables it uses. **That is now enforced
rather than requested**: `_refuse_unless_scratch` requires the database to carry
the `fastagent-v2-scratch` comment *and* to hold no binding this script did not
create. Until 2026-09-10 this paragraph was the only guard, and a paragraph
stops nobody -- see `scripts/seed_stage_real_tenant.py` for the real path.

Credential handling: this script never reads, prints or accepts an API key. The
credential is resolved inside `providers.credential_for`, from the process
environment, exactly as the worker does. The summary is passed through
`runtime.redaction.redact` before printing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.ingress.wecom_proxy.callback import conversation_id_for
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.profiles.registry import ProfileRegistry
from fastagent_v2.runtime.auth import hash_password, sha256_token
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.redaction import redact
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import HttpChannelAdapter
from fastagent_v2.worker.inference import inference_for_provider
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

#: **Read inside `main()`, not at import.** It used to be
#: `Path(sys.argv[1]).resolve()` at module scope, which made this file
#: impossible to import without pretending to be a command line -- and therefore
#: impossible to test. The guard added below is the thing most worth testing, so
#: the module had to become importable first.
TENANT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EMPLOYEE_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
PROFILE_RELEASE = UUID("11111111-1111-4111-8111-111111111111")
#: Who the probe speaks as. The conversation id is derived from this
#: (2026-09-10), so the script prints the conversation rather than picking it.
PROBE_SENDER_ID = "t11-customer"
PROBE_ACCOUNT_ID = "t11-hosted-account"

CHANNEL_TOKEN = "test-only-channel-token-with-32-bytes"  # noqa: S105

#: The database comment a scratch database must carry before this script will
#: touch it.
#:
#: **A marker rather than a flag**, because a flag travels with the operator and
#: the danger travels with the database. `--yes-really` in a shell history is one
#: arrow-up away from being fired at Stage; a comment has to be set **on the
#: database being destroyed**, by someone who owns it.
#:
#: Set it with:
#:
#:     COMMENT ON DATABASE <name> IS 'fastagent-v2-scratch';
#:
#: On a real deployment the runtime roles do not own the database, so this is
#: also awkward to set by accident -- which is the property being bought.
SCRATCH_MARKER = "fastagent-v2-scratch"


def _refuse_unless_scratch(connection: Connection[dict[str, Any]]) -> None:
    """Refuse any database that is not marked disposable, or that holds real work.

    **This used to be a docstring** -- 「point it at a local or Stage-scratch
    database, never at one holding facts you want to keep」 -- and a docstring
    stops nobody. This script `TRUNCATE`s fifteen tables and then seeds a binding
    whose path token is **committed to this repository**, so the two ways it can
    be misused are 「destroy real rows」 and 「publish a public token as a real
    tenant's callback secret」. Both are one wrong argument away.

    Two independent conditions, and both must hold:

    1. **the database says it is scratch** (`SCRATCH_MARKER`);
    2. **nothing in it looks like real work** -- any binding this script did not
       create is decisive, because its own is the only one whose token hash is
       the constant above.

    Either alone would be too weak. A marker alone would let a scratch database
    that somebody later pointed at a real tenant be wiped; the binding check
    alone would let an unmarked, currently-empty production database through on
    the day it happens to be empty -- which is precisely the day a deployment is
    being stood up.
    """

    marked = connection.execute(
        "SELECT shobj_description(oid, 'pg_database') AS note"
        "  FROM pg_database WHERE datname = current_database()"
    ).fetchone()
    note = str(marked["note"]) if marked and marked.get("note") else ""
    # **Exact, not a substring** (independent review 2026-09-10, P3-2). A
    # substring test read `"production: this is NOT fastagent-v2-scratch, do not
    # truncate"` as permission -- a comment explicitly denying that it is scratch
    # opened the gate. This is the only thing standing in front of a `TRUNCATE`.
    if note.strip() != SCRATCH_MARKER:
        raise SystemExit(
            f"refusing: this database is not marked as scratch. This script "
            f"TRUNCATEs fifteen tables and seeds a binding whose path token is "
            f"committed to the repository. If it really is disposable, mark it "
            f"with: COMMENT ON DATABASE <name> IS '{SCRATCH_MARKER}'; "
            f"(current comment: {note!r})"
        )

    own_hash = sha256_token(CHANNEL_TOKEN)
    foreign = connection.execute(
        "SELECT config_id, status FROM channel_bindings WHERE current_token_hash <> %s",
        (own_hash,),
    ).fetchall()
    if foreign:
        raise SystemExit(
            "refusing: this database holds channel bindings this script did not "
            f"create ({[row['config_id'] for row in foreign]}). A binding carries a "
            "token nobody can recover, and TRUNCATE would destroy it along with "
            "every conversation behind it. Use scripts/seed_stage_real_tenant.py "
            "for a real tenant; this script is a fixture."
        )


def seed(settings: Settings, root: Path) -> None:
    package = ProfileRegistry(root=root / "profiles", relative_to=root).package(
        profile_id="synthetic-service-desk", version="1.0.0"
    )
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        # **Before the first destructive statement, not after it.**
        _refuse_unless_scratch(connection)
        connection.execute(
            """
            TRUNCATE audit_events, platform_audit_events, delivery_receipts,
                     outbox_messages, policy_decisions,
                     intent_records, scheduled_events, inbox_events, conversation_aggregates,
                     ws_tickets, employee_sessions, channel_bindings, employees,
                     profile_releases, tenants CASCADE
            """
        )
        connection.execute("INSERT INTO tenants(id, code) VALUES (%s, 'tenant-a')", (TENANT_A,))
        connection.commit()
        published = publish_release(
            database=Database(settings.database_url), tenant_id=TENANT_A, package=package
        )
        assert published.release.release_id == PROFILE_RELEASE
        connection.execute(
            """
            INSERT INTO employees(tenant_id, id, email, password_hash, permissions)
            VALUES (%s, %s, 'operator-a@example.com', %s, %s)
            """,
            (TENANT_A, EMPLOYEE_A, hash_password("local-only-strong-password"),
             Jsonb(["session.read", "websocket_ticket.create"])),
        )
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES ('config-a', %s, %s, %s)
            """,
            (TENANT_A, PROFILE_RELEASE, sha256_token(CHANNEL_TOKEN)),
        )
        connection.commit()


def main() -> int:
    settings = Settings.from_env()
    print(f"provider   : {settings.model_provider}")
    print(f"environment: {settings.environment}")
    seed(settings, Path(sys.argv[1]).resolve())

    queue = PointerQueue(settings.queue_redis_url)
    accepted = WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=CHANNEL_TOKEN,
        # **The vendor's callback shape** (2026-09-10). This script drove
        # `accept()` with v2's own envelope, which the callback-envelope batch
        # made an outright refusal -- so T11's only driver would have raised
        # `CallbackEnvelopeError` before printing a line. The conversation is
        # derived from the sender now, so the script prints it rather than
        # choosing it.
        payload={
            "code": 0,
            "msg": "ok",
            "data": [
                {
                    "cmd": 15000,
                    "msgType": 2,
                    "msgUniqueIdentifier": f"t11-{uuid4()}",
                    "requestId": uuid4().hex,
                    "senderId": PROBE_SENDER_ID,
                    "receiverId": PROBE_ACCOUNT_ID,
                    "userId": PROBE_ACCOUNT_ID,
                    "fromRoomId": 0,
                    "isRoomNotice": 0,
                    "timestamp": int(datetime.now(UTC).timestamp()),
                    "msgData": {
                        "content": Path(sys.argv[2]).read_text(encoding="utf-8")
                    },
                }
            ],
        },
    )
    conversation_id = conversation_id_for(
        tenant_id=TENANT_A, config_id="config-a", peer_id=PROBE_SENDER_ID
    )
    print(
        f"ingress accepted: {accepted.accepted} inbox_id={accepted.inbox_id} "
        f"conversation={conversation_id}"
    )

    from fastagent_v2.worker.cost import CostGovernor
    from fastagent_v2.worker.model_policy import active_release

    release = active_release()
    print(f"model policy release: {release.version} (status {release.document['status']})")
    governor = CostGovernor(database=Database(settings.worker_database_url), release=release)

    started = datetime.now(UTC)
    processor = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(
            settings.model_provider, environment=settings.environment, governor=governor
        ),
        worker_id=f"{socket.gethostname()}-t11",
    )
    processed = processor.process_one(block_ms=2000)
    print(f"worker processed: {processed} in {(datetime.now(UTC) - started).total_seconds():.2f}s")

    dispatched = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=HttpChannelAdapter(settings.channel_send_url),
        dispatcher_id=f"{socket.gethostname()}-t11-sender",
    ).dispatch_one()
    print(f"outbox dispatched: {dispatched}")

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        def rows(sql: str) -> list[dict[str, object]]:
            return connection.execute(sql).fetchall()

        # `list[...] | str`: every value starts as rows, and the two `except`
        # branches replace one with "unavailable: ...". Inferring the type from
        # the literal made those assignments type errors, which is the shape of
        # a real bug -- a reader of `report` has to handle both (T68).
        report: dict[str, list[dict[str, object]] | str] = {
            "inbox_events": rows(
                "SELECT status, attempt, error_code FROM inbox_events ORDER BY id"
            ),
            "conversation_aggregates": rows(
                "SELECT state_version, control_mode, lifecycle, generation,"
                " jsonb_array_length(target_stack) AS frames,"
                " target_stack FROM conversation_aggregates"
            ),
            "intent_records": rows(
                "SELECT kind, status, strategy_id FROM intent_records ORDER BY id"
            ),
            "outbox_messages": rows(
                "SELECT status FROM outbox_messages ORDER BY id"
            ),
            "delivery_receipts": rows(
                "SELECT * FROM delivery_receipts LIMIT 5"
            ),
            "audit_events": rows(
                "SELECT action, resource_type, reason_code, evidence FROM audit_events"
                " ORDER BY created_at"
            ),
        }
        try:
            report["cost_ledger"] = rows(
                "SELECT role, provider_id, model_id, status, input_tokens, output_tokens,"
                " cost_cny, estimated_cost_cny FROM model_invocation_costs"
                " ORDER BY occurred_at"
            )
        except Exception as error:  # noqa: BLE001 - table name differs across migrations
            connection.rollback()
            report["cost_ledger"] = f"unavailable: {type(error).__name__}"
        try:
            evidence = rows(
                "SELECT jsonb_pretty(model_evidence) AS evidence FROM intent_records"
                " LIMIT 1"
            )
            report["model_evidence_sample"] = evidence
        except Exception as error:  # noqa: BLE001
            connection.rollback()
            report["model_evidence_sample"] = f"unavailable: {type(error).__name__}"

    print("---- durable state after one real-provider turn (redacted) ----")
    print(json.dumps(redact(report), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
