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

from graph.journal import log_decision
from graph.nodes.execute_task import from_cacheable
from graph.state import GraphState, Task


def _ready_tasks(tasks: list[Task]) -> list[Task]:
    done_ids = {t["id"] for t in tasks if t["status"] == "done"}
    return [
        task for task in tasks
        if task["status"] == "pending" and all(dep in done_ids for dep in task["depends_on"])
    ]


def _stuck_tasks(tasks: list[Task]) -> list[Task]:
    """Pending tasks that aren't ready and never will be, because at least
    one dependency has already failed."""
    failed_ids = {t["id"] for t in tasks if t["status"] == "failed"}
    ready_ids = {t["id"] for t in _ready_tasks(tasks)}
    return [
        task for task in tasks
        if task["status"] == "pending"
        and task["id"] not in ready_ids
        and any(dep in failed_ids for dep in task["depends_on"])
    ]


def _build_context(
    task: Task, tasks_by_id: dict[str, Task], evidence: list[dict],
    user_id: int | None = None, query: str | None = None,
) -> dict[str, Any]:
    """Maps each dependency tool's name -> its evidence payload, so
    execute_task's tool fn can read an already-fetched dependency result.
    Also injects `_user_id` and `_query` unconditionally (not tied to
    depends_on) since some tools need GraphState-level data no dependency's
    raw result carries: `_user_id` for the alert CRUD tools (Phase 7.2) and
    `_query` for screener's filter-parsing tools (Phase 7.3 — parse_query()
    needs the user's raw message text, which a per-entity Task has no other
    way to see)."""
    latest_by_tool_entity: dict[tuple[str, str], Any] = {}
    for ev in evidence:
        latest_by_tool_entity[(ev["tool"], ev["entity"])] = ev["payload"]

    context: dict[str, Any] = {"_user_id": user_id, "_query": query}
    for dep_id in task["depends_on"]:
        dep_task = tasks_by_id.get(dep_id)
        if dep_task is None:
            continue
        payload = latest_by_tool_entity.get((dep_task["tool"], dep_task["entity"]))
        if payload is not None:
            # Evidence.payload is kept in checkpoint-safe form in GraphState
            # (see graph/nodes/execute_task.py's module docstring) — tool
            # fns like compute_technicals expect the real DataFrame back.
            context[dep_task["tool"]] = from_cacheable(payload)
    return context


def research_executor(state: GraphState) -> dict:
    tasks = state.get("tasks", [])
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
    return [
        Send("execute_task_node", {
            "task": task, "context": _build_context(task, tasks_by_id, evidence, user_id, query),
        })
        for task in ready
    ]
