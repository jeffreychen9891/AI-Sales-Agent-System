"""Chat provider adapters behind one narrow transport seam.

Everything that talks to a model vendor goes through `ChatTransport`. Tests
inject a double; nothing in the test suite is allowed to open a socket, and
nothing here reads a credential from anywhere but the process environment.

> **【2026-10-25 更正，原文留着】** 上面那句「nothing in the test suite is
> allowed to open a socket」**从写下的那天起没有执行者**：
> `requirements-dev.in` 七行里没有任何东西验它。按本仓五条判据，
> 那是第 1 类「约束声明了，运行期无人执行」——**而它犯在声明它的这一行上。**
>
> 今天它有执行者了（`pyproject.toml` 的 `--allow-hosts`，钉它的是
> `tests/contracts/test_the_socket_guard_is_live.py`），
> **而这句话同时要收窄成它真正成立的那个方向**：
>
> * 拦的是 **`connect`**，不是「开 socket」—— `bind` / `listen` / `accept`
>   照常，`tests/conftest.py` 的 `channel_sink` 就靠它起 uvicorn；
> * 拦的是 **非回环目标**。回环必须放行：`integration` / `e2e` 要连本机的
>   PostgreSQL 与 Redis。所以准确的说法是
>   **「用例不许连到本机之外」**，不是「不许开 socket」；
> * 只管**本进程**。用例起的子进程不在它管辖内。
>
> 原句那种不带方向的写法正是 `AGENTS.md`「描述性质的词，在它只于某个方向
> 成立时必须带方向」点名的形状：**下一个人会按最强的读法去读它，
> 然后不再检查另一个方向。**

Three rules this module exists to enforce:

1. **A provider failure is never a business decision.** Timeouts, HTTP errors
   and malformed output raise typed errors; the caller maps them to the failure
   action the model policy release declares. The adapter never substitutes its
   own answer.
2. **A repeatedly failing binding stops being called.** The breaker opens per
   provider/model/role bucket so one broken role cannot spend the whole
   envelope on retries.
3. **Credentials and prompts never enter evidence.** `model_evidence` carries
   provider, model, prompt release, latency and token counts -- nothing else.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from fastagent_v2.runtime.contracts import model_output_schema
from fastagent_v2.worker.cost import (  # noqa: I001 - grouped by concern below
    UNSETTLED_BACKLOG_REASON_CODE,
    CallContext,
    CostCeilingExceeded,
    CostGovernor,
    worst_case_input_tokens,
)
from fastagent_v2.worker.metrics import SCHEMA_REPAIR_TOTAL, SCHEMA_UNREPAIRABLE_TOTAL
from fastagent_v2.worker.model_policy import RoleBinding

#: Environment variable holding a provider credential, by provider id.
CREDENTIAL_ENV_TEMPLATE = "FASTAGENT_MODEL_API_KEY_{provider}"

#: How the transport serialises a JSON body, copied from `httpx._content.encode_json`.
#:
#: **This exists so the size check can measure the request that is actually
#: sent.** `self._client.post(..., json=payload)` hands the dict to httpx, which
#: builds the body with these exact arguments; anything else measures a string
#: nothing transmits. Spelled out here rather than imported because httpx does
#: not export it -- and pinned by
#: `test_the_measured_request_is_the_body_httpx_will_send`, which compares this
#: serialisation byte-for-byte against httpx's own encoder on a payload carrying
#: Chinese and an emoji. **An httpx upgrade that changes the encoding turns that
#: case red rather than silently re-opening the gap it was written for.**
_WIRE_JSON: dict[str, Any] = {
    "ensure_ascii": False,
    "separators": (",", ":"),
    "allow_nan": False,
}


def request_payload(
    binding: RoleBinding, *, system_prompt: str, user_prompt: str
) -> dict[str, Any]:
    """供应商真正收到的那个 payload，**唯一构造点**。

    **它先前长在 `complete()` 里，而这一次搬出来是被审查逼的**
    （独立审查 2026-09-12，P1-1）：从 `MAX_MODEL_INPUT_BYTES` 反推预算时要量
    「一个空请求有多大」，而当时没有任何地方能拿到这个 payload ——
    于是作者从 `tests/contracts/test_prompt_bytes.py` 的 helper 读了那个绝对值，
    **而那个 helper 的 docstring 自己写着「must never be used to measure a
    baseline」**，并点名它缺 `stream` / `reasoning` / `max_output_tokens` /
    `temperature` 且 `input` 用的是 chat 的 message 数组。

    量出来的差是 **8,012 对 7,936，76 字节，方向是 fail-open**。

    **所以这个函数存在的意义不是整洁，是让那个基线有一个可被用例钉住的来源。**
    `test_the_empty_request_baseline_is_measured_from_the_real_payload`
    从这里量，预算的推导式也从这里来 —— 不再有第二份形状。
    """

    if binding.api_compatibility == "openai-responses-v1":
        if binding.output_contract is None:
            raise ProviderMalformedOutput(
                "Responses binding has no output_contract", role=binding.role
            )
        payload = {
            "model": binding.model_id,
            "instructions": system_prompt,
            "input": user_prompt,
            "stream": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": binding.role.lower(),
                    "schema": model_output_schema(binding.output_contract),
                }
            },
        }
        if binding.thinking_mode == "disabled":
            payload["reasoning"] = {"effort": "none"}
        if binding.max_output_tokens is not None:
            payload["max_output_tokens"] = binding.max_output_tokens
    else:
        payload = {
            "model": binding.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        if binding.structured_output == "json_object":
            payload["response_format"] = {"type": "json_object"}
        if binding.max_output_tokens is not None:
            payload["max_tokens"] = binding.max_output_tokens
        if binding.thinking_mode is not None:
            payload["thinking"] = {"type": binding.thinking_mode}
    if binding.temperature is not None:
        payload["temperature"] = binding.temperature
    return payload


class ProviderError(RuntimeError):
    """Base class for a failed model call. Carries a stable reason code."""

    reason_code = "PROVIDER_FAILURE"

    def __init__(self, message: str, *, role: str) -> None:
        super().__init__(message)
        self.role = role


class ProviderCredentialMissing(ProviderError):
    reason_code = "PROVIDER_CREDENTIAL_MISSING"


class ProviderTimeout(ProviderError):
    reason_code = "PROVIDER_TIMEOUT"


class ProviderHttpError(ProviderError):
    reason_code = "PROVIDER_HTTP_ERROR"


class ProviderMalformedOutput(ProviderError):
    reason_code = "PROVIDER_MALFORMED_OUTPUT"


class ProviderCircuitOpen(ProviderError):
    reason_code = "PROVIDER_CIRCUIT_OPEN"


class ProviderInputTooLarge(ProviderError):
    reason_code = "PROVIDER_INPUT_TOO_LARGE"


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: dict[str, Any]


class ChatTransport(Protocol):
    """The only place a socket may be opened."""

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse: ...


class HttpxChatTransport:
    """Real transport. Constructed only by the worker process."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        response = self._client.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        try:
            body = response.json()
        except ValueError:
            body = {}
        return TransportResponse(status_code=int(response.status_code), body=body)


