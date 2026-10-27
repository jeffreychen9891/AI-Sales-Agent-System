"""The vocabulary for text a person at the merchant reads.

`redact()` and `notify_text()` fail in opposite directions on purpose: the log
redactor removes when unsure, the notifier keeps when unsure -- because an
unreadable log costs an engineer minutes and an unreadable notice costs the
merchant the whole point of being notified. Most of what is asserted here is
therefore what is **kept**, which is the half a redaction test usually has none
of, and the half that decays first if nobody pins it.
"""

from __future__ import annotations

import pytest
from fastagent_v2.runtime.notification import (
    BATCHED,
    CREDENTIAL_PLACEHOLDER,
    CUSTOMER_QUOTE_LIMIT,
    IMMEDIATE,
    MERCHANT_ACTION,
    NO_PUSH,
    PLATFORM_INTERNAL,
    REASON_SENTENCES,
    UNKNOWN_SENTENCE,
    Classification,
    batched_kinds_without_a_sentence,
    classify,
    digest_notice,
    manual_task_notice,
    notify_text,
    reason_sentence,
    short_reference,
)
from fastagent_v2.runtime.redaction import redact

CONVERSATION = "3f2a91c4-0000-4000-8000-000000000001"

#: The credential-shaped strings below carry the repository's own placeholder
#: markers (`not-a-real`, `local-only`). They have to look like credentials to
#: `notify_text` and must not look like one to `scripts/check_secrets.py`, whose
#: guidance is "narrow the pattern or move the value out of the repository --
#: do not add the value to an allowlist". Its placeholder vocabulary is that
#: narrowing, and using it is the sanctioned way to write a shape down here;
#: the only files allowed to hold raw shapes are the scanner's own fixtures.


def test_a_customers_own_words_arrive_unchanged() -> None:
    """The merchant may read what their customer said. Verbatim.

    This is the case that separates the two vocabularies, and it is first
    because it is the one that erodes: every future tightening will be argued
    for on safety grounds, and this test is where "safety" has to stop.
    """

    said = "我上周订的那批货到底什么时候发？再不发我就退了，别给我打电话。"

    assert notify_text(said) == said


def test_a_credential_hiding_in_the_customers_words_is_taken_out() -> None:
    """Fail closed on our side of the line, and only on our side.

    The customer pasted something key-shaped. The sentence around it is theirs
    and survives; the key is ours to remove -- once it is in a WeCom message it
    is in someone's phone.
    """

    said = "他们给我的接口密钥是 sk-not-a-real-key-0123456789abcd，这样对吗？"

    cleaned = notify_text(said)

    assert "sk-not-a-real-key-0123456789abcd" not in cleaned
    assert CREDENTIAL_PLACEHOLDER in cleaned
    assert cleaned.startswith("他们给我的接口密钥是")
    assert cleaned.endswith("，这样对吗？")


@pytest.mark.parametrize(
    "carrier",
    [
        "Bearer not-a-real-token-abcdef12",
        "postgresql://app:local-only-password@db.internal:5432/fastagent",
        "AKIDabcdefghijklmnopqrstuvwxyz01",
        "d3b07384d113edec49eaa6238ad5ff00d3b07384d113edec49eaa6238ad5ff00",
    ],
)
def test_every_shape_we_know_to_be_a_credential_is_removed(carrier: str) -> None:
    """Four shapes, one of which this system has actually leaked before.

    The 64-hex one is the WeComProxy callback path token (T31) -- the credential
    that did reach a log file here. A notification that quoted a request line
    would carry it to a phone.
    """

    cleaned = notify_text(f"客户发来：{carrier} 请看一下")

    assert carrier not in cleaned
    assert CREDENTIAL_PLACEHOLDER in cleaned
    assert cleaned.startswith("客户发来：") and cleaned.endswith(" 请看一下")


def test_an_internal_identifier_becomes_something_a_person_can_say() -> None:
    """A merchant needs to know *which* conversation, not 128 bits of it."""

    cleaned = notify_text(f"会话 {CONVERSATION} 需要处理")

    assert CONVERSATION not in cleaned
    assert "3f2a…" in cleaned


