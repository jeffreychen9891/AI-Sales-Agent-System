"""The vendor's callback envelope, translated into what `accept()` can use.

**Why this module exists.** Until this batch `accept()` read `external_event_id`
/ `conversation_id` / `occurred_at` off the top level of the callback body --
**fields of v2's own `message-envelope`**, which no WeComProxy callback has ever
carried. The real body is

    {"code": 0, "msg": "ok", "data": [ {...}, {...} ]}

and every field the platform needs lives inside an element of `data`. The first
real inbound window (2026-09-10, eight callbacks) therefore ended with eight
identical 117-byte `INVALID_CALLBACK_PAYLOAD` refusals and `inbox_events` still
at zero, while nginx recorded eight authenticated `200`s.

**The shape was never missing.** `docs/wecom_proxy.md:41-71` prints it verbatim,
and that document is the one `CLAUDE.md` requires anyone touching WeComProxy to
read before starting. `adapter.py` and `wecom_proxy/inbound.py` were written
before that rule existed, so the rule bound every later batch and never sent
anyone back to re-check the older code. **The gap was not a missing document; it
was an implementation reading one level too high**, and what finally found it
was a real message on a real host.

**The second misreading was the same one.** `inbound.py`'s `message_type` /
`parse_content` / `retained_raw_payload` read `msgType` / `msgData` /
`base64RawData` off the top level too -- correct for an *element*, and the top
level only has `code` / `msg` / `data`. They are not changed by this batch:
feeding them the element makes their assumption true, which is why this module
translates rather than patching each reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from fastagent_v2.ingress.wecom_proxy.inbound import (
    WITHDRAWAL_MSG_TYPE,
    message_sources,
    message_type,
    storable_as_text,
)

#: Namespace for the derived conversation id (user ruling, 2026-09-10).
#:
#: A callback names two people and a message; it does not name a *conversation*,
#: because the vendor has no such concept -- so v2 has to derive one, and the
#: derivation has to survive redelivery, restart and reseeding. `uuid5` over the
#: same triple `contacts` is keyed on (ADR-0048 K11) gives that for free: no
#: lookup, no new state, no chance of two concurrent callbacks minting two
#: conversations for one customer.
#:
#: **What it costs**: a conversation that is ever *closed* would be reopened
#: under the same id by the customer's next message. Nothing in this repository
#: writes `lifecycle = 'closed'` today (the only readers filter on it), so the
#: cost is zero right now and becomes real on the day something does -- which is
#: the expiry condition registered with this ruling.
CONVERSATION_NAMESPACE = UUID("6e2f1c4a-1d3b-4f77-9a1e-8c5f0b2d7a41")

#: Vendor `cmd` values that are never a customer message.
#:
#: * `11016` -- the hosted account logged in, logged out, was kicked;
#: * `20000` -- the answer to an API call we made;
#: * `15500` -- the vendor's own "VX 系统消息" bucket: contact renamed,
#:   blacklisted, tag added, friend request.
#:
#: `15000` (VX 普通消息) is the only bucket a customer speaks through.
NOTICE_CMDS = frozenset({11016, 20000, 15500})

#: `msgType` values the vendor documents as notices rather than messages.
#:
#: **`docs/wecom_proxy.md` lists message types in THREE places, and this set
#: was reconciled against them one at a time, across three rounds of review.**
#: Writing that history down because it is the whole lesson:
#:
#: 1. **section headings** -- the original source. `2001` / `2005` are not
#:    headings, so they were missing;
#: 2. **the `msgType` table** -- added at independent review P1-3 after a probe
#:    fed the table through `classify()` and found `2001` / `2005` coming back
#:    as `customer_message`;
#: 3. **the notification `<table>`** (`:249-495`) -- added at the re-review,
#:    which found `2118` still missing **and** found that this table lists
#:    `1011` as 「操作提示」, contradicting a sentence this module had by then
#:    rewritten twice.
#:
#: **The previous version of this comment said "headings and the table" and
#: claimed the reconciliation was done.** It was not: a third listing existed
#: the whole time and it contained this module's own subject. **If a fourth
#: listing appears, reconcile against it before claiming coverage** -- and note
#: that "I read the document" has now been wrong three times in a row.
#:
#: They are listed here even though most arrive under a `NOTICE_CMDS` value,
#: because the document contradicts that grouping in at least one place: the
#: call-ended notice (`40` / `503` / `2166`) is annotated "(属于15000，VX普通
#: 消息)". A classification that trusted `cmd` alone would file it as something a
#: customer said.
NOTICE_MSG_TYPES = frozenset(
    {
        # 群 (room) lifecycle
        1001,  # 群名变更
        1002,  # 新增群成员
        1003,  # 移除群成员
        1005,  # 群成员自己退群
        1006,  # 群新增
        1022,  # 转让群主
        1023,  # 群解散
        1029,  # 群成员邀请其他人进群申请
        1043,  # 群管理员变动
        # 消息回执（**表里有、标题里没有**，见上）
        2001,  # 消息已读通知
        2005,  # 消息未读通知
        # 群信息变动（**同一张通知表，第三次对账才补上**——见 docstring）
        2118,  # 群个微成员自己退群
        # 联系人与标签
        2104,  # 免打扰/置顶
        2115,  # 联系人标记
        2131,  # 外部联系人信息变动或删除
        2132,  # 好友申请
        2160,  # 聊天标签变动
        2161,  # 聊天标签中的联系人变动
        2185,  # 企业标签新增或删除
        2186,  # 个人标签新增或删除
        2188,  # 内部联系人信息变动
        2313,  # 外部联系人加入黑名单
        2357,  # 好友申请
        # 会话与通话
        2002,  # 删除聊天
        2055,  # 清空聊天记录
        40,  # 语音、视频通话结束
        503,  # 语音、视频通话
        2166,  # 语音、视频通话
        # 朋友圈
        517,  # 朋友圈推送
        2215,  # 朋友圈变动
    }
)

#: The customer took a message back (ADR-0107). Kept as its own outcome because
#: it is neither a notice we ignore nor input we act on: it is recorded.
#:
#: **Imported rather than repeated** (independent review 2026-09-10, P3-4):
#: `inbound.py` has held this number since B3-2b and still reads it for
#: `withdrawn_reference`'s neighbours. Two spellings of one `msgType` is the
#: drift this module exists to prevent elsewhere.

#: What one element of `data` turned out to be.
OUTCOME_MESSAGE = "customer_message"
OUTCOME_WITHDRAWAL = "withdrawal"
OUTCOME_IGNORED = "ignored"
#: **A well-formed message we decline to handle**, which is not the same as a
#: notice we ignore and not the same as a payload we could not read. Today the
#: only member is a group message (user ruling, 2026-09-10): ignoring one
#: silently would leave a merchant with no trace of a customer message that
#: never got an answer, while accepting one delivers the AI's reply privately to
#: a group member. It is refused, out loud, with a reason.
OUTCOME_DECLINED = "declined"
#: **An element with nothing readable in it.** Not a notice, not a declined
#: message, not a payload whose *values* we refused -- there are no values. It
#: is refused by `accept()` like any unreadable element, and it is a separate
#: outcome so that it cannot be counted as any of the three above.
OUTCOME_UNREADABLE = "unreadable"

#: **The provider's 「操作提示」 bucket** -- the grey small-print line a chat
#: window shows -- and **a failed send of ours is one kind of it** (T261).
#:
#: **Two documents, and they do not disagree** (independent re-review
#: 2026-09-10, P1-1). `docs/wecom_proxy.md`'s notification table lists
#: `1011 操作提示(如聊天窗口灰小字体消息)`; `docs/wecom-proxy-message-api.md` §1
#: says a failed send is reported as `msgType = 1011` with the reason as a
#: suffix on `msgUniqueIdentifier` (`..._blacklist`, `spam_block_...`), arriving
#: **after the send endpoint already answered 200**. The second is a subset of
#: the first.
#:
#: **So this code is deliberately named for the bucket, not for the subset**
#: (user's ruling 2026-09-10). It used to be `SEND_FAILED_NOTICE`, which read
#: as "our message was blocked" while actually counting every grey-print hint.
#: The `ingress_answered_total{reason}` series exists to be the one hedge
#: against this classification having **zero real samples** -- a series that
#: over-counts on its first reading is worse than no series.
#:
#: **Narrowing it needs the suffix**, which this module deliberately does not
#: read (`test_the_failure_reason_rides_in_the_identifier_and_is_not_read_today`).
#: That is T261's second half, and this is now one of its reasons to come due.
#:
#: **Until this constant existed, `1011` was not in any of the notice sets and
#: `classify`'s last branch is deliberately fail-*open*** -- so a system notice
#: saying "your message was blocked" was classified as **a customer message**.
#:
#: **What happened next depended on something no sample can tell us yet**
#: (independent review 2026-09-10, P2-3): the document puts this receipt's
#: information in the `msgUniqueIdentifier` suffix and says nothing about
#: `msgData`. If it carries readable content, the turn ran understanding,
#: planning and generation and the platform composed a reply **to a delivery
#: failure**; if it does not, `accept()` refused it as an empty payload and it
#: landed in `ingress_refused{reason="INVALID_CALLBACK_PAYLOAD"}` -- reading as
#: "our parser is broken" rather than "a send was blocked".
#:
#: **Neither is the handling it deserves**, which is what this constant fixes.
#: The stronger claim is not made here because there is no sample behind it.
#:
#: **`test_the_captured_msg_type_is_a_number_and_not_a_word` was written when
#: the callback document did not list `1011`** and concluded v1's sightings of
#: it "belong to an older or different ingest path". The message-module document
#: lists it. v1 was seeing real delivery failures.
OPERATION_HINT_MSG_TYPE = 1011

#: Why an element was ignored. Bounded for the same reason
#: `INGRESS_REFUSED_REASONS` is: these become an audit row's `reason_code`, and
#: a new branch has to be named here before it can be recorded.
IGNORED_NOTICE = "NOTICE_NOT_A_CUSTOMER_MESSAGE"
IGNORED_ECHO = "SENT_BY_THIS_ACCOUNT"
#: **Its own code, not folded into `IGNORED_NOTICE`.** The audit row's
#: `reason_code` is what the Stage collector counts by, so a distinct code makes
#: this class of callback countable; folded into the generic notice code it
#: would sit alongside group-membership churn and be invisible.
#:
#: **Named for what it actually covers** (user's ruling 2026-09-10, re-review
#: P1-1): the vendor's 「操作提示」 bucket. See `SEND_FAILURE_MSG_TYPE` for why
#: the earlier name `SEND_FAILED_NOTICE` was a claim this code cannot support.
IGNORED_OPERATION_HINT = "OPERATION_HINT_NOTICE"

#: **`cmd=20000` is not one shape** (ADR-0126 §三, T272).
#:
#: The vendor's media fetch answers the `/cloud/…Async` call synchronously with
#: a `requestId` and then pushes the result back here. Three specifications show
#: `msgData: {cloudUrl}`; the message-callback document's own `20000` sample
#: shows `msgData: {fileAesKey, fileId, fileKey, fileMd5, fileSize,
#: fileThumbSize}` instead. **One `cmd`, at least two structures.**
#:
#: **This is the shape of risk that cost three rounds of real traffic** -- one
#: identifier covering more than one structure while the code knows only one
#: (out-bound, 2026-09-10, 0 messages delivered). So the discrimination is
#: positive on both sides and **fails back to today's behaviour** when it
#: recognises neither: an unknown `20000` stays a generic notice rather than
#: being filed as whichever of the two the code happened to test first.
MEDIA_ASYNC_CMD = 20000

#: Field names that identify each `cmd=20000` structure.
#:
#: **Not "the full field list" -- the fields that make the shape what it is.**
#: The vendor's samples are examples, not schemas, so requiring every listed key
#: would refuse a real callback for being one field short. The test is `any()`,
#: so any one of these is enough to tell the two structures apart.
#:
#: **`fileSize` is left out for a reason this comment used to state backwards**
#: (independent review 2026-09-11, P3-3). The first version said it was excluded
#: because the document records it absent or zero -- but the field the document
#: annotates 「可能无此字段」 is **`fileThumbSize`**, which is *in* the list, and
#: `fileSize` carries no such note. The real reason is narrower and has nothing
#: to do with the document: **a zero `fileSize` is falsy, so `.get()` cannot tell
#: "sent as 0" from "not sent at all"** -- and `msgType=104` sends exactly zero
#: (`docs/wecom-proxy-cdn-download-api.md` §4.5). A discriminator that reads the
#: same for a present field and a missing one is not a discriminator.
#:
#: `fileThumbSize` stays despite its 「可能无此字段」 note precisely because the
#: test is `any()`: a sample that omits it still matches on the others.
MEDIA_RESULT_KEY = "cloudUrl"
MEDIA_KEY_FIELDS = ("fileAesKey", "fileId", "fileKey", "fileMd5", "fileThumbSize")

#: The reason an element is declined. Same bounded-vocabulary rule.
DECLINED_ROOM = "ROOM_MESSAGE_OUT_OF_SCOPE"

#: **A `cmd=20000` carrying the address of media the vendor fetched for us.**
#:
#: Its own code rather than the generic notice, for the reason
#: `IGNORED_OPERATION_HINT` gives above: the audit row's `reason_code` is what
#: the Stage collector counts by, and folded into `IGNORED_NOTICE` this would
#: sit alongside group-membership churn and be invisible.
#:
#: **Named for the bucket, not for the handling.** Today nothing fetches the
#: address -- this batch makes the arrival visible and countable, and that is
#: all it claims. When the fetch lands, the outcome changes and this name stays
#: true; a name like `…_NOT_CONSUMED` would have to be renamed, and renaming an
#: audit vocabulary churns the collector.
#:
#: **`cloudUrl` itself never reaches this code path's audit row**: it is a
#: directly GET-able address, so `redaction.py` handles it as a credential
#: rather than as a URL (ADR-0126 §五). Measured before the change: `redact()`
#: returned such an address byte-for-byte unchanged.
IGNORED_MEDIA_FETCH_RESULT = "MEDIA_FETCH_RESULT"

#: **A `cmd=20000` carrying media keys rather than an address.** The second
#: documented structure under this `cmd`; same counting argument as above.
IGNORED_MEDIA_KEY_NOTICE = "MEDIA_KEY_NOTICE"

#: **这一条 `1011` 是我们自己发出去的那条被拒了**，不是别的灰字提示。
#:
#: 与 `IGNORED_OPERATION_HINT` 分开，而不是取代它 —— `1011` 那个桶装的是
#: 「聊天窗口里的灰色小字」，**发送失败只是其中一种**（用户 2026-09-10 裁定
#: 按桶命名而不是按子集命名，理由见 `OPERATION_HINT_MSG_TYPE` 上面那段）。
#:
#: 分出来是因为这两件**商家要做的事不一样**：一条灰字提示不需要人管，
#: 而「发给这个客户的消息被拒了」要人看。此前两者共用一个计数，
#: 于是 `ingress_answered_total{reason="OPERATION_HINT"}` 涨一格，
#: **没人分得出涨的是哪一种**。
IGNORED_SEND_FAILED = "SEND_FAILED"

#: **我们自己发起的那次上传失败了。**
#:
#: 运维窗口 2026-09-17 实测：十二份全失败，而本仓**没有任何一处会红** ——
#: 入站答 200，审计行与「有人退群了」共用 `NOTICE_NOT_A_CUSTOMER_MESSAGE`，
#: 目录里那一行看上去是齐的。要查清只能抓包。
#:
#: 这个码存在的全部理由是那句话：**下一次十二份全失败时，要有一个东西涨一格。**
IGNORED_UPLOAD_FAILED = "UPLOAD_FAILED"

IGNORED_REASONS = (
    IGNORED_UPLOAD_FAILED,
    IGNORED_SEND_FAILED,
    IGNORED_NOTICE,
    IGNORED_ECHO,
    IGNORED_OPERATION_HINT,
    IGNORED_MEDIA_FETCH_RESULT,
    IGNORED_MEDIA_KEY_NOTICE,
)
DECLINED_REASONS = (DECLINED_ROOM,)


class CallbackEnvelopeError(ValueError):
    """The body is not an WeComProxy callback envelope at all."""


@dataclass(frozen=True, slots=True)
class CallbackItem:
    """One element of `data`, with the fields v2 needs read out of it."""

    #: The element itself. **`inbound.py` is given this**, not the whole body.
    item: dict[str, Any]
    #: `msgUniqueIdentifier` -- the vendor's identity for this message.
    external_event_id: str
    #: `timestamp`, vendor seconds, as an aware UTC instant.
    occurred_at: datetime | None
    #: `senderId`, the account that sent it.
    sender_id: str
    #: `userId`, the hosted account this callback belongs to.
    account_id: str
    #: `requestId` -- kept as evidence, deliberately **not** the replay key.
    request_id: str
    #: What this element is: one of the three `OUTCOME_*` values.
    outcome: str
    #: Set only when `outcome == OUTCOME_IGNORED`; one of `IGNORED_REASONS`.
    ignored_reason: str | None
    #: Set only when `outcome == OUTCOME_DECLINED`; one of `DECLINED_REASONS`.
    declined_reason: str | None


def _usable_id(value: Any) -> str:
    """A vendor id as a string, or `""` when it is absent or unusable.

    The vendor sends ids as **JSON numbers** (`"senderId": 1688852365307991`),
    which is why this is not a `str` check: `str()` of the number is the
    identity, and `16888...` is well past `2**53` so a float would already have
    lost digits by the time it reached here. `bool` is excluded because `True`
    is an `int` in Python and an identity of `"True"` is not an identity.
    """

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _peer_id(value: Any) -> str:
    """A participant id, or `""` when there is nobody usable to name.

    Three ways a value is nobody, and the third is the one that bites:

    * **absent, or a type that is not an id** (`_usable_id`);
    * **the vendor's `0`** -- how this protocol spells "none", the same `0` that
      means "not a room" in `fromRoomId` and "no recipient" in `receiverId`.
      Read as a string it becomes the identity `"0"`, and then every
      unidentified sender shares one `contacts` row (ADR-0048 K11's "customer
      who never stops talking") and one conversation they can all read;
    * **a string the database cannot store.** Not decoration: the sender is also
      the conversation key now, and `uuid5` encodes its name as UTF-8, so a lone
      surrogate raised `UnicodeEncodeError` **out of `accept()`** -- a `500`, a
      redelivery, and ten of those delete the callback address. T180's rule is
      that such a value costs the identity and never the message; it now holds
      on both paths that read one.

    Both of the last two were found by converting the existing suite to the
    vendor's shape, not by reading the new code: under the v2 envelope `0` was
    "a non-string" and the surrogate never reached a uuid.
    """

    identity = _usable_id(value)
    if identity == "0" or not storable_as_text(identity):
        return ""
    return identity


def _is_zero(value: Any) -> bool:
    """Whether a vendor id field means "none", which it spells as `0`."""

    return _usable_id(value) in ("", "0")


def _occurred_at(value: Any) -> datetime | None:
    """`timestamp` (vendor seconds) as an aware instant, or `None`.

    `None` rather than an exception: the caller refuses the item, and it refuses
    it in the same place it refuses a missing id, so the two failures produce
    one reason code instead of two shapes.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str):
        try:
            seconds = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


