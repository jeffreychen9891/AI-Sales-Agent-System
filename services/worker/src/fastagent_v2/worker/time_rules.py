"""Where a `scheduled_events` row comes from: a `TimeRule`, and never the model.

**B4-2 / ADR-0096 §二.1.** Until this module, `scheduled_events` had **no
production writer at all** -- `INSERT INTO scheduled_events` appeared only in
test fixtures, while the scheduler drained a table nobody filled and
`plan-result.schema.json` let the model *propose* events that
`inference_validation.py` checked and then dropped on the floor.

> **校验一个从不持久化的东西，比不校验更糟：它让人以为那条路通了。**

The ruling has three parts and this module is the first: **the rule derives
`due_at` and `dedupe_key`**, the model's `scheduled_event_changes` stay
advisory, and persistence actually happens. The reasons are RFC §17.2's
requirement that these times be reproducible under a virtual clock, and §6.4's
"不能信任 scheduler 创建时的旧分类" -- a re-check at consumption time is only
possible if creation was deterministic in the first place.

**The reproducibility claim is about `due_at`** (independent review 2026-09-09,
P3-1). It is arithmetic on two values the caller supplies, so replaying a
conversation under a virtual clock reproduces the same instant. `_when_satisfied`
is not in that claim: it compares this process's `now` against timestamps the
database wrote, and says so where it does it.

## When a rule becomes a row: **at the moment its anchor becomes known**

This is the one design decision of the batch, and it is forced rather than
chosen. `due.anchor` names where the clock starts, and the two anchors M1b can
resolve become true at different moments:

* `frame_entered_at` -- true as the transition writes the stack, so those rules
  are created inside `_apply_aggregate_transition`;
* `last_outbound_at` -- **not true until a message has actually been
  delivered**, which happens later, in the dispatcher, in a different
  transaction (`pipeline.py`, where `last_outbound_at = now()` is stamped).

Creating a `last_outbound_at` rule at transition time would anchor it to the
*previous* turn's outbound -- for the shipped 75-second `silence_check` that
means a timer already overdue at birth, firing immediately and reporting
silence about a message the customer has not been sent yet. ADR-0105 §零.2
ruled that anchor precisely to avoid measuring the wrong interval; honouring the
ruling means creating the row where the value exists.

* `commitment_due_at` -- **resolvable since 11d**, and unlike the other two it
  is **one moment per commitment row**, not one per conversation. The caller
  passes the commitment's `due_at` and its id together; the id ends up both in
  the dedupe key (so two promises in one frame are two timers rather than one
  timer moved later) and in `scheduled_events.commitment_id`, which migration
  064 gives a foreign key. Before 11d this anchor was skipped for want of a
  ledger, and ADR-0100 had moved `customer_promise_due` / `ai_commitment_due`
  out of M1b for the same reason; migration 063 built the ledger and
  `DEFERRED_KINDS` is empty as of this batch.

**One anchor remains unresolvable and is skipped**: `business_event_at` needs a
verified business event, which nothing produces yet. Skipping is silent by
design -- a profile may legitimately declare rules for a milestone that has not
arrived -- and `tests/contracts/test_time_rule_producer.py` holds the shipped
profiles to declaring nothing this milestone cannot schedule, so "silently
skipped" cannot become "silently never" for a rule somebody is counting on.

**A second way to be unschedulable arrived with 11d**: a `commitment_due_at`
rule whose `dedupe_template` names no `{commitment_id}` resolves its anchor
perfectly well and still never gets created, because the key it renders cannot
tell two commitments apart. `unschedulable_rules` reports both ways, and the
same shipped-profile test covers both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

#: The anchors this milestone can resolve. Not "the anchors that exist" --
#: `merchant-profile.schema.json` names four and the fourth is still waiting on
#: machinery nothing builds (see the module docstring).
#:
#: **`commitment_due_at` joined this set in batch 11d**, once `commitments`
#: (migration 063) gave it a value to resolve against. It differs from the other
#: two in a way every caller has to honour: `frame_entered_at` and
#: `last_outbound_at` are **one moment per conversation**, while
#: `commitment_due_at` is **one moment per commitment row** -- so the same frame
#: can want several of these rules at once, and they are told apart by
#: `{commitment_id}` in the dedupe template rather than by the rule alone.
SCHEDULABLE_ANCHORS = frozenset(
    {"frame_entered_at", "last_outbound_at", "commitment_due_at"}
)

#: The anchors that need a per-row value passed in rather than one moment the
#: conversation carries. **One entry today**; it is a set rather than an `is`
#: comparison so the next per-row anchor (`business_event_at`, when something
#: produces business events) lands by joining it instead of by adding a branch.
PER_ROW_ANCHORS = frozenset({"commitment_due_at"})

#: The semantic fields a `dedupe_template` may name (RFC §6.2, "allowlisted
#: semantic fields"; ADR-0105 §零 D3 assigned the allowlist to this batch).
#:
#: **Three entries, and that is the whole allowlist.** A dedupe key is not a
#: place to be inventive: the unique constraint is
#: `(tenant_id, conversation_id, aggregate_generation, dedupe_key)`, so two rules
#: whose templates render alike silently become one row -- and the writer's
#: `ON CONFLICT ... DO UPDATE SET due_at = GREATEST(...)` makes that worse than
#: absorption: **the second one pushes the first one's timer later.**
#:
#: **`commitment_id` is the third, added in 11d** (user ruling 2026-09-16), and
#: it is what the batch is for. `commitment_due_at` is a per-commitment moment,
#: so two customer promises in one frame render `{emit_kind}:{target_frame_id}`
#: identically -- the second promise would not get a timer, it would move the
#: first promise's deadline to whichever is later and overwrite its payload.
#: The earlier deadline then silently never fires, which is the exact shape
#: `CLAUDE.md` third section is about.
#:
#: The alternative considered and rejected: have the producer append
#: `:<commitment_id>` for commitment-anchored kinds while leaving the template
#: pinned. That makes the real key something other than what the profile says
#: it is -- "change the spelling and it disappears", which this repo has been
#: bitten by before.
DEDUPE_PLACEHOLDERS = frozenset({"emit_kind", "target_frame_id", "commitment_id"})

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class TimeRuleError(ValueError):
    """A profile asked for something this producer will not do.

    Raised rather than skipped, because every case is a rule the release
    validation should have refused: an unknown placeholder in a dedupe template
    is not a runtime condition, it is a profile that would produce a key nobody
    can predict.
    """


@dataclass(frozen=True, slots=True)
class ScheduledEventDraft:
    """One row this transition intends to write, before it is written."""

    rule_id: str
    kind: str
    due_at: datetime
    dedupe_key: str
    target_frame_id: UUID
    #: The commitment this row hangs off, for the anchors that have one.
    #:
    #: **`None` for the conversation-wide anchors**, and that is not a
    #: placeholder for "not filled in yet": a `silence_check` genuinely has no
    #: commitment. The writer puts this in `scheduled_events.commitment_id`,
    #: which 11d's migration (064) gives a foreign key -- so a wrong value here
    #: is refused by the database rather than stored.
    commitment_id: UUID | None = None


def render_dedupe_key(
    template: str,
    *,
    emit_kind: str,
    target_frame_id: UUID,
    commitment_id: UUID | None = None,
) -> str:
    """Fill a `dedupe_template` from the allowlist, refusing anything else.

    Not `str.format`: that resolves whatever the template names against
    whatever the caller happens to pass, so a typo becomes a `KeyError` at best
    and a wrong-but-plausible key at worst. The placeholders are matched
    explicitly and an unlisted one is an error naming itself.

    **A template naming `{commitment_id}` with no commitment to name is an
    error, not an empty segment.** Rendering it to `""` would produce a key that
    looks right (`customer_promise_due:<frame>:`) and collides with every other
    commitment in the frame -- which is the collision this placeholder exists to
    remove, reintroduced by the failure path.
    """

    named = set(_PLACEHOLDER.findall(template))
    unknown = named - DEDUPE_PLACEHOLDERS
    if unknown:
        raise TimeRuleError(
            f"dedupe_template names {sorted(unknown)}, which is not in the allowlist "
            f"{sorted(DEDUPE_PLACEHOLDERS)}"
        )
    if "commitment_id" in named and commitment_id is None:
        raise TimeRuleError(
            f"dedupe_template {template!r} names {{commitment_id}} but no commitment "
            "was supplied; rendering it empty would collide with every other "
            "commitment in this frame"
        )
    values = {
        "emit_kind": emit_kind,
        "target_frame_id": str(target_frame_id),
        "commitment_id": str(commitment_id),
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def active_frame(aggregate: dict[str, Any]) -> dict[str, Any] | None:
    """The frame whose rules apply: the active one, or nothing.

    A stack with no active frame is a real state (closing, closed, every frame
    interrupted), and it means no rules fire rather than "use the top of the
    stack anyway" -- a suspended frame's `entered_at` would still be there and
    would still produce a plausible-looking `due_at`.
    """

    for frame in aggregate.get("target_stack", []):
        if frame.get("status") == "active":
            return dict(frame)
    return None


def _frame_definition(profile: dict[str, Any], frame: dict[str, Any]) -> dict[str, Any] | None:
    for definition in profile.get("target_frames", []):
        if str(definition.get("id")) == str(frame.get("definition_id")):
            return dict(definition)
    return None


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _when_satisfied(
    when: dict[str, Any],
    *,
    frame: dict[str, Any],
    aggregate: dict[str, Any],
    now: datetime,
) -> bool:
    """Is this rule's creation condition true right now?

    All four selectors are `null`-able and absent means "no condition", so an
    empty `when` (which is what both shipped profiles carry) admits everything.

    **`now` here is this process's clock, and the two timestamps it is compared
    against were written by the database's** (independent review 2026-09-09,
    P3-1). That is the subtraction across two clocks T183 was registered for --
    kept rather than fixed because the offset measured on this machine (T186:
    δ = 0.43s) is orders of magnitude below any threshold a rule would sensibly
    declare, and because no shipped profile carries a `when` at all today.
    **What it does mean is that the module's reproducibility claim covers
    `due_at`, not this function**: `due_at` is arithmetic on values the caller
    supplies, while a `when` evaluated at two different instants can legitimately
    answer differently.
    A condition this milestone cannot evaluate refuses rather than admits:
    `business_event_type` has no input here, and a rule gated on it must not
    fire on every ordinary turn merely because nothing contradicted it.
    """

    active_for = when.get("target_active_for_seconds")
    if active_for is not None:
        entered_at = _parse(frame.get("entered_at"))
        if entered_at is None or (now - entered_at).total_seconds() < float(active_for):
            return False

    quiet_for = when.get("no_customer_reply_for_seconds")
    if quiet_for is not None:
        last_inbound_at = _parse(aggregate.get("last_inbound_at"))
        # No inbound at all counts as "no reply for longer than any threshold":
        # the customer has never replied, which is the condition, not a missing
        # measurement.
        if last_inbound_at is not None and (
            (now - last_inbound_at).total_seconds() < float(quiet_for)
        ):
            return False

    pending_slot = when.get("pending_slot")
    if pending_slot is not None:
        slots = frame.get("slots") or {}
        if slots.get(str(pending_slot)) is not None:
            return False

    if when.get("business_event_type") is not None:
        return False

    return True


def drafts_for_anchor(
    *,
    profile: dict[str, Any],
    aggregate: dict[str, Any],
    anchor: str,
    anchor_at: datetime,
    now: datetime,
    commitment_id: UUID | None = None,
) -> list[ScheduledEventDraft]:
    """Every row the active frame's rules want, for the one anchor just established.

    `anchor_at` is passed in rather than read from the aggregate because the
    caller is the only one who knows it is *settled*: the dispatcher has just
    written `last_outbound_at` and reads it back from the same statement, so the
    value used here is the value in the row, on the database's clock. Deriving
    it here from a copy of the aggregate would reintroduce the two-clock
    subtraction T183 was registered for.

    Rules for other anchors are not this call's business, which is why the
    anchor is an argument: a transition establishing `frame_entered_at` must not
    also materialise the `last_outbound_at` rules against a stale outbound time.
    """

    if anchor not in SCHEDULABLE_ANCHORS:
        raise TimeRuleError(f"{anchor} is not schedulable in this milestone")
    # **Both directions are errors**, and the second one is the one worth having:
    # passing a commitment to a conversation-wide anchor would write a
    # `silence_check` row pointing at a commitment it has nothing to do with,
    # and 064's foreign key would happily accept it -- a real row, a real
    # reference, the wrong meaning.
    if anchor in PER_ROW_ANCHORS and commitment_id is None:
        raise TimeRuleError(f"{anchor} is per-commitment; no commitment was supplied")
    if anchor not in PER_ROW_ANCHORS and commitment_id is not None:
        raise TimeRuleError(f"{anchor} is conversation-wide; a commitment has no place in it")
    frame = active_frame(aggregate)
    if frame is None:
        return []
    definition = _frame_definition(profile, frame)
    if definition is None:
        return []

    drafts: list[ScheduledEventDraft] = []
    seen: dict[str, str] = {}
    target_frame_id = UUID(str(frame["id"]))
    for rule in definition.get("time_rules", []):
        due = rule.get("due") or {}
        if str(due.get("anchor")) != anchor:
            continue
        when = dict(rule.get("when") or {})
        if not _when_satisfied(when, frame=frame, aggregate=aggregate, now=now):
            continue
        emit_kind = str(rule["emit_kind"])
        template = str(rule["dedupe_template"])
        # **A per-row rule whose template cannot tell two rows apart is skipped
        # here rather than refused**, and the reason is the one this repo keeps
        # relearning: `merchant-profile` is validated against **already stored**
        # releases on read paths, so refusing at runtime would turn a profile
        # published before 11d into a 500 every turn (T338 / T349, and the
        # `outcome_rules` narrowing that needed a pre-063 sweep of its own).
        #
        # Skipping is silent, so it does not stand alone: `unschedulable_rules`
        # reports exactly this case and `test_time_rule_producer.py` holds the
        # **shipped** profiles to declaring none of it, which is where "silently
        # skipped" is stopped from becoming "silently never".
        if anchor in PER_ROW_ANCHORS and "{commitment_id}" not in template:
            continue
        dedupe_key = render_dedupe_key(
            template,
            emit_kind=emit_kind,
            target_frame_id=target_frame_id,
            commitment_id=commitment_id,
        )
        # **Two rules that render the same key are refused, not merged**
        # (independent review 2026-09-09, P3-2). The template the schema pins
        # carries no rule id, so a frame declaring two `silence_check` rules --
        # different offsets, different `when` -- produces one key twice, and the
        # unique constraint would absorb the second silently. That is the same
        # shape as an unresolvable anchor, which this module already refuses to
        # let pass unnoticed; the two risks now get the same treatment.
        if dedupe_key in seen:
            raise TimeRuleError(
                f"rules {seen[dedupe_key]!r} and {str(rule['id'])!r} both render "
                f"dedupe_key {dedupe_key!r}; the second would be absorbed by the "
                "unique constraint rather than rejected"
            )
        seen[dedupe_key] = str(rule["id"])
        drafts.append(
            ScheduledEventDraft(
                rule_id=str(rule["id"]),
                kind=emit_kind,
                due_at=anchor_at + timedelta(seconds=int(due["offset_seconds"])),
                dedupe_key=dedupe_key,
                target_frame_id=target_frame_id,
                commitment_id=commitment_id,
            )
        )
    return drafts


#: **Empty since 11d, and empty on purpose -- it used to hold two.**
#:
#: `customer_promise_due` and `ai_commitment_due` were the kinds ADR-0100 moved
#: out of M1b, because both hang off `commitment_due_at` and no ledger existed.
#: Migration 063 built the ledger and this batch wired the anchor, so **a rule
#: of either kind being skipped is no longer the expected state -- it is a
#: defect**, which is the whole point of emptying this rather than deleting it.
#:
#: **It is kept as an empty set rather than removed** for one reason: the
#: callers that consult it are the ones that decide whether a skipped rule is
#: expected. Deleting the name would make each of them drop the question
#: silently; an empty set makes them all answer "nothing here is expected to be
#: skipped", and `test_time_rule_producer.py` pins the emptiness so that
#: quietly putting a kind back needs an argument.
#:
#: **What this is not**: a claim that every anchor resolves. `business_event_at`
#: still has no producer -- but that is an *anchor* with no value, which
#: `unschedulable_rules` reports by anchor. The distinction matters because the
#: two were conflated before: a kind was deferred to stand in for an anchor that
#: could not resolve.
DEFERRED_KINDS: frozenset[str] = frozenset()


def unschedulable_rules(profile: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Rules this milestone would silently never create, as `(rule_id, kind, anchor)`.

    Exposed so a test can hold the **shipped** profiles to declaring nothing of
    the sort. Skipping an unresolvable anchor is right at runtime; shipping a
    rule that depends on one is a promise nothing will keep, and the difference
    between those two has to be checked somewhere other than at runtime, where
    the skip is silent on purpose.

    **The caller has to decide what is expected**, and this returns the kind so
    it can: a `customer_promise_due` anchored on `commitment_due_at` is
    ADR-0100's deferral working as intended, while a `silence_check` anchored on
    the same thing would be a rule nobody will ever get.
    """

    found: list[tuple[str, str, str]] = []
    for definition in profile.get("target_frames", []):
        for rule in definition.get("time_rules", []):
            anchor = str((rule.get("due") or {}).get("anchor"))
            if anchor not in SCHEDULABLE_ANCHORS:
                found.append((str(rule.get("id")), str(rule.get("emit_kind")), anchor))
                continue
            # **The second way a rule never gets created**, added in 11d: the
            # anchor resolves fine, but the template cannot tell two of the
            # anchor's rows apart, so the producer skips it (see there). Both
            # ways end in "a rule nobody will ever get", so both are reported
            # here -- the anchor column says which one it was.
            if anchor in PER_ROW_ANCHORS and "{commitment_id}" not in str(
                rule.get("dedupe_template") or ""
            ):
                found.append(
                    (
                        str(rule.get("id")),
                        str(rule.get("emit_kind")),
                        f"{anchor} (dedupe_template names no {{commitment_id}})",
                    )
                )
    return found
