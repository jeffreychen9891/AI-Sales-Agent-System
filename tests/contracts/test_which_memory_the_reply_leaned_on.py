"""T363：**那条回复引了哪几条记忆** —— 引用集，不是投喂集。

## 这一批之前，这个问题在结构上答不出来

E4-5（2026-09-18）把记忆值接进了**理解步与计划步**，并留下一行
`inference.memories_sent`。而 `app.py` 那条墓碑路由在本批之前写着：

> **而「那条回复真的*引了*哪几条」仍然答不出来** —— 上面那一行记的是
> 「递过去了哪几条」，递过去而没被用上的与用上的长得一样。

那句话为真，**而它为真的原因比它自己说的更深**：
**写回复的那一步根本没收到过记忆。** `request_shape.generate_prompt`
在本批之前没有任何记忆键 —— 于是「引了哪几条」不是「没人去记」，
是**没有任何一条引用可以存在**：引用集要有人去引。

所以本批做两件，顺序不能反：

1. **先让写字那一步看见记忆**（`remembered_facts`，带可引的 ref）；
2. **再把它引的那几条记下来**（`inference.memories_cited`，与 `memories_sent` 成对）。

## 键名换了，形状也换了 —— 这是有意的

| 哪一步 | 键 | 形状 | 它拿它做什么 |
|---|---|---|---|
| understand / plan | `known_memories` | `{field, value}` | **只读** |
| generate | `remembered_facts` | `{ref, value}` | **要引**（把 ref 吐回 `source_evidence_refs`）|

`CLAUDE.md` 第三节点名的那个陷阱正是反面：
`RetrievalEvidence.release pin` **字段名没变、含义变了**，
于是下一个人看到 `pin` 会以为检索结果可重放。
**这里换名字，不换形状** —— 同名而不同形状是安静的，换名字是响的。
"""

from __future__ import annotations

import ast
import json
import pathlib
from hashlib import sha256
from typing import Any

import pytest
from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.memory_prompt import (
    MEMORY_REF_PREFIX,
    field_key_of,
    memories_for_prompt,
    memory_ref,
    remembered_facts_for_prompt,
)
from fastagent_v2.worker.inference_model import _GENERATE_SYSTEM, ModelBackedInference
from fastagent_v2.worker.inference_validation import (
    GenerationRejected,
    ValidatedPlan,
    validate_generation,
)
from fastagent_v2.worker.model_policy import active_release
from fastagent_v2.worker.providers import (
    OpenAICompatibleChatClient,
    TransportResponse,
)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
PIPELINE = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "pipeline.py"
)
MEMORY_PROMPT = (
    REPOSITORY_ROOT
    / "packages"
    / "runtime"
    / "src"
    / "fastagent_v2"
    / "runtime"
    / "memory_prompt.py"
)
VALIDATION = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_validation.py"
)

CREDENTIAL_ENV = "FASTAGENT_MODEL_API_KEY_DEEPSEEK"

#: 一份商家声明 + 库里那几行。**刻意不是字典序**，理由与
#: `test_memory_reaches_the_prompt` 那份夹具逐字相同：
#: 第一版按字典序写，于是「按声明顺序」在「改成按字典序」的变异下照样全绿。
DECLARED = [{"key": "industry"}, {"key": "team_size"}, {"key": "pain_point"}]
STORED = {
    "industry": "做户外装备的跨境电商",
    "team_size": "两个人",
    "pain_point": "夜里没人回消息",
}
INDUSTRY_REF = "memory:industry"
TEAM_REF = "memory:team_size"


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
        "customer_visible_segments": [
            {"type": "text", "content_or_ref": "夜里的消息我们接着。"}
        ],
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
                    {
                        "type": "output_text",
                        "text": json.dumps(content, ensure_ascii=False),
                    }
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


