"""B2-3: the decision to send for real is explicit, and the worker stays fenced in.

Two properties, and neither is about message delivery:

1. **Going live is its own named decision.** ADR-0091 pins Stage to
   `channel-sink.invalid` while nothing may be sent. If "is that URL still the
   placeholder?" decided whether real messages go out, then **editing one URL**
   would be the act that starts messaging real people -- an irreversible step
   performed by a value that exists to be a guard. `FASTAGENT_CHANNEL_PROVIDER`
   is that decision instead, and an unrecognised value is refused rather than
   read as "keep pretending".

2. **The dispatcher's client cannot do the two things a background loop must
   never do.** `create_client` and `set_notify_url` are `CONFIRMED_OPERATIONS`:
   they need a human at a terminal. The worker builds its client with no
   confirmer, so the default `_deny_unconfirmed` refuses both -- and
   `set_notify_url` in particular is the call that echoes a notify URL whose
   last segment is the channel path token.
"""

from __future__ import annotations

import pytest
from fastagent_v2.runtime.settings import Settings, SettingsError
from fastagent_v2.worker.cli import channel_factory_for

REQUIRED = {
    "FASTAGENT_ENVIRONMENT": "local",
    "FASTAGENT_PROCESS_ROLE": "worker",
    "FASTAGENT_WORKER_DATABASE_URL": "postgresql://u:p@localhost/db",
    "FASTAGENT_QUEUE_REDIS_URL": "redis://localhost:6379/0",
    "FASTAGENT_CHANNEL_SEND_URL": "http://channel-sink.invalid/send",
}


