"""
graph/freshness.py — how fresh an answer's market figures are (DATA-06).

Two deterministic additions, written by code, never by the model:
- `freshness_note`: "🕒 *Data as of 15 Oct 2026, 15:31 IST (market closed)*",
  dated by the OLDEST market Evidence in the answer — the honest bound.
- `closed_today_header`: on a weekend or NSE holiday, a quote or movers
  answer opens with "Markets are closed today (Dussehra); these figures are
  from the last session, Mon, 19 Oct 2026."
"""

from __future__ import annotations

from datetime import datetime

from utils.market_clock import as_of_line, closed_today_note

# Evidence whose numbers move with the market.
MARKET_EVIDENCE = frozenset(
    {
        "quote_info",
        "price_ohlcv",
        "technical_indicator",
        "fundamental_ratio",
        "top_movers",
        "screener_results",
        "screener_table",
        "bulk_deals",
        "block_deals",
    }
)
# "Today" figures: on a closed day these are the last session's.
_TODAY_EVIDENCE = frozenset({"quote_info", "top_movers"})


def _market_evidence(state: dict) -> list[dict]:
    return [e for e in state.get("evidence") or [] if e.get("evidence_type") in MARKET_EVIDENCE]


def answer_as_of(state: dict) -> datetime | None:
    times = [e.get("as_of") or e.get("timestamp") for e in _market_evidence(state)]
    stamps = [t for t in times if isinstance(t, datetime)]
    return min(stamps) if stamps else None


def freshness_note(state: dict, now: datetime | None = None) -> str:
    line = as_of_line(answer_as_of(state), now)
    return f"\n\n🕒 *{line}*" if line else ""


def closed_today_header(state: dict, now: datetime | None = None) -> str:
    if not any(e.get("evidence_type") in _TODAY_EVIDENCE for e in _market_evidence(state)):
        return ""
    note = closed_today_note(now)
    return f"📅 *{note}*\n\n" if note else ""