#: **`message_type` is `inbound.py`'s** (review P3-4). This module had a
#: byte-identical copy; the two would have answered the same question about the
#: same field in two places, which is the shape that drifts.


def _cmd(item: dict[str, Any]) -> int | None:
    raw = item.get("cmd")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def is_room_message(item: dict[str, Any]) -> bool:
    """Whether this element came from a group chat rather than a 1:1 one.

    **Either signal is enough**, read off the vendor's own samples in
    `docs/wecom_proxy.md`:

    * a room message carries a **non-zero `fromRoomId`**, and `"receiverId": 0`;
    * a 1:1 message carries `"fromRoomId": 0` and a **real `receiverId`** -- the
      hosted account, equal to `userId`.

    **The `or` is the ruling, and the direction is the whole point** (user,
    2026-09-10, after the v1 survey). The two misreadings do not cost the same:

    * **a 1:1 message misread as a room message** loses one message, loudly --
      it is refused, it leaves an audit row, and the customer's next message
      arrives normally;
    * **a room message misread as 1:1** walks the single-chat path to the end:
      a `contacts` row keyed on a group member's personal id, and then **the
      AI's reply delivered to that member privately**. Something said in a group
      answered in a private chat is a visible incident at a real merchant.

    v1 does not make this distinction at all. That was luck rather than design:
    its instances never joined a group.

    **A third combination exists, and the document shows it** (independent
    review 2026-09-10, P3-5): the withdrawal sample at `docs/wecom_proxy.md:2859`
    carries a non-zero `fromRoomId` *and* a real `receiverId` equal to `userId`.
    The `or` declines it, which is the safe direction -- but the two-way split
    above is not the whole world, and a reader who took it for one would think
    this predicate is complete.

    **Not `isRoomNotice`**, which is `0` in the document's *room* samples too and
    therefore says nothing about where a message came from.

    Room messages are **refused** by this batch, not silently ignored: 200 with
    a business error body (ADR-0103 §二.1), plus an audit row naming the reason.
    Supporting them needs new definitions for conversation identity, the contact
    row and the re-engagement quota -- registered as its own cell (T240).
    """

    return not _is_zero(item.get("fromRoomId")) or _is_zero(item.get("receiverId"))