def _credentials_file(tmp_path):
    """A credentials file with the three keys `load_credentials` requires.

    `0o600` because the loader checks the mode -- credentials readable by more
    than their owner are refused, which is the point of reading them from a file
    rather than the environment in the first place.
    """

    path = tmp_path / "wecom_proxy.env"
    path.write_text(
        "\n".join(
            (
                "WECOM_PROXY_BASE_URL=http://provider.example.test",
                "WECOM_PROXY_APP_ID=app-id-placeholder",
                "WECOM_PROXY_APP_SECRET=app-secret-placeholder",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _load(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for key in list(REQUIRED) + [
        "FASTAGENT_CHANNEL_PROVIDER",
        "FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)
    return Settings.from_env("worker")


def test_the_default_is_still_the_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every environment that has not opted in sends where it sent yesterday."""

    settings = _load(monkeypatch)

    assert settings.channel_provider == "sink"
    assert channel_factory_for(settings) is None


def test_an_unknown_channel_is_refused_rather_than_read_as_the_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not quietly mean "keep pretending".

    Refused at load, so it is a startup error rather than a silence -- the same
    treatment `FASTAGENT_PROCESS_ROLE` and `FASTAGENT_ENVIRONMENT` already get.
    The opposite reading is the dangerous one only in one direction, but this
    direction is the one that is hard to notice: a worker that believes it is
    sending and is not.
    """

    with pytest.raises(SettingsError):
        _load(monkeypatch, FASTAGENT_CHANNEL_PROVIDER="easywerk")


def test_choosing_the_provider_is_not_the_same_as_changing_the_sink_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**ADR-0091's guard keeps its meaning.**

    Pointing `channel_send_url` at something real does *not* start sending
    through the provider, and selecting the provider does *not* require touching
    that URL. The two are independent on purpose: one is a placeholder that a
    seeding script refuses to build a binding against, the other is the decision
    to go live.
    """

    real_url = _load(monkeypatch, FASTAGENT_CHANNEL_SEND_URL="https://real.example.test/send")
    assert real_url.channel_provider == "sink"
    assert channel_factory_for(real_url) is None


def test_the_dispatchers_client_cannot_run_a_confirmed_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**The fence, asserted on the object the dispatcher would really use.**

    Built through `channel_factory_for` rather than by hand: a test that
    constructed its own client would be checking the client's default, not this
    wiring's use of it, and the wiring is what could pass a confirmer by
    accident.

    `set_notify_url` matters most -- it echoes the registered notify URL, whose
    last segment is the 256-bit channel path token (the 2026-09-02 leak). A loop
    with nobody watching must not be able to call it.
    """

    from fastagent_v2.channels.wecom_proxy import (
        CONFIRMED_OPERATIONS,
        ConfirmationRequiredError,
    )

    monkeypatch.setenv(
        "FASTAGENT_CHANNEL_CREDENTIALS_FILE", str(_credentials_file(tmp_path))
    )

    settings = _load(
        monkeypatch,
        FASTAGENT_CHANNEL_PROVIDER="wecom_proxy",
        FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE=str(tmp_path / "allowlist.json"),
    )
    factory = channel_factory_for(settings)
    assert factory is not None

    # The client the factory closed over, reached the way the fence is reached:
    # through the adapter it hands out.
    guid = "11111111-2222-3333-4444-555555555555"
    adapter = factory(guid=guid, recipient_id="wxid_x")
    client = adapter._send_text.__self__  # noqa: SLF001 - the point is *this* object

    # **The guid is allowlisted first, on purpose.** `_guarded_guid` runs before
    # `_require_confirmation`, so an un-allowlisted guid raises
    # `GuidNotAllowedError` and this case would pass without the confirmation
    # fence existing at all -- a green test for the wrong reason. Allowlisting
    # strips the outer fence away so the inner one is what is being asserted.
    client._allowlist.record(guid, device_name="v2-stage-test")  # noqa: SLF001

    assert CONFIRMED_OPERATIONS == {"create_client", "set_notify_url"}
    for operation in sorted(CONFIRMED_OPERATIONS):
        with pytest.raises(ConfirmationRequiredError):
            if operation == "set_notify_url":
                client.set_notify_url(
                    guid=guid,
                    notify_url="https://example.test/wecom_proxy/c/" + "0" * 32,
                )
            else:
                client.create_client(device_name="should-not-happen")


def test_a_provider_worker_without_an_allowlist_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**Written for a claim that turned out to be false, which is why it exists.**

    The first draft passed `GuidAllowlist("")` and called it default-deny. It is
    not: an empty path is `Path(".")`, which *exists* and is a directory, so the
    allowlist raises `WeComProxyConfigError: guid allowlist is unreadable` --
    **on the first message, inside the dispatch loop**, rather than where an
    operator would see it. This case failed on that and the wiring changed.

    So the property is now the stronger one: a worker told to use the provider
    but not told where its allowlist is **does not start**.
    """

    monkeypatch.setenv(
        "FASTAGENT_CHANNEL_CREDENTIALS_FILE", str(_credentials_file(tmp_path))
    )

    settings = _load(monkeypatch, FASTAGENT_CHANNEL_PROVIDER="wecom_proxy")
    assert settings.channel_guid_allowlist_file is None

    with pytest.raises(SettingsError):
        channel_factory_for(settings)


def test_a_configured_but_absent_allowlist_is_default_deny(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The case `GuidAllowlist` *was* designed for, kept distinct from the one above.

    The operator said where the file goes and it is not there yet -- mid-ceremony,
    or a volume not mounted. That must not stop the worker and must not let it
    send: no guid is permitted until the file exists.
    """

    from fastagent_v2.channels.wecom_proxy import GuidNotAllowedError

    monkeypatch.setenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", str(_credentials_file(tmp_path)))

    absent = tmp_path / "not-placed-yet.json"
    assert not absent.exists()
    settings = _load(
        monkeypatch,
        FASTAGENT_CHANNEL_PROVIDER="wecom_proxy",
        FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE=str(absent),
    )

    guid = "11111111-2222-3333-4444-555555555555"
    factory = channel_factory_for(settings)
    assert factory is not None
    adapter = factory(guid=guid, recipient_id="wxid_x")
    client = adapter._send_text.__self__  # noqa: SLF001

    with pytest.raises(GuidNotAllowedError):
        client.send_text(guid=guid, recipient_id="wxid_x", content="hello")


def _seed_module():
    """`scripts/seed_channel_binding.py`, loaded by path.

    Same shape `test_command_resource_gates.py` uses for `check_static`: the
    scripts directory is a tool collection, not a package, and importing by
    path keeps it that way.
    """

    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_channel_binding.py"
    spec = importlib.util.spec_from_file_location("seed_channel_binding", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_seed_guard_still_refuses_the_sink_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0091, unchanged for the case it was written for.

    Nothing about B2-3 makes it acceptable to open an inbound path while
    outbound goes to `channel-sink.invalid`: every message the binding admits
    becomes a billed model call and a manual task, and nothing is delivered.
    """

    seed = _seed_module()
    monkeypatch.delenv("FASTAGENT_CHANNEL_PROVIDER", raising=False)
    monkeypatch.setenv("FASTAGENT_CHANNEL_SEND_URL", "https://channel-sink.invalid/send")

    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()


def test_the_seed_guard_refuses_when_it_cannot_see_the_address_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset is refused too -- the script cannot read the deployment's env, so
    an absent variable means "you have not checked", not "it is fine".
    """

    seed = _seed_module()
    monkeypatch.delenv("FASTAGENT_CHANNEL_PROVIDER", raising=False)
    monkeypatch.delenv("FASTAGENT_CHANNEL_SEND_URL", raising=False)

    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()


def test_naming_the_provider_is_not_enough_for_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**The hole the first version of this guard had** (independent review P1-2).

    Setting only `FASTAGENT_CHANNEL_PROVIDER=wecom_proxy` and configuring nothing
    else used to open the guard. In that exact configuration
    `channel_factory_for` raises `SettingsError` and **the worker does not start
    at all** -- so inbound would be open, every message a billed model call and
    a manual task, and nothing delivered. That is what ADR-0091 §四.1 forbids,
    and worse than it imagined: there the worker was at least running.

    The old comment said the provider being misconfigured "is not something this
    script can see". **These two are exactly what it can see** -- two
    environment variables pointing at two files.
    """

    seed = _seed_module()
    monkeypatch.setenv("FASTAGENT_CHANNEL_PROVIDER", "wecom_proxy")
    monkeypatch.setenv("FASTAGENT_CHANNEL_SEND_URL", "https://channel-sink.invalid/send")
    monkeypatch.delenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", raising=False)

    # Nothing configured: refused because the variable **is not set**. It used
    # to be refused because the fallback path did not exist on a developer
    # machine -- the same answer for a different reason, and the reason changed
    # with ADR-0120: this guard runs on the host and no longer defaults to a
    # container path. The distinction is asserted in
    # `test_the_credentials_check_has_no_default_to_fall_back_on`.
    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()

    # Credentials placed, allowlist still missing: still refused, because a
    # missing allowlist is default-deny and every send would raise.
    monkeypatch.setenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", str(_credentials_file(tmp_path)))
    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()

    # Allowlist configured but empty: same answer, same reason.
    empty = tmp_path / "allowlist.json"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", str(empty))
    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()


def test_the_credentials_check_has_no_default_to_fall_back_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**ADR-0120**, revising ADR-0112 §二.2's half-sentence.

    `DEFAULT_CREDENTIALS_PATH` is the path the credentials file has **inside the
    worker container**; this guard runs on the operator's own host (ADR-0112 §四
    says so and this batch does not change it), where a container path is
    meaningless. So the fallback is gone.

    **The assertion is on the message, not just the refusal**, because both
    versions refuse: the old one because `/run/provider-secrets/wecom_proxy.env`
    does not exist on a host, the new one because nobody said where the file is.
    Only the second sends the operator anywhere useful -- and a case that only
    asserted `SystemExit` would have accepted either.

    **The pair is now symmetric**: the allowlist check beside it has never had a
    default, so this removes an asymmetry rather than inventing a rule.
    """

    seed = _seed_module()
    monkeypatch.setenv("FASTAGENT_CHANNEL_PROVIDER", "wecom_proxy")
    monkeypatch.setenv("FASTAGENT_CHANNEL_SEND_URL", "https://channel-sink.invalid/send")
    monkeypatch.delenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", str(tmp_path / "allowlist.json"))

    with pytest.raises(SystemExit, match="FASTAGENT_CHANNEL_CREDENTIALS_FILE is not set"):
        seed._refuse_while_nothing_can_be_delivered()


def test_a_real_provider_satisfies_the_guard_without_touching_the_sink_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**B2-3's change to the guard, and why it is not a loophole.**

    The guard is about *delivery*, in its own words. Once
    `FASTAGENT_CHANNEL_PROVIDER=wecom_proxy`, the dispatcher builds a provider
    adapter per dispatch and **never reads `channel_send_url`** -- so requiring
    that variable to look real would have the operator write a plausible value
    into a setting nothing consumes, in order to get past a check that then
    protects nothing. **A guard satisfied by an unused variable is worse than no
    guard**, because it still looks like it is guarding.

    So the placeholder is deliberately left in place here: it is the honest
    state of a setting that has no reader, and the guard passes on the fact that
    actually matters.
    """

    seed = _seed_module()
    monkeypatch.setenv("FASTAGENT_CHANNEL_PROVIDER", "wecom_proxy")
    monkeypatch.setenv("FASTAGENT_CHANNEL_SEND_URL", "https://channel-sink.invalid/send")
    # **Both files really present**, which is what "can anything be delivered?"
    # now means. The case above holds the other end: naming the provider without
    # them is refused.
    monkeypatch.setenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", str(_credentials_file(tmp_path)))
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", str(allowlist))

    assert seed._refuse_while_nothing_can_be_delivered() == "provider:wecom_proxy"


def test_a_mistyped_provider_does_not_open_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The reverse probe on the new branch.**

    Only the exact provider name opens it. A typo falls through to the address
    check and is refused by the placeholder -- so the new branch cannot be used
    to wave the guard away with an approximate value, which is exactly how a
    widened check usually goes wrong.

    `Settings.from_env` refuses the same typo at startup, so the two agree; this
    asserts the *guard* does not disagree with the loader.
    """

    seed = _seed_module()
    monkeypatch.setenv("FASTAGENT_CHANNEL_PROVIDER", "easywerk")
    monkeypatch.setenv("FASTAGENT_CHANNEL_SEND_URL", "https://channel-sink.invalid/send")

    with pytest.raises(SystemExit):
        seed._refuse_while_nothing_can_be_delivered()
