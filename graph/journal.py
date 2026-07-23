"""
research_journal helper (RFC_AGENTIC_RESEARCH_GRAPH.md Sec7): every node
appends one JournalEntry on exit via log_decision(). Write-only from the
graph's perspective — no node reads it back; it exists for debugging and
replaying why a past run behaved a certain way given a thread_id.
"""
from __future__ import annotations

from datetime import datetime, timezone

from graph.state import JournalEntry


def log_decision(node: str, decision: str, reason: str) -> dict:
    """
    Returns a partial state update — {"research_journal": [entry]} — meant to
    be merged into a node's return value alongside its other state updates.
    The research_journal field's reducer (operator.add) appends it.
    """
    entry: JournalEntry = {
        "node": node,
        "decision": decision,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc),
    }
    return {"research_journal": [entry]}
