"""Every migration records its own version, or it runs again on the next deploy.

**This gate exists because the clean-checkout gate structurally cannot catch the
thing it guards.** `scripts/migrate.py` skips a file whose stem already appears
in `schema_migrations`, and nothing in that loop writes the row -- each
migration file inserts it at the end. A file that forgets is applied on every
deploy forever.

`ci.sh` builds a fresh database on every run, so every migration runs exactly
once there and the omission is invisible. It showed up in the local loop, which
reuses its database, as `duplicate_object` on the *second* run -- the handoff's
§3.2c ("the gate's own environment hides some defects"), second instance.

Two files were missing the line when this was written: `028`, which would have
failed the second deploy outright because `ADD CONSTRAINT` has no
`IF NOT EXISTS`, and `005`, which had been silently re-running since it landed
and survived only because `ADD COLUMN IF NOT EXISTS` happens to be idempotent.
That is the pair worth remembering: **one broke loudly and one did not break at
all**, and only the second kind accumulates.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "infrastructure" / "migrations"


def test_every_migration_records_its_own_version() -> None:
    paths = sorted(MIGRATIONS.glob("*.sql"))

    assert paths, "no migration files found"
    missing = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        # The stem, quoted -- not merely "some insert into schema_migrations".
        # A file recording a *different* version would satisfy the weaker check
        # and still re-run itself while marking somebody else as applied.
        pattern = re.compile(
            r"INSERT\s+INTO\s+schema_migrations\s*\(\s*version\s*\)\s*"
            r"VALUES\s*\(\s*'" + re.escape(path.stem) + r"'\s*\)",
            re.IGNORECASE,
        )
        if not pattern.search(text):
            missing.append(path.name)

    assert not missing, (
        "these migrations do not record their own version and will re-run on "
        f"every deploy: {missing}"
    )


def test_the_recording_tolerates_a_second_run() -> None:
    """`ON CONFLICT DO NOTHING`, so a re-run cannot fail on the marker itself.

    Without it, a migration that ran once and was interrupted before committing
    could leave a state where re-running dies on the primary key rather than on
    whatever actually needs attention.
    """

    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        index = text.upper().find("INSERT INTO SCHEMA_MIGRATIONS")
        assert index != -1, path.name
        assert "ON CONFLICT" in text[index:].upper(), path.name
