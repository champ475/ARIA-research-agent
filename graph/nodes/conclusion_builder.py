"""
ConclusionBuilder node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). Deterministic —
no LLM call. Turns verified Evidence into Conclusion objects: this is where
"what is true" gets decided, by evidence and arithmetic, not by a model.
Mirrors tools/analysis/fundamental.py's existing practice of recomputing
ratios from raw financials rather than trusting an upstream number —
extends that discipline to the whole report instead of just fundamentals.

Evidence referenced by an unresolved VerificationIssue is excluded: "verified
evidence only" per the RFC means a Conclusion can't be built from something
Verification just flagged as unsupported/contradictory/low-confidence,
even if the loop budget ran out before it could be resolved.

Numeric fields matching fundamental.py's five citable ratios (pe/pb/roce/
de/cr) get that exact citation_key, so CommunicationLayer (next commit) can
reuse cited_footnotes() unchanged. Other numeric fields get a citation_key
of their own field name — a Conclusion, just without a footnote lookup.
"""

from __future__ import annotations

import pandas as pd

from graph.journal import log_decision
from graph.nodes.execute_task import from_cacheable
from graph.quote_fields import display_value, quote_rows
from graph.state import Conclusion, Evidence, GraphState
from utils.helpers import trim_text

# Matches tools/analysis/fundamental.py's _CITATION_FIELDS keys exactly.
CANONICAL_RATIO_KEYS = {"pe", "pb", "roce", "de", "cr"}

# Evidence types whose numeric payload fields are worth a Conclusion each.
_NUMERIC_EVIDENCE_TYPES = {"fundamental_ratio", "quote_info", "technical_indicator"}

# Report/mf/screener-domain free-text payloads (quarterly/annual filings,
# fund comparisons, a rendered screener results table) — not numeric, but
# still worth a Conclusion so CommunicationLayer (which only ever reads
# conclusions, never raw evidence) can reference them. screener_table in
# particular is the RFC §18 cross-entity comparison need's actual fix: since
# screener_run/screener_to_markdown treat the whole matching stock set as
# ONE evidence item (not one Evidence per stock), the full comparison table
# reaches CommunicationLayer as a single coherent Conclusion — no per-entity
# tagging/grouping machinery needed, because the fragmentation problem the
# RFC worried about (Planner cross-producting tool x entity, one Evidence
# per entity) doesn't apply here: screener's entity is the synthetic
# NO_ENTITY_SENTINEL, and the tool itself returns every matching row at once.
_TEXT_EVIDENCE_TYPES = {"pdf_extract", "mf_comparison", "screener_table"}
_NOT_AVAILABLE_MARKERS = ("no ",)  # tools/data/pdf_scraper.py's "not found" convention

# UX-04/UX-05 analysis tools (graph/analysis_tools.py): payload["claims"].
_ANALYSIS_EVIDENCE_TYPES = {
    "peer_comparison",
    "risk_profile",
    "news_sentiment",
    "backtest_result",
    "portfolio",
    "market_overview",
}

# DataFrame payloads worth a match-count Conclusion, same idea as
# news_headline's count-based Conclusion — covers mf (NAV search/category
# listing) and screener/market-data domains (movers, bulk/block deals).
_DATAFRAME_EVIDENCE_TYPES = {
    "mf_nav": "matching mutual fund(s) found",
    "mf_category_funds": "matching mutual fund(s) found",
    "top_movers": "top mover(s) on record",
    "bulk_deals": "bulk deal(s) on record",
    "block_deals": "block deal(s) on record",
}


