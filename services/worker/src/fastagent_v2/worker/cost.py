"""RMB cost ledger and ceiling enforcement.

One row is written per model call, before the call, from a pre-call estimate;
the measured usage is settled into the same row afterwards. A call that never
returns therefore still leaves a trace, and a hard ceiling can block *before*
money is spent rather than after.

The design refuses three tempting shortcuts:

1. **No zero for unknown.** When the release carries no price, cost is NULL, not
   0. A zero would make an unmeasured call look free and a ceiling look
   satisfied.
2. **No enforcement without prices.** If the active release cannot price a role,
   enforcement reports itself inactive. It never falls back to an assumed price,
   and it never reports "under ceiling" for a number it could not compute.
3. **No loosening by tenant.** A tenant override may tighten a platform ceiling.
   An override that exceeds it is clamped back to the platform value, because a
   tenant setting must not be able to raise the platform's own limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.runtime.db import Database
from fastagent_v2.worker.model_policy import ModelPolicyRelease, RoleBinding
from fastagent_v2.worker.policy_output import (
    PolicyOutput,
    audit_only,
    record_policy_decision,
)
from psycopg.types.json import Jsonb

#: Ceiling window names, in the order they are reported.
WINDOWS = ("daily", "monthly")

#: **Judged on settled spend since 2026-09-10** (T259, user's ruling; ADR-0124).
#: It used to be judged on the forecast, and the forecast is **built to be about
#: 9x high on purpose** -- `worst_case_input_tokens` bounds the input, the
#: output is reserved at `max_output_tokens`, and both are priced at the peak
#: uncached rate. Measured over 31 real calls: input 4.18x, output 7.63x, cost
#: **9.78x** (and that number confirms `ESTIMATED_CEILING_FACTOR`, which was
#: fitted to a single call -- see T43).
#:
#: Against a `soft_conversation` of 0.11 a single call's forecast is already
#: 0.07-0.08, so **every conversation past its first turn warned**: the third
#: real window produced eleven of these. A gate that always fires is not a gate,
#: and the real overspend it exists to announce would arrive inside that noise.
#:
#: **The forward-looking warning did not go away** -- it is
#: `ESTIMATED_REASON_CODE`, which ADR-0034 created for exactly that job and
#: which still fires at `hard`. What changed is that these two codes now mean
#: what their names have always claimed: this one is "money actually charged is
#: approaching the budget", that one is "the unproven number says we may already
#: be over".
SOFT_REASON_CODE = "cost_soft_ceiling"
HARD_REASON_CODE = "cost_hard_ceiling"

#: ADR-0034. The estimate crossed a ceiling that the settled spend has not. This
#: warns and lets the call proceed: an unproven number may not stop a customer's
#: conversation. It is a separate code from `SOFT_REASON_CODE` because the two
#: mean different things to whoever is paged -- "spending is approaching the
#: budget" versus "the forward-looking number says we may already be over".
ESTIMATED_REASON_CODE = "cost_estimated_ceiling"

#: ADR-0036. The estimate crossed `hard x ESTIMATED_CEILING_FACTOR`. This one
#: *does* deny, and its only job is the concurrency guard that the settled judge
#: cannot provide: two workers both observe `settled = 0` and would both be let
#: through, because neither call has returned yet. Distinct from
#: `HARD_REASON_CODE` on purpose -- a denial here is not a statement about money
#: actually spent, and anyone diagnosing one must not read it as such.
ESTIMATED_HARD_REASON_CODE = "cost_estimated_hard_ceiling"

#: ADR-0036. Too many calls in this window are still `estimated`. Distinct from
#: every ceiling code: it does not mean the budget is gone, it means settlement
#: has stopped happening, and the spend the settled judge can see is therefore
#: missing an unknown amount. See `_UNSETTLED_*` below for why a count is the
#: right instrument for that and a sum is not.
UNSETTLED_BACKLOG_REASON_CODE = "cost_unsettled_backlog"

#: ADR-0036. How far above a hard ceiling the *estimate* has to reach before it
#: denies. It has to cover how conservative the estimate is, or the premature
#: breaking that ADR-0034 removed simply reappears one factor higher up.
#:
#: **This default is fitted to a single measured call** (T11, 2026-09-03): after
#: `worst_case_input_tokens` stopped reserving the contract maximum, that call's
#: estimate/actual ratio was about 9.2. ADR-0036 records both failure directions
#: and says in as many words that it is not calibrated. Re-fitting it once more
#: real calls exist is a separate task -- do not read "the ADR says 9" as
#: evidence.
ESTIMATED_CEILING_FACTOR = Decimal("9")

#: Which number each warning judged, for the audit row that carries it.
#: **Keyed on the reason code** so that the code and the basis cannot disagree:
#: they are chosen at the same place, and a new warning code that forgets to
#: land here raises `KeyError` in a test rather than writing a plausible lie.
_WARNING_BASIS = {
    SOFT_REASON_CODE: "settled",
    ESTIMATED_REASON_CODE: "estimated",
    ESTIMATED_HARD_REASON_CODE: "estimated",
}


def warning_basis(reason_code: str | None) -> str:
    """Which number this warning judged. **Raises rather than guessing.**

    A warning always carries a reason code, so `None` here means the caller
    changed and this function did not. Returning a default would put a
    plausible-looking word into the row an alert is built on, which is the one
    outcome worth failing loudly to avoid.
    """

    basis = _WARNING_BASIS.get(reason_code or "")
    if basis is None:
        raise ValueError(f"no basis registered for ceiling warning {reason_code!r}")
    return basis

def provable_max_call_cny(release: ModelPolicyRelease) -> Decimal:
    """The provable maximum a single call can cost, in CNY -- **computed, not written down.**

    Input is bounded by the contract at `MAX_MODEL_INPUT_BYTES +
    MODEL_INPUT_FRAMING_TOKENS`, output by the role's `max_output_tokens`, and
    both are priced at the peak, fully uncached rate. The worst role wins,
    because the count gate has to be sized against the worst case.

    ## 它为什么是个函数

    2026-09-15 之前它是 `_PROVABLE_MAX_CALL_CNY = Decimal("0.070656")` ——
    **一个从 `MAX_MODEL_INPUT_BYTES` 推出来、然后被写死的结果**。
    同一天把上限从 16,384 抬到 49,000 的那一批**没有把它带上**：
    按新上限现算是 **0.168504（2.385 倍）**，而钉它的那条用例读的正是那个写死的数，
    **于是它绿着**（独立复核 2026-09-15 §六）。

    **这正是本仓一直在修的那个形状** —— 一处推导、一处结果，两者今天恰好一致，
    直到有一天不一致，而没有任何东西钉它们相等。**这一次是预算链自己造出来的。**

    ## 算式不在这里重写

    调的是 `binding.cost_cny(..., force_peak=True)`，也就是 `CostGovernor.estimate`
    用的那一个。把「输入价 x token + 输出价 x token」再抄一遍就是造第三份副本，
    而本函数存在的理由恰恰是副本会漂。

    ## 口径取 release 声明的**全部**角色，不只请求路径那三个

    `COMMITMENT_CAPABILITY` 与 `VISION` 今天没有调用方，但它们**声明了**绑定与价目。
    计数闸兜的是「未结算的钱」，而未结算的行不问是谁产生的 ——
    少算一个角色，兜出来的上界就是假的。

    **An earlier version of this comment said "~0.0256", which was wrong by 2.4x**
    (independent review 2026-09-03, P2-1): it priced a *typical* Chinese request
    at roughly three bytes per token rather than the contract limit the
    reservation is actually allowed to reach. Sizing a bound on a typical value
    instead of the provable one is the mistake, not the arithmetic slip.
    """

    input_tokens = MAX_MODEL_INPUT_BYTES + MODEL_INPUT_FRAMING_TOKENS
    worst = Decimal(0)
    for name in release.document.get("roles") or {}:
        binding = release.role(name)
        if not binding.can_price or binding.max_output_tokens is None:
            continue
        cost = binding.cost_cny(
            input_tokens=input_tokens,
            output_tokens=binding.max_output_tokens,
            force_peak=True,
        )
        if cost is None:
            continue
        worst = max(worst, Decimal(str(cost)))
    return worst

#: T55. How long a reservation may stay `estimated` before it is treated as
#: abandoned. Derived, not chosen: the longest single call the active release
#: permits is `deadline_ms = 8000`, and the whole inbox event is bounded by
#: `runtime_deadline_seconds = 30`. Fifteen minutes is **thirty times** the
#: event deadline, so no legitimately in-flight call reaches it.
#:
#: The age test lives in SQL rather than depending on the reconciler having run.
#: That matters: a gate that only works once a scheduled job is deployed is a
#: gate that reports "healthy" on a system where the job was never installed --
#: which is exactly how the Stage collector spent a day pushing seven metrics
#: while everyone believed it pushed ten.
RECONCILE_AFTER_SECONDS = 900

#: ADR-0036, the count gate, per conversation. A conversation is processed
#: serially -- the scheduler runs no inference, and one inbox turn's model calls
#: are sequential -- so its steady-state number of unsettled rows is 1. Anything
#: above a handful means rows are not leaving `estimated` at all. Four code
#: paths can do that: a timeout, a transport failure, an HTTP >= 400 with no
#: usable `usage` block, and a malformed response with none -- and **no
#: reconciler exists** in this repository to clean up after any of them
#: (`mark_failed()` has no production caller). Until one does, this is the only
#: bound on how much real spend the settled judge cannot see.
#:
#: **This counts abandoned rows, not in-flight ones.** T55 found the two were
#: conflated: a healthy tenant with four concurrent calls and a broken one with
#: four rows rotting since yesterday looked identical to this gate. The
#: derivation below was always about *invisible money*, which is what abandoned
#: means -- so the numbers carry over unchanged, and now they measure the thing
#: they were derived for.
#:
#: **判据：这道闸放行的「看不见的钱」不该超过它守的那个窗口的硬顶。**
#: 第一版写的是 20，理由是「20 倍突发容忍」，而从没算过这个窗口自己的账
#: （独立审查 2026-09-03 P2-2）：`20 x 0.070656 = 1.41` 对 `hard_conversation` 0.27,
#: 已结算那位判官可以在**五个会话硬顶**的钱看不见之后才出声。
#: 会话里没有合法并发（它串行处理），**没有突发要容忍**。
#:
#: **2026-09-15：这个数不再写死。** 当时按 `4 x 0.070656 = 0.283`（约一个硬顶）定成 4，
#: **而那个 4 与 `_PROVABLE_MAX_CALL_CNY` 同源 —— 同样是一个被写死的推导结果**。
#: 预算链把 `MAX_MODEL_INPUT_BYTES` 抬到 49,000 之后它一起过期了：
#: 按现算的 0.168504，`4 x = 0.674`，是会话硬顶的 **2.5 倍**，
#: 而钉它的用例给自己定的标准是 1.2 倍以内。
#: 用户 2026-09-15 裁定**连它一起现算**：同一批里把整族写死的推导结果一次消掉，
#: 否则下一次抬上限还要再过期一遍。
#:
#: **今天它算出来是 1，而这不是一个更严的新政策** ——
#: 上面那段本来就写着「会话串行处理，稳态未结算行数就是 1」。
#: **推导给出的答案与那句前提本来就吻合**，是 4 那个数在旧上限下显得宽松。
#:
#: **这一格不数 `role = 'EMBEDDING'`**（用户 2026-09-12 裁定，独立审查 P1-3）。
#: 上面整段推导是**按模型调用**做的 —— 「一条会话里四条走了不回来的调用**已经是
#: 病态**」这句话的前提是那四条调用都是这条会话**必需**的。检索（裁定三的第四步）
#: 不是：**裁定六逐字说它失败不许停掉这一回合**，所以「嵌入端点断了一下午」
#: 这件事里，会话是**健康**的，而这一格**不带时间窗、只增不减**。
#:
#: 不排掉它的后果是量得出来的：嵌入端点故障而对话模型一切正常时，
#: 同一条会话里**第 5 个检索回合之后每一次 `reserve()` 都被拒** ——
#: 包括 understand / plan / generate，于是 `_persist_no_send`，
#: **客户从此在这条会话里收不到任何回复**。一个加分项拖垮了必需品。
#:
#: **只排这一格，日窗那一格照数**：那一格有日窗、上限 200，自限且仍然看得见；
#: 运维那个只读计数 `abandoned_reservations()` 也照数。
#: 换句话说：**嵌入的积账仍然可见，只是不再有资格单独掐死一条会话。**
#: 钉住它的是 `tests/integration/test_retrieval_metering.py` 里那条
#: 「嵌入连续失败 N 轮之后模型调用仍然过得去，而同样多的模型积账会被拒」。
def derived_unsettled_conversation_limit(release: ModelPolicyRelease) -> int:
    """会话计数闸的默认值 —— **也是现算的**。理由见上面那段。

    `floor(hard_conversation / 可证单次最坏)`，至少 1。

    **价目缺失时返回 1，不是返回一个宽松的默认**：那时
    `provable_max_call_cny` 返回 0，除不动，而**一道不知道自己在兜多少钱的闸，
    不该按「大概没事」放行**。
    """

    worst = provable_max_call_cny(release)
    ceiling = Decimal(str(release.ceilings_cny.get("hard_conversation") or 0))
    if worst <= 0 or ceiling <= 0:
        return 1
    return max(1, int(ceiling / worst))

#: The same instrument per tenant-day. 200 permanently unsettled calls is not a
#: busy day, it is a broken settlement path. The daily window is where real
#: concurrency lives, and tightening it far enough to matter financially would
#: start refusing healthy traffic, so this one is **chosen and then checked**,
#: not derived -- the conversation gate above is the one doing the precise work.
#:
#: **那个「检查」的数 2026-09-15 现读是 37.0%，而当年接受的是 15.5%。**
#: 原文写的是 `200 x 0.070656 = 14.13 CNY, about 15.5% of hard_daily (91)`,
#: 按现算的可证单次最坏是 `200 x 0.168504 = 33.70 CNY = 37.0%`。
#: **200 本身不动** —— 它与会话那道闸不同源：**会话那个是推导出来的结果**
#: （所以改成现算），**这个是选定的数，百分比只是事后算给人看的一个量级**。
#: **用户 2026-09-15 重新接受了这个量级**（当年 15.5% -> 今天 37.0%），
#: 而接受的理由值得写下来，因为这个数很容易被读成「商家一天只能花 33.70 元」：
#:
#: **它兜的是可观测性的盲区，不是钱。** 33.70 的含义是
#: 「**结算通道真的坏掉时**，在这道闸出声之前，最多有这么多花费是已结算判据看不见的」——
#: 真实花费仍由 `hard_daily`（91 元）管着。
#:
#: **而它数的也不是流量**：上面那个 `count(*) FILTER` 只数
#: 「已作废」或「挂了 `RECONCILE_AFTER_SECONDS` 还没结算」的行 ——
#: **一次成功的调用回来就结算了，不进这个计数**。所以
#: 「一个商家一天接五六个客户就被拦」不会发生：要顶到 200，
#: 得是同一个租户一天里有 200 次调用**发出去再也没回来**。
#: **它是一道失控闸，不是流量闸**，而 T55 记的正是这两者曾经被混在一起。
#:
#: **收紧它的代价是反的**：真正失控时（供应商挂掉、大量调用不回来）它会更早拦，
#: 而拦的方式是**拒掉后续所有调用** —— 供应商恢复之后商家仍然发不出消息。
#:
#: **A count, not a sum, on purpose.** Summing the estimates of unsettled rows
#: would be a third spend judge built on the same unproven number ADR-0034 just
#: removed from the breaker. A count says something the estimate cannot make
#: wrong: this many calls left and never came back.
_UNSETTLED_DAILY_LIMIT = 200

#: Provider-adapter contract: the complete JSON request presented to the model
#: must fit in this many UTF-8 bytes.  The limit is checked before transport.
MAX_MODEL_INPUT_BYTES = 49_000

#: Worst-case allowance for provider-added message framing/special tokens.  The
#: request itself is reserved at one token per byte, so no tokenizer heuristic
#: can make a Unicode/emoji/random-string request exceed the reservation.  A
#: provider binding that needs more hidden framing must not use this adapter
#: until this contract is raised and re-reviewed.
MODEL_INPUT_FRAMING_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """Return a tokenizer-independent upper bound for serialized input.

    Every token consumed from a UTF-8/byte-fallback input covers at least one
    input byte. Reserving one token per byte is therefore an upper bound, unlike
    character-ratio heuristics. ``OpenAICompatibleChatClient`` passes the whole
    serialized request here, including schemas and message framing.
    """

    return len(text.encode("utf-8"))


def worst_case_input_tokens(serialized_request: str) -> int:
    """Enforce the request contract and return its hard-ceiling reservation."""

    request_bytes = estimate_tokens(serialized_request)
    if request_bytes > MAX_MODEL_INPUT_BYTES:
        raise ValueError(
            f"model input is {request_bytes} bytes; contract limit is {MAX_MODEL_INPUT_BYTES}"
        )
    # T43. This used to return `MAX_MODEL_INPUT_BYTES + MODEL_INPUT_FRAMING_TOKENS`
    # -- a constant -- so every call reserved the whole contract no matter how
    # small it was. That single line is the largest term in the 25x
    # over-estimation measured on 2026-09-03: 17408 reserved against 1483
    # actually consumed, 11.7x on its own.
    #
    # The observed size is still a provable upper bound: a token consumed from a
    # UTF-8/byte-fallback input covers at least one input byte, so one token per
    # byte cannot under-reserve *this* request. What the old constant bought was
    # a bound that survived prompt composition changing later; that mattered
    # while the estimate was the breaker, and ADR-0034 moved the breaker onto
    # settled spend. The reservation is now a ledger entry and an alert input,
    # and for both of those a number that tracks the request is worth more than
    # one that ignores it.
    return request_bytes + MODEL_INPUT_FRAMING_TOKENS


class LedgerRowNotSettleable(RuntimeError):
    """A settle found no `estimated` row to write onto.

    Raised rather than returned, because the value `settle()` would otherwise
    hand back is a correctly computed cost for a row that did not receive it.
    """


class CostCeilingExceeded(RuntimeError):
    """Raised instead of making a call that would cross a hard ceiling."""

    reason_code = HARD_REASON_CODE

    def __init__(
        self,
        message: str,
        *,
        window: str,
        limit: Decimal,
        projected: Decimal,
        reason_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.window = window
        self.limit = limit
        self.projected = projected
        # ADR-0036 gave the governor three ways to refuse, and the pipeline
        # reads `error.reason_code` straight into the no-send record and from
        # there into `audit_events` -- which is what the Stage collector counts.
        # Leaving the class attribute to answer for all three would have made
        # every refusal look like "the money ran out", including the two that
        # mean the opposite (a concurrency guard, and settlement having stopped).
        if reason_code is not None:
            self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class Ceilings:
    soft_daily: Decimal
    hard_daily: Decimal
    soft_monthly: Decimal
    hard_monthly: Decimal
    tenant_override_applied: bool
    clamped_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CeilingDecision:
    effect: str  # "allow" | "warn" | "deny"
    reason_code: str | None
    window: str | None
    limit: Decimal | None
    projected: Decimal | None
    enforcement_active: bool
    inactive_reason: str | None = None

    @property
    def is_deny(self) -> bool:
        return self.effect == "deny"

    @property
    def is_warn(self) -> bool:
        return self.effect == "warn"


def resolve_ceilings(*, release: ModelPolicyRelease, override: dict[str, Any] | None) -> Ceilings:
    """Combine the release defaults with a tenant override.

    An override may only tighten. One that tries to raise a platform ceiling is
    clamped and reported in `clamped_fields` rather than silently ignored.
    """

    defaults = release.ceilings_cny
    platform = {
        "soft_daily": Decimal(str(defaults["soft_daily"])),
        "hard_daily": Decimal(str(defaults["hard_daily"])),
        "soft_monthly": Decimal(str(defaults["soft_monthly"])),
        "hard_monthly": Decimal(str(defaults["hard_monthly"])),
    }
    values = dict(platform)
    clamped: list[str] = []
    applied = False

    if override:
        for field in values:
            raw = override.get(f"{field}_cny")
            if raw is None:
                continue
            applied = True
            candidate = Decimal(str(raw))
            if candidate > platform[field]:
                clamped.append(field)
                continue
            values[field] = candidate

    return Ceilings(
        soft_daily=values["soft_daily"],
        hard_daily=values["hard_daily"],
        soft_monthly=values["soft_monthly"],
        hard_monthly=values["hard_monthly"],
        tenant_override_applied=applied,
        clamped_fields=tuple(clamped),
    )


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """One consistent read of the ledger, feeding all three judges.

    ADR-0034 / ADR-0036 need three different numbers about the same window, and
    they have to come from the same instant. Taken from separate statements they
    could disagree -- a call could settle in between, so the settled sum would
    include money the unsettled count still claims is in flight -- and a refusal
    that cannot be reproduced from one snapshot is not diagnosable.
    """

    settled_daily: Decimal
    settled_monthly: Decimal
    settled_conversation: Decimal
    projected_daily: Decimal
    projected_monthly: Decimal
    projected_conversation: Decimal
    #: Rows that left and never came back: already aged out to `unreconciled`,
    #: **or** still `estimated` past `RECONCILE_AFTER_SECONDS`. The second half
    #: is what keeps this correct when the reconciler has not run.
    abandoned_daily: int
    abandoned_conversation: int
    #: Still legitimately in flight. Reported in the refusal so that "settlement
    #: stopped" is never confused with "the tenant is busy".
    in_flight_conversation: int


@dataclass(frozen=True, slots=True)
class CallContext:
    tenant_id: UUID
    conversation_id: UUID
    correlation_id: UUID


class CostGovernor:
    """Ledger writer and ceiling gate for one worker process."""

    def __init__(self, *, database: Database, release: ModelPolicyRelease) -> None:
        self._database = database
        self._release = release

    @property
    def release(self) -> ModelPolicyRelease:
        return self._release

    def enforcement_status(self, binding: RoleBinding) -> tuple[bool, str | None]:
        """Whether a ceiling can be enforced for this binding, and why not."""

        if not self._release.ceilings_cny.get("enforcement_active", True):
            return False, "release ceiling proposal is awaiting user confirmation"
        if not binding.can_price:
            return False, "release carries no price for this role"
        if binding.max_output_tokens is None:
            return False, "release declares no max_output_tokens, so a call cannot be bounded"
        return True, None

    def estimate(
        self, *, binding: RoleBinding, system_prompt: str, user_prompt: str
    ) -> tuple[int, int, Decimal | None]:
        input_tokens = worst_case_input_tokens(system_prompt + user_prompt)
        output_tokens = binding.max_output_tokens or 0
        # Pre-call estimates and hard blocks always use peak, fully uncached rates.
        cost = binding.cost_cny(
            input_tokens=input_tokens, output_tokens=output_tokens, force_peak=True
        )
        return input_tokens, output_tokens, None if cost is None else Decimal(str(cost))

    def spent(self, tenant_id: UUID) -> tuple[Decimal, Decimal]:
        """Settled RMB spend for today and this month, in Asia/Shanghai."""

        with self._database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT
                    coalesce(sum(cost_cny) FILTER (
                        WHERE (occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                              = (now() AT TIME ZONE 'Asia/Shanghai')::date
                    ), 0) AS daily,
                    coalesce(sum(cost_cny) FILTER (
                        WHERE to_char(occurred_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                              = to_char(now() AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                    ), 0) AS monthly
                  FROM model_invocation_costs
                 WHERE tenant_id = %s AND status = 'settled'
                """,
                (tenant_id,),
            ).fetchone()
        assert row is not None
        return Decimal(str(row["daily"])), Decimal(str(row["monthly"]))

    def tenant_override(self, tenant_id: UUID) -> dict[str, Any] | None:
        with self._database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT soft_daily_cny, hard_daily_cny, soft_monthly_cny, hard_monthly_cny
                  FROM tenant_cost_ceilings WHERE tenant_id = %s
                """,
                (tenant_id,),
            ).fetchone()
        return dict(row) if row else None

    def conversation_spent(self, context: CallContext) -> Decimal:
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            row = connection.execute(
                """SELECT coalesce(sum(cost_cny), 0) AS cost
                     FROM model_invocation_costs
                    WHERE tenant_id = %s AND conversation_id = %s AND status = 'settled'""",
                (context.tenant_id, context.conversation_id),
            ).fetchone()
        assert row is not None
        return Decimal(str(row["cost"]))

    def precheck(
        self,
        *,
        binding: RoleBinding,
        context: CallContext,
        estimated_cost: Decimal | None,
    ) -> CeilingDecision:
        active, inactive_reason = self.enforcement_status(binding)
        if not active or estimated_cost is None:
            return CeilingDecision(
                effect="allow",
                reason_code=None,
                window=None,
                limit=None,
                projected=None,
                enforcement_active=False,
                inactive_reason=inactive_reason or "call cost could not be estimated",
            )

        # T43. This used to read `spent()` / `conversation_spent()`, which are
        # settled-only, while `reserve()` -- the path production actually takes
        # -- read an estimate-inclusive total. The two entry points therefore
        # answered differently for the same tenant, and the unit tests that
        # exercise the ceiling arithmetic all go through *this* one. Both now
        # run the same judges over the same snapshot; a preview that can
        # disagree with the decision it previews is worse than no preview.
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            override_row = connection.execute(
                """
                SELECT soft_daily_cny, hard_daily_cny, soft_monthly_cny, hard_monthly_cny
                  FROM tenant_cost_ceilings WHERE tenant_id = %s
                """,
                (context.tenant_id,),
            ).fetchone()
            spend = self._snapshot(
                connection, context=context, occurred_at=datetime.now(UTC)
            )
        return self._decision_for_spend(
            estimated_cost=estimated_cost,
            override=dict(override_row) if override_row else None,
            spend=spend,
        )

    def _snapshot(
        self, connection: Any, *, context: CallContext, occurred_at: datetime
    ) -> LedgerSnapshot:
        """Read every number the three judges need, in one pass.

        ADR-0034 / ADR-0036. `settled_*` is the only family allowed to say a
        customer's spending stopped them; `projected_*` adds the unproven
        estimates and may only warn, except at the amplified threshold where it
        serves as the concurrency guard; `unsettled_*` counts rows that left and
        never came back.
        """

        row = connection.execute(
            """
            SELECT
                coalesce(sum(cost_cny) FILTER (
                    WHERE status = 'settled'
                      AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                          = (%s AT TIME ZONE 'Asia/Shanghai')::date
                ), 0) AS settled_daily,
                coalesce(sum(cost_cny) FILTER (
                    WHERE status = 'settled'
                      AND to_char(occurred_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                          = to_char(%s AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                ), 0) AS settled_monthly,
                coalesce(sum(cost_cny) FILTER (
                    WHERE status = 'settled' AND conversation_id = %s
                ), 0) AS settled_conversation,
                coalesce(sum(coalesce(cost_cny, estimated_cost_cny)) FILTER (
                    -- `unreconciled` is included on purpose (independent review 2026-09-03,
                    -- P1-2). Leaving it out meant a row's estimate vanished from these
                    -- sums the moment the reconciler moved it -- so whether the
                    -- reconciler had run changed the money judgement, which is the exact
                    -- property this batch claims not to have. It is also what the
                    -- migration comment criticises `failed` for: "asserting it releases
                    -- the reservation". Money we cannot account for is money we must
                    -- keep counting.
                    WHERE status IN ('estimated', 'settled', 'unreconciled')
                      AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                          = (%s AT TIME ZONE 'Asia/Shanghai')::date
                ), 0) AS projected_daily,
                coalesce(sum(coalesce(cost_cny, estimated_cost_cny)) FILTER (
                    WHERE status IN ('estimated', 'settled', 'unreconciled')
                      AND to_char(occurred_at AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                          = to_char(%s AT TIME ZONE 'Asia/Shanghai', 'YYYY-MM')
                ), 0) AS projected_monthly,
                coalesce(sum(coalesce(cost_cny, estimated_cost_cny)) FILTER (
                    WHERE status IN ('estimated', 'settled', 'unreconciled')
                      AND conversation_id = %s
                ), 0) AS projected_conversation,
                count(*) FILTER (
                    WHERE (
                        status = 'unreconciled'
                        OR (status = 'estimated' AND occurred_at <= %s - %s * interval '1 second')
                    )
                      AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                          = (%s AT TIME ZONE 'Asia/Shanghai')::date
                ) AS abandoned_daily,
                count(*) FILTER (
                    WHERE (
                        status = 'unreconciled'
                        OR (status = 'estimated' AND occurred_at <= %s - %s * interval '1 second')
                    )
                      AND conversation_id = %s
                      -- **检索的嵌入调用不算进这一格**（用户 2026-09-12 裁定，
                      -- 独立审查 2026-09-12 P1-3）。理由见下面
                      -- `derived_unsettled_conversation_limit` 的推导里新加的那一段：
                      -- 这一格**不带时间窗、只增不减**，而裁定六保证「检索一直失败」
                      -- 时这条会话是**健康**的 —— 让一个加分项把它顶到上限，
                      -- 被拒掉的会是 understand / plan / generate。
                      -- **日窗那一格照数** —— 那是**上面**那个 `abandoned_daily`，
                      -- 不是下面（下面是 `in_flight_conversation`）。
                      -- 方位先前写反了，复审 N-2 抓的：照那句话去核的人会核错格子，
                      -- 而这段注释正是为一道刚被开了口子的成本闸写的。
                      -- `abandoned_daily` 有日窗、上限 200，自限且仍然看得见。
                      AND role <> 'EMBEDDING'
                ) AS abandoned_conversation,
                count(*) FILTER (
                    WHERE status = 'estimated'
                      AND occurred_at > %s - %s * interval '1 second'
                      AND conversation_id = %s
                ) AS in_flight_conversation
              FROM model_invocation_costs
             WHERE tenant_id = %s
            """,
            (
                occurred_at,
                occurred_at,
                context.conversation_id,
                occurred_at,
                occurred_at,
                context.conversation_id,
                occurred_at,
                RECONCILE_AFTER_SECONDS,
                occurred_at,
                occurred_at,
                RECONCILE_AFTER_SECONDS,
                context.conversation_id,
                occurred_at,
                RECONCILE_AFTER_SECONDS,
                context.conversation_id,
                context.tenant_id,
            ),
        ).fetchone()
        assert row is not None
        return LedgerSnapshot(
            settled_daily=Decimal(str(row["settled_daily"])),
            settled_monthly=Decimal(str(row["settled_monthly"])),
            settled_conversation=Decimal(str(row["settled_conversation"])),
            projected_daily=Decimal(str(row["projected_daily"])),
            projected_monthly=Decimal(str(row["projected_monthly"])),
            projected_conversation=Decimal(str(row["projected_conversation"])),
            abandoned_daily=int(row["abandoned_daily"]),
            abandoned_conversation=int(row["abandoned_conversation"]),
            in_flight_conversation=int(row["in_flight_conversation"]),
        )

    def _decision_for_spend(
        self,
        *,
        estimated_cost: Decimal,
        override: dict[str, Any] | None,
        spend: LedgerSnapshot,
    ) -> CeilingDecision:
        """Three judges over one snapshot, in decreasing order of certainty.

        ADR-0034 split what used to be a single number, and ADR-0036 put one
        narrow denial back. The order below is the decision itself, not a
        formatting choice:

        1. **settled spend** -- the only judge whose denial is a statement about
           money actually charged. It carries `HARD_REASON_CODE`.
        2. **the unsettled count** -- says settlement stopped, which means judge
           1 is reading a total missing an unknown amount.
        3. **the estimate at `hard x K`** -- the concurrency guard (ADR-0036).
        4. **the estimate at `hard`, then `soft`** -- warnings only.

        The current call's own estimate is added to the *projected* totals and
        deliberately **not** to the settled ones. Adding it there would put the
        unproven number back into the breaker through the side door, which is
        the whole of what ADR-0034 forbids. The visible consequence is that the
        call which crosses a hard ceiling is allowed and the *next* one is
        refused -- ADR-0034 accepts that in as many words.
        """

        ceilings = resolve_ceilings(release=self._release, override=override)
        hard_conversation = self._release.ceilings_cny.get("hard_conversation")
        factor = self._estimated_ceiling_factor

        # -- 1. settled: the breaker -------------------------------------------
        if hard_conversation is not None and spend.settled_conversation > Decimal(
            str(hard_conversation)
        ):
            return CeilingDecision(
                effect="deny",
                reason_code=HARD_REASON_CODE,
                window="conversation",
                limit=Decimal(str(hard_conversation)),
                projected=spend.settled_conversation,
                enforcement_active=True,
            )
        for window, settled, hard in (
            ("daily", spend.settled_daily, ceilings.hard_daily),
            ("monthly", spend.settled_monthly, ceilings.hard_monthly),
        ):
            if settled > hard:
                return CeilingDecision(
                    effect="deny",
                    reason_code=HARD_REASON_CODE,
                    window=window,
                    limit=hard,
                    projected=settled,
                    enforcement_active=True,
                )

        # -- 2. the count gate: settlement itself has stopped -------------------
        # Counts **abandoned** rows only. Before T55 this counted every
        # `estimated` row, so a tenant with four calls in flight and a tenant
        # with four rows rotting since yesterday tripped it identically -- one
        # of them healthy, the other broken, and the gate could not tell.
        for window, count, count_limit in (
            ("conversation", spend.abandoned_conversation, self._unsettled_conversation_limit),
            ("daily", spend.abandoned_daily, self._unsettled_daily_limit),
        ):
            if count > count_limit:
                return CeilingDecision(
                    effect="deny",
                    reason_code=UNSETTLED_BACKLOG_REASON_CODE,
                    window=window,
                    limit=Decimal(count_limit),
                    projected=Decimal(count),
                    enforcement_active=True,
                    inactive_reason=(
                        f"{spend.in_flight_conversation} call(s) still in flight on this "
                        f"conversation; the {window} count above is calls that left and "
                        "never returned"
                    ),
                )

        forecast_daily = spend.projected_daily + estimated_cost
        forecast_monthly = spend.projected_monthly + estimated_cost
        forecast_conversation = spend.projected_conversation + estimated_cost

        # -- 3. the estimate at hard x K: the concurrency guard -----------------
        if hard_conversation is not None and forecast_conversation > (
            Decimal(str(hard_conversation)) * factor
        ):
            return CeilingDecision(
                effect="deny",
                reason_code=ESTIMATED_HARD_REASON_CODE,
                window="conversation",
                limit=Decimal(str(hard_conversation)) * factor,
                projected=forecast_conversation,
                enforcement_active=True,
            )
        for window, forecast, hard in (
            ("daily", forecast_daily, ceilings.hard_daily),
            ("monthly", forecast_monthly, ceilings.hard_monthly),
        ):
            if forecast > hard * factor:
                return CeilingDecision(
                    effect="deny",
                    reason_code=ESTIMATED_HARD_REASON_CODE,
                    window=window,
                    limit=hard * factor,
                    projected=forecast,
                    enforcement_active=True,
                )

        # -- 4. the estimate at hard, then soft: warnings only ------------------
        if hard_conversation is not None and forecast_conversation > Decimal(
            str(hard_conversation)
        ):
            return CeilingDecision(
                effect="warn",
                reason_code=ESTIMATED_REASON_CODE,
                window="conversation",
                limit=Decimal(str(hard_conversation)),
                projected=forecast_conversation,
                enforcement_active=True,
            )
        for window, forecast, hard in (
            ("daily", forecast_daily, ceilings.hard_daily),
            ("monthly", forecast_monthly, ceilings.hard_monthly),
        ):
            if forecast > hard:
                return CeilingDecision(
                    effect="warn",
                    reason_code=ESTIMATED_REASON_CODE,
                    window=window,
                    limit=hard,
                    projected=forecast,
                    enforcement_active=True,
                )
        # -- 5. settled spend at soft: the budget warning ---------------------
        # **Settled, not forecast** (T259, ADR-0124). See `SOFT_REASON_CODE`.
        # This judge reads the same numbers judge 1 does; the only difference is
        # the threshold it compares them against. That is what makes it the
        # early warning for the breaker rather than a second opinion about a
        # different quantity.
        for window, settled, soft in (
            ("daily", spend.settled_daily, ceilings.soft_daily),
            ("monthly", spend.settled_monthly, ceilings.soft_monthly),
        ):
            if settled > soft:
                return CeilingDecision(
                    effect="warn",
                    reason_code=SOFT_REASON_CODE,
                    window=window,
                    limit=soft,
                    projected=settled,
                    enforcement_active=True,
                )
        soft_conversation = self._release.ceilings_cny.get("soft_conversation")
        if soft_conversation is not None and spend.settled_conversation > Decimal(
            str(soft_conversation)
        ):
            return CeilingDecision(
                effect="warn",
                reason_code=SOFT_REASON_CODE,
                window="conversation",
                limit=Decimal(str(soft_conversation)),
                projected=spend.settled_conversation,
                enforcement_active=True,
            )
        return CeilingDecision(
            effect="allow",
            reason_code=None,
            window=None,
            limit=None,
            projected=None,
            enforcement_active=True,
        )

    @property
    def _estimated_ceiling_factor(self) -> Decimal:
        return Decimal(
            str(
                self._release.ceilings_cny.get(
                    "estimated_ceiling_factor", ESTIMATED_CEILING_FACTOR
                )
            )
        )

    @property
    def _unsettled_conversation_limit(self) -> int:
        return int(
            self._release.ceilings_cny.get(
                "max_unsettled_calls_conversation",
                derived_unsettled_conversation_limit(self._release),
            )
        )

    @property
    def _unsettled_daily_limit(self) -> int:
        return int(
            self._release.ceilings_cny.get("max_unsettled_calls_daily", _UNSETTLED_DAILY_LIMIT)
        )

    def reserve(
        self,
        *,
        binding: RoleBinding,
        context: CallContext,
        estimated_input: int,
        estimated_output: int,
        estimated_cost: Decimal | None,
        prompt_release: str,
        occurred_at: datetime,
    ) -> tuple[CeilingDecision, UUID | None]:
        """Atomically check a tenant ceiling and reserve this call's estimate.

        Calls for different conversations of one tenant share a transaction-scoped
        advisory lock. **The lock serialises the workers; it does not change what
        they see**, and since ADR-0034 the breaker reads settled spend -- which is
        zero for two calls that have not returned. So two workers *can* now cross
        `hard` together. What they cannot cross is `hard x K`: the estimate-based
        guard (ADR-0036) is what carries the concurrency property, one factor
        further out, and it is a guard rather than a cost judgement.

        This docstring previously claimed the unqualified version of that
        guarantee, and ADR-0036 quotes it as the thing being preserved -- so it
        was, for a while, the code asserting a property it had stopped providing
        (independent review 2026-09-03, P2-5).
        """

        active, inactive_reason = self.enforcement_status(binding)
        if not active or estimated_cost is None:
            decision = CeilingDecision(
                effect="allow",
                reason_code=None,
                window=None,
                limit=None,
                projected=None,
                enforcement_active=False,
                inactive_reason=inactive_reason or "call cost could not be estimated",
            )
            return decision, self.open_row(
                binding=binding,
                context=context,
                estimated_input=estimated_input,
                estimated_output=estimated_output,
                estimated_cost=estimated_cost,
                prompt_release=prompt_release,
                occurred_at=occurred_at,
            )

        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(context.tenant_id),),
            )
            override_row = connection.execute(
                """
                SELECT soft_daily_cny, hard_daily_cny, soft_monthly_cny, hard_monthly_cny
                  FROM tenant_cost_ceilings WHERE tenant_id = %s
                """,
                (context.tenant_id,),
            ).fetchone()
            spend = self._snapshot(connection, context=context, occurred_at=occurred_at)
            decision = self._decision_for_spend(
                estimated_cost=estimated_cost,
                override=dict(override_row) if override_row else None,
                spend=spend,
            )
            if decision.is_deny:
                # No audit row here on purpose. A denial propagates as
                # `CostCeilingExceeded`, and the pipeline's no-send path already
                # writes `audit_events` carrying `error.reason_code` -- now the
                # specific one, per the constructor above. Writing a second row
                # here would double every hard-ceiling count the Stage collector
                # reports, and that counter is wired to a real alert.
                return decision, None
            if decision.is_warn:
                self._insert_soft_breach(
                    connection=connection,
                    context=context,
                    decision=decision,
                    binding=binding,
                )
                # A warning never becomes a no-send -- the call proceeds -- so
                # this is the only place it can be made countable. Without it
                # ADR-0034's premise fails: the estimate stops breaking things
                # and nothing takes over saying so.
                self._record_ceiling_event(
                    connection=connection,
                    context=context,
                    decision=decision,
                    binding=binding,
                )
            row_id = uuid4()
            self._insert_row(
                connection=connection,
                row_id=row_id,
                binding=binding,
                context=context,
                estimated_input=estimated_input,
                estimated_output=estimated_output,
                estimated_cost=estimated_cost,
                prompt_release=prompt_release,
                occurred_at=occurred_at,
            )
            return decision, row_id

    def open_row(
        self,
        *,
        binding: RoleBinding,
        context: CallContext,
        estimated_input: int,
        estimated_output: int,
        estimated_cost: Decimal | None,
        prompt_release: str,
        occurred_at: datetime | None = None,
    ) -> UUID:
        row_id = uuid4()
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            self._insert_row(
                connection=connection,
                row_id=row_id,
                binding=binding,
                context=context,
                estimated_input=estimated_input,
                estimated_output=estimated_output,
                estimated_cost=estimated_cost,
                prompt_release=prompt_release,
                occurred_at=occurred_at or datetime.now(UTC),
            )
        return row_id

    def _insert_row(
        self,
        *,
        connection: Any,
        row_id: UUID,
        binding: RoleBinding,
        context: CallContext,
        estimated_input: int,
        estimated_output: int,
        estimated_cost: Decimal | None,
        prompt_release: str,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO model_invocation_costs(
                tenant_id, id, conversation_id, correlation_id, role, provider_id, model_id,
                release_version, release_content_hash, prompt_release, status,
                estimated_input_tokens, estimated_output_tokens, estimated_cost_cny,
                price_input_snapshot, price_output_snapshot, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'estimated',
                      %s, %s, %s, %s, %s, %s)
            """,
            (
                context.tenant_id,
                row_id,
                context.conversation_id,
                context.correlation_id,
                binding.role,
                binding.provider_id,
                binding.model_id,
                self._release.version,
                self._release.content_hash,
                prompt_release,
                estimated_input,
                estimated_output,
                estimated_cost,
                binding.price_input,
                binding.price_output,
                occurred_at,
            ),
        )

    def settle(
        self,
        *,
        row_id: UUID,
        context: CallContext,
        binding: RoleBinding,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        occurred_at: datetime | None = None,
    ) -> Decimal | None:
        """Write measured usage onto the reservation.

        Returns the cost, or `None` when the release cannot price the role.
        **Raises when the row is no longer `estimated`** -- see below.
        """

        period = binding.pricing_period(occurred_at or datetime.now(UTC))
        price_input, price_cached_input, price_output = binding.rate_snapshot(period)
        cost = binding.cost_cny(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            at=occurred_at or datetime.now(UTC),
        )
        value = None if cost is None else Decimal(str(cost))
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            cursor = connection.execute(
                """
                UPDATE model_invocation_costs
                   SET status = 'settled', input_tokens = %s, output_tokens = %s,
                       cached_input_tokens = %s, pricing_period = %s,
                       price_input_snapshot = %s, price_cached_input_snapshot = %s,
                       price_output_snapshot = %s,
                       cost_cny = %s, settled_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'estimated'
                """,
                (
                    input_tokens,
                    output_tokens,
                    cached_input_tokens,
                    period,
                    price_input,
                    price_cached_input,
                    price_output,
                    value,
                    context.tenant_id,
                    row_id,
                ),
            )
            # P3-2. The `status = 'estimated'` guard means a row already moved to
            # `unreconciled` matches nothing -- and before this check the method
            # still returned a neatly computed cost, so the caller believed it
            # had settled. Unreachable today (900s is far beyond the 8s call
            # deadline), but `unreconciled` is the first non-`estimated` state
            # that will actually occur in production, and one threshold change
            # makes it reachable. A settle that settled nothing is exactly the
            # kind of quiet success this repository keeps finding.
            if int(getattr(cursor, "rowcount", -1)) == 0:
                raise LedgerRowNotSettleable(
                    f"reservation {row_id} is no longer 'estimated'; measured usage was "
                    "not recorded"
                )
        return value

    def abandoned_reservations(self, *, tenant_id: UUID, now: datetime | None = None) -> int:
        """How many reservations are past the age at which they count as abandoned.

        Read-only counterpart of `reconcile_abandoned`, so an operator can see
        what a run would move before it moves anything.
        """

        moment = now or datetime.now(UTC)
        with self._database.transaction(tenant_id=tenant_id) as connection:
            row = connection.execute(
                """
                SELECT count(*) AS pending
                  FROM model_invocation_costs
                 WHERE tenant_id = %s
                   AND status = 'estimated'
                   AND occurred_at <= %s - %s * interval '1 second'
                """,
                (tenant_id, moment, RECONCILE_AFTER_SECONDS),
            ).fetchone()
        assert row is not None
        return int(row["pending"])

    def reconcile_abandoned(
        self, *, tenant_id: UUID, now: datetime | None = None
    ) -> int:
        """Age reservations that left and never came back into `unreconciled`.

        Returns how many rows moved. **It does not decide what they cost**, and
        that restraint is the whole design (T55, user decision b3): `failed`
        would assert the supplier did not bill us and `settled` would assert an
        amount, and both are things this system cannot know -- the ledger holds
        no supplier-side identifier, so there is nobody to ask.

        `unreconciled` says "we do not know", which is true, and keeps the
        amount out of settled spend where ADR-0034 forbids unproven numbers.

        Running this changes **no judgement**: the gates already treat an
        over-age `estimated` row as abandoned, and since the independent review
        (P1-2) an abandoned row's estimate stays in the projected sums whether or
        not it has been moved. A deployment where this never runs is therefore
        *conservative*, not blind. What it buys is a ledger that says what it
        means, and a `reconciled_at` that records when we stopped waiting.

        The boundary is `occurred_at <= now - RECONCILE_AFTER_SECONDS`: a row of
        **exactly** that age is already abandoned. Stated because an earlier
        version of this docstring said the opposite of the SQL beside it, and the
        test that was supposed to guard the boundary never touched either.
        """

        moment = now or datetime.now(UTC)
        with self._database.transaction(tenant_id=tenant_id) as connection:
            cursor = connection.execute(
                """
                UPDATE model_invocation_costs
                   SET status = 'unreconciled', reconciled_at = %s
                 WHERE tenant_id = %s
                   AND status = 'estimated'
                   AND occurred_at <= %s - %s * interval '1 second'
                """,
                (moment, tenant_id, moment, RECONCILE_AFTER_SECONDS),
            )
        rowcount = getattr(cursor, "rowcount", None)
        if rowcount is None:
            # Reporting "moved 0" for "the driver's API changed" would be a
            # silent zero, which reads exactly like a healthy run.
            raise RuntimeError(
                "database cursor exposes no rowcount; cannot report how many "
                "reservations were reconciled"
            )
        return int(rowcount)

    def mark_failed(self, *, row_id: UUID, context: CallContext) -> None:
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            connection.execute(
                """
                UPDATE model_invocation_costs
                   SET status = 'failed', settled_at = now()
                 WHERE tenant_id = %s AND id = %s AND status = 'estimated'
                """,
                (context.tenant_id, row_id),
            )

    def _record_ceiling_event(
        self,
        *,
        connection: Any,
        context: CallContext,
        decision: CeilingDecision,
        binding: RoleBinding,
    ) -> None:
        """Make a ceiling warning countable, which is what makes it an alert.

        ADR-0034 says the estimate may only warn, and that the warning "must
        actually go out" or the decision reduces to doing nothing. The Stage
        alert path is not application code: a root-owned collector
        (`scripts/stage_observability.py`) counts `audit_events` by
        `reason_code` every minute, pushes the counts as OTLP metrics, and a
        Grafana rule on the increase routes to `v2-sev-alerts`. So the way to
        raise an alert here is to write a row that query can see.

        `policy_decisions` -- where `_insert_soft_breach` writes -- is **not**
        that place; nothing reads it. Both rows are kept: the policy decision is
        the governance record, this one is the signal.
        """

        connection.execute(
            """
            INSERT INTO audit_events(
                tenant_id, id, actor_id, action, resource_type, resource_id,
                correlation_id, reason_code, evidence
            ) VALUES (%s, %s, NULL, 'model.cost_ceiling_warning',
                      'conversation_aggregate', %s, %s, %s, %s)
            """,
            (
                context.tenant_id,
                uuid4(),
                context.conversation_id,
                context.correlation_id,
                decision.reason_code,
                Jsonb(
                    {
                        "window": decision.window,
                        "limit_cny": str(decision.limit),
                        "projected_cny": str(decision.projected),
                        "role": binding.role,
                        "model_id": binding.model_id,
                        "release_version": self._release.version,
                        # Whoever is paged needs to know whether this number
                        # is a forecast or a charge -- that is the whole content
                        # of ADR-0034's distinction, and it has to survive into
                        # the record or the record re-creates the confusion.
                        #
                        # **Derived, not written down** (T259 / ADR-0124). It
                        # used to be the literal `"estimated"`, which was true
                        # of every warning until `SOFT_REASON_CODE` moved onto
                        # settled spend. A hard-coded basis next to a judge that
                        # can change which number it reads is a record that goes
                        # quietly wrong -- and this row is the one an alert is
                        # built on.
                        "basis": warning_basis(decision.reason_code),
                    }
                ),
            ),
        )

    def record_soft_breach(
        self, *, context: CallContext, decision: CeilingDecision, binding: RoleBinding
    ) -> UUID:
        """Persist a warning-level policy decision. The call still proceeds."""

        policy_id = uuid4()
        with self._database.transaction(tenant_id=context.tenant_id) as connection:
            self._insert_soft_breach(
                connection=connection,
                context=context,
                decision=decision,
                binding=binding,
                policy_id=policy_id,
            )
        return policy_id

    def _insert_soft_breach(
        self,
        *,
        connection: Any,
        context: CallContext,
        decision: CeilingDecision,
        binding: RoleBinding,
        policy_id: UUID | None = None,
    ) -> UUID:
        resolved_policy_id = policy_id or uuid4()
        # **这处判空是换成唯一写入点时 mypy 指出来的**，不是顺手加的洁癖：
        # `CeilingDecision.reason_code` 是 `str | None`（allow 档就是 None），
        # 而 `policy_decisions.reason_code` 是 `text NOT NULL` ——
        # 旧的那条裸 SQL 把它原样塞进去，真传 None 会在**数据库**炸，
        # 而炸的位置离写错的位置隔着两层调用。两个调用点今天都在 `is_warn` 分支后，
        # 所以这条只在 `record_soft_breach` 被将来的调用方误用时才会响。
        if decision.reason_code is None:
            raise ValueError(
                "warn 档的判定必须带理由码 —— 一条没有理由码的 policy_decisions 行不可被检索"
            )
        # **唯一写入点**（裁定二 / ADR-0137 §二）。此前这里自己手写一条 12 列
        # `INSERT`，与另外六处各写一遍。
        #
        # T35 的一半在这里：`"alert"` 不在 `policy-hook` 契约的义务枚举
        # （`audit / redact / require_evidence / create_manual_task`）里，
        # 而**本处是全仓唯一跳过 `validate_contract` 的写点**，于是它静悄悄写进去了，
        # 并且**没有任何读者** —— 「记下来了所以算做到了」的缩微版。
        # 那条 alert 现在走 `_record_ceiling_event`（那一头真有人读）。
        #
        # T35 的另一半原本记着「这个写点跳过校验、而这一列连 CHECK 都没有」。
        # `obligations` 今天**仍然没有 CHECK**，而封闭词表已经落在
        # `PolicyOutput.__post_init__` 里：同一个 `"alert"` 今天在**构造时**就抛。
        # 跳过 `validate_contract` 这件事本身没变，仍然开着。
        record_policy_decision(
            connection,
            tenant_id=context.tenant_id,
            row_id=resolved_policy_id,
            conversation_id=context.conversation_id,
            intent_id=None,
            output=PolicyOutput(
                hook="pre_inference",
                effect="allow",
                reason_code=decision.reason_code,
                policy_id="cost-governor",
                policy_version="1.0.0",
                obligations=audit_only(),
                context={
                    "window": decision.window,
                    "limit_cny": str(decision.limit),
                    "projected_cny": str(decision.projected),
                    "role": binding.role,
                    "model_id": binding.model_id,
                    "release_version": self._release.version,
                    "observed_at": datetime.now(UTC).isoformat(),
                },
            ),
        )
        return resolved_policy_id
