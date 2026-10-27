"""`purge_tenant_data` must cover every tenant-owned table (B1 #12, ADR-0092).

**The list inside the function is deliberately explicit** -- a loop over
`information_schema` would silently absorb whatever exists at run time, and a
destructive operation is the last place to want that. The cost of writing the
list by hand is that it can fall behind, and *this* is what keeps it honest: a
new tenant-owned table must either join the purge or be named as an exclusion
by a person.

That is the correction T184 and T185 both needed one layer up: a rule that
covers only what its author was looking at is not a rule.
"""

from __future__ import annotations

import re

import pytest
from psycopg import Connection

from tests.conftest import _migration_url

#: Tenant-owned tables the purge deliberately does not touch. Each needs a
#: reason a reader can check, not just an entry.
DOCUMENTED_EXCLUSIONS = {
    # §6.4: the audit is not deleted. The tenant row survives for it.
    "audit_events",
    # Append-only by 006's trigger; a DELETE raises. Kept because the surviving
    # audit records decisions made *under a release* -- ADR-0092 §一. **Not a
    # claim that profile content is free of personal data**; T189 is open on
    # exactly that question.
    "profile_releases",
    # The job row is the record *of* the purge. Deleting it inside its own
    # transaction would erase the result the worker is about to write.
    "tenant_purge_jobs",
}


def _purged_tables(connection: Connection) -> set[str]:
    body = connection.execute(
        "SELECT prosrc FROM pg_proc WHERE proname = 'purge_tenant_data'"
    ).fetchone()
    assert body is not None, "purge_tenant_data is missing; migration 043 did not apply"
    listed = re.search(r"ordered_tables text\[\] := ARRAY\[(.*?)\]", body[0], re.DOTALL)
    assert listed is not None, "could not read the table list out of purge_tenant_data"
    return set(re.findall(r"'([a-z_]+)'", listed.group(1)))


