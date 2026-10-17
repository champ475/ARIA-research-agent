"""TEST-05: every fetcher parses its recorded upstream response
(tests/fixtures/http/, refreshed by scripts/record_http_fixtures.py), every
recording satisfies its contract (tools/data/contracts.py), and a drifted
response is an outage — never a silently empty answer."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import feedparser
import pandas as pd
import pytest
import responses

from tools.data import contracts
from utils import resilience
from utils.errors import SourceUnavailable

HTTP = Path(__file__).parent / "fixtures" / "http"


def _text(name: str) -> str:
    return (HTTP / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_breakers():
    resilience.reset_breakers()
    yield
    resilience.reset_breakers()


@pytest.fixture
def web():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.get("https://www.nseindia.com", body="<html></html>")  # NSE cookie bootstrap
        yield rsps


# ── NSE ───────────────────────────────────────────────────────────────────────


def test_top_movers(web):
    from tools.data import nse_scraper

    web.get(
        re.compile(r"https://www\.nseindia\.com/api/live-analysis-variations.*"),
        body=_text("nse_gainers.json"),
    )
    df = nse_scraper.fetch_top_movers("gainers")
    assert len(df) > 0 and {"symbol", "ltp", "perChange"} <= set(df.columns)
    contracts.NseMovers.model_validate(json.loads(_text("nse_gainers.json")))


def test_movers_schema_drift_is_an_outage(web):
    from tools.data import nse_scraper

    drifted = json.loads(_text("nse_gainers.json"))
    drifted["NIFTY50"] = drifted.pop("NIFTY")
    web.get(re.compile(r"https://www\.nseindia\.com/api/live-analysis-variations.*"), json=drifted)
    with pytest.raises(SourceUnavailable):
        nse_scraper.fetch_top_movers("gainers")


def test_bulk_and_block_deals_from_the_large_deals_snapshot(web):
    from tools.data import nse_scraper

    web.get(nse_scraper.LARGE_DEALS_URL, body=_text("nse_large_deals.json"))
    snapshot = contracts.NseLargeDeals.model_validate(json.loads(_text("nse_large_deals.json")))
    bulk = nse_scraper.fetch_bulk_deals()
    assert len(bulk) == len(snapshot.bulk) and {
        "symbol",
        "clientName",
        "buySell",
        "quantity",
        "price",
    } <= set(bulk.columns)
    assert pd.api.types.is_numeric_dtype(bulk["quantity"]) and pd.api.types.is_numeric_dtype(bulk["price"])
    one = snapshot.bulk[0].symbol
    assert set(nse_scraper.fetch_bulk_deals(one)["symbol"]) == {one}
    assert len(nse_scraper.fetch_block_deals()) == len(snapshot.block)
    assert "clientName" in nse_scraper.format_bulk_deals(bulk, one)


def test_retired_deal_endpoints_are_not_used(web):
    """NSE's old /api/bulk-deal-data answers 404 (read as "no deals")."""
    from tools.data import nse_scraper

    web.get(
        nse_scraper.LARGE_DEALS_URL,
        json={"as_on_date": "x", "BULK_DEALS_DATA": [{"oops": 1}], "BLOCK_DEALS_DATA": []},
    )
    with pytest.raises(SourceUnavailable):
        nse_scraper.fetch_bulk_deals()


def test_corporate_actions_and_filings_list(web):
    from tools.data import nse_scraper, pdf_scraper

    web.get(
        re.compile(r"https://www\.nseindia\.com/api/corporates-corporateActions.*"),
        body=_text("nse_corporate_actions_TCS.json"),
    )
    for row in json.loads(_text("nse_corporate_actions_TCS.json")):
        contracts.NseCorporateAction.model_validate(row)
    df = nse_scraper.fetch_corporate_actions("TCS")
    assert len(df) > 0 and set(df["symbol"]) == {"TCS"}
    filings = pdf_scraper.fetch_nse_filings_list("TCS")
    assert filings and filings[0]["subject"]


def test_bhavcopy(web):
    from tools.data import nse_scraper

    web.get(
        "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_15102026.csv",
        body=_text("nse_bhavcopy.csv"),
    )
    df = nse_scraper.get_bhavcopy(datetime(2026, 10, 15))
    contracts.check_bhavcopy(df)
    assert "TCS" not in set() and len(df) > 10


