"""
ResearchExecutor (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6): computes the ready set
(tasks whose depends_on are all `done`) and fans them out in parallel via
LangGraph Send, one Send per ready task, to execute_task_node
(graph/nodes/execute_task.py). Split into two halves per LangGraph's
map-reduce pattern:

- `research_executor` (a real node): marks any task that's `pending` but can
  never become ready (its dependency chain is stuck — usually because a
  dependency failed) as `failed`, so the loop doesn't spin forever. This is
  a genuine state update, so it must happen in a node, not the conditional
  edge function below (conditional edge functions only route, they don't
  merge state).
- `route_after_research_executor` (a conditional-edge function, wired in
  graph/graph.py): re-reads the just-updated state and either returns a
  list[Send] for the next wave, or "EvidenceSummarizer" once nothing is
  ready or pending.

graph/graph.py wires execute_task_node's outgoing edge back to
"ResearchExecutor", so waves keep resolving until every task is done/failed.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Send

from graph.deadline import past_research_cutoff, protected_task_ids
from graph.journal import log_decision
from graph.state import GraphState, Task


def _ready_tasks(tasks: list[Task]) -> list[Task]:
    done_ids = {t["id"] for t in tasks if t["status"] == "done"}
    return [
        task
        for task in tasks
        if task["status"] == "pending" and all(dep in done_ids for dep in task["depends_on"])
    ]


def _stuck_tasks(tasks: list[Task]) -> list[Task]:
    """Pending tasks that aren't ready and never will be, because at least
    one dependency has already failed."""
    failed_ids = {t["id"] for t in tasks if t["status"] == "failed"}
    ready_ids = {t["id"] for t in _ready_tasks(tasks)}
    return [
        task
        for task in tasks
        if task["status"] == "pending"
        and task["id"] not in ready_ids
        and any(dep in failed_ids for dep in task["depends_on"])
    ]


def _build_context(
    task: Task,
    tasks_by_id: dict[str, Task],
    evidence: list[dict],
    user_id: int | None = None,
    query: str | None = None,
    llm_config: dict | None = None,
    intent: str | None = None,
    secondary_intents: list[str] | None = None,
    deadline: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Maps each dependency tool's name -> its evidence payload, so
    execute_task's tool fn can read an already-fetched dependency result.
    Also injects `_user_id`, `_query`, and `_llm_config` unconditionally
    (not tied to depends_on) since some tools need GraphState-level data no
    dependency's raw result carries: `_user_id` for the alert CRUD tools
    (Phase 7.2), `_query` for screener's filter-parsing tools (Phase 7.3 —
    parse_query() needs the user's raw message text, which a per-entity Task
    has no other way to see), and `_llm_config` for alert_create's own LLM
    call (condition extraction) so it uses the same BYO/trial provider as
    the rest of this turn instead of a hardcoded default."""
    latest_by_tool_entity: dict[tuple[str, str], Any] = {}
    for ev in evidence:
        latest_by_tool_entity[(ev["tool"], ev["entity"])] = ev["payload"]

    context: dict[str, Any] = {
        "_user_id": user_id,
        "_query": query,
        "_llm_config": llm_config,
        # SEC-03: execute_task re-checks write permission against these.
        "_intent": intent,
        "_secondary_intents": list(secondary_intents or []),
        # REL-02: execute_task caps each tool call at the time left.
        "_deadline_ts": (deadline or {}).get("deadline_ts"),
        "_research_cutoff_ts": (deadline or {}).get("research_cutoff_ts"),
    }
    for dep_id in task["depends_on"]:
        dep_task = tasks_by_id.get(dep_id)
        if dep_task is None:
            continue
        payload = latest_by_tool_entity.get((dep_task["tool"], dep_task["entity"]))
        if payload is not None:
            # Kept in checkpoint-safe form: this context travels inside a Send,
            # which the Postgres checkpointer stores (a DataFrame here failed
            # every turn with a dependency — found by TEST-04). execute_task
            # restores the real DataFrames just before calling the tool.
            context[dep_task["tool"]] = payload
    return context


def research_executor(state: GraphState) -> dict:
    tasks = state.get("tasks", [])
    if past_research_cutoff(state):
        protected = protected_task_ids(tasks)
        late = [t for t in tasks if t["status"] == "pending" and t["id"] not in protected]
        if late:
            return {
                "tasks": [{**t, "status": "skipped", "error": "deadline"} for t in late],
                **log_decision(
                    "ResearchExecutor",
                    f"skipped {len(late)} task(s)",
                    "turn deadline reached — answering with the evidence gathered so far",
                ),
            }
    stuck = _stuck_tasks(tasks)
    if stuck:
        failed_updates = [{**t, "status": "failed"} for t in stuck]
        return {
            "tasks": failed_updates,
            **log_decision(
                "ResearchExecutor",
                f"marked {len(stuck)} task(s) failed",
                "blocked on a dependency that already failed",
            ),
        }

    ready = _ready_tasks(tasks)
    if ready:
        return log_decision(
            "ResearchExecutor",
            f"dispatching {len(ready)} ready task(s)",
            "fan-out via Send, parallel execution of independent tasks",
        )
    return log_decision("ResearchExecutor", "no ready tasks", "all tasks done or failed")


def route_after_research_executor(state: GraphState):
    tasks = state.get("tasks", [])
    evidence = state.get("evidence", [])
    ready = _ready_tasks(tasks)
    if not ready:
        return "EvidenceSummarizer"

    tasks_by_id = {t["id"]: t for t in tasks}
    user_id = state.get("user_id")
    query = state.get("query")
    llm_config = state.get("llm_config")
    return [
        Send(
            "execute_task_node",
            {
                "task": task,
                "context": _build_context(
                    task,
                    tasks_by_id,
                    evidence,
                    user_id,
                    query,
                    llm_config,
                    state.get("intent"),
                    state.get("secondary_intents"),
                    {
                        "deadline_ts": state.get("deadline_ts"),
                        "research_cutoff_ts": state.get("research_cutoff_ts"),
                    },
                ),
            },
        )
        for task in ready
    ]
