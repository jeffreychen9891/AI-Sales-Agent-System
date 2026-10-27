"""Model policy releases.

A release binds each logical tier to one concrete model. It is immutable: a
model, dimension or deadline change means a new release file, never an edit to
an existing one, so that any recorded `model_evidence` can be resolved back to
the exact binding that produced it.

Two properties are enforced here rather than left to reviewer attention:

1. **Deadlines fit the envelope.** The per-role deadlines of the three chat
   steps must leave room inside the request envelope; a release that overspends
   it is rejected at load time, not at request time.
2. **An unverified release cannot serve real traffic.** Fields that were filled
   from the legacy system rather than confirmed against a v2-owned account are
   listed in `unverified_fields`. While that list is non-empty the release loads
   for structural work but `require_servable` refuses Stage and Production.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastagent_v2.runtime.contracts import CONTRACT_ROOT
from jsonschema import Draft202012Validator, FormatChecker

MODEL_POLICY_ROOT = CONTRACT_ROOT / "model-policy"
SCHEMA_PATH = MODEL_POLICY_ROOT / "model-policy-release.schema.json"
CAPABILITY_PATH = MODEL_POLICY_ROOT / "model-capabilities.json"

#: Roles that participate in one customer-event request and therefore share the
#: request envelope. VISION is excluded: it replaces part of understanding
#: rather than adding a step.
REQUEST_PATH_ROLES = ("UNDERSTAND_VALUE", "PLAN_CAPABILITY", "GENERATE_VALUE")

#: Environments where an unverified release must not be used.
NON_SERVING_ENVIRONMENTS = ("local", "ci")

#: Tiers that run inside a customer request and must therefore be served by a
#: model that returns strict structured output within its deadline.
REALTIME_TIERS = (
    "UNDERSTAND_VALUE",
    "PLAN_CAPABILITY",
    "GENERATE_VALUE",
    "COMMITMENT_CAPABILITY",
    #: **第四步，只在主动轮次跑**（ADR-0149）。它实时、要结构化输出、有自己的
    #: 输出契约 —— 所以进这张表；而它**不在 `REQUEST_PATH_ROLES` 里**，
    #: 因为那一串是「一次客户事件请求里共享同一个信封的角色」，
    #: 而这一步根本不在客户事件那条路上。
    #:
    #: **`COMMITMENT_CAPABILITY` 是现成的先例**：同样实时、同样有契约、
    #: 同样不在请求路径里 —— 这个形状不是本条发明的。
    "SILENCE_CAPABILITY",
)

REALTIME_OUTPUT_CONTRACTS = {
    "UNDERSTAND_VALUE": "understanding-result",
    "PLAN_CAPABILITY": "plan-result",
    "GENERATE_VALUE": "generation-result",
    "COMMITMENT_CAPABILITY": "plan-result",
    "SILENCE_CAPABILITY": "silence-reading-result",
}


class ModelPolicyError(RuntimeError):
    """Raised when a release is malformed, overspent, or not fit to serve."""


@dataclass(frozen=True, slots=True)
class RoleBinding:
    role: str
    provider_id: str
    model_id: str
    api_base: str
    temperature: float | None
    api_compatibility: str
    structured_output: str
    region: str
    context_limit_tokens: int | None
    deadline_ms: int
    max_output_tokens: int | None
    fallback: tuple[dict[str, str], ...]
    failure_action: str
    pricing: dict[str, Any]
    thinking_mode: str | None
    output_contract: str | None

    @property
    def has_fallback(self) -> bool:
        return bool(self.fallback)

    @property
    def can_price(self) -> bool:
        if "input_uncached" in self.pricing:
            return True
        return self.pricing.get("input") is not None and self.pricing.get("output") is not None

    @property
    def price_input(self) -> float | None:
        value = self.pricing.get("input_uncached", {}).get("peak")
        return value if value is not None else self.pricing.get("input")

    @property
    def price_output(self) -> float | None:
        value = self.pricing.get("output")
        if isinstance(value, dict):
            return value.get("peak")
        return value

    def pricing_period(self, at: datetime) -> str:
        """Return the supplier price period for the call instant."""

        if "peak_periods" not in self.pricing:
            return "flat"
        local = at.astimezone(ZoneInfo(str(self.pricing["timezone"])))
        minute = local.hour * 60 + local.minute
        for period in self.pricing["peak_periods"]:
            start_h, start_m = map(int, period["start"].split(":"))
            end_h, end_m = map(int, period["end"].split(":"))
            if (
                local.isoweekday() in period["weekdays"]
                and start_h * 60 + start_m <= minute < end_h * 60 + end_m
            ):
                return "peak"
        return "offpeak"

    def rate_snapshot(self, period: str) -> tuple[float | None, float | None, float | None]:
        """Return uncached-input, cached-input and output rates for ledger evidence."""

        if "input_uncached" not in self.pricing:
            return self.price_input, self.pricing.get("cached_input"), self.price_output
        return (
            float(self.pricing["input_uncached"][period]),
            float(self.pricing["input_cached"][period]),
            float(self.pricing["output"][period]),
        )

    def cost_cny(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        at: datetime | None = None,
        force_peak: bool = False,
    ) -> float | None:
        """Cost of one call, or None when the release carries no price.

        Returning None is deliberate: a zero would silently understate spend and
        make a ceiling look satisfied when nothing was actually measured.
        """

        if not self.can_price:
            return None
        if "input_uncached" not in self.pricing:
            assert self.price_input is not None and self.price_output is not None
            return (input_tokens * self.price_input + output_tokens * self.price_output) / 1_000_000
        period = "peak" if force_peak else self.pricing_period(at or datetime.now().astimezone())
        cached = min(input_tokens, max(0, cached_input_tokens))
        uncached = input_tokens - cached
        return (
            cached * float(self.pricing["input_cached"][period])
            + uncached * float(self.pricing["input_uncached"][period])
            + output_tokens * float(self.pricing["output"][period])
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPolicyRelease:
    document: dict[str, Any]
    content_hash: str
    path: Path

    @property
    def release_id(self) -> str:
        return str(self.document["release_id"])

    @property
    def version(self) -> str:
        return str(self.document["version"])

    @property
    def status(self) -> str:
        return str(self.document["status"])

    @property
    def envelope_deadline_ms(self) -> int:
        return int(self.document["envelope_deadline_ms"])

    @property
    def unverified_fields(self) -> tuple[dict[str, str], ...]:
        return tuple(self.document["unverified_fields"])

    @property
    def is_verified(self) -> bool:
        return not self.unverified_fields

    def role(self, name: str) -> RoleBinding:
        try:
            binding = self.document["roles"][name]
        except KeyError as error:
            raise ModelPolicyError(f"release {self.version} has no role {name!r}") from error
        return RoleBinding(
            role=name,
            provider_id=str(binding["provider_id"]),
            model_id=str(binding["model_id"]),
            api_base=str(binding["api_base"]),
            temperature=binding.get("temperature"),
            api_compatibility=str(binding["api_compatibility"]),
            structured_output=str(binding["structured_output"]),
            region=str(binding["region"]),
            context_limit_tokens=binding["context_limit_tokens"],
            deadline_ms=int(binding["deadline_ms"]),
            max_output_tokens=binding.get("max_output_tokens"),
            fallback=tuple(dict(item) for item in binding["fallback"]),
            failure_action=str(binding["failure_action"]),
            pricing=dict(binding["price_cny_per_million_tokens"]),
            thinking_mode=binding.get("thinking_mode"),
            output_contract=binding.get("output_contract"),
        )

    @property
    def ceilings_cny(self) -> dict[str, Any]:
        return dict(self.document["ceilings_cny"])

    def request_path_deadline_ms(self) -> int:
        return sum(self.role(name).deadline_ms for name in REQUEST_PATH_ROLES)

    def distinct_models(self) -> tuple[str, ...]:
        return tuple(
            sorted({str(binding["model_id"]) for binding in self.document["roles"].values()})
        )

    def require_servable(self, environment: str) -> None:
        """Refuse to serve an environment the release is not fit for."""

        if environment in NON_SERVING_ENVIRONMENTS:
            return
        if self.status != "active":
            raise ModelPolicyError(
                f"release {self.version} has status {self.status!r} and cannot serve {environment}"
            )
        if not self.is_verified:
            paths = ", ".join(item["path"] for item in self.unverified_fields)
            raise ModelPolicyError(
                f"release {self.version} still has unverified fields ({paths}); "
                f"it must not serve {environment}"
            )


@cache
def capabilities() -> dict[str, Any]:
    document = cast(dict[str, Any], json.loads(CAPABILITY_PATH.read_text(encoding="utf-8")))
    return cast(dict[str, Any], document["models"])


def capability_for(model_id: str) -> dict[str, Any] | None:
    return capabilities().get(model_id)


def check_realtime_bindings(release: ModelPolicyRelease) -> None:
    """Refuse a release that puts an unsuitable model on the request path.

    This gate exists because of a concrete incident: a reasoning model spent its
    whole output budget on reasoning, returned `finish_reason=length` with no
    parseable answer, and the failure was only visible once real traffic hit it.
    An annotation table plus a load-time check turns that lesson into something
    the next release cannot repeat by accident.
    """

    table = capabilities()
    for tier in REALTIME_TIERS:
        binding = release.role(tier)
        entry = table.get(binding.model_id)
        if entry is None:
            raise ModelPolicyError(
                f"{tier} binds {binding.model_id!r}, which has no capability annotation; "
                "add one with evidence before binding it to a real-time tier"
            )
        if not entry.get("realtime_eligible"):
            raise ModelPolicyError(
                f"{tier} binds {binding.model_id!r}, which is not eligible for the request "
                f"path: {entry.get('observation', 'no observation recorded')}"
            )
        if not entry.get("strict_structured_output"):
            raise ModelPolicyError(
                f"{tier} binds {binding.model_id!r}, which is not annotated as supporting "
                "strict structured output"
            )
        if binding.thinking_mode != "disabled":
            raise ModelPolicyError(f"{tier} must explicitly disable thinking for real-time use")
        if release.document["schema_version"] != "1.0.0":
            replay_api = entry.get("replay_api_compatibility") if entry else None
            replay_output = entry.get("replay_structured_output") if entry else None
            if (
                binding.api_compatibility != replay_api
                or binding.structured_output != replay_output
            ):
                raise ModelPolicyError(
                    f"{tier} must use the replay-validated protocol combination "
                    f"{replay_api!r} + {replay_output!r}"
                )
        if binding.api_compatibility == "openai-responses-v1":
            expected_contract = REALTIME_OUTPUT_CONTRACTS[tier]
            if binding.structured_output != "json_schema":
                raise ModelPolicyError(f"{tier} Responses binding must use json_schema output")
            if binding.output_contract != expected_contract:
                raise ModelPolicyError(
                    f"{tier} Responses binding must use output_contract {expected_contract!r}"
                )
        if release.document["schema_version"] != "1.0.0" and not entry.get("replay_gate_passed"):
            raise ModelPolicyError(
                f"{tier} binds {binding.model_id!r}, which has not passed the per-role "
                "6/6 replay gate"
            )


def canonical_hash(document: dict[str, Any]) -> str:
    """Hash of the binding content.

    `status` is excluded on purpose: marking a release superseded is a lifecycle
    transition, not a change to what it binds. If the hash moved when a release
    retired, evidence recorded under it could no longer be resolved.
    """

    body = {key: value for key, value in document.items() if key != "status"}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@cache
def _schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def load_release(path: Path) -> ModelPolicyRelease:
    document = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    Draft202012Validator(_schema(), format_checker=FormatChecker()).validate(document)

    release = ModelPolicyRelease(
        document=document, content_hash=canonical_hash(document), path=path
    )
    spent = release.request_path_deadline_ms()
    if spent >= release.envelope_deadline_ms:
        raise ModelPolicyError(
            f"role deadlines total {spent}ms and do not fit the "
            f"{release.envelope_deadline_ms}ms request envelope"
        )
    if release.status == "active":
        check_realtime_bindings(release)
    return release


def active_release() -> ModelPolicyRelease:
    """The single release marked active. More than one is a configuration error."""

    active = []
    for path in sorted(MODEL_POLICY_ROOT.glob("release-*.json")):
        release = load_release(path)
        if release.status == "active":
            active.append(release)
    if not active:
        raise ModelPolicyError("no active model policy release is present")
    if len(active) > 1:
        versions = ", ".join(release.version for release in active)
        raise ModelPolicyError(f"more than one active model policy release: {versions}")
    return active[0]
