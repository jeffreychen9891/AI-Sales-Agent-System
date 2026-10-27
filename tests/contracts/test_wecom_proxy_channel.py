"""The outbound WeComProxy adapter (T96).

What these pin is mostly what the adapter **refuses to claim**. It is the first
`ChannelPort` implementation for a provider nobody has called yet, and the
registered item says so: T96 exists to discover WeComProxy's actual response
shape, timeout behaviour, idempotency-key behaviour and receipt fields. Until a
real call has happened, an adapter that classified errors confidently would be
inventing a taxonomy -- and `channel.py`'s own comment records the price of
guessing that way round: "over-claiming loses a delivered message with no trace".
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastagent_v2.worker.channel import SendCommand
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter


# **不是模块级常量，而这一格是本仓踩过的。**
#
# 它原本写成 `_deadline() = datetime.now(UTC) + timedelta(seconds=30)`——
# 一个**在 import（collection）那一刻**就固定下来的时刻。整棵树跑起来时，
# 从收集到这条用例真正执行之间超过那个窗口，它就已经过期了，
# 于是被测代码走的是「时间不够」那条分支，而用例读起来像是逻辑错了。
#
# **实测（2026-09-20）**：`pytest tests/contracts/test_reconcile_before_unknown.py`
# 单跑 17 条全绿；在 `pytest_collection_finish` 里插 26 秒再跑，**12 条红**，
# 与当天一次全量门禁里红的那 12 条逐条相同。
#
# 现取即可。**一个「还没到的时刻」必须相对它被用的那一刻算，不是相对 import。**
def _deadline() -> datetime:
    """调用时现取。理由见上。"""

    return datetime.now(UTC) + timedelta(seconds=30)
#: Provider-shaped, for the one test that goes through the real guarded client.
REAL_GUID = "ABCDEF01-2345-6789-ABCD-EF0123456789"
TEXT = SendCommand(
    tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    conversation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    content={"type": "text", "text": "您好，已经收到。"},
)


def _adapter(send_text, *, guid: str = "guid-1", recipient: str = "wm-test-user"):
    return WeComProxyChannelAdapter(send_text=send_text, guid=guid, recipient_id=recipient)


#: **The send response the vendor documents**, `data` as §0.3 of
#: `docs/wecom-proxy-message-api.md` prints it (§2–§15 share this body).
#:
#: **Every fixture in this file used to return `{"msgId": …}`, and there is no
#: such field.** That is the second generation of the same defect: the first was
#: the *envelope* (independent review 2026-09-05, P1-1 -- the fixture returned
#: `{"code":…, "data":{…}}`, a shape no caller produces), and the fix pinned the
#: unwrapping while leaving the **field name** invented. Even
#: `test_the_operation_id_comes_out_of_what_the_real_client_returns`, written
#: expressly to stop this, drove the real client against a hand-written vendor
#: body. **Both generations stayed green for the same reason: the response was
#: written next to the code that reads it.**
def sent(server_id: int = 1003054) -> dict[str, object]:
    return {
        "isSendSuccess": 1,
        "msgServerId": server_id,
        "msgType": 0,
        "msgUniqueIdentifier": f"u-{server_id}",
        "seq": 1759127702979498,
        "timestamp": 1759127702,
    }



def test_the_send_carries_the_guid_and_the_recipient_the_row_resolved() -> None:
    """`SendCommand` carries neither, and that is the finding this batch started from.

    It was designed against the M0 simulator, which needed only a conversation
    id. A real send needs the binding's guid and the conversation's counterparty,
    and neither existed anywhere in the schema before migration 017.
    """

    seen: list[dict[str, object]] = []

    def send_text(**kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        # **The shape the real client returns**: `send_text` ends with
        # `return dict(result.get("data") or {})`, so the envelope is already
        # gone. The first version of this fixture returned `{"code": 0, "data":
        # {...}}` -- a shape no caller produces -- and that is why the suite was
        # green while `provider_operation_id` was `None` against the real client
        # (independent review 2026-09-05, P1-1).
        #
        # **And the field inside it was invented too** -- see `sent()`.
        return sent()

    receipt = _adapter(send_text).send(
        command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    assert len(seen) == 1
    call = seen[0]
    assert call["guid"] == "guid-1"
    assert call["recipient_id"] == "wm-test-user"
    assert call["content"] == TEXT.content["text"]
    # The remaining budget travels with the call (P1-2). Asserted as a range
    # rather than a literal: it is a clock reading, and pinning it to 30.0 would
    # be asserting that the test machine is infinitely fast.
    assert isinstance(call["timeout"], float)
    assert 0 < call["timeout"] <= 30
    assert receipt.status == "accepted"
    assert receipt.provider_operation_id == "1003054"


def test_a_provider_failure_becomes_a_receipt_and_never_an_exception() -> None:
    """The port's contract, and the reason for it.

    The dispatcher's only answer to an exception is to drop the process, so a
    provider having a bad hour would become a crash-restart loop instead of a
    recorded delivery state.
    """

    def explode(**_: object) -> dict[str, object]:
        raise RuntimeError("WeComProxy said no, and quoted the request back")

    receipt = _adapter(explode).send(
        command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    assert receipt.status == "unknown"
    assert receipt.evidence["reason"] == "provider_call_failed"
    assert receipt.evidence["error_type"] == "RuntimeError"


def test_the_failure_evidence_never_carries_the_provider_message() -> None:
    """Only the exception's type. Its message can quote the customer's own text.

    Same discipline the HTTP adapter already follows ("Never echo the body: it
    carries customer content"), applied to an error string instead of a body.
    """

    # Named for what it is -- the provider quoting our request back at us --
    # rather than `secret = ...`, which is a shape `check_secrets.py` flags on
    # sight. It is right to flag it: a line that assigns a literal to something
    # called `secret` is worth a human look every time, and the scanner cannot
    # tell that this one is a fixture. Renaming the variable is the fix the
    # scanner asks for; widening its pattern would not be.
    quoted_back = "客户说：我的手机号是 13800000000"

    def explode(**_: object) -> dict[str, object]:
        raise RuntimeError(quoted_back)

    receipt = _adapter(explode).send(
        command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    rendered = repr(receipt.evidence)
    assert quoted_back not in rendered
    assert "13800000000" not in rendered


def test_an_unrecognised_response_yields_no_operation_id_rather_than_a_guess() -> None:
    """The real response shape is one of the four things this batch exists to learn.

    An invented id would be an id nothing can look up, and it would send the
    delivery down the by-operation-id reconcile path -- which this channel
    cannot serve either. `None` sends it down the by-key path, which says
    plainly that the channel cannot be queried.
    """

    receipt = _adapter(lambda **_: {"unexpected": "shape"}).send(
        command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    assert receipt.status == "accepted"
    assert receipt.provider_operation_id is None


@pytest.mark.parametrize(
    "content",
    [
        {"type": "image", "url": "https://example.invalid/a.png"},
        {"type": "text"},
        {"type": "text", "text": ""},
        "not-a-dict",
    ],
)
def test_content_this_channel_cannot_carry_is_terminal_not_unknown(content: object) -> None:
    """Retrying changes nothing, so `failed` is the honest state.

    The distinction matters downstream: `unknown` costs a manual task asking a
    person to find out what happened, and nobody needs to find out that an image
    is not a text message.
    """

    command = SendCommand(
        tenant_id=TEXT.tenant_id, conversation_id=TEXT.conversation_id, content=content
    )

    receipt = _adapter(lambda **_: {"code": 0}).send(
        command=command, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    assert receipt.status == "failed"
    assert receipt.evidence["reason"] == "unsendable_content_type"


def test_neither_reconcile_door_claims_to_have_asked_anything() -> None:
    """**T78, arriving as predicted.**

    WeComProxy offers no per-message status lookup and no lookup by the caller's
    idempotency key. The registered item warned that an adapter which cannot do
    this would return a bare `unknown` -- indistinguishable from "we asked and
    the provider has no record", which is the exact confusion L1 was built to
    remove.

    `query_sent: False` is what keeps that from happening:
    `_reconcile_before_giving_up` reads it and records
    `reconcile_attempted: false` with a reason, instead of writing down that the
    provider was consulted. That flag is L1's P0 fix, and this is the first
    adapter that actually needs it.
    """

    adapter = _adapter(lambda **_: {"code": 0})

    by_id = adapter.reconcile(provider_operation_id="m-1", deadline_at=_deadline())
    by_key = adapter.reconcile_by_key(idempotency_key="send_message:e1:0", deadline_at=_deadline())

    for receipt in (by_id, by_key):
        assert receipt.status == "unknown"
        assert receipt.evidence["query_sent"] is False
    assert by_id.evidence["reason"] == "provider_has_no_status_lookup"
    assert by_key.evidence["reason"] == "channel_cannot_be_queried_by_key"
    # The id it was asked about is kept; the key path never had one.
    assert by_id.provider_operation_id == "m-1"
    assert by_key.provider_operation_id is None


def test_the_operation_id_comes_out_of_what_the_real_client_returns() -> None:
    """The contract the adapter actually consumes, driven through the real client.

    **Not another stub.** The defect this pins was invisible precisely because
    both sides were mocked: the adapter read `answer["data"]["msgId"]` and the
    fixture returned an envelope, so they agreed with each other and disagreed
    with `RestrictedWeComProxyClient.send_text`, whose last line is
    `return dict(result.get("data") or {})`. Against the real client every
    receipt carried `provider_operation_id = None`, and that column is the only
    handle a person has for finding the message on the provider's side.

    So this test injects the **real** `send_text`, bound to a transport that
    returns the provider's envelope, and asserts the id survives the whole way.
    If somebody changes either side's unwrapping, this is what says so.
    """

    from fastagent_v2.channels.wecom_proxy.allowlist import GuidAllowlist
    from fastagent_v2.channels.wecom_proxy.client import (
        RestrictedWeComProxyClient,
        WeComProxyCredentials,
    )

    def transport(path: str, payload: dict, token: str, **_: object) -> dict:
        assert path == "/msg/sendText"
        # The envelope the raw endpoint returns, wrapping the documented body.
        return {"code": 0, "data": sent(2004411)}

    with tempfile.TemporaryDirectory() as directory:
        allowlist = GuidAllowlist(Path(directory) / "allowlist.json")
        # A provider-shaped guid, because `GuidAllowlist` refuses anything else
        # (hex and dashes only): the adapter does not validate guids, the guard
        # does, and this test drives the guard.
        allowlist.record(REAL_GUID, device_name="probe")
        client = RestrictedWeComProxyClient(
            credentials=WeComProxyCredentials(
                base_url="https://provider.example.test",
                app_id="app-id-placeholder",
                app_secret="app-secret-placeholder",
            ),
            allowlist=allowlist,
            audit=lambda record: None,
            confirm=lambda operation, details: True,
            transport=transport,
        )
        client._token = "test-token"  # noqa: SLF001
        client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001

        receipt = _adapter(client.send_text, guid=REAL_GUID).send(
            command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
        )

    assert receipt.status == "accepted"
    assert receipt.provider_operation_id == "2004411"


def test_the_operation_id_is_also_found_inside_a_raw_envelope() -> None:
    """**The fallback branch, which nothing exercised** (independent review P3-2).

    `_operation_id` tries the top level and then `answer["data"]`, and its
    docstring justifies the second layer in as many words: someone wiring this
    adapter straight to the transport -- during a Stage smoke test, say -- gets
    the same answer instead of a silent `None`. The window's probe D deleted the
    whole branch and all 29 cases stayed green: **the claim had nothing behind
    it.**

    Leaving an unexercised branch in *this* function is not a neutral gap. It is
    the function that shipped a guessed key twice, and both times the reason was
    the same -- the response it reads was written next to the code that reads it.
    """

    receipt = _adapter(lambda **_: {"code": 0, "data": sent(7788991)}).send(
        command=TEXT, idempotency_key="send_message:e1:0", deadline_at=_deadline()
    )

    assert receipt.status == "accepted"
    assert receipt.provider_operation_id == "7788991"


def test_an_expired_deadline_sends_nothing_at_all() -> None:
    """The overrun that duplicates a message rather than delaying one.

    `OUTBOX_LEASE_SECONDS` is 30 and the turn budget is about the same, so a
    dispatcher that starts a send after its deadline may be sending a row
    another dispatcher already owns -- `channel.py` states it plainly: "an
    overrun is not a slow log line, it is another dispatcher claiming the row
    and sending the message twice". `HttpChannelAdapter` refuses in exactly this
    shape; before this batch's fix, this adapter sent anyway with a deadline
    five seconds in the past.

    `unknown`, not `failed`: nothing was attempted, so there is no evidence
    about the message -- only about us.
    """

    calls: list[object] = []

    def send_text(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return sent()

    receipt = _adapter(send_text).send(
        command=TEXT,
        idempotency_key="send_message:e1:0",
        deadline_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert calls == [], "an expired deadline must not reach the provider"
    assert receipt.status == "unknown"
    assert receipt.provider_operation_id is None
    assert receipt.evidence["reason"] == "deadline_exceeded_before_send"


def test_the_client_splits_a_budget_across_the_four_httpx_phases() -> None:
    """One number used four times is four times the budget.

    httpx reads a bare float as connect **and** read **and** write **and** pool,
    so a 20-second budget authorises 80 seconds inside one request. `channel.py`
    divides its query budget by four for this reason; this asserts the provider
    client does the same, because that is where the `httpx.Client` is built.
    """

    from fastagent_v2.channels.wecom_proxy.allowlist import GuidAllowlist
    from fastagent_v2.channels.wecom_proxy.client import (
        RestrictedWeComProxyClient,
        WeComProxyCredentials,
    )

    with tempfile.TemporaryDirectory() as directory:
        client = RestrictedWeComProxyClient(
            credentials=WeComProxyCredentials(
                base_url="https://provider.example.test",
                app_id="app-id-placeholder",
                app_secret="app-secret-placeholder",
            ),
            allowlist=GuidAllowlist(Path(directory) / "allowlist.json"),
            audit=lambda record: None,
            confirm=lambda operation, details: True,
        )

        split = client._timeout_for(20.0)  # noqa: SLF001
        default = client._timeout_for(None)  # noqa: SLF001

    assert split.connect == split.read == split.write == split.pool == 5.0
    assert default == 30.0, "no budget means the constructor default, unchanged"
