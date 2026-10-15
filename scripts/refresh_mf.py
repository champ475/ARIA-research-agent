"""
scripts/refresh_mf.py — warm the mutual fund metrics cache (DATA-04).

Recomputes `mf_metrics` for the categories users ask about most, so the
first "best ELSS funds" of the day doesn't wait on dozens of NAV-history
downloads. Safe to run any time; schedule it nightly after AMFI publishes
(around 23:00 IST).

Usage: python scripts/refresh_mf.py [category ...]
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("refresh_mf")

DEFAULT_CATEGORIES = [
    "elss",
    "large cap",
    "flexi cap",
    "mid cap",
    "small cap",
    "large and mid cap",
    "index",
    "short duration",
    "corporate bond",
    "liquid",
    "aggressive hybrid",
    "balanced advantage",
]


def main(argv: list[str]) -> int:
    configure_logging()
    from tools.data import mf_data
    from tools.data.amfi import get_all_nav

    mf_data.RANK_TIME_BUDGET_SECONDS = 300.0  # no user is waiting here
    nav = get_all_nav()
    for category in argv or DEFAULT_CATEGORIES:
        ranked = mf_data.rank_funds(category, top_n=1000, nav_df=nav)
        log.info(
            f"{category}: metrics for {ranked['cagr_1y'].notna().sum() if not ranked.empty else 0} scheme(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
