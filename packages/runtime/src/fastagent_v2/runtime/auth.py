from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.superuser import superuser_identities

_password_hasher = PasswordHasher()


@dataclass(frozen=True, slots=True)
class Subject:
    id: UUID
    type: Literal["employee", "service", "superuser"]
    status: Literal["active", "disabled"]
    credential_version: int
    #: The tenant this subject *belongs to*, as distinct from the tenants it may
    #: reach (`TenantScope`). The two are the same thing for an ordinary
    #: employee and stop being the same thing the moment a subject is promoted,
    #: which is why the field exists.
    #:
    #: **It is what `authorize()` measures a cross-tenant reach against**
    #: (independent review F1). The rule used to be "a superuser touching any
    #: tenant-owned resource is reaching across", which made a promoted
    #: subject a stranger in their own tenant -- so the first effect of putting
    #: somebody on the allowlist was to lock them out of their day job, with no
    #: route able to supply the reason that would let them back in.
    #:
    #: `None` means "no single home tenant", and every tenant-owned resource is
    #: then a cross-tenant reach. That is the fail-closed direction and it is
    #: the right one: a subject we cannot place cannot be given a free pass.
    home_tenant_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Which tenants a subject may reach, and in what sense (`§3.1`).

    Lives here beside `Subject` because the drawing lists them as one family and
    because `AuthContext` needs it; `authz` imports it rather than the reverse.

    It replaces a bare `frozenset[UUID]`, which **could not express "all"** --
    so the rule that `kind="all"` belongs to superusers alone had nothing to
    attach to. `authz.authorize` enforces that rule, since the check needs the
    subject and this type deliberately does not carry one.
    """

    kind: Literal["one", "set", "all"]
    tenant_ids: frozenset[UUID] = frozenset()

    def covers(self, tenant_id: UUID) -> bool:
        """Whether this scope reaches a **tenant-owned** resource's tenant.

        It no longer takes `None`. The old `None` branch returned `False` and a
        comment said a scoped subject could not reach a tenant-less resource --
        but `authorize` never asked, so the rule lived only in the comment while
        the opposite behaviour ran (independent review P3-1). Tenant-less
        resources are decided by catalog membership plus permission.
        """

        return True if self.kind == "all" else tenant_id in self.tenant_ids


@dataclass(frozen=True, slots=True)
class AuthContext:
    subject: Subject
    tenant_scope: TenantScope
    permissions: frozenset[str]
    auth_method: str
    session_id: UUID | None
    correlation_id: UUID
    authenticated_at: datetime
    deadline_at: datetime


class AuthenticationError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def sha256_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def subject_fingerprint(*, key: str, tenant_id: object, email: str) -> str:
    """An irreversible fingerprint of a person, for tombstones (§6.4).

    **Keyed, and T154 is why.** The first version was a bare SHA-256 over
    `tenant_id:email` -- and both halves are low-entropy: the tenant is a value
    the attacker already has, and the address comes from a namespace anybody can
    enumerate. Hashing a list of candidate addresses and comparing is the whole
    attack, and §6.4 asks for something *irreversible*. An unkeyed digest of a
    guessable input is not.

    **It is also a one-way door**, which is why the ruling could not wait: a
    purge destroys the address, so the fingerprint can never be recomputed. Any
    row written under the old scheme stays under it forever, whatever is
    decided later. Production has purged nobody yet, so the cost of changing it
    today is zero and the cost of changing it after the first real purge is
    "those rows are permanently weaker".

    The output is still 64 hex characters, so `026`'s `CHECK` and the column are
    unchanged -- this replaces how the value is derived, not what it looks like.
    """

    message = f"{tenant_id}:{email.strip().lower()}".encode()
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def constant_time_token_match(raw: str, expected_hash: str) -> bool:
    return secrets.compare_digest(sha256_token(raw), expected_hash)


def issue_access_token(
    *,
    settings: Settings,
    employee_id: UUID,
    tenant_id: UUID,
    permissions: frozenset[str],
    credential_version: int,
    session_id: UUID,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "fastagent-v2",
        "aud": "fastagent-control-plane",
        "sub": str(employee_id),
        "tenant_ids": [str(tenant_id)],
        "permissions": sorted(permissions),
        "credential_version": credential_version,
        "session_id": str(session_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        "jti": str(uuid4()),
    }
    return jwt.encode(claims, settings.access_signing_key, algorithm="HS256")


def decode_access_token(*, settings: Settings, raw_token: str) -> AuthContext:
    try:
        claims = jwt.decode(
            raw_token,
            settings.access_signing_key,
            algorithms=["HS256"],
            audience="fastagent-control-plane",
            issuer="fastagent-v2",
        )
        now = datetime.now(UTC)
        tenant_ids = frozenset(UUID(value) for value in claims["tenant_ids"])
        # A bearer token is always scoped to the tenants it names -- never
        # `all`. `all` is reachable only by a superuser path that does not go
        # through this decoder, which is why nothing here can mint one.
        scope = TenantScope(kind="one" if len(tenant_ids) == 1 else "set", tenant_ids=tenant_ids)
        # **The promotion happens here, and K5 says so in as many words.**
        # The token still carries `type="employee"` -- nothing about a
        # superuser is signed into a credential -- and the deployment's
        # allowlist is what decides. Doing this in a route instead would leave
        # the subject an `employee` for `authorize()`, and the cross-tenant
        # reason obligation keys on exactly this field: **every cross-tenant
        # reach would silently lose its reason requirement**, which is what the
        # 2026-09-03 review (P2-2) fixed.
        #
        # **The tenant scope is not widened.** parity §5.2 says a superuser
        # target is submitted per call and 「不能保存为默认上下文」, so a
        # promoted subject keeps the scope its token names. Widening to
        # `kind="all"` here would also break every ordinary route, which reads
        # `next(iter(tenant_scope.tenant_ids))`.
        #
        # **What this does not do, stated plainly** (independent review F2).
        # An earlier version of this comment said a promoted subject "reaches
        # another tenant only by naming it in a request". No such channel
        # exists: `authorize()` checks `tenant_scope.covers()` *before* the
        # superuser branch, and no route in this repository passes
        # `request_facts` at all -- so today a promoted subject naming another
        # tenant is answered `RESOURCE_NOT_FOUND`, measured, and there is a
        # test below that pins it. The sentence described a mechanism that
        # B1 #9-#12 still have to build; until they do, promotion widens
        # nothing. **That is the specification for those four routes**: the
        # per-call target has to enter the authorization decision itself, not
        # be honoured by a route that lets itself through -- the latter moves
        # the decision back out of the authorizer, which is what the
        # 2026-09-03 review (P2-2) removed.
        subject_id = UUID(claims["sub"])
        # **Paired with a tenant, not bare** (F7): `employees` is keyed
        # `(tenant_id, id)`, and a promotion keyed on something the database
        # does not treat as identifying is a promotion that can, in principle,
        # land on the wrong person.
        allowlist = superuser_identities(settings.superuser_allowlist_file)
        is_superuser = any((tenant_id, subject_id) in allowlist for tenant_id in tenant_ids)
        return AuthContext(
            subject=Subject(
                id=subject_id,
                type="superuser" if is_superuser else "employee",
                status="active",
                credential_version=int(claims["credential_version"]),
                # One tenant in the token is the ordinary case and the only one
                # that names a home; a multi-tenant token has no single home and
                # says so with `None`.
                home_tenant_id=next(iter(tenant_ids)) if len(tenant_ids) == 1 else None,
            ),
            tenant_scope=scope,
            permissions=frozenset(str(value) for value in claims["permissions"]),
            auth_method="bearer",
            session_id=UUID(claims["session_id"]),
            correlation_id=uuid4(),
            authenticated_at=now,
            deadline_at=now + timedelta(seconds=settings.runtime_deadline_seconds),
        )
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("invalid access credential") from exc


# `require_allowed` used to live here, taking a permission *string* and raising.
# It made the decision and enforced it in one step, so there was no `Decision` to
# record, no obligations to discharge and nothing to audit -- the drawing's
# §3.1 asks for `authorize() -> Decision` then `require_allowed(decision)`.
# Both now live in `authz.py`, the single entry point (T36).


def new_write_only_token() -> str:
    return secrets.token_urlsafe(32)
