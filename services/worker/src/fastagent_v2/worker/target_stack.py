from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastagent_v2.runtime.topic_vocabulary import topic_vocabulary
from fastagent_v2.worker.platform_frames import (
    NO_GOAL_FRAME_DEFINITION,
    NO_GOAL_FRAME_DEFINITION_ID,
    frame_definitions,
    is_platform_frame,
    materials_for_frame,
)


class TargetStackTransitionError(RuntimeError):
    pass


#: Frame statuses from which some target-stack operation is still possible.
#: `completed` and `abandoned` are terminal for the frame that holds them.
_WORKABLE_FRAME_STATUSES = frozenset(
    {"active", "suspended", "repair_pending", "resume_confirmation_pending"}
)

#: Frame statuses that are final for that frame. A terminal frame is a record of
#: something that already happened; no transition may move it back into play.
#:
#: **Public since B4-4**, because the timer consumption check needs the same
#: answer (`pipeline._frame_is_gone`, T226's `frame_exit`). Spelling it a second
#: time there was the first version of that check, and it was wrong in a way a
#: second copy is characteristically wrong: it asked `status != "active"`, which
#: made a **takeover** -- which suspends every resumable frame -- look like a
#: frame that had exited. `suspended` is a live state; the conversation resumes
#: and the frame comes back.
TERMINAL_FRAME_STATUSES = frozenset({"completed", "abandoned"})

#: The interruption relations that carry a customer exit signal -- "不用了",
#: "别再问", "转人工". RFC §4.2 (`conversation-engine.md:317`) makes them
#: the one input that stops a return to the interrupted goal from being
#: proposed, and `abandon` is the operation they justify (validator:
#: `ABANDON_WITHOUT_EXIT_SIGNAL`).
#:
#: Defined here rather than beside `PUSH_RELATIONS` because the transition layer
#: cannot import the validator -- the validator imports this module. One
#: definition, two readers; a second copy is how the two layers drift apart.
EXIT_SIGNAL_RELATIONS = frozenset({"exit", "human_request"})


def _definition(profile: dict[str, Any], definition_id: str) -> dict[str, Any]:
    definition: dict[str, Any] | None = frame_definitions(profile).get(definition_id)
    if definition is None:
        raise TargetStackTransitionError(f"unknown target frame definition {definition_id!r}")
    return definition


def _stamp(frame: dict[str, Any], *, occurred_at: str) -> None:
    """Record that this frame was worked on. **It does not touch `frame_generation`.**

    **ADR-0098 §二 (Q19), landing in B4-3.** This function used to write both
    fields, which made `frame_generation` a *last-touched* counter. The ADR
    defines it as a **creation pin**: written once when the frame is created and
    never again, so that it answers "which incarnation of this frame is this
    timer for" rather than "has anything happened lately".

    **The reasoning is that the other question already has two answers.**
    `scheduled-event.schema.json` requires both `aggregate_generation` and
    `target_stack_version`, and the second of those already carries "has the
    stack changed". A third counter meaning the same thing is not redundancy, it
    is a second definition of the same word -- the shape ADR-0098 rejects by
    name ("一个字段两种含义，正是 T82 那条编号冲突的同型").

    **The `generation` parameter is gone** (independent review 2026-09-09, P3-2).
    The first version of this change kept it -- unused -- on the argument that
    removing it was wider than the ruling asked for. That holds for the
    signature and not for the callers: `suspend_target_stack_for_human`,
    `resume_target_stack_from_human` and `repair_target_stack_for_repin` each
    computed `int(updated["generation"])` whose only destination was a parameter
    documented as ignored, and the next reader would have taken those three
    lines as evidence the field was still being maintained.

    **`frame_generation` is written at exactly three places, all of them frame
    creation** (P3-1; the earlier wording said "the sites below", which counted
    only this file): `apply_target_stack_operation`'s `push` branch and
    `enter_closing_frame` here, and the root frame in
    `pipeline._initial_aggregate`, which hard-codes `0` beside the aggregate's
    own `"generation": 0`.
    """

    frame["last_progress_at"] = occurred_at


def frames_ever_entered(aggregate: Mapping[str, Any]) -> tuple[str, ...]:
    """**这通对话进过哪几个目标帧** —— 一份不随栈增减的记录（ADR-0146）。

    ⚠️ **不能从 `target_stack` 读。** `request_pop` 会把子帧**真的弹掉**
    （本文件那一句 `stack.pop()`），所以一个正常完成并返回的帧
    **在栈里一点痕迹都不留**。

    实测（2026-09-21）：`introduce` 推进到 `quote` → 报价 → `complete` →
    `repair` → `request_pop` 之后栈只剩 `introduce(active)`，
    而平台**立刻又要推一次 `quote`** —— 每一轮给客户重报一次价，停不下来。

    **存量聚合没有这一格**，所以退回读栈：那是本条落地之前的行为，
    比凭空当成「一个都没进过」强 —— 后者会让老对话被重推一遍。
    """

    recorded = aggregate.get("frames_entered")
    if isinstance(recorded, list):
        return tuple(str(entry) for entry in recorded)
    return tuple(str(frame["definition_id"]) for frame in aggregate.get("target_stack") or [])


#: `frames_entered` 最多留几笔。
#:
#: **与 `conversation-aggregate.schema.json` 的 `frames_entered.maxItems`
#: 是同一个数**，`test_a_list_the_customer_can_overflow.py` 把两处钉成相等。
#:
#: ⚠️ **这一格必须有人在写入侧执行，光写在契约里不够。** 2026-09-22 实测：
#: `_record_entry` 原来是裸的 `history.append(...)`，而契约那边写着
#: `maxItems: 64` —— 第 65 次进帧时 `validate_contract` 在 prepare 的写路径上
#: 抛 `is too long`，**而这个数组声明「永不删除」，没有任何东西能把它缩短**。
#: 那通对话从此每一轮都炸，商家改配包救不了（数组在聚合上不在配包上）。
#:
#: **而长到 65 由客户决定**：`objection` 是 `reenter`，他提 63 次疑虑就到了。
#: 契约那段描述当时写着「一通对话进过 64 个帧，那已经不是『聊回旧话题』」——
#: **`reenter` 恰恰就是聊回旧话题，并且它计数**，那句假设写在 `revisit` 之前。
FRAMES_ENTERED_CAP = 64


def _index_to_forget(history: Sequence[str]) -> int:
    """到顶之后让哪一笔出局 —— **最旧的那笔「别处还有」的记录**。

    判据是**这一笔丢掉之后，那个 `definition_id` 还在不在表里**：
    只要它在后面还出现过，丢掉它不改变任何一个读者的答案
    （三个读者问的都是「进过没有」，见 `frames_ever_entered`）。
    丢掉的是「进过第几次」这一层精度，**保住的是「进过没有」**。

    ⚠️ **`return 0` 那一支今天到不了，而它是有意写成不抛的。**
    到得了它，意味着表里 64 笔互不相同又来了第 65 个不同的帧；
    而配包的 `target_frames` 有 `maxItems`，值正是
    `FRAMES_ENTERED_CAP - len(PLATFORM_FRAME_DEFINITIONS)`
    （`test_a_list_the_customer_can_overflow.py` 按这条算式核，
    **新加一个平台帧而不下调配包那格，它就红**）。
    真到了那里也**不能抛** —— 抛在写路径上，就是本条要修的那场故障本身。
    """

    seen = Counter(history)
    for index, entry in enumerate(history):
        if seen[entry] > 1:
            return index
    return 0


#: 槽级声明「重进这个帧时这个值还算不算数」的那两个取值（ADR-0150）。
#: **与 `merchant-profile.schema.json` 的 `information_slots[].on_revisit`
#: 是同一张词表**，`test_a_second_visit_is_not_the_first.py` 把两处钉成相等。
#:
#: 缺省是 `RESET` —— **那是本条落地之前的行为**，三个在架配包一行不用改。
CARRY_ON_REVISIT = "carry"
RESET_ON_REVISIT = "reset"


def _slots_that_carry(profile: dict[str, Any], definition_id: str) -> frozenset[str]:
    """这个帧里，商家声明了「下次还算数」的是哪几个槽。

    ⚠️ **平台帧没有配包定义**（`platform:` 前缀，`_definition` 会抛），
    所以先问它一句 —— 平台帧的槽是平台自己的，不参与本条。
    """

    if is_platform_frame(definition_id):
        return frozenset()
    try:
        definition = _definition(profile, definition_id)
    except TargetStackTransitionError:
        #: 配包换过、旧帧的定义已经不在了。**这时候什么都不带**：
        #: 「带不带」这个问题的答案只在配包里，而它现在没有答案 ——
        #: fail-closed 到本条落地之前的行为，而不是猜一个。
        return frozenset()
    return frozenset(
        str(slot["name"])
        for slot in definition.get("information_slots") or []
        if str(slot.get("on_revisit") or RESET_ON_REVISIT) == CARRY_ON_REVISIT
    )


