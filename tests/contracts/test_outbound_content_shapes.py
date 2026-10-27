"""Every content shape the platform actually produces, read by one reader.

**The 2026-09-10 outbound defect, stated as the cases that would have caught
it.** The third real window composed three customer replies, approved them,
queued them -- and sent none: three `outbox_messages` rows `FAILED` with
`unsendable_content_type` and an empty `provider_operation_id`. **The provider
was never called.** The adapter accepted exactly one content shape and the model
produced two others.

Both halves' unit tests were green throughout, and that is the point worth
keeping: the producer's tests used the deterministic inference double, which
builds `{"type": "text", "text": ...}`, and the adapter's tests used a constant
of the same shape written beside it. *Two ends of one boundary, each tested
against its own idea of the other.* Green unit tests were not evidence that the
boundary was right, and could not have been.

So the cases here are driven by `tests/fixtures/outbound-round3/commands.json`
-- the three commands as they were captured in production -- rather than by
anything composed alongside the fix. See `docs/wecom_proxy.md` and
`CLAUDE.md`'s 「跨边界的数据结构要有真实样本驱动的用例」.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.worker.channel import SendCommand, customer_text
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter

from tests.contracts.test_wecom_proxy_channel import sent

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "outbound-round3" / "commands.json"
)
CAPTURED: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["commands"]
# **不是模块级常量，而这一格是本仓踩过的。**
#
# 它原本写成 `_deadline() = datetime.now(UTC) + timedelta(seconds=30)`——
# 一个**在 import（collection）那一刻**就固定下来的时刻。整棵树跑起来时，
# 从收集到这条用例真正执行之间超过那个窗口，它就已经过期了，
# 于是被测代码走的是「时间不够」那条分支，而用例读起来像是逻辑错了。
#
# **实测（2026-09-20）**：`pytest tests/contracts/test_reconcile_before_unknown.py`
# 单跑 17 条全绿；在 `pytest_collection_finish` 里插 26 秒再跑，**12 条红**，
# 与当天一次全量门禁里红的那 12 条逐条相同。
#
# 现取即可。**一个「还没到的时刻」必须相对它被用的那一刻算，不是相对 import。**
def _deadline() -> datetime:
    """调用时现取。理由见上。"""

    return datetime.now(UTC) + timedelta(seconds=30)


def _command(content: object) -> SendCommand:
    return SendCommand(
        tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        conversation_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        content=content,
    )


def _adapter(send_text: Any) -> WeComProxyChannelAdapter:
    return WeComProxyChannelAdapter(send_text=send_text, guid="guid-1", recipient_id="wm-test-user")


def test_the_fixture_still_holds_the_three_shapes_the_window_captured() -> None:
    """A guard on the corpus itself, not on the code.

    If someone "tidies" these into one shape the rest of this file keeps
    passing while testing nothing -- which is how the defect survived in the
    first place.
    """

    shapes = [sorted(case["content"]) for case in CAPTURED]
    assert shapes.count(["text"]) == 2, "two replies carried a bare `text`"
    assert shapes.count(["segments"]) == 1, "one carried a `segments` list"
    assert not any("type" in case["content"] for case in CAPTURED), (
        "none of the three declared a content `type` -- that is the whole defect"
    )


@pytest.mark.parametrize("case", CAPTURED, ids=lambda case: f"captured-{case['n']}")
def test_each_captured_command_reaches_the_provider(case: dict[str, Any]) -> None:
    """The three that were lost. **Each one is now a call, not a refusal.**"""

    calls: list[dict[str, object]] = []

    def send_text(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return sent(1003000 + case["n"])

    receipt = _adapter(send_text).send(
        command=_command(case["content"]),
        idempotency_key=f"send_message:e{case['n']}:0",
        deadline_at=_deadline(),
    )

    assert receipt.status == "accepted", receipt.evidence
    assert receipt.provider_operation_id == str(1003000 + case["n"])
    assert len(calls) == 1, "the provider is called exactly once"
    assert calls[0]["content"], "and it is called with words, not an empty string"


@pytest.mark.parametrize("case", CAPTURED, ids=lambda case: f"captured-{case['n']}")
def test_the_words_survive_the_read(case: dict[str, Any]) -> None:
    """Not just "it sent something" -- **it sent what the model wrote.**

    Asserted against the fixture rather than a literal here: a copy of the text
    in this file would drift from the corpus and the drift would look green.
    """

    content = case["content"]
    expected = (
        content["text"]
        if "text" in content
        else "\n".join(part["content_or_ref"] for part in content["segments"])
    )
    assert customer_text(content) == expected


def test_a_material_ref_segment_refuses_the_whole_message() -> None:
    """**Fail closed, and the reason is inbound's lesson applied outbound.**

    A segment list may legally mix `text` and `material_ref`
    (`generation-result.schema.json`). A text-only channel that took the text
    half would deliver *part* of what the model composed, with nothing saying so
    -- the same silent half-loss that inbound spent a batch removing. Refusing
    the whole thing is recoverable; sending half of it is not.
    """

    content = {
        "segments": [
            {"type": "text", "content_or_ref": "这是图片说明"},
            {"type": "material_ref", "content_or_ref": "material:catalogue-page-3"},
        ]
    }

    assert customer_text(content) is None

    receipt = _adapter(lambda **_: sent()).send(
        command=_command(content), idempotency_key="send_message:e9:0", deadline_at=_deadline()
    )
    assert receipt.status == "failed"
    assert receipt.evidence["reason"] == "unsendable_content_type"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param({"type": "image", "text": "看这个"}, id="declared-non-text"),
        pytest.param({"text": "   "}, id="whitespace-only"),
        pytest.param({"text": ""}, id="empty"),
        pytest.param({"segments": []}, id="no-segments"),
        pytest.param({"segments": [{"type": "text", "content_or_ref": ""}]}, id="empty-segment"),
        pytest.param({"segments": ["not-a-part"]}, id="segment-not-an-object"),
        pytest.param({"message_type": "acknowledgement"}, id="the-T110-payload"),
        pytest.param("not-a-dict", id="not-a-dict"),
    ],
)
def test_content_with_no_words_in_it_is_still_refused(content: object) -> None:
    """**Widening what is read is not widening what is accepted.**

    The batch that taught the platform three shapes is exactly the batch that
    has to show it did not turn the check into a formality. `{"type": "image"}`
    is refused because it says it is not text; the rest are refused because
    there is nothing in them to send.
    """

    assert customer_text(content) is None


def test_two_text_segments_are_joined_with_a_newline() -> None:
    """**The separator, pinned** (independent review 2026-09-10, P3-1).

    The window's probe C changed `"\n".join(...)` to `"".join(...)` and all 29
    cases stayed green: the captured `segments` payload has one part, so nothing
    exercised the join. Two parts run together read as one sentence that the
    model never wrote -- **a loss that is neither visible nor recoverable**,
    which is why the reviewer's ruling was to keep the newline rather than
    refuse multi-part messages.

    **This case is deliberately built from a literal, and that does not violate
    `AGENTS.md`'s real-sample rule.** That rule is about the *shape* of data
    crossing a boundary. What is under test here is a rendering choice v2 makes
    on its own side -- how it joins two parts before handing one string to
    `sendText`, whose `content` is a plain string (official §2). There is no
    counterparty to agree with. **Do not "fix" this case by sourcing it from a
    fixture.**
    """

    content = {
        "segments": [
            {"type": "text", "content_or_ref": "收到您的消息。"},
            {"type": "text", "content_or_ref": "稍后回复您。"},
        ]
    }

    assert customer_text(content) == "收到您的消息。\n稍后回复您。"


def test_the_provider_s_own_success_flag_is_recorded_without_being_judged() -> None:
    """**Carried, not interpreted** (independent review 2026-09-10, suggestion).

    `isSendSuccess` is required in the official response body and this adapter
    has never read it. Mapping `0` onto a failure would be guessing -- the
    document says a failed send is reported *afterwards* by callback
    (`msgType=1011`), and this body is the *success* one. So it is recorded and
    nothing branches on it: the first real `isSendSuccess: 0` becomes something
    a person can point at instead of something nobody can prove happened.
    """

    receipt = _adapter(lambda **_: sent()).send(
        command=_command({"text": "您好"}),
        idempotency_key="send_message:e1:0",
        deadline_at=_deadline(),
    )
    assert receipt.status == "accepted"
    assert receipt.evidence["is_send_success"] == 1

    # A body without the field is not a failure to record -- it is a body that
    # did not carry it, and the receipt says so by not claiming anything.
    without = dict(sent())
    del without["isSendSuccess"]
    bare = _adapter(lambda **_: without).send(
        command=_command({"text": "您好"}),
        idempotency_key="send_message:e2:0",
        deadline_at=_deadline(),
    )
    assert "is_send_success" not in bare.evidence


def test_a_declared_text_type_is_still_honoured() -> None:
    """The canonical shape did not stop working, and it is what we now queue."""

    assert customer_text({"type": "text", "text": "您好，已经收到。"}) == "您好，已经收到。"
