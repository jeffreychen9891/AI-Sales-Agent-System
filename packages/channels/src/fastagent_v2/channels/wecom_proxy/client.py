from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastagent_v2.runtime.redaction import REDACTED, redact

from .allowlist import GuidAllowlist
from .credentials import WeComProxyCredentials
from .errors import (
    ConfirmationRequiredError,
    ForbiddenOperationError,
    GuidNotAllowedError,
    WeComProxyError,
)

logger = logging.getLogger(__name__)

#: A sink receives records that have already been through `redact()`, and that
#: narrows what `Any` means here (T20): `datetime` arrives as an ISO string,
#: `Decimal` as a fixed-point string, `UUID` as a string, an unrecognised object
#: as `{"type": ..., "value": "[REDACTION_FAILED_DROPPED]"}`, and **any field
#: whose name looks like a credential arrives as `[REDACTED]`** -- including
#: business-shaped names such as `idempotency_key`, which `is_sensitive_key`
#: matches by substring (the accepted cost of T13's P2-1). Two consequences
#: worth stating rather than discovering: a sink that wants to write a `UUID`
#: into a uuid column must parse the string back, and an audit field must not be
#: given a credential-shaped name or it will be redacted out of its own record.
AuditSink = Callable[[dict[str, Any]], None]
Confirmer = Callable[[str, dict[str, Any]], bool]
#: `(path, payload, token, *, budget: float | None = None) -> dict`.
#:
#: **Spelled `...` rather than written out** because the budget is keyword-only
#: and optional: a caller with no deadline omits it entirely. Widened for T96's
#: P1-2 -- before it, a send could take as long as httpx felt like, and an
#: overrun of the outbox lease is not a slow log line, it is another dispatcher
#: claiming the row and sending the customer the same message twice.
Transport = Callable[..., dict[str, Any]]

#: Operations this client refuses to implement. Each one is destructive to a
#: live connection: enumerating, logging out, stopping, or unhooking the notify
#: URL of one.
#:
#: ADR-0015 justified this list as v1 isolation, on the belief that v2 borrowed
#: v1's provider account and that one appId could operate every instance under
#: it. ADR-0027 corrected that: WeComProxy's permission boundary is the
#: connection, not the account -- each connection is bought separately and binds
#: exactly one WeCom account -- and v2 runs on its own purchased connection,
#: disjoint from v1's. There is no shared account and no reach into v1.
#:
#: The list stays anyway, on the weaker but still valid ground of ordinary blast
#: radius: logging out or stopping v2's *own* connection would cut off a
#: conversation in progress, and M1a has no need to do either. Relaxing it is a
#: separate decision on ordinary risk grounds -- it is no longer governed by the
#: highest-priority isolation rule.
FORBIDDEN_OPERATIONS = frozenset({"list_clients", "logout", "stop_client", "delete_notify_url"})

#: Operations requiring the operator's confirmation for that specific call.
CONFIRMED_OPERATIONS = frozenset({"create_client", "set_notify_url"})


def _deny_unconfirmed(operation: str, details: dict[str, Any]) -> bool:  # noqa: ARG001 - 确认回调的协议签名；本实现不看 details，但形参由调用方约定
    raise ConfirmationRequiredError(
        f"{operation} requires per-call confirmation and no confirmer was supplied"
    )


#: 大文件的 `fileId` 是 `fileConvertId`，而它以这个字符开头。
#:
#: **一个字符的约定，两个模块共用**：`/msg/sendFile` 与 `/msg/sendVideo` 都写着
#: 「大文件时 `fileId` 要送 `fileConvertId`（带 `*` 开头），且此时 `fileAesKey`
#: 可为空」，而 CDN 的大文件下载接口写的是同一规则的另一侧
#: （`fileId` 以 `*` 开头时 `fileKey` 为空）。
#: `docs/wecom-proxy-message-api.md:1306` 把这两侧并排记了下来。
BIG_FILE_ID_PREFIX = "*"


def is_big_file(file_id: str) -> bool:
    """这个 `fileId` 指的是不是一个大文件（`fileConvertId`）。

    **判据是 `fileId` 的形状，不是字节数。** 用大小判会立出第二个「大」的定义，
    而两个定义第一次不一致的时候，症状是一次没有密钥的小文件发送 ——
    供应商回成功，收件人打不开。
    """

    return file_id.startswith(BIG_FILE_ID_PREFIX)


