"""T183, against a real database: the deadline tracks the lease, not the anchor.

The arithmetic cases in `tests/contracts/test_lease_clock_arithmetic.py` are
handed their two database readings, so they cannot see which SQL produced them
-- the difference between `now()` and `clock_timestamp()` is invisible to them.
This one runs `_claim_one` and measures what comes out.

**What makes it discriminating is the delay.** `lease_expires_at` is set from
`now()`, the instant the *transaction* opened, so a claim statement that runs
late is being handed a lease that has already been running. With `now()` as the
second reading the difference is the constant `OUTBOX_LEASE_SECONDS` and the
deadline lands that far past an anchor read after the delay -- overstated by the
delay, in the direction that sends the customer the message twice. With
`clock_timestamp()` the spent time comes off and the deadline lands on the true
expiry.

Ordinary test transactions are too fast for the two to be told apart, so the
dispatcher below spends two seconds inside the transaction on purpose. That is
not an artificial worry: `_dead_letter_exhausted` runs there, it has no
`SKIP LOCKED`, and a second dispatcher waiting on those locks waits for the
whole of the first one's retirement work (independent review P1-1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastagent_v2.ingress.adapter import WeComProxyIngress
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastagent_v2.worker.inference import inference_for_provider
from fastagent_v2.worker.pipeline import (
    OUTBOX_LEASE_SECONDS,
    InboxProcessor,
    OutboxDispatcher,
)

from tests.wecom_proxy_callbacks import callback, new_conversation, sender_of

TRANSACTION_DELAY_SECONDS = 2.0


class SlowRetiringDispatcher(OutboxDispatcher):
    """A dispatcher whose claim transaction takes measurable time to get going.

    The delay goes where the real one goes -- inside the transaction, before the
    claim -- by extending the step that holds the locks in production.

    It is pinned to one tenant because `_claim_one` opens a transaction per
    tenant and the delay would fire in each of them. The anchor the assertion
    uses is read before the loop, so a tenant iterated first would put its whole
    delay between the anchor and the transaction that actually claims -- and the
    measurement would then be of the fixture's tenant ordering rather than of
    the clock.
    """

    def __init__(self, *, tenant_id: UUID, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._only_tenant = tenant_id

    def _tenant_ids(self) -> list[UUID]:
        return [self._only_tenant]

    def _dead_letter_exhausted(self, connection: Any, tenant_id: UUID) -> None:
        super()._dead_letter_exhausted(connection, tenant_id)
        connection.execute("SELECT pg_sleep(%s)", (TRANSACTION_DELAY_SECONDS,))


def test_a_slow_claim_transaction_does_not_extend_the_lease_it_believes_it_holds(
    settings: Settings, seeded_database: dict[str, object]
) -> None:
    tenant_id = UUID(str(seeded_database["tenant_a"]))
    conversation_id = new_conversation(seeded_database["tenant_a"])
    queue = PointerQueue(settings.queue_redis_url)
    WeComProxyIngress(
        database=Database(settings.ingress_database_url),
        pointer_queue=queue,
        runtime_deadline_seconds=settings.runtime_deadline_seconds,
    ).accept(
        config_id="config-a",
        path_token=str(seeded_database["channel_token"]),
        payload=callback(
            external_event_id="lease-deadline-clock",
            sender_id=sender_of(conversation_id),
            text="the deadline must come off the database's clock",
        ),
    )
    persisted = InboxProcessor(
        database=Database(settings.worker_database_url),
        pointer_queue=queue,
        inference=inference_for_provider(settings.model_provider),
        worker_id="lease-clock-worker",
    ).process_one(block_ms=500)
    assert persisted is not None

    dispatcher = SlowRetiringDispatcher(
        tenant_id=tenant_id,
        database=Database(settings.worker_database_url),
        channel=None,  # type: ignore[arg-type]
        dispatcher_id="lease-clock-sender",
    )
    anchor = datetime.now(UTC)
    claimed = dispatcher._claim_one()
    assert claimed is not None, "the seeded customer row must be claimable"
    claimed_tenant, row = claimed
    assert claimed_tenant == tenant_id

    remaining = (row["lease_deadline_at"] - anchor).total_seconds()

    # The lease started when the transaction opened, a hair after `anchor`, and
    # runs `OUTBOX_LEASE_SECONDS` from there. The delay happens *inside* that
    # window; it does not extend it.
    assert remaining < OUTBOX_LEASE_SECONDS + 0.5, (
        f"the deadline is {remaining}s out but the lease only has "
        f"{OUTBOX_LEASE_SECONDS}s in it -- the {TRANSACTION_DELAY_SECONDS}s spent "
        "before the claim was added to the lease instead of coming out of it"
    )
    assert remaining > OUTBOX_LEASE_SECONDS - TRANSACTION_DELAY_SECONDS, (
        "and it must not throw the lease away either; the anchor is a process "
        "reading taken before the statement, so the only loss should be the "
        "round trip"
    )
