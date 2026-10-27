"""控制面响应的形状，进 OpenAPI **做文档**。

## 它补的是什么（ADR-0142 决策 1）

`control-plane-parity.md` §4.3 把 OpenAPI 定成 HTTP 合同的权威，而在本模块之前
**那句话只对请求成立**：实测 2026-09-20，产物里 **59 条 2xx 响应带具体 schema 的是 0 条**，
每一条都是 `{"type":"object","additionalProperties":true}` ——
因为 route handler 全部标注成 `-> dict[str, object]`（72 处）。

后果是安静的：前端手写一份响应类型，后端改一个字段名，**TypeScript 全绿**
（它查的是前端自己写的那份），请求发出去才拿到一个 `undefined`。
在列表页上，那表现为**整列空白** —— 与「这个租户确实没数据」在屏幕上一模一样。

## 为什么是 `responses=` 而不是 `response_model`

**`response_model` 会过滤掉模型里没有的键。** 一个写漏了字段的模型不报错，
它**安静地把那个字段从响应里删掉** —— 正是 `CLAUDE.md` §三 点名的形状：
能建成、能返回 200，只有结果是错的。

`responses={200: {"model": X}}` 只影响文档，**运行期行为一字不改**。

## 代价，以及它必须配的那一条

只做文档，就意味着**它可以说谎**：模型写错了，OpenAPI 就是错的，而没有东西会红。

所以 ADR-0142 要求：**每一个模型都要有一条用例，拿 handler 真实的返回比它的字段集合。**
那条用例是 `tests/contracts/test_response_models_match_handlers.py`，
判据与 `check_openapi_drift.py` 同源 —— 比字段集合，不比语义。
**没有那条用例覆盖的模型不许加进 `ROUTE_RESPONSES`。**
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _Shape(BaseModel):
    """所有响应模型的基类。

    **`extra="forbid"` 在这里是一句断言，不是一道闸** —— 这些模型不参与
    运行期校验（见模块 docstring），所以它挡不住任何东西。
    它的作用是让「这个形状是封闭的」这件事在**生成出来的 OpenAPI 里**
    写成 `additionalProperties: false`，于是前端的类型也是封闭的：
    读一个不存在的字段成为编译错误，而不是一个运行期 `undefined`。
    """

    model_config = ConfigDict(extra="forbid")


class LoginResponse(_Shape):
    """`POST /auth/login`。

    **`must_change_password` 是 T130 加的那一个**，而它只在这里、
    不在 `RefreshResponse` 上（ADR-0142 §二）：一次重置会抬
    `credential_version`，被重置的人手上那张 refresh token 当场就失效，
    所以 refresh 这条路径上不存在「欠一次改密」的会话。
    """

    access_token: str
    token_type: str
    expires_in: int
    must_change_password: bool


class RefreshResponse(_Shape):
    """`POST /auth/refresh`。三个键，一个不多 —— 理由见 `LoginResponse`。"""

    access_token: str
    token_type: str
    expires_in: int


class IdentityResponse(_Shape):
    """`GET /auth/me`。

    **`permissions` 是一个列表而不是一个集合**：JSON 没有集合，
    而排序由服务端定 —— 客户端不得依赖顺序，也不得依赖重复不出现。
    """

    employee_id: str
    tenant_id: str
    permissions: list[str]
    correlation_id: str



# ---------------------------------------------------------------- 通用信封
#
# **这三个是仓里 `packages/contracts/control-plane/*.schema.json` 的镜像**，
# 而不是第二份定义：`tests/contracts/test_response_models_match_handlers.py`
# 从那些 schema 现读字段名来比对，所以这里写错了会红。


class ResourceRefShape(_Shape):
    resource_type: str
    resource_id: str
    tenant_id: str


class CommandReceiptShape(_Shape):
    """`parity` §5.2 的命令回执。`status` 是封闭枚举，由 schema 定。"""

    command_id: str
    status: str
    resource_ref: ResourceRefShape
    accepted_at: str
    idempotency_key: str
    correlation_id: str


# ---------------------------------------------------------------- 租户与策略


class CurrentTenantResponse(_Shape):
    tenant_id: str
    code: str
    status: str
    created_at: str


class ControlPolicyResponse(_Shape):
    """**两个字段一起给。** 只给一个会让另一个在任何地方都读不到
    （`read_control_policy` 的 docstring：T175 的形状）。"""

    default_control_mode: str
    understanding_during_takeover: bool


class ControlPolicyWriteResult(_Shape):
    """写策略之后的 `result`。**五个字段，不是三个。**

    初稿只写了那三个 `conversations_*` 计数 —— 而 handler 同时回了
    **写完之后的策略本身**。那条守卫
    （`test_a_receipts_result_model_names_exactly_the_keys_the_handler_puts_there`）
    当天就抓住了它，报的是「handler 多出来的：`default_control_mode`、
    `understanding_during_takeover`」。

    **留这段话是因为那正是 ADR-0142 担心的那个形状**：`responses=` 只做文档，
    模型写错了没有任何运行期行为会变 —— 会变的只有前端据它生成的类型，
    而那会在页面上表现为两个永远读不到的字段。
    """

    default_control_mode: str
    understanding_during_takeover: bool
    conversations_transitioned: int
    conversations_remaining: int
    conversations_skipped: int


class ControlPolicyWriteResponse(_Shape):
    receipt: CommandReceiptShape
    result: ControlPolicyWriteResult


class ManagerContactResult(_Shape):
    tenant_id: str
    channel: str
    config_id: str
    external_user_id: str


class ManagerContactWriteResponse(_Shape):
    receipt: CommandReceiptShape
    result: ManagerContactResult


# ---------------------------------------------------------------- 员工


class EmployeeListItem(_Shape):
    """列表里的一行。

    **`purged_at` 是 T155 加的**：在它之前，「已 purge」与「被停用且邮箱
    看着奇怪」在客户端这里是同一件事，而 `026` 的列注释明写不得从墓碑
    地址的形状反推。
    """

    employee_id: str
    email: str
    status: str
    permission_count: int
    purged_at: str | None


class EmployeePage(_Shape):
    items: list[EmployeeListItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class EmployeeStatsResponse(_Shape):
    total: int
    active: int
    disabled: int
    as_of: str


class EmployeeDetailStatsResponse(_Shape):
    employee_id: str
    email: str
    status: str
    permission_count: int
    active_session_count: int
    last_session_started_at: str | None
    created_at: str
    as_of: str


class EmployeeUpdatedResponse(_Shape):
    employee_id: str
    email: str
    status: str
    display_name: str | None
    employee_no: str | None
    department: str | None
    correlation_id: str


class EmployeeDeactivateResult(_Shape):
    employee_id: str
    status: str
    disabled_at: str
    purge_eligible_at: str
    sessions_revoked: int


class EmployeeDeactivateResponse(_Shape):
    receipt: CommandReceiptShape
    result: EmployeeDeactivateResult


class EmployeePurgeResult(_Shape):
    employee_id: str
    purged_at: str
    roles_removed: int
    sessions_revoked: int


class EmployeePurgeResponse(_Shape):
    receipt: CommandReceiptShape
    result: EmployeePurgeResult


class EmployeeRolesResult(_Shape):
    employee_id: str
    role_ids: list[str]
    sessions_revoked: int


class EmployeeRolesResponse(_Shape):
    receipt: CommandReceiptShape
    result: EmployeeRolesResult


class PasswordResetResult(_Shape):
    """⚠️ **这里有一个临时密码。** `parity` §5.2 的敏感字段规矩适用：
    它只在这一次响应里出现，不回显、不入日志。字段名照后端逐字。"""

    employee_id: str
    sessions_revoked: int
    must_change_password: bool
    password_changed_at: str


class PasswordResetResponse(_Shape):
    receipt: CommandReceiptShape
    result: PasswordResetResult


# ---------------------------------------------------------------- 角色与权限


class PermissionItem(_Shape):
    """权限目录的一项。

    **这份目录是平台级的**（`tenant_owned=false` 的那个 route），
    每个租户看到同一份 —— 但它**商家读得到**：`permission.list` 可以授给
    租户角色，而角色页要渲染勾选树就得读它。
    """

    action: str
    resource: str
    verb: str
    tenant_owned: bool


class PermissionPage(_Shape):
    items: list[PermissionItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class RoleItem(_Shape):
    role_id: str
    name: str
    description: str | None
    permissions: list[str]
    is_system: bool
    created_at: str
    updated_at: str


class RolePage(_Shape):
    items: list[RoleItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class RolePermissionsUpdatedResponse(_Shape):
    """`PUT /roles/{role_id}/permissions` 回的是**整个角色**加一个关联 id
    （handler 写的是 `{**_role_projection(row), "correlation_id": ...}`）。"""

    role_id: str
    name: str
    description: str | None
    permissions: list[str]
    is_system: bool
    created_at: str
    updated_at: str
    correlation_id: str


# ---------------------------------------------------------------- 会话与联系人（批 6）
#
# **这一族的形状不由 AST 判据验，由「调产出方」验**
# （`tests/contracts/test_projection_shapes.py`）：
# `_conversation_summary` / `_message_projection` / `_contact_projection`
# 都是模块级可导入的**纯函数**，拿一行合成的 row 调它们，
# 把结果**递归**比到这些模型上 —— 不要库、不要请求，而且一路比到嵌套层。
#
# **为什么不扩 AST 判据**：会话列表项里有四个嵌套对象，而 AST 那条只比顶层。
# 再扩一层它就要开始理解 `IfExp`、理解三元里的两支各是什么形状 ——
# 那是在用静态分析重新实现一遍这个函数。**能直接问产出方的时候就不要猜**，
# 与 `GET /permissions` 那条同一个道理。


class ConversationContactSummary(_Shape):
    """会话列表里那一格联系人摘要。

    ⚠️ **`profile_data` 今天恒空**（T179：M1a 不调渠道 profile API），
    所以这一格在界面上只有内部标识与活跃度 —— 那是一处要**写明**的空，
    不是一处可以省略的字段。
    """

    contact_id: str
    config_id: str
    external_user_id: str
    profile_data: dict[str, object]


class LastMessageSummary(_Shape):
    """最后一条消息的摘要。

    **`content` 是 `redact()` 之后的**，而 `redact` 对认不出的东西**失败闭合**，
    所以这里的类型是 `object | None` 而不是 `str` —— 一条被脱敏器改写过的
    内容不保证还是字符串，而在**显示层**为此炸掉是错的地方。
    """

    message_id: str
    direction: str
    created_at: str
    content: object | None


class TargetSummary(_Shape):
    frame_id: str
    definition_id: str
    status: str


class NextEventSummary(_Shape):
    kind: str
    due_at: str


class ConversationListItem(_Shape):
    """`parity` §6.2 第一条读模型的九项，一项不多一项不少。

    **每一项的空态是 `null` 而不是缺席**（ADR-0076 §四.2）：
    九项今天都有真实数据源，所以「没有值」永远只意味着同一件事 ——
    这条会话还没有消息 / 没有联系人 / 没有排期事件。
    """

    conversation_id: str
    contact: ConversationContactSummary | None
    last_message: LastMessageSummary | None
    unread_inbound: int
    control_mode: str
    target: TargetSummary | None
    next_event: NextEventSummary | None
    open_manual_tasks: int
    projection_version: int


class ConversationPage(_Shape):
    items: list[ConversationListItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class MessageItem(_Shape):
    """消息页的一行。

    **后三项是可选的，而「可选」在这里是一句关于方向的话**：
    入站行是一个 `MessageEnvelope`，带 `actor` / `occurred_at`；
    出站行是一条**发送命令**，两样都没有。
    回 `null` 会声称「我们看了，没有」，而事实是出站行**没有这样一件事**
    （`_message_projection` 的 docstring，独立审查 P1-2）。

    ⚠️ **`revoked` 恒为 `False`，而它是答案不是占位**：撤回被 ADR-0048 K15
    移出，没有任何 route 能撤回任何东西。界面据此**不放撤回入口**，不是置灰。

    ⚠️ **没有 `evidence_ref`** —— `parity` §6.2 要它，而后端今天不给。
    这是一处欠账，不是本模型漏了（T378 名下）。
    """

    message_id: str
    direction: str
    created_at: str
    delivery_status: str
    revoked: bool
    content: object | None
    actor: object | None = None
    occurred_at: str | None = None
    correlation_id: str | None = None


class MessagePage(_Shape):
    items: list[MessageItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class ContactDetail(_Shape):
    contact_id: str
    config_id: str
    external_user_id: str
    profile_data: dict[str, object]
    first_seen_at: str
    last_seen_at: str
    message_count: int


class ContactPage(_Shape):
    items: list[ContactDetail]
    next_cursor: str | None
    has_more: bool
    as_of: str


class ReadCursorResult(_Shape):
    last_read_at: str
    last_read_source: str
    last_read_ordinal: int
    last_read_ref: str
    unread_inbound_remaining: int


class ReadCursorResponse(_Shape):
    receipt: CommandReceiptShape
    result: ReadCursorResult


class ProfileReleaseItem(_Shape):
    """`GET /profiles/releases` 的一行 —— **四个元数据字段，没有正文**。

    这四个键与 `ProfileReleaseDocument` 的前四个**逐字相同**，
    而那是有意的：同一份东西在同一个界面上不该有两种拼法。
    列表用来选版本，选中之后由单条读给出正文。
    """

    release_id: str
    profile_id: str
    version: str
    content_hash: str


class ProfileReleasePage(_Shape):
    """⚠️ **本模型是随话术页一起补的，而这条 route 2026-09-02 就存在了。**

    它此前不在 `ROUTE_RESPONSES` 里，所以生成的 TS 给它
    `additionalProperties: true` —— 前端读 `items[0].version` 拿到的是
    `unknown`。ADR-0142 明说本批只覆盖本批接的那些，而这条今天被接了。
    """

    items: list[ProfileReleaseItem]
    next_cursor: str | None
    has_more: bool
    as_of: str


class ProfileReleaseDocument(_Shape):
    """`GET /profiles/releases/{release_id}` —— **商家那半，不是那份文档**。

    ## 它为什么不是「把 profile 发出去」

    ADR-0143 §二 把范围划死：给身份、目标帧、主动性设置、记忆字段、
    知识绑定与素材正文；**不给** `policy_bindings`、
    `confidence_gate_policy_ref`、`manipulation_context_refs`、
    `golden_manifest` —— 那四项是平台的实现细节，
    而**一个被看见的内部字段就不再是内部字段**：商家会据以提要求，
    它却随时会改。

    ## `identity` 在这里摊平成四个键，而不是一个对象

    schema 里 `identity` 的字段集合**恰好就是** `profile_id` / `release_id` /
    `version` / `content_hash`（`additionalProperties: false`），
    也**恰好就是** `GET /profiles/releases` 那条列表每行给的四个。
    嵌一层只会让同一份东西在同一个界面上有两种拼法。

    ⚠️ **这条等价关系是有守卫的**：`identity` 一旦长出第五个字段，
    `tests/contracts/test_profile_release_projection.py` 当场红 ——
    否则那个新字段会安静地掉在摊平的缝里，谁都不会发现。

    ## `content_material_texts` 永远在，哪怕是空的

    它在 schema 里是**可选**的（一个不带 `materials.json` 的 package 就没这个键）。
    但响应里让一个键时有时无，等于把「这个商家没有素材」和
    「这一版的响应漏了素材」变成前端分不出的两件事 ——
    所以缺席时给 `{}`，而**空对象在界面上说的是「还没有素材」**。
    """

    release_id: str
    profile_id: str
    version: str
    content_hash: str
    target_frames: list[dict[str, object]]
    proactivity_policy: dict[str, object]
    memory_fields: list[dict[str, object]]
    knowledge_bindings: list[str]
    content_material_texts: dict[str, str]


class ProfilePinShape(_Shape):
    """这通对话钉在哪一版话术上。一个键，而它是一层对象不是一个字符串 ——
    `profile_pin` 将来还会长出别的（版本、pin 的时刻），而那时改的是这里。"""

    release_id: str


class SlotItem(_Shape):
    """一个信息槽位在投影里的样子。

    ⚠️ **`evidence_event_id` 是单数，而聚合侧存的是数组**（D3）：
    这一格给最新的那一条。要看全部几条是另一件事，**今天没有消费方要它**。
    """

    name: str
    confidence: object | None
    #: `human` / `model`，或 `None` —— ADR-0054 §三.2 加出处之前写下的行没有它。
    source: str | None
    recorded_at: str | None
    evidence_event_id: object | None


class FrameItem(_Shape):
    """目标栈里的一帧。`slots` 是**列表**，不是字典 —— 顺序是投影的一部分。"""

    frame_id: object | None
    definition_id: object | None
    goal: object | None
    status: object | None
    entered_at: object | None
    last_progress_at: object | None
    parent_frame_id: object | None
    interruption_reason: object | None
    resume_confirmation_policy: object | None
    frame_generation: object | None
    slots: list[SlotItem]


class CommitmentDisplay(_Shape):
    """`commitment_display()` 产的那一格 —— **`status` 说清这个 0 是哪一种 0**。

    台账在 M1a 不存在，所以 `count` 永远是 0；而「这一轮确实没有承诺」
    也是 0。光秃秃的数字是**会误导人的真值**（用户 2026-09-13），
    `status` 与 `note` 就是为此加的。
    """

    count: int
    status: str
    note: str


class ConversationCommitments(_Shape):
    """⚠️ **与 `CommitmentDisplay` 只差一个键，而那不是笔误。**

    会话详情把 `count` 换成了 `registered`（**客户端在读那个名字**），
    其余两个键原样带出。两个模型分开写，是因为一个共用模型会让
    「这两处形状本来就不同」这件事消失在一个可选字段里。
    """

    registered: int
    status: str
    note: str


class OutcomeDisplay(_Shape):
    """`outcome_ref` 今天零写点，于是这里永远是 `null` ——
    `status` 与 `note` 说清它是「这一轮没有结果」还是「这个能力还没实现」。"""

    outcome_ref: object | None
    status: str
    note: str


class SlotsWritten(_Shape):
    """接管期里，哪些槽位是人写的、哪些是模型写的、哪些说不清。

    **第三格不是凑数**：ADR-0054 §三.2 加出处之前写下的行没有 `source`，
    把它们算进任何一边都是在编。
    """

    human: list[object]
    model: list[object]
    unattributed: list[object]


class RefusedSlotUpdates(_Shape):
    """接管期里被拒的槽位写入。**`total: 0` 与「没看过」不是一回事** ——
    独立复核 2026-09-06 P1-1 之后，这一格总是给出它是哪一种。"""

    total: int
    by_reason_code: dict[str, int]
    shown: int
    items: list[dict[str, object]]
    note: str


class UnderstandingHaltedByCostGate(_Shape):
    """理解步有没有被成本闸拦过。拦过，接管期那份投影就是不完整的 ——
    而那件事必须说出来，不能让人对着一份缺了几笔的画面做决定。"""

    halted: bool
    occurrences: int
    last_at: str | None
    note: str


class TakeoverProjectionShape(_Shape):
    """「接管期变化投影」——**会话详情与交还预览渲染的是同一个函数**。

    两处各画一遍，就是两处会漂的开始：坐席确认的是一幅画面，
    而详情页显示另一幅。

    ⚠️ **`pending_exit_signal` 与 `takeover_period` 是开放 dict，不是封闭模型。**
    两者都有**多种键集合**：前者在有信号时原样带出引擎写下的那一份，
    没有时给 `{present, note}`；后者按 `source` 分三种
    （`takeover` / `conversation_start` 带一句 `note` / `none`）。
    **给它们各写一个封闭模型，等于宣布只有一种形状** ——
    而变体本身就是信息。

    **它们分两处钉，而那条线画在「要不要数据库」上**：

    * `pending_exit_signal` 的两支由
      `tests/contracts/test_takeover_projection_shapes.py` 直接调产出方枚举；
    * `takeover_period` 的三支与整格 governance 由
      `tests/integration/test_takeover_and_resume_flow.py` 在**真库**上走出来 ——
      它们是库里的事实（`audit_events` / `policy_decisions` 的窄 SELECT），
      **自造几行假数据去喂它，等于把「我以为行长什么样」当成事实。**
    """

    slots_written: SlotsWritten
    pending_exit_signal: dict[str, object]
    commitments_registered: CommitmentDisplay
    takeover_period: dict[str, object]
    refused_slot_updates: RefusedSlotUpdates
    understanding_halted_by_cost_gate: UnderstandingHaltedByCostGate


class ConversationDetail(_Shape):
    """`GET /conversations/{conversation_id}` —— 十四个键，三格嵌套树。

    **批 6 有意没写它**：当时那三格（`takeover_projection` / `commitments` /
    `outcome`）**一个消费方都没有**，而一个没有页面读它的模型
    是一句没人会发现它错了的话。接管页在本批落地，所以它跟着来了。
    """

    conversation_id: str
    lifecycle: object | None
    control_mode: object | None
    projection_version: int
    generation: int
    profile_pin: ProfilePinShape
    channel_config_id: object | None
    last_inbound_at: str | None
    last_outbound_at: str | None
    target_stack: list[FrameItem]
    commitments: ConversationCommitments
    outcome: OutcomeDisplay
    last_transition: object | None
    takeover_projection: TakeoverProjectionShape


class ResumePreview(_Shape):
    """`GET /conversations/{conversation_id}/resume-preview` —— 两阶段的第一阶段。

    **`change_digest` 是这一格存在的理由**：它是坐席「我看的是这一幅画面」
    的凭据，`POST .../resume` 拿它做 CAS。对不上就是 409，
    而那不是错误，是**这通对话在你看的时候又变了**。
    """

    conversation_id: str
    #: AI 现在以为这通对话在聊什么。交还之前请读一遍（裁定八 / 九，T200）。
    conversation_summary: list[str]
    control_mode: object | None
    projection_version: int
    change_digest: str
    takeover_projection: TakeoverProjectionShape


class TakeoverResult(_Shape):
    """**「我刚挂起了几帧、停掉了几个定时器」** —— 别的 route 都不报这个数，
    而它是坐席判断「这通对话是真的安静下来了」的唯一依据。"""

    control_mode: str
    generation: int
    frames_suspended: int
    events_invalidated: int


class TakeoverResponse(_Shape):
    receipt: CommandReceiptShape
    result: TakeoverResult


class ResumeResult(_Shape):
    """⚠️ **`timers_restored` 恒为 0，而它照样是一个字段。**

    接管作废掉的定时器**不会回来**（ADR-0054 §三.4）：它们是照着一份
    已经不存在的计划铸的。这个 0 是一条裁定，不是一个占位符 ——
    而 `frames_active` 是 T4 那个「答 200 却什么都没恢复」的缺陷
    唯一能被外部看见的地方。
    """

    control_mode: str
    generation: int
    frames_active: int
    timers_restored: int


class ResumeResponse(_Shape):
    receipt: CommandReceiptShape
    result: ResumeResult


class SlotWriteResult(_Shape):
    """写槽位是**按名字 upsert**，不是替换整帧的槽位集合 ——
    所以这里报的是「这次写进去几个」，不是「这一帧现在有几个」。"""

    frame_id: str
    slots_written: int
    projection_version: int


class SlotWriteResponse(_Shape):
    receipt: CommandReceiptShape
    result: SlotWriteResult


#: route → 响应模型。**`ROUTE_RESPONSES` 是这张表的唯一副本**，
#: `app.py` 的装饰器与那条守卫用例都读它，所以两边不可能各说各话。
#:
#: 键是 `(method, path)`，与 `openapi.json` 里的写法逐字相同。
#: **只放已经被守卫用例覆盖的那些** —— 见模块 docstring 最后一段。
ROUTE_RESPONSES: dict[tuple[str, str], type[_Shape]] = {
    # A 面
    ("POST", "/auth/login"): LoginResponse,
    ("POST", "/auth/refresh"): RefreshResponse,
    ("GET", "/auth/me"): IdentityResponse,
    # B 面 —— 员工
    ("GET", "/employees"): EmployeePage,
    ("POST", "/employees"): CommandReceiptShape,
    ("PATCH", "/employees/{employee_id}"): EmployeeUpdatedResponse,
    ("GET", "/employees/stats"): EmployeeStatsResponse,
    ("GET", "/employees/{employee_id}/stats"): EmployeeDetailStatsResponse,
    ("POST", "/employees/{employee_id}/deactivate"): EmployeeDeactivateResponse,
    ("POST", "/employees/{employee_id}/purge"): EmployeePurgeResponse,
    ("PUT", "/employees/{employee_id}/roles"): EmployeeRolesResponse,
    ("POST", "/employees/{employee_id}/password-reset"): PasswordResetResponse,
    # B 面 —— 角色与权限
    ("GET", "/permissions"): PermissionPage,
    ("GET", "/roles"): RolePage,
    ("POST", "/roles"): CommandReceiptShape,
    ("PUT", "/roles/{role_id}/permissions"): RolePermissionsUpdatedResponse,
    # B 面 —— 租户与策略
    ("GET", "/tenants/current"): CurrentTenantResponse,
    ("PUT", "/tenants/current/manager-contact"): ManagerContactWriteResponse,
    ("GET", "/control-policy"): ControlPolicyResponse,
    ("PUT", "/control-policy"): ControlPolicyWriteResponse,
    # 批 1/9 —— 人工发送。回执用 `accepted`：这一刻发生的是**排进队列**，
    # 不是送达。界面上那个「已发送」只能来自投递回执（parity §6.2）。
    ("POST", "/conversations/{conversation_id}/messages"): CommandReceiptShape,
    # 批 6 —— 会话与联系人
    ("GET", "/conversations"): ConversationPage,
    ("GET", "/conversations/{conversation_id}/messages"): MessagePage,
    ("POST", "/conversations/{conversation_id}/read"): ReadCursorResponse,
    ("GET", "/contacts"): ContactPage,
    ("GET", "/contacts/{contact_id}"): ContactDetail,
    ("GET", "/contacts/by-conversation/{conversation_id}"): ContactDetail,
    ("GET", "/profiles/releases"): ProfileReleasePage,
    # 批 8 —— 话术（T373 / ADR-0143 §二）。**只给商家那半**，
    # 而「不给的那四项」由 `test_profile_release_projection.py` 钉着。
    ("GET", "/profiles/releases/{release_id}"): ProfileReleaseDocument,
    # 批 7 —— 接管与交还（ADR-0054 §三.4 的两阶段命令）
    ("GET", "/conversations/{conversation_id}"): ConversationDetail,
    ("POST", "/conversations/{conversation_id}/takeover"): TakeoverResponse,
    ("GET", "/conversations/{conversation_id}/resume-preview"): ResumePreview,
    ("POST", "/conversations/{conversation_id}/resume"): ResumeResponse,
    (
        "PUT",
        "/conversations/{conversation_id}/frames/{frame_id}/slots",
    ): SlotWriteResponse,
    # **`DELETE /roles/{role_id}` 不在表里，而那不是遗漏**：它回 204，
    # 没有响应体。一个给 204 写的模型是一句假话。
    #
    # **`GET /conversations/{id}` 批 6 时不在表里**，理由是那三格嵌套树
    # 当时一个消费方都没有。**接管页在本批落地，所以它进表了**（见上）——
    # 那句「随批 7 一起写」兑现在这里。
}


#: 成功状态不是 200 的那几条。**默认 200，例外显式列出。**
#:
#: **为什么需要这张表**：`responses=` 的键是状态码，而把模型挂在一个
#: 这条 route 永远不会返回的状态上，等于**给一个不存在的响应标了类型**，
#: 同时让真正那个留着 `additionalProperties: true`。
#: 2026-09-20 实测撞到过一次：`POST .../messages` 回 202，而模型挂在 200 上 ——
#: 生成的 TS 于是给 200 一个 `CommandReceiptShape`、给 202 一个 `unknown`。
#:
#: **那次是写代码时肉眼发现的，不是守卫发现的** ——
#: 所以同批加了一条判据（`test_the_documented_status_is_the_one_the_route_returns`）。
ROUTE_SUCCESS_STATUS: dict[tuple[str, str], int] = {
    ("POST", "/conversations/{conversation_id}/messages"): 202,
    # ⚠️ **这一条是新判据当场抓到的既有缺陷**（2026-09-20）：
    # 它从进 `ROUTE_RESPONSES` 那天起就回 201，而模型一直挂在 200 上 ——
    # 生成的 TS 于是给一个**永远不会发生的 200** 标了 `CommandReceiptShape`，
    # 而真正的 201 是 `unknown`。没有任何东西会红，因为此前没有判据比状态。
    ("POST", "/employees"): 201,
}


def success_status(method: str, path: str) -> int:
    """这条 route 成功时回哪个状态码。"""

    return ROUTE_SUCCESS_STATUS.get((method.upper(), path), 200)


def openapi_responses(method: str, path: str) -> dict[int | str, dict[str, object]]:
    """给一条 route 生成 `responses=` 的那一格。

    **查不到就返回空 dict**，不抛 —— 一条还没写模型的 route 应当和从前一样，
    而不是启动不起来。那种「少一条就炸」的形状会让这张表只能一次性填满，
    而 ADR-0142 明说本批只覆盖本批接的那些。
    """

    model = ROUTE_RESPONSES.get((method.upper(), path))
    if model is None:
        return {}
    return {success_status(method, path): {"model": model}}
