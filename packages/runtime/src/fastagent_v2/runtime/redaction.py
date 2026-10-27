"""The repository's single redaction vocabulary.

Three renderings of "this value is withheld" exist here. They are not
interchangeable and each is named so a reader can tell which one they are
looking at:

* `REDACTED` -- this module's marker, and the one every new caller should use.
  `WeComProxyCredentials.__repr__` and `Settings.__repr__` both render it.
* `REDACTION_FAILED` -- the redactor could not establish that a value was safe,
  so it dropped it. Never a fallback to `repr(value)`.
* `wecom_proxy/client.py::url_without_token` -- *not* a redaction marker: it
  rewrites a URL down to origin plus first segment. Different job, kept
  separate on purpose (independent review 2026-09-02, P3-3).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED_DROPPED]"

#: Types rendered by `str()` because that *is* their canonical form. Amounts
#: and timestamps are handled separately, each with the serialisation its
#: readers expect -- see the branches in `redact` (T23, and P3-1 for why
#: `str()` alone was not enough).
_SAFE_SCALARS = (UUID,)
# Matched as *substrings* of the normalised key, not by equality. Equality let
# `access_signing_key` through: it normalises to `accesssigningkey`, which is
# not any of the words below, so `redact()` returned the signing key verbatim
# (found 2026-09-02 while fixing T13). Anything carrying one of these markers
# anywhere in its name is a credential.
_SENSITIVE_MARKERS = (
    "password",
    "passwd",
    "token",
    "apikey",
    "secret",
    "cookie",
    "authorization",
    "privatekey",
    "signature",
    "signingkey",
    "credential",
    # **`cloudUrl` is an authorisation wearing a URL's name** (ADR-0126 §五).
    # The vendor's media-fetch flow answers `cmd=20000` with a temporary,
    # **directly GET-able** address for a customer's media -- possession of the
    # string *is* the access. It has to be listed here because the generic rule
    # below would otherwise claim it: `cloudurl` carries the `url` marker, so
    # `_redact_entry` would route it to `redact_url`, which **keeps** scheme,
    # host and path so an operator can still see which database a process was
    # talking to. That trade is right for a DSN and wrong for this: measured,
    # not assumed -- `redact({"msgData": {"cloudUrl": "http://…/cloud/tmp/
    # abc123?sign=…"}})` returned the address **byte-for-byte unchanged**.
    #
    # **Who actually reaches this value, corrected** (independent review
    # 2026-09-11, P3-4): the first version of this comment said "anyone reading
    # that log line", and **there is no such log line today** -- ingress has one
    # `LOGGER.warning`, and it prints `config_id` and `reason_code`, not the
    # callback body. What this really guards is `scripts/ingest_evidence.py`,
    # which re-runs `redact()` over captured payloads before they enter the tree
    # -- **the same path that carried a real media key in on 2026-09-10** -- plus
    # any log that starts printing callback bodies later. The measurement was
    # right; the sentence about who it protects was not, and "evidence taken
    # from somewhere other than where the value actually travels" is its own
    # failure family.
    #
    # `_redact_entry` asks `is_sensitive_key` **before** `is_url_key`, so naming
    # it here is what makes the value dropped rather than url-redacted.
    #
    # Matched as a substring like every marker above, so `cloud_url` and
    # `mediaCloudUrl` are covered without being enumerated.
    "cloudurl",
)
# Key names carrying a URL. Their *value* is redacted with `redact_url` rather
# than dropped: `postgresql://app:[REDACTED]@localhost:55432/fastagent` is
# log-safe and still tells an operator which database the process was talking to.
# Matched as substrings, symmetrically with `_SENSITIVE_MARKERS`: `endswith` let
# `database_urls` (plural) and `url_primary` past (independent review, P3-4).
_URL_MARKERS = ("url", "uri", "dsn")

#: Name endings that make a number a measurement rather than a credential.
#: Checked only for numeric values, and only to *rescue* a name that
#: `_SENSITIVE_MARKERS` already condemned -- so the failure mode of a missing
#: suffix here is over-redaction, which is the side T13's P2-1 already chose.
_MEASUREMENT_SUFFIXES = (
    "tokens",
    "seconds",
    "millis",
    "count",
    "bytes",
    "latency",
    "duration",
    "attempts",
    "size",
)

#: A credential-bearing URL anywhere inside a longer string. `carries_credentials`
#: only sees whole strings -- `urlsplit` puts an embedded DSN in `path` with an
#: empty `netloc` -- and the 2026-09-01 leak was a DSN *in a log line*, which is
#: exactly the shape that missed (registered as T21).
#:
#: Same family as the `url-with-password` rule in `scripts/check_secrets.py`, and
#: deliberately a little wider in two places: the username may be **empty**
#: (`redis://:password@host` is how every Redis URL in this repository is
#: written, and the first draft of this pattern missed all of them), and the
#: password has no minimum length, because a short password is still a password.
#: The scanner can afford to be narrow -- a missed match there is a review
#: finding; a missed match here is a credential in a log.
#:
#: The password may contain `:` (an unescaped colon in the password is exactly
#: the shape a careless operator produces). `urlsplit` accepts it on a whole
#: string, so requiring a colon-free password here would have left a fourth way
#: for the two definitions to disagree -- P3-2 of the independent review.
#:
#: The host part runs to whitespace or a quote/bracket rather than stopping at
#: the first `/`: a match that ended at the path handed `redact_url` only the
#: authority, so credentials in the query string survived while the line
#: *looked* redacted -- worse than no redaction, because a reader trusts it
#: (independent review, P2-2).
_EMBEDDED_CREDENTIAL_URL = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]*:(?P<password>[^\s/@]+)@[^\s\"'<>]+",
    # Schemes are case-insensitive (RFC 3986). Without this, `POSTGRESQL://`
    # went through untouched while the same value as a whole string was
    # normalised by `urlsplit` and caught -- one credential, two answers,
    # depending on whether it had words around it (independent review, P2-1).
    re.IGNORECASE,
)


def _normalise_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def is_sensitive_key(value: object) -> bool:
    """Fail closed on credential-shaped names.

    Public because it is the repository's *one* answer to "is this field name a
    credential". `Settings.__repr__` calls it rather than keeping a second list;
    the whole reason T13 existed is that a security boundary written as a manual
    list omits the field nobody thought to list (independent review, P1-1).

    A trailing `key` counts, which is how `access_signing_key`, `api_key` and any
    future `*_key` field are caught without enumerating them.

    **The cost is decided, not overlooked** (independent review, P2-1): this also
    redacts business identifiers such as `idempotency_key`, `routing_key` and
    `partition_key`, and `token` as a substring redacts the integer knob
    `access_token_ttl_seconds` inside `redact()`. That is the accepted side of
    the trade -- losing a value from a log is cheaper than printing a credential.
    **Do not "fix" it back to equality matching**; equality is what let
    `access_signing_key` through in the first place.
    """

    normalised = _normalise_key(value)
    return normalised.endswith("key") or any(
        marker in normalised for marker in _SENSITIVE_MARKERS
    )


def is_url_key(value: object) -> bool:
    """Whether a field name announces that its value is a URL.

    Public for the same reason as `is_sensitive_key`: `Settings.__repr__` shares
    it instead of testing `endswith("_url")` on its own.
    """

    normalised = _normalise_key(value)
    return any(marker in normalised for marker in _URL_MARKERS)


def carries_credentials(value: str) -> bool:
    """Whether a string is a URL whose authority carries a userinfo section.

    Keys are a heuristic; this is the shape. The 2026-09-01 leak was a DSN in a
    log line, and a DSN reaches `redact()` under plenty of names that no marker
    list will ever contain -- `conn`, a bare list element, `database_urls`
    (independent review, P3-4). Anything whose `netloc` carries `@` goes through
    `redact_url` regardless of what it was called.

    This sees whole strings only -- `urlsplit` leaves an embedded DSN in
    `path` with an empty `netloc`. That half is handled by
    `_EMBEDDED_CREDENTIAL_URL` in `_redact_text`, which is what closed T21;
    the two are used together and neither is sufficient alone.
    """

    try:
        return "@" in urlsplit(value).netloc
    except ValueError:
        return False


def _redact_embedded(match: re.Match[str]) -> str:
    """Redact one embedded URL, leaving already-redacted ones alone.

    `[REDACTED]` contains brackets, so feeding an already-redacted URL back to
    `urlsplit` raises and `redact_url` fails closed to
    `[REDACTION_FAILED_DROPPED]` -- destroying the role, host, port and database
    that the first pass deliberately kept. Redacting twice is not hypothetical:
    T20 redacts at the client boundary and a sink may redact again, and a
    `redacted_repr` string pasted into a message goes through here as well
    (independent review, P1-2).

    The check is on the password group, not on "does the text contain
    `[REDACTED]`": `postgresql://app:[REDACTED]hunter2@db/x` is not
    already-redacted, and it still fails closed rather than being waved through.
    """

    if match.group("password") == REDACTED:
        return match.group(0)
    return redact_url(match.group(0))


def _redact_text(value: str, *, treat_as_url: bool = False) -> str:
    """Redact a string whether the credential is the whole value or inside it.

    Both callers need both halves. The url-key branch of `_redact_entry` used to
    stop at `redact_url`, which only understands a whole URL -- so a sentence
    under a `*_url` key kept its password while the same sentence under any other
    key was caught by the shape fallback. Naming a field `*_url` made it *less*
    safe, exactly inverting this module's own rule that shape beats name
    (independent review, P1-1).
    """

    text = redact_url(value) if (treat_as_url or carries_credentials(value)) else value
    return _EMBEDDED_CREDENTIAL_URL.sub(_redact_embedded, text)


def _redact_pairs(encoded: str) -> str:
    return urlencode(
        [
            (key, REDACTED if is_sensitive_key(key) else item)
            for key, item in parse_qsl(encoded, keep_blank_values=True)
        ],
        safe="[]",
    )


def redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        # Credentials in the authority component. `urlunsplit` puts `netloc` back
        # verbatim, so before 2026-09-02 this function returned
        # `postgresql://app:not-a-real-password@host/db` unchanged while calling
        # itself log-safe -- exactly the shape of the 2026-09-01 leak. The username
        # is kept: it names the database role, which is what makes the line useful.
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, _, host = netloc.rpartition("@")
            username, separator, password = userinfo.partition(":")
            # `separator and password`: `postgresql://app:@host/db` has an empty
            # password, and reporting `[REDACTED]` there invents a credential
            # that does not exist (independent review, P3-1).
            if separator and password:
                netloc = f"{username}:{REDACTED}@{host}"
            else:
                netloc = f"{username}@{host}"
        path_segments = parts.path.split("/")
        for index, segment in enumerate(path_segments):
            previous_is_sensitive = is_sensitive_key(path_segments[index - 1]) if index else False
            token_like = len(segment) >= 32 and all(
                character.isalnum() or character in "-_" for character in segment
            )
            if previous_is_sensitive or token_like:
                path_segments[index] = REDACTED
        safe_path = "/".join(path_segments)
        # The fragment is the same hole as the authority: `urlunsplit` puts it
        # back verbatim, and OAuth implicit flows put access tokens there. It
        # carries `key=value` pairs like the query, so it gets the same treatment
        # (independent review, P2-2).
        # `safe="[]"` keeps the marker greppable: `urlencode` would otherwise
        # emit `password=%5BREDACTED%5D`, which every scanner looking for the
        # literal `[REDACTED]` misses (independent review, P3-2).
        safe_query = _redact_pairs(parts.query)
        safe_fragment = _redact_pairs(parts.fragment) if "=" in parts.fragment else parts.fragment
        return urlunsplit((parts.scheme, netloc, safe_path, safe_query, safe_fragment))
    except Exception:
        return REDACTION_FAILED


def redacted_repr(instance: Any) -> str:
    """Render a dataclass instance with no credential in it.

    Lives here, not on the dataclass, so that the field-name judgement has
    exactly one implementation and a test can apply it to field names that do
    not exist yet (independent review 2026-09-02, P1-1).

    The `isinstance(value, str)` guard is load-bearing: without it the integer
    knob `access_token_ttl_seconds` matches the `token` marker and stops being
    visible, for no security gain. Empty strings are left as they are -- an empty
    `database_url` means this process role is not issued one, which is worth
    seeing.
    """

    rendered = []
    for field in fields(instance):
        value = getattr(instance, field.name)
        if isinstance(value, str) and value:
            if is_sensitive_key(field.name):
                value = REDACTED
            elif is_url_key(field.name):
                value = redact_url(value)
        rendered.append(f"{field.name}={value!r}")
    return f"{type(instance).__name__}({', '.join(rendered)})"


def _redact_entry(key: object, item: Any) -> Any:
    if is_sensitive_key(key) and not _is_measurement(key, item):
        return REDACTED
    if is_url_key(key) and isinstance(item, str):
        return _redact_text(item, treat_as_url=True)
    return redact(item)


def _is_measurement(key: object, value: Any) -> bool:
    """Whether a credential-shaped *name* is in fact a measurement.

    T22's problem: `token` as a substring blanked `input_tokens` and
    `output_tokens` out of every redacted cost record -- the numbers the RMB
    accounting is computed from -- and turned `access_token_ttl_seconds` into
    `[REDACTED]` while the same object's `repr` printed `900`.

    The first fix said "a credential is never an integer" and let **any** number
    under **any** sensitive name through. That is false and it leaked:
    `redact({"password": 123456})` returned `123456`, and PINs, OTPs and
    all-digit webhook keys are exactly that shape (independent review, P2-3).
    T13's P2-1 accepted the cost of redacting *too much*; this was redacting too
    little, which is not the same decision and was never made.

    So the exception is now as narrow as the symptom: the value must be a number
    **and** the name must read as a measurement. Anything else keeps the
    conservative answer.

    `bool` is excluded on purpose: `True` is not a measurement, and a flag named
    like a credential is cheap to lose.
    """

    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return False
    return _normalise_key(key).endswith(_MEASUREMENT_SUFFIXES)


def redact(value: Any) -> Any:
    """Return a recursively redacted, log-safe value.

    Unknown objects and redactor failures fail closed; they never fall back to repr(value).
    """

    try:
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, Mapping):
            return {str(key): _redact_entry(key, item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(redact(item) for item in value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [redact(item) for item in value]
        if isinstance(value, BaseException):
            return {"type": type(value).__name__, "message": REDACTION_FAILED}
        if isinstance(value, str):
            # Shape, not name: a DSN reaches here under names no marker list will
            # ever hold, whether it is the whole value or inside a sentence.
            return _redact_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, (datetime, date, time)):
            # `str(datetime(...))` gives "2026-09-03 12:30:00+00:00" -- a space
            # where ISO-8601 wants a `T`, which downstream parsers reject. T23
            # exists to stop amounts and timestamps disappearing; rendering them
            # in a shape nothing can read loses them a second way (independent
            # review, P3-1).
            return value.isoformat()
        if isinstance(value, Decimal):
            # `str(Decimal("1E+2"))` keeps scientific notation. Fixed-point is
            # what a ledger reads.
            return format(value, "f")
        if isinstance(value, _SAFE_SCALARS):
            # T23. `Decimal` used to fall through to the unknown-object branch and
            # come back `[REDACTION_FAILED_DROPPED]`, which meant every amount
            # vanished from a redacted cost record -- in a system whose whole
            # point is to account for money. Failing closed on a type nobody
            # recognises is right; treating a recognised, harmless one as
            # unrecognised is not. Rendered as text so the caller gets the value
            # rather than a float that lost precision on the way out.
            return str(value)
        return {"type": type(value).__name__, "value": REDACTION_FAILED}
    except Exception:
        return REDACTION_FAILED
