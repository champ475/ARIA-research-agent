"""
Graph assembly (RFC_AGENTIC_RESEARCH_GRAPH.md Sec4). Full node set is now
wired: QueryAnalyzer -> Planner -> ResearchConfirmation (HITL gate) ->
[ResearchExecutor <-> execute_task_node loop] -> EvidenceSummarizer ->
Reflection -> Verification -> VerificationGate (HITL gate) ->
ConclusionBuilder -> CommunicationLayer. Single-ticker equity queries only
for now (tool catalogue scope, graph/tool_catalogue.py) — report/mf/
screener/alert/planning intents are added in Phase 7. `build_graph()` takes
an optional `checkpointer` (graph/checkpointer.py's Postgres saver in
production, an InMemorySaver in tests) — required for the HITL gates'
interrupt()/resume to work at all, since a pause has to persist somewhere.

Reflection's loop-back skips Planner and routes straight to
ResearchExecutor: Reflection already builds and upserts new Tasks itself
(reusing Planner's tool-selection/expansion helpers, graph/nodes/reflection.py),
so re-entering Planner would just be a second, redundant LLM call over the
same objective. loop_count is a shared budget incremented by whichever side
(Reflection when it queues new tasks, Verification when it finds issues)
is asking for another pass — see graph/nodes/verification.py's docstring for
why Verification also has to increment it.

Both HITL gates (graph/nodes/human_review.py) follow the same pattern: if
the user declines, the gate node sets final_report directly and its routing
function sends control straight to END, skipping the rest of the pipeline —
otherwise a later node would silently overwrite the decline with its own
output.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.llm_client import LLMClient
from graph.nodes.communication_layer import build_communication_layer_node
from graph.nodes.conclusion_builder import conclusion_builder
from graph.nodes.evidence_summarizer import evidence_summarizer
from graph.nodes.execute_task import execute_task_node
from graph.nodes.human_review import research_confirmation, verification_gate
from graph.nodes.planner import build_planner_node
from graph.nodes.query_analyzer import build_query_analyzer_node
from graph.nodes.reflection import build_reflection_node
from graph.nodes.research_executor import research_executor, route_after_research_executor
from graph.nodes.verification import build_verification_node
from graph.state import GraphState


def build_graph(
    query_analyzer_client: LLMClient | None = None,
    planner_client: LLMClient | None = None,
    reflection_client: LLMClient | None = None,
    verification_client: LLMClient | None = None,
    communication_client: LLMClient | None = None,
    checkpointer=None,
    provider_config: dict | None = None,
) -> CompiledStateGraph:
    """`provider_config` (BYO-key dict or None for the trial path) is used
    ONLY as a default when the corresponding `*_client` isn't explicitly
    passed — tests inject stub clients directly and are unaffected."""
    graph = StateGraph(GraphState)

    graph.add_node("QueryAnalyzer", build_query_analyzer_node(query_analyzer_client, provider_config))
    graph.add_node("Planner", build_planner_node(planner_client, provider_config))
    graph.add_node("ResearchConfirmation", research_confirmation)
    graph.add_node("ResearchExecutor", research_executor)
    graph.add_node("execute_task_node", execute_task_node)
    graph.add_node("EvidenceSummarizer", evidence_summarizer)
    graph.add_node("Reflection", build_reflection_node(reflection_client, provider_config))
    graph.add_node("Verification", build_verification_node(verification_client, provider_config))
    graph.add_node("VerificationGate", verification_gate)
    graph.add_node("ConclusionBuilder", conclusion_builder)
    graph.add_node("CommunicationLayer", build_communication_layer_node(communication_client, provider_config))

    graph.add_edge(START, "QueryAnalyzer")
    graph.add_edge("QueryAnalyzer", "Planner")

    def _route_after_planner(state: GraphState) -> str:
        return "ResearchConfirmation" if state.get("tasks") else "ConclusionBuilder"

    graph.add_conditional_edges(
        "Planner", _route_after_planner, ["ResearchConfirmation", "ConclusionBuilder"]
    )

    def _route_after_research_confirmation(state: GraphState) -> str:
        return END if state.get("final_report") else "ResearchExecutor"

    graph.add_conditional_edges(
        "ResearchConfirmation", _route_after_research_confirmation, ["ResearchExecutor", END]
    )
    # ResearchExecutor <-> execute_task_node: each wave of ready tasks fans
    # out via Send (route_after_research_executor), then loops back to
    # ResearchExecutor to re-evaluate the ready set until nothing is left,
    # at which point the route function sends control to EvidenceSummarizer
    # instead of dispatching another wave.
    graph.add_conditional_edges(
        "ResearchExecutor", route_after_research_executor, ["execute_task_node", "EvidenceSummarizer"]
    )
    graph.add_edge("execute_task_node", "ResearchExecutor")
    graph.add_edge("EvidenceSummarizer", "Reflection")

    def _route_after_reflection(state: GraphState) -> str:
        # Reflection already built and upserted any new Tasks itself, so a
        # pending task existing here means Reflection just queued one —
        # nothing else in this part of the graph produces a pending task.
        has_pending = any(t["status"] == "pending" for t in state.get("tasks", []))
        return "ResearchExecutor" if has_pending else "Verification"

    graph.add_conditional_edges(
        "Reflection", _route_after_reflection, ["ResearchExecutor", "Verification"]
    )

    def _route_after_verification(state: GraphState) -> str:
        issues = state.get("verification_issues") or []
        loop_count = state.get("loop_count", 0)
        max_loops = state.get("max_loops", 2)
        return "Reflection" if issues and loop_count < max_loops else "VerificationGate"

    graph.add_conditional_edges(
        "Verification", _route_after_verification, ["Reflection", "VerificationGate"]
    )

    def _route_after_verification_gate(state: GraphState) -> str:
        return END if state.get("final_report") else "ConclusionBuilder"

    graph.add_conditional_edges(
        "VerificationGate", _route_after_verification_gate, ["ConclusionBuilder", END]
    )
    graph.add_edge("ConclusionBuilder", "CommunicationLayer")
    graph.add_edge("CommunicationLayer", END)

    return graph.compile(checkpointer=checkpointer)
