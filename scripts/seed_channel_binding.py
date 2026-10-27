#!/usr/bin/env python
"""Operator tool: write the first `channel_bindings` row for a tenant.

**If the tenant does not exist yet, this is the wrong script.** Use
`scripts/seed_stage_real_tenant.py` -- it creates the tenant, publishes the
release and creates this binding as one ceremony, and it generates the path
token instead of taking one. This script is the **binding step alone**, for when
the tenant and release already exist.

**Never use `scripts/probe_real_provider_pipeline.py` for a real tenant**: it is
a destructive fixture that `TRUNCATE`s fifteen tables and seeds a binding whose
path token is a constant committed to this repository (ADR-0121 §四).

**This is the file ADR-0093 §四 (Q3) approved, and the one the activation
tripwire names by hand.** Everything about its shape follows from why it exists.

**Why a script and not a route.** `POST /channel-bindings` is out of M1a
(ADR-0048 K16), and the reason it went out has not changed: `create_client` and
`set_notify_url` are `CONFIRMED_OPERATIONS` -- **a route has nobody standing at
a terminal**. So the ceremony stays a script a person runs, and the script does
**not** call the provider at all: it records a binding for an instance that
already exists.

**Why in the repository.** K4's still-open half says the same thing about the
synthetic tenant's seed scripts: *"Stage 上有一个 X" 这个事实在仓库里没有对应物*.
A binding created by hand at a psql prompt is unreviewable, unrepeatable, and
invisible to the next person. This one is committed, reviewed, and replayable.

**Three refusals, and each is a rule someone already wrote down:**

1. **It refuses while the channel address looks like the sink placeholder**
   (ADR-0091). A binding is the last thing standing between an inbound message
   and a billed model call.

   > **What this check is, exactly** (independent review 2026-09-07, P1-2): it
   > reads `FASTAGENT_CHANNEL_SEND_URL` **from the operator's own environment**,
   > and on Stage that variable lives in `worker.env`, not in the shell running
   > a migration. **So it is a self-check, not a deployment check**: it cannot
   > stop someone who exports a value, and a value it does see may have nothing
   > to do with the one the worker will use. Reading the deployment's env
   > directly (`stage_preflight.parse_env` over `worker.env`) is the stronger
   > form and is deliberately **not** done here -- it would give this script an
   > opinion about where a deployment keeps its files. **The real check belongs
   > to the person running it**: read `worker.env` first, and record what it
   > said in the evidence for the run.

2. **It refuses to touch an existing binding.** Creating is not re-activating:
   the tripwire forbids `status = 'active'` writes for good reasons (a disabled
   binding was disabled by someone), and this tool has no business overriding
   that. `INSERT`, never `UPDATE`.
3. **It never prints, logs or accepts the token on the command line.** The token
   arrives in `FASTAGENT_SEED_CHANNEL_TOKEN` and only its SHA-256 is stored --
   the column holds a hash and the operator keeps the secret. **Surrounding
   whitespace is refused rather than stripped** (P2-4): one stray `\\n` is a
   different digest, the row would look perfectly valid, every callback would
   401 days later, and nothing here can recover the token to compare.

**It needs the migration role.** `channel_bindings` is `ENABLE + FORCE ROW LEVEL
SECURITY` (`001_m0.sql:299`) and this script sets no `app.tenant_id`, so the
duplicate check below only sees existing rows because `fastagent_migrator` is
`rolsuper` (`031`). Run under any other role and that check degrades from a
refusal into **silence** -- it would find nothing and let the `INSERT` fail on
RLS instead (P3-1). Use `FASTAGENT_MIGRATION_DATABASE_URL`, which is what the
runbook hands operators anyway.

**Its expiry is written down**: when K16 is unblocked and `POST /channel-bindings`
exists, this script and its named exception in
`tests/security/test_channel_binding_activation_guard.py` go away together
(T195).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

from fastagent_v2.runtime.auth import sha256_token
from psycopg import Connection
from psycopg.rows import dict_row

#: The address ADR-0091 pins the Stage deployment to while nothing may be sent.
#: A binding created against it would produce a `manual_task` per message and
#: nothing else, which is the state that ADR asks a batch to *leave* first.
SINK_PLACEHOLDER = "channel-sink.invalid"


def _require_readable(
    variable: str, default: str | None, *, must_be_non_empty: bool = False
) -> str:
    """The file `variable` names must exist and be readable, or refuse.

    Checked with `stat`/`open` rather than trusted, because the whole point of
    ADR-0091's guard is that "configured" and "works" are different states and
    only one of them may open an inbound path.
    """

    configured = os.environ.get(variable, "").strip() or default
    if not configured:
        raise SystemExit(
            f"{variable} is not set. FASTAGENT_CHANNEL_PROVIDER=wecom_proxy means the "
            "worker sends through the provider, and it cannot do that without this "
            "file. ADR-0091: outbound has to work before inbound is opened."
        )
    path = Path(configured)
    if not path.is_file():
        raise SystemExit(f"{variable} points at {configured}, which is not a file.")
    try:
        contents = path.read_bytes()
    except OSError as error:
        raise SystemExit(
            f"{variable} points at {configured}, which cannot be read: {error}"
        ) from error
    if must_be_non_empty and not contents.strip():
        raise SystemExit(
            f"{variable} points at {configured}, which is empty. An empty guid allowlist "
            "is default-deny: every send would raise GuidNotAllowedError and nothing "
            "would be delivered."
        )
    return configured


def _token_hash() -> str:
    """The token's digest, computed the way inbound verification computes it.

    `sha256_token` rather than a local `hashlib` call (P2-3): the ingress path
    verifies with it, `scripts/probe_real_provider_pipeline.py` seeds with it,
    and this repository has already paid once for two copies of a digest drifting
    apart (`subject_fingerprint`, bare SHA-256 to keyed HMAC, T154 / ADR-0064).
    If that function ever changes shape, this script must change with it rather
    than keep writing rows that are well-formed and never match.
    """

    token = os.environ.get("FASTAGENT_SEED_CHANNEL_TOKEN", "")
    if token != token.strip():
        raise SystemExit(
            "FASTAGENT_SEED_CHANNEL_TOKEN has leading or trailing whitespace. "
            "Refusing rather than trimming: a stray newline is a different "
            "digest, the row would look valid, and every callback would fail "
            "authentication days later with nothing left to compare against."
        )
    if len(token) < 32:
        raise SystemExit(
            "FASTAGENT_SEED_CHANNEL_TOKEN must be set to the channel's path token "
            "(at least 32 characters). It is never printed and never stored -- "
            "only its SHA-256 goes into the row."
        )
    return sha256_token(token)


def _refuse_while_nothing_can_be_delivered() -> str:
    """ADR-0091's guard: do not open inbound while outbound goes nowhere.

    **What the guard is actually about is delivery, not a URL** -- its own
    words: "every message this binding admits becomes a billed model call and a
    manual task, and nothing is delivered". `FASTAGENT_CHANNEL_SEND_URL` used to
    be the only way to tell, because `HttpChannelAdapter` was the only way to
    send.

    **B2-3 gave the deployment a second way to send, and the guard has to know
    about it or it becomes a lie.** With `FASTAGENT_CHANNEL_PROVIDER=wecom_proxy`
    the dispatcher builds a provider adapter per dispatch and **never reads
    `channel_send_url` at all** -- so an operator could satisfy this check by
    setting a variable nothing consumes. A guard that is passed by writing a
    plausible value into an unused setting is worse than no guard: it still
    looks like it is protecting something.

    So the question is asked once, in the terms the ADR used: **can anything be
    delivered?**
    """

    provider = os.environ.get("FASTAGENT_CHANNEL_PROVIDER", "sink").strip().lower()
    if provider == "wecom_proxy":
        # Outbound goes through the provider adapter, so `channel_send_url` is
        # not consulted and is not asked about.
        #
        # **But "the provider is named" is not "anything can be delivered"**
        # (independent review 2026-09-09, P1-2). The first version of this
        # branch returned here immediately, and a probe showed the cost: set
        # only `FASTAGENT_CHANNEL_PROVIDER=wecom_proxy`, configure nothing else,
        # and the guard opened -- while in that exact configuration
        # `channel_factory_for` raises `SettingsError` and **the worker does not
        # start at all**. Inbound open, every message a billed model call and a
        # manual task, nothing delivered: precisely what ADR-0091 §四.1 forbids,
        # and worse than it imagined, because there the worker was at least
        # running.
        #
        # The comment that version carried -- "the remaining risk is the
        # provider being misconfigured, and that is not something this script
        # can see" -- was simply wrong. **These two are exactly what it can
        # see**: two environment variables pointing at two files.
        # **No default, per ADR-0120** (revising ADR-0112 §二.2's half-sentence).
        # `DEFAULT_CREDENTIALS_PATH` is the path this file has *inside the worker
        # container*; this script runs on the host, where a container path is
        # meaningless. The allowlist check on the next line has passed `None`
        # since ADR-0112 landed -- the two are a pair and only one of them ever
        # had a default, so this **removes an asymmetry rather than inventing a
        # rule**. A missing variable is refused by `_require_readable`'s own
        # "is not set" branch, which already says the right thing.
        _require_readable("FASTAGENT_CHANNEL_CREDENTIALS_FILE", None)
        # The allowlist is default-deny, so a missing or empty one means every
        # send raises `GuidNotAllowedError` -- still nothing delivered. This
        # batch's own `test_a_configured_but_absent_allowlist_is_default_deny`
        # pins that behaviour; this refuses to open inbound on top of it.
        _require_readable("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", None, must_be_non_empty=True)
        return f"provider:{provider}"

    address = os.environ.get("FASTAGENT_CHANNEL_SEND_URL", "")
    if not address:
        raise SystemExit(
            "FASTAGENT_CHANNEL_SEND_URL is unset in this shell. This is the "
            "self-check described in the module docstring: it cannot see the "
            "deployment's worker.env, so it asks you to have read it. Set the "
            "variable to the value that is actually in worker.env -- and if that "
            "value is still the sink placeholder, this tool is refusing for the "
            "right reason (ADR-0091)."
        )
    if SINK_PLACEHOLDER in address:
        raise SystemExit(
            f"FASTAGENT_CHANNEL_SEND_URL still points at {SINK_PLACEHOLDER}, and "
            "FASTAGENT_CHANNEL_PROVIDER is not set to a real channel. "
            "ADR-0091: open outbound before opening inbound -- otherwise every "
            "message this binding admits becomes a billed model call and a "
            "manual task, and nothing is delivered. Either point the sink at "
            "something that delivers, or set FASTAGENT_CHANNEL_PROVIDER to the "
            "provider the deployment really uses."
        )
    return address


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--config-id", required=True)
    parser.add_argument("--profile-release-id", required=True, type=UUID)
    parser.add_argument(
        "--provider-instance-id",
        default=None,
        help="the WeComProxy guid this binding sends through, if it is known yet",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "run every refusal and print the row that would be written, without "
            "writing it. The output is the evidence for the real run (P3-2)."
        ),
    )
    arguments = parser.parse_args()

    address = _refuse_while_nothing_can_be_delivered()
    token_hash = _token_hash()

    dsn = os.environ.get("FASTAGENT_MIGRATION_DATABASE_URL")
    if not dsn:
        raise SystemExit("FASTAGENT_MIGRATION_DATABASE_URL is required")

    # Printed for both modes, because this is the record of what was decided:
    # which tenant, which config, which release, and **which address the
    # operator says the deployment sends through**. The token appears nowhere.
    print(f"config_id          = {arguments.config_id}")
    print(f"tenant_id          = {arguments.tenant_id}")
    print(f"profile_release_id = {arguments.profile_release_id}")
    print(f"provider_instance  = {arguments.provider_instance_id or '(not set yet)'}")
    print(f"channel_send_url   = {address}   (as seen by this shell)")
    print("current_token_hash = (sha256 of FASTAGENT_SEED_CHANNEL_TOKEN; not printed)")

    if arguments.dry_run:
        print("--dry-run: nothing was written")
        return 0

    with Connection.connect(dsn, row_factory=dict_row) as connection:
        existing = connection.execute(
            "SELECT status FROM channel_bindings WHERE config_id = %s",
            (arguments.config_id,),
        ).fetchone()
        if existing is not None:
            raise SystemExit(
                f"a binding for config_id {arguments.config_id!r} already exists "
                f"(status={existing['status']!r}). This tool creates; it does not "
                "re-activate or overwrite -- a disabled binding was disabled by "
                "someone, and that decision is not this script's to reverse."
            )
        connection.execute(
            """
            INSERT INTO channel_bindings(
                config_id, tenant_id, profile_release_id,
                current_token_hash, provider_instance_id
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                arguments.config_id,
                arguments.tenant_id,
                arguments.profile_release_id,
                token_hash,
                arguments.provider_instance_id,
            ),
        )
        connection.commit()

    print("created")
    print(
        "The token itself was never stored. Keep it: the channel presents it in "
        "the callback path, and nothing here can recover it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
