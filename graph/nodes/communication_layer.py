"""
CommunicationLayer node (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6). The only node
allowed to call the primary model — takes `conclusions` only, never raw
evidence or tool output, so it has no path to introduce a number that wasn't
already decided by ConclusionBuilder. Attaches confidence_summary (computed
deterministically from each Conclusion's own confidence, not asked of the
LLM) and renders the price chart via tools/charts/builder.py (reused as-is)
when OHLCV evidence for the primary entity is present.

`chart_html` holds the candlestick() figure as Plotly JSON: graph state is
checkpointed, and a live Figure object isn't serialisable (TEST-04).
GraphOrchestrator turns it back into a Figure for gr.Plot.

Rendering only (ARCH-05): this node reads and writes nothing. The three jobs it
used to do besides rendering now have their own nodes, wired in graph/graph.py:

- planning intake and strategy         -> graph/nodes/planning_node.py
- "show my reports" / "download #N"    -> graph/nodes/report_management.py
- saving a report, storing a CSV       -> graph/nodes/output_files.py (after this node)

`_is_streamable` equity/chat turns defer their LLM call to GraphOrchestrator,
which streams it; every other domain gets its finished text from here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import pandas as pd
from cachetools import TTLCache

import prompts
from agents.llm_client import LLMClient
from config import SYNTHESIS_EVIDENCE_CHAR_BUDGET, TOKENS_SYNTHESIS
from graph.advice_policy import REFRAME_INSTRUCTION
from graph.data_questions import NO_LIVE_DATA_INSTRUCTION
from graph.entity_resolution import blocking_resolutions, clarification_text
from graph.evidence_render import render_evidence_block
from graph.grounding import REGENERATE_INSTRUCTION, unverified_note
from graph.grounding import check as grounding_check
from graph.grounding import record as record_grounding
from graph.journal import log_decision
from graph.nodes.conclusion_builder import CANONICAL_RATIO_KEYS
from graph.nodes.execute_task import from_cacheable
from graph.prompt_safety import wrap_untrusted
from graph.state import Conclusion, GraphState
from tools.analysis.fundamental import cited_footnotes
from tools.charts.builder import candlestick

log = logging.getLogger(__name__)

_SYSTEM = prompts.get("communication.system") + prompts.get("safety.untrusted_notice")


_HISTORY_TURNS_IN_PROMPT = 6  # last N history messages included verbatim, same budget as query_analyzer.py


def _render_history(history: list[dict]) -> str:
    """Renders recent turns for inclusion in a synthesis prompt — this is
    what makes follow-ups ("what about its RSI") read as a continued
    conversation instead of a fresh, context-free question, matching how the
    deleted per-domain agents' own ConversationMemory-backed .chat() calls
    used to behave."""
    if not history:
        return ""
    lines = []
    for msg in history[-_HISTORY_TURNS_IN_PROMPT:]:
        lines.append(f"{msg.get('role', 'user')}: {msg.get('content', '')[:500]}")
    return "Recent conversation (oldest first):\n" + wrap_untrusted("history", "\n".join(lines)) + "\n"


def _build_prompt(
    objective: str, conclusions: list[Conclusion], history: list[dict], evidence_block: str = ""
) -> str:
    lines = [_render_history(history), f"Objective: {objective}", "", "Verified conclusions:"]
    for c in conclusions:
        marker = f" [^{c['citation_key']}]" if c["citation_key"] in CANONICAL_RATIO_KEYS else ""
        lines.append(f"- {c['claim']}{marker} (confidence: {c['confidence']:.2f})")
    if evidence_block:
        # LLM-02: the actual rows, headlines and passages behind the conclusions.
        lines += ["", "Evidence data:", evidence_block]
    return "\n".join(lines)


def _grounded_ask(client: LLMClient, prompt: str) -> str:
    """LLM-03, sync path: every figure in the answer must be traceable to the
    prompt it was written from. One regeneration with the unsupported list;
    whatever is still unsupported gets a visible note."""
    answer = client.ask(prompt, max_tokens=TOKENS_SYNTHESIS)
    result = grounding_check(answer, prompt)
    if not result.ok:
        retry = client.ask(
            prompt + REGENERATE_INSTRUCTION.format(figures=", ".join(result.unsupported)),
            max_tokens=TOKENS_SYNTHESIS,
        )
        result = grounding_check(retry, prompt)
        result.regenerated = True
        answer = retry
        if not result.ok:
            answer += unverified_note(result.unsupported)
    record_grounding(result)
    return answer


# TEST-06: building the candlestick figure (plotly validates every property) was about
# two thirds of the CPU a whole equity turn used, so a load run saturated one core at
# ~1.5 turns/s. The same price history gives the same chart, so the JSON is reused for a
# few minutes; the key is the series itself (symbol, length, first and last bar).
_CHART_TTL_SECONDS = 600
_chart_cache: TTLCache = TTLCache(maxsize=256, ttl=_CHART_TTL_SECONDS)
_chart_lock = threading.Lock()


def clear_chart_cache() -> None:
    with _chart_lock:
        _chart_cache.clear()


def _chart_json(ohlcv: pd.DataFrame, entity: str) -> str | None:
    """The price chart as Plotly JSON for this symbol's history, or None if there is too little."""
    key = (
        entity,
        len(ohlcv),
        str(ohlcv.index[0]),
        str(ohlcv.index[-1]),
        float(ohlcv["Close"].iloc[0]),
        float(ohlcv["Close"].iloc[-1]),
    )
    with _chart_lock:
        hit = _chart_cache.get(key)
    if hit is not None:
        return hit
    fig = candlestick(ohlcv, entity)
    chart = fig.to_json() if fig is not None else None
    if chart is not None:
        with _chart_lock:
            _chart_cache[key] = chart
    return chart


