"""T44 / T101: `_decide_proposal` refuses an unsupported kind, and says which one.

**Read this first, it is the point of the file.** These cases verify the
*behaviour of a function*, **not that the path is reachable in the running
system**. Today it is not: the policy chain in front of `_decide_proposal` is
four guards deep --

1. a plan's candidates must be in the selected strategy's `allowed_intent_kinds`
   (the profile decides);
2. generation's kinds must be a subset of the plan's candidate kinds
   (`GENERATION_ADDED_INTENT`);
3. generation's count must not exceed the plan's candidate count
   (`GENERATION_DUPLICATED_INTENT`);
4. and only then does `_decide_proposal` ask whether the kind is one the send
   path carries (`GENERATION_SUPPORTED_KINDS`, today `{"send_message"}`).

For a turn to hold one acceptable and one unacceptable proposal at once, some
strategy would have to allow `send_message` **and** another kind. No strategy in
either synthetic profile does -- every one is `['send_message']`,
`['manual_task']` or empty. So "a refusal of one leaves the others alone"
(ruling item 3, user 2026-09-04) has **no end-to-end demonstration available**,
and the user's ruling (2026-09-05) is to prove it at the unit level and register
the gap rather than change a profile to make a test path exist. Changing what a
strategy may propose, to make a test reachable, would be letting the test drive
the product.

**That applies to ruling item 3 and to it alone.** Item 4 -- "the failure record
can answer *which* proposal failed" -- was folded in with it, and should not have
been: the record is `_record_proposal_denial`'s row in `policy_decisions`, and it
is perfectly reachable (independent review 2026-09-05, P1-2, which ran it). The
last case in this file writes that row through a recording double and asserts
what lands in it, so item 4 has evidence about the *record* rather than about a
dataclass field.

The registered gap is **T101**; its trigger is written down as "the first time
any profile declares a strategy allowing more than one intent kind". On that day
these cases stop being the only evidence, and an end-to-end one should replace
them.

The refusal branch reads nothing off `prepared` or `plan` -- it returns before
touching either -- which is what makes calling it with stand-ins honest here
rather than a fixture that pretends to be a turn.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastagent_v2.worker.pipeline import GENERATION_SUPPORTED_KINDS, InboxProcessor


def _processor() -> InboxProcessor:
    """The refusal branch touches no collaborator, so none are built."""

    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=None,  # type: ignore[arg-type]
        worker_id="unit",
    )


def _proposal(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "typed_payload": {"content": {"type": "text", "text": "..."}},
        "evidence_refs": [f"event:{uuid4()}"],
    }


def test_a_kind_the_send_path_does_not_carry_is_refused() -> None:
    """**主角换了，问的还是同一件事**（E4-2，2026-09-12）。

    这条原本拿 `remember` 当「发送路径不认的 kind」的例子，
    **而 E4-2 正是给 `remember` 建写入路径的那一批**——它进了
    `GENERATION_WRITE_PATHS`，于是不再是这条用例要说的那种 kind。

    **换的是例子，不是断言。** 用 `quote`：它在合同的 `kind` 枚举里、
    今天没有写入路径，**恰好是本条原本用 `remember` 想表达的那个位置**。
    """

    assert "quote" not in GENERATION_SUPPORTED_KINDS, (
        "本条要的是一个『合同认识、发送路径不认』的 kind；"
        "quote 若被建了写入路径，请换一个仍未注册的，不要改断言"
    )

    decision = _processor()._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal=_proposal("quote"),
        ordinal=0,
        candidate_basis="basis-a",
    )

    assert decision.accepted is False
    assert decision.reason_code == "GENERATION_INTENT_KIND_UNSUPPORTED"
    assert "quote" in decision.detail


def test_the_refusal_names_which_proposal_and_of_what_kind() -> None:
    """Ruling item 4. A denial that cannot say *which* one cannot be acted on.

    Asserted at ordinal 3 rather than 0: an implementation that always reported
    the first proposal would pass at 0 and be wrong everywhere else.
    """

    decision = _processor()._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal=_proposal("schedule_event"),
        ordinal=3,
        candidate_basis="basis-a",
    )

    assert decision.ordinal == 3
    assert decision.kind == "schedule_event"
    assert decision.proposal["kind"] == "schedule_event"


def test_send_message_is_not_refused_by_this_branch() -> None:
    """The negative above proves nothing on its own.

    A `_decide_proposal` that refused *everything* would satisfy both cases
    above. This one separates "the guard selects" from "the guard rejects
    wholesale" -- and it stops before the accepted path's first collaborator,
    which is why it asserts on the raised error rather than on a decision: with
    stand-ins for the database and the turn, an accepted proposal cannot get
    further than the first attribute it needs.
    """

    assert "send_message" in GENERATION_SUPPORTED_KINDS

    try:
        _processor()._decide_proposal(
            None,  # type: ignore[arg-type]
            plan=None,  # type: ignore[arg-type]
            proposal=_proposal("send_message"),
            ordinal=0,
            candidate_basis="basis-a",
        )
    except AttributeError:
        # Reached the accepted path: it went looking for the turn's data. That
        # is the assertion -- the kind guard let it through.
        return
    raise AssertionError("send_message was refused by the kind guard")


def test_a_send_message_without_content_is_refused_rather_than_carried_forward() -> None:
    """**T110**, in the shape a real model actually returned.

    DeepSeek answered `typed_payload = {"message_type": "acknowledgement"}` --
    kind `send_message`, schema-valid, so all four guards in front of this one
    passed. `_persist_accepted_proposal` then read `typed_payload["content"]`
    and raised `KeyError`, which rolled the turn back: the inbox event stayed
    unconsumed, nothing was written down, and the retry hit the same crash.

    **This case would not have failed on an assertion before the fix** -- it
    would have raised `AttributeError` from `plan=None`, because the proposal
    was on its way to being *accepted*. That is the detector: reaching the plan
    at all means the payload was carried forward.
    """

    decision = _processor()._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal={
            "kind": "send_message",
            "typed_payload": {"message_type": "acknowledgement"},
            "evidence_refs": [f"event:{uuid4()}"],
        },
        ordinal=2,
        candidate_basis="basis-a",
    )

    assert decision.accepted is False
    assert decision.reason_code == "GENERATION_PAYLOAD_INCOMPLETE"
    assert decision.ordinal == 2, "the record has to say which proposal"
    assert decision.kind == "send_message"
    # The detail names what was there instead, because "incomplete" alone does
    # not tell the next person what the model actually sent.
    assert "message_type" in decision.detail


def test_a_content_that_is_present_but_not_an_object_is_refused_too() -> None:
    """`content` is an object everywhere downstream (`SendCommand.content`).

    A string here would pass a `in payload` check and then travel all the way to
    the channel, which is the distance this guard exists to shorten.
    """

    decision = _processor()._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal={
            "kind": "send_message",
            "typed_payload": {"content": "just a string"},
            "evidence_refs": [f"event:{uuid4()}"],
        },
        ordinal=0,
        candidate_basis="basis-a",
    )

    assert decision.accepted is False
    assert decision.reason_code == "GENERATION_PAYLOAD_INCOMPLETE"


# --------------------------------------------------------------------------
# Ruling item 4, on the record itself (independent review 2026-09-05, P1-2)
# --------------------------------------------------------------------------


class _RecordingConnection:
    """Captures what `_record_proposal_denial` would have written.

    A double rather than a database, because the reachable end-to-end route to
    this row needs a profile whose strategy allows `manual_task`, and the e2e
    fixture publishes `synthetic-service-desk` -- whose frames do not allow
    `handoff` at all (the review measured `STRATEGY_NOT_IN_FRAME_ALLOWLIST`).
    Publishing a second profile from the fixture is a bigger change than this
    batch should carry, and the review names this as the acceptable fallback.

    What it can still prove is the whole of ruling item 4: that a refusal
    produces a row, that the row is a `pre_execute` denial with no intent id,
    and that it names **which** proposal and of what kind.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.statements.append((statement, parameters))


