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

Report-domain file output (Phase 7.1): PDF/MD generation is deliberately done
HERE, not in ConclusionBuilder, even though the graph-expansion plan's first
draft assumed ConclusionBuilder — `final_report` (the actual markdown text)
doesn't exist until this node's LLM call completes, so ConclusionBuilder
never has a report body to save. Rendering immediately after the LLM call
keeps the "only CommunicationLayer calls the model" contract intact —
render_report() itself is a pure, deterministic I/O side effect, not a
second model call. The rendered files are TEMPORARY (utils/report_files.py)
— durable storage is `reports.content` in Postgres (report_save() below);
re-downloading an older report re-renders from that stored text via the
same render_report() call (see _handle_report_management below), not from
a file that might not exist anymore.

Report list/re-download (Phase 3 of the open-source restructure):
`_handle_report_management` bypasses the Evidence/Conclusion pipeline
entirely for "show my reports" / "download report #N" style queries — same
reasoning as the planning bypass below, there's no tool-selection/evidence-
gathering step useful here, just a DB read and (for a download) a render.

Screener-domain CSV export (Phase 7.3) follows the same pattern: `csv_path`
is populated from the raw `screener_run` evidence (not a Conclusion — the
full row-level CSV is genuinely raw tool output, not a decided fact), which
is fine because it's a side-effect file write, not something CommunicationLayer
reasons about or cites in the LLM prompt.

Planning domain (Phase 7.3) bypasses the Evidence/Conclusion pipeline
entirely — it's handled here, at the top of communication_layer, before the
"simple"/"no conclusions" branches even run. This isn't a workaround; it's
the same reasoning as the report/screener branches taken one step further:
planning_agent's intake+strategy flow (utils/planning_logic.py) is two
sequential LLM calls with Python-only branching in between (is the minimum
profile complete? did the user ask to build?), reading/writing
`financial_profiles` directly — there's no tool-selection/evidence-gathering
step for Planner or ResearchExecutor to usefully do here, since nothing in
graph/tool_catalogue.py models "extract structured profile fields from free
text." See ARCHITECTURE.md's 2026-07-24 "Planning domain on the graph" entry
for why the 3rd HITL gate (capital-allocation confirmation) is deliberately
NOT implemented yet — it needs more than this node can safely provide.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pandas as pd

from agents.llm_client import LLMClient
from config import TOKENS_SYNTHESIS
from graph.journal import log_decision
from graph.nodes.execute_task import from_cacheable
from graph.prompt_safety import UNTRUSTED_NOTICE, wrap_untrusted
from graph.state import Conclusion, GraphState
from tools.analysis.fundamental import cited_footnotes
from tools.charts.builder import candlestick
from utils.db import profile_clear, profile_get, profile_upsert, report_get, report_list, report_save
from utils.helpers import detect_report_type
from utils.planning_logic import (
    BUILD_TRIGGER_KW as _PLANNING_BUILD_TRIGGER_KW,
)
from utils.planning_logic import (
    REQUIRED_FIELDS as _PLANNING_REQUIRED,
)
from utils.planning_logic import (
    build_strategy as _planning_build_strategy,
)
from utils.planning_logic import (
    intake_turn as _planning_intake_turn,
)
from utils.report_files import render_report
from utils.screener_files import save_screener_csv

_PLANNING_SHOW_KW = ["show my profile", "my financial profile", "what do you know about me"]
_PLANNING_RESET_KW = [
    "start over",
    "reset my plan",
    "reset my profile",
    "forget my profile",
    "clear my profile",
]

_CANONICAL_RATIO_KEYS = {"pe", "pb", "roce", "de", "cr"}

_SYSTEM = (
    "You are ARIA, an autonomous financial research engine for Indian equity "
    "markets (NSE/BSE). You are given a list of pre-verified conclusions — "
    "facts already decided and checked, not raw data. Write a clear, direct "
    "answer to the user's objective using ONLY these conclusions. State each "
    "ratio using its exact [^key] marker where one is given, right after the "
    'number (e.g. "P/E of 24.3 [^pe]") — never invent a marker, never attach '
    "one to a value that has none. Do not introduce any number that isn't in "
    "the conclusions list. If the conclusions don't fully cover the "
    "objective, say so plainly rather than filling the gap." + UNTRUSTED_NOTICE
)


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


def _build_prompt(objective: str, conclusions: list[Conclusion], history: list[dict]) -> str:
    lines = [_render_history(history), f"Objective: {objective}", "", "Verified conclusions:"]
    for c in conclusions:
        marker = f" [^{c['citation_key']}]" if c["citation_key"] in _CANONICAL_RATIO_KEYS else ""
        lines.append(f"- {c['claim']}{marker} (confidence: {c['confidence']:.2f})")
    return "\n".join(lines)


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


