"""
tools/data/fetcher.py
Primary market data layer: yfinance + Parquet cache.
All deprecated yfinance calls replaced.

yfinance is a single point of failure for the whole app — an outage or
schema change there would take down equity research, screener, mf, and
alerts simultaneously. get_info()/get_ohlcv() fall back to NSE bhavcopy
(tools/data/nse_scraper.py) for a last-EOD-close price when yfinance has
nothing, so the app degrades instead of dying. Fallback results are tagged
with "_degraded_source" so callers (see EquityAgent's confidence tiering)
can flag to the user that they're looking at a stale/partial quote.
"""

import time
import logging
import yfinance as yf
import pandas as pd
from config import NSE_SUFFIX, BSE_SUFFIX, MACRO_TICKERS, YF_TIMEOUT_SECONDS
from tools.data.cache import cache_read, cache_write
from utils.resilience import guarded_call

log = logging.getLogger(__name__)

# In-memory TTL caches — avoid redundant yfinance roundtrips within a single analysis
_info_cache: dict = {}   # symbol -> (data, timestamp)
_fins_cache: dict = {}   # symbol -> (data, timestamp)
_INFO_TTL = 300          # 5 minutes
_FINS_TTL = 300

# Bhavcopy is one CSV for the whole market — cache it once per calendar day
# rather than re-downloading per symbol.
_bhavcopy_cache: dict = {}   # "YYYY-MM-DD" -> DataFrame


def _tk(symbol: str, nse: bool = True) -> yf.Ticker:
    return yf.Ticker(f"{symbol}{NSE_SUFFIX if nse else BSE_SUFFIX}")


def _yf(fn, *args, **kwargs):
    """Every yfinance network touch goes through here (REL-02): yfinance takes
    no timeout for .info/.financials/etc., so the wait is bounded at
    YF_TIMEOUT_SECONDS, and the shared "yfinance" breaker skips it entirely
    for a cool-down after repeated failures. Errors raise into the callers'
    existing except blocks, which already fall back (BSE, then bhavcopy)."""
    return guarded_call("yfinance", fn, *args, timeout=YF_TIMEOUT_SECONDS, **kwargs)


def _bhavcopy_row(symbol: str) -> dict | None:
    """Best-effort EOD OHLC for `symbol` from NSE bhavcopy — used only when
    yfinance has returned nothing for both the NSE and BSE ticker."""
    from tools.data.nse_scraper import get_bhavcopy

    today_key = time.strftime("%Y-%m-%d")
    df = _bhavcopy_cache.get(today_key)
    if df is None:
        df = get_bhavcopy()
        _bhavcopy_cache.clear()   # only one trading day's worth needs holding
        _bhavcopy_cache[today_key] = df

    if df is None or df.empty or "SYMBOL" not in df.columns:
        return None
    match = df[df["SYMBOL"].str.strip() == symbol.upper()]
    if "SERIES" in df.columns:
        match = match[match["SERIES"].str.strip() == "EQ"]
    if match.empty:
        return None

    row = match.iloc[0]
    try:
        return {
            "close":      float(row["CLOSE_PRICE"]),
            "open":       float(row["OPEN_PRICE"]),
            "high":       float(row["HIGH_PRICE"]),
            "low":        float(row["LOW_PRICE"]),
            "prev_close": float(row["PREV_CLOSE"]),
            "date":       str(row.get("DATE1", "")).strip(),
        }
    except (KeyError, ValueError, TypeError) as e:
        log.warning(f"Bhavcopy row for {symbol} malformed: {e}")
        return None


def get_info(symbol: str) -> dict:
    now = time.time()
    cached = _info_cache.get(symbol)
    if cached and (now - cached[1]) < _INFO_TTL:
        return cached[0]

    try:
        info = _yf(lambda: _tk(symbol).info) or {}
    except Exception as e:
        log.warning(f"yfinance NSE get_info failed for {symbol}: {e}")
        info = {}

    if info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
        try:
            info = _yf(lambda: _tk(symbol, nse=False).info) or info
        except Exception as e:
            log.warning(f"yfinance BSE get_info failed for {symbol}: {e}")

    if info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
        row = _bhavcopy_row(symbol)
        if row:
            info = {
                **info,
                "currentPrice":      row["close"],
                "regularMarketPrice": row["close"],
                "previousClose":     row["prev_close"],
                "dayHigh":           row["high"],
                "dayLow":            row["low"],
                "longName":          info.get("longName") or symbol,
                "shortName":         info.get("shortName") or symbol,
                "_degraded_source":  f"NSE bhavcopy ({row['date'] or 'latest session'}) — yfinance unavailable",
            }

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
            out[key] = _yf(getattr, tk, attr)
        except Exception:
            out[key] = pd.DataFrame()
    _fins_cache[symbol] = (out, now)
    return out


