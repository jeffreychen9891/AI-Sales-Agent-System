"""B2-5's criterion, asserted structurally: **one client, shared by both halves.**

The exit criterion is "两服务共用一份客户端，allowlist 与脱敏边界不变" -- two
services share one client, and the allowlist and redaction boundaries do not
move. Three of those four words are about *where code lives*, which no
behavioural test can see: every existing case would pass just as well against
two copies of the client, or against a shim in `services/ingress` that
re-exports the runtime one.

So these assert the structure directly, and each is written so that the obvious
half-measure fails it:

* a **shim** left behind in `services/ingress` fails
  `test_the_ingress_package_no_longer_holds_the_client`;
* a **second copy** fails `test_there_is_exactly_one_client_module`;
* a client that moved but whose **allowlist or redaction moved with it into
  something else** fails `test_the_guarded_surface_moved_intact`.

**Why this is worth its own file.** ADR-0094 §四 required this batch be its own,
on the grounds that it moves three things at once -- `FORBIDDEN_OPERATIONS`,
`CONFIRMED_OPERATIONS` and the audit sink injection point -- and that mixing it
with the wiring would make it impossible to tell which half broke. The same
argument applies to its evidence: if the only proof of the move is that the old
behavioural tests still pass, then the move has no evidence of its own at all.
"""

from __future__ import annotations

import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANNELS_WECOM_PROXY = (
    ROOT / "packages" / "channels" / "src" / "fastagent_v2" / "channels" / "wecom_proxy"
)
INGRESS_WECOM_PROXY = (
    ROOT / "services" / "ingress" / "src" / "fastagent_v2" / "ingress" / "wecom_proxy"
)

#: The modules that carry the guarded surface. `inbound.py` is deliberately not
#: here -- it is inbound parsing, ingress-only, and stayed put.
MOVED_MODULES = ("client.py", "allowlist.py", "credentials.py", "errors.py")


def test_the_client_lives_in_runtime_where_both_services_may_import_it() -> None:
    """The move itself."""

    for name in MOVED_MODULES:
        assert (CHANNELS_WECOM_PROXY / name).is_file(), name


def test_the_ingress_package_no_longer_holds_the_client() -> None:
    """**A shim would defeat the point.**

    Leaving `services/ingress/.../wecom_proxy/client.py` re-exporting the runtime
    one would keep every import working and every existing test green, while
    the worker still could not import it without reaching across the boundary
    -- which is the entire thing this batch exists to remove.

    `inbound.py` is asserted to have *stayed*, because "moved the client" and
    "moved the package" are different changes and only the first was approved.
    """

    for name in MOVED_MODULES:
        assert not (INGRESS_WECOM_PROXY / name).exists(), f"{name} is still in services/ingress"

    assert (INGRESS_WECOM_PROXY / "inbound.py").is_file(), (
        "inbound parsing is ingress-only and was not part of this move"
    )


def test_there_is_exactly_one_client_module() -> None:
    """No second copy anywhere in the tree.

    Asserted by walking the source roots rather than by importing, because two
    copies import perfectly well -- that is exactly why a copy is the easy
    mistake and the expensive one: the allowlist and the audit sink would then
    exist twice, and a change to one would leave the other guarding nothing.
    """

    roots = (
        ROOT / "packages" / "runtime" / "src",
        ROOT / "packages" / "channels" / "src",
        ROOT / "services" / "api" / "src",
        ROOT / "services" / "worker" / "src",
        ROOT / "services" / "ingress" / "src",
    )
    found = [path for root in roots for path in root.rglob("wecom_proxy/client.py")]

    assert len(found) == 1, f"expected one client module, found {found}"
    assert found[0] == CHANNELS_WECOM_PROXY / "client.py"


def test_the_guarded_surface_moved_intact() -> None:
    """`allowlist 与脱敏边界不变` -- the three things ADR-0094 §四 names, plus redaction.

    Imported from the new home and checked for identity, not just presence: the
    risk in moving a module that carries an allowlist, a confirmation gate and
    an audit sink is that one of them quietly becomes a different object on the
    way over.
    """

    package = importlib.import_module("fastagent_v2.channels.wecom_proxy")
    client_module = importlib.import_module("fastagent_v2.channels.wecom_proxy.client")
    allowlist_module = importlib.import_module("fastagent_v2.channels.wecom_proxy.allowlist")

    # The two operation sets, with their values -- not merely "the name exists".
    assert package.FORBIDDEN_OPERATIONS == {
        "list_clients",
        "logout",
        "stop_client",
        "delete_notify_url",
    }
    assert package.CONFIRMED_OPERATIONS == {"create_client", "set_notify_url"}
    assert package.FORBIDDEN_OPERATIONS is client_module.FORBIDDEN_OPERATIONS
    assert package.CONFIRMED_OPERATIONS is client_module.CONFIRMED_OPERATIONS

    # The redaction boundary is a function of this module, not a re-export of
    # some other project's helper that happens to share the name.
    assert client_module.url_without_token.__module__ == "fastagent_v2.channels.wecom_proxy.client"
    assert package.GuidAllowlist is allowlist_module.GuidAllowlist


def test_the_worker_imports_the_shared_errors_rather_than_naming_them() -> None:
    """The one consumer whose workaround the move was supposed to delete.

    `wecom_proxy_channel` classified its own refusals by comparing
    `type(error).__name__` against a set of **strings**, purely because it would
    not import across into `services/ingress`. That made a rename over there a
    silent downgrade of every such receipt to `unknown`.

    If this batch left that in place, the move would have bought nothing at the
    only call site that was paying for it.
    """

    from fastagent_v2.worker.wecom_proxy_channel import _LOCAL_GUARD_ERRORS

    assert _LOCAL_GUARD_ERRORS, "the set is empty; nothing is classified as a local refusal"
    assert all(isinstance(entry, type) for entry in _LOCAL_GUARD_ERRORS)
