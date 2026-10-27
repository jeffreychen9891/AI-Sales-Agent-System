"""A quiet-hours refusal lands in `policy_decisions.next_allowed_at`. **B4-5 / ADR-0119.**

`tests/contracts/test_quiet_hours_refuse_and_say_when.py` proves the arithmetic.
**It cannot prove this**, because the contract cases construct a verdict and read
it back in the same breath -- they never touch the column, and the column is the
whole of ADR-0117 §四's requirement that a field arrive with somewhere to land.

The failure this guards against is exactly the one ADR-0117 §四 was written
about: `next_allowed_at` computed, threaded into the policy contract, validated,
and then **dropped**, because nothing wrote it anywhere. That version passes
every contract case.

So this drives a real timer through `InboxProcessor` and then reads the column
with SQL. **Both directions are asserted**: a quiet-hours refusal is never NULL,
and an exit-signal refusal always is -- because `NULL` here is the statement
「这条拒绝没有自动失效的时刻」 rather than an unfilled field.
"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.channel import SendCommand, SendReceipt
from fastagent_v2.worker.inference import DeterministicMockInference
from fastagent_v2.worker.pipeline import InboxProcessor, OutboxDispatcher
from fastagent_v2.worker.proactivity import EXIT_PERMANENT_REASON, QUIET_HOURS_REASON
from fastagent_v2.worker.scheduler import Scheduler
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tests.wecom_proxy_callbacks import new_conversation

pytestmark = [pytest.mark.integration]


class AcceptingAdapter:
    def send(
        self, *, command: SendCommand, idempotency_key: str, deadline_at: datetime
    ) -> SendReceipt:
        return SendReceipt("accepted", f"op-{uuid4()}", {"adapter": "test"})

    def reconcile(self, *, provider_operation_id: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")

    def reconcile_by_key(self, *, idempotency_key: str, deadline_at: datetime) -> SendReceipt:
        raise AssertionError("an accepted send must not be reconciled")


def _migration_connection() -> Connection[Any]:
    return Connection.connect(os.environ["FASTAGENT_MIGRATION_DATABASE_URL"], row_factory=dict_row)


def _deliver_one(settings: Settings, seeded: dict[str, Any], conversation_id: UUID) -> None:
    from test_retry_budget import _deliver_to_outbox  # noqa: PLC0415 - suite-local helper

    _deliver_to_outbox(settings=settings, seeded_database=seeded, conversation_id=conversation_id)
    assert (
        OutboxDispatcher(
            database=Database(settings.worker_database_url),
            channel=AcceptingAdapter(),
            dispatcher_id="b45-seed",
        ).dispatch_one()
        is not None
    )


def _publish_a_quiet_release(tenant_id: UUID, seeded: dict[str, Any]) -> datetime:
    """Bind the channel to a release whose quiet window **covers right now**.

    **The window is computed from the wall clock rather than written down**, and
    that is the only way this case can be deterministic: `_proactivity_verdict`
    reads `datetime.now(UTC)`, so a fixed 22:30-08:00 window would make the
    result depend on what time the suite happens to run. A window of
    `now - 1h` to `now + 1h` in **UTC** covers this instant whatever hour it is,
    including across midnight -- the wrap is the shipped shape and is exercised
    by the contract cases.

    **A second release rather than an edited one**: `profile_releases` is
    append-only (migration `006` puts a trigger on it and withholds UPDATE from
    every runtime role), so the production answer to 「这份 release 不对」 is to
    publish another and rebind.

    Returns the instant the window lifts, which is what the column must hold.
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    now = datetime.now(UTC)
    opens = now - timedelta(hours=1)
    lifts = (now + timedelta(hours=1)).replace(second=0, microsecond=0)
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, UUID(str(seeded["profile_release"]))),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row["profile"])
        profile["proactivity_policy"]["quiet_hours"] = {
            "timezone": "UTC",
            "ranges": [{"start": opens.strftime("%H:%M"), "end": lifts.strftime("%H:%M")}],
        }
        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        # ⚠️ 在架 third 自缺陷 19 批起就是 1.0.1 —— 夹具发的「新版」要避开它。
        profile["identity"]["version"] = "1.0.5"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                release_id,
                row["profile_id"],
                "1.0.5",
                canonical_hash(profile),
                Jsonb(profile),
            ),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, tenant_id),
        )
        connection.commit()
    return lifts


