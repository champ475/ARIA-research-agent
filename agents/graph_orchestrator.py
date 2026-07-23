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
"""
from __future__ import annotations

from langgraph.types import Command

from graph.checkpointer import get_postgres_checkpointer
from graph.graph import build_graph

_APPROVE_WORDS = {"y", "yes", "approve", "approved", "proceed", "confirm", "ok", "okay"}
_DEFAULT_MAX_LOOPS = 2


class GraphOrchestrator:
    def __init__(self, user_id: int):
        self._user_id = user_id
        self._thread_id = f"user-{user_id}"
        self._graph = build_graph(checkpointer=get_postgres_checkpointer())
        self._awaiting_confirmation = False

    def process(self, message: str) -> tuple[str, object, str | None, str | None, str | None, str]:
        """Returns (text, chart, pdf_path, md_path, csv_path, tab_used) — the final result of process_stream()."""
        result = None
        for result in self.process_stream(message):
            pass
        return result

    def process_stream(self, message: str):
        """Yields exactly one (text, chart, pdf_path, md_path, csv_path, tab_used) tuple — see module docstring."""
        config = {"configurable": {"thread_id": self._thread_id}}

        if self._awaiting_confirmation:
            approved = message.strip().lower() in _APPROVE_WORDS
            result = self._graph.invoke(Command(resume=approved), config=config)
        else:
            result = self._graph.invoke({"query": message, "max_loops": _DEFAULT_MAX_LOOPS}, config=config)

        if result.get("__interrupt__"):
            self._awaiting_confirmation = True
            prompt = result["__interrupt__"][0].value.get("message", "Proceed?")
            yield f"{prompt} (reply yes/no)", None, None, None, None, "equity"
            return

        self._awaiting_confirmation = False
        text = result.get("final_report", "") or ""
        chart = result.get("chart_html")
        yield text, chart, None, None, None, "equity"

    def generate_suggestions(self, last_response: str, last_query: str) -> list[str]:
        # No suggestions node on the graph yet — matches an agent that
        # simply has nothing to offer, not an error.
        return []

    def reset(self):
        self._awaiting_confirmation = False
