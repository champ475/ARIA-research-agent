"""
tools/screener.py — stock screener (VAL-06, DATA-05).

parse_query() turns the user's words into a Screen (tools/screener_grammar.py)
and returns it as a JSON-safe dict; run() applies every filter; to_markdown()
renders the table *and* the "Filters applied / Not understood" summary, so
nothing the user asked for is silently dropped.

DATA-05: run() is one SQL query over `fundamentals_snapshot` — every NSE
EQ-series stock, refreshed nightly by scripts/refresh_fundamentals.py. Until
a snapshot exists it falls back to fetching the curated list live (first
`max_stocks`), and the answer says which universe was screened and how old
the snapshot is (stale after FUNDAMENTALS_MAX_AGE_HOURS).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from config import FUNDAMENTALS_MAX_AGE_HOURS, SECTOR_UNIVERSE, STOCK_UNIVERSE
from tools.analysis.sector_profile import LENDER, sector_profile
from tools.data.fetcher import get_info
from tools.screener_grammar import YF_SECTORS, Screen, ScreenFilter, parse
from utils.escaping import md_cell
from utils.helpers import safe_float

log = logging.getLogger(__name__)

# Old saved screens (and old cached evidence) used flat keys.
_LEGACY_KEYS: dict[str, tuple[str, str]] = {
    "max_pe": ("pe", "<="),
    "min_pe": ("pe", ">="),
    "max_de": ("de", "<="),
    "min_roce": ("roce", ">="),
    "min_roe": ("roe", ">="),
    "min_rev_growth": ("rev_growth", ">="),
    "min_mktcap_cr": ("mcap", ">="),
    "max_mktcap_cr": ("mcap", "<="),
}


def parse_query(query: str) -> dict:
    """The user's screen as a JSON-safe dict (Screen.as_dict())."""
    return parse(query).as_dict()


def as_screen(filters: dict | None) -> Screen:
    """Screen from a parse_query() dict, or from a legacy flat dict."""
    filters = filters or {}
    if "filters" in filters:
        return Screen.from_dict(filters)
    screen = Screen(sector=filters.get("sector"))
    for key, (fname, op) in _LEGACY_KEYS.items():
        if filters.get(key) is not None:
            screen.filters.append(ScreenFilter(fname, op, float(filters[key])))
    return screen


def metrics_from_info(info: dict) -> dict[str, float | None]:
    """The screenable numbers for one stock, in the units filters use."""
    price = safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    de_raw = safe_float(info.get("debtToEquity"))
    roe = safe_float(info.get("returnOnEquity"))
    rg = safe_float(info.get("revenueGrowth"))
    mc = safe_float(info.get("marketCap"))
    ebit = safe_float(info.get("ebit"))
    ta = safe_float(info.get("totalAssets"))
    cl = safe_float(info.get("totalCurrentLiabilities"))
    hi52 = safe_float(info.get("fiftyTwoWeekHigh"))
    lo52 = safe_float(info.get("fiftyTwoWeekLow"))
    trailing_yield = safe_float(info.get("trailingAnnualDividendYield"))  # a fraction
    lender = sector_profile(info) == LENDER  # DATA-03: no ROCE or D/E for lenders
    return {
        "pe": safe_float(info.get("trailingPE")),
        "pb": safe_float(info.get("priceToBook")),
        "roe": round(roe * 100, 2) if roe is not None else None,
        "roce": round(ebit / (ta - cl) * 100, 2)
        if (ebit and ta and cl and ta != cl and not lender)
        else None,
        # yfinance reports D/E as a percentage (45.2 == 0.452x), always (DATA-03).
        "de": None if lender or de_raw is None else round(de_raw / 100, 2),
        "mcap": round(mc / 1e7, 2) if mc else None,
        "div_yield": round(trailing_yield * 100, 2) if trailing_yield is not None else None,
        "rev_growth": round(rg * 100, 2) if rg is not None else None,
        "price": price,
        "from_52w_high": round((hi52 - price) / hi52 * 100, 2) if (hi52 and price) else None,
        "from_52w_low": round((price - lo52) / lo52 * 100, 2) if (lo52 and price) else None,
    }


def passes(f: ScreenFilter, value: float | None) -> bool:
    """A stock without the metric fails the filter (unknown is not a match)."""
    if value is None:
        return False
    if f.op == "<":
        return value < f.value
    if f.op == "<=":
        return value <= f.value
    if f.op == ">":
        return value > f.value
    if f.op == ">=":
        return value >= f.value
    if f.op == "between":
        return f.value <= value <= (f.high if f.high is not None else f.value)
    return abs(value - f.value) <= max(abs(f.value) * 0.01, 0.01)  # "=" within 1%


def snapshot_status() -> dict[str, Any]:
    """{rows, oldest, newest}; rows=0 when the table is empty or unreachable."""
    try:
        from utils.db import fundamentals_status

        return fundamentals_status()
    except Exception as e:
        log.warning("[screener] snapshot status unavailable: %s", type(e).__name__)
        return {"rows": 0, "oldest": None, "newest": None}


