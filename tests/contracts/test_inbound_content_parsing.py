"""B3-2b / B3-4c: what arrives when the payload is not the shape we hoped for.

Two registered defects, and they are one batch because they are one rule.

**T201** -- `msgType=123` is text *and* a picture, and inbound did not know the
type existed. "Either only the text or only the image, and neither loss reports
itself."

**T206** -- the same `msgType` carries different fields depending on where it
came from, so a one-to-one `msgType -> content` map breaks on the first short
variant. At an inbound boundary an exception is a lost message plus redelivery
into the same crash, and ten of those delete the callback address.

**Most cases here assert on absence rather than on failure**, because the whole
point is that a short payload does *not* raise: it degrades to "this item is
absent" and the message still arrives.
"""

from __future__ import annotations

import pytest
from fastagent_v2.ingress.wecom_proxy.inbound import (
    IMAGE_MSG_TYPES,
    MIXED_MSG_TYPES,
    TEXT_MSG_TYPES,
    message_type,
    parse_content,
)

#: The vendor's own `msgType=123` example (`docs/wecom_proxy.md`, 文字图片混合消息),
#: with the opaque values shortened and nothing else changed. **`msgData` is an
#: array of parts**, the picture in one and the words in another, and the words
#: are base64: `"NDQ="` is `44`.
#:
#: Every mixed-message case below is built from this rather than from a
#: hand-written mapping. That is the whole lesson of the batch this file belongs
#: to -- the previous version's fixtures were invented, so they agreed with the
#: invented code and the gates stayed green while a real `123` was refused
#: whole.
MIXED_PART_IMAGE = {
    "subMsgType": 14,
    "subMsgData": {
        "fileAeskey": "333936643363",
        "fileId": "30680201020461305f02010002047",
        "fileMd5": "2c5817af1f2b45b92f74",
        "fileName": "5LyB5Lia5b6uLnBuZw==",
        "fileSize": 1467,
        "imageHasHd": True,
    },
}
MIXED_PART_TEXT = {"subMsgType": 2, "subMsgData": {"atList": None, "content": "NDQ="}}
MIXED_PAYLOAD = {"msgType": 123, "msgData": [MIXED_PART_IMAGE, MIXED_PART_TEXT]}
MIXED_FILE_ID = "30680201020461305f02010002047"


def test_a_mixed_message_keeps_both_halves() -> None:
    """T201, stated as the loss it prevents -- **on the real payload shape**.

    One envelope, `type` image, caption in `text` (user ruling 2026-09-08). The
    contract already allowed the pair, so this needed no schema change -- and
    one callback stays one event, which is what keeps `external_event_id`
    idempotency untouched.

    **The previous version of this case passed while the code lost everything.**
    It fed `{"msgData": {"content": ..., "filePath": ...}}`, a mapping with two
    invented keys. A real `123` is an array of parts, so the parser found
    neither half, returned `None`, and `accept()` refused the message outright:
    the batch written to stop half a message being lost dropped all of it, and
    no gate could say so because the case and the code shared one wrong premise.
    """

    content = parse_content(MIXED_PAYLOAD)

    assert content == {"type": "image", "text": "44", "payload_ref": MIXED_FILE_ID}
    # Both halves, named individually: an assertion on the whole dict would go
    # green if one half moved to a key nobody reads.
    assert content["text"] == "44"
    assert content["payload_ref"] == MIXED_FILE_ID


