"""The rate gate: over the limit becomes a manual task, never a discard (B1-6).

**Why a gate exists at all**, from T108: the ceiling on messages one event may
produce moved from 1 to 4 when a turn was allowed to raise several intents, and
there was no rate limit anywhere on that path. The registered worry was not the
number — it was that *without* a gate, "the ceiling is 4" becomes the baseline
the next person widens from.

**Why what is over the limit becomes a manual task** (ADR-0095 §五, on
ADR-0084 §三's reasoning): discarding produces a message nobody can look up
afterwards, and silently queueing produces one that arrives at an unpredictable
time. Both were refused by name, so the withheld work has to land somewhere a
person reads.

The two numbers are the user's and are marked in the ADR for recalibration
against the first real merchant. **These cases reference the constants rather
than the literals**, so a recalibration reads as a recalibration and not as a
regression.

## ⚠️ 2026-09-19（T368）：会话这一边的**单位从「条」换成「轮」**

用户当天裁的口径，原话：**「速率闸是用来防止骚扰而不是正常对话。」**

在此之前会话这一边是 **3 条/分钟**，而一轮可以发好几条 —— 批 G 把
「许诺一条 + 紧跟兑现一条」变成了有意为之的形状。按条数算时「还剩一条」
会把那两条劈开：**许诺留下、兑现被扣住**，客户看到一句光秃秃的许诺。

今天它是 **5 轮/分钟，而且一轮是原子的**：要么都发、要么都不发。
**`_spend_budget(count=N)` 因此是「往窗口里放 N 轮」** —— 它每一行一个
独立的 `idempotency_key`，而轮次键取的是那把键的中段（见 `TURN_KEY_SQL`）。

⚠️ **租户那一边仍然数条数**，而它是今天唯一还会把一轮劈开的地方 ——
用户同一句裁定的后半「超额的走批 F 丢弃」挂在它上面。
那一半的单元覆盖在 `tests/contracts/test_the_rate_limit_counts_turns.py` 里，
**这份文件覆盖的是会话/租户两边的算术**。
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker.pipeline import (
    RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE,
    RATE_LIMIT_TENANT_PER_MINUTE,
    RATE_LIMITED_REASON,
)
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

#: How many customer messages one `TwoSendsInference` turn puts in the window.
#: Named because the arithmetic below is written off the constants -- a
#: recalibrated limit should read as a recalibration, not as a regression, and
#: that only holds if the other side of the subtraction has a name too.
#:
#: ⚠️ **它不再参与会话那一边的算术**（T368）：那一边数**轮**，而这个数是
#: 「一轮几条」。它留着是因为**消息数的断言仍然要用它** ——
#: 「这一轮的两条都出去了」是本文件第一条用例的全部内容。
TWO_SENDS_PER_TURN = 2

#: 一个驱动出来的真实回合在窗口里算**一轮**。
#: 写成名字而不是字面 `1`：它与 `TWO_SENDS_PER_TURN` 是两个不同的量
#: （一轮 vs 一轮几条），而先前的算术把它们混在一个减法里。
ONE_DRIVEN_TURN = 1


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _spend_budget(
    tenant_id: UUID, conversation_id: UUID, *, count: int, audience: str | None = "customer"
) -> None:
    """Put `count` customer **turns** into this conversation's window.

    ⚠️ **单位是轮，不是条**（T368，2026-09-19）。会话那一边数的是
    `count(DISTINCT split_part(idempotency_key, ':', 2))`，而下面每一行都带
    **自己的** `rate-fixture:{uuid}` —— 所以 N 行就是 N 轮，一行一轮。
    **这不是巧合，而是这个夹具今天必须具备的形状**：生产里 N 轮是 N 个事务，
    而按轮数的闸要数得出它们。
    ⚠️ 而**真回合**的两行共享一个源事件 id，所以它算**一轮**
    （`ONE_DRIVEN_TURN`）—— 两种形状都在本文件里被用到。

    Written straight to the outbox: what is under test is the gate's *reading*
    of the rate, and driving `count` real turns would drive everything else
    those turns do as well.

    `audience=None` writes rows with **no `audience` key at all**, which is what
    every outbox row older than `35f4770` looks like. They are customer messages
    and the gate has to count them (independent review P2-2).

    **This runs after a turn, never before one**, and the first version got
    that wrong twice over. It looked up a `send_message` intent to borrow
    `target_frame_id` from while the calling cases ran `_seed` ->
    `_spend_budget` -> `_drive`: at that moment `_seed` has only taken the
    customer's message in, so there was no intent. Taking the frame from the
    aggregate instead -- the way the pipeline takes it,
    `prepared.aggregate["target_stack"][-1]["id"]` (`pipeline.py:1402`) -- moves
    the problem rather than fixing it: **`conversation_aggregates` does not
    exist yet either.** Accepting a message writes an inbox event; the
    conversation is built when a turn processes it, and `intent_records` has a
    foreign key onto it.

    So the callers drive one turn first and top the window up from there. The
    frame still comes from the aggregate, which is the source the production
    path reads rather than a by-product of it.
    """

    with _migration_connection() as connection:
        aggregate = connection.execute(
            """
            SELECT target_stack FROM conversation_aggregates
             WHERE tenant_id = %s AND id = %s
            """,
            (tenant_id, conversation_id),
        ).fetchone()
        assert aggregate is not None, "the fixture needs the conversation to exist"
        stack = aggregate["target_stack"]
        assert stack, "the conversation has no target frame to hang rows on"
        target_frame_id = UUID(str(stack[-1]["id"]))
        for index in range(count):
            filler_intent = uuid4()
            connection.execute(
                """
                INSERT INTO intent_records(
                    tenant_id, id, conversation_id, kind, status, proposal, proposed_by,
                    strategy_id, target_frame_id, idempotency_key, preconditions,
                    policy_evidence
                ) VALUES (%s, %s, %s, 'send_message', 'APPROVED', %s, %s, 'rate-fixture',
                          %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    filler_intent,
                    conversation_id,
                    Jsonb({"fixture": True}),
                    Jsonb({"step": "fixture", "version": "1"}),
                    target_frame_id,
                    f"rate-fixture:{filler_intent}",
                    Jsonb({}),
                    Jsonb([]),
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox_messages(
                    tenant_id, id, conversation_id, intent_id, command, idempotency_key, ordinal
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    uuid4(),
                    conversation_id,
                    filler_intent,
                    Jsonb(
                        {
                            **({"audience": audience} if audience is not None else {}),
                            "content": {"type": "text", "text": "x"},
                        }
                    ),
                    f"rate-fixture:{filler_intent}",
                    index,
                ),
            )
        connection.commit()


