"""VAL-03: more than MAX_ENTITIES_PER_TURN companies -> no research, a
clarifying reply listing what was detected."""

from __future__ import annotations

import json
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from config import MAX_ENTITIES_PER_TURN
from graph.graph import build_graph
from tests.graph.test_graph import _StubClient

EIGHT = "Compare TCS, INFY, WIPRO, HCLTECH, TECHM, LTIM, MPHASIS, COFORGE"


def _run(query: str):
    graph = build_graph(
        query_analyzer_client=_StubClient(
            json.dumps({"intent": "equity", "objective": "compare IT names", "complexity": "complex"})
        ),
        planner_client=_StubClient(json.dumps({"tools": ["fetch_info"]})),
        reflection_client=_StubClient(json.dumps({"missing": []})),
        verification_client=_StubClient(json.dumps({"issues": []})),
        communication_client=_StubClient("report"),
        checkpointer=InMemorySaver(),
    )
    with (
        patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 100}) as info,
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
    ):
        out = graph.invoke({"query": query, "max_loops": 2}, {"configurable": {"thread_id": query[:40]}})
    return out, info


def test_eight_companies_produce_no_tasks_and_a_clarifying_reply():
    assert MAX_ENTITIES_PER_TURN == 5
    out, info = _run(EIGHT)
    assert out.get("tasks", []) == []
    assert info.call_count == 0
    reply = out["final_report"]
    assert "You asked about 8 companies" in reply
    for sym in ("TCS", "INFY", "COFORGE"):
        assert f"**{sym}**" in reply
    assert "up to 5" in reply and "screener" in reply


def test_five_companies_are_still_researched():
    out, info = _run("Compare TCS, INFY, WIPRO, HCLTECH, TECHM")
    assert out.get("final_report", "") == "" or "You asked about" not in out["final_report"]
    assert len({t["entity"] for t in out["tasks"]}) == 5
