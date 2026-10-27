"""B1 #12's worker half: the erasure actually happens (parity §6.4, ADR-0092).

**The route only promises; this is where the promise is kept.** The cases that
carry the file are `test_the_tenant_data_is_gone_and_the_row_is_tombstoned`,
`test_the_other_tenant_is_untouched` and
`test_what_survives_is_recorded_with_addresses` -- destruction, blast radius,
and the residue ADR-0092 refuses to leave unaddressable.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.auth import subject_fingerprint
from fastagent_v2.runtime.db import Database
from fastagent_v2.worker.tenant_purge import PURGE_MAX_ATTEMPTS, TenantPurgeRunner
from psycopg import Connection
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration]

FINGERPRINT_KEY = "test-only-purge-fingerprint-key-32-bytes"


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _runner() -> TenantPurgeRunner:
    """**Built from the worker's own URL, and that one line is the point.**

    These cases used to build the runner from
    `FASTAGENT_MIGRATION_DATABASE_URL` -- a superuser with `rolbypassrls`. Every
    one of them passed while the deployed runner, which uses
    `settings.worker_database_url`, could not see a single job: RLS on
    `tenant_purge_jobs` hid the table from `fastagent_worker`, `run_one()`
    returned `None`, and `None` is also what it returns when there is no work
    (independent review P0-1).

    **The suite was testing a role the system never runs as.** Everything else
    in this file is only evidence because of this line.
    """

    return TenantPurgeRunner(
        database=Database(os.environ["FASTAGENT_WORKER_DATABASE_URL"]),
        fingerprint_key=FINGERPRINT_KEY,
    )


def _queue(tenant_id: Any, requested_by: Any) -> UUID:
    job_id = uuid4()
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO tenant_purge_jobs(tenant_id, id, requested_by, reason)
            VALUES (%s, %s, %s, 'retention expired and the merchant asked')
            """,
            (tenant_id, job_id, requested_by),
        )
        connection.commit()
    return job_id


