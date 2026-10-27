"""Which of a frame's materials this one turn may use.

**This module is a seam, and that is the point of it existing separately.**
Everything about *which* materials reach the model goes through
`select_materials`; nothing else in the platform knows the rule. Today the rule
is "read what the profile declared" (`_is_offered` below). At a few
hundred materials -- which is where a real merchant starts, not the 29 the
internal demo has -- a declared filter stops being enough on its own: one topic
can hold dozens of materials, and something has to rank or retrieve within it.
When that day comes, the replacement is this one function; prompt assembly,
validation and the send path all keep calling the same name.

**Why the platform selects and not the model.** The alternative considered was
asking the plan step for "which materials shall we use this turn", which is what
`introduce.goal`'s prose currently describes. It needs a new field on the plan
contract *and* a validator *and* the same declared data -- and once the declared
data exists, the model's choice adds a failure mode without adding a capability.
It is the same shape as ADR-0116: narrow the option set first, then validate
what comes back.

**Why the rule is data and not prose.** The bucket rule lives today in the
merchant's `goal` text, and `goal` is sent to the **plan** step while material
selection happens at the **generate** step -- a rule stated in one prompt and
needed in another. It has already drifted in the way prose drifts: the demo
profile's `goal` calls the reactive bucket 「第二桶」 while the material document
that defines the same buckets calls the *timer* bucket 「第二桶」, and one
material (`demo-opening`) is missing from both of the lists `goal` enumerates.
Nothing could have caught either, because there was nothing to catch it with.

**No category vocabulary is written down here.** A merchant's topics are their
own, differ by trade, and change more often than code: `topics` are opaque
strings compared by containment against a slot the frame names. This module must
never grow a list of them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from fastagent_v2.runtime.topic_vocabulary import (
    TOPIC_SLOT_KEY as TOPIC_SLOT_KEY,
)
from fastagent_v2.runtime.topic_vocabulary import (
    declarations as declarations,
)
from fastagent_v2.runtime.topic_vocabulary import (
    topic_vocabulary as topic_vocabulary,
)

#: Availability a material declares. Unqualified entries (a bare ref string)
#: carry none of these and are offered on every turn -- the shape every profile
#: written before this existed uses, read as "declared without restriction".
#:
#: `AVAILABILITIES` is pinned equal to the schema's enum by
#: `test_the_availability_vocabulary_matches_the_contract`. Two lists of legal
#: values is how a profile comes to declare something admission accepts and
#: selection has never heard of -- which, given the branch below fails closed,
#: would be a material silently offered on no turn at all.
PROACTIVE = "proactive"
ON_REQUEST = "on_request"
TIMER = "timer"
#: **两种轮次都能用**（ADR-0149，2026-09-22）。仍然受阶段约束。
#:
#: ## 为什么要有它，而不是「不声明 availability」
#:
#: 用户 2026-09-22：「演示包让 timer 能调用所有能发的资料」。
#: 而在此之前**没有一种声明方式表达得了它**：
#:
#: | 声明 | 定时器可用 | 进字节预算 |
#: |---|---|---|
#: | 不声明 | ✅ | ❌ **裸 ref 那条绕法** |
#: | `proactive` | ❌ | ✅ |
#: | `timer` | ✅（还要 emit_kinds 匹配）| ✅ |
#:
#: 「不声明」看着能用，**而它是 ADR-0125 §6.2 专门关掉的那个洞**：
#: 那条声明既跳过选取的限制，也**读不进 `MATERIAL_BYTES_BUDGET`** ——
#: 独立审查 2026-09-11 实测过一段 **40 KB** 的裸 ref 通过 admission
#: 而预算读到 **0 字节**。
#:
#: 所以「两种轮次都能用」必须是一个**说出来的**取值，不能靠省略 ——
#: 说出来它就进预算、进 admission 的每一道检查，而那正是省略绕过的东西。
ANY_TURN = "any"
AVAILABILITIES = frozenset({PROACTIVE, ON_REQUEST, TIMER, ANY_TURN})

#: 话题槽那个键与 `declarations` 归一化 —— **定义点在 `runtime.topic_vocabulary`**，
#: 这里只是 re-export，`from ...material_selection import TOPIC_SLOT_KEY/declarations`
#: 那些旧调用点不用动。**它们下沉到 runtime 的理由**：`services/api` 要读同一份词表
#: 去告诉模型合法取值，而它不能 import worker（部署门禁守着）。

#: Frame-level key declaring the conversation's stages, in order, each as
#: `{"name": …, "after": [refs]}`.
#:
#: **The stage is derived, not asked for** (user ruling 2026-09-11). Making it
#: one more model-filled information slot was the cheaper option and was
#: rejected for the reason role and channel were rejected: a model that guesses
#: the stage wrong selects the wrong materials, **and nothing goes red**. What
#: the platform *can* know is which materials it has already sent -- that is its
#: own record, not the model's report -- so a stage is declared as "these have
#: gone out already" and computed from that.
#:
#: The stage names are the merchant's. This module never enumerates them, for
#: the same reason it never enumerates topics.
STAGE_KEY = "material_stages"

#: 一个回合能塞给模型的素材字节数上限，**口径是线路**（`ensure_ascii=False`）——
#: 与 `providers.py` 的 `wire_request` 一致，也就是 httpx 真正发出去的那个 body。
#:
#: **2026-09-15：上限抬到 49,000，这条式子重推，而且多了一项。**
#: 定法是用户裁的：**不精算，直接取宽**。范围与逐条依据见
#: `docs/plans/budget-chain-scope-2026-09-15.md`。
#:
#: ```
#:  49,000   MAX_MODEL_INPUT_BYTES（上界依据是一条实测天花板，不是倍数：
#:           `test_memory_fields_admission` 用「16 条中文记忆声明」证明 T300 撤闸是对的，
#:           实测 16 x text_cost("中"x1024) = 49,216，取 49,000 留 216）
#:  -8,174   空的 generate 请求（现测未变）
#:  -1,580   understanding
#:  -1,826   **知识库 top-2 —— 这一项是 2026-09-15 新加的（T318）**。
#:           先前它从来没进过这条式子，而它比当时整个安全余量还大 1.8 倍；
#:           「要不要抬上限」与「这张表漏了一项」是两件事，后者抬不抬都得补。
#:  -5,000   安全余量（原 1,000 x 5，与池子等比例：它的作用是「不顶到线」，
#:           而那个作用只有随池子等比例缩放才成立。1,000 对旧池子是 11.9%，
#:           对新池子只剩 2.4%。**这个数是拍的，写成常数就是为了能被指着换掉**）
#:  ───────
#:  32,420   MATERIAL_BYTES_BUDGET
#: ```
#:
#: ## 余下的那三万字节是**留给还没建的那几项的，而它们不各自预估一个数**
#:
#: 下面四项**将来都会进 payload，而今天一个都量不出**：
#:   * **多轮增长** —— `memory_cursor` / `summary_ref` 一落地，
#:     请求就随对话长度增长（B1-3，零实现）；
#:   * **rolling summary** —— 产物本身不存在；
#:   * **`recent_exchange`** —— 「还没有位置的两家」之一，先前估约 1,070；
#:   * **B2-2 那九个 kind 的提示词说明** —— 今天 0 字节（enum 已满十二个、
#:     `typed_payload` 无约束、三份提示词只点名 `send_message`）。
#:
#: **不给它们各自编一个预估上界**（用户 2026-09-15）：那些数今天量不出，
#: 编出来的数一年后会长出一整套推理 —— 这个仓刚为 16,384 那个数付过账。
#: **它们落在这个空间里**，而这条注释就是那个「格」。
#:
#: ## 到期条件（2026-09-15「批 1」改挂法，用户裁定）
#:
#: **先前它挂在一个批次上**（「四个 payload 构造器收敛之后重估」）。
#: 那一批做完才发现**它根本不改变上面这四项** —— 四项分别落在 B1-3 / rolling
#: summary / `recent_exchange` / B2-2，全在很后面的批次里。
#: **一条挂在别的批次上的到期条件，等于把一条约束交给一个人记得。**
#:
#: **现在它挂在一条会变红的东西上**：
#: `test_the_empty_request_baseline_is_measured_from_the_real_payload` 钉的是
#: `MAX − 空请求基线 − understanding − 知识库 − 安全余量 == MATERIAL_BYTES_BUDGET`，
#: **而那个基线是从 `request_shape.generate_prompt` 现造现量的**。
#: 所以那一项真的进了 `generate` 的 payload 时，基线就变，这条等式当场红 ——
#: 红的那一刻正是该重估这 5,000 的时刻。
#: 反向验证见 `scripts/probe_budget_identity.py`（P1：往 `generate_prompt`
#: 加一个键，等式立刻红）。
#:
#: ## ⚠️ **这条等式只盖住 `generate`，而上面四项不都落在那里**
#:
#: **2026-09-15 独立复核（批 1 §三）指出、2026-09-16 重量并处置。**
#: 同一个键分别加进 `request_shape` 三个构造点，各跑一遍 `tests/contracts` 全量：
#: `generate_prompt` 红 2 条、`plan_prompt` 红 1 条（**回放脚本键对齐，与字节无关**）、
#: `understand_prompt` **一条都不红**。
#: 成因是固定底那两条（`floor == real`）两边都调 `request_shape` ——
#: **加一个键两边一起变**，等式照样成立（`probe_request_floor` R3 记的那个盲区）。
#:
#: 四项按仓里自己写下的落点分：
#:
#: * **`recent_exchange`** → **generate**（`ideas.md` T280 逐字写着「进生成步」，
#:   分支名也是 `feat/recent-exchange-in-generation-20260911`）—— 这条等式盖得住；
#: * **B2-2 九个 kind 的提示词说明** → **三份 system prompt 或响应 schema**，
#:   实测各红 10–12 条（`PLATFORM_PROMPT_BYTES` 与八帧固定底）—— 盖得住，而且很响；
#: * **B1-3 多轮增长 / rolling summary** → 「把过往回合放进提示词」，
#:   **最可能是 understand（与 plan）的 `user_prompt` 键** —— **这一侧先前是静默的**。
#:
#: **处置**（2026-09-16）：`tests/contracts/test_request_shape_key_sets.py`
#: 按等值集合钉住三份键集（含 plan 的 `current_frame` 那一层）。
#: 于是三种请求里任何一种多一个键都会红，**而红的消息直接说「回去重推预算与固定底」**。
#: **这不是把预算等式扩到三种请求** —— 那是另一件事（plan/understand 的额度由
#: admission 的固定底管，不由这条式子管）；这里补的是「形状变了没人出声」。
#:
#: ~~旧式子（16,384 那一版）留痕如下~~
#: ```
#:  16,384   MAX_MODEL_INPUT_BYTES
#:  -8,174   空的 generate 请求（不带素材、`understanding={}`、`knowledge_chunks=[]`）。
#:           **由 `providers.request_payload` 构造，不是另抄一份形状** ——
#:           `test_the_empty_request_baseline_is_measured_from_the_real_payload`
#:           把这个数钉在那个函数上。
#:           **6c（2026-09-12）把它从 8,012 抬到 8,174**：generate 提示词多了一行
#:           说明（**136**）、`user_prompt` 多了一个空的 `knowledge_chunks` 键
#:           （线路 **26**，原始 24）—— 两笔都是**每次 generate 都付**的固定底。
#:  ───────
#:   8,210   余量
#:  -1,580   **understanding**：今天每回合都在。由
#:           `runtime.understanding_bound.UNDERSTANDING_PROMPT_BYTES` 执行，
#:           两处由用例钉死相等。
#:  -1,000   安全余量：不顶到线
#:  ───────
#:   5,630
#: ```
#:
#: ## 这个数被推导过四次，**前三次都错在「量了什么」而不是「算错了」**
#:
#: | 版本 | 值 | 错在哪 |
#: |---|---:|---|
#: | 原始 | 6,132 | `197 + utf8(正文)` —— 与任何一道闸都不是同一把尺子 |
#: | 「字节口径校准」 | 7,063 | 对齐的是当时那道闸，**而那道闸量的串没有任何东西发送** |
#: | 「口径回正」第一版 | ~~5,868~~ | 口径对了而**基线取自用例的 helper**，见下 |
#: | 「口径回正」 | ~~5,792~~ | 对的 —— 直到基线自己变了（下一行） |
#: | **今天** | **5,630** | —— **6c 把接线的 162 加进了固定底**，从同一个池子里出 |
#:
#:
#: 第三次那个错法要写清楚，因为它最像「已经很小心」：
#: `test_prompt_bytes.py::_request_bytes` 的 docstring **自己写着**
#: 「must never be used to measure a baseline」，并点名它缺 `stream` /
#: `reasoning` / `max_output_tokens` / `temperature`，`input` 用的还是
#: chat 的 message 数组。**作者读过那段，然后从它身上读了一个绝对值。**
#: 7,936 对真实 8,012 —— 76 字节，方向 fail-open。
#: **三次都是审查窗口抓的，一次都不是门禁。** 所以这一次留下的不是一个更对的数，
#: 是那条**把基线钉在真实 payload 构造上**的用例 —— 数会再漂，而那条用例会红。
#:
#: **第二次那一批有两件是对的，不要连坐**：
#:   * 「账本与闸不是同一把尺子」这个发现对 —— 只是当时把错的那一边当成了标准；
#:   * **去掉那 1,931 的重复计数**对，它与单位无关（那份预留在独立审查
#:     2026-09-11 P1-1 之后已经被 `_check_stage_budget` 加进每回合总量，减了两次）。
#:
#: ## 在架三个包（线路口径，逐个二分「最紧的那一个回合」）
#:
#: | 包 | 最紧回合 | 占 5,630 |
#: |---|---:|---:|
#: | `internal-demo-sales` | **3,804** | 67.6 % |
#: | `synthetic-carrier` | 199 | 3.5 % |
#: | `synthetic-third` | 66 | 1.2 % |
#:
#: **演示包在转义口径下被记成 6,544，真实是 3,804** —— 近一倍的差别，
#: 而差的那一半是一个不存在的字符串。
#:
#: ## 还没有位置的两家
#:
#: | 谁 | 线路 | 先前（转义口径）报过的 |
#: |---|---:|---:|
#: | ~~知识库 top-2~~ **已接线（6c）** | **1,826**（ADR-0135：按**契约**的最坏算） | ~~3,272~~ |
#: | `recent_exchange`（演示风险三，cap 512/侧） | 约 **1,070** | ~~约 2,000~~ |
#:
#: **知识库这一格记的是上界不是读数**（独立审查 2026-09-12，P3-1）：
#: 裁定八声明了 chunk 线路 900 × top-2，**那是一个真的上界**，
#: 而 `understanding` 那 1,580 只是今天的读数 —— 两者不该并排写成同一种东西。
#:
#: **裁定八说「top-2 最坏 1,800」——它一直是对的。** 那条裁定本来就是按线路口径裁的，
#: 而「它超了 1.69 倍」是上一批用错口径算出来的，**不是那条裁定的问题**。
#:
#: **知识库那一家 6c 已经接了，而它把池子用完了**（ADR-0135 §三）：
#: `recent_exchange` 从「缓」变成**挡住** —— 它要接的时候必须先有一家腾出位置。
#: 「接线的那一批要自己重推这段算式」这句话本批照做了，**上面那个算式就是重推的结果**。
#:
#: **今天 `5,630 − 3,804 = 1,826` 的空隙恰好等于知识库那一格**，而那不是巧合：
#: 这个常数是「一个架包可以封版到多少素材字节」，而表里那 3,804 是
#: 「演示包最紧那一个回合的实测值」—— **两个数说的是两件事，
#: 而 admission 的推导式里没有知识库这一项**。登记在 **T318**，有明确到期条件，
#: 且**不该**靠「把这个常数减 1,826」顺手关掉（那样演示包会卡在边缘上）。
#: 推导式里给 `understanding` 留的那一项。**它今天是一个读数，不是上界**
#: （T282 / ADR-0132）—— 取的是一份「3 facts / 1 intent / 2 slot_updates」的
#: 真实文档。执行它的是 `runtime.understanding_bound`，那一批到时要把
#: `UNDERSTANDING_PROMPT_BYTES` 钉在这个常数上，**而不是各写一个数**。
UNDERSTANDING_BYTES_RESERVED = 1580

#: 不顶到线的余量。**这个数是作者定的，可以被推翻** —— 写成常数就是为了让它
#: 能被指着说「换一个」，而不是变成下一个「没人知道从哪来」的魔数。
#:
#: **2026-09-15 从 1,000 按比例放到 5,000**：它的作用是「不顶到线」，
#: 而那个作用只有随池子等比例缩放才成立 —— 1,000 对旧池子是 11.9%，
#: 对 49,000 那个池子只剩 2.4%。
#:
#: **它有两次实证的用处，两次都是同一种**：
#:   * 2026-09-12 那批第一版的空请求基线错了 **76** 字节、方向 fail-open，
#:     而在架三个包一个都没受影响 —— 兜住它的正是这一格；
#:   * 同一处的手抄镜像漏掉 `knowledge_chunks` 时少量 **26** 字节，同样方向。
#: **那不是把它花掉的理由，是它值这个价的证据** ——
#: 而这两次错的都是「基线」那一项，所以 2026-09-15「批 1」把基线改成现造
#: （见 `UNDERSTANDING_BYTES_RESERVED` 上面那段的「到期条件」）。
SAFETY_MARGIN_BYTES = 5000

#: 推导式里给「**上一轮我们说的那句话**」留的那一项（2026-09-18，批 E）。
#:
#: 它 2026-09-18 才进生成步的 `user_prompt` —— 在那之前**写跟进那句话的那一步
#: 看不见上一轮说了什么**，于是它写出「上次聊的那件事」（运维 §11）。
#:
#: **为什么它要单独占一行，而不是让基线吃掉**：基线量的是「这个键空着时多大」
#: （+19 字节），而这一项是**变长**的 —— 每轮最多带
#: `InboxProcessor.LAST_OUTBOUND_BYTES` 字节正文进去。
#: 知识库 top-2（`KNOWLEDGE_PROMPT_BYTES`）是同一种记法：
#: **变长内容各自扣一行，不靠安全余量兜。**
#:
#: **这个数与 `InboxProcessor.LAST_OUTBOUND_BYTES` 必须相等**，
#: 由 `test_the_reserved_bytes_match_what_the_pipeline_actually_sends` 钉住 ——
#: 两处各写一个数，就是这条推导式漂掉的那种方式。
LAST_OUTBOUND_BYTES_RESERVED = 1200

#: 推导式里给「**已经讲过哪几段**」留的那一项（2026-09-18，批 E）。
#:
#: **用户当场更正**：「上一句不一定是关键，**之前的内容才是**」——
#: 对话可能结束在一句与目标无关的话上，而跟进要接住的是聊过的那件事。
#: 所以这份清单也进了生成步的 `user_prompt`。
#:
#: **1,100 是量出来的**：`request_shape.MATERIALS_SENT_SHOWN`（24）条
#: 演示包里最长那种 ref，序列化后约 1,070 字节 —— 取 1,100 留一点。
#: 与 `LAST_OUTBOUND_BYTES_RESERVED` 同一种记法：变长内容各自扣一行，
#: **不靠安全余量兜**。
MATERIALS_SENT_BYTES_RESERVED = 1100

#: 推导式里给「**记住的那几件事**」留的那一项（2026-09-19，T363）。
#:
#: 它 2026-09-19 才进生成步的 `user_prompt` —— 在那之前**写回复的那一步
#: 看不见记忆**，于是「那条回复引了哪几条记忆」在结构上答不出来
#: （引用集要有人去引）。
#:
#: **为什么单独占一行**：基线量的是「这两个键空着时多大」（+283），
#: 而记忆的**值**是变长的 —— 每轮最多 `memory_prompt.REMEMBERED_BYTES` 字节。
#: 与 `LAST_OUTBOUND_BYTES_RESERVED` / 知识库 top-2 同一种记法：
#: **变长内容各自扣一行，不靠安全余量兜。**
#:
#: **这个数与 `memory_prompt.REMEMBERED_BYTES` 必须相等**，
#: 由 `test_the_reserved_bytes_match_what_the_pipeline_actually_sends` 同族那条
#: 钉住 —— 两处各写一个数，就是这条推导式漂掉的那种方式。
#: ⚠️ 而**理解步与计划步也各付一份**（它们早就收着记忆）——
#: 那两份不在这条式子里：那一侧由 admission 的固定底管。
REMEMBERED_BYTES_RESERVED = 1200

#: 推导式里给「**挂着没答的那个问题**」留的那一项（2026-09-23，运维缺陷 19）。
#:
#: `pending_question` 本轮起也进生成步的 `user_prompt` —— 缺陷现场是
#: 生成步在同一条消息里把刚被答过的问题又问了一遍，**写字的手要看得见
#: 那个问题才拦得住**（计划步早就收着，只教它拦不住生成步）。
#:
#: **1,000 是算出来的上界**：`question_text` 写入时截尾 200 字符
#: （`pipeline._record_pending_question`），中文最坏 3 字节/字 = 600；
#: 其余是几个定长键（两个 uuid 72、两个时间戳 50、slot ≤ 64、键名约 130），
#: 合计 ≤ 920，取 1,000 留一点。基线只量得到「这个键是 `null` 时多大」，
#: 值是变长的 —— 与 `LAST_OUTBOUND_BYTES_RESERVED` 同一种记法：
#: **变长内容各自扣一行，不靠安全余量兜。**
PENDING_QUESTION_BYTES_RESERVED = 1000

#: **这通对话聊过什么**（T200 / ADR-0122 §三 的继承项，2026-09-20）。
#:
#: **这一行就是那条继承项要的「上下文预算」本体。** ADR-0122 §三 写死：
#: **谁把历史 / 摘要写进提示词，谁在同一批里建上下文预算。**
#: 在本批之前请求大小不随会话长度增长（§二.2 据此判 B4-6 已交付并关格）——
#: **本批让它开始增长，所以本批要给它一格账。**
#:
#: ⚠️ **不扣这一行会是 fail-open**：摘要每轮最多 2,400 字节真的在付，
#: 而如果它不从池子里出，素材预算就**多报了 2,400** —— 一份「刚好过闸」
#: 的配包，真实请求已经超了。`AGENTS.md` 判据 2 的反面
#: （「一项开销真的在付，而它不属于任何一格账」）逐字说的就是这件事。
#:
#: **与 `summary_prompt.SUMMARY_PROMPT_BYTES` 必须相等**，
#: 由 `test_the_summary_reservation_matches_the_budget` 钉住 ——
#: 两处各写一个数，正是这条推导式漂掉的那种方式
#: （与 `REMEMBERED_BYTES_RESERVED` 那一行同一种记法）。
#:
#: ⚠️ **理解步与计划步也各付一份**，而那两份不在这条式子里：
#: 与记忆那一行逐字同源 —— 那一侧由 admission 的固定底管。
SUMMARY_BYTES_RESERVED = 2400

#: **6c（2026-09-12）把它从 5,792 减到 5,630，而不是有人改了预算** ——
#: 它是推导出来的：`16,384 − 空请求 − understanding 1,580 − 安全余量 1,000`，
#: 而**空请求从 8,012 变成 8,174**（6c 把知识库接进 generate：提示词一行 136 +
#: `user_prompt` 里一个空的 `knowledge_chunks` 键 24）。
#: 推导式本身一个字没动，由 `test_the_empty_request_baseline_is_measured_from_the_real_payload`
#: 现量现比。
#:
#: **而这个数与 ADR-0132 §八 那张表里的素材那一行（3,804）差 1,826，
#: 恰好是知识库的额度** —— 因为这条推导式**没有减知识库那一项**。
#: 两个数说的是两件事（封版上限 vs 一个真实回合），**而它们从来没对过**，
#: 登记在 **T318**：那里写明了为什么不该顺手把它减掉
#: （减完素材预算恰好等于演示包今天用掉的 3,804，改一个字就封不了版）。
#: **2026-09-16（E3-4）：32,420 → 32,169，而预算本身没人改过。**
#: 它是推导出来的，而推导式里的空请求基线从 8,174 变成了 8,425：
#: 生成步的提示词加了三行讲 `content_assets`（+227），
#: `user_prompt` 里多了一个空的 `content_assets` 键（线路上 +24）。
#: **推导式一个字没动** —— 与 6c 那次（5,792 → 5,630）同源。
#: **2026-09-18（批 E）32,169 -> 29,186，而这一次推导式真的动了。**
#:
#: 运维 §11：写跟进那句话的那一步**看不见对话之前聊过什么**，
#: 于是它写出「上次聊的那件事」。本批把两样给了它，并把字节从池子里扣：
#:
#: **2026-09-19（批 G）29,186 -> 28,868，而推导式一个字没动** ——
#: 变的只有空请求基线：生成步系统提示多了「许了就把 X 放进下一条 `send_message`」
#: 那四行（**+318**）。用户当天原话：「许了 紧跟下一条来讲完
#: 不用在一条里面完成显得很奇怪」。
#: **这 318 从池子里出，安全余量一个字没动** —— 与批 E 同一种记法。
#:
#: ```
#:  49,000   MAX_MODEL_INPUT_BYTES
#:  -9,426   空的 generate 请求（9,108 -> 9,426，+318：生成步系统提示多的那四行。
#:           上一版：8,425 -> 9,108，+683 = 两个空键 +43 + 系统提示两句 +640）
#:  -1,580   understanding
#:  -1,826   知识库 top-2
#:  -1,200   上一轮那句话（= LAST_OUTBOUND_BYTES_RESERVED）
#:  -1,100   **已经讲过哪几段**（= MATERIALS_SENT_BYTES_RESERVED）
#:  -1,200   **记住的那几件事**（= REMEMBERED_BYTES_RESERVED）
#:  -2,400   **这通对话聊过什么**（= SUMMARY_BYTES_RESERVED）
#:  -5,000   安全余量（**一个字没动**）
#:  ───────
#:  24,923   MATERIAL_BYTES_BUDGET
#: ```
#:
#: **2026-09-19（T363）28,868 -> 27,385**：生成步的 `user_prompt` 多了
#: 「记住的那几件事」（`remembered_facts` + `memories_not_shown` 两个键）。
#: 空请求基线 9,426 -> **9,709**（+283：两个空键 + 系统提示多的那三行），
#: **变长那一项再扣一行 1,200**。**安全余量一个字没动。**
#:
#: **两个变长项各扣一行，不靠安全余量兜** —— 与知识库 top-2 同一种记法。
#: 5.7 那一批往生成步加 245 字节而**没有**扣预算，安全余量当场掉到 4,790，
#: 那一次撤了；**批 E 与批 G 都是照规矩付的账**。
#:
#: ⚠️ **批 G 那 318 字节买的不是一个新键，是一条指令** —— 所以
#: `test_request_shape_key_sets.py` 那三份键集**一个字没变**，
#: 而预算等式照样红了（它从空请求现量现比）。**键集没变不等于字节没变。**
#: **2026-09-20（T200 / E4 第 4 批）27,385 -> 24,923**，而这一次推导式**又动了**
#: —— 与批 E 那次同一种情形：加的不只是键，是一项**变长**的新内容。
#:
#: 两笔，分开记：
#:
#: * **空请求基线 9,709 -> 9,771（+62）**：三个提示词的 `user_prompt` 各多了
#:   两个空键（`conversation_summary: []` + `summary_lines_not_shown: 0`）；
#: * **变长那一项扣一行 2,400**（`SUMMARY_BYTES_RESERVED`）——
#:   **这一行是 ADR-0122 §三 那条继承项的兑现物本身**。
#:
#: **安全余量一个字没动。** 与批 E / 批 G / T363 同一种记法：
#: **变长内容各自扣一行，不靠安全余量兜。**
# **2026-09-21（一条说一件事）24,923 -> 24,572**：生成步系统提示多了
#「每条 60 字上限」那几行，空请求基线 +351，**这 351 从池子里出，
# 安全余量一个字没动**。
# **2026-09-21（教模型怎么记）24,572 -> 23,984**：生成步多了「怎么记一件事」
# 那几行（+559）与一个空的 `rememberable_fields` 键（+29），
# **这 588 从池子里出**。
#: **2026-09-23（ADR-0151）23,984 -> 23,639（-345）**：生成步提示词换掉了
#: 「每条至多 60 字」那一段，改成「没有字数限制，长答案自己分段」。
#: **新增的那 345 字节从这个池子里扣**，不让安全余量兜 ——
#: 批 E 立的规矩，`test_no_unfounded_facts_about_the_customer` 钉着它。
MATERIAL_BYTES_BUDGET = 21213



def topic_text(
    definition: Mapping[str, Any],
    *,
    frame: Mapping[str, Any] | None,
    understanding: Mapping[str, Any] | None,
) -> str:
    """Everything known this turn about what the customer is asking about.

    **Both halves are needed, and the second is the one that is easy to miss.**
    A question asked *in this turn* is not in the frame's stored slots yet -- the
    understanding step produced the update a moment ago and nothing has written
    it down. Reading only the stored value would gate out the material that
    answers the question the customer just asked, every time, on the turn that
    matters.

    **这一轮只认最后一条 update**（2026-09-16，T351 / T274）：在此之前每一条都接上去，
    于是一轮多发几条与 C-1 禁掉的累积串**逐字节等价** —— 存储那条路被关了，
    这条路没关。被丢掉的条数由 `superseded_topic_updates` 报给调用方留痕。

    Returned as one string and matched by containment. **That used to be because
    the value was an accumulated phrase** -- the demo profile asked its model for
    「已覆盖：X、Y；当前：Z」 in a single slot. C-1 (2026-09-15) forbade
    exactly that, so today the stored value is one topic and containment behaves
    as equality. It stays containment because the second half of the rationale
    still holds: parsing the value into categories would be this module inventing
    a vocabulary it has no business owning -- **the vocabulary is derived in
    `runtime.topic_vocabulary`, and the model is told it outright.**
    """

    slot = definition.get(TOPIC_SLOT_KEY)
    if not slot:
        return ""
    parts: list[str] = []
    stored = (frame or {}).get("slots") or {}
    if isinstance(stored, Mapping):
        value = stored.get(str(slot))
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, Mapping) and isinstance(value.get("value"), str):
            parts.append(str(value["value"]))
    # **这一轮只认最后一条**（用户 2026-09-16 裁定，T351 / T274）。
    #
    # 在此之前这里是 `parts.append(...)` —— 每一条都接上去。
    # 于是**一轮对同一个槽发七条 update，与 C-1 禁掉的那个累积串逐字节等价**
    # （实测 25 段 / 4,233 字节，两条路给出同一个结果）。
    # C-1 关的是**存储**那条路（累积串写不进槽），这条路它管不着。
    #
    # **平台已经把规则告诉了模型**：演示包 goal 的原话是
    # 「只填当前这一类，不要累积——把几类拼在一起同样会被整条丢弃」。
    # 多发几条是同一条规则的另一种拼法，而在此之前它**不报错**：
    # 能过 schema、能被接受、安静地给出一个看起来正常的结果。
    #
    # **取最后一条而不是第一条**：`slot_updates` 是这一轮的读数序列，
    # 后面那条是模型最后的判断；取第一条等于让它改不了口。
    # **这一轮那条也要过词表**（2026-09-18）。
    #
    # 实测：一条 update 里塞一个累积串会把两组素材一起点亮 ——
    # 而平台**本来就不肯把那个值存进槽**（C-1）。
    # 过一遍词表，是让这一半与存的那一半口径一致；
    # 去掉的只有平台已经拒绝过的值，**不是把文件那一档的语义改了**。
    updates = _legal_update_values(definition, understanding=understanding)
    if updates:
        parts.append(updates[-1])
    return "\n".join(parts)


def _legal_update_values(
    definition: Mapping[str, Any], *, understanding: Mapping[str, Any] | None
) -> list[str]:
    """这一轮对话题槽发过的、**词表认得的**那几个值，按发出顺序。

    ## 为什么要过词表

    **实测 2026-09-18**：一条 update 里塞一个累积串
    （`已覆盖：花钱；当前：查问题`）会把**两组**素材一起点亮 ——
    匹配是包含式，而那个串里有每一类的名字。

    三道闸各关了一半，而这半没人关：**C-1** 关的是「累积串写不进槽」（存储那条路），
    **T351 / T274** 关的是「一轮多发几条 update」（只认最后一条），
    而「**一条 update 里塞一个累积串**」在此之前没有任何东西拦。

    它从前藏在「槽位现值」后面（`topic_text` 是两半之和，存的那半由 C-1 保证干净）。
    **话术改成只看这一轮那半之后，它成了唯一那条路** —— 所以在这里关上。

    ## 用同一个词表，不另立判据

    词表由 `runtime.topic_vocabulary` **唯一推导**（「裁 B」的兑现物：
    告知模型的集合与 `apply_slot_updates` 执行的集合同源）。
    照用那一个 —— **在这里重写一遍判据，就是让它有第三份。**

    **空词表那一支不施加规则**，与 `apply_slot_updates` 逐字同源：
    「配包没声明话题槽」与「声明了话题槽但还没配素材」是两件事，
    后者是**配到一半**，正确处置是不施加，不是锁死
    （`test_a_package_with_no_topic_materials_can_still_write_the_slot`）。
    """

    slot = definition.get(TOPIC_SLOT_KEY)
    if not slot:
        # 帧没声明话题槽 —— 没有哪个槽可读，与「有槽而词表空」是两件事。
        return []
    values = [
        str(update.get("value"))
        for update in (understanding or {}).get("slot_updates") or []
        if isinstance(update, Mapping) and str(update.get("slot")) == str(slot)
    ]

    # ⚠️ **`topic_vocabulary` 的 `None` 同时表示两件事** —— 我第一版把它读成一件，
    # 于是「配到一半」的包（声明了槽、素材还没标 `topics`）被判成一个值都不认。
    # 那正是那个 `return None` 要防的反面（`26ad831c` 开口二：
    # 空词表会把每个值都判非法，那个包的话题槽**永远写不进任何值**）。
    #
    # 上面先按槽名取值，这里只拿词表**过滤**：取不到词表就不过滤。
    found = topic_vocabulary(definition)
    if found is None:
        return values
    return [value for value in values if value in found[1]]


def topic_asked_this_turn(
    definition: Mapping[str, Any],
    *,
    understanding: Mapping[str, Any] | None,
) -> str:
    """客户**这一轮**问的是什么 —— 不含槽位里存着的那个值。

    ## 它与 `topic_text` 的差别就是那个「存着的值」

    `topic_text` 回的是两半之和：**槽位现值** + 这一轮最后一条 update。
    本函数只回后一半。

    **为什么要分开**（用户 2026-09-18 裁定）：那个槽是**黏的**。
    运维实测 —— 它在 2026-09-17 06:31 被写成「花钱」，
    **此后每一轮三段报价话术都在候选里**，于是客户一提「5000条」这个量，
    模型手边就有价格，顺手说了。用户的原话：

    > 「我没问价格，AI 自己报了价格；报价表没发，我具体要才给。」

    ## 谁用它

    **只有话术那一侧**（`select_materials`）。文件那一侧（`select_assets`）
    仍然用 `topic_text` —— 运维明写过文件那一档「他又问一次价格，
    就该又拿到价目表，这不是重复推送」，而话术不是这样：
    同一句话逐字说三遍，客户读到的是复读。

    **同一个 `_is_offered`，两个调用方传不同的判据** ——
    差别写在调用点上，而不是往那个函数里塞一个开关。

    ## 取最后一条，与 `topic_text` 逐字同源

    `slot_updates` 是这一轮的读数序列，后面那条是模型最后的判断；
    取第一条等于让它改不了口。被丢掉的条数由 `superseded_topic_updates` 留痕。

    ## 它的代价，写在这里不藏着

    客户**追问同一件事**而理解步那一轮没有再写这个槽时，
    那几段话术会不在候选里。两件事拦着它变坏：模型每一轮都收到话题词表
    （`slots_wanted` 的 `allowed_values`），而**批 A 之后它还看得见
    `materials_already_sent`** —— 知道自己已经说过了。

    **而这正是裁定要的方向**：宁可这一轮不重复报价，也不要没问就报。
    """

    updates = _legal_update_values(definition, understanding=understanding)
    return updates[-1] if updates else ""


def superseded_topic_updates(
    definition: Mapping[str, Any], *, understanding: Mapping[str, Any] | None
) -> int:
    """这一轮**被丢掉**的话题更新条数 —— 给调用方留痕用。

    **丢弃必须出声**，否则这次收紧就是把一个安静的错误换成另一个安静的错误：
    模型多发了几条，平台只认最后一条，而没有任何地方记下「它多发了」。
    `pipeline` 拿这个数落一行 `policy_decisions`。

    **只回条数，不回值。** 那些值是配包词表里的词，本身不敏感 ——
    但同一天（2026-09-16）刚有一次泄露是「把数据侧的东西打进留痕」造成的，
    而这里**不需要**那些值就能说清发生了什么：条数 + 槽名足够定位。
    """

    slot = definition.get(TOPIC_SLOT_KEY)
    if not slot:
        return 0
    matched = [
        update
        for update in (understanding or {}).get("slot_updates") or []
        if isinstance(update, Mapping) and str(update.get("slot")) == str(slot)
    ]
    return max(0, len(matched) - 1)


def current_stage(
    definition: Mapping[str, Any], *, delivered: Collection[str]
) -> str | None:
    """Which declared stage this conversation has reached, from what has gone out.

    The stages are an **ordered** list; the answer is the last one whose `after`
    refs have all been delivered. A stage declaring no `after` is reachable from
    the start, which is how the opening stage is written.

    `None` when the frame declares no stages -- then nothing is stage-filtered
    and every proactive material stays available, which is how a profile that
    never heard of stages behaves.
    """

    stages = definition.get(STAGE_KEY) or ()
    if not stages:
        return None
    reached: str | None = None
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        after = {str(ref) for ref in (stage.get("after") or ())}
        if after <= set(delivered):
            reached = str(stage.get("name"))
    return reached


def select_materials(
    *,
    definition: Mapping[str, Any],
    declared: Mapping[str, str],
    emit_kind: str | None,
    #: ⚠️ **这里没有 `frame`，而那是有意的**（2026-09-18）。
    #:
    #: 话术的 `on_request` 判据从「槽位现值」改成「这一轮问了」之后，
    #: 帧在这个函数里一处都用不到 —— 留着它并压掉那条未使用告警是最省事的做法，
    #: 而那样下一个人看到签名里有 `frame` 会以为话术仍然看槽位现值，
    #: 且把 `topic_asked_this_turn` 改回 `topic_text` 一行就够、签名不会拦他。
    #:
    #: **参数的缺席本身就是那道守卫。**
    #: 文件那一侧（`select_assets`）仍然收 `frame` —— 它的判据没变。
    understanding: Mapping[str, Any] | None = None,
    delivered: Collection[str] = (),
    #: 这场对话**已经真的发出去过**的话术 ref（平台从 `intent_records` 现读）。
    #:
    #: 与 `select_assets` 的同名参数**逐字同义**，判据也照搬：
    #: `on_request` 不滤（客户又问一次就该又拿到），`proactive` / `timer` 滤。
    #:
    #: ⚠️ **2026-09-18 之后这一条与上面那道判据是复合的**：
    #: `on_request` 话术本来就只在**这一轮问了**时才进候选，
    #: 所以「不滤」现在的意思是「他这一轮又问了，就又给他」——
    #: 而不再是从前那个「槽位还黏着，所以每一轮都给」。
    #: **两道一起看才是完整规则**，单看这一条会以为它还能每轮重复。
    already_sent: Collection[str] = (),
) -> dict[str, str]:
    """The materials this turn may quote, ref -> text, in the profile's order.

    `declared` is every material the frame declares **and** the package supplies
    words for (`platform_frames.materials_for_frame`); this narrows that set to
    the ones this particular turn is allowed to use. `emit_kind` is the kind of
    the scheduled event that produced a timer turn, and `None` for a customer
    message -- the same value `pipeline._timer_event_kind` derives, from the same
    place, so the two cannot disagree about what kind of turn this is.

    **Order is the profile author's** throughout: `content_materials` is a list,
    and the sequence a merchant wrote their materials in says which to lead with.
    """

    # **话术按「这一轮问了」判，不按槽位现值**（用户 2026-09-18 裁定）。
    #
    # 那个槽是黏的：运维实测它在 06:31 被写成「花钱」之后，
    # **每一轮三段报价话术都在候选里** —— 于是客户提一句「5000条」，
    # 模型手边就有价格，顺手说了，而他从没问过。
    #
    # **文件那一侧不改**（`select_assets` 仍用 `topic_text`）：
    # 「他又问一次价格，就该又拿到价目表」对文件成立，对话术不成立 ——
    # 同一句话逐字说三遍，客户读到的是复读。
    matched = topic_asked_this_turn(definition, understanding=understanding)
    stage = current_stage(definition, delivered=delivered)
    selected: dict[str, str] = {}
    for declaration in declarations(definition):
        ref = str(declaration.get("ref"))
        text = declared.get(ref)
        if text is None:
            continue
        if not _is_offered(declaration, emit_kind=emit_kind, topic_text=matched, stage=stage):
            continue
        if ref in already_sent and declaration.get("availability") in (
            PROACTIVE,
            TIMER,
            ON_REQUEST,
        ):
            # **一段话术，一通对话里只逐字发一次**（用户 2026-09-20 裁定）。
            #
            # ## 这一行 2026-09-20 从「两档」改成了「全部」
            #
            # ~~`if ref in already_sent and declaration.get("availability")
            # in (PROACTIVE, TIMER)`~~ —— 原文划掉保留在下面那几段里，
            # 因为**它当时的理由是对的，错的是把它套到了话术上**。
            #
            # **运维 §八 实测**：一小时里 `material:demo-price-plain:v1`
            # 被逐字引了 **3 次**（16:37:50 / 16:38:41 / 16:39:19），
            # `demo-price-flat:v1` 2 次。判据是运维同节写下的那一条：
            #
            # > 同一段固定话术**在一通对话里逐字说两遍以上**，就算缺陷。
            #
            # ⚠️ **而这不是「把资料那条规则抄过来」** —— 资料那一档的豁免
            # 仍然在（`select_assets` 一个字没动）：**他又问一次价格，
            # 就该又拿到价目表，那不是重复推送。**
            # 运维原话逐字：「那句话对**文件**成立；对一段**话术**不成立 ——
            # 同一句话逐字说三遍，客户读到的是复读。」
            #
            # ## 拦下来之后客户还拿得到价格吗 —— 拿得到，而这是裁定的一半
            #
            # **用户 2026-09-20 裁定：「第一次用商家给的固定话术，
            # 之后简单报价就行。」** 所以帧目标那一句同批改了：
            # 第一次引原文，之后直接说那个数。
            #
            # ⚠️ **不改帧目标会造出一个新的失败形态**：goal 仍写着
            # 「本轮第一句引 `demo-price-plain:v1` 原文」，而那个 ref
            # 已经不在候选里 —— 模型照做就提名一个未提供的 ref，
            # 整条被 `GENERATION_MATERIAL_REF_UNKNOWN` 拒掉。
            #
            # **而「之后简单报价」不会被那道数字闸丢掉**（开工前实测）：
            # `_CUSTOMER_PAYMENT_CLAIM` 只匹配「您/你…付/交/花/掏…<数>」，
            # 也就是**替客户认下一笔支出**；平铺直叙地说「一个月 1888」
            # 不在它的形状里。
            #
            # ---
            # 以下是这一行原本那两档的理由，**一个字没删**：
            #
            # **正向列举那两档，不写成「不是 on_request」**（复核 2026-09-18 退回）。
            # `content_materials` 的 schema `required` 只有 `ref`
            # （资料那边是 `['ref','availability']`），所以**「没声明
            # `availability`」是一个合法档**，两种拼法都合法：裸字符串 ref，
            # 与只带 `ref` 的对象。而它的含义写在 schema 的 description 与
            # 上面 `_is_offered` 的注释里：**「不受限制，每轮都递」**。
            #
            # 第一版写的是 `str(...) != ON_REQUEST` —— `str(None)` 是
            # `"None"`，不等于 `"on_request"`，**于是那一档被滤掉了**，
            # 与它自己声明的含义相反。今天实际影响是 0（三个配包 35 处声明
            # 全带了 `availability`），**所以它不是在跑的 bug，是埋好的那种**。
            #
            # 在此之前**只有资料有这一档**：`select_assets` 滤 `already_sent`，
            # 而这里只拿 `delivered` 算阶段，一条 `proactive` 话术**永远在被递**。
            # 于是「只推一次」那条只能写进 `goal`，靠模型自己填的
            # `material_delivered` 记账 —— 而同一个仓里 `delivered_materials`
            # 的注释正是为了避免这件事才写的：「Not the model's report…
            # guess wrong, select the wrong materials, and nothing goes red」。
            #
            # ~~`on_request` 不滤：他又问一次价格，就该又拿到那句报价。~~
            # **↑ 这一句 2026-09-20 被推翻**，而推翻它的是同一个文件里
            # `topic_asked_this_turn` 的 docstring（2026-09-18 写的）：
            # 「**话术不是这样：同一句话逐字说三遍，客户读到的是复读。**」
            # **两段注释在同一份文件里对同一个决定说了相反的话**，
            # 而跑的是这一段 —— 本仓「同一条规则两个定义点」那一族，
            # 只是这一次两个定义点都是散文。
            continue
        selected[ref] = text
    return selected


#: 帧上声明文件的那个键。**与 `content_materials` 是两个键，装的是两类东西**：
#: 一个是话术（`material:`，解析成商家原文进正文），一个是文件（`asset:`，走 sendFile）。
#: `runtime.references` 那份 docstring 把这条分界线写在了「模型看不看得见」上。
CONTENT_ASSETS_KEY = "content_assets"


def select_assets(
    definition: Mapping[str, Any],
    *,
    frame: Mapping[str, Any] | None = None,
    understanding: Mapping[str, Any] | None = None,
    # **`Collection[str]`，与 `select_materials` 逐字相同** —— 它是已投递的
    # ref 集合，不是一串行。第一版按「一串记录」标，**用例全绿而 mypy 红**：
    # 注解写错不改变运行时的任何事，那正是这个仓最常撞的那种形状。
    delivered: Collection[str] = (),
    emit_kind: str | None = None,
    already_sent: Collection[str] = (),
) -> tuple[dict[str, str], ...]:
    """这一回合可以提给模型的文件，**帧声明的顺序**。

    回的是 `({"ref": ..., "label": ...}, ...)` —— **只有引用与一个标签，没有内容**。
    标签是 ref 的第二段（slug）：schema 今天不允许商家另写标题，见下面那段注释。
    文件的内容不是字；把文件名喂给模型让它描述，等于让它替一份没读过的文件作担保。

    ## 可用性语义**复用 `_is_offered`，不另立一套**

    帧上的 asset 声明与 material 声明形状相同（`availability` / `topics` /
    `stages` / `emit_kinds`），而封版时对 asset 强制的两档
    （`on_request` 要 `topics`、`proactive` 要 `stages`，见 `profiles/registry.py`）
    正是 `_is_offered` 已经实现的那两档。

    **另写一份会是本仓付过好几次钱的那种错**：两份规则各自演化，
    某一天封版这边拒而运行期那边放行 —— 而那时的症状是一个本该关住的文件
    每回合都递到模型面前，**包却是绿的**。

    ## 主动那两档**发过就不再递**

    用户 2026-09-17 裁定：「除非客户明确要，不然资料只能主动发一次。」

    `on_request` 不受这条管 —— 那一档的触发条件本身就是客户问到了。
    `proactive` / `timer` 是平台自己决定递的，递过一次就够了。

    ## 它不查目录

    这里不问 `materials` 表里有没有这一行、传没传过 —— 那是**发送那一刻**
    的事（`pipeline.resolve_asset_for_send`）。分成两处是有意的：
    一份刚导进目录还没上传的资料，模型**仍然**可以提，而那次提议会在派发时
    得到一条 `material_not_ready` 的失败回执，**带着修法**。
    反过来（这里就滤掉）会让运维看不见「它提了但发不出去」这件事。
    """

    matched = topic_text(definition, frame=frame, understanding=understanding)
    stage = current_stage(definition, delivered=delivered)
    chosen: list[dict[str, str]] = []
    for item in (definition.get(CONTENT_ASSETS_KEY) or []):
        if not isinstance(item, Mapping):
            continue
        reference = str(item.get("ref") or "").strip()
        if not reference:
            continue
        if not _is_offered(item, emit_kind=emit_kind, topic_text=matched, stage=stage):
            continue
        if reference in already_sent and str(item.get("availability")) != ON_REQUEST:
            # **主动只发一次**（用户 2026-09-17 裁定）：
            # 「除非客户明确要，不然资料只能主动发一次。」
            #
            # `on_request` 就是「客户明确要」那一档 —— 他又问了一次价格，
            # 就该又拿到价目表，这不是重复推送。
            # `proactive` / `timer` 是我们自己决定递的，**递过一次就够了**：
            # 同一份 PDF 在一场对话里出现两次，客户看得见。
            #
            # **过滤在这里而不是在发送那一刻**：这里滤掉等于模型根本看不见它，
            # 也就不会把它写进方案；在派发时拦会得到一条「提了又没发成」的失败记录，
            # 而那读起来像一次故障。
            continue
        chosen.append(
            {
                "ref": reference,
                # **标签就是 slug，而商家今天没法改它。**
                #
                # `merchant-profile.schema.json` 的 `content_assets` 是
                # `additionalProperties: false`，允许的五个字段里**没有 `title`**。
                # 第一版这里读了 `item.get("title")` —— 那一支**永远命中不了**，
                # 是一段声称某字段存在的死代码，正是本仓最贵那类错的形状
                # （只是方向相反：不是「声明了但不存在」，是「读了一个不存在的」）。
                #
                # 所以标签只能从 ref 里取，而这意味着**它好不好取决于 slug 起得好不好**：
                # `price-list` 够模型判断，`doc-2` 不够。
                # 要让商家能给一份文件写一句人话，得给 schema 加一个 `title` ——
                # 那是一次对外契约的扩张（每份包要回填，且每一条候选都吃提示词字节）。
                # **用户 2026-09-16 裁定：先用真实包试，不够再加。**
                "label": reference.split(":")[1],
            }
        )
    return tuple(chosen)


def _is_offered(
    declaration: Mapping[str, Any],
    *,
    emit_kind: str | None,
    topic_text: str,
    stage: str | None,
) -> bool:
    availability = declaration.get("availability")

    # **阶段先看，而且对每一档都看**（ADR-0149，2026-09-22）。
    #
    # ⚠️ **它先前只长在 `proactive` 那一支里面**，于是三种声明方式里
    # **没有一种是「定时器轮次能用 + 仍按阶段走」**：
    #
    # | 声明 | 定时器可用 | 受阶段约束 |
    # |---|---|---|
    # | 不声明 `availability` | ✅ | ❌ 早返回，到不了阶段那一段 |
    # | `proactive` | ❌ | ✅ |
    # | `timer` | ✅ | ❌ 阶段只在 proactive 分支里检 |
    #
    # 而「对话走到哪一步了」与「这是哪一种轮次」**是两个正交的问题** ——
    # 绑在一起是耦合，不是裁定。契约那一行的措辞（「in which a `proactive`
    # material may be used」）记的是耦合当时的样子，同批一并更正。
    #
    # **提出来之后**：演示包那几段可以去掉 `availability`（于是两种轮次都能用），
    # 而 `stages` 仍然生效 —— 这正是用户 2026-09-22「演示包让 timer 能调用
    # 所有能发的资料」要的，而在此之前它做不到：去掉 `availability` 会连
    # 阶段阶梯一起丢掉，而那个阶梯正是「第二轮该讲能力那段」的唯一机制。
    stages = declaration.get("stages") or ()
    if stages and not (stage is not None and any(str(name) == stage for name in stages)):
        return False

    if availability is None:
        # Declared without restriction. Offered on every turn, which is how a
        # profile written before this mechanism existed already reads.
        return True

    if availability == ANY_TURN:
        # **两种轮次都给**（ADR-0149）。阶段已经在上面看过了。
        #
        # 与「不声明」的区别**不在这里，在 admission**：这一个是说出来的，
        # 所以它进字节预算、进每一道检查；那一个是省略，而省略正是
        # ADR-0125 §6.2 关掉的绕法。**选取这一层两者行为相同是对的** ——
        # 差别本来就该在「有没有被算进账」上，不在「发不发」上。
        return True

    if emit_kind is not None:
        # **A timer turn is offered its rule's materials and nothing else**, the
        # same way B4-4d gives a timer turn one strategy rather than a menu. The
        # material document says why in product terms: 「把开场钩子发到跟进回合里
        # 就走味了」. Enforcing it here rather than asking for it in a prompt is
        # the whole difference between a rule and a request.
        return availability == TIMER and emit_kind in (
            declaration.get("emit_kinds") or ()
        )

    if availability == TIMER:
        # ...and the converse, which matters just as much: follow-up wording is
        # not something to say in the middle of a live conversation.
        return False

    if availability == PROACTIVE:
        # **The proactive bucket is the one that needed a gate and did not have
        # one.** Measured on the 301-segment set: the reactive bucket is 124
        # segments across 22 topic groups (4-9 each, median 6), so the topic gate
        # already narrows it to something a prompt can hold. The proactive bucket
        # is **164 segments with no gate at all** -- offering it whole is ~53 KB
        # against the `cost.MAX_MODEL_INPUT_BYTES` hard limit, which is a refusal
        # before the request leaves the process, not a bigger bill.
        #
        # **The limit was 16 KB when this was measured and is 49,000 since
        # 2026-09-15** (the budget-chain batch, `763b477b`). The conclusion
        # survives -- 53 KB still does not fit -- **but the margin did not**: it
        # went from 3.3x over to 1.1x over. Spelling the number here again would
        # be a fourth copy of a constant that already moved once; naming the
        # constant is what keeps this paragraph true the next time it moves.
        #
        # Stage is what narrows it: one stage's worth is 6 segments, so a turn
        # offers roughly 6 proactive + 6 reactive against a ceiling of about 24.
        #
        # ⚠️ **阶段那一段 2026-09-22 挪到了函数顶部**（ADR-0149），
        # 因为它对每一档都该生效，不只对这一档。**判据一个字没改** ——
        # 挪之前是「声明了阶段就必须命中当前阶段」，挪之后逐字相同，
        # 只是也管着 `timer` 与「不受限」那两档了。
        #
        # 剩在这里的是这一档自己那半：**没声明阶段的 `proactive` 素材照样放行**。
        # 只有「整帧都没声明阶段」时才到得了这里 —— 帧一旦声明了阶段，
        # admission 就要求每一段 proactive 素材点名自己的阶段，因为
        # 「不声明 = 不受限」在 301 段规模上意味着 134 段无条件放行，
        # 那不是素材不受限，是闸没关。
        return True

    if availability != ON_REQUEST:
        # **Fail closed on a value nobody defined.** The schema's enum is the
        # first lock and `AVAILABILITIES` pins it to these three, but this branch
        # used to be a fall-through: an availability the platform did not
        # recognise was treated as `on_request` and offered. A material whose
        # availability is a typo must be offered on no turn, not on the turns the
        # least restrictive value would have allowed.
        return False

    # **A material declaring no topics is the ungated one** -- the
    # safety valve, and it is deliberate rather than a hole. The failure
    # direction of a topic gate is that a mis-filled slot hides the material that
    # answers the question, and the model then writes its own answer, which is
    # the exact harm this whole mechanism exists to prevent. A profile that
    # declares an ungated "I am not sure" material turns that failure into
    # saying so instead of inventing.
    topics = declaration.get("topics") or ()
    if not topics:
        return True
    return any(str(topic) and str(topic) in topic_text for topic in topics)
