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

import hashlib
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from typing import Any

from langgraph.types import Command

import prompts
from agents.conversation_memory import ConversationMemory, strip_badge
from agents.llm_client import LLMProvidersExhausted
from config import TOKENS_SYNTHESIS, TRIAL_DAILY_LIMIT, TRIAL_DAILY_TOKENS
from graph.analysis_tools import analysis_notes
from graph.checkpointer import get_turn_checkpointer
from graph.confirmation import REASK_TEXT as CONFIRM_REPROMPT
from graph.confirmation import classify_reply
from graph.deadline import CUT_OFF_NOTE, data_gaps_note, new_deadline
from graph.entity_resolution import entity_notes_text
from graph.freshness import closed_today_header, freshness_note
from graph.graph import build_graph
from graph.grounding import check as grounding_check
from graph.grounding import record as record_grounding
from graph.grounding import unverified_note
from graph.guardrails import check_input, finalize
from graph.progress import ProgressUpdate, run_with_progress

log = logging.getLogger(__name__)

_DEFAULT_MAX_LOOPS = 2

_TRIAL_EXHAUSTED_MESSAGE = (
    f"You've used today's {TRIAL_DAILY_LIMIT} free messages. Add your own LLM API key in "
    "Settings to keep chatting — any OpenAI-compatible provider works "
    "(OpenAI, Groq, OpenRouter, Together, Fireworks, Ollama, ...), or come "
    f"back tomorrow for another {TRIAL_DAILY_LIMIT} free."
)

# ── Cheap keyword pre-check for classify() (ported from the deleted
# agents/orchestrator.py::route() — this orchestrator's actual routing
# decision is the graph's own LLM-driven QueryAnalyzer node; this keyword
# scorer only answers app.py's cosmetic "does the timeframe suffix apply"
# question, without paying for a second LLM call per message. Kept
# self-contained here (no import of the deleted legacy orchestrator module)
# since classify() must survive its removal. ──────────────────────────────
_REPORT_KW = [
    "ic report",
    "initiating coverage",
    "result update",
    "quarterly results",
    "sector thematic",
    "thematic report",
    "quick note",
    "flash report",
    "generate report",
    "write report",
    "create report",
    "full report",
    "list reports",
    "saved reports",
    "my reports",
    "show reports",
]
_SCREENER_KW = [
    "screen",
    "screener",
    "top gainer",
    "top loser",
    "biggest mover",
    "volume spike",
    "52 week high",
    "52 week low",
    "bulk deal",
    "block deal",
    "fii buying",
    "dii selling",
    "promoter selling",
    "insider",
    "momentum",
    "value screen",
    "quality screen",
    "pe below",
    "pe above",
    "roe above",
    "roce above",
    "de below",
    "undervalued",
    "overvalued",
    "stocks with",
    "which stocks",
    "save screener",
    "saved screener",
    "list screener",
    "my screeners",
]
_MF_KW = [
    "mutual fund",
    " sip ",
    "sip ",
    "elss",
    "nav of",
    "expense ratio",
    "flexi cap fund",
    "flexicap fund",
    "large cap fund",
    "mid cap fund",
    "small cap fund",
    "debt fund",
    "hybrid fund",
    "index fund",
    "lumpsum",
    "fund manager",
    "amc ",
    "direct plan",
    "regular plan",
]
_ALERT_KW = [
    "alert me",
    "notify me",
    "let me know if",
    "ping me",
    "stop loss",
    "stoploss",
    "stop-loss",
    "target price",
    "drops below",
    "goes above",
    "my alerts",
    "my alert",
    "cancel my alert",
    "cancel alert",
    "pause alert",
    "resume alert",
    "delete alert",
    "set an alert",
    "create an alert",
]
_ALERT_CRUD_RE = re.compile(
    r"\b(cancel|delete|remove|pause|resume|reactivate|unpause)\b.{0,30}\balerts?\b", re.I
)
_PLANNING_KW = [
    "investment strategy",
    "financial plan",
    "financial planning",
    "asset allocation",
    "how should i invest",
    "how do i invest",
    "retirement plan",
    "plan for retirement",
    "diversified portfolio",
    "diversify my portfolio",
    "build my portfolio",
    "long term goal",
    "long-term goal",
    "my financial goal",
    "invest for my goal",
    "invest for retirement",
    "wealth plan",
    "financial profile",
    "my risk tolerance",
    "build my plan",
    "build my strategy",
    "investment plan",
]