def _written_row(statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
    """把参数元组按 `INSERT` 的列名对上。

    **裁定二之后这一步是必要的**：hook / effect 从前是 SQL 里的字面量
    （下面两条原来就是在语句文本里搜 `'pre_execute', 'deny'`），
    收编成唯一写入点之后它们是参数 —— 而**在语句文本里搜不到的东西
    并不等于没写**，那两条当场变红正是这个原因。

    按列名对上比按下标取更值得：`policy_id` 与 `policy_version` 相邻且同为 `text`，
    下标取值读起来永远看不出它们有没有换过位置。
    """

    columns = statement.split("policy_decisions(", 1)[1].split(")", 1)[0]
    # `strip()` 已经吃掉换行，所以不必先 replace —— 少一处转义就少一处出错的机会。
    names = [name.strip() for name in columns.split(",") if name.strip()]
    assert len(names) == len(parameters), (
        f"{len(names)} 个列名对 {len(parameters)} 个参数 —— 列表与占位符不一样长"
    )
    return dict(zip(names, parameters, strict=True))


def test_a_refusal_writes_a_row_that_names_which_proposal_it_refused() -> None:
    from types import SimpleNamespace

    from fastagent_v2.worker.pipeline import ProposalDecision

    connection = _RecordingConnection()
    prepared = SimpleNamespace(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        aggregate={"generation": 7, "control_mode": "ai"},
    )
    decision = ProposalDecision(
        ordinal=1,
        kind="schedule_event",
        accepted=False,
        reason_code="GENERATION_INTENT_KIND_UNSUPPORTED",
        detail="the send path does not carry the schedule_event intent yet",
        proposal=_proposal("schedule_event"),
    )

    _processor()._record_proposal_denial(
        connection,
        prepared,  # type: ignore[arg-type]
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        decision=decision,
    )

    assert len(connection.statements) == 1
    statement, parameters = connection.statements[0]
    assert "INSERT INTO policy_decisions" in statement
    row = _written_row(statement, parameters)
    # The row is a denial at the pre-execute hook, and carries no intent id --
    # a refused proposal never became an intent, and borrowing one would point
    # the row at something that does not exist.
    assert row["hook"] == "pre_execute"
    assert row["effect"] == "deny"
    # **`is None`, where this used to be `assert "NULL" in statement`.** The old
    # form passed on any statement with the word NULL anywhere in it; this one
    # reads the value that column actually gets.
    assert row["intent_id"] is None
    assert row["reason_code"] == "GENERATION_INTENT_KIND_UNSUPPORTED"
    context = row["context"].obj
    # Ruling item 4, on the record: which proposal, and of what kind.
    assert context["proposal_ordinal"] == 1
    assert context["proposal_kind"] == "schedule_event"
    assert context["strategy_id"] == "acknowledge"
    assert context["detail"] == decision.detail


def test_the_payload_refusal_reaches_the_record_the_same_way() -> None:
    """**T110's completion criterion**: the refusal has a record, not a `KeyError`.

    The decision here is not hand-built -- it comes out of `_decide_proposal`,
    so this covers the join between the new guard and the row: a payload that
    cannot be sent produces a `pre_execute` denial naming which proposal, of
    what kind, and why.
    """

    from types import SimpleNamespace

    processor = _processor()
    decision = processor._decide_proposal(
        None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        proposal={
            "kind": "send_message",
            "typed_payload": {"message_type": "acknowledgement"},
            "evidence_refs": [f"event:{uuid4()}"],
        },
        ordinal=1,
        candidate_basis="basis-a",
    )

    connection = _RecordingConnection()
    processor._record_proposal_denial(
        connection,
        SimpleNamespace(  # type: ignore[arg-type]
            tenant_id=uuid4(),
            conversation_id=uuid4(),
            aggregate={"generation": 3, "control_mode": "ai"},
        ),
        plan=SimpleNamespace(strategy_id="acknowledge"),  # type: ignore[arg-type]
        decision=decision,
    )

    assert len(connection.statements) == 1
    statement, parameters = connection.statements[0]
    assert "INSERT INTO policy_decisions" in statement
    row = _written_row(statement, parameters)
    assert row["hook"] == "pre_execute"
    assert row["effect"] == "deny"
    assert row["reason_code"] == "GENERATION_PAYLOAD_INCOMPLETE"
    context = row["context"].obj
    assert context["proposal_ordinal"] == 1
    assert context["proposal_kind"] == "send_message"
    assert "message_type" in context["detail"]


# --------------------------------------------------------------------------
# A quoted material is resolved on the way to the outbox (2026-09-10)
# --------------------------------------------------------------------------
#
# **These two exist because a probe said they had to.** The generation-side
# tests (`tests/contracts/test_generation_receives_materials.py`) call
# `customer_text` directly, so they prove the reader resolves a ref -- and stay
# green when `pipeline` stops handing it the materials. Measured: removing
# `materials=` from both call sites left every materials test passing and only
# the database-backed suites able to notice. That is a fix landing on a line
# nobody guards, so the guard is here, where `_decide_proposal` and
# `_persist_accepted_proposal` can be driven without a database.


def _materials_turn() -> tuple[Any, Any, dict[str, Any], str]:
    """A turn standing in a frame that declares one material, quoting it."""

    import json
    from pathlib import Path
    from types import SimpleNamespace

    ref = "material:demo-opening:v1"
    text = json.loads(
        (Path(__file__).resolve().parents[2] / "tests/fixtures/internal-demo-materials.json")
        .read_text(encoding="utf-8")
    )["materials"][ref]
    definition = {"id": "introduce", "content_materials": [ref]}
    prepared = SimpleNamespace(
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        profile={
            "target_frames": [definition],
            "content_material_texts": {ref: text},
        },
        envelope={
            "event_id": str(uuid4()),
            "event_type": "customer_message",
            "correlation_id": str(uuid4()),
            "runtime_deadline_at": "2026-09-10T12:00:00Z",
        },
        aggregate={
            "id": str(uuid4()),
            "state_version": 1,
            "generation": 0,
            "control_mode": "ai",
            "profile_release_id": str(uuid4()),
            "target_stack": [{"id": str(uuid4()), "definition_id": "introduce"}],
        },
    )
    plan = SimpleNamespace(
        document={"candidate_intents": [{"kind": "send_message", "idempotency_basis": "b1"}]},
        definition=definition,
        strategy_id="acknowledge",
    )
    proposal = {
        "kind": "send_message",
        "evidence_refs": [f"event:{uuid4()}"],
        "typed_payload": {
            "content": {"segments": [{"type": "material_ref", "content_or_ref": ref}]}
        },
    }
    return prepared, plan, proposal, text


def _turn_materials(prepared: Any, plan: Any) -> dict[str, str]:
    """Resolved the way `process_prepared` resolves it: from the frame the turn
    **ends** on, which is the frame the intent is filed under."""

    from fastagent_v2.worker.platform_frames import frame_definitions, materials_for_frame

    top = prepared.aggregate["target_stack"][-1]
    definition = frame_definitions(prepared.profile).get(str(top["definition_id"]), {})
    return materials_for_frame(prepared.profile, definition)


def _materials_processor() -> InboxProcessor:
    from fastagent_v2.worker.inference import DeterministicMockInference

    return InboxProcessor(
        database=None,  # type: ignore[arg-type]
        pointer_queue=None,  # type: ignore[arg-type]
        inference=DeterministicMockInference(),
        worker_id="unit",
    )


def test_a_message_that_is_only_a_quoted_material_is_not_refused_as_unreadable() -> None:
    """Without the materials, `customer_text` reads a ref as no words at all.

    So this is the exact failure the wiring prevents: the content guard would
    refuse `GENERATION_CONTENT_UNREADABLE`, and a turn whose entire message is
    one quoted material would send nothing while looking well formed at every
    other layer.
    """

    prepared, plan, proposal, _ = _materials_turn()

    decision = _materials_processor()._decide_proposal(
        prepared,  # type: ignore[arg-type]
        plan=plan,  # type: ignore[arg-type]
        proposal=proposal,
        ordinal=0,
        candidate_basis="basis-a",
        materials=_turn_materials(prepared, plan),
    )

    assert decision.reason_code != "GENERATION_CONTENT_UNREADABLE"
    assert decision.accepted is True


def test_the_queued_row_carries_the_merchants_words_and_not_the_ref() -> None:
    """**The property, at the only place it can be observed without a database.**

    `outbox_messages.command.content.text` is what the adapter sends, and the
    adapter resolves nothing. A row carrying `material:...:v1` is a message that
    reaches the customer as a reference number, or -- because `customer_text`
    refuses it -- as nothing at all.
    """

    import json

    prepared, plan, proposal, text = _materials_turn()
    processor = _materials_processor()
    materials = _turn_materials(prepared, plan)
    decision = processor._decide_proposal(
        prepared,  # type: ignore[arg-type]
        plan=plan,  # type: ignore[arg-type]
        proposal=proposal,
        ordinal=0,
        candidate_basis="basis-a",
        materials=materials,
    )
    connection = _RecordingConnection()

    processor._persist_accepted_proposal(
        connection,
        prepared,  # type: ignore[arg-type]
        plan=plan,  # type: ignore[arg-type]
        decision=decision,
        materials=materials,
    )

    commands = [
        json.loads(parameter.obj) if isinstance(parameter.obj, str) else parameter.obj
        for statement, parameters in connection.statements
        if "outbox_messages" in statement
        for parameter in parameters
        if hasattr(parameter, "obj") and isinstance(parameter.obj, dict | str)
    ]
    queued = next(item for item in commands if isinstance(item, dict) and "content" in item)
    assert queued["content"]["text"] == text
    assert "material:" not in queued["content"]["text"]
