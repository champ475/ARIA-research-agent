"""
tools/screener_grammar.py — parse a plain-English stock screen (VAL-06).

    parse("IT stocks with P/E <= 18.5 and ROE > 15%")
      -> Screen(sector="Technology",
                filters=[ScreenFilter("pe", "<=", 18.5), ScreenFilter("roe", ">", 15)])

Grammar (case-insensitive):
    field     pe | pb | roe | roce | de | mcap | div_yield | rev_growth | price
              (with aliases: "P/E", "price to earnings", "debt to equity",
              "market cap", "dividend yield", "sales growth", ...)
    operator  < <= > >= =  below under "less than" "at most" "up to" above over
              "more than" "at least" "greater than" between…and  from…to  X-Y
    value     any number utils/numbers.py reads: decimals, %, 50,000 cr, 2 lakh crore
    extras    sector words ("IT", "banks", "pharma" — whole words only),
              size words ("large cap", "mid cap", "small cap"),
              "near 52-week high/low"

Every fragment the grammar couldn't place is reported in `not_understood`,
and impossible combinations ("PE below 10 and above 20") in
`contradictions` — the answer always says which filters were applied and
what was ignored, instead of silently dropping it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from utils.numbers import find_numbers

# field -> (label, unit shown to the user)
FIELDS: dict[str, tuple[str, str]] = {
    "pe": ("P/E", ""),
    "pb": ("P/B", ""),
    "roe": ("ROE", "%"),
    "roce": ("ROCE", "%"),
    "de": ("Debt/Equity", ""),
    "mcap": ("Market cap", " cr"),
    "div_yield": ("Dividend yield", "%"),
    "rev_growth": ("Revenue growth", "%"),
    "price": ("Price", ""),
    "from_52w_high": ("Distance from 52-week high", "%"),
    "from_52w_low": ("Distance from 52-week low", "%"),
}

_FIELD_ALIASES: list[tuple[str, str]] = [
    (r"price\s*(?:to|/)\s*earnings?(?:\s*ratio)?", "pe"),
    (r"p\s*/\s*e(?:\s*ratio)?|\bpe(?:\s*ratio)?\b|\bp\.e\.?", "pe"),
    (r"price\s*(?:to|/)\s*book(?:\s*value)?", "pb"),
    (r"p\s*/\s*b(?:\s*ratio)?|\bpb\b|\bp\.b\.?", "pb"),
    (r"\broce\b|return\s+on\s+capital(?:\s+employed)?", "roce"),
    (r"\broe\b|return\s+on\s+equity", "roe"),
    (r"debt\s*(?:to|/|-)\s*equity(?:\s*ratio)?|\bd\s*/\s*e\b|\bde\s*ratio\b", "de"),
    (r"market\s*cap(?:itali[sz]ation)?|\bm(?:kt)?\s*cap\b", "mcap"),
    (r"div(?:idend)?\s*yield", "div_yield"),
    (r"(?:rev(?:enue)?|sales|top\s*line)\s*growth", "rev_growth"),
    (r"(?:share\s+|stock\s+|current\s+)?price|\bcmp\b|\btrading\b", "price"),
]
_FIELD_RE = re.compile("|".join(f"(?P<f{i}>{p})" for i, (p, _) in enumerate(_FIELD_ALIASES)), re.IGNORECASE)

_LT = r"<(?!=)|below|under|less\s+than|lower\s+than|cheaper\s+than|lt\b"
_LE = r"<=|≤|=<|at\s+most|up\s*to|upto|max(?:imum)?|not\s+(?:more|above|over)\s+than|no\s+more\s+than"
_GT = r">(?!=)|above|over|more\s+than|greater\s+than|higher\s+than|exceeding|gt\b"
_GE = r">=|≥|=>|at\s+least|min(?:imum)?|not\s+less\s+than|no\s+less\s+than"
_EQ = r"=|equals?|of|is|at|around|about"
_OPS = [("<=", _LE), (">=", _GE), ("<", _LT), (">", _GT), ("=", _EQ)]
_BETWEEN = re.compile(r"\b(?:between|from|in\s+the\s+range(?:\s+of)?)\b", re.IGNORECASE)

_SECTORS: list[tuple[str, str]] = [
    (r"\bit\b|\btech(?:nology)?\b|\bsoftware\b", "Technology"),
    (r"\bbanks?\b|\bbanking\b|\bfinancials?\b|\bnbfcs?\b|\bfinance\b", "Financial Services"),
    (r"\bpharma(?:ceuticals?)?\b|\bhealth\s*care\b|\bhospitals?\b", "Healthcare"),
    (
        r"\bautos?\b|\bautomobiles?\b|\bauto\s+ancillar(?:y|ies)\b|\bconsumer\s+cyclical\b",
        "Consumer Cyclical",
    ),
    (r"\benergy\b|\boil\s*(?:and|&)\s*gas\b|\bpower\b", "Energy"),
    (r"\bmetals?\b|\bmining\b|\bsteel\b", "Metals & Mining"),
    (r"\bfmcg\b|\bconsumer\s+(?:staples|defensive)\b", "Consumer Defensive"),
    (r"\binfra(?:structure)?\b|\bindustrials?\b|\bcapital\s+goods\b", "Industrials"),
    (r"\brealty\b|\breal\s+estate\b", "Real Estate"),
    (r"\btelecom(?:munications?)?\b", "Telecom"),
]
# Config sector key -> the sector names yfinance reports for it.
YF_SECTORS: dict[str, set[str]] = {
    "Technology": {"Technology"},
    "Financial Services": {"Financial Services"},
    "Healthcare": {"Healthcare"},
    "Consumer Cyclical": {"Consumer Cyclical"},
    "Energy": {"Energy", "Utilities"},
    "Metals & Mining": {"Basic Materials"},
    "Consumer Defensive": {"Consumer Defensive"},
    "Industrials": {"Industrials"},
    "Real Estate": {"Real Estate"},
    "Telecom": {"Communication Services"},
}

_SIZES = [
    (r"\blarge[\s-]?caps?\b", "large", [("mcap", ">=", 20000)]),
    (r"\bmid[\s-]?caps?\b", "mid", [("mcap", ">=", 5000), ("mcap", "<=", 20000)]),
    (r"\bsmall[\s-]?caps?\b", "small", [("mcap", "<=", 5000)]),
]
_NEAR_52W = re.compile(
    r"\b(?:near|close\s+to|at)\s+(?:their\s+|its\s+)?52[\s-]*w(?:ee)?k\s+(high|low)s?\b", re.I
)

# Words that carry no filter meaning ("show me stocks with ...").
_FILLER = re.compile(
    r"\b(?:show|find|list|give|get|me|all|any|the|a|an|stocks?|shares?|companies|company|scrips?|names?|"
    r"with|having|has|have|whose|where|that|which|are|is|and|or|but|in|of|for|on|nse|bse|india(?:n)?|"
    r"screen(?:er)?|filter|please|ratio|value|percent|%|cr|crore|crores|rs|inr|₹|sector|stocks?)\b"
    r"|[,;.?!:()&/\-]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScreenFilter:
    field: str
    op: str  # "<", "<=", ">", ">=", "=", "between"
    value: float
    high: float | None = None  # upper bound for "between"

    def describe(self) -> str:
        label, unit = FIELDS[self.field]
        if self.op == "between":
            return f"{label} between {_fmt(self.value)}{unit} and {_fmt(self.high)}{unit}"
        return f"{label} {self.op} {_fmt(self.value)}{unit}"


@dataclass
class Screen:
    filters: list[ScreenFilter] = field(default_factory=list)
    sector: str | None = None
    size: str | None = None
    not_understood: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    source: str = "grammar"

    def as_dict(self) -> dict[str, Any]:
        return {
            "filters": [asdict(f) for f in self.filters],
            "sector": self.sector,
            "size": self.size,
            "not_understood": list(self.not_understood),
            "contradictions": list(self.contradictions),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Screen:
        d = d or {}
        return cls(
            filters=[ScreenFilter(**f) for f in d.get("filters") or []],
            sector=d.get("sector"),
            size=d.get("size"),
            not_understood=list(d.get("not_understood") or []),
            contradictions=list(d.get("contradictions") or []),
            source=d.get("source", "grammar"),
        )

    def summary(self) -> str:
        """'Filters applied: … / Not understood: …' — always shown with results."""
        applied = [f.describe() for f in self.filters]
        if self.sector:
            applied.insert(0, f"sector: {self.sector}")
        lines = [f"**Filters applied:** {'; '.join(applied) if applied else 'none'}"]
        if self.not_understood:
            lines.append(f"**Not understood (ignored):** {', '.join(repr(t) for t in self.not_understood)}")
        if self.contradictions:
            lines.append(f"**Can't be satisfied:** {'; '.join(self.contradictions)}")
        return "\n\n".join(lines)


def _fmt(v: float | None) -> str:
    if v is None:
        return ""
    return f"{v:,.0f}" if float(v).is_integer() and abs(v) >= 1000 else f"{v:g}"


def _op_before(text: str) -> str | None:
    """The comparator at the end of `text` (the words between a field and its number)."""
    t = text.strip().lower()
    for op, pattern in _OPS:
        if re.search(rf"(?:{pattern})\s*$", t, re.IGNORECASE):
            return op
    return None


def _value(field_name: str, num: Any) -> float:
    """A parsed number in the field's unit: market cap in ₹ crore."""
    v = Decimal(num.value)
    if field_name == "mcap" and num.unit:  # "50,000 cr" -> 50,000; "2 lakh crore" -> 200,000
        v = v / Decimal(10**7)
    return float(v)


