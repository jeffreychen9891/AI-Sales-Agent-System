"""The seven common control-plane HTTP contracts (`control-plane-parity.md` §5).

These are the foundation under all 72 target routes (`control-plane-m1a-route-freeze.md`
§五.3), which is the reason this module exists before any route does: a mistake here
is copied 72 times.

**Three of the seven carry a rule JSON Schema cannot state**, and those rules are the
point of this module rather than the shapes:

* `ResourceRef` -- `tenant_id` comes from the authorization context. A schema can say
  the field is a uuid; it cannot say where the value was allowed to come from.
* `SuperuserTarget` -- submitted explicitly on every cross-tenant call, never saved as
  a default context.
* `SecretWrite` -- the response never echoes the value.

Everything here is a value object. Nothing reaches a database, a route or an
`authorize()` call; those arrive in later batches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any, Literal, NewType, cast, get_args
from uuid import UUID, uuid4

from .auth import AuthContext
from .authz import Decision
from .contracts import (
    CONTRACT_ROOT,
    load_control_plane_schema,
    validate_control_plane_contract,
)
from .redaction import REDACTED, redact

#: `ResourceRef.resource_type` must name a resource the catalog knows. An
#: unknown type is not merely unrecognised -- it is unauthorizable, because
#: `authorize()` resolves actions through this same catalog. Rejecting it here
#: keeps a typo from becoming a reference nothing can ever decide on.
MANIFEST_ROOT = CONTRACT_ROOT / "manifests"

#: **Four values, and `unknown` is not optional.** `conversation-engine.md:228`
#: fixes the receipt vocabulary at `accepted|delivered|failed|unknown` and
#: `:504` forbids collapsing an unconfirmed outcome into a confirmed one
#: ("no reliable inverse -> stay UNKNOWN/manual, do not fake a successful
#: rollback"); `channel.py::RECEIPT_STATUSES` already carries those four and
#: `IntentRecord.status` (`:180`) carries `UNKNOWN`. parity §5.2 requires a
#: command receipt for every external channel action, so a receipt *will* have
#: to report an unconfirmed send -- with three values it could only claim
#: `succeeded` or `failed`, which is the forbidden step.
#:
#: Mapping: `SendReceipt.accepted|delivered` -> `succeeded`;
#: `SendReceipt.failed` -> `failed`; `SendReceipt.unknown` and
#: `IntentRecord.UNKNOWN` -> `unknown`, reconciled by the delivery reconcile
#: path, which is also what raises the manual task. (Independent review P1-1.)
CommandStatus = Literal["accepted", "succeeded", "failed", "unknown"]
SecretStatus = Literal["active", "superseded", "revoked"]

#: What a masked secret reads as. **This is `runtime.redaction.REDACTED`, not a
#: second spelling of it.** The first version of this module defined its own
#: `"<redacted>"` and carried a comment claiming the two were identical; they
#: were not (`"[REDACTED]"` vs `"<redacted>"`), so one error body could carry
#: both spellings -- `message` redacted by `redact()` and `masked` by this
#: module -- and both would have been copied into 72 routes and the generated
#: client. `redaction.py` calls itself the repository's single redaction
#: vocabulary; a fourth marker added quietly is exactly what that sentence
#: exists to prevent. (Independent review 2026-09-03, P2-2.)
MASK = REDACTED


#: The tenant the caller was **authenticated** for -- never a value taken from a
#: URL, body or query.
#:
#: **What this type does and does not do** (measured, independent review round 2
#: R2-2; an earlier version of this comment claimed more than was true):
#:
#: * It stops the *implicit* mistake. Passing a request-supplied `UUID` straight
#:   in is a `mypy --strict` error, where before it was a plain `UUID` parameter
#:   that accepted anything and left the rule living in a naming convention.
#: * It does **not** make the unsafe call impossible. An explicit conversion
#:   around the same value type-checks fine -- and that is what someone writes
#:   when they think they know better.
#:
#: So the guarantee is not "impossible", it is "**explicit, and greppable**".
#: Auditing it means grepping for **both** spellings -- this type and the
#: function below -- because searching only the sanctioned one misses exactly
#: the half that bypassed it. `scripts/check_static.py` enforces that both are
#: constructed only in allowlisted files, which is what actually holds the line
#: across 72 routes. The real closure arrives in CP-1, where a route cannot
#: obtain an `AuthContext` it did not authenticate.
AuthorizedTenantId = NewType("AuthorizedTenantId", UUID)


def authorized_tenant(tenant_id: UUID) -> AuthorizedTenantId:
    """Mark a tenant id as coming from verified credentials.

    The **sanctioned** constructor -- not the only possible one: the type itself
    is callable and exported, so an audit has to look for both names, and
    `scripts/check_static.py` is what keeps either from appearing outside the
    files allowed to mint one. Call this only where a credential has just been
    verified. (Independent review round 2, R2-2.)
    """

    return AuthorizedTenantId(tenant_id)


def authorized_tenant_from_context(context: AuthContext) -> AuthorizedTenantId:
    """Take the tenant out of an authenticated context. **One function, not one per route.**

    The first attempt at this put `services/api/.../app.py` on
    `check_static.py`'s allowlist so the routes could call `authorized_tenant()`
    directly -- and that turned the guard off for the **whole file**, which is
    the file all 72 control-plane routes are going to live in. From then on any
    `authorized_tenant(<any UUID>)` in there would have passed, including one
    taken straight off a path parameter. The guard's own comment says why that
    matters: the type stops the implicit mistake, and "72 routes are coming,
    'the author remembers' is not a control" (independent review round 2, R-3).

    So the conversion moves here instead, where the allowlist already reaches,
    and it takes an `AuthContext` rather than a `UUID`: there is no way to hand
    it a value that did not come through authentication. Same shape ADR-0035
    gave `resource_ref_from_wire` -- one controlled crossing, not a permission
    to cross.

    A context whose scope is not exactly one tenant is refused rather than
    guessed at: `all` and `set` scopes reach several tenants, and picking one of
    them here would be this function inventing the answer it exists to protect.
    """

    tenant_ids = context.tenant_scope.tenant_ids
    if context.tenant_scope.kind != "one" or len(tenant_ids) != 1:
        raise ControlPlaneContractError(
            "TENANT_SCOPE_NOT_SINGULAR",
            "an authorized tenant can only be taken from a context scoped to exactly one",
        )
    return AuthorizedTenantId(next(iter(tenant_ids)))


def authorized_tenant_from_decision(decision: Decision) -> AuthorizedTenantId:
    """The tenant an `authorize()` decision authorised. **The cross-tenant half.**

    `authorized_tenant_from_context` answers "which tenant is this caller
    authenticated for", and for every route that acts inside its own tenant that
    is also the tenant being acted on. **A cross-tenant reach separates the
    two** (ADR-0068): the caller is authenticated for their home tenant and the
    resource belongs to another, so the context answer would name the wrong
    tenant -- silently, and in a receipt that is supposed to say what was
    touched.

    ADR-0068 §四点五 already writes this contract for the transaction id: it may
    come only from `Decision.authorized_tenant_id`, never from `tenant_scope`
    and never from the request. **The receipt is the same question asked
    later**, so it gets the same answer from the same place rather than a second
    derivation that could disagree.

    Minting one here rather than in the route is what
    `check_authorized_tenant_is_constructed_only_where_credentials_are_verified`
    requires, and this file is already where the sanctioned constructors live --
    so the guard's allowlist does not grow. What makes the mint legitimate is
    not that a credential was just checked but something stronger: `authorize()`
    matched this value against the resource it decided about before returning it.

    Refuses a deny and refuses a tenant-less decision, because neither has a
    tenant to name and quietly producing one would be the mistake this type
    exists to prevent.
    """

    if not decision.allowed:
        raise ControlPlaneContractError(
            "a denied decision authorises no tenant",
            "decision",
        )
    if decision.authorized_tenant_id is None:
        raise ControlPlaneContractError(
            "this decision is about a tenant-less resource; use platform_resource_ref",
            "decision",
        )
    return AuthorizedTenantId(decision.authorized_tenant_id)


class ControlPlaneContractError(RuntimeError):
    """A control-plane value object was asked to hold something it must not.

    Carries a **machine code**, because §5.2 forbids pages from branching on a
    human-readable message and a foundation that forces its callers to do
    exactly that would have taught 72 routes to do it. `code` is drawn from the
    same vocabulary as `ErrorEnvelope.code`, so a route can map an exception to
    a response without inventing a second table.

    Base is `RuntimeError`, matching `ProfileError`: a bare `except ValueError`
    around request parsing must not be able to swallow a tenant-boundary
    refusal. (Independent review P1-2.)
    """

    def __init__(self, code: str, message: str) -> None:
        # Checked against the same pattern `ErrorEnvelope.code` uses, because
        # this code is destined to *become* one. Unchecked, a raise site with
        # the wrong case built fine and then threw `INVALID_ERROR_CODE` when a
        # route handed it to `error_envelope()` -- a second failure on the error
        # path, which is the worst place to put one. (Round 2, R2-3.)
        if not _error_code_pattern().match(code):
            raise AssertionError(
                f"control-plane error code must be an uppercase machine token: {code!r}"
            )
        super().__init__(message)
        self.code = code


class ResourceNotFound(ControlPlaneContractError):
    """The named resource is not this tenant's, or is not a known type.

    One class for both, deliberately: telling them apart is telling the caller
    whether their guess named something real, which is the cross-tenant
    existence leak `ErrorEnvelope` forbids. The server side records which it
    actually was; that lands with `authorize()` in CP-1.
    """

    def __init__(self, message: str) -> None:
        super().__init__("RESOURCE_NOT_FOUND", message)


@cache
def resource_catalog() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (MANIFEST_ROOT / "resource-catalog.json").read_text(encoding="utf-8")
        ),
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _checked_timestamp(value: str | None, *, field: str) -> str:
    """Validate a caller-supplied timestamp, because the schema does not.

    `format: date-time` is **inert in this repository**: `jsonschema` only
    registers that format when `rfc3339-validator` is installed, and it is not
    in `requirements.lock`, so `{"as_of": "yesterday"}` validates. Routes will
    read `as_of` as a field the schema vouched for. Checking it in the factory
    is the same shape as the `version < 1` and `aggregate_version < 0` guards
    beside it. The repository-wide half -- making `date-time` actually enforce
    -- is registered as backlog, not done here. (Independent review P2-5.)
    """

    if value is None:
        return _utc_now_iso()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlPlaneContractError(
            "INVALID_TIMESTAMP", f"{field} is not an ISO-8601 timestamp: {value!r}"
        ) from error
    # `fromisoformat` alone accepts a bare date and a naive datetime, so the
    # first version of this guard only caught strings that were not timestamps
    # at all -- looser than the docstring implied (round 2, R2-4). `as_of` and
    # `accepted_at` name an instant a client compares against its own clock; a
    # value with no time or no offset cannot do that.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ControlPlaneContractError(
            "INVALID_TIMESTAMP",
            f"{field} must carry a UTC offset, got {value!r}",
        )
    return value


def _checked_enum(value: str, *, allowed: tuple[str, ...], field: str) -> str:
    """`Literal` is erased at runtime and route input arrives as a string.

    Every other guard in this module runs at construction; these two did not,
    so `secret_write_response(status="rotating")` built cleanly and only the
    schema objected -- if anyone thought to validate. (Independent review P3-1.)
    """

    if value not in allowed:
        raise ControlPlaneContractError(
            "INVALID_ENUM_VALUE", f"{field} must be one of {allowed}, got {value!r}"
        )
    return value


@cache
def _error_code_pattern() -> re.Pattern[str]:
    """The machine-token rule for `ErrorEnvelope.code`, taken from the schema.

    Not retyped here. The first version of this guard was `code != code.upper()`,
    which passes for any script without case -- `"出错了".upper()` is `"出错了"` --
    so the factory admitted exactly the prose code §5.2 forbids pages from
    branching on, while the schema rejected it. Reading the pattern from the
    contract makes the two unable to disagree.
    """

    schema = load_control_plane_schema("error-envelope")
    return re.compile(cast(str, schema["properties"]["code"]["pattern"]))


@dataclass(frozen=True)
class ResourceRef:
    """What a command names, and which tenant owns it.

    Built through `resource_ref()`. The constructor is not the guard -- the
    factory is -- because the guard is about *provenance*, and a frozen
    dataclass cannot see where its arguments came from.
    """

    resource_type: str
    resource_id: str
    #: `None` for a resource that belongs to the platform rather than a tenant
    #: (ADR-0070 §三). It was `UUID` until B1 #9 became the first caller that
    #: needed to name one; `resource_ref()` refused outright and said so --
    #: "how a ResourceRef names a global resource is undecided (CP-1)".
    tenant_id: UUID | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            # **`None`, not an omitted key** (ADR-0070 §三). parity §5.1 makes
            # `tenant_id` unconditionally required, so leaving it out would make
            # "this resource has no tenant" and "this version forgot to send it"
            # identical on the wire. `None` is an answer; a missing key is not.
            "tenant_id": str(self.tenant_id) if self.tenant_id is not None else None,
        }


def resource_ref(
    *,
    resource_type: str,
    resource_id: str,
    #: `None` only for a resource the catalogue marks as not tenant-owned
    #: (ADR-0070 §三). The `NewType` still does its job for every other caller:
    #: a plain `UUID` from a request body will not typecheck here.
    authorized_tenant_id: AuthorizedTenantId | None,
    claimed_tenant_id: UUID | None,
) -> ResourceRef:
    """Build a `ResourceRef` whose tenant comes from the authorization context.

    `authorized_tenant_id` is the tenant the caller was *authenticated* for, and
    its type says so -- see `AuthorizedTenantId`. `claimed_tenant_id` is whatever
    arrived in the URL, body or query. The RFC allows the claimed value exactly
    one use -- being matched against the authorized one -- so a mismatch is
    refused here rather than quietly resolved in the request's favour, and a
    match still stores the authorized value.

    **`claimed_tenant_id` has no default.** It was optional, which made the one
    check in this function opt-in: a route that simply forgot the argument got a
    silent pass, and 72 routes only need one such lapse for the rule to stop
    existing there. Callers with nothing to match now write `claimed_tenant_id=None`
    and mean it. (Independent review P2-1.)
    """

    catalog = resource_catalog()
    entry = catalog.get(resource_type)
    if entry is None:
        raise ResourceNotFound(f"resource type is not in the resource catalog: {resource_type!r}")
    if not entry["tenant_owned"]:
        # **CP-1 answered** (ADR-0070 §三). This used to refuse outright, and
        # the refusal said why: naming a global resource was undecided and
        # failing cost nothing because there was no caller. B1 #9 became the
        # caller, so the question got asked and ruled instead of guessed.
        #
        # **The guard is not relaxed, it forks.** A tenant-owned resource still
        # has to match its claimed tenant (2026-09-04 P2-1). A global one
        # accepts *no* claimed tenant at all: claiming a tenant for something
        # that has none is not a mismatch to resolve, it means the caller has
        # the wrong resource type, and answering as though it were fine would
        # hide that.
        if claimed_tenant_id is not None:
            raise ControlPlaneContractError(
                "RESOURCE_NOT_TENANT_OWNED",
                f"{resource_type!r} is not tenant-owned; a claimed tenant cannot apply to it",
            )
        return ResourceRef(
            resource_type=resource_type,
            resource_id=resource_id,
            tenant_id=None,
        )
    if authorized_tenant_id is None:
        # Reached only for a tenant-owned resource, since the global branch
        # above returns. A route that forgot the tenant must not silently mint a
        # reference with none -- that would be the invented value this function
        # was written to refuse.
        raise ControlPlaneContractError(
            "RESOURCE_TENANT_REQUIRED",
            f"{resource_type!r} is tenant-owned; an authorized tenant is required",
        )
    if claimed_tenant_id is not None and claimed_tenant_id != authorized_tenant_id:
        # Deliberately not "cross-tenant access denied": that phrasing tells the
        # caller their guess named something real. `ErrorEnvelope` forbids
        # leaking cross-tenant existence and this is the same rule one layer in.
        raise ResourceNotFound("resource not found")
    return ResourceRef(
        resource_type=resource_type,
        resource_id=resource_id,
        tenant_id=authorized_tenant_id,
    )


@dataclass(frozen=True)
class Page:
    """One cursor-delimited page of a read model.

    `has_more` and `next_cursor` are kept consistent because the pair is what a
    caller loops on: `has_more` true with no cursor strands them mid-collection
    with no way to ask for the rest, and a cursor with `has_more` false invites
    a request for a page that is not there.
    """

    items: tuple[Any, ...]
    next_cursor: str | None
    as_of: str

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "as_of": self.as_of,
        }


def page(*, items: list[Any], next_cursor: str | None = None, as_of: str | None = None) -> Page:
    if next_cursor is not None and not next_cursor:
        raise ControlPlaneContractError(
            "INVALID_CURSOR", "next_cursor must be absent or non-empty"
        )
    return Page(
        items=tuple(items),
        next_cursor=next_cursor,
        as_of=_checked_timestamp(as_of, field="as_of"),
    )


@dataclass(frozen=True)
class ProjectionVersion:
    aggregate_version: int
    profile_release_id: UUID
    as_of: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "aggregate_version": self.aggregate_version,
            "profile_release_id": str(self.profile_release_id),
            "as_of": self.as_of,
        }


def projection_version(
    *, aggregate_version: int, profile_release_id: UUID, as_of: str | None = None
) -> ProjectionVersion:
    if aggregate_version < 0:
        raise ControlPlaneContractError(
            "INVALID_PROJECTION_VERSION", "aggregate_version must not be negative"
        )
    return ProjectionVersion(
        aggregate_version=aggregate_version,
        profile_release_id=profile_release_id,
        as_of=_checked_timestamp(as_of, field="as_of"),
    )


@dataclass(frozen=True)
class CommandReceipt:
    command_id: UUID
    status: CommandStatus
    resource_ref: ResourceRef
    accepted_at: str
    idempotency_key: str
    correlation_id: UUID

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": str(self.command_id),
            "status": self.status,
            "resource_ref": self.resource_ref.as_dict(),
            "accepted_at": self.accepted_at,
            "idempotency_key": self.idempotency_key,
            "correlation_id": str(self.correlation_id),
        }


def command_receipt(
    *,
    resource: ResourceRef,
    idempotency_key: str,
    correlation_id: UUID,
    status: CommandStatus = "accepted",
    command_id: UUID | None = None,
    accepted_at: str | None = None,
) -> CommandReceipt:
    """A receipt for a command that has been accepted, not necessarily executed.

    `idempotency_key` is required rather than defaulted: §5.2 makes it mandatory
    on every create/update command, and a generated default would satisfy the
    schema while destroying the property -- two retries would get two keys and
    the command would run twice.
    """

    if not idempotency_key.strip():
        raise ControlPlaneContractError(
            "IDEMPOTENCY_KEY_REQUIRED", "idempotency_key must not be blank"
        )
    _checked_enum(status, allowed=get_args(CommandStatus), field="status")
    return CommandReceipt(
        command_id=command_id or uuid4(),
        status=status,
        resource_ref=resource,
        accepted_at=_checked_timestamp(accepted_at, field="accepted_at"),
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )


@dataclass(frozen=True)
class FieldError:
    field: str
    code: str


@dataclass(frozen=True)
class ErrorEnvelope:
    code: str
    message: str
    request_id: UUID
    field_errors: tuple[FieldError, ...]
    retryable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "request_id": str(self.request_id),
            "field_errors": [
                {"field": item.field, "code": item.code} for item in self.field_errors
            ],
            "retryable": self.retryable,
        }


def error_envelope(
    *,
    code: str,
    message: str,
    request_id: UUID,
    field_errors: list[FieldError] | None = None,
    retryable: bool = False,
) -> ErrorEnvelope:
    """An error a page can branch on without reading the prose.

    The `message` is passed through `redact()` on the way in, **which covers
    less than it sounds like**. On free text `redact()` matches URLs carrying
    inline credentials and little else: a DSN like
    `postgresql://user:pw@host/db` is masked, while `?access_token=…`,
    a bare `sk-…`, `Authorization: Bearer …` and `signing_key=…` pass through
    untouched. `_SENSITIVE_MARKERS` keys off *mapping keys*, and `message` is a
    bare string, so it never reaches that path.

    An earlier version of this docstring claimed the field was safe against
    "connection string, token or key" -- one of the three -- and the single test
    happened to cover only that one, which made the claim look proven
    (independent review P2-3). Treat `message` as **partially** scrubbed:
    do not put upstream exception text in it unreviewed. Widening `redact()`
    for free text is registered as backlog; it belongs with the redaction
    family, not here.
    """

    if not _error_code_pattern().match(code):
        raise ControlPlaneContractError(
            "INVALID_ERROR_CODE", "error code must be an uppercase machine token, not prose"
        )
    return ErrorEnvelope(
        code=code,
        message=cast(str, redact(message)),
        request_id=request_id,
        field_errors=tuple(field_errors or ()),
        retryable=retryable,
    )


@dataclass(frozen=True)
class SuperuserTarget:
    """One cross-tenant reach, with its reason attached.

    Deliberately has no `store()`, no module-level slot and no default: §5.2
    says it is submitted per call and cannot be saved as a default context, and
    the way to keep that true is to give the codebase nowhere to put it.
    """

    target_tenant_id: UUID
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"target_tenant_id": str(self.target_tenant_id), "reason": self.reason}


def superuser_target(*, target_tenant_id: UUID, reason: str) -> SuperuserTarget:
    """Build one cross-tenant reach.

    `reason` goes through `redact()` for the same reason `ErrorEnvelope.message`
    does: it is operator-typed free text that lands in an audit trail, and
    pasting a credential-bearing URL into a justification is a concrete way for
    one to get there. Two free-text fields on the same boundary should not have
    two different answers. (Independent review P3-4; the coverage caveat in
    `error_envelope` applies here too.)
    """

    if not reason.strip():
        raise ControlPlaneContractError(
            "REASON_REQUIRED", "a cross-tenant reach must carry a reason; blank is not a reason"
        )
    return SuperuserTarget(
        target_tenant_id=target_tenant_id, reason=cast(str, redact(reason))
    )


@dataclass(frozen=True)
class SecretWriteResponse:
    """What a secret write is allowed to say back. Never the value."""

    rotation_id: UUID
    version: int
    status: SecretStatus

    def as_dict(self) -> dict[str, Any]:
        return {
            "rotation_id": str(self.rotation_id),
            "masked": MASK,
            "version": self.version,
            "status": self.status,
        }


def secret_write_response(
    *, rotation_id: UUID, version: int, status: SecretStatus = "active"
) -> SecretWriteResponse:
    """Build the response half of a `SecretWrite`.

    **The secret is not a parameter.** An earlier shape took the value and
    masked it on the way out, which is one edit away from echoing it and gives
    every caller a reason to have the plaintext in scope at the call site. The
    response is built from metadata that was never secret, so there is nothing
    here to leak.
    """

    if version < 1:
        raise ControlPlaneContractError("INVALID_SECRET_VERSION", "secret version starts at 1")
    _checked_enum(status, allowed=get_args(SecretStatus), field="status")
    return SecretWriteResponse(rotation_id=rotation_id, version=version, status=status)


def validate(name: str, value: dict[str, Any], *, fragment: str | None = None) -> None:
    """Validate a control-plane payload against its schema, or one `$defs` half."""

    validate_control_plane_contract(name, value, fragment=fragment)
