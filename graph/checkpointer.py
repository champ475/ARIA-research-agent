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

import logging
import threading
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from config import DATABASE_URL, DB_POOL_ACQUIRE_TIMEOUT

log = logging.getLogger(__name__)

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


# ── ARCH-04: checkpoints in memory, durable only while a turn is paused ──────


class TurnCheckpointer(InMemorySaver):
    """LangGraph checkpoints for a turn live in memory; Postgres only holds a
    turn that is paused on a yes/no.

    Every turn used to write several checkpoint rows (about 350 KiB measured by
    scripts/growth_sim.py) for a capability only a small share of turns need:
    resuming after an interrupt. Now the graph runs against this in-memory
    saver, and when a turn pauses `persist()` copies its newest checkpoint and
    pending writes to Postgres, so the answer can arrive on another replica or
    after a restart. `mark_resumable()` tells the saver such a thread exists in
    Postgres (the orchestrator learns it from `chat_sessions.pending_thread_id`);
    only those threads ever read from the database. `finish()` deletes the
    durable copy once the turn has completed, and `release()` frees the memory.
    """

    def __init__(self, durable: Callable[[], Any] | None = None):
        super().__init__(serde=make_serializer())
        self._durable_factory = durable or get_postgres_checkpointer
        self._resumable: set[str] = set()
        self._keep: set[str] = set()  # persist failed: memory is the only copy
        self._tlock = threading.Lock()

    def get_tuple(self, config: Any) -> Any:
        found = super().get_tuple(config)
        if found is not None:
            return found
        thread_id = config["configurable"].get("thread_id")
        with self._tlock:
            durable = thread_id in self._resumable
        if durable:
            return self._durable_factory().get_tuple(config)
        return None

    # Turn lifecycle (called by GraphOrchestrator) ---------------------------

    def mark_resumable(self, thread_id: str) -> None:
        with self._tlock:
            self._resumable.add(thread_id)

    def persist(self, thread_id: str) -> bool:
        """Copy the thread's newest checkpoint and its pending writes to Postgres.
        Returns False (and keeps the thread in memory) when that fails."""
        cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        latest = super().get_tuple(cfg)
        if latest is None:
            return False
        try:
            durable = self._durable_factory()
            durable.delete_thread(thread_id)  # a re-pause replaces the earlier copy
            parent = (latest.parent_config or {}).get("configurable", {})
            put_cfg = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                    **({"checkpoint_id": parent["checkpoint_id"]} if parent.get("checkpoint_id") else {}),
                }
            }
            checkpoint = latest.checkpoint
            saved = durable.put(put_cfg, checkpoint, latest.metadata, dict(checkpoint["channel_versions"]))
            by_task: dict[str, list[tuple[str, Any]]] = {}
            for task_id, channel, value in latest.pending_writes or []:
                by_task.setdefault(task_id, []).append((channel, value))
            for task_id, writes in by_task.items():
                durable.put_writes(saved, writes, task_id)
        except Exception:
            log.warning("could not persist the paused turn %s", thread_id, exc_info=True)
            with self._tlock:
                self._keep.add(thread_id)
            return False
        with self._tlock:
            self._resumable.add(thread_id)
        return True

    def finish(self, thread_id: str) -> None:
        """The turn completed: drop the durable copy, if there ever was one."""
        with self._tlock:
            had = thread_id in self._resumable
            self._resumable.discard(thread_id)
            self._keep.discard(thread_id)
        self.delete_thread(thread_id)
        if had:
            try:
                self._durable_factory().delete_thread(thread_id)
            except Exception:
                log.warning("could not delete the finished checkpoint %s", thread_id, exc_info=True)

    def release(self, thread_id: str) -> None:
        """Free the thread's in-memory checkpoints (the durable copy, if any, stays),
        unless saving it failed and memory holds the only copy."""
        with self._tlock:
            if thread_id in self._keep:
                return
        self.delete_thread(thread_id)

    def memory_threads(self) -> int:
        return len(self.storage)


_turn_checkpointer: TurnCheckpointer | None = None


def get_turn_checkpointer() -> TurnCheckpointer:
    """The process-wide checkpointer every compiled graph shares."""
    global _turn_checkpointer
    with _lock:
        if _turn_checkpointer is None:
            _turn_checkpointer = TurnCheckpointer()
        return _turn_checkpointer