# Computed fundamentals (tools/analysis/fundamental.py): how each field reads to a user.
# unit: "x" a plain multiple, "frac" a decimal shown as a percentage, "pct" already a
# percentage, "inr" a rupee amount, "crore" an amount shown in rupees crore.
_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "pe": ("P/E (trailing)", "x"),
    "pb": ("P/B", "x"),
    "ps": ("Price/Sales", "x"),
    "ev_ebitda": ("EV/EBITDA", "x"),
    "peg": ("PEG", "x"),
    "de": ("Debt/Equity", "x"),
    "current_ratio": ("Current ratio", "x"),
    "quick_ratio": ("Quick ratio", "x"),
    "beta": ("Beta", "x"),
    "roe": ("ROE", "frac"),
    "roa": ("ROA", "frac"),
    "net_margin": ("Net margin", "frac"),
    "op_margin": ("Operating margin", "frac"),
    "ebitda_margin": ("EBITDA margin", "frac"),
    "gross_margin": ("Gross margin", "frac"),
    "payout": ("Payout ratio", "frac"),
    "rev_growth": ("Revenue growth (YoY)", "frac"),
    "earn_growth": ("Earnings growth (YoY)", "frac"),
    "nim_proxy": ("Net interest margin (proxy: net interest income / assets)", "frac"),
    "roce": ("ROCE", "pct"),
    "div_yield": ("Dividend yield", "pct"),
    "analyst_upside": ("Analyst upside to mean target", "pct"),
    "eps": ("EPS (TTM)", "inr"),
    "book_value": ("Book value per share", "inr"),
    "price": ("Price", "inr"),
    "target_mean": ("Analyst mean target", "inr"),
    "target_high": ("Analyst high target", "inr"),
    "target_low": ("Analyst low target", "inr"),
    "w52_high": ("52-week high", "inr"),
    "w52_low": ("52-week low", "inr"),
    "mktcap": ("Market cap", "crore"),
    "ev": ("Enterprise value", "crore"),
    "revenue": ("Revenue (latest year)", "crore"),
    "net_income": ("Net income (latest year)", "crore"),
    "ebitda": ("EBITDA (latest year)", "crore"),
    "ebit": ("EBIT (latest year)", "crore"),
    "fcf": ("Free cash flow (latest year)", "crore"),
    "operating_cf": ("Operating cash flow (latest year)", "crore"),
    "total_assets": ("Total assets", "crore"),
    "total_equity": ("Shareholders' equity", "crore"),
    "total_debt": ("Total debt", "crore"),
    "cash": ("Cash and equivalents", "crore"),
}
# Fields the quote already carries (graph/quote_fields.py), so a second copy would
# only repeat it, and where the two disagree (two ROE figures) the reported one wins.
_QUOTE_TWIN = {
    "pe": "trailingPE",
    "pb": "priceToBook",
    "eps": "trailingEps",
    "book_value": "bookValue",
    "roe": "returnOnEquity",
    "net_margin": "profitMargins",
    "rev_growth": "revenueGrowth",
    "earn_growth": "earningsGrowth",
    "price": "currentPrice",
    "mktcap": "marketCap",
    "beta": "beta",
    "div_yield": "trailingAnnualDividendYield",
    "target_mean": "targetMeanPrice",
    "w52_high": "fiftyTwoWeekHigh",
    "w52_low": "fiftyTwoWeekLow",
}
# Numbers in the payload that are not findings (headcount, counts, volume, cached text sizes).
_TRIVIA_FIELDS = frozenset({"employees", "n_analysts", "volume", "mktcap_fmt", "revenue_fmt"})
_CANONICAL_ALWAYS = frozenset(CANONICAL_RATIO_KEYS)  # their [^key] footnotes need them


def _shown(field: str, value: float) -> str:
    _label, unit = _FIELD_LABELS[field]
    if unit == "frac":
        return f"{value * 100:.2f}%"
    if unit == "pct":
        return f"{value:.2f}%"
    if unit == "inr":
        return f"₹{value:,.2f}"
    if unit == "crore":
        return f"₹{value / 1e7:,.2f} crore"
    return f"{value:,.2f}"


def _quote_twins(evidence: list[Evidence]) -> dict[str, set[str]]:
    """entity -> the quote fields that arrived usable (so their computed twins are redundant)."""
    out: dict[str, set[str]] = {}
    for ev in evidence:
        if ev["evidence_type"] == "quote_info" and isinstance(ev["payload"], dict):
            out.setdefault(ev["entity"], set()).update(f for f, *_ in quote_rows(ev["payload"]))
    return out


_PCT_COLUMNS = ("perchange", "pchange", "pct_change", "pct", "change_pct")


def _mover_leaders(df: pd.DataFrame, top: int = 3) -> str:
    """ "TECHM +4.2%, TCS +3.9%, INFY +3.1%" from a movers table, or "" when it has no % column."""
    lower = {str(c).lower(): c for c in df.columns}
    pct_col = next((lower[c] for c in _PCT_COLUMNS if c in lower), None)
    if pct_col is None or "symbol" not in lower:
        return ""
    rows = df.dropna(subset=[pct_col]).head(top)
    return ", ".join(f"{r[lower['symbol']]} {float(r[pct_col]):+.2f}%" for _, r in rows.iterrows())


def _numeric_fields(payload) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    return {
        k: v
        for k, v in payload.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # excludes NaN
    }


def _flagged_evidence_ids(verification_issues: list[dict]) -> set[str]:
    flagged: set[str] = set()
    for issue in verification_issues or []:
        if issue.get("severity", "hard") == "soft":
            continue  # unconfirmed by the judge, not wrong: the data still goes in
        flagged.update(issue.get("evidence_ids", []))
    return flagged