def run(filters: dict, max_stocks: int = 30, limit: int = 50) -> list[dict]:
    screen = as_screen(filters)
    if screen.contradictions:
        return []
    if snapshot_status()["rows"]:
        try:
            return _run_snapshot(screen, limit)
        except Exception as e:
            log.warning("[screener] snapshot query failed (%s); screening live", type(e).__name__)
    return _run_live(screen, max_stocks)


def _r(v: Any, nd: int) -> float | None:
    return round(float(v), nd) if v is not None else None


def _run_snapshot(screen: Screen, limit: int) -> list[dict]:
    from utils.db import fundamentals_screen

    sectors = sorted(YF_SECTORS.get(screen.sector, {screen.sector})) if screen.sector else None
    rows = fundamentals_screen([(f.field, f.op, f.value, f.high) for f in screen.filters], sectors, limit)
    return [
        {
            "symbol": r["symbol"],
            "name": (r["name"] or r["symbol"])[:28],
            "sector": (r["sector"] or "")[:18],
            "pe": _r(r["pe"], 1),
            "pb": _r(r["pb"], 2),
            "de": _r(r["de"], 2),
            "roe": _r(r["roe"], 1),
            "roce": _r(r["roce"], 1),
            "rev_growth": _r(r["rev_growth"], 1),
            "div_yield": _r(r["div_yield"], 2),
            "mktcap_cr": _r(r["mcap_cr"], 0),
            "price": _r(r["price"], 2),
            "source": "snapshot",
            "as_of": r["as_of"].isoformat() if r.get("as_of") else None,
        }
        for r in rows
    ]


def _run_live(screen: Screen, max_stocks: int) -> list[dict]:
    universe = SECTOR_UNIVERSE.get(screen.sector or "", STOCK_UNIVERSE)[:max_stocks]
    wanted_sectors = YF_SECTORS.get(screen.sector or "")
    results = []
    for sym in universe:
        try:
            info = get_info(sym)
            if not info:
                continue
            sec = info.get("sector", "") or ""
            if wanted_sectors and sec not in wanted_sectors and screen.sector not in sec:
                continue
            m = metrics_from_info(info)
            if not all(passes(f, m.get(f.field)) for f in screen.filters):
                continue
            results.append(
                {
                    "symbol": sym,
                    "name": (info.get("shortName") or sym)[:28],
                    "sector": sec[:18],
                    "pe": round(m["pe"], 1) if m["pe"] else None,
                    "pb": round(m["pb"], 2) if m["pb"] else None,
                    "de": m["de"],
                    "roe": round(m["roe"], 1) if m["roe"] is not None else None,
                    "roce": round(m["roce"], 1) if m["roce"] is not None else None,
                    "rev_growth": round(m["rev_growth"], 1) if m["rev_growth"] is not None else None,
                    "div_yield": m["div_yield"],
                    "mktcap_cr": round(m["mcap"], 0) if m["mcap"] else None,
                    "price": m["price"],
                    "source": "live",
                }
            )
        except Exception:
            continue
    results.sort(key=lambda x: x.get("mktcap_cr") or 0, reverse=True)
    return results


def coverage_note(results: list[dict], now: datetime | None = None) -> str:
    """Which universe was screened, and whether the snapshot is stale."""
    status = snapshot_status()
    live = (results and results[0].get("source") == "live") or not status["rows"]
    if live:
        return (
            "*Screened a curated list of large companies, fetched live — the full-market "
            "snapshot isn't loaded yet.*"
        )
    note = f"*Screened {status['rows']:,} NSE stocks from the nightly fundamentals snapshot.*"
    oldest = status.get("oldest")
    if oldest is not None:
        age_h = ((now or datetime.now(UTC)) - oldest).total_seconds() / 3600
        if age_h > FUNDAMENTALS_MAX_AGE_HOURS:
            note += f" ⚠️ *Some figures are {age_h:.0f} hours old — the nightly refresh hasn't run.*"
    return note


def to_markdown(results: list[dict], filters: dict) -> str:
    """The results table plus what was applied and what was ignored."""
    screen = as_screen(filters)
    summary = screen.summary()
    if screen.contradictions:
        return f"{summary}\n\nNo stock can match these filters together — please adjust them."
    coverage = coverage_note(results)
    if not results:
        return f"{summary}\n\nNo stocks in the screened universe matched.\n\n{coverage}"
    lines = [
        "| # | Symbol | Name | Sector | P/E | P/B | D/E | ROE% | ROCE% | Rev Gr% | MCap(Cr) | Price |",
        "|---|--------|------|--------|-----|-----|-----|------|-------|---------|----------|-------|",
    ]
    for i, r in enumerate(results[:20], 1):

        def _v(k: str, r: dict[str, Any] = r) -> str:
            return str(r.get(k)) if r.get(k) is not None else "N/A"

        lines.append(
            f"| {i} | **{md_cell(r['symbol'])}** | {md_cell(r['name'])} | {md_cell(r['sector'])} | "
            f"{_v('pe')} | {_v('pb')} | {_v('de')} | {_v('roe')} | {_v('roce')} | "
            f"{_v('rev_growth')} | {_v('mktcap_cr')} | ₹{_v('price')} |"
        )
    more = f"\n\nShowing the 20 largest of {len(results)} matches." if len(results) > 20 else ""
    return summary + "\n\n" + "\n".join(lines) + more + "\n\n" + coverage
