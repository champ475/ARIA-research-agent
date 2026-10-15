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

Serializer (SEC-09): no pickle, and a strict msgpack allowlist. Anyone able
to write the checkpoint tables (a leaked DATABASE_URL, an injection elsewhere)
must not get code execution when a thread is resumed. `make_serializer()`
turns off `pickle_fallback` (a "pickle"-typed blob is refused outright) and
restricts msgpack extension types to LangGraph's SAFE_MSGPACK_TYPES (datetime,
UUID, Decimal, Send, Interrupt, Command, ...); any other class named in a
blob is NOT imported or constructed — it decodes as plain data. GraphState is
kept JSON-safe at the source (graph/nodes/execute_task.py::_to_cacheable), so
nothing legitimate needs more than that; a new non-native type now fails
loudly at write time instead of silently falling back to pickle.
"""

from __future__ import annotations

import threading
from typing import Any

from config import DATABASE_URL, DB_POOL_ACQUIRE_TIMEOUT

CHECKPOINT_POOL_MAX = 5

_checkpointer: Any = None
_pool = None
_lock = threading.Lock()


def make_serializer() -> Any:
    """The checkpoint serializer: msgpack only, LangGraph's safe types only,
    never pickle (SEC-09)."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None, allowed_json_modules=None)


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
        checkpointer = PostgresSaver(_pool, serde=make_serializer())
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
