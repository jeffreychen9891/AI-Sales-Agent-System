"""The one authorization decision entry point (`unified-authorization-layer.md` §3).

The drawing's first decision summary is that v2 has **one** authorization
language and **one** `authorize()` entry point, and that HTTP, WebSocket, the
worker, background tasks and superuser paths may not each invent their own
tenant check. Until now the repository had none of that: `require_allowed` took
a permission *string* and raised, with no `ResourceRef`, no `Decision` and no
obligations (registered as T36).

**Deny is the default and the only silent outcome is a denial.** Every path
through `authorize` that does not end in an explicit allow returns a deny with a
reason code; there is no fall-through that returns allow.

**`ResourceRef` here is the domain type, not the wire DTO** -- they are two
different types with the same name, which ADR-0035 settles deliberately rather
than renaming either. The wire one lives in `control_plane.py`, is what a client
sees, and has a mandatory `tenant_id`. This one is built only on the server,
carries `attributes` for the policy to judge, and allows a null `tenant_id`
because non-tenant-owned resources exist (`service_health`). Conversion is
one-way and happens in exactly one function here -- see
`resource_ref_from_wire`, and the birth gate that keeps it that way.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from .auth import AuthContext, AuthorizationError, TenantScope
from .contracts import CONTRACT_ROOT

JsonValue = Any

DecisionEffect = Literal["allow", "deny"]
ObligationKind = Literal["audit", "redact", "manual_review", "reason_required"]

#: §3.2. A verb outside this set is not a typo to be tolerated -- it is an action
#: nobody has written a default-deny test for.
ACTION_VERBS = frozenset(
    {
        "create", "read", "list", "update", "delete", "execute", "approve",
        "reconcile", "export", "manage_secret", "assume_tenant", "schedule",
        "cancel", "reschedule", "claim", "enqueue", "invalidate",
    }
)

#: §3.2 forbids these in ordinary roles. They are refused outright rather than
#: merely denied, because a permission set containing one is a configuration
#: mistake, and denying it quietly would let that mistake sit unnoticed.
FORBIDDEN_ACTION_TOKENS = frozenset({"manage_all", "admin", "*"})

#: Resources that belong to the platform rather than to any tenant, **read from
#: the catalogue** (ADR-0070 §五).
#:
#: The first version of this was a hard-coded `frozenset` here, written to close
#: a live escalation quickly, and a reviewer's probe showed what it cost: adding
#: a `tenant_owned: false` resource with a write action to the catalogue passed
#: every gate and put the action into **every tenant's `owner`**, because the
#: authorizer only knew the names somebody had remembered to list (B1 #9, P1-1).
#:
#: The catalogue is already this repository's machine-readable contract -- it is
#: where `service_actions` is declared -- so a parallel Python set was one
#: sentence with two implementations, the family B1 #5's review named. Closing a
#: hole fast is worth doing; leaving the fast shape as the permanent one is not.
#:
#: `check_static.py` requires every tenant-less entry to declare `platform`, so
#: the next one cannot be added without somebody choosing a side.
def platform_resources() -> frozenset[str]:
    """Names of the tenant-less resources marked `platform` in the catalogue."""

    return frozenset(
        name
        for name, entry in _resource_catalog().items()
        if not entry["tenant_owned"] and entry.get("platform")
    )


def platform_only_actions() -> frozenset[str]:
    """Actions on **tenant-owned** resources that only the deployment may grant.

    **The half `platform_resources()` cannot express** (ADR-0075). That one asks
    about a resource: is this whole thing the platform's? `tenant` is not -- a
    merchant reads their own tenant through `GET /tenants/current`, and
    `tenant.read` has to stay in `owner` and `viewer`. What is the platform's is
    one *action* on it: deactivating a tenant.

    Declared per action in the catalogue, the shape `service_actions` already
    uses, so that "which actions are the deployment's" is a fact in the contract
    rather than a set somebody remembered to update -- the same argument
    ADR-0070 §五 made for the resource-level flag after a reviewer's probe
    walked straight past a hard-coded Python frozenset.
    """

    return frozenset(
        action
        for entry in _resource_catalog().values()
        for action in entry.get("platform_only_actions", ())
    )


POLICY_ID = "m1a-baseline"
POLICY_VERSION = "1.0.0"


@cache
def _resource_catalog() -> Mapping[str, Mapping[str, Any]]:
    """The catalog, immutable.

    It is authorization evidence: `entry["actions"]` decides what may be done.
    Handing out the parsed dict let any holder in the process edit the criteria
    the decision is made against (independent review P3-2).

    **Every list that is evidence gets the same treatment**, which the first
    version of `service_actions` did not: freezing `actions` alone left a
    mutable list deciding what may be *granted*, in a mapping whose docstring
    says "immutable" (independent review 2026-09-05, F2). The rule is the shape,
    not the key name -- so the conversion is written as "freeze the lists",
    and a future third list is covered without anybody remembering to add it.
    """

    raw = json.loads(
        (CONTRACT_ROOT / "manifests" / "resource-catalog.json").read_text(encoding="utf-8")
    )
    return MappingProxyType(
        {
            name: MappingProxyType(
                {
                    key: tuple(value) if isinstance(value, list) else value
                    for key, value in entry.items()
                }
            )
            for name, entry in raw.items()
        }
    )


def _catalog_entry(resource_type: str) -> Mapping[str, Any]:
    """Look a resource type up, or refuse. Shared by all three constructors.

    They used to disagree: two checked the catalog and `resource_ref_from_wire`
    did not (independent review P3-3). Three constructors with three strictness
    levels are three different answers to "is this a real resource".
    """

    entry = _resource_catalog().get(resource_type)
    if entry is None:
        raise AuthorizationError(f"{resource_type!r} is not in the resource catalog")
    return entry


#: True only while one of this module's three named constructors is building a
#: `ResourceRef`. A `ContextVar` rather than a module global so concurrent
#: requests cannot lend each other the privilege.
_MINTING: ContextVar[bool] = ContextVar("authz_minting", default=False)


@contextmanager
def _minting() -> Iterator[None]:
    token = _MINTING.set(True)
    try:
        yield
    finally:
        _MINTING.reset(token)


@dataclass(frozen=True)
class ResourceRef:
    """What is being acted on. **Domain type -- server-constructed only.**

    **Construction outside this module raises**, and that is deliberate rather
    than decorative. The first version relied on a text-scanning birth gate, and
    the independent review demonstrated three ordinary Python spellings that
    walked straight past it -- one of them the repository's own parenthesised
    multi-line import style, and one of them `dataclasses.replace`, which needs
    no name at all and produced a *cross-tenant* ref in the reviewer's probe.

    A text scan cannot see construction; it can only see spellings. So the rule
    moved into the type: `__post_init__` refuses unless one of the three named
    constructors is on the stack. `dataclasses.replace` builds a new instance
    and therefore runs `__post_init__` too, which is what closes the spelling-
    free hole. (Independent review 2026-09-03, P1-1.)
    """

    type: str
    id: str | None
    tenant_id: UUID | None
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _MINTING.get():
            raise AuthorizationError(
                "a domain ResourceRef may only be built by authz's named "
                "constructors (resource_ref_from_wire / tenant_resource_ref / "
                "platform_resource_ref); dataclasses.replace is refused for the "
                "same reason"
            )


@dataclass(frozen=True)
class Obligation:
    kind: ObligationKind
    payload: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    effect: DecisionEffect
    policy_id: str
    policy_version: str
    reason_code: str
    obligations: tuple[Obligation, ...] = ()
    #: The tenant this decision authorised, and **the only place a route should
    #: get the tenant it opens its transaction with** (independent review F3).
    #:
    #: Every route today writes
    #: `database.transaction(tenant_id=next(iter(context.tenant_scope.tenant_ids)))`
    #: -- the *caller's* tenant. That was indistinguishable from the right
    #: answer while a subject could only ever reach its own tenant. ADR-0068
    #: makes a cross-tenant reach possible, and then the two come apart: the
    #: reach is authorised for the target while RLS is still pinned to home, so
    #: the query succeeds in the authorizer and finds nothing in the database.
    #:
    #: **The alternative is worse than it looks**: a route could take the tenant
    #: from the request field it just submitted, which puts a caller-supplied id
    #: into `app.tenant_id` -- the one input this repository's RLS design is
    #: built to never trust. Reading it from here means the value was checked
    #: against `resource.tenant_id` by `authorize()` before it existed.
    #:
    #: `None` on a deny, and on an allow for a tenant-less resource.
    authorized_tenant_id: UUID | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


def _deny(reason_code: str) -> Decision:
    return Decision(
        effect="deny",
        policy_id=POLICY_ID,
        policy_version=POLICY_VERSION,
        reason_code=reason_code,
    )


def _allow(
    reason_code: str,
    obligations: tuple[Obligation, ...],
    *,
    authorized_tenant_id: UUID | None = None,
) -> Decision:
    return Decision(
        effect="allow",
        policy_id=POLICY_ID,
        policy_version=POLICY_VERSION,
        reason_code=reason_code,
        obligations=obligations,
        authorized_tenant_id=authorized_tenant_id,
    )


def resource_ref_from_wire(
    wire: Any, *, attributes: dict[str, JsonValue] | None = None
) -> ResourceRef:
    """**The only** wire-DTO-to-domain conversion in the system (ADR-0035).

    One function, not one per route: conversion written 72 times is the shape
    CP-0 was made small to avoid, and each copy is a chance to get the tenant
    wrong. `scripts/check_static.py` enforces that nothing else builds a domain
    `ResourceRef`, so a route cannot hand-roll this even by accident.

    The wire DTO's `tenant_id` is mandatory and already comes from the
    authorization context (`control_plane.resource_ref`), so this does not
    re-derive it -- it carries it across. `attributes` are supplied by the
    caller because they are server-side facts about the resource; the DTO has no
    such field and a client must never be able to influence one.
    """

    resource_type = str(wire.resource_type)
    if not _catalog_entry(resource_type)["tenant_owned"]:
        raise AuthorizationError(
            f"{resource_type!r} is not tenant-owned; a wire ref cannot name one"
        )
    with _minting():
        return ResourceRef(
            type=resource_type,
            # Kept verbatim. The first version parsed it as a uuid and silently
            # fell back to `None` when it was not one -- turning "this one
            # resource" into "this whole resource type" without a word
            # (independent review P3-3). `id` is policy input; widening it
            # quietly is the last thing it should do.
            id=str(wire.resource_id),
            tenant_id=UUID(str(wire.tenant_id)),
            attributes=MappingProxyType(dict(attributes or {})),
        )


def tenant_resource_ref(
    *,
    resource_type: str,
    tenant_id: UUID,
    resource_id: str | UUID | None = None,
    attributes: dict[str, JsonValue] | None = None,
) -> ResourceRef:
    """A reference to a tenant-owned resource, built from server-side facts.

    The third and last named constructor. It exists because the first version of
    this module had only the wire converter and the platform builder, and the
    two routes migrated in this batch have *neither* -- they act on a resource
    identified by the authenticated tenant, with no client-supplied DTO in
    sight. Without this they built a `ResourceRef` inline, which the birth gate
    caught (and which the gate's first version missed -- see
    `check_domain_resource_ref_is_built_only_in_the_authz_module`).

    `tenant_id` is required and typed, so the caller cannot pass `None` and
    accidentally produce a reference that reads as tenant-less.
    """

    if not _catalog_entry(resource_type)["tenant_owned"]:
        raise AuthorizationError(
            f"{resource_type!r} is not tenant-owned; use platform_resource_ref"
        )
    with _minting():
        return ResourceRef(
            type=resource_type,
            id=str(resource_id) if resource_id is not None else None,
            tenant_id=tenant_id,
            attributes=MappingProxyType(dict(attributes or {})),
        )


def platform_resource_ref(
    *, resource_type: str, attributes: dict[str, JsonValue] | None = None
) -> ResourceRef:
    """A reference to a resource that belongs to no tenant.

    Separate from the converter on purpose: this one can only be reached from
    server code that already knows the resource is global, and it cannot be fed
    a client value at all. The wire DTO deliberately refuses these
    (`RESOURCE_NOT_TENANT_OWNED`, user's 2026-09-03 ruling) -- that refusal is
    correct at the HTTP edge and would be wrong here.
    """

    if _catalog_entry(resource_type)["tenant_owned"]:
        raise AuthorizationError(f"{resource_type!r} is tenant-owned; use tenant_resource_ref")
    with _minting():
        return ResourceRef(
            type=resource_type,
            id=None,
            tenant_id=None,
            attributes=MappingProxyType(dict(attributes or {})),
        )


@cache
def command_resources() -> frozenset[str]:
    """Catalog entries that are a command rather than a noun (K1, ADR-0048 §二.1).

    The catalog used to be one shape: a resource, and the verbs you may apply to
    it. That shape cannot hold a finite command like "take over this
    conversation" -- `takeover` is not one of §3.2's seventeen primitives, and
    the ruling is that the primitive set stays closed (ADR-0043 §二.1). So a
    command whose permission has to be **grantable on its own** becomes its own
    resource and is created: `conversation_takeover.create`.

    A command whose permission does *not* need separate granting takes the other
    road -- it maps onto `update` / `delete` and shares that action with its
    neighbours. Route and tests stay two; the action becomes one. That costs the
    "different action" third of `control-plane-parity.md:180`, which ADR-0048
    accepts explicitly and requires to be named wherever it happens; the gate
    `check_shared_actions_are_named_where_they_are_shared` is where "required"
    becomes "enforced".

    **So a catalog entry now needs one question asked of it: is this a thing, or
    a command?** Three keys answer three different questions about an entry and
    it is worth keeping them apart:

    * `kind` -- thing or command (this function);
    * `service_actions` -- which of its actions no person may hold (T98,
      `service_only_actions()`);
    * `tenant_owned` -- whether its rows belong to a tenant.

    They are independent. A command resource is never service-only: a command
    exists because a **person** needs to be granted it, which is the opposite of
    what `service_actions` records.
    """

    return frozenset(
        name for name, entry in _resource_catalog().items() if entry.get("kind") == "command"
    )


def action_is_well_formed(action: str) -> bool:
    """§3.2: `<resource>.<verb>`, verb from the fixed primitive set."""

    if action in FORBIDDEN_ACTION_TOKENS or "*" in action:
        return False
    resource, separator, verb = action.partition(".")
    if not separator or not resource or verb not in ACTION_VERBS:
        return False
    return resource not in FORBIDDEN_ACTION_TOKENS


@cache
def service_only_actions() -> frozenset[str]:
    """Actions no person can trigger from the control plane. A fact, not a spelling.

    T98: `assignable_permissions()` used to be filtered by
    `action_is_well_formed` alone -- a pure syntax predicate -- so every action
    whose verb happened to be in `ACTION_VERBS` was offered to an administrator
    as something a role could hold. Twelve of them are the platform's own:
    `inbox_event.claim/create`, `scheduled_event.claim/enqueue/invalidate`,
    `outbox_message.create/execute/reconcile`,
    `intent_record.create/approve/execute` and `conversation_aggregate.update`.
    **No route calls `authorize()` with any of them** -- the worker, scheduler
    and ingress do the work instead. Said that precisely because the manifest
    has one row that names `inbox_event.create`: the ingress callback
    (`POST /wecom_proxy/{config_id}/{path_token}`), which authenticates by path
    token before any subject exists and is registered in the gate's
    `ROUTES_WITHOUT_INLINE_AUTHORIZE` for that reason. A reader grepping the
    manifest meets that row first (independent review 2026-09-05, F4).
    Two of the twelve are named for the sending M1a deliberately does not
    deliver.

    The user's criterion (2026-09-05) is the one used here: **could a person
    plausibly trigger this action from the control plane?** If not, it does not
    belong in an administrator's list of grantable permissions -- offering it
    reads as a capability and behaves as a denial, which is the failure mode
    this repository keeps paying for.

    **It is declared in the catalog rather than computed.** "Which actions are
    the platform's" is a fact about the system, not a consequence of how an
    action is spelled; deriving it from "no route authorizes it today" would
    silently reclassify every action the moment its route is built, which is
    exactly the 76-route backlog we are working through.

    Two judgement calls, named so they can be argued with:

    * `conversation_aggregate.update` is here even though freeze B3 #6/#7
      (takeover/resume) are things a **person** does to a conversation. Those
      routes do not exist yet and the catalog has no action for them. When they
      land they should get their **own** action rather than borrowing the
      worker's write -- a human taking over and the pipeline advancing a state
      version are not the same authority, and one permission covering both is
      how an operator ends up able to do more than the page suggests.
    * `intent_record.approve` reads like a human verb, and the RFC's intent saga
      does have an approval step. Today that approval is the policy engine's,
      not a person's, and M1a does not deliver a route for it. Same treatment as
      above: when a human approval route exists, it should name its own action.
    """

    return frozenset(
        action
        for entry in _resource_catalog().values()
        for action in entry.get("service_actions", ())
    )


@cache
def assignable_permissions() -> tuple[Mapping[str, Any], ...]:
    """Every permission a role may hold, derived from the catalog. Never a copy of it.

    Route freeze B1 #26 (`C0A-029`) is explicit that this list is **generated
    from the v2 resource catalog** rather than copied from the old system's
    scattered constants. So this reads the same file `authorize()` decides
    against; there is no second list to drift.

    **What this actually computes is "the action is spelled legally"** --
    `action_is_well_formed` is a pure syntax predicate -- and the first version
    of this docstring claimed it computed "what `authorize()` could allow".
    Those differ by a whole class (independent review 2026-09-04, P2-1).

    What it does exclude: the bootstrap actions (`tenant.resolve_for_login`,
    `channel_config.resolve_for_auth`, `inbox_event.consume`), whose verbs sit
    outside `ACTION_VERBS` on purpose because they name paths that run before
    there is a subject to authorize.

    **It also excludes the platform's own actions** -- `service_only_actions()`,
    declared in the catalog. That exclusion is T98, and until 2026-09-05 it was
    missing: this list was filtered by spelling alone, so twelve worker actions
    were offered to administrators as grantable. They were inert only because
    nothing reads `roles` yet; they would have stopped being inert the day
    freeze B1 #16 wires roles into `authorize()`. Making "assignable" a fact in
    the catalog rather than a side effect of spelling was the user's ruling
    (2026-09-05), not this function's choice -- see `service_only_actions()`
    for the criterion and for the two judgement calls inside it.

    The same predicate (`action_is_well_formed`) is what CP-2's catalog gate
    checks, so this list and that gate cannot disagree about which actions are
    real.
    """

    platform_actions = service_only_actions()
    listing = []
    for resource_type, entry in sorted(_resource_catalog().items()):
        for action in sorted(entry["actions"]):
            if not action_is_well_formed(action) or action in platform_actions:
                continue
            if action in platform_only_actions():
                # **The tenant-owned half of the same rule** (ADR-0075). The
                # check below asks whether a whole resource is the platform's;
                # this one asks whether a single action on an otherwise
                # merchant-owned resource is. `tenant.update` deactivates a
                # tenant, and `owner` is *defined* as every grantable action --
                # so without this line every merchant administrator would hold
                # it, could disable their own tenant, and would revoke their own
                # sessions doing it, with nothing in M1a able to undo that
                # (B1 #10 left the milestone in ADR-0074).
                continue
            if not entry["tenant_owned"] and entry.get("platform"):
                # **Platform actions are not assignable at all** (ADR-0070 §四).
                # They used to be listed here, which meant `GET /permissions`
                # offered them to every holder of `permission.list` -- a
                # permission in `viewer` -- and a tenant administrator could
                # build a role around one that then answered 403 forever.
                #
                # This route's own docstring names the rule: "offering an
                # administrator a permission that can never take effect is worse
                # than offering none". B1 #9 applied that to the *system roles*
                # and left this door open (independent review P2-1); both are
                # shut now.
                #
                # **They are still permissions** -- a subject must still hold
                # one -- but the only way to hold one is for the deployment to
                # put it there, the same authority that writes the superuser
                # allowlist.
                continue
            listing.append(
                MappingProxyType(
                    {
                        "action": action,
                        "resource": resource_type,
                        "verb": action.partition(".")[2],
                        "tenant_owned": bool(entry["tenant_owned"]),
                    }
                )
            )
    return tuple(listing)


def assignable_action_set() -> frozenset[str]:
    """The same list as a set, for validating a submitted permission array."""

    return frozenset(item["action"] for item in assignable_permissions())


def _named_target(facts: dict[str, JsonValue]) -> UUID | None:
    """The `target_tenant_id` a caller submitted, as a `UUID`, or `None`.

    **Parsed rather than string-compared** (independent review F5). The first
    version compared `str(target).strip()` against `str(resource.tenant_id)`, so
    an upper-case UUID, one in braces, or one with interior whitespace was read
    as *a different tenant* and refused. Nobody calls this yet, which is exactly
    why it was worth fixing now: the first consuming route would have met it as
    a puzzling refusal rather than as a rule.
    """

    raw = facts.get("target_tenant_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw).strip())
    except (ValueError, AttributeError, TypeError):
        return None


def authorize(
    *,
    context: AuthContext,
    action: str,
    resource: ResourceRef,
    request_facts: dict[str, JsonValue] | None = None,
    now: datetime | None = None,
) -> Decision:
    """The single decision point. Returns a `Decision`; never raises for a deny.

    Order matters and is deliberate: the checks that reveal the least come
    first. Everything the caller could learn from *which* deny it got is kept
    behind the permission check, so nothing about the catalog or about another
    tenant is observable to a caller who does not already hold the permission.

    **Tenant-less resources are decided by catalog membership plus permission,
    not by tenant scope.** `TenantScope.covers(None)` returning `False` used to
    suggest otherwise, but `authorize` never asked it about a tenant-less
    resource, so the stricter reading existed only in a comment while the looser
    one ran (independent review P3-1). The looser one is also the right one: a
    scoped employee holding `service_health.read` should be able to read service
    health. The dead branch is gone rather than the behaviour changed.
    """

    facts = request_facts or {}
    moment = now or datetime.now(UTC)

    if not action_is_well_formed(action):
        return _deny("ACTION_UNKNOWN")

    if context.subject.status != "active":
        return _deny("SUBJECT_DISABLED")

    if moment >= context.deadline_at:
        # §3.1: a runtime resource boundary, not a delivery deadline.
        return _deny("REQUEST_DEADLINE_EXCEEDED")

    if context.tenant_scope.kind == "all" and context.subject.type != "superuser":
        # §3.1: service principals may not hold `all`. A service that reached
        # here holds a scope it was never entitled to, so refuse before looking
        # at anything else about the request.
        return _deny("TENANT_SCOPE_NOT_PERMITTED")

    if action not in context.permissions:
        return _deny("AUTHORIZATION_DENIED")

    # The catalog is consulted **after** the permission check, not before.
    # Ordered the other way, `ACTION_UNKNOWN` versus `AUTHORIZATION_DENIED` let
    # any authenticated caller -- including a disabled one, or one past its
    # deadline -- enumerate which `(resource_type, action)` pairs the catalog
    # grants. Behind the permission check the difference is only visible to a
    # caller who already holds the permission. (Independent review P2-1.)
    entry = _resource_catalog().get(resource.type)
    if entry is None or action not in entry["actions"]:
        return _deny("ACTION_UNKNOWN")

    platform_action = resource.type in platform_resources() or action in platform_only_actions()
    if platform_action and context.subject.type != "superuser":
        # **After the permission check on purpose**, so the refusal is only
        # reachable by a caller who already holds the action.
        #
        # An earlier version of this comment went further and claimed it "tells
        # nobody which platform actions exist". **That was not true and is now
        # deleted** (independent review P2-1): `GET /permissions` lists every
        # catalogued action including these, and `permission.list` is in
        # `viewer` -- so the existence of platform actions is public to anyone
        # who can read the permission catalogue. Ordering this check late is
        # worth doing anyway; claiming an information-hiding property the system
        # does not have is not.
        #
        # Its own code rather than `AUTHORIZATION_DENIED`: an
        # administrator holding the permission and still being refused needs to
        # know it is the *identity* that is missing, not the grant -- otherwise
        # the obvious next step is to grant it again, which changes nothing.
        return _deny("PLATFORM_ACTION_REQUIRES_SUPERUSER")

    if bool(entry["tenant_owned"]) != (resource.tenant_id is not None):
        # A tenant-owned resource with no tenant, or a global one carrying a
        # tenant, is a reference nobody can decide on rather than one to guess at.
        return _deny("RESOURCE_TENANT_MISMATCH")

    if resource.tenant_id is not None and not context.tenant_scope.covers(resource.tenant_id):
        # **The one way past a scope this subject does not hold** (ADR-0068,
        # ruled 2026-09-06 from the T162 design). Before it, a promoted subject
        # could not reach *any* other tenant -- `covers()` is decided here,
        # ahead of the superuser branch below, and C4 deliberately does not
        # widen the scope because parity §5.2 forbids saving a superuser target
        # as a default context. The measured consequence was that promotion
        # granted nothing at all, and B1 #10-#12 could not be built.
        #
        # Four conditions, and each one is doing work:
        #
        #   * **superuser**, which is decided by a file on the deployment host
        #     (K5 / C4), never by anything in the request;
        #   * **an explicit target that equals this resource's tenant** -- so
        #     the channel widens nothing beyond the single resource the caller
        #     already named. It is not a scope, and naming one tenant does not
        #     help with the next request;
        #   * **a reason** -- but enforced **once**, by the `crosses_tenant`
        #     block below, not a second time here. The first version checked it
        #     in both places and justified the duplicate with a window between
        #     the two steps; `authorize()` is a pure decision that commits
        #     nothing and holds no lock, so **there is no such window** and the
        #     justification was wrong (independent review F2). It was also
        #     unfalsifiable: deleting the duplicate left every case green,
        #     because the block below answers with the same code (F1). One
        #     sentence, one implementation -- the same rule B1 #5's review
        #     applied to the permission union;
        #   * nothing is stored. `AuthContext` is untouched, so the next request
        #     starts from the same place -- which is what 「不能保存为默认上下文」
        #     means literally.
        #
        # The refusal below is `CROSS_TENANT_REASON_REQUIRED`, not
        # `RESOURCE_NOT_FOUND`: to a subject the deployment has already
        # promoted, "you did not name a target and a reason" reveals nothing
        # about whether the tenant exists -- they are entitled to know that
        # either way. For everyone else the answer is unchanged, and it is
        # deliberately the same code the wire layer uses for "not yours",
        # because distinguishing it from "does not exist" would tell the caller
        # their guess named something real.
        if context.subject.type != "superuser":
            return _deny("RESOURCE_NOT_FOUND")
        if _named_target(facts) != resource.tenant_id:
            return _deny("CROSS_TENANT_REASON_REQUIRED")

    obligations: list[Obligation] = [
        # **The target travels in the audit payload** (T163). A cross-tenant
        # reach that leaves no trace is the shape this repository least wants,
        # and the route discharging this obligation should not have to
        # reconstruct which tenant was reached from its own local variables --
        # the authorizer already decided it, so the authorizer states it.
        Obligation(
            kind="audit",
            payload={
                "action": action,
                "resource_type": resource.type,
                "target_tenant_id": str(resource.tenant_id) if resource.tenant_id else None,
            },
        )
    ]
    # **Keyed on the reach, not on the shape of the scope.** The first version
    # asked whether `tenant_scope.kind == "all"`, so a superuser holding
    # `kind="set"` reached a second tenant in the set with no reason and no
    # obligation at all. §0.5 / §4.4 / §5.2 require a target and a reason on
    # *every* cross-tenant reach and say nothing about scope shape.
    # (Independent review P2-2.)
    #
    # **And measured against the subject's home tenant, not against "any
    # tenant".** The version in between said "a superuser touching any
    # tenant-owned resource is that reach", which was written while nothing
    # could mint a superuser and was therefore never felt. C4 made the promotion
    # real and it was felt immediately: a promoted subject counted as reaching
    # across tenants **while working inside their own**, and since no route
    # passes `request_facts`, the reason that would discharge the obligation
    # could not be supplied by anyone. Putting somebody on the allowlist locked
    # them out of their own tenant and granted nothing -- promotion was a strict
    # loss of access. (Independent review F1; ruled 2026-09-05.)
    #
    # A second tenant *inside the subject's own scope* is still a reach, which
    # is the part P2-2 added and this keeps: `home_tenant_id` is one tenant, not
    # the scope.
    crosses_tenant = (
        context.subject.type == "superuser"
        and resource.tenant_id is not None
        and resource.tenant_id != context.subject.home_tenant_id
    )
    if crosses_tenant:
        # §5.2's `SuperuserTarget` requires a reason on every cross-tenant reach;
        # the obligation is how that requirement reaches the caller.
        obligations.append(
            Obligation(
                kind="reason_required",
                payload={"target_tenant_id": str(resource.tenant_id)},
            )
        )
        if not str(facts.get("reason", "")).strip():
            return _deny("CROSS_TENANT_REASON_REQUIRED")

    return _allow(
        "ALLOWED", tuple(obligations), authorized_tenant_id=resource.tenant_id
    )


def require_allowed(decision: Decision) -> None:
    """§3.1. Raises on a deny; the reason code is the message.

    Replaces the previous `require_allowed(context, permission, tenant_id)`,
    which made the decision *and* enforced it in one step and therefore had no
    decision to record, no obligations to discharge and nothing to audit.
    """

    if not decision.allowed:
        raise AuthorizationError(decision.reason_code)


def require_obligations_discharged(
    decision: Decision, discharged: frozenset[ObligationKind]
) -> None:
    """§3.1: an obligation that failed fails the original operation.

    "Succeed now, audit later" is the thing the rule exists to prevent, so this
    is a separate call the caller makes *before* committing, not a promise the
    decision makes on its own. It cannot verify that an audit row was really
    written -- only that the caller claims each obligation was met -- so the
    honest description is that it closes the accidental omission, not a
    dishonest caller.
    """

    outstanding = {item.kind for item in decision.obligations} - set(discharged)
    if outstanding:
        raise AuthorizationError(f"OBLIGATION_NOT_DISCHARGED:{','.join(sorted(outstanding))}")

#: Re-exported so callers get the whole authorization language from one module.
__all__ = [
    "ACTION_VERBS",
    "assignable_action_set",
    "assignable_permissions",
    "AuthContext",
    "Decision",
    "Obligation",
    "ResourceRef",
    "TenantScope",
    "action_is_well_formed",
    "authorize",
    "command_resources",
    "platform_only_actions",
    "platform_resource_ref",
    "require_allowed",
    "require_obligations_discharged",
    "resource_ref_from_wire",
    "service_only_actions",
    "tenant_resource_ref",
]
