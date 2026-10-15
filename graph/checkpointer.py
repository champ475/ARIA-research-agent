"""
Postgres-backed checkpointer (RFC_AGENTIC_RESEARCH_GRAPH.md Sec13). Persists
GraphState per thread_id (= chat session id) so the graph survives a crash
mid-run and so human-in-the-loop interrupts (graph/nodes/human_review.py)
have somewhere to pause to.

REL-04: the app has one Postgres driver (psycopg 3) everywhere. The saver
gets its own small pool from the same driver and the same connection settings
as utils/db.py (statement timeout, no prepared statements, dict rows), because
PostgresSaver needs autocommit connections while the app pool runs explicit
transactions; DB_POOL_MAX + CHECKPOINT_POOL_MAX is the process's connection
budget against the database.

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
from typing import Any

from config import DATABASE_URL, DB_POOL_ACQUIRE_TIMEOUT

CHECKPOINT_POOL_MAX = 5

_checkpointer: Any = None
_pool = None
_lock = threading.Lock()


def get_postgres_checkpointer():
    """Lazily creates a singleton PostgresSaver backed by a small psycopg
    connection pool. Safe to call repeatedly — returns the same instance.
    The checkpoint tables are created by the release step
    (scripts/migrate.py -> utils/migrations.setup_checkpointer_tables), not
    here: app processes issue no DDL (ARCH-01)."""
    global _checkpointer, _pool
    if _checkpointer is not None:
        return _checkpointer

    with _lock:
        if _checkpointer is not None:
            return _checkpointer

        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from psycopg_pool import ConnectionPool

        from utils.db import connection_kwargs

        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=CHECKPOINT_POOL_MAX,
            timeout=DB_POOL_ACQUIRE_TIMEOUT,
            kwargs={**connection_kwargs(), "autocommit": True},
            check=ConnectionPool.check_connection,
            name="aria-checkpoints",
            open=True,
        )
        checkpointer = PostgresSaver(_pool, serde=JsonPlusSerializer(pickle_fallback=True))
        _checkpointer = checkpointer
        return _checkpointer


def close_checkpointer() -> None:
    """Close the saver's pool (graceful shutdown, tests)."""
    global _checkpointer, _pool
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _checkpointer = None
