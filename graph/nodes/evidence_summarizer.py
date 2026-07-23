"""
EvidenceSummarizer node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). Deterministic
(no LLM call): groups `evidence` by (entity, evidence_type), keeps the latest
entry's value + confidence per group, and drops full raw payloads down to
what Reflection actually needs. Exists because Reflection reading the raw
`evidence` list directly (potentially hundreds of entries after a few loop
iterations) makes the Reflection prompt balloon and degrades recall.

DataFrame payloads (e.g. fetch_ohlcv's raw OHLCV history) are summarized to
their shape rather than included in full — nothing downstream of Reflection
needs the full time series, just "there is price history and how much".
"""
from __future__ import annotations

import pandas as pd

from graph.journal import log_decision
from graph.state import Evidence, GraphState


def _summarize_payload(payload) -> dict:
    if isinstance(payload, pd.DataFrame):
        return {"rows": len(payload), "columns": list(payload.columns)}
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"count": len(payload)}
    return {"value": payload}


def _summarize(evidence: list[Evidence]) -> dict:
    groups: dict[tuple[str, str], Evidence] = {}
    for ev in evidence:
        key = (ev["entity"], ev["evidence_type"])
        existing = groups.get(key)
        if existing is None or ev["timestamp"] > existing["timestamp"]:
            groups[key] = ev

    summary: dict[str, dict] = {}
    for (entity, evidence_type), ev in groups.items():
        summary.setdefault(entity, {})[evidence_type] = {
            "summary": _summarize_payload(ev["payload"]),
            "confidence": ev["confidence"],
            "source": ev["source"],
            "evidence_id": ev["id"],
        }
    return summary


def evidence_summarizer(state: GraphState) -> dict:
    evidence = state.get("evidence", [])
    summary = _summarize(evidence)
    entities_covered = list(summary.keys())
    return {
        "evidence_summary": summary,
        **log_decision(
            "EvidenceSummarizer",
            f"summarized {len(evidence)} evidence item(s) across {len(entities_covered)} entity(ies)",
            "deterministic groupby (entity, evidence_type), latest value kept",
        ),
    }
