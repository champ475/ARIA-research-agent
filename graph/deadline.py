"""
graph/deadline.py — the per-turn deadline (REL-02).

GraphOrchestrator stamps every turn (and every resume after a yes/no gate)
with `deadline_ts` and `research_cutoff_ts` (RESEARCH_DEADLINE_FRACTION of the
way to it). Past the cutoff:
- ResearchExecutor skips tasks it hasn't started (except writes and what they
  depend on — half-doing "set an alert" is worse than being slow);
- Reflection queues no more research and Verification starts no new loop;
- the answer is built from the evidence that did arrive, and
  `data_gaps_note` tells the user exactly what is missing and why.
execute_task also caps each tool call at the time that's left.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from config import RESEARCH_DEADLINE_FRACTION, TURN_DEADLINE_SECONDS


def new_deadline(now: float | None = None, seconds: float | None = None) -> dict[str, float]:
    now = time.time() if now is None else now
    budget = TURN_DEADLINE_SECONDS if seconds is None else seconds
    return {"deadline_ts": now + budget, "research_cutoff_ts": now + budget * RESEARCH_DEADLINE_FRACTION}


def _get(source: Mapping[str, Any], key: str) -> float | None:
    # GraphState uses the plain key; a Send task's context carries it with a
    # leading underscore, like the other injected values (_user_id, ...).
    value = source.get(key, source.get(f"_{key}"))
    return float(value) if value is not None else None


def past_research_cutoff(source: Mapping[str, Any], now: float | None = None) -> bool:
    cutoff = _get(source, "research_cutoff_ts")
    return cutoff is not None and (time.time() if now is None else now) >= cutoff


def remaining_seconds(source: Mapping[str, Any], now: float | None = None) -> float | None:
    deadline = _get(source, "deadline_ts")
    if deadline is None:
        return None
    return deadline - (time.time() if now is None else now)


def protected_task_ids(tasks: list[dict]) -> set[str]:
    """Write tasks and everything they (transitively) depend on — never
    skipped for time, so a requested alert/save either happens or fails
    for a real reason."""
    from graph.tool_catalogue import get_tool

    by_id = {t["id"]: t for t in tasks}
    stack = [t["id"] for t in tasks if _is_write(get_tool, t["tool"])]
    protected: set[str] = set()
    while stack:
        tid = stack.pop()
        if tid in protected or tid not in by_id:
            continue
        protected.add(tid)
        stack.extend(by_id[tid].get("depends_on", []))
    return protected


def _is_write(get_tool: Any, tool: str) -> bool:
    try:
        return bool(get_tool(tool).side_effect)
    except KeyError:
        return False


_REASON_TEXT = {
    "timeout": "timed out",
    "circuit_open": "source temporarily unavailable",
    "deadline": "skipped to answer in time",
    "error": "couldn't be fetched",
}


def _label(task: dict) -> str:
    name = task["tool"]
    for prefix in ("fetch_", "compute_", "get_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    name = name.replace("_", " ")
    entity = task.get("entity") or ""
    return f"{name} for {entity}" if entity and entity.lower() != "market" else name


def data_gaps_note(tasks: list[dict] | None) -> str:
    """A deterministic line naming research that didn't make it into the
    answer (failed or skipped tasks), or "" when nothing is missing."""
    gaps = [t for t in (tasks or []) if t.get("status") in ("failed", "skipped")]
    if not gaps:
        return ""
    parts = []
    for t in gaps:
        reason = t.get("error") or ("deadline" if t["status"] == "skipped" else "error")
        parts.append(f"{_label(t)} ({_REASON_TEXT.get(reason, _REASON_TEXT['error'])})")
    return "\n\n⚠️ *Some data is missing from this answer:* " + "; ".join(parts) + "."