def _publish_a_release_whose_quiet_window_is_ahead(
    tenant_id: UUID, seeded: dict[str, Any]
) -> tuple[datetime, datetime]:
    """A quiet window that has **not started yet**, so the turn is allowed.

    The sibling helper above covers 「timer 落在窗口里，当场不发」. This one covers
    the other side: the turn is let through, the row is queued, and **the quiet
    window opening ahead of it becomes the row's deadline**. Returns
    `(opens, lifts)` -- what the command must carry as `window_expires_at` and
    `window_lifts_at`.
    """

    from fastagent_v2.profiles.registry import canonical_hash  # noqa: PLC0415 - test-only

    now = datetime.now(UTC)
    # **Ten minutes, not an hour**: the row is an in-session continuation, so
    # its active window closes 30 minutes after the customer's message. A quiet
    # window opening after that would lose the `min()` and this case would be
    # asserting the wrong source -- which is exactly how its first version
    # failed.
    opens = (now + timedelta(minutes=10)).replace(second=0, microsecond=0)
    lifts = (now + timedelta(hours=3)).replace(second=0, microsecond=0)
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT profile, profile_id FROM profile_releases WHERE tenant_id = %s AND id = %s",
            (tenant_id, UUID(str(seeded["profile_release"]))),
        ).fetchone()
        assert row is not None
        profile = copy.deepcopy(row["profile"])
        profile["proactivity_policy"]["quiet_hours"] = {
            "timezone": "UTC",
            "ranges": [{"start": opens.strftime("%H:%M"), "end": lifts.strftime("%H:%M")}],
        }
        release_id = uuid4()
        profile["identity"]["release_id"] = str(release_id)
        profile["identity"]["version"] = "1.0.5"
        connection.execute(
            "INSERT INTO profile_releases(tenant_id, id, profile_id, version, content_hash,"
            "  profile) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                release_id,
                row["profile_id"],
                "1.0.5",
                canonical_hash(profile),
                Jsonb(profile),
            ),
        )
        connection.execute(
            "UPDATE channel_bindings SET profile_release_id = %s WHERE tenant_id = %s",
            (release_id, tenant_id),
        )
        connection.commit()
    return opens, lifts


