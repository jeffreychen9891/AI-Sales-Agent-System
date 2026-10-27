"""Guard tests for the restricted WeComProxy client (ADR-0015, ADR-0027).

These assert three mechanical properties: the destructive operations do not
exist, no guid v2 did not create can be named, and the two mutating calls
cannot fire unconfirmed.

**Why the guards exist has changed; what they do has not.** ADR-0015 justified
them as v1 isolation, believing v2 borrowed v1's WeComProxy account and that
one appId could operate every instance under it. ADR-0027 corrected the
premise: the provider's permission boundary is the *connection*, each is bought
separately and binds one WeCom account, and v2 runs on its own purchased
connection -- disjoint from v1's three. The guards remain on ordinary
blast-radius grounds: these calls are destructive to whichever live connection
they reach, including v2's own. If these tests ever need to change, that is now
an ordinary risk decision, not a v1-isolation one.

Real v1 production guids are still deliberately NOT committed here. v1's
callback endpoint (``POST /api/v1/webhooks/wechat/{guid}``) has no signature
and no token, so a guid is bearer-equivalent for that endpoint and belongs in
neither this repository nor its CI logs -- unchanged by ADR-0027, which is
about v2's credentials, not v1's endpoint. The foreign guids below reproduce
the exact shape; the allowlist is default-deny, so shape is all that is needed
-- rejection does not depend on recognising any particular value. The operator
pins the real values in the runtime denylist file, exercised by
``test_denylist_overrides_allowlist``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.channels.wecom_proxy import (
    CONFIRMED_OPERATIONS,
    FORBIDDEN_OPERATIONS,
    ConfirmationRequiredError,
    ForbiddenOperationError,
    GuidAllowlist,
    GuidNotAllowedError,
    RestrictedWeComProxyClient,
    WeComProxyConfigError,
    WeComProxyCredentials,
    WeComProxyError,
    load_credentials,
    normalize_guid,
    url_without_token,
)
from fastagent_v2.runtime.redaction import REDACTED, REDACTION_FAILED

#: Interpolated rather than written into the string literal, so the repository
#: scanner sees `{AUDIT_SENTINEL}` instead of a credential-shaped DSN. It caught
#: the first draft of these tests, which inlined it.
AUDIT_SENTINEL = "sentinel-audit-password"  # noqa: S105 - fixture sentinel

# Same shape as v1's live guids; values are synthetic. See module docstring.
FOREIGN_GUIDS = (
    "596AC23A-0000-0000-0000-000000000001",
    "A11C6A0E-0000-0000-0000-000000000002",
    "36F30BDF-0000-0000-0000-000000000003",
)
V2_GUID = "11111111-2222-3333-4444-555555555555"
NOTIFY_URL = "https://stage2.example.test/wecom_proxy/config-a/0123456789abcdef0123456789abcdef"


class RecordingTransport:
    """Stands in for the provider. Records what would have been sent."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.budgets: list[float | None] = []
        self._response = response or {"code": 0, "data": {}}

    def __call__(
        self,
        path: str,
        payload: dict[str, Any],
        token: str,
        *,
        budget: float | None = None,
    ) -> dict[str, Any]:
        # `budget` is recorded, not ignored: T96's P1-2 made the per-call
        # deadline part of the transport contract, and a double that swallowed
        # it would let the next regression through unseen.
        self.calls.append((path, payload))
        self.budgets.append(budget)
        return self._response


#: Passed as `audit=` to ask `build_client` for a client with **no** sink
#: injected, so `__init__`'s own `audit or self._default_audit` is what wires it.
_DEFAULT_SINK = object()


def build_client(
    tmp_path: Path,
    *,
    transport: RecordingTransport | None = None,
    audit: list[dict[str, Any]] | Any = None,
    confirm: Any = None,
    denylist: Path | None = None,
) -> tuple[RestrictedWeComProxyClient, RecordingTransport, list[dict[str, Any]]]:
    sent = transport or RecordingTransport()
    records: list[dict[str, Any]] = (
        [] if audit is None or audit is _DEFAULT_SINK else audit
    )
    client = RestrictedWeComProxyClient(
        credentials=WeComProxyCredentials(
            base_url="https://provider.example.test",
            app_id="app-id-placeholder",
            app_secret="app-secret-placeholder",
        ),
        allowlist=GuidAllowlist(tmp_path / "allowlist.json", denylist_path=denylist),
        audit=None if audit is _DEFAULT_SINK else records.append,
        confirm=confirm if confirm is not None else (lambda operation, details: True),
        transport=sent,
    )
    # Skip the login round trip; the transport is injected anyway.
    client._token = "test-token"  # noqa: SLF001
    from datetime import UTC, datetime, timedelta

    client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)  # noqa: SLF001
    return client, sent, records


def allow(client: RestrictedWeComProxyClient, guid: str) -> None:
    client._allowlist.record(guid, device_name="v2-stage-test")  # noqa: SLF001


