"""Contract tests for the RMB ledger and ceiling arithmetic.

The three failure modes under test are all forms of the same mistake: reporting
a number that was never measured. An unpriced call must not read as free, a
missing price must not read as "under ceiling", and a tenant override must not
be able to raise a platform limit.
"""

from __future__ import annotations

import copy
import json
import random
import string
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.cost import (
    ESTIMATED_REASON_CODE,
    HARD_REASON_CODE,
    MAX_MODEL_INPUT_BYTES,
    MODEL_INPUT_FRAMING_TOKENS,
    SOFT_REASON_CODE,
    CostGovernor,
    estimate_tokens,
    resolve_ceilings,
    warning_basis,
    worst_case_input_tokens,
)
from fastagent_v2.worker.model_policy import (
    MODEL_POLICY_ROOT,
    ModelPolicyRelease,
    active_release,
    canonical_hash,
    load_release,
)

MIGRATION = (
    Path(__file__).resolve().parents[2] / "infrastructure" / "migrations" / "002_m1a_cost.sql"
).read_text(encoding="utf-8")
MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "infrastructure" / "migrations"
INPUT_CONTRACT_MIGRATION = (
    MIGRATIONS_ROOT / "004_m1a_cost_input_contract.sql"
).read_text(encoding="utf-8")
INPUT_RESERVATION_MIGRATION = (
    MIGRATIONS_ROOT / "009_cost_input_reservation_tracks_request.sql"
).read_text(encoding="utf-8")


def priced_release(**overrides: Any) -> ModelPolicyRelease:
    """The active release with prices filled in, for arithmetic tests only."""

    document = copy.deepcopy(active_release().document)
    for binding in document["roles"].values():
        binding["price_cny_per_million_tokens"] = {"input": 1.0, "output": 2.0}
    document["ceilings_cny"].update(overrides)
    document["unverified_fields"] = []
    return ModelPolicyRelease(
        document=document, content_hash=canonical_hash(document), path=Path("in-memory")
    )


class FakeDatabase:
    """Stands in for the ledger. Returns whatever spend the test declares.

    `daily` / `monthly` / `conversation` are **settled** spend -- money the
    supplier has confirmed. The `projected_*` arguments default to the same
    values, i.e. a ledger with nothing in flight; give them explicitly to model
    unsettled estimates on top. Keeping the two families separate here is the
    point of ADR-0034: a test that cannot express "settled 0, estimated 500"
    cannot tell the two judges apart.
    """

    def __init__(
        self,
        *,
        daily: str = "0",
        monthly: str = "0",
        conversation: str = "0",
        projected_daily: str | None = None,
        projected_monthly: str | None = None,
        projected_conversation: str | None = None,
        abandoned_daily: int = 0,
        abandoned_conversation: int = 0,
        in_flight_conversation: int = 0,
        override: Any = None,
    ) -> None:
        self.daily = daily
        self.monthly = monthly
        self.conversation = conversation
        self.projected_daily = daily if projected_daily is None else projected_daily
        self.projected_monthly = monthly if projected_monthly is None else projected_monthly
        self.projected_conversation = (
            conversation if projected_conversation is None else projected_conversation
        )
        # T55 renamed these: the gate counts rows that left and never came back,
        # not every unsettled row. A tenant with calls in flight and a tenant
        # whose settlement path is broken used to be indistinguishable here.
        self.abandoned_daily = abandoned_daily
        self.abandoned_conversation = abandoned_conversation
        self.in_flight_conversation = in_flight_conversation
        self.override = override

    def transaction(self, *, tenant_id: Any) -> Any:  # noqa: ARG002
        return _FakeTransaction(self)


