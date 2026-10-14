"""
scripts/refresh_instruments.py — refresh the vendored instrument master (VAL-02).

Downloads NSE's EQUITY_L.csv and the latest BSE equity bhavcopy (for scrip
code -> ISIN), and rewrites data/instruments/. Review the diff before
committing: renamed or delisted symbols may need an entry in
tools/data/instruments.py::RENAMED, and the curated universe (config.py)
must only contain listed symbols (tests/test_instruments.py checks).

Usage: python scripts/refresh_instruments.py
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.data.instruments import BSE_FILE, NSE_FILE  # noqa: E402
from utils import http  # noqa: E402
from utils.logging_setup import configure_logging  # noqa: E402

log = logging.getLogger("refresh_instruments")

NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_URL = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d}_F_0000.CSV"


def refresh_nse() -> int:
    resp = http.get(
        NSE_URL, source="nse", headers={**http.BROWSER_HEADERS, "Referer": "https://www.nseindia.com/"}
    )
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    if len(rows) < 1000 or "SYMBOL" not in rows[0]:
        raise RuntimeError(f"NSE list looks wrong ({len(rows)} rows) — not overwriting")
    NSE_FILE.write_text(resp.text, encoding="utf-8")
    return len(rows)


def refresh_bse(days_back: int = 7) -> int:
    headers = {**http.BROWSER_HEADERS, "Referer": "https://www.bseindia.com/"}
    for i in range(days_back):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        resp = http.get(
            BSE_URL.format(d=d.strftime("%Y%m%d")), source="bse", headers=headers, timeout=(3, 30)
        )
        if resp.status_code != 200 or len(resp.content) < 10_000:
            continue
        out, seen = [], set()
        for row in csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))):
            code = (row.get("FinInstrmId") or "").strip()
            if row.get("FinInstrmTp") != "STK" or not code or code in seen:
                continue
            seen.add(code)
            out.append((code, row["ISIN"].strip(), row["TckrSymb"].strip(), row["FinInstrmNm"].strip()))
        with open(BSE_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["bse_code", "isin", "bse_symbol", "name"])
            writer.writerows(sorted(out))
        return len(out)
    raise RuntimeError(f"no BSE bhavcopy found in the last {days_back} days")


def main() -> int:
    configure_logging()
    log.info(f"NSE: {refresh_nse()} instruments -> {NSE_FILE}")
    log.info(f"BSE: {refresh_bse()} scrip codes -> {BSE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
