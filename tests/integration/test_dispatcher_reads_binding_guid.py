"""B2-2 / T123: the dispatcher may read the guid, and may not read the credentials.

The batch's own criterion is unusually specific, and both halves are here:
**"授权集合从 catalog 读出来断言；反向探针从数据库那侧红"** -- so the grant is
asserted against `information_schema`, not against the migration text that
created it, and the refusal is proven by asking PostgreSQL rather than by
reading the SQL and believing it.

Asserting against the migration file would be circular: it would pass if the
`GRANT` statement were present and had silently failed to apply.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration]

#: Exactly what `046` grants, and nothing else. Written out rather than derived
#: so that widening the grant has to change this line -- which is the point at
#: which somebody has to justify it.
EXPECTED_COLUMNS = {"tenant_id", "config_id", "provider_instance_id", "status"}

#: The columns whose exposure this test exists to prevent. `current_token_hash`
#: and `next_token_hash` are the channel credentials.
CREDENTIAL_COLUMNS = ("current_token_hash", "next_token_hash")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _worker_connection(tenant_id: Any = None) -> Connection[Any]:
    """A worker-role connection, with the tenant context production sets.

    **The context is not optional.** `channel_bindings` is under row-level
    security, so a connection without `app.tenant_id` sees zero rows no matter
    what the grants say -- which is how the first run of this file failed: the
    grant was right and the read still returned nothing. Production reaches this
    table through `Database.transaction(tenant_id=...)`, which sets it; a test
    that skipped it would be testing a configuration nothing uses.

    Left unset deliberately for the refusal cases below: a privilege error must
    come from the grant, and RLS filtering rows would mask it.
    """

    connection = Connection.connect(
        os.environ["FASTAGENT_WORKER_DATABASE_URL"], row_factory=dict_row
    )
    if tenant_id is not None:
        connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    return connection


def test_the_grant_is_column_level_and_is_exactly_these_four() -> None:
    """Read out of the catalog, which is the only place the truth lives.

    T123's expiry condition names the shape as well as the timing: the grant
    must be given "按 `007` 的**列级**口径……**不得顺手给表级**". A table-level
    grant would show up here as all seven columns.
    """

    with _migration_connection() as connection:
        granted = {
            row["column_name"]
            for row in connection.execute(
                """
                SELECT column_name FROM information_schema.column_privileges
                 WHERE grantee = 'fastagent_worker'
                   AND table_name = 'channel_bindings'
                   AND privilege_type = 'SELECT'
                """
            ).fetchall()
        }

    assert granted == EXPECTED_COLUMNS, (
        "the dispatcher's read on channel_bindings must stay exactly the four "
        f"columns 046 justifies; found {sorted(granted)}"
    )


@pytest.mark.parametrize("column", CREDENTIAL_COLUMNS)
def test_the_worker_cannot_read_the_channel_credentials(column: str) -> None:
    """**The reverse probe, from the database side.**

    Not "the migration did not grant it" -- that is a statement about a file.
    This asks PostgreSQL, as the worker role, and requires it to refuse. If a
    later migration widened the grant to table level, the case above would go
    red and so would this one; if somebody granted just this column, only this
    one would.
    """

    with _worker_connection() as connection:
        with pytest.raises(InsufficientPrivilege):
            # noqa: S608 -- the "injected" value is a module constant naming
            # a column we require the database to refuse; parameterising it
            # is not possible and not the point.
            statement = f"SELECT {column} FROM channel_bindings LIMIT 1"  # noqa: S608
            connection.execute(statement).fetchone()


def test_the_worker_can_read_the_guid(settings: Any, seeded_database: dict[str, Any]) -> None:
    """The positive half. Without it the two refusals above are satisfied by a
    role that cannot read the table at all, which would be a broken dispatcher
    that passes its own security tests."""

    with _worker_connection(seeded_database["tenant_a"]) as connection:
        row = connection.execute(
            """
            SELECT provider_instance_id, status FROM channel_bindings
             WHERE tenant_id = %s AND config_id = %s
            """,
            (seeded_database["tenant_a"], "config-a"),
        ).fetchone()

    assert row is not None, "the dispatcher cannot see its own tenant's binding"
    assert "status" in row


def test_the_resolver_distinguishes_its_three_refusals(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """Three reasons, three different things to do about them.

    A resolver that collapsed them into "no guid" would make a provisioning
    gap, a deliberate disable, and an unfinished binding ceremony
    indistinguishable in the receipt -- the same flattening L1 exists to
    prevent between "we asked and do not know" and "we never asked".
    """

    from fastagent_v2.worker.pipeline import (
        BINDING_HAS_NO_INSTANCE,
        CONVERSATION_NOT_VISIBLE,
        resolve_binding_guid,
    )

    tenant = seeded_database["tenant_a"]

    with _migration_connection() as connection:
        # Seeded as the migration role: creating fixtures is not the behaviour
        # under test, and the worker has no INSERT on aggregates.
        # A conversation whose binding has no provider instance yet: the
        # ordinary state between creating a binding and finishing the ceremony.
        conversation_id = uuid4()
        connection.execute(
            """
            INSERT INTO conversation_aggregates(
                tenant_id, id, profile_release_id, config_id
            )
            SELECT %s, %s, profile_release_id, config_id
              FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-a'
            """,
            (tenant, conversation_id, tenant),
        )
        connection.commit()

    # **Resolved as the worker, through the same context production uses.**
    # Running it as the migration role would prove the SQL parses and nothing
    # about whether the dispatcher is allowed to run it.
    with _worker_connection(tenant) as connection:
        guid, reason = resolve_binding_guid(
            connection, tenant_id=tenant, conversation_id=conversation_id
        )
        assert guid is None
        assert reason == BINDING_HAS_NO_INSTANCE

        # A conversation with no binding at all.
        orphan = uuid4()
        guid, reason = resolve_binding_guid(
            connection, tenant_id=tenant, conversation_id=orphan
        )
        assert guid is None
        # Not "no binding": `008`'s foreign key makes that state impossible.
        # The conversation is what could not be read (independent review P2-1).
        assert reason == CONVERSATION_NOT_VISIBLE


def test_the_disabled_binding_refusal_actually_happens(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """**P1-1.** `status` is in the grant *only* so this branch can exist.

    `046` argues for that column with an accident story -- "a dispatcher that
    cannot see it would happily send through a binding somebody deliberately
    disabled" -- and then nothing anywhere exercised the branch. Of the four
    columns, it is the only one justified by behaviour, and it was the only one
    with no behaviour asserted.

    A separate binding row is used rather than flipping `config-a`, so a failure
    here cannot leave the shared fixture disabled for whatever runs next.
    """

    from fastagent_v2.worker.pipeline import (
        BINDING_DISABLED,
        BINDING_HAS_NO_INSTANCE,
        resolve_binding_guid,
    )

    tenant = seeded_database["tenant_a"]
    conversation_id = uuid4()

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash,
                status, provider_instance_id
            )
            SELECT 'config-disabled', tenant_id, profile_release_id,
                   current_token_hash, 'disabled', 'GUID-DISABLED'
              FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-a'
            ON CONFLICT (config_id) DO UPDATE SET status = 'disabled'
            """,
            (tenant,),
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id, config_id)
            SELECT %s, %s, profile_release_id, 'config-disabled'
              FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-disabled'
            """,
            (tenant, conversation_id, tenant),
        )
        connection.commit()

    with _worker_connection(tenant) as connection:
        guid, reason = resolve_binding_guid(
            connection, tenant_id=tenant, conversation_id=conversation_id
        )

    assert guid is None, "a disabled binding must not hand out its guid"
    assert reason == BINDING_DISABLED
    # The guid *is* there -- the refusal is about status, not absence. Without
    # this line the case would also pass if `status` were ignored and the
    # refusal came from an empty `provider_instance_id`.
    assert reason != BINDING_HAS_NO_INSTANCE


def test_the_dispatcher_actually_reads_a_guid(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """**P1-2.** The success path -- what this batch is *for* -- had no coverage.

    `test_the_worker_can_read_the_guid` goes through raw SQL and never calls the
    resolver; the refusal case reaches `BINDING_HAS_NO_INSTANCE` precisely
    *because* the seeded binding has no guid. So `return str(guid), None` was
    never once observed, in a 全流程 batch whose entire point is reading that
    value.
    """

    from fastagent_v2.worker.pipeline import resolve_binding_guid

    tenant = seeded_database["tenant_a"]
    conversation_id = uuid4()

    with _migration_connection() as connection:
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash,
                provider_instance_id
            )
            SELECT 'config-with-guid', tenant_id, profile_release_id,
                   current_token_hash, 'D05FCC41-CAFE-4932-81A4-6A54E8836AFF'
              FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-a'
            ON CONFLICT (config_id) DO UPDATE
                SET provider_instance_id = 'D05FCC41-CAFE-4932-81A4-6A54E8836AFF'
            """,
            (tenant,),
        )
        connection.execute(
            """
            INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id, config_id)
            SELECT %s, %s, profile_release_id, 'config-with-guid'
              FROM channel_bindings WHERE tenant_id = %s AND config_id = 'config-with-guid'
            """,
            (tenant, conversation_id, tenant),
        )
        connection.commit()

    with _worker_connection(tenant) as connection:
        guid, reason = resolve_binding_guid(
            connection, tenant_id=tenant, conversation_id=conversation_id
        )

    assert reason is None, f"the dispatcher refused a healthy binding: {reason}"
    assert guid == "D05FCC41-CAFE-4932-81A4-6A54E8836AFF"