@dataclass
class CircuitBreaker:
    """Per provider/model/role failure bucket.

    Deliberately simple: consecutive failures open it, and it half-opens after a
    cooldown so a recovered provider is retried once rather than being locked
    out until a restart.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 30.0
    _failures: dict[str, int] = field(default_factory=dict)
    _opened_at: dict[str, float] = field(default_factory=dict)

    def _now(self) -> float:
        return time.monotonic()

    def check(self, bucket: str) -> None:
        opened = self._opened_at.get(bucket)
        if opened is None:
            return
        if self._now() - opened < self.cooldown_seconds:
            raise ProviderCircuitOpen(f"circuit open for {bucket}", role=bucket)
        # Half-open: allow exactly one probe by clearing the open marker.
        del self._opened_at[bucket]

    def record_success(self, bucket: str) -> None:
        self._failures.pop(bucket, None)
        self._opened_at.pop(bucket, None)

    def record_failure(self, bucket: str) -> None:
        count = self._failures.get(bucket, 0) + 1
        self._failures[bucket] = count
        if count >= self.failure_threshold:
            self._opened_at[bucket] = self._now()

    def is_open(self, bucket: str) -> bool:
        opened = self._opened_at.get(bucket)
        return opened is not None and self._now() - opened < self.cooldown_seconds


def credential_env_name(provider_id: str) -> str:
    """The one place the environment variable's name is derived.

    Pulled out of `credential_for` because a second reader appeared: the Stage
    balance job runs on the host, outside this image, so it cannot import this
    module and must carry its own copy of the rule. A copy nobody can point at
    is a copy that drifts -- and it did, in the first version of that job, which
    lower-cased the provider id and therefore read an environment variable
    nobody sets (independent review 2026-09-04, T71 F2). A test now pins the
    job's copy to this function rather than to itself.
    """

    return CREDENTIAL_ENV_TEMPLATE.format(provider=provider_id.upper().replace("-", "_"))


def credential_for(provider_id: str) -> str:
    name = credential_env_name(provider_id)
    value = os.getenv(name, "").strip()
    if not value:
        raise ProviderCredentialMissing(
            f"no credential in {name}; v2 must use its own account, never a legacy one",
            role=provider_id,
        )
    return value


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    content: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: int


class OpenAICompatibleChatClient:
    """Chat client for the OpenAI-compatible `/chat/completions` shape."""

    def __init__(
        self,
        *,
        transport: ChatTransport,
        breaker: CircuitBreaker | None = None,
        prompt_release: str = "unset",
    ) -> None:
        self._transport = transport
        self._breaker = breaker or CircuitBreaker()
        self._prompt_release = prompt_release

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def complete(
        self,
        *,
        binding: RoleBinding,
        system_prompt: str,
        user_prompt: str,
        governor: CostGovernor | None = None,
        context: CallContext | None = None,
    ) -> ChatCompletion:
        bucket = f"{binding.provider_id}:{binding.model_id}:{binding.role}"
        self._breaker.check(bucket)
        credential = credential_for(binding.provider_id)

        payload = request_payload(
            binding, system_prompt=system_prompt, user_prompt=user_prompt
        )

        # **The limit is measured on the bytes that actually go on the wire**
        # (运维报障 2026-09-11). The line below used to say "serialize exactly
        # what the transport will receive" and then serialize something else:
        # `ensure_ascii=True` renders every Chinese character as `\uXXXX`, six
        # ASCII bytes where the transport sends three UTF-8 ones. httpx builds
        # the body itself from `payload` -- `httpx._content.encode_json` uses
        # `ensure_ascii=False, separators=(",", ":")` -- so the string being
        # measured was **not the request being sent**.
        #
        # **Measured, not argued**: one plan request from the internal demo
        # profile is **6,132 bytes on the wire and 9,690 as measured** (×1.58;
        # a pure-Chinese段 is ×2.00). A plan call was refused at "16,400 bytes"
        # against a 16,384 limit -- **by this check, not by the provider**,
        # whose copy of that request was roughly half the size. The turn then
        # fell back to a conservative plan and sent nothing.
        #
        # `MAX_MODEL_INPUT_BYTES`'s own docstring says 「must fit in this many
        # **UTF-8 bytes**」. It now does; before this it said one thing and
        # checked another. **Same family as「证据取自与真实不同的环境」**: the
        # measurement was real, it just measured a string nothing sends.
        #
        # `_WIRE_JSON` is pinned byte-for-byte against httpx's own encoder by
        # `test_the_measured_request_is_the_body_httpx_will_send`, so an httpx
        # upgrade that changes the encoding turns that case red instead of
        # silently re-opening this gap.
        wire_request = json.dumps(payload, **_WIRE_JSON)
        try:
            worst_case_input_tokens(wire_request)
        except ValueError as error:
            raise ProviderInputTooLarge(str(error), role=binding.role) from error

        # **The cost reservation keeps the inflated form on purpose.** Reserving
        # one token per byte is an upper bound either way, and the escaped form
        # reserves more -- over-reserving spends budget headroom, under-reserving
        # lets a call through that the governor should have stopped. The limit
        # and the reservation answer different questions, so they are allowed to
        # use different serialisations; what is not allowed is the limit using
        # one that nothing sends.
        serialized_request = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

        ledger_row = None
        call_at = datetime.now(UTC)
        if governor is not None and context is not None:
            # Responses providers bill the JSON Schema as input. Include its
            # serialized form in the conservative local estimate; omitting it
            # would systematically under-reserve every structured call.
            estimated_input, estimated_output, estimated_cost = governor.estimate(
                binding=binding, system_prompt=serialized_request, user_prompt=""
            )
            decision, ledger_row = governor.reserve(
                binding=binding,
                context=context,
                estimated_input=estimated_input,
                estimated_output=estimated_output,
                estimated_cost=estimated_cost,
                prompt_release=self._prompt_release,
                occurred_at=call_at,
            )
            if decision.is_deny:
                # Refused before the request is sent: a hard ceiling that only
                # notices after the money is spent is not a ceiling.
                # The wording splits by reason code because the numbers mean
                # different things: a count gate carries a *count* in `limit` /
                # `projected`, not CNY, and calling that "the ceiling" tells the
                # first person to read it that the budget ran out when what
                # actually happened is that settlement stopped.
                if decision.reason_code == UNSETTLED_BACKLOG_REASON_CODE:
                    # P2-1. The in-flight count was being put on the decision and
                    # read by nobody: the exception message, the no-send record
                    # and `audit_events` all derive from this string, so a number
                    # that never reaches it never reaches a person. "5 abandoned,
                    # 0 in flight" and "5 abandoned, 30 in flight" call for
                    # different first moves, which was the whole reason to carry
                    # it.
                    detail = (
                        f"{decision.projected} abandoned calls in the {decision.window} "
                        f"window exceeds {decision.limit}"
                    )
                    if decision.inactive_reason:
                        detail = f"{detail}; {decision.inactive_reason}"
                else:
                    detail = f"projected {decision.projected} CNY over {decision.limit}"
                raise CostCeilingExceeded(
                    f"{binding.role} refused by {decision.reason_code} "
                    f"({decision.window}): {detail}",
                    window=str(decision.window),
                    limit=decision.limit or Decimal(0),
                    projected=decision.projected or Decimal(0),
                    reason_code=decision.reason_code,
                )

        started = time.monotonic()
        try:
            response = self._transport.post_json(
                url=(
                    f"{binding.api_base.rstrip('/')}/v1/responses"
                    if binding.api_compatibility == "openai-responses-v1"
                    else f"{binding.api_base.rstrip('/')}/v1/chat/completions"
                ),
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                payload=payload,
                timeout_seconds=binding.deadline_ms / 1000,
            )
        except TimeoutError as error:
            self._breaker.record_failure(bucket)
            # Delivery/billing is unknown after a timeout. Keep the conservative
            # estimate reserved until reconciliation; releasing it would let
            # repeated timeouts bypass the hard ceiling.
            raise ProviderTimeout(
                f"{binding.role} exceeded its {binding.deadline_ms}ms deadline", role=binding.role
            ) from error
        except Exception as error:  # noqa: BLE001 - transport failures are all one class here
            self._breaker.record_failure(bucket)
            # A transport exception can happen after the request reached the
            # supplier. Its reservation therefore remains budget-consuming.
            raise ProviderHttpError(
                f"{binding.role} transport failed: {type(error).__name__}", role=binding.role
            ) from error
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            self._breaker.record_failure(bucket)
            self._settle_measured_usage(
                governor, context, ledger_row, binding, response.body, call_at
            )
            raise ProviderHttpError(
                f"{binding.role} returned HTTP {response.status_code}", role=binding.role
            )

        try:
            content = self._extract_content(response.body, binding=binding)
        except ProviderMalformedOutput:
            self._breaker.record_failure(bucket)
            # The supplier may have billed a syntactically unusable response.
            # Settle measured usage when present; otherwise retain the estimate.
            self._settle_measured_usage(
                governor, context, ledger_row, binding, response.body, call_at
            )
            raise

        self._breaker.record_success(bucket)
        input_tokens, output_tokens, cached_input_tokens, measured = self._usage(
            response.body
        )
        if measured and governor is not None and context is not None and ledger_row is not None:
            governor.settle(
                row_id=ledger_row,
                context=context,
                binding=binding,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                occurred_at=call_at,
            )
        return ChatCompletion(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _usage(body: dict[str, Any]) -> tuple[int, int, int, bool]:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return 0, 0, 0, False
        input_value = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_value = usage.get("output_tokens", usage.get("completion_tokens"))
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            return 0, 0, 0, False
        cached_value = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))

        def valid_count(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0

        if not all(valid_count(value) for value in (input_value, output_value, cached_value)):
            return 0, 0, 0, False
        input_count = cast(int, input_value)
        output_count = cast(int, output_value)
        cached_count = cast(int, cached_value)
        if cached_count > input_count:
            return 0, 0, 0, False
        return input_count, output_count, cached_count, True

    @classmethod
    def _settle_measured_usage(
        cls,
        governor: CostGovernor | None,
        context: CallContext | None,
        row_id: Any,
        binding: RoleBinding,
        body: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        input_tokens, output_tokens, cached_input_tokens, measured = cls._usage(body)
        if not measured or governor is None or context is None or row_id is None:
            return
        governor.settle(
            row_id=row_id,
            context=context,
            binding=binding,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            occurred_at=occurred_at,
        )

    def _extract_content(
        self, body: dict[str, Any], *, binding: RoleBinding | None = None
    ) -> dict[str, Any]:
        if binding is not None and binding.api_compatibility == "openai-responses-v1":
            for item in body.get("output") or []:
                if item.get("type") != "message":
                    continue
                for block in item.get("content") or []:
                    if block.get("type") == "output_text" and isinstance(block.get("text"), str):
                        return parse_model_json(block["text"], binding=binding)
            raise ProviderMalformedOutput("response has no output message text", role=binding.role)
        try:
            raw = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderMalformedOutput(
                "response has no choices[0].message.content", role="unknown"
            ) from error
        if not isinstance(raw, str):
            raise ProviderMalformedOutput("message content is not a string", role="unknown")
        return parse_model_json(raw, binding=binding)


#: Ordered repair attempts. Each is a narrow, reversible transformation of the
#: raw text -- never an edit to the parsed structure, which would let the
#: platform invent content the model did not produce.
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?|\n?\s*```\s*$")

#: Sentinel distinguishing "not JSON at all" from "JSON that decoded to None".
_NOT_JSON = object()


def _strip_fence(text: str) -> str:
    return _FENCE.sub("", text).strip()


def _first_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


_REPAIRS: tuple[tuple[str, Any], ...] = (
    ("markdown_fence", _strip_fence),
    ("surrounding_prose", lambda text: _first_object(_strip_fence(text))),
)


def parse_model_json(raw: str, *, binding: RoleBinding | None = None) -> dict[str, Any]:
    """Parse a model response, repairing only its envelope, and count repairs.

    Under a strict `response_format` no repair should be needed. The layer stays
    resident anyway because the failure it guards against is silent and only
    appears under real traffic -- but every repair is counted, so "we needed it
    again today" is a number someone can see rather than a hunch.
    """

    def as_object(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    labels = (
        (binding.provider_id, binding.model_id, binding.role)
        if binding is not None
        else ("unknown", "unknown", "unknown")
    )

    try:
        direct = json.loads(raw)
    except json.JSONDecodeError:
        direct = _NOT_JSON
    if direct is not _NOT_JSON:
        if isinstance(direct, dict):
            return direct
        # Valid JSON of the wrong shape is not an envelope problem, so no
        # repair applies. Digging an object out of an array would mean choosing
        # one element the model returned -- that is editing content, not
        # repairing a wrapper.
        SCHEMA_UNREPAIRABLE_TOTAL.increment(*labels)
        raise ProviderMalformedOutput(
            f"message content is valid JSON but a {type(direct).__name__}, not an object",
            role=labels[2],
        )
    for name, repair in _REPAIRS:
        candidate = as_object(repair(raw))
        if candidate is not None:
            SCHEMA_REPAIR_TOTAL.increment(*labels, name)
            return candidate

    SCHEMA_UNREPAIRABLE_TOTAL.increment(*labels)
    # The raw text is not attached: it is model output about a customer
    # conversation and does not belong in an exception message or log.
    raise ProviderMalformedOutput(
        "message content is not a JSON object, before or after repair",
        role=labels[2],
    )


def model_evidence(
    *, binding: RoleBinding, completion: ChatCompletion, prompt_release: str
) -> dict[str, Any]:
    """Build the evidence record. Never includes prompt or response content."""

    token_usage = {
        "input": completion.input_tokens,
        "output": completion.output_tokens,
    }
    if completion.cached_input_tokens:
        token_usage["cached_input"] = completion.cached_input_tokens
    return {
        "provider": binding.provider_id,
        "model": binding.model_id,
        "prompt_release": prompt_release,
        "latency_ms": completion.latency_ms,
        "token_usage": token_usage,
    }
