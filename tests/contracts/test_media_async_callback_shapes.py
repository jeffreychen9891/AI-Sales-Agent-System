"""`cmd=20000` is not one shape, and the envelope cannot tell them apart.

The vendor's media fetch answers `/cloud/…Async` synchronously with a
`requestId` and pushes the result back as a `cmd=20000` callback. **Two
structures are documented under that one `cmd`** (ADR-0126 §三):

* `docs/wecom-proxy-cdn-download-api.md` -- three specifications, all showing
  `msgData: {cloudUrl}`;
* `docs/wecom_proxy.md:160-193` -- the message-callback document's own `20000`
  sample, showing `msgData: {fileAesKey, fileId, fileKey, fileMd5, fileSize,
  fileThumbSize}`.

**The two worked examples carry a byte-for-byte identical envelope** -- same
`msgUniqueIdentifier`, same `seq`, same `timestamp`, `msgType: 0` on both.
`test_the_envelope_cannot_tell_the_two_shapes_apart` pins that, because it is
the reason this discrimination has to read `msgData` at all.

**This is the shape of risk that cost three rounds of real traffic**: one
identifier covering more than one structure while the code knows only one. The
out-bound batch on 2026-09-10 delivered 0 messages for exactly this reason.

---

**Tier, stated plainly** (`AGENTS.md`「跨边界的数据结构，必须有一条真实样本驱动
的用例」). The rule's first tier is a captured real sample. **There is none**:
a grep for `cloudUrl` and for `cmd`+`20000` over `evidence/` returns nothing --
no `cmd=20000` callback has ever reached this repository, because nothing has
ever called the fetch endpoint. These fixtures are therefore the **third** tier, 「权威文档的原
例，逐字抄」, the same tier `msgType=123` shipped on and the same debt it
carries (observation item 7 / **T224**).

**What that debt is, concretely**: a documented example is what the vendor says
it sends, and this project has already been wrong twice about the difference.
The first real `20000` may carry fields neither sample shows. It will not be
*mis-filed* -- an unrecognised structure keeps the generic notice code -- but it
may be filed as「既不是地址也不是密钥」when it is in fact one of them.
"""

from __future__ import annotations

import pytest
from fastagent_v2.ingress.adapter import INGRESS_ANSWERED_REASONS
from fastagent_v2.ingress.wecom_proxy.callback import (
    IGNORED_MEDIA_FETCH_RESULT,
    IGNORED_MEDIA_KEY_NOTICE,
    IGNORED_NOTICE,
    IGNORED_REASONS,
    OUTCOME_IGNORED,
    classify,
)
from fastagent_v2.runtime.redaction import REDACTED, redact

#: `docs/wecom-proxy-cdn-download-api.md:150-168`, copied field for field.
#:
#: **The four envelope fields above `customParam` were missing from the first
#: version** (independent review 2026-09-11, P3-1). `AGENTS.md`'s third tier
#: says 「逐字抄」 and **not changing the shape** is part of that; dropping
#: fields is changing it. It caused no false green -- the reviewer re-ran
#: `classify` / `redact` / `read_callback` against the full-fidelity sample
#: and got the same answers -- but **`requestId` is the key that ties this
#: callback back to the `/cloud/…Async` call that asked for it**, and the
#: reader of this fixture is the batch that has to do the tying.
CLOUD_URL_SAMPLE = {
    "TenantId": 0,
    "guid": "a3318ad6-5544-4a4f-a1bb-2aa667b2ipad",
    "userId": "16****1804",
    "requestId": "57a360fd-f920-4b4d-84c0-351ec1c63fe8",
    "customParam": "",
    "cmd": 20000,
    "msgServerId": 0,
    "msgType": 0,
    "msgUniqueIdentifier": "cf3e312fbae0f4f9a20422609a203a66",
    "senderId": 0,
    "seq": 1759127702979498,
    "timestamp": 1759127702,
    "msgData": {"cloudUrl": "https://foo.com/0485.jpg"},
}

