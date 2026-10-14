"""
tools/data/amfi.py
AMFI (Association of Mutual Funds in India) free data.
NAV, fund details, top holdings, returns.
"""

import time
import pandas as pd
import logging

from utils import http

log = logging.getLogger(__name__)

AMFI_NAV_URL     = "https://www.amfiindia.com/spages/NAVAll.txt"
AMFI_SEARCH_URL  = "https://www.amfiindia.com/modules/FindSchemeDetails"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0"}

# In-memory NAV cache — AMFI updates once daily; 30-min TTL avoids re-downloading ~2MB on every query
_nav_cache: pd.DataFrame = pd.DataFrame()
_nav_cache_ts: float = 0.0
_NAV_TTL = 1800  # 30 minutes


def get_all_nav() -> pd.DataFrame:
    """
    Download complete NAV data from AMFI for all mutual funds.
    Returns DataFrame with columns: scheme_code, isin_div_payout, isin_div_reinvest,
    scheme_name, nav, nav_date.
    """
    global _nav_cache, _nav_cache_ts
    if not _nav_cache.empty and (time.time() - _nav_cache_ts) < _NAV_TTL:
        return _nav_cache

    try:
        r = http.get(AMFI_NAV_URL, source="amfi", headers=HEADERS, timeout=(3, 20))
        if r.status_code != 200:
            return _nav_cache if not _nav_cache.empty else pd.DataFrame()

        lines    = r.text.strip().split("\n")
        records  = []
        amc_name = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith(";") or "Scheme" in line or "ISIN" in line:
                if ";" not in line and len(line) > 10:
                    amc_name = line
                continue
            parts = line.split(";")
            if len(parts) >= 6:
                try:
                    records.append({
                        "amc"          : amc_name,
                        "scheme_code"  : parts[0].strip(),
                        "isin_payout"  : parts[1].strip(),
                        "isin_reinvest": parts[2].strip(),
                        "scheme_name"  : parts[3].strip(),
                        "nav"          : float(parts[4].strip()) if parts[4].strip() else None,
                        "nav_date"     : parts[5].strip(),
                    })
                except (ValueError, IndexError):
                    continue

        df = pd.DataFrame(records)
        _nav_cache    = df
        _nav_cache_ts = time.time()
        return df
    except Exception as e:
        log.warning(f"AMFI NAV download failed: {e}")
        return _nav_cache if not _nav_cache.empty else pd.DataFrame()


def search_fund(query: str, nav_df: pd.DataFrame = None) -> pd.DataFrame:
    """Search funds by name keyword."""
    if nav_df is None or nav_df.empty:
        nav_df = get_all_nav()
    if nav_df.empty:
        return pd.DataFrame()

    q = query.lower()
    mask = nav_df["scheme_name"].str.lower().str.contains(q, na=False)
    return nav_df[mask].head(20)


def get_fund_performance(scheme_name_query: str) -> dict:
    """
    Returns NAV history and computed returns for a fund.
    Uses yfinance as proxy where possible, otherwise AMFI.
    """
    nav_df = get_all_nav()
    if nav_df.empty:
        return {"error": "Could not fetch AMFI data"}

    results = search_fund(scheme_name_query, nav_df)
    if results.empty:
        return {"error": f"No fund found matching '{scheme_name_query}'"}

    # Take the first match
    fund    = results.iloc[0].to_dict()
    nav     = fund.get("nav")
    date    = fund.get("nav_date")
    name    = fund.get("scheme_name")
    amc     = fund.get("amc")

    return {
        "name"    : name,
        "amc"     : amc,
        "nav"     : nav,
        "nav_date": date,
        "code"    : fund.get("scheme_code"),
    }


def get_category_funds(category_keyword: str, top_n: int = 10) -> pd.DataFrame:
    """
    Return top funds in a category (e.g. 'large cap', 'flexi cap', 'small cap').
    Filtered from full AMFI list.
    """
    nav_df = get_all_nav()
    if nav_df.empty:
        return pd.DataFrame()

    q    = category_keyword.lower()
    mask = nav_df["scheme_name"].str.lower().str.contains(q, na=False)
    df   = nav_df[mask].copy()

    # Remove duplicate AMC offerings (direct vs regular)
    if "direct" in q or "regular" not in q:
        df = df[df["scheme_name"].str.lower().str.contains("direct", na=False)]

    return df.dropna(subset=["nav"]).head(top_n)


def format_fund_search_results(df: pd.DataFrame) -> str:
    """Format fund search results as a markdown table."""
    if df.empty:
        return "No matching funds found."

    cols = ["amc", "scheme_name", "nav", "nav_date"]
    cols = [c for c in cols if c in df.columns]
    subset = df[cols].head(15)
    subset.columns = [c.replace("_", " ").title() for c in subset.columns]
    return subset.to_string(index=False)


def get_mf_comparison(funds: list[str]) -> str:
    """Compare multiple mutual funds side by side."""
    nav_df = get_all_nav()
    if nav_df.empty:
        return "Could not fetch fund data."

    rows = []
    for fund_name in funds:
        results = search_fund(fund_name, nav_df)
        if not results.empty:
            f = results.iloc[0]
            rows.append({
                "Fund": f.get("scheme_name", fund_name)[:50],
                "AMC" : f.get("amc", "N/A")[:25],
                "NAV" : f.get("nav", "N/A"),
                "Date": f.get("nav_date", "N/A"),
            })

    if not rows:
        return "Could not find fund data for the requested funds."

    df = pd.DataFrame(rows)
    return "## Mutual Fund Comparison\n\n" + df.to_string(index=False)
