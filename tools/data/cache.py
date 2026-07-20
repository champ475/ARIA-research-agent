"""
tools/data/cache.py
Parquet cache for OHLCV data + SQLite metadata.
Avoids redundant yfinance calls.
"""

import os
import pandas as pd
from datetime import datetime, timedelta
from config import DATA_DIR

CACHE_DIR = os.path.join(DATA_DIR, "parquet")


def _path(symbol: str, period: str, interval: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{symbol}_{period}_{interval}.parquet")


def cache_read(symbol: str, period: str, interval: str, max_age_hours: int = 4) -> pd.DataFrame | None:
    p = _path(symbol, period, interval)
    if not os.path.exists(p):
        return None
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(p))
    if age > timedelta(hours=max_age_hours):
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def cache_write(symbol: str, period: str, interval: str, df: pd.DataFrame):
    if df.empty:
        return
    try:
        df.to_parquet(_path(symbol, period, interval))
    except Exception:
        pass


def cache_clear(symbol: str = None):
    if not os.path.exists(CACHE_DIR):
        return
    for f in os.listdir(CACHE_DIR):
        if symbol is None or f.startswith(symbol):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except Exception:
                pass
