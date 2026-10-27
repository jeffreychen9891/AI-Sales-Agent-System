"""T44: a turn may propose more than one intent.

The generation step accepted exactly one, and refused everything else with
`GENERATION_INTENT_COUNT_UNSUPPORTED`. The RFC's intent saga has always allowed
a turn to raise several, so the restriction was the implementation deviating
from the blueprint -- which is why the ruling (user, 2026-09-04) is to change
the code and leave the blueprint alone.

The ruling names four things and only four:

1. accept several proposals;
2. run **each** through the full policy chain;
3. a refusal of one leaves the others alone;
4. the failure record can answer *which* one failed.

**Items 1 and 2 are here. Items 3 and 4 are not** -- they need a turn holding
one sendable and one unsendable proposal, and no strategy in either synthetic
profile allows two intent kinds, so that turn is refused two guards earlier.
They are proved at the unit level instead (`tests/contracts/
test_proposal_decision_unit.py`) and the gap is registered as **T101**; the
comment at the bottom of this file says so where somebody looking for them will
be standing.

Ordering between accepted intents, partial-failure semantics and atomicity are
out of scope, and there is no test here that would have to be rewritten when
they arrive.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.worker.inference import DeterministicMockInference
from psycopg import Connection
from psycopg.rows import dict_row

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

from test_m1a_no_send import _accept_customer_message, _drive  # noqa: E402

from tests.wecom_proxy_callbacks import new_conversation  # noqa: E402


def _seed(settings: Any, seeded_database: dict[str, Any]) -> tuple[UUID, UUID]:
    """One accepted customer message, ready for the pipeline to pick up."""

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    _accept_customer_message(
        settings=settings, seeded_database=seeded_database, conversation_id=conversation_id
    )
    return tenant_id, conversation_id


#: The second proposal's text. Named so the assertions can say *which* message
#: they found rather than "two rows exist".
SECOND_MESSAGE_TEXT = "And one more thing."


class TwoSendsInference(DeterministicMockInference):
    """Two well-formed `send_message` proposals in one turn.

    **The plan has to sanction two, not just the generation.** The policy chain
    already refuses a generation that exceeds what the plan authorised
    (`GENERATION_DUPLICATED_INTENT`, `inference_validation.py`), and that guard
    is older than this batch and stays: a model that may add intents the plan
    never proposed is the thing the contract exists to stop. So "a turn may
    raise several intents" means the *plan* raises several candidates and the
    generation fills them in -- overriding only `generate` produced a document
    that the chain was right to refuse, which is what the first run of these
    tests actually showed.
    """

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        document = super().plan(**kwargs)
        first = document["candidate_intents"][0]
        document["candidate_intents"] = [
            first,
            # A distinct `idempotency_basis`: two candidates that are identical
            # down to their basis are one candidate written twice.
            {**first, "idempotency_basis": f"{first['idempotency_basis']}:2"},
        ]
        return document

    def generate(self, **kwargs: Any) -> dict[str, Any]:
        document = super().generate(**kwargs)
        first = document["proposed_intents"][0]
        second = {
            **first,
            "typed_payload": {
                **first["typed_payload"],
                "content": {"type": "text", "text": SECOND_MESSAGE_TEXT},
            },
        }
        document["proposed_intents"] = [first, second]
        return document


def _rows(conversation_id: UUID, sql: str) -> list[dict[str, Any]]:
    """Read with the migration role, which can see every table these assert on."""

    url = os.environ["FASTAGENT_MIGRATION_DATABASE_URL"]
    with Connection.connect(url, row_factory=dict_row) as connection:
        return connection.execute(sql, (conversation_id,)).fetchall()


def test_two_proposals_become_two_intents_and_two_outbox_rows(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """Ruling item 1, and the collision it would have hit on the way.

    The idempotency key used to be `send_message:{event_id}` -- unique only
    while a turn could raise one intent. Two proposals from one event would have
    collided on the outbox's unique key, and the second insert would have failed
    *after* the first was written. The key now carries each plan candidate's
    `idempotency_basis`, which is the "business identity" the RFC's field table
    asks for rather than the proposal's position in a JSON array.

    **The content assertions are the ones with teeth** (independent review
    2026-09-05, P2-3). Counting rows and keys passes on the single most likely
    way to get this wrong -- writing `proposals[0]`'s payload twice -- because
    two rows still exist and their keys still differ. So this reads the text
    back out and pins which text belongs to which proposal.
    """

    tenant_id, conversation_id = _seed(settings, seeded_database)

    result = _drive(settings=settings, inference=TwoSendsInference())

    assert result is not None and result.status == "consumed"
    assert len(result.intent_ids) == 2
    assert len(result.outbox_ids) == 2

    outbox = _rows(
        conversation_id,
        "SELECT idempotency_key, command FROM outbox_messages WHERE conversation_id = %s"
        " ORDER BY idempotency_key",
    )
    assert len(outbox) == 2
    assert len({row["idempotency_key"] for row in outbox}) == 2, "the keys must differ"
    assert [row["command"]["audience"] for row in outbox] == ["customer", "customer"]

    # The two messages are two *different* messages, and each one's text is the
    # one its own proposal carried. `TwoSendsInference` gives the second the
    # sentinel below, so "wrote the first payload twice" is distinguishable
    # from "wrote both".
    texts = [row["command"]["content"]["text"] for row in outbox]
    assert len(set(texts)) == 2, f"both rows carry the same text: {texts}"
    assert SECOND_MESSAGE_TEXT in texts
    # ...and the key that carries the second candidate's basis is the row with
    # the second proposal's text, so the pairing is asserted rather than assumed.
    by_key = {row["idempotency_key"]: row["command"]["content"]["text"] for row in outbox}
    second_key = next(key for key in by_key if key.endswith(":2"))
    assert by_key[second_key] == SECOND_MESSAGE_TEXT
    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        intents = connection.execute(
            "SELECT status FROM intent_records WHERE conversation_id = %s", (conversation_id,)
        ).fetchall()
    assert [row["status"] for row in intents] == ["APPROVED", "APPROVED"]


def test_each_proposal_gets_its_own_policy_decision(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """Ruling item 2: the chain runs per proposal, not once for the turn.

    One decision covering two intents would be an audit record that cannot say
    what was decided about the second -- and the obligations it carries
    (`audit`, `redact`) are per-intent obligations.
    """

    _, conversation_id = _seed(settings, seeded_database)

    _drive(settings=settings, inference=TwoSendsInference())

    decisions = _rows(
        conversation_id,
        """
        SELECT effect, reason_code, intent_id, context FROM policy_decisions
         WHERE conversation_id = %s AND hook = 'pre_execute'
         ORDER BY (context->>'proposal_ordinal')::int
        """,
    )
    assert len(decisions) == 2
    assert [row["effect"] for row in decisions] == ["allow", "allow"]
    assert [row["context"]["proposal_ordinal"] for row in decisions] == [0, 1]
    # Each names the intent it decided about, and the two are different intents.
    assert len({row["intent_id"] for row in decisions}) == 2


# --------------------------------------------------------------------------
# Ruling items 3 and 4 are **not** here, and that is the considered part.
#
# They were here, driven by a turn holding one sendable proposal and one
# `remember`. That turn cannot happen: the chain refuses it two guards earlier
# with `GENERATION_ADDED_INTENT`, because `remember` is not a candidate any
# strategy in either synthetic profile may propose. Making it possible means
# changing what a strategy is allowed to propose -- a product decision taken to
# make a test path exist, which is the test driving the design.
#
# User ruling 2026-09-05: prove those two at the unit level and register the
# gap. They now live in `tests/contracts/test_proposal_decision_unit.py`, whose
# docstring states in its first paragraph that it verifies a function and not a
# reachable path. The gap is **T101**, and its trigger is written down: the
# first time any profile declares a strategy allowing more than one intent
# kind, an end-to-end case should replace the unit ones.


# --------------------------------------------------------------------------
# P1-1: the order of two messages from one turn is **undefined**, on purpose
# --------------------------------------------------------------------------


def test_the_delivery_order_within_one_turn_is_the_turn_s_order(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """T109, closed. **This case replaces the one that pinned the coin flip.**

    What stood here until B1-3 asserted that the order was *not* defined, and
    said in its own docstring: "a future batch that makes the order
    deterministic will have to delete this test -- and in deleting it will read
    why it existed." This is that batch, and this is that reading.

    **The mechanism it recorded is unchanged and still asserted below**: both
    rows of one turn carry the same `created_at`, because `now()` is the
    transaction timestamp and both are written in one transaction (the
    independent review measured 8/8 on 2026-09-05). What changed is that the
    tiebreak is no longer `uuid4()`: migration 044 gives the row an `ordinal`,
    the writer fills it with the proposal's position in the turn, and the
    dispatcher's claim orders by `created_at, ordinal, id`.

    **The old failure, stated once more so it is not lost with the old test**:
    the review saw the delivered order inverted 5 times in 8. What the model
    wrote as "here is the answer" followed by "and one more thing" reached
    roughly half of customers back to front.
    """

    _, conversation_id = _seed(settings, seeded_database)

    _drive(settings=settings, inference=TwoSendsInference())

    rows = _rows(
        conversation_id,
        "SELECT created_at, id, ordinal FROM outbox_messages"
        " WHERE conversation_id = %s ORDER BY created_at, ordinal, id",
    )
    assert len(rows) == 2

    # The mechanism the old test recorded: one transaction, one timestamp.
    assert rows[0]["created_at"] == rows[1]["created_at"], (
        "if these ever differ, the transaction boundary changed and the ordering "
        "story with it -- re-read T109 before assuming this test still describes reality"
    )
    assert rows[0]["id"] != rows[1]["id"]
    # ...and the fix: the turn's own sequence, in a column the dispatcher reads.
    #
    # ⚠️ **钉的是「递增且互不相同」，不是取值**（ADR-0152 改了取值）：
    # `ordinal` 现在是「提案位置 × 步长 + 段位置」，两条独立提案因此是
    # 0 与 100。**这条用例守的一直是顺序**，而取值是实现的事。
    ordinals = [row["ordinal"] for row in rows]
    assert ordinals == sorted(ordinals), f"出站顺序不是这一轮的顺序：{ordinals}"
    assert len(set(ordinals)) == len(ordinals), f"两行拿到了同一个 ordinal：{ordinals}"


def test_the_dispatcher_claims_them_in_that_order(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """Driven through `_claim_one`, not by repeating its `ORDER BY` here.

    A case that re-runs the query it is testing asserts that the query equals
    itself. This one asks the dispatcher for a row twice and checks what it
    hands back -- which is the thing a customer's reading order actually
    depends on.
    """

    from fastagent_v2.worker.pipeline import OutboxDispatcher

    _, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=None,  # type: ignore[arg-type] - only the claim is exercised
        dispatcher_id="t109-order",
    )

    claimed: list[int] = []
    for _ in range(2):
        result = dispatcher._claim_one()
        assert result is not None, "both rows of the turn should be claimable"
        _tenant, row = result
        claimed.append(int(row["ordinal"]))

    # ⚠️ **钉的是顺序，不是取值**（ADR-0152 改了取值）。
    # `ordinal` 从「这条消息在这一轮里的位置」变成了
    # 「**提案位置 × 步长 + 段位置**」—— 两条独立提案因此是 0 与 100，
    # 而不再是 0 与 1。**递增没变，顺序没变**，而这条用例守的一直是顺序：
    # T109 之前 tiebreak 是 `uuid4()`，实测 8 轮里颠倒 5 次。
    assert claimed == sorted(claimed), (
        "the claim handed the turn's messages back out of order; before T109 the "
        f"tiebreak was `uuid4()` and this inverted about half the time: {claimed}"
    )
    assert len(set(claimed)) == 2, f"两条提案拿到了同一个 ordinal：{claimed}"


def test_a_later_segment_waits_for_the_one_before_it(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """⚠️ **分段要排队**（ADR-0151，用户 2026-09-23「本来每段话之间就应该有延迟」）。

    上面那条钉的是「同一轮的两条**各自独立的**消息都取得到」——
    本条钉的是相反的那一面：**同一条消息的后一段，前一段没送到就取不到**。

    ## 没有这一条时会发生什么

    迁移 `044` 自己写过：ordinal 0 发到一半抛了，它在租约内保持 `SENDING`，
    下一趟取到 ordinal 1 发了出去，ordinal 0 等租约过期才走 ——
    「客户读到第二句，三十秒后才读到第一句」。
    对两条独立消息那是难看；**对同一句话的分段，是「因为…」和「所以…」倒着到**。

    ## 判据是 `pace_ms > 0`，不是「同一轮」

    **第一版把谓词写宽了**（写成「同轮里序号更小的行没送到就等」），
    于是 `send_message` + `send_material` 那种也被卡住，全量门禁 7 条红。
    收窄成「前一行还有下一段要来」之后，独立的两条照旧各取各的 ——
    上面那条用例守着那一半。
    """

    from fastagent_v2.worker.pipeline import OutboxDispatcher

    _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    # ⚠️ **用 migration 角色连，不用 worker 角色。**
    # 本文件的 `_rows` 上面写着理由：「which can see every table these
    # assert on」—— worker 角色受 RLS 管，不带 `app.tenant_id` 时**一行都看不见**。
    # 第一版拿 worker 角色去 UPDATE，改到 **0 行**，而整条用例照样跑完了：
    # 「第二段取不到」在没有分段的情况下**同样为真**。
    # 下面那条 `rowcount` 自检是那一跑之后加的，它当场把成因说清了 ——
    # 没有它，这条用例会以一个假的理由绿着。
    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        pending = connection.execute(
            "SELECT id, ordinal FROM outbox_messages WHERE status = 'PENDING' ORDER BY ordinal"
        ).fetchall()
        assert len(pending) == 2, f"这一轮排了 {len(pending)} 行，不是两行"
        # 把这一轮改造成「一条分了两段的消息」：第一行留一段停顿。
        touched = connection.execute(
            "UPDATE outbox_messages SET pace_ms = 60000 WHERE id = %s",
            (pending[0]["id"],),
        ).rowcount
        connection.commit()
        # ⚠️ **先证明布景真的搭起来了。** 改到 0 行的话，下面两条断言
        # 验的是一个没发生过的场景 —— 而「第二段取不到」在没有分段时
        # **同样为真**，于是它会安静地绿。本仓那条「守卫本身也可能是空的」。
        assert touched == 1, f"那条 UPDATE 改到了 {touched} 行 —— 这一轮不是两行、或者会话号不对"

    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=None,  # type: ignore[arg-type] - only the claim is exercised
        dispatcher_id="adr0151-pacing",
    )

    first = dispatcher._claim_one()
    assert first is not None, "第一段自己不该被拦住"
    assert int(first[1]["ordinal"]) == 0

    assert dispatcher._claim_one() is None, (
        "第一段还没送到，第二段就被取走了 —— 它会先于第一段落到客户那里"
    )


def test_an_unknown_first_segment_does_not_pin_the_rest(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """⚠️ **运维缺陷 14 的现场，而这是它的正面。**

    实测那一次：

    ```
    ordinal 0 | pace_ms 800 | UNKNOWN | "不会。"
    ordinal 1 | pace_ms 0   | PENDING | "只有您明确说开，我们才会开通；…"
    ```

    `UNKNOWN` 是供应商回执（「我们问了，而它说不知道」），**它是终态** ——
    没有任何路径把它推进到 `DELIVERED`。而 `074` 第一版的谓词要求
    「前一段已 `DELIVERED`」，于是第 1 段**永远**停在 `PENDING`，
    客户只看到「不会。」三个字。

    **用户 2026-09-23 裁定 A**：前一段进入任意终态就放行后一段 ——
    「本来就有兜底就行」（首段没送到那条已经有 `manual_task` 接着）。

    ## 为什么这是「一次判定」的一部分

    判定已经说过「这条要发」了。投递层再替它判一次「上一段算不算数」，
    **就是「分段之后还要判定」的投递层版本**。

    ## 反侧在上一条用例里

    `test_a_later_segment_waits_for_the_one_before_it` 钉的是
    「前一段**还在投递中**（`SENDING`）时后一段要等」。
    两条合起来才是完整的判据：**等的是「还没走完」，不是「没送到」。**
    """

    from fastagent_v2.worker.pipeline import OutboxDispatcher

    _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    with Connection.connect(
        os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row
    ) as connection:
        pending = connection.execute(
            "SELECT id, ordinal FROM outbox_messages WHERE status = 'PENDING' ORDER BY ordinal"
        ).fetchall()
        assert len(pending) == 2, f"这一轮排了 {len(pending)} 行，不是两行"
        # 第 0 段：留了停顿，而投递结果是 UNKNOWN —— 一个**永远不会**
        # 变成 DELIVERED 的终态。停顿设成 0 是为了把「等停顿」这半排除掉，
        # 本条要验的是「等终态」那半。
        touched = connection.execute(
            "UPDATE outbox_messages SET pace_ms = 0, status = 'UNKNOWN' WHERE id = %s",
            (pending[0]["id"],),
        ).rowcount
        connection.commit()
        assert touched == 1

    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=None,  # type: ignore[arg-type] - only the claim is exercised
        dispatcher_id="adr0152-unknown",
    )

    claimed = dispatcher._claim_one()
    assert claimed is not None, (
        "第一段进了 UNKNOWN（终态）而第二段仍然取不到 —— 它会永远停在 PENDING，"
        "客户只读到半句话"
    )
    assert int(claimed[1]["ordinal"]) == pending[1]["ordinal"]


def test_the_claim_follows_ordinal_even_when_the_ids_disagree(
    settings: Any, seeded_database: dict[str, Any]
) -> None:
    """The decisive form of the case above, and the reason it exists.

    `..._claims_them_in_that_order` drives the real turn and checks the claim
    hands back 0 then 1. **That case catches a regression only about half the
    time**: undo the `ordinal` in the `ORDER BY` and the tiebreak falls back to
    `uuid4()`, which agrees with the intended order in roughly half of runs.
    A reverse probe measured exactly that -- it came back red, but on a coin
    flip, and a guard whose failure is a coin flip is half a guard.

    So this one **makes the two orders disagree on purpose**: it assigns the
    smaller-id row position 1 and the larger-id row position 0. Any ordering
    that falls through to `id` now returns them backwards, every run. What is
    asserted is not "the rows come back sorted" but "**the claim follows the
    turn's position and not the primary key**".
    """

    from fastagent_v2.worker.pipeline import OutboxDispatcher

    tenant_id, conversation_id = _seed(settings, seeded_database)
    _drive(settings=settings, inference=TwoSendsInference())

    with Database(settings.worker_database_url).transaction(tenant_id=tenant_id) as connection:
        rows = connection.execute(
            "SELECT id FROM outbox_messages WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        ).fetchall()
        assert len(rows) == 2
        smaller, larger = UUID(str(rows[0]["id"])), UUID(str(rows[1]["id"]))
        # Smaller id gets the later position: id-order and ordinal-order now
        # point opposite ways, so only one of them can be the one being used.
        connection.execute(
            "UPDATE outbox_messages SET ordinal = 1 WHERE tenant_id = %s AND id = %s",
            (tenant_id, smaller),
        )
        connection.execute(
            "UPDATE outbox_messages SET ordinal = 0 WHERE tenant_id = %s AND id = %s",
            (tenant_id, larger),
        )

    dispatcher = OutboxDispatcher(
        database=Database(settings.worker_database_url),
        channel=None,  # type: ignore[arg-type] - only the claim is exercised
        dispatcher_id="t109-disagree",
    )
    first = dispatcher._claim_one()
    assert first is not None
    _tenant, row = first

    assert UUID(str(row["id"])) == larger, (
        "the claim returned the smaller id first, so it is still ordering by the "
        "primary key -- `ordinal` is either missing from the ORDER BY or is not "
        "being read"
    )
    assert int(row["ordinal"]) == 0