#: `docs/wecom_proxy.md:160-193`, copied field for field. **The values are the
#: document's own masked ones** (`8e1987e*******241`): this is a fixture in a
#: file that is not on `RULE_PATH_EXEMPTIONS`, so copying an unmasked key out of
#: the vendor document would put a credential shape where the secret scan reads
#: it -- and the masked form is what the document prints here anyway.
MEDIA_KEY_SAMPLE = {
    "TenantId": 0,
    "guid": "a3318ad6-5544-4a4f-a1bb-2aa667b2ipad",
    "userId": "16****1804",
    "requestId": "57a360fd-f920-4b4d-84c0-351ec1c63fe8",
    "customParam": "",
    "cmd": 20000,
    "msgServerId": 0,
    "msgType": 0,
    "msgUniqueIdentifier": "cf3e312fbae0f4f9a20422609a203a66",
    "senderId": 0,
    "seq": 1759127702979498,
    "timestamp": 1759127702,
    "msgData": {
        "fileAesKey": "8e1987e*******241",
        "fileId": "306b020102046*******0400",
        "fileKey": "8e1***7ee-dd03-4ee2-91dc-93*******41",
        "fileMd5": "5c6ebc09*******6b9390da5",
        "fileSize": 2899681,
        "fileThumbSize": 7485,
    },
}


def test_the_download_result_gets_its_own_countable_reason() -> None:
    """**The callback the whole media-fetch grid waits for.**

    It stays ignored -- nothing fetches the address yet, and this batch claims
    nothing more than that the arrival is visible. What changes is that it is
    countable: folded into `IGNORED_NOTICE` it would sit alongside group
    membership churn, and 「有多少次代下载真的回来了」 would be unanswerable.
    """

    assert classify(CLOUD_URL_SAMPLE) == (OUTCOME_IGNORED, IGNORED_MEDIA_FETCH_RESULT)


def test_the_media_key_shape_is_a_different_answer() -> None:
    """The second documented structure, told apart by `msgData` alone."""

    assert classify(MEDIA_KEY_SAMPLE) == (OUTCOME_IGNORED, IGNORED_MEDIA_KEY_NOTICE)


def test_the_envelope_cannot_tell_the_two_shapes_apart() -> None:
    """**Why the discrimination has to read `msgData`.**

    Every field outside `msgData` is identical in the two worked examples --
    including `msgType: 0`, which is the field a reader would reach for first.
    A classification built on the envelope would answer the same thing twice.
    """

    envelope_of = lambda sample: {  # noqa: E731 - reads better than a def here
        key: value for key, value in sample.items() if key != "msgData"
    }
    assert envelope_of(CLOUD_URL_SAMPLE) == envelope_of(MEDIA_KEY_SAMPLE)
    assert classify(CLOUD_URL_SAMPLE) != classify(MEDIA_KEY_SAMPLE)


@pytest.mark.parametrize(
    ("label", "item"),
    [
        ("msgData 认不出的结构", {"cmd": 20000, "msgData": {"somethingElse": 1}}),
        ("根本没有 msgData", {"cmd": 20000, "msgType": 0}),
        ("msgData 是标量", {"cmd": 20000, "msgData": 7}),
        ("字段在但值是空的", {"cmd": 20000, "msgData": {"cloudUrl": "", "fileId": ""}}),
    ],
)
def test_an_unrecognised_20000_answers_exactly_what_it_answered_yesterday(
    label: str, item: dict[str, object]
) -> None:
    """**Falling back is the point, not a gap.**

    The two structures above are the two that are *written down*. A `20000`
    matching neither is not evidence that it is one of them, and guessing would
    tell the next batch there is an address to fetch when there is not.

    So the unknown keeps the generic notice code -- **byte-for-byte the answer
    this callback got before this batch existed** -- which is also what makes
    this change safe to land before anything consumes either new code.
    """

    assert classify(item) == (OUTCOME_IGNORED, IGNORED_NOTICE), label


def test_the_result_is_found_when_msgData_arrives_as_an_array() -> None:
    """`msgData` comes as a mapping *or* as an array of parts.

    Reading `item["msgData"]["cloudUrl"]` directly would miss the array form --
    the same assumption that lost a whole real `msgType=123` message (T201).
    This reads through `message_sources`, which already knows all three shapes.
    """

    item = {
        "cmd": 20000,
        "msgData": [{"subMsgType": 1, "subMsgData": {"cloudUrl": "https://foo.com/0485.jpg"}}],
    }
    assert classify(item) == (OUTCOME_IGNORED, IGNORED_MEDIA_FETCH_RESULT)