def _confidence_summary(conclusions: list[Conclusion]) -> dict:
    return {c["citation_key"]: c["confidence"] for c in conclusions}


def _find_ohlcv_evidence(evidence: list[dict], entity: str):
    for ev in evidence:
        if ev["tool"] == "fetch_ohlcv" and ev["entity"] == entity:
            # Evidence.payload is kept in checkpoint-safe form in GraphState
            # (see graph/nodes/execute_task.py's module docstring) — candlestick()
            # needs the real DataFrame back.
            return from_cacheable(ev["payload"])
    return None


def _find_fundamental_payload(evidence: list[dict], entity: str) -> dict:
    for ev in evidence:
        if ev["tool"] == "compute_fundamentals" and ev["entity"] == entity:
            return ev["payload"]
    return {}


def _intents(state: GraphState) -> set[str]:
    """All domains this turn named (Phase 7.4 multi-intent support) — the
    primary `intent` plus any `secondary_intents` QueryAnalyzer detected.
    Report/screener's file-output branches check membership in this set
    rather than equality against `intent` alone, so e.g. "generate a report
    on TCS and alert me if it drops 5%" (intent="report",
    secondary_intents=["alert"]) still writes the report file. Planning is
    intentionally excluded from this: it's checked separately, first, and
    unconditionally short-circuits (see _handle_planning) — combining
    planning with another domain in one turn is not supported, since
    planning's whole flow bypasses Planner/evidence/conclusions entirely."""
    return {state.get("intent")} | set(state.get("secondary_intents") or [])


def _is_streamable(intent: str, intents: set[str]) -> bool:
    """Equity/general-chat turns with no synchronous file output to write
    stream token-by-token (GraphOrchestrator does the actual LLM call, see
    its module docstring) — matches the deleted agents/equity_agent.py's
    "only EquityAgent streams" behavior. Report/screener intents keep the
    synchronous .ask() call here since they need the finished text
    immediately (file write, DB save, footnote-appending) before this node
    returns."""
    return intent in ("equity", "chat") and "report" not in intents and "screener" not in intents


