"""Model-backed implementation of the three-stage inference boundary.

The model supplies semantics; the platform supplies identity. Fields that
establish what a document *is* -- `schema_version`, `event_id`, `origin` and
`model_evidence` -- are written here from measured facts, never copied from the
model's own claim, so a model cannot mislabel its output as something the
platform produced or under-report what it cost.

Prompts live in a versioned constant rather than a database: a prompt change
must be a reviewable diff, and `prompt_release` in the evidence must resolve
back to it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from fastagent_v2.runtime import request_shape
from fastagent_v2.runtime.contracts import (
    inference_contract_violations,
    validate_inference_contract,
)
from fastagent_v2.runtime.topic_vocabulary import allowed_slot_values
from fastagent_v2.runtime.understanding_bound import bound_for_prompt
from fastagent_v2.worker.cost import CallContext, CostGovernor
from fastagent_v2.worker.metrics import UNDERSTANDING_ENTRIES_DROPPED_TOTAL
from fastagent_v2.worker.model_policy import ModelPolicyRelease, RoleBinding
from fastagent_v2.worker.platform_frames import frame_definitions
from fastagent_v2.worker.providers import (
    ChatCompletion,
    OpenAICompatibleChatClient,
    ProviderMalformedOutput,
    model_evidence,
)
from fastagent_v2.worker.strategies import STRATEGY_CATALOG, time_rule_strategy_set
from jsonschema import ValidationError

#: **v8 (2026-09-12)**: the generation prompt gained `knowledge_chunks` -- the
#: retrieved reference material for this turn (`{ref, text}` only, ADR-0135) --
#: and the rule that separates it from `content_materials`: **a material is
#: quoted by ref, a knowledge chunk is not**. Knowledge is reference material the
#: model writes its own words from, and it must **cite** the chunk's ref in
#: `claims[].source_evidence_refs`; quoting it verbatim would send words no
#: merchant approved (裁定四: 素材优先, and materials are the only approved wording).
#: **The citation is checked** against this turn's offered refs -- before this,
#: `evidence_required` only checked that the list was non-empty, never what was
#: in it, so a fabricated provenance passed.
#:
#: **v7 (2026-09-12)**: the plan prompt gained `knowledge_query` -- one optional
#: search string that decides whether this turn looks anything up (裁定三,
#: `knowledge-base-schedule-2026-09-11.md` §5.2.1: retrieval is the fourth
#: pipeline step, and **the plan step is what decides it happens**). It is not a
#: `generation_constraints` member (that vocabulary is closed and holds
#: prohibitions, ADR-0123) and not a boolean (only this step knows *what* to look
#: up). **Nothing reads it yet** -- the retrieval layer is 6b; this batch lands
#: the criterion, and `inference_validation.requested_knowledge_query` is the one
#: place that will answer 「这一轮请求检索了吗」.
#:
#: **v6 (2026-09-10)**: the generation prompt now says which document the
#: customer actually receives. v5 attached the material rule to
#: `customer_visible_segments` and showed `"proposed_intents": []` in both
#: examples -- so the model quoted in the draft, left the payload to its own
#: prose, and the platform sent the payload. Measured over three real turns:
#: the draft quoted 1 / 3 / 1 materials and the payload quoted **none**, six
#: arms out of six. Both examples now carry a `send_message` payload, and the
#: rule that the two must agree is in the validator as well as here.
#:
#: **v10 (2026-09-21)**: the plan prompt is told to list a second
#: `send_message` when one short message will not hold the answer, and the
#: generate prompt is given a **ceiling of 60 characters per message -- a
#: ceiling, not a target** (user's ruling: 「生成不是每次都要 60 字，60 字是上限」).
#:
#: **Where 60 comes from, measured on the shipped demo package**: the
#: merchant's own approved wording runs 17-115 characters with a median of 45
#: (remeasured 2026-09-24 after the bridge material gained real content),
#: and the customer writes about 10. The model's own replies averaged **101**
#: on 2026-09-20 -- every one of them longer than the longest thing the
#: merchant ever wrote.
#:
#: ⚠️ **There is no platform-side cut, on purpose** (user's ruling:
#: 「不能设硬线，长段落也比说错话强」). Two reasons, and the second is
#: structural: a mechanical cut severs 「因为…所以…」, **and** an outbox row's
#: idempotency key is `{prefix}:{event_id}:{candidate_basis}` under
#: `UNIQUE (tenant_id, idempotency_key)` -- one proposal, one row, one planned
#: candidate. A platform-made second row has no key to carry.
#:
#: ⚠️ **Why the rule names the plan's list**: proposing more than the plan
#: listed raises `GENERATION_DUPLICATED_INTENT`, which refuses the whole
#: document -- **the customer would get nothing**. A bare "split it up"
#: instruction turns long answers into silence, which is the one outcome the
#: ruling above rules out.
#:
#: **v17 (2026-09-24, 运维缺陷 22)**：**删掉 `evidence_span_hash`。**
#: 它要求模型对证据片段报 SHA-256 —— **而 LLM 结构上算不了哈希**，
#: 上线以来每一个值都是编造的 hex（63 位那次只是编得不够像），
#: 编错一位的代价是整份 understanding 作废、客户零收到（stage 2026-09-23 实测）。
#: 平台不验证它（T41 自认「the model's own claim with nothing checking it」）、
#: `facts` 零消费方、understanding 文档不持久化 —— 三层都空。
#: 将来真要证据可追溯，按 `evidence_event_id` 的路（平台盖章）另建，
#: 不复用一个模型必然造假的字段。契约同批收缩（`facts` 的 required 三键）。
#:
#: **v16 (2026-09-23, 运维缺陷 18)**：**v15 的那条规则，被它自己的示例拆了。**
#:
#: v15 在 `turn_summary` 那一条上写了「never say what we did, what we answered」，
#: **而四行之下的示例 JSON 逐字示范了那件事**：
#: `"turn_summary": "example: he asked what the price covers; we quoted it."`
#: —— `we quoted it` 就是禁的那一句。
#:
#: ⚠️ **这与 v15 自己修的缺陷 15 是同一个形状**：配包 goal 明文禁止「我记下了」，
#: 而生成步示例写着「收到。」。同一批里修了生成步那一处，**理解步这一处没看**。
#: 本仓的留痕当时就写下了判据：**范例的说服力强过散文禁令**。
#:
#: stage 2026-09-23 的现场：摘要相邻两行，第 6 行「本轮尚未报价」，
#: 第 7 行「He asked us to repeat the price **we had mentioned**」——
#: 后者正是示例的翻版，而整份 `conversation_summary` 都在模型的输入里，
#: **它看得见第 6 行，仍然写下了与之相反的断言**。
#:
#: 同批另外两件（都在那一条 bullet 里）：
#:
#: * **他以为我们讲过，不等于我们讲过。** 客户上一句是「刚才那个价格再说一下」，
#:   而那个价格从没说出来过。规则补一句：把那个预设写成**他的**，不写成发生过的事；
#: * **摘要的语言没有任何规则**（改动前 `grep -n "语言\|language"` 在提示词与
#:   `understanding-result.schema.json` 里都是 0 命中）。那第 7 行是英文，
#:   其余七行是中文。补一句「用他写的那种语言」。
#:
#: 字节账：理解步 **+166**（实测，不是估的 —— 两张表由
#: `test_profile_materials_are_trusted` 与 `test_request_floor_equals_the_real_request`
#: **现量现比**，第一版按 +82 写进去，后者当场报了 `8149 != 8315`）。
#:
#: ⚠️ **这 166 不从 `MATERIAL_BYTES_BUDGET` 里扣**，而先前几批的记法容易让人照扣：
#: 那个池子是 **`generate` 的**，钉它的恒等式量的是
#: `request_shape.generate_prompt` 的空请求基线 —— **理解步的 system prompt
#: 不在那份 payload 里**。v15 那批也是这么处置的（扣了生成步的 994、
#: 没扣理解步的 149），只是没留下这句话。详见 `registry.py` 那个常量上方。
#:
#: **v15 (2026-09-23, 运维缺陷 15 / 16)**：
#:
#: **16 —— 摘要为什么会记错，是结构性的。** `turn_summary` 由**理解步**产出，
#: 而理解步跑在计划与生成之前 —— **那一刻回复还不存在**。
#: 而契约与提示词都要它答「what the customer wanted **and what we did**」——
#: **后半句在结构上不可能答对**，模型只能猜。
#: stage 2026-09-23 它猜了「本轮未给出实质回答」，**而回答就在同一轮的第一条**；
#: 那句猜测进了滚动摘要，`request_shape` 让后面每一轮都读到它。
#: 改成只问他那一半，并明说「the reply does not exist yet」。
#:
#: ⚠️ **这段留痕写在这里，不写在契约的 description 里** ——
#: 那份 schema **整份进请求**，实测把它写进去让响应 schema 从 3,300 涨到 3,786，
#: **每回合都付**。`registry.py` 为同一件事记过三次同样的账。
#:
#: **15 —— 回执话术是我们自己教的。** 配包 goal 明文禁止「我记下了」那五个短语，
#: 而生成步的**示例**逐字示范了其中一种：`"strategy_id": "acknowledge"`
#: 那一段的 `content_or_ref` 原来是**「收到。」**，一个字不差。
#: 两份文档对模型说了相反的话，**而范例的说服力强过散文禁令**。
#: 用户 2026-09-23：「**想办法根除……词汇无法穷尽，闸是加不完的**」——
#: 所以换掉示例（改成一句真的在答他的话）并写成正面规则，**不加词表闸**。
#:
#: **v14 (2026-09-23, ADR-0151)**: **一次生成，多次发出。** 计划步从
#: 「一轮可以带一条以上 `send_message`、装不下就列第二条」改成
#: **「至多列一条」**；生成步的 **60 字上限整个去掉**，换成
#: 「长答案自己分段，每段各成一个气泡」。许诺的兑现物也从
#: 「计划列的下一条 `send_message`」改成 **「下一个分段」**。
#:
#: **为什么 60 字那条不是原因而是补偿**：计划步要在文字写出来之前先猜
#: 这个答案需要几个气泡，猜 1 而答案要 3 时，写手只能把话压进 60 字。
#: 用户 2026-09-23：「**有很多情况不是 60 个字能讲明白的**」。
#:
#: 字节账：计划步 **+28**、生成步 **+408**，两笔都从素材池子里扣
#: （`MATERIAL_BYTES_BUDGET` 23,984 → 23,573），安全余量一个字没动。
#:
#: **v11 (2026-09-21)**: the generate prompt is finally told **how to remember
#: anything**. Two gaps, and the second is the deeper one:
#:
#: 1. the payload shape (`field_key` / `field_value`) appeared **nowhere** in
#:    this file -- `grep -c field_key` returned 0 while
#:    `_remember_payload_refusal` has been refusing on it all along;
#: 2. the prompt never received **which keys are legal**. It got
#:    `remembered_facts` (what is *already* known) and nothing else, so the
#:    first time a fact was worth remembering there was no precedent to copy.
#:
#: Stage measured both at once on 2026-09-20: the model proposed
#: `{'concern_topic': ...}` -- wrong shape **and** undeclared key -- and
#: `_remember_payload_refusal` stopped it. **The customer saw nothing**: the
#: reply in that same turn went out normally, and only the memory write was lost.
#:
#: ⚠️ **Same shape as ADR-0144 decision 3, and the same fix**: a closed
#: vocabulary the model cannot see is a vocabulary it will guess at.
#: `rememberable_fields` is to memory what `slots_wanted` is to slots.
#:
#: ⚠️ **v10 与本条同日，而它先落地**（`481e834a` 一条说一件事，60 字上限）。
#: 本支起初就占了 v11、没有去抢 v10 —— 因为两份不同的提示词正文
#: **永远不许共用一个 release 串**，而哪一支先合是合并窗口的事。
#: rebase 到 `5fe784a5` 之后两笔叠在一起，**字节账因此整笔重量**（见下面那张表）。
#:
#: **v9 (2026-09-20)**: the understanding prompt finally **asks for
#: `turn_summary`**. T200 put it in `understanding-result.required` and never
#: told the model -- while the same paragraph says `Do not add properties.`
#: **So the model was explicitly told not to produce the key the validator
#: then killed the whole document for missing.** Stage measured it on the first
#: real customer message after deploy (`64eedf7c`, 2026-09-20T10:52:04Z):
#: one `UNDERSTAND_VALUE` call, then `PROVIDER_MALFORMED_OUTPUT`, zero outbound.
#: The plan and generation steps were never reached.
#:
#: ⚠️ **The gate stayed green throughout**: the deterministic double supplies
#: the key itself (`inference.py`), so every stubbed path passes and the only
#: path that fails is the real model -- which no test exercises.
#: `test_the_prompt_asks_for_every_key_the_contract_requires` is the guard that
#: would have caught it, and it is in the same batch as this bump.
#:
#: **v5 (2026-09-10)**: the generation prompt gained the frame's
#: `content_materials` -- the merchant's approved wording, ref and text together
#: -- and the rule that says to quote one by reference rather than rewrite it.
#: Until v5 the generation step received no merchant words at all, so「素材只能由
#: profile 声明、模型不得现场编造」was a constraint with nothing to constrain.
#:
#: **v4 (2026-09-10)**: the plan prompt gained the closed set of
#: `generation_constraints` that the selected strategy declares.
#:
#: Moved because a prompt change that keeps its release string makes every
#: stored `model_evidence` point at the wrong prompt.
#: `test_prompt_release_moved_with_the_prompt` and
#: `test_prompt_release_and_v4_replay_golden_move_together` both exist so that
#: forgetting is not one of the available outcomes.
#:
#: ⚠️ **那两条守的是「改了提示词要抬版本号」。本次是反方向的那一半，
#: 而在此之前它一条守卫都没有：契约加了一个必填键，提示词该跟着变而没变。**
#: 经过见下面 v9。
PROMPT_RELEASE = "m1a-prompt-v17"

_SILENCE_SYSTEM = """You read a silence. The customer has said nothing; there is no message to
interpret. You are asked why the conversation stopped moving and what would help.

