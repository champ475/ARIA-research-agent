"""
tools/data/nse_scraper.py
Scrapes NSE/BSE free data: bhavcopy, bulk deals, corporate actions.
Uses headers to avoid bot detection.
"""

import requests
import pandas as pd

from typing import Any

from utils import http
from utils.errors import SourceUnavailable
from utils.market_clock import is_trading_day
from utils.resilience import CircuitOpen
from datetime import datetime, timedelta
from io import StringIO
import logging

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
}

NSE_SESSION_URL = "https://www.nseindia.com"


def _session() -> requests.Session:
    # REL-02: default timeouts, retries with jitter, and the "nse" breaker.
    s = http.session("nse", headers=HEADERS)
    try:
        s.get(NSE_SESSION_URL, timeout=10)
    except Exception:
        pass
    return s


def get_bhavcopy(date: datetime = None) -> pd.DataFrame:
    """
    Download NSE bhavcopy (full market data) for a given date.
    Falls back to previous trading day if today's not available.
    """
    if date is None:
        date = datetime.now()

    for delta in range(5):
        d = date - timedelta(days=delta)
        if not is_trading_day(d.date()):
            continue
        date_str = d.strftime("%d%m%Y")
        url = f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
        try:
            s   = _session()
            r   = s.get(url, timeout=15)
            if r.status_code == 200 and len(r.content) > 1000:
                df = pd.read_csv(StringIO(r.text))
                df.columns = [c.strip() for c in df.columns]
                return df
        except Exception as e:
            log.warning(f"Bhavcopy {date_str} failed: {e}")
    return pd.DataFrame()


def api_json(url: str) -> Any:
    """DATA-02 strict fetch: the parsed JSON, or SourceUnavailable("nse") when
    NSE refuses or fails (blocked, 5xx, not JSON). CircuitOpen passes through
    so the answer can say the source is being skipped. 404 = no data (None)."""
    try:
        r = _session().get(url, timeout=10)
    except CircuitOpen:
        raise
    except Exception as e:
        raise SourceUnavailable("nse", detail=type(e).__name__) from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise SourceUnavailable("nse", detail=f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise SourceUnavailable("nse", detail="not JSON (likely a block page)") from e


def _quiet(fetch, *args, what: str):
    """Legacy callers expect an empty frame on failure, never an exception."""
    try:
        return fetch(*args)
    except Exception as e:
        log.warning(f"{what} fetch failed: {type(e).__name__}")
        return pd.DataFrame()


def _deals(kind: str, symbol: str | None) -> pd.DataFrame:
    data = api_json(f"https://www.nseindia.com/api/{kind}-deal-data")
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if symbol and not df.empty and "symbol" in df.columns:
        df = df[df["symbol"].str.upper() == symbol.upper()]
    return df


def fetch_bulk_deals(symbol: str | None = None) -> pd.DataFrame:
    return _deals("bulk", symbol)


def fetch_block_deals(symbol: str | None = None) -> pd.DataFrame:
    return _deals("block", symbol)


def fetch_corporate_actions(symbol: str) -> pd.DataFrame:
    data = api_json(f"https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={symbol}")
    return pd.DataFrame(data) if data else pd.DataFrame()


def fetch_top_movers(category: str = "gainers") -> pd.DataFrame:
    index = "loosers" if category == "losers" else "gainers"  # NSE spells it "loosers"
    data = api_json(f"https://www.nseindia.com/api/live-analysis-variations?index={index}") or {}
    if not isinstance(data, dict):
        raise SourceUnavailable("nse", detail="unexpected movers shape")
    return pd.DataFrame(data.get("NIFTY", {}).get("data", []) or [])


def get_bulk_deals(symbol: str = None) -> pd.DataFrame:
    """Fetch bulk deal data from NSE (last available); empty on failure."""
    return _quiet(fetch_bulk_deals, symbol, what="Bulk deals")


def get_block_deals(symbol: str = None) -> pd.DataFrame:
    """Fetch block deal data from NSE; empty on failure."""
    return _quiet(fetch_block_deals, symbol, what="Block deals")


def get_corporate_actions(symbol: str) -> pd.DataFrame:
    """Fetch corporate actions (dividends, splits, bonuses) from NSE; empty on failure."""
    return _quiet(fetch_corporate_actions, symbol, what=f"Corporate actions for {symbol}")


def get_shareholding_pattern(symbol: str) -> dict:
    """Fetch latest shareholding pattern from NSE."""
    try:
        s   = _session()
        url = f"https://www.nseindia.com/api/corporate-shareholding-extrainfo?symbol={symbol}&type=recent"
        r   = s.get(url, timeout=10)
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception as e:
        log.warning(f"Shareholding for {symbol} failed: {e}")
        return {}


def get_top_movers(category: str = "gainers") -> pd.DataFrame:
    """Top gainers ('gainers') or losers ('losers') from NSE; empty on failure."""
    return _quiet(fetch_top_movers, category, what="Top movers")


def format_bulk_deals(df: pd.DataFrame, symbol: str) -> str:
    """Format bulk/block deal data as markdown."""
    if df.empty:
        return f"No recent bulk/block deals found for {symbol}."
    cols = [c for c in ["dealDate", "clientName", "buyOrSell", "quantity", "tradePrice"] if c in df.columns]
    if not cols:
        cols = df.columns.tolist()[:5]
    subset = df[cols].head(10)
    return f"### Recent Bulk/Block Deals — {symbol}\n\n" + subset.to_string(index=False)