def _validated_plan() -> ValidatedPlan:
    return ValidatedPlan(
        document={
            **_plan_document(),
            "origin": "model",
            "candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}],
        },
        frame={"id": "frame-1", "definition_id": "introduce"},
        definition={
            "id": "introduce",
            "goal": "answer the question",
            "content_materials": [],
            "allowed_strategy_set": ["acknowledge"],
            "information_slots": [],
            "exit_conditions": [],
            "time_rules": [],
        },
    )


def _generation(*, claims: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """一份 `validate_generation` 眼里的生成结果 —— **带上平台盖的那三个字段**。

    只校验模型那一半，校验的就是一份现实里不存在的文档。
    """

    document = _acknowledgement()
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


def _claim(*refs: str, text: str = "你们是两个人。") -> dict[str, Any]:
    """一条事实声明。**契约里存的是正文的 sha256，不是正文本身。**"""

    return {
        "text_hash": sha256(text.encode("utf-8")).hexdigest(),
        "source_evidence_refs": list(refs),
    }


def _pipeline_source() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _function(name: str) -> ast.FunctionDef:
    """管线里那个函数的 **AST**，不是它的字符串。

    ⚠️ 本仓踩过五次「守卫读到了自己的散文」：docstring 里复现一遍被 grep 的
    字面量，守卫就在钉它自己。**读 AST 时 docstring 是一个 `Constant`，
    与语句分得开** —— 所以下面凡是判「代码里有没有」的，都走结构。
    """

    tree = ast.parse(_pipeline_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"管线里没有 {name} —— 这条用例要跟着改")


def _without_docstring(node: ast.FunctionDef) -> ast.Module:
    """函数体，**去掉 docstring 那一条**，包成一棵能 `unparse` 的树。"""

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return ast.Module(body=body, type_ignores=[])


def _strings(tree: ast.AST) -> set[str]:
    """一棵子树里所有字符串字面量（docstring 已在上面剥掉）。"""

    return {
        child.value
        for child in ast.walk(tree)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


# ---------------------------------------------------------------------------
# 一 · 写字那一步终于看见记忆了（本批存在的理由那条测量）
# ---------------------------------------------------------------------------


def test_the_generate_request_carries_the_memories_with_their_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**走真的 `generate`，量线路上发出去的那个 body。**

    不是读 `generate_prompt` 的返回值：那只证明「那个函数会拼这个键」，
    而这一批要证的是**写字这一步真的收到了** ——
    批 J 有两条自己写的用例正是在这道缝上被探针照出来是弱的
    （「算出来了」不等于「被用上了」）。
    """

    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    shown, not_shown = memories_for_prompt(DECLARED, STORED)
    transport = _RecordingTransport(_model_reply(_acknowledgement()))
    inference = ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    )
    inference.generate(
        plan=_plan_document(),
        understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        materials={},
        knowledge={},
        remembered_facts=remembered_facts_for_prompt(shown),
        memories_not_shown=not_shown,
    )

    assert transport.calls, "provider 一次都没被调 —— 这条用例什么都没量"
    prompt = dict(json.loads(str(transport.calls[0]["payload"]["input"])))
    assert "remembered_facts" in prompt, (
        "写字那一步还是看不见记忆 —— 那「引了哪几条」就还是答不出来"
    )
    assert prompt["remembered_facts"] == [
        {"ref": INDUSTRY_REF, "value": STORED["industry"]},
        {"ref": TEAM_REF, "value": STORED["team_size"]},
        {"ref": "memory:pain_point", "value": STORED["pain_point"]},
    ], "ref 或顺序不对 —— 顺序是商家用 `memory_fields` 的声明顺序定的"
    assert prompt["memories_not_shown"] == 0


def test_an_empty_memory_set_is_still_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """**空着也发**，与 `knowledge_chunks` / `materials_already_sent` 同一个理由。

    「这个客户我们什么都没记住」与「没人告诉你有记忆这回事」
    **不该在模型眼里长得一样** —— 后者会让它照着自己的先验编。
    """

    monkeypatch.setenv(CREDENTIAL_ENV, "test-only-not-a-real-key")
    transport = _RecordingTransport(_model_reply(_acknowledgement()))
    ModelBackedInference(
        release=active_release(),
        client=OpenAICompatibleChatClient(transport=transport),
    ).generate(
        plan=_plan_document(),
        understanding={"event_id": "00000000-0000-4000-8000-000000000000"},
        envelope={"event_type": "customer_message"},
        materials={},
        knowledge={},
    )

    prompt = dict(json.loads(str(transport.calls[0]["payload"]["input"])))
    assert prompt["remembered_facts"] == []
    assert prompt["memories_not_shown"] == 0


def test_the_generate_key_is_not_the_one_understand_and_plan_use() -> None:
    """**两份记忆键不同名，因为它们不同形** —— `CLAUDE.md` 第三节那个陷阱。

    理解步与计划步收 `{field, value}`（只读），生成步收 `{ref, value}`（要引）。
    **同名而不同形状是安静的**：把 `{field, value}` 喂给生成步，
    schema 过得去、请求发得出、模型也会回一份看起来正常的东西 ——
    **而它引不出任何 ref**，于是引用集永远是空的，
    与「这一轮一条都没引」长得一模一样。
    """

    generate = request_shape.generate_prompt(
        strategy_id="s", strategy_reason_code="r", event_type="customer_message"
    )
    understand = request_shape.understand_prompt(
        goal="g", information_slots=[], event_type="customer_message"
    )

    assert "remembered_facts" in generate
    assert "known_memories" not in generate, (
        "生成步用了理解步那个键名 —— 而两者形状不同，这正是那个安静的错法"
    )
    assert "known_memories" in understand
    assert "remembered_facts" not in understand, (
        "理解步用了生成步那个键名 —— 它不引，给它 ref 只是多一份客户事实"
    )


def test_the_shape_the_generate_step_gets_is_ref_and_value() -> None:
    """**形状本身钉一条** —— 上一条钉的是键名，这一条钉的是键里面那个东西。

    只钉键名不够：把 `remembered_facts` 填成 `[{field, value}]`
    两条用例照样一起绿，**而模型拿不到任何可引的东西**。
    """

    shown, _ = memories_for_prompt(DECLARED, STORED)
    converted = remembered_facts_for_prompt(shown)

    assert converted, "转出来是空的 —— 这条用例什么都没量"
    for entry in converted:
        assert set(entry) == {"ref", "value"}, (
            f"生成步那一份的键不对：{sorted(entry)} —— 它要一个可引的 ref"
        )
    assert all(entry["ref"].startswith(MEMORY_REF_PREFIX) for entry in converted)
    # **值原样带过去** —— 转换只加 ref，不许顺手改正文。
    assert [entry["value"] for entry in converted] == [
        entry["value"] for entry in shown
    ]


def test_the_system_prompt_tells_it_to_cite_the_ref() -> None:
    """**递过去而不说怎么用，等于没递。**

    知识库那一族的先例：`knowledge_chunks` 那一行明写「用自己的话写、
    把 ref 放进 `source_evidence_refs`」。记忆这一份**照抄那条规矩** ——
    两种参考料两套说法，是模型把其中一种当成可以照抄的正文的方式。
    """

    assert "remembered_facts" in _GENERATE_SYSTEM, "提示词里没提这个键"
    block = _GENERATE_SYSTEM.split("remembered_facts", 1)[1][:400]
    assert "source_evidence_refs" in block, "没说要把 ref 引回来"
    assert "own words" in block, "没说用自己的话写 —— 那它会照抄记忆的原文"
    assert "invent" in block, "没说不许编一个 ref"


# ---------------------------------------------------------------------------
# 二 · 引一条没递过的记忆 = 拒（fail-closed，与知识库那一族同形）
# ---------------------------------------------------------------------------


def test_a_claim_may_not_cite_a_memory_this_turn_did_not_send() -> None:
    """**一条引错来源的事实声明，比一条没有来源的更糟。**

    后者会被 `evidence_required` 拦住、让模型说「不确定」；
    **前者读起来像有据可查** —— 而客户看到的是一句凭空而来的「你们是两个人」。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim("memory:annual_revenue")]),
            plan=_validated_plan(),
            materials={},
            remembered={INDUSTRY_REF},
        )

    assert raised.value.reason_code == "GENERATION_MEMORY_REF_UNKNOWN"
    assert "memory:annual_revenue" in str(raised.value)


