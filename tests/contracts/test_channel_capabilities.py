"""What a channel can be **asked** is declared, and every adapter declares it.

**B2-4 / T78 / ADR-0094 §三**, which is the named revision of ADR-0042 §二.1 and
of RFC §3.5. ADR-0042 made `reconcile_by_key` a required member of the port
because a timeout leaves no operation id; T78 registered the half that reasoning
missed -- a real provider mostly will not answer a question keyed by *the
caller's* idempotency key, and an adapter that cannot ask can only return
`unknown`, which is the same word as "we asked and the provider has no record".

The evidence half of that was answered in 2026-09-05 (`_cannot_ask` sets
`query_sent: False`). This file is the declaration half: the port says so up
front, so the dispatcher does not have to call and read a receipt to find out.

**What that saves today is a local call, not a round trip** (independent review
2026-09-09, P2-1): the one adapter declaring `False` never sent a packet in the
first place. The gate is a general mechanism, and the traffic it saves belongs
to the first adapter that would go to the wire only to answer "cannot say".

The dispatcher's side of it -- not asking, and writing down that it did not --
lives in `tests/contracts/test_reconcile_before_unknown.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastagent_v2.channels.wecom_proxy import FORBIDDEN_OPERATIONS
from fastagent_v2.channels.wecom_proxy.client import RestrictedWeComProxyClient
from fastagent_v2.worker.channel import ChannelCapabilities, HttpChannelAdapter
from fastagent_v2.worker.wecom_proxy_channel import WeComProxyChannelAdapter

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Where production code lives. Test doubles are deliberately out of scope: they
#: are checked by the dispatcher refusing to run without the method, not by a
#: source scan.
PRODUCTION_ROOTS = (REPOSITORY_ROOT / "services", REPOSITORY_ROOT / "packages")


def _adapter_classes() -> dict[str, set[str]]:
    """Every production class that implements the port, found rather than listed.

    **The list is derived because a hand-written one is the failure this batch
    already saw once.** ADR-0111's group check was a constant nobody used, its
    validator was a function nobody called, and the test that should have caught
    both knew only the two names its author had thought of -- three fixes, each
    moving the same hole one step further out. A gate that names its subjects
    cannot notice a subject that did not exist when it was written.

    "Implements the port" is taken to mean "defines `reconcile_by_key`", which
    is the member ADR-0042 added and the one whose usefulness `capabilities()`
    now qualifies. A class defining it is claiming to be a channel adapter.
    """

    found: dict[str, set[str]] = {}
    for root in PRODUCTION_ROOTS:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods = {
                    item.name
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                if "reconcile_by_key" in methods:
                    found[f"{source.relative_to(REPOSITORY_ROOT).as_posix()}::{node.name}"] = (
                        methods
                    )
    return found


def test_every_adapter_in_the_tree_declares_what_it_can_be_asked() -> None:
    """No adapter gets to stay silent -- and the scan proves it found something.

    The second assertion is not decoration. A scanner whose anchor stops
    matching reports an empty set and passes, which reads exactly like "all
    adapters comply" (`AGENTS.md`, 「锚点没命中」不是「通过」). So the found set
    is checked against the two implementations we know exist by other means:
    the Protocol declares the member, and both are imported at the top of this
    file.
    """

    found = _adapter_classes()

    assert {name.split("::")[-1] for name in found} >= {
        "ChannelAdapter",
        "HttpChannelAdapter",
        "WeComProxyChannelAdapter",
    }, f"the scan lost sight of a known implementation; it found {sorted(found)}"

    silent = sorted(name for name, methods in found.items() if "capabilities" not in methods)
    assert silent == [], (
        "these implement the channel port but never say what they can be asked: "
        f"{silent}. Declare it (ADR-0094 §三); a missing declaration is the "
        "silence T78 registered, wearing a method name."
    )


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        (HttpChannelAdapter("http://channel.invalid/send"), True),
        (WeComProxyChannelAdapter(send_text=lambda **_: {}, guid="g", recipient_id="r"), False),
    ],
    ids=["m0-simulator", "wecom_proxy"],
)
def test_the_two_adapters_answer_differently_and_that_is_the_point(
    adapter: object, expected: bool
) -> None:
    """One can be asked by key, one cannot, and both say so in the same words.

    The M0 simulator serves `/by-key/{key}` -- including the `404` that means
    "no record of this key", which is the provider *answering*. WeComProxy
    serves nothing of the sort. Until this batch both facts were reachable only
    by calling and reading the receipt that came back.
    """

    declared = adapter.capabilities()  # type: ignore[attr-defined]

    assert isinstance(declared, ChannelCapabilities)
    assert declared.reconcile_by_key is expected


def test_the_wecom_proxy_declaration_is_grounded_in_the_surface_it_may_call() -> None:
    """A no that is checked against something, and rechecked when that changes.

    The declaration is not a claim about everything WeComProxy sells -- it is a
    claim about the operations this adapter is permitted to invoke, which is the
    only claim we are in a position to make (`AGENTS.md`, 「查明的事实」必须标注
    来源类型). **Eight** operations are permitted and four are refused outright;
    none of the twelve answers "what became of this message".

    **这两个数是跟着集合走的，不是背景说明** —— 09-16 加了
    `upload_file_by_url` 与 `send_file`，六变八、十变十二。
    下面那个集合改了而这两个数没跟着改，读的人会以为它还在说同一件事。

    So this pins the surface rather than the conclusion. **If the client ever
    grows an operation, this fails and the declaration above gets looked at
    again** -- which is the only mechanism by which a `False` written today can
    stop being true tomorrow.
    """

    # **Every callable attribute, not every `def`** (independent review
    # 2026-09-09, P3-3). `inspect.isfunction` sees plain methods and
    # `staticmethod`, and misses `classmethod` and `property` -- so the one
    # mechanism by which today's `False` can stop being true was blind to two
    # of the ways tomorrow's operation might be written.
    public = {
        name
        for name, value in vars(RestrictedWeComProxyClient).items()
        if not name.startswith("_")
        and (callable(value) or isinstance(value, classmethod | staticmethod | property))
    }

    assert public == {
        "create_client",
        "get_login_qrcode",
        "check_login_qrcode",
        "verify_login_qrcode",
        "set_notify_url",
        "send_text",
        # **`upload_file_by_url` 加进来时，上面那句 `False` 被重看过了，
        # 结论是它不变** —— 这条用例存在的全部意义就是逼出这次重看，
        # 所以把重看的结果写在这里，而不是只把名字塞进集合。
        #
        # 它**确实**带回一个 id（`requestId`），而那个 id 也**确实**能在回调流上
        # 被认领。差别在于那个 id 回答的是哪一个问题：
        #
        # | 问题 | 谁能答 |
        # |---|---|
        # | 「**这次上传**后来怎么样了」 | `requestId` 能（`cmd=20000`） |
        # | 「**这条消息**后来怎么样了」 | 仍然没有任何一个操作能答 |
        #
        # `reconcile_by_key` 问的是后者 —— 拿**调用方自己的幂等键**去问一条已发出
        # 的消息的下落。上传这条路上没有消息，也没有幂等键：它传的是一份资料，
        # 而那份资料还没有被发给任何人。**把这两件事合起来读，会得到一个
        # 「可以对账」的假结论**，而真到要对账的时候没有东西可问。
        "upload_file_by_url",
        # **`send_file` 加进来时又重看了一遍，而这次它是一次真的发送** ——
        # 上一次（`upload_file_by_url`）好判：那条路上根本没有消息。
        #
        # 这一条不同：它**发出一条消息**，所以「有没有办法问这条消息后来怎么样了」
        # 是一个真问题。答案仍然是没有，而理由与 `send_text` 逐字相同 ——
        # `/msg/sendFile` 的响应是 §0.3 那个「消息发送响应成功」，
        # 与 `sendText` 同一个信封：它回一个 `msgId`，
        # **而供应商没有任何一个操作拿这个 id 去问后来怎么样了**，
        # 更没有按*调用方自己的*幂等键问的。
        #
        # 也就是说 `False` 不是因为「这条路上没有消息」，
        # 而是因为**多了一条能发消息的路，并没有多出一条能问的路**。
        "send_file",
    } | set(FORBIDDEN_OPERATIONS), (
        "the permitted surface changed; re-read it before trusting "
        "`WeComProxyChannelAdapter.capabilities()`, which says no on the grounds "
        f"that none of these answers a status question. Found: {sorted(public)}"
    )


def test_a_capability_is_a_value_not_a_setting_that_drifts() -> None:
    """Frozen, because two receipts about the same channel must not disagree.

    `capabilities()` is consulted per dispatch. If the returned object were
    mutable, one dispatch could hand the next a different answer about the same
    channel and the receipts would disagree with no record of why.
    """

    declared = ChannelCapabilities(reconcile_by_key=True)

    with pytest.raises((AttributeError, TypeError)):
        declared.reconcile_by_key = False  # type: ignore[misc]
