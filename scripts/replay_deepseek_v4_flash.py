"""Run the release-gate replay against DeepSeek V4 Flash.

The script emits aggregate evidence only: credentials, prompts and model output
are never printed.  Set FASTAGENT_MODEL_API_KEY_DEEPSEEK in the process
environment before running it.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from fastagent_v2.runtime.contracts import model_output_schema, validate_inference_contract
from fastagent_v2.worker.inference_model import (
    _GENERATE_SYSTEM,
    _PLAN_SYSTEM,
    _UNDERSTAND_SYSTEM,
    PROMPT_RELEASE,
)
from fastagent_v2.worker.providers import ProviderMalformedOutput, parse_model_json

ROOT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((ROOT / "profiles/synthetic-third/profile.json").read_text(encoding="utf-8"))
MODEL = "deepseek-v4-flash"

GOLDEN = json.loads(
    (ROOT / "packages/contracts/inference/golden/v4-replay-cases.json").read_text(encoding="utf-8")
)
CASES = tuple((item["event_type"], item["text"]) for item in GOLDEN["cases"])


def _evidence(latency_ms: int, usage: dict[str, Any]) -> dict[str, Any]:
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    return {
        "provider": "deepseek",
        "model": MODEL,
        "prompt_release": PROMPT_RELEASE,
        "latency_ms": latency_ms,
        "token_usage": {
            "input": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            "cached_input": int(
                usage.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0
            ),
        },
    }


def _call(
    client: httpx.Client,
    key: str,
    *,
    role: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    contract = {
        "UNDERSTAND_VALUE": "understanding-result",
        "PLAN_CAPABILITY": "plan-result",
        "GENERATE_VALUE": "generation-result",
        "COMMITMENT_CAPABILITY": "plan-result",
    }[role]
    started = time.monotonic()
    response = client.post(
        "https://api.deepseek.com/v1/responses",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": MODEL,
            "instructions": system,
            "input": user,
            "stream": False,
            "reasoning": {"effort": "none"},
            "temperature": temperature,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": role.lower(),
                    "schema": model_output_schema(contract),
                }
            },
            "max_output_tokens": max_tokens,
        },
        timeout=35,
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    response.raise_for_status()
    body = response.json()
    usage = body.get("usage") or {}
    raw = next(
        block["text"]
        for item in body.get("output") or []
        if item.get("type") == "message"
        for block in item.get("content") or []
        if block.get("type") == "output_text"
    )
    completed = body.get("status") == "completed"
    meta = {
        "role": role,
        "latency_ms": latency_ms,
        "finish_reason": "stop" if completed else "length",
        "response_model": body.get("model"),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cached_input_tokens": _evidence(latency_ms, usage)["token_usage"]["cached_input"],
    }
    try:
        content = parse_model_json(raw)
    except ProviderMalformedOutput:
        content = {}
        meta["parse_error"] = "invalid_json_object"
    return content, meta


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in ("UNDERSTAND_VALUE", "PLAN_CAPABILITY", "GENERATE_VALUE", "COMMITMENT_CAPABILITY"):
        selected = [row for row in rows if row["role"] == role]
        result[role] = {
            "samples": len(selected),
            "structured_valid": sum(bool(row.get("contract_valid")) for row in selected),
            "finish_reasons": sorted({str(row["finish_reason"]) for row in selected}),
            "truncated": sum(row["finish_reason"] == "length" for row in selected),
            "latency_ms": {
                "p50": round(statistics.median(row["latency_ms"] for row in selected)),
                "p95": _percentile([row["latency_ms"] for row in selected], 0.95),
                "max": max(row["latency_ms"] for row in selected),
            },
            "input_tokens": {
                "p50": round(statistics.median(row["input_tokens"] for row in selected)),
                "p95": _percentile([row["input_tokens"] for row in selected], 0.95),
            },
            "output_tokens": {
                "p50": round(statistics.median(row["output_tokens"] for row in selected)),
                "p95": _percentile([row["output_tokens"] for row in selected], 0.95),
            },
            "cached_input_tokens": sum(row["cached_input_tokens"] for row in selected),
            "response_models": sorted({str(row["response_model"]) for row in selected}),
            "contract_errors": dict(
                Counter(str(row["contract_error"]) for row in selected if row.get("contract_error"))
            ),
        }
    return result


def _validate(contract: str, document: dict[str, Any], meta: dict[str, Any]) -> bool:
    """Record contract validity without emitting customer/model content."""

    if meta.get("parse_error"):
        meta["contract_valid"] = False
        meta["contract_error"] = str(meta["parse_error"])
        return False
    try:
        validate_inference_contract(contract, document)
    except Exception as error:  # aggregate evidence intentionally omits raw values
        meta["contract_valid"] = False
        validator = getattr(error, "validator", type(error).__name__)
        path = ".".join(str(item) for item in getattr(error, "absolute_path", ()))
        meta["contract_error"] = f"{validator}@{path or '<root>'}"
        return False
    meta["contract_valid"] = True
    return True


def main() -> None:
    key = os.environ.get("FASTAGENT_MODEL_API_KEY_DEEPSEEK", "").strip()
    if not key:
        raise SystemExit("FASTAGENT_MODEL_API_KEY_DEEPSEEK is required")
    if GOLDEN["prompt_release"] != PROMPT_RELEASE:
        raise SystemExit("golden replay cases are stale for the current prompt release")

    rows: list[dict[str, Any]] = []
    with httpx.Client() as client:
        for event_type, text in CASES:
            event_id = str(uuid4())
            aggregate = {
                "target_stack": [
                    {
                        "definition_id": "welcome",
                        "goal": "Understand why the customer contacted the service desk",
                        "status": "active",
                        "slots": {},
                    }
                ]
            }
            envelope = {"event_id": event_id, "event_type": event_type, "content": {"text": text}}
            understand_user = json.dumps(
                {
                    "event_type": event_type,
                    "content": envelope["content"],
                    "current_goal": aggregate["target_stack"][-1]["goal"],
                    "known_slots": {},
                },
                ensure_ascii=False,
            )
            understanding, meta = _call(
                client,
                key,
                role="UNDERSTAND_VALUE",
                system=_UNDERSTAND_SYSTEM,
                user=understand_user,
                max_tokens=1024,
                temperature=0.2,
            )
            understanding.update(
                schema_version="1.0.0",
                event_id=event_id,
                model_evidence=_evidence(
                    meta["latency_ms"],
                    {
                        "prompt_tokens": meta["input_tokens"],
                        "completion_tokens": meta["output_tokens"],
                        "prompt_cache_hit_tokens": meta["cached_input_tokens"],
                    },
                ),
            )
            understanding_valid = _validate("understanding-result", understanding, meta)
            rows.append(meta)

            understanding_for_plan = (
                understanding
                if understanding_valid
                else {
                    "schema_version": "1.0.0",
                    "event_id": event_id,
                    "facts": [],
                    "intents": [{"intent_label": "unclassified", "confidence": 1.0}],
                    "slot_updates": [],
                    "interruption": {
                        "relation": "same_goal",
                        "suggested_frame_definition_id": None,
                    },
                    "sentiment": "neutral",
                    "risk_markers": ["none"],
                    "customer_commitments": [],
                    "uncertainties": [],
                    "model_evidence": _evidence(0, {}),
                }
            )

            definition = PROFILE["target_frames"][0]
            allowed = definition["allowed_strategy_set"]
            plan_user = json.dumps(
                {
                    "understanding": understanding_for_plan,
                    "current_frame": {
                        "goal": aggregate["target_stack"][-1]["goal"],
                        "status": "active",
                        "slots": {},
                        "required_slots": ["request_summary"],
                        "exit_conditions": definition["exit_conditions"],
                    },
                    "allowed_strategies": allowed,
                    "allowed_intent_kinds_by_strategy": {
                        name: PROFILE["strategies"][name]["allowed_intent_kinds"]
                        for name in allowed
                        if name in PROFILE["strategies"]
                    },
                    # **The same derivation production uses** (independent
                    # review 2026-09-10, P2-2). This script builds its own plan
                    # prompt while importing the production `_PLAN_SYSTEM`, so
                    # when the system prompt gained "choose only from
                    # allowed_generation_constraints_by_strategy" and this
                    # payload did not, the model was pointed at a key that was
                    # not there -- and `_evidence()` stamped the result
                    # `prompt_release: m1a-prompt-v4` anyway. That is evidence
                    # taken from an environment that differs from the real one,
                    # which is the failure this repository keeps naming.
                    #
                    # `test_replay_script_prompt_matches_production` keeps the
                    # two key sets equal, the way `PROMPT_RELEASE` and the
                    # golden are kept equal.
                    "allowed_generation_constraints_by_strategy": {
                        name: PROFILE["strategies"][name]["generation_constraints"]
                        for name in allowed
                        if name in PROFILE["strategies"]
                    },
                    # 与上面同一条理由：生产的 plan 提示词加了这个键
                    # （裁定一步骤 3 的硬前置），而本脚本与生产**逐键相同**是
                    # `test_replay_script_prompt_matches_production` 钉着的。
                    "required_slots_by_strategy": {
                        name: PROFILE["strategies"][name].get("required_slots", [])
                        for name in allowed
                        if name in PROFILE["strategies"]
                    },
                    "allowed_event_kinds": ["silence_check"],
                    "stack_depth": 1,
                    "frames_entered": [],
                    # **B1-2 / B1-1（2026-09-13）**：生产的 plan 提示词多了这两键，
                    # 而 `test_replay_script_prompt_matches_production.py` 要求这份重放
                    # 与生产**逐键相同** —— 少一个键，重放出来的就不是生产那个请求。
                    # 这里给的是「没有悬着的问题、没有已识别的焦点」那一档，
                    # 也就是一通新对话的第一轮。
                    "pending_question": None,
                    # 与生产同形，由
                    # `test_the_replay_script_sends_the_same_plan_prompt_keys_as_production`
                    # 钉着：
                    # 回放要是少一个键，回放出来的就不是真实那一轮的请求。
                    "content_assets": [],
                    # 同上：回放少一个键，回放出来的就不是真实那一轮的请求。
                    "last_outbound": "",
                    # **这通对话聊过什么**（T200，2026-09-20）。回放脚本与
                    # 生产同形状，而这条正是那条用例守的东西：
                    # 脚本漂了，回放量到的字节就不是生产那一份。
                    "conversation_summary": [],
                    "summary_lines_not_shown": 0,
                    # **记忆那两个键，09-18 加（E4-5）** —— 回放要与生产同形，
                    # 否则模型回答的是一个略有不同的问题，而结果按生产那串归档。
                    "known_memories": [],
                    "memories_not_shown": 0,
                    # 同上：回放少一个键，回放出来的就不是真实那一轮的请求。
                    "materials_already_sent": [],
                    # **T367 那个键，09-19 加（批 J）**：只有「许了没兑现」的重跑
                    # 那一次非空。回放的是**第一次尝试**，所以这里是空的 ——
                    # 而空着也要在，否则回放少一个键。
                    "unkept_promises": [],
                    # ADR-0149：主动轮次那一步的判断。回放的是客户消息那一路，
                    # 所以这里恒空 —— 而**键必须在**，否则脚本问的问题与生产
                    # 不是同一个，而结果却会按生产那个 release 串归档。
                    "silence_reading": {},
                    "focus": {},
                },
                ensure_ascii=False,
            )
            plan, meta = _call(
                client,
                key,
                role="PLAN_CAPABILITY",
                system=_PLAN_SYSTEM,
                user=plan_user,
                max_tokens=2048,
                temperature=0.0,
            )
            plan.update(
                schema_version="1.0.0",
                origin="model",
                model_evidence=_evidence(
                    meta["latency_ms"],
                    {
                        "prompt_tokens": meta["input_tokens"],
                        "completion_tokens": meta["output_tokens"],
                        "prompt_cache_hit_tokens": meta["cached_input_tokens"],
                    },
                ),
            )
            plan_valid = _validate("plan-result", plan, meta)
            rows.append(meta)

            commitment, meta = _call(
                client,
                key,
                role="COMMITMENT_CAPABILITY",
                system=_PLAN_SYSTEM,
                user=plan_user,
                max_tokens=768,
                temperature=0.0,
            )
            commitment.update(
                schema_version="1.0.0",
                origin="model",
                model_evidence=_evidence(
                    meta["latency_ms"],
                    {
                        "prompt_tokens": meta["input_tokens"],
                        "completion_tokens": meta["output_tokens"],
                        "prompt_cache_hit_tokens": meta["cached_input_tokens"],
                    },
                ),
            )
            _validate("plan-result", commitment, meta)
            rows.append(meta)

            plan_for_generation = (
                plan
                if plan_valid
                else {
                    "selected_strategy_id": "acknowledge",
                    "strategy_reason_code": "REPLAY_SAFE_FIXTURE",
                    "generation_constraints": ["no_new_commitment"],
                    "candidate_intents": [
                        {
                            "kind": "send_message",
                            "typed_payload": {},
                            "idempotency_basis": f"replay:{event_id}",
                        }
                    ],
                    "required_slot_requests": [],
                }
            )

            generate_user = json.dumps(
                {
                    "strategy_id": plan_for_generation["selected_strategy_id"],
                    "strategy_reason_code": plan_for_generation["strategy_reason_code"],
                    "generation_constraints": plan_for_generation["generation_constraints"],
                    "candidate_intents": plan_for_generation["candidate_intents"],
                    "required_slot_requests": plan_for_generation["required_slot_requests"],
                    "understanding": understanding_for_plan,
                    "event_type": event_type,
                    # ⚠️ **这四个键是 2026-09-18（批 E）补的，而前三个早就漂了。**
                    #
                    # 键对齐那条守卫先前只盯**计划步**，生成步这份镜像
                    # 因此悄悄少了 `content_materials` / `knowledge_chunks` /
                    # `content_assets` —— 回放出来的不是真实那一轮的请求，
                    # 而**回放的结果按生产那串 prompt_release 归档**。
                    # 本批把守卫扩到生成步，四个键一起补上。
                    "content_materials": [],
                    "knowledge_chunks": [],
                    "content_assets": [],
                    "last_outbound": "",
                    # **这通对话聊过什么**（T200，2026-09-20）。回放脚本与
                    # 生产同形状，而这条正是那条用例守的东西：
                    # 脚本漂了，回放量到的字节就不是生产那一份。
                    "conversation_summary": [],
                    "summary_lines_not_shown": 0,
                    "materials_already_sent": [],
                    # **记住的那几件事，09-19 加（T363）** —— 同上：
                    # 回放少一个键，回放出来的就不是真实那一轮的请求。
                    "remembered_facts": [],
                    # **这个商家允许记哪几件事**（2026-09-21）——
                    # 生产那一侧由 `request_shape.generate_prompt` 发，重放要跟上。
                    "rememberable_fields": [],
                    "memories_not_shown": 0,
                    # **挂着没答的那个问题**（运维缺陷 19，2026-09-23）——
                    # 生产那一侧由 `request_shape.generate_prompt` 发，重放要跟上。
                    "pending_question": None,
                },
                ensure_ascii=False,
            )
            generated, meta = _call(
                client,
                key,
                role="GENERATE_VALUE",
                system=_GENERATE_SYSTEM,
                user=generate_user,
                max_tokens=1024,
                temperature=0.7,
            )
            generated.update(
                schema_version="1.0.0",
                origin="model",
                strategy_id=plan_for_generation["selected_strategy_id"],
                model_evidence=_evidence(
                    meta["latency_ms"],
                    {
                        "prompt_tokens": meta["input_tokens"],
                        "completion_tokens": meta["output_tokens"],
                        "prompt_cache_hit_tokens": meta["cached_input_tokens"],
                    },
                ),
            )
            _validate("generation-result", generated, meta)
            rows.append(meta)

    print(json.dumps(_summary(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