def _find_screener_results(evidence: list[dict]) -> list[dict] | None:
    for ev in evidence:
        if ev["tool"] == "screener_run":
            return ev["payload"]
    return None


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


def _empty_result(final_report: str, decision: str, reason: str) -> dict:
    return {
        "final_report": final_report,
        "pending_synthesis_prompt": None,
        "pending_fundamental_payload": None,
        "chart_html": None,
        "pdf_path": None,
        "md_path": None,
        "csv_path": None,
        "confidence_summary": {},
        **log_decision("CommunicationLayer", decision, reason),
    }


def _is_streamable(intent: str, intents: set[str]) -> bool:
    """Equity/general-chat turns with no synchronous file output to write
    stream token-by-token (GraphOrchestrator does the actual LLM call, see
    its module docstring) — matches the deleted agents/equity_agent.py's
    "only EquityAgent streams" behavior. Report/screener intents keep the
    synchronous .ask() call here since they need the finished text
    immediately (file write, DB save, footnote-appending) before this node
    returns."""
    return intent in ("equity", "chat") and "report" not in intents and "screener" not in intents


_REPORT_LIST_KW = [
    "list reports",
    "list my reports",
    "saved reports",
    "my reports",
    "show reports",
    "show my reports",
]
_REPORT_DOWNLOAD_KW = ["download", "regenerate", "resend", "re-download", "redownload"]
_REPORT_ID_RE = re.compile(r"#?(\d+)")


def _handle_report_management(state: GraphState) -> dict | None:
    """ "Show my reports" / "download report #N" bypass the Evidence/
    Conclusion pipeline entirely, same reasoning as _handle_planning below —
    there's no tool-selection/evidence-gathering step useful for a DB read
    and a re-render. Returns None (not handled) for anything else, so a
    plain "generate a report on TCS" falls through to the normal
    generation flow further down in communication_layer()."""
    user_id = state.get("user_id")
    query = (state.get("query") or "").strip()
    lower = query.lower()

    if any(k in lower for k in _REPORT_LIST_KW):
        if user_id is None:
            return _empty_result(
                "I need to know who you are to show your reports — please sign in again.",
                "report: missing user_id",
                "cannot read reports without a user_id",
            )
        reports = report_list(user_id)
        if not reports:
            text = 'You have no saved reports yet. Try "generate an IC report for TCS".'
        else:
            lines = ["## Your Reports\n", "| # | Type | Title | Date |", "|---|------|-------|------|"]
            for r in reports:
                lines.append(
                    f"| {r['id']} | {r['report_type']} | {r['title']} | {r['created_at'].strftime('%Y-%m-%d')} |"
                )
            lines.append('\nSay "download report #<id>" to get the PDF/Markdown again.')
            text = "\n".join(lines)
        return _empty_result(text, "report: listed saved reports", f"user_id={user_id}")

    if any(k in lower for k in _REPORT_DOWNLOAD_KW):
        id_match = _REPORT_ID_RE.search(query)
        if user_id is None or not id_match:
            return None  # can't tell which report — fall through to normal generation
        report_id = int(id_match.group(1))
        row = report_get(user_id, report_id)
        if row is None:
            return _empty_result(
                f'Couldn\'t find report #{report_id} — try "show my reports" to see the list.',
                "report: download target not found",
                f"user_id={user_id}, report_id={report_id}",
            )
        # Re-renders from the durably stored text (reports.content) — the
        # file from whenever this report was first generated is long gone
        # on an ephemeral host, but the text never depended on it surviving.
        md_path, pdf_path = render_report(row["symbol"] or "report", row["report_type"], row["content"])
        result = _empty_result(
            f"Here's report #{report_id} — **{row['title']}**.",
            "report: regenerated from history",
            f"user_id={user_id}, report_id={report_id}",
        )
        result["md_path"] = md_path
        result["pdf_path"] = pdf_path
        return result

    return None


