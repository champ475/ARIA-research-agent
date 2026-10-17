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

import json

import pandas as pd

from graph.journal import log_decision
from graph.nodes.execute_task import from_cacheable
from graph.state import Evidence, GraphState

_MAX_KEYS_PER_ENTRY = 40  # bounds one entry's own summary dict (e.g. get_info()'s 100+ fields)


def _frame_shape(df: pd.DataFrame) -> dict:
    return {"rows": len(df), "columns": [str(c) for c in df.columns]}


def _summarize_payload(payload) -> dict:
    # Evidence.payload is kept in checkpoint-safe form in GraphState (see
    # graph/nodes/execute_task.py's module docstring) — reconstruct here so
    # a DataFrame still gets shape-summarized instead of leaking its full
    # {"__dataframe__": True, "records": [...]} form into the summary
    # (exactly the payload bloat this module exists to prevent).
    payload = from_cacheable(payload)
    if isinstance(payload, pd.DataFrame):
        return _frame_shape(payload)
    if isinstance(payload, dict):
        # Nested frames (fetch_financials' statements) are summarised too:
        # the summary lives in checkpointed state (TEST-04).
        return {k: _frame_shape(v) if isinstance(v, pd.DataFrame) else v for k, v in payload.items()}
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


def _cap_entry_payload(summary_payload):
    """Bounds a single evidence entry's own summary dict so one outsized
    payload (e.g. get_info()'s 100+ yfinance fields) can't dominate the
    prompt budget alone, regardless of how many entries there are."""
    if isinstance(summary_payload, dict) and len(summary_payload) > _MAX_KEYS_PER_ENTRY:
        capped = dict(list(summary_payload.items())[:_MAX_KEYS_PER_ENTRY])
        capped["_truncated_keys"] = len(summary_payload) - _MAX_KEYS_PER_ENTRY
        return capped
    return summary_payload


def cap_evidence_for_prompt(evidence_summary: dict, char_budget: int) -> dict:
    """
    Bounds the serialized size of evidence_summary before Reflection/
    Verification embed it in an LLM prompt via json.dumps. Confirmed in
    production: a single evidence_summary reached 10,000-11,000 tokens
    against Groq free tier's 6,000 TPM cap in ONE call — no cap existed on
    either a single entry's own payload size or on how many (entity,
    evidence_type) entries accumulate across research loop iterations.

    Two passes:
    1. Per-entry cap (_cap_entry_payload) — bounds each entry's own
       "summary" payload first, so one outsized entry can't dominate the
       budget alone.
    2. Budget pass — ranks entries by confidence descending, greedily
       keeps whole entries (by their full serialized size) while they fit
       under char_budget, skipping (not truncating) any that don't —
       lower-confidence entries after a skip are still tried, so a
       small low-confidence entry can fill space a large one couldn't.
       An entry is never partially included; this never emits a
       truncated/invalid JSON fragment.
    """
    flat: list[tuple[str, str, dict]] = []
    for entity, by_type in evidence_summary.items():
        for evidence_type, entry in by_type.items():
            capped_entry = dict(entry)
            capped_entry["summary"] = _cap_entry_payload(entry.get("summary"))
            flat.append((entity, evidence_type, capped_entry))

    flat.sort(key=lambda item: item[2].get("confidence", 0), reverse=True)

    result: dict[str, dict] = {}
    used = 0
    for entity, evidence_type, entry in flat:
        entry_size = len(json.dumps({evidence_type: entry}, default=str))
        if used + entry_size > char_budget:
            continue
        result.setdefault(entity, {})[evidence_type] = entry
        used += entry_size
    return result


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
