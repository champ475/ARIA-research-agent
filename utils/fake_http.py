"""
utils/fake_http.py — offline answers for outbound HTTP under ARIA_FAKE_DATA
(TEST-04, DATA-02).

utils/http.GuardedSession hands every request here when the flag is on, so
e2e tests and demos never touch the network: known sources are served from
recorded responses shipped in data/fake/ (refreshed alongside
tests/fixtures/http by scripts/record_http_fixtures.py), and anything else
gets a 503, which the fetchers already treat as "source unavailable".
Market quotes and history come from the FakeProvider, not from here.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parents[1] / "data" / "fake"

ROUTES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"amfiindia\.com/.*NAVAll\.txt"), "amfi_navall.txt", "text/plain; charset=utf-8"),
    (re.compile(r"api\.mfapi\.in/mf/\d+"), "mfapi_122639.json", "application/json"),
    (re.compile(r"news\.google\.com/rss"), "google_news_TCS.xml", "application/rss+xml"),
    (re.compile(r"economictimes\.indiatimes\.com/.*rss"), "rss_economic_times.xml", "application/rss+xml"),
    (
        re.compile(r"nseindia\.com/api/snapshot-capital-market-largedeal"),
        "nse_large_deals.json",
        "application/json",
    ),
    (
        re.compile(r"nseindia\.com/api/corporates-corporateActions"),
        "nse_corporate_actions_TCS.json",
        "application/json",
    ),
    (
        re.compile(r"nsearchives\.nseindia\.com/products/content/sec_bhavdata_full_"),
        "nse_bhavcopy.csv",
        "text/csv",
    ),
]


def respond(method: str, url: str) -> requests.Response:
    resp = requests.Response()
    resp.url = url
    resp.request = requests.Request(method, url).prepare()
    for pattern, name, ctype in ROUTES:
        if pattern.search(url):
            resp.status_code = 200
            resp._content = (DATA / name).read_bytes()
            resp.headers["Content-Type"] = ctype
            resp.encoding = "utf-8"
            return resp
    resp.status_code = 503
    resp._content = b"offline (ARIA_FAKE_DATA)"
    return resp
