"""The vendor's callback envelope, and what each element of it turns into.

**These cases exist because the first real inbound window found the code reading
one level too high.** `docs/wecom_proxy.md:41-71` has printed the shape --
`{code, msg, data:[…]}` -- since before either `adapter.py` or
`wecom_proxy/inbound.py` was written, and both read `msgType` / `msgData` /
`msgUniqueIdentifier` off the top level, where a real callback has only `code`,
`msg` and `data`. Eight authenticated callbacks were answered with eight
identical 117-byte `INVALID_CALLBACK_PAYLOAD` bodies and `inbox_events` stayed
at zero.

**Half of these cases are driven by the captured bodies themselves**
(`tests/fixtures/wecom-proxy-first-inbound/`, redacted copies of what arrived),
because a fixture written from the same misunderstanding as the code proves
nothing -- which is exactly what the previous suite did.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastagent_v2.ingress.wecom_proxy.callback import (
    DECLINED_ROOM,
    IGNORED_ECHO,
    IGNORED_NOTICE,
    OUTCOME_DECLINED,
    OUTCOME_IGNORED,
    OUTCOME_MESSAGE,
    OUTCOME_UNREADABLE,
    OUTCOME_WITHDRAWAL,
    CallbackEnvelopeError,
    classify,
    conversation_id_for,
    is_room_message,
    read_callback,
)
from fastagent_v2.ingress.wecom_proxy.inbound import (
    parse_content,
    storable_as_text,
    withdrawn_reference,
)

from tests.wecom_proxy_callbacks import ACCOUNT_ID, callback_body, callback_item

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wecom-proxy-first-inbound"

#: What each captured body is, from the observation record written while they
#: arrived (`evidence/m1b/first-inbound-2026-09-10/README.md` §二).
CAPTURED = {
    "001.json": (2, OUTCOME_MESSAGE, "[呲牙]好的"),
    "002.json": (2, OUTCOME_MESSAGE, "1111111"),
    "003.json": (2063, OUTCOME_WITHDRAWAL, None),
    "008.json": (102, OUTCOME_MESSAGE, None),
    "017.json": (2, OUTCOME_MESSAGE, None),
    "019.json": (2, OUTCOME_MESSAGE, "你好"),
    "022.json": (104, OUTCOME_MESSAGE, None),
    "023.json": (2, OUTCOME_MESSAGE, "[裂开]"),
}


#: The two fields the redaction destroyed, and the value they carried.
#:
#: **This is the one place the batch keeps an inference rather than a
#: measurement, and it is here rather than in the predicate on purpose.**
#: The capture's redactor replaces *identity-shaped fields* with stable
#: pseudonyms, and `fromRoomId` / `isRoomNotice` are on its list -- so in these
#: copies both read `<id-01>`, which is truthy, while the messages themselves
#: are plainly 1:1 (`receiverId == userId`, the hosted account).
#:
#: Both fields carry the **same** pseudonym, which means they carried the same
#: value; `isRoomNotice` is `0` in every sample in `docs/wecom_proxy.md`,
#: including its room ones. So both were `0`.
#:
#: **Restored here, and only here.** The production predicate stays strict --
#: either a non-zero `fromRoomId` or a zero `receiverId` makes a room message,
#: because misreading a room as 1:1 sends the AI's answer to a group member's
#: private chat.
#:
#: **What happens if this restoration is wrong** (independent review
#: 2026-09-10, P2-4, correcting the claim this paragraph used to make): these
#: cases stay **green** either way -- the restoration is unconditional, so they
#: always exercise the 1:1 path -- while production declines all eight real
#: messages as group messages and `inbox_events` returns to zero. That is
#: 2026-09-10 repeated with a different error code, which is the exact shape
#: this batch exists to end. The safety here is the *inference*, not the tests:
#: two keys sharing one pseudonym means one value, `isRoomNotice` is `0` in
#: every vendor sample, and the eight carry a non-zero `receiverId == userId`.
#:
#: **T243 replaces the inference with a measurement**, and it belongs before the
#: next Stage inbound window rather than before merge: one grep of the capture
#: on the Stage host, where the originals and the pseudonym map both still are.
_REDACTED_ROOM_FIELDS = ("fromRoomId", "isRoomNotice")


def _captured(name: str) -> dict[str, Any]:
    body = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    for element in body["data"]:
        for field in _REDACTED_ROOM_FIELDS:
            if isinstance(element.get(field), str) and element[field].startswith("<id-"):
                element[field] = 0
    return body


def _captured_raw(name: str) -> dict[str, Any]:
    """The fixture exactly as it sits on disk, pseudonyms and all."""

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_every_captured_callback_reads_as_one_element(name: str) -> None:
    """The shape, asserted against the bytes that were actually delivered.

    Not "an envelope we believe in": these eight are the redacted copies of the
    eight requests nginx logged on 2026-09-10, and the batch's central claim is
    that they were readable all along.
    """

    body = _captured(name)
    assert sorted(body) == ["code", "data", "msg"]
    items = read_callback(body)
    assert len(items) == 1
    assert items[0].external_event_id
    assert items[0].occurred_at is not None
    assert items[0].occurred_at.tzinfo is not None


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_each_captured_callback_is_classified_the_way_it_was_observed(name: str) -> None:
    """`msgType` decides, and the withdrawal is the one that is not a message."""

    msg_type, outcome, _ = CAPTURED[name]
    item = _captured(name)["data"][0]
    assert item["msgType"] == msg_type
    assert classify(item)[0] == outcome


@pytest.mark.parametrize(
    "name", [name for name, (_, outcome, text) in CAPTURED.items() if text]
)
def test_the_text_of_a_captured_message_is_parsed_from_the_element(name: str) -> None:
    """**The second misreading, and the one that would have survived the first.**

    `parse_content` was reading `msgData` off the top level. Fixing only the
    envelope would have left every message arriving with no content at all --
    and "no content" is a refusal, so the symptom would have been unchanged.
    """

    _, _, expected = CAPTURED[name]
    item = _captured(name)["data"][0]
    content = parse_content(item)
    assert content is not None
    assert content.get("text") == expected


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_the_top_level_of_a_captured_callback_carries_no_content_at_all(name: str) -> None:
    """The reverse of the case above, and the actual root cause.

    Handing `parse_content` the whole body -- what the code did until this batch
    -- finds nothing, because the top level is three keys and none of them is a
    message. This is the assertion that would have failed in September and did
    not exist.
    """

    assert parse_content(_captured(name)) is None


@pytest.mark.parametrize(
    "name", [name for name, (_, outcome, _) in CAPTURED.items() if outcome == OUTCOME_MESSAGE]
)
def test_every_captured_customer_message_survives_the_content_rule(name: str) -> None:
    """**The case that decides whether the first real conversation arrives.**

    `accept()` refuses an element whose content is `None` -- "a message with no
    content is not a message" (T206/T207). So it is not enough that the seven
    customer messages are *classified* right: each has to come out of
    `parse_content` with something, or it is refused exactly as it was in
    September, for a different reason.

    Two of the seven are media (`102` file, `104` GIF) and the observation record
    noted why they are the risky ones: `fileSize` is `0` on the GIF, and
    `fileAesKey` / `fileId` arrive as **empty strings rather than absent keys**.

    **What this case may not be read as saying**: the redaction replaced
    over-long values with descriptor *objects* (`{"__redacted__": …}`), so in
    these fixtures `fileHttpUrl` is not a string and the file message degrades
    to its `fileMd5` -- a branch production would not take, where the URL is a
    616-character string. The assertion is deliberately "there is content", not
    "the content is a locator": the stronger claim would be about the redaction
    rather than about the vendor.
    """

    content = parse_content(_captured(name)["data"][0])
    assert content is not None
    assert content.get("type")


def test_the_captured_withdrawal_points_at_the_captured_message_it_withdrew() -> None:
    """A cross-fixture invariant, and it was measured rather than assumed.

    ADR-0103 §五 item 6 registered "check the four spellings when the first real
    2063 arrives"; it arrived, and `revokeMsgUniqueIdentifier` holds the
    **`msgUniqueIdentifier`** of the withdrawn message -- not its `msgServerId`
    (1000855) and not its `seq` (17350622). Asserting it across two captured
    bodies is what makes this a measurement of the vendor rather than a
    restatement of our own parser.
    """

    withdrawal = _captured("003.json")["data"][0]
    withdrawn = _captured("002.json")["data"][0]
    assert withdrawn_reference(withdrawal) == withdrawn["msgUniqueIdentifier"]
    assert withdrawal["msgUniqueIdentifier"] != withdrawn["msgUniqueIdentifier"]


def test_a_v2_message_envelope_is_no_longer_a_callback() -> None:
    """**The reverse probe** (required by the user for this batch).

    The shape `accept()` used to expect was v2's own `message-envelope`. If a
    body in that shape were still readable here, every case in this file could
    pass while the real defect -- reading the wrong shape -- remained, because
    the code would simply accept both.
    """

    v2_envelope = {
        "external_event_id": "external-1",
        "conversation_id": str(uuid4()),
        "actor_id": "customer-1",
        "text": "hello",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    with pytest.raises(CallbackEnvelopeError):
        read_callback(v2_envelope)


def test_a_body_whose_data_is_not_a_list_is_not_a_callback() -> None:
    """`data` is the array. A mapping there is a different protocol."""

    with pytest.raises(CallbackEnvelopeError):
        read_callback({"code": 0, "msg": "ok", "data": {"msgType": 2}})


def test_one_request_may_carry_several_messages() -> None:
    """`data` is an array and nothing promises one element.

    The vendor's own document prints it as an array; the batch's delivery rule
    is that each element is decided on its own.
    """

    items = read_callback(
        callback_body(
            callback_item(sender_id="a", external_event_id="one"),
            callback_item(sender_id="b", external_event_id="two"),
        )
    )
    assert [item.external_event_id for item in items] == ["one", "two"]
    assert [item.outcome for item in items] == [OUTCOME_MESSAGE, OUTCOME_MESSAGE]


def test_an_empty_data_array_is_read_as_nothing_rather_than_refused() -> None:
    """A request that carries nothing is not a request that is broken.

    Refusing it would spend one of the ten consecutive failures that delete the
    callback address (ADR-0103 §二.1) over a callback that was merely
    uninteresting.
    """

    assert read_callback(callback_body()) == ()


@pytest.mark.parametrize("element", [7, {}, None, "a string", []], ids=str)
def test_an_element_with_nothing_readable_is_refused_not_called_a_group_message(
    element: object,
) -> None:
    """**Independent review 2026-09-10, P2-1.**

    `read_callback` degrades a non-object element to `{}`, and `is_room_message`
    then reads two *absent* fields: `not _is_zero(None) or _is_zero(None)` is
    `False or True`. So every malformed element came back as
    `ROOM_MESSAGE_OUT_OF_SCOPE` -- the caller was told its non-object was a
    group message, and the new `ingress_refused_total{reason=…}` series counted
    malformed payloads as group traffic, which made it unreadable as "how many
    group messages arrive".

    The case that was supposed to cover this asserted only `external_event_id
    == ""` -- true of both answers -- so nothing stood between the docstring's
    promise and the behaviour.
    """

    items = read_callback({"code": 0, "msg": "ok", "data": [element]})
    assert items[0].outcome == OUTCOME_UNREADABLE
    assert items[0].declined_reason is None
    assert items[0].external_event_id == ""


def test_an_element_that_is_not_an_object_does_not_take_the_others_down() -> None:
    """One bad element must not cost the good ones."""

    items = read_callback({"code": 0, "msg": "ok", "data": [7, callback_item()]})
    assert len(items) == 2
    assert items[0].outcome == OUTCOME_UNREADABLE
    assert items[1].outcome == OUTCOME_MESSAGE


@pytest.mark.parametrize(
    ("field", "value"),
    [("cmd", 11016), ("cmd", 20000), ("cmd", 15500), ("msgType", 1002), ("msgType", 2313)],
)
def test_a_notice_is_ignored_rather_than_treated_as_something_a_customer_said(
    field: str, value: int
) -> None:
    """Account status, async API answers, group and contact notices.

    Writing one to `inbox_events` runs understanding, planning and generation
    over it -- the platform would reply to "someone joined the group".
    """

    outcome, reason = classify(callback_item(**{field: value}))
    assert (outcome, reason) == (OUTCOME_IGNORED, IGNORED_NOTICE)


def test_the_call_ended_notice_is_a_notice_even_though_it_arrives_as_an_ordinary_message() -> None:
    """`docs/wecom_proxy.md` files `40` under "(属于15000，VX普通消息)".

    A classification that trusted `cmd` alone would read it as customer input.
    """

    assert classify(callback_item(cmd=15000, msg_type=40)) == (OUTCOME_IGNORED, IGNORED_NOTICE)


def test_a_failed_send_notice_is_not_customer_input() -> None:
    """**T261.** `msgType=1011` is the provider saying one of *our* sends failed.

    `docs/wecom-proxy-message-api.md` §1: the failure is reported as `1011`, with
    the reason as a suffix on `msgUniqueIdentifier` -- `..._blacklist` (the
    customer blocked this account) or `spam_block_...` (risk control). **It
    arrives after the send endpoint already answered 200**, so nothing on the
    outbound side sees it.

    Classified as a customer message it lands in the fail-open branch. **What
    happens next has no sample behind it** (independent review P2-3): the
    document puts this receipt's information in the `msgUniqueIdentifier`
    suffix and says nothing about `msgData`, so the turn either ran the model
    and composed a reply to a delivery failure, or `accept()` refused it as an
    empty payload. **Neither is the handling it deserves.**

    **T261 后半 2026-09-17 到期，所以这条断言翻了面。** 上一版写着
    「the code names the vendor's whole 「操作提示」 bucket」——那时它对：
    分辨被拒的发送与普通灰字提示要读标识符，而当时没读。

    现在读了（`send_failure_marker`），所以带标记的那些落 `SEND_FAILED`，
    **而不带标记的 `1011` 仍然是 `OPERATION_HINT_NOTICE`** ——
    桶没有被子集取代，只是从里面分出了一类。下面两条断言分别钉这两半。
    """

    from fastagent_v2.ingress.wecom_proxy.callback import (
        IGNORED_OPERATION_HINT,
        IGNORED_SEND_FAILED,
    )

    blocked = callback_item(msg_type=1011, external_event_id="abcd_blacklist")
    assert classify(blocked) == (OUTCOME_IGNORED, IGNORED_SEND_FAILED)
    # **另一半：没带标记的 1011 照旧是灰字提示。**
    # 少了这一条，把 `send_failure_marker` 写成「一律回真」也能让上面那条过，
    # 而那会把每一条灰字提示都记成发送失败 —— 那个计数正是用来判断
    # 「要不要有人去看」的。
    plain = callback_item(msg_type=1011, external_event_id="abcd")
    assert classify(plain) == (OUTCOME_IGNORED, IGNORED_OPERATION_HINT)


def test_the_failed_send_notice_is_decided_before_the_cmd_is_read() -> None:
    """**The `cmd` it arrives under has never been observed** (T261).

    The message-module document names the `msgType` and not the `cmd`, and the
    two plausible carriers lead to different answers: under `20000` it would
    already be ignored -- but with the generic notice reason, invisible among
    group-membership churn -- and under `15000` it would fall through to
    "customer message". Deciding on the `msgType` first gives the same answer
    either way.
    """

    from fastagent_v2.ingress.wecom_proxy.callback import IGNORED_OPERATION_HINT

    for cmd in (15000, 20000):
        item = callback_item(msg_type=1011, cmd=cmd)
        assert classify(item) == (OUTCOME_IGNORED, IGNORED_OPERATION_HINT), f"cmd={cmd}"


def test_this_branch_is_the_only_thing_standing_between_1011_and_the_pipeline() -> None:
    """**Stated as the absence it depends on** (T261).

    `1011` is in none of the notice sets, and `classify`'s last branch is
    deliberately fail-*open* -- an unlisted `msgType` is treated as something a
    customer said, which is the right default for a vendor that keeps adding
    types. Both halves of that remain true; this case pins the fact that the
    new branch, and nothing else, is what keeps `1011` out.

    If someone deletes the branch believing a notice set covers it, this is the
    case that says otherwise.
    """

    from fastagent_v2.ingress.wecom_proxy.callback import (
        NOTICE_CMDS,
        NOTICE_MSG_TYPES,
        OPERATION_HINT_MSG_TYPE,
    )

    assert OPERATION_HINT_MSG_TYPE not in NOTICE_MSG_TYPES
    assert OPERATION_HINT_MSG_TYPE not in NOTICE_CMDS

    # And the fail-open default is intact: an unlisted type is still customer
    # input, so the branch above is a named exception rather than a new policy.
    assert classify(callback_item(msg_type=9999))[0] == OUTCOME_MESSAGE


def test_both_documented_failure_shapes_are_recognised_one_prefix_one_suffix() -> None:
    """**T261 后半到期了**，所以这条从「今天不读」翻成了「读了哪两种」。

    上一版的名字是 `..._and_is_not_read_today`，理由是「能承载这个区分的
    投递回执还不存在，等出站真的开始发之后那一批」。出站现在真的在发了。

    ## 官方给的两个样本**形状不同**，而这正是这条用例的重点

    `docs/wecom-proxy-message-api.md` §1「发送失败」逐字：

        CAQQ5efEyQYYop2UgJWAgAMgjI+2zQk=_blacklist
        spam_block_CIGABBDm+8nJBhjYofCnkoCAAyCMrcnOBQ==

    **一个在后、一个在前。** 而本模块此前的注释（以及这条用例的上一版）
    把它概括成「the reason as a **suffix** on `msgUniqueIdentifier`」——
    那句话对第二个样本是错的。**只按后缀剥的实现会漏掉 `spam_block_` 那一整类**，
    而漏掉的方向是「被风控拦下的消息数不出来」。

    ## 仍然不做的那一件

    两种今天落**同一个**原因码 `SEND_FAILED` —— 分辨「被这个客户拉黑」与
    「被风控拒了」要一条能承载它的投递记录，而那要先有一条真实样本
    证明标识符剩下那一段就是我们发出去时拿到的那个。
    **文档从没说过它们相等**（T261 第三段）。
    """

    from fastagent_v2.ingress.wecom_proxy.callback import (
        IGNORED_SEND_FAILED,
        send_failure_marker,
    )

    blacklist = callback_item(msg_type=1011, external_event_id="s4V-Di9rTz_blacklist")
    spam = callback_item(msg_type=1011, external_event_id="spam_block_s4V-Di9rTz")

    assert classify(blacklist) == (OUTCOME_IGNORED, IGNORED_SEND_FAILED)
    assert classify(spam) == (OUTCOME_IGNORED, IGNORED_SEND_FAILED)

    # **两种都认得** —— 而这一条是分开写的：上面两条即使 `send_failure_marker`
    # 一律回真也会过，这一条要它**答得出是哪一个标记**。
    assert send_failure_marker("s4V-Di9rTz_blacklist") == "blacklist"
    assert send_failure_marker("spam_block_s4V-Di9rTz") == "spam_block"
    assert send_failure_marker("s4V-Di9rTz") is None

    # **区分那一半仍然没做，而这是有意的**：两种落同一个原因码。
    assert classify(spam) == classify(blacklist)

def test_the_send_failure_reason_joins_both_bounded_vocabularies() -> None:
    """**Both tuples, because one of them is the one that matters**
    (independent review 2026-09-10, P1-1).

    This case used to assert membership of `IGNORED_REASONS` only, and its
    docstring claimed "a reason code that is not in the tuple cannot be
    recorded". **That was false for this very batch**: `IGNORED_REASONS` has no
    reader, and the tuple `record_answer` actually draws from is
    `adapter.INGRESS_ANSWERED_REASONS` -- which did not list this code. The row
    would have been written with a label no vocabulary declared, which is the
    cardinality failure ADR-0101 §三.1 forbids.

    The systemic half of the fix is in `test_ingress_answer_vocabulary`; this
    case pins the specific code at both ends.
    """

    from fastagent_v2.ingress.adapter import INGRESS_ANSWERED_REASONS
    from fastagent_v2.ingress.wecom_proxy.callback import IGNORED_OPERATION_HINT, IGNORED_REASONS

    assert IGNORED_OPERATION_HINT in IGNORED_REASONS
    assert IGNORED_OPERATION_HINT in INGRESS_ANSWERED_REASONS


def test_a_failed_send_notice_from_our_own_sender_id_is_still_a_failure_notice() -> None:
    """**The ordering this batch depends on, pinned** (independent review P2-3).

    A receipt about *our own* send can plausibly carry *our own* `senderId`, and
    the echo rule (`senderId == userId` -> `SENT_BY_THIS_ACCOUNT`) sits below
    this branch. Every other new case here uses the default customer sender, so
    the reviewer's probe could move the branch past the echo check and only one
    case went red -- and that one for an unrelated reason (`cmd`).

    If somebody later restores `classify()`'s original "cmd first" ordering,
    this is what says a `1011` from our own account must still read as a send
    failure and not as an echo.
    """

    from fastagent_v2.ingress.wecom_proxy.callback import IGNORED_OPERATION_HINT

    own = callback_item(msg_type=1011, sender_id=ACCOUNT_ID, account_id=ACCOUNT_ID)
    assert classify(own) == (OUTCOME_IGNORED, IGNORED_OPERATION_HINT)


@pytest.mark.parametrize(
    ("msg_type", "what"),
    [(2001, "已读"), (2005, "未读"), (2118, "群个微成员自己退群")],
)
def test_a_read_receipt_is_a_notice_and_not_something_a_customer_said(
    msg_type: int, what: str
) -> None:
    """**The same defect `1011` was fixed for, found two doors down**
    (independent review 2026-09-10, P1-3).

    `docs/wecom_proxy.md`'s own `msgType` table lists `2001 消息已读通知` and
    `2005 消息未读通知`, and the document prints a full `2001` sample. Neither
    was in `NOTICE_MSG_TYPES`: the original list was read off the document's
    **section headings**, and these two appear only in the table. A reviewer's
    probe fed the whole table through `classify()` and both came back as
    `customer_message`.

    Milder than `1011` -- these carry no readable content, so downstream would
    most likely have refused them as empty payloads -- but refusing them lands
    in `ingress_refused{reason="INVALID_CALLBACK_PAYLOAD"}`, which reads as "our
    parser is broken" rather than "a read receipt arrived".

    **`2118` joined at the re-review** (P2-1), from the *third* listing in the
    same document -- the notification `<table>`, which neither earlier pass had
    read. It is milder still: a group notice usually arrives with a
    `fromRoomId` and gets stopped by the room rule first. It is here because the
    reconciliation is only worth something when it is complete, and the
    reviewer's probe walked all 27 rows to find this was the last one.
    """

    assert classify(callback_item(msg_type=msg_type)) == (OUTCOME_IGNORED, IGNORED_NOTICE), what


def test_our_own_outbound_coming_back_is_not_customer_input() -> None:
    """`senderId == userId` is this account speaking, echoed to its own callback.

    Feeding it to the pipeline makes the platform answer itself, and the answer
    is another message on the same address.
    """

    echo = callback_item(sender_id=ACCOUNT_ID, account_id=ACCOUNT_ID)
    assert classify(echo) == (OUTCOME_IGNORED, IGNORED_ECHO)


@pytest.mark.parametrize(
    "room",
    [
        pytest.param({"fromRoomId": 10791082136095292, "receiverId": 0}, id="both-signals"),
        pytest.param({"fromRoomId": 10791082136095292}, id="from-room-id-alone"),
        pytest.param({"receiverId": 0}, id="no-recipient-alone"),
    ],
)
def test_either_room_signal_is_enough_to_decline(room: dict[str, object]) -> None:
    """**Either signal, because the two misreadings do not cost the same.**

    A 1:1 message misread as a room message loses one message, loudly: refused,
    audited, and the customer's next message arrives normally. A room message
    misread as 1:1 keys a contact on a group member's personal id and then
    delivers the AI's reply to that member **privately** -- something said in a
    group answered in a private chat, which is a visible incident at a real
    merchant. So the predicate is deliberately eager (user ruling 2026-09-10).
    """

    item = callback_item(**room)  # type: ignore[arg-type]
    assert is_room_message(item) is True
    assert classify(item) == (OUTCOME_DECLINED, DECLINED_ROOM)


def test_a_one_to_one_message_is_not_declined() -> None:
    """The other direction, so the eager predicate cannot swallow everything."""

    ordinary = callback_item(sender_id="cust-1", account_id=ACCOUNT_ID)
    assert ordinary["fromRoomId"] == 0
    assert ordinary["receiverId"] == ACCOUNT_ID
    assert is_room_message(ordinary) is False
    assert classify(ordinary)[0] == OUTCOME_MESSAGE


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_no_captured_callback_is_declined_as_a_room_message(name: str) -> None:
    """The bytes that actually arrived are 1:1 and must stay handled.

    Reads through `_captured`, which restores the two room fields the redaction
    replaced with pseudonyms -- the single inference this batch still carries,
    documented at that function and registered as T243.
    """

    assert is_room_message(_captured(name)["data"][0]) is False


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_the_fixtures_room_fields_are_pseudonyms_rather_than_values(name: str) -> None:
    """**The guard on the inference itself.**

    If a later capture arrives with real values in these fields, or the
    redactor stops touching them, `_captured`'s restoration silently starts
    overwriting *data* rather than repairing a pseudonym. This fails at that
    moment instead.
    """

    element = _captured_raw(name)["data"][0]
    assert isinstance(element["fromRoomId"], str)
    assert element["fromRoomId"].startswith("<id-")
    assert element["fromRoomId"] == element["isRoomNotice"], (
        "the two fields share a pseudonym, which is what says they shared a value"
    )


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_the_captured_msg_type_is_a_number_and_not_a_word(name: str) -> None:
    """**A v1 survey said otherwise, and the bytes decide** (2026-09-10).

    The v1 system recorded raw `msgType` values as strings (`"image"`) and as
    numbers the *callback* document never lists (`1011`, `2001`). If our channel
    spoke that dialect, the classification in `callback.py` -- which is keyed on
    the numeric table in `docs/wecom_proxy.md` -- would be reading a vocabulary it
    does not have.

    It does not: every one of the eight callbacks captured on 2026-09-10 carries
    `msgType` as a **JSON integer** under `cmd: 15000`, exactly as the document
    prints it.

    **The sentence that used to close this paragraph was wrong, and the first
    rewrite of it was still wrong** (2026-09-10; T261, then independent review
    P1-3). It originally said v1's sightings of `1011` and `2001` "belong to an
    older or different ingest path". Both halves are false, for different
    reasons:

    * **`1011`** is listed by the **message-module** document
      (`docs/wecom-proxy-message-api.md` §1) -- it is how a **failed send** is
      reported. The *callback* document does not list it, which is what the
      first rewrite said; that much was right;
    * **`2001`** is listed by the **callback document itself**, in its `msgType`
      table (`2001 消息已读通知`, `2005 消息未读通知`), and the document prints a
      full `2001` sample that `inbound.py` already cites. Calling it "never
      listed" was simply wrong.

    **v1 was seeing real traffic in both cases.** What the dismissal cost is
    recorded in `test_a_failed_send_notice_is_not_customer_input` and
    `test_a_read_receipt_is_a_notice_and_not_something_a_customer_said`.

    The claim this case still makes is the narrow one it can prove: the eight
    captured callbacks carry integer `msgType` under `cmd: 15000`.

    Kept as a case rather than a note because it is the assertion that would go
    red if the vendor ever switched dialects on us -- which is the day the
    classification needs re-reading rather than the day it silently stops
    matching.
    """

    element = _captured(name)["data"][0]
    assert isinstance(element["msgType"], int)
    assert not isinstance(element["msgType"], bool)
    assert isinstance(element["cmd"], int)
    assert element["cmd"] == 15000


def test_the_message_id_is_the_only_dedupe_key_and_msg_server_id_is_not() -> None:
    """**v1 keys on `msgServerId or msgUniqueIdentifier`; we do not** (2026-09-10).

    A short-circuit between two ids makes two independent dedupe tracks with no
    stable mapping between them -- and the vendor's own samples show why it
    cannot work: the notice types (`11016`, `2313`, `2188`, `2104`, `2160`,
    `2161`, `2185`) all carry `msgServerId: 0`, so every one of them would
    collapse onto a single key.

    This platform reads `msgUniqueIdentifier` and nothing else. Two elements
    sharing a `msgServerId` are two events.
    """

    first, second = read_callback(
        callback_body(
            callback_item(external_event_id="one", msgServerId=1000852),
            callback_item(external_event_id="two", msgServerId=1000852),
        )
    )
    assert (first.external_event_id, second.external_event_id) == ("one", "two")

    # ...and a zero `msgServerId` does not erase the identity either.
    zeroed = read_callback(
        callback_body(callback_item(external_event_id="three", msgServerId=0))
    )[0]
    assert zeroed.external_event_id == "three"


def test_an_echo_this_criterion_cannot_see_is_a_known_limit_not_a_silent_one() -> None:
    """**The echo rule is the weaker of the two available, knowingly** (T247).

    v1 compares the sender against the binding's configured account id *and*
    looks the message up in its sent-message table by external id. That is
    stronger -- it recognises an echo whose `userId` we would not have matched --
    and it has a race with no protection: it assumes the send response is
    committed before the callback arrives.

    Taking it here means reading the outbound side from the ingress and
    answering that race, which is a batch rather than a branch. Until then this
    is the exact shape of what gets through: an echo whose `userId` is absent.
    Written as a case so the limit is a fact somebody can find, not a sentence
    in a commit message.
    """

    echo = callback_item(sender_id=ACCOUNT_ID, account_id=ACCOUNT_ID)
    assert classify(echo) == (OUTCOME_IGNORED, IGNORED_ECHO)

    unseen = callback_item(sender_id=ACCOUNT_ID, account_id=ACCOUNT_ID)
    del unseen["userId"]
    assert classify(unseen)[0] == OUTCOME_MESSAGE, (
        "the known limit: with no userId to compare against, an echo reads as customer input"
    )


def test_the_conversation_id_is_derived_from_the_customer_not_from_the_account() -> None:
    """Keying on our own account would put every customer into one conversation."""

    tenant_id = uuid4()
    first = conversation_id_for(tenant_id=tenant_id, config_id="config-a", peer_id="cust-1")
    second = conversation_id_for(tenant_id=tenant_id, config_id="config-a", peer_id="cust-2")
    assert first != second
    assert isinstance(first, UUID)


def test_the_same_customer_always_lands_in_the_same_conversation() -> None:
    """Idempotent by construction, which is what makes redelivery safe.

    The vendor backfills: the first real window delivered three messages whose
    `seq` was *lower* than the live ones, last. A derivation that depended on
    arrival order, or on a lookup racing another delivery, would have minted a
    second conversation for the same customer.
    """

    tenant_id = uuid4()
    again = [
        conversation_id_for(tenant_id=tenant_id, config_id="config-a", peer_id="cust-1")
        for _ in range(3)
    ]
    assert len(set(again)) == 1
    other_binding = conversation_id_for(
        tenant_id=tenant_id, config_id="config-b", peer_id="cust-1"
    )
    assert other_binding != again[0]


def test_the_occurred_at_of_a_captured_callback_is_its_vendor_timestamp() -> None:
    """`timestamp`, vendor seconds, read as UTC -- not the moment we received it.

    Arrival order is not occurrence order on this channel, so the field has to
    come from the payload or it is not answering the question.
    """

    item = _captured("001.json")["data"][0]
    parsed = read_callback(_captured("001.json"))[0]
    assert parsed.occurred_at == datetime.fromtimestamp(item["timestamp"], tz=UTC)


def test_a_callback_with_no_readable_timestamp_has_none() -> None:
    """The refusal belongs to `accept()`; this layer reports what it read."""

    assert read_callback(callback_body(callback_item(timestamp="not-a-number")))[
        0
    ].occurred_at is None


@pytest.mark.parametrize("zero", [0, "0"])
def test_the_vendors_zero_is_nobody_rather_than_the_identity_zero(zero: object) -> None:
    """**A production bug this batch's own conversion found**, kept red-able here.

    `0` is how this vendor spells "none": `fromRoomId: 0` is "not a room",
    `receiverId: 0` is "no recipient", and an account-status callback carries
    `senderId: 0` outright. Read as a string it becomes the identity `"0"` --
    and then every unidentified sender shares one `contacts` row (ADR-0048 K11's
    "customer who never stops talking") and, since 2026-09-10, one conversation
    they can all read.

    It surfaced because the case that feeds `actor_id` seven junk shapes kept
    `0` among them: harmless while the field was a v2 string, a real identity
    the moment the payload became the vendor's.
    """

    parsed = read_callback(callback_body(callback_item(senderId=zero)))[0]
    assert parsed.sender_id == ""


def test_a_sender_the_database_cannot_store_costs_the_identity_not_the_request() -> None:
    """**The second production bug the conversion found**, and it was a `500`.

    The sender is the conversation key now, and `uuid5` encodes its name as
    UTF-8 -- so a lone surrogate raised `UnicodeEncodeError` out of `accept()`
    itself: a framework `500`, a redelivery from the vendor, and ten of those
    delete the callback address (ADR-0103 §二.1). T180's rule is that an
    unstorable value costs the *identity* and never the message.

    Under the v2 envelope this could not happen -- the surrogate went to
    `actor_id`, which `storable_as_text` already refused, and nothing else read
    it. The bug was created by giving the same field a second reader.
    """

    surrogate = "customer\ud800-1"
    parsed = read_callback(callback_body(callback_item(sender_id=surrogate)))[0]
    assert parsed.sender_id == ""
    # And the derivation it feeds is reachable rather than raising.
    assert conversation_id_for(
        tenant_id=uuid4(), config_id="config-a", peer_id="anonymous:evt-1"
    )


def test_the_anonymous_fallback_cannot_carry_an_unstorable_id_into_uuid5() -> None:
    """**Independent review 2026-09-10, P1-1** -- the same rule's third entrance.

    When the sender is unidentifiable the conversation is keyed on the *message*
    id instead, and `uuid5` encodes its name as UTF-8. `_peer_id` had already
    been fixed to reject an unstorable `senderId`; `external_event_id` reaches
    the same call and was checked only *afterwards*, so a payload with no usable
    sender and a surrogate in `msgUniqueIdentifier` raised `UnicodeEncodeError`
    out of `accept()` -- a framework 500, a vendor redelivery, and ten of those
    delete the callback address.

    The case that was meant to guard the fallback passed a clean literal
    (`"anonymous:evt-1"`), so this path had no coverage at all.

    Asserted at the layer that answers it: the id reads as unstorable, so
    `accept()` refuses the element before the derivation can run. That ordering
    is now explicit in `_accept_item`.
    """

    surrogate = "evt-" + chr(0xD800) + "-bad"
    item = callback_item(external_event_id=surrogate)
    del item["senderId"]
    parsed = read_callback(callback_body(item))[0]
    assert parsed.sender_id == "", "precondition: nobody usable to key on"
    assert not storable_as_text(parsed.external_event_id), (
        "the fixture has to carry an id the database cannot store, or this proves nothing"
    )


def test_the_replay_key_is_the_message_not_the_delivery() -> None:
    """**Independent review 2026-09-10, P1-2.**

    The first version of this batch keyed replay detection on `requestId`, "the
    vendor's per-delivery identifier". The vendor's own document falsifies that:
    three pairs of *different* messages share one `requestId`
    (`docs/wecom_proxy.md` 675/737, 1235/1299, 2055/2115 -- different `msgType`,
    `msgServerId`, `seq` and `msgUniqueIdentifier`).

    The cost was not a duplicate but a **loss**: the second message hit the
    "same key, another event id" branch, which raises
    `IngressAuthenticationError`, and the per-element loop deliberately did not
    catch it -- so the whole request aborted, every later element was dropped,
    and the vendor does not redeliver a `200`.

    This pins the shape that made it possible: two elements of one callback may
    share a `requestId`, and nothing about their identity is shared.
    """

    shared = "one-delivery-id"
    first, second = read_callback(
        callback_body(
            callback_item(external_event_id="msg-A", request_id=shared),
            callback_item(external_event_id="msg-B", request_id=shared),
        )
    )
    assert first.request_id == second.request_id == shared
    assert first.external_event_id != second.external_event_id


def test_a_sender_id_arrives_as_a_number_and_is_still_an_identity() -> None:
    """**The vendor sends ids as JSON numbers**, past 2**53.

    An `isinstance(str)` test on `senderId` would make every real customer
    anonymous -- and an anonymous customer gets no contact row at all.
    """

    parsed = read_callback(callback_body(callback_item(senderId=1688852365307991)))[0]
    assert parsed.sender_id == "1688852365307991"