@pytest.mark.parametrize(
    "offered", [None, set()], ids=["never-remembered", "remembered-nothing"]
)
def test_a_turn_that_sent_no_memory_allows_no_memory_ref(
    offered: set[str] | None,
) -> None:
    """**没递过不等于什么都能引** —— 默认 `None` 就是这个意思。

    与知识库那一族逐字同一个默认。两个 id 都要：
    `None` = 这一轮压根没走记忆那条路（旧调用点、没有 contact 的回合），
    `set()` = 走了而这个客户一条记忆都没有。
    **两种都不许引** —— 而如果默认是「不传就不查」，
    那么每一个忘了传的调用点都在 fail-open。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim(INDUSTRY_REF)]),
            plan=_validated_plan(),
            materials={},
            remembered=offered,
        )

    assert raised.value.reason_code == "GENERATION_MEMORY_REF_UNKNOWN"


def test_the_refusal_carries_the_ref_and_not_the_remembered_value() -> None:
    """**决策行里只放 ref，不放客户可见的字。**

    `memory_values.forbidden_value_reason` 的 docstring 记着那次事故：
    一条引用了它所拒的身份证号的拒绝记录，**已经把那个号写进审计面了**。
    记忆的值**就是**客户事实 —— 这条比知识库那条更该有。
    """

    with pytest.raises(GenerationRejected) as raised:
        validate_generation(
            generation=_generation(claims=[_claim(INDUSTRY_REF)]),
            plan=_validated_plan(),
            materials={},
            remembered=set(),
        )

    message = str(raised.value) + json.dumps(
        raised.value.context or {}, ensure_ascii=False
    )
    assert INDUSTRY_REF in message
    for value in STORED.values():
        assert value not in message, "拒绝记录里出现了记忆的值"


def test_a_memory_this_turn_did_send_passes() -> None:
    """**正面那一半** —— 没有它，上面几条可以靠「一律拒」全绿。"""

    shown, _ = memories_for_prompt(DECLARED, STORED)
    sent = {entry["ref"] for entry in remembered_facts_for_prompt(shown)}

    document = validate_generation(
        generation=_generation(claims=[_claim(INDUSTRY_REF, TEAM_REF)]),
        plan=_validated_plan(),
        materials={},
        remembered=sent,
    )

    assert document["claims"][0]["source_evidence_refs"] == [INDUSTRY_REF, TEAM_REF]


def test_a_non_memory_ref_is_not_judged_by_this_gate() -> None:
    """**它只管 `memory:` 那一族。**

    `source_evidence_refs` 里还有 understanding 那一侧的东西
    （`span:9f2c1b` 这种）—— 这道闸认前缀，
    **而一道把所有 ref 都往记忆集里比的闸会把那些全拒掉**：
    方向是 fail-closed 所以不出事故，而它会把整条链路堵死。
    """

    document = validate_generation(
        generation=_generation(claims=[_claim("span:9f2c1b")]),
        plan=_validated_plan(),
        materials={},
        remembered=set(),
    )

    assert document["claims"][0]["source_evidence_refs"] == ["span:9f2c1b"]


# ---------------------------------------------------------------------------
# 三 · 递给模型的那一份与许它引的那一份，是同一份
# ---------------------------------------------------------------------------


def test_the_prompt_and_the_validator_read_one_list() -> None:
    """**递给模型的那一份与许它引的那一份，在结构上不可能不是同一份。**

    ## 判据 2026-09-19 换过一次，而换的理由是复核点名的那件事

    第一版判的是「两个实参都写着 `turn_memories`」。
    **而 `main` 上那一段已经被批 J 抽成了 `_generate_and_validate`，
    并且它被调两遍**（首次尝试 + T367 的重跑）——
    那一段的 docstring 自己写着漂掉的方式是「**其中一处少传一个**」。

    照第一版的判据把 `remembered` 也做成一个参数，就正好落进那个形状：
    **重跑那一处少传一个** → `remembered` 是空集 → 而空集的语义是
    「这一轮没给过记忆，任何 `memory:` 引用都是编的」→
    **重跑系统性地、静默地拒掉每一条引记忆的回复**，而门禁全绿。

    **所以现在只有一个参数，校验器那一侧在 helper 里现推。**
    这一条因此判两件：

    1. 管线**算一次**（`remembered_facts_for_prompt` 只出现一处）；
    2. 那个 helper 里，`remembered=` 的值**是从它自己的形参推出来的**，
       不是另一个形参 —— 于是两个调用点漂不开。
    """

    source = _pipeline_source()
    assert source.count("remembered_facts_for_prompt(") == 1, (
        "不止一处在算 —— 两处各算一遍就会漂"
    )

    tree = ast.parse(source)
    helper = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_generate_and_validate"
        ),
        None,
    )
    assert helper is not None, (
        "管线里没有 `_generate_and_validate` —— 那一段又被内联回去了，"
        "这条用例要跟着改"
    )

    parameters = {
        argument.arg for argument in helper.args.args + helper.args.kwonlyargs
    }
    assert "remembered_facts" in parameters, "helper 不收记忆了"
    assert "remembered" not in parameters, (
        "`remembered` 又变成一个独立形参了 —— 那两个调用点就能各写一遍，"
        "而少传一个的那一次会静默拒掉每一条引记忆的回复"
    )

    prompt_arg: str | None = None
    validator_arg: str | None = None
    for node in ast.walk(helper):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "remembered_facts":
                prompt_arg = ast.unparse(keyword.value)
            if keyword.arg == "remembered":
                validator_arg = ast.unparse(keyword.value)

    assert prompt_arg == "remembered_facts", (
        f"提示词那一侧读的是 {prompt_arg} —— 它该直接读那个形参"
    )
    assert validator_arg and "remembered_facts" in validator_arg, (
        f"校验器那一侧读的是 {validator_arg} —— 它没有从提示词那一份现推"
    )

    # **两个调用点都要真的传它** —— 否则 helper 收了参数也没人给。
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("_generate_and_validate")
    ]
    assert len(calls) == 2, (
        f"`_generate_and_validate` 有 {len(calls)} 个调用点，"
        "而它的 docstring 说两个（首次尝试 + 重跑）—— 这条用例要跟着改"
    )
    for call in calls:
        passed = {keyword.arg for keyword in call.keywords}
        assert "remembered_facts" in passed, (
            f"第 {call.lineno} 行那个调用点没递记忆 —— "
            "那一次尝试会拒掉每一条引记忆的回复，而它只是「放弃重跑」"
        )


def test_the_ref_is_built_in_exactly_one_place() -> None:
    """**前缀只拼一次。**

    三个消费方（提示词、校验器、留痕）各拼一次，就是这道闸漂掉的方式：
    其中一处把 `memory:` 写成 `memory_`，校验器会拒掉每一条合法引用，
    **而那条拒绝读起来像模型在编** —— 与 `TURN_KEY_SQL` 同一条纪律。

    判据是**结构**：除了 `memory_prompt` 自己，没有第二个文件把前缀
    与别的东西拼在一起（f-string 或 `+`）。
    """

    for path in (PIPELINE, VALIDATION):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                inner = {
                    ast.unparse(value.value)
                    for value in node.values
                    if isinstance(value, ast.FormattedValue)
                }
                assert "MEMORY_REF_PREFIX" not in inner, (
                    f"{path.name} 自己在拼 ref —— 定义点在 {MEMORY_PROMPT.name}"
                )
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                assert ast.unparse(node.left) != "MEMORY_REF_PREFIX", (
                    f"{path.name} 自己在拼 ref —— 定义点在 {MEMORY_PROMPT.name}"
                )

    # 正面：定义点确实在那儿，而且两个方向互逆。
    assert memory_ref("industry") == INDUSTRY_REF
    assert field_key_of(INDUSTRY_REF) == "industry"
    assert field_key_of("industry") == "industry", (
        "拿一个已经是 field_key 的进来要原样出去 —— 留痕那一侧会这么用"
    )


# ---------------------------------------------------------------------------
# 四 · 留痕：引了哪几条，只记键不记值
# ---------------------------------------------------------------------------


def test_the_citation_row_records_keys_and_never_values() -> None:
    """**只记 field_key，不记值。**

    与 `_record_memories_sent` 同一条纪律，而这一行更危险：
    它手上有 `turn_memories`，**那一份里 `value` 就在旁边**。
    """

    body = _without_docstring(_function("_record_memories_cited"))
    strings = _strings(body)
    unparsed = ast.unparse(body)

    assert "inference.memories_cited" in " ".join(strings), "action 不对"
    assert "fields" in strings, "没记引了哪几条"
    assert "offered" in strings, "没记递过去哪几条 —— 那就减不出没用上的"
    assert "value" not in strings, "留痕里出现了值"
    assert "field_value" not in strings, "留痕里出现了值"
    assert 'entry["value"]' not in unparsed, "留痕里出现了值"

    # ⚠️ **不是数「`field_key_of` 出现过吗」** —— 那种判法在「两个键里只有一个
    # 过了转换」时照样绿（本仓记着「数错了的守卫看起来在守」那一条）。
    # 判据是**那两个键各自的值里都有一次调用**。
    written = {
        key.value: value
        for node in ast.walk(body)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert set(written) == {"fields", "offered"}, (
        f"写下去的键是 {sorted(written)} —— 这条用例要跟着改"
    )
    for name, value in written.items():
        assert "field_key_of" in ast.unparse(value), (
            f"`{name}` 记的是 ref 而不是 field_key —— "
            "与 `memories_sent` 那一行就对不上，两行相减减不出东西"
        )


def test_the_row_is_handed_refs_and_never_the_values() -> None:
    """**上一条看的是函数里面，这一条看的是喂进去的东西。**

    那个函数只认 ref，而**调用点手上有 `turn_memories`，里面 `value` 就在旁边** ——
    把 `entry["ref"]` 写成 `entry["value"]`，函数体一个字不用改，
    审计面上就多了一份客户事实，而**没有任何东西会变红**：
    留痕写得进、格式对得上、只有内容是客户的原话。
    """

    tree = ast.parse(_pipeline_source())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and "_record_memories_cited" in ast.unparse(node.func)
    ]
    assert calls, "没有任何地方调它 —— 零调用方"

    for call in calls:
        for keyword in call.keywords:
            text = ast.unparse(keyword.value)
            assert '"value"' not in text and "'value'" not in text, (
                f"`{keyword.arg}=` 喂的是记忆的值：{text}"
            )


def test_the_two_rows_are_a_pair() -> None:
    """**`memories_sent` 与 `memories_cited` 要么都在，要么都不在。**

    一行在而另一行不在时，「这一轮没走到生成」与「走到了生成一条都没引」
    **就分不开了** —— 而后者正是最该被数出来的那一种
    （知识库那边 `cited_refs = []` 与 `NULL` 分开，同一个道理）。

    判据是**两处的 `if` 读的是同两个值**，用 AST 取条件里的名字 ——
    读源码字符串会把注释里复现的条件也读进去（本仓踩过五次）。
    """

    tree = ast.parse(_pipeline_source())
    conditions: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        called = {
            ast.unparse(child.func)
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
        }
        for name in ("_record_memories_sent", "_record_memories_cited"):
            if any(name in call for call in called):
                conditions[name] = {
                    child.attr if isinstance(child, ast.Attribute) else child.id
                    for child in ast.walk(node.test)
                    if isinstance(child, ast.Attribute | ast.Name)
                }

    assert set(conditions) == {"_record_memories_sent", "_record_memories_cited"}, (
        f"只找到 {sorted(conditions)} —— 有一处不在 `if` 后面，这条用例要跟着改"
    )
    sent = conditions["_record_memories_sent"]
    cited = conditions["_record_memories_cited"]
    wanted = {"known_memories", "memories_not_shown"}
    assert wanted <= sent, f"递出那一行的条件是 {sorted(sent)}"
    assert wanted <= cited, (
        f"两处条件读的不是同两个值：递出那边 {sorted(sent)}，引用那边 {sorted(cited)}"
    )


def test_the_citation_row_is_written_by_the_step_that_holds_the_connection() -> None:
    """**一条能在回滚之后活下来的留痕，描述的是一个没发生过的回合。**

    判据是它**在词法上落在那个 `with … transaction(…) as connection` 里面** ——
    不是「调它的函数手上有个叫 `connection` 的形参」：`process_prepared`
    自己开事务，那种判法在它身上直接失效，**而失效的方向是这条用例什么都不查**
    （第一版就是那么写的，红在自己身上）。
    把这一行挪到 `with` 之后（或挪进另一个事务），这里红。
    """

    tree = ast.parse(_pipeline_source())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and "_record_memories_cited" in ast.unparse(node.func)
    ]
    assert calls, "没有任何地方调它 —— 零调用方"

    transactions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.optional_vars, ast.Name)
            and item.optional_vars.id == "connection"
            and "transaction" in ast.unparse(item.context_expr)
            for item in node.items
        )
    ]
    assert transactions, (
        "管线里找不到 `with … transaction(…) as connection` —— 这条用例要跟着改"
    )

    for call in calls:
        assert any(
            block.body[0].lineno <= call.lineno <= (block.end_lineno or call.lineno)
            for block in transactions
        ), f"第 {call.lineno} 行那次调用不在任何一个事务块里"


def test_the_ref_carries_no_version_and_says_so() -> None:
    """**记忆的 ref 没有版本号，而这是一个决定，不是一处遗漏。**

    知识库那一族是 `knowledge:<slug>:v<版本>` —— 照抄会得到一个**假的**版本号：
    `contact_memories` 一行会被 `remember` 再写一次同一个 field_key 盖掉，
    **库里只有当前值**。声明一个不能回放的版本号比不声明更糟
    （`RetrievalEvidence.release pin` 那一例逐字如此）。

    所以这一条钉两件：ref 里**没有**版本段，**而理由写在定义点上**。
    """

    assert memory_ref("industry").count(":") == 1, (
        "ref 里多了一段 —— 如果那是版本号，它是假的"
    )

    # ⚠️ **这一半读的是原文，包括注释** —— 本仓给模块常量写理由的惯例是
    # `#:` 注释，而 `ast` 看不见注释。**读原文的代价是「守卫读到自己的散文」**
    # （本仓踩过五次）。这里躲开它的方式是**扫的是另一个文件**：
    # 本文件里这句话出现两次（docstring 与断言），
    # **而它们一个字都进不了 `prose`** —— 自指在结构上不可能发生。
    prose = MEMORY_PROMPT.read_text(encoding="utf-8")
    assert "MEMORY_REF_PREFIX" in prose, "定义点不在这个文件里了 —— 这条用例要跟着改"
    assert "没有版本" in prose, "没写清为什么没有版本号 —— 下一个人会以为是漏了"