@pytest.mark.parametrize("code", sorted(REASON_SENTENCES))
def test_a_known_reason_code_is_replaced_by_a_sentence(code: str) -> None:
    """Every catalogued code, checked -- including that the code itself is gone."""

    sentence = reason_sentence(code)

    assert sentence == REASON_SENTENCES[code]
    assert code not in sentence
    assert sentence.endswith("。"), "these are read alone; they have to be sentences"


def test_an_unknown_reason_code_does_not_leak_itself() -> None:
    """The generic sentence is not politeness -- it is the containment.

    If unknown codes were appended "so support can look it up", every new reason
    code would publish a piece of the platform's state machine to every
    merchant, and nobody would notice until one of them quoted it back.
    """

    sentence = reason_sentence("SOME_FUTURE_INTERNAL_STATE")

    assert sentence == UNKNOWN_SENTENCE
    assert "SOME_FUTURE" not in sentence


def test_the_two_vocabularies_disagree_on_purpose() -> None:
    """The guard against a future "let's unify these".

    They are not two implementations of one idea. `redact()` protects a log and
    removes when unsure; `notify_text()` protects a message to a person and
    keeps when unsure. If someone routes one through the other, this fails --
    which is the point at which they have to read why they are separate.
    """

    said = "我的订单号是 12345，你们那个 token 到底管不管用"

    # Same sentence, two contexts. In a log structure under a key that smells of
    # a secret, the redactor drops the whole value -- correctly, because it
    # cannot know what a field called `channel_token` holds. In a notice to the
    # merchant the same words are the merchant's own customer speaking, and they
    # go through untouched.
    assert notify_text(said) == said
    assert redact({"channel_token": said}) == {"channel_token": "[REDACTED]"}, (
        "the log redactor is expected to drop this and the notifier to keep it; "
        "if they now agree, the two vocabularies have been merged and this test "
        "is the notice"
    )


def test_the_notice_reads_as_three_things_in_the_order_a_person_reads_them() -> None:
    """What happened, which conversation, what the customer said."""

    notice = manual_task_notice(
        reason_code="ROOT_TARGET_FRAME_ABANDONED",
        conversation_id=CONVERSATION,
        customer_message="不用了，别再问了",
    )

    lines = notice.splitlines()
    assert lines[0] == REASON_SENTENCES["ROOT_TARGET_FRAME_ABANDONED"]
    assert lines[1] == f"会话 {short_reference(CONVERSATION)}"
    assert lines[2] == "客户最后一句：不用了，别再问了"
    assert CONVERSATION not in notice


def test_a_notice_without_a_customer_message_says_only_what_it_knows() -> None:
    """Some handoffs are triggered by a timer or a ceiling, not by a message.

    **「接手方式」那一行永远在**（2026-09-17 加）：接手就在这通对话里
    （`control_mode` 翻成 `human` 之后 AI 当场闭嘴，会话还是原来那一通），
    而收到通知的人**没有别的地方可以读到这件事**。
    它不依赖有没有客户那句话，所以它不在条件里。
    """

    notice = manual_task_notice(
        reason_code="cost_hard_ceiling", conversation_id=CONVERSATION
    )

    assert notice.splitlines() == [
        REASON_SENTENCES["cost_hard_ceiling"],
        f"会话 {short_reference(CONVERSATION)}",
        "接手方式：直接在这通对话里回他，AI 已经停了。",
    ]


# --------------------------------------------------------------------------
# Classification: who hears about a manual task, and how often (rulings of
# 2026-09-04). The table is the product decision; these are the four properties
# that must survive an edit to it.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason_code", "relation"),
    [("ROOT_TARGET_FRAME_ABANDONED", "human_request"), ("cost_hard_ceiling", None)],
)
def test_the_kinds_that_are_pushed_immediately(reason_code: str, relation: str | None) -> None:
    """A customer waiting for a person, and a conversation that has gone silent.

    Both are cases where the merchant loses something by hearing about it an
    hour later: in the first a person is waiting, in the second every further
    customer message goes unanswered.
    """

    classification = classify(reason_code, interruption_relation=relation)

    assert classification.audience_class == MERCHANT_ACTION
    assert classification.cadence == IMMEDIATE
    assert classification.pushes_now is True


