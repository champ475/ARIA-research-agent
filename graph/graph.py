"""
Graph assembly (RFC_AGENTIC_RESEARCH_GRAPH.md Sec4). Wired so far:
QueryAnalyzer -> Planner -> [ResearchExecutor <-> execute_task_node loop] ->
EvidenceSummarizer -> Reflection -> Verification -> ConclusionBuilder (stub)
-> CommunicationLayer (stub). The real ConclusionBuilder/CommunicationLayer
land in Phase 5. Single-ticker equity queries only for now (tool catalogue
scope, graph/tool_catalogue.py).

Reflection's loop-back skips Planner and routes straight to
ResearchExecutor: Reflection already builds and upserts new Tasks itself
(reusing Planner's tool-selection/expansion helpers, graph/nodes/reflection.py),
so re-entering Planner would just be a second, redundant LLM call over the
same objective. loop_count is a shared budget incremented by whichever side
(Reflection when it queues new tasks, Verification when it finds issues)
is asking for another pass — see graph/nodes/verification.py's docstring for
why Verification also has to increment it.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.llm_client import LLMClient
from graph.journal import log_decision
from graph.nodes.evidence_summarizer import evidence_summarizer
from graph.nodes.execute_task import execute_task_node
from graph.nodes.planner import build_planner_node
from graph.nodes.query_analyzer import build_query_analyzer_node
from graph.nodes.reflection import build_reflection_node
from graph.nodes.research_executor import research_executor, route_after_research_executor
from graph.nodes.verification import build_verification_node
from graph.state import GraphState


def _conclusion_builder_stub(state: GraphState) -> dict:
    """Placeholder until Phase 5's real ConclusionBuilder (deterministic,
    evidence -> Conclusion)."""
    return {
        "conclusions": [],
        **log_decision(
            "ConclusionBuilder",
            "stub passthrough",
            "real implementation lands in Phase 5",
        ),
    }


def _communication_layer_stub(state: GraphState) -> dict:
    """Placeholder until Phase 5's real CommunicationLayer. Reports the
    evidence actually collected so ResearchExecutor's output is verifiable
    end to end before Reflection/Verification/ConclusionBuilder exist."""
    objective = state.get("objective") or "(no objective classified)"
    task_count = len(state.get("tasks") or [])
    done = sum(1 for t in (state.get("tasks") or []) if t["status"] == "done")
    evidence_count = len(state.get("evidence") or [])
    issue_count = len(state.get("verification_issues") or [])
    text = (
        f"(stub report) Objective: {objective}\n\n"
        f"Planner generated {task_count} task(s), {done} completed. "
        f"ResearchExecutor collected {evidence_count} evidence item(s). "
        f"Verification found {issue_count} unresolved issue(s). "
        f"ConclusionBuilder is not implemented yet (Phase 5) — this is a raw "
        f"summary, not a verified report."
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
    reflection_client: LLMClient | None = None,
    verification_client: LLMClient | None = None,
) -> CompiledStateGraph:
    graph = StateGraph(GraphState)

    graph.add_node("QueryAnalyzer", build_query_analyzer_node(query_analyzer_client))
    graph.add_node("Planner", build_planner_node(planner_client))
    graph.add_node("ResearchExecutor", research_executor)
    graph.add_node("execute_task_node", execute_task_node)
    graph.add_node("EvidenceSummarizer", evidence_summarizer)
    graph.add_node("Reflection", build_reflection_node(reflection_client))
    graph.add_node("Verification", build_verification_node(verification_client))
    graph.add_node("ConclusionBuilder", _conclusion_builder_stub)
    graph.add_node("CommunicationLayer", _communication_layer_stub)

    graph.add_edge(START, "QueryAnalyzer")
    graph.add_edge("QueryAnalyzer", "Planner")

    def _route_after_planner(state: GraphState) -> str:
        return "ResearchExecutor" if state.get("tasks") else "ConclusionBuilder"

    graph.add_conditional_edges(
        "Planner", _route_after_planner, ["ResearchExecutor", "ConclusionBuilder"]
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
        return "Reflection" if issues and loop_count < max_loops else "ConclusionBuilder"

    graph.add_conditional_edges(
        "Verification", _route_after_verification, ["Reflection", "ConclusionBuilder"]
    )
    graph.add_edge("ConclusionBuilder", "CommunicationLayer")
    graph.add_edge("CommunicationLayer", END)

    return graph.compile()
