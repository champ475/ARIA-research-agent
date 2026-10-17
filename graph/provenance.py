"""
graph/provenance.py — where an answer's numbers came from (UX-07).

A deterministic addition, written by code and never asked of the model: a
collapsible "Sources" block under the answer that lists every piece of
evidence the conclusions rest on, with its source, when it is as of (IST) and
how much to trust it, and why when that is lower than full. It is also what
the "Cited calculations" footnotes point back to (`footnote_source`).

Rendered as a Markdown `<details>` block, which Gradio's chat keeps after
sanitising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from utils.escaping import md_cell
from utils.helpers import IST

# tool name (or prefix) -> where its data comes from, in words a user knows.
_SOURCES: tuple[tuple[str, str], ...] = (
    ("fetch_news", "Publisher RSS feeds and Google News"),
    ("fetch_mf_news", "Publisher RSS feeds and Google News"),
    ("news_sentiment", "Publisher RSS feeds and Google News (VADER sentiment)"),
    ("fetch_nav", "AMFI NAV file and mfapi.in"),
    ("fetch_category_funds", "AMFI NAV file and mfapi.in"),
    ("fetch_mf_comparison", "AMFI NAV file and mfapi.in"),
    ("fetch_bulk_deals", "NSE India"),
    ("fetch_block_deals", "NSE India"),
    ("fetch_top_movers", "NSE India"),
    ("fetch_market_overview", "Yahoo Finance index quotes"),
    ("fetch_quarterly_results", "NSE India filings"),
    ("fetch_annual_report_text", "NSE India filings"),
    ("screener_", "ARIA's nightly screener snapshot (Yahoo Finance data)"),
    ("alert_", "Your ARIA alerts"),
    ("portfolio_view", "Your ARIA portfolio, priced with Yahoo Finance"),
    ("fetch_", "Yahoo Finance (NSE end-of-day file if unavailable)"),
    ("compute_", "Yahoo Finance data, calculated by ARIA"),
    ("peer_comparison", "Yahoo Finance data, calculated by ARIA"),
    ("risk_assessment", "Yahoo Finance price history, calculated by ARIA"),
    ("backtest", "Yahoo Finance price history, calculated by ARIA"),
)

_LABELS = {
    "quote_info": "Quote and company info",
    "raw_financials": "Financial statements",
    "price_ohlcv": "Price history",
    "news_headline": "News headlines",
    "fundamental_ratio": "Fundamental ratios",
    "technical_indicator": "Technical indicators",
    "pdf_extract": "Filing text",
    "mf_nav": "Fund NAV",
    "mf_category_funds": "Fund rankings",
    "mf_comparison": "Fund comparison",
    "screener_results": "Screener results",
    "top_movers": "Top movers",
    "bulk_deals": "Bulk deals",
    "block_deals": "Block deals",
    "peer_comparison": "Peer comparison",
    "risk_profile": "Risk profile",
    "backtest_result": "Backtest",
    "portfolio": "Portfolio valuation",
    "alert_list": "Your alerts",
}


def source_of(tool: str) -> str:
    for prefix, text in _SOURCES:
        if tool == prefix or (prefix.endswith("_") and tool.startswith(prefix)):
            return text
    return "ARIA"


def confidence_text(confidence: float) -> str:
    """'full' or 'reduced (60%): a fallback source stood in for the usual one'."""
    if confidence >= 0.999:
        return "full"
    return f"reduced ({confidence:.0%}): the usual source was unavailable, so a fallback stood in"


def _when(ev: dict[str, Any]) -> str:
    ts = ev.get("as_of") or ev.get("timestamp")
    if not isinstance(ts, datetime):
        return "—"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(IST).strftime("%d %b %Y, %H:%M IST")


def used_evidence(state: dict) -> list[dict]:
    """The evidence the conclusions cite, in order of first use. Falls back to
    all evidence when there are no conclusions to follow."""
    evidence = state.get("evidence") or []
    by_id = {e.get("id"): e for e in evidence}
    out, seen = [], set()
    for c in state.get("conclusions") or []:
        for eid in c.get("evidence_ids") or []:
            if eid in by_id and eid not in seen:
                seen.add(eid)
                out.append(by_id[eid])
    return out


def sources_block(state: dict) -> str:
    """The collapsible Sources block, or '' when nothing was fetched."""
    items = used_evidence(state)
    if not items:
        return ""
    lines = [
        "",
        "",
        f"<details><summary>Sources ({len(items)})</summary>",
        "",
        "| Data | Source | As of | Confidence |",
        "|---|---|---|---|",
    ]
    for ev in items:
        label = _LABELS.get(ev.get("evidence_type", ""), str(ev.get("evidence_type", "")).replace("_", " "))
        entity = ev.get("entity")
        what = f"{label} · {entity}" if entity and entity != "market" else label
        lines.append(
            f"| {md_cell(what)} | {md_cell(source_of(str(ev.get('tool', ''))))} | {_when(ev)} | "
            f"{md_cell(confidence_text(float(ev.get('confidence', 1.0))))} |"
        )
    lines += ["", "</details>"]
    return "\n".join(lines)


def footnote_source(state: dict, entity: str | None) -> str:
    """One line for under the "Cited calculations": where the ratios came from and
    when, for the entity they are about. '' if the evidence isn't there."""
    for ev in state.get("evidence") or []:
        if ev.get("evidence_type") == "fundamental_ratio" and (entity is None or ev.get("entity") == entity):
            conf = float(ev.get("confidence", 1.0))
            return f"\n*Source: {source_of(str(ev.get('tool', '')))} · as of {_when(ev)} · confidence {confidence_text(conf)}*\n"
    return ""