def _tenant_owned_tables(connection: Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT c.table_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_name = c.table_name
           AND t.table_schema = 'public'
           AND t.table_type = 'BASE TABLE'
         WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
        """
    ).fetchall()
    return {row[0] for row in rows}


def _purge_order(connection: Connection) -> list[str]:
    """The table list **in the order the function will delete them**.

    `_purged_tables` returns a set, which is all the coverage cases need and
    exactly what an ordering case must not use.
    """

    body = connection.execute(
        "SELECT prosrc FROM pg_proc WHERE proname = 'purge_tenant_data'"
    ).fetchone()
    assert body is not None
    listed = re.search(r"ordered_tables text\[\] := ARRAY\[(.*?)\]", body[0], re.DOTALL)
    assert listed is not None
    return re.findall(r"'([a-z_]+)'", listed.group(1))


def _foreign_keys(connection: Connection) -> list[tuple[str, str, bool]]:
    """Every foreign key between two tables, as `(referencing, referenced, cascades)`.

    `confdeltype = 'c'` is `ON DELETE CASCADE`: the parent's delete removes the
    child, so the order does not matter for it. Everything else does.
    """

    rows = connection.execute(
        """
        SELECT src.relname AS referencing,
               tgt.relname AS referenced,
               con.confdeltype = 'c' AS cascades
          FROM pg_constraint con
          JOIN pg_class src ON src.oid = con.conrelid
          JOIN pg_class tgt ON tgt.oid = con.confrelid
         WHERE con.contype = 'f' AND src.relname <> tgt.relname
        """
    ).fetchall()
    return [(row[0], row[1], bool(row[2])) for row in rows]


@pytest.fixture()
def _connection() -> Connection:
    with Connection.connect(_migration_url()) as connection:
        yield connection


def test_every_tenant_owned_table_is_purged_or_named_as_an_exclusion(
    _connection: Connection,
) -> None:
    missing = (
        _tenant_owned_tables(_connection)
        - _purged_tables(_connection)
        - DOCUMENTED_EXCLUSIONS
    )
    assert not missing, (
        f"tenant-owned tables neither purged nor excluded: {sorted(missing)}. "
        "Add them to purge_tenant_data, or to DOCUMENTED_EXCLUSIONS with the "
        "reason they survive a purge."
    )


def test_the_purge_list_names_no_table_that_does_not_exist(
    _connection: Connection,
) -> None:
    """A typo deletes nothing, forever, and says nothing while doing it.

    `format('DELETE FROM public.%I …')` on a misspelled name would raise -- but
    only when a purge actually runs, which on this deployment is never until it
    matters most.
    """

    phantom = _purged_tables(_connection) - _tenant_owned_tables(_connection)
    assert not phantom, f"purge_tenant_data names non-tenant-owned tables: {sorted(phantom)}"


def test_the_exclusions_are_real_tables_too(_connection: Connection) -> None:
    """An exclusion for a table that no longer exists hides a real gap.

    If `profile_releases` were renamed, the exclusion would keep matching
    nothing and the new name would fall through the first case above silently.
    """

    tables = _tenant_owned_tables(_connection)
    stale = DOCUMENTED_EXCLUSIONS - tables
    assert not stale, f"exclusions naming tables that are not tenant-owned: {sorted(stale)}"


def test_the_function_runs_and_reports_per_table_counts(_connection: Connection) -> None:
    """The job records what it destroyed, so the function has to report it.

    Run against a tenant id that owns nothing: every count is zero, and the
    row-per-table shape is what `tenant_purge_jobs.result` is built from.
    """

    rows = _connection.execute(
        "SELECT table_name, deleted FROM purge_tenant_data(%s)",
        ("00000000-0000-4000-8000-000000000000",),
    ).fetchall()
    assert len(rows) >= 20, f"expected a row per purged table; got {len(rows)}"
    assert all(deleted == 0 for _, deleted in rows)
    assert {name for name, _ in rows} == _purged_tables(_connection)


def test_the_worker_holds_no_blanket_delete(_connection: Connection) -> None:
    """The whole reason the deletion is a definer function.

    `fastagent_worker` parses untrusted inbound content. Granting it DELETE
    across the tenant-owned tables so that one route can erase a tenant would
    make that a permanent, general capability. It may execute *this* function
    and nothing more.
    """

    granted = _connection.execute(
        """
        SELECT table_name FROM information_schema.role_table_grants
         WHERE table_schema = 'public'
           AND grantee = 'fastagent_worker'
           AND privilege_type = 'DELETE'
        """
    ).fetchall()
    assert not granted, (
        f"fastagent_worker gained DELETE on {sorted(row[0] for row in granted)}; "
        "the tenant purge is a SECURITY DEFINER function precisely so that it does not."
    )

    can_execute = _connection.execute(
        "SELECT has_function_privilege('fastagent_worker', 'purge_tenant_data(uuid)', 'EXECUTE')"
    ).fetchone()
    assert can_execute is not None and can_execute[0] is True


def test_the_purge_order_satisfies_every_foreign_key(_connection: Connection) -> None:
    """The order is checked against `pg_constraint`, not against reasoning.

    **The first version of `purge_tenant_data` violated four foreign keys**
    (independent review P0-2). Its comment claimed "children before parents" and
    argued correctly about the pairs its author had looked at; the four it had
    not looked at were each triggered by a row any serving tenant has -- a
    policy decision on an intent, a conversation with a contact, a cost row, a
    manager contact. All four are `NO ACTION` and not deferrable, so the purge
    aborted partway and the job failed three times over.

    Deriving the check from the catalogue is the only version of this that
    survives the next table: a referencing table must be deleted **before** the
    table it references, unless that key cascades.
    """

    order = _purge_order(_connection)
    position = {table: index for index, table in enumerate(order)}

    violations = []
    for referencing, referenced, cascades in _foreign_keys(_connection):
        if cascades or referenced not in position:
            # Nothing to order: either the parent's delete takes the child with
            # it, or the referenced table is not something this purge deletes.
            continue
        if referencing not in position:
            # **A table the purge *keeps* pointing at a table it deletes**
            # (re-review R1). The delete aborts just as surely as a
            # mis-ordering, and no reordering can fix it -- the referencing row
            # is never deleted at all. The first version of this check skipped
            # these outright, which gave it the same blind spot as the defect it
            # replaced: it saw only the tables its author had listed.
            #
            # There is no such foreign key today. The point is the next one --
            # a new column on `audit_events`, or a fourth retained table.
            violations.append(
                f"{referencing} is kept by the purge but references {referenced}, "
                "which the purge deletes: add it to purge_tenant_data, or make "
                "that foreign key ON DELETE CASCADE"
            )
            continue
        if position[referencing] > position[referenced]:
            violations.append(f"{referencing} must be deleted before {referenced}")

    assert not violations, (
        "purge_tenant_data would abort on these foreign keys: " + "; ".join(sorted(violations))
    )


def test_the_check_sees_a_retained_table_pointing_at_a_purged_one(
    _connection: Connection,
) -> None:
    """R1's branch, proved not to be inert.

    The re-review demonstrated this by adding a real foreign key from
    `audit_events` to `employees` and watching `purge_tenant_data` abort while
    every coverage case stayed green. Rather than mutate the schema here, the
    comparison is run against a synthetic edge -- the branch under test is a
    property of the *comparison*, not of the catalogue.
    """

    order = _purge_order(_connection)
    position = {table: index for index, table in enumerate(order)}
    assert "employees" in position, "the fixture for this case must name a purged table"

    # `audit_events` is retained; a key from it into a purged table aborts.
    synthetic = [("audit_events", "employees", False)]
    violations = [
        referencing
        for referencing, referenced, cascades in synthetic
        if not cascades and referenced in position and referencing not in position
    ]
    assert violations == ["audit_events"]

    # And the same edge marked CASCADE is not a violation -- the parent's delete
    # removes the child, which is the one way this shape is safe.
    cascading = [("audit_events", "employees", True)]
    assert not [
        referencing
        for referencing, referenced, cascades in cascading
        if not cascades and referenced in position and referencing not in position
    ]


def test_the_order_check_can_see_a_violation(_connection: Connection) -> None:
    """The case above asserts an absence, and a broken query reports one too.

    Feeding the comparison a deliberately reversed order proves it is reading
    real constraints -- without this, a typo in the catalogue query would leave
    the ordering permanently "correct".
    """

    order = list(reversed(_purge_order(_connection)))
    position = {table: index for index, table in enumerate(order)}
    violations = [
        (referencing, referenced)
        for referencing, referenced, cascades in _foreign_keys(_connection)
        if not cascades
        and referencing in position
        and referenced in position
        and position[referencing] > position[referenced]
    ]
    assert violations, "reversing the order must produce violations, or the check is inert"


def test_every_definer_function_is_search_path_pinned_and_not_public(
    _connection: Connection,
) -> None:
    """R3: this batch made `SECURITY DEFINER` the mechanism that does deletion.

    Before it, the definers in this schema were read functions. Now one of them
    erases twenty-one tables, and `043`'s own comment says why the two
    safeguards are not optional: a definer without `SET search_path` resolves
    names against the caller's path -- which is how a definer becomes a
    privilege escalation -- and one that `PUBLIC` may execute is the definer
    being the vulnerability.

    Both were true of all ten definers when this was written. Nothing was
    keeping them true: every author had simply remembered. This is three lines
    of SQL that remember for them.
    """

    # **`has_function_privilege` rather than reading the ACL text.** The first
    # version of this case parsed `proacl` and asked whether it contained
    # `"=X/"` -- which is true of *every* named grant, because an entry reads
    # `fastagent_worker=X/fastagent_migrator`. It reported all ten functions as
    # PUBLIC-executable, including the ones the re-review had just measured as
    # correct. A PUBLIC grant is the entry with an **empty** grantee, and rather
    # than encode that spelling here, this asks the database the question
    # directly -- which also gets the NULL-ACL case right, where the default
    # really does grant EXECUTE to PUBLIC.
    rows = _connection.execute(
        """
        SELECT p.proname,
               coalesce(array_to_string(p.proconfig, ','), '') AS config,
               has_function_privilege('public', p.oid, 'EXECUTE') AS public_execute
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.prosecdef
        """
    ).fetchall()
    assert rows, "no SECURITY DEFINER functions found; this check would be inert"

    unpinned = [name for name, config, _ in rows if "search_path=" not in config]
    assert not unpinned, (
        f"SECURITY DEFINER functions without SET search_path: {sorted(unpinned)}. "
        "Without it the body resolves names against the caller's search path."
    )

    public_executable = [name for name, _, public_execute in rows if public_execute]
    assert not public_executable, (
        f"SECURITY DEFINER functions executable by PUBLIC: {sorted(public_executable)}. "
        "Add REVOKE ALL ON FUNCTION ... FROM PUBLIC and grant only the role that needs it."
    )


def test_the_definer_check_can_see_a_public_grant(_connection: Connection) -> None:
    """The case above asserts two absences; this proves it can report presence.

    Written after the first version reported **every** definer as
    PUBLIC-executable -- a check can be wrong in the noisy direction too, and a
    gate nobody trusts gets deleted. `has_function_privilege` is asked about a
    function that really is executable by PUBLIC (`now()` is) and one that is
    not, so the comparison itself is pinned rather than assumed.
    """

    public_ok, definer_not_public = _connection.execute(
        """
        SELECT has_function_privilege('public', 'now()', 'EXECUTE'),
               has_function_privilege('public', 'purge_tenant_data(uuid)', 'EXECUTE')
        """
    ).fetchone()
    assert public_ok is True, "a function PUBLIC may execute must read as True"
    assert definer_not_public is False, "this batch's definer must not be PUBLIC-executable"
