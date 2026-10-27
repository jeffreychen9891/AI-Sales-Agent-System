"""K3 (ADR-0047 §二.6): the three default system roles.

The batch this closes had a finding attached to it. When the ruling was made,
`operator` and `viewer` would have been **byte-identical**: every write on the
conversation surface was either service-only (`conversation_aggregate.update`,
T98) or had no catalogued action at all. Shipping two roles with the same
permissions is a boundary that reads like one and is not -- the T92 / T98
family. K1's command resources are what gave `operator` something to hold, which
is why the order in the preconditions list was K1 → K3 and not the reverse.

So the case that matters most here is
`test_operator_and_viewer_are_not_the_same_role`. The rest establish that the
two computed roles really are computed -- an `owner` frozen as a literal list
would pass a "does owner exist" test forever while quietly missing every action
added after it was written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.runtime.authz import (
    assignable_action_set,
    assignable_permissions,
    platform_resources,
)
from fastagent_v2.runtime.system_roles import (
    OPERATOR_ACTIONS,
    VIEWER_SNAPSHOT,
    ensure_system_roles,
    system_role_permissions,
)
from psycopg import Connection
from psycopg.rows import dict_row


def _migration_url() -> str:
    value = os.getenv("FASTAGENT_MIGRATION_DATABASE_URL", "")
    if not value:
        pytest.skip("integration database is not configured")
    return value


def _connection() -> Connection[Any]:
    return Connection.connect(_migration_url(), row_factory=dict_row)


def _roles_of(tenant_id: UUID) -> dict[str, dict[str, Any]]:
    with _connection() as connection:
        rows = connection.execute(
            "SELECT name, permissions, is_system, description FROM roles WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
    return {str(row["name"]): row for row in rows}


def _seed_roles_for(tenant_id: UUID) -> None:
    with _connection() as connection:
        ensure_system_roles(connection, tenant_id=tenant_id)
        connection.commit()


# --------------------------------------------------------------------------
# the definitions
# --------------------------------------------------------------------------


def test_operator_and_viewer_are_not_the_same_role() -> None:
    """The finding this batch had to wait for K1 to resolve.

    Asserted in **both** directions. "Operator has something viewer lacks" alone
    would still pass if operator were viewer plus one action, and a role that is
    a strict superset of the read-only role is not the scoped surface the ruling
    describes -- `viewer` can read employees and roles, and `operator`
    deliberately cannot.
    """

    roles = system_role_permissions()

    assert roles["operator"] != roles["viewer"]
    assert roles["operator"] - roles["viewer"], "operator must hold something viewer cannot"
    assert roles["viewer"] - roles["operator"], "viewer must read things operator cannot"
    # And specifically: the two K1 command resources are the difference that
    # made the distinction possible at all.
    assert {"conversation_takeover.create", "conversation_resume.create"} <= (
        roles["operator"] - roles["viewer"]
    )


def test_owner_is_every_grantable_permission_computed_not_listed() -> None:
    """`owner` is a rule, so it is compared to the rule's own source.

    Comparing it to a literal list written in this file would be comparing a
    snapshot to a snapshot: both would go stale together and agree while doing
    it.

    **The equality came back with ADR-0070 §四.** For one commit this case
    subtracted platform actions, because they were assignable and merely kept
    out of the roles. They are not assignable at all now, so there is nothing to
    subtract -- and the case that guards *that* is the one below, which asks the
    catalogue directly rather than asking `owner` about an absence.
    """

    assert system_role_permissions()["owner"] == assignable_action_set()


def test_no_platform_action_is_assignable_at_all() -> None:
    """**ADR-0070 §四**, and the reason it is a separate case.

    B1 #9 kept platform actions out of the system roles and left them in
    `assignable_permissions()`, so `GET /permissions` still offered them to
    every holder of `permission.list` -- a permission `viewer` holds -- and an
    administrator could build a role around one that then answered 403 forever.
    That route's own docstring names the rule: offering a permission that can
    never take effect is worse than offering none.

    Asked of the assignable catalogue rather than of the roles, because "not in
    `owner`" was already true while the door was open.
    """

    platform = platform_resources()
    assignable = {item["resource"] for item in assignable_permissions()}

    assert platform, "the assertion proves nothing if no resource is platform"
    assert not (platform & assignable)


def test_viewer_holds_only_actions_that_read() -> None:
    roles = system_role_permissions()

    for action in roles["viewer"]:
        assert action.rpartition(".")[2] in {"read", "list"}
    # ...and it is not empty, which a broken filter would also satisfy.
    assert len(roles["viewer"]) >= 5


def test_viewer_matches_its_snapshot_so_it_cannot_grow_in_silence() -> None:
    """**T114, delivered as a mechanism rather than as a criterion** (B2 #1 审查 P2-4).

    T114's expiry said the batch that adds a read action must answer whether it
    belongs in `viewer` "**而不是让它自动进**". B2 #1 answered, and wrote the
    criterion down -- but a criterion in a comment only works while somebody
    remembers it, and the half it exists for has not arrived: B2 #2/#4 add reads
    over credential-derived values, which the criterion says must **not** join
    `viewer`, and without this they would join it in the same commit that added
    them.

    So the rule still computes the set -- no per-action maintenance, no drift --
    and the computed answer is compared against a snapshot. Every action that
    joins costs one reviewable line, next to the criterion that decides it.
    """

    assert system_role_permissions()["viewer"] == VIEWER_SNAPSHOT


def test_a_viewer_that_grew_stops_the_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """And the failure is a failure, not a warning.

    `ensure_system_roles()` runs inside `migrate.py`, so a `viewer` that gained a
    read it was never meant to hold stops the deploy instead of quietly widening
    a role on every tenant. The fix is one line in `VIEWER_SNAPSHOT` -- taken
    after the criterion has been applied, which is the whole point.
    """

    import fastagent_v2.runtime.system_roles as module

    monkeypatch.setattr(module, "VIEWER_SNAPSHOT", frozenset({"role.list"}))

    with pytest.raises(ValueError) as caught:
        module.system_role_permissions()
    assert "VIEWER_SNAPSHOT" in str(caught.value)


def test_no_system_role_holds_a_permission_nobody_may_be_granted() -> None:
    """The seeded roles are subject to T98 like any other role.

    A platform-seeded role holding a service-only action would be the worst
    version of what T98 fixed: not an administrator making a mistake, but the
    product shipping one.
    """

    from fastagent_v2.runtime.authz import service_only_actions

    grantable = assignable_action_set()
    platform = service_only_actions()
    for name, permissions in system_role_permissions().items():
        assert permissions <= grantable, f"{name} holds something not grantable"
        assert not (permissions & platform), f"{name} holds a service-only action"


def test_operator_names_only_actions_that_exist() -> None:
    """A typo in the enumerated set would be stored and then denied on use.

    `system_role_permissions()` refuses at build time rather than seeding it;
    this is that refusal asserted from the outside.
    """

    assert OPERATOR_ACTIONS <= assignable_action_set()


# --------------------------------------------------------------------------
# the seeding
# --------------------------------------------------------------------------


def test_seeding_creates_the_three_roles_marked_as_system(
    seeded_database: dict[str, Any],
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))

    _seed_roles_for(tenant_id)

    roles = _roles_of(tenant_id)
    assert {"owner", "operator", "viewer"} <= set(roles)
    for name in ("owner", "operator", "viewer"):
        assert roles[name]["is_system"] is True, f"{name} must be a system role"
        assert roles[name]["description"], f"{name} must say what it is"


def test_seeding_stores_exactly_the_computed_permissions(
    seeded_database: dict[str, Any],
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))

    _seed_roles_for(tenant_id)

    roles = _roles_of(tenant_id)
    expected = system_role_permissions()
    for name in ("owner", "operator", "viewer"):
        stored = set(roles[name]["permissions"])
        assert stored == set(expected[name]), f"{name} stored a different set"
        # Sorted on the way in, so "did this change" stays answerable later --
        # the same reasoning `_validated_permissions` gives for wire input.
        assert roles[name]["permissions"] == sorted(roles[name]["permissions"])


def test_seeding_twice_changes_nothing_and_resyncs_a_drifted_role(
    seeded_database: dict[str, Any],
) -> None:
    """Idempotence is not the interesting half; the re-sync is.

    It runs on every migrate precisely so that a tenant created before an action
    existed picks it up. This drives that: the role is deliberately corrupted,
    and the next seeding must repair it rather than skip an existing row.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_roles_for(tenant_id)
    first = _roles_of(tenant_id)["owner"]["permissions"]

    with _connection() as connection:
        connection.execute(
            "UPDATE roles SET permissions = %s::jsonb WHERE tenant_id = %s AND name = 'owner'",
            (json.dumps(["role.list"]), tenant_id),
        )
        connection.commit()

    _seed_roles_for(tenant_id)

    assert _roles_of(tenant_id)["owner"]["permissions"] == first


def test_seeding_one_tenant_leaves_another_tenants_rows_untouched(
    seeded_database: dict[str, Any],
) -> None:
    """Per tenant, not global -- asserted by comparing snapshots.

    **The first version of this test asserted `uuid4() != tenant_id`**, which is
    true of every pair of uuids ever generated and never read tenant A at all
    (independent review 2026-09-05, P3-1). The property was real; the assertion
    was not testing it. That is "the anchor matched, but not the thing" -- the
    same failure shape as a probe whose mutation missed.
    """

    tenant_a = UUID(str(seeded_database["tenant_a"]))
    tenant_b = UUID(str(seeded_database["tenant_b"]))
    _seed_roles_for(tenant_a)
    before = {
        name: (row["permissions"], row["is_system"], row["description"])
        for name, row in _roles_of(tenant_a).items()
    }

    _seed_roles_for(tenant_b)

    assert {"owner", "operator", "viewer"} <= set(_roles_of(tenant_b))
    after = {
        name: (row["permissions"], row["is_system"], row["description"])
        for name, row in _roles_of(tenant_a).items()
    }
    assert after == before, "tenant A's rows changed when tenant B was seeded"


# --------------------------------------------------------------------------
# Independent review 2026-09-05: P2-1, P2-2
# --------------------------------------------------------------------------


def test_a_tenants_own_role_of_the_same_name_is_not_taken_over(
    seeded_database: dict[str, Any],
) -> None:
    """P2-1: the platform may only rewrite rows the platform owns.

    `roles`'s unique key is `(tenant_id, name)` and `POST /roles` reserves no
    names, so a tenant can create a role called `viewer`. Before the
    `WHERE roles.is_system` condition, the next deploy would have **widened** it
    to the platform's ten read permissions, set `is_system` -- making it
    undeletable by the person who created it -- and left no trace at all: no
    `audit_events` row (the platform path writes none) and not even a moved
    `updated_at`.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO roles(tenant_id, id, name, description, permissions, is_system)
            VALUES (%s, gen_random_uuid(), 'viewer', 'the tenant made this', %s::jsonb, false)
            """,
            (tenant_id, json.dumps(["role.list"])),
        )
        connection.commit()

    _seed_roles_for(tenant_id)

    theirs = _roles_of(tenant_id)["viewer"]
    assert theirs["permissions"] == ["role.list"], "their permissions were rewritten"
    assert theirs["is_system"] is False, "their role was adopted by the platform"
    assert theirs["description"] == "the tenant made this"
    # The other two are still seeded -- one clash does not stop the rest.
    assert {"owner", "operator"} <= set(_roles_of(tenant_id))


