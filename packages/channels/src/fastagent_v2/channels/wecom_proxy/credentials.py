from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from fastagent_v2.runtime.redaction import REDACTED

from .errors import WeComProxyConfigError

#: **The path this file has inside the worker container**, which is the only
#: place this default is consulted (`worker/cli.py` builds the provider client).
#:
#: It used to name `/etc/fastagent-v2-stage/wecom_proxy.env` -- a **host** path,
#: and one that was never mounted into any container. A worker that fell back to
#: it therefore failed at startup with a file-not-found, on a deployment whose
#: env file simply had not named the key. The mount it should have named has
#: existed since the provider-secrets batch: `worker` binds the deployment's
#: provider secret directory at `/run/provider-secrets`, read-only, group 10004.
#:
#: **The host-side consumer was removed rather than followed.**
#: `scripts/seed_channel_binding.py` used this as a fallback while running on the
#: host, where a container path is meaningless; it now requires the variable to
#: be set, exactly as it already did for the guid allowlist beside it.
DEFAULT_CREDENTIALS_PATH = "/run/provider-secrets/wecom_proxy.env"

_REQUIRED_KEYS = ("WECOM_PROXY_BASE_URL", "WECOM_PROXY_APP_ID", "WECOM_PROXY_APP_SECRET")


@dataclass(frozen=True, slots=True)
class WeComProxyCredentials:
    """Provider credentials held in memory only.

    ``__repr__`` and ``__str__`` are overridden because these objects end up in
    tracebacks. The secret they carry operates v2's own purchased WeComProxy
    connection -- not v1's, which is a separate connection with separate
    credentials (ADR-0027) -- and a provider credential in a traceback is a leak
    whoever it belongs to. Nothing in this class may ever render the secret.
    """

    base_url: str
    app_id: str
    app_secret: str

    def __repr__(self) -> str:
        # One redaction vocabulary, not three: this used to render its own
        # `<redacted>` literal (independent review 2026-09-02, P3-3).
        return (
            f"WeComProxyCredentials(base_url=..., "
            f"app_id={REDACTED}, app_secret={REDACTED})"
        )

    __str__ = __repr__


def credentials_mode_refusal(
    mode: int,
    *,
    owner_gid: int | None = None,
    expected_group_gid: int | None = None,
    require_exact: int | None = None,
) -> str | None:
    """Why this mode is unacceptable for a credentials file, or `None` if it is fine.

    **A pure function on purpose** (independent review 2026-09-09, P0-1).
    The rule used to live inline in `load_credentials`, guarded by
    `os.name == "posix"` -- so on the machine the gate actually runs pytest on
    (Windows), it was skipped entirely and **could not be tested at all**. That
    is how this rule and `stage_preflight`'s came to have an *empty
    intersection* without anything noticing: each was internally consistent and
    neither could be asked about the other.

    Split out so the two rules can be compared **as rules**, on any platform.

    **`require_exact` extends the same rule to the ceremony's audit file**
    (ADR-0113 §五, inherited 2026-09-11). That ADR wrote 「规则被提成纯函数，
    这是本条最该被继承的部分」 -- and it had not been inherited: the writer in
    `wecom_proxy_bind.py` hard-coded a mode while `stage_preflight` demanded one,
    **two independent rules for one file**, which is precisely how they drifted
    (the writer used the umask; the checker wanted `600`; the next deployment
    after any binding ceremony was refused).

    **The parameter exists because the two rules are genuinely different, not
    because one of them is wrong.** `0640` is acceptable to the base rule -- it
    is how a file readable by one more service is spelled -- and unacceptable to
    `stage_preflight` for anything in `provider-secrets/` it does not name. The
    audit file is in that second category, so its writer asks with
    `require_exact=0o600` and gets the same answer the deployment will give it.
    """

    # **`require_exact` 排在最前，为的是让两边的报错措辞对得上**（独立审查 P3-1）。
    # 排在最后时，一个 `0o664` 先命中 world 位那条，写入方说的是
    # 「must not be world accessible」，而运维在 preflight 那边看到的是
    # 「must have mode 600; found 664」——**同一个文件、同一个原因、两句话**。
    # 而这一批的由来正是一次排障；排障时两句话对不上，等于让下一个人自己去对。
    # **不传 `require_exact` 的调用方行为一个字没变**：下面三条的顺序照旧。
    if require_exact is not None and mode != require_exact:
        return f"must have mode {require_exact:o}; found {mode:o}"
    if mode & stat.S_IRWXO:
        return "must not be world accessible"
    if mode & (stat.S_IWGRP | stat.S_IXGRP):
        return "must not be group-writable or group-executable"
    if expected_group_gid is not None and owner_gid is not None and owner_gid != expected_group_gid:
        return f"must belong to group {expected_group_gid}"
    return None


def load_credentials(
    path: str | os.PathLike[str] = DEFAULT_CREDENTIALS_PATH,
    *,
    expected_group_gid: int | None = None,
) -> WeComProxyCredentials:
    """Read credentials from a file, never from the process environment.

    Reading from a file rather than the environment is what keeps the secret out
    of the image, the compose file, CI and ``docker inspect`` output. That half
    has not changed and is not negotiable.

    **What changed is the mode rule** (user ruling 2026-09-09; ADR-0113).
    It used to reject *any* group bit, which made the file readable by exactly
    one uid -- and that was right while only a human read it. The dispatcher now
    reads it from a container running as ``10001`` while the file is owned by
    the deployment user, and **a container reaches a file its own uid cannot
    only through a group**. The old rule and that requirement had **no
    intersection**: ``0600`` was unreadable, ``0640`` was refused here.

    So the rule is now the shape this repository already uses twice for "two
    uids, one secret" (migration role passwords, and the api's channel
    credentials under ADR-0061):

    * **no other bits at all** -- world-anything is still refused outright;
    * **group may read, never write or execute**;
    * **and, when the caller knows which group it should be, that exact gid** --
      ``expected_group_gid``. A readable file in the *wrong* group is a
      different failure from a readable file in the right one, and only the
      caller knows which group its deployment uses. `stage_preflight` pins the
      specific number on the host side; this pins it at the point of use.
    """
    target = Path(path)
    if not target.is_file():
        raise WeComProxyConfigError(f"WeComProxy credentials file is missing: {target}")
    if os.name == "posix":
        info = target.stat()
        refusal = credentials_mode_refusal(
            stat.S_IMODE(info.st_mode),
            owner_gid=info.st_gid,
            expected_group_gid=expected_group_gid,
        )
        if refusal is not None:
            raise WeComProxyConfigError(f"WeComProxy credentials file {refusal}: {target}")
    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    missing = [key for key in _REQUIRED_KEYS if not values.get(key)]
    if missing:
        # Names only. The values are the thing we are protecting.
        raise WeComProxyConfigError(
            f"WeComProxy credentials file is missing keys: {', '.join(missing)}"
        )
    return WeComProxyCredentials(
        base_url=values["WECOM_PROXY_BASE_URL"].rstrip("/"),
        app_id=values["WECOM_PROXY_APP_ID"],
        app_secret=values["WECOM_PROXY_APP_SECRET"],
    )
