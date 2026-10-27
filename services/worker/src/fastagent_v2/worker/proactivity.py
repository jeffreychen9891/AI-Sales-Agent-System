"""ProactivityPolicy, part one: the classification, and the instant it stops being true.

**B4-4a / RFC §6.4 / ADR-0098 §一 / ADR-0117.** Until this module,
`merchant-profile.schema.json`'s `proactivity_policy` was declared by both
shipped profiles and read by **nothing** -- measured on `main`, all five knobs at
zero hits across `services/**/*.py` and `packages/**/*.py`. A profile author
writing 「沉默 90 秒跟一句」 was writing into a field the platform ignored.

## What is in this batch, and what is deliberately not

ADR-0117 split the original one-batch attempt after that attempt came in at 18
cases, three new ADRs, a migration and a contract change -- past the 「单批约 15
条」 line. The module therefore grew one batch at a time, and the list below is
that history rather than a design: **each entry arrived with the thing that
reads it.**

* `active_window_seconds` -> which of RFC §6.4's two classes this moment is in;
* the instant that classification stops being true, for the dispatcher's cheap
  re-check;
* **B4-4b**: `reengagement_limits` -- the rolling cap the second class is
  subject to and the first is exempt from;
* **B4-4c**: `exit_signals` -- which declared signals stop proactive sending at
  all, mapped onto engine facts by **ADR-0115**;
* **B4-4d**: `time_rules[].strategy` -- **not a knob of `proactivity_policy`**,
  but the other half of the same question: having decided a timer turn may send,
  the rule that armed it says *what play* it sends (ADR-0105 §零.1, ADR-0116);
* **B4-5**: `quiet_hours` -- the window in which nothing proactive goes out,
  evaluated in the profile's IANA zone, plus **`next_allowed_at`**: the instant
  the refusal stops being true, which ADR-0119 gave a column so it can be
  queried rather than parsed back out of a string. ADR-0117 re-scoped this one
  from 「改成按 tenant timezone」 to 「从零建这个机制」, because there was no
  mechanism to change.

**Not here**: `rhythm_templates` (T229, no consumer yet).

**`knobs()` therefore parses four knobs and not five.** Parsing the last one now
would produce a value nothing reads -- ADR-0117 §四's rule, which this module is
the first to be written under: **a field arrives with its consumer or not at
all.** `reengagement_limits` arrived in B4-4b with the gate that counts against
it, `exit_signals` in B4-4c with the refusal it produces, and `quiet_hours` in
B4-5 with both a refusal and a column.

**Two of the three mappings `exit_signals` declares reach production narrowly,
and that is written down rather than implied** (independent review 2026-09-09,
P1-1 / P1-2; user's rulings 2026-09-09): `explicit_opt_out` holds for **one**
timer turn, because the durable signal it reads is consumed and cleared in the
same turn (**T232**); `complaint` holds **never**, because the only turn that
evaluates it is a timer turn whose `understanding` describes a system event and
carries no risk markers (**T233**). The mapping stays -- ADR-0115 ruled it --
but nothing here pretends it is doing more work than it is.

## What this applies to: **timer-triggered turns**

RFC §6.4 opens with 「停滞 timer 到期只触发理解/规划」 and then says
「ProactivityPolicy 在 pre_execute 和 pre_delivery 两次检查」. The subject of the
whole section is the turn a **timer** starts. A reply to a message the customer
just sent is not a proactive send and is not gated here -- and the
classification below would call it an in-session continuation anyway, since
`now - last_inbound_at` is approximately zero.

**This is a reading of the RFC's scope, and it is the one place a different
reading would change behaviour** -- and as of B4-5 that is no longer
hypothetical: quiet hours now refuses, so gating customer replies here would
mean **a customer who writes at 23:00 gets no answer until 08:00**. The scope
sentence above is what keeps a reply a reply.

## Why the same arithmetic has to be available twice

RFC §6.4 (`:572`) requires 「策略执行时和 delivery 前都重算，**不能信任 scheduler
创建时的旧分类**」. A row can sit in the outbox long enough for the answer to
change.

**ADR-0098 §一 rules how the second check is done**: not by re-running this
module in the dispatcher -- that would need profile and contact reads, re-opening
an authorization surface ADR-0094 §二 deliberately narrowed -- but by writing
**one instant** into the outbox command and letting the dispatcher compare it
against its own clock. 「写进行里的不是分类结果，是一个到期时刻。」
`window_expires_at` below is that instant, and `pipeline.dispatch_one` is the
only thing that reads it.

**No database access here.** What is in this module is arithmetic, so it can be
tested against a clock rather than against a schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

#: The two classes RFC §6.4 defines. `IN_SESSION_CONTINUATION` is 「距客户最后一条
#: 消息 ≤ active_window_seconds」 and is exempt from the daily quota;
#: `RE_ENGAGEMENT` is everything beyond it and is not. **The quota itself is
#: B4-4b** -- these names exist here because the classification does, and B4-4b
#: imports them rather than spelling the strings again.
IN_SESSION_CONTINUATION = "in_session_continuation"
RE_ENGAGEMENT = "re_engagement"

#: The exit signals a profile may declare, and **ADR-0115** is what connects each
#: one to a fact the engine actually produces. Before that ruling the two
#: vocabularies had **no word in common**: these three strings appeared only in
#: the two shipped profiles and one contract test, so writing the first reader
#: meant inventing a mapping -- which `AGENTS.md` 图纸优先级 第 3/4 条 forbid.
EXIT_SIGNAL_EXPLICIT_OPT_OUT = "explicit_opt_out"
EXIT_SIGNAL_COMPLAINT = "complaint"
EXIT_SIGNAL_HUMAN_TAKEOVER = "human_takeover"

#: **Two reason codes rather than one plus a boolean** (user's ruling
#: 2026-09-09). RFC §6.4 asks for 「永久/临时 deny」 and
#: `policy-hook.schema.json` reserves `terminal_exit` for it -- but that field
#: is not a column, so a boolean would land in `context` and the distinction
#: would only be recoverable by parsing. Two codes are queryable by everything
#: that already groups on `reason_code`, `GOVERNANCE_SQL` included.
#:
#: **Which is which** (user's ruling 2026-09-09): **only `explicit_opt_out` is
#: permanent.** A takeover ends -- control comes back and the conversation
#: resumes. **A complaint is temporary too**, and that is a product judgement
#: rather than a derivation: a complaint is something a person handles, and when
#: they have handled it the conversation should be able to continue. RFC §6.4
#: asks for 「永久/临时」 without assigning the signals, so this assignment is
#: the ruling, not a reading of the RFC.
#:
#: **A mixed verdict is permanent** (same ruling): an opt-out outlives the
#: takeover or the complaint standing beside it.
EXIT_PERMANENT_REASON = "PROACTIVITY_EXIT_PERMANENT"
EXIT_TEMPORARY_REASON = "PROACTIVITY_EXIT_TEMPORARY"

#: `silence_check` is the one kind RFC §6.4 constrains by name: 「只允许在活跃窗口
#: 内产生会话内延续；消费时已超窗则只能 `no_message`、作废」. Kept as a set so the
#: sentence has one home rather than being spelled at the comparison.
SILENCE_CHECK_KINDS = frozenset({"silence_check"})


class ProactivityPolicyError(ValueError):
    """The profile asked for something this policy cannot evaluate.

    Raised rather than defaulted, on the same argument `time_rules.py` makes: a
    missing `active_window_seconds` is not a runtime condition, it is a profile
    `load_package` should have refused. Defaulting would silently give a
    merchant a window they never declared.
    """


#: How far ahead `next_allowed_at` is allowed to be searched for. A profile
#: whose ranges cover every hour of the day has no such instant, and that is a
#: profile error rather than a runtime condition -- so the search is bounded and
#: the caller is told, instead of looping or inventing an answer.
QUIET_SEARCH_LIMIT = timedelta(hours=48)

#: The reason code a quiet-hours refusal carries. **Its own code, not a shared
#: one**: it is the single refusal in this module that expires by the clock, and
#: `policy_decisions.next_allowed_at` is non-NULL exactly for it (ADR-0119).
QUIET_HOURS_REASON = "PROACTIVITY_QUIET_HOURS"


@dataclass(frozen=True, slots=True)
class QuietHours:
    """One profile's quiet window, already resolved into a usable form.

    **The zone is a `ZoneInfo`, not a string, because parsing is where this can
    fail** and `knobs()` is the place allowed to fail (ADR-0117 §四's rule that a
    profile the policy cannot read is refused rather than defaulted). Carrying
    the string would move the failure to the first evaluation, which happens
    inside the accounting transaction.

    **`ranges` are wall-clock times, deliberately.** RFC §6.2: 「所有时间存 UTC；
    quiet hours 使用 profile IANA timezone 计算」. A merchant who says 22:30 means
    22:30 where they are, on whatever offset that date happens to have -- which
    is why nothing here is precomputed into UTC.
    """

    zone: ZoneInfo
    #: `(start, end)` wall times. `start > end` means the range crosses
    #: midnight, which is **the shape both shipped profiles use** (22:30-08:00)
    #: and therefore the main path, not an edge case.
    ranges: tuple[tuple[time, time], ...]

    def covers(self, moment: datetime) -> bool:
        """Is this instant inside the quiet window?

        Converting an instant to a wall clock always succeeds -- there is no
        such thing as a UTC instant with no local time -- so this direction of
        the question needs none of the DST care that `lifts_at` does.
        """

        wall = moment.astimezone(self.zone).timetz().replace(tzinfo=None)
        return any(_within(wall, start, end) for start, end in self.ranges)

    def lifts_at(self, moment: datetime) -> datetime:
        """The first instant at or after `moment` that is **not** quiet.

        **RFC §6.2 asks for 「明确的『首次合法本地时刻』规则」 across DST, and this
        is that rule stated as a search rather than as arithmetic.** Walking
        forward and asking `covers()` -- which only ever converts an instant to
        a wall clock, the direction that cannot fail -- gets the repeated hour
        and the missing hour right by construction:

        * **missing hour** (spring forward): the wall times inside the gap never
          appear, so the first minute this returns is the first one that really
          exists -- which is exactly 「首次合法本地时刻」;
        * **repeated hour** (fall back): the wall clock passes the boundary
          twice, and walking forward stops at the **first** occurrence.

        **Why a scan and not offset arithmetic.** The arithmetic version has to
        resolve a wall time to an instant, and that is the operation with no
        answer during a gap and two answers during a fold -- it is where DST
        bugs live. This runs at most once per refused proactive turn, on a path
        that has already decided not to send, so a bounded minute-by-minute walk
        costs nothing worth optimising. **Correctness here is worth more than
        elegance**, and the elegant version is the one that would need the
        tests to be trusted rather than the code.

        Raises `ProactivityPolicyError` when the ranges leave no allowed instant
        within `QUIET_SEARCH_LIMIT` -- a profile quiet around the clock. That is
        a profile that can never send proactively, which `load_package` cannot
        currently refuse (T237), so it fails **closed** here rather than
        returning a time that does not mean anything.
        """

        step = timedelta(minutes=1)
        candidate = moment.astimezone(UTC).replace(second=0, microsecond=0)
        if candidate < moment:
            candidate += step
        deadline = moment + QUIET_SEARCH_LIMIT
        while candidate <= deadline:
            if not self.covers(candidate):
                return candidate
            candidate += step
        raise ProactivityPolicyError(
            "proactivity_policy.quiet_hours leaves no allowed instant within "
            f"{QUIET_SEARCH_LIMIT}: the ranges cover the whole day"
        )

    def opens_at(self, moment: datetime) -> datetime | None:
        """The first instant at or after `moment` that **is** quiet.

        The second source of `window_expires_at` (ADR-0119 §三.1): a row queued
        while sending is allowed stops being sendable when the window opens, and
        the dispatcher's cheap re-check needs the earlier of the two deadlines.

        **`None` is defensive and unreachable on the contract** (independent
        review 2026-09-09, P3-2). The first version of this docstring justified
        it with 「没有静默时段 is a legal profile」 -- **which is false**:
        `merchant-profile.schema.json` lists `quiet_hours` in
        `proactivity_policy.required`, `ranges` is `minItems: 1`, and
        `_quiet_hours` refuses an empty one. Any profile that survives `knobs()`
        therefore has a window opening within 24 hours. The branch stays because
        a total function is cheaper than an assertion, not because a caller will
        see it.

        **This one runs on the *allowed* path**, unlike `lifts_at` -- once per
        proactive message that is let through, not once per refusal. Measured on
        the shipped 22:30-08:00 window: **0.5-0.9 ms**, the slowest case being
        an instant just after the window closed (roughly 870 minutes to walk).
        `lifts_at`'s 「跑在一条已经决定不发的路径上」 argument does **not** cover
        this call, and saying so is the point of this paragraph.
        """

        step = timedelta(minutes=1)
        candidate = moment.astimezone(UTC).replace(second=0, microsecond=0)
        if candidate < moment:
            candidate += step
        deadline = moment + QUIET_SEARCH_LIMIT
        while candidate <= deadline:
            if self.covers(candidate):
                return candidate
            candidate += step
        return None


def _within(wall: time, start: time, end: time) -> bool:
    """Half-open `[start, end)`, and it wraps when `start > end`.

    Half-open so that a range ending at 08:00 permits sending **at** 08:00; a
    closed interval would make `next_allowed_at` name an instant that is itself
    still refused.
    """

    if start <= end:
        return start <= wall < end
    return wall >= start or wall < end


@dataclass(frozen=True, slots=True)
class ProactivityKnobs:
    """The knobs read so far. **Four of RFC §6.4's five, on purpose**
    (ADR-0117 §四).

    `active_window_seconds`, `reengagement_limits`, `exit_signals` and
    `quiet_hours` are read; `rhythm_templates` (**T229**) is deliberately
    absent, because parsing it here would produce a value nothing reads. One
    knob is not one field: `reengagement_limits` yields the two
    `reengagement_*` fields below.

    **This sentence said "Two" and listed `exit_signals` as absent until
    2026-09-09**, when B4-4c gave that knob its first reader and updated the
    module docstring above without updating this one -- leaving the same file
    saying "three" in one place and "two" in another. It is recorded here
    rather than quietly corrected because it is the T92 / T98 / T118 family
    (the record disagreeing with the fact), and because the fix that missed it
    was itself a review finding about a miscount. **Adding a knob means editing
    this list**; a batch that adds one and leaves this alone has produced the
    same defect again.
    """

    active_window_seconds: int
    #: Which declared exit signals stop proactive sending for this merchant.
    #: **A declaration, not a platform constant** (ADR-0115 §二.2): a merchant
    #: who does not list `complaint` has decided a complaint does not stop
    #: follow-ups -- it remains a `risk_marker` and still reaches every other
    #: judgement. Same family as `strategy` and `cancel_on` (ADR-0105 §零.1).
    exit_signals: frozenset[str]
    #: RFC §6.4: 「默认每客户滚动 **24 小时最多 1 次**，profile 只能收紧」.
    #: **Per customer, and the window is the profile's** -- both shipped
    #: profiles declare 86400/1, which is the default said out loud rather than
    #: a tightening.
    reengagement_window_seconds: int
    reengagement_max_delivered: int
    #: RFC §6.4's 「静默时段」, evaluated in the **profile's** IANA zone (RFC §6.2
    #: says so literally; ADR-0097 calls the same thing 「tenant timezone」
    #: because inside M1b this field is the only timezone the platform has --
    #: there is no `tenants.timezone` column, and channel callbacks carry no
    #: customer zone, ADR-0103 §四).
    quiet_hours: QuietHours


@dataclass(frozen=True, slots=True)
class ProactivityVerdict:
    """One timer turn's proactivity answer.

    **`next_allowed_at` arrives here in B4-5, with its consumer and with a
    column to land in** (ADR-0119). ADR-0117 §四 pulled it out of the first
    attempt because it was computed and then dropped -- the refusal path
    returned before the contract was built, so it was a constant `None` at the
    only place it reached. It comes back now because quiet hours is its
    producer, `policy_decisions.next_allowed_at` is its landing place, and
    `window_expires_at` reads the same arithmetic.

    **`terminal_exit` is still absent**, and still for ADR-0117 §四's reason:
    `exit_signals` (B4-4c) chose two reason codes over a boolean, so nothing
    produces it.
    """

    classification: str
    effect: str
    reason_code: str
    detail: str
    #: The instant this verdict stops being true, for the dispatcher's cheap
    #: re-check (ADR-0098 §一). `None` on a denial: nothing is queued.
    window_expires_at: datetime | None
    #: When this refusal stops being true, for refusals that expire by the
    #: clock. **`None` is a statement, not a gap** (ADR-0119 §二.1): this
    #: refusal has no instant that un-says it. An exit signal is `None`; a
    #: quiet-hours refusal never is. Lands in `policy_decisions.next_allowed_at`.
    #:
    #: **Required, not defaulted**, on the same argument that made `evaluate()`'s
    #: two arguments required (B4-4c independent review, P2-1): a default would
    #: let the next refusal that *does* expire by the clock be built without one
    #: and land as `NULL`, which reads as 「这条拒绝没有失效时刻」 -- a wrong
    #: statement rather than a missing value.
    next_allowed_at: datetime | None
    #: **When the window this row is queued under lifts**, for a row the
    #: dispatcher will later cancel. Non-`None` only when `window_expires_at`
    #: came from quiet hours -- an active window closing has no end to name,
    #: because past it the message is a different class rather than a delayed
    #: one. Carried into `command.proactivity` so `_cancel_expired_proactive`
    #: can write `policy_decisions.next_allowed_at` **without reading the
    #: profile**, which ADR-0098 §一 forbids it (independent review 2026-09-09,
    #: P1-1: the batch that defined this column's semantics also produced the
    #: first row breaking them).
    window_lifts_at: datetime | None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


def knobs(profile: dict[str, Any]) -> ProactivityKnobs:
    """Read what this batch reads out of a profile release.

    The release is the one pinned on the conversation, never "the current
    profile": a mid-conversation publish must not move a customer's active
    window (the same rule `_schedule_from_outbound` states for time rules).
    """

    policy = profile.get("proactivity_policy")
    if not isinstance(policy, dict):
        raise ProactivityPolicyError("profile carries no proactivity_policy")
    limits = policy.get("reengagement_limits")
    if not isinstance(limits, dict):
        # **A plain check rather than an `assert`** (independent review
        # 2026-09-09, P3-6a). The behaviour was already safe -- under `-O` the
        # assert vanishes and `None["window_seconds"]` raises `TypeError`, which
        # the same `except` catches -- but this repository keeps a note about
        # every `assert` left on a production path (`pipeline.dispatch_one`), and
        # not needing the note is cheaper than writing it.
        raise ProactivityPolicyError("proactivity_policy.reengagement_limits is not an object")
    try:
        window = int(policy["active_window_seconds"])
        rolling = int(limits["window_seconds"])
        cap = int(limits["max_delivered"])
    except (KeyError, TypeError, ValueError) as error:
        raise ProactivityPolicyError(
            "proactivity_policy needs active_window_seconds and "
            "reengagement_limits{window_seconds, max_delivered}"
        ) from error
    declared = policy.get("exit_signals")
    if not isinstance(declared, list):
        raise ProactivityPolicyError("proactivity_policy.exit_signals is not an array")
    return ProactivityKnobs(
        active_window_seconds=window,
        exit_signals=frozenset(str(item) for item in declared),
        reengagement_window_seconds=rolling,
        reengagement_max_delivered=cap,
        quiet_hours=_quiet_hours(policy),
    )


def _quiet_hours(policy: dict[str, Any]) -> QuietHours:
    """Parse `quiet_hours`, **failing rather than defaulting**.

    RFC §6.4's last clause on the subject is 「timezone 未知不主动发送」, and this
    is where that is executed: an unreadable zone raises, `process_prepared`
    turns the raise into `PROACTIVITY_POLICY_UNREADABLE` and persists a no-send.
    **The failure direction is 「不发」**, which is the direction the sentence
    asks for.

    **Defaulting would be the worse failure and it is worth naming.** A missing
    or misspelled zone defaulted to UTC would put a Shanghai merchant's quiet
    hours eight hours off -- messages going out at 06:30 local while the profile
    says 22:30-08:00, and nothing anywhere reporting a problem. The schema makes
    both fields required, so reaching any of these raises means a release got
    published that `load_package` should have refused; raising keeps that a
    visible failure instead of a silent timezone.
    """

    quiet = policy.get("quiet_hours")
    if not isinstance(quiet, dict):
        raise ProactivityPolicyError("proactivity_policy.quiet_hours is not an object")
    name = quiet.get("timezone")
    if not isinstance(name, str) or not name:
        raise ProactivityPolicyError("proactivity_policy.quiet_hours.timezone is not a zone name")
    try:
        zone = ZoneInfo(name)
    except (KeyError, ValueError, OSError) as error:
        # **Three exception families, because a zone name is a filesystem key**
        # and `ZoneInfo` reports a bad one in whatever way the lookup failed.
        # Measured rather than guessed:
        #
        # * `"Mars/Olympus_Mons"` -> `ZoneInfoNotFoundError` (a `KeyError`);
        # * `""` and `"../etc/passwd"` -> `ValueError` (`zoneinfo` rejects
        #   traversal itself, so that one never reaches the filesystem);
        # * `"   "` -> **`PermissionError`**, and a 300-character name ->
        #   `OSError`. Both are `OSError`, and neither is a `KeyError`.
        #
        # **The `OSError` half is why this matters.** `knobs()` runs inside the
        # accounting transaction, and `process_prepared` turns
        # `ProactivityPolicyError` into a persisted no-send. An escaping
        # `PermissionError` would not be caught there -- it would take down the
        # turn from inside the transaction rather than refusing to send, which
        # is the opposite of the fail-closed direction this function exists for.
        raise ProactivityPolicyError(
            f"proactivity_policy.quiet_hours.timezone {name!r} is not an IANA zone"
        ) from error
    raw = quiet.get("ranges")
    if not isinstance(raw, list) or not raw:
        raise ProactivityPolicyError("proactivity_policy.quiet_hours.ranges is empty")
    ranges: list[tuple[time, time]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProactivityPolicyError("proactivity_policy.quiet_hours.ranges holds a non-object")
        try:
            start = time.fromisoformat(str(item["start"]))
            end = time.fromisoformat(str(item["end"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ProactivityPolicyError(
                "proactivity_policy.quiet_hours.ranges needs {start, end} as wall clocks"
            ) from error
        if start == end:
            # **Not the same as covering nothing, and not the same as covering
            # everything** -- it is unreadable, so it is refused here rather
            # than guessed. `lifts_at` would loop to its limit on the
            # "everything" reading and never refuse on the "nothing" one.
            raise ProactivityPolicyError(
                f"proactivity_policy.quiet_hours range {start}-{end} starts where it ends"
            )
        ranges.append((start, end))
    return QuietHours(zone=zone, ranges=tuple(ranges))


def classify(*, last_inbound_at: datetime | None, now: datetime, active_window_seconds: int) -> str:
    """Which of RFC §6.4's two classes this moment falls in.

    **Anchored on the customer's last inbound**, which is what §6.4 says: 「以客户
    最后一条已验真 inbound 的 `occurred_at` 为锚点」. `conversation_aggregates.state`
    carries it as `last_inbound_at`, written from the envelope in
    `InboxProcessor.prepare`.

    **The boundary is inclusive on the in-session side** -- 「距客户最后一条消息
    `≤ active_window_seconds`」 -- so a moment exactly one window after the
    customer spoke is still a continuation.

    **A conversation with no inbound at all is a re-engagement.** The customer
    has never spoken, so there is no session to continue -- and the alternative
    (treat a missing anchor as "inside the window") would give the widest
    permission to the case with the least evidence.

    **The anchor is a timestamp the caller gave us, and this batch is the first
    thing that lets it decide whether a message goes out** (independent review
    2026-09-09, P2-1). `last_inbound_at` is `envelope["occurred_at"]`, which
    `ingress/adapter.py` reads straight from the callback body and checks only
    for carrying a timezone -- **there is no bound on how far it may sit from
    our own clock**, and the same envelope carries `received_at`, which is ours.
    Both directions cost something: too old (a slow peer clock, or a channel
    redelivery -- this repository's own note says channels retry "for minutes
    and occasionally hours") misreads a live session as a re-engagement and
    **refuses** the follow-up; too new keeps the window open past the point
    anything re-checked it.

    **Not changed here, deliberately**: RFC §6.4 says 「以客户最后一条已验真
    inbound 的 `occurred_at` 为锚点」, and picking a different anchor is a
    revision of the drawing, not an implementation choice. **Registered as
    T231**, whose due condition is written to the moment the question actually
    becomes answerable: B3 wiring the real inbound mapping, which is where
    somebody decides whether `occurred_at` is the provider's instant or ours.
    """

    if last_inbound_at is None:
        return RE_ENGAGEMENT
    elapsed = (now - last_inbound_at).total_seconds()
    return IN_SESSION_CONTINUATION if elapsed <= active_window_seconds else RE_ENGAGEMENT


def window_deadline(
    *,
    classification: str,
    last_inbound_at: datetime | None,
    knobs: ProactivityKnobs,
    now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """The instant this verdict stops being true. **ADR-0098 §一's 「到期时刻」.**

    The dispatcher compares this against its own clock and cancels the row if it
    has passed. That is the whole of the `pre_delivery` check: no profile read,
    no contact read, no quota read -- 「到点作废是一次纯本地判断」.

    **An in-session continuation stops being one when the active window
    closes.** Past that instant the same message would be a *re-engagement*,
    which (from B4-4b) has a quota that was never applied to it -- so sending it
    would be delivering a message under a classification nothing checked.

    **There are two sources, and B4-5 added the second one.** Until this batch
    a re-engagement had no expiry at all and this returned a constant `None` for
    it. A quiet window opening ahead of the row expires **both** classes: a
    message queued at 22:00 and dispatched at 22:40 would go out inside the
    merchant's quiet hours, having been judged before they began.

    So the answer is the **earlier** of:

    * the active window closing -- in-session continuations only, because that
      is the instant the classification itself changes;
    * the next quiet window opening -- **both classes**, because quiet hours
      constrain sending rather than classification.

    **This is also why a re-engagement's expiry stops being constant** (ADR-0117
    §四's rule applied to a field that already existed): the second source is
    the first thing that ever put a value there.
    """

    deadlines: list[datetime] = []
    if classification == IN_SESSION_CONTINUATION and last_inbound_at is not None:
        deadlines.append(last_inbound_at + timedelta(seconds=knobs.active_window_seconds))
    quiet_opens = knobs.quiet_hours.opens_at(now)
    if quiet_opens is not None:
        deadlines.append(quiet_opens)
    if not deadlines:
        return None, None
    earliest = min(deadlines)
    if quiet_opens is None or earliest != quiet_opens:
        # The active window closed first: past that instant this message would be
        # a re-engagement carrying rules nothing applied to it, and **waiting does
        # not fix that**. There is no instant to name.
        return earliest, None
    # **Quiet hours won, so this expiry has an end** -- and the dispatcher must
    # be told it here, because it is forbidden to read the profile (ADR-0098 §一:
    # 「到点作废是一次纯本地判断」). `lifts_at` is asked about the window that is
    # about to open, not about `now`.
    return earliest, knobs.quiet_hours.lifts_at(earliest)


def active_exit_signals(
    *,
    knobs: ProactivityKnobs,
    aggregate: dict[str, Any],
    understanding: dict[str, Any] | None,
    pending_exit_signal: dict[str, Any] | None,
) -> tuple[str, ...]:
    """The declared exit signals that are true right now. **ADR-0115 §二's table.**

    **Anything read from `prepared.aggregate` at policy time must first be
    checked against what `process_prepared` does to it between reading the
    aggregate and calling this policy** (independent review 2026-09-09 §1(b);
    re-review R3). **No line numbers here on purpose** -- the pair this
    paragraph used to name was wrong when it was written and would have drifted
    anyway. The durable statement is the shape: on a timer turn
    `process_prepared` writes the aggregate **three times** before the policy
    runs -- it **clears `pending_exit_signal`** the moment it feeds the signal
    to the planner, then **replaces the aggregate wholesale** with the
    transitioned stack (`clear()` + `update()`), and the closing-frame branch
    **replaces it a second time**. `last_inbound_at` and `control_mode` are
    untouched. **`target_stack` is the next candidate for exactly this trap** --
    the moment any batch has the policy glance at frame status, the shape below
    repeats itself.

    **`pending_exit_signal` is an argument rather than a field read off the
    aggregate, and that is not cosmetic.** `process_prepared` **clears it** the
    moment it feeds the signal to the planner -- long before this policy runs --
    so reading `aggregate["pending_exit_signal"]` here would find `None` every
    single time and the durable half of the mapping would be silently dead.
    The caller passes the value it captured before clearing.

    **Why the durable half exists at all** (ADR-0115 §二.1): the turn being
    judged is a **timer** turn, whose `understanding` describes a system event
    and not anything the customer said. Reading only this turn would mean a
    customer says 「别再发了」, a timer fires later, and the platform has
    forgotten.

    **`human_takeover` is reported whether or not the profile declared it**, and
    that is ADR-0115 §二.2's one exception: refusing to act while a human holds
    the conversation is an engine invariant, not a merchant preference. Listing
    it keeps the record's account of *why* complete; it does not make it
    switchable.

    **The `control_mode` half is defence in depth and unreachable from this call
    site**: `process_prepared` hands a human-controlled conversation to
    `_process_under_human_control` before any of this runs -- and **that
    interception, not this branch, is what provides ADR-0115 §二.2's
    engine-level invariant.** What *is* reachable is the durable `human_request`
    relation -- control came back, and the signal the customer gave the human is
    still on the aggregate -- **and that half lives exactly as long as
    `explicit_opt_out` does**: same field, same clearing, one timer, then gone
    (re-review R1; folded into **T232**, not a second registration -- it is the
    same column being cleared the same way).
    """

    pending = pending_exit_signal if isinstance(pending_exit_signal, dict) else {}
    pending_relation = str(pending.get("relation") or "")
    relation = ""
    markers: list[str] = []
    if isinstance(understanding, dict):
        interruption = understanding.get("interruption") or {}
        if isinstance(interruption, dict):
            relation = str(interruption.get("relation") or "")
        markers = [str(marker) for marker in understanding.get("risk_markers") or []]

    active: list[str] = []
    # **The durable half holds for exactly one timer turn** (P1-1; user's ruling
    # 2026-09-09 -- acknowledge and register, do not change T53/T54's semantics).
    # `process_prepared` clears `pending_exit_signal` in the same turn it feeds
    # it to the planner and writes the cleared aggregate back, so the **next**
    # timer sees nothing here. `PROACTIVITY_EXIT_PERMANENT` therefore names the
    # kind of refusal, not its lifetime. **T232.**
    if EXIT_SIGNAL_EXPLICIT_OPT_OUT in knobs.exit_signals and "exit" in (
        relation,
        pending_relation,
    ):
        active.append(EXIT_SIGNAL_EXPLICIT_OPT_OUT)
    # **This branch has no production path today, and saying so is the ruling**
    # (independent review 2026-09-09, P1-2; user's ruling 2026-09-09 -- register
    # as 待接线, do not pretend it works). The only turn that evaluates it is a
    # **timer** turn, whose `understanding` is produced by `understand()` for
    # that one system event -- the prompt carries no history -- and the aggregate
    # holds no persistent risk marker. So a `complaint` raised on a customer
    # message cannot reach the place that reads it. **T233.**
    #
    # The mapping stays because ADR-0115 ruled it; what changes is that nothing
    # here claims it is doing work.
    if EXIT_SIGNAL_COMPLAINT in knobs.exit_signals and EXIT_SIGNAL_COMPLAINT in markers:
        active.append(EXIT_SIGNAL_COMPLAINT)
    # **Only one of these two halves is reachable, and it is the short-lived
    # one** (independent re-review 2026-09-09, R1). `control_mode == "human"`
    # never gets here -- `process_prepared` hands such a conversation to
    # `_process_under_human_control` first, and **that interception is what
    # provides ADR-0115 §二.2's invariant, not this branch**. What is left is
    # `pending_relation == "human_request"`, i.e. the same durable field the
    # opt-out branch reads, cleared by the same turn: **one timer, then gone.
    # Folded into T232, not registered separately.**
    if str(aggregate.get("control_mode")) == "human" or "human_request" in (
        relation,
        pending_relation,
    ):
        active.append(EXIT_SIGNAL_HUMAN_TAKEOVER)
    return tuple(active)


def evaluate(
    *,
    knobs: ProactivityKnobs,
    aggregate: dict[str, Any],
    event_kind: str | None,
    now: datetime,
    understanding: dict[str, Any] | None,
    pending_exit_signal: dict[str, Any] | None,
) -> ProactivityVerdict:
    """The `pre_execute` half of RFC §6.4 that these batches carry.

    **`understanding` and `pending_exit_signal` are required, not defaulted**
    (independent review 2026-09-09, P2-1). This batch exists because *passing*
    those values is the thing nothing guards -- a default would open the same
    door again, and a caller who forgot would get a **silent allow** rather than
    an error. Today there is one caller; the ones that will trip over it are
    B4-5's quiet hours and B4-4e's frame check.

    **The order of the two refusals is the order of their finality**: an exit
    signal is a standing refusal about the conversation, the `silence_check`
    window rule is about this one event. Reporting the first one that bites
    means the reason code names the thing a person would have to change.

    Quiet hours (B4-5) is the one refusal still missing, and it cannot be
    evaluated from the arguments here.
    """

    exits = active_exit_signals(
        knobs=knobs,
        aggregate=aggregate,
        understanding=understanding,
        pending_exit_signal=pending_exit_signal,
    )
    last_inbound_at = _parse(aggregate.get("last_inbound_at"))
    classification = classify(
        last_inbound_at=last_inbound_at,
        now=now,
        active_window_seconds=knobs.active_window_seconds,
    )
    if exits:
        # **Permanent iff the customer opted out** (user's ruling 2026-09-09).
        # A takeover ends when control comes back; a complaint ends when
        # somebody has handled it. Only 「别再发了」 has nothing in this
        # milestone that un-says it -- so a mixed verdict is permanent, because
        # the opt-out outlives whatever stands beside it.
        #
        # Two reason codes rather than a boolean in `context`, so that anything
        # grouping on `reason_code` already tells them apart.
        permanent = EXIT_SIGNAL_EXPLICIT_OPT_OUT in exits
        return ProactivityVerdict(
            classification=classification,
            effect="deny",
            reason_code=EXIT_PERMANENT_REASON if permanent else EXIT_TEMPORARY_REASON,
            detail=f"the conversation carries {','.join(exits)}",
            window_expires_at=None,
            # **An exit signal has no expiry by the clock.** `None` here is the
            # statement ADR-0119 §二.1 defines, not an unfilled field: nothing
            # in this milestone un-says 「别再发了」, and a takeover ends when a
            # person hands control back rather than at an hour we could name.
            next_allowed_at=None,
            window_lifts_at=None,
        )

    if event_kind in SILENCE_CHECK_KINDS and classification == RE_ENGAGEMENT:
        # RFC §6.4: 「`silence_check` 只允许在活跃窗口内产生会话内延续；消费时已超窗则
        # 只能 `no_message`、作废或由 profile 另建合法 `goal_stall`，**不得借旧
        # silence event 绕过再触达配额**」. The last clause is the reason this is
        # a refusal rather than a reclassification: letting an overdue
        # `silence_check` become a re-engagement is exactly the bypass -- and it
        # has to be refused **now**, in the batch that introduces the
        # classification, rather than waiting for the quota it would bypass.
        return ProactivityVerdict(
            classification=classification,
            effect="deny",
            reason_code="PROACTIVITY_SILENCE_CHECK_OUTSIDE_WINDOW",
            detail="a silence_check consumed outside the active window may not send",
            window_expires_at=None,
            # This event will never become sendable -- waiting does not fix a
            # `silence_check` that outlived its window, it is the bypass RFC
            # §6.4 refuses. So there is no instant to name.
            next_allowed_at=None,
            window_lifts_at=None,
        )
    if classification == RE_ENGAGEMENT and knobs.quiet_hours.covers(now):
        # ⚠️ **只挡唤醒，不挡会话内延续**（用户 2026-09-21 裁定：
        # 「静默时间应该只限制唤醒对话，而不是正常销售推进 ——
        # **很有可能客户就是晚上进来的**」）。
        #
        # 在此之前这一行不分 classification，实测（同日）：
        # 客户深夜 1 分钟前刚说过话，`in_session_continuation` 也被
        # `PROACTIVITY_QUIET_HOURS` 拒掉 —— **而那一轮是他自己开的头**。
        #
        # 两者的区别是**谁先开的口**：再触达是我们去敲一个已经安静下来的人的门，
        # 会话内延续是把他正在进行的那句话说完。静默时段管的是前者。
        #
        # **Last of the three refusals, because it is the least final** (B4-5).
        # An exit signal is a standing refusal about the conversation and an
        # overdue `silence_check` is an event that may never send; quiet hours
        # is neither -- it is 「不是现在」, and it is the only one of the three
        # that can name the instant it stops being true.
        #
        # **Order matters for the reason code, not for the outcome**: all three
        # refuse. Reporting the most final one means the code names the thing a
        # person would have to change, which for a quiet-hours refusal is
        # nothing -- they wait.
        return ProactivityVerdict(
            classification=classification,
            effect="deny",
            reason_code=QUIET_HOURS_REASON,
            detail="the profile's quiet hours cover this instant",
            window_expires_at=None,
            next_allowed_at=knobs.quiet_hours.lifts_at(now),
            # A refusal queues nothing, so no row will need this later.
            window_lifts_at=None,
        )
    expires_at, lifts_at = window_deadline(
        classification=classification, last_inbound_at=last_inbound_at, knobs=knobs, now=now
    )
    return ProactivityVerdict(
        classification=classification,
        effect="allow",
        reason_code="PROACTIVITY_ALLOWED",
        detail="",
        window_expires_at=expires_at,
        # An allow names no expiry for the refusal it is not.
        next_allowed_at=None,
        window_lifts_at=lifts_at,
    )


def strategy_for_rule(*, profile: dict[str, Any], rule_id: str) -> str | None:
    """The `strategy` the time rule that produced this timer declared.

    **The first production reader of `time_rules[].strategy`** (ADR-0105 §零.1,
    which registered it as 待接线 and named this batch as the consumer). The
    field is not a copy on the `scheduled_events` row: B4-2 wrote only
    `payload_ref = {"rule_id": …}` and pinned the release, precisely so the
    strategy is read from the rule rather than from a second copy that can
    disagree with it.

    `None` when the pinned release no longer declares the rule -- which is
    possible, because a release can be superseded while a timer is pending. The
    caller treats that as "no declared strategy" rather than as an error: the
    timer is still legitimate, and refusing it would turn a publish into a
    dropped conversation.

    **The scan is document-wide, and that is only safe because rule ids are
    unique document-wide** (independent review 2026-09-09, P2-1). The timer
    carries a rule id and nothing else, so there is no frame to scope the search
    to. Two frames sharing an id would make the first one found bind the other
    frame's timer -- and it would not fail closed: if the firing frame's
    `time_rule_strategy_set` happens to contain the wrong rule's strategy, the
    plan passes the validator and goes out. **The premise is enforced at
    admission**, by `profiles.registry.load_package`, so an unpublishable
    profile can never reach here; it is not enforced again on this path, which
    runs on every timer turn.
    """

    for definition in profile.get("target_frames") or []:
        for rule in definition.get("time_rules") or []:
            if str(rule.get("id")) == rule_id:
                strategy = rule.get("strategy")
                return str(strategy) if strategy else None
    return None


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
