"""
Graph assembly (RFC_AGENTIC_RESEARCH_GRAPH.md Sec4). Full node set is now
wired: QueryAnalyzer -> Planner -> ResearchConfirmation (HITL gate) ->
[ResearchExecutor <-> execute_task_node loop] -> EvidenceSummarizer ->
Reflection -> Verification -> VerificationGate (HITL gate) ->
ConclusionBuilder -> CommunicationLayer -> [OutputFiles, report and screener
turns only]. ARCH-05: two intents skip research and end their own turn right
after QueryAnalyzer: planning (PlanningNode) and "show/download my reports"
(ReportManagementNode). `build_graph()` takes an optional `checkpointer`
(graph/checkpointer.py's TurnCheckpointer in production: in memory, with a
paused turn copied to Postgres; an InMemorySaver in tests) — required for the
HITL gates' interrupt()/resume to work at all.

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
from graph.entity_resolution import blocking_resolutions
from graph.nodes.communication_layer import build_communication_layer_node
from graph.nodes.conclusion_builder import conclusion_builder
from graph.nodes.evidence_summarizer import evidence_summarizer
from graph.nodes.execute_task import execute_task_node
from graph.nodes.human_review import research_confirmation, verification_gate
from graph.nodes.output_files import output_files, wants_files
from graph.nodes.planner import build_planner_node
from graph.nodes.planning_node import build_planning_node
from graph.nodes.query_analyzer import build_query_analyzer_node
from graph.nodes.reflection import build_reflection_node
from graph.nodes.report_management import is_report_management, report_management
from graph.nodes.research_executor import research_executor, route_after_research_executor
from graph.nodes.verification import build_verification_node
from graph.state import GraphState
from utils.tracing import traced_node


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

    def _add(name: str, fn) -> None:
        # OBS-02: every node gets a trace span and its wall time on the turn record.
        graph.add_node(name, traced_node(name, fn))

    _add("QueryAnalyzer", build_query_analyzer_node(query_analyzer_client, provider_config))
    _add("Planner", build_planner_node(planner_client, provider_config))
    _add("ResearchConfirmation", research_confirmation)
    _add("ResearchExecutor", research_executor)
    _add("execute_task_node", execute_task_node)
    _add("EvidenceSummarizer", evidence_summarizer)
    _add("Reflection", build_reflection_node(reflection_client, provider_config))
    _add("Verification", build_verification_node(verification_client, provider_config))
    _add("VerificationGate", verification_gate)
    _add("ConclusionBuilder", conclusion_builder)
    _add("CommunicationLayer", build_communication_layer_node(communication_client, provider_config))
    _add("PlanningNode", build_planning_node(communication_client, provider_config))
    _add("ReportManagementNode", report_management)
    _add("OutputFiles", output_files)

    graph.add_edge(START, "QueryAnalyzer")

    def _route_after_query_analyzer(state: GraphState) -> str:
        # An unresolved company name is answered by CommunicationLayer's
        # clarification, whatever the intent, so it never reaches these two.
        if blocking_resolutions(state):
            return "Planner"
        if state.get("intent") == "planning":
            return "PlanningNode"
        if is_report_management(state):
            return "ReportManagementNode"
        return "Planner"

    graph.add_conditional_edges(
        "QueryAnalyzer", _route_after_query_analyzer, ["Planner", "PlanningNode", "ReportManagementNode"]
    )
    graph.add_edge("PlanningNode", END)
    graph.add_edge("ReportManagementNode", END)

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

    graph.add_conditional_edges("Reflection", _route_after_reflection, ["ResearchExecutor", "Verification"])

    def _route_after_verification(state: GraphState) -> str:
        # loop_count < max_loops also does double duty as the exit for total
        # LLM-provider exhaustion: reflection.py/verification.py's
        # LLMProvidersExhausted handlers both return loop_count=max_loops as
        # their delta (loop_count's reducer is Annotated[int, operator.add],
        # graph/state.py) instead of a normal +1, which guarantees the new
        # total is >= max_loops regardless of the count going in — so a
        # provider outage routes straight to VerificationGate here, same as
        # a genuinely exhausted retry budget, rather than looping forever.
        issues = state.get("verification_issues") or []
        loop_count = state.get("loop_count", 0)
        max_loops = state.get("max_loops", 2)
        if not issues or loop_count >= max_loops:
            return "VerificationGate"
        # Only a hard fault (schema, contradiction, low confidence) is worth another
        # research pass. The judge's issues are soft: the answer ships with a note. Looping
        # on them re-ran Reflection and the judge for the same flag, 2 to 3 LLM calls and a
        # tool fan-out (live traces 2026-10-18).
        hard = any(i.get("severity", "hard") != "soft" for i in issues)
        return "Reflection" if hard else "VerificationGate"

    graph.add_conditional_edges("Verification", _route_after_verification, ["Reflection", "VerificationGate"])

    def _route_after_verification_gate(state: GraphState) -> str:
        return END if state.get("final_report") else "ConclusionBuilder"

    graph.add_conditional_edges(
        "VerificationGate", _route_after_verification_gate, ["ConclusionBuilder", END]
    )
    graph.add_edge("ConclusionBuilder", "CommunicationLayer")

    def _route_after_communication(state: GraphState) -> str:
        return "OutputFiles" if wants_files(state) else END

    graph.add_conditional_edges("CommunicationLayer", _route_after_communication, ["OutputFiles", END])
    graph.add_edge("OutputFiles", END)

    return graph.compile(checkpointer=checkpointer)
