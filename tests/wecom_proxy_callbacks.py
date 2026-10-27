"""Builders for WeComProxy callback bodies, in the shape the vendor really sends.

**One builder, because there was one invented shape.** Every ingress test used
to post v2's own `message-envelope` fields at the top level -- the shape
`accept()` read, and a shape no callback has ever carried. Eight real callbacks
on 2026-09-10 were refused by exactly that mismatch, and no test could have
caught it: the cases and the code were written from the same invention.

So the fixtures are built from the vendor's document (`docs/wecom_proxy.md:41-71`)
and cross-checked against the eight captured bodies in
`evidence/m1b/first-inbound-2026-09-10/payloads/redacted/`. Anything a test needs
to vary is an argument here; anything it does not is the vendor's constant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastagent_v2.ingress.wecom_proxy.callback import conversation_id_for

__all__ = [
    "ACCOUNT_ID",
    "callback",
    "callback_body",
    "callback_item",
    "conversation_for",
    "conversation_id_for",
    "new_conversation",
    "sender_of",
]

#: The hosted account the callbacks are about (`userId` / `receiverId`).
#: **Not a sender**: a message whose `senderId` equals it is our own outbound
#: coming back, which the adapter ignores.
ACCOUNT_ID = "1688850000000001"

#: The vendor's `cmd` for an ordinary WeChat message.
CMD_MESSAGE = 15000


def callback_item(
    *,
    sender_id: str = "customer-1",
    text: str | None = "hello",
    external_event_id: str | None = None,
    occurred_at: datetime | None = None,
    msg_type: int = 2,
    cmd: int = CMD_MESSAGE,
    from_room_id: int | str = 0,
    account_id: str = ACCOUNT_ID,
    request_id: str | None = None,
    msg_data: dict[str, Any] | None = None,
    base64_raw_data: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One element of `data`, with the fields every real element carries.

    `text` goes where the vendor puts it -- `msgData.content` -- rather than at
    the element's top level, because that is where `parse_content` has to find
    it. Passing `msg_data` explicitly replaces it whole, for the cases that are
    about media or withdrawal payloads.
    """

    when = occurred_at or datetime.now(UTC)
    item: dict[str, Any] = {
        "TenantId": 0,
        "guid": "D05FCC41-9740-4932-81A4-6A54E8836AFF",
        "userId": account_id,
        "requestId": request_id if request_id is not None else uuid4().hex,
        "customParam": "v2-stage-01",
        "cmd": cmd,
        "msgServerId": 1000852,
        "msgType": msg_type,
        "msgUniqueIdentifier": external_event_id
        if external_event_id is not None
        else f"evt-{uuid4()}",
        "senderId": sender_id,
        "senderName": "",
        "receiverId": account_id,
        "fromRoomId": from_room_id,
        "isRoomNotice": 0,
        "referId": 0,
        "msgFlag": 16777216,
        "seq": 17350621,
        "timestamp": int(when.timestamp()),
    }
    if base64_raw_data is not None:
        item["base64RawData"] = base64_raw_data
    if msg_data is not None:
        item["msgData"] = msg_data
    elif text is not None:
        item["msgData"] = {"atList": [], "content": text, "extras": None, "reply": {}}
    item.update(extra)
    return item


def callback_body(*items: dict[str, Any], code: int = 0, msg: str = "ok") -> dict[str, Any]:
    """The envelope: `{"code": …, "data": [ … ], "msg": …}`.

    Called with no items it builds `data: []` -- a request that carries nothing,
    which is answered `200` and writes nothing.
    """

    return {"code": code, "data": list(items), "msg": msg}


def callback(**kwargs: Any) -> dict[str, Any]:
    """The common case: one message in one callback."""

    return callback_body(callback_item(**kwargs))


def conversation_for(
    *, tenant_id: UUID, sender_id: str, config_id: str = "config-a"
) -> UUID:
    """The conversation a message from this sender lands in.

    Tests used to choose a conversation id and put it in the payload. The vendor
    sends no such field, so the id is **derived** (user ruling 2026-09-10) and a
    test chooses it the only way production does: by choosing who is speaking.
    """

    conversation_id = conversation_id_for(
        tenant_id=tenant_id, config_id=config_id, peer_id=sender_id
    )
    # Registered here as well as in `new_conversation`: a test that names its
    # customer explicitly must still be able to ask `sender_of` later.
    _SENDERS[conversation_id] = sender_id
    return conversation_id


#: Which customer a test's conversation belongs to. Declared before
#: `conversation_for`, which fills it in.
#:
#: **Bookkeeping, not a second definition of the identity.** The derivation is
#: one-way, so a test that wants to name "this conversation" *and* send messages
#: into it has to remember which sender produces it. Populated only by
#: `new_conversation`; a conversation seeded straight into the database never
#: appears here.
_SENDERS: dict[UUID, str] = {}


def new_conversation(tenant_id: Any, *, config_id: str = "config-a") -> UUID:
    """A fresh customer, and the conversation their messages will land in.

    Replaces the `conversation_id = uuid4()` that used to open these tests. That
    line was only possible because the payload carried a conversation id -- v2's
    own field, which no callback has ever sent. Choosing the customer instead is
    what production does.
    """

    sender_id = f"cust-{uuid4()}"
    conversation_id = conversation_for(
        tenant_id=UUID(str(tenant_id)), sender_id=sender_id, config_id=config_id
    )
    _SENDERS[conversation_id] = sender_id
    return conversation_id


def sender_of(conversation_id: UUID) -> str:
    """The customer whose messages land in this conversation."""

    return _SENDERS[conversation_id]
