#!/usr/bin/env python
"""Regenerate the carrier corpus's negative fixtures (content-addressed).

A `forbid` nobody can produce a violation for is not a rule, it is a sentence.
These fixtures are the falsifiability check: for each forbid rubric, one output
that plainly breaks it, one that plainly does not, and -- since the independent
review -- one **derived from the rubric's paired `must`**, which the screen is
forbidden to have an opinion about.

**This script exists in the repository because the artifact it writes is pinned
by hash.** It was originally a throwaway, named in a permanent test docstring and
in a gate record while not being in the repository at all: readers could not find
it and the pinned file could not be regenerated (independent review 2026-09-03,
P3-3). Run from the repository root; it rewrites the fixtures file and re-pins
`active-cases.json`.

`newline` is forced to LF on every write on purpose: the first version used
`Path.write_text`, which on Windows emits CRLF, and the hash was pinned over
bytes git would then normalise -- green locally, red in a fresh checkout.
"""
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

OUT = Path("regression/cases/synthetic-carrier-negatives-2026-09-03.json")
REGISTRY = Path("regression/assertion-rubric-registry-carrier.json")
MANIFEST = Path("regression/active-cases.json")

VISIBLE = "generation.customer_visible_segments"


def pair(
    rubric_id: str,
    decidability: str,
    violating: Sequence[str],
    compliant: Sequence[str],
    *,
    evidence: str = VISIBLE,
    note: str | None = None,
    must_pair: str | None = None,
    satisfying_the_paired_must: Sequence[str] | None = None,
) -> dict[str, object]:
    """One fixture. `satisfying_the_paired_must` is the anti-misfire sample.

    Those samples are **derived, not hand-picked**: each is written to satisfy
    the paired `must` rubric's criterion as literally as it can be written,
    including whatever figure that criterion forces into the output. The screen
    is required to have no opinion about any of them. That rule is what the old
    `no_figures` scope failed -- `SC01-E09-M1` asks for the recorded return date,
    and a date is digits.
    """

    entry: dict[str, object] = {
        "rubric_id": rubric_id,
        "decidability": decidability,
        "evidence_path": evidence,
        "violating": violating,
        "compliant": compliant,
    }
    if must_pair:
        entry["must_pair"] = must_pair
        entry["satisfying_the_paired_must"] = satisfying_the_paired_must
    if note:
        entry["note"] = note
    return entry


fixtures = [
    pair(
        "SC01-E00-F1", "judge_only",
        ["押金 200 元，租金每天 30 元，现在就可以借。", "起步价 199，随时有货。"],
        ["你好，可以帮你办理借用。先说说你想借什么、大概用几天？"],
        must_pair="SC01-E00-M1",
        satisfying_the_paired_must=[
            "你好，这里可以协助 3 类物品的借用，也可以先聊聊你的情况。",
            "你好，我们的服务时间是 9:00-18:00，先说说你想办什么？",
        ],
        note=(
            "Was screenable; the review found the scope wrong in both directions. "
            "The criterion is 'any amount, rate or availability claim': an "
            "availability claim carries no digit, while an opening that names how "
            "many categories exist, or the service hours, carries several."
        ),
    ),
    pair(
        "SC01-E01-F1", "screenable",
        ["你想借什么？大概用几天？需要开发票吗？"],
        ["你想借什么？"],
        must_pair="SC01-E01-M1",
        satisfying_the_paired_must=["你说的是 3 天，对吗？"],
        note=(
            "Screened by counting question marks, not by reading the questions -- "
            "which is why the figure in the derived sample is none of its business."
        ),
    ),
    pair(
        "SC01-E02-F1", "judge_only",
        ["押金大概 200 出头吧，算个整数 200 就行。"],
        ["按你说的类别和三天，押金和租金我按固定价目表给你算：以下为表里的数。"],
        must_pair="SC01-E02-M1",
        satisfying_the_paired_must=[
            "按你记录的类别和 3 天，押金 200 元、租金每天 30 元，都是价目表里的数。"
        ],
        note=(
            "Not screenable: deciding whether a figure was invented, rounded or "
            "adjusted requires the pricing policy's own output to compare against, "
            "and the screen has no access to it. A number being present is not a "
            "violation here -- quoting is exactly what this turn is for."
        ),
    ),
    pair(
        "SC01-E04-F1", "judge_only",
        ["大概 500 到 800 吧。", "跟上次差不多，800 左右。"],
        ["这件事要先评估过才有数字，我先把范围记下来。"],
        must_pair="SC01-E04-M1",
        satisfying_the_paired_must=["这件事要先评估，通常 3 个工作日出结果，现在还没有数字。"],
        note=(
            "Was screenable. The criterion forbids 'any amount, range, estimate or "
            "comparison', and the paired must requires saying the request has to be "
            "assessed before any figure exists -- an answer that states how long the "
            "assessment takes is doing exactly what is asked, and contains digits."
        ),
    ),
    pair(
        "SC01-E05-F1", "judge_only",
        ["就按 600 算吧，我给你做主。", "这次给你破例，按 500 走。"],
        ["这个得让同事看过才能定，我先转给他们。"],
        must_pair="SC01-E05-M1",
        satisfying_the_paired_must=["这个我转给同事处理，1 小时内给你回话。"],
        note=(
            "Was screenable on the theory that 'the figure half screens' -- but the "
            "criterion ('Do not price the request, and do not approve any departure "
            "from policy') has no figure half. Both halves need semantics."
        ),
    ),
    pair(
        "SC01-E07-F1", "screenable",
        ["回到刚才的借用，押金还是 200。", "先回去把借用办完，之前算的是 3 天。"],
        ["回到刚才的借用吧？"],
        must_pair="SC01-E07-M1",
        satisfying_the_paired_must=["咨询这边先到这里，回去把借用确认掉，好吗？"],
        note=(
            "The one `no_figures` entry that survives the review. Its criterion "
            "forbids restating *any* figure, and its paired must -- propose "
            "returning to the rental goal -- is satisfiable without stating one, so "
            "screen and rubric agree about every output. The second violating "
            "example is a period rather than a price, which is the point: the "
            "criterion says figure, not money."
        ),
    ),
    pair(
        "SC01-E08-F1", "judge_only",
        ["你想借什么类别的？借几天？"],
        ["按你之前说的类别和三天，归还日快到了。"],
        must_pair="SC01-E08-M1",
        satisfying_the_paired_must=["你记录的是 3 天，归还日不变，我这边不再问一遍。"],
        note=(
            "Not screenable: whether a question re-asks something the frame already "
            "holds depends on the slot state and on the two phrasings meaning the "
            "same thing. The violating example is a question the corpus already has "
            "the answer to; nothing in the text says so."
        ),
    ),
    pair(
        "SC01-E09-F1", "judge_only",
        ["提醒你归还日快到了，另外续租可以给你 20 元一天。"],
        ["提醒一下，你记录的归还日是 9 月 5 日。"],
        must_pair="SC01-E09-M1",
        satisfying_the_paired_must=["提醒一下，你记录的归还日是 9 月 5 日。"],
        note=(
            "The sharpest of the four. `SC01-E09-M1` requires reminding the customer "
            "of the recorded return date; a date is digits; so the screen failed "
            "every output that did what the corpus asks. The compliant sample used "
            "to be '提醒一下，归还日快到了。' -- it passed the screen only by leaving "
            "the date out, and in doing so probably failed its own paired must. It "
            "now states the date, which is what makes this entry honest. The "
            "'no follow-up after opt-out' half was never screenable either."
        ),
    ),
]

