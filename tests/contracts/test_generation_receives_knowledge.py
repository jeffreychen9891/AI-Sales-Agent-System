"""生成那一步看得见这一轮检出的知识库，**而它只能用自己的话说**。

**这一份钉的是三件不同的事**，混在一起说会把其中两件读成同一件：

1. **命中真的到了模型面前**（`ref` 与 `text` 一起，而键**空着也发**）。
   与素材那一侧同一条理由：「这一轮没有可用的资料」与「没人告诉你有资料这回事」
   不该在模型眼里长得一样。
2. **引用必须是本轮给过的**。`GENERATION_KNOWLEDGE_REF_UNKNOWN` 补的是
   `evidence_required` 一直没检的那一半 —— 那条约束只检 `source_evidence_refs`
   **非空**，从不检里面是什么。
3. **知识库不能被逐字引**，而这件事**不靠提示词措辞维持**：
   `material_ref` 段的 ref 必须在这一轮的 `materials` 里，知识库的 ref 不在那张表里，
   所以那条路本来就走不通（见 `test_a_knowledge_ref_cannot_be_quoted_as_a_material`）。
   提示词里每个字节都是**每次 generate 都付**的，而预算表今天剩 0。

**今天不成立的那一件，写在这里而不是留白**：模型把一条知识库正文**逐字抄进
`text` 段**、同时不引它的 ref —— 这一份**检不出来**
（`test_a_copied_chunk_without_a_citation_is_not_detected_today`），
与素材那一侧的 T268 同源。检出它要判「这段话是不是那段话的抄写」，
那是一次语义判断，平台做不了。
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest
from fastagent_v2.profiles.registry import MAX_MODEL_INPUT_BYTES
from fastagent_v2.runtime.knowledge_bound import KNOWLEDGE_PROMPT_BYTES, bound_for_prompt
from fastagent_v2.worker.channel import MATERIAL_REF_SEGMENT_TYPE
from fastagent_v2.worker.inference_model import ModelBackedInference
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
)
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import (
    _WIRE_JSON,
    OpenAICompatibleChatClient,
    TransportResponse,
)

CREDENTIAL_ENV = "FASTAGENT_MODEL_API_KEY_DEEPSEEK"

SHIPPING = "knowledge:shipping-policy:v1"
RETURNS = "knowledge:returns-window:v1"
KNOWLEDGE: dict[str, str] = {
    SHIPPING: "单笔实付满 99 元包邮，不满 99 元运费 8 元。",
    RETURNS: "签收后 7 天内可退，需商品完好、吊牌未剪。",
}

MATERIAL_REF = "material:demo-opening:v1"
MATERIAL_TEXT = "您好，这里是示例商家的客服助手。"


class _RecordingTransport:
    """Test double. It never opens a socket."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> TransportResponse:
        self.calls.append({"url": url, "payload": payload})
        return TransportResponse(status_code=200, body=self.body)


def _acknowledgement() -> dict[str, Any]:
    return {
        "strategy_id": "acknowledge",
        "customer_visible_segments": [{"type": "text", "content_or_ref": "满 99 元就包邮。"}],
        "proposed_intents": [],
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "claims": [],
        "return_confirmation": {"included": False, "tone_material_ref": None},
        "safety_markers": ["none"],
    }


def _model_reply(content: dict[str, Any]) -> dict[str, Any]:
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


def _plan_document() -> dict[str, Any]:
    return {
        "selected_strategy_id": "acknowledge",
        "strategy_reason_code": "TEST",
        "generation_constraints": [],
        "candidate_intents": [],
        "required_slot_requests": [],
    }