# ── mutual funds ──────────────────────────────────────────────────────────────


def test_amfi_navall(web, monkeypatch):
    from tools.data import amfi

    monkeypatch.setattr(amfi, "_nav_cache", pd.DataFrame())
    web.get(amfi.AMFI_NAV_URL, body=_text("amfi_navall.txt"))
    df = amfi.get_all_nav()
    contracts.check_navall(df)
    assert set(df["plan"]) <= {"direct", "regular", "other"} and (df["nav"] > 0).all()


def test_mfapi_history(web):
    from tools.data import mf_data

    web.get("https://api.mfapi.in/mf/122639", body=_text("mfapi_122639.json"))
    contracts.MfapiHistory.model_validate(json.loads(_text("mfapi_122639.json")))
    series = mf_data.fetch_history("122639")
    assert len(series) > 1000 and (series > 0).all()
    assert mf_data.compute_metrics(series).get("cagr_1y") is not None


# ── news ──────────────────────────────────────────────────────────────────────


def test_google_news(web):
    from tools.data import news_fetcher

    web.get(re.compile(r"https://news\.google\.com/rss/search.*"), body=_text("google_news_TCS.xml"))
    contracts.check_feed(feedparser.parse(_text("google_news_TCS.xml")))
    items = news_fetcher.google_news("TCS share")
    assert items and all(i["title"] for i in items)


def test_market_rss_skips_dead_feeds(web):
    from config import NEWS_FEEDS
    from tools.data import news_fetcher

    for name, url in NEWS_FEEDS.items():
        if name == "economic_times":
            web.get(url, body=_text("rss_economic_times.xml"))
        else:
            web.get(url, status=404)
    contracts.check_feed(feedparser.parse(_text("rss_economic_times.xml")))
    items = news_fetcher.rss_feeds(n_per_feed=5)
    assert items and {i["source"] for i in items} == {"economic_times"}


# ── BSE (synthetic: BSE's CDN refuses scripted clients) ───────────────────────


def test_bse_filings(web):
    from tools.data import pdf_scraper

    body = _text("bse_filings_532540.json")
    contracts.BseFilings.model_validate(json.loads(body))
    web.get(re.compile(r"https://api\.bseindia\.com/.*"), body=body)
    out = pdf_scraper.fetch_bse_filings_list("TCS", "532540")
    assert len(out) == 2 and out[0]["url"].endswith("a1b2c3d4-0001.pdf")


# ── Yahoo (yfinance's own HTTP isn't requests-based: contract-tested on frozen data) ─


def test_yahoo_quote_and_bars_contracts():
    from tools.data.providers.fake import FakeProvider

    info = json.loads(
        (Path(__file__).parent / "fixtures" / "fundamentals" / "TCS.json").read_text(encoding="utf-8")
    )
    contracts.check_quote(info["info"])
    bars = pd.read_csv(
        Path(__file__).parent / "fixtures" / "ohlcv" / "TCS.csv", index_col="Date", parse_dates=True
    )
    contracts.check_bars(bars)
    contracts.check_quote(FakeProvider().quote("TCS"))
    contracts.check_bars(FakeProvider().ohlcv("TCS"))
    with pytest.raises(ValueError):
        contracts.check_quote({"currentPrice": None})


def test_every_recording_is_covered():
    index = json.loads(_text("index.json"))
    files = {p.name for p in HTTP.iterdir()} - {"index.json"}
    assert {e["file"] for e in index.values() if "file" in e} == files


# ── the live canary's plumbing (the live run itself is scripts/data_canary.py) ─


def test_canary_reports_each_source_and_the_job_raises_on_failure(monkeypatch):
    from scripts import data_canary

    def broken():
        raise ValueError("NAVAll columns missing: ['nav']")

    monkeypatch.setattr(data_canary, "CHECKS", {"good": lambda: None, "amfi": broken})
    results = data_canary.run()
    assert results["good"]["ok"] and not results["amfi"]["ok"]
    assert results["amfi"]["error"] == "ValueError: NAVAll columns missing: ['nav']"
    with pytest.raises(RuntimeError, match="amfi"):
        data_canary.job()


def test_the_canary_runs_in_market_hours_every_30_minutes():
    import worker

    job = {j.name: j for j in worker.JOBS}["data_canary"]
    assert job.every is not None and job.every.total_seconds() == 1800 and job.when is not None
