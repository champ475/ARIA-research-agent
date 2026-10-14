"""
Human-in-the-loop checkpoints (RFC_AGENTIC_RESEARCH_GRAPH.md Sec12). Two of
the three interrupt points are wired here:

- ResearchConfirmation: before ResearchExecutor's first wave, when the plan
  is complex and touches more than a handful of tasks (expensive multi-
  ticker fetches/PDF scrapes) — ask before spending the calls.
- VerificationGate: before ConclusionBuilder, only reached when
  Verification's loop budget is exhausted with issues still unresolved —
  "I couldn't verify X, show anyway?" rather than deciding silently.

The third checkpoint (capital-allocation confirmation for planning-intent
turns) is deferred to Phase 7, which is when planning tools first land on
the graph — there's nothing to gate yet.

Both nodes use the same pattern: if declined, set final_report directly and
let the corresponding routing function in graph/graph.py send control
straight to END, skipping the rest of the pipeline rather than letting a
later node (ConclusionBuilder/CommunicationLayer) silently overwrite the
decline with its own output.
"""

from __future__ import annotations

from langgraph.types import interrupt

from graph.journal import log_decision
from graph.state import GraphState

_COMPLEX_TASK_THRESHOLD = 5


def research_confirmation(state: GraphState) -> dict:
    tasks = state.get("tasks", [])
    if state.get("complexity") != "complex" or len(tasks) <= _COMPLEX_TASK_THRESHOLD:
        return log_decision(
            "ResearchConfirmation",
            "proceeding without confirmation",
            f"{len(tasks)} task(s), complexity={state.get('complexity')} — below threshold",
        )

    approved = interrupt(
        {
            "type": "research_confirmation",
            "message": f"This plan involves {len(tasks)} research tasks. Proceed?",
            "task_count": len(tasks),
        }
    )

    if not approved:
        skipped = [{**t, "status": "skipped"} for t in tasks if t["status"] == "pending"]
        return {
            "tasks": skipped,
            "final_report": "You declined to proceed with this research plan, so no report was generated.",
            "chart_html": None,
            "confidence_summary": {},
            **log_decision(
                "ResearchConfirmation", "user declined, tasks skipped", f"{len(tasks)} task(s) not run"
            ),
        }

    return log_decision("ResearchConfirmation", "user approved", f"{len(tasks)} task(s), complexity=complex")


def verification_gate(state: GraphState) -> dict:
    issues = state.get("verification_issues") or []
    if not issues:
        return log_decision("VerificationGate", "no unresolved issues, pass through", "clean")

    approved = interrupt(
        {
            "type": "verification_gate",
            "message": f"{len(issues)} issue(s) could not be fully verified. Show the report anyway?",
            "issues": issues,
        }
    )

    if not approved:
        return {
            "conclusions": [],
            "final_report": (
                "Research could not be verified to a satisfactory confidence "
                "level and was withheld at your request."
            ),
            "chart_html": None,
            "confidence_summary": {},
            **log_decision(
                "VerificationGate", "user declined, report withheld", f"{len(issues)} unresolved issue(s)"
            ),
        }

    return log_decision(
        "VerificationGate", "user approved showing unresolved report", f"{len(issues)} unresolved issue(s)"
    )