#: The fields an element must have at least one of before any rule can read it.
#: Deliberately the identity/routing set rather than "any key at all": an object
#: carrying only `{"foo": 1}` is as unreadable as `7` for every purpose here.
_READABLE_FIELDS = ("msgUniqueIdentifier", "msgType", "cmd", "senderId", "timestamp")


def _has_any_readable_field(item: dict[str, Any]) -> bool:
    return any(item.get(field) is not None for field in _READABLE_FIELDS)


#: 官方为发送失败给出的**两个样本**（`docs/wecom-proxy-message-api.md` §1「发送失败」）：
#:
#:     CAQQ5efEyQYYop2UgJWAgAMgjI+2zQk=_blacklist
#:     spam_block_CIGABBDm+8nJBhjYofCnkoCAAyCMrcnOBQ==
#:
#: **一个在后、一个在前** —— 而本模块此前的注释把它概括成
#: 「the reason as a suffix on `msgUniqueIdentifier`」，那句话对第二个样本是错的。
#: 只按后缀剥的实现会**漏掉 `spam_block_` 那一整类**，而漏掉的方向是
#: 「被风控拦下的消息数不出来」。
#:
#: **词表是闭合的，只收文档里写过的这两个。** 不猜第三个：
#: 猜错的代价是把一条普通灰字提示记成发送失败，而那个计数正是用来
#: 判断「要不要有人去看」的。
SEND_FAILURE_MARKERS = ("_blacklist", "spam_block_")


