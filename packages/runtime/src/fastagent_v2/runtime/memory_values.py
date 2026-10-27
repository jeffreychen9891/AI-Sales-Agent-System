"""Refuse a memory *value* that carries a forbidden category — T298 discharged.

## Why this module exists, and what it is not

ADR-0131 M2 splits the memory field vocabulary in two: **the merchant owns the
list of keys, the platform owns the forbidden categories** (identity numbers,
payment details, health, religion), the ceilings and the lifecycle.

E4-1b enforced the forbidden categories **at seal time, against the key name**
(`profiles/registry.py::_check_memory_fields`), and registered T298 against
itself in the same breath:

    匹配的是**商家自己取的 key 名字**。一个写 `id_card_no` 的商家会被挡住；
    一个写 `zjhm`、`ref_b`、`customer_extra_2` 的**不会**。
    …真正按内容的拒绝只能发生在**写入路径**上（值长什么样，而不是列叫什么），
    那是 E4-2 / E4-3 的事，登记为到期项。

E4-2 landed without it. **E4-3 is the last batch T298 names**, so it is
discharged here rather than re-anchored: a due condition whose named batches all
pass with nothing done is the shape `ADR-0100 §一.4` forbids.

## This is still a tripwire, and the boundary is the point

`_check_memory_fields` had to write its own limits down so nobody would read it
as a compliance guarantee. The same sentence is owed here, and it is a different
sentence:

* **What this catches**: a value that *is* a mainland-China resident identity
  number or *is* a payment card number, in a field called anything at all.
  That is the `customer_extra_2` case T298 was registered for.
* **What it does not catch**: the same number with the digits spelled out, split
  across two memories, base64'd, or transposed. **Health and religion have no
  computable shape at all** — `"糖尿病"` is a fact about a body and a string like
  any other. Those two of the four forbidden categories are enforced **only** at
  the key-name layer, and only against a merchant who names the column honestly.

**Nobody may read this module as "the platform guarantees no sensitive data is
remembered."** It raises the cost of the careless case. It does not raise the
cost of the determined one, and half the denylist is still out of reach.

## The direction the false positives run

Both detectors require a checksum *and* a structural marker, because a checksum
alone is cheap to pass by accident: one in ten arbitrary digit strings satisfies
Luhn. A card number additionally has to start with an issuer prefix this module
recognises; an identity number additionally has to carry a plausible birth date.

**It still over-refuses sometimes** — a 16-digit order number beginning `62`
that happens to satisfy Luhn will be refused, and that is the side chosen on
purpose. The cost of over-refusing is an employee rephrasing a note; the cost of
under-refusing is a customer's card number sitting in a database that was
designed on the premise that it does not hold one. The refusal names the
category so the person reading it can tell which case they are in.
"""

from __future__ import annotations

import re

#: Every forbidden category ADR-0131 M2 names, and whether this module can see
#: it in a value. **The two `False` rows are the honest half of T298** — they
#: are why the module docstring refuses to be read as a guarantee.
FORBIDDEN_CATEGORIES: dict[str, bool] = {
    "identity number": True,
    "payment details": True,
    "health": False,
    "religion": False,
}

#: GB 11643-1999 weights and check characters for the 18-digit resident identity
#: number. The 15-digit legacy format **is deliberately not detected**: it has no
#: check digit, so recognising it would mean matching "15 digits that look like a
#: date in the middle", and a merchant recording a date-stamped order reference
#: would trip it constantly.
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"

#: 18 characters, the last of which may be `X`. Bounded by non-alphanumerics so a
#: 20-digit string does not yield an 18-digit window.
_ID_CANDIDATE = re.compile(r"(?<![0-9A-Za-z])([0-9]{17}[0-9Xx])(?![0-9A-Za-z])")

#: 13–19 digits, optionally grouped by single spaces or hyphens the way people
#: actually type a card.
_CARD_CANDIDATE = re.compile(r"(?<![0-9])([0-9](?:[ -]?[0-9]){12,18})(?![0-9])")

#: Issuer prefixes this module recognises. Not exhaustive — an unrecognised
#: issuer is a miss, which is the under-refusing direction and is why the key
#: name layer is not retired by this one.
_CARD_PREFIXES: tuple[str, ...] = (
    "4",  # Visa
    "34",
    "37",  # American Express
    "35",  # JCB
    "62",  # UnionPay
    "6011",
    "65",  # Discover
    "51",
    "52",
    "53",
    "54",
    "55",  # Mastercard
)

#: Mastercard's 2-series, expressed as a numeric range because it is one.
_MASTERCARD_2_SERIES = range(222100, 272100)


def _is_identity_number(candidate: str) -> bool:
    """18-digit resident identity number: checksum **and** a plausible birth date.

    The checksum alone admits one in eleven arbitrary 18-character strings. The
    date test is what makes the two independent: positions 7–14 are `YYYYMMDD`
    and a real one lands in a range a living person could have been born in.
    """

    digits = candidate.upper()
    total = sum(int(digits[i]) * _ID_WEIGHTS[i] for i in range(17))
    if _ID_CHECK_CHARS[total % 11] != digits[17]:
        return False

    year, month, day = int(digits[6:10]), int(digits[10:12]), int(digits[12:14])
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def _passes_luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_payment_card(candidate: str) -> bool:
    """Luhn **and** a recognised issuer prefix. Either alone is not evidence."""

    digits = candidate.replace(" ", "").replace("-", "")
    if not 13 <= len(digits) <= 19 or not _passes_luhn(digits):
        return False
    if digits.startswith(_CARD_PREFIXES):
        return True
    return int(digits[:6]) in _MASTERCARD_2_SERIES


def forbidden_value_reason(value: str) -> str | None:
    """The category this value carries, or `None`.

    Returns a sentence, not a code: both write paths turn it into their own
    refusal — the route into an HTTP detail, the `remember` path into a
    `policy_decisions` row an operator reads. **The returned text never contains
    the value**, because a refusal that quotes the identity number it refused has
    written the identity number into the audit surface. That mistake has already
    been made once in this repository, in E4-2's own rate-limit path
    (`withheld_content`, independent review 2026-09-12 P1-1).
    """

    for match in _ID_CANDIDATE.finditer(value):
        if _is_identity_number(match.group(1)):
            return (
                "this value looks like a resident identity number, which "
                "ADR-0131 M2 keeps out of what the platform remembers "
                "regardless of what the field is called"
            )

    for match in _CARD_CANDIDATE.finditer(value):
        if _is_payment_card(match.group(1)):
            return (
                "this value looks like a payment card number, which ADR-0131 M2 "
                "keeps out of what the platform remembers regardless of what "
                "the field is called"
            )

    return None
