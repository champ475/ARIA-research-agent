"""
scripts/record_http_fixtures.py — refresh the recorded HTTP responses the
data-contract tests replay (TEST-05).

    python scripts/record_http_fixtures.py            # every source
    python scripts/record_http_fixtures.py amfi nse_bhavcopy

Fetches one known query per source and writes the body, trimmed to a
reviewable size, to tests/fixtures/http/ (index.json records URL, status and
when). Refresh monthly, or when the canary (scripts/data_canary.py) reports
schema drift — then fix the parser and the contract (tools/data/contracts.py)
against the new fixture. A source that refuses us (NSE blocks some IPs) is
reported and its old fixture kept.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger("record_http_fixtures")

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "http"
NSE = "https://www.nseindia.com"


def _last_weekday() -> datetime:
    d = datetime.now(UTC) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _trim_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[:n]) + "\n"


def _trim_items(xml: str, n: int) -> str:
    items = re.findall(r"<item>.*?</item>", xml, flags=re.S)
    if len(items) <= n:
        return xml
    head = xml[: xml.index(items[0])]
    tail = xml[xml.index(items[-1]) + len(items[-1]) :]
    return head + "".join(items[:n]) + tail


def _trim_json(body: bytes, key: str | None, n: int) -> str:
    data = json.loads(body)
    if key and isinstance(data, dict) and isinstance(data.get(key), list):
        data[key] = data[key][:n]
    elif isinstance(data, list):
        data = data[:n]
    return json.dumps(data, indent=1, ensure_ascii=False)


SOURCES = {
    # name: (url, file, trim)
    "nse_gainers": (f"{NSE}/api/live-analysis-variations?index=gainers", "nse_gainers.json", None),
    "nse_large_deals": (
        f"{NSE}/api/snapshot-capital-market-largedeal",
        "nse_large_deals.json",
        ("deals", 15),
    ),
    "nse_corporate_actions": (
        f"{NSE}/api/corporates-corporateActions?index=equities&symbol=TCS",
        "nse_corporate_actions_TCS.json",
        ("json", None, 20),
    ),
    "nse_bhavcopy": (
        "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv",
        "nse_bhavcopy.csv",
        ("lines", 80),
    ),
    "amfi": ("https://portal.amfiindia.com/spages/NAVAll.txt", "amfi_navall.txt", ("lines", 400)),
    "mfapi": ("https://api.mfapi.in/mf/122639", "mfapi_122639.json", ("json", "data", 1300)),
    "google_news": (
        "https://news.google.com/rss/search?q=TCS%20share&hl=en-IN&gl=IN&ceid=IN:en",
        "google_news_TCS.xml",
        ("items", 8),
    ),
    "rss_economic_times": (
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "rss_economic_times.xml",
        ("items", 8),
    ),
    "bse_filings": (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode=532540&type=Quarterly",
        "bse_filings_532540.json",
        ("json", "Table", 10),
    ),
}


def record(name: str) -> dict:
    from utils import http

    url, filename, trim = SOURCES[name]
    url = url.format(date=_last_weekday().strftime("%d%m%Y"))
    headers = {**http.BROWSER_HEADERS, "Referer": f"{NSE}/"}
    if name.startswith("nse"):
        s = http.session("nse", headers=headers)
        s.get(NSE, timeout=10)
        r = s.get(url, timeout=20)
    else:
        r = http.get(url, source=name, headers=headers, timeout=(5, 30))
    entry = {
        "url": url,
        "status": r.status_code,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if r.status_code != 200 or not r.content:
        entry["kept_old"] = True
        return entry
    if trim is None:
        body = r.text
    elif trim[0] == "json":
        body = _trim_json(r.content, trim[1], trim[2])
    elif trim[0] == "deals":
        data = json.loads(r.content)
        for key in ("BULK_DEALS_DATA", "BLOCK_DEALS_DATA", "SHORT_DEALS_DATA"):
            if isinstance(data.get(key), list):
                data[key] = data[key][: trim[1]]
        body = json.dumps(data, indent=1, ensure_ascii=False)
    elif trim[0] == "lines":
        body = _trim_lines(r.content.decode("utf-8-sig", errors="replace"), trim[1])
    else:
        body = _trim_items(r.content.decode("utf-8", errors="replace"), trim[1])
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / filename).write_text(body, encoding="utf-8", newline="\n")
    entry["file"] = filename
    return entry


def main(argv: list[str]) -> int:
    from utils.logging_setup import configure_logging

    configure_logging()
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    for name in argv or list(SOURCES):
        try:
            index[name] = {**index.get(name, {}), **record(name)}
        except Exception as e:
            index.setdefault(name, {})["last_error"] = type(e).__name__
        log.info("%s: %s", name, index[name])
    OUT.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
