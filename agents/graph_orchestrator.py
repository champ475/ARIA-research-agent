"""
GraphOrchestrator: drop-in adapter exposing agents.orchestrator.Orchestrator's
interface (process/process_stream/generate_suggestions/reset) over the
LangGraph research engine (graph/graph.py), so app.py's get_orchestrator()
can switch between the two behind the USE_LANGGRAPH_ORCHESTRATOR flag
without any other integration-point changes.

Scope for now matches the graph's own scope (single-ticker equity queries
only, tool catalogue in graph/tool_catalogue.py) — report/mf/screener/alert/
planning intents route to the graph too, but the graph can't do anything
useful with them yet until Phase 7 extends the tool catalogue.

Streaming note: process_stream() currently yields exactly one result at the
end, not token-by-token — CommunicationLayer (graph/nodes/communication_layer.py)
calls LLMClient.ask(), not .stream(). This matches how report/mf/screener/
alert/planning already behave in the existing Orchestrator (only EquityAgent
streams there too); true incremental streaming for the graph is a future
improvement, not a regression introduced here.

Human-in-the-loop note: when a HITL gate (graph/nodes/human_review.py)
pauses the graph, there's no dedicated confirm/decline UI control yet —
the interrupt's prompt is surfaced as the response text, and the user's
literal next message ("yes"/"no") is interpreted as the resume decision.
A proper UI affordance is a follow-up, not blocking this integration.

Thread-id scoping (Phase 7.4): every fresh (non-resume) call to
process_stream() mints a NEW thread_id (`user-{id}-turn-{n}`), incrementing
`n`; only a resume call (`_awaiting_confirmation` True) reuses the current
thread_id, since interrupt/resume has to happen on the same thread by
LangGraph's design. This is the fix for a real state-leak risk: the
checkpointer persists GraphState per thread_id, and every reducer-backed
field in GraphState (`tasks`/`evidence`/`research_journal`/`loop_count`) is
append-only/merge-only by design (so Reflection can add tasks without
wiping already-`done` ones mid-run) — passing `"tasks": []` etc. on a fresh
invoke() does NOT clear prior state, the reducer just merges an empty list
against whatever's already checkpointed under that thread_id. A brand-new
thread_id per top-level turn sidesteps this entirely: there's no prior
checkpoint to merge against, so no leakage is possible, and it costs nothing
extra since Postgres checkpoint rows are cheap and never queried across
turn boundaries. See ARCHITECTURE.md's entry on this for the full reasoning
(including why the originally-planned "clear specific fields" approach
turned out not to be implementable given LangGraph's reducer semantics).
"""
from __future__ import annotations

from langgraph.types import Command

from agents.orchestrator import route as _keyword_route
from graph.checkpointer import get_postgres_checkpointer
from graph.graph import build_graph

_APPROVE_WORDS = {"y", "yes", "approve", "approved", "proceed", "confirm", "ok", "okay"}
_DEFAULT_MAX_LOOPS = 2


class GraphOrchestrator:
    def __init__(self, user_id: int):
        self._user_id = user_id
        self._turn = 0
        self._thread_id = self._new_thread_id()
        self._graph = build_graph(checkpointer=get_postgres_checkpointer())
        self._awaiting_confirmation = False

    def _new_thread_id(self) -> str:
        return f"user-{self._user_id}-turn-{self._turn}"

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
        """Yields exactly one (text, chart, pdf_path, md_path, csv_path, tab_used) tuple — see module docstring."""
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
            result = self._graph.invoke(
                {"query": message, "max_loops": _DEFAULT_MAX_LOOPS, "user_id": self._user_id}, config=config
            )

        if result.get("__interrupt__"):
            self._awaiting_confirmation = True
            prompt = result["__interrupt__"][0].value.get("message", "Proceed?")
            yield f"{prompt} (reply yes/no)", None, None, None, None, "equity"
            return

        self._awaiting_confirmation = False
        text = result.get("final_report", "") or ""
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
        yield text, chart, pdf_path, md_path, csv_path, tab_used

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        # No suggestions node on the graph yet — matches an agent that
        # simply has nothing to offer, not an error.
        return []

    def reset(self):
        self._awaiting_confirmation = False
        self._turn = 0
        self._thread_id = self._new_thread_id()

    def load_history(self, messages: list[dict]):
        """No-op (Phase 7.4 bug fix): app.py's sidebar session-resume flow
        calls this unconditionally on whichever orchestrator is active —
        without this method, resuming a past session while
        USE_LANGGRAPH_ORCHESTRATOR is on would raise AttributeError. There's
        nothing to seed yet: unlike Orchestrator's specialist agents (each
        wrapping an LLMClient with real multi-turn ConversationMemory), the
        graph's per-node LLMClients (query_analyzer/planner/etc.) make
        single-shot calls with no persistent chat memory to restore. Also
        starts a fresh thread_id, consistent with reset(), so a resumed
        session's first message doesn't merge against whatever thread this
        instance's OWN prior conversation last used."""
        self._awaiting_confirmation = False
        self._turn = 0
        self._thread_id = self._new_thread_id()
