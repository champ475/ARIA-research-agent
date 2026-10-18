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
import logging
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from config import DEGRADED_CONFIDENCE, TOOL_TIMEOUT_SECONDS
from graph.deadline import remaining_seconds
from graph.journal import log_decision
from graph.state import Evidence, Task
from graph.tool_catalogue import get_tool
from graph.write_policy import write_allowed
from tools.data import evidence_cache
from utils import tracing
from utils.errors import SourceUnavailable
from utils.resilience import CallTimeout, CircuitOpen, call_with_timeout

log = logging.getLogger(__name__)


_INDEX_COL_FALLBACK = "index"  # pandas' own default name for reset_index() on an unnamed index


def _frame_to_cacheable(payload: pd.DataFrame) -> dict:
    orig_index_name = payload.index.name
    index_name = orig_index_name or _INDEX_COL_FALLBACK
    index_is_datetime = bool(pd.api.types.is_datetime64_any_dtype(payload.index))
    records = payload.reset_index().to_dict(orient="records")
    # OHLCV's DatetimeIndex becomes a column of pandas Timestamp objects
    # here — those aren't msgpack-safe either (LangGraph's checkpointer
    # serializer round-trips them via a method-call form its own
    # deserializer then refuses to execute for security reasons), so every
    # value is flattened to an ISO string. Column labels too: statements
    # are keyed by fiscal-year-end Timestamps.
    flat = []
    for record in records:
        flat.append(
            {
                (k.isoformat() if isinstance(k, (pd.Timestamp, datetime)) else str(k)): (
                    v.isoformat() if isinstance(v, (pd.Timestamp, datetime)) else _to_cacheable(v)
                )
                for k, v in record.items()
            }
        )
    return {
        "__dataframe__": True,
        "records": flat,
        "index_name": str(index_name),
        "index_had_no_name": orig_index_name is None,
        "index_is_datetime": index_is_datetime,
    }


def _to_cacheable(payload: Any) -> Any:
    """DataFrames anywhere in the payload become checkpoint-safe dicts —
    including nested ones: fetch_financials returns {statement: DataFrame},
    which the Postgres checkpointer couldn't serialise (found by TEST-04)."""
    if isinstance(payload, pd.DataFrame):
        return _frame_to_cacheable(payload)
    if isinstance(payload, dict) and not payload.get("__dataframe__"):
        return {k: _to_cacheable(v) for k, v in payload.items()}
    if isinstance(payload, list | tuple):
        return [_to_cacheable(v) for v in payload]
    if isinstance(payload, np.generic):  # numpy scalars from pandas/ta aren't msgpack-safe
        return payload.item()
    return payload


def _frame_from_cacheable(payload: dict) -> pd.DataFrame:
    df = pd.DataFrame(payload["records"])
    index_name = payload.get("index_name") or _INDEX_COL_FALLBACK
    if index_name in df.columns:
        if payload.get("index_is_datetime"):
            df[index_name] = pd.to_datetime(df[index_name])
        df = df.set_index(index_name)
        if payload.get("index_had_no_name"):
            df.index.name = None  # restore the original unnamed-index shape exactly
    return df


