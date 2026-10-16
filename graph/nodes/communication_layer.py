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
keeps the "only CommunicationLayer calls the model" contract intact. OPS-02:
nothing is rendered here — the report is saved to `reports.content` and the
answer carries /download/report/<id>.pdf|.md links, which any replica serves
by re-rendering the stored text (web/download_routes.py).

Report list/re-download (Phase 3 of the open-source restructure):
`_handle_report_management` bypasses the Evidence/Conclusion pipeline
entirely for "show my reports" / "download report #N" style queries — same
reasoning as the planning bypass below, there's no tool-selection/evidence-
gathering step useful here, just a DB read and (for a download) a render.

Screener-domain CSV export (Phase 7.3): `csv_url` points at the raw
`screener_run` rows (not a Conclusion — row-level output, not a decided
fact), stored for a day in the `downloads` table (OPS-02).

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

import logging
import re
from collections.abc import Callable

import pandas as pd

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
from graph.guardrails import finalize
from graph.journal import log_decision
from graph.nodes.conclusion_builder import CANONICAL_RATIO_KEYS
from graph.nodes.execute_task import from_cacheable
from graph.prompt_safety import wrap_untrusted
from graph.state import Conclusion, GraphState
from tools.analysis.fundamental import cited_footnotes
from tools.charts.builder import candlestick
from utils.audit import audit
from utils.db import (
    download_put,
    profile_clear,
    profile_get,
    profile_upsert,
    report_get,
    report_list,
    report_save,
)
from utils.escaping import md_cell
from utils.financial_profile import is_complete as _profile_complete
from utils.financial_profile import profile_summary as _profile_summary
from utils.financial_profile import sanitize_profile as _sanitize_profile
from utils.financial_profile import wants_build as _wants_build
from utils.helpers import detect_report_type
from utils.planning_logic import (
    build_strategy as _planning_build_strategy,
)
from utils.planning_logic import (
    intake_turn as _planning_intake_turn,
)
from utils.screener_files import screener_csv_bytes

_PLANNING_SHOW_KW = ["show my profile", "my financial profile", "what do you know about me"]
_PLANNING_RESET_KW = [
    "start over",
    "reset my plan",
    "reset my profile",
    "forget my profile",
    "clear my profile",
]


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


def report_urls(report_id: int) -> dict[str, str]:
    """OPS-02: download links any replica can serve (web/download_routes.py)."""
    return {"pdf_url": f"/download/report/{report_id}.pdf", "md_url": f"/download/report/{report_id}.md"}


def _empty_result(final_report: str, decision: str, reason: str) -> dict:
    return {
        "final_report": final_report,
        "pending_synthesis_prompt": None,
        "pending_fundamental_payload": None,
        "chart_html": None,
        "pdf_url": None,
        "md_url": None,
        "csv_url": None,
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
# VAL-08: only "report #12" / "report 12" names an id — "report for 2024" doesn't.
_REPORT_ID_RE = re.compile(r"\breport\s*(?:#|no\.?|number)?\s*(\d+)\b", re.IGNORECASE)


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
                    f"| {r['id']} | {md_cell(r['report_type'])} | {md_cell(r['title'])} | "
                    f"{r['created_at'].strftime('%Y-%m-%d')} |"
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
        # OPS-02: the link re-renders from reports.content on download.
        result = _empty_result(
            f"Here's report #{report_id} — **{row['title']}**.",
            "report: regenerated from history",
            f"user_id={user_id}, report_id={report_id}",
        )
        result.update(report_urls(report_id))
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

    # VAL-05: stored rows from before validation may hold invalid values —
    # never plan on them; they're simply asked for again.
    profile = _sanitize_profile(profile_get(user_id))
    updates, minimum_complete, reply = _planning_intake_turn(client, query, profile)
    if updates:
        profile = _sanitize_profile(profile_upsert(user_id, updates))
    minimum_complete = _profile_complete(profile)

    if not minimum_complete:
        return _empty_result(
            reply, "planning: intake incomplete", f"user_id={user_id}, updates={list(updates)}"
        )

    if _wants_build(query):
        strategy = _planning_build_strategy(client, profile)
        audit("plan.generate", actor_user_id=user_id, details={"fields": sorted(profile)})
        return _empty_result(strategy, "planning: built strategy", f"user_id={user_id}")

    # Complete but not asked for: show exactly what was saved first (the
    # capital-allocation check), and build only on an explicit request.
    text = (
        f"{reply}\n\n**Here's what I've saved — please check it:**\n\n{_profile_summary(profile)}\n\n"
        "When this looks right, say **build my plan** (or correct anything above)."
    )
    return _empty_result(text, "planning: profile complete, awaiting explicit build", f"user_id={user_id}")


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

        urls: dict[str, str | None] = {"pdf_url": None, "md_url": None, "csv_url": None}
        if "report" in intents and primary_entity:
            rtype = detect_report_type((query or objective).lower())
            # LLM-09: the saved/downloaded report carries the same checks and
            # footer as the chat answer (finalize() is idempotent).
            report = finalize(report, state)
            user_id = state.get("user_id")
            if user_id is not None:
                try:
                    report_id = report_save(
                        user_id, primary_entity, rtype, f"{rtype} — {primary_entity}", report
                    )
                    urls.update(report_urls(report_id))
                    audit(
                        "report.generate",
                        actor_user_id=user_id,
                        target=primary_entity,
                        details={"type": rtype},
                    )
                except Exception as e:
                    # Persistence failure shouldn't block returning the report itself.
                    log.warning(f"[CommunicationLayer] report_save failed: {e}")

        if "screener" in intents and state.get("user_id") is not None:
            screener_results = _find_screener_results(evidence)
            if screener_results:
                try:
                    token = download_put(
                        state["user_id"],
                        "aria_screener_results.csv",
                        "text/csv",
                        screener_csv_bytes(screener_results),
                    )
                    urls["csv_url"] = f"/download/file/{token}"
                except Exception as e:
                    log.warning("[CommunicationLayer] screener CSV not stored: %s", type(e).__name__)

        return {
            "final_report": report,
            "pending_synthesis_prompt": None,
            "pending_fundamental_payload": None,
            "chart_html": chart,
            **urls,
            "confidence_summary": _confidence_summary(conclusions),
            **log_decision(
                "CommunicationLayer",
                f"synthesized report from {len(conclusions)} conclusion(s)",
                f"chart={'rendered' if chart is not None else 'none'}"
                + (f", report={urls['pdf_url']}" if "report" in intents else "")
                + (f", csv={'yes' if urls['csv_url'] else 'no'}" if "screener" in intents else ""),
            ),
        }

    return communication_layer
