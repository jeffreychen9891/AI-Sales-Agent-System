"""`entry_cost` predicts what `providers.py` measures, and this proves it does.

**A predictor validated against nothing is the defect this module was written to
fix.** The constant it replaces (`PER_MATERIAL_OVERHEAD_BYTES = 197`) was itself
derived from a measurement once, in 2026-09, and then drifted from the thing it
predicted without anything going red -- because nothing ever compared the two.

So the case below does not assert a number. It builds a real request the way
`inference_model.generate` builds one, serialises it the way `providers.py`
serialises one, and asserts that adding a material moves the measured size by
**exactly** what `entry_cost` said it would. Residual zero, not residual small:
`entry_cost` is an evaluation of the same expression, not an approximation of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastagent_v2.runtime.contracts import model_output_schema
from fastagent_v2.runtime.prompt_bytes import (
    budgeted_entry_cost,
    entry_cost,
    text_cost,
)
from fastagent_v2.worker import inference_model
from fastagent_v2.worker.providers import _WIRE_JSON

ROOT = Path(__file__).resolve().parents[2]

#: The real corpus, not invented strings. The bug being fixed only showed up at
#: the ends of a real length distribution -- an all-ASCII fixture would have
#: reproduced neither half of it.
MATERIALS = json.loads(
    (ROOT / "profiles/internal-demo-sales/materials.json").read_text(encoding="utf-8")
)["materials"]


def _request_bytes(materials: list[tuple[str, str]]) -> int:
    """A request in the **same byte unit** `providers.py` measures — not its shape.

    The two serialisation calls are the point of the whole module: the inner one
    is `ensure_ascii=False` (what `generate` writes) and so is the outer one --
    **the wire**, which is what the transport is actually measured on.

    **Both statements in this paragraph used to say the opposite** (`ensure_ascii=True`
    for the outer; "spelled out here rather than imported"). The unit was corrected
    by the 字节口径回正 batch, and the import is now deliberate: the outer flags come
    from `providers._WIRE_JSON`, so changing the gate's encoder turns this file red
    instead of letting the two drift apart silently. 独立审查 2026-09-12, P2-1 --
    the old wording had become an argument against the very fix it sits inside.

    **It is NOT what `providers.py` actually sends, and must never be used to
    measure a baseline** (独立审查 2026-09-11, P3-1). The real Responses payload
    is `{"model", "instructions", "input": <string>, "stream", "text", …}` and
    the chat one uses `messages`; this builds neither, and omits `stream`,
    `reasoning`, `max_output_tokens` and `temperature`.

    **Every case here measures a difference**, and the outer shape cancels in a
    difference — which is exactly why the residual can be zero while the shape
    is wrong. An absolute number read off this helper would be wrong by whatever
    those missing fields weigh, and **a wrong baseline is the defect P1-1 of the
    same review found in the budget derivation**: 8,321 was measured with an
    empty `understanding`, and every turn carries a real one.

    **2026-09-12: that warning was then ignored, by the batch that corrected the
    unit.** Its budget derivation read 7,936 off this helper; the real payload is
    8,012, so the constant was 76 bytes fail-open. The baseline now comes from
    `providers.request_payload` and is pinned by
    `test_the_empty_request_baseline_is_measured_from_the_real_payload`.
    **A warning in a docstring is not a guard** — this paragraph said exactly the
    right thing and changed nothing.
    """

    user_prompt = json.dumps(
        {
            "strategy_id": "s",
            "strategy_reason_code": "r",
            "generation_constraints": [],
            "candidate_intents": [],
            "required_slot_requests": [],
            "understanding": {},
            "event_type": "customer_message",
            "content_materials": [{"ref": ref, "text": text} for ref, text in materials],
        },
        ensure_ascii=False,
    )
    payload = {
        "model": "m",
        "input": [
            {"role": "system", "content": inference_model._GENERATE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "g",
                "schema": model_output_schema("generation-result"),
            }
        },
    }
    return len(json.dumps(payload, **_WIRE_JSON).encode("utf-8"))


def test_entry_cost_is_exact_for_every_real_material() -> None:
    """Residual zero on all 151, not "close enough on average".

    **Average is how the old constant survived.** `197 + len(utf8)` summed over
    this same corpus to 49,820 against a true 50,124 -- 1% out, and wrong by up
    to 283 bytes per material in the direction that lets an over-budget package
    seal.
    """

    base = _request_bytes([])
    for ref, text in MATERIALS.items():
        marginal = _request_bytes([(ref, text)]) - base
        assert marginal == entry_cost(ref, text), (
            f"{ref}: request grew by {marginal} bytes, entry_cost said "
            f"{entry_cost(ref, text)}"
        )


def test_entry_cost_is_additive_across_a_whole_turn() -> None:
    """One material at a time proving exact does not prove a turn does.

    JSON arrays add a separator per element after the first, and a predictor
    that is exact per item can still be wrong per turn by the comma count. This
    is the case that would have caught that.
    """

    picked = list(MATERIALS.items())[:20]
    base = _request_bytes([])
    measured = _request_bytes(picked) - base
    predicted = sum(entry_cost(ref, text) for ref, text in picked)

    # **Two bytes per join, not one** -- this case caught the author guessing
    # one. `generate` calls `json.dumps(..., ensure_ascii=False)` and passes no
    # `separators`, so the array separator is the default `", "`: a comma *and a
    # space*. `providers.py` uses `separators=(",", ":")` on the outer payload,
    # which is what made one byte the intuitive answer -- but that flag applies
    # to the payload, not to the already-serialised string inside it.
    #
    # The per-entry cost cannot know how many neighbours it has, so a turn-level
    # budget has to add this itself.
    assert measured - predicted == 2 * (len(picked) - 1), (
        f"turn of {len(picked)} materials: measured {measured}, predicted {predicted}"
    )


def test_chinese_costs_its_utf8_length_because_the_wire_is_utf8() -> None:
    """**这条用例先前断言的是 2×，而那是错口径下的事实。**

    旧版的理由是「转义之后一个中文字六字节、UTF-8 三字节，所以到闸那里翻倍」——
    观察没错，**而那道闸量的是一个没有任何东西发送的串**。
    httpx 发的是 UTF-8，所以线路上一个中文字就是它的 UTF-8 长度，**不翻倍**。

    **留着这条用例而不是删掉**：它会在有人把口径改回转义时变红 ——
    而那正是这一批在修的那一次。
    """

    chinese = "请问你们这个产品怎么收费" * 4
    ascii_text = "how much does this product cost" * 4

    chinese_ratio = text_cost(chinese) / len(chinese.encode("utf-8"))
    ascii_ratio = text_cost(ascii_text) / len(ascii_text.encode("utf-8"))

    assert 0.9 < chinese_ratio < 1.1, f"中文被按 {chinese_ratio:.2f}× 计 —— 口径退回转义了"
    assert 0.9 < ascii_ratio < 1.1, ascii_ratio


@pytest.mark.parametrize("text", ["", 'a "quote"', "back\\slash", "line\nbreak", "emoji 🙂"])
def test_the_cost_survives_everything_json_has_to_escape(text: str) -> None:
    """Quotes, backslashes, newlines and astral characters each escape twice.

    They are rare in the corpus and would each be a silent under-count in any
    hand-derived constant. Here they need no special handling at all, which is
    the argument for computing rather than fitting.
    """

    base = _request_bytes([])
    ref = "material:probe-escaping:v1"
    assert _request_bytes([(ref, text)]) - base == entry_cost(ref, text)


def test_the_budget_form_over_counts_a_turn_by_exactly_two_bytes() -> None:
    """`budgeted_entry_cost` charges every material for a separator it may not have.

    A turn of n materials carries n-1 separators, so charging n is high by
    exactly one separator -- two bytes, whatever n is. **Exactly, not roughly**:
    an approximation that nothing pins is how `PER_MATERIAL_OVERHEAD_BYTES`
    became wrong by 283 bytes without anything noticing.

    High is the safe side. The alternative -- counting separators exactly --
    means giving every bucket in `_check_stage_budget` a companion counter, on
    an admission path, to recover two bytes.
    """

    for n in (1, 2, 20, len(MATERIALS)):
        picked = list(MATERIALS.items())[:n]
        measured = _request_bytes(picked) - _request_bytes([])
        budgeted = sum(budgeted_entry_cost(ref, text) for ref, text in picked)
        assert budgeted - measured == 2, f"n={n}: budgeted {budgeted}, measured {measured}"


def test_the_empty_request_baseline_is_measured_from_the_real_payload() -> None:
    """**`MATERIAL_BYTES_BUDGET` 的推导式从哪个数开始，由这一条钉住。**

    独立审查 2026-09-12 P1-1：这个基线先前是从 `_request_bytes` 读的绝对值，
    **而那个 helper 的 docstring 自己写着「must never be used to measure a
    baseline」** —— 它缺 `stream` / `reasoning` / `max_output_tokens` /
    `temperature`，`input` 用的还是 chat 的 message 数组。
    量出来是 **7,936 对真实 8,012，差 76 字节，方向是 fail-open**。

    **这个数被推导过四次，前三次都是审查窗口抓的、一次都不是门禁。**
    所以这一批留下的不是一个更对的数，是这条用例：它走
    `providers.request_payload` —— **生产真正用的那个构造点，不是第四份形状**。

    它同时钉住那条算式：`16,384 − 基线 − understanding − 安全余量 = 预算`。
    任何一项改了而常数没跟着改，这里红。
    """

    from fastagent_v2.profiles.registry import MATERIAL_BYTES_BUDGET, MAX_MODEL_INPUT_BYTES
    from fastagent_v2.runtime import request_shape
    from fastagent_v2.runtime.knowledge_bound import KNOWLEDGE_PROMPT_BYTES
    from fastagent_v2.worker.material_selection import (
        LAST_OUTBOUND_BYTES_RESERVED,
        MATERIALS_SENT_BYTES_RESERVED,
        PENDING_QUESTION_BYTES_RESERVED,
        REMEMBERED_BYTES_RESERVED,
        SAFETY_MARGIN_BYTES,
        SUMMARY_BYTES_RESERVED,
        UNDERSTANDING_BYTES_RESERVED,
    )
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import request_payload

    binding = active_release().role("GENERATE_VALUE")
    # **`inference_model.generate` 装配的那一份，素材与 understanding 都空** ——
    # 走 `request_shape.generate_prompt`，**不再手抄一份镜像**（2026-09-15「批 1」）。
    #
    # 先前这里是一个手写的 dict 字面量，**而它就是那第四份 `user_prompt` 形状**：
    # 生产一份、admission 一份（两者 2026-09-15 已收敛到 `request_shape`）、
    # 线路层 `request_payload` 一份，**加上这里**。
    # **它已经漂过一次**：`generate` 加了 `knowledge_chunks` 而这份镜像没跟上，
    # 量出来的基线比生产小 26 字节 —— 方向 fail-open（余量看起来更多）。
    #
    # **改成现造之后，这条用例顺带成了安全余量的到期守卫**：
    # 往 `generate_prompt` 里加任何一个键，基线就变，`expected` 跟着变，
    # 而 `MATERIAL_BYTES_BUDGET` 是个常数 —— **这里红**。
    # 于是「payload 多了一项而预算没重推」这件事不再靠人记得。
    user_prompt = json.dumps(
        request_shape.generate_prompt(
            strategy_id="s",
            strategy_reason_code="r",
            event_type="customer_message",
        ),
        ensure_ascii=False,
    )
    payload = request_payload(
        binding,
        system_prompt=inference_model._GENERATE_SYSTEM,
        user_prompt=user_prompt,
    )
    baseline = len(json.dumps(payload, **_WIRE_JSON).encode("utf-8"))

    # **2026-09-15：多了知识库那一项（T318）。** 先前这条式子里没有它，
    # 而知识库每回合都可能进 generate 的 `user_prompt` —— 那是「这张表漏了一项」，
    # 与上限多大无关，抬不抬都要补。
    # **2026-09-18（批 E）：多了「上一轮那句话」那一项。**
    # 它进了生成步的 `user_prompt`，而基线只量得到「这个键空着时多大」——
    # 它是**变长**的，所以像知识库那样单独扣一行。
    # 这一刻正是上面那段「到期条件」写着的那一刻：
    # 「那一项真的进了 generate 的 payload 时，基线就变，这条等式当场红」。
    # **2026-09-19（T363）：多了「记住的那几件事」那一项。** 同样的形状 ——
    # 基线量得到的只是「两个键空着时多大」（+283），
    # 而记忆的**值**是变长的，每轮最多 `REMEMBERED_BYTES`。
    expected = (
        MAX_MODEL_INPUT_BYTES
        - baseline
        - UNDERSTANDING_BYTES_RESERVED
        - KNOWLEDGE_PROMPT_BYTES
        - LAST_OUTBOUND_BYTES_RESERVED
        - MATERIALS_SENT_BYTES_RESERVED
        - REMEMBERED_BYTES_RESERVED
        # **挂着没答的那个问题**（运维缺陷 19，2026-09-23）——
        # 同一条规矩的又一次执行：谁把一个变长项写进生成步的
        # `user_prompt`，谁同批给它扣一行。
        - PENDING_QUESTION_BYTES_RESERVED
        # **这通对话聊过什么**（T200，2026-09-20）—— ADR-0122 §三
        # 那条继承项的兑现物：谁把摘要写进提示词，谁同批建预算。
        - SUMMARY_BYTES_RESERVED
        - SAFETY_MARGIN_BYTES
    )
    assert MATERIAL_BYTES_BUDGET == expected, (
        f"空请求实测 {baseline}，按算式应得 {expected}，而常数是 {MATERIAL_BYTES_BUDGET}"
    )


def test_the_reserved_line_equals_the_bound_the_prompt_actually_obeys() -> None:
    """**推导式里留的那 1,200 与提示词真的守的那 1,200 必须是同一个数。**

    `REMEMBERED_BYTES_RESERVED`（推导式那一侧）与 `memory_prompt.REMEMBERED_BYTES`
    （`memories_for_prompt` 真正按它截断的那一侧）**分在两个包里各写一个数**。
    调高截断那一侧而忘了扣账，是 fail-open 的那个方向：
    **请求真的变大了，而预算表说没变。**

    这与 `PLATFORM_PROMPT_BYTES` 两处钉等值是同一条纪律 ——
    一个数写两遍就要有人钉住它俩相等。
    """

    from fastagent_v2.runtime.memory_prompt import REMEMBERED_BYTES
    from fastagent_v2.worker.material_selection import REMEMBERED_BYTES_RESERVED

    assert REMEMBERED_BYTES_RESERVED == REMEMBERED_BYTES, (
        f"推导式扣了 {REMEMBERED_BYTES_RESERVED}，而提示词按 {REMEMBERED_BYTES} 截断"
    )


def test_the_helper_in_this_file_is_not_the_real_payload_shape() -> None:
    """**反面：上面那条不能靠 `_request_bytes` 也能过。**

    没有这一条，下一个人把基线改回从 helper 读、再把常数调成 5,868，
    两条用例会一起绿 —— 而那正是本批被审查打回的那个状态。

    这条断言两者**必须不相等**，并说出差在哪。
    """

    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import request_payload

    binding = active_release().role("GENERATE_VALUE")
    real = request_payload(
        binding, system_prompt=inference_model._GENERATE_SYSTEM, user_prompt="{}"
    )

    assert "instructions" in real and "messages" not in real, (
        "responses 绑定的 payload 用 instructions + input，不是 messages"
    )
    assert isinstance(real["input"], str), "input 是字符串，helper 里是 message 数组"
    for field in ("stream", "reasoning", "max_output_tokens", "temperature"):
        assert field in real, f"真实 payload 带 {field}，而 helper 不带 —— 基线差就差在这里"
