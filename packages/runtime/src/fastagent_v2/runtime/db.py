from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row


@dataclass(frozen=True, slots=True)
class Database:
    dsn: str

    @contextmanager
    def transaction(
        self,
        *,
        tenant_id: UUID | None,
        subject_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> Iterator[Connection[Any]]:
        with Connection.connect(self.dsn, row_factory=dict_row) as connection:
            with connection.transaction():
                if tenant_id is not None:
                    connection.execute(
                        "SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),)
                    )
                if subject_id is not None:
                    connection.execute(
                        "SELECT set_config('app.subject_id', %s, true)", (str(subject_id),)
                    )
                if correlation_id is not None:
                    connection.execute(
                        "SELECT set_config('app.correlation_id', %s, true)",
                        (str(correlation_id),),
                    )
                yield connection