def send_failure_marker(external_event_id: str) -> str | None:
    """这个标识符带着哪一个发送失败标记，没有则 `None`。

    **前后都查**：官方两个样本一个是后缀一个是前缀。

    **只回标记本身，不回剩下那一段。** 那一段看上去像我们发出去时拿到的
    `msgUniqueIdentifier`（`_OPERATION_ID_KEYS` 存进 `provider_operation_id`
    的正是它），**而文档从没说过它们相等**。按「看上去像」去关联那一行投递记录，
    就是本仓那个已经付过账的动作 —— `_OPERATION_ID_KEYS` 里那四个猜出来的键
    对每一次真实发送都回 `None`，而用例夹具是照着猜的形状写的、全绿。
    **所以这一批只分类，不关联**：关联要等一条真实样本，那是 T261 的第三段。
    """

    for marker in SEND_FAILURE_MARKERS:
        if marker.startswith("_") and external_event_id.endswith(marker):
            return marker.lstrip("_")
        if marker.endswith("_") and external_event_id.startswith(marker):
            return marker.rstrip("_")
    return None


def media_async_reason(item: dict[str, Any]) -> str | None:
    """Which documented `cmd=20000` structure this is, or `None` for neither.

    **`None` is a real answer, not a failure.** The two structures in
    `docs/wecom-proxy-cdn-download-api.md` are the two we have seen written
    down; a `20000` matching neither keeps today's behaviour (a generic notice)
    instead of being guessed into one of them. Guessing is the failure this
    whole discrimination exists to avoid -- and an unknown structure guessed as
    `MEDIA_FETCH_RESULT` would tell the next batch there is an address to fetch
    when there is not.

    **Read through `message_sources` rather than `item["msgData"]`.** That
    function already knows `msgData` comes as a mapping, as an array of
    `{subMsgType, subMsgData}` parts, or not at all -- and it knows it because
    a batch that assumed the mapping shape lost a whole real message (T201).
    Reaching into `msgData` here would be the same assumption, freshly made.

    **Two passes, not one loop with two tests**: a payload carrying both an
    address and the keys is a fetch result, wherever in it each one sits. One
    loop would let the *position* of a field decide, so the same callback with
    its parts in the other order would come back a different thing.
    """

    for source in message_sources(item):
        if source.fields.get(MEDIA_RESULT_KEY):
            return IGNORED_MEDIA_FETCH_RESULT
    for source in message_sources(item):
        if any(source.fields.get(field) for field in MEDIA_KEY_FIELDS):
            return IGNORED_MEDIA_KEY_NOTICE
    return None


