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

from graph.journal import log_decision
from graph.state import Conclusion, Evidence, GraphState

# Matches tools/analysis/fundamental.py's _CITATION_FIELDS keys exactly.
_CANONICAL_RATIO_KEYS = {"pe", "pb", "roce", "de", "cr"}

# Evidence types whose numeric payload fields are worth a Conclusion each.
_NUMERIC_EVIDENCE_TYPES = {"fundamental_ratio", "quote_info", "technical_indicator"}


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

    return conclusions


def conclusion_builder(state: GraphState) -> dict:
    evidence = state.get("evidence", [])
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
