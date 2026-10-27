from __future__ import annotations

import os
from dataclasses import dataclass

from fastagent_v2.runtime.redaction import redacted_repr


class SettingsError(RuntimeError):
    """Raised when a security-relevant setting is absent or unsafe."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(f"required setting is missing: {name}")
    return value


#: 两支**不经过任何供应商**的推理实现（`worker.inference.inference_for_provider`），
#: 与它们各自被拒时该说的那句话。两支都只许在 `local` / `ci` 跑。
#:
#: **两条消息分开写、不合并成一句通用的**，因为读到它们的人要修的东西不同：
#: 「mock 进了 stage」通常是部署漏配了 `FASTAGENT_MODEL_PROVIDER`；
#: 「回放脚本进了 stage」通常是**演示环境的那一份 env 被带进了部署** ——
#: 后者还会连带把 `FASTAGENT_SCRIPTED_SCENARIO` 带进去。
#: 一句「不是真模型」会让这两种都去查同一个地方
#: （`AGENTS.md`「一句正确的报错，可以指向错误的原因」）。
#:
#: ⚠️ `mock` 那一句**逐字节不许动**：`tests/security/test_security_birth.py`
#: 按 `match="mock model"` 钉着它，而那条用例钉的是这道闸本身。
OFFLINE_MODEL_PROVIDERS = {
    "mock": "mock model is forbidden outside local and ci",
    "scripted": (
        "scripted replay model is forbidden outside local and ci "
        "(FASTAGENT_MODEL_PROVIDER=scripted replays recorded documents; "
        "it never calls a provider, so serving customers with it would answer "
        "them from a demo script)"
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class Settings:
    """Process configuration.

    `repr=False` is a security decision, not a style one. Six of these fields are
    connection URLs carrying a database or Redis password and one is the access
    signing key, and the generated `repr` printed all seven in the clear. That
    repr reaches places nobody chose to send it to: a pytest fixture that raises
    prints the whole `Settings` object in the traceback, and every one of those
    tracebacks is a candidate for an archived CI transcript (registered as T13
    after T10 moved `conftest` onto the production publish path, which made a
    regression there fail the whole `seeded_database` family at fixture time --
    seven tracebacks, seven copies of the password). See `__repr__` below.
    """

    environment: str
    database_url: str
    ingress_database_url: str
    worker_database_url: str
    scheduler_database_url: str
    queue_redis_url: str
    cache_redis_url: str
    access_signing_key: str
    model_provider: str
    channel_send_url: str
    allowed_ws_origins: frozenset[str]
    #: Path to the superuser allowlist (ADR-0048 §二.2 / K5). **`None` and a
    #: missing file both mean nobody is a superuser** -- the failure mode of "it
    #: was not deployed" has to be that nobody has platform powers.
    superuser_allowlist_file: str | None = None
    #: Where the deployment places channel credentials (`C14`, ADR-0061).
    #: **Read-only, and a subdirectory rather than the whole secret store** --
    #: mounting the latter into the api would hand it every database password.
    #: `None` means no credential can be registered, which is the correct answer
    #: for a deployment that has not wired this up: the route refuses instead of
    #: recording a reference to a file nobody can confirm exists.
    channel_secret_directory: str | None = None
    #: Which channel the dispatcher actually sends through (**B2-3**).
    #:
    #: `"sink"` (the default) keeps `HttpChannelAdapter` and
    #: `channel_send_url` -- the M0 simulator, which is what every environment
    #: has used so far. `"wecom_proxy"` wires the real provider adapter,
    #: constructed per dispatch from the binding the row resolves to.
    #:
    #: **A separate switch rather than "is `channel_send_url` still the
    #: placeholder?"**, and that is the point rather than a preference.
    #: ADR-0091 pins Stage to `channel-sink.invalid` while nothing may be sent,
    #: and `scripts/seed_channel_binding.py` refuses to create a binding against
    #: it. Inferring "go live" from that address changing would make **editing
    #: one URL** the act that starts sending real messages to real people -- an
    #: irreversible step taken by a value that exists to be a guard. Sending is
    #: made an explicit, separately named decision instead.
    #:
    #: **Unknown values are refused, not treated as `sink`** -- but only in the
    #: roles that read it (independent review 2026-09-09, P3-2). `from_env`
    #: validates this under `worker` and `all`; under `api` / `ingress` /
    #: `scheduler` the variable is not read and the field is `"sink"` whatever
    #: the environment says. So a typo in a **shared** env file surfaces when
    #: the worker starts, and not before. A typo must not silently mean "keep
    #: pretending" for the process that would do the sending, and that is the
    #: process this guards.
    channel_provider: str = "sink"
    #: The guid allowlist the dispatcher's provider client reads (**B2-3**).
    #:
    #: **A configured-but-absent file is default-deny** (`GuidAllowlist`'s own
    #: rule): no guid is permitted, so a worker whose allowlist has not been
    #: placed yet cannot send anywhere rather than sending everywhere. Same
    #: shape as `superuser_allowlist_file` above.
    #:
    #: **Not configuring it at all is different, and is refused at startup**
    #: when `channel_provider` is `"wecom_proxy"` -- see `channel_factory_for`.
    #: An empty path is `Path(".")`, which exists and is a directory, so it
    #: would raise from inside the dispatch loop on the first message rather
    #: than on an operator's screen.
    #:
    #: Only read when `channel_provider` is `"wecom_proxy"`.
    channel_guid_allowlist_file: str | None = None
    #: Where the provider client reads its credentials (**B2-3**).
    #:
    #: `None` means `DEFAULT_CREDENTIALS_PATH`, which since **ADR-0120** is the
    #: path this file has **inside the worker container**
    #: (`/run/provider-secrets/wecom_proxy.env`, the read-only mount) rather than
    #: the host location the Stage runbook documents. The two are different
    #: coordinate systems and the constant serves the container one.
    #: Configurable because it is a **file**, deliberately
    #: never the process environment (`load_credentials`' own rule: an appSecret
    #: in `environ` is readable by anything that can read `/proc`, and shows up
    #: in every `docker inspect`).
    #:
    #: Only read when `channel_provider` is `"wecom_proxy"`.
    channel_credentials_file: str | None = None
    #: The key that makes a purge tombstone's fingerprint irreversible (T154).
    #:
    #: **Its own secret, not derived from the access signing key.** Deriving it
    #: would tie "can we still tell two purged records apart" to the rotation
    #: schedule of a session key -- two things with no reason to share a
    #: lifetime, and rotating the signing key is meant to be cheap.
    #:
    #: `None` is allowed to load, so that the services that never purge (worker,
    #: scheduler, ingress) do not need it. The purge route refuses rather than
    #: falling back to an unkeyed digest -- a fallback here would silently
    #: reintroduce exactly what T154 is about.
    purge_fingerprint_key: str | None = None
    access_token_ttl_seconds: int = 900
    #: How long a refresh token stays usable. Fourteen days is the working
    #: figure, not a derived one -- long enough that a person who works one day a
    #: week is not logged out between shifts, short enough that a stolen cookie
    #: expires inside a sprint. **Configurable from the start** because the one
    #: thing that is certain is that a real merchant will want it different, and
    #: a constant compiled into a route is how that becomes a code change.
    refresh_token_ttl_seconds: int = 14 * 24 * 3600
    ws_ticket_ttl_seconds: int = 45
    runtime_deadline_seconds: int = 30

    def __repr__(self) -> str:
        """A log-safe rendering: no credential survives, everything else does.

        The rendering lives in `redaction.redacted_repr`; this method keeps
        **no list of its own**. The first draft tested `endswith("_url")` and
        `endswith("_key")` right here, which happened to cover today's fields
        while being strictly narrower than `redact()` -- a field named
        `channel_app_secret` or `bot_token` would have rendered in the clear
        (independent review 2026-09-02, P1-1). Rebuilding a manual list inside
        the fix is the exact defect T13 exists to remove, so the fix does not
        own one, and a structural test holds the property for whatever field
        gets added next.
        """

        return redacted_repr(self)

    @classmethod
    def from_env(cls, role: str = "all") -> Settings:
        allowed_roles = {"all", "api", "ingress", "worker", "scheduler"}
        if role not in allowed_roles:
            raise SettingsError("unknown process role")
        environment = os.getenv("FASTAGENT_ENVIRONMENT", "local").strip().lower()
        allowed_environments = {"local", "ci", "stage-m0", "stage", "production"}
        if environment not in allowed_environments:
            raise SettingsError("FASTAGENT_ENVIRONMENT is not an approved environment name")
        model_provider = (
            os.getenv("FASTAGENT_MODEL_PROVIDER", "mock").strip().lower()
            if role in {"all", "worker"}
            else ""
        )
        if role in {"all", "worker"} and environment not in {"local", "ci"}:
            # 表在模块顶上（`OFFLINE_MODEL_PROVIDERS`）。查表而不是逐个 `or`：
            # 下一支离线实现加进去时，**要么进表要么没这道闸**，
            # 而「忘了加一个 or」在代码里长得和「有意不挡它」一模一样。
            refusal = OFFLINE_MODEL_PROVIDERS.get(model_provider)
            if refusal is not None:
                raise SettingsError(refusal)

        # **B2-3.** Which channel actually sends. Refused rather than defaulted
        # when unrecognised: a typo in the one variable that decides whether
        # real messages reach real people must not quietly resolve to "keep
        # pretending". Same shape as the role and environment checks above --
        # this repository refuses unknown names at load rather than at the point
        # of use, so the failure is a startup error and not a silence.
        channel_provider = (
            os.getenv("FASTAGENT_CHANNEL_PROVIDER", "sink").strip().lower()
            if role in {"all", "worker"}
            else "sink"
        )
        if channel_provider not in {"sink", "wecom_proxy"}:
            raise SettingsError("FASTAGENT_CHANNEL_PROVIDER is not a known channel")

        signing_key = _required("FASTAGENT_ACCESS_SIGNING_KEY") if role in {"all", "api"} else ""
        if role in {"all", "api"} and len(signing_key.encode("utf-8")) < 32:
            raise SettingsError("FASTAGENT_ACCESS_SIGNING_KEY must contain at least 32 bytes")

        origins = (
            frozenset(
                value.strip()
                for value in _required("FASTAGENT_ALLOWED_WS_ORIGINS").split(",")
                if value.strip()
            )
            if role in {"all", "api"}
            else frozenset()
        )
        if role in {"all", "api"} and not origins:
            raise SettingsError("at least one WebSocket origin is required")

        def scoped(name: str, *roles: str) -> str:
            return _required(name) if role == "all" or role in roles else ""

        return cls(
            environment=environment,
            database_url=scoped("FASTAGENT_DATABASE_URL", "api"),
            ingress_database_url=scoped("FASTAGENT_INGRESS_DATABASE_URL", "ingress"),
            worker_database_url=scoped("FASTAGENT_WORKER_DATABASE_URL", "worker"),
            scheduler_database_url=scoped("FASTAGENT_SCHEDULER_DATABASE_URL", "scheduler"),
            queue_redis_url=scoped("FASTAGENT_QUEUE_REDIS_URL", "ingress", "worker", "scheduler"),
            cache_redis_url=scoped("FASTAGENT_CACHE_REDIS_URL", "api", "ingress"),
            access_signing_key=signing_key,
            model_provider=model_provider,
            channel_send_url=scoped("FASTAGENT_CHANNEL_SEND_URL", "worker"),
            allowed_ws_origins=origins,
            superuser_allowlist_file=(
                os.getenv("FASTAGENT_SUPERUSER_ALLOWLIST_FILE", "").strip() or None
            ),
            channel_provider=channel_provider,
            channel_credentials_file=(
                os.getenv("FASTAGENT_CHANNEL_CREDENTIALS_FILE", "").strip() or None
            ),
            channel_guid_allowlist_file=(
                os.getenv("FASTAGENT_CHANNEL_GUID_ALLOWLIST_FILE", "").strip() or None
            ),
            channel_secret_directory=(
                os.getenv("FASTAGENT_CHANNEL_SECRET_DIRECTORY", "").strip() or None
            ),
            purge_fingerprint_key=(
                os.getenv("FASTAGENT_PURGE_FINGERPRINT_KEY", "").strip() or None
            ),
            access_token_ttl_seconds=int(os.getenv("FASTAGENT_ACCESS_TOKEN_TTL_SECONDS", "900")),
            refresh_token_ttl_seconds=int(
                os.getenv("FASTAGENT_REFRESH_TOKEN_TTL_SECONDS", str(14 * 24 * 3600))
            ),
            ws_ticket_ttl_seconds=int(os.getenv("FASTAGENT_WS_TICKET_TTL_SECONDS", "45")),
            runtime_deadline_seconds=int(os.getenv("FASTAGENT_RUNTIME_DEADLINE_SECONDS", "30")),
        )
