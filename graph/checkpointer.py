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

Serializer: PostgresSaver's default JsonPlusSerializer msgpack-encodes
GraphState after every superstep and raises TypeError on anything it
doesn't natively know how to represent — confirmed to break on a raw
pandas DataFrame (application data; graph/nodes/execute_task.py works
around this at the source by keeping Evidence.payload in a JSON-safe form)
AND on LangGraph's own internal `Send` object (framework-internal pending-
write state during a multi-task fan-out — not something application code
can pre-convert, since it's never exposed to a node function). Constructing
the serde with `pickle_fallback=True` covers both today and any future type
neither side anticipated, instead of hand-listing every non-native type as
it's discovered.
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

        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(conninfo=DATABASE_URL, max_size=5, kwargs={"autocommit": True})
        checkpointer = PostgresSaver(_pool, serde=JsonPlusSerializer(pickle_fallback=True))
        checkpointer.setup()
        _checkpointer = checkpointer
        return _checkpointer
