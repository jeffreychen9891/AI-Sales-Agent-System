from __future__ import annotations

from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

app = FastAPI(title="M0 channel boundary simulator", docs_url=None, redoc_url=None)


class SinkCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    conversation_id: str
    content: dict[str, object]


_lock = Lock()
_by_key: dict[str, dict[str, object]] = {}
_by_operation: dict[str, dict[str, object]] = {}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/send")
def send(
    command: SinkCommand, idempotency_key: str = Header(alias="Idempotency-Key")
) -> dict[str, object]:
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="idempotency key required")
    with _lock:
        existing = _by_key.get(idempotency_key)
        if existing is not None:
            return existing
        operation_id = str(uuid4())
        receipt: dict[str, object] = {
            "provider_operation_id": operation_id,
            "status": "delivered",
            "command": command.model_dump(),
        }
        _by_key[idempotency_key] = receipt
        _by_operation[operation_id] = receipt
        return receipt


@app.get("/send/by-key/{idempotency_key}")
def reconcile_by_key(idempotency_key: str) -> dict[str, object]:
    """Look a send up by the key it was sent with.

    **Declared before `/send/{operation_id}`, and the order is load-bearing**:
    FastAPI matches in declaration order, so the other way round this path is
    swallowed as an operation id called "by-key" and every lookup 404s -- which
    reads exactly like "the provider has no record of it", the one answer this
    endpoint must never give by accident.
    """

    with _lock:
        receipt = _by_key.get(idempotency_key)
        if receipt is None:
            raise HTTPException(status_code=404, detail="not found")
        return receipt


@app.get("/send/{operation_id}")
def reconcile(operation_id: str) -> dict[str, object]:
    with _lock:
        receipt = _by_operation.get(operation_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="not found")
        return receipt


@app.post("/test/reset")
def reset() -> dict[str, bool]:
    with _lock:
        _by_key.clear()
        _by_operation.clear()
    return {"reset": True}