def _job(tenant_id: Any) -> dict[str, Any]:
    with _migration_connection() as connection:
        # **Newest first, explicitly.** The partial unique index lets a tenant
        # have several jobs once earlier ones reach a terminal state, so a bare
        # `fetchone()` would return an arbitrary row as soon as a second case
        # needs two of them (re-review, closing observation).
        row = connection.execute(
            "SELECT * FROM tenant_purge_jobs WHERE tenant_id = %s"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return row


def _tenant(tenant_id: Any) -> dict[str, Any]:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT code, status, purged_at, subject_fingerprint FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return row


def _count(table: str, tenant_id: Any) -> int:
    # The table name comes from this file's own literals, never from input.
    with _migration_connection() as connection:
        row = connection.execute(
            f"SELECT count(*) AS total FROM {table} WHERE tenant_id = %s",  # noqa: S608
            (tenant_id,),
        ).fetchone()
    assert row is not None
    return int(row["total"])


def test_the_tenant_data_is_gone_and_the_row_is_tombstoned(
    seeded_database: dict[str, Any],
) -> None:
    tenant_b = seeded_database["tenant_b"]
    before = _tenant(tenant_b)
    before_code = before["code"]
    assert _count("employees", tenant_b) >= 1, "fixture must give the tenant something to lose"
    _queue(tenant_b, seeded_database["employee_a"])

    _runner().run_one()

    assert _count("employees", tenant_b) == 0
    assert _count("profile_releases", tenant_b) >= 1, "append-only, kept (ADR-0092)"
    tenant = _tenant(tenant_b)
    assert tenant["purged_at"] is not None
    # **`status` is deliberately unchanged** -- asserted against what it was,
    # not against a literal. `026` made `purged_at` the fact so that a purge is
    # never inferred from the shape of a tombstoned string; the employee purge
    # leaves `status` alone for the same reason, and copying that is what keeps
    # `tenants.status`'s CHECK from needing a new value.
    assert tenant["status"] == before["status"]
    assert tenant["code"] != before_code
    assert tenant["code"].startswith("purged-")
    assert tenant["subject_fingerprint"] == subject_fingerprint(
        key=FINGERPRINT_KEY, tenant_id=UUID(str(tenant_b)), email=before_code
    )


def test_the_audit_survives_the_purge(seeded_database: dict[str, Any]) -> None:
    """§6.4 in one assertion: the audit is not deleted.

    It is also why the tenant row survives -- `audit_events.tenant_id`
    references it, so a purge that deleted the row would have to delete the
    audit with it.
    """

    tenant_b = seeded_database["tenant_b"]
    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, %s, 'tenant.update', 'tenant', %s, %s, 'BEFORE_PURGE', '{}')
            """,
            (tenant_b, uuid4(), seeded_database["employee_b"], tenant_b, uuid4()),
        )
        connection.commit()
    _queue(tenant_b, seeded_database["employee_a"])

    _runner().run_one()

    assert _count("audit_events", tenant_b) >= 1


def test_what_survives_is_recorded_with_addresses(seeded_database: dict[str, Any]) -> None:
    """ADR-0092 §二.2: a residue nobody can address is one nobody can act on.

    The ids are what T189 will need when the retention of profile content is
    finally ruled on -- a count would say "something was kept" and leave the
    next person to go looking for it.
    """

    tenant_b = seeded_database["tenant_b"]
    with _migration_connection() as connection:
        expected = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM profile_releases WHERE tenant_id = %s ORDER BY created_at",
                (tenant_b,),
            ).fetchall()
        ]
    _queue(tenant_b, seeded_database["employee_a"])

    _runner().run_one()

    result = _job(tenant_b)["result"]
    assert result["retained"]["profile_release_ids"] == expected
    assert result["retained"]["tenant_row"] == "tombstoned"
    assert result["deleted"]["employees"] >= 1


def test_the_other_tenant_is_untouched(seeded_database: dict[str, Any]) -> None:
    """The blast radius stops at the named tenant.

    The single most important property here: this function deletes across
    twenty-one tables with a definer's privileges, so "it only touched the one
    it was told to" cannot be left to inspection.
    """

    tenant_a, tenant_b = seeded_database["tenant_a"], seeded_database["tenant_b"]
    before = {t: _count(t, tenant_a) for t in ("employees", "profile_releases", "roles")}
    _queue(tenant_b, seeded_database["employee_a"])

    _runner().run_one()

    assert {t: _count(t, tenant_a) for t in before} == before
    assert _tenant(tenant_a)["purged_at"] is None
    assert _tenant(tenant_a)["status"] == "active"


def test_the_job_settles_and_stops_being_claimable(seeded_database: dict[str, Any]) -> None:
    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])

    first = _runner().run_one()
    second = _runner().run_one()

    assert first is not None
    assert second is None, "a settled job must not be claimed again"
    job = _job(tenant_b)
    assert job["status"] == "SUCCEEDED"
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None


def test_a_second_runner_cannot_claim_a_leased_job(seeded_database: dict[str, Any]) -> None:
    """Two workers must not both start erasing one tenant.

    The lease is what prevents it; this asserts the lease is actually taken
    rather than trusting `SKIP LOCKED` to be enough on its own.
    """

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    _runner().run_one()

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenant_purge_jobs SET status = 'RUNNING',"
            " lease_expires_at = now() + interval '5 minutes',"
            " lease_owner = 'someone-else' WHERE tenant_id = %s",
            (tenant_b,),
        )
        connection.commit()

    assert _runner().run_one() is None


def test_an_expired_lease_is_reclaimed(seeded_database: dict[str, Any]) -> None:
    """A worker killed mid-purge must not strand the tenant forever."""

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenant_purge_jobs SET status = 'RUNNING',"
            " lease_expires_at = now() - interval '1 minute',"
            " lease_owner = 'a worker that died' WHERE tenant_id = %s",
            (tenant_b,),
        )
        connection.commit()

    assert _runner().run_one() is not None
    assert _job(tenant_b)["status"] == "SUCCEEDED"


def test_a_job_out_of_attempts_is_failed_not_retried_forever(
    seeded_database: dict[str, Any],
) -> None:
    """The row keeps its count, so "why is this tenant still here" has an answer."""

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenant_purge_jobs SET attempt = %s WHERE tenant_id = %s",
            (PURGE_MAX_ATTEMPTS, tenant_b),
        )
        connection.commit()

    assert _runner().run_one() is None
    job = _job(tenant_b)
    assert job["status"] == "FAILED"
    assert job["attempt"] == PURGE_MAX_ATTEMPTS
    assert _tenant(tenant_b)["purged_at"] is None, "a failed purge must not tombstone"


def test_purging_an_already_purged_tenant_is_not_a_second_erasure(
    seeded_database: dict[str, Any],
) -> None:
    """A duplicate claim settles; it does not recompute the fingerprint.

    Recomputing it would be computing an HMAC over the tombstone rather than the
    identity -- and the identity is gone, so the wrong value would look exactly
    as valid as the right one.
    """

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    _runner().run_one()
    first = _tenant(tenant_b)

    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenant_purge_jobs SET status = 'PENDING', lease_owner = NULL,"
            " lease_expires_at = NULL, attempt = 0 WHERE tenant_id = %s",
            (tenant_b,),
        )
        connection.commit()
    _runner().run_one()

    assert _tenant(tenant_b)["subject_fingerprint"] == first["subject_fingerprint"]
    assert _tenant(tenant_b)["code"] == first["code"]


def test_a_legal_hold_placed_after_queueing_stops_the_erasure(
    seeded_database: dict[str, Any],
) -> None:
    """The route's guard is not enough on its own, because time passes.

    A purge is decided at one moment and executed at another; a hold placed in
    between is exactly the case a hold exists for. Nothing in M1a can set the
    column (T118), so this is unreachable in production today -- which is why
    it is worth pinning now rather than after a hold first matters.
    """

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    with _migration_connection() as connection:
        connection.execute("UPDATE tenants SET legal_hold = true WHERE id = %s", (tenant_b,))
        connection.commit()

    _runner().run_one()

    assert _count("employees", tenant_b) >= 1, "a held tenant must keep its data"
    assert _tenant(tenant_b)["purged_at"] is None
    job = _job(tenant_b)
    assert job["status"] == "FAILED"
    assert job["result"]["refused"] == "tenant is under legal hold"


def _probe_chunk_hash() -> str:
    import hashlib

    return hashlib.sha256("探针正文。".encode()).hexdigest()


def _give_the_tenant_a_life(seeded: dict[str, Any]) -> dict[str, Any]:
    """Rows a tenant that actually served somebody would have.

    **This is the fixture gap that let the delete order through.**
    `seeded_database` gives a tenant `employees`, one `profile_releases` and one
    `channel_bindings` -- and every one of the four foreign keys the first
    ordering violated needs a row this fixture never created: a policy decision
    on an intent, a conversation pointing at a contact, a cost row on a
    conversation, a manager contact on a binding.

    Ten worker cases passed against a tenant with nothing but staff.
    """

    tenant = seeded["tenant_b"]
    ids = {
        name: uuid4()
        for name in ("conversation", "contact", "intent", "knowledge_release")
    }
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant,)
        ).fetchone()
        assert release is not None
        binding = connection.execute(
            "SELECT config_id FROM channel_bindings WHERE tenant_id = %s LIMIT 1", (tenant,)
        ).fetchone()
        if binding is None:
            connection.execute(
                "INSERT INTO channel_bindings(config_id, tenant_id, profile_release_id,"
                " current_token_hash) VALUES (%s, %s, %s, %s)",
                (f"config-{ids['conversation']}", tenant, release["id"], "0" * 64),
            )
            config_id = f"config-{ids['conversation']}"
        else:
            config_id = binding["config_id"]

        connection.execute(
            "INSERT INTO contacts(tenant_id, id, config_id, external_user_id)"
            " VALUES (%s, %s, %s, %s)",
            (tenant, ids["contact"], config_id, "customer-for-purge"),
        )
        # `conversation_aggregates.contact_id` -> `contacts`, and
        # `conversation_aggregates.config_id` -> `channel_bindings`.
        connection.execute(
            "INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id,"
            " contact_id, config_id) VALUES (%s, %s, %s, %s, %s)",
            (tenant, ids["conversation"], release["id"], ids["contact"], config_id),
        )
        connection.execute(
            """
            INSERT INTO intent_records(
                tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                strategy_id, target_frame_id, idempotency_key, preconditions)
            VALUES (%s, %s, %s, 'send_message', 'APPROVED', '{}', '{}', 'probe', %s, %s, '{}')
            """,
            (tenant, ids["intent"], ids["conversation"], uuid4(), f"key-{ids['intent']}"),
        )
        # `policy_decisions.intent_id` -> `intent_records`: the pair the first
        # ordering got backwards.
        #
        # **Every column and every enum value below was read out of the live
        # schema before it was written.** The first draft of this helper invented
        # `decision` / `evidence` here and a `role` of `understanding` there, and
        # the gate caught all of it -- the same way it caught `purged:` against
        # `tenants.code`'s CHECK and `'purged'` against its `status` CHECK. A
        # fixture that guesses at column names is not cheaper than reading them.
        connection.execute(
            """
            INSERT INTO policy_decisions(
                tenant_id, id, conversation_id, intent_id, hook, effect,
                reason_code, policy_id, policy_version)
            VALUES (%s, %s, %s, %s, 'pre_delivery', 'allow', 'PROBE', 'probe', '1')
            """,
            (tenant, uuid4(), ids["conversation"], ids["intent"]),
        )
        # `model_invocation_costs.conversation_id` -> `conversation_aggregates`.
        connection.execute(
            """
            INSERT INTO model_invocation_costs(
                tenant_id, id, conversation_id, correlation_id, role, provider_id,
                model_id, release_version, release_content_hash, prompt_release,
                status, estimated_input_tokens, estimated_output_tokens, occurred_at)
            VALUES (%s, %s, %s, %s, 'UNDERSTAND_VALUE', 'probe', 'probe',
                    '0.0.0', %s, 'probe', 'settled', 0, 0, now())
            """,
            (tenant, uuid4(), ids["conversation"], uuid4(), "5b" * 32),
        )
        # `tenant_manager_contacts.(tenant_id, config_id)` -> `channel_bindings`.
        # No `id` column: the key is `(tenant_id, channel, config_id, …)`.
        connection.execute(
            """
            INSERT INTO tenant_manager_contacts(
                tenant_id, channel, config_id, external_user_id)
            VALUES (%s, 'wecom_proxy', %s, 'manager-for-purge')
            """,
            (tenant, config_id),
        )
        # The three knowledge-base tables (`050`), added when they were
        # (independent review 2026-09-11, P3-4). They joined `purge_tenant_data`
        # with that migration and the coverage cases proved the *list* was right
        # -- name present, ordering legal -- while no case watched a single
        # knowledge row actually go. The mechanism was never in doubt; the gap
        # was that the three newest FORCEd tables were the ones nothing had ever
        # handed a row to.
        #
        # **They chain**: `retrieval_evidence` references both
        # `knowledge_releases` and `conversation_aggregates`, and
        # `knowledge_chunks` references `knowledge_releases`. So these rows
        # exercise the delete order too, not only the counts.
        # `052`：身份行先写（T279），且 `text_hash` 必须真的是 `sha256(body)`（P2-1）。
        # release 建成 `draft` —— 「一条插入生出一个已发布的版本」在产品里不存在，
        # 而 `052` 那条触发器正是拦它的（那个迁移的注释写了为什么）。
        connection.execute(
            "INSERT INTO knowledge_chunk_identity(tenant_id, slug, chunk_version, text_hash)"
            " VALUES (%s, 'kb-probe', 1, %s) ON CONFLICT DO NOTHING",
            (tenant, _probe_chunk_hash()),
        )
        connection.execute(
            """
            INSERT INTO knowledge_releases(
                tenant_id, id, version, status, embedding_model, embedding_dimension,
                distance_metric, normalization, truncation_policy, authoring_spec,
                content_hash, published_at)
            VALUES (%s, %s, 1, 'draft', 'probe-embedding', 2048, 'cosine', 'none',
                    'probe', 'probe', %s, now())
            """,
            (tenant, ids["knowledge_release"], "7c" * 32),
        )
        connection.execute(
            """
            INSERT INTO knowledge_chunks(
                tenant_id, id, release_id, document_ref, document_version, ordinal,
                ref, slug, chunk_version, question, content_type, confidence, body,
                text_hash, source_spans)
            VALUES (%s, %s, %s, 'docs/probe.md', '1', 0, 'knowledge:kb-probe:v1',
                    'kb-probe', 1, '探针问题', '实测数据', '已实测', '探针正文。', %s,
                    '[{"file": "docs/probe.md", "section": "probe"}]')
            """,
            (tenant, uuid4(), ids["knowledge_release"], _probe_chunk_hash()),
        )
        connection.execute(
            """
            INSERT INTO retrieval_evidence(
                tenant_id, id, conversation_id, correlation_id, query_hash,
                release_id, retriever_version, outcome, deadline_ms, elapsed_ms, hits)
            VALUES (%s, %s, %s, %s, %s, %s, 'probe', 'hit', 500, 10,
                    '[{"chunk_id": "probe"}]')
            """,
            (
                tenant,
                uuid4(),
                ids["conversation"],
                uuid4(),
                "9e" * 32,
                ids["knowledge_release"],
            ),
        )
        connection.commit()
    return ids


def test_a_tenant_that_actually_served_somebody_is_purged_completely(
    seeded_database: dict[str, Any],
) -> None:
    """The case the four foreign keys needed, and the ordering case's twin.

    `test_the_purge_order_satisfies_every_foreign_key` proves the order agrees
    with the catalogue; this proves the function survives the rows that make
    those constraints bite. Both, because the catalogue check would still pass
    if `purge_tenant_data` broke for some reason unrelated to ordering.
    """

    tenant_b = seeded_database["tenant_b"]
    _give_the_tenant_a_life(seeded_database)
    for table in (
        "policy_decisions",
        "intent_records",
        "conversation_aggregates",
        "contacts",
        "model_invocation_costs",
        "tenant_manager_contacts",
        "knowledge_releases",
        "knowledge_chunks",
        "retrieval_evidence",
    ):
        assert _count(table, tenant_b) >= 1, f"the probe must give the tenant a {table} row"

    _queue(tenant_b, seeded_database["employee_a"])
    assert _runner().run_one() is not None

    for table in (
        "policy_decisions",
        "intent_records",
        "conversation_aggregates",
        "contacts",
        "model_invocation_costs",
        "tenant_manager_contacts",
        "knowledge_releases",
        "knowledge_chunks",
        "retrieval_evidence",
        "channel_bindings",
        "employees",
    ):
        assert _count(table, tenant_b) == 0, f"{table} survived the purge"
    assert _job(tenant_b)["status"] == "SUCCEEDED"
    assert _tenant(tenant_b)["purged_at"] is not None


def test_a_failed_job_does_not_pin_the_tenant_forever(
    seeded_database: dict[str, Any],
) -> None:
    """P2-1: the uniqueness is over *live* jobs, so a failure can be retried.

    A blanket `UNIQUE (tenant_id)` left a `FAILED` row that the route answered
    as "already queued" -- while nothing was queued, and no role in M1a can
    delete or reset such a row.
    """

    tenant_b = seeded_database["tenant_b"]
    _queue(tenant_b, seeded_database["employee_a"])
    with _migration_connection() as connection:
        connection.execute(
            "UPDATE tenant_purge_jobs SET status = 'FAILED' WHERE tenant_id = %s", (tenant_b,)
        )
        connection.commit()

    # The second queueing is what the blanket constraint made impossible.
    second = _queue(tenant_b, seeded_database["employee_a"])

    assert _runner().run_one() == second
    with _migration_connection() as connection:
        rows = connection.execute(
            "SELECT status FROM tenant_purge_jobs WHERE tenant_id = %s ORDER BY created_at",
            (tenant_b,),
        ).fetchall()
    assert [row["status"] for row in rows] == ["FAILED", "SUCCEEDED"], (
        "the failed attempt must survive as history"
    )