def test_the_words_in_a_mixed_part_are_base64_and_are_decoded() -> None:
    """`"NDQ="` is what the customer typing `44` actually arrives as.

    Storing it undecoded would put base64 in front of the model and into the
    manager's notice. **Decoding is defensive rather than trusting**, because
    the document shows exactly one such part: anything that is not clean base64
    over valid UTF-8 comes back unchanged, so a payload that turns out to be
    plain costs nothing, and no branch can drop the text.
    """

    from fastagent_v2.ingress.wecom_proxy.inbound import decode_part_text

    assert decode_part_text("NDQ=") == "44"
    # Not base64 at all (Chinese cannot be -- base64's alphabet is ASCII).
    assert decode_part_text("这个多少钱") == "这个多少钱"
    # Valid base64 whose bytes are not UTF-8: kept as it arrived, not dropped.
    assert decode_part_text("//79") == "//79"
    # Whitespace-only after decoding is not an improvement on the original.
    assert decode_part_text("ICAg") == "ICAg"

    # And the same string in a *plain* `msgData` is not touched: the rule is
    # positional, so a customer who really types `NDQ=` in an ordinary text
    # message still has it stored as typed.
    assert parse_content({"msgType": 0, "msgData": {"content": "NDQ="}}) == {
        "type": "text",
        "text": "NDQ=",
    }


def test_a_part_that_is_not_a_mapping_does_not_take_the_message_down() -> None:
    """The array can be short, ragged, or carry something unexpected.

    Same rule as everywhere else here: skip what cannot be read, keep whatever
    else arrived. A `123` that lost its text part is still a picture.
    """

    assert parse_content({"msgType": 123, "msgData": [MIXED_PART_IMAGE]}) == {
        "type": "image",
        "payload_ref": MIXED_FILE_ID,
    }
    assert parse_content(
        {"msgType": 123, "msgData": ["not-a-part", None, 7, MIXED_PART_TEXT]}
    ) == {"type": "text", "text": "44"}
    assert parse_content({"msgType": 123, "msgData": [{"subMsgType": 14}]}) is None
    assert parse_content({"msgType": 123, "msgData": []}) is None


def test_the_picture_with_no_caption_is_still_a_message() -> None:
    """The case that used to be refused outright.

    Before this batch `text` was unconditionally required, so an uncaptioned
    image was an `invalid callback payload` -- the batch that exists to stop
    half a message being dropped would have dropped all of it.
    """

    content = parse_content({"msgType": 14, "msgData": {"fileId": "3068abc", "fileSize": 819}})

    assert content == {"type": "image", "payload_ref": "3068abc"}
    assert "text" not in content


def test_the_variant_adr_0103_names_is_the_one_that_must_not_raise() -> None:
    """ADR-0103 §四's worked example: a picture with no `content` at all.

    Named on its own because it is the exact payload the ADR uses to argue that
    field-driven parsing is a premise rather than a caution. **The ADR's own
    wording said that picture carries `filePath`**, which the vendor document
    refutes -- the argument was right and its example was invented, and §四 now
    carries the correction.

    Written as the personal-account image (`msgType=101`), which is the variant
    that matters: **it has no `fileId` whatsoever**. Correcting `filePath` to
    `fileId` alone -- the obvious reading of the corporate example -- would have
    degraded every personal-account picture to a bare checksum without one line
    of code looking wrong.
    """

    content = parse_content(
        {
            "msgType": 101,
            "msgData": {
                # Vendor worked example, copied verbatim out of
                # `docs/wecom_proxy.md` -- the third tier of `AGENTS.md`'s
                # real-sample rule. Not a value we issued (T266 (a), user's
                # ruling 2026-09-10). Suppressed **per line** rather than by
                # exempting this file in `check_secrets.RULE_PATH_EXEMPTIONS`,
                # so the rest of this file stays readable by that rule.
                "fileAeskey": "01bbda3d34aac6def0f9551979a7055e",  # noqa: S105 - vendor sample
                "fileBigHttpUrl": "https://imunion.weixin.qq.com/cgi-bin/x?param=v1_9a25",
                "fileBigSize": 254,
                "fileMd5": "a1aeb5166748cb66189c733e9b68f4a9",
                "fileName": "",
                "imageHasHd": False,
            },
        }
    )

    assert content is not None
    assert content["payload_ref"] == "https://imunion.weixin.qq.com/cgi-bin/x?param=v1_9a25"
    # The checksum is the fallback, not the answer: it identifies the bytes
    # without saying where they are.
    assert content["payload_ref"] != "a1aeb5166748cb66189c733e9b68f4a9"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"msgType": 123},
        {"msgType": 123, "msgData": {}},
        {"msgType": 123, "msgData": {"content": "   "}},
        {"msgType": 999, "msgData": {"somethingNew": "x"}},
        {"msgType": "not-a-number", "msgData": {}},
        {"msgData": None},
        {"msgData": "not-a-mapping"},
    ],
)
def test_a_payload_with_nothing_usable_returns_none_and_never_raises(payload) -> None:
    """The degradation rule, stated over the shapes that could reach it.

    `None` means "no content here" and the caller decides what that is worth.
    **No branch raises** -- that is the property, and it is why these are
    parametrised rather than written as one case: one case stops at the first
    assertion and the remaining shapes would never be exercised.
    """

    assert parse_content(payload) is None


