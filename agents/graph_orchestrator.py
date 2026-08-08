"""
GraphOrchestrator: the sole orchestration path (agents/orchestrator.py and
its 6 specialist agent classes were retired once the graph reached feature
parity across all 6 domains — see ARCHITECTURE.md). Exposes
process/process_stream/generate_suggestions/reset/classify/load_history over
the LangGraph research engine (graph/graph.py) for app.py's
get_orchestrator() to build against.

Covers all 6 domains (equity/report/mf/screener/alert/planning) via
graph/tool_catalogue.py.

Streaming: for equity/chat turns with no synchronous file output,
CommunicationLayer defers its LLM call (`pending_synthesis_prompt` in
GraphState, see that module's `_is_streamable`) instead of calling
LLMClient.ask() itself — this class makes the actual `.stream()` call and
yields growing text chunk-by-chunk, matching the deleted
agents/equity_agent.py's behavior exactly. report/mf/screener/alert/planning
intents keep a single-shot yield (they need the finished text immediately
for file writes/DB saves), same as the legacy Orchestrator.

Human-in-the-loop note: when a HITL gate (graph/nodes/human_review.py)
pauses the graph, there's no dedicated confirm/decline UI control yet —
the interrupt's prompt is surfaced as the response text, and the user's
literal next message ("yes"/"no") is interpreted as the resume decision.
A proper UI affordance is a follow-up, not blocking this integration.

Cross-turn memory: the graph itself is stateless across turns by design (see
"Thread-id scoping" below) — query_analyzer.py/communication_layer.py get
conversation continuity from GraphState's `history` field instead, which
this class populates every invoke() from its own `ConversationMemory`
instance (`self._memory`, same class/cap/summarization behavior every
deleted specialist agent's LLMClient used to provide). `load_history()`
reseeds it on session resume; `reset()` clears it.

Thread-id scoping (Phase 7.4, corrected — see ARCHITECTURE.md's entry on this):
every fresh (non-resume) call to process_stream() mints a NEW thread_id;
only a resume call (`_awaiting_confirmation` True) reuses the current
thread_id, since interrupt/resume has to happen on the same thread by
LangGraph's design. This is the fix for a real state-leak risk: the
checkpointer persists GraphState per thread_id, and every reducer-backed
field in GraphState (`tasks`/`evidence`/`research_journal`/`loop_count`) is
append-only/merge-only by design (so Reflection can add tasks without
wiping already-`done` ones mid-run) — passing `"tasks": []` etc. on a fresh
invoke() does NOT clear prior state, the reducer just merges an empty list
against whatever's already checkpointed under that thread_id.

The thread_id itself is `user-{id}-{uuid4 hex}`, NOT the originally-shipped
`user-{id}-turn-{n}` — the deterministic counter collided with itself the
moment a fresh GraphOrchestrator instance was created for a user who'd
already talked before (an app restart/redeploy, or simply a new session
object, since `self._turn` is in-memory only but the Postgres checkpointer
persists durably across process lifetimes). Two instances both starting
`_turn` at 0 for the same user_id produced the identical thread_id
`user-{id}-turn-1`, and LangGraph's reducers then merged the OLD
checkpoint's tasks/evidence into the brand new turn — confirmed live: a
fresh orchestrator asking about RELIANCE got contaminated with a prior
process's TCS conclusions. A random UUID component per turn has no such
collision, costs nothing extra (Postgres checkpoint rows are cheap either
way), and needs no cross-process counter coordination.
"""
from __future__ import annotations

import uuid

import re

from langgraph.types import Command

from agents.conversation_memory import ConversationMemory
from config import TOKENS_SYNTHESIS
from graph.checkpointer import get_postgres_checkpointer
from graph.graph import build_graph

_APPROVE_WORDS = {"y", "yes", "approve", "approved", "proceed", "confirm", "ok", "okay"}
_DEFAULT_MAX_LOOPS = 2