def _generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    knowledge: dict[str, str],
    materials: dict[str, str] | None = None,
    understanding: dict[str, Any] | None = None,
) -> _RecordingTransport:
    """跑一次真实的 `generate`，把发出去的请求留下来。

    **素材这一侧这里直接给映射，不走 `select_materials`** —— 那条缝有它自己的
    一份用例（`test_generation_receives_materials.py`）在看着，
    本文件问的是「知识库这个键长什么样」。
    """

    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    transport = _RecordingTransport(_model_reply(_acknowledgement()))
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    )
    inference.generate(
        plan=_plan_document(),
        understanding=understanding or {"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        materials=materials or {},
        knowledge=knowledge,
    )
    return transport


def _sent_prompt(transport: _RecordingTransport) -> dict[str, Any]:
    """模型收到的那份 `user_prompt`，**解析回它本来的对象**。

    不是在序列化后的请求里搜子串：提示词自己就是一份 JSON，
    一段带换行的正文在里面长的是 `\\n` —— 于是「短正文过、长正文不过」，
    量的就成了序列化器而不是提示词（素材那一份的同名 helper 记着这次踩坑）。
    """

    assert transport.calls, "the provider was never called"
    return dict(json.loads(str(transport.calls[0]["payload"]["input"])))


def _sent_knowledge(transport: _RecordingTransport) -> dict[str, str]:
    prompt = _sent_prompt(transport)
    assert "knowledge_chunks" in prompt, (
        "空着也要有这个键：「这一轮没有可用的资料」与「没人告诉你有资料这回事」"
        "不该在模型眼里长得一样"
    )
    return {item["ref"]: item["text"] for item in prompt["knowledge_chunks"]}


def _validated_plan(*, constraints: list[str] | None = None) -> ValidatedPlan:
    return ValidatedPlan(
        document={
            **_plan_document(),
            "generation_constraints": constraints or [],
            "origin": "model",
            "candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}],
        },
        frame={"id": "frame-1", "definition_id": "introduce"},
        definition={
            "id": "introduce",
            "goal": "answer the question",
            "content_materials": [MATERIAL_REF],
            "allowed_strategy_set": ["acknowledge"],
            "information_slots": [],
            "exit_conditions": [],
            "time_rules": [],
        },
    )


def _generation(
    *,
    segments: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一份 `validate_generation` 眼里的生成结果 —— **带上平台盖的那三个字段**。

    `_acknowledgement` 是**模型**返回的那一半，下面三项是
    `ModelBackedInference` 从实测事实写上去的。只校验模型那一半，
    校验的就是一份现实里不存在的文档。
    """

    document = _acknowledgement()
    if segments is not None:
        document["customer_visible_segments"] = segments
    document["claims"] = claims or []
    document["schema_version"] = "1.0.0"
    document["origin"] = "model"
    document["model_evidence"] = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "prompt_release": "m1a-prompt-v8",
        "latency_ms": 1,
        "token_usage": {"input": 11, "output": 7},
    }
    return document


def _claim(*refs: str, text: str = "满 99 元包邮。") -> dict[str, Any]:
    """一条事实声明。**契约里存的是正文的 sha256，不是正文本身。**

    这一点顺带解释了上面那条「抄了不引检不出来」为什么是边界而不是懒：
    `claims` 这一侧平台手上只有一个哈希，**连比对的原料都没有**。
    """

    return {
        "text_hash": sha256(text.encode("utf-8")).hexdigest(),
        "source_evidence_refs": list(refs),
    }


# ---------------------------------------------------------------------------
# 1. 命中到了模型面前
# ---------------------------------------------------------------------------


def test_the_prompt_carries_this_turns_chunks_ref_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**本批存在的理由那条测量。**

    两半都要：只给 `text`，模型无从引用（`source_evidence_refs` 里写什么？）；
    只给 `ref`，那是一个它读不了的名字。**引得动才叫可追溯，否则只是像有出处。**
    """

    transport = _generate(monkeypatch, knowledge=dict(KNOWLEDGE))

    assert _sent_knowledge(transport) == KNOWLEDGE


def test_the_key_is_there_even_when_this_turn_retrieved_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检索没命中、或者这一轮压根没请求检索 —— **键还在，是空的**。

    这不是洁癖：一个**缺键**的提示词在模型看来是「这个系统没有知识库这回事」，
    而一个**空键**是「这一轮没有可用的资料」。前者会让它更敢自己编
    （没有任何东西暗示「资料本该从外面来」），后者正好相反。
    """

    transport = _generate(monkeypatch, knowledge={})

    assert _sent_knowledge(transport) == {}


def test_knowledge_and_materials_are_two_keys_not_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两种来源**两个键**（裁定四 / ADR-0135），而它们的规则相反。

    素材**按 ref 逐字引**（平台在发送路径上把 ref 换成商家审过的字），
    知识库**用模型自己的话写**。混进一个数组，
    「哪一边可以逐字引」就回到靠提示词措辞维持 —— 而那不是执行。
    """

    transport = _generate(
        monkeypatch, knowledge=dict(KNOWLEDGE), materials={MATERIAL_REF: MATERIAL_TEXT}
    )
    prompt = _sent_prompt(transport)

    assert [item["ref"] for item in prompt["content_materials"]] == [MATERIAL_REF]
    assert sorted(item["ref"] for item in prompt["knowledge_chunks"]) == sorted(KNOWLEDGE)


def test_nothing_but_ref_and_text_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提示词里一条 chunk 只有两个键（ADR-0135 第 1 条）。

    `bound_for_prompt` 已经挡过 `score` / `source_spans` / `chunk_id`，
    **而这一条是从提示词那一端问同一个问题** —— 上界那一层被绕过（有人换了个
    调用路径、或者直接给 `generate` 传原始命中）时，红的是这一条。
    """

    transport = _generate(monkeypatch, knowledge=dict(KNOWLEDGE))

    for item in _sent_prompt(transport)["knowledge_chunks"]:
        assert sorted(item) == ["ref", "text"], f"多带了字段：{sorted(item)}"


def test_the_generate_prompt_has_exactly_these_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**这份键集是两处字节预算用例的镜像所依赖的东西。**

    `test_prompt_bytes.py` 的基线那条与 `test_knowledge_bound.py` 的预算表那条
    各自手写了一份 `user_prompt` 的形状去量「空请求有多大」。
    **镜像一旦跟生产走样，两条都会安静地量错**（6c 就踩过：`generate` 多了
    `knowledge_chunks` 而镜像没有，基线小了 26 字节，方向是 fail-open）。

    所以这一条对着**真实调用**数键：键集变了它先红，
    而红的时候要去改的是上面那两份镜像。
    """

    transport = _generate(monkeypatch, knowledge={})

    assert sorted(_sent_prompt(transport)) == [
        "candidate_intents",
        # **第三个内容键，09-16（E3-4）加**：文件与话术、知识库并列而不合并 ——
        # 三者模型该做的事不同（引 / 写 / 提一个 send_material）。
        "content_assets",
        "content_materials",
        # **这通对话聊过什么**（T200，2026-09-20）—— 生成步据它别把
        # 已经说过的话再说一遍（运维 §八：同一段逐字说两遍算缺陷）。
        "conversation_summary",
        "event_type",
        "generation_constraints",
        "knowledge_chunks",
        # **上一轮我们说的那句话，09-18 加（批 E）**：写跟进那一句的
        # 正是这一步，而它在此之前**看不见上一轮说了什么** ——
        # 于是运维 §11 那一轮发出去的是「上次聊的那件事」。
        "last_outbound",
        # **已经讲过哪几段，09-18 加（批 E）**：跟进那一句的锚点是它 ——
        # 用户当场更正「**上一句不一定是关键，之前的内容才是**」。
        "materials_already_sent",
        # **记住的那几件事，09-19 加（T363）**：理解步与计划步早就收着记忆，
        # 而**写字那一步一直看不见** —— 于是「那条回复引了哪几条记忆」
        # 在结构上答不出来：引用集要有人去引。
        # ⚠️ 它是 `remembered_facts`（`{ref, value}`）而不是那两步的
        # `known_memories`（`{field, value}`）——
        # **同名而不同形状正是 `CLAUDE.md` 第三节点名的那个陷阱。**
        "memories_not_shown",
        # **挂着没答的那个问题**（运维缺陷 19，2026-09-23）——
        # 写字的手要看得见它才拦得住「把刚被答过的问题又问一遍」。
        "pending_question",
        # **这个商家允许记哪几件事**（2026-09-21）——
        # 与 `remembered_facts` 是两回事：那一个是**已经知道的**，
        # 这一个是**允许记的**。运维 2026-09-20 实测模型猜了一个
        # 没人声明过的键，正是因为它只看得见前者。
        "rememberable_fields",
        "remembered_facts",
        "required_slot_requests",
        "strategy_id",
        "strategy_reason_code",
        # 装不下的行数，与 `memories_not_shown` 成对、同一条理由。
        "summary_lines_not_shown",
        "understanding",
    ]


def test_a_worst_case_turn_still_fits_the_model_input_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**三种来源同时顶格的一个回合，请求仍然装得进 16,384。**

    这一条是 ADR-0132 §八 那张表的**端到端**那一侧：上面那些用例分别钉住
    每一项的额度，而只有这一条把它们加在一起、**用真实请求的字节数**去比。
    量的是 `transport` 收到的那个 payload —— 生产发出去的就是它。

    素材取表里那一行（3,804，演示包最紧的那个回合），
    understanding 给一份超量的让 `_bounded_understanding` 压到它的 1,580，
    知识库给契约允许的最坏两条。
    """

    from fastagent_v2.runtime.knowledge_bound import wire_bytes
    from fastagent_v2.runtime.understanding_bound import UNDERSTANDING_PROMPT_BYTES
    from fastagent_v2.runtime.understanding_bound import wire_bytes as understanding_wire_bytes

    worst_chunks = {
        f"knowledge:worst-{index}:v1": "运" * 287 for index in range(2)
    }
    assert wire_bytes(
        [{"ref": ref, "text": text} for ref, text in worst_chunks.items()]
    ) <= KNOWLEDGE_PROMPT_BYTES, "这一条要顶格但不许超额度 —— 超了就是在测另一件事"

    materials: dict[str, str] = {}
    while True:
        ref = f"material:filler-{len(materials)}:v1"
        text = "示例话术，商家审过的一段。" * 8
        if wire_bytes(
            [{"ref": r, "text": t} for r, t in {**materials, ref: text}.items()]
        ) > 3804:
            break
        materials[ref] = text

    # **顶格要真的顶格**：一个填不满的回合会让这条用例绿得毫无意义
    # （「装得进」当然装得进 —— 因为根本没装东西）。
    filled = wire_bytes([{"ref": r, "text": t} for r, t in materials.items()])
    assert 3600 <= filled <= 3804, f"素材只填到 {filled} 字节，这一条就不是在测顶格"

    transport = _generate(
        monkeypatch,
        knowledge=worst_chunks,
        materials=materials,
        understanding={
            "event_id": "00000000-0000-4000-8000-000000000000",
            "slot_updates": [
                {"slot_id": f"s{index}", "value": "运" * 40} for index in range(60)
            ],
        },
    )
    prompt = _sent_prompt(transport)
    # understanding 那一侧也要顶到它自己的额度上（`_bounded_understanding` 压过之后）：
    # 少了这一句，「给一份超量的」可能被压成一份很小的，于是同样测不到顶格。
    understanding_wire = understanding_wire_bytes(prompt["understanding"])
    assert understanding_wire > UNDERSTANDING_PROMPT_BYTES - 200, (
        f"understanding 只占 {understanding_wire} 字节，离它的额度 "
        f"{UNDERSTANDING_PROMPT_BYTES} 太远 —— 这一条就不是在测顶格"
    )
    wire = len(json.dumps(transport.calls[0]["payload"], **_WIRE_JSON).encode("utf-8"))

    assert wire <= MAX_MODEL_INPUT_BYTES, (
        f"顶格回合实测 {wire} 字节，上限 {MAX_MODEL_INPUT_BYTES} —— "
        "超版的表现是 ProviderInputTooLarge → 回落保守计划 → 客户这一轮收不到消息"
    )


# ---------------------------------------------------------------------------
# 2. 引用必须是本轮给过的
# ---------------------------------------------------------------------------


def test_a_claim_may_cite_a_chunk_this_turn_retrieved() -> None:
    """正路：引本轮给过的 ref，过。"""

    document = validate_generation(
        generation=_generation(claims=[_claim(SHIPPING)]),
        plan=_validated_plan(constraints=["evidence_required"]),
        materials={},
        knowledge=dict(KNOWLEDGE),
    )

    assert document["claims"][0]["source_evidence_refs"] == [SHIPPING]


def test_a_claim_citing_a_chunk_this_turn_did_not_retrieve_stops_the_turn() -> None:
    """**编出来的出处比没有出处更糟，所以是 refuse 不是 drop。**

    没有出处会被 `evidence_required` 拦住、让模型说「不确定」；
    而一条引了不存在来源的事实声明**读起来像有据可查** ——
    客户与商家都没有办法从那句话里看出它是编的。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim("knowledge:invented-policy:v9")]),
            plan=_validated_plan(),
            materials={},
            knowledge={SHIPPING: KNOWLEDGE[SHIPPING]},
        )

    assert raised.value.reason_code == "GENERATION_KNOWLEDGE_REF_UNKNOWN"
    assert "knowledge:invented-policy:v9" in str(raised.value)
    assert KNOWLEDGE[SHIPPING] not in str(raised.value), (
        "决策行里只放 ref，不放客户可见的字"
    )


