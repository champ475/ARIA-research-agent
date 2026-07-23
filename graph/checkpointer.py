"""
Postgres-backed checkpointer (RFC_AGENTIC_RESEARCH_GRAPH.md Sec13). Persists
GraphState per thread_id (= chat session id) so the graph survives a crash
mid-run and so human-in-the-loop interrupts (graph/nodes/human_review.py)
have somewhere to pause to.

LangGraph's PostgresSaver requires psycopg (v3), not the psycopg2 the rest
of this codebase uses (utils/db.py) — the RFC's "reuse utils/db.py's pool"
turned out not to be literally possible (incompatible client libraries), so
this opens its own small connection pool against the same DATABASE_URL
instead. Same database, same credentials, separate client library — the
closest achievable match to the RFC's intent.
"""
from __future__ import annotations

import threading

from config import DATABASE_URL

_checkpointer = None
_pool = None
_lock = threading.Lock()


def get_postgres_checkpointer():
    """Lazily creates a singleton PostgresSaver backed by a small psycopg
    connection pool, running setup() (create checkpoint tables if missing)
    exactly once. Safe to call repeatedly — returns the same instance."""
    global _checkpointer, _pool
    if _checkpointer is not None:
        return _checkpointer

    with _lock:
        if _checkpointer is not None:
            return _checkpointer

        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        _pool = ConnectionPool(conninfo=DATABASE_URL, max_size=5, kwargs={"autocommit": True})
        checkpointer = PostgresSaver(_pool)
        checkpointer.setup()
        _checkpointer = checkpointer
        return _checkpointer