def test_an_unknown_message_type_is_accepted_for_what_it_carries() -> None:
    """`msgType` is a hint that is allowed to be wrong.

    A type nobody has enumerated, carrying a usable field, still arrives. The
    alternative -- refusing what we cannot name -- is the one-to-one map this
    batch exists to avoid, wearing a different hat.
    """

    content = parse_content({"msgType": 4242, "msgData": {"content": "hello"}})
    assert content == {"type": "text", "text": "hello"}


def test_a_flattened_payload_still_parses() -> None:
    """No nested `msgData`: the shape every existing fixture sends."""

    assert parse_content({"text": "hi"}) == {"type": "text", "text": "hi"}


def test_a_non_string_field_is_not_coerced_into_a_caption() -> None:
    """`str(value)` would store `"{'a': 1}"` as the customer's message.

    The same defect independent review found in `str(payload.get("actor_id"))`,
    where coercion accepted anything at all.
    """

    assert parse_content({"msgData": {"content": {"a": 1}}}) is None
    assert parse_content({"msgData": {"content": 12345}}) is None


def test_base64_raw_data_is_not_quietly_promoted_to_a_reference() -> None:
    """T205 / B3-4d own that field; this batch must not answer their question.

    Treating it as a `payload_ref` here would decide where it lives by
    accident, and the observation list has not yet established whether it holds
    the callback payload or the media itself.
    """

    assert parse_content({"msgType": 7, "msgData": {"base64RawData": "AAAA"}}) is None


def test_the_message_type_reader_never_raises() -> None:
    assert message_type({"msgType": 123}) == 123
    assert message_type({"msgType": " 123 "}) == 123
    assert message_type({"msgType": "abc"}) is None
    assert message_type({}) is None
    # `True` is an `int` in Python; a boolean is not a message type.
    assert message_type({"msgType": True}) is None


def test_the_enumerations_do_not_overlap() -> None:
    """Guards the guard: a value in two sets would make the hint ambiguous."""

    assert not TEXT_MSG_TYPES & IMAGE_MSG_TYPES
    assert not TEXT_MSG_TYPES & MIXED_MSG_TYPES
    assert not IMAGE_MSG_TYPES & MIXED_MSG_TYPES
    assert 123 in MIXED_MSG_TYPES


def test_a_mixed_message_still_has_words_for_the_manager_notice() -> None:
    """The half that gets lost **one layer below** this parser.

    `parse_content` hands both halves to the envelope, and the integration
    cases prove they are stored. Neither shows what the *consumer* does with
    them -- and the manager notice used to take
    `content["text"] if content["type"] == "text"`, which reads correctly right
    up until a content type carries both. A mixed message is `type: "image"`,
    so that gate returned `None` and the notice quoted nothing while the
    customer had written a sentence.

    **This is T201's own failure shape, reproduced downstream by the fix for
    it**, which is why it is asserted here next to the parser rather than left
    to whoever next reads `pipeline.py`.
    """

    from fastagent_v2.worker.pipeline import customer_words

    mixed = parse_content(MIXED_PAYLOAD)
    assert customer_words(mixed) == "44"

    # An uncaptioned picture genuinely has no words; `manual_task_notice`
    # already says only what it knows when it gets `None`.
    assert customer_words(parse_content({"msgType": 14, "msgData": {"fileId": "3068x"}})) is None
    assert customer_words(parse_content({"text": "hello"})) == "hello"
    assert customer_words(None) is None
    assert customer_words({}) is None


