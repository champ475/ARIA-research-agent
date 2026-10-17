"""
tools/data/cache.py
OHLCV cache. Avoids redundant yfinance calls.

DATA-10: the shared copy lives in Postgres (`ohlcv_cache`, Parquet bytes), so a
restarted container or a second replica starts warm instead of hitting yfinance
cold. The local Parquet directory remains as a fallback for when the database
is unreachable (and for running without one in dev); it is never the primary.
"""

import io
import logging
import os
import pandas as pd
from datetime import datetime, timedelta
from config import DATA_DIR

log = logging.getLogger(__name__)

CACHE_DIR = os.path.join(DATA_DIR, "parquet")


def _path(symbol: str, period: str, interval: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{symbol}_{period}_{interval}.parquet")


def _disk_read(symbol: str, period: str, interval: str, max_age_hours: float) -> pd.DataFrame | None:
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


def cache_read(symbol: str, period: str, interval: str, max_age_hours: int = 4) -> pd.DataFrame | None:
    from utils import db

    try:
        blob = db.ohlcv_cache_get(symbol, period, interval, max_age_hours)
    except Exception:
        log.debug("OHLCV cache: database unavailable, trying disk", exc_info=True)
        return _disk_read(symbol, period, interval, max_age_hours)
    if blob is None:
        return None
    try:
        return pd.read_parquet(io.BytesIO(blob))
    except Exception:
        log.warning("OHLCV cache: unreadable entry for %s %s %s", symbol, period, interval)
        return None


def cache_write(symbol: str, period: str, interval: str, df: pd.DataFrame):
    if df.empty:
        return
    from utils import db

    try:
        buf = io.BytesIO()
        df.to_parquet(buf)
        db.ohlcv_cache_put(symbol, period, interval, buf.getvalue())
        return
    except Exception:
        log.debug("OHLCV cache: database write failed, writing to disk", exc_info=True)
    try:
        df.to_parquet(_path(symbol, period, interval))
    except Exception:
        pass


def cache_clear(symbol: str = None):
    from utils import db

    try:
        db.ohlcv_cache_clear(symbol)
    except Exception:
        log.debug("OHLCV cache: database clear failed", exc_info=True)
    if not os.path.exists(CACHE_DIR):
        return
    for f in os.listdir(CACHE_DIR):
        if symbol is None or f.startswith(symbol):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except Exception:
                pass
