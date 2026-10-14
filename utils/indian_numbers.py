"""
utils/indian_numbers.py — parse amounts the way Indian users write them.

"₹3,500", "Rs. 3500", "3.5k", "1.2 lakh", "1.2L", "12 lakhs", "2 crore",
"2cr", "1,20,000" (Indian digit grouping) -> float. Used by alert specs
(VAL-04) and the financial profile (VAL-05). Returns None for anything that
isn't a single clear amount ("lots", "all of it", "-").
"""

from __future__ import annotations

import re

_MULTIPLIERS = {
    "k": 1e3,
    "thousand": 1e3,
    "l": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lk": 1e5,
    "cr": 1e7,
    "crs": 1e7,
    "crore": 1e7,
    "crores": 1e7,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "bn": 1e9,
    "billion": 1e9,
}

# One amount: optional currency, digits with , grouping, optional decimals,
# optional unit word. The number itself may be signed.
AMOUNT_RE = re.compile(
    r"(?P<cur>₹|rs\.?|inr)?\s*(?P<num>[-+]?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>"
    + "|".join(sorted(map(re.escape, _MULTIPLIERS), key=len, reverse=True))
    + r")?\b",
    re.IGNORECASE,
)


def parse_amount(text: str | float | int | None) -> float | None:
    """The single amount in `text`, or None if there isn't exactly one clear amount."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    matches = list(AMOUNT_RE.finditer(s))
    if len(matches) != 1:
        return None
    return amount_from_match(matches[0])


def amount_from_match(m: re.Match) -> float | None:
    raw = m.group("num").replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower()
    return value * _MULTIPLIERS.get(unit, 1.0)


def format_inr(value: float) -> str:
    """₹ with Indian digit grouping: 1234567.5 -> ₹12,34,567.50."""
    neg = value < 0
    whole, frac = f"{abs(value):.2f}".split(".")
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
