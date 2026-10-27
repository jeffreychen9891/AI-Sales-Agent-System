"""B2-3: where a row goes, and the asymmetry between the two kinds of row.

`resolve_dispatch_address` composes the two resolvers B2-2 and B2-3 added, and
**its whole reason to exist is that the two row kinds are addressed from
different places**:

| row | which instance | who receives |
|---|---|---|
| customer | the conversation's binding | the conversation's contact |
| manager | `recipient.config_id` | `recipient.external_user_id` |

A manager row borrows a conversation id to satisfy a foreign key (T90). Taking
**either** half of the address from that conversation would deliver a merchant's
backlog summary into that merchant's customer's chat -- the accident
`unroutable_reason` was written to prevent, reintroduced one layer further down.

These are contract-level with a fake connection: the database-level half (RLS,
the column grant, the three refusals) is already covered by
`tests/integration/test_dispatcher_reads_binding_guid.py`, and repeating it here
would be a second copy rather than a second question. **What is asked here is
only "which columns did it read, and from which row".**
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.pipeline import (
    BINDING_DISABLED,
    BINDING_HAS_NO_INSTANCE,
    CONVERSATION_HAS_NO_CONTACT,
    CONVERSATION_NOT_BOUND,
    CONVERSATION_NOT_VISIBLE,
    resolve_dispatch_address,
)

TENANT = uuid4()
CONVERSATION = uuid4()

#: The conversation the manager row borrows. Its binding points at a **different**
#: instance and its contact is a **different** person, so any test that
#: accidentally addresses a manager row from the conversation produces visibly
#: wrong values rather than coincidentally right ones.
CUSTOMER_GUID = "GUID-CUSTOMER-0001"
CUSTOMER_CONTACT = "wxid_customer_0001"
MANAGER_GUID = "GUID-MANAGER-0002"
MANAGER_CONTACT = "wxid_manager_0002"


class FakeConnection:
    """Answers the three queries the resolvers make, and records what was asked.

    Written as a recorder rather than a stub because two of these cases are
    about **which row was consulted**, and a stub that returns the right answer
    for the wrong query would pass them.
    """

    def __init__(
        self,
        *,
        conversation: dict[str, Any] | None,
        bindings: dict[str, dict[str, Any]],
        contact: dict[str, Any] | None,
    ) -> None:
        self.conversation = conversation
        self.bindings = bindings
        self.contact = contact
        self.config_ids_asked: list[Any] = []

    def execute(self, sql: str, parameters: tuple[Any, ...]) -> FakeConnection:
        self._sql = " ".join(sql.split())
        self._parameters = parameters
        return self

    def fetchone(self) -> dict[str, Any] | None:
        if "FROM conversation_aggregates WHERE" in self._sql:
            return self.conversation
        if "FROM channel_bindings" in self._sql:
            config_id = self._parameters[1]
            self.config_ids_asked.append(config_id)
            return self.bindings.get(str(config_id))
        if "FROM conversation_aggregates a" in self._sql or "JOIN contacts" in self._sql:
            return self.contact
        raise AssertionError(f"unexpected query: {self._sql}")


def _connection(**overrides: Any) -> FakeConnection:
    defaults: dict[str, Any] = {
        "conversation": {"config_id": "config-customer"},
        "bindings": {
            "config-customer": {"provider_instance_id": CUSTOMER_GUID, "status": "active"},
            "config-manager": {"provider_instance_id": MANAGER_GUID, "status": "active"},
        },
        "contact": {"external_user_id": CUSTOMER_CONTACT},
    }
    defaults.update(overrides)
    return FakeConnection(**defaults)


def _customer_command() -> dict[str, Any]:
    return {"audience": "customer", "content": "hello"}


def _manager_command() -> dict[str, Any]:
    return {
        "audience": "manager",
        "content": "3 conversations need you",
        "recipient": {
            "channel": "wecom_proxy",
            "config_id": "config-manager",
            "external_user_id": MANAGER_CONTACT,
        },
    }


def _resolve(connection: FakeConnection, command: dict[str, Any]) -> Any:
    return resolve_dispatch_address(
        connection, tenant_id=TENANT, conversation_id=CONVERSATION, command=command
    )


def test_a_customer_row_is_addressed_from_its_conversation() -> None:
    connection = _connection()

    address, refusal = _resolve(connection, _customer_command())

    assert refusal is None
    assert address is not None
    assert address.guid == CUSTOMER_GUID
    assert address.recipient_id == CUSTOMER_CONTACT


def test_a_manager_row_is_addressed_from_its_own_recipient_not_the_conversation() -> None:
    """**The case this function exists for.**

    The manager row names `config-manager`; the conversation it borrows names
    `config-customer`. Both bindings exist and both are active, so nothing goes
    wrong mechanically -- the only thing distinguishing right from wrong here is
    **which row was consulted**, which is why the fixture gives them different
    values and why the connection records the config ids it was asked about.

    If this ever addressed a manager row from the conversation, the digest would
    be sent through the customer's instance to the customer's contact. That is
    T90's accident, and it would not raise anything.
    """

    connection = _connection()

    address, refusal = _resolve(connection, _manager_command())

    assert refusal is None
    assert address is not None
    assert address.guid == MANAGER_GUID
    assert address.recipient_id == MANAGER_CONTACT

    # Named individually: the equality above would still hold if the resolver
    # had *also* read the conversation and then discarded it, and that would be
    # a different function than the one described.
    assert connection.config_ids_asked == ["config-manager"], (
        "a manager row must not consult the conversation's binding at all"
    )
    assert address.recipient_id != CUSTOMER_CONTACT


def test_a_manager_row_does_not_borrow_the_conversations_contact() -> None:
    """Stated separately, because the two halves fail independently.

    A resolver could take the guid from `recipient.config_id` (correct) and the
    recipient from the conversation's contact (the accident). The row would then
    be sent through the right instance to the wrong person -- arguably worse
    than the whole-address mistake, because the sender looks right.
    """

    connection = _connection(contact={"external_user_id": "wxid_should_not_be_used"})

    address, refusal = _resolve(connection, _manager_command())

    assert refusal is None
    assert address is not None
    assert address.recipient_id == MANAGER_CONTACT


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"conversation": None}, CONVERSATION_NOT_VISIBLE),
        ({"conversation": {"config_id": None}}, CONVERSATION_NOT_BOUND),
        (
            {"bindings": {"config-customer": {"provider_instance_id": "g", "status": "disabled"}}},
            BINDING_DISABLED,
        ),
        (
            {"bindings": {"config-customer": {"provider_instance_id": "", "status": "active"}}},
            BINDING_HAS_NO_INSTANCE,
        ),
        ({"contact": None}, CONVERSATION_HAS_NO_CONTACT),
        ({"contact": {"external_user_id": ""}}, CONVERSATION_HAS_NO_CONTACT),
    ],
)
def test_every_refusal_keeps_its_own_name(overrides: dict[str, Any], expected: str) -> None:
    """Five reasons, five names, and no address alongside any of them.

    They are separate because they are **different jobs for different people**:
    a disabled binding was disabled by somebody, an unfinished ceremony is
    operations, a missing contact is a message that can never be addressed, and
    `conversation_not_visible` may be a tenant-context wiring fault on our side.
    Collapsing them into "cannot send" is what makes an operator read the wrong
    code for an hour.
    """

    address, refusal = _resolve(_connection(**overrides), _customer_command())

    assert address is None
    assert refusal == expected


def test_a_row_with_no_audience_is_addressed_as_a_customer() -> None:
    """The pre-`35f4770` rows, which carry no `audience` at all.

    `is_customer_row` reads a missing key as `customer` deliberately (every
    outbox row older than that commit is a customer message). This asserts the
    address resolver inherits that reading rather than inventing a second one --
    two readings of `audience` in one dispatch would be exactly the drift
    `is_customer_row` was extracted to prevent.
    """

    address, refusal = _resolve(_connection(), {"content": "hello"})

    assert refusal is None
    assert address is not None
    assert address.guid == CUSTOMER_GUID
    assert address.recipient_id == CUSTOMER_CONTACT