def classify(item: dict[str, Any]) -> tuple[str, str | None]:
    """What this element is, as `(outcome, ignored_reason)`.

    **Order is the substance of this function**, so each step says why it comes
    where it does:

    1. **a failed send of our own, first of all** (T261). It is the one notice
       whose `cmd` we have never observed, and letting it reach the fail-open
       branch at the bottom means treating a delivery failure as customer input;
    2. **`cmd` notices next.** An account-status or async-API callback is not
       about a conversation at all, so nothing below it applies.

       **`20000` is split by structure before that** (ADR-0126 §三, 2026-09-11).
       The sentence above still holds -- a media-fetch answer is not about a
       conversation either, and it is still ignored -- but it was hiding two
       different callbacks under one reason code. `cmd` is still judged before
       anything below, and it is still judged before content; what changed is
       that *this one* `cmd` is asked which of its documented structures it is,
       so the two become countable apart. **A `20000` matching neither keeps
       exactly the old answer**, so nothing that used to be a generic notice
       stops being one;
    3. **`msgType` notices after that**, because the document files the call-ended
       notice under `cmd=15000` while calling it a notice -- `cmd` alone would
       have read it as something a customer said;
    4. **room before everything about senders**, because a room's "sender" is
       one of many and this batch does not have a definition for that;
    5. **self-sent before withdrawal**, because an echo of our own outbound is
       not the customer withdrawing anything;
    6. **withdrawal before message** (ADR-0107 §二.1): a `2063` carries no
       content of its own, so the content rule would refuse it as an empty
       payload and record "the customer took something back" as a broken
       callback;
    7. **everything else is a customer message** -- deliberately fail-*open*.
       An unlisted `msgType` reaching the content parser degrades to whatever
       fields it can read (T201 / T206) and is refused only if it carries
       nothing at all. An allowlist here would silently drop the message types
       nobody has seen yet, which is the failure T224 already registers.
    """

    if not _has_any_readable_field(item):
        # **An element we cannot read is not a group message** (independent
        # review 2026-09-10, P2-1). Without this, `data: [7]` / `[{}]` / `[null]`
        # reached `is_room_message({})`, which is `not _is_zero(None) or
        # _is_zero(None)` -- `False or True` -- and every malformed element came
        # back as `ROOM_MESSAGE_OUT_OF_SCOPE`.
        #
        # Three things were wrong with that, and only the third is cosmetic: the
        # caller was told "your non-object element is a group message"; the new
        # `ingress_refused_total{reason="ROOM_MESSAGE_OUT_OF_SCOPE"}` series
        # counted malformed payloads as group traffic, so it could never be read
        # as "how many group messages arrive"; and `read_callback`'s docstring
        # promised a refusal that did not happen.
        #
        # It comes first because every rule below reads *fields*, and this one
        # says there are none to read.
        return OUTCOME_UNREADABLE, None
    if message_type(item) == OPERATION_HINT_MSG_TYPE:
        # **发送失败那一小类先答，而它仍然是 `ignored`。**
        #
        # 分类没有变 —— 一条「你的消息被拒了」确实不是客户在说话。
        # 变的是它带着自己的原因码走，于是
        # `ingress_answered_total{reason="SEND_FAILED"}` 与灰字提示分得开。
        #
        # 判据是标识符上的标记，**不是 `msgData` 里的内容** ——
        # 文档对这类的 `msgData` 一个字都没说。
        marker = send_failure_marker(_usable_id(item.get("msgUniqueIdentifier")))
        if marker is not None:
            return OUTCOME_IGNORED, IGNORED_SEND_FAILED
        # **Before the `cmd` check, unlike every other notice** (T261). The
        # message-module document names the `msgType` and does not say which
        # `cmd` carries it, and the two candidates lead to different outcomes:
        # under `20000` it would already be ignored, but with the generic notice
        # reason; under `15000` it would fall through to "customer message".
        # Deciding on the `msgType` first gives the same answer either way, and
        # the answer keeps its own reason code.
        return OUTCOME_IGNORED, IGNORED_OPERATION_HINT
    if _cmd(item) == MEDIA_ASYNC_CMD:
        # **Inside the `cmd` step, not before it** -- `20000` is in
        # `NOTICE_CMDS` and stays there; this only asks which `20000` it is.
        # Falling through to `IGNORED_NOTICE` on `None` is what keeps the
        # unknown structure answering exactly what it answered yesterday.
        return OUTCOME_IGNORED, media_async_reason(item) or IGNORED_NOTICE
    if _cmd(item) in NOTICE_CMDS:
        return OUTCOME_IGNORED, IGNORED_NOTICE
    kind = message_type(item)
    if kind in NOTICE_MSG_TYPES:
        return OUTCOME_IGNORED, IGNORED_NOTICE
    if is_room_message(item):
        return OUTCOME_DECLINED, DECLINED_ROOM
    sender = _peer_id(item.get("senderId"))
    account = _peer_id(item.get("userId"))
    if sender and account and sender == account:
        # **Our own message, handed back to us.** The vendor's own sample of a
        # `2055` shows `senderId` equal to the hosted account with `receiverId`
        # being the other party -- an outbound-direction event on the same
        # callback address. Treating one as customer input would feed the
        # platform's own replies into its understanding step, and the reply to
        # a reply is another reply.
        #
        # **This is the weaker of the two available criteria, and it is chosen
        # knowingly** (v1 survey, 2026-09-10). v1 compares the sender against
        # the binding's configured account id *and* looks the message up in its
        # sent-message table by external id -- stronger, because it recognises
        # an echo whose `userId` we would not have matched. It also has a race
        # with no protection at all: it assumes the send response is committed
        # before the callback arrives, and the opposite order makes the echo
        # look like customer input.
        #
        # Taking that criterion here means reading the outbound side from the
        # ingress and answering the race, which is a batch rather than a branch.
        # **Registered as T247**; until then the field comparison holds, and its
        # limit is exactly: an echo whose `userId` is absent or differs is not
        # recognised as one.
        return OUTCOME_IGNORED, IGNORED_ECHO
    if kind == WITHDRAWAL_MSG_TYPE:
        return OUTCOME_WITHDRAWAL, None
    return OUTCOME_MESSAGE, None


