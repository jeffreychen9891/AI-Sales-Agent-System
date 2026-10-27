"""C4 / ADR-0048 §二.2 (K5): who is a superuser, and where that is decided.

**The case that carries this batch is
`test_the_promotion_reaches_the_authorizer_and_not_just_the_token`.** The
allowlist itself is a text file and a set; what makes it correct is *where* it
is applied. K5 names that in as many words: the promotion happens when the
`AuthContext` is built, because `authorize()`'s cross-tenant reason obligation
keys on `subject.type`. A route that consulted the allowlist itself would leave
the subject an `employee` for the authorizer and **every cross-tenant reach
would lose its reason requirement** -- the thing the 2026-09-03 review fixed.

**Independent review F1 is why the last three cases exist in this shape.** The
first version of this module tested the reason obligation against the subject's
*own* tenant and let that stand as correct. It was not: the promoted subject
was refused there, with no way for any caller to supply the reason that would
have let them through. The batch could not see it because **no case ever named
a second tenant**, so "refused in your own tenant" and "refused in somebody
else's" were the same assertion. They are now three separate cases.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.auth import decode_access_token, issue_access_token
from fastagent_v2.runtime.authz import authorize, tenant_resource_ref
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.superuser import (
    SuperuserAllowlistError,
    parse_superuser_allowlist,
    superuser_identities,
)


def _settings(tmp_path: Path, *, allowlist: str | None) -> Settings:
    base = Settings.from_env("api")
    if allowlist is None:
        return base
    path = tmp_path / "superuser.allowlist"
    path.write_text(allowlist, encoding="utf-8")
    return replace(base, superuser_allowlist_file=str(path))


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def test_an_absent_or_empty_allowlist_means_nobody(tmp_path: Path) -> None:
    """**Empty means nobody, and that is the direction that has to be safe.**

    The failure mode of "the file was never deployed" must be that nobody holds
    platform powers -- not that the check is skipped and everybody does.
    """

    assert superuser_identities(None) == frozenset()
    assert superuser_identities(str(tmp_path / "not-there")) == frozenset()
    empty = tmp_path / "empty"
    empty.write_text("# nobody yet\n\n", encoding="utf-8")
    assert superuser_identities(str(empty)) == frozenset()


def test_comments_and_blank_lines_are_ignored() -> None:
    tenant, first, second = uuid4(), uuid4(), uuid4()
    text = (
        f"# platform on-call\n{tenant}:{first}\n\n"
        f"  {tenant}:{second}  # second pair of hands\n"
    )

    assert parse_superuser_allowlist(text) == frozenset({(tenant, first), (tenant, second)})


def test_a_line_without_a_tenant_is_refused(tmp_path: Path) -> None:
    """**Independent review F7.**

    `employees` is keyed `(tenant_id, id)`; `id` alone carries no unique
    constraint. A bare id in this file is a permission promotion keyed on
    something the database does not consider identifying -- and this is the one
    file that decides who may cross tenant boundaries. The old format is not
    silently accepted, because "accepted with a different meaning" is worse than
    a deployment that stops.
    """

    with pytest.raises(SuperuserAllowlistError):
        parse_superuser_allowlist(f"{uuid4()}\n")


def test_a_malformed_line_raises_rather_than_being_skipped() -> None:
    """**Skipping would be fail-closed and still wrong.**

    Nobody is promoted either way, but a silently-ignored line produces a
    deployment that *looks* configured and is not -- and the symptom arrives
    later as "why is this person getting 403". A configuration error should stop
    the thing that is misconfigured, when it is read.
    """

    with pytest.raises(SuperuserAllowlistError):
        parse_superuser_allowlist(f"{uuid4()}:{uuid4()}\n{uuid4()}:not-a-uuid\n")


def test_a_revocation_that_does_not_change_the_file_size_takes_effect(
    tmp_path: Path,
) -> None:
    """**Independent review F3, and the measurement is the point.**

    The previous version memoised on `(path, mtime_ns, size)` and the case
    written to prove edits were picked up rewrote the file with a comment about
    being "large enough to differ in size" -- while both versions were one uuid
    pair and therefore *exactly* the same size. The reviewer measured the rest:
    two `stat()` calls around a same-size rewrite returned identical stamps 17
    times in 20, and the case itself was red 1 run in 8.

    **Revocation is precisely the same-size edit** -- one identity swapped for
    another, or a line removed and another added. This case now performs that
    edit and demands the new answer, with no sleep and nothing that depends on
    the clock's granularity.
    """

    path = tmp_path / "superuser.allowlist"
    tenant, first, second = uuid4(), uuid4(), uuid4()
    path.write_text(f"{tenant}:{first}\n", encoding="utf-8")
    assert superuser_identities(str(path)) == frozenset({(tenant, first)})

    path.write_text(f"{tenant}:{second}\n", encoding="utf-8")
    assert len(f"{tenant}:{first}\n") == len(f"{tenant}:{second}\n"), (
        "the case is only worth anything if the rewrite is the same size"
    )
    assert superuser_identities(str(path)) == frozenset({(tenant, second)})


def test_a_path_that_cannot_be_read_at_all_is_not_nobody(tmp_path: Path) -> None:
    """The `OSError` half of F4, and a green probe is why it is here.

    `test_a_file_that_exists_but_cannot_be_decoded_is_not_nobody` covers
    undecodable bytes, which raise `UnicodeDecodeError` -- so a probe that put
    `return frozenset()` back into the `OSError` branch changed nothing and
    stayed green. `PermissionError` and `IsADirectoryError` are the branch that
    matters in production: a file whose mode was tightened, or a mount that did
    not land as a file.
    """

    directory = tmp_path / "superuser.allowlist"
    directory.mkdir()

    with pytest.raises(SuperuserAllowlistError):
        superuser_identities(str(directory))


def test_a_file_that_exists_but_cannot_be_decoded_is_not_nobody(tmp_path: Path) -> None:
    """**Independent review F4**: "unreadable" and "absent" are different answers.

    They used to share an `except OSError` that answered `frozenset()`, so an
    allowlist that had been deployed and then became unreadable would quietly
    report that nobody is a superuser. Quiet and wrong is the failure this
    repository keeps paying for; loud and wrong-looking is recoverable.
    """

    path = tmp_path / "superuser.allowlist"
    path.write_bytes(b"\xff\xfe not utf-8 at all\n")

    with pytest.raises(SuperuserAllowlistError):
        superuser_identities(str(path))


# --------------------------------------------------------------------------
# Where it is applied
# --------------------------------------------------------------------------


def _token(settings: Settings, employee_id: UUID, tenant_id: UUID) -> str:
    return issue_access_token(
        settings=settings,
        employee_id=employee_id,
        tenant_id=tenant_id,
        permissions=frozenset({"tenant.read"}),
        credential_version=1,
        session_id=uuid4(),
    )


def test_a_listed_employee_is_promoted_when_the_context_is_built(tmp_path: Path) -> None:
    """K5: the token still says `employee`; the deployment decides."""

    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{tenant_id}:{employee_id}\n")
    raw = _token(settings, employee_id, tenant_id)

    context = decode_access_token(settings=settings, raw_token=raw)

    assert context.subject.type == "superuser"
    # **The scope is not widened.** parity §5.2: a superuser target is submitted
    # per call and may not be saved as a default context.
    assert context.tenant_scope.kind == "one"
    assert context.tenant_scope.tenant_ids == frozenset({tenant_id})


def test_an_unlisted_employee_stays_an_employee(tmp_path: Path) -> None:
    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{tenant_id}:{uuid4()}\n")
    raw = _token(settings, employee_id, tenant_id)

    context = decode_access_token(settings=settings, raw_token=raw)

    assert context.subject.type == "employee"


def test_the_same_id_under_a_different_tenant_is_not_promoted(tmp_path: Path) -> None:
    """**Independent review F7, the half that bites.**

    The allowlist names `(tenant, employee)`. An entry listing this employee id
    under somebody else's tenant must not promote them here -- which is the
    whole reason the tenant is in the key.
    """

    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{uuid4()}:{employee_id}\n")

    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, tenant_id)
    )

    assert context.subject.type == "employee"


def test_the_promotion_reaches_the_authorizer_and_not_just_the_token(
    tmp_path: Path,
) -> None:
    """**K5's actual claim**, restated after the F1 ruling.

    This case used to assert that the cross-tenant reason obligation fired for a
    listed subject -- against that subject's *own* tenant, which is how F1 hid
    here for a whole round. Once the criterion measures against the home tenant,
    the obligation is no longer observable through a real token at all: the only
    resources a promoted subject can name are ones inside their own scope, and
    those are no longer a reach. **That is F2, seen from the other side.**

    So the claim this module can still carry is the one K5 actually makes: the
    promotion happens when the `AuthContext` is built, which is what puts
    `subject.type` and `home_tenant_id` in front of `authorize()` at all. What
    the authorizer then does with a genuine cross-tenant reach is pinned where a
    context can be constructed directly -- `test_authorization_entry_point.py`,
    `test_a_superuser_reaching_another_tenant_must_give_a_reason`.
    """

    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{tenant_id}:{employee_id}\n")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, tenant_id)
    )

    # Both halves of what `authorize()` reads, set by the decoder and by nothing
    # else -- a route that consulted the allowlist itself would leave both wrong.
    assert context.subject.type == "superuser"
    assert context.subject.home_tenant_id == tenant_id


def test_a_promoted_subject_keeps_working_in_their_own_tenant(
    tmp_path: Path,
) -> None:
    """**Independent review F1, and the ruling that closed it (2026-09-05).**

    For one round this case existed with the opposite assertion, because that
    was what the code did: `crosses_tenant` was `subject.type == "superuser" and
    resource.tenant_id is not None`, so a promoted subject counted as reaching
    across tenants **inside their own**, and no route passes `request_facts`, so
    the reason that would have let them through could not be supplied. The first
    effect of adding somebody to the allowlist was to lock them out of their own
    tenant -- promotion granted nothing and took something away.

    The rule now measures against `subject.home_tenant_id`. Note what this case
    does **not** say: it does not say a superuser may go anywhere. The next case
    is the one that holds that line.
    """

    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{tenant_id}:{employee_id}\n")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, tenant_id)
    )

    assert context.subject.type == "superuser"
    assert context.subject.home_tenant_id == tenant_id

    decision = authorize(
        context=context,
        action="tenant.read",
        resource=tenant_resource_ref(resource_type="tenant", tenant_id=tenant_id),
    )

    assert decision.allowed
    assert not any(item.kind == "reason_required" for item in decision.obligations)


def test_a_promoted_subject_reaches_another_tenant_only_by_naming_it(
    tmp_path: Path,
) -> None:
    """**ADR-0068, and this case used to assert the opposite.**

    For one day it read `..._cannot_reach_another_tenant_even_with_a_reason`
    and pinned `RESOURCE_NOT_FOUND`, with a docstring saying the deny *was the
    specification* for the batch that would close the gap. This is that batch,
    so the case turns over -- and it is worth noticing that the old assertion
    was correct at the time and is wrong now, which is exactly what a
    specification-shaped test is for.

    Three answers, one per condition, because a channel that opened on any two
    of them would still pass a case that only checked the third.
    """

    employee_id, home, other = uuid4(), uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{home}:{employee_id}\n")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, home)
    )
    elsewhere = tenant_resource_ref(resource_type="tenant", tenant_id=other)

    named = authorize(
        context=context,
        action="tenant.read",
        resource=elsewhere,
        request_facts={"target_tenant_id": str(other), "reason": "incident 4711"},
    )
    unnamed = authorize(
        context=context,
        action="tenant.read",
        resource=elsewhere,
        request_facts={"reason": "incident 4711"},
    )
    wrong_target = authorize(
        context=context,
        action="tenant.read",
        resource=elsewhere,
        request_facts={"target_tenant_id": str(home), "reason": "incident 4711"},
    )

    assert named.allowed
    # The reach still carries both obligations -- the channel does not replace
    # them, it only makes the reach possible at all.
    assert {item.kind for item in named.obligations} == {"audit", "reason_required"}
    assert not unnamed.allowed
    assert unnamed.reason_code == "CROSS_TENANT_REASON_REQUIRED"
    # **Naming some other tenant does not open this one.** Without this, a
    # target that merely had to be present would let one reach stand in for any.
    assert not wrong_target.allowed
    assert wrong_target.reason_code == "CROSS_TENANT_REASON_REQUIRED"


def test_an_unpromoted_subject_naming_a_tenant_still_gets_the_flat_404(
    tmp_path: Path,
) -> None:
    """The channel is for superusers, and the refusal for everyone else is unchanged.

    An ordinary employee who discovers the field names and submits both still
    gets `RESOURCE_NOT_FOUND` -- the same answer as "does not exist", because
    for them the distinction would be an existence oracle. Only a subject the
    *deployment* has promoted sees the more informative refusal, and they were
    already entitled to know the tenant exists.
    """

    employee_id, home, other = uuid4(), uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist="")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, home)
    )

    decision = authorize(
        context=context,
        action="tenant.read",
        resource=tenant_resource_ref(resource_type="tenant", tenant_id=other),
        request_facts={"target_tenant_id": str(other), "reason": "incident 4711"},
    )

    assert not decision.allowed
    assert decision.reason_code == "RESOURCE_NOT_FOUND"


def test_the_channel_is_not_remembered_between_calls(tmp_path: Path) -> None:
    """parity §5.2「不能保存为默认上下文」, asserted rather than described.

    The same context object is used twice: once naming the target, once not.
    If anything about the first call were retained -- on the context, in a
    cache, anywhere -- the second would succeed, and the per-call requirement
    would be a comment rather than a property.
    """

    employee_id, home, other = uuid4(), uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist=f"{home}:{employee_id}\n")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, home)
    )
    elsewhere = tenant_resource_ref(resource_type="tenant", tenant_id=other)

    first = authorize(
        context=context,
        action="tenant.read",
        resource=elsewhere,
        request_facts={"target_tenant_id": str(other), "reason": "incident 4711"},
    )
    second = authorize(context=context, action="tenant.read", resource=elsewhere)

    assert first.allowed
    assert not second.allowed
    assert context.tenant_scope.tenant_ids == frozenset({home})


def test_an_ordinary_employee_needs_no_reason_for_their_own_tenant(
    tmp_path: Path,
) -> None:
    """The other half: promotion must not make everybody pay the toll.

    Without this, a change that promoted the wrong subjects would look correct
    -- the demanding case above would still pass.
    """

    employee_id, tenant_id = uuid4(), uuid4()
    settings = _settings(tmp_path, allowlist="")
    context = decode_access_token(
        settings=settings, raw_token=_token(settings, employee_id, tenant_id)
    )

    decision = authorize(
        context=context,
        action="tenant.read",
        resource=tenant_resource_ref(resource_type="tenant", tenant_id=tenant_id),
    )

    assert decision.allowed
    assert not any(item.kind == "reason_required" for item in decision.obligations)
