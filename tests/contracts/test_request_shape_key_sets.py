"""三种 `user_prompt` 的键集，**三个都钉住** —— 不只 generate 那一个。

## 它补的是一处量出来的不对称

安全余量那 5,000 的到期条件（`worker/material_selection.py`）挂在
`test_the_empty_request_baseline_is_measured_from_the_real_payload` 上，
说法是「上面四项里**任何一项真的进了 payload**，基线就变，这条等式当场红」。

**独立复核 2026-09-15（批 1 §三）实测：那句话只对 `generate` 成立。**
2026-09-16 在 `88882184` 上重量（同一个键 `rolling_summary`
分别加进 `request_shape` 三个构造点，各跑一遍 `tests/contracts` 全量）：

| 把一个新键加进 | 红 | 红的是谁 |
|---|---:|---|
| `generate_prompt` | **2** | 预算等式 + `test_the_generate_prompt_has_exactly_these_keys` |
| `plan_prompt` | **1** | `..._replay_script_sends_the_same_plan_prompt_keys_...` ——
  **回放脚本键对齐，与字节无关** |
| `understand_prompt` | **0** | —— |
| understand **与** plan 同时加一个键 | **1** | 仍然只有回放那条 |

**`understand` 那一侧是静默的，而 plan 那一条指错了人**：它说的是
「回放脚本与生产不一致」，一个照它去查的人会去改脚本，
**而真正变了的是这一轮请求要付多少字节**。

## 为什么固定底那两条看不见

`test_the_floor_equals_the_request_it_claims_to_measure` 比的是
admission 算的固定底与真实请求，**而两边都调 `request_shape`** ——
加一个键两边一起变，等式照样成立。那是 `probe_request_floor` 的 R3 早记下的盲区：
**「一处算法一份形状」让两边不会漂，也让两边一起漂时没人出声。**

## 所以这里按**等值集合**钉三份键集

这一条不量字节，它只回答一个问题：**这三份请求的形状变了没有**。
变了就红，而红的含义写在下面每条断言的消息里：
**回去重推预算与固定底，别只改这里的期待值。**

`generate` 那一份在 `test_generation_receives_knowledge.py` 里已经有一条
（对着真实调用数键，比这里更强，因为它还能抓到「装配时漏传一个键」）。
**这里不把它删掉，也不假装替代它** —— 这里三份都从 `request_shape` 的**定义点**
数，`generate` 那一条从**调用点**数，两者问的是相邻但不同的问题。
"""

from __future__ import annotations

from fastagent_v2.runtime import request_shape

#: 改这三份清单的人，要连着重推的东西写在这里 —— 一句，不是一段。
_WHY = (
    "键集变了 = 这一轮请求的字节数变了。"
    "\n- generate：`MATERIAL_BYTES_BUDGET` 的推导式从这份形状起算"
    "（`worker/material_selection.py` 那段到期条件）；"
    "\n- plan / understand：admission 的固定底 `_check_request_floors` 从这份形状起算，"
    "**而固定底那两条用例两边都调 `request_shape`，一起变时不会红**。"
    "\n改完请重跑 `scripts/check_material_dimensions.py` 并重推那两处常数。"
)


def test_the_understand_prompt_has_exactly_these_keys() -> None:
    """**这一条是本文件存在的理由**：在它之前，往理解步加一个键全量一条都不红。"""

    document = request_shape.understand_prompt(goal="g", information_slots=[])

    assert sorted(document) == [
        "content",
        # **这通对话聊过什么**（T200，2026-09-20）—— 本仓第一处把过往回合
        # 放进提示词的地方；在此之前 ADR-0122 §二.2 据「提示词从不携带历史」
        # 判 B4-6「上下文预算」已交付并关格。§三 那条继承项因此同批到期。
        "conversation_summary",
        "current_goal",
        "event_type",
        # **记忆那两个键，09-18 加（E4-5）**：表、迁移、RLS、控制面路由、
        # `remember` 写入路径、封闭词表的闸 —— 六样都在，
        # **而值从来没有回到过任何一个提示词**。这两个键是中间那一段。
        # `memories_not_shown` 与它成对：一次安静的截断读起来
        # 与「这个客户没有记忆」一模一样。
        "known_memories",
        "known_slots",
        # **理解步也收上一轮那句话，09-17 加**：客户回的是我们自己那道
        # 选择题的答案（四个字），被这一步判成 `AMBIGUOUS_LOW_SIGNAL_INPUT`、
        # 计划步随即选 `wait`、客户那侧完全沉默。
        # **单看几个字读不出意思，而它本来不该被单独看。**
        "last_outbound",
        # **已经讲过哪几段，09-18 加**：平台每轮从 `intent_records` 现读，
        # 而在此之前**模型一次都没见过它** —— 运维报了三次
        # 「它每一轮都像第一次看到这些素材」。
        "materials_already_sent",
        "memories_not_shown",
        "slots_wanted",
        # 装不下的行数，与 `memories_not_shown` 成对、同一条理由。
        "summary_lines_not_shown",
    ], _WHY


