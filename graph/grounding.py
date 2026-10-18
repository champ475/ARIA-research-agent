"""
graph/grounding.py — post-generation numeric grounding (LLM-03).

"Never introduce a number that isn't in the conclusions" used to be only an
instruction. Now every figure in an answer is checked against the text the
model was actually given (the prompt: conclusions, rendered evidence,
history, the user's own message). A figure is supported if it matches a
source number:
- exactly, or within rounding (±0.5% relative, or half the last displayed
  digit: "24" matches 24.3);
- after a unit change (lakh / crore / k, rupees in crore, ratio <-> percent);
- or as a simple derivation of two source numbers (difference, ratio,
  % change, "x% of y").
Dates, years, ordinals, citation markers and indicator periods ("RSI(14)",
"50-day", "5-year") are not market figures and aren't checked. Small bare
integers (< 100, no unit) are counts and are skipped too.

`check(text, sources)` returns the unsupported figures. CommunicationLayer
regenerates once on failure (sync path); anything still unsupported — and
anything on the streamed path — gets a visible note, and the result is
recorded on the turn (turns.grounding).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import prompts
from utils.numbers import UNIT_MULTIPLIERS

log = logging.getLogger(__name__)

# Same multipliers as every other parser (VAL-09: one source of truth).
_UNITS = {unit: float(mult) for unit, mult in UNIT_MULTIPLIERS.items()}
_NUMBER = re.compile(
    r"(?P<cur>₹\s?|rs\.?\s?|inr\s?)?"
    r"(?<![\w.^#/])(?P<num>-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?![\w.]*\d)"
    r"(?P<pct>\s?%)?"
    r"(?:\s?(?P<unit>lakh\s+crores?|crores?|cr|lakhs?|lacs?|bn|billion|mn|million|k|l|x|times)\b)?",
    re.IGNORECASE,
)
_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_AFTER_NOT_A_FIGURE = re.compile(
    r"^\s?-?\s?(?:day|days|week|weeks|wk|month|months|year|years|yr|yrs|dma|sma|ema|period|quarter|quarters|"
    r"hour|hours|minute|minutes|st|nd|rd|th|" + _MONTHS + r")\b",
    re.IGNORECASE,
)
_BEFORE_NOT_A_FIGURE = re.compile(
    r"(?:\b(?:q|h|fy|fy\s|rsi\s?\(|macd\s?\(|step|no\.?|#|alert\s?#?|top|page|item|" + _MONTHS + r")\s?)$",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^\s*(?:[-*]\s*)?\d+[.)]\s")
# A small bare integer is usually a count ("3 funds") — unless a metric word
# sits right before it ("a P/E of 99", "RSI at 72", "target 95").
_METRIC_BEFORE = re.compile(
    r"\b(?:p\s*/?\s*e|p\s*/?\s*b|pe|pb|roe|roce|rsi|eps|beta|ratio|yield|margin|multiple|target|price|nav|score)\b[^\d]{0,12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    raw: str
    value: float  # with unit multiplier applied
    number: float  # as written
    decimals: int
    percent: bool
    rupees: bool


@dataclass
class GroundingResult:
    checked: int = 0
    unsupported: list[str] = field(default_factory=list)
    regenerated: bool = False

    @property
    def ok(self) -> bool:
        return not self.unsupported

    def as_dict(self) -> dict:
        return {"checked": self.checked, "unsupported": self.unsupported, "regenerated": self.regenerated}


def _parse(m: re.Match) -> Figure | None:
    raw_num = m.group("num").replace(",", "")
    try:
        number = float(raw_num)
    except ValueError:
        return None
    unit = " ".join((m.group("unit") or "").lower().split())
    mult = _UNITS.get(unit, 1.0)
    decimals = len(raw_num.split(".")[1]) if "." in raw_num else 0
    return Figure(
        raw=m.group(0).strip(),
        value=number * mult,
        number=number,
        decimals=decimals,
        percent=bool(m.group("pct")),
        rupees=bool(m.group("cur"))
        or unit
        in ("crore", "crores", "cr", "lakh", "lakhs", "lac", "lacs", "l", "lakh crore", "lakh crores"),
    )


def extract_figures(text: str, *, all_numbers: bool = False) -> list[Figure]:
    """Figures worth checking in `text` (or, with all_numbers, every number —
    used for the source side)."""
    figures: list[Figure] = []
    for line in (text or "").split("\n"):
        # A table separator or a "1." list marker isn't a figure.
        line_body = _LIST_MARKER.sub(" ", line) if not all_numbers else line
        for m in _NUMBER.finditer(line_body):
            fig = _parse(m)
            if fig is None:
                continue
            if all_numbers:
                figures.append(fig)
                continue
            before = line_body[max(0, m.start() - 12) : m.start()]
            after = line_body[m.end() : m.end() + 12]
            if _BEFORE_NOT_A_FIGURE.search(before) or _AFTER_NOT_A_FIGURE.search(after):
                continue
            if before.rstrip().endswith("[^") or before[-1:] == "(" and after[:1] == ")":
                continue
            if not fig.percent and not fig.rupees and fig.decimals == 0 and 1900 <= fig.number <= 2100:
                continue  # a year
            if (
                not fig.percent
                and not fig.rupees
                and fig.decimals == 0
                and abs(fig.number) < 100
                and not _METRIC_BEFORE.search(line_body[max(0, m.start() - 30) : m.start()])
            ):
                continue  # a count / small integer
            figures.append(fig)
    return figures


# Unit changes a source number may need: ratio <-> percent, rupees <-> lakh / crore.
_SCALES = (1.0, 100.0, 0.01, 1e5, 1e-5, 1e7, 1e-7)


def _close(fig: Figure, candidate: float, scales: tuple[float, ...] = _SCALES) -> bool:
    tol_digit = 0.5 * 10 ** (-fig.decimals)
    for target in (fig.value, fig.number):
        for scale in scales:
            c = candidate * scale
            if (
                abs(target - c)
                <= max(abs(c) * 0.005, tol_digit * (target / fig.number if fig.number else 1)) + 1e-9
            ):
                return True
    return False


def _source_pool(sources: str, limit: int = 60) -> tuple[list[float], list[float]]:
    """Source numbers, and the few derivations an answer may legitimately
    state. Derivations are deliberately narrow: with dozens of numbers, every
    pairwise difference/ratio would make almost any figure "derivable"."""
    figures = extract_figures(sources, all_numbers=True)
    values: list[float] = []
    seen: set[float] = set()
    for fig in figures:
        for v in (fig.value, fig.number):
            if v not in seen:
                seen.add(v)
                values.append(v)
    pct_values = {f.number for f in figures if f.percent}
    base = [
        f.value
        for f in figures
        if not (f.decimals == 0 and not f.percent and 1900 <= f.number <= 2100) and abs(f.value) >= 1e-9
    ][:limit]
    derived: list[float] = []
    for i, a in enumerate(base):
        for b in base[i + 1 :]:
            if abs(a) and abs(b) and 0.1 <= abs(a / b) <= 10:  # same kind of quantity (two prices, two NAVs)
                derived += [a - b, b - a, (a - b) / abs(b) * 100, (b - a) / abs(a) * 100]
    for p in pct_values:  # "x% of y"
        derived += [p * v / 100 for v in base]
    return values, derived


_PLANNING_RETURN_CONTEXT = re.compile(
    r"\b(returns?|cagr|yield|grew|growth|inflation|delivered|gained|fell|nav|p\.a\.|per\s+annum|interest)\b",
    re.IGNORECASE,
)


_ILLUSTRATIVE = re.compile(
    r"\b(?:for\s+example|for\s+instance|e\.g\.|example|suppose|imagine|hypothetical(?:ly)?|illustrat\w+)\b",
    re.IGNORECASE,
)


def _is_concept_prompt(sources: str) -> bool:
    """True for a no-live-data answer (a concept question): nothing was fetched, so the only
    figures it can carry are worked examples."""
    from graph.data_questions import NO_LIVE_DATA_INSTRUCTION

    return bool(sources) and sources.lstrip().startswith(NO_LIVE_DATA_INSTRUCTION.strip()[:60])


def check(text: str, sources: str, *, mode: str = "default") -> GroundingResult:
    """Which figures in `text` aren't supported by `sources`. mode="planning"
    checks rupee amounts and return/performance claims only — the allocation
    percentages of a model portfolio are its design, not data claims."""
    values, derived = _source_pool(sources)
    result = GroundingResult()
    concept = _is_concept_prompt(sources)
    # A worked example in a concept answer ("Suppose a share trades at ₹500...") is not a data
    # claim: skip the paragraphs that announce one. Elsewhere every figure is checked.
    paragraphs = [p for p in re.split(r"\n\s*\n", text or "") if not (concept and _ILLUSTRATIVE.search(p))]
    sentences = [s for p in paragraphs for s in re.split(r"(?<=[.!?])\s+|\n", p)]
    for sentence in sentences:
        for fig in extract_figures(sentence):
            if mode == "planning" and fig.percent and not _PLANNING_RETURN_CONTEXT.search(sentence):
                continue
            result.checked += 1
            # Derivations are compared as-is: rescaling them too would let a
            # 5.0% move "support" a ₹5,000 figure.
            if any(_close(fig, v) for v in values) or any(_close(fig, d, (1.0,)) for d in derived):
                continue
            if fig.raw not in result.unsupported:
                result.unsupported.append(fig.raw)
    return result


REGENERATE_INSTRUCTION = prompts.get("grounding.regenerate")


def unverified_note(unsupported: list[str]) -> str:
    shown = ", ".join(unsupported[:8]) + (" …" if len(unsupported) > 8 else "")
    return (
        f"\n\n⚠️ *Unverified figures: {shown} — these weren't found in the data ARIA fetched for this "
        "answer; treat them with caution.*"
    )


def record(result: GroundingResult) -> None:
    """Attach the result to the current turn (turns.grounding) and log violations."""
    from utils import tracing

    rec = tracing.current()
    if rec is not None:
        rec.grounding = result.as_dict()
    if result.unsupported:
        log.warning(f"[grounding] {len(result.unsupported)} unsupported figure(s): {result.unsupported[:8]}")
