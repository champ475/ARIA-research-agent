"""
utils/numbers.py — the one parser for numbers the way Indian users write them (VAL-09).

    parse_inr_amount("₹12.5L")        -> Parsed(Decimal("1250000"), ...)
    parse_inr_amount("1.2 cr")        -> 12,000,000
    parse_inr_amount("1,00,000")      -> 100,000      (Indian grouping)
    parse_inr_amount("Rs. 3,500.75")  -> 3500.75
    parse_number("15%")               -> 15, percent=True
    parse_number("-2.5")              -> -2.5

Handles ₹ / Rs / Rs. / INR (before) and rupees/rs (after); Indian and Western
comma grouping; k, thousand, L, lakh, lac, cr, crore, mn, million, bn,
billion and the compound "lakh crore"; decimals; negatives (also the Unicode
minus); and percent. Values are `Decimal` (no float drift on money); every
result carries the span it was read from, so callers can tell *which*
number in a sentence they got.

Functions return None unless the text holds exactly one clear number —
"10L, maybe 12 lakh" is ambiguous and must be asked about, not guessed.
Used by alert specs (VAL-04), the financial profile (VAL-05), the screener
grammar (VAL-06) and the grounding check (LLM-03).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Unit word -> multiplier. Longest alternatives are tried first in the regex.
UNIT_MULTIPLIERS: dict[str, Decimal] = {
    "lakh crore": Decimal(10) ** 12,
    "lakh crores": Decimal(10) ** 12,
    "k": Decimal(10) ** 3,
    "thousand": Decimal(10) ** 3,
    "l": Decimal(10) ** 5,
    "lac": Decimal(10) ** 5,
    "lacs": Decimal(10) ** 5,
    "lakh": Decimal(10) ** 5,
    "lakhs": Decimal(10) ** 5,
    "lk": Decimal(10) ** 5,
    "cr": Decimal(10) ** 7,
    "crs": Decimal(10) ** 7,
    "crore": Decimal(10) ** 7,
    "crores": Decimal(10) ** 7,
    "m": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "million": Decimal(10) ** 6,
    "bn": Decimal(10) ** 9,
    "billion": Decimal(10) ** 9,
}

_UNIT_ALT = "|".join(
    re.escape(u).replace(r"\ ", r"\s+") for u in sorted(UNIT_MULTIPLIERS, key=len, reverse=True)
)

# One amount: optional currency, a (possibly signed) number with comma
# grouping and decimals, an optional unit word. Group names `cur`, `num`,
# `unit` are part of the public contract (alert_spec embeds this pattern).
AMOUNT_RE = re.compile(
    r"(?:(?P<csign>[-−])\s*(?=₹|rs\b|rs\.|inr\b))?"  # "-₹500" (sign before the currency)
    r"(?P<cur>₹|rs\.?|inr)?\s*"
    r"(?P<num>[-+−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+))"
    r"(?:\s*(?P<unit>" + _UNIT_ALT + r")(?!\w))?"
    # The number must end here: at a non-word character, or right before a
    # percent marker ("9.5pc", "15%") — never in the middle of a word.
    r"(?=\s*(?:%|pc\b|percent\b|per\s*cent\b)|(?!\w))",
    re.IGNORECASE,
)
# A number followed by a percent sign or word.
_PERCENT_AFTER = re.compile(r"\s*(?:%|percent\b|per\s*cent\b|pc\b)", re.IGNORECASE)
_RUPEES_AFTER = re.compile(r"\s*(?:rupees?|rs\.?|inr)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Parsed:
    value: Decimal
    span: tuple[int, int]
    unit: str | None = None
    percent: bool = False
    currency: bool = False

    def __float__(self) -> float:
        return float(self.value)


def _to_decimal(raw: str) -> Decimal | None:
    raw = raw.replace(",", "").replace("−", "-")
    if raw.startswith("+"):
        raw = raw[1:]
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _from_match(m: re.Match[str], text: str) -> Parsed | None:
    value = _to_decimal(m.group("num"))
    if value is None:
        return None
    if m.group("csign"):
        value = -value
    unit = " ".join((m.group("unit") or "").lower().split()) or None
    if unit:
        value *= UNIT_MULTIPLIERS[unit]
    end = m.end()
    percent = False
    currency = bool(m.group("cur"))
    tail = _PERCENT_AFTER.match(text, end)
    if tail and not unit:
        percent, end = True, tail.end()
    else:
        tail = _RUPEES_AFTER.match(text, end)
        if tail:
            currency, end = True, tail.end()
    first = "csign" if m.group("csign") else "cur" if m.group("cur") else "num"
    start = m.start(first)
    return Parsed(value=value, span=(start, end), unit=unit, percent=percent, currency=currency)


def find_numbers(text: str | None) -> list[Parsed]:
    """Every number in `text`, left to right."""
    if not text:
        return []
    out = []
    for m in AMOUNT_RE.finditer(str(text)):
        p = _from_match(m, str(text))
        if p is not None:
            out.append(p)
    return out


def parse_number(text: str | int | float | Decimal | None) -> Parsed | None:
    """The single number in `text` (amounts, plain numbers and percents), or
    None if there is none or more than one."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        return Parsed(value=Decimal(str(text)), span=(0, 0))
    found = find_numbers(text)
    return found[0] if len(found) == 1 else None


def parse_inr_amount(text: str | int | float | Decimal | None) -> Parsed | None:
    """The single rupee amount in `text`, or None (no amount, several, or a
    percentage)."""
    p = parse_number(text)
    if p is None or p.percent:
        return None
    return p


def parse_amount(text: str | int | float | None) -> float | None:
    """Float convenience for parse_inr_amount (profile fields, legacy callers)."""
    p = parse_inr_amount(text)
    return float(p.value) if p is not None else None


def amount_from_match(m: re.Match[str]) -> float | None:
    """Float value of one AMOUNT_RE match (for regex-driven callers)."""
    p = _from_match(m, m.string)
    return float(p.value) if p is not None else None


def format_inr(value: float | Decimal) -> str:
    """₹ with Indian digit grouping: 1234567.5 -> ₹12,34,567.50."""
    d = Decimal(str(value)).quantize(Decimal("0.01"))
    neg = d < 0
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"
