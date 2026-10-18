"""WP4 (smoke test 2026-10-17): "Is the market good today?" had no index levels to answer from."""

import json

from graph.data_questions import MARKET_LEVEL
from graph.guardrails import answer_kind
from graph.nodes.planner import build_planner_node
from graph.tool_catalogue import get_tool


class _Stub:
    def __init__(self, response):
        self.response = response

    def ask(self, prompt, **kwargs):
        return self.response


def test_overview_reports_the_three_indices(monkeypatch):
    from tools.data.providers import fake

    monkeypatch.setattr("graph.tool_catalogue.market_data", lambda: fake.FakeProvider())
    out = get_tool("fetch_market_overview").fn("market", {})
    assert set(out["indices"]) == {"Nifty 50", "Sensex", "Nifty Bank"}
    assert any("Nifty 50 is at 24,850.35, +0.42%" in c["claim"] for c in out["claims"])


def test_overview_skips_an_index_without_numbers(monkeypatch):
    class _Provider:
        def macro(self):
            return {"Nifty 50": {"price": "N/A", "change": "N/A"}, "Sensex": {"price": 100.0, "change": -1.0}}

    monkeypatch.setattr("graph.tool_catalogue.market_data", lambda: _Provider())
    out = get_tool("fetch_market_overview").fn("market", {})
    assert list(out["indices"]) == ["Sensex"]
    assert out["claims"][0]["claim"] == "Sensex is at 100.00, -1.00% against the previous close"


def test_market_question_always_plans_the_index_tool():
    node = build_planner_node(_Stub(json.dumps({"tools": ["fetch_top_movers"]})))
    state = {
        "query": "Is the market good today?",
        "objective": "market today",
        "entities": [],
        "complexity": "moderate",
    }
    tools = [t["tool"] for t in node(state)["tasks"]]
    assert "fetch_market_overview" in tools and "fetch_top_movers" in tools


def test_a_company_question_does_not_add_the_index_tool():
    node = build_planner_node(_Stub(json.dumps({"tools": ["fetch_info"]})))
    state = {
        "query": "How is TCS doing in the market?",
        "objective": "tcs",
        "entities": ["TCS"],
        "complexity": "moderate",
    }
    assert "fetch_market_overview" not in [t["tool"] for t in node(state)["tasks"]]


def test_market_level_pattern():
    assert MARKET_LEVEL.search("how is the market") and MARKET_LEVEL.search("Is Nifty up?")
    assert not MARKET_LEVEL.search("what is a P/E ratio")


def test_footer_for_research_answers_whatever_the_intent():
    state = {"intent": "chat", "tasks": [{"tool": "fetch_top_movers"}], "evidence": [{"id": "e"}]}
    assert answer_kind(state, "The market is up.") == "research"
    assert answer_kind({"intent": "alert", "tasks": [{"tool": "alert_create"}]}, "Alert #1 created.") is None
    assert answer_kind({"intent": "chat"}, "A P/E ratio is a price over earnings.") is None