def _record_a_customer_opt_out(tenant_id: UUID, conversation_id: UUID) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None
        state = copy.deepcopy(row["state"])
        state["pending_exit_signal"] = {
            "event_id": str(uuid4()),
            "relation": "exit",
            "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        connection.commit()


def _make_it_a_wake_up(tenant_id: UUID, conversation_id: UUID) -> None:
    """把这通对话推远，让下一个定时回合成为一次**唤醒**而不是延续。

    ⚠️ **2026-09-21 加的，而它补的是这份夹具一直缺的那一半**：
    在此之前 `last_inbound_at` 离现在很近，那一轮是**会话内延续** ——
    而静默时段那道闸当时**不分 classification**，所以用哪种夹具都 deny，
    **这条用例一直在用延续的夹具钉唤醒的规则**。

    ADR-0148 把闸收到 `re_engagement` 上（用户裁定：「静默时间应该只限制
    唤醒对话，而不是正常销售推进 —— 很有可能客户就是晚上进来的」），
    延续因此放行 —— 于是这一行必须在，否则本条钉的东西整个落空。
    """

    # 照本文件已有的那个写法：**Python 侧构造时间戳**，不在 SQL 里拼 ——
    # `::text` 给的是 `2026-09-20 11:53:41+00`（空格分隔、没有 `T`），
    # 而契约那一格是 `format: date-time`，读回来时当场被拒。
    stamp = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT state FROM conversation_aggregates WHERE tenant_id = %s AND id = %s",
            (tenant_id, conversation_id),
        ).fetchone()
        assert row is not None, "这通对话不在库里"
        state = dict(row["state"])
        state["last_inbound_at"] = stamp
        connection.execute(
            "UPDATE conversation_aggregates SET state = %s WHERE tenant_id = %s AND id = %s",
            (Jsonb(state), tenant_id, conversation_id),
        )
        # ⚠️ **而且这一行不能是 `silence_check`。**
        #
        # 「超窗的 `silence_check` 必拒」（RFC §6.4：不得借旧 silence event
        # 绕过再触达配额）**排在静默时段之前** —— 把会话推远之后，
        # 那一条会先把这一轮截走，于是本条读到的理由码不是 `QUIET_HOURS`。
        # 实测过一次（门禁第三轮：「the refusal did not name itself as a
        # quiet-hours refusal」）。
        #
        # `goal_stall` 是同样合法的一种定时事件，而它不在那条禁令的范围里。
        connection.execute(
            """
            UPDATE scheduled_events SET kind = 'goal_stall'
             WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'
            """,
            (tenant_id, conversation_id),
        )
        connection.commit()


def _fire_the_timer(settings: Settings, tenant_id: UUID, conversation_id: UUID) -> None:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT due_at FROM scheduled_events"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the delivery left no pending timer to fire"
    assert Scheduler(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        scheduler_id="b45-scheduler",
        runtime_deadline_seconds=600,
    ).enqueue_due(now=row["due_at"] + timedelta(seconds=1))
    InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=PointerQueue(settings.queue_redis_url),
        inference=DeterministicMockInference(),
        worker_id="b45-worker",
    ).process_one(block_ms=1000)


def _denials(tenant_id: UUID, conversation_id: UUID, reason_code: str) -> list[dict[str, Any]]:
    with _migration_connection() as connection:
        return connection.execute(
            "SELECT reason_code, next_allowed_at FROM policy_decisions"
            " WHERE tenant_id = %s AND conversation_id = %s AND effect = 'deny'"
            "   AND reason_code = %s",
            (tenant_id, conversation_id, reason_code),
        ).fetchall()


