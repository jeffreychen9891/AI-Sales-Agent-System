"""T97 / B3-2: the hand-seeded interim counterparty column, and keeping it gone.

`017` added `conversation_aggregates.peer_external_user_id` as an admitted
interim column and wrote its own expiry into the comment above it -- "an interim
column with no expiry is just a column". `045` drops it, because the inbound
half has landed and the envelope carries the identity in `actor.actor_id`.

**These cases are the part that outlives the migration.** A migration runs once;
what stops the same shape coming back is an assertion that says why it was
wrong: the value had **no writer, no source and no constraint** -- an operator
seeded it and nothing could check it -- and a single counterparty on the
aggregate is correct only until a rebind changes it.

**Scoped to that column, not to "conversation-level counterparty" as a class**
(independent review 2026-09-08, P2-2). `conversation_aggregates.contact_id`
(`040`) is also a conversation-level counterparty and is entirely legitimate:
production writes it from the event's own identity and a foreign key checks it.
The difference is spelled out on the case that used to over-claim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infrastructure" / "migrations"
ENVELOPE = ROOT / "packages" / "contracts" / "schemas" / "message-envelope.schema.json"

#: The two files allowed to name the dropped column: the one that created it and
#: the one that removed it. Both are history and must keep their reasoning.
ALLOWED = {"017_outbound_channel_addressing.sql", "045_drop_interim_peer_identity.sql"}


def test_the_envelope_still_carries_which_counterparty_spoke() -> None:
    """The durable home. **Asserted first**, because everything below is a
    refusal and a refusal is only safe once the replacement is real.

    This is also the correction the batch turned on: 排期 §三 expected
    "动冻结合同 → 需 ADR" here, on the assumption that the envelope had only a
    `party` enum. It has carried `actor_id` since the M0 skeleton, so the frozen
    contract never needed to move.
    """

    schema = json.loads(ENVELOPE.read_text(encoding="utf-8"))
    actor = schema["properties"]["actor"]

    assert "actor_id" in actor["properties"], "the durable home is gone"
    assert actor["properties"]["actor_id"]["type"] == ["string", "null"]
    # `party` says *what kind* spoke; `actor_id` says *which one*. Losing the
    # distinction is what put the interim column on the aggregate.
    assert "party" in actor["properties"]
    assert actor["required"] == ["party"], (
        "actor_id stays optional: T180 degrades an identity we cannot store, "
        "and the message still arrives"
    )


def test_no_code_reads_the_dropped_interim_column() -> None:
    """The column is gone from the schema; this keeps it gone from the code.

    **Scoped to this one column, not to "conversation-level counterparty" as a
    class** (independent review 2026-09-08, P2-2). The earlier name and message
    claimed the general rule, and the general rule is **not true of this
    codebase**: `conversation_aggregates.contact_id` (migration `040`) is
    documented as "The customer this conversation is with", written by the
    worker and read by the control plane. A tripwire that only greps one column
    name would have stayed green while asserting something false.

    **The two are genuinely different, and `040` says how.** `contact_id` is
    written by production code "from the same `(config_id, external_user_id)`
    identity ingress used to create the contact", and carries a foreign key to
    `contacts` -- it is the event's identity *materialised as a checked
    reference*, which is the same principle this batch is applying rather than
    an exception to it. The dropped column had **no writer at all**: an operator
    seeded it by hand, nothing derived it, nothing referenced it, and nothing
    could check it.

    Whether `contact_id` should also change across a rebind is a real question,
    but it is `040`'s question and not this batch's -- noted here so the next
    reader does not have to rediscover that the distinction was considered.

    Scanned over sources rather than against a live database on purpose: the
    defect this prevents is someone *re-adding* the read, and that shows up in a
    diff long before it shows up in a schema. The database side is asserted
    separately, in the integration case.
    """

    offenders = []
    for root in ("services", "packages", "scripts"):
        for path in sorted((ROOT / root).rglob("*.py")):
            if "peer_external_user_id" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], (
        "peer_external_user_id was dropped by 045; the counterparty comes from "
        f"the inbound event's actor.actor_id -- these still read it: {offenders}"
    )


def test_no_migration_reintroduces_the_interim_column() -> None:
    """A later migration adding it back would pass every other case here.

    Allowed in exactly two files, by name: `017` created it and `045` dropped
    it, and both keep their reasoning. Path-exact rather than a substring match,
    so `046_...` cannot inherit the exemption -- the same discipline the channel
    binding guard uses for its named exception.
    """

    adders = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"peer_external_user_id", text):
            adders.append(path.name)

    assert adders == [], f"these migrations name the dropped interim column: {adders}"


def test_the_drop_is_the_migration_that_says_why() -> None:
    """Guards the guard: `045` must keep the reason, not just the DDL.

    A bare `DROP COLUMN` reads like tidying. The reason it was dropped rather
    than backfilled -- a conversation can change counterparty across a rebind --
    is the part a future reader needs, and it is the part most easily lost when
    someone reformats a migration.
    """

    text = (MIGRATIONS / "045_drop_interim_peer_identity.sql").read_text(encoding="utf-8")

    assert "DROP COLUMN IF EXISTS peer_external_user_id" in text
    assert "T97" in text
    assert "rebind" in text, "the reason for dropping rather than backfilling is gone"
    # **The provenance of the measurement, not the measurement itself**
    # (independent review P3-3). This asserts that `045` still carries the
    # Stage counts it was written against; it says nothing about whether those
    # counts were right, and it must not be read as if it did. What the column
    # actually looks like after the migration runs is asserted against the
    # database, in `tests/integration/test_interim_column_is_gone.py`.
    assert "0 | 0" in text