def _keyword_route(message: str) -> str:
    """Returns one of "equity"|"report"|"mf"|"screener"|"alert"|"planning"
    for this message. No last_tab continuity (unlike the deleted legacy
    route()) since classify()'s only caller (app.py's timeframe-suffix
    check) never had that context either."""
    q = f" {message.lower().strip()} "
    scores = {
        "report": sum(1 for kw in _REPORT_KW if kw in q),
        "screener": sum(1 for kw in _SCREENER_KW if kw in q),
        "mf": sum(1 for kw in _MF_KW if kw in q),
        "alert": sum(1 for kw in _ALERT_KW if kw in q) + (1 if _ALERT_CRUD_RE.search(q) else 0),
        "planning": sum(1 for kw in _PLANNING_KW if kw in q),
    }
    best_tab = max(scores, key=scores.get)
    return best_tab if scores[best_tab] > 0 else "equity"


# REL-03: one compiled graph per provider configuration, shared by every
# conversation that uses it (all trial users share one). Node LLM clients are
# stateless (`ask`), and per-call token usage is thread-local, so concurrent
# turns on one graph don't interfere; each turn has its own thread_id.
_GRAPH_CACHE_SIZE = 64
_graphs: OrderedDict[str, Any] = OrderedDict()
_graphs_lock = threading.Lock()


