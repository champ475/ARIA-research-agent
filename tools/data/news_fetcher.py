"""tools/data/news_fetcher.py — RSS + Google News (free)"""

import logging

import feedparser
import requests
from bs4 import BeautifulSoup

from config import NEWS_FEEDS
from utils import http

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"
}


def _clean(text: str, max_len: int = 250) -> str:
    t = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    return t[:max_len] + "…" if len(t) > max_len else t


def _parse(entry: dict, source: str = "") -> dict:
    return {
        "title"    : entry.get("title", ""),
        "summary"  : _clean(entry.get("summary", "")),
        "link"     : entry.get("link", ""),
        "published": entry.get("published", ""),
        "source"   : source,
    }


def _fetch_feed(url: str, source: str):
    """feedparser.parse(url) has no timeout; fetch through the guarded HTTP
    session (REL-02) and parse the bytes. Network errors propagate, so the
    research task is marked failed and the answer says news is missing."""
    resp = http.get(url, source=source, timeout=(3, 8))
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def google_news(query: str, n: int = 12) -> list[dict]:
    url  = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = _fetch_feed(url, "google_news")
    return [_parse(e, "Google News") for e in feed.entries[:n]]


def rss_feeds(n_per_feed: int = 5) -> list[dict]:
    items = []
    for src, url in NEWS_FEEDS.items():
        try:
            feed = _fetch_feed(url, f"rss:{src}")  # one breaker per feed
            for e in feed.entries[:n_per_feed]:
                items.append(_parse(e, src))
        except Exception as e:
            log.info(f"RSS feed {src} skipped: {e}")
    return items


def company_news(symbol: str, company_name: str = "", n: int = 12) -> list[dict]:
    q = f"{company_name or symbol} NSE BSE India stock 2025"
    return google_news(q, n)


def acquisition_news(company: str = "") -> list[dict]:
    q = f"{company} acquisition merger India 2025" if company else "India company acquisition 2025"
    return google_news(q, 8)


def funding_news(company: str = "") -> list[dict]:
    q = f"{company} funding investment 2025" if company else "India startup funding 2025"
    return google_news(q, 8)


def regulatory_news(company: str = "") -> list[dict]:
    q = f"{company} SEBI RBI regulatory 2025" if company else "SEBI RBI India regulation 2025"
    return google_news(q, 8)


def market_news() -> list[dict]:
    return rss_feeds(n_per_feed=5)
