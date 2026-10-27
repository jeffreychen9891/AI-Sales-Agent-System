"""T258：同一通会话里，回复不许换成另一种语言。

## 现场（2026-09-10 实测，逐字）

第三轮的三条回复里**一条英文、两条中文**。英文那条：

> `"I couldn't read the attached file, so I don't have your request yet."`

同一条链路、同一轮。**根因是零约束** —— 随船 profile 里没有语言相关字段，
`generation_constraints` 的闭合词表（ADR-0123）里也没有对应的词，模型自由发挥。

## 登记里那三问，以及它们今天各自的答案

| | 问题 | 答案 |
|---|---|---|
| ① | 约束名叫什么（要进 profile 词表、进 `content_hash`、重发 release）| **不需要** —— 见下 |
| ② | 谁声明：平台统一 vs 商家自定 | **用户 2026-09-20 裁：平台写死，一律中文** |
| ③ | 校验在哪一层 | **生成之后，与另两条丢弃逐字并排** |

**②那条裁定把①整个消掉了**：一律中文是**平台的**规则，不是某个打法声明的，
所以它不进那张闭合词表 —— 词表里的每一个词都是「某个打法声明它要什么」。
**schema / `content_hash` / 重发 release 三样都不需要**，
而那正是 T258 登记里说这一格贵的地方。

## ⚠️ 商家自己的素材不受这条管

调用方那个循环对 `material_ref` 段直接放行，所以**一份英文素材照样发得出去** ——
本闸管的只是**模型自己写的字**。这不是漏，是那条裁定的边界：
平台管的是「AI 用什么语言说话」，不是「商家能不能有英文素材」。
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from uuid import uuid4

import pytest
from fastagent_v2.runtime.generation_constraints import (
    ADVISORY_CONSTRAINTS,
    ENFORCED_CONSTRAINTS,
    KNOWN_CONSTRAINTS,
)
from fastagent_v2.worker.inference_validation import (
    LATIN_WORDS_FOR_ANOTHER_LANGUAGE,
    REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE,
    UNFOUNDED_CUSTOMER_FACT_REASON_CODE,
    GenerationRejected,
    ValidatedPlan,
    _is_another_language,
    validate_generation,
    validate_plan,
)
from fastagent_v2.worker.pipeline import (
    HANDOFF_REASON_CODES,
    LOOP_ESCALATION_REASON_CODES,
    TASK_WITHOUT_HANDOFF_REASON_CODES,
)

from tests.contracts.return_path_builders import understanding_with

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]
VALIDATION = (
    REPOSITORY_ROOT
    / "services"
    / "worker"
    / "src"
    / "fastagent_v2"
    / "worker"
    / "inference_validation.py"
)
OBSERVABILITY = REPOSITORY_ROOT / "scripts" / "stage_observability.py"

PROFILE = json.loads(
    (
        REPOSITORY_ROOT
        / "profiles"
        / "internal-demo-sales"
        / "profile.json"
    ).read_text(encoding="utf-8")
)

#: **一份中文素材，而它的 ref 本身有四个拉丁词。**
#:
#: `material:demo-price-plain:v1` 拆出来是 material / demo / price / plain ——
#: **恰好等于阈值**。所以「素材段放行」这一条不是纸上的：
#: 少了它，一条中文回复会因为它引的那份素材的**编号**被判成英文。
MATERIAL_REF = "material:demo-price-plain:v1"
MATERIALS = {
    MATERIAL_REF: "价格是一个月 1888，前十天免费试用，试用期内不收费，也不需要先付。"
}

#: 5.7 那一句（隔壁文件逐字取自运维 2026-09-17 06:18:58 的实测出站）。
#: 这里只借它做「两种丢法混在一起」的那一条。
UNFOUNDED = "您付的那 1888 是另一回事，它不按消息条数走。"


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
    # ⚠️ **还要是那个帧允许的。** 这里的夹具站在 `introduce` 帧上，
    # 而「能发消息」与「这个帧允许它」是两件事 —— 2026-09-23 演示包补上
    # `offer_material` 的定义之后，它排在字典序最前而 `introduce` 不允许它，
    # 这四条当场 `STRATEGY_NOT_IN_FRAME_ALLOWLIST`。
    # **判据先前漏了后半句**，而它一直绿是因为那时恰好没有这样的策略。
    allowed = set(PROFILE["target_frames"][0]["allowed_strategy_set"])
    strategy = next(
        name
        for name, body in PROFILE["strategies"].items()
        if "send_message" in body["allowed_intent_kinds"]
        and name != "wait"
        and name in allowed
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


def _generation(plan: ValidatedPlan, texts: list[str], *, quote: bool) -> dict[str, Any]:
    segments: list[dict[str, Any]] = [
        {"type": "text", "content_or_ref": text} for text in texts
    ]
    if quote:
        segments.append({"type": "material_ref", "content_or_ref": MATERIAL_REF})
    return {
        "schema_version": "1.0.0",
        "origin": "model",
        "strategy_id": plan.strategy_id,
        "customer_visible_segments": [dict(segment) for segment in segments],
        "proposed_intents": [
            {
                "kind": "send_message",
                "typed_payload": {
                    "content": {"segments": [dict(segment) for segment in segments]}
                },
                "evidence_refs": [],
            }
        ],
        "confidence": {
            "raw_score": 0.8,
            "calibrated_score": None,
            "assessment_version": "model-v1",
            "factor_codes": [],
        },
        "claims": [],
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


def _run(
    texts: list[str], *, quote: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """⚠️ **传 `unsourced` 就必须一起传 `customer_numbers`**（那个 `ValueError`）。"""

    plan = _plan()
    unsourced: list[dict[str, Any]] = []
    result = validate_generation(
        generation=_generation(plan, texts, quote=quote),
        plan=plan,
        materials=dict(MATERIALS),
        customer_numbers=(),
        last_outbound="",
        unsourced=unsourced,
    )
    return result, unsourced


def _texts(document: dict[str, Any]) -> list[str]:
    segments = document["proposed_intents"][0]["typed_payload"]["content"]["segments"]
    return [
        str(segment["content_or_ref"])
        for segment in segments
        if segment["type"] == "text"
    ]


#: 2026-09-10 实测那一句，**逐字**。
THE_MEASURED_SENTENCE = (
    "I couldn't read the attached file, so I don't have your request yet."
)


def test_the_measured_sentence_is_caught() -> None:
    """**本批存在的理由，而夹具是那一句实测的原话。**

    不自己编一句英文：编的那句会随判据一起漂，
    **而它是这条闸唯一一个真实样本**。
    """

    assert _is_another_language(THE_MEASURED_SENTENCE)


def test_chinese_survives() -> None:
    """对照。没有它，一个「一律判 True」的实现会让上面那条绿。"""

    assert not _is_another_language("价格是一个月 1888，前十天免费试用。")


def test_the_false_positives_that_would_cost_a_real_reply() -> None:
    """⚠️ **拦错的代价是一条本来能发的回复，所以这一组比上面那条更要紧。**

    每一条都是真实会出现的形状：

    * **中英混排仍然是中文** —— 「用 Excel 打开那份 pricing sheet」
      有四个拉丁词，只看词数就会中；
    * **链接** —— `https://example.com/pricing-sheet/download`
      拆出来是五个「词」，而它不是一句英文。先摘链接再数；
    * **产品名 / 短词** —— `OK`、`FastAgent v2`。
    """

    for text in (
        "用 Excel 打开那份 pricing sheet",
        "https://example.com/pricing-sheet/download",
        "报价表在这里 https://example.com/a/b/c/d/e",
        "OK",
        "FastAgent v2",
        "1888",
    ):
        assert not _is_another_language(text), f"误伤了：{text!r}"


def test_both_halves_are_required() -> None:
    """判据是「一个汉字都没有」**且**「拉丁词够多」—— 缺一不可。

    只看前一半：`1888` 会中。只看后一半：中英混排会中。
    """

    # 有汉字 + 很多拉丁词 -> 不算
    assert not _is_another_language(
        "这段话 has plenty of latin words in it 但它是中文"
    )
    # 没汉字 + 词不够 -> 不算
    assert not _is_another_language("OK fine")


def test_the_threshold_is_a_named_number_with_a_reason() -> None:
    """**四个词**，而这个数是为了不误伤定下的，不是随手取的。

    ⚠️ 它与本模块那条「说错比不说贵」**方向相反**：
    那一条管的是替客户认账（说错伤客户），所以宁可整份拒；
    这一条管的是用错语言（拦错伤的是一条本来能发的回复），
    所以**宁可漏一句短的**。两种错的代价不对称，松紧方向也就不同。
    """

    assert LATIN_WORDS_FOR_ANOTHER_LANGUAGE == 4
    # 恰好四个词要中，三个不中 —— **边界用例要站在边界上**
    assert _is_another_language("one two three four")
    assert not _is_another_language("one two three")


def test_it_is_not_a_declared_constraint() -> None:
    """**一律中文是平台的规则，不进那张闭合词表。**

    词表里每一个词都是「**某个打法**声明它要什么」（ADR-0123），
    而这一条不问打法、不问商家 —— 用户 2026-09-20 裁的是「平台写死」。

    ⚠️ **这一条红的时候多半是有人把它改成了商家自定** ——
    那是 T258 登记里另一个分支，它要 schema、要 `content_hash`、要重发 release。
    改之前先回去读那条登记，并且那是**用户的裁定**，不是作者的取舍。
    """

    # ⚠️ **读那两个 frozenset，不读文件正文。**
    #
    # 「文件里没出现 language 这个词」是一条**会自己变空的**断言 ——
    # 词表搬了家、改了名，它照样绿，而它声称守的事已经没人看着了。
    # 先钉住词表今天确实在、确实非空，再判它里面没有语言那一类码。
    assert KNOWN_CONSTRAINTS == ENFORCED_CONSTRAINTS | ADVISORY_CONSTRAINTS
    assert len(KNOWN_CONSTRAINTS) == 8, sorted(KNOWN_CONSTRAINTS)

    languageish = {
        code
        for code in KNOWN_CONSTRAINTS
        if "language" in code or "lang" in code or "chinese" in code or "locale" in code
    }
    assert languageish == set(), (
        f"闭合词表里出现了语言相关的码 {sorted(languageish)} —— "
        "一律中文是平台规则，不该由打法声明"
    )


def test_the_merchants_own_material_is_exempt() -> None:
    """**素材段不走这条闸** —— 一份英文素材照样发得出去。

    判的是那个循环：`material_ref` 段在进到任何一条丢弃判据之前就被放行。
    """

    document, unsourced = _run(["这个数不按条数走。"])
    segments = document["proposed_intents"][0]["typed_payload"]["content"]["segments"]

    assert [segment["type"] for segment in segments] == ["text", "material_ref"]
    assert unsourced == []


def test_a_refusal_that_is_all_one_reason_says_that_reason() -> None:
    """**整份拒时，理由码说的是「为什么一条都不剩」。**

    这一行原本写死「替客户认了一笔没说过的账」—— 在只有那一种丢法时是对的。
    到了三种，一条纯英文的整份拒会写着一件没发生过的事，
    而**接手的人照它去查**。

    判据是**全体一致**：丢法混在一起时没有哪个码说得全，那时仍用原来那个，
    `context.refused_because` 里带着全集。
    """

    with pytest.raises(GenerationRejected) as raised:
        _run([THE_MEASURED_SENTENCE], quote=False)

    assert raised.value.reason_code == REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE
    assert raised.value.context["refused_because"] == [
        REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE
    ]


def test_a_refusal_made_of_two_kinds_falls_back() -> None:
    """**丢法混在一起时，没有哪个码说得全** —— 那时仍用原来那个。

    ⚠️ 这一条是上面那条的**另一半**：只有它们两条在，
    「取第一个」这种实现才会红（那种实现会让上面那条绿）。
    真实构成读 `refused_because`，它带的是**全集**。
    """

    with pytest.raises(GenerationRejected) as raised:
        _run([UNFOUNDED, THE_MEASURED_SENTENCE], quote=False)

    assert raised.value.reason_code == UNFOUNDED_CUSTOMER_FACT_REASON_CODE
    assert raised.value.context["refused_because"] == sorted(
        {UNFOUNDED_CUSTOMER_FACT_REASON_CODE, REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE}
    )


def test_the_english_sentence_goes_and_the_chinese_one_stays() -> None:
    """**逐段判，不整份拒** —— 与另两条丢弃同一条纪律。

    一条中文一条英文进去，出来只剩中文那条，而英文那条进 `unsourced`
    （调用方据它写审计行）。
    """

    document, unsourced = _run(["这个数不按条数走。", THE_MEASURED_SENTENCE])

    assert _texts(document) == ["这个数不按条数走。"]
    # **草稿与载荷各记一条** —— 那个循环走两张段表，与 5.7 那一族同一个数
    #（`test_the_sentence_that_charges_the_customer_is_dropped` 钉的就是这个 2）。
    assert len(unsourced) == 2, f"草稿与载荷各该记一条：{unsourced}"
    assert {item["why"] for item in unsourced} == {
        REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE
    }
    assert {item["latin_words"] for item in unsourced} == {11}  # 量出来的


def test_the_refusal_is_countable_on_the_board() -> None:
    """**它的修法不在代码里，所以它更要看得见** —— 与隔壁那条逐字同一条理由。

    ⚠️ 它数得出来，靠的是上面那条（整份拒报实际的码）：
    在此之前一条纯语言的整份拒会被记成「替客户认账」，
    **这条查询一条都数不到，而隔壁那条会多数一条错的。**
    """

    board = OBSERVABILITY.read_text(encoding="utf-8")
    assert "another_language_refusal" in board
    assert REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE in board


def test_the_drop_records_the_count_and_not_the_words() -> None:
    """**记词数，不记正文** —— 与另外两条丢弃逐字同一条理由。

    正文是客户可见的话，而审计行会被人翻、被导出。
    """

    source = VALIDATION.read_text(encoding="utf-8")
    block = source[source.index('"why": REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE') :]
    block = block[: block.index("continue")]

    assert "latin_words" in block, "没记下那个数 —— 那条记录就说不出它为什么被判成英文"
    assert not re.search(r'"(text|content|own|segment)":', block), (
        "把正文记进审计行了 —— 客户可见的话不该出现在那里"
    )

# ---------------------------------------------------------------------------
# 三 · T384 的那条断言，配一个会在它失效时变红的守卫
# ---------------------------------------------------------------------------

#: **两条只活在提示词里的规则**（T384，2026-09-20 实测 0 命中）。
#:
#: 选这两条而不是那七条全放进来：剩下五条是**标识符**
#: （`usage_intent` 是槽名、`material:…` 是素材号），
#: 它们出现在生产代码里可能只是有人读了那个槽 —— **那不是「有执行者」**，
#: 这条守卫会为它假红。这两条是**散文规则**：
#: 它们一旦出现在生产代码里，只可能是有人给它写了执行者。
RULES_THAT_ONLY_EXIST_IN_THE_PROMPT = ("回执不是答案", "不必一问一答")

PRODUCTION_TREES = ("services", "packages", "scripts")

#: ⚠️ **反向探针不算生产代码。**
#:
#: 这不是我想出来的边界，是**探针自己撞出来的**：本批那支探针的 H 支
#: 就是「往生产代码里塞一句『回执不是答案』」，而那句话为此要写在
#: `scripts/probe_one_conversation_one_language.py` 里 ——
#: 于是**基线还没变异就先红了**（harness 当场拒绝开跑）。
#:
#: 排除它们**不削弱这条守卫**：一条规则的执行者不可能住在一支探针里。
def _is_probe(path: pathlib.Path) -> bool:
    return path.name.startswith("probe_")



def test_those_rules_still_have_no_enforcer() -> None:
    """**T384 说「它们没有执行者」—— 这条守卫让那句话在失效时说话。**

    与 `test_the_claim_that_the_ledger_does_not_exist_is_still_true` 同一个形状：
    一条「今天不存在」的断言，光写在文档里会**安静地过期**。

    ⚠️ **它红了不是坏事**，多半是有人真的建了闸。那时要做三件：

    1. **发生了什么** —— 某条只活在提示词里的规则现在有生产代码提到它了；
    2. **这是预期的吗** —— 是。T384 的到期条件 (1) 写的就是这一刻；
    3. **现在改成什么** —— 回 `docs/backlog/ideas.md` 的 T384，
       把那一条从这个元组里移走并在登记里记下它的执行者在哪（带坐标），
       **而不是把这条用例删掉**。
    """

    found: dict[str, list[str]] = {}
    for rule in RULES_THAT_ONLY_EXIST_IN_THE_PROMPT:
        hits = [
            str(path.relative_to(REPOSITORY_ROOT))
            for tree in PRODUCTION_TREES
            for path in (REPOSITORY_ROOT / tree).rglob("*.py")
            if "__pycache__" not in path.parts
            and not _is_probe(path)
            and rule in path.read_text(encoding="utf-8", errors="ignore")
        ]
        if hits:
            found[rule] = hits

    assert found == {}, (
        f"这些规则现在有生产代码提到了：{found} —— "
        "见 docs/backlog/ideas.md 的 T384（到期条件 (1)）"
    )

def test_a_refusal_nobody_hears_about_is_not_an_option() -> None:
    """**拒绝可以，但要有人被告知**（用户 2026-09-18 裁定，复核退回过一次）。

    这条整份拒**不产出出站行** —— 所以「它自己会走出来」的形态是
    **客户每轮收不到东西，而没有任何人被叫醒**。

    ⚠️ **这一条差一点就漏了，而漏法 T330 早写着**：
    不在任何一张表里的理由码**默认**落进完全无声，
    **而新增一个码时没有任何东西提醒加闸的人来看这些表**。
    5.7 那个码第一版就是这么漏的，复核当场退回。

    **单次不叫人**（一次判错可能只是这一轮的失误，建一条任务是本仓点过名的
    坏形状），由看板那一行数出来；**连续第二次才叫人**。
    """

    assert REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE in LOOP_ESCALATION_REASON_CODES

    # **另外两张表都不该收它** —— 它不是「该由人做的动作」，
    # 也不是「人已经接管了」。收错表 = 每一次判成英文都直接转人工。
    assert REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE not in HANDOFF_REASON_CODES
    assert (
        REPLIED_IN_ANOTHER_LANGUAGE_REASON_CODE
        not in TASK_WITHOUT_HANDOFF_REASON_CODES
    )

    # **与 5.7 那个码同表** —— 两条的形状逐字相同，分开放就是下一次的不一致。
    assert UNFOUNDED_CUSTOMER_FACT_REASON_CODE in LOOP_ESCALATION_REASON_CODES