def _conclusions_from_evidence(ev: Evidence, quote_fields: set[str] | None = None) -> list[Conclusion]:
    conclusions: list[Conclusion] = []

    if ev["evidence_type"] == "quote_info":
        # LLM-04: only the ~20 allowlisted quote fields, labelled, in ₹/₹ crore/%.
        for field, label, value, unit in quote_rows(ev["payload"]):
            shown = display_value(value, unit)
            conclusions.append(
                {
                    "claim": f"{ev['entity']} {label} = {shown}",
                    "value": value,
                    "confidence": ev["confidence"],
                    "evidence_ids": [ev["id"]],
                    "citation_key": f"{ev['entity']}.{field}",
                }
            )

    elif ev["evidence_type"] in _NUMERIC_EVIDENCE_TYPES:
        for field, value in _numeric_fields(ev["payload"]).items():
            if ev["evidence_type"] == "fundamental_ratio":
                if field in _TRIVIA_FIELDS:
                    continue
                twin = _QUOTE_TWIN.get(field)
                if (
                    twin
                    and twin in (quote_fields or ())
                    and (field not in _CANONICAL_ALWAYS or field == "roe")
                ):
                    continue  # the quote already says it (and its ROE is the reported one)
            citation_key = field if field in CANONICAL_RATIO_KEYS else f"{ev['entity']}.{field}"
            if ev["evidence_type"] == "fundamental_ratio" and field in _FIELD_LABELS:
                claim = f"{ev['entity']} {_FIELD_LABELS[field][0]} = {_shown(field, value)}"
            else:
                claim = f"{ev['entity']} {field} = {value}"
            conclusions.append(
                {
                    "claim": claim,
                    "value": value,
                    "confidence": ev["confidence"],
                    "evidence_ids": [ev["id"]],
                    "citation_key": citation_key,
                }
            )

    elif ev["evidence_type"] in _ANALYSIS_EVIDENCE_TYPES and isinstance(ev["payload"], dict):
        # UX-04: the analysis tools write their own deterministic claims.
        for c in ev["payload"].get("claims") or []:
            conclusions.append(
                {
                    "claim": c["claim"],
                    "value": c.get("value"),
                    "confidence": ev["confidence"],
                    "evidence_ids": [ev["id"]],
                    "citation_key": f"{ev['entity']}.{c.get('key') or ev['tool']}",
                }
            )

    elif ev["evidence_type"] == "news_headline" and isinstance(ev["payload"], list):
        conclusions.append(
            {
                "claim": f"{ev['entity']} has {len(ev['payload'])} recent news headline(s) on record",
                "value": len(ev["payload"]),
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": f"{ev['entity']}.news_count",
            }
        )

    elif ev["evidence_type"] in _TEXT_EVIDENCE_TYPES and isinstance(ev["payload"], str):
        text = ev["payload"]
        if text and not text.lower().startswith(_NOT_AVAILABLE_MARKERS):
            conclusions.append(
                {
                    "claim": f"{ev['entity']} {ev['tool']}: {trim_text(text, 150)}",
                    "value": text,
                    "confidence": ev["confidence"],
                    "evidence_ids": [ev["id"]],
                    "citation_key": f"{ev['entity']}.{ev['tool']}",
                }
            )

    elif ev["evidence_type"] in _DATAFRAME_EVIDENCE_TYPES and isinstance(ev["payload"], pd.DataFrame):
        n = len(ev["payload"])
        label = _DATAFRAME_EVIDENCE_TYPES[ev["evidence_type"]]
        df = ev["payload"]
        # DATA-04: a ranked fund list states how it was ranked, so the answer can too.
        if "ranking_method" in df.columns and n:
            label = f"{label} — {df['ranking_method'].iloc[0]}"
        conclusions.append(
            {
                "claim": f"{ev['entity']}: {n} {label}",
                "value": n,
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": f"{ev['entity']}.{ev['evidence_type']}",
            }
        )
        if ev["evidence_type"] == "top_movers":
            leaders = _mover_leaders(df)
            if leaders:
                conclusions.append(
                    {
                        "claim": f"{ev['entity']}: biggest moves are {leaders}",
                        "value": leaders,
                        "confidence": ev["confidence"],
                        "evidence_ids": [ev["id"]],
                        "citation_key": f"{ev['entity']}.movers_leaders",
                    }
                )

    elif ev["evidence_type"] == "screener_saved" and isinstance(ev["payload"], dict):
        p = ev["payload"]
        claim = (
            f"Screen NOT saved — {p.get('reason')}"
            if p.get("result") == "blocked"
            else f"Screener saved as '{p.get('name')}' with filters: {p.get('filters')}"
        )
        conclusions.append(
            {
                "claim": claim,
                "value": p,
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": f"{ev['entity']}.screener_saved",
            }
        )

    elif ev["evidence_type"] == "alert_list" and isinstance(ev["payload"], list):
        conclusions.append(
            {
                "claim": f"You have {len(ev['payload'])} alert(s) on record",
                "value": len(ev["payload"]),
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": "alerts.count",
            }
        )

    elif ev["evidence_type"] == "alert_action" and isinstance(ev["payload"], dict):
        p = ev["payload"]
        conclusions.append(
            {
                "claim": _alert_action_claim(p),
                "value": p,
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": f"{ev['entity']}.alert_{p.get('action')}",
            }
        )

    return conclusions