def _archive_carried_slots(
    aggregate: dict[str, Any], profile: dict[str, Any], frame: Mapping[str, Any]
) -> None:
    """这个帧要离开栈了 —— 把它 `carry` 的那几个槽**连证据一起**留下来。

    `request_pop` 那一句 `stack.pop()` 把帧连它的槽一起丢掉，
    **在此之前没有任何地方归档**（2026-09-22 实跑：一次异议往返之后
    `concern_answered` 的值在聚合里一点不剩）。

    **只存声明了 `carry` 的那几个** —— 商家说了不带的值不该落进存储。
    种回去的时候还会按**当时**的配包再核一次（`_slots_that_carry` 在
    `_seeded_slots` 里再调一遍），那一头让商家改了声明之后立刻生效。
    """

    carried = _slots_that_carry(profile, str(frame["definition_id"]))
    if not carried:
        return
    slots = {
        name: value for name, value in (frame.get("slots") or {}).items() if name in carried
    }
    if not slots:
        return
    evidence = {
        name: value
        for name, value in (frame.get("slot_evidence") or {}).items()
        if name in slots
    }
    store = aggregate.get("carried_slots")
    if not isinstance(store, dict):
        store = {}
    #: **整笔替换，不合并。** 这一次进去填的就是这个帧当时知道的全部；
    #: 与上一次的值合并会造出一个**没有任何一次访问真的处于过**的状态。
    entry: dict[str, Any] = {"slots": slots}
    if evidence:
        entry["slot_evidence"] = evidence
    store[str(frame["definition_id"])] = entry
    aggregate["carried_slots"] = store