def test_the_new_read_path_does_not_cross_tenants(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """**P3-1.** `046` claims the worker still only sees its own tenant's rows.

    That is `001`'s doing, not this batch's -- but this batch is the first to
    give the role a way to reach this table, so the claim it makes should be
    checked where it is made. Asked as the dispatcher would ask it: tenant B's
    conversation, tenant A's context.
    """

    from fastagent_v2.worker.pipeline import CONVERSATION_NOT_VISIBLE, resolve_binding_guid

    tenant_a = seeded_database["tenant_a"]
    tenant_b = seeded_database["tenant_b"]
    conversation_id = uuid4()

    # **Seeded, not skipped.** The first version bailed with `pytest.skip` when
    # tenant B had no binding -- and tenant B has no binding, so the assertion
    # the reviewer asked for never ran and the gate reported it as a pass. A
    # test that opts out under its own precondition is the silent-green shape
    # this batch has already been caught by twice.
    with _migration_connection() as connection:
        release = connection.execute(
            "SELECT id FROM profile_releases WHERE tenant_id = %s LIMIT 1", (tenant_b,)
        ).fetchone()
        assert release is not None, "tenant B has no profile release to bind to"
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id, current_token_hash
            ) VALUES ('config-b-crosstenant', %s, %s, repeat('b', 64))
            ON CONFLICT (config_id) DO NOTHING
            """,
            (tenant_b, release["id"]),
        )
        connection.execute(
            "INSERT INTO conversation_aggregates(tenant_id, id, profile_release_id, config_id)"
            " VALUES (%s, %s, %s, 'config-b-crosstenant')",
            (tenant_b, conversation_id, release["id"]),
        )
        connection.commit()

    # Tenant A's context, tenant B's conversation: RLS must hide it, and the
    # refusal must say "not visible" rather than inventing a supply problem.
    with _worker_connection(tenant_a) as connection:
        guid, reason = resolve_binding_guid(
            connection, tenant_id=tenant_b, conversation_id=conversation_id
        )

    assert guid is None
    assert reason == CONVERSATION_NOT_VISIBLE
