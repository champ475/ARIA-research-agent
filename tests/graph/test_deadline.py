"""REL-02 fault injection: a stalled upstream ends the turn inside its
deadline with a partial answer that names the missing data; past the
research cutoff, unstarted work is skipped but writes never are."""

import json
import threading
import time
from unittest.mock import patch

import pytest

from graph.deadline import data_gaps_note, new_deadline, past_research_cutoff, protected_task_ids
from graph.graph import build_graph
from utils import resilience


class _StubClient:
    def __init__(self, response: str):
        self._response = response

    def ask(self, prompt, **kwargs):
        return self._response


@pytest.fixture(autouse=True)
def _fresh_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


def _graph():
    return build_graph(
        query_analyzer_client=_StubClient(
            json.dumps({"intent": "equity", "objective": "RELIANCE news and price", "complexity": "moderate"})
        ),
        planner_client=_StubClient(json.dumps({"tools": ["fetch_info", "fetch_news"]})),
        reflection_client=_StubClient(json.dumps({"missing": []})),
        verification_client=_StubClient(json.dumps({"issues": []})),
        communication_client=_StubClient("Stub answer."),
    )


def test_stalled_news_source_ends_turn_in_time_with_named_gap(monkeypatch):
    import graph.nodes.execute_task as execute_task_module

    release = threading.Event()

    def stalled_news(entity):
        release.wait(60)  # a 60 s upstream stall
        return []

    monkeypatch.setattr(execute_task_module, "TOOL_TIMEOUT_SECONDS", 0.5)
    with (
        patch("graph.tool_catalogue.get_info", return_value={"currentPrice": 2500}),
        patch("graph.tool_catalogue.company_news", side_effect=stalled_news),
        patch("tools.data.evidence_cache.evidence_cache_get", return_value=None),
        patch("tools.data.evidence_cache.evidence_cache_put", return_value=None),
    ):
        start = time.monotonic()
        result = _graph().invoke({"query": "RELIANCE news and price", **new_deadline(seconds=3)})
        elapsed = time.monotonic() - start
    release.set()

    assert elapsed < 3
    status = {t["tool"]: (t["status"], t.get("error")) for t in result["tasks"]}
    assert status["fetch_info"] == ("done", None)
    assert status["fetch_news"] == ("failed", "timeout")
    assert result["pending_synthesis_prompt"]  # partial answer still produced
    note = data_gaps_note(result["tasks"])
    assert "news for RELIANCE (timed out)" in note


def test_past_cutoff_skips_unstarted_research_but_not_writes():
    from graph.nodes.research_executor import research_executor

    def task(tid, tool, deps=()):
        return {
            "id": tid,
            "tool": tool,
            "entity": "TCS",
            "description": "",
            "depends_on": list(deps),
            "status": "pending",
            "result_evidence_ids": [],
        }

    tasks = [
        task("fetch_news:TCS", "fetch_news"),
        task("alert_list:TCS", "alert_list"),
        task("alert_cancel:TCS", "alert_cancel", deps=["alert_list:TCS"]),
    ]
    assert protected_task_ids(tasks) == {"alert_cancel:TCS", "alert_list:TCS"}
    expired = new_deadline(now=time.time() - 100, seconds=10)
    update = research_executor({"tasks": tasks, **expired})
    assert [(t["id"], t["status"], t["error"]) for t in update["tasks"]] == [
        ("fetch_news:TCS", "skipped", "deadline")
    ]


def test_cutoff_and_note_helpers():
    d = new_deadline(now=1000.0, seconds=100)
    assert d == {"deadline_ts": 1100.0, "research_cutoff_ts": 1070.0}
    assert not past_research_cutoff(d, now=1069.9)
    assert past_research_cutoff({"_research_cutoff_ts": 1070.0}, now=1070.0)  # Send-context spelling
    assert past_research_cutoff({}) is False
    assert data_gaps_note([{"tool": "fetch_info", "entity": "TCS", "status": "done"}]) == ""
    note = data_gaps_note(
        [
            {"tool": "fetch_quarterly_results", "entity": "TCS", "status": "failed", "error": "circuit_open"},
            {"tool": "fetch_top_movers", "entity": "market", "status": "skipped"},
        ]
    )
    assert "quarterly results filing for TCS (source temporarily unavailable)" in note
    assert "top movers (NSE) (skipped to answer in time)" in note


# ── REL-06: one line naming what's missing, never exception text ──────────────


def _task(tool, entity, status="failed", error=None):
    t = {"tool": tool, "entity": entity, "status": status}
    if error:
        t["error"] = error
    return t


def test_one_failed_tool():
    note = data_gaps_note([_task("fetch_info", "TCS", "done"), _task("fetch_news", "TCS", error="timeout")])
    assert note == "\n\n⚠️ *Data not available this time:* news for TCS (timed out)."


def test_two_failures_grouped_by_tool_and_reason():
    note = data_gaps_note(
        [
            _task("fetch_news", "TCS", error="timeout"),
            _task("fetch_news", "INFY", error="timeout"),
            _task("fetch_quarterly_results", "TCS", error="circuit_open"),
        ]
    )
    assert "news for TCS, INFY (timed out)" in note
    assert "quarterly results filing for TCS (source temporarily unavailable)" in note
    assert note.count(";") == 1


def test_all_tools_failing_still_reads_cleanly():
    tasks = [
        _task("fetch_info", "TCS", error="error"),
        _task("fetch_financials", "TCS", error="error"),
        _task("compute_fundamentals", "TCS", "skipped"),
    ]
    note = data_gaps_note(tasks)
    assert note.startswith("\n\n⚠️ *Data not available this time:*")
    for label in ("price and quote", "financial statements", "fundamental ratios"):
        assert label in note
    assert "Traceback" not in note and "Error" not in note