Return one JSON object only. Do not write the message to the customer -- that is
the generation step's job and anything you write there is discarded.

The first response character must be { and the last must be }. Emit no leading
whitespace, explanation or markdown.

Required keys: reading, action.

- `reading` -- your own read of why nothing is happening, in your own words.
  **There is no list to pick from, deliberately**: the situations do not
  enumerate. Say what you actually think is going on, in one short paragraph.

- `action.do` -- what this turn should do. Exactly one of:
  - `wait` -- say nothing this turn. Pick it when speaking now would add
    nothing he has not already been told. **This is a real answer, not a
    failure**: a turn that says nothing is better than a turn that repeats.
  - `ask_again` -- put the open question to him a different way.
  - `send_material` -- send one of the merchant's approved pieces, named in
    `action.material_ref`.
  - `hand_to_human` -- this needs a person.

- `action.material_ref` -- required when `do` is `send_material`, null
  otherwise. **It must be one of the keys of `materials_offered_now`.** Do not
  invent a ref and do not name one from `materials_already_sent`: the first
  cannot be sent, the second he has already received. `materials_offered_now`
  is often empty -- that is normal, and it simply means `send_material` is not
  available this turn.

- `action.because` -- one line: how this action serves `current_goal`.

**Stay on `current_goal`.** It is the merchant's own objective for this frame.
Reading a silence does not licence changing the subject: whatever you choose has
to move that goal, or be `wait`.

