"""tools/screener.py — stock screener, NO tabulate dependency"""

import re

from config import SECTOR_UNIVERSE, STOCK_UNIVERSE
from tools.data.fetcher import get_info
from utils.helpers import safe_float


def parse_query(query: str) -> dict:
    filters = {}
    q = query.lower()
    for kw, sec in {
        "it": "Technology",
        "technology": "Technology",
        "tech": "Technology",
        "bank": "Financial Services",
        "banking": "Financial Services",
        "pharma": "Healthcare",
        "healthcare": "Healthcare",
        "auto": "Consumer Cyclical",
        "energy": "Energy",
        "metal": "Metals & Mining",
        "fmcg": "Consumer Defensive",
        "infra": "Industrials",
        "realty": "Real Estate",
        "telecom": "Telecom",
    }.items():
        if kw in q:
            filters["sector"] = sec
            break

    for pattern, key in [
        (r"p/?e\s*(?:below|under|<|lt)\s*(\d+)", "max_pe"),
        (r"p/?e\s*(?:above|over|>|gt)\s*(\d+)", "min_pe"),
        (r"d/?e\s*(?:below|under|<)\s*([\d.]+)", "max_de"),
        (r"roce?\s*(?:above|over|>)\s*(\d+)", "min_roce"),
        (r"roe?\s*(?:above|over|>)\s*(\d+)", "min_roe"),
        (r"(?:rev|revenue|sales)\s*growth\s*(?:above|>)\s*(\d+)", "min_rev_growth"),
        (r"(?:market\s*cap|mktcap)\s*(?:above|>)\s*(\d+)", "min_mktcap_cr"),
    ]:
        m = re.search(pattern, q)
        if m:
            filters[key] = float(m.group(1))

    if "large cap" in q or "largecap" in q:
        filters["min_mktcap_cr"] = 20000
    elif "mid cap" in q or "midcap" in q:
        filters.update({"min_mktcap_cr": 5000, "max_mktcap_cr": 20000})
    elif "small cap" in q or "smallcap" in q:
        filters["max_mktcap_cr"] = 5000
    return filters


def run(filters: dict, max_stocks: int = 30) -> list[dict]:
    universe = SECTOR_UNIVERSE.get(filters.get("sector", ""), STOCK_UNIVERSE)[:max_stocks]
    results = []
    for sym in universe:
        try:
            info = get_info(sym)
            if not info:
                continue
            pe = safe_float(info.get("trailingPE"))
            de_raw = safe_float(info.get("debtToEquity"))
            de = round(de_raw / 100, 2) if de_raw and de_raw > 20 else de_raw
            roe = safe_float(info.get("returnOnEquity"))
            rg = safe_float(info.get("revenueGrowth"))
            mc = safe_float(info.get("marketCap")) or 0
            sec = info.get("sector", "")

            ebit = safe_float(info.get("ebit"))
            ta = safe_float(info.get("totalAssets"))
            cl = safe_float(info.get("totalCurrentLiabilities"))
            roce = round(ebit / (ta - cl) * 100, 1) if (ebit and ta and cl and (ta - cl) != 0) else None
            mc_cr = mc / 1e7

            if filters.get("sector") and filters["sector"].lower() not in (sec or "").lower():
                continue
            if filters.get("max_pe") and pe and pe > filters["max_pe"]:
                continue
            if filters.get("min_pe") and pe and pe < filters["min_pe"]:
                continue
            if filters.get("max_de") and de and de > filters["max_de"]:
                continue
            if filters.get("min_roe") and roe and roe * 100 < filters["min_roe"]:
                continue
            if filters.get("min_roce") and roce and roce < filters["min_roce"]:
                continue
            if filters.get("min_rev_growth") and rg and rg * 100 < filters["min_rev_growth"]:
                continue
            if filters.get("min_mktcap_cr") and mc_cr < filters["min_mktcap_cr"]:
                continue
            if filters.get("max_mktcap_cr") and mc_cr > filters["max_mktcap_cr"]:
                continue

            results.append(
                {
                    "symbol": sym,
                    "name": (info.get("shortName") or sym)[:28],
                    "sector": sec[:18],
                    "pe": round(pe, 1) if pe else None,
                    "de": round(de, 2) if de else None,
                    "roe": round(roe * 100, 1) if roe else None,
                    "roce": roce,
                    "rev_growth": round(rg * 100, 1) if rg else None,
                    "mktcap_cr": round(mc_cr, 0),
                    "price": info.get("currentPrice") or info.get("regularMarketPrice"),
                }
            )
        except Exception:
            continue
    results.sort(key=lambda x: x.get("mktcap_cr") or 0, reverse=True)
    return results


def to_markdown(results: list[dict], filters: dict) -> str:
    """Pure string formatting — NO tabulate dependency."""
    if not results:
        return f"No stocks matched filters: {filters}"

    # Build header
    lines = [
        "| # | Symbol | Name | Sector | P/E | D/E | ROE% | ROCE% | Rev Gr% | MCap(Cr) | Price |",
        "|---|--------|------|--------|-----|-----|------|-------|---------|----------|-------|",
    ]
    for i, r in enumerate(results[:20], 1):

        def _v(k, r=r):
            return str(r.get(k)) if r.get(k) is not None else "N/A"

        lines.append(
            f"| {i} | **{r['symbol']}** | {r['name']} | {r['sector']} | "
            f"{_v('pe')} | {_v('de')} | {_v('roe')} | {_v('roce')} | "
            f"{_v('rev_growth')} | {_v('mktcap_cr')} | ₹{_v('price')} |"
        )
    return "\n".join(lines)