def test_this_gate_is_what_evidence_required_never_checked() -> None:
    """**反面：这道闸不能靠 `evidence_required` 也能过。**

    没有这一条，下一个人会以为「有 `evidence_required` 就够了」。
    这里让约束**开着**、`source_evidence_refs` **非空**，而里面那个 ref 是编的 ——
    旧口径下这份文档完全合法。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim("knowledge:invented:v1")]),
            plan=_validated_plan(constraints=["evidence_required"]),
            materials={},
            knowledge=dict(KNOWLEDGE),
        )

    assert raised.value.reason_code == "GENERATION_KNOWLEDGE_REF_UNKNOWN", (
        "红在这道闸上，而不是 `evidence_required` 那条 —— 后者看不见 ref 的内容"
    )


@pytest.mark.parametrize("offered", [None, {}], ids=["never-retrieved", "retrieved-nothing"])
def test_a_turn_that_offered_no_knowledge_allows_no_knowledge_ref(
    offered: dict[str, str] | None,
) -> None:
    """**fail-closed：没给过不等于什么都能引。**

    两个入参在语义上不同（`None` = 这条路没接 / 没请求检索，`{}` = 检了没命中），
    而在这道闸上必须同一个结果 —— 它们都意味着**本轮模型没见过任何知识库正文**。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim(SHIPPING)]),
            plan=_validated_plan(),
            materials={},
            knowledge=offered,
        )

    assert raised.value.reason_code == "GENERATION_KNOWLEDGE_REF_UNKNOWN"