Two facts deserve reading carefully, because they separate cases that otherwise
look identical:

- `silent_for_seconds` -- how long it has been since we spoke. A few seconds
  means he has not had a chance to reply yet; hours mean something else.
- `unanswered_rounds` -- how many times we have spoken in a row with no reply.
  **Following up again can be the right sales move even with no answer** -- one
  well-judged nudge is not the same thing as nudging repeatedly. This number is
  how you tell those apart.
"""

_UNDERSTAND_SYSTEM = """You extract facts from one inbound event in a business conversation.

Return one JSON object only. Do not select a strategy, do not write any message
to the customer, and do not call tools -- those belong to later steps and any
attempt is discarded.

The first response character must be { and the last must be }. Emit no leading
whitespace, explanation or markdown. Keep the object concise: at most one item
per array for this single event, and prefer [] over speculative content. Finish
the JSON object well within the output-token limit.

Required keys: facts, intents, slot_updates, interruption, sentiment,
risk_markers, customer_commitments, uncertainties, turn_summary.

- interruption.relation says **what this message does to the goal we are on**.
  It is not a mood label -- each word changes what the platform is allowed to
  do next, so pick by the consequence, not by how the sentence feels:
  - `same_goal` -- he is still on it: answering, asking about it, or saying
    nothing new. **The default.** Also use it when he asks again about
    something already covered; the reply stays in the current frame and
    answers briefly in its own words.
  - `side_question` -- he asked about something else and wants an answer
    before we go on. This is the one that lets the plan open a child frame.
  - `new_goal` -- he wants a different thing done, not a detour.
  - `correction` -- he is telling us an earlier reading was wrong. **This one
    also unlocks overwriting a slot** whose confidence would otherwise refuse
    the change, so use it only when he actually contradicts something.
  - `exit` -- he wants this to stop ("不用了", "别再发了").
  - `human_request` -- he asked for a person.
  ⚠️ `exit` and `human_request` are **durable**: they are recorded on the
  conversation, they are what justifies abandoning a goal, and they stop
  proactive follow-ups. **Never use them for ordinary impatience** -- "你们怎么
  这么慢" is `same_goal` with a negative sentiment, not an exit.
- sentiment is one of: positive, neutral, negative, unknown.
- risk_markers items may only be: none, complaint, urgency, price_pressure,
  compliance_request, escalation_request, unverified_identity. Use ["none"]
  when no marker applies; never invent a marker label.
- Every fact needs a confidence between 0 and 1. State low confidence rather
  than guessing; an uncertainty entry is always preferable to an invented value.
- Report a customer commitment only when the customer stated it. Never infer a
  due date that was not said, and pick its `type` from the schema's list.
- turn_summary: one line on **what he said and what he wants** in this message
  (<=480 chars). **You are reading his message; the reply does not exist yet**,
  so never say what we did, what we answered, or whether the turn was answered
  -- **including when he assumes we already did**: write that assumption down as
  his, never as something that happened. Never restate earlier turns; write one
  even when little happened. Write it in the language he wrote in.
- slot_updates carries the values this event supplies for the goal's declared
  slots. Each item is exactly {slot, value, confidence}. `slot` must be copied
  from the `slots_wanted` list in the input -- never invent a slot name, and
  never send a slot that is not in that list. Emit an item only when this event
  actually states the value; omit it when the value is merely implied. Use []
  when the event supplies nothing.

Use these exact JSON field names and nested shapes. Do not add properties. Empty
arrays are valid when there is no evidence. Example JSON output:
{
  "facts": [{"key": "request_summary", "typed_value": "example", "confidence": 0.9}],
  "intents": [{"intent_label": "ask_information", "confidence": 0.9}],
  "slot_updates": [{"slot": "request_summary", "value": "example", "confidence": 0.9}],
  "interruption": {"relation": "same_goal", "suggested_frame_definition_id": null},
  "sentiment": "neutral",
  "risk_markers": ["none"],
  "customer_commitments": [],
  "uncertainties": [],
  "turn_summary": "example: he asked what the price covers and wants a number."
}
Every facts item must contain only key, typed_value and confidence. Every
uncertainties item contains exactly field and reason; do not add confidence or
suggested_value."""

_PLAN_SYSTEM = """You choose the next strategy for one business conversation.

Return one JSON object only. You may only select a strategy from the allowed set
given below; anything else is rejected by the platform and replaced with a safe
fallback, which is worse for the customer than a correct conservative choice.

Required keys: target_stack_operation, selected_strategy_id,
strategy_reason_code, confidence, required_slot_requests,
scheduled_event_changes, candidate_intents, generation_constraints,
manual_review_reason.

- target_stack_operation.op is one of: keep, push, repair, request_pop,
  complete, abandon. Use complete only when every required slot is already
  filled; believing the goal is met is not sufficient.
- `frames_entered` is every frame this conversation has already been in, in
  order, **including ones it has already finished and left**. The stack does
  not remember those: a frame that completed and was popped leaves no trace
  there, so this list is the only place that answers "have we been here".
- **Before proposing `push`, check that list.** A frame whose `revisit` is
  `brief_answer` is refused on a second entry (`FRAME_ALREADY_ENTERED`) and
  the turn falls back to a deterministic plan -- which is often silence, so
  **the customer gets nothing**. When he asks again about something such a
  frame already covered, **stay where you are and answer it briefly in your
  own words**: he wants the answer again, not the whole routine again.
  A frame whose `revisit` is `reenter` may be pushed again -- that is what it
  is for.