def url_without_token(url: str) -> str:
    """Render a notify URL as origin plus first path segment only.

    v2's ingress authenticates callbacks with a 256-bit path token, so the full
    notify URL is a credential. Confirmation prompts, audit records and logs
    only ever see this reduced form.
    """
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return "<malformed-url>"
    host, _, path = remainder.partition("/")
    first_segment = path.split("/", 1)[0] if path else ""
    suffix = f"/{first_segment}/..." if first_segment else "/"
    return f"{scheme}://{host}{suffix}"


def _trailing_path_segment(url: str) -> str:
    """The last path segment, but only when the URL really has a path.

    Returns `""` for anything shorter than `scheme://host/a/b`. The caller uses
    the result as a *known secret* to strike out of a provider response, and a
    wrong guess there is not harmless: `https://host` would otherwise yield
    `"host"`, and every occurrence of that word in the response would be
    replaced. See `set_notify_url` for why the fallback is safe.
    """

    _, separator, remainder = url.partition("://")
    if not separator:
        return ""
    _, _, path = remainder.partition("/")
    segments = [segment for segment in path.rstrip("/").split("/") if segment]
    return segments[-1] if len(segments) >= 2 else ""


def redact_urls(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """A copy of `value` with notify URLs reduced and known secrets removed.

    **The provider echoes the currently registered notify URL back in its
    response to `setGuidNotifyUrl`, and the last segment of that URL is v2's
    256-bit channel path token.** Confirmation prompts and audit records have
    always reduced it; the return value did not, which pushed the redaction
    duty onto every caller. **That is where the 2026-09-02 token leak came
    from** -- not from a caller being careless, but from an interface that
    handed out a credential and trusted everyone downstream to notice.

    Two passes, because either alone leaves a hole:

    * anything that parses as a URL is reduced to origin plus first segment,
      which is what `url_without_token` already does for display. This catches
      the echo even under a key nobody anticipated;
    * any *known* secret string is replaced outright. The token can appear
      without a URL around it -- a provider is free to return it as its own
      field -- and no amount of URL parsing would find it there.

    Applied to keys as well as values: a provider that used the URL as a map
    key would otherwise carry it straight out.

    **Two consequences of the first pass, both deliberate, both recorded rather
    than fixed** (independent review 2026-09-08, P3-1 and P3-3):

    * it reduces a URL found *inside* a longer string, **and everything after
      the URL goes with it**: `"cannot reach https://h/a/b -- retry"` becomes
      `"cannot reach https://h/a/..."`, losing `-- retry`. Truncating a
      diagnostic is the price of not having to decide where a URL ends inside
      arbitrary prose, and the trade is only worth it because the alternative
      is emitting a credential;
    * because keys are reduced too, **two keys that reduce to the same string
      collapse** -- the dict comprehension keeps the last. Unreachable today
      (keys are short field names, none of them URLs), and named here because
      "keys are reduced as well" is a design point rather than an accident, so
      its cost belongs next to it.
    """

    if isinstance(value, str):
        reduced = url_without_token(value) if "://" in value else value
        for secret in secrets:
            if secret and secret in reduced:
                reduced = reduced.replace(secret, REDACTED)
        return reduced
    if isinstance(value, dict):
        return {
            redact_urls(key, secrets=secrets): redact_urls(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_urls(item, secrets=secrets) for item in value]
    return value


class RestrictedWeComProxyClient:
    """The only WeComProxy surface v2 is allowed to call.

    Three guarantees, in order of how much they matter:

    1. Operations in :data:`FORBIDDEN_OPERATIONS` raise instead of executing.
    2. Every guid-bearing call goes through a default-deny allowlist that only
       ``create_client`` can widen, and only with a guid the provider just
       returned to v2.
    3. ``create_client`` and ``set_notify_url`` additionally require a per-call
       confirmation from the operator.

    Every rejection is audited. Audit records carry the operation, the guid and
    the outcome -- never the credentials, never a raw notify URL.
    """

    def __init__(
        self,
        *,
        credentials: WeComProxyCredentials,
        allowlist: GuidAllowlist,
        audit: AuditSink | None = None,
        confirm: Confirmer | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._credentials = credentials
        self._allowlist = allowlist
        self._audit = audit or self._default_audit
        self._confirm = confirm or _deny_unconfirmed
        self._transport = transport or self._http_transport
        self._timeout = timeout
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    # -- auditing -------------------------------------------------------

    @staticmethod
    def _default_audit(record: dict[str, Any]) -> None:
        logger.info("wecom_proxy.audit %s", record)

    def _record(self, operation: str, outcome: str, **fields: Any) -> None:
        """Hand the sink a record that is already safe to write down.

        Redaction happens **here, not in `_default_audit`** (T20). The sink is an
        injection point -- callers pass their own, and this class must not depend
        on every future sink remembering to redact. Redacting at the boundary
        means the record this class *emits* is safe, whoever receives it.

        The payload is arbitrary by construction: `_record(**fields)` accepts
        whatever the call site passes, and two of those call sites pass material
        this class does not control -- `reason=str(exc)` from a rejected guid,
        and `**details` from a declined confirmation. Before this, all of it went
        straight to `logger.info` as a formatted dict.

        Sequencing note: `redact()` only became safe to put in front of a real
        log path once it stopped blanking token counts (T22) and dropping
        `Decimal` amounts (T23). Wiring this first would have shipped both.
        """

        # Only the caller-supplied fields go through `redact()`. The three
        # values above are this class's own and never need redacting, and
        # including them cost more than it looked: `redact()` fails closed by
        # returning the *string* `[REDACTION_FAILED_DROPPED]`, so one
        # unrenderable field used to turn the whole record into that string --
        # the sink would receive a `str` where its type says `dict`, and the
        # record would stop saying which operation had been refused, exactly
        # when something strange was happening. The `cast` that hid this from
        # mypy is gone with it (independent review 2026-09-03, P3-1).
        redacted = redact(fields)
        if not isinstance(redacted, dict):
            # Redaction failed wholesale. Keep the identity of the record and
            # say plainly that the payload did not survive, rather than losing
            # both.
            redacted = {"fields": redacted}
        self._audit(
            {
                "operation": operation,
                "outcome": outcome,
                "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                **redacted,
            }
        )

    # -- guards ---------------------------------------------------------

    def _guarded_guid(self, operation: str, guid: str) -> str:
        try:
            allowed = self._allowlist.require(guid)
        except GuidNotAllowedError as exc:
            # The attempted guid is echoed on purpose: a rejected guid is by
            # definition not one of ours, and knowing which foreign guid was
            # attempted is the entire value of the record.
            self._record(operation, "rejected_guid", guid=guid, reason=str(exc))
            raise
        return allowed

    def _require_confirmation(self, operation: str, details: dict[str, Any]) -> None:
        """Ask the operator, with `details` **not** redacted -- deliberately.

        `_record` argues that an injection point cannot be trusted to redact for
        itself, and `_confirm` is the same shape of injection point. The
        argument still does not carry here, and the reason is what the two
        outputs are *for*: an audit record is written down and read later, while
        a confirmation prompt is read *now*, by a person deciding whether to
        permit the operation. `[REDACTED]` in that prompt would leave them
        confirming something they cannot see -- which is worse than the exposure
        it prevents, because the prompt is the last human check before a
        destructive call.

        What makes this safe is that the call sites reduce their material
        *semantically* before it gets here rather than redacting it: today they
        pass `{"device_name": ...}` and a `notify_url` already stripped by
        `url_without_token`. That is a constraint on the callers, and it is the
        one to keep: **decide at the call site what an operator needs to see,
        and pass only that.** The declined path then hands the same dict to
        `_record`, where it *is* redacted, because at that point it has stopped
        being a prompt and become a record (independent review 2026-09-03, P2-2).
        """

        if operation not in CONFIRMED_OPERATIONS:
            return
        if not self._confirm(operation, details):
            self._record(operation, "declined", **details)
            raise ConfirmationRequiredError(f"{operation} was not confirmed by the operator")

    # -- transport ------------------------------------------------------

    def _timeout_for(self, budget: float | None) -> httpx.Timeout | float:
        """Four phases from one number, or the constructor default.

        A bare float hands httpx the **same** value for connect, read, write and
        pool, so a call carrying a 20-second budget can legitimately spend 80
        seconds inside one request. `channel.py` splits its query budget four
        ways for exactly this reason; this is the same arithmetic, applied where
        the provider client actually builds the client.
        """

        if budget is None:
            return self._timeout
        share = max(0.05, budget / 4)
        return httpx.Timeout(connect=share, read=share, write=share, pool=share)

    def _token_value(self, *, budget: float | None = None) -> str:
        now = datetime.now(UTC)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token
        with httpx.Client(trust_env=False, timeout=self._timeout_for(budget)) as client:
            response = client.post(
                f"{self._credentials.base_url}/member/login",
                json={
                    "appId": self._credentials.app_id,
                    "appSecret": self._credentials.app_secret,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, 200):
            raise WeComProxyError(f"WeComProxy login failed: {payload.get('msg')}")
        token = (payload.get("data") or {}).get("token")
        if not token:
            raise WeComProxyError("WeComProxy login returned no token")
        self._token = str(token)
        self._token_expires_at = now + timedelta(hours=12)
        return self._token

    def _http_transport(
        self,
        path: str,
        payload: dict[str, Any],
        token: str,
        *,
        budget: float | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._timeout_for(budget)) as client:
            response = client.post(
                f"{self._credentials.base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        try:
            decoded = response.json()
        except ValueError:
            decoded = {}
        result: dict[str, Any] = decoded if isinstance(decoded, dict) else {}
        if response.status_code >= 400 or result.get("code") not in (0, 200):
            # **The provider's `msg` is reduced before it reaches the exception
            # text** (independent review 2026-09-08, P1-2, candidate (a)).
            # `msg` is entirely provider-controlled, and `setGuidNotifyUrl`'s
            # own argument is the URL whose last segment is v2's channel path
            # token -- `"invalid url: <url>"` is the most ordinary error text
            # this endpoint could produce, so **failure is the moment it is
            # most likely to echo the credential back**, not an exception to
            # the rule. Redacting only the success path left the method's own
            # stated guarantee -- "the token does not come out of this method"
            # -- true just when nothing went wrong.
            #
            # No `secrets` are passed: this transport is generic and does not
            # know which call it is serving. It does not need to -- the URL
            # pass alone reduces `…/wecom_proxy/<config>/<token>` to
            # `…/wecom_proxy/...`, and the token is the last segment.
            raise WeComProxyError(
                f"WeComProxy {path} failed "
                f"[{result.get('code', response.status_code)}]: {redact_urls(result.get('msg'))}"
            )
        return result

    def _call(
        self, path: str, payload: dict[str, Any], *, budget: float | None = None
    ) -> dict[str, Any]:
        """One call, optionally inside a wall-clock budget the caller owns.

        **The token round trip comes out of the same budget**, and it has to:
        `/member/login` fires whenever the cached token is cold, so a caller who
        budgeted 20 seconds for "send this message" would otherwise get a login
        of up to 20 plus a send of up to 20. Half is offered to the login, what
        it did not spend goes to the send, and an exhausted budget raises rather
        than sending -- the adapter turns that into a receipt, which is the
        port's contract (`channel.py`: a failure is a receipt, never an
        exception, because the dispatcher's only answer to an exception is to
        drop the process).
        """

        if budget is None:
            result = self._transport(path, payload, self._token_value())
        else:
            started = time.monotonic()
            token = self._token_value(budget=budget / 2)
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0:
                raise WeComProxyError(
                    f"WeComProxy {path} not attempted: the caller's budget was spent "
                    "obtaining a token"
                )
            result = self._transport(path, payload, token, budget=remaining)
        if not isinstance(result, dict):
            raise WeComProxyError(f"WeComProxy {path} returned a non-object response")
        return result

    # -- allowed operations ----------------------------------------------

    def create_client(self, *, device_name: str) -> str:
        """Create a new instance under the account and allowlist its guid."""
        self._require_confirmation("create_client", {"device_name": device_name})
        result = self._call(
            "/client/createClient",
            {"deviceName": device_name, "deviceType": 1, "customerParam": device_name},
        )
        guid = str((result.get("data") or {}).get("guid") or "").strip()
        if not guid:
            raise WeComProxyError("WeComProxy createClient returned no guid")
        entry = self._allowlist.record(guid, device_name=device_name)
        self._record("create_client", "created", guid=entry.guid, device_name=device_name)
        return entry.guid

    def get_login_qrcode(self, *, guid: str, is_reverify: bool = False) -> dict[str, Any]:
        allowed = self._guarded_guid("get_login_qrcode", guid)
        result = self._call("/login/getLoginQrcode", {"guid": allowed, "isReVerify": is_reverify})
        self._record("get_login_qrcode", "ok", guid=allowed)
        return dict(result.get("data") or {})

    def check_login_qrcode(self, *, guid: str) -> dict[str, Any]:
        allowed = self._guarded_guid("check_login_qrcode", guid)
        result = self._call("/login/checkLoginQrCode", {"guid": allowed})
        self._record("check_login_qrcode", "ok", guid=allowed)
        return dict(result.get("data") or {})

    def verify_login_qrcode(self, *, guid: str, code: str) -> dict[str, Any]:
        allowed = self._guarded_guid("verify_login_qrcode", guid)
        result = self._call("/login/verifyLoginQrcode", {"guid": allowed, "code": code})
        self._record("verify_login_qrcode", "ok", guid=allowed)
        return dict(result.get("data") or {})

    def set_notify_url(self, *, guid: str, notify_url: str) -> dict[str, Any]:
        allowed = self._guarded_guid("set_notify_url", guid)
        display_url = url_without_token(notify_url)
        self._require_confirmation("set_notify_url", {"guid": allowed, "notify_url": display_url})
        result = self._call("/client/setGuidNotifyUrl", {"guid": allowed, "url": notify_url})
        self._record("set_notify_url", "ok", guid=allowed, notify_url=display_url)
        # **Redacted before it leaves this method, not by the caller.** The
        # response echoes the registered notify URL, whose last segment is the
        # channel path token; returning it raw made every caller responsible for
        # noticing that, and one did not (2026-09-02). The token we just sent is
        # passed in as a known secret so it is removed even if the provider
        # returns it outside a URL.
        #
        # **A degenerate URL yields no secret rather than a wrong one**
        # (independent review 2026-09-08, P3-2). `rsplit("/")` on
        # `https://host` returns `"host"`, and passing that as a known secret
        # would replace the word `host` everywhere in the response with
        # `[REDACTED]` -- not a leak, but silent over-redaction that reads
        # exactly like a leak being caught. The token is only taken when the
        # URL really has a path of at least two segments, which is the shape
        # v2's own callback route has (`/wecom_proxy/{config_id}/{path_token}`);
        # anything else falls back to the URL pass alone, which is still on.
        token = _trailing_path_segment(notify_url)
        secrets = (token,) if token else ()
        return dict(redact_urls(result.get("data") or {}, secrets=secrets))

    def send_text(
        self,
        *,
        guid: str,
        recipient_id: str,
        content: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one text message; `timeout` is the caller's remaining seconds.

        **`timeout` is not decoration** (T96 independent review, P1-2). The
        outbox lease is 30 seconds and the dispatcher's turn budget is about the
        same; a send that runs past either is not late, it is *duplicated* --
        another dispatcher claims the row and the customer receives the message
        twice. The adapter passes what is left of its deadline, and it reaches
        httpx's four phases rather than sitting in a constructor default.

        **The return value is `data`, not the envelope**, and that is the shape
        the adapter reads its `msgId` out of.
        """

        allowed = self._guarded_guid("send_text", guid)
        result = self._call(
            "/msg/sendText",
            {"guid": allowed, "toId": recipient_id, "content": content},
            budget=timeout,
        )
        self._record("send_text", "ok", guid=allowed)
        return dict(result.get("data") or {})

    def upload_file_by_url(
        self,
        *,
        guid: str,
        filename: str,
        file_url: str,
        file_type: int,
        timeout: float | None = None,
    ) -> str:
        """Hand the provider a URL to fetch, and return the `requestId` it answers with.

        `/cloud/cdnUploadByUrlAsync` (`docs/wecom-proxy-cdn-upload-api.md`). The
        four fields are all of the request body the vendor document declares
        required: `guid`, `filename`, `fileUrl`, `fileType`.

        **The bytes never pass through this process.** The provider fetches
        `file_url` itself, which is why a material catalogue stores an address
        rather than content (`materials.source_url`, migration `065`).

        ## The return value is a `requestId`, and that is *all* the call returns

        **This is the shape that silently misleads a caller**, so the method
        refuses rather than returning a dict: the synchronous response carries
        no `fileId`, no key and no size -- the real result arrives later on the
        callback stream as `cmd=20000`, correlated by this same `requestId`
        (the vendor document: 「回调通知中的`requestId`会与同步响应中的
        `requestId`等同」).

        A caller handed `data` as a mapping would read `data.get("fileId")`,
        get `None`, and store nothing -- **with a 200 and no error anywhere**.
        Returning the one field that exists makes that mistake unwriteable.

        ## An empty or missing `requestId` is refused, not returned

        Without this, the upload is unclaimable: nothing links the callback back
        to the catalogue row, and the row would sit forever looking like an
        upload in flight. `WeComProxyError` here means "this upload did not
        start", which is recoverable by retrying; a stored empty string is not.
        """

        allowed = self._guarded_guid("upload_file_by_url", guid)
        result = self._call(
            "/cloud/cdnUploadByUrlAsync",
            {
                "guid": allowed,
                "filename": filename,
                "fileUrl": file_url,
                "fileType": file_type,
            },
            budget=timeout,
        )
        data = result.get("data") or {}
        request_id = str(data.get("requestId") or "").strip() if isinstance(data, dict) else ""
        if not request_id:
            # **Not `_record(..., "ok")` first.** An upload with no requestId is
            # not a completed operation with a missing field; there is nothing
            # to claim it with later, so it never started as far as this
            # repository is concerned.
            self._record("upload_file_by_url", "no_request_id", guid=allowed)
            raise WeComProxyError(
                "WeComProxy /cloud/cdnUploadByUrlAsync returned no requestId; "
                "the upload cannot be claimed from the callback without one"
            )
        self._record("upload_file_by_url", "ok", guid=allowed)
        return request_id

    def send_file(
        self,
        *,
        guid: str,
        recipient_id: str,
        file_id: str,
        file_size: int,
        file_md5: str,
        filename: str,
        file_aes_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one file the provider already holds; `timeout` is the caller's seconds.

        `/msg/sendFile` (`docs/wecom-proxy-message-api.md` §7). The provider
        fetched the bytes during upload -- this call only names them.

        ## `fileAesKey` is **conditionally** required, and that is the trap

        The vendor's `required` list has six entries and **`fileAesKey` is not
        among them**, while its own description says 「大文件时，可为空；否则必填」.
        So the schema will accept a small-file send with no key, and what comes
        back is a success -- the failure is at the *recipient*, whose client
        cannot decrypt a file it was given no key for.

        **That is this repository's named shape**: it does not error, it returns
        something that looks fine. So the condition is checked here, once, and
        the check refuses rather than sending.

        ## What makes a file "big" is `fileId`, not `file_size`

        §7 and the CDN module agree on the convention: a big file's `fileId` is
        the `fileConvertId`, which **starts with `*`** -- and that is exactly
        when the key may be absent (`docs/wecom-proxy-message-api.md:1306`
        records the two sides of the same rule). Deciding it on `file_size`
        instead would be a second, competing definition of "big", and the two
        would disagree the first time a threshold moved.

        **The decision is `is_big_file`, a named predicate**, because the send
        path is not the only place that will need it.
        """

        allowed = self._guarded_guid("send_file", guid)
        key = (file_aes_key or "").strip()
        if not key and not is_big_file(file_id):
            self._record("send_file", "missing_aes_key", guid=allowed)
            raise WeComProxyError(
                "WeComProxy /msg/sendFile refused before sending: fileAesKey is "
                "required unless the file is a big one (its fileId starts with "
                "'*'); sending without it delivers a file the recipient cannot "
                "decrypt, and the provider answers success"
            )

        payload: dict[str, Any] = {
            "guid": allowed,
            "fileId": file_id,
            "fileSize": file_size,
            "fileMd5": file_md5,
            "filename": filename,
            "toId": recipient_id,
        }
        if key:
            # **Only when there is one.** Sending `"fileAesKey": ""` on a big
            # file is not the same as omitting it: the vendor documents the
            # field as *may be empty* there, and an empty string is a value the
            # next reader of a captured request would take for a real one.
            payload["fileAesKey"] = key

        result = self._call("/msg/sendFile", payload, budget=timeout)
        self._record("send_file", "ok", guid=allowed)
        return dict(result.get("data") or {})

    # -- refused operations ----------------------------------------------

    def list_clients(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 禁用操作的桩：签名要吞下任何调用形状，函数体只负责 raise
        self._refuse("list_clients")

    def logout(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 同上，禁用操作的桩
        self._refuse("logout")

    def stop_client(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 同上，禁用操作的桩
        self._refuse("stop_client")

    def delete_notify_url(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002 - 同上，禁用操作的桩
        self._refuse("delete_notify_url")

    def _refuse(self, operation: str) -> None:
        self._record(operation, "refused_forbidden_operation")
        raise ForbiddenOperationError(
            f"{operation} is not implemented in v2: it can reach instances v2 did not create"
        )