def test_a_ref_that_is_not_a_knowledge_ref_is_left_alone() -> None:
    """这道闸**只管 `knowledge:` 前缀**，不改其它证据 ref 的口径。

    `source_evidence_refs` 里也会有 understanding 那一侧的东西
    （`evidence_span_hash` 一类，模型自己那一轮的证据）。把它们一起收进来
    等于在本批悄悄改一条与知识库无关的规则 —— **而那条规则今天有没有人在看，
    本批没有测量过**，所以不动它。
    """

    document = validate_generation(
        generation=_generation(claims=[_claim("span:9f2c1b", SHIPPING)]),
        plan=_validated_plan(),
        materials={},
        knowledge=dict(KNOWLEDGE),
    )

    assert document["claims"][0]["source_evidence_refs"] == ["span:9f2c1b", SHIPPING]


# ---------------------------------------------------------------------------
# 3. 逐字引这条路本来就走不通（所以提示词不为它花字节）
# ---------------------------------------------------------------------------


def test_a_knowledge_ref_cannot_be_quoted_as_a_material() -> None:
    """**「知识库不能逐字引」这件事已经被执行了，不是靠提示词里的一句话。**

    一个 `material_ref` 段的 ref 必须在这一轮的 `materials` 里
    （`_validate_material_refs`），而知识库的 ref 不在那张表里 —— 这条路走不通。
    这一条是那句话的**测量**：它红，说明「不必在提示词里再说一遍」这个取舍失效了，
    那时候要么补提示词（付字节）要么补闸。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(
                segments=[{"type": MATERIAL_REF_SEGMENT_TYPE, "content_or_ref": SHIPPING}]
            ),
            plan=_validated_plan(),
            materials={MATERIAL_REF: MATERIAL_TEXT},
            knowledge=dict(KNOWLEDGE),
        )

    assert raised.value.reason_code == "GENERATION_MATERIAL_REF_UNKNOWN", (
        "拦它的是素材那道闸 —— 知识库的 ref 压根不在这一轮的素材表里"
    )


def test_a_copied_chunk_without_a_citation_is_not_detected_today() -> None:
    """**本文件测不出来的那一件，写下来而不是留白。**

    模型把一条 chunk 的正文**逐字抄进 `text` 段**、又不引它的 ref：
    这一份全绿。检出它要判「这段话是不是那段话的抄写」—— 一次语义判断，
    **而平台去做这种判断，就是在没有来源的情况下生成一个事实结论**
    （裁定四同一条理由）。

    与素材那一侧的 `test_a_paraphrased_material_is_not_detected_today` / T268 同源。
    **它不是一个洞，是这套机制的边界**：被防住的是「引用变成改写」，
    而「自己写一段像资料的话」与「自己写一段话」在数据上无从区分。
    """

    document = validate_generation(
        generation=_generation(
            segments=[{"type": "text", "content_or_ref": KNOWLEDGE[SHIPPING]}]
        ),
        plan=_validated_plan(),
        materials={},
        knowledge=dict(KNOWLEDGE),
    )

    assert document["customer_visible_segments"][0]["content_or_ref"] == KNOWLEDGE[SHIPPING]


# ---------------------------------------------------------------------------
# 4. 提示词那一行：它说的和闸做的是同一件事
# ---------------------------------------------------------------------------


def test_the_prompt_tells_the_model_the_two_rules_the_gates_enforce() -> None:
    """提示词那一行**只说有闸的那两件**，而这一条把它与闸对上。

    `knowledge_chunks` 那一行说的是：(a) 用自己的话，把 ref 放进
    `source_evidence_refs`（闸 = `GENERATION_KNOWLEDGE_REF_UNKNOWN`）；
    (b) 有素材时素材优先（裁定四，落在 `quoted_side` 那一列上）。
    **它不说「不许逐字引」** —— 那条已经被素材那道闸执行了，
    而提示词里每个字节都是每次 generate 都付的。
    """

    from fastagent_v2.worker.inference_model import _GENERATE_SYSTEM

    assert "knowledge_chunks is reference only" in _GENERATE_SYSTEM
    assert "source_evidence_refs" in _GENERATE_SYSTEM
    assert "content_material overrides it" in _GENERATE_SYSTEM


def test_the_bound_and_the_prompt_agree_on_what_a_chunk_is() -> None:
    """上界产出的形状**直接就是提示词吃的形状**，中间没有转换。

    `bound_for_prompt` 返回 `{ref, text}` 的元组，管线把它变成
    `ref → text` 的映射交给 `generate`。**两处形状必须同源** ——
    中间多一道转换就多一个可以悄悄丢字段或改键名的地方。
    """

    bounded = bound_for_prompt(
        [{"ref": ref, "text": text, "score": 0.5} for ref, text in KNOWLEDGE.items()]
    )
    as_mapping = {chunk["ref"]: chunk["text"] for chunk in bounded.chunks}

    assert as_mapping == KNOWLEDGE
