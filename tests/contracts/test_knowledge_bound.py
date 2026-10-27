"""知识库进提示词的那道额度：**整条丢弃、不截断、不重排**，而额度按**契约**算。

**ADR-0135（用户 2026-09-12）**。这一份最要紧的一条是
`test_the_budget_covers_what_the_contract_allows` —— 它钉的不是 1,826 这个数字，
是**那个数与 `050` 那条 CHECK 之间的关系**：额度必须覆盖合同允许的最坏一条 ×2。

**为什么这件事值一条用例**：先前的额度 1,800 覆盖的是**今天语料**的最坏（1,740），
而合同允许 1,822 —— 于是「下一个作者在合同范围内写一条顶格 chunk」就能让某一回合超版，
**而超版的表现是回落保守计划、客户这一轮收不到消息**。
`050` 那条 CHECK 里的 `20` 是提示词开销的**代理**，实测 31，短了 11。
"""

from __future__ import annotations

import json

from fastagent_v2.runtime.knowledge_bound import (
    KNOWLEDGE_PROMPT_BYTES,
    bound_for_prompt,
    wire_bytes,
)
from fastagent_v2.runtime.prompt_bytes import ENTRY_SEPARATOR_BYTES, document_cost, entry_cost
from fastagent_v2.runtime.understanding_bound import UNDERSTANDING_PROMPT_BYTES
from fastagent_v2.worker.retrieval import TOP_K

#: `050` 的 CHECK：`20 + octet_length(ref) + octet_length(body) <= 900`。
CHUNK_DB_CAP = 900
CHUNK_DB_ALLOWANCE = 20


def _understanding_at_the_bound() -> dict[str, object]:
    """一份**顶到 `UNDERSTANDING_PROMPT_BYTES` 界内最大**的 understanding。

    现造，不写死一个数 —— 那个界改了这里跟着走。
    """

    document: dict[str, object] = {"facts": []}
    while True:
        facts = [
            *document["facts"],  # type: ignore[misc]
            {
                "key": f"k{len(document['facts'])}",  # type: ignore[arg-type]
                "typed_value": "运" * 20,
                "confidence": 0.9,
                "evidence_span_hash": "a" * 64,
            },
        ]
        if document_cost({"facts": facts}) > UNDERSTANDING_PROMPT_BYTES:
            return document
        document = {"facts": facts}


