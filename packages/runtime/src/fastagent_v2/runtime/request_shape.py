"""三种模型请求的 `user_prompt` **长什么样**，只有这一个定义点。

## 这个模块是被一次「三把尺子互不相等」逼出来的

2026-09-15 实测（基线 `f55a46b1`，在架八帧）：同一个 plan 固定底有三个数，
**而三个都不一样，方向全是 fail-open**：

| | `internal-demo-sales/introduce` | `synthetic-carrier/rental` |
|---|---:|---:|
| `scripts/check_material_dimensions.py` 报的 | 9,654 | 8,344 |
| `_check_request_floors` 算的 | 10,179 | 9,195 |
| **真实请求（回合部分全空）** | **10,939** | **10,035** |

**三笔差额，前两笔是算错了，第三笔不是**：

1. **单层对两层** —— 闸用 `json.dumps(..., separators=(",", ":"))` 量一层，
   而真实要付两层（先写进 `user_prompt`，再作为 payload 里的一个字符串值）；
2. **`separators` 的方向也反了** —— 生产那一层**不传** `separators`，
   所以分隔符是 `", "` / `": "`，每个比闸算的多一个空格。
   这与 `prompt_bytes.ENTRY_SEPARATOR_BYTES` 是同一个事实，那边已经踩过并写下了；
3. **骨架被数在零处** —— `user_prompt` 自己那十个顶层键名与花括号，
   **不在 `PLATFORM_PROMPT_BYTES`（那是 system prompt）、不在响应 schema、
   也不在「这个帧贡献的部分」里**。

**第 3 笔要单独记，因为它与前两笔性质不同**（用户 2026-09-15）：
前两笔是**算法错**，换个算法就对了；第 3 笔是
**「这一项不属于任何一格，于是没有人算它」** ——
**成本付了，全仓无人记账**。它是 `AGENTS.md` 判据 2（「数据生产了，全仓无人消费」）
的反向同族，而反向的那一面更难发现：**少记一笔开销不会让任何东西变红，
只会让余量看起来比实际多。**

## 所以这个模块存在的理由是**唯一定义点**，不是复用

`worker/inference_model.py` 与 `profiles/registry.py` 各自拼过一份 `user_prompt`
的形状（后者拼的是它的一个子集），**而那 320 字节的骨架差额正是因为有两处在各拼各的**。
从此两边都调这里：

* 生产（`inference_model.understand/plan/generate`）传**配包那半 + 回合那半**；
* admission（`registry._check_request_floors`）传**配包那半，回合那半留默认（空）**。

**`services/api` 不能 import `services/worker`**（部署时 api 镜像里没有 worker 包，
由 `tests/contracts/test_deployment_root_and_catalog_gate.py` 的
`test_the_registry_trusts_the_deployment_it_runs_in_not_the_tree_it_was_built_from` 守着），
**所以共享的这一份只能落在 `packages/runtime`** —— 与 `prompt_bytes`、
`understanding_bound` 同一个理由、同一个位置。

## 键的**顺序**是承重的，不要"顺手排一下"

`json.dumps` 按插入顺序写，而字节数按写出来的那一份算。
本模块各函数里键的顺序**逐字抄自 2026-09-15 之前的生产代码**，
改顺序不改语义，**但会改字节**，于是 admission 的固定底与
`MATERIAL_BYTES_BUDGET` 的推导都要重跑。要改就连着那两处一起改。

## 这里**不做**策略选择

`allowed_strategies` 由调用方算好了传进来 —— 生产那边要做
`STRATEGY_CATALOG` 过滤与 time-rule 的 `required_strategy` 收窄，
那是策略，不是形状。本模块只负责"把它摆成一个 dict"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 角色的名字，与 `worker.model_policy` 的写法一致。
UNDERSTAND_VALUE = "UNDERSTAND_VALUE"
PLAN_CAPABILITY = "PLAN_CAPABILITY"
GENERATE_VALUE = "GENERATE_VALUE"
#: 第四个（ADR-0149），**只在主动轮次发出去**。
#:
#: ⚠️ 它进固定底那道闸，而那一条是补上的：加完这份请求之后跑全量
#: **2,418 条一条都没红** —— 一个会真的发出去的新请求形状，没有任何东西检它
#: 装不装得下。`registry.request_floor_bytes` 那段注释自己记着同型的上一次
#: （「**三种请求共用同一个 MAX_MODEL_INPUT_BYTES**，而先前这道闸只看两种」），
#: **这是第三次**，所以这一次把它一起接上。
SILENCE_CAPABILITY = "SILENCE_CAPABILITY"

#: 一个**没有回合**的帧是什么状态。
#:
#: `status` 不是一个"可以为空"的字段 —— 每个活着的帧都有一个。
#: 固定底要一个确定的取值，取的是**最短的那个合法值**，
#: 因为固定底的语义是「这一种请求**一定**要付的那部分」。
#: 真实回合里它可能是 `resume_confirmation_pending`（更长），
#: 那多出来的字节属于回合，不属于固定底。
EMPTY_TURN_STATUS = "active"


def _slot_entry(
    slot: Mapping[str, Any], allowed: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    """`slots_wanted` 里的一项。

    **四个键，第五个只在有话可说时才出现。**

    ⚠️ **这段先前写着「`type` 今天由配包写、schema 不声明（T28 / D2）」，
    而那句话 2026-09-15（D2）起就不成立了** —— schema 里 `type` 是四个值的
    闭合词表且必填。**一条登记的依据失效而引它的注释没人改**，
    与 ADR-0144 起草时撞到的是同一件事，顺手更正在这里。

    `description` 是 **ADR-0144 决策 3** 加的：在它之前，一个槽到模型那里
    **只有名字和类型** —— `deposit_amount` 是一个模型得自己猜意思的词。
    schema-guided 那条线（SGD，arXiv:1909.05855）做的正是这件事：
    **每个槽配一段自然语言描述，模型就能填它没见过的槽。**

    `allowed_values` 是 2026-09-15「裁 B」加的：**这个槽只收这些值**。
    它**只在这个槽真有一份词表时才写出来**，所以没有话题槽的帧
    字节一个不变。

    **为什么是这张表而不是一个新的顶层键**：写入约束的执行者是理解步
    （`slot_updates` 是它产的），而 `slots_wanted` 正是「模型能填哪些槽、
    什么类型、必不必填」那张表 —— **意思与合法取值是这张表缺的两格，
    不是两个新概念**。

    `allowed_values` 是 2026-09-15「裁 B」加的第四个键：**这个槽只收这些值**。
    它**只在这个槽真有一份词表时才写出来**，所以没有话题槽的帧
    （在架八帧里的七帧）字节一个不变，三个角色的 `REQUEST_OVERHEAD_BYTES` 也不动。

    **为什么是这张表而不是一个新的顶层键**：写入约束的执行者是理解步
    （`slot_updates` 是它产的），而 `slots_wanted` 正是「模型能填哪些槽、
    什么类型、必不必填」那张表 —— **合法取值是这张表缺的那一格，不是一个新概念**。
    """

    name = slot["name"]
    entry: dict[str, Any] = {
        "name": name,
        # **紧挨着名字**：模型读到一个槽名，下一眼就该是它的意思。
        "description": slot.get("description"),
        "type": slot.get("type"),
        "required": bool(slot.get("required")),
    }
    values = allowed.get(str(name))
    if values:
        entry["allowed_values"] = list(values)
    return entry


def _slot_vocabulary(
    information_slots: Sequence[Mapping[str, Any]],
    allowed_slot_values: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """`slots_wanted`：模型能填哪些槽位，**它们各是什么意思**，以及类型、必填性与合法取值。"""

    allowed = allowed_slot_values or {}
    return [
        _slot_entry(slot, allowed) for slot in information_slots if isinstance(slot, Mapping)
    ]


#: 「已经讲过哪几段」最多带多少条进一个提示词。
#:
#: **这个数今天咬不到任何一份在架配包**：演示包最多的一帧 18 段，
#: 而 `test_the_cap_is_above_every_shipped_frame` 钉住
#: 「上限 ≥ 每一帧的素材条数」—— 哪天有配包超过它，**那条用例先红**，
#: 于是「要不要抬这个数」变成一次决定，而不是一次悄悄的截断。
#:
#: **为什么仍然要有界**：`_delivered_materials` 去重，所以清单长度
#: ≤ 本帧素材条数，而那个条数由 `MATERIAL_BYTES_BUDGET` 兜着 ——
#: 顶满约 **160 段**（`test_a_turn_at_the_material_budget_still_fits_the_limit`
#: 算过）。160 个 ref ≈ 7KB，而理解步与计划步各收一份，**每轮 14KB**。
MATERIALS_SENT_SHOWN = 24


#: 重跑时最多把几句许诺送回去。**两句**：实测那几轮一段里最多两句许诺
#: （批 F 的 `test_two_promises_in_one_paragraph_are_both_recorded` 就是那个形状），
#: 而界的作用是让这一项的字节可算 —— 与 `MATERIALS_SENT_SHOWN` 同一种记法。
UNKEPT_PROMISES_SHOWN = 2

#: 一句许诺最多带多少字节回去。中文一句许诺约 20–30 字 ≈ 60–90 字节，
#: 取 120 留一点；超了截断，**而截断的那一句仍然说得出它许了什么**。
UNKEPT_PROMISE_BYTES = 120


def _recent_promises(promises: Sequence[str]) -> list[str]:
    """送回去的那几句 —— **留头不留尾**，与 `_recent_refs` 的留尾相反。

    一句许诺的信息在**前半**（「那我先把试用怎么开……」），
    而 `_recent_refs` 那一族留尾是因为 ref 的版本号在后面。
    **两种留法都在这个文件里，所以各写一句为什么。**
    """

    kept: list[str] = []
    for promise in list(promises)[:UNKEPT_PROMISES_SHOWN]:
        encoded = promise.encode("utf-8")
        if len(encoded) <= UNKEPT_PROMISE_BYTES:
            kept.append(promise)
            continue
        kept.append(
            encoded[:UNKEPT_PROMISE_BYTES].decode("utf-8", errors="ignore") + "…"
        )
    return kept


def _recent_refs(refs: Sequence[str]) -> list[str]:
    """最近讲过的那几段，**留尾不留头**。

    与 `InboxProcessor.LAST_OUTBOUND_BYTES` 逐字同一条理由：
    「上次讲到哪」问的是最近讲的那几段，从头截会留下一串开场白。
    """

    return [str(ref) for ref in list(refs)[-MATERIALS_SENT_SHOWN:]]


def understand_prompt(
    *,
    goal: str,
    information_slots: Sequence[Mapping[str, Any]],
    allowed_slot_values: Mapping[str, Sequence[str]] | None = None,
    event_type: str = "",
    content: Mapping[str, Any] | None = None,
    known_slots: Mapping[str, Any] | None = None,
    #: 上一轮**我们对这个客户说的那句话**。
    #:
    #: 运维 2026-09-17 实测：客户回的是我们自己那道选择题的答案
    #: （「有人专门负责」四个字），被这一步判成 `AMBIGUOUS_LOW_SIGNAL_INPUT`，
    #: 计划步随即选 `wait`，**客户那侧完全沉默**。
    #: 而在此之前，这一步收到的五样里没有一样告诉过它我们问过什么 ——
    #: **单看几个字读不出意思，而它本来不该被单独看**（用户 2026-09-17）。
    #:
    #: **空着也发**，与 `content_materials` 那几个同一个理由：
    #: 「这通对话我们还没说过话」与「没人告诉你我们说过话」
    #: 不该在模型眼里长得一样。
    last_outbound: str = "",
    #: 这通对话聊过什么，**按发生顺序、已经按 `SUMMARY_PROMPT_BYTES` 收过**
    #: （T200 / ADR-0122 §三 的继承项，2026-09-20）。
    #: 收的那一步在 `runtime.summary_prompt.summary_for_prompt`，不在这里 ——
    #: **这里只负责把它放进 payload**，与 `known_memories` 同样的分工。
    conversation_summary: Sequence[str] = (),
    #: 装不下的有几行。**空着也发**，理由同 `memories_not_shown`。
    summary_lines_not_shown: int = 0,
    #: 这个联系人**已经记住的那几件事**，商家在 `memory_fields` 里声明过的。
    #:
    #: 在此之前这条路是断的：表、迁移、RLS、控制面路由、`remember` 写入路径、
    #: 封闭词表的闸 —— 六样都在，**而值从来没有回到过任何一个提示词**。
    #: 于是「记住了什么」对模型等于没发生过。
    #:
    #: 由 `runtime.memory_prompt.memories_for_prompt` 算出来，
    #: **生产与用例调的是同一个函数**（与 `allowed_slot_values` 同一条纪律）。
    known_memories: Sequence[Mapping[str, str]] | None = None,
    #: 装不下而**没带上的有几条**。
    #:
    #: 一次安静的截断读起来与「这个客户没有记忆」一模一样，
    #: 而这两件事对模型该做的事完全不同。**0 是「全带上了」。**
    memories_not_shown: int = 0,
    #: 这通对话**已经真的发出去过**的话术 ref，按发出顺序。
    #:
    #: 平台从 `intent_records` 现读（`pipeline._delivered_materials`），
    #: **不是模型自己报的** —— 同一个仓里那条注释写着理由：
    #: 「guess wrong, select the wrong materials, and nothing goes red」。
    #:
    #: 在此之前这份清单只喂给**选取**那一步（算阶段、滤已发过的），
    #: **模型一次都没见过** —— 运维报了三次「它每一轮都像第一次看到这些素材」。
    #:
    #: **只给 ref，不给正文**：正文这一轮该不该再出现由选取那一步判，
    #: 而这里要答的是「哪几件事已经讲过了」—— 答它不需要把话再抄一遍。
    materials_already_sent: Sequence[str] = (),
) -> dict[str, Any]:
    """理解步的 `user_prompt`。

    **配包那半**：`goal` · `information_slots` · `allowed_slot_values`。
    **回合那半**：`event_type` · `content` · `known_slots`（默认全空 = 固定底）。

    `allowed_slot_values` 由 `runtime.topic_vocabulary.allowed_slot_values(definition)`
    算出来，**生产与 admission 调的是同一个函数** —— 这是「裁 B」的全部要点：
    **告诉模型的那个集合，与 `apply_slot_updates` 执行的那个集合，同源。**
    传 `None` 就是「这个帧没有任何取值受限的槽」，那时 `slots_wanted` 与从前逐字节相同。
    """

    return {
        "event_type": event_type,
        "content": dict(content) if content else {},
        "current_goal": goal,
        "known_slots": dict(known_slots) if known_slots else {},
        "last_outbound": last_outbound,
        # **这通对话聊过什么**（T200，2026-09-20）——
        # 在此之前**没有任何一处把过往回合放进提示词**
        # （`pipeline` 那段注释逐字写着 "nothing anywhere puts prior turns
        # into a prompt"，ADR-0122 §二.2 据它判 B4-6 已交付）。
        #
        # 三步都要它，各要一半：理解步判「他这句是不是在接上次那件事」；
        # 计划步判「上次停在哪、这次该接着说什么」（运维 §11.4 那条判据：
        # **跟进的内容要和之前对话终止时的情况有关**）；生成步据它别把
        # 已经说过的话再说一遍（运维 §八 那条：同一段逐字说两遍算缺陷）。
        #
        # **空着也发**，与 `last_outbound` 那几个同一个理由：
        # 「这通对话刚开始」与「没人告诉你有摘要这回事」不该长得一样。
        "conversation_summary": [str(line) for line in conversation_summary],
        # **装不下的数出来**，与 `memories_not_shown` 逐字同源：
        # 一段安静截断的上下文，与一段本来就没发生过的对话，
        # 在提示词里长得一模一样 —— 而模型会照后者去问一件它已经问过的事。
        "summary_lines_not_shown": int(summary_lines_not_shown),
        # **记住的那几件事**（2026-09-18）。理解步与计划步收到同一份。
        #
        # 理解步要它：客户说「还是老样子」时，「老样子」是什么只在记忆里；
        # 计划步要它：该问的东西如果已经记着，就不该再问一遍。
        #
        # **空着也发**，与 `last_outbound` 那几个同一个理由：
        # 「这个客户我们还什么都不知道」与「没人告诉你有记忆这回事」
        # 不该在模型眼里长得一样。
        "known_memories": [dict(entry) for entry in (known_memories or [])],
        "memories_not_shown": int(memories_not_shown),
        # **已经讲过哪几段**（2026-09-18，运维 §8.4 / §10.2 / §11.3 报了三次）。
        #
        # 理解步要它：客户说「上次那个」时，「那个」是什么在这份清单里；
        # 计划步要它：**「上次讲到开通流程」正是跟进那一句要接住的东西**，
        # 而在此之前没有任何一步看得见它。
        #
        # **空着也发**，与 `last_outbound` / `known_memories` 同一个理由。
        "materials_already_sent": _recent_refs(materials_already_sent),
        "slots_wanted": _slot_vocabulary(information_slots, allowed_slot_values),
    }


def silence_prompt(
    *,
    #: 下面这一串**原样转给 `understand_prompt`**，一个不多一个不少。
    goal: str,
    information_slots: Sequence[Mapping[str, Any]],
    allowed_slot_values: Mapping[str, Sequence[str]] | None = None,
    event_type: str = "",
    content: Mapping[str, Any] | None = None,
    known_slots: Mapping[str, Any] | None = None,
    last_outbound: str = "",
    conversation_summary: Sequence[str] = (),
    summary_lines_not_shown: int = 0,
    known_memories: Sequence[Mapping[str, str]] | None = None,
    memories_not_shown: int = 0,
    materials_already_sent: Sequence[str] = (),
    #: 下面三样是**主动轮次客观多出来的事实**。
    silent_for_seconds: float = 0.0,
    unanswered_rounds: int = 0,
    offered_materials: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """第四步的 `user_prompt`（ADR-0149）：**上下文与理解步同一份，问题不同**。

    ## 共用那一半为什么是委托，不是抄一遍

    `understand_prompt(...)` 的返回值原样拿来，再加三个键。**抄一遍会漂** ——
    本模块已经为这个形状写过两次纪律（`known_memories` / `materials_already_sent`
    那几段：「管线算一次，两步收同一份」「两处各算一遍会让理解步与计划步看见
    不同的记忆，而那种不一致只在真实回合里显形」）。
    这里把它做成结构上的不可能：**这一步看见的东西，按构造等于理解步看见的东西。**

    ## 为什么不给它一份「静默画像」

    第一版设计挑了七样事实（挂着的问题、沉默多久、最后说的那句……）交给它。
    用户 2026-09-22 指出「**静默情况也是无法穷尽的**」——
    **一张手挑的输入清单编码了一套「沉默为什么发生」的理论**，
    而那套理论对下一个行业照样不够用。挑输入和挑词表是同一个错，只是换了一层。

    所以平台不预先判断哪几样事实解释得了一次沉默，它把**自己知道的**给出去。

    ## 那三个多出来的键

    * `silent_for_seconds` —— 6 秒和 6 小时是两回事，而客户消息那一轮
      **根本没有这个量**；
    * `unanswered_rounds` —— **连着几轮没回音**。它在这里是因为
      「一次有策略的追问」与「一直追问」在模型眼里长得一模一样，除非知道这是第几次
      （用户 2026-09-22：「追问也可能是一种销售策略，不一定真的要客人回答」）；
    * `materials_offered_now` —— 这一轮**真的被 offer** 的话术，`ref -> 原文`。
      由 `select_materials` 按商家的声明算出来，**平台不放宽**。
      **空着也发**，与本模块其余几处同一个理由：「这一轮没有可发的话术」
      与「没人告诉你有话术这回事」不该在模型眼里长得一样 ——
      而空集合今天是常态（在架配包一条 `availability: timer` 的素材都没有）。
    """

    prompt = understand_prompt(
        goal=goal,
        information_slots=information_slots,
        allowed_slot_values=allowed_slot_values,
        event_type=event_type,
        content=content,
        known_slots=known_slots,
        last_outbound=last_outbound,
        conversation_summary=conversation_summary,
        summary_lines_not_shown=summary_lines_not_shown,
        known_memories=known_memories,
        memories_not_shown=memories_not_shown,
        materials_already_sent=materials_already_sent,
    )
    prompt["silent_for_seconds"] = float(silent_for_seconds)
    prompt["unanswered_rounds"] = int(unanswered_rounds)
    prompt["materials_offered_now"] = dict(offered_materials or {})
    return prompt


def plan_prompt(
    *,
    goal: str,
    exit_conditions: Sequence[Any],
    information_slots: Sequence[Mapping[str, Any]],
    allowed_strategies: Sequence[str],
    strategies: Mapping[str, Mapping[str, Any]],
    event_kinds: Sequence[Any],
    understanding: Mapping[str, Any] | None = None,
    status: str = EMPTY_TURN_STATUS,
    slots: Mapping[str, Any] | None = None,
    stack_depth: int = 1,
    #: **这通对话已经进过哪几个帧**，按进入顺序，含已经结束的（2026-09-21）。
    #:
    #: ⚠️ **在此之前计划步只看得见 `stack_depth` 一个数** —— 它答不出
    #: 「报价那一帧去过没有」。而 ADR-0146 之后一个声明成 `once` 的帧
    #: 再 `push` 会被拒（`FRAME_ALREADY_ENTERED`），落到确定性兜底上；
    #: 兜底点不出缺哪个槽时是 `no_message` —— **客户就那样被无视了**。
    #: 所以这一份要发下去：**不让它看见，就等于让它去猜**。
    frames_entered: Sequence[str] = (),
    pending_question: Any = None,
    #: 这一轮可以发的文件，**与生成步收到的是同一份清单**。
    #:
    #: 计划步要它，是因为候选意图的 `typed_payload` 是**必填**的 ——
    #: 提一个 `send_material` 就得同时给出 `{"ref": …}`，而 ref 只能从这里来。
    #: 在此之前这个参数不存在，于是计划步不知道有文件可发，
    #: **从不把 `send_material` 列进候选** —— 而生成步只能提候选里有的 kind。
    #: 十一轮真实对话零次提名，根因就是这一行的缺席。
    content_assets: Sequence[Mapping[str, str]] | None = None,
    focus: Mapping[str, Any] | None = None,
    #: **上一次尝试许了、而这一轮没兑现的那几句**（T367，2026-09-19）。
    #:
    #: 第一次尝试恒为空；只有重跑那一次才非空。它在的时候，
    #: 计划步要做的事是**把兑现那一条列出来**（`send_message` 列两条）——
    #: 而生成步早就被告知「许了就放进计划列的下一条」（批 G）。
    #:
    #: ⚠️ **为什么不能塞进 `focus`**：那是用户 2026-09-15 裁定的**闭合键集**
    #: （`conversation-aggregate` schema 里 `additionalProperties: false`，
    #: 描述逐字写着「有人加第三类键不会红……要加哪一类，改这里就是那次裁定的
    #: 执行者」）。往里塞一个键就是绕过那次裁定，所以这里是一个独立的键。
    #:
    #: ⚠️ **它送的是句子，不是动词短语**：模型要知道它许了**什么**才补得出来，
    #: 而「讲清楚」三个字说不出「试用怎么开」。界见 `UNKEPT_PROMISES_SHOWN`。
    unkept_promises: Sequence[str] = (),
    #: 这一轮那次静默怎么读的，以及读完之后要做什么（ADR-0149）。
    #: 形状是 `silence-reading-result` 的 `{reading, action}` 两格。
    #:
    #: **客户消息那一轮是 `None`**，而它仍然作为空字典发出去 —— 与本模块
    #: 其余几处同一个理由：「这一轮不是主动触达」与「没人告诉你有这回事」
    #: 不该在模型眼里长得一样。
    silence_reading: Mapping[str, Any] | None = None,
    #: 上一轮我们对这个客户说的那句话 —— 与理解步收到的是同一份。
    #: 理由见返回体里那段注释。
    last_outbound: str = "",
    #: 这通对话聊过什么，**按发生顺序、已经按 `SUMMARY_PROMPT_BYTES` 收过**
    #: （T200 / ADR-0122 §三 的继承项，2026-09-20）。
    #: 收的那一步在 `runtime.summary_prompt.summary_for_prompt`，不在这里 ——
    #: **这里只负责把它放进 payload**，与 `known_memories` 同样的分工。
    conversation_summary: Sequence[str] = (),
    #: 装不下的有几行。**空着也发**，理由同 `memories_not_shown`。
    summary_lines_not_shown: int = 0,
    #: 这个联系人**已经记住的那几件事**，商家在 `memory_fields` 里声明过的。
    #:
    #: 在此之前这条路是断的：表、迁移、RLS、控制面路由、`remember` 写入路径、
    #: 封闭词表的闸 —— 六样都在，**而值从来没有回到过任何一个提示词**。
    #: 于是「记住了什么」对模型等于没发生过。
    #:
    #: 由 `runtime.memory_prompt.memories_for_prompt` 算出来，
    #: **生产与用例调的是同一个函数**（与 `allowed_slot_values` 同一条纪律）。
    known_memories: Sequence[Mapping[str, str]] | None = None,
    #: 装不下而**没带上的有几条**。
    #:
    #: 一次安静的截断读起来与「这个客户没有记忆」一模一样，
    #: 而这两件事对模型该做的事完全不同。**0 是「全带上了」。**
    memories_not_shown: int = 0,
    #: 这通对话**已经真的发出去过**的话术 ref，按发出顺序。
    #:
    #: 平台从 `intent_records` 现读（`pipeline._delivered_materials`），
    #: **不是模型自己报的** —— 同一个仓里那条注释写着理由：
    #: 「guess wrong, select the wrong materials, and nothing goes red」。
    #:
    #: 在此之前这份清单只喂给**选取**那一步（算阶段、滤已发过的），
    #: **模型一次都没见过** —— 运维报了三次「它每一轮都像第一次看到这些素材」。
    #:
    #: **只给 ref，不给正文**：正文这一轮该不该再出现由选取那一步判，
    #: 而这里要答的是「哪几件事已经讲过了」—— 答它不需要把话再抄一遍。
    materials_already_sent: Sequence[str] = (),
) -> dict[str, Any]:
    """计划步的 `user_prompt`。

    **配包那半**：`goal` · `exit_conditions` · `information_slots`（取必填的名字）·
    `allowed_strategies` · `strategies` 的三张表 · `event_kinds`。
    **回合那半**：`understanding` · `status` · `slots` · `stack_depth` ·
    `pending_question` · `focus`（默认全空 = 固定底）。

    **三张 by-strategy 表都用 `.get` 而不是下标**：schema 给 `strategies` 补形状时
    没有加 `required`，所以一个策略可以只写其中一两个键；缺席的意思是
    「这个策略不声明这一项」，那是一句真话，而下标会在那种配包上抛 `KeyError`。
    """

    named = [name for name in allowed_strategies if name in strategies]
    return {
        "understanding": dict(understanding) if understanding else {},
        "current_frame": {
            "goal": goal,
            "status": status,
            "slots": dict(slots) if slots else {},
            "required_slots": [
                slot["name"]
                for slot in information_slots
                if isinstance(slot, Mapping) and slot.get("required")
            ],
            "exit_conditions": list(exit_conditions),
        },
        "allowed_strategies": list(allowed_strategies),
        "allowed_intent_kinds_by_strategy": {
            name: strategies[name].get("allowed_intent_kinds", []) for name in named
        },
        "allowed_generation_constraints_by_strategy": {
            name: strategies[name].get("generation_constraints", []) for name in named
        },
        "required_slots_by_strategy": {
            name: strategies[name].get("required_slots", []) for name in named
        },
        "allowed_event_kinds": list(event_kinds),
        "stack_depth": stack_depth,
        "frames_entered": [str(frame) for frame in frames_entered],
        "pending_question": pending_question,
        "focus": dict(focus) if focus else {},
        # **上一次许了而没兑现的那几句**（T367）。**空着也发**，
        # 与隔壁那几个键同一个理由：「这一次没有没兑现的许诺」与
        # 「没人告诉你还有这回事」不该在模型眼里长得一样 ——
        # 而它非空的那一次，正是模型唯一需要改主意的那一次。
        "unkept_promises": _recent_promises(unkept_promises),
        # **那次静默是怎么读的**（ADR-0149）。只有主动轮次非空。
        #
        # 计划步要它，是因为**这一步已经判过一次了**：它说了为什么对面不说话、
        # 这一轮该做什么、要发就发哪一份。计划步如果看不见它，
        # 那次判断就没有消费方 —— 而本仓管那叫「数据生产了，全仓无人消费」。
        #
        # ⚠️ **它不是命令，是一份判断**：选策略仍然是计划步的事，
        # 而 `allowed_strategy_set` / `time_rule_strategy_set` 那两道闸一个没松。
        "silence_reading": dict(silence_reading or {}),
        # **上一轮我们说的那句话**（2026-09-17）。理解步与计划步收到同一份。
        #
        # 计划步要它的理由与理解步不同：模型上一轮说「**我说说它具体怎么接**」，
        # 下一轮换成了一个问句 —— **今天没有任何东西记着它答应过什么**。
        # `pending_question` 记的是 `slot` 与 `asked_at`，**没有正文**。
        "last_outbound": last_outbound,
        # **这通对话聊过什么**（T200，2026-09-20）——
        # 在此之前**没有任何一处把过往回合放进提示词**
        # （`pipeline` 那段注释逐字写着 "nothing anywhere puts prior turns
        # into a prompt"，ADR-0122 §二.2 据它判 B4-6 已交付）。
        #
        # 三步都要它，各要一半：理解步判「他这句是不是在接上次那件事」；
        # 计划步判「上次停在哪、这次该接着说什么」（运维 §11.4 那条判据：
        # **跟进的内容要和之前对话终止时的情况有关**）；生成步据它别把
        # 已经说过的话再说一遍（运维 §八 那条：同一段逐字说两遍算缺陷）。
        #
        # **空着也发**，与 `last_outbound` 那几个同一个理由：
        # 「这通对话刚开始」与「没人告诉你有摘要这回事」不该长得一样。
        "conversation_summary": [str(line) for line in conversation_summary],
        # **装不下的数出来**，与 `memories_not_shown` 逐字同源：
        # 一段安静截断的上下文，与一段本来就没发生过的对话，
        # 在提示词里长得一模一样 —— 而模型会照后者去问一件它已经问过的事。
        "summary_lines_not_shown": int(summary_lines_not_shown),
        # **空着也发，与生成步那三个键同一个理由**：
        # 「这一轮没有文件可发」与「没人告诉你有文件这回事」不该在模型眼里长得一样。
        "content_assets": [dict(entry) for entry in (content_assets or [])],
        # **记住的那几件事**（2026-09-18）。理解步与计划步收到同一份。
        #
        # 理解步要它：客户说「还是老样子」时，「老样子」是什么只在记忆里；
        # 计划步要它：该问的东西如果已经记着，就不该再问一遍。
        #
        # **空着也发**，与 `last_outbound` 那几个同一个理由：
        # 「这个客户我们还什么都不知道」与「没人告诉你有记忆这回事」
        # 不该在模型眼里长得一样。
        "known_memories": [dict(entry) for entry in (known_memories or [])],
        "memories_not_shown": int(memories_not_shown),
        # **已经讲过哪几段**（2026-09-18，运维 §8.4 / §10.2 / §11.3 报了三次）。
        #
        # 理解步要它：客户说「上次那个」时，「那个」是什么在这份清单里；
        # 计划步要它：**「上次讲到开通流程」正是跟进那一句要接住的东西**，
        # 而在此之前没有任何一步看得见它。
        #
        # **空着也发**，与 `last_outbound` / `known_memories` 同一个理由。
        "materials_already_sent": _recent_refs(materials_already_sent),
    }


def memory_fields_wanted(
    memory_fields: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """**这个商家允许记哪几件事，以及每一件是什么意思。**

    与 `slots_wanted` 同一个形状、同一条理由（ADR-0144 决策 3）：
    **一张模型看不见的闭合词表，就是一张它会去猜的词表。**

    运维 2026-09-20 实测那一轮：模型提了一个 `remember`，载荷写成
    `{'concern_topic': …}` —— **既不是要求的形状，也不是声明过的字段**，
    于是先被 `_remember_payload_refusal` 拒（形状），
    形状对了还会被 `_remember_turn_refusal` 再拒一次（字段没声明）。
    **而客户完全看不见**：同一轮的回复照常发出，只是那次记忆写入丢了。

    ⚠️ **`ttl_days` 不下发**：它是平台与商家之间的事（这条记忆能活多久），
    模型既不决定它也不该知道它 —— 多给一个字段就是多一个可以被写错的东西。
    """

    return [
        {"key": str(field["key"]), "description": str(field.get("description", ""))}
        for field in (memory_fields or [])
        if isinstance(field, Mapping) and field.get("key")
    ]


def generate_prompt(
    *,
    strategy_id: str = "",
    strategy_reason_code: str = "",
    generation_constraints: Sequence[Any] | None = None,
    candidate_intents: Sequence[Any] | None = None,
    required_slot_requests: Sequence[Any] | None = None,
    understanding: Mapping[str, Any] | None = None,
    event_type: str = "",
    content_materials: Sequence[Mapping[str, str]] | None = None,
    knowledge_chunks: Sequence[Mapping[str, str]] | None = None,
    content_assets: Sequence[Mapping[str, str]] | None = None,
    #: 上一轮我们对这个客户说的那句话 —— **理解步与计划步早就收着，而写字这一步没有**。
    #:
    #: 运维 §11 实测：定时跟进那一轮发出去的是
    #: 「在吗？想跟您确认一下，上次聊的那件事您那边考虑得怎么样了？」
    #: 用户裁定：**跟进的内容要和之前对话终止时的情况有关**。
    #:
    #: §11.3 把那一轮记成「有上下文而没用上」—— 那句话对**判断**那两步成立，
    #: 对**写字**这一步不成立：本批现读 `generate_prompt` 的形参，
    #: **它既没有 `last_outbound` 也没有素材清单**。定时那一轮的
    #: `understanding` 里没有客户消息、演示包 `timer` 档 0 段素材 ——
    #: **它手里只有一个打法名。**
    #:
    #: ⚠️ **它是有界的（`InboxProcessor.LAST_OUTBOUND_BYTES`），
    #: 而那正是它进得来的原因**：`MATERIAL_BYTES_BUDGET` 的推导式为它扣了一行，
    #: 与知识库 top-2 那一项同一种记法。一个无界项进这里等于拿安全余量去赌。
    last_outbound: str = "",
    #: 这通对话聊过什么，**按发生顺序、已经按 `SUMMARY_PROMPT_BYTES` 收过**
    #: （T200 / ADR-0122 §三 的继承项，2026-09-20）。
    #: 收的那一步在 `runtime.summary_prompt.summary_for_prompt`，不在这里 ——
    #: **这里只负责把它放进 payload**，与 `known_memories` 同样的分工。
    conversation_summary: Sequence[str] = (),
    #: 装不下的有几行。**空着也发**，理由同 `memories_not_shown`。
    summary_lines_not_shown: int = 0,
    #: 这通对话**已经真的讲过哪几段**，平台从 `intent_records` 现读。
    #:
    #: **用户 2026-09-18 当场更正**：
    #: 「**上一句不一定是关键，之前的内容才是** ——
    #: 有可能结束前聊的东西和我们的目标没什么关系。」
    #:
    #: 上一句是一句**字面的话**，而对话可能结束在「好的我看看」这种
    #: 与目标无关的一句上；**照着它接，跟进句会接到一件不重要的事上**。
    #: 「之前的内容」在这个系统里对应的就是这份清单。
    #:
    #: 批 A 把它给了理解步与计划步，**而写字那一步一直没有** ——
    #: 界与留尾都照 `_recent_refs`，字节在 `MATERIALS_SENT_BYTES_RESERVED` 里扣。
    materials_already_sent: Sequence[str] = (),
    #: 这个联系人**已经记住的那几件事**，形状是 `{ref, value}`（T363）。
    #:
    #: **在此之前写字这一步看不见记忆** —— 理解步与计划步收着它（E4-5），
    #: 而**写回复的那一步从没见过**。于是 T363 要答的那个问题
    #: 「那条回复引了哪几条记忆」在结构上答不出来：引用集要有人去引。
    #:
    #: ⚠️ **键名与理解/计划步那一份不同，而形状也不同** ——
    #: 那两步收 `known_memories: [{field, value}]`（它们只读、不引），
    #: 这一步收 `remembered_facts: [{ref, value}]`（它要引）。
    #: **同名而不同形状是 `CLAUDE.md` 第三节点名的陷阱**，所以换名字。
    #: 由 `memory_prompt.remembered_facts_for_prompt` 从同一份 `shown` 转出来，
    #: 所以三个提示词对「递了哪几条」是同一个答案。
    remembered_facts: Sequence[Mapping[str, str]] | None = None,
    #: 装不下而**没带上的有几条** —— 与理解/计划步同一个理由：
    #: 一次安静的截断读起来与「这个客户没有记忆」一模一样。
    memories_not_shown: int = 0,
    #: **这个商家允许记哪几件事** —— 与 `slots_wanted` 同一条理由。
    #:
    #: **空着也发**（`[]`）：「这个商家一件都不让记」与「没人告诉你能记什么」
    #: 不该在模型眼里长得一样 —— 与 `last_outbound` / `content_materials`
    #: 那几个逐字同一条规矩。
    rememberable_fields: Sequence[Mapping[str, Any]] | None = None,
    #: **挂着没答的那个问题**（运维缺陷 19，2026-09-23）。
    #:
    #: 计划步早就收着它（`plan_prompt`），**而真把问题写进正文的是这一步** ——
    #: stage 实测的现场正是生成步在同一条消息的第 ③ 段把第 ① 段刚用过
    #: 答案的那个问题原样又问了一遍。只教计划步拦不住写字的手。
    #:
    #: **它是有界的**：`question_text` 在写入时截尾 200 字符
    #: （`pipeline._record_pending_question`），其余是几个短键。
    #: 空着（`None`）也发 —— 与 `last_outbound` 同一条规矩。
    pending_question: Any = None,
) -> dict[str, Any]:
    """生成步的 `user_prompt`。

    **它没有配包那半** —— 每一项都来自计划结果或这一回合，
    所以生成步的固定底就是「`user_prompt` 为空文档时的那个请求」，
    与帧无关。这就是 `MATERIAL_BYTES_BUDGET` 的推导式里那个空请求基线的由来。

    **`content_materials` / `knowledge_chunks` / `content_assets` 空着也发，
    不省这三个键**：「这一轮没有可引的话术」与「没人告诉你有素材这回事」
    不该在模型眼里长得一样。

    **`content_assets` 是第三类，与前两个都不同**（E3-4，2026-09-16）：

    | 键 | 是什么 | 模型拿它做什么 |
    |---|---|---|
    | `content_materials` | 商家逐字审过的**话术** | 按 ref 引，平台在发送路径换成那段字 |
    | `knowledge_chunks` | 整理出来的**资料** | 用自己的话写，把 ref 放进 `source_evidence_refs` |
    | `content_assets` | 一份**文件** | 提一个 `send_material`，payload 只带那个 ref |

    **它只有 ref 与一句人话的标题，没有内容** —— 文件的内容不是字，
    而「把文件名喂给模型让它描述」会让它替一份没读过的文件作担保。
    """

    return {
        "strategy_id": strategy_id,
        "strategy_reason_code": strategy_reason_code,
        "generation_constraints": list(generation_constraints or []),
        "candidate_intents": list(candidate_intents or []),
        "required_slot_requests": list(required_slot_requests or []),
        "understanding": dict(understanding) if understanding else {},
        "event_type": event_type,
        "content_materials": [dict(entry) for entry in (content_materials or [])],
        "knowledge_chunks": [dict(entry) for entry in (knowledge_chunks or [])],
        "content_assets": [dict(entry) for entry in (content_assets or [])],
        # **上一轮我们说的那句话**（2026-09-18）。三步至此收到同一份。
        #
        # **空着也发**，与上面那三个同一个理由：
        # 「这通对话我们还没说过话」与「没人告诉你我们说过话」
        # 不该在模型眼里长得一样 —— 而跟进那一轮**正是靠它区分**
        # 「第一次主动打招呼」与「接着上次那件事说」。
        "last_outbound": last_outbound,
        # **这通对话聊过什么**（T200，2026-09-20）——
        # 在此之前**没有任何一处把过往回合放进提示词**
        # （`pipeline` 那段注释逐字写着 "nothing anywhere puts prior turns
        # into a prompt"，ADR-0122 §二.2 据它判 B4-6 已交付）。
        #
        # 三步都要它，各要一半：理解步判「他这句是不是在接上次那件事」；
        # 计划步判「上次停在哪、这次该接着说什么」（运维 §11.4 那条判据：
        # **跟进的内容要和之前对话终止时的情况有关**）；生成步据它别把
        # 已经说过的话再说一遍（运维 §八 那条：同一段逐字说两遍算缺陷）。
        #
        # **空着也发**，与 `last_outbound` 那几个同一个理由：
        # 「这通对话刚开始」与「没人告诉你有摘要这回事」不该长得一样。
        "conversation_summary": [str(line) for line in conversation_summary],
        # **装不下的数出来**，与 `memories_not_shown` 逐字同源：
        # 一段安静截断的上下文，与一段本来就没发生过的对话，
        # 在提示词里长得一模一样 —— 而模型会照后者去问一件它已经问过的事。
        "summary_lines_not_shown": int(summary_lines_not_shown),
        # **讲过哪几段**（2026-09-18，用户当场更正）。
        #
        # 跟进那一句的锚点是**这个**，不是上一句 ——
        # 上一句可能是「好的我看看」，而那与我们的目标没关系。
        "materials_already_sent": _recent_refs(materials_already_sent),
        # **记住的那几件事，带 ref**（T363）。**空着也发**，
        # 与上面那几个键同一个理由：「这个客户我们什么都没记住」与
        # 「没人告诉你有记忆这回事」不该在模型眼里长得一样。
        "remembered_facts": [dict(entry) for entry in (remembered_facts or [])],
        "memories_not_shown": int(memories_not_shown),
        "rememberable_fields": memory_fields_wanted(rememberable_fields),
        # **挂着没答的那个问题**（运维缺陷 19）——写字的手要看得见它才拦得住。
        "pending_question": pending_question,
    }