def _state(tenant_id: UUID, conversation_id: UUID) -> dict[str, Any]:
    with _migration_connection() as connection:
        sent = connection.execute(
            """
            SELECT count(*) AS n FROM outbox_messages
             WHERE tenant_id = %s AND conversation_id = %s
               -- Same coalescing the gate uses (`AUDIENCE_IS_CUSTOMER_SQL`).
               -- Written strictly here too, this helper agreed with the bug it
               -- was supposed to catch (independent review P2-2).
               AND coalesce(command->>'audience', 'customer') = 'customer'
            """,
            (tenant_id, conversation_id),
        ).fetchone()
        tasks = connection.execute(
            """
            SELECT count(*) AS n, max(evidence->>'scope') AS scope,
                   max((evidence->>'withheld')::int) AS withheld,
                   max(evidence->>'withheld_ordinals') AS ordinals,
                   max(evidence->>'event_id') AS event_id
              FROM manual_tasks
             WHERE tenant_id = %s AND conversation_id = %s AND reason_code = %s
            """,
            (tenant_id, conversation_id, RATE_LIMITED_REASON),
        ).fetchone()
        audits = connection.execute(
            """
            SELECT count(*) AS n, max(evidence->>'scope') AS scope,
                   max(reason_code) AS reason_code, max(resource_type) AS resource_type
              FROM audit_events
             WHERE tenant_id = %s AND action = 'plan.rate_limited' AND resource_id = %s
            """,
            (tenant_id, conversation_id),
        ).fetchone()
    return {
        "sent": int(sent["n"]),
        "tasks": int(tasks["n"]),
        "withheld": tasks["withheld"],
        "task_scope": tasks["scope"],
        "task_ordinals": tasks["ordinals"],
        "task_event_id": tasks["event_id"],
        "audits": int(audits["n"]),
        "audit_scope": audits["scope"],
        "audit_reason_code": audits["reason_code"],
        "audit_resource_type": audits["resource_type"],
    }


