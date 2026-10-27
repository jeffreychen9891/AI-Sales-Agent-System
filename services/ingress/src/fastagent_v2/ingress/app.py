from __future__ import annotations

import json

import psycopg
from fastagent_v2.ingress.adapter import (
    OUTCOME_REFUSED,
    IngressAuthenticationError,
    IngressOutcomeError,
    IngressPayloadError,
    IngressWindowNotOpenError,
    WeComProxyIngress,
)
from fastagent_v2.ingress.wecom_proxy.callback import OUTCOME_DECLINED
from fastagent_v2.runtime.db import Database
from fastagent_v2.runtime.queue import PointerQueue
from fastagent_v2.runtime.settings import Settings
from fastapi import FastAPI, HTTPException, Request, status
from jsonschema import ValidationError

#: **Every answer this service composes for a refusal it means is `200`**
#: (T204 / ADR-0103 §二.1).
#:
#: WeComProxy deletes a callback address after **ten consecutive failures** --
#: deletes, not pauses, and it does not come back. So a status code here is not
#: a way to describe an outcome; it is a vote on whether the provider should
#: keep talking to us. A refusal we *meant* -- a bad payload, a token that does
#: not match -- must not spend one of those ten.
#:
#: The outcome moves into the body instead, and non-200 is left for the cases
#: where redelivery is what we actually want: **we are broken**, or **we are not
#: ready yet** (see `IngressWindowNotOpenError`).
#:
#: **The success code moved too, and that is not cosmetic.** It used to be
#: `202 Accepted`, and `202` is also not `200`. The vendor's rule is written
#: about "failures" without saying what counts as one; if `202` counts, then
#: **every ordinary message was already spending one of the ten**. Nobody has
#: measured which reading is right, and this is a question we can decline to
#: ask: answering `200` everywhere costs nothing and makes the answer
#: irrelevant.
_ALWAYS = status.HTTP_200_OK


def _refused(code: str, message: str) -> dict[str, object]:
    """A refusal, said in the body because it may not be said in the status.

    `accepted: false` is the field a caller reads; `error.code` is the field a
    log or a metric groups by. Neither says *which* check failed beyond the two
    coarse buckets -- the detail stays in this service's own logs, because
    telling a caller which half of an authentication failed is what an
    authentication path must not do (the reasoning lives in `adapter.py`).
    """

    return {"accepted": False, "duplicate": False, "error": {"code": code, "message": message}}


