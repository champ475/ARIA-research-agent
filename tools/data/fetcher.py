"""
tools/data/fetcher.py
Primary market data layer: yfinance + Parquet cache.
All deprecated yfinance calls replaced.
"""

import time
import yfinance as yf
import pandas as pd
from config import NSE_SUFFIX, BSE_SUFFIX, MACRO_TICKERS
from tools.data.cache import cache_read, cache_write

# In-memory TTL caches — avoid redundant yfinance roundtrips within a single analysis
_info_cache: dict = {}   # symbol -> (data, timestamp)
_fins_cache: dict = {}   # symbol -> (data, timestamp)
_INFO_TTL = 300          # 5 minutes
_FINS_TTL = 300


def _tk(symbol: str, nse: bool = True) -> yf.Ticker:
    return yf.Ticker(f"{symbol}{NSE_SUFFIX if nse else BSE_SUFFIX}")


def get_info(symbol: str) -> dict:
    now = time.time()
    cached = _info_cache.get(symbol)
    if cached and (now - cached[1]) < _INFO_TTL:
        return cached[0]
    tk   = _tk(symbol)
    info = tk.info or {}
    if info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
        info = _tk(symbol, nse=False).info or {}
    _info_cache[symbol] = (info, now)
    return info


def get_financials(symbol: str) -> dict:
    now = time.time()
    cached = _fins_cache.get(symbol)
    if cached and (now - cached[1]) < _FINS_TTL:
        return cached[0]
    tk = _tk(symbol)
    out = {}
    for attr, key in [
        ("income_stmt",             "income_annual"),
        ("quarterly_income_stmt",   "income_quarterly"),
        ("balance_sheet",           "balance_annual"),
        ("quarterly_balance_sheet", "balance_quarterly"),
        ("cashflow",                "cashflow_annual"),
        ("quarterly_cashflow",      "cashflow_quarterly"),
    ]:
        try:
            out[key] = getattr(tk, attr)
        except Exception:
            out[key] = pd.DataFrame()
    _fins_cache[symbol] = (out, now)
    return out


def get_shareholders(symbol: str) -> dict:
    tk = _tk(symbol)
    out = {}
    for attr in ("major_holders", "institutional_holders", "mutualfund_holders"):
        try:
            out[attr] = getattr(tk, attr)
        except Exception:
            pass
    return out


def get_options(symbol: str) -> dict:
    tk  = _tk(symbol)
    out = {"available": False, "calls": pd.DataFrame(), "puts": pd.DataFrame(), "expiry": None}
    try:
        expiries = tk.options
        if not expiries:
            return out
        chain = tk.option_chain(expiries[0])
        out.update({"available": True, "calls": chain.calls, "puts": chain.puts, "expiry": expiries[0]})
    except Exception:
        pass
    return out


def get_recommendations(symbol: str) -> pd.DataFrame:
    try:
        recs = _tk(symbol).recommendations
        return recs if recs is not None and not recs.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_macro() -> dict:
    out = {}
    for name, ticker in MACRO_TICKERS.items():
        try:
            hist = yf.Ticker(ticker).history(period="2d", interval="1d")
            if len(hist) >= 2:
                latest = hist["Close"].iloc[-1]
                prev   = hist["Close"].iloc[-2]
                chg    = ((latest - prev) / abs(prev)) * 100
                out[name] = {"price": round(float(latest), 2), "change": round(float(chg), 2)}
            elif len(hist) == 1:
                out[name] = {"price": round(float(hist["Close"].iloc[-1]), 2), "change": 0.0}
        except Exception:
            out[name] = {"price": "N/A", "change": "N/A"}
    return out


def get_sector_peers(symbol: str, info: dict) -> list[dict]:
    from config import SECTOR_UNIVERSE
    sector = info.get("sector", "")
    peers  = []
    for sec, syms in SECTOR_UNIVERSE.items():
        if sec.lower() in sector.lower() or sector.lower() in sec.lower():
            peers = [s for s in syms if s != symbol][:8]
            break
    if not peers:
        peers = SECTOR_UNIVERSE.get("Technology", [])[:6]

    result = []
    for p in peers:
        try:
            hist = yf.Ticker(f"{p}.NS").history(period="5d", interval="1d")
            if len(hist) >= 2:
                chg = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
                result.append({"symbol": p, "change": round(float(chg), 2), "price": round(float(hist["Close"].iloc[-1]), 2)})
        except Exception:
            continue
    return result


def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    cached = cache_read(symbol, period, interval)
    if cached is not None:
        return cached
    tk = _tk(symbol)
    df = tk.history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        df = _tk(symbol, nse=False).history(period=period, interval=interval, auto_adjust=True)
    cache_write(symbol, period, interval, df)
    return df


def search_ticker(name: str) -> list[dict]:
    try:
        results = yf.Search(name, max_results=6)
        found   = []
        for q in (results.quotes or []):
            if q.get("exchange", "") in ("NSI", "BSE", "NSE", "BOM"):
                sym = q.get("symbol", "").replace(".NS", "").replace(".BO", "")
                found.append({"symbol": sym, "name": q.get("shortname") or q.get("longname", "")})
        return found
    except Exception:
        return []
