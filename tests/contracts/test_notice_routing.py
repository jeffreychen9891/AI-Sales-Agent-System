"""T90: a row that is not for the customer must never be sent to the customer.

**The accident this file exists to prevent, stated once.** The manager digest
borrows a conversation's id so its outbox row can satisfy a foreign key — and in
the `channel-adapter` contract that field *is* the address. A dispatcher written
straight from the contract would deliver a merchant's internal backlog summary
into that merchant's customer's chat. Nothing about the row says "do not send
this here"; the id is real and the send would succeed.

ADR-0095 §三 rules the fix in two halves, and both are asserted below: route by
`command.recipient`, and **refuse explicitly, with a reason code**, rather than
falling back to `conversation_id`. The refusal half matters more than it looks:
a silent fallback and a correct delivery are indistinguishable in every log the
platform keeps.
"""

from __future__ import annotations

import pytest
from fastagent_v2.worker.pipeline import RECIPIENT_FIELDS, unroutable_reason

CUSTOMER = {"audience": "customer", "content": {"type": "text", "text": "hello"}}
MANAGER = {
    "audience": "tenant_manager",
    "content": {"type": "text", "text": "backlog"},
    "recipient": {
        "channel": "wecom_proxy",
        "config_id": "config-a",
        "external_user_id": "manager-userid",
    },
}


def test_a_customer_row_needs_no_recipient() -> None:
    """The conversation *is* the address for a customer, and that stays true.

    If this ever started requiring a recipient, every customer message would be
    refused — which is why it is asserted rather than assumed.
    """

    assert unroutable_reason(CUSTOMER) is None
    assert unroutable_reason({"content": {}}) is None, "a row with no audience is a customer row"


def test_a_fully_addressed_manager_row_is_routable() -> None:
    assert unroutable_reason(MANAGER) is None


def test_a_manager_row_with_no_recipient_is_refused() -> None:
    """**This is the T90 case.** Without the check, this row goes to the customer."""

    command = {key: value for key, value in MANAGER.items() if key != "recipient"}
    assert unroutable_reason(command) == "missing"


@pytest.mark.parametrize("field", RECIPIENT_FIELDS)
def test_a_manager_row_missing_any_one_field_is_refused(field: str) -> None:
    """Half an address is not an address.

    `012` makes the triple the identity of a delivery target and both readers
    use it whole. A row missing `config_id` is not "mostly addressed" — the
    channel it names is a different merchant's binding, or none.
    """

    recipient = {key: value for key, value in MANAGER["recipient"].items() if key != field}
    reason = unroutable_reason({**MANAGER, "recipient": recipient})
    assert reason is not None and field in reason


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_a_blank_field_counts_as_missing(empty: object) -> None:
    """An empty string is what a half-filled configuration form produces.

    It reaches the same nobody a missing key does, and it looks configured while
    doing it — the failure ADR-0104's route validation refuses at the edge, and
    this is the same refusal one layer in, for rows written before that route
    existed.
    """

    recipient = {**MANAGER["recipient"], "external_user_id": empty}
    assert unroutable_reason({**MANAGER, "recipient": recipient}) is not None


def test_a_recipient_that_is_not_an_object_is_refused_rather_than_indexed() -> None:
    """A string here would raise inside the dispatcher, which is a 500 on a queue.

    The dispatcher's failure mode for an unexpected shape must be a refusal it
    can record, not an exception that leaves the row `SENDING` until its lease
    expires.
    """

    assert unroutable_reason({**MANAGER, "recipient": "manager-userid"}) == "missing"
    assert unroutable_reason({**MANAGER, "recipient": None}) == "missing"


def test_the_two_reason_tables_cover_the_same_terminal_states() -> None:
    """**Two tables edited in different places is how this repo drifts.**

    `DELIVERY_MANUAL_TASK_REASONS` decides what a failed *customer* delivery is
    called; `NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS` does the same for a
    manager notice (user's ruling 2026-09-08, independent review P1-1). A third
    terminal state added to one and forgotten in the other would raise `KeyError`
    inside `dispatch_one`, which on a queue means a row stuck `SENDING` until its
    lease expires -- and it would only happen for the audience nobody drives by
    hand. Cheaper to assert the key sets are equal.

    `batched_kinds_without_a_sentence()` guards the other half of this pair for
    the digest's sentences; this is the same idea one table over.
    """

    from fastagent_v2.worker.pipeline import (  # noqa: PLC0415 - see module docstring
        DELIVERY_MANUAL_TASK_REASONS,
        NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS,
    )

    assert set(NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS) == set(DELIVERY_MANUAL_TASK_REASONS)
    assert not set(NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS.values()) & set(
        DELIVERY_MANUAL_TASK_REASONS.values()
    ), "the two audiences share a reason code, so the digest can still find its own failures"


def test_a_notice_that_did_not_arrive_is_platform_internal_and_unpushed() -> None:
    """**The ring, closed by construction** (user's ruling 2026-09-08; P1-1).

    `digest.py` selects `task_class = MERCHANT_ACTION AND notify = BATCHED`. As
    long as these two codes are neither, a failed manager notice cannot become
    the next manager notice -- no exclusion clause, nothing to keep in sync, and
    the task is still written so somebody still owns it.

    The e2e that walks the whole ring is
    `test_a_digest_that_cannot_be_delivered_does_not_become_the_next_digest`;
    this one pins the classification itself, which is where the ruling lives.
    """

    from fastagent_v2.runtime.notification import (  # noqa: PLC0415 - see module docstring
        BATCHED,
        MERCHANT_ACTION,
        NO_PUSH,
        PLATFORM_INTERNAL,
        classify,
    )
    from fastagent_v2.worker.pipeline import (  # noqa: PLC0415 - see module docstring
        NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS,
    )

    for reason_code in NON_CUSTOMER_DELIVERY_MANUAL_TASK_REASONS.values():
        classification = classify(reason_code)
        assert classification.audience_class == PLATFORM_INTERNAL, reason_code
        assert classification.cadence == NO_PUSH, reason_code
        assert not (
            classification.audience_class == MERCHANT_ACTION and classification.cadence == BATCHED
        ), f"{reason_code} is exactly what the digest sweeps up"