def _record(ingress: WeComProxyIngress, exc: IngressOutcomeError, *, kind: str, code: str) -> None:
    """Record the answer, when the refusal knows which binding it was about.

    **Not every refusal can be recorded, and that is the design** (see
    `IngressOutcomeError`): before a binding resolves, `config_id` is a string
    from the URL, and counting by it would hand an unbounded label to anyone who
    can reach this port. Those refusals are answered exactly the same way -- the
    caller cannot tell the difference -- they just do not move a series.

    `http_status` is derived from `kind` rather than passed: the two are the same
    fact said twice, and the pair that could disagree is exactly the pair
    ADR-0103 §二.1 cares about ("a refusal we mean" must be a 200).
    """

    if exc.binding_tenant_id is None or exc.binding_config_id is None:
        return
    ingress.record_answer(
        tenant_id=exc.binding_tenant_id,
        config_id=exc.binding_config_id,
        kind=kind,
        reason_code=code,
        http_status=503 if kind == "ingress.non_2xx" else 200,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env("ingress")
    ingress = WeComProxyIngress(
        database=Database(resolved.ingress_database_url),
        pointer_queue=PointerQueue(resolved.queue_redis_url),
        runtime_deadline_seconds=resolved.runtime_deadline_seconds,
    )
    app = FastAPI(title="FastAgent v2 ingress", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/wecom_proxy/{config_id}/{path_token}", status_code=_ALWAYS)
    async def wecom_proxy(config_id: str, path_token: str, request: Request) -> dict[str, object]:
        try:
            try:
                payload = await request.json()
            except json.JSONDecodeError as exc:
                # **Narrowed from `ValueError`** (independent review 2026-09-07,
                # P3-1). `200` now means "received, do not send it again", so a
                # fault of *ours* mis-filed here costs the message itself. A
                # body that is not JSON is the caller's; a `ValueError` raised
                # deeper in `accept()` -- parsing something read out of our own
                # database, say -- is not, and must stay a 500.
                raise IngressPayloadError("payload is not JSON") from exc
            if not isinstance(payload, dict):
                raise IngressPayloadError("payload must be an object")
            result = ingress.accept(config_id=config_id, path_token=path_token, payload=payload)
        except IngressWindowNotOpenError as exc:
            # **The one refusal that is ours** (user ruling, 2026-09-07; review
            # P2-1). The binding's window has not opened, or closed mid-rotation
            # -- usually clock skew, which is why the message names both clocks.
            # Answering 200 would have the provider record a delivery and never
            # retry, losing a real customer message for good. 503 asks for the
            # redelivery we want, at the cost of one `errorTimes` against a
            # deletion threshold of ten and an alert threshold of five.
            # **Recorded before the raise**, because `raise` leaves this
            # function: the gauge that guards an irreversible deletion cannot
            # depend on a `finally` somebody may later restructure.
            _record(ingress, exc, kind="ingress.non_2xx", code="WINDOW_NOT_OPEN")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="channel binding window is not open yet",
            ) from exc
        except IngressAuthenticationError as exc:
            # Was `401`. A binding configured wrongly for ten messages used
            # to be enough to lose the callback address entirely.
            _record(ingress, exc, kind="ingress.refused", code="CHANNEL_AUTHENTICATION_FAILED")
            return _refused("CHANNEL_AUTHENTICATION_FAILED", "channel authentication failed")
        except (IngressPayloadError, ValidationError, psycopg.DataError) as exc:
            # `ValidationError` is `message-envelope`'s own limits (a 20000+
            # character message, a 255+ character event id) and **is not a
            # `ValueError`**, so it used to reach the framework as a 500.
            # `psycopg.DataError` is the same shape one layer down: a value the
            # database cannot store. Both are the caller's payload, so both are
            # refusals we mean (independent review 2026-09-07, P1-1).
            # `ValidationError` and `psycopg.DataError` are not ours to carry a
            # binding on, and the JSON-shaped `IngressPayloadError` is raised
            # before any binding is resolved. **The branch this call was waiting
            # for arrived with the callback envelope**: a body that
            # authenticates and then turns out not to be an WeComProxy envelope
            # at all is refused *after* the binding is known, so it is recorded.
            # **The refusal itself is unchanged either way**; a metric may not
            # decide what a caller is told.
            if isinstance(exc, IngressOutcomeError):
                _record(ingress, exc, kind="ingress.refused", code="INVALID_CALLBACK_PAYLOAD")
            return _refused("INVALID_CALLBACK_PAYLOAD", "invalid callback payload")
        declined = [item for item in result.items if item.outcome == OUTCOME_DECLINED]
        if result.items and all(item.outcome == OUTCOME_DECLINED for item in result.items):
            # **Understood and declined**, which is not "unreadable": a group
            # message is a perfectly well-formed callback that this platform
            # does not handle yet (user ruling 2026-09-10). It carries its own
            # code so that a merchant investigating "why did nobody answer"
            # finds the reason rather than "invalid payload".
            return _refused(
                declined[0].reason_code or "ROOM_MESSAGE_OUT_OF_SCOPE",
                "this callback is not a one-to-one customer message",
            )
        if result.items and not any(
            item.outcome != OUTCOME_REFUSED for item in result.items
        ):
            # **Every element was refused, so the answer is the refusal it
            # always was** -- byte for byte, including for the single-element
            # request that is all the vendor has ever sent. A request whose
            # elements were *all* unreadable has nothing else to report, and
            # inventing a richer shape for it would change what the security
            # cases about this answer are asserting.
            return _refused("INVALID_CALLBACK_PAYLOAD", "invalid callback payload")
        body: dict[str, object] = {
            "accepted": result.accepted,
            "duplicate": result.duplicate,
            "event_id": None if result.event_id is None else str(result.event_id),
            # **Per element, because one request is now several messages.**
            # The vendor reads none of this -- ADR-0103 §二.1 says the status
            # code is the only thing it acts on -- so this exists for us: a
            # partially refused request has to be able to say *which* element
            # it refused, or the next investigation starts from "eight
            # identical 117-byte bodies" again.
            "items": [
                {
                    "external_event_id": item.external_event_id,
                    "outcome": item.outcome,
                    "event_id": None if item.event_id is None else str(item.event_id),
                    "duplicate": item.duplicate,
                    "reason_code": item.reason_code,
                }
                for item in result.items
            ],
        }
        if not result.accepted:  # some element was refused or declined
            # A mixed request: some elements landed, at least one did not.
            #
            # **The code names what actually happened** (independent review
            # 2026-09-10, P3-1). It used to say `INVALID_CALLBACK_PAYLOAD` for
            # every mixed request, including ones where nothing was invalid --
            # a group message alongside a customer message would report the
            # customer message's request as a bad payload. `error.code` is the
            # field a log or a metric groups by (`_refused`'s docstring says so),
            # so it may not be the coarser of two true answers.
            refused = any(item.outcome == OUTCOME_REFUSED for item in result.items)
            body["error"] = (
                {"code": "INVALID_CALLBACK_PAYLOAD", "message": "invalid callback payload"}
                if refused
                else {
                    "code": declined[0].reason_code or "ROOM_MESSAGE_OUT_OF_SCOPE",
                    "message": "this callback is not a one-to-one customer message",
                }
            )
        return body

    return app


app = create_app()