def _config_fingerprint(provider_config: dict | None) -> str:
    blob = json.dumps(provider_config or {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def shared_graph(provider_config: dict | None) -> Any:
    key = _config_fingerprint(provider_config)
    with _graphs_lock:
        graph = _graphs.get(key)
        if graph is not None:
            _graphs.move_to_end(key)
            return graph
        graph = build_graph(checkpointer=get_turn_checkpointer(), provider_config=provider_config)
        _graphs[key] = graph
        while len(_graphs) > _GRAPH_CACHE_SIZE:
            _graphs.popitem(last=False)
        return graph


def clear_graph_cache() -> None:
    with _graphs_lock:
        _graphs.clear()


_SUGGESTIONS: dict[str, list[str]] = {
    "equity": ["Show the technicals", "Compare with peers", "Latest news", "Set a price alert"],
    "report": ["Show my saved reports", "Quick note instead", "Key risks", "Compare with a peer"],
    "mf": ["Compare two funds", "Best large cap funds", "Show NAV history", "ELSS funds"],
    "screener": ["Save this screen", "Add ROE above 15%", "Large caps only", "Top gainers today"],
    "alert": ["Show my alerts", "Pause an alert", "Alert on RSI", "Connect Telegram"],
    "planning": ["Show my profile", "Build my plan", "Change my risk tolerance", "Start over"],
}
_DEFAULT_SUGGESTIONS = ["Analyse a stock", "Compare mutual funds", "Top gainers today", "Set a price alert"]
_TICKER_RE = re.compile(r"\b[A-Z][A-Z&]{2,14}\b")


def suggestions_for(domain: str, last_query: str) -> list[str]:
    """Follow-up chips without an LLM call (LLM-11), naming the company the
    user just asked about when there is one."""
    chips = list(_SUGGESTIONS.get(domain, _DEFAULT_SUGGESTIONS))
    m = _TICKER_RE.search(last_query or "")
    if domain == "equity" and m:
        sym = m.group(0)
        chips = [f"{sym} technicals", f"Compare {sym} with peers", f"{sym} latest news", f"Alert me on {sym}"]
    return chips


def _figure(chart: Any) -> Any:
    """chart_html travels through checkpointed state as Plotly JSON; gr.Plot wants a Figure."""
    if isinstance(chart, str) and chart.startswith("{"):
        import plotly.io as pio

        return pio.from_json(chart)
    return chart


def _chart_note(chart: Any) -> str:
    """UX-03: the price chart in words, so the answer carries a text alternative."""
    from tools.charts.builder import chart_text_summary

    text = chart_text_summary(chart) if chart is not None else ""
    return f"\n\n{text}" if text else ""


class GraphOrchestrator:
    def __init__(self, user_id: int, session_id: int | None = None):
        self._user_id = user_id
        # VAL-10: state belongs to one chat session, not to the user — two
        # tabs on two chats never share memory or a pending confirmation.
        self._session_id = session_id
        self._turn = 0
        self._thread_id = self._new_thread_id()
        # BYO key resolved once per session (not re-checked every message) —
        # a key added in Settings mid-session takes effect on the next new
        # session/reset, not the very next message. Keeps `self._graph` a
        # stable object for the orchestrator's lifetime, same as before BYO
        # existed, rather than rebuilding it (and re-binding every node's
        # LLMClient) on every single turn for a value that rarely changes.
        self._provider_config = self._resolve_byo_config()
        self._graph = shared_graph(self._provider_config)
        self._awaiting_confirmation = False
        # Cross-turn conversation memory the graph itself has none of (each
        # turn mints a fresh thread_id — see module docstring). Owned here,
        # rendered into GraphState's `history` field on every invoke() so
        # query_analyzer.py/communication_layer.py can read it, same 20-message
        # cap + overflow-summarization behavior every specialist agent's own
        # LLMClient used to give via this exact class.
        self._memory = ConversationMemory(cap=20, summarizer=self._summarize_history)
        if session_id is not None:
            self._restore_session()

    def _restore_session(self) -> None:
        """REL-03/VAL-10: an orchestrator rebuilt after eviction, a restart or
        on another replica picks the chat up where it was — transcript from
        chat_history, and a pending confirmation from the session row (the
        paused graph state itself is in the Postgres checkpointer)."""
        from utils.db import chat_load, session_pending_get

        try:
            rows = chat_load(self._user_id, self._session_id)
            if rows:
                self._memory.load([{"role": m["role"], "content": strip_badge(m["content"])} for m in rows])
            pending = session_pending_get(self._user_id, self._session_id)
        except Exception:
            log.warning("[GraphOrchestrator] could not restore session state", exc_info=True)
            return
        if pending:
            self._thread_id = pending
            self._awaiting_confirmation = True
            ckpt = get_turn_checkpointer()
            if ckpt is not None:  # ARCH-04: this thread's checkpoint is in Postgres, not memory
                ckpt.mark_resumable(pending)

    def _set_pending(self, thread_id: str | None) -> None:
        """Persist (or clear) the paused thread on the session row."""
        if self._session_id is None:
            return
        from utils.db import session_pending_set

        try:
            session_pending_set(self._user_id, self._session_id, thread_id)
        except Exception:
            log.warning("[GraphOrchestrator] could not save the pending confirmation", exc_info=True)

    def _new_thread_id(self) -> str:
        return f"user-{self._user_id}-{uuid.uuid4().hex[:16]}"

    def _resolve_byo_config(self) -> dict | None:
        """Returns the decrypted BYO-key config if this user has one
        configured, else None (meaning: use the app's own trial Groq/Gemini
        keys, agents/llm_client.py's existing path). Falls back to the
        trial path (rather than raising out of __init__) if decrypt() fails
        — e.g. SECRET_KEY was unset/changed since the key was saved — same
        graceful-degrade philosophy as the rest of this constructor."""
        from utils.db import llm_config_get

        row = llm_config_get(self._user_id)
        if row is None:
            return None
        from utils.crypto import decrypt

        try:
            api_key = decrypt(row["api_key_encrypted"])
        except ValueError as e:
            log.error(
                f"[GraphOrchestrator] BYO key decrypt failed for user {self._user_id}, "
                f"falling back to trial path: {e}"
            )
            return None
        return {
            "provider": row["provider"],
            "base_url": row["base_url"],
            "model": row["model"],
            "api_key": api_key,
        }

    def _trial_quota_exceeded(self) -> bool:
        """Only meaningful when self._provider_config is None (no BYO key)
        — checked fresh every message, unlike the BYO config above, since
        the daily count genuinely changes turn to turn."""
        if self._provider_config is not None:
            return False
        from datetime import datetime

        from utils.db import trial_quota_check_and_increment
        from utils.helpers import IST

        today = datetime.now(IST).date()
        # LLM-11/SEC-12: a message can cost 5-9 model calls, so the daily
        # token budget is checked too (before spending a message).
        if TRIAL_DAILY_TOKENS:
            from utils.db import trial_tokens_used

            if trial_tokens_used(self._user_id, today) >= TRIAL_DAILY_TOKENS:
                return True
        allowed = trial_quota_check_and_increment(self._user_id, TRIAL_DAILY_LIMIT, today)
        return not allowed

    def _charge_trial_tokens(self, rec) -> None:
        """Add this turn's tokens to the trial user's daily budget (LLM-11)."""
        if self._provider_config is not None or not (rec.tokens_in or rec.tokens_out):
            return
        from datetime import datetime

        from utils.db import trial_tokens_add
        from utils.helpers import IST

        try:
            trial_tokens_add(self._user_id, rec.tokens_in + rec.tokens_out, datetime.now(IST).date())
        except Exception:
            log.warning("[GraphOrchestrator] could not record trial token use", exc_info=True)

    def _summarize_history(self, old_summary: str, chunk_text: str) -> str:
        """ConversationMemory's overflow summarizer — uses whichever
        provider this turn resolved (BYO or trial), never a separate
        hardcoded client, so a BYO user's summarization also runs on their
        own key/cost, not the app's trial quota."""
        from agents.conversation_memory import SUMMARY_PROMPT
        from agents.llm_client import LLMClient
        from config import TOKENS_FAST

        prompt = SUMMARY_PROMPT.format(old=old_summary or "(none)", chunk=chunk_text)
        client = LLMClient(
            system_prompt=prompts.get("orchestrator.summary_system"),
            provider_config=self._provider_config,
            node="summary",
        )
        return client.ask(prompt, temperature=0.2, max_tokens=TOKENS_FAST)

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
        """Returns (text, chart, pdf_url, md_url, csv_url, tab_used) — the final result of process_stream()."""
        result = None
        for result in self.process_stream(message):  # noqa: B007 — keep only the final yield
            pass
        return result

    _progress = False

    def _run_graph(self, payload: Any, config: dict):
        """The graph run for this turn: streamed with step progress when the
        caller asked for it (UX-02), else a plain invoke."""
        if self._progress:
            return (yield from run_with_progress(self._graph, payload, config))
        return self._graph.invoke(payload, config=config)

    def process_stream(self, message: str, progress: bool = False):
        """OBS-02 wrapper: runs the turn inside a utils/tracing TurnRecord
        (one `turns` row per turn, plus a LangSmith trace when enabled). The
        turn's id comes from the logging context app.py's run_in_turn set, so
        log lines, the turns row and the user-facing error ID all agree.

        UX-02: with progress=True, ProgressUpdate items (one per research
        step) are yielded before the answer's tuples."""
        from utils import tracing
        from utils.logging_setup import session_id_var, turn_id_var

        turn_id = turn_id_var.get() or uuid.uuid4().hex
        with tracing.turn(turn_id, self._user_id, session_id_var.get() or None, message) as rec:
            try:
                self._progress = progress
                for out in self._process_stream(message, rec):
                    if isinstance(out, ProgressUpdate):
                        yield out
                        continue
                    if out[0]:
                        rec.first_output()
                    yield out
            except GeneratorExit:
                rec.status = "cancelled"
                raise
            except Exception:
                rec.status = "error"
                raise
            finally:
                self._charge_trial_tokens(rec)
                ckpt = get_turn_checkpointer()
                if ckpt is not None:
                    ckpt.release(self._thread_id)  # ARCH-04: the turn's in-memory checkpoints

    def _process_stream(self, message: str, rec):
        """Yields (text, chart, pdf_url, md_url, csv_url, tab_used), text
        growing on each yield for equity/chat turns (CommunicationLayer
        deferred its LLM call to `pending_synthesis_prompt` — see that
        module's `_is_streamable`), or exactly once for every other domain,
        matching the deleted legacy Orchestrator's "only EquityAgent
        streams" behavior.

        LLMProvidersExhausted (agents/llm_client.py) can surface from any
        node's client.ask() inside self._graph.invoke() (query_analyzer.py,
        planner.py, communication_layer.py — none of them catch it locally,
        by design, since this is the one place that knows how to format a
        user-facing message either way) or from the deferred synthesis
        stream below. Caught here as a single top-level safety net rather
        than duplicated in every caller."""
        try:
            if self._awaiting_confirmation:
                # VAL-08: a lenient yes/no; a fresh question cancels the
                # pending step and runs as its own turn (never a silent "no").
                kind = classify_reply(message)
                if kind == "unclear":
                    rec.status = "gated"
                    self._memory.add_user(message)
                    self._memory.add_assistant(CONFIRM_REPROMPT)
                    yield CONFIRM_REPROMPT, None, None, None, None, "equity"
                    return
                if kind == "new_question":
                    log.info("[orchestrator] new question while a confirmation was pending; cancelling it")
                    self._awaiting_confirmation = False
                    self._set_pending(None)
            if self._awaiting_confirmation:
                # Must resume on the SAME thread the interrupt paused on — the
                # config resolved for the turn that raised this interrupt is
                # still current in self._provider_config, no need to re-resolve.
                config = {"configurable": {"thread_id": self._thread_id}}
                approved = kind == "yes"
                # A fresh deadline: the user's think time at the gate mustn't
                # count against the research budget (REL-02).
                result = yield from self._run_graph(Command(resume=approved, update=new_deadline()), config)
            else:
                # LLM-09: refused requests get a fixed reply — no research, no
                # LLM call, and no trial quota spent.
                verdict = check_input(message)
                if verdict.action == "refuse":
                    rec.status = "refused"
                    log.info(f"[guardrails] refused ({verdict.category})")
                    self._memory.add_user(message)
                    self._memory.add_assistant(verdict.reply or "")
                    yield verdict.reply, None, None, None, None, "chat"
                    return
                if self._trial_quota_exceeded():
                    rec.status = "quota"
                    self._memory.add_user(message)
                    self._memory.add_assistant(_TRIAL_EXHAUSTED_MESSAGE)
                    yield _TRIAL_EXHAUSTED_MESSAGE, None, None, None, None, "equity"
                    return

                # Fresh top-level turn: mint a new thread_id so this run starts
                # with no prior GraphState to (incorrectly) merge against —
                # see module docstring's "Thread-id scoping" note.
                self._turn += 1
                self._thread_id = self._new_thread_id()
                config = {"configurable": {"thread_id": self._thread_id}}
                self._memory.add_user(message)
                result = yield from self._run_graph(
                    {
                        "query": message,
                        "max_loops": _DEFAULT_MAX_LOOPS,
                        "user_id": self._user_id,
                        "history": self._memory.context(),
                        "llm_config": self._provider_config,
                        "advice_mode": "reframe" if verdict.action == "reframe" else None,
                        "advice_caution": verdict.caution,
                        **new_deadline(),
                    },
                    config,
                )

            rec.intent = result.get("intent") or rec.intent
            rec.complexity = result.get("complexity") or rec.complexity
            if result.get("__interrupt__"):
                rec.status = "gated"
                self._awaiting_confirmation = True
                self._set_pending(self._thread_id)
                ckpt = get_turn_checkpointer()
                if ckpt is not None:  # ARCH-04: only a paused turn is worth a Postgres write
                    ckpt.persist(self._thread_id)
                gate = result["__interrupt__"][0].value
                from utils import metrics

                metrics.observe_gate(gate.get("type", "unknown"))
                prompt = gate.get("message", "Proceed?")
                reply = f"{prompt} (reply yes/no, or use the buttons)"
                self._memory.add_assistant(reply)
                yield reply, None, None, None, None, "equity"
                return

            if self._awaiting_confirmation:
                self._set_pending(None)
                ckpt = get_turn_checkpointer()
                if ckpt is not None:
                    ckpt.finish(self._thread_id)  # the paused copy is no longer needed
            self._awaiting_confirmation = False
            chart = _figure(result.get("chart_html"))
            pdf_url = result.get("pdf_url")
            md_url = result.get("md_url")
            csv_url = result.get("csv_url")
            # tab_used: primary intent, plus any secondary ones QueryAnalyzer
            # detected (Phase 7.4 multi-intent support), joined with "+" — still
            # a single string, so app.py's existing (text, chart, pdf, md, csv,
            # tab_used) 6-tuple contract needs no structural change. app.py's
            # _TAB_LABELS.get(tab_used, tab_used) falls back to the raw joined
            # string for a combo it doesn't have a pretty label for yet.
            intent = result.get("intent") or "equity"
            secondary = result.get("secondary_intents") or []
            tab_used = "+".join([intent] + [s for s in secondary if s != intent])

            gaps_note = entity_notes_text(result) + data_gaps_note(result.get("tasks"))
            # DATA-06: market figures say when they're from (and, on a closed
            # day, that they're the last session's).
            header = closed_today_header(result)
            as_of_note = analysis_notes(result) + freshness_note(result) + _chart_note(chart)
            rec.data_gaps = sum(
                1 for t in result.get("tasks") or [] if t.get("status") in ("failed", "skipped")
            )
            rec.status = "partial" if rec.data_gaps else "ok"
            pending_prompt = result.get("pending_synthesis_prompt")
            if pending_prompt:
                from agents.llm_client import LLMClient
                from graph.nodes.communication_layer import _SYSTEM as _COMM_SYSTEM
                from tools.analysis.fundamental import cited_footnotes

                synthesis_client = LLMClient(
                    system_prompt=_COMM_SYSTEM, provider_config=self._provider_config, node="communication"
                )
                chunks: list[str] = []
                cut_off = False
                try:
                    for delta in synthesis_client.stream(pending_prompt, max_tokens=TOKENS_SYNTHESIS):
                        chunks.append(delta)
                        yield header + "".join(chunks), chart, None, None, None, tab_used
                except Exception:
                    # REL-06: a stream that dies mid-answer keeps what arrived,
                    # plus a plain note; nothing streamed yet -> the normal
                    # error boundary (LLMProvidersExhausted / render_error).
                    if not chunks:
                        raise
                    log.warning("[GraphOrchestrator] synthesis stream broke mid-answer", exc_info=True)
                    rec.status = "partial"
                    cut_off = True
                text = "".join(chunks)
                if cut_off:
                    text += CUT_OFF_NOTE
                # LLM-03, streamed path: checked once the stream ends; anything
                # unsupported gets a note in the final text.
                grounded = grounding_check(text, pending_prompt)
                record_grounding(grounded)
                if not grounded.ok:
                    text += unverified_note(grounded.unsupported)
                fundamental_payload = result.get("pending_fundamental_payload")
                if fundamental_payload:
                    text = text + cited_footnotes(text, fundamental_payload)
                text = header + finalize(text + as_of_note, result) + gaps_note
                self._memory.add_assistant(text)
                yield text, chart, pdf_url, md_url, csv_url, tab_used
                return

            report = result.get("final_report", "") or ""
            text = (header + finalize(report + as_of_note, result) if report else "") + gaps_note
            self._memory.add_assistant(text)
            yield text, chart, pdf_url, md_url, csv_url, tab_used
        except LLMProvidersExhausted as e:
            from agents.llm_client import format_exhausted_message

            rec.status = "llm_unavailable"
            text = format_exhausted_message(e)
            self._memory.add_assistant(text)
            yield text, None, None, None, None, "equity"

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        """Cheap follow-up-chip generator — same contract/prompt shape the
        deleted specialist agents each implemented individually (e.g.
        agents/equity_agent.py::generate_suggestions), now shared here since
        it's a UI concern of the orchestrator, not any one domain. Uses
        whichever provider the last-resolved turn used (BYO or trial) —
        never a separate hardcoded client."""

        # VAL-05: a planning reply waiting for an explicit build gets a real
        # "Build my plan" button (suggestion chips are buttons) — no LLM call.
        if "say **build my plan**" in (last_response or ""):
            return ["Build my plan", "Show my profile", "Start over", "Change my risk tolerance"]

        # LLM-11: on the operator's trial keys, chips come from the domain —
        # no model call. A BYO user's own model still writes tailored ones.
        if self._provider_config is None:
            return suggestions_for(self.classify(last_query or ""), last_query or "")

        from agents.llm_client import LLMClient
        from config import TOKENS_FAST

        prompt = prompts.get("orchestrator.suggestions").format(query=last_query[:80])
        try:
            client = LLMClient(
                system_prompt=prompts.get("orchestrator.suggestions_system"),
                node="suggestions",
                provider_config=self._provider_config,
            )
            raw = client.ask(prompt, temperature=0.8, max_tokens=TOKENS_FAST)
            from utils.llm_json import ParseFailure, extract_json, record_parse_failure

            try:
                sug = extract_json(raw)
            except ParseFailure as e:
                record_parse_failure("suggestions", str(e))
                sug = None
            if isinstance(sug, list):
                return [str(s) for s in sug[:4]]
        except Exception:
            pass
        return ["Analyse a stock", "Compare mutual funds", "Top gainers today", "Set a price alert"]

    def reset(self):
        if self._awaiting_confirmation:
            self._set_pending(None)
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
