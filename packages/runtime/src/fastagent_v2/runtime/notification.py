"""Text a **person at the merchant** is meant to read.

This module exists because `redaction.py` cannot do this job, and the reason is
not implementation detail -- the two have **opposite failure directions**:

* `redact()` protects logs. When unsure it removes, because an unreadable log
  line costs an engineer five minutes and a leaked credential costs a customer.
  It drops any value whose *key* smells of a secret, which is why token counts
  once vanished from cost records (T22).
* `notify_text()` protects a message to the merchant. When unsure about
  **readability** it keeps, because a notice nobody can read is a notice nobody
  acts on -- and the whole point of the manual-task notification is that a human
  acts on it.

Those two rules cannot live in one function, so they live in two, and neither
wraps the other. **Do not "unify" them.** A rule tightened for logs would start
sending merchants text they cannot read; a rule loosened for merchants would
start putting secrets in logs. `test_the_two_vocabularies_disagree_on_purpose`
fails if someone tries.

## What the merchant is allowed to see

Customer conversation content, **verbatim**. The merchant owns the relationship;
withholding what their own customer said would make the notice useless and is
not a privacy boundary this system is entitled to draw (user's ruling,
2026-09-04). What is removed is what belongs to *us*, not to them:

1. **Credentials** -- fail closed. Anything shaped like a key, token, DSN or
   private-key block is replaced, even at the cost of eating a word.
2. **Internal identifiers** -- a UUID becomes a short reference a person can say
   out loud. The full id stays in the audit trail, where it belongs.
3. **Internal status codes** -- translated through a table, never shown raw. A
   merchant reading `ROOT_TARGET_FRAME_ABANDONED` learns nothing and may search
   for it as if it were an error number; an unknown code gets a generic sentence
   and the code itself is dropped, so this path cannot become a way to read the
   platform's internal vocabulary out of a notification.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: What replaces something credential-shaped. Deliberately in Chinese and
#: deliberately not empty: a merchant seeing a gap wonders what was cut, and a
#: merchant seeing this knows nothing was hidden *from them* -- it was something
#: of ours that has no business in a message.
CREDENTIAL_PLACEHOLDER = "【凭据已隐去】"

#: Shapes that are credentials wherever they appear. Order matters only for
#: readability of the result; every pattern is applied.
#:
#: `redaction.py` matches on **key names** in structures. This matches on
#: **value shapes in free text**, because that is what a notification is. The
#: overlap is intentional and the two lists are maintained separately; T39
#: registers that the log redactor's free-text coverage is weaker than this one,
#: which is exactly why this file does not delegate to it.
#: **Not `\b`.** Python's `\w` counts CJK characters as word characters, so
#: there is no word boundary between 汉字 and an adjacent token -- and "汉字
#: immediately followed by a token" is the *normal* shape of a merchant-facing
#: message, not an edge case. Every pattern here used to start with `\b`, and
#: the fixtures all happened to put a space or a full-width colon in front of
#: the token, so the whole vocabulary passed its tests while leaking on the
#: sentences it exists for: `我的key是sk-…`, `会话3f2a…`, and the 64-hex
#: WeComProxy path token this system has actually leaked once (T31, independent
#: review 2026-09-04, F2).
#:
#: The replacement asserts explicitly that no token character sits on either
#: side, which is what `\b` was meant to express here.
_LEFT_EDGE = r"(?<![A-Za-z0-9_\-])"
_RIGHT_EDGE = r"(?![A-Za-z0-9_\-])"
_CREDENTIAL_PATTERNS = (
    re.compile(_LEFT_EDGE + r"(?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}" + _RIGHT_EDGE),
    re.compile(r"(?i)(?<![A-Za-z0-9_\-])bearer\s+[A-Za-z0-9._\-]{16,}" + _RIGHT_EDGE),
    re.compile(_LEFT_EDGE + r"AKID[A-Za-z0-9]{16,}" + _RIGHT_EDGE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(?<![A-Za-z0-9_\-])[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@]+:[^\s/@]+@[^\s]+"),
    #: The WeComProxy callback path token: 64 hex characters in a URL path. Hex
    #: on both sides rather than the general token class, so a longer hex string
    #: is not chewed in half and reported as "redacted plus leftovers".
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
)

_UUID = re.compile(
    r"(?<![0-9a-fA-F-])"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F-])"
)

#: Internal reason codes, translated. **The key set is the contract**: a code
#: that reaches a notification without an entry here gets `UNKNOWN_SENTENCE`,
#: and its raw form is dropped rather than shown.
#:
#: Written as full sentences, not fragments, because they are read alone by
#: someone who was not watching the conversation.
REASON_SENTENCES = {
    "ROOT_TARGET_FRAME_ABANDONED": "客户表示不需要继续，这通对话已交回人工。",
    "ROOT_TARGET_FRAME_COMPLETED": "这通对话的目标已经完成，交回人工确认收尾。",
    "TARGET_STACK_EMPTY": "这通对话没有可继续的目标，已交回人工。",
    # **Lower case, because that is what the producers emit.** `cost.py` writes
    # `cost_hard_ceiling`; the first version of this table wrote
    # `COST_HARD_CEILING`, so the two kinds the ruling pushes *immediately* got
    # the generic fallback sentence while the sentence written for them sat here
    # unreachable (independent review 2026-09-04, F1). The classification table
    # had it right, which is why the routing worked and only the words were
    # wrong -- the failure mode that reaches a merchant and nothing else.
    "cost_hard_ceiling": "这通对话已达当日费用上限，自动回复已停止，交回人工。",
    "cost_estimated_hard_ceiling": "这通对话的预估费用已达上限，自动回复已停止，交回人工。",
    "cost_unsettled_backlog": "有多笔调用未能结算，自动回复已暂停，交回人工。",
    "DELIVERY_UNCONFIRMED": "有一条消息无法确认是否送达，需要人工核对。",
    # **AI 当着客户的面应下了一件它自己做不到的事**（用户裁定 2026-09-11）。
    # 生成步可以提名 `manual_task` / `handoff`，而发送路径今天只认 `send_message`，
    # 于是那条提案被拒、**什么都不发生**——客户听见「我把您标记一下」，
    # 同事那边没有任何东西。这一句是那条待办的固定模板；模型自己那句话
    # 由 `manual_task_notice` 的 `promised_text` 附在后面（同一条裁定的另一半：
    # **固定模板 + 附模型原话，不是二选一**）。
    "INTENT_KIND_NEEDS_A_PERSON": "AI 在这通对话里应下了一件它自己做不到的事，需要人工接手。",
    # **T365（2026-09-19）。** 与上一句是两个触发点、同一件事的两半：
    # 上一句是「模型提名了一个平台发不出去的 `kind`」，
    # 这一句是「模型**只是说了**『报价单这边我来安排』，一个 kind 都没提名」。
    #
    # 那句预告仍然被丢掉（AI 兑现不了它），**而请求不再消失** ——
    # 客户的原始请求要有人接着办，所以这条句子说的是**那件事**，
    # 不是「平台丢了一句话」：同事读到的必须是可以动手的东西。
    "AI_PROMISED_A_HUMAN_ACTION": "AI 答应了客户一件需要人去做的事，需要人工接手。",
}

UNKNOWN_SENTENCE = "这通对话需要人工处理。"


#: **Who a manual task is for.** Ruling (user, 2026-09-04): every task is one of
#: these two, and **only `MERCHANT_ACTION` may ever reach the manager's WeCom**.
#: The field exists now, before the scope is settled, precisely so that changing
#: the scope later is an edit to the table below rather than a change to the
#: mechanism.
MERCHANT_ACTION = "merchant_action"
PLATFORM_INTERNAL = "platform_internal"

#: **How often.** `NO_PUSH` is not "not classified" -- it is "merchant-facing in
#: principle, deliberately not pushed in this batch", which is a different thing
#: from platform-internal and has to stay different: the first is one table edit
#: away from being pushed, the second is a decision that it never should be.
IMMEDIATE = "immediate"
BATCHED = "batched"
NO_PUSH = "none"


@dataclass(frozen=True, slots=True)
class Classification:
    audience_class: str
    cadence: str

    @property
    def pushes_now(self) -> bool:
        return self.audience_class == MERCHANT_ACTION and self.cadence == IMMEDIATE


#: reason code -> classification, with an optional second key for the one code
#: that means two different things.
#:
#: **`ROOT_TARGET_FRAME_ABANDONED` is that code.** "转人工" and "算了这个不用查了"
#: both arrive as an abandon, because `interruption.relation` carries no scope:
#: `human_request` and `exit` are distinct values, but both end the root frame
#: the same way. The ruling wants the first pushed immediately (a customer is
#: waiting for a person) and the second not pushed at all (the volume is unknown
#: and may be large). So the relation is part of the key -- it is the only thing
#: that separates them, and where it is unknown the conservative entry wins.
#: Cross-reference: the backlog item asking for scope on `exit` (T74 / the T53
#: batch's registered follow-ups) is what would let this be one entry again.
_POLICY: dict[tuple[str, str | None], Classification] = {
    # -- pushed now (ruling two) ---------------------------------------------
    ("ROOT_TARGET_FRAME_ABANDONED", "human_request"): Classification(
        MERCHANT_ACTION, IMMEDIATE
    ),
    ("cost_hard_ceiling", None): Classification(MERCHANT_ACTION, IMMEDIATE),
    # Not named in the ruling, classified with `cost_hard_ceiling` because the
    # customer-visible consequence is identical: the conversation goes silent
    # and nobody answers. Flagged in the self-review as an interpretation.
    ("cost_estimated_hard_ceiling", None): Classification(MERCHANT_ACTION, IMMEDIATE),
    # Producers arrive with L2b; the classification is here so that batch adds a
    # producer rather than a policy.
    ("DELIVERY_UNCONFIRMED", None): Classification(MERCHANT_ACTION, BATCHED),
    ("DELIVERY_ATTEMPTS_EXHAUSTED", None): Classification(MERCHANT_ACTION, BATCHED),
    # The provider looked at the send and refused it. Ruling two named
    # "投递终态失败（delivery FAILED）——汇总推"; this table had the sentence for
    # the retries-exhausted half and nothing for the refused half, and L2b
    # produced no task for it at all (independent review 2026-09-04, P1-1).
    # Whether a provider-refused delivery belongs in the merchant's digest is a
    # push-scope question and therefore the user's; the ruling on 2026-09-04
    # was that it does.
    ("DELIVERY_FAILED", None): Classification(MERCHANT_ACTION, BATCHED),
    # **Rate limited (B1-6 / ADR-0095 §五).** The engine wanted to send more
    # than the ruling allows, so the extra messages became this instead of
    # disappearing -- ADR-0084 §三's rule, that a discarded message is one
    # nobody can ever look up.
    #
    # **`BATCHED`, not `IMMEDIATE`, and the reason is a loop.** A limiter that
    # trips is likely to trip repeatedly within the same minute, and an
    # immediate notice is itself a message; pushing one per trip would answer a
    # burst with a burst. The digest is the shape that reports "this happened N
    # times" once.
    ("RATE_LIMITED", None): Classification(MERCHANT_ACTION, BATCHED),
    # **AI 应下了一件它做不到的事（用户裁定 2026-09-11「至少派一条待办」）。**
    #
    # **`IMMEDIATE` 是一次解读，照 `cost_estimated_hard_ceiling` 那条的先例
    # 明写在这里**：推送范围按本表的成规是用户的事，而这一条没有单独裁过。
    # 取 `IMMEDIATE` 的理由是**这条待办的价值随时间塌**——它多半挂在一个买入
    # 信号上（「我把您标记一下」），客户还热着的时候同事接手才有意义，
    # 隔一个汇总窗口再看到就只剩记录价值。
    #
    # **与 `RATE_LIMITED` 取 `BATCHED` 的理由不冲突**：那一条怕的是
    # 「限流器一分钟内反复触发，逐条推等于用一串消息回答一串消息」；
    # 这一条**一个回合至多一条**（幂等键带 `event_id`），而且只在模型提名了
    # 一个没建的 kind 时才发生。
    #
    # **已知的反面**：如果某个 profile 的模型每一轮都提名 `manual_task`，
    # 这里就会一轮一条。真出现的话该改的是那个 profile 或这张表，
    # 而不是让待办消失——写在这里是为了那一天有人能找到该改哪儿。
    ("INTENT_KIND_NEEDS_A_PERSON", None): Classification(MERCHANT_ACTION, IMMEDIATE),
    # **T365（2026-09-19）：与上一行同档。** 两条都是「客户在等一个人」，
    # 而 `IMMEDIATE` 的理由在这一条上更强：上一条至少留下了一行
    # `policy_decisions`（查库的人看得见），**这一条在本批之前
    # 连那一行都没有** —— 那句话被丢掉，没有任何地方记着客户提过这个请求。
    #
    # ⚠️ **`MERCHANT_ACTION` 而不是 `PLATFORM_ACTION`**：要做的事是商家的
    # （约个会、出一份报价），平台做不了也不该代做。
    ("AI_PROMISED_A_HUMAN_ACTION", None): Classification(MERCHANT_ACTION, IMMEDIATE),
    # -- produced, deliberately not pushed yet (ruling three) ----------------
    ("ROOT_TARGET_FRAME_ABANDONED", "exit"): Classification(MERCHANT_ACTION, NO_PUSH),
    ("ROOT_TARGET_FRAME_ABANDONED", None): Classification(MERCHANT_ACTION, NO_PUSH),
    ("ROOT_TARGET_FRAME_COMPLETED", None): Classification(MERCHANT_ACTION, NO_PUSH),
    ("INBOX_EVENT_ATTEMPTS_EXHAUSTED", None): Classification(MERCHANT_ACTION, NO_PUSH),
    # Not named in the ruling. A stack with nothing workable left is the same
    # shape as the two above -- the merchant cannot act on it today -- so it
    # takes the conservative entry rather than an invented one.
    ("TARGET_STACK_EMPTY", None): Classification(MERCHANT_ACTION, NO_PUSH),
    # **A cost ceiling hit while a human already holds the conversation**
    # (ADR-0054 三.6). Classified apart from `cost_hard_ceiling` on purpose,
    # and the difference is the customer: that entry is IMMEDIATE because the
    # conversation goes silent and nobody answers, and **here somebody is
    # already answering**. There is nothing to page a manager about right now,
    # and paging one mid-conversation about a budget is the noise that teaches
    # people to ignore the channel.
    #
    # What is real is the consequence after the fact: the understanding step
    # stopped recording, so the resume plans blind and the resume preview is
    # short its data. That belongs in the merchant's task list, which is what
    # MERCHANT_ACTION + NO_PUSH means here -- "merchant-facing in principle,
    # deliberately not pushed in this batch", one table edit from being pushed
    # if it turns out to matter. **Not `platform_internal`**: that would be a
    # decision that a merchant never needs to know their conversation resumed
    # without knowing what was said to it.
    ("UNDERSTANDING_HALTED_UNDER_HUMAN_CONTROL", None): Classification(
        MERCHANT_ACTION, NO_PUSH
    ),
    # The ruling puts the unreconciled backlog itself in platform-internal; this
    # code is that backlog *refusing a conversation*, which is customer-visible.
    # Classified merchant-facing but unpushed rather than silently deciding
    # either way. Flagged in the self-review.
    ("cost_unsettled_backlog", None): Classification(MERCHANT_ACTION, NO_PUSH),
    # -- never a merchant notice (ruling four) -------------------------------
    ("BALANCE_DIFFERENCE_OVER_TOLERANCE", None): Classification(PLATFORM_INTERNAL, NO_PUSH),
    ("UNRECONCILED_BACKLOG", None): Classification(PLATFORM_INTERNAL, NO_PUSH),
    # **A notice to the manager that did not arrive** (user's ruling,
    # 2026-09-08; B1-5 independent review P1-1). The same two terminal states as
    # `DELIVERY_UNCONFIRMED` / `DELIVERY_FAILED`, kept apart because of *whose*
    # message failed, and the difference is not cosmetic -- it is what keeps the
    # digest from feeding itself.
    #
    # Before B1-5 the `audience = 'customer'` predicate in `_claim_one` had two
    # jobs: it kept manager rows out of the dispatcher, and (nobody wrote this
    # down) it kept their failures out of the digest. B1-5 took over the first
    # job only. With the manager rows delivered for real, a failing manager
    # channel produced: notice fails -> manual task -> `MERCHANT_ACTION` +
    # `BATCHED` -> the next digest window sweeps it up -> writes another manager
    # row -> fails -> another task. `DIGEST_INTERVAL` caps that at one an hour
    # per tenant, which limits the **rate** and not the **termination**, and the
    # count inside the digest sentence starts counting the digest's own failures.
    #
    # The ring is broken **by construction**, not by a `WHERE ... NOT IN`:
    # `digest.py` selects `MERCHANT_ACTION AND BATCHED`, so a platform-internal
    # task is not a row it can ever see. The task is still written and still
    # listed by `GET /manual-tasks` -- somebody still owns it, which is the
    # whole of L2 -- it just cannot become the next notice.
    #
    # **They are also why "we never asked" stopped borrowing the sentence for
    # "the provider refused it"** (P2-3): `DELIVERY_FAILED` reads to a merchant
    # as 「有 N 条消息被渠道拒收」, and for a notice with no address at all every
    # word of that is false. These two codes are never shown to a merchant, so
    # they need no sentence -- and being `NO_PUSH` rather than `BATCHED` they are
    # outside what `batched_kinds_without_a_sentence()` requires one for.
    ("MANAGER_NOTICE_UNCONFIRMED", None): Classification(PLATFORM_INTERNAL, NO_PUSH),
    ("MANAGER_NOTICE_UNDELIVERED", None): Classification(PLATFORM_INTERNAL, NO_PUSH),
}

#: What an unclassified code gets. **Platform-internal and unpushed**, because
#: the failure directions are not symmetric: a merchant-facing task wrongly held
#: back is found by someone reading the task list, while a platform-internal one
#: wrongly pushed is a message to a person who cannot act on it -- and the
#: fastest way to make a notification channel worthless is to put things in it
#: that nobody can act on.
UNCLASSIFIED = Classification(PLATFORM_INTERNAL, NO_PUSH)


def classify(reason_code: str, *, interruption_relation: str | None = None) -> Classification:
    """Look up one manual task's audience and cadence.

    The relation refines the lookup only where the table says it does; every
    other code ignores it, so a caller that has no relation to offer is not
    forced to invent one.
    """

    if interruption_relation is not None:
        refined = _POLICY.get((reason_code, interruption_relation))
        if refined is not None:
            return refined
    return _POLICY.get((reason_code, None), UNCLASSIFIED)


def digest_notice(*, kind: str, count: int) -> str | None:
    """One batched notice, or **nothing at all** when the window was quiet.

    Ruling (user, 2026-09-04): a window with nothing in it produces no message.
    Not "0 pending" -- no message. An empty notice teaches the reader that this
    source is usually noise, and the cost of that lesson is paid on the day the
    notice is not empty and nobody opens it.
    """

    if count <= 0:
        return None
    sentence = DIGEST_SENTENCES.get(kind)
    if sentence is None:
        return None
    return sentence.format(count=count)


#: The batched kinds, as sentences with a count. Separate from
#: `REASON_SENTENCES` because these are summaries of many events rather than a
#: statement about one conversation.
DIGEST_SENTENCES = {
    "DELIVERY_UNCONFIRMED": "有 {count} 条消息的投递状态待确认，需要人工核对。",
    "DELIVERY_ATTEMPTS_EXHAUSTED": "有 {count} 条消息最终未能送达，需要人工处理。",
    # Distinct from the line above on purpose. "Retried until the budget ran
    # out" and "the provider refused it" read the same to a machine and not at
    # all the same to the person acting on it: the first may be worth resending,
    # the second will fail again for the same reason until something changes.
    "DELIVERY_FAILED": "有 {count} 条消息被渠道拒收，需要人工处理。",
    # Says what was withheld and why, without naming a limit the merchant did
    # not set: the two numbers are the platform's (ADR-0095 §五, marked for
    # recalibration against the first real merchant), so the sentence describes
    # the effect rather than quoting a threshold that is expected to move.
    "RATE_LIMITED": "有 {count} 条回复因为发送频率限制没有发出，需要人工确认。",
}


#: Every batched kind must have a sentence here, and this is what says so.
#:
#: `digest_notice` returns `None` for a kind it has no sentence for, and the
#: caller treats `None` as "nothing to send" -- so a batched kind added to
#: `_POLICY` without a sentence is **silently and permanently dropped**: not
#: sent, not marked, not logged (independent review 2026-09-04, P2-4). The two
#: tables are edited in different places by different batches, which is exactly
#: when they drift.
def batched_kinds_without_a_sentence() -> frozenset[str]:
    """The set that must stay empty. Asserted by a contract test."""

    batched = {
        reason
        for (reason, _relation), classification in _POLICY.items()
        if classification.cadence == BATCHED
    }
    return frozenset(batched - set(DIGEST_SENTENCES))


def reason_sentence(reason_code: str) -> str:
    """One internal code in, one sentence out. The code itself never leaves.

    The fallback is not a formatting nicety. If unknown codes were appended to
    the sentence "for support", every new reason code would publish a piece of
    the platform's internal state machine to every merchant, and the first time
    anyone noticed would be when one of them quoted it back.
    """

    return REASON_SENTENCES.get(reason_code, UNKNOWN_SENTENCE)


def short_reference(identifier: str) -> str:
    """A UUID a person can read aloud: first four characters, then an ellipsis.

    Four is enough to tell today's handful of conversations apart in a notice,
    and the full id is one audit query away for anyone who needs to act on it.
    A merchant does not need 128 bits to know which conversation is meant.
    """

    head = identifier.replace("-", "")[:4]
    return f"{head}…" if head else "…"


def notify_text(text: str) -> str:
    """Make one piece of free text safe to send to the merchant.

    Customer wording survives; ours does not. The order is credentials first
    (they may contain hex that looks like an id), then identifiers.
    """

    cleaned = text
    for pattern in _CREDENTIAL_PATTERNS:
        cleaned = pattern.sub(CREDENTIAL_PLACEHOLDER, cleaned)
    return _UUID.sub(lambda match: short_reference(match.group(0)), cleaned)


#: How much of the customer's own message the notice carries.
#:
#: The envelope contract allows 20000 characters and WeCom's own text message
#: limit is about 2048 **bytes**, so an unbounded quote produces a notice that
#: fails at the channel the day delivery is switched on -- with an error
#: ("message too long") that has nothing to do with the thing being reported,
#: which is the worst kind of failure to debug (independent review 2026-09-04,
#: P2-2). 240 characters is roughly a screenful on a phone and leaves room for
#: the two lines above it inside the byte limit even when every character is
#: three bytes of Chinese.
#:
#: The neighbouring `detail[:500]` in `pipeline.py` is the same habit: this
#: repository truncates rather than hopes.
CUSTOMER_QUOTE_LIMIT = 240


def manual_task_notice(
    *,
    reason_code: str,
    conversation_id: str,
    customer_message: str | None = None,
    promised_text: str | None = None,
    known_facts: Mapping[str, Any] | None = None,
) -> str:
    """The notification body for one manual task.

    Parts, in the order a person reads them: what happened, which conversation,
    what the customer said, and -- when the task exists because the AI promised
    something -- **what the AI actually said it would do**.

    **The template is fixed and the model's words are attached, not either/or**
    (user ruling 2026-09-11). The fixed half is what makes the notice readable
    at a glance and identical every time; the model's own sentence is what the
    colleague needs in order to keep the promise **in the words the customer
    already heard**. A template alone would say "a person is needed" without
    saying what was promised; the model's words alone would be a sentence with
    no frame around it, arriving from nowhere.

    ## 我们只尽到通知的义务（用户 2026-09-17）

    **这里不建催办、不建重发、不建升级。** 通知发出去就结束；
    发不出去（没配联系人）写一行 `manager_notice.undeliverable` 并被指标数着。
    写在这里，是免得下一个人顺手加一个「没人回就再发一次」——
    那会把一个通知变成一条投递语义，而那不是这条路要承担的。

    `promised_text` goes through the same redaction and the same limit as the
    customer quote. It is **our own draft** rather than the customer's words,
    but it is not therefore safe: the model composed it from material that can
    contain anything, and `notify_text` is cheap.
    """

    lines = [
        reason_sentence(reason_code),
        f"会话 {short_reference(conversation_id)}",
    ]
    if customer_message:
        lines.append(f"客户最后一句：{_quoted(customer_message)}")
    if promised_text:
        lines.append(f"AI 刚才答应：{_quoted(promised_text)}")
    known = _known_facts_line(known_facts)
    if known:
        lines.append(known)
    # **接手就在这通对话里** —— `control_mode` 翻成 `human` 之后 AI 当场闭嘴，
    # 而会话还是原来那一通。不写这一句，收到通知的人第一反应是「我该去哪回」。
    lines.append("接手方式：直接在这通对话里回他，AI 已经停了。")
    return "\n".join(lines)


def _known_facts_line(known_facts: Mapping[str, Any] | None) -> str | None:
    """我们已经问出来的那几格，一行摆给同事看。

    **同事最不该做的事是把 AI 刚问过的再问一遍**，而不给这份信息
    就一定会发生。槽位是商家自己声明的，值是客户自己说的话 ——
    收件人是这个商家的同事，看的是他自己的客户。

    **空值不写。** 「`concern_topic`：（空）」这样的行只会让人多读一遍
    才发现它什么都没说；没填的格子直接不出现。

    **值也过一遍 `notify_text`**：槽位值是模型从客户的话里摘的，
    而客户的话里可以有任何东西。
    """

    if not known_facts:
        return None
    parts = [
        f"{name}={_quoted(str(value))}"
        for name, value in sorted(known_facts.items())
        if value not in (None, "", [], {})
    ]
    if not parts:
        return None
    return "已经问出来的：" + "，".join(parts)


def _quoted(text: str) -> str:
    """Redact, then truncate. **Never the other way round.**

    Cutting first could split a credential and leave a fragment that no longer
    matches any pattern -- a redactor that leaks when the input is long is worse
    than one that does not run at all.
    """

    quoted = notify_text(text)
    if len(quoted) > CUSTOMER_QUOTE_LIMIT:
        quoted = quoted[:CUSTOMER_QUOTE_LIMIT] + "…（已截断）"
    return quoted
