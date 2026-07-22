"""tools/data/news_fetcher.py — RSS + Google News (free)"""

import feedparser
import requests
from bs4 import BeautifulSoup
from config import NEWS_FEEDS
from langchain_core.tools import tool

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


@tool(description="Search Google News RSS for a query and return parsed news items.")
def google_news(query: str, n: int = 12) -> list[dict]:
    url  = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = feedparser.parse(url)
    return [_parse(e, "Google News") for e in feed.entries[:n]]


@tool(description="Fetch recent items from all configured RSS news feeds.")
def rss_feeds(n_per_feed: int = 5) -> list[dict]:
    items = []
    for src, url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:n_per_feed]:
                items.append(_parse(e, src))
        except Exception:
            pass
    return items


@tool(description="Fetch recent news for a specific NSE/BSE-listed company.")
def company_news(symbol: str, company_name: str = "", n: int = 12) -> list[dict]:
    q = f"{company_name or symbol} NSE BSE India stock 2025"
    return google_news.func(q, n)


@tool(description="Fetch recent acquisition/merger news for a company or India in general.")
def acquisition_news(company: str = "") -> list[dict]:
    q = f"{company} acquisition merger India 2025" if company else "India company acquisition 2025"
    return google_news.func(q, 8)


@tool(description="Fetch recent funding/investment news for a company or Indian startups in general.")
def funding_news(company: str = "") -> list[dict]:
    q = f"{company} funding investment 2025" if company else "India startup funding 2025"
    return google_news.func(q, 8)


@tool(description="Fetch recent SEBI/RBI regulatory news for a company or India in general.")
def regulatory_news(company: str = "") -> list[dict]:
    q = f"{company} SEBI RBI regulatory 2025" if company else "SEBI RBI India regulation 2025"
    return google_news.func(q, 8)


@tool(description="Fetch general market news from all configured RSS feeds.")
def market_news() -> list[dict]:
    return rss_feeds.func(n_per_feed=5)