def _withheld_from_the_database(tenant_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    """What the rate gate refused, read back the way a person would have to.

    `policy_decisions` is where every other refusal in this pipeline lands, one
    row per proposal, identified by `context.proposal_ordinal`. Before the
    disposition of P1-1 the withheld replies had no row anywhere and their text
    existed only in the process that dropped it.
    """

    with _migration_connection() as connection:
        rows = connection.execute(
            """
            SELECT context FROM policy_decisions
             WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'
               AND reason_code = %s
             ORDER BY (context->>'proposal_ordinal')::int
            """,
            (tenant_id, conversation_id, RATE_LIMITED_REASON),
        ).fetchall()
    return [row["context"] for row in rows]


def test_a_turn_inside_the_limit_is_untouched(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """The gate must be invisible when it is not biting.

    Asserted first because it is what a limiter most easily gets wrong, and it
    fails in the direction nobody looks: messages that were supposed to go out
    and quietly did not. **一轮两条，额度五轮，什么都不该被扣。**
    """

    from test_m1a_no_send import _drive  # noqa: PLC0415
    from test_multi_intent_generation import TwoSendsInference, _seed  # noqa: PLC0415

    tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    state = _state(tenant_id, conversation_id)
    assert state["sent"] == 2, "the turn's two messages did not both go out"
    assert state["tasks"] == 0, "the gate filed a task while under the limit"
    assert state["audits"] == 0, "the gate recorded a trip while under the limit"


def test_a_conversation_with_no_room_withholds_both_and_files_one_task(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """Budget already spent: nothing new goes out, and the work is visible.

    **One task, not two.** A turn that went two over is one thing that
    happened; filing a task per withheld message would make the backlog report
    its own volume rather than the merchant's.

    The first turn is what builds the conversation (see `_spend_budget`);
    **它自己占一轮**，所以窗口再补 `LIMIT - 1` 轮就满了，
    第二个回合到达时一轮都不剩。

    ⚠️ **「both」现在是原子性的结果，不只是「刚好两条都超」**（T368）：
    会话这一边没有「还剩几条」这回事，所以这一轮整个不发。
    """

    from test_m1a_no_send import _accept_customer_message, _drive  # noqa: PLC0415
    from test_multi_intent_generation import (  # noqa: PLC0415
        SECOND_MESSAGE_TEXT,
        TwoSendsInference,
        _seed,
    )

    tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())
    _spend_budget(
        tenant_id,
        conversation_id,
        count=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN,
    )

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    result = _drive(settings=settings, inference=TwoSendsInference())

    state = _state(tenant_id, conversation_id)
    # 窗口里的**条**数 = 第一个真回合那两条 + 每轮一条的那几行填充。
    assert state["sent"] == TWO_SENDS_PER_TURN + (
        RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN
    ), "a message went out over the limit; the gate did not bite"
    assert state["tasks"] == 1, "the withheld replies reached no person -- that is a discard"
    assert state["withheld"] == 2
    assert state["task_scope"] == "conversation"
    assert state["audit_scope"] == "conversation", (
        "the metric must say which limit bit: 'we are over' is not actionable, "
        "'this conversation is over while the tenant is fine' is"
    )
    # **The vocabulary, not just the count** (independent review P2-3). The
    # scope lives in `evidence`; `reason_code` says what happened, the same word
    # the manual task uses; `resource_type` is a resource that exists in
    # `resource-catalog.json`.
    assert state["audit_reason_code"] == RATE_LIMITED_REASON
    assert state["audit_resource_type"] == "conversation_aggregate"

    # **The result may not name rows that were never written** (P2-1). The ids
    # are minted at decision time, so a withheld proposal carries a pair of them
    # -- reporting those hands the caller ids that do not exist.
    assert result.outbox_ids == (), (
        "the turn reported outbox rows while withholding everything; "
        f"{result.outbox_ids} was returned and none of it was written"
    )
    assert result.intent_ids == ()

    # **The withheld replies can be read back** (P1-1). "Hand it to a person"
    # means the person can see what to send; before this the text existed only
    # in the process that dropped it.
    withheld = _withheld_from_the_database(tenant_id, conversation_id)
    assert len(withheld) == 2, (
        "the withheld replies left no per-proposal row, so the only record is a "
        "count -- which is what a discard looks like from the database"
    )
    assert [row["proposal_ordinal"] for row in withheld] == [0, 1]
    texts = [row["withheld_content"]["content"]["text"] for row in withheld]
    assert all(text.strip() for text in texts), f"a withheld row carries no text: {withheld}"
    assert SECOND_MESSAGE_TEXT in texts, (
        "the second reply of the turn is not recoverable, so a person picking "
        "this task up cannot send what the model had already written"
    )
    assert state["task_ordinals"] == "[0, 1]", (
        "the task does not point at the rows that carry the text"
    )
    # **The pointer works both ways** (independent re-review P2/N2). The task
    # names the ordinals; each denial row names the event. Without the second
    # half, two limit trips on one conversation in a minute leave four rows with
    # ordinals 0,1,0,1 and two tasks each claiming `[0, 1]`, pairable only by
    # guessing from `created_at`.
    assert {row["event_id"] for row in withheld} == {state["task_event_id"]}, (
        "the denial rows and the task that points at them do not name the same "
        "event, so a second trip in the same minute makes them unpairable"
    )
    # **Which strategy produced the reply that got withheld** (N3). ADR-0095 §五
    # names these rows as the calibration input for the two guessed limits, and
    # "which strategy hits the limit most" is the first question calibration
    # asks. The first version passed `plan=None` here and recorded `"none"`.
    assert all(row["strategy_id"] != "none" for row in withheld), (
        "the withheld rows do not record which strategy produced them, so the "
        "calibration input is missing a dimension"
    )


def test_a_window_one_turn_short_of_the_limit_still_sends_the_whole_turn(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """**一轮是原子的**：还剩一轮时，那一轮的两条**都**出去。

    ⚠️ **这一支是 T368 改写的，而它改写的那条论证正好相反。** 原来的
    docstring 写着：

    > A gate that only handles "all or nothing" would either send both (over the
    > limit) or send neither (withholding a message that was allowed). Sending
    > the first and filing the second is the only answer that keeps both
    > promises.

    那句话在「一轮的两条是两件独立的事」这个前提下成立。**而批 G 之后不成立**：
    第一条可能是许诺、第二条是兑现，**劈开它们等于把一句空许诺发给客户**。
    用户 2026-09-19 裁的口径是「速率闸是用来防止骚扰而不是正常对话」——
    一轮的两条是**一次回复的两个气泡**。

    所以今天会话这一边**没有「劈开」这条路**：还剩一轮就整轮发，
    不剩就整轮不发（上一支钉的是后者）。
    **劈开只发生在租户那一边**，而那时许诺那句会被丢掉
    （`tests/contracts/test_the_rate_limit_counts_turns.py`）。
    """

    from test_m1a_no_send import _accept_customer_message, _drive  # noqa: PLC0415
    from test_multi_intent_generation import TwoSendsInference, _seed  # noqa: PLC0415

    tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())
    # 窗口填到**还剩一轮**：第一个真回合占一轮，再补 LIMIT - 2 轮。
    _spend_budget(
        tenant_id,
        conversation_id,
        count=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN - 1,
    )

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    _drive(settings=settings, inference=TwoSendsInference())

    state = _state(tenant_id, conversation_id)
    assert state["sent"] == TWO_SENDS_PER_TURN * 2 + (
        RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN - 1
    ), "最后那一轮没有整轮发出去 —— 一轮被劈开了，或者整轮被扣住了"
    assert state["tasks"] == 0, "还剩一轮而闸却记了一条待办"
    assert state["audits"] == 0, "一轮是原子的：还剩一轮时闸不该记下一次触发"
    # ⚠️ **「什么都没扣」不能写成 `state["withheld"] == 0`。**
    # 那个键来自 `max((evidence->>'withheld')::int)`，**一行都没有时 `max()` 是
    # NULL**，于是它是 `None` 而不是 `0` —— 本批第一版就这么写，
    # 门禁里红成 `assert None == 0`。
    # 兄弟用例 `..._inside_the_limit_is_untouched` 用的就是 tasks / audits
    # 两个计数，它们无歧义。
    assert state["withheld"] is None, (
        "有待办行而它报了扣住的条数 —— 上面两条计数已经说了不该有待办"
    )


def test_the_tenant_limit_bites_on_a_conversation_that_has_room_of_its_own(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """The other limit, and the label that says which one bit.

    **The two limits are not the same statement.** A conversation with three
    slots free can still be refused because the tenant next door spent the
    minute, and the answer a person needs is "your account is at its limit", not
    "this conversation is". That distinction is the whole reason `scope` exists
    and is the label on `rate_limited_total{scope}` (ADR-0101 §三.2) -- and the
    branch that produces `tenant`, together with the tie-break that sends ties
    to `conversation`, had no case over it.

    Built by spending the tenant's minute in **another** conversation: the one
    under test starts empty, so `conversation_room` is full and only
    `tenant_room` can be doing the cutting. The filler conversation is driven
    first so the queue holds exactly one event when the case drives the second
    -- `process_one` takes whatever is queued, and two pending events would make
    which conversation gets processed a coin toss.
    """

    from test_m1a_no_send import _drive  # noqa: PLC0415
    from test_multi_intent_generation import TwoSendsInference, _seed  # noqa: PLC0415

    tenant_id, elsewhere = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())
    _spend_budget(tenant_id, elsewhere, count=RATE_LIMIT_TENANT_PER_MINUTE)

    _tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    state = _state(tenant_id, conversation_id)
    assert state["sent"] == 0, (
        "a message went out while the tenant's minute was spent; the "
        "conversation's own room is not the only limit"
    )
    assert state["tasks"] == 1
    assert state["withheld"] == 2
    assert state["task_scope"] == "tenant", (
        "the task blames the conversation for a tenant-wide limit -- the person "
        "reading it would go looking in the wrong place"
    )
    assert state["audit_scope"] == "tenant", (
        "rate_limited_total{scope} would count this under the wrong label"
    )


def test_rows_written_before_the_audience_key_existed_still_fill_the_window(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """The gate counts what `is_customer_row` calls a customer row — all of it.

    **The two halves of one rule drifted the moment they were written apart**
    (independent review 2026-09-08, P2-2). `is_customer_row` treats a command
    with no `audience` key as a customer message, and it has to: the key is
    younger than the rows (`35f4770` added it), so everything older carries none
    and every one of those is a customer message. The gate counted with
    `command->>'audience' = 'customer'`, which is **NULL** for exactly those
    rows — so they filled the window without being counted, and the strict half
    was the one deciding whether to let a message out.

    Measured before the fix: six rows in the window, three counted, a fourth
    message let out against a limit of three.

    The top-up here is key-less, so a gate reading the old way sees the window
    one **turn** short and lets a whole extra turn out instead of withholding it.
    """

    from test_m1a_no_send import _accept_customer_message, _drive  # noqa: PLC0415
    from test_multi_intent_generation import TwoSendsInference, _seed  # noqa: PLC0415

    tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())
    _spend_budget(
        tenant_id,
        conversation_id,
        count=RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN,
        audience=None,
    )

    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    _drive(settings=settings, inference=TwoSendsInference())

    state = _state(tenant_id, conversation_id)
    assert state["sent"] == TWO_SENDS_PER_TURN + (
        RATE_LIMIT_CONVERSATION_TURNS_PER_MINUTE - ONE_DRIVEN_TURN
    ), (
        "the window was full of rows written before the `audience` key existed "
        "and the gate did not count them, so it let a whole turn out over the limit"
    )
    assert state["tasks"] == 1
    assert state["withheld"] == 2