_ALERT_DONE_VERB = {"created": "Created", "removed": "Cancelled", "paused": "Paused", "resumed": "Resumed"}
_ALERT_FAIL_TEXT = {
    "not_found": "no matching alert was found — say which one, e.g. 'cancel alert #12'",
    "unrecognized_condition": "the alert condition couldn't be understood",
    "invalid_symbol": "no valid stock symbol was given",
}


def _condition_text(p: dict) -> str:
    """AlertSpec.describe() already starts with "<SYMBOL>: "; don't repeat it."""
    condition = p.get("condition") or ""
    prefix = f"{p.get('symbol')}: "
    return condition[len(prefix) :] if condition.startswith(prefix) else condition


def _alert_action_claim(p: dict) -> str:
    """Every alert write is echoed back in plain words (SEC-03), so the user
    can see exactly what changed — or that nothing did, and why (VAL-04)."""
    action, symbol, result = p.get("action"), p.get("symbol"), p.get("result")
    alert_ref = f"alert #{p['alert_id']}" if p.get("alert_id") is not None else "alert"
    condition = _condition_text(p)
    if result in _ALERT_DONE_VERB:
        detail = f": {condition}" if condition else ""
        recurring = " (recurring)" if p.get("recurring") and "(recurring)" not in detail else ""
        return f"{_ALERT_DONE_VERB[result]} {alert_ref} on {symbol}{detail}{recurring}"
    if result == "blocked":
        return f"Did NOT {action} an alert on {symbol} — {p.get('reason')}"
    if result == "invalid_condition":
        return f"Did NOT create an alert on {symbol} — {p.get('reason')}"
    if result == "needs_confirmation":
        price = p.get("current_price")
        now = f" (it's at ₹{price:,.2f} now)" if isinstance(price, (int, float)) else ""
        if p.get("reason") == "far_from_price":
            return (
                f"Did NOT create the alert on {symbol} yet — the level is far from today's price{now}, "
                f"so it would almost never fire. If that is what you want, ask again and add 'anyway' "
                f"(alert: {condition})"
            )
        return (
            f"Did NOT create the alert on {symbol} yet — the condition is already true{now}, so it would "
            f"fire on the next check. To create it anyway, ask again and add 'anyway' "
            f"(alert: {condition})"
        )
    if result == "duplicate":
        return f"Nothing new created — {alert_ref} on {symbol} already watches for {condition or 'that'}"
    if result == "limit_reached":
        return f"Did NOT create an alert on {symbol} — you already have {p.get('limit')} active alerts; cancel some first"
    if result == "ambiguous":
        listed = "; ".join(
            f"#{m['id']} {_condition_text({**m, 'symbol': m.get('symbol')}) or m.get('symbol')}"
            for m in p.get("matches") or []
        )
        return f"Could not {action} an alert on {symbol}: several match — {listed}. Say which, e.g. '{action} alert #{(p.get('matches') or [{}])[0].get('id', 'N')}'"
    return f"Could not {action} an alert on {symbol}: {_ALERT_FAIL_TEXT.get(result, result)}"


def conclusion_builder(state: GraphState) -> dict:
    # Evidence.payload is kept in checkpoint-safe form in GraphState (see
    # graph/nodes/execute_task.py's module docstring) — this node's own
    # isinstance(payload, pd.DataFrame) checks below need the real object back.
    evidence = [{**ev, "payload": from_cacheable(ev["payload"])} for ev in state.get("evidence", [])]
    flagged = _flagged_evidence_ids(state.get("verification_issues", []))

    conclusions: list[Conclusion] = []
    skipped = 0
    twins = _quote_twins(evidence)
    for ev in evidence:
        if ev["id"] in flagged:
            skipped += 1
            continue
        conclusions.extend(_conclusions_from_evidence(ev, twins.get(ev["entity"])))

    return {
        "conclusions": conclusions,
        **log_decision(
            "ConclusionBuilder",
            f"built {len(conclusions)} conclusion(s) from {len(evidence) - skipped} evidence item(s)",
            f"{skipped} evidence item(s) excluded (flagged by Verification)"
            if skipped
            else "no flagged evidence to exclude",
        ),
    }
