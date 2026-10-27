"""`inbox_events.provider_raw_data` is written and never read, and stays that way.

**T205 / B3-4d / ADR-0103 §三.** The ADR stores the provider's `base64RawData`
so that a parsing mistake has something to be diagnosed against, and requires in
the same breath that its zero-consumer status be **written down**:

> 本仓已经有过 `redact()`（零调用方两个月）、`reconcile()`（零生产调用方）、
> `frame_generation`（只写不读）、`legal_hold`（恒 false）四个先例，
> 而它们共同的代价是**下一个读到那个字段的人以为它在起作用**。

A sentence in a comment is what those four had too. This file is the half a
comment cannot do: it finds the readers itself, so the day somebody adds one,
the batch that adds it has to say so out loud.

The other half -- that the value which *is* stored is the one that arrived --
lives in `tests/integration/test_raw_payload_retention.py`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from fastagent_v2.ingress.wecom_proxy.inbound import RAW_PAYLOAD_KEY, retained_raw_payload

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (
    REPOSITORY_ROOT / "services",
    REPOSITORY_ROOT / "packages",
    REPOSITORY_ROOT / "scripts",
)
COLUMN = "provider_raw_data"
MIGRATION = REPOSITORY_ROOT / "infrastructure" / "migrations" / "047_inbound_raw_payload.sql"


#: A literal counts as a query when it names the column **and** carries a verb
#: that does something with it. Prose about the column is not a reader.
SQL_VERBS = ("select", "insert", "update", "delete", "returning")

#: The one statement allowed to name the column, identified by **where it is**
#: rather than by what it contains (independent review 2026-09-09, P2-1).
#:
#: The first version exempted any literal containing the substring
#: `INSERT INTO inbox_events`, which exempts the whole statement -- so
#: `INSERT INTO inbox_events(...) SELECT ..., provider_raw_data FROM
#: inbox_events` passed untouched. That is not a hypothetical: a backfill or an
#: archive-and-purge statement is the **most likely** first real reader of this
#: column, and the check was blind to exactly it.
WRITER_PATH = "services/ingress/src/fastagent_v2/ingress/adapter.py"


def _sql_literals_mentioning_the_column() -> list[tuple[str, str]]:
    """Every string literal in production code that queries the column.

    **Found by walking the AST, not by listing the files that are allowed to.**
    A hand-written allowlist is a list of the readers somebody remembered; the
    reader this test exists for is the one nobody remembered.

    **Docstrings are skipped explicitly rather than by hoping they contain no
    SQL words.** This module's own helper explains the column at length, and
    counting that as a reader would have made the test unfixable except by
    deleting the explanation -- exactly backwards for a batch whose deliverable
    is the explanation.

    **What this scan cannot see, written down rather than discovered later**
    (independent review 2026-09-09, P2-1(c)):

    * **`.py` files only, under `services/ packages/ scripts/`.** A reader in a
      migration -- a view, a report, a function body -- is invisible to it, and
      a migration is the most natural place for the next column-level reader to
      appear. `apps/` holds only a `.gitkeep` today; code landing there would
      be outside these roots.
    * **Literal strings only.** A column name assembled through a variable
      (`f"SELECT {column} FROM ..."`) is not a literal naming the column, and
      no AST-level check short of dataflow analysis would catch it.
    * **The column's name only.** `SELECT *` is covered by a separate case
      below, because it is the one bypass that is both invisible here and
      actually used in this repository.
    """

    found: list[tuple[str, str]] = []
    for root in PRODUCTION_ROOTS:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            documented = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                )
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if id(node) in documented or COLUMN not in node.value:
                    continue
                lowered = node.value.lower()
                if any(verb in lowered for verb in SQL_VERBS):
                    found.append((source.relative_to(REPOSITORY_ROOT).as_posix(), node.value))
    return found


def test_the_only_statement_that_names_the_column_is_the_insert() -> None:
    """Exactly one statement names it, it lives in the writer, and it only writes.

    Three assertions, and each one exists because the version before it passed
    while something was wrong (independent review 2026-09-09, P2-1):

    1. **something was found** -- a scan whose anchor stops matching reports an
       empty list and passes, which reads exactly like "no readers exist"
       (`AGENTS.md`, 「锚点没命中」不是「通过」);
    2. **exactly one statement, and it is in the writer** -- the exemption is
       anchored to `WRITER_PATH`, so a second statement anywhere fails even if
       it is spelled like an insert;
    3. **that statement does not also read** -- `INSERT ... SELECT` is one
       literal containing both, and the substring exemption it used to get is
       precisely how a backfill would have walked past this test.
    """

    statements = _sql_literals_mentioning_the_column()

    assert statements, (
        "the scan found no mention of the column at all -- including the INSERT "
        "that is known to exist, so the scan is broken rather than the code clean"
    )
    assert len(statements) == 1, (
        f"{COLUMN} is named by {len(statements)} statements, and only the writer may "
        f"name it: {[path for path, _ in statements]}"
    )
    path, statement = statements[0]
    assert path == WRITER_PATH, (
        f"{COLUMN} is named outside the writer ({path}). It is stored with no consumer "
        "by decision (T205, ADR-0103 三). Reading it is allowed, but not quietly: say "
        "what you think it contains, because whether it holds the callback JSON or "
        "media bytes is still an open observation."
    )
    lowered = statement.lower()
    assert lowered.lstrip().startswith("insert into inbox_events"), (
        f"the writer's statement no longer starts as an insert: {statement[:80]!r}"
    )
    assert "select" not in lowered, (
        "the writer's statement now reads as well as writes -- an `INSERT ... SELECT` "
        "is the shape a backfill takes, and it is a reader wearing the writer's name"
    )


def test_no_production_query_selects_every_column_of_inbox_events() -> None:
    """The complement of the scan above, and the reason it is enough today.

    A scan for the column's **name** cannot see `SELECT *`, and this repository
    does use `SELECT *` elsewhere, so "nothing names it" only means "nothing
    reads it" while no query takes the whole row (independent review
    2026-09-09, P2-1(a); the reviewer noted this fact is what makes the
    zero-consumer claim true today, and that it was nowhere written down).
    """

    offenders: list[str] = []
    for root in PRODUCTION_ROOTS:
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                collapsed = " ".join(node.value.lower().split())
                if "select * from inbox_events" in collapsed:
                    offenders.append(source.relative_to(REPOSITORY_ROOT).as_posix())

    assert offenders == [], (
        f"these take every column of inbox_events, which includes {COLUMN}: {offenders}. "
        "Name the columns you want, or say in the batch why reading the raw copy is "
        "intended (T205, ADR-0103 三)."
    )


def test_the_column_carries_its_own_disclaimer() -> None:
    """The 库内注释 half of ADR-0103 §三, checked rather than trusted.

    `COMMENT ON COLUMN` is the only part of this that a person meets by
    accident -- in `\\d+ inbox_events`, in a schema dump, in whatever tool they
    are using when they first see the column and wonder what it is for.
    """

    migration = MIGRATION.read_text(encoding="utf-8")

    assert f"COMMENT ON COLUMN inbox_events.{COLUMN}" in migration
    assert "ZERO CONSUMERS TODAY, BY DESIGN" in migration
    assert "T205" in migration


def test_the_frozen_contract_did_not_move() -> None:
    """The blob is not in the envelope, and `message-envelope` is untouched.

    **User ruling, 2026-09-09.** ADR-0103 §三 says 「库内注释与合同里明写」 without
    naming a revision to `message-envelope`, and 「ADR 未点名即不生效」 -- so the
    frozen contract keeps its shape and the column carries the value instead.
    `additionalProperties: false` means an envelope carrying this field would be
    refused at `validate_contract`, which is the property asserted here.
    """

    schema = json.loads(
        (
            REPOSITORY_ROOT / "packages/contracts/schemas/message-envelope.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["additionalProperties"] is False
    assert RAW_PAYLOAD_KEY not in schema["properties"]
    assert COLUMN not in schema["properties"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({RAW_PAYLOAD_KEY: "CAMQ0e+yBA=="}, "CAMQ0e+yBA=="),
        ({}, None),
        ({RAW_PAYLOAD_KEY: None}, None),
        ({RAW_PAYLOAD_KEY: {"nested": "object"}}, None),
        ({RAW_PAYLOAD_KEY: 12}, None),
        ({RAW_PAYLOAD_KEY: ""}, None),
        ({RAW_PAYLOAD_KEY: "CAMQ\x00BA=="}, None),
        ({RAW_PAYLOAD_KEY: "CAMQ\ud800BA=="}, None),
    ],
    ids=[
        "kept-verbatim",
        "absent",
        "null",
        "not-a-string",
        "a-number",
        "empty",
        "nul-byte",
        "lone-surrogate",
    ],
)
def test_what_is_kept_and_what_degrades(payload: dict[str, object], expected: str | None) -> None:
    """Two of these are the point, and they pull in opposite directions.

    **`not-a-string` yields `None` rather than `str(value)`**: coercion would
    store `"{'nested': 'object'}"` as though the provider had sent it, which is
    the defect independent review found in `str(payload.get("actor_id"))`.

    **`nul-byte` and `lone-surrogate` yield `None` rather than raising**: left
    to the database, `psycopg` raises `UntranslatableCharacter` out of
    `accept()`, the callback gets a 500, and the provider redelivers into the
    same crash -- ten of those and it deletes the callback address (T204 /
    T207 / T223). Losing a customer's message to preserve a field nobody reads
    would be the worst trade available here.
    """

    assert retained_raw_payload(dict(payload)) == expected


def test_it_is_not_trimmed_because_verbatim_is_the_whole_point() -> None:
    """Unlike a caption, where `_usable_string` strips and should.

    The only use this value has is "what actually arrived". A copy that has been
    tidied is a copy that cannot answer that question.
    """

    assert retained_raw_payload({RAW_PAYLOAD_KEY: "  CAMQ0e+yBA==  "}) == "  CAMQ0e+yBA==  "


def test_only_the_top_level_is_searched() -> None:
    """It rides on the envelope beside `msgData`, so that is where it is read.

    Searching the message sources the way content lookups do would invent a
    second location and silently prefer whichever arrived. **Nothing reads the
    result**, so that choice would never surface as a bug -- it would surface
    years later as two rows that disagree about what was received.
    """

    assert retained_raw_payload({"msgData": {RAW_PAYLOAD_KEY: "aW5uZXI="}}) is None
    both = {RAW_PAYLOAD_KEY: "b3V0ZXI=", "msgData": {RAW_PAYLOAD_KEY: "aW5uZXI="}}
    assert retained_raw_payload(both) == "b3V0ZXI="
