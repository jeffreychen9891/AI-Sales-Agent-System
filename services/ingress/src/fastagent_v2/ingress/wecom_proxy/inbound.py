"""Turn a channel callback's `msgType`/`msgData` into envelope content.

**Field-driven, not `msgType`-driven.** ADR-0103 §四 makes that a design
premise rather than a caution: the same `msgType` carries different fields
depending on where the message came from -- a picture that arrived from a
corporate WeChat account has no `content` and locates its bytes with `fileId`,
while the same picture from a personal WeChat account has no `fileId` at all
and locates them with `fileBigHttpUrl`. A one-to-one `msgType -> typed content`
map reads more naturally and **breaks on the first short variant**, and an
exception at the inbound boundary is a lost message plus redelivery into the
same crash -- ten of which delete the callback address (ADR-0103 §二.1).

So `msgType` is read as a **hint that is allowed to be wrong**, and what
actually decides the content is which fields arrived.

## Every field name here comes from the vendor document

`docs/wecom_proxy.md` (the vendor's callback reference, in the repository since
2026-09-08) is the authority for every spelling in this module, and
`CLAUDE.md` requires it be read before any WeComProxy work. **It was not
available when this module was first written**, and three of the names in that
first version turned out to be invented: `filePath` and `fileUrl` occur **zero
times** in the whole document, and the four spellings tried for a withdrawal's
target were all wrong. The batch's own gates were green throughout, because the
cases were written from the same invented shapes as the code. `msgType` values,
media keys and the withdrawal reference are now pinned to the document by
`tests/contracts/test_inbound_payload_conformance.py`, which reads it rather
than trusting this comment.

## Why this file sits next to the T180 degradation rules

T206 requires it in as many words: the rule for "a value the database cannot
hold" and the rule for "a field the payload did not carry" have to be written
together, because they are the same rule -- *degrade to "this item is absent",
keep the message* -- and split across two files the next person reads one half.
`adapter.py` holds the first half (`_storable_as_text`, and the two fields that
may not degrade); this module holds the second, and each points at the other.

## The one thing that is refused rather than degraded

**Content that would be empty.** Not "no text" -- an image with no caption is a
perfectly good message, and that is new: before this batch `text` was
unconditionally required, because text was the only content there was. What
cannot happen is an envelope carrying *nothing*: no text and no media
reference. That is not a message, and it is the analogue of `adapter.py`'s rule
that an event with no id has no idempotency key.

**`msgType` is deliberately not part of that decision.** An unknown or absent
`msgType` whose payload still carries a usable field is accepted for what it
carries; that is the whole point of reading fields rather than types.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, NamedTuple

#: `message-envelope.schema.json` bounds `content.payload_ref` at 2048.
#: **Repeated here for the same reason `adapter.py` repeats its two lengths**:
#: the schema is checked after the envelope is built, and a `ValidationError`
#: there is not a `ValueError`, so it escaped `accept()` as a framework 500.
MAX_PAYLOAD_REF = 2048

#: The values behind the names, from the vendor's `msgType` table
#: (`docs/wecom_proxy.md`, 普通消息类型-总览).
#:
#: **These are hints, not a dispatch table.** They exist so that a payload can
#: be cross-checked against what it claims to be, and so that
#: `msgType=123` -- the mixed text+image type that is high-frequency in a sales
#: conversation and that inbound did not previously recognise at all -- has a
#: name here. Nothing in this module refuses a payload for disagreeing with
#: them.
#:
#: `IMAGE_MSG_TYPES` is 一般图片 (`7`/`14`/`101`) plus Gif (`29`/`104`), which
#: the table lists as separate rows and this module treats alike: both are
#: things a customer looks at. Voice (`16`), video (`22`/`23`/`103`) and file
#: (`15`/`20`/`102`) are deliberately **not** here -- see `parse_content`, where
#: the absence is what stops a voice note being labelled a picture.
TEXT_MSG_TYPES = frozenset({0, 2})
IMAGE_MSG_TYPES = frozenset({7, 14, 101, 29, 104})
MIXED_MSG_TYPES = frozenset({123})

#: The customer took a message back. **The one type this module dispatches on**,
#: and the exception proves the rule rather than breaking it: everything above
#: is a hint about *what content a payload carries*, and content is read from
#: fields. This one is not about content at all -- it says the payload is not a
#: customer message in the first place, and no combination of fields can tell
#: you that. ADR-0107 §二.1 requires inbound to know it, and requires that
#: knowing it never turn into treating it as new customer input.
WITHDRAWAL_MSG_TYPE = 2063

#: Where a caption or a message body may be found. Ordered: the first key that
#: carries a usable string wins. `content` is the vendor's spelling; `text` is
#: ours, and is what every pre-vendor fixture and the normalised payload that
#: reaches `accept()` still use.
TEXT_KEYS = ("content", "text")

#: Where a 2063 payload names the message it withdraws.
#:
#: **`revokeMsgUniqueIdentifier`, and it is no longer a guess.** The first
#: version of this module tried `withdrawMsgId` / `sourceMsgId` / `originMsgId`
#: / `msgId` and said so in as many words -- "plausible spellings rather than an
#: observed contract". All four were wrong, so `withdrawn_reference()` would
#: have returned `None` for **every** real withdrawal and the audit row would
#: never have said what was taken back. ADR-0103 §五 item 6 registered exactly
#: that risk with the expiry condition "check the four names when the first real
#: 2063 arrives"; the vendor document arrived first, which is stronger evidence
#: than one message, and the guesses are deleted rather than kept as fallbacks
#: -- a fallback list would leave the question permanently half-open.
#:
#: The same `msgData` also carries `revokeTime` and `revokeUserId` (when, and
#: by whom). Neither is read here: the audit row's job is to point at the
#: withdrawn message, and widening what it captures is a change to ADR-0107's
#: evidence shape rather than a correction to it.
WITHDRAWN_REFERENCE_KEYS = ("revokeMsgUniqueIdentifier",)

#: Where a reference to the media may be found, **locators before checksums**.
#:
#: The order is the vendor's two dialects, not a preference:
#:
#: * `fileId` is how a **corporate** WeChat message (`14` image, `23` video,
#:   `15` file, `16` voice, and the image half of `123`) names its bytes;
#: * `fileHttpUrl` / `fileBigHttpUrl` / `fileMiddleHttpUrl` /
#:   `fileThumbHttpUrl` are how a **personal** WeChat message names them
#:   (`101` image, `102` file, `103` video, `29` Gif). **A personal-account
#:   image carries no `fileId` whatsoever** -- taking only `fileId` and
#:   `fileMd5` would have degraded every one of them to a checksum, and the
#:   customers M1b actually talks to are personal accounts;
#: * `fileMd5` is the last resort -- it identifies the bytes without locating
#:   them, which is still better than dropping the fact that a picture was sent.
#:
#: The three image sizes are tried largest first, because a thumbnail is the
#: worst usable answer and the vendor sends all three together.
MEDIA_KEYS = (
    "fileId",
    "fileHttpUrl",
    "fileBigHttpUrl",
    "fileMiddleHttpUrl",
    "fileThumbHttpUrl",
    "fileMd5",
)

#: Fields that sit right next to the ones above and are **deliberately not**
#: read. `MEDIA_KEYS` is an allowlist, so nothing here can be picked up by
#: accident; this tuple exists so that the *reason* is written down, and
#: `test_inbound_payload_conformance.py` asserts the two never overlap.
#:
#: * `base64RawData` -- **B3-4d decided where it lives, and it is not here**
#:   (user ruling 2026-09-09): `inbox_events.provider_raw_data`, written by
#:   `adapter.py` through `retained_raw_payload()` below and read by nothing
#:   (T205, migration `047`). It stays out of `MEDIA_KEYS` for the reason it was
#:   always out of them -- it is not a locator, and promoting it to a
#:   `payload_ref` would put an unread blob in the one structure the model and
#:   the message page both read. The vendor document narrows what it holds
#:   without settling it: it rides on the *envelope* beside `msgData` and is 12
#:   base64 characters on a read-receipt, so it is not the media bytes
#:   (ADR-0103 §五 item 4, still open).
#: * `fileAuthkey`, `fileAeskey`, **`fileAesKey`** -- credentials for fetching
#:   or decrypting the media, not references to it. They have no business in
#:   `inbox_events.content`.
#:
#:   **The last two are one field spelled two ways.** 普通消息 writes
#:   `fileAeskey`; API 异步消息 (`cmd=20000`) writes `fileAesKey`. Both are
#:   listed because this module's lookups are case-sensitive, so a check that
#:   knew only one spelling would let the other past. It costs nothing today --
#:   `MEDIA_KEYS` is a positive allowlist, so an unlisted name is ignored either
#:   way -- and it is written down because the next person writing a *negative*
#:   check over these names would not think to look for a second capitalisation.
#: * `fileKey` -- an opaque handle appearing **only** on `cmd=20000`
#:   (API 异步消息), a callback family this module never parses. Its meaning is
#:   undocumented, and a `Key`-shaped value of unknown purpose is not something
#:   to start storing on a guess.
#:
#: **The last three were found by the test, not by reading** (independent review
#: 2026-09-08, P3-1): `test_every_locator_shaped_key_in_the_document_is_
#: accounted_for` walks the document instead of this tuple, and these were
#: nobody's business until it asked.
_EXCLUDED_FROM_MEDIA = (
    "base64RawData",
    "fileAuthkey",
    "fileAeskey",
    "fileAesKey",
    "fileKey",
)


class _Source(NamedTuple):
    """One mapping that may carry content, and how its text is encoded.

    `encoded_text` is **positional, not type-driven**: it says "this mapping is
    a `subMsgData` inside an array-of-parts", and the vendor document shows
    text in that position base64-encoded (`"content": "NDQ="` -> `44`) while
    text in a plain `msgData` is not (`"content": "@全*X  aaa"`). Reading it
    off the *position* rather than off `msgType` keeps the module's premise
    intact -- the declared type is still allowed to be wrong.
    """

    fields: dict[str, Any]
    encoded_text: bool


def storable_as_text(value: str) -> bool:
    """Can PostgreSQL hold this string in a `text` column at all?

    **Lives here, and `adapter.py` imports it**, because T206 asks for the two
    halves of one rule to sit together: "a value the database cannot hold" and
    "a field the payload did not carry" have the same consequence and the same
    handling. Keeping a second copy in `adapter.py` would be the drift this
    module exists to argue against.

    Two ways a `str` cannot be stored: a **NUL byte**, which `text` has no
    representation for, and a **lone surrogate**, which is a valid `str` but not
    encodable as UTF-8.
    """

    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _usable_string(value: Any) -> str | None:
    """A trimmed non-empty string, or `None`.

    Anything that is not a string is **not** coerced. `str(value)` would turn
    a dict into `"{'a': 1}"` and store it as a caption -- the same defect
    independent review found in `str(payload.get("actor_id"))`, where coercion
    accepted anything at all.
    """

    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def decode_part_text(raw: str) -> str:
    """Text taken from a sub-message part, base64-decoded when it decodes.

    The vendor's only worked example of a `123` part is `"content": "NDQ="`,
    which is base64 for `44` -- a perfectly ordinary thing for a customer to
    type, and not something anyone types as `NDQ=`. **Storing the undecoded
    string would put base64 in front of the model and in the manager's notice.**

    **It is one sample, so this decodes defensively rather than trusting it.**
    A string that is not valid base64, or whose bytes are not valid UTF-8, is
    returned unchanged; only a clean round trip to text replaces it. That makes
    a wrong guess cost nothing on payloads that turn out to be plain, and it
    cannot lose the message either way -- the fallback is the raw string, never
    `None`, because dropping text here would recreate the very loss this module
    exists to prevent.

    The residual risk is a *plain* string that happens to be valid base64 and
    to decode to valid UTF-8; it is registered as an observation item rather
    than argued away. Chinese text cannot hit it (base64's alphabet is ASCII),
    which is most of the traffic M1b expects.
    """

    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return raw
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return _usable_string(text) or raw


def _first_present(
    sources: tuple[_Source, ...],
    keys: tuple[str, ...],
    *,
    decode_text: bool = False,
) -> str | None:
    """The first usable value for `keys`, searched across `sources` in order.

    Takes several sources rather than one so the parts of an array, a nested
    `msgData` and the top level can each be consulted **per half**: a payload
    may carry its picture in one part and its caption in another, or its
    picture in `msgData` and its caption at the top level, and reading only one
    of them loses whichever half lives in the other.
    """

    for source in sources:
        for key in keys:
            found = _usable_string(source.fields.get(key))
            if found is None:
                continue
            if decode_text and source.encoded_text:
                return decode_part_text(found)
            return found
    return None


def message_sources(payload: dict[str, Any]) -> tuple[_Source, ...]:
    """Every mapping in this payload that may carry content, in search order.

    Three shapes, and the middle one is why this replaced a function that
    returned a single mapping:

    * **`msgData` is a list** -- the vendor's `msgType=123` is an *array of
      parts*, each `{"subMsgType": ..., "subMsgData": {...}}`, with the picture
      in one part and the words in another. The previous version handled only a
      mapping, so a real mixed message matched neither half, fell back to the
      top level, found nothing, and was refused whole by `accept()`. **T201
      exists to stop half a message being lost, and the code shipped for it lost
      all of it** -- which no gate caught, because every case was written from
      the same invented shape as the code.
    * **`msgData` is a mapping** -- every single-part type.
    * **anything else** (absent, `null`, a scalar) -- the top level alone.

    The top level is always searched last, never instead: that was independent
    review's P3-1, where `{"text": ..., "msgData": {...}}` was refused despite
    carrying usable text because `msgData` existed.
    """

    sources: list[_Source] = []
    nested = payload.get("msgData")
    if isinstance(nested, list):
        for part in nested:
            if not isinstance(part, dict):
                continue
            fields = part.get("subMsgData")
            if isinstance(fields, dict):
                sources.append(_Source(fields, encoded_text=True))
    elif isinstance(nested, dict):
        sources.append(_Source(nested, encoded_text=False))
    sources.append(_Source(payload, encoded_text=False))
    return tuple(sources)


def message_type(payload: dict[str, Any]) -> int | None:
    """The declared `msgType`, or `None` when it is absent or unreadable.

    Never raises: an unparseable `msgType` is a hint we do not have, not a
    reason to lose the message.
    """

    raw = payload.get("msgType")
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


def parse_content(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Envelope `content` for this callback, or `None` if it carries nothing.

    `None` means "there is no content here", and the caller decides what that
    is worth. It is not an error signal: no branch of this function raises.

    The mixed case is the one T201 registered. A `msgType=123` message carries
    a sentence *and* a picture, and both have to arrive -- "either only the
    text or only the image, and neither loss reports itself". It becomes **one**
    envelope whose `type` is `image` and whose `text` holds the caption (user
    ruling, 2026-09-08). The envelope contract already permits that pair:
    `content` requires only `type`, and `text` and `payload_ref` may coexist.
    So this needs no contract change, and it keeps one callback equal to one
    event -- splitting it in two would mean inventing a second
    `external_event_id` and taking the idempotency path with it.
    """

    sources = message_sources(payload)
    text = _first_present(sources, TEXT_KEYS, decode_text=True)
    media = _first_present(sources, MEDIA_KEYS)

    # **A media reference that cannot be stored is dropped, not fatal**
    # (independent review 2026-09-08, P2-1). It was the fifth string to come
    # from the payload and reach `jsonb`, and the only one with no check at the
    # boundary: a reference carrying a NUL raised `UntranslatableCharacter` out
    # of `accept()`, and one longer than the contract's 2048 raised
    # `ValidationError` -- **both refusing the whole message, the customer's
    # sentence along with the picture**. That is the exact shape this batch
    # argues against ("a batch that stops half a message being lost turning into
    # one that loses all of it"), and T206's rule already names the handling:
    # degrade to "this item is absent", keep the message.
    #
    # The bound is not theoretical now that the real keys are known: a personal
    # account's `fileBigHttpUrl` is a signed URL several hundred characters
    # long, so this is the check that decides what happens as they grow.
    if media is not None and not (storable_as_text(media) and len(media) <= MAX_PAYLOAD_REF):
        media = None

    if media is not None:
        # **`type` follows the declared `msgType`, and only for labelling**
        # (P2-2). Whether content is *taken* is still decided by which fields
        # arrived; this decides only what to call it. Calling every media
        # reference `image` marked a voice note as a picture, and `inbox_events`
        # is not rewritable -- a wrong label written today cannot be corrected
        # later, and ADR-0102 §三's second tier is defined to trigger on image
        # types. `file` is already in the envelope's `type` enum, so nothing in
        # the frozen contract moves.
        kind = message_type(payload)
        looks_visual = kind is None or kind in IMAGE_MSG_TYPES | MIXED_MSG_TYPES
        media_type = "image" if looks_visual else "file"
        if text is not None:
            return {"type": media_type, "text": text, "payload_ref": media}
        return {"type": media_type, "payload_ref": media}
    if text is not None:
        return {"type": "text", "text": text}
    return None


#: **`is_withdrawal()` was deleted here** (independent review 2026-09-10,
#: P2-3). It answered "is this a withdrawal" for `accept()`, and the
#: callback-envelope batch moved that question into `wecom_proxy.callback.
#: classify`, which decides *what an element is* once, before any field is read.
#: That left this function with tests as its only callers -- the definition of
#: dead code in `AGENTS.md`'s zero-caller check, with a live replacement and no
#: drawing naming it. ADR-0107's requirement is unchanged and is met by
#: `classify`'s `WITHDRAWAL_MSG_TYPE` branch.


def withdrawn_reference(payload: dict[str, Any]) -> str | None:
    """Which message was taken back, when the payload says so.

    Reads `msgData.revokeMsgUniqueIdentifier`, which is the vendor's own name
    for it -- see `WITHDRAWN_REFERENCE_KEYS` for what it replaced and why the
    four guesses are gone rather than kept as fallbacks.

    **What the value points *at* is not established** (independent review
    2026-09-08, P1-2). The name is authoritative; the join is not. The document
    says only `// 撤回消息id` in an inline comment, and the shapes argue against
    the obvious reading: the sample is a **19-digit decimal**
    (`"5982062000920770451"`), while **none of the 32 `msgUniqueIdentifier`
    values in the document is decimal at all** -- they look like
    `s4V-Di9rTz-et8N`, `37b9d5e6db0f99c7549747973026a134`,
    `CAMQ0uHkxgYYACCklNg==`. The 2063 example carries its *own*
    `msgUniqueIdentifier` in that non-decimal shape, so the two fields are
    plainly not drawn from one space.

    So this returns **the identifier the vendor gave, unresolved**, and stores
    it under its own key in `evidence`. It is never merged into a field that
    claims to be a resolved event id -- which is what makes "we do not yet know
    what it joins to" survivable rather than a defect. Registered as ADR-0103
    §五 item 8; B3-2c's criterion and T218 both come due on that join.

    **`None` when the payload does not say so**, which is a real outcome and
    not a failure: an audit row saying "a withdrawal arrived and it did not say
    what for" is worth strictly more than no row. Whatever is found is stored
    under its own key in `evidence`, never merged into a field that claims to be
    the resolved event id.

    **A non-string value also yields `None`**, and that is deliberate: the
    document quotes the identifier (`"5982062000920770451"`), and it exceeds
    what a JSON number carries exactly, so a string is what it has to be.
    Coercing whatever arrives is the defect independent review found in
    `str(payload.get("actor_id"))`.
    """

    # Searched per half, like every other lookup here (independent review
    # P3-1): a withdrawal may name its target at the top level while carrying
    # a `msgData` for something else, and reading only one source loses it.
    return _first_present(message_sources(payload), WITHDRAWN_REFERENCE_KEYS)


#: The key B3-4d retains, and the one field in this module that is stored
#: **without being read** (T205, ADR-0103 §三).
RAW_PAYLOAD_KEY = "base64RawData"


def retained_raw_payload(payload: dict[str, Any]) -> str | None:
    """The provider's `base64RawData`, verbatim, or `None`.

    **This is not content and it is not a locator.** It stays out of
    `MEDIA_KEYS` for the reason `_EXCLUDED_FROM_MEDIA` records, and it does not
    reach the envelope at all: it is written to `inbox_events.provider_raw_data`
    by `adapter.py` and read by nothing (`047`'s column comment says so; a
    contract test holds the repository to it). What it is *for* is the case
    where this module turns out to have read a field wrong -- then this is the
    only way back to what actually arrived.

    **The top level only, deliberately.** The vendor document puts the field on
    the outer envelope, beside `msgData` rather than inside it (ADR-0103 §五
    item 4, from the `msgType=2001` example where it is 12 base64 characters).
    Searching `message_sources()` the way content lookups do would invent a
    second location for it and quietly prefer whichever arrived -- and since
    nothing reads the result, nobody would ever notice the choice being made.

    Three ways this yields `None`, and each is a real outcome rather than a
    failure:

    * the callback carried no such field -- most of them do not;
    * it carried something that is not a string. No coercion, for the reason
      `_usable_string` gives: `str(value)` would store `"{'a': 1}"` as though
      the provider had sent it;
    * it carried a string PostgreSQL cannot hold in a `text` column (a NUL
      byte, a lone surrogate). **This degrades rather than refusing** -- the
      same call `auth_evidence.nonce` makes, and for the same reason: this is a
      copy kept for evidence, and losing a customer's message to protect a
      field nobody reads would be the worst trade in this module (T207 / T223).

    **Not trimmed.** `_usable_string` strips whitespace because a caption with
    surrounding blanks is the same caption; here the point is to keep what
    arrived, byte for byte, so trimming would defeat the only purpose the value
    has. An empty string still becomes `None` -- "the field was there and held
    nothing" and "the field was not there" are not worth separate storage when
    nothing reads either.
    """

    value = payload.get(RAW_PAYLOAD_KEY)
    if not isinstance(value, str) or not value:
        return None
    return value if storable_as_text(value) else None