def build_communication_layer_node(
    client: LLMClient | None = None,
    provider_config: dict | None = None,
) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM, provider_config, node="communication")

    def communication_layer(state: GraphState) -> dict:
        blocking = blocking_resolutions(state)
        if blocking:
            return {
                "final_report": clarification_text(blocking),
                "pending_synthesis_prompt": None,
                "pending_fundamental_payload": None,
                "chart_html": None,
                "confidence_summary": {},
                **log_decision(
                    "CommunicationLayer",
                    "asked which company",
                    f"unresolved: {[u['query'] for u in blocking]}",
                ),
            }

        objective = state.get("objective", "")
        conclusions = state.get("conclusions", [])
        entities = state.get("entities", [])
        evidence = state.get("evidence", [])
        query = state.get("query", "")
        history = state.get("history", [])

        intent = state.get("intent") or "equity"
        intents = _intents(state)
        # LLM-09: a request for a personal call is answered as analysis.
        advice = REFRAME_INSTRUCTION if state.get("advice_mode") == "reframe" else ""
        evidence_block = render_evidence_block(evidence, SYNTHESIS_EVIDENCE_CHAR_BUDGET, intents, query)

        if not conclusions and state.get("complexity") == "simple":
            # Trivial path: Planner deliberately generated no tasks (RFC's
            # "avoid unnecessary tool execution"), so there was never any
            # evidence to build conclusions from — this isn't a research
            # failure, just a question that doesn't need research. Answer
            # directly instead of reporting a gap that was never a gap.
            # LLM-06: nothing was fetched, so the model must not state figures.
            prompt = NO_LIVE_DATA_INSTRUCTION + _render_history(history) + (query or objective) + advice
            if _is_streamable(intent, intents):
                return {
                    "final_report": "",
                    "pending_synthesis_prompt": prompt,
                    "pending_fundamental_payload": None,
                    "chart_html": None,
                    "confidence_summary": {},
                    **log_decision(
                        "CommunicationLayer", "deferred to streaming, no research needed", "complexity=simple"
                    ),
                }
            report = _grounded_ask(client, prompt)
            return {
                "final_report": report,
                "pending_synthesis_prompt": None,
                "pending_fundamental_payload": None,
                "chart_html": None,
                "confidence_summary": {},
                **log_decision(
                    "CommunicationLayer", "answered directly, no research needed", "complexity=simple"
                ),
            }

        if not conclusions:
            report = (
                f"I couldn't find enough data to answer: {objective or query}\n\n"
                "Try naming the company or fund exactly (for example its NSE symbol), or ask again in a moment."
            )
            return {
                "final_report": report,
                "pending_synthesis_prompt": None,
                "pending_fundamental_payload": None,
                "chart_html": None,
                "confidence_summary": {},
                **log_decision(
                    "CommunicationLayer", "no conclusions to report", "ConclusionBuilder produced nothing"
                ),
            }

        primary_entity = entities[0] if entities else None
        fundamental_payload = _find_fundamental_payload(evidence, primary_entity) if primary_entity else {}

        chart = None
        if primary_entity:
            ohlcv = _find_ohlcv_evidence(evidence, primary_entity)
            if isinstance(ohlcv, pd.DataFrame) and not ohlcv.empty:
                chart = _chart_json(ohlcv, primary_entity)

        if _is_streamable(intent, intents):
            # Chart doesn't depend on the LLM call, so it's still available
            # immediately alongside the streamed text — GraphOrchestrator
            # yields it on the first chunk, matching the deleted
            # agents/equity_agent.py's streaming behavior.
            return {
                "final_report": "",
                "pending_synthesis_prompt": _build_prompt(objective, conclusions, history, evidence_block)
                + advice,
                "pending_fundamental_payload": fundamental_payload or None,
                "chart_html": chart,
                "pdf_url": None,
                "md_url": None,
                "csv_url": None,
                "confidence_summary": _confidence_summary(conclusions),
                **log_decision(
                    "CommunicationLayer", "deferred to streaming", f"{len(conclusions)} conclusion(s)"
                ),
            }

        report = _grounded_ask(
            client, _build_prompt(objective, conclusions, history, evidence_block) + advice
        )
        if fundamental_payload:
            report = report + cited_footnotes(report, fundamental_payload)

        return {
            "final_report": report,
            "pending_synthesis_prompt": None,
            "pending_fundamental_payload": None,
            "chart_html": chart,
            "confidence_summary": _confidence_summary(conclusions),
            **log_decision(
                "CommunicationLayer",
                f"synthesized report from {len(conclusions)} conclusion(s)",
                f"chart={'rendered' if chart is not None else 'none'}",
            ),
        }

    return communication_layer