def test_the_same_reason_code_means_two_different_things_and_is_classified_as_such() -> None:
    """The one place the relation changes the answer.

    "转人工" and "算了这个不用查了" both abandon the root frame, so both arrive as
    `ROOT_TARGET_FRAME_ABANDONED`. The ruling pushes the first immediately and
    holds the second back until its real frequency is known. Nothing but the
    relation separates them today -- and where the relation is unknown, the
    conservative entry has to win, or an unscoped exit signal becomes a phone
    notification.
    """

    asked_for_a_person = classify(
        "ROOT_TARGET_FRAME_ABANDONED", interruption_relation="human_request"
    )
    dropped_a_question = classify("ROOT_TARGET_FRAME_ABANDONED", interruption_relation="exit")
    no_relation_known = classify("ROOT_TARGET_FRAME_ABANDONED")

    assert asked_for_a_person.pushes_now is True
    assert dropped_a_question.pushes_now is False
    assert no_relation_known.pushes_now is False
    # Both remain merchant-facing: the difference is cadence, not audience.
    assert dropped_a_question.audience_class == MERCHANT_ACTION


@pytest.mark.parametrize(
    "reason_code",
    ["ROOT_TARGET_FRAME_COMPLETED", "TARGET_STACK_EMPTY", "INBOX_EVENT_ATTEMPTS_EXHAUSTED"],
)
def test_the_kinds_that_are_produced_but_not_pushed(reason_code: str) -> None:
    """Produced, classified, and deliberately silent -- one edit from being pushed.

    This is the state that would disappear if audience and cadence were one
    column: it is *not* platform-internal, and saying so is what makes turning
    it on later a decision rather than a rediscovery.
    """

    classification = classify(reason_code)

    assert classification.audience_class == MERCHANT_ACTION
    assert classification.cadence == NO_PUSH
    assert classification.pushes_now is False


@pytest.mark.parametrize(
    "reason_code", ["BALANCE_DIFFERENCE_OVER_TOLERANCE", "UNRECONCILED_BACKLOG"]
)
def test_the_kinds_a_merchant_never_hears_about(reason_code: str) -> None:
    """Platform problems. A merchant cannot act on either, so neither is theirs."""

    classification = classify(reason_code)

    assert classification.audience_class == PLATFORM_INTERNAL
    assert classification.pushes_now is False


@pytest.mark.parametrize(
    "reason_code", ["DELIVERY_UNCONFIRMED", "DELIVERY_ATTEMPTS_EXHAUSTED"]
)
def test_the_batched_kinds_are_classified_before_they_have_producers(reason_code: str) -> None:
    """L2b adds the producers; the policy is already here.

    Deliberate: it means that batch adds a *producer*, not a policy, and the
    scope decision stays in one table where it can be read whole.
    """

    assert classify(reason_code).cadence == BATCHED


def test_an_unclassified_reason_code_is_not_pushed_to_anyone() -> None:
    """The default has to fail towards silence, and this says which silence.

    A merchant-facing task wrongly held back is found by whoever reads the task
    list. A platform-internal one wrongly pushed is a message to someone who
    cannot act on it -- and the fastest way to make a notification channel
    worthless is to fill it with things nobody can act on.
    """

    classification = classify("SOME_CODE_NOBODY_HAS_CLASSIFIED_YET")

    assert classification.audience_class == PLATFORM_INTERNAL
    assert classification.cadence == NO_PUSH