def test_an_unstorable_media_reference_costs_the_picture_not_the_message() -> None:
    """**P2-1.** The fifth payload string to reach `jsonb`, and the only one that
    had no boundary check.

    `external_event_id` and `text` are bounded and checked; `actor_id` (T180)
    and `nonce` degrade. The media reference went in raw -- so a NUL in it
    raised `UntranslatableCharacter` out of `accept()` and one over 2048 raised
    `ValidationError`, **each refusing the whole message, the customer's
    sentence along with the picture**. T206's rule already covers it: degrade to
    "this item is absent", keep the message.

    **The bound stopped being theoretical when the real field names arrived**: a
    personal account locates its picture with a signed `fileBigHttpUrl` several
    hundred characters long, so this is the check that decides what happens as
    those grow, not a guard against a hypothetical.
    """

    from fastagent_v2.ingress.wecom_proxy.inbound import MAX_PAYLOAD_REF

    def mixed_with(file_id: str) -> dict:
        part = {"subMsgType": 14, "subMsgData": {"fileId": file_id}}
        return {"msgType": 123, "msgData": [part, MIXED_PART_TEXT]}

    assert parse_content(mixed_with("306\x008abc")) == {"type": "text", "text": "44"}

    over_bound = "3" * (MAX_PAYLOAD_REF + 1)
    assert parse_content(mixed_with(over_bound)) == {"type": "text", "text": "44"}

    # Exactly at the bound is still fine -- an off-by-one here silently drops
    # every reference of the maximum permitted length.
    at_bound = "3" * MAX_PAYLOAD_REF
    ok = parse_content({"msgType": 14, "msgData": {"fileId": at_bound}})
    assert ok == {"type": "image", "payload_ref": at_bound}

    # Only when the degradation leaves nothing is the payload refused.
    assert parse_content({"msgType": 14, "msgData": {"fileId": "306\x008abc"}}) is None


def test_a_non_visual_attachment_is_not_labelled_a_picture() -> None:
    """**P2-2.** `inbox_events` is not rewritable, so a wrong label is permanent.

    Every media reference used to become `type: "image"`, so a voice note was
    stored as a picture. `file` is already in the envelope's `type` enum, so
    correcting this moves nothing in the frozen contract -- and ADR-0102 §三's
    second tier is defined to trigger on image types, which is what makes the
    label load-bearing rather than cosmetic.

    **The number here used to be `34`, and `34` is not a message type.** The
    assertion was right and its example was invented; the vendor document lists
    voice as `16` and has no row for `34` at all. Pinned in
    `test_inbound_payload_conformance.py` so it cannot drift back.
    """

    voice = parse_content({"msgType": 16, "msgData": {"fileId": "3068voice", "voiceTime": 3}})
    assert voice == {"type": "file", "payload_ref": "3068voice"}

    for image_type in sorted(IMAGE_MSG_TYPES | MIXED_MSG_TYPES):
        content = parse_content({"msgType": image_type, "msgData": {"fileId": "3068p"}})
        assert content is not None and content["type"] == "image", image_type

    # No declared type at all: `image` is kept, because that is what every
    # existing caller and fixture has always meant by a bare media reference.
    assert parse_content({"fileId": "3068p"}) == {"type": "image", "payload_ref": "3068p"}


