"""
tools/data/nse_scraper.py
Scrapes NSE/BSE free data: bhavcopy, bulk deals, corporate actions.
Uses headers to avoid bot detection.
"""

import requests
import pandas as pd

from utils import http
from utils.market_clock import is_trading_day
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


def get_bulk_deals(symbol: str = None) -> pd.DataFrame:
    """Fetch bulk deal data from NSE (last available)."""
    try:
        s   = _session()
        url = "https://www.nseindia.com/api/bulk-deal-data"
        r   = s.get(url, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if symbol and not df.empty and "symbol" in df.columns:
            df = df[df["symbol"].str.upper() == symbol.upper()]
        return df
    except Exception as e:
        log.warning(f"Bulk deals fetch failed: {e}")
        return pd.DataFrame()


def get_block_deals(symbol: str = None) -> pd.DataFrame:
    """Fetch block deal data from NSE."""
    try:
        s   = _session()
        url = "https://www.nseindia.com/api/block-deal-data"
        r   = s.get(url, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if symbol and not df.empty and "symbol" in df.columns:
            df = df[df["symbol"].str.upper() == symbol.upper()]
        return df
    except Exception as e:
        log.warning(f"Block deals fetch failed: {e}")
        return pd.DataFrame()


def get_corporate_actions(symbol: str) -> pd.DataFrame:
    """Fetch corporate actions (dividends, splits, bonuses) from NSE."""
    try:
        s   = _session()
        url = f"https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={symbol}"
        r   = s.get(url, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        return pd.DataFrame(data) if data else pd.DataFrame()
    except Exception as e:
        log.warning(f"Corporate actions for {symbol} failed: {e}")
        return pd.DataFrame()


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
    """
    Fetch top gainers or losers from NSE.
    category: 'gainers' | 'losers'
    """
    try:
        s   = _session()
        url = f"https://www.nseindia.com/api/live-analysis-variations?index=gainers"
        if category == "losers":
            url = "https://www.nseindia.com/api/live-analysis-variations?index=loosers"
        r   = s.get(url, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        data = r.json()
        records = data.get("NIFTY", {}).get("data", []) or []
        return pd.DataFrame(records)
    except Exception as e:
        log.warning(f"Top movers fetch failed: {e}")
        return pd.DataFrame()


def format_bulk_deals(df: pd.DataFrame, symbol: str) -> str:
    """Format bulk/block deal data as markdown."""
    if df.empty:
        return f"No recent bulk/block deals found for {symbol}."
    cols = [c for c in ["dealDate", "clientName", "buyOrSell", "quantity", "tradePrice"] if c in df.columns]
    if not cols:
        cols = df.columns.tolist()[:5]
    subset = df[cols].head(10)
    return f"### Recent Bulk/Block Deals — {symbol}\n\n" + subset.to_string(index=False)
