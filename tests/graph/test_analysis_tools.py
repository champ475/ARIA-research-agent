"""UX-04: peer comparison, risk profile, news sentiment and backtest are
reachable research tools — planned when the question asks for them, hidden
from the Planner otherwise, with deterministic claims and caveats."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from graph import analysis_tools as at
from graph.nodes.conclusion_builder import _conclusions_from_evidence
from graph.nodes.planner import build_planner_node
from tools.data import providers as prov
from tools.data.providers.fake import FakeProvider


@pytest.fixture(autouse=True)
def _fake_market():
    prov.set_provider(FakeProvider())
    yield
    prov.set_provider(None)


# ── when the tools are offered ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query, intent, expected",
    [
        ("compare TCS with peers", "equity", ["peer_comparison"]),
        ("how does Infosys stack up against competitors", "equity", ["peer_comparison"]),
        ("risk profile of ADANIPOWER", "equity", ["risk_assessment"]),
        ("is Yes Bank risky?", "equity", ["risk_assessment"]),
        ("news sentiment on ITC", "equity", ["news_sentiment"]),
        ("backtest MA crossover on TATAMOTORS", "equity", ["backtest"]),
        ("back-test an RSI strategy on SBI", "report", ["backtest"]),
        ("my risk tolerance is aggressive", "planning", []),
        ("change my risk tolerance to moderate", "equity", []),
        ("compare TCS and Infosys", "equity", []),
        ("peer funds to Parag Parikh Flexi Cap", "mf", []),
        ("How is TCS doing?", "equity", []),
    ],
)
def test_triggers(query, intent, expected):
    assert at.triggered_tools(query, intent) == expected


def _state(query, complexity="moderate", entities=("TCS",), intent="equity"):
    return {
        "query": query,
        "objective": query,
        "entities": list(entities),
        "complexity": complexity,
        "intent": intent,
        "secondary_intents": [],
        "entity_resolutions": [],
    }


def test_untriggered_tools_stay_out_of_the_planner_prompt():
    client = MagicMock()
    client.ask.return_value = '{"tools": ["fetch_info", "backtest"]}'
    out = build_planner_node(client=client)(_state("How is TCS doing?"))
    prompt = client.ask.call_args[0][0]
    assert not any(name in prompt for name in at.ANALYSIS_TOOLS)
    assert {t["tool"] for t in out["tasks"]} == {"fetch_info"}  # the model can't sneak one in


def test_a_triggered_tool_is_planned_even_if_the_model_skips_it():
    client = MagicMock()
    client.ask.return_value = '{"tools": ["fetch_info"]}'
    out = build_planner_node(client=client)(_state("compare TCS with peers and its valuation"))
    prompt = client.ask.call_args[0][0]
    assert "peer_comparison" in prompt and "backtest" not in prompt
    assert {t["tool"] for t in out["tasks"]} >= {"fetch_info", "peer_comparison"}


def test_simple_question_naming_a_tool_still_runs_it_without_an_llm_call():
    client = MagicMock()
    out = build_planner_node(client=client)(_state("risk profile of ADANIPOWER", "simple", ("ADANIPOWER",)))
    client.ask.assert_not_called()
    tools = {t["tool"] for t in out["tasks"]}
    assert tools == {
        "risk_assessment",
        "compute_fundamentals",
        "compute_technicals",
        "fetch_news",
        "fetch_info",
        "fetch_financials",
        "fetch_ohlcv",
    }


# ── the tools ─────────────────────────────────────────────────────────────────


def _peer_row(sym, **kw):
    base = {
        "symbol": sym,
        "name": sym,
        "pe": 20.0,
        "pb": 4.0,
        "roe": 18.0,
        "roce": 22.0,
        "de": 0.2,
        "mcap_cr": 100000.0,
        "lender": False,
    }
    return {**base, **kw}


def test_peer_comparison_from_the_snapshot():
    found = {
        "self": _peer_row("TCS", pe=25.0),
        "peers": [_peer_row("INFY", pe=22.0), _peer_row("HCLTECH", pe=24.0), _peer_row("WIPRO", pe=18.0)],
        "basis": "industry: Information Technology Services",
        "lender": False,
    }
    with patch("utils.db.fundamentals_peers", return_value=found):
        out = at.peer_comparison("TCS")
    assert out["medians"]["pe"] == 22.0 and out["rows"][0]["is_self"]
    claims = [c["claim"] for c in out["claims"]]
    assert "TCS P/E 25.0 vs peer median 22.0 (3 peers, industry: Information Technology Services)" in claims
    assert any("INFY, HCLTECH, WIPRO" in c for c in claims)


def test_peer_comparison_for_a_lender_drops_roce_and_de():
    found = {
        "self": _peer_row("SBIN", roce=None, de=None, lender=True),
        "peers": [_peer_row("PNB", roce=None, de=None, lender=True), _peer_row("BANKBARODA", lender=True)],
        "basis": "industry: Banks - Regional",
        "lender": True,
    }
    with patch("utils.db.fundamentals_peers", return_value=found):
        out = at.peer_comparison("SBIN")
    assert set(out["medians"]) == {"pe", "pb", "roe"}
    assert not any("ROCE" in c["claim"] or "D/E" in c["claim"] for c in out["claims"])


def test_peer_comparison_falls_back_to_live_quotes_without_a_snapshot():
    with patch("utils.db.fundamentals_peers", side_effect=RuntimeError("no db")):
        out = at.peer_comparison("TCS")
    assert out["source"] == "live (curated list)" and out["rows"][0]["symbol"] == "TCS"


def test_risk_profile_names_what_it_did_not_assess():
    fund = {
        "pe": 30.0,
        "de": 0.1,
        "current_ratio": 2.0,
        "roce": 40.0,
        "roe": 0.4,
        "net_margin": 0.2,
        "price": 3000.0,
        "sector_profile": "general",
    }
    out = at.risk_profile(
        "TCS", fund, {"ATR": 60.0, "current_price": 3000.0}, [{"title": "TCS wins large deal"}]
    )
    assert 0 <= out["score"] <= 100 and out["not_assessed"] == ["Macro (neutral baseline)"]
    assert out["claims"][0]["claim"].startswith("TCS overall risk score")
    assert "🔴" not in str(out) and "🟢" not in str(out)
    lender = at.risk_profile("SBIN", {**fund, "sector_profile": "lender", "de": None}, {}, [])
    assert "Leverage" in lender["not_assessed"]


def test_news_sentiment_scores_headlines():
    news = [
        {"title": "Infosys beats estimates, raises guidance"},
        {"title": "Infosys shares surge on strong deal wins"},
    ]
    out = at.news_sentiment("INFY", news)
    assert out["headlines_scored"] == 2 and out["score"] > 0
    assert out["claims"][0]["claim"].startswith("INFY headline sentiment +")
    assert at.news_sentiment("INFY", [])["claims"] == []


def test_backtest_params_costs_and_caveat():
    from tools.analysis.backtester import ma_crossover_backtest, parse_params

    assert parse_params("backtest 10/30 crossover on TCS over 3 years") == {
        "strategy": "ma_crossover",
        "years": 3,
        "fast": 10,
        "slow": 30,
    }
    assert parse_params("backtest RSI 25/75 on SBI")["rsi_buy"] == 25
    assert parse_params("backtest TCS")["fast"] == 20

    out = at.backtest("TCS", "backtest MA crossover on TCS")
    assert out["strategy"] == "MA Crossover (20/50)" and "0.15% cost per side" in out["assumptions"]
    assert any(c["key"] == "backtest.total_return" for c in out["claims"])
    bars = FakeProvider().ohlcv("TCS")
    free, costly = ma_crossover_backtest(bars, 5, 20), ma_crossover_backtest(bars, 5, 20, cost_pct=0.5)
    assert costly["metrics"]["n_trades"] > 0
    assert costly["metrics"]["total_return_pct"] < free["metrics"]["total_return_pct"]

    note = at.analysis_notes({"evidence": [{"evidence_type": "backtest_result", "payload": out}]})
    assert "Past performance doesn't predict future returns" in note


def test_claims_become_conclusions_and_the_peer_table_is_rendered():
    from graph.evidence_render import render_for_prompt

    found = {
        "self": _peer_row("TCS"),
        "peers": [_peer_row("INFY"), _peer_row("WIPRO")],
        "basis": "industry: IT Services",
        "lender": False,
    }
    with patch("utils.db.fundamentals_peers", return_value=found):
        payload = at.peer_comparison("TCS")
    ev = {
        "id": "e1",
        "tool": "peer_comparison",
        "entity": "TCS",
        "evidence_type": "peer_comparison",
        "payload": payload,
        "confidence": 1.0,
    }
    conclusions = _conclusions_from_evidence(ev)
    assert len(conclusions) == len(payload["claims"]) and conclusions[0]["citation_key"].startswith(
        "TCS.peers."
    )
    rendered = render_for_prompt(ev, 2000)
    assert "| symbol | name | pe |" in rendered and "INFY" in rendered


def test_backtest_runs_through_the_catalogue_with_a_param_cache_key():
    from graph.nodes.execute_task import execute_task
    from graph.tool_catalogue import get_tool

    spec = get_tool("backtest")
    assert spec.cache_key("TCS", {"_query": "backtest rsi on TCS"})["strategy"] == "rsi"
    with (
        patch("graph.nodes.execute_task.evidence_cache.get", return_value=None),
        patch("graph.nodes.execute_task.evidence_cache.put"),
    ):
        ev = execute_task({"id": "t", "tool": "backtest", "entity": "TCS"}, {"_query": "backtest TCS"})
    assert ev["evidence_type"] == "backtest_result" and ev["payload"]["claims"]
