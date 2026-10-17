"""
tools/data/bhavcopy_history.py
Rolling per-symbol OHLC history built from daily NSE bhavcopy rows.

tools/data/fetcher.py already falls back to a single bhavcopy row when
yfinance has nothing for a symbol — enough for a last-known price, but not
enough for any multi-day indicator (RSI-14 needs 14+ rows; every technical
in tools/analysis/technical.py returns None on a 1-row frame). This module
gives that fallback a memory: every time a symbol's bhavcopy row is fetched,
it's appended here (once per calendar day, deduped), so a sustained
yfinance outage degrades technicals over days instead of leaving them blank
forever. This can't backfill days that passed before it started running —
it only ever has what's accumulated since first deployed — so it's a
slow-building safety net, not an instant fix.
"""

import os
import pandas as pd
from config import DATA_DIR

_HISTORY_DIR = os.path.join(DATA_DIR, "bhavcopy_history")
_MAX_ROWS = 300  # ~14 months of trading days — plenty for any indicator in use


def _path(symbol: str) -> str:
    os.makedirs(_HISTORY_DIR, exist_ok=True)
    return os.path.join(_HISTORY_DIR, f"{symbol}.parquet")


def record_row(symbol: str, row: dict):
    """Append one day's bhavcopy OHLC for `symbol`, deduped by date. `row` is
    the dict shape returned by fetcher.py::_bhavcopy_row (close/open/high/low/
    prev_close/date)."""
    date_str = (row.get("date") or "").strip()
    parsed = pd.to_datetime(date_str, format="%d-%b-%Y", errors="coerce")
    day = parsed if pd.notna(parsed) else pd.Timestamp.now().normalize()

    p = _path(symbol)
    try:
        existing = pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame()
    except Exception:
        existing = pd.DataFrame()

    if not existing.empty and day in existing.index:
        return  # already recorded today's row

    new_row = pd.DataFrame(
        {"Open": [row["open"]], "High": [row["high"]], "Low": [row["low"]],
         "Close": [row["close"]], "Volume": [0]},
        index=pd.DatetimeIndex([day]),
    )
    combined = pd.concat([existing, new_row]) if not existing.empty else new_row
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    if len(combined) > _MAX_ROWS:
        combined = combined.iloc[-_MAX_ROWS:]

    try:
        combined.to_parquet(p)
    except Exception:
        pass  # best-effort — a failed write just means no history gained today


def read_history(symbol: str, adjust: bool = True) -> pd.DataFrame:
    """Whatever rolling window has accumulated for `symbol` so far. Empty
    DataFrame if none has been recorded yet.

    Rows are stored as traded; with `adjust` (the default) bars before a split
    or bonus ex-date are rescaled from NSE's corporate-actions feed so the
    series is continuous (DATA-07)."""
    p = _path(symbol)
    if not os.path.exists(p):
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
    except Exception:
        return pd.DataFrame()
    if adjust and not df.empty:
        from tools.data.adjustments import adjust_ohlcv, corporate_factors

        df = adjust_ohlcv(df, corporate_factors(symbol))
    return df
