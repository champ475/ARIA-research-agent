"""
graph/progress.py — step-level progress for a running turn (UX-02).

A research turn takes 20–60 s; behind three bouncing dots it looked frozen.
`run_with_progress` runs the compiled graph with LangGraph's
stream_mode=["updates", "values"]: each finished node (and each research
task) becomes a short status line — "Understanding the question →
Fetching financial statements → Computing fundamental ratios → Verifying
the numbers → Writing the answer" — and the last full state is returned,
with any human-in-the-loop interrupt attached exactly as `invoke` would.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from graph.deadline import TOOL_LABELS

NODE_LABELS = {
    "QueryAnalyzer": "Understanding the question",
    "Planner": "Planning the research",
    "EvidenceSummarizer": "Reviewing the evidence",
    "Reflection": "Checking for gaps",
    "Verification": "Verifying the numbers",
    "ConclusionBuilder": "Drawing conclusions",
    "CommunicationLayer": "Writing the answer",
}


@dataclass(frozen=True)
class ProgressUpdate:
    """One status line; GraphOrchestrator.process_stream(progress=True)
    yields these before the answer."""

    label: str


def task_label(tool: str) -> str:
    what = TOOL_LABELS.get(tool, tool.replace("_", " "))
    if tool.startswith("compute_") or tool in (
        "risk_assessment",
        "news_sentiment",
        "peer_comparison",
        "backtest",
    ):
        return f"Computing {what}" if tool.startswith("compute_") else f"Running {what}"
    return f"Fetching {what}"


def labels_for(update: dict[str, Any]) -> list[str]:
    """Status lines for one `updates` chunk ({node: partial_state})."""
    out: list[str] = []
    for node, partial in update.items():
        if node == "execute_task_node":
            for task in (partial or {}).get("tasks") or []:
                if task.get("status") == "done":
                    out.append(task_label(task["tool"]))
        elif node in NODE_LABELS:
            out.append(NODE_LABELS[node])
    return out


def run_with_progress(graph: Any, payload: Any, config: dict) -> Generator[ProgressUpdate, None, dict]:
    """Yields ProgressUpdate per new step; returns the final state."""
    final: dict = {}
    interrupt = None
    last = None
    for mode, chunk in graph.stream(payload, config=config, stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
            continue
        if "__interrupt__" in chunk:
            interrupt = chunk["__interrupt__"]
            continue
        for label in labels_for(chunk):
            if label != last:
                last = label
                yield ProgressUpdate(label)
    if interrupt is not None:
        final = {**final, "__interrupt__": interrupt}
    return final