for item in fixtures:
    if item.get("must_pair"):
        assert item["satisfying_the_paired_must"], item["rubric_id"]

registry = json.loads(REGISTRY.read_text(encoding="utf-8"))["rubrics"]
forbids = {key for key, value in registry.items() if value["kind"] == "forbid"}
covered = {item["rubric_id"] for item in fixtures}
assert covered == forbids, f"missing {forbids - covered}, extra {covered - forbids}"

document = {
    "schema_version": "1.0.0",
    "kind": "negative_fixtures",
    "status": "ACTIVE",
    "for_case": "regression/cases/synthetic-carrier-v1-2026-09-03.json",
    "rubric_registry": "regression/assertion-rubric-registry-carrier.json",
    "purpose": (
        "A `forbid` nobody can produce a violation for is not a rule, it is a sentence. "
        "Each entry gives one output that plainly breaks its rubric and one that plainly does "
        "not, so the assertion is falsifiable before any judge exists. Writing them is also "
        "what surfaced how much of the corpus a judge is actually load-bearing for -- see "
        "`decidability`."
    ),
    "structural_note": (
        "There used to be a third category, `structural`: two forbid rubrics decidable from "
        "the transition alone. They were deleted on 2026-09-03 by user ruling, because the "
        "corpus's own goal_frame block already asserted exactly the same transitions -- "
        "keeping them as rubrics only bought a judge call each to re-answer a decided "
        "question. The coverage did not go anywhere; "
        "test_the_transitions_those_deleted_rubrics_described_are_still_asserted keeps it "
        "honest."
    ),
    "screen_scope_note": (
        "Four entries moved from screenable to judge_only on 2026-09-03, after an "
        "independent review measured the screen failing correct outputs: a digit "
        "screen had been hung on criteria that never mention figures. Entries now "
        "carry `satisfying_the_paired_must` -- an output written to satisfy the "
        "rubric's paired `must` as literally as possible -- and the screen must "
        "have no opinion about any of them."
    ),
    "decidability_meanings": {
        "screenable": (
            "A deterministic screen can catch clear violations without a model. The screen may "
            "only FAIL, never PASS: it has no opinion on outputs it does not object to."
        ),
        "judge_only": (
            "Needs semantics the screen cannot reach -- comparing against a policy's output, or "
            "deciding that two phrasings ask the same thing."
        ),
    },
    "source": {
        "system": "synthetic",
        "method": (
            "Authored for this repository alongside the carrier corpus. No customer, merchant "
            "or production data of any kind."
        ),
    },
    "fixtures": fixtures,
}

open(OUT, "w", encoding="utf-8", newline="\n").write(
    json.dumps(document, ensure_ascii=False, indent=2) + "\n"
)

digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
manifest["active_cases"][0]["negative_fixtures"] = {
    "file": "regression/cases/synthetic-carrier-negatives-2026-09-03.json",
    "sha256": digest,
}
open(MANIFEST, "w", encoding="utf-8", newline="\n").write(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
)

counts: dict[str, int] = {}
for item in fixtures:
    # `item` holds `object` values by construction, so the key has to be
    # narrowed rather than assumed -- and if it were ever not a string, the
    # tally would silently grow a second bucket instead of failing (T68).
    decidability = item["decidability"]
    assert isinstance(decidability, str)
    counts[decidability] = counts.get(decidability, 0) + 1
print("negatives sha256 =", digest)
print("decidability     =", counts)
