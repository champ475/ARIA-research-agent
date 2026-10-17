"""
scripts/refresh_fundamentals.py — nightly fundamentals snapshot (DATA-05).

    python scripts/refresh_fundamentals.py                  # whole NSE EQ universe
    python scripts/refresh_fundamentals.py --limit 100      # first 100 (a smoke run)
    python scripts/refresh_fundamentals.py --symbols TCS,SBIN
    python scripts/refresh_fundamentals.py --resume-hours 0 # refetch everything

Walks every EQ-series symbol in the instrument master (data/instruments/),
fetches its quote through the market-data provider (bounded, breaker-guarded,
DATA-02) and upserts the screen fields into `fundamentals_snapshot`. A rerun
skips symbols refreshed within --resume-hours (default 20) that have real
fundamentals, so a run cut short resumes where it stopped. Calls are spread
over --workers threads with a --delay between each worker's calls to stay
under Yahoo's rate limits (4 workers / 0.25 s got throttled on the full
universe; 2 / 0.6 didn't, about 21 min for ~2350 symbols).

When Yahoo refuses, the quote falls back to the NSE bhavcopy, a price with
no fundamentals (tagged `_degraded_source`). Such a quote never overwrites
a complete row from an earlier run, only fills a symbol that has none, and
the degraded symbols get one slower retry pass after --retry-pause seconds.

The worker (REL-05) runs this nightly; the screener flags data older than
FUNDAMENTALS_MAX_AGE_HOURS.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("refresh_fundamentals")

BATCH = 50


def universe() -> list[tuple[str, str, str]]:
    """(symbol, name, isin) for every EQ-series instrument."""
    from tools.data.instruments import master

    return [(i.symbol, i.name, i.isin) for i in master().instruments if i.series == "EQ"]


def snapshot_row(symbol: str, name: str, isin: str, info: dict[str, Any]) -> dict[str, Any] | None:
    """The snapshot row for one quote, or None when there's no price at all."""
    from tools.analysis.sector_profile import LENDER, sector_profile
    from tools.screener import metrics_from_info

    if not info:
        return None
    m = metrics_from_info(info)
    if m.get("price") is None:
        return None
    return {
        "symbol": symbol,
        "isin": isin or None,
        "name": info.get("longName") or info.get("shortName") or name or symbol,
        "sector": info.get("sector") or None,
        "industry": info.get("industry") or None,
        "lender": sector_profile(info) == LENDER,
        "price": m["price"],
        "mcap_cr": m["mcap"],
        "pe": m["pe"],
        "pb": m["pb"],
        "roe": m["roe"],
        "roce": m["roce"],
        "de": m["de"],
        "div_yield": m["div_yield"],
        "rev_growth": m["rev_growth"],
        "from_52w_high": m["from_52w_high"],
        "from_52w_low": m["from_52w_low"],
    }


def refresh(
    symbols: list[tuple[str, str, str]],
    workers: int = 2,
    delay: float = 0.6,
    fetch=None,
    degraded_out: list[tuple[str, str, str]] | None = None,
) -> dict[str, int]:
    """Fetch and upsert; returns counts {written, degraded, empty, failed}.
    `written` counts complete rows; a degraded (price-only) quote is only
    inserted for a symbol with no row yet and is appended to `degraded_out`."""
    from tools.data.providers import market_data
    from utils.db import fundamentals_upsert

    quote = fetch or (lambda s: market_data().quote(s))
    counts = {"written": 0, "degraded": 0, "empty": 0, "failed": 0}
    pending: list[dict[str, Any]] = []
    price_only: list[dict[str, Any]] = []

    def one(item: tuple[str, str, str]) -> tuple[dict[str, Any] | None | Exception, bool]:
        symbol, name, isin = item
        try:
            info = quote(symbol)
            return snapshot_row(symbol, name, isin, info), bool(info and info.get("_degraded_source"))
        except Exception as e:
            return e, False
        finally:
            if delay:
                time.sleep(delay)

    def flush() -> None:
        counts["written"] += fundamentals_upsert(pending)
        fundamentals_upsert(price_only, keep_existing=True)
        pending.clear()
        price_only.clear()

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="fundamentals") as pool:
        for i, (item, (result, degraded)) in enumerate(zip(symbols, pool.map(one, symbols), strict=True), 1):
            if isinstance(result, Exception):
                counts["failed"] += 1
            elif result is None:
                counts["empty"] += 1
            elif degraded:
                counts["degraded"] += 1
                price_only.append(result)
                if degraded_out is not None:
                    degraded_out.append(item)
            else:
                pending.append(result)
            if len(pending) + len(price_only) >= BATCH:
                flush()
            if i % 250 == 0:
                log.info("%d/%d done %s", i, len(symbols), counts)
    flush()
    return counts


def main(argv: list[str] | None = None) -> int:
    from utils.db import fundamentals_fresh_symbols
    from utils.logging_setup import configure_logging

    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--resume-hours", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=0.6)
    ap.add_argument("--retry-pause", type=float, default=60.0, help="seconds before retrying degraded quotes")
    args = ap.parse_args(argv)

    todo = universe()
    if args.symbols:
        wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        todo = [t for t in todo if t[0] in wanted]
    if args.resume_hours > 0:
        fresh = fundamentals_fresh_symbols(datetime.now(UTC) - timedelta(hours=args.resume_hours))
        todo = [t for t in todo if t[0] not in fresh]
    if args.limit:
        todo = todo[: args.limit]
    started = time.monotonic()
    log.info("refreshing %d symbols", len(todo))
    degraded: list[tuple[str, str, str]] = []
    counts = refresh(todo, workers=args.workers, delay=args.delay, degraded_out=degraded)
    if degraded:
        # Yahoo throttled part of the run: one slower pass over those symbols
        # once it has cooled down. Still-degraded ones wait for the next run.
        log.warning("%d degraded (price-only) quotes; retrying in %.0fs", len(degraded), args.retry_pause)
        time.sleep(max(0.0, args.retry_pause))
        again = refresh(degraded, workers=1, delay=max(1.0, args.delay * 2))
        counts["written"] += again["written"]
        counts["degraded"] = again["degraded"]
        counts["failed"] += again["failed"]
    log.info("done in %.0fs: %s", time.monotonic() - started, counts)
    return 0 if counts["written"] or not todo else 1


if __name__ == "__main__":
    sys.exit(main())