# ── Cheap keyword pre-check for classify() (ported from the deleted
# agents/orchestrator.py::route() — this orchestrator's actual routing
# decision is the graph's own LLM-driven QueryAnalyzer node; this keyword
# scorer only answers app.py's cosmetic "does the timeframe suffix apply"
# question, without paying for a second LLM call per message. Kept
# self-contained here (no import of the deleted legacy orchestrator module)
# since classify() must survive its removal. ──────────────────────────────
_REPORT_KW = [
    "ic report", "initiating coverage", "result update", "quarterly results",
    "sector thematic", "thematic report", "quick note", "flash report",
    "generate report", "write report", "create report", "full report",
    "list reports", "saved reports", "my reports", "show reports",
]
_SCREENER_KW = [
    "screen", "screener", "top gainer", "top loser", "biggest mover", "volume spike",
    "52 week high", "52 week low", "bulk deal", "block deal", "fii buying", "dii selling",
    "promoter selling", "insider", "momentum", "value screen", "quality screen",
    "pe below", "pe above", "roe above", "roce above", "de below", "undervalued",
    "overvalued", "stocks with", "which stocks", "save screener", "saved screener",
    "list screener", "my screeners",
]
_MF_KW = [
    "mutual fund", " sip ", "sip ", "elss", "nav of", "expense ratio",
    "flexi cap fund", "flexicap fund", "large cap fund", "mid cap fund",
    "small cap fund", "debt fund", "hybrid fund", "index fund", "lumpsum",
    "fund manager", "amc ", "direct plan", "regular plan",
]
_ALERT_KW = [
    "alert me", "notify me", "let me know if", "ping me", "stop loss",
    "stoploss", "stop-loss", "target price", "drops below", "goes above",
    "my alerts", "my alert", "cancel my alert", "cancel alert",
    "pause alert", "resume alert", "delete alert", "set an alert",
    "create an alert",
]
_ALERT_CRUD_RE = re.compile(r"\b(cancel|delete|remove|pause|resume|reactivate|unpause)\b.{0,30}\balerts?\b", re.I)
_PLANNING_KW = [
    "investment strategy", "financial plan", "financial planning", "asset allocation",
    "how should i invest", "how do i invest", "retirement plan", "plan for retirement",
    "diversified portfolio", "diversify my portfolio", "build my portfolio",
    "long term goal", "long-term goal", "my financial goal", "invest for my goal",
    "invest for retirement", "wealth plan", "financial profile", "my risk tolerance",
    "build my plan", "build my strategy", "investment plan",
]


def _keyword_route(message: str) -> str:
    """Returns one of "equity"|"report"|"mf"|"screener"|"alert"|"planning"
    for this message. No last_tab continuity (unlike the deleted legacy
    route()) since classify()'s only caller (app.py's timeframe-suffix
    check) never had that context either."""
    q = f" {message.lower().strip()} "
    scores = {
        "report":   sum(1 for kw in _REPORT_KW   if kw in q),
        "screener": sum(1 for kw in _SCREENER_KW if kw in q),
        "mf":       sum(1 for kw in _MF_KW       if kw in q),
        "alert":    sum(1 for kw in _ALERT_KW    if kw in q) + (1 if _ALERT_CRUD_RE.search(q) else 0),
        "planning": sum(1 for kw in _PLANNING_KW if kw in q),
    }
    best_tab = max(scores, key=scores.get)
    return best_tab if scores[best_tab] > 0 else "equity"


_synthesis_client = None


def _get_synthesis_client():
    """Lazy singleton for the deferred equity/chat synthesis call — see
    graph/nodes/communication_layer.py's `_is_streamable`/`pending_synthesis_prompt`
    and this module's docstring's "Cross-turn memory" note. Uses the same
    system prompt CommunicationLayer's own client would have, imported
    directly rather than duplicated."""
    global _synthesis_client
    if _synthesis_client is None:
        from agents.llm_client import LLMClient
        from graph.nodes.communication_layer import _SYSTEM as _COMM_SYSTEM
        _synthesis_client = LLMClient(system_prompt=_COMM_SYSTEM)
    return _synthesis_client


_suggestion_client = None


