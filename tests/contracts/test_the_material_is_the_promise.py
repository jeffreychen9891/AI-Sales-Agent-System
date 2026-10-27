"""那份素材的正文就是那句承诺，桥后没有路（运维缺陷 21 + 22，2026-09-23/24）。

## 缺陷 21 的现场

模型规规矩矩引了商家原文（`material_ref` 段），兑现闸也正确放行
（「发出来就算兑现」，用户 2026-09-18 裁定）—— 而那份素材的**全部内容**是：

    您说的这件，正好是它接得住的那一类。我说说它具体怎么接，您看对不对得上。

**它把一句「我这就讲」当成了「讲」。** 讲解是 93 秒后被定时器救回来的。

用户随后指示**全部素材重新审核**：29 份逐份过，25 份通过、4 份修
（bridge 补真讲解、两份价格素材的「这个数/那个月费」指代自含化、
close-negative 删「我记一下」）。用户裁定**不建封版闸**，只修内容 ——
所以本文件钉的是 demo 这一份的现状，不是一条通用规则。

## 缺陷 22

`evidence_span_hash` 要求模型对证据片段报 SHA-256 —— **LLM 结构上算不了哈希**，
上线以来每个值都是编造的 hex（T41 早记过它逐字抄示例值），
63 位那次的代价是整份 understanding 作废、客户零收到。
平台不验、`facts` 零消费方、文档不持久化 —— 三层全空。**字段整个删掉**（用户裁定）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.runtime.contracts import validate_inference_contract
from fastagent_v2.worker.inference_model import _UNDERSTAND_SYSTEM
from fastagent_v2.worker.inference_validation import _PROMISE_TO_DELIVER

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _materials() -> dict[str, str]:
    return json.loads(
        (REPOSITORY_ROOT / "profiles/internal-demo-sales/materials.json").read_text(
            encoding="utf-8"
        )
    )["materials"]


# ---------------------------------------------------------------------------
# 缺陷 21：桥后有路
# ---------------------------------------------------------------------------


def test_the_bridge_material_carries_content_beyond_its_promise() -> None:
    """**去掉命中承诺模式的句子之后，正文还剩下实质内容。**

    这是运维给的可判形状（用户裁定不做成封版闸，所以只钉 demo 这一份）：
    旧文 36 字里唯一的实质就是那句「我说说」——去掉承诺句剩不下东西。
    新文把「怎么接」写实（即回 / 上下文保留 / 到点跟进 / 审过的话），
    承诺句去掉后仍然是一段完整的讲解。
    """

    text = _materials()["material:demo-bridge-to-capability:v1"]
    sentences = [s for s in re.split(r"[。？！；]", text) if s.strip()]
    substantive = [s for s in sentences if not _PROMISE_TO_DELIVER.search(s)]

    assert sum(len(s) for s in substantive) >= 50, (
        f"bridge 素材去掉承诺句后只剩 {sum(len(s) for s in substantive)} 字 —— "
        "它又变回「我这就讲」了（运维缺陷 21）"
    )


def test_the_price_materials_are_self_contained() -> None:
    """**「这个数 / 那个月费」不许指一个正文里没有的数。**

    这两份是 `on_request`「花钱」类，**可以被单独引用**（客户问「贵不贵」，
    模型只挑其中一份）——那一刻指代悬空，缺陷 18 的症状就写在素材正文里。
    自含判据：谈价格口径的素材，正文里要有那个价。
    """

    materials = _materials()
    for ref in ("material:demo-price-flat:v1", "material:demo-price-metered-cost:v1"):
        assert "1888" in materials[ref], (
            f"{ref} 的正文里没有价格数字 —— 单独被引时「这个数」指不到任何东西"
        )
        assert "这个数不" not in materials[ref] and "那个月费" not in materials[ref], (
            f"{ref} 又用回了悬空指代"
        )


def test_the_negative_close_does_not_open_with_a_receipt() -> None:
    """「我记一下」是配包自己禁的回执族边缘 —— 商家原文不该示范它。"""

    assert "我记一下" not in _materials()["material:demo-func-close-negative:v1"]


def test_the_promised_checklist_actually_exists() -> None:
    """**landing-next 许的「对接清单」有对应物** —— 审核时排除的疑似，钉住结论。

    `我把对接清单发您` 会被承诺闸抓住并要求同轮兑现；兑现物
    `asset:onboarding-implementation-checklist:v1` 必须与它同帧声明，
    否则那句许诺每次说出口都是一个兑现不了的承诺（bridge 的同族）。
    """

    profile = json.loads(
        (REPOSITORY_ROOT / "profiles/internal-demo-sales/profile.json").read_text(
            encoding="utf-8"
        )
    )
    trial = next(f for f in profile["target_frames"] if f["id"] == "trial_close")
    refs = {a["ref"] for a in trial.get("content_assets") or []}

    assert "material:demo-landing-next:v1" in {
        (m["ref"] if isinstance(m, dict) else m) for m in trial["content_materials"]
    }
    assert "asset:onboarding-implementation-checklist:v1" in refs, (
        "landing-next 许的「对接清单」在本帧没有兑现物 —— 桥后没路的同族"
    )


# ---------------------------------------------------------------------------
# 缺陷 22：evidence_span_hash 删净
# ---------------------------------------------------------------------------


def _understanding(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_id": "22222222-2222-4222-8222-222222222222",
        "facts": [fact],
        "intents": [],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "他问了一件事。",
        "model_evidence": {
            "provider": "mock",
            "model": "test",
            "prompt_release": "m1a-prompt-v17",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def test_a_fact_without_the_hash_is_valid_now() -> None:
    """**三键就够** —— 修复前这一份会因缺 `evidence_span_hash` 整轮作废。"""

    validate_inference_contract(
        "understanding-result",
        _understanding({"key": "team_size", "typed_value": "2", "confidence": 0.9}),
    )


def test_a_fact_carrying_the_hash_is_refused() -> None:
    """**字段回不来** —— `additionalProperties: false` 兜住。

    没有这一条，哪天有人把它加回 schema 而不解决「LLM 算不了哈希」，
    63 位杀整轮的形状就会原样复发。
    """

    with pytest.raises(Exception, match="facts"):
        validate_inference_contract(
            "understanding-result",
            _understanding({
                "key": "team_size",
                "typed_value": "2",
                "confidence": 0.9,
                "evidence_span_hash": "a" * 64,
            }),
        )


def test_the_prompt_no_longer_asks_for_a_hash() -> None:
    """提示词不再教模型编 64 位 hex —— 示例与规则句一并删净。"""

    assert "evidence_span_hash" not in _UNDERSTAND_SYSTEM
    assert "hexadecimal" not in _UNDERSTAND_SYSTEM
