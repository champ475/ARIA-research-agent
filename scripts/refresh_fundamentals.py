"""
scripts/refresh_fundamentals.py — nightly fundamentals snapshot (DATA-05).

    python scripts/refresh_fundamentals.py                  # whole NSE EQ universe
    python scripts/refresh_fundamentals.py --limit 100      # first 100 (a smoke run)
    python scripts/refresh_fundamentals.py --symbols TCS,SBIN
    python scripts/refresh_fundamentals.py --resume-hours 0 # refetch everything

Walks every EQ-series symbol in the instrument master (data/instruments/),
fetches its quote through the market-data provider (bounded, breaker-guarded,
DATA-02) and upserts the screen fields into `fundamentals_snapshot`. A rerun
skips symbols refreshed within --resume-hours (default 20), so a run cut
short resumes where it stopped. Calls are spread over --workers threads with
a --delay between each worker's calls to stay under Yahoo's rate limits.

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
    symbols: list[tuple[str, str, str]], workers: int = 4, delay: float = 0.25, fetch=None
) -> dict[str, int]:
    """Fetch and upsert; returns counts {written, empty, failed}."""
    from tools.data.providers import market_data
    from utils.db import fundamentals_upsert

    quote = fetch or (lambda s: market_data().quote(s))
    counts = {"written": 0, "empty": 0, "failed": 0}
    pending: list[dict[str, Any]] = []

    def one(item: tuple[str, str, str]) -> dict[str, Any] | None | Exception:
        symbol, name, isin = item
        try:
            row = snapshot_row(symbol, name, isin, quote(symbol))
        except Exception as e:
            return e
        finally:
            if delay:
                time.sleep(delay)
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="fundamentals") as pool:
        for i, result in enumerate(pool.map(one, symbols), 1):
            if isinstance(result, Exception):
                counts["failed"] += 1
            elif result is None:
                counts["empty"] += 1
            else:
                pending.append(result)
            if len(pending) >= BATCH:
                counts["written"] += fundamentals_upsert(pending)
                pending.clear()
            if i % 250 == 0:
                log.info("%d/%d done %s", i, len(symbols), counts)
    if pending:
        counts["written"] += fundamentals_upsert(pending)
    return counts


def main(argv: list[str] | None = None) -> int:
    from utils.db import fundamentals_fresh_symbols
    from utils.logging_setup import configure_logging

    configure_logging()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--resume-hours", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.25)
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
    counts = refresh(todo, workers=args.workers, delay=args.delay)
    log.info("done in %.0fs: %s", time.monotonic() - started, counts)
    return 0 if counts["written"] or not todo else 1


if __name__ == "__main__":
    sys.exit(main())