- strategy_reason_code is UPPER_SNAKE_CASE.
- candidate_intents may only use intent kinds the strategy allows.
- **List at most one send_message.** How many bubbles the answer needs is
  decided by the text, and the text does not exist yet when you plan. The
  writer splits it; the platform sends each piece as its own message, in order.
  If the reply will say you are about to explain or send something, the thing
  itself goes in that same send_message as a later segment -- not a second
  intent.
- unkept_promises, when it is not empty, holds sentences a previous attempt at
  this same turn promised and did not keep. This is the retry: the send_message
  you list is the one that delivers them.
- generation_constraints may only contain names listed for the strategy you
  selected in allowed_generation_constraints_by_strategy. Do not invent a name
  and do not rephrase one: "ask_one_question" is not "one_question". An empty
  list is correct only when the strategy declares none; leaving out a name the
  strategy does declare removes that constraint from the generated message.
- required_slot_requests must name every slot listed for your selected strategy
  in required_slots_by_strategy that current_frame.slots still leaves empty.
  Naming a different slot does not count: listing "rental_period" while
  "item_category" is the empty one leaves item_category skipped. Asking inside
  the message text does not count either -- the platform reads this list, not
  your wording.
- pending_question, when it is not null and its answer_evidence is null, is a
  question we already sent (question_text) that the customer has not answered.
  He heard it: do not plan a turn whose point is asking it again. Wait, or
  move forward another way.
- knowledge_query is optional: one short search phrase for a knowledge-base
  lookup this turn, or null when none is needed. Write what to look up, not the
  customer's own words -- "what about this one" retrieves nothing. Omitting it
  means nothing is looked up, so a turn that needed a source can only answer
  "I am not sure".
- Do not write customer-visible text here.

Use these exact JSON field names and nested shapes. Do not add properties. Example
JSON output for a conservative no-side-effect plan:
{
  "target_stack_operation": {"op": "keep", "target_frame_definition_id": null},
  "selected_strategy_id": "wait",
  "strategy_reason_code": "WAIT_FOR_MORE_INFORMATION",
  "confidence": {"raw_score": 0.7, "calibrated_score": null,
                 "assessment_version": "model-v1", "factor_codes": []},
  "required_slot_requests": [],
  "scheduled_event_changes": {"create": [], "cancel": [], "reschedule": []},
  "candidate_intents": [],
  "generation_constraints": [],
  "knowledge_query": null,
  "manual_review_reason": null
}
Every object must use the shown field names. In particular, never rename
target_stack_operation to action or operation. confidence.factor_codes may only
contain: evidence_complete, evidence_missing, slot_conflict, low_signal_input,
ambiguous_intent, policy_constrained, deterministic_output, fallback_applied.
Use [] rather than inventing a code."""

_GENERATE_SYSTEM = """You write the customer-visible draft for an already-decided strategy.

Return one JSON object only. The strategy is fixed: you may not change it, and
you may not introduce an intent the plan did not list.

Required keys: strategy_id, customer_visible_segments, proposed_intents,
confidence, claims, return_confirmation, safety_markers.

- knowledge_chunks is reference only: use your own words and cite its ref in
  source_evidence_refs. A content_material overrides it.
- remembered_facts are things already known about this customer. Same rule as
  knowledge_chunks: write your own words and cite the entry's ref in
  source_evidence_refs when a sentence rests on one. Never invent a memory ref.
- Every factual claim needs source evidence references. If you have no source,
  do not state the fact.
- Do not promise a future action unless it is already registered as a
  commitment.
- If you say you are about to explain or send something, the thing itself goes
  out in this same turn: put it in the **next segment**.
  It is a separate message the customer reads after this one, not the same
  bubble. An unkept
  promise is deleted before the customer reads it, so saying it and not doing
  it costs you the sentence.
- Obey every generation constraint given below.
- What the customer actually receives is
  proposed_intents[].typed_payload.content, not customer_visible_segments. The
  segments list is a draft for review; the payload is what gets queued and sent.
  Put the message in both, and make them say the same thing.
- **Never open by confirming that you received or noted something.** "收到"，
  "我记下了"，"我听到了"，"我知道了"，"我来安排" -- a receipt is not an
  answer. The customer knows you received it; he is waiting to hear what you
  make of it. **Understanding is shown by using what he said, not by saying you
  understood it**: instead of "晚上没人回消息，这个我记下了"，write the
  sentence that does something with it.
- **Do not restate his words back to him**, and do not tell him a topic will be
  covered later. If it is worth saying, say it now; if it is not, leave it out.
  "这条先不展开，回头专门说" is a sentence that costs a bubble and delivers
  nothing -- and in sales, a question asked is a question to answer now.
- **There is no character limit.** Say what actually answers him. What there
  is instead: **break a long answer into segments, and each segment becomes its
  own message** -- each segment is its own message, and the platform sends
  them in order, waiting for each to arrive
  before the next, with a pause in between. One segment is one bubble, so break
  where a person would stop and start a new line: one point per bubble. The
  merchant's own approved wording runs 17 to 115 characters, median 45, and the
  customer writes about 10 -- that is the size a bubble usually is, not a rule.
- A material_ref segment is its own bubble too. Quote the merchant's wording as
  one message, then say your own sentence as the next.
- `rememberable_fields` is what this merchant lets you remember about the
  customer, one line each saying what it holds. To remember something, propose
  a `remember` intent whose payload is exactly
  `{"field_key": "<one of those keys>", "field_value": "<what he said>"}` --
  both non-empty strings, the key at most 128 characters and the value at most
  1024, **counted in characters**. Use a key from that list and no other; an
  invented key is dropped and the fact is lost. Remember only what the
  customer said himself, in this turn.
- A send_message payload's content uses the same segment shape:
  {"segments": [{"type": ..., "content_or_ref": ...}, ...]}.
- content_materials is the merchant's own approved wording for this goal. To
  use one, emit {"type": "material_ref", "content_or_ref": "<its ref>"} and the
  platform inserts the merchant's exact text in its place. Quote by reference or
  leave it out: do not copy, shorten, translate, merge or reword a material into
  a text segment. Text segments are for your own words around a quoted one.
- A material quoted in the draft must also be quoted in the payload. Quoting it
  in the draft and writing your own version of it in the payload is refused, and
  the customer then receives nothing at all.
- Only a ref that appears in content_materials may be used. A ref that does not
  appear there is refused and the customer receives nothing at all, so never
  guess one and never invent a plausible-looking ref.
- An empty content_materials means this goal has no approved wording; write your
  own text as before.
- must_quote_material, when the generation constraints list it, means this
  turn's payload has to quote at least one offered material. Without that
  constraint you may answer in your own words when no material fits.
- content_assets are files, not wording. To send one, propose a separate
  send_material intent whose typed_payload is {"ref": "<its ref>"}; its ref
  never goes in a segment, and the file is not described in your text.
- materials_already_sent lists what this conversation has actually been about,
  in the order it was covered. On a turn the customer did not start, pick the
  follow-up up from one of these -- "the onboarding steps we went through", "the
  price" -- and never write a vague pointer such as "the thing we discussed":
  the customer cannot tell from it that you remember anything.
- last_outbound is the literal sentence you sent last. Use it for tone and to
  avoid repeating yourself, **not** as the thing to follow up on: a conversation
  often ends on "ok, I'll take a look", which is not what it was about.