class _FakeTransaction:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def __enter__(self) -> _FakeConnection:
        return _FakeConnection(self._database)

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self._database = database

    def execute(self, statement: str, _params: Any = None) -> _FakeConnection:
        self._statement = statement
        return self

    def fetchone(self) -> Any:
        database = self._database
        if "tenant_cost_ceilings" in self._statement:
            return database.override
        if "AS settled_daily" in self._statement:
            return {
                "settled_daily": database.daily,
                "settled_monthly": database.monthly,
                "settled_conversation": database.conversation,
                "projected_daily": database.projected_daily,
                "projected_monthly": database.projected_monthly,
                "projected_conversation": database.projected_conversation,
                "abandoned_daily": database.abandoned_daily,
                "abandoned_conversation": database.abandoned_conversation,
                "in_flight_conversation": database.in_flight_conversation,
            }
        if "AS cost" in self._statement:
            return {"cost": database.conversation}
        return {"daily": database.daily, "monthly": database.monthly}


def governor(database: FakeDatabase, release: ModelPolicyRelease | None = None) -> CostGovernor:
    return CostGovernor(database=database, release=release or priced_release())  # type: ignore[arg-type]


def context() -> Any:
    from uuid import UUID

    from fastagent_v2.worker.cost import CallContext

    return CallContext(
        tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        conversation_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        correlation_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    )


def test_the_estimate_convention_is_documented_in_the_migration() -> None:
    """The column comment has to describe the reservation the code makes today.

    Independent review 2026-09-03, P2-3: this used to grep two substrings of
    migration 004, both of which survived T43 inverting the behaviour the
    sentence described -- 004 says the ledger reserves "for the entire contract
    limit", and it now reserves the request's own size. A test that cannot see a
    semantic inversion is not guarding the comment, it is guarding two words.

    So it now asserts on migration 009, which supersedes 004's comment, and it
    asserts the distinguishing clause rather than the shared vocabulary.
    """

    assert "one token per serialized UTF-8 byte" in INPUT_CONTRACT_MIGRATION
    assert "hard input byte limit" in INPUT_CONTRACT_MIGRATION
    # The clause that separates the two conventions, not the one they share.
    assert "of the request itself" in INPUT_RESERVATION_MIGRATION
    assert "entire contract limit" not in INPUT_RESERVATION_MIGRATION.split(
        "It previously reserved"
    )[0], "the live half of the comment must not still describe the old reservation"


def test_the_migration_states_why_cost_is_null_rather_than_zero() -> None:
    assert "storing 0 would make an unmeasured call look free" in MIGRATION


def test_token_estimate_is_a_unicode_byte_upper_bound() -> None:
    assert estimate_tokens("") == 0
    samples = ["你好世界", "abcdef", "👩🏽‍💻🧪", "e\u0301", "\x00\ufffd"]
    rng = random.Random(20260902)  # noqa: S311 - deterministic regression corpus
    samples.append("".join(rng.choice(string.printable + "你好🚀") for _ in range(4096)))
    for sample in samples:
        assert estimate_tokens(sample) == len(sample.encode("utf-8"))


def test_the_reservation_tracks_the_request_instead_of_the_contract_maximum() -> None:
    """T43. This function used to return a constant.

    Reserving `MAX_MODEL_INPUT_BYTES + MODEL_INPUT_FRAMING_TOKENS` for every
    call, however small, is the single largest term in the 25x over-estimate
    measured on 2026-09-03: 17408 tokens reserved where 1483 were consumed.
    Tracking the request keeps the bound provable -- a token covers at least one
    UTF-8 byte -- while making the number mean something.
    """

    assert worst_case_input_tokens("tiny") == len(b"tiny") + MODEL_INPUT_FRAMING_TOKENS
    assert worst_case_input_tokens("你好") == len("你好".encode()) + MODEL_INPUT_FRAMING_TOKENS
    # Still an upper bound at the contract limit, and still not one token beyond.
    at_limit = "x" * MAX_MODEL_INPUT_BYTES
    assert worst_case_input_tokens(at_limit) == MAX_MODEL_INPUT_BYTES + MODEL_INPUT_FRAMING_TOKENS
    with pytest.raises(ValueError, match="contract limit"):
        worst_case_input_tokens("x" * (MAX_MODEL_INPUT_BYTES + 1))


def test_estimate_uses_the_release_output_bound_not_a_guess() -> None:
    release = priced_release()
    binding = release.role("PLAN_CAPABILITY")
    _, output_tokens, cost = governor(FakeDatabase(), release).estimate(
        binding=binding, system_prompt="s", user_prompt="u"
    )
    assert output_tokens == binding.max_output_tokens
    assert cost is not None


