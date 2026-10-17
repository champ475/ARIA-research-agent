"""
scripts/data_canary.py — one live query per data source, checked against its
contract (TEST-05).

    python scripts/data_canary.py            # all sources; exit 1 if any failed
    python scripts/data_canary.py nse_movers amfi

Runs in the worker every 30 minutes during market hours (`data_canary`
job): a failure is recorded in job_heartbeats, and two in a row page the
operator (OBS-04 `canary_failing`). Also available as a scheduled GitHub
workflow (.github/workflows/data-canary.yml). When it reports drift, refresh
the recordings (scripts/record_http_fixtures.py), then fix the fetcher and
tools/data/contracts.py against them.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("data_canary")


def _yahoo_quote() -> None:
    from tools.data.contracts import check_quote
    from tools.data.providers.yahoo import YFinanceProvider

    check_quote(YFinanceProvider().quote("TCS"))


def _yahoo_bars() -> None:
    from tools.data import fetcher
    from tools.data.contracts import check_bars

    check_bars(fetcher.get_ohlcv("TCS", "1mo", "1d"))


def _nse_movers() -> None:
    from tools.data.nse_scraper import fetch_top_movers

    if fetch_top_movers("gainers").empty:
        raise ValueError("no movers")


def _nse_deals() -> None:
    from tools.data.nse_scraper import fetch_bulk_deals

    fetch_bulk_deals()  # validates the snapshot schema; an empty day is fine


def _nse_bhavcopy() -> None:
    from tools.data.contracts import check_bhavcopy
    from tools.data.nse_scraper import get_bhavcopy

    check_bhavcopy(get_bhavcopy())


def _amfi() -> None:
    from tools.data import amfi
    from tools.data.contracts import check_navall

    check_navall(amfi.get_all_nav())


def _mfapi() -> None:
    from tools.data.mf_data import fetch_history

    if len(fetch_history("122639")) < 100:
        raise ValueError("NAV history too short")


def _news() -> None:
    from tools.data.contracts import check_feed
    from tools.data.news_fetcher import _fetch_feed

    check_feed(
        _fetch_feed("https://news.google.com/rss/search?q=Nifty&hl=en-IN&gl=IN&ceid=IN:en", "google_news")
    )


CHECKS: dict[str, Callable[[], None]] = {
    "yahoo_quote": _yahoo_quote,
    "yahoo_bars": _yahoo_bars,
    "nse_movers": _nse_movers,
    "nse_deals": _nse_deals,
    "nse_bhavcopy": _nse_bhavcopy,
    "amfi": _amfi,
    "mfapi": _mfapi,
    "news": _news,
}


def run(names: list[str] | None = None) -> dict[str, dict]:
    """{source: {ok, ms, error?}} — errors are the exception class and a
    short reason, never a response body."""
    results: dict[str, dict] = {}
    for name in names or list(CHECKS):
        t0 = time.monotonic()
        try:
            CHECKS[name]()
            results[name] = {"ok": True}
        except Exception as e:
            reason = str(e)[:120] if isinstance(e, ValueError) else ""
            results[name] = {"ok": False, "error": f"{type(e).__name__}{': ' + reason if reason else ''}"}
        results[name]["ms"] = int((time.monotonic() - t0) * 1000)
    return results


def job() -> None:
    """The worker's `data_canary` job: raises when a source failed, so the
    heartbeat records it (two in a row page the operator)."""
    results = run()
    failed = {k: v["error"] for k, v in results.items() if not v["ok"]}
    log.info("[data_canary] %s", {k: ("ok" if v["ok"] else v["error"]) for k, v in results.items()})
    if failed:
        raise RuntimeError(f"data canary failed: {', '.join(sorted(failed))}")


def main(argv: list[str] | None = None) -> int:
    from utils.logging_setup import configure_logging

    configure_logging()
    results = run(argv or None)
    sys.stdout.write(
        json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "results": results}, indent=2) + "\n"
    )
    return 0 if all(r["ok"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