def read_item(item: dict[str, Any]) -> CallbackItem:
    """Read one element of `data`. Never raises: unusable fields come back empty.

    The refusal belongs to `accept()`, which is where "this item cannot become
    an inbox row" is answered with a reason code and a `200` -- and where the
    other items of the same request are still processed.
    """

    outcome, reason = classify(item)
    return CallbackItem(
        item=item,
        external_event_id=_usable_id(item.get("msgUniqueIdentifier")),
        occurred_at=_occurred_at(item.get("timestamp")),
        sender_id=_peer_id(item.get("senderId")),
        account_id=_peer_id(item.get("userId")),
        request_id=_usable_id(item.get("requestId")),
        outcome=outcome,
        ignored_reason=reason if outcome == OUTCOME_IGNORED else None,
        declined_reason=reason if outcome == OUTCOME_DECLINED else None,
    )


def envelope_status(payload: dict[str, Any]) -> str | None:
    """信封上的 `code` / `msg`，拼成一句可记录的话；正常（`code` 为 0）回 `None`。

    **`read_callback` 有意只回元素，把信封丢掉** —— 一条回调里十二个元素
    共用一个信封，把信封塞进每个元素会让「这一条说的是哪一个」变糊。

    而这一件是例外：上传失败时**元素里什么都没有**（`msgData` 为 `null`），
    失败的原因只在信封上。运维 2026-09-17 抓到的那次逐字是：

        code: 500100, msg: 底层客户端业务错误[获取wxCdnInfo失败]

    所以这个函数单独取它，**由调用方决定要不要用** —— 签名不动，
    适配器手里本来就有整份 payload。

    **记原文，不映射**：这一族的错误码表供应商没给
    （`docs/wecom-proxy-cdn-upload-api.md` §2.4 登记着要问而一直没有），
    而把没见过的码映射成本仓的词是猜。
    """

    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int) or code == 0:
        return None
    message = str(payload.get("msg") or "").strip()
    rendered = f"{code} {message}".strip()
    return rendered[:200]