def test_enforcement_is_inactive_while_the_release_has_no_price() -> None:
    live = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-3.json")
    assert not live.is_verified
    active, reason = governor(FakeDatabase(), live).enforcement_status(live.role("GENERATE_VALUE"))
    assert active is False
    assert reason is not None and "price" in reason


def test_an_unpriceable_call_is_allowed_but_reports_enforcement_inactive() -> None:
    live = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-3.json")
    decision = governor(FakeDatabase(), live).precheck(
        binding=live.role("GENERATE_VALUE"), context=context(), estimated_cost=None
    )
    assert decision.effect == "allow"
    assert decision.enforcement_active is False
    assert decision.inactive_reason is not None
    # Crucially it does not claim the call was under any ceiling.
    assert decision.limit is None and decision.projected is None


def test_spend_under_both_ceilings_is_allowed_with_enforcement_active() -> None:
    decision = governor(FakeDatabase(daily="0", monthly="0")).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "allow"
    assert decision.enforcement_active is True


def test_crossing_the_soft_ceiling_warns_but_still_allows_the_call() -> None:
    """**Settled** spend over soft. T259 / ADR-0124 changed which number this is.

    It used to be the forecast: this case declared settled 36.395 against a soft
    of 36.4 and relied on the call's own estimate to push it over. That is now
    `test_the_estimate_alone_no_longer_raises_the_budget_warning`.
    """

    decision = governor(FakeDatabase(daily="36.5", monthly="36.5")).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "warn"
    assert decision.reason_code == SOFT_REASON_CODE
    assert decision.window == "daily"


def test_the_estimate_alone_no_longer_raises_the_budget_warning() -> None:
    """**The ruling, stated as the case that used to be red** (T259, ADR-0124).

    Settled 36.395 against a soft of 36.4, with an estimate that takes the
    forecast past it. Until 2026-09-10 this warned; it is `allow` now, because
    the forecast is built to be about 9x high on purpose and a budget warning
    sourced from it fires on every conversation past its first turn.

    The forward-looking warning is not gone -- it is `ESTIMATED_REASON_CODE`,
    which still fires at `hard`. This case is about which number `soft` reads.
    """

    decision = governor(
        FakeDatabase(daily="36.395", monthly="36.395"),
    ).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "allow", "a forecast may no longer raise the budget warning"


def test_a_real_conversation_no_longer_warns_on_every_turn() -> None:
    """**The eleven warnings the third real window produced** (T259).

    Measured: about 0.012 CNY of settled spend per turn, and a single call's
    forecast is 0.07-0.08 because the estimate reserves the input by request
    bytes, the output at `max_output_tokens`, and prices both at the peak
    uncached rate. Against `soft_conversation` = 0.11 the forecast crossed on
    the second call of every conversation.

    Eight turns in, settled is 0.096 -- **under** the ceiling, which is the true
    answer -- while the forecast is 0.166. This case is the difference.
    """

    release = priced_release(hard_conversation=0.27, soft_conversation=0.11)
    decision = governor(
        FakeDatabase(conversation="0.096", projected_conversation="0.096"), release
    ).precheck(
        binding=release.role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.07"),
    )
    assert decision.effect == "allow"


def test_the_conversation_budget_warning_fires_on_settled_spend() -> None:
    """And it still fires -- on money actually charged.

    0.12 settled against a soft of 0.11, and well under the hard 0.27, so the
    answer is a warning and not a refusal.
    """

    release = priced_release(hard_conversation=0.27, soft_conversation=0.11)
    decision = governor(
        FakeDatabase(conversation="0.12", projected_conversation="0.12"), release
    ).precheck(
        binding=release.role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.001"),
    )
    assert decision.effect == "warn"
    assert decision.reason_code == SOFT_REASON_CODE
    assert decision.window == "conversation"