def test_the_plan_prompt_has_exactly_these_keys() -> None:
    """plan 先前只有回放脚本那条会红，**而它说的是另一件事**（见模块 docstring）。"""

    document = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
    )

    assert sorted(document) == [
        "allowed_event_kinds",
        "allowed_generation_constraints_by_strategy",
        "allowed_intent_kinds_by_strategy",
        "allowed_strategies",
        # **计划步也收文件清单，09-17 加**：候选意图的 `typed_payload` 是必填的，
        # 提一个 `send_material` 就得同时给出 ref —— 而 ref 只能从这里来。
        "content_assets",
        # **这通对话聊过什么**（T200，2026-09-20）—— 本仓第一处把过往回合
        # 放进提示词的地方；在此之前 ADR-0122 §二.2 据「提示词从不携带历史」
        # 判 B4-6「上下文预算」已交付并关格。§三 那条继承项因此同批到期。
        "conversation_summary",
        "current_frame",
        "focus",
        # **记忆那两个键，09-18 加（E4-5）**：该问的东西如果已经记着，
        # 就不该再问一遍 —— 而 `pending_question` 只管这一通对话。
        # **进过哪几个帧**（ADR-0146）—— 在此之前计划步只看得见
        # `stack_depth` 一个数，答不出「报价那一帧去过没有」，
        # 而一个 `brief_answer` 的帧再 push 会被拒、落到可能静默的兜底上。
        "frames_entered",
        "known_memories",
        # **计划步也收上一轮那句话，09-17 加**：`pending_question` 记的是
        # `slot` 与 `asked_at`，**没有正文** —— 所以模型上一句
        # 「我说说它具体怎么接」许了什么，此前没有任何东西看得见。
        "last_outbound",
        # **已经讲过哪几段，09-18 加**：平台每轮从 `intent_records` 现读，
        # 而在此之前**模型一次都没见过它** —— 运维报了三次
        # 「它每一轮都像第一次看到这些素材」。
        "materials_already_sent",
        "memories_not_shown",
        "pending_question",
        "required_slots_by_strategy",
        # **那次静默是怎么读的，2026-09-22 加（ADR-0149）**：主动轮次在理解步
        # 之前多问一句「他为什么不说话」，而计划步是那次判断的消费方 ——
        # 看不见它的话，那一步产出的东西没有人用，本仓管那叫
        # 「数据生产了，全仓无人消费」。**客户消息那一轮它是空字典**，
        # 与本文件其余几个「空着也发」同一条理由。
        "silence_reading",
        "stack_depth",
        # 装不下的行数，与 `memories_not_shown` 成对、同一条理由。
        "summary_lines_not_shown",
        "understanding",
        # **许了没兑现时的重跑输入，09-19 加（批 J / T367）**：
        # 第一次尝试恒为空，只有重跑那一次非空 —— 它在的时候，
        # 计划步要做的事是把兑现那一条 `send_message` 列出来。
        # ⚠️ **没有塞进 `focus`**：那是用户 2026-09-15 裁定的闭合键集。
        "unkept_promises",
    ], _WHY


def test_the_plan_prompts_current_frame_has_exactly_these_keys() -> None:
    """`current_frame` 是嵌套的那一层，**而字节是按整份文档算的**。

    只钉顶层会漏掉「往 `current_frame` 里加一项」这种改法 ——
    那同样让每回合的 plan 请求变大，而顶层键集一个字没动。
    """

    document = request_shape.plan_prompt(
        goal="g",
        exit_conditions=[],
        information_slots=[],
        allowed_strategies=[],
        strategies={},
        event_kinds=[],
    )

    assert sorted(document["current_frame"]) == [
        "exit_conditions",
        "goal",
        "required_slots",
        "slots",
        "status",
    ], _WHY


def test_the_generate_prompt_has_exactly_these_keys_at_the_definition_point() -> None:
    """generate 那一份从**定义点**再数一遍。

    调用点那条（`test_generation_receives_knowledge.py`）更强，
    **而它需要一个 monkeypatch 过的 transport 才跑得起来**；
    这一条不需要，所以它在「有人重构 `request_shape` 而没跑那条」时也在。
    """

    document = request_shape.generate_prompt()

    assert sorted(document) == [
        "candidate_intents",
        # **第三个内容键，09-16（E3-4）加**：文件与话术、知识库并列而不合并 ——
        # 三者模型该做的事不同（引 / 写 / 提一个 send_material）。
        "content_assets",
        "content_materials",
        # **这通对话聊过什么**（T200，2026-09-20）—— 本仓第一处把过往回合
        # 放进提示词的地方；在此之前 ADR-0122 §二.2 据「提示词从不携带历史」
        # 判 B4-6「上下文预算」已交付并关格。§三 那条继承项因此同批到期。
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
        # 与 `remembered_facts` 是两回事：那个是已经知道的，这个是允许记的。
        "rememberable_fields",
        "remembered_facts",
        "required_slot_requests",
        "strategy_id",
        "strategy_reason_code",
        # 装不下的行数，与 `memories_not_shown` 成对、同一条理由。
        "summary_lines_not_shown",
        "understanding",
    ], _WHY