def test_every_reason_code_that_can_create_a_manual_task_today_is_classified() -> None:
    """Who checks the hand-written table? This does.

    The self-review of the first round admitted this guard was missing and said
    why (reason codes have no single source). `HANDOFF_REASON_CODES` **is** that
    source for the codes that reach a manual task today, so the guard exists now
    -- and a new handoff code will fail here rather than silently defaulting to
    "nobody hears about it".
    """

    from fastagent_v2.worker.pipeline import HANDOFF_REASON_CODES

    unclassified = [
        code
        for code in sorted(HANDOFF_REASON_CODES)
        if classify(code).audience_class == PLATFORM_INTERNAL
        and classify(code).cadence == NO_PUSH
        and code not in {"BALANCE_DIFFERENCE_OVER_TOLERANCE", "UNRECONCILED_BACKLOG"}
    ]
    assert unclassified == [], (
        f"these reason codes create manual tasks but have no entry in the "
        f"notification policy, so they fell through to the default: {unclassified}"
    )


# --------------------------------------------------------------------------
# The digest, and the rule that a quiet window says nothing at all.
# --------------------------------------------------------------------------


def test_a_quiet_window_produces_no_message_at_all() -> None:
    """Ruling (user, 2026-09-04): no "0 pending this hour".

    An empty notice teaches the reader that this source is usually noise, and
    that lesson gets paid for on the day the notice is not empty and nobody
    opens it. `None` rather than an empty string, so a caller cannot accidentally
    send it.
    """

    assert digest_notice(kind="DELIVERY_UNCONFIRMED", count=0) is None
    assert digest_notice(kind="DELIVERY_UNCONFIRMED", count=-1) is None


def test_a_non_empty_window_says_how_many() -> None:
    notice = digest_notice(kind="DELIVERY_UNCONFIRMED", count=3)

    assert notice is not None
    assert "3" in notice
    assert notice.endswith("。")


def test_a_digest_for_a_kind_nobody_wrote_a_sentence_for_is_silent() -> None:
    """Same failure direction as the classification default: silence, not a guess."""

    assert digest_notice(kind="SOMETHING_ELSE", count=5) is None


# --------------------------------------------------------------------------
# What the independent review of 2026-09-04 found, kept as tests.
# --------------------------------------------------------------------------


def test_every_immediately_pushed_code_has_a_sentence_of_its_own() -> None:
    """F1, as the guard that was missing.

    The cost family is written lower case by its producers (`cost.py`) and the
    first version of the sentence table wrote it upper case, so the two kinds the
    ruling pushes **immediately** delivered the generic fallback while the
    sentences meant for them sat unreachable. Nothing caught it because the
    existing test parametrised over `REASON_SENTENCES`' own keys -- **the table
    looking itself up**, true no matter what the producers emit.

    The fact source is the same one the classification guard already uses, so
    the self-review's reason for postponing this ("no single source yet") was
    wrong for the subset that matters: the codes that are actually pushed.
    """

    from fastagent_v2.worker.pipeline import HANDOFF_REASON_CODES

    pushed = [code for code in sorted(HANDOFF_REASON_CODES) if classify(code).pushes_now]
    assert pushed, "if nothing is pushed immediately, this guard is watching nothing"
    without_sentence = [code for code in pushed if reason_sentence(code) == UNKNOWN_SENTENCE]
    assert without_sentence == [], (
        f"these codes are pushed to a merchant immediately and would arrive as "
        f"the generic sentence: {without_sentence}"
    )


@pytest.mark.parametrize(
    ("said", "leak"),
    [
        ("我的key是sk-not-a-real-key-0123456789abcd", "sk-not-a-real-key-0123456789abcd"),
        ("授权头是Bearer not-a-real-token-abcdef12", "not-a-real-token-abcdef12"),
        ("回调令牌是" + "d3b07384d113edec49eaa6238ad5ff00" * 2, "d3b07384d113edec"),
        ("会话3f2a91c4-0000-4000-8000-000000000001需要处理", CONVERSATION),
    ],
)
def test_a_token_touching_a_chinese_character_is_still_taken_out(said: str, leak: str) -> None:
    r"""F2: `\b` does not fire between 汉字 and a token, and that is the normal case.

    Python's `\w` counts CJK as word characters, so there is no boundary to
    match. Every fixture in the first version of these tests happened to put a
    space or a full-width colon before the token -- **the most expensive
    character in this module was a space in a test.** These four are the
    reviewer's own reproductions, including the 64-hex WeComProxy path token
    this system has leaked once for real (T31).
    """

    cleaned = notify_text(said)

    assert leak not in cleaned
    assert cleaned != said