def _handle_planning(client: LLMClient, state: GraphState) -> dict:
    user_id = state.get("user_id")
    query = (state.get("query") or "").strip()
    lower = query.lower()

    if user_id is None:
        return _empty_result(
            "I need to know who you are to track a financial profile — please sign in again.",
            "planning: missing user_id",
            "cannot read/write financial_profiles without a user_id",
        )

    if any(k in lower for k in _PLANNING_SHOW_KW):
        profile = profile_get(user_id)
        if not profile:
            text = "I don't have a profile for you yet. Tell me your age, investable capital, time horizon, and goal to get started."
        else:
            lines = ["## Your Financial Profile\n", "| Field | Value |", "|---|---|"]
            for k, v in profile.items():
                lines.append(f"| {k.replace('_', ' ').title()} | {v} |")
            text = "\n".join(lines)
        return _empty_result(text, "planning: showed profile", f"user_id={user_id}")

    if any(k in lower for k in _PLANNING_RESET_KW):
        profile_clear(user_id)
        return _empty_result(
            "🗑️ Profile cleared. Tell me about yourself again whenever you're ready — age, capital, time horizon, and goal are the essentials.",
            "planning: cleared profile",
            f"user_id={user_id}",
        )

    profile = profile_get(user_id)
    updates, minimum_complete, reply = _planning_intake_turn(client, query, profile)
    if updates:
        profile = profile_upsert(user_id, updates)

    was_already_complete = all(profile.get(f) not in (None, "") for f in _PLANNING_REQUIRED) and not updates
    wants_build = any(k in lower for k in _PLANNING_BUILD_TRIGGER_KW)

    if not minimum_complete:
        return _empty_result(
            reply, "planning: intake incomplete", f"user_id={user_id}, updates={list(updates)}"
        )

    if updates or wants_build or not was_already_complete:
        strategy = _planning_build_strategy(client, profile)
        return _empty_result(strategy, "planning: built strategy", f"user_id={user_id}")
    return _empty_result(reply, "planning: intake reply, no build triggered", f"user_id={user_id}")


def build_communication_layer_node(
    client: LLMClient | None = None,
    provider_config: dict | None = None,
) -> Callable[[GraphState], dict]:
    client = client or LLMClient(_SYSTEM, provider_config)

    def communication_layer(state: GraphState) -> dict:
        if state.get("intent") == "planning":
            return _handle_planning(client, state)

        if state.get("intent") == "report":
            report_mgmt_result = _handle_report_management(state)
            if report_mgmt_result is not None:
                return report_mgmt_result

        objective = state.get("objective", "")
        conclusions = state.get("conclusions", [])
        entities = state.get("entities", [])
        evidence = state.get("evidence", [])
        query = state.get("query", "")
        history = state.get("history", [])

        intent = state.get("intent") or "equity"
        intents = _intents(state)

        if not conclusions and state.get("complexity") == "simple":
            # Trivial path: Planner deliberately generated no tasks (RFC's
            # "avoid unnecessary tool execution"), so there was never any
            # evidence to build conclusions from — this isn't a research
            # failure, just a question that doesn't need research. Answer
            # directly instead of reporting a gap that was never a gap.
            prompt = _render_history(history) + (query or objective)
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
            report = client.ask(prompt, max_tokens=TOKENS_SYNTHESIS)
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
            report = f"I wasn't able to verify enough evidence to answer: {objective or query}"
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
                chart = candlestick(ohlcv, primary_entity)

        if _is_streamable(intent, intents):
            # Chart doesn't depend on the LLM call, so it's still available
            # immediately alongside the streamed text — GraphOrchestrator
            # yields it on the first chunk, matching the deleted
            # agents/equity_agent.py's streaming behavior.
            return {
                "final_report": "",
                "pending_synthesis_prompt": _build_prompt(objective, conclusions, history),
                "pending_fundamental_payload": fundamental_payload or None,
                "chart_html": chart,
                "pdf_path": None,
                "md_path": None,
                "csv_path": None,
                "confidence_summary": _confidence_summary(conclusions),
                **log_decision(
                    "CommunicationLayer", "deferred to streaming", f"{len(conclusions)} conclusion(s)"
                ),
            }

        report = client.ask(_build_prompt(objective, conclusions, history), max_tokens=TOKENS_SYNTHESIS)
        if fundamental_payload:
            report = report + cited_footnotes(report, fundamental_payload)

        pdf_path = md_path = None
        if "report" in intents and primary_entity:
            rtype = detect_report_type((query or objective).lower())
            md_path, pdf_path = render_report(primary_entity, rtype, report)
            user_id = state.get("user_id")
            if user_id is not None:
                try:
                    report_save(user_id, primary_entity, rtype, f"{rtype} — {primary_entity}", report)
                except Exception:
                    pass  # persistence failure shouldn't block returning the report itself

        csv_path = None
        if "screener" in intents:
            screener_results = _find_screener_results(evidence)
            if screener_results:
                csv_path = save_screener_csv(screener_results)

        return {
            "final_report": report,
            "pending_synthesis_prompt": None,
            "pending_fundamental_payload": None,
            "chart_html": chart,
            "pdf_path": pdf_path,
            "md_path": md_path,
            "csv_path": csv_path,
            "confidence_summary": _confidence_summary(conclusions),
            **log_decision(
                "CommunicationLayer",
                f"synthesized report from {len(conclusions)} conclusion(s)",
                f"chart={'rendered' if chart is not None else 'none'}"
                + (f", files=({md_path},{pdf_path})" if "report" in intents else "")
                + (f", csv={csv_path}" if "screener" in intents else ""),
            ),
        }

    return communication_layer
