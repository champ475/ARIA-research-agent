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
from graph.state import GraphState, VerificationIssue

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


def _hard_issues(issues: list[VerificationIssue]) -> list[VerificationIssue]:
    """Issues that withhold evidence and ask the user. A missing severity counts
    as hard; the LLM judge's issues are soft and only add a note to the answer."""
    return [i for i in issues if i.get("severity", "hard") != "soft"]


def _gate_message(hard: list[VerificationIssue]) -> str:
    claims = "; ".join(str(i.get("claim", "")).strip().rstrip(".") for i in hard[:3] if i.get("claim"))
    more = f" (and {len(hard) - 3} more)" if len(hard) > 3 else ""
    if not claims:
        return f"{len(hard)} issue(s) could not be fully verified. Show the report anyway?"
    return f"I couldn't confirm: {claims}{more}. Show the answer anyway?"


def verification_gate(state: GraphState) -> dict:
    issues = state.get("verification_issues") or []
    if not issues:
        return log_decision("VerificationGate", "no unresolved issues, pass through", "clean")
    hard = _hard_issues(issues)
    if not state.get("evidence"):
        # Nothing was collected, so there is no report to show: skip the yes/no
        # and let the answer say what is missing.
        return log_decision(
            "VerificationGate", "no evidence collected, nothing to show", f"{len(issues)} issue(s)"
        )
    if not hard:
        # The judge could not confirm something: the answer carries a note
        # (graph/caveats.py) instead of stopping to ask.
        return log_decision(
            "VerificationGate",
            "soft issues only, answer shown with a note",
            f"{len(issues)} unconfirmed claim(s) from the judge",
        )
    issues = hard

    approved = interrupt(
        {
            "type": "verification_gate",
            "message": _gate_message(issues),
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