def test_the_clash_is_reported_rather_than_passing_silently(
    seeded_database: dict[str, Any],
) -> None:
    """A silent skip and a successful sync look identical from outside.

    The returned counts are the only signal `seed_system_roles()` has to print,
    so the skip has to be visible in them.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO roles(tenant_id, id, name, description, permissions, is_system)
            VALUES (%s, gen_random_uuid(), 'viewer', 'theirs', '[]'::jsonb, false)
            """,
            (tenant_id,),
        )
        connection.commit()

    with _connection() as connection:
        counts = ensure_system_roles(connection, tenant_id=tenant_id)
        connection.commit()

    assert counts["viewer"] == -1, "a skipped role must be distinguishable"
    assert counts["owner"] > 0 and counts["operator"] > 0


def test_resyncing_moves_updated_at(seeded_database: dict[str, Any]) -> None:
    """P2-1: a re-sync really does change the row, so the column must say so."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    _seed_roles_for(tenant_id)
    with _connection() as connection:
        first = connection.execute(
            "SELECT updated_at FROM roles WHERE tenant_id = %s AND name = 'owner'",
            (tenant_id,),
        ).fetchone()["updated_at"]

    _seed_roles_for(tenant_id)

    with _connection() as connection:
        second = connection.execute(
            "SELECT updated_at FROM roles WHERE tenant_id = %s AND name = 'owner'",
            (tenant_id,),
        ).fetchone()["updated_at"]
    assert second > first


def test_the_deployment_entry_point_seeds_every_tenant(
    seeded_database: dict[str, Any],
) -> None:
    """P2-2: `seed_system_roles()` -- the layer deploy actually calls -- had never run.

    The four seeding cases all called `ensure_system_roles()`, the single-tenant
    layer. The loop that deploy uses (walk `tenants`, one transaction each,
    after the SQL migrations) was covered by nothing, and in the gate it is
    necessarily a no-op: no migration inserts a tenant, and the fixture creates
    them **after** `migrate.py` has run. "In use" and "verified" are two
    different things, and this repository paid for that distinction in T44.
    """

    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("migrate_module", root / "scripts/migrate.py")
    assert spec and spec.loader
    migrate = importlib.util.module_from_spec(spec)
    sys.modules["migrate_module"] = migrate
    spec.loader.exec_module(migrate)

    with _connection() as connection:
        connection.execute("DELETE FROM roles")
        connection.commit()

    with Connection.connect(_migration_url()) as connection:
        migrate.seed_system_roles(connection)
        connection.commit()

    for key in ("tenant_a", "tenant_b"):
        roles = _roles_of(UUID(str(seeded_database[key])))
        assert {"owner", "operator", "viewer"} <= set(roles), f"{key} was not seeded"
        assert all(roles[name]["is_system"] for name in ("owner", "operator", "viewer"))
