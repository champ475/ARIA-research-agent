"""
tools/data/adjustments.py — split / bonus adjustment of unadjusted price history (DATA-07).

yfinance history is fetched with auto_adjust=True, so it is already
split/bonus (and dividend) adjusted. The NSE bhavcopy rows kept by
tools/data/bhavcopy_history.py are the prices as traded. Across an ex-date the
raw series falls by the split ratio — a fake crash that corrupts RSI/MACD, the
52-week range and alerts. The history is therefore stored raw and adjusted on
read from NSE's corporate-actions feed: every bar before an ex-date has its
prices divided (and volume multiplied) by that action's factor. One source per
series, never a mix of adjusted and unadjusted bars.

NSE `subject` wording handled:
    "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"   factor 5
    "Bonus 1:1"      (1 new share for every 1 held)                                  factor 2
    "Bonus 4:1"                                                                      factor 5
Rights issues, buy-backs and dividends are not adjusted here.
"""

from __future__ import annotations

import logging
import re
import threading

import pandas as pd
from cachetools import TTLCache

log = logging.getLogger(__name__)

_SPLIT = re.compile(r"from\s+(?:rs\.?|re\.?|inr)?\s*([\d.]+)\D+?to\s+(?:rs\.?|re\.?|inr)?\s*([\d.]+)", re.IGNORECASE)
_BONUS = re.compile(r"bonus\D*?(\d+)\s*:\s*(\d+)", re.IGNORECASE)
_PRICE_COLS = ("Open", "High", "Low", "Close")
MAX_PLAUSIBLE_MOVE = 0.40  # a one-day move beyond this in a stored series is suspect

_cache: TTLCache = TTLCache(maxsize=512, ttl=24 * 3600)
_lock = threading.Lock()


def parse_factor(subject: str) -> float | None:
    """Price divisor for one corporate action, or None if it isn't a split/bonus."""
    text = (subject or "").strip()
    if not text:
        return None
    low = text.lower()
    if "split" in low or "sub-division" in low or "sub division" in low:
        m = _SPLIT.search(text)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if old > 0 and new > 0 and old != new:
                return old / new
        return None
    if "bonus" in low:
        m = _BONUS.search(text)
        if m:
            new_shares, held = float(m.group(1)), float(m.group(2))
            if new_shares > 0 and held > 0:
                return (new_shares + held) / held
    return None


def factors_from_actions(actions: pd.DataFrame) -> list[tuple[pd.Timestamp, float]]:
    """(ex_date, factor) pairs, oldest first, from NSE's corporate-actions table."""
    if actions is None or actions.empty or "subject" not in actions or "exDate" not in actions:
        return []
    out: list[tuple[pd.Timestamp, float]] = []
    for subject, ex in zip(actions["subject"], actions["exDate"], strict=False):
        factor = parse_factor(str(subject))
        day = pd.to_datetime(ex, format="%d-%b-%Y", errors="coerce")
        if factor and pd.notna(day):
            out.append((day.normalize(), factor))
    return sorted(set(out))


def adjust_ohlcv(df: pd.DataFrame, factors: list[tuple[pd.Timestamp, float]]) -> pd.DataFrame:
    """Copy of `df` with every bar before each ex-date divided by that factor
    (volume multiplied). Raw input only: never call it on adjusted data."""
    if df is None or df.empty or not factors:
        return df
    out = df.copy()
    numeric = [c for c in (*_PRICE_COLS, "Volume") if c in out.columns]
    out[numeric] = out[numeric].astype(float)
    cols = [c for c in _PRICE_COLS if c in out.columns]
    idx = out.index.tz_localize(None) if getattr(out.index, "tz", None) is not None else out.index
    for ex_date, factor in factors:
        before = idx < ex_date
        if not before.any():
            continue
        out.loc[before, cols] = out.loc[before, cols] / factor
        if "Volume" in out.columns:
            out.loc[before, "Volume"] = out.loc[before, "Volume"] * factor
    out.attrs = dict(df.attrs)
    return out


def unadjusted_jumps(df: pd.DataFrame, threshold: float = MAX_PLAUSIBLE_MOVE) -> list[pd.Timestamp]:
    """Dates whose close moved more than `threshold` from the previous bar."""
    if df is None or len(df) < 2 or "Close" not in df:
        return []
    moves = df["Close"].pct_change().abs()
    return [d for d, m in moves.items() if pd.notna(m) and m > threshold]


def corporate_factors(symbol: str) -> list[tuple[pd.Timestamp, float]]:
    """Split/bonus factors for a symbol (24 h cache). Empty when NSE can't be reached."""
    key = symbol.upper()
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        from tools.data.nse_scraper import get_corporate_actions

        factors = factors_from_actions(get_corporate_actions(key))
    except Exception:
        log.warning("corporate actions unavailable for %s", key, exc_info=True)
        return []  # not cached: try again next call
    with _lock:
        _cache[key] = factors
    return factors


def clear_cache() -> None:
    with _lock:
        _cache.clear()
