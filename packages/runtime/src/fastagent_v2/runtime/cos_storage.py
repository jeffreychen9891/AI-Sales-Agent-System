"""Minimal Tencent COS boundary for Stage materials, images, and backups.

The implementation intentionally uses only the Python standard library. Long-lived
credentials stay server-side, every upload requests SSE-COS, and callers must use an
immutable, validated object key. Delete and bucket-configuration operations do not
exist in this client because the Stage CAM identity is not allowed to perform them.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

_ENV_KEYS = ("TENCENT_COS_SECRET_ID", "TENCENT_COS_SECRET_KEY")
_CONFIG_KEYS = ("TENCENT_COS_BUCKET", "TENCENT_COS_REGION", "TENCENT_COS_ENDPOINT")
_SECRET_ID = re.compile(r"AKID[A-Za-z0-9]{16,}")
_SECRET_KEY = re.compile(r"[A-Za-z0-9_-]{16,}")
_REGION = re.compile(r"ap-[a-z0-9-]+")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,62}-[0-9]{5,}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KIND = {"document", "image"}


class CosError(RuntimeError):
    """A COS request failed without exposing request credentials."""

    def __init__(self, *, status: int, code: str, request_id: str = "") -> None:
        suffix = f" request_id={request_id}" if request_id else ""
        super().__init__(f"COS request failed status={status} code={code}{suffix}")
        self.status = status
        self.code = code
        self.request_id = request_id


class _Response(Protocol):
    status: int

    def getheaders(self) -> list[tuple[str, str]]: ...

    def read(self, amount: int | None = None) -> bytes: ...


class _Connection(Protocol):
    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None: ...

    def putheader(self, header: str, *values: str) -> None: ...

    def endheaders(self) -> None: ...

    def send(self, data: bytes) -> None: ...

    def getresponse(self) -> _Response: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, float], _Connection]


def _https_connection(host: str, timeout: float) -> _Connection:
    return http.client.HTTPSConnection(host, timeout=timeout)


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


@dataclass(frozen=True, slots=True)
class CosConfig:
    bucket: str
    region: str
    endpoint: str
    secret_id: str
    secret_key: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_files(cls, credential_file: Path, config_file: Path) -> CosConfig:
        credentials = _load_env(credential_file)
        config = _load_env(config_file)
        if tuple(credentials) != _ENV_KEYS:
            raise ValueError("COS credential file must contain exactly the two approved keys")
        if tuple(config) != _CONFIG_KEYS:
            raise ValueError("COS config file must contain exactly the three approved keys")
        secret_id = credentials[_ENV_KEYS[0]]
        secret_key = credentials[_ENV_KEYS[1]]
        bucket = config.get("TENCENT_COS_BUCKET", "")
        region = config.get("TENCENT_COS_REGION", "")
        endpoint = config.get("TENCENT_COS_ENDPOINT", "")
        if not _SECRET_ID.fullmatch(secret_id) or not _SECRET_KEY.fullmatch(secret_key):
            raise ValueError("COS credential format is invalid")
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("COS bucket format is invalid")
        if not _REGION.fullmatch(region):
            raise ValueError("COS region format is invalid")
        expected_endpoint = f"cos.{region}.myqcloud.com"
        if endpoint != expected_endpoint:
            raise ValueError("COS endpoint must be the default endpoint for the configured region")
        return cls(
            bucket=bucket,
            region=region,
            endpoint=endpoint,
            secret_id=secret_id,
            secret_key=secret_key,
        )

    @property
    def host(self) -> str:
        return f"{self.bucket}.{self.endpoint}"


def material_object_key(
    *, tenant_id: uuid.UUID, artifact_id: uuid.UUID, version: int, kind: str, sha256: str
) -> str:
    """Build a tenant-owned immutable key without merchant or customer text."""
    if version < 1:
        raise ValueError("material version must be positive")
    if kind not in _KIND:
        raise ValueError("material kind is not allowlisted")
    normalized_hash = sha256.lower()
    if not _SHA256.fullmatch(normalized_hash):
        raise ValueError("material SHA-256 is invalid")
    return (
        f"materials/tenants/{tenant_id}/{kind}/{artifact_id}/"
        f"v{version}/{normalized_hash}"
    )


def backup_object_key(filename: str) -> str:
    if not re.fullmatch(r"fastagent-[0-9]{8}T[0-9]{6}Z\.dump(?:\.sha256)?", filename):
        raise ValueError("backup filename is invalid")
    return f"backups/postgres/{filename}"


class CosClient:
    def __init__(
        self,
        config: CosConfig,
        *,
        connection_factory: ConnectionFactory = _https_connection,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._connection_factory = connection_factory
        self._clock = clock

    @staticmethod
    def _quote(value: str) -> str:
        return urllib.parse.quote(value, safe="-_.~")

    def _authorization(self, method: str, path: str, signed_headers: Mapping[str, str]) -> str:
        now = int(self._clock())
        key_time = f"{now - 60};{now + 600}"
        normalized_headers = {key.lower(): value for key, value in signed_headers.items()}
        header_names = sorted(normalized_headers)
        header_list = ";".join(self._quote(name) for name in header_names)
        canonical_headers = "&".join(
            f"{self._quote(name)}={self._quote(normalized_headers[name])}"
            for name in header_names
        )
        http_string = "\n".join([method.lower(), path, "", canonical_headers, ""])
        sign_key = hmac.new(
            self._config.secret_key.encode(), key_time.encode(), digestmod="sha1"
        ).hexdigest()
        string_to_sign = "\n".join(
            [
                "sha1",
                key_time,
                hashlib.sha1(http_string.encode(), usedforsecurity=False).hexdigest(),
                "",
            ]
        )
        signature = hmac.new(
            sign_key.encode(), string_to_sign.encode(), digestmod="sha1"
        ).hexdigest()
        return "&".join(
            [
                "q-sign-algorithm=sha1",
                f"q-ak={self._config.secret_id}",
                f"q-sign-time={key_time}",
                f"q-key-time={key_time}",
                f"q-header-list={header_list}",
                "q-url-param-list=",
                f"q-signature={signature}",
            ]
        )

    @staticmethod
    def _error(response: _Response) -> CosError:
        body = response.read(64 * 1024)
        code = f"HTTP_{response.status}"
        request_id = ""
        text = body.decode("utf-8", errors="replace")
        code_match = re.search(r"<Code>([A-Za-z0-9_.-]{1,128})</Code>", text)
        request_match = re.search(r"<RequestId>([A-Za-z0-9_-]{1,256})</RequestId>", text)
        if code_match:
            code = code_match.group(1)
        if request_match:
            request_id = request_match.group(1)
        return CosError(status=response.status, code=code, request_id=request_id)

    def _start(
        self,
        method: str,
        key: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[_Connection, _Response]:
        path = "/" + urllib.parse.quote(key, safe="/-_.~") if key else "/"
        request_headers = {"host": self._config.host, **(headers or {})}
        signed_headers = {
            name: value
            for name, value in request_headers.items()
            if name == "host" or name.startswith("x-cos-")
        }
        request_headers["authorization"] = self._authorization(method, path, signed_headers)
        connection = self._connection_factory(self._config.host, self._config.timeout_seconds)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in request_headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        return connection, connection.getresponse()

    def probe_bucket(self) -> None:
        connection, response = self._start("HEAD", "")
        try:
            if response.status != 200:
                raise self._error(response)
            response.read()
        finally:
            connection.close()

    def put_file(self, source: Path, key: str, *, sha256: str, content_type: str) -> None:
        normalized_hash = sha256.lower()
        if not _SHA256.fullmatch(normalized_hash):
            raise ValueError("object SHA-256 is invalid")
        size = source.stat().st_size
        if size > 5 * 1024**3:
            raise ValueError("single-part COS upload is limited to 5 GiB")
        path = "/" + urllib.parse.quote(key, safe="/-_.~")
        headers = {
            "host": self._config.host,
            "content-length": str(size),
            "content-type": content_type,
            "x-cos-meta-sha256": normalized_hash,
            "x-cos-server-side-encryption": "AES256",
        }
        signed_headers = {
            name: value
            for name, value in headers.items()
            if name == "host" or name.startswith("x-cos-")
        }
        headers["authorization"] = self._authorization("PUT", path, signed_headers)
        connection = self._connection_factory(self._config.host, self._config.timeout_seconds)
        try:
            connection.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with source.open("rb") as stream:
                self._send_stream(connection, stream)
            response = connection.getresponse()
            if response.status not in {200, 201}:
                raise self._error(response)
            response.read()
        finally:
            connection.close()

    @staticmethod
    def _send_stream(connection: _Connection, stream: BinaryIO) -> None:
        while chunk := stream.read(1024 * 1024):
            connection.send(chunk)

    def head_object(self, key: str) -> dict[str, str]:
        connection, response = self._start("HEAD", key)
        try:
            if response.status != 200:
                raise self._error(response)
            response.read()
            return {name.lower(): value for name, value in response.getheaders()}
        finally:
            connection.close()

    def download(self, key: str, destination: Path) -> None:
        connection, response = self._start("GET", key)
        try:
            if response.status != 200:
                raise self._error(response)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, partial_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
            )
            partial = Path(partial_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                os.chmod(partial, 0o600)
                partial.replace(destination)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        finally:
            connection.close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
