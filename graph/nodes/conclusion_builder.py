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
from graph.state import Conclusion, Evidence, GraphState
from utils.helpers import trim_text

# Matches tools/analysis/fundamental.py's _CITATION_FIELDS keys exactly.
_CANONICAL_RATIO_KEYS = {"pe", "pb", "roce", "de", "cr"}

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


def _numeric_fields(payload) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    return {
        k: v for k, v in payload.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v  # excludes NaN
    }


def _flagged_evidence_ids(verification_issues: list[dict]) -> set[str]:
    flagged: set[str] = set()
    for issue in verification_issues or []:
        flagged.update(issue.get("evidence_ids", []))
    return flagged


def _conclusions_from_evidence(ev: Evidence) -> list[Conclusion]:
    conclusions: list[Conclusion] = []

    if ev["evidence_type"] in _NUMERIC_EVIDENCE_TYPES:
        for field, value in _numeric_fields(ev["payload"]).items():
            citation_key = field if field in _CANONICAL_RATIO_KEYS else f"{ev['entity']}.{field}"
            conclusions.append({
                "claim": f"{ev['entity']} {field} = {value}",
                "value": value,
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": citation_key,
            })

    elif ev["evidence_type"] == "news_headline" and isinstance(ev["payload"], list):
        conclusions.append({
            "claim": f"{ev['entity']} has {len(ev['payload'])} recent news headline(s) on record",
            "value": len(ev["payload"]),
            "confidence": ev["confidence"],
            "evidence_ids": [ev["id"]],
            "citation_key": f"{ev['entity']}.news_count",
        })

    elif ev["evidence_type"] in _TEXT_EVIDENCE_TYPES and isinstance(ev["payload"], str):
        text = ev["payload"]
        if text and not text.lower().startswith(_NOT_AVAILABLE_MARKERS):
            conclusions.append({
                "claim": f"{ev['entity']} {ev['tool']}: {trim_text(text, 150)}",
                "value": text,
                "confidence": ev["confidence"],
                "evidence_ids": [ev["id"]],
                "citation_key": f"{ev['entity']}.{ev['tool']}",
            })

    elif ev["evidence_type"] in _DATAFRAME_EVIDENCE_TYPES and isinstance(ev["payload"], pd.DataFrame):
        n = len(ev["payload"])
        label = _DATAFRAME_EVIDENCE_TYPES[ev["evidence_type"]]
        conclusions.append({
            "claim": f"{ev['entity']}: {n} {label}",
            "value": n,
            "confidence": ev["confidence"],
            "evidence_ids": [ev["id"]],
            "citation_key": f"{ev['entity']}.{ev['evidence_type']}",
        })

    elif ev["evidence_type"] == "screener_saved" and isinstance(ev["payload"], dict):
        p = ev["payload"]
        conclusions.append({
            "claim": f"Screener saved as '{p.get('name')}' with filters: {p.get('filters')}",
            "value": p,
            "confidence": ev["confidence"],
            "evidence_ids": [ev["id"]],
            "citation_key": f"{ev['entity']}.screener_saved",
        })

    elif ev["evidence_type"] == "alert_list" and isinstance(ev["payload"], list):
        conclusions.append({
            "claim": f"You have {len(ev['payload'])} alert(s) on record",
            "value": len(ev["payload"]),
            "confidence": ev["confidence"],
            "evidence_ids": [ev["id"]],
            "citation_key": "alerts.count",
        })

    elif ev["evidence_type"] == "alert_action" and isinstance(ev["payload"], dict):
        p = ev["payload"]
        conclusions.append({
            "claim": f"Alert {p.get('action')} on {p.get('symbol')}: {p.get('result')}",
            "value": p,
            "confidence": ev["confidence"],
            "evidence_ids": [ev["id"]],
            "citation_key": f"{ev['entity']}.alert_{p.get('action')}",
        })

    return conclusions


def conclusion_builder(state: GraphState) -> dict:
    # Evidence.payload is kept in checkpoint-safe form in GraphState (see
    # graph/nodes/execute_task.py's module docstring) — this node's own
    # isinstance(payload, pd.DataFrame) checks below need the real object back.
    evidence = [
        {**ev, "payload": from_cacheable(ev["payload"])} for ev in state.get("evidence", [])
    ]
    flagged = _flagged_evidence_ids(state.get("verification_issues", []))

    conclusions: list[Conclusion] = []
    skipped = 0
    for ev in evidence:
        if ev["id"] in flagged:
            skipped += 1
            continue
        conclusions.extend(_conclusions_from_evidence(ev))

    return {
        "conclusions": conclusions,
        **log_decision(
            "ConclusionBuilder",
            f"built {len(conclusions)} conclusion(s) from {len(evidence) - skipped} evidence item(s)",
            f"{skipped} evidence item(s) excluded (flagged by Verification)" if skipped else "no flagged evidence to exclude",
        ),
    }
