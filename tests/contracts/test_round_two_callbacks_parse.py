"""Every callback of the second real window, read through the parser.

**Independent review 2026-09-10, P3-3.** The batch copied eight captured
payloads in as fixtures and then used exactly one of them. The first window's
語料 is parametrised across all eight by
`test_wecom_proxy_callback_envelope.py::CAPTURED`; the second window's new shapes
-- `msgType=104` carrying a `fileMd5`, a `2063` withdrawal, emoji mixed with
text -- were asserted only in the observation record's prose.

Prose is not a guard. These are the same eight, scanned the way the first
window's are, with what each one is taken from the record that was written while
they arrived (`evidence/m1b/first-inbound-2026-09-10/round2/README.md` §三).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastagent_v2.ingress.wecom_proxy.callback import (
    OUTCOME_MESSAGE,
    OUTCOME_WITHDRAWAL,
    classify,
    is_room_message,
    read_callback,
)
from fastagent_v2.ingress.wecom_proxy.inbound import parse_content, withdrawn_reference

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wecom-proxy-round2"

#: file -> (msgType, outcome, content type, text when it is a text message)
CAPTURED: dict[str, tuple[int, str, str | None, str | None]] = {
    "001.json": (2, OUTCOME_MESSAGE, "text", "你好吗[惊讶][憨笑][骷髅][敲打]"),
    "002.json": (2, OUTCOME_MESSAGE, "text", "ok好的👌"),
    "005.json": (104, OUTCOME_MESSAGE, "image", None),
    "008.json": (2, OUTCOME_MESSAGE, "text", "？？？"),
    "009.json": (2063, OUTCOME_WITHDRAWAL, None, None),
    "011.json": (104, OUTCOME_MESSAGE, "image", None),
    "015.json": (104, OUTCOME_MESSAGE, "image", None),
    "016.json": (2, OUTCOME_MESSAGE, "text", "[汗][流泪][鼓掌][晕]"),
}


def _element(name: str) -> dict[str, Any]:
    body = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    items = read_callback(body)
    assert len(items) == 1
    return body["data"][0]


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_each_round_two_callback_is_classified_the_way_it_was_observed(name: str) -> None:
    msg_type, outcome, _, _ = CAPTURED[name]
    element = _element(name)
    assert element["msgType"] == msg_type
    assert classify(element)[0] == outcome
    assert is_room_message(element) is False, "all eight are 1:1 messages"


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_each_round_two_customer_message_yields_content(name: str) -> None:
    """A message that parses to nothing is refused, so this is the line between
    "arrived" and "arrived and was answerable"."""

    _, outcome, content_type, text = CAPTURED[name]
    content = parse_content(_element(name))
    if outcome == OUTCOME_WITHDRAWAL:
        # A withdrawal carries no content of its own -- that is what makes it a
        # withdrawal rather than an empty message (ADR-0107).
        assert content is None or content.get("type") != "text"
        return
    assert content is not None
    assert content["type"] == content_type
    if text is not None:
        assert content["text"] == text


def test_the_round_two_withdrawal_points_at_a_message_id() -> None:
    """The second observation of the same vendor behaviour as round one."""

    reference = withdrawn_reference(_element("009.json"))
    assert reference and reference.isdigit(), (
        "the withdrawal names the withdrawn message's msgUniqueIdentifier"
    )


@pytest.mark.parametrize(
    "name", [name for name, (_, _, kind, _) in CAPTURED.items() if kind == "image"]
)
def test_a_round_two_image_degrades_to_a_locator_rather_than_being_lost(name: str) -> None:
    """**The shape round one could not show**: `msgType=104` with a `fileMd5`.

    `fileSize` is `0` on all three and `fileId` / `fileAesKey` are empty
    strings, so what is left to identify the bytes is the checksum -- and the
    message still has to arrive rather than being refused as empty.
    """

    element = _element(name)
    assert element["msgData"]["fileMd5"], "precondition: this is the round-two shape"
    content = parse_content(element)
    assert content is not None and content["type"] == "image"
    assert content.get("payload_ref"), "an image with no locator would be a lost message"