def from_cacheable(payload: Any) -> Any:
    """Public (no leading underscore) since research_executor.py and
    communication_layer.py both need to reconstruct DataFrames from
    GraphState's checkpoint-safe Evidence.payload — see module docstring.
    Nested frames (a dict of statements) are restored too."""
    if isinstance(payload, dict):
        if payload.get("__dataframe__"):
            return _frame_from_cacheable(payload)
        return {k: from_cacheable(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [from_cacheable(v) for v in payload]
    return payload


# EVAL-01: the answer eval pins the clock and makes evidence ids depend only on
# what was fetched, so the prompts built from them replay byte for byte.
# Never changed in app code.
REPLAYABLE = False
PINNED_NOW: datetime | None = None


def _now() -> datetime:
    return PINNED_NOW or datetime.now(UTC)


def _evidence_id(tool_name: str, entity: str, evidence_type: str, key: str = "") -> str:
    salt = key if REPLAYABLE else datetime.now(UTC).isoformat()
    blob = f"{tool_name}:{entity}:{evidence_type}:{salt}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _blocked_evidence(task: Task, evidence_type: str, reason: str) -> Evidence:
    action = task["tool"].split("_", 1)[1] if task["tool"].startswith("alert_") else "save"
    return {
        "id": _evidence_id(task["tool"], task["entity"], evidence_type),
        "source": task["tool"],
        "tool": task["tool"],
        "entity": task["entity"],
        "evidence_type": evidence_type,
        "payload": {"action": action, "symbol": task["entity"], "result": "blocked", "reason": reason},
        "timestamp": _now(),
        "confidence": 1.0,
        "citation_key": evidence_type,
    }


# Market-wide lists and index levels have no exchange time of their own: while the
# market is shut they are the last session's, not the moment we fetched them.
_LAST_SESSION_EVIDENCE = frozenset({"top_movers", "market_overview", "bulk_deals", "block_deals"})


def _as_of(payload: Any, fetched_at: datetime, evidence_type: str = "") -> datetime:
    """DATA-06: the quote's own exchange time (yfinance `regularMarketTime`,
    epoch seconds) when present and not in the future; for market-wide data the
    last session's close while the market is shut; else the fetch time."""
    if isinstance(payload, dict):
        t = payload.get("regularMarketTime")
        now = _now()
        if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 < t <= now.timestamp():
            return datetime.fromtimestamp(t, UTC)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "as_of" in payload[0]:
        # A screen is dated by the snapshot its rows came from, not by the moment it was run.
        stamps = []
        for row in payload[:500]:
            try:
                stamp = datetime.fromisoformat(str(row.get("as_of")))
            except (TypeError, ValueError):
                continue
            stamps.append(stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC))
        if stamps:
            return min(stamps)
    if evidence_type in _LAST_SESSION_EVIDENCE:
        from utils.market_clock import last_close

        closed_at = last_close()
        if closed_at is not None:
            return closed_at.astimezone(UTC)
    return fetched_at


def _tool_budget(context: dict[str, Any]) -> float:
    """REL-02: one tool call waits at most TOOL_TIMEOUT_SECONDS, and never past
    the turn's deadline."""
    left = remaining_seconds(context)
    return TOOL_TIMEOUT_SECONDS if left is None else max(0.0, min(TOOL_TIMEOUT_SECONDS, left))


def _failure_reason(e: Exception) -> str:
    if isinstance(e, CallTimeout):
        return "timeout"
    if isinstance(e, CircuitOpen):
        return "circuit_open"
    if isinstance(e, SourceUnavailable):
        return "source_unavailable"
    return "error"


def _failure_source(e: Exception) -> str | None:
    """DATA-02: which data source let the task down, when known."""
    if isinstance(e, SourceUnavailable):
        return e.source
    if isinstance(e, CircuitOpen):
        return e.source
    return None


def execute_task(task: Task, context: dict[str, Any]) -> Evidence:
    """
    Runs one Task: cache check, fall back to the bound tool on a miss, always
    returns exactly one Evidence entry. A failed tool call propagates the
    exception to the caller (ResearchExecutor's Send fan-out is responsible
    for marking the Task failed, RFC Sec11) rather than being swallowed here.
    """
    spec = get_tool(task["tool"])
    if spec.side_effect:
        # SEC-03 last line of defence: even if a write task got planned, it
        # only runs when the turn's intent owns it and the user's own message
        # explicitly asked for it.
        ok, reason = write_allowed(
            task["tool"], context.get("_intent"), context.get("_secondary_intents"), context.get("_query")
        )
        if not ok:
            log.warning(f"[execute_task] blocked write tool {task['tool']}: {reason}")
            return _blocked_evidence(task, spec.evidence_type, reason)
    # DATA-01: write tools and user-scoped reads bypass the shared cache
    # entirely; cacheable tools key on every input that changes their output.
    key_parts = spec.cache_key(task["entity"], context) if (spec.cacheable and spec.cache_key) else {}
    cached = (
        evidence_cache.get(task["tool"], task["entity"], spec.evidence_type, **key_parts)
        if spec.cacheable
        else None
    )
    rec = tracing.current()
    if rec is not None:
        rec.add_tool_call(cache_hit=cached is not None)
    if cached is not None:
        cacheable_payload = cached["payload"]
        confidence = cached["confidence"]
        fetched_at = cached.get("fetched_at") or _now()
    else:
        fetched_at = _now()
        # Dependency outputs arrive checkpoint-safe (see research_executor);
        # the tool gets the real objects back.
        context = {k: (v if k.startswith("_") else from_cacheable(v)) for k, v in context.items()}
        if spec.side_effect:
            # Never abandon a write mid-flight: a timed-out caller could
            # report "failed" while the write still lands in the background.
            raw_payload = spec.fn(task["entity"], context)
        else:
            raw_payload = call_with_timeout(
                spec.fn, task["entity"], context, timeout=_tool_budget(context), pool="tool"
            )
        # DATA-02: a fallback source stood in (bhavcopy for a quote) — say so
        # through confidence, which the answer's sourcing reflects.
        degraded = isinstance(raw_payload, dict) and raw_payload.get("_degraded_source")
        confidence = DEGRADED_CONFIDENCE if degraded else 1.0
        cacheable_payload = _to_cacheable(raw_payload)
        if spec.cacheable:
            evidence_cache.put(
                task["tool"], task["entity"], spec.evidence_type, cacheable_payload, confidence, **key_parts
            )

    evidence: Evidence = {
        "id": _evidence_id(task["tool"], task["entity"], spec.evidence_type, repr(sorted(key_parts.items()))),
        "source": task["tool"],
        "tool": task["tool"],
        "entity": task["entity"],
        "evidence_type": spec.evidence_type,
        # Kept in checkpoint-safe form here (not the raw DataFrame) — see
        # module docstring. Callers needing the real object use from_cacheable().
        "payload": cacheable_payload,
        "timestamp": _now(),
        "as_of": _as_of(cacheable_payload, fetched_at, spec.evidence_type),
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
        failed_task = {**task, "status": "failed", "error": _failure_reason(e)}
        source = _failure_source(e)
        if source:
            failed_task["source"] = source
        return {
            "tasks": [failed_task],
            **log_decision("execute_task", f"failed: {task['id']}", _failure_reason(e)),
        }
    done_task = {**task, "status": "done", "result_evidence_ids": [evidence["id"]]}
    return {
        "tasks": [done_task],
        "evidence": [evidence],
        **log_decision(
            "execute_task", f"completed: {task['id']}", f"tool={task['tool']}, entity={task['entity']}"
        ),
    }
