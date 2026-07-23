"""
CommunicationLayer node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). The only node
allowed to call the primary model — takes `conclusions` only, never raw
evidence or tool output, so it has no path to introduce a number that wasn't
already decided by ConclusionBuilder. Attaches confidence_summary (computed
deterministically from each Conclusion's own confidence, not asked of the
LLM) and renders the price chart via tools/charts/builder.py (reused as-is)
when OHLCV evidence for the primary entity is present.

`chart_html` currently holds the raw Plotly Figure object returned by
candlestick() — gr.Plot (app.py) consumes Figure objects directly, not an
HTML string. The field name follows the RFC's schema (Sec5); the actual
UI-facing serialization is a Phase 6 integration decision, not this node's.
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from agents.llm_client import LLMClient
from config import TOKENS_SYNTHESIS
from graph.journal import log_decision
from graph.state import Conclusion, GraphState
from tools.analysis.fundamental import cited_footnotes
from tools.charts.builder import candlestick

_CANONICAL_RATIO_KEYS = {"pe", "pb", "roce", "de", "cr"}

_SYSTEM = (
    "You are ARIA, an autonomous financial research engine for Indian equity "
    "markets (NSE/BSE). You are given a list of pre-verified conclusions — "
    "facts already decided and checked, not raw data. Write a clear, direct "
    "answer to the user's objective using ONLY these conclusions. State each "
    'ratio using its exact [^key] marker where one is given, right after the '
    'number (e.g. "P/E of 24.3 [^pe]") — never invent a marker, never attach '
    "one to a value that has none. Do not introduce any number that isn't in "
    "the conclusions list. If the conclusions don't fully cover the "
    "objective, say so plainly rather than filling the gap."
)


def _build_prompt(objective: str, conclusions: list[Conclusion]) -> str:
    lines = [f"Objective: {objective}", "", "Verified conclusions:"]
    for c in conclusions:
        marker = f" [^{c['citation_key']}]" if c["citation_key"] in _CANONICAL_RATIO_KEYS else ""
        lines.append(f"- {c['claim']}{marker} (confidence: {c['confidence']:.2f})")
    return "\n".join(lines)


def _confidence_summary(conclusions: list[Conclusion]) -> dict:
    return {c["citation_key"]: c["confidence"] for c in conclusions}


def _find_ohlcv_evidence(evidence: list[dict], entity: str):
    for ev in evidence:
        if ev["tool"] == "fetch_ohlcv" and ev["entity"] == entity:
            return ev["payload"]
    return None


def _find_fundamental_payload(evidence: list[dict], entity: str) -> dict:
    for ev in evidence:
        if ev["tool"] == "compute_fundamentals" and ev["entity"] == entity:
            return ev["payload"]
    return {}


def build_communication_layer_node(client: LLMClient | None = None) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM)

    def communication_layer(state: GraphState) -> dict:
        objective = state.get("objective", "")
        conclusions = state.get("conclusions", [])
        entities = state.get("entities", [])
        evidence = state.get("evidence", [])

        if not conclusions:
            report = f"I wasn't able to verify enough evidence to answer: {objective or state.get('query', '')}"
            return {
                "final_report": report,
                "chart_html": None,
                "confidence_summary": {},
                **log_decision(
                    "CommunicationLayer", "no conclusions to report", "ConclusionBuilder produced nothing"
                ),
            }

        report = client.ask(_build_prompt(objective, conclusions), max_tokens=TOKENS_SYNTHESIS)

        primary_entity = entities[0] if entities else None
        fundamental_payload = _find_fundamental_payload(evidence, primary_entity) if primary_entity else {}
        if fundamental_payload:
            report = report + cited_footnotes(report, fundamental_payload)

        chart = None
        if primary_entity:
            ohlcv = _find_ohlcv_evidence(evidence, primary_entity)
            if isinstance(ohlcv, pd.DataFrame) and not ohlcv.empty:
                chart = candlestick(ohlcv, primary_entity)

        return {
            "final_report": report,
            "chart_html": chart,
            "confidence_summary": _confidence_summary(conclusions),
            **log_decision(
                "CommunicationLayer",
                f"synthesized report from {len(conclusions)} conclusion(s)",
                f"chart={'rendered' if chart is not None else 'none'}",
            ),
        }

    return communication_layer