def _queued(tenant_id: UUID, conversation_id: UUID) -> int:
    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND status = 'PENDING'",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def test_a_timer_inside_quiet_hours_writes_when_it_may_send(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The whole of ADR-0119 on the path production takes.

    A `silence_check` falls due while the merchant's quiet window is open.
    Nothing goes out, the refusal names itself, and **the instant it lifts is in
    the column** -- not in a string somebody would have to parse, which is the
    shape ADR-0117 §四 was written to stop.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    # **Publish before delivering, and the order is not arbitrary.** The first
    # version of this case delivered first and rebound afterwards, and the timer
    # went out: `prepare` resolves the profile from `inbox_events.
    # profile_release_id`, so a timer reads the release **pinned when the
    # conversation armed it**, never the currently bound one. That is T205's
    # rule working -- a mid-conversation publish must not move a customer's
    # window -- so the failure was the test's, and it is worth leaving written
    # down: **to change what a timer sees, the release has to be in place before
    # the turn that arms it.**
    lifts = _publish_a_quiet_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    # ⚠️ **这一轮要是一次唤醒**，见 `_make_it_a_wake_up` —— 静默时段只挡那一种。
    _make_it_a_wake_up(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    assert _queued(tenant_id, conversation_id) == 0, "a message went out inside quiet hours"
    rows = _denials(tenant_id, conversation_id, QUIET_HOURS_REASON)
    assert rows, "the refusal did not name itself as a quiet-hours refusal"
    # **`all`, not a filter then `assert stored`** (independent re-review
    # 2026-09-09, P2-1). The first version dropped the `NULL` rows before
    # asserting anything, so it could only ever see the rows that were already
    # right -- and a quiet-hours refusal writes **two** rows (turn level and
    # per proposal), one of which was landing `NULL`. Its sibling below has
    # always used `all(...)`; **two cases guarding one column, one strict and
    # one loose, and the loose one sat on the defective side.**
    assert rows, "no quiet-hours refusal row was written"
    assert all(row["next_allowed_at"] is not None for row in rows), (
        f"a quiet-hours refusal stored NULL, which this column defines as "
        f"having no expiry at all: {rows}"
    )
    stored = [row["next_allowed_at"] for row in rows]
    # To the minute: `lifts_at` walks a minute at a time, and the window was
    # built from the same clock this assertion reads.
    assert all(abs((value - lifts).total_seconds()) <= 60 for value in stored), (
        f"the column holds {stored}, not the instant the window lifts ({lifts})"
    )


def test_an_exit_signal_refusal_leaves_the_column_null(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """The other direction, and the reason `NULL` is a statement rather than a gap.

    **Without this case the column could be a constant** -- always the quiet
    boundary -- and the batch would have delivered exactly the decorated field
    ADR-0117 §四 forbids, one level along. 「别再发了」 has no instant that
    un-says it, so the column must be empty for it.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    # Quiet hours are open **too**, so this also pins the order of the two
    # refusals: the exit signal is the more final one and wins the reason code,
    # and with it the empty column. Published before the delivery for the
    # pinning reason spelled out in the case above.
    _publish_a_quiet_release(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)
    _record_a_customer_opt_out(tenant_id, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    assert _queued(tenant_id, conversation_id) == 0
    rows = _denials(tenant_id, conversation_id, EXIT_PERMANENT_REASON)
    assert rows, "the refusal did not name itself as the permanent kind"
    assert all(row["next_allowed_at"] is None for row in rows), (
        "an opt-out was given an instant at which it expires"
    )
    assert not _denials(tenant_id, conversation_id, QUIET_HOURS_REASON), (
        "quiet hours outranked a standing refusal about the conversation"
    )


def test_the_queued_row_carries_when_its_quiet_window_opens_and_lifts(
    settings: Settings, seeded_database: dict[str, Any]
) -> None:
    """**The producer half of P1-1's fix**, which nothing else reaches.

    `tests/e2e/test_proactivity_window_reaches_the_row.py` proves the dispatcher
    writes the column -- but it builds `command.proactivity` **by hand**, so it
    cannot prove anything about the code that fills it in. Delete the write in
    `_persist_accepted_proposal` and that case stays green while every
    quiet-hours cancellation in production goes back to storing `NULL`.

    **This is B4-4d's P2-2 in a new place**: 「守卫在不在」 and 「送进守卫的东西在
    不在」 are two things, and the half that fails silently is the second one.

    So this drives a real timer with a quiet window **ahead of** the row -- the
    turn is allowed, the row is queued -- and reads what the command actually
    says.
    """

    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(tenant_id)
    opens, lifts = _publish_a_release_whose_quiet_window_is_ahead(tenant_id, seeded_database)
    _deliver_one(settings, seeded_database, conversation_id)

    _fire_the_timer(settings, tenant_id, conversation_id)

    with _migration_connection() as connection:
        row = connection.execute(
            "SELECT command FROM outbox_messages"
            " WHERE tenant_id = %s AND conversation_id = %s AND command ? 'proactivity'"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, conversation_id),
        ).fetchone()
    assert row is not None, "the timer turn produced no proactive row"
    block = row["command"]["proactivity"]

    def _at(key: str) -> datetime:
        return datetime.fromisoformat(str(block[key]).replace("Z", "+00:00"))

    assert _at("window_expires_at") == opens, "the row's deadline is not the quiet window opening"
    assert "window_lifts_at" in block, (
        "the row was expired by a window with an end, and the end did not travel with it"
    )
    assert _at("window_lifts_at") == lifts
