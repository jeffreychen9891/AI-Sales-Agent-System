"""运维 §10.4 判据 2：**许下的，这一轮就要兑现。**

## 现场（运维 2026-09-18 记，逐字）

`06:31:11` 我们发出去的那条消息：

> text：「明白，您自己接。那我先把试用怎么开、您那边要准备什么讲清楚。」
> material_ref `demo-price-plain:v1`：「价格是一个月 1888……」

运维的判词逐字：

> 这个后面应该就接上后面的承诺，**而不是等客人说话再做答** ——
> 因为他不一定想了解我们给出的承诺，**话题转开，前面的承诺就没有机会讲了**。
> **这条比「下一段就要是它」更硬：不是顺序问题，是轮次问题。**

## 用户 2026-09-18 裁的两条

1. **「发出来就算兑现了」** —— 不做语义匹配，不验「讲的是不是许诺的那一件」；
2. **「只丢掉那一段，其余照发」** —— 一条不剩时才整份拒。

## 它抓不到什么，写在这里

**上面那一轮按这条判合格** —— 它确实发了实质内容，只是发错了那一件。
**承诺 A 交付 B 是选材那一族**（批 A/B/C 从选材那侧修过）。
一道闸只答一个问题：「发出来了没有」可量，「发的是不是那一件」不可量。

## 为什么 `no_new_commitment` 顶不住

那条约束**早就有**，演示包七个打法全声明，`advance_goal` 上还由平台无条件
施加（T332）。而它的两个信号 —— `claims[].commitment_id` 与 `safety_markers`
里的 `contains_commitment` —— **都是模型自述的**。模型不举手，闸就看不见。
本文件 `test_the_existing_constraint_only_sees_what_the_model_admits` 钉住这件事。

## ⚠️ 复核 2026-09-18 退回这一批，两条，**同一个根**（重交时改的就是它们）

**第一条：丢的单位错了。** 第一版按**段**丢，而生产的一段是**多句**的 ——
运维 §10.1 那条真实出站的 `text` 段里装着两句话。纯文本回合（这道闸唯一
会跑的那种）又常常**只有一段**，同一份运维记录写着十一轮对话
`send_material` 出现 **0 次**。于是「丢一段」= 整条消息 = **客户整轮收不到东西**，
而自审与四处注释一致地把整份拒写成了「窄支」。

**它在用例里绿，是因为这份文件当时把那句话拆成了两段喂进去** ——
`ACKNOWLEDGE` / `THE_PROMISE_HALF` 那两个常数。拆开是夹具的选择，生产不拆。
**这正是本仓「自造夹具藏得住形状不匹配」那一族**（`AGENTS.md` T349）。
重交之后：**下面每一条实测用例都按生产的形状喂一整段**，
拆两段那一条留着，而它的名字与 docstring 写明它量的是对照。

**第二条：尺子的 docstring 声称「不跨句」，而逗号、顿号、冒号都不算句界。**
于是「我明白**，**您方便说一下这份报价是给哪块业务看的吗」被读成一句许诺 ——
那句话里**动词的主语是客户**。实测这一族八条，外加三条「安排」作名词。
重交之后句界与分句符各是一个常数，**尺子的字符类由它们拼出来**，
于是那句 docstring 不可能再比实现严。

**一条没修，如实登记**：跨**顿号**那一种仍然会中（T366）。顿号是列举符号，
而本批的主证据「那我先把试用怎么开、您那边要准备什么讲清楚」**正是靠跨过它
才读得出**。见 `test_the_ruler_still_crosses_a_list_separator` 与
`test_a_list_separator_used_as_a_clause_separator_still_matches` 那一对。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.worker.inference_validation import (
    _PROMISE_TO_DELIVER,
    SENTENCE_BOUNDARY_CHARS,
    UNKEPT_PROMISE_REASON_CODE,
    GenerationRejected,
    ValidatedPlan,
    _sentences,
    drop_promises_this_turn_does_not_keep,
    validate_generation,
    validate_plan,
)

from tests.contracts.return_path_builders import understanding_with

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = json.loads(
    (REPOSITORY_ROOT / "profiles" / "synthetic-third" / "profile.json").read_text(
        encoding="utf-8"
    )
)
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)

#: 逐字取自运维 2026-09-18 `06:31:11` 的实测出站。**一段，两句。**
PROMISE = "明白，您自己接。那我先把试用怎么开、您那边要准备什么讲清楚。"

#: 同一条出站的两半。**这两个常数不是喂进去的形状，是期望值** ——
#: 复核 2026-09-18 退回第一条：先前它们被当成两个段喂进去，
#: 而**生产不拆**。今天 `PROMISE` 整段喂进去，留下来的应当逐字等于
#: `ACKNOWLEDGE`，被丢掉的是 `THE_PROMISE_HALF` 那一句。
ACKNOWLEDGE = "明白，您自己接。"
THE_PROMISE_HALF = "那我先把试用怎么开、您那边要准备什么讲清楚。"

#: 逐字取自运维 §2.2 —— **许诺 + 一个问题，什么都没出去**。
#: 这一条是「后面还有一段字」那种判据**过不去**的原因。
ARRANGE = "收到，报价单这边我来安排。"
A_QUESTION = "您方便说一下这份报价是给哪块业务、大概什么量级看的吗"

#: 运维 §2.2 那一轮**生产里真实的形状：一段**。
#: `ARRANGE` 与 `A_QUESTION` 在真实 `intent_records.proposal` 里同属一个
#: `text` 段，而这份文件先前把它们拆成两段喂进去 —— 复核退回第一条。
THE_ARRANGE_TURN = ARRANGE + A_QUESTION

#: 除了一句许诺什么都没有 —— **单段、单句**。整份拒那一支要求的就是这种。
#:
#: ⚠️ **先前这条用例喂的是 `PROMISE`**，而 `PROMISE` 是两句：按句丢之后
#: 它留下「明白，您自己接。」，不再整份拒。**夹具当时恰好等于错误答案**
#: （本仓 `a-fixture-that-matches-the-wrong-answer`）——
#: 用例喂什么决定了它量的是哪一支，而不是喂什么都量同一支。
NOTHING_BUT_A_PROMISE = "我这就把报价单发您。"

MATERIAL_REF = "material:demo-price-plain:v1"
KNOWLEDGE_REF = "knowledge:deploy:1"
MATERIALS = {MATERIAL_REF: "价格是一个月 1888，前十天免费试用，试用期内不收费。"}


def _aggregate() -> dict[str, Any]:
    definition = PROFILE["target_frames"][0]
    return {
        "id": str(uuid4()),
        "state_version": 3,
        "target_stack": [
            {
                "id": str(uuid4()),
                "definition_id": definition["id"],
                "goal": definition["goal"],
                "status": "active",
                "slots": {},
            }
        ],
    }


def _plan() -> ValidatedPlan:
    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" in body["allowed_intent_kinds"] and name != "wait"
    )
    return validate_plan(
        plan={
            "schema_version": "1.0.0",
            "origin": "model",
            "target_stack_operation": {"op": "keep", "target_frame_definition_id": None},
            "selected_strategy_id": strategy,
            "strategy_reason_code": "TEST",
            "confidence": {
                "raw_score": 0.9,
                "calibrated_score": None,
                "assessment_version": "model-v1",
                "factor_codes": [],
            },
            "required_slot_requests": [],
            "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
            "candidate_intents": [
                {"kind": "send_message", "typed_payload": {}, "idempotency_basis": "b1"}
            ],
            "generation_constraints": [],
            "manual_review_reason": None,
            "model_evidence": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "prompt_release": "m1a-prompt-v8",
                "latency_ms": 1,
                "token_usage": {"input": 1, "output": 1},
            },
        },
        understanding=understanding_with(),
        aggregate=_aggregate(),
        profile=PROFILE,
    )


def _generation(
    plan: ValidatedPlan,
    texts: list[str],
    *,
    quote: bool = False,
    quote_draft_only: bool = False,
    sourced_claim: bool | None = None,
) -> dict[str, Any]:
    def build(with_material: bool) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = [
            {"type": "text", "content_or_ref": text} for text in texts
        ]
        if with_material:
            segments.append({"type": "material_ref", "content_or_ref": MATERIAL_REF})
        return segments

    intents: list[dict[str, Any]] = [
        {
            "kind": "send_message",
            "typed_payload": {"content": {"segments": build(quote)}},
            "evidence_refs": [],
        }
    ]
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "strategy_id": plan.strategy_id,
        "customer_visible_segments": build(quote or quote_draft_only),
        "proposed_intents": intents,
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        # **真形状是 `{text_hash, source_evidence_refs}`** ——
        # 第一版这里写的是 `{text, evidence_refs}`，而 schema 是
        # `additionalProperties: false`。两边自洽、与真文档对不上，
        # **schema 当场红了**（本仓 `fabricated-fixtures-hide-shape-mismatch`）。
        "claims": (
            []
            if sourced_claim is None
            else [
                {
                    "text_hash": hashlib.sha256(b"deploy").hexdigest(),
                    "source_evidence_refs": [KNOWLEDGE_REF] if sourced_claim else [],
                }
            ]
        ),
        "return_confirmation": {"included": False, "tone_material_ref": None},
        "safety_markers": ["none"],
        "model_evidence": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "prompt_release": "m1a-prompt-v8",
            "latency_ms": 1,
            "token_usage": {"input": 1, "output": 1},
        },
    }


def _run(texts: list[str], **kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = _plan()
    unkept: list[dict[str, Any]] = []
    result = validate_generation(
        generation=_generation(plan, texts, **kwargs),
        plan=plan,
        materials=dict(MATERIALS),
        # **这一轮真的检索过的那几条** —— 不传，一条引知识库的 claim 会先红在
        # `GENERATION_KNOWLEDGE_REF_UNKNOWN` 上，于是那条用例量的就成了那道闸。
        knowledge={KNOWLEDGE_REF: "私有化部署支持独立机房。"},
        unkept=unkept,
    )
    return result, unkept


def _texts(document: dict[str, Any], *, where: str = "payload") -> list[str]:
    segments = (
        document["proposed_intents"][0]["typed_payload"]["content"]["segments"]
        if where == "payload"
        else document["customer_visible_segments"]
    )
    return [
        str(segment["content_or_ref"])
        for segment in segments
        if segment["type"] == "text"
    ]


# ---------------------------------------------------------------------------
# 一 · 实测那两轮
# ---------------------------------------------------------------------------


def test_the_promise_that_pushes_the_content_to_the_next_turn_is_dropped() -> None:
    """**运维 §10.1 那一句，逐字，按生产的形状喂：一段两句。**

    真实那一轮贴了价格素材（所以按这条判合格，见 `…_a_material_counts`）。
    **这里量的是那一句本身**：同样一句话，而这一轮拿不出任何东西。

    ⚠️ **这条用例是复核退回第一条的落点。** 先前它把这一段拆成
    `[ACKNOWLEDGE, THE_PROMISE_HALF]` 两段喂进去，于是量到的是
    「丢一段、另一段照发」—— **而生产不拆**：那两句在真实
    `intent_records.proposal` 里同属一个 `text` 段，按段丢就是整份拒。
    今天整段喂进去，量的是「**丢掉许诺那一句，段里其余的字照发**」。
    """

    document, unkept = _run([PROMISE])

    assert _texts(document) == [ACKNOWLEDGE], (
        "要么前半句被牵连了（整段丢了），要么许诺那一句还在"
    )
    assert len(unkept) == 2, f"草稿与载荷各该记一条：{unkept}"
    assert {item["why"] for item in unkept} == {UNKEPT_PROMISE_REASON_CODE}
    assert {item["promised"] for item in unkept} == {"讲清楚"}, (
        "记进审计行的该是那个动词短语，不是整段匹配的尾巴"
    )


def test_the_same_paragraph_split_into_two_segments_behaves_the_same() -> None:
    """**对照**：同一句话拆成两段时结果不变。

    这条用例**不是生产的形状**（生产不拆，见上一条），它在这里是为了钉住
    「按句丢」没有把「按段丢」那一支弄坏：模型真的分了两段时，
    装着许诺的那一段整个消失，另一段原样照发。
    """

    document, unkept = _run([ACKNOWLEDGE, THE_PROMISE_HALF])

    assert _texts(document) == [ACKNOWLEDGE]
    assert {item["promised"] for item in unkept} == {"讲清楚"}


def test_a_promise_followed_by_a_question_is_still_unkept() -> None:
    """**运维 §2.2 那一轮，逐字，一段** —— 这一条是本批判据的分界线。

    「收到，报价单这边我来安排。您方便说一下……吗」——
    **后面确实还有字，而什么都没出去。**

    ⚠️ 假如判据写成「这条消息里还有别的话就算兑现」，这一轮就判合格，
    **而它正是运维记下来的那次失败**。所以「还有一段字」不能当兑现。

    ⚠️ **也是复核退回第一条的落点**：先前喂的是 `[ARRANGE, A_QUESTION]`
    两段。按段丢的实现在这一条真实的单段串上给出的是**整份拒** ——
    客户连那个问句都收不到。
    """

    document, unkept = _run([THE_ARRANGE_TURN])

    assert _texts(document) == [A_QUESTION], "问句该留下，许诺那一句该被丢掉"
    assert {item["promised"] for item in unkept} == {"安排"}


# ---------------------------------------------------------------------------
# 二 · 「发出来就算兑现」的三条路，各一条
# ---------------------------------------------------------------------------


def test_quoting_a_material_counts_as_delivering() -> None:
    """贴了商家原文 = 有东西出去 —— **实测那一轮就是这样，它判合格**。"""

    document, unkept = _run([PROMISE], quote=True)

    assert _texts(document) == [PROMISE], "许诺被兑现了却还是被丢了"
    assert unkept == []


def test_sending_a_file_counts_as_delivering() -> None:
    """发了文件 = 有东西出去。

    ⚠️ **这一条直调被测函数，不走 `validate_generation`** ——
    现读 `synthetic-third` 的七个打法，`allowed_intent_kinds` **一个都不含
    `send_material`**，硬塞一条进计划会被另一道闸（`GENERATION_ADDED_INTENT`）
    先拦下，**于是这条用例量的就变成了那道闸，不是这一条**。
    """

    plan = _plan()
    document = _generation(plan, [PROMISE])
    document["proposed_intents"].append(
        {
            "kind": "send_material",
            "typed_payload": {"ref": "asset:trial-setup-guide"},
            "evidence_refs": [],
        }
    )
    unkept: list[dict[str, Any]] = []
    drop_promises_this_turn_does_not_keep(document, dropped=unkept)

    assert _texts(document) == [PROMISE]
    assert unkept == []


def test_an_evidenced_claim_counts_as_delivering() -> None:
    """**有来源的散文回答也是内容** —— 这一条不能省。

    省了它，「我说说它具体怎么接」+ 一段引自知识库的解释会被判成空头支票 ——
    **那是一个好回合，而闸把它丢了。**
    """

    document, unkept = _run(["我说说它具体怎么接。"], sourced_claim=True)

    assert _texts(document) == ["我说说它具体怎么接。"]
    assert unkept == []


def test_a_claim_without_evidence_does_not_count() -> None:
    """**带来源的才算。** 没有来源的 claim 是模型自己编的一句话，

    而本函数整条存在的理由就是「不按模型自述判」。
    """

    document, unkept = _run(
        [ACKNOWLEDGE, "我说说它具体怎么接。"], sourced_claim=False
    )

    assert _texts(document) == [ACKNOWLEDGE]
    assert {item["why"] for item in unkept} == {UNKEPT_PROMISE_REASON_CODE}


# ---------------------------------------------------------------------------
# 三 · 逐张判，不整份判
# ---------------------------------------------------------------------------


def test_the_gate_this_one_leans_on_still_runs_first() -> None:
    """**本批「整份判」是靠上游那道闸才成立的。**

    判据按整份算：任何一张段列表里有 `material_ref`，整份就算有东西出去。
    这在「草稿贴了而载荷没贴」时会判错 —— 而那个方向**上游已经拦了**：
    `_validate_material_refs` 里的 `GENERATION_PAYLOAD_DROPPED_MATERIAL`
    保证草稿引的 ref ⊆ 载荷引的 ref，**且它排在本函数前面**。

    ⚠️ 所以这条用例钉的是那个**前提**，不是本批的代码：
    那道闸挪到本函数后面、或者松掉，这里的整份判就该重新想。
    **它红的时候要改的是设计，不是把它删掉。**
    """

    with pytest.raises(GenerationRejected) as raised:
        _run([ACKNOWLEDGE], quote_draft_only=True)

    assert raised.value.reason_code == "GENERATION_PAYLOAD_DROPPED_MATERIAL"

    source = inspect.getsource(validate_generation)
    assert source.index("_validate_material_refs(") < source.index(
        "drop_promises_this_turn_does_not_keep("
    ), "那道闸跑到本函数后面去了 —— 整份判的前提没了"


# ---------------------------------------------------------------------------
# 四 · 一条不剩时整份拒
# ---------------------------------------------------------------------------


def test_a_message_that_is_nothing_but_a_promise_is_refused() -> None:
    """除了那句许诺什么都没有 —— 整份拒，**而事实带在 `context` 里**。

    与 5.7 同一条路径。**代价比那边低**：那边拦的是一句错话，
    这边拦的只是一句没兑现的预告。

    ⚠️ **喂的是单段单句**（`NOTHING_BUT_A_PROMISE`）。先前这里喂的是
    `PROMISE`，而 `PROMISE` 是两句 —— 按句丢之后它留下「明白，您自己接。」，
    **不再走这条路**。那时的夹具恰好等于按段丢时的答案，
    于是这条用例量的其实是另一支。
    """

    with pytest.raises(GenerationRejected) as raised:
        _run([NOTHING_BUT_A_PROMISE])

    assert raised.value.reason_code == UNKEPT_PROMISE_REASON_CODE
    context = raised.value.context or {}
    assert context["refused_because"] == [UNKEPT_PROMISE_REASON_CODE]
    assert len(context["dropped_segments"]) == 2
    assert all(
        NOTHING_BUT_A_PROMISE not in json.dumps(item, ensure_ascii=False)
        for item in context["dropped_segments"]
    ), "正文进了审计上下文 —— 客户可见的话不该再存一份"


def test_the_whole_reply_is_only_refused_when_every_sentence_is_a_promise() -> None:
    """**整份拒那一支到底有多窄** —— 一条正面量它的用例。

    复核退回第一条说的就是这件事：按段丢时，**一句误命中就够**（段只有一个）；
    按句丢之后，整份拒要求**每一句**都是许诺。这条用例把两种形状摆在一起：
    同一句许诺后面接一句不相干的话，就不该整份拒。
    """

    document, unkept = _run([NOTHING_BUT_A_PROMISE + "价格是一个月 1888。"])

    assert _texts(document) == ["价格是一个月 1888。"], "另一句被牵连了"
    assert {item["promised"] for item in unkept} == {"发您"}

    # 而每一句都是许诺时，整份拒照旧。
    with pytest.raises(GenerationRejected):
        _run([NOTHING_BUT_A_PROMISE + "我再把对接清单发过去。"])


def test_two_promises_in_one_paragraph_are_both_recorded() -> None:
    """一段里两句许诺 —— **审计行记两条，不是一条**。

    按段丢时一段只记得下第一句那个动词短语；按句丢之后
    「一条 = 一句」，于是排查的人看得出这一段里许了几件事。
    """

    document, unkept = _run(
        ["我这就把报价单发您。我再给您整理好对接清单。价格是一个月 1888。"]
    )

    assert _texts(document) == ["价格是一个月 1888。"]
    # 草稿与载荷各两条。
    assert len(unkept) == 4, f"两句许诺 × 两份段列表：{unkept}"
    assert {item["promised"] for item in unkept} == {"发您", "整理好"}


def test_the_refusal_wakes_someone_on_the_second_one() -> None:
    """**客户收不到 · 没人被告知 · 每轮都拒也不会升级** —— 5.7 被退回的那三件。

    新增一个会导致沉默的理由码而不进这两张表，默认落进完全无声。
    """

    from fastagent_v2.worker.pipeline import (
        HANDOFF_REASON_CODES,
        LOOP_ESCALATION_REASON_CODES,
    )

    assert UNKEPT_PROMISE_REASON_CODE in LOOP_ESCALATION_REASON_CODES
    assert UNKEPT_PROMISE_REASON_CODE not in HANDOFF_REASON_CODES, (
        "它不是「该转人工」，是我们自己的话写坏了"
    )


def test_someone_counts_it_even_on_the_first_one() -> None:
    """单次不叫人，**而运维要数得出来**（与 `unfounded_fact_refusal` 同源）。"""

    source = (REPOSITORY_ROOT / "scripts" / "stage_observability.py").read_text(
        encoding="utf-8"
    )
    assert "GENERATION_PROMISED_WITHOUT_DELIVERING" in source, "没有任何查询在数它"
    assert "fastagent_v2_stage_unkept_promise_refusal_total" in source, "数了而没渲染成指标"


# ---------------------------------------------------------------------------
# 五 · 那把尺子本身：五条该中、七条不该中
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        PROMISE,
        ARRANGE,
        "我说说它具体怎么接，您看对不对得上。",
        "我把对接清单发您。",
        "我给您整理好再发过去。",
    ],
)
def test_these_are_promises(sentence: str) -> None:
    assert _PROMISE_TO_DELIVER.search(sentence) is not None


@pytest.mark.parametrize(
    "sentence",
    [
        "明白，您自己接。",
        # **第一人称复数是产品事实，不是许诺。**
        "我们支持私有化部署。",
        # **内部动作，客户不会等它。**
        "我这边先确认一下再回您。",
        # **在陈述，不在许诺。**
        "我说的是标准版的价格。",
        "价格是一个月 1888，按年付有折扣。",
        "开通要您这边配合三件：账号、话术终审、对外资料原件。",
        # **主语是客户** —— 「说一下」前面那个人不是我们。
        A_QUESTION,
    ],
)
def test_these_are_not(sentence: str) -> None:
    assert _PROMISE_TO_DELIVER.search(sentence) is None


def test_the_ruler_does_not_cross_a_sentence_boundary() -> None:
    """跨一个句号去凑「我……发您」会把两句不相干的话读成一句许诺。"""

    assert _PROMISE_TO_DELIVER.search("我明白。报价单发您了吗") is None


@pytest.mark.parametrize("boundary", list(SENTENCE_BOUNDARY_CHARS))
def test_the_ruler_does_not_cross_any_character_the_split_treats_as_a_boundary(
    boundary: str,
) -> None:
    """**句界那个集合有两个消费方，这条用例钉住它们是同一个集合。**

    尺子不跨它，`_sentences` 按它切。**两处各写一份就是这道闸漂掉的方式** ——
    复核 2026-09-18 退回第二条抓到的正是那个形状：docstring 声称「不跨句」，
    而字符类里少了几个字符，**于是那句话钉的是措辞**。

    这一条**按行为判，不读源码**：逐个字符造一句跨过它的话，尺子都不该中。
    读源码那种写法（「pattern 里有这几个字符」）在两边各写一份时同样是绿的。
    """

    assert _PROMISE_TO_DELIVER.search(f"我明白{boundary}报价单发您了吗") is None


def test_this_is_still_the_only_place_that_splits_by_sentence() -> None:
    """**按句拆段今天只有这一处** —— 多一处就红。

    `_sentences` 是全仓第一个按句拆段的地方（复核 2026-09-18 在 `1843da7c`
    上现读，0 命中），而「只有一处」这件事**不能写成一条 grep**：
    把那条命令抄进被查文件的 docstring，它就命中自己
    （本仓「数错了的守卫看起来在守」那一族）。这条用例在
    `services/` 与 `packages/` 里数，**而它自己住在 `tests/`**，所以数不到自己。

    它红的时候要想清楚的不是「把期待值加一」，而是：
    **两处拆法要不要共用同一个句界集合。** 两个句界集合意味着
    尺子挡的和刀切的不是同一条线 —— 那正是复核退回第二条的形状。
    """

    definitions = 0
    call_sites: list[str] = []
    for root in ("services", "packages"):
        for path in (REPOSITORY_ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_sentences":
                    definitions += 1
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_sentences"
                ):
                    call_sites.append(f"{path.name}:{node.lineno}")

    assert definitions == 1, f"按句拆有了第二个定义点：{definitions} 处"
    assert len(call_sites) == 1, f"按句拆的调用点不止一处：{call_sites}"


def test_the_half_width_terminators_are_in_the_boundary_set() -> None:
    """⚠️ **上一条参数化取自那个常数本身，所以它看不见「有人从集合里删了一个字符」。**

    那是本仓「空守卫」那一族的一种：参数表跟着被查对象缩，关系照样成立，
    **而集合本身可以悄悄变小**。这一条用**写死的串**钉住半角 `!?;`
    今天在集合里 —— 两头各一条断言：尺子跨不过它，`_sentences` 按它切。

    **全角一族不在这里重钉**：`test_the_ruler_does_not_cross_a_sentence_boundary`
    已经钉了句号那一种，而那条是写死的。
    """

    assert _PROMISE_TO_DELIVER.search("我明白!报价单发您了吗") is None
    assert _PROMISE_TO_DELIVER.search("我明白?报价单发您了吗") is None
    assert _PROMISE_TO_DELIVER.search("我明白;报价单发您了吗") is None
    assert _sentences("我明白!报价单发您了吗") == ["我明白!", "报价单发您了吗"]


def test_the_half_width_full_stop_is_deliberately_not_a_boundary() -> None:
    """**半角句点不在集合里，而那是一个选择，不是漏的。**

    `material:demo-x:v1`、「1.2.3」、「20.5 万」都带点。按它切会把一个 ref
    切成两半，而尺子跨不过它就够不着「我把 20.5 万那一档整理好」。
    """

    assert _sentences("20.5 万") == ["20.5 万"]
    assert _PROMISE_TO_DELIVER.search("我把 20.5 万那一档整理好") is not None


@pytest.mark.parametrize("boundary", list("，,：:"))
def test_the_ruler_stops_at_a_clause_boundary_too(boundary: str) -> None:
    """**复核退回第二条**：逗号 / 半角逗号 / 冒号后面换了主语，就不是许诺了。

    实测那八条里的形状：「我明白**，**您方便说一下这份报价是给哪块业务看的吗」
    —— **动词的主语是客户**，只是同句前面多了一个「我」。
    这与已经钉住的 `A_QUESTION`（不带那个「我」）是同一个理由。
    """

    assert _PROMISE_TO_DELIVER.search(f"我明白{boundary}您方便说一下这份报价吗") is None


def test_the_ruler_still_crosses_a_list_separator() -> None:
    """⚠️ **顿号必须留着可跨** —— 本批的主证据靠它。

    运维 §10.1 那句真实出站是「那我先把试用怎么开**、**您那边要准备什么讲清楚。」：
    顿号在中文里是**列举**符号（「把 A、把 B 讲清楚」），不是分句符。
    把 `、` 加进分句符集合，**这一批要抓的那一句当场漏过去**。

    这条用例是那个取舍的守卫：照着「顿号也该算分句」去改的人会先看到它红，
    而红的含义是「你正在放掉本批的主证据」，不是「这里的期待值该改」。
    """

    assert _PROMISE_TO_DELIVER.search(THE_PROMISE_HALF) is not None
    assert _PROMISE_TO_DELIVER.search("那我先把试用怎么开、您那边要准备什么讲清楚。")


def test_a_list_separator_used_as_a_clause_separator_still_matches() -> None:
    """**T366 · 一条没修的口，钉在这里，连着它的代价一起。**

    顿号留着可跨的代价：拿顿号**当分句符**写的句子仍然会中 ——
    「我这边有两个问题、您先说一下量级」这种。

    **为什么没修**：上面那条真实出站是逐字取自 `intent_records` 的生产串，
    而这一句是本窗口自己编的。**拿编出来的形状去调尺子，正是 T349 反过来。**
    两个候选修法都试算过：把 `、` 加进分句符会让主证据漏过去；
    「跨顿号要求 `我` 与动词之间有一个 `把`」只有一个数据点支撑它。

    **到期条件**：运维实测到一条这种形状的真实出站。
    那一天这条用例该红 —— 而它红的时候要改的是尺子，不是这里的期待值。
    """

    assert _PROMISE_TO_DELIVER.search("我这边有两个问题、您先说一下量级") is not None

    # **代价量到底**：整条消息只有这一句时，它仍然是整份拒。
    with pytest.raises(GenerationRejected):
        _run(["我这边有两个问题、您先说一下量级"])


@pytest.mark.parametrize(
    "sentence",
    [
        # 「的 / 个」在左边 —— 名词用法，实测这一族三条。
        "我看了您的安排，这边没问题",
        "我这边没有别的安排",
        "我觉得这个安排挺好",
    ],
)
def test_the_verb_and_the_noun_called_arrange_are_told_apart(sentence: str) -> None:
    """**「安排」既是动词也是名词**，而只有动词那一种是许诺。

    复核 2026-09-18 实测这一族三条。判据是**左边那个字**：
    名词用法前面是「的 / 个 / 些 / 这 / 那」之一。
    """

    assert _PROMISE_TO_DELIVER.search(sentence) is None


def test_the_lookbehind_is_only_on_arrange() -> None:
    """**那道前瞻只给「安排」，不给整个动词组** —— 不然会漏掉一句真许诺。

    「我把这**个**讲清楚」里「个」同样在动词左边，而那是一句真许诺。
    给整组加前瞻是一个看起来更整齐、而漏掉真阳性的改法。
    """

    assert _PROMISE_TO_DELIVER.search("我把这个讲清楚") is not None
    assert _PROMISE_TO_DELIVER.search("我来安排") is not None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "一句话没有标点",
        PROMISE,
        THE_ARRANGE_TURN,
        "多个标点。。！？；\n收尾没有标点",
        "半角也算!这样?可以;吧",
        "material:demo-price-plain:v1",
        "价格 20.5 万，按 1.2.3 那一版算。",
    ],
)
def test_splitting_a_paragraph_loses_nothing(text: str) -> None:
    """**拼回去必须逐字节等于原文** —— 这是「丢一句」的前提。

    一个把标点吃掉的拆法会让客户读到「明白您自己接」：
    留下来的那几句拼回同一段字，而标点是那段字的一部分。

    最后两条量的是**半角句点不在句界里**这个选择：`material:demo-x:v1`
    与「1.2.3」都带点，按它切会把一个 ref 切成两半。
    """

    assert "".join(_sentences(text)) == text


# ---------------------------------------------------------------------------
# 六 · 为什么既有的那条约束顶不住
# ---------------------------------------------------------------------------


def test_the_existing_constraint_only_sees_what_the_model_admits() -> None:
    """`no_new_commitment` **两个信号都是模型自述的**。

    演示包七个打法全声明了它，`advance_goal` 上还由平台无条件施加（T332）——
    而 §10.1 那一句照样过去了。读 `_validate_constraints` 那一段的 AST：
    它问的是 `claim["commitment_id"]` 与 `safety_markers` 里的
    `contains_commitment`，**两样都要模型自己举手**。

    本仓点过名的那一族：**不是模型的报告 …… 没有任何东西会红。**
    """

    from fastagent_v2.worker import inference_validation

    source = inspect.getsource(inference_validation._validate_constraints)
    tree = ast.parse(inspect.cleandoc(source))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "commitment_id" in strings
    assert "contains_commitment" in strings

    # **而本批这条不问模型任何问题** —— 它只读我们自己写出去的那几段字。
    #
    # ⚠️ **剥掉 docstring 再扫**：那段 docstring 里正好在解释
    # 「为什么不读 `safety_markers`」，连它一起扫等于**钉措辞而不是钉代码** ——
    # 第一版就是这么写的，当场红在自己的解释上。
    body = ast.parse(
        inspect.cleandoc(inspect.getsource(drop_promises_this_turn_does_not_keep))
    ).body[0]
    assert isinstance(body, ast.FunctionDef)
    if (
        body.body
        and isinstance(body.body[0], ast.Expr)
        and isinstance(body.body[0].value, ast.Constant)
    ):
        del body.body[0]
    code = ast.unparse(body)
    for self_report in ("safety_markers", "commitment_id"):
        assert self_report not in code, (
            f"本条闸读了 {self_report} —— 那就又回到「模型不举手就看不见」"
        )


def test_the_pipeline_actually_asks_for_this() -> None:
    """**两头都在、中间没接**是本仓另一族。管线真的传了那个列表吗。"""

    source = PIPELINE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    passed = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_generation"
        for keyword in node.keywords
    }
    assert "unkept" in passed, "校验器认这个参数，而管线一次都没传"