def _seeded_slots(
    aggregate: Mapping[str, Any], profile: dict[str, Any], definition_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """新帧开张时手上有什么 —— 上一次带过来的那几个槽，或者两个空字典。

    **构造新帧的地方有三处**（模型 `push`、平台推进、收尾帧），
    而「带什么过去」是一条规则 —— 所以它只有这一个定义点。
    本仓反复踩的那一族：*同一条规则两个定义点，两处先后分家而没有东西变红*。

    ⚠️ **按当时的配包再核一次 `carry`**：存的时候滤过一遍，这里再滤一遍。
    两头都滤不是冗余 —— 商家把一个槽从 `carry` 改回 `reset` 之后，
    **存量 `carried_slots` 里那一笔立刻不再生效**，不用等它被覆盖。
    """

    store = aggregate.get("carried_slots")
    entry = store.get(definition_id) if isinstance(store, dict) else None
    if not isinstance(entry, dict):
        return {}, {}
    carried = _slots_that_carry(profile, definition_id)
    slots = {
        name: value for name, value in (entry.get("slots") or {}).items() if name in carried
    }
    evidence = {
        name: value
        for name, value in (entry.get("slot_evidence") or {}).items()
        if name in slots
    }
    return slots, evidence


def _record_entry(aggregate: dict[str, Any], definition_id: str) -> None:
    """进了一个帧就记一笔。

    **「进过哪些帧」只增不删** —— 那正是它与栈的区别。
    **而「进过几次」在到顶之后会让位**：超过 `FRAMES_ENTERED_CAP` 时，
    按 `_index_to_forget` 丢掉最旧的那笔重复记录，**每个进过的
    `definition_id` 都还留在表里**。
    """

    history = aggregate.get("frames_entered")
    if not isinstance(history, list):
        history = [str(frame["definition_id"]) for frame in aggregate.get("target_stack") or []]
    history.append(definition_id)
    while len(history) > FRAMES_ENTERED_CAP:
        del history[_index_to_forget(history)]
    aggregate["frames_entered"] = history


def apply_target_stack_operation(
    *,
    aggregate: dict[str, Any],
    operation: dict[str, Any],
    interruption_relation: str,
    profile: dict[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    """Apply one already-validated target-stack proposal to a copied aggregate."""

    updated = deepcopy(aggregate)
    stack = updated["target_stack"]
    if not stack:
        raise TargetStackTransitionError("target stack is empty")
    generation = int(updated["generation"])
    op = str(operation["op"])
    current = stack[-1]

    if op == "keep":
        return updated

    if op == "push":
        definition_id = str(operation["target_frame_definition_id"])
        definition = _definition(profile, definition_id)
        # **镜像 `inference_validation` 的 `FRAME_ALREADY_ENTERED`**（ADR-0146）。
        # N1 先例：两处或都不做 —— 验证器先跑，所以只住在这里的规则会穿过
        # 重试预算，而不是落在 no-send 路径上带一个 reason code。
        if str(definition.get("revisit") or "reenter") == "brief_answer" and (
            definition_id in frames_ever_entered(updated)
        ):
            raise TargetStackTransitionError(
                f"{definition_id!r} is declared `once` and has already been entered"
            )
        current["status"] = "suspended"
        _stamp(current, occurred_at=occurred_at)
        seeded_slots, seeded_evidence = _seeded_slots(updated, profile, definition_id)
        stack.append(
            {
                "id": str(uuid4()),
                "definition_id": definition_id,
                "goal": str(definition["goal"]),
                "status": "active",
                "slots": seeded_slots,
                "slot_evidence": seeded_evidence,
                "entered_at": occurred_at,
                "last_progress_at": occurred_at,
                "parent_frame_id": str(current["id"]),
                "interruption_reason": interruption_relation,
                "resume_confirmation_policy": str(
                    definition["return_confirmation"]["policy"]
                ),
                "frame_generation": generation,
            }
        )
        _record_entry(updated, definition_id)
        return updated

    if op in {"complete", "abandon"}:
        # **A platform frame has no goal, so it cannot be completed** (B4-7,
        # ADR-0099 §五.2 -- the lifecycle question). `complete`'s exit gate is
        # "are the frame's required slots filled", and the no-goal frame
        # declares none, so that gate is vacuously satisfied: without this the
        # model could mark it `completed`, the whole stack would go terminal
        # again, and the conversation would land on exactly the handoff this
        # batch exists to remove.
        #
        # `abandon` is **not** refused. It requires an exit signal
        # (`ABANDON_WITHOUT_EXIT_SIGNAL`), and a customer who says "别再发了"
        # after the goal is met is asking for something real; ADR-0099 §五.3
        # keeps the abandonment road separate, and this is where the two part.
        #
        # Mirrored in `inference_validation._validate_stack_operation` -- both
        # places or neither, the N1 precedent: the validator runs first, so a
        # rule living only here would raise through the retry budget instead of
        # landing on the no-send path with a reason code.
        if op == "complete" and is_platform_frame(current["definition_id"]):
            raise TargetStackTransitionError(
                f"a platform frame has no goal to complete: {current['definition_id']!r}"
            )
        # `repair` used to share this branch, which made it a synonym for
        # `complete` -- and the validator checks the two at different strengths
        # (`complete` must satisfy the frame's required slots, `repair` only
        # needs a parent). A model could therefore mark an unsatisfied goal
        # `completed` just by renaming the op. `repair` now advances the parent
        # and never touches the child, so that route is closed by construction.
        current["status"] = "abandoned" if op == "abandon" else "completed"
        _stamp(current, occurred_at=occurred_at)
        if len(stack) > 1 and stack[-2]["status"] == "suspended":
            # Only a *suspended* parent is waiting to be resumed. `push` is the
            # one operation that leaves one, which is why this used to promote
            # unconditionally -- and why the closing frame broke it: that frame
            # is pushed onto a parent which is already `completed`.
            #
            # Promoting a terminal parent meant the conversation could never
            # finish. Completing the closing frame turned the finished root back
            # into `repair_pending`, so `stack_requires_human_disposition` stopped
            # returning `ROOT_TARGET_FRAME_COMPLETED` -- no handoff, no second
            # closing frame, the machine still holding a conversation whose goal
            # was already met. Worse, two more legal plans (`repair`, then
            # `request_pop`) reopened the completed root as `active` with its
            # slots intact, ready to be completed again (independent review
            # 2026-09-03, P0-1, reproduced with the real transition functions).
            #
            # With this guard `[root completed, closing completed]` is wholly
            # terminal, which is exactly the terminus ADR-0031 asked this batch
            # to define: the conversation goes to a person.
            parent = stack[-2]
            parent["status"] = "repair_pending"
            _stamp(parent, occurred_at=occurred_at)
        return updated

    if op == "repair":
        # RFC §4.2 state table (conversation-engine.md:306-315); this
        # transition is the row at `:311`:
        #   repair_pending + goal still valid and confirmation needed
        #     -> resume_confirmation_pending
        # (The section and line numbers were wrong here -- §4.3 is
        # `UnderstandingResult`, and `:306-308` is the table's header. Corrected
        # per the 2026-09-03 independent review, P2-1.)
        # It is the state half of `confirm_resume`; sending the confirmation is
        # M1b. Without this transition the middle cell of the state table has no
        # writer anywhere in the repository.
        if len(stack) < 2:
            raise TargetStackTransitionError("repair requires a parent frame")
        parent = stack[-2]
        if parent["status"] != "repair_pending":
            raise TargetStackTransitionError(
                f"repair expects a repair_pending parent, found {parent['status']!r}"
            )
        # N1. The write side used to ignore `resume_confirmation_policy` while
        # the read side (`request_pop`, below) honoured it, so a `forbidden`
        # child still drove the parent into `resume_confirmation_pending` -- the
        # one state whose only purpose is to send the very message the profile
        # forbids. Read and write now ask the same question of the same field.
        #
        # `forbidden` reads as "return without asking", not "never return":
        # `request_pop` has never gated on anything but `required`, so such a
        # child still leaves the stack directly. Refusing here stranded nothing.
        #
        # M1a sends no message, so today this is only a state bit. That is why
        # it is worth fixing now rather than after M1b makes it a real one.
        child = stack[-1]
        # T53 (supersedes T1). RFC `:317`: "客户说「不用了 / 别再问 / 转人工」
        # 等退出信号时 ProactivityPolicy 拒绝回归". `resume_confirmation_pending`
        # is the state that proposes the return, so entering it right after the
        # customer refused is that sentence inverted: the machine answers "don't
        # ask me again" by preparing to ask again.
        #
        # Two readings of "the customer refused", because the signal arrives on
        # one turn while the stack outlives it:
        #
        #   * an `abandoned` child -- durable. Two operations write that status
        #     and **both** require an exit signal: `abandon`, which the validator
        #     lets through only on an exit relation
        #     (`ABANDON_WITHOUT_EXIT_SIGNAL`), and `request_pop` below, which
        #     writes it onto the parent only when `exit_signal` is true. So an
        #     abandoned frame *is* the record of an exit signal, still readable
        #     many turns later. (The first draft of this comment said `abandon`
        #     was the only writer -- false in the same batch that wrote it, and
        #     it was the whole justification for the durable criterion.
        #     Independent review 2026-09-04, P2-1. The implication survives; the
        #     sentence supporting it did not.)
        #   * this turn's `interruption_relation` -- for the child that ended
        #     normally, with the exit arriving on the turn `repair` is proposed.
        #
        # Neither is `interruption_reason`, which is what the withdrawn T1 keyed
        # on. `push` is that field's only writer and the validator forbids
        # `push` on an exit relation (`PUSH_WITHOUT_INTERRUPTION`), so it can
        # never hold one in production: the rule was unreachable and its tests
        # green against a state the pipeline cannot build (independent review
        # 2026-09-03, P0-1). Both criteria here are produced by the real
        # transitions, and the tests reach them through `push` -> `abandon`.
        if child["status"] == "abandoned" or interruption_relation in EXIT_SIGNAL_RELATIONS:
            raise TargetStackTransitionError(
                "repair cannot propose a return after an exit signal"
            )
        if str(child.get("resume_confirmation_policy", "optional")) == "forbidden":
            raise TargetStackTransitionError(
                "repair cannot propose a return: the child frame's profile "
                "forbids resume confirmation"
            )
        parent["status"] = "resume_confirmation_pending"
        _stamp(parent, occurred_at=occurred_at)
        return updated

    if op == "request_pop":
        if len(stack) < 2:
            raise TargetStackTransitionError("request_pop requires a parent frame")
        child = stack[-1]
        if child["status"] not in TERMINAL_FRAME_STATUSES:
            raise TargetStackTransitionError(
                f"request_pop cannot remove child in status {child['status']!r}"
            )
        parent = stack[-2]
        # The interruption's own frame declares whether returning to the parent
        # needs a confirmation first. `required` means the parent must already
        # have been advanced to resume_confirmation_pending by `repair`; popping
        # straight from repair_pending would return to the original goal without
        # ever confirming, which is the gate RFC §4.2 (`:311`) puts in this path.
        policy = str(child.get("resume_confirmation_policy", "optional"))
        # The customer's refusal, read the same two ways as in `repair` above:
        # the child's durable status, and the relation carried by this turn.
        exit_signal = (
            child["status"] == "abandoned" or interruption_relation in EXIT_SIGNAL_RELATIONS
        )
        # T53, the second half. The gate above asks for a confirmed return, and
        # `repair` is the only way to get one -- which the exit-signal rule now
        # refuses. Keeping the gate on an `abandoned` child would therefore
        # strand it: neither `repair` nor `request_pop` would ever be legal
        # again, and every later turn would find the same stuck stack.
        #
        # Backlog item 3 stated the defect as a property -- `abandoned` and
        # `completed` were equivalent in front of this gate. They are not. A
        # completed child leaves the parent's goal untouched, so the profile may
        # insist on asking before returning to it; an abandoned child means the
        # customer already said no, and RFC `:313` routes that case around the
        # confirmation entirely (`repair_pending` + customer exit ->
        # abandon/complete the parent -> next goal/closed).
        if (
            policy == "required"
            and not exit_signal
            and parent["status"] != "resume_confirmation_pending"
        ):
            raise TargetStackTransitionError(
                "request_pop needs a confirmed return: parent is "
                f"{parent['status']!r} while the child requires confirmation"
            )
        #: ⚠️ **在 `pop` 之前，不是之后**（ADR-0150）。这一句之后那个帧
        #: 连同它填过的每一个槽就不存在了 —— 本条要留下来的东西
        #: **只在这一行之前存在**。
        _archive_carried_slots(updated, profile, child)
        stack.pop()
        if parent["status"] in TERMINAL_FRAME_STATUSES:
            # A terminal parent keeps its status. `complete` no longer promotes
            # one (see that branch), but `request_pop` reached the same place by
            # a different road: popping the closing frame off a `completed` root
            # used to hand the root back as `active`, with its slots intact and
            # ready to be completed again. Leaving it terminal makes the whole
            # stack terminal, which is what sends the conversation to a person.
            pass
        elif exit_signal:
            # T54, the other half of RFC `:317`. Returning the parent to
            # `active` after the customer said "不用了 / 别再问 / 转人工" hands
            # the engine back the very goal that was just refused -- and under
            # M1b's active window, `active` is a licence to advance it.
            #
            # The state table gives the answer directly rather than leaving it
            # to the planner: RFC `:313` reads "repair_pending + 原目标出口已满足
            # /客户退出 -> abandon/complete parent -> next goal/closed", and the
            # mandatory scenario at `:751` names the end state -- "客户拒绝回归:
            # exit signal deny，原目标 abandoned/suspended".
            #
            # Of those two, `abandoned`. `suspended` is the state a *human*
            # takeover leaves behind, and it is not terminal, so
            # `stack_requires_human_disposition` reports nothing to do while
            # `keep` (the safe fallback's own operation) demands an active
            # frame: every later turn would be rejected, including the fallback,
            # and the rejection would leave the pipeline via the retry budget.
            # `abandoned` instead gives the frame a guaranteed exit. **At stack
            # depth 2 that means the whole stack is terminal**, which is exactly
            # the "next goal/closed" of the state table: with no next goal left,
            # the conversation goes to a person by the route that already exists
            # (`ROOT_TARGET_FRAME_ABANDONED`).
            #
            # **At depth 3 it does not.** The grandparent is still `suspended`,
            # which `stack_requires_human_disposition` counts as workable, so
            # nobody is notified and the only legal plan is another
            # `request_pop` -- which then abandons the grandparent in turn. The
            # exit signal walks up one frame per turn rather than collapsing the
            # stack at once. Whether it should collapse instead is a semantic
            # question RFC `:751` does not answer ("原目标" names one frame), and
            # widening it was deliberately not done here; registered instead.
            # Pinned by a depth-3 test, because the first version of this
            # comment stated the depth-2 result as though it were general
            # (independent review 2026-09-04, P2-2).
            parent["status"] = "abandoned"
        else:
            parent["status"] = "active"
        _stamp(parent, occurred_at=occurred_at)
        return updated

    raise TargetStackTransitionError(f"unsupported target stack operation {op!r}")


#: RFC 4.3 refuses a **low**-confidence update against **high**-confidence
#: evidence, and the first version of this batch implemented neither adjective:
#: any strictly-lower confidence lost, so 0.85 lost to 0.9. The independent
#: review (2026-09-03, P1-1) showed what that costs -- a customer correcting a
#: value the model read confidently is refused, the stale value stays, the
#: `complete` exit gate still passes because the slot is non-empty, and there is
#: no second way in: `PUSH_ONTO_SAME_FRAME` forbids a sub-frame of the same
#: definition, so this function is the only writer for that slot.
#:
#: These two numbers belong in the profile (RFC 4.1 declares a per-slot
#: confidence threshold), which today's `merchant-profile` contract cannot
#: express -- registered as T28. Until it can, they are platform constants and
#: not a per-slot judgement, which is exactly why they are set far apart: the
#: refusal must fire only where the two readings are not close, and every
#: ordinary disagreement must fall through to last-write-wins. Narrowing a
#: constant later is a one-line change; narrowing an unconditional comparison
#: is a behaviour change nobody can bound. Recorded as ADR-0031.
LOW_CONFIDENCE_CEILING = 0.5
HIGH_CONFIDENCE_FLOOR = 0.8


@dataclass(frozen=True, slots=True)
class SlotWriteback:
    """What one understanding document did to the frame's slots.

    Three values rather than two because a refusal is not the absence of an
    update: RFC 4.3 makes the engine drop a value the customer may well have
    just supplied, and a count of what was *applied* cannot say that happened.
    RFC 18.3 states the principle for the confidence gate as a whole --
    "recommended and applied are recorded separately" -- and it applies here for
    the same reason: a rule that silently discards evidence is a rule nobody can
    audit, calibrate or appeal.
    """

    aggregate: dict[str, Any]
    applied: int
    #: One entry per update refused by RFC 4.3, carrying the slot name, both
    #: confidences and both evidence event ids -- **never the value**. The value
    #: is customer content; the audit trail needs to answer "which slot, from
    #: which message, refused against what" and answering it does not require
    #: copying what the customer said into `audit_events`.
    refusals: tuple[dict[str, Any], ...] = ()


#: 一个槽位最多留几条证据。**与契约的 `maxItems` 写两遍，由用例钉死相等** ——
#: `packages/contracts` 不 import 生产代码，与 `MATERIAL_BYTES_BUDGET` 同一个理由。
EVIDENCE_IDS_MAX = 8


def _evidence_ids(recorded: Mapping[str, Any] | None) -> list[str]:
    """已经记下的那几条证据，**两种形状都读**。

    D3（2026-09-15）把这一项从单值改成数组。**已存的行不追溯改写**
    （T338 那一族），所以读的时候要兼容单数键；**写的时候一律写数组**。
    """

    if not recorded:
        return []
    existing = recorded.get("evidence_event_ids")
    if isinstance(existing, list):
        return [str(item) for item in existing]
    single = recorded.get("evidence_event_id")
    return [str(single)] if single else []


def _latest_evidence(recorded: Mapping[str, Any] | None) -> str | None:
    """**最新的那一条** —— 拒绝留痕里「已记录的证据」指的是它。

    拒绝那几条记的是「我拿这条更新去比的是哪一条已记录的证据」，
    而比的那一条是**最后写进去的**（`confidence` 也取自同一次写入）。
    """

    ids = _evidence_ids(recorded)
    return ids[-1] if ids else None


def _appended_evidence(
    recorded: Mapping[str, Any] | None, event_id: str, *, value_changed: bool
) -> list[str]:
    """这个槽位现在的证据集合（D3，RFC:165）。

    ## 「多条证据」指的是**支撑同一个值**的多条消息，不是一段历史

    这一点是被 `test_a_higher_or_equal_confidence_update_replaces_the_earlier_evidence`
    （带 `@pytest.mark.security`）逼出来的，**而它的论证是对的**：

    > The evidence describes the value that is there now, **not a history of it**.
    > Keeping the first would make provenance point at a message
    > **whose value has since been overwritten** -- an answer that is confidently wrong.

    图纸要数组（一个槽位可以由多条证据支撑）与这条用例**不矛盾** ——
    第一版实现把两者混了：

    * **值变了** -> 旧的那几条消息支撑的是**另一个值**，留着就是错的出处
      -> **重置**成只有这一条；
    * **值没变**（同一个值被第二条消息再次确认）-> **追加** ——
      这才是图纸说的「多条证据支撑一个槽位」。

    **去重**：同一条消息更新同一个槽位两次（比如一次纠正）不该出现两遍。
    **截断保留末尾**：满了之后丢最老的 —— 「最近是怎么被确认的」比
    「最早是怎么来的」更常被问到，而两者都留就不是一个有界的字段了。
    """

    if value_changed:
        return [event_id]
    ids = [item for item in _evidence_ids(recorded) if item != event_id]
    ids.append(event_id)
    return ids[-EVIDENCE_IDS_MAX:]


def apply_slot_updates(
    *,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    slot_updates: list[dict[str, Any]],
    recorded_at: str,
    is_correction: bool = False,
) -> SlotWriteback:
    """Write the understanding step's slot findings onto the frames they name.

    `slot_updates` has been part of the understanding contract from the start and
    **nothing ever wrote it anywhere** (registered as T6). The complete gate
    (then `_missing_required_slots`, since 2026-09-24 `_unmet_exit_conditions`)
    reads `frame["slots"]`, so with no writer the `complete` exit-condition gate
    was unreachable in the real pipeline: a goal could never be satisfied, only
    abandoned. Every `complete` in the test suite got there by seeding the slots
    directly (`tests/e2e/test_m1a_no_send.py::_fill_required_slots`).

    Four rules, all restrictive on purpose:

    * an update naming a frame that is not on this stack is **ignored**, not
      applied to the nearest frame -- the model does not get to redirect a fact
      onto a goal it did not name;
    * an update naming a terminal frame is **ignored** -- a completed goal's
      slots are the evidence of how it was satisfied, and backdating them
      rewrites that;
    * an update naming a slot the frame's definition does not declare is
      **ignored**. The plan side already enforces this (`SLOT_NOT_IN_FRAME` on
      `required_slot_requests`); the write side did not, so a model could put
      arbitrary keys into a persisted aggregate, 32 per turn, kept forever
      (independent review 2026-09-03, P1-2). This module's first rule is that
      the platform decides what is legitimate and never takes the model's word
      for it; writeback was the exception;
    * a `null` value is **ignored** rather than treated as an erasure. The exit
      gate counts `None` and `""` as absent, so honouring a `null` would let one
      understanding pass silently un-satisfy a goal that was already met. "I did
      not find a value" and "the value is now gone" are different claims and the
      contract cannot tell them apart.

    Each applied update also records its provenance in `frame["slot_evidence"]`
    (T25), and provenance is what makes RFC 4.3 enforceable: a *low*-confidence
    update does not overwrite *high*-confidence evidence, where both adjectives
    are the thresholds above rather than a bare comparison, and an update the
    understanding step marked as a `correction` is never refused -- a customer
    correcting themselves is the one case where the newer reading wins by
    definition, however hesitantly it was read. A refused update leaves no
    trace on the frame -- the understanding document that proposed it is not
    persisted anywhere either, which is the other half of T25 and stays
    registered. The understanding step has always reported `confidence` and
    `evidence_event_id`; the first version of this function dropped both, which
    left a 0.1-confidence slot and a 0.99 one indistinguishable at the `complete`
    exit gate -- the single judgement of whether a goal was actually met -- and
    left nobody able to answer which message a slot came from. Recording it does
    not itself gate anything: `confidence_gate_policy_ref` has no consumer
    anywhere in the repository, and inventing a threshold here would be defining
    policy in the wrong place. This makes the gate *possible*; building it is
    registered separately.

    Returns a `SlotWriteback`: the updated aggregate, how many updates were
    applied, and which ones RFC 4.3 refused -- the third so the caller can
    record the refusal rather than leave it as a smaller number.
    """

    definitions = frame_definitions(profile)
    writable = {}
    updated = deepcopy(aggregate)
    for frame in updated["target_stack"]:
        if frame["status"] not in _WORKABLE_FRAME_STATUSES:
            continue
        definition = definitions.get(str(frame["definition_id"]))
        if definition is None:
            continue
        declared = {str(slot["name"]) for slot in definition["information_slots"]}
        writable[str(frame["id"])] = (frame, declared, topic_vocabulary(definition))
    applied = 0
    refusals: list[dict[str, Any]] = []
    for update in slot_updates:
        entry = writable.get(str(update["frame_id"]))
        if entry is None:
            continue
        frame, declared, topic_slot_vocabulary = entry
        name = str(update["slot"])
        if name not in declared or update["value"] is None:
            continue
        # **第五条丢弃规则：话题槽的值不在词表里（C-1，用户 2026-09-15 裁定）。**
        #
        # 规则本身写在 `merchant-profile.schema.json` 的 `material_topic_slot` 与
        # `runtime.topic_vocabulary.TOPIC_SLOT_KEY` 上：**一个话题槽的合法取值 = 有素材
        # 对应的话题**。没有素材对应的话题，模型写进去也没有意义 ——
        # 它不会让任何东西被给出去，只占字节。
        #
        # **词表不是另立的一张表，是现推的** —— 取本帧 `content_materials[].topics`
        # 的键集。另立一张就是同一个事实写两处，而本仓已经为那种形状付过账。
        #
        # **它拦的是什么**：`on_request` 的匹配是**包含式**，对的是这个槽的整条文本，
        # 所以模型一旦把话题**累积**写进去（演示包的 goal 原来就是这么要求的），
        # 那条串会同时命中多组，素材集合**随对话单调增长、不回落**（T351）。
        # 实测过一次：七个话题都被填过时，一个回合给出去 24 段 / 5,653 字节，
        # 而预算是 5,630。**丢弃一条不合词表的值，就是不让那条串被写成。**
        #
        # **为什么是丢弃而不是截断或改写**：与上面四条同源 ——
        # 截断会得到一个「看起来合法而意思变了」的值，而平台不拥有商家的词表，
        # 没有资格替它挑一个。丢弃之后槽位保持上一轮的值，**那是一个真值**。
        if topic_slot_vocabulary is not None and name == topic_slot_vocabulary[0]:
            if str(update["value"]) not in topic_slot_vocabulary[1]:
                refusals.append(
                    {
                        "frame_id": str(frame["id"]),
                        "slot": name,
                        # 与另两个理由码并列，名字说的是判据不是症状。
                        "reason": "SLOT_VALUE_NOT_IN_TOPIC_VOCABULARY",
                        "proposed_confidence": float(update["confidence"]),
                        "proposed_evidence_event_id": str(update["evidence_event_id"]),
                    }
                )
                continue
        # RFC 4.3: "a low-confidence key slot must not automatically overwrite
        # existing high-confidence evidence." This is the rule that made storing
        # provenance necessary in the first place -- it is a comparison against
        # what is already on the frame, and before T25 there was nothing to
        # compare against, so every update won by being last.
        #
        # A slot with no recorded evidence (written before T25, or never written)
        # cannot be compared and is not protected: refusing an update on the
        # grounds of evidence nobody recorded would be inventing a reason.
        recorded = frame.get("slot_evidence", {}).get(name)
        # **A human-written slot is refused outright, and the order matters more
        # than the rule** (ADR-0054 §三.2, U3).
        #
        # This check sits *before* the confidence rule and reads neither
        # `is_correction` nor either confidence. Putting it inside that rule --
        # or after it -- would make it empty: `is_correction` exempts an update
        # unconditionally, and it comes from `interruption.relation` in the
        # model's own output (`pipeline.py`). Nothing checks that claim. So a
        # defence that honoured it would be handing the bypass to exactly the
        # party it exists to restrain: label the turn a correction, overwrite
        # what the person wrote.
        #
        # `source` absent means `model`, which is what pre-ADR-0054 frames were
        # -- the same reading T25 established for slots with no evidence at all.
        if recorded is not None and str(recorded.get("source", "model")) == "human":
            # Its own refusal reason, not the confidence one. The two lead
            # somewhere different: a confidence refusal may be the customer
            # correcting a confidently-wrong model and is worth revisiting; this
            # one is the system working as designed, and an operator who cannot
            # tell them apart will eventually "fix" this by loosening it.
            #
            # No value, for `SlotWriteback.refusals`' stated reason: it is
            # customer content.
            refusals.append(
                {
                    "frame_id": str(frame["id"]),
                    "slot": name,
                    "reason": "SLOT_WRITTEN_BY_HUMAN",
                    "proposed_confidence": float(update["confidence"]),
                    "proposed_evidence_event_id": str(update["evidence_event_id"]),
                    "recorded_evidence_event_id": _latest_evidence(recorded),
                }
            )
            continue
        if (
            recorded is not None
            and not is_correction
            and float(update["confidence"]) < LOW_CONFIDENCE_CEILING
            and float(recorded["confidence"]) >= HIGH_CONFIDENCE_FLOOR
        ):
            # Refusing is required; refusing silently is not. The refused update
            # may be the customer correcting a value the model was confidently
            # wrong about, and before this the only trace was a smaller
            # `applied` count -- indistinguishable from the model simply not
            # finding anything. The value itself is deliberately not carried:
            # see `SlotWriteback.refusals`.
            refusals.append(
                {
                    "frame_id": str(frame["id"]),
                    "slot": name,
                    # Named rather than inferred from which keys are present:
                    # there are two refusal kinds now and they are handled
                    # differently (ADR-0054 §三.2).
                    "reason": "LOW_CONFIDENCE_OVER_HIGH",
                    "proposed_confidence": float(update["confidence"]),
                    "recorded_confidence": float(recorded["confidence"]),
                    "proposed_evidence_event_id": str(update["evidence_event_id"]),
                    "recorded_evidence_event_id": _latest_evidence(recorded),
                }
            )
            continue
        # **值变没变，决定证据是追加还是重置**（D3）—— 见 `_appended_evidence`。
        # 取在写之前，因为下一行就把它覆盖掉了。
        value_changed = frame["slots"].get(name) != update["value"]
        frame["slots"][name] = update["value"]
        frame.setdefault("slot_evidence", {})[name] = {
            "confidence": float(update["confidence"]),
            "evidence_event_ids": _appended_evidence(
                recorded, str(update["evidence_event_id"]), value_changed=value_changed
            ),
            "recorded_at": recorded_at,
            # **Always `model` here**: this function writes what the
            # understanding step found, and there is no argument by which a
            # caller could claim otherwise. The human writer is `PUT
            # /conversations/{id}/frames/{frame_id}/slots` (freeze B3 #25),
            # which is **not in this batch** -- so the `human` branch above has
            # no production writer yet. That is a wiring gap with an address
            # (ADR-0054 §三.8, T139), not a speculative feature.
            "source": "model",
        }
        applied += 1
    return SlotWriteback(aggregate=updated, applied=applied, refusals=tuple(refusals))


#: 一条素材真的发出去了，就把声明了它的那个槽写上（运维缺陷 18）。
#:
#: **这一格里写的是 ref，写进槽的是那条素材的原文** —— 原文就是商家审过的
#: 「那个数与口径」，而槽的用处正是让后面几轮读得到它。
DELIVERY_FILLED_SLOT_KEY = "filled_by_delivery"

#: 平台据自己的记录写下的槽，置信度是 1.0。
#:
#: **不是修辞**：`apply_slot_updates` 的 RFC 4.3 规则拿这个数和模型报的数比，
#: 而这一条的依据是 `intent_records` 里一行真的离开过 `APPROVED` 的记录 ——
#: 它不是一次读理解，没有「读得准不准」这回事。
DELIVERY_SLOT_CONFIDENCE = 1.0


def record_delivered_slots(
    *,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    delivered: Collection[str],
    recorded_at: str,
    evidence_event_id: str,
) -> SlotWriteback:
    """把「这条素材真的发出去了」写进声明了它的那个槽（运维缺陷 18）。

    ## 它修的是什么

    stage 2026-09-23：`material:demo-price-plain:v1`（含「一个月 1888」那句）
    在 03:14:17 真的发给了客户，而 `quote` 帧的 `price_quoted` 始终是空的。
    那个槽是该帧 `exit_conditions` 的**唯一**判据
    （`{"slot": "price_quoted", "present": true}`），于是：
    **「报过价」这件事在对话里发生了，在状态里没有发生。**

    ## 同一件事先前有两个定义点

    | 谁在答「报过价了吗」 | 依据 |
    |---|---|
    | `advance_when[].delivered` | 平台现读 `intent_records`（`pipeline._delivered_materials`） |
    | `exit_conditions[].slot` | **模型自填的槽** |

    `frame_to_advance_into` 那一格的注释早就把判据写下了：
    `material_delivered` 是模型自填的槽，**模型忘了填整条链就不触发、
    乱填就过早推进，而两种都不会红**。本函数让权威的那一份也能落到槽上 ——
    不是再造一个事实源，**是让那一个事实源多一个出口**。

    ## 为什么它跑在模型写回**之后**

    同一轮里两边都可能写同一个槽，而平台的那份该赢。**靠顺序，不靠再立一条
    保护规则**：一条「平台写的模型不许改」的规则要在
    `apply_slot_updates` 里多一个分支，而那个分支的正确性取决于调用顺序 ——
    两个定义点又一次。顺序本身就是规则，所以调用点上写了这件事。

    ⚠️ **人写的那份仍然赢过平台。** `SLOT_WRITTEN_BY_HUMAN` 原样复用：
    运维手填的报价口径不是我们能覆盖的，而复用同一个理由码意味着
    **审计那一侧不用加新码、迁移不用动**（`audit_events.reason_code` 的
    那张表在迁移里，加一个码要一条迁移）。

    ## 没有原文就不写

    `materials_for_frame` 要求两件同时成立：帧声明了这个 ref，**且包供了词**。
    今天在架的包大多不供词，那里返回 `{}` —— **这种情况下本函数什么都不做**，
    而不是把 ref 本身写进槽。把 ref 写进去会得到一个「看起来填上了」的槽：
    出口条件就此满足，而模型读到的是一个它没法复述的字符串。
    **这正是本仓最贵的那个方向：能存、能过闸、只有结果是错的。**

    ## 值没变就不写

    再发一次同一条素材不该让证据链多一笔 —— `delivered` 是累积的，
    每一轮都会再命中一次。**不比不写，这个函数每一轮都会给证据追加一条。**
    """

    definitions = frame_definitions(profile)
    delivered_refs = set(delivered)
    updated = deepcopy(aggregate)
    applied = 0
    refusals: list[dict[str, Any]] = []
    for frame in updated["target_stack"]:
        if frame["status"] not in _WORKABLE_FRAME_STATUSES:
            continue
        definition = definitions.get(str(frame["definition_id"]))
        if definition is None:
            continue
        texts = materials_for_frame(profile, definition)
        for slot in definition["information_slots"]:
            reference = str(slot.get(DELIVERY_FILLED_SLOT_KEY) or "").strip()
            if not reference or reference not in delivered_refs:
                continue
            text = texts.get(reference)
            if not text:
                continue
            name = str(slot["name"])
            recorded = frame.get("slot_evidence", {}).get(name)
            # **人写的赢过平台**，与 `apply_slot_updates` 逐字同一条规则，
            # 连理由码都是同一个 —— 审计那一侧按码检索，两种「模型/平台想改
            # 人写的东西」混成两个码会让检索的人漏掉一半。
            if recorded is not None and str(recorded.get("source", "model")) == "human":
                refusals.append(
                    {
                        "frame_id": str(frame["id"]),
                        "slot": name,
                        "reason": "SLOT_WRITTEN_BY_HUMAN",
                        "proposed_confidence": DELIVERY_SLOT_CONFIDENCE,
                        "proposed_evidence_event_id": evidence_event_id,
                        "recorded_evidence_event_id": _latest_evidence(recorded),
                    }
                )
                continue
            if frame["slots"].get(name) == text:
                continue
            frame["slots"][name] = text
            frame.setdefault("slot_evidence", {})[name] = {
                "confidence": DELIVERY_SLOT_CONFIDENCE,
                "evidence_event_ids": _appended_evidence(
                    recorded, evidence_event_id, value_changed=True
                ),
                "recorded_at": recorded_at,
                "source": "platform",
            }
            applied += 1
    return SlotWriteback(aggregate=updated, applied=applied, refusals=tuple(refusals))


def closing_frame_definition_id(profile: dict[str, Any]) -> str | None:
    """The frame a conversation moves into when its root goal completes.

    ADR-0031: the terminal turn had no legal intent owner, so it did not send and
    handed off to a human instead. That cost nothing only while `complete` was
    unreachable; once T6 makes it reachable, every *successfully finished*
    conversation would produce a manual task. The profile now names a frame that
    owns the closing turn.

    Returns `None` when the profile declares none, which is a supported state:
    the hard boundary in ADR-0031 is that a profile without a closing frame still
    hands off to a human rather than sending a message with no owner.
    """

    reference = profile.get("closing_frame_ref")
    return str(reference) if reference else None


def should_enter_closing_frame(
    *, aggregate: dict[str, Any], profile: dict[str, Any], disposition: str | None
) -> bool:
    """Whether the terminal turn can be owned by a closing frame instead of a person.

    Three conditions, each load-bearing, kept here rather than inline in the
    pipeline so each one can be tested on its own:

    * **`ROOT_TARGET_FRAME_COMPLETED` only.** An abandoned root arrived with an
      exit signal or a human request; a farewell is not what that conversation
      needs, so it keeps the handoff.
    * **The profile must declare one.** ADR-0031's hard boundary: a profile
      without a closing frame hands off rather than sending a message filed
      under a completed goal.
    * **Not already in the closing frame.** When the closing frame itself
      completes, the conversation really is over and falls through to the
      handoff. Without this the pipeline would push a fresh closing frame every
      time the previous one completed, and the stack would grow forever --
      ADR-0031 left "define the terminus" to this batch, and this is it.
    """

    if disposition != "ROOT_TARGET_FRAME_COMPLETED":
        return False
    definition_id = closing_frame_definition_id(profile)
    if definition_id is None:
        return False
    stack = aggregate["target_stack"]
    # **"Has this conversation already had its closing turn", not "is the
    # closing frame on top right now"** (independent review 2026-09-10, P0-1).
    #
    # Reading `stack[-1]` was sufficient for exactly as long as the closing
    # frame was the last thing that could ever be pushed: once it completed it
    # stayed on top, the condition went false, and the conversation went to a
    # person. **B4-7 put the platform frame above it**, so the top stopped being
    # the closing frame and this condition started saying yes again -- on every
    # subsequent turn.
    #
    # The measured consequence, with a profile that declares a closing frame
    # (**both shipped profiles do**): the customer sends an exit signal, the
    # platform answers by entering the closing frame again -- drafting another
    # goodbye -- then the no-goal frame, then the closing frame, one extra frame
    # per turn, `disposition` never leaving `None`, so it never reaches a
    # person. Depth 8 by turn 7 in the reproduction.
    return not any(str(frame["definition_id"]) == definition_id for frame in stack)


def enter_closing_frame(
    *, aggregate: dict[str, Any], profile: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    """Push the profile's closing frame so the final turn has a legal owner.

    The pushed frame is `active` and parented to the frame that just completed,
    so the stack still reads as the history of what happened. The generation is
    not bumped: nothing was invalidated, and this is a continuation of the same
    turn rather than a new external decision about the conversation.
    """

    definition_id = closing_frame_definition_id(profile)
    if definition_id is None:
        raise TargetStackTransitionError("profile declares no closing frame")
    completed = aggregate["target_stack"][-1]
    if str(completed["definition_id"]) == definition_id:
        raise TargetStackTransitionError(
            "the closing frame cannot close itself: this conversation is over"
        )
    definition = _definition(profile, definition_id)
    updated = deepcopy(aggregate)
    generation = int(updated["generation"])
    completed = updated["target_stack"][-1]
    seeded_slots, seeded_evidence = _seeded_slots(updated, profile, definition_id)
    updated["target_stack"].append(
        {
            "id": str(uuid4()),
            "definition_id": definition_id,
            "goal": str(definition["goal"]),
            "status": "active",
            "slots": seeded_slots,
            "slot_evidence": seeded_evidence,
            "entered_at": occurred_at,
            "last_progress_at": occurred_at,
            "parent_frame_id": str(completed["id"]),
            "interruption_reason": None,
            "resume_confirmation_policy": str(
                definition["return_confirmation"]["policy"]
            ),
            "frame_generation": generation,
        }
    )
    return updated


#: 平台自己推进时，栈深上限与模型 `push` 用同一个数。
#: **写在这里而不是 import**：`inference_validation` 反过来依赖本模块，
#: 直接 import 会成环。两处相等由
#: `test_the_platform_advance_obeys_the_same_depth_cap` 钉着。
PLATFORM_ADVANCE_DEPTH_CAP = 3


def frame_to_advance_into(
    *,
    aggregate: dict[str, Any],
    profile: dict[str, Any],
    #: **平台自己读出来的、真的发出去过的那些话术 ref**
    #: （`pipeline._delivered_materials`，从 `intent_records` 现读）。
    #: `advance_when[].delivered` 判的就是它 —— **与模型报了什么无关**。
    delivered: Sequence[str] = (),
) -> str | None:
    """**平台自己决定的下一步，不经模型。** 用户 2026-09-21 裁定。

    ## 它补的是哪一段

    在此之前，一通对话只有两种推进：

    * 模型在计划步提一个 `push` —— 那是**它的判断**，它可能不提；
    * 根目标完成后平台进 `closing`（`should_enter_closing_frame`）——
      那只在**终局**。

    中间那一段没有任何确定性的东西在推：「两段讲完了，该报价了」
    全靠模型自觉。用户的原话是「**一般销售觉得聊得差不多都会主动报价来促成交易**」，
    而「觉得」这件事交给模型就等于交给运气。

    ## 判据

    当前**活动帧**的 `advance_when` 逐条看，**按声明顺序取第一条命中的**。
    `present` 看槽有没有值，`equals` 看槽等不等于某个值。

    ## ⚠️ 一个目标帧一生只进一次

    判据是它的 `definition_id` 在这通对话的目标栈里**从未出现过** ——
    **含已经终结的帧**。没有这一条：`quote` 完成、pop 回主线之后
    `material_delivered` 依然满足，平台会立刻再推一次，
    **而那是一个不会停的循环**，每一轮都花一次模型调用、发一条消息给客户。

    这也是它与模型 `push` 的分工：可重复的推进（客户提第二条疑虑，
    再进一次 `objection`）仍然由模型提，**因为「一生一次」正是它不该有的约束**。

    ## 它不做什么

    * **不推平台帧**（`closing` / no-goal）—— 与模型 `push` 同一条禁令，
      理由也一样：一个「这里没有目标」的帧压在活目标上，`complete` 还会被拒，
      栈就再也解不开了；
    * **不推自己**；
    * **栈满了不推**（`PLATFORM_ADVANCE_DEPTH_CAP`，与模型 `push` 同一个数）——
      数的是还能推进的帧，终结的帧是历史。
    """

    stack = aggregate.get("target_stack") or []
    if not stack:
        return None
    # **栈顶就是当前帧** —— 与 `inference_validation.current_frame` 同一条。
    #
    # ⚠️ **2026-09-21 这里错过一次，值得留痕**：我以为「子帧完成 pop 回主线之后
    # 栈顶是个 `completed` 帧」，于是把它改成「从栈顶往下找第一个 `active`」。
    # **那是错的，而且两头都错**：
    #
    # * `complete` **不弹栈**，它把父帧置成 `repair_pending` ——
    #   所以那一刻栈里**一个 `active` 都没有**，改完的版本反而恒返回 `None`；
    # * 真正弹栈的是 `request_pop`（本文件 `stack.pop()`），
    #   而它跑完之后**栈顶就是父帧、而且是 `active`**。
    #
    # 完整返回路径实测三步：
    # `complete`（父 `repair_pending`）→ `repair`（父 `resume_confirmation_pending`）
    # → `request_pop`（子帧弹掉，父 `active`）。
    # 中间两步栈顶确实是终结帧，**而那两步本来就不该推进** —— 对话还在返回途中。
    current = stack[-1]
    if str(current.get("status")) in TERMINAL_FRAME_STATUSES:
        return None

    rules = _definition(profile, str(current["definition_id"])).get("advance_when") or []
    if not rules:
        return None

    slots = current.get("slots") or {}
    #: ⚠️ **读的是那份不随栈增减的记录，不是栈。**
    #: `request_pop` 会把子帧真的弹掉 —— 一个正常完成并返回的帧在栈里
    #: 一点痕迹都不留，而它恰恰是最该拦住的那一个。
    ever_entered = set(frames_ever_entered(aggregate))
    live = [f for f in stack if str(f.get("status")) not in TERMINAL_FRAME_STATUSES]

    for rule in rules:
        target = str(rule["push"])
        # ⚠️ **这里原先还有一道「目标帧必须是 `brief_answer`」的闸，
        # 2026-09-22 由 ADR-0150 去掉了。** 它的理由是：平台推进的条件会
        # **持续满足**（`quote` 完成 pop 回来之后「报过价」依然为真），
        # 所以可重入 + 条件持续满足 = 一个不会停的循环。
        #
        # **那个循环由下面这一行单独挡住了**，而这是量出来的：
        # `advance_into_frame` 自己在结尾调 `_record_entry`，
        # **平台推进一次，那个帧就永久进了 `frames_entered`** ——
        # 第二轮起 `target in ever_entered` 恒为真，平台再也推不进去。
        #
        # 而多出来那一层是有代价的：商家把 `quote` 写成 `reenter`
        # （用户 2026-09-22：「**有些销售场景需要反复报价**」），
        # 那道闸会把 `introduce → quote` 整条推进规则 `continue` 掉 ——
        # **配包照样封得出、照样能跑、没有任何东西变红**，
        # 只是这通对话从此不会自己走到报价了。`CLAUDE.md` 第三节那一类：
        # *传了个合法值，结果安静地不对*。
        #
        # 今天的行为：`reenter` 的帧平台自动推进**只发生一次**，
        # 之后客户再问起，由模型 `push` 进来 —— `reenter` 本来就是这个意思。
        #
        # 判据与验证器那一侧逐字相同（`FRAME_ALREADY_ENTERED`）：
        # `definition_id` 在栈里出现过，**含已终结的帧**。
        if target in ever_entered or target == str(current["definition_id"]):
            continue
        if is_platform_frame(target):
            continue
        if len(live) >= PLATFORM_ADVANCE_DEPTH_CAP:
            continue
        # ⚠️ **平台自己的记录优先。** `delivered` 读的是
        # `intent_records` 里真的离开过 `APPROVED` 的那些 `send_message`
        # （`pipeline._delivered_materials`），**与模型报了什么无关**。
        #
        # 为什么这一格比 `slot` 重要：`material_delivered` 是**模型自填的槽**，
        # `pipeline.py` 那条注释逐字写着「guess wrong, select the wrong
        # materials, and nothing goes red」。把推进挂在它上面，
        # **模型忘了填整条链就不触发、乱填就过早推进，而两种都不会红**。
        if "delivered" in rule:
            if str(rule["delivered"]) in delivered:
                return target
            continue
        value = slots.get(str(rule["slot"]))
        if "equals" in rule:
            if value is not None and str(value) == str(rule["equals"]):
                return target
            continue
        if bool(rule.get("present", True)) and value is not None:
            return target
    return None


def advance_into_frame(
    *, aggregate: dict[str, Any], profile: dict[str, Any], definition_id: str, occurred_at: str
) -> dict[str, Any]:
    """把 `frame_to_advance_into` 选中的那个帧压上去。

    形状与 `enter_closing_frame` 逐字相同，**除了父帧不是刚完成的那个**：
    这里父帧还活着，它被挂起等着子帧回来 —— 与模型 `push` 一样。
    代次不抬：没有任何东西被作废，这是同一个回合的延续。
    """

    definition = _definition(profile, definition_id)
    updated = deepcopy(aggregate)
    generation = int(updated["generation"])
    # 父帧是栈顶那一个 —— 同 `frame_to_advance_into`，与验证器的
    # `current_frame` 一致。（2026-09-21 这里跟着上面错过一次，一并改回。）
    parent = updated["target_stack"][-1]
    parent["status"] = "suspended"
    seeded_slots, seeded_evidence = _seeded_slots(updated, profile, definition_id)
    updated["target_stack"].append(
        {
            "id": str(uuid4()),
            "definition_id": definition_id,
            "goal": str(definition["goal"]),
            "status": "active",
            "slots": seeded_slots,
            "slot_evidence": seeded_evidence,
            "entered_at": occurred_at,
            "last_progress_at": occurred_at,
            "parent_frame_id": str(parent["id"]),
            #: **不是打岔。** 模型 `push` 时这一格记的是打断关系；
            #: 平台推进没有「被谁打断」这回事 —— 是这通对话按商家声明的
            #: 顺序往下走了一步。
            "interruption_reason": None,
            "resume_confirmation_policy": str(definition["return_confirmation"]["policy"]),
            "frame_generation": generation,
        }
    )
    _record_entry(updated, definition_id)
    return updated


def should_enter_no_goal_frame(
    *, aggregate: dict[str, Any], profile: dict[str, Any], disposition: str | None
) -> bool:
    """Whether the platform's no-goal frame owns this turn. **ADR-0099, B4-7.**

    Read it as the second question, asked only after
    `should_enter_closing_frame` has said no. Three conditions:

    * **`ROOT_TARGET_FRAME_COMPLETED` only**, the same first condition as the
      closing frame. An abandoned root arrived with an exit signal or a request
      for a person, and ADR-0099 §五.3 leaves that road where it was;
    * **the profile's own closing frame goes first.** A profile that declares
      one is describing how *it* wants the last word said; the platform frame is
      what happens when nobody described it. This also covers the turn on which
      the declared closing frame itself completes -- `should_enter_closing_
      frame` refuses to push it onto itself, and this frame catches what falls
      through, which before B4-7 was the handoff that terminus produced;
    * **not already in a platform frame.** `complete` is refused on one
      (`apply_target_stack_operation`), so the way a stack arrives here with a
      platform frame on top is `abandon`: the customer sent an exit signal after
      the goal was met. That stack is `[root completed, no-goal abandoned]` --
      not *all* abandoned, so the disposition is still
      `ROOT_TARGET_FRAME_COMPLETED` -- and pushing a second no-goal frame would
      answer "stop" by continuing. It keeps the handoff, and that is why
      `ROOT_TARGET_FRAME_COMPLETED` still has a producer after this batch.
    """

    if disposition != "ROOT_TARGET_FRAME_COMPLETED":
        return False
    if should_enter_closing_frame(
        aggregate=aggregate, profile=profile, disposition=disposition
    ):
        return False
    stack = aggregate["target_stack"]
    # **Same correction as the closing frame's, and for the same reason**
    # (review P0-1): "already been here" rather than "here right now". This one
    # was not reachable in the reproduction -- the closing frame re-entered
    # first and the two took turns -- but it has the identical shape, and
    # fixing only the half that was demonstrated is how the other half becomes
    # the next finding.
    return not any(is_platform_frame(frame["definition_id"]) for frame in stack)


def enter_no_goal_frame(*, aggregate: dict[str, Any], occurred_at: str) -> dict[str, Any]:
    """Push the platform's no-goal frame so a finished conversation still has an owner.

    Shaped exactly like `enter_closing_frame` -- `active`, parented to the frame
    that just went terminal, generation untouched because nothing was
    invalidated -- with one difference in the signature: **no `profile`**. That
    is the point of the batch. The frame comes from the platform, so a profile
    that declares nothing gets the same owner as one that declares everything.
    """

    updated = deepcopy(aggregate)
    stack = updated["target_stack"]
    if not stack:
        raise TargetStackTransitionError("target stack is empty")
    # Read straight from the platform catalog rather than through
    # `_definition`: this frame is not in any profile, and asking a profile for
    # it would make the lookup look conditional on a document that has no say.
    definition = NO_GOAL_FRAME_DEFINITION
    completed = stack[-1]
    if is_platform_frame(completed["definition_id"]):
        raise TargetStackTransitionError(
            "the no-goal frame cannot follow itself: this conversation is over"
        )
    stack.append(
        {
            "id": str(uuid4()),
            "definition_id": NO_GOAL_FRAME_DEFINITION_ID,
            "goal": str(definition["goal"]),
            "status": "active",
            "slots": {},
            "slot_evidence": {},
            "entered_at": occurred_at,
            "last_progress_at": occurred_at,
            "parent_frame_id": str(completed["id"]),
            "interruption_reason": None,
            "resume_confirmation_policy": str(definition["return_confirmation"]["policy"]),
            "frame_generation": int(updated["generation"]),
        }
    )
    return updated


def suspend_target_stack_for_human(
    *, aggregate: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    """Suspend every resumable frame without discarding stack history."""

    updated = deepcopy(aggregate)
    for frame in updated["target_stack"]:
        if frame["status"] in {"active", "repair_pending", "resume_confirmation_pending"}:
            frame["status"] = "suspended"
            _stamp(frame, occurred_at=occurred_at)
    return updated


def resume_target_stack_from_human(
    *, aggregate: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    """Put a suspended stack back to work. **T4**, and without it `resume` lies.

    `suspend_target_stack_for_human` has been in this module since takeover
    landed and **nothing has ever undone it**. The consequence is not subtle:
    `resume` answers 200, `control_mode` goes back to `ai`, and every frame is
    still `suspended` -- after which `keep` and `push` are refused forever,
    because both require an `active` frame. The preconditions review put it in
    one line: "不修它，resume 返回 200 而会话仍然全帧挂起."

    **The top suspended frame comes back `active`, at any depth**
    (**ADR-0058 §二.1**, which revised ADR-0054 §三.4). Frames beneath it stay
    `suspended`; `repair_pending` is produced only by the existing `complete`
    branch, which is this repository's one legal producer of it.

    **The revised rule replaced one that locked the stack.** U4 originally put
    the top frame in `repair_pending` at depth ≥ 2. An independent review then
    built such a stack with the production functions and asked the production
    validator all six operations under all six relations: **none of them is
    legal.** `keep` and `push` hit `*_ON_INACTIVE_FRAME`, `complete` hits
    `EXIT_CONDITION_NOT_SATISFIED`, `repair` hits `REPAIR_ON_UNPREPARED_PARENT`,
    `request_pop` hits `POP_BEFORE_CHILD_RESOLVED`. And `plan_or_fallback`
    **raises** rather than falling back -- `safe_fallback_plan` hard-codes
    `keep`, whose validation sits outside the `try`. The consequence is on the
    record in this very module: `repair_target_stack_for_repin`'s docstring
    describes it from 2026-09-03 -- the event burns `MAX_INBOX_ATTEMPTS` and
    lands `failed`, **with no reply and no handoff**, invisible to
    `stack_requires_human_disposition` because `repair_pending` counts as
    workable.

    That same docstring had already written the rule down: **`repair` advances
    the *parent*, so `repair_pending` is a parent's status; putting it on the
    top of the stack locks the stack.**

    **Terminal frames are never touched.** `completed` / `abandoned` frames are
    the evidence of what happened before the takeover; moving them would rewrite
    history, which is the same rule `apply_slot_updates` follows for their slots.

    The generation is **not** bumped here. The caller owns that, together with
    writing `control_mode` back -- see the pipeline's resume path, which must
    write **both** the column and the `state` document (worker reads
    `state["control_mode"]`; writing only the column means the worker never
    learns the takeover ended).
    """

    updated = deepcopy(aggregate)
    stack = updated["target_stack"]
    # **Idempotent against an already-resumed stack** (independent review
    # 2026-09-05, F5). Resuming twice used to promote the *parent* as well,
    # producing a stack with two workable non-terminal frames -- a shape neither
    # `push` nor `complete` can build, and one where `current_frame` reads a
    # frame that is not the workable one. B3 #7 carries a CAS, but a CAS stops
    # concurrent writes, not a replay after a failed retry.
    #
    # Reading the top of the stack rather than "the last suspended frame" is
    # what makes that impossible: if the innermost non-terminal frame is already
    # back, there is nothing to resume.
    resumable = [
        frame
        for frame in stack
        if frame["status"] not in TERMINAL_FRAME_STATUSES
    ]
    if not resumable or resumable[-1]["status"] != "suspended":
        return updated
    top = resumable[-1]
    top["status"] = "active"
    _stamp(top, occurred_at=occurred_at)
    return updated


@dataclass(frozen=True, slots=True)
class RepinRepair:
    """The result of re-grounding one conversation against a new profile release."""

    aggregate: dict[str, Any]
    #: Definition ids that live on the stack but not in the new profile. A frame
    #: whose goal no longer exists cannot be repaired by any rule this code could
    #: invent, so the caller hands the conversation to a human instead.
    unknown_definition_ids: tuple[str, ...]


def repair_target_stack_for_repin(
    *, aggregate: dict[str, Any], profile: dict[str, Any], occurred_at: str
) -> RepinRepair:
    """Re-ground a live stack after its conversation is repinned to a new release.

    RFC §9.1: an explicit repin must bump the generation, repair the target
    stack, invalidate pending events and audit. This is the target-stack half.

    **Repair here means re-grounding, not a status change.** Every workable frame
    keeps the status it had; what changes is that its `goal` text is refreshed
    from the new release's definition.

    **`frame_generation` is no longer restamped here** (ADR-0098 §二, B4-3).
    Under the ruling it is a creation pin -- 「在帧创建时写一次，之后不动」 -- and a
    repin re-grounds the frames that exist, it does not create new ones. What a
    repin needs to invalidate is timers, and that is done by the **aggregate**
    generation bumped on the next line plus RFC §9.1's invalidation, neither of
    which reads this field. Before B4-3 this function restamped every workable
    frame, which is what made the field a last-touched counter.

    The first version of this function moved every workable frame to
    `repair_pending`, including the top one, on the theory that `repair_pending`
    is still a workable status so the conversation was not stranded. The
    independent review (2026-09-03, P0-2) showed that is false for the *top*
    frame, and proved it against the validator: with a `repair_pending` frame on
    top, `keep` is rejected `KEEP_ON_INACTIVE_FRAME`, `push`
    `PUSH_ON_INACTIVE_FRAME`, `complete` `EXIT_CONDITION_NOT_SATISFIED`,
    `abandon` `ABANDON_WITHOUT_EXIT_SIGNAL`, and `repair` / `request_pop`
    `STACK_OP_REQUIRES_PARENT` -- and `repair` advances the *parent* anyway, so a
    single-frame stack, the ordinary shape, has no route back to `active` at all.
    `safe_fallback_plan` hard-codes `keep`, whose rejection is raised outside the
    fallback's try, so the event would burn its retry budget and land `failed`:
    no reply, no handoff. `stack_requires_human_disposition` would not have
    noticed, because `repair_pending` counts as workable -- the guard cannot see
    a stack that is stranded in exactly this way.

    Terminal frames (`completed`, `abandoned`) are history under the old release
    and are left untouched, including their goal text: rewriting them would
    falsify what the conversation actually pursued.

    The generation bump lives here rather than in the caller because the repair
    and the bump are one transition: splitting them is how an aggregate ends up
    repaired against a release it does not claim to be pinned to. (Before B4-3
    this paragraph also carried the frame-stamping argument; that half went with
    the restamping.)
    """

    updated = deepcopy(aggregate)
    updated["generation"] = int(updated["generation"]) + 1
    # **Platform frames resolve here too, and that is load-bearing** (B4-7).
    # This function runs from the API's repin path (`profiles.binding`) with the
    # *new* release document, and no release declares the platform's frames. A
    # conversation resting in the no-goal frame would otherwise be reported as
    # `unknown_definition_ids`, which repins to `control_mode='human'` -- the
    # manual task ADR-0099 removes, re-entering through the profile-publish
    # door instead of the completion door.
    definitions = frame_definitions(profile)
    unknown: list[str] = []
    for frame in updated["target_stack"]:
        if frame["status"] not in _WORKABLE_FRAME_STATUSES:
            continue
        definition_id = str(frame["definition_id"])
        definition = definitions.get(definition_id)
        if definition is None:
            unknown.append(definition_id)
            continue
        # The goal is what the frame is *for*, and the new release may word it
        # differently. Restamping without refreshing it would leave the frame
        # claiming a goal the release in force no longer states.
        frame["goal"] = str(definition["goal"])
        _stamp(frame, occurred_at=occurred_at)
    return RepinRepair(aggregate=updated, unknown_definition_ids=tuple(unknown))


def stack_requires_human_disposition(aggregate: dict[str, Any]) -> str | None:
    """Return a reason code when no frame on the stack can accept further work.

    Completing or abandoning the root frame leaves a stack whose every frame is
    terminal: `keep` and `push` demand an active frame, `repair` and
    `request_pop` demand a parent. Nothing can move it again. Before this check
    existed the conversation stayed `control_mode='ai'` and `lifecycle='open'`
    while every subsequent customer message fell through plan rejection into a
    silent no-send -- the customer waited forever and nobody was told.

    A suspended frame is not terminal: a human already owns that conversation.
    """

    stack = aggregate["target_stack"]
    if not stack:
        return "TARGET_STACK_EMPTY"
    if any(frame["status"] in _WORKABLE_FRAME_STATUSES for frame in stack):
        return None
    if all(frame["status"] == "abandoned" for frame in stack):
        return "ROOT_TARGET_FRAME_ABANDONED"
    return "ROOT_TARGET_FRAME_COMPLETED"
