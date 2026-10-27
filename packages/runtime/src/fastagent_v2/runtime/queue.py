from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from redis import Redis
from redis.exceptions import ResponseError

INBOX_STREAM = "fastagent:v2:inbox"
INBOX_GROUP = "fastagent:v2:workers"


@dataclass(frozen=True, slots=True)
class InboxPointer:
    stream_id: str
    inbox_id: UUID
    tenant_id: UUID


class PointerQueue:
    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    def ping(self) -> bool:
        return bool(self._client.ping())

    def ensure_group(self) -> None:
        try:
            self._client.xgroup_create(INBOX_STREAM, INBOX_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, *, inbox_id: UUID, tenant_id: UUID) -> str:
        return str(
            self._client.xadd(
                INBOX_STREAM,
                {"inbox_id": str(inbox_id), "tenant_id": str(tenant_id)},
                maxlen=100_000,
                approximate=True,
            )
        )

    def read_one(self, *, consumer: str, block_ms: int = 1000) -> InboxPointer | None:
        self.ensure_group()
        response = cast(
            Any,
            self._client.xreadgroup(
                INBOX_GROUP,
                consumer,
                {INBOX_STREAM: ">"},
                count=1,
                block=block_ms,
            ),
        )
        if not response:
            return None
        _, messages = response[0]
        stream_id, fields = messages[0]
        return InboxPointer(
            stream_id=str(stream_id),
            inbox_id=UUID(fields["inbox_id"]),
            tenant_id=UUID(fields["tenant_id"]),
        )

    def acknowledge(self, pointer: InboxPointer) -> None:
        self._client.xack(INBOX_STREAM, INBOX_GROUP, pointer.stream_id)

    def close(self) -> None:
        self._client.close()
