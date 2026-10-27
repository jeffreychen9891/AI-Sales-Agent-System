"""Operator tool: create v2's own WeComProxy instance and bind it by QR scan.

**Not production code, and not run by any service.** It is the ceremony a human
performs once: `create_client` produces a guid and records it in the allowlist,
then the operator scans a code with the phone that owns the account.

Why a tool rather than a route: route freeze B2 (`C0A-032`..`C0A-041`) is the
surface that will eventually do this, and **none of B2 is built**. Until then the
ceremony still has to happen, and it should happen through
`RestrictedWeComProxyClient` -- the class with the default-deny allowlist, the
audit sink and the redaction boundary -- rather than through curl, which has
none of them.

Three things this tool does not do:

* **It never prints the credentials.** They come from the environment and are
  read once into a value object whose `__repr__` refuses to render them.
* **It never calls a forbidden operation.** `list_clients`, `logout`,
  `stop_client` and `delete_notify_url` raise from the client itself; this tool
  does not reach for them. In particular it cannot enumerate the account, which
  matters: the 2026-08-18 snapshot recorded three live instances under this
  appId, **two of them real merchants' production bindings** (ADR-0015 §3).
* **It does not set a notify URL.** The inbound half is deferred -- there is no
  public callback address (user ruling, 2026-09-04) -- and `set_notify_url` on a
  fresh instance would point the provider at nothing.

Usage:

    FASTAGENT_WECOM_PROXY_BASE_URL=... \\
    FASTAGENT_WECOM_PROXY_APP_ID=... \\
    FASTAGENT_WECOM_PROXY_APP_SECRET=... \\
    python scripts/wecom_proxy_bind.py --allowlist /etc/fastagent-v2-stage/guid-allowlist.json \\
        create --device-name v2-stage-01

    ... qrcode --guid <guid>   # writes the PNG to a file; scan it
    ... poll   --guid <guid>   # until loginQrcodeStatus says 2 (or 10, then `verify`)

The five states this can end in are WeComProxy's, not ours -- see
`QRCODE_STATUS`. Note that a scan can land on **10**, "enter the 6-digit code",
which is a second human step and not an error.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
# The client imports `fastagent_v2.runtime` too, and `fastagent_v2` is a
# namespace package split across several trees -- so every root it reaches goes
# on the path, not just the one holding the module named in the import below.
# `packages/channels/src` is where the client itself lives (B2-5);
# `packages/runtime/src` is still needed for the redaction helpers it uses.
#
# **`services/ingress/src` is no longer among them, and that is the point of
# B2-5**: this tool used to need the ingress tree on its path in order to reach
# a client that ingress itself never used.
#
# **This list is not covered by the gate.** `ci.sh` exports its own
# `PYTHONPATH`, so the import below resolves under pytest and mypy whatever this
# loop says -- a stale entry here breaks only when a human runs the tool. Same
# shape as the packaging list that
# `tests/contracts/test_packaging_registers_every_package.py` now guards, which
# is why this comment says so out loud rather than trusting the next reader to
# notice.
for _src in ("packages/runtime/src", "packages/channels/src"):
    sys.path.insert(0, str(_ROOT / _src))

from fastagent_v2.channels.wecom_proxy import (  # noqa: E402
    GuidAllowlist,
    RestrictedWeComProxyClient,
    WeComProxyCredentials,
)
from fastagent_v2.channels.wecom_proxy.credentials import (  # noqa: E402
    credentials_mode_refusal,
)

#: 仪式审计文件的落盘权限。**`stage_preflight` 对 `provider-secrets/` 下
#: 没点名的文件要求恰好这个值**，所以写入方按它落、并用同一条规则自检。
AUDIT_FILE_MODE = 0o600

#: 文件的 mode 位是不是真的。Windows 上 `os.fstat` 对任何文件都报 `0o666`，
#: 自检会拒绝每一次追加。所以自检的**判断**平台无关地跑——用例因此能在本仓
#: 跑在 Windows 上的门禁里验它——**停下来**这一步只在 mode 位有意义的地方发生。
#: 用例可以把它改成 `True` 来验「停下来」那一半（`_file_audit` 每次读它）。
MODE_BITS_ARE_REAL = os.name == "posix"

CONFIRMED = "--confirmed"

#: `loginQrcodeStatus`, and what each value means.
#:
#: **Read out of v1's production code, not guessed.** v2 has never completed
#: this ceremony, so the only place these values are written down is the system
#: that has: `backend/app/api/v1/wechat.py` returns
#: `result.get("loginQrcodeStatus", 0)` and
#: `admin-frontend/src/pages/Employees/Employees.tsx` maps it to the messages
#: below. (Reading v1 is a read-only inspection, which `CLAUDE.md` permits;
#: nothing here writes to v1 or reuses any of its bindings.)
#:
#: The first version of this tool polled a field called `status` and compared it
#: against strings -- it would have polled until the deadline and reported
#: failure on a scan that had actually succeeded.
QRCODE_STATUS = {
    -1: "二维码已过期，请重新生成",
    0: "等待扫码……",
    1: "已扫码，请在手机上确认登录……",
    2: "登录成功",
    4: "已取消登录",
    10: "需要输入 6 位验证码",
}
QRCODE_BOUND = 2
QRCODE_NEEDS_CODE = 10
QRCODE_TERMINAL = frozenset({-1, 4})


def _status_of(state: dict[str, Any]) -> int:
    """`loginQrcodeStatus`, defaulted the way v1 defaults it."""

    try:
        return int(state.get("loginQrcodeStatus", 0))
    except (TypeError, ValueError):
        return 0



def _credentials() -> WeComProxyCredentials:
    missing = [
        name
        for name in (
            "FASTAGENT_WECOM_PROXY_BASE_URL",
            "FASTAGENT_WECOM_PROXY_APP_ID",
            "FASTAGENT_WECOM_PROXY_APP_SECRET",
        )
        if not os.getenv(name)
    ]
    if missing:
        raise SystemExit(f"missing environment: {', '.join(missing)}")
    return WeComProxyCredentials(
        base_url=os.environ["FASTAGENT_WECOM_PROXY_BASE_URL"].rstrip("/"),
        app_id=os.environ["FASTAGENT_WECOM_PROXY_APP_ID"],
        app_secret=os.environ["FASTAGENT_WECOM_PROXY_APP_SECRET"],
    )


logger = logging.getLogger("wecom_proxy_bind")


def audit_path_for(allowlist_path: Path) -> Path:
    """Next to the allowlist, because they record two halves of one ceremony."""

    return allowlist_path.with_name(f"{allowlist_path.stem}-audit.jsonl")


def _file_audit(allowlist_path: Path) -> Callable[[dict[str, Any]], None]:
    """Write every audit record to a file, and say where.

    **The default sink is `logger.info`, and this script never configured
    logging** -- root logger with no handler, and `lastResort` only speaks at
    WARNING and above. So `create_client / created`, `rejected_guid` and
    `declined` went nowhere at all (independent review 2026-09-05, P2-4). The
    module header above says this tool exists *because* the client has "the
    default-deny allowlist, the audit sink and the redaction boundary"; one of
    those three was, in fact, a no-op.

    It matters more than a missing log line: the 2026-09-05 ceremony that
    created `v2-stage-01` ran through this tool, and the only trace it left was
    the allowlist file. **A record of who created which instance, when, is
    exactly what an operator tool for a shared provider account is for.**

    Records arrive already redacted (`_record` does that before the sink sees
    them), so this writes them out as they come. Appending JSON lines rather
    than rewriting: a ceremony log that can lose earlier entries is not a log.

    **Mode `0600`, set at creation and re-asserted on every append** (运维报障
    2026-09-11). This file is born in `provider-secrets/`, and
    `stage_preflight.py` requires mode `600` of everything it finds there
    (`must have mode 600; found {mode:o}`). `Path.open("a")` creates with the
    process umask -- `0664` on the Stage host -- so **the first deployment after
    anyone ran this tool was refused by preflight.**

    **The failure time and the cause are completely disconnected**: the tool is
    run during a binding ceremony, and the deployment it breaks happens on some
    later day, by someone else, with an error naming a file they did not create.
    That is the shape this batch exists to remove.

    **Why `os.open` rather than `open` + `chmod`**: the mode argument applies at
    creation, so there is no window in which the file exists more widely readable
    than it should be -- the same argument `_write_token_file` in
    `seed_stage_real_tenant.py` makes about the neighbouring token file.

    **Why the unconditional narrowing as well, and why it is not the mistake
    `allowlist.py` warns about**: `_restrict_to_owner` there records that an
    unconditional `fchmod` to `0600` once undid an operator's deliberate `0640`,
    after which the worker could not read the allowlist and every send was
    refused silently. **That case and this one differ in whether widening is ever
    legitimate.** The allowlist has a second reader (the worker container), so
    `0640` is a real configuration. This audit file has **no reader at all** --
    nothing in the deployment opens it -- and preflight refuses anything but
    `600`. A widened audit file is therefore never somebody's intent; it is the
    umask, and leaving it is leaving the next deployment's failure in place.

    On Windows the mode argument does not produce POSIX permissions. Stage is
    Linux; the tests say so rather than asserting what the platform cannot
    deliver -- see `test_the_audit_file_is_created_owner_only`.
    """

    destination = audit_path_for(allowlist_path)

    def sink(record: dict[str, Any]) -> None:
        line = json.dumps(
            {"at": datetime.now(UTC).isoformat(), **record}, ensure_ascii=False, default=str
        )
        descriptor = os.open(
            destination, os.O_CREAT | os.O_WRONLY | os.O_APPEND, AUDIT_FILE_MODE
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, AUDIT_FILE_MODE)
            else:  # pragma: no cover - developer machines only; Stage runs on Linux
                os.chmod(destination, AUDIT_FILE_MODE)
            os.write(descriptor, (line + "\n").encode("utf-8"))
            # **先留痕，再自检**（独立审查 P2-1）。自检失败会当场停下，而到这里
            # 供应商侧的动作**已经发生且不可逆**——留痕排在自检前面，是为了那条
            # 记录无论走哪条路都出现在操作者眼前。
            logger.info("wecom_proxy.audit %s", line)
            # **写完之后拿判定方的那条规则自检**（ADR-0113 §五，2026-09-11 继承）。
            # 先前写入方硬编码一个 mode，而 `stage_preflight` 另外要求一个 mode
            # ——**一个文件两套独立的规则**，它们正是这样漂开的（写入方按 umask、
            # 判定方要 600，于是仪式之后的下一次部署被拒）。
            #
            # **要说准继承到了哪一步**（独立审查 P1-3，改正本批提交信息里
            # 「现在两边问的是同一个函数」那句话）：写入方现在问的是
            # `credentials_mode_refusal`，而 `stage_preflight` 那一侧**仍然是它
            # 自己那条 `if mode != 0o600`**。两条规则今天恰好相等，但仍然是两条。
            # 让它们变成一条要动门禁脚本，另开一批；在那之前，
            # `test_the_two_rules_written_on_the_audit_file_have_a_non_empty_intersection`
            # 是「它们保持相等」的可执行形式——也就是 ADR-0085 那句
            # 「两个半边各自被执行、交集为空」的反面。
            refusal = credentials_mode_refusal(
                stat.S_IMODE(os.fstat(descriptor).st_mode),
                require_exact=AUDIT_FILE_MODE,
            )
            if refusal is not None and MODE_BITS_ARE_REAL:
                raise SystemExit(
                    f"{destination} {refusal}。这个文件落在 provider-secrets/ 下，"
                    "而部署前置按同一条规则检它——现在就停，"
                    "好过让下一次部署带着一个没人认得的文件失败。\n"
                    "**供应商侧的动作已经发生，停在这里不会把它撤销**——"
                    f"刚写下的这条记录就是它，guid 在里面：\n{line}"
                )
        finally:
            os.close(descriptor)

    return sink


def _client(allowlist_path: Path, *, confirmed: bool) -> RestrictedWeComProxyClient:
    def confirm(operation: str, details: dict[str, Any]) -> bool:
        """The per-call confirmation `create_client` requires.

        Default-deny lives in the client (`_deny_unconfirmed`); this returns True
        only when the operator passed `--confirmed` on *this* invocation. A
        confirmation that persists across runs would not be a confirmation.
        """

        if not confirmed:
            print(f"refused: {operation} needs {CONFIRMED} on this invocation", file=sys.stderr)
            print(f"  details: {json.dumps(details, ensure_ascii=False)}", file=sys.stderr)
            return False
        return True

    return RestrictedWeComProxyClient(
        credentials=_credentials(),
        allowlist=GuidAllowlist(allowlist_path),
        confirm=confirm,
        audit=_file_audit(allowlist_path),
    )


def main() -> int:
    # stderr, not stdout: `qrcode --out` and `allowlist` print things an
    # operator pipes elsewhere, and log lines in that stream would corrupt them.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument(CONFIRMED, action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a new instance and allowlist its guid")
    create.add_argument("--device-name", required=True)

    qrcode = sub.add_parser("qrcode", help="fetch the login QR payload for a guid")
    qrcode.add_argument("--guid", required=True)
    qrcode.add_argument("--reverify", action="store_true")
    qrcode.add_argument("--out", type=Path, help="where to write the PNG")

    poll = sub.add_parser("poll", help="poll the scan state until it settles")
    poll.add_argument("--guid", required=True)
    poll.add_argument("--seconds", type=int, default=120)

    verify = sub.add_parser("verify", help="submit the confirmation code, if the flow asks for one")
    verify.add_argument("--guid", required=True)
    verify.add_argument("--code", required=True)

    sub.add_parser("allowlist", help="show what this host has recorded")

    arguments = parser.parse_args()
    if arguments.command == "allowlist":
        for entry in GuidAllowlist(arguments.allowlist).entries():
            print(f"{entry.guid}  device_name={entry.device_name}")
        return 0

    client = _client(arguments.allowlist, confirmed=arguments.confirmed)

    if arguments.command == "create":
        guid = client.create_client(device_name=arguments.device_name)
        print(f"guid: {guid}")
        print("recorded in the allowlist; nothing else can widen it")
        return 0

    if arguments.command == "qrcode":
        payload = client.get_login_qrcode(guid=arguments.guid, is_reverify=arguments.reverify)
        key = payload.get("loginQrcodeKey")
        image = payload.get("loginQrcodeBase64Data") or ""
        if not key:
            # v1 treats a missing key as a hard failure rather than an empty
            # QR: "获取二维码失败：平台未返回二维码数据".
            print(
                f"provider returned no loginQrcodeKey; keys were {sorted(payload)}",
                file=sys.stderr,
            )
            return 1
        # **Written to a file, not to the terminal.** `loginQrcodeBase64Data` is
        # a base64 PNG; printing the payload would fill the scrollback with it
        # and leave the one thing a human needs -- an image to scan -- unusable.
        target = arguments.out or Path(f"wecom-proxy-qrcode-{arguments.guid}.png")
        if image:
            target.write_bytes(base64.b64decode(image.split(",", 1)[-1]))
            print(f"qrcode written to: {target}")
        else:
            print("provider returned a key but no image data", file=sys.stderr)
        print(f"loginQrcodeKey: {key}")
        return 0

    if arguments.command == "poll":
        deadline = time.monotonic() + arguments.seconds
        last: int | None = None
        while time.monotonic() < deadline:
            state = client.check_login_qrcode(guid=arguments.guid)
            status = _status_of(state)
            if status != last:
                print(f"{status}: {QRCODE_STATUS.get(status, 'unknown status')}")
                last = status
            if status == QRCODE_BOUND:
                print("bound")
                return 0
            if status == QRCODE_NEEDS_CODE:
                print("run the `verify` subcommand with the 6-digit code", file=sys.stderr)
                return 2
            if status in QRCODE_TERMINAL:
                return 1
            time.sleep(3)
        print("still not bound within the window", file=sys.stderr)
        return 1

    if arguments.command == "verify":
        print(
            json.dumps(
                client.verify_login_qrcode(guid=arguments.guid, code=arguments.code),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
