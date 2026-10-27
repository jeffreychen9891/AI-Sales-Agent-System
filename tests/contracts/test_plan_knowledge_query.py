"""计划步决定要不要检索，而决定必须带着「查什么」。

**裁定三**（`docs/plans/knowledge-base-schedule-2026-09-11.md` §5.2.1，用户 2026-09-11）：
模型在 plan 阶段说「这一轮需要查资料」才检 —— 不是每轮都检，也不是素材给不出才检。
它改变的是管线形状：`understand → plan → generate` 之间多出**第四步**，
而**是否检索这件事在 plan 就定了**。

这一批（知识库第 6 批 **6a**）只落**判据**：字段、校验器、两处字节常量。
**本批没有任何消费方** —— 检索层是 6b，出口计数是 6c。
「判据先于调用方」的代价与 [[T295]] / [[T301]] 同族，自审首部点了名，
**并登记成 [[T306]]**（独立审查 2026-09-12 P2-2：登记在审查文档里不算登记）。

**两条形状裁定各有一条用例守着**（裁定三连带后果 1、2）：
它不是 `generation_constraints` 里的一个词（那是闭合的**禁止**词表，ADR-0123），
也不是一个布尔（「查什么」只有 plan 步知道）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastagent_v2.runtime.contracts import model_output_schema, validate_inference_contract
from fastagent_v2.worker.inference import DeterministicMockInference, safe_fallback_plan
from fastagent_v2.worker.inference_validation import (
    PlanRejected,
    requested_knowledge_query,
    validate_plan,
)
from jsonschema import ValidationError

FRAME_ID = "welcome"


def _profile() -> dict[str, Any]:
    return {
        "identity": {"version": "1.0.0"},
        "target_frames": [
            {
                "id": FRAME_ID,
                "goal": "greet",
                "entry_conditions": [],
                "exit_conditions": [],
                "information_slots": [],
                "allowed_strategy_set": ["acknowledge"],
                "content_materials": [],
                "time_rules": [],
                "return_confirmation": {"policy": "optional", "tone_material_ref": None},
            }
        ],
        "strategies": {
            "acknowledge": {
                "required_slots": [],
                "allowed_intent_kinds": ["send_message"],
                "generation_constraints": [],
            }
        },
    }


def _aggregate() -> dict[str, Any]:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "generation": 1,
        "state_version": 1,
        "control_mode": "ai",
        "target_stack": [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "definition_id": FRAME_ID,
                "goal": "greet",
                "status": "active",
                "slots": {},
                "entered_at": "2026-09-12T00:00:00Z",
                "last_progress_at": "2026-09-12T00:00:00Z",
                "parent_frame_id": None,
                "frame_generation": 0,
                "resume_confirmation_policy": "optional",
            }
        ],
    }


def _understanding() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "event_id": "33333333-3333-4333-8333-333333333333",
        "facts": [],
        "intents": [{"intent_label": "unclassified", "confidence": 1.0}],
        "slot_updates": [],
        "interruption": {"relation": "same_goal", "suggested_frame_definition_id": None},
        "sentiment": "neutral",
        "risk_markers": ["none"],
        "customer_commitments": [],
        "uncertainties": [],
        "turn_summary": "用例夹具：这一轮的摘要。",
        "model_evidence": {
            "provider": "mock",
            "model": "m",
            "prompt_release": "p",
            "latency_ms": 0,
            "token_usage": {"input": 0, "output": 0},
        },
    }


def _plan(**overrides: Any) -> dict[str, Any]:
    """一份合法的模型计划，可以按字段覆写。

    **走 `DeterministicMockInference().plan`**，不是手写一份字面量 ——
    那个替身是生产里 `FASTAGENT_MODEL_PROVIDER=mock` 真正走的那一支，
    所以这些用例的起点与 e2e 的起点是同一个形状。
    """

    plan = DeterministicMockInference().plan(
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
    )
    plan.update(overrides)
    return plan


def _validated(**overrides: Any) -> Any:
    return validate_plan(
        plan=_plan(**overrides),
        understanding=_understanding(),
        aggregate=_aggregate(),
        profile=_profile(),
    )


def test_a_plan_without_the_field_still_validates() -> None:
    """**缺席是合法的，而这一条是本批最该被读到的那个决定。**

    字段**没有**进 `required`，理由是量出来的（见
    `test_it_is_optional_because_nothing_makes_the_provider_enforce_required`）：
    payload 里没有任何东西要求供应商强制必填项，**所以把一个新键设成必填，
    等于让「模型这次没写这个键」变成整份计划被拒 → 回落保守计划 → 不发消息**。
    那正是本仓 2026-09-11 运维报障的那条路径，而当时每一道门禁都是绿的。
    """

    plan = _plan()
    assert "knowledge_query" not in plan
    validate_inference_contract("plan-result", plan)
    assert _validated().strategy_id == "acknowledge"


def test_a_query_string_passes_the_contract_and_the_validator() -> None:
    """正面：带着查询串的模型计划一路走通。"""

    plan = _plan(knowledge_query="七天无理由退货怎么算运费")
    validate_inference_contract("plan-result", plan)

    validated = _validated(knowledge_query="七天无理由退货怎么算运费")
    assert requested_knowledge_query(validated.document) == "七天无理由退货怎么算运费"


def test_a_boolean_is_not_an_acceptable_knowledge_query() -> None:
    """**裁定三连带后果 2**：只给布尔，等于把「查什么」推给检索层去猜。

    契约层直接拦住，所以「退化成一个开关」不是一条可走的路 ——
    **不是靠谁记得不要那样写**。
    """

    with pytest.raises(ValidationError) as rejected:
        validate_inference_contract("plan-result", _plan(knowledge_query=True))
    # **钉住类型与字段名**（独立审查 2026-09-12，P3-2）：先前写的是
    # `raises(Exception)`，它会为**任何**异常变绿 —— 包括夹具自己因无关原因炸掉。
    # 「拦住了」与「炸在别处」必须分得开，而同族那条 blank 用例本来就钉了 reason_code。
    assert "knowledge_query" in str(rejected.value)


@pytest.mark.parametrize("query", ["", "a", "  ", "运费规则", "查" * 5000])
def test_no_query_string_can_cost_the_customer_the_reply(query: str) -> None:
    """**这一条是处置独立审查 P1-1 留下的那个性质**，它取代了原来的「上下界」两条。

    第一版给字段设了 `minLength: 2` / `maxLength: 200`。审查方实测出代价：
    `""` / `"a"` / 201 字都被**契约层**拒 → `ProviderMalformedOutput` →
    `_persist_no_send(reason_code="PROVIDER_MALFORMED_OUTPUT")`，**无重试、不派待办、
    理由码不指名哪个字段**；而 `"  "` 走本批当时新加的那道闸 → 保守计划 →
    `no_message` → 同样不发消息。**一个可选的检索提示写歪，代价是客户这一轮收不到回复。**

    三条依据说明那个形状不该留，其中两条是本批自己写下的：
    (1) 自审 §三 论证「不设必填」的理由（模型少写一个键不该吃掉回复）
        对「写了但写歪」同样成立；
    (2) **裁定六**（§5.3.2）：检索失败照常生成并发消息 —— 一条不可用的检索请求
        在语义上就是这一轮检不成；
    (3) 本批自己证过 payload 里没有 `strict`，**而供应商不强制 `required`，
        同样不强制 `minLength`** —— 同一个前提，第一版只用在了 `required` 那一侧。

    所以契约层**不再给这个字段设任何长度界**，`minLength` / `maxLength` 都没了，
    而这条用例钉的是那个性质本身：**任何字符串都不该让一份计划作废。**
    """

    plan = _plan(knowledge_query=query)
    validate_inference_contract("plan-result", plan)  # 契约层放过
    assert _validated(knowledge_query=query).strategy_id == "acknowledge"  # 校验器也放过


def test_absent_null_and_blank_all_read_as_no_request() -> None:
    """**三种写法收敛成一个答案**，而这就是 `requested_knowledge_query` 存在的理由。

    缺席、`null`、只有空白的串在语义上是同一件事。让检索层、出口计数、审计行
    各读一遍，就是三处会各自漂移的判断；而其中任意两处不一致时，
    「模型没请求检索」与「请求了但平台没看见」在数据里长得一模一样 ——
    与 T302（understanding 被砍过没有按会话可查的记录）是同一个形状。
    """

    assert requested_knowledge_query(_plan()) is None
    assert requested_knowledge_query(_plan(knowledge_query=None)) is None
    assert requested_knowledge_query({"knowledge_query": "   "}) is None


def test_the_query_is_read_back_stripped() -> None:
    """两端的空白不是查询串的一部分。"""

    assert requested_knowledge_query({"knowledge_query": "  运费规则  "}) == "运费规则"


def test_a_blank_query_is_read_as_no_request_not_as_a_rejected_plan() -> None:
    """**这条用例记着一次被改掉的决定**（独立审查 2026-09-12，P1-1）。

    第一版在这里抛 `KNOWLEDGE_QUERY_BLANK`，为的是把「请求了检索但没检到」
    与「模型没想好要查什么」在数据里分开 —— **那个区分要保留，但代价定错了**：
    拒一份计划的下游是「这一轮不回消息」，而它换来的只是一个更好看的理由码。

    **区分挪到证据面上**：「请求串不可用」是 6b 的 `retrieval_evidence`
    要分的第四态（前三态：没请求、请求了没检到、understanding 被砍过）。
    写进 **T306** 的到期条件，不是留在一句注释里。
    """

    plan = _plan(knowledge_query="  ")
    validate_inference_contract("plan-result", plan)

    validated = _validated(knowledge_query="  ")
    assert validated.strategy_id == "acknowledge", "空白的请求串不许吃掉这一轮的回复"
    assert requested_knowledge_query(validated.document) is None


def test_a_safe_fallback_plan_may_not_request_retrieval() -> None:
    """保守计划是平台自己造的确定性安全阀，**它没有「要查什么」的判断可言**。

    允许它带查询串，等于让一条平台写的记录声称模型做过一个决定 ——
    而裁定三 §4 说丙这条路的真实好处正是「决定本身被记录下来」。
    一份分不清作者的记录答不了那个问题。
    """

    plan = safe_fallback_plan(
        strategy_id="no_message",
        reason_code="PLAN_REJECTED",
        aggregate=_aggregate(),
    )
    plan["knowledge_query"] = "运费规则"

    with pytest.raises(PlanRejected) as rejected:
        validate_plan(
            plan=plan,
            understanding=_understanding(),
            aggregate=_aggregate(),
            profile=_profile(),
        )
    assert rejected.value.reason_code == "FALLBACK_REQUESTS_RETRIEVAL"


def test_the_platform_fallback_writes_null_rather_than_omitting_it() -> None:
    """**平台自己造的计划明写 `null`。**

    读法上缺席与 `null` 同义，所以这一条不是为了读法 —— 是为了让
    「平台决定不查」与「没人做过这个决定」在一份存下来的计划里分得开。
    """

    plan = safe_fallback_plan(
        strategy_id="no_message",
        reason_code="PLAN_REJECTED",
        aggregate=_aggregate(),
    )

    assert "knowledge_query" in plan
    assert plan["knowledge_query"] is None
    assert requested_knowledge_query(plan) is None


def test_it_is_not_a_word_in_the_closed_constraint_vocabulary() -> None:
    """**裁定三连带后果 1**：`generation_constraints` 是闭合词表（ADR-0123），
    而「要检索」不是一条约束 —— 约束管的是生成**不许**做什么。

    往闭合词表里塞一个动作，下一个人读词表时会读到一个不是约束的约束。
    这一条从反面钉：把它当成一个约束名写进去，被既有那道闸拒掉。
    """

    with pytest.raises(PlanRejected) as rejected:
        _validated(generation_constraints=["knowledge_query"])
    assert rejected.value.reason_code == "GENERATION_CONSTRAINT_NOT_DECLARED"


def test_the_prompt_tells_the_model_what_to_write_there() -> None:
    """**一个没被描述过的键，模型永远给缺省值** —— T34 就是这么发现的
    （`slot_updates` 列为必填却从没被描述，于是真实供应商每次都返回 `[]`）。

    所以提示词必须说三件事：它是什么、**写查询串而不是客户原话**、
    不写的后果。「那这个呢」检不出任何东西，这句是裁定三里的原话。
    """

    from fastagent_v2.worker.inference_model import _PLAN_SYSTEM

    assert "knowledge_query" in _PLAN_SYSTEM
    assert "what to look up, not the" in _PLAN_SYSTEM
    assert "retrieves nothing" in _PLAN_SYSTEM
    assert '"knowledge_query": null' in _PLAN_SYSTEM, "示例里要有它，否则模型照示例抄"


def test_it_is_optional_because_nothing_makes_the_provider_enforce_required() -> None:
    """**钉的是前提，不是结论。**

    「设成可选」这个决定只在一个前提下成立：payload **没有**让供应商强制必填项
    （Responses 的 `text.format` 里没有 `strict`）。前提在，omit 就只是
    「这一轮不检索」；前提不在了，omit 会让整份计划被拒。

    **所以哪天有人加了 `strict: true`，这一条会红**，而那时该做的是
    重新判「要不要设成必填」，不是把这条断言删掉。
    本仓为「钉数字而不钉尺子」已经付过四次钱 —— 留痕在
    `docs/reviews/prompt-byte-unit-back-to-wire-self-review-2026-09-12.md` 与
    `tests/contracts/test_prompt_bytes.py` 的模块 docstring 里，**两份都在仓内**。
    （独立审查 2026-09-12 **P3-1**：先前这里引的是一份**未提交**的交接文件，
    合并之后没人查得到。）
    """

    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import request_payload

    binding = active_release().role("PLAN_CAPABILITY")
    payload = request_payload(binding, system_prompt="", user_prompt="")

    assert binding.api_compatibility == "openai-responses-v1"
    assert "strict" not in payload["text"]["format"], (
        "供应商现在会强制必填项了 —— 重新判 knowledge_query 要不要设成必填"
    )
    assert "knowledge_query" not in model_output_schema("plan-result")["required"]


def test_what_the_field_costs_per_plan_turn_is_measured_not_estimated() -> None:
    """**两处字节常量的差值现量现比**，量的是真实 payload 的构造点。

    `providers.request_payload` 是生产那一个构造点（独立审查 2026-09-12 P1-1 逼出来的），
    `_WIRE_JSON` 是它序列化的那一套参数。**这条用例不写「大约」**：
    它把字段从 schema 的副本里删掉、把提示词里那一段减掉，
    两个差值必须逐位等于 `registry.py` 注释里写的 **+456 / +370**。

    量出来的事实（处置 P1-1 之后重测）：
    plan 的固定底 **6,617 → 7,443 / 16,384 = 45.43%**，宽裕。
    **P1-1 的处置让这三个数字全变了一次**（去掉两个长度界、description 收短、
    提示词那一段重写），所以它们是重测的，不是上一版抄下来的。
    """

    from copy import deepcopy

    from fastagent_v2.profiles.registry import (
        MAX_MODEL_INPUT_BYTES,
        PLATFORM_PROMPT_BYTES,
        PLATFORM_RESPONSE_SCHEMA_BYTES,
    )
    from fastagent_v2.worker.inference_model import _PLAN_SYSTEM
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import _WIRE_JSON, request_payload

    def wire(value: object) -> int:
        return len(json.dumps(value, **_WIRE_JSON).encode("utf-8"))

    schema = model_output_schema("plan-result")
    without_field = deepcopy(schema)
    del without_field["properties"]["knowledge_query"]
    assert wire(schema) - wire(without_field) == 456

    # **提示词那一边有两段，不是一段** —— 第一次写这条用例时只减了说明段，
    # 差值算出 334 而常量是 364，**30 字节的缺口是示例 JSON 里那一行**。
    # （P1-1 处置后两段分别是 340 与 30。）
    # 两段分开断言，好让下一个改提示词的人看得出钱花在哪。
    #
    # ⚠️ **下面那两行是结构锚点：`_PLAN_SYSTEM` 里的这两句正被当切片边界用。**
    # 起点 `- knowledge_query is optional:`、终点
    # `- Do not write customer-visible text here.` —— 中间那段就是本条要量的钱。
    # **于是「在终点那一行之前插一条新指令」会把新指令算进 knowledge_query 的账上。**
    # 实测过（2026-09-13，裁定一 ②）：新加的必填槽位指令第一版插在终点之前，
    # 这条断言当场报 **752** 而不是 340 —— **报的数是真的，指的方向是错的**：
    # 它看起来像「knowledge_query 那段变大了」，而真相是有人在它后面加了别的东西。
    #
    # **写提示词的人不知道这里有个锚点**，所以这段注释是写给他的：
    # 往 `_PLAN_SYSTEM` 加条目时，**加在这两行之外**
    # （新指令后来挪到了 `generation_constraints` 之后，那里既在锚点之外、
    # 话题上也更近）。这与反向探针那一族的「锚点没命中不是通过」同形，
    # 只是这次锚点在**提示词**里，不在探针脚本里。
    start = _PLAN_SYSTEM.index("- knowledge_query is optional:")
    end = _PLAN_SYSTEM.index("- Do not write customer-visible text here.")
    without_bullet = _PLAN_SYSTEM[:start] + _PLAN_SYSTEM[end:]
    example_line = '  "knowledge_query": null,\n'
    assert example_line in _PLAN_SYSTEM
    assert wire(_PLAN_SYSTEM) - wire(without_bullet) == 340
    assert wire(_PLAN_SYSTEM) - wire(_PLAN_SYSTEM.replace(example_line, "")) == 30
    assert wire(_PLAN_SYSTEM) - wire(without_bullet.replace(example_line, "")) == 370

    floor = PLATFORM_PROMPT_BYTES["PLAN_CAPABILITY"] + PLATFORM_RESPONSE_SCHEMA_BYTES[
        "PLAN_CAPABILITY"
    ]
    # **7,443 → 7,855（+412），2026-09-13 裁定一 ② 重量。**
    # plan 的 system prompt 加了一句指令（`required_slot_requests` 必须点名
    # `required_slots_by_strategy` 里仍然空着的槽位），而本行量的是
    # 「平台提示词 + 响应 schema」这个与 profile 无关的底。
    # **这一行就是那张表的注释里写的那句话在生效**：
    # 「下一个往任何提示词里加字的人，先重推它。」
    # 上面三条 `knowledge_query` 的差值（456 / 340 / 30 / 370）**一个都没变** ——
    # 新指令插在 `generation_constraints` 之后、`knowledge_query` **之前**，
    # 所以它不在那两个锚点切出来的那一段里。**第一版插在那一段里面，
    # 于是这条用例报 752 而不是 340**，那是它抓到的第一件事。
    # **7,855 → 8,057（+202），2026-09-19 批 G 重量。**
    # plan 的 system prompt 多了三行：一轮可以带一条以上 `send_message`、
    # 按列出的顺序发；回复要说「我先把 X 讲清楚」时再列一条把 X 发出去。
    # **投递侧本来就有序**（`ORDER BY created_at, ordinal, id`，T109 / 迁移 044），
    # 缺的一直是没有任何一句话告诉模型可以发第二条。
    # 那三条 `knowledge_query` 的差值（456 / 340 / 30 / 370）**又一次一个都没变** ——
    # 新那三行同样插在 `knowledge_query` **之前**。
    # **8,057 → 8,253（+196），2026-09-19 批 J / T367 重量。**
    # plan 的 system prompt 多了一句讲 `unkept_promises`：它非空时这是重跑，
    # 把兑现那一条列出来。那三条 `knowledge_query` 的差值**第三次一个都没变** ——
    # 新那一句同样插在 `knowledge_query` 之前。
    # 2026-09-21（一条说一件事）8,253 -> 8,428（+175）：计划步提示多了
    #「一条讲不完就列第二个候选」那一句。
    # 2026-09-23（运维缺陷 19）9,310 -> 9,574（+264）：计划步多了讲
    # `pending_question` 的那条规则，同样插在 `knowledge_query` 之前，三条差值不动。
    assert floor == 9574
    assert floor < MAX_MODEL_INPUT_BYTES // 2, "plan 的固定底吃掉一半上限，这一格要重新排"

    # **常量不是凭空写的**：它们必须小于一个真实空请求，而那个请求里除了
    # 提示词与 schema 还有 model / stream / reasoning / temperature 这些固定键。
    binding = active_release().role("PLAN_CAPABILITY")
    real = wire(request_payload(binding, system_prompt=_PLAN_SYSTEM, user_prompt=""))
    assert floor < real, "两处常量声称的固定底比真实空请求还大，其中一处量错了"