def _at_the_contract_cap(ref: str = "knowledge:a:v1") -> dict[str, str]:
    """一条**顶到合同上界**的 chunk：`ref + body` 恰好用满 880 字节。"""

    room = CHUNK_DB_CAP - CHUNK_DB_ALLOWANCE - len(ref.encode("utf-8"))
    body = "运" * (room // 3)
    assert CHUNK_DB_ALLOWANCE + len(ref.encode()) + len(body.encode()) <= CHUNK_DB_CAP
    return {"ref": ref, "text": body}


def test_the_budget_covers_what_the_contract_allows() -> None:
    """**额度按合同算，不按今天的语料算。**

    两个「最坏」不是同一个东西，而先前的 1,800 坐在它们中间：

    | | top-2 |
    |---|---:|
    | 今天语料的最坏（55 条真实 chunk） | 1,740 |
    | **合同允许的最坏** | **1,822** |

    这一条从合同那一侧现量：两条顶格 chunk 的 `entry_cost` 之和必须 **≤ 额度**。
    **它不写死 1,822** —— 哪天 `050` 那条 CHECK 的 900 变了，这里跟着变，
    而额度不跟着变就会红。
    """

    worst = [_at_the_contract_cap(), _at_the_contract_cap()]
    cost = wire_bytes(worst)

    assert cost <= KNOWLEDGE_PROMPT_BYTES, (
        f"合同允许两条共 {cost} 字节，而额度是 {KNOWLEDGE_PROMPT_BYTES} —— "
        "一个只覆盖今天语料的额度，下一条顶格 chunk 就会让某一回合超版"
    )
    assert bound_for_prompt(worst).dropped == 0, "合同允许的最坏必须装得下，不该退化成 top-1"


def test_the_db_allowance_is_a_proxy_and_it_is_short() -> None:
    """**`050` 那条 CHECK 里的 `20` 是提示词开销的代理，而它短了。**

    这一条把那个差记成用例：DB 那一侧给一条 chunk 留 20 字节的额外开销，
    而它进 `user_prompt` 真实要付 **31**（两层 JSON 的引号与转义）。
    **DB 收得下不等于提示词装得下** —— 这就是 ADR-0135 那条修订的根因，
    而它在今天之前没有任何东西在看。
    """

    chunk = _at_the_contract_cap()
    payload = len(chunk["ref"].encode()) + len(chunk["text"].encode())

    overhead = entry_cost(chunk["ref"], chunk["text"]) - payload

    assert overhead > CHUNK_DB_ALLOWANCE, "代理数如果不短，这条修订就不必存在"
    assert overhead == 31, "两层 JSON 的开销是个常数；它变了说明序列化那一层换了"


def test_dropping_is_unreachable_while_top_2_and_the_db_cap_hold() -> None:
    """**今天这道额度是一道绊索，不是日常路径 —— 而这件事该被写下来。**

    上一条证明契约允许的两条（1,822）装得进 1,826。把它和 `TOP_K = 2` 放在一起，
    结论是：**生产今天丢不掉任何一条** —— `dropped > 0` 与 `hits_sent < hits` 都到不了。

    **那不是说这一层白写**：`TOP_K` 变 3 的那一天，第三条必定被丢
    （三条顶格 = 2,733 > 1,826），而**丢掉的那条要可数**是 ADR-0135 第 3 条。
    这一条用例的作用是**让「到不了」变成一句量出来的话**：
    下一个人读到 `hits_sent` 全是 `= jsonb_array_length(hits)` 的时候，
    不会以为那一列是死代码 —— 也不会以为额度这件事已经被验过了。
    """

    worst = _at_the_contract_cap()
    per_chunk = entry_cost(worst["ref"], worst["text"])

    assert per_chunk * TOP_K <= KNOWLEDGE_PROMPT_BYTES, (
        "top-2 之下丢不掉东西 —— 这是上一条的推论，这里只把它说出来"
    )
    assert per_chunk * (TOP_K + 1) > KNOWLEDGE_PROMPT_BYTES, (
        f"TOP_K 到 {TOP_K + 1} 时额度就必然不够 —— 那时 `hits_sent` 是"
        "唯一能把被丢掉的那条数出来的地方，别在那一批里顺手把它删了"
    )


def test_a_chunk_that_does_not_fit_is_dropped_whole_not_truncated() -> None:
    """装不下就整条不送 —— **截一半的 chunk 是一条会让模型引错的来源**。

    与 `understanding_bound` 那条「整条丢弃、留下的每一条都完整」同一个理由。
    """

    huge = {"ref": "knowledge:a:v1", "text": "运" * 2000}

    bounded = bound_for_prompt([huge])

    assert bounded.chunks == ()
    assert bounded.dropped == 1
    assert bounded.sent == 0


def test_it_stops_at_the_first_miss_instead_of_repacking() -> None:
    """**装不下就停，不跳过一条去装更小的下一条。**

    `hits` 的顺序是检索层用 `<=>` 判出来的相关性顺序。
    跳过一条去装更小的那条**等于让「装得下」决定送哪一条** ——
    那是一次没人看得见的相关性重排，而相关性不该被一个字节预算悄悄改写。

    **第一版代码写的是 `continue`，而注释写的是这句话** —— 用例现在钉的是注释那一侧。

    这一条**显式传 `limit`**，不借生产那个 1,826：合同允许的两条（1,822）本来就装得下，
    借生产的数就造不出「第二条装不下」这个局面。**规则与数字是两件事，这里测规则。**
    """

    first = {"ref": "knowledge:first:v1", "text": "满 99 包邮"}
    second = _at_the_contract_cap("knowledge:second:v1")
    tiny = {"ref": "knowledge:tiny:v1", "text": "运费 8 元"}
    # 刚好装得下第一条和第三条，装不下中间那条。
    limit = wire_bytes([first, tiny]) + 10

    bounded = bound_for_prompt([first, second, tiny], limit=limit)

    assert [chunk["ref"] for chunk in bounded.chunks] == ["knowledge:first:v1"], (
        "第二条装不下，那之后的都不再试 —— 哪怕第三条还留着位置"
    )
    assert bounded.dropped == 2


def test_only_ref_and_text_reach_the_prompt() -> None:
    """`score` 与 `source_spans` **在这一层就被挡住**（ADR-0135 第 1 条）。

    不是靠提示词那一层记得不要塞进去：分数对模型没有意义（让它看见分数
    等于请它按分数说话），而三跳追溯靠的是 `retrieval_evidence.hits` 里那份 span 拷贝。
    """

    hit = {
        "chunk_id": "8d2f",
        "ref": "knowledge:a:v1",
        "text": "满 99 包邮",
        "score": 0.8123,
        "source_spans": [{"file": "docs/x.md", "section": "运费"}],
    }

    bounded = bound_for_prompt([hit])

    assert bounded.chunks == ({"ref": "knowledge:a:v1", "text": "满 99 包邮"},)
    assert "score" not in bounded.chunks[0]
    assert "source_spans" not in bounded.chunks[0]


def test_the_ruler_is_the_one_the_prompt_is_measured_with() -> None:
    """`wire_bytes` 与 `entry_cost` 是同一把尺子，**而这里不写第二个 `json.dumps`**。

    素材那一侧用的就是 `entry_cost`：两种来源进同一个 `user_prompt`，
    **用两把尺子量它们是「账本与闸不是同一把尺子」那一族的入口**
    （`prompt_bytes.py` 顶部记着前几次）。
    """

    chunks = [{"ref": "knowledge:a:v1", "text": "满 99 包邮"}]

    assert wire_bytes(chunks) == entry_cost("knowledge:a:v1", "满 99 包邮")
    # 顺带钉住它不是「裸 JSON 长度」：两层转义是真实存在的那一层。
    naive = len(json.dumps(chunks[0], ensure_ascii=False).encode("utf-8"))
    assert wire_bytes(chunks) > naive


def _a_pending_question_at_the_reservation() -> dict[str, object]:
    """一个**按上界顶满**的 `pending_question`（运维缺陷 19）。

    `question_text` 用写入侧的上界（截尾 200 字符，中文 3 字节/字 = 600），
    其余键按真实形状写满 —— 与 `_a_summary_at_the_reservation` 同一条理由：
    留空就等于拿一个没有挂问的回合去验一条为它扣过账的式子，
    第三头会报「预算比它该有的松」，**松出来的正好是那 1,000**。
    """

    return {
        "question_id": "19191919-1919-4919-8919-191919191919",
        "frame_id": "19191919-1919-4919-8919-999999999999",
        "slot": "a_slot_name_at_the_sixty_four_character_schema_bound_xxxxxxxxxx",
        "asked_at": "2026-09-23T08:56:34+00:00",
        "question_text": "运" * 200,
        "answer_evidence": None,
    }


def _a_summary_at_the_reservation() -> list[str]:
    """一份**恰好顶满 `SUMMARY_BYTES_RESERVED`** 的摘要。

    与 `_materials` 同一种造法：按行往里填，填到再加一行就超为止 ——
    **顶格而不超**，因为超了验的就不是那条式子而是它的越界行为。
    """

    from fastagent_v2.runtime.summary_prompt import SUMMARY_PROMPT_BYTES

    lines: list[str] = []
    spent = 0
    line = "运" * 40
    cost = len(line.encode("utf-8")) + 1
    while spent + cost <= SUMMARY_PROMPT_BYTES:
        lines.append(line)
        spent += cost
    return lines


def test_a_turn_at_the_material_budget_still_fits_the_limit() -> None:
    """**顶满预算的那一回合，整条请求仍装得进 `MAX_MODEL_INPUT_BYTES`；多一段就不装。**

    ## 它换掉了什么，以及为什么（用户 2026-09-15 裁定）

    上一版叫 `test_the_budget_is_the_line_in_the_table`，断言的是
    **那张表的算术**：`池子 − 分配 == 0`，其中「分配」里有一项是
    `materials_line = 3804` —— **演示包最紧那一个回合的读数**。

    **两处毛病，第二处更贵**：

    1. `== 0` 编的是「这张表没有富余」那次裁定。**上限抬到 49,000 之后富余是有意的**，
       那条断言的语义整个失效（实测剩 32,616）；
    2. **它把一份样本的读数写进了平台级断言** —— 而演示包是配包，配包本来就会被改
       （`AGENTS.md`「配包适配代码，不是代码适配演示包」）。
       于是那条用例测的是「闸的算术对不对」，**不是「这个包能不能发出去」**。

    **改成撞真实约束**：`MAX` 是那个数，撞上就是真的发不出去
    （`providers.py` 的 `worst_case_input_tokens` -> `ProviderInputTooLarge` ->
    那一回合回落保守计划、客户收不到东西）。
    **这一条从此不含任何来自演示包的数。**

    ## 它为什么是「一个可能出现的输入」，不是人为造的极端

    顶满 `MATERIAL_BYTES_BUDGET`（**32,420**）要多少素材：按本仓实测的段长
    （演示包平均每段 202 字节 ≈ 67 汉字）是**约 160 段**；
    按一段 300 汉字（900 字节）算是 **36 段**。
    **一个商家给一个阶段配 36 段素材毫不夸张** —— 判据满足。
    """

    from fastagent_v2.profiles.registry import MAX_MODEL_INPUT_BYTES
    from fastagent_v2.runtime import request_shape
    from fastagent_v2.runtime.prompt_bytes import entry_cost
    from fastagent_v2.worker import inference_model
    from fastagent_v2.worker.material_selection import (
        LAST_OUTBOUND_BYTES_RESERVED,
        MATERIAL_BYTES_BUDGET,
        SAFETY_MARGIN_BYTES,
        UNDERSTANDING_BYTES_RESERVED,
    )
    from fastagent_v2.worker.model_policy import active_release
    from fastagent_v2.worker.providers import _WIRE_JSON, request_payload

    def _materials(total: int) -> list[dict[str, str]]:
        """一组素材，`entry_cost` 之和恰好不超过 `total`，尽量顶满。"""

        out: list[dict[str, str]] = []
        spent = 0
        index = 0
        while True:
            ref = f"material:fill-{index:04d}:v1"
            body = "运" * 300
            cost = entry_cost(ref, body) + ENTRY_SEPARATOR_BYTES
            if spent + cost > total:
                break
            out.append({"ref": ref, "text": body})
            spent += cost
            index += 1
        # 用短正文把最后那点缝填上，让它真的贴着预算
        while True:
            ref = f"material:tail-{index:04d}:v1"
            cost = entry_cost(ref, "运") + ENTRY_SEPARATOR_BYTES
            if spent + cost > total:
                break
            out.append({"ref": ref, "text": "运"})
            spent += cost
            index += 1
        return out

    def _wire(materials: list[dict[str, str]]) -> int:
        chunk = _at_the_contract_cap()
        user_prompt = json.dumps(
            request_shape.generate_prompt(
                strategy_id="offer_material",
                strategy_reason_code="STAGE_MATERIAL",
                understanding=_understanding_at_the_bound(),
                event_type="customer_message",
                content_materials=materials,
                knowledge_chunks=[chunk, dict(chunk, ref="knowledge:b:v1")],
                # **两个变长项也顶格**（2026-09-18，批 E）。
                #
                # 推导式为它们各扣了一行（`LAST_OUTBOUND_BYTES_RESERVED` /
                # `MATERIALS_SENT_BYTES_RESERVED`），而扣的是**最坏情形**。
                # 这里留空就等于拿一个不存在的回合去验那条式子 ——
                # 第三头当场报「预算比它该有的松」（实测 48,635 ≤ 49,000），
                # **而松出来的正好是这两项预留的那 2,300**。
                last_outbound="运" * (LAST_OUTBOUND_BYTES_RESERVED // 3),
                materials_already_sent=[
                    f"material:filler-{index:04d}:v1"
                    for index in range(request_shape.MATERIALS_SENT_SHOWN)
                ],
                # **第四个变长项也顶格**（2026-09-20，T200）——
                # 与上面那两个逐字同一条理由：推导式给摘要扣了一行
                # `SUMMARY_BYTES_RESERVED`，扣的是**最坏情形**。
                # 这里留空就等于拿一个没有摘要的回合去验一条为摘要扣过账的式子，
                # 第三头当场报「预算比它该有的松」（实测 46,920 ≤ 49,000），
                # **而松出来的正好是这一项预留的那 2,400**。
                conversation_summary=_a_summary_at_the_reservation(),
                # **第五个变长项也顶格**（2026-09-23，运维缺陷 19）——
                # 推导式给它扣了 `PENDING_QUESTION_BYTES_RESERVED`。
                pending_question=_a_pending_question_at_the_reservation(),
            ),
            ensure_ascii=False,
        )
        payload = request_payload(
            active_release().role("GENERATE_VALUE"),
            system_prompt=inference_model._GENERATE_SYSTEM,
            user_prompt=user_prompt,
        )
        return len(json.dumps(payload, **_WIRE_JSON).encode("utf-8"))

    at_budget = _materials(MATERIAL_BYTES_BUDGET)
    assert at_budget, "一段都装不下，预算或段长算错了"

    fits = _wire(at_budget)
    assert fits <= MAX_MODEL_INPUT_BYTES, (
        f"顶满素材预算（{MATERIAL_BYTES_BUDGET:,}，{len(at_budget)} 段）+ "
        f"understanding 顶格（{UNDERSTANDING_BYTES_RESERVED:,}）+ 知识库顶格之后，"
        f"整条请求 {fits:,} 已经超过 {MAX_MODEL_INPUT_BYTES:,} —— "
        "**预算是从上限反推的，反推错了**。四项里哪一项动了就回头重推那条式子"
        "（`material_selection.MATERIAL_BYTES_BUDGET` 的 docstring）"
    )

    # **第二头：那 5,000 安全余量真的在。**
    # 顶满预算的回合**本来就该离上限还有一个安全余量** —— 那正是它的作用
    # （「不顶到线」）。第一版我在这里断言「再多一段就超限」，**那是错的期待**：
    # 实测多一段是 44,891，离 49,000 还有四千多 —— 而那四千多就是这条余量。
    # **这一条把「余量真的在」钉住**，否则推导式里减了它、而实际没留出来，没有东西会红。
    headroom = MAX_MODEL_INPUT_BYTES - fits
    assert headroom >= SAFETY_MARGIN_BYTES, (
        f"顶满预算的回合离上限只剩 {headroom:,}，而安全余量声称有 "
        f"{SAFETY_MARGIN_BYTES:,} —— **减了却没留出来**，推导式里有一项算重了"
    )

    # **第三头：预算加上安全余量之后，才是真的撞得到的那条线。**
    # 这一条证明那个预算是一个**界**，不是一个随便小一点的数 ——
    # 如果它比该有的松，池子里就还有没记账的余量（T318 就是这么发现的）。
    over = _materials(MATERIAL_BYTES_BUDGET + SAFETY_MARGIN_BYTES + 2_000)
    assert _wire(over) > MAX_MODEL_INPUT_BYTES, (
        f"素材超出「预算 + 安全余量」之后整条请求仍是 {_wire(over):,} ≤ "
        f"{MAX_MODEL_INPUT_BYTES:,} —— 那说明预算比它该有的松"
    )
