"""
Graph assembly (RFC_AGENTIC_RESEARCH_GRAPH.md Sec4). This module currently
wires only the Phase 2 slice: QueryAnalyzer -> Planner -> ConclusionBuilder
(stub) -> CommunicationLayer (stub), single-ticker equity queries only.
ResearchExecutor/EvidenceSummarizer/Reflection/Verification and the real
ConclusionBuilder/CommunicationLayer land in later phases per the migration
plan — this module's edges grow as each phase's nodes are implemented.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.llm_client import LLMClient
from graph.journal import log_decision
from graph.nodes.planner import build_planner_node
from graph.nodes.query_analyzer import build_query_analyzer_node
from graph.state import GraphState


def _conclusion_builder_stub(state: GraphState) -> dict:
    """Placeholder until Phase 5's real ConclusionBuilder (deterministic,
    evidence -> Conclusion) exists. Always empty conclusions for now since
    Phase 2 has no ResearchExecutor to produce evidence yet."""
    return {
        "conclusions": [],
        **log_decision(
            "ConclusionBuilder",
            "stub passthrough",
            "real implementation lands in Phase 5, ResearchExecutor doesn't exist yet",
        ),
    }


def _communication_layer_stub(state: GraphState) -> dict:
    """Placeholder until Phase 5's real CommunicationLayer. Reports what the
    graph decided without executing any research, so Phase 2 is verifiable
    end to end (QueryAnalyzer -> Planner -> here) before ResearchExecutor
    exists."""
    objective = state.get("objective") or "(no objective classified)"
    task_count = len(state.get("tasks") or [])
    text = (
        f"(stub report) Objective: {objective}\n\n"
        f"Planner generated {task_count} task(s). ResearchExecutor is not "
        f"implemented yet (Phase 3) — no evidence was collected."
    )
    return {
        "final_report": text,
        "chart_html": "",
        **log_decision(
            "CommunicationLayer",
            "stub passthrough",
            "real implementation lands in Phase 5",
        ),
    }


def build_graph(
    query_analyzer_client: LLMClient | None = None,
    planner_client: LLMClient | None = None,
) -> CompiledStateGraph:
    graph = StateGraph(GraphState)

    graph.add_node("QueryAnalyzer", build_query_analyzer_node(query_analyzer_client))
    graph.add_node("Planner", build_planner_node(planner_client))
    graph.add_node("ConclusionBuilder", _conclusion_builder_stub)
    graph.add_node("CommunicationLayer", _communication_layer_stub)

    graph.add_edge(START, "QueryAnalyzer")
    graph.add_edge("QueryAnalyzer", "Planner")
    # Phase 2 only: ResearchExecutor doesn't exist yet, so every plan (even a
    # non-trivial one) currently falls straight through to the stub — Phase 3
    # inserts ResearchExecutor between Planner and ConclusionBuilder for the
    # non-empty-tasks branch.
    graph.add_edge("Planner", "ConclusionBuilder")
    graph.add_edge("ConclusionBuilder", "CommunicationLayer")
    graph.add_edge("CommunicationLayer", END)

    return graph.compile()
