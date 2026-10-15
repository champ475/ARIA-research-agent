"""SEC-09 on a real Postgres: a crafted pickle blob written into the
checkpoint tables is refused on resume and never executed."""

import json
import pickle
import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_side_effects: list[str] = []


def _record_side_effect(tag: str) -> str:
    _side_effects.append(tag)
    return tag


class _Evil:
    def __reduce__(self):
        return (_record_side_effect, ("pickle-executed",))


@pytest.fixture
def saver(pg_db, pg_url, monkeypatch):
    import graph.checkpointer as cp

    monkeypatch.setattr(cp, "DATABASE_URL", pg_url)
    cp.close_checkpointer()
    yield cp.get_postgres_checkpointer()
    cp.close_checkpointer()


def test_tampered_checkpoint_blob_is_rejected_on_resume(saver, pg_db, monkeypatch):
    monkeypatch.setattr("graph.nodes.query_analyzer.MAX_ENTITIES_PER_TURN", 10)  # 6 companies (VAL-03)
    from graph.graph import build_graph
    from tests.graph.test_graph import _StubClient

    graph = build_graph(
        query_analyzer_client=_StubClient(
            json.dumps({"intent": "equity", "objective": "compare", "complexity": "complex"})
        ),
        planner_client=_StubClient(json.dumps({"tools": ["fetch_info"]})),
        reflection_client=_StubClient(json.dumps({"missing": []})),
        verification_client=_StubClient(json.dumps({"issues": []})),
        communication_client=_StubClient("report"),
        checkpointer=saver,
    )
    thread_id = f"sec09-tamper-{uuid.uuid4().hex[:8]}"  # checkpoint tables persist across runs
    config = {"configurable": {"thread_id": thread_id}}
    with patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}):
        paused = graph.invoke(
            {"query": "RELIANCE TCS INFY HDFCBANK WIPRO ITC compare", "max_loops": 2}, config
        )
    assert "__interrupt__" in paused
    assert graph.get_state(config).values["query"]  # clean round trip through Postgres

    # An attacker with write access swaps a stored channel value for a pickle.
    changed = pg_db._query(
        "UPDATE checkpoint_blobs SET type='pickle', blob=%s WHERE thread_id=%s RETURNING channel",
        (pickle.dumps(_Evil()), thread_id),
        fetch="all",
        commit=True,
    )
    assert changed, "expected stored channel blobs to tamper with"
    _side_effects.clear()
    with pytest.raises(NotImplementedError):
        graph.get_state(config)
    assert _side_effects == []