def test_the_audit_row_says_which_number_the_warning_judged() -> None:
    """**The record has to move with the judge** (T259, ADR-0124).

    `basis` used to be the literal `"estimated"`, written next to a judge that
    has since changed which number it reads. This row is what the Stage
    collector counts and what a person reads at 3am; a plausible-looking wrong
    word in it is worse than no word.
    """

    assert warning_basis(SOFT_REASON_CODE) == "settled"
    assert warning_basis(ESTIMATED_REASON_CODE) == "estimated"

    # And an unregistered code is a loud failure, not a default. A warning whose
    # basis nobody declared is exactly the case that would otherwise write a
    # confident lie.
    with pytest.raises(ValueError):
        warning_basis("cost_some_future_ceiling")
    with pytest.raises(ValueError):
        warning_basis(None)


def test_settled_spend_over_the_hard_ceiling_denies_the_call() -> None:
    """ADR-0034 judge 1: the only refusal that is a claim about money charged."""

    decision = governor(FakeDatabase(daily="91.5", monthly="91.5")).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "deny"
    assert decision.reason_code == HARD_REASON_CODE
    assert decision.window == "daily"
    # The refusal reports settled spend, not settled spend plus this call's
    # estimate. Adding the estimate here is exactly what ADR-0034 forbids.
    assert decision.projected == Decimal("91.5")


def test_the_estimate_crossing_a_hard_ceiling_warns_rather_than_denying() -> None:
    """ADR-0034's central change, stated as the case that used to deny.

    Settled spend is under the ceiling; only the forward-looking total crosses
    it. Before ADR-0034 this denied -- and with the estimate running about 25x
    conservative, it denied roughly 25x too early, presenting as "ceiling
    reached" while the real spend was nowhere near.
    """

    decision = governor(FakeDatabase(daily="90.995", monthly="90.995")).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "warn"
    assert decision.reason_code == ESTIMATED_REASON_CODE
    assert decision.window == "daily"


def test_the_monthly_ceiling_can_deny_while_the_daily_one_is_satisfied() -> None:
    decision = governor(FakeDatabase(daily="1", monthly="2730.5")).precheck(
        binding=priced_release().role("GENERATE_VALUE"),
        context=context(),
        estimated_cost=Decimal("0.01"),
    )
    assert decision.effect == "deny"
    assert decision.reason_code == HARD_REASON_CODE
    assert decision.window == "monthly"


def test_a_tenant_override_may_tighten_a_ceiling() -> None:
    ceilings = resolve_ceilings(
        release=priced_release(), override={"hard_daily_cny": Decimal("10")}
    )
    assert ceilings.hard_daily == Decimal("10")
    assert ceilings.tenant_override_applied is True
    assert ceilings.clamped_fields == ()


def test_a_tenant_override_may_not_raise_a_platform_ceiling() -> None:
    ceilings = resolve_ceilings(
        release=priced_release(), override={"hard_daily_cny": Decimal("9999")}
    )
    assert ceilings.hard_daily == Decimal("91"), "the platform value must win"
    assert "hard_daily" in ceilings.clamped_fields


def test_an_absent_override_field_inherits_the_release_default() -> None:
    ceilings = resolve_ceilings(
        release=priced_release(), override={"soft_daily_cny": None, "hard_daily_cny": Decimal("5")}
    )
    assert ceilings.soft_daily == Decimal("36.4")
    assert ceilings.hard_daily == Decimal("5")


def test_the_rollup_views_expose_unpriced_calls_separately() -> None:
    """A total that silently omits unpriced calls would understate spend."""

    assert "unpriced_invocations" in MIGRATION
    assert MIGRATION.count("count(*) FILTER (WHERE cost_cny IS NULL)") == 2
    assert "security_invoker = true" in MIGRATION


def test_the_ledger_tables_carry_row_level_security() -> None:
    for table in ("tenant_cost_ceilings", "model_invocation_costs"):
        assert f"'{table}'" in MIGRATION
    assert "FORCE ROW LEVEL SECURITY" in MIGRATION


@pytest.mark.parametrize("statement", ["BYPASSRLS", "row_security = off"])
def test_the_migration_contains_no_rls_escape(statement: str) -> None:
    assert statement not in MIGRATION


