"""
execute_task adapter (RFC_AGENTIC_RESEARCH_GRAPH.md Sec6): the thin bridge
between a Task and the tool catalogue. Checks the evidence cache before
calling the bound tool, then wraps the result as one Evidence entry.

Cache payloads must be JSON-safe for Postgres storage; fetch_ohlcv's
DataFrame result is the one tool output in the current catalogue that needs
converting on the way in/out of the cache — everything else is already
dict/list-shaped.

Evidence.payload placed into GraphState is ALSO kept in this cacheable form
(not the raw DataFrame) — GraphState is checkpointed by LangGraph's
PostgresSaver after every superstep (graph/checkpointer.py), which uses
msgpack and cannot serialize a raw DataFrame (this crashed every OHLCV-
touching query before being caught). Consumers that need the real
DataFrame back (research_executor.py's context-building for
compute_technicals, communication_layer.py's chart lookup) call
`_from_cacheable()` themselves at the point of use — see those modules.
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


_INDEX_COL_FALLBACK = "index"  # pandas' own default name for reset_index() on an unnamed index


def _to_cacheable(payload: Any) -> Any:
    if isinstance(payload, pd.DataFrame):
        orig_index_name = payload.index.name
        index_name = orig_index_name or _INDEX_COL_FALLBACK
        index_is_datetime = bool(pd.api.types.is_datetime64_any_dtype(payload.index))
        records = payload.reset_index().to_dict(orient="records")
        # OHLCV's DatetimeIndex becomes a column of pandas Timestamp objects
        # here — those aren't msgpack-safe either (LangGraph's checkpointer
        # serializer round-trips them via a method-call form its own
        # deserializer then refuses to execute for security reasons), so
        # every record's values get flattened to plain ISO strings, not just
        # the DataFrame shell.
        for record in records:
            for key, value in record.items():
                if isinstance(value, (pd.Timestamp, datetime)):
                    record[key] = value.isoformat()
        return {
            "__dataframe__": True, "records": records,
            "index_name": index_name, "index_had_no_name": orig_index_name is None,
            "index_is_datetime": index_is_datetime,
        }
    return payload


def from_cacheable(payload: Any) -> Any:
    """Public (no leading underscore) since research_executor.py and
    communication_layer.py both need to reconstruct a DataFrame from
    GraphState's checkpoint-safe Evidence.payload — see module docstring."""
    if isinstance(payload, dict) and payload.get("__dataframe__"):
        df = pd.DataFrame(payload["records"])
        index_name = payload.get("index_name") or _INDEX_COL_FALLBACK
        if index_name in df.columns:
            if payload.get("index_is_datetime"):
                df[index_name] = pd.to_datetime(df[index_name])
            df = df.set_index(index_name)
            if payload.get("index_had_no_name"):
                df.index.name = None  # restore the original unnamed-index shape exactly
        return df
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
    # DATA-01: write tools and user-scoped reads bypass the shared cache
    # entirely; cacheable tools key on every input that changes their output.
    key_parts = spec.cache_key(task["entity"], context) if (spec.cacheable and spec.cache_key) else {}
    cached = (
        evidence_cache.get(task["tool"], task["entity"], spec.evidence_type, **key_parts)
        if spec.cacheable else None
    )
    if cached is not None:
        cacheable_payload = cached["payload"]
        confidence = cached["confidence"]
    else:
        raw_payload = spec.fn(task["entity"], context)
        confidence = 1.0
        cacheable_payload = _to_cacheable(raw_payload)
        if spec.cacheable:
            evidence_cache.put(
                task["tool"], task["entity"], spec.evidence_type, cacheable_payload, confidence, **key_parts
            )

    evidence: Evidence = {
        "id": _evidence_id(task["tool"], task["entity"], spec.evidence_type),
        "source": task["tool"],
        "tool": task["tool"],
        "entity": task["entity"],
        "evidence_type": spec.evidence_type,
        # Kept in checkpoint-safe form here (not the raw DataFrame) — see
        # module docstring. Callers needing the real object use from_cacheable().
        "payload": cacheable_payload,
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
