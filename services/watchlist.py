"""
services/watchlist.py — the watchlist table's data (REL-07).

One row per symbol, each needing a quote and a short price history for the RSI.
They used to be fetched one after another on every add, remove and refresh, so
a 20-symbol list took 10-40 s each time. Now:

- rows are fetched in parallel (`WATCHLIST_WORKERS` at a time) under one time
  budget (`WATCHLIST_BUDGET_SECONDS`); a symbol that does not answer in time
  shows a dash and the rest of the table still renders;
- each finished row is cached for `WATCHLIST_ROW_TTL_SECONDS`, so adding one
  symbol re-fetches only the new one, and a refresh within a minute is instant.
"""

from __future__ import annotations

import logging
import threading

from cachetools import TTLCache

from config import WATCHLIST_BUDGET_SECONDS, WATCHLIST_ROW_TTL_SECONDS, WATCHLIST_WORKERS
from utils.db import WatchlistFull  # re-exported: the UI catches it
from utils.forms import WATCHLIST_MAX
from utils.parallel import parallel_map

log = logging.getLogger(__name__)

_rows: TTLCache = TTLCache(maxsize=2048, ttl=WATCHLIST_ROW_TTL_SECONDS)
_lock = threading.Lock()

_BLANK = ["—", "—", "—", "—", "—", "—"]


def clear_cache() -> None:
    with _lock:
        _rows.clear()


def _build_row(sym: str) -> list:
    from tools.analysis.technical import compute_all, snapshot
    from tools.data.fetcher import get_info, get_ohlcv

    info = get_info(sym)
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    prev = info.get("previousClose") or price
    chg = round(((price - prev) / prev) * 100, 2) if prev else 0.0
    name = (info.get("shortName") or sym)[:22]
    high52 = info.get("fiftyTwoWeekHigh", "N/A")
    low52 = info.get("fiftyTwoWeekLow", "N/A")

    # RSI from cached OHLCV (fast — usually already cached). snapshot() always
    # has the "RSI" key but its value is None when the history is too short, so
    # a bare .get("RSI", "—") would render the literal string "None".
    rsi: object = "—"
    try:
        df = get_ohlcv(sym, "3mo", "1d")
        if not df.empty:
            ta = snapshot(compute_all(df.copy()))
            rsi = ta.get("RSI")
            rsi = "N/A" if rsi is None else rsi
    except Exception:
        pass

    arrow = "▲" if chg >= 0 else "▼"
    return [
        sym,
        name,
        f"₹{price:,.2f}",
        f"{arrow} {chg:+.2f}%",
        rsi,
        f"₹{high52:,.2f}" if isinstance(high52, float) else str(high52),
        f"₹{low52:,.2f}" if isinstance(low52, float) else str(low52),
    ]


def _row(sym: str) -> list:
    with _lock:
        cached = _rows.get(sym)
    if cached is not None:
        return cached
    row = _build_row(sym)
    with _lock:
        _rows[sym] = row
    return row


def watchlist_rows(symbols: list[str]) -> list[list]:
    """Rows for the watchlist table, in the order given. A symbol that fails or
    does not answer within the budget gets a blank row (and is not cached)."""
    results = parallel_map(_row, symbols, max_workers=WATCHLIST_WORKERS, budget_s=WATCHLIST_BUDGET_SECONDS)
    rows = []
    for sym, (ok, value) in zip(symbols, results, strict=True):
        if ok and isinstance(value, list):
            rows.append(value)
        else:
            log.info("[watchlist] %s skipped: %s", sym, type(value).__name__)
            rows.append([sym, *_BLANK])
    return rows


# ── The user's list (the UI calls these; it never touches the database) ──────

__all__ = [
    "WATCHLIST_MAX",
    "WatchlistFull",
    "add_symbol",
    "clear_cache",
    "remove_symbol",
    "table_rows",
    "watchlist_rows",
]


def table_rows(user_id: int) -> list[list]:
    """Rows for the watchlist table: Symbol, Name, Price, Change%, RSI, 52W High, 52W Low
    (fetched in parallel and cached per row)."""
    from utils.db import watchlist_all

    symbols = watchlist_all(user_id)
    return watchlist_rows(symbols) if symbols else []


def add_symbol(user_id: int, raw: str) -> tuple[bool, str]:
    """(added, symbol). VAL-07: only listed NSE symbols, resolved like a chat
    mention, at most WATCHLIST_MAX. Raises UserInputError for a bad name and
    WatchlistFull at the cap; `added` is False when it was already there."""
    from utils.db import watchlist_add
    from utils.forms import watchlist_symbol

    sym = watchlist_symbol(raw)
    return watchlist_add(user_id, sym, max_symbols=WATCHLIST_MAX), sym


def remove_symbol(user_id: int, raw: str) -> str | None:
    """The cleaned symbol that was removed, or None for an empty entry."""
    from utils.db import watchlist_remove
    from utils.helpers import clean_ticker

    sym = clean_ticker((raw or "").strip().upper())
    if not sym:
        return None
    watchlist_remove(user_id, sym)
    return sym