def parse(query: str) -> Screen:
    text = query or ""
    screen = Screen()
    used: list[tuple[int, int]] = []

    for pattern, name in _SECTORS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            screen.sector = name
            used.append(m.span())
            break

    raw: list[tuple[str, str, float, float | None]] = []
    for pattern, size, bounds in _SIZES:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            screen.size = size
            used.append(m.span())
            raw.extend((f, op, v, None) for f, op, v in bounds)
            break

    near = _NEAR_52W.search(text)
    if near:
        used.append(near.span())
        raw.append(("from_52w_" + near.group(1).lower(), "<=", 5.0, None))

    fields = list(_FIELD_RE.finditer(text))
    numbers = find_numbers(text)
    for idx, fm in enumerate(fields):
        fname = next(name for i, (_, name) in enumerate(_FIELD_ALIASES) if fm.group(f"f{i}"))
        stop = fields[idx + 1].start() if idx + 1 < len(fields) else len(text)
        window = [n for n in numbers if fm.end() <= n.span[0] < stop]
        op_text = ""
        gap_start = fm.end()
        if not window:
            continue  # a field with no number: left for not_understood
        first = window[0]
        between_text = text[gap_start : first.span[0]]
        used.append(fm.span())
        if _BETWEEN.search(between_text) and len(window) >= 2:
            lo, hi = sorted((_value(fname, window[0]), _value(fname, window[1])))
            raw.append((fname, "between", lo, hi))
            used.extend([first.span, window[1].span, (gap_start, window[1].span[0])])
            continue
        dash = False
        if len(window) >= 2:
            gap = text[first.span[1] : window[1].span[0]]
            # "10-20" reads as 10 and -20: a minus glued to the first number is a range dash.
            dash = bool(re.fullmatch(r"\s*(?:-|–|to)\s*", gap)) or (
                gap == "" and text[window[1].span[0]] in "-–−"
            )
        if dash and not _op_before(op_text + " " + between_text):
            lo, hi = sorted((_value(fname, window[0]), abs(_value(fname, window[1]))))
            raw.append((fname, "between", lo, hi))
            used.extend([first.span, window[1].span, (gap_start, window[1].span[0])])
            continue
        op = _op_before(op_text + " " + between_text)
        if op is None:
            continue
        raw.append((fname, op, _value(fname, first), None))
        used.extend([first.span, (gap_start, first.span[0])])

    for fname, op, value, high in raw:
        screen.filters.append(ScreenFilter(fname, op, value, high))
    screen.contradictions = _contradictions(screen.filters)
    screen.not_understood = _leftovers(text, used)
    return screen