# -- forbidden operations ------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("operation", sorted(FORBIDDEN_OPERATIONS))
def test_forbidden_operations_raise_instead_of_executing(
    tmp_path: Path, operation: str
) -> None:
    client, sent, records = build_client(tmp_path)
    allow(client, V2_GUID)
    with pytest.raises(ForbiddenOperationError):
        getattr(client, operation)(guid=V2_GUID)
    assert sent.calls == [], "a forbidden operation reached the transport"
    assert records[-1]["outcome"] == "refused_forbidden_operation"
    assert records[-1]["operation"] == operation


@pytest.mark.security
def test_forbidden_set_is_exactly_the_four_reaching_foreign_instances() -> None:
    assert FORBIDDEN_OPERATIONS == {
        "list_clients",
        "logout",
        "stop_client",
        "delete_notify_url",
    }


# -- foreign guid rejection (the reverse case) ---------------------------


@pytest.mark.security
@pytest.mark.parametrize("foreign", FOREIGN_GUIDS)
@pytest.mark.parametrize(
    "operation",
    ["get_login_qrcode", "check_login_qrcode", "set_notify_url", "send_text"],
)
def test_foreign_guid_is_rejected_on_every_guid_bearing_call(
    tmp_path: Path, foreign: str, operation: str
) -> None:
    client, sent, records = build_client(tmp_path)
    allow(client, V2_GUID)
    kwargs: dict[str, Any] = {"guid": foreign}
    if operation == "set_notify_url":
        kwargs["notify_url"] = NOTIFY_URL
    if operation == "send_text":
        kwargs.update(recipient_id="someone", content="hello")
    with pytest.raises(GuidNotAllowedError):
        getattr(client, operation)(**kwargs)
    assert sent.calls == [], "a foreign guid reached the transport"
    assert records[-1]["outcome"] == "rejected_guid"
    assert records[-1]["guid"] == foreign


@pytest.mark.security
def test_allowlist_is_default_deny_when_file_absent(tmp_path: Path) -> None:
    client, sent, _ = build_client(tmp_path)
    assert not (tmp_path / "allowlist.json").exists()
    with pytest.raises(GuidNotAllowedError):
        client.get_login_qrcode(guid=V2_GUID)
    assert sent.calls == []


@pytest.mark.security
def test_only_create_client_can_widen_the_allowlist(tmp_path: Path) -> None:
    transport = RecordingTransport({"code": 0, "data": {"guid": V2_GUID}})
    client, sent, records = build_client(tmp_path, transport=transport)
    with pytest.raises(GuidNotAllowedError):
        client.get_login_qrcode(guid=V2_GUID)
    returned = client.create_client(device_name="v2-stage-test")
    assert returned == V2_GUID
    client.get_login_qrcode(guid=V2_GUID)
    assert sent.calls[-1][0] == "/login/getLoginQrcode"
    stored = json.loads((tmp_path / "allowlist.json").read_text(encoding="utf-8"))
    assert [item["guid"] for item in stored] == [V2_GUID]
    assert any(record["outcome"] == "created" for record in records)


@pytest.mark.security
def test_denylist_overrides_allowlist(tmp_path: Path) -> None:
    """The operator pins real foreign guids here, outside Git."""
    denylist = tmp_path / "denied-guids.txt"
    denylist.write_text(f"# operator pinned\n{FOREIGN_GUIDS[0]}\n", encoding="utf-8")
    client, sent, _ = build_client(tmp_path, denylist=denylist)
    # Even if the guid somehow reached the allowlist, the denylist wins.
    with pytest.raises(GuidNotAllowedError):
        client._allowlist.record(FOREIGN_GUIDS[0], device_name="whatever")  # noqa: SLF001
    with pytest.raises(GuidNotAllowedError):
        client.set_notify_url(guid=FOREIGN_GUIDS[0], notify_url=NOTIFY_URL)
    assert sent.calls == []


@pytest.mark.security
@pytest.mark.parametrize("junk", ["", "   ", "not a guid", "DROP TABLE", "A" * 128])
def test_malformed_guids_are_rejected(junk: str) -> None:
    with pytest.raises(GuidNotAllowedError):
        normalize_guid(junk)


# -- per-call confirmation -----------------------------------------------


@pytest.mark.security
def test_confirmed_operations_are_exactly_create_and_set_notify() -> None:
    assert CONFIRMED_OPERATIONS == {"create_client", "set_notify_url"}


@pytest.mark.security
def test_default_confirmer_refuses_so_nothing_fires_unattended(tmp_path: Path) -> None:
    sent = RecordingTransport()
    client = RestrictedWeComProxyClient(
        credentials=WeComProxyCredentials(
            base_url="https://provider.example.test",
            app_id="app-id-placeholder",
            app_secret="app-secret-placeholder",
        ),
        allowlist=GuidAllowlist(tmp_path / "allowlist.json"),
        audit=lambda record: None,
        transport=sent,
    )
    with pytest.raises(ConfirmationRequiredError):
        client.create_client(device_name="v2-stage-test")
    assert sent.calls == []


