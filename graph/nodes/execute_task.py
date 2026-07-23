"""
execute_task adapter (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6): the thin bridge
between a Task and the tool catalogue. Checks the evidence cache before
calling the bound tool, then wraps the result as one Evidence entry.

Cache payloads must be JSON-safe for Postgres storage; fetch_ohlcv's
DataFrame result is the one tool output in the current catalogue that needs
converting on the way in/out of the cache — everything else is already
dict/list-shaped.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from graph.journal import log_decision
from graph.state import Evidence, Task
from graph.tool_catalogue import get_tool
from tools.data import evidence_cache


def _to_cacheable(payload: Any) -> Any:
    if isinstance(payload, pd.DataFrame):
        return {"__dataframe__": True, "records": payload.reset_index().to_dict(orient="records")}
    return payload


def _from_cacheable(payload: Any) -> Any:
    if isinstance(payload, dict) and payload.get("__dataframe__"):
        return pd.DataFrame(payload["records"])
    return payload


def _evidence_id(tool_name: str, entity: str, evidence_type: str) -> str:
    blob = f"{tool_name}:{entity}:{evidence_type}:{datetime.now(timezone.utc).isoformat()}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def execute_task(task: Task, context: dict[str, Any]) -> Evidence:
    """
    Runs one Task: cache check, fall back to the bound tool on a miss, always
    returns exactly one Evidence entry. A failed tool call propagates the
    exception to the caller (ResearchExecutor's Send fan-out is responsible
    for marking the Task failed, RFC Sec11) rather than being swallowed here.
    """
    spec = get_tool(task["tool"])
    cached = evidence_cache.get(task["tool"], task["entity"], spec.evidence_type)
    if cached is not None:
        payload = _from_cacheable(cached["payload"])
        confidence = cached["confidence"]
    else:
        payload = spec.fn(task["entity"], context)
        confidence = 1.0
        evidence_cache.put(task["tool"], task["entity"], spec.evidence_type, _to_cacheable(payload), confidence)

    evidence: Evidence = {
        "id": _evidence_id(task["tool"], task["entity"], spec.evidence_type),
        "source": task["tool"],
        "tool": task["tool"],
        "entity": task["entity"],
        "evidence_type": spec.evidence_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc),
        "confidence": confidence,
        # Fine-grained per-ratio citation keys (matching fundamental.py's
        # [^pe]/[^roce] convention) are assigned by ConclusionBuilder (Phase 5),
        # which knows which specific field within this evidence a claim uses.
        "citation_key": spec.evidence_type,
    }
    return evidence


def execute_task_node(send_state: dict) -> dict:
    """
    The Send target invoked once per ready task by ResearchExecutor's
    fan-out (graph/nodes/research_executor.py). `send_state` is the small
    per-task payload Send carries — {"task": Task, "context": dict} — not
    the full GraphState. Returns a partial state update: the task's new
    status (done/failed) merges via the `tasks` upsert_by_id reducer, the
    Evidence (on success) merges via the `evidence` append_dedup reducer.
    """
    task: Task = send_state["task"]
    context: dict[str, Any] = send_state.get("context", {})
    try:
        evidence = execute_task(task, context)
    except Exception as e:
        failed_task = {**task, "status": "failed"}
        return {
            "tasks": [failed_task],
            **log_decision("execute_task", f"failed: {task['id']}", str(e)),
        }
    done_task = {**task, "status": "done", "result_evidence_ids": [evidence["id"]]}
    return {
        "tasks": [done_task],
        "evidence": [evidence],
        **log_decision("execute_task", f"completed: {task['id']}", f"tool={task['tool']}, entity={task['entity']}"),
    }
