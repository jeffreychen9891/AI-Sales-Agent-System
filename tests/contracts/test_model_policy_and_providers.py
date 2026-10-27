"""Contract tests for the model policy release and the provider seam.

The questions these answer: can an unverified release serve customers, can a
provider failure turn into an invented reply, and can a credential or a prompt
reach evidence.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastagent_v2.worker.cost import MAX_MODEL_INPUT_BYTES, CallContext, CeilingDecision
from fastagent_v2.worker.inference_model import ModelBackedInference
from fastagent_v2.worker.model_policy import (
    MODEL_POLICY_ROOT,
    REQUEST_PATH_ROLES,
    ModelPolicyError,
    active_release,
    canonical_hash,
    load_release,
)
from fastagent_v2.worker.providers import (
    CircuitBreaker,
    OpenAICompatibleChatClient,
    ProviderCircuitOpen,
    ProviderCredentialMissing,
    ProviderHttpError,
    ProviderInputTooLarge,
    ProviderMalformedOutput,
    ProviderTimeout,
    TransportResponse,
    model_evidence,
)

ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_ENV = "FASTAGENT_MODEL_API_KEY_DEEPSEEK"


class RecordingTransport:
    """Test double. It never opens a socket."""

    def __init__(self, *, body: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self.body = body if body is not None else _responses_body({"ok": True})
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return TransportResponse(status_code=self.status_code, body=self.body)


class FailingTransport:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def post_json(self, **_: Any) -> TransportResponse:
        raise self.error


def _chat_body(content: dict[str, Any], *, tokens: tuple[int, int] = (11, 7)) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
    }


def _responses_body(content: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps(content, ensure_ascii=False)}
                ],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


@pytest.fixture()
def credential(monkeypatch: pytest.MonkeyPatch) -> str:
    value = "test-only-not-a-real-key"
    monkeypatch.setenv(CREDENTIAL_ENV, value)
    return value


def test_exactly_one_release_is_active() -> None:
    """**这条红 = 有人发了一份新的模型策略，而那是要被看见的。**

    它钉的不是「版本号必须是这个」，是「**换在架件是一次刻意的动作**」——
    策略件决定每一次模型调用用哪个供应商、哪个模型、什么 deadline、什么预算，
    换了而没人注意，下一次成本或延迟的变化就没有出处。

    **2026-09-22：`-7` → `-8`**（ADR-0149）。`-8` 在 `-7` 之上只加一个角色
    `SILENCE_CAPABILITY`，绑定逐字复刻 `UNDERSTAND_VALUE`、只换 `output_contract`
    （用户裁定：「和理解步用一样的就行，本质是去理解为什么对话没有继续」）。
    `-7` 的内容一个字没动，只把 `status` 翻成 `superseded` —— 而
    `test_retiring_a_release_does_not_move_its_content_hash` 保证那个动作
    不会移动它的 `content_hash`。
    """

    release = active_release()
    assert release.status == "active"
    assert release.version == "2026.09.02-8"


def test_retiring_a_release_does_not_move_its_content_hash() -> None:
    """`status` is excluded from `canonical_hash`, and this is why.

    `model_invocation_costs.release_content_hash` records which binding a cost
    was measured under. If retiring a release moved its hash, every cost row
    recorded under it would point at a hash that no longer resolves. `-4` was
    retired in favour of `-5` on 2026-09-02; its hash must be exactly what it
    was while active.
    """

    retired = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-4.json")
    assert retired.status == "superseded"
    assert (
        retired.content_hash
        == "d1e2cfd4fbe491f386987e4f8606d8de81bbedd983532789398e4d280a0f37fa"
    )


def test_peak_pricing_applies_every_day_not_only_on_weekdays() -> None:
    """User-confirmed 2026-09-02: DeepSeek's peak window runs every day.

    `-4` recorded it as Monday-to-Friday. That error ran in the unsafe
    direction: a Saturday 10:00 call would have been priced off-peak, i.e. 30x
    under-counted on uncached input, so the RMB ceiling would trigger late.
    """

    release = active_release()
    for role in release.document["roles"]:
        price = release.document["roles"][role].get("price_cny_per_million_tokens")
        if price is None:
            continue
        for period in price["peak_periods"]:
            assert period["weekdays"] == [1, 2, 3, 4, 5, 6, 7], role


def test_active_release_explicitly_disables_thinking_in_every_realtime_role(
    credential: str,
) -> None:
    draft = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-4.json")
    transport = RecordingTransport(body=_responses_body({"ok": True}))
    binding = draft.role("GENERATE_VALUE")
    OpenAICompatibleChatClient(transport=transport).complete(
        binding=binding, system_prompt="return json", user_prompt="{}"
    )
    assert binding.thinking_mode == "disabled"
    assert transport.calls[0]["url"].endswith("/v1/responses")
    assert transport.calls[0]["payload"]["reasoning"] == {"effort": "none"}
    assert transport.calls[0]["payload"]["text"]["format"]["type"] == "json_schema"


def test_retiring_a_release_does_not_move_its_binding_hash() -> None:
    """Evidence recorded under a release must stay resolvable after it retires."""

    superseded = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-1.json")
    assert superseded.status == "superseded"
    active = dict(superseded.document)
    active["status"] = "active"
    assert canonical_hash(active) == superseded.content_hash


def test_role_deadlines_fit_inside_the_request_envelope() -> None:
    release = active_release()
    spent = release.request_path_deadline_ms()
    assert spent == sum(release.role(name).deadline_ms for name in REQUEST_PATH_ROLES)
    assert spent < release.envelope_deadline_ms


def test_a_release_whose_deadlines_overspend_the_envelope_is_rejected(tmp_path: Path) -> None:
    document = copy.deepcopy(active_release().document)
    document["roles"]["PLAN_CAPABILITY"]["deadline_ms"] = 25000
    path = tmp_path / "release-9999.01.01-1.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ModelPolicyError, match="request envelope"):
        load_release(path)


def test_an_unverified_release_may_not_serve_stage_or_production() -> None:
    document = copy.deepcopy(
        load_release(MODEL_POLICY_ROOT / "release-2026.09.02-3.json").document
    )
    document["status"] = "active"
    release = type(active_release())(
        document=document, content_hash=canonical_hash(document), path=Path("in-memory")
    )
    assert not release.is_verified
    for environment in ("stage", "production"):
        with pytest.raises(ModelPolicyError, match="unverified"):
            release.require_servable(environment)
    release.require_servable("local")
    release.require_servable("ci")


def test_the_release_records_shared_model_and_verified_responses_contracts() -> None:
    release = active_release()
    chat_models = {
        release.role(name).model_id
        for name in (
            "UNDERSTAND_VALUE",
            "PLAN_CAPABILITY",
            "GENERATE_VALUE",
            "COMMITMENT_CAPABILITY",
        )
    }
    assert len(chat_models) == 1, "the four chat tiers currently share one model"
    assert all(
        release.role(name).api_compatibility == "openai-responses-v1"
        for name in REQUEST_PATH_ROLES
    )
    assert all(release.role(name).output_contract for name in REQUEST_PATH_ROLES)
    assert release.unverified_fields == ()


def test_cost_is_none_rather_than_zero_when_the_release_has_no_price() -> None:
    binding = load_release(MODEL_POLICY_ROOT / "release-2026.09.02-3.json").role(
        "GENERATE_VALUE"
    )
    assert binding.can_price is False
    assert binding.cost_cny(input_tokens=1000, output_tokens=1000) is None


def test_content_hash_is_stable_and_order_independent() -> None:
    release = active_release()
    shuffled = dict(reversed(list(release.document.items())))
    assert canonical_hash(shuffled) == release.content_hash


def test_release_file_matches_its_schema_on_disk() -> None:
    files = sorted(MODEL_POLICY_ROOT.glob("release-*.json"))
    assert files, "at least one release file must exist"
    for path in files:
        load_release(path)


def test_a_missing_credential_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    transport = RecordingTransport()
    client = OpenAICompatibleChatClient(transport=transport)
    with pytest.raises(ProviderCredentialMissing):
        client.complete(
            binding=active_release().role("GENERATE_VALUE"),
            system_prompt="s",
            user_prompt="u",
        )
    assert transport.calls == [], "no request may be sent without a credential"


def test_the_deadline_of_the_binding_is_passed_to_the_transport(credential: str) -> None:
    transport = RecordingTransport()
    binding = active_release().role("PLAN_CAPABILITY")
    OpenAICompatibleChatClient(transport=transport).complete(
        binding=binding, system_prompt="s", user_prompt="u"
    )
    assert transport.calls[0]["timeout_seconds"] == binding.deadline_ms / 1000
    assert transport.calls[0]["payload"]["model"] == binding.model_id


def test_a_timeout_becomes_a_typed_error_not_an_answer(credential: str) -> None:
    client = OpenAICompatibleChatClient(transport=FailingTransport(TimeoutError("slow")))
    with pytest.raises(ProviderTimeout) as error:
        client.complete(
            binding=active_release().role("GENERATE_VALUE"), system_prompt="s", user_prompt="u"
        )
    assert error.value.reason_code == "PROVIDER_TIMEOUT"


def test_oversize_unicode_input_is_refused_before_transport(credential: str) -> None:
    transport = RecordingTransport()
    client = OpenAICompatibleChatClient(transport=transport)
    with pytest.raises(ProviderInputTooLarge, match="contract limit"):
        client.complete(
            binding=active_release().role("GENERATE_VALUE"),
            system_prompt="system",
            user_prompt="🚀" * MAX_MODEL_INPUT_BYTES,
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10},
        {"output_tokens": 2},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": -2},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 10.5, "output_tokens": 2},
        {"input_tokens": 10, "output_tokens": 2, "prompt_cache_hit_tokens": 11},
        {"input_tokens": 10, "output_tokens": 2, "prompt_cache_hit_tokens": -1},
        {"input_tokens": "10", "output_tokens": 2},
    ],
)
def test_incomplete_or_invalid_usage_remains_unmeasured(usage: dict[str, Any]) -> None:
    assert OpenAICompatibleChatClient._usage({"usage": usage}) == (0, 0, 0, False)


def test_complete_zero_usage_is_a_valid_measurement() -> None:
    assert OpenAICompatibleChatClient._usage(
        {"usage": {"input_tokens": 0, "output_tokens": 0, "prompt_cache_hit_tokens": 0}}
    ) == (0, 0, 0, True)


def test_partial_usage_keeps_the_unknown_reservation_unsettled(credential: str) -> None:
    governor = Mock()
    row_id = uuid4()
    governor.estimate.return_value = (10, 10, Decimal("0.01"))
    governor.reserve.return_value = (
        CeilingDecision("allow", None, None, None, None, True),
        row_id,
    )
    context = CallContext(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        uuid4(),
    )
    body = _responses_body({"ok": True})
    body["usage"] = {"input_tokens": 23}
    completion = OpenAICompatibleChatClient(
        transport=RecordingTransport(body=body)
    ).complete(
        binding=active_release().role("GENERATE_VALUE"),
        system_prompt="s",
        user_prompt="u",
        governor=governor,
        context=context,
    )
    assert completion.input_tokens == 0 and completion.output_tokens == 0
    governor.settle.assert_not_called()


def test_a_timeout_keeps_its_unknown_billing_reservation(credential: str) -> None:
    governor = Mock()
    governor.estimate.return_value = (10, 10, Decimal("0.01"))
    governor.reserve.return_value = (
        CeilingDecision("allow", None, None, None, None, True),
        uuid4(),
    )
    context = CallContext(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        uuid4(),
    )
    client = OpenAICompatibleChatClient(transport=FailingTransport(TimeoutError("slow")))
    with pytest.raises(ProviderTimeout):
        client.complete(
            binding=active_release().role("GENERATE_VALUE"),
            system_prompt="s",
            user_prompt="u",
            governor=governor,
            context=context,
        )
    governor.reserve.assert_called_once()
    governor.settle.assert_not_called()
    governor.mark_failed.assert_not_called()


def test_an_http_error_becomes_a_typed_error(credential: str) -> None:
    client = OpenAICompatibleChatClient(transport=RecordingTransport(status_code=500))
    with pytest.raises(ProviderHttpError) as error:
        client.complete(
            binding=active_release().role("GENERATE_VALUE"), system_prompt="s", user_prompt="u"
        )
    assert error.value.reason_code == "PROVIDER_HTTP_ERROR"


def test_non_json_model_output_is_refused_without_echoing_the_output(credential: str) -> None:
    body = {
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "sorry"}]}
        ]
    }
    client = OpenAICompatibleChatClient(transport=RecordingTransport(body=body))
    with pytest.raises(ProviderMalformedOutput) as error:
        client.complete(
            binding=active_release().role("GENERATE_VALUE"), system_prompt="s", user_prompt="u"
        )
    assert "sorry" not in str(error.value)


def test_malformed_but_measured_output_is_settled_before_it_is_refused(
    credential: str,
) -> None:
    governor = Mock()
    row_id = uuid4()
    governor.estimate.return_value = (10, 10, Decimal("0.01"))
    governor.reserve.return_value = (
        CeilingDecision("allow", None, None, None, None, True),
        row_id,
    )
    context = CallContext(
        UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        uuid4(),
    )
    body = {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "bad"}]}],
        "usage": {"input_tokens": 23, "output_tokens": 5},
    }
    client = OpenAICompatibleChatClient(transport=RecordingTransport(body=body))
    binding = active_release().role("GENERATE_VALUE")
    with pytest.raises(ProviderMalformedOutput):
        client.complete(
            binding=binding,
            system_prompt="s",
            user_prompt="u",
            governor=governor,
            context=context,
        )
    governor.settle.assert_called_once()
    settled = governor.settle.call_args.kwargs
    assert settled["row_id"] == row_id
    assert settled["input_tokens"] == 23
    assert settled["output_tokens"] == 5
    governor.mark_failed.assert_not_called()


def test_the_breaker_opens_after_repeated_failures_and_half_opens_later(
    credential: str,
) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=1000)
    client = OpenAICompatibleChatClient(
        transport=FailingTransport(TimeoutError("slow")), breaker=breaker
    )
    binding = active_release().role("GENERATE_VALUE")
    bucket = f"{binding.provider_id}:{binding.model_id}:{binding.role}"

    for _ in range(2):
        with pytest.raises(ProviderTimeout):
            client.complete(binding=binding, system_prompt="s", user_prompt="u")
    assert breaker.is_open(bucket)

    with pytest.raises(ProviderCircuitOpen):
        client.complete(binding=binding, system_prompt="s", user_prompt="u")

    breaker.cooldown_seconds = 0
    with pytest.raises(ProviderTimeout):
        client.complete(binding=binding, system_prompt="s", user_prompt="u")


def test_model_evidence_carries_no_credential_and_no_prompt(credential: str) -> None:
    transport = RecordingTransport()
    binding = active_release().role("GENERATE_VALUE")
    completion = OpenAICompatibleChatClient(transport=transport).complete(
        binding=binding, system_prompt="secret system prompt", user_prompt="customer said hello"
    )
    evidence = model_evidence(
        binding=binding, completion=completion, prompt_release="m1a-prompt-v1"
    )
    serialised = json.dumps(evidence, ensure_ascii=False)
    assert credential not in serialised
    assert "secret system prompt" not in serialised
    assert "customer said hello" not in serialised
    assert evidence["token_usage"] == {"input": 11, "output": 7}


def test_the_platform_overwrites_identity_fields_the_model_claims(credential: str) -> None:
    """A model may not label its own output as platform-produced."""

    body = _responses_body(
        {
            "origin": "safe_fallback",
            "strategy_id": "propose_order",
            "customer_visible_segments": [{"type": "text", "content_or_ref": "hi"}],
            "proposed_intents": [],
            "confidence": {
                "raw_score": 1.0,
                "calibrated_score": None,
                "assessment_version": "claimed",
                "factor_codes": [],
            },
            "claims": [],
            "return_confirmation": {"included": False, "tone_material_ref": None},
            "safety_markers": ["none"],
            "model_evidence": {
                "provider": "someone-else",
                "model": "claimed-model",
                "prompt_release": "claimed",
                "latency_ms": 0,
                "token_usage": {"input": 0, "output": 0},
            },
        }
    )
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=RecordingTransport(body=body)),
    )
    document = inference.generate(
        plan={
            "selected_strategy_id": "acknowledge",
            "strategy_reason_code": "TEST",
            "generation_constraints": [],
            "candidate_intents": [],
            "required_slot_requests": [],
        },
        understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        # This case is about identity fields, not about materials.
        materials={},
    )
    assert document["origin"] == "model"
    assert document["strategy_id"] == "acknowledge"
    assert document["model_evidence"]["provider"] == "deepseek"
    assert document["model_evidence"]["token_usage"] == {"input": 11, "output": 7}


def test_the_refusal_names_the_rule_that_failed(credential: str) -> None:
    """**接线那一半**：`_validated` 真的把违例清单拼进了那句拒绝。

    `tests/contracts/test_a_contract_violation_says_which_rule.py` 验的是
    渲染器本身（列全部、不带客户原话、上限说清）。**那几条全绿，而这一处
    不接上去的话，线上读到的仍然是那句什么都不说的话** —— 2026-09-22
    运维花一个多小时没缩到某一条规则，缺的就是这一处。
    `AGENTS.md`：新增的东西，**它的每一个消费方都要有一条用例走通**。

    这里走真路径：伪造一个响应体 → `generate` → `_validated` 抛。
    """

    body = _responses_body({"strategy_id": "acknowledge"})
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=RecordingTransport(body=body)),
    )
    with pytest.raises(ProviderMalformedOutput) as refused:
        inference.generate(
            plan={
                "selected_strategy_id": "acknowledge",
                "strategy_reason_code": "TEST",
                "generation_constraints": [],
                "candidate_intents": [],
                "required_slot_requests": [],
            },
            understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
            envelope={"event_type": "customer_message"},
            materials={},
        )

    message = str(refused.value)
    assert "generation-result" in message, message
    # **说得出是哪一条规则。** 这份响应体只有 `strategy_id`，所以必填那一条
    # 一定在里面。断言的是「规则名出现了」，不是「消息变长了」。
    assert "required" in message, (
        f"拒绝消息没说是哪一条规则不过：{message}"
        "\n`_validated` 的违例清单没接上 —— 渲染器绿着不代表线上读得到。"
    )


def test_model_output_failing_its_step_contract_is_refused(credential: str) -> None:
    body = _responses_body({"strategy_id": "acknowledge"})
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=RecordingTransport(body=body)),
    )
    with pytest.raises(ProviderMalformedOutput, match="generation-result"):
        inference.generate(
            plan={
                "selected_strategy_id": "acknowledge",
                "strategy_reason_code": "TEST",
                "generation_constraints": [],
                "candidate_intents": [],
                "required_slot_requests": [],
            },
            understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
            envelope={"event_type": "customer_message"},
            materials={},
        )


# ---------------------------------------------------------------------------
# 请求大小的度量，量的必须是发出去的那个 body（运维报障 2026-09-11）
# ---------------------------------------------------------------------------


def _chinese_payload() -> dict[str, Any]:
    """A body shaped like a real plan request: Chinese prose plus one emoji."""

    return {
        "model": "probe-model",
        "messages": [
            {"role": "system", "content": "You choose the next strategy."},
            {"role": "user", "content": "客户问了价格，先讲损耗再讲能力，别急着报价。🙂"},
        ],
        "stream": False,
    }


def test_the_measured_request_is_the_body_httpx_will_send() -> None:
    """**度量与线路逐字节相同，而且由 httpx 自己的编码器来裁判。**

    `self._client.post(..., json=payload)` 把 dict 交给 httpx，由它构造 body。
    先前这里量的是 `ensure_ascii=True` 的串——**每个中文字符 6 个 ASCII 字节，
    而线路上是 3 个 UTF-8 字节**，于是度量的对象根本不是被发送的那个请求。

    **这条断言拿 httpx 的 `encode_json` 当基准**，不是拿我们自己重写的一份。
    httpx 哪天改了编码（它 0.28 之前用的正是 `ensure_ascii=True`），
    **这条会变红，而不是让那个洞悄悄重开**。
    """

    from fastagent_v2.worker.providers import _WIRE_JSON  # noqa: PLC0415
    from httpx._content import encode_json

    payload = _chinese_payload()
    headers, _ = encode_json(payload)
    ours = json.dumps(payload, **_WIRE_JSON).encode("utf-8")

    assert len(ours) == int(headers["Content-Length"]), (
        "我们度量的字节数与 httpx 将要发送的 body 不一致——"
        "度量必须量被发送的那个请求，否则它拒的和放行的都不是真的"
    )


def test_escaping_no_longer_inflates_a_chinese_request() -> None:
    """**翻倍发生在估算器，不在线路上**——这条把倍数钉下来。

    实测的那次失败是「16,400 字节 / 上限 16,384」，只超 16 字节；而线路上的
    真实 body 大约只有一半。**拒它的是我们自己的检查，供应商没看见这个请求。**
    """

    payload = _chinese_payload()
    from fastagent_v2.worker.providers import _WIRE_JSON  # noqa: PLC0415

    wire = len(json.dumps(payload, **_WIRE_JSON).encode("utf-8"))
    escaped = len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))

    assert escaped > wire, "夹具里没有非 ASCII 字符的话，这条用例证明不了任何事"
    assert wire < escaped * 0.8, (
        f"中文请求的转义膨胀应当显著（实测线路 {wire} / 转义 {escaped}）；"
        "这条同时守着「夹具还带着中文」"
    )


def test_a_chinese_request_that_fits_on_the_wire_is_not_refused(credential: str) -> None:
    """**盯的是调用点，不是那个常量。**

    上面两条钉住 `_WIRE_JSON` 的**定义**；反向探针 C 证明那还不够——
    把调用点改回量转义串，两条照样绿，**洞就悄悄重开了**。
    这一条盯的是行为：一个**线路上装得下、转义后装不下**的中文请求必须被放行。

    这正是运维那次的形状：「16,400 / 16,384」只超 16 字节，而线路上的真实
    body 大约只有一半——**拒它的是这个检查，不是供应商**。
    """

    from fastagent_v2.worker.providers import _WIRE_JSON  # noqa: PLC0415

    # 每个汉字线路上 3 字节、转义后 6 字节，所以「线路装得下、转义装不下」
    # 的区间是 `(上限/6, 上限/3)` 个字 —— 取**这个区间的中点**，而不是
    # `MAX // 4`（独立审查 2026-09-11，P3-1）。
    #
    # `MAX // 4` 贴边：实测整包 15,619 / 16,384，**只余 765 字节（4.7%）**。
    # `generation-result` 的 schema 或 generate binding 再长 765 字节，
    # 这条用例就会**因为与它要守的东西无关的原因**变红，
    # 而它自己那条护栏断言只量 `payload_ish` 那一段、量不到整包，
    # **到时候的报错会指向错的方向**。
    #
    # 取中点之后，两边各有约 33% 的余量。
    low, high = MAX_MODEL_INPUT_BYTES // 6, MAX_MODEL_INPUT_BYTES // 3
    prompt = "价" * ((low + high) // 2)
    payload_ish = {"messages": [{"role": "user", "content": prompt}]}
    wire = len(json.dumps(payload_ish, **_WIRE_JSON).encode("utf-8"))
    escaped = len(json.dumps(payload_ish, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    assert wire < MAX_MODEL_INPUT_BYTES < escaped, (
        "夹具要落在「线路上装得下、转义后装不下」那个区间里，否则它证明不了任何事；"
        f"实测线路 {wire} / 转义 {escaped} / 上限 {MAX_MODEL_INPUT_BYTES}"
    )

    transport = RecordingTransport()
    client = OpenAICompatibleChatClient(transport=transport)
    client.complete(
        binding=active_release().role("GENERATE_VALUE"),
        system_prompt="system",
        user_prompt=prompt,
    )

    assert transport.calls, "这个请求线路上装得下，不该在到达 transport 之前就被拒"