- pending_question, when it is not null and its answer_evidence is null, is a
  question we already sent (question_text) that the customer has not answered.
  Never write it again, in these words or others -- he heard it. When he
  answered it this turn, use the answer; asking a question he just answered
  reads as not listening.

Use these exact JSON field names and nested shapes. Do not add properties. Example
JSON output that quotes one material and adds a sentence of its own:
{
  "strategy_id": "advance_goal",
  "customer_visible_segments": [
    {"type": "material_ref", "content_or_ref": "material:example-intro:v1"},
    {"type": "text", "content_or_ref": "您那边现在是谁在接客户消息？"}
  ],
  "proposed_intents": [
    {"kind": "send_message",
     "typed_payload": {"content": {"segments": [
       {"type": "material_ref", "content_or_ref": "material:example-intro:v1"},
       {"type": "text", "content_or_ref": "您那边现在是谁在接客户消息？"}
     ]}},
     "evidence_refs": ["event:00000000-0000-4000-8000-000000000000"]}
  ],
  "confidence": {"raw_score": 0.8, "calibrated_score": null,
                 "assessment_version": "model-v1", "factor_codes": []},
  "claims": [],
  "return_confirmation": {"included": false, "tone_material_ref": null},
  "safety_markers": ["contains_material_ref"]
}
Note that the payload repeats the same two segments: that repetition is the
message the customer receives. And one with no material to quote -- note that
even here it answers something, because a receipt is not an answer:
{
  "strategy_id": "acknowledge",
  "customer_visible_segments": [
    {"type": "text", "content_or_ref": "那按人头算，12 家店 2 个人盯，压力在晚上。"}
  ],
  "proposed_intents": [
    {"kind": "send_message",
     "typed_payload": {"content": {"segments": [
       {"type": "text", "content_or_ref": "那按人头算，12 家店 2 个人盯，压力在晚上。"}
     ]}},
     "evidence_refs": ["event:00000000-0000-4000-8000-000000000000"]}
  ],
  "confidence": {"raw_score": 0.8, "calibrated_score": null,
                 "assessment_version": "model-v1", "factor_codes": []},
  "claims": [],
  "return_confirmation": {"included": false, "tone_material_ref": null},
  "safety_markers": ["none"]
}
Every customer_visible_segments item contains exactly type and content_or_ref;
it is never a string and never uses a content field. confidence.factor_codes may
only contain: evidence_complete, evidence_missing, slot_conflict, low_signal_input,
ambiguous_intent, policy_constrained, deterministic_output, fallback_applied.
Use [] rather than inventing a code."""


def _bounded_understanding(document: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """进提示词的那一份 understanding，过一次字节上界（ADR-0132 / T282）。

    **只作用于提示词那一份副本。** pipeline 手上的原文档照旧拿去写槽位、
    落库、判 correction —— 在这里丢掉一条 `slot_updates` 只是模型在提示词里
    看不到它，**不是那个槽位没有被写**。两者混在一起就会变成
    「为了省字节而悄悄改了业务状态」。

    **plan 与 generate 各调一次，而这不是浪费**：两个请求各自独立地比
    `MAX_MODEL_INPUT_BYTES`，而同一份文档在两处都要付一遍。
    """

    # ⚠️ **`turn_summary` 不在这一份里，而摘它的不是这里** ——
    # `understanding_bound._NOT_IN_PROMPT` 摘的，理由写在它旁边。
    # 第一版摘在这一行上面，那让 `bound_for_prompt` 对一份合同内的文档
    # 不再成立、只是恰好没人那样调它。
    bounded = bound_for_prompt(document)
    for field, count in bounded.dropped.items():
        UNDERSTANDING_ENTRIES_DROPPED_TOTAL.increment(stage, field, amount=count)
    return bounded.document


#: 一条拒绝消息里最多列几条违例。**超出的不静默丢掉，它会说还有几条。**
#:
#: 这个数不是随便定的：这条消息会进审计的 `detail`，而 `detail` 在别处被截到
#: 500 字符。六条 `describe_validation_error` 的输出（每条约 60–80 字符）
#: 加上前缀就在那个量级 —— **再多，留在消息里的会是被截断的半条**。
#: `AGENTS.md`：有上限就把丢掉的数说出来，别让截断看起来像「就这些」。
_MAX_REPORTED_VIOLATIONS = 6


def _violations_for_operator(violations: list[str]) -> str:
    """把违例清单拼成一行，**并且说清有没有没列出来的**。

    空清单也要有话说：`iter_errors` 回空而 `validate()` 刚抛过，
    说明两者看到的不是同一份文档（schema 加载出错、文档被就地改了）——
    那种情况下写「没有违例」比写「未知」更糟，因为前者读起来像它通过了。
    """

    if not violations:
        return (
            "the validator raised but re-validating found no violation "
            "-- the schema or the document changed between the two passes"
        )
    shown = violations[:_MAX_REPORTED_VIOLATIONS]
    line = "; ".join(shown)
    if len(violations) > len(shown):
        line += f"; (+{len(violations) - len(shown)} more, {len(violations)} in total)"
    return line


class ModelBackedInference:
    """`InferencePort` backed by a real chat provider."""

    def __init__(
        self,
        *,
        release: ModelPolicyRelease,
        client: OpenAICompatibleChatClient,
        prompt_release: str = PROMPT_RELEASE,
        governor: CostGovernor | None = None,
    ) -> None:
        self._release = release
        self._client = client
        self._prompt_release = prompt_release
        self._governor = governor
        self.version = f"{release.version}:{prompt_release}"

    @property
    def release(self) -> ModelPolicyRelease:
        return self._release

    def _call(
        self,
        *,
        role: str,
        system_prompt: str,
        user_prompt: str,
        context: CallContext | None,
    ) -> tuple[dict[str, Any], RoleBinding, ChatCompletion]:
        binding = self._release.role(role)
        completion = self._client.complete(
            binding=binding,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            governor=self._governor,
            context=context,
        )
        return completion.content, binding, completion

    def understand(
        self,
        *,
        envelope: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        #: 上一轮我们对这个客户说的那句话。**理解步与计划步收同一份** ——
        #: 单看几个字读不出意思（运维 2026-09-17 实测那一轮的沉默根因）。
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这一轮带给模型的记忆，**以及装不下的有几条**。
        #:
        #: **管线算一次，两步收同一份**（与 `turn_materials` 同一条纪律）：
        #: 两处各算一遍会让理解步与计划步看见不同的记忆，
        #: 而那种不一致只在真实回合里显形。
        #: 管线也据此留痕 —— 「那条回复看见过哪几条记忆」必须答得出来。
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: 这通对话已经真的发出去过的话术 ref，按发出顺序（平台从
        #: `intent_records` 现读，**不是模型自己报的**）。
        materials_already_sent: Sequence[str] = (),
    ) -> dict[str, Any]:
        frame = aggregate["target_stack"][-1]
        # Platform frames resolve here too since B4-7; a scan of
        # `profile["target_frames"]` would raise `StopIteration` on one.
        definition = frame_definitions(profile)[str(frame["definition_id"])]
        user_prompt = json.dumps(
            # **形状的唯一定义点是 `runtime.request_shape`** —— admission 算固定底时
            # 调的是同一个函数。先前两处各拼各的，差出来的那段骨架**被数在零处**
            # （2026-09-15 实测，整段留痕在那个模块的 docstring）。
            #
            # `slots_wanted` 是槽位词表：没有它模型只能编名字，而
            # `apply_slot_updates` 会把帧没声明的名字整条丢掉——于是 `slot_updates`
            # 在实践上不可填，早于那两个 uuid 让它在原理上不可填（T34）。
            request_shape.understand_prompt(
                goal=frame["goal"],
                information_slots=definition["information_slots"],
                # **话题槽的合法取值由素材现推，告诉模型的与代码执行的同源**
                # （2026-09-15「裁 B」）。先前这份名单只活在 goal 的散文里，
                # 而素材声明里另有一份 —— 两份就会漂，漂了也没有东西会红。
                allowed_slot_values=allowed_slot_values(definition),
                event_type=envelope["event_type"],
                content=envelope.get("content") or {},
                known_slots=frame["slots"],
                last_outbound=last_outbound,
                conversation_summary=conversation_summary,
                summary_lines_not_shown=summary_lines_not_shown,
                # **记住的那几件事**（2026-09-18）。带哪几条按 `memory_fields`
                # 的声明顺序算 —— 平台猜不出哪一条对这个行业更重要，
                # 而商家把它写在了声明的顺序里。
                # **装不下的数出来**：一次安静的截断读起来与
                # 「这个客户没有记忆」一模一样。
                known_memories=[dict(entry) for entry in known_memories],
                memories_not_shown=memories_not_shown,
                # **已经讲过哪几段**（2026-09-18）。平台现读那一份，
                # 两步收同一份 —— 模型在此之前一次都没见过它。
                materials_already_sent=list(materials_already_sent),
            ),
            ensure_ascii=False,
        )
        document, binding, completion = self._call(
            role="UNDERSTAND_VALUE",
            system_prompt=_UNDERSTAND_SYSTEM,
            user_prompt=user_prompt,
            context=context,
        )
        document["schema_version"] = "1.0.0"
        document["event_id"] = str(envelope["event_id"])
        document["model_evidence"] = model_evidence(
            binding=binding, completion=completion, prompt_release=self._prompt_release
        )
        # The two identity fields of every slot update, supplied here rather
        # than asked of the model -- the same rule as `event_id` above. The
        # frame is the one the model was just asked about; the evidence event is
        # the envelope being processed. Asking the model for two uuids it has
        # never seen is what made `slot_updates` unanswerable under the strict
        # response schema, so it answered `[]` every time (T34).
        #
        # **This is also why `evidence_event_id` is trustworthy while
        # `evidence_span_hash` is not** (T41): one is measured here, the other
        # is still the model's own claim with nothing checking it.
        for update in document.get("slot_updates", []):
            update["frame_id"] = str(frame["id"])
            update["evidence_event_id"] = str(envelope["event_id"])
        return self._validated("understanding-result", document, role=binding.role)

    def read_silence(
        self,
        *,
        envelope: dict[str, Any],  # noqa: ARG002 - 它的 event_id 不进这份文档，见下
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        materials_already_sent: Sequence[str] = (),
        silent_for_seconds: float = 0.0,
        unanswered_rounds: int = 0,
        offered_materials: Mapping[str, str] = MappingProxyType({}),
    ) -> dict[str, Any]:
        """第四步（ADR-0149）：**上下文与理解步同一份，问的问题不同。**

        ⚠️ **这份文档没有 `event_id`**，而另外三份有。不是漏了：那三份是
        「对某一条入站事件的产物」，而这一步的对象是**一段没有事件的沉默**。
        定时器信封确实有 id，但把它盖上去会让这份文档读起来像是在说那个定时器 ——
        它说的是这通对话此刻的状态。契约的 `required` 里因此也没有它。
        """

        frame = aggregate["target_stack"][-1]
        definition = frame_definitions(profile)[str(frame["definition_id"])]
        user_prompt = json.dumps(
            request_shape.silence_prompt(
                goal=str(definition.get("goal") or ""),
                information_slots=list(definition.get("information_slots") or []),
                allowed_slot_values=allowed_slot_values(definition),
                event_type=str(envelope.get("event_type") or ""),
                content=envelope.get("content") or {},
                known_slots=frame.get("slots") or {},
                last_outbound=last_outbound,
                conversation_summary=list(conversation_summary),
                summary_lines_not_shown=summary_lines_not_shown,
                known_memories=[dict(entry) for entry in known_memories],
                memories_not_shown=memories_not_shown,
                materials_already_sent=list(materials_already_sent),
                silent_for_seconds=silent_for_seconds,
                unanswered_rounds=unanswered_rounds,
                offered_materials=dict(offered_materials),
            ),
            ensure_ascii=False,
        )
        document, binding, completion = self._call(
            role="SILENCE_CAPABILITY",
            system_prompt=_SILENCE_SYSTEM,
            user_prompt=user_prompt,
            context=context,
        )
        document["schema_version"] = "1.0.0"
        document["model_evidence"] = model_evidence(
            binding=binding, completion=completion, prompt_release=self._prompt_release
        )
        return self._validated("silence-reading-result", document, role=binding.role)

    def plan(
        self,
        *,
        understanding: dict[str, Any],
        aggregate: dict[str, Any],
        profile: dict[str, Any],
        context: CallContext | None = None,
        required_strategy: str | None = None,
        assets: Sequence[Mapping[str, str]] = (),
        #: 上一轮我们对这个客户说的那句话。**理解步与计划步收同一份** ——
        #: 单看几个字读不出意思（运维 2026-09-17 实测那一轮的沉默根因）。
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这一轮带给模型的记忆，**以及装不下的有几条**。
        #:
        #: **管线算一次，两步收同一份**（与 `turn_materials` 同一条纪律）：
        #: 两处各算一遍会让理解步与计划步看见不同的记忆，
        #: 而那种不一致只在真实回合里显形。
        #: 管线也据此留痕 —— 「那条回复看见过哪几条记忆」必须答得出来。
        known_memories: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: 这通对话已经真的发出去过的话术 ref，按发出顺序（平台从
        #: `intent_records` 现读，**不是模型自己报的**）。
        materials_already_sent: Sequence[str] = (),
        #: **上一次尝试许了而这一轮没兑现的那几句**（T367）。
        #: 第一次尝试恒为空；只有重跑那一次非空 ——
        #: 它在的时候，这一步要做的事是**把兑现那一条列出来**，
        #: 而生成步早就被告知「许了就放进计划列的下一条」（批 G）。
        unkept_promises: Sequence[str] = (),
        #: 这一轮那次静默怎么读的（ADR-0149）。只有主动轮次非空。
        silence_reading: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        frame = aggregate["target_stack"][-1]
        # Platform frames resolve here too since B4-7; a scan of
        # `profile["target_frames"]` would raise `StopIteration` on one.
        definition = frame_definitions(profile)[str(frame["definition_id"])]
        allowed = [
            strategy
            for strategy in definition["allowed_strategy_set"]
            if strategy in STRATEGY_CATALOG
        ]
        # **A timer turn is offered one strategy, not a menu** (B4-4d,
        # ADR-0105 §零.1: 「打法固定在模板里，不让模型和商家自由发挥」). Narrowing
        # the prompt rather than only rejecting the answer afterwards is what
        # makes the rule an instruction instead of a trap -- the validator
        # refuses any other choice, so a model left free to pick would send every
        # timer turn to the safe fallback.
        #
        # **Narrowed, never widened**: a `required_strategy` outside the frame's
        # allowlist is ignored here and refused downstream, because the frame
        # allowlist is the profile's other statement about the same thing and
        # this must not become a way around it.
        #
        # **`STRATEGY_CATALOG` is re-checked** rather than inherited from the
        # line above (independent review 2026-09-09, P3-2): the assignment
        # replaces `allowed` wholesale, so without it a rule naming a strategy
        # the platform does not define would reach the prompt as the only
        # option. `validate_plan` refuses it as `STRATEGY_NOT_IN_CATALOG`, so
        # the cost is one wasted model call rather than a bad send -- but the
        # filter a line above exists precisely so that call is not made.
        if (
            required_strategy is not None
            and required_strategy in STRATEGY_CATALOG
            and required_strategy in time_rule_strategy_set(definition)
        ):
            allowed = [required_strategy]
        user_prompt = json.dumps(
            # **形状的唯一定义点是 `runtime.request_shape`**（见 understand 那一处的
            # 同一段注释）。下面这几条说的是**为什么这些数据在提示词里**，
            # 那是决策，不是形状，所以留在调用点。
            #
            # **`allowed_generation_constraints_by_strategy`**（2026-09-10）：
            # `validate_plan` 会拒掉配包没声明的约束名，而在那一批之前提示词
            # **从来没说过合法值是什么** —— 契约里要求这个字段、示例写着 `[]`、
            # 没有任何地方点过名。一个被要求填而没有词表的字段，模型会**编一个**：
            # 第二个真实入站窗口实测**七个事件、六份计划被拒
            # （`GENERATION_CONSTRAINT_NOT_DECLARED`）、零条消息发出**，
            # 而编出来的名字是声明值的**近似**（`ask_one_question` 对 `one_question`）
            # ——「那是猜的样子，不是不同意的样子」。
            #
            # **`required_slots_by_strategy`**（裁定一步骤 3，ADR-0137）：
            # `STRATEGY_SKIPS_REQUIRED_SLOT` 那道闸判的是「这个策略声明的必填槽位
            # 空着，而计划既不填也不问」——而在那一行之前，**模型从来没有被告知过
            # 每个策略声明了哪些必填槽位**。`current_frame.required_slots` 给的是
            # 帧的 `information_slots[].required`，是**另一个**数据源（只在
            # `op == "complete"` 那道闸上被读）。少了它，上面那次事故会复制一遍，
            # 而这一次的代价是**每一轮客户都收不到东西**（拒绝路径不产出 outbox 行）。
            #
            # **`pending_question`**（RFC §3.2，B1-2）：`answer_evidence is null`
            # 就是「问了、还没答」——模型据此不必把同一个槽位再问一遍。
            # **放在 `user_prompt` 而不是 system prompt，两个理由**：(1) 它每回合都不同，
            # 而 system prompt 描述的是这个角色**恒定**的规则；(2) system prompt 是
            # prompt caching 的稳定前缀，把逐轮变化的数据塞进去会让它每轮失效。
            #
            # **`focus`**（RFC §3.2，B1-1）：今天只有 `contact` 一类 ——
            # 另三类的解除条件与那次收口写在 `pipeline._focus_projection` 的 docstring
            # 与聚合契约里（`focus` 是闭合键集，加一类要改 schema）。
            request_shape.plan_prompt(
                # **与生成步收到的是同一份清单**（`pipeline` 只选一次）——
                # 两处各选一次会让计划提的 ref 在生成那一刻可能已经不在清单里，
                # 而那种不一致只在真实回合里显形。
                content_assets=[dict(asset) for asset in assets],
                last_outbound=last_outbound,
                conversation_summary=conversation_summary,
                summary_lines_not_shown=summary_lines_not_shown,
                # **记住的那几件事**（2026-09-18）。带哪几条按 `memory_fields`
                # 的声明顺序算 —— 平台猜不出哪一条对这个行业更重要，
                # 而商家把它写在了声明的顺序里。
                # **装不下的数出来**：一次安静的截断读起来与
                # 「这个客户没有记忆」一模一样。
                known_memories=[dict(entry) for entry in known_memories],
                memories_not_shown=memories_not_shown,
                # **已经讲过哪几段**（2026-09-18）。平台现读那一份，
                # 两步收同一份 —— 模型在此之前一次都没见过它。
                materials_already_sent=list(materials_already_sent),
                goal=frame["goal"],
                exit_conditions=definition["exit_conditions"],
                information_slots=definition["information_slots"],
                allowed_strategies=allowed,
                strategies=profile["strategies"],
                # `emit_kind` since B4-1 tightened `time_rules` to RFC 6.2.
                event_kinds=[rule["emit_kind"] for rule in definition["time_rules"]],
                understanding=_bounded_understanding(understanding, stage="plan"),
                status=frame["status"],
                slots=frame["slots"],
                stack_depth=len(aggregate["target_stack"]),
                # **进过哪几个帧**（2026-09-21）——「报价那一帧去过没有」
                # 在此之前计划步答不出来，而 `once` 的帧再 push 会被拒。
                frames_entered=[
                    str(frame["definition_id"]) for frame in aggregate["target_stack"]
                ],
                pending_question=aggregate.get("pending_question"),
                focus=aggregate.get("focus") or {},
                # **重跑那一次才非空**（T367）：上一次许了什么而没兑现。
                # ⚠️ **不塞进 `focus`** —— 那是用户 2026-09-15 裁定的**闭合键集**
                # （`conversation-aggregate` schema 的描述逐字写着
                # 「有人加第三类键不会红」），所以它是一个独立的键。
                unkept_promises=unkept_promises,
                silence_reading=silence_reading,
            ),
            ensure_ascii=False,
        )
        document, binding, completion = self._call(
            role="PLAN_CAPABILITY",
            system_prompt=_PLAN_SYSTEM,
            user_prompt=user_prompt,
            context=context,
        )
        document["schema_version"] = "1.0.0"
        document["origin"] = "model"
        document["model_evidence"] = model_evidence(
            binding=binding, completion=completion, prompt_release=self._prompt_release
        )
        return self._validated("plan-result", document, role=binding.role)

    def generate(
        self,
        *,
        plan: dict[str, Any],
        understanding: dict[str, Any],
        envelope: dict[str, Any],
        materials: Mapping[str, str],
        knowledge: Mapping[str, str] = MappingProxyType({}),
        assets: Sequence[Mapping[str, str]] = (),
        context: CallContext | None = None,
        #: 上一轮我们对这个客户说的那句话。**三步至此收同一份。**
        last_outbound: str = "",
        conversation_summary: Sequence[str] = (),
        summary_lines_not_shown: int = 0,
        #: 这通对话已经讲过哪几段（平台现读）。**跟进那一句的锚点是它** ——
        #: 用户 2026-09-18：「上一句不一定是关键，之前的内容才是」。
        materials_already_sent: Sequence[str] = (),
        #: 这个联系人已经记住的那几件事（T363）。**形状是 `{ref, value}`** ——
        #: 写字这一步要引它，所以它要一个可引的 ref。
        #: 由管线从 `memories_for_prompt` 的同一份结果转出来。
        remembered_facts: Sequence[Mapping[str, str]] = (),
        memories_not_shown: int = 0,
        #: **这个商家允许记哪几件事**，由 pipeline 从配包取好交过来。
        #:
        #: ⚠️ **不在这里从 `profile` 现取，而这不是风格问题**：上面那段注释
        #: 记着 `profile` 当初为什么被从这个签名里拿掉 ——
        #: **「Handed the selection rather than making it」**。
        #: 在这里再取一遍，就是同一个答案算两处。
        rememberable_fields: Sequence[Mapping[str, Any]] = (),
        #: **挂着没答的那个问题**（运维缺陷 19）。计划步早就收着，
        #: 而把问题写进正文的是这一步 —— 只教计划步拦不住写字的手。
        pending_question: Any = None,
    ) -> dict[str, Any]:
        # **`profile` was in this signature and nowhere in this body.** The step
        # that writes every customer-visible sentence was handed the strategy
        # name, a reason code, constraint names, candidate intents, pending
        # slots, the understanding and the event type -- and not one word of the
        # merchant's. So「素材只能由 profile 声明、模型不得现场编造」described
        # nothing: there was no material for the model to not-invent from.
        #
        # **Handed the selection rather than making it.** `materials` arrives
        # already narrowed by `material_selection.select_materials`, which the
        # pipeline calls once for the turn and gives to the validator as well.
        # Deriving it here too would be two computations of one answer, and the
        # validator's copy is what decides whether a quote is legal -- a turn
        # where the two disagreed would offer the model a material and then
        # refuse the message that used it.
        user_prompt = json.dumps(
            # **形状的唯一定义点是 `runtime.request_shape`**（见 understand 那一处）。
            #
            # **Ref and text together, and the ref is what the model is asked to
            # emit.** Sending the text alone leaves it nothing to point at, and
            # "quote this exactly" enforced by asking nicely is not enforcement:
            # a `material_ref` segment is resolved from this same mapping on the
            # send path, so a quoted material reaches the customer as the
            # merchant wrote it whatever the model does with the words around it.
            #
            # **知识库与素材是两种东西，所以是两个键**（裁定四 / ADR-0135）：
            # 素材是商家逐字审过的话术，**按 ref 引**，平台在发送路径上把 ref 换成
            # 那段字；知识库是整理出来的资料，**模型用自己的话写**，把 chunk 的 ref
            # 放进 `claims[].source_evidence_refs`。混成一个键会让「哪一边可以逐字引」
            # 这件事回到靠提示词措辞维持。
            request_shape.generate_prompt(
                strategy_id=plan["selected_strategy_id"],
                strategy_reason_code=plan["strategy_reason_code"],
                generation_constraints=plan["generation_constraints"],
                candidate_intents=plan["candidate_intents"],
                required_slot_requests=plan["required_slot_requests"],
                understanding=_bounded_understanding(understanding, stage="generate"),
                event_type=envelope["event_type"],
                content_materials=[
                    {"ref": ref, "text": text} for ref, text in materials.items()
                ],
                knowledge_chunks=[
                    {"ref": ref, "text": text} for ref, text in knowledge.items()
                ],
                # **只有 ref 与标题，没有内容** —— 文件的内容不是字，
                # 而把文件名喂给模型让它描述，等于让它替一份没读过的文件作担保。
                content_assets=[dict(asset) for asset in assets],
                # **写跟进那句话的那一步，第一次看得见上一轮说了什么**
                # （2026-09-18，运维 §11）。
                last_outbound=last_outbound,
                conversation_summary=conversation_summary,
                summary_lines_not_shown=summary_lines_not_shown,
                # **跟进那一句的锚点是这个，不是上一句**（用户 2026-09-18）。
                materials_already_sent=list(materials_already_sent),
                # **记住的那几件事**（T363）：写字这一步在此之前看不见它们，
                # 于是「那条回复引了哪几条记忆」在结构上答不出来 ——
                # 引用集要有人去引。
                remembered_facts=[dict(entry) for entry in remembered_facts],
                rememberable_fields=rememberable_fields,
                memories_not_shown=memories_not_shown,
                pending_question=pending_question,
            ),
            ensure_ascii=False,
        )
        document, binding, completion = self._call(
            role="GENERATE_VALUE",
            system_prompt=_GENERATE_SYSTEM,
            user_prompt=user_prompt,
            context=context,
        )
        document["schema_version"] = "1.0.0"
        document["origin"] = "model"
        document["strategy_id"] = plan["selected_strategy_id"]
        document["model_evidence"] = model_evidence(
            binding=binding, completion=completion, prompt_release=self._prompt_release
        )
        return self._validated("generation-result", document, role=binding.role)

    def _validated(self, contract: str, document: dict[str, Any], *, role: str) -> dict[str, Any]:
        try:
            validate_inference_contract(contract, document)
        except ValidationError as error:
            # **The document is still not attached** -- it is model output about a
            # customer conversation. What is attached is which *rules* it broke,
            # rendered by `describe_validation_error`'s positive whitelist
            # (validator name, the constraint out of our own schema, the path,
            # and a shape summary with no characters of the value in it).
            #
            # > The comment that stood here read: **"The contract name and role
            # > are enough to triage."** 2026-09-22 is its counter-example. Same
            # > input, understand ran four times and failed twice, and the whole
            # > of the audit trail was `UNDERSTAND_VALUE output does not satisfy
            # > the understanding-result contract`. Ops spent over an hour and
            # > still could not narrow it to a rule. The path and the validator
            # > were sitting in `error` the whole time -- `from error` even kept
            # > them attached -- and nothing wrote them down.
            #
            # `inference_contract_violations` re-validates to list **all** of
            # them, not just the one `validate()` raises: `AGENTS.md` names
            # jsonschema's first violation as a member of the "reports the first,
            # not all" family, and "fix one and run it again" is not a triage
            # loop to hand someone chasing an intermittent provider.
            violations = inference_contract_violations(contract, document)
            raise ProviderMalformedOutput(
                f"{role} output does not satisfy the {contract} contract: "
                f"{_violations_for_operator(violations)}",
                role=role,
            ) from error
        except Exception as error:
            # **Not a contract violation**: a missing schema file, a bad schema,
            # an OS error. Saying "the model sent something invalid" for those
            # sends the reader to debug the wrong artefact -- the shape
            # `AGENTS.md` calls "a correct message pointing at the wrong cause".
            raise ProviderMalformedOutput(
                f"{role} output could not be checked against the {contract} contract "
                f"({type(error).__name__})",
                role=role,
            ) from error
        return document
