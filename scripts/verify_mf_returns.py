"""
scripts/verify_mf_returns.py — check DATA-04's computed returns against AMFI.

For a sample of direct-growth schemes in three categories, computes the
1-year return and the 3- and 5-year CAGR two independent ways:
  - ARIA: tools/data/mf_data.compute_metrics on the mfapi.in NAV history;
  - AMFI: from AMFI's own published NAVs on the same dates (NAVAll for the
    latest NAV, the historical NAV report for 1, 3 and 5 years earlier).
Reports the largest difference; the PRD bar is 0.1 percentage points.

Usage: python scripts/verify_mf_returns.py [--per-category 4]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.data.mf_data import compute_metrics, fetch_history, parse_navall  # noqa: E402
from utils import http  # noqa: E402

HIST_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={d}&todt={d}"
CATEGORIES = ("ELSS", "Large Cap", "Flexi Cap")


def _amfi_navs_on(d: date) -> dict[str, float]:
    """scheme_code -> NAV on `d` (or the closest earlier day AMFI published)."""
    for back in range(0, 7):
        day = d - timedelta(days=back)
        resp = http.get(HIST_URL.format(d=day.strftime("%d-%b-%Y")), source="amfi", timeout=(5, 120))
        navs = {}
        for line in resp.text.splitlines():
            parts = [p.strip() for p in line.split(";")]
            if len(parts) >= 8 and parts[0].isdigit():
                try:
                    navs[parts[0]] = float(parts[6])
                except ValueError:
                    continue
        if navs:
            return navs
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=4)
    args = ap.parse_args()

    latest = parse_navall(
        http.get("https://portal.amfiindia.com/spages/NAVAll.txt", source="amfi", timeout=(5, 60)).text
    )
    pool = latest[
        (latest["plan"] == "direct") & (latest["option"] == "growth") & (latest["scheme_type"] == "open")
    ]
    end = datetime.strptime(pool["nav_date"].mode().iloc[0], "%d-%b-%Y").date()
    starts = {years: _amfi_navs_on(end.replace(year=end.year - years)) for years in (1, 3, 5)}

    worst = 0.0
    checked = 0
    print(f"End date {end}. Columns: scheme | period | ARIA % | AMFI % | diff pp")
    for category in CATEGORIES:
        sample = pool[(pool["category"] == category) & pool["scheme_code"].isin(starts[5].keys())].head(
            args.per_category
        )
        for _, row in sample.iterrows():
            ours = compute_metrics(fetch_history(row["scheme_code"]))
            for years in (1, 3, 5):
                start_nav = starts[years].get(row["scheme_code"])
                mine = ours.get(f"cagr_{years}y")
                if start_nav is None or mine is None:
                    continue
                ratio = row["nav"] / start_nav
                amfi = (ratio - 1) * 100 if years == 1 else (ratio ** (1 / years) - 1) * 100
                diff = abs(mine - amfi)
                worst = max(worst, diff)
                checked += 1
                print(f"{row['scheme_name'][:48]:48} | {years}y | {mine:7.2f} | {amfi:7.2f} | {diff:.3f}")
    print(f"\n{checked} returns compared; largest difference {worst:.3f} pp (bar: 0.1 pp)")
    return 0 if checked and worst <= 0.1 else 1


if __name__ == "__main__":
    sys.exit(main())
