"""DATA-02: a provider interface for market data, per-source health, and a
forced NSE outage that says "NSE data is temporarily unavailable" instead of
answering from an empty table."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.data import nse_scraper
from tools.data import providers as prov
from tools.data.providers.fake import FakeProvider
from utils import resilience
from utils.errors import SourceUnavailable


@pytest.fixture(autouse=True)
def _clean():
    resilience.reset_breakers()
    prov.set_provider(None)
    yield
    resilience.reset_breakers()
    prov.set_provider(None)


def _resp(status: int, body=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    if body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    r.text = text
    return r


# ── source health ─────────────────────────────────────────────────────────────


def test_breaker_states_report_counts_and_times():
    b = resilience.breaker("nse")
    b.record_success()
    b.record_failure()
    state = resilience.breaker_states()["nse"]
    assert state["state"] == "closed"
    assert (state["successes"], state["failures"], state["success_rate"]) == (1, 1, 0.5)
    assert state["last_success_at"] and state["last_failure_at"]


def test_a_403_counts_as_a_source_failure():
    """NSE blocks clients with 401/403; that's an outage, not 'no data'."""
    from utils import http

    with patch("requests.Session.request", return_value=_resp(403, {})):
        http.session("nse").get("https://www.nseindia.com/api/x")
    assert resilience.breaker_states()["nse"]["failures"] == 1


# ── strict NSE fetchers ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "resp",
    [_resp(403, {}), _resp(503, {}), _resp(200, None, "<html>Access Denied</html>")],
)
def test_strict_fetch_raises_source_unavailable(resp):
    session = MagicMock()
    session.get.return_value = resp
    with patch.object(nse_scraper, "_session", return_value=session), pytest.raises(SourceUnavailable) as e:
        nse_scraper.fetch_top_movers("gainers")
    assert e.value.source == "nse"
    assert e.value.user_message == "NSE data is temporarily unavailable."


def test_404_is_no_data_and_legacy_getters_stay_quiet():
    session = MagicMock()
    session.get.return_value = _resp(404, {})
    with patch.object(nse_scraper, "_session", return_value=session):
        assert nse_scraper.fetch_bulk_deals().empty
    session.get.return_value = _resp(403, {})
    with patch.object(nse_scraper, "_session", return_value=session):
        assert nse_scraper.get_top_movers().empty  # old contract: empty, no raise
        assert nse_scraper.get_block_deals("TCS").empty


def test_movers_use_nse_spelling_for_losers():
    session = MagicMock()
    session.get.return_value = _resp(
        200, {"NIFTY": {"data": [{"symbol": "TCS", "ltp": 3000.0, "perChange": -1.2}]}}
    )
    with patch.object(nse_scraper, "_session", return_value=session):
        df = nse_scraper.fetch_top_movers("losers")
    assert "index=loosers" in session.get.call_args[0][0]
    assert list(df["symbol"]) == ["TCS"]


# ── providers ─────────────────────────────────────────────────────────────────


def test_routed_provider_needs_every_method():
    with pytest.raises(ValueError, match="no provider for"):
        prov.RoutedProvider({"quote": FakeProvider()})


def test_default_routes_and_fake_switch():
    default = prov.default_provider()
    assert default.source_for("quote") == "yfinance"
    assert default.source_for("movers") == "nse"
    with patch("config.ARIA_FAKE_DATA", True):
        prov.set_provider(None)
        assert isinstance(prov.market_data(), FakeProvider)


def test_fake_provider_is_deterministic_and_well_shaped():
    fake = FakeProvider()
    assert fake.quote("TCS") == fake.quote("tcs")
    assert fake.quote("TCS")["currentPrice"] != fake.quote("INFY")["currentPrice"]
    ohlcv = fake.ohlcv("TCS")
    assert list(ohlcv.columns) == ["Open", "High", "Low", "Close", "Volume"] and len(ohlcv) == 250
    assert isinstance(ohlcv.index, pd.DatetimeIndex)
    assert not fake.financials("TCS")["income_annual"].empty
    gainers, losers = fake.movers("gainers"), fake.movers("losers")
    assert (gainers["pchange"] > 0).all() and (losers["pchange"] < 0).all()


def test_catalogue_tools_fetch_through_the_active_provider():
    from graph import tool_catalogue as tc

    prov.set_provider(FakeProvider())
    assert (
        tc.get_tool("fetch_info").fn("TCS", {})["currentPrice"] == FakeProvider().quote("TCS")["currentPrice"]
    )
    assert len(tc.get_tool("fetch_top_movers").fn("market", {"_query": "top losers today"})) == 5


# ── a forced outage reaches the answer ────────────────────────────────────────


def test_forced_nse_outage_names_the_source_in_the_answer():
    from graph.deadline import data_gaps_note
    from graph.nodes.execute_task import execute_task_node

    down = MagicMock(name="provider")
    down.movers.side_effect = SourceUnavailable("nse", detail="HTTP 403")
    prov.set_provider(down)
    task = {"id": "t1", "tool": "fetch_top_movers", "entity": "market", "status": "pending", "depends_on": []}
    with patch("graph.nodes.execute_task.evidence_cache.get", return_value=None):
        out = execute_task_node({"task": task, "context": {"_query": "top gainers"}})
    failed = out["tasks"][0]
    assert (failed["status"], failed["error"], failed["source"]) == ("failed", "source_unavailable", "nse")
    note = data_gaps_note(out["tasks"])
    assert "NSE data is temporarily unavailable." in note
    assert "top movers (NSE) (source temporarily unavailable)" in note
    assert "403" not in note


def test_open_breaker_is_named_too():
    from graph.deadline import data_gaps_note
    from graph.nodes.execute_task import execute_task_node

    down = MagicMock(name="provider")
    down.bulk_deals.side_effect = resilience.CircuitOpen("nse")
    prov.set_provider(down)
    task = {"id": "t2", "tool": "fetch_bulk_deals", "entity": "TCS", "status": "pending", "depends_on": []}
    with patch("graph.nodes.execute_task.evidence_cache.get", return_value=None):
        out = execute_task_node({"task": task, "context": {}})
    assert out["tasks"][0]["source"] == "nse"
    assert data_gaps_note(out["tasks"]).startswith("\n\n⚠️ NSE data is temporarily unavailable.")


def test_degraded_source_lowers_confidence():
    from config import DEGRADED_CONFIDENCE
    from graph.nodes.execute_task import execute_task

    stand_in = MagicMock(name="provider")
    stand_in.quote.return_value = {"currentPrice": 10.0, "_degraded_source": "NSE bhavcopy"}
    prov.set_provider(stand_in)
    with (
        patch("graph.nodes.execute_task.evidence_cache.get", return_value=None),
        patch("graph.nodes.execute_task.evidence_cache.put"),
    ):
        ev = execute_task({"id": "t3", "tool": "fetch_info", "entity": "TCS"}, {})
    assert ev["confidence"] == DEGRADED_CONFIDENCE < 1.0


# ── /health/data ──────────────────────────────────────────────────────────────


def test_health_data_route():
    from web.ops_routes import router

    app = FastAPI()
    app.include_router(router)
    resilience.breaker("yfinance").record_success()
    nse = resilience.breaker("nse")
    for _ in range(10):
        nse.record_failure()
    body = TestClient(app).get("/health/data").json()
    assert body["routes"]["movers"] == "nse" and body["routes"]["quote"] == "yfinance"
    assert body["degraded"] == ["nse"]
    assert body["sources"]["yfinance"]["state"] == "closed"
