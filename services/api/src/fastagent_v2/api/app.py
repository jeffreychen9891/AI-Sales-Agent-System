from __future__ import annotations

import hashlib
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastagent_v2.api.commitments_display import (
    COMMITMENT_LEDGER_AVAILABLE as COMMITMENT_LEDGER_AVAILABLE,
)
from fastagent_v2.api.commitments_display import commitment_display
from fastagent_v2.api.outcome_display import outcome_display
from fastagent_v2.api.responses import openapi_responses
from fastagent_v2.profiles.binding import (
    ChannelBindingDisabled,
    ChannelBindingNotFound,
    ProfileReleaseNotFound,
    repin_channel_binding,
)
from fastagent_v2.profiles.cli import DEFAULT_ROOT as PROFILE_REGISTRY_ROOT
from fastagent_v2.profiles.protocol import (
    ProfileError,
    ProfileImmutabilityError,
    ProfileNotFound,
    ProfileRelease,
)
from fastagent_v2.profiles.publisher import publish_release
from fastagent_v2.profiles.registry import (
    CONTENT_ASSETS_KEY,
    MEMORY_FIELDS_KEY,
    ProfileRegistry,
)
from fastagent_v2.profiles.resolver import ProfileResolver
from fastagent_v2.runtime.auth import (
    AuthContext,
    AuthenticationError,
    AuthorizationError,
    decode_access_token,
    hash_password,
    issue_access_token,
    new_write_only_token,
    sha256_token,
    subject_fingerprint,
    verify_password,
)
from fastagent_v2.runtime.authz import (
    assignable_action_set,
    assignable_permissions,
    authorize,
    platform_resource_ref,
    require_allowed,
    require_obligations_discharged,
    tenant_resource_ref,
)
from fastagent_v2.runtime.contracts import (
    SLOT_UPDATE_REFUSED_ACTION,
    UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
    load_schema,
    validate_contract,
    validate_control_plane_contract,
)
from fastagent_v2.runtime.control_plane import (
    authorized_tenant_from_context,
    authorized_tenant_from_decision,
    command_receipt,
    page,
    resource_ref,
)
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.memory_lifetime import is_expired, ttl_days_by_field
from fastagent_v2.runtime.memory_values import forbidden_value_reason
from fastagent_v2.runtime.redaction import redact
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.runtime.superuser import SuperuserAllowlistError, superuser_identities
from fastagent_v2.runtime.throttle import (
    LOGIN_POLICY,
    PASSWORD_CHANGE_POLICY,
    AttemptThrottle,
    ThrottleExceeded,
    ThrottlePolicy,
    ThrottleUnavailable,
)

# **跨层 import，而它有先例**：本文件已经在用 `worker.target_stack`。
# 策略痕只有一份实现，复用它比在控制面写第二个写点好 ——
# 两份实现意味着两处要跟着 `policy_decisions` 的列走。
from fastagent_v2.worker.policy_output import (
    PolicyOutput,
    audit_and_redact,
    record_policy_decision,
)
from fastagent_v2.worker.target_stack import (
    resume_target_stack_from_human,
    suspend_target_stack_for_human,
)
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

bearer = HTTPBearer(auto_error=False)

#: `Idempotency-Key`, defined once.
#:
#: CP-2's review added the `pattern` to `POST /profiles/releases` with the
#: reason "length was the only field here with no shape, **and the next route
#: will copy it**". The next route copied the version without the shape
#: (independent review 2026-09-04, P3-1) -- and the self-review said the pattern
#: had come across, which it had not. One definition removes the copy.
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9_.:-]{8,128}$"),
]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)


class PublishReleaseRequest(BaseModel):
    """Which already-trusted package to publish. Never the document itself.

    The route publishes **from the registry that ships with the image**, so a
    client cannot post a profile body: `profiles/` is the trusted root, and
    `load_package` refuses anything outside the repository. That is the whole
    difference between this route and the raw-prompt PUT it replaces
    (`C0A-012`) -- the freeze's own wording is "draft->validate->publish, no
    free-prompt back door".
    """

    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


#: The permission that can hand out permissions, and therefore the one a tenant
#: must never be able to lose the last copy of.
#:
#: "保护最后管理员" (freeze B1 #24, `C0A-027`) needs a mechanical definition of
#: "administrator", and this is the narrowest honest one: whoever holds
#: `role.update` can rewrite any role's permissions, including their own. Strip
#: the last role carrying it and the tenant can no longer grant anything to
#: anybody -- a lockout no support call can undo without direct database access.
#:
#: Defining it by capability rather than by a role named "admin" is deliberate:
#: a name is a convention, and the guard has to hold for a tenant that called
#: its administrator role something else.
ROLE_ADMIN_ACTION = "role.update"

#: Namespace for the advisory lock that serialises one tenant's role changes.
#: A named constant rather than two integers written at the call site: two
#: features colliding on the same pair would serialise against each other for a
#: reason nobody could find afterwards.
ROLE_LOCK_NAMESPACE = 0x2701


def _tenant_lock_key(tenant_id: UUID) -> int:
    """A stable 32-bit key for one tenant.

    `pg_advisory_xact_lock(int, int)` takes two 32-bit integers and a tenant is a
    128-bit UUID, so this is lossy. A collision costs two tenants queueing behind
    each other for the length of one role change -- a wait, not a wrong answer --
    which is why a lossy key is acceptable here and would not be if it decided
    *what* gets written.
    """

    return int.from_bytes(tenant_id.bytes[:4], "big", signed=True)


class ProvisionTenantRequest(BaseModel):
    """A tenant is a code and nothing else at creation time.

    **No `id` field.** The caller does not choose it: an id supplied from
    outside is an id somebody can collide with, guess at, or reuse from a tenant
    that was purged. The server mints it, and the response says which.

    **No `status` field either.** `001` defaults it to `active`, and a tenant
    created disabled is a shape nobody asked for -- `POST
    /admin/tenants/{id}/deactivate` (B1 #11) is how a tenant stops.
    """

    model_config = ConfigDict(extra="forbid")

    #: The same pattern `tenants.code` carries in `001`, restated rather than
    #: referenced: a request that fails here answers 422 with the field named,
    #: while one that reaches the database answers 500 on a check violation.
    code: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")


class DeactivateTenantRequest(BaseModel):
    """A reason, and nothing else.

    **The reason is not decoration**: `authorize()` refuses a cross-tenant reach
    without one (`CROSS_TENANT_REASON_REQUIRED`), so this field is what makes the
    request answerable at all. parity §5.2 requires it on every reach; ADR-0068
    makes it an obligation the route has to discharge.

    **No `purge` flag and no `force`.** parity §6.4 forbids one boolean
    switching between deactivate and purge -- they are different actions, routes
    and test sets -- and a field named here is the cheapest way that rule gets
    broken later.

    **A floor of ten characters**, because "x" satisfies "a reason was given"
    while answering nobody. The ceiling keeps an operator's note out of the
    territory where it starts carrying customer content into an append-only
    table.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=10, max_length=280)


class PurgeTenantRequest(BaseModel):
    """A reason, submitted again at purge time.

    **Not inherited from the deactivation** -- §6.4 asks for it here, and the
    two decisions can be ninety days apart. The operator who stopped a tenant is
    not necessarily the one erasing it, and "they stopped paying" is not a
    reason to destroy the record.

    **The same floor and ceiling as `DeactivateTenantRequest`**, and for the
    same two reasons: "x" satisfies "a reason was given" while answering nobody,
    and the ceiling keeps an operator's note out of the territory where it
    starts carrying customer content into an append-only table.

    **No `force`, and no flag that skips a guard.** Every one of the five
    refusals below is a state the caller has to change in the world, not in the
    request body.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=10, max_length=280)


def _change_digest(projection: dict[str, object], state_version: int) -> str:
    """The token `POST .../resume` compares against (ADR-0054 §三.4).

    **`state_version` is inside the hash, and that is the whole design.** The
    ruling calls this a CAS, and a compare-and-set on "the changes I showed you"
    would still let an engine write land between the preview and the
    confirmation -- a customer message that adds no slot, a timer that fires --
    and the operator would be confirming a picture that had already moved. With
    the version in, **any** write to the aggregate invalidates the token, and
    the second phase is a genuine compare-and-set on the row.

    The canonical form is the one `profiles/registry.py` already uses for
    `content_hash`: sorted keys, no spaces. Two callers hashing the same facts
    must not disagree because one of them formatted a float differently.
    """

    payload = json.dumps(
        {"state_version": state_version, "changes": projection},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResumeConversationRequest(BaseModel):
    """The digest the operator is confirming, and nothing else.

    **No `force`, and no way to resume without having looked.** The two-phase
    protocol exists because 「转回时 AI 如何继承人产生的事实」 is a question a
    person has to answer, and a field that skips the preview would answer it by
    default -- which is the shape ADR-0054 §三.4 was written to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    acknowledged_change_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    #: **把这通对话的摘要作废**（裁定八 / 裁定九 取 (c)，T200，2026-09-20）。
    #:
    #: ## 为什么它挂在这条 route 上，而不是新开一条
    #:
    #: 裁定九（作者提议、用户 2026-09-12 确认）取的是
    #: 「**第一版只做作废，不做自由文本编辑**」，理由是那样
    #: **不新增 route、不动 freeze** —— 新增一条就是 K21 四条之外的第五条，
    #: 而那要在 ADR 里单独点名。
    #:
    #: **挂在「交还给 AI」这一刻是有道理的，不是找个地方塞**：
    #: 人接管过、看过 `resume-preview`（那一份现在带着摘要），
    #: 现在说「你接着聊，而你以为这通对话在聊什么那件事是错的」。
    #: **作废 + 下一回合重生成，就是裁定八说的「人能改」的最小实现。**
    #:
    #: ## 它与本模型 docstring 那句「没有 `force`」不冲突
    #:
    #: 那句话禁的是**跳过预览**的字段（「no way to resume without having
    #: looked」）。本字段不跳过任何东西 —— 它要求的恰恰是**看过之后**
    #: 再多做一个动作，而摘要本身就在那份预览里。
    #:
    #: **默认 `False`**：作废是有损的（行不会回到 `active`，`054` 同族的
    #: 触发器那一条），所以它必须是人明确要的那一次，不是顺手的副作用。
    invalidate_summary: bool = False


#: 一个槽位值最多多长 —— **从契约读，不在这里写第三遍**。
#:
#: `conversation-aggregate` 的 `slots` 逐值 `maxLength` 是那个数的唯一定义点
#: （批 2，2026-09-15）；写入侧照它收，两边就不可能漂。
#: **写死一个 400 在这里是第三份副本**，而这一轮已经为「第四份副本」付过一次账
#: （批 1：那份副本藏在守卫自己身上）。
SLOT_VALUE_MAX_LENGTH: int = int(
    load_schema("conversation-aggregate")["properties"]["target_stack"]["items"][
        "properties"
    ]["slots"]["additionalProperties"]["maxLength"]
)


class WriteConversationSlotsRequest(BaseModel):
    """The slots a person is recording, by name.

    **An upsert by name, not a replacement of the frame's slot set.** `PUT` on a
    collection often means "make it look like this", and here that would delete
    everything the understanding step had written -- which 「人工写槽位」 does not
    ask for and no verb in the drawing provides. Naming what you are writing is
    also the only shape that survives two agents working the same conversation.
    """

    model_config = ConfigDict(extra="forbid")

    #: **`max_length=32` 是字典的大小，`SLOT_VALUE_MAX_LENGTH` 才是每个值的长度。**
    #:
    #: 逐值那道界是 2026-09-15（批 2）给 `conversation-aggregate` 的 `slots` 加的，
    #: **而这条写入路径当时没跟着加** —— 于是这里能收下一个 1,000 字的槽位值、
    #: 写进聚合，然后**在读路径上炸**：`api/app.py` 的接管与交回、
    #: `pipeline` 的多处校验都是**读出一行再就地校验**，
    #: 一行不合规 = 那条会话的那条路径 **500**，而不是写入时的 422。
    #:
    #: **独立复核 2026-09-15（批 2 §四）指出那条收紧对已存行是追溯性的**，
    #: 而顺着查下去发现**比那更严重的一半**：不只是存量可能脏，
    #: **写入侧今天就能造出新的脏行**。这一行把两侧对齐。
    slots: dict[str, Annotated[str, Field(max_length=SLOT_VALUE_MAX_LENGTH)]] = Field(
        min_length=1, max_length=32
    )


class FileManualTaskRequest(BaseModel):
    """What a person must say to put work on somebody's list.

    **Owner, SLA and evidence are required, and that is freeze B3 #16 word for
    word** (「owner/SLA/evidence 必填」). Migration 034 leaves all three nullable
    because the engine has no source for any of them (T91) -- a person does:
    they are looking at the conversation, they know who should take it and by
    when, and they can say why. The nullable column and the required field are
    the same ruling read from two sides, not a contradiction.

    **`reason_code`, not prose.** The column is a code column: the engine writes
    `DELIVERY_UNCONFIRMED` and the digest groups by it. Letting a sentence in
    here would make one column hold two vocabularies and quietly break that
    grouping. The sentence belongs in `evidence`, which is what `evidence` is
    for.
    """

    model_config = ConfigDict(extra="forbid")

    owner_employee_id: UUID
    sla_due_at: datetime
    #: A short screaming-snake code, the shape `classify()` already produces.
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    #: Free-form by design (034), bounded so one task cannot carry a document.
    evidence: dict[str, str] = Field(min_length=1, max_length=32)


class ResolveManualTaskRequest(BaseModel):
    """What was done, in the words of whoever did it.

    **Required, and that is a requirement the freeze does not state.** B3 #18 is
    one line ("resolve"), and a resolve that records only a timestamp produces
    exactly the shape T91 registered on the other end: a row whose most important
    fact -- what actually happened -- is nowhere. The note is merged into
    `evidence` under `resolution`, so the account and the task stay one row.

    Flagged in the self-review as an added requirement rather than smuggled in.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    note: str = Field(min_length=8, max_length=500)


class ReassignManualTaskRequest(BaseModel):
    """Who takes it now.

    **`reason` is required for the same reason `POST /employees/{id}/deactivate`
    requires one**: taking work off one named person and putting it on another
    is a fact about two people, and the only record of *why* is this string.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner_employee_id: UUID
    reason: str = Field(min_length=8, max_length=500)


class CancelManualTaskRequest(BaseModel):
    """Why this work is not going to be done.

    Cancelling is the branch freeze B3 #20 chose over `DELETE` -- 「审计事实不物理
    删除」 -- so the row survives and the reason is what makes the survival worth
    anything.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=8, max_length=500)


class WriteManagerContactRequest(BaseModel):
    """Who to tell, when the platform has something a merchant must act on.

    **All three fields, because the row is an address and half an address is
    not one.** `012` made the triple the identity of a delivery target, and the
    digest and the escalation notice both read it whole; a partial update would
    let a caller move the channel while leaving the user id from the old one.

    `channel` is a `Literal` for the reason `WriteControlPolicyRequest` gives:
    the column carries a CHECK with the same values, and a 422 naming the field
    beats a 500 naming a constraint. **`wecom_proxy` is the only value today** --
    it is the only channel this deployment delivers through, and adding one is
    a decision about delivery, not about this route.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["wecom_proxy"]
    #: **200 because the foreign key is what bounds it.** `012` puts no length
    #: CHECK on `config_id`; what it does put there is
    #: `FOREIGN KEY (tenant_id, config_id) REFERENCES channel_bindings`, so an
    #: over-long value fails as "no such binding" rather than as a length. The
    #: route pre-checks that key below so the caller is told which it was.
    config_id: str = Field(min_length=1, max_length=200)
    #: **128, and the number is not arbitrary**: `012` carries
    #: `CHECK (length(external_user_id) BETWEEN 1 AND 128)`. The first version of
    #: this model said 200, which meant a 129-character id passed validation and
    #: died in the database as a 500 claiming `retryable: true` -- while this
    #: model's own docstring says a 422 naming the field beats a 500 naming a
    #: constraint (independent review 2026-09-08, P1-1).
    external_user_id: str = Field(min_length=1, max_length=128)


class RecordContactMemoryRequest(BaseModel):
    """A fact about a customer, the field it goes in, and why we are recording it.

    **`consent` is not in this body, and that is the point of ADR-0131 M1.**
    Freeze B4 #9 lists `consent`, and the word does not mean 「客户同意了」——it
    means 「哪个员工授权记的」. The answer is `context.subject.id`, taken from the
    bearer token, **never from the body**: an employee who could name somebody
    else as the authoriser could record a customer's private fact under a
    colleague's name, and the column exists precisely so that cannot happen.
    Same shape `file_manual_task` gives for `task_class`.

    **`note` is the `evidence` item, and it is required.** The `remember` path's
    evidence is `source_event_id` -- the customer's own words. An employee typing
    into a form has no inbound event, so what they are going on has to be said
    out loud or freeze B4 #9's fifth item has no answer on this path. The bounds
    match `060`'s CHECK, so an over-long note is a 422 naming the field rather
    than a 500 naming a constraint (the lesson `WriteManagerContactRequest`
    already paid for).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    #: **128 because `054` says `length(field_key) BETWEEN 1 AND 128`.** The
    #: profile's own declaration is bounded tighter (64, by the schema), and the
    #: declared-field check is what actually decides -- this bound only keeps an
    #: unbounded string out of the database's face.
    field_key: str = Field(min_length=1, max_length=128)
    #: 1024, `054`'s other CHECK. **Characters, not bytes** -- that table uses
    #: `length()`, and E4-1b already paid once for reading it as bytes.
    field_value: str = Field(min_length=1, max_length=1024)
    note: str = Field(min_length=8, max_length=500)


class ReviseContactMemoryRequest(BaseModel):
    """A correction: the new value, and why.

    **No `field_key`.** Freeze B4 #10 is a revision of *this* memory, and `054`
    carries the composite foreign key
    `(tenant_id, supersedes, contact_id, field_key)` precisely so a revision
    cannot point at another contact's or another field's row. Offering the key
    here would invite a caller to try, and the database would answer with a
    constraint violation instead of the route answering with a sentence.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field_value: str = Field(min_length=1, max_length=1024)
    note: str = Field(min_length=8, max_length=500)


class TombstoneContactMemoryRequest(BaseModel):
    """Why we are forgetting this.

    **A required note on a delete is not decoration.** B4 #11 pairs tombstone
    with 「引用回答可解释」: a reply that already used this memory must stay
    explicable afterwards, and "we stopped believing it on Tuesday, because…" is
    half of that explanation. The row keeps its own note from when it was
    recorded; this one says why it stopped being true, and it lands on the audit
    event rather than the row, because the row is immutable by `054`'s trigger.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    note: str = Field(min_length=8, max_length=500)


class WriteControlPolicyRequest(BaseModel):
    """The whole policy, because a `PUT` on two fields has no other honest shape.

    **The `Literal` rather than a free string**: the column carries a CHECK with
    the same two values, and a 422 naming the field beats a 500 from the
    database naming a constraint. Both are the values
    `conversation_aggregates.control_mode` accepts -- a default the engine could
    not start a conversation in would be a switch that breaks the next inbound
    message rather than one that does nothing.
    """

    model_config = ConfigDict(extra="forbid")

    default_control_mode: Literal["ai", "human"]
    understanding_during_takeover: bool
    #: **`C0B-013`'s other half** (「更新默认并可套用当前会话」), delivered by
    #: B3 #11 under ADR-0079. `"none"` is the default because applying a
    #: policy to conversations already in flight is a second decision, and a
    #: caller who only meant to change the default should not make it by
    #: omission.
    #:
    #: **`"ai"` is accepted by the model and refused by the route**, which is
    #: deliberate: leaving it out of the `Literal` would make a caller who
    #: asked for it read a schema error and guess. ADR-0079 §二 is the answer
    #: they should get instead, and the route says so by name.
    transition_existing: Literal["none", "human", "ai"] = "none"


class SendMessageRequest(BaseModel):
    """坐席要发出去的那句话。

    **只有文字，而这是一次裁定不是一个第一版的偷懒**（ADR-0141 决策 2）：
    用户 2026-09-12 裁定 E3 第一版不发文件，人工发送沿用同一条边界，
    freeze B4 #7（发素材）**不跟着开**。多一个 `kind` 字段会让下一个人
    以为只要填对值就能发图片 —— 而那条路的另一头根本不存在。

    ⚠️ **2000 这个上限是我们的，不是供应商的。** `docs/wecom-proxy-message-api.md`
    里**没有**任何文长限制（实测 grep 无命中），所以这里给的是一个
    「够长到不碍事、短到不至于把一条坏消息送进供应商」的数。
    真实上限要等第一次真实发送撞到它 —— 登记 **T381**。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class MarkConversationReadRequest(BaseModel):
    """The message the reader has read up to.

    **A message id, not a timestamp.** A client that sent its own clock would be
    asserting a position in an ordering it does not own -- and the ordering here
    is `(created_at, id)` precisely because timestamps collide. The route reads
    the time off the row, so the cursor always names a real place in the
    transcript.

    **No `employee_id`**: the cursor is the caller's own, and there is no
    spelling of this request that says otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    last_read_ref: UUID


class CreateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=256)
    permissions: tuple[str, ...] = Field(default=(), max_length=128)


class ReplacePermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: tuple[str, ...] = Field(max_length=128)


class ReplaceEmployeeRolesRequest(BaseModel):
    """The whole set of roles this person is to hold. Not a delta.

    `PUT` with the complete set for `PUT /roles/{id}/permissions`'s reason: a
    partial change to an authorization set has an effect that depends on state
    the client cannot see, and the audit row that records it then cannot be read
    without also finding what the state was.

    An empty array is a legitimate submission -- it is how a person is stripped
    of every role -- so it is not confused with "not submitted": the field is
    required.
    """

    model_config = ConfigDict(extra="forbid")
    role_ids: tuple[UUID, ...] = Field(max_length=64)


class UpdateEmployeeRequest(BaseModel):
    """The three profile fields, and nothing else on the row.

    `extra="forbid"` plus three named optional fields is the whole of parity's
    "宽泛 PUT/partial mass assignment 拆成有限 action": a client cannot reach
    `permissions`, `status`, `credential_version` or `password_hash` through
    this model, and the migration's column-level `GRANT` means it could not
    write them even if it could name them.

    Optional here means "not submitted", not "set to null" -- which
    `model_fields_set` is what distinguishes. Sending `{"department": null}`
    clears the department; not sending the key leaves it alone. A PATCH that
    could not tell those apart would make clearing a field impossible, or make
    every PATCH clear everything the caller did not mention.
    """

    # `str_strip_whitespace` before `min_length=1`: `" "` passes both the field
    # constraint and the column's CHECK, and then shows as blank wherever the
    # control plane names a person -- the same failure K7 made the column
    # required to avoid, only quieter (independent review 2026-09-05, P3-3).
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    employee_no: str | None = Field(default=None, min_length=1, max_length=64)
    department: str | None = Field(default=None, min_length=1, max_length=128)


class UpdateChannelBindingRequest(BaseModel):
    """A binding's non-secret metadata, which today is one field.

    **The field set is what is left after the freeze split `C0A-033`.** That
    inventory row was one endpoint writing `guid/key/secret/active/provider`;
    the freeze cut it into B2 #4 (`key`/`secret`), B2 #5 (`active`), and this
    route (`guid`). `provider` is settled when the binding is created: a
    credential, a callback token and an instance id are all issued by the
    provider they belong to, so moving a live row to another provider is a new
    binding wearing an old row's name, not a metadata edit. `profile_release_id`
    is B2 #14's, and K9's six credential columns do not exist yet (`D18`,
    ADR-0053).

    Optional means "not submitted", exactly as `PATCH /employees/{id}` uses it,
    and `model_fields_set` is what tells the two apart. Sending
    `{"provider_instance_id": null}` clears the column -- which `017` made
    nullable on purpose, because a binding row exists before a guid does -- and
    omitting the key leaves it alone. **Deliberately the same rule as the
    sibling PATCH**: two partial-update routes two files apart with different
    ideas about what an absent key means is how a client learns to send
    everything every time.
    """

    # `str_strip_whitespace` before `min_length=1`, the sibling's reason: `" "`
    # would satisfy both the constraint and `017`'s `length BETWEEN 1 AND 128`
    # CHECK, and then be a guid made of nothing.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    #: `128` is `017`'s CHECK, not a guess. A stricter shape here would reject
    #: values the column accepts and an operator may already have seeded.
    provider_instance_id: str | None = Field(default=None, min_length=1, max_length=128)


class PurgeEmployeeRequest(BaseModel):
    """The reason, submitted a second time, because §6.4 asks for it by name.

    Deactivation takes no body at all. Purge takes one field and it is not
    ceremony: parity §6.4 lists 「再次提交 reason」 among the four things a purge
    must have, alongside eligibility, the absence of a legal hold, and superuser
    scope (**ADR-0056 §四** rules that last one down to a tenant action, because
    `C4` does not exist and an employee is a tenant-internal subject).

    A floor of eight characters rather than one: the reason is the only record of
    *why* an irreversible action was taken, and "x" satisfies a required field
    while answering nothing.
    """

    #: **Do not put the subject's name or contact details in here**
    #: (independent review 2026-09-05, F11). This route destroys an identity and
    #: then writes this string into an append-only table, so a reason that names
    #: the person undoes half the thing it is justifying -- and that row cannot
    #: be deleted afterwards. Say *why*, not *who*: "requested erasure, ticket
    #: 1234" rather than a name. **A request, not a guard** -- registered as
    #: T157, where the options for making it one are written down.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=8, max_length=500)
#: The cookie the refresh token lives in, and the only container
#: `unified-authorization-layer.md:151` allows: 「只以 HttpOnly + Secure +
#: SameSite cookie 或等价安全容器保存」. Read together with `:153` -- the two
#: frontends may not put a refresh token in `localStorage`, `sessionStorage`, a
#: URL or a query cache -- **the server has to set it**, because those are all
#: the places browser JavaScript could put a token handed to it in a body.
def _channel_secret_fingerprint(settings: Settings, tenant_id: UUID, secret_ref: str) -> str:
    """Read the placed credential and return eight hex characters of its digest.

    **Read-only, and that is the whole reason this route can exist** (ADR-0061):
    writing into the secret store is impossible from this container, reading a
    file that is mounted read-only never was.

    Three refusals, and they are different failures on purpose:

    * **the store is not configured or not mounted** -> 503. The deployment is
      incomplete; the caller did nothing wrong and a retry after somebody fixes
      the mount is the right next step.
    * **the named file is not there** -> 422. The caller named something the
      operator has not placed. Registering it anyway would put a `secret_ref` in
      the database pointing at nothing, which is the "looks configured and is
      not" shape this repository keeps paying for.
    * **the path escapes the directory** -> 422, and it should be unreachable:
      the request model's pattern admits no slashes. It is checked because the
      pattern is a claim about a string and this is a claim about a path, and
      the day somebody widens the pattern for a good reason, this still holds.
    """

    if not settings.channel_secret_directory:
        raise HTTPException(status_code=503, detail="CHANNEL_SECRET_STORE_UNAVAILABLE")
    directory = Path(settings.channel_secret_directory).resolve()
    # **The tenant is part of the path, and this function composes it**
    # (independent review F2). The store used to be one flat directory, so a
    # `secret_ref` naming another tenant's file resolved happily inside it: the
    # caller got eight characters of somebody else's credential digest, and this
    # tenant's row was left holding a pointer at it -- a pointer whose entire
    # purpose is "load the credential from here later". Composing the tenant
    # segment rather than accepting it makes that request unexpressible; the
    # containment check below stays as the second line, not the only one.
    candidate = (directory / str(tenant_id) / secret_ref).resolve()
    if not candidate.is_relative_to(directory / str(tenant_id)):
        raise HTTPException(status_code=422, detail="CHANNEL_SECRET_REF_INVALID")
    try:
        material = candidate.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail="CHANNEL_SECRET_NOT_PLACED") from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="CHANNEL_SECRET_STORE_UNAVAILABLE") from exc
    # **Normalised once, and both the emptiness test and the digest use the same
    # bytes** (independent review F5). They disagreed before: emptiness was
    # judged on the stripped material and the digest taken over the raw bytes,
    # so `echo secret > f` and `printf secret > f` -- the same credential to
    # anybody who reads the file the way this repository reads its other secret
    # files -- produced two different fingerprints. `last_verified_at` means
    # "the server checked", and a fingerprint that depends on a trailing newline
    # cannot support that claim.
    normalised = material.strip()
    if not normalised:
        # An empty file is a placed secret in form only. Refusing here keeps
        # `fingerprint` from becoming the digest of nothing, which would compare
        # equal across every binding that made the same mistake.
        raise HTTPException(status_code=422, detail="CHANNEL_SECRET_EMPTY")
    return hashlib.sha256(normalised).hexdigest()[:8]


def _actor_tenant(context: AuthContext) -> str | None:
    """The actor's own tenant, for the audit row of a platform-level action.

    A promoted subject acting on the registry belongs to a tenant of their own,
    and "who did this" is more useful with that than without. `None` when the
    token names more than one, because then there is no single answer and
    inventing one would be worse than admitting it.

    **Two callers, one meaning**: provisioning (B1 #9) and the directory read
    (B1 #8). The first version said "for the provisioning record" and explained
    the `None` branch in terms of reaching the registry -- true of both, but
    written as though there were one caller (independent review P3-5).
    """

    if context.tenant_scope.kind != "one":
        return None
    return str(next(iter(context.tenant_scope.tenant_ids)))
def _effective_permissions(
    connection: object, *, tenant_id: object, employee_id: object
) -> set[str]:
    """One employee's effective permission set: the column union the roles.

    **Asked of the database twice around the write rather than predicted**
    (T161). Whether an assignment takes anything away is a question about the
    union, and `employee_permissions_query` exists so that union has exactly one
    implementation -- rebuilding it in Python from the role ids would be a
    second one, which is the defect B1 #5's independent review found.
    """

    row = connection.execute(  # type: ignore[attr-defined]
        employee_permissions_query(
            columns="e.id",
            where="e.tenant_id = %s AND e.id = %s",
        ),
        (tenant_id, employee_id),
    ).fetchone()
    return set(row["permissions"]) if row is not None else set()


def employee_permissions_query(*, columns: str, where: str) -> str:
    """`SELECT <columns>, <effective permissions> FROM employees e WHERE <where>`.

    **The effective permission set is the column union the roles**, and this
    function exists so that sentence has exactly one implementation. The
    independent review found the second one asking a different question (F3):
    `POST /auth/refresh` read `employees.permissions` alone, so **every
    permission a person held through a role vanished the first time their client
    refreshed** -- silently, and in the direction that removes access.

    **What the union is, and why it is a correlated subquery rather than a
    join** (T92, and independent review 2026-09-03 P3-1 which corrected an
    earlier comment claiming a `LEFT JOIN`): `authorize()` reads
    `context.permissions`, which is minted from this row, so this is where "a
    role grants something" either becomes true or stays the fiction migration
    015 warned about. The column is the tenant's per-person exception, the roles
    are its reusable answer, and `authorize()` has no reason to care which of
    the two a permission arrived by. An employee holding no role keeps exactly
    the column's permissions because `jsonb_array_elements_text` in a `FROM`
    position yields **zero rows** for an empty array rather than a null, and the
    `coalesce` covers a wholly null aggregate.

    `columns` and `where` are **fragments this module writes**, never caller
    input; every value still travels as a parameter.
    """

    return (
        f"SELECT {columns},\n"  # noqa: S608
        """
                       coalesce(
                           (
                               SELECT jsonb_agg(DISTINCT code)
                                 FROM (
                                     SELECT jsonb_array_elements_text(e.permissions) AS code
                                     UNION
                                     SELECT jsonb_array_elements_text(r.permissions) AS code
                                       FROM employee_roles er
                                       JOIN roles r
                                         ON r.tenant_id = er.tenant_id
                                        AND r.id = er.role_id
                                      WHERE er.tenant_id = e.tenant_id
                                        AND er.employee_id = e.id
                                 ) AS granted
                           ),
                           '[]'::jsonb
                       ) AS permissions
                  FROM employees e
        """
        f" WHERE {where}"
    )


REFRESH_COOKIE_NAME = "fastagent_refresh"

#: Scoped to the two routes that may see it. A cookie sent on every request is a
#: refresh token offered to every route, including ones that only need the
#: bearer token, and each of those is another place it can be logged.
REFRESH_COOKIE_PATH = "/auth"


def _set_refresh_cookie(response: Response, *, token: str, ttl_seconds: int) -> None:
    """`HttpOnly` + `Secure` + `SameSite=strict`, all three for their own reason.

    * `HttpOnly` -- script cannot read it, which is what makes `:153` keepable
      rather than a rule the frontend has to remember;
    * `Secure` -- it never travels in clear;
    * `SameSite=strict` -- a refresh endpoint that rotates state on a bare POST
      is a CSRF target, and strict is the answer that does not need a second
      token to be correct. It costs a cross-site link into the app arriving
      logged out, which for a merchant console is not a cost.
    """

    response.set_cookie(
        REFRESH_COOKIE_NAME,
        token,
        max_age=ttl_seconds,
        httponly=True,
        secure=True,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


class RegisterChannelCredentialRequest(BaseModel):
    """Which credential this binding uses -- **not the credential** (ADR-0061).

    parity §5.2 describes `SecretWrite` as carrying the secret itself. It cannot
    here, and ADR-0061 records why in full: K9 refuses a second credential store
    in the database, and the Stage secret directory is not writable from the api
    container (`read_only: true`, uid 10001, the directory is not mounted). The
    operator places the file; this names which one was placed.

    `version` is the deployment's own numbering. The server does not invent it,
    because the server does not know what the operator called it -- and a
    version this table made up would disagree with the one written on the host
    the first time anybody compared them.
    """

    model_config = ConfigDict(extra="forbid")

    #: Matched against the same pattern migration 029 puts on the column. No
    #: slashes, so no traversal is expressible; the route re-checks the resolved
    #: path anyway, because a regex is a claim about the string and the check
    #: below is a claim about the file.
    secret_ref: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    version: int = Field(gt=0)


class RepinBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: UUID
    #: Whether to move conversations that are still open on another release.
    #:
    #: **Default false, and the default is the point.** Rebinding and moving
    #: live conversations are two operations with two idempotency bases
    #: (`binding.py`'s docstring, from the 2026-09-03 review): an operator
    #: rebinds first and decides about the live ones afterwards. A route that
    #: always did both would take that sequence away.
    repin_live_conversations: bool = False


class CreateEmployeeRequest(BaseModel):
    """Who the person is, and the password they will be given. Nothing else.

    **The omissions are the design**, and they are the same ones migration 020
    makes impossible at the database: no `permissions`, no `status`, no
    `credential_version`. `extra="forbid"` means a client cannot even name them,
    and the column-level `GRANT` means the connection could not write them if it
    could. Two independent defences for one property, because the property is
    "a create route cannot mint privilege".

    `display_name` is required for the same reason `PATCH /employees/{id}` made
    it required (ADR-0048 K7): the control plane has to be able to say who
    took over a conversation, and a person with no name shows as a uuid.
    """

    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    #: The same floor as `POST /auth/login` accepts, deliberately: a create
    #: route that allowed a password login would refuse would be creating an
    #: account nobody can use.
    #:
    #: **Nothing normalises it.** The first version of this model carried
    #: `str_strip_whitespace=True` in `model_config`, which in pydantic applies
    #: to *every* `str` field on the model -- `password` included -- while
    #: `LoginRequest` has no such switch and `verify_password` is handed the
    #: login input untouched. An administrator who set `"  pw  "` stored the
    #: hash of `"pw"`, the person they handed it to typed it back exactly and
    #: got a 401 that explains nothing, and **there is no route in the
    #: repository that can change a password** to recover from it (independent
    #: review 2026-09-05, P2-1). Rewriting a credential is a decision, not a
    #: side effect of a switch aimed at three name fields.
    password: str = Field(min_length=12, max_length=1024)
    #: The stripping is **per field**, and only on the three fields it was ever
    #: for: `" "` passes `min_length=1` and the column CHECK and then shows as
    #: blank (B1 #15's independent review, P3-3). Strip first, then measure --
    #: which is what makes `"   "` a 422 rather than a stored blank.
    display_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ]
    employee_no: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
        | None
    ) = None
    department: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
        | None
    ) = None


class ResetEmployeePasswordRequest(BaseModel):
    """The new password, and nothing else at all.

    No `must_change_password` key: the flag is not a choice the caller makes.
    ADR-0048 §二.5 settles it for every reset, and a request that could opt out
    would be a request that can hand somebody a permanent password over a
    channel this system does not control -- which is the sentence the ruling
    exists to prevent.

    **Nothing normalises the password** (B1 #14's independent review, P2-1): a
    model-wide `str_strip_whitespace` rewrote the credential on the way in while
    login left it alone, and the account that produced could not be recovered
    from inside the system.
    """

    model_config = ConfigDict(extra="forbid")
    #: The same floor as login accepts. A reset that set a password login would
    #: refuse creates an account nobody can use, and the way that shows up is a
    #: 401 with no explanation.
    new_password: str = Field(min_length=12, max_length=1024)


class ChangeOwnPasswordRequest(BaseModel):
    """Both passwords. The current one **is** the authorisation for this route.

    Freeze B1 #30 (ADR-0052) does not go through `authorize()`, so there is no
    permission behind this: what proves the caller may replace this credential
    is that they can produce the one being replaced. Dropping
    `current_password` would make a stolen access token sufficient to take an
    account over permanently.
    """

    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=1, max_length=2048)
    topics: tuple[str, ...] = Field(min_length=1, max_length=16)


#: How many rows a list route on this surface will return before it refuses.
#:
#: `page.schema.json` makes `has_more: true` require a string `next_cursor`, so
#: "ship the shape now, add the cursor later" is not a path the contract leaves
#: open: the moment there is a second page, a cursor-less route can only lie.
#: Neither of these lists had a `LIMIT` at all -- `/employees` returned a
#: tenant's whole staff and `/profiles/releases` its entire append-only publish
#: history (independent review 2026-09-04, P2-6).
#:
#: So until there is a real cursor, exceeding the cap is an **error**, not a
#: silent truncation with `has_more: false` on it. A route that refuses is a
#: bug report; a route that quietly drops rows is a wrong answer that looks
#: right, and the caller has no way to tell.
#: The envelope's `code` shape, from `error-envelope.schema.json`.
MACHINE_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Anything pydantic puts in an error `type` that the shape above forbids.
MACHINE_CODE_CLEANUP = re.compile(r"[^A-Z0-9_]+")

#: Which `inbox_events` rows are messages somebody said, and which
#: `outbox_messages` rows are messages the customer received.
#:
#: **One definition, because two consumers now depend on it being the same set**
#: (B3 #5). `GET /conversations/{id}/messages` pages over it, and
#: `POST /conversations/{id}/read` both resolves the marker id against it and
#: counts what is left after the cursor. If these drifted apart, a person could
#: mark read a row the transcript never showed them, or be told they have
#: unread messages that do not exist on the page they are looking at.
#:
#: The two predicates were written for B3 #3 and their reasoning is unchanged:
#: `inbox_events` also carries timers, channel events, business events and
#: delivery receipts -- things that happened, not things anybody said; and
#: `outbox_messages` also carries manager notifications, which the dispatcher
#: delivers to the tenant's manager and never into this conversation.
#: `coalesce` is there because `audience` arrived with that work and rows
#: written before it have no such key.
#:
#: **`CANCELLED` is excluded, and it is the second half of the same rule**
#: (B4-4a, independent review 2026-09-09 P1-1 / P1-2 — found by a probe the
#: reviewer ran against a real stack, not by reading). The audience clause keeps
#: out rows addressed to somebody else; this one keeps out a row addressed to
#: **this** customer that was **deliberately never sent**: B4-4a's
#: `pre_delivery` re-check retires a proactive message whose window closed
#: before the dispatcher reached it, without asking the provider anything.
#:
#: **Why it matters more than it looks**: a takeover operator reads this
#: transcript, sees a line the AI apparently already said, and does not repeat
#: it — while the customer received nothing. `delivery_status` is in the
#: response (`:1650`) so a UI *could* grey it out, but the transcript's job is
#: to be the set of things that were said, not a set that needs a second column
#: to be read correctly.
#:
#: **Only `CANCELLED`, on purpose.** `PENDING` will still be delivered, and
#: `FAILED` / `UNKNOWN` leave a manual task so somebody is already chasing them;
#: whether those three belong on a transcript is an older question this batch
#: does not answer (user's ruling 2026-09-09 — 「不要把三件事摧成一件」).
#:
#: **A consequence worth saying out loud: the outbound transcript is no longer
#: append-only** (B4-4a re-review 2026-09-09, P3-2). `PENDING` is visible, so a
#: proactive message appears on the timeline while it waits and **disappears**
#: if its window closes before the dispatcher reaches it. Usually that is
#: seconds; a lease that has to expire before another dispatcher reclaims the
#: row stretches it to minutes. The user's ruling above settled whether
#: `PENDING`/`FAILED`/`UNKNOWN` belong on a transcript — **it did not settle
#: whether a row may leave one**, which is a property this batch created. **If a
#: disappearing line is unacceptable to the takeover surface, that is a product
#: decision**, and the alternatives are excluding `PENDING` too or keeping
#: `CANCELLED` visible with `delivery_status` as the discriminator.
#: The two tables a transcript row can come from -- K12's `source_table`
#: values, and the only ones a cursor may name.
_MESSAGE_SOURCE_TABLES = frozenset({"inbox_events", "outbox_messages"})

_INBOUND_IS_A_MESSAGE = "event_type = 'customer_message'"
_OUTBOUND_IS_A_MESSAGE = (
    "coalesce(command->>'audience', 'customer') = 'customer' AND status <> 'CANCELLED'"
)

#: The transcript as `(id, created_at)`, which is all the read cursor needs.
#: Built from the two predicates above rather than repeating them, and left open
#: for the caller to append its own `AND ...` -- the two call sites want "is this
#: id in the set" and "what is in the set after this point".
#: **`source_table` is K12's third key component, not decoration**
#: (independent review 2026-09-06, P2-2). ADR-0048 §二.9 says the stable cursor
#: is `(created_at, source_table, id)`; B3 #3 shipped two of the three and this
#: batch inherited the omission while citing the same ruling as authority for
#: its own table. Two parts are enough for *uniqueness* -- uuids are globally
#: unique -- but `AGENTS.md` says an ADR governs until explicitly revised, and
#: that an implementation may not be used to reinterpret the drawing.
#:
#: It also earns its place: a ref that names its own table is resolvable without
#: scanning the union, which is what `038` complained it could not do when it
#: chose `text` for `last_read_ref`.
_VISIBLE_MESSAGE_TEMPLATE = """
    SELECT id, created_at, source_table, ordinal FROM (
        -- **`0 AS ordinal` 是占位，不是数据**（T369 / ADR-0140）：
        -- `inbox_events` 没有这一列，而一条入站事件自己占一个事务 ——
        -- 它没有「这一轮第几条」这回事。
        SELECT id, created_at, 'inbox_events' AS source_table, 0 AS ordinal
          FROM inbox_events
         WHERE tenant_id = %(tenant)s AND conversation_id = %(conversation)s
           AND {inbound}
        UNION ALL
        SELECT id, created_at, 'outbox_messages' AS source_table, ordinal
          FROM outbox_messages
         WHERE tenant_id = %(tenant)s AND conversation_id = %(conversation)s
           AND {outbound}
    ) AS visible
     WHERE true
"""

#: Both substitutions are the constants above -- **fragments this module writes,
#: never caller input**; every value still travels as a parameter. Same shape
#: and same justification as `_employee_query`.
#:
#: A template plus `.format` rather than an f-string, only because a suppression
#: has to sit on the line the checker reports and that line is a `"""` opener.
_VISIBLE_MESSAGE = _VISIBLE_MESSAGE_TEMPLATE.format(  # noqa: S608
    inbound=_INBOUND_IS_A_MESSAGE, outbound=_OUTBOUND_IS_A_MESSAGE
)

#: parity §6.2's first read model, as one statement.
#:
#: **Four lateral joins rather than four round trips.** Each conversation on the
#: page needs its last message, its unread count, its next scheduled event and
#: its open task count; asking per row from Python would be a page of round
#: trips whose cost the caller cannot see. `LEFT JOIN LATERAL` keeps each of
#: them scoped to the one conversation, and `041` adds the two indexes the first
#: of them needs.
#:
#: **The unread count is the caller's**, which is why `%(employee)s` appears in
#: it: the read cursor is per person (B3 #5), so this list is personal even
#: though every other column is not.
#:
#: **The message predicates are the shared constants**, so this list, the
#: message page and the read cursor cannot come to disagree about what counts as
#: a message.
_CONVERSATION_LIST_TEMPLATE = """
SELECT a.id, a.control_mode, a.state_version, a.state,
       c.id AS contact_id, c.config_id AS contact_config_id,
       c.external_user_id AS contact_external_user_id,
       c.profile_data AS contact_profile_data,
       last_message.id AS last_message_id,
       last_message.created_at AS last_message_at,
       last_message.direction AS last_message_direction,
       last_message.payload AS last_message_payload,
       unread.total AS unread_total,
       next_event.kind AS next_event_kind,
       next_event.due_at AS next_event_due_at,
       tasks.total AS open_manual_tasks
  FROM conversation_aggregates a
  LEFT JOIN contacts c
    ON c.tenant_id = a.tenant_id AND c.id = a.contact_id
  LEFT JOIN LATERAL (
      SELECT id, created_at, direction, payload FROM (
          -- **`0 AS ordinal` 是占位，不是数据**（T369，2026-09-19）：
          -- `inbox_events` 没有这一列，而 `UNION ALL` 两侧的列要对齐。
          -- 一条入站事件自己占一个事务，所以它没有「这一轮第几条」这回事。
          SELECT id, created_at, 0 AS ordinal, 'inbound' AS direction,
                 envelope AS payload
            FROM inbox_events
           WHERE tenant_id = a.tenant_id AND conversation_id = a.id
             AND {inbound}
          UNION ALL
          SELECT id, created_at, ordinal, 'outbound' AS direction,
                 command AS payload
            FROM outbox_messages
           WHERE tenant_id = a.tenant_id AND conversation_id = a.id
             AND {outbound}
      ) AS timeline
       -- **`ordinal` 在中间，而它 2026-09-19（T369）之前不在。**
       --
       -- 一轮可以写好几行出站，而一轮写的行 `created_at` **相同**
       -- （它是事务时间戳）。先前这里排 `created_at DESC, id DESC` ——
       -- `id` 是 `uuid4()`，**于是「最后一条」在一轮多行时是抛硬币**：
       -- 会话列表里的预览每次刷新都可能换一条，而运维照它排查
       -- 「我们最后说了什么」时看到的是随机的那一条。
       --
       -- 投递那一侧早就修过同一件事（T109 / 迁移 044，那条领取查询的排序里
       -- 有 `ordinal`，`044` 还为它建了 `(tenant_id, created_at, ordinal, id)`
       -- 索引）。**一处修了、这个读取方没跟上** —— 「写两遍会漂」那一族，
       -- 而本仓 2026-09-19 一天之内在三个读取方上撞到它。
       --
       -- ⚠️ **本文件另外两处仍然没排它，那是有意的**：
       -- `_MESSAGE_PAGE` 与未读计数用的 `(created_at, source_table, id)`
       -- 是 **ADR-0048 §二.9 定的稳定游标**，而 `employee_read_cursors`
       -- 只存那三个分量（迁移 `038`）。往那个序里插一个分量
       -- 等于改游标的定义 —— 要改 ADR、要迁移、而且**已经存下来的游标
       -- 会指到别的位置**。那一半留在 T369 里等裁定，
       -- **而这一处不受游标约束：它只要「最新那一行」。**
       ORDER BY created_at DESC, ordinal DESC, id DESC
       LIMIT 1
  ) AS last_message ON true
  LEFT JOIN LATERAL (
      SELECT count(*) AS total
        FROM inbox_events i
        LEFT JOIN employee_read_cursors r
          ON r.tenant_id = a.tenant_id AND r.conversation_id = a.id
         AND r.employee_id = %(employee)s
       WHERE i.tenant_id = a.tenant_id AND i.conversation_id = a.id
         -- The shared predicate, not a copy of it (independent review
         -- 2026-09-06, P3-1): the header of this statement promises the
         -- list, the message page and the read cursor cannot disagree
         -- about what a message is, and this line said the same thing in
         -- its own words. Same value today; the promise is what broke.
         AND {inbound}
         AND (r.last_read_at IS NULL OR i.created_at > r.last_read_at)
  ) AS unread ON true
  LEFT JOIN LATERAL (
      SELECT kind, due_at FROM scheduled_events
       WHERE tenant_id = a.tenant_id AND conversation_id = a.id
         AND status IN ('PENDING', 'CLAIMED', 'ENQUEUED')
       ORDER BY due_at
       LIMIT 1
  ) AS next_event ON true
  LEFT JOIN LATERAL (
      SELECT count(*) AS total FROM manual_tasks
       WHERE tenant_id = a.tenant_id AND conversation_id = a.id
         AND status IN ('open', 'claimed')
  ) AS tasks ON true
 WHERE a.tenant_id = %(tenant)s
   AND (%(control_mode)s::text IS NULL OR a.control_mode = %(control_mode)s)
   AND (
       %(after)s::timestamptz IS NULL
       OR (coalesce(last_message.created_at, '-infinity'::timestamptz), a.id)
          < (%(after)s::timestamptz, %(after_id)s::uuid)
   )
 -- **`coalesce`, and it is the whole of P1-1** (independent review
 -- 2026-09-06). A row comparison containing a `NULL` evaluates to `NULL`,
 -- which `WHERE` drops: a conversation with no visible message was absent
 -- from **every** cursor page, and `NULLS LAST` meant such a conversation
 -- could only ever appear on one. The whole class was unreachable past page
 -- one, and nothing said so.
 --
 -- `-infinity` sorts after every real timestamp under `DESC`, so the
 -- ordering is the one this list wants and the keyset arithmetic works on
 -- a total order with no holes in it.
 ORDER BY coalesce(last_message.created_at, '-infinity'::timestamptz) DESC, a.id DESC
 LIMIT %(limit)s
"""

#: Same justification as `_MESSAGE_PAGE`: the interpolations are the two module
#: constants above, never caller input, and every value travels as a parameter.
_CONVERSATION_LIST = _CONVERSATION_LIST_TEMPLATE.format(  # noqa: S608
    inbound=_INBOUND_IS_A_MESSAGE, outbound=_OUTBOUND_IS_A_MESSAGE
)

_MESSAGE_PAGE_TEMPLATE = """
WITH timeline AS (
    -- **`0 AS ordinal` 是占位**，理由与 `_VISIBLE_MESSAGE_TEMPLATE` 里那一处
    -- 逐字相同（T369 / ADR-0140）。
    SELECT id, created_at, 'inbound' AS direction,
           'inbox_events' AS source_table, 0 AS ordinal,
           envelope AS payload, NULL::text AS delivery_status
      FROM inbox_events
     WHERE tenant_id = %(tenant)s
       AND conversation_id = %(conversation)s
       AND {inbound}
    UNION ALL
    SELECT id, created_at, 'outbound' AS direction,
           'outbox_messages' AS source_table, ordinal,
           command AS payload, status AS delivery_status
      FROM outbox_messages
     WHERE tenant_id = %(tenant)s
       AND conversation_id = %(conversation)s
       -- **Only what was sent to the customer** (independent
       -- review P1-1). `outbox_messages` also carries manager
       -- notifications -- an escalation the dispatcher delivers
       -- to the tenant's manager, never into this conversation
       -- -- and `command.audience` exists precisely so the
       -- dispatcher can tell them apart. Without this line an
       -- operator reads a transcript containing messages the
       -- customer never received, and B3 #2's takeover is built
       -- on that transcript.
       --
       -- **`coalesce`, because `audience` is not on every row**:
       -- it arrived with the manager-notification work, and rows
       -- written before it have no such key. `= 'customer'`
       -- alone would silently swallow every old message.
       AND {outbound}
)
SELECT id, created_at, direction, source_table, ordinal, payload, delivery_status
  FROM timeline
 -- **`ordinal` 插在 `source_table` 与 `id` 之间**（T369，ADR-0140 修订
 -- ADR-0048 §二.9 的三分量）。一轮写的行 `created_at` 与 `source_table`
 -- 都相同，没有它排序就落到 `uuid4()` 上 —— 于是一轮两条消息在逐字记录里的
 -- 先后是抛硬币的，而人读记录时可能看到兑现排在许诺前面。
 --
 -- ⚠️ **比较式与 `ORDER BY` 必须是同一个序**：只改一边，页边界上会漏行或重行。
 -- 那正是不能「只改展示序」的原因，也是游标要多一个分量的原因。
 WHERE %(after)s::timestamptz IS NULL
    OR (created_at, source_table, ordinal, id)
        > (%(after)s::timestamptz, %(after_source)s::text,
           %(after_ordinal)s::integer, %(after_id)s::uuid)
 ORDER BY created_at, source_table, ordinal, id
 LIMIT %(limit)s
"""

#: The transcript page `GET /conversations/{id}/messages` reads, built from
#: the same two predicates as `_VISIBLE_MESSAGE` so the page and the read
#: cursor cannot come to disagree about which rows are messages.
_MESSAGE_PAGE = _MESSAGE_PAGE_TEMPLATE.format(  # noqa: S608
    inbound=_INBOUND_IS_A_MESSAGE, outbound=_OUTBOUND_IS_A_MESSAGE
)


#: How many conversations one `PUT /control-policy` may hand to people
#: (ADR-0048 K13, kept by ADR-0079). The reason is review, not throughput:
#: 「一次操作的影响面要能在一屏内复核」. A caller with more than this repeats
#: the call and is told how many are left.
#:
#: **It is also the number of aggregate rows one transaction holds locked**
#: (independent review P3-3), and the worker takes the same lock on its way
#: through `process_one`. Raising it means answering how long the engine may
#: be blocked, which is a different question from how much a person can
#: review -- both have to be answered, not just the second.
BULK_TRANSITION_LIMIT = 100

MAX_PAGE_ITEMS = 200


def _page_or_refuse(items: list[Any], *, has_more: bool) -> dict[str, object]:
    """Assemble a `Page<T>` through the shared constructor, or refuse.

    **`page()` rather than a hand-written dict** (independent review round 2,
    R-4): `Page` derives `has_more` from `next_cursor is not None`, so the lie
    P2-6 was about -- claiming there is nothing more when nobody looked -- is
    not expressible here. This was the fourth hand-rolled page dict on this
    surface, and the argument for adopting `CommandReceipt` ("the geology
    everything else is built on; if it is not laid down, every route builds its
    own") applies word for word to the other half of CP-0.

    **409, not 503** (R-5): the refusal is deterministic and permanent -- rows
    do not become fewer -- so "try again later" is the wrong thing to tell a
    client, and it reads to monitoring as an availability incident. The detail
    is a bare code like every other error on this file, rather than the object
    the first version introduced, which would have been a third error shape
    alongside `{"detail": "CODE"}` and the `ErrorEnvelope` still owed by T87.
    """

    if has_more:
        raise HTTPException(status_code=409, detail="PAGE_LIMIT_EXCEEDED")
    return page(items=items).as_dict()


#: How many messages one page carries. Smaller than `MAX_PAGE_ITEMS` on purpose:
#: this is the only paginated read in the repository, and a page of conversation
#: content is heavier per row than a page of role names.
MESSAGE_PAGE_SIZE = 50


def _encode_cursor(
    created_at: datetime, source_table: str, ordinal: int, row_id: UUID
) -> str:
    """`(created_at, source_table, ordinal, id)` as one opaque-ish token.

    ## ⚠️ 2026-09-19（T369 / ADR-0140）：**四个分量，先前是三个**

    一轮写的行 `created_at` 与 `source_table` 都相同，于是排序落到 `id`
    （`uuid4()`）—— 一轮两条消息在逐字记录里的先后是抛硬币的。
    `ordinal` 插在中间。**分页的比较式与 `ORDER BY` 必须是同一个序**，
    所以这个 token 也得多一个分量。

    **三分量的旧 token 一律拒**，而这条规矩是下面那段注释在 2→3 那次就立好的：
    「接受旧形状等于同时背两套排序 —— 而那正是这把键刚被改掉的漂移」。
    游标是不透明且短命的（一个客户端只在一次滚动期间持有它），
    所以拒掉旧 token 的代价是一次 `400`，而不是一次错的分页。

    **All three, because that is the key ADR-0048 K12 specifies** (independent
    review 2026-09-06, P2-2). The first version carried two: the timestamp is not
    a total order -- two messages can share a microsecond, an inbound event and
    an outbound reply being written by different processes -- and the id breaks
    that tie. `source_table` is redundant *for uniqueness*, and that was the
    argument for leaving it out; the ruling is still the ruling, and an
    implementation does not get to reinterpret it.

    It is not merely obedience: with the source in the key, a cursor says which
    table its row came from, so resolving one never needs the union.
    """

    # **base64url, so the token survives a query string.** The first version
    # returned the pair verbatim, and an ISO timestamp carries a `+` -- which a
    # query string decodes as a space, so the second page answered
    # `400 INVALID_CURSOR` and the walk stopped after one page. A test caught it;
    # a client would have met it as "pagination is broken".
    #
    # Opaque as a consequence rather than as a goal: a caller who takes it apart
    # is depending on an ordering key this route is free to change.
    raw = f"{created_at.isoformat()}|{source_table}|{ordinal}|{row_id}".encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str, int, UUID]:
    """The inverse, refusing anything it did not produce.

    A malformed cursor is a client error, not an empty page: answering 200 with
    nothing would look identical to "you have read everything", and a client
    looping on `has_more` would stop early and silently.
    """

    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = urlsafe_b64decode(cursor + padding).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from error
    parts = decoded.split("|")
    # **A two-part token is refused rather than accommodated.** Cursors are
    # opaque and short-lived (a client holds one for the length of a scroll),
    # and accepting the old shape would mean carrying two orderings at once --
    # which is the drift this key was just corrected to remove.
    #
    # **三分量的旧 token 照同一条拒**（T369 / ADR-0140，2026-09-19）：
    # 那段论证一个字不用改，只是这次被拒的是上一版自己。
    if len(parts) != 4 or parts[1] not in _MESSAGE_SOURCE_TABLES:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR")
    timestamp, source_table, ordinal, row_id = parts
    try:
        return (
            datetime.fromisoformat(timestamp),
            source_table,
            int(ordinal),
            UUID(row_id),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from error


def _locked_manual_task(
    connection: Any, *, tenant_id: Any, task_id: UUID
) -> dict[str, Any]:
    """The task, locked for the length of the transaction, or a flat 404.

    **`FOR UPDATE`, because all four verbs read the status and then write it.**
    Two people clicking "claim" on the same unowned task at the same moment
    would otherwise both read `open` and both write themselves in, and the
    second write would win silently -- the first person keeps working a task
    the record says is somebody else's.

    A task belonging to another tenant is not visible under this session (RLS),
    so it produces the same 404 as an id that never existed. That equality is
    what stops the id being an existence oracle.
    """

    row = connection.execute(
        """
        SELECT id, status, owner_employee_id, conversation_id
          FROM manual_tasks
         WHERE tenant_id = %s AND id = %s
         FOR UPDATE
        """,
        (tenant_id, task_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="MANUAL_TASK_NOT_FOUND")
    return dict(row)


def _audit_manual_task_event(
    connection: Any,
    *,
    tenant_id: Any,
    context: AuthContext,
    action: str,
    resource_type: str,
    task_id: UUID,
    reason_code: str,
    evidence: dict[str, Any],
) -> None:
    """One audit row per verb, written inside the same transaction as the move.

    Shared by the four routes because the alternative is four copies of the same
    nine-column insert, and a copy is where the ninth column quietly stops being
    filled. `resource_type` is a parameter rather than a constant: two of the
    four authorize against a command resource (K1), and the audit row names the
    resource that was actually authorized -- otherwise the log and the decision
    disagree about what happened.
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type,
            resource_id, correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            context.subject.id,
            action,
            resource_type,
            task_id,
            context.correlation_id,
            reason_code,
            Jsonb(evidence),
        ),
    )


def _audit_material_event(
    connection: Any,
    *,
    tenant_id: Any,
    context: AuthContext,
    action: str,
    material_id: UUID,
    reason_code: str,
    evidence: dict[str, Any],
) -> None:
    """一条审计行，写在改动它的那个事务里。

    **与 `_audit_manual_task_event` 分开，而不是把 `resource_type` 再参数化一层。**
    那一个是四条待办 route 共用的，它的 `resource_type` 是参数**因为那四条里有两条
    对命令资源授权**（K1）。资料这边只有一条 route 会写审计，资源类型恒为
    `material`——把它做成参数等于开一个没有第二种取值的口子。

    **`evidence` 里放引用不放 id**：审计行要能拿去和 profile 对照，
    而 profile 里写的是 `asset:<slug>:v<N>`。
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type,
            resource_id, correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, 'material', %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            context.subject.id,
            action,
            material_id,
            context.correlation_id,
            reason_code,
            Jsonb(evidence),
        ),
    )


def _audit_memory_event(
    connection: Any,
    *,
    tenant_id: Any,
    context: AuthContext,
    action: str,
    memory_id: UUID,
    reason_code: str,
    evidence: dict[str, Any],
) -> None:
    """A memory's lifecycle move, written in the transaction that made it.

    **Separate from `_audit_material_event` for that function's own stated
    reason, applied in the other direction.** That one refused to parameterise
    `resource_type` because it had exactly one value; so does this one, and the
    two values differ. Merging them would produce a helper whose only remaining
    job is to pass a string through.

    **This helper never copies `field_value` in, and that is a weaker promise
    than the one first written here** (independent review 2026-09-12, P3-1).

    The first version said the evidence "never carries `field_value`", reasoning
    that the audit surface is read by people who may not be allowed to read the
    memory itself, and that copying a fact into a row nothing tombstones would
    quietly undo the tombstone. **The reasoning stands; the promise did not** --
    `tombstone_contact_memory`, twenty lines away, puts the employee's `note`
    in here, and "客户说她妈妈其实戴 17 号不是 18 号" is the natural way to write
    that note. **A threat model broken by its own author in the same batch is
    worse than no threat model**, so the sentence is now what the code does.

    What holds: **this function copies no memory field except `field_key`**, the
    merchant's own column name. What a caller passes in `evidence` is the
    caller's to justify -- and the one caller that passes free text does so
    because B4 #11 asks for the explanation (「引用回答可解释」), which cannot
    live on the row: `054`'s trigger makes the row immutable and the note is
    written after it.

    **The exposure is real but not yet reachable**: no route reads
    `audit_events` today (`060`'s own comment: 「控制面的第一次审计读仍未建」).
    The batch that builds that read is the one that has to decide who may see
    this text -- registered rather than left for it to discover.
    """

    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type,
            resource_id, correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, 'contact_memory', %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            context.subject.id,
            action,
            memory_id,
            context.correlation_id,
            reason_code,
            Jsonb(evidence),
        ),
    )


def _manual_task_receipt(
    *,
    context: AuthContext,
    resource_type: str,
    task_id: UUID,
    idempotency_key: str,
    status: str,
) -> dict[str, object]:
    """`{"receipt": ..., "result": ...}` -- ADR-0050's two-part shape.

    The result is one field, and it is the one a client cannot derive: which
    state the task is in now. A bare receipt would make every caller re-read the
    list to find out whether the move landed.
    """

    return {
        "receipt": command_receipt(
            resource=resource_ref(
                resource_type=resource_type,
                resource_id=str(task_id),
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict(),
        "result": {"task_id": str(task_id), "status": status},
    }


def _manual_task_projection(row: dict[str, Any]) -> dict[str, object]:
    """One task, with the three T91 fields answered rather than implied.

    **`null` here is a fact, not an omission** -- the opposite of ADR-0076's
    absent fields. An engine-filed task genuinely has no owner and no SLA
    (`034` §T91), and saying so is the whole reason the columns exist: "nobody
    is on this" becomes something a client can display and sort by, instead of
    a silence. A person-filed task always has both (B3 #16 requires them), so
    the two producers are told apart by looking at the row.

    `evidence` goes through `redact()` for the reason `_message_projection`
    gives: it is free-form, it carries customer text, and the redactor fails
    closed on what it cannot recognise.
    """

    evidence = redact(row["evidence"] if isinstance(row["evidence"], dict) else {})
    return {
        "task_id": str(row["id"]),
        "status": row["status"],
        "task_class": row["task_class"],
        "notify": row["notify"],
        "reason_code": row["reason_code"],
        "owner_employee_id": (
            str(row["owner_employee_id"]) if row["owner_employee_id"] is not None else None
        ),
        "sla_due_at": _iso_timestamp_or_none(row["sla_due_at"]),
        "expires_at": _iso_timestamp_or_none(row["expires_at"]),
        "created_at": _iso_timestamp(row["created_at"]),
        "claimed_at": _iso_timestamp_or_none(row["claimed_at"]),
        "resolved_at": _iso_timestamp_or_none(row["resolved_at"]),
        "evidence": evidence if isinstance(evidence, dict) else {},
    }


def _frame_to_speak_in(state: object) -> UUID | None:
    """人工消息挂在哪一帧上。

    **取活跃帧；没有活跃帧就取栈顶。** `intent_records.target_frame_id`
    是 NOT NULL，而人工发送本身不推进任何一帧 —— 这个值回答的是
    「这句话发生在哪个上下文里」，不是「它属于哪个计划」。

    栈空时返回 `None`，由调用方拒绝。今天不可达（每条会话出生就带一个
    入口帧），而**把它写成一个显式的分支，好过让 `None` 走到写库那一刻**。
    """

    frames = state.get("target_stack") if isinstance(state, dict) else None
    if not isinstance(frames, list) or not frames:
        return None
    chosen = next(
        (f for f in frames if isinstance(f, dict) and f.get("status") == "active"),
        frames[-1],
    )
    if not isinstance(chosen, dict):
        return None
    raw = chosen.get("id")
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _conversation_summary(row: dict[str, Any]) -> dict[str, object]:
    """One row of parity §6.2's first read model, all nine fields.

    **Absent is never used here; `null` is** -- and the difference is deliberate
    (ADR-0076 §四.2). Every one of these nine has a source now, so "no value"
    always means the same thing: this conversation has no last message yet, no
    contact recorded, no event scheduled. Those are answers. The fields ADR-0076
    left *absent* were the ones nothing could answer at all, and none of them
    are in this list.

    **The last message is a summary, not the message**: direction, time, and the
    text after `redact()`. A list is read at a glance and stored in whatever the
    client caches; the full envelope -- attachments, evidence refs, delivery
    state -- is what `GET /conversations/{id}/messages` is for.
    """

    payload = row["last_message_payload"] if isinstance(row["last_message_payload"], dict) else {}
    redacted = redact(payload)
    content = redacted.get("content") if isinstance(redacted, dict) else None
    state = row["state"] if isinstance(row["state"], dict) else {}
    raw_frames = state.get("target_stack")
    frames = raw_frames if isinstance(raw_frames, list) else []
    active = next(
        (frame for frame in frames if isinstance(frame, dict) and frame.get("status") == "active"),
        None,
    )
    return {
        "conversation_id": str(row["id"]),
        "contact": (
            None
            if row["contact_id"] is None
            else {
                "contact_id": str(row["contact_id"]),
                "config_id": row["contact_config_id"],
                "external_user_id": row["contact_external_user_id"],
                "profile_data": (
                    row["contact_profile_data"]
                    if isinstance(row["contact_profile_data"], dict)
                    else {}
                ),
            }
        ),
        "last_message": (
            None
            if row["last_message_id"] is None
            else {
                "message_id": str(row["last_message_id"]),
                "direction": row["last_message_direction"],
                "created_at": _iso_timestamp(row["last_message_at"]),
                "content": content,
            }
        ),
        "unread_inbound": int(row["unread_total"] or 0),
        "control_mode": row["control_mode"],
        # **The active frame, not the whole stack.** A list wants "what is this
        # conversation trying to do"; the stack itself -- suspended parents,
        # slot evidence, repair history -- is `GET /conversations/{id}`.
        "target": (
            None
            if active is None
            else {
                "frame_id": str(active.get("id")),
                "definition_id": active.get("definition_id"),
                "status": active.get("status"),
            }
        ),
        "next_event": (
            None
            if row["next_event_kind"] is None
            else {
                "kind": row["next_event_kind"],
                "due_at": _iso_timestamp(row["next_event_due_at"]),
            }
        ),
        "open_manual_tasks": int(row["open_manual_tasks"] or 0),
        "projection_version": int(row["state_version"]),
    }


def _contact_projection(row: dict[str, Any]) -> dict[str, object]:
    """One contact, with every field the three routes return.

    **`profile_data` passes through unfiltered on purpose.** `039` puts the
    allowlist in a `CHECK` -- removing `display_name` and `avatar_url` from the
    document must leave nothing -- so a projection that filtered again would be
    a second copy of the rule, and the two would drift the day K11's list grows.

    **`external_user_id` is here rather than behind a second permission**, per
    the ruling recorded in `list_contacts` and registered as T179.
    """

    profile = row["profile_data"] if isinstance(row["profile_data"], dict) else {}
    return {
        "contact_id": str(row["id"]),
        # The two travel together: an external id means something only inside
        # the binding it came from (K11 §2, and `012` says the same of the
        # manager's id).
        "config_id": row["config_id"],
        "external_user_id": row["external_user_id"],
        "profile_data": profile,
        "first_seen_at": _iso_timestamp(row["first_seen_at"]),
        "last_seen_at": _iso_timestamp(row["last_seen_at"]),
        "message_count": int(row["message_count"]),
    }


#: The floor a conversation with no visible message sorts at, in the cursor
#: and in the query alike. `datetime.min` is what `'-infinity'` decodes to on
#: the way back, so the two spellings have to mean the same instant.
_BEFORE_EVERY_MESSAGE = datetime.min.replace(tzinfo=UTC)


def _encode_keyset_cursor(sort_key: datetime, row_id: UUID) -> str:
    """`(sort_key, id)` for a keyset-paginated list.

    **Named for the shape rather than the caller** (independent review P3-3):
    it serves the contact list (`last_seen_at`) and the conversation list
    (the last message's `created_at`). Feeding one list's cursor to the other
    still answers an empty page rather than an error -- registered as the
    reviewer wrote it, not fixed here, because a list identifier inside the
    token is a format change and both lists are days old.

    **Two parts, not three, and the difference is the point**: K12's third part
    is `source_table`, which exists because the message timeline is a union of
    two tables. Neither list here is a union -- `contacts` is one table, and the
    conversation list keys on one column of one row -- so there is nothing to
    discriminate, and `id` breaks ties in the ordering each list uses.
    """

    raw = f"{sort_key.isoformat()}|{row_id}".encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_keyset_cursor(cursor: str) -> tuple[datetime, UUID]:
    """The inverse, refusing anything it did not issue.

    Same reasoning as the message page: an empty page and a bad cursor must not
    look alike, or a client walking `next_cursor` stops early and silently.
    """

    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = urlsafe_b64decode(cursor + padding).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from error
    timestamp, separator, row_id = decoded.partition("|")
    if not separator:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR")
    try:
        return datetime.fromisoformat(timestamp), UUID(row_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from error


def _hand_conversation_to_a_person(
    connection: Connection[dict[str, Any]],
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    actor_id: UUID,
    correlation_id: UUID,
    reason_code: str,
) -> dict[str, int] | None:
    """Take one conversation over, as `POST /conversations/{id}/takeover` does.

    **Extracted so the bulk path cannot become a shortcut** (ADR-0079 §三). The
    batch half of `PUT /control-policy` hands existing conversations to people,
    and the tempting implementation is one `UPDATE ... SET control_mode = 'human'`
    over a hundred rows. That would skip every invariant ADR-0054 and ADR-0058
    put here: the stack would not be suspended, the generation would not move, and
    live timers would keep firing into conversations a person now holds. The way
    to make "reuse the semantics" true is to have one body and two callers.

    **`None` means "not this one", not "failed"** -- a conversation already held
    by a person, or one whose document the engine could not process either. The
    single-conversation route turns those into 409s because the caller named that
    conversation; the bulk caller named none of them, so it skips and counts.

    Returns the counts the caller reports: how many frames were suspended and how
    many pending events were invalidated.
    """

    row = connection.execute(
        """
        SELECT state_version, generation, control_mode, state
          FROM conversation_aggregates
         WHERE tenant_id = %s AND id = %s
         FOR UPDATE
        """,
        (tenant_id, conversation_id),
    ).fetchone()
    if row is None or row["control_mode"] == "human":
        return None
    document = row["state"]
    if not isinstance(document, dict) or "target_stack" not in document:
        return None

    occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    aggregate = suspend_target_stack_for_human(aggregate=document, occurred_at=occurred_at)
    aggregate["control_mode"] = "human"
    aggregate["generation"] = int(row["generation"]) + 1
    aggregate["state_version"] = int(row["state_version"]) + 1
    validate_contract("conversation-aggregate", aggregate)
    suspended = sum(1 for frame in aggregate["target_stack"] if frame["status"] == "suspended")
    connection.execute(
        """
        UPDATE conversation_aggregates
           SET generation = %s,
               state_version = %s,
               target_stack = %s,
               control_mode = %s,
               state = %s,
               updated_at = now()
         WHERE tenant_id = %s AND id = %s AND state_version = %s
        """,
        (
            aggregate["generation"],
            aggregate["state_version"],
            Jsonb(aggregate["target_stack"]),
            aggregate["control_mode"],
            Jsonb(aggregate),
            tenant_id,
            conversation_id,
            int(row["state_version"]),
        ),
    )
    # **不读规则的 `cancel_on`，一律取消**（T358）：人接管之后不再发主动消息，
    # 这一条**不给配包留否决权** —— 那是本仓 Sev0/1 那一族的失败方向。
    invalidated = connection.execute(
        """
        UPDATE scheduled_events
           SET status = 'INVALIDATED',
               invalidated_at = now(),
               invalidation_reason = 'CONVERSATION_TAKEN_OVER'
         WHERE tenant_id = %s AND conversation_id = %s
           AND status IN ('PENDING', 'CLAIMED')
        """,
        (tenant_id, conversation_id),
    ).rowcount
    connection.execute(
        """
        INSERT INTO audit_events(
            tenant_id, id, actor_id, action, resource_type,
            resource_id, correlation_id, reason_code, evidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            uuid4(),
            actor_id,
            "conversation_takeover.create",
            "conversation_takeover",
            conversation_id,
            correlation_id,
            reason_code,
            Jsonb(
                {
                    "generation": aggregate["generation"],
                    "frames_suspended": suspended,
                    "events_invalidated": invalidated,
                }
            ),
        ),
    )
    return {
        "generation": int(aggregate["generation"]),
        "frames_suspended": suspended,
        "events_invalidated": invalidated,
    }


def _control_policy_projection(row: dict[str, Any]) -> dict[str, object]:
    """The two switches, named the way the columns are named.

    Shared by the read, both halves of the write's audit evidence and the
    write's result, so they cannot drift into four spellings of the same policy
    -- the failure `_message_projection` was extracted to avoid.

    **Two of those four were hand-copied literals until the independent review
    pointed at them** (2026-09-06, P3-2): the paragraph above argued against
    hand-copying while `after` and `result` did exactly that, four lines apart.
    They now take the same road as `before`, from the validated command rather
    than from the row -- the value that is about to be written is the value the
    caller sent.
    """

    return {
        "default_control_mode": row["default_control_mode"],
        "understanding_during_takeover": bool(row["understanding_during_takeover"]),
    }


def _release_document_projection(release: ProfileRelease) -> dict[str, object]:
    """商家那半 —— **白名单构造，不是黑名单过滤**（ADR-0143 §二）。

    裁定点名了不给的四项（`policy_bindings`、
    `confidence_gate_policy_ref`、`manipulation_context_refs`、
    `golden_manifest`）。**照着那四项写一个 `del` 会是错的**：
    schema 今天 15 个顶层键，明天长出第 16 个时，
    一个黑名单会**默默地把它发给商家**，而没有任何东西会红。
    这里逐键挑，所以新键的默认去向是「不给」——
    要给它，得有人来改这个函数，那一刻正是该想一想的时刻。

    **裁定没点名的五个**（`strategies`、`outcome_rules`、
    `annotation_taxonomy`、`assist_policy`、`closing_frame_ref`）
    因此也不在这里。它们不是「被拒了」，是**还没裁**——
    登记 T500，由 `test_profile_release_projection.py` 逐键钉住分类。

    `identity` 摊平成四个键：它的字段集合恰好就是这四个，
    也恰好是列表 route 每行给的四个（见 `ProfileReleaseDocument`）。
    """

    document = release.document
    return {
        # 与 `_release_projection` 逐字同名 —— 同一个界面上同一份东西
        # 不该有两种拼法。
        "release_id": str(release.release_id),
        "profile_id": release.profile_id,
        "version": release.version,
        "content_hash": release.content_hash,
        "target_frames": document["target_frames"],
        "proactivity_policy": document["proactivity_policy"],
        # 这两个在 schema 里是**可选**的。缺席时给空值而不是省略键：
        # 一个时有时无的键，会让「这个商家没有素材」与
        # 「这一版的响应漏了素材」在前端变成分不出的两件事。
        "memory_fields": document.get("memory_fields", []),
        "content_material_texts": document.get("content_material_texts", {}),
        "knowledge_bindings": document["knowledge_bindings"],
    }


def _message_projection(row: dict[str, Any]) -> dict[str, object]:
    """One message, recursively redacted (parity §6.2).

    **`redact()` over the whole payload, not a field list.** The envelope is a
    contract with room for provider-specific extras, and a projection that named
    the safe fields would go stale the first time a channel adapter added one.
    The redactor fails closed on anything it cannot recognise, which is the
    behaviour worth having on a path that carries customer text.

    **`revoked` is `False` and that is a fact, not a placeholder.** Message
    revocation (freeze B3 #12) was moved out of M1a by ADR-0048 K15, so no route
    can revoke anything and no message can be in that state. This is the
    opposite of ADR-0076's absent fields: those were unanswerable, this one is
    answerable and the answer is no.
    """

    raw = row["payload"] if isinstance(row["payload"], dict) else {}
    # **Redacted first, then read** (independent review P2-2). The previous
    # version picked four keys and redacted two of them, while both docstrings
    # claimed the whole payload went through -- and a comment that contradicts
    # its code is worse than no comment. Redacting the mapping first makes the
    # sentence true and covers the keys that used to pass through untouched.
    payload = redact(raw)
    payload = payload if isinstance(payload, dict) else {}
    projection: dict[str, object] = {
        "message_id": str(row["id"]),
        "direction": row["direction"],
        "created_at": _iso_timestamp(row["created_at"]),
        # `PENDING / SENDING / DELIVERED / FAILED / UNKNOWN` outbound; inbound
        # messages have no delivery state of their own -- they arrived.
        "delivery_status": row["delivery_status"],
        "revoked": False,
        "content": payload.get("content"),
    }
    # **Absent rather than null, per direction** (independent review P1-2, and
    # ADR-0076 §四.2's rule applied again). An inbound row is a `MessageEnvelope`
    # and carries `actor` and `occurred_at`; an outbound row is a *send command*
    # and carries neither -- `pipeline.py` writes `audience`, `tenant_id`,
    # `conversation_id`, `content`, `deadline_at`, `correlation_id`. Returning
    # `null` for the two would claim we looked and found nothing, when the truth
    # is that an outbound row has no such fact. `direction` says who spoke and
    # `created_at` says when the platform recorded it.
    for optional in ("actor", "occurred_at", "correlation_id"):
        if optional in payload:
            projection[optional] = payload[optional]
    return projection


def _iso_timestamp_or_none(value: datetime | None) -> str | None:
    """`_iso_timestamp` for a column that is allowed to be empty.

    Four routes had written `_iso_timestamp(x) if x is not None else None`
    inline before B3 #2 needed it three times in one projection.
    """

    return _iso_timestamp(value) if value is not None else None


def _slot_projection(frame: dict[str, Any]) -> list[dict[str, object]]:
    """One frame's slots, as provenance rather than content.

    **No values** (ADR-0076, and parity §6.2's own next clause). A slot's value
    is what the customer typed -- a name, an address, a number -- and 「详细敏感
    证据需额外 action」 puts that behind a second action this milestone does not
    have. What a person working the conversation needs from *this* route is
    which slots are filled, how sure the system is, and who put them there.

    `slot_evidence` is optional per the aggregate schema: frames created before
    it existed carry none. A slot with no evidence entry is reported with
    `confidence: null` and `source: null` rather than dropped -- "filled, and we
    cannot say by whom" is a fact about the frame, and omitting it would make an
    old frame look empty.
    """

    slots = frame.get("slots")
    evidence = frame.get("slot_evidence")
    if not isinstance(slots, dict):
        return []
    if not isinstance(evidence, dict):
        evidence = {}
    projection: list[dict[str, object]] = []
    for name in sorted(slots):
        entry = evidence.get(name)
        entry = entry if isinstance(entry, dict) else {}
        projection.append(
            {
                "name": name,
                "confidence": entry.get("confidence"),
                # `human` / `model`, or `None` on a frame written before
                # ADR-0054 §三.2 added provenance.
                "source": entry.get("source"),
                "recorded_at": entry.get("recorded_at"),
                # A reference, not content: it names the message a reader can
                # fetch through `GET /conversations/{id}/messages`, where the
                # recursive redaction §6.2 requires already applies.
                #
                # **D3（2026-09-15）：聚合侧现在存数组**（`evidence_event_ids`），
                # 而**这一格保持单数并给出最新的那一条** —— 客户端在读它，
                # 加键不删键。存量行仍是单数键，两种形状都读。
                # **要看全部几条**是另一件事：那要新开一个键，
                # 而今天没有消费方要它（本仓判据 2：先有消费方再产数据）。
                "evidence_event_id": _latest_slot_evidence(entry),
            }
        )
    return projection


def _latest_slot_evidence(entry: dict[str, object]) -> object:
    """这个槽位最新的那一条证据 id，**两种形状都读**（D3）。

    新写回产出 `evidence_event_ids`（数组，最新的在末尾）；
    D3 之前写下的行是单数 `evidence_event_id`，而**它们不追溯改写**。
    """

    ids = entry.get("evidence_event_ids")
    if isinstance(ids, list) and ids:
        return ids[-1]
    return entry.get("evidence_event_id")


def _frame_projection(frame: object) -> dict[str, object]:
    """One `TargetFrameInstance`, summarised (`conversation-engine.md` §3.2)."""

    if not isinstance(frame, dict):
        return {}
    return {
        "frame_id": frame.get("id"),
        "definition_id": frame.get("definition_id"),
        "goal": frame.get("goal"),
        "status": frame.get("status"),
        "entered_at": frame.get("entered_at"),
        "last_progress_at": frame.get("last_progress_at"),
        "parent_frame_id": frame.get("parent_frame_id"),
        "interruption_reason": frame.get("interruption_reason"),
        "resume_confirmation_policy": frame.get("resume_confirmation_policy"),
        "frame_generation": frame.get("frame_generation"),
        "slots": _slot_projection(frame),
    }


def _takeover_projection(
    frames: list[Any], exit_signal: object, commitment_refs: list[Any]
) -> dict[str, object]:
    """ADR-0054's takeover-period projection -- the three items the document holds.

    §6.2's revision annotation lists five. The other two -- refused slot updates
    and the cost-gate interruption -- are rows in `audit_events` and
    `policy_decisions`, and `_takeover_governance_projection` reads them since
    migration 042. **Both routes that render this projection merge those two in
    themselves**, and that is deliberate: this function also feeds
    `_change_digest`, the token `POST .../resume` compares to decide whether the
    operator saw the current picture. A digest over rows another service writes
    would go stale without the conversation document changing at all, turning a
    concurrency check into a source of 409s nobody can act on.

    So the split is not "the preview shows less" -- `resume-preview` returns all
    five items too, because it and the detail page rendering 「接管期变化投影」
    differently is exactly the drift `conversation_resume_preview` warns about.
    The split is that the **digest** covers what the aggregate holds.

    **(1) Slots written during the takeover, split by who wrote them.** The split
    is the whole point: ADR-0054 §三.2 makes a human-written slot something the
    model may not revise, so "which of these did a person put there" is the
    question a returning agent has to answer before trusting the frame.

    **(3) `pending_exit_signal`, and its empty state is a sentence.** Empty means
    the customer has not said they are leaving -- which is a finding, not an
    absence, and §6.2 says so in as many words.

    **(4) Commitments registered during the takeover.** Always zero in M1a
    because the ledger does not exist, and ADR-0054 requires it be *shown* as
    zero: an omitted field reads as "the system remembered".
    """

    written: dict[str, list[dict[str, object]]] = {"human": [], "model": [], "unattributed": []}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        for slot in _slot_projection(frame):
            source = slot.get("source")
            bucket = source if source in ("human", "model") else "unattributed"
            written[str(bucket)].append({"frame_id": frame.get("id"), **slot})
    return {
        "slots_written": written,
        "pending_exit_signal": (
            exit_signal
            if isinstance(exit_signal, dict)
            else {"present": False, "note": "the customer has not signalled an exit"}
        ),
        "commitments_registered": commitment_display(commitment_refs),
    }


#: How many refused slot updates the detail projection carries.
#:
#: A cap rather than a page: the field is a summary inside a bigger response,
#: and a conversation whose model has been refused two hundred times has a
#: problem the twenty most recent refusals already describe. The **total** is
#: returned next to the list, so "there are more" is a number the caller reads
#: rather than a fact the shape hides -- B3 #11 shipped a boolean wearing a
#: count's name and an independent review had to find it.
REFUSED_SLOT_UPDATE_PAGE = 20

#: The audit action `_hand_conversation_to_a_person` writes, and therefore the
#: row whose `created_at` **is** the moment the current takeover began.
#:
#: **The first version of this file said no such column existed** (independent
#: review 2026-09-06, P1-1). It does, in the table 042 had just opened, readable
#: with the four columns 042 itself grants -- so "we cannot bound the period"
#: was not a trade-off, it was a mistake, and it had been written into the
#: docstring, the self-review and ADR-0081 alike.
TAKEOVER_AUDIT_ACTION = "conversation_takeover.create"

#: The refusal fields this route republishes, by name.
#:
#: **A whitelist, not the stored blob.** `target_stack.py` writes refusals
#: without slot *values* on purpose -- "it is customer content" is its own
#: comment -- and this projection could simply forward `evidence` and inherit
#: that promise. It does not: `evidence` is a `jsonb` written by one service
#: and read by another, and the day a writer adds a value to it is the day it
#: reaches a client through here. The reader protects the boundary it owns.
#:
#: **`recorded_confidence` is in the list because the writer has two shapes**
#: (independent review P2-1). The confidence refusal carries the number it was
#: compared against, and that rule *is* the comparison: `proposed 0.31` without
#: `recorded 0.97` cannot be read as either a sane refusal or a customer
#: correction that should have gone through. The first list was written against
#: one shape and silently dropped the other's only distinguishing field.
_REFUSAL_FIELDS = (
    "frame_id",
    "slot",
    "reason",
    "proposed_confidence",
    "recorded_confidence",
    "proposed_evidence_event_id",
    "recorded_evidence_event_id",
)


def _refusal_projection(evidence: object) -> list[dict[str, object]]:
    refusals = evidence.get("refusals") if isinstance(evidence, dict) else None
    if not isinstance(refusals, list):
        return []
    return [
        {field: refusal[field] for field in _REFUSAL_FIELDS if field in refusal}
        for refusal in refusals
        if isinstance(refusal, dict)
    ]


def _takeover_period(
    connection: Any, *, tenant_id: UUID, conversation_id: UUID, control_mode: object
) -> dict[str, object]:
    """When the conversation's current takeover began -- three answers, not two.

    **`takeover`**: a person took it over, and the most recent
    `conversation_takeover.create` audit row says when. Most recent rather than
    first: a conversation can be handed over, resumed and handed over again, and
    「接管期」 means the one being looked at.

    **`conversation_start`**: the tenant's policy puts new conversations in
    `human` (migration 037, ADR-0077), so there is a person holding it and no
    takeover row will ever exist. The period is the whole conversation, and
    `started_at` is `None` because no column records when that was --
    `conversation_aggregates` has `updated_at` and no birth timestamp.

    **`none`**: nobody has ever held this conversation. The takeover-period
    projection is then empty *because there was no period*, which is a different
    statement from "nothing happened during it" and is said as such.
    """

    row = connection.execute(
        """
        SELECT max(created_at) AS started_at
          FROM audit_events
         WHERE tenant_id = %s AND resource_id = %s AND action = %s
        """,
        (tenant_id, conversation_id, TAKEOVER_AUDIT_ACTION),
    ).fetchone()
    started_at = row["started_at"] if row is not None else None
    if started_at is not None:
        return {
            "source": "takeover",
            "started_at": _iso_timestamp(started_at),
            "_bound": started_at,
        }
    if control_mode == "human":
        return {
            "source": "conversation_start",
            "started_at": None,
            "_bound": None,
            "note": "born under a policy that hands new conversations to people",
        }
    return {"source": "none", "started_at": None, "_bound": None}


def _takeover_governance_projection(
    connection: Any, *, tenant_id: UUID, conversation_id: UUID, control_mode: object
) -> dict[str, object]:
    """ADR-0054 §6.2's items (2) and (5) -- the two that needed a grant.

    ADR-0076 shipped this route without them and said why: they live in
    `audit_events` and `policy_decisions`, and the API role could read neither,
    so returning `null` would have claimed we looked. Migration 042 grants the
    columns these queries name and nothing else (user ruling 2026-09-06,
    「发窄 SELECT」; ADR-0081).

    **Bounded by the takeover period** (independent review 2026-09-06, P1-1).
    The first version read every refusal the conversation had ever collected,
    so a conversation *nobody had ever taken over* reported refusals under a key
    called 「接管期变化投影」. `_takeover_period` finds the lower bound; when
    there is no period at all, both items are empty and say which kind of empty.

    **The two refusal kinds are counted apart** (same review). RFC 4.3 refuses a
    slot update for two unrelated reasons: `SLOT_WRITTEN_BY_HUMAN` is ADR-0054
    §三.2 -- the model trying to revise what a person wrote -- while
    `LOW_CONFIDENCE_OVER_HIGH` is an ordinary-turn confidence rule that has
    nothing to do with takeover. A single `total` cannot carry the §三.2
    conclusion the ADR read off it, so the count is grouped by reason code and
    the conclusion is drawn from the group that means it.

    **Now that we can look, the empty states are stated rather than omitted** --
    the same rule ADR-0076 §四.2 states, not a reversal of it. Before the grant,
    absence meant "not looked at"; after it, `total: 0` inside a real period
    means "looked, nothing there", which is a finding.

    **Counted with `GROUP BY`, listed with `LIMIT`.** The total is the real
    number of refusals, not "twenty or more".
    """

    period = _takeover_period(
        connection,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        control_mode=control_mode,
    )
    bound = period.pop("_bound")
    if period["source"] == "none":
        return {
            "takeover_period": period,
            "refused_slot_updates": {
                "total": 0,
                "by_reason_code": {},
                "shown": 0,
                "items": [],
                "note": "this conversation has never been held by a person",
            },
            "understanding_halted_by_cost_gate": {
                "halted": False,
                "occurrences": 0,
                "last_at": None,
                "note": "this conversation has never been held by a person",
            },
        }

    # **The bound is a parameter, not a fragment.** A conversation born in
    # `human` mode has no start row, and the two ways to express "then do not
    # bound it" are appending to the SQL text or passing `NULL` and saying so
    # in the predicate. The second keeps the statement a constant -- which is
    # what `ruff`'s S608 is about -- at the cost of binding the value twice.
    # The cast is required: psycopg cannot infer a type for a bare `NULL`
    # parameter, and an earlier draft of this batch learned that as a 500.
    grouped = connection.execute(
        """
        SELECT reason_code, count(*) AS occurrences
          FROM audit_events
         WHERE tenant_id = %s AND resource_id = %s AND action = %s
           AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)
         GROUP BY reason_code
        """,
        (tenant_id, conversation_id, SLOT_UPDATE_REFUSED_ACTION, bound, bound),
    ).fetchall()
    by_reason_code = {str(row["reason_code"]): int(row["occurrences"]) for row in grouped}
    total = sum(by_reason_code.values())
    refusals = connection.execute(
        """
        SELECT reason_code, evidence, created_at
          FROM audit_events
         WHERE tenant_id = %s AND resource_id = %s AND action = %s
           AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)
         ORDER BY created_at DESC
         LIMIT %s
        """,
        (
            tenant_id,
            conversation_id,
            SLOT_UPDATE_REFUSED_ACTION,
            bound,
            bound,
            REFUSED_SLOT_UPDATE_PAGE,
        ),
    ).fetchall()
    # **`id` is not in the grant, so it is not in the ORDER BY.** Two refusal
    # rows can only share a `created_at` if one transaction wrote both, and the
    # writer writes at most one row per transition -- so `created_at DESC` is a
    # total order in practice. Ties would cost order within one microsecond,
    # which is not worth a column that would let this role correlate audit rows
    # across resources.
    halted = connection.execute(
        """
        SELECT count(*) AS occurrences, max(created_at) AS last_at
          FROM policy_decisions
         WHERE tenant_id = %s AND conversation_id = %s AND reason_code = %s
           AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)
        """,
        (
            tenant_id,
            conversation_id,
            UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL,
            bound,
            bound,
        ),
    ).fetchone()
    occurrences = int(halted["occurrences"]) if halted is not None else 0
    human_written = by_reason_code.get("HUMAN_WRITTEN_SLOT_UPDATE_REFUSED", 0)
    return {
        "takeover_period": period,
        "refused_slot_updates": {
            "total": total,
            # **The group is what carries the meaning** (independent review
            # P1-1). `HUMAN_WRITTEN_SLOT_UPDATE_REFUSED` is the model trying to
            # revise a person's slot; `LOWER_CONFIDENCE_SLOT_UPDATE_REFUSED` is
            # the ordinary confidence rule and can happen on any turn. Reading
            # "the model stayed inside ADR-0054 §三.2" off their sum was wrong
            # in both directions.
            "by_reason_code": by_reason_code,
            "shown": len(refusals),
            "items": [
                {
                    "reason_code": row["reason_code"],
                    "at": _iso_timestamp(row["created_at"]),
                    "refusals": _refusal_projection(row["evidence"]),
                }
                for row in refusals
            ],
            "note": (
                "no slot update has been refused during this takeover"
                if total == 0
                else f"the {len(refusals)} most recent of {total};"
                f" {human_written} of them refused a person's slot"
            ),
        },
        "understanding_halted_by_cost_gate": {
            "halted": occurrences > 0,
            "occurrences": occurrences,
            "last_at": (
                _iso_timestamp(halted["last_at"])
                if halted is not None and halted["last_at"] is not None
                else None
            ),
            # **`last_at: null` is honest here and would not have been before.**
            # `halted: false` next to it says the lookup happened; a bare `null`
            # with no counter is the shape ADR-0076 §四.2 refused.
            "note": (
                "the cost ceiling has not interrupted understanding during this takeover"
                if occurrences == 0
                else "the understanding step was stopped by a cost ceiling"
            ),
        },
    }


def _role_projection(row: object) -> dict[str, object]:
    """One shape for a role, used by all three routes that return one.

    A fixed key set, and the `permissions` array is part of it: a role *is* its
    permission set, so hiding it would leave an administrator unable to tell two
    roles apart before assigning one -- the opposite of the employee list, where
    the permission array is deliberately withheld.
    """

    return {
        "role_id": str(row["id"]),  # type: ignore[index]
        "name": row["name"],  # type: ignore[index]
        "description": row["description"],  # type: ignore[index]
        "permissions": list(row["permissions"]),  # type: ignore[index]
        "is_system": bool(row["is_system"]),  # type: ignore[index]
        "created_at": _iso_timestamp(row["created_at"]),  # type: ignore[index]
        "updated_at": _iso_timestamp(row["updated_at"]),  # type: ignore[index]
    }


def _iso_timestamp(value: datetime) -> str:
    """Normalise a stored timestamp to the offset form the rest of this file emits.

    The first draft of this helper appended a `Z` and claimed in its own docstring
    that this was "the shape every other timestamp on this surface uses" -- which
    was false: every other timestamp here is a plain `.isoformat()`, so `+00:00`.
    A single field in a divergent format is exactly the kind of thing a client
    parses wrong once and then hard-codes around, and a comment asserting the
    opposite of the code next to it is worse than no comment.

    What is worth keeping is `astimezone`: a column read back without it can be
    naive, and a timestamp with no offset is not a point in time.
    """

    return value.astimezone(UTC).isoformat()



#: Statuses whose failure a caller may sensibly repeat with the same request.
#:
#: `retryable` is a field the caller *acts* on, so guessing it is worse than
#: omitting it would have been. The rule here is deliberately narrow: a 5xx is
#: the server's problem and may pass, and a 429 says "later" in so many words.
#: Everything else in 4xx is a statement about the request, and repeating it
#: unchanged produces the same answer -- `control-plane-parity.md` 5.2 is
#: explicit that pages must not branch on the message, which only works if the
#: machine fields mean what they say.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _message_for(code: str) -> str:
    """A human rendering of the code -- never a second vocabulary.

    The envelope requires a `message`, and parity 5.2 forbids pages from
    branching on it. A lookup table would therefore be a second place to
    maintain, drifting from the codes it describes, for a field nobody is
    allowed to key on. So the message is *derived*: `ROLE_NOT_FOUND` reads
    "role not found". It cannot go stale, and it cannot quietly become the
    thing clients parse.
    """

    return code.replace("_", " ").strip().lower() or "request failed"


def _envelope(
    *,
    code: str,
    request: Request,
    status_code: int,
    field_errors: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """The one place an error body is built (T81/T87, `ErrorEnvelope`).

    Before this, every route answered with FastAPI's default `{"detail": ...}`
    while `error-envelope.schema.json` and parity 5.2 defined
    `{code, message, request_id, field_errors, retryable}`. CP-0 landed the
    shared contract precisely so that each route would not invent its own
    shape; the receipt half was wired up and this half was not, and the route
    count kept climbing (T87's expiry is "before B1's write routes are laid
    down" -- 30 of the 40 remaining routes are writes).

    Two properties the schema enforces and this function must not undermine:
    `additionalProperties: false` and no `stack`/`detail` member. There is
    nowhere for an internal trace to land, which is the point -- so the
    unhandled-exception handler below reports a code and nothing else.
    """

    body = {
        "code": code,
        "message": _message_for(code),
        "request_id": str(getattr(request.state, "request_id", "")) or str(uuid4()),
        "field_errors": field_errors or [],
        "retryable": status_code in RETRYABLE_STATUSES,
    }
    validate_control_plane_contract("error-envelope", body)
    # **The header is set here, not only in the middleware.** An unhandled
    # exception escapes `call_next`, so the middleware's line assigning the
    # header never runs -- the response a caller sees for a 500 is built by
    # Starlette's outer error middleware from this function. The id in the body
    # was right; the header was missing on exactly the responses where somebody
    # is most likely to quote one (independent review 2026-09-05, P2-1).
    # **`headers` exists for `retry-after`, and it had to.** A `429` telling a
    # caller to wait without saying how long is a `429` every client turns into
    # its own guess. `HTTPException(headers=...)` does not survive this handler
    # on its own -- the response a caller receives is built here, from scratch,
    # so anything the raise site attached is dropped unless it is passed
    # through. That was true of every header any route has ever attached to an
    # error, and the throttle is the first one to need it.
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"x-request-id": str(body["request_id"]), **(headers or {})},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env("api")
    database = Database(resolved.database_url)
    # The Redis ADR-0001 §2 set aside as the "cache/rate-limit" one, used for the
    # first time here. Constructed eagerly like `Database`: a limiter built
    # lazily inside the first request would make its first failure a request
    # failure rather than a startup one.
    throttle = AttemptThrottle(resolved.cache_redis_url)
    # **Resolved once here so a malformed allowlist stops the process, not the
    # traffic** (independent review F4). The argument is the one three lines
    # above, applied to a second file: a list parsed lazily inside the first
    # request makes its first failure a request failure -- and because the
    # reader sits inside `decode_access_token`, that failure was *every*
    # authenticated request answering 500, not just the superuser paths. The
    # return value is deliberately discarded: the list is re-read per request
    # so that a revocation lands (F3), and this call exists for its exception.
    superuser_identities(resolved.superuser_allowlist_file)
    app = FastAPI(title="FastAgent v2 API", docs_url=None, redoc_url=None)

    def _authenticate(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> tuple[AuthContext, bool]:
        """Everything both dependencies share, plus the flag they differ on.

        Returns the context and whether this subject owes a password change.
        **The two callers below are the only place that flag is acted on**, so
        that "while `must_change_password` is true, everything answers 403"
        (ADR-0048 §二.5) is one statement rather than a rule each route has to
        remember -- and so that a route added next year inherits it without
        knowing it exists.
        """

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        try:
            context = decode_access_token(settings=resolved, raw_token=credentials.credentials)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED") from exc
        except SuperuserAllowlistError as exc:
            # **503, not 500, and not 401** (independent review F4). The
            # allowlist was readable when the process started -- `create_app`
            # made sure of it -- so reaching here means it stopped being
            # readable afterwards. That is not "your credentials are bad"
            # (401 would send a valid user to re-authenticate forever) and it
            # is not an unlabelled crash: it is a dependency of this request
            # being unreachable, the same shape as `THROTTLE_UNAVAILABLE`.
            raise HTTPException(
                status_code=503, detail="SUPERUSER_ALLOWLIST_UNAVAILABLE"
            ) from exc
        if context.tenant_scope.kind != "one" or context.session_id is None:
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            current = connection.execute(
                """
                SELECT e.status, e.credential_version, e.must_change_password,
                       s.revoked_at,
                       s.expires_at <= clock_timestamp() AS session_expired
                  FROM employees e
                  JOIN employee_sessions s
                    ON s.tenant_id = e.tenant_id AND s.employee_id = e.id
                 WHERE e.tenant_id = %s AND e.id = %s AND s.id = %s
                """,
                (tenant_id, context.subject.id, context.session_id),
            ).fetchone()
        if (
            current is None
            or current["status"] != "active"
            or int(current["credential_version"]) != context.subject.credential_version
            or current["revoked_at"] is not None
            # Decided by the database, because the database now writes it. The
            # comparison this replaced was against `datetime.now(UTC)`, which
            # was self-consistent only while the write was on that clock too.
            # `clock_timestamp()` rather than `now()`: `now()` is the
            # transaction's start, which would keep a session alive for the
            # transaction's own duration past its expiry -- small, but on the
            # permissive side, and nothing here needs to spend it.
            #
            # **The column is not selected any more, only the answer**
            # (independent review F4): leaving `s.expires_at` in the row after
            # nothing read it would invite the next reader to compare it here by
            # hand, on this process's clock, which is the thing being removed.
            or bool(current["session_expired"])
        ):
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        return context, bool(current["must_change_password"])

    def authenticated(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> AuthContext:
        """The dependency every route uses, and the one that enforces K8.

        **403, not 401.** The token is valid and the caller is who they say they
        are; what is missing is not authentication but a step they owe. A 401
        would tell every client to throw the token away and log in again, which
        is the one thing that cannot help here -- logging in again produces
        another token in the same state.

        The code is `PASSWORD_CHANGE_REQUIRED`, and it is the *only* way a
        caller discovers this: nothing else on the surface says so, including
        `POST /auth/login`, which answers 200 and hands out a token that works
        for exactly one route. Leaving login alone is deliberate -- it is a
        merged route and this batch already touches two others -- but the cost
        is real and belongs in the record rather than in a docstring nobody
        reads: **the error envelope is the discovery mechanism.**
        """

        context, must_change_password = _authenticate(credentials)
        if must_change_password:
            raise HTTPException(status_code=403, detail="PASSWORD_CHANGE_REQUIRED")
        return context

    def authenticated_pending_password_change(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> AuthContext:
        """The same checks **minus** the one `POST /auth/change-password` exists to end.

        There is exactly one legitimate user of this dependency, and
        `tests/contracts/test_forced_password_change.py` asserts that by reading
        this file: a second route reaching for it would be a route that works
        while its caller is locked out, which is the whole guarantee undone.

        A path-sniffing exemption inside `authenticated` was the alternative and
        is worse: it puts the list of exempt routes in a string comparison,
        where adding to it is invisible in a route's own diff.
        """

        context, _ = _authenticate(credentials)
        return context

    def _record_password_failure(
        *, scope: str, identity: str, policy: ThrottlePolicy
    ) -> None:
        """Counting a failure can fail, and it must not become a 500.

        **P3-1.** `_guard_password_attempts` wrapped `guard()` and nothing
        wrapped `record_failure`, so one fault -- Redis unreachable -- had two
        answers: 503 `THROTTLE_UNAVAILABLE` on the way in, and a generic 500 on
        the way out. The window used to be narrow (Redis had to die between the
        two calls); **switching this instance to `noeviction` widens it**, since
        a full Redis now refuses writes while still answering reads. So the
        write side gets the same answer as the read side.
        """

        try:
            throttle.record_failure(scope=scope, identity=identity, policy=policy)
        except ThrottleUnavailable as unavailable:
            raise HTTPException(
                status_code=503, detail="THROTTLE_UNAVAILABLE"
            ) from unavailable

    def _guard_password_attempts(
        *, scope: str, identity: str, policy: ThrottlePolicy
    ) -> None:
        """Consult the limiter, and turn its two failures into two answers.

        **429 for too many attempts, 503 when the limiter cannot be reached.**
        They are different facts and a caller acts on them differently: the
        first is "wait", the second is "this is broken, and it is not you".
        Both are in `RETRYABLE_STATUSES`, so the envelope says `retryable: true`
        for each -- which is true of both.

        The 503 is the fail-closed half, and it is the price of the choice
        `ThrottleUnavailable` documents: **while the cache Redis is unreachable
        nobody can log in.** Registered as T132 rather than left as a surprise.
        """

        try:
            throttle.guard(scope=scope, identity=identity, policy=policy)
        except ThrottleExceeded as exceeded:
            raise HTTPException(
                status_code=429,
                detail="TOO_MANY_PASSWORD_ATTEMPTS",
                headers={"retry-after": str(exceeded.retry_after_seconds)},
            ) from exceeded
        except ThrottleUnavailable as unavailable:
            raise HTTPException(
                status_code=503, detail="THROTTLE_UNAVAILABLE"
            ) from unavailable

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/auth/login",
        # **响应形状进 OpenAPI，只做文档**（ADR-0142 决策 1）。
        # 不是 `response_model` —— 那个会过滤掉模型里没有的键，
        # 于是一个写漏了字段的模型会**安静地把那个字段删掉**。
        # 这里运行期行为一字不改；形状对不对由
        # `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/auth/login"),
    )
    def login(command: LoginRequest, request: Request, response: Response) -> dict[str, object]:
        """**T117's first missing call site**, closed here rather than deferred again.

        `_audit_session_event` was written by B1 #18 with the rule stated in one
        place -- *every transition in a session's life is a row* -- and wired to
        one of its five call sites. Its remaining expiry was "B1 #17 落地时", and
        this is that batch, so the two authentication routes are wired here
        rather than registered for a third time. Password reset and
        `POST /auth/change-password` both end sessions in bulk; a table that
        recorded those and not the ordinary login and logout would make the
        record look complete while answering "when did this person last sign
        in?" with nothing.

        **`correlation_id` comes from the request id**, not from an
        `AuthContext`: there is no context yet -- creating one is what this
        route does -- and the request id is the value the caller already has in
        their `x-request-id` header, which makes the row joinable to the call
        that wrote it.
        """

        # **Before the tenant is even resolved.** The key is what the caller
        # claims to be, which is all that is known at this point and all the
        # limit needs: an attacker guessing passwords for `a@example.com` is
        # counted whether or not that address exists. Keying on something only
        # discoverable after a successful lookup would leave the "does this
        # account exist" question unlimited.
        attempt_identity = f"{command.tenant_code}:{str(command.email).lower()}"
        _guard_password_attempts(
            scope="login", identity=attempt_identity, policy=LOGIN_POLICY
        )
        with database.transaction(tenant_id=None) as connection:
            tenant = connection.execute(
                "SELECT id, status FROM resolve_login_tenant(%s)", (command.tenant_code,)
            ).fetchone()
        if tenant is None or tenant["status"] != "active":
            _record_password_failure(
                scope="login", identity=attempt_identity, policy=LOGIN_POLICY
            )
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        tenant_id = UUID(str(tenant["id"]))
        with database.transaction(tenant_id=tenant_id) as connection:
            # The concatenated fragment is a module constant with no
            # interpolation; the values still travel as parameters.
            employee = connection.execute(
                employee_permissions_query(
                    columns=(
                        # `must_change_password` joined the list for **T130**: the
                        # login response now says so, instead of leaving the 403 on
                        # the next request as the only way to find out.
                        "e.id, e.password_hash, e.status, e.credential_version, "
                        "e.must_change_password"
                    ),
                    where="e.tenant_id = %s AND lower(e.email) = lower(%s)",
                ),
                (tenant_id, str(command.email)),
            ).fetchone()
            if (
                employee is None
                or employee["status"] != "active"
                or not verify_password(str(employee["password_hash"]), command.password)
            ):
                # Counted for all three reasons, and the three stay
                # indistinguishable on the wire: an unknown address, a disabled
                # account and a wrong password answer the same 401 and cost the
                # same attempt. A limit that only counted wrong passwords would
                # leave account enumeration unlimited.
                _record_password_failure(
                    scope="login", identity=attempt_identity, policy=LOGIN_POLICY
                )
                raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
            session_id = uuid4()
            # **One timestamp for both rows, and it is the database's.**
            #
            # The session and its refresh family end at the same moment by
            # construction rather than at two values milliseconds apart. That
            # part is unchanged; `now()` is the transaction timestamp, so both
            # INSERTs below take the identical instant -- exactly equal now,
            # where before they were equal because one Python value was passed
            # twice.
            #
            # What changed is *whose* clock. This was
            # `datetime.now(UTC) + ttl`, a process-clock instant, while three of
            # the four readers of `employee_sessions.expires_at` compare it
            # against the database's `now()` (the ws-ticket claim, the session
            # list, and the takeover query) and only the HTTP auth check used
            # the process clock. **One column written on one clock and read on
            # two: at most one of those readers could be right.**
            #
            # The database clock is the one that stays a common reference when a
            # second API replica exists -- the argument T183 made for the
            # scheduler's lease. Today the offset is small and points the safe
            # way (T186 measured the database 0.43s ahead, so the SQL readers
            # expire a session marginally early), which is why this is a
            # consistency fix and not an incident.
            session_ttl_seconds = resolved.refresh_token_ttl_seconds
            connection.execute(
                """
                INSERT INTO employee_sessions(
                    tenant_id, id, employee_id, credential_version, expires_at
                ) VALUES (%s, %s, %s, %s, now() + %s * interval '1 second')
                """,
                (
                    tenant_id,
                    session_id,
                    employee["id"],
                    employee["credential_version"],
                    # **The session row lives as long as the refresh family, not
                    # as long as one access token** (independent review
                    # 2026-09-05, F1). It used to be `now + access_token_ttl`,
                    # and `_authenticate` refuses a request whose session row has
                    # expired -- so fifteen minutes after login the refresh route
                    # was **returning 200 with a token nothing would accept**.
                    # Every case in this batch ran inside that window and saw
                    # nothing.
                    #
                    # The two clocks are now the same one: a session and its
                    # family both start here and both end fourteen days later.
                    # The access token is still fifteen minutes -- that is the
                    # JWT's own `exp`, and it is what limits a stolen bearer
                    # token. What lengthens is the row that **logout,
                    # deactivation and a role change revoke**, so those
                    # revocations matter more than before, not less.
                    session_ttl_seconds,
                ),
            )
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=UUID(str(employee["id"])),
                subject_employee_id=UUID(str(employee["id"])),
                action="session.create",
                correlation_id=UUID(str(request.state.request_id)),
                reason_code="EMPLOYEE_LOGGED_IN",
                # A count and an id, like every other row this helper writes.
                # Not the email, not the tenant code, and certainly not the
                # password that was just verified.
                evidence={"session_id": str(session_id)},
            )
            # **The first member of a new family** (`:151`). Login is the only
            # place a family starts; every later token is a rotation of this one,
            # which is what makes "this token belongs to a chain nobody else has
            # used" a question the refresh route can ask.
            refresh_plaintext = new_write_only_token()
            connection.execute(
                """
                INSERT INTO refresh_tokens(
                    tenant_id, id, family_id, employee_id, session_id, token_hash,
                    credential_version, rotation_counter, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, 0,
                    now() + %s * interval '1 second'
                )
                """,
                (
                    tenant_id,
                    uuid4(),
                    uuid4(),
                    employee["id"],
                    session_id,
                    sha256_token(refresh_plaintext),
                    employee["credential_version"],
                    session_ttl_seconds,
                ),
            )
            permissions = frozenset(str(value) for value in employee["permissions"])
            token = issue_access_token(
                settings=resolved,
                employee_id=UUID(str(employee["id"])),
                tenant_id=tenant_id,
                permissions=permissions,
                credential_version=int(employee["credential_version"]),
                session_id=session_id,
            )
        # The right password ends the window. Anything else would count a
        # person's own successful login against them.
        throttle.clear(scope="login", identity=attempt_identity)
        response.headers["Cache-Control"] = "no-store, private"
        # **The refresh token leaves in a cookie, never in the body.** Putting it
        # in the JSON would hand it to script, and `:153` forbids script from
        # having anywhere to put it -- so the only shape that satisfies both
        # clauses is one the browser stores and JavaScript cannot read.
        _set_refresh_cookie(
            response,
            token=refresh_plaintext,
            ttl_seconds=resolved.refresh_token_ttl_seconds,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": resolved.access_token_ttl_seconds,
            # **T130, and the field is here rather than on `/auth/refresh`.**
            #
            # Before this, somebody an administrator had reset got a 200 and a
            # token that works on exactly one route, and **nothing in the body
            # said so**. The only way to find out was the 403
            # `PASSWORD_CHANGE_REQUIRED` that the next request -- any request --
            # came back with. A client either wrote a "bump into it and go back"
            # branch or fired a throwaway request to probe. Both are code that
            # exists only because this key was missing.
            #
            # **`/auth/refresh` deliberately does not carry it** (ADR-0142).
            # A reset bumps `credential_version`, and `:2574` treats a moved
            # version as a dead session -- so the person whose password was
            # reset has no refresh token left to present. Adding the key there
            # would publish a fact that is `false` on every path that can reach
            # it, which is the misleading-true-value shape this repository has
            # already ruled on once (the commitments `0`).
            "must_change_password": bool(employee["must_change_password"]),
        }


    @app.middleware("http")
    async def _assign_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Every request gets an id, whether or not it ends in an error.

        The envelope requires `request_id`, and an id minted inside the error
        handler would be useless for the thing it exists for. So it is assigned
        on the way **in**, before anything can fail, and every response that
        passes back through this middleware carries it.

        **Not every response passes back through it.** An unhandled exception
        escapes `call_next`, and the response is then built by Starlette's outer
        error middleware -- outside this one. `_envelope()` therefore sets the
        header itself, which is what makes the claim true for 500s as well
        (independent review 2026-09-05, P2-1). An earlier version of this
        docstring said "every response" and offered timeouts and proxy 502s as
        the examples; those are cases where the application produces no response
        at all, so no header of ours exists to be read.
        """

        request.state.request_id = uuid4()
        response = await call_next(request)
        response.headers["x-request-id"] = str(request.state.request_id)
        return response

    @app.exception_handler(StarletteHTTPException)
    def _http_error(request: Request, exc: Exception) -> JSONResponse:
        """Every `HTTPException` in this app already raises a machine code.

        `detail="ROLE_NOT_FOUND"` and its 23 siblings were written as tokens
        from the start, so adopting the envelope is a change of *shape*, not of
        vocabulary: the same code that used to sit in `detail` is now `code`.

        A detail that is not a machine token (FastAPI's own 404/405 for an
        unrouted path, for instance) would break the schema's
        `^[A-Z][A-Z0-9_]*$`, so it is mapped by status instead of passed
        through. That is also what stops a future `detail=f"...{value}..."`
        from putting request content into an error body.
        """

        assert isinstance(exc, StarletteHTTPException)
        raw = exc.detail
        field_errors: list[dict[str, str]] = []
        if isinstance(raw, dict):
            # The structured form: a code plus the fields it is about. Kept to
            # exactly these two members so that "detail" cannot become a place
            # to attach arbitrary internals -- the envelope's
            # `additionalProperties: false` would reject them anyway, and
            # failing at the raise site is easier to read than failing at the
            # wire.
            code = str(raw.get("code", ""))
            # `.get()` rather than `[...]`: a raise site that forgot a key
            # would otherwise raise `KeyError` **inside the handler**, and the
            # caller would receive an opaque 500 -- the comment below used to
            # claim failing at the raise site is easier to read than failing at
            # the wire, while doing the second (independent review 2026-09-05,
            # P3-1). A malformed entry is dropped; the code still travels.
            field_errors = [
                {"field": str(item["field"]), "code": str(item["code"])}
                for item in raw.get("field_errors", [])
                if isinstance(item, dict) and item.get("field") and item.get("code")
            ]
        else:
            code = raw if isinstance(raw, str) else ""
        if not MACHINE_CODE.fullmatch(code):
            code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(
                exc.status_code, "REQUEST_FAILED"
            )
        return _envelope(
            code=code,
            request=request,
            status_code=exc.status_code,
            field_errors=field_errors,
            # Only the headers a raise site chose to attach. `x-request-id` is
            # added by `_envelope` itself and is not overridable from here.
            headers={
                str(name): str(value)
                for name, value in (getattr(exc, "headers", None) or {}).items()
                if str(name).lower() != "x-request-id"
            },
        )

    @app.exception_handler(RequestValidationError)
    def _validation_error(request: Request, exc: Exception) -> JSONResponse:
        """422 is the one status whose envelope has something to carry.

        `field_errors` exists so a form can mark the fields that failed without
        parsing prose. The mapping keeps the field path and pydantic's error
        type, uppercased into the schema's token shape -- and **nothing else**:
        pydantic's `msg` and `input` echo the submitted value, which is how a
        password or a token would end up in an error body.
        """

        assert isinstance(exc, RequestValidationError)
        field_errors = []
        for error in exc.errors():
            location = [str(part) for part in error.get("loc", ()) if part != "body"]
            token = str(error.get("type", "invalid")).upper()
            token = MACHINE_CODE_CLEANUP.sub("_", token).strip("_") or "INVALID"
            field_errors.append({"field": ".".join(location) or "body", "code": token})
        return _envelope(
            code="VALIDATION_FAILED",
            request=request,
            status_code=422,
            field_errors=field_errors,
        )

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001 - FastAPI 异常处理器的签名由框架定
        """A bug becomes a 500 envelope with a code and nothing else.

        FastAPI's default for an unhandled exception is to re-raise, which in a
        deployed process means the server's own 500 page and, depending on the
        stack, a traceback. The envelope schema has no member a trace could
        land in (`additionalProperties: false`), and this handler is what makes
        that guarantee true of the running application rather than only of the
        schema file.

        The exception itself is not swallowed silently -- it reaches the
        logger through Starlette's normal path -- but nothing about it crosses
        the wire except `retryable: true`, which is honest: the caller did
        nothing wrong and repeating may work.
        """

        return _envelope(code="INTERNAL_ERROR", request=request, status_code=500)

    def _revoke_refresh_family(
        connection: object, *, tenant_id: UUID, family_id: UUID, reason: str
    ) -> None:
        """Kill every live token in one chain.

        Called from the two places `:151` and `:152` name -- a replayed token and
        a credential that moved. It never deletes: a deleted row is a replay this
        table can no longer recognise, which is the one thing it is for.
        """

        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE refresh_tokens
               SET revoked_at = now(), revoke_reason = %s
             WHERE tenant_id = %s AND family_id = %s AND revoked_at IS NULL
            """,
            (reason, tenant_id, family_id),
        )

    @app.post(
        "/auth/refresh",
        # **响应形状进 OpenAPI，只做文档**（ADR-0142 决策 1）。
        # 不是 `response_model` —— 那个会过滤掉模型里没有的键，
        # 于是一个写漏了字段的模型会**安静地把那个字段删掉**。
        # 这里运行期行为一字不改；形状对不对由
        # `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/auth/refresh"),
    )
    def refresh_session(
        request: Request,
        response: Response,
    ) -> dict[str, object]:
        """Route freeze B1 #5 (`C0A-002`): 一次性 rotation + reuse detection.

        **No `Authorization` header, by design.** The access token this route
        exists to replace is, in the case that matters, already expired --
        requiring a live one would make the route useless exactly when it is
        needed. The credential here is the cookie, and it is the only one.

        **Every use rotates** (`unified-authorization-layer.md:151`). The
        presented token is stamped `used_at`, a new row joins the same family,
        and the caller gets a new cookie. So a token is a one-shot: the copy an
        attacker stole stops working the moment the real client refreshes.

        **Reuse revokes the family, not the token.** A token that arrives with
        `used_at` already set means two parties hold the same chain, and there is
        no way to tell from here which one is the thief -- the honest client may
        well be the one that just refreshed. So the whole family dies and both
        parties have to log in again. Annoying for one of them, and the only
        answer that does not leave the attacker holding a working chain.

        **A credential change ends the family too** (`:152`). The row carries the
        `credential_version` it was issued under; a password reset or a
        deactivation moves the employee's, and the mismatch is caught here rather
        than relying on every one of those routes to remember this table.

        **Every failure answers the same 401 with the same code.** "Which of
        expired / revoked / reused / unknown" is exactly the question an attacker
        with a stolen cookie wants answered.
        """

        presented = request.cookies.get(REFRESH_COOKIE_NAME)
        if not presented:
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        presented_hash = sha256_token(presented)
        # **Tenant-less lookup, on purpose.** The caller has no bearer token, so
        # there is no `AuthContext` and no tenant to scope by yet -- the token
        # itself is what names the tenant. `token_hash` is unique across the
        # table, so this cannot match another tenant's row by accident, and the
        # row's own `tenant_id` is what every statement below is scoped to.
        with database.transaction(tenant_id=None) as connection:
            # **Through the `SECURITY DEFINER` resolver, not a plain SELECT.**
            # The policy on `refresh_tokens` keys on `app.tenant_id`, and there
            # is no tenant to set yet -- the token is what names one -- so a
            # direct read would see zero rows and every refresh would 401.
            # `001` answered the identical shape for ingress with
            # `resolve_channel_binding`; migration 027 follows it, fixed column
            # list and all.
            row = connection.execute(
                # `token_expired` computed here for the same reason as the
                # session check above: the column is on the database's clock, so
                # the comparison is too. Wrapping the definer in a SELECT keeps
                # migration 027's signature untouched.
                "SELECT t.*, t.expires_at <= clock_timestamp() AS token_expired"
                " FROM resolve_refresh_token(%s) AS t",
                (presented_hash,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        tenant_id = UUID(str(row["tenant_id"]))
        family_id = UUID(str(row["family_id"]))
        session_id = UUID(str(row["session_id"]))

        def _refuse_and_revoke(reason: str, *, end_session: bool = False) -> HTTPException:
            """Revoke the family in its **own** transaction, then refuse.

            **Raising inside the transaction would undo the revocation** -- the
            context manager rolls back on an exception, so the family this route
            just decided to kill would come back alive with the 401. The tests
            caught it: the replay answered 401 while the *other* token in the
            family kept working, which is the one outcome this rule exists to
            prevent.
            """

            with database.transaction(tenant_id=tenant_id) as revoking:
                _revoke_refresh_family(
                    revoking, tenant_id=tenant_id, family_id=family_id, reason=reason
                )
                if end_session:
                    # **F6.** The docstring promised "both parties have to log in
                    # again", and revoking the family did not deliver it: the
                    # access token minted moments earlier stays valid for its
                    # full fifteen minutes. On a *known theft* that is the one
                    # path where the session should end immediately -- logout
                    # already does it, and logout is the benign case.
                    revoking.execute(
                        """
                        UPDATE employee_sessions
                           SET revoked_at = now()
                         WHERE tenant_id = %s AND id = %s AND revoked_at IS NULL
                        """,
                        (tenant_id, session_id),
                    )
            return HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")

        if row["used_at"] is not None:
            # **The reuse case, on the snapshot.** The claim below catches the
            # racing half; this catches the ordinary replay without paying for a
            # write.
            raise _refuse_and_revoke("reuse-detected", end_session=True)
        if row["revoked_at"] is not None or bool(row["token_expired"]):
            # Already dead or simply old. **Not reuse** -- nothing to revoke, and
            # calling it reuse would let an expired cookie kill a family that a
            # newer login may have started.
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        with database.transaction(tenant_id=tenant_id) as connection:
            employee = connection.execute(
                employee_permissions_query(
                    columns="e.id, e.status, e.credential_version",
                    where="e.tenant_id = %s AND e.id = %s",
                ),
                (tenant_id, row["employee_id"]),
            ).fetchone()
            # **F5: the session has to still be alive.** This route used to read
            # only `refresh_tokens` and `employees`, so a session revoked by
            # `PUT /employees/{id}/roles` -- which revokes sessions *deliberately
            # without* touching `credential_version` -- left this chain working
            # for a fortnight. Read in the same transaction as the claim below.
            #
            # **`clock_timestamp()`, because this one is a gate** (independent
            # review F4). The rule the batch settled on for the readers of
            # `employee_sessions.expires_at`: a predicate that decides whether to
            # *admit* a caller reads `clock_timestamp()`, so a session cannot
            # outlive its expiry by the duration of the transaction asking; a
            # predicate that only picks the rows a statement is about to *stamp*
            # stays on `now()`, so the predicate and the stamp name one instant.
            session = connection.execute(
                """
                SELECT id FROM employee_sessions
                 WHERE tenant_id = %s AND id = %s
                   AND revoked_at IS NULL AND expires_at > clock_timestamp()
                """,
                (tenant_id, session_id),
            ).fetchone()
            if session is None:
                raise _refuse_and_revoke("session-ended")
            if (
                employee is None
                or employee["status"] != "active"
                or int(employee["credential_version"]) != int(row["credential_version"])
            ):
                # `:152`. The family cannot outlive the credential it was issued
                # under, and this is where that is enforced rather than in every
                # route that changes one. Same reason as above for doing it in a
                # separate transaction.
                raise _refuse_and_revoke("credential-changed")
            # **F2: the claim is the check.** The snapshot above came from a
            # transaction that has already closed, so two concurrent refreshes
            # both saw `used_at IS NULL` and both rotated -- one family, two live
            # tokens, and reuse detected never. Making the write conditional and
            # reading its row count moves the decision into the database, where
            # the two racers are serialised.
            claimed = connection.execute(
                """
                UPDATE refresh_tokens
                   SET used_at = now(), revoked_at = now(), revoke_reason = 'rotated'
                 WHERE tenant_id = %s AND id = %s AND used_at IS NULL
                RETURNING id
                """,
                (tenant_id, row["id"]),
            ).fetchone()
            if claimed is None:
                # Somebody else claimed it between the snapshot and here. That is
                # the racing half of reuse, and it gets the same answer.
                raise _refuse_and_revoke("reuse-detected", end_session=True)
            rotated_plaintext = new_write_only_token()
            connection.execute(
                """
                INSERT INTO refresh_tokens(
                    tenant_id, id, family_id, employee_id, session_id, token_hash,
                    credential_version, rotation_counter, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    row["family_id"],
                    row["employee_id"],
                    row["session_id"],
                    sha256_token(rotated_plaintext),
                    employee["credential_version"],
                    int(row["rotation_counter"]) + 1,
                    # **The family's life is not extended by using it.** A
                    # rotation that reset the clock would make a chain that is
                    # refreshed often immortal, which is the opposite of what an
                    # expiry is for.
                    row["expires_at"],
                ),
            )
            permissions = frozenset(str(value) for value in employee["permissions"])
            access = issue_access_token(
                settings=resolved,
                employee_id=UUID(str(employee["id"])),
                tenant_id=tenant_id,
                permissions=permissions,
                credential_version=int(employee["credential_version"]),
                session_id=UUID(str(row["session_id"])),
            )
        response.headers["Cache-Control"] = "no-store, private"
        _set_refresh_cookie(
            response,
            token=rotated_plaintext,
            ttl_seconds=resolved.refresh_token_ttl_seconds,
        )
        return {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": resolved.access_token_ttl_seconds,
        }

    @app.post("/auth/logout", status_code=204)
    def logout(
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> Response:
        """Route freeze B1 #6: an explicit way to end **your own** session.

        The freeze calls this one "platform new" -- the old system had no such
        entry, and its absence is why the sibling routes that must revoke a
        session (deactivate, password reset) had nowhere to point. It is the
        first user of `session.delete`, which is the action ADR-0043 renamed
        from `session.revoke` precisely because `revoke` is not one of §3.2's
        primitives and would have answered 403 to everybody the day somebody
        built this.

        **It revokes the session the caller is holding, and no other.** There is
        no body and no session id on the wire: the only session this route can
        name is `context.session_id`, which came out of the presented token. A
        route that accepted an id would need to answer "may this subject revoke
        *that* session", which is a different question (an administrator ending
        somebody else's session is B1 #18's, and it is not this route).

        **204 rather than a receipt**: parity 5.2 reserves `CommandReceipt` for
        commands whose execution outlives the request -- money, sending, purge,
        bulk state. This one is finished when it returns.

        Calling it twice is not an error the second time -- it is a **401**: the
        token stops authenticating as soon as the row is revoked, so the second
        call never reaches this function. The test that pins this says so, since
        "idempotent" would be the wrong word for it.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="session.delete",
                resource=tenant_resource_ref(resource_type="session", tenant_id=tenant_id),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # `revoked_at IS NULL` keeps the first revocation's timestamp: when
            # a session was ended is an audit fact, and a second write must not
            # move it.
            #
            # **It is unreachable through this route**, because the dependency
            # rejects a revoked token and a second call never arrives here. A
            # probe deleting this guard left the suite green, which is how the
            # first attempt at "testing" it was found to be running its own SQL
            # rather than this statement.
            #
            # **The expiry has since fired, and it is now tested.** B1 #18
            # (deactivate) was the batch that arrived first, and it revokes
            # sessions it did not issue -- so the second write is reachable
            # through *that* route. `test_a_session_the_target_already_ended_is_
            # not_re_revoked` logs in twice, logs one session out, deactivates,
            # and asserts the ended session's `revoked_at` did not move and that
            # only one session was counted. Left here rather than moved, because
            # this is the statement being defended; the test naming this comment
            # lives with the route that can reach it.
            connection.execute(
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND id = %s AND employee_id = %s
                   AND revoked_at IS NULL
                """,
                (tenant_id, context.session_id, context.subject.id),
            )
            # **Logging out ends the refresh family as well** (user ruling,
            # 2026-09-05, on B1 #5's self-review §八.1).
            #
            # **This route did not need to know about refresh tokens until
            # today, and that is exactly the hazard.** Before B1 #5 there was
            # nothing to revoke but the session row, so "log out" and "the
            # session is over" were the same statement. Adding a credential that
            # outlives the access token silently made them different: without
            # this, a person who logged out on a shared machine left a cookie
            # behind that still mints access tokens for a fortnight.
            #
            # Keyed on `session_id` rather than the family: the caller's token
            # names a session, and every family member carries the session it
            # was issued for. `revoked_at IS NULL` for the same reason as above
            # -- a second write must not move when it happened.
            connection.execute(
                """
                UPDATE refresh_tokens
                   SET revoked_at = now(), revoke_reason = 'logout'
                 WHERE tenant_id = %s AND session_id = %s AND revoked_at IS NULL
                """,
                (tenant_id, context.session_id),
            )
            # T117's second missing call site. Here the actor and the subject
            # **are** the same person, which is the contrast that makes the
            # reset and deactivate rows worth reading: on those two they differ.
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=context.subject.id,
                action="session.delete",
                correlation_id=context.correlation_id,
                reason_code="EMPLOYEE_LOGGED_OUT",
                evidence={"session_id": str(context.session_id)},
            )
        # The header goes on the returned object, and **only** there: when a
        # handler returns a `Response`, FastAPI uses that object -- headers set
        # on an injected `response` are not merged in. The first version set it
        # in both places, and the second one did nothing (independent review
        # 2026-09-05, P3-1). Worth removing rather than leaving as harmless:
        # B1 #17 and #18 will copy this route.
        logged_out = Response(
            status_code=204, headers={"cache-control": "no-store, private"}
        )
        # **Clearing the cookie is hygiene, not the mechanism.** The family is
        # already revoked in the database; deleting the cookie stops the browser
        # sending a credential that is now dead, which keeps it out of every
        # later request's logs. A client that ignores this gets a 401, not a
        # session.
        logged_out.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)
        return logged_out

    @app.get("/channel-bindings")
    def list_channel_bindings(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B2 #1 (`C0A-031` + `C0A-042` 归并): masked metadata, no secret.

        The freeze note is "返回 masked metadata，移除 `app_secret` 字段", and the
        shape of "masked" was settled by **ADR-0048 §二.6 (K9)**: the secret is
        not in the database at all -- it lives in the Stage Secret directory, and
        the row carries only non-sensitive references. So there is no masking to
        do here in the usual sense; there is a projection to keep honest.

        **What is deliberately absent, and why each one is absent:**

        * `current_token_hash` / `next_token_hash` -- migration 023 does not grant
          them and never should. A callback token's hash is not a redacted
          token; it is the **verifier** the ingress path compares against, so
          publishing it publishes the ability to test guesses offline.
        * `provider_instance_id` -- **T123**. `017` added it readable by nobody
          and said, in the column comment, that granting it belongs to the
          outbound wiring batch together with the capability declaration T78
          still owes. A list route picking it up because the column is adjacent
          is exactly what that registration exists to prevent.
        * a `rotation_pending` flag -- it would have needed either
          `next_token_hash` (the verifier again) or an inference the schema does
          not guarantee, and the three timestamps already say everything it
          could: when the current credential started, when the next one begins,
          when the overlap ends.
        * the six K9 columns (`secret_ref`, `version`, `last_verified_at`,
          `fingerprint`, ...) -- **they do not exist yet**. `D18` is the gap and
          B2 #2/#4 are the batches that close it. Returning `null` for four
          fields that no writer can ever set would be a shape a client codes
          against and a promise nobody made.

        `Page<T>` through the shared constructor, like every other list route.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="channel_config.list",
                resource=tenant_resource_ref(
                    resource_type="channel_config", tenant_id=tenant_id
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT config_id, source, status, profile_release_id,
                       current_valid_from, next_valid_from, overlap_ends_at, created_at,
                       secret_ref, secret_version, secret_fingerprint,
                       secret_updated_at, secret_last_verified_at
                  FROM channel_bindings
                 WHERE tenant_id = %s
                 ORDER BY created_at, config_id
                 LIMIT %s
                """,
                (tenant_id, MAX_PAGE_ITEMS + 1),
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        return _page_or_refuse(
            [
                {
                    "config_id": row["config_id"],
                    "source": row["source"],
                    "status": row["status"],
                    # The release this binding is pinned to, and the one field
                    # that ties this page to
                    # `PUT /channel-bindings/{config_id}/profile-release`.
                    #
                    # **`release_id`, not `profile_release_id`** -- the column is
                    # called the latter, and the wire has called it the former
                    # since CP-2: `GET /profiles/releases`, the repin request
                    # body and the repin response all say `release_id`. The
                    # first version of this route published the column name, so
                    # a client could read a binding here and have its own value
                    # rejected by the write route two lines later. Found by
                    # `test_the_listed_binding_is_the_one_the_repin_route_names`,
                    # which exists because the two routes were built two days
                    # apart.
                    "release_id": str(row["profile_release_id"]),
                    "current_valid_from": _iso_timestamp(row["current_valid_from"]),
                    "next_valid_from": (
                        _iso_timestamp(row["next_valid_from"])
                        if row["next_valid_from"] is not None
                        else None
                    ),
                    "overlap_ends_at": (
                        _iso_timestamp(row["overlap_ends_at"])
                        if row["overlap_ends_at"] is not None
                        else None
                    ),
                    "created_at": _iso_timestamp(row["created_at"]),
                    # **K9's masked metadata, and the reason this route counted
                    # as only half-delivered until now.** ADR-0053 recorded that
                    # B2 #1 shipped the wiring and could not ship these, because
                    # the columns did not exist (`D18`); migration 029 adds
                    # them, and the freeze row's condition for counting this
                    # route as delivered is that they appear here.
                    #
                    # **Their arrival is what took `channel_config.list` out of
                    # `viewer`** -- T114's criterion names `secret_ref`, a
                    # version and a fingerprint in as many words. See
                    # `system_roles.VIEWER_EXCLUSIONS`.
                    #
                    # Null across the board for a binding whose credential was
                    # never registered, which is every binding today. 029's
                    # constraint makes that all-or-nothing, so these four cannot
                    # disagree about whether a registration happened.
                    "secret_ref": row["secret_ref"],
                    "version": row["secret_version"],
                    "fingerprint": row["secret_fingerprint"],
                    "updated_at": (
                        _iso_timestamp(row["secret_updated_at"])
                        if row["secret_updated_at"] is not None
                        else None
                    ),
                    "last_verified_at": (
                        _iso_timestamp(row["secret_last_verified_at"])
                        if row["secret_last_verified_at"] is not None
                        else None
                    ),
                }
                for row in rows[:MAX_PAGE_ITEMS]
            ],
            has_more=len(rows) > MAX_PAGE_ITEMS,
        )
    @app.post("/auth/change-password", status_code=204)
    def change_password(
        command: ChangeOwnPasswordRequest,
        context: AuthContext = Depends(authenticated_pending_password_change),  # noqa: B008
    ) -> Response:
        """Route freeze B1 #30 (**ADR-0052**): the only way out of K8's flag.

        **This route is on the freeze because ADR-0048 §二.5 required it and no
        drawing contained it.** K8 says `must_change_password` is set on reset
        and that while it is true everything answers 403 except "改密路由" --
        and that route existed in no freeze, no interface inventory and no code.
        Without it the ruling is a closed loop: the only action that looks like
        an escape is another reset, and reset is what sets the flag.

        **It does not call `authorize()`**, and that is the design rather than an
        omission (ADR-0052 §四.3). Subject and object are the same person, and
        the authorisation is `current_password` -- something no permission grant
        can substitute for and no stolen token contains. A permission here would
        lock out precisely the account this route is for: a freshly created
        employee holds `permissions == []` (B1 #14, T125), so a guarded
        change-password route would answer 403 to the one caller who must be
        able to reach it.

        **Everything the caller holds stops working, including the token that
        made this call.** `credential_version` is incremented and every session
        row is revoked, so the answer is 204 and the next step is logging in
        again. `unified-authorization-layer.md:160` asks for exactly this on
        reset ("撤销 refresh family，递增 credential version"), and the same
        reasoning covers a voluntary change: if the old password leaked, the
        sessions it opened are what the attacker still has.

        A wrong `current_password` is **401**, deliberately indistinguishable
        from any other authentication failure -- it is the same question login
        answers, and the same silence.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        # **This is the route the throttle exists for.** K8 leaves it as the one
        # door open to a token that is otherwise locked out of everything, and
        # it verifies a secret -- so without a limit the lockout was a doorway
        # for guessing rather than a containment boundary (B1 #17 independent
        # review, residual risk 2; the user ruled it be fixed rather than
        # registered).
        #
        # Keyed on the subject, taken from the token: the caller cannot choose
        # whose counter they spend.
        _guard_password_attempts(
            scope="password-change",
            identity=str(context.subject.id),
            policy=PASSWORD_CHANGE_POLICY,
        )
        if command.new_password == command.current_password:
            # Not pedantry: under K8 this is the shape that would satisfy the
            # flag without changing anything, which is the whole point of the
            # flag. Refused before the hash is computed so that the answer does
            # not depend on how long argon2 takes.
            raise HTTPException(status_code=422, detail="NEW_PASSWORD_MUST_DIFFER")
        # Hashed before the transaction opens, for the reason `POST /employees`
        # gives: argon2 costs tens to hundreds of milliseconds and about 64 MiB,
        # and none of that needs a connection held open.
        new_password_hash = hash_password(command.new_password)
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                "SELECT password_hash FROM employees WHERE tenant_id = %s AND id = %s",
                (tenant_id, context.subject.id),
            ).fetchone()
            if row is None or not verify_password(
                str(row["password_hash"]), command.current_password
            ):
                _record_password_failure(
                    scope="password-change",
                    identity=str(context.subject.id),
                    policy=PASSWORD_CHANGE_POLICY,
                )
                raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
            connection.execute(
                """
                UPDATE employees
                   SET password_hash = %s,
                       password_changed_at = now(),
                       credential_version = credential_version + 1,
                       must_change_password = false
                 WHERE tenant_id = %s AND id = %s
                """,
                (new_password_hash, tenant_id, context.subject.id),
            )
            revoked = connection.execute(
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND employee_id = %s AND revoked_at IS NULL
                RETURNING id
                """,
                (tenant_id, context.subject.id),
            ).fetchall()
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=context.subject.id,
                action="employee.change_own_password",
                correlation_id=context.correlation_id,
                reason_code="PASSWORD_CHANGED_BY_OWNER",
                evidence={"sessions_revoked": len(revoked)},
            )
        throttle.clear(scope="password-change", identity=str(context.subject.id))
        # 204 for `POST /auth/logout`'s reason: this command is finished when it
        # returns, so parity 5.2 does not ask for a receipt. There is also
        # nothing safe to echo -- the caller knows what they set, and a body
        # here could only carry either the credential or a claim about it.
        return Response(status_code=204)

    def _audit_channel_binding_event(
        connection: object,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        config_id: str,
        action: str,
        correlation_id: UUID,
        reason_code: str,
        evidence: dict[str, object],
    ) -> None:
        """One row per change to a binding's own state.

        **`resource_id` is deliberately `NULL`, and that is a settled decision
        rather than an omission.** A binding's identity is `config_id`, which is
        `text` and does not fit a `uuid` column; the repin path learned this the
        expensive way -- its first version put the *release* id there under
        `resource_type='channel_binding'`, so anyone querying by
        `(resource_type, resource_id)` would have been handed a release and told
        it was a binding (independent review 2026-09-03, P3-1). The identity
        travels in `evidence.config_id`, exactly as `profiles/binding.py` does
        it.

        A second helper rather than a shared one, because the one in
        `binding.py` is repin-shaped (it takes two release ids and a
        conversation count) and generalising it would make both call sites
        describe their event through a signature built for the other.
        """

        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, %s, %s, 'channel_binding', NULL, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                actor_id,
                action,
                correlation_id,
                reason_code,
                Jsonb({"config_id": config_id, **evidence}),
            ),
        )

    @app.post("/channel-bindings/{config_id}/deactivate", status_code=200)
    def deactivate_channel_binding(
        config_id: str,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B2 #5 (`C0A-034`): 停用 + revoke callback token；旧 DELETE 不移植.

        **Two clauses, and the second is the one with teeth.** Flipping `status`
        is enough to stop callbacks *now* -- the ingress adapter refuses any
        binding that is not `active` before it looks at the token at all. What it
        does not do is stop them *later*: the hash stays in the row, so anything
        that re-activates the binding silently restores a credential that may
        have been the reason for disabling it. So this route **overwrites**
        `current_token_hash` with the hash of a token it generates and throws
        away, and clears the pending rotation.

        **Nothing is deleted.** parity §6.4 removed the old `DELETE` from v2 for
        tenants and employees, and the same reasoning governs here: the binding
        row is what every `inbox_events` / `conversation_aggregates` row points
        at through `config_id`, and deleting it would strand history that the
        conversation surface still reads.

        **No retention clock.** §6.4's `disabled_at` / `purge_eligible_at` pair is
        written about subjects that have a purge route to become eligible for;
        there is no B2 purge in the freeze, and a clock nothing reads is the
        shape migration 018 refused to add. `status` is the fact, and
        `GET /channel-bindings` publishes it.

        **A second call answers 409**, like `POST /employees/{id}/deactivate` and
        for the same reason: re-running it would mint and discard another token,
        which is a write with no meaning, and a retry must not be able to
        manufacture the appearance of a fresh revocation.

        **`Idempotency-Key` is required and nothing de-duplicates on it**
        (**T121**: there is no command-level store anywhere in the repo), so a
        genuine retry after a lost 200 gets the 409, not the original receipt.
        `POST /employees/{id}/deactivate` names the way out -- read the state
        back -- and **here that answer only covers half**: `GET
        /channel-bindings` publishes `status`, so "is it disabled" is
        answerable, but nothing publishes a credential fingerprint, so "was the
        token really destroyed" is not until K9's six columns land with B2 #4.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="channel_config.update",
                resource=tenant_resource_ref(
                    resource_type="channel_config", tenant_id=tenant_id
                ),
            )
        )
        # Generated outside the transaction and never returned: the plaintext
        # exists for exactly as long as it takes to hash it. What lands in the
        # column is a well-formed hash of a token **nobody holds**, which is what
        # makes this a revocation rather than a flag.
        revoked_token_hash = sha256_token(new_write_only_token())
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **One statement, because the fact being recorded is the
            # *prior* state** (independent review 2026-09-05, P3-1). This write
            # can destroy **two** credentials -- the live one and a rotation
            # already scheduled in `next_token_hash` -- and migration 024 argues
            # at length that clearing the second is necessary. An audit row that
            # does not say whether it happened is missing a fact the migration
            # thought worth a paragraph.
            #
            # `next_valid_from IS NOT NULL` is the proxy, and it is deliberately
            # not `next_token_hash`: 023 withholds `SELECT` on both hashes and
            # this batch is not the place to widen that. The timestamps are how
            # B2 #1 publishes rotation state for the same reason. The proxy
            # misses "hash present, no valid_from", which the ingress path
            # ignores anyway -- `next_match` requires both.
            #
            # A CTE rather than two statements: both arms see one snapshot, so
            # the `prior` read cannot be overtaken by a concurrent write between
            # them.
            row = connection.execute(
                """
                WITH prior AS (
                    SELECT next_valid_from IS NOT NULL AS pending_rotation
                      FROM channel_bindings
                     WHERE tenant_id = %s AND config_id = %s AND status = 'active'
                ), updated AS (
                    UPDATE channel_bindings
                       SET status = 'disabled',
                           current_token_hash = %s,
                           next_token_hash = NULL,
                           next_valid_from = NULL,
                           overlap_ends_at = NULL
                     WHERE tenant_id = %s AND config_id = %s AND status = 'active'
                    RETURNING config_id, status
                )
                SELECT updated.config_id, updated.status, prior.pending_rotation
                  FROM updated CROSS JOIN prior
                """,
                (tenant_id, config_id, revoked_token_hash, tenant_id, config_id),
            ).fetchone()
            if row is None:
                # Told apart **inside the transaction**, the shape B1 #18 uses:
                # two statements can be overtaken by a concurrent deactivation
                # between them.
                existing = connection.execute(
                    "SELECT status FROM channel_bindings"
                    " WHERE tenant_id = %s AND config_id = %s",
                    (tenant_id, config_id),
                ).fetchone()
                if existing is None:
                    # A binding this tenant cannot see and one that does not
                    # exist are one answer, or the route becomes a cross-tenant
                    # existence oracle. RLS makes both arrive here identically.
                    raise HTTPException(status_code=404, detail="CHANNEL_BINDING_NOT_FOUND")
                raise HTTPException(status_code=409, detail="CHANNEL_BINDING_ALREADY_DISABLED")
            _audit_channel_binding_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                config_id=config_id,
                action="channel_binding.deactivate",
                correlation_id=context.correlation_id,
                reason_code="CHANNEL_BINDING_DEACTIVATED",
                # Counts and facts, never the hash: the evidence records **that**
                # the credential was replaced, not what it was replaced with. A
                # hash in an append-only table is a verifier somebody can grind
                # against offline, which is the whole reason B2 #1 refuses to
                # publish one.
                evidence={
                    "callback_token_revoked": True,
                    # **Not a constant**, unlike the boolean above it: this one
                    # says whether a second credential -- one already scheduled
                    # to become valid -- was destroyed along with the live one.
                    "pending_rotation_cleared": bool(row["pending_rotation"]),
                },
            )
        response.headers["cache-control"] = "no-store, private"
        # `{"receipt": …, "result": …}` (ADR-0050 §三): revoking a channel
        # credential is an external-channel action, one of the cases parity §5.2
        # names, so the receipt has to be a receipt and the domain fields nest.
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="channel_config",
                    resource_id=config_id,
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "config_id": row["config_id"],
                "status": row["status"],
                # Stated rather than implied: the caller asked to stop a channel,
                # and "the token is gone too" is the half they cannot see from
                # `status` alone.
                "callback_token_revoked": True,
            },
        }

    @app.get(
        "/conversations/{conversation_id}",
        responses=openapi_responses("GET", "/conversations/{conversation_id}"),
    )
    def conversation_detail(
        conversation_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #2 (`C0B-002`): the conversation's state, not its messages.

        **The first route on the B3 surface**, and the cluster reads a different
        drawing from B1 and B2: `conversation-engine.md` is the behavioural
        authority, `control-plane-parity.md` §6.2 is the field list.

        **Still partial delivery, and now for one reason instead of three**
        (ADR-0076, then ADR-0081). §6.2's detail projection plus ADR-0054's
        takeover-period projection come to eight groups. Two of them --
        refused slot updates in `audit_events`, the cost gate stopping
        understanding in `policy_decisions` -- were absent because the API role
        could not read those tables; migration 042 grants the columns and
        `_takeover_governance_projection` returns them.

        **What is still missing is a different kind of missing**:
        **manipulation observation** is not written by anything. The engine
        contract lists it (`conversation-engine.md:833`) and no Python produces
        it, so no grant would help and this route cannot fix it. The freeze row
        stays 部分交付 for it, on the 口径 ADR-0053 set for B2 #1: a route whose
        surface is incomplete does not count as delivered, even when the gap is
        somebody else's writer.

        **It is not returned as `null`.** A null would say "we looked and there
        was nothing", and what is true is "nothing has ever written it". The two
        items that *are* returned now say `total: 0` / `halted: false` in their
        empty state, which is the same rule pointing the other way: after a
        lookup, zero is a finding.

        **Slot values are not here, on purpose.** §6.2 asks for 信息槽摘要 and
        says in the next clause that 详细敏感证据 needs an extra action. A slot's
        *value* is customer content -- a name, an address, a number they typed --
        and this repository has no field-level authorisation to split one slot
        from another (T164 registered exactly that gap). So the summary is
        provenance: which slots exist, how confident, who wrote each one, and
        which event it came from. The narrow reading can be widened later on
        evidence; the wide one cannot be taken back.

        **The columns win over `state`.** The aggregate is stored twice -- the
        engine writes the whole document into `state` *and* keeps `lifecycle`,
        `control_mode`, `state_version`, `generation` and `target_stack` as
        columns. They agree today (the handoff path sets the key on the document
        before the same transaction writes both), but the columns are what the
        engine's CAS and `POST .../takeover` key on, so the projection reads
        those and uses `state` only for what has no column of its own.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="conversation_aggregate.read",
                resource=tenant_resource_ref(
                    resource_type="conversation_aggregate",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT id, lifecycle, control_mode, state_version, generation,
                       profile_release_id, config_id, target_stack, state,
                       last_inbound_at, last_outbound_at
                  FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if row is None:
                # Same reasoning as every other detail route here: a row this
                # tenant cannot see and a row that does not exist must be
                # indistinguishable from the wire, or the 404/403 split becomes a
                # cross-tenant existence oracle. RLS makes both arrive here
                # identically.
                #
                # **Raised inside the transaction now** (second delivery): the
                # two governance reads have to see the same snapshot as the
                # aggregate, so the block cannot close before the check. The
                # takeover route has raised from inside its transaction since it
                # was written; a read that rolls back is the same read.
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            governance = _takeover_governance_projection(
                connection,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                control_mode=row["control_mode"],
            )

        state = row["state"] if isinstance(row["state"], dict) else {}
        frames = row["target_stack"] if isinstance(row["target_stack"], list) else []
        commitment_refs = state.get("commitment_refs") or []
        exit_signal = state.get("pending_exit_signal")

        response.headers["cache-control"] = "no-store, private"
        return {
            "conversation_id": str(row["id"]),
            "lifecycle": row["lifecycle"],
            "control_mode": row["control_mode"],
            # **`state_version`, named for what it is to a reader.** §6.2 calls
            # it "projection version": the number a client echoes back to say
            # which picture it was looking at.
            "projection_version": int(row["state_version"]),
            "generation": int(row["generation"]),
            "profile_pin": {"release_id": str(row["profile_release_id"])},
            "channel_config_id": row["config_id"],
            "last_inbound_at": _iso_timestamp_or_none(row["last_inbound_at"]),
            "last_outbound_at": _iso_timestamp_or_none(row["last_outbound_at"]),
            "target_stack": [_frame_projection(frame) for frame in frames],
            # **The empty state is stated, never omitted** (ADR-0054's revision
            # to §6.2, and ADR-0076 §四.1 repeats it). The commitment ledger does
            # not exist in M1a, so this is always zero -- and a missing field
            # would be read as "the system remembered something and chose not to
            # show it".
            #
            # **而「显示」与「显示成什么」是两件事**（用户 2026-09-13）：
            # 一个光秃秃的 `0` 与「这一轮确实没有承诺」在屏幕上一模一样，
            # 于是它成了一个**会误导人的真值**。`status` 那一格说清它是哪一种 0，
            # 而字段本身照显 —— 图纸那半句一个字不改。
            "commitments": {
                # `registered` 这个键名不动（客户端在读），而它旁边多了 `status`：
                # **台账没启用时的 0 与「这一轮没有承诺」的 0 不是一回事**。
                "registered": len(commitment_refs),
                **{
                    key: value
                    for key, value in commitment_display(commitment_refs).items()
                    if key != "count"
                },
            },
            # **与上面那一格同一个裁定**（用户 2026-09-13，本轮照抄）：
            # `outcome_ref` 今天零写点，于是这里永远投影一个 `null`，
            # 而客户端分不出「这一轮没有结果」与「这个能力还没实现」。
            # 字段照显，旁边那句话说清它是哪一种 —— 见 `outcome_display`。
            "outcome": outcome_display(state.get("outcome_ref")),
            "last_transition": state.get("last_transition"),
            # **§6.2's five takeover-period items, in one place.** Three come
            # from the aggregate document and two from the governance tables
            # (042). They are merged here rather than inside
            # `_takeover_projection` because that function also feeds
            # `_change_digest`, and the resume token must not move when a row
            # another service wrote appears -- the reason is written there.
            #
            # `GET .../resume-preview` returns the same five: an operator who
            # confirms one picture and then reads another on the detail page is
            # the drift that route's docstring already argues against. What it
            # does *not* do is hash them into the change digest.
            "takeover_projection": {
                **_takeover_projection(frames, exit_signal, commitment_refs),
                **governance,
            },
        }

    @app.post(
        "/conversations/{conversation_id}/takeover",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        responses=openapi_responses("POST", "/conversations/{conversation_id}/takeover"),
    )
    def take_conversation_over(
        conversation_id: UUID,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #6 (`C0B-007` 拆): a person takes the conversation.

        §8 names the transition in one sentence -- 「人工接管 transition 必须
        generation++、invalidate pending events、阻止自主生成与发送」-- and this
        route does exactly those three things and nothing else.

        **`suspend_target_stack_for_human` is imported, not rewritten.** The
        engine performs the same transition when a policy hook decides handoff,
        and the rule about which frame statuses may be suspended belongs to one
        function. `services/api/.../profiles/binding.py` already reaches into
        `worker.target_stack` for `repair_target_stack_for_repin` -- same
        situation, same answer, and the image ships all three services anyway.

        **`generation++` is done here rather than inside that function**,
        because suspending is a stack operation and bumping the generation is a
        statement about the whole conversation: every timer and every plan
        minted before this moment is now stale. The engine's handoff turn does
        the same two things side by side.

        **What is not blocked** (ADR-0054's revision to §8): the understanding
        step keeps running, slots keep being written, and the copilot only
        *starts* working once `control_mode` is `human`. What stops is
        autonomous sending -- the `pre-generation` and `pre-delivery` hooks read
        `control_mode`. An earlier wording said "block the model", and §18.5
        contradicts it outright.

        **409 on a conversation already held by a person.** Not idempotent
        politeness: a second takeover would bump the generation again and
        invalidate another round of timers, so "no-op" would be a lie and
        "repeat the work" would be worse.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="conversation_takeover.create",
                resource=tenant_resource_ref(
                    resource_type="conversation_takeover",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **The reasons stay here, the work moves out** (B3 #11, ADR-0079).
            # A caller who named this conversation is owed the difference
            # between "no such conversation", "somebody already has it" and
            # "the engine cannot process this document either"; the bulk
            # caller named none of them and skips instead. So the checks are
            # read here and the transition itself is
            # `_hand_conversation_to_a_person`, which both callers share.
            row = connection.execute(
                """
                SELECT control_mode, state FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            if row["control_mode"] == "human":
                raise HTTPException(status_code=409, detail="CONVERSATION_ALREADY_HUMAN")
            document = row["state"]
            if not isinstance(document, dict) or "target_stack" not in document:
                # Fail closed, the same way the repin path does: a conversation
                # whose document is missing is one the engine cannot process
                # either, and flipping `control_mode` on it would hand a person
                # something nobody can resume.
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_RESUMABLE")
            taken = _hand_conversation_to_a_person(
                connection,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                actor_id=context.subject.id,
                correlation_id=context.correlation_id,
                reason_code="CONVERSATION_TAKEN_OVER",
            )
            if taken is None:
                # **Only one of the body's three refusals can reach here**
                # (independent review P3-2). It returns `None` for a missing
                # row, for a conversation already held, and for an
                # unprocessable document -- and the checks above have just
                # ruled the first and third out on this connection. What is
                # left is the race: somebody took it between the read and the
                # lock. A third caller of the body would have to make that
                # argument again, which is the reason it is written here.
                raise HTTPException(status_code=409, detail="CONVERSATION_ALREADY_HUMAN")

            require_obligations_discharged(decision, frozenset({"audit"}))
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="conversation_takeover",
                    resource_id=str(conversation_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            # **`{"receipt", "result"}`** (ADR-0050 §三): the command has a
            # domain answer. "How many frames did I just suspend and how many
            # timers did I just stop" is what tells an operator whether the
            # conversation is actually quiet, and no other route reports it.
            "result": {
                "control_mode": "human",
                "generation": taken["generation"],
                "frames_suspended": taken["frames_suspended"],
                "events_invalidated": taken["events_invalidated"],
            },
        }

    @app.get(
        "/conversations/{conversation_id}/messages",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("GET", "/conversations/{conversation_id}/messages"),
    )
    def conversation_messages(
        conversation_id: UUID,
        response: Response,
        cursor: str | None = None,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #3 (`C0B-003` + `030` 归并): 稳定 cursor、递归脱敏.

        **The first route in this repository that actually pages.** Every other
        list answers `409 PAGE_LIMIT_EXCEEDED` when one page is not enough --
        a deliberate refusal while no read model could outgrow a page. A
        conversation can, and §6.2 asks for a stable cursor by name, so this one
        carries the `next_cursor` half of `Page` that has been in the contract
        since CP-0 and unused until now.

        **One timeline from two tables.** Inbound messages live in
        `inbox_events` and outbound ones in `outbox_messages`; neither is the
        conversation. The union is ordered by `(created_at, id)` -- a total
        order, which the timestamp alone is not: two rows written by different
        processes can share a microsecond, and paging on the timestamp would
        skip one of them or repeat it forever.

        **Only `customer_message` events.** `inbox_events` also carries timers,
        channel events, business events and delivery receipts. They are things
        that happened, not things anybody said, and a message page that showed
        them would make the customer look like they had sent a timer.

        **Recursive redaction over the payload** (§6.2), not a field allow-list:
        the envelope has room for provider-specific extras, and a list of safe
        fields goes stale the first time a channel adapter adds one.

        **`message.list` is not in `viewer`** -- it is the first action to hit
        the first bullet of the criterion in `system_roles.py` ("conversation or
        message content"). `operator` gains it in the same commit, because an
        operator who takes a conversation over and cannot read what the customer
        said cannot work it.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="message.list",
                resource=tenant_resource_ref(
                    resource_type="message",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        after = _decode_cursor(cursor) if cursor else None
        with database.transaction(tenant_id=tenant_id) as connection:
            # **The conversation is resolved first, and separately.** Without
            # this, an unknown id and a conversation with no messages both
            # answer an empty page -- and a caller cannot tell "nothing was
            # said" from "no such conversation". The aggregate read is also what
            # makes the 404 here mean the same thing it means in B3 #2.
            known = connection.execute(
                "SELECT 1 FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
                (tenant_id, conversation_id),
            ).fetchone()
            if known is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            rows = connection.execute(
                _MESSAGE_PAGE,
                # (the query is a module constant beside the two predicates it
                # shares with `POST .../read`; see `_VISIBLE_MESSAGE`)
                {
                    "tenant": tenant_id,
                    "conversation": conversation_id,
                    "after": after[0] if after else None,
                    "after_source": after[1] if after else None,
                    "after_ordinal": after[2] if after else None,
                    "after_id": after[3] if after else None,
                    # One more than the page, so "is there another page" is
                    # answered by the query rather than by a second count that
                    # could disagree with it.
                    "limit": MESSAGE_PAGE_SIZE + 1,
                },
            ).fetchall()
        has_more = len(rows) > MESSAGE_PAGE_SIZE
        visible = rows[:MESSAGE_PAGE_SIZE]
        next_cursor = (
            _encode_cursor(
                visible[-1]["created_at"],
                visible[-1]["source_table"],
                visible[-1]["ordinal"],
                visible[-1]["id"],
            )
            if has_more and visible
            else None
        )
        response.headers["cache-control"] = "no-store, private"
        return page(
            items=[_message_projection(row) for row in visible],
            next_cursor=next_cursor,
        ).as_dict()

    @app.post(
        "/conversations/{conversation_id}/messages",
        status_code=202,
        responses=openapi_responses("POST", "/conversations/{conversation_id}/messages"),
    )
    def send_message(
        conversation_id: UUID,
        command: SendMessageRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #4（ADR-0048 K15 → ADR-0141 决策 2 裁开）：坐席说一句话。

        **在这条之前，接管能接，接了之后说不了话。** 全仓
        `INSERT INTO outbox_messages` 只有三处、全在 `services/worker`，
        而 `fastagent_app` 对那张表只有 `033` 给的 `SELECT`。
        盘点把它列为商家页面的第一条硬前置。

        ## 四步，而这个顺序是被外键强制的

        `control-plane-parity.md` §6.2：「员工发送仍走 intent/policy/outbox/
        delivery」。那不是一句约定 —— `outbox_messages` 有
        `FOREIGN KEY (tenant_id, intent_id) REFERENCES intent_records`
        （`001:243`），所以一条出站行**必须**先有一条意图。
        本 route 走的四步与引擎逐字相同（`pipeline.py` 的
        `_persist_accepted_proposal`）：立意图 → 记策略痕 → 标已批准 → 排队。

        ## ⚠️ 它**不**过主动性闸，而那是一次裁定（ADR-0143 §一）

        `worker/proactivity.py` 那四道（活跃窗口 / 再触达上限 / 静默时段 /
        退出信号）**对这条路径一律不生效**。用户 2026-09-20 裁定，理由是
        那四道是给 **AI 自发行为**设的节流，而坐席是在一次看得见上下文的
        接待里按下发送并为这一条负责；把闸加在人身上，会让坐席遇到
        「我按了发送，而什么都没发生」——**那是一种比它要防的东西更常见、
        更消耗信任的故障**。

        **代价写明并被接受**：静默时段绕过（半夜发得出去）、退出信号绕过
        （客户说过「别再发了」之后也发得出去）。

        **实现上它表现为「不做某件事」**：不写 `command` 里那个
        proactivity 块。`proactivity_deadline` 在块缺席时返回 `None`，
        而它的 docstring 逐字写着「`None` for every row that is not a
        proactive send」—— 人工发送本来就不是主动发送，语义天然对上。

        **而「不做某件事」最容易被下一个人顺手改掉**：在这里加一句
        `evaluate_proactivity(...)` 看起来永远像在修一个漏洞。所以有
        `tests/contracts/test_human_send_skips_proactivity.py` 钉着它。

        ## 要求会话已被接管，而这**不是**那四道闸之一

        `control_mode != 'human'` 时拒绝。理由来自 ADR-0054 而不是本裁定：
        接管被做成一等命令、会抬 `generation`、会作废活着的定时器，
        正是为了让「谁在说话」无歧义。允许未接管就发，等于有两个说话者
        而没有交接记录，而 `resume-preview` 那套「接管期间变了什么」
        也就没有锚点。

        ## 回执是 `accepted` 而不是 `succeeded`

        这一刻发生的事是**排进队列**，不是**送达**。parity §5.2 里
        `accepted` 的意思正是「will be worked」。界面上那个「已发送」
        只能来自投递回执 —— 同一节逐字：「UI 的『已发送』只能来自
        receipt，不来自按钮点击成功」。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="conversation_message.create",
                resource=tenant_resource_ref(
                    resource_type="conversation_message",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )

        intent_id = uuid4()
        policy_id = uuid4()
        outbox_id = uuid4()

        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            aggregate = connection.execute(
                """
                SELECT control_mode, state
                  FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if aggregate is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            if aggregate["control_mode"] != "human":
                # **409 而不是 403**：这不是「你没有权限」，是「这条会话
                # 现在不归你说话」—— 一个状态冲突，先接管就能解决。
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_TAKEN_OVER")

            frame_id = _frame_to_speak_in(aggregate["state"])
            if frame_id is None:
                # 每条会话出生时都带一个入口帧（`pipeline.py` 的初始聚合），
                # 所以这一支在今天不可达 —— 但 `target_frame_id` 是 NOT NULL，
                # 而**一个不可达的分支好过一个会在写库那一刻才炸的 None**。
                raise HTTPException(status_code=409, detail="CONVERSATION_HAS_NO_FRAME")

            connection.execute(
                """
                INSERT INTO intent_records(
                    tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                    strategy_id, target_frame_id, idempotency_key, preconditions,
                    policy_evidence
                ) VALUES (%s, %s, %s, 'send_message', 'PROPOSED', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    intent_id,
                    conversation_id,
                    Jsonb({"content": {"type": "text", "text": command.text}}),
                    # **`step` 说清这一条是谁提的。** 引擎那边写的是
                    # `{"step": "generation"}` / `{"step": "platform"}`；
                    # 这里是 `human`，并带上是哪一个人 —— 台账上
                    # 「这句话是谁发的」不该要再去关联一次审计。
                    Jsonb({"step": "human", "employee_id": str(context.subject.id)}),
                    # `strategy_id` 是 NOT NULL text。人工发送没有策略，
                    # 而**写一个假的策略名比写这个诚实的常量更糟**。
                    "human-send",
                    frame_id,
                    idempotency_key,
                    # 引擎那边这里放的是计划步算出的前置条件。人工发送的
                    # 前置就是**这条会话归人**，而那一条上面刚验过。
                    Jsonb({"control_mode": "human"}),
                    Jsonb([str(policy_id)]),
                ),
            )
            record_policy_decision(
                connection,
                tenant_id=tenant_id,
                row_id=policy_id,
                conversation_id=conversation_id,
                intent_id=intent_id,
                output=PolicyOutput(
                    hook="pre_execute",
                    effect="allow",
                    reason_code="HUMAN_SEND",
                    policy_id="human-send",
                    policy_version="1.0.0",
                    obligations=audit_and_redact(),
                    context={"employee_id": str(context.subject.id)},
                ),
            )
            connection.execute(
                """
                UPDATE intent_records SET status = 'APPROVED', updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'PROPOSED'
                """,
                (tenant_id, intent_id),
            )
            connection.execute(
                """
                INSERT INTO outbox_messages(
                    tenant_id, id, conversation_id, intent_id, command, idempotency_key,
                    ordinal
                ) VALUES (%s, %s, %s, %s, %s, %s, 0)
                """,
                (
                    tenant_id,
                    outbox_id,
                    conversation_id,
                    intent_id,
                    # **`command` 里没有 `recipient`。** 客户那一侧由 dispatcher
                    # 用 `resolve_recipient_id(conversation_id)` 现解；
                    # `command["recipient"]` 只给管理者摘要那一族用。
                    #
                    # **也没有 proactivity 块** —— 见 docstring 里那一节。
                    Jsonb(
                        {
                            "audience": "customer",
                            "content": {"type": "text", "text": command.text},
                        }
                    ),
                    idempotency_key,
                ),
            )

        response.headers["Cache-Control"] = "no-store, private"
        return command_receipt(
            resource=resource_ref(
                resource_type="conversation_message",
                resource_id=str(outbox_id),
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="accepted",
        ).as_dict()

    @app.get(
        "/conversations/{conversation_id}/resume-preview",
        responses=openapi_responses(
            "GET", "/conversations/{conversation_id}/resume-preview"
        ),
    )
    def conversation_resume_preview(
        conversation_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #24 (平台新增, ADR-0054): the first half of handing back.

        **Phase one of a two-phase command.** ADR-0054 §三.4 makes resuming a
        conversation something a person confirms rather than something a button
        does: this route shows what changed while they held it, and hands back a
        `change_digest` that `POST .../resume` will compare against.

        **The projection is the same function the conversation detail uses.**
        「接管期变化投影」 has one definition (parity §6.2's revision), and two
        routes rendering it two ways is how the two would drift -- the operator
        would confirm one picture and the detail page would show another.

        **This is not `confirm_resume`** (ADR-0054 §三.4, said twice because the
        names collide): that one is a sentence to the *customer* inside the
        conversation. This is a control-plane handshake with the agent.

        **`conversation_aggregate.read`, not a new action.** Nothing here is
        outside what `GET /conversations/{id}` already returns except the
        digest, which is a hash of state and grants nothing on its own -- the
        resume still needs `conversation_resume.create`.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="conversation_aggregate.read",
                resource=tenant_resource_ref(
                    resource_type="conversation_aggregate",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT state_version, control_mode, target_stack, state
                  FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            governance = _takeover_governance_projection(
                connection,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                control_mode=row["control_mode"],
            )
            # **这通对话的摘要，给要交还的人看**（T200，2026-09-20）。
            #
            # 裁定九取的 (c) 是「只作废、不逐字改」，而作废那个动作挂在
            # `POST .../resume` 的 `invalidate_summary` 上。**一个人要能作废它，
            # 先得看得见它** —— 否则那个字段是一个没人知道该不该按的按钮。
            #
            # 只读 `active`：被作废过的行留在库里（审计），但它既不进提示词、
            # 也不该再出现在「AI 现在以为这通对话在聊什么」这份预览里。
            summary_rows = connection.execute(
                """
                SELECT line FROM conversation_summaries
                 WHERE tenant_id = %s AND conversation_id = %s AND status = 'active'
                 ORDER BY ordinal
                """,
                (tenant_id, conversation_id),
            ).fetchall()
        state = row["state"] if isinstance(row["state"], dict) else {}
        frames = row["target_stack"] if isinstance(row["target_stack"], list) else []
        projection = _takeover_projection(
            frames, state.get("pending_exit_signal"), state.get("commitment_refs") or []
        )
        response.headers["cache-control"] = "no-store, private"
        return {
            "conversation_id": str(conversation_id),
            # **AI 现在以为这通对话在聊什么。** 交还之前请读一遍 ——
            # 不对就用 `invalidate_summary` 把它作废（裁定八 / 九）。
            "conversation_summary": [str(r["line"]) for r in summary_rows],
            "control_mode": row["control_mode"],
            "projection_version": int(row["state_version"]),
            # **Offered even when the conversation is not held by a person.**
            # The digest is a fact about the row, and refusing to compute it
            # here would only move the 409 from `resume` (where it belongs, with
            # the write) to a read that changes nothing.
            # **The digest hashes `projection`, the response carries the merge**
            # (B3 #2 second delivery). Two rules pull against each other here and
            # both survive: this route and the detail page must render 「接管期
            # 变化投影」 the same way -- the paragraph above says why -- while the
            # digest must move only when the operator's picture of the
            # *conversation* moves. A digest over rows the worker writes would
            # go stale with the document untouched, and answer `POST .../resume`
            # with a 409 nobody can act on.
            "change_digest": _change_digest(projection, int(row["state_version"])),
            "takeover_projection": {**projection, **governance},
        }

    @app.post(
        "/conversations/{conversation_id}/resume",
        status_code=200,
        responses=openapi_responses("POST", "/conversations/{conversation_id}/resume"),
    )
    def resume_conversation(
        conversation_id: UUID,
        command: ResumeConversationRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #7 (`C0B-007/008` 归并, ADR-0054 §三.4): hand it back.

        **Three writes, and ADR-0054 says none may be skipped:**

        1. **`control_mode` back to `ai`, in the column *and* the `state`
           document.** The worker reads `aggregate["control_mode"]` from the
           document (`pipeline.py:750`), so writing only the column leaves the
           engine believing a person still holds the conversation -- it would
           keep refusing to generate, forever, with a route having answered 200;
        2. **Un-suspend the stack** (**T4**). `suspend_target_stack_for_human`
           has existed since takeover landed and until this route **nothing had
           ever undone it**: `resume` would answer 200, `control_mode` would go
           back to `ai`, and every frame would still be `suspended` -- after
           which `keep` and `push` are refused forever. This route is
           `resume_target_stack_from_human`'s first caller;
        3. **`generation++`, and old timers do not come back.** The takeover
           invalidated them; resuming does not resurrect them, because they were
           minted against a plan that no longer exists.

        **The digest is a compare-and-set, not a formality.** It is a hash over
        the takeover-period changes *and* the `state_version` they were read at,
        so a customer message that lands between the preview and this call makes
        the token stale and the answer 409. The operator then looks again --
        which is the entire point of the two phases.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="conversation_resume.create",
                resource=tenant_resource_ref(
                    resource_type="conversation_resume",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        audit_id = uuid4()
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                SELECT state_version, generation, control_mode, target_stack, state
                  FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                 FOR UPDATE
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            if row["control_mode"] != "human":
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_HELD_BY_A_PERSON")
            document = row["state"]
            if not isinstance(document, dict) or "target_stack" not in document:
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_RESUMABLE")

            frames = row["target_stack"] if isinstance(row["target_stack"], list) else []
            current = _change_digest(
                _takeover_projection(
                    frames,
                    document.get("pending_exit_signal"),
                    document.get("commitment_refs") or [],
                ),
                int(row["state_version"]),
            )
            if not compare_digest(current, command.acknowledged_change_digest):
                # **409 with nothing else said.** Which field moved is a
                # question `GET .../resume-preview` answers by being called
                # again, and answering it here would put conversation content
                # into an error body -- the shape `_http_error` refuses.
                raise HTTPException(status_code=409, detail="CHANGE_DIGEST_STALE")

            aggregate = resume_target_stack_from_human(
                aggregate=document, occurred_at=occurred_at
            )
            aggregate["control_mode"] = "ai"
            aggregate["generation"] = int(row["generation"]) + 1
            aggregate["state_version"] = int(row["state_version"]) + 1
            validate_contract("conversation-aggregate", aggregate)
            active = sum(
                1 for frame in aggregate["target_stack"] if frame["status"] == "active"
            )
            connection.execute(
                """
                UPDATE conversation_aggregates
                   SET generation = %s,
                       state_version = %s,
                       target_stack = %s,
                       control_mode = %s,
                       state = %s,
                       updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND state_version = %s
                """,
                (
                    aggregate["generation"],
                    aggregate["state_version"],
                    Jsonb(aggregate["target_stack"]),
                    aggregate["control_mode"],
                    Jsonb(aggregate),
                    tenant_id,
                    conversation_id,
                    int(row["state_version"]),
                ),
            )
            if command.invalidate_summary:
                # **把这通对话现有的摘要行整批作废**（裁定八 / 九 取 (c)，T200）。
                #
                # **行不删** —— 那是「保留双版本 / 审计」那一半
                # （`conversation-engine.md:1146`，裁定八逐字引的那条）。
                # 它们只是不再进提示词：`pipeline._conversation_summary`
                # 只读 `status = 'active'`。
                #
                # **而「新事实不静默覆盖人工 revision」在这里自动成立**：
                # 下一回合追的是**新的一行**（`ordinal` 更大），它不碰、
                # 也碰不到这些被作废的行 —— `071` 的触发器连 `line` 都不许改。
                # 人这一次的判断因此留得住。
                #
                # ⚠️ **不可逆**：`071` 的触发器写着
                # 「an invalidated summary line cannot become active again」——
                # 这正是 `invalidate_summary` 默认 `False` 的理由。
                connection.execute(
                    """
                    UPDATE conversation_summaries
                       SET status = 'invalidated',
                           invalidated_at = now(),
                           invalidated_by = %s
                     WHERE tenant_id = %s AND conversation_id = %s
                       AND status = 'active'
                    """,
                    (context.subject.id, tenant_id, conversation_id),
                )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    audit_id,
                    context.subject.id,
                    "conversation_resume.create",
                    "conversation_resume",
                    conversation_id,
                    context.correlation_id,
                    "CONVERSATION_RESUMED",
                    Jsonb(
                        {
                            "generation": aggregate["generation"],
                            "frames_active": active,
                            # The digest the operator confirmed. It is a hash of
                            # state, not state -- recording it makes "what did
                            # they say they had read" answerable later, and
                            # nothing else in the system can answer it.
                            "acknowledged_change_digest": command.acknowledged_change_digest,
                        }
                    ),
                ),
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="conversation_resume",
                    resource_id=str(conversation_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "control_mode": "ai",
                "generation": aggregate["generation"],
                "frames_active": active,
                # **Stated, because T4's failure was invisible**: a resume that
                # answers 200 with nothing active is the bug this route exists
                # to have fixed, and the number is how a caller can tell.
                "timers_restored": 0,
            },
        }

    @app.put(
        "/conversations/{conversation_id}/frames/{frame_id}/slots",
        status_code=200,
        responses=openapi_responses(
            "PUT", "/conversations/{conversation_id}/frames/{frame_id}/slots"
        ),
    )
    def write_conversation_slots(
        conversation_id: UUID,
        frame_id: UUID,
        command: WriteConversationSlotsRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #25 (平台新增, ADR-0054): the human writer of a slot.

        **`apply_slot_updates` refuses a model update onto a slot whose evidence
        says `source: human`, and until this route nothing could produce such a
        slot.** The engine's writeback hard-codes `source: "model"` and says so
        in a comment naming this route by name -- 「没有写入入口的防御一次都不会
        被执行」, which is why the freeze puts #25 in the same batch as #6/#7.

        **It is not in that batch, and that is a deviation I am recording rather
        than smoothing over**: takeover shipped alone, resume shipped with the
        preview, and this arrives third. Nothing regressed -- the defence was
        equally unexercised before -- but the freeze asked for one batch and got
        three.

        **`conversation_slot.update`, not `conversation_aggregate.update`.** The
        latter is declared a `service_action`: no person may trigger it, and the
        declaration is right -- it is how the engine writes back a whole
        transition. A person writing one slot is a different decision to grant,
        so it is a different action (K1).

        **Only while a person holds the conversation.** ADR-0054 §三 is about the
        takeover period, and a human slot written while the AI is driving would
        silently freeze that slot against the model forever -- the defence
        working exactly as designed, against a value nobody meant to protect.
        The narrow reading can be widened on evidence; the wide one leaves
        conversations with slots no engine can correct. Registered, not assumed.

        **`confidence` is 1.0 and `evidence_event_id` is the audit row.** The
        contract requires both on every evidence entry. A person asserting a
        value is not a probability, and §三.2 exempts human slots from the
        confidence comparison anyway; the id points at the one record that says
        who wrote it and when -- which is the only evidence a human write has.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="conversation_slot.update",
                resource=tenant_resource_ref(
                    resource_type="conversation_slot",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        audit_id = uuid4()
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                SELECT state_version, control_mode, state
                  FROM conversation_aggregates
                 WHERE tenant_id = %s AND id = %s
                 FOR UPDATE
                """,
                (tenant_id, conversation_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            if row["control_mode"] != "human":
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_HELD_BY_A_PERSON")
            document = row["state"]
            if not isinstance(document, dict) or "target_stack" not in document:
                raise HTTPException(status_code=409, detail="CONVERSATION_NOT_RESUMABLE")

            aggregate = deepcopy(document)
            frame = next(
                (
                    item
                    for item in aggregate["target_stack"]
                    if str(item.get("id")) == str(frame_id)
                ),
                None,
            )
            if frame is None:
                # **The frame, not the nearest frame.** `apply_slot_updates`
                # ignores an update naming a frame that is not on the stack
                # rather than redirecting it, and a person's write gets the same
                # rule stated louder: a 404 rather than a value landing on a goal
                # they did not name.
                raise HTTPException(status_code=404, detail="TARGET_FRAME_NOT_FOUND")
            if frame.get("status") in {"completed", "abandoned"}:
                # Terminal frames are the evidence of what happened; the engine
                # refuses to write their slots and so does this.
                raise HTTPException(status_code=409, detail="TARGET_FRAME_IS_TERMINAL")

            slots = frame.setdefault("slots", {})
            evidence = frame.setdefault("slot_evidence", {})
            for name, value in command.slots.items():
                slots[name] = value
                evidence[name] = {
                    "confidence": 1.0,
                    # **人工写也产出数组**（D3）—— 契约的 `anyOf` 收得下单数，
                    # 所以漏掉这里门禁不会红，**而那正是它值得写一句的原因**：
                    # 一个「新写入仍产出旧形状」的路径会让那句
                    # 「新的写回一律产出 `evidence_event_ids`」变成假话。
                    #
                    # **人工写是替换语义**（一个人明确写下这个值），所以是一条，不追加。
                    "evidence_event_ids": [str(audit_id)],
                    "recorded_at": recorded_at,
                    "source": "human",
                }
            aggregate["state_version"] = int(row["state_version"]) + 1
            validate_contract("conversation-aggregate", aggregate)
            connection.execute(
                """
                UPDATE conversation_aggregates
                   SET state_version = %s,
                       target_stack = %s,
                       state = %s,
                       updated_at = now()
                 WHERE tenant_id = %s AND id = %s AND state_version = %s
                """,
                (
                    aggregate["state_version"],
                    Jsonb(aggregate["target_stack"]),
                    Jsonb(aggregate),
                    tenant_id,
                    conversation_id,
                    int(row["state_version"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    audit_id,
                    context.subject.id,
                    "conversation_slot.update",
                    "conversation_slot",
                    conversation_id,
                    context.correlation_id,
                    "SLOTS_WRITTEN_BY_A_PERSON",
                    # **Names, not values** (ADR-0055 §二). A slot's value is what
                    # a customer told somebody -- personal data by default, and
                    # `audit_events` is append-only, so a value written here can
                    # never be taken back. Which slots were touched, by whom and
                    # on which frame is what makes the write reviewable.
                    Jsonb(
                        {
                            "frame_id": str(frame_id),
                            "slots": sorted(command.slots),
                        }
                    ),
                ),
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="conversation_slot",
                    resource_id=str(frame_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "frame_id": str(frame_id),
                # Names again, for the same reason.
                "slots_written": sorted(command.slots),
                "projection_version": aggregate["state_version"],
            },
        }

    @app.post("/conversations/{conversation_id}/manual-tasks", status_code=201)
    def file_manual_task(
        conversation_id: UUID,
        command: FileManualTaskRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #16 (`C0B-025`): owner / SLA / evidence required.

        **The first writer that can fill T91's three columns.** `034` created
        them nullable because the `manual_review` hook has no source for an SLA
        duration and no way to pick an owner; this route has both, so here they
        are not optional. A task filed with nobody on it and no deadline is the
        state the backlog cannot distinguish from the engine's own output, and
        that indistinguishability is what T91 was about.

        **`task_class` is `merchant_action` and is not in the body.** The other
        value is `platform_internal`, and it means "the platform has to do
        something about this delivery" -- a classification the engine derives
        from a delivery reason code. A merchant's employee filing work on their
        own conversation cannot honestly assert it, and offering the field would
        let them mark their own work as somebody else's.

        **`notify` is `none`, also not in the body.** `immediate` and `batched`
        exist so the *merchant* learns about something nobody saw; this task was
        filed by a person on that side, looking at the conversation. Writing
        `batched` here would put it in the hourly digest sent to the manager --
        a notification about work the merchant just created themselves.

        **That silence has ended, and the argument above did not depend on
        it** (ADR-0104, 2026-09-08). What this paragraph used to say was that
        nothing reaches a manager because **no route can configure one** --
        `tenant_manager_contacts` had two production readers and no writer at
        all, registered as T77/T177. `PUT /tenants/current/manager-contact` is
        now that writer, so a configured tenant *does* get its digest and its
        escalation notices. The decision above stands on what the row *means*,
        not on the silence, which is exactly why it survives the silence
        ending.

        **The owner is checked before the insert, and the foreign key is still
        there.** The pre-check turns "that employee is not here" into a 404
        rather than a constraint violation surfacing as a 500; the composite key
        `(tenant_id, owner_employee_id)` is what actually makes it impossible to
        hand a task across a tenant boundary, and it stays. A pre-check alone
        would be a race; the key alone would be an unreadable error.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="manual_task.create",
                resource=tenant_resource_ref(
                    resource_type="manual_task",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        # **Refused before the row is written, not after.** An SLA already past
        # is not a deadline, and a task that is born late is indistinguishable
        # in every report from one that was missed.
        if command.sla_due_at <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="SLA_DUE_AT_IS_IN_THE_PAST")

        task_id = uuid4()
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
                    (tenant_id, conversation_id),
                ).fetchone()
                is None
            ):
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            if (
                connection.execute(
                    "SELECT 1 FROM employees WHERE tenant_id = %s AND id = %s",
                    (tenant_id, command.owner_employee_id),
                ).fetchone()
                is None
            ):
                # 404 rather than 422, and the same 404 another tenant's
                # employee id produces: RLS makes them equally absent, so the
                # answer cannot be used to ask whether an id exists elsewhere.
                raise HTTPException(status_code=404, detail="OWNER_NOT_FOUND")
            connection.execute(
                """
                INSERT INTO manual_tasks(
                    tenant_id, id, conversation_id, status,
                    task_class, notify, reason_code,
                    owner_employee_id, sla_due_at, evidence
                ) VALUES (%s, %s, %s, 'open', 'merchant_action', 'none', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    task_id,
                    conversation_id,
                    command.reason_code,
                    command.owner_employee_id,
                    command.sla_due_at,
                    Jsonb(command.evidence),
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    context.subject.id,
                    "manual_task.create",
                    "manual_task",
                    task_id,
                    context.correlation_id,
                    command.reason_code,
                    # **Keys, not the evidence itself** (ADR-0055 §二, the rule
                    # B3 #25 applied to slot values). `evidence` is where a
                    # person writes what the customer said; `audit_events` is
                    # append-only, so a copy taken here can never be withdrawn.
                    # Who filed it, on which conversation, for whom and by when
                    # is what makes the act reviewable.
                    Jsonb(
                        {
                            "conversation_id": str(conversation_id),
                            "owner_employee_id": str(command.owner_employee_id),
                            "sla_due_at": command.sla_due_at.isoformat(),
                            "evidence_keys": sorted(command.evidence),
                        }
                    ),
                ),
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return command_receipt(
            resource=resource_ref(
                resource_type="manual_task",
                resource_id=str(task_id),
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict()

    @app.get("/conversations/{conversation_id}/manual-tasks")
    def list_manual_tasks(
        conversation_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #15 (`C0B-024`): one state machine, not a second one.

        **Every task on this conversation, whoever filed it.** The engine's
        `manual_review` hook and this batch's #16 write the same table by
        design (K18), so a reader that showed only one of them would be showing
        a filtered backlog while looking like the whole one. The freeze note
        (「复用人工任务状态机，不建第二套」) is about exactly this: `status` here
        is the same five values the engine's rows carry.

        **Terminal tasks stay in the list.** A resolved task is the record that
        somebody did the work, and dropping it would make "nothing to do" and
        "everything was done" the same answer. `status` is what a client filters
        on; this route does not decide for them.

        **`evidence` is returned, recursively redacted** -- the same treatment
        `GET /conversations/{id}/messages` gives an envelope, for the same
        reason: it is free-form, it carries whatever the person filing the task
        wrote down, and a field allow-list over free-form JSON goes stale by
        construction. Returning it is also what keeps `evidence` from being a
        column with no reader (ADR-0061's C14 shape) -- and it is why
        `manual_task.list` is excluded from `viewer`.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="manual_task.list",
                resource=tenant_resource_ref(
                    resource_type="manual_task",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
                    (tenant_id, conversation_id),
                ).fetchone()
                is None
            ):
                # The same 404 the write side gives, so a conversation that is
                # not this tenant's reads the same from both.
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            rows = connection.execute(
                """
                SELECT id, status, task_class, notify, reason_code, owner_employee_id,
                       sla_due_at, expires_at, evidence, created_at, claimed_at, resolved_at
                  FROM manual_tasks
                 WHERE tenant_id = %s AND conversation_id = %s
                 ORDER BY created_at, id
                 LIMIT %s
                """,
                (tenant_id, conversation_id, MAX_PAGE_ITEMS + 1),
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        return _page_or_refuse(
            [_manual_task_projection(row) for row in rows[:MAX_PAGE_ITEMS]],
            has_more=len(rows) > MAX_PAGE_ITEMS,
        )

    @app.post("/manual-tasks/{task_id}/claim", status_code=200)
    def claim_manual_task(
        task_id: UUID,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #17 (`C0B-026` 拆): somebody is on it now.

        **`claim` is one of the seventeen primitives**, so this is a verb on a
        noun and not a command resource -- the road K1 takes when the catalog can
        already say what happened. `scheduled_event.claim` is the other user of
        the verb and means the same thing: this worker has taken it.

        **A task that already has an owner may be claimed only by that owner.**
        B3 #16 assigns an owner at filing, so a person-filed task is born `open`
        *and* owned -- "claim" is then that person saying they have started.
        Letting anyone else claim it would be a reassignment with no record that
        one happened, which is what B3 #19 exists to write down.

        An engine-filed task has no owner (034 §T91), so the first person to
        claim it becomes the owner. That is the case this route was drawn for.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="manual_task.claim",
                resource=tenant_resource_ref(
                    resource_type="manual_task", tenant_id=tenant_id, resource_id=task_id
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            task = _locked_manual_task(connection, tenant_id=tenant_id, task_id=task_id)
            if task["status"] != "open":
                raise HTTPException(status_code=409, detail="MANUAL_TASK_NOT_OPEN")
            owner = task["owner_employee_id"]
            if owner is not None and UUID(str(owner)) != UUID(str(context.subject.id)):
                raise HTTPException(status_code=409, detail="MANUAL_TASK_BELONGS_TO_SOMEBODY_ELSE")
            connection.execute(
                """
                UPDATE manual_tasks
                   SET status = 'claimed', owner_employee_id = %s,
                       claimed_at = now(), updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (context.subject.id, tenant_id, task_id),
            )
            _audit_manual_task_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="manual_task.claim",
                resource_type="manual_task",
                task_id=task_id,
                reason_code="MANUAL_TASK_CLAIMED",
                evidence={"previous_status": task["status"]},
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return _manual_task_receipt(
            context=context,
            resource_type="manual_task",
            task_id=task_id,
            idempotency_key=idempotency_key,
            status="claimed",
        )

    @app.post("/manual-tasks/{task_id}/resolve", status_code=200)
    def resolve_manual_task(
        task_id: UUID,
        command: ResolveManualTaskRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #18 (`C0B-026` 拆): the work is done.

        **A command resource, by K1's criterion.** `resolve` is not one of the
        seventeen primitives and the set stays closed (ADR-0043 §二.1), so the
        question is the one K1 asks: does this permission need granting on its
        own? It does. "May take work" and "may declare work finished" are
        different trusts -- the second closes the record, and a backlog where
        anyone who can pick a task up can also mark it done reports whatever
        people find convenient. So `manual_task_resolution.create`, the shape
        `conversation_takeover.create` already uses.

        **Resolvable from `open` as well as `claimed`.** Requiring a claim first
        would make a two-minute fix a two-call ceremony, and the owner is
        recorded either way -- by the claim, or by B3 #16's filing.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="manual_task_resolution.create",
                resource=tenant_resource_ref(
                    resource_type="manual_task_resolution",
                    tenant_id=tenant_id,
                    resource_id=task_id,
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            task = _locked_manual_task(connection, tenant_id=tenant_id, task_id=task_id)
            if task["status"] not in {"open", "claimed"}:
                raise HTTPException(status_code=409, detail="MANUAL_TASK_ALREADY_CLOSED")
            connection.execute(
                """
                UPDATE manual_tasks
                   SET status = 'resolved', resolved_at = now(), updated_at = now(),
                       evidence = evidence || %s
                 WHERE tenant_id = %s AND id = %s
                """,
                (Jsonb({"resolution": command.note}), tenant_id, task_id),
            )
            _audit_manual_task_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="manual_task_resolution.create",
                resource_type="manual_task_resolution",
                task_id=task_id,
                reason_code="MANUAL_TASK_RESOLVED",
                # **The note is not copied here**, for the reason B3 #16's audit
                # row gives: it is free text written while looking at a
                # conversation, and `audit_events` cannot be edited afterwards.
                # It lives on the task, where a correction is possible.
                evidence={"previous_status": task["status"]},
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return _manual_task_receipt(
            context=context,
            resource_type="manual_task_resolution",
            task_id=task_id,
            idempotency_key=idempotency_key,
            status="resolved",
        )

    @app.post("/manual-tasks/{task_id}/reassign", status_code=200)
    def reassign_manual_task(
        task_id: UUID,
        command: ReassignManualTaskRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #19 (`C0B-026` 拆): somebody else takes it.

        **A command resource, and the one this batch is most confident about.**
        Moving work off one named person onto another is a supervisory act --
        the criterion K1 asks about is exactly whether a different role needs it,
        and the answer here is the clearest of the four. It is deliberately
        **not** in `operator`: an agent who can take a task and finish it does
        not thereby get to hand other people's work around.

        **A reassigned task goes back to `open`.** The new owner has not claimed
        it -- they may not even know yet -- and `claimed` with somebody else's
        `claimed_at` would be a lie the `manual_tasks_claimed_pair` constraint
        would happily store. So the claim is cleared with the owner.

        Only the owner moves. The deadline does not: an SLA that follows the
        reassignment is an SLA that can be reset by handing work in a circle,
        which is why `036` grants `owner_employee_id` and withholds `sla_due_at`.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="manual_task_reassignment.create",
                resource=tenant_resource_ref(
                    resource_type="manual_task_reassignment",
                    tenant_id=tenant_id,
                    resource_id=task_id,
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            task = _locked_manual_task(connection, tenant_id=tenant_id, task_id=task_id)
            if task["status"] not in {"open", "claimed"}:
                raise HTTPException(status_code=409, detail="MANUAL_TASK_ALREADY_CLOSED")
            if (
                connection.execute(
                    "SELECT 1 FROM employees WHERE tenant_id = %s AND id = %s",
                    (tenant_id, command.owner_employee_id),
                ).fetchone()
                is None
            ):
                raise HTTPException(status_code=404, detail="OWNER_NOT_FOUND")
            connection.execute(
                """
                UPDATE manual_tasks
                   SET owner_employee_id = %s, status = 'open',
                       claimed_at = NULL, updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (command.owner_employee_id, tenant_id, task_id),
            )
            _audit_manual_task_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="manual_task_reassignment.create",
                resource_type="manual_task_reassignment",
                task_id=task_id,
                reason_code="MANUAL_TASK_REASSIGNED",
                evidence={
                    # Both ends, because the fact being recorded is a transfer.
                    # These are employee ids, not names: the same discipline
                    # `POST /employees/{id}/purge` asks of its reason string.
                    "previous_owner_employee_id": (
                        str(task["owner_employee_id"])
                        if task["owner_employee_id"] is not None
                        else None
                    ),
                    "owner_employee_id": str(command.owner_employee_id),
                    "reason": command.reason,
                    "previous_status": task["status"],
                },
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return _manual_task_receipt(
            context=context,
            resource_type="manual_task_reassignment",
            task_id=task_id,
            idempotency_key=idempotency_key,
            status="open",
        )

    @app.post("/manual-tasks/{task_id}/cancel", status_code=200)
    def cancel_manual_task(
        task_id: UUID,
        command: CancelManualTaskRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #20 (`C0B-027`): 「审计事实不物理删除」.

        **`cancel` is a primitive, so this is a verb on the noun** -- and the
        freeze's own note is why the verb exists rather than `DELETE`: the row is
        the record that somebody once had to do this, and deleting it deletes the
        history along with the work.

        **Not in `operator`.** Cancelling is deciding that work will not be done
        -- including, when the engine filed the task, work the system decided was
        necessary. That is the same shape as reassignment: a decision about
        somebody else's queue.

        `reason` is required, and it is the whole value of keeping the row.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="manual_task.cancel",
                resource=tenant_resource_ref(
                    resource_type="manual_task", tenant_id=tenant_id, resource_id=task_id
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            task = _locked_manual_task(connection, tenant_id=tenant_id, task_id=task_id)
            if task["status"] not in {"open", "claimed"}:
                raise HTTPException(status_code=409, detail="MANUAL_TASK_ALREADY_CLOSED")
            connection.execute(
                """
                UPDATE manual_tasks
                   SET status = 'cancelled', updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, task_id),
            )
            _audit_manual_task_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="manual_task.cancel",
                resource_type="manual_task",
                task_id=task_id,
                reason_code="MANUAL_TASK_CANCELLED",
                evidence={"reason": command.reason, "previous_status": task["status"]},
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
        return _manual_task_receipt(
            context=context,
            resource_type="manual_task",
            task_id=task_id,
            idempotency_key=idempotency_key,
            status="cancelled",
        )

    @app.post(
        "/conversations/{conversation_id}/read",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("POST", "/conversations/{conversation_id}/read"),
    )
    def mark_conversation_read(
        conversation_id: UUID,
        command: MarkConversationReadRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #5 (`C0B-006`): 按 employee read cursor 幂等，不改业务 message.

        **The cursor belongs to the caller, and the route offers no way to say
        otherwise.** There is no `employee_id` in the path or the body: marking
        somebody else's conversation read would falsify the one thing this table
        records. That is also why the action is not the platform's or a
        supervisor's -- it is an act on your own state.

        **A noun with `update`, not a command resource** (ADR-0048 K1). The
        criterion is whether the permission needs granting on its own, and this
        one does not need granting *away* from anybody who works conversations:
        an operator who can read a conversation and take it over can obviously
        record that they have read it. So `conversation_read_cursor.update`,
        and `ACTION_VERBS` stays closed. **No `shares_action_with`**: the second
        class of K1 costs `control-plane-parity.md:180`'s "different action"
        when two routes share one, and this action has exactly one route.

        **`viewer` does not get it, and that is a real consequence.** A viewer
        may read conversations and cannot clear their own unread count, because
        `viewer` is reads-only by construction and this is a write. If that
        turns out to be the wrong trade the fix is a decision about the role,
        not a special case here.

        **The message must be one this conversation actually shows.** The id is
        resolved against the same union `GET /conversations/{id}/messages` pages
        over -- inbound customer messages, and outbound rows whose `audience` is
        the customer. A manager notification is in `outbox_messages` and is not
        in the transcript, so it cannot be a place somebody "read up to"; an id
        from another conversation is not found at all.

        **No audit row, and that is a decision** (independent review 2026-09-06,
        P3-3). Every other write route in this cluster -- takeover, resume, slot
        write, control policy, the four manual-task verbs -- writes one, and
        `authorize()` attaches an `audit` obligation to *every* allow. This route
        does not discharge it, for two reasons: the act changes nothing anybody
        else can observe (one person's own reading position), and it happens at
        the frequency of scrolling, so an audit row per call would bury the rows
        that record decisions under rows that record attention.

        **The obligation therefore goes unfulfilled here, which is T49** -- the
        registered, explicitly accepted gap that `authorize()` cannot force a
        route to write what it promised. This route is an instance of it rather
        than an oversight, and if T49 is ever closed by making the obligation
        binding, this is the call site that has to answer for itself first.

        **Monotonic: the cursor never moves backwards.** Re-sending the same id
        writes nothing and answers the same way -- which is the idempotency the
        freeze asks for, and the primary key is what provides it. An *older* id
        is also refused movement rather than rejected: two agents on the same
        account, or a retried request that overtook a newer one, must not be
        able to make a conversation unread again.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="conversation_read_cursor.update",
                resource=tenant_resource_ref(
                    resource_type="conversation_read_cursor",
                    tenant_id=tenant_id,
                    resource_id=conversation_id,
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
                    (tenant_id, conversation_id),
                ).fetchone()
                is None
            ):
                raise HTTPException(status_code=404, detail="CONVERSATION_NOT_FOUND")
            marker = connection.execute(
                _VISIBLE_MESSAGE + " AND id = %(message)s",
                {
                    "tenant": tenant_id,
                    "conversation": conversation_id,
                    "message": command.last_read_ref,
                },
            ).fetchone()
            if marker is None:
                raise HTTPException(status_code=404, detail="MESSAGE_NOT_FOUND")

            connection.execute(
                """
                INSERT INTO employee_read_cursors(
                    tenant_id, employee_id, conversation_id,
                    last_read_at, last_read_source, last_read_ordinal, last_read_ref
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, employee_id, conversation_id) DO UPDATE
                   SET last_read_at = EXCLUDED.last_read_at,
                       last_read_source = EXCLUDED.last_read_source,
                       last_read_ordinal = EXCLUDED.last_read_ordinal,
                       last_read_ref = EXCLUDED.last_read_ref,
                       updated_at = now()
                 -- **四分量，`ordinal` 插在中间**（T369 / ADR-0140）。
                 -- 这条 `WHERE` 是「游标只许前进」那一半 —— 它用的序必须与
                 -- 逐字记录页的 `ORDER BY` 一致，否则「更新的那个」在两处
                 -- 的判定会不同：一轮里第二条消息在这里会被判成不比第一条新。
                 WHERE (
                           EXCLUDED.last_read_at, EXCLUDED.last_read_source,
                           EXCLUDED.last_read_ordinal, EXCLUDED.last_read_ref
                       ) > (
                           employee_read_cursors.last_read_at,
                           employee_read_cursors.last_read_source,
                           employee_read_cursors.last_read_ordinal,
                           employee_read_cursors.last_read_ref
                       )
                """,
                (
                    tenant_id,
                    context.subject.id,
                    conversation_id,
                    marker["created_at"],
                    # Derived from where the id was found, not asked of the
                    # caller: the union already knows which table answered.
                    marker["source_table"],
                    # **同样是从命中的那一行派生，不问调用方**（T369 / ADR-0140）：
                    # `_VISIBLE_MESSAGE` 现在带这一列，所以它跟 `source_table`
                    # 一样是查出来的，客户端无从伪造。
                    marker["ordinal"],
                    command.last_read_ref,
                ),
            )
            # **Read back rather than assume**, because the `WHERE` above may
            # have declined the move: what the caller needs to know is where the
            # cursor *is*, not where this call tried to put it.
            cursor = connection.execute(
                """
                SELECT last_read_at, last_read_source, last_read_ordinal,
                       last_read_ref
                  FROM employee_read_cursors
                 WHERE tenant_id = %s AND employee_id = %s AND conversation_id = %s
                """,
                (tenant_id, context.subject.id, conversation_id),
            ).fetchone()
            assert cursor is not None  # noqa: S101 -- upserted immediately above
            # **Unread means the customer's messages, not the whole timeline**
            # (independent review 2026-09-06, P3-2). The first version counted
            # every visible row after the cursor, so an agent's own reply made
            # their own unread count go up until they marked read again. The
            # count is what a conversation list sorts a person's day by, and
            # "things said to me that I have not read" is the only reading that
            # survives that use.
            #
            # **Counted in the database** (P3-4): the first version fetched the
            # rows and took `len()`, which drags a long conversation's tail into
            # the api process to produce one integer.
            unread = connection.execute(
                "SELECT count(*) AS total FROM ("  # noqa: S608
                + _VISIBLE_MESSAGE
                + " AND source_table = 'inbox_events'"
                # **四分量，与逐字记录页同一个序**（T369 / ADR-0140）。
                # ⚠️ 今天这个数**不会因此改变**：这一句只数 `inbox_events` 的行，
                # 而那一侧的 `ordinal` 是恒为 0 的占位。写成一致的形状是为了
                # 「页、游标、未读三者不许对同一个序有两种说法」——
                # 本文件自己的承诺（见 `_VISIBLE_MESSAGE` 上面那段）。
                + " AND (created_at, source_table, ordinal, id)"
                + " > (%(at)s::timestamptz, %(source)s::text,"
                + "    %(ordinal)s::integer, %(ref)s::uuid)"
                + ") AS unread",
                {
                    "tenant": tenant_id,
                    "conversation": conversation_id,
                    "at": cursor["last_read_at"],
                    "source": cursor["last_read_source"],
                    "ordinal": cursor["last_read_ordinal"],
                    "ref": cursor["last_read_ref"],
                },
            ).fetchone()
            assert unread is not None  # noqa: S101 -- `count(*)` always returns a row

        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="conversation_read_cursor",
                    resource_id=str(conversation_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            # **The cursor gets a reader in the same batch it gets a writer.**
            # `GET /conversations` (freeze B3 #1) is where an unread count
            # belongs and it is not built (T173); shipping the write alone would
            # leave a table nothing reads -- the shape registered twice in one
            # day as T175 and T177. So the answer carries the count derived from
            # the cursor that was just written, which is also the only number a
            # caller cannot compute for themselves.
            "result": {
                "last_read_at": _iso_timestamp(cursor["last_read_at"]),
                "last_read_source": cursor["last_read_source"],
                # **第三个分量也回给调用方**（T369 / ADR-0140）：
                # 上面那三个是「游标此刻在哪」，而少报一个分量就等于
                # 报了一个**不足以定位那一行**的位置 —— 一轮两条消息时
                # 调用方拿这三个字段拼不回同一个点。
                "last_read_ordinal": int(cursor["last_read_ordinal"]),
                "last_read_ref": str(cursor["last_read_ref"]),
                # Named for what it counts, so a client cannot read it as
                # "everything after the cursor" (P3-2).
                "unread_inbound_remaining": int(unread["total"]),
            },
        }

    @app.get(
        "/conversations",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("GET", "/conversations"),
    )
    def list_conversations(
        response: Response,
        cursor: str | None = None,
        control_mode: str | None = None,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #1 (`C0B-001` + `029`): 读模型字段见 parity §6.2 第一条.

        **All nine fields, and every one of them has a real source now.** T173
        registered this route as blocked on three of them, and the three landed
        in the batches since: `contacts` (B3 #21-#23) for the contact summary,
        `employee_read_cursors` (B3 #5) for the unread count, `manual_tasks`
        (K18) for the task count. **This route is what closes T173** -- and the
        reason it was worth waiting for is on the other side of that registration:
        every one of those three would otherwise have been an empty column, and
        「查过了，没有」 would have been indistinguishable from 「从来没有数据源」.

        | §6.2 | field | source |
        |---|---|---|
        | conversation id | `conversation_id` | the aggregate |
        | contact 摘要 | `contact` | `contacts` via `contact_id` (040) |
        | 最后消息摘要/时间 | `last_message` | the same union the message page reads |
        | 未读数 | `unread_inbound` | `employee_read_cursors`, **this caller's** |
        | 当前控制方 | `control_mode` | the aggregate |
        | 目标摘要 | `target` | the active frame of `target_stack` |
        | 下一主动事件摘要 | `next_event` | `scheduled_events`, earliest pending |
        | 人工任务数 | `open_manual_tasks` | `manual_tasks`, open or claimed |
        | projection version | `projection_version` | `state_version` |

        **The unread count is per caller, and that makes this list personal.**
        Two operators looking at the same tenant see the same conversations in
        the same order with different numbers -- which is the point of a read
        cursor per person (B3 #5) and the reason `no-store, private` is on the
        response.

        **`conversation_aggregate.list` is not a `viewer`'s.** The last-message summary is
        redacted customer text, which is the first bullet of the exclusion
        criterion word for word -- the same sentence that keeps `message.list`
        and `contact.list` out.

        **Ordered by the last message, not by `updated_at`.** The aggregate's
        timestamp moves whenever the engine writes anything -- a slot, a
        generation bump -- so ordering on it would float conversations nobody
        has spoken in to the top of somebody's queue.

        **The empty conversation sorts last rather than being hidden.** A
        conversation with no visible message yet (created by an event that was
        not a customer message) has `last_message: null`, and `null` sorts after
        every timestamp in this ordering. Hiding it would make "my list" and
        "the conversations that exist" two different sets without saying so.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="conversation_aggregate.list",
                resource=tenant_resource_ref(
                    resource_type="conversation_aggregate", tenant_id=tenant_id
                ),
            )
        )
        if control_mode is not None and control_mode not in {"ai", "human"}:
            # The only filter, and bounded by the same two values the column
            # accepts -- an operator wants "who is waiting on a person", and
            # anything else here would be a filter language over customer data.
            raise HTTPException(status_code=422, detail="UNKNOWN_CONTROL_MODE")
        after = _decode_keyset_cursor(cursor) if cursor else None
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                _CONVERSATION_LIST,
                {
                    "tenant": tenant_id,
                    "employee": context.subject.id,
                    "control_mode": control_mode,
                    "after": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": MAX_PAGE_ITEMS + 1,
                },
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        visible = rows[:MAX_PAGE_ITEMS]
        next_cursor = None
        if len(rows) > MAX_PAGE_ITEMS and visible:
            # **The second half of P1-1.** This used to require the last row
            # of the page to carry a message; a page ending on a silent
            # conversation therefore answered `next_cursor: null` -- and
            # `Page` derives `has_more` from that, so the client was told the
            # list had ended while rows remained. The cursor is issued
            # whenever a page was filled, and the silent rows carry
            # `-infinity` in the key rather than nothing.
            next_cursor = _encode_keyset_cursor(
                visible[-1]["last_message_at"] or _BEFORE_EVERY_MESSAGE,
                visible[-1]["id"],
            )
        return page(
            items=[_conversation_summary(row) for row in visible],
            next_cursor=next_cursor,
        ).as_dict()

    @app.get(
        "/contacts",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("GET", "/contacts"),
    )
    def list_contacts(
        response: Response,
        cursor: str | None = None,
        config_id: str | None = None,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #21 (`C0B-033`): cursor、bounded filters、敏感字段 permission.

        **The sensitive field is behind the action, not behind a second one**
        (user ruling 2026-09-06). The freeze note asks for a field-level
        permission gate, and this repository has only ever had action-level
        ones. Today `profile_data` is empty for every contact -- M1a calls no
        channel profile API -- so the only field carrying anything is
        `external_user_id`, and a "base tier" without it shows an internal uuid
        and two counters: a row that cannot tell you who anybody is. The split
        is therefore registered (T179) with its due date written down -- when
        `profile_data` has a producer -- rather than built as a mechanism whose
        cheap tier is useless. `contact.list` is out of `viewer` in the
        meantime, on the criterion that already keeps `message.list` out.

        **`config_id` is the only filter, and it is bounded by being an
        equality.** The freeze says 「bounded filters」; an open filter language
        over a customer table is a way to ask "who wrote in about X" without
        anybody granting that, and none of the fields here are worth searching
        on yet -- a contact carries no name.

        **Ordered by last activity, which is the order this list is for**: who
        wrote recently is what somebody picking up a queue needs, and
        `039`'s index is built for exactly this ordering -- `last_seen_at`
        descending, `id` ascending to break ties. The two agreed only after
        the independent review measured them (P2-3): the first version broke
        ties `id DESC` and paid an Incremental Sort per page while saying it
        did not.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="contact.list",
                resource=tenant_resource_ref(resource_type="contact", tenant_id=tenant_id),
            )
        )
        after = _decode_keyset_cursor(cursor) if cursor else None
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT id, config_id, external_user_id, profile_data,
                       first_seen_at, last_seen_at, message_count
                  FROM contacts
                 WHERE tenant_id = %(tenant)s
                   AND (%(config)s::text IS NULL OR config_id = %(config)s)
                   AND (
                       %(after)s::timestamptz IS NULL
                       OR last_seen_at < %(after)s::timestamptz
                       OR (last_seen_at = %(after)s::timestamptz
                           AND id > %(after_id)s::uuid)
                   )
                 -- **The tie-break matches the index** (independent review
                 -- 2026-09-06, P2-3). `039` indexes
                 -- `(tenant_id, last_seen_at DESC, id)` -- `id` ascending --
                 -- and this used to say `id DESC`, which cost an Incremental
                 -- Sort on every page while three places claimed the two
                 -- agreed "by construction". The keyset predicate above is
                 -- spelled out rather than a row comparison because the two
                 -- columns now run in opposite directions.
                 ORDER BY last_seen_at DESC, id
                 LIMIT %(limit)s
                """,
                {
                    "tenant": tenant_id,
                    "config": config_id,
                    "after": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": MAX_PAGE_ITEMS + 1,
                },
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        visible = rows[:MAX_PAGE_ITEMS]
        next_cursor = (
            _encode_keyset_cursor(visible[-1]["last_seen_at"], visible[-1]["id"])
            if len(rows) > MAX_PAGE_ITEMS and visible
            else None
        )
        return page(
            items=[_contact_projection(row) for row in visible],
            next_cursor=next_cursor,
        ).as_dict()

    @app.get(
        "/contacts/{contact_id}",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("GET", "/contacts/{contact_id}"),
    )
    def read_contact(
        contact_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #22 (`C0B-031`): `profile_data` typed/allowlisted.

        **The allowlist is the database's** (`039`): a `CHECK` refuses any key
        but `display_name` and `avatar_url`, so this route does not filter the
        document on the way out -- it cannot contain anything else. K11 chose
        those two and refused to fix more now, because 「将来销冠语料会带来更多
        字段需求，现在定死会返工」.

        **It is empty today, and that is an answer rather than a placeholder.**
        Nothing in M1a can fill it: there is no channel profile call. A reader
        who sees `{}` is being told the platform holds no profile fields for
        this person, which is true.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="contact.read",
                resource=tenant_resource_ref(
                    resource_type="contact", tenant_id=tenant_id, resource_id=contact_id
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT id, config_id, external_user_id, profile_data,
                       first_seen_at, last_seen_at, message_count
                  FROM contacts WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, contact_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="CONTACT_NOT_FOUND")
        response.headers["cache-control"] = "no-store, private"
        return _contact_projection(row)

    @app.get(
        "/contacts/by-conversation/{conversation_id}",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 这一族的形状由 `tests/contracts/test_projection_shapes.py` 钉住 ——
        # 它调产出方并**递归**比到嵌套层，AST 那条只比顶层。
        responses=openapi_responses("GET", "/contacts/by-conversation/{conversation_id}"),
    )
    def read_contact_by_conversation(
        conversation_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #23 (`C0B-032` + `058`): tenant 组合外键一致.

        **One join on a stored column** (migration 040, user ruling 2026-09-06).
        The alternative was to work the contact out at read time from
        `inbox_events` -- the conversation's first customer message names the
        identity `039` keyed on -- which needs no column and costs a scan every
        time. `GET /conversations` would pay it once per row in the list, and
        that read model is what T173 is waiting for.

        **404 has two causes here and they are deliberately the same answer**:
        the conversation is not this tenant's (RLS), or it has no contact
        recorded -- because its first message carried no identity, or because it
        predates `039` and there is no backfill. A client cannot tell them
        apart, and neither can anybody else without asking `created_at`; making
        the distinction visible would be inventing a fact.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="contact.read",
                # **No `resource_id`** (independent review P3-4). This used
                # to pass the *conversation* id as the id of a `contact` --
                # unreachable today, since `authorize()` reads the type and
                # the tenant, but it is the field a future "authorize per
                # resource" would believe. Looking a contact up by
                # conversation does not know which contact it is yet, which
                # is the honest reason there is nothing to name here, and it
                # matches how the two list routes authorize.
                resource=tenant_resource_ref(
                    resource_type="contact", tenant_id=tenant_id
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT c.id, c.config_id, c.external_user_id, c.profile_data,
                       c.first_seen_at, c.last_seen_at, c.message_count
                  FROM conversation_aggregates a
                  JOIN contacts c
                    ON c.tenant_id = a.tenant_id AND c.id = a.contact_id
                 WHERE a.tenant_id = %s AND a.id = %s
                """,
                (tenant_id, conversation_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="CONTACT_NOT_FOUND")
        response.headers["cache-control"] = "no-store, private"
        return _contact_projection(row)

    # ------------------------------------------------------------------ 资料
    #
    # **E3-6 的三条**（freeze B4 #2 / #5 / #6）。另外两条（#3 上传票据、#4 收货
    # 确认）**按 ADR-0130 裁定一不做**：第一版资料人工投放，没有字节入站的路。
    # 所以这三条里**没有一条能写出一行 `materials`** —— 行由人手工插入，
    # 规格在 `docs/plans/material-placement-spec-2026-09-12.md`。

    def _material_projection(row: Any) -> dict[str, object]:
        """一份资料对外的样子。**不含载体，因为表里没有载体**（`053` 的表头写明了）。"""

        return {
            "id": str(row["id"]),
            "reference": f"asset:{row['slug']}:v{row['version']}",
            "slug": row["slug"],
            "version": row["version"],
            "filename": row["filename"],
            "media_type": row["media_type"],
            "byte_size": row["byte_size"],
            "content_hash": row["content_hash"],
            "status": row["status"],
            "archived_at": _iso_timestamp_or_none(row["archived_at"]),
            "created_at": _iso_timestamp(row["created_at"]),
        }

    def _declared_asset_slugs(connection: Any, tenant_id: Any) -> dict[str, list[str]]:
        """**在架 profile 声明了哪些资料 slug**，slug → 声明它的 release 列表。

        ## 键是 slug，**版本段被丢掉**（独立审查 2026-09-12，P1-1）

        本批第一版拿**整条带版本的 ref** 当键，而图纸三处逐字写死的是另一种读法：
        `control-plane-m1a-route-freeze.md:160`（B4 #5，正是本批交的那条）
        「**profile 声明 slug 不声明版本，发送时解析当前那一版**」（ADR-0130 §二.2），
        `references.py` 说得更死——「**这一段今天在 profile 里写 `v1` 只是语法占位，
        读它的人要知道真正决定发哪一版的不是它**」。

        **按整条比会漏**：profile 里写着 `asset:price-list:v1`，商家放进了 v2，
        归档 v2 时算出的字符串是 `asset:price-list:v2`，profile 里没有它 → 放行。

        **用户 2026-09-12 裁定：按 slug 算。**

        ## 「在架」= 有绑定指着它 **∪** 有非 closed 会话 pin 着它（P1-2）

        第一版只算前一半，而这个仓自己承认后一半是常态：
        `profiles/binding.py` 的重绑有 `LIMIT`、有 `repin_live_conversations` 开关、
        **只按 `config_id` 选**，并显式返回 `remaining` 与 `unlinked` 两个计数——
        **一次重绑搬不走的那些会话，仍然 pin 在一份没有绑定指向的 release 上**。
        用户 2026-09-12 裁定：两半都算。

        > **`channel_bindings.status = 'disabled'` 的那些仍算在架**，这是有意的：
        > 停用只挡新入站，**旧会话可能还在跑**。方向是保守的那一侧。

        **为什么读整份 profile 而不是查一张索引表**：`content_assets` 是
        **帧级**声明（ADR-0130 / E3-2b），活在 `profile_releases.profile` 这个
        JSONB 文档里，仓里没有把它摊平成行的表。摊平要建表、要迁移、要回填。
        **成本边界：release 的条数有界，而这次调用的成本不由它决定**
        （独立复审 2026-09-12，R-2）。P1-2 把「有绑定指着它」扩成了并集之后，
        那个 `EXISTS` 要在 `conversation_aggregates` 上按
        `(tenant_id, profile_release_id)` 找——**而全仓没有这个索引**
        （`008` 建的是 `(tenant_id, config_id) WHERE config_id IS NOT NULL`），
        所以它扫的是这个租户的会话行，**随会话数增长，没有上限**。

        **而 `GET /materials/{material_id}` 每次单读都调它，且那条 route 在
        `VIEWER_SNAPSHOT` 里**——一个只读角色的一次普通读会触发这次扫描。
        **本批不建索引**（要改迁移，而支撑它的测量今天取不到），
        **登记 T313。** 到期条件当天改了两次——先按复审方倾向从 E3-7 提前到 E3-4，
        随后用户裁定**第一版不做文件发送**，**E3-4 与 E3-7 一并不做**，两个锚点同时没了。
        **现在它不挂任何 E3 批次**：本条要的测量是「一个租户有多少条会话」，
        与文件发送无关——**第一个真实租户跑起来、这张表有真实行数可量时**。
        那一批本来就要碰发送时的 `asset:` 解析，会读同一批「哪些 release 还在架」的事实。
        **与 `GET /materials` 的排序索引脱钩**——那一条等的是「有多少份资料」，
        **本条等的是「一个租户有多少条会话」，两个测量不是同一件事。**
        """

        rows = connection.execute(
            """
            SELECT DISTINCT r.id, r.profile
              FROM profile_releases r
             WHERE r.tenant_id = %s
               AND (
                   EXISTS (
                       SELECT 1 FROM channel_bindings b
                        WHERE b.tenant_id = r.tenant_id AND b.profile_release_id = r.id
                   )
                   OR EXISTS (
                       -- **在飞会话 pin 着的那一半**（P1-2）。`fastagent_app` 早在
                       -- `001_m0.sql` 就持有 `SELECT ON conversation_aggregates`，
                       -- 所以这一支**不需要改 `058`**。
                       SELECT 1 FROM conversation_aggregates a
                        WHERE a.tenant_id = r.tenant_id
                          AND a.profile_release_id = r.id
                          AND a.lifecycle <> 'closed'
                   )
               )
            """,
            (tenant_id,),
        ).fetchall()
        declared: dict[str, list[str]] = {}
        for row in rows:
            document = row["profile"] or {}
            for frame in document.get("target_frames", []) or []:
                for item in (frame or {}).get(CONTENT_ASSETS_KEY, []) or []:
                    reference = (item or {}).get("ref")
                    if not isinstance(reference, str):
                        continue
                    parsed = reference.split(":")
                    # `kind:slug:v<N>` —— 只取中间那一段。畸形的原样跳过：
                    # 封版校验已经在前面拦过，这里不替它下第二次判断。
                    if len(parsed) == 3 and parsed[0] == "asset" and parsed[1]:
                        releases = declared.setdefault(parsed[1], [])
                        if str(row["id"]) not in releases:
                            releases.append(str(row["id"]))
        return declared

    @app.get("/materials")
    def list_materials(
        response: Response,
        cursor: str | None = None,
        status: str | None = None,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #2：资料目录。

        **`status` 是唯一的过滤器，而它是一个等值**，与 `GET /contacts` 同一条
        理由：一门开放的过滤语言等于让人不经授权地问「你们卖什么」。

        **默认不过滤**，两种状态都返回。归档过的版本仍要看得见——
        「这一版从哪一刻起不再能发」是运维会问的问题，而答案就在行上
        （`archived_at`），把它从默认视图里藏掉只会让人以为它不存在。

        **排序按 `created_at DESC, id`，而这一条没有索引撑着**，与
        `GET /contacts` 不同（`039` 为它建了索引）。**说清楚而不是假装**：
        `053` 只有 `UNIQUE (tenant_id, slug, version)` 和主键。
        本批不加索引，**理由只有一条**：**支撑它的测量今天取不到**
        （没有任何真实租户有资料）。

        > 这里原本还写着「加索引要改迁移，那是全流程档」——**那半句是假的**
        > （独立审查 2026-09-12，P3-1）：本批已经是全流程档、也已经在改迁移
        > （`058`）。**一个假的理由会让下一个人以为这批没碰迁移**，
        > 而真正的理由本来就写在后半句。
        目录是人工投放、手工策展的集合，量级是几十。
        **到期：E3-7 真实租户验收时量一次**，那时再决定。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="material.list",
                resource=tenant_resource_ref(resource_type="material", tenant_id=tenant_id),
            )
        )
        if status is not None and status not in {"active", "archived"}:
            # **拒绝而不是当成「没有匹配」**：一个打错的过滤词返回空页，
            # 读的人会以为这个租户没有资料。
            raise HTTPException(status_code=400, detail="INVALID_MATERIAL_STATUS")
        after = _decode_keyset_cursor(cursor) if cursor else None
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT id, slug, version, filename, media_type, byte_size,
                       content_hash, status, archived_at, created_at
                  FROM materials
                 WHERE tenant_id = %(tenant)s
                   AND (%(status)s::text IS NULL OR status = %(status)s)
                   AND (
                       %(after)s::timestamptz IS NULL
                       OR created_at < %(after)s::timestamptz
                       OR (created_at = %(after)s::timestamptz
                           AND id > %(after_id)s::uuid)
                   )
                 ORDER BY created_at DESC, id
                 LIMIT %(limit)s
                """,
                {
                    "tenant": tenant_id,
                    "status": status,
                    "after": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": MAX_PAGE_ITEMS + 1,
                },
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        visible = rows[:MAX_PAGE_ITEMS]
        next_cursor = (
            _encode_keyset_cursor(visible[-1]["created_at"], visible[-1]["id"])
            if len(rows) > MAX_PAGE_ITEMS and visible
            else None
        )
        return page(
            items=[_material_projection(row) for row in visible],
            next_cursor=next_cursor,
        ).as_dict()

    @app.get("/materials/{material_id}")
    def read_material(
        material_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #5：一份资料。

        **`material.read` 与 `material.list` 是两个 action，不共享。**
        K1 的判据是「这个权限需不需要单独授予」，而这里需要：
        列表是商家的**整本目录**，单读是**一份文件的元数据**。
        一个能查出「那条回复引用的是哪一份资料」的操作员，
        不因此就是一个能枚举商家全部在卖什么的操作员。

        **返回里带 `reference`**（`asset:<slug>:v<N>`），因为那才是这份资料
        在 profile 与提案里被称呼的名字；`id` 是我们内部的主键，
        **拿着 `id` 去 profile 里搜是搜不到的**。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="material.read",
                resource=tenant_resource_ref(
                    resource_type="material", tenant_id=tenant_id, resource_id=material_id
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT id, slug, version, filename, media_type, byte_size,
                       content_hash, status, archived_at, created_at
                  FROM materials WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, material_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="MATERIAL_NOT_FOUND")
            declared = _declared_asset_slugs(connection, tenant_id)
        response.headers["cache-control"] = "no-store, private"
        projection = _material_projection(row)
        # **「还有谁在声明它」是这份资料的一个属性，所以它挂在读上。**
        #
        # 归档那条 route 会因为它而拒绝，**而拒绝里带不了这个清单**——
        # 错误信封只放行 `code`（见 `archive_material` 里的说明）。
        # 放在这里更有用：操作员在按下归档**之前**就看得到该去改哪一份 profile。
        #
        # **按 slug 查，与归档那道闸同一个键**（P1-1）。第一版按整条带版本的 ref 查，
        # 于是「profile 声明 v1、这一行是 v2」时返回空清单——
        # **操作员据此以为没人声明它**，而实际上发送时解析到的正是这个 slug。
        #
        # **只在单读上算，不在列表上算**：列表一页最多 200 条，
        # 每条都扫一遍在架 profile 是把一次扫描乘以 200。
        # 需要「整本目录里哪些还被声明着」的那天，那是一次聚合查询，不是这个形状。
        projection["declared_by_releases"] = sorted(declared.get(row["slug"], []))
        return projection

    @app.post("/materials/{material_id}/archive", status_code=200)
    def archive_material(
        material_id: UUID,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #6：下架一份资料。**带在架声明检查。**

        ## 仍被在架 profile 声明的资料，**拒绝归档**

        裁定五定了「引用在**发送时**解析」。于是归档一份仍被在架 profile 声明的
        资料，**在归档这一刻不会报任何错**——它会在下一次有人触发那一帧、
        闸去解析那条引用时才炸，**而那时炸的是一次真实对话**。
        **这道检查把那个缺口从运行时搬回到这一刻。**

        **拒绝而不是警告**（排期 §3.2 第 8 行，作者定，用户可推翻）：
        警告要有人看见才有用，而这条路径上没有人在等着看警告——
        它是一次 API 调用，返回 200 的那一刻操作员就去做下一件事了。

        **解法是先改 profile 再归档**，两步的顺序有意义：
        profile 改完发布之后，这份资料不再被任何在架 release 声明，
        归档随即可行。**反过来做会留下一个「声明指着一份发不出去的资料」的窗口。**

        ## 归档单向，第二次调用答 409

        `053` 的触发器写死了「归档过的行不能取消归档」「`archived_at` 只盖一次」。
        **第二次调用不是幂等地成功**，它是一次没有意义的写——
        与 `POST /channel-bindings/{id}/deactivate` 同一条理由：
        重试不得制造出一次新的下架。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        decision = authorize(
            context=context,
            action="material.invalidate",
            resource=tenant_resource_ref(
                resource_type="material", tenant_id=tenant_id, resource_id=material_id
            ),
        )
        require_allowed(decision)
        # **写路径要报名**（独立审查 2026-09-12，P3-3）：同族的写路径都传这两个
        # （`deactivate_channel_binding`、`profiles/binding.py`），而本批第一版只传
        # `tenant_id`。**今天没有任何触发器或策略读它们**——审查方 grep 过
        # `infrastructure/migrations/` 下的 `app.subject_id` / `app.correlation_id`，
        # 零命中——所以这不是缺陷，是**一致性**：
        # 下一个给某张表加「写入方必须报名」触发器的人，
        # 会发现这条路径是唯一不报名的那条。补两个参数比留一条例外便宜。
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                SELECT id, slug, version, filename, media_type, byte_size,
                       content_hash, status, archived_at, created_at
                  FROM materials WHERE tenant_id = %s AND id = %s FOR UPDATE
                """,
                (tenant_id, material_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="MATERIAL_NOT_FOUND")
            if row["status"] == "archived":
                raise HTTPException(status_code=409, detail="MATERIAL_ALREADY_ARCHIVED")

            reference = f"asset:{row['slug']}:v{row['version']}"
            declared_by = _declared_asset_slugs(connection, tenant_id).get(row["slug"], [])
            # **按 slug 判，而且只在「归档之后这个 slug 一个 active 都不剩」时拒**
            # （用户 2026-09-12 裁定）。
            #
            # **为什么不是「被声明的 slug 的任何版本都不许归档」**：那会堵死
            # 「发新版本之后下架旧版本」这条正常路径——而投放规格
            # （`material-placement-spec-2026-09-12.md` §六）把它写成了标准做法。
            #
            # **为什么这样就够**：发送时按 slug 解析当前那一版（ADR-0130 §二.2）。
            # 只要这个 slug 还剩至少一个 active 版本，那一帧就仍有东西可发；
            # **归档最后一个才是把引用打空**，也正是 ADR-0130 §二.3 要挡的那一格。
            #
            # **一个残留的产品后果，写出来而不是藏着**：归档一个非最后的版本，
            # 会静默改变「当前那一版」是哪一份——客户下次收到的是另一个文件。
            # 那是「发送时解析当前版本」这条裁定本身带来的，
            # **不是这道闸该挡的事**；要挡它得先定义「当前那一版」，而今天没有。
            still_active = connection.execute(
                """
                SELECT count(*) AS n FROM materials
                 WHERE tenant_id = %s AND slug = %s AND status = 'active' AND id <> %s
                """,
                (tenant_id, row["slug"], material_id),
            ).fetchone()
            if declared_by and int((still_active or {"n": 0})["n"]) == 0:
                # **只带码，不带释明**——这是错误信封的合同逼出来的形状，不是我选的。
                #
                # 本批第一版的 `detail` 是一个带 `reference` 与
                # `declared_by_releases` 的对象，想让操作员一眼看到该去改哪一份
                # profile。**`_http_error` 会把它整个丢掉**：结构化的 `detail`
                # 只保留 `code` 与 `field_errors` 两个成员，理由是
                # 「so that "detail" cannot become a place to attach arbitrary
                # internals」。**那条约束是对的，是我的设计越过了它**，
                # 而门禁在 `KeyError: 'detail'` 上把这件事顶了回来。
                #
                # **「谁在声明它」因此挪到了读那一面**（`GET /materials/{id}` 的
                # `declared_by_releases`）——那本来就是操作员在归档之前会看的地方，
                # 而一个属性挂在资源上，比只在失败时出现一次更有用。
                raise HTTPException(status_code=409, detail="MATERIAL_STILL_DECLARED")

            connection.execute(
                """
                UPDATE materials SET status = 'archived', archived_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'active'
                """,
                (tenant_id, material_id),
            )
            _audit_material_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="material.invalidate",
                material_id=material_id,
                reason_code="MATERIAL_ARCHIVED",
                evidence={
                    # **引用而不是 id**：审计行要能被拿去和 profile 对照，
                    # 而 profile 里写的是引用。
                    "reference": reference,
                    "filename": row["filename"],
                },
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
            archived = connection.execute(
                """
                SELECT id, slug, version, filename, media_type, byte_size,
                       content_hash, status, archived_at, created_at
                  FROM materials WHERE tenant_id = %s AND id = %s
                """,
                (tenant_id, material_id),
            ).fetchone()
        response.headers["cache-control"] = "no-store, private"
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="material",
                    resource_id=str(material_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": _material_projection(archived),
        }

    # ------------------------------------------------------------------ 记忆
    #
    # **E4-3 的四条**（freeze B4 #8 / #9 / #10 / #11），K21 对这四行到期。
    # 表是 `054` 建的，写入路径 E4-2 已经有一条（模型的 `remember`）；
    # **这四条是人的那一条**。
    #
    # **两条写入路径的分工就是裁定八**（ADR-0131 M3）：
    # 「摘要人工可编辑，模型不能自改」——模型只能追加（`057` 不授 `UPDATE`），
    # 人可以改（`060` 授给 `fastagent_app`）。**同一条裁定的两半，落在两个角色上。**

    def _memory_projection(
        row: Any,
        *,
        #: 这位联系人**当前绑定**的那份 profile 给哪几个字段声明了寿命。
        #: **没有默认值**：四条 route 共用本函数，而一个「忘了传就当没人声明」
        #: 的默认会让某一条 route 上的 `expired` 恒为 `false` ——
        #: 而那正是本字段要消灭的那种「两种含义长得一样」（T299）。
        lifetimes: Mapping[str, int],
    ) -> dict[str, object]:
        """一条记忆对外的样子。**两个来源字段都带上，因为它们是互斥的。**

        `source_event_id` 有值 = 模型从一条客户消息里记下的；
        `authorised_by` 有值 = 某个员工授权记的（freeze B4 #9 的 `consent`，
        按 ADR-0131 M1 读作「哪个员工」而不是「客户同意了」）。
        **`060` 的 CHECK 保证恰好一个有值**，所以读的人不必猜。

        ## `expired`：`status` 答不了的那一问（T299，2026-09-20）

        记忆过期是**读时**判的 —— 行照旧躺在表里、`status` 照旧是 `active`，
        变的只是引擎不再召回它（理由见 `runtime/memory_lifetime.py`：
        `ttl_days` 是商家当前配包的属性，而 tombstone 不可逆）。

        **于是屏幕上会出现一条 `active` 而模型从不使用的记忆** ——
        「这条还在用」与「它已经过期了」长得一模一样。
        **本仓为这个形状付过一次钱**：`manual_tasks.expires_at` 全仓零写者，
        而读模型照样把它显示出来，于是那一格永远是空的，
        「这个任务不过期」与「没人填过这一格」在屏幕上分不开
        （`test_manual_task_owner_sla_expiry.py` 把它钉成了一个事实）。
        **这一次不留那个洞。**

        ⚠️ **只有 `active` 的行才可能为 `true`**：`superseded` / `tombstoned`
        的行已经由 `status` 说清楚了它不在用，再叠一个 `expired` 是噪音 ——
        本字段只回答 `status` 答不了的那一问。

        ⚠️ **依据的是「当前绑定」那份 profile，不是会话 pin 的那一份** ——
        与 `_declared_memory_fields_for_contact` 同一条理由，逐字见它的 docstring：
        控制面问的是「这家商家**现在**要记什么」。
        **所以同一条记忆在控制面显示为已过期，而一条 pin 着旧 release 的
        在跑会话里可能仍在使用** —— 那不是偏差，是两个问题各自的正确答案。
        """

        expired = row["status"] == "active" and is_expired(
            field_key=str(row["field_key"]),
            created_at=row["created_at"],
            now=datetime.now(UTC),
            lifetimes=lifetimes,
        )
        return {
            "id": str(row["id"]),
            "contact_id": str(row["contact_id"]),
            "field_key": row["field_key"],
            "field_value": row["field_value"],
            "status": row["status"],
            "revision": row["revision"],
            "supersedes": str(row["supersedes"]) if row["supersedes"] else None,
            "source_event_id": (
                str(row["source_event_id"]) if row["source_event_id"] else None
            ),
            "authorised_by": str(row["authorised_by"]) if row["authorised_by"] else None,
            "authorisation_note": row["authorisation_note"],
            "created_at": _iso_timestamp(row["created_at"]),
            "tombstoned_at": _iso_timestamp_or_none(row["tombstoned_at"]),
            "expired": expired,
        }

    #: 四条 route 共用的列清单。**一处定义，因为四处写同一串列名时，
    #: 漏掉一个的那一处会在 `_memory_projection` 里变成 `KeyError`。**
    _MEMORY_COLUMNS = (
        "id, contact_id, field_key, field_value, status, revision, supersedes, "
        "source_event_id, authorised_by, authorisation_note, created_at, tombstoned_at"
    )

    def _contact_or_404(
        connection: Any, tenant_id: Any, contact_id: UUID, *, lock: bool = False
    ) -> Any:
        """这个联系人在不在，顺带把它的 `config_id` 取出来。

        **404 而不是空清单**：一个打错的 contact id 返回 `[]`，读的人会以为
        「我们对这位客户什么都不记得」——而那是一句关于客户的错话，不是关于 id 的。
        跨租户的 id 走同一个 404，因为 RLS 让它们一样地不存在。

        ## `lock=True`：**和模型那条写入路径取同一把锁**

        三条写 route 都传它，读那条不传。**第一版一条都不传，那是个缺陷**
        （作者自查，独立审查窗口开着的时候发现的）：

        `054` 的 `contact_memories_one_active_per_field_idx` 是一条
        **partial unique index**，`POST` 的占用检查与它之间有一个窗口。
        两个写入方同时落在同一个 `(contact, field_key)` 上，第二条撞索引，
        **而撞索引是 500 不是 409**。

        **更贵的是跨进程的那一半。** E4-2 已经为一模一样的竞态取了锁
        （`pipeline.py::_memory_gate`），代价写在它的 docstring 里。
        本 route 不取的话，**两个写入方各自安全而彼此不可见**——
        模型正在写一条 `ring_size`，员工同时手工写一条，
        撞索引的那一侧如果是 worker，后果不是一个 500：
        整轮回滚、inbox 事件不消费、退回重试预算里再炸一次
        （`_memory_gate` 的原话）。

        **所以键必须逐字相同**：`hashtext(tenant_id), hashtext(contact_id)`。
        换一把锁（比如 `contacts` 行上的 `FOR UPDATE`）两边就锁不到一起，
        **那种「取了锁但没锁住对面」的写法比不取更坏，因为它读起来像安全的**。

        > **为什么不锁 `contacts` 行**：入站适配器每收一条消息就 `UPDATE`
        > 那一行，而一次回合的事务里有模型调用。行锁会让控制面的一次写
        > 等在一次模型调用后面。咨询锁与行锁不冲突，
        > 且 `_memory_gate` 持有它的那一段在模型调用**之后**。

        **不成环**：worker 的顺序是「聚合行锁 → 咨询锁」，本 route 是
        「咨询锁 → `contact_memories` 行锁」，**而没有任何一方在持有咨询锁时
        去取聚合锁**，也没有任何一方在持有 `contact_memories` 行锁时取咨询锁。
        """

        row = connection.execute(
            "SELECT id, config_id FROM contacts WHERE tenant_id = %s AND id = %s",
            (tenant_id, contact_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="CONTACT_NOT_FOUND")
        if lock:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (str(tenant_id), str(contact_id)),
            )
        return row

    def _declared_memory_fields_for_contact(
        connection: Any, tenant_id: Any, config_id: str
    ) -> frozenset[str]:
        """这位联系人所在渠道**当前在架的那份 profile** 声明了哪些记忆字段。

        ## 为什么是「当前绑定的那一份」，而不是会话 pin 的那一份

        **worker 读的是会话 pin 住的 profile**（`prepared.profile`），
        本 route 读的是 `channel_bindings` 指向的那一份。**两者可以不同，
        而这不是一处需要对齐的偏差，是两个不同的问题各自的正确答案**：

        * 模型在一条**正在跑的会话**里记东西，那条会话 pin 着一份 release，
          它的行为必须随那一份走（ADR-0127 同族：会话不随发布漂移）；
        * 员工在控制面记东西，问的是「**这家商家现在要记什么**」——
          那是绑定说了算的。商家刚把 `ring_size` 从声明里去掉，
          员工就不该再手工添一条；而旧会话里的模型仍按旧声明办，是 pin 的应有之义。

        **所以差异的方向是收紧的那一侧**，写下来免得下一个人把它当 bug 修掉。

        ## 没有声明 = 一个都不能记

        与 `pipeline.py::_declared_memory_fields` 同一个关门方向，理由逐字相同：
        ADR-0131 §三 把「field allowlist」定义成「**存在一份封闭的、被强制的字段
        集合**」，而一份没声明 `memory_fields` 的 profile，它对「能记什么」的回答
        是「什么都不记」，**不是「没说所以随便」**。

        **`config_id` 来自 `contacts` 行**，不是调用方给的——`039` 的
        `FOREIGN KEY (tenant_id, config_id) REFERENCES channel_bindings` 保证它一定
        指得着一条绑定，所以这里取不到行只可能是绑定被删了，那是 500 该说的话，
        不是本函数该编一个答案。
        """

        row = connection.execute(
            """
            SELECT r.profile
              FROM channel_bindings b
              JOIN profile_releases r
                ON r.tenant_id = b.tenant_id AND r.id = b.profile_release_id
             WHERE b.tenant_id = %s AND b.config_id = %s
            """,
            (tenant_id, config_id),
        ).fetchone()
        if row is None:
            return frozenset()
        declared = (row["profile"] or {}).get(MEMORY_FIELDS_KEY) or []
        return frozenset(
            str((entry or {}).get("key")) for entry in declared if isinstance(entry, dict)
        )

    def _memory_lifetimes_for_contact(
        connection: Any, tenant_id: Any, config_id: str
    ) -> Mapping[str, int]:
        """这位联系人所在渠道**当前在架**的那份 profile，给哪几个字段声明了寿命。

        **与 `_declared_memory_fields_for_contact` 读的是同一份 profile**
        （同一条 `channel_bindings → profile_releases`），理由逐字相同，
        不在这里重复一遍 —— 去读那一个的 docstring。

        **解析那一步不在这里做**：`runtime/memory_lifetime.ttl_days_by_field`
        是全仓唯一一处读 `ttl_days` 的地方，引擎那两处也调它。
        **一个键两个解析点，就是本仓刚花一整批合并掉的那个形状**（T276）。
        """

        row = connection.execute(
            """
            SELECT r.profile
              FROM channel_bindings b
              JOIN profile_releases r
                ON r.tenant_id = b.tenant_id AND r.id = b.profile_release_id
             WHERE b.tenant_id = %s AND b.config_id = %s
            """,
            (tenant_id, config_id),
        ).fetchone()
        if row is None:
            return {}
        return ttl_days_by_field(row["profile"] or {})

    def _refuse_forbidden_memory_value(value: str) -> None:
        """禁止项那一层，按**取值**判（T298）。

        **闸本身在 `packages/runtime/src/.../memory_values.py`，两条写入路径共用。**
        E4-1b 的闸读的是商家给列取的名字（`id_card_no` 挡得住、`customer_extra_2`
        挡不住），它自己把这条缺口登记成 T298，到期条件写死「E4-2 / E4-3 落地时」。
        **E4-3 是它点名的最后一批**，所以在这里兑现，并同时补上模型那一条路径。

        **拒绝里不带取值**（`MEMORY_VALUE_FORBIDDEN` 只有码），
        与 `archive_material` 的 `MATERIAL_STILL_DECLARED` 同一条约束：
        `_http_error` 只放行 `code` 与 `field_errors`。**这里不是被合同逼的，
        是本来就该如此**——一条「拒绝记录身份证号」的错误信封若把那个号带出去，
        它就把号写进了日志。
        """

        if forbidden_value_reason(value) is not None:
            raise HTTPException(status_code=422, detail="MEMORY_VALUE_FORBIDDEN")

    @app.get("/contacts/{contact_id}/memories")
    def list_contact_memories(
        contact_id: UUID,
        response: Response,
        cursor: str | None = None,
        status: str | None = None,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #8：我们记得这位客户的哪些事。

        **默认三种状态都返回**，与 `GET /materials` 同一条理由，而这里更硬：
        B4 #11 要求「引用回答可解释」——**一条被作废的记忆必须仍然看得见**，
        否则「那句回复当时凭什么那么说」就没有地方可查。
        把 tombstoned 从默认视图里藏掉，等于把删除做成了真删。

        **`status` 是唯一的过滤器，而它是一个等值**，与 `GET /contacts` /
        `GET /materials` 同族：一门开放的过滤语言，等于让人不经授权地问
        「你们都记了这位客户的什么」——而这里的取值是客户的私事，不是资料元数据。

        **排序 `created_at DESC, id`，索引是 `(tenant_id, contact_id, status)`
        ——排序那一段没有索引撑着，而这一次上界是真的**：
        一位联系人的记忆条数 ≤ profile 声明的字段数（schema `maxItems: 32`）
        × 修订次数。**这与 `GET /materials` 那条「测量取不到」不是同一种情况**，
        所以这里不登记到期项：那边的上界不存在，这边的上界写在 schema 里。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="contact_memory.list",
                resource=tenant_resource_ref(
                    resource_type="contact_memory",
                    tenant_id=tenant_id,
                    resource_id=contact_id,
                ),
            )
        )
        if status is not None and status not in {"active", "superseded", "tombstoned"}:
            raise HTTPException(status_code=400, detail="INVALID_MEMORY_STATUS")
        after = _decode_keyset_cursor(cursor) if cursor else None
        with database.transaction(tenant_id=tenant_id) as connection:
            contact = _contact_or_404(connection, tenant_id, contact_id)
            lifetimes = _memory_lifetimes_for_contact(
                connection, tenant_id, contact["config_id"]
            )
            rows = connection.execute(
                f"""
                SELECT {_MEMORY_COLUMNS}
                  FROM contact_memories
                 WHERE tenant_id = %(tenant)s
                   AND contact_id = %(contact)s
                   AND (%(status)s::text IS NULL OR status = %(status)s)
                   AND (
                       %(after)s::timestamptz IS NULL
                       OR created_at < %(after)s::timestamptz
                       OR (created_at = %(after)s::timestamptz
                           AND id > %(after_id)s::uuid)
                   )
                 ORDER BY created_at DESC, id
                 LIMIT %(limit)s
                """,  # noqa: S608 - `_MEMORY_COLUMNS` is a module-local literal
                {
                    "tenant": tenant_id,
                    "contact": contact_id,
                    "status": status,
                    "after": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": MAX_PAGE_ITEMS + 1,
                },
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        visible = rows[:MAX_PAGE_ITEMS]
        next_cursor = (
            _encode_keyset_cursor(visible[-1]["created_at"], visible[-1]["id"])
            if len(rows) > MAX_PAGE_ITEMS and visible
            else None
        )
        return page(
            items=[_memory_projection(row, lifetimes=lifetimes) for row in visible],
            next_cursor=next_cursor,
        ).as_dict()

    @app.post("/contacts/{contact_id}/memories", status_code=201)
    def record_contact_memory(
        contact_id: UUID,
        command: RecordContactMemoryRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #9：一位员工手工记下一件事。

        **依据列的五项，各自落在哪里**（B4 #9 逐字：
        「field allowlist、consent、evidence、PII 与 provenance」）：

        * **field allowlist** —— `_declared_memory_fields_for_contact`。
          **清单归 profile**，平台守的是「有一份封闭的清单」
          （ADR-0131 §三 给这个词下的定义）；
        * **consent** —— `authorised_by = context.subject.id`，**不问 body 要**；
        * **evidence** —— `authorisation_note`，必填
          （`060` 的 CHECK 与请求模型同口径）；
        * **PII** —— 声明层的 denylist（E4-1b）**加**取值层的绊线（T298，本批）；
        * **provenance** —— `authorised_by` + `created_at` + `revision` 链。

        **已经有一条 active 就拒，不静默 supersede。**
        `054` 的 `contact_memories_one_active_per_field_idx` 保证一个字段只有一条
        active，而「撞唯一索引」是 500、「拒」是一句话。**更要紧的是语义**：
        `POST` 是「新记一件」，`PUT` 是「改一件」，freeze 把它们列成两行。
        让 `POST` 顺手覆盖，等于让一个手滑的重放把上一条证据换成新的——
        而 B4 #10 那句「不覆盖来源证据」正是要挡这个。

        **第一版没有 `source_event_id`。** 员工手工记的东西没有入站事件，
        `060` 的 `CHECK (num_nonnulls(source_event_id, authorised_by) = 1)` 把
        「恰好一个来源」写成了数据库事实。**要让员工把一条客户消息挂上来**
        （「他在第 3 条消息里说的」）是一件有价值但今天没有需求证据的事，
        **不在本批做，也不登记**——没有人要过它，凭空登记一条到期条件，
        是给下一个人留一件他不知道为什么要做的活。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        decision = authorize(
            context=context,
            action="contact_memory.create",
            resource=tenant_resource_ref(
                resource_type="contact_memory",
                tenant_id=tenant_id,
                resource_id=contact_id,
            ),
        )
        require_allowed(decision)
        _refuse_forbidden_memory_value(command.field_value)

        memory_id = uuid4()
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            contact = _contact_or_404(connection, tenant_id, contact_id, lock=True)
            lifetimes = _memory_lifetimes_for_contact(
                connection, tenant_id, contact["config_id"]
            )
            declared = _declared_memory_fields_for_contact(
                connection, tenant_id, contact["config_id"]
            )
            if command.field_key not in declared:
                # 422 而不是 404：字段名本身没打错，是**这家商家没声明要记它**。
                raise HTTPException(status_code=422, detail="MEMORY_FIELD_NOT_DECLARED")
            # **`FOR UPDATE` 去掉了，因为它在这里骗人**：要占位的那一行
            # 恰恰是**不存在**的那一行，而 `FOR UPDATE` 锁不住一条不存在的行。
            # 占位由上面那把咨询锁做，这里只是一次读。
            occupied = connection.execute(
                """
                SELECT 1 FROM contact_memories
                 WHERE tenant_id = %s AND contact_id = %s AND field_key = %s
                   AND status = 'active'
                """,
                (tenant_id, contact_id, command.field_key),
            ).fetchone()
            if occupied is not None:
                raise HTTPException(status_code=409, detail="MEMORY_FIELD_ALREADY_ACTIVE")
            connection.execute(
                """
                INSERT INTO contact_memories(
                    tenant_id, id, contact_id, field_key, field_value,
                    source_event_id, revision, supersedes, status,
                    authorised_by, authorisation_note
                ) VALUES (%s, %s, %s, %s, %s, NULL, 1, NULL, 'active', %s, %s)
                """,
                (
                    tenant_id,
                    memory_id,
                    contact_id,
                    command.field_key,
                    command.field_value,
                    context.subject.id,
                    command.note,
                ),
            )
            _audit_memory_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="contact_memory.create",
                memory_id=memory_id,
                reason_code="MEMORY_RECORDED",
                evidence={"field_key": command.field_key, "contact_id": str(contact_id)},
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
            written = connection.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM contact_memories "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s",
                (tenant_id, memory_id),
            ).fetchone()
        response.headers["cache-control"] = "no-store, private"
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="contact_memory",
                    resource_id=str(memory_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": _memory_projection(written, lifetimes=lifetimes),
        }

    @app.put("/contacts/{contact_id}/memories/{memory_id}", status_code=200)
    def revise_contact_memory(
        contact_id: UUID,
        memory_id: UUID,
        command: ReviseContactMemoryRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #10：改一条记错的记忆。**新修订，不是改写。**

        ## 它写两行，不改一行

        被改的那一条 `status` 变 `superseded`（`054` 的触发器只放行这一格），
        **新的一条是一整行**：`revision = 旧 + 1`、`supersedes = 旧 id`、
        contact 与 field_key 与旧的相同——**后两个必须相同，因为 `054` 的
        复合外键 `(tenant_id, supersedes, contact_id, field_key)` 就是为此而设**：
        「这一条的上一版是哪一条」不能跨客户、跨字段地回答。

        **顺序是先标旧后插新**，不能反过来：
        `contact_memories_one_active_per_field_idx` 是一个 partial unique index，
        两条 active 在同一个字段上会在插入那一刻撞。

        ## 「不覆盖来源证据」的两个方向，第二个才是本批的那个

        B4 #10 那句 **「不覆盖来源证据」** 通常被读成「别把旧证据擦掉」——
        这一条由「不改行、写新行」自动满足。
        **而真正需要一道闸的是反方向**：新行**不许继承**旧行的
        `source_event_id`。旧的那条是模型从一条客户消息里记的，
        新的这条是员工改的；把事件 id 抄过去，等于让新行声称
        **客户说过一句其实是员工写的话**。
        `060` 的 `CHECK (num_nonnulls(source_event_id, authorised_by) = 1)`
        让这件事在数据库层不可能，**而这里也不抄**。
        旧行留着它自己的证据，`supersedes` 一路走回去。

        ## 只能改 active 的那一条

        `superseded` 的再改一次，语义是「改一个已经不作数的版本」；
        `tombstoned` 的更不必说——触发器写死了不能复活。
        两者都是 409 `MEMORY_NOT_ACTIVE`，与 `archive_material` 第二次调用答 409
        同一条理由：**重试不得制造出一次新的修订。**
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        decision = authorize(
            context=context,
            action="contact_memory.update",
            resource=tenant_resource_ref(
                resource_type="contact_memory",
                tenant_id=tenant_id,
                resource_id=memory_id,
            ),
        )
        require_allowed(decision)
        _refuse_forbidden_memory_value(command.field_value)

        revision_id = uuid4()
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            contact = _contact_or_404(connection, tenant_id, contact_id, lock=True)
            lifetimes = _memory_lifetimes_for_contact(
                connection, tenant_id, contact["config_id"]
            )
            row = connection.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM contact_memories "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s AND contact_id = %s FOR UPDATE",
                (tenant_id, memory_id, contact_id),
            ).fetchone()
            if row is None:
                # **contact 也进 WHERE**：`/contacts/A/memories/{B 的记忆}` 必须是
                # 404，而不是悄悄改了 B 的那一条。路径里两个 id 都要算数。
                raise HTTPException(status_code=404, detail="MEMORY_NOT_FOUND")
            if row["status"] != "active":
                raise HTTPException(status_code=409, detail="MEMORY_NOT_ACTIVE")
            connection.execute(
                """
                UPDATE contact_memories SET status = 'superseded'
                 WHERE tenant_id = %s AND id = %s AND status = 'active'
                """,
                (tenant_id, memory_id),
            )
            connection.execute(
                """
                INSERT INTO contact_memories(
                    tenant_id, id, contact_id, field_key, field_value,
                    source_event_id, revision, supersedes, status,
                    authorised_by, authorisation_note
                ) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, 'active', %s, %s)
                """,
                (
                    tenant_id,
                    revision_id,
                    contact_id,
                    row["field_key"],
                    command.field_value,
                    int(row["revision"]) + 1,
                    memory_id,
                    context.subject.id,
                    command.note,
                ),
            )
            _audit_memory_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="contact_memory.update",
                memory_id=revision_id,
                reason_code="MEMORY_REVISED",
                evidence={
                    "field_key": row["field_key"],
                    "contact_id": str(contact_id),
                    "supersedes": str(memory_id),
                    "revision": int(row["revision"]) + 1,
                },
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
            written = connection.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM contact_memories "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s",
                (tenant_id, revision_id),
            ).fetchone()
        response.headers["cache-control"] = "no-store, private"
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="contact_memory",
                    resource_id=str(revision_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": _memory_projection(written, lifetimes=lifetimes),
        }

    @app.delete("/contacts/{contact_id}/memories/{memory_id}", status_code=200)
    def tombstone_contact_memory(
        contact_id: UUID,
        memory_id: UUID,
        command: TombstoneContactMemoryRequest,
        idempotency_key: IdempotencyKey,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B4 #11：不再用这条记忆。**墓碑，不是删行。**

        ## action 是 `invalidate`，不是 `delete`

        授权原语里 `delete` 是存在的（`tenant.delete`、`session.delete`），
        **而那些是真的删**。这一条不删任何东西：它把行标成 `tombstoned` 并盖一个
        时刻。先例是 `material.invalidate` 与 `scheduled_event.invalidate`。
        **HTTP 动词仍是 `DELETE`**，因为 freeze 定死了路径与动词——
        与 E3-6 把 `POST /materials/{id}/archive` 的 action 取作 `invalidate`
        而路径仍叫 `/archive` 是同一处理。

        ## 「retention、引用回答可解释」按 ADR-0131 M1 读

        「retention」= **商家清理自己的内部数据**，不是客户行使删除权
        （记忆不向客户告知、不请求客户同意）。
        **按 retention 的定时真删第一版不做**（排期 §4.4「不做」表）——
        本条只做墓碑。

        「引用回答可解释」今天成立到什么程度，**说清楚而不是含混**：
        * **新回复不再用它**——`prepare` 读记忆时带 `status = 'active'`
          （`pipeline.py::_remembered_memories`），墓碑立刻生效；
        * **旧回复仍解释得清**——行还在，`GET …/memories` 默认也仍然返回它，
          连同 `tombstoned_at`；
        * **「那条回复看见过哪几条记忆」从 2026-09-18 起记得下来**——
          E4-5 把记忆值接进了理解步与计划步，同批补了一行审计
          `inference.memories_sent`（**只记 field_key，不记值**）。
          ⚠️ **上一版这里写的是「记忆还没有进提示词，所以这一半不是欠账，
          是还没有发生」——那句话在 E4-5 之后不成立**，所以改在这里；
        * **「那条回复真的*引了*哪几条」从 2026-09-19 起也记得下来**（T363）——
          `inference.memories_cited`（`pipeline.py::_record_memories_cited`），
          与上面那一行成对：那一行答「递过去了哪几条」，这一行答「引了哪几条」，
          **两行相减才是「递过去而没被用上的那几条」**。
          它同样**只记 field_key，不记值**。
          知识库那边答过同型的题（ADR-0127 的 `RetrievalEvidence`、
          `062` 的 `cited_refs`），**排期 §4.5 写死了「可以搬，但要显式搬一次，
          不是默认继承」**——搬的记录写在 `_record_memories_cited` 的 docstring 里
          （搬的是那个问题与三条纪律，**没搬「一张表加一列」那个形状**）。
          ⚠️ **上一版这里写的是「仍然答不出来……登记在 T363」**，
          那句话在 T363 之后不成立；
        * **而仍然答不出来的是「引的那条记忆当时是哪一版」**——
          `contact_memories` 只存当前值，`remember` 再写一次同一个 field_key
          就把旧值盖掉了。**所以记忆的 ref 没有版本号**（知识库那边有）——
          登记在 T363 的状态栏里，**声明一个假的版本号比不声明更糟**。

        ## `DELETE` 带请求体，这是有意的

        **不常见，所以说清楚。** freeze 定死了路径与动词，而 B4 #11 要的
        「引用回答可解释」需要一句「为什么不再信它」——那句话只能由调用方给。
        剩下的位置只有查询串，而**一段自由文本挂在 URL 上会进访问日志**，
        它讲的是客户的事。

        `fetch` 与 `httpx` 都允许 `DELETE` 带体（只有 `GET` / `HEAD` 不允许），
        **而中间层未必**。真遇到吞体的代理时，退路是把它改成
        `POST …/memories/{id}/forget`——**那要改 freeze，所以不在本批做**。

        ## 已经是墓碑的，第二次调用答 409

        与 `archive_material` 同一条理由：重试不得制造出一次新的遗忘。
        **`superseded` 的可以立墓碑**——触发器放行 `active|superseded →
        tombstoned`，而商家清理旧版本是 M1「商家可删」的正当用法。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        decision = authorize(
            context=context,
            action="contact_memory.invalidate",
            resource=tenant_resource_ref(
                resource_type="contact_memory",
                tenant_id=tenant_id,
                resource_id=memory_id,
            ),
        )
        require_allowed(decision)

        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            contact = _contact_or_404(connection, tenant_id, contact_id, lock=True)
            lifetimes = _memory_lifetimes_for_contact(
                connection, tenant_id, contact["config_id"]
            )
            row = connection.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM contact_memories "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s AND contact_id = %s FOR UPDATE",
                (tenant_id, memory_id, contact_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="MEMORY_NOT_FOUND")
            if row["status"] == "tombstoned":
                raise HTTPException(status_code=409, detail="MEMORY_ALREADY_TOMBSTONED")
            connection.execute(
                """
                UPDATE contact_memories
                   SET status = 'tombstoned', tombstoned_at = now()
                 WHERE tenant_id = %s AND id = %s AND status <> 'tombstoned'
                """,
                (tenant_id, memory_id),
            )
            _audit_memory_event(
                connection,
                tenant_id=tenant_id,
                context=context,
                action="contact_memory.invalidate",
                memory_id=memory_id,
                reason_code="MEMORY_TOMBSTONED",
                evidence={
                    "field_key": row["field_key"],
                    "contact_id": str(contact_id),
                    # **员工说的「为什么不再信它」落在这里，不落在行上**：
                    # `054` 的触发器让行是不可变的，而这句话产生在行写下之后。
                    "note": command.note,
                    "previous_status": row["status"],
                },
            )
            require_obligations_discharged(decision, frozenset({"audit"}))
            tombstoned = connection.execute(
                f"SELECT {_MEMORY_COLUMNS} FROM contact_memories "  # noqa: S608
                "WHERE tenant_id = %s AND id = %s",
                (tenant_id, memory_id),
            ).fetchone()
        response.headers["cache-control"] = "no-store, private"
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="contact_memory",
                    resource_id=str(memory_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": _memory_projection(tombstoned, lifetimes=lifetimes),
        }

    @app.get(
        "/control-policy",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/control-policy"),
    )
    def read_control_policy(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #10 (`C0B-012`): 默认 AI/人工模式是 policy，不是 aggregate 状态.

        **The legacy route was `GET /conversations/default-status`, and the name
        was the bug.** A default that lives under `/conversations` invites the
        UI to treat it as a fact about a conversation -- so a page would read it,
        show it as the current mode, and be wrong about every conversation
        somebody had taken over. This one is tenant-level, has no conversation in
        its path, and says nothing about any particular conversation.

        **Both switches, always, even though only one of them is what most
        callers came for.** The policy is two fields; answering with one of them
        would make the other unreadable anywhere (T175's shape, registered the
        same day).
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="control_policy.read",
                resource=tenant_resource_ref(
                    resource_type="control_policy", tenant_id=tenant_id
                ),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT default_control_mode, understanding_during_takeover
                  FROM tenants WHERE id = %s
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:
            # The subject authenticated into this tenant, so the row existing is
            # an invariant rather than a case -- but RLS answers "no rows" for
            # a tenant that has been purged mid-session, and 404 is the honest
            # word for that.
            raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
        response.headers["cache-control"] = "no-store, private"
        return _control_policy_projection(row)

    @app.put(
        "/control-policy",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("PUT", "/control-policy"),
    )
    def write_control_policy(
        command: WriteControlPolicyRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B3 #11 (`C0B-013`): the policy half, audited.

        **`PUT` means both fields, and both are required.** A partial update on
        a two-field policy reads as "leave the other one alone", which is the
        same sentence as "I did not think about the other one" -- and the two
        switches interact: a tenant whose default is `human` and whose
        understanding is off has an engine that does nothing at all, which is a
        legitimate configuration and a terrible accident. Stating both makes the
        combination something somebody chose.

        **What this route does not do, and it is half of `C0B-013`.** The legacy
        route was 「更新默认**并可套用当前会话**」, and the freeze keeps that half:
        「policy release 与批量 transition 分 action、审计与上限」, with K13 setting
        the cap at 100. **The bulk transition is not in this batch** -- not
        because it is large, but because applying `ai` to conversations a person
        is holding is a resume, and ADR-0054 §三.4 makes resuming a two-phase
        command: a preview the operator reads, then a CAS on the digest they
        saw. One hundred previews nobody reads is that ruling with the safety
        removed. Registered rather than improvised, and the partial delivery is
        declared in the manifest the way ADR-0076 declared B3 #2's.

        **`tenant.update` is not this action.** ADR-0075 made that one
        platform-only and it is what disables a tenant; this is a switch the
        merchant owns. Two different decisions to grant, so two actions.

        **The batch half, and only one direction** (ADR-0079, user ruling
        2026-09-06). `transition_existing="human"` hands conversations that
        are still on the AI to people, at most `BULK_TRANSITION_LIMIT` per
        call (K13: 一次操作的影响面要能在一屏内复核). The other direction is
        refused by name, for two separate reasons the ADR keeps apart:

        * **safety is asymmetric** -- handing control over fails
          conservatively, taking it back has to inherit what happened during
          the takeover, and ADR-0054 §三.4 makes that a two-phase command
          precisely because a person must *read* the preview. A hundred
          previews nobody reads is that ruling with the safety removed;
        * **frequency is asymmetric** -- bulk-returning conversations to the
          AI is not a real scenario. **That half is the user's judgement, not
          something derived from the code**, and ADR-0079 §2.2 marks it as
          such so nobody later tries to "verify" it against an implementation.

        **Each conversation goes through the takeover body itself**
        (`_hand_conversation_to_a_person`), not through a bulk `UPDATE`: the
        stack is suspended, the generation moves, live timers are
        invalidated and each transition writes its own audit row. A shortcut
        here would quietly discard every invariant ADR-0054/0058 put on
        taking a conversation over.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            decision := authorize(
                context=context,
                action="control_policy.update",
                resource=tenant_resource_ref(
                    resource_type="control_policy", tenant_id=tenant_id
                ),
            )
        )
        if command.transition_existing == "human":
            # **The second grant, and `C0B-013` asks for it by name**
            # (「policy release 与批量 transition 分 action」; user ruling
            # 2026-09-06 on the review that found it missing). Handing a
            # hundred live conversations to people is the takeover act, a
            # hundred times -- this route already writes
            # `conversation_takeover.create` audit rows for each one, so
            # the audit layer had answered "which action is it" before the
            # authorizer did.
            #
            # **The existing action rather than a new one.** A fresh
            # `bulk_transition` action would overlap `conversation_takeover.create`
            # almost entirely, and K1 exists to stop the catalog swelling
            # into a command table. Two grants, both already meaningful:
            # "may change how this tenant runs" and "may take conversations
            # over".
            #
            # **Only on the branch that asks for it**: a plain policy
            # update still needs one grant, which is what makes these two
            # separately grantable rather than a bundle.
            require_allowed(
                authorize(
                    context=context,
                    action="conversation_takeover.create",
                    resource=tenant_resource_ref(
                        resource_type="conversation_takeover", tenant_id=tenant_id
                    ),
                )
            )
        if command.transition_existing == "ai":
            # ADR-0079 §二, and the code says which reason: refusing by name
            # is what tells a caller this is a decision rather than a gap.
            #
            # **After the authorization, not before** (independent review
            # 2026-09-06, P2-2). This used to answer 422 to anybody --
            # somebody holding only `control_policy.read` learned which
            # values this route takes and which one was ruled out. Every
            # other route in this file authorizes first, and B3 #1 (merged
            # the same day) puts its `control_mode` 422 after the check for
            # the same reason. It is still before the transaction, so
            # nothing is written.
            raise HTTPException(
                status_code=422, detail="BULK_TRANSITION_TO_AI_NOT_SUPPORTED"
            )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            before = connection.execute(
                """
                SELECT default_control_mode, understanding_during_takeover
                  FROM tenants WHERE id = %s
                 FOR UPDATE
                """,
                (tenant_id,),
            ).fetchone()
            if before is None:
                raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
            connection.execute(
                """
                UPDATE tenants
                   SET default_control_mode = %s,
                       understanding_during_takeover = %s
                 WHERE id = %s
                """,
                (
                    command.default_control_mode,
                    command.understanding_during_takeover,
                    tenant_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    context.subject.id,
                    "control_policy.update",
                    "control_policy",
                    tenant_id,
                    context.correlation_id,
                    "CONTROL_POLICY_UPDATED",
                    # **Both states, because a switch's history is the only way
                    # to answer "since when".** Neither value is customer data
                    # -- they are two enums about how this tenant runs -- so
                    # unlike a slot value or a task note, copying them into an
                    # append-only table costs nothing and is the whole point.
                    Jsonb(
                        {
                            "before": _control_policy_projection(before),
                            "after": _control_policy_projection(command.model_dump()),
                        }
                    ),
                ),
            )
            transitioned = 0
            remaining = 0
            skipped = 0
            if command.transition_existing == "human":
                # **Oldest first, and the order is part of the cap.** A
                # caller who repeats the call walks the backlog instead of
                # re-reading the same page, and "the hundred that have been
                # waiting longest" is the set an operator would pick.
                # **A count, because the field is named like one**
                # (independent review P1-2). Fetching `LIMIT + 1` rows made
                # `conversations_remaining` a boolean wearing a number's
                # name: 105 waiting reported "1 left". Counting is one extra
                # index scan on a page-sized operation, and the alternative
                # was to rename the field to `has_more` -- but "how many are
                # left" is what decides whether an operator calls again.
                waiting = connection.execute(
                    """
                    SELECT count(*) AS total FROM conversation_aggregates
                     WHERE tenant_id = %s AND control_mode = 'ai'
                    """,
                    (tenant_id,),
                ).fetchone()
                assert waiting is not None  # noqa: S101 -- count(*) always returns a row
                candidates = connection.execute(
                    """
                    SELECT id FROM conversation_aggregates
                     WHERE tenant_id = %s AND control_mode = 'ai'
                     ORDER BY updated_at, id
                     LIMIT %s
                    """,
                    (tenant_id, BULK_TRANSITION_LIMIT),
                ).fetchall()
                for candidate in candidates:
                    moved = _hand_conversation_to_a_person(
                        connection,
                        tenant_id=tenant_id,
                        conversation_id=UUID(str(candidate["id"])),
                        actor_id=context.subject.id,
                        correlation_id=context.correlation_id,
                        # Its own code: an operator reading the audit trail
                        # should be able to tell one deliberate takeover from
                        # a hundred that a policy change produced.
                        reason_code="CONVERSATION_TAKEN_OVER_BY_POLICY",
                    )
                    if moved is not None:
                        transitioned += 1
                    else:
                        # **The body refused it, and the answer says so**
                        # (independent review P2-1). `_hand_conversation_to_a_person`
                        # returns `None` for a conversation whose document
                        # the engine cannot process either -- the single
                        # route answers 409 `CONVERSATION_NOT_RESUMABLE` for
                        # exactly that. Counting it as neither moved nor
                        # remaining told the caller the tenant was fully
                        # handed over when one conversation was still on the
                        # AI and would stay there through every retry.
                        skipped += 1
                # **Reported, not silently truncated.** How many are still
                # waiting is what decides whether the caller comes back --
                # and the ones this call could not move are not waiting for
                # another call, so they are counted apart.
                remaining = max(int(waiting["total"]) - transitioned - skipped, 0)
            require_obligations_discharged(decision, frozenset({"audit"}))
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="control_policy",
                    resource_id=str(tenant_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            # The policy as it now stands, so a caller does not have to re-read
            # it to know what it agreed to.
            "result": {
                **_control_policy_projection(command.model_dump()),
                # **Named, not implied.** A caller that expected this call to
                # also switch existing conversations is told in the answer
                # what happened, rather than discovering it by watching the
                # conversations. It was hard-coded `0` while the batch half
                # was unbuilt (T176); it is the real number now.
                "conversations_transitioned": transitioned,
                # Zero unless a page was filled: "call again" is the only
                # thing a caller can do about it, so they are told.
                "conversations_remaining": remaining,
                # Looked at and not moved -- calling again will not help
                # these, which is why they are not in `remaining` (P2-1).
                "conversations_skipped": skipped,
            },
        }

    @app.get("/admin/tenants")
    def list_tenants(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #8 (`C0A-006` 拆): the platform's tenant directory.

        **A platform-level read, not a cross-tenant reach** (ADR-0066). The
        freeze line used to say this route needs a `SuperuserTarget`, which is
        "one target tenant plus one reason" -- and this route enumerates *all*
        tenants, so there is no single target to name. `authorize()`'s
        cross-tenant branch agrees: it requires `resource.tenant_id is not None`,
        which a list cannot produce. So the obligation would never have fired,
        and asking for a reason would have been a ceremony that changed nobody's
        access. Who may see the directory is decided by the deployment's
        allowlist (C4), which is a file on the host.

        **The resource is `tenant_registry`** (ADR-0069), the same tenant-less
        resource `POST /admin/tenants` acts on: the directory and writes to the
        directory belong together, rather than one being platform-level and the
        other borrowing the cross-tenant channel.

        **Four columns, and the fourth was ruled** (ADR-0073 §四). ADR-0066 §四
        says the directory returns id, code and status -- "not a window around
        `covers()`". `created_at` joins them because it is the ordering key and
        because **T167** has no other answer: a 201 lost on the network leaves a
        tenant this milestone cannot otherwise name, and "the one created just
        now" is how an operator finds it. Nothing about the tenant's own
        business content is here, and this route is not the place to add it.

        **The audit row goes to `platform_audit_events`** (ADR-0073). ADR-0066
        §三 required this route to discharge the audit obligation and did not say
        where the row goes; `audit_events.tenant_id` is `NOT NULL REFERENCES
        tenants(id)` and a directory read names no tenant. B1 #9 did not hit this
        because it creates a tenant for its row to belong to. The choice was
        ruled rather than picked, for the reason B1 #9's review gave about
        response shapes: the first platform-level read is the precedent every
        later one copies.

        **The 409 writes no audit row**, the same way a taken code writes none in
        `030`: nothing was disclosed, and a record saying the directory was read
        would be false. The obligation is discharged only on the path that
        actually answers.

        **`no-store, private`**, like every other list on this surface: a
        directory of every tenant on the deployment is the last thing that should
        sit in a shared cache.
        """

        require_allowed(
            decision := authorize(
                context=context,
                action="tenant_registry.list",
                resource=platform_resource_ref(resource_type="tenant_registry"),
            )
        )
        with database.transaction(
            tenant_id=None,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            rows = connection.execute(
                """
                SELECT id, code, status, created_at
                  FROM list_tenant_registry(%s)
                """,
                (MAX_PAGE_ITEMS + 1,),
            ).fetchall()
            # **Refused before anything is recorded.** `_page_or_refuse` raises
            # when the directory is larger than one page, and that path returns
            # nothing to the caller -- so it must not leave a row claiming the
            # directory was read.
            listing = _page_or_refuse(
                [
                    {
                        "id": str(row["id"]),
                        "code": row["code"],
                        "status": row["status"],
                        "created_at": _iso_timestamp(row["created_at"]),
                    }
                    for row in rows[:MAX_PAGE_ITEMS]
                ],
                has_more=len(rows) > MAX_PAGE_ITEMS,
            )
            connection.execute(
                """
                SELECT record_platform_audit_event(%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    context.subject.id,
                    "tenant_registry.list",
                    "tenant_registry",
                    # No `resource_id`: the directory itself is the resource, and
                    # it has no id. Naming one of the rows would be a guess.
                    None,
                    context.correlation_id,
                    "TENANT_REGISTRY_LISTED",
                    # **Two values, both admissible under ADR-0055 §二**: neither
                    # is personal data, neither is a verifier, and neither can be
                    # recovered afterwards -- how many tenants existed at the
                    # moment of the read is not a question any route can answer
                    # later. `returned` is the half that says how much was
                    # disclosed, which is the point of auditing a read at all.
                    Jsonb(
                        {
                            "actor_tenant": _actor_tenant(context),
                            "returned": len(rows[:MAX_PAGE_ITEMS]),
                        }
                    ),
                ),
            )
            # Inside the transaction and before the commit, for the reason B1 #9
            # gives: afterwards, "the audit failed" would have nothing left to
            # refuse.
            require_obligations_discharged(decision, frozenset({"audit"}))
        response.headers["cache-control"] = "no-store, private"
        return listing

    @app.post("/admin/tenants", status_code=201)
    def provision_tenant(
        command: ProvisionTenantRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #9 (`C0A-007`): 「显式 provisioning + audit」.

        **The resource is `tenant_registry`, not `tenant`** (ADR-0069). A tenant
        being created has no tenant to belong to, and `authorize()` requires a
        resource's tenant-ownership to match whether it carries a tenant id --
        so asking about `tenant` here could only be done with the *new* id,
        which is in nobody's scope and answers `RESOURCE_NOT_FOUND`. The
        registry is a platform object; creating an entry in it is a platform
        action, and `PLATFORM_RESOURCES` additionally requires a subject the
        deployment has promoted.
        **Holding the permission is not enough, and that is deliberate**: `owner`
        is defined as every grantable action, so without the platform rule the
        first tenant administrator to be seeded would have been able to create
        tenants.

        **This is the first route to discharge the audit obligation** (T163),
        and the claim needs stating precisely, because the first version of this
        docstring overstated it (independent review P2-3).

        `require_obligations_discharged` closes an **omission**, not a
        dishonesty -- its own docstring says so: it cannot verify that an audit
        row was written, only that the caller claims each obligation was met. A
        reviewer's probe made the difference concrete: replacing `provision_tenant`
        with a version that writes no audit row still answered 201, and deleting
        the discharge call below turned nothing red.

        So the two guarantees are separate and both are needed. **The audit row
        is guaranteed by migration `030`**, which writes it and the tenant in one
        statement -- "provisioned but unrecorded" is not a state the database can
        hold. **The call below guarantees that a future edit cannot quietly stop
        *claiming* the obligation** while `authorize()` still demands it.

        **A bare `CommandReceipt`** (ADR-0070 §二), the same shape the three
        sibling create routes return. The first version hand-wrote a flat body,
        which was an implementation choosing a contract: `resource_ref()` refused
        global resources outright and said naming one was "undecided (CP-1), no
        caller today", and this route was that caller. Roughly seventy routes
        will copy whichever shape lands first, so it was ruled rather than
        picked (independent review P1-2).

        **Not ADR-0050's `{"receipt", "result"}` layering**: that exists for a
        command that also has a domain answer to give -- B1 #16 reports how many
        sessions it revoked. What provisioning has to answer is "which thing did
        you make", and that is the receipt's `resource_ref`. A wrapper around it
        would add a level and say nothing.

        **201, and deliberately no `Location`** (independent review P3-1). The
        first version sent `Location: /admin/tenants/{id}`, and RFC 7231 says
        that header names the URI of the created resource -- but M1a has no
        item-level read for a tenant at all (#8 is a list, #10 patches, #11
        deactivates, #12 purges), so the address 404s. A header pointing at
        nothing is worse than an absent one: it invites a client to follow it.
        The id is in the body, which is where a caller can actually use it.
        """

        require_allowed(
            decision := authorize(
                context=context,
                action="tenant_registry.create",
                resource=platform_resource_ref(resource_type="tenant_registry"),
            )
        )
        tenant_id = uuid4()
        audit_id = uuid4()
        with database.transaction(
            tenant_id=None,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                SELECT id, code, status, created_at, created
                  FROM provision_tenant(%s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    command.code,
                    context.subject.id,
                    context.correlation_id,
                    audit_id,
                    Jsonb({"code": command.code, "actor_tenant": _actor_tenant(context)}),
                ),
            ).fetchone()
            assert row is not None  # noqa: S101 -- the function returns exactly one row
            if not row["created"]:
                # **409, the code alone -- and P3-3 stays open** (ADR-0070 §六
                # as amended). The plan was to return the existing tenant's id
                # here, so that a 201 lost on the network could be recovered by
                # retrying. `ErrorEnvelope` does not allow it: it is
                # `additionalProperties: false`, and the handler above says in
                # as many words that `detail` must not become a place to attach
                # internals. Widening a contract that exists for that reason, as
                # a side effect of one route's convenience, is the wrong trade.
                #
                # So the gap the reviewer found is real and is **registered**
                # (T167) rather than papered over: until either idempotency is
                # stored or an item read exists (B1 #8), a lost 201 leaves a
                # tenant that no M1a route can name.
                #
                # The function still tells us `created`, which is why this is a
                # clean 409 rather than a caught `UniqueViolation`: the lookup
                # cannot be done afterwards -- the transaction would be aborted
                # and RLS hides `tenants` from a tenant-less session anyway.
                raise HTTPException(status_code=409, detail="TENANT_CODE_TAKEN")
            # Discharged **inside the transaction**, before the commit: after it,
            # "the audit failed" would have nothing left to refuse.
            require_obligations_discharged(decision, frozenset({"audit"}))
        return command_receipt(
            resource=resource_ref(
                resource_type="tenant_registry",
                resource_id=str(row["id"]),
                # **A global resource, so no tenant** (ADR-0070 §三). The
                # registry belongs to the platform; the tenant it just created
                # is the *subject* of the command, not its owner.
                authorized_tenant_id=None,
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict()
    @app.post("/admin/tenants/{tenant_id}/deactivate", status_code=200)
    def deactivate_tenant(
        tenant_id: UUID,
        command: DeactivateTenantRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #11 (`C0A-009` + parity §6.4): deactivate a tenant.

        §6.4 names three effects and this route does exactly those three:
        **revoke sessions immediately**, stop the channel bindings so no new
        inbox arrives, and record `disabled_at` with the retention clock at
        `purge_eligible_at = disabled_at + 90 days`. Purge is a different route,
        a different action and a different test set -- §6.4 forbids a boolean
        switching between them, which is why nothing here takes a flag.

        **The first real consumer of the cross-tenant channel** (ADR-0068). The
        caller names the target and a reason; `authorize()` refuses unless the
        subject was promoted by the deployment, the named target equals this
        resource's tenant, and a reason is present.

        **The transaction opens on `Decision.authorized_tenant_id`**, never on
        `tenant_scope` and never on the path parameter (ADR-0068 §四点五). The
        path value did reach `authorize()` -- as the resource it decided about --
        and what comes back is the tenant the decision authorised. Taking it
        from the request instead would put a caller-supplied id into
        `app.tenant_id`, the one input this repository's RLS design refuses to
        trust.

        **Two obligations are discharged here, and the second is new**: `audit`,
        and `reason_required` -- the one ADR-0068 attaches to every cross-tenant
        reach. T163's other half: until now no route had ever reached across
        tenants, so nothing had ever discharged it.

        **A second call answers 409**, the same choice B1 #18 made for employees
        and for the same reason: re-stamping `disabled_at` would move the
        retention clock, and a retry must not quietly extend how long a
        merchant's data is kept.

        **Channel bindings are stopped, not destroyed.** §6.4 says
        「撤销 session、channel/binding 并阻止新 inbox」, and flipping `status`
        achieves the stated effect -- the ingress adapter refuses any binding
        whose status is not `active`, which is where "no new inbox" actually
        lives. Destroying the callback tokens the way `POST
        /channel-bindings/{id}/deactivate` does would be **irreversible**, and
        this state is explicitly a 90-day holding state that ends in a separate
        purge. The narrower reading is the one that can still be widened later;
        the wider one cannot be undone. **Registered rather than assumed** --
        see the batch's self-review.
        """

        require_allowed(
            decision := authorize(
                context=context,
                action="tenant.update",
                resource=tenant_resource_ref(resource_type="tenant", tenant_id=tenant_id),
                request_facts={
                    "target_tenant_id": str(tenant_id),
                    "reason": command.reason,
                },
            )
        )
        authorized = decision.authorized_tenant_id
        assert authorized is not None  # noqa: S101 -- an allow on a tenant-owned resource
        audit_id = uuid4()
        with database.transaction(
            tenant_id=authorized,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **`WHERE status = 'active'` rather than a read-then-write.** Two
            # callers racing would otherwise both read `active` and both stamp
            # `disabled_at`, and the second stamp moves the retention clock. The
            # row count is the answer to "was I the one who did it".
            stopped = connection.execute(
                """
                UPDATE tenants
                   SET status = 'disabled',
                       disabled_at = now(),
                       purge_eligible_at = now() + interval '90 days'
                 WHERE id = %s AND status = 'active'
             RETURNING disabled_at, purge_eligible_at
                """,
                (authorized,),
            ).fetchone()
            if stopped is None:
                # **Which of the two happened** (independent review P2-1). The
                # first version answered 409 "already disabled" for both, and
                # for a tenant id that never existed that sentence is simply
                # false -- an operator who mistypes an id reads "I already
                # stopped this one", stops looking, and the tenant they meant to
                # stop is still serving. The comment here used to give a reason
                # that argued for the opposite of what the code did.
                #
                # Telling them apart is safe *here* and nowhere else: reaching
                # this line means `authorize()` allowed a platform-only action
                # for a subject the deployment promoted by writing a file on the
                # host. Existence of a tenant is not a secret from that subject
                # -- `GET /admin/tenants` lists every one of them for the same
                # audience.
                present = connection.execute(
                    "SELECT status FROM tenants WHERE id = %s",
                    (authorized,),
                ).fetchone()
                if present is None:
                    raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
                raise HTTPException(status_code=409, detail="TENANT_ALREADY_DISABLED")
            sessions = connection.execute(
                # **`now()` on purpose** (independent review F4): the predicate
                # picks the rows this statement is about to stamp, and it names
                # the same instant the stamp does. It is also the conservative
                # direction -- a session that expired while this transaction ran
                # gets revoked as well, which is the harmless half of the choice.
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND revoked_at IS NULL AND expires_at > now()
                """,
                (authorized,),
            ).rowcount
            bindings = connection.execute(
                """
                UPDATE channel_bindings
                   SET status = 'disabled'
                 WHERE tenant_id = %s AND status = 'active'
                """,
                (authorized,),
            ).rowcount
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    authorized,
                    audit_id,
                    context.subject.id,
                    "tenant.update",
                    "tenant",
                    authorized,
                    context.correlation_id,
                    "TENANT_DEACTIVATED",
                    # **The reason is recorded because it is the whole point of
                    # the channel** (ADR-0068): a cross-tenant reach that leaves
                    # no trace of *why* is the shape §5.2 exists to prevent. It
                    # is operator-written text about an administrative action,
                    # not customer content, and no route can answer "why did
                    # they do that" afterwards -- ADR-0055 §二's three
                    # conditions all hold.
                    Jsonb(
                        {
                            "reason": command.reason,
                            "actor_tenant": _actor_tenant(context),
                            "sessions_revoked": sessions,
                            "bindings_stopped": bindings,
                        }
                    ),
                ),
            )
            require_obligations_discharged(
                decision, frozenset({"audit", "reason_required"})
            )
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="tenant",
                    resource_id=str(authorized),
                    # **From the decision, not from the context**
                    # (ADR-0068 §四点五 applied to the receipt): on a
                    # cross-tenant reach the caller's own tenant is the
                    # wrong answer, and it would be the wrong answer
                    # silently, inside the field that says what was
                    # touched.
                    authorized_tenant_id=authorized_tenant_from_decision(decision),
                    # **The path parameter is a claimed tenant, so it is passed
                    # as one** (independent review P3-1). `claimed_tenant_id`
                    # has no default precisely because "forgot to pass it"
                    # silently skips the one comparison it exists for, and
                    # writing `None` says "there is nothing to compare" --
                    # untrue here. The comparison is provably redundant today
                    # (`authorize()` already matched the submitted target
                    # against the resource), and that is the point: this is
                    # the first route that genuinely has a claimed value, and
                    # the next one will copy whatever this one does.
                    claimed_tenant_id=tenant_id,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            # **`{"receipt", "result"}`** (ADR-0050 §三): this command has a
            # domain answer of its own, exactly as B1 #16 does. "How many
            # sessions did I just end, and how many channels did I just stop"
            # is not visible anywhere else -- there is no route that lists a
            # deactivated tenant's sessions -- and an operator needs it to know
            # whether the merchant is really quiet.
            "result": {
                "status": "disabled",
                "disabled_at": _iso_timestamp(stopped["disabled_at"]),
                "purge_eligible_at": _iso_timestamp(stopped["purge_eligible_at"]),
                "sessions_revoked": sessions,
                "bindings_stopped": bindings,
            },
        }

    @app.post("/admin/tenants/{tenant_id}/purge", status_code=202)
    def purge_tenant(
        tenant_id: UUID,
        command: PurgeTenantRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #12 + parity §6.4: erase a tenant's data.

        **Asynchronous, and the receipt says `accepted`** -- the opposite of
        what B1 #19 does for employees, deliberately. ADR-0056 allowed the
        employee purge to run inline because its work is *bounded*: four tables
        carry an employee's identity. A tenant purge deletes from twenty-one
        tables, and ADR-0056's own 修订标注 wrote that租户侧「是真正的异步作业」.
        `accepted` is therefore the honest word: the work is queued, and
        `C13` (ADR-0083) is satisfied because the executor now exists on Stage.

        **Five guards, each with its own code**, for the reason B1 #19 gives:
        `409` alone tells an operator to retry, and four of these never succeed
        on retry. Not found; already purged; not disabled; under legal hold; not
        yet eligible.

        **The reason is submitted again here** rather than inherited from the
        deactivation. §6.4 asks for it at purge time, and the two decisions are
        months apart -- the operator who deactivated a tenant in March is not
        necessarily the one erasing it in June, and the reason that justified
        stopping it is not the reason for destroying it.

        **`legal_hold` is read and never written**, exactly as the employee
        purge reads it: `032` withheld the `UPDATE` grant on that column on
        purpose (T118). A route that can clear its own guard is not a guard.

        **What this route does not destroy is in the response**, not only in an
        ADR (ADR-0092 §二.1). `profile_releases` survives -- `006` makes it
        append-only, and the audit that §6.4 preserves records decisions made
        *under a release*, so deleting releases would leave the audit pointing
        at nothing. **That is not a claim that profile content carries no
        personal data** (T189 is open on precisely that), which is why the
        caller is told rather than left to assume a purge purged everything.

        **The job row is the promise.** It is written in the same transaction as
        the audit event, so a caller who received a receipt has a queued job;
        there is no window where the receipt exists and the work does not.
        """

        require_allowed(
            decision := authorize(
                context=context,
                action="tenant.delete",
                resource=tenant_resource_ref(resource_type="tenant", tenant_id=tenant_id),
                request_facts={
                    "target_tenant_id": str(tenant_id),
                    "reason": command.reason,
                },
            )
        )
        authorized = decision.authorized_tenant_id
        assert authorized is not None  # noqa: S101 -- an allow on a tenant-owned resource
        audit_id = uuid4()
        job_id = uuid4()
        with database.transaction(
            tenant_id=authorized,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **`FOR UPDATE`, so the five guards cannot be overtaken** between
            # the read and the insert -- the same reason B1 #19 reads its
            # subject that way. Two callers racing here would otherwise both
            # see an eligible tenant and both queue a purge.
            subject = connection.execute(
                """
                SELECT status, disabled_at, purge_eligible_at, legal_hold, purged_at
                  FROM tenants
                 WHERE id = %s
                   FOR UPDATE
                """,
                (authorized,),
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
            if subject["purged_at"] is not None:
                raise HTTPException(status_code=409, detail="TENANT_ALREADY_PURGED")
            if subject["status"] != "disabled":
                # §6.4 orders the two: a tenant is stopped, the retention clock
                # runs, and only then may it be erased. Purging a serving tenant
                # would destroy data while conversations are still arriving.
                raise HTTPException(status_code=409, detail="TENANT_NOT_DISABLED")
            if subject["legal_hold"]:
                raise HTTPException(status_code=409, detail="TENANT_UNDER_LEGAL_HOLD")
            # **Compared in SQL, against the clock that wrote it.**
            #
            # `purge_eligible_at` is written with Postgres `now()`. This used to
            # compare it to `datetime.now(UTC)` with a comment arguing the
            # crossing was safe: "a process clock running behind the database
            # refuses a purge that is barely eligible, and refusing to erase for
            # a few seconds longer is the error worth making."
            #
            # **That argument only ran in one direction.** It says what happens
            # when this process is behind, and stops there. When the process is
            # *ahead*, the same expression makes a tenant eligible early, and
            # what it authorises is erasure. T193 then measured the offset on
            # this host swinging both ways -- `-0.858s` to `+1.538s` -- so the
            # unexamined direction is not hypothetical, it is half the cycle.
            #
            # The window is ninety days, so nothing here turns on a second. The
            # reason to fix it anyway is the reason the *employee* purge guard
            # a few thousand lines down already gives, having been corrected by
            # an independent review (2026-09-05, F8) for this exact shape: a
            # comparison across two clocks is wrong in a way nobody notices
            # until it is. Two identical retention guards, and only one of them
            # had been brought over -- so the rule read as having an exception.
            # **`now()` here rather than `clock_timestamp()`, on purpose.** The
            # rule this repository follows -- a predicate that decides whether
            # to *admit* something reads `clock_timestamp()`, a predicate that
            # only picks the rows a statement is about to stamp keeps `now()`
            # -- points at `clock_timestamp()` for this one: it admits an
            # erasure. It stays on `now()` for two reasons, and they belong in
            # the file rather than in a self-review: what the rule buys here is
            # one transaction's length against a *ninety day* window, which is
            # not "small" but irrelevant; and the tenant and employee guards
            # must stay literally identical, because a source assertion counts
            # this exact statement twice and the entire argument for touching
            # either of them is that a rule with one exception reads as a rule
            # with an exception (independent review 2026-09-07, P3).
            #
            # **`::timestamptz` on both parameters is load-bearing.**
            # `%s IS NOT NULL` gives PostgreSQL nothing to infer a type from:
            # psycopg sends `None` with OID 0, the statement cannot be planned
            # (`IndeterminateDatatype`), and a defined `409` becomes an
            # unhandled `500`. The cast makes the NULL row answer `due = false`
            # -- the same refusal the `is None` short-circuit used to give
            # before this comparison moved into SQL (same review, P2).
            eligible = connection.execute(
                "SELECT %s::timestamptz IS NOT NULL AND %s::timestamptz <= now() AS due",
                (subject["purge_eligible_at"], subject["purge_eligible_at"]),
            ).fetchone()
            # The `is None` half cannot happen -- a `SELECT` without a `FROM`
            # returns exactly one row -- and stays only because the row type is
            # optional. `not due` is the half that decides, and it now covers a
            # NULL `purge_eligible_at` too.
            if eligible is None or not eligible["due"]:
                raise HTTPException(status_code=409, detail="TENANT_NOT_YET_ELIGIBLE")

            # **`ON CONFLICT` on the *live* job index** (independent review
            # P2-1). A retry must not queue a second erasure, and a `409` says
            # the promise was already made rather than pretending to make it
            # twice -- but the uniqueness is now scoped to jobs still in flight.
            # A blanket constraint also pinned the tenant after a job reached a
            # terminal state, and no role in M1a can clear such a row: a purge
            # that failed three times, or one a legal hold refused, left the
            # tenant permanently un-queueable while the route kept answering
            # "already queued".
            queued = connection.execute(
                """
                INSERT INTO tenant_purge_jobs(tenant_id, id, requested_by, reason)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id) WHERE status IN ('PENDING', 'RUNNING')
                    DO NOTHING
                RETURNING id
                """,
                (authorized, job_id, context.subject.id, command.reason),
            ).fetchone()
            if queued is None:
                raise HTTPException(status_code=409, detail="TENANT_PURGE_ALREADY_QUEUED")
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    authorized,
                    audit_id,
                    context.subject.id,
                    "tenant.delete",
                    "tenant",
                    authorized,
                    context.correlation_id,
                    "TENANT_PURGE_QUEUED",
                    Jsonb(
                        {
                            "reason": command.reason,
                            "actor_tenant": _actor_tenant(context),
                            "job_id": str(job_id),
                        }
                    ),
                ),
            )
            require_obligations_discharged(
                decision, frozenset({"audit", "reason_required"})
            )
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="tenant",
                    resource_id=str(authorized),
                    authorized_tenant_id=authorized_tenant_from_decision(decision),
                    claimed_tenant_id=tenant_id,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                # **`accepted`, and B1 #19 says `succeeded`** -- the difference
                # is the whole of ADR-0083. Saying `succeeded` here would
                # promise an erasure that has not happened.
                status="accepted",
            ).as_dict(),
            "result": {
                "job_id": str(job_id),
                "status": "PENDING",
                # **Told, not implied** (ADR-0092 §二.1). A caller of something
                # named "purge" should not have to read an ADR to learn what it
                # keeps.
                "retained": {
                    "audit_events": "parity §6.4 forbids deleting the audit",
                    "profile_releases": (
                        "append-only (migration 006); the surviving audit records "
                        "decisions made under a release. This is not a statement "
                        "about whether profile content carries personal data."
                    ),
                    "tenant_row": (
                        "tombstoned, not deleted: audit_events references it. The "
                        "identifying code is destroyed."
                    ),
                },
            },
        }

    @app.post("/channel-bindings/{config_id}/credentials", status_code=200)
    def register_channel_credential(
        config_id: str,
        command: RegisterChannelCredentialRequest,
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B2 #4 (`C0A-033` 拆): credential rotation, per ADR-0061.

        **The request does not carry the secret**, and that is the whole of what
        ADR-0061 decided. What it carries is a reference to a file the operator
        already placed, plus the version they gave it. The server reads that
        file -- read-only, which is the half that was always possible -- hashes
        it, and stores eight characters of the digest.

        **Why the server reads it at all**, rather than taking a fingerprint the
        caller supplies: a fingerprint that arrives in the request is a claim
        nothing can check, and if it is wrong then `fingerprint` and
        `last_verified_at` are both lies -- while `last_verified_at` means
        precisely "the server looked". Reading makes both columns facts.

        **What it does not prove.** That the file is there and its contents hash
        to this value. Whether the provider agrees is `POST
        /channel-bindings/verify`'s question, and ADR-0061 §五 says so rather
        than letting this route's success imply it.

        **No `CommandReceipt`** (ADR-0050 §三, parity §5.2). Receipts are for
        money, sending, purge, restore, bulk state change and external-channel
        actions. This writes five metadata columns and calls nothing outward --
        the sibling `POST .../deactivate` carries one because destroying a
        credential *is* an external-channel action. The criterion decides it,
        not the cluster.

        **A disabled binding is refused with 409**, the same reasoning as
        `PATCH /channel-bindings/{config_id}`: B2 #5 destroys the callback token
        and M1a has no reactivation route, so registering a credential against a
        disabled binding writes state that can never take effect.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="channel_config.manage_secret",
                resource=tenant_resource_ref(
                    resource_type="channel_config", tenant_id=tenant_id
                ),
            )
        )
        fingerprint = _channel_secret_fingerprint(resolved, tenant_id, command.secret_ref)
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                UPDATE channel_bindings
                   SET secret_ref = %s,
                       secret_version = %s,
                       secret_fingerprint = %s,
                       secret_updated_at = now(),
                       secret_last_verified_at = now()
                 WHERE tenant_id = %s AND config_id = %s AND status = 'active'
                RETURNING secret_ref, secret_version, secret_fingerprint,
                          secret_updated_at, secret_last_verified_at, status
                """,
                (
                    command.secret_ref,
                    command.version,
                    fingerprint,
                    tenant_id,
                    config_id,
                ),
            ).fetchone()
            if row is None:
                # Told apart inside the transaction (B1 #18's shape): a
                # concurrent deactivation can overtake two statements.
                existing = connection.execute(
                    "SELECT status FROM channel_bindings"
                    " WHERE tenant_id = %s AND config_id = %s",
                    (tenant_id, config_id),
                ).fetchone()
                if existing is None:
                    # Another tenant's binding and a missing one are one answer,
                    # or the route is a cross-tenant existence oracle.
                    raise HTTPException(status_code=404, detail="CHANNEL_BINDING_NOT_FOUND")
                raise HTTPException(status_code=409, detail="CHANNEL_BINDING_DISABLED")
            _audit_channel_binding_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                config_id=config_id,
                action="channel_binding.register_credential",
                correlation_id=context.correlation_id,
                reason_code="CHANNEL_CREDENTIAL_REGISTERED",
                # **`secret_ref` and the version, and deliberately not the
                # fingerprint** (ADR-0055). The reference and the version are
                # facts nothing else in the system can answer once this row is
                # overwritten by the next rotation -- which is the criterion's
                # third clause. The fingerprint fails the criterion's second:
                # it is credential-derived, and the audit log is read by more
                # people than the column is.
                evidence={
                    "config_id": config_id,
                    "secret_ref": command.secret_ref,
                    "version": command.version,
                },
            )
        return {
            # K9's field names, not the column names. The columns are prefixed
            # so that `updated_at` cannot be confused with the row's own
            # `created_at`; the response is the masked metadata set K9 names.
            "config_id": config_id,
            "secret_ref": row["secret_ref"],
            "version": row["secret_version"],
            "status": row["status"],
            "fingerprint": row["secret_fingerprint"],
            "updated_at": _iso_timestamp(row["secret_updated_at"]),
            "last_verified_at": _iso_timestamp(row["secret_last_verified_at"]),
            "correlation_id": str(context.correlation_id),
        }

    @app.patch("/channel-bindings/{config_id}")
    def update_channel_binding(
        config_id: str,
        command: UpdateChannelBindingRequest,
        response: Response,
        # parity 5.2: "所有 create/update command 默认需要 `Idempotency-Key`".
        # Demanded rather than used, like the sibling PATCH -- a retry of this
        # route is a full overwrite of the field it names.
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B2 #3 (`C0A-033` 拆): 「仅非 secret 元数据」.

        **One writable field, and that is the whole route.** The freeze split
        the old endpoint four ways (see `UpdateChannelBindingRequest`), and what
        is left here is the `guid` -- `provider_instance_id`, which `017` added
        with the column comment "Written by route freeze B2 when it lands;
        seeded by an operator until then". This is the batch that comment names.

        **The response does not contain the field this route writes**, and that
        is the one thing worth reading twice. `PATCH /employees/{id}` echoes the
        stored values because "what did my partial update actually leave here"
        is the question a partial update creates -- and this route cannot answer
        it, because **T123** reserves `SELECT (provider_instance_id)` for the
        outbound wiring batch and migration `025` grants only `UPDATE`. The
        alternatives were both worse: granting the read here would take half of
        T123's decision as a side effect of a response body, and echoing the
        request back would publish the caller's own input dressed as state. So
        the body is the projection `GET /channel-bindings` publishes, plus
        `correlation_id`, and **T135** carries the gap with T123's expiry.

        **No `CommandReceipt`** (ADR-0050 §三). parity §5.2 requires one for
        money, sending, purge, restore, bulk state change and external-channel
        actions; this route writes one metadata column and calls nothing
        outward. Its sibling `POST .../deactivate` does carry a receipt, because
        destroying a channel credential is an external-channel action -- the
        difference between the two rows is the criterion, not the cluster.

        **A disabled binding is refused with 409**, not silently updated: B2 #5
        destroys the callback token and M1a has no reactivation route, so a
        disabled binding can never send again. Retargeting one writes state that
        can never take effect -- and, if a reactivation route ever lands, leaves
        a binding that looks configured and holds no credential.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="channel_config.update",
                resource=tenant_resource_ref(
                    resource_type="channel_config", tenant_id=tenant_id
                ),
            )
        )
        submitted = {
            field: getattr(command, field)
            for field in ("provider_instance_id",)
            if field in command.model_fields_set
        }
        if not submitted:
            # The sibling's answer, and the only one available: with `extra`
            # forbidden, an empty body names nothing to change.
            raise HTTPException(status_code=422, detail="NO_FIELDS_SUBMITTED")
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                UPDATE channel_bindings
                   SET provider_instance_id = %s
                 WHERE tenant_id = %s AND config_id = %s AND status = 'active'
                RETURNING config_id, source, status, profile_release_id,
                          current_valid_from, next_valid_from, overlap_ends_at, created_at,
                          secret_ref, secret_version, secret_fingerprint,
                          secret_updated_at, secret_last_verified_at
                """,
                # **A plain assignment, not the sibling's `coalesce` /
                # `CASE WHEN`** -- and not by preference. Those forms *read* the
                # column they might leave alone, and reading a column inside
                # `UPDATE ... SET` needs `SELECT` on it, which `025`
                # deliberately does not grant. B2 #5's reverse probes learned
                # this the expensive way. With one field, "submitted" already
                # means "assign it", so there is nothing to leave alone; the day
                # a second field lands, it needs either its own `SELECT` or a
                # statement built from `submitted`.
                (submitted["provider_instance_id"], tenant_id, config_id),
            ).fetchone()
            if row is None:
                # Told apart **inside the transaction**, B1 #18's shape: a
                # concurrent deactivation can overtake two statements.
                existing = connection.execute(
                    "SELECT status FROM channel_bindings"
                    " WHERE tenant_id = %s AND config_id = %s",
                    (tenant_id, config_id),
                ).fetchone()
                if existing is None:
                    # Another tenant's binding and a missing one are one answer,
                    # or the route is a cross-tenant existence oracle.
                    raise HTTPException(status_code=404, detail="CHANNEL_BINDING_NOT_FOUND")
                raise HTTPException(status_code=409, detail="CHANNEL_BINDING_DISABLED")
            _audit_channel_binding_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                config_id=config_id,
                action="channel_binding.update",
                correlation_id=context.correlation_id,
                reason_code="CHANNEL_BINDING_METADATA_UPDATED",
                # **The value is recorded here, and the sibling's rule says it
                # should not be -- so the difference has to be stated.** `PATCH
                # /employees/{id}` records field names only, because a display
                # name is somebody's name and `GET /employees/{id}` can answer
                # "what is it now". Neither holds here: a provider instance id
                # is infrastructure identity, not personal data, and **no route
                # and no runtime role can read this column** (T123), so an audit
                # row without the value is one nothing in the system can join
                # back to a value. `fastagent_app` holds `INSERT` on
                # `audit_events` and no `SELECT` (`001`), so this does not hand
                # the API a way back in.
                #
                # **Per field, not by spreading `submitted`.** B2 #4 brings
                # `secret_ref` and `fingerprint` to this table; a rule that
                # writes whatever was submitted into an append-only table would
                # welcome them in.
                evidence={
                    "fields": sorted(submitted),
                    "provider_instance_id": submitted["provider_instance_id"],
                },
            )
        response.headers["cache-control"] = "no-store, private"
        return {
            # **The same keys `GET /channel-bindings` publishes**, down to
            # `release_id` being the wire's name for `profile_release_id` -- a
            # caller must not be able to read a binding here and have the value
            # rejected by a route two lines away.
            #
            # It was eight keys until B2 #4 and is thirteen now: 029 added K9's
            # metadata to the list projection, and `test_the_body_is_the_shape_
            # the_list_route_publishes` turned red until this followed. That
            # case is the reason the two cannot drift -- widening one route's
            # projection and forgetting its sibling is precisely what it exists
            # to catch, and it caught it.
            "config_id": row["config_id"],
            "source": row["source"],
            "status": row["status"],
            "release_id": str(row["profile_release_id"]),
            "current_valid_from": _iso_timestamp(row["current_valid_from"]),
            "next_valid_from": (
                _iso_timestamp(row["next_valid_from"])
                if row["next_valid_from"] is not None
                else None
            ),
            "overlap_ends_at": (
                _iso_timestamp(row["overlap_ends_at"])
                if row["overlap_ends_at"] is not None
                else None
            ),
            "created_at": _iso_timestamp(row["created_at"]),
            "secret_ref": row["secret_ref"],
            "version": row["secret_version"],
            "fingerprint": row["secret_fingerprint"],
            "updated_at": (
                _iso_timestamp(row["secret_updated_at"])
                if row["secret_updated_at"] is not None
                else None
            ),
            "last_verified_at": (
                _iso_timestamp(row["secret_last_verified_at"])
                if row["secret_last_verified_at"] is not None
                else None
            ),
            "correlation_id": str(context.correlation_id),
        }

    @app.put("/channel-bindings/{config_id}/profile-release")
    def repin_binding(
        config_id: str,
        command: RepinBindingRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B2 #14: point a channel binding at a published release.

        **This route is the missing entry point, not new machinery.** T15 built
        `repin_channel_binding` and the target-stack repair behind it; T24's
        note then recorded what was left: `repin_channel_binding` has **no
        production caller** -- the CLI has `list` / `publish` / `show` and
        nothing else -- so "no human or process can trigger a repin today", and
        the exit item "三 · ② 绑定" stayed ❌ for want of a door rather than for
        want of a room. The freeze put the door in B2 #14 and said it waited on
        the control-plane foundations. Those landed (CP-0, K1, C1), so here it
        is.

        **`PUT` rather than `POST`**: pointing a binding at a release is
        idempotent against the binding -- doing it twice leaves the same state,
        and the second call reports `changed: false`. That is what `PUT` means,
        and it is why the underlying function was built to be idempotent
        separately from the conversation move.

        **The response is `{"receipt": …, "result": …}`** (ADR-0050). A receipt
        alone would hide the half an operator has to act on: whether live
        conversations were moved, how many are left (the call is bounded by
        `DEFAULT_REPIN_LIMIT`, so `remaining` non-zero means "call again"), and
        how many pre-008 rows were out of scope entirely. `RepinResult` already
        reports all three and `binding.py` explains why the last one is reported
        rather than skipped silently; dropping them would put the operator back
        to guessing. They sit in `result` rather than beside the receipt's own
        fields because the receipt contract is `additionalProperties: false` --
        and the shape here is the one about 70 later routes will copy.

        **`tenant_conversations_unlinked` is on a different scale from the other
        two**, and its name now carries that: pre-008 conversation rows have no
        binding, so it counts rows for the **tenant**, and every binding in the
        tenant reports the same figure.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="channel_config.update",
                resource=tenant_resource_ref(
                    resource_type="channel_config", tenant_id=tenant_id
                ),
            )
        )
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **Read the binding first, so that the exception below can only
            # mean one thing** (independent review 2026-09-05, P2-2).
            # `binding.py` raises `ChannelBindingNotFound` from two places: the
            # binding really is not this tenant's (line 157), and a conversation
            # row carries no aggregate document (line 304, its fail-closed
            # branch). The second is a **data-integrity fault**, and answering it
            # with 404 "not found" sends an operator looking for a binding that
            # is sitting right there -- with `retryable: false` on it, which
            # reads as "your request is wrong, stop retrying".
            #
            # The review's evidence is that this is reachable today, not
            # theoretical: the first draft of this batch's own test hit exactly
            # that 404 with a naively seeded conversation, and it was mistaken
            # for a broken fixture. T115 stays open because the real remedy is a
            # separate exception type in `binding.py` -- T15's code, with its own
            # review history, and widening a route batch into it is how batches
            # stop being reviewable. This is the part that can be done **without
            # touching `binding.py`**: once the binding is known to exist, the
            # only `ChannelBindingNotFound` left is the fail-closed one.
            #
            # Two transactions, so in principle the binding could vanish in
            # between. Nothing deletes bindings today, and if something ever
            # does, the behaviour degrades to exactly what it was before.
            visible = connection.execute(
                "SELECT 1 FROM channel_bindings WHERE tenant_id = %s AND config_id = %s",
                (tenant_id, config_id),
            ).fetchone()
        if visible is None:
            raise HTTPException(
                status_code=404, detail="CHANNEL_BINDING_OR_RELEASE_NOT_FOUND"
            )
        try:
            result = repin_channel_binding(
                database=database,
                tenant_id=tenant_id,
                config_id=config_id,
                release_id=command.release_id,
                repinned_by=context.subject.id,
                correlation_id=context.correlation_id,
                repin_live_conversations=command.repin_live_conversations,
            )
        except ProfileReleaseNotFound as error:
            # The release is not this tenant's -- or does not exist. **The same
            # code as the missing binding above**, for the reason `GET
            # /tenants/current` gives: a caller must not be able to tell "not
            # yours" from "not there", or the route becomes a cross-tenant
            # existence oracle.
            raise HTTPException(
                status_code=404, detail="CHANNEL_BINDING_OR_RELEASE_NOT_FOUND"
            ) from error
        except ChannelBindingDisabled as error:
            # **409, the same code and the same reasoning as
            # `PATCH /channel-bindings/{config_id}`** (T142). Not a 404: the
            # binding is there, and telling the caller it is not would send them
            # looking for something that is sitting in front of them -- the
            # mistake the repin path already made once with its two
            # `ChannelBindingNotFound` branches.
            raise HTTPException(
                status_code=409, detail="CHANNEL_BINDING_DISABLED"
            ) from error
        except ChannelBindingNotFound as error:
            # **Reachable only as the fail-closed branch**, because the binding
            # was read above. It means a conversation row has no aggregate
            # document -- a data-integrity fault, not a missing binding, so 5xx
            # and not 404. `repin_channel_binding` does its work in one
            # transaction, so nothing is half-written when this fires: the answer
            # was wrong, the data was not.
            #
            # The code is deliberately its own: an operator who sees
            # `CONVERSATION_DOCUMENT_MISSING` goes and looks at conversations.
            # **T115 remains the item that gives this its own exception type in
            # `binding.py`**; until then this route tells the two apart by what
            # it knows rather than by what it caught.
            raise HTTPException(
                status_code=500, detail="CONVERSATION_DOCUMENT_MISSING"
            ) from error
        response.headers["cache-control"] = "no-store, private"
        receipt = command_receipt(
            resource=resource_ref(
                resource_type="channel_config",
                resource_id=config_id,
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            # `succeeded`, not `accepted`: the binding is durable before this
            # returns. `accepted` would promise a worker that does not exist.
            status="succeeded",
        ).as_dict()
        # **Two layers, and the receipt is one of them** (ADR-0050 §二, from this
        # batch's independent review P1-1). The first version returned
        # `{**receipt, …six domain fields}`, and `command-receipt.schema.json` is
        # `additionalProperties: false` -- so a caller who reasonably validated
        # the body as a receipt got a rejection. The contract is not widened to
        # fit the implementation; the implementation moves.
        return {
            "receipt": receipt,
            "result": {
                "changed": result.changed,
                "release_id": str(result.release.release_id),
                "previous_release_id": str(result.previous_release_id),
                "conversations_repinned": len(result.conversations),
                "conversations_remaining": result.remaining_conversations,
                # **Tenant-level, and the name has to say so** (P3-1). pre-008
                # rows carry no binding, so every binding in this tenant reports
                # the same number: it reads "this tenant still has n
                # conversations older than links are", **not** "this binding has
                # n left to move". Three numbers side by side are otherwise read
                # as three numbers on one scale, and a comment cannot fix a name
                # that is standing next to two others.
                "tenant_conversations_unlinked": result.unlinked_conversations,
            },
        }

    @app.exception_handler(AuthorizationError)
    def _authorization_denied(request: Request, exc: Exception) -> JSONResponse:
        """§3.3: a deny is 403 with a machine code, on every route.

        `/auth/me` had no `except` at all, so `require_allowed` raising went out
        as **500 Internal Server Error** -- and this batch widened the set of
        denials that reach it (`ACTION_UNKNOWN`, `TENANT_SCOPE_NOT_PERMITTED`,
        `RESOURCE_TENANT_MISMATCH`). Registering one handler rather than adding
        a second `try` is the shape that survives 70 more routes: a route added
        without an `except` is then still correct. (Independent review P2-3.)

        The body carries the reason code and nothing else -- no tenant, no
        resource id -- because `AuthorizationError` is raised for cross-tenant
        misses too, and §3.3 forbids leaking cross-tenant existence.
        """

        # **`str(exc)` is not a machine code most of the time.** Of the seven
        # places `AuthorizationError` is raised, one carries `decision.reason_code`
        # and six carry prose. Passing prose through fails the envelope's
        # `^[A-Z][A-Z0-9_]*$`, which raises inside this handler and lands the
        # caller on a **500 with `retryable: true`** -- told to retry a request
        # that will never succeed -- where §3.3 requires 403 with a machine code
        # (independent review 2026-09-05, P2-3).
        #
        # `_http_error` two handlers up already normalises exactly this risk.
        # Two handlers, one guard, was the asymmetry.
        raw = str(exc)
        head, separator, detail = raw.partition(":")
        field_errors: list[dict[str, str]] = []
        if separator and MACHINE_CODE.fullmatch(head):
            # `OBLIGATION_NOT_DISCHARGED:audit,redact` -- the token is the code,
            # and what was not discharged belongs in `field_errors` rather than
            # glued into it.
            code = head
            field_errors = [
                {"field": item.strip(), "code": head}
                for item in detail.split(",")
                if item.strip()
            ]
        elif MACHINE_CODE.fullmatch(raw):
            code = raw
        else:
            code = "AUTHORIZATION_DENIED"
        return _envelope(
            code=code, request=request, status_code=403, field_errors=field_errors
        )

    @app.get(
        "/auth/me",
        # **响应形状进 OpenAPI，只做文档**（ADR-0142 决策 1）。
        # 不是 `response_model` —— 那个会过滤掉模型里没有的键，
        # 于是一个写漏了字段的模型会**安静地把那个字段删掉**。
        # 这里运行期行为一字不改；形状对不对由
        # `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/auth/me"),
    )
    def me(context: AuthContext = Depends(authenticated)) -> dict[str, object]:  # noqa: B008
        """**T125, answered.** Reading who you are needs no permission.

        This route used to authorize `session.read`, and `permissions` defaults
        to `'[]'`, so a correctly created employee logged in and then could not
        read their own identity -- while every client calls `/auth/me`
        immediately after login. B1 #14's gate found it; its independent review
        put the argument for this answer plainly: the route reads **the subject
        that arrived in the request**, crosses no tenant data, and guarding it
        with a permission means a correctly created account cannot bootstrap.
        The user ruled for it on 2026-09-05.

        The catalogued action is `session.read_own`, whose verb sits outside
        `ACTION_VERBS` on purpose -- so it can never be granted, listed by
        `GET /permissions`, or accepted by `PUT /roles/{id}/permissions`. That
        is the same shape `employee.change_own_password` uses (ADR-0052 §四.3),
        and the two now form one small family: **your own identity and your own
        credential are reachable by being the subject, not by holding
        something.** `session.read` is gone from the catalog rather than left
        behind: an action no route asks for is a permission an administrator can
        grant and nobody can use, which is T98's failure exactly, and it was
        seeded into two system roles.

        What it still cannot do is tell you anything about anybody else -- every
        field below comes out of the token.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        return {
            "employee_id": str(context.subject.id),
            "tenant_id": str(tenant_id),
            "permissions": sorted(context.permissions),
            "correlation_id": str(context.correlation_id),
        }

    @app.post("/ws/tickets")
    def issue_ticket(
        command: TicketRequest,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        # Was `websocket.connect`, which is not a `<resource>.<verb>` action at
        # all -- `connect` is not one of §3.2's verbs, and the route manifest has
        # said `websocket_ticket.create` all along (T36).
        require_allowed(
            authorize(
                context=context,
                action="websocket_ticket.create",
                resource=tenant_resource_ref(
                    resource_type="websocket_ticket", tenant_id=tenant_id
                ),
            )
        )
        if command.origin not in resolved.allowed_ws_origins:
            raise HTTPException(status_code=403, detail="ORIGIN_NOT_ALLOWED")
        allowed_topics = {"conversation.events"}
        if not set(command.topics).issubset(allowed_topics):
            raise HTTPException(status_code=403, detail="TOPIC_NOT_ALLOWED")

        raw_ticket = f"{tenant_id}.{new_write_only_token()}"
        ticket_id = uuid4()
        # The one site that was already cross-clock before this change: written
        # here from the process clock, and claimed with `t.expires_at > now()`
        # on the database's. The INSERT now writes it and returns it, so the
        # value handed to the caller is the one the claim will actually test.
        ticket_ttl_seconds = resolved.ws_ticket_ttl_seconds
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            ticket_row = connection.execute(
                """
                INSERT INTO ws_tickets(
                    tenant_id, id, token_hash, employee_id, session_id,
                    credential_version, origin, topics, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    now() + %s * interval '1 second'
                )
             RETURNING expires_at
                """,
                (
                    tenant_id,
                    ticket_id,
                    sha256_token(raw_ticket),
                    context.subject.id,
                    context.session_id,
                    context.subject.credential_version,
                    command.origin,
                    Jsonb(command.topics),
                    ticket_ttl_seconds,
                ),
            ).fetchone()
            # An unconditional INSERT ... RETURNING yields exactly one row; the
            # assert is here so the type is narrowed at the point of use rather
            # than by a 500 branch that can never be taken.
            assert ticket_row is not None
            expires_at = ticket_row["expires_at"]
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type, resource_id,
                    correlation_id, reason_code, evidence
                ) VALUES (
                    %s, %s, %s, 'websocket_ticket.create', 'ws_ticket',
                    %s, %s, 'TICKET_ISSUED', %s
                )
                """,
                (
                    tenant_id,
                    uuid4(),
                    context.subject.id,
                    ticket_id,
                    context.correlation_id,
                    Jsonb({"origin": command.origin, "topics": command.topics}),
                ),
            )
        response.headers["Cache-Control"] = "no-store, private"
        return {
            "ticket": raw_ticket,
            "expires_at": expires_at.isoformat(),
            "topics": command.topics,
        }

    def _release_projection(row: dict[str, object]) -> dict[str, object]:
        """Metadata only. The document is deliberately not in the list.

        A release document is the whole merchant profile -- prompts, materials,
        time rules. `GET /profiles/releases` is a projection for an operator
        asking "what is published here", and the freeze does not grant a
        single-release read route in M1a. Returning the document from a list
        would decide that question by accident, which is exactly what the route
        freeze exists to prevent.
        """

        return {
            "release_id": str(row["id"]),
            "profile_id": row["profile_id"],
            "version": row["version"],
            "content_hash": row["content_hash"],
        }

    @app.get(
        "/tenants/current",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/tenants/current"),
    )
    def current_tenant(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #7 (`C0A-006` split): *this* tenant, not a list.

        The old system had an agent read the tenant **list** and take the first
        row to work out which tenant it was in -- the over-broad semantics §7.2
        froze deliberately. A read model for the caller's own tenant answers the
        question the page actually asks, and cannot answer any other: the scope
        comes from the token, never from a parameter.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="tenant.read",
                resource=tenant_resource_ref(resource_type="tenant", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                "SELECT id, code, status, created_at FROM tenants WHERE id = %s",
                (tenant_id,),
            ).fetchone()
        if row is None:
            # RLS is what makes this reachable at all: a token whose tenant no
            # longer exists (or is no longer visible) must not be told which of
            # the two happened.
            raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
        response.headers["cache-control"] = "no-store, private"
        return {
            "tenant_id": str(row["id"]),
            "code": row["code"],
            "status": row["status"],
            "created_at": _iso_timestamp(row["created_at"]),
        }

    @app.put(
        "/tenants/current/manager-contact",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("PUT", "/tenants/current/manager-contact"),
    )
    def write_manager_contact(
        command: WriteManagerContactRequest,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #31 (ADR-0104): the writer `012` never had.

        **The gap this closes is four days old and was registered twice.**
        `tenant_manager_contacts` has had `SELECT, INSERT, UPDATE` granted to
        `fastagent_app` since `012` -- permissions laid down for a configuration
        route that was never built -- and **zero writers**: two readers
        (`digest.py`, `pipeline.py`), no route, no script, no seed. ADR-0078
        ruled in M1a that the deployment would write it by hand, and wrote down
        the cost: a tenant nobody configured is told nothing, and **the system
        does not error**. In M1a that cost was free, because K17 had moved the
        notification surface out and nothing was delivered at all. B1-5 makes
        delivery real, which is the reopening condition ADR-0078 §四 wrote for
        itself, and the user reopened it on 2026-09-08.

        **`tenant.update` is not this action** -- the same sentence
        `PUT /control-policy` carries, for the same reason. ADR-0075 put
        `tenant.update` in `platform_only_actions`: it is what disables a
        tenant, and only the deployment may grant it. This is a switch the
        merchant owns, so it is its own resource and its own action. Choosing
        `tenant.update` here would have shipped a route no merchant could ever
        call, and **no gate would have said so** -- it is the zero-caller family
        wearing a different hat.

        **`PUT` on a singleton, not `POST` to a collection**: `012`'s primary
        key is the tenant, one row per tenant, so there is nothing to enumerate
        and nothing to address by id.

        **What this route still does not do** (ADR-0104 §五): there is no `GET`,
        so a caller cannot read back what is configured **later**, and nothing
        here makes an *unconfigured* tenant visible. The second is the failure
        mode that matters -- silence -- and it belongs to whoever makes delivery
        real. What the answer *does* carry is the triple this call just stored
        (independent review 2026-09-08, P3-3): echoing what was written is not a
        read surface -- it tells the caller what this request did, which is the
        one question no `GET` is needed to answer, and it is what the template
        route `PUT /control-policy` does with the policy it just stored.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        # **The decision is kept, not thrown away** (independent review
        # 2026-09-08, P3-3). `authz.py` hangs an `audit` obligation on every
        # allow, and `require_obligations_discharged` below is what turns
        # "succeed now, audit later" into a failure instead of a habit. The
        # first version called `require_allowed(authorize(...))` inline, which
        # audits correctly today and has nothing that goes red the day somebody
        # edits this block.
        decision = authorize(
            context=context,
            action="tenant_manager_contact.update",
            resource=tenant_resource_ref(
                resource_type="tenant_manager_contact", tenant_id=tenant_id
            ),
        )
        require_allowed(decision)
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **The binding is checked before the insert, and the foreign key
            # stays** -- the pattern `POST /conversations/{id}/manual-tasks`
            # uses for its owner, and for the same reason: the key is what makes
            # a cross-tenant write impossible, while the pre-check is what turns
            # "there is no such binding" into a sentence naming the field
            # instead of a 500 naming a constraint.
            #
            # A caller reaches this honestly: `config_id` is copied from
            # `GET /channel-bindings`, and a binding can be retired or
            # re-pinned between the copy and the write. The first version of
            # this route let that surface as `INTERNAL_ERROR` with
            # `retryable: true`, which is false -- retrying cannot make a
            # binding exist (independent review 2026-09-08, P1-1).
            known = connection.execute(
                """
                SELECT 1 FROM channel_bindings
                 WHERE tenant_id = %s AND config_id = %s
                """,
                (tenant_id, command.config_id),
            ).fetchone()
            if known is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "UNKNOWN_CHANNEL_BINDING",
                        "message": "no channel binding with this config_id",
                        "field_errors": [
                            {"field": "config_id", "code": "UNKNOWN_CHANNEL_BINDING"}
                        ],
                    },
                )
            connection.execute(
                """
                INSERT INTO tenant_manager_contacts(
                    tenant_id, channel, config_id, external_user_id, updated_by
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                   SET channel = excluded.channel,
                       config_id = excluded.config_id,
                       external_user_id = excluded.external_user_id,
                       -- **The three columns the first version forgot**
                       -- (independent review 2026-09-08, P2-1). `updated_at`'s
                       -- `DEFAULT now()` fires on INSERT only, so without this
                       -- line it froze at the first write for ever; `updated_by`
                       -- was never written at all, though this route has the
                       -- subject in hand and puts it in the audit row.
                       --
                       -- **`display_name` is cleared rather than kept**: the
                       -- second write is somebody replacing the person, and a
                       -- new address under the old name is the shape T118 and
                       -- T193 are about -- a field that is present, meaningful,
                       -- and saying something untrue. Nothing writes it today;
                       -- clearing it keeps it empty rather than stale.
                       display_name = NULL,
                       updated_at = now(),
                       updated_by = excluded.updated_by
                """,
                (
                    tenant_id,
                    command.channel,
                    command.config_id,
                    command.external_user_id,
                    context.subject.id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events(
                    tenant_id, id, actor_id, action, resource_type,
                    resource_id, correlation_id, reason_code, evidence
                ) VALUES (%s, %s, %s, 'tenant_manager_contact.update',
                          'tenant_manager_contact', %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    context.subject.id,
                    tenant_id,
                    context.correlation_id,
                    "MANAGER_CONTACT_SET",
                    # **The channel and the config, not the user id.** Which
                    # channel a merchant routes notifications through is
                    # configuration; `external_user_id` identifies a person at
                    # the provider, and an audit row is read by more people than
                    # the row it describes.
                    Jsonb({"channel": command.channel, "config_id": command.config_id}),
                ),
            )
            # **Before the commit, not after** -- the whole point of §3.1 is
            # that an operation which cannot discharge its obligations does not
            # succeed. The audit row two statements up is the discharge.
            require_obligations_discharged(decision, frozenset({"audit"}))
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="tenant_manager_contact",
                    resource_id=str(tenant_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            # **All three fields, echoed** (independent review 2026-09-08,
            # P3-3). The first version answered `{tenant_id, channel}` -- one of
            # the three things it was given -- and ADR-0104 §五.1 deliberately
            # builds no `GET`, so a merchant who mistyped an `external_user_id`
            # had no surface anywhere in the system that could tell them what
            # landed. The value is not a secret from the person who just sent
            # it; what stays out of the *audit* row (which more people read
            # than this answer) is unchanged.
            "result": {
                "tenant_id": str(tenant_id),
                "channel": command.channel,
                "config_id": command.config_id,
                "external_user_id": command.external_user_id,
            },
        }

    @app.get(
        "/employees",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/employees"),
    )
    def list_employees(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #13 (`C0A-017`): tenant scope comes from the context.

        `Page<T>` again, and the projection is deliberately narrow: identity,
        status and permission **count**. Not the permission list -- a directory
        page does not need to publish who can do what, and the role surface that
        would present it properly (B1 #22-#26) is not built.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.list",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT id, email, status, jsonb_array_length(permissions) AS permission_count,
                       purged_at, created_at
                  FROM employees WHERE tenant_id = %s ORDER BY created_at, id
                 LIMIT %s
                """,
                (tenant_id, MAX_PAGE_ITEMS + 1),
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        return _page_or_refuse(
            [
                {
                    "employee_id": str(row["id"]),
                    "email": row["email"],
                    "status": row["status"],
                    "permission_count": int(row["permission_count"]),
                    # **T155.** `026`'s column comment says this state 「不可从
                    # 墓碑地址的形状反推，也不得那样读」 -- and until this key
                    # existed, that reading was the only one available to a
                    # client: 「已 purge」 and 「被停用且邮箱看着奇怪」 arrived
                    # identical. The registration's reopen condition was written
                    # as 「出现第一个读者时（控制面前端渲染员工列表）」, and this
                    # batch is that reader.
                    #
                    # **A timestamp, not a boolean**: 「when」 answers 「whether」
                    # and a boolean does not answer 「when」. `null` for a row
                    # nobody purged is an answer, not an absence.
                    "purged_at": _iso_timestamp_or_none(row["purged_at"]),
                }
                for row in rows[:MAX_PAGE_ITEMS]
            ],
            has_more=len(rows) > MAX_PAGE_ITEMS,
        )

    @app.get(
        "/employees/stats",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/employees/stats"),
    )
    def employee_stats(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #20 (`C0A-022`): a tenant-scoped projection.

        Counts, not rows. The one number worth its own field is `disabled`: an
        operator asking "how many people can still get in" is asking about the
        difference between the two, and a page that reported only a total would
        make them go and count.

        **Declared before `/employees/{employee_id}` will be** -- FastAPI matches
        in declaration order, so a future by-id route must come after this one or
        `stats` becomes an employee id. The same trap the channel sink's by-key
        lookup already carries a guard for.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.read",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'active') AS active,
                       count(*) FILTER (WHERE status = 'disabled') AS disabled
                  FROM employees WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - an aggregate always returns a row
            raise HTTPException(status_code=500, detail="EMPLOYEE_STATS_UNAVAILABLE")
        response.headers["cache-control"] = "no-store, private"
        return {
            "total": int(row["total"]),
            "active": int(row["active"]),
            "disabled": int(row["disabled"]),
            "as_of": datetime.now(UTC).isoformat(),
        }

    @app.get(
        "/employees/{employee_id}/stats",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/employees/{employee_id}/stats"),
    )
    def employee_detail_stats(
        employee_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #21 (`C0A-023`): a typed projection for one employee.

        `C0A-023` says only "typed projection + employee.read authorization",
        and the page that used the untyped original does not exist in this
        repository, so the field list cannot be read off a consumer. It is
        therefore chosen from what the database can actually answer today, and
        every field names its source:

        * `status` and `permission_count` -- `employees`, the same two the
          directory (`GET /employees`) already publishes, so one employee's row
          means the same thing on both pages;
        * `active_session_count` and `last_session_started_at` --
          `employee_sessions`. These are the by-id half of the question the
          tenant projection answers in aggregate ("how many people can still
          get in"): for one person it is "can *they* get in right now, and when
          did they last".

        **`role_count` is deliberately not here, and the omission is the
        considered part of this route.** `employee_roles` exists (migration
        `015`) and the count would be one join away -- but a role grants
        nothing to nobody until freeze B1 #16 wires assignment into
        `authorize()`, which still reads `employees.permissions` alone (**T92**).
        A stats page reporting `role_count: 3` next to `permission_count: 0`
        states, in the only language a projection has, that this person has
        capabilities they do not have. That is precisely the failure T92 is
        registered for, one level louder. The field belongs here the day roles
        take effect, not before.

        Path shape note: this is three segments, so it cannot shadow
        `/employees/stats`; the hazard the sibling's docstring warns about is a
        future two-segment `/employees/{employee_id}`, which must still be
        declared after it.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.read",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT e.id,
                       e.email,
                       e.status,
                       jsonb_array_length(e.permissions) AS permission_count,
                       e.created_at,
                       -- `now()` on purpose (independent review F4): this
                       -- counts sessions for a display, it does not admit
                       -- anyone, and a statistic that shifts by the duration of
                       -- its own transaction is not a statistic anyone reads.
                       count(s.id) FILTER (
                           WHERE s.revoked_at IS NULL AND s.expires_at > now()
                       ) AS active_session_count,
                       max(s.created_at) AS last_session_started_at
                  FROM employees e
                  LEFT JOIN employee_sessions s
                    ON s.tenant_id = e.tenant_id AND s.employee_id = e.id
                 WHERE e.tenant_id = %s AND e.id = %s
                 GROUP BY e.id, e.email, e.status, e.permissions, e.created_at
                """,
                (tenant_id, employee_id),
            ).fetchone()
        if row is None:
            # Same reasoning as `GET /tenants/current`: a row this tenant cannot
            # see and a row that does not exist must be indistinguishable from
            # the wire, or the 404/403 split becomes a cross-tenant existence
            # oracle. RLS makes both cases arrive here identically.
            raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
        last_started = row["last_session_started_at"]
        response.headers["cache-control"] = "no-store, private"
        return {
            "employee_id": str(row["id"]),
            "email": row["email"],
            "status": row["status"],
            "permission_count": int(row["permission_count"]),
            "active_session_count": int(row["active_session_count"]),
            "last_session_started_at": (
                _iso_timestamp(last_started) if last_started is not None else None
            ),
            "created_at": _iso_timestamp(row["created_at"]),
            "as_of": datetime.now(UTC).isoformat(),
        }

    @app.patch(
        "/employees/{employee_id}",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("PATCH", "/employees/{employee_id}"),
    )
    def update_employee(
        employee_id: UUID,
        command: UpdateEmployeeRequest,
        response: Response,
        # Required by parity 5.2 ("所有 create/update command 默认需要
        # `Idempotency-Key`") and enforced by being declared, even though the
        # response no longer echoes it: the header's job is to be *demanded*,
        # and a retry of this route is harmless because it is a full overwrite
        # of the fields it names.
        idempotency_key: IdempotencyKey,  # noqa: ARG001
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #15 (`C0A-019` split): profile fields only.

        The split is the design. `C0A-019` was one endpoint that wrote whatever
        the client sent; the freeze cut it into this route and three others
        (#16 roles, #17 password, #18 deactivate) so that changing what somebody
        *may do* can never ride along with changing what they are *called*.

        **Three defences, not one, and they are deliberately redundant:** the
        request model forbids unknown keys, the statement below names its three
        columns, and migration 018 grants `UPDATE` on exactly those three
        columns and no others. The third is the one that still holds if somebody
        later widens the first two without noticing what else lives on this row.

        `display_name` cannot be cleared -- the column is `NOT NULL` because the
        pages that name a person have no fallback that is not a uuid or an email
        address (ADR-0048 K7). The two optional fields can: sending an explicit
        `null` clears them, not sending the key leaves them alone.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.update",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        # **`purged_at IS NULL` in the statement below** (independent review
        # 2026-09-05, F4). B1 #19 overwrites exactly these three columns to
        # destroy an identity, and migration 026's comment says the values left
        # behind are ones nobody holds -- **that sentence was false after a
        # single PATCH**, because this route had no guard at all. #16 checks
        # `status != 'active'` and #17 checks `status = 'active'`; this was the
        # one employee-writing route that checked nothing.
        #
        # A missing row and a purged one answer the same 404 below, which is the
        # right answer for both: to this route a tombstone is not an employee.
        submitted = {
            field: getattr(command, field)
            for field in ("display_name", "employee_no", "department")
            if field in command.model_fields_set
        }
        if not submitted:
            raise HTTPException(status_code=422, detail="NO_FIELDS_SUBMITTED")
        if "display_name" in submitted and submitted["display_name"] is None:
            # The column is NOT NULL. Refusing here names the reason; letting it
            # reach the database would answer with a constraint name.
            raise HTTPException(status_code=422, detail="DISPLAY_NAME_REQUIRED")
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                UPDATE employees
                   SET display_name = coalesce(%s, display_name),
                       employee_no = CASE WHEN %s THEN %s ELSE employee_no END,
                       department = CASE WHEN %s THEN %s ELSE department END
                 WHERE tenant_id = %s AND id = %s AND purged_at IS NULL
                RETURNING id, email, status, display_name, employee_no, department
                """,
                (
                    submitted.get("display_name"),
                    "employee_no" in submitted,
                    submitted.get("employee_no"),
                    "department" in submitted,
                    submitted.get("department"),
                    tenant_id,
                    employee_id,
                ),
            ).fetchone()
            if row is not None:
                # **T128's first call site.** `employee.update` is tenant-wide,
                # so whoever holds it can rename anybody in the tenant, and
                # until now nothing recorded who did. The freeze writes
                # 「授权变更必审计」 for #16 and is silent about #15, so that
                # batch was not in breach -- but T117's scope was widened to
                # cover it and T128 carried the remainder with "B1 #16 落地时"
                # written as its expiry. This is that batch.
                #
                # **The evidence names the fields, not the values.** Which
                # columns a person changed is what an auditor needs; a display
                # name is somebody's name, and this table is append-only.
                _audit_employee_event(
                    connection,
                    tenant_id=tenant_id,
                    actor_id=context.subject.id,
                    subject_employee_id=employee_id,
                    action="employee.update",
                    correlation_id=context.correlation_id,
                    reason_code="EMPLOYEE_PROFILE_UPDATED",
                    evidence={"fields": sorted(submitted)},
                )
        if row is None:
            # Same reasoning as the two stats routes: a row this tenant cannot
            # see and a row that does not exist are one answer, or the route
            # becomes a cross-tenant existence oracle.
            raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
        response.headers["cache-control"] = "no-store, private"
        # **No `CommandReceipt` here** (ADR-0050 §三, independent review P1-1).
        # The first version returned `{**receipt, "employee": {...}}`, which is a
        # *superset* of the receipt contract -- and that contract is
        # `additionalProperties: false`, so the body a caller would reasonably
        # validate as a receipt does not validate. Two shapes were available;
        # this is the one parity 5.2 asks for: profile updates are not in the
        # list of commands that must carry a receipt (money, sending, purge,
        # restore, bulk state change, external channel), and the cluster's only
        # existing update route -- `PUT /roles/{role_id}/permissions` -- returns
        # a projection plus `correlation_id`. Adding a receipt where the rule
        # does not ask for one is what makes "receipt" stop meaning anything.
        #
        # The stored values are echoed because "what did my partial update
        # actually leave here" is the question a partial update creates.
        return {
            "employee_id": str(row["id"]),
            "email": row["email"],
            "status": row["status"],
            "display_name": row["display_name"],
            "employee_no": row["employee_no"],
            "department": row["department"],
            "correlation_id": str(context.correlation_id),
        }


    @app.post(
        "/employees/{employee_id}/deactivate",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/employees/{employee_id}/deactivate"),
    )
    def deactivate_employee(
        employee_id: UUID,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #18 (`C0A-021` + parity §6.4): deactivate, never delete.

        parity §6.4 is explicit that the old `DELETE` does not exist in v2, and
        that deactivation does three things: **revoke sessions immediately**,
        record `disabled_at`, and start the retention clock at
        `purge_eligible_at = disabled_at + 90 days`. Purge is a **different
        route, a different action and a different test set** -- §6.4 forbids one
        boolean switching between them, which is why this route has no body at
        all and no `purge` flag anywhere near it.

        **It is not idempotent in the way a retry wants, and says so.** A second
        call on an already-disabled employee answers 409 rather than silently
        re-stamping `disabled_at` -- moving that timestamp would move the
        retention clock, and a retry must not quietly extend how long somebody's
        data is kept.

        **So the `Idempotency-Key` this route demands has nowhere to be used**,
        and the tension is worth naming rather than leaving for a caller to
        discover: there is no command-deduplication store anywhere in the repo,
        so a genuine retry -- the first 200 lost on the wire -- gets the 409 and
        cannot get the original receipt back. Until that store exists, **the
        caller's answer is `GET /employees/{employee_id}`**: `status` is the
        fact, the 409 is not evidence of anything except that somebody got there
        first. Registered as **T121**, because about 70 later routes inherit it.

        The session revocation is the same statement `POST /auth/logout` uses,
        with one difference that is the reason T117 exists: **the actor is not
        the subject**. An audit row records both.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.update",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        if employee_id == context.subject.id:
            # Deactivating yourself would revoke the session making the request
            # and leave the tenant one administrator short with no way back in
            # -- the last-administrator shape CP-3 guards for roles. Refusing is
            # not a courtesy: it is the cheapest version of that guard, and it
            # does not need to know anything about who else holds what.
            raise HTTPException(status_code=409, detail="CANNOT_DEACTIVATE_SELF")
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # **T120, and the reason it is here rather than in #18** (independent
            # review 2026-09-05, P1-1). `_administrator_exists` counts *active*
            # people, so deactivation is precisely an action that can flip it
            # from true to false: somebody holding only `employee.update` -- not
            # an administrator at all -- could disable the tenant's last one, and
            # nothing afterwards could grant a role or edit a permission set.
            # There is no route back to `active` either (#19 is not built), so
            # the state is terminal.
            #
            # The guard was unreachable before B1 #16 in the sense that mattered:
            # "is there another administrator" had no answer while
            # `employee_roles` was empty. T120's expiry condition was "角色接线
            # T92 落地时", which is this batch, and the same lock and the same
            # predicate the role routes use are now in hand. Refusing only the
            # true->false transition means every existing deactivation case is
            # untouched -- their callers do not hold `role.update`.
            _serialise_role_changes(connection, tenant_id)
            had_administrator = _administrator_exists(connection, tenant_id=tenant_id)
            row = connection.execute(
                """
                UPDATE employees
                   SET status = 'disabled',
                       disabled_at = now(),
                       purge_eligible_at = now() + interval '90 days'
                 WHERE tenant_id = %s AND id = %s AND status = 'active'
                RETURNING id, email, status, disabled_at, purge_eligible_at
                """,
                (tenant_id, employee_id),
            ).fetchone()
            if row is None:
                # Either they do not exist here, or they are already disabled.
                # Told apart **inside the transaction**, so the answer cannot be
                # overtaken by a concurrent deactivation between two statements.
                existing = connection.execute(
                    "SELECT status FROM employees WHERE tenant_id = %s AND id = %s",
                    (tenant_id, employee_id),
                ).fetchone()
                if existing is None:
                    raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
                raise HTTPException(status_code=409, detail="EMPLOYEE_ALREADY_DISABLED")
            if had_administrator and not _administrator_exists(
                connection, tenant_id=tenant_id
            ):
                # Asked after the write for the reason the two role routes give:
                # the union of a column and a set of roles is a question for the
                # database, not an effect to predict in the application. The
                # exception unwinds the transaction, so the disable never lands.
                raise HTTPException(status_code=409, detail="LAST_ADMINISTRATOR_PROTECTED")
            revoked = connection.execute(
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND employee_id = %s AND revoked_at IS NULL
                RETURNING id
                """,
                (tenant_id, employee_id),
            ).fetchall()
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=employee_id,
                action="employee.update",
                correlation_id=context.correlation_id,
                reason_code="EMPLOYEE_DEACTIVATED",
                evidence={
                    "sessions_revoked": len(revoked),
                    "disabled_at": _iso_timestamp(row["disabled_at"]),
                    "purge_eligible_at": _iso_timestamp(row["purge_eligible_at"]),
                },
            )
        response.headers["cache-control"] = "no-store, private"
        # **`{"receipt": …, "result": …}`** (ADR-0050 §三). This route *does*
        # carry a receipt -- it revokes sessions in bulk, which is one of the
        # cases parity §5.2 names -- and the receipt therefore has to be a
        # receipt: `command-receipt.schema.json` is `additionalProperties:
        # false`, so domain fields alongside it produce a body that a caller
        # cannot validate. Its neighbour `PATCH /employees/{id}` carries no
        # receipt at all, and that is not an inconsistency: the test is parity
        # §5.2's list, not what the previous route happened to do.
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="employee",
                    resource_id=str(row["id"]),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "employee_id": str(row["id"]),
                "status": row["status"],
                "disabled_at": _iso_timestamp(row["disabled_at"]),
                # The retention clock, echoed because "when may this be purged"
                # is the question deactivation creates and the only place it is
                # answered.
                "purge_eligible_at": _iso_timestamp(row["purge_eligible_at"]),
                "sessions_revoked": len(revoked),
            },
        }


    @app.post(
        "/employees",
        status_code=201,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/employees"),
    )
    def create_employee(
        command: CreateEmployeeRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #14 (`C0A-018`): password write-only, never echoed.

        **The password appears in exactly two places and neither is readable**:
        the request body, and `hash_password`'s argument. It is not in the
        response, not in the audit evidence, not in an error message, and not in
        the `INSERT` -- the column holds the hash. `C0A-018`'s freeze note is
        one line ("密码只写、响应永不含密码") and this route is that line.

        **A new employee can do nothing yet, and that is not an oversight.**
        `permissions` defaults to `'[]'` and this route cannot write it --
        neither through the model (`extra="forbid"`) nor through the connection
        (migrations 020 and 021 grant `INSERT` on eight named columns, and that is
        not one of them). Freeze B1 #16 (`PUT /employees/{employee_id}/roles`) is
        the door to permissions, and it is not in this batch.

        **They can log in, and then they must change the password** -- that is
        the half that carries risk, and B1 #17 is where it stopped being a
        registration and became a column. The administrator chooses the initial
        password and hands it over through some channel outside this system, so
        ADR-0048 K8's sentence about reset passwords ("不强制改密等于让它长期
        有效") is true of this one word for word. **T124 was the registration of
        that gap and this line is its discharge**: the row is written with
        `must_change_password = true`, and until the person calls `POST
        /auth/change-password` every other route answers 403.

        409, not 422, for an email already used: the unique constraint does the
        refusing rather than a read-then-write, because two requests naming the
        same address can arrive together and a pre-check would let both through
        (the shape `POST /roles` uses, for the same reason).
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.create",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        employee_id = uuid4()
        # **Hashed before the transaction opens, not inside it** (independent
        # review 2026-09-05, P3-2). argon2 costs tens to hundreds of
        # milliseconds and about 64 MiB by design, and as an argument to
        # `connection.execute` it ran with the connection and the transaction
        # held open for all of it. `POST /auth/login` still does this and is not
        # changed here -- moving it on the route being written is free, moving
        # it on a merged one is a separate edit to a separate route.
        #
        # The only thing this route does with the password.
        password_hash = hash_password(command.password)
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                INSERT INTO employees(
                    tenant_id, id, email, password_hash,
                    display_name, employee_no, department,
                    -- **Named rather than left to the default** (T124, K8). The
                    -- column defaults to `false` because rows that predate it
                    -- carry no record of who set their password; every row this
                    -- route writes does carry that record, and it is "somebody
                    -- other than the owner". A default would make the flag a
                    -- fact about when the column was added rather than about
                    -- the password.
                    must_change_password
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (tenant_id, email) DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id,
                    employee_id,
                    command.email,
                    password_hash,
                    command.display_name,
                    command.employee_no,
                    command.department,
                ),
            ).fetchone()
            if row is not None:
                # **T128's second call site.** A route that creates an account
                # able to log in, and nothing recorded who created it. Written
                # inside the transaction, so a conflict that writes no employee
                # writes no audit row either.
                _audit_employee_event(
                    connection,
                    tenant_id=tenant_id,
                    actor_id=context.subject.id,
                    subject_employee_id=UUID(str(row["id"])),
                    action="employee.create",
                    correlation_id=context.correlation_id,
                    reason_code="EMPLOYEE_CREATED",
                    # The state the account starts in, which is the fact K8 makes
                    # load-bearing. Not the email, and obviously not the password.
                    evidence={"must_change_password": True},
                )
        if row is None:
            raise HTTPException(status_code=409, detail="EMPLOYEE_EMAIL_ALREADY_USED")
        response.headers["cache-control"] = "no-store, private"
        # A bare `CommandReceipt`, like the cluster's other two create routes
        # (`POST /roles`, `POST /profiles/releases`): the id is in
        # `resource_ref.resource_id` and there is no domain result to carry, so
        # there is nothing to nest. ADR-0050's `{"receipt": …, "result": …}` is
        # for commands that answer with something *besides* the receipt -- see
        # its §二: needing a receipt and needing a result are separate questions.
        #
        # **Echoing the row would be the mistake this route exists to avoid.**
        return command_receipt(
            resource=resource_ref(
                resource_type="employee",
                resource_id=str(row["id"]),
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict()

    @app.post(
        "/employees/{employee_id}/purge",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/employees/{employee_id}/purge"),
    )
    def purge_employee(
        employee_id: UUID,
        command: PurgeEmployeeRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #19 + parity §6.4, executed under **ADR-0056**.

        **Synchronous, and the receipt says `succeeded`.** §6.4 asks for an
        asynchronous receipt; the executor for one does not exist (`C13` --
        neither worker nor scheduler runs on Stage). ADR-0056 rules that the
        work here is *bounded* -- only four tables carry an employee's identity
        and M1a delivers no messages, so there is no author content to scrub --
        and that `accepted` would promise a worker that does not exist, which is
        the shape `PUT /channel-bindings/{id}/profile-release` already refused.
        **`succeeded` is the stronger claim, not the weaker one**: it says the
        work is done, not that it was queued.

        **The action is `employee.delete`, and that is a correction to ADR-0056.**
        The ADR wrote `employee.purge`; `purge` is not in `ACTION_VERBS`, so such
        an action can never be granted to a role (`assignable_permissions()`
        filters by spelling) and the route would be callable by nobody. What the
        ADR was ruling is unchanged -- **tenant-scoped, and its own action, not
        `employee.update`** -- which `employee.delete` delivers with a verb the
        closed set already has. Growing that set is ADR-0043's territory and not
        something a route batch decides. See ADR-0056's 补记.

        **Four guards, and the order is the answer to a different question each
        time**: not yourself; already purged; not disabled; under legal hold;
        not yet eligible. Each has its own code, because "409" alone tells an
        operator to retry and three of these will never succeed on retry.

        **`legal_hold` is read and never written** -- by this route or any other
        (**T118**: nothing in M1a can set it, so today the guard is a guard over
        a field that is always `false`). Migration 026 deliberately does not
        grant `UPDATE` on it: a route that can clear its own guard is not a
        guard.

        **`ws_tickets` is deliberately untouched, and that is worth stating**
        (independent review 2026-09-05, F10). ADR-0056 names four tables that
        carry an employee's identity and this route writes three of them. The
        fourth needs nothing: a ticket is exchanged only for an `active`
        employee whose `credential_version` still matches and whose session is
        unrevoked, and a purge breaks all three at once. Left to the next reader
        to re-derive, it looks like an omission.

        **The row survives.** `audit_events.actor_id`, `employee_sessions` and
        `employee_roles` all point at it, and §6.4 forbids deleting the audit.
        What is destroyed is the identity: the address, the names, and the
        credential. `subject_fingerprint` keeps the one question that outlives a
        purge -- whether two purged records were the same person.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee.delete",
                resource=tenant_resource_ref(resource_type="employee", tenant_id=tenant_id),
            )
        )
        if employee_id == context.subject.id:
            # Deactivation refuses this to avoid locking the tenant out; here the
            # reason is larger and simpler: the subject cannot be the one who
            # decides their own record is gone, and the session making the
            # request belongs to the record being destroyed.
            raise HTTPException(status_code=409, detail="CANNOT_PURGE_SELF")
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            subject = connection.execute(
                """
                SELECT email, status, legal_hold, purged_at, purge_eligible_at,
                       credential_version
                  FROM employees
                 WHERE tenant_id = %s AND id = %s
                   FOR UPDATE
                """,
                (tenant_id, employee_id),
            ).fetchone()
            if subject is None:
                raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
            # **Read under `FOR UPDATE`, so the four guards below cannot be
            # overtaken** by a concurrent purge or deactivation between the check
            # and the write. B1 #18 tells its two cases apart inside the
            # transaction for the same reason; this route has four.
            if subject["purged_at"] is not None:
                raise HTTPException(status_code=409, detail="EMPLOYEE_ALREADY_PURGED")
            if subject["status"] != "disabled":
                # §6.4 orders the two: deactivation starts the retention clock
                # and purge ends it. Purging a live account would skip the
                # ninety days it exists to observe.
                raise HTTPException(status_code=409, detail="EMPLOYEE_NOT_DISABLED")
            if bool(subject["legal_hold"]):
                raise HTTPException(status_code=409, detail="EMPLOYEE_UNDER_LEGAL_HOLD")
            # **Compared in SQL, against the clock that wrote it**
            # (independent review 2026-09-05, F8). `purge_eligible_at` is
            # written with Postgres `now()` by B1 #18; comparing it to the host's
            # `datetime.now(UTC)` puts a retention decision across two clocks.
            # The window is ninety days so nothing turns on milliseconds -- but
            # this repository spent a batch on that exact shape today
            # (`9caa3da`, the logout timestamp), and the reason it did is that a
            # comparison across two clocks is wrong in a way nobody notices
            # until it is.
            # **`now()` here rather than `clock_timestamp()`, on purpose.** The
            # rule this repository follows -- a predicate that decides whether
            # to *admit* something reads `clock_timestamp()`, a predicate that
            # only picks the rows a statement is about to stamp keeps `now()`
            # -- points at `clock_timestamp()` for this one: it admits an
            # erasure. It stays on `now()` for two reasons, and they belong in
            # the file rather than in a self-review: what the rule buys here is
            # one transaction's length against a *ninety day* window, which is
            # not "small" but irrelevant; and the tenant and employee guards
            # must stay literally identical, because a source assertion counts
            # this exact statement twice and the entire argument for touching
            # either of them is that a rule with one exception reads as a rule
            # with an exception (independent review 2026-09-07, P3).
            #
            # **`::timestamptz` on both parameters is load-bearing.**
            # `%s IS NOT NULL` gives PostgreSQL nothing to infer a type from:
            # psycopg sends `None` with OID 0, the statement cannot be planned
            # (`IndeterminateDatatype`), and a defined `409` becomes an
            # unhandled `500`. The cast makes the NULL row answer `due = false`
            # -- the same refusal the `is None` short-circuit used to give
            # before this comparison moved into SQL (same review, P2).
            eligible = connection.execute(
                "SELECT %s::timestamptz IS NOT NULL AND %s::timestamptz <= now() AS due",
                (subject["purge_eligible_at"], subject["purge_eligible_at"]),
            ).fetchone()
            # The `is None` half cannot happen -- a `SELECT` without a `FROM`
            # returns exactly one row -- and stays only because the row type is
            # optional. `not due` is the half that decides, and it now covers a
            # NULL `purge_eligible_at` too.
            if eligible is None or not eligible["due"]:
                raise HTTPException(status_code=409, detail="PURGE_NOT_YET_ELIGIBLE")
            # Generated and discarded inside the request, exactly as B2 #5 does
            # for a callback token: what lands in the column is a well-formed
            # hash of a password **nobody holds and nobody can present**.
            tombstoned_credential = hash_password(new_write_only_token())
            if not resolved.purge_fingerprint_key:
                # **Refuse, never fall back.** An unkeyed digest of
                # `tenant_id:email` is reversible by hashing a candidate address
                # list, which is precisely what T154 registered and §6.4
                # forbids. Falling back would produce a tombstone that looks
                # right and is not -- and a purge cannot be redone, because the
                # address it fingerprints is destroyed in the same statement.
                raise HTTPException(status_code=503, detail="PURGE_FINGERPRINT_KEY_MISSING")
            fingerprint = subject_fingerprint(
                key=resolved.purge_fingerprint_key,
                tenant_id=tenant_id,
                email=str(subject["email"]),
            )
            row = connection.execute(
                """
                UPDATE employees
                   SET email = %s,
                       display_name = %s,
                       employee_no = NULL,
                       department = NULL,
                       password_hash = %s,
                       credential_version = %s,
                       must_change_password = false,
                       purged_at = now(),
                       subject_fingerprint = %s
                 WHERE tenant_id = %s AND id = %s
                RETURNING id, purged_at
                """,
                (
                    # Unique per tenant by construction (the primary key is in
                    # it) and unmistakably not an address: `.invalid` is reserved
                    # by RFC 2606 precisely so that nothing can route to it.
                    # **This also releases the original address for reuse**,
                    # which is the half of T126 a purge is supposed to answer.
                    f"purged+{employee_id}@purged.invalid",
                    "(purged)",
                    tombstoned_credential,
                    int(subject["credential_version"]) + 1,
                    fingerprint,
                    tenant_id,
                    employee_id,
                ),
            ).fetchone()
            assert row is not None  # noqa: S101 -- selected FOR UPDATE above
            # Authorization is not history: a tombstone must not keep holding
            # roles. The audit row below is what records that they were held.
            removed_roles = connection.execute(
                """
                DELETE FROM employee_roles
                 WHERE tenant_id = %s AND employee_id = %s
                RETURNING role_id
                """,
                (tenant_id, employee_id),
            ).fetchall()
            revoked = connection.execute(
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND employee_id = %s AND revoked_at IS NULL
                RETURNING id
                """,
                (tenant_id, employee_id),
            ).fetchall()
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=employee_id,
                # **`employee.delete`, the action that was actually
                # authorised** (independent review 2026-09-05, F5). The first
                # version wrote `employee.purge` -- a string that is in no
                # catalog and can never be granted, which is the very thing this
                # batch corrected ADR-0056 about. Writing it into an append-only
                # table would have made this the one audit row whose `action`
                # matches nothing an operator can search for. `reason_code`
                # already says which command it was.
                action="employee.delete",
                correlation_id=context.correlation_id,
                reason_code="EMPLOYEE_PURGED",
                # **The reason is recorded; the fingerprint is not** (ADR-0055).
                # The reason passes all three of that criterion's tests: it is
                # the operator's own justification rather than the subject's
                # personal data, it is not a verifier, and **nothing else in the
                # system can answer "why"** -- the row it justifies has just had
                # its identity destroyed. The fingerprint fails the second test
                # by name (「token / hash / fingerprint / 签名」), and an
                # append-only table is the last place to put one.
                evidence={
                    "reason": command.reason,
                    "roles_removed": len(removed_roles),
                    "sessions_revoked": len(revoked),
                },
            )
        response.headers["cache-control"] = "no-store, private"
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="employee",
                    resource_id=str(employee_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                # ADR-0056: done when it returns.
                status="succeeded",
            ).as_dict(),
            "result": {
                "employee_id": str(employee_id),
                "purged_at": _iso_timestamp(row["purged_at"]),
                "roles_removed": len(removed_roles),
                "sessions_revoked": len(revoked),
            },
        }

    @app.put(
        "/employees/{employee_id}/roles",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("PUT", "/employees/{employee_id}/roles"),
    )
    def replace_employee_roles(
        employee_id: UUID,
        command: ReplaceEmployeeRolesRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #16 (`C0A-019` split): 角色变更单列，授权变更必审计.

        **This is the route migration 015 kept pointing at.** That migration
        created `roles` and `employee_roles` and said, in its own comment and in
        the table comments and in three route docstrings, that a role granted
        nothing to nobody until this existed. It exists now, and the sentence
        that makes it true is not here -- it is in `POST /auth/login`, which
        mints the token from **the union of `employees.permissions` and the
        permissions of the roles named here**. Registered as **T92**.

        **Its own action, not `employee.update`.** #15 (profile) and #18
        (deactivate) share `employee.update`, and mapping this onto it would
        mean that whoever may rename a colleague may also grant themselves every
        permission in the catalog. `employee_role.update` is therefore a
        separate grantable permission -- the freeze's "角色变更单列" is about
        exactly this, and it is the one place in the employee cluster where
        parity's "different action, different route, different tests" is
        delivered in full rather than traded away (ADR-0048 §二.1).

        **The set is replaced, not merged**, for the reason `PUT
        /roles/{id}/permissions` gives: a delta against an authorization set has
        an effect the client cannot predict, and the audit row recording it
        cannot be read later without the state it was applied to. An empty array
        is a legitimate submission and strips every role.

        **The target's sessions are revoked.** Permissions travel in the access
        token, minted at login, so without this a revoked role keeps working
        until the token expires -- which is the one direction that must not be
        slow. Revoking rather than bumping `credential_version`: the session row
        is what `authenticated` already re-reads on every request, and
        `credential_version` means "the credential changed", which is not what
        happened here.

        **The last administrator cannot be stripped**, including by themselves,
        and that guard is new in kind rather than in strength -- until this route
        existed there was no way to take `role.update` away from a *person*. See
        `_administrator_exists`.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee_role.update",
                resource=tenant_resource_ref(
                    resource_type="employee_role", tenant_id=tenant_id
                ),
            )
        )
        requested = sorted(set(command.role_ids))
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            # The same lock the role routes take, and **the reason CP-3's
            # re-review (R-4) asked for it here**: without it `DELETE
            # /roles/{id}`'s `ROLE_STILL_ASSIGNED` check races this route's
            # insert, and the foreign key catches it as a 500 rather than a 409.
            # The lock is a convention, not a mechanism -- nothing forces a new
            # route to take it -- which is why the assignment route taking it is
            # written into T92's landing list rather than left to be noticed.
            _serialise_role_changes(connection, tenant_id)
            target = connection.execute(
                "SELECT status FROM employees WHERE tenant_id = %s AND id = %s",
                (tenant_id, employee_id),
            ).fetchone()
            if target is None:
                raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
            if target["status"] != "active":
                # Granting roles to somebody who cannot log in is a write with
                # no reachable effect, and it would leave the tenant's
                # authorization state describing a person who is gone. Same
                # answer `POST /employees/{id}/password-reset` gives.
                raise HTTPException(status_code=409, detail="EMPLOYEE_DISABLED")
            known = {
                UUID(str(row["id"]))
                for row in connection.execute(
                    "SELECT id FROM roles WHERE tenant_id = %s", (tenant_id,)
                ).fetchall()
            }
            unknown = [role_id for role_id in requested if role_id not in known]
            if unknown:
                # **422 with the ids named**, the shape `_validated_permissions`
                # settled on: the caller can already list this tenant's roles
                # through `GET /roles`, so echoing which of their own
                # submissions were unrecognised reveals nothing they could not
                # read -- and swallowing it leaves them guessing.
                #
                # A role id belonging to **another tenant** arrives here as
                # unknown rather than as a 403, because RLS makes the read
                # tenant-scoped: the two cases are indistinguishable to this
                # route, which is what stops it being a cross-tenant existence
                # oracle.
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "ROLE_NOT_IN_TENANT",
                        "field_errors": [
                            {"field": str(role_id), "code": "ROLE_NOT_IN_TENANT"}
                            for role_id in unknown
                        ],
                    },
                )
            before = sorted(
                str(row["role_id"])
                for row in connection.execute(
                    "SELECT role_id FROM employee_roles WHERE tenant_id = %s"
                    " AND employee_id = %s",
                    (tenant_id, employee_id),
                ).fetchall()
            )
            # T161 needs the effective set *before* the write, and it has to be
            # read here rather than after: once the role rows change the old
            # union is gone, and this transaction is the only place both sides
            # can be seen consistently.
            permissions_before = _effective_permissions(
                connection, tenant_id=tenant_id, employee_id=employee_id
            )
            had_administrator = _administrator_exists(connection, tenant_id=tenant_id)
            # **The difference, not the whole set** (independent review
            # 2026-09-05, P2-1). Deleting everything and re-inserting `requested`
            # rewrote `granted_by` and `granted_at` on **every** role the person
            # holds, so a call that added one role restamped the rest with this
            # caller and this moment. Migration 022's column comment promises
            # that "who gave this person this role" survives on the row; what
            # the previous shape could actually answer was "who last touched
            # this person's role set", which is a weaker fact wearing the
            # stronger one's name.
            held = {UUID(role_id) for role_id in before}
            for role_id in sorted(held - set(requested)):
                connection.execute(
                    """
                    DELETE FROM employee_roles
                     WHERE tenant_id = %s AND employee_id = %s AND role_id = %s
                    """,
                    (tenant_id, employee_id, role_id),
                )
            for role_id in sorted(set(requested) - held):
                connection.execute(
                    """
                    INSERT INTO employee_roles(tenant_id, employee_id, role_id, granted_by)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (tenant_id, employee_id, role_id, context.subject.id),
                )
            if had_administrator and not _administrator_exists(
                connection, tenant_id=tenant_id
            ):
                # The whole transaction unwinds, so the delete above never
                # lands. Asked after the write for `PUT /roles/{id}/permissions`'s
                # reason: the union of a column and a set of roles is a question
                # for the database, not an effect to predict in the application.
                raise HTTPException(status_code=409, detail="LAST_ADMINISTRATOR_PROTECTED")
            # **T161: an assignment that only adds does not end the target's
            # sessions.** Until B1 #5 it had to -- permissions were minted at
            # login and never recomputed, so a new role could not take effect
            # without one. `POST /auth/refresh` now recomputes the union, so an
            # added permission arrives within one refresh by itself and the
            # logout bought nothing but interruption.
            #
            # **Removal still ends them immediately, and the asymmetry is the
            # point** (T129): granting late is an inconvenience, revoking late
            # is a security problem. Hence a superset test rather than equality
            # -- anything at all leaving the set costs the sessions.
            #
            # Both sides are read from the database inside this transaction
            # rather than inferred from which role ids moved: two roles can
            # grant the same permission, so *losing a role* and *losing a
            # permission* are different events and only the second matters.
            permissions_after = _effective_permissions(
                connection, tenant_id=tenant_id, employee_id=employee_id
            )
            purely_additive = permissions_after >= permissions_before
            revoked = (
                []
                if purely_additive
                else connection.execute(
                    """
                    UPDATE employee_sessions
                       SET revoked_at = now()
                     WHERE tenant_id = %s AND employee_id = %s AND revoked_at IS NULL
                    RETURNING id
                    """,
                    (tenant_id, employee_id),
                ).fetchall()
            )
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=employee_id,
                action="employee_role.update",
                correlation_id=context.correlation_id,
                reason_code="EMPLOYEE_ROLES_REPLACED",
                # **Both sides**, for `_audit_role_change`'s reason: the question
                # asked afterwards is what changed, and a row holding only the
                # new set sends the reader to find an old one that is gone.
                evidence={
                    "roles_before": before,
                    "roles_after": [str(role_id) for role_id in requested],
                    "sessions_revoked": len(revoked),
                    # **Why the sessions survived, recorded** (ADR-0055's third
                    # clause): after this row is written nothing can answer "was
                    # that assignment purely additive" -- the two permission sets
                    # it compared are gone. The sets themselves are not recorded;
                    # they are neither a credential nor unanswerable, since the
                    # current one is always readable.
                    "purely_additive": purely_additive,
                },
            )
        response.headers["cache-control"] = "no-store, private"
        # `{"receipt": …, "result": …}` (ADR-0050 §三): this command revokes
        # sessions in bulk and changes authorization, both of which parity §5.2
        # names. The receipt has to be a receipt, so the domain fields nest.
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="employee",
                    resource_id=str(employee_id),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "employee_id": str(employee_id),
                "role_ids": [str(role_id) for role_id in requested],
                "sessions_revoked": len(revoked),
            },
        }

    @app.post(
        "/employees/{employee_id}/password-reset",
        status_code=200,
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/employees/{employee_id}/password-reset"),
    )
    def reset_employee_password(
        employee_id: UUID,
        command: ResetEmployeePasswordRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #17 (`C0A-019/024` split).

        The freeze note has three clauses and this route is those three, plus
        the one ADR-0048 §二.5 (K8) added afterwards:

        * **写入即哈希** -- the password reaches `hash_password` and the request
          model, and nowhere else. It is not in the response, the audit
          evidence, an error message or the `UPDATE`.
        * **不回显** -- and that is why the administrator supplies the password
          rather than the server generating one. `C0A-024`, the route this
          replaces, returned `new_password` in its body; a generated password
          would have to be echoed to be usable, which is that same behaviour
          under a better name.
        * **撤销既有 session** -- every session row for this employee, not the
          caller's. `credential_version` is incremented in the same statement,
          which is the belt to that braces: a token minted before the reset
          stops authenticating even if a session row were missed
          (`unified-authorization-layer.md:160` asks for both).
        * **`must_change_password = true`** -- K8. The password was set by
          somebody other than its owner and travelled through a channel this
          system does not control, so it is good for exactly one thing: the
          call to `POST /auth/change-password` that replaces it.

        **Resetting your own password is refused (409).** Not for the lockout
        reason `POST /employees/{id}/deactivate` refuses it, but because the
        flag would then say something false -- `must_change_password` means
        "somebody else set this" -- while the caller's own session was revoked
        mid-request. The door to your own credential is freeze B1 #30.

        A disabled employee is refused too, and told apart from a missing one
        **inside the transaction**, the shape #18 uses and for its reason: two
        statements can be overtaken by a concurrent deactivation between them.
        Writing a credential onto an account that cannot log in is not harmful,
        but it is a write with no reachable effect, and the 409 says which.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="employee_password_reset.create",
                resource=tenant_resource_ref(
                    resource_type="employee_password_reset", tenant_id=tenant_id
                ),
            )
        )
        if employee_id == context.subject.id:
            raise HTTPException(status_code=409, detail="CANNOT_RESET_OWN_PASSWORD")
        # Before the transaction opens, for the reason `POST /employees` gives.
        new_password_hash = hash_password(command.new_password)
        with database.transaction(
            tenant_id=tenant_id,
            subject_id=context.subject.id,
            correlation_id=context.correlation_id,
        ) as connection:
            row = connection.execute(
                """
                UPDATE employees
                   SET password_hash = %s,
                       password_changed_at = now(),
                       credential_version = credential_version + 1,
                       must_change_password = true
                 WHERE tenant_id = %s AND id = %s AND status = 'active'
                RETURNING id, credential_version, password_changed_at, must_change_password
                """,
                (new_password_hash, tenant_id, employee_id),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT status FROM employees WHERE tenant_id = %s AND id = %s",
                    (tenant_id, employee_id),
                ).fetchone()
                if existing is None:
                    # A row this tenant cannot see and a row that does not exist
                    # are one answer, or the route becomes a cross-tenant
                    # existence oracle. RLS makes both arrive here identically.
                    raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")
                raise HTTPException(status_code=409, detail="EMPLOYEE_DISABLED")
            revoked = connection.execute(
                """
                UPDATE employee_sessions
                   SET revoked_at = now()
                 WHERE tenant_id = %s AND employee_id = %s AND revoked_at IS NULL
                RETURNING id
                """,
                (tenant_id, employee_id),
            ).fetchall()
            _audit_employee_event(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                subject_employee_id=employee_id,
                action="employee_password_reset.create",
                correlation_id=context.correlation_id,
                reason_code="EMPLOYEE_PASSWORD_RESET",
                evidence={
                    "sessions_revoked": len(revoked),
                    "credential_version": int(row["credential_version"]),
                },
            )
        response.headers["cache-control"] = "no-store, private"
        # `{"receipt": …, "result": …}` (ADR-0050 §三), for the reason
        # `POST /employees/{id}/deactivate` carries one: this command revokes
        # sessions in bulk, one of the cases parity §5.2 names, and domain
        # fields beside a receipt produce a body that fails
        # `command-receipt.schema.json`'s `additionalProperties: false`.
        #
        # **`resource_type` is `employee`, not `employee_password_reset`.** The
        # command resource is what the caller needed a permission for; the thing
        # that changed is a person, and `resource_id` is their id. A receipt
        # naming a resource type whose ids do not exist anywhere would be a
        # reference nobody can follow.
        return {
            "receipt": command_receipt(
                resource=resource_ref(
                    resource_type="employee",
                    resource_id=str(row["id"]),
                    authorized_tenant_id=authorized_tenant_from_context(context),
                    claimed_tenant_id=None,
                ),
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
                status="succeeded",
            ).as_dict(),
            "result": {
                "employee_id": str(row["id"]),
                "sessions_revoked": len(revoked),
                # Echoed because it is the fact the caller has to act on: the
                # password they just set works for one route and no other.
                "must_change_password": bool(row["must_change_password"]),
                "password_changed_at": _iso_timestamp(row["password_changed_at"]),
            },
        }

    def _validated_permissions(submitted: tuple[str, ...]) -> list[str]:
        """Every code must be one `authorize()` could actually allow.

        Freeze B1 #23 (`C0A-026`): "resource catalog 校验，不能写未知 permission".
        A role holding a permission the catalog does not know is not a smaller
        problem than a missing one -- it reads to an administrator as a granted
        capability and behaves as a denied one, and the difference only shows up
        the day somebody relies on it.

        Duplicates are collapsed rather than refused: submitting the same code
        twice is a client's redundancy, not a claim about authorization. The
        stored order is sorted so that two equivalent sets compare equal in the
        database, which is what makes the "did this change anything" question
        answerable later.

        The unknown codes are named in the error. That is safe here and would
        not be everywhere: the full list is already served by `GET /permissions`
        to anyone holding `permission.list`, so echoing which of the caller's
        own submissions were unrecognised reveals nothing they could not read
        directly, and swallowing it would leave them guessing at a typo.
        """

        assignable = assignable_action_set()
        unknown = sorted(set(submitted) - assignable)
        if unknown:
            # **A bare code, like every other error on this surface.** The first
            # version returned `{"error": ..., "unknown": [...]}` -- a third
            # error shape, next to `{"detail": "CODE"}` and the `ErrorEnvelope`
            # that T87 still owes -- which is exactly what `_page_or_refuse`'s
            # docstring, written two days earlier by CP-2's review, says not to
            # do (independent review 2026-09-04, P2-3).
            #
            # Naming the unrecognised codes is still right, and the reasoning
            # holds: `GET /permissions` publishes the whole list, so echoing
            # which of the caller's own submissions were unknown reveals nothing
            # they could not fetch. They belong in `ErrorEnvelope.field_errors`,
            # and T87 now records that this file has a second consumer waiting.
            # **T87's second consumer, now that it exists.** The comment above
            # has said since CP-3 that naming the unrecognised codes is right
            # and owed, and that they belong in `ErrorEnvelope.field_errors`
            # rather than in a third bespoke error shape. This is that.
            #
            # The unknown code goes in `field`, not in `code`: `field` is free
            # text, while `code` is a machine token (`^[A-Z][A-Z0-9_]*$`) and a
            # submitted string like `role.manage_all` is not one. So each entry
            # reads "this submission, rejected for this reason", which is what
            # a form needs to mark it.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PERMISSION_NOT_IN_CATALOG",
                    "field_errors": [
                        {"field": code, "code": "PERMISSION_NOT_IN_CATALOG"}
                        for code in sorted(unknown)
                    ],
                },
            )
        return sorted(set(submitted))


    def _audit_employee_event(
        connection: object,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        subject_employee_id: UUID,
        action: str,
        correlation_id: UUID,
        reason_code: str,
        evidence: dict[str, object],
    ) -> None:
        """One row per event **about an employee** (T117, T128).

        **The rule, written once here rather than per route.** T117 was
        registered when B1 #6 landed: issuing a ticket that lives for minutes
        wrote an audit row, and ending the session that issued it wrote nothing.
        Login wrote nothing either. The review's expiry condition was "B1 #17 or
        #18, whichever lands first, defines the rule for authentication events
        rather than adding one line to its own route" -- because at #18 the
        character changes: **this route ends somebody else's session**, and
        without a record nobody can answer who signed whom out.

        **It was called `_audit_session_event` and is not any more** (independent
        review 2026-09-05, P2-4). The rule started as "every transition in a
        session's life is a row" and has outgrown that sentence twice: B1 #17
        added a credential change, and B1 #16 adds an authorization change and
        the two profile writes T128 was registered for. Two of the seven callers
        are not about sessions at all, and a helper whose name is narrower than
        its use is the same "the record says one thing" family this file keeps
        paying for.

        The rule now: **every event that changes who an employee is, what they
        may do, or whether they are still signed in, is a row.** Today that is
        nine call sites -- `session.create` at login, `session.delete` at
        logout, `employee.update` at deactivation and at a profile change,
        `employee.create`, `employee_password_reset.create`,
        `employee.change_own_password`, and `employee_role.update`.

        `actor_id` is who did it and `resource_id` is who it happened to; for
        login, logout and a self-service password change they are the same
        person, and for the others they are not. That difference is the whole
        reason the row exists.

        `evidence` never carries a token, a hash or an address -- only counts
        and ids. What an auditor needs from this row is "who, whom, when, how
        many", and anything else here would be a credential in a table that is
        deliberately append-only.
        """

        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, %s, %s, 'employee', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                actor_id,
                action,
                subject_employee_id,
                correlation_id,
                reason_code,
                Jsonb(evidence),
            ),
        )

    def _audit_role_change(
        connection: object,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        action: str,
        role_id: UUID,
        correlation_id: UUID,
        reason_code: str,
        evidence: dict[str, object],
    ) -> None:
        """One row per change to a tenant's authorization data.

        **P1-1.** `C0A-027` says "授权变更审计" and `C0A-028` says "审计"; the three
        routes wrote `roles` and nothing else, so a tenant's permission set could
        be rewritten or deleted with **no record of who did it, when, or to
        what** -- in the one cluster whose entire subject is authorization.

        This is not the gap T49 covers. T49 is about
        `require_obligations_discharged()` having no caller and waiting on CP-1's
        audit sink; an `audit_events` row needs no sink at all. Two hundred lines
        away in this same file `/ws/tickets` inserts one in its own transaction,
        and `profiles/publisher.py` does too. The table, the grant and the
        vocabulary were all already there.

        **The evidence records the permission sets, not just that something
        changed.** "Somebody edited this role" is not an audit trail for an
        authorization system: the question asked afterwards is always *what did
        they grant*, and a row that cannot answer it costs the same to write.
        """

        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, %s, %s, 'role', %s, %s, %s, %s)
            """,
            (
                tenant_id,
                uuid4(),
                actor_id,
                action,
                role_id,
                correlation_id,
                reason_code,
                Jsonb(evidence),
            ),
        )

    def _serialise_role_changes(connection: object, tenant_id: UUID) -> None:
        """Make one tenant's role mutations happen one at a time.

        **P1-2.** The last-administrator guard reads, then the route writes, and
        `db.py` sets no isolation level -- so psycopg's READ COMMITTED applies
        and the two statements are not one decision. With two admin roles R1 and
        R2, concurrent `DELETE /roles/R1` and `DELETE /roles/R2` each see the
        other's still-uncommitted row, each concludes "there is another one",
        and the tenant commits its way to **zero** admin roles: exactly the
        lockout the guard's own docstring says needs direct database access to
        undo.

        The reviewer proposed `FOR UPDATE` on the guard's SELECT. That does fix
        the read, but the two transactions then take row locks in opposite
        orders -- each has locked the role the other is about to delete -- and
        the outcome is a deadlock that PostgreSQL breaks by aborting one side.
        Correct, and it surfaces as a 500 with a deadlock in the log.
        Serialising the decision per tenant gives the same guarantee and lets
        the loser simply see the committed truth and refuse properly.
        `pg_advisory_xact_lock` is the same tool and the same reasoning as
        **`worker/digest.py:101`** (two-argument form, keyed on the tenant; the
        older single-argument precedent is `worker/cost.py:802`): what has to be
        serialised is a *decision*, not a row -- and one of these decisions is
        about a row that is about to stop existing, so there is no row to lock.

        (Round 2's R-2 read this as citing `runtime/notification.py`, which has
        no lock. The lock is in `worker/digest.py`; the citation was right and
        too vague to check, which is the same defect from the reader's side.
        Both paths are now named.)

        **This depends on READ COMMITTED, and the dependency is the point**
        (round 2, R-3). The lock provides mutual exclusion; what makes the
        second holder *see the first one's delete* is that READ COMMITTED takes
        a fresh snapshot per statement. Under REPEATABLE READ the snapshot would
        be fixed at the transaction's first statement -- `set_config` in
        `db.transaction` -- and the guard would read a stale view **while holding
        the lock**, with no serialisation failure to catch it because the two
        transactions touch different rows. The original defect would come back
        silently. `db.py` sets no isolation level today; anyone changing that
        has to come through here.

        Role changes are rare and per tenant, so the cost of serialising them is
        not worth measuring; the cost of not serialising them is a tenant that
        cannot grant permissions to anybody.
        """

        connection.execute(  # type: ignore[attr-defined]
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (ROLE_LOCK_NAMESPACE, _tenant_lock_key(tenant_id)),
        )

    def _administrator_exists(connection: object, *, tenant_id: UUID) -> bool:
        """Does **anybody in this tenant actually hold** `role.update` right now?

        **This replaces `_other_admin_role_exists`, and the difference is T92's
        third landing item.** The old guard asked whether another *role* carried
        `role.update`. Until this batch that was the only question it could ask:
        `employee_roles` was empty by construction, so "who holds it" had no
        answer and a role nobody held counted as protection. Now roles are real,
        and the honest question is the one an administrator locked out would ask
        -- *is there still a person who can grant permissions?*

        It is strictly better in both directions. A tenant whose only admin role
        is held by nobody is no longer protected by that role, so deleting it is
        allowed -- previously refused for nothing. And a person who holds
        `role.update` only through a role can no longer have it stripped by
        `PUT /employees/{id}/roles` if they are the last one -- previously not
        checked at all, because that route did not exist.

        **Both sources count**, for the reason login unions them: a permission
        held through the column and one held through a role are the same
        permission, and a guard that looked at only one of them would be a guard
        with a documented way around it.

        Only `active` employees count. A disabled employee cannot log in, so
        their `role.update` is not a way back into the tenant -- counting them
        would let the last real administrator be stripped while a tombstone
        stood in for them.

        Called **twice** by each of the three routes that can take authorization
        away from a person -- `PUT /roles/{id}/permissions`,
        `PUT /employees/{id}/roles` and `POST /employees/{id}/deactivate` -- and
        only under `_serialise_role_changes`: once before the write and once
        after,
        because the refusal is about the *transition* true -> false. A tenant
        that already has no administrator is not made worse by an unrelated
        assignment, and refusing that one would be a false alarm on top of a
        real problem.
        """

        found = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT 1
              FROM employees e
             WHERE e.tenant_id = %s
               AND e.status = 'active'
               AND (
                     e.permissions ? %s
                  OR EXISTS (
                       SELECT 1
                         FROM employee_roles er
                         JOIN roles r
                           ON r.tenant_id = er.tenant_id AND r.id = er.role_id
                        WHERE er.tenant_id = e.tenant_id
                          AND er.employee_id = e.id
                          AND r.permissions ? %s
                     )
                   )
             LIMIT 1
            """,
            (tenant_id, ROLE_ADMIN_ACTION, ROLE_ADMIN_ACTION),
        ).fetchone()
        return found is not None

    @app.get(
        "/permissions",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/permissions"),
    )
    def list_permissions(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #26 (`C0A-029`): generated from the catalog, not copied.

        Platform-scoped for the same reason `GET /profiles/schema` is: the
        permission vocabulary ships with the image and is identical for every
        tenant, so a tenant resource ref would be a lie about what is being
        read.

        What it lists is what `authorize()` could allow -- see
        `assignable_permissions`. The catalog's bootstrap actions are left out
        on purpose; offering an administrator a permission that can never take
        effect is worse than offering none.
        """

        require_allowed(
            authorize(
                context=context,
                action="permission.list",
                resource=platform_resource_ref(resource_type="permission"),
            )
        )
        listing: list[object] = [dict(item) for item in assignable_permissions()]
        response.headers["cache-control"] = "no-store, private"
        # The cap applies here too, and it is not ceremony: this list grows with
        # the resource catalog, and a catalog that outgrows one page would
        # otherwise start silently truncating the vocabulary an administrator
        # picks permissions from -- the one list where a missing entry looks
        # exactly like a permission that does not exist.
        return _page_or_refuse(
            listing[:MAX_PAGE_ITEMS],
            has_more=len(listing) > MAX_PAGE_ITEMS,
        )

    @app.get(
        "/roles",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("GET", "/roles"),
    )
    def list_roles(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #22 (`C0A-025`): a tenant-scoped role catalog.

        The old interface took an *optional tenant filter* -- a parameter whose
        default answered for everybody. Here the scope comes from the token and
        there is no parameter to leave off, the same shape as
        `GET /tenants/current`.

        Unlike the employee list, this one does return the permission array:
        a role **is** its permission set, and a catalog that hid it would leave
        an administrator unable to tell two roles apart before assigning one.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="role.list",
                resource=tenant_resource_ref(resource_type="role", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, permissions, is_system, created_at, updated_at
                  FROM roles WHERE tenant_id = %s ORDER BY name
                 LIMIT %s
                """,
                (tenant_id, MAX_PAGE_ITEMS + 1),
            ).fetchall()
        response.headers["cache-control"] = "no-store, private"
        return _page_or_refuse(
            [_role_projection(row) for row in rows[:MAX_PAGE_ITEMS]],
            has_more=len(rows) > MAX_PAGE_ITEMS,
        )

    @app.post(
        "/roles",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("POST", "/roles"),
    )
    def create_role(
        command: CreateRoleRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #23 (`C0A-026`): every permission checked against the catalog.

        **A role created here grants nothing to anybody.** `authorize()` reads
        `employees.permissions`; nothing reads `roles`. The route that connects
        them is freeze B1 #16 (`PUT /employees/{employee_id}/roles`), which is
        not in this batch. Written here, in the migration, in the self-review and
        in the backlog, because the opposite belief is the dangerous one: an
        operator who creates a role called "read-only" and thinks it has taken
        effect has been handed a boundary that is not there.

        `is_system` is not settable from the wire. A client that could mint a
        system role could mint one the delete route refuses to remove.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="role.create",
                resource=tenant_resource_ref(resource_type="role", tenant_id=tenant_id),
            )
        )
        permissions = _validated_permissions(command.permissions)
        role_id = uuid4()
        with database.transaction(tenant_id=tenant_id) as connection:
            _serialise_role_changes(connection, tenant_id)
            row = connection.execute(
                """
                INSERT INTO roles(tenant_id, id, name, description, permissions, is_system)
                VALUES (%s, %s, %s, %s, %s, false)
                ON CONFLICT (tenant_id, name) DO NOTHING
                RETURNING id, name, description, permissions, is_system,
                          created_at, updated_at
                """,
                (tenant_id, role_id, command.name, command.description, Jsonb(permissions)),
            ).fetchone()
            if row is not None:
                _audit_role_change(
                    connection,
                    tenant_id=tenant_id,
                    actor_id=context.subject.id,
                    action="role.create",
                    role_id=role_id,
                    correlation_id=context.correlation_id,
                    reason_code="ROLE_CREATED",
                    evidence={"name": command.name, "permissions": permissions},
                )
        if row is None:
            # The unique constraint did the refusing, not a pre-check: two
            # requests naming the same role can arrive together, and a read
            # before the write would let both through.
            raise HTTPException(status_code=409, detail="ROLE_NAME_ALREADY_USED")
        response.status_code = 201
        response.headers["cache-control"] = "no-store, private"
        # `CommandReceipt`, by the same ruling that moved `POST /profiles/releases`
        # (2026-09-04): every create command on this surface returns one. The role
        # itself is not echoed -- a receipt names what was created, and the caller
        # reads it back from `GET /roles` if it wants the row. That the permission
        # array does not come back here is deliberate: this route already answered
        # what it stored by refusing anything it would not have stored.
        return command_receipt(
            resource=resource_ref(
                resource_type="role",
                resource_id=str(row["id"]),
                # The context conversion, not the bare-id one: CP-2's review
                # (round 2, R-3) revoked this file's allowlist entry, so the only
                # sanctioned crossing is the one that takes a verified
                # `AuthContext` and cannot be handed a raw UUID.
                #
                # The other spelling is not written out even in this comment --
                # the guard is a text scan, and it counts a mention as a call.
                # Blunt, and correctly so: a scan that tried to tell code from
                # prose would be a scan with an exception in it.
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict()

    @app.put(
        "/roles/{role_id}/permissions",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        # 形状由 `tests/contracts/test_response_models_match_handlers.py` 钉住。
        responses=openapi_responses("PUT", "/roles/{role_id}/permissions"),
    )
    def replace_role_permissions(
        role_id: UUID,
        command: ReplacePermissionsRequest,
        response: Response,
        idempotency_key: IdempotencyKey,  # noqa: ARG001 - 声明它就是让 FastAPI 校验并要求这个头；函数体不读它，但它在做事
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #24 (`C0A-027`): the two refusals this route exists for.

        **System roles are refused.** `is_system` is a column, not a naming
        convention, so the refusal cannot be defeated by a rename.

        **The last role that can hand out permissions is refused.** Whoever
        holds `role.update` can rewrite any role, including their own; remove
        the last copy and the tenant can no longer grant anything to anybody --
        a lockout that needs direct database access to undo. The guard is by
        capability, not by a role called "admin", because a tenant may have
        called it something else.

        The permission array is **replaced**, not merged. A partial update of an
        authorization set is a request whose effect depends on state the client
        cannot see; `PUT` with the whole set means the client is stating what it
        believes the answer should be, which is also what makes the audit line
        readable.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="role.update",
                resource=tenant_resource_ref(resource_type="role", tenant_id=tenant_id),
            )
        )
        permissions = _validated_permissions(command.permissions)
        with database.transaction(tenant_id=tenant_id) as connection:
            _serialise_role_changes(connection, tenant_id)
            current = connection.execute(
                "SELECT is_system, permissions FROM roles WHERE tenant_id = %s AND id = %s",
                (tenant_id, role_id),
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="ROLE_NOT_FOUND")
            if current["is_system"]:
                raise HTTPException(status_code=409, detail="SYSTEM_ROLE_IS_IMMUTABLE")
            # **The guard is now about people, not about roles** (T92). It
            # asks the question twice around the write and refuses only the
            # transition -- see `_administrator_exists`. Before this batch it
            # asked whether another *role* carried `role.update`, which was the
            # only question available while `employee_roles` was empty; a role
            # nobody held counted as protection, and a person who held it
            # through a role could not be counted at all.
            had_administrator = _administrator_exists(connection, tenant_id=tenant_id)
            row = connection.execute(
                """
                UPDATE roles SET permissions = %s, updated_at = now()
                 WHERE tenant_id = %s AND id = %s
                RETURNING id, name, description, permissions, is_system,
                          created_at, updated_at
                """,
                (Jsonb(permissions), tenant_id, role_id),
            ).fetchone()
            if had_administrator and not _administrator_exists(
                connection, tenant_id=tenant_id
            ):
                # Rolled back by the exception leaving the transaction, so the
                # write above never lands. Checking after the write rather than
                # predicting its effect is what makes this exact: the union of
                # a column and a set of roles is not something to re-derive in
                # the application when the database can be asked.
                raise HTTPException(status_code=409, detail="LAST_ADMIN_ROLE_PROTECTED")
            _audit_role_change(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                action="role.update",
                role_id=role_id,
                correlation_id=context.correlation_id,
                reason_code="ROLE_PERMISSIONS_REPLACED",
                evidence={
                    # Both sides, because the question asked afterwards is what
                    # changed -- and an audit row holding only the new set makes
                    # the reader go and find the old one, which by then is gone.
                    "permissions_before": list(current["permissions"]),
                    "permissions_after": permissions,
                },
            )
        assert row is not None  # noqa: S101 - the SELECT above ran in this transaction
        response.headers["cache-control"] = "no-store, private"
        return {**_role_projection(row), "correlation_id": str(context.correlation_id)}

    @app.delete("/roles/{role_id}")
    def delete_role(
        role_id: UUID,
        response: Response,  # noqa: ARG001 - 同上，Response 由框架注入
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> Response:
        """Route freeze B1 #25 (`C0A-028`): reference check, system roles kept.

        Three refusals, and the third is the one that needs a table:

        * a role that does not exist -> 404;
        * a system role -> 409, same reasoning as the update route;
        * **a role somebody still holds -> 409.** `employee_roles` **is written
          now** -- freeze B1 #16 landed it -- so this check really does refuse
          things; the sentence that used to stand here ("finds nothing today")
          stopped being true with that batch, and it sat directly above the
          reasoning that depends on it (independent review 2026-09-05, P2-4).

          **That reasoning is why this route no longer carries a
          last-administrator check**: a role reaching the deletion is provably
          held by nobody, so deleting it cannot take `role.update` away from any
          person. "Provably" is doing real work and rests on T92's fourth
          landing item -- this read, the delete, and the assignment route all run
          under the same `_serialise_role_changes` tenant lock, so there is no
          window between "read that nobody holds it" and "committed". Without
          the lock this would be a timing argument and would not hold.

        Deleting is a delete, not a flag. §6.4's stop/purge distinction is about
        tenants and employees -- things with history worth keeping; a role is a
        permission set, and one nobody holds has nothing to preserve.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="role.delete",
                resource=tenant_resource_ref(resource_type="role", tenant_id=tenant_id),
            )
        )
        with database.transaction(tenant_id=tenant_id) as connection:
            _serialise_role_changes(connection, tenant_id)
            current = connection.execute(
                "SELECT name, is_system, permissions FROM roles"
                " WHERE tenant_id = %s AND id = %s",
                (tenant_id, role_id),
            ).fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="ROLE_NOT_FOUND")
            if current["is_system"]:
                raise HTTPException(status_code=409, detail="SYSTEM_ROLE_IS_IMMUTABLE")
            held = connection.execute(
                "SELECT 1 FROM employee_roles WHERE tenant_id = %s AND role_id = %s LIMIT 1",
                (tenant_id, role_id),
            ).fetchone()
            if held is not None:
                raise HTTPException(status_code=409, detail="ROLE_STILL_ASSIGNED")
            # **The last-administrator check is gone from this route, and its
            # absence is the point** (T92). The refusal immediately above proves
            # the role being deleted is held by **nobody**, so deleting it
            # cannot take `role.update` away from any person -- and now that
            # `employee_roles` is written, "any person" is the question that
            # matters. The old check asked whether another *role* carried
            # `role.update` and refused if not, which protected a tenant against
            # losing a capability nobody had.
            #
            # Kept as a comment rather than as an unreachable `raise`: a guard
            # that cannot fire reads like protection and is tested by a case
            # that cannot fail, which is the shape this repository keeps finding
            # rather than the shape it wants. The protection that matters lives
            # in `_administrator_exists`, on the two routes that can actually
            # take a permission away from a person.
            connection.execute(
                "DELETE FROM roles WHERE tenant_id = %s AND id = %s", (tenant_id, role_id)
            )  # noqa: E501 - the audit row below is what makes this recoverable
            _audit_role_change(
                connection,
                tenant_id=tenant_id,
                actor_id=context.subject.id,
                action="role.delete",
                role_id=role_id,
                correlation_id=context.correlation_id,
                reason_code="ROLE_DELETED",
                evidence={
                    # The row is gone after this statement; if the audit did not
                    # carry what it held, nothing would.
                    "name": current["name"],
                    "permissions": list(current["permissions"]),
                },
            )
        # One response object. The previous version also set the header on the
        # injected `response`, which a 204 built here discards entirely -- the
        # behaviour was right and the code read as though both mattered, so the
        # next person to delete the "duplicate" had even odds of deleting the
        # one that works (independent review 2026-09-04, P3-4).
        return Response(status_code=204, headers={"cache-control": "no-store, private"})

    @app.get(
        "/profiles/releases",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        responses=openapi_responses("GET", "/profiles/releases"),
    )
    def list_profile_releases(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #27 (`C0A-011`): raw prompt GET -> release read model.

        Shaped as `Page<T>` (parity 5.2) even though a tenant has a handful of
        releases: the contract exists so that no list on this surface ever
        starts life with an unstable offset, and starting the first one right
        costs nothing.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="profile_release.read",
                resource=tenant_resource_ref(
                    resource_type="profile_release", tenant_id=tenant_id
                ),
            )
        )
        rows, has_more = ProfileResolver(database=database).release_summaries(
            tenant_id=tenant_id, limit=MAX_PAGE_ITEMS
        )
        response.headers["cache-control"] = "no-store, private"
        return _page_or_refuse(
            [_release_projection(row) for row in rows],
            has_more=has_more,
        )

    @app.get(
        "/profiles/releases/{release_id}",
        # 响应形状进 OpenAPI，只做文档（ADR-0142 决策 1）。
        responses=openapi_responses("GET", "/profiles/releases/{release_id}"),
    )
    def read_profile_release(
        release_id: UUID,
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """T373 / ADR-0143 §二：**看得到这一版说了什么**，只给商家那半。

        在这条 route 之前，商家能看到自己有哪几版话术、却看不到任何一版
        **写了什么** —— `GET /profiles/releases` 的投影只有四个元数据字段，
        它的 docstring 逐字写着「文档故意不在列表里」，
        理由是 M1a 的 route freeze 没冻单条读，
        「从列表里返回文档等于顺手替 freeze 做了决定」。
        **那个决定现在由 ADR-0143 §二 明着做了**，所以文档从这条专门的
        route 出去，而列表那一侧一个字都不动。

        **与列表同一个 action**（`profile_release.read`，租户级）。
        K1 的判据是「这个权限需不需要单独授予」，而对商家来说
        「看得到有哪些版本」与「看得到那一版说了什么」是同一件事。

        **`ProfileResolver.release` 会校验并重算 sha256**，这条路径要的正是
        它：`release_summaries` 的 docstring 说过「完整性校验属于发文档的
        那条路径，不属于列名字的那条」—— 这就是发文档的那条。
        校验不过是 500 而不是 404：**库里那一行与它自己的哈希对不上**
        是一次真实的损坏，把它说成「没找到」会让商家去重新发布一版，
        而问题在别处。
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="profile_release.read",
                resource=tenant_resource_ref(
                    resource_type="profile_release", tenant_id=tenant_id
                ),
            )
        )
        try:
            release = ProfileResolver(database=database).release(
                tenant_id=tenant_id, release_id=release_id
            )
        except ProfileNotFound:
            # 看不见的与不存在的是同一个答案，否则这条 route 会变成一个
            # 跨租户的存在性探针。RLS 让两种情况一模一样地到达这里。
            raise HTTPException(
                status_code=404, detail="PROFILE_RELEASE_NOT_FOUND"
            ) from None
        except ProfileImmutabilityError:
            raise HTTPException(
                status_code=500, detail="PROFILE_RELEASE_CORRUPT"
            ) from None
        response.headers["cache-control"] = "no-store, private"
        return _release_document_projection(release)

    @app.post("/profiles/releases")
    def publish_profile_release(
        command: PublishReleaseRequest,
        response: Response,
        idempotency_key: IdempotencyKey,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #28 (`C0A-012`): draft -> validate -> publish.

        The publishing logic is T10's and is not reimplemented here; this route
        is the entry point the CLI's own docstring said was waiting for the
        freeze ("adding a route ahead of that freeze would decide by accident
        what the freeze is supposed to decide"). The freeze is done, so here it
        is, with the same database role and therefore the same RLS.

        **What `Idempotency-Key` does and does not do.** Parity 5.2 requires the
        header on create commands and it is required here. It is *not* what
        makes this route idempotent: `publish_release` is idempotent on
        (tenant, profile_id, version, content_hash) and returns `created=False`
        for a repeat, which is stronger than a key-keyed replay cache because it
        also catches two different clients publishing the same thing. The header
        is echoed so a retrying client can match its own attempt; nothing is
        keyed on it, and saying so here is better than letting a reader assume
        a replay store exists.
        """

        tenant_id = next(iter(context.tenant_scope.tenant_ids))
        require_allowed(
            authorize(
                context=context,
                action="profile_release.create",
                resource=tenant_resource_ref(
                    resource_type="profile_release", tenant_id=tenant_id
                ),
            )
        )
        try:
            package = ProfileRegistry(root=PROFILE_REGISTRY_ROOT).package(
                profile_id=command.profile_id, version=command.version
            )
        except ProfileError as error:
            # The registry refuses unknown packages, packages outside the
            # repository, and packages whose content hash does not match. All
            # three are the client naming something untrusted; none of them may
            # echo the underlying message, which can carry a filesystem path.
            raise HTTPException(
                status_code=422, detail="PROFILE_PACKAGE_NOT_TRUSTED"
            ) from error
        result = publish_release(
            database=database,
            tenant_id=tenant_id,
            package=package,
            published_by=context.subject.id,
            correlation_id=context.correlation_id,
        )
        response.status_code = 201 if result.created else 200
        response.headers["cache-control"] = "no-store, private"
        # **`CommandReceipt`, by ruling (2026-09-04): every create command on
        # this surface returns one.** parity §5.2 only *mandates* a receipt for
        # six kinds of command and profile publishing is not among them, so the
        # first version returned a domain body with a bespoke `created` boolean
        # -- which would have been the precedent the remaining ~70 routes copied
        # (independent review 2026-09-04, P2-4).
        #
        # `status` is `succeeded`, not `accepted`: the release is durable before
        # this returns, and `accepted` means "will be worked". The `created`
        # boolean does not survive the move -- the enum has no word for "already
        # existed" -- so the distinction is carried by the status code, 201
        # against 200, which is where HTTP puts it anyway.
        return command_receipt(
            resource=resource_ref(
                resource_type="profile_release",
                resource_id=str(result.release.release_id),
                authorized_tenant_id=authorized_tenant_from_context(context),
                claimed_tenant_id=None,
            ),
            idempotency_key=idempotency_key,
            correlation_id=context.correlation_id,
            status="succeeded",
        ).as_dict()

    @app.get("/profiles/schema")
    def profile_schema(
        response: Response,
        context: AuthContext = Depends(authenticated),  # noqa: B008
    ) -> dict[str, object]:
        """Route freeze B1 #29 (`C0A-010`): the placeholder list becomes a schema.

        Platform-scoped, not tenant-scoped: the merchant-profile schema and the
        set of packages shipped in the image are identical for every tenant, so
        a tenant resource ref would be a lie about what is being read. That is
        why this batch adds `profile_schema` to the resource catalog rather than
        reusing `profile_release`, whose rows are tenant-owned.

        It returns what a caller needs to name a package in the publish command
        above, and nothing that would let one be smuggled in: package identity
        and content hash, never a document.
        """

        require_allowed(
            authorize(
                context=context,
                action="profile_schema.read",
                resource=platform_resource_ref(resource_type="profile_schema"),
            )
        )
        try:
            packages = ProfileRegistry(root=PROFILE_REGISTRY_ROOT).packages()
        except ProfileError as error:
            # Previously uncaught, so a registry that could not be trusted --
            # which is what a misconfigured deployment root produces -- came out
            # as a 500 with a traceback (independent review 2026-09-04, P1-1 and
            # P2-5). Same discipline as the publish route: an answer, and never
            # the underlying message, which can carry a filesystem path.
            raise HTTPException(
                status_code=503, detail="PROFILE_REGISTRY_UNAVAILABLE"
            ) from error
        response.headers["cache-control"] = "no-store, private"
        return {
            "schema": load_schema("merchant-profile"),
            "packages": [
                {
                    "profile_id": package.profile_id,
                    "version": package.version,
                    "content_hash": package.content_hash,
                }
                for package in packages
            ],
        }

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        ticket: str,
        nonce: str,
        origin: str | None = Header(default=None),
    ) -> None:
        try:
            tenant_prefix, _ = ticket.split(".", 1)
            tenant_id = UUID(tenant_prefix)
        except (ValueError, AttributeError):
            await websocket.close(code=4401)
            return
        if origin is None or origin not in resolved.allowed_ws_origins or len(nonce) < 16:
            await websocket.close(code=4403)
            return
        with database.transaction(tenant_id=tenant_id) as connection:
            consumed = connection.execute(
                # **Both expiries are gates, so both read `clock_timestamp()`**
                # (independent review F4) -- this statement decides whether a
                # socket is allowed to open. `consumed_at = now()` stays on the
                # transaction timestamp: that one is a stamp, not a gate, and it
                # belongs on the same instant as the row's other bookkeeping.
                """
                UPDATE ws_tickets t
                   SET consumed_at = now(), connection_nonce = %s
                  FROM employees e, employee_sessions s
                 WHERE t.tenant_id = %s AND t.token_hash = %s
                   AND t.origin = %s AND t.consumed_at IS NULL
                   AND t.expires_at > clock_timestamp()
                   AND e.tenant_id = t.tenant_id AND e.id = t.employee_id
                   AND e.status = 'active' AND e.credential_version = t.credential_version
                   AND s.tenant_id = t.tenant_id AND s.id = t.session_id
                   AND s.revoked_at IS NULL AND s.expires_at > clock_timestamp()
                RETURNING t.employee_id, t.topics
                """,
                (nonce, tenant_id, sha256_token(ticket), origin),
            ).fetchone()
        if consumed is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "authenticated",
                "tenant_id": str(tenant_id),
                "employee_id": str(consumed["employee_id"]),
                "topics": consumed["topics"],
            }
        )
        try:
            await websocket.receive_text()
        except WebSocketDisconnect:
            return

    return app


app = create_app()