def get_shareholders(symbol: str) -> dict:
    tk = _tk(symbol)
    out = {}
    for attr in ("major_holders", "institutional_holders", "mutualfund_holders"):
        try:
            out[attr] = _yf(getattr, tk, attr)
        except Exception:
            pass
    return out


def get_options(symbol: str) -> dict:
    tk  = _tk(symbol)
    out = {"available": False, "calls": pd.DataFrame(), "puts": pd.DataFrame(), "expiry": None}
    try:
        expiries = _yf(lambda: tk.options)
        if not expiries:
            return out
        chain = _yf(tk.option_chain, expiries[0])
        out.update({"available": True, "calls": chain.calls, "puts": chain.puts, "expiry": expiries[0]})
    except Exception:
        pass
    return out


def get_recommendations(symbol: str) -> pd.DataFrame:
    try:
        recs = _yf(lambda: _tk(symbol).recommendations)
        return recs if recs is not None and not recs.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


_FX_TTL = 3600
_fx_cache: dict[str, tuple[float, float]] = {}


def get_fx(from_currency: str, to_currency: str) -> float | None:
    """Units of `to_currency` per unit of `from_currency` (last daily close), or
    None when it can't be fetched. Cached for an hour."""
    pair = f"{from_currency}{to_currency}".upper()
    now = time.time()
    hit = _fx_cache.get(pair)
    if hit and now - hit[1] < _FX_TTL:
        return hit[0]
    try:
        hist = _yf(yf.Ticker(f"{pair}=X").history, period="5d", interval="1d")
        rate = float(hist["Close"].dropna().iloc[-1])
    except Exception as e:
        log.warning(f"FX {pair} unavailable: {e}")
        return None
    if not rate > 0:
        return None
    _fx_cache[pair] = (rate, now)
    return rate


def get_macro() -> dict:
    out = {}
    for name, ticker in MACRO_TICKERS.items():
        try:
            hist = _yf(yf.Ticker(ticker).history, period="2d", interval="1d")
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


def get_ohlcv(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    cached = cache_read(symbol, period, interval)
    if cached is not None:
        return cached

    try:
        df = _yf(_tk(symbol).history, period=period, interval=interval, auto_adjust=True)
    except Exception as e:
        log.warning(f"yfinance NSE OHLCV failed for {symbol}: {e}")
        df = pd.DataFrame()

    if df.empty:
        try:
            df = _yf(_tk(symbol, nse=False).history, period=period, interval=interval, auto_adjust=True)
        except Exception as e:
            log.warning(f"yfinance BSE OHLCV failed for {symbol}: {e}")
            df = pd.DataFrame()

    degraded = False
    if df.empty:
        # yfinance has nothing — fall back to NSE bhavcopy. Every row we ever
        # see here also gets recorded into a per-symbol rolling history
        # (tools/data/bhavcopy_history.py), so a sustained outage builds up
        # real multi-day OHLC over time instead of being stuck at one row
        # forever. Prefer that accumulated window when it's long enough for
        # indicators to compute on; otherwise fall back to today's single
        # row, which still keeps a last-known price/candle available instead
        # of a hard failure (callers already guard "len(df) < N" before
        # running multi-day indicators — see EquityAgent._fetch_bundle).
        from tools.data.bhavcopy_history import record_row, read_history

        row = _bhavcopy_row(symbol)
        if row:
            record_row(symbol, row)

        history = read_history(symbol)
        if len(history) >= 2:
            df = history
            df.attrs["_degraded_source"] = (
                f"NSE bhavcopy rolling history ({len(history)} sessions) — yfinance unavailable"
            )
            degraded = True
        elif row:
            parsed = pd.to_datetime(row["date"], format="%d-%b-%Y", errors="coerce")
            idx = parsed if pd.notna(parsed) else pd.Timestamp.now()
            df = pd.DataFrame(
                {"Open": [row["open"]], "High": [row["high"]], "Low": [row["low"]],
                 "Close": [row["close"]], "Volume": [0]},
                index=pd.DatetimeIndex([idx]),
            )
            df.attrs["_degraded_source"] = f"NSE bhavcopy ({row['date'] or 'latest session'})"
            degraded = True

    if not degraded:
        cache_write(symbol, period, interval, df)
    if len(df) > 1:
        from tools.data.adjustments import unadjusted_jumps

        jumps = unadjusted_jumps(df)
        if jumps:  # DATA-07: a real >40% day is rare; usually a missed split or bonus
            log.warning("%s: close moved >40%% in one day on %s — check split/bonus adjustment", symbol, jumps[:3])
    return df


def search_ticker(name: str) -> list[dict]:
    try:
        results = _yf(yf.Search, name, max_results=6)
        found   = []
        for q in (results.quotes or []):
            if q.get("exchange", "") in ("NSI", "BSE", "NSE", "BOM"):
                sym = q.get("symbol", "").replace(".NS", "").replace(".BO", "")
                found.append({"symbol": sym, "name": q.get("shortname") or q.get("longname", "")})
        return found
    except Exception:
        return []