def _get_suggestion_client():
    """Lazy singleton, mirrors graph/tool_catalogue.py's _get_extract_client
    — avoids constructing an LLMClient at import time for every process
    that imports this module, only paying for it once suggestions are
    actually requested."""
    global _suggestion_client
    if _suggestion_client is None:
        from agents.llm_client import LLMClient
        _suggestion_client = LLMClient(system_prompt="Generate short follow-up question suggestions.")
    return _suggestion_client


def _summarize_history(old_summary: str, chunk_text: str) -> str:
    """ConversationMemory's overflow summarizer — reuses the same lazy
    client as suggestions since both are cheap, single-shot, non-user-
    facing utility calls."""
    from config import TOKENS_FAST
    prompt = (
        f"Existing summary of earlier conversation (may be empty):\n{old_summary or '(none)'}\n\n"
        f"New older messages to fold in:\n{chunk_text}\n\n"
        "Write an updated concise summary (3-5 sentences) capturing key facts, figures, "
        "tickers, and decisions mentioned, so later replies stay consistent with this history. "
        "Return only the summary text."
    )
    return _get_suggestion_client().ask(prompt, temperature=0.2, max_tokens=TOKENS_FAST)


class GraphOrchestrator:
    def __init__(self, user_id: int):
        self._user_id = user_id
        self._turn = 0
        self._thread_id = self._new_thread_id()
        self._graph = build_graph(checkpointer=get_postgres_checkpointer())
        self._awaiting_confirmation = False
        # Cross-turn conversation memory the graph itself has none of (each
        # turn mints a fresh thread_id — see module docstring). Owned here,
        # rendered into GraphState's `history` field on every invoke() so
        # query_analyzer.py/communication_layer.py can read it, same 20-message
        # cap + overflow-summarization behavior every specialist agent's own
        # LLMClient used to give via this exact class.
        self._memory = ConversationMemory(cap=20, summarizer=_summarize_history)

    def _new_thread_id(self) -> str:
        return f"user-{self._user_id}-{uuid.uuid4().hex[:16]}"

    def classify(self, message: str) -> str:
        """Cheap pre-check used by app.py:102 to decide whether to append a
        timeframe keyword. Deliberately reuses the deterministic keyword
        `route()` rather than invoking the graph's own QueryAnalyzer node —
        running QueryAnalyzer here would mean a second LLM call on every
        single message just to decide on a cosmetic keyword suffix, doubling
        exactly the per-turn LLM-call cost the RFC's §17a latency/cost risk
        is about. This is an intentional, minor precision trade-off (the
        graph's actual routing decision is LLM-driven, this pre-check is
        keyword-driven), not an oversight — Orchestrator.classify() makes
        the same call for the same reason (its process_stream() also uses
        route() internally, so there's no divergence there at least)."""
        return _keyword_route(message)

    def process(self, message: str) -> tuple[str, object, str | None, str | None, str | None, str]:
        """Returns (text, chart, pdf_path, md_path, csv_path, tab_used) — the final result of process_stream()."""
        result = None
        for result in self.process_stream(message):
            pass
        return result

    def process_stream(self, message: str):
        """Yields (text, chart, pdf_path, md_path, csv_path, tab_used), text
        growing on each yield for equity/chat turns (CommunicationLayer
        deferred its LLM call to `pending_synthesis_prompt` — see that
        module's `_is_streamable`), or exactly once for every other domain,
        matching the deleted legacy Orchestrator's "only EquityAgent
        streams" behavior."""
        if self._awaiting_confirmation:
            # Must resume on the SAME thread the interrupt paused on.
            config = {"configurable": {"thread_id": self._thread_id}}
            approved = message.strip().lower() in _APPROVE_WORDS
            result = self._graph.invoke(Command(resume=approved), config=config)
        else:
            # Fresh top-level turn: mint a new thread_id so this run starts
            # with no prior GraphState to (incorrectly) merge against —
            # see module docstring's "Thread-id scoping" note.
            self._turn += 1
            self._thread_id = self._new_thread_id()
            config = {"configurable": {"thread_id": self._thread_id}}
            self._memory.add_user(message)
            result = self._graph.invoke(
                {
                    "query": message, "max_loops": _DEFAULT_MAX_LOOPS, "user_id": self._user_id,
                    "history": self._memory.context(),
                },
                config=config,
            )

        if result.get("__interrupt__"):
            self._awaiting_confirmation = True
            prompt = result["__interrupt__"][0].value.get("message", "Proceed?")
            reply = f"{prompt} (reply yes/no)"
            self._memory.add_assistant(reply)
            yield reply, None, None, None, None, "equity"
            return

        self._awaiting_confirmation = False
        chart = result.get("chart_html")
        pdf_path = result.get("pdf_path")
        md_path = result.get("md_path")
        csv_path = result.get("csv_path")
        # tab_used: primary intent, plus any secondary ones QueryAnalyzer
        # detected (Phase 7.4 multi-intent support), joined with "+" — still
        # a single string, so app.py's existing (text, chart, pdf, md, csv,
        # tab_used) 6-tuple contract needs no structural change. app.py's
        # _TAB_LABELS.get(tab_used, tab_used) falls back to the raw joined
        # string for a combo it doesn't have a pretty label for yet.
        intent = result.get("intent") or "equity"
        secondary = result.get("secondary_intents") or []
        tab_used = "+".join([intent] + [s for s in secondary if s != intent])

        pending_prompt = result.get("pending_synthesis_prompt")
        if pending_prompt:
            from tools.analysis.fundamental import cited_footnotes
            chunks: list[str] = []
            for delta in _get_synthesis_client().stream(pending_prompt, max_tokens=TOKENS_SYNTHESIS):
                chunks.append(delta)
                yield "".join(chunks), chart, None, None, None, tab_used
            text = "".join(chunks)
            fundamental_payload = result.get("pending_fundamental_payload")
            if fundamental_payload:
                text = text + cited_footnotes(text, fundamental_payload)
            self._memory.add_assistant(text)
            yield text, chart, pdf_path, md_path, csv_path, tab_used
            return

        text = result.get("final_report", "") or ""
        self._memory.add_assistant(text)
        yield text, chart, pdf_path, md_path, csv_path, tab_used

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        """Cheap follow-up-chip generator — same contract/prompt shape the
        deleted specialist agents each implemented individually (e.g.
        agents/equity_agent.py::generate_suggestions), now shared here since
        it's a UI concern of the orchestrator, not any one domain."""
        import json
        from config import GROQ_MODEL_FAST, TOKENS_FAST
        prompt = (
            f'User asked: "{last_query[:80]}". Response was about Indian stocks/funds/'
            f'reports/alerts/planning.\n'
            f'Return a JSON array of exactly 4 short follow-up questions (max 7 words each) '
            f'a user would ask next. Only the JSON array, nothing else.'
        )
        try:
            raw = _get_suggestion_client().ask(
                prompt, model=GROQ_MODEL_FAST, temperature=0.8, max_tokens=TOKENS_FAST,
            )
            raw = raw.strip().strip("```json").strip("```").strip()
            sug = json.loads(raw)
            if isinstance(sug, list):
                return [str(s) for s in sug[:4]]
        except Exception:
            pass
        return ["Analyse a stock", "Compare mutual funds", "Top gainers today", "Set a price alert"]

    def reset(self):
        self._awaiting_confirmation = False
        self._turn = 0
        self._thread_id = self._new_thread_id()
        self._memory.clear()

    def load_history(self, messages: list[dict]):
        """Resumes a saved chat: reseeds `self._memory` (the graph's only
        source of cross-turn context, since each turn mints a fresh
        thread_id — see module docstring) from the persisted transcript, so
        a resumed session's first new message still has the prior
        conversation available to query_analyzer.py/communication_layer.py
        via GraphState's `history` field. Also starts a fresh thread_id,
        consistent with reset(), so this doesn't merge against whatever
        thread this instance's OWN prior conversation last used."""
        self._awaiting_confirmation = False
        self._turn = 0
        self._thread_id = self._new_thread_id()
        self._memory.load(messages)
