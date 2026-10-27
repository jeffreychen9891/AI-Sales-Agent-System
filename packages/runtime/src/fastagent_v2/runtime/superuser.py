"""Who is a superuser, and the one place that question is answered.

**ADR-0048 §二.2 (K5)**, in four clauses:

* tokens keep minting `type="employee"` -- there is no second subject type on
  the wire, and nothing about a superuser is signed into a credential;
* the allowlist names employees and lives in the **Stage Secret directory**,
  under the same permission model as every other secret (`700`/`600`, owned by
  the deployment user, checked by preflight);
* `authorize_superuser_action()` reads it;
* **an empty allowlist means nobody is a superuser.**

**Why a file and not a column.** A row in `employees` saying "this person is a
superuser" would be editable by whatever can write that table -- and this
repository has spent five registrations (T127) on the fact that column grants
belong to a shared role. A file in the secret directory is edited by whoever can
reach the host, which is the same authority that holds the signing key. The
answer to "who may promote a superuser" becomes "whoever already owns the
deployment", instead of a new question.

**Where the promotion happens matters more than the list.** K5 names it
explicitly: the promotion must occur **when the `AuthContext` is built**, not
inside a route. `authz.py`'s cross-tenant reason obligation keys on
`subject.type == "superuser"`; a route that checked the allowlist itself would
leave that subject an `employee` for the authorizer, and **every cross-tenant
reach would silently lose its reason requirement** -- which is exactly what the
2026-09-03 independent review (P2-2) fixed.

**A line names a tenant and an employee, not an employee alone.** Independent
review F7: `employees` is keyed `(tenant_id, id)` and `id` alone carries no
unique constraint, so an allowlist of bare ids does a permission promotion on a
key the database does not consider identifying. uuid4 makes the collision
improbable, not impossible, and "improbable" is the wrong standard for the one
file that decides who crosses tenant boundaries.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


class SuperuserAllowlistError(RuntimeError):
    """The allowlist exists but cannot be read as one.

    **Raised rather than skipped**, and the choice is deliberate. Ignoring a
    malformed line is fail-closed in the narrow sense -- nobody is promoted --
    but it produces a deployment that *looks* configured and is not, which is
    the shape this repository keeps registering as "record and fact disagree".
    A configuration error should stop the thing that is misconfigured, at the
    moment it is read, not go quiet and surface as "why is this person getting
    403".

    **Where that moment is, is the part independent review F4 corrected.** It
    used to be "every request", because the only reader was on the hot path --
    so a file that went unreadable after deployment turned every authenticated
    request into a 500. It is now *also* startup: `create_app` resolves the
    list once, so a malformed file stops the process instead of the traffic.
    """


def parse_superuser_allowlist(text: str) -> frozenset[tuple[UUID, UUID]]:
    """One `tenant_id:employee_id` per line; `#` comments and blanks ignored.

    Deliberately not JSON or YAML: the file is edited by a person on a host,
    under time pressure, and a format where a missing bracket silently changes
    the meaning is the wrong one for a list whose emptiness means "nobody".
    """

    entries: set[tuple[UUID, UUID]] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tenant, separator, employee = line.partition(":")
        if not separator:
            raise SuperuserAllowlistError(
                f"line {number} of the superuser allowlist is not "
                f"`tenant_id:employee_id`: {line!r}"
            )
        try:
            entries.add((UUID(tenant.strip()), UUID(employee.strip())))
        except ValueError as error:
            raise SuperuserAllowlistError(
                f"line {number} of the superuser allowlist is not "
                f"`tenant_id:employee_id`: {line!r}"
            ) from error
    return frozenset(entries)


def superuser_identities(allowlist_file: str | None) -> frozenset[tuple[UUID, UUID]]:
    """The `(tenant, employee)` pairs the deployment considers superusers.

    **Empty means nobody.** A missing path and a missing file both answer
    empty, and that is the safe direction: the failure mode of "the file was
    not deployed" is that nobody has platform powers, not that everybody does.

    **A file that exists and cannot be read is not "nobody".** That distinction
    is independent review F4: `PermissionError` and undecodable bytes used to
    fall into the same `except OSError` as "no such file", so a list that had
    been deployed and then became unreadable would answer "nobody is a
    superuser" -- a silent, wrong answer where the loud one is correct.

    **Read on every call, deliberately.** The previous version memoised on
    `(path, mtime_ns, size)`, and independent review F3 measured what that
    costs: rewriting the file at the same size left both stamps identical on
    17 of 20 attempts, so **revoking someone -- which is exactly a same-size
    edit, one uuid pair swapped for another -- could silently not take
    effect**, and the test written to prove otherwise was red 1 run in 8. The
    file is a few hundred bytes and the old path already called `stat()` per
    request; a cache whose miss condition is "the clock ticked" is not worth a
    revocation that does not land.
    """

    if not allowlist_file:
        return frozenset()
    path = Path(allowlist_file)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return frozenset()
    except OSError as error:
        raise SuperuserAllowlistError(
            f"the superuser allowlist at {allowlist_file!r} exists but cannot be read"
        ) from error
    except UnicodeDecodeError as error:
        raise SuperuserAllowlistError(
            f"the superuser allowlist at {allowlist_file!r} is not UTF-8"
        ) from error
    return parse_superuser_allowlist(text)