def test_release_json_and_ceiling_defaults_agree() -> None:
    live = active_release()
    ceilings = live.ceilings_cny
    assert json.dumps(ceilings, ensure_ascii=False)
    assert ceilings["soft_daily"] == 36.4
    assert ceilings["hard_daily"] == 91
    assert ceilings["soft_monthly"] == 1092
    assert ceilings["hard_monthly"] == 2730
    # **2026-09-17 抬到 1 元**（用户裁定）。复核当天在 Stage 上量到一通**正常**
    # 对话预计 0.274191 元，而硬顶是 0.27 —— 超 1.5% 即拒 `UNDERSTAND_VALUE`，
    # 于是自动交接给人、跟进定时器到期时按设计沉默。链上每一步都对，**错的是这个数**。
    #
    # 用户的判据：**上限是防止出问题的，不是用来影响正常对话的。**
    # 一个会在正常会话上撞线的上限不是防炸的闸，它就是那个炸点。
    #
    # soft 跟着抬（0.11 → 0.4，沿原比例）：soft 只出现在 `cost.py` 的
    # 「warnings only」两段，留在 0.11 的后果是**每通正常对话都告警一次**，
    # 而一个永远在响的告警等于没有告警。
    # **2026-09-17 再抬一次：0.4 → 0.6（硬顶的 60%）。**
    # 0.4 对实测正常会话 0.274191 只有 1.46 倍余量 —— 长一点的会话
    # 照样告警，而一个总在响的告警等于没有告警。
    assert ceilings["soft_conversation"] == 0.6
    assert ceilings["hard_conversation"] == 1.0
    assert ceilings["enforcement_active"] is True
    assert "Stage" in ceilings["provenance"]


def test_time_of_use_price_uses_call_instant_and_measured_cache_tokens() -> None:
    draft = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-4.json")
    binding = draft.role("GENERATE_VALUE")
    peak = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # Monday 10:00 Asia/Shanghai
    offpeak = datetime(2026, 9, 6, 2, 0, tzinfo=UTC)  # Sunday
    assert binding.cost_cny(
        input_tokens=1_000_000,
        cached_input_tokens=250_000,
        output_tokens=500_000,
        at=peak,
    ) == pytest.approx(6.775)
    assert binding.cost_cny(
        input_tokens=1_000_000,
        cached_input_tokens=250_000,
        output_tokens=500_000,
        at=offpeak,
    ) == pytest.approx(3.3875)


def test_ceiling_can_be_kept_inactive_until_user_confirmation() -> None:
    document = copy.deepcopy(active_release().document)
    document["ceilings_cny"]["enforcement_active"] = False
    draft = ModelPolicyRelease(
        document=document, content_hash=canonical_hash(document), path=Path("in-memory")
    )
    decision = governor(FakeDatabase(), draft).precheck(
        binding=draft.role("GENERATE_VALUE"), context=context(), estimated_cost=Decimal("1")
    )
    assert decision.enforcement_active is False
    assert "confirmation" in str(decision.inactive_reason)


def test_conversation_hard_ceiling_is_checked_before_tenant_windows() -> None:
    release = priced_release(hard_conversation=0.27, soft_conversation=0.11)
    decision = governor(FakeDatabase(conversation="0.30"), release).precheck(
        binding=release.role("GENERATE_VALUE"), context=context(), estimated_cost=Decimal("0.02")
    )
    assert decision.effect == "deny"
    assert decision.reason_code == HARD_REASON_CODE
    assert decision.window == "conversation"


def test_the_conversation_window_warns_on_the_estimate_like_the_others() -> None:
    """The ordering above must not have been bought by exempting this window.

    Settled 0.26 against a 0.27 conversation ceiling: under it on proven spend,
    over it once this call's estimate is added. Same answer as the tenant
    windows -- warn, do not deny.
    """

    release = priced_release(hard_conversation=0.27, soft_conversation=0.11)
    decision = governor(FakeDatabase(conversation="0.26"), release).precheck(
        binding=release.role("GENERATE_VALUE"), context=context(), estimated_cost=Decimal("0.02")
    )
    assert decision.effect == "warn"
    assert decision.reason_code == ESTIMATED_REASON_CODE
    assert decision.window == "conversation"