@pytest.mark.security
def test_declined_confirmation_blocks_set_notify_url(tmp_path: Path) -> None:
    client, sent, records = build_client(
        tmp_path, confirm=lambda operation, details: False
    )
    allow(client, V2_GUID)
    with pytest.raises(ConfirmationRequiredError):
        client.set_notify_url(guid=V2_GUID, notify_url=NOTIFY_URL)
    assert sent.calls == []
    assert records[-1]["outcome"] == "declined"


@pytest.mark.security
def test_confirmation_prompt_shows_guid_and_redacted_url(tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def confirm(operation: str, details: dict[str, Any]) -> bool:
        seen.append((operation, details))
        return True

    client, _, _ = build_client(tmp_path, confirm=confirm)
    allow(client, V2_GUID)
    client.set_notify_url(guid=V2_GUID, notify_url=NOTIFY_URL)
    operation, details = seen[-1]
    assert operation == "set_notify_url"
    assert details["guid"] == V2_GUID
    assert "0123456789abcdef" not in details["notify_url"]


# -- credential and audit hygiene ----------------------------------------


@pytest.mark.security
def test_audit_records_never_carry_credentials_or_path_token(tmp_path: Path) -> None:
    transport = RecordingTransport({"code": 0, "data": {"guid": V2_GUID}})
    client, _, records = build_client(tmp_path, transport=transport)
    client.create_client(device_name="v2-stage-test")
    client.set_notify_url(guid=V2_GUID, notify_url=NOTIFY_URL)
    client.send_text(guid=V2_GUID, recipient_id="someone", content="hello")
    blob = json.dumps(records)
    assert "app-secret-placeholder" not in blob
    assert "app-id-placeholder" not in blob
    assert "0123456789abcdef" not in blob


@pytest.mark.security
def test_url_without_token_keeps_origin_and_drops_the_secret() -> None:
    assert url_without_token(NOTIFY_URL) == "https://stage2.example.test/wecom_proxy/..."
    assert url_without_token("not-a-url") == "<malformed-url>"


@pytest.mark.security
def test_credentials_never_render_their_secret() -> None:
    credentials = WeComProxyCredentials(
        base_url="https://provider.example.test",
        app_id="app-id-placeholder",
        app_secret="app-secret-placeholder",
    )
    assert "app-secret-placeholder" not in repr(credentials)
    assert "app-secret-placeholder" not in str(credentials)
    assert "app-id-placeholder" not in repr(credentials)


@pytest.mark.security
def test_credentials_come_from_a_file_not_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WECOM_PROXY_APP_ID", "from-environment")
    monkeypatch.setenv("WECOM_PROXY_APP_SECRET", "from-environment")
    monkeypatch.setenv("WECOM_PROXY_BASE_URL", "https://from-environment.example.test")
    missing = tmp_path / "wecom_proxy.env"
    with pytest.raises(WeComProxyConfigError, match="missing"):
        load_credentials(missing)


@pytest.mark.security
def test_credentials_file_reports_missing_keys_by_name_only(tmp_path: Path) -> None:
    path = tmp_path / "wecom_proxy.env"
    path.write_text(
        "WECOM_PROXY_BASE_URL=https://provider.example.test\nWECOM_PROXY_APP_ID=only-the-id\n",
        encoding="utf-8",
    )
    with pytest.raises(WeComProxyConfigError) as excinfo:
        load_credentials(path)
    assert "WECOM_PROXY_APP_SECRET" in str(excinfo.value)
    assert "only-the-id" not in str(excinfo.value)


@pytest.mark.security
def test_the_audit_record_is_redacted_before_it_reaches_any_sink(tmp_path: Path) -> None:
    """T20. `redact()` had no production caller; this is the first one.

    Redaction happens in `_record`, not in `_default_audit`, because the sink is
    an injection point: this test uses its own sink and must still receive a
    redacted record. A guarantee that only holds for the default sink is not a
    guarantee.

    The payload here is the shape that actually reaches this method in
    production -- `reason=str(exc)` from a rejected guid, i.e. text this class
    does not control.
    """

    client, _, records = build_client(tmp_path)
    dsn = f"postgresql://fastagent_app:{AUDIT_SENTINEL}@db.internal:5432/fastagent"
    client._record(  # noqa: SLF001 - the boundary under test
        "set_notify_url",
        "rejected_guid",
        reason=f"upstream said: could not connect to {dsn}",
        notify_url="https://provider.example.test/hook",
        input_tokens=1234,
    )

    assert len(records) == 1
    record = records[0]
    assert AUDIT_SENTINEL not in str(record)
    assert REDACTED in record["reason"]
    # The rest of the line survives: an audit record that says nothing is not
    # worth writing.
    assert record["operation"] == "set_notify_url"
    assert record["outcome"] == "rejected_guid"
    assert record["notify_url"] == "https://provider.example.test/hook"
    # And T22 holds on this path too -- the count is not a credential.
    assert record["input_tokens"] == 1234


@pytest.mark.security
def test_the_default_sink_logs_the_redacted_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same guarantee, observed where it originally failed: the log line."""

    # Not injected and not reassigned afterwards: the previous version of this
    # test set `client._audit = client._default_audit` by hand, which tested
    # that `_default_audit` redacts but not that anything ever *reaches* it --
    # it would have stayed green if `__init__` stopped falling back to it
    # (independent review 2026-09-03, P3-4).
    client, _, _ = build_client(tmp_path, audit=_DEFAULT_SINK)
    assert client._audit == client._default_audit  # noqa: SLF001 - the wiring is the point
    dsn = f"redis://:{AUDIT_SENTINEL}@cache.internal:6379/0"
    with caplog.at_level(logging.INFO, logger="fastagent_v2.channels.wecom_proxy.client"):
        client._record("send_text", "ok", reason=f"queue {dsn} unreachable")  # noqa: SLF001

    assert caplog.records, "the default sink did not log"
    logged = caplog.records[0].getMessage()
    assert AUDIT_SENTINEL not in logged
    assert REDACTED in logged


@pytest.mark.security
def test_a_wholesale_redaction_failure_still_leaves_a_usable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-1. Failing closed must not take the record's identity with it.

    `redact()` fails closed by returning the *string* `[REDACTION_FAILED_DROPPED]`,
    not a dict, so the first version of this batch -- which passed
    `operation` / `outcome` / `at` through the same call -- would hand the sink a
    `str` where `AuditSink` says `dict`, and the record would stop saying which
    operation had been refused. That is the wrong thing to lose at exactly the
    moment something strange is happening.

    Not reachable through `_record(**fields)` today, because kwargs keys are
    always plain strings; the failure is forced here rather than left to a
    future call site to discover.
    """

    client, _, records = build_client(tmp_path)
    monkeypatch.setattr(
        "fastagent_v2.channels.wecom_proxy.client.redact",
        lambda value: REDACTION_FAILED,
    )
    client._record("set_notify_url", "rejected_guid", reason="anything at all")  # noqa: SLF001

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    # The identity of the record survives...
    assert record["operation"] == "set_notify_url"
    assert record["outcome"] == "rejected_guid"
    assert record["at"]
    # ...and the payload says plainly that it did not.
    assert record["fields"] == REDACTION_FAILED
    assert "anything at all" not in str(record)


def test_a_guid_the_allowlist_refuses_is_a_failed_send_not_an_unknown_one(
    tmp_path: Path,
) -> None:
    """Our own refusal, and the receipt has to say so.

    The adapter's blanket `unknown` was right about the provider -- classifying
    a provider's error taxonomy before the first real call would be inventing
    one -- and wrong about us. An allowlist refusal is raised **before any
    packet exists**, it is deterministic, and retrying it a thousand times gives
    the same answer. `unknown` buys a manual task per message whose text is "we
    do not know whether this was delivered", about something that was never
    sent (independent review 2026-09-05, P2-3).

    This test lives on the ingress side of the fence on purpose: the worker's
    adapter matches these exceptions **by name** rather than importing them, so
    the coupling has to be pinned from the side where importing both is nobody's
    boundary violation.
    """

    from fastagent_v2.worker.channel import SendCommand
    from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter

    client, _, _ = build_client(tmp_path)
    adapter = WeComProxyChannelAdapter(
        send_text=client.send_text,
        guid="ABCDEF01-2345-6789-ABCD-EF0123456789",  # never allowlisted
        recipient_id="wm-test-user",
    )

    receipt = adapter.send(
        command=SendCommand(
            tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            conversation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            content={"type": "text", "text": "您好"},
        ),
        idempotency_key="send_message:e1:0",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )

    assert receipt.status == "failed"
    assert receipt.evidence["reason"] == "local_guard_refused"
    assert receipt.evidence["error_type"] == "GuidNotAllowedError"


def test_the_worker_matches_these_classes_themselves_not_their_names() -> None:
    """**B2-5 turned this from a pin into a check that the pin is gone.**

    This case used to exist to pay for a workaround: `_LOCAL_GUARD_ERRORS` held
    *strings*, because the worker would not import from `services/ingress`, and
    a rename over here would have silently downgraded those receipts back to
    `unknown` -- a regression nothing else would notice, since the adapter's own
    tests inject stubs. So the two sides' agreement had to be asserted by hand.

    The move made the workaround unnecessary: the errors live in
    `fastagent_v2.channels.wecom_proxy`, which the worker imports like any other
    runtime module. **A rename is now an import error, and no test has to stand
    in for the type system.** What is asserted here is therefore the *new*
    property -- that the set really holds classes, and the shared ones --
    because a silent revert to strings would go green against
    `type(error).__name__` again and nothing else would object.
    """

    from fastagent_v2.worker.wecom_proxy_channel import _LOCAL_GUARD_ERRORS

    for error_class in (
        GuidNotAllowedError,
        ConfirmationRequiredError,
        ForbiddenOperationError,
    ):
        assert error_class in _LOCAL_GUARD_ERRORS

    # The point of the change, stated so that a revert to strings goes red here
    # rather than only in a receipt nobody is reading.
    assert all(isinstance(entry, type) for entry in _LOCAL_GUARD_ERRORS), (
        "the set went back to holding names; a rename would stop being caught"
    )
    # And they are the *shared* classes, not same-named locals.
    assert all(
        entry.__module__.startswith("fastagent_v2.channels.wecom_proxy")
        for entry in _LOCAL_GUARD_ERRORS
        if entry is not TypeError
    )


def test_the_binding_tool_writes_its_audit_records_somewhere(tmp_path: Path) -> None:
    """The ceremony leaves a trace, which it did not before.

    `RestrictedWeComProxyClient`'s default sink is `logger.info`, and the tool
    configured no logging at all: root logger with no handler, and `lastResort`
    only speaks at WARNING and above. So `create_client / created`,
    `rejected_guid` and `declined` went **nowhere** -- while the tool's own
    module header justifies its existence by having "the default-deny
    allowlist, the audit sink and the redaction boundary" (independent review
    2026-09-05, P2-4).

    The 2026-09-05 ceremony that created `v2-stage-01` ran through this tool and
    left only the allowlist file behind.
    """

    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "wecom_proxy_bind.py"
    spec = importlib.util.spec_from_file_location("wecom_proxy_bind", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    allowlist_path = tmp_path / "allowlist.json"
    sink = module._file_audit(allowlist_path)  # noqa: SLF001
    sink({"operation": "create_client", "outcome": "created", "guid": "ABCDEF01"})
    sink({"operation": "get_login_qrcode", "outcome": "rejected_guid"})

    written = module.audit_path_for(allowlist_path)
    lines = [json.loads(line) for line in written.read_text(encoding="utf-8").splitlines()]
    assert [line["operation"] for line in lines] == ["create_client", "get_login_qrcode"]
    assert all("at" in line for line in lines), "a ceremony record without a time is half a record"


def test_the_notify_url_return_value_cannot_carry_the_path_token(tmp_path: Path) -> None:
    """The 2026-09-02 leak, closed at the interface instead of at every caller.

    `setGuidNotifyUrl` **echoes the currently registered notify URL back**, and
    its last segment is v2's 256-bit channel path token. Confirmation prompts
    and audit records had always reduced it; the return value handed it out raw
    and left every caller to notice. One did not.

    Three shapes, because a provider is free to return the value under a key we
    never anticipated -- so the guarantee has to be "the token does not come out
    of this method", not "we redacted the field we expected".
    """

    token = NOTIFY_URL.rsplit("/", 1)[-1]
    echo = RecordingTransport(
        response={
            "code": 0,
            "data": {
                # 1. the shape actually observed
                "url": NOTIFY_URL,
                # 2. the same credential under an unexpected key
                "somethingNew": NOTIFY_URL,
                # 3. the token on its own, with no URL around it: URL parsing
                #    alone would walk straight past this one
                "callbackToken": token,
                "nested": [{"echoed": NOTIFY_URL}],
            },
        }
    )
    client, _, _ = build_client(tmp_path, transport=echo)
    client._allowlist.record(V2_GUID, device_name="v2-stage-01")  # noqa: SLF001

    returned = client.set_notify_url(guid=V2_GUID, notify_url=NOTIFY_URL)

    rendered = json.dumps(returned, ensure_ascii=False)
    assert token not in rendered, f"the path token came back out: {rendered}"
    assert NOTIFY_URL not in rendered
    # Reduced rather than deleted: the caller can still see **which host** the
    # provider is talking about, which is why `url_without_token` reduces the
    # field instead of dropping it.
    #
    # **It does not preserve which `config_id`** (independent review
    # 2026-09-08, P2-1). The earlier wording here claimed it did, and that was
    # wrong about v2's own URL shape: the route is
    # `/wecom_proxy/{config_id}/{path_token}`, so the *first* path segment --
    # the only one the reduction keeps -- is the constant `wecom_proxy`, and two
    # different configs reduce to the same string. Asserted, so the claim and
    # the code cannot drift apart again.
    assert "stage2.example.test" in rendered

    other_config = NOTIFY_URL.replace("/config-a/", "/config-b/")
    assert url_without_token(NOTIFY_URL) == url_without_token(other_config)


def test_a_notify_url_we_did_not_send_is_redacted_too(tmp_path: Path) -> None:
    """**P1-1.** The pass that had no coverage at all.

    `redact_urls` has two passes and the batch's own argument is that each
    covers the other's hole. Only one of them was ever exercised: the
    known-secret pass had already carved the token out of every URL in the
    fixture, so the three URL-shaped assertions were satisfied without the URL
    pass doing anything. A probe that disabled the URL reduction entirely left
    all 41 cases green.

    **And the uncovered pass is the one the registered defect is actually
    about.** `setGuidNotifyUrl` echoes back the URL *currently registered for
    that guid* -- and you call it precisely when you are about to **change**
    that URL. So the echo is the **previous** address, carrying the **previous**
    token: a credential that is not among the secrets we passed in, because we
    never sent it. Nothing but the URL pass stands between it and the caller.
    """

    stale = "https://stage2.example.test/wecom_proxy/config-zz/" + "9" * 43
    stale_token = stale.rsplit("/", 1)[-1]
    assert stale_token not in NOTIFY_URL, "the point of this case is that it is a *different* token"

    echo = RecordingTransport(
        response={"code": 0, "data": {"url": stale, "previous": {"was": stale}}}
    )
    client, _, _ = build_client(tmp_path, transport=echo)
    client._allowlist.record(V2_GUID, device_name="v2-stage-01")  # noqa: SLF001

    returned = client.set_notify_url(guid=V2_GUID, notify_url=NOTIFY_URL)

    rendered = json.dumps(returned, ensure_ascii=False)
    assert stale_token not in rendered, f"a token we never sent came back out: {rendered}"
    assert stale not in rendered
    assert "stage2.example.test" in rendered


def test_a_failing_call_does_not_echo_the_url_in_its_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**P1-2.** Failure is when this endpoint is *most* likely to quote the URL.

    The provider's `msg` is provider-controlled text, and `setGuidNotifyUrl`'s
    argument is the URL whose last segment is the channel path token, so
    `"invalid url: <url>"` is the most ordinary thing it could say. The method
    docstring promised "the token does not come out of this method"; before
    this, that held only on the success path.

    **This one has to drive `_http_transport` itself.** Every other case in this
    file injects `RecordingTransport`, which *replaces* that method -- and the
    `code`/`status_code` check, and therefore the whole failure branch, lives
    inside it. So the injected transport cannot reach this at all, and going
    through `set_notify_url` with a fake would have asserted nothing while
    looking like it did. `_http_transport` had **no coverage of any kind**
    before this case.
    """

    from fastagent_v2.channels.wecom_proxy import client as client_module

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"code": 40001, "msg": f"invalid url: {NOTIFY_URL} -- rejected"}

    class _Client:
        def __init__(self, **_kwargs: Any) -> None: ...

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def post(self, *_args: Any, **_kwargs: Any) -> _Response:
            return _Response()

    monkeypatch.setattr(client_module.httpx, "Client", _Client)

    client, _, _ = build_client(tmp_path)

    with pytest.raises(WeComProxyError) as raised:
        client._http_transport(  # noqa: SLF001
            "/client/setGuidNotifyUrl", {"url": NOTIFY_URL}, "test-token"
        )

    message = str(raised.value)
    assert NOTIFY_URL.rsplit("/", 1)[-1] not in message, f"token in exception text: {message}"
    assert NOTIFY_URL not in message
    # The code is still there -- redacting must not cost the operator the one
    # thing that says *why* it failed.
    assert "40001" in message
    # And the host survives, so the message still says who rejected it.
    assert "stage2.example.test" in message


def test_a_degenerate_notify_url_does_not_become_a_bogus_secret(tmp_path: Path) -> None:
    """**P3-2.** `rsplit("/")` on `https://host` yields `"host"`.

    Passed as a known secret, that word would be struck out of the whole
    response -- silent over-redaction that reads exactly like a leak being
    caught. No secret is better than a wrong one; the URL pass is still on
    either way.
    """

    from fastagent_v2.channels.wecom_proxy.client import _trailing_path_segment  # noqa: SLF001

    assert _trailing_path_segment(NOTIFY_URL) == NOTIFY_URL.rsplit("/", 1)[-1]
    assert _trailing_path_segment(NOTIFY_URL + "/") == NOTIFY_URL.rsplit("/", 1)[-1]
    # Fewer than two path segments: no secret at all.
    assert _trailing_path_segment("https://host") == ""
    assert _trailing_path_segment("https://host/") == ""
    assert _trailing_path_segment("https://host/only-one") == ""
    assert _trailing_path_segment("not-a-url") == ""


def _bind_module() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "wecom_proxy_bind.py"
    spec = importlib.util.spec_from_file_location("wecom_proxy_bind", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_audit_file_is_created_owner_only(tmp_path: Path) -> None:
    """**运维报障 2026-09-11：这个文件让下一次部署挂在 preflight 上。**

    `_file_audit` 在 `provider-secrets/` 下建 `…-audit.jsonl`，而
    `stage_preflight.py` 要求那个目录里的东西是 **600**
    （`must have mode 600; found {mode:o}`）。原来用的是 `Path.open("a")`，
    按进程 umask 创建——Stage 上落成 **664**，于是**任何人跑过一次绑定工具，
    下一次部署第一步就被拒**。

    **发作时间与起因完全脱节**：仪式是今天某个人做的，炸的是改天另一个人的部署，
    错误信息指着一个他没建过的文件。**这类缺陷靠人查最贵**，所以要有东西钉住。

    两条断言，分工写明：

    * **mode 那条是 POSIX-only**——Windows 产生不了 POSIX 权限位，而本仓的门禁
      跑在 Windows 上。照 `test_the_token_file_is_created_restrictively_and_never_widened`
      的既有口径办：**说明限制，而不是断言平台给不出的东西**；
    * **`os.open` 的 mode 实参那条是平台无关的**，它才是本仓门禁上真正会变红的那一条。
      只留 POSIX 那一条等于「今天没有东西守着」换了个说法。
    """

    module = _bind_module()
    allowlist_path = tmp_path / "allowlist.json"
    seen: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if str(path) == str(module.audit_path_for(allowlist_path)):
            seen.append(mode)
        return real_open(path, flags, mode, **kwargs)

    module.os.open = spy
    try:
        sink = module._file_audit(allowlist_path)  # noqa: SLF001
        sink({"operation": "create_client", "outcome": "created"})
    finally:
        module.os.open = real_open

    assert seen == [0o600], (
        "审计文件必须在**创建时**就是 0600，不能靠 umask，也不能 open 之后再 chmod"
        f"（那中间有一个窗口）；实际传给 os.open 的 mode 是 {[oct(m) for m in seen]}"
    )
    if os.name == "posix":
        written = module.audit_path_for(allowlist_path)
        assert written.stat().st_mode & 0o777 == 0o600


def test_an_already_widened_audit_file_is_narrowed_on_the_next_append(
    tmp_path: Path,
) -> None:
    """**光改创建路径不够：Stage 上那个 664 的文件已经存在了。**

    `os.open` 的 mode 实参**只在创建时生效**，所以运维手上那份已经落成 664 的
    文件不会因为本批而变好——而它正是让部署挂掉的那一个。

    **这条无条件收窄不是 `allowlist.py` 警告过的那个错误**，两者差在「放宽是否
    可能是有意的」：allowlist 有第二个读者（worker 容器），`0640` 是一种真实配置，
    所以那里的 `fchmod` 要带 `group_readable`。**这个审计文件没有任何读者**，
    而 preflight 只接受 600——所以宽出来的位只可能来自 umask，留着它就是留着下一次部署的故障。
    """

    if os.name != "posix":
        pytest.skip("POSIX permissions only; Stage is Linux")  # pragma: no cover

    module = _bind_module()
    allowlist_path = tmp_path / "allowlist.json"
    written = module.audit_path_for(allowlist_path)
    written.write_text('{"operation": "earlier"}\n', encoding="utf-8")
    os.chmod(written, 0o664)

    module._file_audit(allowlist_path)({"operation": "create_client"})  # noqa: SLF001

    assert written.stat().st_mode & 0o777 == 0o600
    assert "earlier" in written.read_text(encoding="utf-8"), "收窄不能把先前的记录弄丢"


def test_every_append_re_asserts_the_mode(tmp_path: Path) -> None:
    """**收窄那一半也要有平台无关的守卫。**

    上面那条「已经宽掉的文件会被收窄」是 POSIX-only，而本仓的门禁跑在 Windows 上
    ——反向探针实测：去掉无条件收窄之后它只是 **skipped**，不是红。
    **「没有东西守着」换个平台照样成立**，而那正是本批要消灭的形状。

    所以这一条盯的是调用本身：每次追加都要把 fd 收到 `0600`。
    盯 `fchmod`（POSIX）或 `chmod`（没有 fchmod 的平台），两条路都算数。
    """

    module = _bind_module()
    allowlist_path = tmp_path / "allowlist.json"
    modes: list[int] = []
    real_fchmod = getattr(module.os, "fchmod", None)
    real_chmod = module.os.chmod

    def fchmod_spy(fileno: int, mode: int) -> None:
        modes.append(mode)
        assert real_fchmod is not None
        real_fchmod(fileno, mode)

    def chmod_spy(path: Any, mode: int, **kwargs: Any) -> None:
        if str(path) == str(module.audit_path_for(allowlist_path)):
            modes.append(mode)
        real_chmod(path, mode, **kwargs)

    if real_fchmod is not None:
        module.os.fchmod = fchmod_spy
    module.os.chmod = chmod_spy
    try:
        sink = module._file_audit(allowlist_path)  # noqa: SLF001
        sink({"operation": "create_client"})
        sink({"operation": "get_login_qrcode"})
    finally:
        if real_fchmod is not None:
            module.os.fchmod = real_fchmod
        module.os.chmod = real_chmod

    assert modes == [0o600, 0o600], (
        "每一次追加都要把审计文件收到 0600——`os.open` 的 mode 实参只在**创建**时生效，"
        "而运维手上那份已经落成 664 的文件正是让部署挂掉的那一个；"
        f"实际收到的 mode 序列是 {[oct(m) for m in modes]}"
    )


def test_every_append_asks_the_deployment_rule_whether_the_mode_is_acceptable(
    tmp_path: Path,
) -> None:
    """**落盘后的自检，先前一支探针都没有**（独立审查 P1-2，探针 C 实测全绿）。

    自检那一段被整块删掉时，`test_wecom_proxy_guard.py` 是 **46 passed / 0 failed**
    ——也就是说本批声称「这一行才是继承本身」的那一行，**没有任何东西守着它**。

    这一条盯的是**问没问**，不是问的结果：每写一条记录，都要拿写入方落的 mode 去
    问判定方那条规则，并且带着 `require_exact=AUDIT_FILE_MODE`。
    **平台无关**——Windows 上 `os.fstat` 报 `0o666`、于是这里问出来的是一条拒绝，
    而 `MODE_BITS_ARE_REAL` 为假所以不停下；问这个动作本身照样发生，
    照样能在本仓跑在 Windows 上的门禁里变红。这是
    `test_every_append_re_asserts_the_mode` 已经立过的口径：
    **只写 POSIX 那一条，等于「今天没有东西守着」换了个说法。**
    """

    module = _bind_module()
    allowlist_path = tmp_path / "allowlist.json"
    asked: list[tuple[int, object]] = []
    real_refusal = module.credentials_mode_refusal

    def spy(mode: int, **kwargs: Any) -> str | None:
        asked.append((mode, kwargs.get("require_exact")))
        return real_refusal(mode, **kwargs)

    module.credentials_mode_refusal = spy
    try:
        sink = module._file_audit(allowlist_path)  # noqa: SLF001
        sink({"operation": "create_client"})
        sink({"operation": "get_login_qrcode"})
    finally:
        module.credentials_mode_refusal = real_refusal

    assert [required for _mode, required in asked] == [0o600, 0o600], (
        "每一次追加都要拿落盘后的 mode 去问判定方那条规则，且带 require_exact=0o600；"
        f"实际问出去的是 {[(oct(m), r) for m, r in asked]}"
    )


def test_a_widened_audit_file_stops_the_ceremony_and_names_what_already_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自检问出一条拒绝时会**停下**，并且**说出供应商侧已经发生了什么**。

    两件事各一条断言：

    * **停下**（独立审查 P1-2）。探针 C 删掉整段自检时没有任何东西变红，
      所以「停」这一半也要有守卫。用 `MODE_BITS_ARE_REAL` 把平台那道闸打开、
      再让 `os.fstat` 报一个 `0o664`，就能在 Windows 上验 Stage 的那条路径；
    * **说出已经发生的事**（独立审查 P2-1）。`create_client` 的顺序是
      ①`/client/createClient`（**WeComProxy 侧不可逆**）→ ②落 allowlist →
      ③写审计。自检在第 ③ 步抛，于是实例已经建好、guid 已经落盘，
      **而工具不返回 guid**。停是对的——一个宽掉的文件正是本批要消灭的东西——
      但操作者必须知道供应商那边多了个什么。所以报错里要带上那条记录。

    报错措辞那条断言同时钉住 P3-1：写入方说的话要和运维在 preflight 那边
    看到的那句 `must have mode 600; found 664` **逐字相同**。
    """

    module = _bind_module()
    monkeypatch.setattr(module, "MODE_BITS_ARE_REAL", True)

    class _Widened:
        st_mode = 0o100664

    monkeypatch.setattr(module.os, "fstat", lambda _fd: _Widened())

    allowlist_path = tmp_path / "allowlist.json"
    sink = module._file_audit(allowlist_path)  # noqa: SLF001
    with pytest.raises(SystemExit) as caught:
        sink({"operation": "create_client", "guid": "GUID-ALREADY-CREATED-UPSTREAM"})

    message = str(caught.value)
    assert "must have mode 600; found 664" in message, (
        "写入方的措辞要和 stage_preflight 那句逐字相同，否则排障时两边对不上"
        f"（P3-1）；实际是：{message}"
    )
    assert "GUID-ALREADY-CREATED-UPSTREAM" in message, (
        "停下来的时候必须说出供应商侧已经建好的那个 guid——"
        f"它不会因为我们停下而消失（P2-1）；实际是：{message}"
    )


def test_require_exact_refuses_a_mode_the_base_rule_accepts() -> None:
    """**`require_exact` 自己也没有守卫**（独立审查 P1-2，探针 D 实测全绿）。

    让 `require_exact` 永不拒绝时，两个安全用例文件合计 **155 passed / 0 failed**。

    这个参数存在的**全部理由**就是第二、三条断言那一对：`0640` 对基础规则合法
    ——它正是「多一个服务读得到」的写法——而对 `provider-secrets/` 下未点名的
    文件不合法。两者都为真，参数才有意义；`require_exact` 一旦失效，
    审计文件就退回到「写入方按自己的规则落、判定方按自己的规则拒」那个状态。
    """

    from fastagent_v2.channels.wecom_proxy.credentials import credentials_mode_refusal

    assert credentials_mode_refusal(0o600, require_exact=0o600) is None
    # 参数存在的理由：同一个 mode，两条规则给不同答案。
    assert credentials_mode_refusal(0o640) is None
    assert credentials_mode_refusal(0o640, require_exact=0o600) is not None
    # 运维报的那个值，以及它该说的那句话（P3-1：与 preflight 逐字相同）。
    assert credentials_mode_refusal(0o664, require_exact=0o600) == (
        "must have mode 600; found 664"
    )
    # 不传时，基础规则一个字没变。
    assert credentials_mode_refusal(0o600) is None
    assert credentials_mode_refusal(0o664) is not None