def read_callback(payload: dict[str, Any]) -> tuple[CallbackItem, ...]:
    """Every element of `data`, in the order the vendor sent them.

    Raises `CallbackEnvelopeError` when the body is not a callback envelope --
    **which is what the v2-envelope shape this code used to expect now hits**,
    and the reverse probe for this batch asserts exactly that.

    **An empty `data` is not an error.** It carries nothing, so nothing is
    written and the answer is a `200` -- the same treatment a request full of
    notices gets, and for the same reason: refusing it would spend one of the
    ten consecutive failures that delete the callback address (ADR-0103 §二.1)
    over a request that was merely uninteresting.

    **Elements that are not objects are dropped rather than refused whole**
    (`data: [item, 7]`): the batch's whole delivery rule is that one bad element
    must not cost the good ones. A dropped element still produces a refusal
    outcome downstream -- it comes back as an item with no id -- so it is
    counted, not silent.
    """

    if not isinstance(payload, dict):
        raise CallbackEnvelopeError("callback body is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise CallbackEnvelopeError("callback body has no data array")
    return tuple(
        read_item(element if isinstance(element, dict) else {}) for element in data
    )


def conversation_id_for(*, tenant_id: UUID, config_id: str, peer_id: str) -> UUID:
    """The conversation a message from this peer belongs to (user ruling, 2026-09-10).

    Derived, not looked up and not invented: `uuid5` over the same triple
    `contacts` is unique on, so two callbacks delivered concurrently -- which
    the vendor does, the first real window arrived with three backfilled
    messages out of `seq` order -- cannot mint two conversations for one
    customer, and a redelivery lands on the row it landed on before.

    `peer_id` is the **customer's** id (`senderId` on an inbound message), never
    the hosted account: keying on our own account would put every customer of a
    binding into one conversation.
    """

    return uuid5(CONVERSATION_NAMESPACE, f"{tenant_id}:{config_id}:{peer_id}")
