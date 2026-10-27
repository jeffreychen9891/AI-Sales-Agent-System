"""Quiet hours refuse, and say when it lifts. **B4-5 / RFC §6.2 / ADR-0097 / ADR-0119.**

Before this batch `quiet_hours` had **zero production consumers** -- ADR-0117
measured it, which is why that ADR re-scoped B4-5 from 「改成按 tenant timezone」
to 「从零建这个机制」: there was no mechanism whose timezone could be wrong.

Two things asserted here are easy to get subtly wrong and impossible to notice
in production:

* **the window crosses midnight**, which is the shape both shipped profiles use
  (22:30-08:00), so wrap-around is the main path rather than an edge case;
* **DST**, where RFC §6.2 asks for 「明确的『首次合法本地时刻』规则」. Neither
  shipped profile can reach it -- `Asia/Shanghai` has no DST -- so the cases
  below bring `America/New_York` deliberately. **A criterion nothing shipped can
  exercise is the trap this repository keeps paying for** (T233's shape), and
  the answer is to bring data that reaches it rather than to leave the rule
  asserted only in prose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastagent_v2.worker.proactivity import (
    QUIET_HOURS_REASON,
    ProactivityPolicyError,
    ProactivityVerdict,
    evaluate,
    knobs,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


def _profile(
    timezone: str = "Asia/Shanghai", ranges: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    return {
        "proactivity_policy": {
            "active_window_seconds": 1800,
            "reengagement_limits": {"window_seconds": 86400, "max_delivered": 1},
            "exit_signals": ["explicit_opt_out", "complaint", "human_takeover"],
            "quiet_hours": {
                "timezone": timezone,
                "ranges": ranges or [{"start": "22:30", "end": "08:00"}],
            },
        }
    }


def _aggregate(moment: datetime, *, quiet_for_seconds: int = 86_400) -> dict[str, Any]:
    """⚠️ **默认是「客户很久没说话」，也就是一次唤醒。**

    2026-09-21 之前这里写的是 `last_inbound_at = moment` —— 客户**此刻**刚说过话，
    那是一次**会话内延续**。而静默时段那道闸当时不分 classification，
    所以用哪种夹具都 deny，**这份文件一直在用延续的夹具钉唤醒的规则**。

    用户同日裁定：「**静默时间应该只限制唤醒对话，而不是正常销售推进** ——
    很有可能客户就是晚上进来的」。闸收到 `re_engagement` 上之后，
    那个夹具会 allow —— 所以这里改成真正的唤醒，
    **而「延续不被挡」由 `test_a_continuation_at_night_is_not_stopped` 单独钉**。
    """

    stamp = (moment - timedelta(seconds=quiet_for_seconds)).isoformat().replace("+00:00", "Z")
    return {"control_mode": "ai", "last_inbound_at": stamp, "target_stack": []}


def _verdict(
    profile: dict[str, Any], now: datetime, pending: dict[str, Any] | None = None
) -> ProactivityVerdict:
    return evaluate(
        knobs=knobs(profile),
        aggregate=_aggregate(now),
        event_kind="goal_stall",
        now=now,
        understanding={"interruption": {"relation": "same_goal"}, "risk_markers": ["none"]},
        pending_exit_signal=pending,
    )


def test_the_quiet_window_crosses_midnight_and_says_when_it_lifts() -> None:
    """23:00 Shanghai is inside 22:30-08:00, and the answer is 08:00 **tomorrow**.

    The wrap is the point: a naive `start <= wall < end` reads 22:30-08:00 as an
    empty range and would allow every hour of the night. That version passes any
    case whose instant is in the afternoon -- which is what every other
    proactivity case in this suite uses -- so this is the only place the
    wrap-around is actually exercised.
    """

    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)  # 23:00 Shanghai
    verdict = _verdict(_profile(), now)

    assert verdict.effect == "deny"
    assert verdict.reason_code == QUIET_HOURS_REASON
    assert verdict.next_allowed_at == datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    assert verdict.next_allowed_at.astimezone(SHANGHAI).hour == 8
    # A refusal queues nothing, so there is nothing for the dispatcher to expire.
    assert verdict.window_expires_at is None



def test_a_continuation_at_night_is_not_stopped() -> None:
    """⚠️ **静默时段只挡唤醒，不挡把话说完**（用户 2026-09-21 裁定）。

    原话：「静默时间应该只限制**唤醒对话**，而不是正常销售推进 ——
    **很有可能客户就是晚上进来的**」。

    两者的区别是**谁先开的口**：再触达是我们去敲一个已经安静下来的人的门，
    会话内延续是把他正在进行的那句话说完。

    ⚠️ **这一条是那次改动的另一半**：上面那几条把夹具换成了真正的唤醒，
    没有这一条，把闸改回「不分 classification」也不会有任何东西变红。
    """

    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)  # 23:00 Shanghai，静默时段内
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(now, quiet_for_seconds=30),  # 他半分钟前刚说过话
        event_kind="goal_stall",
        now=now,
        understanding=None,
        pending_exit_signal=None,
    )

    assert verdict.classification == "in_session_continuation"
    assert verdict.effect == "allow", (
        f"深夜的会话内延续被挡了（{verdict.reason_code}）—— "
        "而那一轮是客户自己开的头"
    )

def test_the_afternoon_is_not_quiet_and_carries_no_next_allowed_at() -> None:
    """The negative half, without which "always deny" would pass the case above.

    **The second assertion is the one ADR-0119 needs**: an allow carries
    `next_allowed_at is None`, so the column is not a constant in the other
    direction either.
    """

    now = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)  # 14:00 Shanghai
    verdict = _verdict(_profile(), now)

    assert verdict.effect == "allow"
    assert verdict.next_allowed_at is None


def test_the_boundary_is_half_open_so_the_instant_it_names_is_itself_allowed() -> None:
    """`next_allowed_at` must name an instant that is actually allowed.

    A closed interval would leave 08:00 still quiet, so the value returned would
    name a moment that is itself refused, and a dispatcher re-checking at exactly
    that instant would refuse again. Asserted by feeding the answer back in --
    the only way to catch an off-by-one that both halves of the comparison share.
    """

    quiet = knobs(_profile()).quiet_hours
    lifts = quiet.lifts_at(datetime(2026, 9, 9, 15, 0, tzinfo=UTC))

    assert not quiet.covers(lifts)
    assert quiet.covers(lifts - timedelta(minutes=1))


def test_a_missing_local_hour_resolves_to_the_first_instant_that_exists() -> None:
    """Spring forward: the wall time the profile names never happens that day.

    `America/New_York` jumps 02:00 -> 03:00 on 2026-03-08, so a range ending at
    **02:30** ends at a wall clock that does not exist. RFC §6.2 asks for 「首次
    合法本地时刻」, which is 03:00 local -- the instant the gap closes.

    **The arithmetic answer is wrong, and this pins the difference.**
    Constructing `datetime(2026, 3, 8, 2, 30, tzinfo=NEW_YORK)` and normalising
    gives **03:30** local, an hour late. Both are "after the gap", so a case that
    only asserted "is allowed" would accept either.
    """

    profile = _profile("America/New_York", [{"start": "22:00", "end": "02:30"}])
    now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # 01:00 EST, still quiet
    verdict = _verdict(profile, now)

    assert verdict.reason_code == QUIET_HOURS_REASON
    assert verdict.next_allowed_at == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert verdict.next_allowed_at.astimezone(NEW_YORK).strftime("%H:%M") == "03:00"
    arithmetic = datetime(2026, 3, 8, 2, 30, tzinfo=NEW_YORK).astimezone(UTC)
    assert verdict.next_allowed_at < arithmetic, "the arithmetic answer is an hour late"


def test_a_repeated_local_hour_resolves_to_the_first_occurrence() -> None:
    """Fall back: the wall time the profile names happens twice.

    `America/New_York` repeats 01:00-02:00 on 2026-11-01, so a range ending at
    **01:30** ends at a wall clock with two instants. 「首次合法本地时刻」 is the
    **first** of them -- 01:30 EDT, an hour before 01:30 EST.

    Choosing the second would hold every message an extra hour with nothing
    reporting it, which is this whole area's failure mode: silently wrong, and
    only visible twice a year.
    """

    profile = _profile("America/New_York", [{"start": "22:00", "end": "01:30"}])
    now = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # 00:00 EDT, still quiet
    verdict = _verdict(profile, now)

    assert verdict.reason_code == QUIET_HOURS_REASON
    assert verdict.next_allowed_at == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    local = verdict.next_allowed_at.astimezone(NEW_YORK)
    offset = local.utcoffset()
    assert local.strftime("%H:%M") == "01:30"
    assert offset is not None and offset.total_seconds() == -4 * 3600, (
        "the first occurrence of 01:30 is still EDT"
    )


def test_an_unknown_quiet_timezone_is_refused_rather_than_defaulted() -> None:
    """RFC §6.4: 「timezone 未知不主动发送」, executed as a raise.

    **Defaulting to UTC is the failure this prevents**, and it is worth naming:
    a Shanghai merchant's 22:30-08:00 read in UTC allows sending at 06:30 local
    and refuses at 14:30, with nothing anywhere reporting a problem. The raise
    becomes `PROACTIVITY_POLICY_UNREADABLE` and a persisted no-send, so the
    failure direction is 「不发」 -- the direction that sentence asks for.

    **The three inputs are not decoration.** A zone name is a filesystem key, so
    `ZoneInfo` reports a bad one in whatever way the lookup failed, and the
    families are genuinely different -- measured, not assumed:
    `"Mars/Olympus_Mons"` raises `ZoneInfoNotFoundError` (a `KeyError`), `""`
    raises `ValueError`, and `"   "` raises **`PermissionError`**, which is an
    `OSError` and not a `KeyError`.

    The whitespace case is the one that found a real defect: the first version
    caught `(ZoneInfoNotFoundError, ValueError)`, so a whitespace zone escaped
    as `PermissionError` **out of the accounting transaction**, where nothing
    converts it into a no-send. It would have taken the turn down instead of
    refusing to send.
    """

    for broken in ("Mars/Olympus_Mons", "", "   ", "A" * 300):
        with pytest.raises(ProactivityPolicyError):
            knobs(_profile(broken))


def test_a_window_that_never_lifts_fails_closed_instead_of_looping() -> None:
    """A profile quiet around the clock has no answer, and says so.

    Two ranges that between them cover every hour leave `lifts_at` nothing to
    return. **It raises rather than returning a far-future placeholder**: a
    placeholder would land in `policy_decisions.next_allowed_at` and be read as
    「几点可以发」, which would be a lie about a profile that can never send
    proactively at all.

    `load_package` cannot refuse this today (**T237**), so the engine is the only
    place it can be caught.
    """

    profile = _profile(
        ranges=[{"start": "00:00", "end": "12:00"}, {"start": "12:00", "end": "00:00"}]
    )
    with pytest.raises(ProactivityPolicyError, match="whole day"):
        _verdict(profile, datetime(2026, 9, 9, 6, 0, tzinfo=UTC))


def test_a_range_that_starts_where_it_ends_is_refused_at_parse_time() -> None:
    """`22:00-22:00` is not readable, so it is not read.

    It could mean "never" or "always", and those are opposites. Guessing either
    would be the implementation deciding a product question, so `knobs()`
    refuses -- the same fail-closed direction as the unknown zone.
    """

    with pytest.raises(ProactivityPolicyError, match="starts where it ends"):
        knobs(_profile(ranges=[{"start": "22:00", "end": "22:00"}]))


def test_an_exit_signal_outranks_quiet_hours_and_carries_no_expiry() -> None:
    """Order of finality, and the `NULL` half of ADR-0119 §二.1.

    Both refuse, so the assertion is on **which reason code** and on
    `next_allowed_at`. 「别再发了」 has no instant that un-says it, and reporting a
    quiet-hours refusal instead would tell a reader the message goes out at
    08:00 -- when it must never go out at all.
    """

    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)  # inside quiet hours
    verdict = _verdict(
        _profile(),
        now,
        pending={
            "event_id": "11111111-1111-4111-8111-111111111111",
            "relation": "exit",
            "recorded_at": "2026-09-09T03:00:00Z",
        },
    )

    assert verdict.reason_code != QUIET_HOURS_REASON
    assert verdict.next_allowed_at is None, "an opt-out has no instant that un-says it"


def test_an_overdue_silence_check_outranks_quiet_hours_and_carries_no_expiry() -> None:
    """The third pair in the order of finality, and it is not about tidiness.

    **The two refusals write opposite values into the column this batch
    delivers**: an overdue `silence_check` may never send, so its
    `next_allowed_at` is `None`; a quiet-hours refusal always names an instant.
    Which one wins therefore decides what a reader is told -- 「这条永远不发」 or
    「08:00 再发」 -- and today that is decided by the order of two branches with
    nothing holding them there (independent review 2026-09-09, P2-1).

    The `silence_check` wins because it is the more final: RFC §6.4 refuses to
    let an overdue one become a re-engagement at all, so waiting for the quiet
    window to lift would not make it sendable.
    """

    # 23:00 Shanghai, i.e. inside the quiet window, and an inbound old enough
    # that the silence_check is being consumed outside its active window.
    now = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate={
            "control_mode": "ai",
            "last_inbound_at": "2026-09-08T04:00:00Z",
            "target_stack": [],
        },
        event_kind="silence_check",
        now=now,
        understanding={"interruption": {"relation": "same_goal"}, "risk_markers": ["none"]},
        pending_exit_signal=None,
    )

    assert verdict.reason_code == "PROACTIVITY_SILENCE_CHECK_OUTSIDE_WINDOW"
    assert verdict.next_allowed_at is None, (
        "an event that may never send was told when it may send again"
    )


def test_a_row_queued_before_a_quiet_window_carries_when_that_window_lifts() -> None:
    """The value the dispatcher will need, computed while the profile is still in hand.

    A message allowed at 22:00 and dispatched at 22:40 is expired by the quiet
    window opening -- and **that kind of expiry has an end**, unlike an active
    window closing, past which the message is a different class rather than a
    delayed one. The dispatcher may not read a profile to work that out
    (ADR-0098 §一: 「到点作废是一次纯本地判断」), so the answer travels with the row.

    Asserted here rather than only end-to-end because it is the arithmetic:
    `window_lifts_at` must be the **end of the window that expires the row**,
    not the end of whatever window `now` happens to be in.
    """

    # A **re-engagement** in the afternoon: the customer last wrote a day ago,
    # so there is no active window and the quiet period is the only source --
    # which is exactly the class that had no deadline at all before B4-5.
    afternoon = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)  # 14:00 Shanghai
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate={
            "control_mode": "ai",
            "last_inbound_at": "2026-09-08T06:00:00Z",
            "target_stack": [],
        },
        event_kind="goal_stall",
        now=afternoon,
        understanding={"interruption": {"relation": "same_goal"}, "risk_markers": ["none"]},
        pending_exit_signal=None,
    )

    assert verdict.effect == "allow"
    assert verdict.window_expires_at == datetime(2026, 9, 9, 14, 30, tzinfo=UTC), "22:30 local"
    assert verdict.window_lifts_at == datetime(2026, 9, 10, 0, 0, tzinfo=UTC), (
        "08:00 local the next day -- the end of the window that expires the row"
    )


def test_an_active_window_closing_first_names_no_instant() -> None:
    """The other half, and the reason `window_lifts_at` is not simply 「静默窗口结束」.

    When the active window closes before any quiet period opens, the row is
    expired for a reason waiting does not fix: past that instant the same
    message is a **re-engagement**, carrying a quota that was never applied to
    it. So there is no instant to name, and a value here would be a wrong
    answer rather than a missing one.
    """

    # ⚠️ **这一条要的是会话内延续** —— 客户刚写过，所以显式传 30 秒，
    # 不用 `_aggregate` 那个「很久没说话」的默认值（2026-09-21 改的）。
    #
    # ⚠️ **120 是写死的，而那是第二版** —— 第一版从配包现读，
    # 想的是「不写字面量就不会过期」。**反向探针 B 支当场证明那是空的**：
    # 把配包改回 1800，夹具跟着变成 1800，**这一条一声不吭**。
    # 本仓「拿常量核常量」那一族。
    #
    # 写死之后它会在改配包那一刻红，而报错文案指着要做的那件事：
    # 用户 2026-09-21 裁的是「会话内延续在 2 分钟内才有意义」。
    # ⚠️ `_profile()` 是**这份文件自造的夹具**，不是在架配包 ——
    # 第一版我把自核写成了 `knobs(_profile())`，问错了对象。
    # **在架那份由本文件末尾那条单独钉**。这里只要夹具自己的窗口。
    noon = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
    window = knobs(_profile()).active_window_seconds
    verdict = evaluate(
        knobs=knobs(_profile()),
        aggregate=_aggregate(noon, quiet_for_seconds=30),
        event_kind="goal_stall",
        now=noon,
        understanding=None,
        pending_exit_signal=None,
    )

    assert verdict.window_expires_at == noon - timedelta(seconds=30) + timedelta(
        seconds=window
    )
    assert verdict.window_lifts_at is None



def test_the_shipped_window_is_the_two_minutes_the_user_ruled() -> None:
    """⚠️ **在架配包的活跃窗口是 2 分钟**（用户 2026-09-21 裁定）。

    原话：「**会话内延续在 2 分钟内才有意义**，我们所有延续都应该控制在
    这个时间里面」。

    ⚠️ **这个数写死，而那是第二版**：第一版把它写成「从配包现读」，
    想的是「不写字面量就不会过期」——**反向探针当场证明那是空的**，
    配包一改它跟着漂。本仓「拿常量核常量」那一族。

    它同时把「什么算延续」收紧了：15 分钟那条 `followup_due`
    因此落进 `re_engagement`，受再触达额度与静默时段管。**那是要的。**
    """

    shipped = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "profiles"
            / "internal-demo-sales"
            / "profile.json"
        ).read_text(encoding="utf-8")
    )
    window = shipped["proactivity_policy"]["active_window_seconds"]
    assert window == 120, (
        f"在架配包的活跃窗口是 {window} 秒 —— 用户裁的是 2 分钟；"
        "改了就把这个数改过来，并想清楚「延续」的边界动了没有"
    )

def test_the_shipped_profiles_declare_a_quiet_window_this_policy_can_read() -> None:
    """The knob has a first reader, so the shipped values have to survive it.

    `test_time_rule_shape_is_no_longer_free_form.py` pins the *shape* against the
    schema. This pins that the shape the schema allows is one `knobs()` actually
    accepts -- the two were never connected before, because nothing read the
    field.
    """

    root = Path(__file__).resolve().parents[2]
    for name in ("synthetic-carrier", "synthetic-third"):
        profile = json.loads((root / "profiles" / name / "profile.json").read_text("utf-8"))
        quiet = knobs(profile).quiet_hours
        assert quiet.zone.key == "Asia/Shanghai", name
        assert quiet.ranges == ((time(22, 30), time(8, 0)),), name
