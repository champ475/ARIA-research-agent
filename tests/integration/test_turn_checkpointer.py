"""ARCH-04 on a real Postgres: a turn that never pauses writes no checkpoint
rows; a turn paused on a yes/no survives a restart and finishes on resume; the
durable copy is removed when it does."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from langgraph.types import Command

from graph.checkpointer import TurnCheckpointer
from graph.graph import build_graph
from tests.graph.test_graph import _StubClient

pytestmark = pytest.mark.integration

PAUSING = "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare"  # 6 companies: asks to confirm first


@pytest.fixture
def pg_saver(pg_db, pg_url, monkeypatch):
    import graph.checkpointer as cp

    monkeypatch.setattr(cp, "DATABASE_URL", pg_url)
    monkeypatch.setattr("graph.nodes.query_analyzer.MAX_ENTITIES_PER_TURN", 10)
    cp.close_checkpointer()
    for t in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        pg_db._query(f"DELETE FROM {t}", fetch=None, commit=True)
    yield cp.get_postgres_checkpointer()
    cp.close_checkpointer()


def _graph(checkpointer, intent="equity", complexity="complex"):
    return build_graph(
        query_analyzer_client=_StubClient(
            json.dumps({"intent": intent, "objective": "compare", "complexity": complexity})
        ),
        planner_client=_StubClient(json.dumps({"tools": ["fetch_info"]})),
        reflection_client=_StubClient(json.dumps({"missing": []})),
        verification_client=_StubClient(json.dumps({"issues": []})),
        communication_client=_StubClient("report"),
        checkpointer=checkpointer,
    )


def _rows(db) -> int:
    return db._query("SELECT count(*) AS n FROM checkpoints", fetch="one")["n"]


def test_a_turn_that_does_not_pause_writes_nothing_to_postgres(pg_db, pg_saver):
    tc = TurnCheckpointer(durable=lambda: pg_saver)
    graph = _graph(tc, intent="chat", complexity="simple")
    thread = f"arch04-plain-{uuid.uuid4().hex[:8]}"
    out = graph.invoke({"query": "hello there", "max_loops": 2}, {"configurable": {"thread_id": thread}})
    assert "__interrupt__" not in out
    assert _rows(pg_db) == 0
    assert thread in tc.storage  # the checkpoints were in memory...
    tc.release(thread)
    assert thread not in tc.storage  # ...and are freed at the end of the turn


def test_a_paused_turn_resumes_after_a_restart_and_cleans_up(pg_db, pg_saver):
    thread = f"arch04-pause-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread}}

    first = TurnCheckpointer(durable=lambda: pg_saver)
    with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
        paused = _graph(first).invoke({"query": PAUSING, "max_loops": 2}, config)
        assert "__interrupt__" in paused
        assert first.persist(thread) is True
        first.release(thread)
        assert _rows(pg_db) >= 1

        # "restart": a new process has a fresh saver and a freshly compiled graph
        second = TurnCheckpointer(durable=lambda: pg_saver)
        second.mark_resumable(thread)
        done = _graph(second).invoke(Command(resume=True), config)

    assert "__interrupt__" not in done
    assert done["conclusions"], "research ran to completion after the resume"
    assert "Verified conclusions" in done["pending_synthesis_prompt"]
    second.finish(thread)
    second.release(thread)
    assert _rows(pg_db) == 0
    assert db_writes_gone(pg_db, thread)


def db_writes_gone(db, thread) -> bool:
    return not db._query("SELECT 1 FROM checkpoint_writes WHERE thread_id=%s", (thread,), fetch="all")


def test_a_thread_never_marked_resumable_never_reads_postgres(pg_saver):
    calls = []

    def durable():
        calls.append(1)
        return pg_saver

    tc = TurnCheckpointer(durable=durable)
    assert tc.get_tuple({"configurable": {"thread_id": "unknown", "checkpoint_ns": ""}}) is None
    assert calls == []


def test_failed_persist_keeps_the_thread_in_memory(pg_saver):
    class Broken:
        def delete_thread(self, *_):
            raise RuntimeError("database down")

    tc = TurnCheckpointer(durable=lambda: Broken())
    thread = f"arch04-keep-{uuid.uuid4().hex[:8]}"
    with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
        _graph(tc).invoke({"query": PAUSING, "max_loops": 2}, {"configurable": {"thread_id": thread}})
    assert tc.persist(thread) is False
    tc.release(thread)  # must not drop the only copy
    assert thread in tc.storage
    tc.finish(thread)  # ...until the turn is over
    assert thread not in tc.storage