def test_a_long_hex_string_is_not_chewed_in_half() -> None:
    """The other side of tightening the 64-hex rule.

    Anchoring on hex rather than on the general token class means a 128-character
    hash is left alone instead of being reported as "redacted plus leftovers",
    which would look like a partially hidden secret and send someone hunting.
    """

    long_hash = "d3b07384d113edec49eaa6238ad5ff00" * 4

    assert notify_text(f"哈希是{long_hash}") == f"哈希是{long_hash}"


def test_a_very_long_customer_message_is_truncated_before_it_reaches_a_channel() -> None:
    """P2-2: WeCom's text limit is about 2048 bytes; the envelope allows 20000.

    An unbounded quote produces a notice that fails **at the channel** the day
    delivery is switched on, with an error about message length that has nothing
    to do with the thing being reported. The repository's habit next door is
    `detail[:500]`; this is the same habit applied where a person will read it.
    """

    said = "退款" * 5000

    notice = manual_task_notice(
        reason_code="cost_hard_ceiling",
        conversation_id=CONVERSATION,
        customer_message=said,
    )

    quoted = notice.splitlines()[2]
    assert quoted.endswith("…（已截断）")
    assert len(quoted) < CUSTOMER_QUOTE_LIMIT + 40


def test_truncation_happens_after_redaction_not_before() -> None:
    """Order matters, and the wrong order leaks.

    Cutting first could split a credential so that no pattern matches the
    remaining fragment -- a redactor that fails on long input is worse than one
    that is not there, because the tests would still be green.
    """

    leak = "sk-not-a-real-key-0123456789abcd"
    said = "填充" * (CUSTOMER_QUOTE_LIMIT - 5) + leak + "填充" * 50

    notice = manual_task_notice(
        reason_code="cost_hard_ceiling",
        conversation_id=CONVERSATION,
        customer_message=said,
    )

    assert leak not in notice
    assert "sk-not-a-real" not in notice


def test_every_batched_kind_has_a_sentence_to_say() -> None:
    """P2-4: the two tables are edited by different batches, and they drift.

    `digest_notice` returns `None` for a kind it has no sentence for, and the
    digest treats `None` as "nothing to report". So a kind classified `batched`
    in `_POLICY` but missing from `DIGEST_SENTENCES` is **silently and
    permanently dropped** -- never sent, and (before this batch) never logged.

    Asserted as set equality against a computed set, not against a literal list:
    a literal would have to be edited by whoever adds the kind, which is exactly
    the person who just forgot to edit `DIGEST_SENTENCES`.
    """

    assert batched_kinds_without_a_sentence() == frozenset()


def test_a_refused_delivery_is_a_merchant_action_reported_in_the_digest() -> None:
    """The ruling (user, 2026-09-04): `DELIVERY_FAILED` goes to the merchant, batched.

    It was classified nowhere -- `classify("DELIVERY_FAILED")` fell through to
    `platform_internal / none` -- while the batch's own design note said
    "投递终态失败（delivery FAILED）——汇总推". The producer did not exist either
    (independent review 2026-09-04, P1-1).

    The two delivery failures keep separate sentences on purpose: "we retried
    until the budget ran out" and "the channel refused it" are the same event to
    a machine and different work to the person acting on it.
    """

    assert classify("DELIVERY_FAILED") == Classification(MERCHANT_ACTION, BATCHED)

    refused = digest_notice(kind="DELIVERY_FAILED", count=2)
    exhausted = digest_notice(kind="DELIVERY_ATTEMPTS_EXHAUSTED", count=2)
    assert refused is not None and exhausted is not None
    assert refused != exhausted, "a refusal and an exhausted retry budget must not read alike"
    assert "2" in refused