def _contradictions(filters: list[ScreenFilter]) -> list[str]:
    """Fields whose combined bounds leave no possible value."""
    out = []
    by_field: dict[str, list[ScreenFilter]] = {}
    for f in filters:
        by_field.setdefault(f.field, []).append(f)
    for name, fs in by_field.items():
        lo, lo_strict, hi, hi_strict = float("-inf"), False, float("inf"), False
        for f in fs:
            lows = [(f.value, f.op == ">")] if f.op in (">", ">=") else []
            highs = [(f.value, f.op == "<")] if f.op in ("<", "<=") else []
            if f.op == "between":
                lows, highs = [(f.value, False)], [(f.high or f.value, False)]
            if f.op == "=":
                lows, highs = [(f.value, False)], [(f.value, False)]
            for v, strict in lows:
                if v > lo or (v == lo and strict):
                    lo, lo_strict = v, strict
            for v, strict in highs:
                if v < hi or (v == hi and strict):
                    hi, hi_strict = v, strict
        if lo > hi or (lo == hi and (lo_strict or hi_strict)):
            out.append(f"{FIELDS[name][0]}: " + " and ".join(f.describe() for f in fs))
    return out


def _leftovers(text: str, used: list[tuple[int, int]]) -> list[str]:
    mask = list(text)
    for a, b in used:
        for i in range(max(a, 0), min(b, len(mask))):
            mask[i] = " "
    remaining = _FILLER.sub(" ", "".join(mask))
    return [frag for frag in (" ".join(p.split()) for p in re.split(r"\s{2,}", remaining)) if frag]