def test_a_callback_carrying_both_is_a_fetch_result_whatever_the_order() -> None:
    """**Position must not decide.** The address is the actionable half.

    **The two mapping cases below do not actually guard that** -- both keys sit
    in one `msgData`, and a single loop testing the address first answers the
    same thing either way. The probe said so: collapsing the two passes into one
    loop left this case **green** (probe C), which is the reverse probe doing
    the only job it has.

    The array case is the one that discriminates. `message_sources` yields one
    `_Source` per part, so a single loop would decide on **whichever part came
    first** and call a callback carrying an address a key-notice. Two passes ask
    "is there an address anywhere" before "are there keys anywhere".
    """

    keys_first = {"cmd": 20000, "msgData": {"fileId": "a", "cloudUrl": "https://foo.com/0485.jpg"}}
    url_first = {"cmd": 20000, "msgData": {"cloudUrl": "https://foo.com/0485.jpg", "fileId": "a"}}
    assert classify(keys_first) == classify(url_first)
    assert classify(keys_first) == (OUTCOME_IGNORED, IGNORED_MEDIA_FETCH_RESULT)

    keys_in_an_earlier_part = {
        "cmd": 20000,
        "msgData": [
            {"subMsgType": 1, "subMsgData": {"fileId": "a", "fileAesKey": "8e1987e*******241"}},
            {"subMsgType": 2, "subMsgData": {"cloudUrl": "https://foo.com/0485.jpg"}},
        ],
    }
    assert classify(keys_in_an_earlier_part) == (OUTCOME_IGNORED, IGNORED_MEDIA_FETCH_RESULT)


@pytest.mark.parametrize("cmd", [11016, 15500])
def test_the_other_notice_cmds_are_untouched(cmd: int) -> None:
    """This batch splits one `cmd`; the rest of `NOTICE_CMDS` answers as before."""

    assert classify({"cmd": cmd, "msgType": 0}) == (OUTCOME_IGNORED, IGNORED_NOTICE)


@pytest.mark.parametrize("reason", [IGNORED_MEDIA_FETCH_RESULT, IGNORED_MEDIA_KEY_NOTICE])
def test_both_new_codes_are_declared_where_they_can_be_counted(reason: str) -> None:
    """**Two lists, and a code has to be in both** (independent review
    2026-09-10, P1-1). `IGNORED_REASONS` is what `classify` draws from;
    `INGRESS_ANSWERED_REASONS` is what `record_answer` forwards. The first new
    ignored reason in this repository's history was in one and not the other,
    and walked straight into `audit_events` as an unbounded metric label.
    """

    assert reason in IGNORED_REASONS
    assert reason in INGRESS_ANSWERED_REASONS


def test_the_cloud_url_is_dropped_from_a_log_line_rather_than_url_redacted() -> None:
    """**`cloudUrl` is an authorisation wearing a URL's name** (ADR-0126 §五).

    It is directly GET-able and temporary, so possession of the string is the
    access. Before this batch `redact()` returned such an address **byte-for-
    byte unchanged**: `cloudurl` carries the `url` marker, so `_redact_entry`
    routed it to `redact_url`, which deliberately keeps scheme, host and path so
    an operator can still read which database a process was talking to.

    That trade is right for a DSN and wrong for this one. The DSN half is
    asserted beside it so that tightening this cannot quietly loosen that.
    """

    redacted = redact(CLOUD_URL_SAMPLE)
    assert redacted["msgData"]["cloudUrl"] == REDACTED
    assert "foo.com" not in str(redacted)

    # **值里自报家门，因为门禁抓到了它。** 第一版写的是一个普通口令，
    # `url-with-password` 当场命中——**那条规则是对的**，这一行就是它守的形状。
    # 修法按扫描器自己的失败信息：不进 allowlist，也不靠把口令缩短到六位以下
    # 去绕（那等于教人用拼接躲开秘密门禁）。`PLACEHOLDER` 的词表里有
    # `not-a-real`，而它存在的理由正是这一条：**committed 进本仓的假凭据
    # 必须在自己的值里声明它是假的**，好让 grep 到它的人不会当成真的。
    dsn = redact({"database_url": "postgresql://app:not-a-real-password@db:5432/x"})
    assert dsn["database_url"] == "postgresql://app:[REDACTED]@db:5432/x"