def test_a_half_in_msg_data_and_a_half_at_the_top_level_both_arrive() -> None:
    """**P3-1.** The lookup used to be `msgData` *or* the top level, never both.

    So `{"text": "hello", "msgData": {"fileSize": 100}}` was refused outright
    despite carrying usable text: `msgData` existed, so the top level was never
    read. That is this module becoming the thing that drops messages.

    (The original wording of this case said `imgSize`. That field appears
    nowhere in the vendor document -- the size field is `fileSize` -- and T96's
    first observation item was pinned to the same non-existent name.)
    """

    assert parse_content({"text": "hello", "msgType": 0, "msgData": {"fileSize": 100}}) == {
        "type": "text",
        "text": "hello",
    }
    # Picture nested, caption at the top level: both halves survive.
    assert parse_content({"text": "看这个", "msgType": 14, "msgData": {"fileId": "3068p"}}) == {
        "type": "image",
        "text": "看这个",
        "payload_ref": "3068p",
    }
    # Same, across an *array* of parts: the top level is searched after every
    # part, never instead of them.
    assert parse_content({"text": "看这个", "msgType": 123, "msgData": [MIXED_PART_IMAGE]}) == {
        "type": "image",
        "text": "看这个",
        "payload_ref": MIXED_FILE_ID,
    }


def test_the_text_is_trimmed_and_blank_text_is_no_content() -> None:
    """**P3-4.** Two behaviour changes the first round did not write down.

    Both are deliberate: surrounding whitespace is not part of what a customer
    said, and a body that is only whitespace is not a message. Recorded because
    "the plain text path is unchanged" was asserted with `"hello"`, which cannot
    tell either of these apart from the old behaviour.
    """

    assert parse_content({"text": "  hello  "}) == {"type": "text", "text": "hello"}
    assert parse_content({"text": "   "}) is None

def test_a_withdrawal_is_recognised_and_carries_no_content() -> None:
    """B3-2c / ADR-0107 §二.1 -- **recognition only**.

    **This case deliberately does not claim that a 2063 cannot become content**
    (independent review P3-2 caught the earlier docstring claiming exactly
    that while asserting nothing of the kind). `parse_content` on a 2063 that
    carries a `content` field returns text quite happily, and that is fine:
    "a withdrawal never arrives as customer input" is a property of **the order
    of the branches in `accept()`**, not of this parser.

    It is asserted where it lives, in
    `tests/integration/test_inbound_content_arrives.py::
    test_a_2063_carrying_text_still_does_not_become_a_message` -- which goes red
    if the branch is moved below the content rule, as a reverse probe confirmed.
    """

    from fastagent_v2.ingress.wecom_proxy.callback import classify
    from fastagent_v2.ingress.wecom_proxy.inbound import (
        WITHDRAWAL_MSG_TYPE,
        withdrawn_reference,
    )

    assert WITHDRAWAL_MSG_TYPE == 2063
    # **Asked of `classify` now** (review P2-3): `is_withdrawal` was this
    # module's answer to "is this a withdrawal", and the callback-envelope batch
    # moved that decision to where an element's kind is decided once. The
    # question the case asks is unchanged.
    # `receiverId` is part of the element because the room rule reads it: a
    # callback that names no recipient is declined as a group message
    # (deliberately eager, user ruling 2026-09-10), and every real element
    # carries one.
    def _element(msg_type: object) -> dict[str, object]:
        return {
            "msgType": msg_type,
            "senderId": "c1",
            "userId": "a1",
            "receiverId": "a1",
            "fromRoomId": 0,
        }

    assert classify(_element(2063))[0] == "withdrawal"
    assert classify(_element("2063"))[0] == "withdrawal"
    assert classify(_element(123))[0] != "withdrawal"

    # The vendor's own field, and the only one looked for. The four spellings
    # this replaced (`withdrawMsgId`, `sourceMsgId`, `originMsgId`, `msgId`)
    # were all invented, so `withdrawn_reference` returned `None` for **every**
    # real withdrawal -- the audit row could never say what was taken back, and
    # nothing was ever going to go red about it.
    documented = "5982062000920770451"
    assert withdrawn_reference({"msgData": {"revokeMsgUniqueIdentifier": documented}}) == documented
    assert withdrawn_reference({"msgData": {"withdrawMsgId": "a"}}) is None
    # Absent, or present but not a string: `None` rather than an invented value.
    # The identifier is quoted in the document, and it is larger than a JSON
    # number carries exactly -- a string is what it has to be.
    assert withdrawn_reference({"msgData": {}}) is None
    assert withdrawn_reference({"msgData": {"revokeMsgUniqueIdentifier": 598206200092077}}) is None
