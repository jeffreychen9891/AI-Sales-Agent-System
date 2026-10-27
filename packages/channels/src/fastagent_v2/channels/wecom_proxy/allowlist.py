from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import GuidNotAllowedError, WeComProxyConfigError

_GUID_MAX_LENGTH = 64


def _restrict_to_owner(fileno: int, path: str, *, group_readable: bool) -> None:
    """Owner read/write, plus group *read* when the deployment needs it.

    **`group_readable` exists because the ceremony was silently undoing the
    deployment** (independent review 2026-09-09, P1-3). Every write went through
    an unconditional `fchmod` to `0600`, so an operator who set the file to
    `0640` for the worker got it reset to `0600` by the next
    `wecom_proxy_bind.py` run -- after which the worker's allowlist reads
    fail, the list is default-deny, **every send is refused silently**, and
    `stage_preflight` would not say so until the next deployment.

    It stays `0600` by default: the tool is normally run by a human on a host
    where nothing else needs to read the file, and **widening a permission has
    to be asked for, not inherited**.
    """

    mode = stat.S_IRUSR | stat.S_IWUSR
    if group_readable:
        mode |= stat.S_IRGRP
    if hasattr(os, "fchmod"):
        os.fchmod(fileno, mode)
    else:  # pragma: no cover - developer machines only; Stage runs on Linux
        os.chmod(path, mode)


def normalize_guid(value: str) -> str:
    """Canonicalise a provider guid for comparison.

    Normalisation only ever affects which entries an *already allowlisted* guid
    matches. The list is default-deny, so no normalisation can turn a guid v2
    never created into an allowed one.
    """
    guid = (value or "").strip().upper()
    if not guid or len(guid) > _GUID_MAX_LENGTH:
        raise GuidNotAllowedError("guid is empty or implausibly long")
    if any(character not in "0123456789ABCDEF-" for character in guid):
        raise GuidNotAllowedError("guid contains characters no provider guid uses")
    return guid


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    guid: str
    device_name: str
    created_at: str


class GuidAllowlist:
    """Default-deny record of the guids v2 created for itself.

    Every guid-bearing provider call must pass ``require``. The list is written
    only by ``record``, which is called only from ``create_client`` with the
    guid the provider just returned. Nothing else can widen it, so any guid v2
    did not create is unreachable by construction rather than by enumeration.

    ``denylist_path`` is an optional operator-managed file (one guid per line,
    ``#`` comments allowed) that is checked *before* the allowlist and always
    wins. It exists so the operator can pin known-foreign guids without those
    identifiers entering this repository. It was introduced under ADR-0015 to
    pin v1's guids; ADR-0027 established that v2's connection cannot reach them
    anyway, so it is now belt-and-braces rather than the load-bearing guard.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        denylist_path: str | os.PathLike[str] | None = None,
        group_readable: bool = False,
    ) -> None:
        self._path = Path(path)
        self._denylist_path = Path(denylist_path) if denylist_path else None
        #: Whether a rewrite keeps the file readable by its group.
        #:
        #: **Off by default, and asked for by the one caller that needs it.**
        #: On Stage the worker container reads this file through a shared group,
        #: so a rewrite that dropped the group bit would leave the dispatcher
        #: unable to read its own allowlist -- default-deny, every send refused,
        #: nothing in the logs saying why (P1-3).
        self._group_readable = group_readable

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> tuple[AllowlistEntry, ...]:
        if not self._path.exists():
            return ()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError) as exc:
            raise WeComProxyConfigError(f"guid allowlist is unreadable: {self._path}") from exc
        if not isinstance(raw, list):
            raise WeComProxyConfigError(f"guid allowlist is not a list: {self._path}")
        entries: list[AllowlistEntry] = []
        for item in raw:
            if not isinstance(item, dict) or "guid" not in item:
                raise WeComProxyConfigError(f"guid allowlist has a malformed entry: {self._path}")
            entries.append(
                AllowlistEntry(
                    guid=normalize_guid(str(item["guid"])),
                    device_name=str(item.get("device_name", "")),
                    created_at=str(item.get("created_at", "")),
                )
            )
        return tuple(entries)

    def _denied(self) -> frozenset[str]:
        if self._denylist_path is None or not self._denylist_path.exists():
            return frozenset()
        try:
            lines = self._denylist_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise WeComProxyConfigError(
                f"guid denylist is unreadable: {self._denylist_path}"
            ) from exc
        denied: set[str] = set()
        for line in lines:
            candidate = line.split("#", 1)[0].strip()
            if candidate:
                denied.add(candidate.upper())
        return frozenset(denied)

    def contains(self, guid: str) -> bool:
        candidate = normalize_guid(guid)
        if candidate in self._denied():
            return False
        return any(entry.guid == candidate for entry in self.entries())

    def require(self, guid: str) -> str:
        """Return the normalised guid, or raise if v2 did not create it."""
        candidate = normalize_guid(guid)
        if candidate in self._denied():
            raise GuidNotAllowedError("guid is on the operator denylist")
        if not any(entry.guid == candidate for entry in self.entries()):
            raise GuidNotAllowedError("guid is not on v2's allowlist")
        return candidate

    def record(self, guid: str, *, device_name: str) -> AllowlistEntry:
        candidate = normalize_guid(guid)
        if candidate in self._denied():
            raise GuidNotAllowedError("refusing to allowlist a denylisted guid")
        existing = self.entries()
        if any(entry.guid == candidate for entry in existing):
            return next(entry for entry in existing if entry.guid == candidate)
        entry = AllowlistEntry(
            guid=candidate,
            device_name=device_name,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        payload = [
            {"guid": item.guid, "device_name": item.device_name, "created_at": item.created_at}
            for item in (*existing, entry)
        ]
        self._write(payload)
        return entry

    def _write(self, payload: list[dict[str, str]]) -> None:
        parent = self._path.parent
        if not parent.exists():
            raise WeComProxyConfigError(f"allowlist directory does not exist: {parent}")
        handle, temporary = tempfile.mkstemp(prefix=".wecom-proxy-allowlist.", dir=parent)
        try:
            # The fd is adopted by the stream so that it is closed on every
            # path; a leaked handle leaves an undeletable temp file on Windows.
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                _restrict_to_owner(
                    stream.fileno(), temporary, group_readable=self._group_readable
                )
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
